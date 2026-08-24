from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from services.index_config import (
    CFFEX_FUTURES_MAIN_PRODUCTS, INDEX_CONFIG, INDEX_FINAL_HISTORY_SOURCE,
    INDEX_LONG_HISTORY_BARS, INDEX_LONG_HISTORY_SOURCE,
    INDEX_RECENT_GAP_LOOKBACK_SESSIONS, INDEX_REPORT_DISPLAY_DAYS,
    INDEX_SOURCE_CORRECTION_SOURCE, YAHOO_CHART_HOSTS, YAHOO_REQUEST_GATE,
)
from services.market_calendar import (
    expected_latest_trade_date, get_market_window, is_market_holiday,
    is_market_trading_day, latest_completed_trade_date, latest_settled_trade_date,
)

def sanitize_index_report_market_dates(report_df: pd.DataFrame | None) -> pd.DataFrame | None:
    if report_df is None or report_df.empty or "日期" not in report_df.columns:
        return report_df

    result = report_df.copy()
    dates = pd.to_datetime(result["日期"], errors="coerce")
    for index_name, index_config in INDEX_CONFIG.items():
        market = get_market_window(str(index_config.get("market_group") or ""))
        if market is None:
            continue
        index_columns = [column for column in result.columns if str(column).startswith(f"{index_name}_")]
        if not index_columns:
            continue
        valid_dates = dates.dt.date.map(
            lambda day: not pd.isna(day) and day.weekday() < 5 and not is_market_holiday(market, day)
        )
        result.loc[dates.isna() | ~valid_dates, index_columns] = pd.NA

    value_columns = [column for column in result.columns if column != "日期"]
    result = result.dropna(how="all", subset=value_columns)
    result["日期"] = dates.loc[result.index].dt.strftime("%Y-%m-%d")
    return result.sort_values("日期").reset_index(drop=True)

def build_summary(report_df: pd.DataFrame) -> pd.DataFrame:
    report_df = sanitize_index_report_market_dates(report_df)
    if report_df is None or report_df.empty:
        return pd.DataFrame()
    rows = []
    for index_name, index_config in INDEX_CONFIG.items():
        close_col = f"{index_name}_收盘价"
        ma20_col = f"{index_name}_MA20"
        deviation_col = f"{index_name}_偏离率(%)"
        if close_col not in report_df.columns:
            continue
        price_rows = report_df.dropna(subset=[close_col])
        if price_rows.empty:
            continue
        latest = price_rows.iloc[-1]
        indicator_rows = (
            price_rows.dropna(subset=[ma20_col])
            if ma20_col in price_rows.columns
            else pd.DataFrame()
        )
        show_deviation = index_config.get("show_ma20_deviation", True)
        transition_col = f"{index_name}_状态转变时间"
        interval_col = f"{index_name}_区间涨幅(%)"
        previous_transition_col = f"{index_name}_上一状态转换时间"
        previous_interval_col = f"{index_name}_上一区间涨幅(%)"
        transition_date, interval_return_pct = (pd.NA, pd.NA)
        previous_transition_date, previous_interval_return_pct = (pd.NA, pd.NA)
        if show_deviation and not indicator_rows.empty:
            calculated_transition = calculate_ma20_transition_snapshot(
                indicator_rows,
                close_col,
                ma20_col,
                date_col="日期",
            )
            transition_date = (
                latest[transition_col]
                if transition_col in latest and not pd.isna(latest[transition_col])
                else calculated_transition[0]
            )
            interval_return_pct = (
                latest[interval_col]
                if interval_col in latest and not pd.isna(latest[interval_col])
                else calculated_transition[1]
            )
            previous_transition_date = (
                latest[previous_transition_col]
                if previous_transition_col in latest
                and not pd.isna(latest[previous_transition_col])
                else calculated_transition[2]
            )
            previous_interval_return_pct = (
                latest[previous_interval_col]
                if previous_interval_col in latest
                and not pd.isna(latest[previous_interval_col])
                else calculated_transition[3]
            )
        previous_close = pd.NA
        daily_change_pct = pd.NA
        if len(price_rows) >= 2:
            previous_close = price_rows.iloc[-2][close_col]
            if previous_close:
                daily_change_pct = (latest[close_col] / previous_close - 1) * 100
        rows.append(
            {
                "指数": index_name,
                "代码": display_index_symbol(index_config),
                "日期": latest["日期"],
                "收盘价": latest[close_col],
                "前收盘价": previous_close,
                "当日涨跌幅(%)": daily_change_pct,
                "MA20": latest[ma20_col] if ma20_col in latest else pd.NA,
                "偏离率(%)": latest[deviation_col] if show_deviation and deviation_col in latest else pd.NA,
                "状态转变时间": transition_date,
                "区间涨幅(%)": interval_return_pct,
                "上一状态转换时间": previous_transition_date,
                "上一区间涨幅(%)": previous_interval_return_pct,
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).reset_index(drop=True)

def calculate_ma20_transition(
    valid_rows: pd.DataFrame,
    close_col: str,
    ma20_col: str,
    date_col: str = "日期",
) -> tuple[object, object]:
    transition = calculate_ma20_transition_snapshot(
        valid_rows,
        close_col,
        ma20_col,
        date_col=date_col,
    )
    return transition[0], transition[1]

def calculate_ma20_transition_snapshot(
    valid_rows: pd.DataFrame,
    close_col: str,
    ma20_col: str,
    date_col: str = "日期",
) -> tuple[object, object, object, object]:
    history = calculate_ma20_transition_history(
        valid_rows,
        close_col,
        ma20_col,
        date_col=date_col,
    )
    valid_history = history.dropna(subset=["状态转变时间"])
    if valid_history.empty:
        return pd.NA, pd.NA, pd.NA, pd.NA

    latest = valid_history.iloc[-1]
    return (
        latest["状态转变时间"],
        latest["区间涨幅"],
        latest["上一状态转换时间"],
        latest["上一区间涨幅"],
    )

def calculate_ma20_transition_history(
    valid_rows: pd.DataFrame,
    close_col: str,
    ma20_col: str,
    date_col: str = "日期",
) -> pd.DataFrame:
    history = pd.DataFrame(
        {
            "状态转变时间": pd.Series(pd.NA, index=valid_rows.index, dtype="object"),
            "区间涨幅": pd.Series(pd.NA, index=valid_rows.index, dtype="Float64"),
            "上一状态转换时间": pd.Series(
                pd.NA, index=valid_rows.index, dtype="object"
            ),
            "上一区间涨幅": pd.Series(
                pd.NA, index=valid_rows.index, dtype="Float64"
            ),
        }
    )
    if len(valid_rows) < 2 or date_col not in valid_rows.columns:
        return history

    data = valid_rows[[date_col, close_col, ma20_col]].copy()
    data[close_col] = pd.to_numeric(data[close_col], errors="coerce")
    data[ma20_col] = pd.to_numeric(data[ma20_col], errors="coerce")
    data = data.dropna(subset=[date_col, close_col, ma20_col])
    if len(data) < 2:
        return history

    is_above = data[close_col] >= data[ma20_col]
    is_transition = is_above.ne(is_above.shift()) & is_above.shift().notna()
    transition_close = data[close_col].where(is_transition).ffill()
    transition_date = (
        pd.to_datetime(data[date_col], errors="coerce")
        .dt.strftime("%Y-%m-%d")
        .where(is_transition)
        .ffill()
    )
    interval_return = (data[close_col] / transition_close - 1) * 100
    transition_event_dates = (
        pd.to_datetime(data.loc[is_transition, date_col], errors="coerce")
        .dt.strftime("%Y-%m-%d")
    )
    transition_event_closes = data.loc[is_transition, close_col]
    previous_transition_date = pd.Series(pd.NA, index=data.index, dtype="object")
    previous_interval_return = pd.Series(pd.NA, index=data.index, dtype="Float64")
    previous_transition_date.loc[transition_event_dates.index] = (
        transition_event_dates.shift(1)
    )
    completed_interval_return = (
        transition_event_closes / transition_event_closes.shift(1) - 1
    ) * 100
    previous_interval_return.loc[completed_interval_return.index] = (
        completed_interval_return
    )
    previous_transition_date = previous_transition_date.ffill()
    previous_interval_return = previous_interval_return.ffill()

    history.loc[data.index, "状态转变时间"] = transition_date
    history.loc[data.index, "区间涨幅"] = interval_return.astype("Float64")
    history.loc[data.index, "上一状态转换时间"] = previous_transition_date
    history.loc[data.index, "上一区间涨幅"] = previous_interval_return
    return history

__all__ = ['sanitize_index_report_market_dates', 'build_summary', 'calculate_ma20_transition', 'calculate_ma20_transition_snapshot', 'calculate_ma20_transition_history']
