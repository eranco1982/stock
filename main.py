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

#  הגדרת לוגים
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
DB_PATH = 'stocks_v5.db'  # גרסה v5 - תבצע דריסה חד-פעמית לכולם
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
        # הוספנו עמודה בשם was_overwritten כדי לזכור מי כבר עבר דריסה חד-פעמית
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
        logging.info(f"Yahoo blocked for {ticker_symbol}, trying Google News...")

    try:
        clean_ticker = ticker_symbol.split('-')[0]
        rss_url = f"https://news.google.com/rss/search?q={clean_ticker}+stock+news&hl=en-US&gl=US&ceid=US:en"
        feed = feedparser.parse(rss_url)
        if feed.entries:
            analysis = f"🗞 **תקציר חדשות עבור {ticker_symbol} (Google):**\n\n"
            for entry in feed.entries[:3]:
                analysis += f"• [{entry.title}]({entry.link})\n\n"
            news_cache[ticker_symbol] = (now, analysis)
            return analysis
    except:
        pass

    inv_url = f"https://www.investing.com/search/?q={ticker_symbol.split('-')[0]}"
    return f"⚠️ לא ניתן לשלוף תקציר כרגע.\n\n[צפה בחדשות ב-Investing.com]({inv_url})"

async def get_prices_text(user_id, user_name):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT name, ticker, quantity, purchase_price FROM stocks WHERE user_id = ?", (user_id,))
        rows = c.fetchall()
        conn.close()
        greeting = get_greeting(user_name)
        if not rows: 
            return f"{greeting}\n\nהתיק שלך ריק כרגע.", None
        
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
        logging.error(f"Price load error: {e}")
        return "⚠️ שגיאה בטעינת הנתונים.", None

# --- פקודת התחלה עם בדיקת דריסה חד-פעמית ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # רישום המשתמש אם לא קיים
    c.execute("INSERT OR IGNORE INTO users (user_id, first_name) VALUES (?, ?)", (user_id, user.first_name))
    
    # בדיקה האם המשתמש כבר עבר את הדריסה החד-פעמית
    c.execute("SELECT was_overwritten FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    was_overwritten = result[0] if result else 0
    
    # אם הוא עדיין לא עבר דריסה - נדרוס לו את התיק עכשיו בפעם היחידה
    if not was_overwritten:
        c.execute("DELETE FROM stocks WHERE user_id = ?", (user_id,))
        default_stocks = [
            ('נאסד"ק 100', 'QQQ', user_id, 0, 0),
            ('S&P 500', 'SPY', user_id, 0, 0),
            ('ביטקוין', 'BTC-USD', user_id, 0, 0),
            ('דולר/שקל', 'USDILS=X', user_id, 0, 0),
            ('מדד תא 35', 'TA35.TA', user_id, 0, 0)
        ]
        c.executemany("INSERT INTO stocks (name, ticker, user_id, quantity, purchase_price) VALUES (?, ?, ?, ?, ?)", default_stocks)
        
        # סימון ב-DB שהמשתמש הזה כבר עבר את הדריסה ולא צריך לגעת בו יותר
        c.execute("UPDATE users SET was_overwritten = 1 WHERE user_id = ?", (user_id,))
        msg_suffix = "\n\nהתיק שלך עודכן באופן חד-פעמי עם מניות ברירת המחדל! 📈"
    else:
        msg_suffix = ""

    conn.commit()
    conn.close()
    
    kb = [['📊 הצג את כל השערים'], ['➕ הוספת מניה', '❌ הסרת מניה']]
    if user_id == ADMIN_ID: 
        kb.append(['📊 סטטיסטיקה', '💎 ניהול פרימיום', '📢 הודעה לכולם'])
        
    await update.message.reply_text(
        f"{get_greeting(user.first_name)}\nברוך הבא לבוט המניות האישי שלך!{msg_suffix}", 
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )

# --- טיפול בהודעות ולחיצות כפתור ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    text = update.message.text
    state = context.user_data.get('state')

    main_kb = [['📊 הצג את כל השערים'], ['➕ הוספת מניה', '❌ הסרת מניה']]
    if user_id == ADMIN_ID: main_kb.append(['📊 סטטיסטיקה', '💎 ניהול פרימיום', '📢 הודעה לכולם'])
    reply_markup = ReplyKeyboardMarkup(main_kb, resize_keyboard=True)

    if user_id == ADMIN_ID:
        if text == '📊 סטטיסטיקה':
            conn = sqlite3.connect(DB_PATH); c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM users"); u_cnt = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1"); p_cnt = c.fetchone()[0]
            await update.message.reply_text(f"👥 משתמשים: {u_cnt}\n💎 פרימיום: {p_cnt}")
            return
        elif text == '💎 ניהול פרימיום':
            conn = sqlite3.connect(DB_PATH); c = conn.cursor()
            c.execute("SELECT user_id, first_name, is_premium FROM users WHERE user_id != ?", (ADMIN_ID,))
            users = c.fetchall(); conn.close()
            kb = [[InlineKeyboardButton(f"{'💎' if p else '👤'} {n}", callback_data=f"tgp_{uid}")] for uid, n, p in users]
            await update.message.reply_text("בחר משתמש לשינוי סטטוס:", reply_markup=InlineKeyboardMarkup(kb))
            return
        elif text == '📢 הודעה לכולם':
            await update.message.reply_text("כתוב את ההודעה לשידור:", reply_markup=ReplyKeyboardRemove())
            context.user_data['state'] = 'BROADCAST'; return

    if text == '📊 הצג את כל השערים':
        msg, kb = await get_prices_text(user_id, user_name)
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=kb, disable_web_page_preview=True)
    elif text == '➕ הוספת מניה':
        await update.message.reply_text("סימול המניה (למשל AAPL):", reply_markup=ReplyKeyboardRemove())
        context.user_data['state'] = 'T'
    elif text == '❌ הסרת מניה':
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("SELECT name, ticker FROM stocks WHERE user_id = ?", (user_id,))
        rows = c.fetchall(); conn.close()
        if not rows: await update.message.reply_text("אין מניות להסרה."); return
        kb = [[InlineKeyboardButton(f"❌ {n}", callback_data=f"del_{t}")] for n, t in rows]
        await update.message.reply_text("בחר מניה להסרה:", reply_markup=InlineKeyboardMarkup(kb))
    
    elif state == 'T':
        context.user_data['temp_t'] = text.upper(); context.user_data['state'] = 'N'
        await update.message.reply_text("שם המניה (למשל אפל):")
    elif state == 'N':
        context.user_data['temp_n'] = text; context.user_data['state'] = 'P'
        await update.message.reply_text("מחיר קנייה? (או 'דלג')", reply_markup=ReplyKeyboardMarkup([['דלג ⏩']], resize_keyboard=True))
    elif state == 'P':
        context.user_data['temp_p'] = float(text) if text != 'דלג ⏩' else 0
        context.user_data['state'] = 'Q'
        await update.message.reply_text("כמות? (או 'דלג'):")
    elif state == 'Q':
        qty = float(text) if text != 'דלג ⏩' else 0
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("INSERT INTO stocks (name, ticker, user_id, quantity, purchase_price) VALUES (?, ?, ?, ?, ?)",
                  (context.user_data['temp_n'], context.user_data['temp_t'], user_id, qty, context.user_data['temp_p']))
        conn.commit(); conn.close(); context.user_data.clear()
        await update.message.reply_text("✅ המניה נוספה בהצלחה!", reply_markup=reply_markup)
    elif state == 'BROADCAST':
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("SELECT user_id FROM users"); users = c.fetchall(); conn.close()
        for u in users:
            try: await context.bot.send_message(chat_id=u[0], text=f"📢 **הודעה מהמערכת:**\n\n{text}", parse_mode='Markdown')
            except: pass
        await update.message.reply_text("✅ ההודעה נשלחה לכולם!", reply_markup=reply_markup)
        context.user_data.clear()

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    user_id = update.effective_user.id
    if query.data.startswith("analyze_"):
        analysis = await get_stock_analysis(query.data.split("_")[1])
        await context.bot.send_message(chat_id=user_id, text=analysis, parse_mode='Markdown', disable_web_page_preview=True)
    elif query.data == "refresh":
        msg, kb = await get_prices_text(user_id, update.effective_user.first_name)
        try: await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=kb, disable_web_page_preview=True)
        except: pass
    elif query.data.startswith("tgp_"):
        target_id = int(query.data.split("_")[1])
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("UPDATE users SET is_premium = 1 - is_premium WHERE user_id = ?", (target_id,))
        conn.commit(); conn.close()
        await query.edit_message_text("✅ סטטוס פרימיום עודכן!")
    elif query.data.startswith("del_"):
        ticker = query.data.replace("del_", "")
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("DELETE FROM stocks WHERE ticker = ? AND user_id = ?", (ticker, user_id))
        conn.commit(); conn.close()
        await query.edit_message_text(f"✅ {ticker} הוסרה מהתיק.")

def main():
    init_db()
    token = os.getenv("TOKEN")
    if not token:
        logging.error("No TOKEN found in environment variables!")
        return

    keep_alive()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logging.info("Bot is starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
