import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, MetaData, update
from sqlalchemy.orm import sessionmaker
from database import BrokerAccount, TelegramMember, PendingVerification

def reset_claims():
    load_dotenv()
    DATABASE_URL = os.getenv("DATABASE_URL")

    if not DATABASE_URL:
        print("❌ Error: DATABASE_URL not found in .env file.")
        return

    is_sqlite = DATABASE_URL.startswith("sqlite")
    print(f"Connecting to {'Local SQLite' if is_sqlite else 'Supabase PostgreSQL'} Database to reset claims...")

    try:
        engine = create_engine(DATABASE_URL)
        SessionLocal = sessionmaker(bind=engine)
        db = SessionLocal()
    except Exception as e:
        print(f"❌ Failed to connect to database: {e}")
        return

    try:
        # 1. Reset BrokerAccount claims
        print("Resetting claims in 'broker_accounts' table...")
        updated_count = db.query(BrokerAccount).update({
            BrokerAccount.is_claimed: False,
            BrokerAccount.claimed_by_telegram_id: None,
            BrokerAccount.claimed_at: None
        })
        print(f"  └─ Reset {updated_count} broker accounts to unclaimed.")

        # 2. Clear TelegramMember table (so users can verify again for the new group)
        print("Clearing 'telegram_members' table...")
        deleted_members = db.query(TelegramMember).delete()
        print(f"  └─ Removed {deleted_members} records.")

        # 3. Clear PendingVerification table
        print("Clearing 'pending_verifications' table...")
        deleted_pending = db.query(PendingVerification).delete()
        print(f"  └─ Removed {deleted_pending} records.")

        db.commit()
        print("\n✅ Database claims and member history successfully reset!")

    except Exception as e:
        db.rollback()
        print(f"❌ Error during database reset: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    confirm = input("⚠️ WARNING: This will reset all claimed broker accounts and remove member history from the active database. Type 'yes' to confirm: ")
    if confirm.strip().lower() == "yes":
        reset_claims()
    else:
        print("❌ Aborted.")
