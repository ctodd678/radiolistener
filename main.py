import subprocess
import os
import time
import glob
import signal
import wave
from datetime import datetime
import smtplib
import json
from email.message import EmailMessage
from faster_whisper import WhisperModel

# --- 1. CONFIGURATION ---
def load_config():
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    with open(config_path, 'r') as f:
        return json.load(f)

config = load_config()
SENDER_EMAIL = config['sender_email']
APP_PASSWORD = config['app_password']
RECIPIENTS = config['recipients']

# Contest Timing & Logic
STRICT_KEYWORDS = ["keyword", "104536", "80 thousand", "eighty thousand", "cash plus"]
PRIZE_KEYWORDS = ["cash", "money", "win", "dollar", "thousand", "jackpot"]
STREAM_URL = "https://15723.live.streamtheworld.com/CHUMFMAAC_SC?dist=onlineradiobox"
MODEL_SIZE = "base"

# --- 2. DYNAMIC PATHING (Windows vs. N100 RAM Disk) ---
RAMDISK_PATH = "/mnt/ramdisk"
if os.path.exists(RAMDISK_PATH):
    BASE_DIR = os.path.join(RAMDISK_PATH, "radioscout")
else:
    # Windows/Local Fallback
    BASE_DIR = os.path.join(os.path.dirname(__file__), "data")

SEGMENT_DIR = os.path.join(BASE_DIR, "segments")
LOG_FILE = os.path.join(os.path.dirname(__file__), "chum_transcript.txt")

# Create directories if they don't exist
os.makedirs(SEGMENT_DIR, exist_ok=True)

# Globals for alert tracking
LAST_ALERT_TIME = 0
COOLDOWN_SECONDS = 600 

# --- 3. HELPER FUNCTIONS ---

def is_contest_active():
    """
    Weekdays: 6:00 AM - 8:00 PM
    Weekends: 1:00 PM - 6:00 PM
    """
    now = datetime.now()
    day = now.weekday() # 0=Mon, 6=Sun
    hour = now.hour
    if day < 5: 
        return 6 <= hour < 20
    else: 
        return 13 <= hour < 18

def send_email_blast(found_text):
    global LAST_ALERT_TIME
    current_time = time.time()
    if not is_contest_active():
        return
    if current_time - LAST_ALERT_TIME > COOLDOWN_SECONDS:
        hour_label = time.strftime("%I:00%p").lstrip('0')
        print(f"\n[!] ALERT TRIGGERED: Sending {hour_label} emails...")
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

# --- 4. THE ENGINE ---

def is_valid_wav(filepath, min_bytes=4096):
    """Returns True only if the file is a readable, non-empty WAV."""
    if os.path.getsize(filepath) < min_bytes:
        return False
    try:
        with wave.open(filepath, 'rb') as wf:
            return wf.getnframes() > 0
    except Exception:
        return False

def start_ffmpeg():
    """Starts a persistent background FFmpeg process to segment the stream."""
    print(f"[{time.strftime('%H:%M:%S')}] Connecting to persistent stream...")
    # Clean out old segments first
    for f in glob.glob(os.path.join(SEGMENT_DIR, "*.wav")):
        try: os.remove(f)
        except: pass

    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-user_agent', 'Mozilla/5.0',
        '-i', STREAM_URL,
        '-f', 'segment', '-segment_time', '15',
        '-segment_format', 'wav',
        '-acodec', 'pcm_s16le', '-ar', '16000',
        os.path.join(SEGMENT_DIR, 'chunk%03d.wav')
    ]
    # Uses shell=True on Windows to handle process groups better, False on Linux
    return subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)

def listen_and_spot():
    print(f"--- Radio Listener Active (Whisper {MODEL_SIZE}) ---")
    print(f"Storage: {SEGMENT_DIR}")
    
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    ffmpeg_proc = start_ffmpeg()

    try:
        while True:
            # 1. Health Check: If FFmpeg died, restart it
            if ffmpeg_proc.poll() is not None:
                print("⚠️ FFmpeg connection lost. Restarting...")
                ffmpeg_proc = start_ffmpeg()

            # 2. Monitor Segments
            files = sorted(glob.glob(os.path.join(SEGMENT_DIR, "*.wav")))
            
            # Only process files that FFmpeg has finished writing (at least 2 in list)
            if len(files) > 1:
                target_file = files[0]
                
                try:
                    if not is_valid_wav(target_file):
                        continue

                    segments, _ = model.transcribe(target_file, beam_size=1)
                    full_text = " ".join([s.text.strip() for s in segments]).strip()

                    if full_text:
                        timestamp = time.strftime('%H:%M:%S')
                        print(f"[{timestamp}] {full_text}")
                        
                        with open(LOG_FILE, "a", encoding="utf-8") as f:
                            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {full_text}\n")
                        
                        text_lower = full_text.lower()
                        is_strict = any(k in text_lower for k in STRICT_KEYWORDS)
                        is_prize = any(k in text_lower for k in PRIZE_KEYWORDS)
                        
                        if is_strict or (is_prize and is_contest_active()):
                            send_email_blast(full_text)
                
                except Exception as e:
                    print(f"❌ Transcription error: {e}")
                
                finally:
                    # Delete the file once done or if it failed
                    try:
                        os.remove(target_file)
                    except Exception as e:
                        print(f"⚠️ Could not delete {target_file}: {e}")

            time.sleep(2) # Polling interval

    except KeyboardInterrupt:
        print("\nScout shutting down...")
    finally:
        if os.name == 'nt':
            # Kills windows process
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(ffmpeg_proc.pid)], capture_output=True)
        else:
            ffmpeg_proc.terminate()

if __name__ == "__main__":
    listen_and_spot()