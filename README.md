# Squidmin

CRM for managing e-sport training subscriptions. Tracks client payments, subscription periods, and sends alerts when subscriptions are about to expire.

## Features

- Client and payment management with subscription status tracking
- Receipt storage (photos and screenshots)
- CSV export
- Telegram bot — forward a WhatsApp payment message, AI parses it and creates a record automatically

## Stack

- Python, FastAPI, SQLite
- Telegram Bot API + OpenAI GPT-4o-mini

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env
# fill in .env
venv/bin/uvicorn main:app --port 8000
```

## Environment variables

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather |
| `OPENAI_API_KEY` | OpenAI API key |
| `BASE_URL` | Public HTTPS URL of the app (for Telegram webhook) |
| `DATA_DIR` | Directory for database and uploads (default: `.`) |

## Deploy

The app is designed for CapRover. Add a `captain-definition` and `Dockerfile` are included.

Set `/data` as a persistent directory in CapRover and set `DATA_DIR=/data` in env vars.
