import html
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from core.db import init_db
from core.ui import (
    DEFAULT_CHART_HEIGHT,
    LARGE_CHART_HEIGHT,
    SECONDARY_CHART_HEIGHT,
    apply_global_style,
    apply_plotly_layout,
    render_metric_grid,
    render_page_header,
)
from services.fund_analysis import (
    FUND_ADJUSTMENT_OPTIONS,
    calculate_current_drawdown_info,
    calculate_max_drawdown_info,
    calculate_yearly_drawdowns,
    extract_drawdown_periods,
)
from services.position_analysis import (
    DEFAULT_ETF_CODES,
    DEFAULT_OPTION_CODES,
    DEFAULT_SPREAD_GROUPS,
    ETF_MIDSESSION_TIMING_REFRESH_SECONDS,
    ETF_MORNING_TIMING_REFRESH_SECONDS,
    ETF_REALTIME_TIMING_END_TIME,
    ETF_REALTIME_TIMING_REFRESH_SECONDS,
    PositionItem,
    apply_etf_realtime_quote,
    apply_etf_realtime_quotes_to_items,
    apply_etf_realtime_quote_to_timing,
    build_recent_etf_operation_guidance,
    build_etf_timing_table,
    etf_afternoon_timing_fetch_ready,
    etf_final_close_ready,
    etf_intraday_quote_ready,
    etf_lunch_timing_fetch_ready,
    etf_lunch_timing_preview_ready,
    etf_morning_timing_fetch_ready,
    etf_morning_timing_preview_ready,
    etf_realtime_timing_ready,
    fetch_tickflow_etf_quotes,
    filter_current_etf_realtime_quotes,
    latest_final_etf_trade_date,
    load_runtime_etf_quotes,
    load_or_fetch_etf,
    load_or_fetch_option,
    load_or_fetch_spread,
    normalize_etf_base_code,
    parse_position_codes,
    parse_spread_groups,
    refresh_position_derivative_items,
    remember_runtime_etf_quotes,
)


st.set_page_config(page_title="持仓分析", layout="wide")
init_db()
apply_global_style()
st.markdown(
    """
    <style>
    div[data-testid="stElementContainer"][data-stale="true"] {
        opacity: 1 !important;
        transition: none !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

render_page_header(
    "持仓分析",
    "按指数监控的方式展示个人持仓标的。早盘每10分钟、盘中每30分钟、尾盘每2分钟更新卡片与择时预判。",
    eyebrow="Positions",
)


def format_number(value: object, digits: int = 2, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "-"
    if isinstance(value, (int, float)):
        return f"{float(value):.{digits}f}{suffix}"
    return str(value)


def display_digits(item: PositionItem, metric_name: str) -> int:
    if item.category == "ETF" and metric_name == "最新价":
        return 3
    if item.category in {"期货价差", "期权"}:
        if metric_name in {"最新成交量", "最新持仓量"}:
            return 0
        return 1
    return 2


def format_metric_for_item(item: PositionItem, metric_name: str) -> str:
    value = item.metrics.get(metric_name)
    return format_number(value, digits=display_digits(item, metric_name))


def position_key(item: PositionItem) -> str:
    return f"{item.category}::{item.code}"


def display_position_code(item: PositionItem) -> str:
    return normalize_etf_base_code(item.code) if item.category == "ETF" else item.code


def get_query_position_detail(items: list[PositionItem]) -> str | None:
    keys = {position_key(item) for item in items}
    value = st.query_params.get("position_detail")
    if isinstance(value, list):
        value = value[0] if value else None
    if not value:
        return None
    detail_key = unquote(str(value))
    return detail_key if detail_key in keys else None


def clear_position_detail() -> None:
    st.query_params.clear()


def build_overview_table(items: list[PositionItem]) -> pd.DataFrame:
    metric_order = [
        "最新价",
        "最新收盘",
        "最新价差",
        "日涨跌(%)",
        "价差日变化",
        "最新占比(%)",
        "20日涨跌(%)",
        "60日涨跌(%)",
        "20日波动(%)",
        "MA20偏离(%)",
        "价格百分位",
        "最新成交量",
        "最新持仓量",
    ]
    rows = []
    for item in items:
        for key in item.metrics:
            if key not in metric_order:
                metric_order.append(key)
        row = {
            "类别": item.category,
            "代码": display_position_code(item),
            "名称": item.name,
            "状态": item.status,
            "最新日期": item.latest_date or "-",
            "来源": item.source or "-",
            "缓存时间": item.cache_time or "-",
            "备注": item.error or "",
        }
        for key in metric_order:
            row[key] = format_metric_for_item(item, key)
        rows.append(row)
    return pd.DataFrame(rows)


def format_etf_table_value(column: str, value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    if text == "-":
        return text
    try:
        if column in {"最新价", "对应均线", "触发收盘价"}:
            return format(
                Decimal(text).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP),
                ".3f",
            )
        if column in {"当日涨跌幅(%)", "偏离率(%)", "区间涨幅(%)", "上一区间涨幅(%)"}:
            return format(
                Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                ".2f",
            )
    except (InvalidOperation, ValueError):
        return text
    return text


def render_etf_timing_table(df: pd.DataFrame) -> None:
    if df is None or df.empty:
        st.info("当前没有可展示的 ETF 汇总数据。")
        return
    headers = "".join(f"<th>{html.escape(str(column))}</th>" for column in df.columns)
    timing_column_index = df.columns.get_loc("择时判断") + 1
    rows = []
    for _, row in df.iterrows():
        timing_action = str(row.get("择时判断", ""))
        row_class = (
            " timing-buy"
            if timing_action in {"买入", "加至满仓"}
            else " timing-sell"
            if timing_action in {"卖出", "降至半仓"}
            else ""
        )
        cells = "".join(
            f"<td>{html.escape(format_etf_table_value(column, row[column]))}</td>"
            for column in df.columns
        )
        rows.append(f'<tr class="{row_class.strip()}">{cells}</tr>')
    st.markdown(
        f"""
        <style>
        .position-etf-summary-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.92rem;
        }}
        .position-etf-summary-table-scroll {{
            width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }}
        .position-etf-summary-table th,
        .position-etf-summary-table td {{
            text-align: center;
            padding: 0.45rem 0.6rem;
            border-bottom: 1px solid rgba(49, 51, 63, 0.12);
            white-space: nowrap;
        }}
        .position-etf-summary-table th {{
            font-weight: 600;
            background: rgba(49, 51, 63, 0.04);
        }}
        .position-etf-summary-table tr.timing-buy td {{
            background: rgba(254, 226, 226, 0.72);
        }}
        .position-etf-summary-table tr.timing-sell td {{
            background: rgba(220, 252, 231, 0.78);
        }}
        .position-etf-summary-table tr.timing-buy td:first-child,
        .position-etf-summary-table tr.timing-buy td:nth-child({timing_column_index}) {{
            color: rgb(190, 18, 60);
            font-weight: 700;
        }}
        .position-etf-summary-table tr.timing-sell td:first-child,
        .position-etf-summary-table tr.timing-sell td:nth-child({timing_column_index}) {{
            color: rgb(22, 101, 52);
            font-weight: 700;
        }}
        </style>
        <div class="position-etf-summary-table-scroll">
        <table class="position-etf-summary-table">
            <thead><tr>{headers}</tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_etf_operation_guidance(df: pd.DataFrame) -> None:
    headers = "".join(f"<th>{html.escape(str(column))}</th>" for column in df.columns)
    rows = []
    for _, row in df.iterrows():
        action = str(row.get("操作指引", ""))
        row_class = (
            "timing-buy"
            if action in {"买入", "加至满仓"}
            else "timing-sell"
            if action in {"卖出", "降至半仓"}
            else ""
        )
        cells = "".join(
            f"<td>{html.escape(format_etf_table_value(column, row[column]))}</td>"
            for column in df.columns
        )
        rows.append(f'<tr class="{row_class}">{cells}</tr>')
    st.markdown(
        f"""
        <style>
        .position-operation-guidance-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.92rem;
        }}
        .position-operation-guidance-table-scroll {{
            width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }}
        .position-operation-guidance-table th,
        .position-operation-guidance-table td {{
            text-align: center;
            padding: 0.45rem 0.6rem;
            border-bottom: 1px solid rgba(49, 51, 63, 0.12);
            white-space: nowrap;
        }}
        .position-operation-guidance-table th {{
            font-weight: 600;
            background: rgba(49, 51, 63, 0.04);
        }}
        .position-operation-guidance-table tr.timing-buy td {{
            background: rgba(254, 226, 226, 0.72);
        }}
        .position-operation-guidance-table tr.timing-sell td {{
            background: rgba(220, 252, 231, 0.78);
        }}
        .position-operation-guidance-table tr.timing-buy td:nth-child(5) {{
            color: rgb(190, 18, 60);
            font-weight: 700;
        }}
        .position-operation-guidance-table tr.timing-sell td:nth-child(5) {{
            color: rgb(22, 101, 52);
            font-weight: 700;
        }}
        </style>
        <div class="position-operation-guidance-table-scroll">
        <table class="position-operation-guidance-table">
            <thead><tr>{headers}</tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


@st.fragment(run_every=f"{ETF_REALTIME_TIMING_REFRESH_SECONDS}s")
def render_etf_timing_section(
    etf_codes: list[str],
    *,
    position_items: list[PositionItem],
    show_cache_caption: bool,
    api_key: str,
    count: int,
    market_count: int,
    max_workers: int,
    adjust: str | None,
    updates_enabled: bool,
    save_to_cache: bool,
) -> None:
    market_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    formal_items = [
        load_or_fetch_etf(
            code,
            api_key=api_key,
            count=count,
            adjust=adjust,
            allow_fetch=False,
            market_now=market_now,
        )
        for code in etf_codes
    ]
    target_date = latest_final_etf_trade_date(market_now)
    stale_codes = []
    for code, item in zip(etf_codes, formal_items):
        item_date = pd.to_datetime(item.latest_date, errors="coerce")
        if pd.isna(item_date) or item_date.date() < target_date:
            stale_codes.append(code)

    attempt_key = "position_etf_auto_final_last_attempt"
    last_attempt = pd.to_datetime(st.session_state.get(attempt_key), errors="coerce")
    retry_ready = pd.isna(last_attempt) or (market_now.replace(tzinfo=None) - last_attempt).total_seconds() >= 600
    if (
        updates_enabled
        and etf_final_close_ready(market_now)
        and stale_codes
        and retry_ready
    ):
        with st.spinner(f"正在自动更新 {target_date:%Y-%m-%d} ETF收盘数据..."):
            refreshed_by_code = {
                code: load_or_fetch_etf(
                    code,
                    api_key=api_key,
                    count=count,
                    adjust=adjust,
                    allow_fetch=True,
                    force_refresh=True,
                    save_to_cache=save_to_cache,
                    market_now=market_now,
                )
                for code in stale_codes
            }
            formal_items = [
                refreshed_by_code.get(code, item)
                for code, item in zip(etf_codes, formal_items)
            ]
        st.session_state[attempt_key] = market_now.replace(tzinfo=None).isoformat()

    formal_close_missing = any(
        pd.isna(item_date := pd.to_datetime(item.latest_date, errors="coerce"))
        or item_date.date() < target_date
        for item in formal_items
    )

    timing_items = formal_items
    active_preview_quotes = filter_current_etf_realtime_quotes(
        load_runtime_etf_quotes(),
        market_now=market_now,
        retain_after_close=True,
    )
    if etf_final_close_ready(market_now) and not formal_close_missing:
        active_preview_quotes = {}
    derivative_refresh_due = False
    realtime_timing_error = ""
    missing_realtime_codes: list[str] = []
    morning_preview_key = "position_etf_morning_timing_preview"
    lunch_preview_key = "position_etf_lunch_timing_preview"
    preview_date = market_now.date().isoformat()
    market_now_naive = market_now.replace(tzinfo=None)
    morning_preview_state = st.session_state.get(morning_preview_key, {})
    same_morning_date = morning_preview_state.get("trade_date") == preview_date
    morning_quotes = morning_preview_state.get("quotes", {}) if same_morning_date else {}
    if updates_enabled and etf_morning_timing_fetch_ready(market_now):
        morning_refresh_band = (
            "early" if market_now.time() < datetime.strptime("10:00", "%H:%M").time() else "midmorning"
        )
        morning_refresh_seconds = (
            ETF_MORNING_TIMING_REFRESH_SECONDS
            if morning_refresh_band == "early"
            else ETF_MIDSESSION_TIMING_REFRESH_SECONDS
        )
        last_morning_fetch = pd.to_datetime(
            morning_preview_state.get("fetched_at"), errors="coerce"
        )
        morning_refresh_due = (
            not same_morning_date
            or morning_preview_state.get("refresh_band") != morning_refresh_band
            or pd.isna(last_morning_fetch)
            or (market_now_naive - last_morning_fetch).total_seconds()
            >= morning_refresh_seconds
        )
        if morning_refresh_due:
            derivative_refresh_due = True
            if api_key:
                previous_quotes = morning_quotes
                try:
                    morning_quotes = fetch_tickflow_etf_quotes(
                        etf_codes,
                        api_key=api_key,
                        market_now=market_now,
                    )
                    remember_runtime_etf_quotes(morning_quotes)
                    morning_preview_state = {
                        "trade_date": preview_date,
                        "quotes": morning_quotes,
                        "error": "",
                        "fetched_at": market_now_naive.isoformat(),
                        "refresh_band": morning_refresh_band,
                    }
                except Exception as exc:
                    morning_preview_state = {
                        "trade_date": preview_date,
                        "quotes": previous_quotes,
                        "error": str(exc),
                        "fetched_at": market_now_naive.isoformat(),
                        "refresh_band": morning_refresh_band,
                    }
                st.session_state[morning_preview_key] = morning_preview_state
            else:
                realtime_timing_error = "未填写TickFlow API Key"

    if etf_morning_timing_preview_ready(market_now):
        realtime_timing_error = (
            realtime_timing_error or morning_preview_state.get("error", "")
        )
        if morning_quotes:
            active_preview_quotes = morning_quotes
            timing_items = [
                apply_etf_realtime_quote_to_timing(
                    item,
                    morning_quotes.get(normalize_etf_base_code(code), {}),
                    market_now=market_now,
                )
                for code, item in zip(etf_codes, formal_items)
            ]
            missing_realtime_codes = [
                normalize_etf_base_code(code)
                for code in etf_codes
                if normalize_etf_base_code(code) not in morning_quotes
            ]

    lunch_preview_state = st.session_state.get(lunch_preview_key, {})
    same_lunch_date = lunch_preview_state.get("trade_date") == preview_date
    lunch_quotes = lunch_preview_state.get("quotes", {}) if same_lunch_date else {}
    lunch_fetch_ready = etf_lunch_timing_fetch_ready(market_now)
    afternoon_fetch_ready = etf_afternoon_timing_fetch_ready(market_now)
    lunch_refresh_due = False
    lunch_refresh_band = ""
    if updates_enabled and lunch_fetch_ready and not lunch_quotes:
        lunch_refresh_band = "lunch"
        last_lunch_attempt = pd.to_datetime(
            lunch_preview_state.get("fetched_at"), errors="coerce"
        )
        lunch_refresh_due = (
            pd.isna(last_lunch_attempt)
            or (market_now_naive - last_lunch_attempt).total_seconds() >= 600
        )
    elif updates_enabled and afternoon_fetch_ready:
        lunch_refresh_band = "afternoon"
        last_afternoon_fetch = pd.to_datetime(
            lunch_preview_state.get("fetched_at"), errors="coerce"
        )
        lunch_refresh_due = (
            not same_lunch_date
            or lunch_preview_state.get("refresh_band") != lunch_refresh_band
            or pd.isna(last_afternoon_fetch)
            or (market_now_naive - last_afternoon_fetch).total_seconds()
            >= ETF_MIDSESSION_TIMING_REFRESH_SECONDS
        )
    if lunch_refresh_due:
        derivative_refresh_due = True
        if api_key:
            previous_quotes = lunch_quotes
            try:
                lunch_quotes = fetch_tickflow_etf_quotes(
                    etf_codes,
                    api_key=api_key,
                    market_now=market_now,
                )
                remember_runtime_etf_quotes(lunch_quotes)
                lunch_preview_state = {
                    "trade_date": preview_date,
                    "quotes": lunch_quotes,
                    "error": "",
                    "fetched_at": market_now_naive.isoformat(),
                    "refresh_band": lunch_refresh_band,
                }
            except Exception as exc:
                lunch_preview_state = {
                    "trade_date": preview_date,
                    "quotes": previous_quotes,
                    "error": str(exc),
                    "fetched_at": market_now_naive.isoformat(),
                    "refresh_band": lunch_refresh_band,
                }
            st.session_state[lunch_preview_key] = lunch_preview_state
        else:
            realtime_timing_error = "未填写TickFlow API Key"

    if etf_lunch_timing_preview_ready(market_now):
        realtime_timing_error = (
            realtime_timing_error or lunch_preview_state.get("error", "")
        )

    if etf_lunch_timing_preview_ready(market_now) and lunch_quotes:
        active_preview_quotes = lunch_quotes
        timing_items = [
            apply_etf_realtime_quote_to_timing(
                item,
                lunch_quotes.get(normalize_etf_base_code(code), {}),
                market_now=market_now,
            )
            for code, item in zip(etf_codes, formal_items)
        ]
        missing_realtime_codes = [
            normalize_etf_base_code(code)
            for code in etf_codes
            if normalize_etf_base_code(code) not in lunch_quotes
        ]

    if updates_enabled and etf_realtime_timing_ready(market_now):
        preview_key = "position_etf_realtime_timing_preview"
        preview_state = st.session_state.get(preview_key, {})
        same_preview_date = preview_state.get("trade_date") == preview_date
        last_preview_fetch = pd.to_datetime(
            preview_state.get("fetched_at"), errors="coerce"
        )
        preview_refresh_due = (
            not same_preview_date
            or pd.isna(last_preview_fetch)
            or (market_now_naive - last_preview_fetch).total_seconds()
            >= ETF_REALTIME_TIMING_REFRESH_SECONDS
        )
        if preview_refresh_due:
            derivative_refresh_due = True
            if api_key:
                previous_quotes = preview_state.get("quotes", {}) if same_preview_date else {}
                try:
                    realtime_quotes = fetch_tickflow_etf_quotes(
                        etf_codes,
                        api_key=api_key,
                        market_now=market_now,
                    )
                    remember_runtime_etf_quotes(realtime_quotes)
                    preview_state = {
                        "trade_date": preview_date,
                        "quotes": realtime_quotes,
                        "error": "",
                        "fetched_at": market_now_naive.isoformat(),
                    }
                except Exception as exc:
                    preview_state = {
                        "trade_date": preview_date,
                        "quotes": previous_quotes,
                        "error": str(exc),
                        "fetched_at": market_now_naive.isoformat(),
                    }
                st.session_state[preview_key] = preview_state
            else:
                realtime_timing_error = "未填写TickFlow API Key"

        realtime_quotes = preview_state.get("quotes", {})
        if realtime_quotes:
            active_preview_quotes = realtime_quotes
        realtime_timing_error = realtime_timing_error or preview_state.get("error", "")
        missing_realtime_codes = [
            normalize_etf_base_code(code)
            for code in etf_codes
            if normalize_etf_base_code(code) not in realtime_quotes
        ]
        timing_items = [
            apply_etf_realtime_quote_to_timing(
                item,
                active_preview_quotes.get(normalize_etf_base_code(code), {}),
                market_now=market_now,
            )
            for code, item in zip(etf_codes, formal_items)
        ]

    timing_preview_window = bool(
        etf_intraday_quote_ready(market_now)
        or (etf_final_close_ready(market_now) and formal_close_missing)
    )
    if timing_preview_window and active_preview_quotes:
        timing_items = [
            apply_etf_realtime_quote_to_timing(
                item,
                active_preview_quotes.get(normalize_etf_base_code(code), {}),
                market_now=market_now,
                allow_close_retention=(
                    market_now.time() >= ETF_REALTIME_TIMING_END_TIME
                ),
            )
            for code, item in zip(etf_codes, formal_items)
        ]
        missing_realtime_codes = [
            normalize_etf_base_code(code)
            for code in etf_codes
            if normalize_etf_base_code(code) not in active_preview_quotes
        ]

    timing_preview_active = (
        updates_enabled
        and timing_preview_window
    )
    derivative_state_key = "position_derivative_realtime_preview"
    derivative_state = st.session_state.get(derivative_state_key, {})
    derivative_preview_version = 2
    if (
        timing_preview_active
        and derivative_state.get("version") != derivative_preview_version
    ):
        derivative_refresh_due = True
    same_derivative_date = derivative_state.get("trade_date") == preview_date
    derivative_items = derivative_state.get("items", {}) if same_derivative_date else {}
    derivative_realtime_error = ""
    if derivative_refresh_due:
        refreshed_derivatives, derivative_errors = refresh_position_derivative_items(
            position_items,
            api_key=api_key,
            max_workers=max_workers,
            option_count=market_count,
            market_now=market_now,
        )
        derivative_items = dict(derivative_items)
        derivative_items.update(
            {position_key(item): item for item in refreshed_derivatives}
        )
        derivative_realtime_error = " | ".join(derivative_errors)
        derivative_fetched_at = (
            market_now_naive.isoformat()
            if refreshed_derivatives
            else derivative_state.get("fetched_at", "")
        )
        derivative_state = {
            "version": derivative_preview_version,
            "trade_date": preview_date,
            "items": derivative_items,
            "error": derivative_realtime_error,
            "fetched_at": derivative_fetched_at,
        }
        st.session_state[derivative_state_key] = derivative_state
        same_derivative_date = True
    else:
        derivative_realtime_error = derivative_state.get("error", "")
    outer_etf_items = {
        normalize_etf_base_code(item.code): item
        for item in position_items
        if item.category == "ETF"
    }
    formal_etf_items = {
        normalize_etf_base_code(item.code): item for item in formal_items
    }
    base_card_items: list[PositionItem] = []
    for item in position_items:
        if item.category != "ETF":
            base_card_items.append(item)
            continue
        code = normalize_etf_base_code(item.code)
        outer_item = outer_etf_items.get(code, item)
        base_item = (
            formal_etf_items.get(code, outer_item)
            if etf_final_close_ready(market_now) and not formal_close_missing
            else outer_item
            if outer_item.status == "盘中"
            else formal_etf_items.get(code, outer_item)
        )
        base_card_items.append(base_item)

    card_items = apply_etf_realtime_quotes_to_items(
        base_card_items,
        active_preview_quotes,
    )
    card_items = [
        derivative_items.get(position_key(item), item)
        if item.category in {"期货价差", "期权"}
        else item
        for item in card_items
    ]

    if show_cache_caption:
        quote_times: list[pd.Timestamp] = []
        quote_groups = [
            active_preview_quotes,
            morning_preview_state.get("quotes", {}),
            lunch_preview_state.get("quotes", {}),
            st.session_state.get("position_etf_realtime_quotes", {}),
        ]
        for quote_group in quote_groups:
            for quote in quote_group.values():
                quote_time = pd.to_datetime(quote.get("quote_time"), errors="coerce")
                if pd.isna(quote_time):
                    continue
                if quote_time.tzinfo is not None:
                    quote_time = quote_time.tz_convert("Asia/Shanghai").tz_localize(None)
                if quote_time.date() == market_now.date():
                    quote_times.append(quote_time)
        derivative_update_time = pd.to_datetime(
            derivative_state.get("fetched_at"), errors="coerce"
        )
        if same_derivative_date and not pd.isna(derivative_update_time):
            quote_times.append(derivative_update_time)
        realtime_update_text = (
            max(quote_times).strftime("%Y-%m-%d %H:%M:%S")
            if quote_times
            else "-"
        )
        st.caption(
            "当前为缓存视图；9:30-10:00每10分钟，10:00-11:30和13:00-14:50每30分钟，"
            "午间收盘更新一次，14:50-15:00每2分钟更新卡片与择时预判，"
            "交易日15:05后才写入ETF日线并正式更新择时表格。"
            f"本次实时更新时间为：{realtime_update_text}。"
        )

    render_position_cards(card_items)
    if derivative_realtime_error:
        st.warning(
            "期货价差/期权实时更新部分失败，继续显示上次有效数据："
            f"{derivative_realtime_error}"
        )

    st.subheader("ETF择时状态")
    render_etf_timing_table(build_etf_timing_table(timing_items))
    if timing_preview_active:
        if realtime_timing_error:
            st.warning(
                "ETF择时实时预判获取失败，继续显示上次预判或正式日线："
                f"{realtime_timing_error}"
            )
        elif missing_realtime_codes:
            st.warning(
                "以下ETF未返回实时行情，继续显示正式日线："
                + "、".join(missing_realtime_codes)
            )
        if etf_morning_timing_preview_ready(market_now):
            if market_now.time() < datetime.strptime("10:00", "%H:%M").time():
                st.caption(
                    "交易日9:30-10:00每10分钟更新ETF卡片和择时预判；"
                    "实时价格不写入缓存。"
                )
            else:
                st.caption(
                    "交易日10:00-11:30每30分钟更新ETF卡片和择时预判；"
                    "实时价格不写入缓存。"
                )
        elif etf_realtime_timing_ready(market_now):
            st.caption(
                "14:50-15:00每2分钟更新一次实时行情并预判择时状态；"
                "实时价格不写入缓存，也不参与近一周操作指引。"
            )
        else:
            if etf_afternoon_timing_fetch_ready(market_now):
                st.caption(
                    "交易日13:00-14:50每30分钟更新ETF卡片和择时预判；"
                    "实时价格不写入缓存。"
                )
            else:
                st.caption(
                    "交易日午间收盘后获取一次午间价格并预判择时状态；"
                    "午间价格不写入缓存，也不参与近一周操作指引。"
                )

    st.subheader("近一周操作指引")
    guidance_df = build_recent_etf_operation_guidance(formal_items, days=7)
    if guidance_df.empty:
        st.info("最近7个自然日没有新的调仓指引，继续按上方当前仓位执行。")
    else:
        render_etf_operation_guidance(guidance_df)
    st.caption("依据正式收盘日线计算；盘中实时报价不参与，展示窗口以最新正式交易日为截止日。")


def primary_value(item: PositionItem) -> tuple[str, object, int]:
    if item.category == "ETF":
        return "最新价", item.metrics.get("最新价"), 3
    if item.category == "期货价差":
        return "最新价差", item.metrics.get("最新价差"), 1
    return "最新收盘", item.metrics.get("最新收盘"), 1


def delta_value(item: PositionItem) -> tuple[str, object, str]:
    if item.category == "期货价差":
        return "日变化", item.metrics.get("价差日变化"), ""
    return "日涨跌", item.metrics.get("日涨跌(%)"), "%"


def render_position_cards(items: list[PositionItem]) -> None:
    for start in range(0, len(items), 4):
        columns = st.columns(4)
        for col, item in zip(columns, items[start : start + 4]):
            with col:
                render_position_card(item)


def render_position_card(item: PositionItem) -> None:
    _, value, digits = primary_value(item)
    delta_label, delta, delta_suffix = delta_value(item)
    is_available = item.status not in {"失败", "无缓存"} and not item.dataframe.empty
    detail_href = f"?position_detail={quote(position_key(item))}"

    if pd.isna(delta):
        delta_class = "neutral"
        arrow = "·"
        delta_text = "-"
    else:
        delta_class = "positive" if float(delta) >= 0 else "negative"
        arrow = "↑" if float(delta) >= 0 else "↓"
        delta_digits = 1 if item.category in {"期货价差", "期权"} else 2
        delta_text = f"{float(delta):+.{delta_digits}f}{delta_suffix}"

    if is_available:
        value_text = format_number(value, digits=digits)
        status_text = item.latest_date or item.status
    else:
        value_text = "暂无缓存" if item.status == "无缓存" else "获取失败"
        status_text = item.error or item.status

    title = item.name if item.name else item.code
    card_html = (
        "<style>"
        ".position-card-single{min-height:12.25rem;border:1px solid rgba(49,51,63,.14);border-radius:8px;background:rgba(255,255,255,.78);padding:1.25rem 1.35rem;box-shadow:0 10px 26px rgba(15,23,42,.06);margin:.35rem 0 .75rem;}"
        ".position-card-single:hover{border-color:rgba(37,99,235,.42);box-shadow:0 14px 30px rgba(15,23,42,.11);}"
        ".position-card-title{min-height:2.45rem;font-size:1.08rem;font-weight:700;line-height:1.25;overflow-wrap:anywhere;}"
        ".position-card-title a{color:rgba(49,51,63,.72);text-decoration:none;}"
        ".position-card-title a:hover{color:rgb(37,99,235);text-decoration:none;}"
        ".position-card-code{min-height:1.6rem;margin-top:.22rem;color:rgba(49,51,63,.58);font-size:.98rem;line-height:1.25;overflow-wrap:anywhere;}"
        ".position-card-value{margin-top:1rem;color:rgb(31,41,55);font-size:1.7rem;font-weight:650;line-height:1.08;font-variant-numeric:tabular-nums;white-space:normal;overflow-wrap:anywhere;}"
        ".position-card-foot{display:flex;align-items:center;justify-content:space-between;gap:.5rem;margin-top:1rem;}"
        ".position-card-delta{display:inline-flex;align-items:center;justify-content:center;gap:.28rem;border-radius:999px;padding:.32rem .75rem;font-size:.98rem;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap;}"
        ".position-card-delta.positive{color:rgb(190,18,60);background:rgba(254,226,226,.9);}"
        ".position-card-delta.negative{color:rgb(22,101,52);background:rgba(220,252,231,.9);}"
        ".position-card-delta.neutral{color:rgb(75,85,99);background:rgba(243,244,246,.95);}"
        ".position-card-date{min-width:0;color:rgba(49,51,63,.58);font-size:.84rem;line-height:1.25;text-align:right;overflow-wrap:anywhere;}"
        "</style>"
        '<div class="position-card-single">'
        f'<div class="position-card-title"><a href="{detail_href}">{html.escape(title)}</a></div>'
        f'<div class="position-card-code">{html.escape(item.category)} · {html.escape(display_position_code(item))}</div>'
        f'<div class="position-card-value">{html.escape(value_text)}</div>'
        '<div class="position-card-foot">'
        f'<div class="position-card-delta {delta_class}" title="{html.escape(delta_label)}">'
        f"<span>{arrow}</span><span>{html.escape(delta_text)}</span>"
        "</div>"
        f'<div class="position-card-date">{html.escape(status_text)}</div>'
        "</div>"
        "</div>"
    )
    st.markdown(card_html, unsafe_allow_html=True)


def filter_range(df: pd.DataFrame, date_col: str, range_label: str) -> pd.DataFrame:
    if df.empty or date_col not in df.columns:
        return df
    dates = pd.to_datetime(df[date_col], errors="coerce")
    latest_date = dates.max()
    if pd.isna(latest_date):
        return df
    if range_label == "今年来":
        start_date = pd.Timestamp(year=latest_date.year, month=1, day=1)
    elif range_label == "近3年":
        start_date = latest_date - pd.DateOffset(years=3)
    elif range_label == "近5年":
        start_date = latest_date - pd.DateOffset(years=5)
    elif range_label == "成立来":
        return df
    else:
        start_date = latest_date - pd.DateOffset(years=1)
    return df[dates >= start_date].copy()


def metric_row(items: list[tuple[str, object, str, int]]) -> None:
    columns = st.columns(len(items))
    for column, (label, value, suffix, digits) in zip(columns, items):
        column.metric(label, format_number(value, digits=digits, suffix=suffix))


def render_summary_table(rows: list[tuple[str, object]]) -> None:
    summary_df = pd.DataFrame(rows, columns=["指标", "数值"])
    summary_df["数值"] = summary_df["数值"].map(lambda value: "-" if value is None or pd.isna(value) else str(value))
    st.dataframe(summary_df, use_container_width=True, hide_index=True)


def round_numeric_columns(df: pd.DataFrame, digits: int = 1, integer_columns: tuple[str, ...] = ()) -> pd.DataFrame:
    result = df.copy()
    for column in result.select_dtypes(include="number").columns:
        if column in integer_columns:
            result[column] = result[column].round(0)
        else:
            result[column] = result[column].round(digits)
    return result


def rolling_annual_label(df: pd.DataFrame) -> str:
    if len(df) >= 252 * 3:
        return "三年滚动年化收益率(%)"
    if len(df) >= 252:
        return "一年滚动年化收益率(%)"
    return "滚动年化收益率(%)"


def format_pct(value: object, digits: int = 2) -> str:
    return format_number(value, digits=digits, suffix="%")


def render_etf_detail(item: PositionItem) -> None:
    df = item.dataframe.copy()
    if df.empty:
        st.info(item.error or "当前没有可展示的 ETF 数据。")
        return
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "price"]).sort_values("date")
    latest = df.iloc[-1]
    metric_row(
        [
            ("最新价", item.metrics.get("最新价"), "", 3),
            ("日涨跌", item.metrics.get("日涨跌(%)"), "%", 2),
            ("20日涨跌", item.metrics.get("20日涨跌(%)"), "%", 2),
            ("60日涨跌", item.metrics.get("60日涨跌(%)"), "%", 2),
            ("MA20偏离", item.metrics.get("MA20偏离(%)"), "%", 2),
            ("价格百分位", item.metrics.get("价格百分位"), "", 2),
        ]
    )

    range_label = st.segmented_control(
        "走势区间",
        options=["近一年", "今年来", "近3年", "近5年", "成立来"],
        default="近一年",
        key=f"position_range_{position_key(item)}",
    )
    view_df = filter_range(df, "date", range_label)
    if view_df.empty:
        view_df = df

    trend_tab, drawdown_tab, summary_tab, table_tab = st.tabs(["走势", "回撤", "摘要", "数据"])
    with trend_tab:
        rsi_col = next((col for col in df.columns if col.startswith("rsi_")), "rsi_14")
        rolling_label = rolling_annual_label(df)
        fig = make_subplots(
            rows=4,
            cols=1,
            shared_xaxes=True,
            row_heights=[0.50, 0.18, 0.16, 0.16],
            vertical_spacing=0.04,
            subplot_titles=("走势与均线", "RSI", "20日涨幅", rolling_label),
        )
        fig.add_trace(
            go.Scatter(x=view_df["date"], y=view_df["price"], mode="lines", name="价格", line={"width": 2}),
            row=1,
            col=1,
        )
        ma_colors = {20: "#eab308", 60: "#2563eb", 120: "#dc2626", 250: "#059669"}
        for period, color in ma_colors.items():
            ma_col = f"ma_{period}"
            if ma_col in view_df.columns:
                fig.add_trace(
                    go.Scatter(
                        x=view_df["date"],
                        y=view_df[ma_col],
                        mode="lines",
                        name=f"MA{period}",
                        line={"width": 1.3, "color": color},
                    ),
                    row=1,
                    col=1,
                )
        if rsi_col in view_df.columns:
            fig.add_trace(
                go.Scatter(x=view_df["date"], y=view_df[rsi_col], mode="lines", name="RSI"),
                row=2,
                col=1,
            )
            fig.add_hline(y=70, line_dash="dash", line_color="#dc2626", row=2, col=1)
            fig.add_hline(y=30, line_dash="dash", line_color="#059669", row=2, col=1)
        if "return_20d_pct" in view_df.columns:
            fig.add_trace(
                go.Scatter(x=view_df["date"], y=view_df["return_20d_pct"], mode="lines", name="20日涨幅(%)"),
                row=3,
                col=1,
            )
            fig.add_hline(y=0, line_color="#6b7280", row=3, col=1)
        if "rolling_annual_return_pct" in view_df.columns:
            fig.add_trace(
                go.Scatter(
                    x=view_df["date"],
                    y=view_df["rolling_annual_return_pct"],
                    mode="lines",
                    name=rolling_label,
                    line={"color": "#7c3aed"},
                ),
                row=4,
                col=1,
            )
            fig.add_hline(y=0, line_color="#6b7280", row=4, col=1)
        apply_plotly_layout(fig, height=LARGE_CHART_HEIGHT)
        fig.update_xaxes(hoverformat="%Y-%m-%d")
        fig.update_xaxes(rangeslider={"visible": True, "thickness": 0.06}, row=4, col=1)
        fig.update_layout(yaxis={"type": "log" if view_df["price"].min() > 0 and len(df) >= 252 * 3 else "linear", "title": "价格"})
        fig.update_yaxes(title_text="RSI", row=2, col=1, range=[0, 100])
        fig.update_yaxes(title_text="涨幅%", row=3, col=1)
        fig.update_yaxes(title_text="年化%", row=4, col=1)
        st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})

    with drawdown_tab:
        if "drawdown_pct" not in df.columns:
            st.info("当前数据没有回撤字段。")
        else:
            max_drawdown_info = calculate_max_drawdown_info(df)
            current_drawdown_info = calculate_current_drawdown_info(df)
            current_status = str(current_drawdown_info.get("当前修复状态") or "-")
            render_metric_grid(
                [
                    ("最大回撤", format_pct(max_drawdown_info.get("回撤深度(%)")), "历史最大回撤"),
                    ("谷底日期", str(max_drawdown_info.get("谷底日期") or "-"), f"峰值日期：{max_drawdown_info.get('峰值日期', '-')}"),
                    ("当前回撤", format_pct(current_drawdown_info.get("当前回撤(%)")), "最新交易日相对历史高点的回撤"),
                    ("当前回撤时间", format_number(current_drawdown_info.get("当前回撤时间"), digits=0, suffix="天"), "从当前回撤峰值日至最新交易日"),
                    (
                        "修复状态",
                        current_status,
                        f"当前回撤峰值日：{current_drawdown_info.get('当前回撤峰值日', '-')}；当前谷底日：{current_drawdown_info.get('当前谷底日', '-')}",
                    ),
                ]
            )

            dd_fig = make_subplots(
                rows=2,
                cols=1,
                shared_xaxes=True,
                row_heights=[0.62, 0.38],
                vertical_spacing=0.06,
                subplot_titles=("价格、历史峰值与回撤区域", "回撤曲线"),
            )
            dd_fig.add_trace(
                go.Scatter(x=df["date"], y=df["price"], mode="lines", name="价格", line={"color": "#2563eb"}),
                row=1,
                col=1,
            )
            if "running_peak" in df.columns:
                dd_fig.add_trace(
                    go.Scatter(
                        x=df["date"],
                        y=df["running_peak"],
                        mode="lines",
                        name="历史峰值",
                        line={"color": "#6b7280", "dash": "dash"},
                    ),
                    row=1,
                    col=1,
                )
                dd_fig.add_trace(
                    go.Scatter(
                        x=df["date"].tolist() + df["date"].tolist()[::-1],
                        y=df["running_peak"].tolist() + df["price"].tolist()[::-1],
                        fill="toself",
                        fillcolor="rgba(220,38,38,0.18)",
                        line={"color": "rgba(255,255,255,0)"},
                        hoverinfo="skip",
                        name="回撤区域",
                    ),
                    row=1,
                    col=1,
                )
            dd_fig.add_trace(
                go.Scatter(
                    x=df["date"],
                    y=df["drawdown_pct"],
                    mode="lines",
                    fill="tozeroy",
                    name="回撤(%)",
                    line={"color": "#dc2626"},
                ),
                row=2,
                col=1,
            )
            dd_fig.add_hline(y=0, line_color="#6b7280", row=2, col=1)
            trough_date = max_drawdown_info.get("谷底日期")
            if trough_date:
                trough_ts = pd.Timestamp(trough_date)
                trough_row = df[df["date"] == trough_ts]
                if not trough_row.empty:
                    dd_fig.add_trace(
                        go.Scatter(
                            x=[trough_row.iloc[0]["date"]],
                            y=[trough_row.iloc[0]["drawdown_pct"]],
                            mode="markers",
                            name="最大回撤谷底",
                            marker={"color": "#16a34a", "size": 10},
                        ),
                        row=2,
                        col=1,
                    )
            apply_plotly_layout(dd_fig, height=SECONDARY_CHART_HEIGHT)
            dd_fig.update_xaxes(hoverformat="%Y-%m-%d")
            dd_fig.update_yaxes(
                title_text="价格（对数）" if df["price"].min() > 0 and len(df) >= 252 * 3 else "价格",
                type="log" if df["price"].min() > 0 and len(df) >= 252 * 3 else "linear",
                row=1,
                col=1,
            )
            dd_fig.update_yaxes(title_text="回撤%", row=2, col=1)
            st.plotly_chart(dd_fig, use_container_width=True)

            drawdown_periods = extract_drawdown_periods(df)
            yearly_drawdowns = calculate_yearly_drawdowns(df)
            st.subheader("回撤波段")
            if drawdown_periods.empty:
                st.info("没有发现独立回撤波段。")
            else:
                st.dataframe(drawdown_periods, use_container_width=True, hide_index=True)

            st.subheader("年度最大回撤")
            if yearly_drawdowns.empty:
                st.info("没有年度回撤数据。")
            else:
                st.dataframe(yearly_drawdowns, use_container_width=True, hide_index=True)

    with summary_tab:
        summary_rows = [
            ("类别", item.category),
            ("代码", display_position_code(item)),
            ("名称", item.name),
            ("最新日期", item.latest_date),
            ("数据来源", item.source),
            ("缓存时间", item.cache_time or "-"),
            ("最新价", format_metric_for_item(item, "最新价")),
            ("日涨跌(%)", format_number(item.metrics.get("日涨跌(%)"))),
            ("20日涨跌(%)", format_number(item.metrics.get("20日涨跌(%)"))),
            ("年化波动(%)", format_number(item.metrics.get("年化波动(%)"))),
            (rolling_annual_label(df), format_number(latest.get("rolling_annual_return_pct"), suffix="%")),
            ("当前区间", range_label),
            ("区间样本", len(view_df)),
        ]
        render_summary_table(summary_rows)

    with table_tab:
        display_cols = [
            col
            for col in [
                "date",
                "price",
                "daily_return_pct",
                "return_20d_pct",
                "return_60d_pct",
                "rolling_annual_return_pct",
                "ma_20",
                "ma_60",
                "ma_120",
                "ma_250",
                "drawdown_pct",
            ]
            if col in view_df.columns
        ]
        rsi_col = next((col for col in view_df.columns if col.startswith("rsi_")), None)
        if rsi_col:
            display_cols.insert(4, rsi_col)
        table_df = view_df[display_cols].sort_values("date", ascending=False).copy()
        table_df["date"] = table_df["date"].dt.strftime("%Y-%m-%d")
        st.dataframe(table_df, use_container_width=True, hide_index=True)


def render_spread_detail(item: PositionItem) -> None:
    df = item.dataframe.copy()
    if df.empty:
        st.info(item.error or "当前没有可展示的价差数据。")
        return
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    spread_cols = [col for col in df.columns if col.startswith("spread_") and not col.endswith("_pct")]
    if not spread_cols:
        st.info("没有可绘制的价差列。")
        return
    spread_col = spread_cols[0]
    metric_row(
        [
            ("最新价差", item.metrics.get("最新价差"), "", 1),
            ("价差日变化", item.metrics.get("价差日变化"), "", 1),
            ("最新占比", item.metrics.get("最新占比(%)"), "%", 1),
            ("平均价差", item.metrics.get("平均价差"), "", 1),
        ]
    )
    range_label = st.segmented_control(
        "走势区间",
        options=["近一年", "今年来", "近3年", "近5年", "成立来"],
        default="近一年",
        key=f"position_range_{position_key(item)}",
    )
    view_df = filter_range(df, "date", range_label)
    if view_df.empty:
        view_df = df

    spread_tab, price_tab, summary_tab, table_tab = st.tabs(["价差", "合约价格", "摘要", "数据"])
    with spread_tab:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=view_df["date"], y=view_df[spread_col], mode="lines", name=item.code, line={"width": 2}))
        apply_plotly_layout(fig, height=DEFAULT_CHART_HEIGHT)
        fig.update_layout(yaxis_title="价差")
        st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})

    with price_tab:
        close_cols = [col for col in view_df.columns if col.endswith("_close")]
        fig = go.Figure()
        for col in close_cols:
            fig.add_trace(go.Scatter(x=view_df["date"], y=view_df[col], mode="lines", name=col.replace("_close", ""), line={"width": 1.7}))
        apply_plotly_layout(fig, height=DEFAULT_CHART_HEIGHT)
        fig.update_layout(yaxis_title="收盘价")
        st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})

    with summary_tab:
        summary_rows = [
            ("类别", item.category),
            ("价差对", item.code),
            ("名称", item.name),
            ("最新日期", item.latest_date),
            ("数据来源", item.source),
            ("缓存时间", item.cache_time or "-"),
            ("最新价差", format_metric_for_item(item, "最新价差")),
            ("价差日变化", format_metric_for_item(item, "价差日变化")),
            ("最新占比(%)", format_metric_for_item(item, "最新占比(%)")),
            ("平均价差", format_metric_for_item(item, "平均价差")),
            ("最大价差", format_metric_for_item(item, "最大价差")),
            ("最小价差", format_metric_for_item(item, "最小价差")),
        ]
        render_summary_table(summary_rows)

    with table_tab:
        table_df = view_df.drop(columns=[col for col in view_df.columns if col.startswith("_")], errors="ignore").sort_values("date", ascending=False)
        table_df = round_numeric_columns(table_df, digits=1)
        table_df["date"] = table_df["date"].dt.strftime("%Y-%m-%d")
        st.dataframe(table_df, use_container_width=True, hide_index=True)


def render_option_detail(item: PositionItem) -> None:
    df = item.dataframe.copy()
    if df.empty:
        st.info(item.error or "当前没有可展示的期权数据。")
        return
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date")
    metric_row(
        [
            ("最新收盘", item.metrics.get("最新收盘"), "", 1),
            ("日涨跌", item.metrics.get("日涨跌(%)"), "%", 1),
            ("20日涨跌", item.metrics.get("20日涨跌(%)"), "%", 1),
            ("20日波动", item.metrics.get("20日波动(%)"), "%", 1),
            ("成交量", item.metrics.get("最新成交量"), "", 0),
            ("持仓量", item.metrics.get("最新持仓量"), "", 0),
        ]
    )
    range_label = st.segmented_control(
        "走势区间",
        options=["近一年", "今年来", "近3年", "近5年", "成立来"],
        default="近一年",
        key=f"position_range_{position_key(item)}",
    )
    view_df = filter_range(df, "date", range_label)
    if view_df.empty:
        view_df = df

    trend_tab, activity_tab, summary_tab, table_tab = st.tabs(["走势", "成交持仓", "摘要", "数据"])
    with trend_tab:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=view_df["date"], y=view_df["close"], mode="lines", name="收盘价", line={"width": 2}))
        ma_colors = {5: "#d97706", 20: "#2563eb", 60: "#dc2626", 120: "#059669"}
        for period, color in ma_colors.items():
            ma_col = f"ma_{period}"
            if ma_col in view_df.columns:
                fig.add_trace(go.Scatter(x=view_df["date"], y=view_df[ma_col], mode="lines", name=f"MA{period}", line={"width": 1.25, "color": color}))
        apply_plotly_layout(fig, height=DEFAULT_CHART_HEIGHT)
        fig.update_layout(yaxis_title="期权价格")
        st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})

    with activity_tab:
        fig = go.Figure()
        if "volume" in view_df.columns:
            fig.add_trace(go.Bar(x=view_df["date"], y=view_df["volume"], name="成交量", marker_color="#94a3b8"))
        if "open_interest" in view_df.columns:
            fig.add_trace(go.Scatter(x=view_df["date"], y=view_df["open_interest"], mode="lines", name="持仓量", line={"width": 2, "color": "#2563eb"}, yaxis="y2"))
            fig.update_layout(yaxis2={"overlaying": "y", "side": "right", "title": "持仓量"})
        apply_plotly_layout(fig, height=420)
        fig.update_layout(yaxis_title="成交量")
        st.plotly_chart(fig, use_container_width=True)

    with summary_tab:
        summary_rows = [
            ("类别", item.category),
            ("代码", item.code),
            ("名称", item.name),
            ("最新日期", item.latest_date),
            ("数据来源", item.source),
            ("缓存时间", item.cache_time or "-"),
            ("最新收盘", format_metric_for_item(item, "最新收盘")),
            ("日涨跌(%)", format_metric_for_item(item, "日涨跌(%)")),
            ("20日波动(%)", format_metric_for_item(item, "20日波动(%)")),
            ("价格百分位", format_metric_for_item(item, "价格百分位")),
            ("最新成交量", format_metric_for_item(item, "最新成交量")),
            ("最新持仓量", format_metric_for_item(item, "最新持仓量")),
        ]
        render_summary_table(summary_rows)

    with table_tab:
        table_df = view_df.drop(columns=[col for col in view_df.columns if col.startswith("_")], errors="ignore").sort_values("date", ascending=False)
        table_df = round_numeric_columns(table_df, digits=1, integer_columns=("volume", "open_interest"))
        table_df["date"] = table_df["date"].dt.strftime("%Y-%m-%d")
        st.dataframe(table_df, use_container_width=True, hide_index=True)


def render_position_detail(item: PositionItem) -> None:
    st.divider()
    title_col, action_col = st.columns([5, 1])
    title_col.markdown(f"### {item.name}")
    action_col.button("返回全部", key="clear_position_detail", on_click=clear_position_detail)
    st.caption(
        f"{item.category} · {display_position_code(item)} · "
        f"最新日期：{item.latest_date or '-'} · 来源：{item.source or '-'}"
    )
    if item.error and item.status != "无缓存":
        st.warning(item.error)

    if item.category == "ETF":
        render_etf_detail(item)
    elif item.category == "期货价差":
        render_spread_detail(item)
    else:
        render_option_detail(item)


with st.sidebar:
    st.subheader("更新设置")
    api_key = st.text_input(
        "TickFlow API Key",
        value=os.getenv("TICKFLOW_API_KEY", ""),
        type="password",
        placeholder="可选；留空优先使用免费或缓存数据",
    )
    update_clicked = st.button("加载持仓信息", type="primary", use_container_width=True)
    force_refresh = st.checkbox(
        "联网检查并更新已有缓存",
        value=True,
        help=(
            "加载时联网检查近期数据。复权历史未变化时只追加新日期；"
            "发现分红等因素导致历史价格回溯时，仅在完整校验通过后重建对应标的新版缓存。"
        ),
    )
    save_to_cache = st.checkbox("更新后保存到本地缓存", value=True)

    with st.expander("持仓清单", expanded=False):
        etf_text = st.text_area("ETF持仓", value="\n".join(DEFAULT_ETF_CODES), height=150)
        spread_text = st.text_area(
            "期货价差",
            value="\n".join(" ".join(group) for group in DEFAULT_SPREAD_GROUPS),
            height=90,
            help="每行一组价差，第一个合约作为基准合约。",
        )
        option_text = st.text_area("期权持仓", value="\n".join(DEFAULT_OPTION_CODES), height=110)

    with st.expander("高级参数", expanded=False):
        adjust_option = st.selectbox(
            "ETF复权",
            options=list(FUND_ADJUSTMENT_OPTIONS),
            index=0,
            help=(
                "普通前复权使用差值口径；比例口径仅用于复现旧结果。"
                "不复权缓存只追加新日期，复权因分红发生历史回溯时仅重建对应标的。"
            ),
        )
        etf_count = st.number_input("ETF日线条数", min_value=300, max_value=10000, value=5000, step=100)
        market_count = st.number_input("期货/期权日线条数", min_value=20, max_value=5000, value=500, step=100)
        max_workers = st.slider("期货并发请求数", min_value=1, max_value=4, value=2)

adjust_map = FUND_ADJUSTMENT_OPTIONS
etf_codes = parse_position_codes(etf_text)
spread_groups = parse_spread_groups(spread_text)
option_codes = parse_position_codes(option_text)
allow_fetch = bool(update_clicked)
if "position_updates_enabled" not in st.session_state:
    st.session_state.position_updates_enabled = False
if update_clicked:
    st.session_state.position_updates_enabled = True
updates_enabled = bool(st.session_state.position_updates_enabled)
refresh_existing = bool(update_clicked and force_refresh)
market_now = datetime.now(ZoneInfo("Asia/Shanghai"))
intraday_market_active = etf_intraday_quote_ready(market_now)
intraday_quote_mode = bool(update_clicked and intraday_market_active)

items: list[PositionItem] = []
progress_total = len(etf_codes) + len(option_codes) + len(spread_groups)
progress_done = 0
progress_bar = st.progress(0) if update_clicked and progress_total else None
progress_status = st.empty() if update_clicked and progress_total else None


def update_position_progress(label: str) -> None:
    global progress_done
    if progress_bar is None or progress_status is None or progress_total <= 0:
        return
    progress_done += 1
    progress_bar.progress(progress_done / progress_total)
    progress_status.info(f"{label} 处理完成，进度 {progress_done}/{progress_total}")


intraday_quotes: dict[str, dict[str, object]] = {}
intraday_quote_error = ""
if intraday_quote_mode:
    try:
        intraday_quotes = fetch_tickflow_etf_quotes(
            etf_codes,
            api_key=api_key,
            market_now=market_now,
        )
        missing_quote_codes = [
            code for code in etf_codes
            if normalize_etf_base_code(code) not in intraday_quotes
        ]
        if missing_quote_codes:
            intraday_quote_error = f"部分ETF未返回当天实时行情：{', '.join(missing_quote_codes)}"
        stored_intraday_quotes = dict(st.session_state.get("position_etf_realtime_quotes", {}))
        stored_intraday_quotes.update(intraday_quotes)
        st.session_state.position_etf_realtime_quotes = stored_intraday_quotes
        remember_runtime_etf_quotes(intraday_quotes)
    except Exception as exc:
        intraday_quote_error = str(exc)

stored_intraday_quotes = load_runtime_etf_quotes()
stored_intraday_quotes.update(st.session_state.get("position_etf_realtime_quotes", {}))
active_intraday_quotes = filter_current_etf_realtime_quotes(
    stored_intraday_quotes,
    market_now=market_now,
)
if not active_intraday_quotes and "position_etf_realtime_quotes" in st.session_state:
    del st.session_state.position_etf_realtime_quotes


with st.spinner("正在整理持仓数据..."):
    for code in etf_codes:
        if intraday_quote_mode:
            card_item = load_or_fetch_etf(
                code,
                api_key=api_key,
                count=int(etf_count),
                adjust=adjust_map[adjust_option],
                allow_fetch=True,
                force_refresh=refresh_existing,
                save_to_cache=save_to_cache,
                market_now=market_now,
            )
        else:
            card_item = load_or_fetch_etf(
                code,
                api_key=api_key,
                count=int(etf_count),
                adjust=adjust_map[adjust_option],
                allow_fetch=allow_fetch,
                force_refresh=refresh_existing,
                save_to_cache=save_to_cache,
                market_now=market_now,
            )
        quote_data = active_intraday_quotes.get(normalize_etf_base_code(code))
        if quote_data is not None:
            card_item = apply_etf_realtime_quote(card_item, quote_data)
        items.append(card_item)
        update_position_progress(f"ETF {code}")

    for spread_contracts in spread_groups:
        items.append(
            load_or_fetch_spread(
                spread_contracts,
                base_contract=spread_contracts[0],
                api_key=api_key,
                max_workers=int(max_workers),
                allow_fetch=allow_fetch,
                force_refresh=refresh_existing,
                save_to_cache=save_to_cache,
                market_now=market_now,
            )
        )
        update_position_progress(f"期货价差 {' - '.join(spread_contracts)}")

    for code in option_codes:
        items.append(
            load_or_fetch_option(
                code,
                count=int(market_count),
                allow_fetch=allow_fetch,
                force_refresh=refresh_existing,
                save_to_cache=save_to_cache,
                market_now=market_now,
            )
        )
        update_position_progress(f"期权 {code}")

if progress_bar is not None and progress_status is not None:
    progress_bar.progress(1.0)
    progress_status.success(f"持仓数据整理完成，共 {progress_total} 个标的。")
if intraday_quote_error:
    st.warning(f"ETF盘中实时行情获取失败，卡片继续显示正式日线缓存：{intraday_quote_error}")

overview_df = build_overview_table(items)
selected_key = get_query_position_detail(items)
selected_item = next((item for item in items if position_key(item) == selected_key), None)
if selected_item is not None:
    render_position_detail(selected_item)

available_count = sum(1 for item in items if item.status not in {"失败", "无缓存"} and not item.dataframe.empty)
missing_count = sum(1 for item in items if item.status == "无缓存")
failed_count = sum(1 for item in items if item.status == "失败")
latest_dates = pd.to_datetime(overview_df["最新日期"].replace("-", pd.NA), errors="coerce").dropna() if not overview_df.empty else pd.Series(dtype="datetime64[ns]")
latest_date_text = latest_dates.max().strftime("%Y-%m-%d") if not latest_dates.empty else "-"

st.subheader(f"持仓摘要 · {latest_date_text}")
status_cols = st.columns(4)
status_cols[0].metric("持仓标的", len(items))
status_cols[1].metric("可用数据", available_count)
status_cols[2].metric("缺失缓存", missing_count)
status_cols[3].metric("获取失败", failed_count)

render_etf_timing_section(
    etf_codes,
    position_items=items,
    show_cache_caption=not update_clicked,
    api_key=api_key,
    count=int(etf_count),
    market_count=int(market_count),
    max_workers=int(max_workers),
    adjust=adjust_map[adjust_option],
    updates_enabled=updates_enabled,
    save_to_cache=save_to_cache,
)
