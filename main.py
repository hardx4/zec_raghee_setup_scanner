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
TF_WEIGHT = {"1h": 1, "4h": 2, "1d": 3}
INTERVAL_MS = {
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}

MAX_CANDLES_PER_REQUEST = 4999
WAVE_PERIOD = 34
WAVE_LOOKBACK = 8
FLAT_PCT = 0.35
STRONG_PCT = 1.20


def notify(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram não configurado")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
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

    # ignora candle em formação
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


def clock_regime(slope):
    if pd.isna(slope):
        return "NONE", "⚪ Sem dados"

    if abs(slope) < FLAT_PCT:
        return "FLAT", "⚪ 3h Horizontal — PARE"

    if slope >= STRONG_PCT:
        return "UP_STRONG", "🟢 12-2 Alta forte"

    if slope > 0:
        return "UP_WEAK", "🟡 2-4 Alta fraca"

    if slope <= -STRONG_PCT:
        return "DOWN_STRONG", "🔴 4-6 Baixa forte"

    return "DOWN_WEAK", "🟠 4h Baixa fraca"


def price_vs_wave(row):
    if row["close"] > row["W_HIGH"]:
        return "ABOVE", "Acima da onda"
    if row["close"] < row["W_LOW"]:
        return "BELOW", "Abaixo da onda"
    return "INSIDE", "Dentro da onda"


def analyze(df):
    df = add_wave(df)
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last

    regime, label = clock_regime(last["SLOPE"])
    pos, pos_label = price_vs_wave(last)

    # pullback: veio de fora e voltou para a onda
    pullback_buy = (
        regime in ("UP_STRONG", "UP_WEAK")
        and pos == "INSIDE"
        and prev["close"] > prev["W_HIGH"]
    )
    pullback_sell = (
        regime in ("DOWN_STRONG", "DOWN_WEAK")
        and pos == "INSIDE"
        and prev["close"] < prev["W_LOW"]
    )

    # regra fiel da Raghee:
    # tendência forte + recuo na onda = setup
    # onda horizontal = não opera
    if regime == "FLAT":
        bias = "WAIT"
        setup = "Nenhum — onda horizontal"
    elif regime == "UP_STRONG" and pos in ("INSIDE", "ABOVE"):
        bias = "BUY"
        setup = "Compra em pullback/continuidade da onda"
    elif regime == "DOWN_STRONG" and pos in ("INSIDE", "BELOW"):
        bias = "SELL"
        setup = "Venda em pullback/continuidade da onda"
    elif regime == "UP_WEAK":
        bias = "WAIT"
        setup = "Alta fraca — não forçar"
    elif regime == "DOWN_WEAK":
        bias = "WAIT"
        setup = "Baixa fraca — não forçar"
    else:
        bias = "WAIT"
        setup = "Sem alinhamento"

    if pullback_buy:
        bias = "BUY"
        setup = "Pullback na onda de alta"
    if pullback_sell:
        bias = "SELL"
        setup = "Pullback na onda de baixa"

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
    }


def discover_1d_start():
    end_time = int(time.time() * 1000)
    start_time = end_time - 20 * 365 * 24 * 60 * 60 * 1000
    df, price, ts = get_candles("1d", start_time, end_time)
    first_ts = int(df.iloc[0]["t"])
    print(f"📅 Histórico 1d desde: {df.iloc[0]['datetime'].strftime('%Y-%m-%d %H:%M UTC')}")
    return first_ts, df, price, ts


def run_scan():
    print(f"\n===== {ASSET_NAME} | RELÓGIO RAGHEE =====\n")
    results = {}

    try:
        history_start, df_1d, price_1d, time_1d = discover_1d_start()
        data = analyze(df_1d)
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
            data = analyze(df)
            data["current_price"] = price
            data["current_time"] = ts
            results[tf] = data
            del df
        except Exception as e:
            results[tf] = {"erro": str(e)}

    buy = 0
    sell = 0
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

        print(f"\n{tf}")
        print(f"🕯️ {d['candles']} | {d['first']} → {d['last']}")
        print(f"💰 {d['current_price']} | {d['current_time']}")
        print(f"🕒 {d['label']} | {d['pos_label']}")
        print(f"Onda H/M/L: {d['w_high']} / {d['w_mid']} / {d['w_low']}")
        print(f"slope={d['slope']}% | ângulo={d['angle']}°")
        print(f"Setup: {d['setup']} | bias={d['bias']}")

        blocos.append(
            f"\n<b>{tf}</b>\n"
            f"{d['label']}\n"
            f"{d['pos_label']}\n"
            f"slope={d['slope']}% | ângulo={d['angle']}°\n"
            f"{d['setup']}"
        )

        if d["bias"] == "BUY":
            buy += TF_WEIGHT[tf]
        elif d["bias"] == "SELL":
            sell += TF_WEIGHT[tf]

    daily = results.get("1d", {})
    daily_regime = daily.get("regime", "NONE")

    # hierarquia dela: o timeframe maior manda
    if daily_regime == "FLAT":
        sinal = "🟡 AGUARDAR — 1D horizontal"
    elif buy >= 4 and daily_regime in ("UP_STRONG", "UP_WEAK"):
        sinal = "🟢 COMPRA — onda alinhada"
    elif sell >= 4 and daily_regime in ("DOWN_STRONG", "DOWN_WEAK"):
        sinal = "🔴 VENDA — onda alinhada"
    else:
        sinal = "🟡 AGUARDAR"

    print("\n=================================")
    print(sinal)
    notify(
        f"<b>{ASSET_NAME} RELÓGIO RAGHEE</b>\n"
        f"{sinal}\n"
        f"Preço: {preco} | {horario}\n"
        f"BUY={buy} | SELL={sell}"
        + "".join(blocos)
    )
    print("=================================\n")


if __name__ == "__main__":
    try:
        run_scan()
    except Exception as e:
        print(f"ERRO na varredura: {e}")
