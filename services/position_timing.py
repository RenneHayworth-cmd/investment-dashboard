from __future__ import annotations

import pandas as pd

from services.position_models import (
    ETF_512890_ACTIVE_TRANSFER_SOURCE_CODES,
    ETF_512890_TRANSFER_SOURCE_CODES,
    ETF_DISPLAY_NAMES,
    ETF_PORTFOLIO_WEIGHTS_PCT,
    ETF_POSITION_STRATEGIES,
    ETF_TIMING_STRATEGIES,
    ETF_TIMING_TABLE_EXCLUDED_CODES,
    PositionItem,
    display_etf_name,
    normalize_etf_base_code,
)

def calculate_etf_timing_snapshot(
    df: pd.DataFrame,
    *,
    ma_period: int,
    threshold_pct: float,
) -> dict[str, object]:
    data = df[["date", "price"]].copy() if {"date", "price"}.issubset(df.columns) else pd.DataFrame()
    if data.empty:
        return {}
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data["price"] = pd.to_numeric(data["price"], errors="coerce")
    data = data.dropna(subset=["date", "price"]).sort_values("date").reset_index(drop=True)
    if data.empty:
        return {}

    ma_col = f"ma_{int(ma_period)}"
    data[ma_col] = data["price"].rolling(window=int(ma_period)).mean()
    threshold = float(threshold_pct) / 100
    position = 0
    latest_action = "等待均线"
    transition_date = None
    transition_price = None
    previous_transition_date = None
    previous_transition_price = None
    previous_interval_return_pct = pd.NA
    for _, row in data.iterrows():
        ma_value = pd.to_numeric(row[ma_col], errors="coerce")
        if pd.isna(ma_value):
            continue
        price = float(row["price"])
        desired_position = (
            1
            if price > float(ma_value) * (1 + threshold)
            else 0
            if price < float(ma_value) * (1 - threshold)
            else position
        )
        if desired_position != position:
            previous_transition_date = transition_date
            previous_transition_price = transition_price
            latest_action = "买入" if desired_position else "卖出"
            transition_date = pd.Timestamp(row["date"])
            transition_price = price
            previous_interval_return_pct = (
                (transition_price / previous_transition_price - 1) * 100
                if previous_transition_price is not None and previous_transition_price != 0
                else pd.NA
            )
        else:
            latest_action = "持有" if position else "空仓"
        position = desired_position

    latest = data.iloc[-1]
    latest_ma = pd.to_numeric(latest[ma_col], errors="coerce")
    latest_price = float(latest["price"])
    deviation_pct = (
        (latest_price / float(latest_ma) - 1) * 100
        if not pd.isna(latest_ma) and float(latest_ma) != 0
        else pd.NA
    )
    interval_return_pct = (
        (latest_price / transition_price - 1) * 100
        if transition_price is not None and transition_price != 0
        else pd.NA
    )
    return {
        "策略参数": f"MA{int(ma_period)} / {float(threshold_pct):.1f}%",
        "策略均线": latest_ma,
        "策略偏离(%)": deviation_pct,
        "择时判断": latest_action,
        "状态转换时间": transition_date.strftime("%Y-%m-%d") if transition_date is not None else pd.NA,
        "策略区间涨幅(%)": interval_return_pct,
        "上一状态转换时间": (
            previous_transition_date.strftime("%Y-%m-%d")
            if previous_transition_date is not None
            else pd.NA
        ),
        "策略上一区间涨幅(%)": previous_interval_return_pct,
    }


def etf_position_decision(code: str, timing_action: object) -> object:
    if timing_action is None or pd.isna(timing_action):
        return pd.NA
    action = str(timing_action)
    if ETF_POSITION_STRATEGIES.get(normalize_etf_base_code(code)) != "半仓持有半仓择时":
        return action
    return {
        "买入": "加至满仓",
        "持有": "持有",
        "卖出": "降至半仓",
        "空仓": "半仓",
        "等待均线": "半仓（等待均线）",
    }.get(action, action)


def calculate_etf_timing_transitions(
    df: pd.DataFrame,
    *,
    ma_period: int,
    threshold_pct: float,
) -> pd.DataFrame:
    columns = ["日期", "收盘价", "均线", "原始信号"]
    if df is None or df.empty or not {"date", "price"}.issubset(df.columns):
        return pd.DataFrame(columns=columns)

    data = df[["date", "price"]].copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data["price"] = pd.to_numeric(data["price"], errors="coerce")
    data = data.dropna(subset=["date", "price"]).sort_values("date").reset_index(drop=True)
    if data.empty:
        return pd.DataFrame(columns=columns)

    ma_col = f"ma_{int(ma_period)}"
    data[ma_col] = data["price"].rolling(window=int(ma_period)).mean()
    threshold = float(threshold_pct) / 100
    position = 0
    rows = []
    for _, row in data.iterrows():
        ma_value = pd.to_numeric(row[ma_col], errors="coerce")
        if pd.isna(ma_value):
            continue
        price = float(row["price"])
        desired_position = (
            1
            if price > float(ma_value) * (1 + threshold)
            else 0
            if price < float(ma_value) * (1 - threshold)
            else position
        )
        if desired_position != position:
            rows.append(
                {
                    "日期": pd.Timestamp(row["date"]),
                    "收盘价": price,
                    "均线": float(ma_value),
                    "原始信号": "买入" if desired_position else "卖出",
                }
            )
        position = desired_position
    return pd.DataFrame(rows, columns=columns)


def _calculate_etf_timing_position_series(
    df: pd.DataFrame | None,
    *,
    ma_period: int,
    threshold_pct: float,
) -> pd.Series:
    if df is None or df.empty or not {"date", "price"}.issubset(df.columns):
        return pd.Series(dtype="int64")
    data = df[["date", "price"]].copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    data["price"] = pd.to_numeric(data["price"], errors="coerce")
    data = (
        data.dropna(subset=["date", "price"])
        .sort_values("date")
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
    )
    if data.empty:
        return pd.Series(dtype="int64")

    ma_values = data["price"].rolling(window=int(ma_period)).mean()
    threshold = float(threshold_pct) / 100
    position = 0
    dates: list[pd.Timestamp] = []
    positions: list[int] = []
    for row_index, row in data.iterrows():
        ma_value = pd.to_numeric(ma_values.iloc[row_index], errors="coerce")
        if pd.isna(ma_value):
            continue
        price = float(row["price"])
        if price > float(ma_value) * (1 + threshold):
            position = 1
        elif price < float(ma_value) * (1 - threshold):
            position = 0
        dates.append(pd.Timestamp(row["date"]))
        positions.append(position)
    return pd.Series(positions, index=pd.DatetimeIndex(dates), dtype="int64")


def _timing_action_position(value: object) -> int | None:
    if value is None or pd.isna(value):
        return None
    action = str(value)
    if action in {"买入", "持有", "加至满仓"}:
        return 1
    if action in {"卖出", "空仓", "降至半仓", "等待均线"}:
        return 0
    return None


def calculate_512890_parking_snapshot(items: list[PositionItem]) -> dict[str, object]:
    etf_items = {
        normalize_etf_base_code(item.code): item
        for item in items
        if item.category == "ETF"
    }
    parking_item = etf_items.get("512890")
    if parking_item is None:
        return {}

    active_source_items = [
        etf_items.get(code) for code in ETF_512890_ACTIVE_TRANSFER_SOURCE_CODES
    ]
    if any(item is None or not item.formal_history_valid for item in active_source_items):
        return {
            "组合权重比例": "-",
            "择时判断": "-",
            "状态转换时间": "-",
            "策略区间涨幅(%)": pd.NA,
            "上一状态转换时间": "-",
            "策略上一区间涨幅(%)": pd.NA,
        }

    source_series: dict[str, pd.Series] = {}
    current_source_positions: dict[str, int] = {}
    latest_date_candidates: list[pd.Timestamp] = []
    for code in ETF_512890_ACTIVE_TRANSFER_SOURCE_CODES:
        item = etf_items.get(code)
        strategy = ETF_TIMING_STRATEGIES.get(code)
        if item is None or strategy is None:
            continue
        series = _calculate_etf_timing_position_series(
            item.dataframe,
            ma_period=int(strategy[0]),
            threshold_pct=float(strategy[1]),
        )
        if not series.empty:
            source_series[code] = series
        current_position = _timing_action_position(item.metrics.get("择时判断"))
        if current_position is None and not series.empty:
            current_position = int(series.iloc[-1])
        if current_position is not None:
            current_source_positions[code] = current_position
        latest_date = pd.to_datetime(item.latest_date, errors="coerce")
        if pd.notna(latest_date):
            latest_date_candidates.append(pd.Timestamp(latest_date).normalize())

    parking_prices = pd.Series(dtype="float64")
    if parking_item.dataframe is not None and {"date", "price"}.issubset(
        parking_item.dataframe.columns
    ):
        parking_history = parking_item.dataframe[["date", "price"]].copy()
        parking_history["date"] = pd.to_datetime(
            parking_history["date"], errors="coerce"
        ).dt.normalize()
        parking_history["price"] = pd.to_numeric(parking_history["price"], errors="coerce")
        parking_history = (
            parking_history.dropna(subset=["date", "price"])
            .sort_values("date")
            .drop_duplicates(subset=["date"], keep="last")
        )
        parking_prices = parking_history.set_index("date")["price"]
    parking_latest_date = pd.to_datetime(parking_item.latest_date, errors="coerce")
    if pd.notna(parking_latest_date):
        latest_date_candidates.append(pd.Timestamp(parking_latest_date).normalize())

    transitions: list[dict[str, object]] = []
    formal_latest_state = 0
    formal_latest_date = pd.NaT
    if len(source_series) == len(ETF_512890_ACTIVE_TRANSFER_SOURCE_CODES):
        first_common_ready_date = max(series.index.min() for series in source_series.values())
        combined_positions = pd.concat(source_series, axis=1).sort_index()
        combined_positions = combined_positions.loc[first_common_ready_date:].ffill().dropna()
        if not combined_positions.empty:
            parking_states = combined_positions.eq(0).any(axis=1).astype(int)
            previous_state = 0
            for transition_date, parking_state in parking_states.items():
                state = int(parking_state)
                if state != previous_state:
                    transition_price = pd.to_numeric(
                        parking_prices.get(pd.Timestamp(transition_date)), errors="coerce"
                    )
                    transitions.append(
                        {
                            "date": pd.Timestamp(transition_date),
                            "state": state,
                            "price": transition_price,
                        }
                    )
                previous_state = state
            formal_latest_state = int(parking_states.iloc[-1])
            formal_latest_date = pd.Timestamp(parking_states.index[-1])

    all_current_states_ready = len(current_source_positions) == len(
        ETF_512890_ACTIVE_TRANSFER_SOURCE_CODES
    )
    empty_source_count = (
        sum(position == 0 for position in current_source_positions.values())
        if all_current_states_ready
        else None
    )
    current_state = int(empty_source_count > 0) if empty_source_count is not None else None
    current_date = max(latest_date_candidates) if latest_date_candidates else formal_latest_date
    latest_price = pd.to_numeric(parking_item.metrics.get("最新价"), errors="coerce")
    if pd.isna(latest_price) and not parking_prices.empty:
        latest_price = float(parking_prices.iloc[-1])

    if (
        current_state is not None
        and pd.notna(current_date)
        and (pd.isna(formal_latest_date) or current_date > formal_latest_date)
        and current_state != formal_latest_state
    ):
        transitions.append(
            {
                "date": pd.Timestamp(current_date),
                "state": current_state,
                "price": latest_price,
            }
        )

    latest_transition = transitions[-1] if transitions else None
    previous_transition = transitions[-2] if len(transitions) >= 2 else None
    interval_return_pct = pd.NA
    previous_interval_return_pct = pd.NA
    if latest_transition is not None:
        transition_price = pd.to_numeric(latest_transition["price"], errors="coerce")
        if pd.notna(latest_price) and pd.notna(transition_price) and float(transition_price) != 0:
            interval_return_pct = (float(latest_price) / float(transition_price) - 1) * 100
    if latest_transition is not None and previous_transition is not None:
        transition_price = pd.to_numeric(latest_transition["price"], errors="coerce")
        previous_price = pd.to_numeric(previous_transition["price"], errors="coerce")
        if pd.notna(transition_price) and pd.notna(previous_price) and float(previous_price) != 0:
            previous_interval_return_pct = (
                float(transition_price) / float(previous_price) - 1
            ) * 100

    return {
        "组合权重比例": f"{empty_source_count * 10}%" if empty_source_count is not None else "-",
        "择时判断": (
            "持有" if current_state == 1 else "空仓" if current_state == 0 else "-"
        ),
        "状态转换时间": (
            pd.Timestamp(latest_transition["date"]).strftime("%Y-%m-%d")
            if latest_transition is not None
            else "-"
        ),
        "策略区间涨幅(%)": interval_return_pct,
        "上一状态转换时间": (
            pd.Timestamp(previous_transition["date"]).strftime("%Y-%m-%d")
            if previous_transition is not None
            else "-"
        ),
        "策略上一区间涨幅(%)": previous_interval_return_pct,
    }


def build_recent_etf_operation_guidance(
    items: list[PositionItem],
    *,
    days: int = 7,
) -> pd.DataFrame:
    columns = ["日期", "ETF名称", "代码", "策略参数", "操作指引", "操作后仓位", "触发收盘价"]
    latest_dates = []
    for item in items:
        if (
            item.category != "ETF"
            or not item.formal_history_valid
            or item.dataframe is None
            or item.dataframe.empty
        ):
            continue
        dates = pd.to_datetime(item.dataframe.get("date"), errors="coerce").dropna()
        if not dates.empty:
            latest_dates.append(dates.max())
    if not latest_dates:
        return pd.DataFrame(columns=columns)

    end_date = max(latest_dates).normalize()
    start_date = end_date - pd.Timedelta(days=max(int(days), 1) - 1)
    rows = []
    parking_item = next(
        (
            item
            for item in items
            if item.category == "ETF" and normalize_etf_base_code(item.code) == "512890"
        ),
        None,
    )
    parking_prices = pd.Series(dtype="float64")
    if parking_item is not None and parking_item.dataframe is not None:
        parking_history = parking_item.dataframe.copy()
        if {"date", "price"}.issubset(parking_history.columns):
            parking_history["date"] = pd.to_datetime(
                parking_history["date"], errors="coerce"
            ).dt.normalize()
            parking_history["price"] = pd.to_numeric(parking_history["price"], errors="coerce")
            parking_history = (
                parking_history.dropna(subset=["date", "price"])
                .sort_values("date")
                .drop_duplicates(subset=["date"], keep="last")
            )
            parking_prices = parking_history.set_index("date")["price"]
    parking_buys: dict[pd.Timestamp, set[str]] = {}
    parking_sources_valid = not any(
        candidate.category == "ETF"
        and normalize_etf_base_code(candidate.code)
        in ETF_512890_ACTIVE_TRANSFER_SOURCE_CODES
        and not candidate.formal_history_valid
        for candidate in items
    )
    for item in items:
        if item.category != "ETF" or not item.formal_history_valid:
            continue
        base_code = normalize_etf_base_code(item.code)
        strategy = ETF_TIMING_STRATEGIES.get(base_code)
        if strategy is None or base_code in ETF_TIMING_TABLE_EXCLUDED_CODES:
            continue
        ma_period, threshold_pct = strategy
        transitions = calculate_etf_timing_transitions(
            item.dataframe,
            ma_period=ma_period,
            threshold_pct=threshold_pct,
        )
        if transitions.empty:
            continue
        transitions = transitions[
            (transitions["日期"] >= start_date) & (transitions["日期"] <= end_date)
        ]
        for _, transition in transitions.iterrows():
            raw_action = str(transition["原始信号"])
            action = etf_position_decision(base_code, raw_action)
            half_timing = ETF_POSITION_STRATEGIES.get(base_code) == "半仓持有半仓择时"
            post_position = (
                "持有"
                if raw_action == "买入" and half_timing
                else "半仓"
                if raw_action == "卖出" and half_timing
                else "持有"
                if raw_action == "买入"
                else "空仓"
            )
            rows.append(
                {
                    "日期": pd.Timestamp(transition["日期"]).strftime("%Y-%m-%d"),
                    "ETF名称": display_etf_name(base_code, item.name),
                    "代码": base_code,
                    "策略参数": f"MA{int(ma_period)} / {float(threshold_pct):.1f}%",
                    "操作指引": action,
                    "操作后仓位": post_position,
                    "触发收盘价": round(float(transition["收盘价"]), 3),
                }
            )
            if (
                raw_action == "卖出"
                and parking_sources_valid
                and base_code in ETF_512890_TRANSFER_SOURCE_CODES
                and ETF_PORTFOLIO_WEIGHTS_PCT.get(base_code, 0) > 0
            ):
                transition_date = pd.Timestamp(transition["日期"]).normalize()
                parking_buys.setdefault(transition_date, set()).add(base_code)
    for transition_date, source_codes in parking_buys.items():
        parking_price = pd.to_numeric(parking_prices.get(transition_date), errors="coerce")
        rows.append(
            {
                "日期": transition_date.strftime("%Y-%m-%d"),
                "ETF名称": ETF_DISPLAY_NAMES["512890"],
                "代码": "512890",
                "策略参数": f"承接{'、'.join(sorted(source_codes))}空仓资金",
                "操作指引": "买入",
                "操作后仓位": "持有",
                "触发收盘价": (
                    round(float(parking_price), 3) if pd.notna(parking_price) else pd.NA
                ),
            }
        )
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns).sort_values(
        ["日期", "代码"], ascending=[False, True]
    ).reset_index(drop=True)


def build_etf_timing_table(items: list[PositionItem]) -> pd.DataFrame:
    columns = [
        "ETF名称",
        "代码",
        "组合权重比例",
        "最新价",
        "当日涨跌幅(%)",
        "策略参数",
        "对应均线",
        "偏离率(%)",
        "择时判断",
        "状态转换时间",
        "区间涨幅(%)",
        "上一状态转换时间",
        "上一区间涨幅(%)",
    ]
    rows = []
    parking_snapshot = calculate_512890_parking_snapshot(items)
    for item in items:
        if item.category != "ETF":
            continue
        base_code = normalize_etf_base_code(item.code)
        is_parking_etf = base_code == "512890"
        row = {
            "ETF名称": display_etf_name(base_code, item.name),
            "代码": base_code,
            "组合权重比例": (
                parking_snapshot.get("组合权重比例", "-")
                if is_parking_etf
                else f"{ETF_PORTFOLIO_WEIGHTS_PCT.get(base_code, 0):g}%"
            ),
            "最新价": item.metrics.get("最新价"),
            "当日涨跌幅(%)": item.metrics.get("日涨跌(%)"),
            "策略参数": "-" if is_parking_etf else pd.NA,
            "对应均线": "-" if is_parking_etf else pd.NA,
            "偏离率(%)": "-" if is_parking_etf else pd.NA,
            "择时判断": "-" if is_parking_etf else pd.NA,
            "状态转换时间": "-" if is_parking_etf else pd.NA,
            "区间涨幅(%)": "-" if is_parking_etf else pd.NA,
            "上一状态转换时间": "-" if is_parking_etf else pd.NA,
            "上一区间涨幅(%)": "-" if is_parking_etf else pd.NA,
        }
        if is_parking_etf:
            row.update(
                {
                    "择时判断": parking_snapshot.get("择时判断", "-"),
                    "状态转换时间": parking_snapshot.get("状态转换时间", "-"),
                    "区间涨幅(%)": parking_snapshot.get("策略区间涨幅(%)", pd.NA),
                    "上一状态转换时间": parking_snapshot.get("上一状态转换时间", "-"),
                    "上一区间涨幅(%)": parking_snapshot.get(
                        "策略上一区间涨幅(%)", pd.NA
                    ),
                }
            )
        if base_code in ETF_TIMING_STRATEGIES:
            ma_period, threshold_pct = ETF_TIMING_STRATEGIES[base_code]
            row.update(
                {
                    "策略参数": item.metrics.get(
                        "策略参数",
                        f"MA{int(ma_period)} / {float(threshold_pct):.1f}%",
                    ),
                    "对应均线": item.metrics.get("策略均线", pd.NA),
                    "偏离率(%)": item.metrics.get("策略偏离(%)", pd.NA),
                    "择时判断": etf_position_decision(
                        base_code,
                        item.metrics.get("择时判断", pd.NA),
                    ),
                    "状态转换时间": item.metrics.get("状态转换时间", pd.NA),
                    "区间涨幅(%)": item.metrics.get("策略区间涨幅(%)", pd.NA),
                    "上一状态转换时间": item.metrics.get("上一状态转换时间", pd.NA),
                    "上一区间涨幅(%)": item.metrics.get("策略上一区间涨幅(%)", pd.NA),
                }
            )
        rows.append(row)
    if not rows:
        return pd.DataFrame(columns=columns)
    result = pd.DataFrame(rows, columns=columns)
    result["_sort_deviation"] = pd.to_numeric(result["偏离率(%)"], errors="coerce")
    return result.sort_values("_sort_deviation", ascending=False, na_position="last").drop(
        columns="_sort_deviation"
    ).reset_index(drop=True)
