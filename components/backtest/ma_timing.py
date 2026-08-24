from __future__ import annotations

import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.cache import save_dataset
from services.fund_analysis import (
    build_fund_cache_symbol,
    fetch_tickflow_fund_close,
    infer_tickflow_symbol,
)
from services.fund_rotation import (
    build_standard_backtest_periods,
    normalize_rotation_dataframe,
    run_ma20_timing_backtest,
)

from .common import (
    BACKTEST_ADJUSTMENT_OPTIONS,
    FULL_HISTORY_CACHE_PERIOD,
    FULL_HISTORY_COUNT,
    default_backtest_dates,
    format_cache_time,
    format_value,
    load_rotation_cache,
    render_drawdown_chart,
    to_csv_bytes,
)


def build_timing_period_table(
    fund,
    end_date,
    *,
    ma_period: int,
    threshold_pct: float,
    initial_capital: float,
    transaction_cost: float,
    lot_size: int,
) -> pd.DataFrame:
    rows = []
    for label, period_start in build_standard_backtest_periods(end_date):
        try:
            period_result = run_ma20_timing_backtest(
                fund=fund,
                ma_period=ma_period,
                threshold_pct=threshold_pct,
                initial_capital=initial_capital,
                transaction_cost=transaction_cost,
                lot_size=lot_size,
                start_date=period_start,
                end_date=end_date,
            )
            summary = period_result.summary
            rows.append(
                {
                    "区间": label,
                    "实际开始": summary.get("开始日期"),
                    "实际结束": summary.get("结束日期"),
                    "总收益率(%)": summary.get("总收益率(%)"),
                    "年化收益率(%)": summary.get("年化收益率(%)"),
                    "策略最大回撤(%)": summary.get("策略最大回撤(%)"),
                    "一直持有最大回撤(%)": summary.get("一直持有最大回撤(%)"),
                    "夏普比率": summary.get("夏普比率"),
                    "交易胜率(%)": summary.get("交易胜率(%)"),
                    "交易次数": summary.get("交易次数"),
                    "一直持有收益率(%)": summary.get("一直持有收益率(%)"),
                    "超额收益(%)": summary.get("超额收益(%)"),
                }
            )
        except Exception as exc:
            rows.append({"区间": label, "说明": str(exc)})
    return pd.DataFrame(rows)


def render_timing_nav_chart(result_df: pd.DataFrame) -> None:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=result_df["日期"],
            y=result_df["账户净值"],
            mode="lines",
            name="MA20择时",
            hovertemplate="%{x|%Y-%m-%d}<br>账户净值=%{y:.2f}<extra></extra>",
            line=dict(width=2.4, color="#d62728"),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=result_df["日期"],
            y=result_df["一直持有净值"],
            mode="lines",
            name="一直持有",
            hovertemplate="%{x|%Y-%m-%d}<br>账户净值=%{y:.2f}<extra></extra>",
            line=dict(width=1.8, color="#4b5563", dash="dot"),
        )
    )
    fig.update_layout(
        height=520,
        margin=dict(l=10, r=10, t=30, b=10),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        xaxis_title="日期",
        yaxis_title="账户净值",
    )
    st.plotly_chart(fig, use_container_width=True)


def render_timing_signal_chart(
    result_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    ma_period: int,
) -> None:
    ma_col = f"MA{ma_period}"
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=result_df["日期"],
            y=result_df["收盘价"],
            mode="lines",
            name="收盘价",
            line=dict(width=2, color="#1f77b4"),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=result_df["日期"],
            y=result_df[ma_col],
            mode="lines",
            name=ma_col,
            line=dict(width=1.8, color="#ff7f0e"),
        )
    )
    if {"买入线", "卖出线"}.issubset(result_df.columns):
        fig.add_trace(
            go.Scatter(
                x=result_df["日期"],
                y=result_df["买入线"],
                mode="lines",
                name="买入线",
                line=dict(width=1.2, color="#d62728", dash="dash"),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=result_df["日期"],
                y=result_df["卖出线"],
                mode="lines",
                name="卖出线",
                line=dict(width=1.2, color="#2ca02c", dash="dash"),
            )
        )
    if not trades_df.empty:
        buy_df = trades_df[trades_df["操作"] == "买入"]
        sell_df = trades_df[trades_df["操作"] == "卖出"]
        if not buy_df.empty:
            fig.add_trace(
                go.Scatter(
                    x=buy_df["日期"],
                    y=buy_df["成交价"],
                    mode="markers",
                    name="买入",
                    marker=dict(symbol="triangle-up", size=11, color="#d62728"),
                )
            )
        if not sell_df.empty:
            fig.add_trace(
                go.Scatter(
                    x=sell_df["日期"],
                    y=sell_df["成交价"],
                    mode="markers",
                    name="卖出",
                    marker=dict(symbol="triangle-down", size=11, color="#2ca02c"),
                )
            )
    fig.update_layout(
        height=520,
        margin=dict(l=10, r=10, t=30, b=10),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        xaxis_title="日期",
        yaxis_title="价格",
    )
    st.plotly_chart(fig, use_container_width=True)


def render_ma20_timing_mode() -> None:
    default_start_date, default_end_date = default_backtest_dates()
    with st.sidebar:
        st.subheader("MA20择时参数")
        ma_period = st.number_input("均线周期", min_value=2, max_value=250, value=20, step=1)
        timing_threshold_pct = st.number_input(
            "触发阈值(%)",
            min_value=0.0,
            max_value=20.0,
            value=1.0,
            step=0.1,
            help="高于均线上方该比例才买入，低于均线下方该比例才卖出。",
        )
        timing_initial_capital = st.number_input(
            "初始资金", min_value=1000.0, value=100000.0, step=10000.0
        )
        timing_transaction_cost_bp = st.number_input(
            "单边交易成本（万分之）",
            min_value=0.0,
            value=0.6,
            step=0.1,
            key="ma20_timing_transaction_cost_bp",
        )
        timing_lot_size = st.number_input(
            "交易单位", min_value=1, max_value=10000, value=100, step=100
        )
        st.subheader("回测区间")
        timing_start_date = st.date_input(
            "开始日期",
            value=default_start_date,
            key="ma20_timing_start_date",
        )
        timing_end_date = st.date_input(
            "结束日期",
            value=default_end_date,
            key="ma20_timing_end_date",
        )

    with st.form("ma20_timing_form"):
        code = st.text_input(
            "场内基金/ETF代码",
            value="512890",
            placeholder="例如 512890、159915 或 512890.SH",
        )
        adjust_option = st.selectbox(
            "复权",
            options=list(BACKTEST_ADJUSTMENT_OPTIONS),
            index=0,
        )
        api_key = st.text_input(
            "TickFlow API Key",
            value=os.getenv("TICKFLOW_API_KEY", ""),
            type="password",
        )
        run_clicked = st.form_submit_button("运行MA20择时回测", type="primary")

    if not run_clicked:
        st.info("默认用 512890 跑 MA20 择时；信号按当日收盘价和 MA20 缓冲带比较，成交价也使用当日收盘价。")
        return
    if pd.Timestamp(timing_start_date) > pd.Timestamp(timing_end_date):
        st.error("开始日期不能晚于结束日期。")
        return

    try:
        symbol = infer_tickflow_symbol(code)
        adjust_value = BACKTEST_ADJUSTMENT_OPTIONS[adjust_option]
        cache_symbol = build_fund_cache_symbol("fund_rotation", symbol, adjust_value)
        cached_df, cache_meta, _cache_period = load_rotation_cache(cache_symbol)
        try:
            with st.spinner(f"正在通过 TickFlow 拉取 {symbol} 的{adjust_option}日线..."):
                raw_df = fetch_tickflow_fund_close(
                    symbol=symbol,
                    api_key=api_key,
                    count=FULL_HISTORY_COUNT,
                    adjust=adjust_value,
                )
            save_dataset(
                cache_symbol,
                f"{symbol} {adjust_option}",
                "tickflow_fund_rotation",
                "fund_rotation_raw",
                raw_df,
                period=FULL_HISTORY_CACHE_PERIOD,
            )
            st.success(f"{symbol} 已更新并保存到本地缓存。")
        except Exception as fetch_exc:
            if cached_df is None:
                raise
            raw_df = cached_df
            st.warning(
                f"{symbol} 联网更新失败，已改用本地缓存（缓存时间："
                f"{format_cache_time(cache_meta.get('last_update_time') if cache_meta else None)}）。"
                f"原因：{fetch_exc}"
            )

        fund = normalize_rotation_dataframe(raw_df, fallback_name=f"{symbol} {adjust_option}")
        result = run_ma20_timing_backtest(
            fund=fund,
            ma_period=int(ma_period),
            threshold_pct=float(timing_threshold_pct),
            initial_capital=float(timing_initial_capital),
            transaction_cost=float(timing_transaction_cost_bp) / 10000,
            lot_size=int(timing_lot_size),
            start_date=timing_start_date,
            end_date=timing_end_date,
        )
    except Exception as exc:
        st.error(f"MA20择时回测出错：{exc}")
        return

    summary = result.summary
    metric_cols = st.columns(6)
    metric_cols[0].metric("总收益率", format_value(summary.get("总收益率(%)"), "%"))
    metric_cols[1].metric("一直持有收益", format_value(summary.get("一直持有收益率(%)"), "%"))
    metric_cols[2].metric("超额收益", format_value(summary.get("超额收益(%)"), "%"))
    metric_cols[3].metric("策略最大回撤", format_value(summary.get("策略最大回撤(%)"), "%"))
    metric_cols[4].metric(
        "一直持有最大回撤", format_value(summary.get("一直持有最大回撤(%)"), "%")
    )
    metric_cols[5].metric("最新信号", str(summary.get("最新信号", "-")))

    detail_cols = st.columns(5)
    detail_cols[0].metric("年化收益率", format_value(summary.get("年化收益率(%)"), "%"))
    detail_cols[1].metric("夏普比率", format_value(summary.get("夏普比率")))
    detail_cols[2].metric("交易胜率", format_value(summary.get("交易胜率(%)"), "%"))
    detail_cols[3].metric("交易次数", format_value(summary.get("交易次数")))
    detail_cols[4].metric("回测区间", f"{summary.get('开始日期')} → {summary.get('结束日期')}")

    period_df = build_timing_period_table(
        fund,
        result.end_date,
        ma_period=int(ma_period),
        threshold_pct=float(timing_threshold_pct),
        initial_capital=float(timing_initial_capital),
        transaction_cost=float(timing_transaction_cost_bp) / 10000,
        lot_size=int(timing_lot_size),
    )
    st.subheader("分期回测结果")
    st.dataframe(period_df, use_container_width=True, hide_index=True)
    st.download_button(
        "下载分期回测结果 CSV",
        data=to_csv_bytes(period_df),
        file_name="ma20_timing_period_results.csv",
        mime="text/csv",
    )

    tab_nav, tab_signal, tab_drawdown, tab_trades, tab_daily, tab_summary = st.tabs(
        ["净值走势", "标的与信号", "回撤分析", "交易明细", "每日数据", "摘要"]
    )
    with tab_nav:
        render_timing_nav_chart(result.data)
        st.download_button(
            "下载择时净值 CSV",
            data=to_csv_bytes(result.data),
            file_name="ma20_timing_nav_data.csv",
            mime="text/csv",
        )
    with tab_signal:
        render_timing_signal_chart(result.data, result.trades, int(ma_period))
    with tab_drawdown:
        render_drawdown_chart(result.drawdown)
        if not result.yearly_stats.empty:
            st.subheader("年度收益与回撤")
            st.dataframe(result.yearly_stats, use_container_width=True, hide_index=True)
    with tab_trades:
        if result.trades.empty:
            st.info("回测区间内没有触发交易。")
        else:
            st.dataframe(result.trades, use_container_width=True, hide_index=True)
            st.download_button(
                "下载交易明细 CSV",
                data=to_csv_bytes(result.trades),
                file_name="ma20_timing_trades.csv",
                mime="text/csv",
            )
    with tab_daily:
        st.dataframe(result.data, use_container_width=True, hide_index=True)
    with tab_summary:
        summary_df = pd.DataFrame(
            [{"指标": key, "数值": str(value)} for key, value in summary.items()]
        )
        st.dataframe(summary_df, use_container_width=True, hide_index=True)


__all__ = [
    "build_timing_period_table",
    "render_ma20_timing_mode",
    "render_timing_nav_chart",
    "render_timing_signal_chart",
]
