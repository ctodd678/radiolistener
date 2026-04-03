# 📻 Radio Listener: AI Keyword Monitor

An AI-powered radio monitoring service built to run on an Intel N100 Proxmox node. It listens to the desired station live stream 24/7, transcribes audio in real-time using Whisper AI, and sends email alerts to a distribution list when keywords are detected.

## 🎬 Demo

> *Receipt to grocery data JSON. Back-end AI model running locally being called by flutter application via Android Emulator.*

<p align="center">
  <img src="https://github.com/user-attachments/assets/6f404bd5-0360-47cf-b9a4-53986dbdd29c" width="350" alt="fridgewise_demo_flutter">
</p>

## 🚀 Features

* **AI Transcription**: Uses `faster-whisper` (Base model) optimized for CPU execution with INT8 quantization.
* **Real-time Monitoring**: Captures 10-second audio chunks via FFmpeg with browser-spoofing headers to bypass stream blocks.
* **Smart Alerts**: Sends multi-recipient emails with time-aware subjects like `🚨 4:00PM Keyword Alert`.
* **Persistent Logging**: Maintains a local `radio_transcript.txt` for auditing and historical review.
* **Resource Efficient**: Designed for LXC containers with minimal RAM and CPU overhead.

## 📋 Prerequisites

* Python 3.10 or higher
* FFmpeg installed and in your system PATH
* A Gmail account with 2-Factor Authentication and an **App Password**

## 🔧 Installation

1.  **Clone the repository**
    ```bash
    git clone [https://github.com/your-username/radiolistener.git](https://github.com/your-username/radiolistener.git)
    cd radiolistener
    ```

2.  **Set up the Virtual Environment**
    ```bash
    python3 -m venv venv
    # Windows
    .\venv\Scripts\activate
    # Linux/LXC
    source venv/bin/activate
    ```

3.  **Install Dependencies**
    ```bash
    pip install faster-whisper requests
    ```

## ⚙️ Configuration

Create a `config.json` file in the root directory. **Ensure this file is added to your .gitignore to protect your credentials.**

```json
{
    "sender_email": "your-scout-bot@gmail.com",
    "app_password": "xxxx xxxx xxxx xxxx",
    "recipients": [
        "connor.example@gmail.com",
        "friend.name@outlook.com"
    ],
    "keywords": [
        "cash", "keyword", "money", "win", "entry", "dollar", "thousand", "80", "jackpot"
    ]
}
