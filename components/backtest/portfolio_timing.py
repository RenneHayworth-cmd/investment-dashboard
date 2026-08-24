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
    PORTFOLIO_STRATEGY_CASH,
    PORTFOLIO_STRATEGY_HALF_TIMING,
    PORTFOLIO_STRATEGY_HOLD,
    PORTFOLIO_STRATEGY_TIMING,
    PortfolioTimingAllocation,
    build_standard_backtest_periods,
    normalize_rotation_dataframe,
    run_portfolio_timing_backtest,
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


PORTFOLIO_STRATEGY_LABELS = {
    "一直持有": PORTFOLIO_STRATEGY_HOLD,
    "纯择时": PORTFOLIO_STRATEGY_TIMING,
    "半仓持有、半仓择时": PORTFOLIO_STRATEGY_HALF_TIMING,
    "现金": PORTFOLIO_STRATEGY_CASH,
}
PORTFOLIO_STRATEGY_DISPLAY = {
    value: key for key, value in PORTFOLIO_STRATEGY_LABELS.items()
}
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


def parse_portfolio_allocations(
    config_df: pd.DataFrame,
) -> list[PortfolioTimingAllocation]:
    required_columns = [
        "ETF代码",
        "标的名称",
        "配置比例(%)",
        "策略类型",
        "均线周期",
        "触发阈值(%)",
    ]
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
            if strategy in (
                PORTFOLIO_STRATEGY_TIMING,
                PORTFOLIO_STRATEGY_HALF_TIMING,
            ):
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
                "配置比例(%)": st.column_config.NumberColumn(
                    "配置比例(%)",
                    min_value=0.0,
                    max_value=100.0,
                    step=1.0,
                    format="%.2f",
                ),
                "策略类型": st.column_config.SelectboxColumn(
                    "策略类型",
                    options=list(PORTFOLIO_STRATEGY_LABELS),
                    required=True,
                ),
                "均线周期": st.column_config.NumberColumn(
                    "均线周期", min_value=2, max_value=250, step=1
                ),
                "触发阈值(%)": st.column_config.NumberColumn(
                    "触发阈值(%)",
                    min_value=0.0,
                    max_value=20.0,
                    step=0.1,
                    format="%.1f",
                ),
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
        force_refresh = st.checkbox(
            "联网更新数据", value=False, key="portfolio_timing_force_refresh"
        )
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
                    with st.spinner(
                        f"正在获取 {allocation.symbol} 的{adjust_option}日线..."
                    ):
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
                        raise ValueError(
                            f"{allocation.symbol} 获取失败：{fetch_exc}"
                        ) from fetch_exc
                    raw_df = cached_df
                    data_messages.append(f"{allocation.symbol}：更新失败，使用本地缓存")
            else:
                cache_time = format_cache_time(
                    cache_meta.get("last_update_time") if cache_meta else None
                )
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
    metric_cols[5].metric(
        "一直持有最大回撤", format_value(summary.get("一直持有最大回撤(%)"), "%")
    )

    detail_cols = st.columns(6)
    detail_cols[0].metric(
        "年化超额", format_value(summary.get("年化超额收益(百分点)"), "个百分点")
    )
    detail_cols[1].metric("夏普比率", format_value(summary.get("夏普比率")))
    detail_cols[2].metric("交易胜率", format_value(summary.get("交易胜率(%)"), "%"))
    detail_cols[3].metric("交易次数", format_value(summary.get("交易次数")))
    detail_cols[4].metric("当前ETF仓位", format_value(summary.get("当前ETF仓位(%)"), "%"))
    detail_cols[5].metric("当前现金仓位", format_value(summary.get("当前现金仓位(%)"), "%"))
    st.caption(
        f"实际回测区间：{summary.get('开始日期')} → {summary.get('结束日期')}；"
        f"累计交易成本：{format_value(summary.get('累计总成本'))}"
    )

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
    component_df["策略类型"] = (
        component_df["策略类型"]
        .map(PORTFOLIO_STRATEGY_DISPLAY)
        .fillna(component_df["策略类型"])
    )
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
        summary_df = pd.DataFrame(
            [{"指标": key, "数值": str(value)} for key, value in summary.items()]
        )
        st.dataframe(summary_df, use_container_width=True, hide_index=True)

    st.download_button(
        "下载组合回测结果 CSV",
        data=to_csv_bytes(result.nav_data),
        file_name="portfolio_timing_nav_data.csv",
        mime="text/csv",
    )


__all__ = [
    "DEFAULT_PORTFOLIO_CONFIG",
    "PORTFOLIO_STRATEGY_DISPLAY",
    "PORTFOLIO_STRATEGY_LABELS",
    "build_portfolio_timing_period_table",
    "parse_portfolio_allocations",
    "render_portfolio_timing_mode",
    "render_portfolio_timing_nav_chart",
]
