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
