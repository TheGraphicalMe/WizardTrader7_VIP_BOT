import asyncio
import html
import logging
import httpx
from datetime import date, datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from telegram import Bot
from telegram.error import TelegramError

from bot import _group_id
from config import (
    VANTAGE_USER_ID, VANTAGE_SECRET, FIXIE_URL,
    VANTAGE_INACTIVITY_DAYS, VANTAGE_KICK_START_DATE, VANTAGE_REMINDER_DAYS,
    VANTAGE_KICK_DRY_RUN, VANTAGE_KICK_MAX_RATIO,
)
from database import BrokerAccount, SessionLocal, TelegramMember, get_ist_time
from google_sheets import trigger_sheet_sync
from xm import _plural, _describe, _remove_member, _send_report, is_kick_exempt

logger = logging.getLogger(__name__)

VANTAGE_BASE_URL = "https://openapi.vantagemarkets.com"

async def _post_vantage(endpoint: str, start_time: str, end_time: str) -> dict:
    if not VANTAGE_USER_ID or not VANTAGE_SECRET:
        logger.error("Vantage credentials not configured")
        return {"code": 500, "msg": "Vantage credentials missing", "data": []}

    payload = {
        "userId": VANTAGE_USER_ID,
        "secret": VANTAGE_SECRET,
        "startTime": start_time,
        "endTime": end_time
    }
    
    headers = {
        "Content-Type": "application/json"
    }

    proxies = {"all://": FIXIE_URL} if FIXIE_URL else None # type: ignore

    async with httpx.AsyncClient(proxies=proxies) as client: # type: ignore
        try:
            response = await client.post(
                f"{VANTAGE_BASE_URL}{endpoint}",
                json=payload,
                headers=headers,
                timeout=30.0
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error communicating with Vantage API: {e}")
            return {"code": 500, "msg": str(e), "data": []}

async def fetch_account_data(start_time: str, end_time: str) -> list:
    """Fetches account data from Vantage API."""
    res = await _post_vantage("/api/ibData/accountData", start_time, end_time)
    if res.get("code") == 1:
        return res.get("data", [])
    else:
        logger.error(f"Vantage API returned error: {res.get('msg')}")
        return []

async def verify_vantage_account(account_id: str, db: Session) -> bool:
    """
    On-demand check if a Vantage account exists in the API for the past year.
    If it exists, saves it to the database and returns True.
    """
    if not account_id:
        return False
        
    tz = timezone(timedelta(hours=3))
    end_dt = datetime.now(tz)
    start_dt = end_dt - timedelta(days=365)
    
    start_time_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_time_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")

    logger.info(f"Querying Vantage API for past 365 days accounts...")
    accounts = await fetch_account_data(start_time_str, end_time_str)
    
    # API returns accounts as ints or strings.
    is_requested_account_valid = False
    new_accounts_added = 0
    seen_ids = set()
    
    for acc in accounts:
        # The IB Dashboard displays the userId, so we store and verify against userId instead of trading account
        acc_id = str(acc.get("userId", ""))
        if not acc_id or acc_id in seen_ids:
            continue
            
        seen_ids.add(acc_id)
            
        if acc_id == str(account_id):
            is_requested_account_valid = True
            
        db_account = BrokerAccount(
            account_id=acc_id,
            broker="vantage",
            client_email="",
            client_uid=acc_id,
            mt5_id=str(acc.get("account") or "") or None,
        )
        db.add(db_account)
        try:
            db.commit()
            new_accounts_added += 1
            
            # Filter the data to only send userId, account, and date. Rest are empty.
            filtered_acc = {
                "userId": acc.get("userId", ""),
                "account": acc.get("account", ""),
                "accountType": "",
                "platform": "",
                "currency": "",
                "date": acc.get("date", ""),
                "upperId": ""
            }
            trigger_sheet_sync("vantage", acc_id, "", extra_data=filtered_acc,
                               client_uid=acc_id, mt5_id=str(acc.get("account") or ""))
        except IntegrityError:
            db.rollback() # Account already exists, skip
            
    if new_accounts_added > 0:
        logger.info(f"✅ [vantage] Dynamically fetched and stored {new_accounts_added} new accounts.")
            
    if is_requested_account_valid:
        logger.info(f"✅ [vantage] Account {account_id} verified successfully.")
        return True
    else:
        logger.info(f"❌ [vantage] Account {account_id} not found in Vantage API")
        return False

# --- Other endpoints (Implemented for completeness) ---

async def fetch_leads_data(start_time: str, end_time: str) -> list:
    res = await _post_vantage("/api/ibData/leadsData", start_time, end_time)
    if res.get("code") == 1:
        return res.get("data", [])
    return []

async def fetch_commission_data(start_time: str, end_time: str) -> list:
    res = await _post_vantage("/api/ibData/commissionData", start_time, end_time)
    if res.get("code") == 1:
        return res.get("data", [])
    return []

async def fetch_allocation_data(start_time: str, end_time: str) -> list:
    res = await _post_vantage("/api/ibData/allocationData", start_time, end_time)
    if res.get("code") == 1:
        return res.get("data", [])
    return []


# ═════════════════════════════════════════════════════════════════════════════
# INACTIVITY CHECK
#
# Every day at 01:00 IST (see kick_scheduler.py), for each active Vantage member:
#   • activity = the latest trade on ANY Vantage account of the member's userId
#   • at VANTAGE_REMINDER_DAYS (7 / 12 / 15) they get a reminder DM
#   • after VANTAGE_INACTIVITY_DAYS (15), i.e. on day 16, they are removed (ban + immediate unban)
#
# One commissionData call returns every account under the IB that traded in the window,
# with its lastTradeTime. Vantage allows roughly one call every 5–6 minutes (it answers
# "No access permission" when called too soon) and blocks the endpoint 0:00–6:00 server time.
# ═════════════════════════════════════════════════════════════════════════════

JOB_NAME = "vantage_inactivity_kick"   # daily run is scheduled by kick_scheduler.py

COMMISSION_WINDOW_DAYS   = VANTAGE_INACTIVITY_DAYS + 1   # one day past the limit, so day-15 trades still count
COMMISSION_CACHE_SECONDS = 600   # rejoin checks shortly after another call reuse the result
RATE_LIMIT_RETRY_SECONDS = 400   # wait out Vantage's per-call rate limit before retrying

# Below this many members the max-ratio safety check is skipped (small groups swing wildly).
MIN_MEMBERS_FOR_RATIO_CHECK = 10

_commission_cache: tuple[datetime, dict[str, datetime]] | None = None
_commission_lock = asyncio.Lock()


def _parse_trade_time(value) -> datetime | None:
    """lastTradeTime (UTC) → naive IST, matching the other dates stored for members."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):   # documented as epoch milliseconds
            utc = datetime.fromtimestamp(value / 1000, timezone.utc).replace(tzinfo=None)
        else:                                  # returned in practice as "yyyy-MM-dd HH:mm:ss"
            utc = datetime.fromisoformat(str(value)).replace(tzinfo=None)
        return utc + timedelta(hours=5, minutes=30)
    except (ValueError, OSError, OverflowError):
        return None


async def fetch_last_trade_by_user(retries: int = 0) -> dict[str, datetime] | None:
    """
    Maps each Vantage userId that traded in the last 31 days to its latest trade time (any of its accounts).
    Returns None if the call failed. Results are cached for 10 minutes.
    """
    global _commission_cache
    for attempt in range(retries + 1):
        # The lock is held only around the call itself, so a rejoin check never waits out a retry pause.
        async with _commission_lock:
            now = datetime.utcnow()
            if _commission_cache and now - _commission_cache[0] < timedelta(seconds=COMMISSION_CACHE_SECONDS):
                return _commission_cache[1]

            tz = timezone(timedelta(hours=3))
            end_dt = datetime.now(tz)
            start_dt = end_dt - timedelta(days=COMMISSION_WINDOW_DAYS)
            res = await _post_vantage(
                "/api/ibData/commissionData",
                start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            )
            if res.get("code") == 1 and isinstance(res.get("data"), list):
                fetched_at = get_ist_time()
                last: dict[str, datetime] = {}
                for row in res["data"]:
                    user_id = str(row.get("userId") or "").strip()
                    if not user_id:
                        continue
                    # Listed rows traded inside the window; a missing time counts as a trade now.
                    ts = _parse_trade_time(row.get("lastTradeTime")) or fetched_at
                    if user_id not in last or ts > last[user_id]:
                        last[user_id] = ts

                _commission_cache = (datetime.utcnow(), last)
                return last

            logger.warning(f"[vantage-kick] commissionData failed (attempt {attempt + 1}): {res.get('msg')}")

        if attempt < retries:
            await asyncio.sleep(RATE_LIMIT_RETRY_SECONDS)

    return None


async def check_recent_vantage_trade(user_id: str) -> tuple[bool | None, datetime | None]:
    """
    Checks one Vantage client for a trade inside the inactivity window.
    Returns (traded, last_trade_time); traded is None if the Vantage API call failed.
    """
    last = await fetch_last_trade_by_user()
    if last is None:
        return None, None
    ts = last.get(str(user_id).strip())
    if not ts:
        return False, None
    return (get_ist_time().date() - ts.date()).days <= VANTAGE_INACTIVITY_DAYS, ts


def _inactive_days(member: TelegramMember, today: date) -> int:
    """Days since the latest of: last trade, join date, and the programme start date."""
    reference = VANTAGE_KICK_START_DATE
    for dt in (member.joined_at, member.last_trade_date):
        if dt and dt.date() > reference:
            reference = dt.date()
    return (today - reference).days


def _reminder_text(member: TelegramMember, days: int) -> str:
    days_left  = max(VANTAGE_INACTIVITY_DAYS - days, 1)
    first_name = html.escape((member.full_name or "Trader").split()[0])
    account    = html.escape(str(member.account_id))

    if days_left <= 1:
        headline = "🚨 <b>FINAL WARNING — you'll be removed tomorrow!</b>"
        urgency  = (
            "⛔ If there's still no trade by <b>1:00 AM IST tomorrow</b>, "
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
        f"We noticed there's been <b>no trading activity</b> on your Vantage account (UID <code>{account}</code>) "
        f"for the last <b>{_plural(days, 'day')}</b>.\n\n"
        "The Active Traders Community is for active traders, so members need "
        f"<b>at least one trade every {VANTAGE_INACTIVITY_DAYS} days</b> to stay in.\n\n"
        f"{urgency}\n\n"
        "✅ <b>How to keep your spot:</b>\n"
        "1️⃣ Log in to your Vantage account\n"
        "2️⃣ Place a trade on any of your Vantage trading accounts\n\n"
        "That's all it takes!\n\n"
        "We'd love to keep you with us. Happy trading! 📈"
    )


async def _send_reminder(bot: Bot, member: TelegramMember, days: int) -> bool:
    try:
        await bot.send_message(chat_id=int(member.telegram_id), text=_reminder_text(member, days), parse_mode="HTML")
        return True
    except (TelegramError, ValueError) as e:
        logger.info(f"[vantage-kick] Could not send reminder to {member.telegram_id}: {e}")
        return False


async def _notify_removed(bot: Bot, member: TelegramMember):
    try:
        await bot.send_message(
            chat_id=int(member.telegram_id),
            text=(
                "😔 <b>You have been removed from the Active Traders Community</b>\n\n"
                f"Your Vantage account (UID <code>{html.escape(str(member.account_id))}</code>) has had no trading activity "
                f"for {VANTAGE_INACTIVITY_DAYS} days.\n\n"
                "<b>Want back in? It's easy:</b>\n"
                "1️⃣ Place at least one trade on your Vantage account\n"
                "2️⃣ Send /start and verify again\n\n"
                "See you back soon! 📈"
            ),
            parse_mode="HTML",
        )
    except (TelegramError, ValueError):
        pass  # user blocked the bot or never started it


async def run_vantage_inactivity_check(bot: Bot, dry_run: bool = VANTAGE_KICK_DRY_RUN) -> str:
    """Runs one inactivity pass (reminders + removals), sends the report to the report recipients, and returns it."""
    now   = get_ist_time()
    today = now.date()
    first_removal_date = VANTAGE_KICK_START_DATE + timedelta(days=VANTAGE_INACTIVITY_DAYS + 1)

    lines = [
        f"<b>Vantage Inactivity Report</b> — {now:%d %b %Y %H:%M} IST",
        f"Mode: {'DRY RUN (no reminders sent, nobody removed)' if dry_run else 'LIVE'}",
        f"Activity: latest trade on any account of the Vantage client (last {COMMISSION_WINDOW_DAYS} days)",
    ]
    if today < first_removal_date:
        lines.append(
            f"Fresh start: everyone's clock started {VANTAGE_KICK_START_DATE:%d %b %Y}. "
            f"First removals possible on {first_removal_date:%d %b %Y}."
        )
    lines.append("")

    last_trade = await fetch_last_trade_by_user(retries=2)
    if last_trade is None:
        lines.append("Vantage API call failed. Run aborted — no reminders sent, nobody removed.")
        report = "\n".join(lines)
        await _send_report(bot, report)
        return report

    db = SessionLocal()
    removed, failed = [], []
    try:
        members = db.query(TelegramMember).filter(
            TelegramMember.broker    == "vantage",
            TelegramMember.is_active == True,
        ).all()

        first_reminder = VANTAGE_REMINDER_DAYS[0] if VANTAGE_REMINDER_DAYS else VANTAGE_INACTIVITY_DAYS
        active, exempt, waiting, reminders_due, inactive = [], [], [], [], []
        for member in members:
            ts = last_trade.get(str(member.client_uid or member.account_id).strip())
            if ts and (member.last_trade_date is None or ts > member.last_trade_date):
                member.last_trade_date = ts

            if is_kick_exempt(member):
                exempt.append(member)
                continue

            days = _inactive_days(member, today)
            if days < first_reminder:
                member.inactivity_reminder_stage = 0
                active.append(member)
            elif days > VANTAGE_INACTIVITY_DAYS:  # removed on day VANTAGE_INACTIVITY_DAYS + 1, once the last day's trades are in
                inactive.append(member)
            else:
                stage = max(d for d in VANTAGE_REMINDER_DAYS if d <= days)
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

        stage_counts = {d: sum(1 for _, _, s in reminders_due if s == d) for d in VANTAGE_REMINDER_DAYS}
        lines += [
            f"Active Vantage members checked: <b>{len(members)}</b>",
            f"Active (inactive under {first_reminder} days): {len(active)}",
            f"Exempt (never reminded or removed): {len(exempt)}",
            f"Reminders {'due' if dry_run else 'sent'}: "
            + " · ".join(f"{d}-day: {stage_counts[d]}" for d in VANTAGE_REMINDER_DAYS)
            + (f" (could not deliver: {undelivered})" if undelivered else ""),
            f"Already reminded, not traded yet: {len(waiting)}",
        ]

        # ── Removals ──────────────────────────────────────────────

        too_many = (
            len(members) >= MIN_MEMBERS_FOR_RATIO_CHECK
            and len(inactive) > len(members) * VANTAGE_KICK_MAX_RATIO
        )
        if dry_run:
            lines.append(f"Would remove: <b>{len(inactive)}</b>")
            if too_many:
                lines.append(f"That is over {VANTAGE_KICK_MAX_RATIO:.0%} of members — a LIVE run would abort.")
        elif too_many:
            lines += [
                f"Inactive over {VANTAGE_INACTIVITY_DAYS} days: <b>{len(inactive)}</b>",
                f"Over {VANTAGE_KICK_MAX_RATIO:.0%} of members would be removed. Removals aborted as a safety measure — "
                "nobody was removed. Check the Vantage data, then raise VANTAGE_KICK_MAX_RATIO if this is expected.",
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

        final_stage = VANTAGE_REMINDER_DAYS[-1] if VANTAGE_REMINDER_DAYS else None
        final_warnings = [m for m, _, s in reminders_due if s == final_stage]
        if final_warnings:
            lines += ["", f"<b>Final warning {'due' if dry_run else 'sent'} — removal tomorrow if no trade:</b>"]
            lines += [_describe(m) for m in final_warnings]
        if removed:
            lines += ["", "<b>Removed:</b>"]
            lines += [_describe(m) + (f" — {html.escape(note)}" if note else "") for m, note in removed]
        elif inactive and (dry_run or too_many):
            lines += ["", f"<b>Inactive over {VANTAGE_INACTIVITY_DAYS} days:</b>"]
            lines += [_describe(m) for m in inactive]
        if failed:
            lines += ["", "<b>Failed to remove:</b>"]
            lines += [f"{_describe(m)} — {html.escape(note)}" for m, note in failed]
    finally:
        db.close()

    report = "\n".join(lines)
    logger.info(
        f"[vantage-kick] Run finished (dry_run={dry_run}): {len(reminders_due)} reminders, "
        f"{len(inactive)} inactive, {len(removed)} removed, {len(failed)} failed"
    )
    await _send_report(bot, report)
    return report
