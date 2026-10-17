# Paperdrop - sends documents to Paperless-ngx after compression and review
# Copyright (C) 2026 Pascal2108
#
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU Affero General Public License, version 3, as published by the Free Software Foundation.
# It comes WITHOUT ANY WARRANTY. See the file LICENSE for the full text.
#
# SPDX-License-Identifier: AGPL-3.0-only

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
from contextlib import ExitStack
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

def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return default if not value else value in ("1", "true", "yes", "on")


# Unsafe options, off unless switched on explicitly. Each one is announced in the log at every
# start and on the page itself, so that nobody runs with it by accident.
NO_LOGIN = env_flag("UNSAFE_NO_LOGIN")  # anyone who reaches the page may use it
ALLOW_HTTP = env_flag("UNSAFE_HTTP")    # the session cookie also works over plain HTTP
HIDE_WARNINGS = env_flag("UNSAFE_HIDE_WARNINGS")  # no notices on the page; the log still warns
BEHIND_PROXY = env_flag("BEHIND_PROXY", default=True)
# Camera photos go through the document scanner in the browser (find the page, straighten it);
# false uploads them as taken
DOCUMENT_SCANNER = env_flag("DOCUMENT_SCANNER", default=True)

SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "24"))
if NO_LOGIN:
    # Nothing to sign in with: the login settings are not needed and not used
    LOGIN_USER = LOGIN_PASSWORD = ""
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
else:
    missing = [n for n in ("UPLOADER_USER", "UPLOADER_PASSWORD", "SECRET_KEY") if not os.environ.get(n)]
    if missing:
        logging.critical("%s missing in secrets.env. Set it, then start the container again.",
                         ", ".join(missing))
        raise SystemExit(1)
    LOGIN_USER, LOGIN_PASSWORD = os.environ["UPLOADER_USER"], os.environ["UPLOADER_PASSWORD"]
    SECRET_KEY = os.environ["SECRET_KEY"]

    # The values in secrets.env.example are public: never run with them
    for _name, _example in (("UPLOADER_PASSWORD", "a-long-secure-password"),
                            ("SECRET_KEY", "paste-random-key-here")):
        if os.environ[_name] == _example:
            logging.critical("%s still has the value from secrets.env.example. Set your own in "
                             "secrets.env, then start the container again.", _name)
            raise SystemExit(1)
    if len(LOGIN_PASSWORD) < 12:
        logging.warning("UPLOADER_PASSWORD is shorter than 12 characters. Choose a longer one.")
    if len(SECRET_KEY) < 32:
        logging.warning("SECRET_KEY is short. Generate one with: openssl rand -hex 32")

if NO_LOGIN:
    logging.warning("UNSAFE: UNSAFE_NO_LOGIN is on. There is no login: anyone who can reach this "
                    "page can send documents to Paperless. Use it only in a network you fully trust.")
if ALLOW_HTTP:
    logging.warning("UNSAFE: UNSAFE_HTTP is on. Over plain HTTP, documents%s travel unencrypted "
                    "and can be read by anyone on the network.", "" if NO_LOGIN else ", password and session")
if HIDE_WARNINGS and (NO_LOGIN or ALLOW_HTTP):
    logging.warning("UNSAFE: UNSAFE_HIDE_WARNINGS is on. The page no longer tells its users about the "
                    "unsafe options above. This log is the only place that still does.")
if not BEHIND_PROXY and not NO_LOGIN:
    logging.warning("BEHIND_PROXY is off: the login lockout counts the address of each connection. "
                    "Do not put a proxy in front, or all users share one address.")

app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=MAX_MB * 1024 * 1024)

# Exactly one proxy (NPM) in front: only its X-Forwarded-For entry is trusted,
# so a spoofed header sent by the client is ignored. Without a proxy (BEHIND_PROXY off)
# the header is not trusted at all, since a client could otherwise choose its own address.
if BEHIND_PROXY:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Signing in sets a cookie with a random session token. Scripts cannot read it (HttpOnly)
# and the browser sends it over HTTPS only. It is valid for SESSION_HOURS from the moment
# of signing in, across reloads and browser restarts. Sessions are kept in memory, so a
# restart of the container signs everyone out.
#
# A session belongs to the address it was started from: a cookie copied to another device
# is refused there, while the owner stays signed in.
#
# The cookie alone is not enough for an action. Each one also carries a second token that
# only the page knows, so that another website cannot act with the user's cookie (CSRF).
# It comes in a header, so that a request is turned away before its body is read.
#
# Signing in answers with a redirect: reloading the page then never repeats the login form.
# The form also works only once, so a remembered password cannot be replayed with it. The
# per-start salt invalidates open login forms on a restart, when the list of used ones is gone.
#
# UNSAFE_HTTP drops the HTTPS-only flag (and the __Host- prefix, which requires it), so that
# the browser keeps the cookie over plain HTTP. UNSAFE_NO_LOGIN needs no cookie at all: the
# page token is the same for everyone and still keeps other websites from acting (CSRF).
COOKIE = "paperdrop" if ALLOW_HTTP else "__Host-paperdrop"
COOKIE_FLAGS = dict(secure=not ALLOW_HTTP, httponly=True, samesite="Lax", path="/")
OPEN_PAGE_TOKEN = secrets.token_urlsafe(32)  # the page token in UNSAFE_NO_LOGIN mode
_signer = URLSafeTimedSerializer(SECRET_KEY, salt=f"login-{secrets.token_hex(8)}")
_used_forms: dict[str, float] = {}
_sessions: dict[str, tuple[float, str, str]] = {}  # session token -> (end of validity, page token, address)
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
    if request.is_secure:
        # The session cookie works over HTTPS only anyway: have the browser never try plain HTTP
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    if resp.mimetype == "text/html":
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        )
    # Static files may be kept but are revalidated; nothing else is stored by the browser
    resp.headers["Cache-Control"] = "no-cache" if request.path.startswith("/static/") else "no-store"
    return resp


def locked(ip: str) -> bool:
    now = time.time()
    with _lock:
        # Addresses without a recent failure are forgotten, so the list cannot grow without end
        for old in [i for i, ts in _failed.items() if now - ts[-1] >= WINDOW]:
            del _failed[old]
        return len([t for t in _failed.get(ip, []) if now - t < WINDOW]) >= MAX_TRIES


def note_failure(ip: str) -> None:
    with _lock:
        _failed.setdefault(ip, []).append(time.time())


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


def current_session() -> tuple[float, str] | None:
    """(end of validity, page token) of the signed-in browser, or None."""
    now = time.time()
    if NO_LOGIN:
        return now + SESSION_HOURS * 3600, OPEN_PAGE_TOKEN
    with _lock:
        for old in [s for s, entry in _sessions.items() if entry[0] < now]:
            del _sessions[old]
        entry = _sessions.get(request.cookies.get(COOKIE, ""))
    if not entry:
        return None
    expires, page_token, ip = entry
    if ip != client_ip():
        app.logger.warning("Session started from %s refused for %s", ip, client_ip())
        return None
    return expires, page_token


def is_api() -> bool:
    return request.path.startswith("/api/")


def login_page(error: str | None = None, status: int = 200):
    return render_template("login.html", error=error, once=_signer.dumps(secrets.token_hex(8))), status


def signed_in(form: bool) -> bool:
    """True if the request has a valid session cookie and the page token that belongs to it.

    "Open PDF" is a plain form posted to a new tab and carries the page token as a field
    (form=True). The body is only read once the cookie has been accepted.
    """
    session = current_session()
    if not session:
        return False
    sent = request.form.get("auth", "") if form else request.headers.get("X-Auth", "")
    return hmac.compare_digest(_digest(sent), _digest(session[1]))


def login_required(view=None, *, form: bool = False):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not signed_in(form):
                if is_api():
                    return {"error": "signed out"}, 401
                if NO_LOGIN:
                    # e.g. "Open PDF" from a page that is older than the last restart
                    return redirect("/", 303)
                return login_page("Please sign in again.", 401)
            return view(*args, **kwargs)
        return wrapper
    return decorator(view) if view else decorator


# Shown on the login page after a redirect from /login
LOGIN_MESSAGES = {
    "again": "Please sign in again.",
    "locked": "Too many failed attempts – please wait 15 minutes.",
    "invalid": "Invalid username or password.",
}


@app.get("/")
def home():
    """The upload page for a signed-in browser, the login page for everyone else."""
    session = current_session()
    if not session:
        return login_page(LOGIN_MESSAGES.get(request.args.get("m", "")))
    expires, page_token = session
    return render_template("index.html", boot={
        "auth": page_token, "items": staged_items(), "sessionHours": SESSION_HOURS,
        "expiresIn": int(expires - time.time()), "maxMb": MAX_MB, "quality": DEFAULT_QUALITY,
        "scanner": DOCUMENT_SCANNER,
    })


@app.post("/login")
def login():
    if NO_LOGIN:
        return redirect("/", 303)
    ip = client_ip()
    # A repeated form (restored tab, back button) is turned away before the password is looked at
    if not login_form_fresh(request.form.get("once", "")):
        return redirect("/?m=again", 303)
    if locked(ip):
        return redirect("/?m=locked", 303)
    if not check_credentials(request.form.get("user", ""), request.form.get("password", "")):
        note_failure(ip)
        app.logger.warning("Failed login from %s", ip)
        time.sleep(1)
        return redirect("/?m=invalid", 303)
    with _lock:
        _failed.pop(ip, None)
    app.logger.info("Login from %s", ip)
    if not request.is_secure and not ALLOW_HTTP:
        app.logger.warning("Login over plain HTTP: the browser will not keep the session cookie. "
                           "Open the page through the HTTPS address of your reverse proxy "
                           "(or, unsafe, set UNSAFE_HTTP).")
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = (time.time() + SESSION_HOURS * 3600, secrets.token_urlsafe(32), ip)
    resp = redirect("/", 303)
    resp.set_cookie(COOKIE, token, max_age=SESSION_HOURS * 3600, **COOKIE_FLAGS)
    return resp


@app.post("/api/logout")
@login_required
def logout():
    with _lock:
        _sessions.pop(request.cookies.get(COOKIE, ""), None)
    resp = app.make_response({"ok": True})
    resp.delete_cookie(COOKIE, **COOKIE_FLAGS)
    return resp


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
# A large photo is shrunk to fit the page; a small one (a screenshot) keeps its size and is
# centred. Stretching it would leave so few pixels per inch that Paperless cannot read the text.
A4 = (img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297))
A4_LAYOUT = img2pdf.get_layout_fun(pagesize=A4, fit=img2pdf.FitMode.shrink, auto_orient=True)


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


def merge_to_pdf(parts: list[tuple[Path, str]], out: Path) -> None:
    """PDFs and images -> one PDF, in the given order. Images become A4 pages, PDFs keep theirs."""
    with ExitStack() as stack:
        merged = stack.enter_context(pikepdf.new())
        for path, kind in parts:
            if kind == "image":
                page = path.with_suffix(".pdf")
                images_to_pdf([path], page)
                path = page
            # The source stays open until the result is saved: pages are copied from it only then
            merged.pages.extend(stack.enter_context(pikepdf.open(path)).pages)
        merged.save(out)


def compress_pdf(src: Path, dst: Path, preset: str) -> Path:
    """Compresses with Ghostscript. Returns whichever file is smaller."""
    cmd = [
        "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.6",
        f"-dPDFSETTINGS=/{preset}",
        "-dNOPAUSE", "-dQUIET", "-dBATCH", "-dSAFER",
        f"-sOutputFile={dst}", str(src),
    ]
    try:
        # Ghostscript keeps its own temporary files next to the output, in the work folder
        subprocess.run(cmd, check=True, timeout=300, env={**os.environ, "TMPDIR": str(dst.parent)})
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
    status, result = str(rows[0].get("status") or "").upper(), task_result(rows[0])
    if status == "SUCCESS":
        return {"state": "consumed", "final": True, "detail": result}
    if status == "FAILURE":
        return {"state": "duplicate" if "duplicate" in result.lower() else "failed",
                "final": True, "detail": result}
    return {"state": "in queue", "final": False}


def task_result(row: dict) -> str:
    """What Paperless says about a finished task, in older and newer versions of its API.

    Older versions send `result` as text and the status in capitals; newer ones send
    `result_data` with an error, or with the id of the new document.
    """
    data = row.get("result_data")
    if isinstance(data, dict):
        if data.get("error_message") or data.get("error_type"):
            text = f"{data.get('error_type') or ''}: {data.get('error_message') or ''}".strip(": ")
        elif data.get("document_id") is not None:
            text = f"Success. New document id {data['document_id']} created"
        else:
            text = ""
    else:
        text = str(row.get("result") or "")
    return " ".join(text.split())[:300]


STAGING = Path(os.environ.get("STAGING_DIR", "/tmp/staging")).resolve()
STAGING.mkdir(parents=True, exist_ok=True)
KEEP_MINUTES = int(os.environ.get("KEEP_MINUTES", "30"))  # delete undecided files after this
# Files of a request in progress. Whatever an interrupted request leaves here is swept away.
WORK = STAGING / "work"
ID_RE = re.compile(r"^[0-9a-f]{32}$")


def stage(pdf_in: Path, title: str, work: Path, preset: str, sources: list[Path], images: int) -> dict:
    """Compresses and stores the result for review.

    `sources` are the uploaded files the PDF was made from, `images` how many of them are
    photos. The size shown as "before" is theirs, so the saving is measured against what was uploaded.
    """
    before = sum(p.stat().st_size for p in sources)
    final = compress_pdf(pdf_in, work / f"{secrets.token_hex(4)}.compressed.pdf", preset)
    item_id = secrets.token_hex(16)
    pdf = STAGING / f"{item_id}.pdf"
    shutil.move(str(final), pdf)
    meta = {"id": item_id, "title": title, "before": before, "after": pdf.stat().st_size,
            "files": len(sources), "images": images, "pages": page_count(pdf), "created": time.time()}
    (STAGING / f"{item_id}.json").write_text(json.dumps(meta))
    app.logger.info("Staged %r (%s -> %s)", masked(title), mb(before), mb(meta["after"]))
    return meta


def staged_items() -> list[dict]:
    limit = time.time() - KEEP_MINUTES * 60
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


def remove_expired() -> None:
    """Drops staged documents past KEEP_MINUTES, and files of a staging that was cut short."""
    staged_items()
    limit = time.time() - KEEP_MINUTES * 60
    for f in STAGING.iterdir():
        if f.is_file() and f.stat().st_mtime < limit:
            f.unlink(missing_ok=True)


def sweep_work(max_age: float) -> None:
    """Removes what interrupted requests left in the work folder."""
    limit = time.time() - max_age
    for entry in WORK.iterdir():
        try:
            if entry.lstat().st_mtime >= limit:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as e:
            app.logger.warning("Could not remove %s: %s", entry.name, e)


def janitor() -> None:
    """Every minute: expired documents. Every hour: leftovers in the work folder."""
    minutes = 0
    while True:
        time.sleep(60)
        minutes += 1
        try:
            remove_expired()
            if minutes % 60 == 0:
                sweep_work(3600)
        except Exception:
            app.logger.exception("Cleanup failed")


def masked(title: str) -> str:
    """A document title as written to the log: only its first and last three characters."""
    return f"{title[:3]}******{title[-3:]}" if len(title) > 6 else "******"


def mb(n: int) -> str:
    return f"{n / 1024 / 1024:.2f} MB"


def clean_title(raw: str) -> str:
    return " ".join(raw.split())[:200]


def joined_title(stems: list[str]) -> str:
    """Title for one or more files merged into one document."""
    return " + ".join(stems) if len(stems) <= 2 else f"{stems[0]} + {len(stems) - 1} more"


# Name and logo: Paperdrop's own unless docker-compose.yml sets APP_NAME or mounts a logo file
DEFAULT_NAME = "Paperdrop"
APP_NAME = os.environ.get("APP_NAME", "").strip() or DEFAULT_NAME
LOGO = Path(os.environ.get("LOGO_PATH", "/logo"))

# Colours of the page: dark, light, or auto, which follows the setting of the device
THEME = os.environ.get("THEME", "").strip().lower() or "dark"
if THEME not in ("dark", "light", "auto"):
    logging.warning("THEME must be dark, light or auto, not %r. Using dark.", THEME)
    THEME = "dark"


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
    (Path(app.static_folder) / name).read_bytes() for name in ("app.js", "app.css", "scanner.js"))).hexdigest()[:10]


@app.context_processor
def branding():
    return {"app_name": APP_NAME, "own_name": APP_NAME == DEFAULT_NAME, "v": ASSET_VERSION, "theme": THEME,
            "logo": "/logo" if LOGO_MIME else None,
            "paperless_host": urlsplit(PAPERLESS_URL).hostname or PAPERLESS_URL,
            "login_enabled": not NO_LOGIN, "unsafe_notes": unsafe_notes(),
            "http_hint": not (NO_LOGIN or ALLOW_HTTP or request.is_secure or is_local_host())}


def unsafe_notes() -> list[dict]:
    """The warnings shown on every page while an unsafe option is in effect.

    "soft" is a quieter line for a setting chosen on purpose that stays the same on every visit;
    "strong" is for what puts this very connection at risk.
    """
    notes = []
    if HIDE_WARNINGS:  # chosen by the admin: the log at start is the only warning left
        return notes
    if NO_LOGIN:
        notes.append({"level": "soft", "text": "No login: anyone who can open this page can send documents to Paperless."})
    if ALLOW_HTTP and not request.is_secure:
        notes.append({"level": "strong", "text": "This connection is not encrypted. Documents"
                      + ("" if NO_LOGIN else " and your password")
                      + " travel in plain text and can be read by others on the network."})
    return notes


def is_local_host() -> bool:
    # Browsers keep HTTPS-only cookies for these addresses even over plain HTTP
    return request.host.rsplit(":", 1)[0].strip("[]") in ("localhost", "127.0.0.1", "::1")


MAGIC = {b"%PDF-": "pdf", b"\xff\xd8\xff": "image", b"\x89PNG\r\n\x1a\n": "image"}


@app.post("/api/compress")
@login_required
def compress():
    """Turns the uploaded files into one staged document, merged in the order they were sent."""
    files = [f for f in request.files.getlist("files") if f.filename]
    preset = QUALITY.get(request.form.get("quality", ""), QUALITY[DEFAULT_QUALITY])
    if not files:
        return {"error": "No file received."}, 400
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        work = Path(tmp)
        saved, kinds = [], []
        for i, f in enumerate(files):
            dest = work / f"{i}.upload"
            f.save(dest)
            with dest.open("rb") as fh:
                head = fh.read(8)
            kind = next((k for magic, k in MAGIC.items() if head.startswith(magic)), None)
            if not kind:
                return {"error": f"{f.filename}: only PDF, JPG and PNG are allowed"}, 400
            saved.append(dest)
            kinds.append(kind)
        stems = [Path(f.filename).stem for f in files]
        title = joined_title(stems)
        if kinds == ["pdf"]:
            pdf = saved[0]
        else:
            pdf = work / "merged.pdf"
            try:
                if "pdf" in kinds:
                    merge_to_pdf(list(zip(saved, kinds)), pdf)
                else:
                    images_to_pdf(saved, pdf)
            except (Image.DecompressionBombError, Image.DecompressionBombWarning):
                return {"error": f"{title}: an image is larger than {MAX_MEGAPIXELS} megapixels"}, 400
            except pikepdf.PasswordError:
                return {"error": f"{title}: a PDF is password-protected and cannot be merged"}, 400
            except Exception as e:
                app.logger.warning("Conversion of %r failed: %s", masked(title), e)
                return {"error": f"{title}: a file could not be read"}, 400
        try:
            images = kinds.count("image")
            return {"item": stage(pdf, clean_title(title) or "Document", work, preset, saved, images)}
        except Exception:
            app.logger.exception("Error processing %r", masked(title))
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
        app.logger.error("Upload of %r failed: %s | %s", masked(title), reason, e, exc_info=unexpected)
        return {"error": f"Upload failed – {reason}. File kept for retry."}, 502
    remove_item(item_id)
    app.logger.info("Uploaded %r to Paperless (task %s)", masked(title), task)
    return {"title": title, "task": task if TASK_RE.match(task) else None}


@app.post("/api/item/<item_id>/delete")
@login_required
def delete_item(item_id):
    pdf, meta = item_paths(item_id)
    title = json.loads(meta.read_text())["title"]
    remove_item(item_id)
    app.logger.info("Deleted %r", masked(title))
    return {"ok": True}


@app.post("/api/tasks")
@login_required
def tasks():
    ids = [i for i in request.form.get("ids", "").split(",") if TASK_RE.match(i)][:20]
    return {"tasks": {i: task_state(i) for i in ids}}


@app.post("/api/ping")
@login_required
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


# One worker serves all requests, so at start nothing in the work folder is in use
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True, exist_ok=True)
remove_expired()
threading.Thread(target=janitor, daemon=True).start()

app.logger.info("%s started – Paperless at %s", APP_NAME, PAPERLESS_URL)
