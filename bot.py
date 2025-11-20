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
from hypercorn.asyncio import serve
from hypercorn.config import Config
from asgiref.wsgi import WsgiToAsgi # <<< EKSİK OLAN KRİTİK IMPORT EKLENDİ
import time

# ----------------------------------------------------------------------
# 1. ORTAM DEĞİŞKENLERİ VE YAPILANDIRMA
# ----------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')

DB_HOST = os.getenv('DB_HOST')
DB_PORT = os.getenv('DB_PORT', 5432)
DB_NAME = os.getenv('DB_NAME')
DB_USER = os.getenv('DB_USER')
DB_PASS = os.getenv('DB_PASS')

X_CONSUMER_KEY = os.getenv('X_CONSUMER_KEY')
X_CONSUMER_SECRET = os.getenv('X_CONSUMER_SECRET')
X_ACCESS_TOKEN = os.getenv('X_ACCESS_TOKEN')
X_ACCESS_TOKEN_SECRET = os.getenv('X_ACCESS_TOKEN_SECRET')

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

user_client = TelegramClient('user_session', API_ID, API_HASH)
bot_client = TelegramClient('bot_session', API_ID, API_HASH)
app = Flask(__name__)
app.secret_key = os.urandom(24).hex()
pg_pool = None
telethon_loop = None
telethon_ready = False
bot_running = True

# ----------------------------------------------------------------------
# 2. VERİTABANI YÖNETİMİ
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

def get_channels_sync(channel_type):
    """Kanal listesini senkron olarak çeker."""
    conn = None
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            cur.execute("SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'channels'")
            if cur.fetchone() is None:
                return []
                
            cur.execute("SELECT channel_id FROM channels WHERE channel_type = %s", (channel_type,))
            rows = cur.fetchall()
            return [{"channel_id": r["channel_id"]} for r in rows]
    except Exception as e:
        logger.error(f"Error getting {channel_type} channels sync: {e}")
        return []
    finally:
        if conn:
            conn.close()

def init_db_pool():
    global pg_pool
    try:
        pg_pool = pool.SimpleConnectionPool(
            1, 20, 
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            sslmode="require" if DB_HOST.endswith('render.com') else "allow"
        )
        logging.info("✅ Database connection pool created.")
    except Exception as e:
        logging.error(f"❌ Error creating connection pool: {e}")
        raise e

def init_db():
    conn = pg_pool.getconn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS processed_signals (
                        signal_key TEXT PRIMARY KEY,
                        source_channel TEXT NOT NULL,
                        target_message_id BIGINT,
                        tweet_id BIGINT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS admins (
                        user_id BIGINT PRIMARY KEY,
                        first_name TEXT NOT NULL,
                        last_name TEXT,
                        lang TEXT,
                        is_default BOOLEAN DEFAULT FALSE
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS channels (
                        id SERIAL PRIMARY KEY,
                        channel_id BIGINT NOT NULL UNIQUE,
                        username TEXT,
                        title TEXT,
                        channel_type TEXT CHECK (channel_type IN ('source','target'))
                    );
                """)
                conn.commit()
            logging.info("Veritabanı tabloları hazırlandı.")
        except Exception as e:
            logging.error(f"Veritabanı başlatma hatası: {e}")
        finally:
            pg_pool.putconn(conn)

def get_signal_data(signal_key):
    """Verilen signal_key'e ait mesaj ID ve Tweet ID'sini getirir."""
    conn = get_connection()
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
        if conn:
            conn.close()

def record_processed_signal(signal_key, target_message_id, tweet_id):
    """Yeni işlenen sinyali ve ID'lerini kaydeder."""
    conn = get_connection()
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
            logging.info(f"Signal recorded/updated: {signal_key}")
            return True
    except Exception as e:
        logging.error(f"Sinyal kaydetme hatası: {e}")
        return False
    finally:
        if conn:
            conn.close()

# ----------------------------------------------------------------------
# 3. VERİ ÇIKARMA VE ŞABLONLAMA
# ----------------------------------------------------------------------

def extract_bet_data(message_text):
    """Bahis sinyalinden ve sonuçtan gerekli verileri Regex ile çıkarır."""
    data = {}
    
    match_score_match = re.search(r'⚽ (.*?)\s*\(.*?\)', message_text, re.DOTALL)
    data['maç_skor'] = match_score_match.group(0).strip().replace('⚽ ', '') if match_score_match else None
    
    lig_match = re.search(r'🏟 (.*?)\n', message_text)
    data['lig'] = lig_match.group(1).strip() if lig_match else None
    
    dakika_match = re.search(r'⏰ (\d+)\s*', message_text)
    data['dakika'] = dakika_match.group(1).strip() if dakika_match else None
    
    tahmin_match = re.search(r'❗ (.*?)\n', message_text)
    tahmin_en_match = re.search(r'\((.*?)\)', tahmin_match.group(1)) if tahmin_match and '(' in tahmin_match.group(1) else tahmin_match
    data['tahmin'] = tahmin_en_match.group(1).strip() if tahmin_en_match else (tahmin_match.group(1).strip() if tahmin_match else None)
    
    alert_code_match = re.search(r'👉 AlertCode: (\d+)', message_text)
    data['alert_code'] = alert_code_match.group(1).strip() if alert_code_match else None

    result_match = re.match(r'([✅❌])', message_text.strip())
    data['result_icon'] = result_match.group(1) if result_match else None

    final_score_match = re.search(r'#⃣ FT (\d+ - \d+)', message_text)
    data['final_score'] = final_score_match.group(1).strip() if final_score_match else None

    if all([data.get('maç_skor'), data.get('dakika'), data.get('tahmin')]):
        maç_temiz = re.sub(r'[\(\)]', '', data['maç_skor']).strip().replace(' ', '_').replace('-', '')
        tahmin_temiz = re.sub(r'[^\w\s]', '', data['tahmin']).strip().replace(' ', '_')
        data['signal_key'] = f"{maç_temiz}_{data['dakika']}_{tahmin_temiz}"
    else:
        data['signal_key'] = None
    
    return data if data['signal_key'] else None

def build_telegram_message(data):
    """Ultra Minimalist İngilizce Şablonu (Yeni Sinyal)"""
    return f"""
{data['maç_skor']}
{data['lig']}
{data['dakika']}. min
{data['tahmin']}
"""

def build_x_tweet(data):
    """X (Twitter) için minimalist şablon (Yeni Sinyal)"""
    return f"""
{data['maç_skor']} | {data['dakika']}. min
{data['tahmin']}
"""

def build_telegram_edit(result_icon):
    """Telegram mesaj düzenlemesi için sonuç metni (İngilizce)"""
    if result_icon == '✅':
        return "\n\n🟢 RESULT: WON! 🎉"
    elif result_icon == '❌':
        return "\n\n🔴 RESULT: LOST! 😔"
    return ""

def build_x_reply_tweet(data):
    """X (Twitter) yanıt tweet'i için final şablonu (İngilizce)"""
    
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
    """Verilen metni X'e post eder ve Tweet ID'sini döndürür."""
    try:
        if not all([X_CONSUMER_KEY, X_CONSUMER_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET]):
            logger.warning("X anahtarları eksik! Tweet atılamıyor.")
            return None
        
        client_instance = tweepy.Client(
            consumer_key=X_CONSUMER_KEY,
            consumer_secret=X_CONSUMER_SECRET,
            access_token=X_ACCESS_TOKEN,
            access_token_secret=X_ACCESS_TOKEN_SECRET
        )

        if reply_to_id:
            response = client_instance.create_tweet(text=tweet_text, in_reply_to_tweet_id=reply_to_id)
            logger.info(f"X'e yanıt başarıyla post edildi: {response.data['id']}")
        else:
            response = client_instance.create_tweet(text=tweet_text)
            logger.info(f"X'e yeni sinyal başarıyla post edildi: {response.data['id']}")
            
        return response.data['id']
    except Exception as e:
        logger.error(f"Tweet atılamadı: {e}")
        return None

# ----------------------------------------------------------------------
# 5. TELEGRAM HANDLERS (Orijinal yapı korundu)
# ----------------------------------------------------------------------

@user_client.on(events.NewMessage(incoming=True, chats=[c['channel_id'] for c in get_channels_sync('source')]))
async def channel_handler(event):
    """Yeni sinyal ve sonuçları işler."""
    
    if not bot_running:
        return
        
    message_text = event.raw_text.strip()
    data = await asyncio.to_thread(extract_bet_data, message_text) # Sync extraction
    
    if not data or not data['signal_key']:
        return

    is_result = data['result_icon'] is not None
    signal_key = data['signal_key']

    if is_result:
        # --- A. SONUÇ MESAJI İŞLEME ---
        signal_record = await asyncio.to_thread(get_signal_data, signal_key)

        if signal_record:
            target_message_id = signal_record.get('target_message_id')
            tweet_id = signal_record.get('tweet_id')
            
            logger.info(f"Sonuç tespit edildi. Mesaj ID: {target_message_id}, Tweet ID: {tweet_id} düzenleniyor.")
            
            # 1. TELEGRAM MESAJINI DÜZENLE
            try:
                original_msg = await bot_client.get_messages(TARGET_CHANNEL, ids=target_message_id)
                new_text = original_msg.message + build_telegram_edit(data['result_icon'])
                
                await bot_client.edit_message(
                    entity=TARGET_CHANNEL,
                    message=target_message_id,
                    text=new_text,
                    buttons=BETTING_BUTTONS
                )
                logger.info(f"Telegram mesajı başarıyla düzenlendi: {target_message_id}")
            except Exception as e:
                logger.error(f"Telegram mesaj düzenleme hatası: {e}")
                
            # 2. X'E YANIT TWEET'İ GÖNDER
            x_reply_tweet_text = build_x_reply_tweet(data)
            if x_reply_tweet_text and tweet_id:
                 await asyncio.to_thread(post_to_x_sync, x_reply_tweet_text, reply_to_id=tweet_id)

        else:
            logger.warning(f"Sonuç geldi ancak orijinal sinyal veritabanında bulunamadı veya ID'ler eksik: {signal_key}")

    else:
        # --- B. YENİ SİNYAL İŞLEME ---
        
        # 1. Filtreleme Kontrolü (Alert Code)
        if data.get('alert_code') not in ALLOWED_ALERT_CODES:
            logger.info(f"AlertCode: {data.get('alert_code')} izin verilenler listesinde değil. Atlanıyor.")
            return

        # 2. Tekrar Kontrolü (Veritabanı) - Sadece TAMAMLANMIŞ kayıtları atlar
        if await asyncio.to_thread(get_signal_data, signal_key):
            logger.info(f"Sinyal {signal_key} daha önce işlenmiş (ve tamamlanmış). Atlanıyor.")
            return

        logger.info(f"Yeni AlertCode {data['alert_code']} sinyali tespit edildi: {signal_key}. İşleniyor...")

        # 3. Yayınlama
        telegram_message = build_telegram_message(data)
        x_tweet = build_x_tweet(data)
        tweet_id = None
        target_message_id = None

        # X'e (Twitter) Post Et
        tweet_id = await asyncio.to_thread(post_to_x_sync, x_tweet)
        
        # 5. Telegram'a Post Et
        try:
            sent_message = await bot_client.send_message(
                entity=TARGET_CHANNEL,
                message=telegram_message,
                parse_mode='Markdown',
                buttons=BETTING_BUTTONS
            )
            target_message_id = sent_message.id
        except Exception as e:
            logger.error(f"Telegram post hatası: {e}")
            
        # 6. Veritabanına Kaydet
        await asyncio.to_thread(record_processed_signal, signal_key, target_message_id, tweet_id)

# ----------------------------------------------------------------------
# 6. ASENKRON ZAMANLAMA GÖREVİ (4 SAAT)
# ----------------------------------------------------------------------

async def scheduled_post_task():
    """Her 4 saatte bir otomatik mesaj gönderir."""
    
    interval = 4 * 60 * 60
    
    now = time.time()
    next_run_time = (now // interval + 1) * interval
    initial_wait = next_run_time - now
    
    logger.info(f"Otomatik gönderim döngüsü başlatılıyor. İlk gönderim için bekleme süresi: {initial_wait:.2f} saniye.")
    
    await asyncio.sleep(initial_wait)
    
    try:
        target_entity = int(TARGET_CHANNEL)
    except ValueError:
        target_entity = TARGET_CHANNEL
        
    while True:
        if bot_running and bot_client.is_connected():
            try:
                await bot_client.send_message(
                    entity=target_entity,
                    message=SCHEDULED_MESSAGE,
                    parse_mode='Markdown'
                )
                logger.info("Otomatik 4 saatlik gönderi başarıyla atıldı.")
            except Exception as e:
                logger.error(f"Otomatik gönderi hatası: {e}")
        
        await asyncio.sleep(interval)

# ----------------------------------------------------------------------
# 7. YÖNETİM VE FLASK ROTALARI
# ----------------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
async def login():
    if request.method == 'POST':
        form = request.form
        phone = form.get('phone', '').strip()
        if not phone:
            return "<p>Phone number is required.</p>", 400
        session['phone'] = phone
        try:
            await user_client.connect()
            await user_client.send_code_request(phone)
            logger.info(f"Sent login code request to {phone}")
            return redirect('/submit-code')
        except Exception as e:
            logger.error(f"Error sending login code to {phone}: {e}")
            return f"<p>Error sending code: {e}</p>", 500
    return render_template_string(LOGIN_FORM)

@app.route('/submit-code', methods=['GET', 'POST'])
async def submit_code():
    if 'phone' not in session:
        return redirect('/login')

    phone = session['phone']

    if request.method == 'POST':
        form = request.form
        code = form.get('code', '').strip()
        if not code:
            return "<p>Code is required.</p>", 400
        try:
            await user_client.sign_in(phone, code)
            logger.info(f"Logged in user-client for {phone}")
            session.pop('phone', None)
            return "<p>Login successful! You can close this tab.</p>"
        except Exception as e:
            logger.error(f"Login failed for {phone}: {e}")
            return f"<p>Login failed: {e}</p>", 400

    return render_template_string(CODE_FORM)

@app.route('/')
def root():
    return jsonify(status="ok", message="Bot is running"), 200

@app.route('/health')
def health():
    return jsonify(status="ok"), 200

# ----------------------------------------------------------------------
# 8. ANA BAŞLATMA MANTIĞI (ASYNCIO GATHER VE HYPERCORN)
# ----------------------------------------------------------------------

async def main_bot_runner():
    """Botun ana asenkron görevlerini çalıştırır."""
    
    await init_db()
    
    # Client'ları başlat
    await bot_client.start(bot_token=BOT_TOKEN)
    await user_client.start()
    
    if not await user_client.is_user_authorized():
        logger.warning("⚠ User client not authorized. Please visit /login to authorize.")
        
    logger.info("✅ Bot clients started.")
    
    # Background görevleri başlat
    asyncio.create_task(scheduled_post_task())
    
    # Botu sonsuza kadar çalıştır
    await user_client.run_until_disconnected()

if __name__ == '__main__':
    # Flask app'i ASGI'ye çevir
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    from asgiref.wsgi import WsgiToAsgi
    
    asgi_app = WsgiToAsgi(app)
    config = Config()
    
    # Port ve binding ayarları
    config.bind = [f"0.0.0.0:{int(os.environ.get('PORT', '5000'))}"]
    config.accesslog = '-'
    config.errorlog = '-'
    
    async def runner():
        server_task = asyncio.create_task(serve(asgi_app, config))
        logger.info("Hypercorn server task created.")
        
        bot_task = asyncio.create_task(main_bot_runner())
        logger.info("Main bot task created.")
        
        await asyncio.gather(server_task, bot_task)
        
    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        logger.info("Bot interrupted by user. Shutting down.")
    except Exception as e:
        logger.critical(f"Unhandled exception in main runner: {e}")
