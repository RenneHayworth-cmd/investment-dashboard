from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

import pandas as pd

from services import futures_live_models
from services import futures_live_prices
from services import futures_live_settlements
from services import futures_live_trading as facade


EXPECTED_EXPORTS = {
    "ASSET_TYPES",
    "BUY_SELL_VALUES",
    "CASH_FLOW_TYPES",
    "OPEN_CLOSE_VALUES",
    "OPTION_EXPIRY_OUTCOMES",
    "_fetch_cffex_settlement_history",
    "_historical_contract_requirements",
    "_infer_previous_settlement",
    "_parse_dce_option_settlement_payload",
    "_save_daily_close_frame",
    "_save_daily_settlement_frame",
    "_update_position_settlements",
    "add_manual_cash_flow",
    "add_manual_daily_pnl",
    "add_manual_trade",
    "build_contract_pnl_history",
    "build_current_position_pnl",
    "build_daily_account_pnl",
    "build_estimated_positions",
    "build_futures_daily_returns",
    "configured_statement_dir",
    "confirm_option_expiry_event",
    "delete_manual_cash_flow",
    "delete_manual_daily_pnl",
    "delete_manual_option_expiry_event",
    "delete_manual_trade",
    "iron_ore_option_expiry_date",
    "latest_monthly_account",
    "list_futures_cash_flows",
    "list_futures_daily_pnl_overrides",
    "list_futures_live_trades",
    "list_monthly_accounts",
    "list_option_expiry_candidates",
    "list_option_expiry_events",
    "list_statement_imports",
    "load_daily_closes",
    "parse_statement",
    "resolve_manual_daily_pnl",
    "summarize_futures_live_pnl",
    "sync_statements",
    "update_position_daily_closes",
    "update_traded_contract_daily_closes",
    "update_traded_contract_daily_settlements",
}

EXPECTED_SIGNATURES = {
    "add_manual_cash_flow": "(*, flow_date: 'object', entry_type: 'str', amount: 'float', notes: 'str' = '') -> 'int'",
    "add_manual_daily_pnl": "(*, trade_date: 'object', pnl_amount: 'float', notes: 'str' = '') -> 'int'",
    "add_manual_trade": "(*, trade_date: 'object', trade_time: 'str' = '', asset_type: 'str', contract: 'str', buy_sell: 'str', open_close: 'str', price: 'float', quantity: 'int', turnover: 'float | None' = None, fee: 'float' = 0, close_pnl: 'float | None' = None, broker_trade_id: 'str' = '', strategy: 'str' = '', notes: 'str' = '') -> 'int'",
    "build_contract_pnl_history": "(*, as_of: 'object' = None, valuation_mode: 'str' = 'close') -> 'pd.DataFrame'",
    "build_current_position_pnl": "(*, as_of: 'object' = None, valuation_mode: 'str' = 'close') -> 'pd.DataFrame'",
    "build_daily_account_pnl": "(*, as_of: 'object' = None, valuation_mode: 'str' = 'close') -> 'pd.DataFrame'",
    "build_estimated_positions": "(*, as_of: 'object' = None) -> 'pd.DataFrame'",
    "configured_statement_dir": "(value: 'str | os.PathLike[str] | None' = None) -> 'Path'",
    "confirm_option_expiry_event": "(*, option_contract: 'str', outcome: 'str', quantity: 'int | None' = None, notes: 'str' = '') -> 'int'",
    "list_futures_cash_flows": "(*, include_taken_over: 'bool' = True) -> 'pd.DataFrame'",
    "list_futures_live_trades": "(*, include_taken_over: 'bool' = True) -> 'pd.DataFrame'",
    "load_daily_closes": "(asset_type: 'str | None' = None, contract: 'str | None' = None) -> 'pd.DataFrame'",
    "parse_statement": "(path: 'str | os.PathLike[str]') -> 'StatementPayload'",
    "summarize_futures_live_pnl": "(*, as_of: 'object' = None, valuation_mode: 'str' = 'close', include_declaration_fee: 'bool' = True) -> 'dict[str, object]'",
    "sync_statements": "(directory: 'str | os.PathLike[str] | None' = None, *, force: 'bool' = False) -> 'StatementSyncResult'",
    "update_position_daily_closes": "(*, api_key: 'str' = '', force: 'bool' = False, market_now: 'datetime | None' = None) -> 'dict[str, object]'",
    "update_traded_contract_daily_closes": "(*, api_key: 'str' = '', force: 'bool' = False, market_now: 'datetime | None' = None) -> 'dict[str, object]'",
    "update_traded_contract_daily_settlements": "(*, force: 'bool' = False, market_now: 'datetime | None' = None) -> 'dict[str, object]'",
}


class FuturesLiveFacadeContractTests(unittest.TestCase):
    def test_existing_import_surface_is_explicitly_exported(self) -> None:
        self.assertTrue(EXPECTED_EXPORTS.issubset(set(facade.__all__)))
        for name in EXPECTED_EXPORTS:
            self.assertTrue(hasattr(facade, name), name)

    def test_high_level_signatures_and_defaults_are_stable(self) -> None:
        actual = {
            name: str(inspect.signature(getattr(facade, name)))
            for name in EXPECTED_SIGNATURES
        }
        self.assertEqual(actual, EXPECTED_SIGNATURES)

    def test_facade_reuses_model_dataclass_identity(self) -> None:
        self.assertIs(facade.StatementPayload, futures_live_models.StatementPayload)
        self.assertIs(facade.StatementSyncResult, futures_live_models.StatementSyncResult)
        self.assertEqual(facade.StatementPayload.__module__, facade.__name__)
        self.assertEqual(facade.StatementSyncResult.__module__, facade.__name__)

    def test_facade_routes_legacy_dependency_patches_at_call_time(self) -> None:
        with patch.object(
            facade,
            "_historical_contract_requirements",
            return_value=pd.DataFrame(),
        ) as requirements:
            result = facade.update_traded_contract_daily_settlements()

        requirements.assert_called_once_with(market_now=None)
        self.assertEqual(result["contracts"], 0)

    def test_settlement_backfill_parses_iron_ore_option_requirements(self) -> None:
        requirements = pd.DataFrame(
            [
                {
                    "asset_type": "期权",
                    "contract": "I2609P730",
                    "first_date": "2026-08-17",
                    "target_date": "2026-08-17",
                }
            ]
        )
        official = pd.DataFrame(
            [
                {
                    "date": "2026-08-17",
                    "contract": "I2609P730",
                    "close": 12.0,
                    "settlement": 12.5,
                }
            ]
        )
        with (
            patch.object(
                futures_live_settlements,
                "_historical_contract_requirements",
                return_value=requirements,
            ),
            patch.object(
                futures_live_settlements,
                "load_daily_closes",
                return_value=pd.DataFrame(
                    columns=[
                        "asset_type",
                        "contract",
                        "trade_date",
                        "settlement_price",
                    ]
                ),
            ),
            patch.object(
                futures_live_settlements,
                "_futures_trading_dates",
                return_value=["2026-08-17"],
            ),
            patch.object(
                futures_live_settlements,
                "_fetch_dce_option_settlements_for_date",
                return_value=(official, "大商所测试"),
            ) as fetch,
            patch.object(
                futures_live_settlements,
                "_save_daily_settlement_frame",
                return_value={"updated": 1, "conflicts": []},
            ) as save,
        ):
            result = futures_live_settlements.update_traded_contract_daily_settlements()

        fetch.assert_called_once_with("2026-08-17", {"I2609P730"})
        save.assert_called_once()
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["errors"], [])

    def test_close_backfill_fetches_iron_ore_option_history(self) -> None:
        requirements = pd.DataFrame(
            [
                {
                    "asset_type": "期权",
                    "contract": "I2609P730",
                    "first_date": "2026-08-17",
                    "target_date": "2026-08-18",
                }
            ]
        )
        history = pd.DataFrame(
            [
                {"date": "2026-08-17", "close": 9.1},
                {"date": "2026-08-18", "close": 1.3},
            ]
        )
        with (
            patch.object(
                futures_live_prices,
                "_historical_contract_requirements",
                return_value=requirements,
            ),
            patch.object(
                futures_live_prices,
                "load_daily_closes",
                return_value=pd.DataFrame(
                    columns=["asset_type", "contract", "trade_date"]
                ),
            ),
            patch.object(
                futures_live_prices,
                "fetch_option_from_akshare",
                return_value=(history, "AkShare期权日线", False),
            ) as fetch,
            patch.object(
                futures_live_prices,
                "_save_daily_close_frame",
                return_value=2,
            ) as save,
        ):
            result = futures_live_prices.update_traded_contract_daily_closes()

        fetch.assert_called_once_with(
            "i2609P730",
            "1d",
            5000,
            prefer_realtime_snapshot=False,
            market_now=None,
        )
        save.assert_called_once()
        self.assertEqual(result["updated"], 2)
        self.assertEqual(result["errors"], [])


if __name__ == "__main__":
    unittest.main()
