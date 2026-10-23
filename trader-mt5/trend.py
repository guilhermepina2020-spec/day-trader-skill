#!/usr/bin/env python3
"""Trend following on daily (or H4) bars: does it hold up, costs and swaps included?

Three classic families, few parameters each:

  donchian  enter on a close beyond the N-bar high/low, exit on a close beyond
            the M-bar low/high (the Turtle rule)
  ema       long while EMA fast > EMA slow, short while below; exit on the
            opposite cross
  tsmom     time-series momentum: long if the close is above the close L bars
            ago, short if below; checked every 5 bars

Every trade also has a protective stop k x ATR(20) from the entry, which defines
1R. Entries and exits fill at the next bar's open (a gap through the stop fills
at the open). Spread is paid on entry and exit, and the broker's swap is charged
for every night held (triple on Wednesdays), at today's swap rates.

    python trend.py --symbols EURUSD GBPUSD AUDUSD --tf D1
    python trend.py ... --grid                       # every parameter set, in-sample vs out-of-sample
    python trend.py ... --family donchian --n 55 --m 20 --k 3 --trades

Results are in R (multiples of the initial risk), so they do not depend on
position size. --risk-pct turns them into an equity curve.
"""

import argparse
import datetime
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from opening import load, load_point, ts, ema_series, atr_series, BtError  # noqa: E402

# Hantec swap points per lot per night (long, short), read from the terminal on 2026-10-23
SWAPS = {"AUDCAD": (2.79, -8.734), "AUDCHF": (3.893, -9.053), "AUDJPY": (6.36, -14.6664),
         "AUDNZD": (1.768, -11.316), "AUDUSD": (-1.84, -3.68), "CADCHF": (2.097, -5.841),
         "CADJPY": (0.976, -7.68), "CHFJPY": (-8.4964, 0.8176), "EURAUD": (-14.0292, 4.5496),
         "EURCAD": (-3.113, -4.455), "EURCHF": (3.094, -9.086), "EURGBP": (-7.029, 1.0296),
         "EURJPY": (3.5464, -11.3832), "EURNZD": (-7.425, -4.686), "EURUSD": (-8.154, 1.9272),
         "GBPAUD": (-7.205, -3.252), "GBPCAD": (4.383, -13.475), "GBPCHF": (6.66, -17.952),
         "GBPJPY": (9.0904, -26.868), "GBPNZD": (2.3562, -15.0183), "GBPUSD": (-2.75, -1.089),
         "NZDCAD": (-1.287, -2.871), "NZDCHF": (0.75636, -5.3361), "NZDJPY": (1.08, -8.184),
         "NZDUSD": (-4.224, 0.584), "USDCAD": (2.168, -11.004), "USDCHF": (4.8, -13.44),
         "USDJPY": (6.4, -19.248)}
FX28 = sorted(SWAPS)


def signals(b, fam, p):
    """Desired position after each bar's close: +1, -1 or 0 (None = keep)."""
    c, h, l = b["c"], b["h"], b["l"]
    n = len(c)
    want = [None] * n
    if fam == "donchian":
        N, M = p["n"], p["m"]
        for i in range(max(N, M), n):
            hi_n, lo_n = max(h[i - N:i]), min(l[i - N:i])
            hi_m, lo_m = max(h[i - M:i]), min(l[i - M:i])
            if c[i] > hi_n:
                want[i] = 1
            elif c[i] < lo_n:
                want[i] = -1
            else:
                want[i] = ("exit_long" if c[i] < lo_m else None, "exit_short" if c[i] > hi_m else None)
    elif fam == "ema":
        f, s = ema_series(c, p["fast"]), ema_series(c, p["slow"])
        for i in range(n):
            if f[i] is not None and s[i] is not None:
                want[i] = 1 if f[i] > s[i] else -1
    elif fam == "tsmom":
        L = p["lookback"]
        for i in range(L, n):
            if i % 5 == 0:
                want[i] = 1 if c[i] > c[i - L] else -1
    elif fam == "rsi":
        r = rsi_series(c, p["period"])
        lo = p["level"]
        for i in range(n):
            if r[i] is None:
                continue
            if r[i] < lo:
                want[i] = 1
            elif r[i] > 100 - lo:
                want[i] = -1
            else:
                want[i] = ("exit_long" if r[i] > 50 else None, "exit_short" if r[i] < 50 else None)
    elif fam == "bollinger":
        N, z = p["period"], p["z"]
        for i in range(N - 1, n):
            w = c[i - N + 1:i + 1]
            mid, sd = statistics.mean(w), statistics.pstdev(w)
            if c[i] < mid - z * sd:
                want[i] = 1
            elif c[i] > mid + z * sd:
                want[i] = -1
            else:
                want[i] = ("exit_long" if c[i] >= mid else None, "exit_short" if c[i] <= mid else None)
    return want


def rsi_series(c, n):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    ag = sum(max(c[i] - c[i - 1], 0) for i in range(1, n + 1)) / n
    al = sum(max(c[i - 1] - c[i], 0) for i in range(1, n + 1)) / n
    for i in range(n, len(c)):
        if i > n:
            d = c[i] - c[i - 1]
            ag = (ag * (n - 1) + max(d, 0)) / n
            al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def backtest(b, symbol, point, fam, p, k):
    """List of trades: dict(entry_date, exit_date, dir, R, nights, why)."""
    want = signals(b, fam, p)
    atr = atr_series(b["h"], b["l"], b["c"], 20)
    sw_long, sw_short = SWAPS.get(symbol, (0.0, 0.0))
    spreads = sorted(s for s in b["sp"] if s > 0)
    spread = (spreads[len(spreads) // 2] if spreads else 10) * point * 1.5
    trades, pos = [], None
    for i in range(len(b["c"]) - 1):
        o_next = b["o"][i + 1]
        if pos:
            d = pos["dir"]
            # protective stop during bar i (after entry bar)
            if i > pos["i"]:
                if (d == 1 and b["l"][i] <= pos["stop"]) or (d == -1 and b["h"][i] >= pos["stop"]):
                    fill = pos["stop"]
                    if (d == 1 and b["o"][i] < pos["stop"]) or (d == -1 and b["o"][i] > pos["stop"]):
                        fill = b["o"][i]
                    trades.append(close(pos, fill, b, i, spread, point, sw_long, sw_short, "stop"))
                    pos = None
        w = want[i]
        exit_now = False
        if pos:
            d = pos["dir"]
            if isinstance(w, tuple):
                exit_now = (d == 1 and w[0]) or (d == -1 and w[1])
            elif w is not None and w != d:
                exit_now = True
            if exit_now:
                trades.append(close(pos, o_next, b, i + 1, spread, point, sw_long, sw_short, "sinal"))
                pos = None
        if pos is None and w in (1, -1) and atr[i]:
            if fam == "donchian" and trades and trades[-1]["exit_i"] == i + 1 and trades[-1]["dir"] == w:
                continue
            d = w
            entry = o_next + (spread / 2) * d
            stop = entry - d * k * atr[i]
            pos = {"dir": d, "entry": entry, "stop": stop, "risk": k * atr[i], "i": i + 1,
                   "date": ts(b["t"][i + 1]).date()}
    return trades


def close(pos, px, b, i, spread, point, sw_long, sw_short, why):
    d = pos["dir"]
    exit_px = px - (spread / 2) * d
    nights = 0
    for j in range(pos["i"], i):
        nights += 3 if ts(b["t"][j]).weekday() == 2 else 1
    swap = (sw_long if d == 1 else sw_short) * point * nights
    r = ((exit_px - pos["entry"]) * d + swap) / pos["risk"]
    return {"entry_date": pos["date"], "exit_date": ts(b["t"][i]).date(), "exit_i": i, "dir": d,
            "R": r, "nights": nights, "why": why}


def summary(trades, years):
    if not trades:
        return {"n": 0}
    rs = [t["R"] for t in sorted(trades, key=lambda t: t["exit_date"])]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": len(rs), "win": 100.0 * len(wins) / len(rs), "avg_win": statistics.mean(wins) if wins else 0,
            "avg_loss": statistics.mean(losses) if losses else 0, "exp": statistics.mean(rs), "total": sum(rs),
            "per_year": sum(rs) / years if years else 0, "dd": dd}


def fmt(s):
    if not s["n"]:
        return "   0 trades"
    return ("%4d trades  acerto %4.1f%%  ganho medio %+.2fR  perda media %+.2fR  exp %+.3fR  total %+6.1fR  "
            "%+5.1fR/ano  DD %4.1fR" % (s["n"], s["win"], s["avg_win"], s["avg_loss"], s["exp"], s["total"],
                                       s["per_year"], s["dd"]))


def equity(trades, risk_pct):
    """Compounded equity at risk_pct per trade, trades taken in exit order: (final multiple, max DD %)."""
    eq, peak, dd = 1.0, 1.0, 0.0
    for t in sorted(trades, key=lambda t: t["exit_date"]):
        eq *= 1 + risk_pct / 100.0 * t["R"]
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak)
    return eq, dd * 100


def run_all(data, symbols, tf, fam, p, k):
    out = []
    for s in symbols:
        b = load(data, s, tf)
        for t in backtest(b, s, load_point(data, s), fam, p, k):
            t["symbol"] = s
            out.append(t)
    return out


def label(fam, p, k):
    return "%-8s %-22s stop %.0fxATR" % (fam, " ".join("%s=%s" % kv for kv in sorted(p.items())), k)


GRID = {"donchian": [{"n": n, "m": m} for n in (20, 40, 55, 100) for m in (10, 20) if m < n],
        "ema": [{"fast": f, "slow": s} for f, s in ((10, 50), (20, 50), (20, 100), (50, 200))],
        "tsmom": [{"lookback": L} for L in (20, 60, 120, 250)],
        "rsi": [{"period": n, "level": lv} for n in (2, 3, 5) for lv in (10, 20, 30)],
        "bollinger": [{"period": n, "z": z} for n in (10, 20, 40) for z in (1.5, 2.0, 2.5)]}
TREND = ("donchian", "ema", "tsmom")
REVERSION = ("rsi", "bollinger")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Seguir tendencia em D1/H4, com custos e swaps.")
    ap.add_argument("--symbols", nargs="+", default=["EURUSD", "GBPUSD", "AUDUSD"])
    ap.add_argument("--data", default="trader/data")
    ap.add_argument("--tf", default="D1", choices=["D1", "H4"])
    ap.add_argument("--split", default="2023-12-31")
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--families", nargs="+", default=list(GRID), choices=list(GRID), help="familias no --grid")
    ap.add_argument("--period", type=int, default=2)
    ap.add_argument("--level", type=float, default=10)
    ap.add_argument("--z", type=float, default=2.0)
    ap.add_argument("--family", default="donchian", choices=list(GRID))
    ap.add_argument("--n", type=int, default=55)
    ap.add_argument("--m", type=int, default=20)
    ap.add_argument("--fast", type=int, default=20)
    ap.add_argument("--slow", type=int, default=100)
    ap.add_argument("--lookback", type=int, default=120)
    ap.add_argument("--k", type=float, default=3.0)
    ap.add_argument("--risk-pct", type=float, default=0.5)
    ap.add_argument("--trades", action="store_true")
    a = ap.parse_args(argv)
    try:
        split = datetime.date.fromisoformat(a.split)
        b0 = load(a.data, a.symbols[0], a.tf)
        start, end = ts(b0["t"][0]).date(), ts(b0["t"][-1]).date()
        y_in = (split - start).days / 365.25
        y_out = (end - split).days / 365.25
        print("%s %s de %s a %s. Ajuste ate %s (%.1f anos), validacao %.1f anos. Custos: spread e swap." %
              (" ".join(a.symbols), a.tf, start, end, split, y_in, y_out))
        if a.grid:
            rows = []
            for fam, plist in GRID.items():
                if fam not in a.families:
                    continue
                for p in plist:
                    for k in (2.0, 3.0, 5.0):
                        tr = run_all(a.data, a.symbols, a.tf, fam, p, k)
                        ins = summary([t for t in tr if t["exit_date"] <= split], y_in)
                        oos = summary([t for t in tr if t["exit_date"] > split], y_out)
                        rows.append((fam, p, k, ins, oos))
            pos_in = sum(1 for r in rows if r[3]["n"] and r[3]["exp"] > 0)
            pos_out = sum(1 for r in rows if r[4]["n"] and r[4]["exp"] > 0)
            both = sum(1 for r in rows if r[3]["n"] and r[4]["n"] and r[3]["exp"] > 0 and r[4]["exp"] > 0)
            print("%d variantes. Positivas no ajuste: %d. Na validacao: %d. Nos dois: %d.\n" %
                  (len(rows), pos_in, pos_out, both))
            for fam, p, k, ins, oos in rows:
                print(label(fam, p, k))
                print("    ajuste    " + fmt(ins))
                print("    validacao " + fmt(oos))
            return 0
        p = {"donchian": {"n": a.n, "m": a.m}, "ema": {"fast": a.fast, "slow": a.slow},
             "tsmom": {"lookback": a.lookback}, "rsi": {"period": a.period, "level": a.level},
             "bollinger": {"period": a.period, "z": a.z}}[a.family]
        tr = run_all(a.data, a.symbols, a.tf, a.family, p, a.k)
        print(label(a.family, p, a.k))
        for s in a.symbols:
            print("  %-7s %s" % (s, fmt(summary([t for t in tr if t["symbol"] == s], y_in + y_out))))
        print("  ajuste  " + fmt(summary([t for t in tr if t["exit_date"] <= split], y_in)))
        print("  valid.  " + fmt(summary([t for t in tr if t["exit_date"] > split], y_out)))
        mult, dd = equity(tr, a.risk_pct)
        yrs = y_in + y_out
        print("  com %.2f%% de risco por trade: capital x%.2f em %.1f anos (%+.1f%%/ano), queda maxima %.1f%%" %
              (a.risk_pct, mult, yrs, (mult ** (1 / yrs) - 1) * 100, dd))
        if a.trades:
            for t in sorted(tr, key=lambda t: t["entry_date"]):
                print("  %s %s -> %s %-4s %+6.2fR  %3d noites  %s" % (t["symbol"], t["entry_date"], t["exit_date"],
                                                                     "BUY" if t["dir"] == 1 else "SELL", t["R"],
                                                                     t["nights"], t["why"]))
        return 0
    except BtError as e:
        print("trend.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
