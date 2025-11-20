import os
import re
import asyncio
import logging
import threading
from telethon import TelegramClient, events, Button
import tweepy
import psycopg2
from psycopg2 import pool
from flask import Flask, jsonify, request, session, redirect, render_template_string
import time
import queue

# ----------------------------------------------------------------------
# 1. ORTAM DEĞİŞKENLERİ VE YAPILANDIRMA
# ----------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')
DEFAULT_ADMIN_ID = int(os.getenv('DEFAULT_ADMIN_ID', 0))

SOURCE_CHANNEL = os.getenv('SOURCE_CHANNEL')
TARGET_CHANNEL = os.getenv('TARGET_CHANNEL')

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

# Global variables
app = Flask(__name__)
app.secret_key = os.urandom(24).hex()
pg_pool = None
telegram_manager = None
bot_running = True

# ----------------------------------------------------------------------
# 2. TELEGRAM MANAGER - Ayrı thread'de çalışacak
# ----------------------------------------------------------------------

class TelegramManager:
    def __init__(self):
        self.user_client = None
        self.bot_client = None
        self.loop = None
        self.ready = False
        self.task_queue = queue.Queue()
        self.result_queue = queue.Queue()
    
    def start(self):
        """Telegram client'larını ayrı thread'de başlat"""
        def run_telegram():
            try:
                # Yeni event loop oluştur
                self.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self.loop)
                
                # Client'ları oluştur
                self.user_client = TelegramClient('user_session', API_ID, API_HASH, loop=self.loop)
                self.bot_client = TelegramClient('bot_session', API_ID, API_HASH, loop=self.loop)
                
                # Başlat
                self.loop.run_until_complete(self._start_clients())
                
                # Sürekli çalış
                self.loop.run_forever()
                
            except Exception as e:
                logging.error(f"❌ Telegram manager failed: {e}")
                self.ready = False
        
        telegram_thread = threading.Thread(target=run_telegram, daemon=True)
        telegram_thread.start()
        logging.info("✅ Telegram manager thread started")
        return telegram_thread
    
    async def _start_clients(self):
        """Client'ları başlat ve handler'ları kur"""
        try:
            # Bot client'ı başlat
            await self.bot_client.start(bot_token=BOT_TOKEN)
            logging.info("✅ Bot client started successfully")
            
            # User client'ı bağla (login değil)
            await self.user_client.connect()
            logging.info("✅ User client connected (ready for login)")
            
            # Handler'ları kur
            await self._setup_handlers()
            
            # Zamanlama görevini başlat
            asyncio.create_task(self._scheduled_post_task())
            
            self.ready = True
            logging.info("✅ Telegram manager is ready")
            
        except Exception as e:
            logging.error(f"❌ Client startup failed: {e}")
            self.ready = False
    
    async def _setup_handlers(self):
        """Message handler'ları kur"""
        @self.user_client.on(events.NewMessage(chats=int(SOURCE_CHANNEL) if SOURCE_CHANNEL and SOURCE_CHANNEL.startswith('-100') else SOURCE_CHANNEL))
        async def handle_incoming_message(event):
            await self._process_signal_message(event)
    
    async def _process_signal_message(self, event):
        """Sinyal mesajlarını işle"""
        global bot_running
        
        if not bot_running:
            return
            
        message_text = event.message.message
        data = await asyncio.to_thread(extract_bet_data, message_text)
        
        if not data or not data['signal_key']:
            return

        is_result = data['result_icon'] is not None
        signal_key = data['signal_key']

        if is_result:
            # SONUÇ MESAJI İŞLEME
            target_message_id, tweet_id = await asyncio.to_thread(get_signal_data, signal_key)

            if target_message_id and tweet_id:
                logging.info(f"Sonuç tespit edildi. Mesaj ID: {target_message_id}, Tweet ID: {tweet_id} düzenleniyor.")
                
                # 1. TELEGRAM MESAJINI DÜZENLE
                try:
                    original_msg = await self.bot_client.get_messages(TARGET_CHANNEL, ids=target_message_id)
                    new_text = original_msg.message + build_telegram_edit(data['result_icon'])
                    
                    await self.bot_client.edit_message(
                        entity=TARGET_CHANNEL,
                        message=target_message_id,
                        text=new_text,
                        buttons=BETTING_BUTTONS
                    )
                    logging.info(f"Telegram mesajı başarıyla düzenlendi: {target_message_id}")
                except Exception as e:
                    logging.error(f"Telegram mesaj düzenleme hatası: {e}")
                    
                # 2. X'E YANIT TWEET'İ GÖNDER
                x_reply_tweet_text = build_x_reply_tweet(data)
                if x_reply_tweet_text:
                    await asyncio.to_thread(post_to_x_sync, x_reply_tweet_text, reply_to_id=tweet_id)

            else:
                logging.warning(f"Sonuç geldi ancak orijinal sinyal veritabanında bulunamadı: {signal_key}")

        else:
            # YENİ SİNYAL İŞLEME
            if data.get('alert_code') not in ALLOWED_ALERT_CODES:
                logging.info(f"AlertCode: {data.get('alert_code')} izin verilenler listesinde değil. Atlanıyor.")
                return

            # Tekrar kontrolü
            if await asyncio.to_thread(get_signal_data, signal_key):
                logging.info(f"Sinyal {signal_key} daha önce işlenmiş. Atlanıyor.")
                return

            logging.info(f"Yeni AlertCode {data['alert_code']} sinyali tespit edildi: {signal_key}. İşleniyor...")

            # Şablonları oluştur
            telegram_message = build_telegram_message(data)
            x_tweet = build_x_tweet(data)
            tweet_id = None
            target_message_id = None

            # X'e post et
            tweet_id = await asyncio.to_thread(post_to_x_sync, x_tweet)
            
            # Telegram'a post et
            try:
                sent_message = await self.bot_client.send_message(
                    entity=TARGET_CHANNEL,
                    message=telegram_message,
                    parse_mode='Markdown',
                    buttons=BETTING_BUTTONS
                )
                target_message_id = sent_message.id
            except Exception as e:
                logging.error(f"Telegram post hatası: {e}")
                
            # Veritabanına kaydet
            if target_message_id:
                await asyncio.to_thread(record_processed_signal, signal_key, target_message_id, tweet_id)

    async def _scheduled_post_task(self):
        """Her 4 saatte bir otomatik mesaj gönderir."""
        interval = 4 * 60 * 60
        
        now = time.time()
        next_run_time = (now // interval + 1) * interval
        initial_wait = next_run_time - now
        
        logging.info(f"Otomatik gönderim döngüsü başlatılıyor. İlk gönderim için bekleme: {initial_wait:.2f} saniye.")
        
        await asyncio.sleep(initial_wait)
        
        while True:
            if bot_running and self.bot_client.is_connected():
                try:
                    await self.bot_client.send_message(
                        entity=TARGET_CHANNEL,
                        message=SCHEDULED_MESSAGE,
                        parse_mode='Markdown'
                    )
                    logging.info("Otomatik 4 saatlik gönderi başarıyla atıldı.")
                except Exception as e:
                    logging.error(f"Otomatik gönderi hatası: {e}")
            
            await asyncio.sleep(interval)

    def send_code_request(self, phone):
        """Telegram'a kod gönder"""
        if not self.loop or not self.ready:
            raise Exception("Telegram client not ready")
        
        async def _send_code():
            return await self.user_client.send_code_request(phone)
        
        future = asyncio.run_coroutine_threadsafe(_send_code(), self.loop)
        return future.result(timeout=30)
    
    def sign_in(self, phone, code):
        """Kod ile giriş yap"""
        if not self.loop or not self.ready:
            raise Exception("Telegram client not ready")
        
        async def _sign_in():
            return await self.user_client.sign_in(phone, code)
        
        future = asyncio.run_coroutine_threadsafe(_sign_in(), self.loop)
        return future.result(timeout=30)

# ----------------------------------------------------------------------
# 3. VERİTABANI YÖNETİMİ
# ----------------------------------------------------------------------

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
                conn.commit()
            logging.info("Veritabanı tabloları hazırlandı.")
        except Exception as e:
            logging.error(f"Veritabanı başlatma hatası: {e}")
        finally:
            pg_pool.putconn(conn)

def get_signal_data(signal_key):
    """Verilen signal_key'e ait mesaj ID ve Tweet ID'sini getirir."""
    conn = pg_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT target_message_id, tweet_id FROM processed_signals WHERE signal_key = %s", (signal_key,))
            result = cur.fetchone()
            return result if result else (None, None)
    except Exception as e:
        logging.error(f"Sinyal veri kontrol hatası: {e}")
        return None, None
    finally:
        pg_pool.putconn(conn)

def record_processed_signal(signal_key, target_message_id, tweet_id):
    """Yeni işlenen sinyali ve ID'lerini kaydeder."""
    conn = pg_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO processed_signals (signal_key, source_channel, target_message_id, tweet_id) 
                VALUES (%s, %s, %s, %s) ON CONFLICT (signal_key) DO NOTHING
                """, (signal_key, SOURCE_CHANNEL, target_message_id, tweet_id))
            conn.commit()
            logging.info(f"Sinyal ID, Mesaj ID ve Tweet ID kaydedildi: {signal_key}")
            return True
    except Exception as e:
        logging.error(f"Sinyal kaydetme hatası: {e}")
        return False
    finally:
        pg_pool.putconn(conn)

# ----------------------------------------------------------------------
# 4. VERİ ÇIKARMA VE ŞABLONLAMA
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

# ----------------------------------------------------------------------
# 5. X (TWITTER) İŞLEMLERİ
# ----------------------------------------------------------------------

def post_to_x_sync(tweet_text, reply_to_id=None):
    """Verilen metni X'e post eder ve Tweet ID'sini döndürür."""
    try:
        client = tweepy.Client(
            consumer_key=X_CONSUMER_KEY,
            consumer_secret=X_CONSUMER_SECRET,
            access_token=X_ACCESS_TOKEN,
            access_token_secret=X_ACCESS_TOKEN_SECRET
        )
        
        if reply_to_id:
            response = client.create_tweet(text=tweet_text, in_reply_to_tweet_id=reply_to_id)
            logging.info(f"X'e yanıt başarıyla post edildi: {response.data['id']}")
        else:
            response = client.create_tweet(text=tweet_text)
            logging.info(f"X'e yeni sinyal başarıyla post edildi: {response.data['id']}")
            
        return response.data['id']
    except tweepy.TweepyException as e:
        logging.error(f"X post hatası: {e}")
        return None
    except Exception as e:
        logging.error(f"Genel X hatası: {e}")
        return None

# ----------------------------------------------------------------------
# 6. FLASK ROTALARI - Basit ve hızlı
# ----------------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Basit login sayfası"""
    
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        if not phone:
            return "<p>❌ Phone number is required.</p>", 400
        
        if not phone.startswith('+'):
            return "<p>❌ Please use international format: +1234567890</p>", 400
        
        session['phone'] = phone
        
        try:
            telegram_manager.send_code_request(phone)
            logging.info(f"✅ Code sent to {phone}")
            return redirect('/submit-code')
            
        except Exception as e:
            logging.error(f"❌ Code send failed for {phone}: {e}")
            return f"""
            <h2>❌ Failed to Send Code</h2>
            <p><strong>Error:</strong> {str(e)}</p>
            <p><a href="/login">Try Again</a></p>
            """, 500
    
    return """
    <!doctype html>
    <html>
    <head>
        <title>Telegram Login</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 500px; margin: 50px auto; padding: 20px; }
            .form-group { margin: 20px 0; }
            input[type="text"] { width: 100%; padding: 12px; font-size: 16px; border: 1px solid #ddd; border-radius: 5px; }
            button { width: 100%; padding: 12px; font-size: 16px; background: #0088cc; color: white; border: none; border-radius: 5px; cursor: pointer; }
            button:hover { background: #006699; }
        </style>
    </head>
    <body>
        <h2>🔐 Telegram Login</h2>
        <form method="post">
            <div class="form-group">
                <input type="text" name="phone" placeholder="+1234567890" required>
            </div>
            <button type="submit">📲 Send Verification Code</button>
        </form>
    </body>
    </html>
    """

@app.route('/submit-code', methods=['GET', 'POST'])
def submit_code():
    """Kod doğrulama sayfası"""
    
    if 'phone' not in session:
        return redirect('/login')
    
    phone = session['phone']
    
    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        if not code:
            return "<p>❌ Verification code is required.</p>", 400
        
        try:
            telegram_manager.sign_in(phone, code)
            logging.info(f"✅ Successfully logged in: {phone}")
            session.pop('phone', None)
            
            return """
            <!doctype html>
            <html>
            <head>
                <title>Login Successful</title>
                <style>
                    body { font-family: Arial, sans-serif; max-width: 500px; margin: 50px auto; padding: 20px; text-align: center; }
                    .success { color: #28a745; font-size: 48px; }
                </style>
            </head>
            <body>
                <div class="success">✅</div>
                <h2>Login Successful!</h2>
                <p>Your Telegram account has been connected successfully.</p>
                <p>The bot is now ready to process signals.</p>
                <br>
                <a href="/">Go to Dashboard</a>
            </body>
            </html>
            """
            
        except Exception as e:
            logging.error(f"❌ Login failed for {phone}: {e}")
            return f"""
            <h2>❌ Verification Failed</h2>
            <p><strong>Error:</strong> {str(e)}</p>
            <p>Please check the code and try again.</p>
            <p><a href="/submit-code">Try Again</a> | <a href="/login">Use Different Number</a></p>
            """, 400
    
    return f"""
    <!doctype html>
    <html>
    <head>
        <title>Enter Verification Code</title>
        <style>
            body {{ font-family: Arial, sans-serif; max-width: 500px; margin: 50px auto; padding: 20px; }}
            .phone-info {{ background: #e8f5e8; padding: 10px; border-radius: 5px; margin: 15px 0; }}
            .form-group {{ margin: 20px 0; }}
            input[type="text"] {{ width: 100%; padding: 12px; font-size: 18px; text-align: center; border: 2px solid #ddd; border-radius: 5px; }}
            button {{ width: 100%; padding: 12px; font-size: 16px; background: #28a745; color: white; border: none; border-radius: 5px; cursor: pointer; }}
        </style>
    </head>
    <body>
        <h2>📋 Enter Verification Code</h2>
        <div class="phone-info">
            <strong>Phone:</strong> {phone}
        </div>
        <form method="post">
            <div class="form-group">
                <input type="text" name="code" placeholder="12345" required>
            </div>
            <button type="submit">✅ Verify Code</button>
        </form>
    </body>
    </html>
    """

@app.route('/status')
def status():
    """Sistem durumunu göster"""
    status_info = {
        'flask_running': True,
        'telegram_ready': telegram_manager.ready if telegram_manager else False,
        'bot_running': bot_running
    }
    
    return jsonify(status_info)

@app.route('/', methods=['GET'])
def health_check():
    """Ana sayfa"""
    return jsonify({
        "status": "ok",
        "message": "Bot infrastructure is running",
        "bot_state": "running" if bot_running else "stopped"
    }), 200

# ----------------------------------------------------------------------
# 7. UYGULAMA BAŞLATMA
# ----------------------------------------------------------------------

if __name__ == '__main__':
    # Database başlat
    try:
        init_db_pool()
        init_db()
        logging.info("✅ Database initialized")
    except Exception as e:
        logging.error(f"❌ Database init failed: {e}")
    
    # Telegram manager'ı başlat
    telegram_manager = TelegramManager()
    telegram_thread = telegram_manager.start()
    
    # Flask'ı başlat
    port = int(os.environ.get('PORT', 5000))
    logging.info(f"🚀 Starting Flask on port {port}")
    
    app.run(host='0.0.0.0', port=port, debug=False)
