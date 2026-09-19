"""
winpro.py
─────────
Winpro IB API (clientapi.winpro.finance), account verification and the daily inactivity check.

Verification (called from bot.py):
  1. the MT5 account must sit under our IB
  2. its successful deposits must reach WINPRO_MIN_DEPOSIT_USD

Every day at 01:00 IST (see kick_scheduler.py), for each active Winpro member:
  • activity = the most recent closed deal in the account's MT5 history
  • at WINPRO_REMINDER_DAYS (7 / 12 / 15) they get a reminder DM
  • after WINPRO_INACTIVITY_DAYS (15), i.e. on day 16, they are removed (ban + immediate unban)
A report is sent to INACTIVITY_REPORT_TELEGRAM_IDS.

Notes on the trade feed, which differs from the published API docs:
  • /history is POST, not GET
  • it requires a master_password field but never validates it — authorisation is the IB API key,
    and the response carries belongs_to_ib, as the docs describe
  • from/to are accepted and then ignored, so the window is applied here, not by the API
  • rows come back newest-first, 50 per page, as MT5 orders with a state field; only executed
    orders (state 3/4) count, never canceled ones (state 2)

A history read that fails never counts as "no trades": that member is skipped for the run, and a
run where no member could be read aborts outright rather than emptying the group.
"""

import asyncio
import html
import logging
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from telegram import Bot
from telegram.error import TelegramError

from bot import _group_id
from config import (
    WINPRO_API_KEY, WINPRO_MIN_DEPOSIT_USD, WINPRO_MASTER_PASSWORD,
    WINPRO_INACTIVITY_DAYS, WINPRO_KICK_START_DATE, WINPRO_REMINDER_DAYS,
    WINPRO_KICK_DRY_RUN, WINPRO_KICK_MAX_RATIO,
)
from database import SessionLocal, BrokerAccount, TelegramMember, get_ist_time
from google_sheets import trigger_sheet_sync
from xm import _plural, _describe, _remove_member, _send_report, is_kick_exempt

logger = logging.getLogger(__name__)

WINPRO_BASE_URL = "https://clientapi.winpro.finance/api"
JOB_NAME        = "winpro_inactivity_kick"   # daily run is scheduled by kick_scheduler.py

# Below this many members the max-ratio safety check is skipped (small groups swing wildly).
MIN_MEMBERS_FOR_RATIO_CHECK = 10
# History is fetched one account at a time; keep a few in flight without hammering the API.
HISTORY_CONCURRENCY = 5

async def verify_winpro_account(account_id: str, db: Session) -> tuple[bool, str]:
    """
    Checks if the account is under our IB, then that its successful deposits reach
    WINPRO_MIN_DEPOSIT_USD.

    This is the authoritative gate and runs on every claim attempt — a row in
    broker_accounts proves nothing on its own, because sync_all_winpro_accounts()
    stores every account under the IB regardless of how much it has deposited.

    Returns: (is_valid: bool, reason: str)
    """
    if not WINPRO_API_KEY:
        logger.error("WINPRO_API_KEY not configured")
        return False, "api_error"

    if not account_id.isdigit():
        return False, "invalid_format"

    async with httpx.AsyncClient() as client:
        # ── Step 1: Check if the account belongs to the IB ────────────
        try:
            ib_check_res = await client.get(
                f"{WINPRO_BASE_URL}/v1/brokers/mt5-accounts",
                params={"search": account_id},
                headers={"X-API-KEY": WINPRO_API_KEY, "Accept": "application/json"},
                timeout=30.0
            )
            ib_check_res.raise_for_status()
            ib_data = ib_check_res.json()
            
            if not ib_data.get("ok"):
                return False, "api_error"
            
            items = ib_data.get("data", {}).get("items", [])
            
            # Verify the exact account_id is in the returned list
            matched = next((i for i in items if str(i.get("mt5_id")) == str(account_id)), None)
            if not matched:
                return False, "not_under_ib"
            client_uid = str(matched.get("client_id") or "") or None
            client_email = (matched.get("client_email") or "").strip()
            client_name = (matched.get("client_name") or "").strip()

        except Exception as e:
            logger.error(f"Error checking IB status on Winpro API: {e}")
            return False, "api_error"

        # ── Step 2: Check total successful deposits ───────────────────
        total_deposits = 0.0
        page = 1
        
        while True:
            try:
                response = await client.get(
                    f"{WINPRO_BASE_URL}/v1/brokers/deposits",
                    params={"mt5_id": account_id, "page": page, "per_page": 100},
                    headers={"X-API-KEY": WINPRO_API_KEY, "Accept": "application/json"},
                    timeout=30.0
                )
                response.raise_for_status()
                data = response.json()

                if not data.get("ok"):
                    return False, "api_error"

                items = data.get("data", {}).get("items", [])
                if not items:
                    break

                for item in items:
                    total_deposits += float(item.get("amount", 0))
                    if not client_email and item.get("client_email"):
                        client_email = item.get("client_email")
                    if not client_name and item.get("client_name"):
                        client_name = item.get("client_name")

                pagination = data.get("data", {}).get("pagination", {})
                if page >= pagination.get("total_pages", 1):
                    break
                    
                page += 1

            except Exception as e:
                logger.error(f"Error fetching Winpro deposits: {e}")
                return False, "api_error"

    logger.info(f"[winpro] Account {account_id} is under IB. Total deposits: ${total_deposits}")

    if total_deposits >= WINPRO_MIN_DEPOSIT_USD:
        db_account = BrokerAccount(
            account_id=str(account_id),
            broker="winpro",
            client_email=client_email or client_name,
            client_uid=client_uid,
            mt5_id=str(account_id),
        )
        db.add(db_account)
        try:
            db.commit()
            trigger_sheet_sync("winpro", str(account_id), client_email or client_name,
                               extra_data={"client_name": client_name, "client_id": client_uid or ""},
                               client_uid=client_uid or "", mt5_id=str(account_id))
        except IntegrityError:
            db.rollback()
        return True, "success"
    else:
        return False, f"insufficient_deposit:{total_deposits}"


async def sync_all_winpro_accounts(db: Session):
    """
    Fetch all MT5 accounts from Winpro and save new ones to the DB.
    """
    if not WINPRO_API_KEY:
        return

    page = 1
    per_page = 100
    
    async with httpx.AsyncClient() as client:
        while True:
            try:
                response = await client.get(
                    f"{WINPRO_BASE_URL}/v1/brokers/mt5-accounts",
                    params={"page": page, "per_page": per_page},
                    headers={"X-API-KEY": WINPRO_API_KEY, "Accept": "application/json"},
                    timeout=30.0
                )
                response.raise_for_status()
                data = response.json()
                
                if not data.get("ok"):
                    logger.error(f"Failed to sync Winpro accounts: {data.get('message')}")
                    break
                    
                items = data.get("data", {}).get("items", [])
                
                for item in items:
                    account_id = str(item.get("mt5_id"))
                    if not account_id or account_id == "None":
                        continue
                        
                    client_email = item.get("client_email")
                    client_name = item.get("client_name")
                    email_to_save = client_email or client_name or ""
                    
                    db_account = BrokerAccount(
                        account_id=account_id,
                        broker="winpro",
                        client_email=email_to_save,
                        client_uid=str(item.get("client_id") or "") or None,
                        mt5_id=account_id,
                    )
                    db.add(db_account)
                    try:
                        db.commit()
                        logger.info(f"✅ [winpro] Automatically synced new account: {account_id}")
                        trigger_sheet_sync("winpro", account_id, email_to_save, extra_data=item,
                                           client_uid=str(item.get("client_id") or ""), mt5_id=account_id)
                    except IntegrityError:
                        db.rollback()
                        
                pagination = data.get("data", {}).get("pagination", {})
                if page >= pagination.get("total_pages", 1):
                    break
                    
                page += 1
                
            except Exception as e:
                logger.error(f"Error in sync_all_winpro_accounts: {e}")
                break

# ═════════════════════════════════════════════════════════════════════════════
# MT5 TRADE HISTORY
# ═════════════════════════════════════════════════════════════════════════════

# Keys the mt5feeder payload has been seen (or documented) to nest the order list under.
# Live responses use result.data, but the others are kept so a reshaped payload still parses.
_DEAL_LIST_KEYS = ("history", "deals", "trades", "items", "data", "result")
# Time fields on an order, most specific first. Live rows carry time_done (execution time).
_DEAL_TIME_KEYS = ("time_done", "close_time", "closeTime", "time_close", "time_setup",
                   "time", "open_time", "openTime", "date")
# MT5 order states that mean the order actually executed. Live data also contains state 2
# (canceled), which must not count as trading activity.
_EXECUTED_STATES = {"3", "4"}   # 3 = partially filled, 4 = filled


def _parse_deal_time(value) -> datetime | None:
    """Deal timestamps arrive as epoch seconds or as an ISO 8601 / 'Y-m-d H:i:s' string."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit():
        try:
            return datetime.utcfromtimestamp(int(text))
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _extract_deals(result) -> list:
    """Pulls the deal list out of mt5feeder's response, whatever level it is nested at."""
    if isinstance(result, list):
        return result
    if not isinstance(result, dict):
        return []
    for key in _DEAL_LIST_KEYS:
        value = result.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):          # e.g. {"history": {"deals": [...]}}
            nested = _extract_deals(value)
            if nested:
                return nested
    return []


def _is_trade_deal(deal: dict) -> bool:
    """
    True only for orders that actually executed.

    Canceled/rejected/expired orders and balance operations (deposits, withdrawals, credits)
    are not trading activity — placing and pulling an order must not keep a spot.
    """
    if not isinstance(deal, dict):
        return False
    if "symbol" in deal and not str(deal.get("symbol") or "").strip():
        return False
    if "state" in deal:
        return str(deal.get("state")).strip() in _EXECUTED_STATES
    return True


def _latest_deal_time(result) -> datetime | None:
    latest = None
    for deal in _extract_deals(result):
        if not _is_trade_deal(deal):
            continue
        ts = next((t for t in (_parse_deal_time(deal.get(k)) for k in _DEAL_TIME_KEYS) if t), None)
        if ts and (latest is None or ts > latest):
            latest = ts
    return latest


async def fetch_last_trade(client: httpx.AsyncClient, mt5_id: str, start: date, end: date) -> tuple[bool, datetime | None]:
    """
    Reads one account's MT5 order history and returns its most recent executed order.

    Returns (readable, last_trade_time). readable=False means the API would not tell us
    (auth, master password, IB mismatch, network) — the caller must NOT read that as "no trades".
    readable=True with last_trade_time=None is a real answer: this account has never traded.
    """
    # from/to are sent as documented, but the live endpoint ignores them and always returns the
    # full history newest-first, so the window is applied here rather than trusted to the API.
    # That also means page 1 is enough: the newest executed order is the last trade.
    payload = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "master_password": WINPRO_MASTER_PASSWORD,
    }

    try:
        # Documented as GET with a query string, but the live route only accepts POST;
        # the window is sent both ways so it keeps working whichever the API settles on.
        response = await client.post(
            f"{WINPRO_BASE_URL}/v1/brokers/account/{mt5_id}/history",
            params={"from": start.isoformat(), "to": end.isoformat()},
            json=payload,
            headers={"X-API-KEY": WINPRO_API_KEY, "Accept": "application/json"},
            timeout=60.0,
        )
    except Exception as e:
        logger.warning(f"[winpro-kick] History call failed for {mt5_id}: {e}")
        return False, None

    # The API explains itself in the body even on a 4xx (422 + "master_password is required."),
    # so the body is read before the status is judged.
    try:
        data = response.json()
    except ValueError:
        logger.warning(f"[winpro-kick] History for {mt5_id}: HTTP {response.status_code}, non-JSON response")
        return False, None

    if response.status_code >= 400 or not data.get("ok"):
        logger.warning(
            f"[winpro-kick] History unavailable for {mt5_id} "
            f"(HTTP {response.status_code}): {data.get('message') or data.get('error')}"
        )
        return False, None

    body = data.get("data", {}) or {}
    if not body.get("belongs_to_ib", True):
        logger.info(f"[winpro-kick] Account {mt5_id} is no longer under our IB.")
        return False, None

    return True, _latest_deal_time(body.get("result"))


async def _last_trade_by_member(members: list[TelegramMember], start: date, end: date
                                ) -> tuple[dict[str, datetime], list[TelegramMember]]:
    """
    Maps telegram_id → latest trade time, plus the members whose history could not be read.
    Unreadable members are never counted as inactive.
    """
    last: dict[str, datetime] = {}
    unreadable: list[TelegramMember] = []
    gate = asyncio.Semaphore(HISTORY_CONCURRENCY)

    async with httpx.AsyncClient() as client:
        async def one(member: TelegramMember):
            mt5 = str(member.mt5_id or member.account_id).strip()
            async with gate:
                readable, ts = await fetch_last_trade(client, mt5, start, end)
            if not readable:
                unreadable.append(member)
            elif ts:
                last[member.telegram_id] = ts

        await asyncio.gather(*(one(m) for m in members))

    return last, unreadable


async def check_recent_winpro_trade(mt5_id: str) -> tuple[bool | None, datetime | None]:
    """
    Checks one account for a trade inside the inactivity window.
    Returns (traded, last_trade_time); traded is None if the history could not be read.
    """
    end   = get_ist_time().date()
    start = end - timedelta(days=WINPRO_INACTIVITY_DAYS)
    async with httpx.AsyncClient() as client:
        readable, ts = await fetch_last_trade(client, str(mt5_id), start, end)
    if not readable:
        return None, None
    return (ts is not None), ts


# ═════════════════════════════════════════════════════════════════════════════
# MEMBER MESSAGES
# ═════════════════════════════════════════════════════════════════════════════

def _inactive_days(member: TelegramMember, today: date) -> int:
    """Days since the latest of: last trade, join date, and the programme start date."""
    reference = WINPRO_KICK_START_DATE
    for dt in (member.joined_at, member.last_trade_date):
        if dt and dt.date() > reference:
            reference = dt.date()
    return (today - reference).days


def _reminder_text(member: TelegramMember, days: int) -> str:
    days_left  = max(WINPRO_INACTIVITY_DAYS - days, 1)
    first_name = html.escape((member.full_name or "Trader").split()[0])
    account    = html.escape(str(member.account_id))

    if days_left <= 1:
        headline = "🚨 <b>FINAL WARNING — you'll be removed tomorrow!</b>"
        urgency  = (
            "⛔ If there's still no closed trade by <b>1:00 AM IST tomorrow</b>, "
            "you'll be removed from the community automatically."
        )
    elif days_left <= 5:
        headline = f"⚠️ <b>Only {_plural(days_left, 'day')} left to keep your spot!</b>"
        urgency  = f"⏳ Time is running out — just <b>{_plural(days_left, 'day')}</b> to go."
    else:
        headline = "⏰ <b>Friendly reminder — your spot is waiting for a trade!</b>"
        urgency  = f"⏳ You still have <b>{_plural(days_left, 'day')}</b> — plenty of time, but don't leave it too late."

    return (
        f"{headline}\n\n"
        f"Hey {first_name} 👋\n\n"
        f"We noticed there's been <b>no trading activity</b> on your Winpro account <code>{account}</code> "
        f"for the last <b>{_plural(days, 'day')}</b>.\n\n"
        "The Active Traders Community is for active traders, so members need "
        f"<b>at least one closed trade every {WINPRO_INACTIVITY_DAYS} days</b> to stay in.\n\n"
        f"{urgency}\n\n"
        "✅ <b>How to keep your spot:</b>\n"
        "1️⃣ Log in to your Winpro MT5 account\n"
        "2️⃣ Place a trade\n"
        "3️⃣ Close it — <i>only closed trades count</i>\n\n"
        "That's all it takes! Activity is checked every night at 1:00 AM IST.\n\n"
        "We'd love to keep you with us. Happy trading! 📈"
    )


async def _send_reminder(bot: Bot, member: TelegramMember, days: int) -> bool:
    try:
        await bot.send_message(chat_id=int(member.telegram_id), text=_reminder_text(member, days), parse_mode="HTML")
        return True
    except (TelegramError, ValueError) as e:
        logger.info(f"[winpro-kick] Could not send reminder to {member.telegram_id}: {e}")
        return False


async def _notify_removed(bot: Bot, member: TelegramMember):
    try:
        await bot.send_message(
            chat_id=int(member.telegram_id),
            text=(
                "😔 <b>You have been removed from the Active Traders Community</b>\n\n"
                f"Your Winpro account <code>{html.escape(str(member.account_id))}</code> has had no trading activity "
                f"for {WINPRO_INACTIVITY_DAYS} days.\n\n"
                "<b>Want back in? It's easy:</b>\n"
                "1️⃣ Place and close at least one trade on your Winpro account\n"
                "2️⃣ Wait a few minutes\n"
                "3️⃣ Send /start and verify again\n\n"
                "See you back soon! 📈"
            ),
            parse_mode="HTML",
        )
    except (TelegramError, ValueError):
        pass  # user blocked the bot or never started it


# ═════════════════════════════════════════════════════════════════════════════
# INACTIVITY CHECK
# ═════════════════════════════════════════════════════════════════════════════

async def run_winpro_inactivity_check(bot: Bot, dry_run: bool = WINPRO_KICK_DRY_RUN) -> str:
    """Runs one inactivity pass (reminders + removals), sends the report to the report recipients, and returns it."""
    now   = get_ist_time()
    today = now.date()
    end   = today
    start = end - timedelta(days=WINPRO_INACTIVITY_DAYS)
    first_removal_date = WINPRO_KICK_START_DATE + timedelta(days=WINPRO_INACTIVITY_DAYS + 1)

    mode = "DRY RUN (no reminders sent, nobody removed)" if dry_run else "LIVE"
    lines = [
        f"<b>Winpro Inactivity Report</b> — {now:%d %b %Y %H:%M} IST",
        f"Mode: {mode}",
        "Activity: latest closed deal in the account's MT5 history",
        f"History window: {start} → {end}",
    ]
    if today < first_removal_date:
        lines.append(
            f"Fresh start: everyone's clock started {WINPRO_KICK_START_DATE:%d %b %Y}. "
            f"First removals possible on {first_removal_date:%d %b %Y}."
        )
    lines.append("")

    db = SessionLocal()
    removed, failed = [], []
    reminders_due, inactive = [], []
    try:
        members = db.query(TelegramMember).filter(
            TelegramMember.broker    == "winpro",
            TelegramMember.is_active == True,
        ).all()

        if not members:
            lines.append("No active Winpro members to check.")
            report = "\n".join(lines)
            await _send_report(bot, report)
            return report

        last_trade, unreadable = await _last_trade_by_member(members, start, end)

        # Reading nothing at all means the feed is down (today: the master_password requirement),
        # not that the whole community stopped trading. Abort rather than remove everyone.
        if len(unreadable) == len(members):
            lines += [
                f"Could not read trade history for any of the {len(members)} members. "
                "Run aborted — no reminders sent, nobody removed.",
                "This is expected while the Winpro MT5 endpoints still demand a master password.",
            ]
            report = "\n".join(lines)
            await _send_report(bot, report)
            return report

        unreadable_ids = {m.telegram_id for m in unreadable}

        first_reminder = WINPRO_REMINDER_DAYS[0] if WINPRO_REMINDER_DAYS else WINPRO_INACTIVITY_DAYS
        active, exempt, waiting = [], [], []
        for member in members:
            ts = last_trade.get(member.telegram_id)
            if ts and (member.last_trade_date is None or ts > member.last_trade_date):
                member.last_trade_date = ts

            if is_kick_exempt(member):
                exempt.append(member)
                continue
            if member.telegram_id in unreadable_ids:
                continue  # history unavailable — leave this member untouched this run

            days = _inactive_days(member, today)
            if days < first_reminder:
                member.inactivity_reminder_stage = 0
                active.append(member)
            elif days > WINPRO_INACTIVITY_DAYS:  # removed on day WINPRO_INACTIVITY_DAYS + 1, once the last day's trades are in
                inactive.append(member)
            else:
                stage = max(d for d in WINPRO_REMINDER_DAYS if d <= days)
                if stage > (member.inactivity_reminder_stage or 0):
                    reminders_due.append((member, days, stage))
                else:
                    waiting.append(member)
        db.commit()

        # ── Reminders ──────────────────────────────────────────────

        undelivered = 0
        if not dry_run:
            for member, days, stage in reminders_due:
                if not await _send_reminder(bot, member, days):
                    undelivered += 1
                # Record the stage even if undelivered (bot blocked) so it isn't retried every night.
                member.inactivity_reminder_stage = stage
                db.commit()
                await asyncio.sleep(0.05)

        sent_word = "due" if dry_run else "sent"
        stage_counts = {d: sum(1 for _, _, s in reminders_due if s == d) for d in WINPRO_REMINDER_DAYS}
        reminder_line = f"Reminders {sent_word}: " + " · ".join(
            f"{d}-day: {stage_counts[d]}" for d in WINPRO_REMINDER_DAYS
        )
        if undelivered:
            reminder_line += f" (could not deliver: {undelivered})"
        lines += [
            f"Active Winpro members checked: <b>{len(members)}</b>",
            f"Active (inactive under {first_reminder} days): {len(active)}",
            f"Exempt (never reminded or removed): {len(exempt)}",
            f"History unreadable (skipped, not removed): {len(unreadable)}",
            reminder_line,
            f"Already reminded, not traded yet: {len(waiting)}",
        ]

        # ── Removals ──────────────────────────────────────────────

        checked = len(members) - len(unreadable)
        too_many = (
            checked >= MIN_MEMBERS_FOR_RATIO_CHECK
            and len(inactive) > checked * WINPRO_KICK_MAX_RATIO
        )
        if dry_run:
            lines.append(f"Would remove: <b>{len(inactive)}</b>")
            if too_many:
                lines.append(f"That is over {WINPRO_KICK_MAX_RATIO:.0%} of members — a LIVE run would abort.")
        elif too_many:
            lines += [
                f"Inactive over {WINPRO_INACTIVITY_DAYS} days: <b>{len(inactive)}</b>",
                f"Over {WINPRO_KICK_MAX_RATIO:.0%} of members would be removed. Removals aborted as a safety measure — "
                "nobody was removed. Check the Winpro data, then raise WINPRO_KICK_MAX_RATIO if this is expected.",
            ]
        else:
            group = _group_id()
            for member in inactive:
                ok, note = await _remove_member(bot, group, member)
                if ok:
                    member.is_active = False
                    member.inactivity_reminder_stage = 0
                    db.commit()
                    removed.append((member, note))
                    await _notify_removed(bot, member)
                else:
                    failed.append((member, note))
                await asyncio.sleep(0.2)  # stay well under Telegram rate limits
            lines += [
                f"Removed: <b>{len(removed)}</b>",
                f"Failed to remove: {len(failed)}",
            ]

        # ── Detail lists ──────────────────────────────────────────────

        final_stage = WINPRO_REMINDER_DAYS[-1] if WINPRO_REMINDER_DAYS else None
        final_warnings = [m for m, _, s in reminders_due if s == final_stage]
        if final_warnings:
            lines += ["", f"<b>Final warning {sent_word} — removal tomorrow if no trade:</b>"]
            lines += [_describe(m) for m in final_warnings]
        if removed:
            lines += ["", "<b>Removed:</b>"]
            lines += [_describe(m) + (f" — {html.escape(note)}" if note else "") for m, note in removed]
        elif inactive and (dry_run or too_many):
            lines += ["", f"<b>Inactive over {WINPRO_INACTIVITY_DAYS} days:</b>"]
            lines += [_describe(m) for m in inactive]
        if failed:
            lines += ["", "<b>Failed to remove:</b>"]
            lines += [f"{_describe(m)} — {html.escape(note)}" for m, note in failed]
        if unreadable:
            lines += ["", "<b>History unreadable — skipped this run:</b>"]
            lines += [_describe(m) for m in unreadable[:25]]
            if len(unreadable) > 25:
                lines.append(f"… and {len(unreadable) - 25} more")
    finally:
        db.close()

    report = "\n".join(lines)
    logger.info(
        f"[winpro-kick] Run finished (dry_run={dry_run}): {len(reminders_due)} reminders, "
        f"{len(inactive)} inactive, {len(removed)} removed, {len(failed)} failed"
    )
    await _send_report(bot, report)
    return report
