import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")

MAIN_CHANNEL_ID = int(os.getenv("MAIN_CHANNEL_ID", "0")) if os.getenv("MAIN_CHANNEL_ID") else 0
COMMENTS_CHAT_ID = int(os.getenv("COMMENTS_CHAT_ID", "0")) if os.getenv("COMMENTS_CHAT_ID") else 0

MODERATORS_CHAT_ID = int(os.getenv("MODERATORS_CHAT_ID", "0")) if os.getenv("MODERATORS_CHAT_ID") else 0
MODERATORS_TOPIC_ID = int(os.getenv("MODERATORS_TOPIC_ID", "0")) if os.getenv("MODERATORS_TOPIC_ID") else 0

ADMINS_CHAT_ID = int(os.getenv("ADMINS_CHAT_ID", "0")) if os.getenv("ADMINS_CHAT_ID") else 0
ADMINS_TOPIC_ID = int(os.getenv("ADMINS_TOPIC_ID", "0")) if os.getenv("ADMINS_TOPIC_ID") else 0

# Parse admin IDs from comma-separated string
ADMINS_STR = os.getenv("ADMINS", "")
ADMINS = [int(uid.strip()) for uid in ADMINS_STR.split(",") if uid.strip()] if ADMINS_STR else []
MODERATORS = []

DB_NAME = os.getenv("DB_NAME", "smotrbot.db")

# ================== REQUIRED SUBSCRIPTIONS ==================
REQUIRED_SUBSCRIPTIONS = []