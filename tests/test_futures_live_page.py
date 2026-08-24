from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest


PAGE = Path(__file__).parents[1] / "pages" / "7_期货实盘.py"
TARGET_DATE = "2026-08-21"
ACCOUNT = pd.Series(
    {
        "statement_month": "2026-07",
        "statement_end_date": "2026-07-31",
        "customer_equity": 500_000.0,
        "monthly_pnl": 1_000.0,
        "floating_pnl": 500.0,
        "risk_ratio": 0.1,
        "margin": 50_000.0,
        "available_funds": 450_000.0,
    }
)
MONTHLY_ACCOUNT_COLUMNS = [
    "statement_month",
    "customer_equity",
    "monthly_pnl",
    "floating_pnl",
    "monthly_fee",
    "declaration_fee",
    "margin",
    "available_funds",
    "risk_ratio",
    "deposits_withdrawals",
]


def _sync_result() -> SimpleNamespace:
    return SimpleNamespace(scanned=0, imported=0, skipped=0, failed=0, errors=[])


def _patch_cached_page(stack: ExitStack) -> None:
    stack.enter_context(patch("core.db.init_db"))
    returns = {
        "configured_statement_dir": "/tmp/futures-statements",
        "sync_statements": _sync_result(),
        "latest_monthly_account": ACCOUNT,
        "summarize_futures_live_pnl": {},
        "build_daily_account_pnl": pd.DataFrame(),
        "list_option_expiry_candidates": pd.DataFrame(),
        "list_option_expiry_events": pd.DataFrame(),
        "build_current_position_pnl": pd.DataFrame(),
        "list_futures_daily_pnl_overrides": pd.DataFrame(),
        "build_contract_pnl_history": pd.DataFrame(),
        "list_futures_cash_flows": pd.DataFrame(),
        "list_futures_live_trades": pd.DataFrame(),
        "list_monthly_accounts": pd.DataFrame(columns=MONTHLY_ACCOUNT_COLUMNS),
        "list_statement_imports": pd.DataFrame(),
    }
    for name, value in returns.items():
        stack.enter_context(
            patch(f"services.futures_live_trading.{name}", return_value=value)
        )
    stack.enter_context(
        patch(
            "services.futures_spread.completed_futures_daily_cutoff",
            return_value=pd.Timestamp(f"{TARGET_DATE} 15:00:00"),
        )
    )


class FuturesLivePageSmokeTests(unittest.TestCase):
    def test_default_empty_render_is_cache_first_and_never_fetches_prices(self):
        with ExitStack() as stack:
            stack.enter_context(patch("core.db.init_db"))
            stack.enter_context(
                patch(
                    "services.futures_live_trading.configured_statement_dir",
                    return_value="/tmp/futures-statements",
                )
            )
            stack.enter_context(
                patch(
                    "services.futures_live_trading.sync_statements",
                    return_value=_sync_result(),
                )
            )
            stack.enter_context(
                patch(
                    "services.futures_live_trading.latest_monthly_account",
                    return_value=None,
                )
            )
            history_close = stack.enter_context(
                patch("services.futures_live_trading.update_traded_contract_daily_closes")
            )
            history_settlement = stack.enter_context(
                patch(
                    "services.futures_live_trading.update_traded_contract_daily_settlements"
                )
            )
            positions = stack.enter_context(
                patch("services.futures_live_trading.update_position_daily_closes")
            )
            app = AppTest.from_file(str(PAGE), default_timeout=20).run()

        self.assertEqual(list(app.exception), [])
        self.assertEqual([item.value for item in app.subheader], ["数据更新"])
        self.assertEqual(app.text_input[0].label, "月结单目录")
        history_close.assert_not_called()
        history_settlement.assert_not_called()
        positions.assert_not_called()

    def test_cached_account_render_reuses_completed_session_without_network(self):
        with ExitStack() as stack:
            _patch_cached_page(stack)
            history_close = stack.enter_context(
                patch(
                    "services.futures_live_trading.update_traded_contract_daily_closes",
                    side_effect=AssertionError("已完成会话不应再次更新收盘价"),
                )
            )
            history_settlement = stack.enter_context(
                patch(
                    "services.futures_live_trading.update_traded_contract_daily_settlements",
                    side_effect=AssertionError("已完成会话不应再次更新结算价"),
                )
            )
            positions = stack.enter_context(
                patch(
                    "services.futures_live_trading.update_position_daily_closes",
                    side_effect=AssertionError("已完成会话不应再次更新当前持仓"),
                )
            )
            app = AppTest.from_file(str(PAGE), default_timeout=20)
            app.session_state["futures_live_history_close_target"] = (
                f"{ACCOUNT['statement_month']}|{TARGET_DATE}"
            )
            app.session_state["futures_live_auto_close_target"] = TARGET_DATE
            app.run()

        self.assertEqual(list(app.exception), [])
        self.assertEqual(
            [item.value for item in app.subheader],
            [
                "数据更新",
                "当前持仓盈亏",
                "账户盈亏趋势",
                "历史盈亏",
                "资金流水明细",
                "成交明细",
                "月度账户与导入状态",
            ],
        )
        self.assertEqual(
            [(item.label, item.value) for item in app.radio],
            [("持仓类型", "全部"), ("历史类型", "全部")],
        )
        self.assertEqual(app.segmented_control[0].label, "盈亏口径")
        self.assertEqual(app.segmented_control[0].value, "盯市")
        history_close.assert_not_called()
        history_settlement.assert_not_called()
        positions.assert_not_called()

    def test_first_session_auto_refresh_keeps_history_then_position_order(self):
        calls: list[str] = []

        def history_close(*args, **kwargs):
            calls.append("历史收盘")
            return {"updated": 0, "errors": []}

        def history_settlement(*args, **kwargs):
            calls.append("历史结算")
            return {"updated": 0, "errors": [], "conflicts": []}

        def positions(*args, **kwargs):
            calls.append("当前持仓")
            return {
                "updated": 0,
                "settlement_updated": 0,
                "errors": [],
                "settlement_errors": [],
                "target_date": TARGET_DATE,
            }

        with ExitStack() as stack:
            _patch_cached_page(stack)
            stack.enter_context(
                patch(
                    "services.futures_live_trading.update_traded_contract_daily_closes",
                    side_effect=history_close,
                )
            )
            stack.enter_context(
                patch(
                    "services.futures_live_trading.update_traded_contract_daily_settlements",
                    side_effect=history_settlement,
                )
            )
            stack.enter_context(
                patch(
                    "services.futures_live_trading.update_position_daily_closes",
                    side_effect=positions,
                )
            )
            app = AppTest.from_file(str(PAGE), default_timeout=20).run()

        self.assertEqual(list(app.exception), [])
        self.assertEqual(calls, ["历史收盘", "历史结算", "当前持仓"])
        self.assertEqual(
            app.session_state["futures_live_history_close_target"],
            f"{ACCOUNT['statement_month']}|{TARGET_DATE}",
        )
        self.assertEqual(
            app.session_state["futures_live_auto_close_target"], TARGET_DATE
        )


if __name__ == "__main__":
    unittest.main()
