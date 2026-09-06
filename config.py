import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ── Telegram Bot ──────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_GROUP_ID  = os.getenv("TELEGRAM_GROUP_ID", "")

# ── Google Sheets Webhook ───────────────────────────────────────────────────
GOOGLE_SHEETS_WEBHOOK_URL = os.getenv("GOOGLE_SHEETS_WEBHOOK_URL", "")

# ── Per-Broker Postback Secrets ───────────────────────────────────────────────
BROKER_WEBHOOK_SECRETS: dict[str, str] = {
    "exness":  os.getenv("EXNESS_WEBHOOK_SECRET",  ""),
    "delta":   os.getenv("DELTA_WEBHOOK_SECRET",   ""),
    "xm":      os.getenv("XM_WEBHOOK_SECRET",      ""),
}

# Vantage API Credentials
VANTAGE_USER_ID = os.getenv("VANTAGE_USER_ID", "")
VANTAGE_SECRET  = os.getenv("VANTAGE_SECRET", "")

# Winpro API Credentials
WINPRO_API_KEY = os.getenv("WINPRO_API_KEY", "")

# Fixie Proxy for external API requests
FIXIE_URL       = os.getenv("FIXIE_URL", "")

# ── Form Links ──────────────────────────────────────────────────────────────
SMART_AI_FORM_URL = os.getenv("SMART_AI_FORM_URL", "https://forms.gle/KVspdfgTRdcGXQLL9")

# ── Partner Links & Codes ───────────────────────────────────────────────────
BROKER_AFFILIATE_INFO = {
    "winpro": {
        "name": "Winpro",
        "link": "https://my.winprofx.org/register?promo=Harshitpatel",
        "code": "Harshitpatel",
    },
    "vantage": {
        "name": "Vantage",
        "link": "https://vigco.co/la-com-inv/WIZARDTRADER",
        "code": "WIZARDTRADER",
    },
    "xm": {
        "name": "XM",
        "link": "https://affs.click/DDB1D",
        "code": "WIZARTRADER",
    },
}

# Canonical list of supported broker slugs (Exness and Delta temporarily removed)
SUPPORTED_BROKERS = ["xm"]
if VANTAGE_USER_ID and VANTAGE_SECRET:
    SUPPORTED_BROKERS.append("vantage")
if WINPRO_API_KEY:
    SUPPORTED_BROKERS.append("winpro")

# ── App ───────────────────────────────────────────────────────────────────────
APP_BASE_URL  = os.getenv("APP_BASE_URL", "http://localhost:8000")
DATABASE_URL  = os.getenv("DATABASE_URL", "sqlite:///./vip_bot.db")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
ALLOWED_USERS  = os.getenv("ALLOWED_USERS", "")