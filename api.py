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
def get_log(lines: int = 300):
    return {"log": read_tail(os.path.join(BASE, "radio_listener.log"), lines=lines)}


@app.get("/transcript")
def get_transcript(lines: int = 150):
    return {"transcript": read_tail(os.path.join(BASE, "radio_transcript.txt"), lines=lines)}


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


# --- config.json editor ---

# keys that are safe to expose and edit via the dashboard
SAFE_CONFIG_KEYS = [
    "station_name",
    "stream_url",
    "instant_alerts",
    "heartbeat_hours",
    "crash_alert_threshold",
    "sender_email",
    "recipients",
]


@app.get("/config")
def get_config():
    data = read_json(os.path.join(BASE, "config.json"))
    if data is None:
        raise HTTPException(status_code=404, detail="config.json not found")
    # strip sensitive keys before sending
    return {k: v for k, v in data.items() if k in SAFE_CONFIG_KEYS}


class ConfigBody(BaseModel):
    data: dict


@app.post("/config")
def set_config(body: ConfigBody):
    path = os.path.join(BASE, "config.json")
    existing = read_json(path)
    if existing is None:
        raise HTTPException(status_code=404, detail="config.json not found")
    # only update safe keys, preserve everything else (passwords, api keys etc)
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
    """
    Runs the same detection logic as main.py keyword_spotted()
    and returns a detailed breakdown of what fired and what didn't.
    """
    text_lower = text.lower()
    results = []

    # 1. check exclusions first
    for phrase in keywords.get("exclude_keywords", []):
        if phrase.lower() in text_lower:
            results.append({
                "rule":    "exclusion",
                "match":   phrase,
                "fired":   True,
                "verdict": "EXCLUDED",
            })
            return {
                "detected": False,
                "verdict":  "EXCLUDED",
                "reason":   f"matched exclusion: \"{phrase}\"",
                "results":  results,
            }

    # 2. strict phrases
    for phrase in keywords.get("strict_keywords", []):
        if phrase.lower() in text_lower:
            results.append({
                "rule":    "strict_phrase",
                "match":   phrase,
                "fired":   True,
                "verdict": "MATCH",
            })
            return {
                "detected": True,
                "verdict":  "DETECTED",
                "reason":   f"strict phrase: \"{phrase}\"",
                "results":  results,
            }
        else:
            results.append({"rule": "strict_phrase", "match": phrase, "fired": False})

    # 3. spelling detector
    spelling_regex = r"\b([a-z](?:[- ][a-z]){3,})\b"
    spelled = re.findall(spelling_regex, text_lower)
    has_spelled = bool(spelled)

    shortcodes   = keywords.get("shortcodes", [])
    prize_words  = keywords.get("prize_keywords", [])
    has_shortcode = any(c in text_lower for c in shortcodes)
    has_prize     = any(w in text_lower for w in prize_words)
    has_keyword   = "keyword" in text_lower

    if has_spelled and (has_shortcode or has_keyword or has_prize):
        results.append({
            "rule":    "spelling_detector",
            "match":   ", ".join(spelled),
            "fired":   True,
            "verdict": "MATCH",
        })
        return {
            "detected": True,
            "verdict":  "DETECTED",
            "reason":   f"spelled word(s) \"{', '.join(spelled)}\" with contest context",
            "results":  results,
        }
    elif has_spelled:
        results.append({
            "rule":    "spelling_detector",
            "match":   ", ".join(spelled),
            "fired":   False,
            "verdict": "no contest context alongside spelling",
        })

    # 4. shortcode match
    matched_codes = [c for c in shortcodes if c in text_lower]
    if matched_codes and (has_prize or has_keyword):
        results.append({
            "rule":    "shortcode",
            "match":   ", ".join(matched_codes),
            "fired":   True,
            "verdict": "MATCH",
        })
        return {
            "detected": True,
            "verdict":  "DETECTED",
            "reason":   f"shortcode \"{matched_codes[0]}\" with contest context",
            "results":  results,
        }
    elif matched_codes:
        results.append({
            "rule":    "shortcode",
            "match":   ", ".join(matched_codes),
            "fired":   False,
            "verdict": "shortcode found but no prize/keyword context",
        })

    # 5. prize fallback
    if has_prize and has_keyword and (has_shortcode or has_spelled):
        results.append({
            "rule":    "prize_fallback",
            "match":   "prize + keyword + shortcode/spelling",
            "fired":   True,
            "verdict": "MATCH",
        })
        return {
            "detected": True,
            "verdict":  "DETECTED",
            "reason":   "prize context fallback",
            "results":  results,
        }

    results.append({"rule": "prize_fallback", "fired": False})

    return {
        "detected": False,
        "verdict":  "NO MATCH",
        "reason":   "no rules fired",
        "results":  results,
    }


@app.post("/test")
def test_detection(body: TestBody):
    return run_keyword_test(body.text, body.keywords)