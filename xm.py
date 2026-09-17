"""
xm.py
─────
XM Partners Trade Statistics API and the daily inactivity check.

Every day at 01:00 IST, for each active XM member:
  • days inactive = days since the latest of: last XM trade, join date, XM_KICK_START_DATE
  • at XM_REMINDER_DAYS (20 / 25 / 30) they get a reminder DM
  • after XM_INACTIVITY_DAYS (30), i.e. on day 31, they are removed from the group (ban + immediate unban,
    so they can rejoin after trading) and marked inactive
A report is sent to INACTIVITY_REPORT_TELEGRAM_IDS.
"""

import asyncio
import html
import logging
from datetime import date, datetime, timedelta

import httpx
from telegram import Bot
from telegram.error import BadRequest, TelegramError

from bot import _group_id
from config import (
    XM_API_KEY, XM_INACTIVITY_DAYS, XM_KICK_START_DATE, XM_REMINDER_DAYS, XM_KICK_DRY_RUN,
    XM_KICK_MAX_RATIO, INACTIVITY_REPORT_TELEGRAM_IDS,
)
from database import SessionLocal, TelegramMember, get_ist_time

logger = logging.getLogger(__name__)

XM_TRADES_URL = "https://mypartners.xm.com/api/trader-statistics/trades"
JOB_NAME      = "xm_inactivity_kick"   # daily run is scheduled by kick_scheduler.py

# Below this many members the max-ratio safety check is skipped (small groups swing wildly).
MIN_MEMBERS_FOR_RATIO_CHECK = 10
TELEGRAM_MESSAGE_LIMIT      = 4000


# ═════════════════════════════════════════════════════════════════════════════
# XM API
# ═════════════════════════════════════════════════════════════════════════════

async def fetch_xm_trades(start_date: date, end_date: date, trader_ids: list[str] | None = None) -> list | None:
    """Returns all trades in the window (optionally for specific accounts), or None if the call failed."""
    if not XM_API_KEY:
        logger.error("XM_API_KEY not configured")
        return None

    params = {"startTime": start_date.isoformat(), "endTime": end_date.isoformat()}
    if trader_ids:
        params["traderIds"] = ",".join(trader_ids)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                XM_TRADES_URL,
                params=params,
                headers={"Authorization": f"Bearer {XM_API_KEY}", "Accept": "application/json"},
                timeout=120.0,
            )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.error(f"Error communicating with XM API: {e}")
        return None

    # An account with no trades comes back as 200 []; anything that isn't a list is treated as a failure.
    if not isinstance(data, list):
        logger.error(f"Unexpected XM API response: {str(data)[:200]}")
        return None
    return data


def _trade_window(now_ist: datetime) -> tuple[date, date]:
    end = now_ist.date()
    return end - timedelta(days=XM_INACTIVITY_DAYS), end


def _parse_time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None) if value else None
    except ValueError:
        return None


def _last_trade_by_login(trades: list) -> dict[str, datetime | None]:
    """Maps each loginId that traded to its latest close (or open) time."""
    last: dict[str, datetime | None] = {}
    for trade in trades:
        login = str(trade.get("loginId") or "").strip()
        if not login:
            continue
        ts = _parse_time(trade.get("closeTime")) or _parse_time(trade.get("openTime"))
        current = last.get(login)
        if login not in last or (ts and (current is None or ts > current)):
            last[login] = ts
    return last


async def check_recent_xm_trade(account_id: str) -> tuple[bool | None, datetime | None]:
    """
    Checks one account for trades in the inactivity window.
    Returns (traded, last_trade_time); traded is None if the XM API call failed.
    """
    start, end = _trade_window(get_ist_time())
    trades = await fetch_xm_trades(start, end, trader_ids=[account_id])
    if trades is None:
        return None, None
    last = _last_trade_by_login(trades)
    if account_id not in last:
        return False, None
    return True, last[account_id]


# ═════════════════════════════════════════════════════════════════════════════
# MEMBER MESSAGES
# ═════════════════════════════════════════════════════════════════════════════

def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _inactive_days(member: TelegramMember, today: date) -> int:
    """Days since the latest of: last trade, join date, and the programme start date."""
    reference = XM_KICK_START_DATE
    for dt in (member.joined_at, member.last_trade_date):
        if dt and dt.date() > reference:
            reference = dt.date()
    return (today - reference).days


def _reminder_text(member: TelegramMember, days: int) -> str:
    days_left  = max(XM_INACTIVITY_DAYS - days, 1)
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
        f"We noticed there's been <b>no trading activity</b> on your XM account <code>{account}</code> "
        f"for the last <b>{_plural(days, 'day')}</b>.\n\n"
        "The Active Traders Community is for active traders, so members need "
        f"<b>at least one closed trade every {XM_INACTIVITY_DAYS} days</b> to stay in.\n\n"
        f"{urgency}\n\n"
        "✅ <b>How to keep your spot:</b>\n"
        "1️⃣ Log in to your XM account\n"
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
        logger.info(f"[xm-kick] Could not send reminder to {member.telegram_id}: {e}")
        return False


async def _notify_removed(bot: Bot, member: TelegramMember):
    try:
        await bot.send_message(
            chat_id=int(member.telegram_id),
            text=(
                "😔 <b>You have been removed from the Active Traders Community</b>\n\n"
                f"Your XM account <code>{html.escape(str(member.account_id))}</code> has had no trading activity "
                f"for {XM_INACTIVITY_DAYS} days.\n\n"
                "<b>Want back in? It's easy:</b>\n"
                "1️⃣ Place and close at least one trade on your XM account\n"
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

def _describe(member: TelegramMember) -> str:
    name     = html.escape(member.full_name or "—")
    username = f" @{html.escape(member.telegram_username)}" if member.telegram_username else ""
    last     = f"last trade {member.last_trade_date:%d %b %Y}" if member.last_trade_date else "no trade on record"
    return f"• {name}{username} — <code>{html.escape(str(member.account_id))}</code> (tg <code>{member.telegram_id}</code>, {last})"


async def _remove_member(bot: Bot, group, member: TelegramMember) -> tuple[bool, str]:
    """
    Removes the member from the group without a permanent ban.
    Returns (removed, note). removed=True means they are no longer in the group.
    """
    try:
        user_id = int(member.telegram_id)
    except ValueError:
        return False, "invalid telegram id"

    try:
        chat_member = await bot.get_chat_member(group, user_id)
    except BadRequest as e:
        if "not found" in str(e).lower():
            return True, "not in group"
        return False, str(e)
    except TelegramError as e:
        return False, str(e)

    if chat_member.status in ("administrator", "creator"):
        return False, "group admin — not removed"
    if chat_member.status in ("left", "kicked"):
        return True, "already out of group"

    try:
        await bot.ban_chat_member(group, user_id)
    except TelegramError as e:
        return False, str(e)

    try:
        # Unban straight away so the removal isn't permanent and a new invite link works for them.
        await bot.unban_chat_member(group, user_id, only_if_banned=True)
    except TelegramError as e:
        logger.error(f"[xm-kick] Removed {user_id} but unban failed: {e}")
        return True, f"still banned — unban manually ({e})"

    return True, ""


async def _send_report(bot: Bot, report: str):
    if not INACTIVITY_REPORT_TELEGRAM_IDS:
        logger.warning("INACTIVITY_REPORT_TELEGRAM_IDS not set — XM inactivity report not sent.")
        return

    # Split on line boundaries to stay under Telegram's message size limit.
    chunks, current = [], ""
    for line in report.split("\n"):
        if current and len(current) + len(line) + 1 > TELEGRAM_MESSAGE_LIMIT:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)

    for chat_id in INACTIVITY_REPORT_TELEGRAM_IDS:
        for chunk in chunks:
            try:
                await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML")
            except TelegramError as e:
                logger.error(f"Could not send XM inactivity report to {chat_id}: {e}")
                break


async def run_xm_inactivity_check(bot: Bot, dry_run: bool = XM_KICK_DRY_RUN) -> str:
    """Runs one inactivity pass (reminders + removals), sends the report to the report recipients, and returns it."""
    now   = get_ist_time()
    today = now.date()
    start, end = _trade_window(now)
    first_removal_date = XM_KICK_START_DATE + timedelta(days=XM_INACTIVITY_DAYS + 1)

    lines = [
        f"<b>XM Inactivity Report</b> — {now:%d %b %Y %H:%M} IST",
        f"Mode: {'DRY RUN (no reminders sent, nobody removed)' if dry_run else 'LIVE'}",
        f"Trades window: {start} → {end}",
    ]
    if today < first_removal_date:
        lines.append(
            f"Fresh start: everyone's clock started {XM_KICK_START_DATE:%d %b %Y}. "
            f"First removals possible on {first_removal_date:%d %b %Y}."
        )
    lines.append("")

    trades = await fetch_xm_trades(start, end)
    if trades is None:
        lines.append("XM API call failed. Run aborted — no reminders sent, nobody removed.")
        report = "\n".join(lines)
        await _send_report(bot, report)
        return report

    last_trade = _last_trade_by_login(trades)

    db = SessionLocal()
    removed, failed = [], []
    try:
        members = db.query(TelegramMember).filter(
            TelegramMember.broker    == "xm",
            TelegramMember.is_active == True,
        ).all()

        first_reminder = XM_REMINDER_DAYS[0] if XM_REMINDER_DAYS else XM_INACTIVITY_DAYS
        active, waiting, reminders_due, inactive = [], [], [], []
        for member in members:
            account = str(member.mt5_id or member.account_id).strip()
            if account in last_trade:
                ts = last_trade[account] or now
                if member.last_trade_date is None or ts > member.last_trade_date:
                    member.last_trade_date = ts

            days = _inactive_days(member, today)
            if days < first_reminder:
                member.inactivity_reminder_stage = 0
                active.append(member)
            elif days > XM_INACTIVITY_DAYS:  # removed on day XM_INACTIVITY_DAYS + 1, once the last day's trades are in
                inactive.append(member)
            else:
                stage = max(d for d in XM_REMINDER_DAYS if d <= days)
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

        stage_counts = {d: sum(1 for _, _, s in reminders_due if s == d) for d in XM_REMINDER_DAYS}
        lines += [
            f"Active XM members checked: <b>{len(members)}</b>",
            f"Active (inactive under {first_reminder} days): {len(active)}",
            f"Reminders {'due' if dry_run else 'sent'}: "
            + " · ".join(f"{d}-day: {stage_counts[d]}" for d in XM_REMINDER_DAYS)
            + (f" (could not deliver: {undelivered})" if undelivered else ""),
            f"Already reminded, not traded yet: {len(waiting)}",
        ]

        # ── Removals ──────────────────────────────────────────────

        too_many = (
            len(members) >= MIN_MEMBERS_FOR_RATIO_CHECK
            and len(inactive) > len(members) * XM_KICK_MAX_RATIO
        )
        if dry_run:
            lines.append(f"Would remove: <b>{len(inactive)}</b>")
            if too_many:
                lines.append(f"That is over {XM_KICK_MAX_RATIO:.0%} of members — a LIVE run would abort.")
        elif too_many:
            lines += [
                f"Inactive over {XM_INACTIVITY_DAYS} days: <b>{len(inactive)}</b>",
                f"Over {XM_KICK_MAX_RATIO:.0%} of members would be removed. Removals aborted as a safety measure — "
                "nobody was removed. Check the XM data, then raise XM_KICK_MAX_RATIO if this is expected.",
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

        final_stage = XM_REMINDER_DAYS[-1] if XM_REMINDER_DAYS else None
        final_warnings = [m for m, _, s in reminders_due if s == final_stage]
        if final_warnings:
            lines += ["", f"<b>Final warning {'due' if dry_run else 'sent'} — removal tomorrow if no trade:</b>"]
            lines += [_describe(m) for m in final_warnings]
        if removed:
            lines += ["", "<b>Removed:</b>"]
            lines += [_describe(m) + (f" — {html.escape(note)}" if note else "") for m, note in removed]
        elif inactive and (dry_run or too_many):
            lines += ["", f"<b>Inactive over {XM_INACTIVITY_DAYS} days:</b>"]
            lines += [_describe(m) for m in inactive]
        if failed:
            lines += ["", "<b>Failed to remove:</b>"]
            lines += [f"{_describe(m)} — {html.escape(note)}" for m, note in failed]
    finally:
        db.close()

    report = "\n".join(lines)
    logger.info(
        f"[xm-kick] Run finished (dry_run={dry_run}): {len(reminders_due)} reminders, "
        f"{len(inactive)} inactive, {len(removed)} removed, {len(failed)} failed"
    )
    await _send_report(bot, report)
    return report
