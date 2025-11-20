import os
import re
import asyncio
import logging
import threading
from telethon import TelegramClient, events, Button
import tweepy
import psycopg2
from psycopg2 import pool, extras
from flask import Flask, jsonify, request, session, redirect, render_template_string
import time

# ----------------------------------------------------------------------
# 1. ORTAM DEĞİŞKENLERİ VE YAPILANDIRMA
# ----------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Environment Variables
API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')

# KRİTİK: Kanal ID'leri - Render Environment'dan al veya hardcode yap
SOURCE_CHANNEL = os.getenv('SOURCE_CHANNEL', '-1002093384030')  # Kaynak kanal
TARGET_CHANNEL = os.getenv('TARGET_CHANNEL', '-1002013840743')  # Hedef kanal

# Database
DB_HOST = os.getenv('DB_HOST')
DB_PORT = os.getenv('DB_PORT', 5432)
DB_NAME = os.getenv('DB_NAME')
DB_USER = os.getenv('DB_USER')
DB_PASS = os.getenv('DB_PASS')

# Twitter/X
X_CONSUMER_KEY = os.getenv('X_CONSUMER_KEY')
X_CONSUMER_SECRET = os.getenv('X_CONSUMER_SECRET')
X_ACCESS_TOKEN = os.getenv('X_ACCESS_TOKEN')
X_ACCESS_TOKEN_SECRET = os.getenv('X_ACCESS_TOKEN_SECRET')

# Filtreler ve Mesajlar
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

# HTML Formlar
LOGIN_FORM = """<!doctype html>
<title>Telegram Login</title>
<h2>Step 1: Enter your phone number</h2>
<form method="post">
  <input name="phone" placeholder="+1234567890" required>
  <button type="submit">Send Code</button>
</form>
"""

CODE_FORM = """<!doctype html>
<title>Enter the Code</title>
<h2>Step 2: Enter the code you received</h2>
<form method="post">
  <input name="code" placeholder="12345" required>
  <button type="submit">Verify</button>
</form>
"""

# Global Clients
user_client = TelegramClient('user_session', API_ID, API_HASH)
bot_client = TelegramClient('bot_session', API_ID, API_HASH)
app = Flask(__name__)
app.secret_key = os.urandom(24).hex()
bot_running = True

# ----------------------------------------------------------------------
# 2. VERİTABANI YÖNETİMİ
# ----------------------------------------------------------------------

def get_connection():
    """Database bağlantısı oluşturur"""
    try:
        return psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            host=DB_HOST,
            port=DB_PORT,
            sslmode="require"
        )
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        return None

def init_db():
    """Database tablolarını oluşturur"""
    conn = get_connection()
    if not conn:
        return
        
    try:
        with conn.cursor() as cur:
            # Sinyal takip tablosu
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_signals (
                    signal_key TEXT PRIMARY KEY,
                    source_channel TEXT NOT NULL,
                    target_message_id BIGINT,
                    tweet_id BIGINT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
            logger.info("✅ Database tabloları hazırlandı.")
    except Exception as e:
        logger.error(f"❌ Database başlatma hatası: {e}")
    finally:
        conn.close()

def get_signal_data(signal_key):
    """Sinyal verisini getirir"""
    conn = get_connection()
    if not conn:
        return None
        
    try:
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT target_message_id, tweet_id FROM processed_signals 
                WHERE signal_key = %s AND target_message_id IS NOT NULL AND tweet_id IS NOT NULL
            """, (signal_key,))
            result = cur.fetchone()
            return dict(result) if result else None
    except Exception as e:
        logger.error(f"Sinyal veri kontrol hatası: {e}")
        return None
    finally:
        conn.close()

def record_processed_signal(signal_key, target_message_id, tweet_id):
    """Sinyali veritabanına kaydeder"""
    conn = get_connection()
    if not conn:
        return False
        
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO processed_signals (signal_key, source_channel, target_message_id, tweet_id) 
                VALUES (%s, %s, %s, %s) 
                ON CONFLICT (signal_key) DO UPDATE SET 
                target_message_id = EXCLUDED.target_message_id, 
                tweet_id = EXCLUDED.tweet_id;
            """, (signal_key, SOURCE_CHANNEL, target_message_id, tweet_id))
            conn.commit()
            logger.info(f"✅ Sinyal kaydedildi: {signal_key}")
            return True
    except Exception as e:
        logger.error(f"❌ Sinyal kaydetme hatası: {e}")
        return False
    finally:
        conn.close()

# ----------------------------------------------------------------------
# 3. VERİ ÇIKARMA VE ŞABLONLAMA
# ----------------------------------------------------------------------

def extract_bet_data(message_text):
    """Bahis sinyalinden verileri çıkarır"""
    data = {}
    
    # Maç skoru
    match_score_match = re.search(r'⚽ (.*?)\s*\(.*?\)', message_text, re.DOTALL)
    data['maç_skor'] = match_score_match.group(0).strip().replace('⚽ ', '') if match_score_match else None
    
    # Lig
    lig_match = re.search(r'🏟 (.*?)\n', message_text)
    data['lig'] = lig_match.group(1).strip() if lig_match else None
    
    # Dakika
    dakika_match = re.search(r'⏰ (\d+)\s*', message_text)
    data['dakika'] = dakika_match.group(1).strip() if dakika_match else None
    
    # Tahmin
    tahmin_match = re.search(r'❗ (.*?)\n', message_text)
    tahmin_en_match = re.search(r'\((.*?)\)', tahmin_match.group(1)) if tahmin_match and '(' in tahmin_match.group(1) else tahmin_match
    data['tahmin'] = tahmin_en_match.group(1).strip() if tahmin_en_match else (tahmin_match.group(1).strip() if tahmin_match else None)
    
    # Alert Code
    alert_code_match = re.search(r'👉 AlertCode: (\d+)', message_text)
    data['alert_code'] = alert_code_match.group(1).strip() if alert_code_match else None

    # Sonuç ikonu
    result_match = re.match(r'([✅❌])', message_text.strip())
    data['result_icon'] = result_match.group(1) if result_match else None

    # Final skor
    final_score_match = re.search(r'#⃣ FT (\d+ - \d+)', message_text)
    data['final_score'] = final_score_match.group(1).strip() if final_score_match else None

    # Signal Key oluştur
    if all([data.get('maç_skor'), data.get('dakika'), data.get('tahmin')]):
        maç_temiz = re.sub(r'[\(\)]', '', data['maç_skor']).strip().replace(' ', '_').replace('-', '')
        tahmin_temiz = re.sub(r'[^\w\s]', '', data['tahmin']).strip().replace(' ', '_')
        data['signal_key'] = f"{maç_temiz}_{data['dakika']}_{tahmin_temiz}"
    else:
        data['signal_key'] = None
    
    return data if data['signal_key'] else None

def build_telegram_message(data):
    """Telegram mesaj şablonu"""
    return f"""
{data['maç_skor']}
{data['lig']}
{data['dakika']}. min
{data['tahmin']}
"""

def build_x_tweet(data):
    """Twitter mesaj şablonu"""
    return f"""
{data['maç_skor']} | {data['dakika']}. min
{data['tahmin']}
"""

def build_telegram_edit(result_icon):
    """Telegram sonuç güncelleme"""
    if result_icon == '✅':
        return "\n\n🟢 RESULT: WON! 🎉"
    elif result_icon == '❌':
        return "\n\n🔴 RESULT: LOST! 😔"
    return ""

def build_x_reply_tweet(data):
    """Twitter sonuç yanıtı"""
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
    """Twitter'a tweet atar"""
    try:
        if not all([X_CONSUMER_KEY, X_CONSUMER_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET]):
            logger.warning("❌ Twitter anahtarları eksik!")
            return None
        
        client = tweepy.Client(
            consumer_key=X_CONSUMER_KEY,
            consumer_secret=X_CONSUMER_SECRET,
            access_token=X_ACCESS_TOKEN,
            access_token_secret=X_ACCESS_TOKEN_SECRET
        )

        if reply_to_id:
            response = client.create_tweet(text=tweet_text, in_reply_to_tweet_id=reply_to_id)
            logger.info(f"✅ Twitter yanıtı gönderildi: {response.data['id']}")
        else:
            response = client.create_tweet(text=tweet_text)
            logger.info(f"✅ Yeni tweet gönderildi: {response.data['id']}")
            
        return response.data['id']
    except Exception as e:
        logger.error(f"❌ Tweet hatası: {e}")
        return None

# ----------------------------------------------------------------------
# 4. TELEGRAM MESAJ İŞLEYİCİLERİ
# ----------------------------------------------------------------------

@user_client.on(events.NewMessage(chats=SOURCE_CHANNEL))
async def handle_incoming_message(event):
    """Kaynak kanaldan gelen mesajları işler"""
    
    if not bot_running:
        return
        
    message_text = event.message.message
    data = extract_bet_data(message_text)
    
    if not data or not data['signal_key']:
        return

    is_result = data['result_icon'] is not None
    signal_key = data['signal_key']

    if is_result:
        # SONUÇ MESAJI İŞLEME
        signal_record = get_signal_data(signal_key)

        if signal_record:
            target_message_id = signal_record.get('target_message_id')
            tweet_id = signal_record.get('tweet_id')
            
            logger.info(f"🔁 Sonuç güncelleniyor: {signal_key}")
            
            # Telegram mesajını güncelle
            try:
                original_msg = await bot_client.get_messages(TARGET_CHANNEL, ids=target_message_id)
                new_text = original_msg.message + build_telegram_edit(data['result_icon'])
                
                await bot_client.edit_message(
                    entity=TARGET_CHANNEL,
                    message=target_message_id,
                    text=new_text,
                    buttons=BETTING_BUTTONS
                )
                logger.info(f"✅ Telegram mesajı güncellendi: {target_message_id}")
            except Exception as e:
                logger.error(f"❌ Telegram güncelleme hatası: {e}")
                
            # Twitter yanıtı gönder
            x_reply_tweet_text = build_x_reply_tweet(data)
            if x_reply_tweet_text and tweet_id:
                post_to_x_sync(x_reply_tweet_text, reply_to_id=tweet_id)

        else:
            logger.warning(f"⚠️ Sonuç bulunamadı: {signal_key}")

    else:
        # YENİ SİNYAL İŞLEME
        
        # AlertCode filtresi
        if data.get('alert_code') not in ALLOWED_ALERT_CODES:
            logger.info(f"⏭️ AlertCode filtrelendi: {data.get('alert_code')}")
            return

        # Tekrar kontrolü
        if get_signal_data(signal_key):
            logger.info(f"⏭️ Sinyal zaten işlenmiş: {signal_key}")
            return

        logger.info(f"🎯 Yeni sinyal: {signal_key}")

        # Şablonları oluştur
        telegram_message = build_telegram_message(data)
        x_tweet = build_x_tweet(data)
        tweet_id = None
        target_message_id = None

        # Twitter'a gönder
        tweet_id = post_to_x_sync(x_tweet)
        
        # Telegram'a gönder
        try:
            sent_message = await bot_client.send_message(
                entity=TARGET_CHANNEL,
                message=telegram_message,
                parse_mode='Markdown',
                buttons=BETTING_BUTTONS
            )
            target_message_id = sent_message.id
            logger.info(f"✅ Telegram mesajı gönderildi: {target_message_id}")
        except Exception as e:
            logger.error(f"❌ Telegram gönderme hatası: {e}")
            
        # Veritabanına kaydet
        record_processed_signal(signal_key, target_message_id, tweet_id)

# ----------------------------------------------------------------------
# 5. OTOMATİK MESAJ SİSTEMİ
# ----------------------------------------------------------------------

async def scheduled_post_task():
    """4 saatte bir otomatik mesaj gönderir"""
    interval = 4 * 60 * 60  # 4 saat
    
    # İlk çalışma için bekle
    now = time.time()
    next_run_time = (now // interval + 1) * interval
    initial_wait = next_run_time - now
    
    logger.info(f"⏰ Otomatik mesaj sistemi başlatıldı. İlk mesaj: {initial_wait:.0f} saniye sonra")
    
    await asyncio.sleep(initial_wait)
    
    while True:
        if bot_running and bot_client.is_connected():
            try:
                await bot_client.send_message(
                    entity=TARGET_CHANNEL,
                    message=SCHEDULED_MESSAGE,
                    parse_mode='Markdown'
                )
                logger.info("✅ Otomatik sponsor mesajı gönderildi")
            except Exception as e:
                logger.error(f"❌ Otomatik mesaj hatası: {e}")
        
        await asyncio.sleep(interval)

# ----------------------------------------------------------------------
# 6. FLASK WEB ARAYÜZÜ
# ----------------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Telegram login sayfası"""
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        if not phone:
            return "<p>❌ Phone number is required.</p>", 400
        
        session['phone'] = phone
        try:
            asyncio.run_coroutine_threadsafe(user_client.connect(), asyncio.get_event_loop())
            asyncio.run_coroutine_threadsafe(user_client.send_code_request(phone), asyncio.get_event_loop())
            logger.info(f"✅ Login code sent to {phone}")
            return redirect('/submit-code')
        except Exception as e:
            logger.error(f"❌ Login error: {e}")
            return f"<p>❌ Error: {e}</p>", 500
    
    return render_template_string(LOGIN_FORM)

@app.route('/submit-code', methods=['GET', 'POST'])
def submit_code():
    """Code verification sayfası"""
    if 'phone' not in session:
        return redirect('/login')

    phone = session['phone']

    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        if not code:
            return "<p>❌ Code is required.</p>", 400
        
        try:
            asyncio.run_coroutine_threadsafe(user_client.sign_in(phone, code), asyncio.get_event_loop())
            logger.info(f"✅ Logged in: {phone}")
            session.pop('phone', None)
            return """
            <h2>✅ Login Successful!</h2>
            <p>You can close this tab. The bot is now running.</p>
            <p><a href="/">Go to Dashboard</a></p>
            """
        except Exception as e:
            logger.error(f"❌ Login failed: {e}")
            return f"<p>❌ Login failed: {e}</p>", 400

    return render_template_string(CODE_FORM)

@app.route('/')
def index():
    """Ana sayfa"""
    return jsonify({
        "status": "ok", 
        "message": "Bot is running",
        "bot_state": "running" if bot_running else "stopped"
    }), 200

@app.route('/health')
def health():
    """Health check"""
    return jsonify({"status": "healthy"}), 200

# ----------------------------------------------------------------------
# 7. ANA BAŞLATMA SİSTEMİ
# ----------------------------------------------------------------------

async def main_async():
    """Ana asenkron fonksiyon"""
    # Database başlat
    init_db()
    
    # Telegram client'ları başlat
    await bot_client.start(bot_token=BOT_TOKEN)
    await user_client.start()
    
    logger.info("✅ Telegram clients started successfully")
    
    # Arkaplan görevlerini başlat
    asyncio.create_task(scheduled_post_task())
    
    # Botu çalıştır
    await user_client.run_until_disconnected()

def run_async():
    """Asenkron fonksiyonu thread'de çalıştır"""
    asyncio.run(main_async())

if __name__ == '__main__':
    # Telegram botunu ayrı thread'de başlat
    telegram_thread = threading.Thread(target=run_async, daemon=True)
    telegram_thread.start()
    
    # Flask'ı başlat
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"🚀 Starting Flask on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
