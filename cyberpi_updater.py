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
import enum
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from dataclasses import asdict, dataclass
from datetime import datetime
from http.client import HTTPMessage
from pathlib import Path
from typing import Any, TextIO

try:
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is missing. Run: pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent
FIRMWARE_DIR = ROOT / "firmware"
FIRMWARE_META = FIRMWARE_DIR / "latest.json"
REGION_CACHE_DIR = FIRMWARE_DIR / ".regions"
LOG_DIR = ROOT / "logs"
BACKUP_DIR = ROOT / "backups"
TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

# USB-serial chip on the CyberPi (WCH CH340), as declared by mBlock's CyberPi extension.
USB_VID, USB_PID = 0x1A86, 0x7523

# Flash layout and parameters used by mBlock's built-in ESP32 flasher for the CyberPi.
FLASH_START = 0x0
FLASH_SIZE = 8 * 1024 * 1024
IMAGE_ADDR = 0x1000  # the image starts with the bootloader
IMAGE_FLASH_ARGS = ("--flash-mode", "dio", "--flash-size", "8MB", "--flash-freq", "keep")
ESP_IMAGE_MAGIC = 0xE9

# ESP-IDF partition table: a 4 KB sector holding up to 0xC00 bytes of 32-byte entries.
PARTITION_TABLE_ADDR = 0x8000
PARTITION_TABLE_SECTOR = 0x1000
PARTITION_TABLE_MAX_LEN = 0xC00
PARTITION_ENTRY = struct.Struct("<2sBBII16sI")  # magic, type, subtype, offset, size, label, flags
PARTITION_MAGIC = b"\xaa\x50"
PARTITION_TYPE_APP = 0

# The CyberPi's CH340 stops responding at 921600 baud. 460800 works for writing (every
# block is acknowledged and checksummed), but reading back at 460800 loses bytes.
DEFAULT_BAUD = 460800
READ_BAUD = 230400
FALLBACK_BAUD = 115200

WATCH_MAX_JOBS = 32
WATCH_POLL_SECONDS = 1.0
WATCH_SETTLE_SECONDS = 1.5  # time a freshly plugged device needs to finish enumerating
PROGRESS_STEP = 25  # report progress every 25 %
HTTP_TIMEOUT = 120

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

# esptool runs in its own process group, so Ctrl+C in the terminal doesn't cut a flash
# short. What happens to running updates on Ctrl+C is decided by UpdateRunner.
ESPTOOL_ENV = {**os.environ, "NO_COLOR": "1", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
ESPTOOL_ISOLATION: dict[str, Any]
if sys.platform == "win32":
    ESPTOOL_ISOLATION = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
else:
    ESPTOOL_ISOLATION = {"start_new_session": True}

ABORTED_EXIT_CODE = 130

print_lock = threading.Lock()
running_esptools: set[subprocess.Popen[str]] = set()
running_esptools_lock = threading.Lock()
abort_requested = threading.Event()


class Status(enum.Enum):
    UPDATED = "updated"
    UP_TO_DATE = "up-to-date"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Result:
    port: str
    status: Status
    detail: str = ""
    seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class RemoteFirmware:
    """The firmware image mBlock's CyberPi extension currently points to."""

    version: str
    url: str
    filename: str
    extension: str | None


@dataclass(frozen=True, slots=True)
class FirmwareMeta:
    """What `fetch` stored in firmware/latest.json about the downloaded image."""

    version: str
    file: str
    md5: str
    size: int
    url: str
    extension: str | None
    fetched_at: str

    @property
    def path(self) -> Path:
        return FIRMWARE_DIR / self.file


@dataclass(frozen=True, slots=True)
class FlashPlan:
    """How to update a device. Shared read-only by all worker threads."""

    write_args: tuple[str, ...]
    verify_args: tuple[str, ...]  # empty when every device is reflashed (--force)
    bauds: tuple[int, ...]  # first attempt, then the fallback


def say(port: str, message: str) -> None:
    name = Path(port).name.removeprefix("cu.")
    with print_lock:
        print(f"{datetime.now():%H:%M:%S}  {name:<24} {message}", flush=True)


def timestamp() -> str:
    return datetime.now().strftime(TIMESTAMP_FORMAT)


def display_path(path: Path) -> str:
    """The path relative to the project when it is inside it, for shorter messages."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def write_atomically(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(data)
    os.replace(tmp.name, path)


# --------------------------------------------------------------------------- firmware


def http_get(url: str) -> tuple[bytes, HTTPMessage]:
    req = urllib.request.Request(url, headers={"User-Agent": "cyberpi-updater"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read(), resp.headers


def find_remote_firmware() -> RemoteFirmware:
    """Ask mBlock which firmware image its CyberPi extension currently ships."""
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
    return RemoteFirmware(
        version=version,
        url=ext["extractPath"] + res_path,
        filename=res_path.rsplit("/", 1)[-1].split("_", 1)[-1],  # drop the hash prefix
        extension=ext.get("commitId"),
    )


def download_firmware(remote: RemoteFirmware) -> FirmwareMeta:
    print(f"Downloading firmware {remote.version} ({remote.filename})...")
    data, headers = http_get(remote.url)
    md5 = hashlib.md5(data).hexdigest()
    expected = headers.get("Content-MD5")
    if expected and base64.b64decode(expected).hex() != md5:
        sys.exit("Download is corrupted (MD5 mismatch). Please try again.")
    if not data or data[0] != ESP_IMAGE_MAGIC:
        sys.exit("Downloaded file does not look like an ESP32 flash image.")

    meta = FirmwareMeta(
        version=remote.version,
        file=remote.filename,
        md5=md5,
        size=len(data),
        url=remote.url,
        extension=remote.extension,
        fetched_at=datetime.now().isoformat(timespec="seconds"),
    )
    FIRMWARE_DIR.mkdir(exist_ok=True)
    meta.path.write_bytes(data)
    FIRMWARE_META.write_text(json.dumps(asdict(meta), indent=2) + "\n", encoding="utf-8")
    print(f"Saved {display_path(meta.path)} ({len(data) / 1e6:.1f} MB, md5 {md5})")
    return meta


def fetch_firmware() -> FirmwareMeta:
    """Download the firmware mBlock currently ships for the CyberPi, unless already here."""
    print("Looking up the CyberPi extension used by mBlock...")
    try:
        remote = find_remote_firmware()
        meta = read_firmware_meta()
        if meta and meta.url == remote.url and meta.path.is_file():
            print(f"Firmware {meta.version} is already downloaded: {display_path(meta.path)}")
            return meta
        return download_firmware(remote)
    except (urllib.error.URLError, TimeoutError) as exc:
        sys.exit(f"Could not download from Makeblock: {exc}")
    except (KeyError, TypeError, ValueError) as exc:
        sys.exit(
            f"Makeblock's server answered in an unexpected format ({exc!r}); "
            "pass a firmware file with --firmware instead."
        )


def read_firmware_meta() -> FirmwareMeta | None:
    try:
        return FirmwareMeta(**json.loads(FIRMWARE_META.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError):
        return None


def resolve_firmware(path: str | None) -> tuple[Path, str]:
    """Return the image to flash and a label for it, downloading it if needed."""
    if path:
        firmware = Path(path).expanduser().resolve()
        if not firmware.is_file():
            sys.exit(f"Firmware file not found: {firmware}")
        return firmware, firmware.name

    meta = read_firmware_meta()
    if not (meta and meta.path.is_file()):
        print("No firmware downloaded yet.")
        meta = fetch_firmware()
    return meta.path, f"{meta.version} ({meta.file})"


def unchanging_regions(image: bytes) -> list[tuple[int, int]]:
    """Flash ranges of the image that the firmware never rewrites.

    Once a CyberPi boots, it writes its settings (nvs) and file system (storage), so
    comparing the whole image would never match. The bootloader, partition table and app
    partitions stay identical as long as the firmware is the same.
    """
    table_start = PARTITION_TABLE_ADDR - IMAGE_ADDR
    table = image[table_start : table_start + PARTITION_TABLE_MAX_LEN]
    regions = [(IMAGE_ADDR, PARTITION_TABLE_ADDR + PARTITION_TABLE_SECTOR)]
    for offset in range(0, len(table) - PARTITION_ENTRY.size + 1, PARTITION_ENTRY.size):
        magic, ptype, _, start, size, _, _ = PARTITION_ENTRY.unpack_from(table, offset)
        if magic != PARTITION_MAGIC:
            break
        if ptype == PARTITION_TYPE_APP:
            regions.append((start, start + size))
    if len(regions) == 1:  # no partition table found, compare the whole image
        return [(IMAGE_ADDR, IMAGE_ADDR + len(image))]
    return regions


def verify_args(firmware: Path) -> tuple[str, ...]:
    """esptool arguments that check whether a device already runs `firmware`."""
    image = firmware.read_bytes()
    cache = REGION_CACHE_DIR / hashlib.md5(image).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    args = ["verify-flash"]
    for start, end in unchanging_regions(image):
        chunk = image[start - IMAGE_ADDR : end - IMAGE_ADDR]
        if chunk:
            path = cache / f"{start:#08x}.bin"
            if not path.is_file():
                write_atomically(path, chunk)
            args += [f"{start:#x}", str(path)]
    return tuple(args)


def make_plan(firmware: Path, args: argparse.Namespace) -> FlashPlan:
    return FlashPlan(
        write_args=("write-flash", *IMAGE_FLASH_ARGS, f"{IMAGE_ADDR:#x}", str(firmware)),
        verify_args=() if args.force else verify_args(firmware),
        bauds=(args.baud, args.fallback_baud) if args.fallback_baud else (args.baud,),
    )


# --------------------------------------------------------------------------- devices


def find_cyberpis() -> list[str]:
    """Serial ports of all connected CyberPis (one per USB device)."""
    ports: dict[str, str] = {}
    for p in list_ports.comports():
        if p.vid == USB_VID and p.pid == USB_PID:
            # Some drivers expose the same device twice; keep one port per USB location.
            ports.setdefault(p.location or p.device, p.device)
    return sorted(ports.values())


def is_connected(port: str) -> bool:
    return any(p.device == port for p in list_ports.comports())


def single_port(port: str | None) -> str:
    if port:
        return port
    ports = find_cyberpis()
    if len(ports) != 1:
        found = "".join(f"\n  {p}" for p in ports)
        sys.exit(
            f"Found {len(ports)} CyberPis. Plug in exactly one, or pick one with --port.{found}"
        )
    return ports[0]


def open_log(port: str, action: str) -> TextIO:
    LOG_DIR.mkdir(exist_ok=True)
    return open(LOG_DIR / f"{timestamp()}_{action}_{Path(port).name}.log", "w", encoding="utf-8")


def run_esptool(port: str, baud: int, command: Sequence[str], log: TextIO) -> tuple[int, list[str]]:
    """Run one esptool command against a CyberPi, reporting progress as it goes."""
    cmd = [
        sys.executable, "-m", "esptool",
        "--chip", "esp32", "--port", port, "--baud", str(baud),
        "--before", "default-reset", "--after", "hard-reset",
        *command,
    ]
    log.write(f"$ {' '.join(cmd)}\n")
    log.flush()

    lines: list[str] = []
    next_report = PROGRESS_STEP
    with running_esptools_lock:
        if abort_requested.is_set():
            log.write("Not started: aborted with Ctrl+C.\n")
            return ABORTED_EXIT_CODE, ["aborted with Ctrl+C"]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            env=ESPTOOL_ENV,
            **ESPTOOL_ISOLATION,
        )
        running_esptools.add(proc)
    with proc:
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                log.write(raw)
                line = raw.strip()
                if not line:
                    continue
                lines.append(line)
                if line.startswith("Connecting"):
                    say(port, "connecting...")
                elif m := PROGRESS_RE.search(line):
                    pct = float(m.group(2))
                    if next_report <= pct < 100:
                        say(port, f"{m.group(1).lower()} {pct:3.0f}%")
                        next_report = (int(pct) // PROGRESS_STEP + 1) * PROGRESS_STEP
                elif line.startswith("Verifying written data"):
                    say(port, "verifying...")
        except KeyboardInterrupt:  # only reaches here when run from the main thread
            proc.kill()
            raise
        finally:
            with running_esptools_lock:
                running_esptools.discard(proc)
    if proc.returncode != 0 and abort_requested.is_set():
        lines.append("aborted with Ctrl+C")
    return proc.returncode, lines


def abort_esptools() -> None:
    """Kill the running esptool processes and refuse to start new ones."""
    with running_esptools_lock:
        abort_requested.set()
        for proc in running_esptools:
            proc.kill()


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


def failure_detail(lines: list[str], log: TextIO) -> str:
    return f"{error_summary(lines)}  (log: {display_path(Path(log.name))})"


def flash_device(port: str, plan: FlashPlan) -> Result:
    started = time.monotonic()

    def result(status: Status, detail: str = "") -> Result:
        return Result(port, status, detail, time.monotonic() - started)

    needs_update = not plan.verify_args
    lines: list[str] = []
    with open_log(port, "flash") as log:
        for attempt, baud in enumerate(plan.bauds):
            if attempt == 0:
                say(port, f"starting at {baud} baud")
            elif is_connected(port):
                say(port, f"failed, retrying at {baud} baud...")
            else:
                break  # unplugged, nothing to retry

            if not needs_update:
                say(port, "checking installed firmware...")
                code, lines = run_esptool(port, baud, plan.verify_args, log)
                if code == 0:
                    return result(Status.UP_TO_DATE)
                if not any("digest mismatch" in line for line in lines):
                    continue  # could not talk to the device
                needs_update = True

            code, lines = run_esptool(port, baud, plan.write_args, log)
            if code == 0:
                return result(Status.UPDATED)
        return result(Status.FAILED, failure_detail(lines, log))


def report(result: Result) -> None:
    if result.status is Status.UPDATED:
        say(result.port, f"DONE, firmware updated in {result.seconds:.0f}s")
    elif result.status is Status.UP_TO_DATE:
        say(result.port, "DONE, already had this firmware (left untouched)")
    else:
        say(result.port, f"FAILED: {result.detail}")


class UpdateRunner:
    """Updates devices on worker threads, reporting and counting each result.

    Waiting goes through the futures rather than `ThreadPoolExecutor.shutdown(wait=True)`:
    on Python < 3.13, Ctrl+C during `Thread.join()` can mark a running worker as finished,
    and the script would then exit in the middle of a flash.
    """

    def __init__(self, plan: FlashPlan, max_jobs: int, settle_seconds: float = 0.0) -> None:
        self._plan = plan
        self._settle_seconds = settle_seconds
        self._pool = ThreadPoolExecutor(max_workers=max_jobs)
        self._futures: list[Future[None]] = []
        self._lock = threading.Lock()
        self.counts: Counter[Status] = Counter()

    def submit(self, port: str, on_done: Callable[[Result], None] | None = None) -> None:
        self._futures.append(self._pool.submit(self._update, port, on_done))

    def _update(self, port: str, on_done: Callable[[Result], None] | None) -> None:
        try:
            time.sleep(self._settle_seconds)
            result = flash_device(port, self._plan)
        except Exception as exc:  # one device must never take the others down
            result = Result(port, Status.FAILED, repr(exc))
        with self._lock:
            self.counts[result.status] += 1
        try:
            report(result)
        finally:
            if on_done is not None:
                on_done(result)

    def wait(self) -> None:
        """Wait for every submitted update. Ctrl+C hands over to `stop`."""
        try:
            wait_for_futures(self._futures)
        except KeyboardInterrupt:
            self.stop()
        else:
            self._pool.shutdown()

    def stop(self) -> None:
        """Skip queued updates and let running ones finish. A second Ctrl+C kills them."""
        print("\nStopping, waiting for running updates to finish (Ctrl+C again to abort)...")
        self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            wait_for_futures(self._futures)
        except KeyboardInterrupt:
            print("Aborting. Interrupted CyberPis need to be flashed again.")
            abort_esptools()
            wait_for_futures(self._futures)
        self._pool.shutdown()

    def print_summary(self) -> None:
        print(
            f"\nSummary: {self.counts[Status.UPDATED]} updated, "
            f"{self.counts[Status.UP_TO_DATE]} already up to date, "
            f"{self.counts[Status.FAILED]} failed."
        )


# --------------------------------------------------------------------------- commands


def cmd_fetch(args: argparse.Namespace) -> int:
    fetch_firmware()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    ports = find_cyberpis()
    if not ports:
        print("No CyberPi found. Check the USB cable (it must carry data) and the CH340 driver.")
        return 1
    print(f"{len(ports)} CyberPi(s) connected:")
    for port in ports:
        print(f"  {port}")
    return 0


def cmd_flash(args: argparse.Namespace) -> int:
    firmware, label = resolve_firmware(args.firmware)
    ports = args.port or find_cyberpis()
    if not ports:
        print("No CyberPi found. Plug them in (or use `watch`) and try again.")
        return 1

    print(f"Flashing firmware {label} to {len(ports)} CyberPi(s)...\n")
    runner = UpdateRunner(make_plan(firmware, args), max_jobs=args.jobs or len(ports))
    for port in ports:
        runner.submit(port)
    runner.wait()
    runner.print_summary()
    return 1 if runner.counts[Status.FAILED] else 0


def cmd_watch(args: argparse.Namespace) -> int:
    firmware, label = resolve_firmware(args.firmware)
    runner = UpdateRunner(
        make_plan(firmware, args), max_jobs=args.jobs or WATCH_MAX_JOBS, settle_seconds=args.settle
    )
    print(f"Firmware {label}")
    print("Waiting for CyberPis. Plug them in; unplug each one once it says DONE.")
    print("Press Ctrl+C to stop.\n")

    lock = threading.Lock()
    state: dict[str, Status | None] = {}  # port -> final status, None while it is being flashed

    def on_done(result: Result) -> None:
        with lock:
            state[result.port] = result.status
        if result.status is Status.FAILED:
            say(result.port, "unplug and plug it back in to retry")

    try:
        while True:
            present = set(find_cyberpis())
            with lock:
                for port in sorted(present - state.keys()):
                    state[port] = None
                    say(port, "detected")
                    runner.submit(port, on_done)
                for port in [p for p, s in state.items() if s is not None and p not in present]:
                    del state[port]
                    say(port, "unplugged")
            time.sleep(WATCH_POLL_SECONDS)
    except KeyboardInterrupt:
        runner.stop()
    runner.print_summary()
    # Failed devices are retried by replugging them, so they don't fail the whole session.
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    port = single_port(args.port)
    out = Path(args.out).expanduser().resolve() if args.out else None
    if out and out.exists():
        sys.exit(f"{out} already exists.")
    stamp = timestamp()
    BACKUP_DIR.mkdir(exist_ok=True)
    partial = BACKUP_DIR / f"incomplete_{stamp}.bin"

    try:
        with open_log(port, "backup") as log:
            say(port, f"reading all {FLASH_SIZE >> 20} MB of flash at {args.baud} baud...")
            read = ("read-flash", f"{FLASH_START:#x}", str(FLASH_SIZE), str(partial))
            code, lines = run_esptool(port, args.baud, read, log)
            if code != 0:
                say(port, f"FAILED: {failure_detail(lines, log)}")
                return 1
            mac = device_mac(lines) or "unknown"

            # Have the device hash its flash and compare it with the file we just wrote.
            say(port, "checking the backup against the device...")
            verify = ("verify-flash", f"{FLASH_START:#x}", str(partial))
            code, lines = run_esptool(port, args.baud, verify, log)
            if code != 0:
                detail = failure_detail(lines, log)
                say(port, f"FAILED: the backup does not match the device: {detail}")
                return 1

        target = out or BACKUP_DIR / f"cyberpi_{mac.replace(':', '')}_{stamp}.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(partial, target)
        md5 = hashlib.md5(target.read_bytes()).hexdigest()
        say(port, f"DONE, backup of {mac} saved to {display_path(target)} (md5 {md5})")
        return 0
    finally:
        partial.unlink(missing_ok=True)  # only still here if the backup failed or was interrupted


def cmd_restore(args: argparse.Namespace) -> int:
    image = Path(args.backup).expanduser().resolve()
    if not image.is_file():
        sys.exit(f"Backup not found: {image}")
    if image.stat().st_size != FLASH_SIZE:
        sys.exit(f"{image.name} is not a full CyberPi backup (expected {FLASH_SIZE} bytes).")
    port = single_port(args.port)

    with open_log(port, "restore") as log:
        say(port, f"writing {image.name} at {args.baud} baud...")
        write = ("write-flash", f"{FLASH_START:#x}", str(image))
        code, lines = run_esptool(port, args.baud, write, log)
        if code != 0:
            say(port, f"FAILED: {failure_detail(lines, log)}")
            return 1
    say(port, "DONE, backup restored and verified")
    return 0


# --------------------------------------------------------------------------- command line


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update the firmware of many Makeblock CyberPis over USB at once."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "fetch", help="download the current CyberPi firmware from Makeblock"
    ).set_defaults(func=cmd_fetch)
    sub.add_parser("list", help="list connected CyberPis").set_defaults(func=cmd_list)

    flash_opts = argparse.ArgumentParser(add_help=False)
    flash_opts.add_argument("--firmware", help="flash this image instead of the downloaded one")
    flash_opts.add_argument(
        "--baud", type=positive_int, default=DEFAULT_BAUD,
        help="flashing speed (default: %(default)s)",
    )
    flash_opts.add_argument(
        "--fallback-baud", type=int, default=FALLBACK_BAUD,
        help="retry once at this speed if flashing fails, 0 to disable (default: %(default)s)",
    )
    flash_opts.add_argument(
        "--jobs", type=positive_int, help="max devices flashed at the same time"
    )
    flash_opts.add_argument(
        "--force", action="store_true",
        help="reflash even if the device already has this firmware "
        "(also wipes its programs and settings)",
    )

    flash = sub.add_parser("flash", parents=[flash_opts], help="flash all connected CyberPis once")
    flash.add_argument("--port", action="append", help="only flash this port (repeatable)")
    flash.set_defaults(func=cmd_flash)

    watch = sub.add_parser(
        "watch", parents=[flash_opts],
        help="keep running and flash each CyberPi as it is plugged in",
    )
    watch.add_argument(
        "--settle", type=float, default=WATCH_SETTLE_SECONDS,
        help="seconds to wait after a device appears (default: %(default)s)",
    )
    watch.set_defaults(func=cmd_watch)

    device_opts = argparse.ArgumentParser(add_help=False)
    device_opts.add_argument("--port", help="serial port (default: the only connected CyberPi)")

    backup = sub.add_parser(
        "backup", parents=[device_opts], help="save the whole flash of one CyberPi to backups/"
    )
    backup.add_argument("--out", help="file to write (default: backups/cyberpi_<mac>_<time>.bin)")
    backup.add_argument(
        "--baud", type=positive_int, default=READ_BAUD, help="transfer speed (default: %(default)s)"
    )
    backup.set_defaults(func=cmd_backup)

    restore = sub.add_parser(
        "restore", parents=[device_opts], help="write a backup back to one CyberPi"
    )
    restore.add_argument("backup", help="file created by the backup command")
    restore.add_argument(
        "--baud", type=positive_int, default=DEFAULT_BAUD,
        help="transfer speed (default: %(default)s)",
    )
    restore.set_defaults(func=cmd_restore)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    command: Callable[[argparse.Namespace], int] = args.func
    try:
        return command(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
