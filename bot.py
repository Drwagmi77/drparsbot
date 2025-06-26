import json
import time
import requests
import threading

# Telegram Bot Bilgileri
TELEGRAM_BOT_TOKEN = "7963189403:AAFw4xKnEeYBmGdgkyeNmHwcj5ipl2pIHsw"
TELEGRAM_CHAT_ID = "-1002701984074"

# SportMonks API bilgileri
SPORTMONKS_API_URL = "https://api.sportmonks.com/v3/football/livescores?include=localTeam,visitorTeam,league"
API_KEY = "O9tROfdiu5zhZ078qc8EwUe0uepND6J98wuS5UtOBKjmdWWBULgLi90FCzeG"

# Gönderilen sinyalleri takip dosyası
SIGNALS_DB_FILE = "signals_db.json"

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    r = requests.post(url, data=data)
    if r.status_code != 200:
        print("Telegram mesaj gönderilemedi:", r.text)

def load_signals_db():
    try:
        with open(SIGNALS_DB_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_signals_db(db):
    with open(SIGNALS_DB_FILE, "w") as f:
        json.dump(db, f, indent=4)

def get_live_matches():
    params = {
        "api_token": API_KEY
    }
    try:
        response = requests.get(SPORTMONKS_API_URL, params=params)
        if response.status_code == 200:
            data = response.json()
            return data.get("data", [])
        else:
            print("API çağrısı başarısız:", response.status_code, response.text)
            return []
    except Exception as e:
        print("API çağrısında hata:", e)
        return []

def analyze_match_for_signal(match):
    elapsed = match.get("time", {}).get("minute", 0)
    goals = match.get("scores", {}).get("localteam_score", 0) + match.get("scores", {}).get("visitorteam_score", 0)

    if 60 <= elapsed <= 70 and goals >= 1:
        return "+0.5 ÜST"

    if elapsed <= 20 and goals >= 1:
        return "İlk Yarı 1.5 ÜST"

    return None

def generate_signal_message(match, bet_type):
    home = match.get("localTeam", {}).get("data", {}).get("name", "Bilinmiyor")
    away = match.get("visitorTeam", {}).get("data", {}).get("name", "Bilinmiyor")
    league = match.get("league", {}).get("data", {}).get("name", "Lig Bilgisi Yok")
    elapsed = match.get("time", {}).get("minute", 0)
    goals = match.get("scores", {}).get("localteam_score", 0) + match.get("scores", {}).get("visitorteam_score", 0)

    emoji_new = "⚽🚀"

    message = (
        f"{emoji_new} <b>Yeni Bahis Sinyali!</b>\n"
        f"{home} - {away} | {league}\n"
        f"Sinyal: {bet_type}\n"
        f"Dakika: {elapsed}'\n"
        f"Goller: {goals}\n"
    )
    return message

def main_loop():
    print("SportMonks üzerinden sinyaller izleniyor...")
    signals_db = load_signals_db()

    while True:
        live_matches = get_live_matches()
        for match in live_matches:
            match_id = str(match.get("id"))
            bet_type = analyze_match_for_signal(match)

            if bet_type is None:
                continue

            signal_key = f"{match_id}-{bet_type}"
            if signal_key in signals_db:
                continue

            signals_db[signal_key] = {
                "match_id": match_id,
                "bet_type": bet_type,
                "timestamp": time.time()
            }

            message = generate_signal_message(match, bet_type)
            send_telegram_message(message)
            print("✅ Sinyal gönderildi:", message)

        save_signals_db(signals_db)
        time.sleep(60)

def send_periodic_message(message, interval_hours=4):
    while True:
        send_telegram_message(message)
        print("🕓 Periyodik mesaj gönderildi.")
        time.sleep(interval_hours * 3600)

if __name__ == "__main__":
    sabit_metin = """✅ AKTIF SIKINTISIZ SINIRSIZ CEKIM  

⛔️Direkt linklere tıklayıp kayıt olun, sayfadan çıkmadan.

💯 Yatırımınızı yapıp VIP gruba katılmak için ulaşabilirsiniz 👇

🟢🟡Melbet   👉promo kodu :drpars
https://bit.ly/drparsbet"""

    threading.Thread(target=send_periodic_message, args=(sabit_metin, 4), daemon=True).start()
    main_loop()
