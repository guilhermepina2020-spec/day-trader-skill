#!/usr/bin/env python3
"""Which price regions does the market respect, and does volume at the touch tell you more?

For each server day, levels known BEFORE the London open:

  PDH/PDL/PDC   previous day high, low, close
  PWH/PWL       previous week high, low
  ASIA_H/L      today's range from 00:00 server to the London open
  POC/VAH/VAL   previous day's tick-volume profile: point of control and the 70 % value area
  R00/R50       round numbers (x.xx00 and x.xx50) within half a daily range of the open
  CONTROL       every real level shifted by +/- 0.35 x the average daily range: same
                distances, same days, but not a level. A level type only counts as
                "respected" if it beats its controls.

During the session (London open to +9 h) the FIRST touch of each level is
traded two ways, costs included (the bar's spread):

  limit      buy at a support / sell at a resistance on the touch, stop 1 unit
             beyond, target 2 units (unit = --unit x ADR). Random walk: ~33 % wins.
  rejection  wait for the touch bar to close back on the level's side, enter at
             that close, stop beyond the touch bar's extreme, target 2R.

Each touch also records its relative tick volume (bar volume / median of the
same minute over the previous 20 days), so the tables split by volume.

    python levels.py study --symbols EURUSD GBPUSD AUDUSD --tf M5
"""

import argparse
import datetime
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from opening import load, load_point, london_open_server, ts, BtError  # noqa: E402

SESSION_MIN = 9 * 60
REAL = ("PDH", "PDL", "PDC", "PWH", "PWL", "ASIA_H", "ASIA_L", "POC", "VAH", "VAL", "R00", "R50")


def by_day(b):
    days = {}
    for i, t in enumerate(b["t"]):
        days.setdefault(ts(t).date(), []).append(i)
    return days


def profile(b, idx, bin_size):
    """Tick-volume profile of some bars: (poc, vah, val)."""
    vol = {}
    for i in idx:
        lo, hi = int(b["l"][i] / bin_size), int(b["h"][i] / bin_size)
        n = hi - lo + 1
        for k in range(lo, hi + 1):
            vol[k] = vol.get(k, 0.0) + b["v"][i] / n
    if not vol:
        return None
    poc = max(vol, key=vol.get)
    total, inside = sum(vol.values()), vol[poc]
    lo = hi = poc
    while inside < 0.7 * total:
        up, dn = vol.get(hi + 1, 0.0), vol.get(lo - 1, 0.0)
        if up == 0 and dn == 0:
            break
        if up >= dn:
            hi += 1
            inside += up
        else:
            lo -= 1
            inside += dn
    return (poc + 0.5) * bin_size, (hi + 1) * bin_size, lo * bin_size


def levels_for(b, days, dates, di, pip, adr):
    date = dates[di]
    prev = days[dates[di - 1]]
    today = days[date]
    om = london_open_server(date)
    pre = [i for i in today if ts(b["t"][i]).hour * 60 + ts(b["t"][i]).minute < om]
    lv = {"PDH": max(b["h"][i] for i in prev), "PDL": min(b["l"][i] for i in prev), "PDC": b["c"][prev[-1]]}
    wk = date.isocalendar()[1]
    pw = [i for d in dates[max(0, di - 10):di] if d.isocalendar()[1] == (wk - 1 if wk > 1 else 52) for i in days[d]]
    if pw:
        lv["PWH"], lv["PWL"] = max(b["h"][i] for i in pw), min(b["l"][i] for i in pw)
    if len(pre) >= 4:
        lv["ASIA_H"], lv["ASIA_L"] = max(b["h"][i] for i in pre), min(b["l"][i] for i in pre)
    prof = profile(b, prev, pip)
    if prof:
        lv["POC"], lv["VAH"], lv["VAL"] = prof
    out = [(k, v) for k, v in lv.items()]
    sess = [i for i in today if ts(b["t"][i]).hour * 60 + ts(b["t"][i]).minute >= om]
    if not sess:
        return [], [], om
    o = b["o"][sess[0]]
    step = 50 * pip
    k0 = int((o - adr / 2) / step)
    for k in range(k0, k0 + int(adr / step) + 2):
        v = k * step
        if abs(v - o) <= adr / 2:
            out.append(("R00" if k % 2 == 0 else "R50", v))
    controls = []
    for name, v in out:
        for s in (1, -1):
            controls.append(("CONTROL", v + s * 0.35 * adr))
    return out + controls, sess, om


def first_touch(b, sess, level, side, end_i):
    """Index (in sess) of the first bar that reaches the level from `side` (+1: price above, support)."""
    for n, i in enumerate(sess):
        if i > end_i:
            return None
        if (side == 1 and b["l"][i] <= level) or (side == -1 and b["h"][i] >= level):
            return n
    return None


def outcome(b, sess, n, level, side, unit, point, rr=2.0):
    """The two trades on a touch. Returns (limit_R, rejection_R or None)."""
    i = sess[n]
    res = []
    # limit: entry at the level, stop one unit beyond, target rr units
    sp = b["sp"][i] * point
    entry = level + (sp if side == 1 else 0)
    stop, target = level - side * unit, level + side * rr * unit
    res.append(walk(b, sess, n, entry, stop, target, side, point, include_first=True))
    # rejection: touch bar closes back on the level's side
    c = b["c"][i]
    if side * (c - level) > 0:
        entry = c + (sp if side == 1 else 0)
        ext = b["l"][i] if side == 1 else b["h"][i] + sp
        stop = ext - side * 0.1 * unit
        risk = (entry - stop) * side
        if risk > 0:
            target = entry + side * rr * risk
            res.append(walk(b, sess, n + 1, entry, stop, target, side, point, include_first=True, risk=risk))
        else:
            res.append(None)
    else:
        res.append(None)
    return res


def walk(b, sess, n, entry, stop, target, side, point, include_first, risk=None):
    risk = risk or abs(entry - stop)
    for k in range(n, len(sess)):
        i = sess[k]
        sp = b["sp"][i] * point
        lo_exit = b["l"][i] + (0 if side == 1 else sp)
        hi_exit = b["h"][i] + (0 if side == 1 else sp)
        if (side == 1 and lo_exit <= stop) or (side == -1 and hi_exit >= stop):
            return -1.0
        if k == n and include_first and n == 0:
            pass
        if (side == 1 and hi_exit >= target) or (side == -1 and lo_exit <= target):
            return (target - entry) * side / risk
    i = sess[-1]
    exit_px = b["c"][i] + (0 if side == 1 else b["sp"][i] * point)
    return (exit_px - entry) * side / risk


def study_symbol(data, symbol, tf, unit_frac, rng):
    b = load(data, symbol, tf)
    point = load_point(data, symbol)
    pip = point * 10
    step = 5 if tf == "M5" else 15
    days = by_day(b)
    dates = [d for d in sorted(days) if d.weekday() < 5 and len(days[d]) > 0.7 * 1440 / step]
    ranges = [max(b["h"][i] for i in days[d]) - min(b["l"][i] for i in days[d]) for d in dates]
    vol_hist = {}
    for i in range(len(b["t"])):
        t = ts(b["t"][i])
        vol_hist.setdefault(t.hour * 60 + t.minute, []).append((t.date(), b["v"][i]))
    touches = []
    for di in range(15, len(dates)):
        date = dates[di]
        adr = statistics.mean(ranges[di - 14:di])
        unit = unit_frac * adr
        lvls, sess, om = levels_for(b, days, dates, di, pip, adr)
        if not sess:
            continue
        sess = [i for i in sess if (ts(b["t"][i]) - ts(b["t"][sess[0]])).total_seconds() < SESSION_MIN * 60]
        if len(sess) < 0.7 * SESSION_MIN / step:
            continue
        o = b["o"][sess[0]]
        for name, lv in lvls:
            if abs(o - lv) < 0.05 * adr:
                continue
            side = 1 if o > lv else -1
            n = first_touch(b, sess, lv, side, sess[-1])
            if n is None or n >= len(sess) - 3:
                continue
            i = sess[n]
            t = ts(b["t"][i])
            hist = [v for d, v in vol_hist[t.hour * 60 + t.minute] if d < date][-20:]
            rvol = b["v"][i] / statistics.median(hist) if len(hist) >= 10 and statistics.median(hist) else None
            lim, rej = outcome(b, sess, n, lv, side, unit, point)
            touches.append({"symbol": symbol, "date": date, "type": name, "side": side, "rvol": rvol,
                            "limit": lim, "rejection": rej, "minute": n * step})
    return touches


def line(name, xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return "  %-26s    0" % name
    wins = sum(x > 0 for x in xs)
    return "  %-26s %5d  acerto %5.1f%%  exp %+.3fR" % (name, len(xs), 100.0 * wins / len(xs), statistics.mean(xs))


def report(touches, split):
    for per, sel in (("AJUSTE ate %s" % split, lambda t: t["date"] <= split),
                     ("VALIDACAO depois", lambda t: t["date"] > split)):
        ts_ = [t for t in touches if sel(t)]
        print("\n%s  (%d toques)" % (per, len(ts_)))
        print("  %-26s %5s  %s" % ("regiao", "n", "LIMITE no nivel, stop 1u, alvo 2u (aleatorio ~33%)"))
        for k in REAL + ("CONTROL",):
            print(line(k, [t["limit"] for t in ts_ if t["type"] == k]))
        print("  %-26s %5s  %s" % ("regiao", "n", "REJEICAO: fechou de volta, stop no pavio, alvo 2R"))
        for k in REAL + ("CONTROL",):
            print(line(k, [t["rejection"] for t in ts_ if t["type"] == k]))
        print("  volume no toque (todas as regioes reais, rejeicao):")
        real = [t for t in ts_ if t["type"] != "CONTROL" and t["rvol"] is not None]
        ctrl = [t for t in ts_ if t["type"] == "CONTROL" and t["rvol"] is not None]
        for lo, hi, nm in ((0, 0.8, "volume baixo  (<0.8x)"), (0.8, 1.5, "volume normal (0.8-1.5x)"),
                           (1.5, 99, "volume alto   (>1.5x)")):
            print(line("real    " + nm, [t["rejection"] for t in real if lo <= t["rvol"] < hi]))
            print(line("controle " + nm, [t["rejection"] for t in ctrl if lo <= t["rvol"] < hi]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Regioes que o mercado respeita (e o volume no toque).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("study")
    p.add_argument("--symbols", nargs="+", default=["EURUSD", "GBPUSD", "AUDUSD"])
    p.add_argument("--data", default="trader/data")
    p.add_argument("--tf", default="M5", choices=["M5", "M15"])
    p.add_argument("--unit", type=float, default=0.15, help="unidade de stop = fracao do range diario medio")
    p.add_argument("--split", default="2026-03-31")
    p.add_argument("--by-symbol", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd != "study":
        ap.print_help()
        return 1
    try:
        rng = random.Random(7)
        split = datetime.date.fromisoformat(a.split)
        allt = []
        for s in a.symbols:
            tt = study_symbol(a.data, s, a.tf, a.unit, rng)
            allt += tt
            if a.by_symbol:
                print("\n===== %s" % s)
                report(tt, split)
        print("\n===== TODOS: %s, %s, unidade %.2f ADR" % (" ".join(a.symbols), a.tf, a.unit))
        report(allt, split)
        return 0
    except BtError as e:
        print("levels.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
