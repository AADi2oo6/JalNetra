"""JalNetra Telegram bot (Phase 2): a GPT-4o-mini assistant, grounded in the
platform's own live data through OpenAI function calling (Layer 2) -- lake
lookup, indicator health, active alerts and satellite-scan triggers, plus
native GPS location sharing for "what's near me".

Run it directly (polling, no webhook/ngrok needed):

    cd backend && uv run python -m app.telegram.bot
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.core.logging import configure_logging
from app.telegram.tools import (
    TOOL_FUNCTIONS,
    TOOLS,
    discover_lakes,
    fetch_thumbnail_bytes,
    get_active_alerts,
    get_lake_health,
    get_lake_health_by_id,
    list_top_lakes,
    list_wishlist,
)

load_dotenv()  # walks up from this file to the repo-root .env, same as test_ai_key.py

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL", "http://localhost:5173")

NEARBY_RADIUS_KM = 15.0
MAX_TOOL_ROUNDS = 3  # a chained question ("find lakes, then check the biggest one") needs >1
QUICK_LAKES_ON_LIST = 6  # /list and /saved show this many inline-keyboard buttons

SYSTEM_PROMPT = (
    "You are JalNetra AI, an intelligent satellite-based water quality & "
    "contamination intelligence assistant for Maharashtra and India. You help "
    "officials monitor surface water bodies, lakes, and reservoirs. You explain "
    "indicators like Turbidity (NDTI), Chlorophyll-a (NDCI), Floating Algae (FAI), "
    "and Surface Extent (MNDWI) politely and scientifically. When asked about a "
    "specific lake's current status, nearby water bodies, active alerts, or to run "
    "a satellite scan, use the tools available to you rather than guessing -- the "
    "platform has live registry, indicator and alert data."
)

WELCOME_MESSAGE = (
    "🌊 Welcome to JalNetra Water Intelligence Bot! 🛰️\n\n"
    "I am your AI assistant for monitoring lakes, reservoirs, and rivers across "
    "India using Sentinel-2 satellite imagery. You can:\n"
    "• Ask me questions about water quality and indicators.\n"
    "• Discuss contamination, turbidity, or algal blooms.\n"
    "• Send your live location (or /nearby) to discover surrounding lakes!"
)

HELP_MESSAGE = (
    "*JalNetra Water Intelligence Bot — commands*\n\n"
    "/start — welcome message and what this bot can do\n"
    "/help — this list\n"
    "/status <lake> — a satellite snapshot + indicator card for one lake\n"
    "/list — tap a monitored lake for its status card\n"
    "/saved — your wishlist, same tap-for-status buttons\n"
    "/nearby — share your location to find water bodies around you\n"
    "/alerts — currently active high-priority alerts\n"
    "/lakes — top monitored lakes across Maharashtra\n\n"
    "Otherwise, just type your question in plain language — e.g. \"What does high "
    "NDCI mean in a lake?\", \"Check water quality of Khadakwasla Reservoir\", or "
    "\"Scan Bhatghar Reservoir\" — and I'll answer directly, no command needed."
)

_openai_client: AsyncOpenAI | None = None


def _openai() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set (checked the environment and .env).")
        _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


async def reply_markdown_safe(message: Message, text: str) -> None:
    """Lake, zone and district names routinely carry underscores/parentheses,
    which legacy Markdown reads as unmatched entity markers and rejects
    outright -- fall back to plain text rather than losing the reply."""
    try:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await message.reply_text(text)


def _fmt_indicator(indicators: dict[str, Any], key: str) -> tuple[str, str]:
    r = indicators.get(key) or {}
    v = r.get("value")
    value_str = f"{v:.2f}" if isinstance(v, int | float) else "—"
    return value_str, r.get("status", "—")


def format_status_caption(health: dict[str, Any]) -> str:
    """The rich card attached to a lake's satellite photo -- also used
    text-only when no image is available (GEE off, or no pass that day)."""
    ind = health.get("indicators", {})
    ndti_v, ndti_s = _fmt_indicator(ind, "turbidity")
    ndci_v, ndci_s = _fmt_indicator(ind, "chlorophyll")
    extent = health.get("water_extent_km2")
    extent_str = f"{extent:.2f} km²" if isinstance(extent, int | float) else "—"
    clear = health.get("cloud_clear_pct")
    date_str = health.get("observed_on") or "no scene yet"
    if isinstance(clear, int | float):
        date_str += f" ({clear:.0f}% cloud-free)"
    return (
        f"🛰️ *Sentinel-2 Observation: {health.get('name', 'Unknown lake')}*\n"
        f"📅 *Date:* {date_str}\n"
        f"📏 *Water Extent:* {extent_str}\n"
        f"🌊 *Turbidity (NDTI):* {ndti_v} ({ndti_s})\n"
        f"🌿 *Chlorophyll (NDCI):* {ndci_v} ({ndci_s})\n"
        f"⚠️ *Active Alerts:* {health.get('open_alerts', 0)}\n"
        f"🔗 *Dashboard:* {DASHBOARD_BASE_URL}/?wb={health.get('water_body_id', '')}"
    )


async def send_lake_status(bot: Bot, chat_id: int, health: dict[str, Any]) -> None:
    """The photo + data card for one lake -- used by /status, the /list and
    /saved inline buttons, and natural-language lake-health questions alike."""
    caption = format_status_caption(health)
    photo = None
    observed_on = health.get("observed_on")
    wb_id = health.get("water_body_id")
    if observed_on and wb_id:
        photo = await fetch_thumbnail_bytes(wb_id, observed_on)
    if photo:
        try:
            await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, parse_mode=ParseMode.MARKDOWN)
            return
        except BadRequest:
            log.warning("send_photo rejected the caption; falling back to text", extra={"chat_id": chat_id})
    try:
        await bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await bot.send_message(chat_id=chat_id, text=caption)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(WELCOME_MESSAGE)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await reply_markdown_safe(update.message, HELP_MESSAGE)


async def nearby_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton(text="📍 Share My Location", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        f"Tap the button below to share your location — I'll find water bodies "
        f"within {NEARBY_RADIUS_KM:.0f} km.",
        reply_markup=keyboard,
    )


async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    result = await get_active_alerts()
    if "error" in result:
        await update.message.reply_text(f"Could not load alerts: {result['error']}")
        return
    alerts = result.get("alerts", [])
    if not alerts:
        await update.message.reply_text(
            "✅ No active alerts right now — every monitored water body is within its "
            "normal baseline."
        )
        return
    lines = [f"🚨 *{result['total_open']} open alert(s)* — showing top {len(alerts)}:\n"]
    for a in alerts:
        trigger = a.get("trigger") or a["severity"].upper()
        lines.append(
            f"• *{a['water_body']}* ({a['zone']}) — {trigger}\n"
            f"  Severity: {a['severity'].upper()} · Priority {a['priority_score']:.0f}/100 · "
            f"{a['observed_on']}"
        )
    await reply_markdown_safe(update.message, "\n".join(lines))


async def lakes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    top = await list_top_lakes(10)
    if not top:
        await update.message.reply_text("No water bodies are registered yet.")
        return
    lines = ["🛰️ *Top monitored water bodies:*\n"]
    for i, wb in enumerate(top, 1):
        flag = " 🚨" if wb["open_alerts"] else ""
        lines.append(f"{i}. *{wb['name']}* — {wb['district']} · {wb['area_km2']:.1f} km²{flag}")
    await reply_markdown_safe(update.message, "\n".join(lines))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    name = " ".join(context.args) if context.args else ""
    if not name:
        await update.message.reply_text("Usage: /status <lake name> — e.g. /status Khadakwasla Reservoir")
        return
    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
    health = await get_lake_health(name)
    if "error" in health:
        await update.message.reply_text(health["error"])
        return
    await send_lake_status(context.bot, update.effective_chat.id, health)


def _lake_buttons(lakes: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Two per row, callback_data carries the id so a tap goes straight to
    get_lake_health_by_id -- no re-typing, no name-matching round trip."""
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(lakes), 2):
        pair = lakes[i : i + 2]
        rows.append([InlineKeyboardButton(wb["name"], callback_data=f"status:{wb['id']}") for wb in pair])
    rows.append(
        [
            InlineKeyboardButton("🔍 Search other", callback_data="search_hint"),
            InlineKeyboardButton("⭐ View wishlist", callback_data="wishlist"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    top = await list_top_lakes(QUICK_LAKES_ON_LIST)
    if not top:
        await update.message.reply_text("No water bodies are registered yet.")
        return
    await update.message.reply_text(
        "🛰️ Monitored water bodies — tap one for its status:", reply_markup=_lake_buttons(top)
    )


async def saved_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    items = await list_wishlist()
    if not items:
        await update.message.reply_text(
            "Your wishlist is empty. Save a lake from the dashboard, or use /list to browse."
        )
        return
    lakes = [
        {"id": it["water_body_id"], "name": it.get("custom_name") or it["water_body"]["name"]}
        for it in items[:QUICK_LAKES_ON_LIST]
    ]
    await update.message.reply_text("⭐ Your saved lakes — tap one for its status:", reply_markup=_lake_buttons(lakes))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or query.message is None:
        return
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data
    if data.startswith("status:"):
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
        health = await get_lake_health_by_id(data.removeprefix("status:"))
        if "error" in health:
            await context.bot.send_message(chat_id=chat_id, text=health["error"])
            return
        await send_lake_status(context.bot, chat_id, health)
    elif data == "search_hint":
        await context.bot.send_message(
            chat_id=chat_id, text="Type a lake name (e.g. \"Ambazari Lake\"), or use /nearby to search by location."
        )
    elif data == "wishlist":
        items = await list_wishlist()
        if not items:
            await context.bot.send_message(chat_id=chat_id, text="Your wishlist is empty.")
            return
        lakes = [
            {"id": it["water_body_id"], "name": it.get("custom_name") or it["water_body"]["name"]}
            for it in items[:QUICK_LAKES_ON_LIST]
        ]
        await context.bot.send_message(
            chat_id=chat_id, text="⭐ Your saved lakes:", reply_markup=_lake_buttons(lakes)
        )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.location is None:
        return
    lat, lon = message.location.latitude, message.location.longitude
    await message.reply_text(
        "🔍 Scanning satellite and mapping database for water bodies within "
        f"{NEARBY_RADIUS_KM:.0f} km of your location..."
    )
    result = await discover_lakes(lat, lon, NEARBY_RADIUS_KM)
    lakes = result.get("lakes", [])
    if not lakes:
        await message.reply_text(
            f"No significant water bodies found within {NEARBY_RADIUS_KM:.0f} km of this location."
        )
        return
    msg = f"📍 *Found {len(lakes)} Water Bodies near your location:*\n\n"
    for i, lake in enumerate(lakes, 1):
        area = lake.get("area_km2")
        area_str = f"{area:.2f}" if isinstance(area, int | float) else "N/A"
        msg += f"{i}. *{lake['name']}* — `{area_str} km²`\n"
    msg += f"\n💡 *Tip:* Ask me: 'Check water quality of {lakes[0]['name']}' to inspect it!"
    await reply_markdown_safe(message, msg)


async def _run_tool_call(name: str, raw_arguments: str) -> dict[str, Any]:
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        args = json.loads(raw_arguments or "{}")
        result: Any = await fn(**args)
        return result if isinstance(result, dict) else {"result": result}
    except Exception as exc:
        log.exception("tool call failed", extra={"tool": name})
        return {"error": str(exc)}


async def ask_gpt(question: str) -> tuple[str, list[dict[str, Any]]]:
    """Ask GPT-4o-mini, letting it call JalNetra's own tools (Layer 2) for
    anything that needs live data -- a specific lake, nearby water bodies,
    open alerts, or a satellite scan -- rather than guessing. Each turn is
    still stateless (no cross-message memory), matching Layer 1's scope.

    Returns (reply_text, lake_cards): any successful get_lake_health /
    get_lake_health_by_id results along the way, so a natural-language "check
    on Khadakwasla" gets the same photo card /status sends, not just text."""
    client = _openai()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    lake_cards: list[dict[str, Any]] = []
    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message
        if not msg.tool_calls:
            return msg.content or "I didn't get a response — please try rephrasing.", lake_cards
        messages.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            }
        )
        for tc in msg.tool_calls:
            result = await _run_tool_call(tc.function.name, tc.function.arguments)
            if tc.function.name == "get_lake_health" and "error" not in result:
                lake_cards.append(result)
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, default=str)}
            )
    return (
        "I looked into a few things but couldn't finish — please try asking again, more specifically.",
        lake_cards,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.text:
        return
    await message.chat.send_action(ChatAction.TYPING)
    try:
        reply, lake_cards = await ask_gpt(message.text)
    except Exception:
        log.exception("gpt reply failed", extra={"chat_id": message.chat_id})
        await message.reply_text(
            "Sorry, I couldn't reach the AI service just now — please try again in a moment."
        )
        return
    # The photo card first (if the question resolved to a specific lake),
    # then GPT's own conversational answer.
    for card in lake_cards[:1]:
        await send_lake_status(context.bot, message.chat_id, card)
    await reply_markdown_safe(message, reply)


def build_application() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set (checked the environment and .env).")
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("saved", saved_command))
    application.add_handler(CommandHandler("nearby", nearby_command))
    application.add_handler(CommandHandler("alerts", alerts_command))
    application.add_handler(CommandHandler("lakes", lakes_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.LOCATION, handle_location))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return application


def main() -> None:
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    application = build_application()
    log.info("JalNetra telegram bot starting (polling)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
