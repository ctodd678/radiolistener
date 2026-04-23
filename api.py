"""
Radio Listener — per-container API
Runs inside each LXC container on port 5001.
"""

import json
import os
import re
import subprocess
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE = "/root/radiolistener"


def read_tail(path, lines=300):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except Exception:
        return ""


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def is_service_active(name):
    try:
        r = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=3
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


# safe config keys that can be viewed/edited via the dashboard
SAFE_CONFIG_KEYS = [
    "station_name",
    "stream_url",
    "instant_alerts",
    "heartbeat_hours",
    "crash_alert_threshold",
    "sender_email",
    "recipients",
    "weekday_start",
    "weekday_end",
    "weekend_start",
    "weekend_end",
    "run_weekends",
    "midday_hour",
    # whisper / vad settings
    "model_size",
    "vad_threshold",
    "vad_min_speech_ms",
    "vad_min_silence_ms",
    "whisper_beam_size",
    "whisper_temperature",
]


# --- routes ---

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/status")
def get_status():
    today = datetime.now().strftime("%Y-%m-%d")
    detections = read_json(os.path.join(BASE, "batch_detections.json")) or []
    today_dets = [d for d in detections if d.get("timestamp", "").startswith(today)]
    last = today_dets[-1]["timestamp"] if today_dets else None

    return {
        "detections_today":   len(today_dets),
        "last_detection":     last,
        "radioscout_running": is_service_active("radioscout.service"),
        "rlapi_running":      is_service_active("rlapi.service"),
        "date":               today,
    }


@app.get("/detections")
def get_detections():
    data = read_json(os.path.join(BASE, "batch_detections.json")) or []
    today = datetime.now().strftime("%Y-%m-%d")
    return [d for d in data if d.get("timestamp", "").startswith(today)]


@app.get("/log")
def get_log(lines: int = 2000):
    return {"log": read_tail(os.path.join(BASE, "radio_listener.log"), lines=lines)}


@app.get("/transcript")
def get_transcript(lines: int = 2000):
    return {"transcript": read_tail(os.path.join(BASE, "radio_transcript.txt"), lines=lines)}


# --- keyword schedule ---

@app.get("/schedule")
def get_schedule():
    data = read_json(os.path.join(BASE, "keyword_schedule.json"))
    if data is None:
        # return empty structure if no schedule has been generated yet today
        cfg = read_json(os.path.join(BASE, "config.json")) or {}
        weekday = datetime.now().weekday()
        run_weekends = cfg.get("run_weekends", True)

        if weekday < 5:
            start = cfg.get("weekday_start", 6)
            end   = cfg.get("weekday_end", 20)
        elif run_weekends:
            start = cfg.get("weekend_start", 13)
            end   = cfg.get("weekend_end", 18)
        else:
            return {"date": datetime.now().strftime("%Y-%m-%d"), "updated_at": None, "summary": None, "slots": []}

        slots = [
            {
                "hour":    h,
                "label":   f"{h % 12 or 12}:00{'AM' if h < 12 else 'PM'}",
                "keyword": "unclear",
            }
            for h in range(start, end)
        ]
        return {
            "date":       datetime.now().strftime("%Y-%m-%d"),
            "updated_at": None,
            "summary":    None,
            "slots":      slots,
        }
    return data


# --- keywords ---

@app.get("/keywords")
def get_keywords():
    data = read_json(os.path.join(BASE, "keywords.json"))
    if data is None:
        raise HTTPException(status_code=404, detail="keywords.json not found")
    return data


class KeywordsBody(BaseModel):
    data: dict


@app.post("/keywords")
def set_keywords(body: KeywordsBody):
    try:
        write_json(os.path.join(BASE, "keywords.json"), body.data)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- config ---

@app.get("/config")
def get_config():
    data = read_json(os.path.join(BASE, "config.json"))
    if data is None:
        raise HTTPException(status_code=404, detail="config.json not found")
    return {k: v for k, v in data.items() if k in SAFE_CONFIG_KEYS}


class ConfigBody(BaseModel):
    data: dict


@app.post("/config")
def set_config(body: ConfigBody):
    path = os.path.join(BASE, "config.json")
    existing = read_json(path)
    if existing is None:
        raise HTTPException(status_code=404, detail="config.json not found")
    for k, v in body.data.items():
        if k in SAFE_CONFIG_KEYS:
            existing[k] = v
    try:
        write_json(path, existing)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- keyword tester ---

class TestBody(BaseModel):
    text: str
    keywords: dict


def run_keyword_test(text: str, keywords: dict):
    text_lower = text.lower()
    results = []

    # exclusions first
    for phrase in keywords.get("exclude_keywords", []):
        if phrase.lower() in text_lower:
            results.append({"rule": "exclusion", "match": phrase, "fired": True, "verdict": "EXCLUDED"})
            return {"detected": False, "verdict": "EXCLUDED", "reason": f"matched exclusion: \"{phrase}\"", "results": results}

    # strict phrases
    for phrase in keywords.get("strict_keywords", []):
        if phrase.lower() in text_lower:
            results.append({"rule": "strict_phrase", "match": phrase, "fired": True, "verdict": "MATCH"})
            return {"detected": True, "verdict": "DETECTED", "reason": f"strict phrase: \"{phrase}\"", "results": results}
        else:
            results.append({"rule": "strict_phrase", "match": phrase, "fired": False})

    # spelling detector
    spelling_regex = r"\b([a-z](?:[- ][a-z]){3,})\b"
    spelled = re.findall(spelling_regex, text_lower)
    has_spelled   = bool(spelled)
    has_shortcode = any(c in text_lower for c in keywords.get("shortcodes", []))
    has_prize     = any(w in text_lower for w in keywords.get("prize_keywords", []))
    has_keyword   = "keyword" in text_lower

    if has_spelled and (has_shortcode or has_keyword or has_prize):
        results.append({"rule": "spelling_detector", "match": ", ".join(spelled), "fired": True, "verdict": "MATCH"})
        return {"detected": True, "verdict": "DETECTED", "reason": f"spelled word(s) with contest context", "results": results}
    elif has_spelled:
        results.append({"rule": "spelling_detector", "match": ", ".join(spelled), "fired": False, "verdict": "no contest context alongside spelling"})

    # shortcode
    matched_codes = [c for c in keywords.get("shortcodes", []) if c in text_lower]
    if matched_codes and (has_prize or has_keyword):
        results.append({"rule": "shortcode", "match": ", ".join(matched_codes), "fired": True, "verdict": "MATCH"})
        return {"detected": True, "verdict": "DETECTED", "reason": f"shortcode with contest context", "results": results}
    elif matched_codes:
        results.append({"rule": "shortcode", "match": ", ".join(matched_codes), "fired": False, "verdict": "shortcode found but no prize/keyword context"})

    # prize fallback
    if has_prize and has_keyword and (has_shortcode or has_spelled):
        results.append({"rule": "prize_fallback", "match": "prize + keyword + shortcode/spelling", "fired": True, "verdict": "MATCH"})
        return {"detected": True, "verdict": "DETECTED", "reason": "prize context fallback", "results": results}

    results.append({"rule": "prize_fallback", "fired": False})
    return {"detected": False, "verdict": "NO MATCH", "reason": "no rules fired", "results": results}


@app.post("/test")
def test_detection(body: TestBody):
    return run_keyword_test(body.text, body.keywords)


# --- archive ---

@app.get("/archive/list")
def list_archive():
    """returns a list of archived dates that have files"""
    archive_dir = os.path.join(BASE, "archive")
    if not os.path.exists(archive_dir):
        return []

    dates = set()
    for fname in os.listdir(archive_dir):
        # extract date from filenames like radio_transcript_2026-04-16.txt
        parts = fname.rsplit("_", 1)
        if len(parts) == 2:
            date_part = parts[1].split(".")[0]
            if len(date_part) == 10 and date_part[4] == "-":
                dates.add(date_part)

    return sorted(dates, reverse=True)


@app.get("/archive/{date}/transcript")
def get_archive_transcript(date: str):
    path = os.path.join(BASE, "archive", f"radio_transcript_{date}.txt")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Not found")
    return {"transcript": read_tail(path, lines=99999), "date": date}


@app.get("/archive/{date}/log")
def get_archive_log(date: str):
    path = os.path.join(BASE, "archive", f"radio_listener_{date}.log")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Not found")
    return {"log": read_tail(path, lines=99999), "date": date}


@app.get("/archive/{date}/detections")
def get_archive_detections(date: str):
    path = os.path.join(BASE, "archive", f"batch_detections_{date}.json")
    if not os.path.exists(path):
        # fall back to main batch file filtered by date
        data = read_json(os.path.join(BASE, "batch_detections.json")) or []
        return [d for d in data if d.get("timestamp", "").startswith(date)]
    return read_json(path) or []


@app.get("/archive/{date}/schedule")
def get_archive_schedule(date: str):
    path = os.path.join(BASE, "archive", f"keyword_schedule_{date}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Not found")
    return read_json(path) or {}


# --- virgin radio auto-submitter ---

class VirginSubmitBody(BaseModel):
    keyword: str | None = None
    force: bool = False
    date: str | None = None   # YYYY-MM-DD; None = today's keyword_schedule.json


@app.post("/virgin/submit")
def virgin_submit(body: VirginSubmitBody = VirginSubmitBody()):
    node = "/usr/bin/node"
    script = os.path.join(BASE, "virgin_submit.js")
    config = os.path.join(BASE, "config.json")

    if not os.path.exists(script):
        raise HTTPException(status_code=404, detail="virgin_submit.js not found on this container")
    if not os.path.exists(node):
        node = "node"

    cmd = [node, script, "--config", config]

    if body.keyword:
        # specific keyword — no schedule needed
        cmd += ["--keyword", body.keyword]
    else:
        # resolve which keyword_schedule to use
        today = datetime.now().strftime("%Y-%m-%d")
        if body.date and body.date != today:
            schedule_path = os.path.join(BASE, "archive", f"keyword_schedule_{body.date}.json")
        else:
            schedule_path = os.path.join(BASE, "keyword_schedule.json")

        if not os.path.exists(schedule_path):
            # fall back to root keyword_schedule.json
            fallback = os.path.join(BASE, "keyword_schedule.json")
            if os.path.exists(fallback):
                schedule_path = fallback
            else:
                # fall back to most recent archived schedule
                archive_dir = os.path.join(BASE, "archive")
                candidates = sorted([
                    f for f in os.listdir(archive_dir)
                    if f.startswith("keyword_schedule_") and f.endswith(".json")
                ], reverse=True) if os.path.exists(archive_dir) else []
                if candidates:
                    schedule_path = os.path.join(archive_dir, candidates[0])
                else:
                    raise HTTPException(status_code=404, detail=f"No keyword schedule found for {body.date or today}. Submit a specific keyword instead.")

        cmd += ["--schedule", schedule_path]
    if body.force:
        cmd += ["--force"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=BASE,
        )
        output = result.stdout + result.stderr
        success = result.returncode == 0 and "SUCCESS" in output
        return {"ok": success, "output": output, "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="virgin_submit.js timed out after 120s")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/virgin/status")
def virgin_status():
    log_path = os.path.join(BASE, "virgin_submissions.json")
    today = datetime.now().strftime("%Y-%m-%d")
    data = read_json(log_path) or {}
    raw = data.get(today, [])

    # parse "email:KEYWORD" entries into per-profile groups
    by_profile = {}
    for entry in raw:
        if ":" in entry:
            email, kw = entry.split(":", 1)
            by_profile.setdefault(email, []).append(kw)
        else:
            by_profile.setdefault("legacy", []).append(entry)

    return {
        "today": today,
        "submitted_today": raw,
        "by_profile": by_profile,
        "all": data,
    }