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
        data = json.load(f)
    
    print(f"--- Configuration Loaded ---")
    print(f"STRICT: {data.get('strict_keywords', [])}")
    print(f"SHORTCODES: {data.get('shortcodes', [])}")
    print(f"EXCLUSIONS: {data.get('exclude_keywords', [])}")
    return data

config = load_config()
SENDER_EMAIL = config['sender_email']
APP_PASSWORD = config['app_password']
RECIPIENTS = config['recipients']

STREAM_URL = "https://15723.live.streamtheworld.com/CHUMFMAAC_SC?dist=onlineradiobox"
MODEL_SIZE = "small" 

# 2. DYNAMIC PATHING
RAMDISK_PATH = "/mnt/ramdisk"
if os.path.exists(RAMDISK_PATH):
    BASE_DIR = os.path.join(RAMDISK_PATH, "radiolistener")
else:
    BASE_DIR = os.path.join(os.path.dirname(__file__), "data")

SEGMENT_DIR = os.path.join(BASE_DIR, "segments")
LOG_FILE = os.path.join(os.path.dirname(__file__), "radio_transcript.txt")

os.makedirs(SEGMENT_DIR, exist_ok=True)

# 3. HELPER FUNCTIONS
def is_contest_active():
    """
    Weekdays: 6:00 AM to 8:00 PM
    Weekends: 1:00 PM to 6:00 PM
    """
    now = datetime.now()
    day = now.weekday()
    hour = now.hour
    if day < 5: 
        return 6 <= hour < 20
    else: 
        return 13 <= hour < 18

def send_email_blast(found_text):
    timestamp = time.strftime("%I:%M%p").lstrip('0')
    print(f"\n[!] KEYWORD DETECTED: Sending alert for: {found_text}")
    
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(SENDER_EMAIL, APP_PASSWORD)
            for recipient in RECIPIENTS:
                msg = EmailMessage()
                msg.set_content(f"Radio Listener Alert at {timestamp}:\n\n\"{found_text}\"")
                msg["Subject"] = f"🚨 Radio Alert: {timestamp}"
                msg["From"] = SENDER_EMAIL
                msg["To"] = recipient
                server.send_message(msg)
            print(f"✅ Emails sent successfully to {len(RECIPIENTS)} recipients.")
    except Exception as e:
        print(f"❌ Email Error: {e}")

def keyword_spotted(text_chunk):
    text_lower = text_chunk.lower()
    
    # 1. EXCLUSIONS (Immediate Kill for PSAs/Teasers)
    EXCLUDE_KEYWORDS = config.get('exclude_keywords', [])
    for bad_word in EXCLUDE_KEYWORDS:
        if bad_word.lower() in text_lower:
            return False

    # 2. DYNAMIC SPELLING DETECTOR
    # Looks for 4 or more single letters separated by hyphens or spaces
    # Matches "w-i-l-d", "c-a-s-h", "m o n e y", etc.
    spelling_regex = r'\b([a-z](?:[- ][a-z]){3,})\b'
    has_spelled_word = bool(re.search(spelling_regex, text_lower))

    # 3. ANCHOR PHRASES (High Confidence)
    STRICT_KEYWORDS = config.get('strict_keywords', [])
    for phrase in STRICT_KEYWORDS:
        if phrase.lower() in text_lower:
            return True

    # 4. SHORTCODE & CONTEXT LOGIC
    SHORTCODES = config.get('shortcodes', [])
    PRIZE_WORDS = config.get('prize_keywords', [])
    
    has_shortcode = any(code in text_lower for code in SHORTCODES)
    has_prize_context = any(word in text_lower for word in PRIZE_WORDS)
    has_keyword_mention = "keyword" in text_lower

    # If the DJ is dynamically spelling a word out...
    if has_spelled_word:
        # And they also mention a shortcode OR the word "keyword", trigger immediately.
        if has_shortcode or has_keyword_mention or has_prize_context:
            return True

    # If they mention the text line (104536), they MUST also mention a contest word
    if has_shortcode:
        if has_prize_context or has_keyword_mention:
            return True
        return False

    # 5. GENERAL PRIZE CHECK (Contest Hours Only)
    if is_contest_active() and has_prize_context and has_keyword_mention:
        return True

    return False

# 4. THE ENGINE
def is_valid_wav(filepath, min_bytes=8192):
    if not os.path.exists(filepath) or os.path.getsize(filepath) < min_bytes:
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
    
    last_segment_time = time.time()
    MAX_STALL_SECONDS = 60 

    try:
        while True:
            current_time = time.time()
            process_died = ffmpeg_proc.poll() is not None
            stream_stalled = (current_time - last_segment_time) > MAX_STALL_SECONDS

            if process_died or stream_stalled:
                reason = "Crash" if process_died else "Stall"
                print(f"[{time.strftime('%H:%M:%S')}] ⚠️ FFmpeg {reason}. Restarting...")
                
                if not process_died:
                    try:
                        if os.name == 'nt':
                            subprocess.run(['taskkill', '/F', '/T', '/PID', str(ffmpeg_proc.pid)], capture_output=True)
                        else:
                            ffmpeg_proc.kill()
                    except: pass

                ffmpeg_proc = start_ffmpeg()
                last_segment_time = time.time() 

            files = sorted(glob.glob(os.path.join(SEGMENT_DIR, "*.wav")))
            
            if len(files) > 1:
                last_segment_time = time.time() 
                target_file = files[0]
                
                try:
                    if not is_valid_wav(target_file):
                        continue

                    segments, _ = model.transcribe(
                        target_file, 
                        beam_size=1, 
                        vad_filter=True,
                        condition_on_previous_text=False,
                        temperature=0.0
                    )
                    
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
                    except: pass

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