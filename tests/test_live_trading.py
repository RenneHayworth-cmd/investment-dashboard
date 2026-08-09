import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from services.live_trading import (
    add_live_trade,
    build_live_positions,
    delete_live_trade,
    list_live_trades,
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


if __name__ == "__main__":
    unittest.main()
