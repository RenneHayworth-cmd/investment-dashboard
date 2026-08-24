from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import pandas as pd

from core.db import get_conn, init_db
from services.futures_live_trading import (
    _historical_contract_requirements,
    _fetch_cffex_settlement_history,
    _infer_previous_settlement,
    _parse_dce_option_settlement_payload,
    _save_daily_close_frame,
    _save_daily_settlement_frame,
    _update_position_settlements,
    add_manual_cash_flow,
    add_manual_daily_pnl,
    add_manual_trade,
    build_contract_pnl_history,
    build_current_position_pnl,
    build_daily_account_pnl,
    build_estimated_positions,
    build_futures_daily_returns,
    confirm_option_expiry_event,
    delete_manual_cash_flow,
    delete_manual_daily_pnl,
    delete_manual_trade,
    iron_ore_option_expiry_date,
    list_futures_cash_flows,
    list_futures_daily_pnl_overrides,
    list_futures_live_trades,
    load_daily_closes,
    list_monthly_accounts,
    list_option_expiry_candidates,
    list_option_expiry_events,
    list_statement_imports,
    parse_statement,
    resolve_manual_daily_pnl,
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
    cash_flows: list[tuple[str, float, float, str]] | None = None,
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
    cash_flows = cash_flows or []
    deposits_withdrawals = sum(
        float(deposit or 0) - float(withdrawal or 0)
        for _, deposit, withdrawal, _ in cash_flows
    )
    cash_flow_rows = [
        [flow_date, deposit or None, withdrawal or None, summary]
        for flow_date, deposit, withdrawal, summary in cash_flows
    ]
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
        ["当月存取合计", deposits_withdrawals, None, None, None, "实有货币资金", customer_equity],
        ["当月盈亏", monthly_pnl, None, None, None, "非货币充抵金额", 0],
        ["当月总权利金", 0, None, None, None, "货币充抵金额", 0],
        ["当月手续费", monthly_fee, None, None, None, "冻结资金", 0],
        ["当月结存", customer_equity - floating_pnl, None, None, None, "保证金占用", margin],
        ["浮动盈亏", floating_pnl, None, None, None, "可用资金", available],
        [None, None, None, None, None, "风险度", "22.00%"],
        [None, None, None, None, None, "追加保证金", 0],
        [],
        ["期货期权账户出入金明细（单位：人民币）"],
        ["发生日期", "入金", "出金", "摘要"],
        *cash_flow_rows,
        ["合计"],
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

    def test_statement_cash_flows_include_daily_transfers_and_account_fees(self) -> None:
        path = self.root / "00226050000029_2026-04.xlsx"
        write_statement(
            path,
            month="2026-04",
            monthly_fee=11,
            cash_flows=[
                ("2026-04-01", 200000, 0, "银期转账"),
                ("2026-04-24", 0, 21.2, "银期转账"),
            ],
            declaration_fees=[("2026-04-01", 2), ("2026-04-24", 6)],
        )

        payload = parse_statement(path)
        external = payload.cash_flows[
            payload.cash_flows["entry_type"].isin(["入金", "出金"])
        ]
        fees = payload.cash_flows[payload.cash_flows["entry_type"].eq("申报费")]

        self.assertEqual(len(external), 2)
        self.assertAlmostEqual(
            external.loc[external["entry_type"].eq("入金"), "amount"].sum(),
            200000,
        )
        self.assertAlmostEqual(
            external.loc[external["entry_type"].eq("出金"), "amount"].sum(),
            21.2,
        )
        self.assertAlmostEqual(fees["amount"].sum(), 8)
        self.assertAlmostEqual(payload.account["deposits_withdrawals"], 199978.8)

    def test_manual_cash_flow_is_deletable_and_later_statement_takes_it_over(self) -> None:
        self._july_path()
        sync_statements(self.root)
        removable_id = add_manual_cash_flow(
            flow_date="2026-08-03",
            entry_type="出金",
            amount=100,
        )
        self.assertTrue(delete_manual_cash_flow(removable_id))
        add_manual_cash_flow(
            flow_date="2026-08-04",
            entry_type="入金",
            amount=5000,
            notes="待月结单核对",
        )
        august = self.root / "00226050000029_2026-08.xlsx"
        write_statement(
            august,
            month="2026-08",
            cash_flows=[("2026-08-04", 5000, 0, "银期转账")],
        )

        sync_statements(self.root)
        flows = list_futures_cash_flows()
        manual = flows[flows["source"].eq("手工")].iloc[0]
        official = flows[
            flows["source"].eq("月结单")
            & flows["flow_date"].eq("2026-08-04")
        ].iloc[0]
        self.assertEqual(manual["reconciliation_status"], "已接管")
        self.assertEqual(int(manual["matched_statement_flow_id"]), int(official["id"]))

    def test_daily_return_uses_economic_equity_and_netted_cash_flow(self) -> None:
        path = self.root / "00226050000029_2026-07.xlsx"
        write_statement(
            path,
            month="2026-07",
            monthly_fee=0,
            cash_flows=[
                ("2026-07-01", 10000, 0, "首次入金"),
                ("2026-07-02", 1000, 0, "追加入金"),
                ("2026-07-02", 0, 200, "同日出金"),
                ("2026-07-03", 0, 500, "净出金"),
                ("2026-07-04", 1000, 0, "周末入金"),
            ],
            futures_positions=[
                ["I2609", 1, 100, None, None, 100, 100, 0, 1000, "投机"]
            ],
            option_positions=[],
            futures_trades=[
                ["2026-07-01", "I2609", "F1", "09:31:00", "买", "投机", 100, 1, 1000, "开", 0, "--", "2026-07-01"]
            ],
            option_trades=[],
            floating_pnl=0,
        )
        sync_statements(self.root)
        with get_conn() as conn:
            for day, close in (
                ("2026-07-01", 100),
                ("2026-07-02", 110),
                ("2026-07-03", 120),
                ("2026-07-06", 130),
                ("2026-07-07", 140),
            ):
                conn.execute(
                    """
                    INSERT INTO futures_daily_closes (
                        asset_type, contract, trade_date, close_price,
                        source, updated_at
                    ) VALUES ('期货', 'I2609', ?, ?, '测试', '2026-07-06 16:00:00')
                    """,
                    (day, close),
                )
            conn.commit()

        daily = build_daily_account_pnl(as_of="2026-07-07").set_index("date")
        self.assertEqual(daily.loc["2026-07-01", "net_cash_flow"], 10000)
        self.assertEqual(daily.loc["2026-07-02", "net_cash_flow"], 800)
        self.assertEqual(daily.loc["2026-07-03", "net_cash_flow"], -500)
        self.assertEqual(daily.loc["2026-07-06", "net_cash_flow"], 1000)
        self.assertEqual(daily.loc["2026-07-07", "net_cash_flow"], 0)
        self.assertEqual(daily.loc["2026-07-01", "return_base"], 10000)
        self.assertEqual(daily.loc["2026-07-02", "return_base"], 10800)
        self.assertEqual(daily.loc["2026-07-03", "return_base"], 10900)
        self.assertEqual(daily.loc["2026-07-06", "return_base"], 11500)
        self.assertEqual(daily.loc["2026-07-07", "return_base"], 11600)
        self.assertAlmostEqual(daily.loc["2026-07-02", "daily_return_pct"], 100 / 10800 * 100)
        self.assertAlmostEqual(daily.loc["2026-07-03", "daily_return_pct"], 100 / 10900 * 100)
        self.assertAlmostEqual(daily.loc["2026-07-06", "daily_return_pct"], 100 / 11500 * 100)
        self.assertAlmostEqual(daily.loc["2026-07-07", "daily_return_pct"], 100 / 11600 * 100)
        returns = build_futures_daily_returns(daily.reset_index())
        self.assertAlmostEqual(returns["pnl_amount"].sum(), daily.iloc[-1]["net_pnl"])

    def test_daily_close_save_excludes_dates_after_completed_target(self) -> None:
        saved = _save_daily_close_frame(
            "期货",
            "I2609",
            pd.DataFrame(
                [
                    {"date": "2026-07-01", "close": 700},
                    {"date": "2026-07-02", "close": 710},
                ]
            ),
            "测试",
            max_trade_date="2026-07-01",
        )

        self.assertEqual(saved, 1)
        self.assertEqual(load_daily_closes()["trade_date"].tolist(), ["2026-07-01"])

    def test_settlement_save_is_append_only_and_reports_conflict(self) -> None:
        invalid_future = _save_daily_settlement_frame(
            "期货",
            "IM2609",
            pd.DataFrame(
                [{"date": "2026-07-01", "close": 7600, "settlement": 0}]
            ),
            "无效测试",
        )
        first = _save_daily_settlement_frame(
            "期权",
            "I2609P730",
            pd.DataFrame(
                [{"date": "2026-07-01", "close": 0, "settlement": 12.5}]
            ),
            "大商所测试",
        )
        second = _save_daily_settlement_frame(
            "期权",
            "I2609P730",
            pd.DataFrame(
                [{"date": "2026-07-01", "close": 1, "settlement": 13.0}]
            ),
            "另一来源",
        )

        self.assertEqual(invalid_future["updated"], 0)
        self.assertEqual(first["updated"], 1)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(len(second["conflicts"]), 1)
        saved = load_daily_closes().iloc[0]
        self.assertEqual(saved["close_price"], 0)
        self.assertEqual(saved["settlement_price"], 12.5)

    def test_force_position_settlement_refresh_reuses_formal_cached_value(self) -> None:
        _save_daily_settlement_frame(
            "期权",
            "I2609P730",
            pd.DataFrame(
                [{"date": "2026-08-14", "close": 10, "settlement": 12.5}]
            ),
            "大商所测试",
        )
        positions = pd.DataFrame(
            [{"asset_type": "期权", "contract": "I2609P730"}]
        )

        akshare = Mock()
        with patch.dict("sys.modules", {"akshare": akshare}):
            result = _update_position_settlements(
                positions,
                "2026-08-14",
                force=True,
            )

        self.assertEqual(result, {"updated": 0, "skipped": 1, "errors": []})
        self.assertFalse(akshare.method_calls)

    def test_dce_option_payload_is_filtered_and_normalized(self) -> None:
        parsed = _parse_dce_option_settlement_payload(
            {
                "data": [
                    {"contractId": "i2609-P-730", "close": "12.0", "clearPrice": "12.5"},
                    {"contractId": "i2609-P-740", "close": "20.0", "clearPrice": "20.5"},
                ]
            },
            "2026-08-13",
            {"I2609P730"},
        )

        self.assertEqual(parsed["contract"].tolist(), ["I2609P730"])
        self.assertEqual(parsed.iloc[0]["settlement"], 12.5)

    def test_cffex_monthly_history_normalizes_option_contracts(self) -> None:
        csv_text = ",".join(f"c{index}" for index in range(15)) + "\n"
        csv_text += ",".join(
            [
                "MO2606-P-5400", "0", "0", "0", "1", "2", "3", "0",
                "26.4", "27.0", "25.0", "0", "0", "0", "0",
            ]
        )
        archive_buffer = BytesIO()
        with zipfile.ZipFile(archive_buffer, "w") as archive:
            archive.writestr("20260317_1.csv", csv_text.encode("gb2312"))
        response = Mock(content=archive_buffer.getvalue())
        response.raise_for_status.return_value = None

        with patch("requests.get", return_value=response):
            frame, source = _fetch_cffex_settlement_history(
                {"MO2606P5400"},
                "2026-03-17",
                "2026-03-17",
            )

        self.assertEqual(source, "中金所历史日行情结算价")
        self.assertEqual(frame.iloc[0]["contract"], "MO2606P5400")
        self.assertEqual(frame.iloc[0]["settlement"], 27.0)

    def test_mark_to_market_uses_settlement_equity_and_trade_fees_only(self) -> None:
        path = self.root / "00226050000029_2026-07.xlsx"
        write_statement(
            path,
            month="2026-07",
            monthly_fee=10,
            declaration_fees=[("2026-07-01", 8)],
            cash_flows=[("2026-07-01", 10000, 0, "首次入金")],
            futures_positions=[
                ["I2609", 1, 100, None, None, 100, 100, 0, 1000, "投机"]
            ],
            option_positions=[],
            futures_trades=[
                ["2026-07-01", "I2609", "F1", "09:31:00", "买", "投机", 100, 1, 1000, "开", 2, "--", "2026-07-01"]
            ],
            option_trades=[],
            floating_pnl=0,
        )
        sync_statements(self.root)
        with get_conn() as conn:
            for day, settlement in (
                ("2026-07-01", 100),
                ("2026-07-02", 110),
            ):
                conn.execute(
                    """
                    INSERT INTO futures_daily_closes (
                        asset_type, contract, trade_date, close_price,
                        settlement_price, source, settlement_source, updated_at
                    ) VALUES ('期货', 'I2609', ?, ?, ?, '测试', '测试', '2026-07-02 16:00:00')
                    """,
                    (day, settlement + 5, settlement),
                )
            conn.commit()

        daily = build_daily_account_pnl(
            as_of="2026-07-02", valuation_mode="settlement"
        ).set_index("date")
        self.assertEqual(daily.loc["2026-07-01", "fee"], 2)
        self.assertEqual(daily.loc["2026-07-01", "daily_pnl"], -2)
        self.assertEqual(daily.loc["2026-07-02", "daily_pnl"], 100)
        self.assertEqual(daily.loc["2026-07-02", "return_base"], 9998)
        self.assertAlmostEqual(
            daily.loc["2026-07-02", "daily_return_pct"], 100 / 9998 * 100
        )
        add_manual_daily_pnl(trade_date="2026-07-02", pnl_amount=100.03)
        reconciled = build_daily_account_pnl(
            as_of="2026-07-02", valuation_mode="settlement"
        ).set_index("date")
        self.assertEqual(reconciled.loc["2026-07-02", "daily_pnl"], 100)
        self.assertEqual(
            reconciled.loc["2026-07-02", "reconciliation_status"], "已一致"
        )

    def test_manual_daily_pnl_fills_gap_and_reconciles_formal_difference(self) -> None:
        path = self.root / "00226050000029_2026-07.xlsx"
        write_statement(
            path,
            month="2026-07",
            monthly_fee=0,
            cash_flows=[("2026-07-01", 10000, 0, "首次入金")],
            futures_positions=[
                ["I2609", 1, 100, None, None, 100, 100, 0, 1000, "投机"]
            ],
            option_positions=[],
            futures_trades=[
                ["2026-07-01", "I2609", "F1", "09:31:00", "买", "投机", 100, 1, 1000, "开", 0, "--", "2026-07-01"]
            ],
            option_trades=[],
            floating_pnl=0,
        )
        sync_statements(self.root)
        with get_conn() as conn:
            for day, settlement in (
                ("2026-07-01", 100),
                ("2026-07-03", 120),
            ):
                conn.execute(
                    """
                    INSERT INTO futures_daily_closes (
                        asset_type, contract, trade_date, close_price,
                        settlement_price, source, settlement_source, updated_at
                    ) VALUES ('期货', 'I2609', ?, ?, ?, '测试', '测试', '2026-07-03 16:00:00')
                    """,
                    (day, settlement, settlement),
                )
            conn.commit()
        record_id = add_manual_daily_pnl(
            trade_date="2026-07-02", pnl_amount=90, notes="同花顺截图"
        )

        estimated = build_daily_account_pnl(
            as_of="2026-07-03", valuation_mode="settlement"
        ).set_index("date")
        self.assertEqual(estimated.loc["2026-07-02", "status"], "手工估算")
        self.assertEqual(estimated.loc["2026-07-02", "daily_pnl"], 90)
        self.assertEqual(estimated.loc["2026-07-03", "daily_pnl"], 110)
        self.assertEqual(
            estimated.loc["2026-07-03", "confirmation_status"], "待前序核对"
        )

        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO futures_daily_closes (
                    asset_type, contract, trade_date, close_price,
                    settlement_price, source, settlement_source, updated_at
                ) VALUES ('期货', 'I2609', '2026-07-02', 110, 110,
                          '测试', '测试', '2026-07-03 16:00:00')
                """
            )
            conn.commit()
        compared = build_daily_account_pnl(
            as_of="2026-07-03", valuation_mode="settlement"
        ).set_index("date")
        self.assertEqual(compared.loc["2026-07-02", "formal_daily_pnl"], 100)
        self.assertEqual(compared.loc["2026-07-02", "difference"], 10)
        self.assertEqual(compared.loc["2026-07-02", "daily_pnl"], 90)
        self.assertEqual(compared.loc["2026-07-03", "daily_pnl"], 100)
        self.assertEqual(compared.loc["2026-07-03", "net_pnl"], 190)
        self.assertEqual(
            compared.loc["2026-07-02", "reconciliation_status"], "待核对"
        )
        resolve_manual_daily_pnl(record_id, "采用正式")
        resolved = build_daily_account_pnl(
            as_of="2026-07-03", valuation_mode="settlement"
        ).set_index("date")
        self.assertEqual(resolved.loc["2026-07-02", "daily_pnl"], 100)
        self.assertEqual(resolved.loc["2026-07-02", "status"], "完整")
        self.assertEqual(
            resolved.loc["2026-07-02", "reconciliation_status"], "采用正式"
        )
        self.assertEqual(
            resolved.loc["2026-07-03", "confirmation_status"], "正式"
        )

        removable = add_manual_daily_pnl(
            trade_date="2026-07-03", pnl_amount=100
        )
        self.assertTrue(delete_manual_daily_pnl(removable))
        records = list_futures_daily_pnl_overrides()
        self.assertEqual(records["trade_date"].tolist(), ["2026-07-02"])

    def test_isolated_manual_daily_pnl_remains_visible_without_cumulative_anchor(self) -> None:
        path = self.root / "00226050000029_2026-07.xlsx"
        write_statement(
            path,
            month="2026-07",
            monthly_fee=0,
            futures_positions=[
                ["I2609", 1, 100, None, None, 100, 100, 0, 1000, "投机"]
            ],
            option_positions=[],
            futures_trades=[
                ["2026-07-01", "I2609", "F1", "09:31:00", "买", "投机", 100, 1, 1000, "开", 0, "--", "2026-07-01"]
            ],
            option_trades=[],
            floating_pnl=0,
        )
        sync_statements(self.root)
        add_manual_daily_pnl(trade_date="2026-07-02", pnl_amount=-5560)

        daily = build_daily_account_pnl(
            as_of="2026-07-02", valuation_mode="settlement"
        )
        manual_day = daily.set_index("date").loc["2026-07-02"]
        self.assertEqual(manual_day["status"], "手工估算")
        self.assertEqual(manual_day["daily_pnl"], -5560)
        self.assertTrue(pd.isna(manual_day["net_pnl"]))
        returns = build_futures_daily_returns(daily)
        self.assertEqual(returns.iloc[0]["pnl_amount"], -5560)
        self.assertTrue(pd.isna(returns.iloc[0]["return_pct"]))

    def test_iron_ore_option_expiry_can_be_confirmed_per_strike(self) -> None:
        path = self.root / "00226050000029_2026-07.xlsx"
        write_statement(
            path,
            month="2026-07",
            monthly_fee=0,
            futures_positions=[],
            option_positions=[
                ["2026-07-31", "I2609-P-730", "I2609", "看跌期权", 730, None, None, 1, 20, 18, 17, 1000, "T1"],
                ["2026-07-31", "I2609-P-740", "I2609", "看跌期权", 740, None, None, 2, 30, 28, 27, 2000, "T2"],
            ],
            futures_trades=[
                ["2026-07-01", "I2609", "F1", "09:31:00", "买", "投机", 700, 1, 7000, "开", 0, "--", "2026-07-01"],
                ["2026-07-02", "I2609", "F2", "09:31:00", "卖", "投机", 710, 1, 7100, "平", 0, 100, "2026-07-02"],
            ],
            option_trades=[
                ["2026-07-10", "I2609-P-730", "O1", "09:32:00", "卖", 20, 1, 2000, "", 0, "2026-07-10"],
                ["2026-07-10", "I2609-P-740", "O2", "09:33:00", "卖", 30, 2, 6000, "", 0, "2026-07-10"],
            ],
            floating_pnl=0,
        )
        sync_statements(self.root)
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO futures_daily_closes (
                    asset_type, contract, trade_date, close_price,
                    settlement_price, source, settlement_source, updated_at
                ) VALUES ('期货', 'I2609', '2026-08-18', 735, 735,
                          '测试', '测试结算', '2026-08-18 16:00:00')
                """
            )
            conn.commit()

        self.assertEqual(iron_ore_option_expiry_date("I2609P730"), "2026-08-18")
        with patch(
            "services.futures_live_trading.completed_futures_daily_cutoff",
            return_value=pd.Timestamp("2026-08-19"),
        ):
            requirements = _historical_contract_requirements()
            underlying = requirements[
                requirements["asset_type"].eq("期货")
                & requirements["contract"].eq("I2609")
            ].iloc[0]
            self.assertEqual(underlying["target_date"], "2026-08-18")

            candidates = list_option_expiry_candidates(as_of="2026-08-18")
            expected = dict(zip(candidates["option_contract"], candidates["expected_outcome"]))
            self.assertEqual(expected, {"I2609P730": "作废", "I2609P740": "履约"})
            with self.assertRaisesRegex(ValueError, "一次确认全部"):
                confirm_option_expiry_event(
                    option_contract="I2609P740", outcome="履约", quantity=1
                )
            confirm_option_expiry_event(option_contract="I2609P730", outcome="作废")
            confirm_option_expiry_event(option_contract="I2609P740", outcome="履约")

        positions = build_estimated_positions(as_of="2026-08-19")
        assigned = positions[
            positions["asset_type"].eq("期货")
            & positions["contract"].eq("I2609")
            & positions["side"].eq("多")
        ].iloc[0]
        self.assertEqual(assigned["estimated_quantity"], 2)
        self.assertEqual(assigned["average_price"], 740)
        self.assertEqual(len(list_option_expiry_events()), 2)

    def test_option_closed_before_expiry_does_not_need_expiry_confirmation(self) -> None:
        path = self.root / "00226050000029_2026-07.xlsx"
        write_statement(
            path,
            month="2026-07",
            monthly_fee=0,
            futures_positions=[],
            option_positions=[
                ["2026-07-31", "I2609-P-730", "I2609", "看跌期权", 730, None, None, 1, 20, 18, 17, 1000, "T1"]
            ],
            futures_trades=[],
            option_trades=[
                ["2026-07-10", "I2609-P-730", "O1", "09:32:00", "卖", 20, 1, 2000, "", 0, "2026-07-10"]
            ],
            floating_pnl=0,
        )
        sync_statements(self.root)
        add_manual_trade(
            trade_date="2026-08-10",
            asset_type="期权",
            contract="I2609P730",
            buy_sell="买",
            open_close="平",
            price=10,
            quantity=1,
            turnover=1000,
            fee=1,
        )

        candidates = list_option_expiry_candidates(as_of="2026-08-18")
        self.assertTrue(candidates.empty)

    def test_unconfirmed_expiry_pauses_formal_returns_from_expiry_date(self) -> None:
        path = self.root / "00226050000029_2026-07.xlsx"
        write_statement(
            path,
            month="2026-07",
            monthly_fee=0,
            futures_positions=[],
            option_positions=[
                ["2026-07-31", "I2609-P-730", "I2609", "看跌期权", 730, None, None, 1, 20, 18, 17, 1000, "T1"]
            ],
            futures_trades=[],
            option_trades=[
                ["2026-07-10", "I2609-P-730", "O1", "09:32:00", "卖", 20, 1, 2000, "", 0, "2026-07-10"]
            ],
            floating_pnl=0,
        )
        sync_statements(self.root)
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO futures_daily_closes (
                    asset_type, contract, trade_date, close_price,
                    settlement_price, source, settlement_source, updated_at
                ) VALUES ('期货', 'I2609', '2026-08-18', 720, 720,
                          '测试', '测试结算', '2026-08-18 16:00:00')
                """
            )
            conn.commit()

        daily = build_daily_account_pnl(as_of="2026-08-19").set_index("date")
        self.assertEqual(daily.loc["2026-08-18", "status"], "数据不完整")
        self.assertIn("到期处理待确认", daily.loc["2026-08-18", "missing_contracts"])
        self.assertEqual(daily.loc["2026-08-19", "status"], "数据不完整")

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
