#!/usr/bin/env python3
"""X-ray of one trading day: what drove the dollar, the risk mood and the focus pairs.

For the day before and the day asked (server time, M5 bars):

  forca     strength of each of the 8 currencies: the mean % move of the
            currency across its 7 pairs (28 pairs in all), per session
  dolar     USIndex (the broker's DXY) next to the USD strength of the 28 pairs:
            when they disagree, the DXY is being moved by one leg (often EUR)
  risco     US500, GER40, VIX, XAUUSD, USOIL+ and AUDJPY: risk-on when stocks
            and AUDJPY rise and the VIX falls
  pares     EURUSD, GBPUSD, AUDUSD in pips and in ADR, next to what the
            strengths implied (base strength - quote strength)

Sessions: Asia 00:00 to the London open, London to 15:00 (08:00 New York),
New York 15:00 to 23:00. The hour by hour table of the day shows when the
drive appeared and whether the pairs followed. Yields and the calendar do not
come from MT5; they are read on Investing.

    python drive.py --date 2026-10-23
    python drive.py --date 2026-10-23 --hourly
"""

import argparse
import bisect
import datetime
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from opening import load, load_point, london_open_server, ts, BtError, EPOCH  # noqa: E402
from trend import FX28  # noqa: E402

CCY = ("USD", "EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "JPY")
FOCUS = ("EURUSD", "GBPUSD", "AUDUSD")
MACRO = ("USIndex", "US500", "GER40", "VIX", "XAUUSD", "USOIL+")
NY_FX = 15 * 60
CLOSE = 23 * 60


class Series(object):
    def __init__(self, data, symbol):
        b = load(data, symbol, "M5")
        self.symbol, self.t, self.o, self.c, self.h, self.l = symbol, b["t"], b["o"], b["c"], b["h"], b["l"]
        try:
            self.point = load_point(data, symbol)
        except (IOError, OSError, StopIteration):
            self.point = None

    def price(self, when):
        """Close of the last bar that ended at or before `when` (a datetime)."""
        sec = int((when - EPOCH).total_seconds())
        k = bisect.bisect_right(self.t, sec - 300) - 1
        return self.c[k] if k >= 0 else None

    def move(self, a, b):
        pa, pb = self.price(a), self.price(b)
        if pa is None or pb is None or pa == pb == 0:
            return None
        return 100.0 * math.log(pb / pa)

    def day_range(self, date):
        lo = int((datetime.datetime(date.year, date.month, date.day) - EPOCH).total_seconds())
        i, j = bisect.bisect_left(self.t, lo), bisect.bisect_left(self.t, lo + 86400)
        if j <= i:
            return None
        return max(self.h[i:j]) - min(self.l[i:j])


def strengths(pairs, a, b):
    acc = dict((c, []) for c in CCY)
    for s in pairs.values():
        m = s.move(a, b)
        if m is None:
            continue
        acc[s.symbol[:3]].append(m)
        acc[s.symbol[3:6]].append(-m)
    return dict((c, statistics.mean(v) if v else 0.0) for c, v in acc.items())


def sessions(date):
    d0 = datetime.datetime(date.year, date.month, date.day)
    lo = london_open_server(date)
    at = lambda m: d0 + datetime.timedelta(minutes=m)  # noqa: E731
    return [("Asia", at(0), at(lo)), ("Londres", at(lo), at(NY_FX)), ("NY", at(NY_FX), at(CLOSE)),
            ("DIA", at(0), at(CLOSE))]


def adr(series, date, n=14):
    out, d = [], date - datetime.timedelta(days=1)
    while len(out) < n and d > date - datetime.timedelta(days=40):
        if d.weekday() < 5:
            r = series.day_range(d)
            if r:
                out.append(r)
        d -= datetime.timedelta(days=1)
    return statistics.mean(out) if out else None


def f(x, fmt="%+.2f"):
    return " " * (len(fmt % 0.0)) if x is None else fmt % x


def report_day(date, pairs, macro, title):
    print("\n========== %s  %s (%s)" % (title, date, ("seg", "ter", "qua", "qui", "sex", "sab", "dom")[date.weekday()]))
    ss = sessions(date)
    st = [strengths(pairs, a, b) for _, a, b in ss]
    print("\nFORCA DAS MOEDAS (% medio nos 7 pares)    " +"".join("%9s" % n for n, _, _ in ss))
    order = sorted(CCY, key=lambda c: -st[-1][c])
    for c in order:
        print("  %-38s %s" % (c, "".join("%9s" % f(s[c]) for s in st)))
    print("\nDOLAR")
    print("  %-38s %s" % ("USD forca (28 pares)", "".join("%9s" % f(s["USD"]) for s in st)))
    if "USIndex" in macro:
        print("  %-38s %s" % ("USIndex (DXY) %", "".join("%9s" % f(macro["USIndex"].move(a, b)) for _, a, b in ss)))
    print("\nRISCO (%)")
    for k in MACRO[1:]:
        if k in macro:
            mv = [macro[k].move(a, b) for _, a, b in ss]
            warn = "  (salto: rolagem do CFD? confira no Investing)" if k == "USOIL+" and abs(mv[-1] or 0) > 5 else ""
            print("  %-38s %s%s" % (k, "".join("%9s" % f(x) for x in mv), warn))
    print("  %-38s %s" % ("AUDJPY", "".join("%9s" % f(pairs["AUDJPY"].move(a, b)) for _, a, b in ss)))
    print("\nPARES FOCO       (pips | ADR | implicado pela forca)")
    for p in FOCUS:
        s = pairs[p]
        rng = adr(s, date)
        pip = (s.point or 0.00001) * 10
        cells = []
        for (_, a, b), sx in zip(ss, st):
            pa, pb = s.price(a), s.price(b)
            if pa is None or pb is None:
                cells.append("%26s" % "-")
                continue
            imp = sx[p[:3]] - sx[p[3:]]
            cells.append("%+7.1f %+5.2f %+6.2f%%  " % ((pb - pa) / pip, (pb - pa) / rng if rng else 0, imp))
        print("  %-8s ADR %4.0f pips  %s" % (p, rng / pip if rng else 0, " ".join(cells)))
    print("  %-26s %s" % ("", " ".join("%-26s" % n for n, _, _ in ss)))
    return st


def hourly(date, pairs, macro):
    d0 = datetime.datetime(date.year, date.month, date.day)
    lo = london_open_server(date)
    print("\nHORA A HORA (acumulado desde 00:00; forca em %, pares em pips, risco em %)")
    cols = ["USD", "EUR", "GBP", "AUD", "JPY"]
    hdr = "  hora  " + "".join("%7s" % c for c in cols) + "   DXY%  " + "".join("%8s" % p for p in FOCUS) + \
          "   US500    VIX   OURO  PETRO"
    print(hdr)
    for h in range(1, 24):
        b = d0 + datetime.timedelta(hours=h)
        st = strengths(pairs, d0, b)
        row = "  %02d:00 " % h + "".join("%7s" % f(st[c]) for c in cols)
        row += "  %6s " % f(macro["USIndex"].move(d0, b)) if "USIndex" in macro else "         "
        for p in FOCUS:
            s = pairs[p]
            pa, pb = s.price(d0), s.price(b)
            row += "%8s" % ("-" if pa is None or pb is None else "%+.1f" % ((pb - pa) / ((s.point or 1e-5) * 10)))
        for k in ("US500", "VIX", "XAUUSD", "USOIL+"):
            row += " %6s" % (f(macro[k].move(d0, b)) if k in macro else "")
        mark = "  <- abre Londres" if h * 60 == lo else ("  <- NY 08:00" if h * 60 == NY_FX else "")
        print(row + mark)


def prev_weekday(d):
    d -= datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(description="Raio-x do dia: forca das moedas, dolar, risco e pares foco.")
    ap.add_argument("--data", default="trader/data")
    ap.add_argument("--date", required=True, help="AAAA-MM-DD (horario do servidor)")
    ap.add_argument("--hourly", action="store_true", help="tabela hora a hora do dia")
    a = ap.parse_args(argv)
    try:
        date = datetime.datetime.strptime(a.date, "%Y-%m-%d").date()
        pairs = dict((s, Series(a.data, s)) for s in FX28 if os.path.exists(os.path.join(a.data, s + "_M5.csv")))
        macro = dict((s, Series(a.data, s)) for s in MACRO if os.path.exists(os.path.join(a.data, s + "_M5.csv")))
        missing = [s for s in FX28 if s not in pairs and s != "USDINR"]
        if missing:
            print("aviso: sem M5 de %s" % " ".join(missing))
        report_day(prev_weekday(date), pairs, macro, "DIA ANTERIOR")
        report_day(date, pairs, macro, "DIA ANALISADO")
        if a.hourly:
            hourly(date, pairs, macro)
        print("\nLondres abre %02d:00 no servidor; NY 08:00 = 15:00. Juros e calendario: Investing."
              % (london_open_server(date) // 60))
        return 0
    except BtError as e:
        print("drive.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
