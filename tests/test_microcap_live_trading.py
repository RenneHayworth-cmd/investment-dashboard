import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core import db
from services.microcap_live_trading import (
    add_microcap_cash_flow,
    add_microcap_position_adjustment,
    add_microcap_trade,
    build_microcap_account_snapshot,
    build_microcap_daily_returns,
    build_microcap_positions,
    commit_microcap_trade_import,
    get_microcap_fee_settings,
    list_microcap_cash_flows,
    list_microcap_import_batches,
    list_microcap_trades,
    preview_microcap_trade_import,
    update_microcap_fee_settings,
    build_microcap_symbol_history,
)


class MicrocapLiveTradingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "cache.db"
        self.patchers = [
            patch("core.db.DB_PATH", self.database_path),
            patch("core.db.ensure_dirs"),
        ]
        for patcher in self.patchers:
            patcher.start()
        db.init_db()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def _fund(self, amount=2000, day="2026-09-01"):
        return add_microcap_cash_flow(flow_date=day, entry_type="期初资金", amount=amount)

    def test_default_fees_and_moving_average_realized_pnl(self):
        self.assertEqual(get_microcap_fee_settings()["buy_commission"], 5.0)
        self._fund(2000)
        add_microcap_trade(
            trade_date="2026-09-01", symbol="600000", name="浦发银行", side="买入",
            price=10, quantity=100,
        )
        add_microcap_trade(
            trade_date="2026-09-02", symbol="600000", name="浦发银行", side="卖出",
            price=12, quantity=50,
        )
        trades = list_microcap_trades()
        self.assertEqual(trades.loc[0, "commission_amount"], 5.0)
        self.assertEqual(trades.loc[1, "stamp_tax_amount"], 0.3)
        position = build_microcap_positions(trades).iloc[0]
        self.assertEqual(position["quantity"], 50)
        self.assertAlmostEqual(position["cost_basis"], 502.5)
        self.assertAlmostEqual(position["realized_pnl"], 92.2)
        self.assertAlmostEqual(position["fee_amount"], 10.3)

    def test_insufficient_cash_and_oversell_are_rejected_atomically(self):
        self._fund(1000)
        with self.assertRaisesRegex(ValueError, "现金为负"):
            add_microcap_trade(
                trade_date="2026-09-01", symbol="600000", name="浦发银行", side="买入",
                price=10, quantity=100,
            )
        self.assertTrue(list_microcap_trades().empty)
        add_microcap_trade(
            trade_date="2026-09-01", symbol="600000", name="浦发银行", side="买入",
            price=9.95, quantity=100,
        )
        with self.assertRaisesRegex(ValueError, "卖出数量超过"):
            add_microcap_trade(
                trade_date="2026-09-02", symbol="600000", name="浦发银行", side="卖出",
                price=10, quantity=101,
            )
        self.assertEqual(len(list_microcap_trades()), 1)

    def test_initial_position_and_bonus_shares_preserve_total_basis(self):
        self._fund(500)
        add_microcap_position_adjustment(
            event_date="2026-09-01", symbol="600000", name="浦发银行",
            adjustment_type="期初持仓", quantity_delta=100, cost_basis_delta=1000,
        )
        add_microcap_position_adjustment(
            event_date="2026-09-02", symbol="600000", name="浦发银行",
            adjustment_type="送转", quantity_delta=100,
        )
        position = build_microcap_positions().iloc[0]
        self.assertEqual(position["quantity"], 200)
        self.assertEqual(position["cost_basis"], 1000)
        self.assertEqual(position["average_cost"], 5)

    def test_cash_deposit_is_neutralized_but_dividend_is_return(self):
        self._fund(1005)
        add_microcap_trade(
            trade_date="2026-09-01", symbol="600000", name="浦发银行", side="买入",
            price=10, quantity=100, commission_amount=5,
        )
        add_microcap_cash_flow(flow_date="2026-09-02", entry_type="资金转入", amount=100)
        add_microcap_cash_flow(flow_date="2026-09-02", entry_type="现金分红", amount=20, symbol="600000")
        prices = {"600000": pd.DataFrame({"date": ["2026-09-01", "2026-09-02"], "close": [10, 11]})}
        daily = build_microcap_daily_returns(price_histories=prices)
        self.assertAlmostEqual(daily.iloc[0]["pnl_amount"], -5)
        self.assertAlmostEqual(daily.iloc[1]["pnl_amount"], 120)
        self.assertAlmostEqual(daily.iloc[1]["total_pnl"], 115)
        self.assertAlmostEqual(daily.iloc[1]["total_assets"], 1220)
        snapshot = build_microcap_account_snapshot(price_histories=prices)
        position = snapshot["positions"].iloc[0]
        self.assertAlmostEqual(position["realized_pnl"], 20)
        self.assertAlmostEqual(position["cumulative_pnl"], 115)
        self.assertAlmostEqual(position["cumulative_buy_cost"], 1005)

    def test_import_uses_statement_total_fee_and_is_idempotent(self):
        self._fund(2000)
        raw = (
            "日期,代码,名称,方向,价格,数量,总费用\n"
            "2026-09-01,600000,浦发银行,买入,10,100,5\n"
            "2026-09-02,600000,浦发银行,卖出,20,100,6\n"
        ).encode("utf-8-sig")
        mapping = {"trade_date": "日期", "symbol": "代码", "name": "名称", "side": "方向",
                   "price": "价格", "quantity": "数量", "total_fee": "总费用"}
        preview = preview_microcap_trade_import(raw, "交割单.csv", mapping)
        self.assertTrue(preview["ok"], preview["errors"])
        self.assertEqual(preview["rows"][0]["commission_amount"], 5)
        self.assertEqual(preview["rows"][0]["stamp_tax_amount"], 0)
        self.assertEqual(preview["rows"][1]["commission_amount"], 5)
        self.assertEqual(preview["rows"][1]["stamp_tax_amount"], 1)
        result = commit_microcap_trade_import(preview)
        self.assertEqual(result["row_count"], 2)
        duplicate = preview_microcap_trade_import(raw, "交割单.csv", mapping)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(list_microcap_trades()), 2)
        self.assertEqual(len(list_microcap_import_batches()), 1)

    def test_import_books_transfer_fees_and_exact_amount_against_net_cash(self):
        self._fund(30000, "2026-09-22")
        # Shape of a real 交割单: 手续费 equals 佣金, 过户费 listed separately, rounded 成交均价.
        raw = (
            "证券代码\t证券名称\t操作\t成交数量\t成交均价\t成交金额\t发生金额\t手续费\t印花税\t其他杂费\t交收日期\t佣金\t过户费\t清算费(B股)\n"
            "688701\t卓锦股份\t证券买入\t1200\t8.69\t10428\t-10433.11\t5\t0\t0\t20260923\t5\t0.11\t0\n"
            "300621\t维业股份\t证券买入\t1400\t7.086\t9920\t-9925\t5\t0\t0\t20260923\t5\t0\t0\n"
        ).encode("gbk")
        mapping = {
            "trade_date": "交收日期", "symbol": "证券代码", "name": "证券名称", "side": "操作",
            "price": "成交均价", "quantity": "成交数量", "gross_amount": "成交金额",
            "commission_amount": "佣金", "stamp_tax_amount": "印花税",
            "other_fee_amount": ["其他杂费", "过户费", "清算费(B股)"], "net_amount": "发生金额",
        }
        preview = preview_microcap_trade_import(raw, "statement.xls", mapping)
        self.assertTrue(preview["ok"], preview["errors"])
        self.assertEqual([row["other_fee_amount"] for row in preview["rows"]], [0.11, 0.0])
        commit_microcap_trade_import(preview)
        snapshot = build_microcap_account_snapshot(price_histories={})
        self.assertAlmostEqual(snapshot["summary"]["cash"], 30000 - 10433.11 - 9925, places=2)
        self.assertAlmostEqual(snapshot["summary"]["fee_amount"], 10.11, places=2)
        position = build_microcap_positions().set_index("symbol")
        self.assertAlmostEqual(position.loc["300621", "cost_basis"], 9925, places=2)

        unmapped = {key: value for key, value in mapping.items() if key != "other_fee_amount"}
        rejected = preview_microcap_trade_import(raw + b" ", "statement2.xls", unmapped)
        self.assertFalse(rejected["ok"])
        self.assertIn("可能有费用列未映射", rejected["errors"][0])

    def test_init_db_adds_other_fee_column_to_existing_ledger(self):
        legacy = Path(self.temp_dir.name) / "legacy.db"
        with sqlite3.connect(legacy) as conn:
            conn.execute(
                "CREATE TABLE microcap_live_trades (id INTEGER PRIMARY KEY, record_key TEXT, trade_date TEXT, "
                "trade_time TEXT, symbol TEXT, name TEXT, side TEXT, price REAL, quantity INTEGER, "
                "commission_amount REAL, stamp_tax_amount REAL, strategy TEXT, notes TEXT, source TEXT, "
                "import_batch_id INTEGER, source_row INTEGER, created_at TEXT)"
            )
            conn.execute("INSERT INTO microcap_live_trades (id, commission_amount, stamp_tax_amount) VALUES (1, 5, 0)")
        with patch("core.db.DB_PATH", legacy):
            db.init_db()
        with sqlite3.connect(legacy) as conn:
            self.assertEqual(conn.execute("SELECT other_fee_amount FROM microcap_live_trades WHERE id=1").fetchone()[0], 0.0)

    def test_manual_trade_other_fee_is_part_of_cost(self):
        self._fund(2000)
        add_microcap_trade(
            trade_date="2026-09-01", symbol="600000", name="浦发银行", side="买入",
            price=10, quantity=100, other_fee_amount=0.01,
        )
        self.assertAlmostEqual(build_microcap_positions().iloc[0]["cost_basis"], 1005.01, places=2)

    def test_invalid_batch_rolls_back_all_rows(self):
        self._fund(1000)
        raw = (
            "日期,代码,方向,价格,数量\n"
            "2026-09-01,600000,买入,9,100\n"
            "2026-09-01,600001,买入,10,100\n"
        ).encode("utf-8-sig")
        mapping = {"trade_date": "日期", "symbol": "代码", "side": "方向", "price": "价格", "quantity": "数量"}
        preview = preview_microcap_trade_import(raw, "bad.csv", mapping)
        self.assertTrue(preview["ok"], preview["errors"])
        with self.assertRaisesRegex(ValueError, "现金为负"):
            commit_microcap_trade_import(preview)
        self.assertTrue(list_microcap_trades().empty)
        self.assertTrue(list_microcap_import_batches().empty)

    def test_fee_rounding_and_initial_position_event_order(self):
        self._fund(0)
        update_microcap_fee_settings(buy_commission=0, sell_commission=0)
        add_microcap_position_adjustment(
            event_date="2026-09-01", event_time="23:59:59", symbol="600000", name="浦发银行",
            adjustment_type="期初持仓", quantity_delta=200, cost_basis_delta=10,
        )
        add_microcap_trade(
            trade_date="2026-09-01", trade_time="10:00:00", symbol="600000", name="浦发银行",
            side="卖出", price=0.05, quantity=200,
        )
        trade = list_microcap_trades().iloc[0]
        self.assertEqual(trade["stamp_tax_amount"], 0.01)
        self.assertTrue(build_microcap_positions().empty)

    def test_xls_reader_dispatches_to_excel_engine(self):
        import services.microcap_live_trading as trading
        expected = pd.DataFrame({"日期": ["2026-09-01"]})
        with patch("services.microcap_live_trading.pd.read_excel", return_value=expected) as reader:
            raw = bytes.fromhex("D0CF11E0A1B11AE1") + b"binary xls content"
            actual = trading.inspect_microcap_import_columns(raw, "statement.xls")
        reader.assert_called_once()
        self.assertEqual(reader.call_args.kwargs["engine"], "xlrd")
        self.assertEqual(actual["columns"], ["日期"])

    def test_gb18030_tabular_text_with_xls_suffix_is_previewable(self):
        raw = (
            "交收日期\t证券代码\t证券名称\t操作\t成交数量\t成交均价\t佣金\t印花税\n"
            "2026-09-23\t600000\t浦发银行\t证券买入\t100\t10.5\t5\t0\n"
            "2026-09-23\t600000\t浦发银行\t证券卖出\t100\t11\t5\t0.55\n"
            "2026-09-23\t600000\t浦发银行\t红利入账\t0\t0\t0\t0\n"
        ).encode("gb18030")
        mapping = {
            "trade_date": "交收日期", "symbol": "证券代码", "name": "证券名称",
            "side": "操作", "price": "成交均价", "quantity": "成交数量",
            "commission_amount": "佣金", "stamp_tax_amount": "印花税",
        }
        preview = preview_microcap_trade_import(raw, "statement.xls", mapping)
        self.assertTrue(preview["ok"], preview["errors"])
        self.assertEqual(len(preview["rows"]), 2)
        self.assertEqual(preview["rows"][0]["side"], "买入")
        self.assertEqual(preview["rows"][0]["commission_amount"], 5)
        self.assertEqual(preview["rows"][1]["stamp_tax_amount"], 0.55)
        self.assertEqual(preview["ignored_rows"], [{
            "source_row": 4, "kind": "cash_dividend", "description": "红利入账",
        }])

    def test_xlsx_preview_and_commit(self):
        self._fund(2000)
        stream = BytesIO()
        pd.DataFrame([{
            "成交日期": "2026-09-01", "代码": "600000", "方向": "买入",
            "价格": 10, "数量": 100, "佣金": 4.5,
        }]).to_excel(stream, index=False)
        raw = stream.getvalue()
        mapping = {"trade_date": "成交日期", "symbol": "代码", "side": "方向",
                   "price": "价格", "quantity": "数量", "commission_amount": "佣金"}
        self.assertIn("成交日期", __import__("services.microcap_live_trading", fromlist=["inspect_microcap_import_columns"]).inspect_microcap_import_columns(raw, "statement.xlsx")["columns"])
        preview = preview_microcap_trade_import(raw, "statement.xlsx", mapping)
        self.assertTrue(preview["ok"], preview["errors"])
        self.assertEqual(preview["rows"][0]["commission_amount"], 4.5)
        commit_microcap_trade_import(preview)
        self.assertEqual(list_microcap_trades().iloc[0]["commission_amount"], 4.5)

    def test_partial_sales_do_not_shrink_lifetime_return_denominator(self):
        self._fund(2000)
        add_microcap_trade(
            trade_date="2026-09-01", symbol="600000", name="浦发银行", side="买入", price=10, quantity=100,
        )
        add_microcap_trade(
            trade_date="2026-09-02", symbol="600000", name="浦发银行", side="卖出", price=12, quantity=50,
        )
        prices = {"600000": pd.DataFrame({"date": ["2026-09-01", "2026-09-02"], "close": [10, 12]})}
        position = build_microcap_account_snapshot(price_histories=prices)["positions"].iloc[0]
        self.assertAlmostEqual(position["cumulative_pnl"], 189.7)
        self.assertAlmostEqual(position["cumulative_buy_cost"], 1005)
        self.assertAlmostEqual(position["cumulative_return_pct"], 189.7 / 1005 * 100)

    def test_closed_symbol_history_keeps_realized_results(self):
        self._fund(3000)
        add_microcap_trade(
            trade_date="2026-09-01", symbol="600000", name="浦发银行", side="买入", price=10, quantity=100,
        )
        add_microcap_trade(
            trade_date="2026-09-02", symbol="600000", name="浦发银行", side="卖出", price=11, quantity=100,
        )
        add_microcap_cash_flow(flow_date="2026-09-01", entry_type="现金分红", amount=20, symbol="600000")
        history = build_microcap_symbol_history(price_histories={})
        self.assertEqual(history.iloc[0]["status"], "已清仓")
        self.assertAlmostEqual(history.iloc[0]["realized_pnl"], 109.45)

    def test_user_commission_change_only_affects_new_trades(self):
        self._fund(4000)
        add_microcap_trade(
            trade_date="2026-09-01", symbol="600000", name="浦发银行", side="买入",
            price=10, quantity=100,
        )
        update_microcap_fee_settings(buy_commission=2, sell_commission=3)
        add_microcap_trade(
            trade_date="2026-09-02", symbol="600001", name="邯郸钢铁", side="买入",
            price=10, quantity=100,
        )
        trades = list_microcap_trades()
        self.assertEqual(trades["commission_amount"].tolist(), [5.0, 2.0])

    def test_daily_position_pnl_respects_intraday_trade_prices(self):
        self._fund(10000, "2026-09-01")
        add_microcap_trade(
            trade_date="2026-09-02", symbol="600000", name="浦发银行", side="买入",
            price=15, quantity=100,
        )
        prices = {"600000": pd.DataFrame({"date": ["2026-09-01", "2026-09-02"], "close": [10, 20]})}
        snapshot = build_microcap_account_snapshot(price_histories=prices)
        self.assertEqual(snapshot["positions"].iloc[0]["daily_pnl"], 495)
        self.assertAlmostEqual(snapshot["summary"]["daily_pnl"], 495)

    def test_daily_series_reports_missing_formal_close_dates(self):
        self._fund(2000, "2026-09-01")
        add_microcap_trade(
            trade_date="2026-09-01", symbol="600000", name="浦发银行", side="买入",
            price=10, quantity=100,
        )
        prices = {"600000": pd.DataFrame({"date": ["2026-09-02"], "close": [10.5]})}
        daily = build_microcap_daily_returns(price_histories=prices)
        self.assertEqual(daily.attrs["missing_days"], [{"date": "2026-09-01", "symbols": ["600000"]}])
        self.assertEqual(daily["date"].dt.strftime("%Y-%m-%d").tolist(), ["2026-09-02"])

    def test_unsettled_session_is_not_reported_as_formal_gap(self):
        self._fund(20000, "2026-09-24")
        add_microcap_trade(
            trade_date="2026-09-24", symbol="600000", name="浦发银行", side="买入",
            price=10, quantity=100,
        )
        add_microcap_trade(
            trade_date="2026-09-28", symbol="600000", name="浦发银行", side="买入",
            price=10, quantity=100,
        )
        prices = {"600000": pd.DataFrame({"date": ["2026-09-24"], "close": [10]})}
        before = build_microcap_account_snapshot(
            price_histories=prices,
            market_now=pd.Timestamp("2026-09-28 15:03:00", tz="Asia/Shanghai").to_pydatetime(),
        )
        self.assertFalse(any("历史正式行情缺口" in text for text in before["warnings"]))
        after = build_microcap_account_snapshot(
            price_histories=prices,
            market_now=pd.Timestamp("2026-09-28 15:10:00", tz="Asia/Shanghai").to_pydatetime(),
        )
        self.assertTrue(any("2026-09-28缺600000" in text for text in after["warnings"]))

    def test_daily_account_baseline_and_intraday_quote_are_not_persisted(self):
        self._fund(1005)
        add_microcap_trade(
            trade_date="2026-09-01", symbol="600000", name="浦发银行", side="买入",
            price=10, quantity=100,
        )
        prices = {"600000": pd.DataFrame({"date": ["2026-09-01"], "close": [10]})}
        daily = build_microcap_daily_returns(price_histories=prices)
        self.assertAlmostEqual(daily.iloc[0]["pnl_amount"], -5)
        snapshot = build_microcap_account_snapshot(
            price_histories=prices,
            quotes={"600000": {"price": 10.5, "quote_time": pd.Timestamp("2026-09-02 10:00:00", tz="Asia/Shanghai"), "source": "test"}},
            market_now=pd.Timestamp("2026-09-02 10:00:00", tz="Asia/Shanghai").to_pydatetime(),
        )
        self.assertEqual(snapshot["positions"].iloc[0]["price_status"], "实时")
        self.assertEqual(snapshot["positions"].iloc[0]["latest_price"], 10.5)
        self.assertEqual(len(snapshot["formal_daily"]), 1)
        self.assertEqual(snapshot["formal_daily"].iloc[0]["date"], pd.Timestamp("2026-09-01"))


if __name__ == "__main__":
    unittest.main()
