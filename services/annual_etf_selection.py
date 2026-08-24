from __future__ import annotations

from dataclasses import asdict
from math import ceil, floor
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from services.annual_etf_models import (
    ALL_SLOTS,
    SCORE_WEIGHTS,
    US_SLOTS,
    AnnualBacktestSettings,
    AnnualQualificationResult,
    AnnualSelection,
    HistoricalEtfRecord,
    _empty_frame,
)
from services.annual_etf_market import (
    stitch_proxy_history,
    validate_registry_against_whitelist,
)


def _decision_date(year: int, market_data: dict[str, pd.DataFrame]) -> pd.Timestamp | None:
    latest: list[pd.Timestamp] = []
    cutoff = pd.Timestamp(year - 1, 12, 31)
    for frame in market_data.values():
        if frame is None or frame.empty:
            continue
        dates = pd.to_datetime(frame["trade_date"], errors="coerce")
        dates = dates[dates <= cutoff]
        if not dates.empty:
            latest.append(pd.Timestamp(dates.max()).normalize())
    return max(latest) if latest else None


def _research_window(
    frame: pd.DataFrame,
    decision_date: pd.Timestamp,
    settings: AnnualBacktestSettings,
) -> pd.DataFrame:
    start = decision_date - pd.DateOffset(years=settings.max_history_years) + pd.Timedelta(days=1)
    dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame.loc[dates.between(start, decision_date)].copy().reset_index(drop=True)


def _proxy_for_record(
    record: HistoricalEtfRecord,
    market_data: dict[str, pd.DataFrame],
    proxy_data: dict[str, pd.DataFrame] | None,
    decision_date: pd.Timestamp,
) -> pd.DataFrame | None:
    if record.proxy_symbol:
        candidate = market_data.get(record.proxy_symbol)
        if candidate is not None and not candidate.empty:
            return candidate
    if (
        record.proxy_available_date is not None
        and record.proxy_available_date > decision_date
    ):
        return None
    return (proxy_data or {}).get(record.symbol)


def preflight_annual_candidates(
    records: list[HistoricalEtfRecord],
    whitelist: dict[str, object],
    market_data: dict[str, pd.DataFrame],
    settings: AnnualBacktestSettings,
    proxy_data: dict[str, pd.DataFrame] | None = None,
) -> AnnualQualificationResult:
    registry_status = validate_registry_against_whitelist(records, whitelist).set_index("symbol")
    end_date = pd.Timestamp(settings.end_date or pd.Timestamp.today()).normalize()
    rows: list[dict[str, object]] = []
    research: dict[tuple[int, str], pd.DataFrame] = {}
    error_rows: list[dict[str, object]] = []
    end_year = end_date.year
    for year in range(int(settings.start_year), end_year + 1):
        decision = _decision_date(year, market_data)
        if decision is None:
            error_rows.append({"year": year, "stage": "资格预检", "error": "缺少上一年度正式行情"})
            continue
        for record in records:
            base = registry_status.loc[record.symbol]
            reason = str(base["registry_reason"] or "")
            qualified = bool(base["registry_eligible"])
            actual = market_data.get(record.symbol)
            actual_days = 0
            research_days = 0
            proxy_ratio = 0.0
            turnover_days = 0
            turnover_median = np.nan
            if actual is None or actual.empty:
                qualified = False
                reason = reason or "缺少未复权正式行情"
                stitched = pd.DataFrame()
            else:
                actual_dates = pd.to_datetime(actual["trade_date"], errors="coerce")
                actual_days = int(
                    ((actual_dates >= record.listing_date) & (actual_dates <= decision)).sum()
                )
                if decision < record.listing_date:
                    qualified = False
                    reason = reason or "决策日尚未上市"
                elif actual_days < settings.min_listing_days:
                    qualified = False
                    reason = reason or f"上市实际交易日不足{settings.min_listing_days}日"
                proxy = _proxy_for_record(record, market_data, proxy_data, decision)
                stitched = stitch_proxy_history(actual, proxy, record.listing_date)
                stitched = _research_window(stitched, decision, settings)
                research_days = len(stitched)
                proxy_ratio = float(stitched["is_proxy"].mean() * 100) if len(stitched) else 0.0
                split_index = int(ceil(research_days * settings.train_ratio))
                train_days = split_index
                validation_days = research_days - split_index
                if (
                    train_days < settings.min_train_days
                    or validation_days < settings.min_validation_days
                ):
                    qualified = False
                    reason = reason or (
                        f"70/30拆分后筛选{train_days}日、验证{validation_days}日，"
                        f"不足{settings.min_train_days}/{settings.min_validation_days}日"
                    )
                if not stitched.empty:
                    research[(year, record.symbol)] = stitched
                recent = actual.loc[actual_dates <= decision].tail(settings.turnover_window)
                amount = pd.to_numeric(recent.get("amount"), errors="coerce").dropna()
                turnover_days = len(amount)
                if turnover_days >= settings.min_turnover_days:
                    turnover_median = float(amount.median())
            rows.append(
                {
                    "year": year,
                    "decision_date": decision,
                    "symbol": record.symbol,
                    "name": record.name,
                    "direction": record.direction,
                    "tracked_index": record.tracked_index,
                    "index_family": record.index_family,
                    "listing_date": record.listing_date,
                    "actual_trading_days": actual_days,
                    "research_days": research_days,
                    "proxy_ratio_pct": proxy_ratio,
                    "turnover_valid_days": turnover_days,
                    "turnover_median": turnover_median,
                    "qualified_before_index_dedup": qualified,
                    "qualified": qualified,
                    "representative": False,
                    "reason": reason,
                }
            )
    qualification = pd.DataFrame(rows)
    if qualification.empty:
        return AnnualQualificationResult(qualification, research, pd.DataFrame(error_rows))

    for (_year, _index), group in qualification.groupby(["year", "tracked_index"], sort=False):
        eligible = group[group["qualified_before_index_dedup"]].copy()
        if eligible.empty:
            continue
        eligible["has_turnover"] = eligible["turnover_valid_days"] >= settings.min_turnover_days
        eligible["listing_date"] = pd.to_datetime(eligible["listing_date"])
        eligible = eligible.sort_values(
            ["has_turnover", "turnover_median", "listing_date", "research_days", "symbol"],
            ascending=[False, False, True, False, True],
            na_position="last",
        )
        winner = eligible.index[0]
        qualification.loc[winner, "representative"] = True
        for index in eligible.index[1:]:
            qualification.loc[index, "qualified"] = False
            qualification.loc[index, "reason"] = "同指数代表ETF未胜出"
    qualification.loc[
        qualification["qualified_before_index_dedup"] & ~qualification["representative"],
        "qualified",
    ] = False
    return AnnualQualificationResult(qualification, research, pd.DataFrame(error_rows))




def _desired_states(signal: pd.Series, ma_period: int, threshold_pct: float) -> tuple[pd.Series, pd.Series]:
    prices = pd.to_numeric(signal, errors="coerce")
    ma = prices.rolling(int(ma_period), min_periods=int(ma_period)).mean()
    threshold = float(threshold_pct) / 100
    state = 0
    states = []
    for price, average in zip(prices, ma):
        if np.isfinite(price) and np.isfinite(average):
            if price > average * (1 + threshold):
                state = 1
            elif price < average * (1 - threshold):
                state = 0
        states.append(state)
    return pd.Series(states, index=signal.index, dtype=int), ma


def _apply_split(shares: float, ratio: float, rounding: str) -> float:
    if shares <= 0 or not np.isfinite(ratio) or np.isclose(ratio, 1.0):
        return shares
    adjusted = shares * ratio
    if rounding == "ceil":
        return float(np.ceil(adjusted - 1e-12))
    if rounding == "round":
        return float(np.rint(adjusted))
    return float(np.floor(adjusted + 1e-12))


def _research_leg(
    frame: pd.DataFrame,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    *,
    ma_period: int,
    threshold_pct: float,
    capital: float,
    commission_rate: float,
    lot_size: int,
    cash_annual_rate: float,
    always_hold: bool,
) -> pd.DataFrame:
    data = frame.copy().sort_values("trade_date").reset_index(drop=True)
    states, _ma = _desired_states(data["signal_close"], ma_period, threshold_pct)
    data["desired"] = 1 if always_hold else states
    selected = data[data["trade_date"].between(start_date, end_date)].copy()
    if selected.empty:
        return pd.DataFrame()
    cash = float(capital)
    shares = 0.0
    rows = []
    previous_date: pd.Timestamp | None = None
    for row in selected.itertuples(index=False):
        trade_date = pd.Timestamp(row.trade_date)
        if previous_date is not None and cash > 0 and cash_annual_rate:
            cash *= (1 + cash_annual_rate) ** (max(0, (trade_date - previous_date).days) / 365)
        shares = _apply_split(
            shares,
            float(getattr(row, "share_split_ratio", 1.0) or 1.0),
            str(getattr(row, "share_split_rounding", "") or ""),
        )
        dividend = float(getattr(row, "dividend_per_share", 0.0) or 0.0)
        if shares > 0 and dividend > 0:
            cash += shares * dividend
        price = float(row.raw_close)
        desired = int(row.desired)
        if desired and shares <= 0:
            affordable = cash / (price * (1 + commission_rate))
            buy_shares = floor(affordable / lot_size) * lot_size
            if buy_shares > 0:
                gross = buy_shares * price
                cash -= gross + gross * commission_rate
                shares = float(buy_shares)
        elif not desired and shares > 0:
            gross = shares * price
            cash += gross - gross * commission_rate
            shares = 0.0
        rows.append({"trade_date": trade_date, "value": cash + shares * price})
        previous_date = trade_date
    return pd.DataFrame(rows)


def _research_strategy(
    frame: pd.DataFrame,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    *,
    strategy: str,
    ma_period: int,
    threshold_pct: float,
    settings: AnnualBacktestSettings,
    capital: float = 100000.0,
) -> pd.DataFrame:
    if strategy == "half_timing":
        hold = _research_leg(
            frame,
            start_date,
            end_date,
            ma_period=ma_period,
            threshold_pct=threshold_pct,
            capital=capital / 2,
            commission_rate=settings.commission_rate,
            lot_size=settings.lot_size,
            cash_annual_rate=settings.cash_annual_rate,
            always_hold=True,
        )
        timing = _research_leg(
            frame,
            start_date,
            end_date,
            ma_period=ma_period,
            threshold_pct=threshold_pct,
            capital=capital - capital / 2,
            commission_rate=settings.commission_rate,
            lot_size=settings.lot_size,
            cash_annual_rate=settings.cash_annual_rate,
            always_hold=False,
        )
        if hold.empty or timing.empty:
            return pd.DataFrame()
        merged = hold.merge(timing, on="trade_date", suffixes=("_hold", "_timing"))
        return pd.DataFrame(
            {"trade_date": merged["trade_date"], "portfolio_value": merged["value_hold"] + merged["value_timing"]}
        )
    timing = _research_leg(
        frame,
        start_date,
        end_date,
        ma_period=ma_period,
        threshold_pct=threshold_pct,
        capital=capital,
        commission_rate=settings.commission_rate,
        lot_size=settings.lot_size,
        cash_annual_rate=settings.cash_annual_rate,
        always_hold=False,
    )
    return timing.rename(columns={"value": "portfolio_value"})


def _longest_underwater_days(values: pd.Series, dates: pd.Series, initial_value: float) -> int:
    peak = float(initial_value)
    peak_date = pd.Timestamp(dates.iloc[0]) - pd.Timedelta(days=1)
    longest = 0
    for date, value in zip(pd.to_datetime(dates), pd.to_numeric(values, errors="coerce")):
        if not np.isfinite(value):
            continue
        if value >= peak - 1e-10:
            if value > peak:
                peak = float(value)
                peak_date = pd.Timestamp(date)
        else:
            longest = max(longest, int((pd.Timestamp(date) - peak_date).days))
    return int(longest)


def performance_metrics(
    daily: pd.DataFrame,
    initial_capital: float,
    cash_annual_rate: float,
    *,
    value_column: str = "portfolio_value",
    date_column: str = "trade_date",
) -> dict[str, object]:
    if daily is None or daily.empty:
        raise ValueError("净值序列为空。")
    observations = pd.DataFrame(
        {
            "date": pd.to_datetime(daily[date_column], errors="coerce"),
            "value": pd.to_numeric(daily[value_column], errors="coerce"),
        }
    ).dropna()
    if observations.empty:
        raise ValueError("净值序列没有有效日期和数值。")
    observations = observations.sort_values("date").reset_index(drop=True)
    values = observations["value"]
    dates = observations["date"]
    seeded = pd.concat([pd.Series([float(initial_capital)]), values], ignore_index=True)
    returns = seeded.pct_change().dropna()
    total_return = float(values.iloc[-1] / initial_capital - 1)
    elapsed = max(1, int((dates.iloc[-1] - dates.iloc[0]).days))
    annual_return = (1 + total_return) ** (365 / elapsed) - 1 if total_return > -1 else -1.0
    volatility = float(returns.std(ddof=1) * np.sqrt(252)) if len(returns) > 1 else 0.0
    risk_free_daily = (1 + cash_annual_rate) ** (1 / 252) - 1
    sharpe = (
        float((returns.mean() - risk_free_daily) / returns.std(ddof=1) * np.sqrt(252))
        if len(returns) > 1 and returns.std(ddof=1) > 0
        else 0.0
    )
    drawdown = seeded / seeded.cummax() - 1
    return {
        "start_date": dates.iloc[0],
        "end_date": dates.iloc[-1],
        "trading_days": int(len(values)),
        "final_value": float(values.iloc[-1]),
        "net_profit": float(values.iloc[-1] - initial_capital),
        "total_return_pct": total_return * 100,
        "annual_return_pct": annual_return * 100,
        "max_drawdown_pct": float(drawdown.min() * 100),
        "annual_volatility_pct": volatility * 100,
        "sharpe_ratio": sharpe,
        "longest_underwater_days": _longest_underwater_days(values, dates, initial_capital),
    }


def score_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    scored = frame.copy()
    if scored.empty:
        return scored
    scored["underwater_score"] = scored["longest_underwater_days"].rank(
        ascending=False, pct=True, method="average"
    )
    scored["drawdown_score"] = scored["max_drawdown_pct"].rank(
        ascending=True, pct=True, method="average"
    )
    scored["volatility_score"] = scored["annual_volatility_pct"].rank(
        ascending=False, pct=True, method="average"
    )
    scored["return_score"] = scored["annual_return_pct"].rank(
        ascending=True, pct=True, method="average"
    )
    scored["sharpe_score"] = scored["sharpe_ratio"].rank(
        ascending=True, pct=True, method="average"
    )
    score_columns = {
        "longest_underwater_days": "underwater_score",
        "max_drawdown_pct": "drawdown_score",
        "annual_volatility_pct": "volatility_score",
        "annual_return_pct": "return_score",
        "sharpe_ratio": "sharpe_score",
    }
    scored["composite_score"] = sum(
        scored[score_columns[metric]] * weight for metric, weight in SCORE_WEIGHTS.items()
    )
    return scored


def _best_parameter(
    record: HistoricalEtfRecord,
    research: pd.DataFrame,
    settings: AnnualBacktestSettings,
    decision_date: pd.Timestamp,
) -> tuple[dict[str, object], pd.DataFrame, dict[str, object]]:
    split = int(ceil(len(research) * settings.train_ratio))
    train_days = split
    validation_days = len(research) - split
    if train_days < settings.min_train_days or validation_days < settings.min_validation_days:
        raise ValueError(
            f"70/30拆分后筛选{train_days}日、验证{validation_days}日，"
            f"不足{settings.min_train_days}/{settings.min_validation_days}日。"
        )
    train_start = pd.Timestamp(research.iloc[0]["trade_date"])
    train_end = pd.Timestamp(research.iloc[split - 1]["trade_date"])
    validation_start = pd.Timestamp(research.iloc[split]["trade_date"])
    validation_end = pd.Timestamp(research.iloc[-1]["trade_date"])
    strategy = "half_timing" if record.direction in US_SLOTS else "timing"
    rows = []
    for ma_period in settings.ma_periods:
        for threshold in settings.threshold_pcts:
            daily = _research_strategy(
                research,
                train_start,
                train_end,
                strategy=strategy,
                ma_period=int(ma_period),
                threshold_pct=float(threshold),
                settings=settings,
            )
            metrics = performance_metrics(daily, 100000.0, settings.cash_annual_rate)
            rows.append(
                {
                    "symbol": record.symbol,
                    "name": record.name,
                    "decision_date": decision_date,
                    "segment": "parameter_training",
                    "ma_period": int(ma_period),
                    "threshold_pct": float(threshold),
                    **metrics,
                }
            )
    scored = score_metrics(pd.DataFrame(rows))
    chosen = scored.sort_values(
        [
            "composite_score",
            "sharpe_ratio",
            "longest_underwater_days",
            "max_drawdown_pct",
            "annual_return_pct",
            "ma_period",
            "threshold_pct",
        ],
        ascending=[False, False, True, False, False, True, True],
    ).iloc[0].to_dict()
    validation_daily = _research_strategy(
        research,
        validation_start,
        validation_end,
        strategy=strategy,
        ma_period=int(chosen["ma_period"]),
        threshold_pct=float(chosen["threshold_pct"]),
        settings=settings,
    )
    validation = performance_metrics(validation_daily, 100000.0, settings.cash_annual_rate)
    validation.update(
        {
            "symbol": record.symbol,
            "name": record.name,
            "direction": record.direction,
            "tracked_index": record.tracked_index,
            "ma_period": int(chosen["ma_period"]),
            "threshold_pct": float(chosen["threshold_pct"]),
            "strategy": strategy,
            "validation_start": validation_start,
            "validation_end": validation_end,
        }
    )
    return chosen, scored, validation


def build_annual_selections(
    records: list[HistoricalEtfRecord],
    preflight: AnnualQualificationResult,
    settings: AnnualBacktestSettings,
    progress_callback: Callable[[str, float], None] | None = None,
) -> tuple[list[AnnualSelection], pd.DataFrame, pd.DataFrame]:
    records_by_symbol = {item.symbol: item for item in records}
    qualification = preflight.qualification
    selections: list[AnnualSelection] = []
    parameter_frames: list[pd.DataFrame] = []
    error_rows: list[dict[str, object]] = []
    years = sorted(qualification["year"].unique()) if not qualification.empty else []
    total = max(1, sum(len(group[group["qualified"]]) for _, group in qualification.groupby("year")))
    completed = 0
    for year in years:
        annual = qualification[(qualification["year"] == year) & qualification["qualified"]]
        validation_rows: list[dict[str, object]] = []
        for row in annual.itertuples(index=False):
            record = records_by_symbol[str(row.symbol)]
            try:
                _chosen, parameters, validation = _best_parameter(
                    record,
                    preflight.research_data[(int(year), record.symbol)],
                    settings,
                    pd.Timestamp(row.decision_date),
                )
                parameters.insert(0, "year", int(year))
                parameter_frames.append(parameters)
                validation["proxy_ratio_pct"] = float(row.proxy_ratio_pct)
                validation_rows.append(validation)
            except Exception as exc:
                error_rows.append(
                    {"year": int(year), "symbol": record.symbol, "stage": "参数筛选", "error": str(exc)}
                )
            completed += 1
            if progress_callback:
                progress_callback(f"年度筛选：{year} {record.symbol}", completed / total)
        validation_frame = pd.DataFrame(validation_rows)
        for slot in ALL_SLOTS:
            candidates = validation_frame[validation_frame["direction"] == slot].copy()
            if candidates.empty:
                error_rows.append({"year": int(year), "symbol": "", "stage": "年度选择", "error": f"{slot} 无合格候选"})
                continue
            candidates = score_metrics(candidates)
            gated = candidates[candidates["annual_return_pct"] >= settings.annual_return_gate_pct]
            relaxed = gated.empty
            ranked = candidates if relaxed else gated
            winner = ranked.sort_values(
                [
                    "composite_score",
                    "sharpe_ratio",
                    "longest_underwater_days",
                    "max_drawdown_pct",
                    "annual_return_pct",
                    "symbol",
                ],
                ascending=[False, False, True, False, False, True],
            ).iloc[0]
            record = records_by_symbol[str(winner["symbol"])]
            selections.append(
                AnnualSelection(
                    year=int(year),
                    slot=slot,
                    symbol=record.symbol,
                    name=record.name,
                    ma_period=int(winner["ma_period"]),
                    threshold_pct=float(winner["threshold_pct"]),
                    strategy="half_timing" if slot in US_SLOTS else "timing",
                    validation_score=float(winner["composite_score"]),
                    validation_annual_return_pct=float(winner["annual_return_pct"]),
                    validation_sharpe=float(winner["sharpe_ratio"]),
                    return_gate_relaxed=bool(relaxed),
                    proxy_ratio_pct=float(winner["proxy_ratio_pct"]),
                    decision_date=pd.Timestamp(qualification[qualification["year"] == year]["decision_date"].iloc[0]),
                )
            )
    parameters = pd.concat(parameter_frames, ignore_index=True) if parameter_frames else pd.DataFrame()
    return selections, parameters, pd.DataFrame(error_rows)


def selections_frame(selections: Iterable[AnnualSelection]) -> pd.DataFrame:
    rows = []
    for item in selections:
        row = asdict(item)
        row["decision_date"] = item.decision_date
        rows.append(row)
    return pd.DataFrame(rows)


def _selection_map(selections: Iterable[AnnualSelection]) -> dict[tuple[int, str], AnnualSelection]:
    return {(item.year, item.slot): item for item in selections}
