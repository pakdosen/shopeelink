# shopeelink

Konversi link pendek Shopee (`s.shopee.co.id/...`) menjadi link produk langsung
(`https://shopee.co.id/product/<shop_id>/<item_id>`). Mendukung satu atau
banyak link sekaligus.

## Contoh

```
Input  : https://s.shopee.co.id/1VvkmRGQgz
Output : https://shopee.co.id/product/2637287/23082544058
```

## Persyaratan

- Python 3.10+ (hanya menggunakan modul standar — tidak ada dependency tambahan).

## Pemakaian

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
