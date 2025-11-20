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
        
        # 2. Kanal Tablosu
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
        raise
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

def get_all_signal_keys():
    """Tüm sinyal key'lerini getirir (live update için)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT signal_key FROM processed_signals WHERE target_message_id IS NOT NULL")
            return [row[0] for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"Error getting signal keys: {e}")
        return []
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
# 3. SİNYAL ÇIKARMA VE ŞABLONLAMA (GÜNCELLENDİ - LIVE UPDATE DESTEKLİ)
# ----------------------------------------------------------------------

def extract_bet_data(message_text):
    data = {}
    
    # 1. MAÇ SKORU - İki farklı format destekle
    match_score_match = re.search(r'⚽[️\s]*(.*?)\s*\(.*?\)', message_text, re.DOTALL)
    if match_score_match:
        data['maç_skor'] = match_score_match.group(0).strip().replace('⚽', '').replace('️', '').strip()
    else:
        # Alternatif format
        match_alt = re.search(r'([A-Za-z\s]+-\s*[A-Za-z\s]+)\s*\(\s*(\d+\s*-\s*\d+)\s*\)', message_text)
        if match_alt:
            data['maç_skor'] = f"{match_alt.group(1)} ({match_alt.group(2)})"
        else:
            data['maç_skor'] = None
    
    # 2. LİG
    lig_match = re.search(r'🏟\s*(.*?)\n', message_text)
    data['lig'] = lig_match.group(1).strip() if lig_match else None
    
    # 3. DAKİKA - İki farklı format
    dakika_match = re.search(r'⏰\s*(\d+)\s*', message_text)
    data['dakika'] = dakika_match.group(1).strip() if dakika_match else None
    
    # 4. TAHMİN - Parantez içindeki İngilizce kısmı al
    tahmin_match = re.search(r'❗[️\s]*(.*?)\n', message_text)
    if tahmin_match:
        tahmin_text = tahmin_match.group(1).strip()
        tahmin_en_match = re.search(r'\((.*?)\)', tahmin_text)
        data['tahmin'] = tahmin_en_match.group(1).strip() if tahmin_en_match else tahmin_text
    else:
        data['tahmin'] = None
    
    # 5. ALERT CODE
    alert_code_match = re.search(r'👉\s*AlertCode:\s*(\d+)', message_text)
    data['alert_code'] = alert_code_match.group(1).strip() if alert_code_match else None

    # 6. SONUÇ İKONU
    result_match = re.search(r'([✅❌])', message_text)
    data['result_icon'] = result_match.group(1) if result_match else None

    # 7. LIVE UPDATE TESPİTİ - YENİ ÖZELLİK!
    live_score_match = re.search(r'⏰\s*(\d+)\s*⚽[️\s]*(\d+\s*-\s*\d+)', message_text)
    if live_score_match:
        data['live_minute'] = live_score_match.group(1).strip()
        data['live_score'] = live_score_match.group(2).strip()
        data['is_live_update'] = True
        logger.info(f"🔴 LIVE UPDATE detected: {data['live_score']} at {data['live_minute']}")
    else:
        data['is_live_update'] = False

    # 8. FINAL SKOR
    final_score_match = re.search(r'#⃣\s*FT\s*(\d+\s*-\s*\d+)', message_text)
    data['final_score'] = final_score_match.group(1).strip() if final_score_match else None

    # 9. SIGNAL KEY OLUŞTUR - Live update için orijinal key'i bul
    if all([data.get('maç_skor'), data.get('tahmin')]):
        maç_temiz = re.sub(r'[\(\)]', '', data['maç_skor']).strip().replace(' ', '_').replace('-', '')
        tahmin_temiz = re.sub(r'[^\w\s]', '', data['tahmin']).strip().replace(' ', '_')
        
        # Dakika olmadan da key oluşturabilir (live update için)
        if data.get('dakika'):
            data['signal_key'] = f"{maç_temiz}_{data['dakika']}_{tahmin_temiz}"
        else:
            data['signal_key'] = f"{maç_temiz}_{tahmin_temiz}"
    else:
        data['signal_key'] = None
    
    return data if data['signal_key'] else None

def find_original_signal_key(current_data):
    """Live update için orijinal sinyal key'ini bulur."""
    all_keys = get_all_signal_keys()
    
    maç_temiz = re.sub(r'[\(\)]', '', current_data['maç_skor']).strip().replace(' ', '_').replace('-', '')
    tahmin_temiz = re.sub(r'[^\w\s]', '', current_data['tahmin']).strip().replace(' ', '_')
    
    # Maç adı ve tahmin eşleşen key'leri ara
    for key in all_keys:
        if maç_temiz in key and tahmin_temiz in key:
            logger.info(f"🔍 Original signal found: {key}")
            return key
    
    logger.warning(f"❌ No original signal found for: {maç_temiz}_{tahmin_temiz}")
    return None

def build_telegram_message(data):
    return f"""
{data['maç_skor']}
{data['lig']}
{data['dakika']}. min
{data['tahmin']}
"""

def build_telegram_live_update(data):
    """Live update için Telegram mesajı"""
    return f"""
🟢 LIVE UPDATE: {data['live_score']} ({data['live_minute']}')

{data['maç_skor']}
{data['lig']}
{data['tahmin']} - WON! 🎉
"""

def build_x_tweet(data):
    return f"""
{data['maç_skor']} | {data['dakika']}. min
{data['tahmin']}
"""

def build_x_live_tweet(data):
    """Live update için X tweet'i"""
    return f"""
🟢 LIVE: {data['live_score']} ({data['live_minute']}')

{data['maç_skor']}
{data['tahmin']} - WON! 🎉
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
    """X'e tweet atar."""
    try:
        if not client: return None
        if reply_to_id:
            response = client.create_tweet(text=tweet_text, in_reply_to_tweet_id=reply_to_id)
            logger.info(f"X Reply: {response.data['id']}")
        else:
            response = client.create_tweet(text=tweet_text)
            logger.info(f"X Tweet: {response.data['id']}")
        return response.data['id']
    except Exception as e:
        logger.error(f"X Post Error: {e}")
        return None

async def post_to_x_async(text, reply_id=None):
    return await asyncio.to_thread(post_to_x_sync, text, reply_id)

# ----------------------------------------------------------------------
# 4. TELEGRAM HANDLER & TASKS (GÜNCELLENDİ - LIVE UPDATE DESTEKLİ)
# ----------------------------------------------------------------------

async def scheduled_post_task():
    """4 Saatlik Promosyon"""
    interval = 4 * 60 * 60
    await asyncio.sleep(10)
    while True:
        if bot_running:
            targets = get_channels_sync("target")
            for t in targets:
                try:
                    await bot_client.send_message(t['channel_id'], SCHEDULED_MESSAGE, parse_mode='Markdown')
                    logger.info(f"Promo sent to {t['channel_id']}")
                except Exception as e:
                    logger.error(f"Promo error {t['channel_id']}: {e}")
        await asyncio.sleep(interval)

async def channel_handler(event):
    if not bot_running: return
    
    message_text = event.raw_text.strip()
    data = await asyncio.to_thread(extract_bet_data, message_text)

    if not data or not data['signal_key']: return

    is_result = data['result_icon'] is not None
    is_live_update = data.get('is_live_update', False)
    signal_key = data['signal_key']
    
    if is_result:
        # SONUÇ İŞLEME
        signal_record = await asyncio.to_thread(get_signal_data, signal_key)
        if signal_record:
            target_message_id = signal_record.get('target_message_id')
            tweet_id = signal_record.get('tweet_id')
            
            targets = get_channels_sync('target')
            for t in targets:
                try:
                    original_msg = await bot_client.get_messages(t['channel_id'], ids=target_message_id)
                    if original_msg:
                        new_text = original_msg.message + build_telegram_edit(data['result_icon'])
                        await bot_client.edit_message(t['channel_id'], target_message_id, text=new_text, buttons=BETTING_BUTTONS)
                        logger.info(f"Result updated: {signal_key}")
                except Exception as e: 
                    logger.error(f"Edit error: {e}")
            
            x_reply = build_x_reply_tweet(data)
            if x_reply and tweet_id:
                await post_to_x_async(x_reply, tweet_id)
    
    elif is_live_update:
        # LIVE UPDATE İŞLEME - YENİ ÖZELLİK!
        logger.info(f"🟢 Processing LIVE UPDATE: {data['live_score']} at {data['live_minute']}")
        
        # Orijinal sinyal key'ini bul
        original_key = await asyncio.to_thread(find_original_signal_key, data)
        if not original_key:
            logger.warning(f"❌ No original signal found for live update: {data['signal_key']}")
            return
            
        signal_record = await asyncio.to_thread(get_signal_data, original_key)
        if signal_record:
            target_message_id = signal_record.get('target_message_id')
            tweet_id = signal_record.get('tweet_id')
            
            # Telegram'a yeni mesaj olarak gönder (edit değil)
            targets = get_channels_sync('target')
            for t in targets:
                try:
                    await bot_client.send_message(
                        t['channel_id'], 
                        build_telegram_live_update(data), 
                        buttons=BETTING_BUTTONS,
                        reply_to=target_message_id  # Orijinal mesaja yanıt olarak
                    )
                    logger.info(f"Live update sent: {data['live_score']}")
                except Exception as e:
                    logger.error(f"Live update send error: {e}")
            
            # Twitter'a yanıt gönder
            if tweet_id:
                x_live_tweet = build_x_live_tweet(data)
                await post_to_x_async(x_live_tweet, tweet_id)
    
    else:
        # YENİ SİNYAL
        if data.get('alert_code') not in ALLOWED_ALERT_CODES: return
        if await asyncio.to_thread(get_signal_data, signal_key): return

        logger.info(f"New Signal Found: {signal_key}")

        # X
        tweet_id = await post_to_x_async(build_x_tweet(data))
        
        # Telegram
        target_message_id = None
        targets = get_channels_sync('target')
        for t in targets:
            try:
                msg = await bot_client.send_message(t['channel_id'], build_telegram_message(data), buttons=BETTING_BUTTONS)
                target_message_id = msg.id
                logger.info(f"New signal sent to {t['channel_id']}")
            except Exception as e: 
                logger.error(f"Send error: {e}")
        
        await asyncio.to_thread(record_processed_signal, signal_key, target_message_id, tweet_id)

# ----------------------------------------------------------------------
# 5. FLASK ROTALARI
# ----------------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
async def login():
    if request.method == 'POST':
        phone = request.form.get('phone')
        session['phone'] = phone
        if not user_client.is_connected(): 
            await user_client.connect()
        await user_client.send_code_request(phone)
        return redirect('/code')
    return render_template_string(LOGIN_FORM)

@app.route('/code', methods=['GET', 'POST'])
async def code():
    if request.method == 'POST':
        code = request.form.get('code')
        await user_client.sign_in(session['phone'], code)
        return "Login Success! You can close this tab."
    return render_template_string(CODE_FORM)

@app.route('/health')
def health(): return "OK", 200

@app.route('/')
def home(): return jsonify({"status": "running", "features": ["betting_signals", "live_updates", "x_posting"]})

# ----------------------------------------------------------------------
# 6. ANA ÇALIŞTIRMA (FLOODWAIT KORUMALI)
# ----------------------------------------------------------------------

async def main():
    try:
        # DB Başlat
        await asyncio.to_thread(init_db_sync)
        logger.info("Database initialization complete.")
        
        # Bot Client - FloodWait Korumalı
        try:
            await bot_client.start(bot_token=BOT_TOKEN)
            logger.info("Bot client started successfully.")
        except Exception as e:
            if "FloodWait" in str(e):
                wait_time = 3084  # 51 dakika
                logger.error(f"🚨 FLOOD WAIT: {wait_time} seconds. Waiting...")
                await asyncio.sleep(wait_time)
                await bot_client.start(bot_token=BOT_TOKEN)
                logger.info("Bot client started after flood wait.")
            else:
                raise e
        
        # User Client  
        await user_client.connect()
        logger.info("User client connected.")
        
        if await user_client.is_user_authorized():
            logger.info("User client authorized.")
        else:
            logger.warning("User client NOT authorized. Please visit /login")

        # Handler'ı bağla
        source_ids = [c['channel_id'] for c in get_channels_sync('source')]
        user_client.add_event_handler(channel_handler, events.NewMessage(incoming=True, chats=source_ids))
        logger.info(f"📡 Listening on channels: {source_ids}")
        
        # Arka plan görevleri
        asyncio.create_task(scheduled_post_task())
        
        # Botu çalıştır
        logger.info("✅ Bot is fully operational!")
        await user_client.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"Main error: {e}")

if __name__ == '__main__':
    asgi_app = WsgiToAsgi(app)
    config = Config()
    config.bind = [f"0.0.0.0:{int(os.environ.get('PORT', '5000'))}"]
    
    async def runner():
        await asyncio.gather(
            serve(asgi_app, config),
            main()
        )
    
    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
