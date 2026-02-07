import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")

MAIN_CHANNEL_ID = int(os.getenv("MAIN_CHANNEL_ID", "-1002120185316"))
COMMENTS_CHAT_ID = int(os.getenv("COMMENTS_CHAT_ID", "0"))

MODERATORS_CHAT_ID = int(os.getenv("MODERATORS_CHAT_ID", "-1002156153932"))
MODERATORS_TOPIC_ID = int(os.getenv("MODERATORS_TOPIC_ID", "70619"))

ADMINS_CHAT_ID = int(os.getenv("ADMINS_CHAT_ID", "-1002104468174"))
ADMINS_TOPIC_ID = int(os.getenv("ADMINS_TOPIC_ID", "76774"))

# Parse admin IDs from comma-separated string
ADMINS_STR = os.getenv("ADMINS", "6702947726,1171717255")
ADMINS = [int(uid.strip()) for uid in ADMINS_STR.split(",") if uid.strip()] if ADMINS_STR else []
MODERATORS = []

DB_NAME = os.getenv("DB_NAME", "smotrbot.db")

# ================== REQUIRED SUBSCRIPTIONS ==================
REQUIRED_SUBSCRIPTIONS = []