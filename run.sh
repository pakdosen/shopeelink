#!/usr/bin/env bash
# Klik dua kali file ini (atau jalankan ./run.sh) untuk membuka Shopee Link
# Converter di Linux/macOS. Pada run pertama akan dibuat virtualenv dan
# dependency diinstal otomatis (sekitar 2-3 menit; sekitar 30-60 detik
# untuk upgrade ke versi baru karena perlu unduh playwright).
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
    "$VENV_DIR/bin/python" -m pip install --upgrade pip
    "$VENV_DIR/bin/python" -m pip install -r requirements.txt
fi

# Make sure new dependencies (e.g. playwright in v1.3.0) are installed even
# if the venv was created by an older release. We intentionally do NOT pass
# --quiet so the user sees download progress for big wheels like playwright.
echo "[shopeelink] Memeriksa dependency..."
"$VENV_DIR/bin/python" -m pip install -r requirements.txt --disable-pip-version-check

echo "[shopeelink] Memvalidasi instalasi..."
if ! "$VENV_DIR/bin/python" -c "import customtkinter, playwright" 2>&1; then
    echo
    echo "[shopeelink] Gagal mengimpor dependency yang baru saja di-install."
    echo "Coba hapus folder '.venv' lalu jalankan ulang run.sh."
    exit 1
fi

echo "[shopeelink] Membuka aplikasi..."
exec "$VENV_DIR/bin/python" shopeelink_gui.py "$@"
