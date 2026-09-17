#!/usr/bin/env python3
"""
Flash a Xilinx 7-series bitstream to the on-board SPI flash via the
FPGA's JTAG TAP using a CH347 USB-JTAG dongle.

Probes the JTAG chain, reads the chip's IDCODE, finds candidate .bin
files (in directories passed via --search-dir, defaulting to the script
dir), verifies each bitstream targets the same chip family, picks the
newest match, confirms once, and flashes it through a bscan_spi proxy.

Vendored binaries are auto-downloaded on first run from official
upstream sources into `.ch347_runtime/` next to the script:
    - openocd.exe + DLLs + xilinx-xc7.cfg + jtagspi.cfg from
      WCHSoftGroup/ch347 (WCH-patched build; the .cfg files match its
      command syntax — openocd upstream's don't)
    - bscan_spi_xc7a<part>.bit from quartiq/bscan_spi_bitstreams

Subsequent runs use the cache. Delete `.ch347_runtime/` to force fresh
downloads.

One-time prerequisite (not auto-installable): the WCH openocd build loads
WCH's proprietary CH347DLL.DLL at runtime and needs WCH's CH347 kernel
driver bound to the dongle (CH347 mode 1 / vid 1a86 pid 55dd has no in-box
Windows driver). Install both from WCH's CH347 package:
    https://www.wch-ic.com/products/CH347.html
Alternatively, drop a matching 32-bit CH347DLL.DLL into `.ch347_runtime/`
(openocd finds it next to openocd.exe), but the kernel driver still has to
be installed. flash.py prints these steps if the DLL or device is missing.

Usage:
    python flash.py                              # search dir next to script
    python flash.py --search-dir builds/         # also look in builds/
    python flash.py --search-dir a/ --search-dir b/
    python flash.py --bitstream path/to/foo.bin  # skip auto-detect
"""

import argparse
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

CACHE_DIR_NAME = ".ch347_runtime"
USER_AGENT = "ch347-xc7-tools/flash.py"

WCH_REPO = "https://raw.githubusercontent.com/WCHSoftGroup/ch347/main/OpenOCD_CH347"
WCH_BASE = f"{WCH_REPO}/bin"
# The .cfg files ship with WCH's openocd build and match its (older) command
# syntax — `-chain-position`, `-no_jstart`, etc. Do NOT source them from
# openocd-org/openocd master: master has no xilinx-xc7.cfg at all, and its
# jtagspi.cfg uses newer `-tap` syntax this build rejects.
WCH_CPLD = f"{WCH_REPO}/scripts/cpld"
BSCAN_BASE = "https://raw.githubusercontent.com/quartiq/bscan_spi_bitstreams/master"

# WCH's CH347 driver + CH347DLL.DLL package. The WCH openocd build loads
# CH347DLL.DLL at runtime (see _diagnose_probe_failure); it's proprietary
# and not redistributed here.
DRIVER_URL = "https://www.wch-ic.com/products/CH347.html"

CORE_DOWNLOADS = [
    (f"{WCH_BASE}/openocd.exe",        "openocd.exe"),
    (f"{WCH_BASE}/libusb-1.0.dll",     "libusb-1.0.dll"),
    (f"{WCH_BASE}/libhidapi-0.dll",    "libhidapi-0.dll"),
    (f"{WCH_CPLD}/xilinx-xc7.cfg",     "xilinx-xc7.cfg"),
    (f"{WCH_CPLD}/jtagspi.cfg",        "jtagspi.cfg"),
]

# IDCODE & 0x0FFFFFFF clears the 4-bit silicon revision in the top
# nibble. xc7a75t comes back as 0x13632093 on rev-1 silicon, 0x03632093
# on rev-0 — both map to 75t after masking. Add new parts here as you
# encounter them; PRs welcome.
CHIP_BY_IDCODE = {
    0x0362D093: "35t",
    0x03632093: "75t",
    0x03631093: "100t",
    0x0363E093: "200t",
}

IDCODE_MASK = 0x0FFFFFFF

# Type 1 write to register 0x0C (IDCODE), word count 1.
#   [31:29]=001 (Type1) [28:27]=10 (Write) [26:13]=0x0C [12:11]=00 [10:0]=1
# Encoded big-endian as a 4-byte tag immediately followed by the 4-byte
# IDCODE the bitstream is built for.
WRITE_IDCODE_TYPE1 = bytes.fromhex("30018001")

IDCODE_RE = re.compile(r"tap/device found:\s*0x([0-9a-fA-F]+)")

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / CACHE_DIR_NAME

SPEED_KHZ = 10000

PROBE_CFG_TEMPLATE = """\
adapter driver ch347
ch347 vid_pid 0x1a86 0x55dd
adapter speed {khz}

source xilinx-xc7.cfg

init
shutdown
"""

# Triple-brace -> literal `{<bitstream>}` so Tcl treats the path as one
# token even with spaces in it.
# No `flash verify_image` — WCH's openocd build raises "doesn't support
# checksum_memory" against the bscan_spi proxy on the verify step even
# though the write itself succeeded. Power-cycle + boot is the real test.
FLASH_CFG_TEMPLATE = """\
adapter driver ch347
ch347 vid_pid 0x1a86 0x55dd
adapter speed {khz}

source xilinx-xc7.cfg
source jtagspi.cfg

init
jtagspi_init 0 {proxy}
flash write_image erase {{{bitstream}}} 0 bin
shutdown
"""


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=120)


def _ensure_file(url, name):
    target = CACHE_DIR / name
    if target.is_file():
        return target
    print(f"  Downloading {name}...", end="", flush=True)
    try:
        with _http_get(url) as resp:
            data = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        print(" failed")
        raise RuntimeError(f"could not download {name} from {url}: {e}") from e
    target.write_bytes(data)
    print(f" {len(data) / 1024:.0f} KB")
    return target


def _ensure_core():
    CACHE_DIR.mkdir(exist_ok=True)
    for url, name in CORE_DOWNLOADS:
        _ensure_file(url, name)


def _ensure_proxy(part_short):
    name = f"bscan_spi_xc7a{part_short}.bit"
    _ensure_file(f"{BSCAN_BASE}/{name}", name)
    return name


def _probe_idcode():
    (CACHE_DIR / "probe.cfg").write_text(PROBE_CFG_TEMPLATE.format(khz=SPEED_KHZ))
    proc = subprocess.run(
        [str(CACHE_DIR / "openocd.exe"), "-f", "probe.cfg"],
        cwd=str(CACHE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
    )
    m = IDCODE_RE.search(proc.stdout)
    if not m:
        raise RuntimeError(_diagnose_probe_failure(proc.stdout))
    return int(m.group(1), 16)


def _diagnose_probe_failure(output):
    """Turn an openocd failure into an actionable message. The WCH openocd
    build is hardwired on Windows to load WCH's proprietary CH347DLL.DLL
    (CH347DLLA64.DLL on 64-bit builds); it has no libusb backend on
    Windows. That DLL, plus WCH's CH347 kernel driver, are one-time
    prerequisites we can't bundle — the DLL isn't redistributed in the
    WCH repo and a kernel driver can't be a dropped file."""
    if "Not find CH347DLL" in output:
        detail = (
            "openocd could not load CH347DLL.DLL — WCH's user-mode CH347\n"
            "library. It is not bundled (proprietary; not in the WCH repo).\n"
            "Install it one of two ways:\n"
            f"  * install WCH's CH347 package (driver + DLL) from\n"
            f"    {DRIVER_URL} , or\n"
            "  * drop CH347DLL.DLL (32-bit, to match the bundled 32-bit\n"
            f"    openocd.exe) into {CACHE_DIR}\n"
            "You also need WCH's CH347 kernel driver installed for the DLL\n"
            "to reach the device — CH347 mode 1 has no in-box Windows driver."
        )
    elif "CH347 open error" in output:
        detail = (
            "CH347DLL.DLL loaded but the device could not be opened. Check:\n"
            f"  * WCH's CH347 kernel driver is installed ({DRIVER_URL})\n"
            "  * the dongle is plugged in and not open in another program\n"
            "  * the CH347 is in mode 1 (JTAG) — not mode 0/2/3"
        )
    else:
        detail = (
            "could not read JTAG IDCODE — check the cable and that the JTAG\n"
            "header is connected and the card is powered."
        )
    return detail + "\n\nopenocd output:\n" + output


def _chip_family(idcode):
    if idcode is None:
        return None
    return CHIP_BY_IDCODE.get(idcode & IDCODE_MASK)


def _bitstream_idcode(path):
    """Extract the IDCODE that the bitstream targets — find the Type 1
    write-to-IDCODE-register packet and read the next 4 bytes. Returns
    None if not found (not a 7-series .bin, bit-reversed, corrupt, etc.)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    idx = data.find(WRITE_IDCODE_TYPE1)
    if idx < 0 or idx + 8 > len(data):
        return None
    return int.from_bytes(data[idx + 4:idx + 8], "big")


def _candidate_bins(search_dirs):
    """Yield every .bin in the search dirs, recursively. De-dupes by
    resolved path."""
    seen = set()
    for d in search_dirs:
        if not d.is_dir():
            continue
        for b in d.rglob("*.bin"):
            r = b.resolve()
            if r in seen:
                continue
            seen.add(r)
            yield r


def _find_newest_compatible_bin(card_idcode, search_dirs):
    all_bins = []
    for path in _candidate_bins(search_dirs):
        bs_id = _bitstream_idcode(path)
        all_bins.append((path, bs_id))
    chip_mask = card_idcode & IDCODE_MASK
    compatible = [p for p, bs in all_bins if bs is not None and (bs & IDCODE_MASK) == chip_mask]
    chosen = max(compatible, key=lambda p: p.stat().st_mtime) if compatible else None
    return chosen, all_bins


def _verify_compat(card_idcode, bitstream_path):
    bs_id = _bitstream_idcode(bitstream_path)
    if bs_id is None:
        return False, (
            f"could not find an IDCODE write in {bitstream_path.name} — "
            f"this doesn't look like a Xilinx 7-series .bin."
        )
    if (bs_id & IDCODE_MASK) != (card_idcode & IDCODE_MASK):
        bs_part = _chip_family(bs_id) or "unknown"
        card_part = _chip_family(card_idcode) or "unknown"
        return False, (
            f"bitstream targets xc7a{bs_part} (IDCODE 0x{bs_id:08X}) but "
            f"card is xc7a{card_part} (0x{card_idcode:08X})"
        )
    return True, f"bitstream IDCODE 0x{bs_id:08X} matches card"


def _resolve_bitstream(detected):
    if detected:
        prompt = "Press Enter to flash this, paste a different .bin path, or Ctrl+C to abort: "
    else:
        prompt = "Paste a .bin path or Ctrl+C to abort: "
    raw = input(prompt).strip()
    if not raw:
        return detected
    # Windows drag-and-drop wraps the path in quotes — strip them.
    raw = raw.strip('"').strip("'")
    path = Path(raw)
    if not path.is_file():
        print(f"error: not a file: {path}")
        return None
    return path.resolve()


def _run_flash(proxy_name, bitstream):
    (CACHE_DIR / "flash.cfg").write_text(FLASH_CFG_TEMPLATE.format(
        khz=SPEED_KHZ, proxy=proxy_name, bitstream=bitstream.as_posix(),
    ))
    print()
    print(f"openocd -f flash.cfg  ({SPEED_KHZ} kHz)")
    print("-" * 60)
    rc = subprocess.call(
        [str(CACHE_DIR / "openocd.exe"), "-f", "flash.cfg"],
        cwd=str(CACHE_DIR),
    )
    print("-" * 60)
    print(f"openocd exit {rc}")
    return rc


def _parse_args():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--search-dir", action="append", type=Path, metavar="DIR",
        help="directory to recursively scan for candidate .bin files. "
             "May be repeated. Defaults to the directory containing this "
             "script.",
    )
    ap.add_argument(
        "--bitstream", type=Path, default=None, metavar="FILE",
        help="skip auto-detect and flash this specific .bin. Still "
             "verified against the card's IDCODE before flashing.",
    )
    return ap.parse_args()


def main():
    args = _parse_args()
    search_dirs = args.search_dir if args.search_dir else [SCRIPT_DIR]

    print("ch347-xc7-tools flash")
    print("=====================")
    try:
        _ensure_core()
    except RuntimeError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1

    print()
    print("Probing JTAG chain...")
    try:
        idcode = _probe_idcode()
    except subprocess.TimeoutExpired:
        sys.stderr.write("error: openocd timed out after 30 s — driver / cable / power?\n")
        return 1
    except RuntimeError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1

    part_short = _chip_family(idcode)
    if not part_short:
        sys.stderr.write(
            f"error: IDCODE 0x{idcode:08X} is not a known xc7a part.\n"
            f"       Add it to CHIP_BY_IDCODE in {Path(__file__).name} if "
            f"this is a new variant.\n"
        )
        return 1
    print(f"  Detected xc7a{part_short} (IDCODE 0x{idcode:08X})")

    # Manual-override path: --bitstream wins, skip auto-detect entirely.
    if args.bitstream is not None:
        if not args.bitstream.is_file():
            sys.stderr.write(f"error: --bitstream not a file: {args.bitstream}\n")
            return 1
        bitstream = args.bitstream.resolve()
        print()
        print(f"Using --bitstream: {bitstream}")
    else:
        print()
        print(f"Searching: {', '.join(str(d) for d in search_dirs)}")
        detected, all_bins = _find_newest_compatible_bin(idcode, search_dirs)
        if detected:
            print("Most recent compatible build:")
            print(f"  {detected}")
        elif all_bins:
            print(f"Found {len(all_bins)} .bin but none target xc7a{part_short}:")
            for path, bs_id in all_bins:
                fam = _chip_family(bs_id) or "?"
                tag = f"0x{bs_id:08X}" if bs_id is not None else "no IDCODE"
                print(f"  - {path}  ({tag}, xc7a{fam})")
        else:
            print("No .bin found in any search dir.")

        print()
        bitstream = _resolve_bitstream(detected)
        if bitstream is None:
            return 1

    ok, msg = _verify_compat(idcode, bitstream)
    if not ok:
        sys.stderr.write(f"error: {msg}\n")
        sys.stderr.write("       Refusing to flash. The card would fail to boot.\n")
        return 1
    print(f"  {msg}")

    try:
        proxy_name = _ensure_proxy(part_short)
    except RuntimeError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1

    rc = _run_flash(proxy_name, bitstream)
    if rc == 0:
        print()
        print("Flash OK — power-cycle the card for the new bitstream to take effect.")
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
