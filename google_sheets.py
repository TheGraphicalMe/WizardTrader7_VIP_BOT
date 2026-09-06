import logging
import httpx
from datetime import datetime
import asyncio
from config import GOOGLE_SHEETS_WEBHOOK_URL

logger = logging.getLogger(__name__)

async def append_to_sheet(broker: str, account_id: str, email: str = "", extra_data: dict = None):
    """
    Sends newly registered account data to the Google Sheets Webhook.
    This runs asynchronously and doesn't block the main flow.
    """
    if not GOOGLE_SHEETS_WEBHOOK_URL:
        # Silently skip if the webhook URL isn't configured yet
        return

    # Use current UTC time for consistency
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "timestamp": timestamp,
        "broker": broker,
        "account_id": account_id,
        "email": email if email else "",
        "extra_data": extra_data or {}
    }

    try:
        # Post to Google Apps Script Webhook
        async with httpx.AsyncClient(follow_redirects=True) as client:
            # Send fire-and-forget without waiting too long
            response = await client.post(
                GOOGLE_SHEETS_WEBHOOK_URL, 
                json=payload, 
                timeout=10.0
            )
            response.raise_for_status()
            logger.info(f"✅ Synced account {account_id} ({broker}) to Google Sheets.")
    except Exception as e:
        logger.error(f"❌ Failed to sync account {account_id} to Google Sheets: {e}")

def trigger_sheet_sync(broker: str, account_id: str, email: str = "", extra_data: dict = None):
    """
    Wrapper to fire the async sheet sync as a background task
    from synchronous or asynchronous contexts.
    """
    if GOOGLE_SHEETS_WEBHOOK_URL:
        try:
            # Check if there is a running event loop
            loop = asyncio.get_running_loop()
            loop.create_task(append_to_sheet(broker, account_id, email, extra_data))
        except RuntimeError:
            # If no running loop, we shouldn't reach here in a fastapi/telegram context normally,
            # but just in case, we can't easily spawn without blocking, so we'll run it synchronously
            asyncio.run(append_to_sheet(broker, account_id, email, extra_data))
