import html

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from core.cache import load_dataset, save_dataset
from core.db import init_db
from services.fund_analysis import analyze_fund_nav, normalize_nav_dataframe
from services.us_stock_analysis import fetch_tickflow_us_daily, infer_us_symbol, parse_us_symbols


def _format_metric(value, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def _load_cache(symbol: str, source: str, data_type: str, period: str = "1d"):
    cached_df, meta = load_dataset(symbol, source, data_type, period=period)
    if cached_df is None or not meta or not meta.get("last_update_time"):
        return None, None
    return cached_df, meta


def _merge_raw_data(old_df: pd.DataFrame | None, new_df: pd.DataFrame) -> pd.DataFrame:
    if old_df is None or old_df.empty:
        merged = new_df.copy()
    else:
        merged = pd.concat([old_df, new_df], ignore_index=True)
    if "日期" not in merged.columns:
        return merged
    merged["日期"] = pd.to_datetime(merged["日期"], errors="coerce")
    merged = merged.dropna(subset=["日期"])
    return merged.sort_values("日期").drop_duplicates("日期", keep="last").reset_index(drop=True)


def _text_metric(label: str, display_value: str, tooltip: str) -> None:
    st.markdown(
        f"""
        <div title="{html.escape(tooltip)}" style="
            border: 1px solid rgba(49, 51, 63, 0.2);
            border-radius: 6px;
            padding: 0.65rem 0.75rem;
            min-height: 84px;
        ">
            <div style="font-size:0.875rem;color:rgba(49,51,63,0.72);margin-bottom:0.35rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{html.escape(label)}</div>
            <div style="font-size:1.25rem;line-height:1.25;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{html.escape(display_value)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _compact_date_range(start: str | None, end: str | None) -> str:
    if not start or not end:
        return "-"
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if start_ts.year == end_ts.year:
        return f"{start_ts:%Y-%m-%d} → {end_ts:%m-%d}"
    return f"{start} → {end}"


st.set_page_config(page_title="美股分析", layout="wide")
init_db()

st.title("美股分析")
st.caption("输入美股代码，通过 TickFlow 获取日线收盘价，计算均线、RSI、滚动年化收益率和回撤。")

with st.sidebar:
    st.subheader("分析设置")
    ma_periods = st.multiselect("均线周期", options=[20, 60, 120, 250], default=[20, 60, 120, 250])
    rsi_period = st.number_input("RSI周期", min_value=5, max_value=60, value=14, step=1)
    base_date = st.date_input("区间基准日", value=pd.Timestamp("2025-04-07"))
    force_refresh = st.checkbox("联网更新数据（有缓存时增量）", value=False)
    save_to_cache = st.checkbox("分析后保存分析结果", value=True)

with st.form("us_stock_form"):
    col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
    with col1:
        symbol_text = st.text_input("美股代码", value="AAPL", placeholder="例如 AAPL、MSFT、COWZ")
    with col2:
        count = st.number_input("日线条数", min_value=300, max_value=10000, value=1500, step=100)
    with col3:
        adjust_option = st.selectbox("复权", options=["前复权", "后复权", "不复权"], index=0)
    with col4:
        api_key = st.text_input("TickFlow Key", value="", type="password")
    submitted = st.form_submit_button("拉取并分析", type="primary")

if not submitted:
    st.info("输入美股代码后点击「拉取并分析」。可输入 AAPL、MSFT、SPMO、COWZ 等。")
    st.stop()

symbols = parse_us_symbols(symbol_text)
if not symbols:
    st.warning("请输入至少一个美股代码。")
    st.stop()
if len(symbols) > 1:
    st.warning("当前页面一次分析一个标的；已使用第一个代码。")

adjust_map = {"前复权": "forward", "后复权": "backward", "不复权": None}
try:
    symbol = infer_us_symbol(symbols[0])
    adjust_value = adjust_map[adjust_option]
    raw_symbol = f"us_daily_{symbol}_{adjust_value or 'none'}"
    period = f"{int(count)}_1d"
    cached_df, cache_meta = _load_cache(raw_symbol, "tickflow_us", "us_daily_raw", period=period)

    if cached_df is not None and not force_refresh:
        source_df = cached_df
        st.info(f"已使用本地缓存，缓存时间：{cache_meta['last_update_time']}")
    else:
        if cached_df is not None:
            incremental_count = min(max(120, int(count) // 20), int(count))
            with st.spinner(f"正在增量更新 {symbol} 最近 {incremental_count} 条日线..."):
                latest_df = fetch_tickflow_us_daily(
                    symbol=symbol,
                    api_key=api_key,
                    count=incremental_count,
                    adjust=adjust_value,
                )
            source_df = _merge_raw_data(cached_df, latest_df)
            st.info(f"已基于本地缓存增量更新：{len(cached_df)} 条 → {len(source_df)} 条")
        else:
            with st.spinner(f"正在通过 TickFlow 拉取 {symbol} 日线数据..."):
                source_df = fetch_tickflow_us_daily(
                    symbol=symbol,
                    api_key=api_key,
                    count=int(count),
                    adjust=adjust_value,
                )
        save_dataset(
            symbol=raw_symbol,
            name=f"{symbol} 美股日线",
            source="tickflow_us",
            data_type="us_daily_raw",
            period=period,
            df=source_df,
        )

    stock_name, nav_df = normalize_nav_dataframe(source_df, fallback_name=symbol)
    if stock_name == symbol:
        stock_name = f"{symbol} 美股"
    result = analyze_fund_nav(
        nav_df,
        fund_name=stock_name,
        ma_periods=ma_periods,
        rsi_period=int(rsi_period),
        base_date=base_date.strftime("%Y-%m-%d"),
    )
except Exception as exc:
    st.error(f"分析失败：{exc}")
    st.stop()

if save_to_cache:
    save_dataset(
        symbol=f"us_analysis_{result.fund_name}",
        name=f"{result.fund_name} 美股分析",
        source="tickflow_us",
        data_type="us_analysis",
        df=result.dataframe,
    )

summary = result.summary
st.subheader(result.fund_name)
st.caption(f"数据范围：{summary.get('起始日期')} 至 {summary.get('最新日期')}，共 {summary.get('数据行数')} 条")

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
    fig.add_trace(go.Scatter(x=df["date"], y=df["price"], mode="lines", name="价格", line=dict(width=2)), row=1, col=1)
    ma_colors = {20: "#eab308", 60: "#2563eb", 120: "#dc2626", 250: "#059669"}
    for period_item in ma_periods:
        ma_col = f"ma_{period_item}"
        if ma_col in df.columns:
            fig.add_trace(
                go.Scatter(x=df["date"], y=df[ma_col], mode="lines", name=f"MA{period_item}", line=dict(width=1.4, color=ma_colors.get(period_item))),
                row=1,
                col=1,
            )
    rsi_col = f"rsi_{int(rsi_period)}"
    fig.add_trace(go.Scatter(x=df["date"], y=df[rsi_col], mode="lines", name=f"RSI({int(rsi_period)})"), row=2, col=1)
    fig.add_hline(y=70, line_dash="dash", line_color="#dc2626", row=2, col=1)
    fig.add_hline(y=30, line_dash="dash", line_color="#059669", row=2, col=1)
    fig.add_trace(go.Scatter(x=df["date"], y=df["return_20d_pct"], mode="lines", name="20日涨幅(%)"), row=3, col=1)
    fig.add_hline(y=0, line_color="#6b7280", row=3, col=1)
    fig.add_trace(
        go.Scatter(x=df["date"], y=df["rolling_annual_return_pct"], mode="lines", name=summary.get("滚动年化类型", "滚动年化收益率(%)"), line=dict(color="#7c3aed")),
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
        full = f"{drawdown_info['峰值日']} → {drawdown_info['谷底日']}"
        _text_metric("峰值日 → 谷底日", _compact_date_range(drawdown_info["峰值日"], drawdown_info["谷底日"]), full)
    with info_cols[2]:
        st.metric("下跌天数", _format_metric(drawdown_info["下跌天数"], "天"))
    with info_cols[3]:
        if drawdown_info["是否已修复"]:
            recovery_text = f"已修复：{_format_metric(drawdown_info['修复天数'], '天')}"
            recovery_display = recovery_text
        else:
            recovery_text = f"未修复，截至 {drawdown_info['修复日']}"
            recovery_display = "未修复"
        _text_metric("修复状态", recovery_display, recovery_text)

    dd_fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.62, 0.38],
        vertical_spacing=0.06,
        subplot_titles=("价格、历史峰值与回撤区域", "回撤曲线"),
    )
    dd_fig.add_trace(go.Scatter(x=df["date"], y=df["price"], mode="lines", name="价格", line=dict(color="#2563eb")), row=1, col=1)
    dd_fig.add_trace(go.Scatter(x=df["date"], y=df["running_peak"], mode="lines", name="历史峰值", line=dict(color="#6b7280", dash="dash")), row=1, col=1)
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
    dd_fig.add_trace(go.Scatter(x=df["date"], y=df["drawdown_pct"], mode="lines", fill="tozeroy", name="回撤(%)", line=dict(color="#dc2626")), row=2, col=1)
    dd_fig.add_hline(y=0, line_color="#6b7280", row=2, col=1)
    dd_fig.update_layout(height=720, hovermode="x unified", legend=dict(orientation="h"))
    dd_fig.update_yaxes(title_text="价格", row=1, col=1)
    dd_fig.update_yaxes(title_text="回撤%", row=2, col=1)
    st.plotly_chart(dd_fig, use_container_width=True)

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("回撤波段")
        st.dataframe(result.drawdown_periods, use_container_width=True, hide_index=True)
    with col_b:
        st.subheader("年度最大回撤")
        st.dataframe(result.yearly_drawdowns, use_container_width=True, hide_index=True)

with tabs[2]:
    summary_df = pd.DataFrame([{"指标": key, "数值": _format_metric(value)} for key, value in summary.items()])
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
    for period_item in ma_periods:
        display_cols.extend([f"ma_{period_item}", f"ma_{period_item}_deviation_pct"])
    display_cols = [col for col in display_cols if col in result.dataframe.columns]
    st.dataframe(result.dataframe[display_cols].sort_values("date", ascending=False), use_container_width=True)
    csv_bytes = result.dataframe.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button("下载分析结果 CSV", data=csv_bytes, file_name=f"{result.fund_name}_analysis.csv", mime="text/csv")

with tabs[4]:
    st.dataframe(source_df, use_container_width=True)
