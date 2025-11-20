import re
import asyncio
import logging
import os
import threading
import time
import json
import random
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from telethon import TelegramClient, events, Button
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
from flask import Flask, jsonify, request, redirect, session, render_template_string
import tweepy
from datetime import datetime

# ====================== X TWEET AYARLARI ======================
TWEET_TITLES = [
    "NEW LIVE BET",
    "HOT SIGNAL ALERT",
    "STRONG PLAY",
    "BANKER OF THE DAY",
    "PREMIUM TIP",
    "EXCLUSIVE BET",
    "HIGH VALUE PLAY",
    "MOONSHOT BET"
]
HASHTAGS = "#BettingTips #FootballBetting #LiveBetting #WinningBets #SportsBetting #FreeTips #OverUnder #BTTS".split()

class TweetManager:
    def __init__(self): self.i = 0
    def title(self): self.i = (self.i + 1) % len(TWEET_TITLES); return TWEET_TITLES[self.i-1]
    def tags(self): return " ".join(random.sample(HASHTAGS, 5))
tm = TweetManager()

# ====================== AYARLAR ======================
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

bot_client = TelegramClient('bot', API_ID, API_HASH)
user_client = TelegramClient('user', API_ID, API_HASH)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or os.urandom(24).hex()

DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")

X_CONSUMER_KEY = os.getenv("X_CONSUMER_KEY")
X_CONSUMER_SECRET = os.getenv("X_CONSUMER_SECRET")
X_ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN")
X_ACCESS_TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET")

client = tweepy.Client(
    consumer_key=X_CONSUMER_KEY,
    consumer_secret=X_CONSUMER_SECRET,
    access_token=X_ACCESS_TOKEN,
    access_token_secret=X_ACCESS_TOKEN_SECRET
) if X_CONSUMER_KEY else None

DEFAULT_ADMIN_ID = int(os.getenv("DEFAULT_ADMIN_ID", "7567322437"))

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("logs/betbot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ====================== DATABASE ======================
def get_connection():
    return psycopg2.connect(dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST, port=DB_PORT, sslmode="require")

def init_db_sync():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS admins(user_id BIGINT PRIMARY KEY, first_name TEXT, is_default BOOLEAN DEFAULT FALSE);""")
    cur.execute("""CREATE TABLE IF NOT EXISTS channels(id SERIAL PRIMARY KEY, channel_id BIGINT UNIQUE, title TEXT, channel_type TEXT);""")
    cur.execute("""CREATE TABLE IF NOT EXISTS processed_messages(chat_id BIGINT, message_id BIGINT, PRIMARY KEY(chat_id, message_id));""")
    cur.execute("""CREATE TABLE IF NOT EXISTS processed_signals(signal_key TEXT PRIMARY KEY);""")
    cur.execute("""CREATE TABLE IF NOT EXISTS signal_mappings(signal_key TEXT PRIMARY KEY, announcement_id BIGINT);""")
    cur.execute("""CREATE TABLE IF NOT EXISTS bot_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT);""")
    conn.commit(); conn.close()
    logger.info("DB hazır")

# Sync fonksiyonlar
def is_message_processed_sync(c, m): conn=get_connection(); cur=conn.cursor(); cur.execute("SELECT 1 FROM processed_messages WHERE chat_id=%s AND message_id=%s",(c,m)); r=cur.fetchone(); conn.close(); return bool(r)
def record_processed_message_sync(c, m): conn=get_connection(); cur=conn.cursor(); cur.execute("INSERT INTO processed_messages VALUES(%s,%s) ON CONFLICT DO NOTHING",(c,m)); conn.commit(); conn.close()
def is_signal_processed_sync(k): conn=get_connection(); cur=conn.cursor(); cur.execute("SELECT 1 FROM processed_signals WHERE signal_key=%s",(k,)); r=cur.fetchone(); conn.close(); return bool(r)
def record_signal_sync(k): conn=get_connection(); cur=conn.cursor(); cur.execute("INSERT INTO processed_signals VALUES(%s) ON CONFLICT DO NOTHING",(k,)); conn.commit(); conn.close()
def get_channels_sync(t): conn=get_connection(); cur=conn.cursor(cursor_factory=RealDictCursor); cur.execute("SELECT * FROM channels WHERE channel_type=%s",(t,)); r=cur.fetchall(); conn.close(); return r
def add_channel_sync(cid, title, ctype): conn=get_connection(); cur=conn.cursor(); cur.execute("INSERT INTO channels(channel_id,title,channel_type) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",(cid,title,ctype)); conn.commit(); conn.close()
def get_bot_setting_sync(k): conn=get_connection(); cur=conn.cursor(); cur.execute("SELECT setting_value FROM bot_settings WHERE setting_key=%s",(k,)); r=cur.fetchone(); conn.close(); return r[0] if r else None
def set_bot_setting_sync(k,v): conn=get_connection(); cur=conn.cursor(); cur.execute("INSERT INTO bot_settings VALUES(%s,%s) ON CONFLICT(setting_key) DO UPDATE SET setting_value=%s",(k,v,v)); conn.commit(); conn.close()
def add_mapping_sync(k, aid): conn=get_connection(); cur=conn.cursor(); cur.execute("INSERT INTO signal_mappings VALUES(%s,%s) ON CONFLICT DO NOTHING",(k,aid)); conn.commit(); conn.close()

# Async wrapper
async def is_message_processed(c,m): return await asyncio.to_thread(is_message_processed_sync,c,m)
async def record_processed_message(c,m): await asyncio.to_thread(record_processed_message_sync,c,m)
async def is_signal_processed(k): return await asyncio.to_thread(is_signal_processed_sync,k)
async def record_signal(k): await asyncio.to_thread(record_signal_sync,k)
async def get_channels(t): return await asyncio.to_thread(get_channels_sync,t)
async def add_channel(cid,title,ctype): await asyncio.to_thread(add_channel_sync,cid,title,ctype)
async def get_bot_setting(k): return await asyncio.to_thread(get_bot_setting_sync,k) or {"running":"running","enabled":"enabled","custom_gif":"https://media.giphy.com/media/3o7abldj0b3rxrZUxW/giphy.gif","allowed_codes":"1,17,21,32,41,48"}.get(k)
async def set_bot_setting(k,v): await asyncio.to_thread(set_bot_setting_sync,k,v)
async def add_mapping(k,aid): await asyncio.to_thread(add_mapping_sync,k,aid)

# ====================== SİNYAL ÇIKARMA ======================
def extract(text):
    text = text.replace("′","'").replace("’","'")
    d = {}
    for l in text.splitlines():
        l = l.strip()
        if any(x in l.lower() for x in [" vs "," - "," — ","–","×"]) or "⚽" in l: d["match"] = l.replace("⚽","").strip()
        if re.search(r"\d+['′+]?\d*", l): m = re.search(r"(\d+[\+′]?\d*)", l); d["min"] = m.group(1)+"'" if m else "?"
        if any(x in l for x in ["❗","➡️","PREDICTION","Tip:"]): d["pred"] = l.split("❗")[-1].split("➡️")[-1].split("Tip:")[-1].strip()
        if "AlertCode:" in l: d["code"] = re.search(r"AlertCode:\s*(\d+)", l); d["code"] = d["code"].group(1) if d["code"] else None
    if all(k in d for k in ["match","min","pred"]):
        key = f"{d['match']}_{d['min']}_{d['pred']}"[:150]
        d["key"] = re.sub(r"[^\w]","",key)
        return d
    return None

# ====================== ANA HANDLER ======================
@user_client.on(events.NewMessage(chats=[c["channel_id"] for c in get_channels_sync("source")]))
async def handler(event):
    if await is_message_processed(event.chat_id, event.id): return
    await record_processed_message(event.chat_id, event.id)
    if (await get_bot_setting("bot_status")) != "running": return

    data = extract(event.message.message or "")
    if not data: return

    # AlertCode filtre
    allowed = (await get_bot_setting("allowed_codes")).split(",")
    if data.get("code") and data["code"] not in allowed: return

    if await is_signal_processed(data["key"]): return
    await record_signal(data["key"])

    msg = f"⚽ {data['match']}\n🏟 {data['min']}\n🎯 {data['pred']}"
    buttons = [
        [Button.url("Melbet", "https://bit.ly/drparsbet")],
        [Button.url("1xBet", "http://bit.ly/3fAja06")]
    ]

    # X paylaşımı
    if client and (await get_bot_setting("x_posting_enabled")) == "enabled":
        tweet = f"{tm.title()}\n\n⚽ {data['match']}\n⏰ {data['min']}\n🎯 {data['pred']}\n\n{tm.tags()}"
        try: client.create_tweet(text=tweet[:280]); logger.info("X atıldı")
        except: pass

    # Kanallara gönder
    targets = await get_channels("target")
    ann_id = None
    for ch in targets:
        sent = await bot_client.send_message(ch["channel_id"], msg, buttons=buttons, file=await get_bot_setting("custom_gif"))
        if not ann_id: ann_id = sent.id
    await add_mapping(data["key"], ann_id)

# ====================== LOGIN SAYFASI ======================
LOGIN_FORM = """<!doctype html><title>Login</title><h2>Phone</h2><form method=post><input name=phone placeholder="+90..." required><button>Send Code</button></form>"""
CODE_FORM = """<!doctype html><title>Code</title><h2>Enter Code</h2><form method=post><input name=code placeholder=12345 required><button>Login</button></form>"""

@app.route('/login', methods=['GET','POST'])
async def login():
    if request.method == 'POST':
        phone = request.form.get('phone')
        session['phone'] = phone
        await user_client.send_code_request(phone)
        return redirect('/code')
    return render_template_string(LOGIN_FORM)

@app.route('/code', methods=['GET','POST'])
async def code():
    if 'phone' not in session: return redirect('/login')
    if request.method == 'POST':
        code = request.form.get('code')
        await user_client.sign_in(session['phone'], code)
        session.pop('phone', None)
        return "<h1>BAŞARILI! Kapatabilirsin.</h1>"
    return render_template_string(CODE_FORM)

@app.route('/health')
def health(): return "OK", 200

# ====================== MAIN ======================
async def main():
    init_db_sync()
    # Default admin ve ayarlar
    conn = get_connection(); cur=conn.cursor()
    cur.execute("INSERT INTO admins VALUES(%s,'Owner',true) ON CONFLICT DO NOTHING",(DEFAULT_ADMIN_ID,))
    for k,v in {"bot_status":"running","x_posting_enabled":"enabled","custom_gif":"https://media.giphy.com/media/3o7abldj0b3rxrZUxW/giphy.gif","allowed_codes":"1,17,21,32,41,48"}.items():
        cur.execute("INSERT INTO bot_settings VALUES(%s,%s) ON CONFLICT DO NOTHING",(k,v))
    conn.commit(); conn.close()

    await bot_client.start(bot_token=BOT_TOKEN)
    await user_client.start()
    logger.info("BOT ÇALIŞIYOR!")
    await asyncio.Event().wait()

if __name__ == '__main__':
    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
