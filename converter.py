"""Konversi PDF ke EPUB.

Dua mode:
- "text"  : ekstrak teks + gambar jadi EPUB reflowable (cocok untuk buku/dokumen).
- "image" : render tiap halaman jadi gambar, EPUB fixed-layout (cocok untuk scan/komik).

Bisa dipakai sebagai modul (fungsi `convert`) atau CLI:
    python converter.py input.pdf -m text -o output.epub
"""

import argparse
import hashlib
import html
import os
import re
import uuid
from collections import Counter

import pymupdf
from ebooklib import epub

from epubzip import repack_epub
from ocr import default_language, find_tessdata, ocr_pages

from tidy import (FONT_TYPES, add_tidy_args, build_css, fix_heading_text, font_ext,
                  options_from_args, tidy_fragment)

PAGES_PER_CHAPTER = 10  # dipakai bila PDF tidak punya daftar isi
MARGIN_RATIO = 0.08  # area atas/bawah halaman yang dianggap header/footer
MIN_IMAGE_SIDE = 50  # gambar lebih kecil dari ini (px) dianggap hiasan, dibuang

# Mode teks: kompresi gambar tertanam
IMAGE_MAX_W = 1200  # lebar maksimal (px)
IMAGE_MAX_H = 1600  # tinggi maksimal (px), untuk gambar yang sangat tinggi
IMAGE_MIN_W = 800  # batas bawah saat memperkecil demi mengejar target ukuran
IMAGE_BUDGET = 150 * 1024  # target ukuran per gambar
IMAGE_JPEG_QUALITIES = (80, 75, 70)
SMALL_ALPHA_PX = 400  # gambar transparan sekecil ini (ikon/logo) boleh tetap PNG

# Gambar latar scan di belakang teks OCR dilewati agar isi tidak dobel
SCAN_MIN_TEXT = 30  # minimal karakter teks (non-spasi) di halaman
SCAN_COVER_RATIO = 0.75  # gambar menutupi minimal sekian bagian halaman

# Mode gambar: DPI render, kualitas JPEG, dan batas sisi terpanjang (px)
QUALITY_PRESETS = {
    "kecil": {"dpi": 96, "jpeg": 60, "max_px": 1200},
    "sedang": {"dpi": 120, "jpeg": 72, "max_px": 1600},
    "tinggi": {"dpi": 150, "jpeg": 85, "max_px": 2200},
}
DEFAULT_QUALITY = "sedang"

TEXT_CSS = """
body { font-family: serif; line-height: 1.5; margin: 0 5%; }
h1, h2, h3 { font-family: sans-serif; line-height: 1.25; }
p { margin: 0 0 0.8em 0; text-align: justify; }
figure { margin: 1em 0; text-align: center; }
figure img { max-width: 100%; height: auto; }
"""


# ---------------------------------------------------------------- utilitas

def _open_pdf(pdf_path):
    try:
        doc = pymupdf.open(pdf_path, filetype="pdf")
    except (pymupdf.FileDataError, RuntimeError):
        raise ValueError("File bukan PDF yang valid atau rusak.") from None
    if doc.needs_pass:
        doc.close()
        raise ValueError("PDF terproteksi password, tidak bisa dikonversi.")
    if doc.page_count == 0:
        doc.close()
        raise ValueError("PDF tidak memiliki halaman.")
    return doc


def _metadata(doc, pdf_path, title, author):
    meta = doc.metadata or {}
    title = (title or meta.get("title") or "").strip()
    if not title:
        title = os.path.splitext(os.path.basename(pdf_path))[0]
    author = (author or meta.get("author") or "").strip() or "Tidak diketahui"
    return title, author


def _new_book(title, author, language):
    book = epub.EpubBook()
    book.set_identifier(f"urn:uuid:{uuid.uuid4()}")
    book.set_title(title)
    book.set_language(language)
    book.add_author(author)
    return book


def _chapter_starts(doc):
    """Daftar (judul, halaman_awal_0based) dari outline PDF level teratas."""
    toc = doc.get_toc(simple=True)
    if not toc:
        return []
    top = min(level for level, _, _ in toc)
    starts = []
    for level, title, page in toc:
        if level != top or page < 1 or page > doc.page_count:
            continue
        if starts and starts[-1][1] == page - 1:
            continue  # dua bab di halaman sama: ambil yang pertama
        starts.append((title.strip() or f"Halaman {page}", page - 1))
    starts.sort(key=lambda s: s[1])
    return starts


def _chapter_ranges(doc):
    """Bagi halaman menjadi bab: list of (judul, [index halaman])."""
    n = doc.page_count
    starts = _chapter_starts(doc)
    if not starts:
        return [
            (f"Halaman {i + 1}–{min(i + PAGES_PER_CHAPTER, n)}",
             list(range(i, min(i + PAGES_PER_CHAPTER, n))))
            for i in range(0, n, PAGES_PER_CHAPTER)
        ]
    ranges = []
    if starts[0][1] > 0:
        ranges.append(("Awal", list(range(0, starts[0][1]))))
    for idx, (title, start) in enumerate(starts):
        end = starts[idx + 1][1] if idx + 1 < len(starts) else n
        ranges.append((title, list(range(start, end))))
    return ranges


def _add_cover(book, doc, create_page):
    """Cover dari halaman pertama, sisi terpanjang maks 800 px."""
    page = doc[0]
    dpi = min(100, 800 / (max(page.rect.width, page.rect.height) / 72))
    cs = pymupdf.csGRAY if _is_grayscale(page) else pymupdf.csRGB
    pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), colorspace=cs, alpha=False)
    book.set_cover("images/cover.jpg", pix.tobytes("jpeg", jpg_quality=70),
                   create_page=create_page)


# ---------------------------------------------------------------- mode teks

def _line_spans(line):
    """Span non-kosong dari satu baris: list of (teks, bold, italic, size)."""
    spans = []
    for s in line["spans"]:
        if not s["text"]:
            continue
        bold = bool(s["flags"] & 16) or "bold" in s["font"].lower()
        italic = bool(s["flags"] & 2) or "italic" in s["font"].lower()
        spans.append((s["text"], bold, italic, s["size"]))
    return spans


def _normalize(text):
    return re.sub(r"\d+", "#", text.strip().lower())


def _text_char_count(blocks):
    return sum(len(re.sub(r"\s", "", s["text"]))
               for b in blocks if b["type"] == 0 for line in b["lines"] for s in line["spans"])


def _needs_ocr(page, blocks):
    """Halaman scan tanpa lapisan teks: hampir tanpa teks + gambar menutupi sebagian besar halaman."""
    if _text_char_count(blocks) >= SCAN_MIN_TEXT:
        return False
    page_area = page.rect.width * page.rect.height
    return any((pymupdf.Rect(info["bbox"]) & page.rect).get_area() >= SCAN_COVER_RATIO * page_area
               for info in page.get_image_info())


def _collect_stats(blocks, page_height, sizes, running_counter):
    """Tambahkan statistik satu halaman: ukuran font & teks di area header/footer."""
    seen = set()
    for block in blocks:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for s in line["spans"]:
                sizes[round(s["size"], 1)] += len(s["text"].strip())
            y0, y1 = line["bbox"][1], line["bbox"][3]
            if y1 < page_height * MARGIN_RATIO or y0 > page_height * (1 - MARGIN_RATIO):
                text = "".join(s["text"] for s in line["spans"])
                if text.strip():
                    seen.add(_normalize(text))
    running_counter.update(seen)


def _analyze(doc, report, ocr=None):
    """Baca teks seluruh PDF untuk mencari:
    - header/footer yang berulang di banyak halaman (dinormalisasi)
    - ukuran font isi (paling banyak dipakai)
    - (bila `ocr` diisi) teks OCR untuk halaman hasil scan, dikerjakan paralel

    Return (running, body_size, ocr_blocks) dengan ocr_blocks = {nomor_halaman: blok teks}.
    """
    running_counter = Counter()
    sizes = Counter()
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES  # tanpa gambar = lebih cepat
    n = doc.page_count

    # 1) baca lapisan teks (cepat) & catat halaman scan yang perlu OCR
    to_ocr = []
    read_share = 10 if ocr else 40  # bila ada OCR, sebagian besar waktu dipakai OCR
    for i, page in enumerate(doc):
        blocks = page.get_text("dict", flags=flags)["blocks"]
        if ocr and _needs_ocr(page, blocks):
            to_ocr.append(i)
        else:
            _collect_stats(blocks, page.rect.height, sizes, running_counter)
        report(int(read_share * (i + 1) / n), f"Menganalisis halaman {i + 1}/{n}")

    # 2) OCR halaman scan sekaligus di beberapa core CPU
    ocr_blocks = {}
    if to_ocr:
        def ocr_progress(done, total):
            report(10 + int(30 * done / total), f"OCR halaman {done}/{total}")

        report(10, f"OCR halaman 0/{len(to_ocr)}")
        ocr_blocks = ocr_pages(ocr["pdf_path"], to_ocr, ocr["language"], ocr["tessdata"],
                               ocr_progress)
        # 3) statistik dari teks hasil OCR (halaman yang gagal di-OCR tetap jadi gambar)
        for i, blocks in ocr_blocks.items():
            _collect_stats(blocks, doc[i].rect.height, sizes, running_counter)
    report(40, "Analisis selesai")

    running = set()
    if n >= 3:
        threshold = max(2, n // 2)
        running = {t for t, c in running_counter.items() if c >= threshold}
    body_size = sizes.most_common(1)[0][0] if sizes else 11.0
    return running, body_size, ocr_blocks


def _is_margin_noise(line, page_height, running, body_size):
    y0, y1 = line["bbox"][1], line["bbox"][3]
    if not (y1 < page_height * MARGIN_RATIO or y0 > page_height * (1 - MARGIN_RATIO)):
        return False
    text = "".join(s["text"] for s in line["spans"]).strip()
    if re.fullmatch(r"[\divxlcIVXLC\-– ]+|(halaman|page|hal\.?) *\d+", text, re.I):
        return True  # nomor halaman
    if line["spans"] and max(s["size"] for s in line["spans"]) >= body_size * 1.2:
        return False  # berukuran judul (mis. "Bab 3" di atas halaman), bukan header berulang
    return _normalize(text) in running


def _spans_to_html(spans):
    out = []
    for text, bold, italic, _ in spans:
        t = html.escape(text)
        if italic:
            t = f"<em>{t}</em>"
        if bold:
            t = f"<strong>{t}</strong>"
        out.append(t)
    # gabungkan tag yang bersebelahan: </strong><strong> -> ""
    return re.sub(r"</(strong|em)><\1>", "", "".join(out))


def _join_lines(lines):
    """Gabungkan baris-baris menjadi satu list span, menangani pemenggalan kata."""
    merged = []
    for spans in lines:
        if not spans:
            continue
        if merged:
            prev_text = merged[-1][0]
            next_text = spans[0][0]
            if prev_text.endswith("-") and next_text[:1].islower():
                last = merged[-1]
                merged[-1] = (prev_text[:-1],) + last[1:]
            elif not prev_text.endswith(" ") and not next_text.startswith(" "):
                last = merged[-1]
                merged[-1] = (prev_text + " ",) + last[1:]
        merged.extend(spans)
    return merged


def _heading_level(spans, body_size):
    text = "".join(s[0] for s in spans).strip()
    if not text or len(text) > 200:
        return None
    size = max(s[3] for s in spans)
    if size >= body_size * 1.5:
        return 1
    if size >= body_size * 1.2:
        return 2
    return None


def _page_html(page, book, image_cache, running, body_size, ocr_blocks=None):
    parts = []
    height = page.rect.height
    if ocr_blocks is not None and _text_char_count(ocr_blocks) >= SCAN_MIN_TEXT:
        # halaman scan dengan hasil OCR cukup: gambar scan pasti dilewati, jadi tidak perlu
        # men-decode gambarnya (lambat untuk scan resolusi tinggi)
        blocks = ocr_blocks
    else:
        blocks = page.get_text("dict")["blocks"]
        if ocr_blocks is not None:
            # OCR menghasilkan sedikit teks: gambar scan tetap dipakai
            blocks = [b for b in blocks if b["type"] == 1] + ocr_blocks

    # jumlah karakter teks isi (termasuk OCR tak terlihat, tanpa header/footer/nomor halaman)
    text_chars = sum(
        len(re.sub(r"\s", "", "".join(s["text"] for s in line["spans"])))
        for block in blocks if block["type"] == 0
        for line in block["lines"] if not _is_margin_noise(line, height, running, body_size)
    )
    page_area = page.rect.width * page.rect.height

    for block in blocks:
        if block["type"] == 1:
            covered = (pymupdf.Rect(block["bbox"]) & page.rect).get_area()
            if text_chars >= SCAN_MIN_TEXT and covered >= SCAN_COVER_RATIO * page_area:
                continue  # latar scan di belakang teks OCR: cukup teksnya
            parts.append(_image_html(block, page, book, image_cache))
            continue

        paragraph = []

        def flush():
            if paragraph:
                body = _spans_to_html(_join_lines(paragraph)).strip()
                if body:
                    parts.append(f"<p>{body}</p>")
                paragraph.clear()

        for line in block["lines"]:
            if _is_margin_noise(line, height, running, body_size):
                continue
            spans = _line_spans(line)
            if not spans:
                continue
            level = _heading_level(spans, body_size)
            if level:
                flush()
                text = html.escape("".join(s[0] for s in spans).strip())
                # baris heading berturut-turut dengan level sama digabung
                prev = parts[-1] if parts else ""
                if prev.startswith(f"<h{level}>"):
                    parts[-1] = prev[: -len(f"</h{level}>")] + f" {text}</h{level}>"
                else:
                    parts.append(f"<h{level}>{text}</h{level}>")
            else:
                paragraph.append(spans)
        flush()
    return "\n".join(p for p in parts if p)


def _image_html(block, page, book, image_cache):
    data = block.get("image")
    if not data or block["width"] < MIN_IMAGE_SIDE or block["height"] < MIN_IMAGE_SIDE:
        return ""
    # dedupe berdasarkan byte asli: gambar yang sama di banyak halaman disimpan sekali
    digest = hashlib.sha1(data).hexdigest()[:16]
    if digest not in image_cache:
        data, ext = _compress_image(data, block.get("ext", ""), page, block["bbox"])
        if data is None:
            image_cache[digest] = None
            return ""
        name = f"images/img_{digest}.{ext}"
        media = "image/jpeg" if ext in ("jpeg", "jpg") else f"image/{ext}"
        book.add_item(epub.EpubImage(uid=f"img_{digest}", file_name=name,
                                     media_type=media, content=data))
        image_cache[digest] = name
    if image_cache[digest] is None:
        return ""
    return f'<figure><img src="../{image_cache[digest]}" alt=""/></figure>'


def _image_pixmap(data, page, bbox):
    """Decode gambar tertanam menjadi Pixmap, dengan fallback bila stream-nya tak terbaca."""
    try:
        return pymupdf.Pixmap(data)
    except Exception:
        pass
    rect = pymupdf.Rect(bbox)
    # 1) cari xref gambar dengan posisi yang sama, decode langsung dari PDF (JBIG2 + globals, dll.)
    for info in page.get_image_info(xrefs=True):
        if info.get("xref") and all(abs(a - b) < 2 for a, b in zip(info["bbox"], rect)):
            try:
                return pymupdf.Pixmap(page.parent, info["xref"])
            except Exception:
                break
    # 2) terakhir: render HANYA area gambar tersebut (bukan halaman penuh)
    clip = rect & page.rect
    if clip.is_empty:
        return None
    dpi = min(300, max(72, 72 * IMAGE_MAX_W / clip.width))
    return page.get_pixmap(clip=clip, matrix=pymupdf.Matrix(dpi / 72, dpi / 72), alpha=False)


def _samples_gray(pix):
    """True bila pixmap RGB (tanpa alpha) praktis tidak berwarna. Dicek dari sampel piksel."""
    if pix.n < 3:
        return True
    n = pix.n
    step = max(1, (pix.width * pix.height) // 40000) * n
    s = pix.samples
    r, g, b = s[0::step], s[1::step], s[2::step]
    colored = sum(1 for x, y, z in zip(r, g, b) if max(x, y, z) - min(x, y, z) > 40)
    return colored <= len(r) // 2000  # toleransi 0.05% piksel


def _flatten_white(pix):
    """Gabungkan pixmap ber-alpha ke latar putih (hasil tanpa alpha, bisa jadi JPEG)."""
    n, s = pix.n, pix.samples
    alpha = s[n - 1::n]
    out = bytearray(pix.width * pix.height * (n - 1))
    for k in range(n - 1):
        # sampel MuPDF premultiplied: warna di atas putih = c + (255 - a)
        out[k::n - 1] = bytes(min(255, c + 255 - a) for c, a in zip(s[k::n], alpha))
    return pymupdf.Pixmap(pix.colorspace, pix.width, pix.height, bytes(out), False)


def _fit(pix, max_w, max_h):
    scale = min(1.0, max_w / pix.width, max_h / pix.height)
    if scale >= 1.0:
        return pix
    return pymupdf.Pixmap(pix, max(1, int(pix.width * scale)), max(1, int(pix.height * scale)), None)


def _encode_jpeg(pix):
    """JPEG kualitas 80 -> 75 -> 70; bila masih > target, perkecil 15% bertahap."""
    while True:
        for q in IMAGE_JPEG_QUALITIES:
            jpg = pix.tobytes("jpeg", jpg_quality=q)
            if len(jpg) <= IMAGE_BUDGET:
                return jpg
        if pix.width * 0.85 < IMAGE_MIN_W:
            return jpg  # sudah sekecil batas bawah, pakai hasil terakhir
        pix = pymupdf.Pixmap(pix, int(pix.width * 0.85), max(1, int(pix.height * 0.85)), None)


def _compress_image(data, ext, page, bbox):
    """Kompres gambar tertanam untuk EPUB. Return (bytes, ext) atau (None, None)."""
    ext = "jpeg" if ext.lower() in ("jpg", "jpeg") else ext.lower()
    try:
        pix = _image_pixmap(data, page, bbox)
        if pix is None:
            return None, None
        converted = pix.colorspace is None or pix.colorspace.n > 3
        if converted:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)  # CMYK/tanpa colorspace -> RGB

        fits = pix.width <= IMAGE_MAX_W and pix.height <= IMAGE_MAX_H
        if fits and len(data) <= IMAGE_BUDGET and not pix.alpha and not converted:
            if ext in ("jpeg", "png", "gif"):
                return data, ext  # sudah hemat dan berformat EPUB: simpan apa adanya

        if pix.alpha:
            if max(pix.width, pix.height) <= SMALL_ALPHA_PX:
                png = pix.tobytes("png")
                if len(png) <= IMAGE_BUDGET:
                    return png, "png"  # ikon/logo transparan kecil
            pix = _flatten_white(_fit(pix, IMAGE_MAX_W, IMAGE_MAX_H))

        if pix.n >= 3 and _samples_gray(pix):
            pix = pymupdf.Pixmap(pymupdf.csGRAY, pix)
        return _encode_jpeg(_fit(pix, IMAGE_MAX_W, IMAGE_MAX_H)), "jpeg"
    except Exception:
        return None, None


def _convert_text(doc, book, language, report, tidy=None, ocr=None):
    css_text = TEXT_CSS
    if tidy:
        font_href = None
        if tidy.font == "custom":
            font_href = "font" + font_ext(tidy.font_file)
            with open(tidy.font_file, "rb") as f:
                book.add_item(epub.EpubItem(uid="font", file_name=f"style/{font_href}",
                                            media_type=FONT_TYPES[font_ext(tidy.font_file)],
                                            content=f.read()))
        css_text += build_css(tidy, font_href=font_href)
    css = epub.EpubItem(uid="style", file_name="style/main.css",
                        media_type="text/css", content=css_text)
    book.add_item(css)

    running, body_size, ocr_blocks = _analyze(doc, report, ocr)
    image_cache = {}
    chapters = []
    n = doc.page_count
    done = 0

    for idx, (title, pages) in enumerate(_chapter_ranges(doc), start=1):
        parts = []
        for p in pages:
            parts.append(_page_html(doc[p], book, image_cache, running, body_size,
                                    ocr_blocks.get(p)))
            done += 1
            report(40 + int(55 * done / n), f"Menyusun halaman {done}/{n}")
        body = "\n".join(parts).strip()
        if tidy:
            body = tidy_fragment(body, tidy)
            if tidy.fix_headings:
                title = fix_heading_text(title)
        if not body:
            body = f"<h1>{html.escape(title)}</h1>"
        chapter = epub.EpubHtml(title=title, file_name=f"text/chap_{idx:04d}.xhtml",
                                lang=language)
        chapter.content = f"<html><body>{body}</body></html>"
        chapter.add_link(href="../style/main.css", rel="stylesheet", type="text/css")
        book.add_item(chapter)
        chapters.append(chapter)

    _add_cover(book, doc, create_page=True)
    book.toc = chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["cover", "nav", *chapters]


# ---------------------------------------------------------------- mode gambar

PAGE_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}">
<head>
<title>Halaman {num}</title>
<meta name="viewport" content="width={w}, height={h}"/>
<style>html, body {{ margin: 0; padding: 0; }} img {{ display: block; width: {w}px; height: {h}px; }}</style>
</head>
<body><img src="../images/page_{num:04d}.jpg" alt="Halaman {num}"/></body>
</html>
"""


def _is_grayscale(page):
    """True bila halaman (praktis) tidak berwarna, dicek dari thumbnail kecil."""
    return _samples_gray(page.get_pixmap(dpi=20, colorspace=pymupdf.csRGB, alpha=False))


def _scan_jpeg(page, preset):
    """Bila halaman hanyalah satu gambar scan JPEG, kembalikan (bytes, w, h) aslinya."""
    images = page.get_images(full=True)
    if len(images) != 1 or page.rotation:
        return None
    if any(t["type"] != 3 for t in page.get_texttrace()):
        return None  # ada teks yang terlihat (teks OCR tak terlihat boleh)
    xref = images[0][0]
    rects = page.get_image_rects(xref, transform=True)
    if len(rects) != 1:
        return None
    rect, matrix = rects[0]
    if abs(matrix.b) > 1e-3 or abs(matrix.c) > 1e-3 or matrix.a <= 0 or matrix.d <= 0:
        return None  # gambar diputar/dibalik
    if rect.width * rect.height < 0.95 * page.rect.width * page.rect.height:
        return None
    info = page.parent.extract_image(xref)
    if (info.get("ext") not in ("jpeg", "jpg") or info.get("smask")
            or info.get("colorspace") not in (1, 3)
            or max(info["width"], info["height"]) > preset["max_px"]):
        return None
    return info["image"], info["width"], info["height"]


def _page_image(page, preset, dpi=None):
    """Gambar JPEG satu halaman: (bytes, lebar, tinggi)."""
    if not dpi:
        scan = _scan_jpeg(page, preset)
        if scan:
            return scan
        longest_inch = max(page.rect.width, page.rect.height) / 72
        dpi = min(preset["dpi"], preset["max_px"] / longest_inch)
    cs = pymupdf.csGRAY if _is_grayscale(page) else pymupdf.csRGB
    pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), colorspace=cs, alpha=False)
    return pix.tobytes("jpeg", jpg_quality=preset["jpeg"]), pix.width, pix.height


def _convert_image(doc, book, language, preset, report, dpi=None):
    book.add_metadata(None, "meta", "pre-paginated", {"property": "rendition:layout"})
    book.add_metadata(None, "meta", "auto", {"property": "rendition:spread"})

    pages = []
    n = doc.page_count
    for i, page in enumerate(doc, start=1):
        report(int(95 * (i - 1) / n), f"Merender halaman {i}/{n}")
        data, width, height = _page_image(page, preset, dpi)
        book.add_item(epub.EpubImage(uid=f"page_img_{i:04d}",
                                     file_name=f"images/page_{i:04d}.jpg",
                                     media_type="image/jpeg",
                                     content=data))
        item = epub.EpubItem(uid=f"page_{i:04d}", file_name=f"text/page_{i:04d}.xhtml",
                             media_type="application/xhtml+xml",
                             content=PAGE_XHTML.format(lang=language, num=i,
                                                       w=width, h=height))
        book.add_item(item)
        pages.append(item)

    starts = _chapter_starts(doc) or [("Mulai", 0)]
    book.toc = [epub.Link(pages[p].file_name, title, f"toc_{n}")
                for n, (title, p) in enumerate(starts)]

    _add_cover(book, doc, create_page=False)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = pages


# ---------------------------------------------------------------- API utama

def convert(pdf_path, epub_path, mode="text", title=None, author=None,
            dpi=None, language="id", progress=None, tidy=None, quality=DEFAULT_QUALITY,
            ocr=True, ocr_language=None):
    """Konversi `pdf_path` menjadi `epub_path`. Mengembalikan judul buku.

    `progress(persen, pesan)` (opsional) dipanggil selama proses berjalan.
    `tidy` (opsional, `tidy.TidyOptions`) merapikan font & penulisan (mode teks saja).
    `quality` ("kecil"|"sedang"|"tinggi") dan `dpi` (override) hanya untuk mode gambar.
    `ocr` (mode teks): OCR halaman scan tanpa lapisan teks bila Tesseract tersedia;
    `ocr_language` mis. "ind+eng" (default: otomatis).
    """
    if mode not in ("text", "image"):
        raise ValueError("Mode harus 'text' atau 'image'.")
    if quality not in QUALITY_PRESETS:
        raise ValueError("Kualitas harus 'kecil', 'sedang', atau 'tinggi'.")
    report = progress or (lambda percent, message: None)
    report(0, "Membuka PDF")
    doc = _open_pdf(pdf_path)
    try:
        title, author = _metadata(doc, pdf_path, title, author)
        book = _new_book(title, author, language)
        if mode == "text":
            ocr_config = None
            tessdata = find_tessdata() if ocr else None
            if tessdata:
                ocr_config = {"language": ocr_language or default_language(tessdata),
                              "tessdata": tessdata, "pdf_path": pdf_path}
            _convert_text(doc, book, language, report, tidy, ocr_config)
        else:
            _convert_image(doc, book, language, QUALITY_PRESETS[quality], report,
                           int(dpi) if dpi else None)
        report(96, "Menyimpan file EPUB")
        epub.write_epub(epub_path, book)
        repack_epub(epub_path)  # ebooklib tidak mengompresi isi ZIP
    finally:
        doc.close()
    report(100, "Selesai")
    return title


def _print_progress(percent, message):
    bar = "#" * (percent // 4)
    print(f"\r[{bar:<25}] {percent:3d}% {message:<40}", end="", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Konversi PDF ke EPUB.")
    parser.add_argument("pdf", help="file PDF masukan")
    parser.add_argument("-o", "--output", help="file EPUB keluaran (default: nama PDF .epub)")
    parser.add_argument("-m", "--mode", choices=["text", "image"], default="text",
                        help="text = reflowable, image = tiap halaman jadi gambar")
    parser.add_argument("-t", "--title", help="judul buku")
    parser.add_argument("-a", "--author", help="nama penulis")
    parser.add_argument("-q", "--quality", choices=list(QUALITY_PRESETS), default=DEFAULT_QUALITY,
                        help="kualitas/ukuran mode image (default sedang)")
    parser.add_argument("--dpi", type=int, help="paksa resolusi mode image (mengabaikan --quality)")
    parser.add_argument("--lang", default="id", help="kode bahasa (default id)")
    parser.add_argument("--no-ocr", action="store_true", help="jangan OCR halaman hasil scan")
    parser.add_argument("--ocr-lang", help="bahasa OCR Tesseract, mis. ind+eng (default otomatis)")
    add_tidy_args(parser)
    args = parser.parse_args()

    output = args.output or os.path.splitext(args.pdf)[0] + ".epub"
    convert(args.pdf, output, args.mode, args.title, args.author, args.dpi, args.lang,
            progress=_print_progress, tidy=options_from_args(args), quality=args.quality,
            ocr=not args.no_ocr, ocr_language=args.ocr_lang)
    print(f"\nSelesai: {output}")


if __name__ == "__main__":
    main()
