"""OCR halaman PDF hasil scan memakai Tesseract (lewat PyMuPDF).

Tidak butuh ocrmypdf/Ghostscript: cukup Tesseract terpasang beserta data bahasanya.
"""

import glob
import os
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

import pymupdf

OCR_DPI = 300
MAX_WORKERS = 8  # jumlah halaman yang di-OCR bersamaan
PREFERRED_LANGUAGES = ("ind", "eng")
TESSDATA_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tessdata",
    r"C:\Program Files (x86)\Tesseract-OCR\tessdata",
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tesseract-ocr/4.00/tessdata",
    "/usr/local/share/tessdata",
    "/opt/homebrew/share/tessdata",
)


def _has_languages(folder):
    return bool(folder) and bool(glob.glob(os.path.join(folder, "*.traineddata")))


def find_tessdata():
    """Lokasi folder `tessdata` Tesseract, atau None bila tidak ditemukan."""
    candidates = [os.environ.get("TESSDATA_PREFIX")]
    exe = shutil.which("tesseract")
    if exe:
        candidates.append(os.path.join(os.path.dirname(exe), "tessdata"))
    candidates.extend(TESSDATA_CANDIDATES)
    return next((c for c in candidates if _has_languages(c)), None)


def ocr_languages(tessdata):
    return sorted(os.path.splitext(os.path.basename(f))[0]
                  for f in glob.glob(os.path.join(tessdata, "*.traineddata")))


def default_language(tessdata):
    available = [lang for lang in ocr_languages(tessdata) if lang != "osd"]
    preferred = [lang for lang in PREFERRED_LANGUAGES if lang in available]
    return "+".join(preferred or available[:1])


def ocr_available():
    tessdata = find_tessdata()
    return bool(tessdata and default_language(tessdata))


def ocr_page_blocks(page, language, tessdata, dpi=OCR_DPI):
    """OCR satu halaman; kembalikan blok teks dengan struktur sama seperti get_text("dict")."""
    textpage = page.get_textpage_ocr(language=language, dpi=dpi, full=True, tessdata=tessdata)
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    return page.get_text("dict", textpage=textpage, flags=flags)["blocks"]


# ---------------------------------------------------------------- OCR paralel

_worker = {}


def _init_worker(pdf_path, language, tessdata):
    os.environ["OMP_THREAD_LIMIT"] = "1"  # 1 thread per proses: core dibagi antar halaman
    _worker.update(doc=pymupdf.open(pdf_path), language=language, tessdata=tessdata)


def _ocr_worker(pno):
    try:
        page = _worker["doc"][pno]
        return pno, ocr_page_blocks(page, _worker["language"], _worker["tessdata"])
    except Exception:
        return pno, None


def _ocr_sequential(pdf_path, pages, language, tessdata, done):
    doc = pymupdf.open(pdf_path)
    try:
        for pno in pages:
            try:
                blocks = ocr_page_blocks(doc[pno], language, tessdata)
            except Exception:
                blocks = None
            done(pno, blocks)
    finally:
        doc.close()


def ocr_pages(pdf_path, pages, language, tessdata, progress=None):
    """OCR beberapa halaman sekaligus memakai banyak core CPU.

    Return {nomor_halaman: blok teks}; halaman yang gagal di-OCR tidak ada di hasil.
    `progress(selesai, total)` dipanggil setiap satu halaman selesai.
    """
    results = {}
    pages = list(pages)
    if not pages:
        return results

    def done(pno, blocks):
        if blocks is not None:
            results[pno] = blocks
        if progress:
            progress(len(done.seen) + 1, len(pages))
        done.seen.add(pno)
    done.seen = set()

    # sisakan 1 core agar komputer tetap responsif; maks 8 proses (tiap proses OCR makan RAM)
    workers = min(len(pages), MAX_WORKERS, max(1, (os.cpu_count() or 2) - 1))
    if workers == 1:
        _ocr_sequential(pdf_path, pages, language, tessdata, done)
        return results
    try:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                                 initargs=(pdf_path, language, tessdata)) as pool:
            for future in as_completed([pool.submit(_ocr_worker, p) for p in pages]):
                done(*future.result())
    except (BrokenProcessPool, OSError):
        # proses paralel tidak bisa dijalankan: lanjutkan halaman sisanya berurutan
        _ocr_sequential(pdf_path, [p for p in pages if p not in done.seen],
                        language, tessdata, done)
    return results
