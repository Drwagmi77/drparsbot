import os
import re
import asyncio
import logging
import time
import psycopg2
from psycopg2.extras import RealDictCursor
from telethon import TelegramClient, events, Button
from flask import Flask, jsonify, request, redirect, session, render_template_string
import tweepy
from hypercorn.asyncio import serve
from hypercorn.config import Config
from asgiref.wsgi import WsgiToAsgi  # EKLENDİ: Bu satır NameError hatasını çözer

# ----------------------------------------------------------------------
# 1. AYARLAR VE ORTAM DEĞİŞKENLERİ
# ----------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Zorunlu Değişkenler (Hata vermemesi için varsayılan değerler 0 veya boş)
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

DB_HOST = os.environ.get("DB_HOST")
DB_PORT = os.environ.get("DB_PORT")
DB_NAME = os.environ.get("DB_NAME")
DB_USER = os.environ.get("DB_USER")
DB_PASS = os.environ.get("DB_PASS")

X_CONSUMER_KEY = os.environ.get("X_CONSUMER_KEY")
X_CONSUMER_SECRET = os.environ.get("X_CONSUMER_SECRET")
X_ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN")
X_ACCESS_TOKEN_SECRET = os.environ.get("X_ACCESS_TOKEN_SECRET")

# Kanal ID'lerini güvenli bir şekilde sayıya çeviriyoruz
try:
    SOURCE_CHANNEL = int(os.environ.get("SOURCE_CHANNEL", 0))
    TARGET_CHANNEL = int(os.environ.get("TARGET_CHANNEL", 0))
except ValueError:
    logger.error("❌ HATA: SOURCE_CHANNEL veya TARGET_CHANNEL sayı değil! Lütfen Env ayarlarını kontrol edin.")
    SOURCE_CHANNEL = 0
    TARGET_CHANNEL = 0

# Sabitler
ALLOWED_ALERT_CODES = {'17', '41', '32', '48', '1', '21'} 

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

# ----------------------------------------------------------------------
# 2. CLIENT VE FLASK KURULUMU
# ----------------------------------------------------------------------

bot_client = TelegramClient('bot', API_ID, API_HASH)
user_client = TelegramClient('user', API_ID, API_HASH)
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24).hex()

# Twitter Client
client = None
if X_CONSUMER_KEY and X_CONSUMER_SECRET:
    client = tweepy.Client(
        consumer_key=X_CONSUMER_KEY,
        consumer_secret=X_CONSUMER_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_TOKEN_SECRET
    )

bot_running = True 

# ----------------------------------------------------------------------
# 3. VERİTABANI İŞLEMLERİ
# ----------------------------------------------------------------------

def get_connection():
    return psycopg2.connect(
        dbname=DB_NAME, user=DB_USER, password=DB_PASS,
        host=DB_HOST, port=DB_PORT, sslmode="require"
    )

def init_db_sync():
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_signals (
                    signal_key TEXT PRIMARY KEY,
                    source_channel BIGINT,
                    target_message_id BIGINT,
                    tweet_id BIGINT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
        logger.info("✅ Veritabanı tablosu hazır.")
    except Exception as e:
        logger.error(f"Veritabanı başlatma hatası: {e}")
    finally:
        if conn: conn.close()

def get_signal_data(signal_key):
    conn = None
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT target_message_id, tweet_id FROM processed_signals WHERE signal_key = %s", (signal_key,))
            result = cur.fetchone()
            return dict(result) if result else None
    except Exception as e:
        logger.error(f"Sinyal kontrol hatası: {e}")
        return None
    finally:
        if conn: conn.close()

def record_processed_signal(signal_key, target_message_id, tweet_id):
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO processed_signals (signal_key, source_channel, target_message_id, tweet_id) 
                VALUES (%s, %s, %s, %s) 
                ON CONFLICT (signal_key) DO UPDATE SET 
                target_message_id = EXCLUDED.target_message_id, 
                tweet_id = EXCLUDED.tweet_id;
            """, (signal_key, SOURCE_CHANNEL, target_message_id, tweet_id))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"Sinyal kayıt hatası: {e}")
        return False
    finally:
        if conn: conn.close()

async def init_db():
    await asyncio.to_thread(init_db_sync)

# ----------------------------------------------------------------------
# 4. MANTIK VE FORMATLAMA
# ----------------------------------------------------------------------

def extract_bet_data(message_text):
    data = {}
    match_score = re.search(r'⚽ (.*?)\s*\(.*?\)', message_text, re.DOTALL)
    data['maç_skor'] = match_score.group(0).strip().replace('⚽ ', '') if match_score else None
    
    lig = re.search(r'🏟 (.*?)\n', message_text)
    data['lig'] = lig.group(1).strip() if lig else None
    
    dakika = re.search(r'⏰ (\d+)\s*', message_text)
    data['dakika'] = dakika.group(1).strip() if dakika else None
    
    tahmin = re.search(r'❗ (.*?)\n', message_text)
    if tahmin:
        inner = re.search(r'\((.*?)\)', tahmin.group(1))
        data['tahmin'] = inner.group(1).strip() if inner else tahmin.group(1).strip()
    else:
        data['tahmin'] = None
    
    alert = re.search(r'👉 AlertCode: (\d+)', message_text)
    data['alert_code'] = alert.group(1).strip() if alert else None

    result = re.match(r'([✅❌])', message_text.strip())
    data['result_icon'] = result.group(1) if result else None

    final = re.search(r'#⃣ FT (\d+ - \d+)', message_text)
    data['final_score'] = final.group(1).strip() if final else None

    if all([data.get('maç_skor'), data.get('dakika'), data.get('tahmin')]):
        # Benzersiz anahtar oluştur
        m = re.sub(r'[\(\)]', '', data['maç_skor']).strip().replace(' ', '_').replace('-', '')
        t = re.sub(r'[^\w\s]', '', data['tahmin']).strip().replace(' ', '_')
        data['signal_key'] = f"{m}_{data['dakika']}_{t}"
    else:
        data['signal_key'] = None
    
    return data if data['signal_key'] else None

def build_telegram_message(data):
    return f"{data['maç_skor']}\n{data['lig']}\n{data['dakika']}. min\n{data['tahmin']}"

def build_x_tweet(data):
    return f"{data['maç_skor']} | {data['dakika']}. min\n{data['tahmin']}"

def post_to_x_sync(text, reply_to=None):
    if not client: return None
    try:
        if reply_to:
            return client.create_tweet(text=text, in_reply_to_tweet_id=reply_to).data['id']
        return client.create_tweet(text=text).data['id']
    except Exception as e:
        logger.error(f"Twitter hatası: {e}")
        return None

# ----------------------------------------------------------------------
# 5. OTOMATİK GÖREVLER VE HANDLERLAR
# ----------------------------------------------------------------------

async def scheduled_post_task():
    """Her 4 saatte bir mesaj atar."""
    interval = 4 * 60 * 60
    while True:
        if bot_running and bot_client.is_connected():
            try:
                # TARGET_CHANNEL int olduğu için 'Cannot get entity' hatası vermez
                await bot_client.send_message(
                    entity=TARGET_CHANNEL,
                    message=SCHEDULED_MESSAGE,
                    parse_mode='Markdown'
                )
                logger.info("✅ Otomatik mesaj gönderildi.")
            except Exception as e:
                logger.error(f"Otomatik mesaj hatası: {e}")
        await asyncio.sleep(interval)

@user_client.on(events.NewMessage(incoming=True, chats=[SOURCE_CHANNEL] if SOURCE_CHANNEL else []))
async def channel_handler(event):
    if not bot_running: return
    
    msg = event.raw_text.strip()
    data = await asyncio.to_thread(extract_bet_data, msg)
    
    if not data or not data['signal_key']: return

    key = data['signal_key']
    
    # SONUÇ GELDİYSE (✅ veya ❌)
    if data['result_icon']:
        record = await asyncio.to_thread(get_signal_data, key)
        if record:
            # Telegram Düzenle
            try:
                res_text = "\n\n🟢 RESULT: WON! 🎉" if data['result_icon'] == '✅' else "\n\n🔴 RESULT: LOST! 😔"
                orig_msg = await bot_client.get_messages(TARGET_CHANNEL, ids=record['target_message_id'])
                if orig_msg:
                    await bot_client.edit_message(TARGET_CHANNEL, record['target_message_id'], text=orig_msg.message + res_text, buttons=BETTING_BUTTONS)
            except Exception as e:
                logger.error(f"Edit hatası: {e}")

            # Twitter Yanıtla
            if record['tweet_id']:
                match_name = data['maç_skor'].split(' (')[0]
                reply_txt = f"{'🟢 WON' if data['result_icon'] == '✅' else '🔴 LOST'}!\n\n{match_name}: {data['final_score']}"
                await asyncio.to_thread(post_to_x_sync, reply_txt, reply_to=record['tweet_id'])

    # YENİ SİNYAL GELDİYSE
    else:
        if data.get('alert_code') not in ALLOWED_ALERT_CODES: return
        if await asyncio.to_thread(get_signal_data, key): return # Zaten var

        # Twitter'a at
        tw_id = await asyncio.to_thread(post_to_x_sync, build_x_tweet(data))
        
        # Telegram'a at
        tg_id = None
        try:
            sent = await bot_client.send_message(TARGET_CHANNEL, build_telegram_message(data), buttons=BETTING_BUTTONS)
            tg_id = sent.id
        except Exception as e:
            logger.error(f"Telegram gönderim hatası: {e}")

        # DB'ye kaydet
        await asyncio.to_thread(record_processed_signal, key, tg_id, tw_id)

# ----------------------------------------------------------------------
# 6. FLASK VE BAŞLATMA
# ----------------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
async def login():
    if request.method == 'POST':
        phone = request.form.get('phone')
        session['phone'] = phone
        await user_client.connect()
        await user_client.send_code_request(phone)
        return redirect('/submit-code')
    return render_template_string("""<form method="post"><input name="phone"><button>Send</button></form>""")

@app.route('/submit-code', methods=['GET', 'POST'])
async def submit_code():
    if request.method == 'POST':
        await user_client.sign_in(session['phone'], request.form.get('code'))
        return "Logged in!"
    return render_template_string("""<form method="post"><input name="code"><button>Verify</button></form>""")

@app.route('/')
def root(): return "Bot Running", 200

async def main_bot_runner():
    await init_db()
    await bot_client.start(bot_token=BOT_TOKEN)
    await user_client.start()
    asyncio.create_task(scheduled_post_task())
    await user_client.run_until_disconnected()

if __name__ == '__main__':
    asgi_app = WsgiToAsgi(app)
    config = Config()
    config.bind = [f"0.0.0.0:{int(os.environ.get('PORT', '5000'))}"]
    
    async def runner():
        await asyncio.gather(
            serve(asgi_app, config),
            main_bot_runner()
        )
        
    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass
