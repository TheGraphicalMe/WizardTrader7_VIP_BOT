"""
bot.py
──────
Telegram bot that guides users through verification and sends a one-time invite link.
"""

import uuid
import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_GROUP_ID, SUPPORTED_BROKERS
from database import SessionLocal, BrokerAccount, TelegramMember, PendingVerification

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
CHOOSE_BROKER   = 1
ENTER_ACCOUNT   = 2

# ── Helper: parse group ID ────────────────────────────────────────────────────
def _group_id():
    try:
        return int(TELEGRAM_GROUP_ID)
    except ValueError:
        return TELEGRAM_GROUP_ID


# ═════════════════════════════════════════════════════════════════════════════
# HANDLERS
# ═════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return CHOOSE_BROKER
    user = update.effective_user

    buttons, row = [], []
    for broker in SUPPORTED_BROKERS:
        row.append(InlineKeyboardButton(broker.capitalize(), callback_data=f"broker:{broker}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await update.message.reply_text(
        f"👋 Welcome, {user.first_name}!\n\n"
        "To join our *Active Traders Community*, I need to verify your broker account.\n\n"
        "📌 *Which broker did you open an account with?*\n"
        "_Only accounts opened through our affiliate link are eligible._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return CHOOSE_BROKER


async def choose_broker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ENTER_ACCOUNT
    await query.answer()

    broker = query.data.split(":")[1]
    context.user_data["broker"] = broker

    await query.edit_message_text(
        f"✅ Broker selected: *{broker.capitalize()}*\n\n"
        f"Now please enter your *{broker.capitalize()} Account ID*.\n\n"
        "📋 You can find your Account ID in your broker dashboard after logging in.\n\n"
        "_Type your Account ID and press Send:_",
        parse_mode="Markdown",
    )
    return ENTER_ACCOUNT


async def enter_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text or not update.effective_user:
        return ConversationHandler.END
    account_id = update.message.text.strip()
    broker     = context.user_data.get("broker", "")
    user       = update.effective_user
    telegram_id = str(user.id)

    if not broker:
        await update.message.reply_text("⚠️ Something went wrong. Please start over by sending /start")
        return ConversationHandler.END

    if broker == "vantage" and not account_id.isdigit():
        await update.message.reply_text(
            "❌ *Account not found or Timeout Active*\n\n"
            "Your account UID is wrong, or you must try after 3 hours if you have recently registered.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    db = SessionLocal()
    try:
        # ── 1. Find the account in DB ─────────────────────────────────────────
        account = db.query(BrokerAccount).filter(
            BrokerAccount.account_id == account_id,
            BrokerAccount.broker     == broker,
        ).first()

        # ── 1.5 Dynamic check for Vantage ──────────────────────────────────────
        if not account and broker == "vantage":
            now = datetime.utcnow()
            timeout_until = context.user_data.get('vantage_timeout_until')
            if timeout_until and now < timeout_until:
                await update.message.reply_text(
                    "❌ *Account not found or Timeout Active*\n\n"
                    "Your account UID is wrong, or you must try after 3 hours if you have recently registered.",
                    parse_mode="Markdown"
                )
                return ConversationHandler.END
                
            requests = context.user_data.get('vantage_requests', [])
            requests = [t for t in requests if now - t < timedelta(minutes=15)]
            
            if len(requests) >= 3:
                context.user_data['vantage_timeout_until'] = now + timedelta(hours=3)
                await update.message.reply_text(
                    "❌ *Account not found or Timeout Active*\n\n"
                    "Your account UID is wrong, or you must try after 3 hours if you have recently registered.",
                    parse_mode="Markdown"
                )
                return ConversationHandler.END
                
            requests.append(now)
            context.user_data['vantage_requests'] = requests

            await update.message.reply_text("⏳ Checking Vantage systems for your account, please wait...")
            from vantage import verify_vantage_account
            is_valid = await verify_vantage_account(account_id, db)
            if is_valid:
                account = db.query(BrokerAccount).filter(
                    BrokerAccount.account_id == account_id,
                    BrokerAccount.broker     == broker,
                ).first()

        # ── 1.6 Dynamic check for Winpro ──────────────────────────────────────
        # if not account and broker == "winpro":
        #     await update.message.reply_text("⏳ Checking Winpro systems for your account and deposits, please wait...")
        #     from winpro import verify_winpro_account
        #     is_valid, reason = await verify_winpro_account(account_id, db)
        #     
        #     if is_valid:
        #         account = db.query(BrokerAccount).filter(
        #             BrokerAccount.account_id == account_id,
        #             BrokerAccount.broker     == broker,
        #         ).first()
        #     else:
        #         if reason == "not_under_ib":
        #             await update.message.reply_text(
        #                 "❌ *Verification Failed: Account Not Found*\n\n"
        #                 f"We could not find account `{account_id}` registered under our affiliate link.\n\n"
        #                 "**What to do:**\n"
        #                 "Please ensure you created your Winpro account using our exact referral link. "
        #                 "If you already had a Winpro account, you must open a new one using our link to join the Active Traders Community.\n\n"
        #                 "Send /start to try again.",
        #                 parse_mode="Markdown"
        #             )
        #         elif reason.startswith("insufficient_deposit"):
        #             current_deposit = reason.split(":")[1]
        #             await update.message.reply_text(
        #                 "❌ *Verification Failed: Insufficient Deposits*\n\n"
        #                 f"Your account `{account_id}` is correctly registered under us, but your total successful deposits are currently **${current_deposit}**.\n\n"
        #                 "**Requirement:** You need a cumulative deposit of **at least $50** to join the Active Traders Community.\n\n"
        #                 "Once you have deposited the required amount, wait a few minutes for it to be approved, then try again.\n\n"
        #                 "Send /start to try again.",
        #                 parse_mode="Markdown"
        #             )
        #         elif reason == "invalid_format":
        #             await update.message.reply_text(
        #                 "❌ *Verification Failed: Invalid Format*\n\n"
        #                 "Winpro Account IDs should only contain numbers.\n\n"
        #                 "Send /start to try again.",
        #                 parse_mode="Markdown"
        #             )
        #         else:
        #             await update.message.reply_text(
        #                 "❌ *Verification Failed*\n\n"
        #                 "An error occurred while checking your Winpro account. Please try again later or contact support.\n\n"
        #                 "Send /start to try again.",
        #                 parse_mode="Markdown"
        #             )
        #         return ConversationHandler.END

        if not account:
            await update.message.reply_text(
                "❌ *Account not found*\n\n"
                f"We couldn't find account `{account_id}` for *{broker.capitalize()}* in our system.\n\n"
                "This usually means:\n"
                "• You opened your account *before* clicking our affiliate link\n"
                "• You typed the Account ID incorrectly\n\n"
                "✅ *What to do:*\n"
                "1. Double-check your Account ID in your broker dashboard\n"
                "2. If you just signed up, wait 5 minutes and try again\n"
                "3. Contact support if the issue continues\n\n"
                "Send /start to try again.",
                parse_mode="Markdown",
            )
            return ConversationHandler.END

        # ── 2. Check if account already claimed by SOMEONE ELSE ──────────────
        if account.is_claimed and account.claimed_by_telegram_id != telegram_id:
            await update.message.reply_text(
                "❌ *Account already used*\n\n"
                f"Account `{account_id}` has already been used to join the Active Traders Community.\n\n"
                "Each broker account can only be linked to one Telegram account.\n\n"
                "If you think this is a mistake, contact support.\n\n"
                "Send /start to try again.",
                parse_mode="Markdown",
            )
            return ConversationHandler.END

        # ── 3. Check if THIS Telegram user already joined via this broker ─────
        existing = db.query(TelegramMember).filter(
            TelegramMember.telegram_id == telegram_id,
            TelegramMember.broker      == broker,
        ).first()

        if existing:
            msg_text = (
                "ℹ️ *You're already a member!*\n\n"
                f"Your Telegram account is already in the Active Traders Community via *{broker.capitalize()}*.\n\n"
                "Open Telegram and look for the Active Traders Community in your chat list.\n"
                "If you were removed and want to rejoin, contact support."
            )
            reply_markup = None
            if not getattr(existing, 'form_link_sent', False):
                msg_text += "\n\n📝 *Please fill the form below for FREE Smart AI Lite access:*"
                existing.form_link_sent = True
                db.commit()
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📝 Smart AI Lite Form", url="https://forms.gle/suoKtSqeRh1KasBj7")
                ]])

            await update.message.reply_text(
                msg_text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            return ConversationHandler.END

        # ── 4. Generate a one-time invite link ────────────────────────────────
        await update.message.reply_text("⏳ Verifying your account...")

        try:
            expire_time = datetime.utcnow() + timedelta(hours=5, minutes=30) + timedelta(hours=24)
            invite = await context.bot.create_chat_invite_link(
                chat_id     = _group_id(),
                name        = f"ATC-{broker}-{account_id[:8]}",
                member_limit= 1,
                expire_date = expire_time,
            )
        except TelegramError as e:
            logger.error(f"Failed to create invite link: {e}")
            await update.message.reply_text(
                "❌ *Something went wrong on our end.*\n\n"
                "We couldn't generate your invite link right now.\n"
                "Please try again in a few minutes or contact support.\n\n"
                "Send /start to try again.",
                parse_mode="Markdown",
            )
            return ConversationHandler.END

        # ── 5. Save pending verification to DB ────────────────────────────────
        token = str(uuid.uuid4())
        db.add(PendingVerification(
            token       = token,
            telegram_id = telegram_id,
            broker      = broker,
            account_id  = account_id,
            invite_link = invite.invite_link,
            expires_at  = expire_time,
            is_used     = False,
        ))

        account.is_claimed              = True
        account.claimed_by_telegram_id  = telegram_id
        account.claimed_at              = datetime.utcnow() + timedelta(hours=5, minutes=30)

        db.add(TelegramMember(
            telegram_id       = telegram_id,
            broker            = broker,
            telegram_username = user.username,
            full_name         = user.full_name,
            account_id        = account_id,
            joined_at         = datetime.utcnow() + timedelta(hours=5, minutes=30),
            is_active         = True,
            form_link_sent    = True,
        ))

        db.commit()

        # ── 6. Send the invite link ────────────────────────────────────────────
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Join Active Traders Community", url=invite.invite_link)],
            [InlineKeyboardButton("📝 Smart AI Lite Form", url="https://forms.gle/suoKtSqeRh1KasBj7")]
        ])

        await update.message.reply_text(
            "✅ *Verified! You're in!*\n\n"
            f"Account `{account_id}` ({broker.capitalize()}) has been confirmed.\n\n"
            "👇 Tap the buttons below to join the Active Traders Community and get Smart AI Lite access:\n\n"
            "⚠️ _The community invite link is valid for 24 hours and can only be used once._\n\n"
            "📝 *Please fill the form below for FREE Smart AI Lite access:*",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        logger.info(f"Invite sent — telegram_id={telegram_id} broker={broker} account={account_id}")

    except Exception as e:
        logger.error(f"Error in enter_account: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ An unexpected error occurred. Please try again or contact support.\n\n"
            "Send /start to try again."
        )
    finally:
        db.close()

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END
    await update.message.reply_text("Cancelled. Send /start whenever you're ready to verify your account.")
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        "ℹ️ *Help*\n\n"
        "This bot adds verified broker account holders to our Active Traders Community.\n\n"
        "*How it works:*\n"
        "1. Open a trading account using our affiliate link\n"
        "2. Send /start to this bot\n"
        "3. Select your broker and enter your Account ID\n"
        "4. Get your one-time invite link instantly\n\n"
        "*Commands:*\n"
        "/start — Begin verification\n"
        "/cancel — Cancel and start over\n"
        "/help — Show this message",
        parse_mode="Markdown",
    )

def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("(?i)^start$"), start)
        ],
        states={
            CHOOSE_BROKER: [
                CallbackQueryHandler(choose_broker, pattern="^broker:")
            ],
            ENTER_ACCOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_account)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start",  start),
            MessageHandler(filters.Regex("(?i)^start$"), start),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("help", help_command))

    return app