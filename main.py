# Replit-optimized version of your Telegram bot
# Keeps running using Flask + UptimeRobot ping

import requests
import logging
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from threading import Thread
from flask import Flask

# === WEB SERVER TO KEEP BOT ALIVE ===
app = Flask('')


@app.route('/')
def home():
    return "NodePrompt Bot is alive!"


def run_web():
    app.run(host='0.0.0.0', port=8080)


Thread(target=run_web).start()

# === CONFIG ===
TOKEN = "8461136691:AAEGSNcXVyFFjlpl6AboAGKl8uhEWi2w3yc"
COINGECKO_API = "https://api.coingecko.com/api/v3"
logging.basicConfig(level=logging.INFO)

symbol_to_id = {}
live_tasks = {}


# === Load top 50 coins ===
def load_top_coins():
    global symbol_to_id
    try:
        res = requests.get(f"{COINGECKO_API}/coins/markets",
                           params={
                               "vs_currency": "usd",
                               "order": "market_cap_desc",
                               "per_page": 50,
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
    msg = ("👋 *Welcome to NodePrompt Crypto Bot!*\n\n"
           "Commands you can use:\n"
           "• `/price btc` – Live price\n"
           "• `/top` – Top 10 coins\n"
           "• `/live btc` – 30s live stream\n"
           "• `/stop` – Stop streaming\n"
           "• `/help` – All commands")
    await update.message.reply_text(msg, parse_mode="Markdown")


# === /help ===
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# === /price ===
async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: /price [symbol] (e.g. /price btc)")
        return

    symbol = context.args[0].lower()
    coin_id = symbol_to_id.get(symbol)
    if not coin_id:
        await update.message.reply_text(f"❌ `{symbol}` not in top 50.",
                                        parse_mode="Markdown")
        return

    try:
        res = requests.get(f"{COINGECKO_API}/simple/price",
                           params={
                               "ids": coin_id,
                               "vs_currencies": "usd",
                               "include_market_cap": "true",
                               "include_24hr_vol": "true"
                           })
        res.raise_for_status()
        data = res.json().get(coin_id)

        if not data or 'usd' not in data:
            raise ValueError("Price data incomplete or unavailable.")

        price = data['usd']
        market_cap = data.get('usd_market_cap', 0)
        vol_24h = data.get('usd_24h_vol', 0)

        msg = (f"💸 *{symbol.upper()}*\n"
               f"• Price: `${price:,.6f}`\n"
               f"• Market Cap: `${market_cap:,.0f}`\n"
               f"• Volume 24h: `${vol_24h:,.0f}`")

        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Price fetch error: {e}")
        await update.message.reply_text("❌ Failed to fetch price data.")


# === /top ===
async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        res = requests.get(f"{COINGECKO_API}/coins/markets",
                           params={
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
        await update.message.reply_text(
            "⚠️ Usage: /live [symbol] (e.g. /live btc)")
        return

    user_id = update.effective_user.id
    symbol = context.args[0].lower()
    coin_id = symbol_to_id.get(symbol)
    if not coin_id:
        await update.message.reply_text(f"❌ `{symbol}` not found.",
                                        parse_mode="Markdown")
        return

    # Cancel previous if running
    if user_id in live_tasks:
        live_tasks[user_id].cancel()

    async def stream():
        try:
            for _ in range(10):
                res = requests.get(f"{COINGECKO_API}/simple/price",
                                   params={
                                       "ids": coin_id,
                                       "vs_currencies": "usd"
                                   })
                res.raise_for_status()
                price = res.json()[coin_id]["usd"]
                await update.message.reply_text(
                    f"💹 *{symbol.upper()}* → `${price:,.4f}`",
                    parse_mode="Markdown")
                await asyncio.sleep(30)
            await update.message.reply_text("✅ Live updates finished.")
        except asyncio.CancelledError:
            await update.message.reply_text("🛑 Live updates stopped.")
        finally:
            live_tasks.pop(user_id, None)

    task = asyncio.create_task(stream())
    live_tasks[user_id] = task
    await update.message.reply_text(
        f"📡 Streaming `{symbol}` every 30s. Type /stop to cancel.",
        parse_mode="Markdown")


# === /stop ===
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in live_tasks:
        live_tasks[user_id].cancel()
        await update.message.reply_text("🛑 Live updates stopped.")
    else:
        await update.message.reply_text("ℹ️ No live updates running.")


# === RUN BOT ===
async def startup(app):
    load_top_coins()
    logging.info(f"✅ Loaded symbols: {list(symbol_to_id.keys())[:10]}")


if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("live", live))
    app.add_handler(CommandHandler("stop", stop))
    app.run_polling()

