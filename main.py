import os
import math
import time
import requests
import pandas as pd

URL = "https://api.hyperliquid.xyz/info"
COIN = "@272"
ASSET_NAME = "ZCASH"

MAX_CANDLES_PER_REQUEST = 4999
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEFRAMES = ["1d", "4h", "1h"]
INTERVAL_MS = {
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}

WAVE_PERIOD = 34
WAVE_LOOKBACK = 8
HTF_FLAT = 0.9
HTF_STRONG = 2.0
LTF_FLAT = 0.40
LTF_STRONG = 1.50


def notify(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram nao configurado")
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
    price = float(df.iloc[-1]["close"])
    ts = df.iloc[-1]["datetime"].strftime("%Y-%m-%d %H:%M UTC")
    if len(df) > 1:
        df = df.iloc[:-1].copy()
    df.reset_index(drop=True, inplace=True)
    return df, price, ts


def add_wave(df):
    out = df.copy()
    out["W_HIGH"] = out["high"].ewm(span=WAVE_PERIOD, adjust=False).mean()
    out["W_MID"] = out["close"].ewm(span=WAVE_PERIOD, adjust=False).mean()
    out["W_LOW"] = out["low"].ewm(span=WAVE_PERIOD, adjust=False).mean()
    mid = out["W_MID"]
    out["SLOPE"] = ((mid / mid.shift(WAVE_LOOKBACK)) - 1) * 100
    out["ANGLE"] = out["SLOPE"].apply(
        lambda x: 0.0 if pd.isna(x) else math.degrees(math.atan(x / WAVE_LOOKBACK))
    )
    return out


def is_htf(tf):
    return INTERVAL_MS[tf] >= 4 * 60 * 60 * 1000


def cuts_for(tf):
    return (HTF_FLAT, HTF_STRONG) if is_htf(tf) else (LTF_FLAT, LTF_STRONG)


def clock_regime(slope, tf):
    if pd.isna(slope):
        return "NONE", "⚪ Sem dados"
    flat, strong = cuts_for(tf)
    if abs(slope) < flat:
        return "FLAT", "🟡 3h LATERAL"
    if slope >= strong:
        return "UP_STRONG", "🟢 12-2 ALTA FORTE"
    if slope > 0:
        return "UP_WEAK", "🟢 2-4 ALTA FRACA"
    if slope <= -strong:
        return "DOWN_STRONG", "🔴 4-6 BAIXA FORTE"
    return "DOWN_WEAK", "🔴 BAIXA FRACA"


def price_pos(row):
    if row["close"] > row["W_HIGH"]:
        return "ABOVE", "Acima da onda"
    if row["close"] < row["W_LOW"]:
        return "BELOW", "Abaixo da onda"
    return "INSIDE", "Dentro da onda"


def side_of(regime):
    if regime in ("UP_STRONG", "UP_WEAK"):
        return "UP"
    if regime in ("DOWN_STRONG", "DOWN_WEAK"):
        return "DOWN"
    return "FLAT"


def analyze(df, tf):
    df = add_wave(df)
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    regime, label = clock_regime(last["SLOPE"], tf)
    pos, pos_label = price_pos(last)
    prev_pos, _ = price_pos(prev)
    flat, strong = cuts_for(tf)

    pullback_buy = prev_pos == "ABOVE" and pos == "INSIDE" and last["close"] > last["W_MID"]
    pullback_sell = prev_pos == "BELOW" and pos == "INSIDE" and last["close"] < last["W_MID"]

    if regime == "FLAT":
        leitura = "Ponteiro deitado. Nao forcar."
    elif regime == "UP_STRONG" and pullback_buy:
        leitura = "12-2 e recuo na onda. Melhor compra deste TF."
    elif regime == "UP_STRONG" and pos == "ABOVE":
        leitura = "12-2, preco ja acima. Tendencia de alta, entrada tardia."
    elif regime == "UP_STRONG":
        leitura = "12-2. Vies de alta neste TF."
    elif regime == "DOWN_STRONG" and pullback_sell:
        leitura = "4-6 e recuo na onda. Melhor venda deste TF."
    elif regime == "DOWN_STRONG" and pos == "BELOW":
        leitura = "4-6, preco ja abaixo. Tendencia de baixa, entrada tardia."
    elif regime == "DOWN_STRONG":
        leitura = "4-6. Vies de baixa neste TF."
    elif regime == "UP_WEAK":
        leitura = "Alta fraca. Ainda nao e 12-2."
    else:
        leitura = "Baixa fraca. Ainda nao e 4-6."

    return {
        "candles": len(df),
        "first": df["datetime"].iloc[0].strftime("%Y-%m-%d %H:%M UTC"),
        "last": df["datetime"].iloc[-1].strftime("%Y-%m-%d %H:%M UTC"),
        "w_high": round(float(last["W_HIGH"]), 6),
        "w_mid": round(float(last["W_MID"]), 6),
        "w_low": round(float(last["W_LOW"]), 6),
        "slope": round(float(last["SLOPE"] or 0), 3),
        "angle": round(float(last["ANGLE"] or 0), 2),
        "regime": regime,
        "label": label,
        "pos": pos,
        "pos_label": pos_label,
        "leitura": leitura,
        "pullback_buy": bool(pullback_buy),
        "pullback_sell": bool(pullback_sell),
        "grupo": "4h+" if is_htf(tf) else "LTF",
        "flat": flat,
        "strong": strong,
        "stop_buy": round(float(min(last["W_LOW"], last["low"])), 6),
        "stop_sell": round(float(max(last["W_HIGH"], last["high"])), 6),
    }


def decidir(d1, h4, h1):
    r1d = d1.get("regime", "NONE")
    r4 = h4.get("regime", "NONE")
    s1d = side_of(r1d)
    s1 = side_of(h1.get("regime", "NONE"))

    if r4 == "FLAT" or r1d == "FLAT":
        return "AGUARDAR", "Onda maior deitada (3h). Relogio pede para nao operar."

    if r4 == "UP_STRONG" and s1d != "DOWN":
        if h1.get("pullback_buy"):
            return "COMPRAR", "1D/4h apontam alta e o 1h voltou para a onda. Maior probabilidade de continuacao de alta."
        if s1 == "DOWN":
            return "AGUARDAR", "4h em 12-2, mas 1h ainda aponta baixo. Esperar o recuo terminar."
        return "COMPRAR", "Relogio maior em 12-2. Probabilidade maior de o preco seguir para cima. Preferir recuo na onda do 1h."

    if r4 == "DOWN_STRONG" and s1d != "UP":
        if h1.get("pullback_sell"):
            return "VENDER", "1D/4h apontam baixa e o 1h voltou para a onda. Maior probabilidade de continuacao de baixa."
        if s1 == "UP":
            return "AGUARDAR", "4h em 4-6, mas 1h ainda aponta alta. Esperar o recuo terminar."
        return "VENDER", "Relogio maior em 4-6. Probabilidade maior de o preco seguir para baixo. Preferir recuo na onda do 1h."

    if r4 == "UP_WEAK" and s1d == "UP":
        return "AGUARDAR", "Alta existe, mas o 4h ainda nao e 12-2. Sem forcar compra."
    if r4 == "DOWN_WEAK" and s1d == "DOWN":
        return "AGUARDAR", "Baixa existe, mas o 4h ainda nao e 4-6. Sem forcar venda."

    return "AGUARDAR", "Relogios desalinhados. Sem vantagem clara de direcao."


def discover_1d_start():
    end_time = int(time.time() * 1000)
    start_time = end_time - 20 * 365 * 24 * 60 * 60 * 1000
    df, price, ts = get_candles("1d", start_time, end_time)
    print(f"Historico 1d desde: {df.iloc[0]['datetime'].strftime('%Y-%m-%d %H:%M UTC')}")
    return int(df.iloc[0]["t"]), df, price, ts


def run_scan():
    print(f"\n===== {ASSET_NAME} RELOGIO =====\n")
    results = {}
    try:
        history_start, df_1d, price_1d, time_1d = discover_1d_start()
        results["1d"] = analyze(df_1d, "1d")
        results["1d"]["current_price"] = price_1d
        results["1d"]["current_time"] = time_1d
        del df_1d
    except Exception as e:
        results["1d"] = {"erro": str(e)}
        history_start = int(time.time() * 1000) - 1500 * 24 * 60 * 60 * 1000

    for tf in ("4h", "1h"):
        try:
            df, price, ts = get_candles(tf, history_start)
            results[tf] = analyze(df, tf)
            results[tf]["current_price"] = price
            results[tf]["current_time"] = ts
            del df
        except Exception as e:
            results[tf] = {"erro": str(e)}

    blocos = []
    preco = horario = None
    for tf in TIMEFRAMES:
        d = results[tf]
        if "erro" in d:
            print(f"{tf} | ERRO: {d['erro']}")
            blocos.append(f"\n{tf}: ERRO {d['erro']}")
            continue
        if preco is None:
            preco, horario = d["current_price"], d["current_time"]
        print(f"\n{tf} | {d['grupo']} {d['flat']}/{d['strong']}")
        print(f"{d['label']} | {d['pos_label']} | slope={d['slope']}%")
        print(d["leitura"])
        blocos.append(
            f"\n<b>{tf}</b> ({d['grupo']} {d['flat']}/{d['strong']})\n"
            f"{d['label']}\n"
            f"{d['pos_label']} | slope={d['slope']}%\n"
            f"{d['leitura']}"
        )

    acao, motivo = decidir(results.get("1d", {}), results.get("4h", {}), results.get("1h", {}))
    if acao == "COMPRAR":
        acao_txt = "🟢 COMPRAR"
    elif acao == "VENDER":
        acao_txt = "🔴 VENDER"
    else:
        acao_txt = "🟡 AGUARDAR"

    h1 = results.get("1h", {})
    extra = ""
    if acao == "COMPRAR" and h1.get("stop_buy"):
        extra = f"\nStop sugerido 1h: {h1['stop_buy']}"
    if acao == "VENDER" and h1.get("stop_sell"):
        extra = f"\nStop sugerido 1h: {h1['stop_sell']}"

    print("\n=================================")
    print(f"{acao_txt}\n{motivo}{extra}")
    notify(
        f"<b>{ASSET_NAME} RELOGIO</b>\n"
        f"{acao_txt}\n"
        f"{motivo}{extra}\n"
        f"Preco: {preco} | {horario}"
        + "".join(blocos)
    )
    print("=================================\n")


if __name__ == "__main__":
    try:
        run_scan()
    except Exception as e:
        print(f"ERRO na varredura: {e}")
