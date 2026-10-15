#!/usr/bin/env python3
"""Update the firmware of many Makeblock CyberPis at once.

mBlock's "Update firmware" button writes a single ESP32 flash image to the
CyberPi through the chip's ROM bootloader, which is exactly what esptool does.
This script downloads that image from Makeblock and flashes it to every
CyberPi plugged into this computer, in parallel.

    python cyberpi_updater.py fetch    # download the current firmware
    python cyberpi_updater.py list     # show connected CyberPis
    python cyberpi_updater.py flash    # flash all connected CyberPis once
    python cyberpi_updater.py watch    # flash every CyberPi as it is plugged in
    python cyberpi_updater.py backup   # save the whole flash of one CyberPi
    python cyberpi_updater.py restore FILE   # write such a backup back
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is missing. Run: pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent
FIRMWARE_DIR = ROOT / "firmware"
FIRMWARE_META = FIRMWARE_DIR / "latest.json"
LOG_DIR = ROOT / "logs"
BACKUP_DIR = ROOT / "backups"

# USB-serial chip on the CyberPi (WCH CH340), as declared by mBlock's CyberPi extension.
USB_VID, USB_PID = 0x1A86, 0x7523

# Flash parameters used by mBlock's built-in ESP32 flasher for the CyberPi.
FLASH_ADDR = "0x1000"
FLASH_ARGS = ["--flash-mode", "dio", "--flash-size", "8MB", "--flash-freq", "keep"]
FLASH_SIZE = 8 * 1024 * 1024
PARTITION_TABLE_ADDR = 0x8000
PARTITION_TYPE_APP = 0

# The CyberPi's CH340 stops responding at 921600 baud. 460800 works for writing (every
# block is acknowledged and checksummed), but reading back at 460800 loses bytes.
DEFAULT_BAUD = 460800
READ_BAUD = 230400

# mBlock web loads the CyberPi extension from here; the extension names the firmware file.
EXT_LIST_URL = (
    "https://extservice.mblock.cc/api/publish/ext/list"
    "?ap=mblockweb&v=beta&ids=cyberpi&pageNumber=1&pageSize=10"
)
FIRMWARE_RE = re.compile(
    r'firmware:\[\{id:"\w+",name:"[^"]*",version:"([\d.]+)"'
    r'.{0,400}?getExtResPath\("([^"]+\.bin)"',
    re.S,
)

PROGRESS_RE = re.compile(r"(Writing|Reading) (?:at|from) .*?(\d+(?:\.\d+)?)%")
MAC_RE = re.compile(r"MAC:\s+([0-9a-f]{2}(?::[0-9a-f]{2}){5})")
ESP_IMAGE_MAGIC = 0xE9

print_lock = threading.Lock()


def say(port: str, message: str) -> None:
    name = Path(port).name.removeprefix("cu.")
    with print_lock:
        print(f"{datetime.now():%H:%M:%S}  {name:<24} {message}", flush=True)


# --------------------------------------------------------------------------- firmware


def http_get(url: str) -> tuple[bytes, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "cyberpi-updater"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read(), dict(resp.headers)


def fetch_firmware() -> Path:
    """Download the firmware that mBlock currently ships for the CyberPi."""
    print("Looking up the CyberPi extension used by mBlock...")
    body, _ = http_get(EXT_LIST_URL)
    exts = json.loads(body)["data"]["list"]
    ext = next((e for e in exts if e.get("extId") == "cyberpi"), None)
    if ext is None:
        sys.exit("mBlock's extension list no longer contains the CyberPi extension.")

    script, _ = http_get(ext["extractPath"] + ext["main"])
    match = FIRMWARE_RE.search(script.decode("utf-8", "replace"))
    if match is None:
        sys.exit(
            "Could not find the firmware file in the CyberPi extension "
            f"({ext.get('commitId')}). Makeblock may have changed its format; "
            "pass a firmware file with --firmware instead."
        )
    version, res_path = match.groups()
    url = ext["extractPath"] + res_path
    filename = res_path.rsplit("/", 1)[-1].split("_", 1)[-1]  # drop the hash prefix
    target = FIRMWARE_DIR / filename

    meta = read_firmware_meta()
    if meta and meta.get("url") == url and (FIRMWARE_DIR / meta["file"]).exists():
        print(f"Firmware {version} is already downloaded: {target.relative_to(ROOT)}")
        return target

    print(f"Downloading firmware {version} ({filename})...")
    data, headers = http_get(url)
    md5 = hashlib.md5(data).hexdigest()
    expected = headers.get("Content-MD5") or headers.get("content-md5")
    if expected and base64.b64decode(expected).hex() != md5:
        sys.exit("Download is corrupted (MD5 mismatch). Please try again.")
    if not data or data[0] != ESP_IMAGE_MAGIC:
        sys.exit("Downloaded file does not look like an ESP32 flash image.")

    FIRMWARE_DIR.mkdir(exist_ok=True)
    target.write_bytes(data)
    FIRMWARE_META.write_text(
        json.dumps(
            {
                "version": version,
                "file": filename,
                "md5": md5,
                "size": len(data),
                "url": url,
                "extension": ext.get("commitId"),
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Saved {target.relative_to(ROOT)} ({len(data) / 1e6:.1f} MB, md5 {md5})")
    return target


def read_firmware_meta() -> dict | None:
    try:
        return json.loads(FIRMWARE_META.read_text())
    except (OSError, ValueError):
        return None


def resolve_firmware(path: str | None) -> tuple[Path, str]:
    """Return the image to flash and a label for it, downloading it if needed."""
    if path:
        firmware = Path(path).expanduser().resolve()
        if not firmware.is_file():
            sys.exit(f"Firmware file not found: {firmware}")
        return firmware, firmware.name

    meta = read_firmware_meta()
    if meta and (FIRMWARE_DIR / meta["file"]).is_file():
        return FIRMWARE_DIR / meta["file"], f"{meta['version']} ({meta['file']})"

    print("No firmware downloaded yet.")
    firmware = fetch_firmware()
    return firmware, f"{read_firmware_meta()['version']} ({firmware.name})"


# --------------------------------------------------------------------------- devices


def find_cyberpis() -> list[str]:
    """Serial ports of all connected CyberPis (one per USB device)."""
    ports: dict[str, str] = {}
    for p in list_ports.comports():
        if p.vid == USB_VID and p.pid == USB_PID:
            # Some drivers expose the same device twice; keep one port per USB location.
            ports.setdefault(p.location or p.device, p.device)
    return sorted(ports.values())


@dataclass
class Result:
    port: str
    status: str  # "updated", "up-to-date" or "failed"
    detail: str = ""
    seconds: float = 0.0


def open_log(port: str, action: str):
    LOG_DIR.mkdir(exist_ok=True)
    return open(LOG_DIR / f"{datetime.now():%Y%m%d-%H%M%S}_{action}_{Path(port).name}.log", "w")


def run_esptool(port: str, baud: int, command: list[str], log) -> tuple[int, list[str]]:
    """Run one esptool command against a CyberPi, reporting progress as it goes."""
    cmd = [
        sys.executable, "-m", "esptool",
        "--chip", "esp32", "--port", port, "--baud", str(baud),
        "--before", "default-reset", "--after", "hard-reset",
        *command,
    ]
    log.write(f"$ {' '.join(cmd)}\n")
    log.flush()

    env = {**os.environ, "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env
    )
    lines: list[str] = []
    next_report = 25
    for line in proc.stdout:
        log.write(line)
        line = line.strip()
        if not line:
            continue
        lines.append(line)
        if line.startswith("Connecting"):
            say(port, "connecting...")
        elif m := PROGRESS_RE.search(line):
            pct = float(m.group(2))
            if pct >= next_report and pct < 100:
                say(port, f"{m.group(1).lower()} {pct:3.0f}%")
                next_report = (int(pct) // 25 + 1) * 25
        elif line.startswith("Verifying written data"):
            say(port, "verifying...")
    return proc.wait(), lines


def device_mac(lines: list[str]) -> str | None:
    for line in lines:
        if m := MAC_RE.search(line):
            return m.group(1)
    return None


def error_summary(lines: list[str]) -> str:
    for line in reversed(lines):
        if "error" in line.lower() or "failed" in line.lower():
            return line
    return lines[-1] if lines else "esptool produced no output"


def code_regions(firmware: Path) -> list[str]:
    """esptool address/file arguments for the parts of the image the firmware never rewrites.

    Once a CyberPi boots, it writes its settings (nvs) and file system (storage), so
    comparing the whole image would never match. The bootloader, partition table and app
    partitions stay identical as long as the firmware is the same.
    """
    data = firmware.read_bytes()
    base = int(FLASH_ADDR, 16)
    table = data[PARTITION_TABLE_ADDR - base : PARTITION_TABLE_ADDR - base + 0xC00]
    regions = [(base, PARTITION_TABLE_ADDR + 0x1000)]  # bootloader and partition table
    for i in range(0, len(table), 32):
        if table[i : i + 2] != b"\xaa\x50":
            break
        ptype, _, offset, size = struct.unpack("<BBII", table[i + 2 : i + 12])
        if ptype == PARTITION_TYPE_APP:
            regions.append((offset, offset + size))
    if len(regions) == 1:  # no partition table found, compare the whole image
        regions = [(base, base + len(data))]

    cache = FIRMWARE_DIR / ".regions" / hashlib.md5(data).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    args = []
    for start, end in regions:
        chunk = data[start - base : end - base]
        if chunk:
            path = cache / f"{start:#08x}.bin"
            if not path.exists():
                path.write_bytes(chunk)
            args += [f"{start:#x}", str(path)]
    return args


def flash_device(port: str, firmware: Path, args) -> Result:
    started = time.monotonic()
    bauds = [args.baud] + ([args.fallback_baud] if args.fallback_baud else [])
    write = ["write-flash", *FLASH_ARGS, FLASH_ADDR, str(firmware)]
    verify = ["verify-flash", *code_regions(firmware)]
    needs_update = args.force

    with open_log(port, "flash") as log:
        for attempt, baud in enumerate(bauds):
            if attempt == 0:
                say(port, f"starting at {baud} baud")
            elif os.path.exists(port):
                say(port, f"failed, retrying at {baud} baud...")
            else:
                break  # unplugged, nothing to retry

            if not needs_update:
                say(port, "checking installed firmware...")
                code, lines = run_esptool(port, baud, verify, log)
                if code == 0:
                    return Result(port, "up-to-date", seconds=time.monotonic() - started)
                if not any("digest mismatch" in line for line in lines):
                    continue  # could not talk to the device
                needs_update = True

            code, lines = run_esptool(port, baud, write, log)
            if code == 0:
                return Result(port, "updated", seconds=time.monotonic() - started)

    detail = f"{error_summary(lines)}  (log: {Path(log.name).relative_to(ROOT)})"
    return Result(port, "failed", detail, time.monotonic() - started)


def report(result: Result) -> None:
    if result.status == "updated":
        say(result.port, f"DONE, firmware updated in {result.seconds:.0f}s")
    elif result.status == "up-to-date":
        say(result.port, "DONE, already had this firmware (left untouched)")
    else:
        say(result.port, f"FAILED: {result.detail}")


def print_summary(counts: Counter) -> None:
    print(
        f"\nSummary: {counts['updated']} updated, {counts['up-to-date']} already "
        f"up to date, {counts['failed']} failed."
    )


# --------------------------------------------------------------------------- commands


def cmd_fetch(args) -> int:
    fetch_firmware()
    return 0


def cmd_list(args) -> int:
    ports = find_cyberpis()
    if not ports:
        print("No CyberPi found. Check the USB cable (it must carry data) and the CH340 driver.")
        return 1
    print(f"{len(ports)} CyberPi(s) connected:")
    for port in ports:
        print(f"  {port}")
    return 0


def cmd_flash(args) -> int:
    firmware, label = resolve_firmware(args.firmware)
    ports = args.port or find_cyberpis()
    if not ports:
        print("No CyberPi found. Plug them in (or use `watch`) and try again.")
        return 1

    print(f"Flashing firmware {label} to {len(ports)} CyberPi(s)...\n")
    counts: Counter = Counter()
    with ThreadPoolExecutor(max_workers=args.jobs or len(ports)) as pool:
        futures = [pool.submit(flash_device, port, firmware, args) for port in ports]
        for future in as_completed(futures):
            result = future.result()
            report(result)
            counts[result.status] += 1
    print_summary(counts)
    return 1 if counts["failed"] else 0


def cmd_watch(args) -> int:
    firmware, label = resolve_firmware(args.firmware)
    print(f"Firmware {label}")
    print("Waiting for CyberPis. Plug them in; unplug each one once it says DONE.")
    print("Press Ctrl+C to stop.\n")

    lock = threading.Lock()
    state: dict[str, str] = {}  # port -> "busy" or the final Result.status
    counts: Counter = Counter()
    pool = ThreadPoolExecutor(max_workers=args.jobs or 32)

    def job(port: str) -> None:
        time.sleep(args.settle)  # let the device finish enumerating
        try:
            result = flash_device(port, firmware, args)
        except Exception as exc:  # never let one device take the station down
            result = Result(port, "failed", repr(exc))
        report(result)
        if result.status == "failed":
            say(port, "unplug and plug it back in to retry")
        with lock:
            state[port] = result.status
            counts[result.status] += 1

    try:
        while True:
            present = set(find_cyberpis())
            with lock:
                for port in sorted(present - state.keys()):
                    state[port] = "busy"
                    say(port, "detected")
                    pool.submit(job, port)
                for port in [p for p, s in state.items() if p not in present and s != "busy"]:
                    del state[port]
                    say(port, "unplugged")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping, waiting for running updates to finish (Ctrl+C again to abort)...")
        pool.shutdown(wait=True, cancel_futures=True)
    print_summary(counts)
    return 0


def single_port(port: str | None) -> str:
    if port:
        return port
    ports = find_cyberpis()
    if len(ports) != 1:
        found = "".join(f"\n  {p}" for p in ports)
        sys.exit(f"Found {len(ports)} CyberPis. Plug in exactly one, or pick one with --port.{found}")
    return ports[0]


def cmd_backup(args) -> int:
    port = single_port(args.port)
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    partial = BACKUP_DIR / f"incomplete_{stamp}.bin"

    with open_log(port, "backup") as log:
        say(port, f"reading all {FLASH_SIZE >> 20} MB of flash at {args.baud} baud...")
        code, lines = run_esptool(port, args.baud, ["read-flash", "0", str(FLASH_SIZE), str(partial)], log)
        if code != 0:
            partial.unlink(missing_ok=True)
            say(port, f"FAILED: {error_summary(lines)}  (log: {Path(log.name).relative_to(ROOT)})")
            return 1
        mac = device_mac(lines) or "unknown"
        target = Path(args.out) if args.out else BACKUP_DIR / f"cyberpi_{mac.replace(':', '')}_{stamp}.bin"
        partial.rename(target)

        # Have the device hash its flash and compare it with the file we just wrote.
        say(port, "checking the backup against the device...")
        code, lines = run_esptool(port, args.baud, ["verify-flash", "0", str(target)], log)
        if code != 0:
            say(port, f"FAILED: the backup does not match the device: {error_summary(lines)}")
            return 1

    md5 = hashlib.md5(target.read_bytes()).hexdigest()
    say(port, f"DONE, backup of {mac} saved to {target.relative_to(ROOT)} (md5 {md5})")
    return 0


def cmd_restore(args) -> int:
    image = Path(args.backup).expanduser().resolve()
    if not image.is_file():
        sys.exit(f"Backup not found: {image}")
    if image.stat().st_size != FLASH_SIZE:
        sys.exit(f"{image.name} is not a full CyberPi backup (expected exactly {FLASH_SIZE} bytes).")
    port = single_port(args.port)

    with open_log(port, "restore") as log:
        say(port, f"writing {image.name} at {args.baud} baud...")
        code, lines = run_esptool(port, args.baud, ["write-flash", "0x0", str(image)], log)
        if code != 0:
            say(port, f"FAILED: {error_summary(lines)}  (log: {Path(log.name).relative_to(ROOT)})")
            return 1
    say(port, "DONE, backup restored and verified")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update the firmware of many Makeblock CyberPis over USB at once."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("fetch", help="download the current CyberPi firmware from Makeblock")
    sub.add_parser("list", help="list connected CyberPis")

    flash_opts = argparse.ArgumentParser(add_help=False)
    flash_opts.add_argument("--firmware", help="flash this image instead of the downloaded one")
    flash_opts.add_argument(
        "--baud", type=int, default=DEFAULT_BAUD, help=f"flashing speed (default: {DEFAULT_BAUD})"
    )
    flash_opts.add_argument(
        "--fallback-baud", type=int, default=115200,
        help="retry once at this speed if flashing fails, 0 to disable (default: 115200)",
    )
    flash_opts.add_argument("--jobs", type=int, default=0, help="max devices flashed at the same time")
    flash_opts.add_argument(
        "--force", action="store_true",
        help="reflash even if the device already has this firmware (also wipes its programs and settings)",
    )

    flash = sub.add_parser("flash", parents=[flash_opts], help="flash all connected CyberPis once")
    flash.add_argument("--port", action="append", help="only flash this port (repeatable)")

    watch = sub.add_parser(
        "watch", parents=[flash_opts], help="keep running and flash each CyberPi as it is plugged in"
    )
    watch.add_argument(
        "--settle", type=float, default=1.5, help="seconds to wait after a device appears (default: 1.5)"
    )

    device_opts = argparse.ArgumentParser(add_help=False)
    device_opts.add_argument("--port", help="serial port (default: the only connected CyberPi)")

    backup = sub.add_parser(
        "backup", parents=[device_opts], help="save the whole flash of one CyberPi to backups/"
    )
    backup.add_argument("--out", help="file to write (default: backups/cyberpi_<mac>_<time>.bin)")
    backup.add_argument(
        "--baud", type=int, default=READ_BAUD, help=f"transfer speed (default: {READ_BAUD})"
    )

    restore = sub.add_parser(
        "restore", parents=[device_opts], help="write a backup back to one CyberPi"
    )
    restore.add_argument("backup", help="file created by the backup command")
    restore.add_argument(
        "--baud", type=int, default=DEFAULT_BAUD, help=f"transfer speed (default: {DEFAULT_BAUD})"
    )

    args = parser.parse_args()
    commands = {
        "fetch": cmd_fetch, "list": cmd_list, "flash": cmd_flash, "watch": cmd_watch,
        "backup": cmd_backup, "restore": cmd_restore,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
