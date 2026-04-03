import subprocess
import os
import time
import smtplib
import json
from email.message import EmailMessage
from faster_whisper import WhisperModel

# --- INITIALIZATION & CONFIG ---
def load_config():
    with open('config.json', 'r') as f:
        return json.load(f)

config = load_config()
SENDER_EMAIL = config['sender_email']
APP_PASSWORD = config['app_password']
RECIPIENTS = config['recipients']
KEYWORDS = config['keywords']

STREAM_URL = "https://15723.live.streamtheworld.com/CHUMFMAAC_SC?dist=onlineradiobox"
MODEL_SIZE = "base"
LOG_FILE = "chum_transcript.txt"
LAST_ALERT_TIME = 0
COOLDOWN_SECONDS = 600 

def write_to_log(text):
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {text}\n")
    except Exception as e:
        print(f"❌ Logging Error: {e}")

def send_email_blast(found_text):
    """Sends a direct email with the specific hour in the subject."""
    global LAST_ALERT_TIME
    current_time = time.time()
    
    if current_time - LAST_ALERT_TIME > COOLDOWN_SECONDS:
        # Generate the hour label (e.g., 4:00PM)
        # %I is 12-hour clock, %p is AM/PM. .lstrip('0') removes the leading zero.
        hour_label = time.strftime("%I:00%p").lstrip('0')
        
        print(f"\n[!] $80K KEYWORD DETECTED AT {hour_label}")
        print(f"📧 Blasting emails to {len(RECIPIENTS)} recipients...")
        
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(SENDER_EMAIL, APP_PASSWORD)
                
                for recipient in RECIPIENTS:
                    msg = EmailMessage()
                    msg.set_content(f"The {hour_label} keyword was just mentioned!\n\nDetected Text: \"{found_text}\"\n\nGo enter now!")
                    
                    # This is your requested dynamic subject line
                    msg["Subject"] = f"🚨 {hour_label} Keyword Alert"
                    msg["From"] = SENDER_EMAIL
                    msg["To"] = recipient
                    
                    server.send_message(msg)
                    print(f"   ✅ Sent to {recipient}")
                    time.sleep(0.5) 
                    
            LAST_ALERT_TIME = current_time
        except Exception as e:
            print(f"❌ Email Blast Error: {e}")

def listen_and_spot():
    print(f"--- $80K Scout Active (Whisper {MODEL_SIZE}) ---")
    print(f"Monitoring CHUM 104.5 for Keywords (7AM-7PM)...")

    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")

    while True:
        command = [
            'ffmpeg', 
            '-user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)',
            '-headers', 'Referer: https://www.iheart.com/',
            '-i', STREAM_URL, '-t', '10', '-f', 'wav', '-acodec', 'pcm_s16le', '-ar', '16000', 'chunk.wav', '-y'
        ]

        subprocess.run(command, capture_output=True, text=True)
        
        if os.path.exists("chunk.wav") and os.path.getsize("chunk.wav") > 5000:
            segments, _ = model.transcribe("chunk.wav", beam_size=1)
            
            for segment in segments:
                text = segment.text.strip()
                if text:
                    timestamp = time.strftime('%H:%M:%S')
                    print(f"[{timestamp}] {text}")
                    write_to_log(text)
                    
                    if any(key in text.lower() for key in KEYWORDS):
                        send_email_blast(text)
        else:
            time.sleep(2)

if __name__ == "__main__":
    try:
        listen_and_spot()
    except KeyboardInterrupt:
        print("\nScout offline. Good luck!")
        if os.path.exists("chunk.wav"): os.remove("chunk.wav")