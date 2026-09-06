# Production deployment checklist

## 1. Configure secrets

Copy `.env.example` to `.env` and fill in at minimum:

- `BOT_TOKEN`
- `MAIN_CHANNEL_ID`
- `ADMINS`
- `SUPER_ADMINS` (recommended)
- chat/topic IDs used by moderation and admin notifications

Never commit `.env`, the SQLite database, logs, or backups.

## 2. Native Linux deployment

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo apt-get install tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng
python main.py
```

The local semantic model is downloaded on first startup. Subsequent inference is local.

## 3. Docker

```bash
docker build -t smotrbot .
docker run --env-file .env -v ./data:/app/data -v ./backups:/app/backups smotrbot
```

For Docker, set `DB_NAME=/app/data/smotrbot.db` in `.env` so the database is on the mounted volume.

## 4. Telegram permissions

The bot needs the permissions required by the enabled features: access to the moderation/admin chats, publication rights in the target channel, and access to the discussion chat if comments are enabled.

## 5. First launch

Watch the logs for:

- database initialization;
- local AI availability;
- OCR/Tesseract availability when photos are enabled;
- successful polling start.

Send a test post and verify the complete flow: moderation → publication → first comment → deletion request.

## Экономия RAM

По умолчанию включён `LOW_MEMORY_MODE=true`: NudeNet загружается только на время
проверки фото и освобождается после анализа. Локальный ИИ загружается лениво,
а не при старте, и по умолчанию используется компактная
`paraphrase-multilingual-MiniLM-L3-v2`. `LOCAL_AI_PRELOAD=false` оставляйте на
небольшом VPS. Одновременные AI-инференсы сериализованы, чтобы несколько
пользователей не создавали большой пик памяти.
