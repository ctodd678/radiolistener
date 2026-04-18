import subprocess
import os
import time
import glob
import wave
import re
from datetime import datetime, timedelta
import smtplib
import json
from email.message import EmailMessage
from faster_whisper import WhisperModel
import threading
import logging
import urllib.request

# logging to both console and file
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
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

SENDER_EMAIL   = config["sender_email"]
APP_PASSWORD   = config["app_password"]
RECIPIENTS     = config["recipients"]
OPENAI_KEY     = config.get("openai_api_key", "")
INSTANT_ALERTS = config.get("instant_alerts", True)
STATION_NAME   = config.get("station_name", "Radio Listener")
STREAM_URL     = config.get("stream_url", "")
MODEL_SIZE     = "small"

HEARTBEAT_HOURS       = config.get("heartbeat_hours", [12, 16])
CRASH_ALERT_THRESHOLD = config.get("crash_alert_threshold", 3)

# --- CONTEST HOURS (from config, with sensible defaults) ---
WEEKDAY_START  = config.get("weekday_start", 6)
WEEKDAY_END    = config.get("weekday_end", 20)
WEEKEND_START  = config.get("weekend_start", 13)
WEEKEND_END    = config.get("weekend_end", 18)
RUN_WEEKENDS   = config.get("run_weekends", True)

# midday summary fires halfway through the contest window
MIDDAY_HOUR = config.get("midday_hour", (WEEKDAY_START + WEEKDAY_END) // 2)

# --- TUNING ---
SEGMENT_TIME_SECONDS    = 30
MAX_STALL_SECONDS       = 45
STARTUP_GRACE_SECONDS   = 60
MAX_QUEUED_CHUNKS       = 8
KEYWORD_RELOAD_INTERVAL = 60
EMAIL_RETRIES           = 3
OVERLAP_WORD_COUNT      = 30

# --- PATHS ---
RAMDISK_PATH = "/mnt/ramdisk"
BASE_DIR = (
    os.path.join(RAMDISK_PATH, "radiolistener")
    if os.path.exists(RAMDISK_PATH)
    else os.path.join(os.path.dirname(__file__), "data")
)

SCRIPT_DIR    = os.path.dirname(__file__)
SEGMENT_DIR   = os.path.join(BASE_DIR, "segments")
LOG_FILE      = os.path.join(SCRIPT_DIR, "radio_transcript.txt")
APP_LOG       = os.path.join(SCRIPT_DIR, "radio_listener.log")
BATCH_FILE    = os.path.join(SCRIPT_DIR, "batch_detections.json")
SCHEDULE_FILE = os.path.join(SCRIPT_DIR, "keyword_schedule.json")
ARCHIVE_DIR   = os.path.join(SCRIPT_DIR, "archive")

os.makedirs(SEGMENT_DIR, exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)
log.info(f"Segment dir: {SEGMENT_DIR}")
log.info(f"Contest hours — weekdays {WEEKDAY_START}:00-{WEEKDAY_END}:00 | weekends {'disabled' if not RUN_WEEKENDS else f'{WEEKEND_START}:00-{WEEKEND_END}:00'}")

# --- BATCH STORAGE ---
batch_lock       = threading.Lock()
batch_sent_today = False

def load_batch():
    if os.path.exists(BATCH_FILE):
        try:
            with open(BATCH_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            today = time.strftime("%Y-%m-%d")
            todays = [d for d in data if d["timestamp"].startswith(today)]
            if len(todays) < len(data):
                log.info(f"Filtered out {len(data) - len(todays)} detection(s) from previous days.")
            return todays
        except Exception:
            pass
    return []

def save_batch(detections):
    try:
        with open(BATCH_FILE, "w", encoding="utf-8") as f:
            json.dump(detections, f, indent=2)
    except Exception as e:
        log.warning(f"Failed to save batch file: {e}")

def add_to_batch(text):
    if not is_contest_active():
        log.info(f"[BATCH] Outside contest hours — not batching.")
        return

    global batch_detections
    entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "text": text,
    }
    with batch_lock:
        batch_detections.append(entry)
        save_batch(batch_detections)
    log.info(f"[BATCH] Detection queued ({len(batch_detections)} total today).")

def clear_batch():
    global batch_detections
    with batch_lock:
        batch_detections = []
        save_batch(batch_detections)

batch_detections = load_batch()
log.info(f"Loaded {len(batch_detections)} existing detections from previous session.")

# --- KEYWORD SCHEDULE ---
def save_keyword_schedule(hour_to_keyword, schedule_hours, label):
    """
    Writes keyword_schedule.json after each batch extraction.
    The dashboard reads this file to display the hourly keyword grid.
    """
    slots = []
    for hour in schedule_hours:
        keyword = hour_to_keyword.get(hour, "unclear")
        slots.append({
            "hour":    hour,
            "label":   f"{hour % 12 or 12}:00{'AM' if hour < 12 else 'PM'}",
            "keyword": keyword,
        })

    data = {
        "date":       time.strftime("%Y-%m-%d"),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary":    label,
        "slots":      slots,
    }
    try:
        with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log.info(f"[SCHEDULE] keyword_schedule.json updated ({len(slots)} slots).")
    except Exception as e:
        log.warning(f"[SCHEDULE] Failed to write keyword_schedule.json: {e}")

# --- LOG ARCHIVING ---
def archive_daily_logs():
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        files = {
            LOG_FILE: os.path.join(ARCHIVE_DIR, f"radio_transcript_{yesterday}.txt"),
            APP_LOG:  os.path.join(ARCHIVE_DIR, f"radio_listener_{yesterday}.log"),
        }
        for src, dest in files.items():
            if os.path.exists(src) and os.path.getsize(src) > 0:
                os.replace(src, dest)
                log.info(f"Archived {src} -> {dest}")
        open(LOG_FILE, "w").close()
        open(APP_LOG, "w").close()
        # archive the schedule too
        schedule_dest = os.path.join(ARCHIVE_DIR, f"keyword_schedule_{yesterday}.json")
        if os.path.exists(SCHEDULE_FILE):
            os.replace(SCHEDULE_FILE, schedule_dest)
    except Exception as e:
        log.warning(f"Failed to archive logs: {e}")

# --- KEYWORD EXTRACTION ---
def extract_keywords_with_openai(detections):
    if not OPENAI_KEY:
        log.warning("No openai_api_key in config.json — skipping AI extraction.")
        return None

    raw_texts = "\n".join(
        f"{i+1}. [{d['timestamp']}] {d['text']}"
        for i, d in enumerate(detections)
    )

    prompt = (
        "You are extracting contest keywords from radio transcripts.\n\n"
        "Rules:\n"
        "1. Each item is a radio segment where a DJ announced a keyword contest word.\n"
        "2. For each item output ONLY the single contest keyword the DJ announced.\n"
        "3. The keyword is always a single common English word (e.g. schedule, sunshine, ocean).\n"
        "4. The DJ usually says it explicitly: your keyword is X or keyword to cash is X or spells it out.\n"
        "5. If you cannot find a clear keyword write unclear.\n"
        "6. Output EXACTLY one line per input item, in order. Do not skip any items.\n"
        "7. Output ONLY a numbered list. One word per line. No explanation. No punctuation after the word.\n\n"
        "Example output for 3 inputs:\n"
        "1. schedule\n"
        "2. unclear\n"
        "3. sunshine\n\n"
        "Transcripts:\n"
        f"{raw_texts}"
    )

    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 500,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENAI_KEY}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            text = result["choices"][0]["message"]["content"]
            log.info(f"[OPENAI] Extraction result:\n{text}")

            lines = text.strip().splitlines()
            numbered = [l for l in lines if re.match(r"^\d+\.", l.strip())]
            if len(numbered) >= max(1, len(detections) // 2):
                lines = numbered
            elif any(len(line.split()) > 5 for line in lines[:3]):
                log.warning("[OPENAI] Response looks malformed — skipping extraction.")
                return None

            return lines
    except Exception as e:
        log.error(f"OpenAI API error: {e} — skipping extraction.")
        return None

def extract_keywords_from_text(text):
    patterns = [
        r"keyword(?:\s+to\s+cash)?\s+is\s+(?:the\s+word\s+)?([a-z]+)",
        r"your\s+keyword\s+(?:right\s+now\s+)?is\s+([a-z]+)",
        r"text\s+(?:the\s+word\s+)?([a-z]+)\s+(?:and\s+your|plus\s+your)",
    ]
    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            word = match.group(1).strip()
            if len(word) > 2:
                return word
    return None

def get_schedule_hours():
    """returns the list of hours to show in the keyword schedule for today"""
    now     = datetime.now()
    weekday = now.weekday()
    if weekday < 5:
        return list(range(WEEKDAY_START, WEEKDAY_END))
    elif RUN_WEEKENDS:
        return list(range(WEEKEND_START, WEEKEND_END))
    return []

def send_batch_email(clear=True):
    global batch_sent_today

    with batch_lock:
        detections = list(batch_detections)

    # for midday cutoff — only include detections before now
    if not clear:
        cutoff_hour = datetime.now().hour
        detections = [
            d for d in detections
            if datetime.strptime(d["timestamp"], "%Y-%m-%d %H:%M:%S").hour < cutoff_hour
        ]

    if not detections:
        log.info("[BATCH] No detections, skipping summary email.")
        if clear:
            batch_sent_today = True
        return

    is_final      = clear
    summary_label = "End-of-Day" if is_final else "Midday"
    log.info(f"[BATCH] Sending {summary_label} summary with {len(detections)} detection(s).")

    extracted = extract_keywords_with_openai(detections)

    # build keyword schedule
    hour_to_keyword = {}
    if extracted is not None:
        for i, (d, line) in enumerate(zip(detections, extracted)):
            word = re.sub(r"^\d+\.\s*", "", line).strip().lower()
            if not word or len(word.split()) > 1:
                word = "unclear"
            try:
                hour = datetime.strptime(d["timestamp"], "%Y-%m-%d %H:%M:%S").hour
            except Exception:
                continue
            if hour not in hour_to_keyword or hour_to_keyword[hour] == "unclear":
                hour_to_keyword[hour] = word
    else:
        log.warning("[BATCH] AI extraction unavailable — attempting regex fallback.")
        for d in detections:
            word = extract_keywords_from_text(d["text"])
            if not word:
                continue
            try:
                hour = datetime.strptime(d["timestamp"], "%Y-%m-%d %H:%M:%S").hour
            except Exception:
                continue
            if hour not in hour_to_keyword or hour_to_keyword[hour] == "unclear":
                hour_to_keyword[hour] = word

    schedule_hours = get_schedule_hours()

    # write keyword_schedule.json so the dashboard can read it
    save_keyword_schedule(hour_to_keyword, schedule_hours, summary_label)

    schedule_lines = []
    found_keywords = []
    for hour in schedule_hours:
        label   = f"{hour % 12 or 12}:00{'AM' if hour < 12 else 'PM'}"
        keyword = hour_to_keyword.get(hour, "unclear")
        schedule_lines.append(f"  {label}: {keyword.upper() if keyword != 'unclear' else 'unclear'}")
        if keyword != "unclear":
            found_keywords.append(keyword.upper())

    schedule_section = "\n".join(schedule_lines)
    keywords_section = ", ".join(found_keywords) if found_keywords else "none detected"

    raw_section = "\n".join(
        f"  {i+1}. [{d['timestamp']}] {d['text']}"
        for i, d in enumerate(detections)
    )

    body = (
        f"{STATION_NAME} {summary_label} Summary\n"
        f"Date: {time.strftime('%Y-%m-%d')}\n"
        f"Total detections: {len(detections)}\n"
        f"Keywords found: {keywords_section}\n\n"
        f"--- KEYWORD SCHEDULE ---\n"
        f"{schedule_section}\n\n"
        f"--- RAW DETECTIONS ---\n"
        f"{raw_section}\n"
    )

    subject_label = "Midday Update" if not is_final else "Summary"
    for attempt in range(1, EMAIL_RETRIES + 1):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
                server.login(SENDER_EMAIL, APP_PASSWORD)
                for recipient in RECIPIENTS:
                    msg = EmailMessage()
                    msg.set_content(body)
                    msg["Subject"] = f"{STATION_NAME} {subject_label}: {time.strftime('%b %d %Y')} — {keywords_section}"
                    msg["From"]    = SENDER_EMAIL
                    msg["To"]      = recipient
                    server.send_message(msg)
            log.info(f"[BATCH] {summary_label} email sent. Keywords: {keywords_section}")
            if clear:
                clear_batch()
                batch_sent_today = True
            return
        except Exception as e:
            log.error(f"[BATCH] Email error (attempt {attempt}/{EMAIL_RETRIES}): {e}")
            if attempt < EMAIL_RETRIES:
                time.sleep(2)

    log.error("[BATCH] All batch email attempts failed.")

# --- BATCH SCHEDULER THREAD ---
def batch_scheduler():
    global batch_sent_today
    log.info("[BATCH SCHEDULER] Started.")

    midday_sent_today = False

    while True:
        now     = datetime.now()
        weekday = now.weekday()
        hour    = now.hour
        minute  = now.minute

        if hour == 0 and minute == 0:
            archive_daily_logs()
            batch_sent_today  = False
            midday_sent_today = False
            log.info("[BATCH SCHEDULER] Daily reset.")

        # midday summary — weekdays only, at MIDDAY_HOUR
        if weekday < 5 and not midday_sent_today:
            if hour == MIDDAY_HOUR and minute == 0:
                log.info(f"[BATCH SCHEDULER] Midday summary ({MIDDAY_HOUR}:00) — sending.")
                send_batch_email(clear=False)
                midday_sent_today = True

        # end of day summary
        if not batch_sent_today:
            end_hour = WEEKDAY_END if weekday < 5 else WEEKEND_END
            if hour == end_hour and minute == 0:
                log.info(f"[BATCH SCHEDULER] Contest window closed ({end_hour}:00) — sending final summary.")
                send_batch_email(clear=True)

        time.sleep(30)

# --- CRASH ALERT ---
def send_crash_alert(reason, restart_count):
    log.warning(f"[CRASH ALERT] Sending alert after {restart_count} restarts.")
    subject = f"{STATION_NAME} CRASH ALERT: {time.strftime('%I:%M%p').lstrip('0')}"
    body = (
        f"{STATION_NAME} has restarted {restart_count} times in a row.\n\n"
        f"Last reason: {reason}\n"
        f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"The app will keep trying to recover automatically. "
        f"Check the server if restarts continue."
    )
    for attempt in range(1, EMAIL_RETRIES + 1):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
                server.login(SENDER_EMAIL, APP_PASSWORD)
                for recipient in RECIPIENTS:
                    msg = EmailMessage()
                    msg.set_content(body)
                    msg["Subject"] = subject
                    msg["From"]    = SENDER_EMAIL
                    msg["To"]      = recipient
                    server.send_message(msg)
            log.info("[CRASH ALERT] Alert sent.")
            return
        except Exception as e:
            log.error(f"[CRASH ALERT] Email error (attempt {attempt}/{EMAIL_RETRIES}): {e}")
            if attempt < EMAIL_RETRIES:
                time.sleep(2)

# --- HEARTBEAT ---
def send_heartbeat():
    with batch_lock:
        detection_count = len(batch_detections)

    subject = f"{STATION_NAME} OK: {time.strftime('%I:%M%p').lstrip('0')}"
    body = (
        f"{STATION_NAME} is running normally.\n\n"
        f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Detections so far today: {detection_count}\n"
    )
    for attempt in range(1, EMAIL_RETRIES + 1):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
                server.login(SENDER_EMAIL, APP_PASSWORD)
                for recipient in RECIPIENTS:
                    msg = EmailMessage()
                    msg.set_content(body)
                    msg["Subject"] = subject
                    msg["From"]    = SENDER_EMAIL
                    msg["To"]      = recipient
                    server.send_message(msg)
            log.info(f"[HEARTBEAT] Status email sent ({detection_count} detections so far).")
            return
        except Exception as e:
            log.error(f"[HEARTBEAT] Email error (attempt {attempt}/{EMAIL_RETRIES}): {e}")
            if attempt < EMAIL_RETRIES:
                time.sleep(2)

# --- HEARTBEAT SCHEDULER THREAD ---
def heartbeat_scheduler():
    sent_hours = set()
    log.info(f"[HEARTBEAT SCHEDULER] Started. Will ping at hours: {HEARTBEAT_HOURS}")

    while True:
        now  = datetime.now()
        hour = now.hour

        if hour == 0 and now.minute == 0:
            sent_hours.clear()

        if hour in HEARTBEAT_HOURS and hour not in sent_hours:
            send_heartbeat()
            sent_hours.add(hour)

        time.sleep(30)

# --- HELPERS ---
def reload_keywords():
    global keywords
    try:
        keywords_path = os.path.join(os.path.dirname(__file__), "keywords.json")
        with open(keywords_path, "r") as f:
            keywords = json.load(f)
    except Exception as e:
        log.warning(f"Failed to reload keywords.json: {e} — keeping previous values.")

def is_contest_active():
    now     = datetime.now()
    day     = now.weekday()
    hour    = now.hour
    if day < 5:
        return WEEKDAY_START <= hour < WEEKDAY_END
    elif RUN_WEEKENDS:
        return WEEKEND_START <= hour < WEEKEND_END
    return False

# --- EMAIL ---
def send_email_blast(found_text):
    timestamp = time.strftime("%I:%M%p").lstrip("0")
    log.info(f"[!] KEYWORD DETECTED — sending alert: {found_text}")

    for attempt in range(1, EMAIL_RETRIES + 1):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
                server.login(SENDER_EMAIL, APP_PASSWORD)
                for recipient in RECIPIENTS:
                    msg = EmailMessage()
                    msg.set_content(f"{STATION_NAME} Alert at {timestamp}:\n\n\"{found_text}\"")
                    msg["Subject"] = f"{STATION_NAME} Alert: {timestamp}"
                    msg["From"]    = SENDER_EMAIL
                    msg["To"]      = recipient
                    server.send_message(msg)
            log.info(f"Emails sent to {len(RECIPIENTS)} recipients.")
            return
        except Exception as e:
            log.error(f"Email error (attempt {attempt}/{EMAIL_RETRIES}): {e}")
            if attempt < EMAIL_RETRIES:
                time.sleep(2)

    log.error("All email attempts failed.")

# --- HALLUCINATION FILTER ---
def is_hallucination(text):
    words = text.lower().split()

    if len(words) < 3:
        return True

    if len(words) >= 9:
        trigrams = [" ".join(words[i:i+3]) for i in range(len(words) - 2)]
        for trigram in trigrams:
            if trigrams.count(trigram) >= 3:
                log.info(f"[HALLUCINATION] Repeated trigram — skipping.")
                return True

    return False

# --- KEYWORD DETECTION ---
def keyword_spotted(text_chunk):
    text_lower = text_chunk.lower()

    # exclusions always win — checked first even before strict phrases
    for bad_word in keywords.get("exclude_keywords", []):
        if bad_word.lower() in text_lower:
            log.info(f"[EXCLUDED] Matched '{bad_word}' — skipping.")
            return False

    # strict phrases
    for phrase in keywords.get("strict_keywords", []):
        if phrase.lower() in text_lower:
            log.info(f"[STRICT MATCH] '{phrase}'")
            return True

    # spelling detector
    spelling_regex   = r"\b([a-z](?:[- ][a-z]){3,})\b"
    has_spelled_word = bool(re.search(spelling_regex, text_lower))

    SHORTCODES  = keywords.get("shortcodes", [])
    PRIZE_WORDS = keywords.get("prize_keywords", [])

    has_shortcode       = any(code in text_lower for code in SHORTCODES)
    has_prize_context   = any(word in text_lower for word in PRIZE_WORDS)
    has_keyword_mention = "keyword" in text_lower

    if has_spelled_word and (has_shortcode or has_keyword_mention or has_prize_context):
        log.info(f"[SPELLING MATCH] Spelled word with contest context.")
        return True

    if is_hallucination(text_chunk):
        return False

    if has_shortcode:
        if has_prize_context or has_keyword_mention:
            log.info(f"[SHORTCODE MATCH] Shortcode with contest context.")
            return True
        return False

    if is_contest_active() and has_prize_context and has_keyword_mention:
        if has_shortcode or has_spelled_word:
            log.info(f"[PRIZE MATCH] Prize context during contest hours.")
            return True

    return False

# --- WAV VALIDATION ---
def is_valid_wav(filepath, min_bytes=8192, timeout=2.0):
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
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass

def drain_stderr(proc, label="ffmpeg"):
    def _drain():
        try:
            for line in proc.stderr:
                msg = line.decode(errors="replace").strip()
                if msg:
                    log.debug(f"[{label}] {msg}")
        except Exception:
            pass

    t = threading.Thread(target=_drain, daemon=True, name=f"{label}-stderr")
    t.start()
    return t

def start_ffmpeg():
    log.info("Connecting to stream...")

    for f in glob.glob(os.path.join(SEGMENT_DIR, "*.wav")):
        try:
            os.remove(f)
        except OSError:
            pass

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
        "-live_start_index", "-3",
        "-user_agent", "Mozilla/5.0",
        "-multiple_requests", "1",
        "-seekable", "0",
        "-i", STREAM_URL,
        "-f", "segment",
        "-segment_time", str(SEGMENT_TIME_SECONDS),
        "-segment_format", "wav",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        os.path.join(SEGMENT_DIR, "chunk%03d.wav"),
    ]

    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )

    drain_stderr(proc, label="ffmpeg")
    return proc

# --- MAIN LOOP ---
def listen_and_spot():
    log.info(f"{STATION_NAME} active (Whisper {MODEL_SIZE})")
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")

    threading.Thread(target=batch_scheduler, daemon=True, name="batch-scheduler").start()
    threading.Thread(target=heartbeat_scheduler, daemon=True, name="heartbeat-scheduler").start()

    ffmpeg_proc          = start_ffmpeg()
    last_segment_time    = time.time()
    last_keyword_reload  = time.time()
    started_at           = time.time()
    previous_tail        = ""
    consecutive_restarts = 0

    try:
        while True:
            if time.time() - last_keyword_reload > KEYWORD_RELOAD_INTERVAL:
                reload_keywords()
                last_keyword_reload = time.time()

            process_died   = ffmpeg_proc.poll() is not None
            in_grace       = (time.time() - started_at) < STARTUP_GRACE_SECONDS
            stream_stalled = (
                not in_grace
                and (time.time() - last_segment_time) > MAX_STALL_SECONDS
            )

            if process_died or stream_stalled:
                reason = "crash" if process_died else "stall"
                consecutive_restarts += 1
                log.warning(f"FFmpeg {reason} detected (restart #{consecutive_restarts}) — restarting...")
                kill_ffmpeg(ffmpeg_proc)
                ffmpeg_proc       = start_ffmpeg()
                last_segment_time = time.time()
                started_at        = time.time()

                if consecutive_restarts >= CRASH_ALERT_THRESHOLD:
                    send_crash_alert(reason, consecutive_restarts)
                    consecutive_restarts = 0

                time.sleep(3)
                continue

            consecutive_restarts = 0

            files = sorted(glob.glob(os.path.join(SEGMENT_DIR, "*.wav")))

            if len(files) <= 1:
                time.sleep(1)
                continue

            last_segment_time = time.time()

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
                    vad_parameters=dict(
                        threshold=0.3,
                        min_speech_duration_ms=500,
                        min_silence_duration_ms=500,
                    ),
                    condition_on_previous_text=False,
                    temperature=0.0,
                )
                full_text = " ".join(s.text.strip() for s in segments).strip()

                if full_text:
                    log.info(full_text)
                    with open(LOG_FILE, "a", encoding="utf-8") as lf:
                        lf.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {full_text}\n")

                    combined_text = (previous_tail + " " + full_text).strip()

                    if keyword_spotted(combined_text):
                        if INSTANT_ALERTS:
                            send_email_blast(combined_text)
                        add_to_batch(combined_text)

                    words = full_text.split()
                    previous_tail = (
                        " ".join(words[-OVERLAP_WORD_COUNT:])
                        if len(words) > OVERLAP_WORD_COUNT
                        else full_text
                    )

            except Exception as e:
                log.error(f"Transcription error: {e}")

            finally:
                try:
                    os.remove(target_file)
                except OSError:
                    pass

    except KeyboardInterrupt:
        log.info("Shutting down...")
        if batch_detections:
            log.info("[BATCH] Unsent detections saved to batch_detections.json for next session.")
    finally:
        kill_ffmpeg(ffmpeg_proc)

if __name__ == "__main__":
    listen_and_spot()