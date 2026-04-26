# 📻 Radio Listener

Listens to a radio station 24/7, transcribes everything in real-time, and emails you when it hears a keyword. Built to run on a cheap N100 mini PC inside a Proxmox LXC container and basically forget about it.

## Dashboard

Check out the front-end for this project here! https://github.com/ctodd678/radiolistener-dashboard

## Demo

> *Running on an N100 Proxmox node in an LXC container. Draws less than 10 watts around the clock.*

<p align="center">
  <img src="https://github.com/user-attachments/assets/6f404bd5-0360-47cf-b9a4-53986dbdd29c" width="700" alt="radio_listener_demo">
</p>

## What it does

- Streams audio via FFmpeg and transcribes it in 30-second chunks using `faster-whisper`
- Detects keywords using a mix of strict phrase matching, spelling detection, and shortcode matching
- Sends instant email alerts on detection (optional, can be turned off)
- Sends a midday summary and end-of-day summary with a full keyword schedule extracted by GPT-4o-mini
- Archives logs daily so you only see today's output
- Hot-reloads `keywords.json` every 60 seconds so you can push changes without restarting
- Sends heartbeat emails and crash alerts so you know if something goes wrong

## Prerequisites

- Python 3.10+
- FFmpeg in your PATH
- Gmail account with 2FA and an App Password
- OpenAI API key (for batch summary extraction — costs basically nothing, under $0.01/day)

## Installation

```bash
git clone https://github.com/ctodd678/radiolistener.git
cd radiolistener
python3 -m venv venv
source venv/bin/activate  # Windows: .\venv\Scripts\activate
pip install faster-whisper
```

## Configuration

Copy `config.example.json` to `config.json` and fill it in. Make sure `config.json` is in your `.gitignore` — it has your credentials in it.

```json
{
  "sender_email": "your-bot@gmail.com",
  "app_password": "xxxx xxxx xxxx xxxx",
  "recipients": ["you@gmail.com", "friend@outlook.com"],
  "openai_api_key": "sk-...",
  "instant_alerts": false,
  "station_name": "CHUM 104.5",
  "stream_url": "https://playerservices.streamtheworld.com/api/livestream-redirect/CHUMFM_ADP.m3u8",
  "heartbeat_hours": [12, 16],
  "crash_alert_threshold": 3
}
```

**`openai_api_key`** — used for the batch summary emails. GPT-4o-mini extracts the actual keyword from each raw detection and builds a clean hourly schedule. At 1-2 requests per day this costs a fraction of a cent. Get a key at [platform.openai.com](https://platform.openai.com). If you leave this blank the batch emails will still send but the schedule will fall back to regex extraction.

**`instant_alerts`** — set to `true` if you want an email every single time a keyword is detected. `false` means you only get the midday and end-of-day summaries.

**`stream_url`** — any HLS stream URL works here. Swap this out to monitor a different station without touching anything else.

## Keywords

Edit `keywords.json` to configure what gets detected. The app hot-reloads this file every 60 seconds so changes take effect without a restart. If you're running on a server, just push to git and `git pull` on the box.

```json
{
  "shortcodes": ["104536", "104-536"],
  "strict_keywords": ["your keyword is", "keyword to cash is"],
  "prize_keywords": ["cash", "thousand", "jackpot"],
  "exclude_keywords": ["coming up", "still to come", "hear it, text it, win it"]
}
```

## Running

```bash
python main.py
```

To run it as a background service on Linux, set it up with systemd and a crontab to start/stop on a schedule. The app handles its own crash recovery internally — FFmpeg restarts automatically if the stream dies.

## Multiple stations

Each station gets its own LXC container with its own copy of the repo and its own `config.json`. Swap out `station_name`, `stream_url`, and `keywords.json` per container. The code is identical across all of them.
