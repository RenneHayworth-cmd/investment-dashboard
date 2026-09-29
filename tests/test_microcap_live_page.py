import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest
from datetime import datetime
from zoneinfo import ZoneInfo


class MicrocapLivePageTests(unittest.TestCase):
    def test_empty_page_loads_without_market_network_or_etf_ledger_access(self):
        temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(temp_dir.name) / "cache.db"
        page_path = Path(__file__).parents[1] / "pages" / "11_微盘实盘.py"
        with (
            patch("core.db.DB_PATH", database_path),
            patch("core.db.ensure_dirs"),
            patch("components.microcap_live.dashboard.load_microcap_histories", return_value=({}, {})) as history_mock,
            patch("components.microcap_live.dashboard.fetch_microcap_realtime_quotes", return_value=({}, {})) as quote_mock,
            patch("components.microcap_live.rotation._load_snapshots", return_value=pd.DataFrame()),
            patch("components.microcap_live.rotation.fetch_microcap_stocks") as rotation_fetch,
        ):
            app = AppTest.from_file(str(page_path), default_timeout=30).run()
        self.assertEqual(list(app.exception), [])
        self.assertEqual([tab.label for tab in app.tabs], ["账户总览", "轮动监控", "成交与持仓", "资金与权益", "交割单导入", "费用设置"])
        self.assertIn("缺少BK1158收盘成分快照", " ".join(item.value for item in app.warning))
        rotation_fetch.assert_not_called()
        self.assertIn("暂无持仓", " ".join(item.value for item in app.info))
        history_mock.assert_called_once()
        quote_mock.assert_not_called()
        with sqlite3.connect(database_path) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("microcap_live_trades", tables)
        self.assertIn("live_trades", tables)
        self.assertFalse(pd.read_sql_query("SELECT * FROM live_trades", sqlite3.connect(database_path)).shape[0])
        temp_dir.cleanup()

    def test_dashboard_renders_injected_microcap_holdings(self):
        from core.db import init_db
        from services.microcap_live_trading import add_microcap_cash_flow, add_microcap_trade

        temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(temp_dir.name) / "cache.db"
        page_path = Path(__file__).parents[1] / "pages" / "11_微盘实盘.py"
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        with (
            patch("core.db.DB_PATH", database_path),
            patch("core.db.ensure_dirs"),
        ):
            init_db()
            add_microcap_cash_flow(flow_date=today, entry_type="期初资金", amount=2000)
            add_microcap_trade(
                trade_date=today, symbol="600000", name="浦发银行", side="买入", price=10, quantity=100,
            )
            with (
                patch("components.microcap_live.dashboard.load_microcap_histories", return_value=(
                    {"600000": pd.DataFrame({"date": [today], "close": [10.5]})}, {}
                )),
                patch("components.microcap_live.dashboard._market_open", return_value=False),
                patch("components.microcap_live.rotation._load_snapshots", return_value=pd.DataFrame()),
            ):
                app = AppTest.from_file(str(page_path), default_timeout=30).run()
        self.assertEqual(list(app.exception), [])
        rendered = " ".join(item.value for item in app.markdown if isinstance(item.value, str))
        self.assertIn("浦发银行", rendered)
        self.assertTrue(any("2,045.00" in metric.value for metric in app.metric))
        temp_dir.cleanup()


    def test_dashboard_renders_nav_and_daily_pnl_chart(self):
        from core.db import init_db
        from services.microcap_live_trading import add_microcap_cash_flow, add_microcap_trade

        temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(temp_dir.name) / "cache.db"
        page_path = Path(__file__).parents[1] / "pages" / "11_微盘实盘.py"
        with (
            patch("core.db.DB_PATH", database_path),
            patch("core.db.ensure_dirs"),
        ):
            init_db()
            add_microcap_cash_flow(flow_date="2026-09-22", entry_type="期初资金", amount=10000)
            add_microcap_trade(
                trade_date="2026-09-22", symbol="600000", name="浦发银行", side="买入", price=10, quantity=500,
            )
            histories = {
                "600000": pd.DataFrame({
                    "date": ["2026-09-22", "2026-09-23", "2026-09-24"],
                    "close": [10.0, 9.8, 10.2],
                })
            }
            with (
                patch("components.microcap_live.dashboard.load_microcap_histories", return_value=(histories, {})),
                patch("components.microcap_live.dashboard._market_open", return_value=False),
                patch("components.microcap_live.rotation._load_snapshots", return_value=pd.DataFrame()),
            ):
                app = AppTest.from_file(str(page_path), default_timeout=30).run()
        self.assertEqual(list(app.exception), [])
        captions = [c.value for c in app.caption]
        self.assertIn("横坐标仅排列完整正式估值交易日，周末和节假日已自动跳过。", captions)
        charts = app.get("plotly_chart")
        self.assertGreaterEqual(len(charts), 1)
        spec = json.loads(charts[0].proto.spec)
        bars = [t for t in spec["data"] if t.get("type") == "bar"][0]
        # 3 dates should produce an adaptive bar width of 0.09 instead of default wide category block
        self.assertEqual(bars.get("width"), 0.09)
        # Check segmented control exists for time period
        self.assertTrue(any("全部" in ctrl.options for ctrl in app.segmented_control))
        temp_dir.cleanup()

    @staticmethod
    def _rotation_snapshots(day):
        rows = []
        for i in range(1, 23):
            cap = 5.0 if i == 21 else 10.0 + i  # 600021 shrinks into the top-20, pushing 600020 out
            rows.append({"快照日期": day, "快照时间": f"{day} 15:05:00", "代码": f"{600000 + i:06d}",
                         "名称": f"股票{i:02d}", "总市值(亿元)": cap, "成交量": 1000.0, "成交额": 1e6, "是否停牌": False})
        return pd.DataFrame(rows)

    def _seed_rotation_ledger(self, day):
        from core.db import init_db
        from services.microcap_live_trading import add_microcap_cash_flow, add_microcap_trade

        init_db()
        add_microcap_cash_flow(flow_date=day, entry_type="期初资金", amount=300000)
        for i in range(1, 21):
            add_microcap_trade(trade_date=day, symbol=f"{600000 + i:06d}", name=f"股票{i:02d}", side="买入", price=10, quantity=1000)

    def test_rotation_tab_lists_out_and_new_names_without_network(self):
        temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(temp_dir.name) / "cache.db"
        page_path = Path(__file__).parents[1] / "pages" / "11_微盘实盘.py"
        with (
            patch("core.db.DB_PATH", database_path),
            patch("core.db.ensure_dirs"),
        ):
            self._seed_rotation_ledger("2026-09-24")
            with (
                patch("components.microcap_live.dashboard.load_microcap_histories", return_value=({}, {})),
                patch("components.microcap_live.dashboard._market_open", return_value=False),
                patch("components.microcap_live.rotation._market_open", return_value=False),
                patch("components.microcap_live.rotation._load_snapshots", return_value=self._rotation_snapshots("2026-09-24")),
                patch("components.microcap_live.rotation.fetch_microcap_stocks") as fetch_mock,
            ):
                app = AppTest.from_file(str(page_path), default_timeout=30).run()
        self.assertEqual(list(app.exception), [])
        markdown = " ".join(item.value for item in app.markdown if isinstance(item.value, str))
        self.assertIn("持仓已掉出前20（1只）", markdown)
        self.assertIn("新进前20未买入（1只）", markdown)
        frames = [frame.value for frame in app.dataframe]
        self.assertTrue(any("600020" in frame.get("代码", pd.Series(dtype=str)).tolist() and "第21名" in frame.get("当前排名", pd.Series(dtype=str)).tolist() for frame in frames))
        self.assertTrue(any("600021" in frame.get("代码", pd.Series(dtype=str)).tolist() and "第1名" in frame.get("排名", pd.Series(dtype=str)).tolist() for frame in frames))
        self.assertTrue(any(button.label == "获取盘中实时排名" and button.disabled for button in app.button))
        fetch_mock.assert_not_called()
        temp_dir.cleanup()

    def test_rotation_realtime_button_ranks_without_saving_snapshot(self):
        temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(temp_dir.name) / "cache.db"
        page_path = Path(__file__).parents[1] / "pages" / "11_微盘实盘.py"
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        live = self._rotation_snapshots(today).rename(columns={"快照日期": "日期", "快照时间": "更新时间"})
        with (
            patch("core.db.DB_PATH", database_path),
            patch("core.db.ensure_dirs"),
        ):
            self._seed_rotation_ledger("2026-09-24")
            with (
                patch("components.microcap_live.dashboard.load_microcap_histories", return_value=({}, {})),
                patch("components.microcap_live.dashboard._market_open", return_value=False),
                patch("components.microcap_live.rotation._market_open", return_value=True),
                patch("components.microcap_live.rotation._load_snapshots", return_value=self._rotation_snapshots("2026-09-24")),
                patch("components.microcap_live.rotation.fetch_microcap_stocks", return_value=live) as fetch_mock,
                patch("services.microcap.save_microcap_constituent_snapshot") as save_mock,
            ):
                app = AppTest.from_file(str(page_path), default_timeout=30).run()
                fetch_mock.assert_not_called()
                button = next(b for b in app.button if b.label == "获取盘中实时排名")
                app = button.click().run()
        self.assertEqual(list(app.exception), [])
        fetch_mock.assert_called_once()
        save_mock.assert_not_called()
        self.assertTrue(any("实时，未保存" in caption.value for caption in app.caption))
        temp_dir.cleanup()

    def test_rotation_realtime_flags_delayed_quotes(self):
        temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(temp_dir.name) / "cache.db"
        page_path = Path(__file__).parents[1] / "pages" / "11_微盘实盘.py"
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        live = self._rotation_snapshots(today).rename(columns={"快照日期": "日期", "快照时间": "更新时间"})
        live.attrs.update(source_hosts=["push2delay.eastmoney.com"], delayed=True, quote_time=f"{today} 14:41:00")
        with (
            patch("core.db.DB_PATH", database_path),
            patch("core.db.ensure_dirs"),
        ):
            self._seed_rotation_ledger("2026-09-24")
            with (
                patch("components.microcap_live.dashboard.load_microcap_histories", return_value=({}, {})),
                patch("components.microcap_live.dashboard._market_open", return_value=False),
                patch("components.microcap_live.rotation._market_open", return_value=True),
                patch("components.microcap_live.rotation._load_snapshots", return_value=self._rotation_snapshots("2026-09-24")),
                patch("components.microcap_live.rotation.fetch_microcap_stocks", return_value=live),
            ):
                app = AppTest.from_file(str(page_path), default_timeout=30).run()
                app = next(b for b in app.button if b.label == "获取盘中实时排名").click().run()
        self.assertEqual(list(app.exception), [])
        captions = " ".join(caption.value for caption in app.caption)
        self.assertIn("延时行情，未保存", captions)
        self.assertIn(f"行情时间 {today} 14:41:00", captions)
        self.assertIn("push2delay.eastmoney.com", captions)
        self.assertTrue(any("延时行情" in warning.value for warning in app.warning))
        temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
