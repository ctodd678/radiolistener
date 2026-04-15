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
GEMINI_KEY   = config.get("gemini_api_key", "")

STREAM_URL = "https://playerservices.streamtheworld.com/api/livestream-redirect/CHUMFM_ADP.m3u8"
MODEL_SIZE = "small"

# heartbeat hours (24h) and crash alert threshold pulled from config
HEARTBEAT_HOURS       = config.get("heartbeat_hours", [12, 16])
CRASH_ALERT_THRESHOLD = config.get("crash_alert_threshold", 3)

# --- TUNING ---
SEGMENT_TIME_SECONDS    = 30
MAX_STALL_SECONDS       = 45
STARTUP_GRACE_SECONDS   = 60
MAX_QUEUED_CHUNKS       = 8
KEYWORD_RELOAD_INTERVAL = 60
EMAIL_RETRIES           = 3
OVERLAP_WORD_COUNT      = 30

# --- PATHS ---
# use ramdisk if available, otherwise just dump in /data
RAMDISK_PATH = "/mnt/ramdisk"
BASE_DIR = (
    os.path.join(RAMDISK_PATH, "radiolistener")
    if os.path.exists(RAMDISK_PATH)
    else os.path.join(os.path.dirname(__file__), "data")
)

SEGMENT_DIR = os.path.join(BASE_DIR, "segments")
LOG_FILE    = os.path.join(os.path.dirname(__file__), "radio_transcript.txt")
BATCH_FILE  = os.path.join(os.path.dirname(__file__), "batch_detections.json")

os.makedirs(SEGMENT_DIR, exist_ok=True)
log.info(f"Segment dir: {SEGMENT_DIR}")

# --- BATCH STORAGE ---
batch_lock       = threading.Lock()
batch_sent_today = False

def load_batch():
    # load persisted detections from previous runs, but only keep today's
    # so we don't accidentally email old ones
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
    # only queue detections during contest hours — no point keeping overnight noise
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

# --- GEMINI KEYWORD EXTRACTION ---
def extract_keywords_with_gemini(detections):
    # ask gemini to pull just the contest word out of each raw detection
    # falls back to raw texts if the api call fails or looks malformed
    if not GEMINI_KEY:
        log.warning("No Gemini API key in config.json — skipping AI extraction.")
        return [d["text"] for d in detections]

    raw_texts = "\n".join(
        f"{i+1}. [{d['timestamp']}] {d['text']}"
        for i, d in enumerate(detections)
    )

    prompt = (
        "You are extracting contest keywords from radio transcripts.\n\n"
        "Rules:\n"
        "1. Each item is a radio segment where a DJ announced a keyword to cash contest word.\n"
        "2. For each item output ONLY the single contest keyword the DJ announced.\n"
        "3. The keyword is always a single common English word (e.g. schedule, sunshine, ocean).\n"
        "4. The DJ usually says it explicitly: your keyword is X or keyword to cash is X or spells it out.\n"
        "5. If you cannot find a clear keyword write unclear.\n"
        "6. Output ONLY a numbered list. One word per line. No explanation. No punctuation after the word.\n\n"
        "Example output:\n"
        "1. schedule\n"
        "2. sunshine\n"
        "3. unclear\n\n"
        "Transcripts:\n"
        f"{raw_texts}"
    )

    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}]
    }).encode("utf-8")

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={GEMINI_KEY}"
    )

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            text = result["candidates"][0]["content"]["parts"][0]["text"]
            log.info(f"[GEMINI] Extraction result:\n{text}")

            lines = text.strip().splitlines()

            # sanity check — if first few lines are very long, gemini returned
            # something unexpected (full sentences instead of single words)
            if any(len(line.split()) > 5 for line in lines[:3]):
                log.warning("[GEMINI] Response looks malformed — falling back to raw detections.")
                return [d["text"] for d in detections]

            return lines
    except Exception as e:
        log.error(f"Gemini API error: {e} — falling back to raw detections.")
        return [d["text"] for d in detections]

def send_batch_email(clear=True):
    global batch_sent_today

    with batch_lock:
        detections = list(batch_detections)

    if not detections:
        log.info("[BATCH] No detections, skipping summary email.")
        if clear:
            batch_sent_today = True
        return

    is_final      = clear
    summary_label = "End-of-Day" if is_final else "Midday"
    log.info(f"[BATCH] Sending {summary_label} summary with {len(detections)} detection(s).")

    extracted = extract_keywords_with_gemini(detections)

    # map each detection to its hour and extracted keyword
    hour_to_keyword = {}
    for i, (d, line) in enumerate(zip(detections, extracted)):
        word = re.sub(r"^\d+\.\s*", "", line).strip().lower()

        # if gemini returned more than one word it didn't extract properly
        if not word or len(word.split()) > 1:
            word = "unclear"

        try:
            hour = datetime.strptime(d["timestamp"], "%Y-%m-%d %H:%M:%S").hour
        except Exception:
            continue

        # only keep real keywords, don't overwrite a good one with unclear
        if hour not in hour_to_keyword or hour_to_keyword[hour] == "unclear":
            hour_to_keyword[hour] = word

    now     = datetime.now()
    weekday = now.weekday()
    if weekday < 5:
        schedule_hours = list(range(6, 20))   # weekdays 6am to 7pm = 14 slots
    else:
        schedule_hours = list(range(13, 18))  # weekends 1pm to 5pm = 5 slots

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
        f"Radio Listener {summary_label} Summary\n"
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
                    msg["Subject"] = f"Radio Listener {subject_label}: {time.strftime('%b %d %Y')} — {keywords_section}"
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
    # midday summary at 1pm on weekdays (doesn't clear batch)
    # end of day summary at 8pm weekdays / 6pm weekends (clears batch)
    # resets at midnight
    global batch_sent_today
    log.info("[BATCH SCHEDULER] Started.")

    midday_sent_today = False

    while True:
        now     = datetime.now()
        weekday = now.weekday()
        hour    = now.hour
        minute  = now.minute

        if hour == 0 and minute == 0:
            batch_sent_today  = False
            midday_sent_today = False
            log.info("[BATCH SCHEDULER] Daily reset.")

        # midday summary at 1pm on weekdays only
        if weekday < 5 and not midday_sent_today:
            if hour == 13 and minute == 0:
                log.info("[BATCH SCHEDULER] Midday summary (1:00PM) — sending.")
                send_batch_email(clear=False)
                midday_sent_today = True

        # end of day summary
        if not batch_sent_today:
            end_hour = 20 if weekday < 5 else 18
            if hour == end_hour and minute == 0:
                log.info(f"[BATCH SCHEDULER] Contest window closed ({end_hour}:00) — sending final summary.")
                send_batch_email(clear=True)

        time.sleep(30)

# --- CRASH ALERT ---
def send_crash_alert(reason, restart_count):
    # fires when ffmpeg has restarted too many times in a row
    log.warning(f"[CRASH ALERT] Sending alert after {restart_count} restarts.")
    subject = f"Radio Listener CRASH ALERT: {time.strftime('%I:%M%p').lstrip('0')}"
    body = (
        f"Radio Listener has restarted {restart_count} times in a row.\n\n"
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
    # quick status ping so I know the app is alive
    with batch_lock:
        detection_count = len(batch_detections)

    subject = f"Radio Listener OK: {time.strftime('%I:%M%p').lstrip('0')}"
    body = (
        f"Radio Listener is running normally.\n\n"
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
    # sends a status ping at each hour in HEARTBEAT_HOURS, once per day each
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
                    msg.set_content(f"Radio Listener Alert at {timestamp}:\n\n\"{found_text}\"")
                    msg["Subject"] = f"Radio Alert: {timestamp}"
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
    # whisper likes to make stuff up when there's no real speech
    # catch the obvious cases before wasting time on keyword checks
    words = text.lower().split()

    # too short to be real
    if len(words) < 3:
        return True

    # repeated trigrams = whisper looping on itself
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

    # strict phrases fire immediately — high confidence, exclusions don't apply
    for phrase in keywords.get("strict_keywords", []):
        if phrase.lower() in text_lower:
            log.info(f"[STRICT MATCH] '{phrase}'")
            return True

    # spelling detector — also high confidence, checked before exclusions
    # catches "w-i-l-d", "G L O W", "s e a s o n" etc.
    spelling_regex   = r"\b([a-z](?:[- ][a-z]){3,})\b"
    has_spelled_word = bool(re.search(spelling_regex, text_lower))

    SHORTCODES  = keywords.get("shortcodes", [])
    PRIZE_WORDS = keywords.get("prize_keywords", [])

    has_shortcode       = any(code in text_lower for code in SHORTCODES)
    has_prize_context   = any(word in text_lower for word in PRIZE_WORDS)
    has_keyword_mention = "keyword" in text_lower

    # dj spelling something out + any contest context = probably the keyword
    if has_spelled_word and (has_shortcode or has_keyword_mention or has_prize_context):
        log.info(f"[SPELLING MATCH] Spelled word with contest context.")
        return True

    # apply exclusions for softer matches only
    for bad_word in keywords.get("exclude_keywords", []):
        if bad_word.lower() in text_lower:
            log.info(f"[EXCLUDED] Matched '{bad_word}' — skipping.")
            return False

    if is_hallucination(text_chunk):
        return False

    # shortcode by itself isn't enough, need at least one other signal
    if has_shortcode:
        if has_prize_context or has_keyword_mention:
            log.info(f"[SHORTCODE MATCH] Shortcode with contest context.")
            return True
        return False

    # prize match fallback — tightened to require shortcode OR spelling
    # plain mentions of "$80,000" and "keyword" in a promo jingle won't fire this
    if is_contest_active() and has_prize_context and has_keyword_mention:
        if has_shortcode or has_spelled_word:
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

def drain_stderr(proc, label="ffmpeg"):
    # drain stderr in background so the pipe buffer never fills and blocks ffmpeg
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

    # clear out any leftover chunks from last run
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
    log.info(f"Radio Listener active (Whisper {MODEL_SIZE})")
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")

    # start background threads
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
            # hot reload keywords so git pull takes effect without restart
            if time.time() - last_keyword_reload > KEYWORD_RELOAD_INTERVAL:
                reload_keywords()
                last_keyword_reload = time.time()

            # restart ffmpeg if it crashed or the stream went silent
            # grace period suppresses false stall detections right after a (re)start
            process_died   = ffmpeg_proc.poll() is not None
            in_grace       = (time.time() - started_at) < STARTUP_GRACE_SECONDS
            stream_stalled = (
                not in_grace
                and (time.time() - last_segment_time) > MAX_STALL_SECONDS
            )

            if process_died or stream_stalled:
                reason = "crash" if process_died else "stall"
                consecutive_restarts += 1
                log.warning(
                    f"FFmpeg {reason} detected (restart #{consecutive_restarts}) — restarting..."
                )
                kill_ffmpeg(ffmpeg_proc)
                ffmpeg_proc       = start_ffmpeg()
                last_segment_time = time.time()
                started_at        = time.time()

                # send crash alert if it keeps happening
                if consecutive_restarts >= CRASH_ALERT_THRESHOLD:
                    send_crash_alert(reason, consecutive_restarts)
                    consecutive_restarts = 0  # reset after alerting so we don't spam

                time.sleep(3)
                continue

            # reset restart counter on any successful segment processing
            consecutive_restarts = 0

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

                    # prepend tail of previous chunk so keywords split across
                    # chunk boundaries don't get missed
                    combined_text = (previous_tail + " " + full_text).strip()

                    if keyword_spotted(combined_text):
                        send_email_blast(combined_text)
                        add_to_batch(combined_text)

                    # save last N words for next chunk
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