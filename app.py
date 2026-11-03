"""Web app lokal untuk konversi PDF ke EPUB dan merapikan EPUB. Jalankan: python app.py

Alur:
  POST /convert            -> upload PDF, konversi jalan di background, balas {job_id}
  POST /tidy               -> upload EPUB, dirapikan di background, balas {job_id}
  GET  /progress/<job_id>  -> {percent, message, done, error}
  GET  /download/<job_id>  -> unduh EPUB (lalu file sementara dihapus)
"""

import io
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
import webbrowser

from flask import Flask, jsonify, render_template, request, send_file

from converter import DEFAULT_QUALITY, QUALITY_PRESETS, convert
from ocr import ocr_available
from tidy import FONT_TYPES, TidyOptions, font_ext, tidy_epub

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

JOB_TTL = 60 * 60  # job yang tidak diunduh dihapus setelah 1 jam
jobs = {}
jobs_lock = threading.Lock()


def _safe_name(name):
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "buku"
    return name[:150]


def _cleanup_old_jobs():
    now = time.time()
    with jobs_lock:
        expired = [jid for jid, j in jobs.items() if now - j["created"] > JOB_TTL]
        for jid in expired:
            shutil.rmtree(jobs.pop(jid)["dir"], ignore_errors=True)


def _update(job_id, **fields):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)


def _format_size(path):
    size = os.path.getsize(path)
    return f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{size / 1024:.0f} KB"


def _run_job(job_id, epub_path, work):
    def progress(percent, message):
        _update(job_id, percent=percent, message=message)

    try:
        note = work(progress)
        message = f"Selesai — {_format_size(epub_path)}" + (f" ({note})" if note else "")
        _update(job_id, done=True, percent=100, message=message)
    except ValueError as e:
        _update(job_id, done=True, error=str(e))
    except Exception as e:  # file rusak, dll.
        app.logger.exception("Proses gagal")
        _update(job_id, done=True, error=f"Proses gagal: {e}")


def _start_job(job_dir, epub_path, download_name, work):
    """Jalankan `work(progress)` di background; hasilnya file `epub_path`."""
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "dir": job_dir,
            "epub": epub_path,
            "download_name": download_name,
            "created": time.time(),
            "percent": 0,
            "message": "Menunggu",
            "done": False,
            "error": None,
        }
    threading.Thread(target=_run_job, args=(job_id, epub_path, work), daemon=True).start()
    return jsonify(job_id=job_id)


def _receive_upload(ext):
    """Validasi upload `file` dan simpan ke folder job baru. Return (job_dir, path, nama)."""
    file = request.files.get("file")
    if not file or not file.filename:
        raise ValueError(f"Pilih file {ext.upper().lstrip('.')} terlebih dahulu.")
    if not file.filename.lower().endswith(ext):
        raise ValueError(f"File harus berformat {ext}.")
    job_dir = tempfile.mkdtemp(prefix="pdf2epub_")
    path = os.path.join(job_dir, _safe_name(os.path.basename(file.filename)))
    file.save(path)
    return job_dir, path, _safe_name(os.path.splitext(file.filename)[0])


def _tidy_options_from_form(job_dir):
    form = request.form

    def number(name, default, cast):
        try:
            return cast(form.get(name, default))
        except ValueError:
            return default

    font = form.get("font", "default")
    font_file = None
    if font == "custom":
        f = request.files.get("font_file")
        if not f or not f.filename:
            raise ValueError("Pilih file font untuk font kustom.")
        ext = font_ext(f.filename)
        if ext not in FONT_TYPES:
            raise ValueError("Font harus berformat .ttf, .otf, .woff, atau .woff2.")
        font_file = os.path.join(job_dir, "font" + ext)
        f.save(font_file)

    return TidyOptions(
        font=font,
        font_file=font_file,
        font_size=number("font_size", 100, int),
        line_height=number("line_height", 1.5, float),
        align=form.get("align", "justify"),
        indent=bool(form.get("indent")),
        clean_text=bool(form.get("clean_text")),
        merge_paragraphs=bool(form.get("merge_paragraphs")),
        fix_headings=bool(form.get("fix_headings")),
    )


@app.get("/")
def index():
    return render_template("index.html", ocr_available=ocr_available())


@app.post("/convert")
def convert_route():
    _cleanup_old_jobs()
    job_dir = None
    try:
        job_dir, pdf_path, base = _receive_upload(".pdf")
        mode = request.form.get("mode", "text")
        tidy = _tidy_options_from_form(job_dir) if mode == "text" else None
    except ValueError as e:
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error=str(e)), 400

    quality = request.form.get("quality", DEFAULT_QUALITY)
    if quality not in QUALITY_PRESETS:
        quality = DEFAULT_QUALITY
    epub_path = os.path.join(job_dir, "output.epub")
    options = {
        "mode": mode,
        "title": request.form.get("title", "").strip() or None,
        "author": request.form.get("author", "").strip() or None,
        "quality": quality,
        "tidy": tidy,
        "ocr": bool(request.form.get("ocr")),
    }

    def work(progress):
        convert(pdf_path, epub_path, progress=progress, **options)

    return _start_job(job_dir, epub_path, base + ".epub", work)


@app.post("/tidy")
def tidy_route():
    _cleanup_old_jobs()
    job_dir = None
    try:
        job_dir, in_path, base = _receive_upload(".epub")
        opts = _tidy_options_from_form(job_dir)
    except ValueError as e:
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error=str(e)), 400

    out_path = os.path.join(job_dir, "output.epub")

    def work(progress):
        result = tidy_epub(in_path, out_path, opts, progress=progress)
        if result["skipped"]:
            return f"{result['skipped']} bagian tidak bisa dibaca, dibiarkan apa adanya"
        return None

    return _start_job(job_dir, out_path, base + "-rapi.epub", work)


@app.get("/progress/<job_id>")
def progress_route(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(error="Proses tidak ditemukan."), 404
        return jsonify({k: job[k] for k in ("percent", "message", "done", "error")})


@app.get("/download/<job_id>")
def download_route(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or not job["done"] or job["error"]:
            return jsonify(error="File belum siap atau tidak ditemukan."), 404
        jobs.pop(job_id)
    with open(job["epub"], "rb") as f:
        data = io.BytesIO(f.read())
    shutil.rmtree(job["dir"], ignore_errors=True)
    return send_file(data, mimetype="application/epub+zip",
                     as_attachment=True, download_name=job["download_name"])


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="File terlalu besar (maksimal 200 MB)."), 413


def _port_in_use(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def main():
    host = "127.0.0.1"
    port = int(os.environ.get("PDF2EPUB_PORT", 5000))
    url = f"http://{host}:{port}"
    open_browser = "--open" in sys.argv[1:]

    if _port_in_use(host, port):
        print(f"Server sudah berjalan di {url}")
        if open_browser:
            webbrowser.open(url)
        return

    print(f"Buka {url} di browser. Tekan Ctrl+C (atau tutup jendela ini) untuk mematikan server.")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
