import logging
import httpx
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from config import WINPRO_API_KEY
from database import BrokerAccount
from google_sheets import trigger_sheet_sync

logger = logging.getLogger(__name__)

WINPRO_BASE_URL = "https://clientapi.winpro.finance/api"

async def verify_winpro_account(account_id: str, db: Session) -> tuple[bool, str]:
    """
    Checks if account is under IB, then checks if deposits >= $50.
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
            belongs_to_ib = any(str(item.get("mt5_id")) == str(account_id) for item in items)
            if not belongs_to_ib:
                return False, "not_under_ib"

        except Exception as e:
            logger.error(f"Error checking IB status on Winpro API: {e}")
            return False, "api_error"

        # ── Step 2: Check total successful deposits ───────────────────
        total_deposits = 0.0
        page = 1
        client_email = ""
        client_name = ""
        
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

    if total_deposits >= 50.0:
        db_account = BrokerAccount(
            account_id=str(account_id),
            broker="winpro",
            client_email=client_email or client_name
        )
        db.add(db_account)
        try:
            db.commit()
            trigger_sheet_sync("winpro", str(account_id), client_email or client_name, extra_data={"client_name": client_name})
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
                        client_email=email_to_save
                    )
                    db.add(db_account)
                    try:
                        db.commit()
                        logger.info(f"✅ [winpro] Automatically synced new account: {account_id}")
                        trigger_sheet_sync("winpro", account_id, email_to_save, extra_data=item)
                    except IntegrityError:
                        db.rollback()
                        
                pagination = data.get("data", {}).get("pagination", {})
                if page >= pagination.get("total_pages", 1):
                    break
                    
                page += 1
                
            except Exception as e:
                logger.error(f"Error in sync_all_winpro_accounts: {e}")
                break