import unittest
from datetime import date, datetime
import json
from io import StringIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from services.index_frames import build_export_df
from services.taoxi_microcap_index import (
    TaoxiDataError,
    build_taoxi_series,
    classify_snapshot_halt_status,
    extend_sessions,
    fetch_taoxi_intraday_quote,
    publish_taoxi_datasets,
    refresh_incremental_formal_bars,
    select_snapshot_constituents,
    validate_v3_directory,
)
from services.taoxi_microcap_index import _snapshot_is_close_confirmed


def make_bars(day, codes, ratio=1.1, halted=None):
    halted = set(halted or [])
    rows = []
    for code in codes:
        is_halted = code in halted
        rows.append({
            "date": day, "code": f"sh.{code}", "open": 10 if is_halted else 10 * ratio,
            "close": 10 if is_halted else 10 * ratio, "preclose": 10,
            "pctChg": 0 if is_halted else (ratio - 1) * 100,
            "volume": 0 if is_halted else 100, "amount": 0 if is_halted else 1000,
            "adjustflag": 3, "tradestatus": 0 if is_halted else 1, "isST": 0,
        })
    return rows


class TaoxiMicrocapTests(unittest.TestCase):
    def test_t_minus_one_selection_drives_open_and_close(self):
        first = [f"600{i:03d}" for i in range(20)]
        second = [f"601{i:03d}" for i in range(20)]
        estimates = pd.DataFrame([
            {"date": day, "代码": code, "名称": code, "strategy_rank": rank}
            for day, codes in (("2026-01-05", first), ("2026-01-06", second))
            for rank, code in enumerate(codes, 1)
        ])
        bars = pd.DataFrame(make_bars("2026-01-06", first, 1.10) + make_bars("2026-01-07", second, 1.20))
        history, members, inputs, quality = build_taoxi_series(
            estimates, pd.DataFrame(columns=["快照日期", "代码"]), bars,
            list(pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])),
        )
        self.assertAlmostEqual(history.iloc[1]["open"], 1100)
        self.assertAlmostEqual(history.iloc[1]["close"], 1100)
        self.assertAlmostEqual(history.iloc[2]["close"], 1320)
        self.assertEqual(set(inputs[inputs["trade_date"].eq(pd.Timestamp("2026-01-06"))]["代码"]), set(first))
        self.assertEqual(len(members[members["effective_date"].eq(pd.Timestamp("2026-01-07"))]), 20)
        self.assertTrue(quality["quality_status"].eq("通过").all())

    def test_confirmed_halt_contributes_ratio_one(self):
        codes = [f"600{i:03d}" for i in range(20)]
        estimates = pd.DataFrame([
            {"date": "2026-01-05", "代码": code, "strategy_rank": rank}
            for rank, code in enumerate(codes, 1)
        ])
        bars = pd.DataFrame(make_bars("2026-01-06", codes, 1.10, halted={codes[0]}))
        history, _, inputs, _ = build_taoxi_series(
            estimates, pd.DataFrame(columns=["快照日期", "代码"]), bars,
            list(pd.to_datetime(["2026-01-05", "2026-01-06"])),
        )
        self.assertAlmostEqual(history.iloc[-1]["close"], 1095)
        halted = inputs[inputs["代码"].eq(codes[0])].iloc[0]
        self.assertEqual(halted["停牌状态"], "停牌")
        self.assertEqual(halted["close_ratio"], 1)

    def test_authorized_august_19_estimate_bridges_only_august_20(self):
        base_codes = [f"600{i:03d}" for i in range(20)]
        bridge_codes = [f"601{i:03d}" for i in range(20)]
        estimates = pd.DataFrame([
            {"date": day, "代码": code, "strategy_rank": rank}
            for day, codes in (("2026-01-05", base_codes), ("2026-08-19", bridge_codes))
            for rank, code in enumerate(codes, 1)
        ])
        bars = pd.DataFrame(
            make_bars("2026-08-19", base_codes, 1.0)
            + make_bars("2026-08-20", bridge_codes, 1.1)
        )
        history, members, inputs, _ = build_taoxi_series(
            estimates, pd.DataFrame(columns=["快照日期", "代码", "快照时间"]), bars,
            list(pd.to_datetime(["2026-01-05", "2026-08-19", "2026-08-20"])),
        )
        august_20 = history[history["trade_date"].eq(pd.Timestamp("2026-08-20"))].iloc[0]
        self.assertEqual(august_20["source_stage"], "历史估算补缺")
        self.assertEqual(
            set(inputs[inputs["trade_date"].eq(pd.Timestamp("2026-08-20"))]["代码"]),
            set(bridge_codes),
        )
        bridge = members[members["effective_date"].eq(pd.Timestamp("2026-08-20"))]
        self.assertTrue(bridge["selection_source"].eq("历史估算补缺").all())

    def test_snapshot_missing_fields_use_95_percent_rule(self):
        rows = [{"代码": f"600{i:03d}", "成交量": 1, "成交额": 1, "是否停牌": False} for i in range(400)]
        rows[3]["成交量"] = None
        rows[3]["成交额"] = None
        classified = classify_snapshot_halt_status(pd.DataFrame(rows))
        self.assertEqual(classified.iloc[3]["停牌状态"], "停牌")
        all_blank = pd.DataFrame(rows).assign(成交量=None, 成交额=None)
        self.assertTrue(classify_snapshot_halt_status(all_blank)["停牌状态"].eq("未知").all())

    def test_june_22_unknown_is_supplemented_and_halted_candidate_removed(self):
        codes = [f"600{i:03d}" for i in range(400)]
        snapshot = pd.DataFrame({"代码": codes, "名称": codes, "总市值(亿元)": range(1, 401),
            "成交量": None, "成交额": None, "是否停牌": False})
        bars = pd.DataFrame(make_bars("2026-06-22", codes[:25], 1.0, halted={codes[0]}))
        selected = select_snapshot_constituents(snapshot, bars)
        self.assertNotIn(codes[0], set(selected["代码"]))
        self.assertEqual(len(selected), 20)

    def test_unknown_candidate_blocks_selection(self):
        codes = [f"600{i:03d}" for i in range(400)]
        snapshot = pd.DataFrame({"代码": codes, "名称": codes, "总市值(亿元)": range(1, 401),
            "成交量": None, "成交额": None, "是否停牌": False})
        with self.assertRaisesRegex(TaoxiDataError, "停牌状态未知"):
            select_snapshot_constituents(snapshot, pd.DataFrame())

    def test_ma20_ignores_estimate_segment(self):
        raw = pd.DataFrame({"trade_date": pd.date_range("2026-05-01", periods=80, freq="B"), "close": range(1000, 1080)})
        report = build_export_df(raw, "桃囍微盘", days=1000)
        before = report[pd.to_datetime(report["日期"]).lt("2026-06-23")]
        self.assertTrue(before["桃囍微盘_MA20"].isna().all())
        first_ma = report[report["桃囍微盘_MA20"].notna()].iloc[0]
        self.assertGreaterEqual(pd.Timestamp(first_ma["日期"]), pd.Timestamp("2026-07-20"))

    def test_v3_gate_requires_adjusted_reference_fields(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "request.json").write_text(json.dumps({"schema_version": 3, "fields": "date,open,close"}))
            (root / "report.json").write_text(json.dumps({"publication_gate": "通过", "failures": []}))
            with self.assertRaisesRegex(TaoxiDataError, "preclose"):
                validate_v3_directory(root)

    def test_late_capture_accepts_only_without_intervening_session(self):
        group = pd.DataFrame({"快照日期": ["2026-09-24"], "快照时间": ["2026-09-26 19:56:00"]})
        self.assertTrue(_snapshot_is_close_confirmed(group, list(pd.to_datetime(["2026-09-24"]))))
        self.assertFalse(_snapshot_is_close_confirmed(group, list(pd.to_datetime(["2026-09-24", "2026-09-25"]))))

    def test_sessions_extend_past_frozen_v3_calendar(self):
        v3_sessions = list(pd.to_datetime(["2026-09-30", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"]))
        self.assertEqual(extend_sessions(v3_sessions, date(2026, 10, 1)), v3_sessions)
        extended = extend_sessions(v3_sessions, date(2026, 10, 16))
        self.assertEqual(extended[5:], list(pd.to_datetime(["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19"])))
        # National Day closures come from the maintained holiday table.
        bridged = extend_sessions(list(pd.to_datetime(["2026-09-30"])), date(2026, 9, 30))
        self.assertEqual(bridged[-1], pd.Timestamp("2026-10-08"))

    def test_sessions_refuse_uncovered_holiday_year(self):
        with patch("services.taoxi_microcap_index.uncovered_calendar_years", return_value=[2027]):
            with self.assertRaisesRegex(TaoxiDataError, "未覆盖2027年"):
                extend_sessions(list(pd.to_datetime(["2026-12-31"])), date(2026, 12, 31))

    @patch("services.taoxi_microcap_index.BASE_DATE", pd.Timestamp("2026-06-19"))
    def test_snapshot_after_v3_calendar_end_takes_effect_next_session(self):
        first = [f"600{i:03d}" for i in range(20)]
        codes = [f"601{i:03d}" for i in range(400)]
        estimates = pd.DataFrame([
            {"date": "2026-06-19", "代码": code, "strategy_rank": rank} for rank, code in enumerate(first, 1)
        ])
        snapshot = pd.DataFrame({"快照日期": "2026-06-22", "快照时间": "2026-06-22 15:05:00", "代码": codes,
            "名称": codes, "总市值(亿元)": range(1, 401), "成交量": 1, "成交额": 1, "是否停牌": False})
        bars = pd.DataFrame(make_bars("2026-06-22", first, 1.10) + make_bars("2026-06-23", codes[:20], 1.20))
        frozen = list(pd.to_datetime(["2026-06-19", "2026-06-22"]))
        history, _, _, _ = build_taoxi_series(estimates, snapshot, bars, frozen)
        self.assertEqual(history["trade_date"].max(), pd.Timestamp("2026-06-22"))
        history, members, _, _ = build_taoxi_series(
            estimates, snapshot, bars, extend_sessions(frozen, date(2026, 6, 22))
        )
        self.assertEqual(history["trade_date"].max(), pd.Timestamp("2026-06-23"))
        self.assertAlmostEqual(history.iloc[-1]["close"], 1320)
        self.assertEqual(set(members[members["effective_date"].eq(pd.Timestamp("2026-06-23"))]["代码"]), set(codes[:20]))

    @patch("services.taoxi_microcap_index.save_dataset")
    @patch("services.taoxi_microcap_index.load_dataset")
    def test_partial_formal_bar_cache_is_repaired_before_saving(self, load, save):
        codes = ["600001", "600002"]
        base = pd.DataFrame(make_bars("2026-09-28", codes, 1.0))
        cached = pd.DataFrame(make_bars("2026-09-29", [codes[0]], 1.0)).drop(columns="代码", errors="ignore")
        load.return_value = (cached, None)
        snapshots = pd.DataFrame([
            {"快照日期": day, "代码": code, "总市值(亿元)": rank}
            for day in ("2026-09-28", "2026-09-29")
            for rank, code in enumerate(codes, 1)
        ])
        result = SimpleNamespace(
            error_code="0",
            error_msg="",
            get_data=lambda: pd.DataFrame(make_bars("2026-09-29", [codes[1]], 1.0)),
        )
        fake_baostock = SimpleNamespace(
            login=lambda: SimpleNamespace(error_code="0", error_msg=""),
            logout=lambda: None,
            query_history_k_data_plus=lambda *args, **kwargs: result,
        )
        with patch.dict(sys.modules, {"baostock": fake_baostock}):
            refreshed = refresh_incremental_formal_bars(
                base, snapshots, list(pd.to_datetime(["2026-09-28", "2026-09-29"]))
            )
        day_rows = refreshed[refreshed["date"].eq(pd.Timestamp("2026-09-29"))]
        self.assertEqual(set(day_rows["代码"]), set(codes))
        save.assert_called_once()

    @patch("services.taoxi_microcap_index._fetch_eastmoney_completed_stock_rows", return_value=pd.DataFrame())
    @patch("services.taoxi_microcap_index.save_dataset")
    @patch("services.taoxi_microcap_index.load_dataset")
    def test_incomplete_formal_bar_refresh_is_never_saved(self, load, save, eastmoney):
        codes = ["600001", "600002"]
        base = pd.DataFrame(make_bars("2026-09-28", codes, 1.0))
        cached = pd.DataFrame(make_bars("2026-09-29", [codes[0]], 1.0))
        load.return_value = (cached, None)
        snapshots = pd.DataFrame([
            {"快照日期": day, "代码": code, "总市值(亿元)": rank}
            for day in ("2026-09-28", "2026-09-29")
            for rank, code in enumerate(codes, 1)
        ])
        empty_result = SimpleNamespace(error_code="0", error_msg="", get_data=lambda: pd.DataFrame())
        fake_baostock = SimpleNamespace(
            login=lambda: SimpleNamespace(error_code="0", error_msg=""),
            logout=lambda: None,
            query_history_k_data_plus=lambda *args, **kwargs: empty_result,
        )
        with patch.dict(sys.modules, {"baostock": fake_baostock}):
            with self.assertRaisesRegex(TaoxiDataError, "2026-09-29 600002"):
                refresh_incremental_formal_bars(
                    base, snapshots, list(pd.to_datetime(["2026-09-28", "2026-09-29"]))
                )
        eastmoney.assert_called_once()
        save.assert_not_called()

    @patch("services.taoxi_microcap_index.save_dataset")
    @patch("services.taoxi_microcap_index.load_dataset")
    def test_publish_rejects_rewriting_existing_history(self, load, save):
        existing = pd.DataFrame([
            {"trade_date": "2026-01-05", "open": 1000.0, "close": 1000.0,
             "source_stage": "历史估算", "quality_status": "通过", "input_hash": "base"},
            {"trade_date": "2026-01-06", "open": 1001.0, "close": 1002.0,
             "source_stage": "历史估算", "quality_status": "通过", "input_hash": "day1"},
        ])
        changed = existing.copy()
        changed.loc[1, "close"] = 1002.01
        quality = pd.DataFrame([{"trade_date": "2026-01-06", "quality_status": "通过"}])
        load.return_value = (existing, None)
        with self.assertRaisesRegex(TaoxiDataError, "已发布close点位发生变化"):
            publish_taoxi_datasets(changed, pd.DataFrame([{"rank": 1}]), pd.DataFrame(), quality)
        save.assert_not_called()

    @patch("services.taoxi_microcap_index.save_dataset")
    @patch("services.taoxi_microcap_index.requests.get", side_effect=requests.ConnectionError("代理不可用"))
    @patch("services.taoxi_microcap_index.requests.Session")
    @patch("services.taoxi_microcap_index.load_taoxi_constituents")
    @patch("services.taoxi_microcap_index.load_taoxi_history")
    def test_intraday_requires_all_20_and_never_persists(self, load_history, load_members, session_factory, proxy_get, save):
        codes = [f"600{i:03d}" for i in range(20)]
        load_history.return_value = (pd.DataFrame([{"trade_date": "2026-09-28", "close": 1000}]), None)
        load_members.return_value = (pd.DataFrame([
            {"effective_date": "2026-09-29", "selection_date": "2026-09-28", "代码": code, "rank": rank}
            for rank, code in enumerate(codes, 1)
        ]), None)
        response = Mock()
        response.json.return_value = {"data": {"diff": [
            {"f12": code, "f2": 10.1, "f17": 10, "f18": 10, "f5": 1, "f6": 1, "f124": 1790647200}
            for code in codes[:-1]
        ]}}
        response.raise_for_status.return_value = None
        session_factory.return_value.get.return_value = response
        with self.assertRaisesRegex(TaoxiDataError, "盘中行情缺少"):
            fetch_taoxi_intraday_quote(datetime(2026, 9, 29, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
        save.assert_not_called()

    @patch("services.taoxi_microcap_index._RUNTIME_QUOTE", None)
    @patch("services.taoxi_microcap_index.save_dataset")
    @patch("services.taoxi_microcap_index.requests.Session")
    @patch("services.taoxi_microcap_index.load_taoxi_constituents")
    @patch("services.taoxi_microcap_index.load_taoxi_history")
    def test_intraday_matches_numeric_csv_codes_and_keeps_halted_weight(
        self, load_history, load_members, session_factory, save,
    ):
        now = datetime(2026, 9, 30, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        codes = [f"{i:06d}" for i in range(1, 11)] + [f"600{i:03d}" for i in range(10)]
        history = pd.DataFrame([{"trade_date": "2026-09-29", "close": 1000.0}])
        members = pd.DataFrame([
            {"effective_date": "2026-09-30", "selection_date": "2026-09-29", "代码": code, "rank": rank}
            for rank, code in enumerate(codes, 1)
        ])
        # 复现真实 core.cache 的 CSV 读回：六位代码变成整数，前导零丢失。
        cached_members = pd.read_csv(StringIO(members.to_csv(index=False)))
        self.assertEqual(cached_members["代码"].dtype.kind, "i")
        load_history.return_value = (history, None)
        load_members.return_value = (cached_members, None)
        response = Mock()
        rows = [
            {"f12": code, "f2": 10.2, "f17": 10, "f18": 10, "f5": 1, "f6": 1, "f124": int(now.timestamp())}
            for code in codes
        ]
        rows[0].update({"f2": "-", "f17": "-", "f18": "-", "f5": 0, "f6": 0})
        response.json.return_value = {"data": {"diff": rows}}
        session_factory.return_value.get.return_value = response

        result = fetch_taoxi_intraday_quote(now)

        self.assertAlmostEqual(result["price"], 1019.0)
        self.assertAlmostEqual(result["change_pct"], 1.9)
        self.assertEqual(result["constituent_count"], 20)
        self.assertEqual(result["quote_time"], now)
        requested = session_factory.return_value.get.call_args.kwargs["params"]["secids"].split(",")
        self.assertEqual(len(requested), 20)
        self.assertIn("0.000001", requested)
        self.assertIn("1.600000", requested)
        self.assertEqual(cached_members["代码"].dtype.kind, "i")
        self.assertEqual(history.iloc[0]["close"], 1000.0)
        self.assertFalse(session_factory.return_value.trust_env)
        session_factory.return_value.close.assert_called_once()
        save.assert_not_called()

        with self.subTest("直连失败后使用环境代理"):
            session_factory.return_value.get.side_effect = requests.ConnectionError("直连失败")
            with patch("services.taoxi_microcap_index.requests.get", return_value=response) as proxy_get:
                fallback_result = fetch_taoxi_intraday_quote(now)
            self.assertAlmostEqual(fallback_result["price"], 1019.0)
            proxy_get.assert_called_once()
            self.assertEqual(proxy_get.call_args.kwargs["params"]["secids"].split(","), requested)
            save.assert_not_called()


class TaoxiIntradayFallbackTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 30, 11, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.codes = [f"{i:06d}" for i in range(1, 11)] + [f"600{i:03d}" for i in range(10)]
        history = pd.DataFrame([{"trade_date": "2026-09-29", "close": 1000.0}])
        members = pd.DataFrame([
            {"effective_date": "2026-09-30", "selection_date": "2026-09-29", "代码": code, "rank": rank}
            for rank, code in enumerate(self.codes, 1)
        ])
        self.enterContext(patch("services.taoxi_microcap_index.load_taoxi_history", return_value=(history, None)))
        self.enterContext(patch("services.taoxi_microcap_index.load_taoxi_constituents", return_value=(members, None)))
        self.save = self.enterContext(patch("services.taoxi_microcap_index.save_dataset"))
        self.session = self.enterContext(patch("services.taoxi_microcap_index.requests.Session")).return_value
        self.proxy_get = self.enterContext(patch("services.taoxi_microcap_index.requests.get",
            side_effect=requests.ConnectionError("代理不可用")))
        self.previous = {"price": 999.0, "source": "上次有效数据"}
        self.enterContext(patch("services.taoxi_microcap_index._RUNTIME_QUOTE", self.previous))

    def eastmoney_response(self, codes=None, **overrides):
        response = Mock()
        response.json.return_value = {"data": {"diff": [
            dict({"f12": code, "f2": 10.2, "f17": 10, "f18": 10,
                "f5": 100, "f6": 1020, "f124": int(self.now.timestamp())}, **overrides)
            for code in (self.codes if codes is None else codes)
        ]}}
        return response

    def tencent_response(self, override=None):
        lines = []
        for code in self.codes:
            fields = [""] * 38
            fields[2], fields[3], fields[4], fields[5] = code, "10.2", "10", "10"
            fields[6], fields[30], fields[37] = "100", self.now.strftime("%Y%m%d%H%M%S"), "1.02"
            if override is not None:
                override(code, fields)
            market = "sh" if code.startswith("6") else "sz"
            lines.append(f'v_{market}{code}="' + "~".join(fields) + '";')
        return Mock(content="\n".join(lines).encode("gbk"))

    def test_connection_failure_uses_one_direct_tencent_batch(self):
        self.session.get.side_effect = [requests.ConnectionError("东方财富断开"), self.tencent_response()]
        result = fetch_taoxi_intraday_quote(self.now)
        self.assertAlmostEqual(result["price"], 1020.0)
        self.assertAlmostEqual(result["change_pct"], 2.0)
        self.assertEqual(result["quote_time"], self.now)
        self.assertEqual(result["source"], "腾讯20只成分批量实时行情")
        url = self.session.get.call_args.args[0]
        self.assertEqual(url, "https://qt.gtimg.cn/q=" + ",".join(
            ("sh" if c.startswith("6") else "sz") + c for c in self.codes))
        self.assertEqual(self.session.get.call_count, 2)
        self.assertFalse(self.session.trust_env)
        self.session.close.assert_called_once()
        self.proxy_get.assert_not_called()
        self.save.assert_not_called()

    def test_partial_eastmoney_response_uses_complete_tencent_batch(self):
        self.session.get.side_effect = [self.eastmoney_response(self.codes[:-1]), self.tencent_response()]
        result = fetch_taoxi_intraday_quote(self.now)
        self.assertAlmostEqual(result["price"], 1020.0)
        self.assertEqual(result["constituent_count"], 20)
        self.proxy_get.assert_not_called()
        self.save.assert_not_called()

    def test_halted_tencent_member_keeps_its_weight(self):
        def halted(code, fields):
            if code == self.codes[0]:
                fields[3] = fields[4] = fields[5] = fields[6] = fields[37] = "0"
                fields[30] = "20260929150000"
        self.session.get.side_effect = [requests.ConnectionError("断开"), self.tencent_response(halted)]
        result = fetch_taoxi_intraday_quote(self.now)
        self.assertAlmostEqual(result["price"], 1019.0)
        self.assertEqual(result["quote_time"], self.now)
        self.save.assert_not_called()

    def test_stale_active_tencent_quote_falls_back_to_proxy(self):
        def stale(code, fields):
            if code == self.codes[0]:
                fields[30] = "20260929150000"
        self.session.get.side_effect = [requests.ConnectionError("断开"), self.tencent_response(stale)]
        self.proxy_get.side_effect = None
        self.proxy_get.return_value = self.eastmoney_response()
        result = fetch_taoxi_intraday_quote(self.now)
        self.assertEqual(result["source"], "东方财富20只成分批量实时行情")
        self.proxy_get.assert_called_once()
        self.save.assert_not_called()

    def test_failed_batches_retain_previous_quote_and_never_persist(self):
        from services.taoxi_microcap_index import load_runtime_taoxi_quote
        def mismatched(code, fields):
            if code == self.codes[0]:
                fields[2] = "000999"
        self.session.get.side_effect = [self.eastmoney_response(f124=int(self.now.timestamp()) - 86400),
            self.tencent_response(mismatched)]
        with self.assertRaisesRegex(TaoxiDataError, "盘中行情日期或时间无效.*盘中行情缺少000001"):
            fetch_taoxi_intraday_quote(self.now)
        self.assertEqual(load_runtime_taoxi_quote(), self.previous)
        self.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
