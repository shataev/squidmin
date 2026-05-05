import os
import json
import sqlite3
from datetime import date, timedelta
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

DB_PATH = "data.db"
UPLOADS_DIR = "uploads"
os.makedirs(UPLOADS_DIR, exist_ok=True)

PERIOD_DAYS   = {"1_day": 1, "1_week": 7, "1_month": 30}
PERIOD_LABELS = {"1_day": "1 day", "1_week": "1 week", "1_month": "1 month"}
METHOD_LABELS = {"cash": "Cash", "transfer": "Transfer"}
REQUIRED      = ["client_name", "game", "amount_baht", "payment_method", "subscription_period"]


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def calc_end_date(payment_date: str, period: str) -> str:
    d = date.fromisoformat(payment_date)
    return (d + timedelta(days=PERIOD_DAYS.get(period, 30))).isoformat()


def save_payment(data: dict, receipt_filename: str = None) -> int:
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO payments (client_name, game, amount_baht, payment_method, "
        "payment_date, subscription_period, end_date, receipt_filename) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            data["client_name"], data["game"], data["amount_baht"],
            data["payment_method"], data["payment_date"], data["subscription_period"],
            calc_end_date(data["payment_date"], data["subscription_period"]),
            receipt_filename,
        ),
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


# ── AI helpers ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You extract payment info for an e-sport training company.
Return ONLY a JSON object with these fields (use null if unknown):
- client_name: string
- game: string  (e.g. Valorant, CS2, Dota 2)
- amount_baht: number
- payment_method: "cash" or "transfer"  (cash=cash/нал/наличные; transfer=transfer/перевод/qr/online)
- subscription_period: "1_day" or "1_week" or "1_month"
  Rules: "monthly"/"month"/"subscription" → "1_month"; "weekly"/"week" → "1_week"; "daily"/"day" → "1_day"
  Ignore visit frequency (e.g. "1visit/week" is NOT the period — it's training schedule)
- payment_date: ISO date YYYY-MM-DD (today={today} if not specified)

Return only valid JSON, no markdown."""


def parse_with_ai(text: str, context_hint: str = "") -> dict:
    today = date.today().isoformat()
    system = SYSTEM_PROMPT.replace("{today}", today)
    user_msg = f"{context_hint}\n\nMessage: {text}" if context_hint else text

    resp = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {"role": "system", "content": system},
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


def get_missing(data: dict) -> list[str]:
    return [f for f in REQUIRED if not data.get(f)]


def merge(base: dict, update: dict) -> dict:
    """Merge update into base, only filling null/missing fields."""
    result = dict(base)
    for k, v in update.items():
        if v is not None and not result.get(k):
            result[k] = v
    return result


# ── Formatting ────────────────────────────────────────────────────────────────

FIELD_NAMES = {
    "client_name": "Client name",
    "game": "Game",
    "amount_baht": "Amount (฿)",
    "payment_method": "Payment method",
    "subscription_period": "Period",
}


def format_summary(data: dict) -> str:
    def val(key):
        v = data.get(key)
        if v is None:
            return "❓"
        if key == "payment_method":
            return METHOD_LABELS.get(v, v)
        if key == "subscription_period":
            return PERIOD_LABELS.get(v, v)
        if key == "amount_baht":
            return f"฿{v:,.0f}"
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


CANCEL_KB = InlineKeyboardMarkup([[InlineKeyboardButton("✖ Cancel", callback_data="cancel")]])
CONFIRM_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Save", callback_data="confirm_yes"),
     InlineKeyboardButton("✖ Cancel", callback_data="cancel")],
])


def missing_prompt(missing: list[str]) -> str:
    names = [FIELD_NAMES[f] for f in missing]
    listed = "\n".join(f"  • {n}" for n in names)
    return f"❓ Still need:\n{listed}\n\nReply with the missing info in any format."


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Hi! I'm *Squid Bot*.\n\n"
        "Forward or send a payment message and I'll save it.\n\n"
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
    text = update.message.text.strip()
    state = context.user_data.get("state")

    # ── Confirmation ──
    if state == "confirm":
        if text.lower() in ("yes", "y", "да", "save", "ok", "yep", "yeah"):
            pid = save_payment(context.user_data["pending"])
            context.user_data["payment_id"] = pid
            context.user_data["state"] = "receipt"
            await update.message.reply_text(
                "✅ *Saved!* Send receipt photo or /done to finish.",
                parse_mode="Markdown",
            )
        elif text.lower() in ("no", "n", "нет", "cancel"):
            context.user_data.clear()
            await update.message.reply_text("❌ Cancelled.")
        else:
            await update.message.reply_text("Reply *yes* to save or *no* to cancel.", parse_mode="Markdown")
        return

    # ── Waiting for receipt but got text ──
    if state == "receipt":
        context.user_data.clear()
        await update.message.reply_text("✅ Done, no receipt attached.")
        return

    # ── Filling missing fields ──
    if state == "filling":
        pending = context.user_data.get("pending", {})
        missing = get_missing(pending)
        hint = f"Previously known: {json.dumps({k: pending[k] for k in REQUIRED if pending.get(k)})}\nMissing fields: {', '.join(missing)}"
        fresh = parse_with_ai(text, context_hint=hint)
        context.user_data["pending"] = merge(pending, fresh)
        await proceed(update, context)
        return

    # ── Fresh message ──
    msg = await update.message.reply_text("🔍 Parsing...")
    parsed = parse_with_ai(text)
    await msg.delete()
    context.user_data["pending"] = parsed
    context.user_data["state"] = "filling"
    await proceed(update, context)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")

    # Photo + caption = new payment
    if not state and update.message.caption:
        msg = await update.message.reply_text("🔍 Parsing...")
        parsed = parse_with_ai(update.message.caption)
        await msg.delete()
        context.user_data["pending"] = parsed
        context.user_data["state"] = "filling"
        context.user_data["_pending_photo"] = update.message.photo[-1].file_id
        await proceed(update, context)
        return

    # Receipt after saving
    if state == "receipt":
        pid = context.user_data.get("payment_id")
        if pid:
            photo = update.message.photo[-1]
            tg_file = await context.bot.get_file(photo.file_id)
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


async def proceed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending", {})
    missing = get_missing(pending)

    await update.message.reply_text(format_summary(pending), parse_mode="Markdown")

    if missing:
        context.user_data["state"] = "filling"
        await update.message.reply_text(missing_prompt(missing), reply_markup=CANCEL_KB)
    else:
        context.user_data["state"] = "confirm"
        await update.message.reply_text(
            "Everything looks good!",
            parse_mode="Markdown",
            reply_markup=CONFIRM_KB,
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelled.")

    elif query.data == "confirm_yes":
        pending = context.user_data.get("pending")
        if not pending:
            await query.edit_message_text("❌ No data found. Send a new message.")
            return
        pid = save_payment(pending)
        context.user_data["payment_id"] = pid
        context.user_data["state"] = "receipt"
        await query.edit_message_text("✅ *Saved!* Send receipt photo or /done to finish.", parse_mode="Markdown")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("🦑 Squid Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
