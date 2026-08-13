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


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
DEFAULT_BAUD = 115200
READ_TIMEOUT = 0.4          # per-read timeout (s)
SETTLE       = 0.4          # wait after writing before reading (s)
LOGIN_RETRIES = 2           # how many newline pokes to try to raise a prompt
PORT_TIMEOUT = 5            # hard wall-clock budget per port (s); watchdog aborts

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
# Marker-based command execution (survives kernel-log spam, never throws)
# ---------------------------------------------------------------------------
MARK = "XUSCANX"                                   # unlikely to appear naturally
KLOG = re.compile(r"^\[\s*\d+\.\d+\]")             # kernel log line: [  12.345] ...


def raw_exec(ser, cmd, timeout=4.0):
    """
    Run `cmd` bracketed by echo markers and return (ok, output_lines).

    We send:  echo MARKs; <cmd>; echo MARKe
    then read until the end marker appears. Only text between the markers is
    kept, so an interleaving kernel-log line or a noisy prompt can't corrupt
    the parse, and `ok` is a reliable "a shell actually ran this" signal.
    """
    try:
        ser.reset_input_buffer()
        ser.write(f"echo {MARK}s; {cmd}; echo {MARK}e\r\n".encode())
        ser.flush()
    except Exception:
        return False, []

    buf = ""
    end = time.time() + timeout
    while time.time() < end:
        buf += drain(ser, 0.2)
        if (MARK + "e") in buf and buf.count(MARK) >= 3:
            break

    ok = (MARK + "s") in buf and (MARK + "e") in buf
    seg = buf
    if ok:
        # take the LAST bracketed region (ignores the echoed command line)
        seg = buf.rsplit(MARK + "e", 1)[0]
        seg = seg.rsplit(MARK + "s", 1)[1]

    lines = []
    for ln in seg.splitlines():
        s = ln.strip()
        if not s or MARK in s or KLOG.match(s):
            continue
        lines.append(s)
    return ok, lines


def shell_alive(ser):
    """True if a shell responds to a bracketed no-op (markers echo back)."""
    ok, _ = raw_exec(ser, "true", timeout=3.0)
    return ok


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


# ---------------------------------------------------------------------------
# Device fingerprinting
# ---------------------------------------------------------------------------
def _first(lines, reject=("no such", "not found", "command not found")):
    for l in lines:
        low = l.lower()
        if l and not any(r in low for r in reject):
            return l.replace("\x00", "").strip()
    return None


def fingerprint(ser):
    """Ask the shell what it is. Return a short type string."""
    bits = []

    # Board / SoC model from the device tree (great for embedded Linux boards).
    ok, lines = raw_exec(ser, "cat /proc/device-tree/model 2>/dev/null")
    m = _first(lines)
    if m:
        bits.append("model=" + m)

    # Distro pretty name.
    ok, lines = raw_exec(ser, "grep -h PRETTY_NAME /etc/os-release 2>/dev/null")
    for l in lines:
        if "PRETTY_NAME" in l and "=" in l:
            bits.append(l.split("=", 1)[1].strip().strip('"'))
            break

    # Kernel / arch.
    ok, lines = raw_exec(ser, "uname -srm")
    u = _first(lines)
    if u and ("Linux" in u or "BSD" in u or "GNU" in u):
        bits.append(u)

    # Hostname (always useful, cheap identifier).
    ok, lines = raw_exec(ser, "hostname")
    h = _first(lines)
    if h:
        bits.append("host=" + h)

    # De-dup while preserving order.
    bits = list(dict.fromkeys(b for b in bits if b))
    return " | ".join(bits) if bits else "unknown shell"


def get_ip(ser):
    """
    Return interface:ip pairs for non-loopback IPv4 addresses.

    Handles both busybox `ifconfig` (iface on its own line, `inet addr:` on the
    next) and iproute2 `ip -o -4 addr` (`N: iface  inet 1.2.3.4/24 ...`).
    """
    ok, lines = raw_exec(ser, "ifconfig 2>/dev/null || ip -o -4 addr 2>/dev/null")
    pairs = []
    iface = "?"
    for ln in lines:
        toks = ln.split()
        # ifconfig: interface name starts a non-indented, non-'inet' line.
        if toks and not ln[:1].isspace() and not ln.lstrip().lower().startswith("inet"):
            iface = toks[0].rstrip(":")
        # iproute2 -o form:  "2: eth0    inet 192.168.1.5/24 ..."
        if len(toks) >= 2 and toks[0].rstrip(":").isdigit():
            iface = toks[1].rstrip(":")

        m = re.search(r"inet (?:addr:)?(\d{1,3}(?:\.\d{1,3}){3})", ln)
        if m:
            ip = m.group(1)
            if not ip.startswith("127."):
                pairs.append(f"{iface}:{ip}")

    pairs = list(dict.fromkeys(pairs))
    return ", ".join(pairs) if pairs else "-"


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
        # Collect whatever is already coming out, and poke for a prompt.
        banner = drain(ser, 0.6)
        banner += send(ser, "")
        banner += send(ser, "")

        login_seen = looks_like(banner, LOGIN_PROMPTS) and not is_shell(banner)

        # If a login prompt is showing, log in as root with no password.
        if login_seen:
            reply = send(ser, "root", settle=1.0)
            banner += reply
            if verbose:
                print(f"--- {port} after 'root' ---\n{reply!r}\n")
            if looks_like(reply, PASSWORD_PROMPTS):
                banner += send(ser, "", settle=1.0)   # empty password
            send(ser, "")                              # settle to a prompt

        # Definitive shell test (works for: open shell, post-login shell, or a
        # console that's just spewing kernel logs but still has a live shell).
        alive = shell_alive(ser)
        if not alive:
            send(ser, "")
            time.sleep(0.4)
            alive = shell_alive(ser)          # one retry (slow / mid-boot boards)

        if verbose:
            print(f"--- {port} banner ---\n{banner!r}\n  shell_alive={alive}\n")

        if alive:
            result["status"] = "accessible (root)" if login_seen else "accessible (open shell)"
            result["type"] = fingerprint(ser)
            result["ip"] = get_ip(ser)
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
    ctx = mp.get_context("spawn")           # Windows-safe
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


def _getch():
    """Read one keypress on Windows, returning a normalized token."""
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


def _menu_label(r):
    return f"{r['port']:<7} {r['status']:<24} {r['type'][:34]}"


def _menu_numbered(choices, baud, exe):
    """Fallback picker for consoles without VT / keypress support: type a number."""
    while True:
        print("\nResponding ports:")
        for i, r in enumerate(choices, 1):
            print(f"  {i}. {_menu_label(r)}")
        try:
            raw = input("Open which in TeraTerm? (number, or q to quit): ").strip()
        except EOFError:
            return
        if raw.lower() in ("q", "quit", "", "exit"):
            return
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            print("  " + launch_teraterm(choices[int(raw) - 1]["port"], baud, exe))
        else:
            print("  ? enter a listed number, or q to quit")


def _menu_arrows(choices, baud, exe):
    """
    Arrow-key picker with reverse-video highlight (requires VT + msvcrt).

    Draws a fixed-height block and repaints it in place on every keypress, so
    selecting a port does not scroll a fresh copy down the screen. The launch
    result is shown on a status line inside the block.
    """
    sel = 0
    status = "\u2191/\u2193 move \u00b7 Enter open \u00b7 Esc/q quit"
    header = "Select a port to open in TeraTerm:"
    block = len(choices) + 3          # header + rows + blank + status
    first = True

    while True:
        if not first:
            sys.stdout.write(f"\x1b[{block}A")   # jump back to top of block
        first = False
        sys.stdout.write("\x1b[0J")              # erase everything below

        print(header)
        for i, r in enumerate(choices):
            marker = ">" if i == sel else " "
            label = _menu_label(r)
            if i == sel:
                print(f" {marker} \x1b[7m{label}\x1b[0m")
            else:
                print(f" {marker} {label}")
        print("")
        print(status)

        key = _getch()
        if key == "up":
            sel = (sel - 1) % len(choices)
        elif key == "down":
            sel = (sel + 1) % len(choices)
        elif key == "enter":
            status = launch_teraterm(choices[sel]["port"], baud, exe)
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
    if os.name == "nt":
        try:
            import msvcrt  # noqa: F401
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
    args = ap.parse_args()

    if not args.no_resize:
        set_console_size()

    if args.ports:
        ports = args.ports
    else:
        ports = [p.device for p in list_ports.comports()]

    if not ports:
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
    fmt = "{:<7} {:<24} {:<40} {:<22}"
    print(fmt.format("PORT", "STATUS", "TYPE / DEVICE", "IP (iface:addr)"))
    print("-" * W)
    for r in results:
        typ = r["type"] if r["type"] != "-" else (r["note"] or "-")
        print(fmt.format(r["port"], r["status"][:24], typ[:40], r["ip"][:22]))
    print("=" * W)

    accessible = [r for r in results if r["status"].startswith("accessible")]
    print(f"\n{len(accessible)}/{len(results)} port(s) gave a shell.")

    # Detail block for anything we got into, so long fields aren't truncated.
    for r in accessible:
        print(f"\n  {r['port']}  [{r['status']}]")
        print(f"      device: {r['type']}")
        print(f"      ip    : {r['ip']}")

    # Resolve TeraTerm: explicit flag > auto-detect > legacy default.
    teraterm = args.teraterm or find_teraterm() or DEFAULT_TERATERM
    if not args.no_menu:
        print(f"\nTeraTerm: {teraterm}"
              f"{'' if os.path.isfile(teraterm) else '   (NOT FOUND)'}")
        interactive_menu(results, args.baud, teraterm)


if __name__ == "__main__":
    # Required so PyInstaller/`spawn` child processes don't re-run main().
    mp.freeze_support()
    main()
