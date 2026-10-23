#!/usr/bin/env python3
"""London-open impulse: export MT5 history and backtest the opening setup.

The setup (UK100 by default, all times are the broker's server time):

  window     09:45-10:00 pre-open range, entries 10:00-11:30, flat at 12:00
  volume     tick volume of the first minutes after the open vs the same
             window over the previous 20 sessions (relative volume)
  direction  where price closes the signal window: a bearish window -> look
             for a sell, a bullish one -> a buy (optionally in the outer third
             of its range)
  context    EMA 9 / SMA 21 on M15 on the same side as the signal
  entry      market after the signal, a pullback to the EMA 9 rejected and
             broken (averages apart), a fresh EMA 9 x SMA 21 cross (averages
             together), or auto: pullback or cross depending on the separation
  exit       stop beyond the signal extreme or the pullback swing, target in
             R multiples, flat at 12:00 whatever happens

    python opening.py export --symbol UK100 --out trader/data
    python opening.py backtest --data trader/data --symbol UK100 --tf M5
    python opening.py backtest ... --grid            # every variant, in-sample vs out-of-sample
    python opening.py backtest ... --mode pullback --target 1.5 --trades

Bars are bid prices. A buy fills at bid + spread and exits at bid; a sell fills
at bid and exits at bid + spread. When a bar touches both the stop and the
target, the stop is counted first. The standard library only, except export.
"""

import argparse
import bisect
import csv
import datetime
import itertools
import os
import sys

EPOCH = datetime.datetime(1970, 1, 1)


class BtError(Exception):
    pass


# ---------------------------------------------------------------- data

def ts(sec):
    return EPOCH + datetime.timedelta(seconds=int(sec))


def export(symbol, out, terminal_path=None, tfs=("M1", "M5", "M15")):
    import MetaTrader5 as mt5
    ok = mt5.initialize(path=terminal_path) if terminal_path else mt5.initialize()
    if not ok:
        raise BtError("nao conectou ao MT5: %s" % (mt5.last_error(),))
    try:
        info = mt5.symbol_info(symbol)
        if info is None:
            raise BtError("simbolo %s nao existe" % symbol)
        mt5.symbol_select(symbol, True)
        os.makedirs(out, exist_ok=True)
        for tf in tfs:
            rows, end = {}, datetime.datetime.now() + datetime.timedelta(days=1)
            for _ in range(80):
                start = end - datetime.timedelta(days=30)
                r = mt5.copy_rates_range(symbol, getattr(mt5, "TIMEFRAME_" + tf), start, end)
                if r is None or len(r) == 0:
                    break
                for x in r:
                    rows[int(x["time"])] = (int(x["time"]), float(x["open"]), float(x["high"]), float(x["low"]),
                                            float(x["close"]), int(x["tick_volume"]), int(x["spread"]))
                end = start
            path = os.path.join(out, "%s_%s.csv" % (symbol, tf))
            with open(path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["time", "open", "high", "low", "close", "tick_volume", "spread_points"])
                for k in sorted(rows):
                    w.writerow(rows[k])
            first = ts(min(rows)) if rows else None
            print("%s %s: %d barras desde %s -> %s" % (symbol, tf, len(rows), first, path))
        with open(os.path.join(out, "%s_spec.csv" % symbol), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["point", "digits", "tick_value", "tick_size", "volume_min", "volume_step"])
            w.writerow([info.point, info.digits, info.trade_tick_value, info.trade_tick_size,
                        info.volume_min, info.volume_step])
    finally:
        mt5.shutdown()


def load(data, symbol, tf):
    path = os.path.join(data, "%s_%s.csv" % (symbol, tf))
    if not os.path.exists(path):
        raise BtError("sem %s; rode export primeiro" % path)
    cols = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": [], "sp": []}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            cols["t"].append(int(row["time"]))
            cols["o"].append(float(row["open"]))
            cols["h"].append(float(row["high"]))
            cols["l"].append(float(row["low"]))
            cols["c"].append(float(row["close"]))
            cols["v"].append(int(row["tick_volume"]))
            cols["sp"].append(int(row["spread_points"]))
    return cols


def load_point(data, symbol):
    path = os.path.join(data, "%s_spec.csv" % symbol)
    with open(path, newline="") as fh:
        return float(next(csv.DictReader(fh))["point"])


# ---------------------------------------------------------------- indicators over a whole series

def ema_series(x, n):
    out = [None] * len(x)
    if len(x) < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(x[:n]) / float(n)
    out[n - 1] = prev
    for i in range(n, len(x)):
        prev = x[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def sma_series(x, n):
    out = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def atr_series(h, l, c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def with_indicators(b):
    b["ema9"] = ema_series(b["c"], 9)
    b["sma21"] = sma_series(b["c"], 21)
    b["atr"] = atr_series(b["h"], b["l"], b["c"])
    return b


# ---------------------------------------------------------------- sessions

def sessions(b, step):
    """{date: {minute_of_day: index}} for the entry timeframe."""
    days = {}
    for i, t in enumerate(b["t"]):
        d = ts(t)
        days.setdefault(d.date(), {})[d.hour * 60 + d.minute] = i
    return days


def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def _last_sunday(year, month):
    d = datetime.date(year + (month == 12), month % 12 + 1, 1) - datetime.timedelta(days=1)
    return d - datetime.timedelta(days=(d.weekday() + 1) % 7)


def london_open_server(date):
    """The London cash open (08:00 London) in server minutes. The server runs GMT+2, and GMT+3 while
    US daylight time is on, so the open is 10:00 most of the year and 11:00 in the weeks the UK is
    already (or still) on GMT while the US is on daylight time."""
    uk_dst = _last_sunday(date.year, 3) <= date < _last_sunday(date.year, 10)
    us_dst = _nth_sunday(date.year, 3, 2) <= date < _nth_sunday(date.year, 11, 1)
    utc_open = 8 - (1 if uk_dst else 0)
    return (utc_open + (3 if us_dst else 2)) * 60


def detect_open(date, day):
    m = london_open_server(date)
    return m if m in day else None


# ---------------------------------------------------------------- the backtest

DEFAULTS = {"sig_min": 15, "rvol_min": 0.0, "clv": None, "ctx": "aligned", "mode": "auto", "sep_atr": 0.5,
            "stop": "swing", "target": 1.5, "pre_min": 15, "last_entry_min": 90, "flat_min": 120,
            "max_risk_atr": 4.0, "side": "both", "min_spread_pts": 0, "trigger_bars": 3}


def run(b, ctx_b, step, point, p, start=None, end=None, keep=False):
    """One parameter set over every session. Returns the list of trades (dicts)."""
    p = dict(DEFAULTS, **p)
    days = sessions(b, step)
    ctx_close = [t + 15 * 60 for t in ctx_b["t"]]
    trades, hist_vol = [], []
    for date in sorted(days):
        day = days[date]
        o_min = detect_open(date, day)
        if o_min is None:
            continue
        sig_idx = [day[m] for m in range(o_min, o_min + p["sig_min"], step) if m in day]
        if len(sig_idx) * step < p["sig_min"]:
            continue
        vol = sum(b["v"][i] for i in sig_idx)
        prev = hist_vol[-20:]
        hist_vol.append(vol)
        if (start and date < start) or (end and date > end):
            continue
        if len(prev) < 20:
            continue
        rvol = vol / (sum(prev) / 20.0)
        if rvol < p["rvol_min"]:
            continue
        o, c = b["o"][sig_idx[0]], b["c"][sig_idx[-1]]
        h = max(b["h"][i] for i in sig_idx)
        l = min(b["l"][i] for i in sig_idx)
        rng = h - l
        if rng <= 0 or c == o:
            continue
        clv = (c - l) / rng
        d = 1 if c > o else -1
        if p["clv"] is not None and ((d == -1 and clv > p["clv"]) or (d == 1 and clv < 1 - p["clv"])):
            continue
        if (p["side"] == "sell" and d == 1) or (p["side"] == "buy" and d == -1):
            continue
        sig_end_t = b["t"][sig_idx[-1]] + step * 60
        if p["ctx"] != "none":
            k = bisect.bisect_right(ctx_close, sig_end_t) - 1
            if k < 0 or ctx_b["ema9"][k] is None or ctx_b["sma21"][k] is None:
                continue
            e9, s21, cc = ctx_b["ema9"][k], ctx_b["sma21"][k], ctx_b["c"][k]
            if d * (e9 - s21) <= 0:
                continue
            if p["ctx"] == "strict" and d * (cc - e9) <= 0:
                continue
        pre = [day[m] for m in range(o_min - p["pre_min"], o_min, step) if m in day]
        t = simulate(b, day, o_min, sig_idx, d, h, l, step, point, p)
        if t:
            t.update({"date": date.isoformat(), "dir": "BUY" if d == 1 else "SELL", "rvol": round(rvol, 2),
                      "clv": round(clv, 2),
                      "pre_high": max(b["h"][i] for i in pre) if pre else None,
                      "pre_low": min(b["l"][i] for i in pre) if pre else None})
            trades.append(t)
    return trades


def spread_at(b, i, point, p):
    return max(b["sp"][i], p["min_spread_pts"]) * point


def simulate(b, day, o_min, sig_idx, d, sig_h, sig_l, step, point, p):
    first = o_min + p["sig_min"]
    last_entry = o_min + p["last_entry_min"]
    flat = o_min + p["flat_min"]
    mins = [m for m in range(first, flat + step, step) if m in day]
    entry = stop = None
    entry_m = None
    pending = None   # (trigger price, stop, expires at minute, how)
    for m in mins:
        i = day[m]
        if m > last_entry and pending is None:
            break
        if pending:
            trig, pstop, exp, how = pending
            if m > exp or m > last_entry:
                pending = None
            elif (d == -1 and b["l"][i] <= trig) or (d == 1 and b["h"][i] + spread_at(b, i, point, p) >= trig):
                fill = min(trig, b["o"][i]) if d == -1 else max(trig, b["o"][i] + spread_at(b, i, point, p))
                entry, stop, entry_m, kind = fill, pstop, m, how
                break
        if m > last_entry:
            continue
        e9, s21, a = b["ema9"][i], b["sma21"][i], b["atr"][i]
        j = day.get(m - step)
        if e9 is None or s21 is None or a is None or j is None or b["ema9"][j] is None or b["sma21"][j] is None:
            continue
        sp = spread_at(b, i, point, p)
        mode = p["mode"]
        if mode == "auto":
            mode = "pullback" if abs(e9 - s21) >= p["sep_atr"] * a else "cross"
        if mode == "market":
            entry = b["o"][i] + (sp if d == 1 else 0)
            stop = (sig_h + sp) if d == -1 else (sig_l - 0)
            entry_m, kind = m, "market"
            break
        if mode == "pullback":
            aligned = d * (e9 - s21) > 0
            touched = (d == -1 and b["h"][i] + sp >= e9) or (d == 1 and b["l"][i] <= e9)
            rejected = d * (b["c"][i] - e9) > 0
            if aligned and touched and rejected:
                trig = b["l"][i] if d == -1 else b["h"][i] + sp
                swing = (b["h"][i] + sp) if d == -1 else b["l"][i]
                pending = (trig, swing, m + step * p["trigger_bars"], "pullback")
            continue
        if mode == "cross":
            crossed = d * (e9 - s21) > 0 and d * (b["ema9"][j] - b["sma21"][j]) <= 0
            if crossed:
                k = day.get(m + step)
                if k is None:
                    break
                look = [day[x] for x in range(m - 2 * step, m + step, step) if x in day]
                swing = (max(b["h"][x] for x in look) + sp) if d == -1 else min(b["l"][x] for x in look)
                entry = b["o"][k] + (sp if d == 1 else 0)
                stop, entry_m, kind = swing, m + step, "cross"
                break
    if entry is None:
        return None
    if p["stop"] == "signal" or kind == "market":
        sp0 = spread_at(b, day[entry_m], point, p)
        stop = (sig_h + sp0) if d == -1 else sig_l
    risk = (stop - entry) * -d
    a = b["atr"][day[entry_m]] or 0
    if risk <= 0 or (a and risk > p["max_risk_atr"] * a):
        return None
    target = entry + d * risk * p["target"] if p["target"] else None
    exit_px = None
    why = "tempo"
    for m in range(entry_m, flat + step, step):
        if m not in day:
            continue
        i = day[m]
        sp = spread_at(b, i, point, p)
        if m >= flat:
            exit_px = b["o"][i] + (sp if d == -1 else 0)
            break
        hi_exit = b["h"][i] + (sp if d == -1 else 0)
        lo_exit = b["l"][i] + (sp if d == -1 else 0)
        if (d == -1 and hi_exit >= stop) or (d == 1 and lo_exit <= stop):
            exit_px, why = stop, "stop"
            break
        if target is not None and ((d == -1 and lo_exit <= target) or (d == 1 and hi_exit >= target)):
            exit_px, why = target, "alvo"
            break
    if exit_px is None:
        i = day[max(x for x in day if x <= flat)]
        exit_px = b["c"][i] + (spread_at(b, i, point, p) if d == -1 else 0)
    r = (exit_px - entry) * d / risk
    return {"entry_time": "%02d:%02d" % divmod(entry_m, 60), "entry": round(entry, 2), "stop": round(stop, 2),
            "target": round(target, 2) if target else None, "exit": round(exit_px, 2), "why": why,
            "risk_pts": round(risk, 2), "R": round(r, 3), "kind": kind}


# ---------------------------------------------------------------- stats

def stats(trades):
    n = len(trades)
    if not n:
        return {"n": 0}
    rs = [t["R"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    eq = peak = dd = 0.0
    streak = worst = 0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    gl = -sum(losses)
    return {"n": n, "win": 100.0 * len(wins) / n, "exp": sum(rs) / n, "total": sum(rs),
            "pf": (sum(wins) / gl) if gl > 0 else float("inf"), "dd": dd, "streak": worst}


def fmt_stats(s):
    if not s["n"]:
        return "   0 trades"
    return "%4d trades  acerto %5.1f%%  exp %+.3fR  total %+6.1fR  PF %4.2f  DD %5.1fR  seq.perdas %2d" % (
        s["n"], s["win"], s["exp"], s["total"], s["pf"], s["dd"], s["streak"])


def label(p):
    return "sig%-2d rvol%.1f clv%-4s ctx=%-7s %-8s stop=%-6s alvo=%s" % (
        p["sig_min"], p["rvol_min"], p["clv"], p["ctx"], p["mode"], p["stop"], p["target"] or "tempo")


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Impulso da abertura de Londres: export e backtest.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("export")
    p.add_argument("--symbol", default="UK100")
    p.add_argument("--out", default="trader/data")
    p.add_argument("--terminal-path")
    p = sub.add_parser("backtest")
    p.add_argument("--symbol", default="UK100")
    p.add_argument("--data", default="trader/data")
    p.add_argument("--tf", default="M5", choices=["M1", "M5"])
    p.add_argument("--split", default="2026-03-31", help="fim do periodo de ajuste (in-sample)")
    p.add_argument("--grid", action="store_true")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--min-trades", type=int, default=30)
    p.add_argument("--trades", action="store_true", help="lista cada trade")
    for k, v in DEFAULTS.items():
        flag = "--" + k.replace("_", "-")
        if isinstance(v, str):
            p.add_argument(flag, default=v)
        elif v is None:
            p.add_argument(flag, type=float, default=None)
        else:
            p.add_argument(flag, type=type(v), default=v)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "export":
            export(a.symbol, a.out, a.terminal_path)
            return 0
        if a.cmd != "backtest":
            ap.print_help()
            return 1
        step = 1 if a.tf == "M1" else 5
        b = with_indicators(load(a.data, a.symbol, a.tf))
        ctx = with_indicators(load(a.data, a.symbol, "M15"))
        point = load_point(a.data, a.symbol)
        split = datetime.date.fromisoformat(a.split)
        first, lastd = ts(b["t"][0]).date(), ts(b["t"][-1]).date()
        print("%s %s de %s a %s; ajuste ate %s, validacao depois" % (a.symbol, a.tf, first, lastd, split))
        if not a.grid:
            p = {k: getattr(a, k) for k in DEFAULTS}
            print(label(dict(DEFAULTS, **p)))
            for name, s, e in (("ajuste   ", None, split), ("validacao", split + datetime.timedelta(days=1), None),
                               ("tudo     ", None, None)):
                tr = run(b, ctx, step, point, p, s, e)
                print("  %s %s" % (name, fmt_stats(stats(tr))))
            if a.trades:
                for t in run(b, ctx, step, point, p):
                    print("  %(date)s %(dir)-4s %(entry_time)s %(kind)-8s entrada %(entry)9.2f stop %(stop)9.2f "
                          "saida %(exit)9.2f %(why)-5s %(R)+6.2fR rvol %(rvol).2f" % t)
            return 0
        grid = {"sig_min": [5, 15], "rvol_min": [0.0, 1.3], "clv": [None, 0.34],
                "ctx": ["none", "aligned", "strict"], "mode": ["market", "pullback", "cross", "auto"],
                "stop": ["signal", "swing"], "target": [1.0, 1.5, 2.0, 0.0]}
        keys = list(grid)
        rows = []
        for combo in itertools.product(*[grid[k] for k in keys]):
            p = dict(zip(keys, combo))
            if p["mode"] == "market" and p["stop"] == "swing":
                continue
            ins = stats(run(b, ctx, step, point, p, None, split))
            if ins["n"] < a.min_trades:
                continue
            rows.append((ins["exp"], p, ins))
        rows.sort(key=lambda x: -x[0])
        print("%d variantes com >= %d trades no ajuste. As %d melhores no ajuste, e como foram na validacao:"
              % (len(rows), a.min_trades, a.top))
        for exp, p, ins in rows[:a.top]:
            oos = stats(run(b, ctx, step, point, p, split + datetime.timedelta(days=1), None))
            print(label(dict(DEFAULTS, **p)))
            print("    ajuste    " + fmt_stats(ins))
            print("    validacao " + fmt_stats(oos))
        return 0
    except BtError as e:
        print("opening.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
