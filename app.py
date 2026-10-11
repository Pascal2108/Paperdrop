import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

import img2pdf
import pikepdf
import requests
from flask import Flask, abort, redirect, render_template, request, send_file
from itsdangerous import BadSignature, URLSafeTimedSerializer
from PIL import Image, ImageOps
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

# Everything goes to stdout so it shows up in `docker logs` / Portainer
logging.basicConfig(stream=sys.stdout, level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(message)s")

# Protection against decompression bombs (huge pixel count in a small file).
# Pillow only warns above its limit and refuses at twice that; here the limit is the limit.
MAX_MEGAPIXELS = 120
Image.MAX_IMAGE_PIXELS = MAX_MEGAPIXELS * 1_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)

PAPERLESS_URL = os.environ["PAPERLESS_URL"].rstrip("/")
PAPERLESS_TOKEN = os.environ["PAPERLESS_TOKEN"]
PAPERLESS_AUTH = {"Authorization": f"Token {PAPERLESS_TOKEN}"}
MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))

# Compression strength offered on the page -> Ghostscript preset
QUALITY = {"light": "printer", "balanced": "ebook", "strong": "screen"}
GS_QUALITY = os.environ.get("GS_QUALITY", "ebook")  # preselected strength, as a Ghostscript preset
DEFAULT_QUALITY = next((k for k, v in QUALITY.items() if v == GS_QUALITY), "balanced")

LOGIN_USER = os.environ["UPLOADER_USER"]
LOGIN_PASSWORD = os.environ["UPLOADER_PASSWORD"]
SESSION_MINUTES = int(os.environ.get("SESSION_MINUTES", "30"))

# The values in secrets.env.example are public: never run with them
for _name, _example in (("UPLOADER_PASSWORD", "a-long-secure-password"),
                        ("SECRET_KEY", "paste-random-key-here")):
    if os.environ[_name] == _example:
        logging.critical("%s still has the value from secrets.env.example. Set your own in "
                         "secrets.env, then start the container again.", _name)
        raise SystemExit(1)
if len(LOGIN_PASSWORD) < 12:
    logging.warning("UPLOADER_PASSWORD is shorter than 12 characters. Choose a longer one.")
if len(os.environ["SECRET_KEY"]) < 32:
    logging.warning("SECRET_KEY is short. Generate one with: openssl rand -hex 32")

app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=MAX_MB * 1024 * 1024)

# Exactly one proxy (NPM) in front: only its X-Forwarded-For entry is trusted,
# so a spoofed header sent by the client is ignored.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# No cookies. Signing in returns the page together with a random session token that
# only lives in that page's memory and is sent with every request, so it doubles as
# CSRF protection. Reloading or reopening the page loses it.
#
# The login form itself works once: a browser repeats the last form when a page is
# reloaded or a closed tab is restored, and would otherwise sign in again with the
# remembered password. The per-start salt invalidates open login forms on a restart,
# when the list of used ones is gone.
_signer = URLSafeTimedSerializer(os.environ["SECRET_KEY"], salt=f"login-{secrets.token_hex(8)}")
_used_forms: dict[str, float] = {}
_sessions: dict[str, float] = {}  # session token -> time of last activity
_lock = threading.Lock()
LOGIN_FORM_SECONDS = 12 * 3600  # how long an opened login page can still be submitted

# Simple brute-force protection: max. 5 failed attempts per IP in 15 minutes.
# There is no limit across all IPs: anyone could use it to keep the owner locked out.
_failed: dict[str, list[float]] = {}
MAX_TRIES, WINDOW = 5, 15 * 60

# Only the compress route takes files; every other request is a short form
SMALL_BODY = 64 * 1024


def client_ip() -> str:
    return request.remote_addr or "?"


@app.before_request
def limit_body():
    if request.endpoint != "compress":
        request.max_content_length = SMALL_BODY


@app.after_request
def security_headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if resp.mimetype == "text/html":
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        )
    # Static files may be kept but are revalidated; nothing else is stored by the browser
    resp.headers["Cache-Control"] = "no-cache" if request.path.startswith("/static/") else "no-store"
    return resp


def locked(ip: str) -> bool:
    now = time.time()
    _failed[ip] = [t for t in _failed.get(ip, []) if now - t < WINDOW]
    return len(_failed[ip]) >= MAX_TRIES


def _digest(s: str) -> bytes:
    # Hash first so the comparison does not leak the length either
    return hashlib.sha256(s.encode()).digest()


def check_credentials(user: str, pw: str) -> bool:
    ok_user = hmac.compare_digest(_digest(user), _digest(LOGIN_USER))
    ok_pw = hmac.compare_digest(_digest(pw), _digest(LOGIN_PASSWORD))
    return ok_user and ok_pw


def login_form_fresh(token: str) -> bool:
    """True if this login form is genuine, not too old and submitted for the first time."""
    try:
        form_id = _signer.loads(token, max_age=LOGIN_FORM_SECONDS)
    except (BadSignature, TypeError, ValueError):  # BadSignature also covers expired forms
        return False
    now = time.time()
    with _lock:
        for old in [i for i, t in _used_forms.items() if now - t > LOGIN_FORM_SECONDS]:
            del _used_forms[old]
        if form_id in _used_forms:
            return False
        _used_forms[form_id] = now
    return True


def session_valid(token: str, touch: bool) -> bool:
    """touch=False for background requests, which must not count as activity."""
    now = time.time()
    with _lock:
        for old in [s for s, t in _sessions.items() if now - t > SESSION_MINUTES * 60]:
            del _sessions[old]
        if token not in _sessions:
            return False
        if touch:
            _sessions[token] = now
    return True


def is_api() -> bool:
    return request.path.startswith("/api/")


def login_page(error: str | None = None, status: int = 200):
    return render_template("login.html", error=error, once=_signer.dumps(secrets.token_hex(8))), status


def session_token(form: bool = False) -> str:
    """The session token of this request.

    It comes in a header, so that a request is turned away before its body is read.
    Only "Open PDF" is a plain form posted to a new tab and carries it as a field.
    """
    return request.form.get("auth", "") if form else request.headers.get("X-Auth", "")


def login_required(view=None, *, touch: bool = True, form: bool = False):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session_valid(session_token(form), touch):
                if is_api():
                    return {"error": "signed out"}, 401
                return login_page("Please sign in again.", 401)
            return view(*args, **kwargs)
        return wrapper
    return decorator(view) if view else decorator


@app.get("/")
def login_form():
    return login_page()


@app.post("/login")
def login():
    ip = client_ip()
    # A repeated form (reload, restored tab) is turned away before the password is looked at
    if not login_form_fresh(request.form.get("once", "")):
        return login_page("Please sign in again.", 401)
    if locked(ip):
        error = "Too many failed attempts – please wait 15 minutes."
    elif check_credentials(request.form.get("user", ""), request.form.get("password", "")):
        _failed.pop(ip, None)
        app.logger.info("Login from %s", ip)
        token = secrets.token_urlsafe(32)
        with _lock:
            _sessions[token] = time.time()
        return render_template("index.html", boot={
            "auth": token, "items": staged_items(), "sessionMinutes": SESSION_MINUTES,
            "maxMb": MAX_MB, "quality": DEFAULT_QUALITY,
        })
    else:
        _failed.setdefault(ip, []).append(time.time())
        app.logger.warning("Failed login from %s", ip)
        time.sleep(1)
        error = "Invalid username or password."
    return login_page(error, 401)


@app.post("/api/logout")
@login_required
def logout():
    with _lock:
        _sessions.pop(session_token(), None)
    return {"ok": True}


@app.errorhandler(HTTPException)
def http_error(e):
    if is_api():
        messages = {404: "This file no longer exists.", 413: f"Too large – the limit is {MAX_MB} MB."}
        return {"error": messages.get(e.code, e.name)}, e.code
    if e.code == 405:
        # e.g. a bookmarked or manually opened action URL
        return redirect("/")
    return e


# Photos are placed on A4 pages, turned to landscape where the photo is wider than tall.
# The picture keeps its proportions and its pixels; only the page around it is fixed.
A4 = (img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297))
A4_LAYOUT = img2pdf.get_layout_fun(pagesize=A4, auto_orient=True)


def images_to_pdf(images: list[Path], out: Path) -> None:
    """JPGs and PNGs -> A4 PDF, applying EXIF rotation first."""
    fixed = []
    for img_path in images:
        with Image.open(img_path, formats=["JPEG", "PNG"]) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "PA", "P"):
                # JPEG has no transparency: put the picture on white paper
                im = im.convert("RGBA")
                page = Image.new("RGB", im.size, "white")
                page.paste(im, mask=im.getchannel("A"))
                im = page
            p = img_path.with_suffix(".fixed.jpg")
            im.convert("RGB").save(p, "JPEG", quality=90)
            fixed.append(str(p))
    out.write_bytes(img2pdf.convert(fixed, layout_fun=A4_LAYOUT))


def compress_pdf(src: Path, dst: Path, preset: str) -> Path:
    """Compresses with Ghostscript. Returns whichever file is smaller."""
    cmd = [
        "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.6",
        f"-dPDFSETTINGS=/{preset}",
        "-dNOPAUSE", "-dQUIET", "-dBATCH", "-dSAFER",
        f"-sOutputFile={dst}", str(src),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=300)
    except Exception as e:
        app.logger.warning("Ghostscript failed (%s), using original", e)
        return src
    return dst if dst.stat().st_size < src.stat().st_size else src


def page_count(pdf: Path) -> int:
    try:
        with pikepdf.open(pdf) as doc:
            return len(doc.pages)
    except Exception:
        return 0


class PaperlessError(Exception):
    """Paperless answered, but refused the document."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status, self.body = status, body


def upload_to_paperless(pdf: Path, title: str) -> str:
    filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title) + ".pdf"
    with pdf.open("rb") as f:
        r = requests.post(
            f"{PAPERLESS_URL}/api/documents/post_document/",
            headers=PAPERLESS_AUTH,
            files={"document": (filename, f, "application/pdf")},
            data={"title": title},
            timeout=120,
        )
    if not r.ok:
        raise PaperlessError(r.status_code, " ".join(r.text.split())[:300])
    return r.text.strip().strip('"')  # Task-ID


HTTP_HINTS = {
    401: "token rejected – check PAPERLESS_TOKEN",
    403: "the token's user lacks the permission to add documents",
    404: "API not found – check PAPERLESS_URL",
    413: "file too large for Paperless",
}


def failure_reason(e: Exception) -> str:
    """Short cause of a failed upload, shown on the page and written to the log."""
    if isinstance(e, PaperlessError):
        hint = HTTP_HINTS.get(e.status)
        return f"Paperless answered HTTP {e.status}" + (f" ({hint})" if hint else f": {e.body}")
    if isinstance(e, requests.exceptions.Timeout):
        return f"no answer from {PAPERLESS_URL} within the time limit"
    if isinstance(e, requests.exceptions.ConnectionError):
        return (f"cannot reach {PAPERLESS_URL} – check PAPERLESS_URL and that both "
                "containers share a Docker network")
    return e.__class__.__name__


TASK_RE = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_task_warned = False


def task_state(task_id: str) -> dict:
    """What Paperless did with an uploaded document: consumed, in queue, failed or duplicate.

    "sent" means the status cannot be read. `final` tells the page to stop asking.
    """
    global _task_warned
    try:
        r = requests.get(f"{PAPERLESS_URL}/api/tasks/", params={"task_id": task_id},
                         headers=PAPERLESS_AUTH, timeout=10)
    except requests.exceptions.RequestException:
        return {"state": "sent", "final": False}
    if r.status_code in (401, 403):
        if not _task_warned:
            _task_warned = True
            app.logger.warning("Cannot read the consume status (HTTP %s): the token's user needs "
                               "the permission to view tasks. Uploads still work.", r.status_code)
        return {"state": "sent", "final": True, "detail": "The Paperless user may not view tasks."}
    try:
        data = r.json() if r.ok else None
    except ValueError:
        data = None
    if data is None:
        return {"state": "sent", "final": False}
    rows = data.get("results", []) if isinstance(data, dict) else data
    if not rows:
        return {"state": "in queue", "final": False}
    status, result = rows[0].get("status"), " ".join(str(rows[0].get("result") or "").split())[:300]
    if status == "SUCCESS":
        return {"state": "consumed", "final": True, "detail": result}
    if status == "FAILURE":
        return {"state": "duplicate" if "duplicate" in result.lower() else "failed",
                "final": True, "detail": result}
    return {"state": "in queue", "final": False}


STAGING = Path(os.environ.get("STAGING_DIR", "/tmp/staging")).resolve()
STAGING.mkdir(parents=True, exist_ok=True)
KEEP_HOURS = int(os.environ.get("KEEP_HOURS", "24"))  # delete undecided files after this
ID_RE = re.compile(r"^[0-9a-f]{32}$")


def stage(pdf_in: Path, title: str, work: Path, preset: str, images: list[Path] = ()) -> dict:
    """Compresses and stores the result for review.

    For a PDF made from photos, `images` are the originals: the size shown as
    "before" is theirs, so the saving is measured against what was uploaded.
    """
    before = sum(p.stat().st_size for p in images) if images else pdf_in.stat().st_size
    final = compress_pdf(pdf_in, work / f"{secrets.token_hex(4)}.compressed.pdf", preset)
    item_id = secrets.token_hex(16)
    pdf = STAGING / f"{item_id}.pdf"
    shutil.move(str(final), pdf)
    meta = {"id": item_id, "title": title, "before": before, "after": pdf.stat().st_size,
            "images": len(images), "pages": page_count(pdf), "created": time.time()}
    (STAGING / f"{item_id}.json").write_text(json.dumps(meta))
    app.logger.info("Staged %r (%s -> %s)", title, mb(before), mb(meta["after"]))
    return meta


def staged_items() -> list[dict]:
    limit = time.time() - KEEP_HOURS * 3600
    items = []
    for j in STAGING.glob("*.json"):
        try:
            meta = json.loads(j.read_text())
        except Exception:
            continue
        if meta["created"] < limit:
            remove_item(meta["id"])
        else:
            items.append(meta)
    return sorted(items, key=lambda m: m["created"])


def item_paths(item_id: str) -> tuple[Path, Path]:
    if not ID_RE.match(item_id):
        abort(404)
    pdf, meta = STAGING / f"{item_id}.pdf", STAGING / f"{item_id}.json"
    if not (pdf.exists() and meta.exists()):
        abort(404)
    return pdf, meta


def remove_item(item_id: str) -> None:
    for ext in (".pdf", ".json"):
        (STAGING / f"{item_id}{ext}").unlink(missing_ok=True)


def mb(n: int) -> str:
    return f"{n / 1024 / 1024:.2f} MB"


def clean_title(raw: str) -> str:
    return " ".join(raw.split())[:200]


def joined_title(stems: list[str]) -> str:
    """Title for several photos merged into one document."""
    return " + ".join(stems) if len(stems) <= 2 else f"{stems[0]} + {len(stems) - 1} more"


# Name and logo: Paperdrop's own unless docker-compose.yml sets APP_NAME or mounts a logo file
DEFAULT_NAME = "Paperdrop"
APP_NAME = os.environ.get("APP_NAME", "").strip() or DEFAULT_NAME
LOGO = Path(os.environ.get("LOGO_PATH", "/logo"))


def logo_mimetype() -> str | None:
    """MIME type of the mounted logo, or None if there is none or it is unusable."""
    if LOGO.is_dir():
        # Docker creates a directory when the host side of the mount does not exist
        app.logger.warning("Logo ignored: the file given in docker-compose.yml does not exist")
        return None
    if not LOGO.exists():
        return None
    try:
        with Image.open(LOGO, formats=["PNG", "JPEG", "WEBP"]) as im:
            return im.get_format_mimetype()
    except Exception as e:
        app.logger.warning("Logo ignored, it must be a PNG, JPG or WEBP image: %s", e)
        return None


LOGO_MIME = logo_mimetype()

# Added to the address of the script and the stylesheet, so that a browser or a caching
# proxy never pairs a new page with files it kept from the previous version
ASSET_VERSION = hashlib.sha256(b"".join(
    (Path(app.static_folder) / name).read_bytes() for name in ("app.js", "app.css"))).hexdigest()[:10]


@app.context_processor
def branding():
    return {"app_name": APP_NAME, "own_name": APP_NAME == DEFAULT_NAME, "v": ASSET_VERSION,
            "logo": "/logo" if LOGO_MIME else None,
            "paperless_host": urlsplit(PAPERLESS_URL).hostname or PAPERLESS_URL}


MAGIC = {b"%PDF-": "pdf", b"\xff\xd8\xff": "image", b"\x89PNG\r\n\x1a\n": "image"}


@app.post("/api/compress")
@login_required
def compress():
    """Turns the uploaded files into one staged document: a single PDF, or one or more photos."""
    files = [f for f in request.files.getlist("files") if f.filename]
    preset = QUALITY.get(request.form.get("quality", ""), QUALITY[DEFAULT_QUALITY])
    if not files:
        return {"error": "No file received."}, 400
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        saved, kinds = [], set()
        for i, f in enumerate(files):
            dest = work / f"{i}.upload"
            f.save(dest)
            with dest.open("rb") as fh:
                head = fh.read(8)
            kind = next((k for magic, k in MAGIC.items() if head.startswith(magic)), None)
            if not kind:
                return {"error": f"{f.filename}: only PDF, JPG and PNG are allowed"}, 400
            saved.append(dest)
            kinds.add(kind)
        stems = [Path(f.filename).stem for f in files]
        if kinds == {"pdf"} and len(saved) == 1:
            pdf, images, title = saved[0], [], stems[0]
        elif kinds == {"image"}:
            pdf, images, title = work / "merged.pdf", saved, joined_title(stems)
            try:
                images_to_pdf(images, pdf)
            except (Image.DecompressionBombError, Image.DecompressionBombWarning):
                return {"error": f"{title}: larger than {MAX_MEGAPIXELS} megapixels"}, 400
            except Exception as e:
                app.logger.warning("Image conversion of %r failed: %s", title, e)
                return {"error": f"{title}: the image could not be read"}, 400
        else:
            return {"error": "Send one PDF, or one or more images, per request."}, 400
        try:
            return {"item": stage(pdf, clean_title(title) or "Document", work, preset, images)}
        except Exception:
            app.logger.exception("Error processing %r", title)
            return {"error": f"{title}: processing failed"}, 500


@app.post("/item/<item_id>/view")
@login_required(form=True)
def view_item(item_id):
    pdf, meta = item_paths(item_id)
    title = json.loads(meta.read_text())["title"]
    return send_file(pdf, mimetype="application/pdf", download_name=f"{title}.pdf")


@app.post("/api/item/<item_id>/upload")
@login_required
def upload_item(item_id):
    pdf, meta = item_paths(item_id)
    title = clean_title(request.form.get("title", "")) or json.loads(meta.read_text())["title"]
    try:
        task = upload_to_paperless(pdf, title)
    except Exception as e:
        reason = failure_reason(e)
        # Full traceback only for errors that are not explained by the reason already
        unexpected = not isinstance(e, (PaperlessError, requests.exceptions.RequestException))
        app.logger.error("Upload of %r failed: %s | %s", title, reason, e, exc_info=unexpected)
        return {"error": f"Upload failed – {reason}. File kept for retry."}, 502
    remove_item(item_id)
    app.logger.info("Uploaded %r to Paperless (task %s)", title, task)
    return {"title": title, "task": task if TASK_RE.match(task) else None}


@app.post("/api/item/<item_id>/delete")
@login_required
def delete_item(item_id):
    pdf, meta = item_paths(item_id)
    title = json.loads(meta.read_text())["title"]
    remove_item(item_id)
    app.logger.info("Deleted %r", title)
    return {"ok": True}


@app.post("/api/tasks")
@login_required(touch=False)
def tasks():
    ids = [i for i in request.form.get("ids", "").split(",") if TASK_RE.match(i)][:20]
    return {"tasks": {i: task_state(i) for i in ids}}


@app.post("/api/ping")
@login_required(touch=False)
def ping():
    """Whether Paperless can be reached with the configured token."""
    try:
        r = requests.get(f"{PAPERLESS_URL}/api/", headers=PAPERLESS_AUTH, timeout=5)
    except requests.exceptions.RequestException as e:
        return {"ok": False, "detail": failure_reason(e)}
    if r.status_code == 401:
        return {"ok": False, "detail": HTTP_HINTS[401]}
    return {"ok": True, "detail": "Paperless is reachable"}


@app.get("/logo")
def logo():
    if not LOGO_MIME:
        abort(404)
    return send_file(LOGO, mimetype=LOGO_MIME)


@app.get("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")


@app.get("/health")
def health():
    return "ok"


app.logger.info("%s started – Paperless at %s", APP_NAME, PAPERLESS_URL)
