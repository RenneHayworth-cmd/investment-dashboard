from __future__ import annotations

import inspect
import unittest

import services.index_config as index_config
import services.index_ma20 as index_ma20
import services.index_source_router as index_source_router
import services.index_sources_yahoo as index_sources_yahoo
import services.update_tasks as update_tasks


class IndexServiceFacadeContractTests(unittest.TestCase):
    def test_index_ma20_facade_keeps_complete_legacy_contract(self):
        expected = {
            "YAHOO_CHART_HOSTS",
            "YAHOO_REQUEST_GATE",
            "INDEX_CONFIG",
            "INDEX_LONG_HISTORY_SOURCE",
            "INDEX_FINAL_HISTORY_SOURCE",
            "INDEX_SOURCE_CORRECTION_SOURCE",
            "INDEX_LONG_HISTORY_BARS",
            "INDEX_REPORT_DISPLAY_DAYS",
            "INDEX_RECENT_GAP_LOOKBACK_SESSIONS",
            "CFFEX_FUTURES_MAIN_PRODUCTS",
            "overlay_finalized_index_rows",
            "missing_recent_market_trade_dates",
            "source_correction_start",
            "extract_source_correction_rows",
            "source_correction_fetch_days",
            "filter_market_trading_dates",
            "filter_completed_market_dates",
            "sanitize_index_report_market_dates",
            "build_export_df",
            "normalize_akshare_index_df",
            "fetch_yahoo_chart_payload",
            "fetch_yahoo_latest_index_row",
            "supplement_stale_yahoo_history",
            "append_akshare_latest_index_row",
            "append_eastmoney_quote_row",
            "fetch_eastmoney_clist_latest_index_row",
            "append_eastmoney_clist_latest_index_row",
            "append_eastmoney_latest_index_row",
            "append_hk_index_spot_row",
            "append_futures_spot_row",
            "get_index_data_from_akshare_csindex",
            "get_index_data_from_akshare_cn",
            "get_index_data_from_akshare_cni",
            "extract_raw_from_export_df",
            "merge_newer_index_rows",
            "get_index_data_from_akshare_us",
            "get_index_data_from_akshare_hk",
            "get_index_data_from_yahoo",
            "is_sparse_daily_history",
            "get_index_data_from_eastmoney_kline",
            "get_index_data_from_akshare_eastmoney_fallback",
            "fetch_eastmoney_completed_global_row",
            "get_index_data_from_cboe_vix",
            "get_index_data_from_akshare_global",
            "get_index_data_from_akshare_futures_main",
            "fetch_index_from_source",
            "_append_unseen_raw_history",
            "_latest_completed_date_for_market",
            "_latest_raw_date",
            "fetch_index_history",
            "get_index_data_from_tickflow",
            "tickflow_quote_date",
            "append_tickflow_quote_row",
            "normalize_tickflow_index_df",
            "get_index_raw_from_tickflow",
            "merge_raw_index_data",
            "raw_cache_symbol",
            "display_index_symbol",
            "merge_by_date",
            "generate_index_ma20_report",
            "fetch_one_index",
            "build_summary",
            "calculate_ma20_transition",
            "calculate_ma20_transition_snapshot",
            "calculate_ma20_transition_history",
        }
        self.assertEqual(set(index_ma20.__all__), expected)
        self.assertTrue(all(hasattr(index_ma20, name) for name in expected))

    def test_update_tasks_facade_keeps_complete_legacy_contract(self):
        expected = {
            "ProgressCallback",
            "INDEX_HISTORY_BOOTSTRAP_DAYS",
            "INDEX_HISTORY_BOOTSTRAP_BARS",
            "INDEX_HISTORY_MIN_ROWS",
            "INDEX_HISTORY_INCREMENTAL_DAYS",
            "INDEX_VERIFICATION_TOLERANCE_PCT",
            "FUTURES_CURRENT_CONTRACT_HISTORY_SOURCE",
            "FUTURES_MAIN_CONTRACT_CACHE_SYMBOL",
            "FUTURES_MAIN_CONTRACT_CACHE_SOURCE",
            "FUTURES_MAIN_INDEX_NAMES",
            "UpdateResult",
            "_verification_report",
            "verify_updated_index_data",
            "run_index_ma20_update",
            "build_index_update_message",
            "build_timing_row",
            "cached_report_satisfies_current_quotes",
            "merge_index_report",
            "trim_index_report",
            "append_cached_index_rows",
            "futures_contract_history_cache_symbol",
            "load_futures_main_contract_mapping",
            "load_futures_current_contract_history",
            "_fetch_and_cache_futures_contract_history",
            "refresh_futures_current_contract_histories",
            "find_pending_futures_current_contract_index_names",
            "build_futures_current_contract_report",
            "sync_index_long_history",
            "enrich_index_report_indicators",
            "extract_cached_index_report",
            "refresh_cached_eastmoney_index_report",
            "persist_confirmed_index_report_row",
            "latest_index_trade_date",
            "build_stale_quote_message",
            "has_current_index_quote",
            "fetch_index_report",
        }
        self.assertEqual(set(update_tasks.__all__), expected)
        self.assertTrue(all(hasattr(update_tasks, name) for name in expected))

    def test_high_level_signatures_and_defaults_are_unchanged(self):
        self.assertEqual(
            list(inspect.signature(index_ma20.fetch_index_history).parameters),
            ["index_name", "index_config", "days"],
        )
        self.assertEqual(
            inspect.signature(index_ma20.fetch_index_history).parameters["days"].default,
            10000,
        )
        update_signature = inspect.signature(update_tasks.run_index_ma20_update)
        self.assertEqual(
            list(update_signature.parameters),
            [
                "api_key",
                "days",
                "cache_source",
                "use_fresh_cache",
                "progress_callback",
                "market_names",
                "index_names",
                "max_workers",
            ],
        )
        self.assertEqual(update_signature.parameters["api_key"].default, "")
        self.assertEqual(update_signature.parameters["days"].default, 120)
        self.assertEqual(update_signature.parameters["cache_source"].default, "auto")
        self.assertTrue(update_signature.parameters["use_fresh_cache"].default)
        self.assertEqual(update_signature.parameters["max_workers"].default, 4)
        self.assertEqual(update_tasks.UpdateResult.__module__, "services.update_tasks")

    def test_config_and_yahoo_request_gate_have_single_shared_instances(self):
        self.assertIs(index_ma20.INDEX_CONFIG, index_config.INDEX_CONFIG)
        self.assertIs(index_ma20.YAHOO_REQUEST_GATE, index_config.YAHOO_REQUEST_GATE)
        self.assertIs(
            index_sources_yahoo.YAHOO_REQUEST_GATE,
            index_config.YAHOO_REQUEST_GATE,
        )
        self.assertEqual(
            index_ma20._IMPLEMENTATIONS["fetch_index_from_source"].__module__,
            index_source_router.__name__,
        )


if __name__ == "__main__":
    unittest.main()
