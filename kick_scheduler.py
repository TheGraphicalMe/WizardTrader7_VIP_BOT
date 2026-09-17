"""
kick_scheduler.py
─────────────────
Runs every broker's inactivity check daily at 01:00 IST, one broker after another,
so the runs never message Telegram at the same time.

01:00 IST is also outside Vantage's commissionData maintenance window (0:00–6:00 server time).
Each broker claims its own daily slot, so restarts or multiple workers never run a job twice,
and a slot missed while the server was down runs on startup.
"""

import asyncio
import html
import logging
from datetime import date, datetime, time, timedelta
from typing import Awaitable, Callable

from sqlalchemy.exc import IntegrityError
from telegram import Bot

from database import SessionLocal, ScheduledJobRun, get_ist_time
from xm import _send_report

logger = logging.getLogger(__name__)

RUN_AT_IST = time(hour=1, minute=0)

KickJob = tuple[str, Callable[[Bot], Awaitable[str]]]   # (job name, run function)


def _current_slot(now_ist: datetime) -> date:
    """IST date of the most recent 01:00 slot."""
    return (now_ist - timedelta(hours=RUN_AT_IST.hour, minutes=RUN_AT_IST.minute)).date()


def _claim_run(job_name: str, slot: date) -> bool:
    """Atomically marks the slot as run. False if it already ran (restart or another worker)."""
    db = SessionLocal()
    try:
        db.add(ScheduledJobRun(job_name=job_name, run_date=slot.isoformat()))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
    finally:
        db.close()


async def inactivity_scheduler(bot: Bot, jobs: list[KickJob]):
    while True:
        slot = _current_slot(get_ist_time())
        for job_name, run in jobs:
            try:
                if _claim_run(job_name, slot):
                    logger.info(f"[{job_name}] Starting run for slot {slot}")
                    await run(bot)
            except Exception as e:
                logger.error(f"Error in {job_name}: {e}", exc_info=True)
                try:
                    await _send_report(bot, f"{html.escape(job_name)} crashed: {html.escape(str(e))}")
                except Exception:
                    pass

        next_run = datetime.combine(_current_slot(get_ist_time()) + timedelta(days=1), RUN_AT_IST)
        await asyncio.sleep(max(60, (next_run - get_ist_time()).total_seconds()))
