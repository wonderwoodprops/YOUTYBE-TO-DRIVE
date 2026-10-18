import os
import sys
import logging
import traceback
import threading
from flask import Flask, request, jsonify
import yt_dlp
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

# הגדרת פורמט לוגים מפורט שיוצג בבלוגים של Railway
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

app = Flask(__name__)

def check_environment_variables():
    """בדיקה מקדימה של משתני הסביבה הנדרשים"""
    required_vars = [
        'GOOGLE_REFRESH_TOKEN',
        'GOOGLE_CLIENT_ID',
        'GOOGLE_CLIENT_SECRET',
        'GOOGLE_DRIVE_FOLDER_ID'
    ]
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    if missing_vars:
        logging.warning(f"אזהרה: משתני הסביבה הבאים חסרים: {', '.join(missing_vars)}")
    else:
        logging.info("כל משתני הסביבה הנדרשים נמצאו בהצלחה.")

def get_drive_service():
    """חיבור לשירות Google Drive API"""
    try:
        logging.info("מנסה ליצור חיבור ל-Google Drive API...")
        creds = Credentials(
            token=None,
            refresh_token=os.environ.get('GOOGLE_REFRESH_TOKEN'),
            client_id=os.environ.get('GOOGLE_CLIENT_ID'),
            client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
            token_uri="https://oauth2.googleapis.com/token"
        )
        service = build('drive', 'v3', credentials=creds)
        logging.info("החיבור ל-Google Drive API נוצר בהצלחה.")
        return service
    except Exception as e:
        logging.error(f"שגיאה ביצירת החיבור ל-Google Drive: {str(e)}")
        logging.error(traceback.format_exc())
        raise

@app.route('/', methods=['GET'])
def health_check():
    """נתיב בדיקת תקינות (Health Check) לשליפה מהירה בדפדפן"""
    logging.info("התקבלה בקשת Health Check בנתיב הראשי /")
    return jsonify({
        "status": "online",
        "message": "השרת פעיל וממתין לבקשות הורדה בנתיב /download"
    }), 200

@app.route('/download', methods=['POST'])
def download_video():
    """קבלת בקשת ההורדה מ-Make או מקור חיצוני"""
    logging.info("=== התקבלה בקשת POST חדשה בנתיב /download ===")
    
    data = request.get_json(silent=True)
    if not data:
        logging.error("הבקשה נכשלה: לא התקבל payload בפורמט JSON תקין.")
        return jsonify({"error": "No JSON payload or invalid JSON"}), 400

    url = data.get('url')
    if not url:
        logging.error("הבקשה נכשלה: השדה 'url' חסר בתוך ה-JSON.")
        return jsonify({"error": "No URL provided in JSON"}), 400

    logging.info(f"התקבל קישור לעיבוד: {url}")

    # הפעלת תהליך ההורדה וההעלאה בתהליכון נפרד (Thread) כדי להחזיר תשובה מיידית ב-HTTP 200
    thread = threading.Thread(target=process_and_upload, args=(url,))
    thread.daemon = True
    thread.start()

    logging.info("תהליכון הטיפול ברקע הופעל בהצלחה. מחזיר תשובה ללקוח.")
    return jsonify({"status": "Processing started", "url": url}), 200

def process_and_upload(url):
    """תהליך הורדת הסרטון והעלאתו ל-Google Drive"""
    filename = None
    try:
        logging.info(f"[הרצה ברקע] מתחיל הורדת סרטון מיוטיוב: {url}")
        
        output_dir = 'downloads'
        os.makedirs(output_dir, exist_ok=True)

        ydl_opts = {
            'format': 'bv*+ba/b',
            'outtmpl': os.path.join(output_dir, '%(title)s.%(ext)s'),
            'quiet': False,
            'no_warnings': False,
        }

        # 1. הורדה באמצעות yt-dlp
        logging.info("[הרצה ברקע] מפעיל את yt-dlp...")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        logging.info(f"[הרצה ברקע] ההורדה הושלמה בהצלחה. הקובץ נשמר בנתיב: {filename}")

        if not os.path.exists(filename):
            logging.error(f"[הרצה ברקע] שגיאה: הקובץ {filename} לא נמצא על הדיסק לאחר ההורדה.")
            return

        # 2. העלאה ל-Google Drive
        logging.info("[הרצה ברקע] מתחיל תהליך העלאה ל-Google Drive...")
        service = get_drive_service()
        folder_id = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')

        file_metadata = {
            'name': os.path.basename(filename),
            'parents': [folder_id] if folder_id else []
        }

        media = MediaFileUpload(filename, resumable=True)
        request_upload = service.files().create(body=file_metadata, media_body=media, fields='id')
        
        response = None
        while response is None:
            status, response = request_upload.next_chunk()
            if status:
                logging.info(f"[הרצה ברקע] התקדמות העלאה: {int(status.progress() * 100)}%")

        logging.info(f"[הרצה ברקע] הקובץ הועלה בהצלחה ל-Google Drive! מזהה קובץ: {response.get('id')}")

    except Exception as e:
        logging.error(f"[הרצה ברקע] שגיאה קריטית בתהליך העיבוד: {str(e)}")
        logging.error(traceback.format_exc())

    finally:
        # 3. ניקוי הקובץ המקומי
        if filename and os.path.exists(filename):
            try:
                os.remove(filename)
                logging.info(f"[הרצה ברקע] הקובץ המקומי {filename} נמחק בהצלחה מהשרת.")
            except Exception as cleanup_error:
                logging.error(f"[הרצה ברקע] שגיאה במחיקת הקובץ המקומי: {str(cleanup_error)}")

if __name__ == '__main__':
    logging.info("מפעיל את השרת...")
    check_environment_variables()
    port = int(os.environ.get('PORT', 8080))
    logging.info(f"השרת מאזין בפורט: {port}")
    app.run(host='0.0.0.0', port=port)
