#!/bin/sh
# עדכון yt-dlp בכל עליית שרת: יוטיוב משנה דברים כל כמה ימים, וגרסה ישנה היא הסיבה מספר 1 לכשלים.
pip install -q -U "yt-dlp[default]" || echo "עדכון yt-dlp נכשל, ממשיך עם הגרסה המותקנת"

# worker אחד בלבד (מצב המשימות נשמר בזיכרון), עם מספר threads לבקשות במקביל
exec gunicorn app:app \
  --bind "0.0.0.0:${PORT:-8080}" \
  --workers 1 \
  --threads 8 \
  --timeout 120 \
  --graceful-timeout 120 \
  --access-logfile -
