import os
import re
import asyncio
import logging
import threading
from telethon import TelegramClient, events, Button
import tweepy
import psycopg2
from psycopg2 import pool
from flask import Flask, jsonify
import time

# ----------------------------------------------------------------------
# 1. ORTAM DEĞİŞKENLERİ VE YAPILANDIRMA
# ----------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Ortam değişkenlerinden çekilir (DB, API, X anahtarları)
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

# İZİN VERİLEN KODLAR (FILTRE)
ALLOWED_ALERT_CODES = {'17', '41', '32', '48', '1', '21'} 

# 4 SAATLİK OTOMATİK GÖNDERİ METNİ (İNGİLİZCE)
SCHEDULED_MESSAGE = """
✅ OUR SPONSOR SITES; 

⛔️Click the links and register without leaving the page.

💯 You can reach us to join the VIP group after making your investment 👇

🟢🟡Melbet 👉Promo Code: drpars
https://bit.ly/drparsbet

🔴🔵1xbet 👉Promo Code: drparsbet
bit.ly/3fAja06
"""

# GLOBAL BUTONLAR (HER SİNYALİN ALTINA)
BETTING_BUTTONS = [
    [
        Button.url("JOIN MELBET (drpars)", "https://bit.ly/drparsbet"),
        Button.url("JOIN 1XBET (drparsbet)", "http://bit.ly/3fAja06")
    ]
]

# Global Değişkenler
user_client = TelegramClient('user_session', API_ID, API_HASH)
bot_client = TelegramClient('bot_session', API_ID, API_HASH)
app = Flask(__name__)
pg_pool = None
bot_running = True

# ----------------------------------------------------------------------
# 2. VERİTABANI YÖNETİMİ
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

#LiveBet #BettingTips #FootballTips
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
# 4. X (TWITTER) İŞLEMLERİ
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
# 5. TELEGRAM İŞLEYİCİLERİ (HANDLER)
# ----------------------------------------------------------------------

@user_client.on(events.NewMessage(chats=int(SOURCE_CHANNEL) if SOURCE_CHANNEL and SOURCE_CHANNEL.startswith('-100') else SOURCE_CHANNEL))
async def handle_incoming_message(event):
    """Kaynak kanaldan gelen tüm mesajları işler (Yeni sinyal veya Sonuç)."""
    global bot_running

    if not bot_running:
        return
        
    message_text = event.message.message
    data = extract_bet_data(message_text)
    
    if not data or not data['signal_key']:
        return

    is_result = data['result_icon'] is not None
    signal_key = data['signal_key']

    if is_result:
        # --- A. SONUÇ MESAJI İŞLEME ---
        target_message_id, tweet_id = await asyncio.to_thread(get_signal_data, signal_key)

        if target_message_id and tweet_id:
            logging.info(f"Sonuç tespit edildi. Mesaj ID: {target_message_id}, Tweet ID: {tweet_id} düzenleniyor.")
            
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
                logging.info(f"Telegram mesajı başarıyla düzenlendi: {target_message_id}")
            except Exception as e:
                logging.error(f"Telegram mesaj düzenleme hatası: {e}")
                
            # 2. X'E YANIT TWEET'İ GÖNDER
            x_reply_tweet_text = build_x_reply_tweet(data)
            if x_reply_tweet_text:
                 await asyncio.to_thread(post_to_x_sync, x_reply_tweet_text, reply_to_id=tweet_id)

        else:
            logging.warning(f"Sonuç geldi ancak orijinal sinyal veritabanında bulunamadı veya ID'ler eksik: {signal_key}")

    else:
        # --- B. YENİ SİNYAL İŞLEME ---
        
        # 1. Filtreleme Kontrolü (Alert Code)
        if data.get('alert_code') not in ALLOWED_ALERT_CODES:
            logging.info(f"AlertCode: {data.get('alert_code')} izin verilenler listesinde değil. Atlanıyor.")
            return

        # 2. Tekrar Kontrolü (Veritabanı)
        if await asyncio.to_thread(get_signal_data, signal_key):
            logging.info(f"Sinyal {signal_key} daha önce işlenmiş. Atlanıyor.")
            return

        logging.info(f"Yeni AlertCode {data['alert_code']} sinyali tespit edildi: {signal_key}. İşleniyor...")

        # 3. Şablonları Oluştur
        telegram_message = build_telegram_message(data)
        x_tweet = build_x_tweet(data)
        tweet_id = None
        target_message_id = None

        # 4. X'e (Twitter) Post Et
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
            logging.error(f"Telegram post hatası: {e}")
            
        # 6. Başarılıysa Veritabanına Kaydet
        if target_message_id:
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
    
    logging.info(f"Otomatik gönderim döngüsü başlatılıyor. İlk gönderim için bekleme süresi: {initial_wait:.2f} saniye.")
    
    await asyncio.sleep(initial_wait)
    
    while True:
        if bot_running and bot_client.is_connected():
            try:
                await bot_client.send_message(
                    entity=TARGET_CHANNEL,
                    message=SCHEDULED_MESSAGE,
                    parse_mode='Markdown'
                )
                logging.info("Otomatik 4 saatlik gönderi başarıyla atıldı.")
            except Exception as e:
                logging.error(f"Otomatik gönderi hatası: {e}")
        
        await asyncio.sleep(interval)

# ----------------------------------------------------------------------
# 7. YÖNETİM VE FLASK (RENDER)
# ----------------------------------------------------------------------

@bot_client.on(events.NewMessage(pattern='/start', chats=DEFAULT_ADMIN_ID))
async def start_handler(event):
    """Admin'in botu başlatma komutu."""
    global bot_running
    if not bot_running:
        bot_running = True
        await event.respond('✅ Betting Signal Bot is RUNNING and listening for signals.')
    else:
        await event.respond('Bot is already running.')

@bot_client.on(events.NewMessage(pattern='/stop', chats=DEFAULT_ADMIN_ID))
async def stop_handler(event):
    """Admin'in botu durdurma komutu."""
    global bot_running
    if bot_running:
        bot_running = False
        await event.respond('🛑 Betting Signal Bot is STOPPED. New signals will not be processed.')
    else:
        await event.respond('Bot is already stopped.')

@app.route('/', methods=['GET'])
def health_check():
    """Render sağlık kontrolü (Health Check) endpoint'i."""
    return jsonify({
        "status": "ok",
        "message": "Bot infrastructure is running (Flask active).",
        "bot_state": "running" if bot_running else "stopped"
    }), 200

# ----------------------------------------------------------------------
# 8. ANA BAŞLATMA MANTIĞI
# ----------------------------------------------------------------------

def run_telethon_clients():
    """Telethon client'larını başlatır."""
    logging.info("Telethon clients starting...")
    
    # DB Pool'u ve tabloları başlat
    try:
        init_db_pool()
        init_db()
    except Exception as e:
        logging.error(f"CRITICAL: DB initialization failed. {e}")
        return

    # >>> KRİTİK HATA ÇÖZÜMÜ: Ayrı Thread'de Loop Oluşturma <<<
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def start_clients_and_tasks():
        try:
            # Client'ları asenkron olarak başlat
            await user_client.start()
            await bot_client.start(bot_token=BOT_TOKEN)
            logging.info("User Client and Bot Client started.")
            
            # Asenkron zamanlama görevini başlat
            loop.create_task(scheduled_post_task())
            
            # Ana döngüyü çalıştır (Bağlantı kesilene kadar bekle)
            await user_client.run_until_disconnected()

        except Exception as e:
            logging.error(f"Client startup failed or runtime error: {e}")
    
    # Loop'u çalıştır
    loop.run_until_complete(start_clients_and_tasks())


if __name__ == '__main__':
    # Telethon client'larını ayrı bir thread'de çalıştır
    telethon_thread = threading.Thread(target=run_telethon_clients)
    telethon_thread.daemon = True
    telethon_thread.start()
    
    # Flask uygulamasını ana thread'de çalıştır (Render)
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
