import os
from flask import Flask, request, jsonify
import threading
import yt_dlp
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

app = Flask(__name__)

def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ.get('GOOGLE_REFRESH_TOKEN'),
        client_id=os.environ.get('GOOGLE_CLIENT_ID'),
        client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
        token_uri="https://oauth2.googleapis.com/token"
    )
    return build('drive', 'v3', credentials=creds)

@app.route('/download', methods=['POST'])
def download_video():
    data = request.json
    if not data:
        return jsonify({"error": "No JSON payload"}), 400

    url = data.get('url')
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # הפעלת ההורדה ברקע כדי שהשרת יחזיר תשובה מיידית
    threading.Thread(target=process_and_upload, args=(url,)).start()
    return jsonify({"status": "Processing started"}), 200

def process_and_upload(url):
    try:
        ydl_opts = {
            'format': 'bv*+ba/b',
            'outtmpl': 'downloads/%(title)s.%(ext)s',
        }

        os.makedirs('downloads', exist_ok=True)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        print(f"Downloaded: {filename}")

        service = get_drive_service()
        folder_id = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')

        file_metadata = {
            'name': os.path.basename(filename),
            'parents': [folder_id] if folder_id else []
        }

        media = MediaFileUpload(filename, resumable=True)
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        print(f"Uploaded to Google Drive with ID: {file.get('id')}")

        os.remove(filename)

    except Exception as e:
        print(f"Error: {str(e)}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
