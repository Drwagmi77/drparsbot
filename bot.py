import os
import re
import asyncio
import logging
import time
import tweepy
import psycopg2
from datetime import date
from psycopg2.extras import RealDictCursor
from telethon import TelegramClient, events, Button
from flask import Flask, jsonify, request, redirect, session, render_template_string
from hypercorn.asyncio import serve
from hypercorn.config import Config
from asgiref.wsgi import WsgiToAsgi

# ----------------------------------------------------------------------
# 1. ORTAM DEĞİŞKENLERİ VE YAPILANDIRMA
# ----------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

try:
    API_ID = int(os.environ.get("API_ID"))
    API_HASH = os.environ.get("API_HASH")
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    
    DB_HOST = os.environ.get("DB_HOST")
    DB_PORT = os.environ.get("DB_PORT", "5432")
    DB_NAME = os.environ.get("DB_NAME")
    DB_USER = os.environ.get("DB_USER")
    DB_PASS = os.environ.get("DB_PASS")
    
    X_CONSUMER_KEY = os.environ.get("X_CONSUMER_KEY")
    X_CONSUMER_SECRET = os.environ.get("X_CONSUMER_SECRET")
    X_ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN")
    X_ACCESS_TOKEN_SECRET = os.environ.get("X_ACCESS_TOKEN_SECRET")
    
    DEFAULT_ADMIN_ID = int(os.getenv("DEFAULT_ADMIN_ID", "0"))
except Exception as e:
    logger.critical(f"Missing environment variables: {e}")

# --- GLOBAL AYARLAR ---
ALLOWED_ALERT_CODES = {'17', '41', '32', '48', '1', '21'} 
DAILY_TWEET_LIMIT = 15

CLOSING_TWEET_TEXT = """🚫 DAILY X LIMIT REACHED (15/15)

🚀 The winning streak continues exclusively on our Telegram channel!

Don't miss the rest of today's high-confidence signals.

👇 JOIN VIP FREE:
https://t.me/aitipsterwon
"""

SCHEDULED_MESSAGE = """
✅ OUR SPONSOR SITES; 

⛔️Click the links and register without leaving the page.

💯 You can reach us to join the VIP group after making your investment 👇

🟢🟡Melbet 👉Promo Code: drpars
https://bit.ly/drparsbet

🔴🔵1xbet 👉Promo Code: drparsbet
bit.ly/3fAja06
"""

BETTING_BUTTONS = [
    [
        Button.url("JOIN MELBET (drpars)", "https://bit.ly/drparsbet"),
        Button.url("JOIN 1XBET (drparsbet)", "http://bit.ly/3fAja06")
    ]
]

ALERT_TEMPLATES = {
    '1': {'title': "🎯 LIVE GOAL SIGNAL 🎯", 'bet_type': "NEXT GOAL AFTER 65' (+0.5)", 'stake': "4/5", 'analysis': "High scoring pattern"},
    '17': {'title': "🎯 LIVE TOTAL GOALS SIGNAL 🎯", 'bet_type': "TOTAL GOALS 2.5 OVER BEFORE 60'", 'stake': "4/5", 'analysis': "Fast paced game"},
    '21': {'title': "🎯 LIVE CORNER SIGNAL 🎯", 'bet_type': "TOTAL CORNERS - MATCH RESULT", 'stake': "3/5", 'analysis': "High corner frequency"},
    '32': {'title': "🎯 LIVE TOTAL GOALS SIGNAL 🎯", 'bet_type': "TOTAL GOALS 3.5 OVER - MATCH RESULT", 'stake': "3/5", 'analysis': "High scoring game"},
    '41': {'title': "🎯 LIVE GOAL SIGNAL 🎯", 'bet_type': "3RD GOAL BEFORE 60' (V2)", 'stake': "4/5", 'analysis': "Early goals expected"},
    '47': {'title': "🎯 LIVE CORNER SIGNAL 🎯", 'bet_type': "TOTAL CORNERS - FIRST HALF", 'stake': "4/5", 'analysis': "High corner frequency"},
    '48': {'title': "🎯 LIVE CORNER SIGNAL 🎯", 'bet_type': "TOTAL CORNERS - MATCH RESULT", 'stake': "4/5", 'analysis': "High corner frequency"}
}

bot_client = TelegramClient('bot_session', API_ID, API_HASH)
user_client = TelegramClient('user_session', API_ID, API_HASH)
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24).hex()

x_client = None
try:
    if all([X_CONSUMER_KEY, X_CONSUMER_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET]):
        x_client = tweepy.Client(
            consumer_key=X_CONSUMER_KEY,
            consumer_secret=X_CONSUMER_SECRET,
            access_token=X_ACCESS_TOKEN,
            access_token_secret=X_ACCESS_TOKEN_SECRET
        )
        logger.info("✅ X Client initialized with OAuth 1.0a")
    else:
        logger.warning("❌ X Credentials missing in ENV")
except Exception as e:
    logger.error(f"❌ X Init Error: {e}")

bot_running = True 

LOGIN_FORM = """<!doctype html><title>Login</title><h2>Phone</h2><form method=post><input name=phone placeholder="+90..." required><button>Send Code</button></form>"""
CODE_FORM = """<!doctype html><title>Code</title><h2>Enter Code</h2><form method=post><input name=code placeholder=12345 required><button>Login</button></form>"""

# ----------------------------------------------------------------------
# 2. VERİTABANI YÖNETİMİ (ID SİSTEMİ EKLENDİ)
# ----------------------------------------------------------------------

def get_connection():
    try:
        return psycopg2.connect(dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST, port=DB_PORT, sslmode="require")
    except Exception as e:
        logger.error(f"DB Connect Fail: {e}")
        raise e

def init_db_sync():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Ana Tablo
        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_signals (
                signal_key TEXT PRIMARY KEY,
                source_channel TEXT NOT NULL,
                target_message_id BIGINT,
                tweet_id BIGINT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'PENDING',
                source_message_id BIGINT
            );
        """)
        
        # MIGRATION: Eğer tablo varsa ve sütun eksikse ekle
        try:
            cur.execute("ALTER TABLE processed_signals ADD COLUMN IF NOT EXISTS source_message_id BIGINT;")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_source_msg_id ON processed_signals(source_message_id);")
            conn.commit()
        except:
            conn.rollback()

        # Günlük Sayaç Tablosu
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                date DATE PRIMARY KEY,
                tweet_count INTEGER DEFAULT 0
            );
        """)
        
        # Kanallar Tablosu
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                channel_id BIGINT UNIQUE,
                title TEXT,
                channel_type TEXT
            );
        """)
        conn.commit()
        logger.info("✅ DB Tables Ready")
    except Exception as e:
        logger.error(f"DB Init Error: {e}")
    finally:
        if conn: conn.close()

# 🔥 YENİ FONKSİYON: ID İLE ARAMA 🔥
def get_signal_by_source_id(source_msg_id):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM processed_signals WHERE source_message_id = %s", (source_msg_id,))
            result = cur.fetchone()
            return dict(result) if result else None
    except Exception as e:
        return None
    finally:
        if conn: conn.close()

# 🔥 GÜNCELLENMİŞ KAYIT FONKSİYONU 🔥
def record_processed_signal(signal_key, target_message_id, tweet_id, source_message_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO processed_signals (signal_key, source_channel, target_message_id, tweet_id, status, source_message_id) 
                VALUES (%s, %s, %s, %s, 'PENDING', %s) 
                ON CONFLICT (signal_key) DO UPDATE SET 
                target_message_id = EXCLUDED.target_message_id, 
                tweet_id = EXCLUDED.tweet_id,
                source_message_id = EXCLUDED.source_message_id;
            """, (signal_key, "source", target_message_id, tweet_id, source_message_id))
            conn.commit()
            logger.info(f"💾 Signal Recorded with ID: {source_message_id}")
            return True
    except Exception as e:
        logger.error(f"❌ Record Signal Error: {e}")
        return False
    finally:
        if conn: conn.close()

def get_daily_tweet_count():
    conn = get_connection()
    today = date.today()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tweet_count FROM daily_stats WHERE date = %s", (today,))
            result = cur.fetchone()
            return result[0] if result else 0
    except Exception as e:
        return 0
    finally:
        if conn: conn.close()

def increment_daily_tweet_count():
    conn = get_connection()
    today = date.today()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO daily_stats (date, tweet_count) VALUES (%s, 1)
                ON CONFLICT (date) DO UPDATE SET tweet_count = daily_stats.tweet_count + 1
                RETURNING tweet_count;
            """, (today,))
            new_count = cur.fetchone()[0]
            conn.commit()
            return new_count
    except Exception as e:
        return 0
    finally:
        if conn: conn.close()

def get_channels_sync(t): 
    source_id = os.environ.get("SOURCE_CHANNEL")
    target_id = os.environ.get("TARGET_CHANNEL")
    channels = []
    try:
        if t == 'source' and source_id:
            channels.append({"channel_id": int(source_id) if source_id.startswith('-100') else source_id})
        elif t == 'target' and target_id:
            channels.append({"channel_id": int(target_id) if target_id.startswith('-100') else target_id})
    except: pass
    return channels

# ----------------------------------------------------------------------
# 3. VERİ İŞLEME VE FORMATLAMA
# ----------------------------------------------------------------------

def extract_bet_data(message_text):
    data = {}
    
    # Skor
    match_score_match = re.search(r'⚽[️\s]*(.*?)\s*\(.*?\)', message_text, re.DOTALL)
    if match_score_match:
        data['maç_skor'] = match_score_match.group(0).strip().replace('⚽', '').replace('️', '').strip()
    else:
        match_alt = re.search(r'([A-Za-z\s]+-\s*[A-Za-z\s]+)\s*\(\s*(\d+\s*-\s*\d+)\s*\)', message_text)
        data['maç_skor'] = f"{match_alt.group(1)} ({match_alt.group(2)})" if match_alt else None
    
    # Lig, Dakika, Tahmin
    lig_match = re.search(r'🏟\s*(.*?)\n', message_text)
    data['lig'] = lig_match.group(1).strip() if lig_match else None
    
    dakika_match = re.search(r'⏰\s*(\d+)\s*', message_text)
    data['dakika'] = dakika_match.group(1).strip() if dakika_match else None
    
    tahmin_match = re.search(r'❗[️\s]*(.*?)\n', message_text)
    if tahmin_match:
        tahmin_text = tahmin_match.group(1).strip()
        corner_match = re.search(r'(\d+\.?\d*)\s*(üst|over|alt|under)', tahmin_text, re.IGNORECASE)
        if corner_match:
            data['corner_number'] = corner_match.group(1)
            data['corner_type'] = corner_match.group(2)
        
        tahmin_en = re.search(r'\((.*?)\)', tahmin_text)
        data['tahmin'] = tahmin_en.group(1).strip() if tahmin_en else tahmin_text
    else:
        data['tahmin'] = None
    
    alert_code_match = re.search(r'👉\s*AlertCode:\s*(\d+)', message_text)
    data['alert_code'] = alert_code_match.group(1).strip() if alert_code_match else None
    
    result_match = re.search(r'([✅❌])', message_text)
    data['result_icon'] = result_match.group(1) if result_match else None

    # Live Update
    live_score_match = re.search(r'⏰\s*(\d+)\s*⚽[️\s]*(\d+\s*-\s*\d+)', message_text)
    if live_score_match:
        data['live_minute'] = live_score_match.group(1).strip()
        data['live_score'] = live_score_match.group(2).strip()
        data['is_live_update'] = True
    else:
        data['is_live_update'] = False

    # Maç Bitti mi?
    ft_match = re.search(r'#⃣\s*FT\s*(\d+\s*-\s*\d+)', message_text)
    if ft_match:
        data['match_ended'] = True
        data['final_score'] = ft_match.group(1).strip()
    else:
        data['match_ended'] = False

    # Signal Key
    if all([data.get('maç_skor'), data.get('tahmin')]):
        maç_temiz = re.sub(r'[\(\)]', '', data['maç_skor']).strip().replace(' ', '_').replace('-', '')
        tahmin_temiz = re.sub(r'[^\w\s]', '', data['tahmin']).strip().replace(' ', '_')
        if data.get('dakika'):
            data['signal_key'] = f"{maç_temiz}_{data['dakika']}_{tahmin_temiz}"
        else:
            data['signal_key'] = f"{maç_temiz}_{tahmin_temiz}"
    else:
        data['signal_key'] = None
    
    return data if data['signal_key'] else None

def build_telegram_message(data):
    alert_code = data.get('alert_code')
    template = ALERT_TEMPLATES.get(alert_code, {})
    
    if data.get('corner_number'):
        corner_number = data['corner_number']
        bet_type = f"TOTAL CORNERS {corner_number} OVER"
        analysis = f"High corner frequency, {corner_number}+ corners expected"
    else:
        bet_type = template.get('bet_type', data['tahmin'])
        analysis = template.get('analysis', 'Professional betting signal')
    
    return f"""
{template.get('title', '🎯 BETTING SIGNAL 🎯')}

🏟 {data['maç_skor']}
🏆 {data['lig']}  
⏰ {data['dakika']}' | 📊 Score: {data['maç_skor'].split('(')[-1].replace(')', '') if '(' in data['maç_skor'] else 'Live'}

🎯 {bet_type}

📈 Analysis: {analysis}
💸 Stake: {template.get('stake', '3/5')}
⚡ Time: Bet now!
"""

def build_telegram_live_update(data):
    return f"""
🟢 LIVE UPDATE 🟢

🏟 {data['maç_skor']}
🏆 {data['lig']}  
⏰ {data['live_minute']}' | 📊 Score: {data['live_score']}

🎯 {data['tahmin']} - IN PROGRESS! 🔄

⚡ Stay tuned for final result!
"""

def build_x_tweet(data):
    alert_code = data.get('alert_code')
    template = ALERT_TEMPLATES.get(alert_code, {})
    
    if data.get('corner_number'):
        corner_number = data['corner_number']
        bet_type = f"TOTAL CORNERS {corner_number} OVER"
        analysis = f"High corner frequency, {corner_number}+ corners expected"
    else:
        bet_type = template.get('bet_type', data['tahmin'])
        analysis = template.get('analysis', 'Professional signal')
    
    return f"""
{template.get('title', '🎯 BETTING SIGNAL 🎯')}

{data['maç_skor']} | {data['dakika']}'
{data['lig']}

🎯 {bet_type}

📈 {analysis}
💸 Stake: {template.get('stake', '3/5')}

#Betting #SportsBetting
"""

def build_x_live_tweet(data):
    return f"""
🟢 LIVE UPDATE 🟢

{data['maç_skor']} 
{data['lig']}
⏰ {data['live_minute']}' | 📊 {data['live_score']}

🎯 {data['tahmin']} - IN PROGRESS! 🔄

#LiveBetting #Sports
"""

def build_x_reply_tweet(data):
    maç_adı = data['maç_skor'].split(' (')[0].strip()
    if data['result_icon'] == '✅':
        result_text = "🟢 RESULT: WON! 🎉"
    elif data['result_icon'] == '❌':
        result_text = "🔴 RESULT: LOST! 😔"
    else:
        return None
    return f"""
{result_text}

{maç_adı}
FT: {data.get('final_score', 'Finished')}

#BettingResults #SportsBetting
"""

def post_to_x_sync(tweet_text, reply_to_id=None):
    max_retries = 2
    for attempt in range(max_retries):
        try:
            if not x_client: return None
            if attempt > 0: time.sleep(5)
            
            if reply_to_id:
                response = x_client.create_tweet(text=tweet_text, in_reply_to_tweet_id=reply_to_id)
            else:
                response = x_client.create_tweet(text=tweet_text)
            logger.info(f"✅ X Tweet Sent: {response.data['id']}")
            return response.data['id']
        except Exception as e:
            if "429" in str(e): return None
            logger.warning(f"⚠️ X Post Retry ({attempt+1}/{max_retries}): {e}")
    return None

async def post_to_x_async(text, reply_id=None):
    return await asyncio.to_thread(post_to_x_sync, text, reply_id)

# ----------------------------------------------------------------------
# 4. HANDLER (MESAJ ID ve DÜZENLEME MANTIĞI)
# ----------------------------------------------------------------------

async def scheduled_post_task():
    interval = 4 * 60 * 60
    await asyncio.sleep(10)
    while True:
        if bot_running:
            targets = get_channels_sync("target")
            for t in targets:
                try:
                    await bot_client.send_message(t['channel_id'], SCHEDULED_MESSAGE, parse_mode='Markdown')
                except: pass
        await asyncio.sleep(interval)

async def process_match_result(data, signal_record):
    """Maç sonucunu işle (Edit + Reply)"""
    target_message_id = signal_record.get('target_message_id')
    tweet_id = signal_record.get('tweet_id')
    
    targets = get_channels_sync('target')
    for t in targets:
        try:
            await bot_client.edit_message(
                t['channel_id'], 
                target_message_id, 
                text=build_telegram_message(data), # Yeni skorla güncelle
                buttons=BETTING_BUTTONS
            )
            logger.info(f"✅ Telegram UPDATED (Result): {data['signal_key']}")
        except Exception as e: 
            logger.error(f"❌ Telegram Edit error: {e}")
    
    x_reply = build_x_reply_tweet(data)
    if x_reply and tweet_id:
        await post_to_x_async(x_reply, tweet_id)

async def process_live_update(data, signal_record):
    """Canlı güncelleme (Edit değil, Reply at)"""
    target_message_id = signal_record.get('target_message_id')
    tweet_id = signal_record.get('tweet_id')
    
    targets = get_channels_sync('target')
    for t in targets:
        try:
            await bot_client.send_message(
                t['channel_id'], 
                build_telegram_live_update(data), 
                buttons=BETTING_BUTTONS, 
                reply_to=target_message_id
            )
        except: pass
    
    if tweet_id:
        await post_to_x_async(build_x_live_tweet(data), tweet_id)

async def process_general_update(data, signal_record):
    """Sadece skor/dakika değiştiyse mesajı güncelle"""
    target_message_id = signal_record.get('target_message_id')
    targets = get_channels_sync('target')
    for t in targets:
        try:
            await bot_client.edit_message(
                t['channel_id'], 
                target_message_id, 
                text=build_telegram_message(data),
                buttons=BETTING_BUTTONS
            )
            logger.info(f"✏️ Telegram UPDATED (General): {data['signal_key']}")
        except: pass

async def channel_handler(event):
    if not bot_running: return
    
    # 🔥 1. MESAJ ID'SİNİ AL (KİMLİK NUMARASI) 🔥
    source_msg_id = event.id
    
    message_text = event.raw_text.strip()
    data = await asyncio.to_thread(extract_bet_data, message_text)

    if not data or not data['signal_key']: return
    if data.get('alert_code') not in ALLOWED_ALERT_CODES: return

    # 🔥 2. ID İLE VERİTABANINDA ARA 🔥
    signal_record = await asyncio.to_thread(get_signal_by_source_id, source_msg_id)
    
    # --- DURUM A: ZATEN VAR (GÜNCELLEME) ---
    if signal_record:
        logger.info(f"🔄 GÜNCELLEME ALGILANDI (ID: {source_msg_id})")
        
        if data.get('match_ended') or data.get('result_icon'):
            await process_match_result(data, signal_record)
        elif data.get('is_live_update'):
            await process_live_update(data, signal_record)
        else:
            await process_general_update(data, signal_record)

    # --- DURUM B: YOK (YENİ SİNYAL) ---
    else:
        # Eğer bu bir 'Edit' eventiyse ve veritabanında yoksa, eski bir mesajdır, işlem yapma.
        if isinstance(event, events.MessageEdited):
            return

        logger.info(f"🆕 YENİ SİNYAL (ID: {source_msg_id})")

        # X Limit
        current_count = await asyncio.to_thread(get_daily_tweet_count)
        tweet_id = None
        
        if current_count < DAILY_TWEET_LIMIT:
            try:
                tweet_id = await post_to_x_async(build_x_tweet(data))
                if tweet_id:
                    new_count = await asyncio.to_thread(increment_daily_tweet_count)
                    if new_count == DAILY_TWEET_LIMIT:
                        await post_to_x_async(CLOSING_TWEET_TEXT)
            except: pass

        # Telegram
        target_message_id = None
        targets = get_channels_sync('target')
        for t in targets:
            try:
                msg = await bot_client.send_message(t['channel_id'], build_telegram_message(data), buttons=BETTING_BUTTONS)
                target_message_id = msg.id
            except Exception as e: 
                logger.error(f"Telegram Send error: {e}")
        
        # 🔥 3. ID İLE KAYDET 🔥
        if target_message_id:
            await asyncio.to_thread(record_processed_signal, data['signal_key'], target_message_id, tweet_id, source_msg_id)

# ----------------------------------------------------------------------
# 5. FLASK & ANA ÇALIŞTIRMA
# ----------------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
async def login():
    if request.method == 'POST':
        phone = request.form.get('phone')
        session['phone'] = phone
        if not user_client.is_connected(): await user_client.connect()
        await user_client.send_code_request(phone)
        return redirect('/code')
    return render_template_string(LOGIN_FORM)

@app.route('/code', methods=['GET', 'POST'])
async def code():
    if request.method == 'POST':
        code = request.form.get('code')
        await user_client.sign_in(session['phone'], code)
        return "Login Success!"
    return render_template_string(CODE_FORM)

@app.route('/health')
def health(): return "OK", 200

async def main():
    try:
        await asyncio.to_thread(init_db_sync)
        try:
            await bot_client.start(bot_token=BOT_TOKEN)
        except Exception as e:
            if "FloodWait" in str(e):
                await asyncio.sleep(3084)
                await bot_client.start(bot_token=BOT_TOKEN)
            else: raise e
        
        await user_client.connect()
        source_ids = [c['channel_id'] for c in get_channels_sync('source')]
        
        # Hem YENİ hem DÜZENLENMİŞ mesajları dinle
        user_client.add_event_handler(channel_handler, events.NewMessage(incoming=True, chats=source_ids))
        user_client.add_event_handler(channel_handler, events.MessageEdited(chats=source_ids))
        
        logger.info(f"📡 Listening on channels (NEW + EDITED): {source_ids}")
        
        asyncio.create_task(scheduled_post_task())
        await user_client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Main error: {e}")

if __name__ == '__main__':
    asgi_app = WsgiToAsgi(app)
    config = Config()
    config.bind = [f"0.0.0.0:{int(os.environ.get('PORT', '5000'))}"]
    async def runner():
        await asyncio.gather(serve(asgi_app, config), main())
    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass
