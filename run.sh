#!/usr/bin/env bash
# Klik dua kali file ini (atau jalankan ./run.sh) untuk membuka Shopee Link
# Converter di Linux/macOS. Pada run pertama akan dibuat virtualenv dan
# customtkinter diinstal otomatis.
set -e

cd "$(dirname "$0")"

VENV_DIR=".venv"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "[shopeelink] Menyiapkan environment untuk pertama kali..."
    if command -v python3 >/dev/null 2>&1; then
        PY=python3
    elif command -v python >/dev/null 2>&1; then
        PY=python
    else
        echo "Python tidak ditemukan. Install Python 3.10+ terlebih dahulu." >&2
        exit 1
    fi
    "$PY" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
    "$VENV_DIR/bin/python" -m pip install -r requirements.txt
fi

# Make sure new dependencies (e.g. playwright in v1.3.0) are installed even
# if the venv was created by an older release.
"$VENV_DIR/bin/python" -m pip install -r requirements.txt --quiet --disable-pip-version-check

exec "$VENV_DIR/bin/python" shopeelink_gui.py "$@"
