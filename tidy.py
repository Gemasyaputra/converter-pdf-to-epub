"""Merapikan font dan penulisan EPUB.

Dipakai dua cara:
- oleh converter.py saat konversi PDF (fungsi `tidy_fragment`, `build_css`)
- untuk merapikan EPUB yang sudah ada (fungsi `tidy_epub`), juga lewat CLI:
    python tidy.py buku.epub -o buku-rapi.epub --font serif --size 110
"""

import argparse
import html.entities
import io
import os
import posixpath
import re
import zipfile
from dataclasses import dataclass
from urllib.parse import unquote

from lxml import etree

from epubzip import write_epub_zip

XHTML_NS = "http://www.w3.org/1999/xhtml"
OPF_NS = "http://www.idpf.org/2007/opf"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

FONT_STACKS = {
    "serif": 'Georgia, "Times New Roman", serif',
    "sans": '"Segoe UI", Helvetica, Arial, sans-serif',
    "mono": 'Consolas, "Courier New", monospace',
}
FONT_CHOICES = ("default", "serif", "sans", "mono", "custom")
FONT_TYPES = {".ttf": "font/ttf", ".otf": "font/otf",
              ".woff": "font/woff", ".woff2": "font/woff2"}
CUSTOM_FONT_FAMILY = "TidyCustomFont"

# Enkripsi yang hanya menyamarkan font (bukan DRM), aman untuk diproses.
FONT_OBFUSCATION = {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}


@dataclass
class TidyOptions:
    font: str = "default"
    font_file: str | None = None
    font_size: int = 100  # persen
    line_height: float = 1.5
    align: str = "justify"
    indent: bool = False
    clean_text: bool = True
    merge_paragraphs: bool = True
    fix_headings: bool = True

    def __post_init__(self):
        if self.font not in FONT_CHOICES:
            self.font = "default"
        if self.font == "custom":
            if not self.font_file:
                raise ValueError("Pilih file font untuk font kustom.")
            if font_ext(self.font_file) not in FONT_TYPES:
                raise ValueError("Font harus berformat .ttf, .otf, .woff, atau .woff2.")
        self.font_size = min(max(int(self.font_size), 80), 150)
        self.line_height = round(min(max(float(self.line_height), 1.2), 2.0), 2)
        if self.align not in ("justify", "left"):
            self.align = "justify"


def font_ext(path):
    return os.path.splitext(path)[1].lower()


# ---------------------------------------------------------------- CSS

def build_css(opts, important=False, font_href=None):
    """CSS tampilan sesuai opsi. `important=True` untuk menimpa CSS penerbit."""
    imp = " !important" if important else ""
    rules = []
    family = FONT_STACKS.get(opts.font)
    if opts.font == "custom":
        rules.append(f'@font-face {{ font-family: "{CUSTOM_FONT_FAMILY}"; '
                     f'src: url("{font_href}"); }}')
        family = f'"{CUSTOM_FONT_FAMILY}", serif'

    body = [f"font-size: {opts.font_size}%{imp}", f"line-height: {opts.line_height}{imp}"]
    if family:
        body.append(f"font-family: {family}{imp}")
    rules.append("body { " + "; ".join(body) + "; }")

    text = [f"text-align: {opts.align}{imp}", f"line-height: {opts.line_height}{imp}"]
    if family:
        text.append(f"font-family: {family}{imp}")
    rules.append("p, li, blockquote, dd { " + "; ".join(text) + "; }")

    if opts.indent:
        rules.append(f"p {{ text-indent: 1.5em{imp}; margin-top: 0{imp}; margin-bottom: 0{imp}; }}")
        rules.append(f"h1 + p, h2 + p, h3 + p, h4 + p, figure + p {{ text-indent: 0{imp}; }}")
    else:
        rules.append(f"p {{ text-indent: 0{imp}; margin-top: 0{imp}; margin-bottom: 0.8em{imp}; }}")
    return "\n".join(rules) + "\n"


# ---------------------------------------------------------------- teks

LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
             "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
INVISIBLE_RE = re.compile("[­​‌‍⁠﻿]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
OPEN_QUOTE_CTX = r"(?<![^\s(\[{—–])"  # awal teks atau setelah spasi/kurung/dash


def clean_text(s):
    if not s:
        return s
    for lig, rep in LIGATURES.items():
        s = s.replace(lig, rep)
    s = INVISIBLE_RE.sub("", s)
    s = CONTROL_RE.sub("", s)
    s = re.sub(r"[ \t\r\n]{2,}", " ", s)
    s = re.sub(r"[ \t]+([,.;:!?])", r"\1", s)  # spasi sebelum tanda baca
    s = re.sub(r"(?<=[a-z]{2})([.,;:!?])(?=[A-Z])", r"\1 ", s)  # "kata.Kata" -> "kata. Kata"
    s = re.sub(r"(?<=[A-Za-z])([,;])(?=[A-Za-z])", r"\1 ", s)  # "kata,kata" (angka 1,5 aman)
    s = re.sub(r"\.{3,}", "…", s)
    s = s.replace(" -- ", " – ")
    s = re.sub(OPEN_QUOTE_CTX + r'"(?=\S)', "“", s)
    s = s.replace('"', "”")
    s = re.sub(OPEN_QUOTE_CTX + r"'(?=\w)", "‘", s)
    s = s.replace("'", "’")
    return s


SMALL_WORDS = {
    "dan", "di", "ke", "dari", "yang", "untuk", "atau", "dengan", "pada", "dalam",
    "oleh", "bagi", "serta", "terhadap", "sebagai", "akan", "tentang",
    "a", "an", "the", "of", "and", "or", "in", "on", "at", "to", "for", "by", "with",
}
ROMAN_RE = re.compile(r"^(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")


def _is_all_caps(text):
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 4 and all(c.isupper() for c in letters)


def _capitalize(part):
    for i, c in enumerate(part):
        if c.isalpha():
            return part[:i] + c.upper() + part[i + 1:].lower()
    return part


def _title_word(word, first):
    core = re.sub(r"^\W+|\W+$", "", word)
    if not first and core.lower() in SMALL_WORDS:
        return word.lower()
    if core and ROMAN_RE.match(core):
        return word  # BAB II, IV, dst.
    if core.isalpha() and not re.search(r"[AIUEOaiueo]", core):
        return word  # singkatan tanpa huruf vokal: PT, CV, TBK, PDF
    return "-".join(_capitalize(p) for p in word.split("-"))


def _title_case(text, first=True):
    out = []
    for part in re.split(r"(\s+)", text):
        if part and not part.isspace():
            part = _title_word(part, first)
            first = False
        out.append(part)
    return "".join(out), first


def fix_heading_text(text):
    """Versi teks biasa (misalnya untuk judul bab di daftar isi)."""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?<![.…])\.$", "", text)
    if _is_all_caps(text):
        text, _ = _title_case(text)
    return text


# ---------------------------------------------------------------- pohon HTML

SKIP_TAGS = {"pre", "code", "script", "style", "svg", "math"}
SENTENCE_END = set(".!?:;…\"”’)»]")


def _local(el):
    return etree.QName(el).localname if isinstance(el.tag, str) else None


def _text_slots(el):
    """Semua tempat teks di dalam elemen, urut dokumen: (objek, 'text'|'tail')."""
    yield el, "text"
    for child in el:
        if isinstance(child.tag, str):
            yield from _text_slots(child)
        yield child, "tail"


def _walk_text(el, fn):
    if isinstance(el.tag, str) and _local(el) not in SKIP_TAGS:
        el.text = fn(el.text)
        for child in el:
            _walk_text(child, fn)
            child.tail = fn(child.tail)


def _fix_heading_el(el):
    slots = [s for s in _text_slots(el) if getattr(*s)]
    if not slots:
        return
    caps = _is_all_caps("".join(getattr(*s) for s in slots))
    first = True
    for obj, attr in slots:
        t = re.sub(r"\s+", " ", getattr(obj, attr))
        if caps:
            t, first = _title_case(t, first)
        setattr(obj, attr, t)
    obj, attr = slots[0]
    setattr(obj, attr, getattr(obj, attr).lstrip())
    obj, attr = slots[-1]
    setattr(obj, attr, re.sub(r"(?<![.…])\.$", "", getattr(obj, attr).rstrip()))


def _last_text_slot(el):
    for obj, attr in reversed(list(_text_slots(el))):
        if (getattr(obj, attr) or "").strip():
            return obj, attr
    return None, None


def _append_text(el, text):
    if len(el):
        el[-1].tail = (el[-1].tail or "") + text
    else:
        el.text = (el.text or "") + text


def _should_merge(a, b):
    ta = "".join(a.itertext()).rstrip()
    tb = "".join(b.itertext()).lstrip()
    return bool(ta and tb) and ta[-1] not in SENTENCE_END and tb[0].islower()


def _merge_into(a, b):
    obj, attr = _last_text_slot(a)
    sep = " "
    if obj is not None:
        t = getattr(obj, attr).rstrip()
        if t.endswith("-") and len(t) > 1 and t[-2].isalpha():
            t, sep = t[:-1], ""  # kata terpenggal: "pemba-" + "gian"
        setattr(obj, attr, t)
    _append_text(a, sep + (b.text or "").lstrip())
    for child in list(b):
        a.append(child)
    a.tail = b.tail
    b.getparent().remove(b)


def _merge_paragraphs_in(parent):
    prev = None
    for el in list(parent):
        if _local(el) != "p":
            prev = None
            continue
        if prev is not None and not (prev.tail or "").strip() and _should_merge(prev, el):
            _merge_into(prev, el)
            continue
        prev = el


def tidy_html_tree(root, opts):
    """Rapikan isi <body> (atau root bila tidak ada body) secara langsung."""
    body = next((el for el in root.iter() if _local(el) == "body"), root)
    if opts.clean_text:
        _walk_text(body, clean_text)
    if opts.merge_paragraphs:
        for parent in list(body.iter()):
            if isinstance(parent.tag, str) and _local(parent) not in SKIP_TAGS:
                _merge_paragraphs_in(parent)
    if opts.fix_headings:
        for el in body.iter():
            if _local(el) in ("h1", "h2", "h3", "h4", "h5", "h6"):
                _fix_heading_el(el)


def tidy_fragment(fragment, opts):
    """Rapikan potongan HTML (isi body) dan kembalikan sebagai string."""
    root = etree.fromstring(f"<body>{fragment}</body>")
    tidy_html_tree(root, opts)
    return (root.text or "") + "".join(etree.tostring(c, encoding="unicode") for c in root)


# ---------------------------------------------------------------- EPUB

def _named_entities_to_numeric(text):
    def repl(m):
        name = m.group(1)
        if name in ("amp", "lt", "gt", "quot", "apos"):
            return m.group(0)
        chars = html.entities.html5.get(name + ";")
        return "".join(f"&#{ord(c)};" for c in chars) if chars else m.group(0)
    return re.sub(r"&([A-Za-z][A-Za-z0-9]*);", repl, text)


def _parse_xhtml(data):
    parser = etree.XMLParser(no_network=True, huge_tree=True)
    try:
        return etree.parse(io.BytesIO(data), parser)
    except etree.XMLSyntaxError:
        # entitas HTML seperti &nbsp; tidak dikenal parser XML -> ubah ke angka
        fixed = _named_entities_to_numeric(data.decode("utf-8", "replace"))
        fixed = re.sub(r"^<\?xml[^>]*\?>", "", fixed.lstrip())
        return etree.parse(io.BytesIO(fixed.encode("utf-8")), parser)


def _resolve(base_dir, href):
    return posixpath.normpath(posixpath.join(base_dir, unquote(href.split("#")[0])))


def _unique_id(existing, wanted):
    uid, n = wanted, 2
    while uid in existing:
        uid, n = f"{wanted}-{n}", n + 1
    existing.add(uid)
    return uid


def _check_encryption(zin):
    try:
        enc = etree.fromstring(zin.read("META-INF/encryption.xml"))
    except KeyError:
        return
    algorithms = {el.get("Algorithm") for el in enc.iter()
                  if _local(el) == "EncryptionMethod"}
    if algorithms - FONT_OBFUSCATION:
        raise ValueError("EPUB ini dilindungi DRM, tidak bisa dirapikan.")


def tidy_epub(in_path, out_path, opts, progress=None):
    """Rapikan EPUB `in_path` -> `out_path`. Mengembalikan dict {total, skipped}."""
    report = progress or (lambda percent, message: None)
    report(0, "Membuka EPUB")
    try:
        zin = zipfile.ZipFile(in_path)
    except zipfile.BadZipFile:
        raise ValueError("File bukan EPUB yang valid atau rusak.") from None

    with zin:
        _check_encryption(zin)
        try:
            container = etree.fromstring(zin.read("META-INF/container.xml"))
            opf_path = container.find(f".//{{{CONTAINER_NS}}}rootfile").get("full-path")
            opf_tree = etree.parse(io.BytesIO(zin.read(opf_path)))
        except (KeyError, AttributeError, etree.XMLSyntaxError):
            raise ValueError("Struktur EPUB tidak valid (container/OPF tidak ditemukan).") from None

        opf = opf_tree.getroot()
        for meta in opf.iter(f"{{{OPF_NS}}}meta"):
            if meta.get("property") == "rendition:layout" and (meta.text or "").strip() == "pre-paginated":
                raise ValueError("EPUB ini fixed-layout (berisi gambar halaman), "
                                 "teks dan font-nya tidak bisa dirapikan.")

        opf_dir = posixpath.dirname(opf_path)
        tidy_dir = posixpath.join(opf_dir, "tidy")
        css_path = posixpath.join(tidy_dir, "tidy.css")
        manifest = opf.find(f"{{{OPF_NS}}}manifest")

        # buang hasil perapian sebelumnya (jika EPUB pernah dirapikan)
        for item in list(manifest):
            if _resolve(opf_dir, item.get("href", "")).startswith(tidy_dir + "/"):
                manifest.remove(item)
        ids = {item.get("id") for item in manifest}

        docs = [_resolve(opf_dir, item.get("href", "")) for item in manifest
                if item.get("media-type") == "application/xhtml+xml"]
        docs = [d for d in docs if d in zin.namelist()]

        new_files = {}
        skipped = 0
        for i, name in enumerate(docs, start=1):
            report(5 + int(85 * i / max(len(docs), 1)), f"Merapikan bagian {i}/{len(docs)}")
            try:
                tree = _parse_xhtml(zin.read(name))
            except etree.XMLSyntaxError:
                skipped += 1
                continue
            root = tree.getroot()
            tidy_html_tree(root, opts)

            head = next((el for el in root.iter() if _local(el) == "head"), None)
            if head is not None:
                doc_dir = posixpath.dirname(name)
                for link in list(head):
                    if (_local(link) == "link"
                            and _resolve(doc_dir, link.get("href", "")).startswith(tidy_dir + "/")):
                        head.remove(link)
                ns = etree.QName(root).namespace
                link = etree.SubElement(head, f"{{{ns}}}link" if ns else "link")
                link.set("rel", "stylesheet")
                link.set("type", "text/css")
                link.set("href", posixpath.relpath(css_path, doc_dir or "."))
            new_files[name] = etree.tostring(tree, xml_declaration=True, encoding="utf-8")

        report(92, "Menyimpan EPUB")
        font_href = None
        if opts.font == "custom":
            font_href = "font" + font_ext(opts.font_file)
            with open(opts.font_file, "rb") as f:
                new_files[posixpath.join(tidy_dir, font_href)] = f.read()
            etree.SubElement(manifest, f"{{{OPF_NS}}}item", {
                "id": _unique_id(ids, "tidy-font"), "href": "tidy/" + font_href,
                "media-type": FONT_TYPES[font_ext(opts.font_file)]})
        new_files[css_path] = build_css(opts, important=True, font_href=font_href).encode("utf-8")
        etree.SubElement(manifest, f"{{{OPF_NS}}}item", {
            "id": _unique_id(ids, "tidy-css"), "href": "tidy/tidy.css",
            "media-type": "text/css"})
        new_files[opf_path] = etree.tostring(opf_tree, xml_declaration=True, encoding="utf-8")

        def entries():
            for info in zin.infolist():
                name = info.filename
                if name in new_files or name.startswith(tidy_dir + "/"):
                    continue
                yield name, zin.read(info)
            yield from new_files.items()

        write_epub_zip(out_path, entries())

    return {"total": len(docs), "skipped": skipped}


# ---------------------------------------------------------------- CLI

def add_tidy_args(parser):
    g = parser.add_argument_group("perapian font & penulisan")
    g.add_argument("--font", choices=FONT_CHOICES[:-1], default="default",
                   help="jenis font (default: bawaan pembaca)")
    g.add_argument("--font-file", help="sematkan font sendiri (.ttf/.otf/.woff/.woff2)")
    g.add_argument("--size", type=int, default=100, help="ukuran font dalam persen (80-150)")
    g.add_argument("--line-height", type=float, default=1.5, help="jarak baris (1.2-2.0)")
    g.add_argument("--align", choices=["justify", "left"], default="justify")
    g.add_argument("--indent", action="store_true", help="indentasi awal paragraf")
    g.add_argument("--no-clean", action="store_true", help="jangan bersihkan teks")
    g.add_argument("--no-merge", action="store_true", help="jangan gabung paragraf terpotong")
    g.add_argument("--no-headings", action="store_true", help="jangan rapikan judul")


def options_from_args(args):
    return TidyOptions(
        font="custom" if args.font_file else args.font,
        font_file=args.font_file,
        font_size=args.size,
        line_height=args.line_height,
        align=args.align,
        indent=args.indent,
        clean_text=not args.no_clean,
        merge_paragraphs=not args.no_merge,
        fix_headings=not args.no_headings,
    )


def _print_progress(percent, message):
    bar = "#" * (percent // 4)
    print(f"\r[{bar:<25}] {percent:3d}% {message:<40}", end="", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Rapikan font & penulisan file EPUB.")
    parser.add_argument("epub", help="file EPUB masukan")
    parser.add_argument("-o", "--output", help="file keluaran (default: <nama>-rapi.epub)")
    add_tidy_args(parser)
    args = parser.parse_args()

    output = args.output or os.path.splitext(args.epub)[0] + "-rapi.epub"
    result = tidy_epub(args.epub, output, options_from_args(args), progress=_print_progress)
    _print_progress(100, "Selesai")
    note = f" ({result['skipped']} bagian dilewati)" if result["skipped"] else ""
    print(f"\nSelesai: {output}{note}")


if __name__ == "__main__":
    main()
