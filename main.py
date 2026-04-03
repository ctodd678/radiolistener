import subprocess
import os
import time
import glob
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

STRICT_KEYWORDS = ["keyword", "104536", "80 thousand", "eighty thousand", "cash plus"]
PRIZE_KEYWORDS = ["cash", "money", "win", "dollar", "thousand", "jackpot"]

STREAM_URL = "https://15723.live.streamtheworld.com/CHUMFMAAC_SC?dist=onlineradiobox"
MODEL_SIZE = "base"
LOG_FILE = os.path.join(os.path.dirname(__file__), "chum_transcript.txt")
# Using a dedicated subfolder for segments keeps things clean
SEGMENT_DIR = "segments"
if not os.path.exists(SEGMENT_DIR):
    os.makedirs(SEGMENT_DIR)

LAST_ALERT_TIME = 0
COOLDOWN_SECONDS = 600 

# --- HELPERS ---

def is_contest_active():
    now = datetime.now()
    day = now.weekday() 
    hour = now.hour
    if day < 5: return 6 <= hour < 20
    else: return 13 <= hour < 18

def send_email_blast(found_text):
    global LAST_ALERT_TIME
    current_time = time.time()
    if not is_contest_active(): return
    if current_time - LAST_ALERT_TIME > COOLDOWN_SECONDS:
        hour_label = time.strftime("%I:00%p").lstrip('0')
        print(f"\n[!] ALERT: Sending emails for {hour_label}...")
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(SENDER_EMAIL, APP_PASSWORD)
                for recipient in RECIPIENTS:
                    msg = EmailMessage()
                    msg.set_content(f"CHUM $80K Alert at {hour_label}:\n\n\"{found_text}\"")
                    msg["Subject"] = f"🚨 {hour_label} Keyword Alert"
                    msg["From"] = SENDER_EMAIL
                    msg["To"] = recipient
                    server.send_message(msg)
            LAST_ALERT_TIME = current_time
        except Exception as e: print(f"❌ Email Error: {e}")

# --- THE SCOUT ---

def listen_and_spot():
    print(f"--- Persistent Scout Active ---")
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")

    # Start FFmpeg in the background to segment the stream
    # This keeps the connection OPEN so we bypass pre-rolls
    ffmpeg_cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-user_agent', 'Mozilla/5.0',
        '-i', STREAM_URL,
        '-f', 'segment', '-segment_time', '15',
        '-segment_format', 'wav',
        '-acodec', 'pcm_s16le', '-ar', '16000',
        os.path.join(SEGMENT_DIR, 'chunk%03d.wav')
    ]
    
    print("Connecting to live stream (Persistent)...")
    ffmpeg_proc = subprocess.Popen(ffmpeg_cmd)

    try:
        while True:
            # Look for finished segments (exclude the one currently being written)
            files = sorted(glob.glob(os.path.join(SEGMENT_DIR, "*.wav")))
            
            # We process all but the last one (which FFmpeg is currently writing to)
            if len(files) > 1:
                target_file = files[0]
                
                # Transcribe
                segments, _ = model.transcribe(target_file, beam_size=1)
                full_text = " ".join([s.text.strip() for s in segments]).strip()

                if full_text:
                    print(f"[{time.strftime('%H:%M:%S')}] {full_text}")
                    with open(LOG_FILE, "a", encoding="utf-8") as f:
                        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {full_text}\n")
                    
                    text_lower = full_text.lower()
                    if any(k in text_lower for k in STRICT_KEYWORDS) or \
                       (any(k in text_lower for k in PRIZE_KEYWORDS) and is_contest_active()):
                        send_email_blast(full_text)

                # Delete processed segment
                os.remove(target_file)
            
            time.sleep(2) # Poll for new segments every 2 seconds

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        ffmpeg_proc.terminate()
        # Clean up any leftover segments
        for f in glob.glob(os.path.join(SEGMENT_DIR, "*.wav")):
            os.remove(f)

if __name__ == "__main__":
    listen_and_spot()