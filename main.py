import subprocess
import os
import time
import glob
import wave
import re
from datetime import datetime
import smtplib
import json
from email.message import EmailMessage
from faster_whisper import WhisperModel
import threading
import logging

# logging to both console and file
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("radio_listener.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)
logging.getLogger("faster_whisper").setLevel(logging.ERROR)

# --- CONFIG ---
def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, "r") as f:
        return json.load(f)

def load_keywords():
    # separate from config so I can push keyword changes to github
    # and pull them on the server without touching credentials
    keywords_path = os.path.join(os.path.dirname(__file__), "keywords.json")
    with open(keywords_path, "r") as f:
        data = json.load(f)
    log.info(
        f"Keywords loaded | "
        f"STRICT: {data.get('strict_keywords', [])} | "
        f"SHORTCODES: {data.get('shortcodes', [])} | "
        f"EXCLUSIONS: {data.get('exclude_keywords', [])}"
    )
    return data

config   = load_config()
keywords = load_keywords()

SENDER_EMAIL = config["sender_email"]
APP_PASSWORD = config["app_password"]
RECIPIENTS   = config["recipients"]

STREAM_URL = "https://playerservices.streamtheworld.com/api/livestream-redirect/CHUMFM_ADP.m3u8"
# STREAM_URL_BACKUP = "https://15723.live.streamtheworld.com/CHUMFMAAC_SC?dist=onlineradiobox"
MODEL_SIZE = "small"

# --- PATHS ---
# use ramdisk if available, otherwise just dump in /data
RAMDISK_PATH = "/mnt/ramdisk"
BASE_DIR     = (
    os.path.join(RAMDISK_PATH, "radiolistener")
    if os.path.exists(RAMDISK_PATH)
    else os.path.join(os.path.dirname(__file__), "data")
)

SEGMENT_DIR = os.path.join(BASE_DIR, "segments")
LOG_FILE    = os.path.join(os.path.dirname(__file__), "radio_transcript.txt")

os.makedirs(SEGMENT_DIR, exist_ok=True)
log.info(f"Segment dir: {SEGMENT_DIR}")

# --- HELPERS ---
def reload_keywords():
    # re-reads keywords.json from disk, called every 60s in the main loop
    # so a git pull on the server picks up changes without a restart
    global keywords
    try:
        keywords_path = os.path.join(os.path.dirname(__file__), "keywords.json")
        with open(keywords_path, "r") as f:
            keywords = json.load(f)
    except Exception as e:
        log.warning(f"Failed to reload keywords.json: {e} — keeping previous values.")

def is_contest_active():
    # weekdays 6am-8pm, weekends 1pm-6pm
    now  = datetime.now()
    day  = now.weekday()
    hour = now.hour
    return (6 <= hour < 20) if day < 5 else (13 <= hour < 18)

def send_email_blast(found_text):
    timestamp = time.strftime("%I:%M%p").lstrip("0")
    log.info(f"[!] KEYWORD DETECTED — sending alert: {found_text}")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(SENDER_EMAIL, APP_PASSWORD)
            for recipient in RECIPIENTS:
                msg = EmailMessage()
                msg.set_content(f"Radio Listener Alert at {timestamp}:\n\n\"{found_text}\"")
                msg["Subject"] = f"Radio Alert: {timestamp}"
                msg["From"]    = SENDER_EMAIL
                msg["To"]      = recipient
                server.send_message(msg)
        log.info(f"Emails sent to {len(RECIPIENTS)} recipients.")
    except Exception as e:
        log.error(f"Email error: {e}")

# --- HALLUCINATION FILTER ---
def is_hallucination(text):
    # whisper likes to make stuff up when there's no real speech
    # catch the obvious cases before wasting time on keyword checks
    words = text.lower().split()

    # too short to be a real announcement
    if len(words) < 4:
        return True

    # repeated trigrams = whisper looping on itself, e.g. "i can't win i can't win i can't win"
    if len(words) >= 9:
        trigrams = [" ".join(words[i:i+3]) for i in range(len(words) - 2)]
        for trigram in trigrams:
            if trigrams.count(trigram) >= 3:
                log.info(f"[HALLUCINATION] Repeated trigram — skipping.")
                return True

    return False

# --- KEYWORD DETECTION ---
def keyword_spotted(text_chunk):
    if is_hallucination(text_chunk):
        return False

    text_lower = text_chunk.lower()

    # exclusions first — if any bad word is in the chunk just kill it
    for bad_word in keywords.get("exclude_keywords", []):
        if bad_word.lower() in text_lower:
            log.info(f"[EXCLUDED] Matched '{bad_word}' — skipping.")
            return False

    # looks for 4+ single letters separated by hyphens or spaces
    # catches "w-i-l-d", "G L O W", "s e a s o n" etc.
    spelling_regex   = r"\b([a-z](?:[- ][a-z]){3,})\b"
    has_spelled_word = bool(re.search(spelling_regex, text_lower))

    # strict phrases are a guaranteed hit, no further checks needed
    for phrase in keywords.get("strict_keywords", []):
        if phrase.lower() in text_lower:
            log.info(f"[STRICT MATCH] '{phrase}'")
            return True

    SHORTCODES  = keywords.get("shortcodes", [])
    PRIZE_WORDS = keywords.get("prize_keywords", [])

    has_shortcode       = any(code in text_lower for code in SHORTCODES)
    has_prize_context   = any(word in text_lower for word in PRIZE_WORDS)
    has_keyword_mention = "keyword" in text_lower

    # dj spelling something out + any contest context = probably the keyword
    if has_spelled_word and (has_shortcode or has_keyword_mention or has_prize_context):
        log.info(f"[SPELLING MATCH] Spelled word with contest context.")
        return True

    # shortcode by itself isn't enough, need at least one other signal
    if has_shortcode:
        if has_prize_context or has_keyword_mention:
            log.info(f"[SHORTCODE MATCH] Shortcode with contest context.")
            return True
        return False

    # fallback during contest hours only
    if is_contest_active() and has_prize_context and has_keyword_mention:
        log.info(f"[PRIZE MATCH] Prize context during contest hours.")
        return True

    return False

# --- WAV VALIDATION ---
def is_valid_wav(filepath, min_bytes=8192, timeout=2.0):
    # runs in a thread so it doesn't block if ffmpeg is still writing the file
    if not os.path.exists(filepath):
        return False
    if os.path.getsize(filepath) < min_bytes:
        return False

    result = [False]

    def _check():
        try:
            with wave.open(filepath, "rb") as wf:
                result[0] = wf.getnframes() > 0
        except Exception:
            result[0] = False

    t = threading.Thread(target=_check, daemon=True)
    t.start()
    t.join(timeout)
    return result[0]

# --- FFMPEG ---
def kill_ffmpeg(proc):
    if proc is None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )
        else:
            proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass

def start_ffmpeg():
    log.info("Connecting to stream...")

    # clear out any leftover chunks from last run
    for f in glob.glob(os.path.join(SEGMENT_DIR, "*.wav")):
        try:
            os.remove(f)
        except OSError:
            pass

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-reconnect", "1",           # auto reconnect on drop
        "-reconnect_streamed", "1",  # reconnect mid-stream too
        "-reconnect_delay_max", "10",
        "-user_agent", "Mozilla/5.0",
        "-i", STREAM_URL,
        "-f", "segment",
        "-segment_time", "60",
        "-segment_format", "wav",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        os.path.join(SEGMENT_DIR, "chunk%03d.wav"),
    ]

    return subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )

# --- MAIN LOOP ---
MAX_STALL_SECONDS      = 180 #stall time 3x the chunk length
MAX_QUEUED_CHUNKS      = 5
KEYWORD_RELOAD_INTERVAL = 60  # how often to re-read keywords.json in seconds

def listen_and_spot():
    log.info(f"Radio Listener active (Whisper {MODEL_SIZE})")
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")

    ffmpeg_proc         = start_ffmpeg()
    last_segment_time   = time.time()
    last_keyword_reload = time.time()

    try:
        while True:
            # hot reload keywords so git pull takes effect without restart
            if time.time() - last_keyword_reload > KEYWORD_RELOAD_INTERVAL:
                reload_keywords()
                last_keyword_reload = time.time()

            # restart ffmpeg if it crashed or the stream went silent
            process_died   = ffmpeg_proc.poll() is not None
            stream_stalled = (time.time() - last_segment_time) > MAX_STALL_SECONDS

            if process_died or stream_stalled:
                reason = "crash" if process_died else "stall"
                log.warning(f"FFmpeg {reason} detected — restarting...")
                kill_ffmpeg(ffmpeg_proc)
                ffmpeg_proc       = start_ffmpeg()
                last_segment_time = time.time()
                time.sleep(3)
                continue

            files = sorted(glob.glob(os.path.join(SEGMENT_DIR, "*.wav")))

            # need at least 2 files so we're never touching the one ffmpeg is writing
            if len(files) <= 1:
                time.sleep(1)
                continue

            last_segment_time = time.time()

            # if we're falling behind just drop the old chunks and catch up
            if len(files) > MAX_QUEUED_CHUNKS:
                log.warning(f"Backlog of {len(files)} chunks — purging oldest to catch up.")
                for stale in files[: len(files) - MAX_QUEUED_CHUNKS]:
                    try:
                        os.remove(stale)
                    except OSError:
                        pass
                files = files[-MAX_QUEUED_CHUNKS:]

            target_file = files[0]

            try:
                if not is_valid_wav(target_file):
                    time.sleep(0.5)
                    continue

                segments, _ = model.transcribe(
                    target_file,
                    beam_size=1,
                    vad_filter=True,
                    condition_on_previous_text=False,
                    temperature=0.0,
                )
                full_text = " ".join(s.text.strip() for s in segments).strip()

                if full_text:
                    log.info(full_text)
                    with open(LOG_FILE, "a", encoding="utf-8") as lf:
                        lf.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {full_text}\n")

                    if keyword_spotted(full_text):
                        send_email_blast(full_text)

            except Exception as e:
                log.error(f"Transcription error: {e}")

            finally:
                try:
                    os.remove(target_file)
                except OSError:
                    pass

    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        kill_ffmpeg(ffmpeg_proc)

if __name__ == "__main__":
    listen_and_spot()