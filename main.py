import os
import math
import time
import requests
import pandas as pd

URL = "https://api.hyperliquid.xyz/info"
COIN = "@272"
ASSET_NAME = "ZCASH"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEFRAMES = ["1d", "4h", "1h"]
INTERVAL_MS = {
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}

MAX_CANDLES_PER_REQUEST = 4999
WAVE_PERIOD = 34
WAVE_LOOKBACK = 8

HTF_FLAT = 0.9
HTF_STRONG = 2.0
LTF_FLAT = 0.40
LTF_STRONG = 1.50

THRESH = {
    "1h": {"flat": LTF_FLAT, "strong": LTF_STRONG},
    "4h": {"flat": HTF_FLAT, "strong": HTF_STRONG},
    "1d": {"flat": HTF_FLAT, "strong": HTF_STRONG},
}

ALLOW_WEAK_TREND = False
ONLY_BUY = True
BLOCK_IF_DAILY_FLAT = False
BLOCK_IF_DAILY_AGAINST = True
LEVERAGE = 5.0
MAINT_MARGIN = 0.01


def notify(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram não configurado")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"Falha Telegram: {e}")


def fetch_candle_page(interval, start_time, end_time):
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": COIN,
            "interval": interval,
            "startTime": int(start_time),
            "endTime": int(end_time),
        },
    }
    r = requests.post(URL, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise Exception(f"Resposta inesperada: {data}")
    return data


def get_candles(interval, start_time, end_time=None):
    if end_time is None:
        end_time = int(time.time() * 1000)

    rows = []
    cursor = int(start_time)

    for _ in range(50):
        if cursor >= end_time:
            break

        page_end = min(end_time, cursor + MAX_CANDLES_PER_REQUEST * INTERVAL_MS[interval])
        page = fetch_candle_page(interval, cursor, page_end)

        if not page:
            if page_end >= end_time:
                break
            cursor = page_end + 1
            continue

        rows.extend(page)
        last_close = int(page[-1].get("T", page[-1]["t"]))
        nxt = last_close + 1
        if nxt <= cursor:
            break
        cursor = nxt

        if len(page) < MAX_CANDLES_PER_REQUEST and page_end >= end_time:
            break
        time.sleep(0.15)

    if not rows:
        raise Exception("Nenhum candle retornado")

    df = pd.DataFrame(rows)
    df["open"] = pd.to_numeric(df["o"])
    df["high"] = pd.to_numeric(df["h"])
    df["low"] = pd.to_numeric(df["l"])
    df["close"] = pd.to_numeric(df["c"])
    df["datetime"] = pd.to_datetime(pd.to_numeric(df["t"]), unit="ms", utc=True)
    df = df.sort_values("t").drop_duplicates(subset=["t"])

    current_price = float(df.iloc[-1]["close"])
    current_time = df.iloc[-1]["datetime"].strftime("%Y-%m-%d %H:%M UTC")

    if len(df) > 1:
        df = df.iloc[:-1].copy()
    df.reset_index(drop=True, inplace=True)
    return df, current_price, current_time


def add_wave(df):
    df["W_HIGH"] = df["high"].ewm(span=WAVE_PERIOD, adjust=False).mean()
    df["W_MID"] = df["close"].ewm(span=WAVE_PERIOD, adjust=False).mean()
    df["W_LOW"] = df["low"].ewm(span=WAVE_PERIOD, adjust=False).mean()
    mid = df["W_MID"]
    df["SLOPE"] = ((mid / mid.shift(WAVE_LOOKBACK)) - 1) * 100
    df["ANGLE"] = df["SLOPE"].apply(
        lambda x: 0.0 if pd.isna(x) else math.degrees(math.atan(x / WAVE_LOOKBACK))
    )
    return df


def is_htf(tf):
    return INTERVAL_MS[tf] >= 4 * 60 * 60 * 1000


def cuts_for(tf):
    if is_htf(tf):
        return HTF_FLAT, HTF_STRONG
    return LTF_FLAT, LTF_STRONG


def clock_regime(slope, tf):
    if pd.isna(slope):
        return "NONE", "⚪ Sem dados"

    flat, strong = cuts_for(tf)

    if abs(slope) < flat:
        return "FLAT", "⚪ 3h Horizontal"
    if slope >= strong:
        return "UP_STRONG", "🟢 12-2 Alta forte"
    if slope > 0:
        return "UP_WEAK", "🟡 2-4 Alta fraca"
    if slope <= -strong:
        return "DOWN_STRONG", "🔴 4-6 Baixa forte"
    return "DOWN_WEAK", "🟠 Baixa fraca"


def price_vs_wave(row):
    if row["close"] > row["W_HIGH"]:
        return "ABOVE", "Acima da onda"
    if row["close"] < row["W_LOW"]:
        return "BELOW", "Abaixo da onda"
    return "INSIDE", "Dentro da onda"


def bullish(regime):
    return regime == "UP_STRONG" if not ALLOW_WEAK_TREND else regime in ("UP_STRONG", "UP_WEAK")


def bearish(regime):
    return regime == "DOWN_STRONG" if not ALLOW_WEAK_TREND else regime in ("DOWN_STRONG", "DOWN_WEAK")


def analyze(df, tf):
    df = add_wave(df)
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last

    regime, label = clock_regime(last["SLOPE"], tf)
    pos, pos_label = price_vs_wave(last)
    prev_pos, _ = price_vs_wave(prev)
    flat, strong = cuts_for(tf)
    grupo = "4h+" if is_htf(tf) else "LTF"

    pullback_buy = (
        prev_pos == "ABOVE"
        and pos == "INSIDE"
        and last["close"] > last["W_MID"]
    )
    pullback_sell = (
        prev_pos == "BELOW"
        and pos == "INSIDE"
        and last["close"] < last["W_MID"]
    )

    if regime == "FLAT":
        bias = "WAIT"
        setup = "Onda horizontal"
    elif bullish(regime) and pullback_buy:
        bias = "BUY"
        setup = "Pullback ABOVE → INSIDE"
    elif (not ONLY_BUY) and bearish(regime) and pullback_sell:
        bias = "SELL"
        setup = "Pullback BELOW → INSIDE"
    elif bullish(regime) and pos == "ABOVE":
        bias = "WAIT"
        setup = "Alta forte, sem pullback"
    elif bullish(regime) and pos == "INSIDE":
        bias = "WAIT"
        setup = "Dentro da onda, sem origem ABOVE"
    else:
        bias = "WAIT"
        setup = "Sem setup"

    stop = float(min(last["W_LOW"], last["low"]))
    return {
        "candles": len(df),
        "first": df["datetime"].iloc[0].strftime("%Y-%m-%d %H:%M UTC"),
        "last": df["datetime"].iloc[-1].strftime("%Y-%m-%d %H:%M UTC"),
        "close": round(float(last["close"]), 6),
        "w_high": round(float(last["W_HIGH"]), 6),
        "w_mid": round(float(last["W_MID"]), 6),
        "w_low": round(float(last["W_LOW"]), 6),
        "slope": round(float(last["SLOPE"] or 0), 3),
        "angle": round(float(last["ANGLE"] or 0), 2),
        "regime": regime,
        "label": label,
        "pos": pos,
        "pos_label": pos_label,
        "bias": bias,
        "setup": setup,
        "pullback_buy": bool(pullback_buy),
        "pullback_sell": bool(pullback_sell),
        "stop": round(stop, 6),
        "grupo": grupo,
        "flat": flat,
        "strong": strong,
    }


def discover_1d_start():
    end_time = int(time.time() * 1000)
    start_time = end_time - 20 * 365 * 24 * 60 * 60 * 1000
    df, price, ts = get_candles("1d", start_time, end_time)
    first_ts = int(df.iloc[0]["t"])
    print(f"Historico 1d desde: {df.iloc[0]['datetime'].strftime('%Y-%m-%d %H:%M UTC')}")
    return first_ts, df, price, ts


def run_scan():
    print(f"\n===== {ASSET_NAME} | RELOGIO RAGHEE =====\n")
    results = {}

    try:
        history_start, df_1d, price_1d, time_1d = discover_1d_start()
        data = analyze(df_1d, "1d")
        data["current_price"] = price_1d
        data["current_time"] = time_1d
        results["1d"] = data
        del df_1d
    except Exception as e:
        results["1d"] = {"erro": str(e)}
        history_start = int(time.time() * 1000) - 1500 * 24 * 60 * 60 * 1000

    for tf in ("4h", "1h"):
        try:
            df, price, ts = get_candles(tf, history_start)
            data = analyze(df, tf)
            data["current_price"] = price
            data["current_time"] = ts
            results[tf] = data
            del df
        except Exception as e:
            results[tf] = {"erro": str(e)}

    blocos = []
    preco = None
    horario = None

    for tf in TIMEFRAMES:
        d = results[tf]
        if "erro" in d:
            print(f"{tf} | ERRO: {d['erro']}")
            blocos.append(f"\n{tf}: ERRO {d['erro']}")
            continue

        if preco is None:
            preco = d["current_price"]
            horario = d["current_time"]

        print(f"\n{tf} | grupo {d['grupo']} | flat={d['flat']} strong={d['strong']}")
        print(f"candles {d['candles']} | {d['first']} -> {d['last']}")
        print(f"{d['current_price']} | {d['current_time']}")
        print(f"{d['label']} | {d['pos_label']}")
        print(f"Onda H/M/L: {d['w_high']} / {d['w_mid']} / {d['w_low']}")
        print(f"slope={d['slope']}% | angulo={d['angle']}")
        print(f"Setup: {d['setup']} | bias={d['bias']}")

        blocos.append(
            f"\n<b>{tf}</b> ({d['grupo']} {d['flat']}/{d['strong']})\n"
            f"{d['label']}\n"
            f"{d['pos_label']}\n"
            f"slope={d['slope']}% | angulo={d['angle']}\n"
            f"{d['setup']}"
        )

    d1 = results.get("1d", {})
    h4 = results.get("4h", {})
    h1 = results.get("1h", {})

    d1_regime = d1.get("regime", "NONE")
    h4_regime = h4.get("regime", "NONE")
    h1_ok = h1.get("pullback_buy", False)
    h1_sell = h1.get("pullback_sell", False)

    blocked = False
    if BLOCK_IF_DAILY_FLAT and d1_regime == "FLAT":
        blocked = True
    if BLOCK_IF_DAILY_AGAINST and bearish(d1_regime) and bullish(h4_regime):
        blocked = True

    if blocked:
        sinal = "AGUARDAR — 1D bloqueando"
    elif bullish(h4_regime) and h1_ok:
        stop = h1.get("stop")
        liq = preco * (1 - 1 / LEVERAGE + MAINT_MARGIN) if preco else 0
        risco = ((preco - stop) / preco * 100) if preco and stop else 0
        sinal = (
            "COMPRA — 4h UP_STRONG + pullback 1h\n"
            f"Stop 1h: {stop}\n"
            f"Trail: W_LOW 1h = {h1.get('w_low')}\n"
            f"Liq. ~{LEVERAGE:.0f}x: {liq:.6f}\n"
            f"Risco ate o stop: {risco:.2f}%"
        )
    elif (not ONLY_BUY) and bearish(h4_regime) and h1_sell:
        sinal = "VENDA — 4h DOWN_STRONG + pullback 1h"
    else:
        sinal = "AGUARDAR"

    print("\n=================================")
    print(sinal)
    notify(
        f"<b>{ASSET_NAME} RELOGIO RAGHEE</b>\n"
        f"{sinal}\n"
        f"Preco: {preco} | {horario}"
        + "".join(blocos)
    )
    print("=================================\n")


if __name__ == "__main__":
    try:
        run_scan()
    except Exception as e:
        print(f"ERRO na varredura: {e}")
