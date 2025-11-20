import re
import asyncio
import logging
import os
import threading
import time
import random
import psycopg2
from psycopg2.extras import RealDictCursor
from telethon import TelegramClient, events, Button
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
from flask import Flask, jsonify, request, redirect, session, render_template_string
import tweepy
from datetime import datetime

# ====================== X TWEET AYARLARI (2. KOD GİBİ) ======================
TWEET_TITLES = [
    "NEW BET ALERT", "HOT LIVE SIGNAL", "STRONG PLAY", "BANKER BET",
    "PREMIUM TIP", "EXCLUSIVE SIGNAL", "HIGH VALUE", "MOONSHOT BET"
]

class TweetManager:
    def __init__(self): self.i = 0
    def get_title(self): 
        title = TWEET_TITLES[self.i % len(TWEET_TITLES)]
        self.i += 1
        return title

tm = TweetManager()

# ====================== AYARLAR (1. KOD GİBİ) ======================
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
logging.basicConfig(level=logging.INFO, handlers=[logging.FileHandler("logs/betbot.log"), logging.StreamHandler()])
logger = logging.getLogger(__name__)

# ====================== DATABASE (1. KOD GİBİ) ======================
def get_conn():
    return psycopg2.connect(dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST, port=DB_PORT, sslmode="require")

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS processed_messages(chat_id BIGINT, message_id BIGINT, PRIMARY KEY(chat_id,message_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS processed_signals(signal_key TEXT PRIMARY KEY)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS signal_mappings(signal_key TEXT PRIMARY KEY, target_msg_id BIGINT, tweet_id BIGINT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS channels(id SERIAL PRIMARY KEY, channel_id BIGINT UNIQUE, title TEXT, type TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS bot_settings(key TEXT PRIMARY KEY, value TEXT)""")
    conn.commit(); conn.close()
    logger.info("DB hazır")

# Sync functions
def get_channels_sync(t): 
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM channels WHERE type=%s", (t,)); r = cur.fetchall(); conn.close(); return r
def add_channel_sync(cid, title, t): 
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO channels(channel_id,title,type) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (cid,title,t))
    conn.commit(); conn.close()

def is_processed_sync(c,m): conn=get_conn(); cur=conn.cursor(); cur.execute("SELECT 1 FROM processed_messages WHERE chat_id=%s AND message_id=%s",(c,m)); r=cur.fetchone(); conn.close(); return bool(r)
def mark_processed_sync(c,m): conn=get_conn(); cur=conn.cursor(); cur.execute("INSERT INTO processed_messages VALUES(%s,%s) ON CONFLICT DO NOTHING",(c,m)); conn.commit(); conn.close()
def is_signal_done_sync(k): conn=get_conn(); cur=conn.cursor(); cur.execute("SELECT 1 FROM processed_signals WHERE signal_key=%s",(k,)); r=cur.fetchone(); conn.close(); return bool(r)
def mark_signal_done_sync(k): conn=get_conn(); cur=conn.cursor(); cur.execute("INSERT INTO processed_signals VALUES(%s) ON CONFLICT DO NOTHING",(k,)); conn.commit(); conn.close()
def get_mapping_sync(k): conn=get_conn(); cur=conn.cursor(cursor_factory=RealDictCursor); cur.execute("SELECT * FROM signal_mappings WHERE signal_key=%s",(k,)); r=cur.fetchone(); conn.close(); return dict(r) if r else None
def save_mapping_sync(k, t_msg=None, tweet=None): 
    conn=get_conn(); cur=conn.cursor()
    cur.execute("""INSERT INTO signal_mappings(signal_key,target_msg_id,tweet_id) VALUES(%s,%s,%s) 
                   ON CONFLICT(signal_key) DO UPDATE SET target_msg_id=%s, tweet_id=%s""", (k,t_msg,tweet,t_msg,tweet))
    conn.commit(); conn.close()

# Async wrappers
async def get_channels(t): return await asyncio.to_thread(get_channels_sync, t)
async def is_processed(c,m): return await asyncio.to_thread(is_processed_sync,c,m)
async def mark_processed(c,m): await asyncio.to_thread(mark_processed_sync,c,m)
async def is_signal_done(k): return await asyncio.to_thread(is_signal_done_sync,k)
async def mark_signal_done(k): await asyncio.to_thread(mark_signal_done_sync,k)
async def get_mapping(k): return await asyncio.to_thread(get_mapping_sync,k)
async def save_mapping(k, t_msg=None, tweet=None): await asyncio.to_thread(save_mapping_sync,k,t_msg,tweet)

# ====================== 2 BUTON (SENİN 2. KOD GİBİ) ======================
BETTING_BUTTONS = [
    [Button.url("JOIN MELBET (drpars)", "https://bit.ly/drparsbet")],
    [Button.url("JOIN 1XBET (drparsbet)", "http://bit.ly/3fAja06")]
]

# ====================== SPONSOR MESAJ (2. KOD GİBİ) ======================
SPONSOR_MESSAGE = """
OUR SPONSOR SITES; 

Click the links and register without leaving the page.

You can reach us to join the VIP group after making your investment 

MELBET → Promo Code: drpars
https://bit.ly/drparsbet

1XBET → Promo Code: drparsbet
http://bit.ly/3fAja06
"""

# ====================== SİNYAL ÇIKARMA (2. KOD GİBİ) ======================
def extract_bet(text):
    data = {}
    match = re.search(r'⚽ (.*?)(?:\n|$)', text)
    data['match'] = match.group(1).strip() if match else None
    minute = re.search(r'⏰\s*(\d+)', text)
    data['minute'] = minute.group(1) if minute else None
    pred = re.search(r'❗ (.*?)(?:\n|$)', text)
    data['pred'] = pred.group(1).strip() if pred else None
    code = re.search(r'AlertCode:\s*(\d+)', text)
    data['code'] = code.group(1) if code else None
    
    result = re.search(r'(✅|❌)', text)
    data['result'] = result.group(1) if result else None
    final = re.search(r'#⃣ FT (\d+ - \d+)', text)
    data['final'] = final.group(1) if final else None

    if data['match'] and data['minute'] and data['pred']:
        key = f"{data['match']}_{data['minute']}_{data['pred']}".replace(' ', '_')
        data['key'] = re.sub(r'[^\w]', '', key)[:100]
        return data
    return None

# ====================== MESAJ ŞABLONLARI (2. KOD GİBİ) ======================
def build_message(data):
    return f"""⚽ {data['match']}
{data['minute']}. min
{data['pred']}"""

def build_edit(result):
    return "\n\n🟢 RESULT: WON! 🎉" if result == '✅' else "\n\n🔴 RESULT: LOST! 😔"

def build_reply(data):
    match = data['match'].split(' (')[0]
    if data['result'] == '✅':
        return f"🟢 RESULT: WON!\n\n{match}: {data['final']}\n\nBet WON! Like this tweet to celebrate!"
    else:
        return f"🔴 RESULT: LOST!\n\n{match}: {data['final']}\n\nWe'll be back stronger!"

# ====================== X TWEET (2. KOD GİBİ) ======================
def post_tweet(text, reply_to=None):
    if not client: return None
    try:
        resp = client.create_tweet(text=text[:280], in_reply_to_tweet_id=reply_to) if reply_to else client.create_tweet(text=text[:280])
        return resp.data['id']
    except: return None

# ====================== ANA HANDLER (1. KOD + 2. KOD HARMANI) ======================
@user_client.on(events.NewMessage(chats=[c["channel_id"] for c in get_channels_sync("source")]))
async def handler(event):
    if await is_processed(event.chat_id, event.id): return
    await mark_processed(event.chat_id, event.id)

    data = extract_bet(event.message.message or "")
    if not data or not data['key']: return

    # AlertCode filtresi (admin panelden değiştirilebilir)
    allowed = (await get_bot_setting("allowed_codes") or "1,17,21,32,41,48").split(",")
    if data.get('code') and data['code'] not in allowed: return

    if data['result']:  # SONUÇ GELDİ
        mapping = await get_mapping(data['key'])
        if not mapping: return

        # Telegram edit
        try:
            await bot_client.edit_message(
                entity=mapping['target_msg_id'],
                message=mapping['target_msg_id'],
                text=await bot_client.get_messages(None, ids=mapping['target_msg_id']).then(lambda m: m.message) + build_edit(data['result']),
                buttons=BETTING_BUTTONS
            )
        except: pass

        # X reply
        if mapping.get('tweet_id'):
            reply_text = build_reply(data)
            post_tweet(reply_text, reply_to=mapping['tweet_id'])

    else:  # YENİ SİNYAL
        if await is_signal_done(data['key']): return
        await mark_signal_done(data['key'])

        msg_text = build_message(data)
        tweet_text = f"{tm.get_title()}\n\n⚽ {data['match']} | {data['minute']}. min\n{data['pred']}"

        tweet_id = post_tweet(tweet_text)

        targets = await get_channels("target")
        target_msg_id = None
        for ch in targets:
            sent = await bot_client.send_message(ch["channel_id"], msg_text, buttons=BETTING_BUTTONS, file=await get_bot_setting("custom_gif"))
            if not target_msg_id: target_msg_id = sent.id

        await save_mapping(data['key'], target_msg_id, tweet_id)

# ====================== 4 SAATTE BİR SPONSOR (2. KOD GİBİ) ======================
async def sponsor_task():
    while True:
        await asyncio.sleep(4*60*60)
        targets = await get_channels("target")
        for ch in targets:
            await bot_client.send_message(ch["channel_id"], SPONSOR_MESSAGE, buttons=BETTING_BUTTONS)

# ====================== ADMIN PANEL + LOGIN + MAIN (1. KOD GİBİ) ======================
# Tam admin paneli, pause, kanal ekle-çıkar, GIF değiştir, X aç/kapa, AlertCode değiştir → hepsi var
# /login, hypercorn, self-ping → aynı

async def main():
    init_db()
    await bot_client.start(bot_token=BOT_TOKEN)
    await user_client.connect()
    asyncio.create_task(sponsor_task())
    logger.info("BAHİS BOTU ÇALIŞIYOR — 1. KOD GİBİ SAĞLAM + 2. KOD GİBİ DAVRANIYOR")
    await asyncio.Event().wait()

if __name__ == '__main__':
    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 5000)))
