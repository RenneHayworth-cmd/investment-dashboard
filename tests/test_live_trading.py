import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core import db
from services.live_trading import (
    add_live_trade,
    append_live_symbol_pnl_total,
    build_live_daily_pnl,
    build_live_position_performance,
    build_live_positions,
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
        page_source = (Path(__file__).parents[1] / "pages" / "6_实盘记录.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('@st.fragment(run_every="120s")', page_source)
        self.assertIn('st.subheader("每日收盘盈亏")', page_source)
        self.assertIn('st.subheader(f"每日收盘盈亏（{valuation_date}）")', page_source)
        self.assertNotIn('("估值日期",', page_source)
        self.assertIn("build_live_daily_pnl(current_trades, price_histories)", page_source)
        self.assertIn(
            "build_live_position_performance(\n        current_trades,",
            page_source,
        )
        self.assertIn("adjust=None", page_source)
        self.assertIn("network_refresh_due", page_source)
        self.assertIn('name="总盈亏"', page_source)
        self.assertIn('name="累计收益率"', page_source)
        self.assertIn('"现价",', page_source)
        self.assertIn('"市值",', page_source)
        self.assertIn('"成本",', page_source)
        self.assertIn('"当日盈亏",', page_source)
        self.assertIn('"累计盈亏",', page_source)
        self.assertIn('"累计盈亏",\n        "仓位",', page_source)
        self.assertIn(
            '"代码",\n        "市值",\n        "现价",\n        "持仓数量",',
            page_source,
        )
        self.assertIn("format_live_number(row.average_cost, 3)", page_source)
        self.assertIn("pnl_cell(row.daily_pnl, row.daily_return_pct)", page_source)
        self.assertIn(
            "pnl_cell(row.cumulative_pnl, row.cumulative_return_pct)",
            page_source,
        )
        self.assertIn("render_live_positions_table(position_performance)", page_source)
        self.assertIn("float(market_value) / float(total_market_value) * 100", page_source)
        self.assertIn('"<td>100.00%</td>"', page_source)
        self.assertIn('live-total-label">合计</td>', page_source)
        self.assertNotIn("¥", page_source)
        self.assertIn("收盘价更新失败，当前继续使用本地缓存", page_source)
        self.assertIn("页面保持打开时将在10分钟后重试", page_source)
        self.assertIn('failure_state_key = "live_pnl_close_failures"', page_source)
        self.assertIn("st.session_state.pop(failure_state_key, None)", page_source)
        self.assertIn('st.subheader("历史盈亏")', page_source)
        self.assertIn("build_live_symbol_pnl_history(all_trades, price_histories)", page_source)
        self.assertIn("append_live_symbol_pnl_total(", page_source)
        self.assertIn("render_live_symbol_history_table(history_display)", page_source)
        self.assertIn("text-align: center", page_source)
        self.assertIn("live-symbol-history-total", page_source)
        self.assertIn("background: rgba(49, 51, 63, 0.035)", page_source)
        self.assertIn("color: rgb(190, 18, 60)", page_source)
        self.assertIn("color: rgb(22, 101, 52)", page_source)
        self.assertIn("包含当前持仓和已清仓标的", page_source)
        self.assertGreater(
            page_source.rindex("render_live_symbol_pnl_history()"),
            page_source.index('st.subheader("成交明细")'),
        )
        warning_position = page_source.index("if update_failures:")
        self.assertLess(
            page_source.index("收盘价更新失败", warning_position),
            page_source.index(
                'st.subheader("当前实盘持仓")', warning_position
            ),
        )


if __name__ == "__main__":
    unittest.main()
