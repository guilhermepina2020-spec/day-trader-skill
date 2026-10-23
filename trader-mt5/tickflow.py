#!/usr/bin/env python3
"""Tick flow around the London open: effort (tick activity, tick delta) vs result (price).

CFD ticks carry no traded volume and no aggressor side, so the flow is read
from the quotes themselves:

  activity   ticks in a window vs the same window over the previous 20 sessions
  delta      (upticks - downticks) / (upticks + downticks) of the mid price:
             the pressure, the closest a CFD feed gets to "buy vs sell volume"
  result     how far the mid price actually moved, in units of its usual move
  absorption high activity, delta one way, price the other way

  export   downloads the ticks of every session from the open window and stores
           them as 1-second buckets (trader/data/<SYMBOL>_flow.csv)
  study    for each minute after the open, the flow of the last N minutes and
           what price did over the next 15/30/60 minutes, grouped by pattern,
           in-sample vs out-of-sample

    python tickflow.py export --symbol UK100
    python tickflow.py study --symbol UK100 --window 3
"""

import argparse
import csv
import datetime
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from opening import london_open_server, BtError  # noqa: E402

EPOCH = datetime.datetime(1970, 1, 1)


def ts(sec):
    return EPOCH + datetime.timedelta(seconds=int(sec))


# ---------------------------------------------------------------- export

def export(symbol, out, terminal_path, since, before_min=30, after_min=150):
    import MetaTrader5 as mt5
    ok = mt5.initialize(path=terminal_path) if terminal_path else mt5.initialize()
    if not ok:
        raise BtError("nao conectou ao MT5: %s" % (mt5.last_error(),))
    path = os.path.join(out, "%s_flow.csv" % symbol)
    os.makedirs(out, exist_ok=True)
    days = 0
    try:
        mt5.symbol_select(symbol, True)
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["sec", "ticks", "up", "down", "open", "high", "low", "close", "spread"])
            d = since
            while d <= datetime.date.today():
                if d.weekday() < 5:
                    om = london_open_server(d)
                    frm = datetime.datetime(d.year, d.month, d.day) - datetime.timedelta(hours=6)
                    t = mt5.copy_ticks_range(symbol, frm, frm + datetime.timedelta(hours=36), mt5.COPY_TICKS_ALL)
                    lo = int((datetime.datetime(d.year, d.month, d.day) - EPOCH).total_seconds()) + (om - before_min) * 60
                    hi = lo + (before_min + after_min) * 60
                    if t is not None and len(t):
                        n = write_buckets(w, t, lo, hi)
                        days += n > 0
                d += datetime.timedelta(days=1)
    finally:
        mt5.shutdown()
    print("%s: %d sessoes em %s" % (symbol, days, path))


def write_buckets(w, ticks, lo, hi):
    """1-second buckets of the mid price: tick count, upticks, downticks, OHLC, mean spread."""
    cur, rows, last_mid = None, 0, None
    for x in ticks:
        s = int(x["time"])
        if s < lo or s >= hi:
            continue
        bid, ask = float(x["bid"]), float(x["ask"])
        if bid <= 0 or ask <= 0:
            continue
        mid = (bid + ask) / 2
        if cur is None or cur[0] != s:
            if cur:
                w.writerow(cur[:8] + [round(cur[8] / cur[1], 3)])
                rows += 1
            cur = [s, 0, 0, 0, mid, mid, mid, mid, 0.0]
        cur[1] += 1
        if last_mid is not None:
            if mid > last_mid:
                cur[2] += 1
            elif mid < last_mid:
                cur[3] += 1
        cur[5], cur[6], cur[7] = max(cur[5], mid), min(cur[6], mid), mid
        cur[8] += ask - bid
        last_mid = mid
    if cur:
        w.writerow(cur[:8] + [round(cur[8] / cur[1], 3)])
        rows += 1
    return rows


# ---------------------------------------------------------------- minutes

def load_minutes(data, symbol):
    """{date: {server_minute: [ticks, up, down, open, high, low, close, spread]}}"""
    path = os.path.join(data, "%s_flow.csv" % symbol)
    if not os.path.exists(path):
        raise BtError("sem %s; rode export primeiro" % path)
    days = {}
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            t = ts(r["sec"])
            m = t.hour * 60 + t.minute
            day = days.setdefault(t.date(), {})
            c = float(r["close"])
            row = day.get(m)
            if row is None:
                day[m] = [int(r["ticks"]), int(r["up"]), int(r["down"]), float(r["open"]), float(r["high"]),
                          float(r["low"]), c, float(r["spread"])]
            else:
                row[0] += int(r["ticks"])
                row[1] += int(r["up"])
                row[2] += int(r["down"])
                row[4] = max(row[4], float(r["high"]))
                row[5] = min(row[5], float(r["low"]))
                row[6] = c
                row[7] = max(row[7], float(r["spread"]))
    return days


def features(days, window, horizons=(15, 30, 60), start=1, stop=90, flat=120):
    """One row per (session, minute after the open): the flow of the last `window` minutes and the
    forward move. Activity and moves are scaled by the same minute's values over the previous 20 sessions."""
    rows = []
    hist_ticks, hist_move = {}, {}
    for date in sorted(days):
        day = days[date]
        om = london_open_server(date)
        if om not in day:
            continue
        for k in range(start, stop + 1):
            m = om + k
            span = [day.get(x) for x in range(m - window, m)]
            if any(s is None for s in span):
                continue
            ticks = sum(s[0] for s in span)
            up, down = sum(s[1] for s in span), sum(s[2] for s in span)
            first, last = span[0][3], span[-1][6]
            move = last - first
            ht = hist_ticks.setdefault(k, [])
            hm = hist_move.setdefault(k, [])
            prev_t, prev_m = ht[-20:], hm[-20:]
            ht.append(ticks)
            hm.append(abs(move))
            if len(prev_t) < 20 or up + down == 0:
                continue
            base_t = statistics.median(prev_t) or 1
            base_m = statistics.median(prev_m) or 1e-9
            fwd = {}
            for h in horizons:
                e = day.get(min(m + h, om + flat) - 1)
                if e is not None:
                    fwd[h] = (e[6] - last) / base_m
            rows.append({"date": date, "k": k, "act": ticks / float(base_t),
                         "delta": (up - down) / float(up + down), "ret": move / base_m,
                         "spread": span[-1][7] / base_m, "fwd": fwd})
    return rows


def summarize(rows, name, horizons=(15, 30, 60)):
    """Average forward move in the direction of PRICE (+ = price kept going), and the hit rate."""
    if not rows:
        print("  %-48s    0" % name)
        return
    parts = ["  %-48s %4d" % (name, len(rows))]
    for h in horizons:
        xs = [r["fwd"][h] * (1 if r["ret"] > 0 else -1) for r in rows if h in r["fwd"]]
        if xs:
            parts.append("+%2dmin %+.2f (%2.0f%%)" % (h, statistics.mean(xs), 100.0 * sum(x > 0 for x in xs) / len(xs)))
    parts.append("custo %.2f" % statistics.mean(r["spread"] for r in rows))
    print("  ".join(parts))


def study(a):
    days = load_minutes(a.data, a.symbol)
    rows = features(days, a.window)
    split = datetime.date.fromisoformat(a.split)
    print("%s: %d sessoes, janela de fluxo %d min, decisoes de +1 a +90 min da abertura" %
          (a.symbol, len(days), a.window))
    print("Numeros: movimento futuro medio NA DIRECAO DO PRECO da janela (+ continua, - reverte),")
    print("em unidades do movimento tipico de %d min naquele horario; (%%) = vezes que continuou." % a.window)
    for per_name, sel in (("AJUSTE ate %s" % split, lambda r: r["date"] <= split),
                          ("VALIDACAO depois", lambda r: r["date"] > split)):
        rs = [r for r in rows if sel(r) and abs(r["ret"]) >= a.min_move]
        print("\n" + per_name)
        summarize(rs, "todas (preco andou >= %.1f)" % a.min_move)
        hi = [r for r in rs if r["act"] >= a.act]
        lo = [r for r in rs if r["act"] < a.act]
        agree = lambda r: r["delta"] * r["ret"] > 0 and abs(r["delta"]) >= a.delta
        diverge = lambda r: r["delta"] * r["ret"] < 0 and abs(r["delta"]) >= a.delta
        summarize([r for r in hi if agree(r)], "atividade alta, pressao A FAVOR do preco")
        summarize([r for r in hi if diverge(r)], "atividade alta, pressao CONTRA o preco (absorcao)")
        summarize([r for r in lo if agree(r)], "atividade baixa, pressao a favor")
        summarize([r for r in lo if diverge(r)], "atividade baixa, pressao contra")
        for k0, k1 in ((1, 15), (16, 45), (46, 90)):
            summarize([r for r in hi if diverge(r) and k0 <= r["k"] <= k1],
                      "  absorcao, +%d a +%d min da abertura" % (k0, k1))


def trade_day(day, om, p, hist):
    """First minute after the open where activity is high and pressure agrees with a real price move:
    enter in the price's direction at that minute's close, stop beyond the window's extreme (or a
    multiple of the typical move), target in R, flat at open + 120. Mid prices, half the spread paid
    on each side. Returns a trade dict or None."""
    w = p["window"]
    for k in range(max(w, 1), p["last_entry"] + 1):
        m = om + k
        span = [day.get(x) for x in range(m - w, m)]
        if any(s is None for s in span):
            continue
        ticks = sum(s[0] for s in span)
        up, down = sum(s[1] for s in span), sum(s[2] for s in span)
        move = span[-1][6] - span[0][3]
        ht, hm = hist.setdefault(k, ([], []))
        base_t = statistics.median(ht[-20:]) if len(ht) >= 20 else None
        base_m = statistics.median(hm[-20:]) if len(hm) >= 20 else None
        if base_t is None or not base_m or up + down == 0:
            continue
        act, delta, ret = ticks / float(base_t), (up - down) / float(up + down), move / base_m
        if act < p["act"] or abs(ret) < p["min_move"] or delta * ret <= 0 or abs(delta) < p["delta"]:
            continue
        d = 1 if ret > 0 else -1
        half = span[-1][7] / 2
        entry = span[-1][6] + d * half
        if p["stop"] == "extreme":
            stop = (min(s[5] for s in span) if d == 1 else max(s[4] for s in span)) - d * half
        else:
            stop = entry - d * p["stop_mult"] * base_m
        risk = (entry - stop) * d
        if risk <= 0:
            continue
        target = entry + d * risk * p["target"] if p["target"] else None
        for x in range(m, om + p["flat"]):
            b = day.get(x)
            if b is None:
                continue
            sp = b[7] / 2
            lo, hi = b[5] - d * sp, b[4] - d * sp
            if (d == 1 and lo <= stop) or (d == -1 and hi >= stop):
                return {"k": k, "dir": d, "R": -1.0, "why": "stop", "act": act, "delta": delta}
            if target is not None and ((d == 1 and hi >= target) or (d == -1 and lo <= target)):
                return {"k": k, "dir": d, "R": p["target"], "why": "alvo", "act": act, "delta": delta}
        last = day.get(max(x for x in day if x < om + p["flat"]))
        exit_px = last[6] - d * last[7] / 2
        return {"k": k, "dir": d, "R": (exit_px - entry) * d / risk, "why": "tempo", "act": act, "delta": delta}
    return None


def update_hist(day, om, hist, window, upto):
    for k in range(max(window, 1), upto + 1):
        span = [day.get(x) for x in range(om + k - window, om + k)]
        if any(s is None for s in span):
            continue
        ht, hm = hist.setdefault(k, ([], []))
        ht.append(sum(s[0] for s in span))
        hm.append(abs(span[-1][6] - span[0][3]))


def backtest(days, p, split):
    hist, ins, oos = {}, [], []
    for date in sorted(days):
        day = days[date]
        om = london_open_server(date)
        if om in day:
            t = trade_day(day, om, p, hist)
            if t:
                t["date"] = date
                (ins if date <= split else oos).append(t)
            update_hist(day, om, hist, p["window"], p["last_entry"])
    return ins, oos


def bt_stats(trades):
    n = len(trades)
    if not n:
        return "   0 trades"
    rs = [t["R"] for t in trades]
    wins = [r for r in rs if r > 0]
    gl = -sum(r for r in rs if r <= 0)
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return "%3d trades  acerto %5.1f%%  exp %+.2fR  total %+6.1fR  PF %5.2f  DD %4.1fR" % (
        n, 100.0 * len(wins) / n, sum(rs) / n, sum(rs), sum(wins) / gl if gl else float("inf"), dd)


def cmd_backtest(a):
    import itertools
    days = load_minutes(a.data, a.symbol)
    split = datetime.date.fromisoformat(a.split)
    base = {"window": a.window, "act": a.act, "delta": a.delta, "min_move": a.min_move, "stop": a.stop,
            "stop_mult": a.stop_mult, "target": a.target, "last_entry": 90, "flat": 120}
    if not a.grid:
        ins, oos = backtest(days, base, split)
        print("ajuste    " + bt_stats(ins))
        print("validacao " + bt_stats(oos))
        if a.trades:
            for t in ins + oos:
                print("  %s %s +%2dmin act %.1f delta %+.2f %-5s %+.2fR" % (
                    t["date"], "BUY " if t["dir"] == 1 else "SELL", t["k"], t["act"], t["delta"], t["why"], t["R"]))
        return
    grid = {"window": [1, 3], "act": [1.5, 2.0, 3.0], "min_move": [1.0, 2.0], "delta": [0.1, 0.3],
            "stop": ["extreme", "mult"], "target": [1.0, 2.0, 0.0]}
    rows = []
    for combo in itertools.product(*grid.values()):
        p = dict(base, **dict(zip(grid, combo)))
        ins, oos = backtest(days, p, split)
        if len(ins) < 20:
            continue
        rows.append((sum(t["R"] for t in ins) / len(ins), p, ins, oos))
    rows.sort(key=lambda x: -x[0])
    print("%d variantes com >= 20 trades no ajuste; todas as variantes, das melhores no ajuste para as piores:" % len(rows))
    pos_oos = 0
    for exp, p, ins, oos in rows:
        oos_exp = sum(t["R"] for t in oos) / len(oos) if oos else 0
        pos_oos += oos_exp > 0
    print("variantes com expectativa positiva na validacao: %d de %d" % (pos_oos, len(rows)))
    for exp, p, ins, oos in rows[:a.top]:
        print("jan%d act%.1f move%.1f delta%.1f stop=%s alvo=%s" % (p["window"], p["act"], p["min_move"], p["delta"],
                                                                   p["stop"], p["target"] or "tempo"))
        print("    ajuste    " + bt_stats(ins))
        print("    validacao " + bt_stats(oos))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fluxo de ticks na abertura de Londres.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("export")
    p.add_argument("--symbol", default="UK100")
    p.add_argument("--out", default="trader/data")
    p.add_argument("--terminal-path")
    p.add_argument("--since", default="2026-01-01")
    p = sub.add_parser("study")
    p.add_argument("--symbol", default="UK100")
    p.add_argument("--data", default="trader/data")
    p.add_argument("--window", type=int, default=3)
    p.add_argument("--act", type=float, default=1.5, help="atividade alta: ticks / mediana de 20 sessoes")
    p.add_argument("--delta", type=float, default=0.1, help="pressao minima |delta|")
    p.add_argument("--min-move", type=float, default=0.5, help="preco andou pelo menos x o movimento tipico")
    p.add_argument("--split", default="2026-06-30")
    p = sub.add_parser("backtest")
    p.add_argument("--symbol", default="UK100")
    p.add_argument("--data", default="trader/data")
    p.add_argument("--window", type=int, default=1)
    p.add_argument("--act", type=float, default=2.0)
    p.add_argument("--delta", type=float, default=0.1)
    p.add_argument("--min-move", type=float, default=1.0)
    p.add_argument("--stop", default="extreme", choices=["extreme", "mult"])
    p.add_argument("--stop-mult", type=float, default=1.5)
    p.add_argument("--target", type=float, default=2.0)
    p.add_argument("--split", default="2026-06-30")
    p.add_argument("--grid", action="store_true")
    p.add_argument("--top", type=int, default=12)
    p.add_argument("--trades", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "export":
            export(a.symbol, a.out, a.terminal_path, datetime.date.fromisoformat(a.since))
        elif a.cmd == "study":
            study(a)
        elif a.cmd == "backtest":
            cmd_backtest(a)
        else:
            ap.print_help()
            return 1
        return 0
    except BtError as e:
        print("tickflow.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
