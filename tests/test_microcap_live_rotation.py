import unittest
from unittest.mock import patch
from urllib.parse import urlparse

import pandas as pd

from services.microcap_live_rotation import (
    NEW_LABEL,
    OUT_LABEL,
    build_rotation_monitor,
    compare_with_holdings,
    holdings_as_of,
    rank_realtime,
    rank_snapshot,
    trading_days_since,
)


def snapshot(day, rows):
    """rows: (code, name, market_cap[, halted])"""
    return pd.DataFrame([
        {"快照日期": day, "快照时间": f"{day} 15:05:00", "代码": r[0], "名称": r[1], "总市值(亿元)": r[2],
         "成交量": 0.0 if (len(r) > 3 and r[3]) else 1000.0, "成交额": 0.0 if (len(r) > 3 and r[3]) else 1e6,
         "是否停牌": bool(len(r) > 3 and r[3])}
        for r in rows
    ])


def universe(day, caps):
    """25 ordinary constituents S01..S25 with market caps from ``caps`` (code -> cap overrides)."""
    rows = []
    for i in range(1, 26):
        code = f"{600000 + i:06d}"
        rows.append((code, f"股票{i:02d}", caps.get(code, 10.0 + i)))
    return snapshot(day, rows)


def trade(trade_id, day, code, side, quantity, price=10.0):
    return {"id": trade_id, "trade_date": day, "trade_time": None, "symbol": code, "name": f"名{code}",
            "side": side, "price": price, "quantity": quantity, "commission_amount": 5.0,
            "stamp_tax_amount": 0.0 if side == "买入" else round(price * quantity * 0.0005, 2),
            "created_at": f"{day}T15:30:00"}


EMPTY_ADJ = pd.DataFrame(columns=["id", "event_date", "event_time", "symbol", "name", "adjustment_type",
                                  "quantity_delta", "cost_basis_delta", "created_at"])


class RankSnapshotTests(unittest.TestCase):
    def test_excludes_st_and_suspended_and_breaks_ties_by_code(self):
        ranked = rank_snapshot(snapshot("2026-09-24", [
            ("000003", "丙公司", 12.0), ("000002", "*ST乙", 9.0), ("000004", "丁公司", 8.0, True),
            ("000005", "戊公司", 12.0), ("000001", "甲公司", 11.0),
        ]))
        ranked_only = ranked.dropna(subset=["rank"])
        self.assertEqual(ranked_only["code"].tolist(), ["000001", "000003", "000005"])
        self.assertEqual(ranked_only["rank"].tolist(), [1, 2, 3])
        reasons = dict(zip(ranked["code"], ranked["excluded_reason"]))
        self.assertEqual(reasons["000002"], "ST")
        self.assertEqual(reasons["000004"], "停牌")
        self.assertEqual(ranked.attrs["snapshot_date"], "2026-09-24")

    def test_rejects_multi_day_frames(self):
        both = pd.concat([universe("2026-09-23", {}), universe("2026-09-24", {})])
        with self.assertRaises(ValueError):
            rank_snapshot(both)

    def test_realtime_frame_is_ranked_under_given_day(self):
        live = pd.DataFrame({"代码": ["000001", "000002"], "名称": ["甲", "乙"], "总市值(亿元)": [12.0, 11.0],
                             "成交量": [10.0, 10.0], "成交额": [1e5, 1e5], "是否停牌": [False, False],
                             "日期": ["2026-09-28", "2026-09-28"], "更新时间": ["2026-09-28 14:50:00"] * 2})
        ranked = rank_realtime(live, "2026-09-28")
        self.assertEqual(ranked["code"].tolist(), ["000002", "000001"])
        self.assertEqual(ranked.attrs["snapshot_date"], "2026-09-28")


class CompareTests(unittest.TestCase):
    def test_splits_out_of_top_and_new_names(self):
        ranked = rank_snapshot(snapshot("2026-09-24", [
            *[(f"{600000 + i:06d}", f"股票{i}", 10.0 + i) for i in range(1, 22)],
            ("600099", "ST退", 5.0),
        ]))
        holdings = pd.DataFrame({
            "symbol": ["600001", "600021", "600099", "300999"], "name": ["股票1", "股票21", "ST退", "已调出"],
            "opened_date": ["2026-09-22", "2026-09-22", "2026-09-22", "2026-09-22"],
        })
        result = compare_with_holdings(ranked, holdings, as_of="2026-09-24")
        out = result["out"].set_index("code")
        self.assertEqual(sorted(out.index), ["300999", "600021", "600099"])
        self.assertEqual(out.loc["600021", "rank_label"], "第21名")
        self.assertAlmostEqual(out.loc["600021", "gap_to_cutoff_pct"], (31.0 / 30.0 - 1) * 100)
        self.assertEqual(out.loc["600099", "rank_label"], "已剔除（ST）")
        self.assertEqual(out.loc["300999", "rank_label"], "不在快照前400名")
        self.assertEqual(out.loc["600021", "holding_days"], 2)
        self.assertEqual(result["out"]["code"].tolist()[0], "600021")  # ranked rows first
        self.assertEqual(len(result["new"]), 19)
        self.assertNotIn("600001", result["new"]["code"].tolist())
        self.assertEqual(result["cutoff"], 30.0)


class HoldingsTests(unittest.TestCase):
    def test_holdings_as_of_respects_date_and_reopen(self):
        trades = pd.DataFrame([
            trade(1, "2026-09-22", "600001", "买入", 100),
            trade(2, "2026-09-23", "600001", "卖出", 100),
            trade(3, "2026-09-24", "600001", "买入", 200),
            trade(4, "2026-09-23", "600002", "买入", 300),
        ])
        day22 = holdings_as_of(trades, EMPTY_ADJ, "2026-09-22")
        self.assertEqual(day22["symbol"].tolist(), ["600001"])
        day23 = holdings_as_of(trades, EMPTY_ADJ, "2026-09-23")
        self.assertEqual(day23["symbol"].tolist(), ["600002"])
        day24 = holdings_as_of(trades, EMPTY_ADJ, "2026-09-24").set_index("symbol")
        self.assertEqual(day24.loc["600001", "quantity"], 200)
        self.assertEqual(day24.loc["600001", "opened_date"], "2026-09-24")
        self.assertEqual(day24.loc["600002", "opened_date"], "2026-09-23")

    def test_trading_days_skip_holiday_and_weekend(self):
        self.assertEqual(trading_days_since("2026-09-24", "2026-09-24"), 0)
        self.assertEqual(trading_days_since("2026-09-24", "2026-09-28"), 1)  # 09-25 中秋休市，09-26/27 周末
        self.assertIsNone(trading_days_since(None, "2026-09-28"))


class MonitorTests(unittest.TestCase):
    def test_streaks_and_history_start_at_first_position(self):
        days = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"]
        # 600021 enters the top-20 on 09-23 by shrinking; 600020 is pushed out on 09-23.
        frames = [universe(d, {"600021": 5.0} if d >= "2026-09-23" else {}) for d in days]
        trades = pd.DataFrame([trade(i + 1, "2026-09-22", f"{600000 + i + 1:06d}", "买入", 100) for i in range(20)])
        monitor = build_rotation_monitor(pd.concat(frames), trades, EMPTY_ADJ)
        self.assertEqual(monitor["latest_date"], "2026-09-24")
        self.assertEqual(monitor["out"]["code"].tolist(), ["600020"])
        self.assertEqual(int(monitor["out"].iloc[0]["streak_days"]), 2)
        self.assertEqual(int(monitor["out"].iloc[0]["holding_days"]), 2)  # 09-22 买入，至 09-24 已过 2 个交易日
        self.assertEqual(monitor["new"]["code"].tolist(), ["600021"])
        self.assertEqual(int(monitor["new"].iloc[0]["streak_days"]), 2)
        history = monitor["history"]
        self.assertEqual(sorted(history["date"].unique()), ["2026-09-23", "2026-09-24"])  # 09-21/22 无掉出/未买入
        self.assertEqual(set(history["type"]), {OUT_LABEL, NEW_LABEL})
        self.assertFalse((history["date"] < "2026-09-22").any())

    def test_cash_only_build_day_is_not_listed(self):
        frames = pd.concat([universe("2026-09-22", {}), universe("2026-09-23", {})])
        trades = pd.DataFrame([trade(i + 1, "2026-09-23", f"{600000 + i + 1:06d}", "买入", 100) for i in range(20)])
        monitor = build_rotation_monitor(frames, trades, EMPTY_ADJ)
        self.assertTrue(monitor["history"].empty)
        self.assertEqual(monitor["history_start"], "2026-09-23")

    def test_empty_snapshots(self):
        monitor = build_rotation_monitor(None, None, None)
        self.assertIsNone(monitor["latest_date"])
        self.assertTrue(monitor["out"].empty and monitor["new"].empty and monitor["history"].empty)


class FakeSession:
    """requests.Session stand-in: hosts in ``working`` answer, every other host drops the connection."""

    def __init__(self, working, calls):
        self.working, self.calls, self.trust_env = working, calls, True

    def get(self, url, params=None, headers=None, timeout=None):
        host = urlparse(url).hostname
        self.calls.append(host)
        if host not in self.working:
            raise ConnectionError("Remote end closed connection without response")
        diff = [{"f12": "600001", "f14": "甲", "f20": 1.2e9, "f2": 5.0, "f3": 1.0, "f5": 100, "f6": 5e4, "f124": 1790577600},
                {"f12": "600002", "f14": "乙", "f20": 1.1e9, "f2": 4.0, "f3": 1.0, "f5": 100, "f6": 4e4, "f124": 1790577660}]

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": {"total": 2, "diff": diff}}
        return Response()

    def close(self):
        pass


class FetchProvenanceTests(unittest.TestCase):
    def fetch(self, working):
        from services import microcap

        calls = []
        with patch.object(microcap.requests, "Session", side_effect=lambda: FakeSession(working, calls)):
            frame = microcap.fetch_microcap_stocks(page_size=10, retries=1)
        return frame, calls

    def test_pushguest_is_tried_before_delayed_host(self):
        frame, calls = self.fetch({"pushguest.eastmoney.com", "push2delay.eastmoney.com"})
        self.assertEqual(calls[:4], ["push2.eastmoney.com", "36.push2.eastmoney.com", "48.push2.eastmoney.com",
                                     "pushguest.eastmoney.com"])
        self.assertNotIn("push2delay.eastmoney.com", calls)
        self.assertEqual(frame.attrs["source_hosts"], ["pushguest.eastmoney.com"])
        self.assertFalse(frame.attrs["delayed"])
        self.assertEqual(frame.attrs["quote_time"], "2026-09-28 14:41:00")
        self.assertEqual(frame["代码"].tolist(), ["600002", "600001"])
        self.assertNotIn("行情时间", frame.columns)  # provenance stays out of the cached/displayed columns

    def test_delayed_host_is_flagged(self):
        frame, _ = self.fetch({"push2delay.eastmoney.com"})
        self.assertEqual(frame.attrs["source_hosts"], ["push2delay.eastmoney.com"])
        self.assertTrue(frame.attrs["delayed"])


if __name__ == "__main__":
    unittest.main()
