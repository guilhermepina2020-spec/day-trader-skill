#!/usr/bin/env python3
"""MetaTrader 5 bridge for the Day Trader pack: analysis, sizing, orders and monitoring.

Talks to a running, logged-in MT5 terminal through the official MetaTrader5
Python package (pip install MetaTrader5; Windows only). The indicator math is
standard library and runs without it.

  status     terminal and account: DEMO or REAL, balance, equity, algo trading on/off
  symbols    contract specs: digits, pip, spread, lot min/step, tick value
  analyze    multi-timeframe read of one symbol: EMAs, RSI, ATR, ADX, pivots, swings, bias
  scan       ranks a list of symbols by trend strength and cost (spread vs ATR)
  plan       sizes a market order to your risk in money, runs the risk checks,
             writes a ticket to trader/mt5/orders.json. Sends nothing.
  tickets    shows today's tickets, READY or not
  approve    records your explicit yes ("sim" / "yes") for ONE ticket, word for word
  decline    records a no
  send       sends ONE approved ticket to MT5 (order_check first). Refuses a REAL
             account unless allow_live is true in the config, which only you set.
  positions  open positions with P/L, distance to stop and target
  history    closed deals and today's realized P/L
  close      closes one position (needs your yes in --reply)
  modify     moves the stop and/or target of one position (needs your yes in --reply)
  watch      read-only monitor: prints positions every N seconds, warns near
             stop/target and at the daily loss limit. Never trades.

    python mt5.py status
    python mt5.py analyze EURUSD
    python mt5.py scan --symbols EURUSD GBPUSD AUDUSD
    python mt5.py plan EURUSD --side buy --sl-atr 1.5 --rr 2
    python mt5.py approve --index 1 --reply "sim"
    python mt5.py send --index 1
    python mt5.py watch --every 30 --minutes 60

Times shown are the broker's server time. Config: trader/mt5_config.json
(see mt5_config.example.json). Logs: trader/mt5/log.json (append only).
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys
import time

DEFAULT_CONFIG = {
    "terminal_path": None,
    "symbols": ["EURUSD", "GBPUSD", "AUDUSD"],
    "risk_per_trade": 20.0,
    "max_daily_loss": 60.0,
    "max_trades_per_day": 3,
    "min_reward_risk": 1.5,
    "require_stop": True,
    "max_lots": 1.0,
    "max_spread_atr_pct": 15.0,
    "max_margin_pct_of_free": 50.0,
    "deviation_points": 20,
    "magic": 260101,
    "allow_live": False,
}
TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")
YES_WORDS = ("sim", "s", "yes", "y")
BATCH_WORDS = {"todas", "todos", "tudo", "ambas", "ambos", "all", "every", "everything", "both", "rest"}
DOUBT_WORDS = {"nao", "não", "no", "not", "espera", "espere", "mas", "talvez", "depois", "cancela",
               "cancelar", "se", "wait", "but", "maybe", "later", "if", "unless"}


class Mt5Error(Exception):
    pass


# ---------------------------------------------------------------- indicators (pure, no MT5)

def ema(values, n):
    """Exponential moving average, seeded with the SMA of the first n values."""
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(values[:n]) / float(n)
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes, n=14):
    """Wilder's RSI; the last value, or None without enough bars."""
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + ag / al)


def true_ranges(highs, lows, closes):
    tr = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    return tr


def atr(highs, lows, closes, n=14):
    """Wilder's ATR; the last value, or None."""
    tr = true_ranges(highs, lows, closes)
    if len(tr) < n + 1:
        return None
    a = sum(tr[1:n + 1]) / n
    for x in tr[n + 1:]:
        a = (a * (n - 1) + x) / n
    return a


def adx(highs, lows, closes, n=14):
    """Wilder's ADX with +DI and -DI: (adx, plus_di, minus_di), or (None, None, None)."""
    if len(closes) < 2 * n + 1:
        return None, None, None
    tr = true_ranges(highs, lows, closes)
    pdm, mdm = [0.0], [0.0]
    for i in range(1, len(closes)):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
    s_tr, s_p, s_m = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(mdm[1:n + 1])
    dxs, pdi, mdi = [], 0.0, 0.0
    for i in range(n, len(closes)):
        if i > n:
            s_tr = s_tr - s_tr / n + tr[i]
            s_p = s_p - s_p / n + pdm[i]
            s_m = s_m - s_m / n + mdm[i]
        pdi = 100 * s_p / s_tr if s_tr else 0.0
        mdi = 100 * s_m / s_tr if s_tr else 0.0
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
    a = sum(dxs[:n]) / n
    for x in dxs[n:]:
        a = (a * (n - 1) + x) / n
    return a, pdi, mdi


def pivots(high, low, close):
    """Classic floor pivots from one period's high, low and close."""
    p = (high + low + close) / 3.0
    return {"P": p, "R1": 2 * p - low, "S1": 2 * p - high, "R2": p + (high - low), "S2": p - (high - low)}


def swings(highs, lows, k=2):
    """Fractal swing points: a high (low) higher (lower) than k bars each side. Returns (highs, lows) as (index, price)."""
    sh, sl = [], []
    for i in range(k, len(highs) - k):
        if all(highs[i] > highs[j] for j in range(i - k, i + k + 1) if j != i):
            sh.append((i, highs[i]))
        if all(lows[i] < lows[j] for j in range(i - k, i + k + 1) if j != i):
            sl.append((i, lows[i]))
    return sh, sl


def trend_of(close, e20, e50, e200):
    """+2 strong up, +1 up, 0 sideways, -1 down, -2 strong down (EMA stack and price)."""
    if e20 is None or e50 is None:
        return 0
    if e200 is not None:
        if close > e20 > e50 > e200:
            return 2
        if close < e20 < e50 < e200:
            return -2
    if close > e20 > e50:
        return 1
    if close < e20 < e50:
        return -1
    return 0


TREND_WORDS = {2: "ALTA forte", 1: "alta", 0: "lateral", -1: "baixa", -2: "BAIXA forte"}


def read_bars(bars):
    """Bars (dicts or numpy records with open/high/low/close/time) -> dict of lists."""
    out = {"time": [], "open": [], "high": [], "low": [], "close": []}
    for b in bars:
        for k in out:
            out[k].append(float(b[k]) if k != "time" else int(b[k]))
    return out


def tf_stats(b):
    c, h, l = b["close"], b["high"], b["low"]
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    a, pdi, mdi = adx(h, l, c)
    last = c[-1] if c else None
    return {"close": last, "ema20": e20[-1] if c else None, "ema50": e50[-1] if c else None,
            "ema200": e200[-1] if c else None, "rsi": rsi(c), "atr": atr(h, l, c),
            "adx": a, "plus_di": pdi, "minus_di": mdi,
            "trend": trend_of(last, e20[-1], e50[-1], e200[-1]) if c else 0}


# ---------------------------------------------------------------- sizing and the yes (pure)

def lots_for_risk(risk_money, loss_per_lot, vmin, vstep, vmax, cap=None):
    """Largest lot size, a multiple of vstep, whose loss at the stop stays within risk_money.
    Returns (lots, why) with lots 0 when even the minimum lot risks too much."""
    if loss_per_lot <= 0:
        raise Mt5Error("the loss per lot at the stop must be positive")
    raw = risk_money / loss_per_lot
    steps = math.floor(raw / vstep + 1e-9)
    lots = round(steps * vstep, 8)
    limit = min(vmax, cap) if cap else vmax
    why = "risco %.2f / perda por lote %.2f = %.4f lotes" % (risk_money, loss_per_lot, raw)
    if lots > limit:
        lots, why = limit, why + ", limitado a %s" % fmt_lots(limit, vstep)
    if lots < vmin - 1e-9:
        return 0.0, why + ", abaixo do lote minimo %s" % fmt_lots(vmin, vstep)
    return lots, why


def fmt_lots(v, step=0.01):
    dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return ("%." + str(dec) + "f") % v


def parse_yes(reply, symbol, numbers=(), other_symbols=()):
    """Is this reply an explicit yes ("sim" / "yes") for THIS one ticket? (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "a resposta esta vazia"
    if "?" in raw:
        return False, "a resposta e uma pergunta, nao um sim"
    words = re.findall(r"[a-zà-ú']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "so vale uma resposta que comeca com \"sim\" (ou \"yes\"), nao ok, pode, manda"
    if BATCH_WORDS & set(words):
        return False, "um sim vale para um ticket por vez, nao para varios"
    if DOUBT_WORDS & set(words[1:]):
        return False, "a resposta tem condicao ou duvida; pergunte de novo"
    sym = symbol.upper()
    named = set(re.findall(r"[A-Z]{6}", raw.upper())) & ({s.upper() for s in other_symbols} - {sym})
    if named:
        return False, "a resposta cita %s, mas o ticket e de %s" % (", ".join(sorted(named)), sym)
    nums = [float(n.replace(",", ".")) for n in re.findall(r"\d+(?:[.,]\d+)?", raw)]
    if nums and numbers:
        if not any(abs(n - w) < 1e-6 for n in nums for w in numbers):
            return False, "a resposta cita %s, que nao bate com este ticket" % ", ".join("%g" % n for n in nums)
    return True, "sim explicito para %s" % sym


def fingerprint(order):
    core = {k: order.get(k) for k in ("id", "symbol", "side", "volume", "sl", "tp")}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- files

def read_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise Mt5Error("%s nao e JSON valido: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(read_json(path, {}) or {})
    return cfg


def today_local():
    return datetime.date.today().isoformat()


def now_local():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def append_log(path, rec):
    log = read_json(path, {"events": []})
    rec = dict({"at": now_local(), "date": today_local()}, **rec)
    log.setdefault("events", []).append(rec)
    write_json(path, log)
    return rec


# ---------------------------------------------------------------- MT5 connection

_mt5 = None


def mt5mod():
    global _mt5
    if _mt5 is None:
        try:
            import MetaTrader5
        except ImportError:
            raise Mt5Error("o pacote MetaTrader5 nao esta instalado: python -m pip install MetaTrader5")
        _mt5 = MetaTrader5
    return _mt5


def connect(cfg):
    mt5 = mt5mod()
    ok = mt5.initialize(path=cfg["terminal_path"]) if cfg.get("terminal_path") else mt5.initialize()
    if not ok:
        raise Mt5Error("nao conectou ao terminal MT5 (%s). Abra o MetaTrader 5 e faca login na conta."
                       % (mt5.last_error(),))
    acct = mt5.account_info()
    if acct is None:
        raise Mt5Error("o terminal esta aberto mas sem conta logada. Faca login no MT5.")
    return mt5


def account_kind(mt5, acct):
    if acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO:
        return "DEMO"
    if acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_CONTEST:
        return "CONTEST"
    return "REAL"


def resolve_symbol(mt5, name):
    """EURUSD -> the broker's own name (EURUSD, EURUSD.r, EURUSDm...), added to Market Watch."""
    info = mt5.symbol_info(name)
    if info is None:
        cands = mt5.symbols_get("*%s*" % name.upper()) or []
        cands = [s for s in cands if s.name.upper().startswith(name.upper())]
        if not cands:
            raise Mt5Error("simbolo %s nao existe nesta corretora" % name)
        info = sorted(cands, key=lambda s: len(s.name))[0]
    if not info.visible:
        mt5.symbol_select(info.name, True)
        info = mt5.symbol_info(info.name)
    return info


def pip_size(info):
    return info.point * 10 if info.digits in (3, 5) else info.point


def bars(mt5, symbol, tf, count):
    if tf not in TIMEFRAMES:
        raise Mt5Error("timeframe %s invalido; use %s" % (tf, ", ".join(TIMEFRAMES)))
    rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, "TIMEFRAME_" + tf), 0, count)
    if rates is None or len(rates) == 0:
        raise Mt5Error("sem candles de %s %s (%s)" % (symbol, tf, mt5.last_error()))
    return read_bars(rates)


def server_offset(mt5, symbol):
    """Seconds the broker's server clock runs ahead of UTC, from a fresh tick (rounded to 30 min)."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or abs(tick.time - time.time()) > 15 * 3600:
        return 0
    return int(round((tick.time - time.time()) / 1800.0)) * 1800


def server_day_start(mt5, symbol):
    """Server-clock epoch of today's server midnight (deal times use the server clock)."""
    now_srv = time.time() + server_offset(mt5, symbol)
    return int(now_srv // 86400 * 86400)


def deals_today(mt5, symbol_hint):
    start = server_day_start(mt5, symbol_hint)
    frm = datetime.datetime.now() - datetime.timedelta(days=3)
    to = datetime.datetime.now() + datetime.timedelta(days=1)
    deals = mt5.history_deals_get(frm, to) or []
    return [d for d in deals if d.time >= start]


def realized_today(mt5, symbol_hint):
    return sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
               for d in deals_today(mt5, symbol_hint) if d.type in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL))


def entries_today(mt5, symbol_hint):
    return sum(1 for d in deals_today(mt5, symbol_hint)
               if d.entry == mt5.DEAL_ENTRY_IN and d.type in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL))


def floating(mt5):
    return sum(p.profit + p.swap for p in (mt5.positions_get() or []))


def open_risk(mt5):
    """Money lost if every open position hit its stop now (positions without a stop count as None)."""
    total, unknown = 0.0, 0
    for p in mt5.positions_get() or []:
        if not p.sl:
            unknown += 1
            continue
        kind = mt5.ORDER_TYPE_BUY if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_SELL
        pl = mt5.order_calc_profit(kind, p.symbol, p.volume, p.price_open, p.sl)
        if pl is not None and pl < 0:
            total += -pl
    return total, unknown


def filling_mode(mt5, info):
    fm = info.filling_mode
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def srv_time(epoch):
    return (datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(epoch))).strftime("%Y-%m-%d %H:%M")


def px(info, v):
    return ("%." + str(info.digits) + "f") % v


# ---------------------------------------------------------------- commands: read only

def cmd_status(a, cfg):
    mt5 = connect(cfg)
    acct, term = mt5.account_info(), mt5.terminal_info()
    kind = account_kind(mt5, acct)
    print("Conta %s  #%s  %s  servidor %s" % (kind, acct.login, acct.name, acct.server))
    print("  saldo %.2f %s   patrimonio %.2f   margem livre %.2f   alavancagem 1:%s"
          % (acct.balance, acct.currency, acct.equity, acct.margin_free, acct.leverage))
    print("  terminal %s build %s, conectado: %s" % (term.name, term.build, "sim" if term.connected else "NAO"))
    print("  AlgoTrading (botao Algo Trading no MT5): %s" % ("ligado" if term.trade_allowed else
          "DESLIGADO: ligue para poder enviar ordens"))
    print("  negociacao permitida na conta: %s" % ("sim" if acct.trade_allowed else "NAO"))
    if kind == "REAL":
        print("  ATENCAO: conta REAL. send recusa ordens enquanto allow_live for false no config.")
    return 0


def cmd_symbols(a, cfg):
    mt5 = connect(cfg)
    print("%-10s %6s %8s %8s %8s %8s %10s %10s" % ("simbolo", "digits", "pip", "spread", "lot min",
                                                   "lot step", "tick val", "contrato"))
    for s in a.symbols or cfg["symbols"]:
        info = resolve_symbol(mt5, s)
        tick = mt5.symbol_info_tick(info.name)
        spread_pips = (tick.ask - tick.bid) / pip_size(info) if tick else float("nan")
        print("%-10s %6d %8s %8.1f %8s %8s %10.4f %10s" % (info.name, info.digits, px(info, pip_size(info)),
                                                          spread_pips, info.volume_min, info.volume_step,
                                                          info.trade_tick_value, info.trade_contract_size))
    print("spread em pips. tick val = valor de 1 tick para 1 lote, na moeda da conta.")
    return 0


def analysis(mt5, info, entry_tf):
    """Everything analyze and scan need for one symbol."""
    out = {"symbol": info.name, "tf": {}}
    for tf in ("D1", "H4", "H1", "M15", "M5"):
        out["tf"][tf] = tf_stats(bars(mt5, info.name, tf, 400))
    d1 = bars(mt5, info.name, "D1", 3)
    out["prev_day"] = {"high": d1["high"][-2], "low": d1["low"][-2], "close": d1["close"][-2]}
    out["pivots"] = pivots(d1["high"][-2], d1["low"][-2], d1["close"][-2])
    h1 = bars(mt5, info.name, "H1", 200)
    sh, sl = swings(h1["high"], h1["low"])
    tick = mt5.symbol_info_tick(info.name)
    out["bid"], out["ask"] = tick.bid, tick.ask
    mid = (tick.bid + tick.ask) / 2
    out["swing_above"] = min([p for _, p in sh if p > mid], default=None)
    out["swing_below"] = max([p for _, p in sl if p < mid], default=None)
    pip = pip_size(info)
    out["pip"] = pip
    out["spread_pips"] = (tick.ask - tick.bid) / pip
    atr_e = out["tf"][entry_tf]["atr"]
    out["atr_entry_pips"] = atr_e / pip if atr_e else None
    out["spread_atr_pct"] = 100 * (tick.ask - tick.bid) / atr_e if atr_e else None
    weights = {"D1": 1, "H4": 2, "H1": 2, "M15": 1, "M5": 0.5}
    out["score"] = sum(weights[k] * v["trend"] for k, v in out["tf"].items())
    return out


def bias_words(score):
    if score >= 8:
        return "COMPRADOR forte"
    if score >= 3:
        return "comprador"
    if score <= -8:
        return "VENDEDOR forte"
    if score <= -3:
        return "vendedor"
    return "neutro / sem direcao clara"


def cmd_analyze(a, cfg):
    mt5 = connect(cfg)
    info = resolve_symbol(mt5, a.symbol)
    r = analysis(mt5, info, a.tf)
    if a.json:
        print(json.dumps(r, indent=2))
        return 0
    p = lambda v: px(info, v) if v is not None else "-"
    print("%s  bid %s  ask %s  spread %.1f pips   (hora do servidor %s)"
          % (info.name, p(r["bid"]), p(r["ask"]), r["spread_pips"], srv_time(mt5.symbol_info_tick(info.name).time)))
    print("")
    print("%-4s %-12s %10s %10s %10s %10s %6s %8s %6s %6s %6s" % ("tf", "tendencia", "preco", "EMA20", "EMA50",
                                                               "EMA200", "RSI", "ATR pip", "ADX", "+DI", "-DI"))
    for tf, s in r["tf"].items():
        f1 = lambda v: "%.1f" % v if v is not None else "-"
        print("%-4s %-12s %10s %10s %10s %10s %6s %8s %6s %6s %6s"
              % (tf, TREND_WORDS[s["trend"]], p(s["close"]), p(s["ema20"]), p(s["ema50"]), p(s["ema200"]),
                 f1(s["rsi"]), f1(s["atr"] / r["pip"] if s["atr"] else None), f1(s["adx"]),
                 f1(s["plus_di"]), f1(s["minus_di"])))
    pv, pd = r["pivots"], r["prev_day"]
    print("")
    print("Dia anterior: max %s  min %s  fech %s" % (p(pd["high"]), p(pd["low"]), p(pd["close"])))
    print("Pivots: S2 %s  S1 %s  P %s  R1 %s  R2 %s" % (p(pv["S2"]), p(pv["S1"]), p(pv["P"]), p(pv["R1"]), p(pv["R2"])))
    print("Swing H1 mais proximo: acima %s  abaixo %s" % (p(r["swing_above"]), p(r["swing_below"])))
    print("")
    print("Vies multi-timeframe: %s (pontuacao %+.1f de -13 a +13)" % (bias_words(r["score"]), r["score"]))
    e = r["tf"][a.tf]
    notes = []
    if e["adx"] is not None:
        notes.append("ADX %s %.0f: %s" % (a.tf, e["adx"], "tendencia com forca" if e["adx"] >= 25 else
                                          "tendencia fraca / range" if e["adx"] < 20 else "forca moderada"))
    if e["rsi"] is not None:
        if e["rsi"] >= 70:
            notes.append("RSI %s %.0f: sobrecomprado, cuidado com compras atrasadas" % (a.tf, e["rsi"]))
        elif e["rsi"] <= 30:
            notes.append("RSI %s %.0f: sobrevendido, cuidado com vendas atrasadas" % (a.tf, e["rsi"]))
    if r["spread_atr_pct"] is not None:
        notes.append("custo: spread = %.0f%% do ATR %s (limite do config %.0f%%)"
                     % (r["spread_atr_pct"], a.tf, cfg["max_spread_atr_pct"]))
    if e["atr"] and e["ema20"] is not None:
        dist = abs(e["close"] - e["ema20"]) / e["atr"]
        notes.append("preco a %.1f ATR da EMA20 %s (%s)" % (dist, a.tf, "pullback / perto da media" if dist < 0.5
                                                            else "esticado" if dist > 2 else "normal"))
    for n in notes:
        print("  - " + n)
    if e["atr"]:
        stop_pips = 1.5 * e["atr"] / r["pip"]
        print("  - stop tecnico sugerido: 1.5 x ATR %s = %.1f pips" % (a.tf, stop_pips))
    print("")
    print("Leitura tecnica automatica, nao e recomendacao financeira.")
    return 0


def cmd_scan(a, cfg):
    mt5 = connect(cfg)
    rows = []
    for s in a.symbols or cfg["symbols"]:
        try:
            info = resolve_symbol(mt5, s)
            r = analysis(mt5, info, a.tf)
        except Mt5Error as e:
            print("%s: %s" % (s, e))
            continue
        h1 = r["tf"]["H1"]
        rows.append((abs(r["score"]), info.name, r, h1))
    rows.sort(key=lambda x: (-x[0], x[2]["spread_atr_pct"] or 99))
    print("%-10s %-22s %6s %7s %9s %9s %10s" % ("simbolo", "vies", "score", "ADX H1", "ATR pip", "spread",
                                                "spread/ATR"))
    for _, name, r, h1 in rows:
        sa = r["spread_atr_pct"]
        flag = "" if sa is None or sa <= cfg["max_spread_atr_pct"] else "  caro"
        print("%-10s %-22s %+6.1f %7s %9.1f %9.1f %9.0f%%%s"
              % (name, bias_words(r["score"]), r["score"], "%.0f" % h1["adx"] if h1["adx"] else "-",
                 r["atr_entry_pips"] or 0, r["spread_pips"], sa or 0, flag))
    print("ATR do %s. Ordenado pela forca da tendencia (|score|), depois pelo custo. Nao e recomendacao financeira."
          % a.tf)
    return 0


# ---------------------------------------------------------------- commands: tickets

def orders_doc(a):
    return read_json(a.orders, {"orders": []})


def todays(doc):
    return [o for o in doc.get("orders", []) if o.get("date") == today_local()]


def last_event(log, fp):
    found = None
    for e in log.get("events", []):
        if e.get("fingerprint") == fp and e.get("date") == today_local():
            found = e
    return found


def ticket_status(order, log):
    if order.get("verdict") != "PASS":
        return False, "BLOQUEADO: " + "; ".join(order.get("blocks", []))
    ev = last_event(log, order["fingerprint"])
    if ev is None:
        return True, "PRONTO: checagem de risco PASS, aguardando seu sim"
    if ev["status"] == "approved":
        return True, "APROVADO por voce as %s, aguardando send" % ev["at"][11:16]
    return False, "FEITO: %s as %s" % (ev["status"], ev["at"][11:16])


def render_ticket(o, idx, log, kind):
    ok, label = ticket_status(o, log)
    d = o["digits"]
    f = lambda v: ("%." + str(d) + "f") % v
    lines = ["TICKET %d, conta %s, %s" % (idx, kind or o.get("account"), o["created_at"][11:16]),
             "  %s %s lotes %s, a mercado (agora ~%s)" % (o["side"].upper(), o["volume_text"], o["symbol"], f(o["entry"])),
             "  stop-loss %s (%.1f pips)   take-profit %s (%.1f pips)   R:R 1:%.2f"
             % (f(o["sl"]), o["sl_pips"], f(o["tp"]), o["tp_pips"], o["rr"]),
             "  perda maxima ~%.2f %s se o stop executar em %s; ganho no alvo ~%.2f"
             % (o["loss_at_sl"], o["currency"], f(o["sl"]), o["gain_at_tp"]),
             "  margem ~%.2f   motivo: %s" % (o["margin"], o.get("why") or "-")]
    for w in o.get("warnings", []):
        lines.append("  aviso: " + w)
    lines.append("  " + label)
    return "\n".join(lines), ok


def cmd_plan(a, cfg):
    mt5 = connect(cfg)
    acct = mt5.account_info()
    kind = account_kind(mt5, acct)
    info = resolve_symbol(mt5, a.symbol)
    tick = mt5.symbol_info_tick(info.name)
    side = a.side.lower()
    entry = tick.ask if side == "buy" else tick.bid
    pip = pip_size(info)
    sgn = 1 if side == "buy" else -1
    b = bars(mt5, info.name, a.tf, 200)
    atr_v = atr(b["high"], b["low"], b["close"])

    if a.sl is not None:
        sl = a.sl
    elif a.sl_pips is not None:
        sl = entry - sgn * a.sl_pips * pip
    elif a.sl_atr is not None:
        if not atr_v:
            raise Mt5Error("sem ATR para calcular o stop")
        sl = entry - sgn * a.sl_atr * atr_v
    else:
        raise Mt5Error("diga onde fica o stop: --sl PRECO, --sl-pips N ou --sl-atr MULT")
    sl = round(sl, info.digits)
    if (entry - sl) * sgn <= 0:
        raise Mt5Error("o stop %s esta do lado errado do preco %s para %s" % (px(info, sl), px(info, entry), side))
    risk_dist = abs(entry - sl)
    if a.tp is not None:
        tp = a.tp
    else:
        tp = entry + sgn * (a.rr or max(cfg["min_reward_risk"], 2.0)) * risk_dist
    tp = round(tp, info.digits)
    if (tp - entry) * sgn <= 0:
        raise Mt5Error("o alvo %s esta do lado errado do preco %s" % (px(info, tp), px(info, entry)))
    otype = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    pl = mt5.order_calc_profit(otype, info.name, 1.0, entry, sl)
    if pl is None:
        raise Mt5Error("order_calc_profit falhou: %s" % (mt5.last_error(),))
    risk_money = a.risk if a.risk is not None else cfg["risk_per_trade"]
    lots, size_why = lots_for_risk(risk_money, abs(pl), info.volume_min, info.volume_step, info.volume_max,
                                   cfg.get("max_lots"))
    if a.lots is not None:
        lots, size_why = a.lots, "lotes informados por voce"

    blocks, warnings = [], []
    if lots <= 0:
        blocks.append("o lote minimo %s ja arrisca %.2f, mais que %.2f" % (info.volume_min, abs(pl) * info.volume_min,
                                                                          risk_money))
        lots = info.volume_min
    loss = abs(mt5.order_calc_profit(otype, info.name, lots, entry, sl) or 0)
    gain = abs(mt5.order_calc_profit(otype, info.name, lots, entry, tp) or 0)
    margin = mt5.order_calc_margin(otype, info.name, lots, entry) or 0.0
    rr = abs(tp - entry) / risk_dist
    if kind == "REAL" and not cfg.get("allow_live"):
        blocks.append("conta REAL e allow_live e false no config")
    if loss > cfg["risk_per_trade"] + 1e-6:
        blocks.append("perda no stop %.2f passa do limite por trade %.2f" % (loss, cfg["risk_per_trade"]))
    if rr + 1e-9 < cfg["min_reward_risk"]:
        blocks.append("R:R 1:%.2f abaixo do minimo 1:%.2f" % (rr, cfg["min_reward_risk"]))
    if lots > cfg["max_lots"] + 1e-9:
        blocks.append("%s lotes passa do maximo %s" % (lots, cfg["max_lots"]))
    stops = info.trade_stops_level * info.point
    if stops and (risk_dist < stops or abs(tp - entry) < stops):
        blocks.append("stop ou alvo mais perto que o minimo da corretora (%d pontos)" % info.trade_stops_level)
    spread = tick.ask - tick.bid
    if atr_v:
        sa = 100 * spread / atr_v
        if sa > cfg["max_spread_atr_pct"]:
            blocks.append("spread = %.0f%% do ATR %s, acima de %.0f%%" % (sa, a.tf, cfg["max_spread_atr_pct"]))
        if risk_dist < atr_v * 0.5:
            warnings.append("stop menor que meio ATR %s: facil de ser violado por ruido" % a.tf)
    n_today = entries_today(mt5, info.name)
    if n_today >= cfg["max_trades_per_day"]:
        blocks.append("ja foram %d entradas hoje (maximo %d)" % (n_today, cfg["max_trades_per_day"]))
    lost = -min(0.0, realized_today(mt5, info.name))
    at_risk, no_stop = open_risk(mt5)
    if lost + at_risk + loss > cfg["max_daily_loss"] + 1e-6:
        blocks.append("perda do dia %.2f + risco aberto %.2f + este trade %.2f passa do limite diario %.2f"
                      % (lost, at_risk, loss, cfg["max_daily_loss"]))
    if no_stop:
        warnings.append("%d posicao(oes) aberta(s) sem stop" % no_stop)
    if margin > acct.margin_free * cfg["max_margin_pct_of_free"] / 100.0:
        blocks.append("margem %.2f passa de %.0f%% da margem livre" % (margin, cfg["max_margin_pct_of_free"]))
    if info.name.upper()[:6] not in [s.upper()[:6] for s in cfg["symbols"]]:
        warnings.append("%s nao esta na lista symbols do config" % info.name)

    doc = orders_doc(a)
    oid = "%s-%d" % (today_local(), len(doc.get("orders", [])) + 1)
    order = {"id": oid, "date": today_local(), "created_at": now_local(), "account": kind,
             "login": acct.login, "symbol": info.name, "side": side, "volume": lots,
             "volume_text": fmt_lots(lots, info.volume_step), "entry": entry, "sl": sl, "tp": tp,
             "digits": info.digits, "sl_pips": risk_dist / pip, "tp_pips": abs(tp - entry) / pip, "rr": rr,
             "loss_at_sl": round(loss, 2), "gain_at_tp": round(gain, 2), "margin": round(margin, 2),
             "currency": acct.currency, "sizing": size_why, "why": a.why or "",
             "verdict": "BLOCK" if blocks else "PASS", "blocks": blocks, "warnings": warnings}
    order["fingerprint"] = fingerprint(order)
    doc.setdefault("orders", []).append(order)
    write_json(a.orders, doc)
    idx = len(todays(doc))
    append_log(a.log, {"status": "planned", "fingerprint": order["fingerprint"], "id": oid,
                       "symbol": info.name, "side": side, "volume": lots, "verdict": order["verdict"]})
    text, _ = render_ticket(order, idx, read_json(a.log, {"events": []}), kind)
    print(text)
    print("  dimensionamento: " + size_why)
    if not blocks:
        print("\nPara enviar: o usuario responde \"sim\" a ESTE ticket, depois approve --index %d e send --index %d."
              % (idx, idx))
    print("Nao e recomendacao financeira.")
    return 0 if not blocks else 1


def _pick(a):
    orders = todays(orders_doc(a))
    if not orders:
        raise Mt5Error("nenhum ticket hoje; rode plan primeiro")
    if not 1 <= a.index <= len(orders):
        raise Mt5Error("nao existe o ticket %d (hoje ha %d)" % (a.index, len(orders)))
    return orders[a.index - 1], orders


def cmd_tickets(a, cfg):
    orders = todays(orders_doc(a))
    if not orders:
        print("Nenhum ticket hoje.")
        return 1
    log = read_json(a.log, {"events": []})
    for i, o in enumerate(orders, 1):
        print(render_ticket(o, i, log, None)[0])
        print("")
    print("Um sim vale para um ticket. Nao e recomendacao financeira.")
    return 0


def cmd_approve(a, cfg):
    order, orders = _pick(a)
    log = read_json(a.log, {"events": []})
    ok, label = ticket_status(order, log)
    ev = last_event(log, order["fingerprint"])
    if not ok or (ev and ev["status"] == "approved"):
        raise Mt5Error("ticket %d nao pode ser aprovado: %s" % (a.index, label))
    nums = (order["volume"], order["sl"], order["tp"], order["loss_at_sl"], a.index)
    yes, why = parse_yes(a.reply, order["symbol"][:6], nums, [o["symbol"][:6] for o in orders])
    if not yes:
        print("Nao registrado: %s. Pergunte de novo mostrando o ticket %d." % (why, a.index))
        return 1
    append_log(a.log, {"status": "approved", "fingerprint": order["fingerprint"], "id": order["id"],
                       "symbol": order["symbol"], "reply": a.reply})
    print("Registrado seu sim para o ticket %d: %s %s %s. Agora: send --index %d"
          % (a.index, order["side"].upper(), order["volume_text"], order["symbol"], a.index))
    return 0


def cmd_decline(a, cfg):
    order, _ = _pick(a)
    append_log(a.log, {"status": "declined", "fingerprint": order["fingerprint"], "id": order["id"],
                       "symbol": order["symbol"], "reply": a.reply or "nao"})
    print("Registrado: nao para o ticket %d. Nada foi enviado." % a.index)
    return 0


def cmd_send(a, cfg):
    order, _ = _pick(a)
    log = read_json(a.log, {"events": []})
    ev = last_event(log, order["fingerprint"])
    if not ev or ev["status"] != "approved":
        raise Mt5Error("o ticket %d nao tem sim registrado hoje (approve primeiro)" % a.index)
    mt5 = connect(cfg)
    acct = mt5.account_info()
    kind = account_kind(mt5, acct)
    if kind == "REAL" and not cfg.get("allow_live"):
        raise Mt5Error("conta REAL e allow_live e false: nada enviado")
    if acct.login != order["login"]:
        raise Mt5Error("o ticket foi feito na conta %s, o terminal esta na %s" % (order["login"], acct.login))
    if not mt5.terminal_info().trade_allowed:
        raise Mt5Error("o botao Algo Trading do MT5 esta desligado: ligue e tente de novo")
    info = resolve_symbol(mt5, order["symbol"])
    tick = mt5.symbol_info_tick(info.name)
    buy = order["side"] == "buy"
    price = tick.ask if buy else tick.bid
    otype = mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL
    if (price - order["sl"]) * (1 if buy else -1) <= 0 or (order["tp"] - price) * (1 if buy else -1) <= 0:
        raise Mt5Error("o preco %s ja passou do stop ou do alvo; refaca o plano" % px(info, price))
    loss_now = abs(mt5.order_calc_profit(otype, info.name, order["volume"], price, order["sl"]) or 0)
    if loss_now > cfg["risk_per_trade"] * 1.2:
        raise Mt5Error("o preco andou: a perda no stop agora seria %.2f (limite %.2f); refaca o plano"
                       % (loss_now, cfg["risk_per_trade"]))
    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": info.name, "volume": float(order["volume"]),
           "type": otype, "price": price, "sl": order["sl"], "tp": order["tp"],
           "deviation": int(cfg["deviation_points"]), "magic": int(cfg["magic"]),
           "comment": "claude %s" % order["id"], "type_time": mt5.ORDER_TIME_GTC,
           "type_filling": filling_mode(mt5, info)}
    chk = mt5.order_check(req)
    if chk is None or chk.retcode not in (0, mt5.TRADE_RETCODE_DONE):
        why = chk.comment if chk is not None else mt5.last_error()
        append_log(a.log, {"status": "failed", "fingerprint": order["fingerprint"], "id": order["id"],
                           "symbol": info.name, "note": "order_check: %s" % (why,)})
        raise Mt5Error("order_check recusou: %s" % (why,))
    res = mt5.order_send(req)
    if res is None or res.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
        why = "%s %s" % (res.retcode, res.comment) if res is not None else mt5.last_error()
        append_log(a.log, {"status": "failed", "fingerprint": order["fingerprint"], "id": order["id"],
                           "symbol": info.name, "note": str(why)})
        raise Mt5Error("order_send falhou: %s. Nao reenvie sem novo plano e novo sim." % (why,))
    append_log(a.log, {"status": "sent", "fingerprint": order["fingerprint"], "id": order["id"],
                       "symbol": info.name, "side": order["side"], "volume": res.volume, "price": res.price,
                       "deal": res.deal, "order": res.order, "account": kind})
    print("ENVIADA (%s): %s %s %s a %s, ordem #%s, deal #%s. Stop %s, alvo %s."
          % (kind, order["side"].upper(), res.volume, info.name, px(info, res.price), res.order, res.deal,
             px(info, order["sl"]), px(info, order["tp"])))
    return 0


# ---------------------------------------------------------------- commands: positions

def position_rows(mt5):
    rows = []
    for p in mt5.positions_get() or []:
        info = mt5.symbol_info(p.symbol)
        pip = pip_size(info)
        buy = p.type == mt5.POSITION_TYPE_BUY
        sgn = 1 if buy else -1
        rows.append({"p": p, "info": info, "side": "BUY" if buy else "SELL",
                     "pips": (p.price_current - p.price_open) * sgn / pip,
                     "to_sl": abs(p.price_current - p.sl) / pip if p.sl else None,
                     "to_tp": abs(p.tp - p.price_current) / pip if p.tp else None})
    return rows


def print_positions(mt5):
    rows = position_rows(mt5)
    if not rows:
        print("Nenhuma posicao aberta.")
        return rows
    print("%-10s %-10s %-4s %6s %10s %10s %10s %10s %8s %9s %9s"
          % ("ticket", "simbolo", "lado", "lotes", "abertura", "atual", "stop", "alvo", "pips", "P/L", "ate stop"))
    for r in rows:
        p, info = r["p"], r["info"]
        print("%-10s %-10s %-4s %6s %10s %10s %10s %10s %+8.1f %+9.2f %9s"
              % (p.ticket, p.symbol, r["side"], p.volume, px(info, p.price_open), px(info, p.price_current),
                 px(info, p.sl) if p.sl else "SEM", px(info, p.tp) if p.tp else "-", r["pips"], p.profit + p.swap,
                 "%.1f" % r["to_sl"] if r["to_sl"] is not None else "-"))
    return rows


def cmd_positions(a, cfg):
    mt5 = connect(cfg)
    acct = mt5.account_info()
    print_positions(mt5)
    hint = cfg["symbols"][0]
    print("Patrimonio %.2f %s, P/L aberto %+.2f, realizado hoje %+.2f"
          % (acct.equity, acct.currency, floating(mt5), realized_today(mt5, resolve_symbol(mt5, hint).name)))
    return 0


def cmd_history(a, cfg):
    mt5 = connect(cfg)
    frm = datetime.datetime.now() - datetime.timedelta(days=a.days)
    deals = [d for d in (mt5.history_deals_get(frm, datetime.datetime.now() + datetime.timedelta(days=1)) or [])
             if d.type in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL)]
    if not deals:
        print("Nenhum negocio nos ultimos %d dia(s)." % a.days)
        return 1
    total = 0.0
    for d in deals:
        res = d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
        total += res
        print("%s  %-10s %-4s %-3s %6s a %-10s %+9.2f  %s"
              % (srv_time(d.time), d.symbol, "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
                 "IN" if d.entry == mt5.DEAL_ENTRY_IN else "OUT", d.volume, d.price, res, d.comment))
    print("Resultado: %+.2f %s; realizado hoje (dia do servidor): %+.2f"
          % (total, mt5.account_info().currency, realized_today(mt5, resolve_symbol(mt5, cfg["symbols"][0]).name)))
    return 0


def _position(mt5, ticket):
    ps = mt5.positions_get(ticket=ticket)
    if not ps:
        raise Mt5Error("nao ha posicao aberta com ticket %s" % ticket)
    return ps[0]


def cmd_close(a, cfg):
    mt5 = connect(cfg)
    p = _position(mt5, a.ticket)
    vol = a.volume or p.volume
    yes, why = parse_yes(a.reply, p.symbol[:6], (a.ticket, vol))
    if not yes:
        print("Nao fechado: %s." % why)
        return 1
    info = resolve_symbol(mt5, p.symbol)
    tick = mt5.symbol_info_tick(p.symbol)
    buy = p.type == mt5.POSITION_TYPE_BUY
    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": float(vol), "position": p.ticket,
           "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, "price": tick.bid if buy else tick.ask,
           "deviation": int(cfg["deviation_points"]), "magic": int(cfg["magic"]), "comment": "claude close",
           "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling_mode(mt5, info)}
    res = mt5.order_send(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        raise Mt5Error("fechamento falhou: %s" % ("%s %s" % (res.retcode, res.comment) if res else mt5.last_error()))
    append_log(a.log, {"status": "closed", "position": p.ticket, "symbol": p.symbol, "volume": vol,
                       "price": res.price, "reply": a.reply})
    print("FECHADA: posicao %s %s %s lotes a %s." % (p.ticket, p.symbol, vol, px(info, res.price)))
    return 0


def cmd_modify(a, cfg):
    mt5 = connect(cfg)
    p = _position(mt5, a.ticket)
    sl = a.sl if a.sl is not None else p.sl
    tp = a.tp if a.tp is not None else p.tp
    yes, why = parse_yes(a.reply, p.symbol[:6], tuple(v for v in (a.ticket, a.sl, a.tp) if v is not None))
    if not yes:
        print("Nao alterado: %s." % why)
        return 1
    buy = p.type == mt5.POSITION_TYPE_BUY
    if p.sl and sl and ((buy and sl < p.sl) or (not buy and sl > p.sl)) and not a.widen:
        raise Mt5Error("isso afasta o stop (aumenta o risco). Se for mesmo isso, use --widen")
    req = {"action": mt5.TRADE_ACTION_SLTP, "symbol": p.symbol, "position": p.ticket, "sl": sl, "tp": tp,
           "magic": int(cfg["magic"])}
    res = mt5.order_send(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        raise Mt5Error("alteracao falhou: %s" % ("%s %s" % (res.retcode, res.comment) if res else mt5.last_error()))
    append_log(a.log, {"status": "modified", "position": p.ticket, "symbol": p.symbol, "sl": sl, "tp": tp,
                       "reply": a.reply})
    info = mt5.symbol_info(p.symbol)
    print("ALTERADA: posicao %s, stop %s, alvo %s." % (p.ticket, px(info, sl) if sl else "-", px(info, tp) if tp else "-"))
    return 0


def cmd_watch(a, cfg):
    mt5 = connect(cfg)
    hint = resolve_symbol(mt5, cfg["symbols"][0]).name
    end = time.time() + a.minutes * 60
    warned = set()
    while True:
        acct = mt5.account_info()
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        rows = position_rows(mt5)
        day = realized_today(mt5, hint) + floating(mt5)
        print("[%s] patrimonio %.2f  P/L dia %+.2f  posicoes %d" % (stamp, acct.equity, day, len(rows)))
        for r in rows:
            p = r["p"]
            print("    %s %s %s %s  %+.1f pips  %+.2f  stop a %s pips, alvo a %s pips"
                  % (p.ticket, r["side"], p.volume, p.symbol, r["pips"], p.profit + p.swap,
                     "%.1f" % r["to_sl"] if r["to_sl"] is not None else "SEM STOP",
                     "%.1f" % r["to_tp"] if r["to_tp"] is not None else "-"))
            for key, dist, word in (("sl", r["to_sl"], "STOP"), ("tp", r["to_tp"], "ALVO")):
                if dist is not None and dist <= a.near_pips and (p.ticket, key) not in warned:
                    warned.add((p.ticket, key))
                    print("    >>> ALERTA: %s a %.1f pips do %s" % (p.symbol, dist, word))
            if not p.sl and (p.ticket, "nostop") not in warned:
                warned.add((p.ticket, "nostop"))
                print("    >>> ALERTA: posicao %s sem stop" % p.ticket)
        if day <= -cfg["max_daily_loss"] and "daily" not in warned:
            warned.add("daily")
            print("    >>> ALERTA: perda do dia %.2f atingiu o limite %.2f. Sem novas entradas hoje."
                  % (day, cfg["max_daily_loss"]))
        sys.stdout.flush()
        if time.time() + a.every > end:
            return 0
        time.sleep(a.every)


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="MetaTrader 5: analise, tickets, ordens e monitoramento.")
    ap.add_argument("--config", default="trader/mt5_config.json")
    ap.add_argument("--orders", default="trader/mt5/orders.json")
    ap.add_argument("--log", default="trader/mt5/log.json")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status")
    p = sub.add_parser("symbols")
    p.add_argument("--symbols", nargs="*")
    p = sub.add_parser("analyze")
    p.add_argument("symbol")
    p.add_argument("--tf", default="M15", choices=TIMEFRAMES, help="timeframe de entrada (ATR, RSI, ADX)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("scan")
    p.add_argument("--symbols", nargs="*")
    p.add_argument("--tf", default="M15", choices=TIMEFRAMES)
    p = sub.add_parser("plan")
    p.add_argument("symbol")
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--sl", type=float)
    p.add_argument("--sl-pips", type=float)
    p.add_argument("--sl-atr", type=float)
    p.add_argument("--tp", type=float)
    p.add_argument("--rr", type=float)
    p.add_argument("--risk", type=float, help="risco em dinheiro (default: risk_per_trade do config)")
    p.add_argument("--lots", type=float, help="forca o lote (os limites ainda valem)")
    p.add_argument("--tf", default="M15", choices=TIMEFRAMES)
    p.add_argument("--why")
    sub.add_parser("tickets")
    for name in ("approve", "decline"):
        p = sub.add_parser(name)
        p.add_argument("--index", type=int, required=True)
        p.add_argument("--reply", required=(name == "approve"), help="a resposta do usuario, palavra por palavra")
    p = sub.add_parser("send")
    p.add_argument("--index", type=int, required=True)
    sub.add_parser("positions")
    p = sub.add_parser("history")
    p.add_argument("--days", type=int, default=1)
    p = sub.add_parser("close")
    p.add_argument("--ticket", type=int, required=True)
    p.add_argument("--volume", type=float)
    p.add_argument("--reply", required=True)
    p = sub.add_parser("modify")
    p.add_argument("--ticket", type=int, required=True)
    p.add_argument("--sl", type=float)
    p.add_argument("--tp", type=float)
    p.add_argument("--widen", action="store_true", help="permite afastar o stop")
    p.add_argument("--reply", required=True)
    p = sub.add_parser("watch")
    p.add_argument("--every", type=int, default=30)
    p.add_argument("--minutes", type=float, default=60)
    p.add_argument("--near-pips", type=float, default=5)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        cfg = load_config(a.config)
        return globals()["cmd_" + a.cmd](a, cfg)
    except Mt5Error as e:
        print("mt5.py: %s" % e, file=sys.stderr)
        return 1
    finally:
        if _mt5 is not None:
            _mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
