"""持仓页面唯一实时 fragment 的实现。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from components.position.cards_tables import (
    render_etf_operation_guidance,
    render_etf_timing_table,
    render_position_cards,
)
from components.position.formatting import position_key
from components.position.performance import render_position_timing_performance
from services import position_analysis as position


def render_etf_timing_section_impl(
    etf_codes: list[str],
    *,
    quote_codes: list[str] | None = None,
    position_items: list[position.PositionItem],
    show_cache_caption: bool,
    api_key: str,
    count: int,
    market_count: int,
    max_workers: int,
    adjust: str | None,
    updates_enabled: bool,
    save_to_cache: bool,
    value_formatter: Callable[[str, object], str],
) -> None:
    market_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    quote_codes = sorted(set(quote_codes or etf_codes))
    formal_items = [
        position.load_or_fetch_etf(
            code,
            api_key=api_key,
            count=count,
            adjust=adjust,
            allow_fetch=False,
            market_now=market_now,
        )
        for code in etf_codes
    ]
    target_date = position.latest_final_etf_trade_date(market_now)
    stale_codes = []
    for code, item in zip(etf_codes, formal_items):
        item_date = pd.to_datetime(item.latest_date, errors="coerce")
        if pd.isna(item_date) or item_date.date() < target_date:
            stale_codes.append(code)

    attempt_key = "position_etf_auto_final_last_attempt"
    last_attempt = pd.to_datetime(st.session_state.get(attempt_key), errors="coerce")
    retry_ready = (
        pd.isna(last_attempt)
        or (market_now.replace(tzinfo=None) - last_attempt).total_seconds() >= 600
    )
    if (
        updates_enabled
        and position.etf_final_close_ready(market_now)
        and stale_codes
        and retry_ready
    ):
        with st.spinner(f"正在自动更新 {target_date:%Y-%m-%d} ETF收盘数据..."):
            refreshed_by_code = {
                code: position.load_or_fetch_etf(
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
    active_preview_quotes = position.filter_current_etf_realtime_quotes(
        position.load_runtime_etf_quotes(),
        market_now=market_now,
        retain_after_close=True,
    )
    if position.etf_final_close_ready(market_now) and not formal_close_missing:
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
    morning_quotes = (
        morning_preview_state.get("quotes", {}) if same_morning_date else {}
    )
    if updates_enabled and position.etf_morning_timing_fetch_ready(market_now):
        morning_refresh_band = (
            "early"
            if market_now.time() < datetime.strptime("10:00", "%H:%M").time()
            else "midmorning"
        )
        morning_refresh_seconds = (
            position.ETF_MORNING_TIMING_REFRESH_SECONDS
            if morning_refresh_band == "early"
            else position.ETF_MIDSESSION_TIMING_REFRESH_SECONDS
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
                    morning_quotes = position.refresh_runtime_etf_quotes(
                        quote_codes,
                        api_key=api_key,
                        market_now=market_now,
                    )
                    position.remember_runtime_etf_quotes(morning_quotes)
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

    if position.etf_morning_timing_preview_ready(market_now):
        realtime_timing_error = (
            realtime_timing_error or morning_preview_state.get("error", "")
        )
        if morning_quotes:
            active_preview_quotes = morning_quotes
            timing_items = [
                position.apply_etf_realtime_quote_to_timing(
                    item,
                    morning_quotes.get(position.normalize_etf_base_code(code), {}),
                    market_now=market_now,
                )
                for code, item in zip(etf_codes, formal_items)
            ]
            missing_realtime_codes = [
                position.normalize_etf_base_code(code)
                for code in etf_codes
                if position.normalize_etf_base_code(code) not in morning_quotes
            ]

    lunch_preview_state = st.session_state.get(lunch_preview_key, {})
    same_lunch_date = lunch_preview_state.get("trade_date") == preview_date
    lunch_quotes = lunch_preview_state.get("quotes", {}) if same_lunch_date else {}
    lunch_fetch_ready = position.etf_lunch_timing_fetch_ready(market_now)
    afternoon_fetch_ready = position.etf_afternoon_timing_fetch_ready(market_now)
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
            >= position.ETF_MIDSESSION_TIMING_REFRESH_SECONDS
        )
    if lunch_refresh_due:
        derivative_refresh_due = True
        if api_key:
            previous_quotes = lunch_quotes
            try:
                lunch_quotes = position.refresh_runtime_etf_quotes(
                    quote_codes,
                    api_key=api_key,
                    market_now=market_now,
                )
                position.remember_runtime_etf_quotes(lunch_quotes)
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

    if position.etf_lunch_timing_preview_ready(market_now):
        realtime_timing_error = (
            realtime_timing_error or lunch_preview_state.get("error", "")
        )

    if position.etf_lunch_timing_preview_ready(market_now) and lunch_quotes:
        active_preview_quotes = lunch_quotes
        timing_items = [
            position.apply_etf_realtime_quote_to_timing(
                item,
                lunch_quotes.get(position.normalize_etf_base_code(code), {}),
                market_now=market_now,
            )
            for code, item in zip(etf_codes, formal_items)
        ]
        missing_realtime_codes = [
            position.normalize_etf_base_code(code)
            for code in etf_codes
            if position.normalize_etf_base_code(code) not in lunch_quotes
        ]

    if updates_enabled and position.etf_realtime_timing_ready(market_now):
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
            >= position.ETF_REALTIME_TIMING_REFRESH_SECONDS
        )
        if preview_refresh_due:
            derivative_refresh_due = True
            if api_key:
                previous_quotes = (
                    preview_state.get("quotes", {}) if same_preview_date else {}
                )
                try:
                    realtime_quotes = position.refresh_runtime_etf_quotes(
                        quote_codes,
                        api_key=api_key,
                        market_now=market_now,
                    )
                    position.remember_runtime_etf_quotes(realtime_quotes)
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
            position.normalize_etf_base_code(code)
            for code in etf_codes
            if position.normalize_etf_base_code(code) not in realtime_quotes
        ]
        timing_items = [
            position.apply_etf_realtime_quote_to_timing(
                item,
                active_preview_quotes.get(position.normalize_etf_base_code(code), {}),
                market_now=market_now,
            )
            for code, item in zip(etf_codes, formal_items)
        ]

    timing_preview_window = bool(
        position.etf_intraday_quote_ready(market_now)
        or (
            position.etf_final_close_ready(market_now)
            and formal_close_missing
        )
    )
    if timing_preview_window and active_preview_quotes:
        timing_items = [
            position.apply_etf_realtime_quote_to_timing(
                item,
                active_preview_quotes.get(position.normalize_etf_base_code(code), {}),
                market_now=market_now,
                allow_close_retention=(
                    market_now.time() >= position.ETF_REALTIME_TIMING_END_TIME
                ),
            )
            for code, item in zip(etf_codes, formal_items)
        ]
        missing_realtime_codes = [
            position.normalize_etf_base_code(code)
            for code in etf_codes
            if position.normalize_etf_base_code(code) not in active_preview_quotes
        ]

    timing_preview_active = updates_enabled and timing_preview_window
    derivative_state_key = "position_derivative_realtime_preview"
    derivative_state = st.session_state.get(derivative_state_key, {})
    derivative_preview_version = 3
    if (
        timing_preview_active
        and derivative_state.get("version") != derivative_preview_version
    ):
        derivative_refresh_due = True
    same_derivative_date = derivative_state.get("trade_date") == preview_date
    derivative_items = (
        derivative_state.get("items", {}) if same_derivative_date else {}
    )
    derivative_realtime_error = ""
    if derivative_refresh_due:
        refreshed_derivatives, derivative_errors = (
            position.refresh_position_derivative_items(
                position_items,
                api_key=api_key,
                max_workers=max_workers,
                option_count=market_count,
                market_now=market_now,
            )
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
        position.normalize_etf_base_code(item.code): item
        for item in position_items
        if item.category == "ETF"
    }
    formal_etf_items = {
        position.normalize_etf_base_code(item.code): item for item in formal_items
    }
    base_card_items: list[position.PositionItem] = []
    for item in position_items:
        if item.category != "ETF":
            base_card_items.append(item)
            continue
        code = position.normalize_etf_base_code(item.code)
        outer_item = outer_etf_items.get(code, item)
        base_item = (
            formal_etf_items.get(code, outer_item)
            if position.etf_final_close_ready(market_now) and not formal_close_missing
            else outer_item
            if outer_item.status == "盘中"
            else formal_etf_items.get(code, outer_item)
        )
        base_card_items.append(base_item)

    card_items = position.apply_etf_realtime_quotes_to_items(
        base_card_items,
        active_preview_quotes,
    )
    card_items = [
        derivative_items.get(position_key(item), item)
        if item.category in {"期货", "期货价差", "期权"}
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
            "期货/价差实时更新部分失败，继续显示上次有效数据："
            f"{derivative_realtime_error}"
        )

    st.subheader("ETF择时状态")
    render_etf_timing_table(
        position.build_etf_timing_table(timing_items),
        value_formatter=value_formatter,
    )
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
        if position.etf_morning_timing_preview_ready(market_now):
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
        elif position.etf_realtime_timing_ready(market_now):
            st.caption(
                "14:50-15:00每2分钟更新一次实时行情并预判择时状态；"
                "实时价格不写入缓存，也不参与近一周操作指引。"
            )
        else:
            if position.etf_afternoon_timing_fetch_ready(market_now):
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
    guidance_df = position.build_recent_etf_operation_guidance(formal_items, days=7)
    if guidance_df.empty:
        st.info("最近7个自然日没有新的调仓指引，继续按上方当前仓位执行。")
    else:
        render_etf_operation_guidance(
            guidance_df,
            value_formatter=value_formatter,
        )
    st.caption("依据正式收盘日线计算；盘中实时报价不参与，展示窗口以最新正式交易日为截止日。")

    render_position_timing_performance(formal_items)
