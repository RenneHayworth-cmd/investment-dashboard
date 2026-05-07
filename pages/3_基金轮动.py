import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.cache import load_dataset, save_dataset
from core.db import init_db
from services.fund_analysis import (
    fetch_eastmoney_fund_nav,
    fetch_tickflow_fund_close,
    infer_tickflow_symbol,
    read_uploaded_table,
)
from services.fund_rotation import normalize_rotation_dataframe, run_fund_rotation_backtest


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
                    y=group["单独持有净值"],
                    mode="lines",
                    name=f"单独持有：{label}",
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


st.set_page_config(page_title="基金轮动", layout="wide")
init_db()

st.title("基金轮动")
st.caption("上传文件，或输入场内/场外基金代码获取数据，按 22 个交易日动量逻辑满仓轮动。场内 ETF 按开盘价成交并计滑点，场外基金按累计净值成交。")

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

with st.sidebar:
    st.subheader("回测参数")
    data_source = st.radio("数据来源", options=["上传文件", "TickFlow获取", "场外基金"], index=0)
    uploaded_files = []
    tickflow_codes = ""
    eastmoney_codes = ""
    count = 5000
    adjust_option = "前复权"
    api_key = ""
    max_workers = 8
    force_refresh = False
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
        count = st.number_input("日线条数", min_value=300, max_value=10000, value=5000, step=100)
        adjust_option = st.selectbox("复权", options=["前复权", "后复权"], index=0)
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
    run_clicked = st.button("运行轮动回测", type="primary")

if data_source == "上传文件" and not uploaded_files:
    st.info("请在左侧上传至少两个 CSV 或 Excel 文件。文件需要包含日期列和价格列，例如 trade_date / close。")
    st.stop()

if data_source == "TickFlow获取" and not tickflow_codes.strip():
    st.info("请输入至少两个场内基金/ETF代码，例如：159915 512890。")
    st.stop()

if data_source == "场外基金" and not eastmoney_codes.strip():
    st.info("请输入至少两个场外基金 6 位代码。场外基金使用东方财富累计净值。")
    st.stop()

if not run_clicked:
    st.info("参数设置完成后点击左侧「运行轮动回测」。当前策略为始终满仓，信号用前一交易日收盘计算，调仓按开盘价成交并计入买卖滑点。")
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
            adjust_map = {"前复权": "forward", "后复权": "backward"}
            adjust_value = adjust_map[adjust_option]
            for code in codes:
                try:
                    symbol = infer_tickflow_symbol(code)
                    cache_symbol = f"fund_rotation_{symbol}_{adjust_value}"
                    cache_period = f"{int(count)}_1d"
                    cached_df, cache_meta = load_dataset(
                        cache_symbol,
                        "tickflow_fund_rotation",
                        "fund_rotation_raw",
                        period=cache_period,
                    )
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
                                count=int(count),
                                adjust=adjust_value,
                            )
                        save_dataset(
                            cache_symbol,
                            f"{symbol} {adjust_option}",
                            "tickflow_fund_rotation",
                            "fund_rotation_raw",
                            raw_df,
                            period=cache_period,
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
    )
except Exception as exc:
    st.error(f"回测执行出错：{exc}")
    st.stop()

summary = result.summary
metric_cols = st.columns(5)
with metric_cols[0]:
    st.metric("总收益率", format_value(summary.get("总收益率(%)"), "%"))
with metric_cols[1]:
    st.metric("年化收益率", format_value(summary.get("年化收益率(%)"), "%"))
with metric_cols[2]:
    st.metric("最大回撤", format_value(summary.get("最大回撤(%)"), "%"))
with metric_cols[3]:
    st.metric("期末资金", format_value(summary.get("期末资金")))
with metric_cols[4]:
    st.metric("调仓次数", format_value(summary.get("调仓次数")))

cost_cols = st.columns(4)
with cost_cols[0]:
    st.metric("年化波动率", format_value(summary.get("年化波动率(%)"), "%"))
with cost_cols[1]:
    st.metric("夏普比率", format_value(summary.get("夏普比率")))
with cost_cols[2]:
    st.metric("累计总成本", format_value(summary.get("累计总成本")))
with cost_cols[3]:
    st.metric("回测区间", f"{summary.get('开始日期')} → {summary.get('结束日期')}")

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
    summary_df = pd.DataFrame([{"指标": key, "数值": value} for key, value in summary.items()])
    st.subheader("策略摘要")
    st.dataframe(summary_df, use_container_width=True, hide_index=True)
    if not result.individual_results.empty:
        st.subheader("单只持有对比")
        st.dataframe(result.individual_results, use_container_width=True, hide_index=True)
