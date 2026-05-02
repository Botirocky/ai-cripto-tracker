import sys
import os
import argparse
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
    from PySide6.QtWidgets import (
        QApplication,
        QWidget,
        QVBoxLayout,
        QPushButton,
        QTextEdit,
        QListWidget,
        QLabel,
        QHBoxLayout,
        QAbstractItemView,
        QInputDialog,
    )
    QT_AVAILABLE = True
    QT_IMPORT_ERROR = None
except Exception as e:
    QT_AVAILABLE = False
    QT_IMPORT_ERROR = e

from sklearn.ensemble import RandomForestClassifier

# ---- ESZKÖZÖK ----
ASSETS = {
    "Bitcoin": "BTC-USD",
    "Arany": "GC=F",
    "Kőolaj": "CL=F",
    "Földgáz": "NG=F",
}

FX_URL = "https://api.exchangerate-api.com/v4/latest/USD"
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


def predict(prices):
    X, y = build_features_and_labels(prices)
    if X.shape[0] < 80:
        raise ValueError("Túl kevés minta a tanításhoz.")

    split = int(X.shape[0] * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    model = RandomForestClassifier(
        n_estimators=300,
        min_samples_leaf=3,
        random_state=42,
    )
    model.fit(X_train, y_train)

    accuracy = float((model.predict(X_test) == y_test).mean()) if y_test.size else 0.0
    next_up_prob = float(model.predict_proba(X[-1].reshape(1, -1))[0][1])

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


def analyze_asset(name, symbol, rate):
    prices = get_asset(symbol) * rate
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
            self.setWindowTitle("Pro AI Trading App")
            self.setGeometry(100, 100, 900, 500)

            layout = QHBoxLayout()

            self.list_widget = QListWidget()
            self.list_widget.setSelectionMode(
                QAbstractItemView.SelectionMode.ExtendedSelection
            )
            for name in ASSETS:
                self.list_widget.addItem(name)
            self.list_widget.setCurrentRow(0)

            layout.addWidget(self.list_widget)

            right = QVBoxLayout()

            self.label = QLabel("AI + Indikátor rendszer — válassz eszközt, vagy Ctrl+A mind")
            right.addWidget(self.label)

            self.output = QTextEdit()
            right.addWidget(self.output)

            self.btn = QPushButton("Elemzés")
            self.btn.clicked.connect(self.run_analysis)
            right.addWidget(self.btn)

            layout.addLayout(right)
            self.setLayout(layout)

        def selected_asset_items(self):
            names = [i.text() for i in self.list_widget.selectedItems()]
            if names:
                return [(n, ASSETS[n]) for n in names if n in ASSETS]
            return list(ASSETS.items())

        def run_analysis(self):
            self.output.clear()

            rate, currency, rate_warning = get_effective_rate()
            if rate_warning:
                self.output.append(f"Figyelem: árfolyam hiba, USD módra váltva. ({rate_warning})\n")

            amount, ok = QInputDialog.getDouble(
                self,
                "Befektetés",
                f"Mennyit szeretnél ma befektetni? ({currency})",
                100_000.0,
                0.0,
                1e15,
                0,
            )
            if not ok:
                self.output.append("Elemzés megszakítva (nincs megadott összeg).\n")
                return

            has_huf = rate_warning is None and currency == "HUF"
            fx_ctx = get_usdhuf_vs_ma20()

            for name, symbol in self.selected_asset_items():
                try:
                    result = analyze_asset(name, symbol, rate)
                    result["investment"] = evaluate_today_investment(
                        amount,
                        currency,
                        has_huf,
                        fx_ctx,
                        result,
                    )
                    self.output.append(format_result(result, currency=currency) + "\n")
                except (requests.RequestException, ValueError, KeyError) as e:
                    self.output.append(f"{name}: hiba — {e}\n\n")


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
            result = analyze_asset(name, symbol, rate)
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
