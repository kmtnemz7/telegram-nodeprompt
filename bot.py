import os
import re
import time
import logging
import asyncio
import datetime
import requests
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    MessageHandler, filters
)
from threading import Thread
from flask import Flask
from io import BytesIO
import sys

# ───────────────────────────── Logging ─────────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("NodePromptBot")

# ─────────────────────────── Flask keepalive ───────────────────────
app_web = Flask(__name__)

@app_web.route("/")
def home():
    return "NodePrompt Bot is alive!"

def run_web():
    app_web.run(host="0.0.0.0", port=8080)

Thread(target=run_web, daemon=True).start()

# ─────────────────────────── Config / Secrets ──────────────────────
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    log.error("BOT_TOKEN env var not set. Exiting.")
    sys.exit(1)

COINGECKO_API = "https://api.coingecko.com/api/v3"
COINGECKO_HEADERS = {
    "x-cg-demo-api-key": "CG-EJg28u9CCZB4i7aQphoDJQKw"
}

symbol_to_id = {}
live_tasks = {}

# ───────────────────────── DEXScreener helpers ─────────────────────
# simple 20s cache so you don't spam the API
_dex_cache = {}  # {address: (ts, data)}
CACHE_TTL = 20

# Solana base58 (no 0, O, I, l), 32–44 chars
SOL_CA_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

def pick_best_pair(pairs):
    # prefer solana chain, highest liquidity
    sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
    if not sol_pairs:
        sol_pairs = pairs
    return max(sol_pairs, key=lambda p: (p.get("liquidity", {}).get("usd") or 0), default=None)

def get_dexscreener_for(address: str):
    now = time.time()
    hit = _dex_cache.get(address)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]

    url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        return None
    data = r.json()
    _dex_cache[address] = (now, data)
    return data

def fmt_pct(x):
    return f"{x:+.2f}%" if isinstance(x, (int, float)) else "—"

def fmt_usd(x, digs=0):
    try:
        return f"${float(x):,.{digs}f}"
    except Exception:
        return "—"

# ─────────────────────────── Utilities ─────────────────────────────
async def say(update: Update, text: str, **kw):
    return await update.effective_chat.send_message(text, **kw)

async def send_photo(update: Update, photo, **kw):
    return await update.effective_chat.send_photo(photo=photo, **kw)

# ─────────────────────── Load top 150 coins (CG) ───────────────────
def load_top_coins():
    global symbol_to_id
    try:
        res = requests.get(
            f"{COINGECKO_API}/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 150,
                "page": 1,
                "sparkline": False,
            },
            headers=COINGECKO_HEADERS,
            timeout=15,
        )
        res.raise_for_status()
        data = res.json()
        symbol_to_id = {coin["symbol"].lower(): coin["id"] for coin in data}
        log.info("Loaded %d coin symbols from CoinGecko", len(symbol_to_id))
    except Exception as e:
        log.error("Failed to load top coins: %s", e)

# ───────────────────────────── Commands ────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Welcome to NodePrompt Crypto Bot!*\n\n"
        "Commands you can use:\n"
        "• /price (crypto) – Live price\n"
        "• /live (crypto) – 30s live stream\n"
        "• /analyze (crypto) – AI analysis\n"
        "• /top – Top 10 coins\n"
        "• /stop – Stop streaming\n"
        "• /help – All commands"
    )
    await say(update, msg, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

def fetch_chart_data(coin_id: str, days: int = 30):
    url = f"{COINGECKO_API}/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": days, "interval": "daily"}
    res = requests.get(url, params=params, headers=COINGECKO_HEADERS, timeout=20)
    if res.status_code != 200:
        return None
    return res.json()

# Generate chart image as PNG
def generate_chart_image(prices, symbol: str):
    timestamps = [datetime.datetime.fromtimestamp(p[0] / 1000) for p in prices]
    values = [p[1] for p in prices]

    plt.style.use("dark_background")
    plt.figure(figsize=(10, 4))
    plt.plot(timestamps, values, linewidth=2.5, label=f"{symbol.upper()} USD")
    plt.title(f"{symbol.upper()} – Last {len(values)} points", fontsize=14, fontweight="bold")
    plt.xlabel("Date", fontsize=12)
    plt.ylabel("Price (USD)", fontsize=12)
    plt.grid(True, linestyle=":", alpha=0.4)
    plt.legend(fontsize=10)

    ax = plt.gca()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    plt.xticks(rotation=45)
    plt.tight_layout()

    image_bytes = BytesIO()
    plt.savefig(image_bytes, format="png")
    image_bytes.seek(0)
    plt.close()
    return image_bytes

async def chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        return await say(update, "⚠️ Usage: /chart [symbol] [days]\nExample: /chart btc 30")

    symbol = context.args[0].lower()
    try:
        days = int(context.args[1]) if len(context.args) > 1 else 30
    except ValueError:
        return await say(update, "⚠️ Invalid number of days. Example: /chart btc 30")

    coin_id = symbol_to_id.get(symbol)
    if not coin_id:
        return await say(
            update,
            f"❌ {symbol} not found in top 150.\nTry using /top to see supported coins.",
            parse_mode="Markdown",
        )

    try:
        data = fetch_chart_data(coin_id, days=days)
        if not data or "prices" not in data:
            raise ValueError("No price data returned.")

        image = generate_chart_image(data["prices"], symbol)
        await send_photo(update, image, caption=f"📈 {symbol.upper()} – Last {days} Days")
    except Exception as e:
        await say(update, f"❌ Failed to generate chart for {symbol}.\nError: {e}", parse_mode="Markdown")

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await say(update, "⚠️ Usage: /price [symbol] (e.g. /price btc)")

    symbol = context.args[0].lower()
    coin_id = symbol_to_id.get(symbol)
    if not coin_id:
        return await say(update, f"❌ {symbol} not in top 150.", parse_mode="Markdown")

    try:
        res = requests.get(
            f"{COINGECKO_API}/simple/price",
            params={
                "ids": coin_id,
                "vs_currencies": "usd",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
            },
            headers=COINGECKO_HEADERS,
            timeout=15,
        )
        res.raise_for_status()
        data = res.json()[coin_id]
        msg = (
            f"💸 *{symbol.upper()}*\n"
            f"• Price: ${data['usd']:,.6f}\n"
            f"• Market Cap: ${data['usd_market_cap']:,.0f}\n"
            f"• Volume 24h: ${data['usd_24h_vol']:,.0f}"
        )
        await say(update, msg, parse_mode="Markdown")
    except Exception:
        await say(update, "❌ Failed to fetch price data.")

async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await say(update, "⚠️ Usage: /analyze [symbol] (e.g. /analyze btc)")

    symbol = context.args[0].lower()
    coin_id = symbol_to_id.get(symbol)
    if not coin_id:
        return await say(update, f"❌ {symbol} not in top 150.", parse_mode="Markdown")

    try:
        res = requests.get(f"{COINGECKO_API}/coins/{coin_id}", headers=COINGECKO_HEADERS, timeout=20)
        res.raise_for_status()
        data = res.json()

        name = data["name"]
        price_usd = data["market_data"]["current_price"]["usd"]
        market_cap = data["market_data"]["market_cap"]["usd"]
        change_24h = data["market_data"]["price_change_percentage_24h"]

        if change_24h > 15:
            trend = "🚀 *Exploding bullish momentum*"
            recommendation = "📢 *Recommendation:* High momentum detected ➡️ _BUY NOW!_"
        elif change_24h > 10:
            trend = "📈 *Strong uptrend forming*"
            recommendation = "✅ *Recommendation:* Market favorable ➡️ _Consider buying_"
        elif change_24h > 5:
            trend = "🟢 *Mild bullish pressure*"
            recommendation = "📊 *Recommendation:* Potential for upside ➡️ _Buy with caution_"
        elif change_24h > 0:
            trend = "🟡 *Consolidation*"
            recommendation = "🕒 *Recommendation:* Wait for confirmation on movement ➡️ _Monitor closely_"
        elif change_24h > -5:
            trend = "⚪ *Mild bearish pressure*"
            recommendation = "🤔 *Recommendation:* Slight downtrend ➡️ _Hold / Observe_"
        elif change_24h > -10:
            trend = "🟠 *Strong downtrend forming*"
            recommendation = "⚠️ *Recommendation:* Losing momentum ➡️ _Consider exiting_"
        elif change_24h > -15:
            trend = "🔻 *Strong downward momentum*"
            recommendation = "❌ *Recommendation:* Bearish wave ➡️ _SELL NOW_"
        else:
            trend = "💀 *Freefall – heavy selling*"
            recommendation = "🚨 *Recommendation:* Panic selling detected ➡️ _EXIT IMMEDIATELY!_"

        msg = (
            f"*📊 {name} Market Analysis:*\n\n"
            f"• *Price:* ${price_usd:,.2f}\n"
            f"• *Market Cap:* ${market_cap:,.0f}\n"
            f"• *24h Change:* {change_24h:.2f}%\n\n"
            f"• *AI Insight:* {trend}\n\n"
            f"{recommendation}"
        )
        await say(update, msg, parse_mode="Markdown")
    except Exception:
        await say(update, "❌ Error fetching analysis.")

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        res = requests.get(
            f"{COINGECKO_API}/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 10,
                "page": 1,
                "sparkline": False,
            },
            headers=COINGECKO_HEADERS,
            timeout=15,
        )
        res.raise_for_status()
        data = res.json()
        msg = "*🌐 Top 10 Cryptos:*\n\n"
        for i, coin in enumerate(data, 1):
            msg += f"{i}. *{coin['symbol'].upper()}* – ${coin['current_price']:,.2f} ({coin['price_change_percentage_24h']:.2f}%)\n"
        await say(update, msg, parse_mode="Markdown")
    except Exception:
        await say(update, "❌ Failed to load top coins.")

async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await say(update, "⚠️ Usage: /live [symbol] (e.g. /live btc)")

    user_id = update.effective_user.id
    symbol = context.args[0].lower()
    coin_id = symbol_to_id.get(symbol)
    if not coin_id:
        return await say(update, f"❌ {symbol} not found.", parse_mode="Markdown")

    if user_id in live_tasks:
        live_tasks[user_id].cancel()

    async def stream():
        try:
            for _ in range(10):
                res = requests.get(
                    f"{COINGECKO_API}/simple/price",
                    params={"ids": coin_id, "vs_currencies": "usd"},
                    headers=COINGECKO_HEADERS,
                    timeout=10,
                )
                res.raise_for_status()
                price = res.json()[coin_id]["usd"]
                await say(update, f"💹 *{symbol.upper()}* → ${price:,.4f}", parse_mode="Markdown")
                await asyncio.sleep(30)
            await say(update, "✅ Live updates finished.")
        except asyncio.CancelledError:
            await say(update, "🛑 Live updates stopped.")
        finally:
            live_tasks.pop(user_id, None)

    task = asyncio.create_task(stream())
    live_tasks[user_id] = task
    await say(update, f"📡 Streaming {symbol} every 30s. Type /stop to cancel.", parse_mode="Markdown")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in live_tasks:
        live_tasks[user_id].cancel()
        await say(update, "🛑 Live updates stopped.")
    else:
        await say(update, "ℹ️ No live updates running.")

# ─────────────── Group listener: Solana CA → DEXScreener ───────────
async def handle_solana_ca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = (getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip()
    if not text:
        return

    addresses = SOL_CA_RE.findall(text)
    if not addresses:
        return

    ca = addresses[0]
    data = get_dexscreener_for(ca)
    if not data or "pairs" not in data or not data["pairs"]:
        return await say(update, f"❌ No DEXScreener data for `{ca}`", parse_mode="Markdown")

    pair = pick_best_pair(data["pairs"])
    if not pair:
        return await say(update, f"❌ No active pairs for `{ca}` on DEXScreener", parse_mode="Markdown")

    base = pair.get("baseToken", {})
    price = pair.get("priceUsd")
    liq = pair.get("liquidity", {}).get("usd")
    fdv = pair.get("fdv")
    vol24 = pair.get("volume", {}).get("h24")
    pc = pair.get("priceChange", {}) or {}
    dex = pair.get("dexId")
    chain = pair.get("chainId")
    url = pair.get("url") or pair.get("pairUrl")

    name = base.get("name") or base.get("symbol") or "Token"
    symbol = base.get("symbol") or ""

    card = (
        f"💊 *{name}* ({symbol})\n"
        f"╰─🧬 *CA* → `{ca}`\n"
        f"   │\n"
        f"   💵 *Price*      → {fmt_usd(price, 8)}\n"
        f"   📈 *FDV*        → {fmt_usd(fdv)}\n"
        f"   💧 *Liquidity*  → {fmt_usd(liq)}\n"
        f"   🔊 *Vol 24h*    → {fmt_usd(vol24)}\n"
        f"   🧭 *Change*     → 1h {fmt_pct(pc.get('h1'))} | 6h {fmt_pct(pc.get('h6'))} | 24h {fmt_pct(pc.get('h24'))}\n"
        f"   │\n"
        f"   ⚖️ *DEX*        → {dex or '—'}\n"
        f"   🌐 *Chain*      → {chain or '—'}\n"
        f"   🔗 *Link*       → {url or '—'}"
    )
    await say(update, card, parse_mode="Markdown")

# ───────────────────────── Error handler ───────────────────────────
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)

# ───────────────────────────── Runner ──────────────────────────────
if __name__ == "__main__":
    load_top_coins()

    application = ApplicationBuilder().token(TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("price", price))
    application.add_handler(CommandHandler("top", top))
    application.add_handler(CommandHandler("live", live))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("analyze", analyze))
    application.add_handler(CommandHandler("chart", chart))

    # Group listener for Solana contract addresses
    application.add_handler(MessageHandler(filters.TEXT | filters.Caption, handle_solana_ca))

    # Errors
    application.add_error_handler(on_error)

    application.run_polling()
