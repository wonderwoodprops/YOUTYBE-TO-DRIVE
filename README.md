# YouTube → Google Drive (Railway)

## קבצים להעלאה ל-GitHub
`app.py`, `requirements.txt`, `Dockerfile`, `start.sh`, `railway.json`

## משתני סביבה ב-Railway

| משתנה | חובה | הסבר |
|---|---|---|
| `GOOGLE_REFRESH_TOKEN`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_DRIVE_FOLDER_ID` | כן | חיבור ל-Drive |
| `YOUTUBE_COOKIES` או `YOUTUBE_COOKIES_B64` | מומלץ מאוד | תוכן קובץ cookies.txt (פורמט Netscape). B64 בטוח יותר כי לא נשברות שורות/טאבים |
| `YTDLP_PROXY` | מומלץ מאוד | פרוקסי residential, למשל `http://user:pass@host:port`. IP של Railway הוא datacenter ויוטיוב חוסמת אותו לעיתים קרובות |
| `API_KEY` | מומלץ | אם מוגדר, חובה לשלוח header בשם `X-API-Key` |
| `CALLBACK_URL` | לא | webhook של Make שיקבל JSON עם התוצאה בסיום |
| `DOWNLOAD_WORKERS` | לא | הורדות במקביל (ברירת מחדל 2) |
| `DOWNLOAD_SUBS` | לא | ברירת מחדל לכתוביות כשהבקשה לא מציינת (`1`/`0`). התוסף שולח בחירה לכל בקשה |
| `SUB_LANGS` | לא | ברירת מחדל `he,iw` (יוטיוב משתמשת לפעמים בקוד הישן `iw`) |
| `SUB_ATTEMPTS` | לא | ניסיונות לכתוביות (ברירת מחדל 4, עם backoff של 30/60/120 שניות) |
| `SUB_INITIAL_DELAY` | לא | המתנה בשניות בין הסרטון לבקשת הכתוביות (ברירת מחדל 10) |
| `SUB_BACKOFF` | לא | בסיס ה-backoff בשניות (ברירת מחדל 30) |
| `MIN_REQUEST_GAP` | לא | מרווח מינימלי בשניות בין בקשות מול יוטיוב, גלובלי (ברירת מחדל 6) |
| `UPLOAD_DESCRIPTION` | לא | ברירת מחדל לתיאור כשהבקשה לא מציינת (`1`/`0`) |
| `EMBED_SUBS` | לא | `0` (ברירת מחדל). `1` מטמיע את הכתוביות בתוך הסרטון שכבר בדרייב, ומעלה אותו מחדש |
| `DRIVE_ALLOW_ANY` | לא | `0` (ברירת מחדל): מעתיק מדרייב רק קבצי וידאו. `1` מאפשר כל סוג קובץ |
| `MEGA_PROXY` | לא | פרוקסי להורדות MEGA בלבד (למשל אם מתקבל `MEGA_QUOTA`) |
| `MEGA_TIMEOUT` | לא | זמן מקסימלי להורדת קובץ MEGA בשניות (ברירת מחדל 10800) |
| `YT_FORMAT` | לא | ברירת מחדל: עד 1080p, מחובר ל-mp4 |
| `YT_PLAYER_CLIENTS` | לא | לדוגמה `tv,web_safari` - עוקף את סדר הניסיונות האוטומטי |
| `POT_PROVIDER_URL` | לא | כתובת שרת bgutil PO-token (ראה למטה) |

## איך מייצאים קוקיז בצורה יציבה
יוטיוב מחליפה (rotate) קוקיז של חלון רגיל, ואז הם מפסיקים לעבוד תוך ימים. לכן:
1. פתח **חלון incognito**, התחבר ליוטיוב (עדיף חשבון ייעודי, לא הראשי).
2. באותו חלון גש ל-`https://www.youtube.com/robots.txt`.
3. ייצא קוקיז של youtube.com בלבד בעזרת תוסף "Get cookies.txt LOCALLY".
4. **סגור את חלון ה-incognito** ואל תשתמש בו שוב.
5. המר ל-base64 (`base64 -w0 cookies.txt`) והדבק ב-`YOUTUBE_COOKIES_B64`.

## בקשה לדוגמה
```
POST /download
X-API-Key: <API_KEY>
{"url": "https://www.youtube.com/watch?v=..."}
```
תגובה: `{"job_id": "..."}`, ובדיקת מצב ב-`GET /status/<job_id>`.

## אם עדיין חוסמים (Sign in to confirm you're not a bot)
1. הפרוקסי הוא התיקון האפקטיבי ביותר.
2. רענן קוקיז לפי התהליך למעלה.
3. הוסף שירות שני ב-Railway שמריץ את האימג' `brainicism/bgutil-ytdlp-pot-provider` (פורט 4416), הוסף `bgutil-ytdlp-pot-provider` ל-requirements, והגדר `POT_PROVIDER_URL=http://<service>.railway.internal:4416`.

## קבצים מ-MEGA
שולחים ל-`/download` קישור קובץ בודד עם המפתח: `https://mega.nz/file/ID#KEY` (גם הפורמט הישן `#!ID!KEY`). תיקיות לא נתמכות.
- ההורדה נעשית עם `megatools` (מותקן ב-Dockerfile) לדיסק זמני של Railway, ואז מועלית ל-Drive. הדיסק הזמני חייב להכיל את כל הקובץ.
- מפתח ההצפנה (החלק שאחרי `#`) מוסתר בלוגים, ב-`/status` וב-callback.
- שגיאות: `MEGA_QUOTA` (מכסת הורדה ל-IP, נפתרת אחרי כמה שעות או עם `MEGA_PROXY`), `MEGA_NOT_FOUND`, `MEGA_BAD_LINK`, `MEGA_TIMEOUT`, `MEGA_UNAVAILABLE`.

## העתקת קבצים מ-Google Drive
שולחים את אותו `/download` עם קישור דרייב ציבורי, למשל `https://drive.google.com/file/d/<ID>/view`.
השרת מעתיק את הקובץ בצד של גוגל לתיקיית היעד (`GOOGLE_DRIVE_FOLDER_ID`), בלי להוריד ובלי לשתף.
- נדרשת הרשאת `https://www.googleapis.com/auth/drive` בטוקן. עם `drive.file` בלבד תתקבל השגיאה `DRIVE_NOT_FOUND`.
- אם בעל הקובץ חסם העתקה/הורדה: `DRIVE_BLOCKED`. תיקיות לא נתמכות.

## סדר העיבוד ובקרה לפי בקשה (יוטיוב)
1. הסרטון יורד ומועלה לדרייב **קודם**. מיד אחרי זה `/status` מחזיר `video_uploaded: true` ו-`drive_file_id`.
2. אחר כך, אם התבקש, נשמר התיאור כקובץ `.txt`, ואחריו הכתוביות כקובץ `.srt` נפרד (עם backoff מול 429).
3. כשל בתיאור או בכתוביות לא פוגע בסרטון: הסטטוס הסופי `done`, ושדות `description` ו-`subtitles` מציינים מה קרה.

הבחירה נשלחת בכל בקשה: `{"url": "...", "cookies": [...], "options": {"subtitles": true, "description": true}}`.
שדה חסר -> נלקחת ברירת המחדל ממשתני הסביבה. ב-Make יש להוסיף לגוף הבקשה לשרת: `"options":{{toJSON(1.options)}}`.
ערכי `subtitles` ב-`/status`: `uploaded`, `embedded`, `unavailable`, `rate_limited`, `failed`, `upload_failed`, `disabled`, `pending`.

## סריקת דף ובחירה ידנית (`/ui`)

פתח `https://<האפליקציה-שלך>.up.railway.app/ui`, הזן את ה-`API_KEY`, הדבק כתובת של דף (למשל עמוד הבית של מאקו) ולחץ "סרוק דף".
השרת מחלץ מהדף קישורי יוטיוב, דרייב, MEGA, קבצי וידאו ישירים ודפי צפייה, ומציג אותם לבחירה. אפשר "לבדוק" דף בודד לפני העלאה, ואז "העלה לדרייב". הסרטון עולה קודם, ורק אחריו תיאור וכתוביות.

| משתנה | ברירת מחדל | תיאור |
|---|---|---|
| `ALLOW_GENERIC` | `1` | מאפשר סריקה והורדה כללית. דורש `API_KEY` מוגדר. `0` מכבה. |
| `GENERIC_PROXY` | ריק | פרוקסי לאתרים עם חסימה גיאוגרפית (למשל פרוקסי ישראלי). |

מגבלות: אתרי VOD מסחריים (כמו מאקו) מוגנים לרוב ב-DRM או בחסימה גיאוגרפית ולכן ייכשלו. הכפתור "בדוק" מראה זאת מראש. סרטוני יוטיוב מה-UI משתמשים בקוקיז ממשתני הסביבה, כי לדף אין גישה לקוקיז הדפדפן.
