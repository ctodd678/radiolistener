import subprocess
import os
import time
import glob
import signal
import wave
import re
from datetime import datetime
import smtplib
import json
from email.message import EmailMessage
from faster_whisper import WhisperModel

# 1. CONFIGURATION
def load_config():
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    with open(config_path, 'r') as f:
        return json.load(f)

config = load_config()
SENDER_EMAIL = config['sender_email']
APP_PASSWORD = config['app_password']
RECIPIENTS = config['recipients']

STRICT_KEYWORDS = config.get('strict_keywords', [])
PRIZE_KEYWORDS = config.get('prize_keywords', [])

STREAM_URL = "https://15723.live.streamtheworld.com/CHUMFMAAC_SC?dist=onlineradiobox"
MODEL_SIZE = "base"

# 2. DYNAMIC PATHING
RAMDISK_PATH = "/mnt/ramdisk"
if os.path.exists(RAMDISK_PATH):
    BASE_DIR = os.path.join(RAMDISK_PATH, "radiolistener")
else:
    BASE_DIR = os.path.join(os.path.dirname(__file__), "data")

SEGMENT_DIR = os.path.join(BASE_DIR, "segments")
LOG_FILE = os.path.join(os.path.dirname(__file__), "radio_transcript.txt")

os.makedirs(SEGMENT_DIR, exist_ok=True)

LAST_ALERT_TIME = 0
COOLDOWN_SECONDS = 600 

# 3. HELPER FUNCTIONS
def is_contest_active():
    now = datetime.now()
    day = now.weekday()
    hour = now.hour
    if day < 5: 
        return 6 <= hour < 20
    else: 
        return 13 <= hour < 18

def send_email_blast(found_text):
    global LAST_ALERT_TIME
    current_time = time.time()
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

def keyword_spotted(text_chunk):
    text_lower = text_chunk.lower()
    clean_text = re.sub(r'[^\w\s]', '', text_lower)
    words_in_text = set(clean_text.split())

    for phrase in STRICT_KEYWORDS:
        if phrase.lower() in text_lower:
            return True

    if is_contest_active():
        for word in PRIZE_KEYWORDS:
            if word.lower() in words_in_text:
                return True

    return False

# 4. THE ENGINE
def is_valid_wav(filepath, min_bytes=4096):
    if os.path.getsize(filepath) < min_bytes:
        return False
    try:
        with wave.open(filepath, 'rb') as wf:
            return wf.getnframes() > 0
    except Exception:
        return False

def start_ffmpeg():
    print(f"[{time.strftime('%H:%M:%S')}] Connecting to stream...")
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
    return subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)

def listen_and_spot():
    print(f"--- Radio Listener Active (Whisper {MODEL_SIZE}) ---")
    print(f"Storage: {SEGMENT_DIR}")
    
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    ffmpeg_proc = start_ffmpeg()
    
    # WATCHDOG TIMER
    last_segment_time = time.time()
    MAX_STALL_SECONDS = 60 # If no file is produced in 60s, assume a freeze

    try:
        while True:
            current_time = time.time()
            
            # Health Check A: Did the process crash?
            process_died = ffmpeg_proc.poll() is not None
            
            # Health Check B: Did the stream silently freeze?
            stream_stalled = (current_time - last_segment_time) > MAX_STALL_SECONDS

            if process_died or stream_stalled:
                error_reason = "Process crashed" if process_died else "Stream stalled"
                print(f"[{time.strftime('%H:%M:%S')}] ⚠️ FFmpeg connection lost ({error_reason}). Restarting...")
                
                # Force kill the zombie if it's a stall
                if not process_died:
                    try:
                        if os.name == 'nt':
                            subprocess.run(['taskkill', '/F', '/T', '/PID', str(ffmpeg_proc.pid)], capture_output=True)
                        else:
                            ffmpeg_proc.kill()
                    except Exception as e:
                        print(f"Error killing stuck process: {e}")

                # Restart and reset the watchdog
                ffmpeg_proc = start_ffmpeg()
                last_segment_time = time.time() 

            files = sorted(glob.glob(os.path.join(SEGMENT_DIR, "*.wav")))
            
            if len(files) > 1:
                # We got a file! The stream is healthy.
                last_segment_time = time.time() 
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
                        
                        if keyword_spotted(full_text):
                            send_email_blast(full_text)
                
                except Exception as e:
                    print(f"❌ Transcription error: {e}")
                
                finally:
                    try:
                        os.remove(target_file)
                    except Exception as e:
                        print(f"⚠️ Could not delete {target_file}: {e}")

            time.sleep(2) 

    except KeyboardInterrupt:
        print("\nRadio Listener shutting down...")
    finally:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(ffmpeg_proc.pid)], capture_output=True)
        else:
            ffmpeg_proc.terminate()

if __name__ == "__main__":
    listen_and_spot()