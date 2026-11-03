"""Penulisan arsip EPUB (ZIP) sesuai standar OCF.

- `mimetype` menjadi entri pertama dan disimpan tanpa kompresi (ZIP_STORED).
- Semua entri lain (XHTML, CSS, gambar, OPF, dll.) dikompresi DEFLATE level 9.

Dibutuhkan karena ebooklib 0.21 menulis setiap file memakai `ZipInfo` yang
default-nya ZIP_STORED, sehingga isi EPUB-nya sama sekali tidak terkompresi.
"""

import os
import tempfile
import zipfile

MIMETYPE = b"application/epub+zip"
COMPRESS_LEVEL = 9


def write_epub_zip(out_path, entries):
    """Tulis EPUB dari `entries` (iterable (nama, bytes)); entri `mimetype` diabaikan."""
    with zipfile.ZipFile(out_path, "w") as zout:
        zout.writestr(zipfile.ZipInfo("mimetype"), MIMETYPE, compress_type=zipfile.ZIP_STORED)
        for name, data in entries:
            if name == "mimetype" or name.endswith("/"):
                continue
            zout.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED,
                          compresslevel=COMPRESS_LEVEL)


def repack_epub(path):
    """Kemas ulang EPUB yang sudah ada di `path` dengan kompresi yang benar."""
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(suffix=".epub", dir=folder)
    os.close(fd)
    try:
        with zipfile.ZipFile(path) as zin:
            write_epub_zip(tmp, ((info.filename, zin.read(info)) for info in zin.infolist()))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
