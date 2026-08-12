from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from core.db import get_conn, init_db
from services.futures_live_trading import (
    _infer_previous_settlement,
    add_manual_trade,
    build_contract_pnl_history,
    build_current_position_pnl,
    build_daily_account_pnl,
    build_estimated_positions,
    delete_manual_trade,
    list_futures_live_trades,
    list_monthly_accounts,
    list_statement_imports,
    parse_statement,
    summarize_futures_live_pnl,
    sync_statements,
)


def _padded(rows: list[list[object]], width: int = 13) -> pd.DataFrame:
    return pd.DataFrame([row + [None] * (width - len(row)) for row in rows])


def write_statement(
    path: Path,
    *,
    month: str,
    workbook_month: str | None = None,
    customer_equity: float = 10000,
    monthly_pnl: float = 0,
    monthly_fee: float = 3,
    declaration_fees: list[tuple[str, float]] | None = None,
    floating_pnl: float = 100,
    margin: float = 2200,
    available: float = 7800,
    futures_positions: list[list[object]] | None = None,
    option_positions: list[list[object]] | None = None,
    futures_trades: list[list[object]] | None = None,
    option_trades: list[list[object]] | None = None,
) -> None:
    futures_positions = futures_positions if futures_positions is not None else [
        ["I2609", 2, 100, None, None, 104, 105, 100, 1200, "投机"]
    ]
    option_positions = option_positions if option_positions is not None else [
        [f"{month}-28", "I2609-P-90", "I2609", "看跌期权", 90, None, None, 1, 5, 4.5, 4, 1000, "T1"]
    ]
    futures_trades = futures_trades if futures_trades is not None else [
        [f"{month}-10", "I2609", "F1", "09:31:00", "买", "投机", 100, 2, 2000, "开", 2, "--", f"{month}-10"]
    ]
    option_trades = option_trades if option_trades is not None else [
        [f"{month}-11", "I2609-P-90", "O1", "09:32:00", "卖", 5, 1, 500, "", 1, f"{month}-11"]
    ]
    workbook_month = workbook_month or month
    declaration_fees = declaration_fees or []
    other_fund_rows: list[list[object]] = []
    for fee_date, amount in declaration_fees:
        other_fund_rows.extend(
            [
                [fee_date, None, "中国金融期货交易所", None, "中金所申报费", None, -abs(amount)],
                ["合计", None, None, None, "中金所申报费", None, -abs(amount)],
            ]
        )
    report_rows = [
        ["客户交易结算月报(逐笔对冲)"],
        [], [], [],
        ["客户期货期权内部资金账户", None, "TEST", None, None, "交易月份", None, workbook_month],
        [], [], [], [], [],
        ["期货期权账户资金状况"],
        ["上月结存", 9000, None, None, None, "客户权益", customer_equity],
        ["当月存取合计", 0, None, None, None, "实有货币资金", customer_equity],
        ["当月盈亏", monthly_pnl, None, None, None, "非货币充抵金额", 0],
        ["当月总权利金", 0, None, None, None, "货币充抵金额", 0],
        ["当月手续费", monthly_fee, None, None, None, "冻结资金", 0],
        ["当月结存", customer_equity - floating_pnl, None, None, None, "保证金占用", margin],
        ["浮动盈亏", floating_pnl, None, None, None, "可用资金", available],
        [None, None, None, None, None, "风险度", "22.00%"],
        [None, None, None, None, None, "追加保证金", 0],
        [],
        ["其它资金明细（单位：人民币）"],
        ["发生日期", None, "交易所", None, "类型", None, "金额", None, "备注"],
        *other_fund_rows,
        [],
        ["期货持仓汇总"],
        ["合约", "买持仓", "买均价", "卖持仓", "卖均价", "昨结算价", "今结算价", "浮动盈亏", "交易保证金", "投机（一般）/套保/套利"],
        *futures_positions,
        ["合计"],
        [],
        ["期权持仓汇总"],
        ["日期", "品种合约", "标的合约", "期权类型", "执行价", "买持仓", "买均价", "卖持仓", "卖均价", "昨结算价", "今结算价", "交易保证金", "交易编码"],
        *option_positions,
        ["合计"],
    ]
    futures_rows = [
        [], [], ["交易月份", month], [], [], [], [], [], ["成交明细"],
        ["交易日期", "合约", "成交序号", "成交时间", "买/卖", "投机（一般）/套保/套利", "成交价", "手数", "成交额", "开/平", "手续费", "平仓盈亏", "实际成交日期"],
        *futures_trades,
        ["合计"],
    ]
    option_rows = [
        [], [], ["交易月份", month], [], [], [], [], [], ["期权成交明细"],
        ["日期", "品种合约", "流水号", "成交时间", "买/卖", "权利金单价", "成交量", "权利金", "是否备兑", "手续费", "成交日期"],
        *option_trades,
        ["合计"],
    ]
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        _padded(report_rows).to_excel(writer, sheet_name="客户交易结算月报", header=False, index=False)
        _padded(futures_rows).to_excel(writer, sheet_name="成交明细", header=False, index=False)
        _padded(option_rows).to_excel(writer, sheet_name="期权成交明细", header=False, index=False)


class FuturesLiveTradingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_patcher = patch("core.db.DB_PATH", self.root / "cache.db")
        self.db_patcher.start()
        init_db()

    def tearDown(self) -> None:
        self.db_patcher.stop()
        self.temp.cleanup()

    def _july_path(self) -> Path:
        path = self.root / "00226050000029_2026-07.xlsx"
        write_statement(path, month="2026-07")
        return path

    def test_parse_statement_normalizes_positions_trades_and_multiplier(self) -> None:
        payload = parse_statement(self._july_path())

        self.assertEqual(payload.statement_month, "2026-07")
        self.assertEqual(payload.account["customer_equity"], 10000)
        self.assertEqual(set(payload.positions["asset_type"]), {"期货", "期权"})
        option = payload.positions[payload.positions["asset_type"].eq("期权")].iloc[0]
        self.assertEqual(option["contract"], "I2609P90")
        future_trade = payload.trades[payload.trades["asset_type"].eq("期货")].iloc[0]
        option_trade = payload.trades[payload.trades["asset_type"].eq("期权")].iloc[0]
        self.assertEqual(future_trade["multiplier"], 10)
        self.assertEqual(option_trade["multiplier"], 100)
        self.assertEqual(option_trade["open_close"], "未提供")

    def test_sync_is_idempotent_and_replaces_changed_statement(self) -> None:
        path = self._july_path()
        first = sync_statements(self.root)
        second = sync_statements(self.root)

        self.assertEqual(first.imported, 1)
        self.assertEqual(second.skipped, 1)
        self.assertEqual(len(list_futures_live_trades()), 2)

        write_statement(path, month="2026-07", customer_equity=12345, monthly_fee=4)
        changed = sync_statements(self.root)
        self.assertEqual(changed.imported, 1)
        self.assertEqual(len(list_futures_live_trades()), 2)
        self.assertEqual(list_monthly_accounts().iloc[-1]["customer_equity"], 12345)

    def test_filename_month_wins_when_all_trade_dates_confirm_source_anomaly(self) -> None:
        path = self.root / "00226050000029_2026-04.xlsx"
        write_statement(path, month="2026-04", workbook_month="2026-05")

        payload = parse_statement(path)

        self.assertEqual(payload.statement_month, "2026-04")
        self.assertTrue(any("已按 2026-04 导入" in item for item in payload.warnings))

    def test_declaration_fee_is_included_in_account_fee_reconciliation(self) -> None:
        path = self.root / "00226050000029_2026-04.xlsx"
        write_statement(
            path,
            month="2026-04",
            monthly_fee=11,
            declaration_fees=[("2026-04-01", 2), ("2026-04-24", 6)],
        )

        payload = parse_statement(path)
        self.assertEqual(payload.account["declaration_fee"], 8)
        self.assertFalse(any("账户手续费" in item for item in payload.warnings))

        sync_statements(self.root)
        summary = summarize_futures_live_pnl()
        self.assertEqual(summary["fee"], 11)
        self.assertEqual(summary["declaration_fee"], 8)
        self.assertEqual(summary["unallocated_fee"], 0)

    def test_option_previous_settlement_is_inferred_from_quote_change(self) -> None:
        self.assertEqual(_infer_previous_settlement(12.6, -10.00), 14.0)
        self.assertEqual(_infer_previous_settlement(20.5, -4.65), 21.5)
        self.assertEqual(_infer_previous_settlement(30.8, 0.00), 30.8)
        self.assertEqual(_infer_previous_settlement(39.6, -2.70), 40.7)

    def test_manual_open_close_updates_estimated_position_and_rejects_over_close(self) -> None:
        self._july_path()
        sync_statements(self.root)
        open_id = add_manual_trade(
            trade_date="2026-08-01",
            asset_type="期货",
            contract="I2609",
            buy_sell="买",
            open_close="开",
            price=110,
            quantity=1,
            turnover=1100,
            fee=1,
        )
        add_manual_trade(
            trade_date="2026-08-02",
            asset_type="期货",
            contract="I2609",
            buy_sell="卖",
            open_close="平",
            price=120,
            quantity=3,
            turnover=3600,
            fee=1,
        )

        position = build_estimated_positions().query("asset_type == '期货' and contract == 'I2609'").iloc[0]
        self.assertEqual(position["official_quantity"], 2)
        self.assertEqual(position["estimated_quantity"], 0)
        history = build_contract_pnl_history().query("asset_type == '期货' and contract == 'I2609'").iloc[0]
        self.assertAlmostEqual(history["realized_pnl"], (120 - (100 * 2 + 110) / 3) * 3 * 10)

        with self.assertRaisesRegex(ValueError, "最多可平"):
            add_manual_trade(
                trade_date="2026-08-03",
                asset_type="期货",
                contract="I2609",
                buy_sell="卖",
                open_close="平",
                price=120,
                quantity=1,
                turnover=1200,
            )
        with self.assertRaisesRegex(ValueError, "不能删除"):
            delete_manual_trade(open_id)

    def test_statement_takes_over_matching_manual_trade(self) -> None:
        self._july_path()
        sync_statements(self.root)
        add_manual_trade(
            trade_date="2026-08-01",
            trade_time="10:00:00",
            asset_type="期货",
            contract="I2609",
            buy_sell="买",
            open_close="开",
            price=110,
            quantity=1,
            turnover=1100,
            broker_trade_id="AUG1",
        )
        august = self.root / "00226050000029_2026-08.xlsx"
        write_statement(
            august,
            month="2026-08",
            futures_positions=[["I2609", 3, 103.333333, None, None, 109, 110, 200, 1800, "投机"]],
            option_positions=[],
            futures_trades=[["2026-08-01", "I2609", "AUG1", "10:00:00", "买", "投机", 110, 1, 1100, "开", 1, "--", "2026-08-01"]],
            option_trades=[],
            floating_pnl=200,
            margin=1800,
        )

        sync_statements(self.root)
        manual = list_futures_live_trades().query("source == '手工'").iloc[0]
        self.assertEqual(manual["reconciliation_status"], "已接管")
        position = build_estimated_positions().query("asset_type == '期货'").iloc[0]
        self.assertEqual(position["official_quantity"], 3)
        self.assertEqual(position["post_month_change"], 0)

    def test_ambiguous_fallback_match_is_left_for_review(self) -> None:
        self._july_path()
        sync_statements(self.root)
        add_manual_trade(
            trade_date="2026-08-01",
            trade_time="10:00:00",
            asset_type="期货",
            contract="I2609",
            buy_sell="买",
            open_close="开",
            price=110,
            quantity=1,
            turnover=1100,
        )
        august = self.root / "00226050000029_2026-08.xlsx"
        duplicate_rows = [
            ["2026-08-01", "I2609", broker_id, "10:00:00", "买", "投机", 110, 1, 1100, "开", 1, "--", "2026-08-01"]
            for broker_id in ("A1", "A2")
        ]
        write_statement(
            august,
            month="2026-08",
            futures_positions=[["I2609", 4, 105, None, None, 109, 110, 200, 1800, "投机"]],
            option_positions=[],
            futures_trades=duplicate_rows,
            option_trades=[],
            monthly_fee=2,
            floating_pnl=200,
            margin=1800,
        )

        sync_statements(self.root)
        manual = list_futures_live_trades().query("source == '手工'").iloc[0]
        self.assertEqual(manual["reconciliation_status"], "待核对")

    def test_current_pnl_uses_latest_common_formal_close_date(self) -> None:
        self._july_path()
        sync_statements(self.root)
        with get_conn() as conn:
            for asset_type, contract, day, close, settlement in (
                ("期货", "I2609", "2026-08-03", 110, 109),
                ("期货", "I2609", "2026-08-04", 112, 111),
                ("期权", "I2609P90", "2026-08-03", 3, 3.1),
                ("期权", "I2609P90", "2026-08-04", 2, 2.2),
            ):
                conn.execute(
                    """
                    INSERT INTO futures_daily_closes (
                        asset_type, contract, trade_date, close_price,
                        settlement_price, source, settlement_source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, '测试', '测试结算', '2026-08-04 16:00:00')
                    """,
                    (asset_type, contract, day, close, settlement),
                )
            conn.commit()

        current = build_current_position_pnl()
        self.assertEqual(set(current["valuation_date"]), {"2026-08-04"})
        future = current.query("asset_type == '期货'").iloc[0]
        option = current.query("asset_type == '期权'").iloc[0]
        self.assertEqual(future["daily_pnl"], 40)
        self.assertEqual(option["daily_pnl"], 100)
        summary = summarize_futures_live_pnl()
        self.assertEqual(summary["daily_pnl"], 140)
        self.assertEqual(summary["valuation_date"], "2026-08-04")
        mark_summary = summarize_futures_live_pnl(
            valuation_mode="settlement",
            include_declaration_fee=False,
        )
        self.assertEqual(mark_summary["valuation_date"], "2026-08-04")
        self.assertAlmostEqual(mark_summary["net_pnl"], summary["net_pnl"] - 40)
        daily = build_daily_account_pnl()
        formal_dates = daily.loc[daily["source"].eq("正式收盘估值"), "date"].tolist()
        self.assertEqual(formal_dates, ["2026-08-03", "2026-08-04"])

    def test_changed_file_parse_failure_keeps_last_good_data(self) -> None:
        path = self._july_path()
        sync_statements(self.root)
        original_equity = list_monthly_accounts().iloc[-1]["customer_equity"]
        path.write_text("not an excel workbook", encoding="utf-8")

        result = sync_statements(self.root)

        self.assertEqual(result.failed, 1)
        self.assertEqual(list_monthly_accounts().iloc[-1]["customer_equity"], original_equity)
        self.assertEqual(len(list_futures_live_trades()), 2)

    def test_import_status_records_success_and_warnings(self) -> None:
        path = self.root / "00226050000029_2026-04.xlsx"
        write_statement(path, month="2026-04", workbook_month="2026-05", monthly_fee=10)
        result = sync_statements(self.root)
        imports = list_statement_imports()

        self.assertEqual(result.failed, 0)
        self.assertEqual(imports.iloc[0]["status"], "成功")
        self.assertIn("工作簿交易月份", imports.iloc[0]["warnings"])


if __name__ == "__main__":
    unittest.main()
