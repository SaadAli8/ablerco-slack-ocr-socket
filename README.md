# Able RCO Agent — Slack OCR Bot (Socket Mode)

This is a **Slack Socket Mode** backend that:

- Listens for messages in **DMs** (and optionally channels)
- Detects **image uploads** (`image/*`)
- Downloads the file from Slack using the bot token
- Runs OCR locally using **Tesseract** (`pytesseract` + `Pillow`)
- Replies in a **thread** under the original message with extracted text

## Requirements

- Python **3.11+**
- A Slack app with **Socket Mode enabled**
- Tokens in `.env`:
  - `SLACK_BOT_TOKEN` (starts with `xoxb-`) with `chat:write`, `files:read`, `im:history` (and optionally `channels:history`)
  - `SLACK_APP_TOKEN` (starts with `xapp-`) with scope `connections:write`

## Local run (venv)

1. Create and activate a venv:

```bash
python -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Install Tesseract OCR:

- **Windows**: install from the Tesseract installer (and optionally set `TESSERACT_CMD` in `.env`).
- **Linux**: `sudo apt-get install tesseract-ocr`
- **macOS**: `brew install tesseract`

4. Create your `.env` file:

```bash
cp .env.example .env
```

Fill in `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`.

5. Run:

```bash
python app.py
```

## Docker run

1. Create `.env` from the example:

```bash
cp .env.example .env
```

2. Build + run:

```bash
docker compose up --build
```

The container installs `tesseract-ocr` and runs `python app.py`.

## How to test

- Open Slack
- DM the bot (Able RCO Agent)
- Upload a screenshot/image
- Expect:
  - First thread reply: “Got it — running OCR on your screenshot…”
  - Second thread reply: extracted text inside a code block (truncated to ~3500 chars)

## Notes / Settings

- `ALLOW_CHANNELS` (default `true`): if enabled, OCR also works in public/private channels where the bot is present
- `OCR_LANG` (default `eng`): language passed to Tesseract
- `TESSERACT_CMD` (optional): set this on Windows if `tesseract.exe` is not on PATH
