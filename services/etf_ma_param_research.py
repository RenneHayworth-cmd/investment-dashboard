"""Offline MA timing parameter research on adjusted ETF and proxy index series.

Pure computation: reads local CSV files, never touches the network, the cache
database or any position configuration. Results are a simulation on adjusted
(or proxy index) close series with fractional units, not a real-share backtest.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from services.dynamic_threshold_research import calculate_lagged_sigma

DEFAULT_DATA_DIR = Path("/mnt/c/Users/78224/Desktop/Project/ETF均线择时策略")
RESEARCH_START = pd.Timestamp("2019-09-30")
RESEARCH_END = pd.Timestamp("2026-09-30")

MA_PERIODS: tuple[int, ...] = (5, 10, 15, 20, 25, 30, 40, 50, 60)
FIXED_THRESHOLDS_PCT: tuple[float, ...] = (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0)
VOL_MULTIPLIERS: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5)
BASE_FRACTIONS: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)
SIGMA_PERIOD = 60

FEE_RATE = 0.00006
CASH_ANNUAL_RATE = 0.015
INITIAL_CAPITAL = 100000.0
MIN_RECOVERY_DEPTH = 0.02
RECOVERY_VOL_FRACTION = 1 / 3
TRADING_DAYS_PER_YEAR = 252
PEAK_TOLERANCE = 1e-10
# Corner cells (3 neighbours) carry too little evidence to form a plateau.
PLATEAU_MIN_NEIGHBORS = 5

FAMILY_FIXED = "fixed"
FAMILY_VOL = "vol"
FAMILY_ORDER = {FAMILY_FIXED: 0, FAMILY_VOL: 1}
FAMILY_LABELS = {FAMILY_FIXED: "固定阈值", FAMILY_VOL: "波动率阈值"}

EXECUTION_SAME_CLOSE = "same_close"
EXECUTION_NEXT_CLOSE = "next_close"

# metric -> (weight, higher_is_better)
SCORE_METRICS: dict[str, tuple[float, bool]] = {
    "longest_underwater_days": (3.0, False),
    "annual_return_pct": (3.0, True),
    "max_drawdown_pct": (2.0, True),
    "avg_recovery_days": (2.0, False),
}

KIND_ETF = "etf_adjusted"
KIND_INDEX = "index_proxy"
KIND_SPLICED = "spliced"


@dataclass(frozen=True)
class PlateauRule:
    """A rule is good when its 3/3/2/2 score beats buy-and-hold in both segments; it is on a
    plateau when it is good and at least ``neighbor_share`` of its (>= ``min_neighbors``)
    adjacent grid cells are good as well."""

    neighbor_share: float = 0.75
    min_neighbors: int = PLATEAU_MIN_NEIGHBORS

    @property
    def label(self) -> str:
        return f"样本内外均优于买入持有 · 相邻好参数≥{self.neighbor_share:.0%}"


DEFAULT_PLATEAU_RULE = PlateauRule()


@dataclass(frozen=True)
class SeriesSpec:
    key: str
    name: str
    holding_code: str | None
    relation: str
    etf_file: str | None = None
    index_file: str | None = None
    current_ma: int | None = None
    current_threshold_pct: float | None = None
    current_base_fraction: float = 0.0

    @property
    def kind(self) -> str:
        if self.etf_file and self.index_file:
            return KIND_SPLICED
        if self.index_file:
            return KIND_INDEX
        return KIND_ETF


HSHY_INDEX_FILE = "恒生港股通高息低波_成立来_2019-08-12_2026-09-30.csv"
HSHY_ETF_FILE = "恒生红利低波ETF易方达_原始数据.csv"

SERIES_SPECS: tuple[SeriesSpec, ...] = (
    SeriesSpec("510500", "中证500ETF南方", "510500", "同一基金", "中证500ETF南方_原始数据.csv", None, 15, 1.0),
    SeriesSpec("159915", "创业板ETF易方达", "159915", "同一基金", "创业板ETF易方达_原始数据.csv", None, 20, 1.0),
    SeriesSpec("159967", "创业板成长ETF华夏", "159967", "同一基金", "创业板成长ETF华夏_原始数据.csv", None, 25, 2.0),
    SeriesSpec("588000", "科创50ETF华夏", "588000", "同一基金", "科创50ETF华夏_原始数据.csv", None, 20, 1.0),
    SeriesSpec("161128", "标普信息科技LOF易方达", "161128", "同一基金", "标普信息科技LOF易方达_原始数据.csv", None, 25, 1.5),
    SeriesSpec("513310", "中韩半导体ETF华泰柏瑞", "513310", "同一基金", "中韩半导体ETF华泰柏瑞_原始数据.csv", None, 15, 0.5),
    SeriesSpec("513880", "日经225ETF华安", "513880", "同一基金", "日经225ETF华安_原始数据.csv", None, 10, 2.0),
    SeriesSpec("159552", "中证2000增强ETF招商", "159552", "同一基金", "中证2000增强ETF招商_原始数据.csv", None, 10, 2.5),
    SeriesSpec("513130", "恒生科技ETF华泰柏瑞", "513260", "同指数的另一只基金", "恒生科技ETF华泰柏瑞_原始数据.csv", None, 20, 1.0),
    SeriesSpec("513500", "标普500ETF博时", "159655", "同指数的另一只基金", "标普500ETF博时_原始数据.csv", None, 25, 2.0, 0.5),
    SeriesSpec("159941", "纳指ETF广发", "159501", "同指数的另一只基金", "纳指ETF广发_原始数据.csv", None, 25, 2.0, 0.5),
    SeriesSpec("518800", "黄金ETF国泰", "518850", "同标的的另一只基金", "黄金ETF国泰_原始数据.csv", None, 30, 1.5),
    SeriesSpec("159545", "恒生红利低波（指数拼接）", "159545", "代理拼接", HSHY_ETF_FILE, HSHY_INDEX_FILE, 10, 1.0),
    SeriesSpec("159545_ETF", "恒生红利低波ETF易方达（ETF自身）", "159545", "ETF自身对照", HSHY_ETF_FILE, None, 10, 1.0),
    SeriesSpec("980092", "国证自由现金流指数", "159201", "指数代理", None, "国证自由现金流_成立来_2012-12-31_2026-09-30.csv", 20, 0.5),
    SeriesSpec("512890", "红利低波ETF华泰柏瑞", None, "仅作研究", "红利低波ETF华泰柏瑞_原始数据.csv"),
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def read_close_csv(path: str | Path) -> pd.DataFrame:
    raw = pd.read_csv(path, encoding="utf-8-sig")
    frame = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(raw["日期"], errors="coerce").dt.normalize(),
            "close": pd.to_numeric(raw["收盘价"], errors="coerce"),
        }
    )
    frame = frame.dropna()
    frame = frame[frame["close"] > 0]
    return frame.sort_values("trade_date").drop_duplicates("trade_date", keep="last").reset_index(drop=True)


def truncate(frame: pd.DataFrame, end: pd.Timestamp | str = RESEARCH_END) -> pd.DataFrame:
    return frame[frame["trade_date"] <= pd.Timestamp(end)].reset_index(drop=True)


def splice_index_before_etf(etf: pd.DataFrame, index: pd.DataFrame) -> pd.DataFrame:
    """Chain index daily returns before the ETF's first date onto the ETF price level."""
    if etf.empty:
        raise ValueError("拼接需要ETF数据。")
    first_date = pd.Timestamp(etf["trade_date"].iloc[0])
    anchor_rows = index[index["trade_date"] <= first_date]
    if anchor_rows.empty:
        raise ValueError("指数数据晚于ETF首日，无法拼接。")
    anchor_close = float(anchor_rows["close"].iloc[-1])
    scale = float(etf["close"].iloc[0]) / anchor_close
    before = index[index["trade_date"] < first_date].copy()
    before["close"] = before["close"] * scale
    before["source_kind"] = KIND_INDEX
    after = etf.copy()
    after["source_kind"] = KIND_ETF
    return pd.concat([before, after], ignore_index=True)


def load_series(spec: SeriesSpec, data_dir: str | Path, end: pd.Timestamp | str = RESEARCH_END) -> pd.DataFrame:
    data_dir = Path(data_dir)
    etf = truncate(read_close_csv(data_dir / spec.etf_file), end) if spec.etf_file else None
    index = truncate(read_close_csv(data_dir / spec.index_file), end) if spec.index_file else None
    if etf is not None and index is not None:
        return splice_index_before_etf(etf, index)
    frame = etf if etf is not None else index
    if frame is None:
        raise ValueError(f"{spec.key} 未配置数据文件。")
    frame = frame.copy()
    frame["source_kind"] = KIND_ETF if etf is not None else KIND_INDEX
    return frame


# ---------------------------------------------------------------------------
# Window and split
# ---------------------------------------------------------------------------


def warmup_rows(ma_periods: Iterable[int] = MA_PERIODS, sigma_period: int = SIGMA_PERIOD) -> int:
    """Index of the first row where the longest MA's lagged deviation sigma exists."""
    return int(max(ma_periods)) + int(sigma_period) - 1


def split_ratio(years: float) -> float:
    if years >= 5:
        return 0.70
    if years >= 3:
        return 0.65
    return 0.60


def research_window(
    frame: pd.DataFrame,
    start: pd.Timestamp | str = RESEARCH_START,
    end: pd.Timestamp | str = RESEARCH_END,
    *,
    ma_periods: Iterable[int] = MA_PERIODS,
    sigma_period: int = SIGMA_PERIOD,
) -> dict[str, object]:
    dates = pd.to_datetime(truncate(frame, end)["trade_date"]).reset_index(drop=True)
    warm = warmup_rows(ma_periods, sigma_period)
    on_or_after = np.flatnonzero((dates >= pd.Timestamp(start)).to_numpy())
    if not len(on_or_after):
        raise ValueError("研究起点之后没有数据。")
    start_idx = max(warm, int(on_or_after[0]))
    end_idx = len(dates) - 1
    if start_idx >= end_idx:
        raise ValueError("预热后剩余数据不足。")
    eval_start = pd.Timestamp(dates.iloc[start_idx])
    eval_end = pd.Timestamp(dates.iloc[end_idx])
    years = (eval_end - eval_start).days / 365.25
    ratio = split_ratio(years)
    total = end_idx - start_idx + 1
    n_is = int(floor(total * ratio))
    is_end_idx = start_idx + n_is - 1
    return {
        "start_idx": start_idx,
        "is_end_idx": is_end_idx,
        "oos_start_idx": is_end_idx + 1,
        "end_idx": end_idx,
        "eval_start": eval_start,
        "is_end": pd.Timestamp(dates.iloc[is_end_idx]),
        "oos_start": pd.Timestamp(dates.iloc[is_end_idx + 1]),
        "eval_end": eval_end,
        "years": years,
        "ratio": ratio,
        "n_is": n_is,
        "n_oos": total - n_is,
    }


# ---------------------------------------------------------------------------
# Signals and simulation
# ---------------------------------------------------------------------------


def desired_states(close: np.ndarray | pd.Series, ma_period: int, threshold: float | np.ndarray) -> np.ndarray:
    """Hysteresis state: hold above MA*(1+t), exit below MA*(1-t), else keep previous state.

    ``threshold`` is a fraction (0.01 = 1%), scalar or per-day array. Days where the
    MA or threshold is unavailable keep the previous state (initially empty).
    """
    prices = np.asarray(close, dtype=float)
    ma = pd.Series(prices).rolling(int(ma_period), min_periods=int(ma_period)).mean().to_numpy()
    thresholds = np.broadcast_to(np.asarray(threshold, dtype=float), prices.shape)
    states = np.zeros(len(prices), dtype=np.int8)
    state = 0
    for i in range(len(prices)):
        average = ma[i]
        band = thresholds[i]
        if np.isfinite(average) and np.isfinite(band):
            price = prices[i]
            if price > average * (1 + band):
                state = 1
            elif price < average * (1 - band):
                state = 0
        states[i] = state
    return states


def lagged_sigma(dates: pd.Series, close: np.ndarray, ma_period: int, sigma_period: int = SIGMA_PERIOD) -> np.ndarray:
    market = pd.DataFrame({"trade_date": pd.to_datetime(dates).to_numpy(), "signal_close": np.asarray(close, dtype=float)})
    sigma = calculate_lagged_sigma(market, int(ma_period), int(sigma_period))
    aligned = sigma.set_index("trade_date")["sigma_prev"].reindex(pd.to_datetime(market["trade_date"]))
    return aligned.to_numpy(dtype=float)


def threshold_series(family: str, param: float, sigma: np.ndarray | None) -> float | np.ndarray:
    if family == FAMILY_FIXED:
        return float(param) / 100
    if float(param) == 0:
        return 0.0
    if sigma is None:
        raise ValueError("波动率阈值需要sigma序列。")
    return float(param) * sigma


def simulate_nav(
    dates: pd.Series | np.ndarray,
    close: np.ndarray,
    desired: np.ndarray,
    start_idx: int,
    end_idx: int,
    *,
    capital: float = INITIAL_CAPITAL,
    fee_rate: float = FEE_RATE,
    cash_rate: float = CASH_ANNUAL_RATE,
    execution: str = EXECUTION_SAME_CLOSE,
    base_fraction: float = 0.0,
) -> pd.DataFrame:
    """Simulate a two-sub-account NAV over ``[start_idx, end_idx]``.

    The base sub-account buys at the first close and holds; the timing sub-account
    starts in cash. Day-t price change accrues to positions held after the t-1 close;
    trades happen at the close (same day or one day later) and only affect later days.
    Units are fractional; cash accrues interest by calendar days.
    """
    day_index = pd.DatetimeIndex(pd.to_datetime(np.asarray(dates)))
    prices = np.asarray(close, dtype=float)
    signals = np.asarray(desired)
    if execution not in (EXECUTION_SAME_CLOSE, EXECUTION_NEXT_CLOSE):
        raise ValueError(f"未知成交方式：{execution}")
    base_fraction = float(base_fraction)
    cash = float(capital) * (1 - base_fraction)
    units = 0.0
    base_units = float(capital) * base_fraction / (prices[start_idx] * (1 + fee_rate)) if base_fraction > 0 else 0.0
    values = np.empty(end_idx - start_idx + 1)
    for offset, i in enumerate(range(start_idx, end_idx + 1)):
        if offset and cash > 0 and cash_rate:
            days = (day_index[i] - day_index[i - 1]).days
            cash *= (1 + cash_rate) ** (days / 365)
        if execution == EXECUTION_SAME_CLOSE:
            signal = signals[i]
        else:
            signal = signals[i - 1] if i > 0 else 0
        price = prices[i]
        if signal and units == 0 and cash > 0:
            units = cash / (price * (1 + fee_rate))
            cash = 0.0
        elif not signal and units > 0:
            cash = units * price * (1 - fee_rate)
            units = 0.0
        values[offset] = cash + (units + base_units) * price
    return pd.DataFrame({"trade_date": day_index[start_idx : end_idx + 1], "value": values})


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def drawdown_episodes(
    values: Iterable[float],
    dates: Iterable[pd.Timestamp],
    initial_value: float,
    initial_date: pd.Timestamp,
) -> list[dict[str, object]]:
    """Split a NAV path into underwater episodes.

    A value at or above the running peak (relative tolerance 1e-10) ends any open
    episode and resets the peak date, so touching the prior high counts as recovered.
    """
    peak = float(initial_value)
    peak_date = pd.Timestamp(initial_date)
    episodes: list[dict[str, object]] = []
    open_episode: dict[str, object] | None = None
    last_date = pd.Timestamp(initial_date)
    for date, value in zip(dates, values):
        date = pd.Timestamp(date)
        value = float(value)
        last_date = date
        if not np.isfinite(value):
            continue
        if value >= peak * (1 - PEAK_TOLERANCE):
            if open_episode is not None:
                open_episode.update({"end_date": date, "recovered": True})
                episodes.append(open_episode)
                open_episode = None
            peak = max(peak, value)
            peak_date = date
        elif open_episode is None:
            open_episode = {"peak_date": peak_date, "peak_value": peak, "trough_date": date, "trough_value": value}
        elif value < float(open_episode["trough_value"]):
            open_episode.update({"trough_date": date, "trough_value": value})
    if open_episode is not None:
        open_episode.update({"end_date": last_date, "recovered": False})
        episodes.append(open_episode)
    for episode in episodes:
        episode["depth"] = float(episode["trough_value"]) / float(episode["peak_value"]) - 1
        episode["underwater_days"] = int((episode["end_date"] - episode["peak_date"]).days)
        episode["recovery_days"] = int((episode["end_date"] - episode["trough_date"]).days)
    return episodes


def longest_underwater_days(
    values: Iterable[float],
    dates: Iterable[pd.Timestamp],
    initial_value: float,
    initial_date: pd.Timestamp,
) -> int:
    episodes = drawdown_episodes(values, dates, initial_value, initial_date)
    return max((int(item["underwater_days"]) for item in episodes), default=0)


def recovery_stats(episodes: list[dict[str, object]], min_depth: float) -> dict[str, object]:
    """Average trough-to-recovery days for episodes at least ``min_depth`` deep.

    Unrecovered episodes count up to the period end, so a trough on the last day
    contributes 0 days; counts are reported to disclose this censoring.
    """
    qualifying = [item for item in episodes if float(item["depth"]) <= -float(min_depth) + 1e-12]
    recovered = [item for item in qualifying if item["recovered"]]
    return {
        "avg_recovery_days": float(np.mean([item["recovery_days"] for item in qualifying])) if qualifying else 0.0,
        "qualifying_drawdowns": len(qualifying),
        "recovered_drawdowns": len(recovered),
        "unrecovered_drawdowns": len(qualifying) - len(recovered),
        "no_qualifying_drawdown": not qualifying,
    }


def period_metrics(nav: pd.DataFrame, capital: float, min_depth: float) -> dict[str, object]:
    dates = pd.to_datetime(nav["trade_date"]).reset_index(drop=True)
    values = nav["value"].to_numpy(dtype=float)
    start_date = pd.Timestamp(dates.iloc[0])
    seeded = np.concatenate([[float(capital)], values])
    running_peak = np.maximum.accumulate(seeded)
    total_return = values[-1] / capital - 1
    elapsed = (pd.Timestamp(dates.iloc[-1]) - start_date).days
    annual_return = (1 + total_return) ** (365 / elapsed) - 1 if elapsed > 0 and total_return > -1 else total_return
    episodes = drawdown_episodes(values, dates, capital, start_date)
    return {
        "start_date": start_date,
        "end_date": pd.Timestamp(dates.iloc[-1]),
        "trading_days": int(len(values)),
        "total_return_pct": total_return * 100,
        "annual_return_pct": annual_return * 100,
        "max_drawdown_pct": float((seeded / running_peak - 1).min() * 100),
        "longest_underwater_days": max((int(item["underwater_days"]) for item in episodes), default=0),
        "end_drawdown_pct": float((values[-1] / running_peak[-1] - 1) * 100),
        **recovery_stats(episodes, min_depth),
    }


def buy_hold_volatility(close: np.ndarray, start_idx: int, end_idx: int) -> float:
    returns = pd.Series(np.asarray(close, dtype=float)[start_idx : end_idx + 1]).pct_change().dropna()
    return float(returns.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR)) if len(returns) > 1 else 0.0


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def composite_scores(frame: pd.DataFrame, prefix: str = "") -> pd.Series:
    """3/3/2/2 weighted percentile score within ``frame`` (full precision)."""
    if frame.empty:
        return pd.Series(dtype=float)
    total = pd.Series(0.0, index=frame.index)
    weight_sum = 0.0
    for metric, (weight, higher_is_better) in SCORE_METRICS.items():
        ranks = frame[f"{prefix}{metric}"].rank(ascending=higher_is_better, pct=True, method="average")
        total = total + ranks * weight
        weight_sum += weight
    return total / weight_sum


def pick_best(frame: pd.DataFrame, score_column: str, prefix: str = "is_") -> pd.Series:
    ordered = frame.assign(_family_order=frame["family"].map(FAMILY_ORDER)).sort_values(
        [
            score_column,
            f"{prefix}annual_return_pct",
            f"{prefix}max_drawdown_pct",
            f"{prefix}longest_underwater_days",
            "ma_period",
            "_family_order",
            "param",
        ],
        ascending=[False, False, False, True, True, True, True],
        kind="mergesort",
    )
    return ordered.iloc[0]


def pick_base(frame: pd.DataFrame, score_column: str) -> pd.Series:
    ordered = frame.sort_values([score_column, "base_fraction"], ascending=[False, True], kind="mergesort")
    return ordered.iloc[0]


def param_label(family: str, ma_period: int, param: float) -> str:
    if family == FAMILY_FIXED:
        return f"MA{int(ma_period)} / {float(param):g}%"
    return f"MA{int(ma_period)} / {float(param):g}σ"


# ---------------------------------------------------------------------------
# Research per series
# ---------------------------------------------------------------------------


@dataclass
class SeriesContext:
    spec: SeriesSpec
    frame: pd.DataFrame
    dates: pd.Series
    close: np.ndarray
    window: dict[str, object]
    min_depth: float
    sigma_cache: dict[int, np.ndarray]

    def sigma(self, ma_period: int) -> np.ndarray:
        if ma_period not in self.sigma_cache:
            self.sigma_cache[ma_period] = lagged_sigma(self.dates, self.close, ma_period)
        return self.sigma_cache[ma_period]

    def desired(self, family: str, ma_period: int, param: float) -> np.ndarray:
        sigma = self.sigma(ma_period) if family == FAMILY_VOL and float(param) != 0 else None
        return desired_states(self.close, ma_period, threshold_series(family, param, sigma))

    def run(
        self,
        desired: np.ndarray,
        segment: str,
        *,
        base_fraction: float = 0.0,
        execution: str = EXECUTION_SAME_CLOSE,
        start_idx: int | None = None,
    ) -> dict[str, object]:
        window = self.window
        if segment == "is":
            first, last = int(window["start_idx"]), int(window["is_end_idx"])
        elif segment == "oos":
            first, last = int(window["oos_start_idx"]), int(window["end_idx"])
        elif segment == "full":
            first, last = int(window["start_idx"]), int(window["end_idx"])
        else:
            first, last = int(start_idx), int(window["end_idx"])
        nav = simulate_nav(
            self.dates, self.close, desired, first, last, execution=execution, base_fraction=base_fraction
        )
        return period_metrics(nav, INITIAL_CAPITAL, self.min_depth)


def build_context(
    spec: SeriesSpec,
    frame: pd.DataFrame,
    *,
    start: pd.Timestamp | str = RESEARCH_START,
    end: pd.Timestamp | str = RESEARCH_END,
    ma_periods: Iterable[int] = MA_PERIODS,
) -> SeriesContext:
    frame = truncate(frame, end)
    window = research_window(frame, start, end, ma_periods=ma_periods)
    close = frame["close"].to_numpy(dtype=float)
    volatility = buy_hold_volatility(close, int(window["start_idx"]), int(window["is_end_idx"]))
    window["is_buy_hold_volatility_pct"] = volatility * 100
    min_depth = max(MIN_RECOVERY_DEPTH, volatility * RECOVERY_VOL_FRACTION)
    return SeriesContext(spec, frame, frame["trade_date"], close, window, min_depth, {})


def _prefixed(metrics: dict[str, object], prefix: str) -> dict[str, object]:
    return {f"{prefix}{key}": value for key, value in metrics.items()}


def evaluate_grid(
    context: SeriesContext,
    *,
    ma_periods: Iterable[int] = MA_PERIODS,
    fixed_thresholds: Iterable[float] = FIXED_THRESHOLDS_PCT,
    vol_multipliers: Iterable[float] = VOL_MULTIPLIERS,
) -> pd.DataFrame:
    rows = []
    for ma_period in ma_periods:
        for family, params in ((FAMILY_FIXED, fixed_thresholds), (FAMILY_VOL, vol_multipliers)):
            for param in params:
                desired = context.desired(family, int(ma_period), float(param))
                rows.append(
                    {
                        "series": context.spec.key,
                        "family": family,
                        "ma_period": int(ma_period),
                        "param": float(param),
                        "label": param_label(family, int(ma_period), float(param))
                        + ("（=0%）" if family == FAMILY_VOL and float(param) == 0 else ""),
                        # vol k=0 is the same rule as fixed 0%; excluded from the combined ranking.
                        "duplicate_of_fixed_zero": family == FAMILY_VOL and float(param) == 0,
                        **_prefixed(context.run(desired, "is"), "is_"),
                        **_prefixed(context.run(desired, "oos"), "oos_"),
                    }
                )
    grid = pd.DataFrame(rows)
    combined = ~grid["duplicate_of_fixed_zero"]
    grid["is_score_combined"] = composite_scores(grid[combined], "is_")
    grid["oos_score_combined"] = composite_scores(grid[combined], "oos_")
    for family in (FAMILY_FIXED, FAMILY_VOL):
        mask = grid["family"] == family
        grid.loc[mask, "is_score_family"] = composite_scores(grid[mask], "is_")
    hold = np.ones(len(context.close), dtype=np.int8)
    for segment in ("is", "oos"):
        hold_metrics = context.run(hold, segment, base_fraction=1.0)
        columns = [f"{segment}_{metric}" for metric in SCORE_METRICS]
        pool = pd.concat(
            [grid.loc[combined, columns], pd.DataFrame([{f"{segment}_{metric}": hold_metrics[metric] for metric in SCORE_METRICS}], index=["hold"])]
        )
        scores = composite_scores(pool, f"{segment}_")
        grid.loc[combined, f"{segment}_score_vs_hold"] = scores.drop("hold").astype(float)
        grid[f"{segment}_hold_score"] = float(scores.loc["hold"])
    grid["is_rank_combined"] = grid["is_score_combined"].rank(ascending=False, method="min")
    grid["oos_rank_combined"] = grid["oos_score_combined"].rank(ascending=False, method="min")
    return grid


def combined_row(grid: pd.DataFrame, choice: pd.Series) -> pd.Series:
    """Row carrying the combined-ranking scores for ``choice`` (vol k=0 maps to fixed 0%)."""
    if not bool(choice["duplicate_of_fixed_zero"]):
        return choice
    match = grid[(grid["family"] == FAMILY_FIXED) & (grid["ma_period"] == choice["ma_period"]) & (grid["param"] == 0)]
    return match.iloc[0]


def in_sample_best(grid: pd.DataFrame) -> pd.Series:
    """Diagnostic only: the single best in-sample rule of the deduplicated 117."""
    return pick_best(grid[~grid["duplicate_of_fixed_zero"]], "is_score_combined")


def add_plateau_scores(
    grid: pd.DataFrame,
    *,
    ma_periods: Iterable[int] = MA_PERIODS,
    fixed_thresholds: Iterable[float] = FIXED_THRESHOLDS_PCT,
    vol_multipliers: Iterable[float] = VOL_MULTIPLIERS,
    rule: PlateauRule = DEFAULT_PLATEAU_RULE,
) -> pd.DataFrame:
    """Combine in- and out-of-sample scores and flag parameter plateaus.

    A rule is good when its 3/3/2/2 score, ranked together with buy-and-hold, beats
    buy-and-hold in both segments. ``rule`` decides which good rules sit on a plateau
    within the family's MA x threshold grid (3x3 neighbourhood). The robust score used
    to rank plateau cells averages each segment's percentile among the deduplicated rules.
    """
    scored = grid.copy()
    unique = ~scored["duplicate_of_fixed_zero"]
    for segment in ("is", "oos"):
        scored[f"{segment}_score_pct"] = scored.loc[unique, f"{segment}_score_combined"].rank(pct=True, method="average")
    for index in scored.index[scored["duplicate_of_fixed_zero"]]:
        source = combined_row(scored, scored.loc[index])
        columns = ["is_score_pct", "oos_score_pct", "is_score_vs_hold", "oos_score_vs_hold"]
        scored.loc[index, columns] = [source[column] for column in columns]
    scored["robust_score"] = (scored["is_score_pct"] + scored["oos_score_pct"]) / 2
    scored["beats_hold_is"] = scored["is_score_vs_hold"] > scored["is_hold_score"]
    scored["beats_hold_oos"] = scored["oos_score_vs_hold"] > scored["oos_hold_score"]
    scored["good"] = scored["beats_hold_is"] & scored["beats_hold_oos"]
    good = scored["good"].to_numpy(dtype=bool)
    robust = scored["robust_score"].to_numpy(dtype=float)
    neighbor_count = np.zeros(len(scored), dtype=int)
    neighbor_share = np.full(len(scored), np.nan)
    plateau = np.zeros(len(scored), dtype=bool)
    plateau_score = np.full(len(scored), np.nan)
    position = {index: offset for offset, index in enumerate(scored.index)}
    ma_list = [int(item) for item in ma_periods]
    for family, params in ((FAMILY_FIXED, fixed_thresholds), (FAMILY_VOL, vol_multipliers)):
        param_list = [float(item) for item in params]
        family_rows = scored[scored["family"] == family]
        lookup = {
            (int(ma), float(param)): position[index]
            for index, ma, param in zip(family_rows.index, family_rows["ma_period"], family_rows["param"])
        }
        for i, ma_period in enumerate(ma_list):
            for j, param in enumerate(param_list):
                cell = lookup[(ma_period, param)]
                neighbors = [
                    lookup[(ma_list[a], param_list[b])]
                    for a in range(max(0, i - 1), min(len(ma_list), i + 2))
                    for b in range(max(0, j - 1), min(len(param_list), j + 2))
                    if (a, b) != (i, j)
                ]
                share = float(good[neighbors].mean()) if neighbors else 0.0
                neighbor_count[cell] = len(neighbors)
                neighbor_share[cell] = share
                plateau[cell] = bool(good[cell]) and len(neighbors) >= rule.min_neighbors and share >= rule.neighbor_share - 1e-12
                plateau_score[cell] = float(robust[neighbors + [cell]].mean())
    scored["neighbor_count"] = neighbor_count
    scored["neighbor_good_share"] = neighbor_share
    scored["plateau"] = plateau
    scored["plateau_score"] = plateau_score
    return scored


def recommend_parameters(scored: pd.DataFrame) -> pd.Series | None:
    """Best plateau cell by neighbourhood robust score; ``None`` means no plateau exists."""
    plateau = scored[scored["plateau"]]
    if plateau.empty:
        return None
    ordered = plateau.assign(_family_order=plateau["family"].map(FAMILY_ORDER)).sort_values(
        ["plateau_score", "robust_score", "ma_period", "_family_order", "param"],
        ascending=[False, False, True, True, True],
        kind="mergesort",
    )
    return ordered.iloc[0]


def evaluate_base_fractions(
    context: SeriesContext,
    choice: pd.Series,
    base_fractions: Iterable[float] = BASE_FRACTIONS,
) -> pd.DataFrame:
    desired = context.desired(str(choice["family"]), int(choice["ma_period"]), float(choice["param"]))
    rows = [
        {
            "series": context.spec.key,
            "family": choice["family"],
            "ma_period": int(choice["ma_period"]),
            "param": float(choice["param"]),
            "label": choice["label"],
            "base_fraction": float(fraction),
            **_prefixed(context.run(desired, "is", base_fraction=float(fraction)), "is_"),
            **_prefixed(context.run(desired, "oos", base_fraction=float(fraction)), "oos_"),
            **_prefixed(context.run(desired, "full", base_fraction=float(fraction)), "full_"),
        }
        for fraction in base_fractions
    ]
    frame = pd.DataFrame(rows)
    frame["is_score_base"] = composite_scores(frame, "is_")
    frame["oos_score_base"] = composite_scores(frame, "oos_")
    frame["robust_score_base"] = (frame["is_score_base"] + frame["oos_score_base"]) / 2
    frame["chosen"] = False
    frame.loc[pick_base(frame, "robust_score_base").name, "chosen"] = True
    return frame


SUMMARY_METRICS = (
    "annual_return_pct",
    "max_drawdown_pct",
    "longest_underwater_days",
    "avg_recovery_days",
    "unrecovered_drawdowns",
    "end_drawdown_pct",
)


def evaluate_series(
    spec: SeriesSpec,
    frame: pd.DataFrame,
    *,
    start: pd.Timestamp | str = RESEARCH_START,
    end: pd.Timestamp | str = RESEARCH_END,
    ma_periods: Iterable[int] = MA_PERIODS,
    fixed_thresholds: Iterable[float] = FIXED_THRESHOLDS_PCT,
    vol_multipliers: Iterable[float] = VOL_MULTIPLIERS,
) -> tuple[SeriesContext, pd.DataFrame]:
    context = build_context(spec, frame, start=start, end=end, ma_periods=tuple(ma_periods))
    grid = evaluate_grid(
        context, ma_periods=ma_periods, fixed_thresholds=fixed_thresholds, vol_multipliers=vol_multipliers
    )
    return context, grid


def research_series(
    spec: SeriesSpec,
    frame: pd.DataFrame,
    *,
    start: pd.Timestamp | str = RESEARCH_START,
    end: pd.Timestamp | str = RESEARCH_END,
    ma_periods: Iterable[int] = MA_PERIODS,
    fixed_thresholds: Iterable[float] = FIXED_THRESHOLDS_PCT,
    vol_multipliers: Iterable[float] = VOL_MULTIPLIERS,
    base_fractions: Iterable[float] = BASE_FRACTIONS,
    rule: PlateauRule = DEFAULT_PLATEAU_RULE,
) -> dict[str, object]:
    grids = dict(ma_periods=tuple(ma_periods), fixed_thresholds=tuple(fixed_thresholds), vol_multipliers=tuple(vol_multipliers))
    context, grid = evaluate_series(spec, frame, start=start, end=end, **grids)
    return finalize_series(context, grid, rule=rule, base_fractions=base_fractions, **grids)


def finalize_series(
    context: SeriesContext,
    grid: pd.DataFrame,
    *,
    rule: PlateauRule = DEFAULT_PLATEAU_RULE,
    ma_periods: Iterable[int] = MA_PERIODS,
    fixed_thresholds: Iterable[float] = FIXED_THRESHOLDS_PCT,
    vol_multipliers: Iterable[float] = VOL_MULTIPLIERS,
    base_fractions: Iterable[float] = BASE_FRACTIONS,
) -> dict[str, object]:
    spec = context.spec
    grid = add_plateau_scores(
        grid, ma_periods=ma_periods, fixed_thresholds=fixed_thresholds, vol_multipliers=vol_multipliers, rule=rule
    )
    recommendation = recommend_parameters(grid)
    diagnostic = in_sample_best(grid)
    grid["recommended"] = False
    if recommendation is not None:
        grid.loc[recommendation.name, "recommended"] = True

    base = evaluate_base_fractions(context, recommendation, base_fractions) if recommendation is not None else pd.DataFrame()
    chosen_base = base[base["chosen"]].iloc[0] if not base.empty else None

    hold = np.ones(len(context.close), dtype=np.int8)
    benchmarks = [
        {
            "series": spec.key,
            "scheme": "买入持有",
            "label": "",
            "base_fraction": 1.0,
            **_prefixed(context.run(hold, "is", base_fraction=1.0), "is_"),
            **_prefixed(context.run(hold, "oos", base_fraction=1.0), "oos_"),
            **_prefixed(context.run(hold, "full", base_fraction=1.0), "full_"),
        }
    ]
    if spec.current_ma is not None:
        current_desired = context.desired(FAMILY_FIXED, spec.current_ma, float(spec.current_threshold_pct))
        benchmarks.append(
            {
                "series": spec.key,
                "scheme": "现行参数（仅参考）",
                "label": param_label(FAMILY_FIXED, spec.current_ma, float(spec.current_threshold_pct)),
                "base_fraction": spec.current_base_fraction,
                **_prefixed(context.run(current_desired, "is", base_fraction=spec.current_base_fraction), "is_"),
                **_prefixed(context.run(current_desired, "oos", base_fraction=spec.current_base_fraction), "oos_"),
                **_prefixed(context.run(current_desired, "full", base_fraction=spec.current_base_fraction), "full_"),
            }
        )

    sensitivity = []
    if recommendation is not None:
        desired = context.desired(str(recommendation["family"]), int(recommendation["ma_period"]), float(recommendation["param"]))
        for execution in (EXECUTION_SAME_CLOSE, EXECUTION_NEXT_CLOSE):
            for fraction in sorted({0.0, float(chosen_base["base_fraction"])}):
                sensitivity.append(
                    {
                        "series": spec.key,
                        "label": recommendation["label"],
                        "execution": execution,
                        "base_fraction": fraction,
                        **_prefixed(context.run(desired, "is", base_fraction=fraction, execution=execution), "is_"),
                        **_prefixed(context.run(desired, "oos", base_fraction=fraction, execution=execution), "oos_"),
                        **_prefixed(context.run(desired, "full", base_fraction=fraction, execution=execution), "full_"),
                    }
                )

    window = context.window
    unique = grid[~grid["duplicate_of_fixed_zero"]]
    summary: dict[str, object] = {
        "series": spec.key,
        "name": spec.name,
        "kind": spec.kind,
        "holding_code": spec.holding_code,
        "relation": spec.relation,
        "eval_start": window["eval_start"],
        "is_end": window["is_end"],
        "oos_start": window["oos_start"],
        "eval_end": window["eval_end"],
        "years": window["years"],
        "split": f"{round(window['ratio'] * 100)}/{round((1 - window['ratio']) * 100)}",
        "n_is": window["n_is"],
        "n_oos": window["n_oos"],
        "is_buy_hold_volatility_pct": window["is_buy_hold_volatility_pct"],
        "min_recovery_depth_pct": context.min_depth * 100,
        "plateau_rule": rule.label,
        "good_rules": int(unique["good"].sum()),
        "beats_hold_is_rules": int(unique["beats_hold_is"].sum()),
        "beats_hold_oos_rules": int(unique["beats_hold_oos"].sum()),
        "plateau_cells": int(grid["plateau"].sum()),
        "suitable": recommendation is not None,
        "recommended_label": recommendation["label"] if recommendation is not None else "",
        "recommended_family": recommendation["family"] if recommendation is not None else "",
        "recommended_ma": int(recommendation["ma_period"]) if recommendation is not None else np.nan,
        "recommended_param": float(recommendation["param"]) if recommendation is not None else np.nan,
        "recommended_plateau_score": float(recommendation["plateau_score"]) if recommendation is not None else np.nan,
        "recommended_neighbor_good_share": float(recommendation["neighbor_good_share"]) if recommendation is not None else np.nan,
        "recommended_is_score_pct": float(recommendation["is_score_pct"]) if recommendation is not None else np.nan,
        "recommended_oos_score_pct": float(recommendation["oos_score_pct"]) if recommendation is not None else np.nan,
        "recommended_base_fraction": float(chosen_base["base_fraction"]) if chosen_base is not None else np.nan,
        "is_best_label": diagnostic["label"],
        "is_best_oos_rank": float(diagnostic["oos_rank_combined"]),
    }
    for segment in ("is", "oos", "full"):
        for metric in SUMMARY_METRICS:
            summary[f"rec_{segment}_{metric}"] = chosen_base[f"{segment}_{metric}"] if chosen_base is not None else np.nan
    hold_row = benchmarks[0]
    for segment in ("is", "oos", "full"):
        for metric in SUMMARY_METRICS[:4]:
            summary[f"hold_{segment}_{metric}"] = hold_row[f"{segment}_{metric}"]
    return {
        "context": context,
        "summary": summary,
        "candidates": grid,
        "recommendation": recommendation,
        "base_allocation": base,
        "benchmarks": pd.DataFrame(benchmarks),
        "sensitivity": pd.DataFrame(sensitivity),
    }


def common_oos_comparison(spliced: dict[str, object], etf_only: dict[str, object]) -> pd.DataFrame:
    """Frozen configurations from both 159545 studies on the same ETF return series and window."""
    etf_context: SeriesContext = etf_only["context"]
    spliced_context: SeriesContext = spliced["context"]
    common_start = max(pd.Timestamp(spliced_context.window["oos_start"]), pd.Timestamp(etf_context.window["oos_start"]))
    start_idx = int(np.flatnonzero((pd.to_datetime(etf_context.dates) >= common_start).to_numpy())[0])
    candidates = []
    for source, result in (("指数拼接研究推荐", spliced), ("ETF自身研究推荐", etf_only)):
        choice = result["recommendation"]
        if choice is None:
            continue
        base = result["base_allocation"]
        fraction = float(base[base["chosen"]]["base_fraction"].iloc[0])
        candidates.append((source, str(choice["family"]), int(choice["ma_period"]), float(choice["param"]), fraction))
    spec = etf_context.spec
    if spec.current_ma is not None:
        candidates.append(("现行参数（仅参考）", FAMILY_FIXED, int(spec.current_ma), float(spec.current_threshold_pct), spec.current_base_fraction))
    rows = []
    hold = np.ones(len(etf_context.close), dtype=np.int8)
    rows.append({"source": "买入持有", "label": "", "base_fraction": 1.0, **etf_context.run(hold, "custom", base_fraction=1.0, start_idx=start_idx)})
    for source, family, ma_period, param, fraction in candidates:
        desired = etf_context.desired(family, ma_period, param)
        rows.append(
            {
                "source": source,
                "label": param_label(family, ma_period, param),
                "base_fraction": fraction,
                **etf_context.run(desired, "custom", base_fraction=fraction, start_idx=start_idx),
            }
        )
    return pd.DataFrame(rows)


def run_research(
    data_dir: str | Path = DEFAULT_DATA_DIR,
    *,
    end: pd.Timestamp | str = RESEARCH_END,
    specs: Iterable[SeriesSpec] = SERIES_SPECS,
    progress=None,
) -> dict[str, object]:
    evaluated: dict[str, tuple[SeriesContext, pd.DataFrame]] = {}
    for spec in specs:
        if progress:
            progress(spec)
        frame = load_series(spec, data_dir, end)
        evaluated[spec.key] = evaluate_series(spec, frame, end=end)
    rule = DEFAULT_PLATEAU_RULE
    results = {key: finalize_series(context, grid, rule=rule) for key, (context, grid) in evaluated.items()}

    def stack(name: str) -> pd.DataFrame:
        frames = [item[name] for item in results.values() if not item[name].empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    tables: dict[str, object] = {
        "summary": pd.DataFrame([item["summary"] for item in results.values()]),
        "candidates": stack("candidates"),
        "base_allocation": stack("base_allocation"),
        "benchmarks": stack("benchmarks"),
        "sensitivity": stack("sensitivity"),
        "plateau_rule": rule,
    }
    if "159545" in results and "159545_ETF" in results:
        tables["common_oos_159545"] = common_oos_comparison(results["159545"], results["159545_ETF"])
    tables["results"] = results
    return tables


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


def round_for_export(frame: pd.DataFrame) -> pd.DataFrame:
    rounded = frame.copy()
    for column in rounded.columns:
        series = rounded[column]
        if pd.api.types.is_datetime64_any_dtype(series):
            rounded[column] = series.dt.strftime("%Y-%m-%d")
            continue
        if not pd.api.types.is_float_dtype(series):
            continue
        if column.endswith("_days"):
            rounded[column] = series.round(0)
        elif "score" in column:
            rounded[column] = series.round(3)
        elif column in ("param", "base_fraction", "years"):
            rounded[column] = series.round(2)
        else:
            rounded[column] = series.round(2)
    return rounded
