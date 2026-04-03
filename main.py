import subprocess
import os
import time
from datetime import datetime
import smtplib
import json
from email.message import EmailMessage
from faster_whisper import WhisperModel

# --- CONFIG ---
def load_config():
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    with open(config_path, 'r') as f:
        return json.load(f)

config = load_config()
SENDER_EMAIL = config['sender_email']
APP_PASSWORD = config['app_password']
RECIPIENTS = config['recipients']

# Keywords refined for the $80,000 contest
STRICT_KEYWORDS = ["keyword", "104536", "80 thousand", "eighty thousand", "cash plus"]
PRIZE_KEYWORDS = ["cash", "money", "win", "dollar", "thousand", "jackpot"]

STREAM_URL = "https://15723.live.streamtheworld.com/CHUMFMAAC_SC?dist=onlineradiobox"
MODEL_SIZE = "base"
LOG_FILE = os.path.join(os.path.dirname(__file__), "chum_transcript.txt")
CHUNK_PATH = "/mnt/ramdisk/chunk.wav" if os.path.exists("/mnt/ramdisk") else "chunk.wav"

LAST_ALERT_TIME = 0
COOLDOWN_SECONDS = 600 

def is_contest_active():
    now = datetime.now()
    day = now.weekday()
    hour = now.hour
    if day < 5: # Weekdays
        return 6 <= hour < 20
    else: # Weekends
        return 13 <= hour < 18

def send_email_blast(found_text):
    global LAST_ALERT_TIME
    current_time = time.time()
    
    # Only block if it's NOT a strict keyword outside of hours
    # This ensures we don't miss a 'test' or 'bonus' mention
    if not is_contest_active():
        return

    if current_time - LAST_ALERT_TIME > COOLDOWN_SECONDS:
        hour_label = time.strftime("%I:00%p").lstrip('0')
        print(f"\n[!] CONTEST ALERT: Sending {hour_label} emails...")
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(SENDER_EMAIL, APP_PASSWORD)
                for recipient in RECIPIENTS:
                    msg = EmailMessage()
                    msg.set_content(f"CHUM $80K ALERT at {hour_label}:\n\n\"{found_text}\"")
                    msg["Subject"] = f"🚨 {hour_label} Keyword Alert"
                    msg["From"] = SENDER_EMAIL
                    msg["To"] = recipient
                    server.send_message(msg)
            LAST_ALERT_TIME = current_time
        except Exception as e:
            print(f"❌ Email Error: {e}")

def listen_and_spot():
    print(f"--- $80K Scout Active (Whisper {MODEL_SIZE}) ---")
    print(f"Schedule: Weekdays 6a-8p | Weekends 1p-6p")
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")

    while True:
        if os.path.exists(CHUNK_PATH):
            os.remove(CHUNK_PATH)

        command = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
            '-user_agent', 'Mozilla/5.0',
            '-i', STREAM_URL, '-t', '10', '-f', 'wav', 
            '-acodec', 'pcm_s16le', '-ar', '16000', CHUNK_PATH
        ]

        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(CHUNK_PATH):
            time.sleep(5)
            continue

        segments, _ = model.transcribe(CHUNK_PATH, beam_size=1)
        for segment in segments:
            text = segment.text.strip()
            if not text: continue
            
            timestamp = time.strftime('%H:%M:%S')
            print(f"[{timestamp}] {text}")
            
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
            
            text_lower = text.lower()
            is_strict = any(k in text_lower for k in STRICT_KEYWORDS)
            is_prize = any(k in text_lower for k in PRIZE_KEYWORDS)
            
            # Primary Logic: Strict keywords always alert. 
            # Prize keywords only alert during contest windows.
            if is_strict or (is_prize and is_contest_active()):
                send_email_blast(text)

if __name__ == "__main__":
    listen_and_spot()