#!/usr/bin/env python3
"""Day-trade hypotheses on FX, M15, every position flat before the rollover (no swap).

All times are server time (GMT+2, GMT+3 with US daylight time, so the server is
always New York + 7 h). The London open comes from opening.london_open_server.
ADR is the mean (high - low) of the previous 14 server days.

  asia_break   stop orders at the Asian range (00:00 to the London open) for 4 h
               after the open, stop at the other side, target 1R / 2R / flat
  stretch      price runs more than k x ADR away from the day open between the
               London open and 20:00: fade it, stop 0.3 ADR, target the day open
               or half way back
  rsi_m15      RSI(n) on M15 beyond a level between 10:00 and 21:00: fade it,
               exit when RSI crosses 50, stop 0.25 ADR
  fix_fade     London open to the 16:00 London fix moved more than k x ADR: fade
               it after the fix, stop 0.3 ADR
  daily_rsi    daily RSI(5) of the closed days beyond 20/80 (or 10/90): trade the
               reversal inside today only, from 00:00 or from the London open,
               stop 0.5 ADR
  open_momo    direction from 00:00 to the London open, kept until the flat

Every position is closed at the open of the 23:00 bar. Costs: 1.5 x the
pair's median spread for that hour, plus --comm pips per round trip. When a
bar touches the stop and the target, the stop counts. Groups: the 3 pairs the
earlier studies used (ajuste) and the other 25 (validacao), each split in two
periods.

    python intraday.py --data trader/data
    python intraday.py --data trader/data --only stretch --by-symbol
"""

import argparse
import datetime
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from opening import load, load_point, london_open_server, ts, BtError  # noqa: E402
from trend import FX28, rsi_series  # noqa: E402

FIT = ("EURUSD", "GBPUSD", "AUDUSD")
FLAT = 23 * 60
START = datetime.date(2022, 10, 20)
SPLIT = datetime.date(2024, 10, 1)


# ---------------------------------------------------------------- data

def prepare(data, symbol, cost_mult=1.5):
    b = load(data, symbol, "M15")
    point = load_point(data, symbol)
    pip = point * 10
    days = {}
    for i, t in enumerate(b["t"]):
        d = ts(t)
        days.setdefault(d.date(), {})[d.hour * 60 + d.minute] = i
    dates = [d for d in sorted(days) if d.weekday() < 5 and len(days[d]) >= 80]
    hours = {}
    for i, t in enumerate(b["t"]):
        if b["sp"][i] > 0:
            hours.setdefault(ts(t).hour, []).append(b["sp"][i])
    med = {h: sorted(v)[len(v) // 2] * point * cost_mult for h, v in hours.items()}
    worst = max(med.values())
    b["cost"] = [med.get(ts(t).hour, worst) for t in b["t"]]
    ranges, closes = [], []
    for d in dates:
        idx = list(days[d].values())
        ranges.append(max(b["h"][i] for i in idx) - min(b["l"][i] for i in idx))
        closes.append(b["c"][max(idx)])
    rsi5 = rsi_series(closes, 5)
    sorted_days = {d: sorted(days[d].items()) for d in dates}
    return {"b": b, "point": point, "pip": pip, "days": sorted_days, "dates": dates, "ranges": ranges,
            "rsi5": rsi5, "symbol": symbol}


def spread(P, i):
    return P["b"]["cost"][i]


def day_bars(P, date):
    """Sorted (minute, index) of the day."""
    return P["days"][date]


# ---------------------------------------------------------------- execution

def run_trade(P, bars, k, side, entry, stop, target, comm, exit_rsi=None):
    """Walk bars[k:] from an entry already done at `entry` (ask for buys). Returns R or None.

    bars is the day's (minute, index) list; exits at the stop, the target, an RSI
    cross of 50 (exit_rsi: series, checked at bar close, filled next open) or the
    open of the first bar at or after FLAT."""
    b = P["b"]
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    for n in range(k, len(bars)):
        m, i = bars[n]
        sp = spread(P, i)
        if m >= FLAT:
            px = b["o"][i] + (sp if side == -1 else 0)
            return ((px - entry) * side - comm) / risk
        lo, hi = b["l"][i] + (sp if side == -1 else 0), b["h"][i] + (sp if side == -1 else 0)
        o = b["o"][i] + (sp if side == -1 else 0)
        if (side == 1 and lo <= stop) or (side == -1 and hi >= stop):
            fill = o if (side == 1 and o < stop) or (side == -1 and o > stop) else stop
            return ((fill - entry) * side - comm) / risk
        if target is not None and ((side == 1 and hi >= target) or (side == -1 and lo <= target)):
            fill = o if (side == 1 and o > target) or (side == -1 and o < target) else target
            return ((fill - entry) * side - comm) / risk
        if exit_rsi is not None and n + 1 < len(bars):
            r = exit_rsi[i]
            if r is not None and ((side == 1 and r >= 50) or (side == -1 and r <= 50)):
                j = bars[n + 1][1]
                px = b["o"][j] + (spread(P, j) if side == -1 else 0)
                return ((px - entry) * side - comm) / risk, n + 1
    m, i = bars[-1]
    px = b["c"][i] + (spread(P, i) if side == -1 else 0)
    return ((px - entry) * side - comm) / risk


def market(P, bars, k, side, stop_dist, target_px, comm, exit_rsi=None):
    """Market entry at the open of bars[k]."""
    b = P["b"]
    i = bars[k][1]
    entry = b["o"][i] + (spread(P, i) if side == 1 else 0)
    stop = entry - side * stop_dist
    return run_trade(P, bars, k, side, entry, stop, target_px, comm, exit_rsi)


def first_at(bars, minute):
    for n, (m, _) in enumerate(bars):
        if m >= minute:
            return n
    return None


# ---------------------------------------------------------------- hypotheses

def h_asia_break(P, di, bars, adr, comm, p):
    b = P["b"]
    lo_m = london_open_server(P["dates"][di])
    asia = [i for m, i in bars if m < lo_m]
    k0 = first_at(bars, lo_m)
    if len(asia) < 0.7 * lo_m / 15 or k0 is None:
        return []
    ah, al = max(b["h"][i] for i in asia), min(b["l"][i] for i in asia)
    for n in range(k0, len(bars)):
        m, i = bars[n]
        if m >= lo_m + 240:
            return []
        sp = spread(P, i)
        up, dn = b["h"][i] + sp >= ah + sp, b["l"][i] <= al
        if up and dn:
            return []
        if up or dn:
            side = 1 if up else -1
            if side == 1:
                entry = max(b["o"][i], ah) + sp
                stop = al
            else:
                entry = min(b["o"][i], al)
                stop = ah + sp
            risk = abs(entry - stop)
            target = entry + side * p["rr"] * risk if p["rr"] else None
            r = run_trade(P, bars, n, side, entry, stop, target, comm)
            return [r]
    return []


def h_stretch(P, di, bars, adr, comm, p):
    b = P["b"]
    lo_m = london_open_server(P["dates"][di])
    ref = b["o"][bars[0][1]]
    for n, (m, i) in enumerate(bars):
        if m < lo_m or n + 1 >= len(bars):
            continue
        if m >= 20 * 60:
            return []
        dev = b["c"][i] - ref
        if abs(dev) > p["k"] * adr:
            side = -1 if dev > 0 else 1
            target = ref if p["target"] == "open" else ref + dev / 2
            return [market(P, bars, n + 1, side, 0.3 * adr, target, comm)]
    return []


def h_rsi_m15(P, di, bars, adr, comm, p):
    key = "rsi_m15_%d" % p["n"]
    if key not in P:
        P[key] = rsi_series(P["b"]["c"], p["n"])
    rsi = P[key]
    out, n = [], 0
    while n < len(bars) - 1:
        m, i = bars[n]
        if 10 * 60 <= m < 21 * 60 and rsi[i] is not None and (rsi[i] < p["lvl"] or rsi[i] > 100 - p["lvl"]):
            side = 1 if rsi[i] < p["lvl"] else -1
            res = market(P, bars, n + 1, side, 0.25 * adr, None, comm, exit_rsi=rsi)
            if isinstance(res, tuple):
                out.append(res[0])
                n = res[1]
                continue
            if res is not None:
                out.append(res)
            return out
        n += 1
    return out


def h_fix_fade(P, di, bars, adr, comm, p):
    b = P["b"]
    lo_m = london_open_server(P["dates"][di])
    k0, kf = first_at(bars, lo_m), first_at(bars, lo_m + 8 * 60 + 15)
    if k0 is None or kf is None or kf >= len(bars) - 1:
        return []
    move = b["c"][bars[kf][1]] - b["o"][bars[k0][1]]
    if abs(move) <= p["k"] * adr:
        return []
    return [market(P, bars, kf + 1, -1 if move > 0 else 1, 0.3 * adr, None, comm)]


def h_daily_rsi(P, di, bars, adr, comm, p):
    r = P["rsi5"][di - 1]
    if r is None or p["lvl"] <= r <= 100 - p["lvl"]:
        return []
    side = 1 if r < p["lvl"] else -1
    k = 0 if p["entry"] == "00:00" else first_at(bars, london_open_server(P["dates"][di]))
    if k is None:
        return []
    return [market(P, bars, k, side, 0.5 * adr, None, comm)]


def h_open_momo(P, di, bars, adr, comm, p):
    b = P["b"]
    k = first_at(bars, london_open_server(P["dates"][di]))
    if k is None:
        return []
    move = b["o"][bars[k][1]] - b["o"][bars[0][1]]
    if move == 0:
        return []
    return [market(P, bars, k, 1 if move > 0 else -1, 0.5 * adr, None, comm)]


VARIANTS = [("asia_break", h_asia_break, {"rr": rr}) for rr in (1.0, 2.0, None)]
VARIANTS += [("stretch", h_stretch, {"k": k, "target": t}) for k in (0.5, 0.75) for t in ("open", "half")]
VARIANTS += [("rsi_m15", h_rsi_m15, {"n": n, "lvl": lvl}) for n, lvl in ((5, 15), (2, 5))]
VARIANTS += [("fix_fade", h_fix_fade, {"k": k}) for k in (0.3, 0.5)]
VARIANTS += [("daily_rsi", h_daily_rsi, {"lvl": lvl, "entry": e}) for lvl in (20, 10) for e in ("00:00", "london")]
VARIANTS += [("open_momo", h_open_momo, {})]


# ---------------------------------------------------------------- report

def run_variant(P, fn, p, comm_pips):
    out = []
    comm = comm_pips * P["pip"]
    for di in range(15, len(P["dates"])):
        date = P["dates"][di]
        if date < START:
            continue
        bars = day_bars(P, date)
        adr = statistics.mean(P["ranges"][di - 14:di])
        for r in fn(P, di, bars, adr, comm, p):
            if r is not None:
                out.append((date, P["symbol"], r))
    return out


def cell(rs):
    if len(rs) < 2:
        return "%5d" % len(rs) + " " * 31
    m, sd = statistics.mean(rs), statistics.stdev(rs)
    t = m / (sd / math.sqrt(len(rs))) if sd else 0.0
    return "%5d %5.1f%% %+.3fR t=%+5.1f" % (len(rs), 100.0 * sum(r > 0 for r in rs) / len(rs), m, t)


def drawdown(trades):
    eq = peak = dd = 0.0
    for _, _, r in sorted(trades):
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


def report(name, p, trades, symbols):
    parts = []
    for grp, sel in (("ajuste", lambda s: s in FIT), ("valid", lambda s: s not in FIT)):
        for per, ps in (("A", lambda d: d < SPLIT), ("B", lambda d: d >= SPLIT)):
            parts.append(cell([r for d, s, r in trades if sel(s) and ps(d)]))
    val = [t for t in trades if t[1] not in FIT]
    pos = 0
    vs = [s for s in symbols if s not in FIT]
    for s in vs:
        rs = [r for _, x, r in val if x == s]
        pos += bool(rs) and statistics.mean(rs) > 0
    lab = "%s %s" % (name, " ".join("%s=%s" % kv for kv in sorted(p.items())))
    print("%-34s %s | %s || %s | %s  pares+ %2d/%d  DD %.0fR"
          % (lab, parts[0], parts[1], parts[2], parts[3], pos, len(vs), drawdown(val)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Hipoteses de day trade em FX (M15, zerado antes do rollover).")
    ap.add_argument("--data", default="trader/data")
    ap.add_argument("--symbols", nargs="+", default=list(FX28))
    ap.add_argument("--only", nargs="+", help="so estas hipoteses")
    ap.add_argument("--comm", type=float, default=0.0, help="comissao em pips por ida e volta")
    ap.add_argument("--cost", type=float, default=1.5, help="multiplo do spread mediano (0 = sem custo)")
    ap.add_argument("--by-symbol", action="store_true")
    a = ap.parse_args(argv)
    try:
        Ps = [prepare(a.data, s, a.cost) for s in a.symbols if os.path.exists(os.path.join(a.data, s + "_M15.csv"))]
        syms = [P["symbol"] for P in Ps]
        print("%d pares, %s a hoje, divisao %s, comissao %.1f pip" % (len(Ps), START, SPLIT, a.comm))
        hdr = "n  acerto   exp     t"
        print("%-34s %-36s | %-36s || %-36s | %-36s" % ("", "AJUSTE (3 pares) ate " + str(SPLIT), "AJUSTE depois",
                                                       "VALIDACAO (25) ate " + str(SPLIT), "VALIDACAO depois"))
        print("%-34s %-36s | %-36s || %-36s | %-36s" % ("hipotese", hdr, hdr, hdr, hdr))
        for name, fn, p in VARIANTS:
            if a.only and name not in a.only:
                continue
            trades = []
            for P in Ps:
                trades += run_variant(P, fn, p, a.comm)
            report(name, p, trades, syms)
            if a.by_symbol:
                for s in syms:
                    print("    %-8s %s" % (s, cell([r for _, x, r in trades if x == s])))
        return 0
    except BtError as e:
        print("intraday.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
