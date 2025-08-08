import os
import requests
import logging
import asyncio
import matplotlib.pyplot as plt
import datetime
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from threading import Thread
from flask import Flask
from io import BytesIO

# DEX SCREENER DEX SCREENER DEX SCREENER DEX SCREENER DEX SCREENER DEX SCREENER
 ── simple 20s cache so you don't spam the API ───────────────
_dex_cache = {}  # {address: (ts, data)}
CACHE_TTL = 20

# Solana base58 (no 0, O, I, l), 32–44 chars
SOL_CA_RE = re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')

def pick_best_pair(pairs):
    # prefer solana chain, highest liquidity
    sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
    if not sol_pairs:
        sol_pairs = pairs
    return max(sol_pairs, key=lambda p: (p.get("liquidity", {}).get("usd") or 0), default=None)

def get_dexscreener_for(address: str):
    # cache
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
    except:
        return "—"

async def handle_solana_ca(update, context):
    msg = update.effective_message
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    addresses = SOL_CA_RE.findall(text)
    if not addresses:
        return

    # Only process the first CA found in the message
    ca = addresses[0]

    data = get_dexscreener_for(ca)
    if not data or "pairs" not in data or not data["pairs"]:
        return await msg.reply_text(f"❌ No DEXScreener data for `{ca}`", parse_mode="Markdown")

    pair = pick_best_pair(data["pairs"])
    if not pair:
        return await msg.reply_text(f"❌ No active pairs for `{ca}` on DEXScreener", parse_mode="Markdown")

    base = pair.get("baseToken", {})
    quote = pair.get("quoteToken", {})
    price = pair.get("priceUsd")
    liq = pair.get("liquidity", {}).get("usd")
    fdv = pair.get("fdv")
    vol24 = pair.get("volume", {}).get("h24")
    pc = pair.get("priceChange", {}) or {}
    mcap = pair.get("marketCap")  # sometimes present
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

    await msg.reply_text(card, parse_mode="Markdown")

#END DEX SCREENER END DEX SCREENER END DEX SCREENER

# === WEB SERVER TO KEEP BOT ALIVE ===
app = Flask('')

@app.route('/')
def home():
    return "NodePrompt Bot is alive!"

def run_web():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run_web).start()

# === CONFIG ===
TOKEN = os.getenv("BOT_TOKEN")
COINGECKO_API = "https://api.coingecko.com/api/v3"
COINGECKO_HEADERS = {
    "x-cg-demo-api-key": "CG-EJg28u9CCZB4i7aQphoDJQKw"
}
logging.basicConfig(level=logging.INFO)


symbol_to_id = {}
live_tasks = {}

# === Load top 150 coins ===
def load_top_coins():
    global symbol_to_id
    try:
        res = requests.get(f"{COINGECKO_API}/coins/markets", params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 150,
            "page": 1,
            "sparkline": False
        })
        res.raise_for_status()
        data = res.json()
        symbol_to_id = {coin["symbol"].lower(): coin["id"] for coin in data}
    except Exception as e:
        logging.error(f"Failed to load top coins: {e}")

# === /start ===
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
    await update.message.reply_text(msg, parse_mode="Markdown")

# === /help ===
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

def fetch_chart_data(coin_id: str, days: int = 30):
    url = f"{COINGECKO_API}/coins/{coin_id}/market_chart"
    params = {
        "vs_currency": "usd",
        "days": days,
        "interval": "daily"
    }
    res = requests.get(url, params=params, headers=COINGECKO_HEADERS)
    if res.status_code != 200:
        return None
    return res.json()

# --- Generate chart image as PNG ---
def generate_chart_image(prices, coin_id: str):
    timestamps = [datetime.datetime.fromtimestamp(p[0] / 1000) for p in prices]
    price_values = [p[1] for p in prices]

    plt.figure(figsize=(10, 4))
    plt.plot(timestamps, price_values, label=f"{coin_id.upper()} USD", color="blue", linewidth=2)
    plt.title(f"{coin_id.upper()} Price Chart")
    plt.xlabel("Date")
    plt.ylabel("Price (USD)")
    plt.grid(True)
    plt.tight_layout()
    plt.legend()

    image_bytes = BytesIO()
    plt.savefig(image_bytes, format='png')
    image_bytes.seek(0)
    plt.close()
    return image_bytes

# === /chart ===
async def chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("⚠️ Usage: /chart [symbol] [days]\nExample: /chart btc 30")
        return

    symbol = context.args[0].lower()
    try:
        days = int(context.args[1]) if len(context.args) > 1 else 30
    except ValueError:
        await update.message.reply_text("⚠️ Invalid number of days. Example: /chart btc 30")
        return

    coin_id = symbol_to_id.get(symbol)
    if not coin_id:
        await update.message.reply_text(
            f"❌ {symbol} not found in top 150.\nTry using /top to see supported coins.",
            parse_mode="Markdown"
        )
        return

    await update.message.chat.send_action("upload_photo")

    # === Fetch chart data ===
    url = f"{COINGECKO_API}/coins/{coin_id}/market_chart"
    params = {
        "vs_currency": "usd",
        "days": days,
        "interval": "daily"
    }
    res = requests.get(url, params=params, headers=COINGECKO_HEADERS)

    try:
        res = requests.get(url, params=params, headers=COINGECKO_HEADERS)
        res.raise_for_status()
        data = res.json()
        if "prices" not in data:
            raise ValueError("No price data returned.")

        # === Generate chart ===
        prices = data["prices"]
        timestamps = [datetime.datetime.fromtimestamp(p[0] / 1000) for p in prices]
        values = [p[1] for p in prices]

        plt.style.use("dark_background")  # Apply dark background first
        plt.figure(figsize=(10, 4))
        plt.plot(timestamps, values, color="#00FF00", linewidth=2.5, label=f"{symbol.upper()} USD")  # Terminal green
        plt.title(f"{symbol.upper()} – Last {days} Days", fontsize=14, fontweight="bold", color="lime")
        plt.xlabel("Date", fontsize=12, color="white")
        plt.ylabel("Price (USD)", fontsize=12, color="white")
        plt.grid(True, linestyle=":", color="gray", alpha=0.4)
        plt.legend(facecolor="black", edgecolor="white", fontsize=10)

        # Format x-axis ticks to show "Jul 28", "Jul 29", etc.
        ax = plt.gca()
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))
        plt.xticks(rotation=45, color="white")
        plt.yticks(color="white")
        plt.tight_layout()

        image = BytesIO()
        plt.savefig(image, format="png")
        image.seek(0)
        plt.close()

        await update.message.reply_photo(photo=image, caption=f"📈 {symbol.upper()} – Last {days} Days")

    except Exception as e:
        await update.message.reply_text(f"❌ Failed to generate chart for {symbol}.\nError: {e}", parse_mode="Markdown")

# === /price ===
async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usage: /price [symbol] (e.g. /price btc)")
        return

    symbol = context.args[0].lower()
    coin_id = symbol_to_id.get(symbol)
    if not coin_id:
        await update.message.reply_text(f"❌ {symbol} not in top 150.", parse_mode="Markdown")
        return

    try:
        res = requests.get(f"{COINGECKO_API}/simple/price", params={
            "ids": coin_id,
            "vs_currencies": "usd",
            "include_market_cap": "true",
            "include_24hr_vol": "true"
        })
        res.raise_for_status()
        data = res.json()[coin_id]
        msg = (
            f"💸 *{symbol.upper()}*\n"
            f"• Price: ${data['usd']:,.6f}\n"
            f"• Market Cap: ${data['usd_market_cap']:,.0f}\n"
            f"• Volume 24h: ${data['usd_24h_vol']:,.0f}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text("❌ Failed to fetch price data.")
COINGECKO_API = "https://api.coingecko.com/api/v3"

# === /analyze ===
async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usage: /analyze [symbol] (e.g. /analyze btc)")
        return

    symbol = context.args[0].lower()
    coin_id = symbol_to_id.get(symbol)

    if not coin_id:
        await update.message.reply_text(f"❌ {symbol} not in top 150.", parse_mode="Markdown")
        return

    try:
        res = requests.get(f"{COINGECKO_API}/coins/{coin_id}")
        res.raise_for_status()
        data = res.json()

        name = data['name']
        price = data['market_data']['current_price']['usd']
        market_cap = data['market_data']['market_cap']['usd']
        change_24h = data['market_data']['price_change_percentage_24h']

        # Generate simple AI-style insight
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
            f"*📊 {name} Market Analysis:*\n\n\n"
            f"• *Price:* ${price:,.2f}\n\n"
            f"• *Market Cap:* ${market_cap:,.0f}\n\n"
            f"• *24h Change:* {change_24h:.2f}%\n\n"
            f"• *AI Insight:* {trend}\n\n"
            f"{recommendation}"
        )

        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ Error fetching analysis.")


# === /top ===
async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        res = requests.get(f"{COINGECKO_API}/coins/markets", params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 10,
            "page": 1,
            "sparkline": False
        })
        res.raise_for_status()
        data = res.json()
        msg = "*🌐 Top 10 Cryptos:*\n\n"
        for i, coin in enumerate(data, 1):
            msg += f"{i}. *{coin['symbol'].upper()}* – ${coin['current_price']:,.2f} ({coin['price_change_percentage_24h']:.2f}%)\n"
        await update.message.reply_text(msg, parse_mode="Markdown")
    except:
        await update.message.reply_text("❌ Failed to load top coins.")

# === /live ===
async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usage: /live [symbol] (e.g. /live btc)")
        return

    user_id = update.effective_user.id
    symbol = context.args[0].lower()
    coin_id = symbol_to_id.get(symbol)
    if not coin_id:
        await update.message.reply_text(f"❌ {symbol} not found.", parse_mode="Markdown")
        return

    # Cancel previous if running
    if user_id in live_tasks:
        live_tasks[user_id].cancel()

    async def stream():
        try:
            for _ in range(10):
                res = requests.get(f"{COINGECKO_API}/simple/price", params={"ids": coin_id, "vs_currencies": "usd"})
                res.raise_for_status()
                price = res.json()[coin_id]["usd"]
                await update.message.reply_text(f"💹 *{symbol.upper()}* → ${price:,.4f}", parse_mode="Markdown")
                await asyncio.sleep(30)
            await update.message.reply_text("✅ Live updates finished.")
        except asyncio.CancelledError:
            await update.message.reply_text("🛑 Live updates stopped.")
        finally:
            live_tasks.pop(user_id, None)

    task = asyncio.create_task(stream())
    live_tasks[user_id] = task
    await update.message.reply_text(f"📡 Streaming {symbol} every 30s. Type /stop to cancel.", parse_mode="Markdown")

# === /stop ===
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in live_tasks:
        live_tasks[user_id].cancel()
        await update.message.reply_text("🛑 Live updates stopped.")
    else:
        await update.message.reply_text("ℹ️ No live updates running.")

# === RUN BOT ===
if __name__ == "__main__":
    load_top_coins()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("live", live))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("chart", chart))

    app.run_polling()
