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
# 1. ORTAM DEĞİŞKENLERİ
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

# --- AYARLAR ---
ALLOWED_ALERT_CODES = {'17', '41', '32', '48', '1', '21'} 
DAILY_TWEET_LIMIT = 999999 # SONSUZ MOD (Limit kaldırıldı)

# Linkler güncellendi
SCHEDULED_MESSAGE = """
✅ OUR SPONSOR SITES; 

⛔️Click the links and register without leaving the page.

💯 You can reach us to join the VIP group after making your investment 👇

🟢🟡Melbet 👉Promo Code: drpars
https://refpa3665.com/L?tag=d_728751m_45415c_&site=728751&ad=45415

🔴🔵1xbet 👉Promo Code: drparsbet
https://reffpa.com/L?tag=d_1868215m_97c_&site=1868215&ad=97
"""

# Butonlar güncellendi ve renk emojileri eklendi
BETTING_BUTTONS = [
    [
        Button.url("🟡 JOIN MELBET (drpars)", "https://refpa3665.com/L?tag=d_728751m_45415c_&site=728751&ad=45415"),
        Button.url("🔵 JOIN 1XBET (drparsbet)", "https://reffpa.com/L?tag=d_1868215m_97c_&site=1868215&ad=97")
    ]
]

ALERT_TEMPLATES = {
    '1': {'bet_type': "NEXT GOAL AFTER 65' (+0.5)", 'stake': "4/5", 'analysis': "High scoring pattern"},
    '17': {'bet_type': "TOTAL GOALS 2.5 OVER BEFORE 60'", 'stake': "4/5", 'analysis': "Fast paced game"},
    '21': {'bet_type': "TOTAL CORNERS - MATCH RESULT", 'stake': "3/5", 'analysis': "High corner frequency"},
    '32': {'bet_type': "TOTAL GOALS 3.5 OVER - MATCH RESULT", 'stake': "3/5", 'analysis': "High scoring game"},
    '41': {'bet_type': "3RD GOAL BEFORE 60' (V2)", 'stake': "4/5", 'analysis': "Early goals expected"},
    '47': {'bet_type': "TOTAL CORNERS - FIRST HALF", 'stake': "4/5", 'analysis': "High corner frequency"},
    '48': {'bet_type': "TOTAL CORNERS - MATCH RESULT", 'stake': "4/5", 'analysis': "High corner frequency"}
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
        logger.info("✅ X Client initialized")
    else:
        logger.warning("❌ X Credentials missing")
except Exception as e:
    logger.error(f"❌ X Init Error: {e}")

bot_running = True 

LOGIN_FORM = """<!doctype html><title>Login</title><h2>Phone</h2><form method=post><input name=phone placeholder="+90..." required><button>Send Code</button></form>"""
CODE_FORM = """<!doctype html><title>Code</title><h2>Enter Code</h2><form method=post><input name=code placeholder=12345 required><button>Login</button></form>"""

# ----------------------------------------------------------------------
# 2. VERİTABANI YÖNETİMİ
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
        conn.autocommit = True
        cur = conn.cursor()
        
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
        
        try:
            cur.execute("ALTER TABLE processed_signals ADD COLUMN status TEXT DEFAULT 'PENDING';")
            cur.execute("ALTER TABLE processed_signals ADD COLUMN source_message_id BIGINT;")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_source_msg_id ON processed_signals(source_message_id);")
        except: pass

        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                date DATE PRIMARY KEY,
                tweet_count INTEGER DEFAULT 0
            );
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                channel_id BIGINT UNIQUE,
                title TEXT,
                channel_type TEXT
            );
        """)
        logger.info("✅ DB Ready")
    except Exception as e:
        logger.error(f"DB Init Error: {e}")
    finally:
        if conn: conn.close()

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
            logger.info(f"💾 Saved ID: {source_message_id}")
            return True
    except Exception as e:
        logger.error(f"❌ DB Error: {e}")
        return False
    finally:
        if conn: conn.close()

def get_daily_tweet_count():
    # SONSUZ MOD: Her zaman 0 döndürür, böylece limit kontrolüne takılmaz.
    return 0

def increment_daily_tweet_count():
    pass

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
# 3. VERİ İŞLEME (GELİŞMİŞ TESPİT)
# ----------------------------------------------------------------------

def extract_bet_data(message_text):
    data = {}
    
    # 1. TEMİZLİK
    cleaned_text = re.sub(r'🏟\s*[\d\s\-]+', '🏟', message_text)
    cleaned_text = re.sub(r'^\s*\d+\s*-\s*\d+.*$', '', cleaned_text, flags=re.MULTILINE)
    
    # HEADER SCORE
    header_score_match = re.search(r'(?:🏟|⚽|🟢 LIVE UPDATE 🟢\s*)\s*(\d+\s*-\s*\d+)', message_text)
    if not header_score_match:
        header_score_match = re.search(r'^\s*(\d+\s*-\s*\d+)', message_text, re.MULTILINE)
    
    data['header_score'] = header_score_match.group(1).strip() if header_score_match else None

    # MAÇ SKORU
    match_score_match = re.search(r'⚽[️\s]*(.*?)\s*\(.*?\)', cleaned_text, re.DOTALL)
    if match_score_match:
        data['maç_skor'] = match_score_match.group(0).strip().replace('⚽', '').replace('️', '').strip()
    else:
        match_alt = re.search(r'([A-Za-z\s]+-\s*[A-Za-z\s]+)\s*\(\s*(\d+\s*-\s*\d+)\s*\)', cleaned_text)
        data['maç_skor'] = f"{match_alt.group(1)} ({match_alt.group(2)})" if match_alt else None
    
    # LİG
    lig_match = re.search(r'🏆\s*(.*?)\n', cleaned_text)
    if lig_match:
        data['lig'] = lig_match.group(1).strip()
    else:
        lig_alt = re.search(r'🏟\s*(.*?)\n', cleaned_text)
        text_yan = lig_alt.group(1).strip() if lig_alt else ""
        if text_yan and not re.match(r'^\d+\s*-\s*\d+$', text_yan):
            data['lig'] = text_yan
        else:
            data['lig'] = "League Info"
    
    # DAKİKA
    dakika_match = re.search(r'⏰\s*(\d+)\s*', cleaned_text)
    data['dakika'] = dakika_match.group(1).strip() if dakika_match else None
    
    # TAHMİN
    tahmin_match = re.search(r'❗[️\s]*(.*?)\n', cleaned_text)
    if tahmin_match:
        tahmin_text = tahmin_match.group(1).strip()
        
        is_corner_word = "corner" in tahmin_text.lower() or "korner" in tahmin_text.lower()
        corner_match = re.search(r'(\d+\.?\d*)\s*(üst|over|alt|under)', tahmin_text, re.IGNORECASE)
        
        if is_corner_word and corner_match:
            data['corner_number'] = corner_match.group(1)
            data['is_corner'] = True
        else:
            data['is_corner'] = False
            data['corner_number'] = None
            
        tahmin_en = re.search(r'\((.*?)\)', tahmin_text)
        data['tahmin'] = tahmin_en.group(1).strip() if tahmin_en else tahmin_text
    else:
        data['tahmin'] = None
        data['is_corner'] = False
    
    # ALERT CODE
    alert_code_match = re.search(r'👉\s*AlertCode:\s*(\d+)', cleaned_text)
    data['alert_code'] = alert_code_match.group(1).strip() if alert_code_match else None
    
    # SONUÇ (WON/LOST)
    result_match = re.search(r'([✅❌])', message_text) 
    data['result_icon'] = result_match.group(1) if result_match else None

    # LIVE UPDATE TESPİTİ
    live_title = re.search(r'🟢 LIVE UPDATE 🟢', message_text)
    live_score_match = re.search(r'⏰\s*(\d+)\s*⚽[️\s]*(\d+\s*-\s*\d+)', cleaned_text)
    
    if live_title or (data.get('header_score') and data.get('dakika')):
        data['is_live_update'] = True
    else:
        data['is_live_update'] = False

    # MAÇ BİTTİ Mİ?
    ft_match = re.search(r'#⃣\s*FT\s*(\d+\s*-\s*\d+)', cleaned_text)
    if ft_match:
        data['match_ended'] = True
        data['final_score'] = ft_match.group(1).strip()
    else:
        data['match_ended'] = False

    # SIGNAL KEY
    if all([data.get('maç_skor'), data.get('tahmin'), data.get('alert_code')]):
        maç_adi = data['maç_skor'].split(' (')[0].strip()
        maç_temiz = re.sub(r'[^A-Za-z\s]', '', maç_adi).strip().replace(' ', '_')
        
        tahmin_raw = data['tahmin']
        tahmin_temiz = re.sub(r'\d+\.?\d*\s*', '', tahmin_raw)
        tahmin_temiz = re.sub(r'[^\w\s\.]', '', tahmin_temiz)
        tahmin_temiz = re.sub(r'\s+', '_', tahmin_temiz.strip())
        
        data['signal_key'] = f"{maç_temiz}_{data['alert_code']}_{tahmin_temiz}"
    else:
        data['signal_key'] = None
    
    return data if data['signal_key'] else None

def build_telegram_message(data):
    alert_code = data.get('alert_code')
    template = ALERT_TEMPLATES.get(alert_code, {})
    
    tahmin_lower = data.get('tahmin', '').lower()
    
    # 1. STATE CONFIGURATION
    if data.get('result_icon') == '✅':
        state = "WON"
    elif data.get('result_icon') == '❌':
        state = "LOST"
    elif data.get('match_ended'):
        state = "ENDED"
    elif data.get('header_score'): 
        state = "UPDATE"
    else:
        state = "NEW"

    # 2. MESSAGE CONTENT
    if state == "WON":
        main_title = "✅✅ BET WON! ✅✅"
        final_sc = data.get('final_score', data.get('header_score', 'Finished'))
        top_info_line = f"🏁 Full Time: {final_sc}"
        footer_status = "💵 Result: WON (PROFIT)"
    
    elif state == "LOST":
        main_title = "❌ BET LOST"
        top_info_line = f"🏁 Full Time: {data.get('final_score', 'Finished')}"
        footer_status = "💵 Result: LOST"
    
    elif state == "ENDED":
        main_title = "🏁 MATCH FINISHED"
        top_info_line = f"🏁 Full Time: {data.get('final_score', 'Finished')}"
        footer_status = "Result: Match Ended"

    elif state == "UPDATE":
        main_title = f"⚽ GOAL UPDATE! ({data.get('dakika', '')}') Score: {data['header_score']}"
        entry_score = data['maç_skor'].split('(')[-1].replace(')', '') if '(' in data['maç_skor'] else '0-0'
        top_info_line = f"⏰ {data.get('dakika')}' │ 📊 Entry Score: {entry_score}"
        footer_status = "Status: Bet Pending... (Match Active)"

    else: # NEW
        if data.get('is_corner'):
            main_title = "🎯 LIVE CORNER SIGNAL 🎯"
        else:
            main_title = template.get('title', "🎯 LIVE BETTING SIGNAL")
        
        entry_score = data['maç_skor'].split('(')[-1].replace(')', '') if '(' in data['maç_skor'] else 'Live'
        top_info_line = f"⏰ {data.get('dakika')}' │ 📊 Score: {entry_score}"
        footer_status = "⚡ Status: Bet Now!"

    # 3. BET TYPE
    if data.get('is_corner') and data.get('corner_number'):
        bet_type = f"TOTAL CORNERS {data.get('corner_number', '')} OVER"
    else:
        bet_type = template.get('bet_type', data['tahmin'])

    return f"""
{main_title}

{top_info_line}
🏆 {data['lig']}
🆚 {data['maç_skor']}

🎯 {bet_type}

📉 Analysis: {template.get('analysis', 'Professional betting signal')}
💰 Confidence: {template.get('stake', '3/5')}
{footer_status}
"""

def build_x_tweet(data):
    if data.get('is_corner'):
        main_title = "🎯 LIVE CORNER SIGNAL 🎯"
        bet_type = f"TOTAL CORNERS {data.get('corner_number', '')} OVER"
    else:
        main_title = "🎯 LIVE GOAL SIGNAL 🎯"
        bet_type = data['tahmin']
    
    return f"""
{main_title}

{data['maç_skor']}
{data['dakika']}'
{data['lig']}

🎯 {bet_type}

💸 Stake: 3/5

#Betting #SportsBetting
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
            logger.warning(f"⚠️ X Post Retry: {e}")
    return None

async def post_to_x_async(text, reply_id=None):
    return await asyncio.to_thread(post_to_x_sync, text, reply_id)

# ----------------------------------------------------------------------
# 4. HANDLER
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

async def update_existing_message(data, signal_record):
    target_message_id = signal_record.get('target_message_id')
    tweet_id = signal_record.get('tweet_id')
    
    # 1. TELEGRAM
    targets = get_channels_sync('target')
    for t in targets:
        try:
            await bot_client.edit_message(
                t['channel_id'], 
                target_message_id, 
                text=build_telegram_message(data),
                buttons=BETTING_BUTTONS
            )
            logger.info(f"✏️ Telegram Updated")
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" in str(e):
                logger.info("ℹ️ Mesaj içeriği aynı, güncelleme gerekmedi.")
            elif "MESSAGE_ID_INVALID" in str(e):
                logger.warning("⚠️ Mesaj bulunamadı veya silinmiş.")
            else:
                logger.error(f"❌ Telegram Edit error: {e}")
    
    # 2. X (Twitter)
    if (data.get('match_ended') or data.get('result_icon')) and tweet_id:
        x_reply = build_x_reply_tweet(data)
        if x_reply:
            await post_to_x_async(x_reply, tweet_id)

async def channel_handler(event):
    if not bot_running: return
    
    source_msg_id = event.id
    message_text = event.raw_text.strip()
    data = await asyncio.to_thread(extract_bet_data, message_text)

    if not data or not data['signal_key']: return
    if data.get('alert_code') not in ALLOWED_ALERT_CODES: return

    signal_record = await asyncio.to_thread(get_signal_by_source_id, source_msg_id)
    
    # UPDATE
    if signal_record:
        logger.info(f"🔄 UPDATE (ID: {source_msg_id})")
        await update_existing_message(data, signal_record)

    # NEW
    else:
        if isinstance(event, events.MessageEdited): return

        logger.info(f"🆕 NEW SIGNAL (ID: {source_msg_id})")

        # X Tweet (Limitsiz)
        tweet_id = None
        try:
            tweet_id = await post_to_x_async(build_x_tweet(data))
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
        
        # Save
        if target_message_id:
            await asyncio.to_thread(record_processed_signal, data['signal_key'], target_message_id, tweet_id, source_msg_id)

# ----------------------------------------------------------------------
# 5. FLASK
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
        
        user_client.add_event_handler(channel_handler, events.NewMessage(incoming=True, chats=source_ids))
        user_client.add_event_handler(channel_handler, events.MessageEdited(chats=source_ids))
        
        logger.info(f"📡 Bot Ready (Source ID: {source_ids})")
        
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
