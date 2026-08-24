import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core import db
from services.live_trading import (
    add_live_trade,
    append_live_symbol_pnl_total,
    build_live_daily_pnl,
    build_live_daily_returns,
    build_live_period_returns,
    build_live_position_performance,
    build_live_positions,
    build_live_return_month_grid,
    build_live_symbol_pnl_history,
    delete_live_trade,
    live_close_refresh_due,
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
        valuation_source = (root / "components" / "live_record" / "valuation.py").read_text(
            encoding="utf-8"
        )
        tables_source = (root / "components" / "live_record" / "tables.py").read_text(
            encoding="utf-8"
        )
        history_source = (root / "components" / "live_record" / "history.py").read_text(
            encoding="utf-8"
        )

        self.assertLessEqual(len(page_source.splitlines()), 250)
        self.assertEqual(page_source.count('@st.fragment(run_every="120s")'), 2)
        self.assertIn("list_trades=list_live_trades", page_source)
        self.assertIn("load_etf=load_or_fetch_etf", page_source)
        self.assertLess(
            page_source.rindex("render_daily_close_pnl()"),
            page_source.rindex("_render_live_trade_form("),
        )
        self.assertLess(
            page_source.rindex("_render_live_trade_details("),
            page_source.rindex("render_live_symbol_pnl_history()"),
        )

        self.assertIn('st.subheader("每日收盘盈亏")', valuation_source)
        self.assertIn('st.subheader(f"每日收盘盈亏（{valuation_date}）")', valuation_source)
        self.assertIn("build_daily_pnl(current_trades, price_histories)", valuation_source)
        self.assertIn("build_position_performance(\n        current_trades,", valuation_source)
        self.assertIn("adjust=adjustment", valuation_source)
        self.assertIn("adjustment=FUND_ADJUST_NONE", page_source)
        self.assertIn('attempt_scope_key = "live_pnl_close_last_scope"', valuation_source)
        self.assertIn("refresh_scope=refresh_scope", valuation_source)
        self.assertIn("页面将在下次自动检查时联网补齐", valuation_source)
        self.assertIn('name="总盈亏"', valuation_source)
        self.assertIn('name="累计收益率"', valuation_source)
        self.assertIn("build_daily_returns(daily_pnl)", valuation_source)
        self.assertIn("不包含账户未投资现金", valuation_source)
        self.assertIn("render_calendar(daily_pnl, first_trade_date=first_trade_date)", valuation_source)
        self.assertLess(
            valuation_source.index(
                "render_calendar(daily_pnl, first_trade_date=first_trade_date)"
            ),
            valuation_source.index('figure = make_subplots(specs=[[{"secondary_y": True}]])'),
        )
        self.assertIn("收盘价更新失败，当前继续使用本地缓存", valuation_source)
        self.assertIn("页面保持打开时将在10分钟后重试", valuation_source)
        self.assertIn('failure_state_key = "live_pnl_close_failures"', valuation_source)
        self.assertIn("st.session_state.pop(failure_state_key, None)", valuation_source)
        warning_position = valuation_source.index("if update_failures:")
        self.assertLess(
            valuation_source.index("收盘价更新失败", warning_position),
            valuation_source.index(
                'st.subheader("当前实盘持仓")', warning_position
            ),
        )

        self.assertIn('"累计盈亏",\n        "仓位",', tables_source)
        self.assertIn("format_live_number(row.average_cost, 3)", tables_source)
        self.assertIn("pnl_cell(row.daily_pnl, row.daily_return_pct)", tables_source)
        self.assertIn("float(market_value) / float(total_market_value) * 100", tables_source)
        self.assertIn('"<td>100.00%</td>"', tables_source)
        self.assertIn('live-total-label">合计</td>', tables_source)
        self.assertIn("live-symbol-history-total", tables_source)
        self.assertIn("text-align: center", tables_source)
        self.assertIn("color: rgb(190, 18, 60)", tables_source)
        self.assertIn("color: rgb(22, 101, 52)", tables_source)
        self.assertNotIn("¥", tables_source)

        self.assertIn('st.subheader("历史盈亏")', history_source)
        self.assertIn("build_history(all_trades, price_histories)", history_source)
        self.assertIn("append_total(build_history", history_source)
        self.assertIn("render_history_table(history_display)", history_source)
        self.assertIn("包含当前持仓和已清仓标的", history_source)

        # 被后一个同名函数覆盖的旧收益日历实现已经删除，继续复用共享日历。
        self.assertNotIn("def _live_return_tile", page_source + valuation_source)
        self.assertNotIn("build_live_period_returns", page_source + valuation_source)


if __name__ == "__main__":
    unittest.main()
