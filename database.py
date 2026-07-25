from sqlalchemy import create_engine, Column, String, DateTime, Boolean, BigInteger, Integer, Text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from datetime import datetime, timedelta
from config import DATABASE_URL

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine        = create_engine(DATABASE_URL, connect_args=_connect_args)
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
    registered_at = Column(DateTime, default=get_ist_time)

    # Claimed = someone has already used this account to get an invite link
    is_claimed    = Column(Boolean,  default=False)

    # Which Telegram user claimed it — prevents one broker ID → multiple Telegram accounts
    claimed_by_telegram_id = Column(String, nullable=True)
    claimed_at             = Column(DateTime, nullable=True)


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
    account_id        = Column(String,   nullable=False)
    joined_at         = Column(DateTime, default=get_ist_time)
    last_trade_date   = Column(DateTime, nullable=True)   # for future 60-day kick feature
    is_active         = Column(Boolean,  default=True)
    form_link_sent    = Column(Boolean,  default=False)


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
    invite_link    = Column(String)                        # the actual t.me/joinchat/... link
    created_at     = Column(DateTime, default=get_ist_time)
    expires_at     = Column(DateTime)
    is_used        = Column(Boolean,  default=False)


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
