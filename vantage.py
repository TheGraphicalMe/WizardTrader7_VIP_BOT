import logging
import httpx
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from config import VANTAGE_USER_ID, VANTAGE_SECRET, FIXIE_URL
from database import BrokerAccount

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
    start_dt = end_dt - timedelta(days=30)
    
    start_time_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_time_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")

    logger.info(f"Querying Vantage API for past 30 days accounts...")
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
            client_email=""
        )
        db.add(db_account)
        try:
            db.commit()
            new_accounts_added += 1
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
