from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from services import etf_ma_param_research as research


def _dates(count: int, start: str = "2019-01-02") -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=count)


def _random_walk(count: int, seed: int = 7, start: str = "2019-01-02", level: float = 1.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = level * np.cumprod(1 + rng.normal(0.0004, 0.015, count))
    return pd.DataFrame({"trade_date": _dates(count, start), "close": close})


SMALL_GRID = dict(ma_periods=(5, 10), fixed_thresholds=(0.0, 1.0), vol_multipliers=(0.0, 0.5), base_fractions=(0.0, 0.5, 1.0))
SPEC = research.SeriesSpec("TEST", "测试", "TEST", "同一基金", "x.csv", None, 10, 1.0)


# --- return attribution and simulation ---------------------------------------------------------


def test_first_buy_at_second_close_only_earns_following_return():
    dates = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    close = np.array([100.0, 110.0, 121.0])
    desired = np.array([0, 1, 1])
    nav = research.simulate_nav(dates, close, desired, 0, 2, capital=100.0, cash_rate=0.0, fee_rate=0.001)
    assert nav["value"].iloc[0] == pytest.approx(100.0)
    assert nav["value"].iloc[1] == pytest.approx(100.0 / 1.001)
    assert nav["value"].iloc[2] == pytest.approx(110.0 / 1.001)


def test_start_day_buy_only_pays_fee():
    dates = pd.to_datetime(["2026-01-05", "2026-01-06"])
    nav = research.simulate_nav(dates, np.array([50.0, 55.0]), np.array([1, 1]), 0, 1, capital=100.0, cash_rate=0.0, fee_rate=0.01)
    assert nav["value"].iloc[0] == pytest.approx(100.0 / 1.01)
    assert nav["value"].iloc[1] == pytest.approx(110.0 / 1.01)


def test_next_close_still_bears_price_change_before_selling():
    dates = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    close = np.array([100.0, 100.0, 80.0])
    desired = np.array([1, 0, 0])
    same = research.simulate_nav(dates, close, desired, 0, 2, capital=100.0, cash_rate=0.0, fee_rate=0.0)
    nxt = research.simulate_nav(
        dates, close, desired, 1, 2, capital=100.0, cash_rate=0.0, fee_rate=0.0, execution=research.EXECUTION_NEXT_CLOSE
    )
    assert same["value"].iloc[-1] == pytest.approx(100.0)
    # signal from day 0 (hold) executes at day 1 close, the day-1 sell signal executes at day 2 close after the drop
    assert nxt["value"].iloc[0] == pytest.approx(100.0)
    assert nxt["value"].iloc[1] == pytest.approx(80.0)


def test_cash_interest_uses_calendar_days():
    dates = pd.to_datetime(["2026-01-02", "2026-01-05"])  # Friday -> Monday
    nav = research.simulate_nav(dates, np.array([1.0, 1.0]), np.array([0, 0]), 0, 1, capital=100.0, cash_rate=0.0365)
    assert nav["value"].iloc[1] == pytest.approx(100.0 * 1.0365 ** (3 / 365))


def test_base_fraction_equals_two_independent_sub_accounts():
    frame = _random_walk(80)
    close = frame["close"].to_numpy()
    desired = research.desired_states(close, 5, 0.01)
    kwargs = dict(fee_rate=0.0005, cash_rate=0.015)
    mixed = research.simulate_nav(frame["trade_date"], close, desired, 10, 79, capital=1000.0, base_fraction=0.5, **kwargs)
    timing = research.simulate_nav(frame["trade_date"], close, desired, 10, 79, capital=500.0, **kwargs)
    hold = research.simulate_nav(frame["trade_date"], close, np.ones(80), 10, 79, capital=500.0, **kwargs)
    np.testing.assert_allclose(mixed["value"], timing["value"] + hold["value"])
    full_hold = research.simulate_nav(frame["trade_date"], close, desired, 10, 79, capital=1000.0, base_fraction=1.0, **kwargs)
    np.testing.assert_allclose(full_hold["value"], 2 * hold["value"])


def test_loop_simulation_matches_vectorized_reference():
    frame = _random_walk(300, seed=3)
    close = frame["close"].to_numpy()
    desired = research.desired_states(close, 20, 0.01)
    fee = 0.00006
    nav = research.simulate_nav(frame["trade_date"], close, desired, 50, 299, capital=1.0, fee_rate=fee, cash_rate=0.0)
    # vectorized: position held over day t is the state decided at close t-1
    window = slice(50, 300)
    returns = pd.Series(close).pct_change().to_numpy()[window]
    held = np.concatenate([[0], desired[50:299]])
    trades = np.abs(np.diff(np.concatenate([[0], desired[50:300]])))
    buy_cost = np.where((trades == 1) & (desired[50:300] == 1), 1 / (1 + fee), 1.0)
    sell_cost = np.where((trades == 1) & (desired[50:300] == 0), 1 - fee, 1.0)
    growth = np.where(held == 1, 1 + np.nan_to_num(returns), 1.0) * buy_cost * sell_cost
    np.testing.assert_allclose(nav["value"].to_numpy(), np.cumprod(growth), rtol=1e-10)


def test_index_level_series_can_be_traded():
    frame = _random_walk(200, level=1500.0)
    close = frame["close"].to_numpy()
    nav = research.simulate_nav(frame["trade_date"], close, np.ones(200), 0, 199, capital=100000.0, cash_rate=0.0, fee_rate=0.0)
    assert nav["value"].iloc[-1] == pytest.approx(100000.0 * close[-1] / close[0])


# --- signals --------------------------------------------------------------------------------------


def test_state_holds_inside_band():
    close = np.array([10, 10, 10, 10.5, 10.05, 10.0, 9.5, 9.95, 10.0])
    states = research.desired_states(close, 3, 0.02)
    assert states.tolist() == [0, 0, 0, 1, 1, 1, 0, 0, 0]


def test_vol_k_zero_equals_fixed_zero():
    frame = _random_walk(250)
    close = frame["close"].to_numpy()
    sigma = research.lagged_sigma(frame["trade_date"], close, 10)
    fixed = research.desired_states(close, 10, research.threshold_series(research.FAMILY_FIXED, 0.0, None))
    vol = research.desired_states(close, 10, research.threshold_series(research.FAMILY_VOL, 0.0, sigma))
    np.testing.assert_array_equal(fixed, vol)


@pytest.mark.parametrize("family,param", [(research.FAMILY_FIXED, 1.0), (research.FAMILY_VOL, 0.75)])
def test_future_data_does_not_change_past_signals(family, param):
    frame = _random_walk(300)
    close = frame["close"].to_numpy()
    altered = close.copy()
    altered[200:] *= np.linspace(0.5, 2.0, 100)

    def signals(prices):
        sigma = research.lagged_sigma(frame["trade_date"], prices, 20)
        return research.desired_states(prices, 20, research.threshold_series(family, param, sigma))

    np.testing.assert_array_equal(signals(close)[:200], signals(altered)[:200])


# --- metrics ---------------------------------------------------------------------------------------


def test_underwater_resets_when_touching_prior_peak():
    dates = pd.date_range("2026-01-01", periods=6)
    values = [100, 110, 100, 110, 100, 110]
    assert research.longest_underwater_days(values, dates, 100.0, dates[0]) == 2


def test_underwater_plateau_and_unrecovered_tail():
    dates = pd.date_range("2026-01-01", periods=7)
    assert research.longest_underwater_days([100, 100, 100], dates[:3], 100.0, dates[0]) == 0
    values = [100, 120, 120, 110, 120, 100, 105]
    episodes = research.drawdown_episodes(values, dates, 100.0, dates[0])
    assert [item["recovered"] for item in episodes] == [True, False]
    assert episodes[0]["underwater_days"] == 2  # peak re-dated on the plateau day 2 -> recovery day 4
    assert episodes[1]["underwater_days"] == 2  # peak day 4 -> period end day 6
    assert episodes[1]["recovery_days"] == 1


def test_recovery_stats_threshold_and_censoring():
    dates = pd.date_range("2026-01-01", periods=8)
    values = [100, 99, 100, 90, 95, 100, 101, 80]
    episodes = research.drawdown_episodes(values, dates, 100.0, dates[0])
    stats = research.recovery_stats(episodes, 0.05)
    # 1% dip filtered; 10% dip trough day 3 -> recovered day 5 (2 days); final 20.8% drop at last day -> 0 days
    assert stats["qualifying_drawdowns"] == 2
    assert stats["recovered_drawdowns"] == 1
    assert stats["unrecovered_drawdowns"] == 1
    assert stats["avg_recovery_days"] == pytest.approx(1.0)
    none = research.recovery_stats(research.drawdown_episodes([100, 101, 102], dates[:3], 100.0, dates[0]), 0.05)
    assert none["avg_recovery_days"] == 0.0 and none["no_qualifying_drawdown"]


# --- data ------------------------------------------------------------------------------------------


def test_splice_chains_index_returns_before_etf():
    index = pd.DataFrame({"trade_date": pd.to_datetime(["2024-04-11", "2024-04-12", "2024-04-15", "2024-04-16"]), "close": [3000.0, 3030.0, 3060.3, 3100.0]})
    etf = pd.DataFrame({"trade_date": pd.to_datetime(["2024-04-15", "2024-04-16"]), "close": [0.856, 0.852]})
    spliced = research.splice_index_before_etf(etf, index)
    assert spliced["trade_date"].tolist() == list(pd.to_datetime(["2024-04-11", "2024-04-12", "2024-04-15", "2024-04-16"]))
    returns = spliced["close"].pct_change()
    assert returns.iloc[1] == pytest.approx(0.01)
    assert returns.iloc[2] == pytest.approx(3060.3 / 3030.0 - 1)
    assert returns.iloc[3] == pytest.approx(0.852 / 0.856 - 1)
    assert spliced["source_kind"].tolist() == ["index_proxy", "index_proxy", "etf_adjusted", "etf_adjusted"]


def test_159552_spec_uses_etf_only():
    spec = next(item for item in research.SERIES_SPECS if item.key == "159552")
    assert spec.index_file is None and spec.kind == research.KIND_ETF


def test_window_respects_start_warmup_and_split_tiers():
    long = _random_walk(1900, start="2018-01-02")
    window = research.research_window(long, end="2026-09-30")
    assert window["eval_start"] >= pd.Timestamp("2019-09-30")
    assert window["ratio"] == 0.70
    short = _random_walk(600, start="2024-06-28")
    window = research.research_window(short, end="2026-09-30")
    assert window["start_idx"] == research.warmup_rows()
    assert window["ratio"] == 0.60
    assert window["n_is"] == int(np.floor((window["end_idx"] - window["start_idx"] + 1) * 0.60))
    assert research.split_ratio(5.0) == 0.70
    assert research.split_ratio(4.99) == 0.65
    assert research.split_ratio(3.0) == 0.65
    assert research.split_ratio(2.99) == 0.60


def test_data_after_end_does_not_change_results():
    frame = _random_walk(700, start="2023-06-01")
    end = frame["trade_date"].iloc[600]
    base = research.research_series(SPEC, frame.iloc[:601], end=end, **SMALL_GRID)
    extended = research.research_series(SPEC, frame, end=end, **SMALL_GRID)
    pd.testing.assert_frame_equal(base["candidates"], extended["candidates"])
    pd.testing.assert_frame_equal(base["base_allocation"], extended["base_allocation"])


# --- scoring ---------------------------------------------------------------------------------------


def test_composite_score_weights_and_directions():
    frame = pd.DataFrame(
        {
            "is_longest_underwater_days": [100, 200],
            "is_annual_return_pct": [5.0, 10.0],
            "is_max_drawdown_pct": [-10.0, -20.0],
            "is_avg_recovery_days": [30.0, 60.0],
        }
    )
    scores = research.composite_scores(frame, "is_")
    # row0 wins underwater(3), drawdown(2), recovery(2); row1 wins return(3)
    assert scores.iloc[0] == pytest.approx((3 * 1 + 3 * 0.5 + 2 * 1 + 2 * 1) / 10)
    assert scores.iloc[1] == pytest.approx((3 * 0.5 + 3 * 1 + 2 * 0.5 + 2 * 0.5) / 10)


def test_tie_break_order():
    base = {"is_score": 0.5, "is_annual_return_pct": 1.0, "is_max_drawdown_pct": -5.0, "is_longest_underwater_days": 10}
    frame = pd.DataFrame(
        [
            {**base, "ma_period": 20, "family": research.FAMILY_FIXED, "param": 1.0},
            {**base, "ma_period": 10, "family": research.FAMILY_VOL, "param": 0.5},
            {**base, "ma_period": 10, "family": research.FAMILY_FIXED, "param": 2.0},
            {**base, "ma_period": 10, "family": research.FAMILY_FIXED, "param": 1.5},
        ]
    )
    best = research.pick_best(frame, "is_score")
    assert (best["ma_period"], best["family"], best["param"]) == (10, research.FAMILY_FIXED, 1.5)


def test_grid_scoring_scopes_and_dedup():
    frame = _random_walk(500, start="2023-01-02")
    result = research.research_series(SPEC, frame, end=frame["trade_date"].iloc[-1], **SMALL_GRID)
    grid = result["candidates"]
    assert len(grid) == 8
    duplicates = grid[grid["duplicate_of_fixed_zero"]]
    assert len(duplicates) == 2 and duplicates["is_score_combined"].isna().all()
    assert grid["is_score_combined"].notna().sum() == 6
    for family in (research.FAMILY_FIXED, research.FAMILY_VOL):
        assert grid[grid["family"] == family]["is_score_family"].notna().sum() == 4
    mapped = research.combined_row(grid, duplicates.iloc[0])
    assert mapped["family"] == research.FAMILY_FIXED and mapped["param"] == 0
    assert np.isfinite(mapped["oos_rank_combined"])
    # duplicates inherit the fixed-0% percentiles
    for _, row in duplicates.iterrows():
        source = research.combined_row(grid, row)
        assert row["is_score_pct"] == source["is_score_pct"] and row["oos_score_pct"] == source["oos_score_pct"]
    base = result["base_allocation"]
    if result["recommendation"] is None:
        assert base.empty and not result["summary"]["suitable"]
    else:
        assert base["chosen"].sum() == 1 and result["summary"]["suitable"]


def test_common_oos_uses_same_window_and_series():
    etf = _random_walk(600, start="2024-01-02", seed=11)
    index = _random_walk(1200, start="2019-08-12", seed=12, level=3000.0)
    spliced_frame = research.splice_index_before_etf(etf, index[index["trade_date"] <= etf["trade_date"].iloc[0]])
    end = etf["trade_date"].iloc[-1]
    spliced_spec = research.SeriesSpec("S", "拼接", "159545", "代理拼接", "e", "i", 10, 1.0)
    etf_spec = research.SeriesSpec("E", "ETF", "159545", "ETF自身对照", "e", None, 10, 1.0)
    spliced = research.research_series(spliced_spec, spliced_frame, end=end, **SMALL_GRID)
    etf_only = research.research_series(etf_spec, etf, end=end, **SMALL_GRID)
    table = research.common_oos_comparison(spliced, etf_only)
    expected_start = max(spliced["context"].window["oos_start"], etf_only["context"].window["oos_start"])
    assert (table["start_date"] == expected_start).all()
    assert (table["end_date"] == end).all()
    assert set(table["source"]) <= {"买入持有", "指数拼接研究推荐", "ETF自身研究推荐", "现行参数（仅参考）"}
    assert {"买入持有", "现行参数（仅参考）"} <= set(table["source"])


# --- plateau ---------------------------------------------------------------------------------------

PLATEAU_MAS = (5, 10, 15, 20, 25)
PLATEAU_FIXED = (0.0, 1.0, 2.0, 3.0)
PLATEAU_VOL = (0.0, 0.5, 1.0, 1.5)


def _synthetic_grid(score_fn):
    rows = []
    for family, params in ((research.FAMILY_FIXED, PLATEAU_FIXED), (research.FAMILY_VOL, PLATEAU_VOL)):
        for i, ma in enumerate(PLATEAU_MAS):
            for j, param in enumerate(params):
                is_score, oos_score = score_fn(family, i, j)
                rows.append(
                    {
                        "family": family,
                        "ma_period": ma,
                        "param": param,
                        "label": research.param_label(family, ma, param),
                        "duplicate_of_fixed_zero": family == research.FAMILY_VOL and param == 0,
                        "is_score_combined": is_score,
                        "oos_score_combined": oos_score,
                    }
                )
    grid = pd.DataFrame(rows)
    grid.loc[grid["duplicate_of_fixed_zero"], ["is_score_combined", "oos_score_combined"]] = np.nan
    # synthetic scores double as the vs-buy-and-hold scores; buy-and-hold scores 0.5 in both segments
    grid["is_score_vs_hold"] = grid["is_score_combined"]
    grid["oos_score_vs_hold"] = grid["oos_score_combined"]
    grid["is_hold_score"] = 0.5
    grid["oos_hold_score"] = 0.5
    return research.add_plateau_scores(
        grid, ma_periods=PLATEAU_MAS, fixed_thresholds=PLATEAU_FIXED, vol_multipliers=PLATEAU_VOL
    )


def test_plateau_found_in_consistent_block():
    # fixed family cells with MA index 1..3 and threshold index 1..3 score high in both segments
    def score(family, i, j):
        high = family == research.FAMILY_FIXED and 1 <= i <= 3 and 1 <= j <= 3
        return (0.9 + 0.01 * (i == 2 and j == 2), 0.9) if high else (0.1, 0.1)

    grid = _synthetic_grid(score)
    plateau = grid[grid["plateau"]]
    # interior centre plus the right-edge cell whose 5 existing neighbours are all good
    assert set(zip(plateau["ma_period"], plateau["param"])) == {(15, 2.0), (15, 3.0)}
    best = research.recommend_parameters(grid)
    assert best["family"] == research.FAMILY_FIXED and (best["ma_period"], best["param"]) in {(15, 2.0), (15, 3.0)}


def test_corner_cells_cannot_form_plateau():
    def score(family, i, j):
        return (0.9, 0.9) if family == research.FAMILY_FIXED and i <= 1 and j <= 1 else (0.1, 0.1)

    grid = _synthetic_grid(score)
    assert not grid["plateau"].any()


def test_isolated_peak_is_not_a_plateau():
    # the peak's direct neighbours are the worst cells; far-away cells fill the top half
    def score(family, i, j):
        if family == research.FAMILY_FIXED and (i, j) == (2, 2):
            return 1.0, 1.0
        distance = max(abs(i - 2), abs(j - 2)) + (0 if family == research.FAMILY_FIXED else 3)
        return 0.1 * distance, 0.1 * distance

    grid = _synthetic_grid(score)
    center = grid[(grid["family"] == research.FAMILY_FIXED) & (grid["ma_period"] == 15) & (grid["param"] == 2.0)].iloc[0]
    assert center["good"] and center["neighbor_good_share"] == 0 and not center["plateau"]
    best = research.recommend_parameters(grid)
    assert best is None or best["family"] == research.FAMILY_VOL


def test_no_plateau_means_unsuitable():
    def score(family, i, j):
        # good cells only on a checkerboard: no cell has 75% good neighbours
        good = (i + j) % 2 == 0
        return (0.9, 0.9) if good else (0.1, 0.1)

    grid = _synthetic_grid(score)
    assert not grid["plateau"].any()
    assert research.recommend_parameters(grid) is None


def test_good_requires_both_segments():
    def score(family, i, j):
        return (0.9, 0.1) if family == research.FAMILY_FIXED else (0.1, 0.9)

    grid = _synthetic_grid(score)
    assert not grid["good"].any()


def test_base_fraction_uses_in_and_out_of_sample_average():
    frame = _random_walk(500, start="2023-01-02", seed=5)
    context = research.build_context(SPEC, frame, end=frame["trade_date"].iloc[-1], ma_periods=(5, 10))
    choice = pd.Series({"family": research.FAMILY_FIXED, "ma_period": 10, "param": 1.0, "label": "MA10 / 1%"})
    base = research.evaluate_base_fractions(context, choice, (0.0, 0.5, 1.0))
    expected = (base["is_score_base"] + base["oos_score_base"]) / 2
    np.testing.assert_allclose(base["robust_score_base"], expected)
    chosen = base[base["chosen"]].iloc[0]
    assert chosen["robust_score_base"] == base["robust_score_base"].max()
    assert {"full_annual_return_pct", "full_max_drawdown_pct"} <= set(base.columns)


def test_good_means_beating_buy_and_hold_in_both_segments():
    frame = _random_walk(500, start="2023-01-02", seed=9)
    result = research.research_series(SPEC, frame, end=frame["trade_date"].iloc[-1], **SMALL_GRID)
    grid = result["candidates"]
    unique = grid[~grid["duplicate_of_fixed_zero"]]
    expected = (unique["is_score_vs_hold"] > unique["is_hold_score"]) & (unique["oos_score_vs_hold"] > unique["oos_hold_score"])
    pd.testing.assert_series_equal(unique["good"], expected, check_names=False)
    # the hold benchmark is scored inside the same pool, so scores stay within (0, 1]
    assert unique["is_score_vs_hold"].between(0, 1).all() and 0 < unique["is_hold_score"].iloc[0] <= 1
