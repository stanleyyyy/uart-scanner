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


# ---------------------------------------------------------------------------
# ANSI color + glyphs (Linux natively; Windows once VT processing is enabled).
# Muted standard palette -- readable on a dark console, not neon.
# ---------------------------------------------------------------------------
FG_RED, FG_GREEN, FG_YELLOW, FG_BLUE = 31, 32, 33, 34
FG_MAGENTA, FG_CYAN, FG_GRAY = 35, 36, 90
BOLD, DIM = 1, 2
USE_COLOR = False
USE_UNICODE = False

# Device-field colors (one per kind, muted).
COL_MODEL  = (FG_MAGENTA,)
COL_SYSTEM = (FG_YELLOW,)
COL_HOST   = (FG_CYAN,)
COL_IP     = (FG_GREEN,)

# Status "kind" -> color and glyph. A color-coded filled dot (●) reads clearly
# and is present in virtually every monospace font; ASCII fallback for pipes.
_KIND_STYLE = {"ok": (FG_GREEN,), "fail": (FG_RED,), "part": (FG_YELLOW,)}
_KIND_GLYPH = {"ok": ("●", "+"), "fail": ("●", "x"), "part": ("●", "*")}


def setup_color():
    """Enable colored/unicode output when the terminal supports it (NO_COLOR aware).

    We do NOT reconfigure stdout: on Windows, Python writes to an interactive
    console via the Unicode API (WriteConsoleW), so box-drawing and ● render
    regardless of the console code page. Unicode is only used when stdout is a
    real terminal; piped output stays ASCII so no encoding error can occur.
    """
    global USE_COLOR, USE_UNICODE
    tty = sys.stdout.isatty()
    if not tty or os.environ.get("NO_COLOR"):
        USE_COLOR = False
    elif IS_WIN:
        USE_COLOR = enable_vt()              # defined below; called at runtime
    else:
        USE_COLOR = True
    USE_UNICODE = tty
    return USE_COLOR


def paint(text, *codes):
    """Wrap text in SGR color codes when coloring is on; otherwise return as-is."""
    if not USE_COLOR or not codes:
        return text
    return "\x1b[" + ";".join(str(c) for c in codes) + "m" + text + "\x1b[0m"


def status_kind(status):
    s = status.lower()
    if s.startswith("accessible"):
        return "ok"
    if s.startswith(("timeout", "inaccessible", "silent")):
        return "fail"
    return "part"                            # login required / responded


def status_style(status):
    return _KIND_STYLE[status_kind(status)]


def status_glyph(status):
    """A colored ✓/✗/• (ASCII +/x/* when the console can't do Unicode)."""
    kind = status_kind(status)
    uni, asc = _KIND_GLYPH[kind]
    return paint(uni if USE_UNICODE else asc, *_KIND_STYLE[kind])


def rule(width, heavy=False):
    """A horizontal rule using box-drawing (─/═) or ASCII (-/=)."""
    if USE_UNICODE:
        ch = "═" if heavy else "─"
    else:
        ch = "=" if heavy else "-"
    return ch * width


def _detail_fields(r):
    """Ordered (label, value, color) tuples describing a port's device."""
    info = r.get("info") or {}
    # Distro (often absent on embedded) + kernel on one 'system' line.
    system = " | ".join(x for x in (info.get("os", ""), info.get("uname", "")) if x)
    return [
        ("model",  info.get("model", ""),                 COL_MODEL),
        ("system", system,                                COL_SYSTEM),
        ("host",   info.get("host", ""),                  COL_HOST),
        ("ip",     r.get("ip") or info.get("ip", ""),     COL_IP),
    ]


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
        "info": None,          # structured {model, os, uname, host, ip} if a shell
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
            result["info"] = info
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


def scan_parallel(ports, baud, verbose, budget, max_workers,
                  on_log=None, on_result=None):
    """
    Probe all ports concurrently. Each port runs in its own process so a
    blocked serial call can be forcibly terminated (which also releases the
    port) instead of hanging the whole scan.

    on_log(text) / on_result(dict): optional callbacks for live UIs. When
    on_log is None, progress is printed to stdout (colored) as before.
    """
    def log(plain, colored=None):
        if on_log:
            on_log(plain)
        else:
            print(colored if colored is not None else plain, flush=True)

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
            log(f"probing {port} (max {budget:g}s) ...",
                f"[*] probing {port} (max {budget:g}s) ...")

        time.sleep(0.15)

        # Collect finished / timed-out jobs.
        for job in list(active):
            got = None
            try:
                got = job["q"].get_nowait()
            except queuemod.Empty:
                pass

            done = None
            if got is not None:
                job["proc"].join(1)
                active.remove(job)
                done = got
            elif time.time() > job["deadline"]:
                job["proc"].terminate()         # hard kill, frees the port
                job["proc"].join(2)
                active.remove(job)
                done = _timeout_result(job["port"], budget)

            if done is not None:
                results.append(done)
                if on_result:
                    on_result(done)
                log(f"{done['port']}: {done['status']}",
                    f"    {status_glyph(done['status'])} {paint(done['port'], BOLD)}  "
                    f"{paint(done['status'], *status_style(done['status']))}")

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


# One panel line per device field (model / system / host / ip).
_DETAIL_LINES = 4


def _field_line(label, value, codes, width):
    """A single 'label: value' detail line: dim label + field-colored value."""
    value = (value or "").strip()
    prefix = "  " + f"{label:<6}" + ": "
    avail = max(6, width - len(prefix))
    if not value:
        return paint(prefix, DIM) + paint("-", DIM)
    return paint(prefix, DIM) + paint(value[:avail], *codes)


def _detail_block(r, width):
    """Fixed-height detail panel for the highlighted port, one color per field."""
    if r.get("info"):
        lines = [_field_line(lbl, val, codes, width)
                 for lbl, val, codes in _detail_fields(r)]
    else:
        # No shell: show the status and note across the reserved lines.
        st = r.get("status", "")
        lines = [paint("  status: ", DIM) + paint(st, *status_style(st))]
        note = r.get("note", "") or "-"
        lines += [paint(x, DIM) if x.strip() else x
                  for x in _wrap(note, "  note  : ", width, _DETAIL_LINES - 1)]
    return (lines + [""] * _DETAIL_LINES)[:_DETAIL_LINES]


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

        print(paint(header[:width], BOLD))
        for i, r in enumerate(choices):
            info = r.get("info") or {}
            dev = info.get("model") or info.get("uname") or ""
            devcodes = COL_MODEL if info.get("model") else COL_SYSTEM
            dev_w = max(6, width - 38)
            port = f"{r['port']:<10}"[:10]
            stat = f"{r['status']:<22}"[:22]
            dv = f"{dev[:dev_w]:<{dev_w}}"
            kind = status_kind(r["status"])
            gch = (_KIND_GLYPH[kind][0] if USE_UNICODE else _KIND_GLYPH[kind][1])
            if i == sel:
                # reverse-video highlight (no inner color: a reset would end it)
                print(f" \x1b[7m{gch} {port} {stat} {dv}\x1b[0m")
            else:
                print(" " + paint(gch, *_KIND_STYLE[kind]) + " "
                      + paint(port, BOLD) + " "
                      + paint(stat, *status_style(r["status"])) + " "
                      + (paint(dv, *devcodes) if dev else dv))
        print("")
        print(("  " + rule(max(1, width - 4)))[:width])   # separator rule
        for dl in _detail_block(choices[sel], width):
            print(dl)
        print(paint(status[:width], DIM))

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
# Full-screen TUI (curses): live log, scrollable, navigable port list
# ---------------------------------------------------------------------------
def _tui_put(win, y, x, text, attr=0):
    """addstr that clips to the window and never raises at the edges."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    text = str(text)[:max(0, w - x - 1)]
    try:
        win.addstr(y, x, text, attr)
    except Exception:
        pass


def _blank_row(port):
    return {"port": port, "status": "scanning...", "type": "-", "ip": "-",
            "info": None, "note": ""}


def _tui_loop(stdscr, ports, baud, timeout, workers, verbose, opener_exe,
              auto_detect=False, include_all=False):
    import curses
    import threading

    curses.curs_set(0)
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    pair = {}
    for i, col in enumerate([curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_YELLOW,
                             curses.COLOR_MAGENTA, curses.COLOR_CYAN, curses.COLOR_BLUE,
                             curses.COLOR_WHITE], start=1):
        try:
            curses.init_pair(i, col, -1)
        except curses.error:
            pass
    GREEN, RED, YELLOW = curses.color_pair(1), curses.color_pair(2), curses.color_pair(3)
    MAGENTA, CYAN = curses.color_pair(4), curses.color_pair(5)
    B = curses.A_BOLD
    KIND_ATTR = {"ok": GREEN | B, "fail": RED | B, "part": YELLOW | B}
    FIELD_ATTR = {"model": MAGENTA | B, "system": YELLOW | B,
                  "host": CYAN | B, "ip": GREEN | B}

    stdscr.nodelay(True)
    stdscr.keypad(True)
    stdscr.timeout(120)

    lock = threading.Lock()
    rows = [_blank_row(p) for p in ports]
    row_of = {p: i for i, p in enumerate(ports)}
    logs = []
    st = {"scanning": False, "sel": 0, "top": 0, "log_off": 0, "msg": "",
          "stop": False}

    def add_log(text):
        with lock:
            logs.append(text)
            if len(logs) > 3000:
                del logs[:len(logs) - 3000]

    def add_result(r):
        with lock:
            i = row_of.get(r["port"])
            if i is not None:
                rows[i] = r

    def scan_ports_bg(targets):
        """Probe a subset of ports in a background thread.

        Does not touch the global 'scanning' banner -- used for auto-detected
        new ports so the rest of the app stays responsive. Each port runs in
        its own process, so this is safe to overlap with any other scan.
        """
        targets = list(targets)
        if not targets:
            return
        w = max(1, min(workers, len(targets)))

        def run():
            try:
                scan_parallel(targets, baud, verbose, timeout, w,
                              on_log=add_log, on_result=add_result)
            except Exception as e:
                add_log("auto-scan error: %s" % e)

        threading.Thread(target=run, daemon=True).start()

    def apply_port_diff(current):
        """Sync `rows` to the enumerated `current` list. Caller must hold lock.

        Existing rows (and their scan results) are preserved for ports that are
        still present; removed ports drop out; added ports get a blank row.
        Returns the list of newly-added ports.
        """
        nonlocal ports, rows, row_of
        old = set(ports)
        new = set(current)
        added = [p for p in current if p not in old]
        removed = [p for p in ports if p not in new]
        if not added and not removed:
            return []
        for p in added:
            logs.append("+++ new port detected: %s" % p)
        for p in removed:
            logs.append("--- port removed: %s" % p)
        keep = {r["port"]: r for r in rows}
        sel_port = ports[st["sel"]] if 0 <= st["sel"] < len(ports) else None
        ports = list(current)
        rows = [keep.get(p) or _blank_row(p) for p in ports]
        row_of = {p: i for i, p in enumerate(ports)}
        # Keep the cursor on the same port if it still exists.
        st["sel"] = row_of.get(sel_port, min(st["sel"], max(0, len(ports) - 1)))
        st["top"] = min(st["top"], max(0, len(ports) - 1))
        return added

    def poll_ports():
        """Re-enumerate; apply removals immediately and auto-scan additions.

        Runs in a background thread so a slow enumeration never blocks the UI.
        Only active when the port list was auto-detected.
        """
        while not st["stop"]:
            time.sleep(1.5)
            if st["stop"] or not auto_detect:
                if not auto_detect:
                    return
                continue
            try:
                current = enumerate_ports(include_all=include_all)
            except Exception:
                continue
            with lock:
                if set(current) == set(ports):
                    continue
                added = apply_port_diff(current)
            if added:
                scan_ports_bg(added)

    def start_scan():
        if st["scanning"]:
            return
        with lock:
            if auto_detect:
                try:
                    apply_port_diff(enumerate_ports(include_all=include_all))
                except Exception as e:
                    logs.append("port refresh failed: %s" % e)
            for i, p in enumerate(ports):
                rows[i] = _blank_row(p)
            scan_ports = list(ports)
            logs.append("--- scan start (%d ports @ %d baud) ---" % (len(scan_ports), baud))
        if not scan_ports:
            with lock:
                logs.append("--- no ports to scan ---")
            return
        st["scanning"] = True

        def worker():
            try:
                scan_parallel(scan_ports, baud, verbose, timeout, workers,
                              on_log=add_log, on_result=add_result)
            except Exception as e:
                add_log("scan error: %s" % e)
            with lock:
                acc = sum(1 for r in rows if r["status"].startswith("accessible"))
                logs.append("--- scan complete: %d/%d shells ---" % (acc, len(rows)))
            st["scanning"] = False

        threading.Thread(target=worker, daemon=True).start()

    def log_attr(line):
        low = line.lower()
        if "accessible" in low or "shells" in low:
            return GREEN
        if "timeout" in low or "inaccessible" in low or "silent" in low or "error" in low:
            return RED
        if "probing" in low:
            return curses.A_DIM
        return 0

    def draw():
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        with lock:
            snap = list(rows)
            loglines = list(logs)
        acc = sum(1 for r in snap if r["status"].startswith("accessible"))

        head = " UART Scanner   %d 8N1   %s" % (
            baud, "scanning..." if st["scanning"] else "done  %d/%d shells" % (acc, len(snap)))
        _tui_put(stdscr, 0, 0, head.ljust(w - 1), curses.A_REVERSE | B)

        Lw = max(20, min(34, w // 3))
        logh = max(5, (h - 7) // 3)
        mid = h - logh - 2                     # row of the horizontal separator
        _tui_put(stdscr, 1, 1, "PORTS", curses.A_DIM)
        _tui_put(stdscr, 1, Lw + 3, "DETAILS", curses.A_DIM)

        # --- port list (left) ---
        listrows = max(1, mid - 2)
        if st["sel"] < st["top"]:
            st["top"] = st["sel"]
        if st["sel"] >= st["top"] + listrows:
            st["top"] = st["sel"] - listrows + 1
        for k in range(listrows):
            ri = st["top"] + k
            if ri >= len(snap):
                break
            r = snap[ri]
            y = 2 + k
            kind = status_kind(r["status"])
            _tui_put(stdscr, y, 1, "  ", KIND_ATTR[kind] | curses.A_REVERSE)
            sel = (ri == st["sel"])
            label = "%-8s %s" % (r["port"], r["status"])
            _tui_put(stdscr, y, 4, label[:Lw - 4],
                     curses.A_REVERSE | B if sel else 0)

        try:
            stdscr.vline(2, Lw + 1, curses.ACS_VLINE, max(1, mid - 2))
        except curses.error:
            pass

        # --- details (right) ---
        if snap:
            r = snap[min(st["sel"], len(snap) - 1)]
            x = Lw + 3
            _tui_put(stdscr, 2, x, "%s  " % r["port"], B)
            _tui_put(stdscr, 2, x + len(r["port"]) + 2, r["status"],
                     KIND_ATTR[status_kind(r["status"])])
            dy = 4
            for label, val, _codes in _detail_fields(r):
                _tui_put(stdscr, dy, x, "%-7s: " % label, curses.A_DIM)
                _tui_put(stdscr, dy, x + 9, (val or "-"), FIELD_ATTR.get(label, 0))
                dy += 1
            if not r["info"] and r.get("note"):
                _tui_put(stdscr, dy + 1, x, "note   : ", curses.A_DIM)
                _tui_put(stdscr, dy + 1, x + 9, r["note"], curses.A_DIM)

        # --- log (bottom) ---
        try:
            stdscr.hline(mid, 0, curses.ACS_HLINE, w)
        except curses.error:
            pass
        vis = h - 1 - (mid + 1)
        total = len(loglines)
        maxoff = max(0, total - vis)
        st["log_off"] = min(st["log_off"], maxoff)
        tag = " LOG  (PgUp/PgDn scroll%s) " % ("  +%d" % st["log_off"] if st["log_off"] else "")
        _tui_put(stdscr, mid, 2, tag, B)
        start = max(0, total - vis - st["log_off"])
        for i in range(vis):
            li = start + i
            if li >= total:
                break
            _tui_put(stdscr, mid + 1 + i, 1, loglines[li], log_attr(loglines[li]))

        auto = "  auto-detect on" if auto_detect else ""
        foot = st["msg"] or (" up/down select | Enter open | r rescan all | PgUp/PgDn log | q quit"
                             + auto + " ")
        _tui_put(stdscr, h - 1, 0, foot.ljust(w - 1), curses.A_REVERSE)
        stdscr.refresh()

    start_scan()
    if auto_detect:
        threading.Thread(target=poll_ports, daemon=True).start()
    try:
        while True:
            draw()
            try:
                ch = stdscr.getch()
            except KeyboardInterrupt:
                break
            if ch == -1:
                continue
            st["msg"] = ""
            with lock:
                n = len(rows)
            if ch in (curses.KEY_UP, ord("k")):
                st["sel"] = max(0, st["sel"] - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                st["sel"] = min(n - 1, st["sel"] + 1)
            elif ch == curses.KEY_HOME:
                st["sel"] = 0
            elif ch == curses.KEY_END:
                st["sel"] = n - 1
            elif ch == curses.KEY_PPAGE:
                st["log_off"] += 5
            elif ch == curses.KEY_NPAGE:
                st["log_off"] = max(0, st["log_off"] - 5)
            elif ch in (ord("r"), ord("R")):
                start_scan()
            elif ch in (curses.KEY_ENTER, 10, 13):
                with lock:
                    r = rows[st["sel"]] if rows else None
                if r:
                    msg = open_port(r["port"], baud, opener_exe)
                    add_log(msg)
                    st["msg"] = " " + msg + " "
            elif ch in (ord("q"), 27):
                break
    finally:
        st["stop"] = True


def run_tui(ports, baud, timeout, workers, verbose, opener_exe,
            auto_detect=False, include_all=False):
    """Run the curses TUI. Returns True if it ran, False to fall back to CLI."""
    try:
        import curses  # noqa: F401  (windows-curses on Windows)
    except Exception as e:
        hint = "  Install it:  pip install windows-curses" if IS_WIN else ""
        print(f"[TUI disabled] the 'curses' module is not available ({e}).{hint}")
        print("Falling back to plain output. Use --no-tui to silence this.\n")
        return False
    try:
        curses.wrapper(lambda scr: _tui_loop(scr, ports, baud, timeout,
                                             workers, verbose, opener_exe,
                                             auto_detect, include_all))
        return True
    except Exception as e:                     # terminal restored by wrapper
        print(f"[TUI error] {e}; using plain output.\n")
        return False


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
    ap.add_argument("--no-tui", action="store_true",
                    help="use plain scrolling output instead of the full-screen TUI")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--no-resize", action="store_true",
                    help="do not shrink the console window on start")
    ap.add_argument("--all", action="store_true",
                    help="scan every port incl. phantom /dev/ttyS* (Linux)")
    args = ap.parse_args()

    if not args.no_resize:
        set_console_size()
    setup_color()          # enable ANSI colors (also turns on VT on Windows)

    if args.ports:
        ports = args.ports
        auto_detect = False
    else:
        ports = enumerate_ports(include_all=args.all)
        auto_detect = True

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

    # Resolve the "opener" up front (TeraTerm on Windows, a serial tool on Unix).
    if IS_WIN:
        opener = args.teraterm or find_teraterm() or DEFAULT_TERATERM
    else:
        opener = ""

    # Default experience: the full-screen TUI (unless disabled or non-interactive).
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if interactive and not args.no_tui:
        if run_tui(ports, args.baud, args.timeout, workers, args.verbose, opener,
                   auto_detect=auto_detect, include_all=args.all):
            return
        # else: TUI unavailable -> fall through to plain output

    print(f"Scanning {len(ports)} port(s) at {args.baud} baud "
          f"({workers} in parallel, hard timeout {args.timeout:g}s/port): "
          f"{', '.join(ports)}\n")

    results = scan_parallel(ports, args.baud, args.verbose, args.timeout, workers)

    # ---- summary ----  (kept ~96 cols wide so a small console doesn't wrap)
    W = 96
    print("\n" + rule(W, heavy=True))
    print(paint("  SUMMARY", BOLD))
    print(rule(W, heavy=True))
    print(paint(f"    {'PORT':<8} {'STATUS':<22} {'DEVICE':<32} "
                f"{'IP (iface:addr)':<18}", BOLD))
    print(rule(W))
    for r in results:
        info = r.get("info") or {}
        dev = info.get("model") or info.get("uname") or ""
        devcodes = COL_MODEL if info.get("model") else COL_SYSTEM
        has_ip = r["ip"] not in ("-", "")
        row = (f"  {status_glyph(r['status'])} "
               + paint(f"{r['port'][:8]:<8}", BOLD) + " "
               + paint(f"{r['status'][:22]:<22}", *status_style(r["status"])) + " "
               + (paint(f"{dev[:32]:<32}", *devcodes) if dev else f"{'-':<32}") + " "
               + (paint(f"{r['ip'][:18]:<18}", *COL_IP) if has_ip else f"{'-':<18}"))
        print(row)
    print(rule(W, heavy=True))

    accessible = [r for r in results if r["status"].startswith("accessible")]
    n = len(accessible)
    tail = paint(f"{n}", FG_GREEN, BOLD) if n else paint("0", FG_RED, BOLD)
    print(f"\n  {tail}/{len(results)} port(s) gave a shell.")

    # Detail block for anything we got into, one color per field (full, untruncated).
    for r in accessible:
        print(f"\n  {status_glyph(r['status'])} {paint(r['port'], BOLD)}  "
              f"{paint(r['status'], *status_style(r['status']))}")
        for label, val, codes in _detail_fields(r):
            if val:
                print("      " + paint(f"{label:<6}: ", DIM) + paint(val, *codes))

    # Opener note for the plain-output picker.
    if IS_WIN:
        opener_note = (f"TeraTerm: {opener}"
                       f"{'' if os.path.isfile(opener) else '   (NOT FOUND)'}")
    else:
        tool = next((n for n, _ in _SERIAL_TOOLS if shutil.which(n)), None)
        opener_note = (f"Serial console: {tool}" if tool else
                       "Serial console: none found (install tio/picocom/minicom/screen)")

    if not args.no_menu:
        print("\n" + opener_note)
        interactive_menu(results, args.baud, opener)


if __name__ == "__main__":
    # Required so PyInstaller/`spawn` child processes don't re-run main().
    mp.freeze_support()
    main()
