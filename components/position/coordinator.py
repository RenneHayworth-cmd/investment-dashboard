"""持仓页面控件、缓存优先加载和渲染顺序协调。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import os
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from components.position.details import render_position_detail
from components.position.formatting import (
    build_overview_table,
    get_query_position_detail,
    position_key,
)
from services import fund_analysis as fund
from services import position_analysis as position


TimingRenderer = Callable[..., None]


def render_position_page(timing_renderer: TimingRenderer) -> None:
    with st.sidebar:
        st.subheader("更新设置")
        api_key = st.text_input(
            "TickFlow API Key",
            value=os.getenv("TICKFLOW_API_KEY", ""),
            type="password",
            placeholder="可选；留空优先使用免费或缓存数据",
        )
        update_clicked = st.button(
            "加载持仓信息",
            type="primary",
            use_container_width=True,
        )
        force_refresh = st.checkbox(
            "强制重新检查已是最新的ETF缓存",
            value=False,
            help=(
                "默认关闭时只联网补齐缺失交易日或修复无效缓存。开启后会重新检查每只ETF；"
                "复权历史未变化时只追加新日期，发现分红等因素导致历史价格回溯时，"
                "仅在完整校验通过后重建对应标的新版缓存。"
            ),
        )
        save_to_cache = st.checkbox("更新后保存到本地缓存", value=True)

        with st.expander("持仓清单", expanded=False):
            etf_text = st.text_area(
                "ETF持仓",
                value="\n".join(position.DEFAULT_ETF_CODES),
                height=150,
            )
            futures_text = st.text_area(
                "期货持仓",
                value="\n".join(position.DEFAULT_FUTURES_CONTRACTS),
                height=70,
            )
            spread_text = st.text_area(
                "期货价差",
                value="\n".join(
                    " ".join(group) for group in position.DEFAULT_SPREAD_GROUPS
                ),
                height=90,
                help="每行一组价差，第一个合约作为基准合约。",
            )

        with st.expander("高级参数", expanded=False):
            adjust_option = st.selectbox(
                "ETF复权",
                options=list(fund.FUND_ADJUSTMENT_OPTIONS),
                index=0,
                help=(
                    "普通前复权使用差值口径；比例口径仅用于复现旧结果。"
                    "不复权缓存只追加新日期，复权因分红发生历史回溯时仅重建对应标的。"
                ),
            )
            etf_count = st.number_input(
                "ETF日线条数",
                min_value=300,
                max_value=10000,
                value=5000,
                step=100,
            )
            market_count = st.number_input(
                "期货日线条数",
                min_value=20,
                max_value=5000,
                value=500,
                step=100,
            )
            max_workers = st.slider(
                "期货并发请求数",
                min_value=1,
                max_value=4,
                value=2,
            )

    adjust_map = fund.FUND_ADJUSTMENT_OPTIONS
    etf_codes = position.parse_position_codes(etf_text)
    quote_codes = sorted(set(etf_codes))
    futures_codes = position.parse_position_codes(futures_text)
    spread_groups = position.parse_spread_groups(spread_text)
    allow_fetch = bool(update_clicked)
    if "position_updates_enabled" not in st.session_state:
        st.session_state.position_updates_enabled = False
    if update_clicked:
        st.session_state.position_updates_enabled = True
    updates_enabled = bool(st.session_state.position_updates_enabled)
    refresh_existing = bool(update_clicked and force_refresh)
    market_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    intraday_market_active = position.etf_intraday_quote_ready(market_now)
    intraday_quote_mode = bool(update_clicked and intraday_market_active)

    items: list[position.PositionItem] = []
    progress_total = len(etf_codes) + len(futures_codes) + len(spread_groups)
    progress_done = 0
    progress_bar = st.progress(0) if update_clicked and progress_total else None
    progress_status = st.empty() if update_clicked and progress_total else None

    def update_position_progress(label: str) -> None:
        nonlocal progress_done
        if progress_bar is None or progress_status is None or progress_total <= 0:
            return
        progress_done += 1
        progress_bar.progress(progress_done / progress_total)
        progress_status.info(
            f"{label} 处理完成，进度 {progress_done}/{progress_total}"
        )

    intraday_quotes: dict[str, dict[str, object]] = {}
    intraday_quote_error = ""
    if intraday_quote_mode:
        try:
            intraday_quotes = position.refresh_runtime_etf_quotes(
                quote_codes,
                api_key=api_key,
                market_now=market_now,
            )
            missing_quote_codes = [
                code
                for code in quote_codes
                if position.normalize_etf_base_code(code) not in intraday_quotes
            ]
            if missing_quote_codes:
                intraday_quote_error = (
                    "部分ETF未返回当天实时行情：" + ", ".join(missing_quote_codes)
                )
            stored_intraday_quotes = dict(
                st.session_state.get("position_etf_realtime_quotes", {})
            )
            stored_intraday_quotes.update(intraday_quotes)
            st.session_state.position_etf_realtime_quotes = stored_intraday_quotes
            position.remember_runtime_etf_quotes(intraday_quotes)
        except Exception as exc:
            intraday_quote_error = str(exc)

    stored_intraday_quotes = position.load_runtime_etf_quotes()
    stored_intraday_quotes.update(
        st.session_state.get("position_etf_realtime_quotes", {})
    )
    active_intraday_quotes = position.filter_current_etf_realtime_quotes(
        stored_intraday_quotes,
        market_now=market_now,
    )
    if (
        not active_intraday_quotes
        and "position_etf_realtime_quotes" in st.session_state
    ):
        del st.session_state.position_etf_realtime_quotes

    with st.spinner("正在整理持仓数据..."):
        for code in etf_codes:
            if intraday_quote_mode:
                card_item = position.load_or_fetch_etf(
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
                card_item = position.load_or_fetch_etf(
                    code,
                    api_key=api_key,
                    count=int(etf_count),
                    adjust=adjust_map[adjust_option],
                    allow_fetch=allow_fetch,
                    force_refresh=refresh_existing,
                    save_to_cache=save_to_cache,
                    market_now=market_now,
                )
            quote_data = active_intraday_quotes.get(
                position.normalize_etf_base_code(code)
            )
            if quote_data is not None:
                card_item = position.apply_etf_realtime_quote(card_item, quote_data)
            items.append(card_item)
            update_position_progress(f"ETF {code}")

        for code in futures_codes:
            items.append(
                position.load_or_fetch_futures_contract(
                    code,
                    api_key=api_key,
                    count=int(market_count),
                    allow_fetch=allow_fetch,
                    force_refresh=refresh_existing,
                    save_to_cache=save_to_cache,
                    market_now=market_now,
                )
            )
            update_position_progress(f"期货 {code}")

        for spread_contracts in spread_groups:
            items.append(
                position.load_or_fetch_spread(
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

    if progress_bar is not None and progress_status is not None:
        progress_bar.progress(1.0)
        progress_status.success(f"持仓数据整理完成，共 {progress_total} 个标的。")
    if intraday_quote_error:
        st.warning(
            "ETF盘中实时行情获取失败，卡片继续显示正式日线缓存："
            f"{intraday_quote_error}"
        )

    overview_df = build_overview_table(items)
    available_count = sum(
        1
        for item in items
        if item.status not in {"失败", "无缓存"} and not item.dataframe.empty
    )
    missing_count = sum(1 for item in items if item.status == "无缓存")
    failed_count = sum(1 for item in items if item.status == "失败")
    latest_dates = (
        pd.to_datetime(
            overview_df["最新日期"].replace("-", pd.NA), errors="coerce"
        ).dropna()
        if not overview_df.empty
        else pd.Series(dtype="datetime64[ns]")
    )
    latest_date_text = (
        latest_dates.max().strftime("%Y-%m-%d") if not latest_dates.empty else "-"
    )

    st.subheader(f"分析数据状态 · {latest_date_text}")
    status_cols = st.columns(4)
    status_cols[0].metric("分析标的", len(items))
    status_cols[1].metric("可用数据", available_count)
    status_cols[2].metric("缺失缓存", missing_count)
    status_cols[3].metric("获取失败", failed_count)

    selected_key = get_query_position_detail(items)
    selected_item = next(
        (item for item in items if position_key(item) == selected_key), None
    )
    if selected_item is not None:
        render_position_detail(selected_item)

    timing_renderer(
        etf_codes,
        quote_codes=quote_codes,
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
