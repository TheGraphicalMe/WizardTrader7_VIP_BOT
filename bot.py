"""
bot.py
──────
Telegram bot that guides users through verification and sends a one-time invite link.
"""

import uuid
import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
    TypeHandler,
    ApplicationHandlerStop,
)
from telegram.error import TelegramError

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_GROUP_ID, SUPPORTED_BROKERS, ALLOWED_USERS, BROKER_AFFILIATE_INFO
from database import SessionLocal, BrokerAccount, TelegramMember, PendingVerification, TelegramUser

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
REQUEST_PHONE   = 0
CHOOSE_BROKER   = 1
ENTER_ACCOUNT   = 2

# Cache for photo file_ids to speed up sending
BROKER_PHOTO_FILE_IDS = {}

# ── Helper: parse group ID ────────────────────────────────────────────────────
def _group_id():
    try:
        return int(TELEGRAM_GROUP_ID)
    except ValueError:
        return TELEGRAM_GROUP_ID


# ═════════════════════════════════════════════════════════════════════════════
# HANDLERS
# ═════════════════════════════════════════════════════════════════════════════

async def whitelist_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    # If ALLOWED_USERS is not set, allow everyone (default behavior)
    if not ALLOWED_USERS:
        return

    user_id = str(update.effective_user.id)
    username = update.effective_user.username.lower() if update.effective_user.username else ""
    
    allowed_list = [x.strip().lower() for x in ALLOWED_USERS.split(",") if x.strip()]

    if user_id not in allowed_list and username not in allowed_list:
        if update.message:
            await update.message.reply_text(
                "🚧 *Under Maintenance*\n\n"
                "This bot is currently undergoing scheduled maintenance. Please try again later.",
                parse_mode="Markdown"
            )
        elif update.callback_query:
            await update.callback_query.answer(
                "Bot is currently under maintenance. Please try again later.", 
                show_alert=True
            )
        raise ApplicationHandlerStop()

async def ask_to_choose_broker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    buttons, row = [], []
    
    # ⚠️ ACTION REQUIRED: Replace these placeholder IDs with your actual Custom Emoji IDs
    broker_custom_emojis = {
        "vantage": "6064274490857103869", # Example ID, replace with real Vantage emoji ID
        "xm": "6061996135260628880",      # Example ID, replace with real XM emoji ID
        "winpro": "6062062303526790930",  # Example ID, replace with real Winpro emoji ID
        "exness": "6062115647020606488",  # Example ID, replace with real Exness emoji ID
        "delta": "6062270334562740009"    # Example ID, replace with real Delta emoji ID
    }
    
    for broker in SUPPORTED_BROKERS:
        emoji_id = broker_custom_emojis.get(broker.lower(), "")
        
        button_kwargs = {
            "text": f" {broker.capitalize()}", 
            "callback_data": f"broker:{broker}"
        }
        
        if emoji_id:
            button_kwargs["icon_custom_emoji_id"] = emoji_id
            
        row.append(InlineKeyboardButton(**button_kwargs))
        
        # Display 1 button per row exactly like the screenshot
        if len(row) == 1:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await update.message.reply_text(
        "📌 *Which broker did you open an account with?*\n"
        "_Only accounts opened through our affiliate link are eligible._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return CHOOSE_BROKER


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return REQUEST_PHONE
    user = update.effective_user

    db = SessionLocal()
    try:
        db_user = db.query(TelegramUser).filter(TelegramUser.telegram_id == str(user.id)).first()
        if not db_user:
            keyboard = [[KeyboardButton("📱 Share Phone Number", request_contact=True)]]
            reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
            await update.message.reply_text(
                f"👋 Welcome, {user.first_name}!\n\n"
                "To join our *Active Traders Community*, we first need to verify your phone number.\n\n"
                "Please tap the button below to share your phone number securely.",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            return REQUEST_PHONE
    finally:
        db.close()

    await update.message.reply_text(f"👋 Welcome back, {user.first_name}!")
    return await ask_to_choose_broker(update, context)


async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return REQUEST_PHONE

    if not update.message.contact:
        await update.message.reply_text("Please use the '📱 Share Phone Number' button below to share your contact.")
        return REQUEST_PHONE
        
    user = update.effective_user
    contact = update.message.contact
    
    if contact.user_id != user.id:
        await update.message.reply_text("❌ Please share your own contact, not someone else's.")
        return REQUEST_PHONE
        
    phone_number = contact.phone_number
    
    db = SessionLocal()
    try:
        db_user = TelegramUser(telegram_id=str(user.id), phone_number=phone_number)
        db.add(db_user)
        db.commit()
    except Exception as e:
        logger.error(f"Error saving user phone: {e}")
        db.rollback()
    finally:
        db.close()
        
    await update.message.reply_text(
        "✅ Phone number verified successfully!",
        reply_markup=ReplyKeyboardRemove()
    )
    
    return await ask_to_choose_broker(update, context)


async def choose_broker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ENTER_ACCOUNT
    await query.answer()

    broker = query.data.split(":")[1]
    context.user_data["broker"] = broker

    # Delete the previous text message to replace it with a photo message
    try:
        await query.message.delete()
    except TelegramError:
        pass

    caption = (
        f"✅ Broker selected: *{broker.capitalize()}*\n\n"
        f"Now please enter your *{broker.capitalize()} Account ID (UID)*.\n\n"
        "📋 Check the image above to see where to find your Account ID in your broker dashboard.\n\n"
        "_Type your Account ID and press Send:_"
    )

    import os
    image_path = f"assets/{broker}_uid.png"
    if not os.path.exists(image_path):
        image_path = f"assets/{broker}_uid.jpg"
    
    if os.path.exists(image_path):
        if broker in BROKER_PHOTO_FILE_IDS:
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=BROKER_PHOTO_FILE_IDS[broker],
                caption=caption,
                parse_mode="Markdown"
            )
        else:
            with open(image_path, "rb") as photo:
                msg = await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=photo,
                    caption=caption,
                    parse_mode="Markdown"
                )
                if msg and msg.photo:
                    BROKER_PHOTO_FILE_IDS[broker] = msg.photo[-1].file_id
    else:
        # Fallback if image is missing
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=caption + f"\n\n*(Note: Add an image at '{image_path}' to show the UID guide here)*",
            parse_mode="Markdown"
        )
        
    return ENTER_ACCOUNT


async def enter_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text or not update.effective_user:
        return ConversationHandler.END
    account_id = update.message.text.strip()

    if account_id.startswith("#"):
        await update.message.reply_text("❌ Please enter your Account ID (UID) without the '#' symbol.")
        return ENTER_ACCOUNT

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
            requests = [t for t in requests if now - t < timedelta(hours=24)]
            
            if len(requests) >= 10:
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
        if not account and broker == "winpro":
            await update.message.reply_text("⏳ Checking Winpro systems for your account and deposits, please wait...")
            from winpro import verify_winpro_account
            is_valid, reason = await verify_winpro_account(account_id, db)
            
            if is_valid:
                account = db.query(BrokerAccount).filter(
                    BrokerAccount.account_id == account_id,
                    BrokerAccount.broker     == broker,
                ).first()
            else:
                if reason == "not_under_ib":
                    info = BROKER_AFFILIATE_INFO.get("winpro", {})
                    link = info.get("link", "N/A")
                    code = info.get("code", "N/A")
                    await update.message.reply_text(
                        "❌ *Winpro Verification Failed*\n\n"
                        "You are not registered under our affiliate link.\n"
                        "Please register using the official link below:\n\n"
                        f"📌 *Partner Link:* {link}\n\n"
                        f"🔹 *Partner Code:* `{code}`\n\n"
                        "After completing the registration or partner code change, please join our bot:\n"
                        "🤖 @SwappySyndicateBot\n\n"
                        "If you need any assistance, feel free to contact us.",
                        parse_mode="Markdown"
                    )
                elif reason.startswith("insufficient_deposit"):
                    current_deposit = reason.split(":")[1]
                    await update.message.reply_text(
                        "❌ *Verification Failed: Insufficient Deposits*\n\n"
                        f"Your account `{account_id}` is correctly registered under us, but your total successful deposits are currently **${current_deposit}**.\n\n"
                        "**Requirement:** You need a cumulative deposit of **at least $50** to join the Active Traders Community.\n\n"
                        "Once you have deposited the required amount, wait a few minutes for it to be approved, then try again.\n\n"
                        "Send /start to try again.",
                        parse_mode="Markdown"
                    )
                elif reason == "invalid_format":
                    await update.message.reply_text(
                        "❌ *Verification Failed: Invalid Format*\n\n"
                        "Winpro Account IDs should only contain numbers.\n\n"
                        "Send /start to try again.",
                        parse_mode="Markdown"
                    )
                else:
                    await update.message.reply_text(
                        "❌ *Verification Failed*\n\n"
                        "An error occurred while checking your Winpro account. Please try again later or contact support.\n\n"
                        "Send /start to try again.",
                        parse_mode="Markdown"
                    )
                return ConversationHandler.END

        if not account:
            info = BROKER_AFFILIATE_INFO.get(broker.lower(), {})
            b_name = info.get("name", broker.capitalize())
            link = info.get("link", "N/A")
            code = info.get("code", "N/A")

            reply_markup = None
            if broker.lower() == "vantage":
                reply_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✉️ Change Partner (Email Format)", callback_data="vantage_change_partner")]
                ])

            await update.message.reply_text(
                f"❌ *{b_name} Verification Failed*\n\n"
                "You are not registered under our affiliate link.\n"
                "Please register using the official link below:\n\n"
                f"📌 *Partner Link:* {link}\n\n"
                f"🔹 *Partner Code:* `{code}`\n\n"
                "After completing the registration or partner code change, please join our bot:\n"
                "🤖 @SwappySyndicateBot\n\n"
                "If you need any assistance, feel free to contact us.",
                parse_mode="Markdown",
                reply_markup=reply_markup,
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


async def vantage_change_partner_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        mail_text = (
            "📧 *Vantage Partner Change Mail Format*\n\n"
            "If you already have a Vantage account, send an email with the following details:\n\n"
            "📬 *To:* `india.care@vantagemarkets.com`\n"
            "📑 *CC:* `jahnvi.ahuja@vantagemarkets.com`\n"
            "📌 *Subject:* `Request to Map Account under IB 23143035`\n\n"
            "📝 *Email Body (Tap to copy):*\n"
            "```\n"
            "Hello Team,\n\n"
            "I want to work with Name: Harshit Patel Ram Krishna Patel\n"
            "IB : 23143035 \n"
            "Kindly map my account under him as soon as possible.\n"
            "```\n\n"
            "Once sent, wait for Vantage support to confirm your account mapping, then try verifying again with /start."
        )
        mailto_url = (
            "mailto:india.care@vantagemarkets.com"
            "?cc=jahnvi.ahuja@vantagemarkets.com"
            "&subject=Request%20to%20Map%20Account%20under%20IB%2023143035"
            "&body=Hello%20Team%2C%0A%0AI%20want%20to%20work%20with%20Name%3A%20Harshit%20Patel%20Ram%20Krishna%20Patel%0AIB%20%3A%2023143035%20%0AKindly%20map%20my%20account%20under%20him%20as%20soon%20as%20possible."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✉️ Open Email App", url=mailto_url)]
        ])
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=mail_text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )


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

    if ALLOWED_USERS:
        app.add_handler(TypeHandler(Update, whitelist_middleware), group=-1)

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("(?i)^start$"), start)
        ],
        states={
            REQUEST_PHONE: [
                MessageHandler(filters.CONTACT, receive_phone),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)
            ],
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
    app.add_handler(CallbackQueryHandler(vantage_change_partner_handler, pattern="^vantage_change_partner$"))

    return app