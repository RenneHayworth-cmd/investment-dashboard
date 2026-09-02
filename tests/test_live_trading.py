import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core import db
from services.live_trading import (
    add_live_cash_flow,
    add_live_trade,
    append_live_symbol_pnl_total,
    build_live_account_daily,
    build_live_account_snapshot,
    build_live_daily_pnl,
    build_live_daily_returns,
    build_live_period_returns,
    build_live_position_performance,
    build_live_positions,
    build_live_return_month_grid,
    build_live_symbol_pnl_history,
    delete_live_cash_flow,
    delete_live_trade,
    live_close_refresh_due,
    list_live_cash_flows,
    list_live_trades,
    summarize_live_position_performance,
    summarize_live_trades,
)


class LiveTradingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "cache.db"
        self.patchers = [
            patch("core.db.DB_PATH", self.database_path),
            patch("core.db.ensure_dirs"),
            patch(
                "services.live_trading.get_conn",
                side_effect=lambda: sqlite3.connect(self.database_path, timeout=30),
            ),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def test_buy_trade_calculates_fee_and_position_cost(self):
        trade_id, created = add_live_trade(
            trade_date="2026-08-05",
            symbol="159501.SZ",
            name="纳指ETF嘉实",
            side="买入",
            price=2.111,
            quantity=22200,
            fee_rate_pct=0.6,
            strategy="MA25/2%，半仓持有半仓择时",
        )

        trades = list_live_trades()
        summary = summarize_live_trades(trades)
        positions = build_live_positions(trades)

        self.assertTrue(created)
        self.assertEqual(trade_id, 1)
        self.assertEqual(trades.iloc[0]["symbol"], "159501")
        self.assertAlmostEqual(summary["buy_amount"], 46864.2)
        self.assertAlmostEqual(summary["fee_amount"], 281.1852)
        self.assertAlmostEqual(summary["net_investment"], 47145.3852)
        self.assertEqual(positions.iloc[0]["quantity"], 22200)
        self.assertAlmostEqual(positions.iloc[0]["average_cost"], 2.123666)

    def test_schema_upgrade_adds_trade_time_and_cash_flow_table(self):
        with sqlite3.connect(self.database_path) as conn:
            conn.execute(
                """
                CREATE TABLE live_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_key TEXT UNIQUE,
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    fee_rate_pct REAL NOT NULL,
                    strategy TEXT,
                    notes TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
        db.init_db()
        with sqlite3.connect(self.database_path) as conn:
            trade_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(live_trades)")
            }
            cash_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='live_cash_flows'"
            ).fetchone()
        self.assertIn("trade_time", trade_columns)
        self.assertIsNotNone(cash_table)

    def test_trade_time_is_optional_and_controls_display_order(self):
        add_live_trade(
            trade_date="2026-08-05",
            trade_time="09:35:10",
            symbol="159501",
            name="纳指ETF嘉实",
            side="买入",
            price=2,
            quantity=100,
            fee_rate_pct=0,
        )
        add_live_trade(
            trade_date="2026-08-05",
            symbol="518850",
            name="黄金ETF华夏",
            side="买入",
            price=8,
            quantity=100,
            fee_rate_pct=0,
        )
        trades = list_live_trades()
        self.assertEqual(trades.iloc[0]["trade_time"], "09:35:10")
        self.assertTrue(pd.isna(trades.iloc[1]["trade_time"]))

    def test_backfilled_sell_cannot_precede_same_day_buy(self):
        add_live_trade(
            trade_date="2026-08-05",
            trade_time="10:00:00",
            symbol="159501",
            name="纳指ETF嘉实",
            side="买入",
            price=2,
            quantity=100,
            fee_rate_pct=0,
        )

        with self.assertRaisesRegex(ValueError, "本次记录未保存"):
            add_live_trade(
                trade_date="2026-08-05",
                trade_time="09:30:00",
                symbol="159501",
                name="纳指ETF嘉实",
                side="卖出",
                price=2,
                quantity=100,
                fee_rate_pct=0,
            )

        trades = list_live_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades.iloc[0]["side"], "买入")

    def test_cash_ledger_builds_account_equity_and_internal_income(self):
        add_live_cash_flow(
            flow_date="2026-08-05",
            entry_type="期初资金",
            amount=10_000,
        )
        add_live_trade(
            trade_date="2026-08-05",
            symbol="159501",
            name="纳指ETF嘉实",
            side="买入",
            price=10,
            quantity=100,
            fee_rate_pct=1,
        )
        add_live_cash_flow(
            flow_date="2026-08-05",
            entry_type="现金分红",
            amount=20,
            symbol="159501",
        )
        history = pd.DataFrame(
            {"date": pd.to_datetime(["2026-08-05", "2026-08-06"]), "price": [10, 11]}
        )
        account = build_live_account_daily(
            list_live_trades(),
            list_live_cash_flows(),
            {"159501": history},
        )
        self.assertAlmostEqual(account.iloc[0]["cash"], 9010.0)
        self.assertAlmostEqual(account.iloc[0]["total_assets"], 10010.0)
        self.assertAlmostEqual(account.iloc[0]["account_pnl"], 10.0)
        self.assertAlmostEqual(account.iloc[1]["daily_pnl"], 100.0)
        self.assertAlmostEqual(account.iloc[1]["return_base"], 10010.0)

    def test_external_deposit_changes_return_base_but_not_profit(self):
        add_live_cash_flow(
            flow_date="2026-08-05", entry_type="期初资金", amount=10_000
        )
        add_live_trade(
            trade_date="2026-08-05",
            symbol="159501",
            name="纳指ETF嘉实",
            side="买入",
            price=10,
            quantity=100,
            fee_rate_pct=1,
        )
        add_live_cash_flow(
            flow_date="2026-08-06", entry_type="资金转入", amount=1_000
        )
        history = pd.DataFrame(
            {"date": pd.to_datetime(["2026-08-05", "2026-08-06"]), "price": [10, 11]}
        )
        account = build_live_account_daily(
            list_live_trades(), list_live_cash_flows(), {"159501": history}
        )
        self.assertAlmostEqual(account.iloc[1]["external_flow"], 1000.0)
        self.assertAlmostEqual(account.iloc[1]["daily_pnl"], 100.0)
        self.assertAlmostEqual(account.iloc[1]["return_base"], 10990.0)

    def test_withdrawal_does_not_reduce_account_return_base(self):
        add_live_cash_flow(
            flow_date="2026-08-05", entry_type="期初资金", amount=10_000
        )
        add_live_trade(
            trade_date="2026-08-05",
            symbol="159501",
            name="纳指ETF嘉实",
            side="买入",
            price=10,
            quantity=100,
            fee_rate_pct=0,
        )
        add_live_cash_flow(
            flow_date="2026-08-06", entry_type="资金转出", amount=1_000
        )
        history = pd.DataFrame(
            {"date": pd.to_datetime(["2026-08-05", "2026-08-06"]), "price": [10, 10]}
        )
        account = build_live_account_daily(
            list_live_trades(), list_live_cash_flows(), {"159501": history}
        )
        self.assertAlmostEqual(account.iloc[1]["external_flow"], -1000.0)
        self.assertAlmostEqual(account.iloc[1]["daily_pnl"], 0.0)
        self.assertAlmostEqual(account.iloc[1]["return_base"], 10000.0)

    def test_realtime_snapshot_is_transient_and_requires_complete_quotes(self):
        add_live_cash_flow(
            flow_date="2026-08-05", entry_type="期初资金", amount=10_000
        )
        add_live_trade(
            trade_date="2026-08-05",
            symbol="159501",
            name="纳指ETF嘉实",
            side="买入",
            price=10,
            quantity=100,
            fee_rate_pct=0,
        )
        history = pd.DataFrame(
            {"date": pd.to_datetime(["2026-08-05"]), "price": [10.0]}
        )
        snapshot = build_live_account_snapshot(
            list_live_trades(),
            list_live_cash_flows(),
            {"159501": history},
            quotes={
                "159501": {
                    "price": 12.0,
                    "quote_time": datetime(2026, 8, 6, 10, 0),
                }
            },
            market_now=datetime(2026, 8, 6, 10, 1),
            formal_target_date="2026-08-05",
        )
        self.assertEqual(len(history), 1)
        self.assertEqual(snapshot["formal_holding_daily"].iloc[-1]["date"], pd.Timestamp("2026-08-05"))
        self.assertEqual(snapshot["view_holding_daily"].iloc[-1]["date"], pd.Timestamp("2026-08-06"))
        self.assertEqual(snapshot["positions"].iloc[0]["price_status"], "实时")
        self.assertAlmostEqual(snapshot["summary"]["total_assets"], 10200.0)

    def test_partial_realtime_quotes_use_labelled_formal_fallback_and_reconcile(self):
        add_live_cash_flow(
            flow_date="2026-08-05", entry_type="期初资金", amount=20_000
        )
        for symbol in ("159501", "510500"):
            add_live_trade(
                trade_date="2026-08-05",
                symbol=symbol,
                name=symbol,
                side="买入",
                price=10,
                quantity=100,
                fee_rate_pct=0,
            )
        histories = {
            symbol: pd.DataFrame(
                {"date": pd.to_datetime(["2026-08-05"]), "price": [10.0]}
            )
            for symbol in ("159501", "510500")
        }
        snapshot = build_live_account_snapshot(
            list_live_trades(),
            list_live_cash_flows(),
            histories,
            quotes={
                "159501": {
                    "price": 12.0,
                    "quote_time": datetime(2026, 8, 6, 10, 0),
                }
            },
            market_now=datetime(2026, 8, 6, 10, 1),
            formal_target_date="2026-08-05",
        )
        positions = snapshot["positions"].set_index("symbol")
        self.assertAlmostEqual(positions.loc["159501", "market_value"], 1200.0)
        self.assertAlmostEqual(positions.loc["510500", "market_value"], 1000.0)
        self.assertEqual(positions.loc["159501", "price_status"], "实时")
        self.assertEqual(positions.loc["510500", "price_status"], "正式收盘")
        self.assertAlmostEqual(snapshot["summary"]["market_value"], 2200.0)
        self.assertAlmostEqual(snapshot["summary"]["total_assets"], 20200.0)
        self.assertEqual(snapshot["incomplete_realtime_symbols"], ["510500"])

    def test_same_day_buy_uses_quote_and_fee_in_transient_account_pnl(self):
        add_live_cash_flow(
            flow_date="2026-08-06", entry_type="期初资金", amount=10_000
        )
        add_live_trade(
            trade_date="2026-08-06",
            trade_time="10:00:00",
            symbol="159501",
            name="纳指ETF嘉实",
            side="买入",
            price=10,
            quantity=100,
            fee_rate_pct=1,
        )
        snapshot = build_live_account_snapshot(
            list_live_trades(),
            list_live_cash_flows(),
            {
                "159501": pd.DataFrame(
                    {"date": pd.to_datetime(["2026-08-05"]), "price": [9.0]}
                )
            },
            quotes={
                "159501": {
                    "price": 11.0,
                    "quote_time": datetime(2026, 8, 6, 10, 5),
                }
            },
            market_now=datetime(2026, 8, 6, 10, 6),
            formal_target_date="2026-08-05",
        )
        self.assertAlmostEqual(snapshot["summary"]["cash"], 8990.0)
        self.assertAlmostEqual(snapshot["summary"]["market_value"], 1100.0)
        self.assertAlmostEqual(snapshot["summary"]["total_assets"], 10090.0)
        self.assertAlmostEqual(snapshot["summary"]["daily_pnl"], 90.0)

    def test_cash_flow_requires_opening_and_can_be_deleted(self):
        with self.assertRaisesRegex(ValueError, "先录入期初资金"):
            add_live_cash_flow(
                flow_date="2026-08-05", entry_type="资金转入", amount=1_000
            )
        flow_id = add_live_cash_flow(
            flow_date="2026-08-05", entry_type="期初资金", amount=1_000
        )
        self.assertTrue(delete_live_cash_flow(flow_id))
        self.assertTrue(list_live_cash_flows().empty)

    def test_close_refresh_backfills_weekend_and_rechecks_when_target_changes(self):
        saturday = datetime(2026, 8, 15, 10, 0)

        self.assertTrue(
            live_close_refresh_due(
                target_date="2026-08-14",
                market_now=saturday,
            )
        )
        self.assertFalse(
            live_close_refresh_due(
                target_date="2026-08-14",
                market_now=saturday,
                last_attempt="2026-08-15 09:55:00",
                last_target_date="2026-08-14",
            )
        )
        monday_after_close = datetime(2026, 8, 17, 15, 6)
        self.assertTrue(
            live_close_refresh_due(
                target_date="2026-08-17",
                market_now=monday_after_close,
                last_attempt="2026-08-17 15:04:00",
                last_target_date="2026-08-14",
            )
        )

    def test_close_refresh_rechecks_immediately_when_symbol_scope_changes(self):
        market_now = datetime(2026, 8, 14, 14, 0)

        self.assertFalse(
            live_close_refresh_due(
                target_date="2026-08-13",
                market_now=market_now,
                last_attempt="2026-08-14 13:55:00",
                last_target_date="2026-08-13",
                refresh_scope="159967",
                last_refresh_scope="159967",
            )
        )
        self.assertTrue(
            live_close_refresh_due(
                target_date="2026-08-13",
                market_now=market_now,
                last_attempt="2026-08-14 13:55:00",
                last_target_date="2026-08-13",
                refresh_scope="159967|513310",
                last_refresh_scope="159967",
            )
        )

    def test_seed_key_is_idempotent_and_record_can_be_deleted(self):
        kwargs = {
            "trade_date": "2026-08-05",
            "symbol": "510500",
            "name": "中证500ETF南方",
            "side": "买入",
            "price": 7.812,
            "quantity": 6400,
            "fee_rate_pct": 0.6,
            "record_key": "initial-510500",
        }
        first_id, first_created = add_live_trade(**kwargs)
        second_id, second_created = add_live_trade(**kwargs)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_id, second_id)
        self.assertEqual(len(list_live_trades()), 1)
        self.assertTrue(delete_live_trade(first_id))
        self.assertTrue(list_live_trades().empty)

    def test_seed_key_rejects_different_trade_content(self):
        kwargs = {
            "trade_date": "2026-08-05",
            "symbol": "510500",
            "name": "中证500ETF南方",
            "side": "买入",
            "price": 7.812,
            "quantity": 6400,
            "fee_rate_pct": 0.6,
            "record_key": "initial-510500",
        }
        add_live_trade(**kwargs)
        kwargs["price"] = 7.9

        with self.assertRaisesRegex(ValueError, "另一笔不同"):
            add_live_trade(**kwargs)

    def test_sell_cannot_exceed_recorded_position(self):
        db.init_db()
        with self.assertRaisesRegex(ValueError, "最多可卖出 0 份"):
            add_live_trade(
                trade_date="2026-08-05",
                symbol="518850",
                name="黄金ETF华夏",
                side="卖出",
                price=8.722,
                quantity=4700,
                fee_rate_pct=0.6,
            )

    def test_sell_uses_average_cost_and_cannot_precede_the_buy(self):
        add_live_trade(
            trade_date="2026-08-05",
            symbol="159501",
            name="纳指ETF嘉实",
            side="买入",
            price=10,
            quantity=100,
            fee_rate_pct=1,
        )
        add_live_trade(
            trade_date="2026-08-06",
            symbol="159501",
            name="纳指ETF嘉实",
            side="买入",
            price=12,
            quantity=100,
            fee_rate_pct=1,
        )
        sell_id, _ = add_live_trade(
            trade_date="2026-08-07",
            symbol="159501",
            name="纳指ETF嘉实",
            side="卖出",
            price=15,
            quantity=150,
            fee_rate_pct=1,
        )

        positions = build_live_positions(list_live_trades())
        self.assertEqual(positions.iloc[0]["quantity"], 50)
        self.assertAlmostEqual(positions.iloc[0]["average_cost"], 11.11)
        self.assertAlmostEqual(positions.iloc[0]["cost_basis"], 555.5)
        self.assertAlmostEqual(positions.iloc[0]["realized_pnl"], 561.0)

        with self.assertRaisesRegex(ValueError, "最多可卖出 0 份"):
            add_live_trade(
                trade_date="2026-08-04",
                symbol="159501",
                name="纳指ETF嘉实",
                side="卖出",
                price=9,
                quantity=1,
                fee_rate_pct=1,
            )

        buy_id = int(list_live_trades().sort_values("id").iloc[0]["id"])
        with self.assertRaisesRegex(ValueError, "不能删除"):
            delete_live_trade(buy_id)
        self.assertIn(sell_id, list_live_trades()["id"].tolist())

    def test_daily_pnl_uses_formal_close_and_buy_fee_in_cost(self):
        add_live_trade(
            trade_date="2026-08-05",
            symbol="159501",
            name="纳指ETF嘉实",
            side="买入",
            price=10,
            quantity=100,
            fee_rate_pct=1,
        )
        history = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-05", "2026-08-06"]),
                "price": [10.0, 12.0],
            }
        )

        daily = build_live_daily_pnl(list_live_trades(), {"159501": history})

        self.assertEqual(daily["date"].tolist(), list(pd.to_datetime(["2026-08-05", "2026-08-06"])))
        self.assertAlmostEqual(daily.iloc[0]["market_value"], 1000.0)
        self.assertAlmostEqual(daily.iloc[0]["cost_basis"], 1010.0)
        self.assertAlmostEqual(daily.iloc[0]["total_pnl"], -10.0)
        self.assertAlmostEqual(daily.iloc[1]["unrealized_pnl"], 190.0)
        self.assertAlmostEqual(daily.iloc[1]["return_pct"], 190 / 1010 * 100)

    def test_daily_pnl_keeps_realized_profit_after_partial_sell(self):
        add_live_trade(
            trade_date="2026-08-05",
            symbol="159501",
            name="纳指ETF嘉实",
            side="买入",
            price=10,
            quantity=100,
            fee_rate_pct=1,
        )
        add_live_trade(
            trade_date="2026-08-06",
            symbol="159501",
            name="纳指ETF嘉实",
            side="卖出",
            price=15,
            quantity=50,
            fee_rate_pct=1,
        )
        history = pd.DataFrame(
            {
                "日期": pd.to_datetime(["2026-08-05", "2026-08-06"]),
                "收盘价": [10.0, 15.0],
            }
        )

        daily = build_live_daily_pnl(list_live_trades(), {"159501": history})
        latest = daily.iloc[-1]

        self.assertAlmostEqual(latest["cost_basis"], 505.0)
        self.assertAlmostEqual(latest["market_value"], 750.0)
        self.assertAlmostEqual(latest["realized_pnl"], 237.5)
        self.assertAlmostEqual(latest["unrealized_pnl"], 245.0)
        self.assertAlmostEqual(latest["total_pnl"], 482.5)
        self.assertAlmostEqual(latest["net_investment"], 267.5)

    def test_daily_pnl_stops_before_a_held_symbol_missing_latest_close(self):
        for symbol in ("159501", "510500"):
            add_live_trade(
                trade_date="2026-08-05",
                symbol=symbol,
                name=symbol,
                side="买入",
                price=10,
                quantity=100,
                fee_rate_pct=0,
            )
        histories = {
            "159501": pd.DataFrame(
                {
                    "date": pd.to_datetime(["2026-08-05", "2026-08-06"]),
                    "price": [10.0, 11.0],
                }
            ),
            "510500": pd.DataFrame(
                {"date": pd.to_datetime(["2026-08-05"]), "price": [10.0]}
            ),
        }

        daily = build_live_daily_pnl(list_live_trades(), histories)

        self.assertEqual(daily["date"].tolist(), [pd.Timestamp("2026-08-05")])

    def test_daily_pnl_stops_at_first_internal_formal_gap(self):
        for symbol in ("159501", "510500"):
            add_live_trade(
                trade_date="2026-08-05",
                symbol=symbol,
                name=symbol,
                side="买入",
                price=10,
                quantity=100,
                fee_rate_pct=0,
            )
        histories = {
            "159501": pd.DataFrame(
                {
                    "date": pd.to_datetime(["2026-08-05", "2026-08-06", "2026-08-07"]),
                    "price": [10.0, 11.0, 12.0],
                }
            ),
            "510500": pd.DataFrame(
                {
                    "date": pd.to_datetime(["2026-08-05", "2026-08-07"]),
                    "price": [10.0, 12.0],
                }
            ),
        }
        daily = build_live_daily_pnl(list_live_trades(), histories)
        self.assertEqual(daily["date"].tolist(), [pd.Timestamp("2026-08-05")])

    def test_daily_returns_include_first_day_fee_and_new_investment(self):
        daily_pnl = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-05", "2026-08-06", "2026-08-07"]),
                "market_value": [990.0, 2178.0, 2400.0],
                "total_pnl": [-10.0, 168.0, 390.0],
                "cumulative_buy_cost": [1010.0, 2222.0, 2222.0],
                "net_investment": [1010.0, 2222.0, 2222.0],
            }
        )

        result = build_live_daily_returns(daily_pnl)

        self.assertAlmostEqual(result.iloc[0]["pnl_amount"], -10.0)
        self.assertAlmostEqual(result.iloc[0]["return_base"], 1010.0)
        self.assertAlmostEqual(result.iloc[0]["return_pct"], -10 / 1010 * 100)
        self.assertAlmostEqual(result.iloc[1]["pnl_amount"], 178.0)
        self.assertAlmostEqual(result.iloc[1]["return_base"], 990.0 + 1212.0)
        self.assertAlmostEqual(result.iloc[1]["return_pct"], 178 / 2202 * 100)
        self.assertAlmostEqual(result.iloc[2]["pnl_amount"], 222.0)
        self.assertAlmostEqual(result.iloc[2]["return_base"], 2178.0)

    def test_daily_returns_do_not_double_count_same_day_rotation(self):
        daily_pnl = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-05", "2026-08-06"]),
                "market_value": [1000.0, 1100.0],
                "total_pnl": [0.0, 100.0],
                "cumulative_buy_cost": [1000.0, 2000.0],
                "net_investment": [1000.0, 1000.0],
            }
        )

        result = build_live_daily_returns(daily_pnl)

        self.assertAlmostEqual(result.iloc[1]["daily_buy_cost"], 1000.0)
        self.assertAlmostEqual(result.iloc[1]["daily_sell_proceeds"], 1000.0)
        self.assertAlmostEqual(result.iloc[1]["return_base"], 1000.0)
        self.assertAlmostEqual(result.iloc[1]["return_pct"], 10.0)

    def test_daily_returns_keep_previous_market_value_as_base_when_reducing(self):
        daily_pnl = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-05", "2026-08-06"]),
                "market_value": [1000.0, 600.0],
                "total_pnl": [0.0, 100.0],
                "cumulative_buy_cost": [1000.0, 1000.0],
                "net_investment": [1000.0, 500.0],
            }
        )

        result = build_live_daily_returns(daily_pnl)

        self.assertAlmostEqual(result.iloc[1]["daily_sell_proceeds"], 500.0)
        self.assertAlmostEqual(result.iloc[1]["return_base"], 1000.0)
        self.assertAlmostEqual(result.iloc[1]["return_pct"], 10.0)

    def test_daily_returns_use_new_buy_as_base_after_empty_position(self):
        daily_pnl = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-05", "2026-08-06"]),
                "market_value": [0.0, 1010.0],
                "total_pnl": [100.0, 110.0],
                "cumulative_buy_cost": [1000.0, 2000.0],
                "net_investment": [-100.0, 900.0],
            }
        )

        result = build_live_daily_returns(daily_pnl)

        self.assertAlmostEqual(result.iloc[1]["daily_buy_cost"], 1000.0)
        self.assertAlmostEqual(result.iloc[1]["return_base"], 1000.0)
        self.assertAlmostEqual(result.iloc[1]["return_pct"], 1.0)

    def test_period_returns_sum_amounts_and_compound_rates_across_boundaries(self):
        daily_returns = pd.DataFrame(
            {
                "date": pd.to_datetime(["2025-12-29", "2025-12-31", "2026-01-02"]),
                "pnl_amount": [100.0, 50.0, -25.0],
                "return_base": [1000.0, 1100.0, 1150.0],
                "return_pct": [10.0, 5.0, -2.0],
                "daily_buy_cost": [1000.0, 0.0, 0.0],
                "daily_sell_proceeds": [0.0, 0.0, 0.0],
            }
        )

        weekly = build_live_period_returns(daily_returns, period="week")
        monthly = build_live_period_returns(daily_returns, period="month")
        yearly = build_live_period_returns(daily_returns, period="year")

        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly.iloc[0]["period_start"], pd.Timestamp("2025-12-29"))
        self.assertEqual(weekly.iloc[0]["period_end"], pd.Timestamp("2026-01-02"))
        self.assertAlmostEqual(weekly.iloc[0]["pnl_amount"], 125.0)
        self.assertAlmostEqual(
            weekly.iloc[0]["return_pct"],
            ((1.10 * 1.05 * 0.98) - 1) * 100,
        )
        self.assertEqual(monthly["pnl_amount"].tolist(), [150.0, -25.0])
        self.assertEqual(yearly["pnl_amount"].tolist(), [150.0, -25.0])

    def test_period_returns_exclude_weekends_and_market_holidays(self):
        daily_returns = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-06-19", "2026-06-20", "2026-06-22"]),
                "pnl_amount": [100.0, 200.0, 300.0],
                "return_pct": [1.0, 2.0, 3.0],
            }
        )

        daily = build_live_period_returns(
            daily_returns,
            period="day",
            excluded_dates={date(2026, 6, 19)},
        )
        monthly = build_live_period_returns(
            daily_returns,
            period="month",
            excluded_dates={date(2026, 6, 19)},
        )

        self.assertEqual(daily["period_start"].tolist(), [pd.Timestamp("2026-06-22")])
        self.assertEqual(monthly.iloc[0]["pnl_amount"], 300.0)
        self.assertAlmostEqual(monthly.iloc[0]["return_pct"], 3.0)

    def test_month_grid_contains_only_weekdays_and_keeps_cross_month_weekdays(self):
        june = build_live_return_month_grid(2026, 6)
        august = build_live_return_month_grid(2026, 8)

        self.assertTrue(all(len(week) == 5 for week in june + august))
        self.assertTrue(all(day.weekday() < 5 for week in june + august for day in week))
        self.assertEqual(june[-1], [
            date(2026, 6, 29),
            date(2026, 6, 30),
            date(2026, 7, 1),
            date(2026, 7, 2),
            date(2026, 7, 3),
        ])
        self.assertEqual(august[0][0], date(2026, 8, 3))

    def test_period_returns_keep_unknown_rate_unavailable(self):
        daily_returns = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-05", "2026-08-06"]),
                "pnl_amount": [0.0, 10.0],
                "return_base": [0.0, 0.0],
                "return_pct": [0.0, pd.NA],
                "daily_buy_cost": [0.0, 0.0],
                "daily_sell_proceeds": [0.0, 0.0],
            }
        )

        monthly = build_live_period_returns(daily_returns, period="month")

        self.assertEqual(monthly.iloc[0]["pnl_amount"], 10.0)
        self.assertTrue(pd.isna(monthly.iloc[0]["return_pct"]))

    def test_position_performance_reports_daily_and_cumulative_pnl(self):
        add_live_trade(
            trade_date="2026-08-05",
            symbol="159501",
            name="纳指ETF嘉实",
            side="买入",
            price=10,
            quantity=100,
            fee_rate_pct=1,
        )
        history = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-05", "2026-08-06"]),
                "price": [10.0, 12.0],
            }
        )

        result = build_live_position_performance(
            list_live_trades(),
            {"159501": history},
        )
        row = result.iloc[0]

        self.assertEqual(row["valuation_date"], pd.Timestamp("2026-08-06"))
        self.assertAlmostEqual(row["latest_price"], 12.0)
        self.assertAlmostEqual(row["market_value"], 1200.0)
        self.assertAlmostEqual(row["daily_pnl"], 200.0)
        self.assertAlmostEqual(row["daily_return_pct"], 20.0)
        self.assertAlmostEqual(row["daily_return_base"], 1000.0)
        self.assertAlmostEqual(row["cumulative_pnl"], 190.0)
        self.assertAlmostEqual(row["cumulative_return_pct"], 190 / 1010 * 100)
        self.assertAlmostEqual(row["cumulative_buy_cost"], 1010.0)

    def test_position_performance_first_day_includes_buy_fee(self):
        add_live_trade(
            trade_date="2026-08-05",
            symbol="159501",
            name="纳指ETF嘉实",
            side="买入",
            price=10,
            quantity=100,
            fee_rate_pct=1,
        )
        history = pd.DataFrame(
            {"date": pd.to_datetime(["2026-08-05"]), "price": [10.0]}
        )

        row = build_live_position_performance(
            list_live_trades(),
            {"159501": history},
        ).iloc[0]

        self.assertAlmostEqual(row["daily_pnl"], -10.0)
        self.assertAlmostEqual(row["daily_return_pct"], -10 / 1010 * 100)
        self.assertAlmostEqual(row["cumulative_pnl"], -10.0)
        self.assertAlmostEqual(row["cumulative_return_pct"], -10 / 1010 * 100)

    def test_position_performance_keeps_pnl_blank_when_close_precedes_trade(self):
        add_live_trade(
            trade_date="2026-08-06",
            symbol="159501",
            name="纳指ETF嘉实",
            side="买入",
            price=10,
            quantity=100,
            fee_rate_pct=1,
        )
        stale_history = pd.DataFrame(
            {"date": pd.to_datetime(["2026-08-05"]), "price": [10.0]}
        )

        row = build_live_position_performance(
            list_live_trades(),
            {"159501": stale_history},
        ).iloc[0]

        self.assertTrue(pd.isna(row["daily_pnl"]))
        self.assertTrue(pd.isna(row["latest_price"]))
        self.assertTrue(pd.isna(row["market_value"]))
        self.assertTrue(pd.isna(row["daily_return_pct"]))
        self.assertTrue(pd.isna(row["daily_return_base"]))
        self.assertTrue(pd.isna(row["cumulative_pnl"]))
        self.assertTrue(pd.isna(row["cumulative_return_pct"]))
        self.assertTrue(pd.isna(row["cumulative_buy_cost"]))

    def test_position_performance_total_recalculates_portfolio_rates(self):
        positions = pd.DataFrame(
            {
                "market_value": [1200.0, 1800.0],
                "daily_pnl": [200.0, -100.0],
                "daily_return_base": [1000.0, 2000.0],
                "cumulative_pnl": [190.0, 300.0],
                "cumulative_buy_cost": [1010.0, 2100.0],
                "realized_pnl": [50.0, 25.0],
                "fee_amount": [10.0, 20.0],
            }
        )

        result = summarize_live_position_performance(positions)

        self.assertAlmostEqual(result["market_value"], 3000.0)
        self.assertAlmostEqual(result["daily_pnl"], 100.0)
        self.assertAlmostEqual(result["daily_return_pct"], 100 / 3000 * 100)
        self.assertAlmostEqual(result["cumulative_pnl"], 490.0)
        self.assertAlmostEqual(result["cumulative_return_pct"], 490 / 3110 * 100)
        self.assertAlmostEqual(result["realized_pnl"], 75.0)
        self.assertAlmostEqual(result["fee_amount"], 30.0)

    def test_symbol_pnl_history_keeps_open_and_fully_closed_symbols(self):
        add_live_trade(
            trade_date="2026-08-05",
            symbol="159501",
            name="纳指ETF嘉实",
            side="买入",
            price=10,
            quantity=100,
            fee_rate_pct=1,
        )
        add_live_trade(
            trade_date="2026-08-06",
            symbol="159501",
            name="纳指ETF嘉实",
            side="卖出",
            price=12,
            quantity=100,
            fee_rate_pct=1,
        )
        add_live_trade(
            trade_date="2026-08-05",
            symbol="510500",
            name="中证500ETF南方",
            side="买入",
            price=20,
            quantity=100,
            fee_rate_pct=1,
        )
        histories = {
            "510500": pd.DataFrame(
                {
                    "date": pd.to_datetime(["2026-08-05", "2026-08-06"]),
                    "price": [20.0, 22.0],
                }
            )
        }

        result = build_live_symbol_pnl_history(list_live_trades(), histories).set_index(
            "symbol"
        )
        closed = result.loc["159501"]
        opened = result.loc["510500"]

        self.assertEqual(closed["status"], "已清仓")
        self.assertEqual(closed["quantity"], 0)
        self.assertAlmostEqual(closed["cumulative_buy_cost"], 1010.0)
        self.assertAlmostEqual(closed["cumulative_sell_proceeds"], 1188.0)
        self.assertAlmostEqual(closed["realized_pnl"], 178.0)
        self.assertAlmostEqual(closed["unrealized_pnl"], 0.0)
        self.assertAlmostEqual(closed["total_pnl"], 178.0)
        self.assertAlmostEqual(closed["return_pct"], 178 / 1010 * 100)

        self.assertEqual(opened["status"], "持仓")
        self.assertEqual(opened["quantity"], 100)
        self.assertAlmostEqual(opened["market_value"], 2200.0)
        self.assertAlmostEqual(opened["realized_pnl"], 0.0)
        self.assertAlmostEqual(opened["unrealized_pnl"], 180.0)
        self.assertAlmostEqual(opened["total_pnl"], 180.0)
        self.assertAlmostEqual(opened["return_pct"], 180 / 2020 * 100)

        with_total = append_live_symbol_pnl_total(result.reset_index())
        total = with_total.iloc[-1]
        self.assertEqual(total["name"], "合计")
        self.assertTrue(pd.isna(total["quantity"]))
        self.assertAlmostEqual(total["cumulative_buy_cost"], 3030.0)
        self.assertAlmostEqual(total["cumulative_sell_proceeds"], 1188.0)
        self.assertAlmostEqual(total["market_value"], 2200.0)
        self.assertAlmostEqual(total["realized_pnl"], 178.0)
        self.assertAlmostEqual(total["unrealized_pnl"], 180.0)
        self.assertAlmostEqual(total["total_pnl"], 358.0)
        self.assertAlmostEqual(total["return_pct"], 358 / 3030 * 100)
        self.assertAlmostEqual(total["fee_amount"], 42.0)
        self.assertTrue(pd.isna(total["first_trade_date"]))
        self.assertTrue(pd.isna(total["last_trade_date"]))
        self.assertTrue(pd.isna(total["valuation_date"]))

    def test_live_page_renders_close_based_pnl_curve(self):
        root = Path(__file__).parents[1]
        page_source = (root / "pages" / "6_实盘记录.py").read_text(encoding="utf-8")
        dashboard_source = (root / "components" / "live_record" / "dashboard.py").read_text(
            encoding="utf-8"
        )
        tables_source = (root / "components" / "live_record" / "tables.py").read_text(
            encoding="utf-8"
        )
        shared_table_source = (root / "components" / "position_table.py").read_text(
            encoding="utf-8"
        )
        history_source = (root / "components" / "live_record" / "history.py").read_text(
            encoding="utf-8"
        )
        account_source = (root / "components" / "live_record" / "account.py").read_text(
            encoding="utf-8"
        )

        self.assertLessEqual(len(page_source.splitlines()), 250)
        self.assertEqual(page_source.count('@st.fragment(run_every="120s")'), 2)
        self.assertIn("_render_live_account_dashboard(", page_source)
        self.assertNotIn('st.button("启用实时行情"', page_source)
        self.assertNotIn('st.expander("实时行情"', page_source)
        self.assertIn("_render_live_cash_flow_form(", page_source)
        self.assertLess(
            page_source.rindex("render_daily_close_pnl()"),
            page_source.rindex("_render_live_trade_form("),
        )

        self.assertIn('st.subheader("每日正式收盘盈亏")', dashboard_source)
        self.assertIn('["账户口径", "持仓口径"]', dashboard_source)
        self.assertIn("adjust=FUND_ADJUST_NONE", dashboard_source)
        self.assertIn("build_live_account_snapshot(", dashboard_source)
        self.assertIn("refresh_runtime_etf_quotes(", dashboard_source)
        self.assertIn("load_runtime_etf_quotes()", dashboard_source)
        self.assertIn("filter_current_etf_realtime_quotes(", dashboard_source)
        self.assertIn("quotes=shared_quotes", dashboard_source)
        self.assertNotIn("realtime_enabled", dashboard_source)
        self.assertIn('name="每日盈亏"', dashboard_source)
        self.assertIn('name="持仓当日盈亏"', dashboard_source)
        self.assertIn('name="账户净值"', dashboard_source)
        self.assertIn('name="持仓净值"', dashboard_source)
        self.assertIn('type="category"', dashboard_source)
        self.assertIn("categoryarray=chart_dates.tolist()", dashboard_source)
        self.assertIn("周末和节假日已自动跳过", dashboard_source)
        self.assertIn("render_return_calendar(", dashboard_source)
        self.assertIn("临时混合估值", account_source)
        self.assertIn("持仓分析", account_source)
        self.assertIn("不影响", account_source)
        self.assertIn("下方每日正式收盘盈亏", account_source)
        self.assertNotIn("继续显示最近一次完整估值", account_source)

        self.assertIn('"行情状态",', tables_source)
        self.assertIn("position_number_cell(row.average_cost, digits=3)", tables_source)
        self.assertIn(
            "position_pnl_cell(row.daily_pnl, row.daily_return_pct)",
            tables_source,
        )
        self.assertIn("float(market_value) / base_assets * 100", tables_source)
        self.assertIn("total_weight_pct", tables_source)
        self.assertIn(
            'position_text_cell("合计", class_name="position-total-label")',
            tables_source,
        )
        self.assertIn(
            "render_position_table(headers, rows, total_cells=total_cells",
            tables_source,
        )
        self.assertIn("live-symbol-history-total", tables_source)
        self.assertIn("text-align: center", shared_table_source)
        self.assertIn("color: rgb(190, 18, 60)", shared_table_source)
        self.assertIn("color: rgb(22, 101, 52)", shared_table_source)
        self.assertNotIn("¥", tables_source + shared_table_source)

        self.assertIn('st.subheader("历史盈亏")', history_source)
        self.assertIn("build_history(all_trades, price_histories)", history_source)
        self.assertIn("append_total(build_history", history_source)
        self.assertIn("render_history_table(history_display)", history_source)
        self.assertIn("包含当前持仓和已清仓标的", history_source)

        # 被后一个同名函数覆盖的旧收益日历实现已经删除，继续复用共享日历。
        self.assertNotIn("def _live_return_tile", page_source + dashboard_source)


if __name__ == "__main__":
    unittest.main()
