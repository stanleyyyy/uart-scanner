# UART Scanner

Scan every serial (UART/COM) port on a Windows machine, try to reach a root
shell, fingerprint what's on the other end, and open a chosen port in TeraTerm —
from a single console app with an arrow-key menu.

Built for poking at lab boards and embedded Linux devices over their debug
UARTs.

```
========================================================================
SUMMARY
========================================================================
PORT    STATUS                   TYPE / DEVICE                  IP (iface:addr)
------------------------------------------------------------------------
COM7    accessible (open shell)  model=StreamUnlimited ... i.MX8 eth0:10.0.6.158
COM29   accessible (open shell)  model=Naim S195X | Linux 6.1.22 eth0:10.0.6.69
COM8    TIMEOUT (>5s, killed)    port wedged on open/read       -
...
```

## What it does

For every COM port (all detected, or the ones you name) it:

1. Opens the port at **115200 8N1**.
2. Pokes it with newlines and reads the response.
3. If a `login:` prompt appears, logs in as **root** with **no password**.
4. Confirms a shell is live (using echo-marker probes that survive kernel-log
   spam), then fingerprints it:
   - board/SoC from `/proc/device-tree/model`
   - distro `PRETTY_NAME` from `/etc/os-release`
   - `uname -srm`, `hostname`
5. Reads the first non-loopback IPv4 via `ifconfig` (falls back to `ip addr`),
   reported as `iface:addr`.
6. Prints a summary table, then shows an **interactive picker** to open any
   responding port in **TeraTerm** at 115200.

Each port is probed in its **own process** with a hard **5 s timeout**, so a
wedged/hung port is force-killed and skipped instead of freezing the scan. Ports
are probed **in parallel**.

> ⚠️ This sends a login attempt to whatever is on the wire. Point it at your own
> devices. A stray `root` + Enter goes to every responding port, so if a port is
> mid-protocol you care about, pass explicit port names instead of scanning all.

Runs on **Windows** and **Linux/macOS** — the scan engine is the same; only the
console handling and the "open port" tool differ per platform.

## Requirements

**Windows**
- Windows 10/11
- Python 3.8+ with **pyserial** (only from source — the prebuilt `.exe` bundles
  everything)
- [TeraTerm](https://teratermproject.github.io/) (optional, for "open port") —
  auto-detected from PATH, the registry, and common install dirs

**Linux / macOS**
- Python 3.8+ with **pyserial**
- A serial console tool for "open port": one of **tio**, **picocom**,
  **minicom**, **screen**, **cu** (auto-detected)
- Read access to `/dev/tty*` — on Linux add yourself to the `dialout` group:
  `sudo usermod -aG dialout "$USER"` (then log out/in)

## Install / setup

### Linux / macOS

```sh
./install.sh
```

Installs `pyserial`, drops `uart_scan.py` into `~/.local/bin/uart-scan`, and
(on Linux) adds a `.desktop` launcher. Then run:

```sh
uart-scan
```

Or just run the script directly without installing: `python3 uart_scan.py`.
The arrow-key menu works the same (via `termios`); selecting a port opens it in
your serial tool — in a GUI terminal if `$DISPLAY` is set, otherwise it prints
the exact `tio`/`picocom`/… command to run.

### Windows

### Option A — run from source

```bat
install.bat
```

This installs `pyserial` and creates **Desktop + Start Menu** shortcuts
("UART Scanner"). The shortcuts launch through `scripts\launch.vbs`, which opens
the console **already sized** (no resize flash). Then launch from the shortcut,
or run either of:

```bat
scripts\launch.vbs      rem flash-free, pre-sized window (same as the shortcut)
uart_scan.bat           rem simple direct launcher (brief resize on open)
```

### Option B — standalone .exe (no Python on the target)

Build once on a machine that has Python:

```bat
build.bat
```

Copy the resulting `dist\uart_scan.exe` anywhere and run it — nothing else
required.

### Option C — shareable installer (best for handing to others)

Build a single `Setup.exe` that bundles the standalone app and creates the
shortcuts on the target machine:

```bat
installer\build_installer.bat
```

Output: `installer\Output\UART-Scanner-Setup.exe`. Share that one file. It
installs per-user (no admin prompt), needs **no Python**, and adds a Start Menu
(and optional Desktop) shortcut that launches the flash-free window. Uninstall
via *Settings → Apps* like any program.

## Usage

```
uart_scan.bat [options] [PORT ...]

Positional:
  PORT ...        Specific ports to scan (default: all detected)

Options:
  --baud N        Baud rate (default 115200)
  --timeout SEC   Hard per-port timeout in seconds (default 5)
  --workers N     Max ports probed in parallel (default 8)
  --teraterm PATH Path to ttermpro.exe (auto-detected if omitted)
  --no-menu       Skip the interactive TeraTerm picker
  --no-resize     Don't shrink the console window on start
  --verbose, -v   Dump raw serial dialogue (for tuning prompt detection)
```

Examples:

```bat
uart_scan.bat                          rem scan everything
uart_scan.bat COM7 COM8                rem only these ports
uart_scan.bat --timeout 10 --verbose   rem slow boards, show dialogue
python uart_scan.py --no-menu          rem run the script directly
```

### Interactive picker

After the scan, responding ports are listed:

- **↑ / ↓** move the highlight
- **Enter** open the selected port in TeraTerm (@ scan baud)
- **Esc / q** quit

On consoles without ANSI support it falls back to a numbered prompt
(type a number + Enter).

## Status meanings

| Status | Meaning |
|---|---|
| `accessible (open shell)` | Reached a shell with no login needed |
| `accessible (root)` | Logged in as root (no password) |
| `login required (root/no-pass rejected)` | Login prompt, but root/no-password didn't get in |
| `responded (no shell)` | Sent data (e.g. kernel logs) but no usable shell |
| `silent (no response)` | Port opened, nothing came back |
| `TIMEOUT (>Ns, killed)` | Port wedged; process force-killed |
| `inaccessible` | Could not open the port (busy / permission / gone) |

## Re-deploying / redistributing

Everything needed to rebuild lives in this repo:

- `uart_scan.py` — the whole app (single file)
- `scripts/launch.vbs` — flash-free launcher (pre-sizes the console window)
- `run_app.bat` — inner runner used by launch.vbs (exe first, then script)
- `uart_scan.bat` — simple direct launcher (fallback)
- `install.bat` — installs deps + creates shortcuts
- `build.bat` — builds the standalone `.exe`
- `scripts/create_shortcuts.ps1` — shortcut creation (called by install.bat)
- `requirements.txt` — `pyserial`

The compact window size lives in `scripts/launch.vbs` (`cols`/`rows`).

To rebuild the exe on a fresh machine: install Python (tick "Add to PATH"),
clone this repo, run `build.bat`.

## How it works (notes)

- **Marker-framed commands.** Each command is sent as
  `echo MARKs; <cmd>; echo MARKe` and only the text between the markers is
  parsed. This makes shell-detection reliable and ignores interleaved kernel log
  lines — important for consoles that spew `dmesg` while you type.
- **Process-per-port + terminate.** Python threads can't be force-killed, and a
  blocked Windows `open()`/read can hang below pyserial's own timeout. Running
  each probe in a `multiprocessing` process lets a hung port be `terminate()`d,
  which also releases the handle.
- **`freeze_support()`** is called so the `spawn` child processes work inside the
  PyInstaller onefile exe (otherwise the exe would re-launch itself).

## License

MIT — see [LICENSE](LICENSE).
