#!/usr/bin/env bash
# ===========================================================================
# install.sh  -  set up the UART scanner on Linux / macOS
#   * installs the pyserial dependency
#   * installs uart_scan.py as `uart-scan` in ~/.local/bin
#   * (Linux) creates a .desktop launcher
#   * prints serial-permission / tool hints
# Run:  ./install.sh          (no root needed for a per-user install)
# ===========================================================================
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bindir="${HOME}/.local/bin"
target="${bindir}/uart-scan"

# --- Python ---------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. Install it with your package manager, e.g.:"
    echo "  Debian/Ubuntu:  sudo apt install python3 python3-pip python3-serial"
    echo "  Fedora:         sudo dnf install python3 python3-pyserial"
    echo "  Arch:           sudo pacman -S python python-pyserial"
    exit 1
fi

# --- pyserial -------------------------------------------------------------
if python3 -c "import serial" >/dev/null 2>&1; then
    echo "pyserial: already present"
else
    echo "Installing pyserial..."
    if python3 -m pip install --user pyserial >/dev/null 2>&1; then
        echo "pyserial: installed via pip (--user)"
    else
        echo "pip install failed (often PEP 668 'externally managed')."
        echo "Install the distro package instead, e.g.:"
        echo "  sudo apt install python3-serial   # or python3-pyserial"
        echo "...then re-run this script."
        exit 1
    fi
fi

# --- install the script ---------------------------------------------------
mkdir -p "${bindir}"
install -m 0755 "${here}/uart_scan.py" "${target}"
echo "installed: ${target}"

case ":${PATH}:" in
    *":${bindir}:"*) : ;;
    *) echo "note: ${bindir} is not on your PATH -- add to ~/.bashrc:"
       echo "      export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# --- desktop launcher (Linux) --------------------------------------------
if command -v update-desktop-database >/dev/null 2>&1 || [ -d "${HOME}/.local/share/applications" ]; then
    appdir="${HOME}/.local/share/applications"
    mkdir -p "${appdir}"
    cat > "${appdir}/uart-scanner.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=UART Scanner
Comment=Scan serial ports for a shell
Exec=${target}
Terminal=true
Categories=Development;Utility;
EOF
    echo "installed: ${appdir}/uart-scanner.desktop"
fi

# --- hints ----------------------------------------------------------------
echo
echo "Serial permissions: your user must be able to read /dev/tty* ."
if getent group dialout >/dev/null 2>&1; then
    if id -nG "$USER" | grep -qw dialout; then
        echo "  dialout group: OK"
    else
        echo "  add yourself:  sudo usermod -aG dialout \"$USER\"   (then log out/in)"
    fi
fi

have_tool=""
for t in tio picocom minicom screen cu; do
    if command -v "$t" >/dev/null 2>&1; then have_tool="$t"; break; fi
done
if [ -n "${have_tool}" ]; then
    echo "Serial console tool: ${have_tool} (used by the 'open port' menu)"
else
    echo "Serial console tool: none found -- install one to open ports from the menu:"
    echo "  sudo apt install tio      # or picocom / minicom / screen"
fi

echo
echo "Done. Run:  uart-scan            (or ${target})"
