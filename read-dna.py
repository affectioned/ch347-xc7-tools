#!/usr/bin/env python3
"""
Read the 57-bit Device DNA of a Xilinx 7-series FPGA over JTAG using a
CH347 USB-JTAG dongle.

The Device DNA is a per-die unique identifier burned at the silicon
foundry — independent of the running bitstream, configuration state, or
any user logic. JTAG goes straight to the dedicated DNA shift register
on the chip, so it works even when the bitstream is failing to
configure, the device is held in reset, or PCIe enumeration is broken.

Common uses: per-card licensing / "dongle" anti-clone protection,
per-die calibration data lookup, RMA tracking.

On first run this downloads OpenOCD + a few cfg files from official
upstream sources into `.ch347_runtime/` next to the script:

    - openocd.exe + libusb-1.0.dll + libhidapi-0.dll +
      xilinx-xc7.cfg + jtagspi.cfg + xilinx-dna.cfg
      from WCHSoftGroup/ch347 (WCH ships their own openocd build with
      CH347 driver patches; the .cfg files match its command syntax —
      openocd upstream's don't, and it has no xilinx-xc7.cfg at all)

Subsequent runs use the cache. To force a fresh download, delete
`.ch347_runtime/`.

Requirements:
    - Windows (paths and openocd.exe binary are Windows-targeted).
      Porting to Linux/macOS is a matter of swapping the WCH base URL
      for a system OpenOCD install — PRs welcome.
    - CH347 dongle wired to the FPGA JTAG header (TCK/TMS/TDI/TDO/GND).
    - WCH CH347 driver + CH347DLL.DLL installed (the WCH openocd build
      loads CH347DLL.DLL at runtime; CH347 mode 1 / vid 1a86 pid 55dd
      has no in-box Windows driver):
        https://www.wch-ic.com/products/CH347.html
      Device Manager should then show a CH347-JTAG / CH347 interface.

Usage:
    python read-dna.py
"""

import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

CACHE_DIR_NAME = ".ch347_runtime"
USER_AGENT = "ch347-xc7-tools/read-dna.py"

WCH_REPO = "https://raw.githubusercontent.com/WCHSoftGroup/ch347/main/OpenOCD_CH347"
WCH_BASE = f"{WCH_REPO}/bin"
# .cfg files ship with WCH's openocd build and match its (older) command
# syntax — `-chain-position`, `-no_jstart`, etc. Do NOT source them from
# openocd-org/openocd master: master has no xilinx-xc7.cfg at all, and its
# jtagspi.cfg uses newer `-tap` syntax this build rejects.
WCH_CPLD = f"{WCH_REPO}/scripts/cpld"
WCH_FPGA = f"{WCH_REPO}/scripts/fpga"

# WCH's CH347 driver + CH347DLL.DLL package. The WCH openocd build loads
# CH347DLL.DLL at runtime; it's proprietary and not redistributed here.
DRIVER_URL = "https://www.wch-ic.com/products/CH347.html"

DOWNLOADS = [
    (f"{WCH_BASE}/openocd.exe",       "openocd.exe"),
    (f"{WCH_BASE}/libusb-1.0.dll",    "libusb-1.0.dll"),
    (f"{WCH_BASE}/libhidapi-0.dll",   "libhidapi-0.dll"),
    (f"{WCH_CPLD}/xilinx-xc7.cfg",    "xilinx-xc7.cfg"),
    (f"{WCH_CPLD}/jtagspi.cfg",       "jtagspi.cfg"),
    (f"{WCH_FPGA}/xilinx-dna.cfg",    "xilinx-dna.cfg"),
]

# Generated locally. Sourced cfg files live in the same dir (cwd at run).
INIT_CFG = """\
adapter driver ch347
ch347 vid_pid 0x1a86 0x55dd
adapter speed 10000

source xilinx-dna.cfg
source xilinx-xc7.cfg
source jtagspi.cfg

init
xilinx_print_dna [xc7_get_dna $_CHIPNAME.tap]
shutdown
"""


def _cache_dir():
    return Path(__file__).resolve().parent / CACHE_DIR_NAME


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=120)


def _provision_runtime():
    cache = _cache_dir()
    cache.mkdir(exist_ok=True)

    missing = [(u, n) for (u, n) in DOWNLOADS if not (cache / n).is_file()]
    if missing:
        print(f"First-run setup: fetching {len(missing)} file(s) from "
              f"official sources (WCHSoftGroup/ch347)...")
        for url, name in missing:
            print(f"  {name}...", end="", flush=True)
            try:
                with _http_get(url) as resp:
                    data = resp.read()
            except (urllib.error.URLError, urllib.error.HTTPError) as e:
                print(" failed")
                raise RuntimeError(
                    f"could not download {name} from {url}: {e}"
                ) from e
            (cache / name).write_bytes(data)
            print(f" {len(data) / 1024:.0f} KB")

    # Always rewrite init.cfg so an updated INIT_CFG in this script takes
    # effect without the user nuking the cache.
    (cache / "init.cfg").write_text(INIT_CFG)
    return cache


def _run_openocd(cache):
    exe = cache / "openocd.exe"
    proc = subprocess.run(
        [str(exe), "-f", "init.cfg"],
        cwd=str(cache),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


# xilinx_print_dna emits:  DNA = <57 binary bits> (0x<hex>)
DNA_LINE_RE = re.compile(r"DNA\s*=\s*([01]{57})\s*\(0x([0-9a-fA-F]+)\)")


def main():
    try:
        cache = _provision_runtime()
    except RuntimeError as e:
        sys.stderr.write(
            f"error: {e}\n"
            f"       Either get online and re-run, or fetch the missing\n"
            f"       file manually and drop it into:\n"
            f"         {_cache_dir()}\n"
        )
        return 1

    print("Reading DNA over JTAG via CH347...")
    try:
        rc, output = _run_openocd(cache)
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            "error: openocd.exe timed out after 30s.\n"
            "       Common causes:\n"
            "       - CH347 driver / CH347DLL.DLL not installed. Get it from\n"
            f"           {DRIVER_URL}\n"
            "       - JTAG cable on the wrong header.\n"
            "       - FPGA not powered.\n"
        )
        return 1

    match = DNA_LINE_RE.search(output)
    if not match:
        if "Not find CH347DLL" in output:
            sys.stderr.write(
                "error: openocd could not load CH347DLL.DLL — WCH's user-mode\n"
                "       CH347 library. It is not bundled (proprietary). Install\n"
                "       WCH's CH347 package (driver + DLL) from\n"
                f"         {DRIVER_URL}\n"
                "       or drop a 32-bit CH347DLL.DLL (matching the 32-bit\n"
                f"       openocd.exe) into {_cache_dir()}\n"
            )
            return 1
        if "CH347 open error" in output:
            sys.stderr.write(
                "error: CH347DLL.DLL loaded but the device could not be opened.\n"
                f"       Install WCH's CH347 kernel driver ({DRIVER_URL}),\n"
                "       check the dongle is plugged in and not open elsewhere,\n"
                "       and that it's in mode 1 (JTAG).\n"
            )
            return 1
        sys.stderr.write(
            f"error: openocd ran (exit {rc}) but no DNA line in output.\n"
            f"       Full output below for triage:\n"
            f"--------------------------------\n{output}"
            f"--------------------------------\n"
        )
        return 1

    binary = match.group(1)
    dna_57 = int(match.group(2), 16)

    print()
    print(f"DNA            : 0x{dna_57:015X}")
    print(f"binary         : {binary}")
    print()
    print("Bake it into your design as a 57-bit constant — e.g.")
    print(f"    parameter [56:0] EXPECTED_DNA = 57'h{dna_57:015X};")
    print("or whatever your build system expects (HDL, Verilog header,")
    print("Tcl define, .vh include, etc).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
