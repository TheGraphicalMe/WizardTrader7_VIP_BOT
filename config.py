import os
from datetime import date
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

# ── Vantage Inactivity Kick ─────────────────────────────────────────────────
# Same rules as XM; runs daily at 01:00 IST. Activity = a trade on any account of the Vantage client.
VANTAGE_INACTIVITY_DAYS = int(os.getenv("VANTAGE_INACTIVITY_DAYS", "15"))
VANTAGE_KICK_START_DATE = date.fromisoformat(os.getenv("VANTAGE_KICK_START_DATE", "2026-09-18").strip())
VANTAGE_REMINDER_DAYS = sorted(
    d for d in (int(x) for x in os.getenv("VANTAGE_REMINDER_DAYS", "7,12,15").split(",") if x.strip())
    if 0 < d <= VANTAGE_INACTIVITY_DAYS
)
# When true, the daily job only reports — no reminders sent, nobody removed.
VANTAGE_KICK_DRY_RUN   = os.getenv("VANTAGE_KICK_DRY_RUN", "false").strip().lower() == "true"
VANTAGE_KICK_MAX_RATIO = float(os.getenv("VANTAGE_KICK_MAX_RATIO", "0.9"))

# Winpro API Credentials
WINPRO_API_KEY = os.getenv("WINPRO_API_KEY", "")
# Cumulative successful deposits (USD) an MT5 account needs before it can claim VIP access.
WINPRO_MIN_DEPOSIT_USD = float(os.getenv("WINPRO_MIN_DEPOSIT_USD", "50"))
# The MT5 history endpoint rejects a request that omits master_password, but never checks the
# value — the IB relationship is what the server actually verifies (it returns belongs_to_ib),
# exactly as the API docs describe. Any non-empty string satisfies the field. If Winpro ever
# starts validating it, history reads fail closed and the kick skips those members instead of
# removing them, and a real value can be supplied here.
WINPRO_MASTER_PASSWORD = os.getenv("WINPRO_MASTER_PASSWORD", "-").strip() or "-"

# ── Winpro Inactivity Kick ──────────────────────────────────────────────────
# Same rules as XM; runs daily at 01:00 IST. Activity = a closed deal in the account's MT5 history.
WINPRO_INACTIVITY_DAYS = int(os.getenv("WINPRO_INACTIVITY_DAYS", "15"))
WINPRO_KICK_START_DATE = date.fromisoformat(os.getenv("WINPRO_KICK_START_DATE", "2026-09-18").strip())
WINPRO_REMINDER_DAYS = sorted(
    d for d in (int(x) for x in os.getenv("WINPRO_REMINDER_DAYS", "7,12,15").split(",") if x.strip())
    if 0 < d <= WINPRO_INACTIVITY_DAYS
)
# When true, the daily job only reports — no reminders sent, nobody removed.
WINPRO_KICK_DRY_RUN   = os.getenv("WINPRO_KICK_DRY_RUN", "false").strip().lower() == "true"
WINPRO_KICK_MAX_RATIO = float(os.getenv("WINPRO_KICK_MAX_RATIO", "0.9"))

# XM Partners API (Trade Statistics) — used for the inactivity kick
XM_API_KEY = os.getenv("XM_API_KEY", "").strip()

# ── XM Inactivity Kick ──────────────────────────────────────────────────────
# Members with no XM trades for XM_INACTIVITY_DAYS are removed daily at 01:00 IST.
XM_INACTIVITY_DAYS = int(os.getenv("XM_INACTIVITY_DAYS", "15"))
# Everyone's inactivity clock starts no earlier than this date (fresh start for existing members).
XM_KICK_START_DATE = date.fromisoformat(os.getenv("XM_KICK_START_DATE", "2026-09-18").strip())
# Days without trading at which a reminder DM is sent.
XM_REMINDER_DAYS = sorted(
    d for d in (int(x) for x in os.getenv("XM_REMINDER_DAYS", "7,12,15").split(",") if x.strip())
    if 0 < d <= XM_INACTIVITY_DAYS
)
# When true, the daily job only reports — no reminders sent, nobody removed.
XM_KICK_DRY_RUN = os.getenv("XM_KICK_DRY_RUN", "false").strip().lower() == "true"
# Abort the run (kick nobody) if more than this share of members would be kicked at once.
XM_KICK_MAX_RATIO = float(os.getenv("XM_KICK_MAX_RATIO", "0.9"))
# Comma-separated Telegram user IDs that receive the daily report.
INACTIVITY_REPORT_TELEGRAM_IDS = [
    x.strip() for x in os.getenv("INACTIVITY_REPORT_TELEGRAM_IDS", "").split(",") if x.strip()
]
# Comma-separated Telegram user IDs and/or @usernames that are never reminded and never
# removed (admins, staff). Applies to every broker's inactivity check.
# Example: KICK_EXEMPT_TELEGRAM_IDS=123456789,@harshit,another_user
_exempt = [x.strip() for x in os.getenv("KICK_EXEMPT_TELEGRAM_IDS", "").split(",") if x.strip()]
KICK_EXEMPT_TELEGRAM_IDS = {x for x in _exempt if x.isdigit()}
KICK_EXEMPT_USERNAMES    = {x.lstrip("@").lower() for x in _exempt if not x.isdigit()}

# ── Exness Affiliates API ────────────────────────────────────────────────────
# Partner-area login, used to verify MT5 accounts + emails and for the inactivity kick.
EXNESS_LOGIN    = os.getenv("EXNESS_LOGIN", "").strip()
EXNESS_PASSWORD = os.getenv("EXNESS_PASSWORD", "").strip()

# ── Exness Inactivity Kick ──────────────────────────────────────────────────
# Same rules as XM; runs daily at 01:00 IST. Activity = a trade on any account of the Exness client.
EXNESS_INACTIVITY_DAYS = int(os.getenv("EXNESS_INACTIVITY_DAYS", "15"))
EXNESS_KICK_START_DATE = date.fromisoformat(os.getenv("EXNESS_KICK_START_DATE", "2026-09-18").strip())
EXNESS_REMINDER_DAYS = sorted(
    d for d in (int(x) for x in os.getenv("EXNESS_REMINDER_DAYS", "7,12,15").split(",") if x.strip())
    if 0 < d <= EXNESS_INACTIVITY_DAYS
)
# When true, the daily job only reports — no reminders sent, nobody removed.
EXNESS_KICK_DRY_RUN   = os.getenv("EXNESS_KICK_DRY_RUN", "false").strip().lower() == "true"
EXNESS_KICK_MAX_RATIO = float(os.getenv("EXNESS_KICK_MAX_RATIO", "0.9"))

# Fixie Proxy for external API requests
FIXIE_URL       = os.getenv("FIXIE_URL", "")

# ── Form Links ──────────────────────────────────────────────────────────────
SMART_AI_FORM_URL = os.getenv("SMART_AI_FORM_URL", "https://forms.gle/KVspdfgTRdcGXQLL9")
SMART_AI_WEBSITE_URL = os.getenv("SMART_AI_WEBSITE_URL", "https://www.smartaitradingpro.com/")

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
    "exness": {
        "name": "Exness",
        "link": "https://one.exnessonelink.com/a/xume42lkdk",
        "code": "xume42lkdk",
    },
}

# Canonical list of supported broker slugs (Delta temporarily removed)
SUPPORTED_BROKERS = ["xm"]
if VANTAGE_USER_ID and VANTAGE_SECRET:
    SUPPORTED_BROKERS.append("vantage")
if EXNESS_LOGIN and EXNESS_PASSWORD:
    SUPPORTED_BROKERS.append("exness")
if WINPRO_API_KEY:
    SUPPORTED_BROKERS.append("winpro")

# ── App ───────────────────────────────────────────────────────────────────────
APP_BASE_URL  = os.getenv("APP_BASE_URL", "http://localhost:8000")
DATABASE_URL  = os.getenv("DATABASE_URL", "sqlite:///./vip_bot.db")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
ALLOWED_USERS  = os.getenv("ALLOWED_USERS", "")