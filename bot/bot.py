"""
Ratsion Telegram bot + Order API
--------------------------------
What it does:
  /start  -> greets the user and shows a button that opens the Ratsion Mini App
  /myid   -> tells you the current chat id (use it to fill MANAGER_CHAT_ID)

  Manager-only commands (ignored for everyone except MANAGER_CHAT_ID):
  /delivering <order#>  -> mark an order as "Доставляется" + notify the customer
  /delivered  <order#>  -> mark an order as "Доставлен"   + notify the customer
  /orders               -> list recent orders and their statuses

  The Mini App sends an order to this bot through TWO possible channels:

    1. HTTP API  (PRIMARY, reliable)
       The Mini App does fetch(API_URL + "/order"). This works no matter how
       the app was opened, returns a REAL server-assigned order number, and only
       reports success once the manager message was actually delivered.

    2. WebApp.sendData (LEGACY fallback)
       Telegram only delivers sendData() when the app was opened from a
       *reply-keyboard* web_app button - not from an inline button / menu / link.
       That limitation is why orders used to silently never reach the manager.
       It is kept here only so the app still half-works if no API is configured.

Setup (environment variables on Render -> Settings -> Environment):
  BOT_TOKEN        - from @BotFather                       (required)
  MANAGER_CHAT_ID  - chat that should receive orders        (required; use /myid)
  WEBAPP_URL       - your deployed Mini App URL             (required)
  PORT             - injected by Render automatically       (default 8080)
  ALLOW_ORIGIN     - the Mini App origin for CORS, or "*"   (default "*")
  ORDER_SEQ_FILE   - file that stores the order counter     (default order_seq.txt)
  ORDERS_FILE      - file that stores orders + statuses      (default orders.json)

IMPORTANT (Render): deploy this as a **Web Service** (not a Background Worker),
because it now binds an HTTP port. The Mini App must point CONFIG.apiBase at the
public URL Render gives this service.

Run locally:
  pip install -r requirements.txt
  python bot.py
"""

import os
import json
import hmac
import hashlib
import html
import asyncio
import logging
from urllib.parse import parse_qsl
from datetime import datetime, timezone, timedelta

from aiohttp import web
from telegram import (
    Update,
    WebAppInfo,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================ CONFIG ============================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PASTE_TOKEN_FOR_LOCAL_TEST")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://aizbek.github.io/Ratsion-tashkent/")
MANAGER_CHAT_ID = int(os.environ.get("MANAGER_CHAT_ID", "0"))
PORT = int(os.environ.get("PORT", "8080"))
ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", "*")
SEQ_FILE = os.environ.get("ORDER_SEQ_FILE", "order_seq.txt")
ORDERS_FILE = os.environ.get("ORDERS_FILE", "orders.json")
SEQ_START = 1041  # first order will be SEQ_START + 1
# ===============================================================

TASHKENT = timezone(timedelta(hours=5))  # Uzbekistan, no DST

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ratsion")

_seq_lock = asyncio.Lock()
_orders_lock = asyncio.Lock()


# ----------------------------- orders store -----------------------------
# Maps order_no -> {uid, status, category, date, days, total}. Lets the manager
# update delivery status and lets the Mini App read each customer's statuses.
# NOTE: on Render's FREE tier the disk is ephemeral (resets on redeploy/sleep),
# so this can be lost. The customer is still notified by chat message either way.
# For rock-solid persistence use a Render persistent disk or a small database.

def _read_orders():
    try:
        with open(ORDERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_orders(data):
    try:
        with open(ORDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError as e:
        log.warning("Could not persist orders: %s", e)


async def store_order(order_no, uid, info):
    async with _orders_lock:
        data = _read_orders()
        data[order_no] = {"uid": uid, "status": "Принят", **info}
        _write_orders(data)


async def set_order_status(order_no, status):
    async with _orders_lock:
        data = _read_orders()
        rec = data.get(order_no)
        if not rec:
            return None
        rec["status"] = status
        data[order_no] = rec
        _write_orders(data)
        return rec


def orders_for_uid(uid):
    return [
        {"no": no, "status": rec.get("status", "Принят")}
        for no, rec in _read_orders().items()
        if rec.get("uid") == uid
    ]


def fmt_sum(n):
    """Format 8400000 -> '8 400 000 сум'."""
    try:
        return f"{int(n):,}".replace(",", " ") + " сум"
    except (TypeError, ValueError):
        return "—"


async def next_order_no():
    """Server-authoritative order number, e.g. 'R-250626-1042'.

    The counter persists in SEQ_FILE so numbers keep climbing across restarts.
    (Render's free disk is ephemeral; if the file is lost the counter restarts
    from SEQ_START, but the date prefix keeps numbers readable and unique enough
    for a single small business.)
    """
    async with _seq_lock:
        seq = SEQ_START
        try:
            with open(SEQ_FILE, "r", encoding="utf-8") as f:
                seq = int((f.read().strip() or str(SEQ_START)))
        except (OSError, ValueError):
            pass
        seq += 1
        try:
            with open(SEQ_FILE, "w", encoding="utf-8") as f:
                f.write(str(seq))
        except OSError as e:
            log.warning("Could not persist order seq: %s", e)
    today = datetime.now(TASHKENT).strftime("%d%m%y")
    return f"R-{today}-{seq}"


def verify_init_data(init_data):
    """Validate Telegram WebApp initData and return the parsed payload.

    Returns a dict {"user": <dict|None>, "fields": <dict>} when the HMAC checks
    out, or None when init_data is missing/empty/tampered. This proves the
    request really came from a Telegram user opening OUR Mini App, and lets us
    trust the user id we message the confirmation to.
    """
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, received_hash):
        return None
    user = None
    if "user" in pairs:
        try:
            user = json.loads(pairs["user"])
        except json.JSONDecodeError:
            user = None
    return {"user": user, "fields": pairs}


def build_card(d, order_no, user=None):
    """Format the order card sent to the manager (plain text, never breaks).

    Field order per client request (June 2026): start date, duration, customer
    name + @username, program, price per day, total for the period, address,
    phone, payment method, delivery time.
    """
    # prefer the verified Telegram identity, fall back to what the app sent
    uname = None
    if user and user.get("username"):
        uname = "@" + user["username"]
    elif d.get("tg_username"):
        uname = "@" + d["tg_username"]
    uid = (user or {}).get("id") or d.get("tg_id")

    lines = [
        f"🥗 НОВЫЙ ЗАКАЗ  {order_no}",
        "━━━━━━━━━━━━━━",
        f"Дата начала: {d.get('date', '—')}",
        f"Срок: {d.get('duration', '—')}",
        "━━━━━━━━━━━━━━",
        f"Имя: {d.get('name', '—')}",
    ]
    if uname:
        lines.append(f"Telegram: {uname}")
    elif uid:
        lines.append(f"Telegram ID: {uid}")
    lines.append(f"Рост: {d.get('height') or '—'} см")
    lines.append(f"Вес: {d.get('weight') or '—'} кг")
    lines += [
        "━━━━━━━━━━━━━━",
        f"Программа: {d.get('category', '—')}",
        f"Калорийность: {d.get('kcal', '—')} ккал/день",
        f"Цена за день: {fmt_sum(d.get('price_per_day'))}",
        f"{d.get('days', '?')} × {fmt_sum(d.get('price_per_day'))} = {fmt_sum(d.get('subtotal'))}",
    ]
    if d.get("discount_pct"):
        lines.append(f"Скидка: {d['discount_pct']}% (−{fmt_sum(d.get('discount_amt'))})")
    lines += [
        f"ИТОГО: {fmt_sum(d.get('total'))}",
        "━━━━━━━━━━━━━━",
        f"Адрес: {d.get('address', '—')}",
        f"Телефон: {d.get('phone', '—')}",
        f"Оплата: {d.get('payment', '—')}",
        f"Время доставки: {d.get('delivery_time', '—')}",
        "━━━━━━━━━━━━━━",
        "Свяжитесь с клиентом для подтверждения заказа.",
    ]
    return "\n".join(lines)


def build_contact_message(user, phone):
    """Message the manager gets when a customer taps 'Написать менеджеру'.

    Field order per client request (July 2026):
      1. the customer's Telegram name (clickable when Telegram can resolve them),
      2. the phone number they entered (this is the reliable way to reach them),
      3. their @username — or, if they have none, a clear note that their profile
         has no @username, so there is NO direct way to open a Telegram chat and
         the manager must call the phone instead.

    This is intentionally SEPARATE from build_card() (the order message) so the
    two never affect each other.
    """
    if user:
        uid = user.get("id")
        name = (user.get("first_name", "") + " " + user.get("last_name", "")).strip()
        username = user.get("username")
    else:
        uid, name, username = None, "", None

    display = html.escape(name) if name else "—"
    if uid and name:
        name_line = f'Имя: <a href="tg://user?id={uid}">{display}</a>'
    else:
        name_line = f"Имя: {display}"

    lines = [
        "📞 НОВЫЙ ЗАПРОС НА СВЯЗЬ",
        "━━━━━━━━━━━━━━",
        name_line,
        f"Телефон: {html.escape(phone) if phone else '—'}",
    ]
    if username:
        lines.append(f"Telegram: @{html.escape(username)}")
    else:
        lines.append(
            "Telegram: у клиента не задан @username (профиль скрыт), "
            "поэтому написать ему напрямую в Telegram нельзя — свяжитесь по телефону."
        )
    lines += [
        "━━━━━━━━━━━━━━",
        "Свяжитесь с клиентом.",
    ]
    return "\n".join(lines)


# ----------------------------- bot commands -----------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greet and show the 'Open Ratsion' button."""
    open_btn = InlineKeyboardButton(
        "🍽 Открыть Ratsion",
        web_app=WebAppInfo(url=WEBAPP_URL),
    )
    # also pin a reply-keyboard button so the legacy sendData channel still works
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("🍽 Открыть Ratsion", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True,
    )
    await update.message.reply_text(
        "Добро пожаловать в *Ratsion* — здоровая и вкусная еда \n\n"
        "Здесь вы можете:\n"
        "• Заказать программу правильного питания\n"
        "• Рассчитать суточную норму калорий\n"
        "• Узнать о наших акциях\n"
        "• Связаться с нами\n\n"
        "Нажмите кнопку ниже, чтобы начать ",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    await update.message.reply_text(
        "Открыть приложение:", reply_markup=InlineKeyboardMarkup([[open_btn]])
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Helper: tells you the current chat's ID (use to fill MANAGER_CHAT_ID)."""
    await update.message.reply_text(
        f"Этот chat_id: `{update.effective_chat.id}`", parse_mode="Markdown"
    )


async def on_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """LEGACY channel: order arrives via WebApp.sendData (reply-keyboard only)."""
    raw = update.effective_message.web_app_data.data
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Bad web_app_data: %s", raw)
        return

    user = update.effective_user
    user_dict = {"id": user.id, "username": user.username} if user else None

    if d.get("type") == "contact_manager":
        if MANAGER_CHAT_ID:
            contact_user = {
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
            } if user else None
            await context.bot.send_message(
                MANAGER_CHAT_ID,
                build_contact_message(contact_user, (d.get("phone") or "").strip()),
                parse_mode="HTML",
            )
        await update.effective_message.reply_text(
            "Спасибо! Менеджер свяжется с вами в ближайшее время."
        )
        return

    if d.get("type") != "new_order":
        return

    order_no = d.get("orderNo") or await next_order_no()
    if MANAGER_CHAT_ID:
        try:
            await context.bot.send_message(MANAGER_CHAT_ID, build_card(d, order_no, user_dict))
        except Exception as e:
            log.error("Could not send order to manager (sendData channel): %s", e)

    await update.effective_message.reply_text(
        f"✅ Ваш заказ {order_no} принят!\n"
        f"Менеджер свяжется с вами в ближайшее время для подтверждения. Спасибо!"
    )


# ----------------------------- HTTP API -----------------------------

def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = ALLOW_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


async def handle_preflight(request):
    return _cors(web.Response(status=204))


async def handle_health(request):
    return web.Response(text="Ratsion bot is up")


async def handle_order(request):
    """PRIMARY channel: the Mini App POSTs the order here."""
    bot = request.app["bot"]
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"ok": False, "error": "bad_json"}, status=400))

    init_data = body.get("initData", "")
    order = body.get("order", {}) or {}

    # Reject empty / probe requests so they can't mint phantom order numbers.
    if not (order.get("category") and order.get("total") and order.get("phone")):
        return _cors(web.json_response({"ok": False, "error": "empty_order"}, status=400))

    # If the app sent initData, it MUST be valid (anti-spoof). If it's empty
    # (e.g. tested in a plain browser) we still accept, just without a verified user.
    auth = verify_init_data(init_data)
    if init_data and auth is None:
        return _cors(web.json_response({"ok": False, "error": "bad_init_data"}, status=403))
    user = (auth or {}).get("user")

    if not MANAGER_CHAT_ID:
        log.error("MANAGER_CHAT_ID is not configured - cannot deliver order")
        return _cors(web.json_response({"ok": False, "error": "no_manager"}, status=500))

    order_no = await next_order_no()
    try:
        await bot.send_message(MANAGER_CHAT_ID, build_card(order, order_no, user))
    except Exception as e:
        log.error("Could not send order to manager (API channel): %s", e)
        return _cors(web.json_response({"ok": False, "error": "send_failed"}, status=502))

    # remember the order so the manager can update its status later
    await store_order(order_no, (user or {}).get("id"), {
        "category": order.get("category"),
        "date": order.get("date"),
        "days": order.get("days"),
        "total": order.get("total"),
    })

    # best-effort confirmation back to the customer (needs them to have started the bot)
    if user and user.get("id"):
        try:
            await bot.send_message(
                user["id"],
                f"✅ Ваш заказ {order_no} принят!\n"
                f"Менеджер свяжется с вами в ближайшее время для подтверждения. Спасибо!",
            )
        except Exception as e:
            log.info("Could not send confirmation to customer: %s", e)

    return _cors(web.json_response({"ok": True, "orderNo": order_no}))


async def handle_contact(request):
    """Customer tapped 'Написать менеджеру' inside the app."""
    bot = request.app["bot"]
    try:
        body = await request.json()
    except Exception:
        body = {}

    auth = verify_init_data(body.get("initData", ""))
    user = (auth or {}).get("user")
    phone = (body.get("phone") or "").strip()

    if not MANAGER_CHAT_ID:
        return _cors(web.json_response({"ok": False, "error": "no_manager"}, status=500))

    try:
        await bot.send_message(
            MANAGER_CHAT_ID,
            build_contact_message(user, phone),
            parse_mode="HTML",
        )
    except Exception as e:
        log.error("Could not forward contact request: %s", e)
        return _cors(web.json_response({"ok": False, "error": "send_failed"}, status=502))

    return _cors(web.json_response({"ok": True}))


async def handle_my_orders(request):
    """The Mini App asks for this user's orders + live delivery statuses."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    auth = verify_init_data(body.get("initData", ""))
    uid = ((auth or {}).get("user") or {}).get("id") if auth else None
    if not uid:
        return _cors(web.json_response({"ok": True, "orders": []}))
    return _cors(web.json_response({"ok": True, "orders": orders_for_uid(uid)}))


# ----------------------------- manager-only commands -----------------------------

def _is_manager(update: Update) -> bool:
    """True only for the configured manager chat. Guards delivery commands."""
    return bool(MANAGER_CHAT_ID) and update.effective_chat and update.effective_chat.id == MANAGER_CHAT_ID


async def _set_status_cmd(update, context, status, verb):
    # Silently ignore for everyone except the manager — a random user typing
    # /delivered <number> must NOT be able to change anyone's order.
    if not _is_manager(update):
        return
    if not context.args:
        await update.message.reply_text(f"Использование: /{verb} <номер заказа>\nНапример: /{verb} R-260626-1042")
        return
    order_no = context.args[0].strip()
    rec = await set_order_status(order_no, status)
    if not rec:
        await update.message.reply_text(f"Заказ {order_no} не найден.")
        return
    uid = rec.get("uid")
    if uid:
        try:
            if status == "Доставляется":
                await context.bot.send_message(uid, f"🚗 Ваш заказ {order_no} передан в доставку. Курьер уже в пути!")
            else:
                await context.bot.send_message(uid, f"✅ Ваш заказ {order_no} доставлен. Приятного аппетита, и спасибо, что выбрали Ratsion 💚")
        except Exception as e:
            log.info("Could not notify customer %s: %s", uid, e)
    await update.message.reply_text(f"Готово ✅ Заказ {order_no}: статус «{status}».")


async def delivering(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_status_cmd(update, context, "Доставляется", "delivering")


async def delivered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_status_cmd(update, context, "Доставлен", "delivered")


async def list_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manager-only: quick list of recent orders and their statuses."""
    if not _is_manager(update):
        return
    data = _read_orders()
    if not data:
        await update.message.reply_text("Заказов пока нет.")
        return
    lines = [f"{no} — {rec.get('status', 'Принят')} — {fmt_sum(rec.get('total'))}" for no, rec in list(data.items())[-25:]]
    await update.message.reply_text("Последние заказы:\n" + "\n".join(lines))


# ----------------------------- bootstrap -----------------------------

async def setup_commands(bot):
    """Set the '/' command menu. Manager commands are scoped to the manager's
    chat only, so regular customers never even see them."""
    # everyone sees just this
    await bot.set_my_commands(
        [BotCommand("start", "Открыть приложение Ratsion")],
        scope=BotCommandScopeDefault(),
    )
    # the manager additionally sees the delivery commands
    if MANAGER_CHAT_ID:
        try:
            await bot.set_my_commands(
                [
                    BotCommand("start", "Открыть приложение Ratsion"),
                    BotCommand("orders", "Последние заказы и их статусы"),
                    BotCommand("delivering", "В доставке: /delivering <номер>"),
                    BotCommand("delivered", "Доставлен: /delivered <номер>"),
                    BotCommand("myid", "Показать chat_id"),
                ],
                scope=BotCommandScopeChat(chat_id=MANAGER_CHAT_ID),
            )
        except Exception as e:
            log.warning("Could not set manager commands: %s", e)


async def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("myid", myid))
    application.add_handler(CommandHandler("delivering", delivering))
    application.add_handler(CommandHandler("delivered", delivered))
    application.add_handler(CommandHandler("orders", list_orders))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_webapp_data))

    web_app = web.Application()
    web_app["bot"] = application.bot
    web_app.add_routes([
        web.get("/", handle_health),
        web.post("/order", handle_order),
        web.options("/order", handle_preflight),
        web.post("/contact", handle_contact),
        web.options("/contact", handle_preflight),
        web.post("/my-orders", handle_my_orders),
        web.options("/my-orders", handle_preflight),
    ])

    # start the polling bot (for /start, /myid, legacy sendData)
    await application.initialize()
    await application.start()
    await setup_commands(application.bot)
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    # start the HTTP API (for the Mini App order channel)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    log.info("Ratsion bot + order API running (HTTP on :%s)", PORT)
    if not MANAGER_CHAT_ID:
        log.warning("MANAGER_CHAT_ID is NOT set - orders cannot be delivered!")

    try:
        await asyncio.Event().wait()  # run forever
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
