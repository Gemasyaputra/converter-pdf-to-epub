# PDF ke EPUB

Web app lokal untuk mengubah PDF menjadi EPUB.

- **Mode Teks**: teks dan gambar diekstrak menjadi EPUB *reflowable* (teks menyesuaikan layar). Cocok untuk buku/dokumen. Header/footer berulang dan nomor halaman dibuang, heading dideteksi dari ukuran font, bab dibuat dari daftar isi PDF (atau per 10 halaman jika tidak ada).
- **Mode Gambar**: tiap halaman dirender menjadi gambar, EPUB *fixed-layout*. Cocok untuk PDF hasil scan atau komik.

## Instalasi

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Menjalankan web app

```powershell
.\.venv\Scripts\python.exe app.py
```

Buka http://127.0.0.1:5000, pilih PDF, pilih mode, klik **Konversi**.

## Lewat command line

```powershell
.\.venv\Scripts\python.exe converter.py buku.pdf                     # mode teks -> buku.epub
.\.venv\Scripts\python.exe converter.py scan.pdf -m image -q kecil      # mode gambar, file ringan (kecil|sedang|tinggi)
.\.venv\Scripts\python.exe converter.py buku.pdf -o hasil.epub -t "Judul" -a "Penulis"
```

## Rapikan font & penulisan

Tersedia di tab **Rapikan EPUB** (upload EPUB yang sudah ada) dan di bagian **Rapikan font & penulisan** saat konversi PDF mode Teks:

- Font (serif / sans-serif / monospace / upload font sendiri), ukuran, jarak baris, rata kiri atau kanan-kiri, indentasi paragraf — dengan pratinjau langsung.
- Bersihkan teks: spasi ganda, spasi sebelum tanda baca, `kata.Kata` → `kata. Kata`, `...` → `…`, kutip lurus → kutip lengkung, ligatur rusak (`ﬁ` → `fi`), karakter tak terlihat.
- Gabung paragraf yang terpotong (paragraf tanpa tanda baca akhir + paragraf berikutnya diawali huruf kecil).
- Rapikan judul: `BAB II LATAR BELAKANG` → `Bab II Latar Belakang` (angka Romawi dan singkatan seperti PT tetap).

Lewat command line:

```powershell
.\.venv\Scripts\python.exe tidy.py buku.epub --font serif --size 110 --indent   # -> buku-rapi.epub
.\.venv\Scripts\python.exe tidy.py buku.epub --font-file MyFont.ttf -o hasil.epub
.\.venv\Scripts\python.exe converter.py buku.pdf --font sans --line-height 1.7
```

EPUB ber-DRM dan EPUB fixed-layout (mode Gambar) tidak bisa dirapikan.

## Catatan

- PDF yang diproteksi password tidak bisa dikonversi.
- PDF hasil scan **dengan** OCR: di mode teks hanya teks OCR yang diambil; gambar scan latarnya dilewati (halaman dengan ≥30 karakter teks dan gambar ≥75% halaman).
- PDF hasil scan **tanpa** OCR: di mode teks, halaman scan otomatis di-**OCR** dengan Tesseract (bahasa Indonesia + Inggris, ±3 detik/halaman) sehingga hasilnya teks yang bisa di-reflow. Opsi bisa dimatikan (centang "OCR halaman hasil scan" di web, atau `--no-ocr` di CLI; bahasa lain: `--ocr-lang eng`). Bila OCR tidak tersedia/gagal, halaman dimasukkan sebagai gambar JPEG terkompresi (grayscale bila hitam-putih, lebar ≤1200 px, target ≤150 KB).
- OCR butuh **Tesseract** (bukan ocrmypdf). Di komputer lain pasang dengan `winget install UB-Mannheim.TesseractOCR` dan sertakan data bahasa `ind` (folder `tessdata`). Lokasi dicari otomatis: `TESSDATA_PREFIX`, PATH, atau `C:\Program Files\Tesseract-OCR`.
- Gambar tertanam dikompres (JPEG kualitas 80→70, lebar maks 1200 px, gambar transparan besar di-flatten ke latar putih) dan disimpan sekali saja meski muncul di banyak halaman.
- Arsip EPUB: `mimetype` tanpa kompresi, file lain DEFLATE level 9.
- Tata letak rumit (multi-kolom, tabel) bisa tidak sempurna di mode teks.
