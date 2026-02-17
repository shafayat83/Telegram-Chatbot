import logging
import os
import asyncio
import sqlite3
import requests
import io
from datetime import datetime, timedelta
from typing import List, Dict
from aiohttp import web

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import Update, constants, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
    filters, CallbackQueryHandler
)

# Load environment variables
load_dotenv()

# --- CONFIG ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_TOKEN = os.getenv("OPENROUTER_TOKEN")
AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-4o-mini")
HF_TOKEN = os.getenv("HF_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7449421046"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "@AssassinCodar")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/AssassinCodar")
COINBASE_LINK = os.getenv("COINBASE_LINK")
PORT = int(os.getenv("PORT", "8080"))  # Required for Choreo

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


# --- DATABASE ---
def init_db():
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (user_id INTEGER PRIMARY KEY, username TEXT, ref_by INTEGER, 
                  ref_count INTEGER DEFAULT 0, is_pro INTEGER DEFAULT 0, 
                  expiry TEXT, joined_channel INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()


init_db()

# --- AI CLIENT ---
ai_client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_TOKEN)


# --- UTILITIES ---

def is_pro_valid(user_id):
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("SELECT is_pro, expiry FROM users WHERE user_id=?", (user_id,))
    res = c.fetchone()
    conn.close()
    if not res or res[0] == 0: return False
    try:
        expiry_date = datetime.strptime(res[1], '%Y-%m-%d')
        return datetime.now() < expiry_date
    except:
        return False


def get_user_status(user_id):
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("SELECT is_pro, expiry, ref_count FROM users WHERE user_id=?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res if res else (0, "None", 0)


def split_message(text: str, max_length: int = 4000) -> List[str]:
    chunks = []
    while len(text) > max_length:
        split_at = text.rfind("\n", 0, max_length)
        if split_at == -1: split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    chunks.append(text)
    return chunks


async def is_subscribed(bot, user_id):
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in [constants.ChatMemberStatus.MEMBER, constants.ChatMemberStatus.ADMINISTRATOR,
                                 constants.ChatMemberStatus.OWNER]
    except:
        return False


async def keep_typing(context, chat_id, stop_event):
    while not stop_event.is_set():
        await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4)
        except:
            continue


# --- HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,))

    if not c.fetchone():
        ref_by = int(args[0]) if args and args[0].isdigit() else None
        if ref_by == user.id: ref_by = None
        c.execute("INSERT INTO users (user_id, username, ref_by) VALUES (?, ?, ?)", (user.id, user.username, ref_by))
        if ref_by:
            c.execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id=?", (ref_by,))
            c.execute("SELECT ref_count FROM users WHERE user_id=?", (ref_by,))
            count = c.fetchone()[0]
            try:
                name = f"@{user.username}" if user.username else user.first_name
                notif = f"🔔 *New Referral!*\n{name} joined.\nTotal Referrals: `{count}`"
                if count % 5 == 0:
                    exp = (datetime.now() + timedelta(days=12)).strftime('%Y-%m-%d')
                    c.execute("UPDATE users SET is_pro=1, expiry=? WHERE user_id=?", (exp, ref_by))
                    notif += "\n🎉 12 Days PRO added automatically!"
                await context.bot.send_message(ref_by, notif, parse_mode='Markdown')
            except:
                pass
        conn.commit()
    conn.close()

    if not await is_subscribed(context.bot, user.id):
        kb = [[InlineKeyboardButton("Join Channel", url=CHANNEL_LINK)],
              [InlineKeyboardButton("✅ Verify Join", callback_data="verify_join")]]
        await update.message.reply_text(f"👋 Welcome {user.first_name}!\nPlease join our channel to use the AI.",
                                        reply_markup=InlineKeyboardMarkup(kb))
    else:
        btns = [['Deep Research 🔍', 'Web Search 🌐'], ['Generate Image 🎨', 'My Account 👤']]
        await update.message.reply_text(f"Welcome back {user.first_name}!",
                                        reply_markup=ReplyKeyboardMarkup(btns, resize_keyboard=True))


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    uid = update.effective_user.id

    if text == 'My Account 👤':
        s = get_user_status(uid)
        pro_active = is_pro_valid(uid)
        ref = f"https://t.me/{(await context.bot.get_me()).username}?start={uid}"
        status_str = "PRO ✅" if pro_active else "Free ❌"
        msg = f"👤 *Account Details*\nStatus: {status_str}\nExpiry: {s[1]}\nRefs: {s[2]}\n\nRef Link: `{ref}`"

        kb = []
        if not pro_active:
            kb.append([InlineKeyboardButton("💳 Buy PRO ($5)", url=COINBASE_LINK)])
            kb.append([InlineKeyboardButton("📤 Send Payment Proof", callback_data="send_proof")])
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif text == 'Generate Image 🎨':
        if not is_pro_valid(uid):
            await update.message.reply_text("❌ PRO membership required.")
        else:
            context.user_data['mode'] = 'image'
            await update.message.reply_text("🎨 Send an image prompt. I will generate it.")

    elif text in ['Deep Research 🔍', 'Web Search 🌐']:
        context.user_data['mode'] = 'research' if 'Deep' in text else 'web'
        await update.message.reply_text(f"✅ {text} enabled. Send your prompt.")


async def process_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg = update.message
    mode = context.user_data.get('mode', 'chat')

    if not await is_subscribed(context.bot, uid):
        await msg.reply_text("❌ Join channel first.")
        return

    # Handle Proof Submission
    if context.user_data.get('state') == 'waiting_proof':
        admin_msg = f"📩 *New Payment Proof*\nUser: {update.effective_user.first_name}\nID: `{uid}`"
        kb = [[InlineKeyboardButton("Approve ✅", callback_data=f"adm_app:{uid}"),
               InlineKeyboardButton("Reject ❌", callback_data=f"adm_rej:{uid}")]]

        if msg.photo:
            await context.bot.send_photo(ADMIN_ID, msg.photo[-1].file_id, caption=admin_msg,
                                         reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else:
            await context.bot.send_message(ADMIN_ID, f"{admin_msg}\nProof: {msg.text}",
                                           reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

        await msg.reply_text("✅ Proof sent! Admin will verify soon.")
        context.user_data['state'] = None
        return

    # AI Logic
    stop = asyncio.Event()
    task = asyncio.create_task(keep_typing(context, update.effective_chat.id, stop))
    try:
        if mode == 'image':
            url = "https://api-inference.huggingface.co/models/runwayml/stable-diffusion-v1-5"
            headers = {"Authorization": f"Bearer {HF_TOKEN}"}
            res = requests.post(url, headers=headers, json={"inputs": msg.text})
            await msg.reply_photo(photo=io.BytesIO(res.content))
            context.user_data['mode'] = 'chat'
        else:
            # Language Matching
            sys = "Detect user language and respond in the same language. Use Markdown."
            if mode == 'research': sys += " Provide very long scientific analysis."

            resp = await ai_client.chat.completions.create(
                model=AI_MODEL,
                messages=[{"role": "system", "content": sys}, {"role": "user", "content": msg.text}]
            )
            for chunk in split_message(resp.choices[0].message.content):
                try:
                    await msg.reply_text(chunk, parse_mode='Markdown')
                except:
                    await msg.reply_text(chunk)
                await asyncio.sleep(0.5)
    except Exception as e:
        await msg.reply_text(f"⚠️ Error: {e}")
    finally:
        stop.set()
        await task


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    data = query.data

    if data == "verify_join":
        if await is_subscribed(context.bot, uid):
            await query.message.delete()
            btns = [['Deep Research 🔍', 'Web Search 🌐'], ['Generate Image 🎨', 'My Account 👤']]
            await context.bot.send_message(uid, "✅ Verified!",
                                           reply_markup=ReplyKeyboardMarkup(btns, resize_keyboard=True))
        else:
            await query.answer("Join channel first!", show_alert=True)

    elif data == "send_proof":
        context.user_data['state'] = 'waiting_proof'
        await query.message.reply_text("📤 Send your Screenshot or Transaction ID now.")
        await query.answer()

    elif data.startswith("adm_") and uid == ADMIN_ID:
        action, tid = data.split(":")
        tid = int(tid)
        conn = sqlite3.connect("bot_data.db")
        c = conn.cursor()
        if "app" in action:
            exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
            c.execute("UPDATE users SET is_pro=1, expiry=? WHERE user_id=?", (exp, tid))
            await context.bot.send_message(tid, "🎉 *PRO Activated!* Your buttons are now updated.")
            kb = [[InlineKeyboardButton("Cancel Sub 🚫", callback_data=f"adm_can:{tid}")]]
            await query.edit_message_text(f"✅ Approved: `{tid}`", reply_markup=InlineKeyboardMarkup(kb))
        elif "can" in action:
            c.execute("UPDATE users SET is_pro=0, expiry='Cancelled' WHERE user_id=?", (tid,))
            await context.bot.send_message(tid, "⚠️ Subscription Cancelled.")
            kb = [[InlineKeyboardButton("Re-Approve ✅", callback_data=f"adm_app:{tid}")]]
            await query.edit_message_text(f"🚫 Cancelled: `{tid}`", reply_markup=InlineKeyboardMarkup(kb))
        elif "rej" in action:
            await context.bot.send_message(tid, "❌ Rejected. Send valid proof.")
            await query.edit_message_text(f"❌ Rejected: `{tid}`")
        conn.commit()
        conn.close()


# --- CHOREO HEALTH CHECK SERVER ---
async def handle_health(request):
    return web.Response(text="Bot is running")


async def start_server():
    app = web.Application()
    app.router.add_get('/', handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()


# --- MAIN ---
async def run_bot():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(
        MessageHandler(filters.Regex('^(Deep Research 🔍|Web Search 🌐|Generate Image 🎨|My Account 👤)$'), handle_buttons))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & (~filters.COMMAND), process_ai))

    # Start Health Server
    await start_server()

    # Start Bot
    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run_bot())