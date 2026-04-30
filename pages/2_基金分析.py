import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from core.cache import save_dataset
from core.db import init_db
from services.fund_analysis import (
    analyze_fund_nav,
    fetch_eastmoney_fund_nav,
    fetch_tickflow_fund_close,
    infer_tickflow_symbol,
    normalize_nav_dataframe,
    read_uploaded_table,
)


def _format_metric(value, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


st.set_page_config(page_title="基金分析", layout="wide")
init_db()

st.title("基金分析")
st.caption("输入基金代码或上传净值文件，计算均线、RSI、涨跌幅、波动率、百分位和回撤。")

with st.sidebar:
    st.subheader("分析设置")
    input_mode = st.radio("数据来源", options=["场外基金", "场内基金/ETF", "上传文件"], horizontal=False)
    ma_periods = st.multiselect(
        "均线周期",
        options=[20, 60, 120, 250],
        default=[20, 60, 120, 250],
    )
    rsi_period = st.number_input("RSI周期", min_value=5, max_value=60, value=14, step=1)
    base_date = st.date_input("区间基准日", value=pd.Timestamp("2024-09-30"))
    save_to_cache = st.checkbox("分析后保存到本地缓存", value=True)

source_df = None
result = None
cache_source = "eastmoney"

if input_mode == "场外基金":
    with st.form("fund_code_form"):
        col1, col2, col3 = st.columns([2, 1, 1])
        with col1:
            fund_code = st.text_input("场外基金代码", value="000001", placeholder="例如 000001")
        with col2:
            full_history = st.checkbox("全部历史", value=True)
        with col3:
            max_workers = st.number_input("并发数", min_value=1, max_value=12, value=8, step=1)
        submitted = st.form_submit_button("拉取并分析", type="primary")

    if not submitted:
        st.info("输入场外基金 6 位代码后点击「拉取并分析」。场外基金使用东方财富累计净值。")
        st.stop()

    try:
        with st.spinner(f"正在拉取 {fund_code} 的净值数据..."):
            source_df = fetch_eastmoney_fund_nav(
                fund_code=fund_code,
                full_history=full_history,
                max_workers=int(max_workers),
            )
        fund_name, nav_df = normalize_nav_dataframe(source_df, fallback_name=f"{fund_code} 场外基金")
        result = analyze_fund_nav(
            nav_df,
            fund_name=fund_name,
            ma_periods=ma_periods,
            rsi_period=int(rsi_period),
            base_date=base_date.strftime("%Y-%m-%d"),
        )
    except Exception as exc:
        st.error(f"分析失败：{exc}")
        st.stop()
elif input_mode == "场内基金/ETF":
    with st.form("exchange_fund_form"):
        col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
        with col1:
            fund_code = st.text_input("场内代码", value="512890", placeholder="例如 512890 或 512890.SH")
        with col2:
            count = st.number_input("日线条数", min_value=300, max_value=10000, value=5000, step=100)
        with col3:
            adjust_option = st.selectbox("复权", options=["不复权", "前复权", "后复权"], index=0)
        with col4:
            api_key = st.text_input("TickFlow Key", value="", type="password")
        submitted = st.form_submit_button("拉取并分析", type="primary")

    if not submitted:
        st.info("输入场内基金/ETF 代码后点击「拉取并分析」。场内基金使用 TickFlow 日收盘价。")
        st.stop()

    adjust_map = {"不复权": None, "前复权": "forward", "后复权": "backward"}
    try:
        symbol = infer_tickflow_symbol(fund_code)
        with st.spinner(f"正在通过 TickFlow 拉取 {symbol} 的日线收盘价..."):
            source_df = fetch_tickflow_fund_close(
                symbol=symbol,
                api_key=api_key,
                count=int(count),
                adjust=adjust_map[adjust_option],
            )
        fund_name, nav_df = normalize_nav_dataframe(source_df, fallback_name=f"{symbol} 场内基金")
        result = analyze_fund_nav(
            nav_df,
            fund_name=fund_name,
            ma_periods=ma_periods,
            rsi_period=int(rsi_period),
            base_date=base_date.strftime("%Y-%m-%d"),
        )
        cache_source = "tickflow"
    except Exception as exc:
        st.error(f"分析失败：{exc}")
        st.stop()
else:
    uploaded = st.file_uploader("上传 CSV / Excel 净值文件", type=["csv", "xlsx", "xls"])
    cache_source = "upload"

    if uploaded is None:
        st.info("请选择一个包含日期列和净值/价格列的文件。常见列名如：日期、trade_date、累计净值、单位净值、close。")
        st.stop()

    try:
        raw = uploaded.getvalue()
        source_df = read_uploaded_table(raw, uploaded.name)
        fallback_name = uploaded.name.rsplit(".", 1)[0]
        fund_name, nav_df = normalize_nav_dataframe(source_df, fallback_name=fallback_name)
        result = analyze_fund_nav(
            nav_df,
            fund_name=fund_name,
            ma_periods=ma_periods,
            rsi_period=int(rsi_period),
            base_date=base_date.strftime("%Y-%m-%d"),
        )
    except Exception as exc:
        st.error(f"分析失败：{exc}")
        st.stop()

if save_to_cache:
    save_dataset(
        symbol=f"fund_analysis_{fund_name}",
        name=f"{fund_name} 基金分析",
        source=cache_source,
        data_type="fund_analysis",
        df=result.dataframe,
    )

summary = result.summary
st.subheader(result.fund_name)

metric_cols = st.columns(5)
metric_items = [
    ("最新价格", summary.get("最新价格")),
    ("20日涨幅", summary.get("20日涨幅(%)"), "%"),
    ("60日涨幅", summary.get("60日涨幅(%)"), "%"),
    (f"RSI({int(rsi_period)})", summary.get(f"RSI({int(rsi_period)})")),
    ("滚动年化", summary.get(summary.get("滚动年化类型")), "%"),
]
for idx, item in enumerate(metric_items):
    label = item[0]
    value = item[1]
    suffix = item[2] if len(item) > 2 else ""
    with metric_cols[idx]:
        st.metric(label, _format_metric(value, suffix))

tabs = st.tabs(["走势", "回撤分析", "摘要", "指标数据", "原始数据"])

with tabs[0]:
    df = result.dataframe.copy()
    fig = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.50, 0.18, 0.16, 0.16],
        vertical_spacing=0.04,
        subplot_titles=("价格与均线", "RSI", "20日涨幅", summary.get("滚动年化类型", "滚动年化收益率")),
    )
    fig.add_trace(
        go.Scatter(x=df["date"], y=df["price"], mode="lines", name="价格", line=dict(width=2)),
        row=1,
        col=1,
    )
    ma_colors = {20: "#eab308", 60: "#2563eb", 120: "#dc2626", 250: "#059669"}
    for period in ma_periods:
        ma_col = f"ma_{period}"
        if ma_col in df.columns:
            fig.add_trace(
                go.Scatter(
                    x=df["date"],
                    y=df[ma_col],
                    mode="lines",
                    name=f"MA{period}",
                    line=dict(width=1.4, color=ma_colors.get(period)),
                ),
                row=1,
                col=1,
            )
    rsi_col = f"rsi_{int(rsi_period)}"
    fig.add_trace(
        go.Scatter(x=df["date"], y=df[rsi_col], mode="lines", name=f"RSI({int(rsi_period)})"),
        row=2,
        col=1,
    )
    fig.add_hline(y=70, line_dash="dash", line_color="#dc2626", row=2, col=1)
    fig.add_hline(y=30, line_dash="dash", line_color="#059669", row=2, col=1)
    fig.add_trace(
        go.Scatter(x=df["date"], y=df["return_20d_pct"], mode="lines", name="20日涨幅(%)"),
        row=3,
        col=1,
    )
    fig.add_hline(y=0, line_color="#6b7280", row=3, col=1)
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["rolling_annual_return_pct"],
            mode="lines",
            name=summary.get("滚动年化类型", "滚动年化收益率(%)"),
            line=dict(color="#7c3aed"),
        ),
        row=4,
        col=1,
    )
    fig.add_hline(y=0, line_color="#6b7280", row=4, col=1)
    fig.update_layout(height=900, hovermode="x unified", legend=dict(orientation="h"))
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="RSI", row=2, col=1, range=[0, 100])
    fig.update_yaxes(title_text="涨幅%", row=3, col=1)
    fig.update_yaxes(title_text="年化%", row=4, col=1)
    st.plotly_chart(fig, use_container_width=True)

with tabs[1]:
    df = result.dataframe.copy()
    drawdown_info = {
        "峰值日": summary.get("最大回撤峰值日"),
        "谷底日": summary.get("最大回撤谷底日"),
        "修复日": summary.get("最大回撤修复日"),
        "下跌天数": summary.get("最大回撤下跌天数"),
        "修复天数": summary.get("最大回撤修复天数"),
        "是否已修复": summary.get("最大回撤是否已修复"),
    }
    info_cols = st.columns(4)
    with info_cols[0]:
        st.metric("最大回撤", _format_metric(summary.get("最大回撤(%)"), "%"))
    with info_cols[1]:
        st.metric("峰值日 → 谷底日", f"{drawdown_info['峰值日']} → {drawdown_info['谷底日']}")
    with info_cols[2]:
        st.metric("下跌天数", _format_metric(drawdown_info["下跌天数"], "天"))
    with info_cols[3]:
        recovery_text = (
            _format_metric(drawdown_info["修复天数"], "天")
            if drawdown_info["是否已修复"]
            else f"未修复，截至 {drawdown_info['修复日']}"
        )
        st.metric("修复状态", recovery_text)

    dd_fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.62, 0.38],
        vertical_spacing=0.06,
        subplot_titles=("价格、历史峰值与回撤区域", "回撤曲线"),
    )
    dd_fig.add_trace(
        go.Scatter(x=df["date"], y=df["price"], mode="lines", name="价格", line=dict(color="#2563eb")),
        row=1,
        col=1,
    )
    dd_fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["running_peak"],
            mode="lines",
            name="历史峰值",
            line=dict(color="#6b7280", dash="dash"),
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
            line=dict(color="rgba(255,255,255,0)"),
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
            line=dict(color="#dc2626"),
        ),
        row=2,
        col=1,
    )
    dd_fig.add_hline(y=0, line_color="#6b7280", row=2, col=1)
    if drawdown_info["谷底日"]:
        trough_date = pd.Timestamp(drawdown_info["谷底日"])
        trough_row = df[df["date"] == trough_date]
        if not trough_row.empty:
            dd_fig.add_trace(
                go.Scatter(
                    x=[trough_row.iloc[0]["date"]],
                    y=[trough_row.iloc[0]["drawdown_pct"]],
                    mode="markers",
                    name="最大回撤谷底",
                    marker=dict(color="#16a34a", size=10),
                ),
                row=2,
                col=1,
            )
    dd_fig.update_layout(height=720, hovermode="x unified", legend=dict(orientation="h"))
    dd_fig.update_yaxes(title_text="价格", row=1, col=1)
    dd_fig.update_yaxes(title_text="回撤%", row=2, col=1)
    st.plotly_chart(dd_fig, use_container_width=True)

    table_cols = st.columns(2)
    with table_cols[0]:
        st.subheader("回撤波段")
        if result.drawdown_periods.empty:
            st.info("没有发现独立回撤波段。")
        else:
            st.dataframe(result.drawdown_periods, use_container_width=True, hide_index=True)
    with table_cols[1]:
        st.subheader("年度最大回撤")
        if result.yearly_drawdowns.empty:
            st.info("没有年度回撤数据。")
        else:
            st.dataframe(result.yearly_drawdowns, use_container_width=True, hide_index=True)

with tabs[2]:
    summary_df = pd.DataFrame(
        [{"指标": key, "数值": _format_metric(value)} for key, value in summary.items()]
    )
    st.dataframe(summary_df, use_container_width=True, hide_index=True)

with tabs[3]:
    display_cols = [
        "date",
        "price",
        "daily_return_pct",
        "return_20d_pct",
        "return_60d_pct",
        "ytd_return_pct",
        "base_date_return_pct",
        "volatility_20d_pct",
        "momentum_volatility_20d",
        f"rsi_{int(rsi_period)}",
        "price_percentile",
        "drawdown_pct",
        "running_peak",
        "rolling_annual_return_pct",
    ]
    for period in ma_periods:
        display_cols.extend([f"ma_{period}", f"ma_{period}_deviation_pct"])
    display_cols = [col for col in display_cols if col in result.dataframe.columns]
    st.dataframe(result.dataframe[display_cols].sort_values("date", ascending=False), use_container_width=True)

    csv_bytes = result.dataframe.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button(
        "下载分析结果 CSV",
        data=csv_bytes,
        file_name=f"{result.fund_name}_analysis.csv",
        mime="text/csv",
    )

with tabs[4]:
    st.dataframe(source_df, use_container_width=True)
