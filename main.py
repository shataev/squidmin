import os
import json
import sqlite3
import shutil
import csv
import io
from datetime import date, timedelta
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

DATA_DIR    = os.getenv("DATA_DIR", ".")
DB_PATH     = os.path.join(DATA_DIR, "data.db")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
BASE_URL       = os.getenv("BASE_URL", "")      # e.g. https://squid.yourdomain.com

PERIOD_DAYS   = {"1_day": 1, "1_week": 7, "1_month": 30}
PERIOD_LABELS = {"1_day": "1 day", "1_week": "1 week", "1_month": "1 month"}
METHOD_LABELS = {"cash": "Cash", "transfer": "Transfer"}
REQUIRED      = ["client_name", "game", "amount_baht", "payment_method", "subscription_period"]
FIELD_NAMES   = {
    "client_name": "Client name", "game": "Game",
    "amount_baht": "Amount (฿)", "payment_method": "Payment method",
    "subscription_period": "Period",
}

# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_name TEXT NOT NULL,
            game TEXT NOT NULL,
            amount_baht REAL NOT NULL,
            payment_method TEXT NOT NULL,
            payment_date TEXT NOT NULL,
            subscription_period TEXT NOT NULL,
            end_date TEXT NOT NULL,
            receipt_filename TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def calc_end_date(payment_date: str, period: str) -> str:
    d = date.fromisoformat(payment_date)
    return (d + timedelta(days=PERIOD_DAYS.get(period, 30))).isoformat()


def get_status(end_date: str) -> str:
    today = date.today()
    end = date.fromisoformat(end_date)
    if end < today:
        return "expired"
    if end <= today + timedelta(days=3):
        return "expiring"
    return "active"


def row_to_dict(row) -> dict:
    d = dict(row)
    d["status"] = get_status(d["end_date"])
    return d


def save_payment(data: dict, receipt_filename: str = None) -> int:
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO payments (client_name, game, amount_baht, payment_method, "
        "payment_date, subscription_period, end_date, receipt_filename) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (data["client_name"], data["game"], data["amount_baht"], data["payment_method"],
         data["payment_date"], data["subscription_period"],
         calc_end_date(data["payment_date"], data["subscription_period"]), receipt_filename),
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


# ── Bot logic ─────────────────────────────────────────────────────────────────

bot_app = None


def _get_openai():
    from openai import OpenAI
    return OpenAI(api_key=OPENAI_API_KEY)


SYSTEM_PROMPT = """You extract payment info for an e-sport training company.
Return ONLY a JSON object with these fields (use null if unknown):
- client_name: string
- game: string (e.g. Valorant, CS2, Dota 2)
- amount_baht: number
- payment_method: "cash" or "transfer" (cash=cash/нал/наличные; transfer=transfer/перевод/qr/online)
- subscription_period: "1_day" or "1_week" or "1_month"
  Rules: monthly/month/subscription → "1_month"; weekly/week → "1_week"; daily/day → "1_day"
  IMPORTANT: visit frequency like "1visit/week" is training schedule, NOT subscription period
- payment_date: ISO date YYYY-MM-DD (use {today} if not specified)
Return only valid JSON, no markdown."""


def parse_with_ai(text: str, context_hint: str = "") -> dict:
    today = date.today().isoformat()
    client = _get_openai()
    user_msg = f"{context_hint}\n\nMessage: {text}" if context_hint else text
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT.replace("{today}", today)},
            {"role": "user", "content": user_msg},
        ],
    )
    try:
        data = json.loads(resp.choices[0].message.content.strip())
        if not data.get("payment_date"):
            data["payment_date"] = today
        return data
    except Exception:
        return {"payment_date": today}


def get_missing(data: dict) -> list:
    return [f for f in REQUIRED if not data.get(f)]


def merge(base: dict, patch: dict) -> dict:
    result = dict(base)
    for k, v in patch.items():
        if v is not None and not result.get(k):
            result[k] = v
    return result


def format_summary(data: dict) -> str:
    def val(key):
        v = data.get(key)
        if v is None:
            return "❓"
        if key == "payment_method":   return METHOD_LABELS.get(v, v)
        if key == "subscription_period": return PERIOD_LABELS.get(v, v)
        if key == "amount_baht":      return f"฿{v:,.0f}"
        return str(v)
    return (
        "📋 *Payment summary*\n"
        f"👤 Client: {val('client_name')}\n"
        f"🎮 Game: {val('game')}\n"
        f"💰 Amount: {val('amount_baht')}\n"
        f"💳 Method: {val('payment_method')}\n"
        f"📅 Date: {data.get('payment_date', '—')}\n"
        f"⏱ Period: {val('subscription_period')}"
    )


def missing_prompt(missing: list) -> str:
    listed = "\n".join(f"  • {FIELD_NAMES[f]}" for f in missing)
    return f"❓ Still need:\n{listed}\n\nReply with the missing info in any format."


async def setup_bot():
    global bot_app
    if not TELEGRAM_TOKEN:
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

    CANCEL_KB  = InlineKeyboardMarkup([[InlineKeyboardButton("✖ Cancel", callback_data="cancel")]])
    CONFIRM_KB = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Save",   callback_data="confirm_yes"),
        InlineKeyboardButton("✖ Cancel", callback_data="cancel"),
    ]])

    async def proceed(update, context):
        pending = context.user_data.get("pending", {})
        missing = get_missing(pending)
        msg = update.message or update.callback_query.message
        await msg.reply_text(format_summary(pending), parse_mode="Markdown")
        if missing:
            context.user_data["state"] = "filling"
            await msg.reply_text(missing_prompt(missing), reply_markup=CANCEL_KB)
        else:
            context.user_data["state"] = "confirm"
            await msg.reply_text("Everything looks good!", reply_markup=CONFIRM_KB)

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        await update.message.reply_text(
            "👋 Hi! I'm *Squid Bot*.\n\nForward or send a payment message and I'll save it.\n\n"
            "_Example: Muchuan paid 3600 baht cash for Valorant 1 month_",
            parse_mode="Markdown",
        )

    async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        await update.message.reply_text("❌ Cancelled.")

    async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        await update.message.reply_text("✅ Done.")

    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text  = update.message.text.strip()
        state = context.user_data.get("state")

        if state == "confirm":
            if text.lower() in ("yes", "y", "да", "save", "ok", "yep"):
                pid = save_payment(context.user_data["pending"])
                context.user_data.update({"payment_id": pid, "state": "receipt"})
                await update.message.reply_text("✅ *Saved!* Send receipt photo or /done to finish.", parse_mode="Markdown")
            elif text.lower() in ("no", "n", "нет", "cancel"):
                context.user_data.clear()
                await update.message.reply_text("❌ Cancelled.")
            else:
                await update.message.reply_text("Reply *yes* to save or *no* to cancel.", parse_mode="Markdown")
            return

        if state == "receipt":
            context.user_data.clear()
            await update.message.reply_text("✅ Done, no receipt attached.")
            return

        if state == "filling":
            pending = context.user_data.get("pending", {})
            missing = get_missing(pending)
            hint = (f"Previously known: {json.dumps({k: pending[k] for k in REQUIRED if pending.get(k)})}\n"
                    f"Missing: {', '.join(missing)}")
            fresh = parse_with_ai(text, context_hint=hint)
            context.user_data["pending"] = merge(pending, fresh)
            await proceed(update, context)
            return

        # Fresh message
        msg = await update.message.reply_text("🔍 Parsing...")
        parsed = parse_with_ai(text)
        await msg.delete()
        context.user_data.update({"pending": parsed, "state": "filling"})
        await proceed(update, context)

    async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        state = context.user_data.get("state")

        if not state and update.message.caption:
            msg = await update.message.reply_text("🔍 Parsing...")
            parsed = parse_with_ai(update.message.caption)
            await msg.delete()
            context.user_data.update({"pending": parsed, "state": "filling"})
            await proceed(update, context)
            return

        if state == "receipt":
            pid = context.user_data.get("payment_id")
            if pid:
                tg_file = await context.bot.get_file(update.message.photo[-1].file_id)
                filename = f"{pid}.jpg"
                await tg_file.download_to_drive(os.path.join(UPLOADS_DIR, filename))
                conn = get_db()
                conn.execute("UPDATE payments SET receipt_filename = ? WHERE id = ?", (filename, pid))
                conn.commit()
                conn.close()
                await update.message.reply_text("✅ Receipt saved!")
            context.user_data.clear()
            return

        await update.message.reply_text("Send a payment message first, then the receipt photo.")

    async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if query.data == "cancel":
            context.user_data.clear()
            await query.edit_message_text("❌ Cancelled.")
        elif query.data == "confirm_yes":
            pending = context.user_data.get("pending")
            if not pending:
                await query.edit_message_text("❌ No data. Send a new message.")
                return
            pid = save_payment(pending)
            context.user_data.update({"payment_id": pid, "state": "receipt"})
            await query.edit_message_text("✅ *Saved!* Send receipt photo or /done to finish.", parse_mode="Markdown")

    application = Application.builder().token(TELEGRAM_TOKEN).updater(None).build()
    application.add_handler(CommandHandler("start",  cmd_start))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("done",   cmd_done))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await application.initialize()
    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL}/telegram/webhook")

    bot_app = application


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await setup_bot()
    yield
    if bot_app:
        await bot_app.shutdown()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Telegram webhook ──────────────────────────────────────────────────────────

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if not bot_app:
        return {"ok": False}
    from telegram import Update
    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return {"ok": True}


# ── Web API ───────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/payments")
def get_payments():
    conn = get_db()
    rows = conn.execute("SELECT * FROM payments ORDER BY created_at DESC").fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]


@app.post("/payments")
async def create_payment(
    client_name: str = Form(...),
    game: str = Form(...),
    amount_baht: float = Form(...),
    payment_method: str = Form(...),
    payment_date: str = Form(...),
    subscription_period: str = Form(...),
    receipt: Optional[UploadFile] = File(None),
):
    end_date = calc_end_date(payment_date, subscription_period)
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO payments (client_name, game, amount_baht, payment_method, payment_date, subscription_period, end_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (client_name, game, amount_baht, payment_method, payment_date, subscription_period, end_date),
    )
    payment_id = cursor.lastrowid
    if receipt and receipt.filename:
        ext = os.path.splitext(receipt.filename)[1]
        receipt_filename = f"{payment_id}{ext}"
        with open(os.path.join(UPLOADS_DIR, receipt_filename), "wb") as f:
            shutil.copyfileobj(receipt.file, f)
        conn.execute("UPDATE payments SET receipt_filename = ? WHERE id = ?", (receipt_filename, payment_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/receipts/{payment_id}")
def get_receipt(payment_id: int, download: int = 0):
    conn = get_db()
    row = conn.execute("SELECT receipt_filename FROM payments WHERE id = ?", (payment_id,)).fetchone()
    conn.close()
    if not row or not row["receipt_filename"]:
        return {"error": "No receipt"}
    filepath = os.path.join(UPLOADS_DIR, row["receipt_filename"])
    if not os.path.exists(filepath):
        return {"error": "File not found"}
    headers = {"Content-Disposition": f"attachment; filename={row['receipt_filename']}"} if download else {}
    return FileResponse(filepath, headers=headers)


@app.delete("/payments/{payment_id}")
def delete_payment(payment_id: int):
    conn = get_db()
    row = conn.execute("SELECT receipt_filename FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if row and row["receipt_filename"]:
        filepath = os.path.join(UPLOADS_DIR, row["receipt_filename"])
        if os.path.exists(filepath):
            os.remove(filepath)
    conn.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/export/csv")
def export_csv():
    conn = get_db()
    rows = conn.execute("SELECT * FROM payments ORDER BY created_at DESC").fetchall()
    conn.close()
    period_labels = {"1_day": "1 day", "1_week": "1 week", "1_month": "1 month"}
    method_labels = {"cash": "Cash", "transfer": "Transfer"}
    status_labels = {"active": "Active", "expiring": "Expiring", "expired": "Expired"}
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Client", "Game", "Amount (฿)", "Payment", "Date", "Period", "Valid Until", "Status", "Receipt"])
    for r in rows:
        d = dict(r)
        status = get_status(d["end_date"])
        writer.writerow([
            d["id"], d["client_name"], d["game"], d["amount_baht"],
            method_labels.get(d["payment_method"], d["payment_method"]),
            d["payment_date"],
            period_labels.get(d["subscription_period"], d["subscription_period"]),
            d["end_date"], status_labels.get(status, status),
            "Yes" if d["receipt_filename"] else "No",
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=payments.csv"},
    )
