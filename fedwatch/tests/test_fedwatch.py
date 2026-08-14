import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fedwatch_engine as eng  # noqa: E402
import fedwatch_radar as radar  # noqa: E402
from fomc_calendar import parse_fed_calendar, upcoming  # noqa: E402


class TestSplit(unittest.TestCase):
    def test_integer(self):
        self.assertEqual(eng._split(0.0), {0: 1.0})
        self.assertEqual(eng._split(1.0), {1: 1.0})

    def test_fraction(self):
        s = eng._split(0.3)
        self.assertAlmostEqual(s[0], 0.7)
        self.assertAlmostEqual(s[1], 0.3)

    def test_negative_fraction(self):
        s = eng._split(-0.4)  # 인하 40% 반영
        self.assertAlmostEqual(s[-1], 0.4)
        self.assertAlmostEqual(s[0], 0.6)

    def test_expectation_preserved(self):
        for x in (0.15, 0.72, 1.4, -0.9):
            s = eng._split(x)
            self.assertAlmostEqual(sum(k * p for k, p in s.items()), x, places=9)


class TestTree(unittest.TestCase):
    def test_probabilities_sum_to_one(self):
        for dist in eng.build_tree([0.3, 0.24, 0.39]):
            self.assertAlmostEqual(sum(dist.values()), 1.0, places=6)

    def test_expectation_matches_cumulative(self):
        deltas = [0.3, 0.24, 0.39]
        cum = 0.0
        for delta, dist in zip(deltas, eng.build_tree(deltas)):
            cum += delta
            self.assertAlmostEqual(sum(k * p for k, p in dist.items()), cum, places=6)

    def test_spread_widens(self):
        tree = eng.build_tree([0.3, 0.3, 0.3])
        self.assertLess(len(tree[0]), len(tree[2]))


class TestCompute(unittest.TestCase):
    """실제 관측치(2026-08-14 종가)로 계산 결과를 고정 검증."""

    def setUp(self):
        self.all_meetings = [dt.date(2026, 9, 16), dt.date(2026, 10, 28),
                             dt.date(2026, 12, 9), dt.date(2027, 1, 26)]
        self.implied = {
            (2026, 8): 100 - 96.368, (2026, 9): 100 - 96.335,
            (2026, 10): 100 - 96.28, (2026, 11): 100 - 96.235,
            (2026, 12): 100 - 96.165, (2027, 1): 100 - 96.13,
            (2027, 2): 100 - 96.10,
        }

    def _run(self):
        return eng.compute(self.all_meetings, self.implied, 3.63, 3.50, 3.75,
                           self.all_meetings)

    def test_anchor_is_tight(self):
        _, diag = self._run()
        # 회의 없는 달의 내재금리는 현 목표 중값과 1bp 이내여야 한다
        self.assertIsNotNone(diag["anchor_gap_bp"])
        self.assertLess(abs(diag["anchor_gap_bp"]), 1.0)

    def test_september_hold_dominant(self):
        outs, _ = self._run()
        sep = dict(outs[0].scenarios)
        self.assertGreater(sep[0], 0.5)
        self.assertLess(sep[0], 0.9)
        self.assertAlmostEqual(sum(sep.values()), 1.0, places=6)

    def test_method_selection(self):
        outs, _ = self._run()
        # 10월 회의는 11월에 회의가 없으므로 다음달 계약을 그대로 읽는다
        self.assertEqual(outs[1].method, "next-month")
        # 12월 회의는 1월에 회의가 있으므로 일수 가중
        self.assertEqual(outs[2].method, "day-weighted")

    def test_path_is_monotonic_hiking(self):
        outs, _ = self._run()
        cums = [o.cum_moves for o in outs]
        self.assertEqual(cums, sorted(cums))
        self.assertGreater(cums[-1], 0.5)

    def test_low_leverage_flagged(self):
        # 회의가 월말(30일)에 있고 다음 달에도 회의가 있으면 신뢰도 하향
        meetings = [dt.date(2026, 9, 28), dt.date(2026, 10, 28)]
        outs, _ = eng.compute(meetings, self.implied, 3.63, 3.50, 3.75, meetings)
        self.assertEqual(outs[0].confidence, "LOW")

    def test_last_day_meeting_not_dropped(self):
        # 회의가 월 마지막 날이어도 누락되지 않고 폴백 경로를 탄다
        meetings = [dt.date(2026, 9, 30), dt.date(2026, 10, 28)]
        outs, _ = eng.compute(meetings, self.implied, 3.63, 3.50, 3.75, meetings)
        self.assertEqual(len(outs), 2)
        self.assertEqual(outs[0].confidence, "LOW")


class TestLabels(unittest.TestCase):
    def test_range_label(self):
        self.assertEqual(eng.range_label(0, 3.50, 3.75), "3.50~3.75%")
        self.assertEqual(eng.range_label(1, 3.50, 3.75), "3.75~4.00%")
        self.assertEqual(eng.range_label(-2, 3.50, 3.75), "3.00~3.25%")

    def test_move_label(self):
        self.assertEqual(eng.move_label(0), "동결")
        self.assertEqual(eng.move_label(2), "인상 50bp")
        self.assertEqual(eng.move_label(-1), "인하 25bp")


class TestCalendar(unittest.TestCase):
    HTML = ("<div>2026 FOMC Meetings</div><p>January 27-28</p><p>March 17-18*</p>"
            "<p>April 28-29</p><p>June 16-17*</p><p>July 28-29</p>"
            "<p>September 15-16*</p><p>October 27-28</p><p>December 8-9*</p>"
            "<div>2027 FOMC Meetings</div><p>January 26-27</p><p>March 16-17*</p>")

    def test_parses_end_dates(self):
        got = parse_fed_calendar(self.HTML)
        self.assertIn(dt.date(2026, 9, 16), got)
        self.assertIn(dt.date(2026, 12, 9), got)
        self.assertIn(dt.date(2027, 1, 27), got)

    def test_caps_at_eight_per_year(self):
        got = parse_fed_calendar(self.HTML)
        self.assertEqual(len([d for d in got if d.year == 2026]), 8)

    def test_month_rollover(self):
        got = parse_fed_calendar("<div>2026 FOMC Meetings</div><p>January 31-1</p>")
        self.assertIn(dt.date(2026, 2, 1), got)

    def test_upcoming_filters_past(self):
        meetings = [dt.date(2026, 7, 29), dt.date(2026, 9, 16), dt.date(2026, 10, 28)]
        self.assertEqual(upcoming(meetings, dt.date(2026, 8, 15), 2),
                         [dt.date(2026, 9, 16), dt.date(2026, 10, 28)])


class TestHistory(unittest.TestCase):
    HIST = {
        "2026-08-01": {"2026-09-16": {"cum": 0.10, "p": {"0": 0.90, "1": 0.10}}},
        "2026-08-13": {"2026-09-16": {"cum": 0.25, "p": {"0": 0.75, "1": 0.25}}},
    }

    def test_lookup_prior_picks_nearest_before(self):
        got = radar.lookup_prior(self.HIST, "2026-08-14", 1)
        self.assertEqual(got["2026-09-16"]["cum"], 0.25)

    def test_lookup_prior_week(self):
        got = radar.lookup_prior(self.HIST, "2026-08-14", 7)
        self.assertEqual(got["2026-09-16"]["cum"], 0.10)

    def test_lookup_prior_missing(self):
        self.assertIsNone(radar.lookup_prior(self.HIST, "2026-07-01", 1))

    def test_delta_formatting(self):
        prior = self.HIST["2026-08-13"]
        self.assertIn("▲", radar.delta_pp(0.30, prior, "2026-09-16", 1))
        self.assertIn("▼", radar.delta_pp(0.60, prior, "2026-09-16", 0))
        self.assertEqual(radar.delta_pp(0.752, prior, "2026-09-16", 0), " (—)")
        self.assertEqual(radar.delta_pp(0.30, None, "2026-09-16", 1), "")


class TestMessage(unittest.TestCase):
    def test_message_renders(self):
        meetings = [dt.date(2026, 9, 16), dt.date(2026, 10, 28), dt.date(2026, 12, 9)]
        implied = {(2026, 8): 3.632, (2026, 9): 3.665, (2026, 10): 3.72,
                   (2026, 11): 3.765, (2026, 12): 3.835, (2027, 1): 3.87}
        outs, diag = eng.compute(meetings, implied, 3.63, 3.50, 3.75, meetings)
        msg = radar.build_message(
            {"effr": 3.63, "target_lo": 3.50, "target_hi": 3.75, "date": "2026-08-14"},
            outs, diag, "2026-08-14", {}, "test")
        self.assertIn("FedWatch", msg)
        self.assertIn("동결", msg)
        self.assertIn("근시일 관측", msg)
        # HTML 태그 균형 (텔레그램 parse_mode=HTML)
        self.assertEqual(msg.count("<b>"), msg.count("</b>"))
        self.assertEqual(msg.count("<i>"), msg.count("</i>"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
