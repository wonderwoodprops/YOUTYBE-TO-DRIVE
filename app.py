import base64
import logging
import os
import shutil
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
import yt_dlp
from flask import Flask, jsonify, request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from yt_dlp.utils import DownloadError

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("app")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024  # עד 1MB לבקשה

# ---------------------------------------------------------------------------
# הגדרות (הכל ניתן לשינוי ממשתני סביבה ב-Railway)
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("API_KEY", "")                      # אם מוגדר - נדרש header בשם X-API-Key
PROXY = os.environ.get("YTDLP_PROXY", "")                    # למשל http://user:pass@host:port (מומלץ residential)
CALLBACK_URL = os.environ.get("CALLBACK_URL", "")            # webhook (Make) לעדכון בסיום
MAX_WORKERS = int(os.environ.get("DOWNLOAD_WORKERS", "2"))
VIDEO_FORMAT = os.environ.get(
    "YT_FORMAT", "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
)
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "")    # אופציונלי: שרת bgutil PO-token
COOKIES_SRC = "/tmp/yt_cookies_src.txt"
COOKIES_MIN = "/tmp/yt_cookies_min.txt"

# כתוביות: מוטמעות בתוך קובץ ה-mp4 (soft subtitles), ובנוסף נשמר .srt נפרד ב-Drive
DOWNLOAD_SUBS = os.environ.get("DOWNLOAD_SUBS", "1") == "1"
UPLOAD_SRT = os.environ.get("UPLOAD_SRT", "1") == "1"
SUB_LANGS = [l.strip() for l in os.environ.get("SUB_LANGS", "he,iw").split(",") if l.strip()]

# קוקיז הכרחיים בלבד. נשמטים הקוקיז המתחלפים (PSIDTS / PSIDCC / BUCKET) שהם הסיבה
# הנפוצה לפקיעת תוקף, ויוטיוב מנפיקה אותם מחדש בעצמה.
ESSENTIAL_COOKIES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "LOGIN_INFO", "PREF",
}
WORK_ROOT = os.environ.get("WORK_DIR", "/tmp/downloads")

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be", "www.youtu.be",
}

# סדר ניסיונות: ברירת מחדל של yt-dlp קודם (הכי מעודכן), אחר כך לקוחות חלופיים
_custom = os.environ.get("YT_PLAYER_CLIENTS", "")
PLAYER_CLIENT_ATTEMPTS = (
    [[c.strip() for c in _custom.split(",") if c.strip()]]
    if _custom
    else [None, ["tv", "web_safari"], ["mweb"], ["web_embedded"]]
)

PERMANENT_ERRORS = (
    "private video",
    "video unavailable",
    "has been removed",
    "members-only",
    "account associated with this video has been terminated",
    "this live event will begin",
    "premieres in",
)
AUTH_ERRORS = ("sign in to confirm", "not a bot", "login required", "cookies")

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="dl")
jobs = {}
jobs_lock = threading.Lock()
MAX_JOBS_KEPT = 300


# ---------------------------------------------------------------------------
# אתחול
# ---------------------------------------------------------------------------
def _normalize_cookies(raw: str) -> str:
    """מתקן קוקיז שנשברו בהעתקה למשתנה סביבה (שורות/טאבים)."""
    text = raw.strip().replace("\r\n", "\n").replace("\r", "\n")
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    if "\\t" in text:
        text = text.replace("\\t", "\t")
    # אם הטאבים הפכו לרווחים בהעתקה - משחזר את הפורמט (7 שדות מופרדים בטאב)
    fixed = []
    for line in text.split("\n"):
        if line.strip() and "\t" not in line and (not line.startswith("#") or line.startswith("#HttpOnly_")):
            parts = line.split(None, 6)
            if len(parts) == 7:
                line = "\t".join(parts)
        fixed.append(line)
    text = "\n".join(fixed)
    if not text.startswith("# Netscape HTTP Cookie File") and not text.startswith("# HTTP Cookie File"):
        text = "# Netscape HTTP Cookie File\n" + text
    return text + "\n"


def setup_cookies_file():
    b64 = os.environ.get("YOUTUBE_COOKIES_B64")
    raw = os.environ.get("YOUTUBE_COOKIES")
    if b64:
        raw = base64.b64decode(b64).decode("utf-8")
    if not raw:
        log.warning("לא הוגדרו קוקיז (YOUTUBE_COOKIES / YOUTUBE_COOKIES_B64). משרת ענן סביר שיוטיוב יחסום.")
        return
    normalized = _normalize_cookies(raw)
    with open(COOKIES_SRC, "w", encoding="utf-8") as f:
        f.write(normalized)

    minimal = ["# Netscape HTTP Cookie File"]
    total = 0
    for line in normalized.split("\n"):
        parts = line.split("\t")
        if len(parts) >= 7:
            total += 1
            if parts[5] in ESSENTIAL_COOKIES:
                minimal.append(line)
    with open(COOKIES_MIN, "w", encoding="utf-8") as f:
        f.write("\n".join(minimal) + "\n")
    log.info("קובץ קוקיז נוצר (%d רשומות, מתוכן %d הכרחיות לגיבוי).", total, len(minimal) - 1)
    if total == 0:
        log.warning("לא זוהו רשומות קוקיז תקינות - בדוק את פורמט המשתנה.")


def startup_checks():
    required = ["GOOGLE_REFRESH_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_DRIVE_FOLDER_ID"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        log.warning("משתני סביבה חסרים: %s", ", ".join(missing))
    log.info("yt-dlp גרסה: %s", yt_dlp.version.__version__)
    log.info("ffmpeg: %s | deno: %s | node: %s",
             bool(shutil.which("ffmpeg")), bool(shutil.which("deno")), bool(shutil.which("node")))
    if not shutil.which("deno") and not shutil.which("node"):
        log.warning("אין JS runtime (deno/node) - yt-dlp לא יוכל לפתור את אתגרי יוטיוב.")
    if not API_KEY:
        log.warning("API_KEY לא מוגדר - ה-endpoint פתוח לכולם!")
    if PROXY:
        log.info("פרוקסי מוגדר.")
    os.makedirs(WORK_ROOT, exist_ok=True)


setup_cookies_file()
startup_checks()


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------
def cookies_to_netscape(cookies):
    """ממיר רשימת קוקיז בפורמט chrome.cookies לקובץ Netscape. מחזיר None אם אין תקינים."""
    if not isinstance(cookies, list):
        return None
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies[:300]:
        if not isinstance(c, dict):
            continue
        domain, name, value = str(c.get("domain", "")), str(c.get("name", "")), str(c.get("value", ""))
        if not name or not (domain.endswith("youtube.com") or domain.endswith("google.com")):
            continue
        if any(ch in (domain + name + value) for ch in "\t\r\n"):
            continue
        try:
            exp = int(float(c.get("expirationDate") or 0))
        except (TypeError, ValueError):
            exp = 0
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        lines.append("\t".join([
            prefix + domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            str(c.get("path") or "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(exp), name, value,
        ]))
    return "\n".join(lines) + "\n" if len(lines) > 1 else None


def filter_essential(netscape_text: str) -> str:
    out = ["# Netscape HTTP Cookie File"]
    for line in netscape_text.split("\n"):
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5] in ESSENTIAL_COOKIES:
            out.append(line)
    return "\n".join(out) + "\n"


def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_to_drive(path: str) -> str:
    service = get_drive_service()
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    metadata = {"name": os.path.basename(path)}
    if folder_id:
        metadata["parents"] = [folder_id]

    media = MediaFileUpload(path, chunksize=16 * 1024 * 1024, resumable=True)
    req = service.files().create(
        body=metadata, media_body=media, fields="id,webViewLink", supportsAllDrives=True
    )
    response = None
    last_pct = -1
    while response is None:
        status, response = req.next_chunk(num_retries=5)
        if status:
            pct = int(status.progress() * 100)
            if pct // 10 != last_pct // 10:
                log.info("העלאה: %d%%", pct)
                last_pct = pct
    return response["id"]


# ---------------------------------------------------------------------------
# הורדה
# ---------------------------------------------------------------------------
def build_ydl_opts(job_dir: str, player_clients, cookie_src=None):
    opts = {
        "format": VIDEO_FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(job_dir, "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "noprogress": True,
        "logger": logging.getLogger("yt-dlp"),
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 4,
        "cachedir": "/tmp/yt-dlp-cache",
    }
    youtube_args = {}
    if player_clients:
        youtube_args["player_client"] = player_clients

    extractor_args = {}
    if youtube_args:
        extractor_args["youtube"] = youtube_args
    if POT_PROVIDER_URL:
        extractor_args["youtubepot-bgutilhttp"] = {"base_url": [POT_PROVIDER_URL]}
    if extractor_args:
        opts["extractor_args"] = extractor_args

    if PROXY:
        opts["proxy"] = PROXY

    if cookie_src and os.path.exists(cookie_src):
        # עותק לכל משימה: yt-dlp כותב חזרה לקובץ הקוקיז, ואסור שמשימות ידרסו זו את זו
        job_cookies = os.path.join(job_dir, "cookies.txt")
        shutil.copyfile(cookie_src, job_cookies)
        opts["cookiefile"] = job_cookies

    if DOWNLOAD_SUBS and SUB_LANGS:
        opts.update({
            "writesubtitles": True,          # כתוביות ידניות (עדיפות)
            "writeautomaticsub": True,       # אוטומטיות אם אין ידניות
            "subtitleslangs": SUB_LANGS,
            "subtitlesformat": "srt/vtt/best",
            "postprocessors": [
                {"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"},
                # הטמעה בתוך ה-mp4; already_have_subtitle שומר את קובצי ה-srt להעלאה נפרדת
                {"key": "FFmpegEmbedSubtitle", "already_have_subtitle": True},
            ],
        })
    return opts


def find_output_file(info, job_dir):
    try:
        path = info["requested_downloads"][0]["filepath"]
        if os.path.exists(path):
            return path
    except (KeyError, IndexError, TypeError):
        pass
    candidates = [
        os.path.join(job_dir, f) for f in os.listdir(job_dir)
        if not f.endswith((".txt", ".part", ".ytdl", ".srt", ".vtt"))
    ]
    return max(candidates, key=os.path.getsize) if candidates else None


def download_with_fallbacks(url: str, job_dir: str, extra_cookies: str = None):
    last_error = None
    # קודם כל הקוקיז המלאים, ואם יש בעיית אימות - רק ההכרחיים
    cookie_modes = []
    if extra_cookies:
        # קוקיז טריים שנשלחו עם הבקשה (מהתוסף) - עדיפות ראשונה
        req_full = os.path.join(job_dir, "req_cookies_full.txt")
        req_min = os.path.join(job_dir, "req_cookies_min.txt")
        with open(req_full, "w", encoding="utf-8") as f:
            f.write(extra_cookies)
        with open(req_min, "w", encoding="utf-8") as f:
            f.write(filter_essential(extra_cookies))
        cookie_modes += [("request-full", req_full), ("request-min", req_min)]
    cookie_modes += [(n, p) for n, p in (("full", COOKIES_SRC), ("minimal", COOKIES_MIN)) if os.path.exists(p)]
    if not cookie_modes:
        cookie_modes = [("none", None)]

    for i, clients in enumerate(PLAYER_CLIENT_ATTEMPTS, start=1):
        label = ",".join(clients) if clients else "default"
        for mode_name, cookie_path in cookie_modes:
            log.info("ניסיון %d/%d (clients=%s, cookies=%s)", i, len(PLAYER_CLIENT_ATTEMPTS), label, mode_name)
            try:
                with yt_dlp.YoutubeDL(build_ydl_opts(job_dir, clients, cookie_path)) as ydl:
                    info = ydl.extract_info(url, download=True)
                path = find_output_file(info, job_dir)
                if path:
                    return path
                last_error = RuntimeError("ההורדה הסתיימה אך לא נמצא קובץ")
                break
            except DownloadError as e:
                msg = str(e).lower()
                last_error = e
                if any(p in msg for p in PERMANENT_ERRORS):
                    log.error("שגיאה קבועה, אין טעם לנסות שוב: %s", e)
                    raise
                if any(a in msg for a in AUTH_ERRORS):
                    log.warning("בעיית אימות (cookies=%s, clients=%s) - עובר לאפשרות הבאה.", mode_name, label)
                    continue  # מנסה את סט הקוקיז הבא עם אותו לקוח
                log.warning("הורדה נכשלה (clients=%s): %s", label, e)
                break  # שגיאה אחרת: עובר ללקוח הבא
        time.sleep(3 * i)
    raise last_error or RuntimeError("ההורדה נכשלה")


def set_job(job_id, **fields):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(fields)


def notify_callback(job_id):
    if not CALLBACK_URL:
        return
    try:
        with jobs_lock:
            payload = dict(jobs[job_id], job_id=job_id)
        requests.post(CALLBACK_URL, json=payload, timeout=15)
    except Exception as e:
        log.warning("שליחת callback נכשלה: %s", e)


def process_and_upload(job_id: str, url: str, extra_cookies: str = None):
    job_dir = os.path.join(WORK_ROOT, job_id)
    os.makedirs(job_dir, exist_ok=True)
    try:
        set_job(job_id, status="downloading")
        log.info("[%s] מתחיל הורדה: %s", job_id, url)
        path = download_with_fallbacks(url, job_dir, extra_cookies)
        size_mb = os.path.getsize(path) / 1024 / 1024
        log.info("[%s] הורדה הושלמה: %s (%.1f MB)", job_id, os.path.basename(path), size_mb)

        srt_files = sorted(
            os.path.join(job_dir, f) for f in os.listdir(job_dir) if f.endswith(".srt")
        )
        if DOWNLOAD_SUBS:
            if srt_files:
                log.info("[%s] נמצאו כתוביות: %s", job_id, [os.path.basename(s) for s in srt_files])
            else:
                log.info("[%s] לא נמצאו כתוביות בעברית לסרטון הזה.", job_id)

        set_job(job_id, status="uploading", filename=os.path.basename(path), subtitles=bool(srt_files))
        file_id = upload_to_drive(path)
        if UPLOAD_SRT:
            for srt in srt_files:
                try:
                    upload_to_drive(srt)
                except Exception as e:
                    log.warning("[%s] העלאת %s נכשלה: %s", job_id, os.path.basename(srt), e)
        set_job(job_id, status="done", drive_file_id=file_id)
        log.info("[%s] הועלה ל-Drive: %s", job_id, file_id)
    except HttpError as e:
        log.error("[%s] שגיאת Google Drive: %s", job_id, e)
        set_job(job_id, status="error", error=f"Drive: {e}")
    except Exception as e:
        log.error("[%s] שגיאה: %s", job_id, e)
        log.error(traceback.format_exc())
        set_job(job_id, status="error", error=str(e)[:500])
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        notify_callback(job_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def authorized() -> bool:
    if not API_KEY:
        return True
    import hmac
    return hmac.compare_digest(request.headers.get("X-API-Key", ""), API_KEY)


def valid_youtube_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and (p.hostname or "").lower() in ALLOWED_HOSTS
    except Exception:
        return False


@app.route("/", methods=["GET"])
def health_check():
    return jsonify({
        "status": "online",
        "yt_dlp": yt_dlp.version.__version__,
        "cookies": os.path.exists(COOKIES_SRC),
        "proxy": bool(PROXY),
        "js_runtime": bool(shutil.which("deno") or shutil.which("node")),
    }), 200


@app.route("/download", methods=["POST"])
def download_video():
    if not authorized():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not valid_youtube_url(url):
        return jsonify({"error": "Only YouTube URLs are allowed"}), 400

    extra_cookies = cookies_to_netscape(data.get("cookies"))
    if data.get("cookies"):
        log.info("התקבלו קוקיז עם הבקשה (תקינים: %s).", bool(extra_cookies))

    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        if len(jobs) >= MAX_JOBS_KEPT:
            for old in list(jobs)[: MAX_JOBS_KEPT // 3]:
                jobs.pop(old, None)
        jobs[job_id] = {"status": "queued", "url": url}

    executor.submit(process_and_upload, job_id, url, extra_cookies)
    return jsonify({"status": "success", "message": "הבקשה התקבלה!", "job_id": job_id}), 202


@app.route("/status/<job_id>", methods=["GET"])
def job_status(job_id):
    if not authorized():
        return jsonify({"error": "Unauthorized"}), 401
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify(job), 200


if __name__ == "__main__":
    # להרצה מקומית בלבד. ב-Railway רץ gunicorn (ראה start.sh)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
