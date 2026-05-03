import sys
import os
import argparse
import time
import shutil
import subprocess
import csv
from pathlib import Path
import requests
import numpy as np


def configure_cross_platform_stdio():
    """
    Windows konzolon a magyar ékezet és az emoji stabilabb megjelenítése (UTF-8).
    Linux/macOS: változatlanul hagyja az alapértelmezett streamet.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

try:
    from PySide6.QtCore import QTimer, QDateTime
    from PySide6.QtWidgets import (
        QApplication,
        QWidget,
        QVBoxLayout,
        QPushButton,
        QTextEdit,
        QListWidget,
        QLabel,
        QHBoxLayout,
        QGridLayout,
        QAbstractItemView,
        QInputDialog,
        QTabWidget,
        QCheckBox,
    )
    QT_AVAILABLE = True
    QT_IMPORT_ERROR = None
except Exception as e:
    QT_AVAILABLE = False
    QT_IMPORT_ERROR = e

from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split

# ---- ESZKÖZÖK ----
ASSETS = {
    "Bitcoin": "BTC-USD",
    "Ethereum": "ETH-USD",
    "BNB": "BNB-USD",
    "Solana": "SOL-USD",
    "XRP": "XRP-USD",
    "Cardano": "ADA-USD",
    "Dogecoin": "DOGE-USD",
    "TRON": "TRX-USD",
    "Polkadot": "DOT-USD",
    "Avalanche": "AVAX-USD",
    "Chainlink": "LINK-USD",
    "Litecoin": "LTC-USD",
    "ApeCoin (NFT)": "APE-USD",
    "Decentraland (NFT/Metaverse)": "MANA-USD",
    "The Sandbox (NFT/Metaverse)": "SAND-USD",
    "Enjin Coin (NFT)": "ENJ-USD",
    "Flow (NFT infrastruktúra)": "FLOW-USD",
    "Arany": "GC=F",
    "Kőolaj": "CL=F",
    "Földgáz": "NG=F",
    "Apple": "AAPL",
    "Microsoft": "MSFT",
    "NVIDIA": "NVDA",
    "Amazon": "AMZN",
    "Meta": "META",
    "Alphabet (Google)": "GOOGL",
    "Tesla": "TSLA",
    "AMD": "AMD",
    "Netflix": "NFLX",
    # Magyar részvények (BÉT, Yahoo: .BD)
    "OTP Bank": "OTP.BD",
    "MOL": "MOL.BD",
    "Richter Gedeon": "RICHTER.BD",
    "Magyar Telekom": "MTEL.BD",
    "4iG": "4IG.BD",
    "Opus Global": "OPUS.BD",
    "Waberer's": "WABERERS.BD",
    "Masterplast": "MASTERPLAST.BD",
    "AKKO Invest": "AKKO.BD",
    "Appeninn": "APPENINN.BD",
}

FX_URL = "https://api.exchangerate-api.com/v4/latest/USD"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL_DEFAULT = "gpt-4o-mini"
REQUEST_TIMEOUT = 15
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AssetTracker/1.0)",
}


def http_get(url, timeout=REQUEST_TIMEOUT, headers=None):
    try:
        return requests.get(url, timeout=timeout, headers=headers)
    except requests.RequestException as first_error:
        # Ha env proxy akadályozza, próbáljuk direktben (trust_env=False).
        try:
            with requests.Session() as session:
                session.trust_env = False
                return session.get(url, timeout=timeout, headers=headers)
        except requests.RequestException:
            raise first_error


def http_post(url, timeout=REQUEST_TIMEOUT, **kwargs):
    try:
        return requests.post(url, timeout=timeout, **kwargs)
    except requests.RequestException as first_error:
        try:
            with requests.Session() as session:
                session.trust_env = False
                return session.post(url, timeout=timeout, **kwargs)
        except requests.RequestException:
            raise first_error


# ---- USD → HUF ----
def get_rate(timeout=REQUEST_TIMEOUT, strict=True):
    errors = []

    try:
        r = http_get(FX_URL, timeout=timeout)
        r.raise_for_status()
        return float(r.json()["rates"]["HUF"])
    except (requests.RequestException, KeyError, ValueError, TypeError) as e:
        errors.append(f"exchangerate-api: {e}")

    # Fallback: Yahoo FX árfolyam
    try:
        yahoo_fx = "https://query1.finance.yahoo.com/v8/finance/chart/USDHUF=X?range=1d&interval=5m"
        r = http_get(yahoo_fx, timeout=timeout, headers=YAHOO_HEADERS)
        r.raise_for_status()
        data = r.json()
        chart = data.get("chart") or {}
        result = (chart.get("result") or [None])[0]
        closes = (((result or {}).get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        closes = [v for v in closes if v is not None]
        if not closes:
            raise ValueError("Yahoo FX close érték hiányzik")
        return float(closes[-1])
    except (requests.RequestException, KeyError, ValueError, TypeError) as e:
        errors.append(f"yahoo-fx: {e}")

    error = RuntimeError("USD→HUF árfolyam nem elérhető | " + " | ".join(errors))
    if strict:
        raise error
    return 1.0


def get_effective_rate():
    try:
        return get_rate(strict=True), "HUF", None
    except Exception as e:
        return 1.0, "USD", str(e)


def yahoo_price_display_multiplier(symbol, rate, display_currency):
    """
    Yahoo chart: USA részvények és *-USD instrumentumok USD-ben;
    BÉT (.BD) részvények HUF-ban.
    rate: 1 USD = rate HUF (get_rate érték).
    """
    cur = (display_currency or "HUF").strip().upper()
    r = float(rate) if rate not in (None, 0) else 1.0
    if r <= 0:
        r = 1.0
    sym = str(symbol or "").strip().upper()
    is_bet = sym.endswith(".BD")
    if cur == "HUF":
        return 1.0 if is_bet else r
    if is_bet:
        return 1.0 / r
    return 1.0


def get_usdhuf_vs_ma20(timeout=REQUEST_TIMEOUT):
    """
    Napi USD/HUF: utolsó záró ár vs. 20 napos mozgóátlag (%).
    None, ha nincs elég adat vagy hiba.
    """
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/USDHUF=X"
        "?range=90d&interval=1d"
    )
    try:
        r = http_get(url, timeout=timeout, headers=YAHOO_HEADERS)
        r.raise_for_status()
        data = r.json()
        chart = data.get("chart") or {}
        result = (chart.get("result") or [None])[0]
        quotes = ((result or {}).get("indicators") or {}).get("quote") or []
        if not quotes:
            return None
        closes = quotes[0].get("close") or []
        arr = np.array([x for x in closes if x is not None], dtype=float)
        if arr.size < 21:
            return None
        current = float(arr[-1])
        ma20 = float(np.mean(arr[-20:]))
        if ma20 == 0:
            return None
        pct_vs_ma = (current / ma20 - 1.0) * 100.0
        return {"current": current, "ma20": ma20, "pct_vs_ma": pct_vs_ma}
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return None


# ---- ADAT ----
def get_asset(symbol, timeout=REQUEST_TIMEOUT):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        "?range=90d&interval=1h"
    )
    r = http_get(url, timeout=timeout, headers=YAHOO_HEADERS)
    r.raise_for_status()
    data = r.json()

    chart = data.get("chart") or {}
    if chart.get("error") or not chart.get("result"):
        raise ValueError(f"Nincs árfolyam adat: {symbol}")

    result = chart["result"][0]
    quotes = (result.get("indicators") or {}).get("quote") or []
    if not quotes:
        raise ValueError(f"Üres idősor: {symbol}")

    prices = quotes[0].get("close") or []
    prices = np.array([p for p in prices if p is not None], dtype=float)
    if prices.size < 2:
        raise ValueError(f"Túl kevés érvényes ár: {symbol}")
    return prices


# ---- RSI (Wilder simítás) ----
def calculate_rsi(prices, period=14):
    if prices.size < period + 1:
        return 50.0

    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ---- MOZGÓÁTLAG ----
def moving_average(prices, window=20):
    w = min(window, prices.size)
    return float(np.mean(prices[-w:]))


# ---- AI ----
def build_features_and_labels(prices):
    """
    Features on each hour:
    - short / medium returns
    - short volatility
    - moving average ratios
    - RSI normalized
    Label: next-hour direction (1=up, 0=down/flat)
    """
    if prices.size < 120:
        raise ValueError("Túl kevés adat a modellhez (min. 120 pont kell).")

    X, y = [], []
    returns = np.diff(prices) / prices[:-1]
    lookback = 30

    for i in range(lookback, prices.size - 1):
        ret_1 = returns[i - 1]
        ret_3 = (prices[i] / prices[i - 3]) - 1.0
        ret_12 = (prices[i] / prices[i - 12]) - 1.0
        vol_12 = float(np.std(returns[i - 12:i]))
        ma_10 = float(np.mean(prices[i - 10:i]))
        ma_30 = float(np.mean(prices[i - 30:i]))
        ma_ratio_10 = (prices[i] / ma_10) - 1.0
        ma_ratio_30 = (prices[i] / ma_30) - 1.0
        rsi = calculate_rsi(prices[: i + 1]) / 100.0

        X.append(
            [
                ret_1,
                ret_3,
                ret_12,
                vol_12,
                ma_ratio_10,
                ma_ratio_30,
                rsi,
            ]
        )
        y.append(1 if prices[i + 1] > prices[i] else 0)

    return np.array(X, dtype=float), np.array(y, dtype=int)


def random_forest_up_probability(model, feature_row):
    """
    P(következő óra fel) = osztály 1 valószínűsége.
    Ha a tanítóhalmaz egy osztályú volt, a predict_proba csak 1 oszlop — a [0][1] index hibát dobna.
    """
    row = np.asarray(feature_row, dtype=float).reshape(1, -1)
    proba = model.predict_proba(row)[0]
    classes = np.asarray(getattr(model, "classes_", np.arange(len(proba))))
    if classes.size <= 1:
        if classes.size == 0:
            return 0.5
        return 1.0 if int(classes[0]) == 1 else 0.0
    idx = np.flatnonzero(classes == 1)
    if idx.size > 0:
        return float(proba[int(idx[0])])
    return float(1.0 - proba[0])


def predict(prices):
    X, y = build_features_and_labels(prices)
    if X.shape[0] < 80:
        raise ValueError("Túl kevés minta a tanításhoz.")

    X_train = X_test = y_train = y_test = None
    uniq, counts = np.unique(y, return_counts=True)
    can_stratify = uniq.size >= 2 and int(counts.min()) >= 2
    if can_stratify:
        try:
            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=0.2, random_state=42, stratify=y
            )
        except ValueError:
            X_train = None
    if X_train is None:
        split = int(X.shape[0] * 0.8)
        split = max(1, min(split, X.shape[0] - 1))
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]

    model = RandomForestClassifier(
        n_estimators=300,
        min_samples_leaf=3,
        random_state=42,
    )
    model.fit(X_train, y_train)

    accuracy = float((model.predict(X_test) == y_test).mean()) if y_test.size else 0.0
    next_up_prob = random_forest_up_probability(model, X[-1])

    current = float(prices[-1])
    tail = prices[-49:]
    recent_returns = np.diff(tail) / tail[:-1]
    recent_abs_move = float(np.mean(np.abs(recent_returns)))
    drift = (2.0 * next_up_prob - 1.0) * recent_abs_move
    future = current * (1.0 + drift)

    return current, future, next_up_prob, accuracy


# ---- KOMBINÁLT DÖNTÉS ----
def smart_decision(prices):
    current, future, next_up_prob, accuracy = predict(prices)

    rsi = calculate_rsi(prices)
    ma = moving_average(prices)

    signals = []

    if next_up_prob < 0.45:
        signals.append("SELL")
    elif next_up_prob > 0.55:
        signals.append("BUY")

    if rsi > 70:
        signals.append("SELL")
    elif rsi < 30:
        signals.append("BUY")

    if current > ma:
        signals.append("BUY")
    else:
        signals.append("SELL")

    sell_count = signals.count("SELL")
    buy_count = signals.count("BUY")

    if sell_count >= 2:
        decision = "ELADÁS ⚠️"
    elif buy_count >= 2:
        decision = "VÉTEL 📈"
    else:
        decision = "VÁRJ ⏳"

    return decision, current, future, rsi, ma, next_up_prob, accuracy


def build_recommendation(current, future, next_up_prob, accuracy, rsi, ma):
    edge = next_up_prob - 0.5
    confidence = max(0.0, min(1.0, (abs(edge) * 2.0) * (0.6 + 0.4 * accuracy)))

    # Volatilitás alapú kockázati távolság (minimum 1.5%)
    base_risk = max(0.015, abs((future / current) - 1.0))
    stop_distance = base_risk * 0.8
    tp_distance = base_risk * 1.6

    if next_up_prob >= 0.58:
        side = "LONG"
        action = "VÉTEL"
        entry = current
        stop = entry * (1.0 - stop_distance)
        take_profit = entry * (1.0 + tp_distance)
    elif next_up_prob <= 0.42:
        side = "SHORT"
        action = "ELADÁS"
        entry = current
        stop = entry * (1.0 + stop_distance)
        take_profit = entry * (1.0 - tp_distance)
    else:
        side = "NEUTRAL"
        action = "KIVÁRÁS"
        entry = current
        stop = current
        take_profit = current

    reasons = []
    reasons.append(f"AI valószínűség: {next_up_prob * 100:.1f}%")
    reasons.append(f"Modell validáció: {accuracy * 100:.1f}%")
    reasons.append("RSI túlvett" if rsi > 70 else "RSI túladott" if rsi < 30 else "RSI semleges")
    reasons.append("Ár MA felett" if current > ma else "Ár MA alatt")

    return {
        "action": action,
        "side": side,
        "confidence": confidence,
        "entry": entry,
        "stop": stop,
        "take_profit": take_profit,
        "reasons": reasons,
    }


def evaluate_today_investment(amount, currency, has_real_huf_rate, fx_ctx, result):
    """
    Összeg + árfolyam-kontextus + AI/jel: napi „érdemes-e” és javasolt tét (%).
    Nem pénzügyi tanács — csak modell-alapú összefoglaló.
    """
    rec = result["recommendation"]
    decision = result["decision"]
    conf = float(rec["confidence"])
    side = rec["side"]

    deploy_frac = 0.0
    if side == "LONG" or "VÉTEL" in decision:
        deploy_frac = min(0.85, 0.18 + 0.62 * conf)
        verdict = (
            "A mai jelzések alapján érdemes lehet korlátozott összeggel részt venni "
            "(lásd javasolt tét)."
            if conf >= 0.48
            else "Mérsékelt a jel; csak kisebb részt tarts ma kockázaton."
        )
    elif side == "SHORT" or "ELADÁS" in decision:
        deploy_frac = 0.0
        verdict = (
            "Ma nem javasolt új vételi pozíciót nyitni; inkább kivárás vagy "
            "meglévő pozíció felülvizsgálata."
        )
    else:
        deploy_frac = min(0.30, 0.08 + 0.22 * conf)
        verdict = (
            "Ma nincs egyértelmű vételi jel; érdemes várni, vagy csak nagyon kis tétet fontolgatni."
        )

    fx_note = ""
    if fx_ctx is not None:
        p = float(fx_ctx["pct_vs_ma"])
        if currency == "HUF" and has_real_huf_rate:
            if p > 1.2:
                deploy_frac *= 0.82
                fx_note = (
                    f"USD/HUF kb {p:+.1f}% a 20 napos átlag felett — a forintban vásárlás "
                    "ma általában drágább; a javasolt tétet enyhén csökkentettem."
                )
            elif p < -1.2:
                deploy_frac = min(0.88, deploy_frac * 1.06)
                fx_note = (
                    f"USD/HUF kb {p:+.1f}% a 20 napos átlag alatt — relatíve kedvezőbb "
                    "árfolyam-környezet a dollárban árazott eszközökhöz."
                )
            else:
                fx_note = (
                    f"USD/HUF kb {p:+.1f}% a 20 napos átlaghoz képest — közel „normál” "
                    "árfolyam-sáv."
                )
        else:
            fx_note = (
                f"(Tájékoztató) USD/HUF kb {p:+.1f}% a 20 napos átlaghoz képest — "
                "ha forintból váltanál, ez a környezet számít."
            )

    deploy_frac = max(0.0, min(deploy_frac, 0.88))
    deploy_amount = float(amount) * deploy_frac

    if deploy_frac < 0.08 and side != "SHORT" and "ELADÁS" not in decision:
        verdict = (
            "Ma gyenge a jel egy nagyobb befektetéshez; inkább minimális összeg, "
            "vagy kihagyás."
        )

    return {
        "amount": float(amount),
        "deploy_frac": deploy_frac,
        "deploy_amount": deploy_amount,
        "verdict": verdict,
        "fx_note": fx_note,
    }


def analyze_asset(name, symbol, rate, display_currency="HUF"):
    mult = yahoo_price_display_multiplier(symbol, rate, display_currency)
    prices = get_asset(symbol) * mult
    decision, current, future, rsi, ma, next_up_prob, accuracy = smart_decision(prices)
    recommendation = build_recommendation(
        current=current,
        future=future,
        next_up_prob=next_up_prob,
        accuracy=accuracy,
        rsi=rsi,
        ma=ma,
    )
    return {
        "name": name,
        "current": current,
        "future": future,
        "rsi": rsi,
        "ma": ma,
        "decision": decision,
        "next_up_prob": next_up_prob,
        "accuracy": accuracy,
        "recommendation": recommendation,
    }


def get_current_price(symbol, rate, display_currency="HUF"):
    prices = get_asset(symbol)
    mult = yahoo_price_display_multiplier(symbol, rate, display_currency)
    return float(prices[-1] * mult)


def notify_price_change(title, message):
    # Linux desktop notification via notify-send (if available)
    if sys.platform.startswith("linux") and shutil.which("notify-send"):
        try:
            subprocess.run(
                ["notify-send", title, message],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            pass
    print(f"[ÉRTESÍTÉS] {title} - {message}")


def send_telegram_message(bot_token, chat_id, message, timeout=REQUEST_TIMEOUT):
    if not bot_token or not chat_id:
        return False, "Hiányzó Telegram token/chat_id"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    try:
        r = requests.post(url, data=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            return False, str(data)
        return True, None
    except (requests.RequestException, ValueError, TypeError) as e:
        return False, str(e)


def append_live_csv(csv_path, rows):
    if not rows:
        return
    path = Path(csv_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp",
                "asset",
                "currency",
                "current",
                "future",
                "decision",
                "next_up_prob",
                "model_accuracy",
                "confidence",
                "invest_amount",
                "deploy_amount",
                "deploy_frac",
            ],
        )
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def get_ai_commentary(result, currency, api_key, model=OPENAI_MODEL_DEFAULT, timeout=20):
    if not api_key:
        return None, "Nincs AI API kulcs."
    rec = result.get("recommendation", {})
    prompt = (
        "Adj rovid, 3-5 pontos magyar piaci osszefoglalot ezek alapjan. "
        "Ne adj befektetesi garanciat, legyen ovatos hangnem.\n\n"
        f"Eszkoz: {result.get('name')}\n"
        f"Most: {result.get('current'):.2f} {currency}\n"
        f"AI celar (1 ora): {result.get('future'):.2f} {currency}\n"
        f"AI fel esely: {result.get('next_up_prob', 0.0) * 100:.1f}%\n"
        f"Modell pontossag: {result.get('accuracy', 0.0) * 100:.1f}%\n"
        f"RSI: {result.get('rsi', 0.0):.2f}\n"
        f"Mozgoatlag: {result.get('ma', 0.0):.2f}\n"
        f"Dontes: {result.get('decision')}\n"
        f"Javasolt irany: {rec.get('action', 'ismeretlen')}\n"
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Te egy konzervativ penzugyi elemzo asszisztens vagy."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
    }
    try:
        r = http_post(OPENAI_API_URL, headers=headers, json=payload, timeout=timeout)
        data = r.json()
        if r.status_code >= 400:
            err = data.get("error") if isinstance(data, dict) else None
            if isinstance(err, dict):
                msg = err.get("message") or err.get("code") or str(err)
            else:
                msg = str(err) if err else r.text or r.reason
            return None, f"OpenAI API ({r.status_code}): {msg}"
        content = (((data.get("choices") or [None])[0] or {}).get("message") or {}).get("content")
        if not content:
            return None, "Ures AI valasz."
        return str(content).strip(), None
    except (requests.RequestException, ValueError, TypeError) as e:
        return None, str(e)


def monitor_price_changes(selected_assets, interval_sec=60, threshold_pct=1.0):
    print(
        f"Árfigyelés indul | időköz: {interval_sec}s | értesítési küszöb: {threshold_pct:.2f}%"
    )
    print("Kilépés: Ctrl+C")

    rate, currency, rate_warning = get_effective_rate()
    if rate_warning:
        print(f"Figyelem: árfolyam hiba, USD módra váltva. ({rate_warning})")

    last_prices = {}
    had_error = False
    for name, symbol in selected_assets:
        try:
            current = get_current_price(symbol, rate, currency)
            last_prices[name] = current
            print(f"Kezdő ár — {name}: {current:,.2f} {currency}")
        except (requests.RequestException, ValueError, KeyError) as e:
            had_error = True
            print(f"{name}: induló ár lekérés hiba — {e}")

    if not last_prices:
        return 1

    while True:
        try:
            time.sleep(max(1, int(interval_sec)))
            rate, currency, rate_warning = get_effective_rate()
            if rate_warning:
                print(f"Figyelem: árfolyam hiba, USD módra váltva. ({rate_warning})")

            for name, symbol in selected_assets:
                if name not in last_prices:
                    continue
                try:
                    current = get_current_price(symbol, rate, currency)
                    previous = last_prices[name]
                    if previous == 0:
                        last_prices[name] = current
                        continue

                    change_pct = ((current / previous) - 1.0) * 100.0
                    if abs(change_pct) >= threshold_pct:
                        direction = "nőtt" if change_pct > 0 else "csökkent"
                        msg = (
                            f"{name}: {direction} {change_pct:+.2f}% | "
                            f"{previous:,.2f} → {current:,.2f} {currency}"
                        )
                        print(msg)
                        notify_price_change("Árfolyam riasztás", msg)
                        last_prices[name] = current
                except (requests.RequestException, ValueError, KeyError) as e:
                    had_error = True
                    print(f"{name}: figyelési hiba — {e}")
        except KeyboardInterrupt:
            print("\nÁrfigyelés leállítva.")
            return 1 if had_error else 0


def format_result(result, currency="HUF"):
    rec = result["recommendation"]
    text = f"{result['name']}\n"
    text += f"Most: {int(result['current']):,} {currency}\n"
    text += f"AI célár (1 óra): {int(result['future']):,} {currency}\n"
    text += f"AI esély fel: {result['next_up_prob'] * 100:.1f}%\n"
    text += f"AI validáció pontosság: {result['accuracy'] * 100:.1f}%\n"
    text += f"RSI: {result['rsi']:.2f}\n"
    text += f"MA: {int(result['ma']):,}\n"
    text += f"Döntés: {result['decision']}\n"
    text += "Ajánlás:\n"
    text += f"- Irány: {rec['action']} ({rec['side']})\n"
    text += f"- Bizalom: {rec['confidence'] * 100:.1f}%\n"
    if rec["side"] != "NEUTRAL":
        text += f"- Belépő: {int(rec['entry']):,} {currency}\n"
        text += f"- Stop-loss: {int(rec['stop']):,} {currency}\n"
        text += f"- Take-profit: {int(rec['take_profit']):,} {currency}\n"
    text += f"- Indok: {', '.join(rec['reasons'])}\n"
    if result.get("investment"):
        inv = result["investment"]
        text += "\nBefektetés (megadott összeg):\n"
        text += f"- Tőke: {int(inv['amount']):,} {currency}\n"
        text += f"- Ma érdemes-e nagyobb tét: {inv['verdict']}\n"
        if inv.get("fx_note"):
            text += f"- Árfolyam (USD/HUF): {inv['fx_note']}\n"
        text += (
            f"- Javasolt tét ma: ~{int(inv['deploy_amount']):,} {currency} "
            f"({inv['deploy_frac'] * 100:.0f}% a megadott összegből)\n"
        )
    return text


if QT_AVAILABLE:
    # ---- GUI ----
    class App(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("AI Eszkozelemzo Pro")
            self.setGeometry(100, 100, 1220, 700)
            self.apply_styles()
            self.invest_amount = None
            self.live_interval_sec = 30
            self.alert_threshold_pct = 0.8
            self.auto_clear_analysis = True
            self.sound_alerts_enabled = True
            self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
            self.telegram_enabled = bool(self.telegram_bot_token and self.telegram_chat_id)
            self.ai_commentary_enabled = False
            self.ai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
            self.ai_model = OPENAI_MODEL_DEFAULT
            self.csv_autosave_enabled = True
            self.csv_path = str(Path.cwd() / "live_history.csv")
            self.last_prices_live = {}
            self.live_timer = QTimer(self)
            self.live_timer.timeout.connect(self.run_live_tick)

            layout = QHBoxLayout()
            layout.setContentsMargins(14, 14, 14, 14)
            layout.setSpacing(12)

            self.list_widget = QListWidget()
            self.list_widget.setSelectionMode(
                QAbstractItemView.SelectionMode.ExtendedSelection
            )
            for name in ASSETS:
                self.list_widget.addItem(name)
            self.list_widget.setCurrentRow(0)
            self.list_widget.setMinimumWidth(280)

            layout.addWidget(self.list_widget)

            right = QVBoxLayout()
            right.setSpacing(10)

            self.label = QLabel("AI + Indikator elemzes | Pro felulet")
            right.addWidget(self.label)

            stats_grid = QGridLayout()
            self.stats_assets = QLabel("Kivalasztva: 0")
            self.stats_decisions = QLabel("Jelzesek: Vetel 0 | Eladas 0 | Varj 0")
            self.stats_update = QLabel("Utolso frissites: -")
            stats_grid.addWidget(self.stats_assets, 0, 0)
            stats_grid.addWidget(self.stats_decisions, 0, 1)
            stats_grid.addWidget(self.stats_update, 1, 0, 1, 2)
            right.addLayout(stats_grid)

            action_row = QHBoxLayout()

            self.btn_select_all = QPushButton("Összes kijelölése")
            self.btn_select_all.clicked.connect(self.select_all_assets)
            action_row.addWidget(self.btn_select_all)

            self.btn_clear = QPushButton("Kimenet törlése")
            self.btn_clear.clicked.connect(self.output_clear)
            action_row.addWidget(self.btn_clear)

            self.btn_set_amount = QPushButton("Összeg beállítása")
            self.btn_set_amount.clicked.connect(self.set_invest_amount)
            action_row.addWidget(self.btn_set_amount)

            right.addLayout(action_row)

            self.tabs = QTabWidget()

            analysis_tab = QWidget()
            analysis_layout = QVBoxLayout()
            self.output = QTextEdit()
            self.output.setReadOnly(True)
            self.output.setPlaceholderText("Az elemzes eredmenye itt fog megjelenni...")
            analysis_layout.addWidget(self.output)
            analysis_tab.setLayout(analysis_layout)
            self.tabs.addTab(analysis_tab, "Elemzes")

            alerts_tab = QWidget()
            alerts_layout = QVBoxLayout()
            self.alert_output = QTextEdit()
            self.alert_output.setReadOnly(True)
            self.alert_output.setPlaceholderText("Elo riasztasok es esemenyek...")
            alerts_layout.addWidget(self.alert_output)
            self.btn_clear_alerts = QPushButton("Ertesitesi naplo torlese")
            self.btn_clear_alerts.clicked.connect(self.clear_alerts)
            alerts_layout.addWidget(self.btn_clear_alerts)
            alerts_tab.setLayout(alerts_layout)
            self.tabs.addTab(alerts_tab, "Ertesitesek")

            portfolio_tab = QWidget()
            portfolio_layout = QVBoxLayout()
            self.portfolio_output = QTextEdit()
            self.portfolio_output.setReadOnly(True)
            self.portfolio_output.setPlaceholderText("Portfolio osszegzes itt jelenik meg...")
            portfolio_layout.addWidget(self.portfolio_output)
            portfolio_tab.setLayout(portfolio_layout)
            self.tabs.addTab(portfolio_tab, "Portfolio")

            settings_tab = QWidget()
            settings_layout = QVBoxLayout()
            self.auto_clear_checkbox = QCheckBox("Elemzes torlese minden frissites elott")
            self.auto_clear_checkbox.setChecked(self.auto_clear_analysis)
            self.auto_clear_checkbox.toggled.connect(self.toggle_auto_clear)
            settings_layout.addWidget(self.auto_clear_checkbox)

            self.sound_alert_checkbox = QCheckBox("Hangriasztas engedelyezve")
            self.sound_alert_checkbox.setChecked(self.sound_alerts_enabled)
            self.sound_alert_checkbox.toggled.connect(self.toggle_sound_alerts)
            settings_layout.addWidget(self.sound_alert_checkbox)

            self.csv_autosave_checkbox = QCheckBox("CSV automatikus mentes eloben")
            self.csv_autosave_checkbox.setChecked(self.csv_autosave_enabled)
            self.csv_autosave_checkbox.toggled.connect(self.toggle_csv_autosave)
            settings_layout.addWidget(self.csv_autosave_checkbox)

            self.btn_csv_path = QPushButton("CSV fajl: live_history.csv")
            self.btn_csv_path.clicked.connect(self.set_csv_path)
            settings_layout.addWidget(self.btn_csv_path)

            self.btn_alert_threshold = QPushButton("Riasztasi kuszob: 0.80%")
            self.btn_alert_threshold.clicked.connect(self.set_alert_threshold)
            settings_layout.addWidget(self.btn_alert_threshold)

            self.btn_telegram = QPushButton("Telegram beallitas")
            self.btn_telegram.clicked.connect(self.configure_telegram)
            settings_layout.addWidget(self.btn_telegram)

            self.telegram_status = QLabel("Telegram: kikapcsolva")
            settings_layout.addWidget(self.telegram_status)

            self.ai_checkbox = QCheckBox("Felho AI magyarazat engedelyezve")
            self.ai_checkbox.setChecked(self.ai_commentary_enabled)
            self.ai_checkbox.toggled.connect(self.toggle_ai_commentary)
            settings_layout.addWidget(self.ai_checkbox)

            self.btn_ai_config = QPushButton("AI beallitas (API kulcs + modell)")
            self.btn_ai_config.clicked.connect(self.configure_ai)
            settings_layout.addWidget(self.btn_ai_config)

            self.ai_status = QLabel("AI: kikapcsolva")
            settings_layout.addWidget(self.ai_status)

            settings_layout.addStretch(1)
            settings_tab.setLayout(settings_layout)
            self.tabs.addTab(settings_tab, "Beallitasok")

            right.addWidget(self.tabs)

            live_row = QHBoxLayout()

            self.btn = QPushButton("Elemzés")
            self.btn.clicked.connect(self.run_analysis)
            live_row.addWidget(self.btn)

            self.btn_live = QPushButton("Élő mód indítása")
            self.btn_live.clicked.connect(self.toggle_live_mode)
            live_row.addWidget(self.btn_live)

            self.btn_interval = QPushButton("Időköz: 30s")
            self.btn_interval.clicked.connect(self.set_live_interval)
            live_row.addWidget(self.btn_interval)

            right.addLayout(live_row)

            self.live_status = QLabel("Élő mód: kikapcsolva")
            right.addWidget(self.live_status)

            layout.addLayout(right)
            self.setLayout(layout)
            self.list_widget.itemSelectionChanged.connect(self.update_selected_count)
            self.update_selected_count()
            self.refresh_telegram_status()
            self.refresh_ai_status()

        def apply_styles(self):
            self.setStyleSheet(
                """
                QWidget {
                    background-color: #111827;
                    color: #e5e7eb;
                    font-size: 13px;
                }
                QLabel {
                    font-size: 14px;
                    font-weight: 600;
                    color: #f3f4f6;
                }
                QTabWidget::pane {
                    border: 1px solid #374151;
                    border-radius: 8px;
                    background-color: #1f2937;
                }
                QTabBar::tab {
                    background: #111827;
                    color: #d1d5db;
                    padding: 8px 12px;
                    border: 1px solid #374151;
                    border-bottom: none;
                    border-top-left-radius: 6px;
                    border-top-right-radius: 6px;
                    margin-right: 2px;
                }
                QTabBar::tab:selected {
                    background: #2563eb;
                    color: #ffffff;
                }
                QListWidget, QTextEdit {
                    background-color: #1f2937;
                    border: 1px solid #374151;
                    border-radius: 8px;
                    padding: 6px;
                    selection-background-color: #2563eb;
                }
                QPushButton {
                    background-color: #2563eb;
                    color: #ffffff;
                    border: none;
                    border-radius: 8px;
                    padding: 8px 12px;
                    font-weight: 600;
                }
                QPushButton:hover {
                    background-color: #1d4ed8;
                }
                QPushButton:pressed {
                    background-color: #1e40af;
                }
                QCheckBox {
                    font-size: 13px;
                    color: #e5e7eb;
                }
                """
            )

        def select_all_assets(self):
            self.list_widget.selectAll()

        def output_clear(self):
            self.output.clear()

        def clear_alerts(self):
            self.alert_output.clear()

        def update_selected_count(self):
            selected = len(self.list_widget.selectedItems())
            if selected == 0:
                selected = len(ASSETS)
            self.stats_assets.setText(f"Kivalasztva: {selected}")

        def toggle_auto_clear(self, checked):
            self.auto_clear_analysis = bool(checked)

        def toggle_sound_alerts(self, checked):
            self.sound_alerts_enabled = bool(checked)

        def toggle_csv_autosave(self, checked):
            self.csv_autosave_enabled = bool(checked)

        def toggle_ai_commentary(self, checked):
            self.ai_commentary_enabled = bool(checked)
            self.refresh_ai_status()

        def refresh_ai_status(self):
            if self.ai_commentary_enabled and self.ai_api_key:
                self.ai_status.setText(f"AI: aktiv ({self.ai_model})")
            elif self.ai_api_key:
                self.ai_status.setText(f"AI: kulcs beallitva ({self.ai_model})")
            else:
                self.ai_status.setText("AI: kikapcsolva")

        def configure_ai(self):
            key, ok_key = QInputDialog.getText(
                self,
                "AI API kulcs",
                "OpenAI API key:",
                text=self.ai_api_key,
            )
            if not ok_key:
                return
            model, ok_model = QInputDialog.getText(
                self,
                "AI modell",
                "Model:",
                text=self.ai_model,
            )
            if not ok_model:
                return
            self.ai_api_key = key.strip()
            self.ai_model = model.strip() if model.strip() else OPENAI_MODEL_DEFAULT
            self.refresh_ai_status()
            if self.ai_api_key:
                self.log_alert(f"AI beallitva: {self.ai_model}")

        def set_csv_path(self):
            value, ok = QInputDialog.getText(
                self,
                "CSV mentesi utvonal",
                "Fajl eleresi ut:",
                text=self.csv_path,
            )
            if ok and value.strip():
                self.csv_path = value.strip()
                self.btn_csv_path.setText(f"CSV fajl: {Path(self.csv_path).name}")
                self.log_alert(f"CSV mentesi utvonal: {self.csv_path}")

        def set_alert_threshold(self):
            value, ok = QInputDialog.getDouble(
                self,
                "Riasztasi kuszob",
                "Szazalek:",
                self.alert_threshold_pct,
                0.05,
                50.0,
                2,
            )
            if ok:
                self.alert_threshold_pct = value
                self.btn_alert_threshold.setText(f"Riasztasi kuszob: {value:.2f}%")

        def refresh_telegram_status(self):
            if self.telegram_enabled and self.telegram_bot_token and self.telegram_chat_id:
                self.telegram_status.setText("Telegram: aktiv")
            elif self.telegram_bot_token or self.telegram_chat_id:
                self.telegram_status.setText("Telegram: reszben beallitva")
            else:
                self.telegram_status.setText("Telegram: kikapcsolva")

        def configure_telegram(self):
            token, ok_token = QInputDialog.getText(
                self,
                "Telegram bot token",
                "Bot token:",
                text=self.telegram_bot_token,
            )
            if not ok_token:
                return
            chat_id, ok_chat = QInputDialog.getText(
                self,
                "Telegram chat ID",
                "Chat ID:",
                text=self.telegram_chat_id,
            )
            if not ok_chat:
                return
            self.telegram_bot_token = token.strip()
            self.telegram_chat_id = chat_id.strip()
            self.telegram_enabled = bool(self.telegram_bot_token and self.telegram_chat_id)
            self.refresh_telegram_status()
            if self.telegram_enabled:
                ok, err = send_telegram_message(
                    self.telegram_bot_token,
                    self.telegram_chat_id,
                    "AI Eszkozelemzo Pro: Telegram kapcsolat aktiv.",
                )
                if ok:
                    self.log_alert("Telegram tesztuzenet elkuldve.")
                else:
                    self.log_alert(f"Telegram hiba: {err}")

        def trigger_alert_channels(self, message):
            notify_price_change("Pro riasztas", message)
            if self.sound_alerts_enabled:
                QApplication.beep()
            if self.telegram_enabled:
                ok, err = send_telegram_message(
                    self.telegram_bot_token,
                    self.telegram_chat_id,
                    f"Pro riasztas\n{message}",
                )
                if not ok:
                    self.log_alert(f"Telegram kuldes sikertelen: {err}")

        def log_alert(self, text):
            now_text = QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss")
            self.alert_output.append(f"[{now_text}] {text}")

        def update_decision_stats(self, results):
            def normalize(text):
                return (
                    text.upper()
                    .replace("É", "E")
                    .replace("Á", "A")
                    .replace("Ó", "O")
                    .replace("Ö", "O")
                    .replace("Ő", "O")
                    .replace("Ú", "U")
                    .replace("Ü", "U")
                    .replace("Ű", "U")
                )

            buy = sum(1 for r in results if "VETEL" in normalize(r["decision"]))
            sell = sum(1 for r in results if "ELADAS" in normalize(r["decision"]))
            wait = max(0, len(results) - buy - sell)
            self.stats_decisions.setText(
                f"Jelzesek: Vetel {buy} | Eladas {sell} | Varj {wait}"
            )
            if buy > sell:
                color = "#22c55e"
            elif sell > buy:
                color = "#ef4444"
            else:
                color = "#f59e0b"
            self.stats_decisions.setStyleSheet(f"color: {color}; font-weight: 700;")

        def update_portfolio_tab(self, results, currency):
            if not results:
                self.portfolio_output.setPlainText("Nincs portfolio adat.")
                return
            total_current = sum(float(r["current"]) for r in results)
            total_future = sum(float(r["future"]) for r in results)
            avg_prob = sum(float(r["next_up_prob"]) for r in results) / len(results)
            total_deploy = sum(float(r["investment"]["deploy_amount"]) for r in results if r.get("investment"))
            total_input = sum(float(r["investment"]["amount"]) for r in results if r.get("investment"))
            lines = [
                "Portfolio osszegzes",
                "=" * 40,
                f"Eszkozok szama: {len(results)}",
                f"Ossz jelenlegi ertek (indikativ): {total_current:,.2f} {currency}",
                f"Ossz AI celar (1 ora): {total_future:,.2f} {currency}",
                f"Atlagos AI fel-valoszinuseg: {avg_prob * 100:.1f}%",
                f"Osszesitett javasolt tet: {total_deploy:,.2f} {currency}",
            ]
            if total_input > 0:
                lines.append(f"Kitettseg arany: {(total_deploy / total_input) * 100:.1f}%")
            lines.append("")
            lines.append("Eszkozonkenti gyorsnezet:")
            for r in results:
                lines.append(
                    f"- {r['name']}: {r['decision']} | Most: {r['current']:,.2f} {currency} | "
                    f"AI fel: {r['next_up_prob'] * 100:.1f}%"
                )
            self.portfolio_output.setPlainText("\n".join(lines))

        def set_invest_amount(self):
            rate, currency, _ = get_effective_rate()
            amount, ok = QInputDialog.getDouble(
                self,
                "Befektetés",
                f"Mennyit szeretnél befektetni? ({currency})",
                self.invest_amount if self.invest_amount is not None else 100_000.0,
                0.0,
                1e15,
                0,
            )
            if ok:
                self.invest_amount = amount
                self.output.append(f"Befektetési összeg beállítva: {int(amount):,} {currency}\n")

        def ensure_invest_amount(self):
            if self.invest_amount is None:
                self.set_invest_amount()
            return self.invest_amount is not None

        def set_live_interval(self):
            value, ok = QInputDialog.getInt(
                self,
                "Élő frissítés időköze",
                "Másodperc:",
                self.live_interval_sec,
                5,
                3600,
                1,
            )
            if ok:
                self.live_interval_sec = value
                self.btn_interval.setText(f"Időköz: {value}s")
                if self.live_timer.isActive():
                    self.live_timer.start(self.live_interval_sec * 1000)

        def toggle_live_mode(self):
            if self.live_timer.isActive():
                self.live_timer.stop()
                self.btn_live.setText("Élő mód indítása")
                self.live_status.setText("Élő mód: kikapcsolva")
                return

            if not self.ensure_invest_amount():
                self.output.append("Élő mód nem indult: nincs befektetési összeg.\n")
                return

            self.run_analysis(prompt_amount=False)
            self.live_timer.start(self.live_interval_sec * 1000)
            self.btn_live.setText("Élő mód leállítása")
            self.live_status.setText(f"Élő mód: fut ({self.live_interval_sec}s)")

        def run_live_tick(self):
            self.run_analysis(prompt_amount=False)

        def selected_asset_items(self):
            names = [i.text() for i in self.list_widget.selectedItems()]
            if names:
                return [(n, ASSETS[n]) for n in names if n in ASSETS]
            return list(ASSETS.items())

        def run_analysis(self, prompt_amount=True):
            if self.auto_clear_analysis:
                self.output.clear()

            rate, currency, rate_warning = get_effective_rate()
            if rate_warning:
                self.output.append(f"Figyelem: árfolyam hiba, USD módra váltva. ({rate_warning})\n")

            if prompt_amount:
                amount, ok = QInputDialog.getDouble(
                    self,
                    "Befektetés",
                    f"Mennyit szeretnél ma befektetni? ({currency})",
                    self.invest_amount if self.invest_amount is not None else 100_000.0,
                    0.0,
                    1e15,
                    0,
                )
                if not ok:
                    self.output.append("Elemzés megszakítva (nincs megadott összeg).\n")
                    return
                self.invest_amount = amount

            if self.invest_amount is None:
                self.output.append("Nincs befektetési összeg beállítva.\n")
                return

            has_huf = rate_warning is None and currency == "HUF"
            fx_ctx = get_usdhuf_vs_ma20()
            now_text = QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss")
            self.output.append(f"Frissítve: {now_text} | Deviza: {currency}\n")
            self.output.append("=" * 64 + "\n")
            self.stats_update.setText(f"Utolso frissites: {now_text}")

            results = []
            csv_rows = []

            for name, symbol in self.selected_asset_items():
                try:
                    result = analyze_asset(name, symbol, rate, currency)
                    result["investment"] = evaluate_today_investment(
                        self.invest_amount,
                        currency,
                        has_huf,
                        fx_ctx,
                        result,
                    )
                    results.append(result)
                    self.output.append(format_result(result, currency=currency) + "\n")
                    if self.ai_commentary_enabled and self.ai_api_key:
                        ai_text, ai_err = get_ai_commentary(
                            result=result,
                            currency=currency,
                            api_key=self.ai_api_key,
                            model=self.ai_model,
                        )
                        if ai_text:
                            self.output.append("AI magyarazat:\n" + ai_text + "\n")
                        elif ai_err:
                            self.output.append(f"AI magyarazat hiba: {ai_err}\n")
                    csv_rows.append(
                        {
                            "timestamp": now_text,
                            "asset": name,
                            "currency": currency,
                            "current": f"{float(result['current']):.6f}",
                            "future": f"{float(result['future']):.6f}",
                            "decision": str(result["decision"]),
                            "next_up_prob": f"{float(result['next_up_prob']):.6f}",
                            "model_accuracy": f"{float(result['accuracy']):.6f}",
                            "confidence": f"{float(result['recommendation']['confidence']):.6f}",
                            "invest_amount": f"{float(self.invest_amount):.6f}",
                            "deploy_amount": f"{float(result['investment']['deploy_amount']):.6f}",
                            "deploy_frac": f"{float(result['investment']['deploy_frac']):.6f}",
                        }
                    )

                    current = float(result["current"])
                    prev = self.last_prices_live.get(name)
                    if prev and prev != 0:
                        change_pct = ((current / prev) - 1.0) * 100.0
                        if abs(change_pct) >= self.alert_threshold_pct and self.live_timer.isActive():
                            direction = "emelkedes" if change_pct > 0 else "eses"
                            msg = (
                                f"{name}: {direction} {change_pct:+.2f}% | "
                                f"{prev:,.2f} -> {current:,.2f} {currency}"
                            )
                            self.log_alert(msg)
                            self.trigger_alert_channels(msg)
                    self.last_prices_live[name] = current
                except (requests.RequestException, ValueError, KeyError) as e:
                    self.output.append(f"{name}: hiba — {e}\n\n")
            if results:
                self.update_decision_stats(results)
                self.update_portfolio_tab(results, currency)
                if self.live_timer.isActive() and self.csv_autosave_enabled:
                    try:
                        append_live_csv(self.csv_path, csv_rows)
                    except Exception as e:
                        self.log_alert(f"CSV mentesi hiba: {e}")


def prompt_invest_amount(currency):
    if not sys.stdin.isatty():
        return None
    try:
        raw = input(f"Befektetendő összeg ({currency}): ").strip().replace(" ", "")
        raw = raw.replace(",", ".")
        return float(raw)
    except ValueError:
        return None


def run_cli(selected_assets, invest_amount=None):
    rate, currency, rate_warning = get_effective_rate()
    if rate_warning:
        print(f"Figyelem: árfolyam hiba, USD módra váltva. ({rate_warning})")
        print("-" * 40)

    amount = invest_amount
    if amount is None:
        amount = prompt_invest_amount(currency)
    if amount is None:
        print("Nincs befektetési összeg. Add meg: --osszeg 500000")
        return 1

    has_huf = rate_warning is None and currency == "HUF"
    fx_ctx = get_usdhuf_vs_ma20()

    had_error = False
    for name, symbol in selected_assets:
        try:
            result = analyze_asset(name, symbol, rate, currency)
            result["investment"] = evaluate_today_investment(
                amount,
                currency,
                has_huf,
                fx_ctx,
                result,
            )
            print(format_result(result, currency=currency))
            print("-" * 40)
        except (requests.RequestException, ValueError, KeyError) as e:
            had_error = True
            print(f"{name}: hiba — {e}")
            print("-" * 40)
    return 1 if had_error else 0


def parse_args():
    parser = argparse.ArgumentParser(description="AI alapú eszközelemző (GUI + CLI).")
    parser.add_argument("--cli", action="store_true", help="Futtatás terminál módban.")
    parser.add_argument(
        "--osszeg",
        type=float,
        default=None,
        help="Befektetendő összeg (HUF vagy USD, az aktuális megjelenítés szerint).",
    )
    parser.add_argument(
        "--asset",
        action="append",
        choices=list(ASSETS.keys()),
        help="Elemzendő eszköz neve. Többször is megadható.",
    )
    parser.add_argument("--all", action="store_true", help="Minden eszköz elemzése.")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Folyamatos árfigyelés és értesítés árfolyamváltozásra.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Figyelés lekérdezési időköze másodpercben (alap: 60).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=1.0,
        help="Értesítési küszöb százalékban, pl. 1.5 (alap: 1.0).",
    )
    return parser.parse_args()


def resolve_selected_assets(args):
    if args.all:
        return list(ASSETS.items())
    if args.asset:
        return [(name, ASSETS[name]) for name in args.asset]
    return list(ASSETS.items())


def main():
    configure_cross_platform_stdio()
    args = parse_args()
    selected_assets = resolve_selected_assets(args)

    if args.cli:
        if args.watch:
            sys.exit(
                monitor_price_changes(
                    selected_assets,
                    interval_sec=args.interval,
                    threshold_pct=max(0.01, abs(args.threshold)),
                )
            )
        sys.exit(run_cli(selected_assets, invest_amount=args.osszeg))

    if not QT_AVAILABLE:
        print(
            f"GUI nem indítható (PySide6 hiányzik/hibás: {QT_IMPORT_ERROR}). "
            "Használd: --cli"
        )
        sys.exit(1)

    if sys.platform == "win32":
        os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    app = QApplication(sys.argv)
    window = App()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
