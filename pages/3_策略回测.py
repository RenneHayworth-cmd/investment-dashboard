import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.cache import load_dataset, save_dataset
from core.db import init_db
from services.fund_analysis import (
    FUND_ADJUSTMENT_OPTIONS,
    FUND_ADJUST_NONE,
    build_fund_cache_symbol,
    fetch_eastmoney_fund_nav,
    fetch_tickflow_fund_close,
    infer_tickflow_symbol,
    read_uploaded_table,
)
from services.fund_rotation import (
    EXECUTION_AFTER_CLOSE,
    EXECUTION_NEXT_OPEN,
    PORTFOLIO_STRATEGY_CASH,
    PORTFOLIO_STRATEGY_HALF_TIMING,
    PORTFOLIO_STRATEGY_HOLD,
    PORTFOLIO_STRATEGY_TIMING,
    PortfolioTimingAllocation,
    build_standard_backtest_periods,
    normalize_rotation_dataframe,
    run_fund_rotation_backtest,
    run_ma20_timing_backtest,
    run_portfolio_timing_backtest,
)


FULL_HISTORY_COUNT = 10000
FULL_HISTORY_CACHE_PERIOD = "full_1d"
LEGACY_CACHE_PERIODS = ("10000_1d", "5000_1d")
BACKTEST_ADJUSTMENT_OPTIONS = {
    label: mode
    for label, mode in FUND_ADJUSTMENT_OPTIONS.items()
    if mode != FUND_ADJUST_NONE
}
PORTFOLIO_STRATEGY_LABELS = {
    "一直持有": PORTFOLIO_STRATEGY_HOLD,
    "纯择时": PORTFOLIO_STRATEGY_TIMING,
    "半仓持有、半仓择时": PORTFOLIO_STRATEGY_HALF_TIMING,
    "现金": PORTFOLIO_STRATEGY_CASH,
}
PORTFOLIO_STRATEGY_DISPLAY = {value: key for key, value in PORTFOLIO_STRATEGY_LABELS.items()}
DEFAULT_PORTFOLIO_CONFIG = pd.DataFrame(
    [
        {"ETF代码": "513260", "标的名称": "恒生科技", "配置比例(%)": 10.0, "策略类型": "纯择时", "均线周期": 20, "触发阈值(%)": 1.0},
        {"ETF代码": "510500", "标的名称": "中证500", "配置比例(%)": 10.0, "策略类型": "纯择时", "均线周期": 15, "触发阈值(%)": 1.0},
        {"ETF代码": "159967", "标的名称": "创业板成长", "配置比例(%)": 10.0, "策略类型": "纯择时", "均线周期": 25, "触发阈值(%)": 2.0},
        {"ETF代码": "159545", "标的名称": "恒生红利低波", "配置比例(%)": 10.0, "策略类型": "纯择时", "均线周期": 10, "触发阈值(%)": 1.0},
        {"ETF代码": "159501", "标的名称": "纳指", "配置比例(%)": 10.0, "策略类型": "半仓持有、半仓择时", "均线周期": 25, "触发阈值(%)": 2.0},
        {"ETF代码": "159655", "标的名称": "标普500", "配置比例(%)": 10.0, "策略类型": "半仓持有、半仓择时", "均线周期": 25, "触发阈值(%)": 2.0},
        {"ETF代码": "518850", "标的名称": "黄金", "配置比例(%)": 10.0, "策略类型": "纯择时", "均线周期": 30, "触发阈值(%)": 1.5},
        {"ETF代码": "512890", "标的名称": "红利低波", "配置比例(%)": 30.0, "策略类型": "一直持有", "均线周期": 20, "触发阈值(%)": 1.0},
    ]
)


def format_value(value, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")


def format_cache_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value).replace("T", " ")


def default_backtest_dates() -> tuple[object, object]:
    end_date = pd.Timestamp.today().normalize()
    start_date = end_date - pd.DateOffset(years=5)
    return start_date.date(), end_date.date()


def load_rotation_cache(cache_symbol: str):
    for period in (FULL_HISTORY_CACHE_PERIOD, *LEGACY_CACHE_PERIODS):
        cached_df, cache_meta = load_dataset(
            cache_symbol,
            "tickflow_fund_rotation",
            "fund_rotation_raw",
            period=period,
        )
        if cached_df is not None:
            return cached_df, cache_meta, period
    return None, None, FULL_HISTORY_CACHE_PERIOD


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


def build_rotation_period_table(
    funds,
    end_date,
    *,
    frequency: str,
    lookback_period: int,
    num_positions: int,
    initial_capital: float,
    transaction_cost: float,
    execution_mode: str,
) -> pd.DataFrame:
    rows = []
    for label, period_start in build_standard_backtest_periods(end_date):
        try:
            period_result = run_fund_rotation_backtest(
                funds=funds,
                frequency=frequency,
                lookback_period=lookback_period,
                num_positions=num_positions,
                initial_capital=initial_capital,
                transaction_cost=transaction_cost,
                start_date=period_start,
                end_date=end_date,
                execution_mode=execution_mode,
            )
            summary = period_result.summary
            rows.append(
                {
                    "区间": label,
                    "实际开始": summary.get("开始日期"),
                    "实际结束": summary.get("结束日期"),
                    "成交方式": summary.get("成交方式"),
                    "总收益率(%)": summary.get("总收益率(%)"),
                    "年化收益率(%)": summary.get("年化收益率(%)"),
                    "策略最大回撤(%)": summary.get("策略最大回撤(%)"),
                    "夏普比率": summary.get("夏普比率"),
                    "交易胜率(%)": summary.get("交易胜率(%)"),
                    "调仓次数": summary.get("调仓次数"),
                    "期末资金": summary.get("期末资金"),
                }
            )
        except Exception as exc:
            rows.append({"区间": label, "说明": str(exc)})
    return pd.DataFrame(rows)


def render_nav_chart(nav_df: pd.DataFrame, individual_df: pd.DataFrame | None = None) -> None:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=nav_df["日期"],
            y=nav_df["账户净值"],
            mode="lines",
            name="轮动策略",
            hovertemplate="%{x|%Y-%m-%d}<br>账户净值=%{y:.2f}<extra></extra>",
            line=dict(width=2.4, color="#d62728"),
        )
    )
    if individual_df is not None and not individual_df.empty:
        for _, group in individual_df.groupby("标的", sort=False):
            label = str(group["标的"].iloc[0])
            fig.add_trace(
                go.Scatter(
                    x=group["日期"],
                    y=group["一直持有净值"],
                    mode="lines",
                    name=f"一直持有：{label}",
                    hovertemplate="%{x|%Y-%m-%d}<br>净值=%{y:.2f}<extra></extra>",
                    line=dict(width=1.6, dash="dot"),
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


def render_drawdown_chart(drawdown_df: pd.DataFrame) -> None:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=drawdown_df["日期"],
            y=drawdown_df["回撤(%)"],
            mode="lines",
            name="回撤",
            fill="tozeroy",
            hovertemplate="%{x|%Y-%m-%d}<br>回撤=%{y:.2f}%<extra></extra>",
            line=dict(width=1.8, color="#2ca02c"),
        )
    )
    fig.update_layout(
        height=360,
        margin=dict(l=10, r=10, t=30, b=10),
        hovermode="x unified",
        xaxis_title="日期",
        yaxis_title="回撤(%)",
    )
    st.plotly_chart(fig, use_container_width=True)


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


def render_timing_signal_chart(result_df: pd.DataFrame, trades_df: pd.DataFrame, ma_period: int) -> None:
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
        timing_initial_capital = st.number_input("初始资金", min_value=1000.0, value=100000.0, step=10000.0)
        timing_transaction_cost_bp = st.number_input(
            "单边交易成本（万分之）",
            min_value=0.0,
            value=0.6,
            step=0.1,
            key="ma20_timing_transaction_cost_bp",
        )
        timing_lot_size = st.number_input("交易单位", min_value=1, max_value=10000, value=100, step=100)
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
        code = st.text_input("场内基金/ETF代码", value="512890", placeholder="例如 512890、159915 或 512890.SH")
        adjust_option = st.selectbox(
            "复权",
            options=list(BACKTEST_ADJUSTMENT_OPTIONS),
            index=0,
        )
        api_key = st.text_input("TickFlow API Key", value=os.getenv("TICKFLOW_API_KEY", ""), type="password")
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
    metric_cols[4].metric("一直持有最大回撤", format_value(summary.get("一直持有最大回撤(%)"), "%"))
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
        summary_df = pd.DataFrame([{"指标": key, "数值": str(value)} for key, value in summary.items()])
        st.dataframe(summary_df, use_container_width=True, hide_index=True)


def build_portfolio_timing_period_table(
    funds,
    allocations,
    end_date,
    *,
    initial_capital: float,
    transaction_cost: float,
    lot_size: int,
) -> pd.DataFrame:
    rows = []
    for label, period_start in build_standard_backtest_periods(end_date):
        try:
            period_result = run_portfolio_timing_backtest(
                funds=funds,
                allocations=allocations,
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
                    "策略收益率(%)": summary.get("总收益率(%)"),
                    "策略年化(%)": summary.get("年化收益率(%)"),
                    "策略最大回撤(%)": summary.get("策略最大回撤(%)"),
                    "一直持有收益率(%)": summary.get("一直持有收益率(%)"),
                    "一直持有年化(%)": summary.get("一直持有年化(%)"),
                    "一直持有最大回撤(%)": summary.get("一直持有最大回撤(%)"),
                    "年化超额(百分点)": summary.get("年化超额收益(百分点)"),
                    "夏普比率": summary.get("夏普比率"),
                    "交易胜率(%)": summary.get("交易胜率(%)"),
                    "交易次数": summary.get("交易次数"),
                }
            )
        except Exception as exc:
            rows.append({"区间": label, "说明": str(exc)})
    return pd.DataFrame(rows)


def render_portfolio_timing_nav_chart(nav_df: pd.DataFrame) -> None:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=nav_df["日期"],
            y=nav_df["账户净值"],
            mode="lines",
            name="组合择时",
            hovertemplate="%{x|%Y-%m-%d}<br>账户净值=%{y:.2f}<extra></extra>",
            line=dict(width=2.4, color="#d62728"),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=nav_df["日期"],
            y=nav_df["一直持有净值"],
            mode="lines",
            name="相同比例一直持有",
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


def parse_portfolio_allocations(config_df: pd.DataFrame) -> list[PortfolioTimingAllocation]:
    required_columns = ["ETF代码", "标的名称", "配置比例(%)", "策略类型", "均线周期", "触发阈值(%)"]
    if config_df is None or config_df.empty:
        raise ValueError("请至少配置一个ETF或现金仓位。")
    missing = [column for column in required_columns if column not in config_df.columns]
    if missing:
        raise ValueError(f"配置表缺少字段：{'、'.join(missing)}")

    allocations = []
    cash_count = 0
    for row_number, row in config_df.iterrows():
        if row[required_columns].isna().all():
            continue
        strategy_label = str(row.get("策略类型", "")).strip()
        strategy = PORTFOLIO_STRATEGY_LABELS.get(strategy_label)
        if strategy is None:
            raise ValueError(f"第 {row_number + 1} 行策略类型无效。")
        weight = pd.to_numeric(row.get("配置比例(%)"), errors="coerce")
        if pd.isna(weight) or float(weight) < 0:
            raise ValueError(f"第 {row_number + 1} 行配置比例不能为负数。")
        if float(weight) == 0:
            continue

        raw_code = str(row.get("ETF代码", "")).strip()
        name = str(row.get("标的名称", "")).strip()
        if strategy == PORTFOLIO_STRATEGY_CASH:
            cash_count += 1
            if cash_count > 1:
                raise ValueError("现金仓位最多配置一行。")
            symbol = ""
            name = name or "现金"
            ma_period = 20
            threshold_pct = 1.0
        else:
            if not raw_code:
                raise ValueError(f"第 {row_number + 1} 行缺少ETF代码。")
            symbol = infer_tickflow_symbol(raw_code)
            name = name or symbol
            ma_period_raw = pd.to_numeric(row.get("均线周期"), errors="coerce")
            threshold_raw = pd.to_numeric(row.get("触发阈值(%)"), errors="coerce")
            ma_period = int(ma_period_raw) if not pd.isna(ma_period_raw) else 20
            threshold_pct = float(threshold_raw) if not pd.isna(threshold_raw) else 1.0
            if strategy in (PORTFOLIO_STRATEGY_TIMING, PORTFOLIO_STRATEGY_HALF_TIMING):
                if ma_period < 2:
                    raise ValueError(f"第 {row_number + 1} 行均线周期必须至少为2。")
                if threshold_pct < 0:
                    raise ValueError(f"第 {row_number + 1} 行触发阈值不能为负数。")
        allocations.append(
            PortfolioTimingAllocation(
                symbol=symbol,
                name=name,
                weight_pct=float(weight),
                strategy=strategy,
                ma_period=ma_period,
                threshold_pct=threshold_pct,
            )
        )
    return allocations


def render_portfolio_timing_mode() -> None:
    default_start_date, default_end_date = default_backtest_dates()
    with st.sidebar:
        st.subheader("组合回测参数")
        portfolio_initial_capital = st.number_input(
            "初始资金",
            min_value=1000.0,
            value=100000.0,
            step=10000.0,
            key="portfolio_timing_initial_capital",
        )
        portfolio_transaction_cost_bp = st.number_input(
            "单边交易成本（万分之）",
            min_value=0.0,
            value=0.6,
            step=0.1,
            key="portfolio_timing_transaction_cost_bp",
        )
        portfolio_lot_size = st.number_input(
            "交易单位",
            min_value=1,
            max_value=10000,
            value=100,
            step=100,
            key="portfolio_timing_lot_size",
        )
        st.subheader("回测区间")
        portfolio_start_date = st.date_input(
            "开始日期",
            value=default_start_date,
            key="portfolio_timing_start_date",
        )
        portfolio_end_date = st.date_input(
            "结束日期",
            value=default_end_date,
            key="portfolio_timing_end_date",
        )

    with st.form("portfolio_timing_form"):
        st.subheader("ETF择时与持仓配置")
        config_df = st.data_editor(
            DEFAULT_PORTFOLIO_CONFIG,
            num_rows="dynamic",
            hide_index=True,
            use_container_width=True,
            column_config={
                "ETF代码": st.column_config.TextColumn("ETF代码", help="现金行可留空"),
                "标的名称": st.column_config.TextColumn("标的名称"),
                "配置比例(%)": st.column_config.NumberColumn("配置比例(%)", min_value=0.0, max_value=100.0, step=1.0, format="%.2f"),
                "策略类型": st.column_config.SelectboxColumn("策略类型", options=list(PORTFOLIO_STRATEGY_LABELS), required=True),
                "均线周期": st.column_config.NumberColumn("均线周期", min_value=2, max_value=250, step=1),
                "触发阈值(%)": st.column_config.NumberColumn("触发阈值(%)", min_value=0.0, max_value=20.0, step=0.1, format="%.1f"),
            },
            key="portfolio_timing_config_editor",
        )
        adjust_option = st.selectbox(
            "复权",
            options=list(BACKTEST_ADJUSTMENT_OPTIONS),
            index=0,
            key="portfolio_timing_adjust",
        )
        api_key = st.text_input(
            "TickFlow API Key",
            value=os.getenv("TICKFLOW_API_KEY", ""),
            type="password",
            key="portfolio_timing_api_key",
        )
        force_refresh = st.checkbox("联网更新数据", value=False, key="portfolio_timing_force_refresh")
        run_clicked = st.form_submit_button("运行组合择时回测", type="primary")

    if not run_clicked:
        st.info("配置比例合计不足100%时，剩余比例自动作为现金；设为0%的行会在本次回测中忽略。半仓策略会把该标的配置拆成50%长期持有和50%均线择时。")
        return
    if pd.Timestamp(portfolio_start_date) > pd.Timestamp(portfolio_end_date):
        st.error("开始日期不能晚于结束日期。")
        return

    try:
        allocations = parse_portfolio_allocations(config_df)
        adjust_value = BACKTEST_ADJUSTMENT_OPTIONS[adjust_option]
        funds = []
        data_messages = []
        for allocation in allocations:
            if allocation.strategy == PORTFOLIO_STRATEGY_CASH:
                continue
            cache_symbol = build_fund_cache_symbol(
                "fund_rotation", allocation.symbol, adjust_value
            )
            cached_df, cache_meta, _cache_period = load_rotation_cache(cache_symbol)
            raw_df = cached_df
            if force_refresh or cached_df is None:
                try:
                    with st.spinner(f"正在获取 {allocation.symbol} 的{adjust_option}日线..."):
                        raw_df = fetch_tickflow_fund_close(
                            symbol=allocation.symbol,
                            api_key=api_key,
                            count=FULL_HISTORY_COUNT,
                            adjust=adjust_value,
                        )
                    save_dataset(
                        cache_symbol,
                        f"{allocation.symbol} {adjust_option}",
                        "tickflow_fund_rotation",
                        "fund_rotation_raw",
                        raw_df,
                        period=FULL_HISTORY_CACHE_PERIOD,
                    )
                    data_messages.append(f"{allocation.symbol}：已更新")
                except Exception as fetch_exc:
                    if cached_df is None:
                        raise ValueError(f"{allocation.symbol} 获取失败：{fetch_exc}") from fetch_exc
                    raw_df = cached_df
                    data_messages.append(f"{allocation.symbol}：更新失败，使用本地缓存")
            else:
                cache_time = format_cache_time(cache_meta.get("last_update_time") if cache_meta else None)
                data_messages.append(f"{allocation.symbol}：本地缓存 {cache_time}")
            fund = normalize_rotation_dataframe(raw_df, fallback_name=allocation.name)
            fund.symbol = allocation.symbol
            fund.name = allocation.name
            funds.append(fund)

        result = run_portfolio_timing_backtest(
            funds=funds,
            allocations=allocations,
            initial_capital=float(portfolio_initial_capital),
            transaction_cost=float(portfolio_transaction_cost_bp) / 10000,
            lot_size=int(portfolio_lot_size),
            start_date=portfolio_start_date,
            end_date=portfolio_end_date,
        )
    except Exception as exc:
        st.error(f"组合择时回测出错：{exc}")
        return

    st.caption("数据来源：" + "；".join(data_messages))
    summary = result.summary
    metric_cols = st.columns(6)
    metric_cols[0].metric("策略收益", format_value(summary.get("总收益率(%)"), "%"))
    metric_cols[1].metric("策略年化", format_value(summary.get("年化收益率(%)"), "%"))
    metric_cols[2].metric("策略最大回撤", format_value(summary.get("策略最大回撤(%)"), "%"))
    metric_cols[3].metric("一直持有收益", format_value(summary.get("一直持有收益率(%)"), "%"))
    metric_cols[4].metric("一直持有年化", format_value(summary.get("一直持有年化(%)"), "%"))
    metric_cols[5].metric("一直持有最大回撤", format_value(summary.get("一直持有最大回撤(%)"), "%"))

    detail_cols = st.columns(6)
    detail_cols[0].metric("年化超额", format_value(summary.get("年化超额收益(百分点)"), "个百分点"))
    detail_cols[1].metric("夏普比率", format_value(summary.get("夏普比率")))
    detail_cols[2].metric("交易胜率", format_value(summary.get("交易胜率(%)"), "%"))
    detail_cols[3].metric("交易次数", format_value(summary.get("交易次数")))
    detail_cols[4].metric("当前ETF仓位", format_value(summary.get("当前ETF仓位(%)"), "%"))
    detail_cols[5].metric("当前现金仓位", format_value(summary.get("当前现金仓位(%)"), "%"))
    st.caption(f"实际回测区间：{summary.get('开始日期')} → {summary.get('结束日期')}；累计交易成本：{format_value(summary.get('累计总成本'))}")

    period_df = build_portfolio_timing_period_table(
        funds,
        allocations,
        result.end_date,
        initial_capital=float(portfolio_initial_capital),
        transaction_cost=float(portfolio_transaction_cost_bp) / 10000,
        lot_size=int(portfolio_lot_size),
    )
    st.subheader("分期回测结果")
    st.dataframe(period_df, use_container_width=True, hide_index=True)

    component_df = result.component_results.copy()
    component_df["策略类型"] = component_df["策略类型"].map(PORTFOLIO_STRATEGY_DISPLAY).fillna(component_df["策略类型"])
    tab_nav, tab_drawdown, tab_trades, tab_positions, tab_daily, tab_summary = st.tabs(
        ["净值走势", "回撤分析", "交易明细", "配置与当前仓位", "每日净值", "摘要"]
    )
    with tab_nav:
        render_portfolio_timing_nav_chart(result.nav_data)
    with tab_drawdown:
        render_drawdown_chart(result.drawdown)
        if not result.yearly_stats.empty:
            st.subheader("年度收益与回撤")
            st.dataframe(result.yearly_stats, use_container_width=True, hide_index=True)
    with tab_trades:
        if result.trades.empty:
            st.info("回测区间内没有择时交易。")
        else:
            st.dataframe(result.trades, use_container_width=True, hide_index=True)
    with tab_positions:
        st.dataframe(component_df, use_container_width=True, hide_index=True)
    with tab_daily:
        st.dataframe(result.nav_data, use_container_width=True, hide_index=True)
    with tab_summary:
        summary_df = pd.DataFrame([{"指标": key, "数值": str(value)} for key, value in summary.items()])
        st.dataframe(summary_df, use_container_width=True, hide_index=True)

    st.download_button(
        "下载组合回测结果 CSV",
        data=to_csv_bytes(result.nav_data),
        file_name="portfolio_timing_nav_data.csv",
        mime="text/csv",
    )


st.set_page_config(page_title="策略回测", layout="wide")
init_db()

st.title("策略回测")
st.caption("支持单标的均线择时、多ETF配置择时，以及按动量排名执行多基金轮动。")

st.markdown(
    """
    <style>
    div[data-testid="stMetric"] * {
        white-space: normal !important;
        overflow: visible !important;
        text-overflow: clip !important;
        overflow-wrap: anywhere;
    }
    div[data-testid="stMetricValue"] {
        font-size: clamp(1.05rem, 1.7vw, 1.55rem) !important;
        line-height: 1.2 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

strategy_mode = st.radio(
    "策略类型",
    options=["单标的MA20择时", "多ETF配置择时", "多基金动量轮动"],
    horizontal=True,
)
if strategy_mode == "单标的MA20择时":
    render_ma20_timing_mode()
    st.stop()
if strategy_mode == "多ETF配置择时":
    render_portfolio_timing_mode()
    st.stop()

uploaded_files = []
tickflow_codes = ""
eastmoney_codes = ""
adjust_option = "前复权（差值）"
api_key = ""
max_workers = 8
force_refresh = False
default_start_date, default_end_date = default_backtest_dates()

st.subheader("数据来源")
data_source = st.radio("数据来源", options=["上传文件", "TickFlow获取", "场外基金"], index=0, horizontal=True)

with st.form("fund_rotation_data_form"):
    if data_source == "上传文件":
        uploaded_files = st.file_uploader(
            "基金数据文件",
            type=["csv", "xlsx", "xls"],
            accept_multiple_files=True,
        )
    elif data_source == "TickFlow获取":
        tickflow_codes = st.text_area(
            "场内基金/ETF代码",
            value="159915 512890",
            height=96,
            placeholder="可输入 159915 512890，或 159915.SZ 512890.SH",
        )
        adjust_option = st.selectbox(
            "复权",
            options=list(BACKTEST_ADJUSTMENT_OPTIONS),
            index=0,
        )
        api_key = st.text_input("TickFlow API Key", value=os.getenv("TICKFLOW_API_KEY", ""), type="password")
        force_refresh = st.checkbox("联网更新数据", value=False)
    elif data_source == "场外基金":
        eastmoney_codes = st.text_area(
            "场外基金代码",
            value="",
            height=96,
            placeholder="可输入多个 6 位代码，例如：000001 110022",
        )
        max_workers = st.number_input("并发数", min_value=1, max_value=12, value=8, step=1)
    run_clicked = st.form_submit_button("运行轮动回测", type="primary")

with st.sidebar:
    st.subheader("回测参数")
    execution_label = st.selectbox(
        "调仓成交方式",
        options=["盘后固定价", "次日开盘"],
        index=0,
        help=(
            "盘后固定价：调仓日收盘后用当日收盘价计算动量，并按同一收盘价模拟成交。"
            "该机制自2026-07-06起扩展至全部A股和ETF；历史区间结果属于按当前规则的模拟，"
            "且假设委托全部成交。"
        ),
    )
    execution_mode = (
        EXECUTION_AFTER_CLOSE
        if execution_label == "盘后固定价"
        else EXECUTION_NEXT_OPEN
    )
    frequency_label = st.selectbox("轮动频率", options=["每周一", "每月1号"], index=0)
    lookback_period = st.number_input("动量周期", min_value=1, max_value=500, value=22, step=1)
    num_positions = st.number_input("持仓数量", min_value=1, max_value=20, value=1, step=1)
    initial_capital = st.number_input("初始资金", min_value=1000.0, value=100000.0, step=10000.0)
    transaction_cost_bp = st.number_input(
        "单边交易成本（万分之）",
        min_value=0.0,
        value=0.6,
        step=0.1,
        key=f"rotation_transaction_cost_bp_{data_source}",
    )
    st.subheader("回测区间")
    rotation_start_date = st.date_input(
        "开始日期",
        value=default_start_date,
        key="rotation_start_date",
    )
    rotation_end_date = st.date_input(
        "结束日期",
        value=default_end_date,
        key="rotation_end_date",
    )

if data_source == "上传文件" and not uploaded_files:
    st.info("请在数据来源区域上传至少两个 CSV 或 Excel 文件。文件需要包含日期列和价格列，例如 trade_date / close。")
    st.stop()

if data_source == "TickFlow获取" and not tickflow_codes.strip():
    st.info("请输入至少两个场内基金/ETF代码，例如：159915 512890。")
    st.stop()

if data_source == "场外基金" and not eastmoney_codes.strip():
    st.info("请输入至少两个场外基金 6 位代码。场外基金使用东方财富累计净值。")
    st.stop()

if not run_clicked:
    if execution_mode == EXECUTION_AFTER_CLOSE:
        st.info("参数设置完成后点击「运行轮动回测」。盘后固定价模式使用调仓日收盘价计算动量，并按同一收盘价模拟成交。")
    else:
        st.info("参数设置完成后点击「运行轮动回测」。次日开盘模式使用前一交易日收盘价计算动量，并按开盘价成交。")
    st.stop()
if pd.Timestamp(rotation_start_date) > pd.Timestamp(rotation_end_date):
    st.error("开始日期不能晚于结束日期。")
    st.stop()

try:
    funds = []
    errors = []
    if data_source == "上传文件":
        for uploaded_file in uploaded_files:
            try:
                raw_df = read_uploaded_table(uploaded_file.getvalue(), uploaded_file.name)
                funds.append(normalize_rotation_dataframe(raw_df, fallback_name=uploaded_file.name))
            except Exception as exc:
                errors.append(f"{uploaded_file.name}: {exc}")
    else:
        if data_source == "场外基金":
            codes = [
                item.strip()
                for item in eastmoney_codes.replace(",", " ").replace("，", " ").replace("\n", " ").split()
                if item.strip()
            ]
            if len(codes) < 2:
                st.error("至少需要输入 2 个场外基金代码。")
                st.stop()
            for code in codes:
                try:
                    if not code.isdigit() or len(code) != 6:
                        raise ValueError("场外基金代码需要 6 位数字。")
                    with st.spinner(f"正在通过东方财富拉取 {code} 的累计净值..."):
                        raw_df = fetch_eastmoney_fund_nav(
                            fund_code=code,
                            full_history=True,
                            max_workers=int(max_workers),
                        )
                    fund = normalize_rotation_dataframe(raw_df, fallback_name=f"{code} 场外基金")
                    fund.trade_lot_size = 0
                    fund.apply_slippage = False
                    funds.append(fund)
                except Exception as exc:
                    errors.append(f"{code}: {exc}")
        else:
            codes = [
                item.strip()
                for item in tickflow_codes.replace(",", " ").replace("，", " ").replace("\n", " ").split()
                if item.strip()
            ]
            if len(codes) < 2:
                st.error("至少需要输入 2 个场内基金/ETF代码。")
                st.stop()
            adjust_value = BACKTEST_ADJUSTMENT_OPTIONS[adjust_option]
            for code in codes:
                try:
                    symbol = infer_tickflow_symbol(code)
                    cache_symbol = build_fund_cache_symbol(
                        "fund_rotation", symbol, adjust_value
                    )
                    cached_df, cache_meta, _cache_period = load_rotation_cache(cache_symbol)
                    if cached_df is not None and not force_refresh:
                        raw_df = cached_df
                        st.info(
                            f"{symbol} 已使用本地缓存，缓存时间："
                            f"{format_cache_time(cache_meta.get('last_update_time') if cache_meta else None)}"
                        )
                    else:
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
                    funds.append(normalize_rotation_dataframe(raw_df, fallback_name=f"{symbol} {adjust_option}"))
                except Exception as exc:
                    errors.append(f"{code}: {exc}")

    if errors:
        st.warning("部分文件未能解析：\n\n" + "\n".join(errors))
    if len(funds) < 2:
        st.error("至少需要成功解析 2 只基金。")
        st.stop()

    frequency = "week" if frequency_label == "每周一" else "month"
    result = run_fund_rotation_backtest(
        funds=funds,
        frequency=frequency,
        lookback_period=int(lookback_period),
        num_positions=int(num_positions),
        initial_capital=float(initial_capital),
        transaction_cost=float(transaction_cost_bp) / 10000,
        start_date=rotation_start_date,
        end_date=rotation_end_date,
        execution_mode=execution_mode,
    )
except Exception as exc:
    st.error(f"回测执行出错：{exc}")
    st.stop()

summary = result.summary
if execution_mode == EXECUTION_AFTER_CLOSE:
    st.caption("盘后固定价回测假设委托可按收盘价全部成交，未模拟时间优先排队导致的部分成交或未成交。")
metric_cols = st.columns(5)
with metric_cols[0]:
    st.metric("总收益率", format_value(summary.get("总收益率(%)"), "%"))
with metric_cols[1]:
    st.metric("年化收益率", format_value(summary.get("年化收益率(%)"), "%"))
with metric_cols[2]:
    st.metric("策略最大回撤", format_value(summary.get("策略最大回撤(%)"), "%"))
with metric_cols[3]:
    st.metric("期末资金", format_value(summary.get("期末资金")))
with metric_cols[4]:
    st.metric("调仓次数", format_value(summary.get("调仓次数")))

cost_cols = st.columns(5)
with cost_cols[0]:
    st.metric("年化波动率", format_value(summary.get("年化波动率(%)"), "%"))
with cost_cols[1]:
    st.metric("夏普比率", format_value(summary.get("夏普比率")))
with cost_cols[2]:
    st.metric("交易胜率", format_value(summary.get("交易胜率(%)"), "%"))
with cost_cols[3]:
    st.metric("累计总成本", format_value(summary.get("累计总成本")))
with cost_cols[4]:
    st.metric("回测区间", f"{summary.get('开始日期')} → {summary.get('结束日期')}")

period_df = build_rotation_period_table(
    funds,
    result.end_date,
    frequency=frequency,
    lookback_period=int(lookback_period),
    num_positions=int(num_positions),
    initial_capital=float(initial_capital),
    transaction_cost=float(transaction_cost_bp) / 10000,
    execution_mode=execution_mode,
)
st.subheader("分期回测结果")
st.dataframe(period_df, use_container_width=True, hide_index=True)
st.download_button(
    "下载分期回测结果 CSV",
    data=to_csv_bytes(period_df),
    file_name="fund_rotation_period_results.csv",
    mime="text/csv",
)

tab_nav, tab_drawdown, tab_trades, tab_daily, tab_summary = st.tabs(
    ["净值走势", "回撤分析", "交易明细", "每日持仓", "摘要"]
)

with tab_nav:
    render_nav_chart(result.nav_data, result.individual_nav_data)
    st.download_button(
        "下载每日净值 CSV",
        data=to_csv_bytes(result.nav_data),
        file_name="fund_rotation_nav_data.csv",
        mime="text/csv",
    )

with tab_drawdown:
    render_drawdown_chart(result.drawdown)
    if not result.yearly_stats.empty:
        st.subheader("年度收益与回撤")
        st.dataframe(result.yearly_stats, use_container_width=True, hide_index=True)
        st.download_button(
            "下载年度统计 CSV",
            data=to_csv_bytes(result.yearly_stats),
            file_name="fund_rotation_yearly_stats.csv",
            mime="text/csv",
        )

with tab_trades:
    st.dataframe(result.trades, use_container_width=True, hide_index=True)
    st.download_button(
        "下载交易明细 CSV",
        data=to_csv_bytes(result.trades),
        file_name="fund_rotation_trades.csv",
        mime="text/csv",
    )

with tab_daily:
    st.dataframe(result.nav_data, use_container_width=True, hide_index=True)
    st.download_button(
        "下载每日持仓 CSV",
        data=to_csv_bytes(result.nav_data),
        file_name="fund_rotation_daily_holdings.csv",
        mime="text/csv",
    )

with tab_summary:
    summary_df = pd.DataFrame([{"指标": key, "数值": str(value)} for key, value in summary.items()])
    st.subheader("策略摘要")
    st.dataframe(summary_df, use_container_width=True, hide_index=True)
    if not result.individual_results.empty:
        st.subheader("一直持有对比")
        st.dataframe(result.individual_results, use_container_width=True, hide_index=True)
