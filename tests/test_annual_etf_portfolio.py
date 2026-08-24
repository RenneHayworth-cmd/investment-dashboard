from __future__ import annotations

import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import services.annual_etf_portfolio as annual_etf_portfolio
from services.annual_etf_portfolio import (
    ALL_SLOTS,
    EXECUTION_NEXT_CLOSE,
    EXECUTION_SAME_CLOSE,
    AnnualBacktestSettings,
    AnnualCheckpointStore,
    AnnualQualificationResult,
    AnnualSelection,
    HistoricalEtfRecord,
    PARKING_LISTING_DATE,
    _simulate_parking_benchmark,
    build_annual_selections,
    data_fingerprint,
    normalize_annual_market_data,
    preflight_annual_candidates,
    run_annual_etf_backtest,
    score_metrics,
    simulate_annual_portfolio,
    stitch_proxy_history,
    validate_registry_against_whitelist,
)


class AnnualEtfFacadeContractTests(unittest.TestCase):
    def test_compatibility_facade_keeps_exports_and_run_signature(self):
        expected_exports = {
            "AnnualBacktestSettings",
            "AnnualCheckpointStore",
            "AnnualPortfolioResult",
            "AnnualQualificationResult",
            "AnnualSelection",
            "HistoricalEtfRecord",
            "build_annual_selections",
            "data_fingerprint",
            "normalize_annual_market_data",
            "preflight_annual_candidates",
            "run_annual_etf_backtest",
            "score_metrics",
            "simulate_annual_portfolio",
            "stitch_proxy_history",
            "_simulate_parking_benchmark",
        }
        self.assertFalse(expected_exports - set(annual_etf_portfolio.__all__))
        for name in expected_exports:
            self.assertTrue(hasattr(annual_etf_portfolio, name), name)

        for type_name in (
            "HistoricalEtfRecord",
            "AnnualEtfRegistryEntry",
            "AnnualBacktestSettings",
            "AnnualSelection",
            "AnnualQualificationResult",
            "AnnualPortfolioResult",
            "DirectionSleeveState",
            "AnnualCheckpointStore",
        ):
            self.assertEqual(
                getattr(annual_etf_portfolio, type_name).__module__,
                "services.annual_etf_portfolio",
            )

        signature = inspect.signature(annual_etf_portfolio.run_annual_etf_backtest)
        self.assertEqual(
            list(signature.parameters),
            [
                "records",
                "whitelist",
                "market_data",
                "settings",
                "proxy_data",
                "checkpoint_dir",
                "progress_callback",
            ],
        )
        self.assertIsNone(signature.parameters["proxy_data"].default)
        self.assertIsNone(signature.parameters["checkpoint_dir"].default)
        self.assertIsNone(signature.parameters["progress_callback"].default)


def make_market(dates, prices, *, amount=1_000_000.0):
    prices = np.asarray(prices, dtype=float)
    raw = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(dates),
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "amount": amount if np.isscalar(amount) else amount,
        }
    )
    return normalize_annual_market_data(raw)


def make_selection(year, slot, symbol, *, ma=3, threshold=0.0, score=1.0):
    return AnnualSelection(
        year=year,
        slot=slot,
        symbol=symbol,
        name=f"ETF-{symbol}",
        ma_period=ma,
        threshold_pct=threshold,
        strategy="half_timing" if slot in {"us_sp500", "us_nasdaq"} else "timing",
        validation_score=score,
        validation_annual_return_pct=12.0,
        validation_sharpe=1.0,
        return_gate_relaxed=False,
        proxy_ratio_pct=0.0,
        decision_date=pd.Timestamp(year - 1, 12, 31),
    )


class AnnualEtfDataTests(unittest.TestCase):
    def test_future_dividend_does_not_rewrite_past_signal(self):
        dates = pd.bdate_range("2020-01-01", periods=6)
        raw = pd.DataFrame({"trade_date": dates, "close": [10, 11, 12, 13, 14, 15]})
        baseline = normalize_annual_market_data(raw)
        with_future_action = normalize_annual_market_data(
            raw,
            dividends=pd.DataFrame(
                {"trade_date": [dates[-1]], "dividend_per_share": [1.0]}
            ),
        )
        pd.testing.assert_series_equal(
            baseline.loc[:4, "signal_close"],
            with_future_action.loc[:4, "signal_close"],
            check_names=False,
        )
        self.assertGreater(
            with_future_action.loc[5, "signal_close"], baseline.loc[5, "signal_close"]
        )

    def test_proxy_chain_is_anchored_without_making_prelisting_rows_actual(self):
        proxy_dates = pd.bdate_range("2019-12-25", periods=5)
        actual_dates = pd.bdate_range("2020-01-02", periods=3)
        proxy = make_market(proxy_dates, [8, 9, 10, 11, 12])
        actual = make_market(actual_dates, [100, 101, 102])
        stitched = stitch_proxy_history(actual, proxy, actual_dates[0])
        proxy_rows = stitched[stitched["is_proxy"]]
        actual_rows = stitched[~stitched["is_proxy"]]
        self.assertTrue((proxy_rows["trade_date"] < actual_dates[0]).all())
        self.assertTrue((actual_rows["trade_date"] >= actual_dates[0]).all())
        self.assertAlmostEqual(proxy_rows.iloc[-1]["signal_close"], 100.0)
        self.assertAlmostEqual(actual_rows.iloc[0]["signal_close"], 100.0)

    def test_preflight_applies_listing_turnover_and_same_index_representative(self):
        dates = pd.bdate_range("2019-01-01", "2023-12-29")
        rising = np.linspace(10, 20, len(dates))
        records = [
            HistoricalEtfRecord(
                "510001", "低成交额", "SH", pd.Timestamp("2018-01-01"), "同一指数", "family", "a_large", "official"
            ),
            HistoricalEtfRecord(
                "510002", "高成交额", "SH", pd.Timestamp("2018-01-01"), "同一指数", "family", "a_large", "official"
            ),
            HistoricalEtfRecord(
                "510003",
                "新上市",
                "SH",
                pd.Timestamp("2023-11-20"),
                "同一指数",
                "family",
                "a_large",
                "official",
                proxy_symbol="510001",
            ),
        ]
        market = {
            "510001": make_market(dates, rising, amount=100.0),
            "510002": make_market(dates, rising, amount=200.0),
            "510003": make_market(dates[-30:], rising[-30:], amount=500.0),
        }
        whitelist = {"directions": {"a_large": {"families": ["family"]}}}
        settings = AnnualBacktestSettings(start_year=2024, end_date="2024-06-30")
        result = preflight_annual_candidates(records, whitelist, market, settings)
        rows = result.qualification.set_index("symbol")
        self.assertTrue(bool(rows.loc["510002", "qualified"]))
        self.assertFalse(bool(rows.loc["510001", "qualified"]))
        self.assertEqual(rows.loc["510001", "reason"], "同指数代表ETF未胜出")
        self.assertFalse(bool(rows.loc["510003", "qualified"]))
        self.assertIn("上市实际交易日不足", rows.loc["510003", "reason"])

    def test_percentile_score_rewards_lower_risk_and_higher_return(self):
        frame = pd.DataFrame(
            [
                {
                    "name": "better",
                    "longest_underwater_days": 10,
                    "max_drawdown_pct": -10,
                    "annual_volatility_pct": 12,
                    "annual_return_pct": 15,
                    "sharpe_ratio": 1.2,
                },
                {
                    "name": "worse",
                    "longest_underwater_days": 40,
                    "max_drawdown_pct": -30,
                    "annual_volatility_pct": 25,
                    "annual_return_pct": 2,
                    "sharpe_ratio": 0.1,
                },
            ]
        )
        scored = score_metrics(frame).set_index("name")
        self.assertGreater(
            scored.loc["better", "composite_score"], scored.loc["worse", "composite_score"]
        )

    def test_validation_return_gate_relaxes_when_every_candidate_is_below_ten_percent(self):
        dates = pd.bdate_range("2019-01-01", periods=100)
        research = make_market(dates, np.full(len(dates), 10.0))
        record = HistoricalEtfRecord(
            "510001", "平盘ETF", "SH", pd.Timestamp("2010-01-01"), "指数", "family", "a_large", "official"
        )
        qualification = pd.DataFrame(
            [
                {
                    "year": 2020,
                    "decision_date": dates[-1],
                    "symbol": record.symbol,
                    "name": record.name,
                    "direction": record.direction,
                    "tracked_index": record.tracked_index,
                    "proxy_ratio_pct": 0.0,
                    "qualified": True,
                }
            ]
        )
        preflight = AnnualQualificationResult(
            qualification=qualification,
            research_data={(2020, record.symbol): research},
        )
        settings = AnnualBacktestSettings(
            start_year=2020,
            end_date="2020-12-31",
            train_ratio=0.7,
            min_train_days=50,
            min_validation_days=20,
            ma_periods=(10,),
            threshold_pcts=(0.0,),
            cash_annual_rate=0.0,
        )
        selections, _parameters, _errors = build_annual_selections(
            [record], preflight, settings
        )
        self.assertEqual(len(selections), 1)
        self.assertTrue(selections[0].return_gate_relaxed)


    def test_seventy_thirty_split_enforces_validation_minimum_after_split(self):
        dates = pd.bdate_range("2019-01-01", periods=756)
        record = HistoricalEtfRecord(
            "510010", "样本ETF", "SH", pd.Timestamp("2010-01-01"), "指数", "family", "a_large", "official"
        )
        market = {"510010": make_market(dates, np.linspace(10, 20, len(dates)))}
        whitelist = {"directions": {"a_large": {"families": ["family"]}}}
        decision_year = dates[-1].year + 1
        settings = AnnualBacktestSettings(
            start_year=decision_year,
            end_date=f"{decision_year}-06-30",
        )
        result = preflight_annual_candidates([record], whitelist, market, settings)
        row = result.qualification.iloc[0]
        self.assertFalse(bool(row["qualified"]))
        self.assertIn("70/30拆分后", row["reason"])
        self.assertIn("验证226日", row["reason"])

    def test_registry_rejects_proxy_etf_that_tracks_a_different_index(self):
        records = [
            HistoricalEtfRecord(
                "510001", "老ETF", "SH", pd.Timestamp("2010-01-01"), "指数甲", "family", "a_large", "official"
            ),
            HistoricalEtfRecord(
                "510002",
                "新ETF",
                "SH",
                pd.Timestamp("2020-01-01"),
                "指数乙",
                "family",
                "a_large",
                "official",
                proxy_symbol="510001",
            ),
        ]
        whitelist = {"directions": {"a_large": {"families": ["family"]}}}
        checked = validate_registry_against_whitelist(records, whitelist).set_index("symbol")
        self.assertFalse(bool(checked.loc["510002", "registry_eligible"]))
        self.assertEqual(checked.loc["510002", "registry_reason"], "代理ETF跟踪指数不一致")


class AnnualEtfSimulationTests(unittest.TestCase):
    def setUp(self):
        self.symbols = {slot: f"1{index:05d}" for index, slot in enumerate(ALL_SLOTS, 1)}
        self.dates = pd.bdate_range("2019-11-01", "2020-01-10")
        self.market = {
            symbol: make_market(self.dates, np.linspace(80 + index, 120 + index, len(self.dates)))
            for index, symbol in enumerate(self.symbols.values())
        }
        self.market["512890"] = make_market(self.dates, np.linspace(5, 6, len(self.dates)))
        self.selections = [
            make_selection(
                2020,
                slot,
                symbol,
                score=2.0 if slot == "us_sp500" else 1.0,
            )
            for slot, symbol in self.symbols.items()
        ]
        self.settings = AnnualBacktestSettings(
            start_year=2020,
            end_date="2020-01-10",
            initial_capital=100000.0,
            commission_rate=0.0,
            lot_size=1,
            cash_annual_rate=0.0,
        )

    def test_initial_weights_total_one_hundred_and_us_rank_gets_25_15(self):
        _daily, _trades, _migrations, contribution = simulate_annual_portfolio(
            self.market, self.selections, self.settings
        )
        capital = contribution.set_index("slot")["initial_capital"]
        self.assertAlmostEqual(capital.sum(), 100000.0)
        self.assertAlmostEqual(capital["us_sp500"], 25000.0)
        self.assertAlmostEqual(capital["us_nasdaq"], 15000.0)
        for slot in set(ALL_SLOTS) - {"us_sp500", "us_nasdaq"}:
            self.assertAlmostEqual(capital[slot], 10000.0)

    def test_trades_use_hundred_share_lots_and_configured_commission(self):
        settings = AnnualBacktestSettings(
            start_year=2020,
            end_date="2020-01-10",
            initial_capital=500000.0,
            commission_rate=0.00006,
            lot_size=100,
            cash_annual_rate=0.0,
        )
        _daily, trades, _migrations, _contribution = simulate_annual_portfolio(
            self.market, self.selections, settings
        )
        buys = trades[trades["action"] == "buy"]
        self.assertFalse(buys.empty)
        self.assertTrue(((buys["shares"] % 100) == 0).all())
        np.testing.assert_allclose(
            buys["commission"], buys["gross_amount"] * settings.commission_rate
        )

    def test_us_long_half_is_bought_even_when_timing_signal_is_empty(self):
        flat_market = {
            symbol: make_market(self.dates, np.full(len(self.dates), 100.0))
            for symbol in self.symbols.values()
        }
        flat_market["512890"] = make_market(self.dates, np.full(len(self.dates), 5.0))
        _daily, trades, _migrations, _contribution = simulate_annual_portfolio(
            flat_market, self.selections, self.settings
        )
        us_buys = trades[
            trades["slot"].isin(["us_sp500", "us_nasdaq"])
            & (trades["action"] == "buy")
        ]
        self.assertEqual(set(us_buys["leg"]), {"long"})
        self.assertTrue(us_buys["reason"].str.contains("长期半仓").all())

    def test_next_close_pressure_delays_frozen_signal_execution_one_session(self):
        _daily, same_trades, _migrations, _contribution = simulate_annual_portfolio(
            self.market, self.selections, self.settings, execution_mode=EXECUTION_SAME_CLOSE
        )
        _daily, next_trades, _migrations, _contribution = simulate_annual_portfolio(
            self.market, self.selections, self.settings, execution_mode=EXECUTION_NEXT_CLOSE
        )
        self.assertLess(
            pd.Timestamp(same_trades["execution_date"].min()),
            pd.Timestamp(next_trades["execution_date"].min()),
        )

    def test_same_symbol_new_parameter_applies_on_first_session_of_year(self):
        dates = pd.bdate_range("2020-11-02", "2021-01-08")
        market = {}
        selections = []
        for index, slot in enumerate(ALL_SLOTS, 1):
            symbol = f"2{index:05d}"
            prices = np.linspace(100, 109, len(dates))
            if slot == "a_mid_small":
                first_2021_index = next(i for i, date in enumerate(dates) if date.year == 2021)
                prices[:first_2021_index] = np.linspace(100, 109, first_2021_index)
                prices[first_2021_index:] = np.linspace(108.5, 109.0, len(dates) - first_2021_index)
            market[symbol] = make_market(dates, prices)
            selections.append(make_selection(2020, slot, symbol, ma=10))
            if slot == "a_mid_small":
                selections.append(make_selection(2021, slot, symbol, ma=2))
        market["512890"] = make_market(dates, np.linspace(5, 6, len(dates)))
        settings = AnnualBacktestSettings(
            start_year=2020,
            end_date="2021-01-08",
            initial_capital=100000,
            commission_rate=0,
            lot_size=1,
            cash_annual_rate=0,
        )
        _daily, trades, _migrations, _contribution = simulate_annual_portfolio(
            market, selections, settings
        )
        first_2021 = min(date for date in dates if date.year == 2021)
        sells = trades[
            (trades["slot"] == "a_mid_small")
            & (trades["action"] == "sell")
            & (pd.to_datetime(trades["execution_date"]) == first_2021)
        ]
        self.assertEqual(len(sells), 1)

    def test_delayed_migration_uses_latest_target(self):
        dates = pd.bdate_range("2020-11-02", "2022-01-12")
        market = {}
        selections = []
        old_symbol = "300004"
        target_2021 = "400004"
        target_2022 = "500004"
        for index, slot in enumerate(ALL_SLOTS, 1):
            symbol = old_symbol if slot == "a_growth" else f"3{index:05d}"
            prices = np.linspace(100, 300, len(dates))
            if slot == "a_growth":
                first_2022_index = next(i for i, date in enumerate(dates) if date.year == 2022)
                prices[first_2022_index + 2 :] = np.linspace(
                    80, 90, len(prices) - first_2022_index - 2
                )
            market[symbol] = make_market(dates, prices)
            selections.append(make_selection(2020, slot, symbol, ma=3))
        market[target_2021] = make_market(dates, np.linspace(50, 120, len(dates)))
        market[target_2022] = make_market(dates, np.linspace(60, 140, len(dates)))
        market["512890"] = make_market(dates, np.linspace(5, 8, len(dates)))
        selections.extend(
            [
                make_selection(2021, "a_growth", target_2021, ma=3),
                make_selection(2022, "a_growth", target_2022, ma=3),
            ]
        )
        settings = AnnualBacktestSettings(
            start_year=2020,
            end_date="2022-01-12",
            initial_capital=100000,
            commission_rate=0,
            lot_size=1,
            cash_annual_rate=0,
        )
        _daily, trades, migrations, _contribution = simulate_annual_portfolio(
            market, selections, settings
        )
        migrated = migrations[migrations["slot"] == "a_growth"]
        self.assertEqual(migrated.iloc[-1]["new_symbol"], target_2022)
        self.assertFalse((trades["symbol"] == target_2021).any())
        first_2022 = min(date for date in dates if date.year == 2022)
        self.assertGreater(pd.Timestamp(migrated.iloc[-1]["exit_date"]), first_2022)

    def test_parking_benchmark_accrues_cash_before_listing(self):
        master_dates = pd.bdate_range("2019-01-02", "2019-01-25")
        parking_dates = master_dates[master_dates >= PARKING_LISTING_DATE]
        market = {"512890": make_market(parking_dates, np.full(len(parking_dates), 10.0))}
        settings = AnnualBacktestSettings(
            start_year=2019,
            initial_capital=100000,
            commission_rate=0,
            lot_size=100,
            cash_annual_rate=0.10,
        )
        benchmark = _simulate_parking_benchmark(market, master_dates, settings)
        before_listing = benchmark[benchmark["trade_date"] < PARKING_LISTING_DATE]
        self.assertGreater(before_listing.iloc[-1]["portfolio_value"], 100000.0)
        first_trade = benchmark[benchmark["trade_count"] == 1].iloc[0]
        self.assertEqual(pd.Timestamp(first_trade["trade_date"]), PARKING_LISTING_DATE)


class AnnualEtfCheckpointTests(unittest.TestCase):
    def test_fingerprint_changes_for_registry_whitelist_and_source_data(self):
        dates = pd.bdate_range("2020-01-01", periods=3)
        market = {"510001": make_market(dates, [10, 11, 12])}
        settings = AnnualBacktestSettings(start_year=2020, end_date="2020-12-31")
        record = HistoricalEtfRecord(
            "510001", "ETF", "SH", pd.Timestamp("2010-01-01"), "指数", "family", "a_large", "official"
        )
        base = data_fingerprint(
            market,
            settings,
            records=[record],
            whitelist={"version": "v1"},
        )
        changed_whitelist = data_fingerprint(
            market,
            settings,
            records=[record],
            whitelist={"version": "v2"},
        )
        changed_market = {"510001": make_market(dates, [10, 11, 13])}
        changed_source = data_fingerprint(
            changed_market,
            settings,
            records=[record],
            whitelist={"version": "v1"},
        )
        self.assertNotEqual(base, changed_whitelist)
        self.assertNotEqual(base, changed_source)

    def test_checkpoint_save_load_is_atomic_and_handles_empty_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AnnualCheckpointStore(directory, "fingerprint")
            frame = pd.DataFrame({"value": [1, 2]})
            store.save("stage", frame)
            pd.testing.assert_frame_equal(store.load("stage"), frame)
            store.save("empty", pd.DataFrame())
            self.assertTrue(store.load("empty").empty)
            self.assertTrue((Path(directory) / "fingerprint" / "manifest.json").exists())
            self.assertFalse(list((Path(directory) / "fingerprint").glob("*.tmp")))


    def test_complete_checkpoint_is_reused_before_preflight(self):
        dates = pd.bdate_range("2019-08-01", "2020-01-10")
        records = []
        market = {}
        directions = {}
        for index, slot in enumerate(ALL_SLOTS, 1):
            symbol = f"6{index:05d}"
            family = f"family_{slot}"
            records.append(
                HistoricalEtfRecord(
                    symbol,
                    f"ETF-{slot}",
                    "SH",
                    pd.Timestamp("2010-01-01"),
                    f"index_{slot}",
                    family,
                    slot,
                    "official",
                )
            )
            directions[slot] = {"families": [family]}
            market[symbol] = make_market(
                dates, np.linspace(10 + index, 20 + index, len(dates))
            )
        market["512890"] = make_market(dates, np.linspace(4, 5, len(dates)))
        whitelist = {"directions": directions}
        settings = AnnualBacktestSettings(
            start_year=2020,
            end_date="2020-01-10",
            initial_capital=100000,
            commission_rate=0,
            lot_size=1,
            cash_annual_rate=0,
            min_listing_days=1,
            min_turnover_days=1,
            train_ratio=0.7,
            min_train_days=50,
            min_validation_days=20,
            ma_periods=(3,),
            threshold_pcts=(0.0,),
        )
        with tempfile.TemporaryDirectory() as directory:
            first = run_annual_etf_backtest(
                records, whitelist, market, settings, checkpoint_dir=directory
            )
            with patch(
                "services.annual_etf_portfolio.preflight_annual_candidates",
                side_effect=AssertionError("完整检查点不应重新预检"),
            ):
                second = run_annual_etf_backtest(
                    records, whitelist, market, settings, checkpoint_dir=directory
                )
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(first.daily), len(second.daily))
        self.assertEqual(len(second.selections), len(ALL_SLOTS))


if __name__ == "__main__":
    unittest.main()
