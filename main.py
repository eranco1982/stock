import os
import sqlite3
import pandas as pd
import pytz
import asyncio
import logging
import feedparser
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread
from yahooquery import Ticker
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler

# הגדרת לוגים
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- הגדרות שרת Keep-Alive עבור Render ---
server = Flask('')

@server.route('/')
def home():
    return "Bot is alive and running!"

def run():
    server.run(host='0.0.0.0', port=10000)

def keep_alive():
    t = Thread(target=run)
    t.start()

# --- הגדרות מערכת ---
DB_PATH = 'stocks_v5.db'
ADMIN_ID = 7969303152 
DEFAULT_LIMIT = 10
PREMIUM_LIMIT = 50

# זיכרון זמני לחדשות
news_cache = {}

def init_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('CREATE TABLE IF NOT EXISTS stocks (name TEXT, ticker TEXT, user_id INTEGER, quantity REAL DEFAULT 0, purchase_price REAL DEFAULT 0)')
        c.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_name TEXT, is_premium INTEGER DEFAULT 0, was_overwritten INTEGER DEFAULT 0)')
        conn.commit()
        conn.close()
        logging.info("Database initialized successfully.")
    except Exception as e: 
        logging.error(f"DB Error: {e}")

def get_greeting(first_name):
    israel_tz = pytz.timezone('Asia/Jerusalem')
    hour = datetime.now(israel_tz).hour
    if 5 <= hour < 12: greet = "בוקר טוב"
    elif 12 <= hour < 18: greet = "צהריים טובים"
    elif 18 <= hour < 22: greet = "ערב טוב"
    else: greet = "לילה טוב"
    return f"{greet}, {first_name}! 💎"

# --- לוגיקת נתונים וניתוח ---

async def get_stock_analysis(ticker_symbol):
    now = datetime.now()
    if ticker_symbol in news_cache:
        cached_time, cached_data = news_cache[ticker_symbol]
        if now - cached_time < timedelta(minutes=15): return cached_data
    try:
        ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        t = Ticker(ticker_symbol, user_agent=ua)
        news_data = t.news(3)
        if news_data and isinstance(news_data, list):
            analysis = f"🧐 **חדשות עבור {ticker_symbol} (Yahoo):**\n\n"
            for item in news_data:
                analysis += f"• [{item.get('title')}]({item.get('link')})\n\n"
            news_cache[ticker_symbol] = (now, analysis)
            return analysis
    except:
        logging.info(f"Yahoo blocked for {ticker_symbol}")
    return f"⚠️ לא ניתן לשלוף תקציר כרגע."

async def get_prices_text(user_id, user_name):
    try:
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("SELECT name, ticker, quantity, purchase_price FROM stocks WHERE user_id = ?", (user_id,))
        rows = c.fetchall(); conn.close()
        greeting = get_greeting(user_name)
        if not rows: return f"{greeting}\n\nהתיק שלך ריק כרגע.", None
        
        t = Ticker([r[1] for r in rows], asynchronous=True, formatted=False)
        prices = t.price
        msg = f"{greeting}\nמצב התיק שלך:\n━━━━━━━━━━━━━━━\n\n"
        kb = []
        for name, ticker, qty, buy_p in rows:
            d = prices.get(ticker, {})
            curr_p = d.get('regularMarketPrice', 0)
            change = d.get('regularMarketChangePercent', 0) * 100
            icon = "🟢" if change >= 0 else "🔴"
            symbol = "₪" if ".TA" in ticker or "USDILS" in ticker else "$"
            msg += f"🔹 **{name}**\nשער: `{symbol}{curr_p:,.2f}` ({icon} {change:+.2f}%)\n\n"
            kb.append([InlineKeyboardButton(f"🔍 ניתוח: {name}", callback_data=f"analyze_{ticker}")])
        kb.append([InlineKeyboardButton("🔄 רענון נתונים", callback_data="refresh")])
        return msg, InlineKeyboardMarkup(kb)
    except Exception as e:
        return "⚠️ שגיאה בטעינת הנתונים.", None

# --- פקודת התחלה ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    
    c.execute("INSERT OR IGNORE INTO users (user_id, first_name) VALUES (?, ?)", (user_id, user.first_name))
    c.execute("SELECT was_overwritten FROM users WHERE user_id = ?", (user_id,))
    was_overwritten = c.fetchone()[0]
    
    if was_overwritten == 0:
        # דריסה ראשונית עם המדדים הנכונים
        default_stocks = [
            ('מדד נאסד"ק 100', '^NDX', user_id, 0, 0),
            ('מדד S&P 500', '^GSPC', user_id, 0, 0),
            ('ביטקוין', 'BTC-USD', user_id, 0, 0),
            ('דולר/שקל', 'USDILS=X', user_id, 0, 0),
            ('מדד תא 35', 'TA35.TA', user_id, 0, 0)
        ]
        c.execute("DELETE FROM stocks WHERE user_id = ?", (user_id,))
        c.executemany("INSERT INTO stocks (name, ticker, user_id, quantity, purchase_price) VALUES (?, ?, ?, ?, ?)", default_stocks)
        c.execute("UPDATE users SET was_overwritten = 1 WHERE user_id = ?", (user_id,))
        msg_suffix = "\n\nהתיק שלך עודכן עם המדדים המדויקים! 📈"
    else:
        msg_suffix = ""

    conn.commit(); conn.close()
    kb = [['📊 הצג את כל השערים'], ['➕ הוספת מניה', '❌ הסרת מניה']]
    if user_id == ADMIN_ID: kb.append(['📊 סטטיסטיקה', '💎 ניהול פרימיום', '📢 הודעה לכולם'])
    await update.message.reply_text(f"{get_greeting(user.first_name)}\nברוך הבא!{msg_suffix}", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

# --- שאר הפונקציות (handle_message וכו') ---
# (השארתי את הלוגיקה שלך לניהול הודעות)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user_id = update.effective_user.id
    text = update.message.text
    state = context.user_data.get('state')
    main_kb = [['📊 הצג את כל השערים'], ['➕ הוספת מניה', '❌ הסרת מניה']]
    if user_id == ADMIN_ID: main_kb.append(['📊 סטטיסטיקה', '💎 ניהול פרימיום', '📢 הודעה לכולם'])
    reply_markup = ReplyKeyboardMarkup(main_kb, resize_keyboard=True)

    if text == '📊 הצג את כל השערים':
        msg, kb = await get_prices_text(user_id, update.effective_user.first_name)
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=kb, disable_web_page_preview=True)
    elif text == '➕ הוספת מניה':
        await update.message.reply_text("סימול המניה (למשל AAPL):", reply_markup=ReplyKeyboardRemove())
        context.user_data['state'] = 'T'
    elif text == '❌ הסרת מניה':
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("SELECT name, ticker FROM stocks WHERE user_id = ?", (user_id,))
        rows = c.fetchall(); conn.close()
        if not rows: await update.message.reply_text("אין מניות."); return
        kb = [[InlineKeyboardButton(f"❌ {n}", callback_data=f"del_{t}")] for n, t in rows]
        await update.message.reply_text("בחר להסרה:", reply_markup=InlineKeyboardMarkup(kb))
    elif state == 'T':
        context.user_data['temp_t'] = text.upper(); context.user_data['state'] = 'N'
        await update.message.reply_text("שם המניה:")
    elif state == 'N':
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("INSERT INTO stocks (name, ticker, user_id) VALUES (?, ?, ?)", (text, context.user_data['temp_t'], user_id))
        conn.commit(); conn.close(); context.user_data.clear()
        await update.message.reply_text("✅ נוספה!", reply_markup=reply_markup)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data.startswith("del_"):
        ticker = query.data.replace("del_", "")
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("DELETE FROM stocks WHERE ticker = ? AND user_id = ?", (ticker, update.effective_user.id))
        conn.commit(); conn.close()
        await query.edit_message_text(f"✅ {ticker} הוסרה.")

def main():
    init_db()
    token = os.getenv("TOKEN")
    keep_alive()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
