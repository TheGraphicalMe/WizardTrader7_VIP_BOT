"""
exness.py
─────────
Exness Affiliates API, account verification and the daily inactivity check.

Verification (called from bot.py):
  1. find_account(mt5_id)      → the MT5 account must be under our partner account
  2. save_verified_account(...) → stored with its client_uid and mt5_id

Every day at 01:00 IST (see kick_scheduler.py) every account under the partner account is
stored and pushed to Google Sheets, then for each active Exness member:
  • activity = the latest trade on ANY trading account of that Exness client
    (the Exness report is updated daily, so trades show up the next day)
  • at EXNESS_REMINDER_DAYS (7 / 12 / 15) they get a reminder DM
  • after EXNESS_INACTIVITY_DAYS (15), i.e. on day 16, they are removed from the group (ban + immediate unban)
A report is sent to INACTIVITY_REPORT_TELEGRAM_IDS.
"""

import asyncio
import html
import logging
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from telegram import Bot
from telegram.error import TelegramError

from bot import _group_id
from config import (
    GOOGLE_SHEETS_WEBHOOK_URL, EXNESS_LOGIN, EXNESS_PASSWORD, EXNESS_INACTIVITY_DAYS, EXNESS_KICK_START_DATE,
    EXNESS_REMINDER_DAYS, EXNESS_KICK_DRY_RUN, EXNESS_KICK_MAX_RATIO,
)
from database import SessionLocal, BrokerAccount, TelegramMember, get_ist_time
from google_sheets import append_to_sheet, trigger_sheet_sync
from xm import _plural, _describe, _remove_member, _send_report, is_kick_exempt

logger = logging.getLogger(__name__)

EXNESS_BASE_URL = "https://my.exnessaffiliates.com"
JOB_NAME        = "exness_inactivity_kick"   # daily runs are scheduled by kick_scheduler.py
SYNC_JOB_NAME   = "exness_account_sync"

PAGE_SIZE    = 100   # rows per report page
FILTER_BATCH = 50    # IDs per comma-separated filter, keeps the URL short

# Below this many members the max-ratio safety check is skipped (small groups swing wildly).
MIN_MEMBERS_FOR_RATIO_CHECK = 10

# The login token lasts 6 hours; it is refreshed whenever the API answers 401.
_token: str | None = None
_token_lock = asyncio.Lock()


# ═════════════════════════════════════════════════════════════════════════════
# EXNESS API
# ═════════════════════════════════════════════════════════════════════════════

async def _login(client: httpx.AsyncClient) -> str | None:
    if not EXNESS_LOGIN or not EXNESS_PASSWORD:
        logger.error("Exness credentials not configured")
        return None
    try:
        response = await client.post(
            f"{EXNESS_BASE_URL}/api/v2/auth/",
            json={"login": EXNESS_LOGIN, "password": EXNESS_PASSWORD},
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json().get("token") or None
    except Exception as e:
        logger.error(f"Exness login failed: {e}")
        return None


async def _request(method: str, path: str, params: dict | None = None, json: dict | None = None) -> dict | None:
    """Calls the Exness API with the cached login token. Returns the JSON body, or None if the call failed."""
    global _token
    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt in range(3):
            async with _token_lock:
                if not _token:
                    _token = await _login(client)
                token = _token
            if not token:
                return None

            try:
                response = await client.request(
                    method, f"{EXNESS_BASE_URL}{path}", params=params, json=json,
                    headers={"Authorization": f"JWT {token}", "Accept": "application/json"},
                )
            except httpx.HTTPError as e:
                logger.error(f"Error communicating with Exness API ({path}): {e}")
                return None

            if response.status_code == 401:
                async with _token_lock:
                    if _token == token:
                        _token = None
                continue
            if response.status_code == 429:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if response.status_code >= 400:
                logger.error(f"Exness API {path} returned {response.status_code}: {response.text[:200]}")
                return None

            try:
                data = response.json()
            except ValueError:
                logger.error(f"Exness API {path} returned non-JSON: {response.text[:200]}")
                return None
            return data if isinstance(data, dict) else None

    logger.error(f"Exness API {path}: giving up after repeated 401/429 responses")
    return None


async def fetch_accounts(client_accounts: list[str] | None = None, client_uids: list[str] | None = None) -> list | None:
    """
    Rows from the client accounts report (one row per trading account), all pages.
    Filter by trading account numbers or by client UIDs. Returns None if any call failed.
    """
    if client_accounts:
        batches = [(("client_account", ",".join(client_accounts[i:i + FILTER_BATCH])),)
                   for i in range(0, len(client_accounts), FILTER_BATCH)]
    elif client_uids:
        batches = [(("client_uid", ",".join(client_uids[i:i + FILTER_BATCH])),)
                   for i in range(0, len(client_uids), FILTER_BATCH)]
    else:
        batches = [()]   # no filter — every account under the partner account

    rows = []
    for batch in batches:
        offset = 0
        while True:
            # A fixed sort order keeps paging stable; without it rows shift between pages and some are missed.
            params = {"limit": PAGE_SIZE, "offset": offset, "ordering": "client_account"}
            params.update(batch)
            data = await _request("GET", "/api/reports/clients/accounts/", params=params)
            if data is None:
                return None
            page = data.get("data") or []
            rows += page
            offset += PAGE_SIZE
            if not page or offset >= (data.get("totals") or {}).get("count", 0):
                break
    return rows


async def find_account(mt5_id: str) -> tuple[bool, dict | None]:
    """
    Looks up one trading account under our partner account.
    Returns (ok, row): ok=False if the API call failed, row=None if the account isn't ours.
    """
    rows = await fetch_accounts(client_accounts=[mt5_id])
    if rows is None:
        return False, None
    for row in rows:
        if str(row.get("client_account")) == mt5_id:
            return True, row
    return True, None


def sheet_fields(row: dict) -> dict:
    """The Google Sheets payload for one Exness account."""
    return {
        "client_uid":   str(row.get("client_uid") or ""),
        "mt5_id":       str(row.get("client_account") or ""),
        "account_type": row.get("client_account_type") or "",
        "platform":     row.get("platform") or "",
        "created":      row.get("client_account_created") or "",
    }


def save_verified_account(db: Session, row: dict, push_to_sheet: bool = True) -> BrokerAccount:
    """Stores (or updates) a verified Exness account with its client UID and MT5 ID."""
    mt5_id     = str(row.get("client_account"))
    client_uid = str(row.get("client_uid") or "") or None

    account = db.query(BrokerAccount).filter(
        BrokerAccount.mt5_id == mt5_id,
        BrokerAccount.broker == "exness",
    ).first()
    if account:
        account.client_uid   = client_uid
        account.mt5_id       = mt5_id
        db.commit()
        return account

    db.add(BrokerAccount(account_id=mt5_id, broker="exness", client_uid=client_uid, mt5_id=mt5_id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # stored concurrently — fall through to the existing row
    else:
        if push_to_sheet:
            fields = sheet_fields(row)
            trigger_sheet_sync("exness", mt5_id, "", extra_data=fields,
                               client_uid=fields["client_uid"], mt5_id=mt5_id)

    return db.query(BrokerAccount).filter(
        BrokerAccount.mt5_id == mt5_id,
        BrokerAccount.broker == "exness",
    ).first()


async def sync_all_exness_accounts(bot: Bot | None = None) -> str:
    """
    Daily job: stores every trading account under the partner account that isn't in the
    database yet, and pushes each new one to Google Sheets. No user action needed.
    """
    rows = await fetch_accounts()
    if rows is None:
        logger.error("[exness-sync] Exness API call failed — nothing synced")
        return "Exness account sync: API call failed, nothing synced."

    db = SessionLocal()
    new_rows = []
    try:
        for row in rows:
            mt5_id = str(row.get("client_account") or "")
            if not mt5_id:
                continue
            existed = db.query(BrokerAccount).filter(
                BrokerAccount.mt5_id == mt5_id,
                BrokerAccount.broker == "exness",
            ).first() is not None
            save_verified_account(db, row, push_to_sheet=False)
            if not existed:
                new_rows.append(row)
    finally:
        db.close()

    # Sent one at a time and awaited: fire-and-forget tasks are dropped when the run ends,
    # and Apps Script rejects a burst of parallel requests.
    sent = await push_rows_to_sheet(new_rows)

    logger.info(f"[exness-sync] {len(rows)} accounts checked, {len(new_rows)} new, {sent} added to the sheet")
    result = f"Exness account sync: {len(rows)} accounts checked, {len(new_rows)} new, {sent} added to the sheet."
    if sent < len(new_rows):
        result += f" {len(new_rows) - sent} could not be written to the sheet — see the logs."
    return result


async def push_rows_to_sheet(rows: list, delay: float = 0.3) -> int:
    """Appends each account to Google Sheets, one at a time. Returns how many were written."""
    if not rows or not GOOGLE_SHEETS_WEBHOOK_URL:
        return 0
    sent = 0
    for row in rows:
        fields = sheet_fields(row)
        if await append_to_sheet("exness", fields["mt5_id"], "", fields,
                                 client_uid=fields["client_uid"], mt5_id=fields["mt5_id"]):
            sent += 1
        await asyncio.sleep(delay)
    return sent


def _parse_date(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


async def fetch_last_trade_by_client(client_uids: list[str]) -> dict[str, datetime] | None:
    """Maps each client UID that has ever traded to its latest trade date (any of its accounts)."""
    rows = await fetch_accounts(client_uids=client_uids)
    if rows is None:
        return None
    last: dict[str, datetime] = {}
    for row in rows:
        uid = str(row.get("client_uid") or "")
        ts  = _parse_date(row.get("client_account_last_trade"))
        if uid and ts and (uid not in last or ts > last[uid]):
            last[uid] = ts
    return last


async def check_recent_exness_trade(client_uid: str | None, mt5_id: str) -> tuple[bool | None, datetime | None]:
    """
    Checks one client for a trade inside the inactivity window.
    Returns (traded, last_trade_date); traded is None if the Exness API call failed.
    """
    if not client_uid:
        ok, row = await find_account(mt5_id)
        if not ok:
            return None, None
        client_uid = str((row or {}).get("client_uid") or "")
        if not client_uid:
            return False, None

    last = await fetch_last_trade_by_client([client_uid])
    if last is None:
        return None, None
    ts = last.get(client_uid)
    if not ts:
        return False, None
    return (get_ist_time().date() - ts.date()).days <= EXNESS_INACTIVITY_DAYS, ts


# ═════════════════════════════════════════════════════════════════════════════
# MEMBER MESSAGES
# ═════════════════════════════════════════════════════════════════════════════

def _inactive_days(member: TelegramMember, today: date) -> int:
    """Days since the latest of: last trade, join date, and the programme start date."""
    reference = EXNESS_KICK_START_DATE
    for dt in (member.joined_at, member.last_trade_date):
        if dt and dt.date() > reference:
            reference = dt.date()
    return (today - reference).days


def _reminder_text(member: TelegramMember, days: int) -> str:
    days_left  = max(EXNESS_INACTIVITY_DAYS - days, 1)
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
        f"We noticed there's been <b>no trading activity</b> on your Exness account <code>{account}</code> "
        f"for the last <b>{_plural(days, 'day')}</b>.\n\n"
        "The Active Traders Community is for active traders, so members need "
        f"<b>at least one trade every {EXNESS_INACTIVITY_DAYS} days</b> to stay in.\n\n"
        f"{urgency}\n\n"
        "✅ <b>How to keep your spot:</b>\n"
        "1️⃣ Log in to your Exness account\n"
        "2️⃣ Place a trade on any of your Exness accounts\n\n"
        "That's all it takes!\n\n"
        "We'd love to keep you with us. Happy trading! 📈"
    )


async def _send_reminder(bot: Bot, member: TelegramMember, days: int) -> bool:
    try:
        await bot.send_message(chat_id=int(member.telegram_id), text=_reminder_text(member, days), parse_mode="HTML")
        return True
    except (TelegramError, ValueError) as e:
        logger.info(f"[exness-kick] Could not send reminder to {member.telegram_id}: {e}")
        return False


async def _notify_removed(bot: Bot, member: TelegramMember):
    try:
        await bot.send_message(
            chat_id=int(member.telegram_id),
            text=(
                "😔 <b>You have been removed from the Active Traders Community</b>\n\n"
                f"Your Exness account <code>{html.escape(str(member.account_id))}</code> has had no trading activity "
                f"for {EXNESS_INACTIVITY_DAYS} days.\n\n"
                "<b>Want back in? It's easy:</b>\n"
                "1️⃣ Place at least one trade on your Exness account\n"
                "2️⃣ Send /start and verify again\n\n"
                "See you back soon! 📈"
            ),
            parse_mode="HTML",
        )
    except (TelegramError, ValueError):
        pass  # user blocked the bot or never started it


# ═════════════════════════════════════════════════════════════════════════════
# INACTIVITY CHECK
# ═════════════════════════════════════════════════════════════════════════════

async def _last_trade_by_member(db: Session, members: list[TelegramMember]) -> dict[str, datetime] | None:
    """Maps telegram_id → latest trade date of that member's Exness client. None if the API failed."""
    # Exness rejects non-numeric account filters with a 400, which would abort the whole run.
    mt5_ids = [mt5 for mt5 in (str(m.mt5_id or m.account_id).strip() for m in members) if mt5.isdigit()]
    accounts = {
        a.mt5_id: a for a in db.query(BrokerAccount).filter(
            BrokerAccount.broker == "exness",
            BrokerAccount.mt5_id.in_(mt5_ids),
        )
    } if mt5_ids else {}
    uid_by_mt5 = {mt5: a.client_uid for mt5, a in accounts.items() if a.client_uid}

    # Accounts stored without a client UID are looked up once and filled in.
    missing = [mt5 for mt5 in mt5_ids if mt5 not in uid_by_mt5]
    if missing:
        rows = await fetch_accounts(client_accounts=missing)
        if rows is None:
            return None
        for row in rows:
            mt5, uid = str(row.get("client_account")), str(row.get("client_uid") or "")
            if uid and mt5 in missing:
                uid_by_mt5[mt5] = uid
                if mt5 in accounts:
                    accounts[mt5].client_uid = uid
                    accounts[mt5].mt5_id     = mt5
        db.commit()

    last_by_uid = await fetch_last_trade_by_client(sorted(set(uid_by_mt5.values())))
    if last_by_uid is None:
        return None

    result = {}
    for member in members:
        ts = last_by_uid.get(uid_by_mt5.get(str(member.mt5_id or member.account_id).strip(), ""))
        if ts:
            result[member.telegram_id] = ts
    return result


async def run_exness_inactivity_check(bot: Bot, dry_run: bool = EXNESS_KICK_DRY_RUN) -> str:
    """Runs one inactivity pass (reminders + removals), sends the report to the report recipients, and returns it."""
    now   = get_ist_time()
    today = now.date()
    first_removal_date = EXNESS_KICK_START_DATE + timedelta(days=EXNESS_INACTIVITY_DAYS + 1)

    lines = [
        f"<b>Exness Inactivity Report</b> — {now:%d %b %Y %H:%M} IST",
        f"Mode: {'DRY RUN (no reminders sent, nobody removed)' if dry_run else 'LIVE'}",
        "Activity: latest trade on any account of the Exness client",
    ]
    if today < first_removal_date:
        lines.append(
            f"Fresh start: everyone's clock started {EXNESS_KICK_START_DATE:%d %b %Y}. "
            f"First removals possible on {first_removal_date:%d %b %Y}."
        )
    lines.append("")

    db = SessionLocal()
    removed, failed = [], []
    try:
        members = db.query(TelegramMember).filter(
            TelegramMember.broker    == "exness",
            TelegramMember.is_active == True,
        ).all()

        last_trade = await _last_trade_by_member(db, members)
        if last_trade is None:
            lines.append("Exness API call failed. Run aborted — no reminders sent, nobody removed.")
            report = "\n".join(lines)
            await _send_report(bot, report)
            return report

        first_reminder = EXNESS_REMINDER_DAYS[0] if EXNESS_REMINDER_DAYS else EXNESS_INACTIVITY_DAYS
        active, exempt, waiting, reminders_due, inactive = [], [], [], [], []
        for member in members:
            ts = last_trade.get(member.telegram_id)
            if ts and (member.last_trade_date is None or ts > member.last_trade_date):
                member.last_trade_date = ts

            if is_kick_exempt(member):
                exempt.append(member)
                continue

            days = _inactive_days(member, today)
            if days < first_reminder:
                member.inactivity_reminder_stage = 0
                active.append(member)
            elif days > EXNESS_INACTIVITY_DAYS:  # removed on day EXNESS_INACTIVITY_DAYS + 1, once the last day's trades are in
                inactive.append(member)
            else:
                stage = max(d for d in EXNESS_REMINDER_DAYS if d <= days)
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

        stage_counts = {d: sum(1 for _, _, s in reminders_due if s == d) for d in EXNESS_REMINDER_DAYS}
        lines += [
            f"Active Exness members checked: <b>{len(members)}</b>",
            f"Active (inactive under {first_reminder} days): {len(active)}",
            f"Exempt (never reminded or removed): {len(exempt)}",
            f"Reminders {'due' if dry_run else 'sent'}: "
            + " · ".join(f"{d}-day: {stage_counts[d]}" for d in EXNESS_REMINDER_DAYS)
            + (f" (could not deliver: {undelivered})" if undelivered else ""),
            f"Already reminded, not traded yet: {len(waiting)}",
        ]

        # ── Removals ──────────────────────────────────────────────

        too_many = (
            len(members) >= MIN_MEMBERS_FOR_RATIO_CHECK
            and len(inactive) > len(members) * EXNESS_KICK_MAX_RATIO
        )
        if dry_run:
            lines.append(f"Would remove: <b>{len(inactive)}</b>")
            if too_many:
                lines.append(f"That is over {EXNESS_KICK_MAX_RATIO:.0%} of members — a LIVE run would abort.")
        elif too_many:
            lines += [
                f"Inactive over {EXNESS_INACTIVITY_DAYS} days: <b>{len(inactive)}</b>",
                f"Over {EXNESS_KICK_MAX_RATIO:.0%} of members would be removed. Removals aborted as a safety measure — "
                "nobody was removed. Check the Exness data, then raise EXNESS_KICK_MAX_RATIO if this is expected.",
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

        final_stage = EXNESS_REMINDER_DAYS[-1] if EXNESS_REMINDER_DAYS else None
        final_warnings = [m for m, _, s in reminders_due if s == final_stage]
        if final_warnings:
            lines += ["", f"<b>Final warning {'due' if dry_run else 'sent'} — removal tomorrow if no trade:</b>"]
            lines += [_describe(m) for m in final_warnings]
        if removed:
            lines += ["", "<b>Removed:</b>"]
            lines += [_describe(m) + (f" — {html.escape(note)}" if note else "") for m, note in removed]
        elif inactive and (dry_run or too_many):
            lines += ["", f"<b>Inactive over {EXNESS_INACTIVITY_DAYS} days:</b>"]
            lines += [_describe(m) for m in inactive]
        if failed:
            lines += ["", "<b>Failed to remove:</b>"]
            lines += [f"{_describe(m)} — {html.escape(note)}" for m, note in failed]
    finally:
        db.close()

    report = "\n".join(lines)
    logger.info(
        f"[exness-kick] Run finished (dry_run={dry_run}): {len(reminders_due)} reminders, "
        f"{len(inactive)} inactive, {len(removed)} removed, {len(failed)} failed"
    )
    await _send_report(bot, report)
    return report
