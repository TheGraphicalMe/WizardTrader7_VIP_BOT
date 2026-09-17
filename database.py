from sqlalchemy import create_engine, Column, String, DateTime, Boolean, BigInteger, Integer, Text, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from datetime import datetime, timedelta
from config import DATABASE_URL

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine        = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal  = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

def get_ist_time():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


class RawWebhookEvent(Base):
    """
    Logs raw webhook payloads from brokers to help debug unexpected data formats.
    """
    __tablename__ = "raw_webhook_events"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    received_at = Column(DateTime, default=get_ist_time)
    method = Column(String)
    url = Column(String)
    headers = Column(Text, nullable=True)
    query_params = Column(Text, nullable=True)
    raw_body = Column(Text, nullable=True)


class BrokerAccount(Base):
    """
    One row per trading account confirmed via a broker affiliate postback.
    ALL brokers share this single table — the 'broker' column tells them apart.

    Why one table for all brokers?
      - Single place to query "is this account valid?"
      - Easy to add new brokers — just a new postback URL, no DB change
      - Clean reporting across brokers
    """
    __tablename__ = "broker_accounts"

    # Composite primary key: same account_id number could exist across brokers
    account_id    = Column(String,   primary_key=True)
    broker        = Column(String,   primary_key=True)  # 'exness','delta','xm'

    client_email  = Column(String,   nullable=True)
    client_uid    = Column(String,   nullable=True)   # broker's client ID, when the broker has one
    mt5_id        = Column(String,   nullable=True)   # trading account number
    registered_at = Column(DateTime, default=get_ist_time)

    # Claimed = someone has already used this account to get an invite link
    is_claimed    = Column(Boolean,  default=False)

    # Which Telegram user claimed it — prevents one broker ID → multiple Telegram accounts
    claimed_by_telegram_id = Column(String, nullable=True)
    claimed_at             = Column(DateTime, nullable=True)


class TelegramUser(Base):
    """
    Stores onboarding details for users who have verified their phone number.
    """
    __tablename__ = "telegram_users"

    telegram_id = Column(String, primary_key=True)
    phone_number = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    account_size = Column(String, nullable=True)
    created_at = Column(DateTime, default=get_ist_time)


class TelegramMember(Base):
    """
    One row per person added to the VIP Telegram group.
    Broker-aware so you know which broker each member came from.
    Also stores last_trade_date for the future 60-day kick feature.
    """
    __tablename__ = "telegram_members"

    # telegram_id is the user's unique numeric Telegram ID (never changes, unlike username)
    telegram_id   = Column(String,   primary_key=True)
    broker        = Column(String,   primary_key=True)  # which broker they joined through

    telegram_username = Column(String,   nullable=True)   # @username — can change, stored for display
    full_name         = Column(String,   nullable=True)
    account_id        = Column(String,   nullable=False)   # kept as a backup of the verification ID
    client_uid        = Column(String,   nullable=True)
    mt5_id            = Column(String,   nullable=True)
    joined_at         = Column(DateTime, default=get_ist_time)
    last_trade_date   = Column(DateTime, nullable=True)   # latest XM trade seen by the inactivity check
    is_active         = Column(Boolean,  default=True)
    form_link_sent    = Column(Boolean,  default=False)
    # Highest inactivity reminder already sent (e.g. 20/25/29); 0 once they trade again.
    inactivity_reminder_stage = Column(Integer, default=0)


class PendingVerification(Base):
    """
    Temporary one-time invite links waiting to be used.
    Created when the bot validates a broker account ID.
    Deleted / marked used once the user joins the group.
    """
    __tablename__ = "pending_verifications"

    token          = Column(String,   primary_key=True)   # random UUID, used in invite link name
    telegram_id    = Column(String,   index=True)
    broker         = Column(String)
    account_id     = Column(String)
    client_uid     = Column(String,   nullable=True)
    mt5_id         = Column(String,   nullable=True)
    invite_link    = Column(String)                      # the actual t.me/joinchat/... link
    created_at     = Column(DateTime, default=get_ist_time)
    expires_at     = Column(DateTime)
    is_used        = Column(Boolean,  default=False)


class ScheduledJobRun(Base):
    """
    One row per scheduled job run. The composite primary key makes claiming a run
    atomic, so restarts or multiple workers never run the same day's job twice.
    """
    __tablename__ = "scheduled_job_runs"

    job_name   = Column(String, primary_key=True)
    run_date   = Column(String, primary_key=True)   # IST date of the slot, YYYY-MM-DD
    started_at = Column(DateTime, default=get_ist_time)


def init_db():
    Base.metadata.create_all(bind=engine)

    # create_all does not add columns to an existing Render/Postgres database.
    # Add newer fields once so deployments work with existing rows too.
    # client_uid / mt5_id were added by hand with scripts/fill_ids_from_apis.py, not on deploy.
    added_columns = {
        "telegram_users": {
            "full_name": "VARCHAR",
            "account_size": "VARCHAR",
        },
        "telegram_members": {
            "inactivity_reminder_stage": "INTEGER DEFAULT 0",
        },
    }
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table_name, columns in added_columns.items():
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, column_type in columns.items():
                if column_name not in existing_columns:
                    connection.execute(text(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                    ))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
