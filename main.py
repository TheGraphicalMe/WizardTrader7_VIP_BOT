import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
from sqlalchemy.orm import Session
from telegram import Update

from config import (
    TELEGRAM_BOT_TOKEN, APP_BASE_URL,
    BROKER_WEBHOOK_SECRETS, SUPPORTED_BROKERS, ADMIN_PASSWORD
)
from database import init_db, get_db, BrokerAccount, TelegramMember, RawWebhookEvent
from bot import build_app
import hashlib
import json
from google_sheets import trigger_sheet_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# Build bot application once at module level
_bot_app = build_app()

async def winpro_sync_task():
    """Background task to sync Winpro accounts every 3 hours."""
    from winpro import sync_all_winpro_accounts
    from database import SessionLocal
    while True:
        try:
            db = SessionLocal()
            try:
                await sync_all_winpro_accounts(db)
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Error in Winpro sync background task: {e}")
        
        # Sleep for 3 hours (10800 seconds)
        await asyncio.sleep(10800)


# ─── Startup validation ───────────────────────────────────────────────────────

def _validate_config():
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not APP_BASE_URL or APP_BASE_URL == "http://localhost:8000":
        logger.warning("APP_BASE_URL is localhost — Telegram webhook won't work until deployed.")
    if not any(BROKER_WEBHOOK_SECRETS.values()):
        missing.append("At least one BROKER_WEBHOOK_SECRET (EXNESS / DELTA / XM)")
    if missing:
        raise RuntimeError(
            "\n\n❌ Missing required environment variables:\n"
            + "\n".join(f"   • {v}" for v in missing)
            + "\n\nFill these in your .env / Railway Variables and restart.\n"
        )


# Create a secure token deterministically to support multi-worker setups
WEBHOOK_SECRET_TOKEN = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).hexdigest()

@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_config()
    init_db()
    await _bot_app.initialize()
    await _bot_app.start()

    # Start the Winpro background sync task
    sync_task = asyncio.create_task(winpro_sync_task())

    # Register Telegram webhook or start polling if local
    webhook_url = f"{APP_BASE_URL}/webhook/telegram"
    if APP_BASE_URL.startswith("http://localhost") or APP_BASE_URL.startswith("http://127.0.0.1"):
        logger.info("Local environment detected. Falling back to polling mode...")
        await _bot_app.bot.delete_webhook()
        await _bot_app.updater.start_polling(drop_pending_updates=True)
    else:
        try:
            await _bot_app.bot.set_webhook(
                url=webhook_url, 
                secret_token=WEBHOOK_SECRET_TOKEN, 
                drop_pending_updates=False
            )
            logger.info(f"✅ Telegram webhook set → {webhook_url}")
        except Exception as e:
            logger.warning(f"Could not set Telegram webhook. Falling back to polling mode. Error: {e}")
            await _bot_app.bot.delete_webhook()
            await _bot_app.updater.start_polling(drop_pending_updates=True)

    logger.info("✅ Server ready.")
    yield

    if _bot_app.updater and _bot_app.updater.running:
        await _bot_app.updater.stop()
    await _bot_app.stop()
    await _bot_app.shutdown()
    
    # Cancel the background task on shutdown
    sync_task.cancel()


app = FastAPI(
    title="Active Traders Community Bot — Multi-Broker",
    description="Verifies broker accounts and adds users to Active Traders Community Telegram group",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ═════════════════════════════════════════════════════════════════════════════
# 1.  TELEGRAM BOT WEBHOOK
#     Telegram sends every user message / button tap to this endpoint.
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    # 1. Verify that the request is actually from Telegram
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not token or not secrets.compare_digest(token, WEBHOOK_SECRET_TOKEN):
        logger.warning("Unauthorized webhook access attempt")
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        data   = await request.json()
        update = Update.de_json(data, _bot_app.bot)
        await _bot_app.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Telegram webhook error: {e}", exc_info=True)
        # Avoid leaking exact error message in response for security
        return {"ok": False, "error": "Internal Server Error"}


# ═════════════════════════════════════════════════════════════════════════════
# 2.  BROKER AFFILIATE POSTBACKS
#
#     Configure ONE postback URL per broker in their affiliate dashboard:
#
#     Exness:
#       https://yourdomain.com/webhook/broker?broker=exness&secret=EXNESS_SECRET&account_id={ACCOUNT_ID}&email={EMAIL}
#
#     Delta / XM — same pattern, change broker= and secret=
# ═════════════════════════════════════════════════════════════════════════════

from sqlalchemy.exc import IntegrityError

async def _store_account(broker: str, account_id: str, secret: str, email: str, db: Session, extra_data: dict = None):
    """Shared logic for GET and POST broker postbacks."""
    broker = (broker or "").strip().lower()

    if broker not in SUPPORTED_BROKERS:
        raise HTTPException(status_code=400,
            detail=f"Unknown broker '{broker}'. Supported: {SUPPORTED_BROKERS}")

    # Skip all authentication/secret checks for XM
    if broker != "xm":
        expected = BROKER_WEBHOOK_SECRETS.get(broker, "")
        if not expected:
            raise HTTPException(status_code=500,
                detail=f"Webhook secret for '{broker}' is not configured on the server.")
        if not secret or not secrets.compare_digest(secret, expected):
            logger.warning(f"Bad secret for broker={broker}")
            raise HTTPException(status_code=403, detail="Invalid secret token.")
            
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id is required.")

    exists = db.query(BrokerAccount).filter(
        BrokerAccount.account_id == account_id,
        BrokerAccount.broker     == broker,
    ).first()

    if exists:
        logger.info(f"[{broker}] {account_id} already in DB — skipped.")
        return {"status": "already_exists", "broker": broker, "account_id": account_id}

    db.add(BrokerAccount(account_id=account_id, broker=broker, client_email=email))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.info(f"[{broker}] {account_id} already in DB (race condition handled) — skipped.")
        return {"status": "already_exists", "broker": broker, "account_id": account_id}
        
    logger.info(f"✅ [{broker}] New account stored: {account_id}")
    trigger_sheet_sync(broker, account_id, email, extra_data=extra_data)
    return {"status": "success", "broker": broker, "account_id": account_id}


async def dump_raw_webhook(request: Request, db: Session):
    try:
        raw_body_bytes = await request.body()
        raw_body = raw_body_bytes.decode('utf-8', errors='replace')
    except Exception:
        raw_body = ""
        
    try:
        headers_dict = dict(request.headers)
        headers_str = json.dumps(headers_dict)
    except Exception:
        headers_str = str(request.headers)
        
    try:
        query_params_dict = dict(request.query_params)
        query_params_str = json.dumps(query_params_dict)
    except Exception:
        query_params_str = str(request.query_params)

    event = RawWebhookEvent(
        method=request.method,
        url=str(request.url),
        headers=headers_str,
        query_params=query_params_str,
        raw_body=raw_body
    )
    db.add(event)
    try:
        db.commit()
    except Exception as e:
        logger.error(f"Failed to log raw webhook: {e}")
        db.rollback()

@app.get("/webhook/broker")
async def broker_postback_get(request: Request, db: Session = Depends(get_db)):
    await dump_raw_webhook(request, db)
    p = request.query_params
    
    try:
        raw_account_id = (
            p.get("account_id") or 
            p.get("accountId") or 
            p.get("traderLoginId", "")
        )
        return await _store_account(
            broker     = p.get("broker", ""),
            account_id = str(raw_account_id) if raw_account_id else "",
            secret     = p.get("secret", ""),
            email      = p.get("email", ""),
            db         = db,
            extra_data = dict(p)
        )
    except HTTPException as e:
        logger.error(f"Broker GET webhook validation failed: {e.detail}")
        return {"status": "received_but_invalid", "detail": e.detail}


@app.post("/webhook/broker")
async def broker_postback_post(request: Request, db: Session = Depends(get_db)):
    await dump_raw_webhook(request, db)
    
    body = {}
    raw_body_bytes = await request.body()
    # Attempt 1: Raw JSON parse independent of headers
    try:
        if raw_body_bytes:
            import json
            body = json.loads(raw_body_bytes.decode('utf-8', errors='ignore'))
    except Exception:
        # Attempt 2: Form encoding
        try:
            form = await request.form()
            body = dict(form)
        except Exception:
            body = {}
    p = request.query_params
    
    try:
        # XM sends the account ID inside a nested 'data' object.
        # We try to extract account ID from multiple possible fields to support multiple brokers.
        xm_data = body.get("data", {}) if isinstance(body, dict) else {}
        xm_trader_id = xm_data.get("traderLoginId") if isinstance(xm_data, dict) else None
        
        raw_account_id = (
            body.get("account_id") or 
            body.get("accountId") or 
            xm_trader_id or 
            body.get("traderLoginId") or 
            p.get("account_id", "")
        )
        
        # Auto-detect broker logic
        # If no broker query param is given, but we see 'traderLoginId' (nested or flat), assume it's XM
        inferred_broker = body.get("broker") or p.get("broker", "")
        inferred_secret = body.get("secret") or p.get("secret", "")
        
        if not inferred_broker and (xm_trader_id or body.get("traderLoginId")):
            inferred_broker = "xm"
            # Auto-inject the expected secret to bypass validation since XM didn't provide one
            # Secure this later via IP whitelisting when the logs confirm their IPs
            if not inferred_secret:
                inferred_secret = BROKER_WEBHOOK_SECRETS.get("xm", "")

        return await _store_account(
            broker     = inferred_broker,
            account_id = str(raw_account_id) if raw_account_id else "",
            secret     = inferred_secret,
            email      = body.get("email")      or p.get("email", ""),
            db         = db,
            extra_data = body or dict(p)
        )
    except HTTPException as e:
        logger.error(f"Broker POST webhook validation failed: {e.detail}")
        return {"status": "received_but_invalid", "detail": e.detail}


# ═════════════════════════════════════════════════════════════════════════════
# 3.  ADMIN ENDPOINTS
#     Add a password check here before going live in production.
# ═════════════════════════════════════════════════════════════════════════════

security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@app.get("/admin/accounts")
async def list_accounts(broker: str | None = None, db: Session = Depends(get_db), admin: str = Depends(verify_admin)):
    """All stored broker accounts. Filter with ?broker=exness"""
    q = db.query(BrokerAccount)
    if broker:
        q = q.filter(BrokerAccount.broker == broker.lower())
    return [
        {
            "broker":        a.broker,
            "account_id":    a.account_id,
            "email":         a.client_email,
            "registered_at": a.registered_at,
            "is_claimed":    a.is_claimed,
            "claimed_by":    a.claimed_by_telegram_id,
            "claimed_at":    a.claimed_at,
        }
        for a in q.order_by(BrokerAccount.registered_at.desc()).all()
    ]


@app.get("/admin/members")
async def list_members(broker: str | None = None, db: Session = Depends(get_db), admin: str = Depends(verify_admin)):
    """All community members. Filter with ?broker=exness"""
    q = db.query(TelegramMember)
    if broker:
        q = q.filter(TelegramMember.broker == broker.lower())
    return [
        {
            "broker":    m.broker,
            "telegram_id":       m.telegram_id,
            "telegram_username": m.telegram_username,
            "full_name":         m.full_name,
            "account_id":        m.account_id,
            "joined_at":         m.joined_at,
            "is_active":         m.is_active,
        }
        for m in q.order_by(TelegramMember.joined_at.desc()).all()
    ]


@app.post("/admin/add-account")
async def manually_add_account(
    account_id: str,
    broker:     str,
    email:      str | None = None,
    db: Session = Depends(get_db),
    admin: str = Depends(verify_admin),
):
    """Manually whitelist a broker account (for testing or pre-existing accounts)."""
    broker = broker.lower()
    if broker not in SUPPORTED_BROKERS:
        raise HTTPException(status_code=400, detail=f"Unknown broker '{broker}'.")
    if db.query(BrokerAccount).filter(
        BrokerAccount.account_id == account_id,
        BrokerAccount.broker     == broker,
    ).first():
        return {"status": "already_exists"}
    db.add(BrokerAccount(account_id=account_id, broker=broker, client_email=email))
    db.commit()
    logger.info(f"Manually added: broker={broker} account={account_id}")
    return {"status": "added", "broker": broker, "account_id": account_id}


# ═════════════════════════════════════════════════════════════════════════════
# 4.  HEALTH CHECK
#     Render pings GET or HEAD on the health-check path every few seconds.
#     A failed check (non-2xx) causes Render to restart the service.
# ═════════════════════════════════════════════════════════════════════════════

from fastapi.responses import JSONResponse, Response


@app.get("/health")
@app.head("/health")
@app.head("/")
async def health_check():
    """Ultra-lightweight probe — no external API calls."""
    return JSONResponse({"status": "ok"})


@app.get("/")
async def root():
    return {
        "service":           "Active Traders Community Bot — Multi-Broker",
        "status":            "running",
        "supported_brokers": SUPPORTED_BROKERS,
        "bot_username":      (await _bot_app.bot.get_me()).username,
        "endpoints": {
            "telegram_webhook": "POST /webhook/telegram",
            "broker_postback":  "GET  /webhook/broker?broker=exness&secret=X&account_id=Y",
            "admin_accounts":   "GET  /admin/accounts?broker=exness",
            "admin_members":    "GET  /admin/members",
            "manual_add":       "POST /admin/add-account?broker=delta&account_id=Z",
        },
    }
