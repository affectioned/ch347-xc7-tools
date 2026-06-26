# ch347-xc7-tools

Two zero-install Python scripts for working with Xilinx 7-series FPGAs
over a CH347 USB-JTAG dongle:

| Script         | What it does                                                          |
| ---            | ---                                                                   |
| `read-dna.py`  | Read the 57-bit Device DNA. Independent of the running bitstream.     |
| `flash.py`     | Write a `.bin` to the on-board SPI flash via a `bscan_spi` proxy.     |

Both scripts shell out to OpenOCD and share a single `.ch347_runtime/`
cache that's auto-populated on first run from official upstream sources
(WCH's openocd build with CH347 driver patches, mainline openocd cfgs,
and quartiq's bscan_spi proxies). No manual install of OpenOCD,
libusb, or proxy bitstreams.

## Why this exists

CH347 is a cheap (~$10) USB JTAG dongle that runs at up to 60 MHz —
much faster than the FT2232-class clones — but using it with Xilinx
parts means you have to chase down WCH's patched OpenOCD build, the
right `xilinx-xc7.cfg` / `jtagspi.cfg`, a matching `bscan_spi_xc7a*.bit`
proxy, and figure out the openocd Tcl plumbing yourself. None of that
is hard, but it's all undocumented and the WCH OpenOCD build is hidden
in a subdirectory of an otherwise opaque vendor repo.

These scripts wrap that flow so reading a DNA or flashing a bitstream
is one command and no manual setup.

## Requirements

- **Windows.** The vendored `openocd.exe` is a Windows binary. Linux
  port is a matter of swapping the WCH base URL for a system OpenOCD
  install — PRs welcome.
- **Python 3.8+**, standard library only.
- **CH347 dongle** wired to the FPGA's JTAG header (TCK / TMS / TDI /
  TDO / GND). Bringup tips:
  - On Captain DMA / Squirrel / similar PCIleech-style boards the JTAG
    header is the one **closest to the gold finger** — the other one
    is the FT601 DATA port. Wrong header = silent timeout.
  - The FPGA needs power. On PCIe cards that means either the card is
    seated in a powered slot, or you're feeding it through an external
    12V / 3.3V harness.
- **WCH CH347 driver** installed:
  <https://www.wch-ic.com/downloads/CH347PAR_ZIP.html>
  Device Manager should show a `CH347-JTAG` interface after install.

## Quick start

```bash
git clone https://github.com/affectioned/ch347-xc7-tools
cd ch347-xc7-tools

# Read the silicon DNA
python read-dna.py

# Flash a bitstream sitting next to flash.py
python flash.py

# Flash a bitstream from a custom search dir, recursively
python flash.py --search-dir C:\builds

# Skip auto-detect, point at a specific .bin (still IDCODE-checked)
python flash.py --bitstream C:\builds\latest\top.bin
```

First invocation downloads about 4 MB of vendored binaries + cfgs into
`.ch347_runtime/`. Subsequent runs are instant.

## `read-dna.py`

Reads the 57-bit Device DNA — a per-die unique identifier burned at the
silicon foundry. Useful for per-card licensing / anti-clone protection,
per-die calibration data lookup, or RMA tracking.

```
$ python read-dna.py
Reading DNA over JTAG via CH347...

DNA            : 0x07C6D420441A85C
binary         : 000011111000110110101...

Bake it into your design as a 57-bit constant — e.g.
    parameter [56:0] EXPECTED_DNA = 57'h07C6D420441A85C;
or whatever your build system expects (HDL, Verilog header,
Tcl define, .vh include, etc).
```

JTAG goes straight to the dedicated DNA shift register — independent of
configuration state, so it works even when the running bitstream is
failing to configure or holding the device in reset.

## `flash.py`

Writes a `.bin` to the on-board SPI flash via the FPGA's BSCAN_SPI
proxy. What it does, in order:

1. **Probe** — runs `openocd init; shutdown` (~2 s), parses the JTAG
   IDCODE, maps it to xc7a35t / 75t / 100t / 200t.
2. **Find candidate `.bin`s** — recursively scans every `--search-dir`
   (defaults to the script dir). For each `.bin` it parses out the
   IDCODE the bitstream targets (Type-1 write to register `0x0C`) and
   keeps only ones that match the card. Picks the newest by mtime.
3. **Confirm** — prints the chosen `.bin`, prompts for Enter (accept) /
   paste a different path / Ctrl+C (abort).
4. **Compatibility check** — re-runs the IDCODE compare on the final
   selection (catches a wrong-chip `.bin` pasted at the override
   prompt). **Refuses to flash on mismatch** rather than bricking boot.
5. **Flash** — runs `openocd flash write_image erase ...` against the
   matching `bscan_spi_xc7a<part>.bit` proxy. ~7 s for a typical
   xc7a75t bitstream at 10 MHz JTAG.

Skip the auto-detect with `--bitstream FILE` if you already know what
you want flashed. The IDCODE check still runs — the script will not
write a wrong-family bitstream regardless of how you point at it.

After flashing, **power-cycle the card** for the new bitstream to load.
JTAG-loaded bitstreams are volatile; the SPI flash boot is what brings
the new firmware live.

### Adding new parts

Edit `CHIP_BY_IDCODE` near the top of `flash.py`. Mask is `0x0FFFFFFF`
(clears the top-nibble silicon revision). Currently shipped:

| IDCODE (masked) | xc7a part |
| ---             | ---       |
| `0x0362D093`    | 35t       |
| `0x03632093`    | 75t       |
| `0x03631093`    | 100t      |
| `0x0363E093`    | 200t      |

You'll also need a `bscan_spi_xc7a<part>.bit` in
[quartiq/bscan_spi_bitstreams](https://github.com/quartiq/bscan_spi_bitstreams)
to back the new entry; the script grabs whichever proxy matches at
flash time.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `openocd timed out after 30s` | CH347 driver missing, JTAG cable on the wrong header, or FPGA not powered |
| `JTAG-DP STICKY ERROR` / wrong IDCODE | Bitstream targets a different chip — `flash.py` will catch this and refuse to write |
| First-run download fails | Behind a corporate proxy / no internet; fetch the missing files manually and drop them into `.ch347_runtime/` |
| Flash "verify failed" mid-write | Cable too long for 10 MHz — drop `SPEED_KHZ` near the top of `flash.py` to 5000 or 2000 |
| `couldn't find an IDCODE write in <foo>.bin` | Not a Xilinx 7-series `.bin`, or it's bit-reversed (`.bit` vs `.bin`), or corrupt |

JTAG speed is hardcoded to 10 MHz. CH347 silently rounds up to 15 MHz —
its divider has no 10 MHz tap, harmless. Edit `SPEED_KHZ` if you need
to drop it for a long cable or unreliable signal integrity.

## Where the vendored files come from

| File | Source | License |
| --- | --- | --- |
| `openocd.exe`, `libusb-1.0.dll`, `libhidapi-0.dll` | [WCHSoftGroup/ch347](https://github.com/WCHSoftGroup/ch347) — WCH's openocd build with CH347 driver patches | GPL-2.0+ (OpenOCD), LGPL-2.1 (libusb / libhidapi) |
| `xilinx-xc7.cfg`, `jtagspi.cfg`, `xilinx-dna.cfg` | [openocd-org/openocd](https://github.com/openocd-org/openocd) | GPL-2.0+ |
| `bscan_spi_xc7a*.bit` | [quartiq/bscan_spi_bitstreams](https://github.com/quartiq/bscan_spi_bitstreams) | BSD-2-Clause |

These are *cached*, not bundled — the scripts download them on first
run from their canonical upstream URLs. This repo redistributes
nothing; the code in here (the two scripts + this README) is MIT.

## Contributing

PRs welcome — especially:

- Linux / macOS support (system openocd path detection).
- Additional `xc7a*` part IDs.
- Kintex-7 / Virtex-7 / Spartan-7 IDCODE tables.
- Faster JTAG-speed tuning (CH347 can go to 60 MHz; we're conservative
  at 10).

## License

MIT — see [LICENSE](LICENSE). The third-party files this code
downloads are governed by their own upstream licenses (listed above).
