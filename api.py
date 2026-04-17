"""
Radio Listener — per-container API for Radio Listener Dasboard
"""

import json
import os
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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


# --- routes ---

@app.get("/health")
def health():
    return {"ok": True}


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
    path = os.path.join(BASE, "keywords.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(body.data, f, indent=2)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status")
def get_status():
    # basic info about the app state
    today = datetime.now().strftime("%Y-%m-%d")
    detections = read_json(os.path.join(BASE, "batch_detections.json")) or []
    today_dets = [d for d in detections if d.get("timestamp", "").startswith(today)]

    # check if main.py process is running
    import subprocess
    try:
        result = subprocess.run(
            ["pgrep", "-f", "main.py"],
            capture_output=True, text=True
        )
        running = bool(result.stdout.strip())
    except Exception:
        running = False

    last = today_dets[-1]["timestamp"] if today_dets else None

    return {
        "detections_today": len(today_dets),
        "last_detection": last,
        "app_running": running,
        "date": today,
    }