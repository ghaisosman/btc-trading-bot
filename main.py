import os
import time
import pytz
import pandas as pd
import pandas_ta as ta
import requests
from datetime import datetime
from flask import Flask, jsonify, render_template_string
from threading import Thread
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET

# === CONFIG ===
USE_TESTNET = True
SYMBOL = "BTCUSDT"
LEVERAGE = 90
MAX_TRADE_USD = 20
TP_PERCENT = 0.05
SL_PERCENT = 0.05
TIMEZONE = "Asia/Dubai"
KEEP_ALIVE_URL = "https://ghaisosman.btc-trading-bot-1.repl.co"

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

client = Client(API_KEY, API_SECRET, testnet=USE_TESTNET)
client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
print("Connected to Binance Testnet")

# === GLOBAL STATUS FOR WEB ===
status = {
    "bot_status": "Starting...",
    "last_checked": None,
    "last_price": None,
    "last_signal": None,
    "open_trades": []
}

# === TELEGRAM ===
def send_telegram(message):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})

# === GET CANDLES ===
def get_klines(interval="5m", limit=100):
    klines = client.futures_klines(symbol=SYMBOL, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "tb_base", "tb_quote", "ignore"
    ])
    df["close"] = df["close"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    return df

# === INDICATORS ===
def apply_indicators(df):
    macd = ta.macd(df["close"])
    rsi = ta.rsi(df["close"], length=20)
    df["MACD"] = macd["MACD_12_26_9"]
    df["MACD_signal"] = macd["MACDs_12_26_9"]
    df["RSI20"] = rsi
    return df

# === SIGNAL LOGIC ===
def check_signal(df):
    last, prev = df.iloc[-1], df.iloc[-2]
    macd_cross_up = prev["MACD"] < prev["MACD_signal"] and last["MACD"] > last["MACD_signal"]
    macd_cross_down = prev["MACD"] > prev["MACD_signal"] and last["MACD"] < last["MACD_signal"]
    momentum_up = (last["MACD"] - last["MACD_signal"]) > 10
    momentum_down = (last["MACD_signal"] - last["MACD"]) > 10

    if macd_cross_up and last["RSI20"] < 70 and momentum_up:
        return "BUY"
    elif macd_cross_down and last["RSI20"] > 30 and momentum_down:
        return "SELL"
    return None

# === TRADE ===
def get_trade_quantity(price):
    return round((MAX_TRADE_USD * LEVERAGE) / price, 5)

def place_market_order(signal, trade_num):
    side = SIDE_BUY if signal == "BUY" else SIDE_SELL
    price = float(client.futures_mark_price(symbol=SYMBOL)["markPrice"])
    qty = get_trade_quantity(price)
    order = client.futures_create_order(symbol=SYMBOL, side=side, type=ORDER_TYPE_MARKET, quantity=qty)
    entry = float(order['fills'][0]['price'])

    tp = round(entry * (1 + TP_PERCENT) if signal == "BUY" else entry * (1 - TP_PERCENT), 2)
    sl = round(entry * (1 - SL_PERCENT) if signal == "BUY" else entry * (1 + SL_PERCENT), 2)

    send_telegram(f"{signal} order placed: Entry={entry}, TP={tp}, SL={sl}")
    return {"side": signal, "entry": entry, "qty": qty, "tp": tp, "sl": sl}

def monitor_open_trades(trades):
    updated = []
    price = float(client.futures_mark_price(symbol=SYMBOL)["markPrice"])
    balance = next(b for b in client.futures_account_balance() if b["asset"] == "USDT")["balance"]

    for t in trades:
        pnl = (price - t["entry"]) / t["entry"] if t["side"] == "BUY" else (t["entry"] - price) / t["entry"]
        if pnl >= TP_PERCENT:
            close_qty = round(t["qty"] * 0.75, 5)
            side = SIDE_SELL if t["side"] == "BUY" else SIDE_BUY
            client.futures_create_order(symbol=SYMBOL, side=side, type=ORDER_TYPE_MARKET, quantity=close_qty)
            send_telegram(f"TP hit: {t['side']} at {t['entry']} -> TP {t['tp']}")
            t["qty"] -= close_qty
            if t["qty"] > 0: updated.append(t)
        elif pnl <= -SL_PERCENT:
            side = SIDE_SELL if t["side"] == "BUY" else SIDE_BUY
            client.futures_create_order(symbol=SYMBOL, side=side, type=ORDER_TYPE_MARKET, quantity=t["qty"])
            send_telegram(f"SL hit: {t['side']} at {t['entry']} -> SL {t['sl']}")
        else:
            updated.append(t)
    return updated

# === BOT LOOP ===
def run_bot():
    trades = []
    count = 1
    while True:
        try:
            now = datetime.now(pytz.timezone(TIMEZONE))
            print(f"{now} - Checking market...")
            df = apply_indicators(get_klines())
            signal = check_signal(df)
            current_price = df["close"].iloc[-1]

            status.update({
                "bot_status": "Running",
                "last_checked": now.strftime("%Y-%m-%d %H:%M:%S"),
                "last_price": current_price,
                "last_signal": signal,
                "open_trades": trades
            })

            # Replit keep-alive
            try:
                requests.get(KEEP_ALIVE_URL)
            except: pass

            if signal:
                trade = place_market_order(signal, count)
                trades.append(trade)
                count += 1
            trades = monitor_open_trades(trades)
            time.sleep(60)
        except Exception as e:
            print("Error:", str(e))
            send_telegram(f"Bot crashed: {str(e)}")

# === FLASK SERVER ===
app = Flask(__name__)

@app.route('/')
def dashboard():
    return render_template_string(f"""
    <h2>BTC Bot Dashboard</h2>
    <p>Status: {status['bot_status']}</p>
    <p>Last Checked: {status['last_checked']}</p>
    <p>Price: {status['last_price']}</p>
    <p>Signal: {status['last_signal']}</p>
    <p>Open Trades: {len(status['open_trades'])}</p>
    <ul>
        {''.join(f"<li>{t['side']} @ {t['entry']} → TP {t['tp']}, SL {t['sl']}</li>" for t in status['open_trades'])}
    </ul>
    """)

@app.route('/json')
def json_status():
    return jsonify(status)

def run_server():
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)

# === RUN ===
Thread(target=run_server).start()
run_bot()
