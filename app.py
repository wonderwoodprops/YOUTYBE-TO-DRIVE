import base64
import hmac
import logging
import os
import random
import re
import shutil
import subprocess
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
API_KEY = os.environ.get("API_KEY", "")
PROXY = os.environ.get("YTDLP_PROXY", "")
CALLBACK_URL = os.environ.get("CALLBACK_URL", "")
MAX_WORKERS = int(os.environ.get("DOWNLOAD_WORKERS", "2"))
VIDEO_FORMAT = os.environ.get("YT_FORMAT", "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b")
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "")
COOKIES_SRC = "/tmp/yt_cookies_src.txt"
COOKIES_MIN = "/tmp/yt_cookies_min.txt"
WORK_ROOT = os.environ.get("WORK_DIR", "/tmp/downloads")

# מרווח מינימלי (שניות) בין בקשות מול יוטיוב, גלובלי לכל ה-workers
MIN_REQUEST_GAP = float(os.environ.get("MIN_REQUEST_GAP", "6"))

# כתוביות: מורדות בבקשה נפרדת, אחרי הסרטון, עם backoff. כשל בהן לא פוגע בסרטון.
DOWNLOAD_SUBS = os.environ.get("DOWNLOAD_SUBS", "1") == "1"
UPLOAD_SRT = os.environ.get("UPLOAD_SRT", "1") == "1"   # גם srt נפרד לצד הסרטון המוטמע
SUB_LANGS = [l.strip() for l in os.environ.get("SUB_LANGS", "he,iw").split(",") if l.strip()]
SUB_ATTEMPTS = int(os.environ.get("SUB_ATTEMPTS", "4"))
SUB_INITIAL_DELAY = float(os.environ.get("SUB_INITIAL_DELAY", "10"))  # המתנה לפני הניסיון הראשון
SUB_BACKOFF = float(os.environ.get("SUB_BACKOFF", "30"))              # 30, 60, 120... שניות

# תיאור הסרטון מיוטיוב נשמר כקובץ טקסט לצד הסרטון
UPLOAD_DESCRIPTION = os.environ.get("UPLOAD_DESCRIPTION", "1") == "1"

# העתקת קבצים מ-Google Drive: ברירת מחדל - רק קבצי וידאו
DRIVE_ALLOW_ANY = os.environ.get("DRIVE_ALLOW_ANY", "0") == "1"
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp", ".ts")
DRIVE_HOSTS = {"drive.google.com", "docs.google.com"}

# MEGA: מורד עם megatools (megadl) לדיסק זמני ומעלה ל-Drive
MEGA_HOSTS = {"mega.nz", "mega.co.nz"}
MEGA_TIMEOUT = int(os.environ.get("MEGA_TIMEOUT", "10800"))   # שניות להורדה אחת (ברירת מחדל 3 שעות)
MEGA_PROXY = os.environ.get("MEGA_PROXY", "")                 # אופציונלי, נפרד מפרוקסי יוטיוב (חוסך תעבורה יקרה)

ESSENTIAL_COOKIES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "LOGIN_INFO", "PREF",
}

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be", "www.youtu.be",
}

_custom = os.environ.get("YT_PLAYER_CLIENTS", "")
PLAYER_CLIENT_ATTEMPTS = (
    [[c.strip() for c in _custom.split(",") if c.strip()]]
    if _custom
    else [None, ["tv", "web_safari"], ["mweb"], ["web_embedded"]]
)

PERMANENT_ERRORS = (
    "private video", "video unavailable", "has been removed", "members-only",
    "account associated with this video has been terminated",
    "this live event will begin", "premieres in",
)
AUTH_ERRORS = ("sign in to confirm", "not a bot", "login required", "cookies")

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="dl")
jobs = {}
jobs_lock = threading.Lock()
MAX_JOBS_KEPT = 300

_throttle_lock = threading.Lock()
_last_request_at = 0.0


def throttle():
    """מבטיח מרווח מינימלי בין בקשות מול יוטיוב, גם בין workers שונים."""
    global _last_request_at
    with _throttle_lock:
        wait = _last_request_at + MIN_REQUEST_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def is_rate_limited(msg: str) -> bool:
    m = msg.lower()
    return "429" in m or "too many requests" in m


class CaptureLogger:
    """לוגר ל-yt-dlp שמזהה 429 באזהרות (כשל בכתוביות מדווח כאזהרה ולא כחריגה)."""

    def __init__(self):
        self._log = logging.getLogger("yt-dlp")
        self.rate_limited = False
        self.cookies_invalid = False

    def debug(self, msg):
        (self._log.debug if msg.startswith("[debug] ") else self._log.info)(msg)

    def info(self, msg):
        self._log.info(msg)

    def warning(self, msg):
        if "cookies are no longer valid" in msg:
            if not self.cookies_invalid:  # ההודעה חוזרת עשרות פעמים - מתעדים פעם אחת
                self._log.warning("הקוקיז אינם תקינים (עברו rotation בדפדפן).")
            self.cookies_invalid = True
            return
        if is_rate_limited(msg):
            self.rate_limited = True
        self._log.warning(msg)

    def error(self, msg):
        if is_rate_limited(msg):
            self.rate_limited = True
        self._log.error(msg)


# ---------------------------------------------------------------------------
# קוקיז
# ---------------------------------------------------------------------------
def _normalize_cookies(raw: str) -> str:
    text = raw.strip().replace("\r\n", "\n").replace("\r", "\n")
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    if "\\t" in text:
        text = text.replace("\\t", "\t")
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


def filter_essential(netscape_text: str) -> str:
    out = ["# Netscape HTTP Cookie File"]
    for line in netscape_text.split("\n"):
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5] in ESSENTIAL_COOKIES:
            out.append(line)
    return "\n".join(out) + "\n"


def cookies_to_netscape(cookies):
    """ממיר רשימת קוקיז בפורמט chrome.cookies לקובץ Netscape. None אם אין תקינים."""
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
        if exp <= 0:
            # קוקיז סשן (בלי תוקף): expiry=0 עלול להיחשב כפג תוקף ולהיזרק בטעינה
            exp = int(time.time()) + 86400
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        lines.append("\t".join([
            prefix + domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            str(c.get("path") or "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(exp), name, value,
        ]))
    return "\n".join(lines) + "\n" if len(lines) > 1 else None


def setup_cookies_file():
    b64 = os.environ.get("YOUTUBE_COOKIES_B64")
    raw = os.environ.get("YOUTUBE_COOKIES")
    if b64:
        raw = base64.b64decode(b64).decode("utf-8")
    if not raw:
        log.warning("לא הוגדרו קוקיז במשתנה סביבה (אפשר גם לקבל אותם עם כל בקשה).")
        return
    normalized = _normalize_cookies(raw)
    with open(COOKIES_SRC, "w", encoding="utf-8") as f:
        f.write(normalized)
    with open(COOKIES_MIN, "w", encoding="utf-8") as f:
        f.write(filter_essential(normalized))
    total = sum(1 for l in normalized.split("\n") if len(l.split("\t")) >= 7)
    log.info("קובץ קוקיז נוצר (%d רשומות).", total)
    if total == 0:
        log.warning("לא זוהו רשומות קוקיז תקינות - בדוק את פורמט המשתנה.")


def startup_checks():
    required = ["GOOGLE_REFRESH_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_DRIVE_FOLDER_ID"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        log.warning("משתני סביבה חסרים: %s", ", ".join(missing))
    log.info("yt-dlp גרסה: %s", yt_dlp.version.__version__)
    log.info("ffmpeg: %s | deno: %s | node: %s | megadl: %s",
             bool(shutil.which("ffmpeg")), bool(shutil.which("deno")), bool(shutil.which("node")),
             bool(shutil.which("megadl")))
    if not shutil.which("deno") and not shutil.which("node"):
        log.warning("אין JS runtime (deno/node) - yt-dlp לא יוכל לפתור את אתגרי יוטיוב.")
    if not API_KEY:
        log.warning("API_KEY לא מוגדר - ה-endpoint פתוח לכולם!")
    os.makedirs(WORK_ROOT, exist_ok=True)


setup_cookies_file()
startup_checks()


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------
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
    req = service.files().create(body=metadata, media_body=media, fields="id", supportsAllDrives=True)
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
# העתקת קובץ מ-Google Drive ציבורי לדרייב שלך (העתקה בצד של גוגל, בלי שיתוף)
# ---------------------------------------------------------------------------
def parse_drive_link(url):
    """מחזיר (file_id, resource_key) מקישור דרייב, או None אם זה לא קישור לקובץ."""
    try:
        p = urlparse(url)
    except Exception:
        return None
    if p.scheme not in ("http", "https") or (p.hostname or "").lower() not in DRIVE_HOSTS:
        return None
    if re.search(r"/(drive/)?folders/|/drive/u/\d+/folders/", p.path):
        return None  # תיקיות לא נתמכות
    m = re.search(r"/(?:file|document|spreadsheets|presentation)/(?:u/\d+/)?d/([\w-]{20,})", p.path)
    fid = m.group(1) if m else None
    q = dict(kv.split("=", 1) for kv in (p.query or "").split("&") if "=" in kv)
    if not fid:
        fid = q.get("id") if re.fullmatch(r"[\w-]{20,}", q.get("id", "")) else None
    if not fid:
        return None
    return fid, q.get("resourcekey")


def _drive_exec(request_obj, file_id, resource_key):
    if resource_key:
        request_obj.headers["X-Goog-Drive-Resource-Keys"] = f"{file_id}/{resource_key}"
    return request_obj.execute(num_retries=3)


def _http_reason(e: HttpError) -> str:
    try:
        return e.error_details[0].get("reason", "") if e.error_details else ""
    except Exception:
        return ""


def copy_drive_file(file_id, resource_key, job_dir):
    """מעתיק קובץ לתיקיית היעד. מחזיר (id חדש, שם, שיטה: copy/download)."""
    service = get_drive_service()
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")

    try:
        meta = _drive_exec(
            service.files().get(fileId=file_id, fields="id,name,mimeType,size", supportsAllDrives=True),
            file_id, resource_key,
        )
    except HttpError as e:
        if e.resp.status == 404:
            raise RuntimeError(
                "DRIVE_NOT_FOUND: הקובץ לא נמצא או שאינו נגיש. בדוק שהקישור ציבורי, "
                "ושהטוקן של גוגל כולל הרשאת drive מלאה (לא drive.file)."
            )
        raise

    mime, name = meta.get("mimeType", ""), meta.get("name", file_id)
    if mime == "application/vnd.google-apps.folder":
        raise RuntimeError("DRIVE_UNSUPPORTED: זו תיקייה, לא קובץ.")
    if not DRIVE_ALLOW_ANY and not (mime.startswith("video/") or name.lower().endswith(VIDEO_EXTS)):
        raise RuntimeError(f"DRIVE_NOT_VIDEO: הקובץ אינו וידאו ({mime}). לאישור כל סוג קובץ: DRIVE_ALLOW_ANY=1")
    log.info("קובץ דרייב: %s (%s, %s bytes)", name, mime, meta.get("size", "?"))

    body = {}
    if folder_id:
        body["parents"] = [folder_id]
    try:
        copied = _drive_exec(
            service.files().copy(fileId=file_id, body=body, fields="id,name", supportsAllDrives=True),
            file_id, resource_key,
        )
        return copied["id"], copied.get("name", name), "copy"
    except HttpError as e:
        reason = _http_reason(e)
        log.warning("copy נכשל (%s, %s) - מנסה הורדה והעלאה.", e.resp.status, reason)
        if reason == "storageQuotaExceeded":
            raise RuntimeError("DRIVE_QUOTA: אין מקום פנוי בדרייב שלך.")
        if e.resp.status not in (403, 400):
            raise

    # גיבוי: הורדה דרך ה-API והעלאה מחדש (כשהעתקה ישירה חסומה אבל קריאה מותרת)
    from googleapiclient.http import MediaIoBaseDownload
    dest = os.path.join(job_dir, re.sub(r"[\\/:*?\"<>|]", "_", name))
    try:
        req = service.files().get_media(fileId=file_id, supportsAllDrives=True)
        if resource_key:
            req.headers["X-Goog-Drive-Resource-Keys"] = f"{file_id}/{resource_key}"
        with open(dest, "wb") as fh:
            dl = MediaIoBaseDownload(fh, req, chunksize=32 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk(num_retries=5)
    except HttpError as e:
        if _http_reason(e) in ("downloadQuotaExceeded", "cannotDownloadFile", "forbidden") or e.resp.status == 403:
            raise RuntimeError("DRIVE_BLOCKED: בעל הקובץ חסם העתקה/הורדה, או שחרגה מכסת ההורדות של הקובץ.")
        raise
    return upload_to_drive(dest), name, "download"


# ---------------------------------------------------------------------------
# תיאור הסרטון כקובץ טקסט
# ---------------------------------------------------------------------------
def write_description_file(info, video_path):
    """כותב קובץ טקסט עם כותרת, ערוץ, תאריך ותיאור. None אם אין תיאור."""
    info = info or {}
    desc = (info.get("description") or "").strip()
    if not desc:
        return None
    d = info.get("upload_date") or ""
    if re.fullmatch(r"\d{8}", d):
        d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    header = [
        f"כותרת: {info.get('title') or ''}",
        f"ערוץ: {info.get('uploader') or info.get('channel') or ''}",
        f"תאריך העלאה: {d}",
        f"קישור: {info.get('webpage_url') or ''}",
        "", "-" * 40, "",
    ]
    path = os.path.splitext(video_path)[0] + ".txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(header) + desc + "\n")
    return path


# ---------------------------------------------------------------------------
# MEGA (קובץ בודד)
# ---------------------------------------------------------------------------
def mega_host(url):
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.") in MEGA_HOSTS
    except Exception:
        return False


def parse_mega_link(url):
    """מחזיר קישור קנוני https://mega.nz/file/ID#KEY, או None (גם לתיקיות)."""
    if not mega_host(url):
        return None
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return None
    frag = p.fragment or ""
    m = re.fullmatch(r"/(?:file|embed)/([\w-]{8})", p.path)
    if m and re.fullmatch(r"[\w-]{16,}", frag):
        return f"https://mega.nz/file/{m.group(1)}#{frag}"
    m = re.fullmatch(r"!([\w-]{8})!([\w-]{16,})", frag)       # פורמט ישן: #!ID!KEY
    if p.path in ("", "/") and m:
        return f"https://mega.nz/file/{m.group(1)}#{m.group(2)}"
    return None


def redact_url(url):
    """המפתח שאחרי # הוא סוד: לא נכנס ללוגים, ל-/status או ל-callback."""
    if "#" in url and "mega." in url:
        return url.split("#", 1)[0] + "#***"
    return url


def run_megadl(url, job_dir):
    if not shutil.which("megadl"):
        raise RuntimeError("MEGA_UNAVAILABLE: megatools לא מותקן בשרת (נדרש להריץ דרך ה-Dockerfile).")
    key = url.split("#", 1)[1]
    cmd = ["megadl", "--no-progress", "--path", job_dir]
    if MEGA_PROXY:
        cmd += ["--proxy", MEGA_PROXY]
    cmd.append(url)

    out = ""
    for attempt in range(1, 4):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=MEGA_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"MEGA_TIMEOUT: ההורדה לא הסתיימה תוך {MEGA_TIMEOUT} שניות.")
        out = (((r.stdout or "") + "\n" + (r.stderr or "")).strip()).replace(key, "***")
        if r.returncode == 0:
            return
        low = out.lower()
        log.warning("megadl נכשל (ניסיון %d/3): %s", attempt, out[-300:])
        if any(s in low for s in ("quota", "509", "overquota", "bandwidth limit")):
            raise RuntimeError("MEGA_QUOTA: חרגת ממכסת ההורדה של MEGA ל-IP הזה. נסה שוב בעוד כמה שעות, "
                               "או הגדר MEGA_PROXY.")
        if any(s in low for s in ("not found", "enoent", "no such file", "does not exist", "removed")):
            raise RuntimeError("MEGA_NOT_FOUND: הקובץ לא נמצא או שהוסר.")
        if any(s in low for s in ("ekey", "decrypt", "invalid key", "invalid link", "bad link")):
            raise RuntimeError("MEGA_BAD_LINK: הקישור או המפתח שלו לא תקינים.")
        if attempt < 3:
            time.sleep(20 * attempt)
    raise RuntimeError("MEGA_FAILED: " + out[-300:])


def find_mega_file(job_dir):
    files = [os.path.join(job_dir, f) for f in os.listdir(job_dir)
             if not f.startswith(".megatmp") and not f.endswith(".megatmp") and
             os.path.isfile(os.path.join(job_dir, f))]
    return max(files, key=os.path.getsize) if files else None


def process_mega(job_id, url):
    job_dir = os.path.join(WORK_ROOT, job_id)
    os.makedirs(job_dir, exist_ok=True)
    try:
        set_job(job_id, status="downloading")
        log.info("[%s] מתחיל הורדה מ-MEGA: %s", job_id, redact_url(url))
        run_megadl(url, job_dir)
        path = find_mega_file(job_dir)
        if not path:
            raise RuntimeError("MEGA_FAILED: ההורדה הסתיימה אך לא נמצא קובץ.")
        log.info("[%s] הורדה הושלמה: %s (%.1f MB)", job_id, os.path.basename(path),
                 os.path.getsize(path) / 1024 / 1024)
        set_job(job_id, status="uploading", filename=os.path.basename(path))
        file_id = upload_to_drive(path)
        set_job(job_id, status="done", drive_file_id=file_id)
        log.info("[%s] הועלה ל-Drive: %s", job_id, file_id)
    except HttpError as e:
        log.error("[%s] שגיאת Google Drive: %s", job_id, e)
        set_job(job_id, status="error", error=f"Drive: {e}"[:500])
    except Exception as e:
        log.error("[%s] שגיאה: %s", job_id, e)
        set_job(job_id, status="error", error=str(e)[:500])
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        notify_callback(job_id)


# ---------------------------------------------------------------------------
# הורדת הסרטון (בלי כתוביות)
# ---------------------------------------------------------------------------
def build_ydl_opts(job_dir, player_clients, cookie_src=None, logger=None):
    opts = {
        "format": VIDEO_FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(job_dir, "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "noprogress": True,
        "logger": logger or CaptureLogger(),
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 4,
        "sleep_interval_requests": 1,
        "cachedir": "/tmp/yt-dlp-cache",
    }
    extractor_args = {}
    if player_clients:
        extractor_args["youtube"] = {"player_client": player_clients}
    if POT_PROVIDER_URL:
        extractor_args["youtubepot-bgutilhttp"] = {"base_url": [POT_PROVIDER_URL]}
    if extractor_args:
        opts["extractor_args"] = extractor_args
    if PROXY:
        opts["proxy"] = PROXY
    if cookie_src and os.path.exists(cookie_src):
        # עותק לכל משימה: yt-dlp כותב חזרה לקובץ הקוקיז
        job_cookies = os.path.join(job_dir, "cookies.txt")
        shutil.copyfile(cookie_src, job_cookies)
        opts["cookiefile"] = job_cookies
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


def download_with_fallbacks(url, job_dir, extra_cookies=None):
    """מחזיר (נתיב הקובץ, player_clients שהצליחו, קובץ הקוקיז שהצליח, info של yt-dlp)."""
    last_error = None

    # (שם, נתיב, קבוצה). קבוצה = מקור הקוקיז; אם yt-dlp מדווח שהקוקיז של מקור "לא תקינים"
    # (עברו rotation) מדלגים על כל הקבוצה, כי ניסיון חוזר עם אותם קוקיז הוא בזבוז.
    cookie_modes = []
    if extra_cookies:
        req_full = os.path.join(job_dir, "req_cookies_full.txt")
        req_min = os.path.join(job_dir, "req_cookies_min.txt")
        with open(req_full, "w", encoding="utf-8") as f:
            f.write(extra_cookies)
        with open(req_min, "w", encoding="utf-8") as f:
            f.write(filter_essential(extra_cookies))
        cookie_modes += [("request-full", req_full, "request"), ("request-min", req_min, "request")]
    cookie_modes += [(n, p, "env") for n, p in (("full", COOKIES_SRC), ("minimal", COOKIES_MIN)) if os.path.exists(p)]
    if not cookie_modes:
        cookie_modes = [("none", None, "none")]

    dead_groups = set()
    saw_429 = False

    for i, clients in enumerate(PLAYER_CLIENT_ATTEMPTS, start=1):
        label = ",".join(clients) if clients else "default"
        rate_limited = False
        tried = False
        for mode_name, cookie_path, group in cookie_modes:
            if group in dead_groups:
                continue
            tried = True
            log.info("ניסיון %d/%d (clients=%s, cookies=%s)", i, len(PLAYER_CLIENT_ATTEMPTS), label, mode_name)
            throttle()
            cap = CaptureLogger()
            try:
                with yt_dlp.YoutubeDL(build_ydl_opts(job_dir, clients, cookie_path, cap)) as ydl:
                    info = ydl.extract_info(url, download=True)
                path = find_output_file(info, job_dir)
                if path:
                    log.info("הורדה הצליחה עם cookies=%s, clients=%s", mode_name, label)
                    return path, clients, cookie_path, info
                last_error = RuntimeError("ההורדה הסתיימה אך לא נמצא קובץ")
                break
            except DownloadError as e:
                msg = str(e).lower()
                last_error = e
                saw_429 = saw_429 or cap.rate_limited
                if any(p in msg for p in PERMANENT_ERRORS):
                    log.error("שגיאה קבועה, אין טעם לנסות שוב: %s", e)
                    raise
                if cap.cookies_invalid:
                    dead_groups.add(group)
                    log.warning("הקוקיז ממקור '%s' לא תקינים - מדלג על כל הסט שלהם.", group)
                    continue
                if is_rate_limited(msg):
                    rate_limited = True
                    log.warning("429 מול יוטיוב (clients=%s) - ממתין לפני ניסיון נוסף.", label)
                    break
                if any(a in msg for a in AUTH_ERRORS):
                    log.warning("בעיית אימות (cookies=%s, clients=%s) - עובר לאפשרות הבאה.", mode_name, label)
                    continue
                log.warning("הורדה נכשלה (clients=%s): %s", label, e)
                break
        if not tried:
            break  # כל מקורות הקוקיז נפסלו - אין טעם לעבור ללקוחות נוספים
        pause = (30 * i + random.uniform(0, 10)) if (rate_limited or saw_429) else 3 * i
        time.sleep(pause)

    # הודעות שגיאה ברורות (מופיעות ב-/status וב-callback)
    if dead_groups and all(g in dead_groups for _, _, g in cookie_modes):
        raise RuntimeError(
            "COOKIES_INVALID: כל הקוקיז שנוסו אינם תקינים (עברו rotation). "
            "שלח קוקיז טריים מהתוסף (וודא ש-Make מעביר את השדה cookies) או ייצא קוקיז חדשים."
        )
    if saw_429:
        raise RuntimeError(
            "RATE_LIMITED: יוטיוב מחזירה 429 ל-IP של השרת. נדרש פרוקסי residential (YTDLP_PROXY) או המתנה."
        )
    raise last_error or RuntimeError("ההורדה נכשלה")


# ---------------------------------------------------------------------------
# כתוביות: בקשה נפרדת עם backoff, והטמעה ב-ffmpeg
# ---------------------------------------------------------------------------
def find_sub_files(job_dir):
    names = os.listdir(job_dir)
    srt = sorted(os.path.join(job_dir, n) for n in names if n.endswith(".srt"))
    return srt or sorted(os.path.join(job_dir, n) for n in names if n.endswith(".vtt"))


def pick_sub(files):
    for lang in ("he", "iw"):
        for f in files:
            if f.rsplit(".", 2)[-2] == lang:
                return f
    return files[0]


def fetch_subtitles(url, job_dir, clients, cookie_path):
    """
    מחזיר (רשימת קבצים, סיבה). סיבה: ok / unavailable / rate_limited / failed.
    ה-429 על כתוביות מדווח ע"י yt-dlp כאזהרה בלבד, ולכן בודקים גם אם הקבצים באמת נוצרו.
    """
    reason = "failed"
    for attempt in range(1, SUB_ATTEMPTS + 1):
        delay = SUB_INITIAL_DELAY if attempt == 1 else SUB_BACKOFF * (2 ** (attempt - 2)) + random.uniform(0, 5)
        log.info("כתוביות: ניסיון %d/%d בעוד %.0f שניות", attempt, SUB_ATTEMPTS, delay)
        time.sleep(delay)
        throttle()

        cap = CaptureLogger()
        opts = build_ydl_opts(job_dir, clients, cookie_path, cap)
        opts.update({
            "skip_download": True,
            "ignore_no_formats_error": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": SUB_LANGS,
            "subtitlesformat": "srt/vtt/best",
            "sleep_interval_subtitles": 3,
            "postprocessors": [{"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"}],
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except DownloadError as e:
            msg = str(e).lower()
            log.warning("כתוביות: ניסיון %d נכשל: %s", attempt, e)
            if any(p in msg for p in PERMANENT_ERRORS):
                return [], "failed"
            reason = "rate_limited" if is_rate_limited(msg) else "failed"
            continue

        if not (info or {}).get("requested_subtitles"):
            log.info("כתוביות: אין כתוביות בעברית לסרטון הזה.")
            return [], "unavailable"

        files = find_sub_files(job_dir)
        if files:
            return files, "ok"
        reason = "rate_limited" if cap.rate_limited else "failed"
        log.warning("כתוביות: הקבצים לא נוצרו (%s).", reason)
    return [], reason


def embed_subtitles(video_path, sub_path) -> bool:
    ext = os.path.splitext(video_path)[1].lower()
    codec = {".mp4": "mov_text", ".m4v": "mov_text", ".mov": "mov_text",
             ".mkv": "srt", ".webm": "webvtt"}.get(ext)
    if not codec or not shutil.which("ffmpeg"):
        return False
    out = os.path.splitext(video_path)[0] + ".embedded" + ext
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", video_path, "-i", sub_path,
        "-map", "0:v", "-map", "0:a?", "-map", "1:0",
        "-c:v", "copy", "-c:a", "copy", "-c:s", codec,
        "-metadata:s:s:0", "language=heb",
        out,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=900, capture_output=True)
        os.replace(out, video_path)
        return True
    except Exception as e:
        log.warning("הטמעת כתוביות נכשלה, הסרטון יישמר בלעדיהן (srt יועלה בנפרד): %s", e)
        if os.path.exists(out):
            os.remove(out)
        return False


# ---------------------------------------------------------------------------
# ניהול משימה
# ---------------------------------------------------------------------------
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


def process_drive_copy(job_id, drive_ref):
    job_dir = os.path.join(WORK_ROOT, job_id)
    os.makedirs(job_dir, exist_ok=True)
    try:
        file_id, resource_key = drive_ref
        set_job(job_id, status="copying")
        new_id, name, method = copy_drive_file(file_id, resource_key, job_dir)
        set_job(job_id, status="done", drive_file_id=new_id, filename=name, method=method)
        log.info("[%s] הועתק לדרייב שלך (%s): %s", job_id, method, new_id)
    except HttpError as e:
        log.error("[%s] שגיאת Google Drive: %s", job_id, e)
        set_job(job_id, status="error", error=f"Drive: {e}"[:500])
    except Exception as e:
        log.error("[%s] שגיאה: %s", job_id, e)
        set_job(job_id, status="error", error=str(e)[:500])
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        notify_callback(job_id)


def process_and_upload(job_id, url, extra_cookies=None):
    job_dir = os.path.join(WORK_ROOT, job_id)
    os.makedirs(job_dir, exist_ok=True)
    try:
        set_job(job_id, status="downloading")
        log.info("[%s] מתחיל הורדה: %s", job_id, url)
        path, clients, cookie_path, info = download_with_fallbacks(url, job_dir, extra_cookies)
        log.info("[%s] הורדה הושלמה: %s (%.1f MB)", job_id, os.path.basename(path),
                 os.path.getsize(path) / 1024 / 1024)

        subs_state = "disabled"
        srt_to_upload = None
        if DOWNLOAD_SUBS and SUB_LANGS:
            set_job(job_id, status="subtitles")
            try:
                files, reason = fetch_subtitles(url, job_dir, clients, cookie_path)
            except Exception as e:  # כתוביות לעולם לא מפילות את המשימה
                log.warning("[%s] כשל לא צפוי בכתוביות: %s", job_id, e)
                files, reason = [], "failed"
            if files:
                chosen = pick_sub(files)
                if embed_subtitles(path, chosen):
                    subs_state = "embedded"
                    srt_to_upload = chosen if UPLOAD_SRT else None
                else:
                    subs_state = "separate_only"
                    srt_to_upload = chosen
            else:
                subs_state = reason  # unavailable / rate_limited / failed
            log.info("[%s] מצב כתוביות: %s", job_id, subs_state)

        desc_path, desc_state = None, "disabled"
        if UPLOAD_DESCRIPTION:
            try:
                desc_path = write_description_file(info, path)
                desc_state = "uploaded" if desc_path else "empty"
            except Exception as e:  # תיאור לעולם לא מפיל את המשימה
                log.warning("[%s] יצירת קובץ התיאור נכשלה: %s", job_id, e)
                desc_state = "failed"

        set_job(job_id, status="uploading", filename=os.path.basename(path),
                subtitles=subs_state, description=desc_state)
        file_id = upload_to_drive(path)
        if srt_to_upload:
            try:
                upload_to_drive(srt_to_upload)
            except Exception as e:
                log.warning("[%s] העלאת srt נכשלה: %s", job_id, e)
        if desc_path:
            try:
                upload_to_drive(desc_path)
            except Exception as e:
                log.warning("[%s] העלאת קובץ התיאור נכשלה: %s", job_id, e)
                set_job(job_id, description="failed")
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
        "subtitles": DOWNLOAD_SUBS,
        "description": UPLOAD_DESCRIPTION,
        "mega": bool(shutil.which("megadl")),
    }), 200


@app.route("/download", methods=["POST"])
def download_video():
    if not authorized():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    drive_ref = parse_drive_link(url)
    mega_url = parse_mega_link(url)
    is_youtube = valid_youtube_url(url)
    if mega_host(url) and not mega_url:
        return jsonify({"error": "MEGA: only single-file links with a key (mega.nz/file/ID#KEY) are supported"}), 400
    if not is_youtube and not drive_ref and not mega_url:
        return jsonify({"error": "Only YouTube, Google Drive or MEGA file URLs are allowed"}), 400
    source = "youtube" if is_youtube else "mega" if mega_url else "drive"

    extra_cookies = None
    if is_youtube:
        extra_cookies = cookies_to_netscape(data.get("cookies"))
        if data.get("cookies"):
            log.info("התקבלו קוקיז עם הבקשה (תקינים: %s).", bool(extra_cookies))
        else:
            log.warning("הבקשה הגיעה בלי קוקיז - בדוק שב-Make מועבר השדה cookies.")

    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        if len(jobs) >= MAX_JOBS_KEPT:
            for old in list(jobs)[: MAX_JOBS_KEPT // 3]:
                jobs.pop(old, None)
        jobs[job_id] = {"status": "queued", "url": redact_url(url), "source": source}

    if is_youtube:
        executor.submit(process_and_upload, job_id, url, extra_cookies)
    elif mega_url:
        executor.submit(process_mega, job_id, mega_url)
    else:
        executor.submit(process_drive_copy, job_id, drive_ref)
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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
