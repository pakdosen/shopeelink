# shopeelink

Konversi link pendek Shopee (`s.shopee.co.id/...`) menjadi link produk langsung
(`https://shopee.co.id/product/<shop_id>/<item_id>`). Mendukung satu atau
banyak link sekaligus, lewat **aplikasi desktop GUI** atau **CLI**.

## Contoh

```
Input  : https://s.shopee.co.id/1VvkmRGQgz
Output : https://shopee.co.id/product/2637287/23082544058
```

## Aplikasi Desktop (GUI) — paling mudah

| Light mode | Dark mode |
| :---: | :---: |
| ![GUI light mode](docs/screenshot-light.png) | ![GUI dark mode](docs/screenshot-dark.png) |

Dua cara, pilih salah satu:

### A. Download `.exe` (Windows, tanpa perlu Python)

1. Buka halaman [Releases](https://github.com/pakdosen/shopeelink/releases).
2. Download `ShopeeLinkConverter.exe` dari rilis terbaru.
3. Klik dua kali file `.exe`-nya — aplikasi langsung terbuka.

> Belum ada rilis? Repo owner bisa membuat tag (mis. `v1.0.0`) dan push tag tersebut. GitHub Actions akan otomatis build `.exe` dan attach ke release. Atau jalankan workflow `build-windows` secara manual dari tab **Actions** untuk mendapatkan artifact.

### B. Klik dua kali `run.bat` (Windows) / `run.sh` (Linux/macOS)

1. Pastikan Python 3.10+ sudah terinstal (https://www.python.org/downloads/, **centang "Add Python to PATH"** saat install).
2. Clone atau download repo:
   ```bash
   git clone https://github.com/pakdosen/shopeelink.git
   ```
3. Klik dua kali:
   - **Windows**: `run.bat`
   - **Linux/macOS**: `run.sh` (terminal: `./run.sh` — beri izin eksekusi dengan `chmod +x run.sh` bila perlu)

   Run pertama akan otomatis membuat virtualenv dan menginstal `customtkinter` (~1–2 menit). Setelah itu tinggal dobel-klik dan langsung jalan.

Cara pakai aplikasi:
1. Tempel link pendek Shopee — satu link per baris — di textarea **Input link**.
2. Klik **Convert**.
3. Hasil muncul di area **Hasil**. Klik **Copy results** untuk salin ke clipboard.

## Pemakaian via CLI

### Persyaratan

Python 3.10+ (CLI tidak butuh dependency tambahan; GUI butuh `customtkinter` — lihat `requirements.txt`).

### Satu link

```bash
python shopeelink.py https://s.shopee.co.id/1VvkmRGQgz
```

### Banyak link sekaligus (argumen)

```bash
python shopeelink.py \
    https://s.shopee.co.id/1VvkmRGQgz \
    https://s.shopee.co.id/AbCdEfGhIj
```

### Banyak link via stdin (satu link per baris)

```bash
cat urls.txt | python shopeelink.py
```

### Sertakan input pada output (format TSV)

```bash
python shopeelink.py --show-input https://s.shopee.co.id/1VvkmRGQgz
# https://s.shopee.co.id/1VvkmRGQgz<TAB>https://shopee.co.id/product/2637287/23082544058
```

### Atur timeout HTTP

```bash
python shopeelink.py --timeout 30 https://s.shopee.co.id/1VvkmRGQgz
```

## Cara kerja

1. Untuk link pendek (`s.shopee.*`), tool melakukan request HTTP dan mengikuti
   redirect hingga mencapai URL final.
2. Dari URL final, tool mengekstrak `shop_id` dan `item_id` dari path. Format
   yang didukung:
   - `/product/<shop_id>/<item_id>`
   - `/opaanlp/<shop_id>/<item_id>` (path landing affiliate Shopee)
   - `/<slug>-i.<shop_id>.<item_id>` (slug produk kanonik)
3. URL hasil dibangun ulang menjadi `https://<host>/product/<shop_id>/<item_id>`.

Karena tool ini hanya membaca header redirect, ia jauh lebih ringan dibanding
mengunduh seluruh halaman produk.

## Pemakaian sebagai library

```python
from shopeelink import convert, convert_many

convert("https://s.shopee.co.id/1VvkmRGQgz")
# -> "https://shopee.co.id/product/2637287/23082544058"

for url_in, url_out, err in convert_many(["https://s.shopee.co.id/1VvkmRGQgz"]):
    if err:
        print("gagal:", url_in, err)
    else:
        print(url_in, "->", url_out)
```

## Menjalankan tes

```bash
python -m unittest discover -s tests -v
```

Tes unit tidak melakukan akses jaringan.

## Exit code CLI

- `0` — semua link berhasil dikonversi.
- `1` — minimal satu link gagal (pesan error ditulis ke `stderr`).
- `2` — tidak ada input.

## Build `.exe` Windows secara lokal

Workflow `.github/workflows/build-windows.yml` dijalankan otomatis pada tag
`v*` dan via **Run workflow** (manual). Untuk build sendiri di Windows:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --windowed `
    --name "ShopeeLinkConverter" `
    --collect-all customtkinter `
    shopeelink_gui.py
# Hasil ada di dist\ShopeeLinkConverter.exe
```
