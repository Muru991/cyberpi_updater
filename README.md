# CyberPi updater

Updates the firmware of many Makeblock CyberPis (also the one in mBot2) over USB at the same time, without clicking through mBlock.

## How it works

mBlock's **Settings → Update firmware** flashes one ESP32 image to the CyberPi through the chip's ROM bootloader. This script does the same thing with [esptool](https://github.com/espressif/esptool), using the same settings mBlock uses:

| | |
|---|---|
| Image | `cyberpi_firmware_<version>.bin`, taken from mBlock's current CyberPi extension |
| Address | `0x1000` (merged image: bootloader, partition table, app, file system) |
| Flash | DIO, 8 MB, flash speed kept from the image |
| Detection | USB VID:PID `1a86:7523` (the CyberPi's CH340 USB-serial chip) |

The image compresses to about 3 MB on the wire. mBlock sends it at 115200 baud; this script uses 460800 baud and retries a failed device once at 115200. (The CyberPi's CH340 doesn't respond at 921600 baud.) Every write is verified by MD5. Measured on a CyberPi going from 44.01.013 to 44.01.016: about 95 seconds per update, devices in parallel.

Before writing, the script hashes the bootloader, partition table and app partitions on the device. If they already match the image, the device already has this firmware and is left untouched. Otherwise the whole image is written, and, as with mBlock's update, that replaces everything on the device: saved programs, device name and Wi-Fi settings.

## Setup

Requires Python 3.10+.

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python cyberpi_updater.py fetch
```

`fetch` downloads the firmware mBlock currently ships to `firmware/` and checks its MD5. Do this before the event; flashing doesn't need internet.

## Usage

Flash every CyberPi that is plugged in right now:

```bash
.venv/bin/python cyberpi_updater.py flash
```

Or run it as a station: it flashes each CyberPi as soon as you plug it in, and you unplug it when it says `DONE`:

```bash
.venv/bin/python cyberpi_updater.py watch
```

```
20:19:10  usbserial-110            detected
20:19:12  usbserial-110            starting at 460800 baud
20:19:12  usbserial-110            checking installed firmware...
20:19:12  usbserial-110            connecting...
20:19:19  usbserial-110            connecting...
20:19:41  usbserial-110            writing  25%
...
20:20:42  usbserial-110            verifying...
20:20:47  usbserial-110            DONE, firmware updated in 95s
```

A CyberPi that already runs the firmware shows `DONE, already had this firmware (left untouched)` after about 5 seconds.

Other commands and options:

| | |
|---|---|
| `list` | show the connected CyberPis |
| `--port PORT` | (`flash` only) flash only this port; can be repeated |
| `--jobs N` | flash at most N devices at once |
| `--baud N` | flashing speed (default 460800) |
| `--fallback-baud N` | speed for the single retry, `0` to disable (default 115200) |
| `--force` | reflash even if the device already has this firmware |
| `--firmware FILE` | flash a different image |

Each esptool run is logged to `logs/`. When a device fails, its log path is printed.

## Backup and restore

Save the complete 8 MB flash of one CyberPi (firmware, programs, settings) before experimenting:

```bash
.venv/bin/python cyberpi_updater.py backup
```

This writes `backups/cyberpi_<mac>_<time>.bin` and then has the device hash its flash to confirm the file matches. It takes about 6½ minutes. Reading is slower than writing: it runs at 230400 baud, because the CH340 loses bytes when reading at 460800.

Write a backup back, e.g. to recover a CyberPi after a failed update:

```bash
.venv/bin/python cyberpi_updater.py restore backups/cyberpi_<mac>_<time>.bin
```

A restore takes about 90 seconds and is verified like an update. Both commands work on exactly one connected CyberPi, or the one given with `--port`. A backup can also be restored onto a different CyberPi. The MAC address lives in the chip, not in the flash, so it isn't copied.

## Tips

- Use a **powered USB hub** and cables that carry data. Some USB-C cables only charge.
- Close mBlock and mLink first. They keep the serial ports open.
- If many devices on one hub time out, lower `--jobs` (e.g. `--jobs 6`) or `--baud 230400`. Cheap (single-TT) hubs share one 12 Mbit/s link across all their ports.
- macOS 11+ includes the CH340 driver (ports are named `/dev/cu.usbserial-*`). Windows may need the [WCH CH340 driver](https://www.wch-ic.com/downloads/CH341SER_EXE.html). On Linux, add your user to the `dialout` group.

## Not covered

When the CyberPi sits on an mBot2 shield or a Pocket/Starter shield, mBlock can also update the shield's own microcontroller. That uses Makeblock's own protocol through the CyberPi, not esptool, so the shields still need mBlock if it asks for an update.
