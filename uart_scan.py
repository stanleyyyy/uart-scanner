#!/usr/bin/env python3
"""
uart_scan.py  -  Probe all serial (UART) ports for a root shell.

For every serial port on the machine it:
  1. Opens the port at 115200 8N1.
  2. Pokes it with a couple of newlines and reads what comes back.
  3. If it looks like a login prompt, tries to log in as 'root' with no password.
  4. Once (if) a shell is reached, fingerprints the device (uname / issue / etc.)
     and grabs an IP address via `ifconfig` (falls back to `ip addr`).
  5. Prints a summary table: port / accessible? / device type / IP.

Requires:  pip install pyserial

Usage:
  python uart_scan.py                # scan every detected port
  python uart_scan.py COM5 COM7      # scan only these ports
  python uart_scan.py --baud 115200  # override baud (default 115200)
  python uart_scan.py --verbose      # dump raw dialogue for debugging

NOTE: This is intended for YOUR OWN devices / lab boards. It sends a login
      attempt to whatever is on the wire, so don't point it at ports you don't
      own or that carry a live protocol you care about.
"""

import argparse
import multiprocessing as mp
import os
import queue as queuemod
import re
import shutil
import subprocess
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial not installed.  Run:  pip install pyserial")

IS_WIN = os.name == "nt"


def enumerate_ports(include_all=False):
    """
    List serial ports worth scanning.

    On Windows: every detected COM port.
    On Linux/macOS: real ports only -- USB adapters (ttyUSB*/ttyACM*, macOS
    cu.*/tty.*) plus any legacy port backed by real hardware (a
    /sys/class/tty/<name>/device link). This drops the ~190 phantom /dev/ttyS*
    stubs that Linux/WSL register but that have no device behind them. Pass
    include_all=True to scan everything regardless.
    """
    infos = list_ports.comports()
    devices = [p.device for p in infos]
    if IS_WIN or include_all:
        return devices

    real = []
    for dev in devices:
        name = os.path.basename(dev)
        if name.startswith(("ttyUSB", "ttyACM")) or name.startswith(("cu.", "tty.")):
            real.append(dev)
        elif os.path.exists(f"/sys/class/tty/{name}/device"):
            real.append(dev)
    return real


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
DEFAULT_BAUD = 115200
READ_TIMEOUT = 0.4          # per-read timeout (s)
SETTLE       = 0.4          # wait after writing before reading (s)
LOGIN_RETRIES = 2           # how many newline pokes to try to raise a prompt
PORT_TIMEOUT = 8            # hard wall-clock budget per port (s); watchdog aborts

LOGIN_PROMPTS = ("login:", "username:", "user:")
PASSWORD_PROMPTS = ("password:", "passwd:")
SHELL_PROMPTS = ("#", "$", "~ #", "/ #", ">", "~]#")   # trailing shell markers


# ---------------------------------------------------------------------------
# Low-level serial helpers
# ---------------------------------------------------------------------------
def drain(ser, seconds=SETTLE):
    """Read everything available for `seconds`, return decoded text."""
    end = time.time() + seconds
    chunks = []
    while time.time() < end:
        n = ser.in_waiting
        if n:
            chunks.append(ser.read(n))
        else:
            time.sleep(0.03)
    return b"".join(chunks).decode("utf-8", "replace")


def send(ser, text, settle=SETTLE):
    """Write text (+CR) and return whatever the device replies."""
    ser.reset_input_buffer()
    ser.write((text + "\r\n").encode())
    ser.flush()
    time.sleep(settle)
    return drain(ser, settle)


# ---------------------------------------------------------------------------
# Single-round-trip shell probe (fast: one command gets identity + IP)
# ---------------------------------------------------------------------------
MARK = "UZP9Q"                                     # unlikely to appear naturally
KLOG = re.compile(r"^\[\s*\d+\.\d+\]")             # kernel log line: [  12.345] ...

# One command that: prints a start marker, then labelled identity fields, then
# the network config, then an end marker. Built via a shell variable so the
# literal marker strings (MARK+"S"/MARK+"E") never appear in the command we
# type -- only in the command's *output* -- which makes detection reliable
# whether or not the console echoes input back.
_INFO_TMPL = (
    "_Z={m}; echo ${{_Z}}S; "
    "echo U $(uname -sm 2>/dev/null); "
    "echo M $(cat /proc/device-tree/model 2>/dev/null | tr -d '\\000'); "
    "echo O $(. /etc/os-release 2>/dev/null; echo \"$PRETTY_NAME\"); "
    "echo H $(hostname 2>/dev/null); "
    "ifconfig 2>/dev/null || ip -o -4 addr 2>/dev/null; "
    "echo ${{_Z}}E"
)
INFO_CMD = _INFO_TMPL.format(m=MARK)


def _parse_info(seg):
    """Pull identity + IP out of the marked command output."""
    info = {"uname": "", "model": "", "os": "", "host": "", "ip": "-"}
    ips, iface = [], "?"
    for ln in seg.splitlines():
        s = ln.strip()
        if not s or MARK in s or KLOG.match(s):
            continue
        if s.startswith("U ") and not info["uname"]:
            info["uname"] = s[2:].strip()
            continue
        if s.startswith("M ") and not info["model"]:
            v = s[2:].strip().replace("\x00", "")
            if v and "no such" not in v.lower():
                info["model"] = v
            continue
        if s.startswith("O ") and not info["os"]:
            v = s[2:].strip().strip('"')
            if v:
                info["os"] = v
            continue
        if s.startswith("H ") and not info["host"]:
            info["host"] = s[2:].strip()
            continue
        # Network output (ifconfig or `ip -o -4 addr`).
        toks = s.split()
        if toks and not ln[:1].isspace() and not s.lower().startswith("inet"):
            iface = toks[0].rstrip(":")
        if len(toks) >= 2 and toks[0].rstrip(":").isdigit():   # "2: eth0 ..."
            iface = toks[1].rstrip(":")
        m = re.search(r"inet (?:addr:)?(\d{1,3}(?:\.\d{1,3}){3})", s)
        if m and not m.group(1).startswith("127."):
            ips.append(f"{iface}:{m.group(1)}")
    if ips:
        info["ip"] = ", ".join(dict.fromkeys(ips))
    return info


def shell_probe(ser, timeout=3.5):
    """
    Fire one combined identity command and read until the end marker.

    Returns an info dict if a shell answered (end marker seen), else None.
    This is a single round-trip, so a responsive console finishes in well
    under a second instead of the old multi-command sequence.
    """
    start, endm = MARK + "S", MARK + "E"
    try:
        ser.reset_input_buffer()
        ser.write((INFO_CMD + "\r\n").encode())
        ser.flush()
    except Exception:
        return None

    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        buf += drain(ser, 0.12)
        if endm in buf:
            break

    if endm not in buf:
        return None                       # no shell (or too slow to answer)
    seg = buf.split(endm, 1)[0]
    if start in seg:
        seg = seg.split(start, 1)[1]
    return _parse_info(seg)


def info_type(info):
    """Build the short 'TYPE / DEVICE' string from a probe info dict."""
    bits = []
    if info["model"]:
        bits.append("model=" + info["model"])
    if info["os"]:
        bits.append(info["os"])
    if info["uname"] and any(k in info["uname"] for k in ("Linux", "BSD", "GNU")):
        bits.append(info["uname"])
    if info["host"]:
        bits.append("host=" + info["host"])
    bits = list(dict.fromkeys(b for b in bits if b))
    return " | ".join(bits) if bits else "unknown shell"


# ---------------------------------------------------------------------------
# Prompt / banner detection
# ---------------------------------------------------------------------------
def looks_like(text, needles):
    low = text.lower()
    return any(n in low for n in needles)


def is_shell(text):
    """Heuristic: does the tail of the buffer look like a shell prompt?"""
    stripped = text.rstrip()
    if not stripped:
        return False
    last = stripped.splitlines()[-1].strip()
    return last.endswith(("#", "$")) or last in ("#", "$", ">")


def _snippet(text, n=60):
    return " ".join(text.split())[:n]


# ---------------------------------------------------------------------------
# Per-port probe
# ---------------------------------------------------------------------------
def probe_port(port, baud, verbose=False):
    """Return a dict describing the port."""
    result = {
        "port": port,
        "status": "inaccessible",
        "type": "-",
        "ip": "-",
        "note": "",
    }

    try:
        ser = serial.Serial(
            port, baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=READ_TIMEOUT,
            write_timeout=2,
        )
    except Exception as e:
        result["note"] = f"open failed: {e}"
        return result

    try:
        # Read anything already coming out, then one CR to raise a prompt.
        banner = drain(ser, 0.4)
        banner += send(ser, "", settle=0.3)

        login_seen = looks_like(banner, LOGIN_PROMPTS) and not is_shell(banner)

        # If a login prompt is showing, log in as root with no password.
        if login_seen:
            reply = send(ser, "root", settle=0.8)
            banner += reply
            if verbose:
                print(f"--- {port} after 'root' ---\n{reply!r}\n")
            if looks_like(reply, PASSWORD_PROMPTS):
                banner += send(ser, "", settle=0.8)   # empty password

        # One combined command: is there a shell, and if so, what/where is it?
        info = shell_probe(ser, timeout=3.5)
        if info is None and not login_seen:
            send(ser, "", settle=0.2)                  # nudge, retry once
            info = shell_probe(ser, timeout=2.5)

        if verbose:
            print(f"--- {port} banner ---\n{banner!r}\n  shell={info is not None}\n")

        if info is not None:
            result["status"] = "accessible (root)" if login_seen else "accessible (open shell)"
            result["type"] = info_type(info)
            result["ip"] = info["ip"]
            return result

        # No shell -- classify what we did see.
        if looks_like(banner, LOGIN_PROMPTS):
            result["status"] = "login required (root/no-pass rejected)"
            result["note"] = _snippet(banner)
        elif banner.strip():
            result["status"] = "responded (no shell)"
            result["note"] = _snippet(banner)
        else:
            result["status"] = "silent (no response)"
        return result

    except Exception as e:
        result["note"] = f"error: {e}"
        return result
    finally:
        try:
            ser.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Parallel scheduler: one killable process per port, with a hard timeout
# ---------------------------------------------------------------------------
def _worker(port, baud, verbose, q):
    """Child-process entry point: probe one port, push result to the queue."""
    try:
        q.put(probe_port(port, baud, verbose))
    except Exception as e:                       # pragma: no cover
        q.put({
            "port": port, "status": "error", "type": "-", "ip": "-",
            "note": f"worker crashed: {e}",
        })


def _timeout_result(port, budget):
    return {
        "port": port,
        "status": f"TIMEOUT (>{budget:g}s, killed)",
        "type": "-",
        "ip": "-",
        "note": "port wedged on open/read; process terminated",
    }


def scan_parallel(ports, baud, verbose, budget, max_workers):
    """
    Probe all ports concurrently. Each port runs in its own process so a
    blocked serial call can be forcibly terminated (which also releases the
    port) instead of hanging the whole scan.
    """
    # spawn on Windows (no fork); fork on Unix (faster, no re-import needed).
    ctx = mp.get_context("spawn" if IS_WIN else "fork")
    remaining = list(ports)
    active = []                             # list of job dicts
    results = []

    while remaining or active:
        # Fill free worker slots.
        while remaining and len(active) < max_workers:
            port = remaining.pop(0)
            q = ctx.Queue()
            p = ctx.Process(target=_worker, args=(port, baud, verbose, q),
                            daemon=True)
            p.start()
            active.append({"port": port, "proc": p, "q": q,
                           "deadline": time.time() + budget})
            print(f"[*] probing {port} (max {budget:g}s) ...", flush=True)

        time.sleep(0.15)

        # Collect finished / timed-out jobs.
        for job in list(active):
            got = None
            try:
                got = job["q"].get_nowait()
            except queuemod.Empty:
                pass

            if got is not None:
                results.append(got)
                job["proc"].join(1)
                active.remove(job)
                print(f"    -> {got['port']}: {got['status']}", flush=True)
            elif time.time() > job["deadline"]:
                job["proc"].terminate()         # hard kill, frees the port
                job["proc"].join(2)
                res = _timeout_result(job["port"], budget)
                results.append(res)
                active.remove(job)
                print(f"    -> {res['port']}: {res['status']}", flush=True)

    # Preserve original port order in the summary.
    order = {p: i for i, p in enumerate(ports)}
    results.sort(key=lambda r: order.get(r["port"], 1e9))
    return results


# ---------------------------------------------------------------------------
# TeraTerm launch + interactive picker
# ---------------------------------------------------------------------------
DEFAULT_TERATERM = r"C:\Program Files (x86)\teraterm\ttermpro.exe"


def find_teraterm():
    """
    Locate ttermpro.exe. Checks, in order: PATH, the Windows uninstall registry
    keys (both 32/64-bit views, HKLM+HKCU), and common install dirs. Returns a
    full path or None.
    """
    cands = []

    w = shutil.which("ttermpro") or shutil.which("ttermpro.exe")
    if w:
        cands.append(w)

    if os.name == "nt":
        try:
            import winreg
            subkey = (r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                      r"\Uninstall\Tera Term_is1")
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                    try:
                        key = winreg.OpenKey(root, subkey, 0,
                                             winreg.KEY_READ | view)
                    except OSError:
                        continue
                    with key:
                        for val in ("InstallLocation", "DisplayIcon"):
                            try:
                                data, _ = winreg.QueryValueEx(key, val)
                            except OSError:
                                continue
                            if not data:
                                continue
                            data = data.split(",")[0].strip('"')
                            if data.lower().endswith(".exe"):
                                cands.append(data)
                            else:
                                cands.append(os.path.join(data, "ttermpro.exe"))
        except Exception:
            pass

    for base in (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("ProgramFiles", r"C:\Program Files")):
        for folder in ("teraterm5", "teraterm", "teraterm4"):
            cands.append(os.path.join(base, folder, "ttermpro.exe"))

    seen = set()
    for c in cands:
        c = os.path.expandvars(c)
        if c and c not in seen and os.path.isfile(c):
            return c
        seen.add(c)
    return None


def responded(r):
    """A port is 'responsive' if it did anything other than fail to open / stay silent."""
    s = r["status"]
    return not (s == "inaccessible" or s.startswith("silent"))


def launch_teraterm(port, baud, exe):
    """Open TeraTerm on a COM port. Returns a short status message string."""
    if not os.path.isfile(exe):
        alt = shutil.which("ttermpro") or shutil.which("ttermpro.exe")
        if alt:
            exe = alt
        else:
            return f"! TeraTerm not found at {exe} (use --teraterm)"
    m = re.search(r"(\d+)", port)
    if not m:
        return f"! could not parse a COM number from {port!r}"
    # TeraTerm: /C=<n> selects COMn, /BAUD sets the rate.
    cmd = [exe, f"/C={m.group(1)}", f"/BAUD={baud}"]
    try:
        subprocess.Popen(cmd, close_fds=True)
        return f"launched TeraTerm on {port} @ {baud}"
    except Exception as e:
        return f"! failed to launch TeraTerm: {e}"


# Serial console tools (preferred first) -> argv builder for `<tool> port baud`.
_SERIAL_TOOLS = [
    ("tio",     lambda p, b: ["tio", "-b", str(b), p]),
    ("picocom", lambda p, b: ["picocom", "-b", str(b), p]),
    ("minicom", lambda p, b: ["minicom", "-b", str(b), "-D", p]),
    ("screen",  lambda p, b: ["screen", p, str(b)]),
    ("cu",      lambda p, b: ["cu", "-l", p, "-s", str(b)]),
]

# Terminal emulators (preferred first) -> flag that precedes the command argv.
_TERMINALS = [
    ("x-terminal-emulator", "-e"),
    ("gnome-terminal", "--"),
    ("konsole", "-e"),
    ("xfce4-terminal", "-x"),
    ("mate-terminal", "-x"),
    ("alacritty", "-e"),
    ("kitty", None),          # kitty <cmd...>
    ("xterm", "-e"),
]


def launch_serial_terminal(port, baud):
    """
    Open `port` in a serial console on Linux/macOS. Prefers tio/picocom/minicom/
    screen/cu, hosted in a GUI terminal if a display is available; otherwise
    returns the command to run by hand. Returns a status message string.
    """
    tool = next(((n, b) for n, b in _SERIAL_TOOLS if shutil.which(n)), None)
    if not tool:
        return ("! no serial tool found -- install one of: "
                "tio, picocom, minicom, screen, cu")
    name, build = tool
    scmd = build(port, baud)

    have_display = bool(os.environ.get("DISPLAY") or
                        os.environ.get("WAYLAND_DISPLAY"))
    if have_display:
        term = next(((n, f) for n, f in _TERMINALS if shutil.which(n)), None)
        if term:
            tname, flag = term
            argv = [tname] + ([flag] if flag else []) + scmd
            try:
                subprocess.Popen(argv, close_fds=True,
                                 start_new_session=True)
                return f"launched {tname} -> {name} on {port} @ {baud}"
            except Exception as e:
                return f"! failed to launch {tname}: {e}"

    # Headless / SSH / no terminal emulator: hand back the command.
    return "run:  " + " ".join(scmd)


def open_port(port, baud, exe):
    """Open a responding port in the platform's serial console."""
    if IS_WIN:
        return launch_teraterm(port, baud, exe)
    return launch_serial_terminal(port, baud)


def set_console_size(cols=100, lines=40):
    """Shrink the console window (Windows only, real console only)."""
    if os.name != "nt" or not sys.stdout.isatty():
        return
    try:
        os.system(f"mode con: cols={cols} lines={lines}")
    except Exception:
        pass


def enable_vt():
    """
    Turn on ANSI/VT escape processing for the current console.

    The classic Windows console (conhost) prints escape codes literally unless
    ENABLE_VIRTUAL_TERMINAL_PROCESSING is set on the stdout handle. Returns True
    if VT is usable (always True on non-Windows).
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)                        # STD_OUTPUT_HANDLE
        if h == 0 or h == -1:
            return False
        mode = wintypes.DWORD()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        ENABLE_VT = 0x0004
        if mode.value & ENABLE_VT:
            return True
        return bool(k.SetConsoleMode(h, mode.value | ENABLE_VT))
    except Exception:
        return False


def _getch_win():
    import msvcrt
    ch = msvcrt.getch()
    if ch in (b"\x00", b"\xe0"):          # arrow / function key prefix
        ch2 = msvcrt.getch()
        return {b"H": "up", b"P": "down"}.get(ch2, "other")
    if ch in (b"\r", b"\n"):
        return "enter"
    if ch == b"\x1b":
        return "esc"
    try:
        return ch.decode("ascii", "ignore").lower()
    except Exception:
        return "other"


def _getch_unix():
    import termios
    import tty
    import select
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x1b":
            # Could be a bare Esc or an arrow (ESC [ A/B). Peek without blocking.
            if select.select([sys.stdin], [], [], 0.005)[0]:
                seq = sys.stdin.read(1)
                if seq == "[" and select.select([sys.stdin], [], [], 0.005)[0]:
                    code = sys.stdin.read(1)
                    return {"A": "up", "B": "down"}.get(code, "other")
            return "esc"
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _getch():
    """Read one keypress and return a normalized token (up/down/enter/esc/char)."""
    return _getch_win() if IS_WIN else _getch_unix()


# What the "open" action is called on this platform.
OPENER = "TeraTerm" if IS_WIN else "a serial console"


def _term_width(default=100):
    try:
        return max(40, shutil.get_terminal_size((default, 40)).columns - 1)
    except Exception:
        return default


def _menu_label(r, width=None):
    """One compact row: PORT  STATUS  short-type."""
    label = f"{r['port']:<12} {r['status']:<24} {r['type'][:32]}"
    if width:
        label = label[:width - 4]
    return label


def _wrap(text, prefix, width, max_lines):
    """Wrap `text` under a fixed label prefix into exactly `max_lines` lines."""
    import textwrap
    pad = " " * len(prefix)
    avail = max(8, width - len(prefix))
    chunks = textwrap.wrap(text, avail) or [""]
    out = []
    for i in range(max_lines):
        head = prefix if i == 0 else pad
        if i < len(chunks):
            body = chunks[i]
            if i == max_lines - 1 and len(chunks) > max_lines:   # ran out of room
                body = body[:max(1, avail - 3)] + "..."
            out.append((head + body)[:width])
        else:
            out.append("")
    return out


# Lines reserved for the highlighted item's detail panel (device wraps to 2).
_DETAIL_LINES = 3


def _detail_block(r, width):
    """Fixed-height detail panel for the highlighted port."""
    dev = r["type"] if r["type"] not in ("-", "") else (r["note"] or "-")
    lines = _wrap(dev, "  device: ", width, _DETAIL_LINES - 1)
    lines.append(("  ip     : " + (r.get("ip") or "-"))[:width])
    return lines


def _menu_numbered(choices, baud, exe):
    """Fallback picker for consoles without VT / keypress support: type a number."""
    while True:
        print("\nResponding ports:")
        for i, r in enumerate(choices, 1):
            print(f"  {i}. {r['port']:<10} {r['status']}")
            if r["type"] not in ("-", ""):
                print(f"       {r['type']}")
            if r.get("ip", "-") not in ("-", ""):
                print(f"       ip: {r['ip']}")
        try:
            raw = input(f"Open which in {OPENER}? (number, or q to quit): ").strip()
        except EOFError:
            return
        if raw.lower() in ("q", "quit", "", "exit"):
            return
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            print("  " + open_port(choices[int(raw) - 1]["port"], baud, exe))
        else:
            print("  ? enter a listed number, or q to quit")


def _menu_arrows(choices, baud, exe):
    """
    Arrow-key picker with reverse-video highlight (requires VT + msvcrt).

    Draws a fixed-height block and repaints it in place on every keypress. Under
    the port list is a detail panel showing the highlighted port's full device
    string and IP, so long values that don't fit the row are still readable.
    """
    sel = 0
    status = "\u2191/\u2193 move \u00b7 Enter open \u00b7 Esc/q quit"
    header = f"Select a port to open in {OPENER}:"
    # header + rows + blank + separator + detail lines + status
    block = 1 + len(choices) + 1 + 1 + _DETAIL_LINES + 1
    first = True

    while True:
        width = _term_width()
        if not first:
            sys.stdout.write(f"\x1b[{block}A")   # jump back to top of block
        first = False
        sys.stdout.write("\x1b[0J")              # erase everything below

        print(header[:width])
        for i, r in enumerate(choices):
            marker = ">" if i == sel else " "
            label = _menu_label(r, width)
            if i == sel:
                print(f" {marker} \x1b[7m{label}\x1b[0m")
            else:
                print(f" {marker} {label}")
        print("")
        print(("  " + "-" * (width - 4))[:width])   # separator rule
        for dl in _detail_block(choices[sel], width):
            print(dl)
        print(status[:width])

        key = _getch()
        if key == "up":
            sel = (sel - 1) % len(choices)
        elif key == "down":
            sel = (sel + 1) % len(choices)
        elif key == "enter":
            status = open_port(choices[sel]["port"], baud, exe)
        elif key in ("esc", "q"):
            sys.stdout.write("\n")
            return


def interactive_menu(results, baud, exe):
    """Choose a responding port and open TeraTerm on it."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return  # piped / non-interactive: nothing to drive the menu

    choices = [r for r in results if responded(r)]
    if not choices:
        print("\nNo responding ports to open.")
        return

    have_keys = False
    try:
        if IS_WIN:
            import msvcrt  # noqa: F401
        else:
            import termios  # noqa: F401
            import tty      # noqa: F401
        have_keys = True
    except ImportError:
        have_keys = False

    # Use the fancy arrow menu only when both keypress input AND VT rendering
    # are available; otherwise fall back to a robust numbered prompt.
    if have_keys and enable_vt():
        _menu_arrows(choices, baud, exe)
    else:
        _menu_numbered(choices, baud, exe)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Probe UART ports for a root shell.")
    ap.add_argument("ports", nargs="*", help="specific ports (default: all detected)")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--timeout", type=float, default=PORT_TIMEOUT,
                    help=f"hard per-port timeout in seconds (default {PORT_TIMEOUT})")
    ap.add_argument("--workers", type=int, default=8,
                    help="max ports probed in parallel (default 8)")
    ap.add_argument("--teraterm", default=None,
                    help="path to ttermpro.exe (auto-detected if omitted)")
    ap.add_argument("--no-menu", action="store_true",
                    help="skip the interactive TeraTerm picker after scanning")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--no-resize", action="store_true",
                    help="do not shrink the console window on start")
    ap.add_argument("--all", action="store_true",
                    help="scan every port incl. phantom /dev/ttyS* (Linux)")
    args = ap.parse_args()

    if not args.no_resize:
        set_console_size()

    if args.ports:
        ports = args.ports
    else:
        ports = enumerate_ports(include_all=args.all)

    if not ports:
        if not IS_WIN and not args.all:
            print("No real serial ports found. USB adapters show as "
                  "/dev/ttyUSB* or /dev/ttyACM*.\n"
                  "  - In WSL, attach the USB device first: usbipd attach --wsl --busid <id>\n"
                  "  - To scan the phantom /dev/ttyS* stubs anyway, re-run with --all")
        else:
            print("No serial ports found.")
        return

    workers = max(1, min(args.workers, len(ports)))
    print(f"Scanning {len(ports)} port(s) at {args.baud} baud "
          f"({workers} in parallel, hard timeout {args.timeout:g}s/port): "
          f"{', '.join(ports)}\n")

    results = scan_parallel(ports, args.baud, args.verbose, args.timeout, workers)

    # ---- summary ----  (kept ~96 cols wide so a small console doesn't wrap)
    W = 96
    print("\n" + "=" * W)
    print("SUMMARY")
    print("=" * W)
    fmt = "{:<13} {:<22} {:<38} {:<20}"
    print(fmt.format("PORT", "STATUS", "TYPE / DEVICE", "IP (iface:addr)"))
    print("-" * W)
    for r in results:
        typ = r["type"] if r["type"] != "-" else (r["note"] or "-")
        print(fmt.format(r["port"][:13], r["status"][:22], typ[:38], r["ip"][:20]))
    print("=" * W)

    accessible = [r for r in results if r["status"].startswith("accessible")]
    print(f"\n{len(accessible)}/{len(results)} port(s) gave a shell.")

    # Detail block for anything we got into, so long fields aren't truncated.
    for r in accessible:
        print(f"\n  {r['port']}  [{r['status']}]")
        print(f"      device: {r['type']}")
        print(f"      ip    : {r['ip']}")

    # Resolve the "opener": TeraTerm on Windows, a serial tool on Unix.
    if IS_WIN:
        teraterm = args.teraterm or find_teraterm() or DEFAULT_TERATERM
        opener_note = (f"TeraTerm: {teraterm}"
                       f"{'' if os.path.isfile(teraterm) else '   (NOT FOUND)'}")
    else:
        teraterm = ""
        tool = next((n for n, _ in _SERIAL_TOOLS if shutil.which(n)), None)
        opener_note = (f"Serial console: {tool}" if tool else
                       "Serial console: none found (install tio/picocom/minicom/screen)")

    if not args.no_menu:
        print("\n" + opener_note)
        interactive_menu(results, args.baud, teraterm)


if __name__ == "__main__":
    # Required so PyInstaller/`spawn` child processes don't re-run main().
    mp.freeze_support()
    main()
