import os
import re
import asyncio
import logging
import threading
import time
import json
import random
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from telethon import TelegramClient, events, Button
from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
from telethon.tl.functions.channels import GetParticipantRequest
from flask import Flask, jsonify, request, redirect, session, render_template_string
import tweepy
from hypercorn.asyncio import serve
from hypercorn.config import Config
from asgiref.wsgi import WsgiToAsgi

# ----------------------------------------------------------------------
# 1. ORTAM DEĞİŞKENLERİ VE YAPILANDIRMA
# ----------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Ortam değişkenlerinden çekilir
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
    # Hata durumunda devam etmemesi için exit
    # exit(1) # Render'da logu görmek için exit yapmıyoruz ama logluyoruz

# GLOBAL BUTONLAR, METİNLER VE FİLTRELER
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

# Client ve Flask tanımları
# Loop argümanı verilmiyor, asenkron bağlamda kendisi bulur
bot_client = TelegramClient('bot_session', API_ID, API_HASH)
user_client = TelegramClient('user_session', API_ID, API_HASH)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24).hex()

# X Client (Hata yönetimi ile)
client = None
try:
    if X_CONSUMER_KEY:
        client = tweepy.Client(
            consumer_key=X_CONSUMER_KEY,
            consumer_secret=X_CONSUMER_SECRET,
            access_token=X_ACCESS_TOKEN,
            access_token_secret=X_ACCESS_TOKEN_SECRET
        )
except Exception as e:
    logger.error(f"X Client Init Error: {e}")

bot_running = True 

# Flask Login HTML Formları
LOGIN_FORM = """<!doctype html><title>Login</title><h2>Phone</h2><form method=post><input name=phone placeholder="+90..." required><button>Send Code</button></form>"""
CODE_FORM = """<!doctype html><title>Code</title><h2>Enter Code</h2><form method=post><input name=code placeholder=12345 required><button>Login</button></form>"""

# ----------------------------------------------------------------------
# 2. VERİTABANI YÖNETİMİ (POSTGRESQL - SENKRON)
# ----------------------------------------------------------------------

def get_connection():
    """Yeni bağlantı alır."""
    try:
        return psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            host=DB_HOST,
            port=DB_PORT,
            sslmode="require"
        )
    except psycopg2.OperationalError as e:
        logger.error(f"Database connection failed: {e}")
        raise e

def init_db_sync():
    """ZORUNLU TABLOLARIN BAŞLATILMASI"""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # 1. Sinyal Takip Tablosu
        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_signals (
                signal_key TEXT PRIMARY KEY,
                source_channel TEXT NOT NULL,
                target_message_id BIGINT,
                tweet_id BIGINT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # 2. Kanal Tablosu (Eski yapı uyumluluğu için)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                channel_id BIGINT UNIQUE,
                title TEXT,
                channel_type TEXT
            );
        """)
        
        conn.commit()
        logger.info("✅ DB tabloları hazır.")
    except Exception as e:
        logger.error(f"Error during database initialization: {e}")
        # raise # Hata olsa bile devam etmeye çalışalım, belki geçicidir
    finally:
        if conn:
            conn.close()

def get_signal_data(signal_key):
    """Sinyal verisini alır."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT target_message_id, tweet_id FROM processed_signals 
                WHERE signal_key = %s AND target_message_id IS NOT NULL AND tweet_id IS NOT NULL
            """, (signal_key,))
            result = cur.fetchone()
            return dict(result) if result else None
    except Exception as e:
        logger.error(f"Error checking signal data: {e}")
        return None
    finally:
        if conn:
            conn.close()

def record_processed_signal(signal_key, target_message_id, tweet_id):
    """Yeni işlenen sinyali kaydeder veya günceller."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO processed_signals (signal_key, source_channel, target_message_id, tweet_id) 
                VALUES (%s, %s, %s, %s) 
                ON CONFLICT (signal_key) DO UPDATE SET 
                target_message_id = EXCLUDED.target_message_id, 
                tweet_id = EXCLUDED.tweet_id;
            """, (signal_key, "source", target_message_id, tweet_id))
            conn.commit()
            logger.info(f"Signal recorded: {signal_key}")
            return True
    except Exception as e:
        logger.error(f"Error recording signal: {e}")
        return False
    finally:
        if conn:
            conn.close()

def get_channels_sync(t): 
    """Kanal ID'lerini çeker."""
    # ENV'den oku
    source_id = os.environ.get("SOURCE_CHANNEL")
    target_id = os.environ.get("TARGET_CHANNEL")
    
    channels = []
    try:
        if t == 'source' and source_id:
            channels.append({"channel_id": int(source_id) if source_id.startswith('-100') else source_id})
        elif t == 'target' and target_id:
            channels.append({"channel_id": int(target_id) if target_id.startswith('-100') else target_id})
    except ValueError:
        logger.error(f"Invalid channel ID format in ENV: {source_id} or {target_id}")

    return channels

# ----------------------------------------------------------------------
# 3. SİNYAL ÇIKARMA VE ŞABLONLAMA
# ----------------------------------------------------------------------

def extract_bet_data(message_text):
    data = {}
    # Regex ile veri çekme
    match_score_match = re.search(r'⚽ (.*?)\s*\(.*?\)', message_text, re.DOTALL)
    data['maç_skor'] = match_score_match.group(0).strip().replace('⚽ ', '') if match_score_match else None
    
    lig_match = re.search(r'🏟 (.*?)\n', message_text)
    data['lig'] = lig_match.group(1).strip() if lig_match else None
    
    dakika_match = re.search(r'⏰ (\d+)\s*', message_text)
    data['dakika'] = dakika_match.group(1).strip() if dakika_match else None
    
    tahmin_match = re.search(r'❗ (.*?)\n', message_text)
    # Parantez içindeki İngilizce kısmı al
    tahmin_en_match = re.search(r'\((.*?)\)', tahmin_match.group(1)) if tahmin_match and '(' in tahmin_match.group(1) else tahmin_match
    data['tahmin'] = tahmin_en_match.group(1).strip() if tahmin_en_match else (tahmin_match.group(1).strip() if tahmin_match else None)
    
    alert_code_match = re.search(r'👉 AlertCode: (\d+)', message_text)
    data['alert_code'] = alert_code_match.group(1).strip() if alert_code_match else None

    result_match = re.match(r'([✅❌])', message_text.strip())
    data['result_icon'] = result_match.group(1) if result_match else None

    final_score_match = re.search(r'#⃣ FT (\d+ - \d+)', message_text)
    data['final_score'] = final_score_match.group(1).strip() if final_score_match else None

    # Benzersiz anahtar oluştur
    if all([data.get('maç_skor'), data.get('dakika'), data.get('tahmin')]):
        maç_temiz = re.sub(r'[\(\)]', '', data['maç_skor']).strip().replace(' ', '_').replace('-', '')
        tahmin_temiz = re.sub(r'[^\w\s]', '', data['tahmin']).strip().replace(' ', '_')
        data['signal_key'] = f"{maç_temiz}_{data['dakika']}_{tahmin_temiz}"
    else:
        data['signal_key'] = None
    
    return data if data['signal_key'] else None

def build_telegram_message(data):
    return f"""
{data['maç_skor']}
{data['lig']}
{data['dakika']}. min
{data['tahmin']}
"""

def build_x_tweet(data):
    return f"""
{data['maç_skor']} | {data['dakika']}. min
{data['tahmin']}
"""

def build_telegram_edit(result_icon):
    if result_icon == '✅': return "\n\n🟢 RESULT: WON! 🎉"
    elif result_icon == '❌': return "\n\n🔴 RESULT: LOST! 😔"
    return ""

def build_x_reply_tweet(data):
    maç_adı = data['maç_skor'].split(' (')[0].strip()
    if data['result_icon'] == '✅':
        result_text = "🟢 RESULT: WON! 🎉"
        call_to_action = "Bet WON! Like this tweet to celebrate!"
    elif data['result_icon'] == '❌':
        result_text = "🔴 RESULT: LOST! 😔"
        call_to_action = "Bet LOST. We'll be back stronger!"
    else:
        return None
    return f"""
{result_text}

{maç_adı}: {data['final_score']}

{call_to_action}
"""

def post_to_x_sync(tweet_text, reply_to_id=None):
    """
    X'e tweet atar. Tamamen SENKRON'dur, await içermez.
    """
    try:
        if not client: 
            logger.warning("X client not initialized.")
            return None
        
        if reply_to_id:
            response = client.create_tweet(text=tweet_text, in_reply_to_tweet_id=reply_to_id)
            logger.info(f"X Reply Sent: {response.data['id']}")
        else:
            response = client.create_tweet(text=tweet_text)
            logger.info(f"X Tweet Sent: {response.data['id']}")
            
        return response.data['id']
    except Exception as e:
        logger.error(f"X post error: {e}")
        return None

# ----------------------------------------------------------------------
# 4. HANDLER (LOGIC)
# ----------------------------------------------------------------------

async def channel_handler(event):
    if not bot_running: return
    message_text = event.raw_text.strip()
    data = await asyncio.to_thread(extract_bet_data, message_text)
    
    if not data or not data['signal_key']: return

    is_result = data['result_icon'] is not None
    signal_key = data['signal_key']
    target_channel_id = os.environ.get("TARGET_CHANNEL")
    
    # ID'yi integer'a çevirmeyi dene
    try:
        target_int = int(target_channel_id)
    except:
        target_int = target_channel_id

    if is_result:
        # --- SONUÇ İŞLEME ---
        signal_record = await asyncio.to_thread(get_signal_data, signal_key)
        if signal_record:
            target_message_id = signal_record.get('target_message_id')
            tweet_id = signal_record.get('tweet_id')
            
            # Telegram Edit
            try:
                original_msg = await bot_client.get_messages(target_int, ids=target_message_id)
                if original_msg:
                    new_text = original_msg.message + build_telegram_edit(data['result_icon'])
                    await bot_client.edit_message(target_int, target_message_id, text=new_text, buttons=BETTING_BUTTONS)
                    logger.info("Telegram message edited.")
            except Exception as e: logger.error(f"Edit error: {e}")
            
            # X Reply
            x_reply = build_x_reply_tweet(data)
            if x_reply and tweet_id:
                await asyncio.to_thread(post_to_x_sync, x_reply, tweet_id)
    else:
        # --- YENİ SİNYAL ---
        if data.get('alert_code') not in ALLOWED_ALERT_CODES: return
        if await asyncio.to_thread(get_signal_data, signal_key): return

        logger.info(f"New Signal: {signal_key}")

        # X'e Tweet At
        tweet_id = await asyncio.to_thread(post_to_x_sync, build_x_tweet(data))
        
        # Telegram'a Mesaj At
        try:
            sent = await bot_client.send_message(target_int, build_telegram_message(data), buttons=BETTING_BUTTONS)
            # DB'ye Kaydet
            await asyncio.to_thread(record_processed_signal, signal_key, sent.id, tweet_id)
        except Exception as e: logger.error(f"Send error: {e}")

# ----------------------------------------------------------------------
# 5. ZAMANLANMIŞ GÖREV
# ----------------------------------------------------------------------

async def scheduled_post_task():
    interval = 4 * 60 * 60
    await asyncio.sleep(10) 
    target = os.environ.get("TARGET_CHANNEL")
    try:
        target = int(target)
    except:
        pass
        
    while True:
        if bot_running and bot_client.is_connected():
            try:
                await bot_client.send_message(target, SCHEDULED_MESSAGE, parse_mode='Markdown')
                logger.info("Auto-promo sent.")
            except Exception as e: logger.error(f"Auto post error: {e}")
        await asyncio.sleep(interval)

# ----------------------------------------------------------------------
# 6. FLASK ROTALARI
# ----------------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
async def login():
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        session['phone'] = phone
        try:
            # ÖNEMLİ: connect() yeterli, start() değil!
            if not user_client.is_connected():
                await user_client.connect()
            
            await user_client.send_code_request(phone)
            return redirect('/submit-code')
        except Exception as e: return f"Error: {e}"
    return render_template_string(LOGIN_FORM)

@app.route('/submit-code', methods=['GET', 'POST'])
async def submit_code():
    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        try:
            await user_client.sign_in(session['phone'], code)
            return "Login Successful! Restart the bot."
        except Exception as e: return f"Error: {e}"
    return render_template_string(CODE_FORM)

@app.route('/health')
def health(): return jsonify(status="ok"), 200

# ----------------------------------------------------------------------
# 7. BAŞLATMA MANTIĞI
# ----------------------------------------------------------------------

async def main_bot_runner():
    # DB Başlat
    await asyncio.to_thread(init_db_sync)
    
    # Clientları Başlat
    # Bot Client: Token ile başlar
    await bot_client.start(bot_token=BOT_TOKEN)
    
    # User Client: Sadece bağlanır, login'i web'den bekler
    # start() YERİNE connect() KULLANIYORUZ - BU EOF HATASINI ÇÖZER
    await user_client.connect()
    
    if await user_client.is_user_authorized():
        logger.info("User client authorized.")
    else:
        logger.warning("User client NOT authorized. Please visit /login")
    
    logger.info("Clients started/connected.")
    
    # Handler'ı Ekle (DB hazır olduktan sonra)
    source_ids = [c['channel_id'] for c in get_channels_sync('source')]
    user_client.add_event_handler(channel_handler, events.NewMessage(incoming=True, chats=source_ids))
    logger.info(f"Handler attached for: {source_ids}")
    
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
