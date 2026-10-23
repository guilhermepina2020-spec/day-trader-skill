import unittest

from _load import load

m = load("trader-mt5", "mt5")


class Indicators(unittest.TestCase):
    def test_ema_seeds_with_the_sma(self):
        out = m.ema([1, 2, 3, 4, 5], 3)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[3], 3.0)
        self.assertAlmostEqual(out[4], 4.0)

    def test_rsi_extremes(self):
        self.assertEqual(m.rsi(list(range(30))), 100.0)
        self.assertAlmostEqual(m.rsi(list(range(30, 0, -1))), 0.0)
        self.assertIsNone(m.rsi([1, 2, 3]))

    def test_atr_of_constant_ranges(self):
        h = [1.1] * 30
        l = [1.0] * 30
        c = [1.05] * 30
        self.assertAlmostEqual(m.atr(h, l, c), 0.1)

    def test_adx_strong_uptrend(self):
        h = [1 + i * 0.01 + 0.005 for i in range(60)]
        l = [1 + i * 0.01 - 0.005 for i in range(60)]
        c = [1 + i * 0.01 for i in range(60)]
        a, pdi, mdi = m.adx(h, l, c)
        self.assertGreater(a, 50)
        self.assertGreater(pdi, mdi)

    def test_pivots(self):
        p = m.pivots(1.2, 1.0, 1.1)
        self.assertAlmostEqual(p["P"], 1.1)
        self.assertAlmostEqual(p["R1"], 1.2)
        self.assertAlmostEqual(p["S1"], 1.0)

    def test_swings(self):
        highs = [1, 2, 5, 2, 1, 2, 3, 2, 1]
        lows = [3, 2, 4, 2, 0, 2, 3, 2, 3]
        sh, sl = m.swings(highs, lows)
        self.assertIn((2, 5), sh)
        self.assertIn((4, 0), sl)

    def test_trend_stack(self):
        self.assertEqual(m.trend_of(5, 4, 3, 2), 2)
        self.assertEqual(m.trend_of(1, 2, 3, 4), -2)
        self.assertEqual(m.trend_of(3, 4, 2, None), 0)


class Sizing(unittest.TestCase):
    def test_lots_round_down_to_the_step(self):
        lots, _ = m.lots_for_risk(20, 150, 0.01, 0.01, 100)
        self.assertAlmostEqual(lots, 0.13)

    def test_cap(self):
        lots, why = m.lots_for_risk(1000, 10, 0.01, 0.01, 100, cap=1.0)
        self.assertEqual(lots, 1.0)
        self.assertIn("limitado", why)

    def test_too_small_for_the_minimum_lot(self):
        lots, why = m.lots_for_risk(1, 500, 0.01, 0.01, 100)
        self.assertEqual(lots, 0.0)
        self.assertIn("minimo", why)


class TheYes(unittest.TestCase):
    def test_plain_yes(self):
        self.assertTrue(m.parse_yes("sim", "EURUSD")[0])
        self.assertTrue(m.parse_yes("Sim, pode enviar", "EURUSD")[0])
        self.assertTrue(m.parse_yes("yes", "EURUSD")[0])

    def test_not_a_yes(self):
        for r in ("ok", "pode", "manda", "", "sim?", "sim para todos", "sim, mas espera", "nao"):
            self.assertFalse(m.parse_yes(r, "EURUSD")[0], r)

    def test_wrong_symbol_or_number(self):
        self.assertFalse(m.parse_yes("sim GBPUSD", "EURUSD", (), ["EURUSD", "GBPUSD"])[0])
        self.assertFalse(m.parse_yes("sim 0.5 lotes", "EURUSD", (0.13, 1.08))[0])
        self.assertTrue(m.parse_yes("sim 0,13 lotes", "EURUSD", (0.13, 1.08))[0])

    def test_fingerprint_changes_with_the_order(self):
        o = {"id": "x", "symbol": "EURUSD", "side": "buy", "volume": 0.1, "sl": 1.08, "tp": 1.09}
        p = dict(o, volume=0.2)
        self.assertNotEqual(m.fingerprint(o), m.fingerprint(p))


if __name__ == "__main__":
    unittest.main()
