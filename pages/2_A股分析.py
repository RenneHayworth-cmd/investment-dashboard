import os
import re

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from core.cache import load_dataset, save_dataset
from core.db import init_db
from core.ui import (
    LARGE_CHART_HEIGHT,
    SECONDARY_CHART_HEIGHT,
    apply_global_style,
    apply_plotly_layout,
    render_metric_card,
    render_metric_grid,
    render_page_header,
)
from services.fund_analysis import (
    FUND_ADJUSTMENT_OPTIONS,
    FUND_ADJUST_NONE,
    analyze_fund_nav,
    build_fund_cache_symbol,
    calculate_current_drawdown_info,
    fetch_eastmoney_fund_nav,
    fetch_tickflow_fund_close,
    infer_tickflow_symbol,
    normalize_nav_dataframe,
    read_uploaded_table,
    resolve_price_axis_type,
)


def _format_metric(value, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def _format_datetime_text(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value).replace("T", " ")


def _compact_date_range(start: str | None, end: str | None) -> str:
    if not start or not end:
        return "-"
    try:
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        if start_ts.year == end_ts.year:
            return f"{start_ts:%Y-%m-%d} → {end_ts:%m-%d}"
    except Exception:
        pass
    return f"{start} → {end}"


def _safe_file_stem(value: object) -> str:
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value).strip())
    return normalized.strip(" ._") or "A股分析"


def _chart_export_config(
    file_stem: str,
    *,
    height: int,
    scroll_zoom: bool = False,
) -> dict[str, object]:
    return {
        "displayModeBar": True,
        "displaylogo": False,
        "scrollZoom": scroll_zoom,
        "toImageButtonOptions": {
            "format": "png",
            "filename": _safe_file_stem(file_stem),
            "width": 1600,
            "height": height,
            "scale": 2,
        },
    }


def _download_dataframe(df: pd.DataFrame, *, label: str, file_name: str) -> None:
    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        label,
        data=csv_bytes,
        file_name=f"{_safe_file_stem(file_name)}.csv",
        mime="text/csv",
    )


def _text_metric(label: str, display_value: str, tooltip: str) -> None:
    render_metric_card(label, display_value, tooltip)


def _drawdown_metric_grid(items: list[tuple[str, str, str]]) -> None:
    render_metric_grid(items)


def _load_cache(symbol: str, source: str, data_type: str, period: str = "1d"):
    cached_df, meta = load_dataset(symbol, source, data_type, period=period)
    if cached_df is None or not meta or not meta.get("last_update_time"):
        return None, None
    return cached_df, meta


def _merge_raw_data(
    old_df: pd.DataFrame | None,
    new_df: pd.DataFrame,
    *,
    preserve_existing: bool = False,
) -> pd.DataFrame:
    if old_df is None or old_df.empty:
        merged = new_df.copy()
    else:
        merged = pd.concat([old_df, new_df], ignore_index=True)
    if "日期" not in merged.columns:
        return merged
    merged["日期"] = pd.to_datetime(merged["日期"], errors="coerce")
    merged = merged.dropna(subset=["日期"])
    keep = "first" if preserve_existing else "last"
    return merged.sort_values("日期").drop_duplicates("日期", keep=keep).reset_index(drop=True)


st.set_page_config(page_title="A股分析", layout="wide")
init_db()
apply_global_style()

render_page_header(
    "A股分析",
    "输入场外基金、场内基金或 A 股股票代码，或上传净值/价格文件，计算均线、RSI、涨跌幅、波动率、百分位和回撤。",
    eyebrow="A Share",
)

with st.sidebar:
    st.subheader("分析设置")
    input_mode = st.radio("数据来源", options=["场外基金", "场内基金/股票", "上传文件"], index=1, horizontal=False)
    ma_periods = st.multiselect(
        "均线周期",
        options=[20, 60, 120, 250],
        default=[20, 60, 120, 250],
    )
    rsi_period = st.number_input("RSI周期", min_value=5, max_value=60, value=14, step=1)
    base_date = st.date_input("区间基准日", value=pd.Timestamp("2024-09-24"))
    price_axis_mode = st.selectbox("价格轴", options=["自动", "普通坐标", "对数坐标"], index=0)
    force_refresh = st.checkbox("联网更新数据（有缓存时增量）", value=False)
    save_to_cache = st.checkbox("分析后保存分析结果", value=True)

source_df = None
result = None
cache_source = "eastmoney"
analysis_state_key = "a_share_analysis_source"
analysis_state = st.session_state.get(analysis_state_key, {})
fresh_analysis = False


def restore_analysis_source(mode: str, identity: tuple[object, ...]):
    if analysis_state.get("mode") != mode or analysis_state.get("identity") != identity:
        return None
    saved = analysis_state.get("source_df")
    if saved is None or saved.empty:
        return None
    return saved.copy(), str(analysis_state.get("fund_name") or ""), str(
        analysis_state.get("cache_source") or ""
    )

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

    input_identity = ("场外基金", fund_code.strip(), bool(full_history))
    if not submitted:
        restored = restore_analysis_source(input_mode, input_identity)
        if restored is None:
            st.info("输入场外基金 6 位代码后点击「拉取并分析」。场外基金使用东方财富累计净值。")
            st.stop()
        source_df, fund_name, cache_source = restored
        _, nav_df = normalize_nav_dataframe(source_df, fallback_name=fund_name)
        result = analyze_fund_nav(
            nav_df,
            fund_name=fund_name,
            ma_periods=ma_periods,
            rsi_period=int(rsi_period),
            base_date=base_date.strftime("%Y-%m-%d"),
        )

    if submitted:
      try:
        fund_code = fund_code.strip()
        raw_symbol = f"fund_nav_{fund_code}_{'full' if full_history else 'latest'}"
        cached_df, cache_meta = _load_cache(
            raw_symbol,
            "eastmoney",
            "fund_nav_raw",
            period="1d",
        )
        if cached_df is not None and not force_refresh:
            source_df = cached_df
            st.info(f"已使用本地缓存，缓存时间：{_format_datetime_text(cache_meta['last_update_time'])}")
        else:
            if cached_df is not None:
                with st.spinner(f"正在增量更新 {fund_code} 的最新净值..."):
                    latest_df = fetch_eastmoney_fund_nav(
                        fund_code=fund_code,
                        full_history=False,
                        max_workers=int(max_workers),
                    )
                source_df = _merge_raw_data(cached_df, latest_df)
                st.info(
                    f"已基于本地缓存增量更新：{len(cached_df)} 条 → {len(source_df)} 条"
                )
            else:
                with st.spinner(f"正在全量拉取 {fund_code} 的净值数据..."):
                    source_df = fetch_eastmoney_fund_nav(
                        fund_code=fund_code,
                        full_history=full_history,
                        max_workers=int(max_workers),
                    )
            save_dataset(
                symbol=raw_symbol,
                name=f"{fund_code} 场外基金原始净值",
                source="eastmoney",
                data_type="fund_nav_raw",
                df=source_df,
            )
        fund_name, nav_df = normalize_nav_dataframe(source_df, fallback_name=f"{fund_code} 场外基金")
        if fund_name == fund_code:
            fund_name = f"{fund_name} 场外基金"
        result = analyze_fund_nav(
            nav_df,
            fund_name=fund_name,
            ma_periods=ma_periods,
            rsi_period=int(rsi_period),
            base_date=base_date.strftime("%Y-%m-%d"),
        )
        fresh_analysis = True
      except Exception as exc:
          st.error(f"分析失败：{exc}")
          st.stop()
elif input_mode == "场内基金/股票":
    with st.form("exchange_fund_form"):
        col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
        with col1:
            fund_code = st.text_input("场内代码", value="512890", placeholder="例如 512890、600519 或 512890.SH")
        with col2:
            count = st.number_input("日线条数", min_value=300, max_value=10000, value=5000, step=100)
        with col3:
            adjust_option = st.selectbox(
                "复权",
                options=list(FUND_ADJUSTMENT_OPTIONS),
                index=0,
            )
        with col4:
            api_key = st.text_input("API Key", value=os.getenv("TICKFLOW_API_KEY", ""), type="password")
        submitted = st.form_submit_button("拉取并分析", type="primary")

    input_identity = ("场内基金/股票", fund_code.strip(), int(count), adjust_option)
    if not submitted:
        restored = restore_analysis_source(input_mode, input_identity)
        if restored is None:
            st.info("输入场内基金或股票代码后点击「拉取并分析」。场内数据使用 TickFlow 日收盘价。")
            st.stop()
        source_df, fund_name, cache_source = restored
        _, nav_df = normalize_nav_dataframe(source_df, fallback_name=fund_name)
        result = analyze_fund_nav(
            nav_df,
            fund_name=fund_name,
            ma_periods=ma_periods,
            rsi_period=int(rsi_period),
            base_date=base_date.strftime("%Y-%m-%d"),
        )

    if submitted:
      try:
        symbol = infer_tickflow_symbol(fund_code)
        adjust_value = FUND_ADJUSTMENT_OPTIONS[adjust_option]
        raw_symbol = build_fund_cache_symbol("fund_close", symbol, adjust_value)
        cached_df, cache_meta = _load_cache(
            raw_symbol,
            "tickflow",
            "fund_close_raw",
            period=f"{int(count)}_1d",
        )
        if cached_df is not None and not force_refresh:
            source_df = cached_df
            st.info(f"已使用本地缓存，缓存时间：{_format_datetime_text(cache_meta['last_update_time'])}")
        else:
            if cached_df is not None and adjust_value == FUND_ADJUST_NONE:
                incremental_count = min(max(120, int(count) // 20), int(count))
                with st.spinner(f"正在增量更新 {symbol} 最近 {incremental_count} 条日线..."):
                    latest_df = fetch_tickflow_fund_close(
                        symbol=symbol,
                        api_key=api_key,
                        count=incremental_count,
                        adjust=adjust_value,
                    )
                source_df = _merge_raw_data(
                    cached_df,
                    latest_df,
                    preserve_existing=True,
                )
                st.info(
                    f"已基于本地缓存增量更新：{len(cached_df)} 条 → {len(source_df)} 条"
                )
            else:
                action_text = "重建" if cached_df is not None else "拉取"
                with st.spinner(f"正在通过 TickFlow 全量{action_text} {symbol} 的日线收盘价..."):
                    source_df = fetch_tickflow_fund_close(
                        symbol=symbol,
                        api_key=api_key,
                        count=int(count),
                        adjust=adjust_value,
                    )
            save_dataset(
                symbol=raw_symbol,
                name=f"{symbol} 场内基金/股票原始收盘价",
                source="tickflow",
                data_type="fund_close_raw",
                period=f"{int(count)}_1d",
                df=source_df,
            )
        fund_name, nav_df = normalize_nav_dataframe(source_df, fallback_name=f"{symbol} 场内基金/股票")
        if fund_name == symbol:
            fund_name = f"{fund_name} 场内基金/股票"
        result = analyze_fund_nav(
            nav_df,
            fund_name=fund_name,
            ma_periods=ma_periods,
            rsi_period=int(rsi_period),
            base_date=base_date.strftime("%Y-%m-%d"),
        )
        cache_source = "tickflow"
        fresh_analysis = True
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
        input_identity = ("上传文件", uploaded.name, len(raw), hash(raw))
        fresh_analysis = True
    except Exception as exc:
        st.error(f"分析失败：{exc}")
        st.stop()

if fresh_analysis:
    st.session_state[analysis_state_key] = {
        "mode": input_mode,
        "identity": input_identity,
        "source_df": source_df.copy(),
        "fund_name": fund_name,
        "cache_source": cache_source,
    }

if save_to_cache and fresh_analysis:
    save_dataset(
        symbol=f"fund_analysis_{fund_name}",
        name=f"{fund_name} A股分析",
        source=cache_source,
        data_type="fund_analysis",
        df=result.dataframe,
    )

summary = result.summary
download_name = _safe_file_stem(result.fund_name)
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
        render_metric_card(label, _format_metric(value, suffix))

tabs = st.tabs(["走势", "回撤分析", "摘要", "指标数据", "原始数据"])

with tabs[0]:
    df = result.dataframe.copy()
    price_columns = ["price", *[f"ma_{period}" for period in ma_periods]]
    price_axis_type = resolve_price_axis_type(df, price_axis_mode, price_columns=price_columns)
    fig = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.50, 0.18, 0.16, 0.16],
        vertical_spacing=0.04,
        subplot_titles=("日K线", "RSI", "20日涨幅", summary.get("滚动年化类型", "滚动年化收益率")),
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
    apply_plotly_layout(fig, height=LARGE_CHART_HEIGHT)
    fig.update_xaxes(hoverformat="%Y-%m-%d")
    fig.update_xaxes(rangeslider=dict(visible=True, thickness=0.06), row=4, col=1)
    price_yaxis_options = {
        "title_text": "价格（对数）" if price_axis_type == "log" else "价格",
        "type": price_axis_type,
        "row": 1,
        "col": 1,
    }
    if price_axis_type == "linear":
        price_yaxis_options["rangemode"] = "tozero"
    fig.update_yaxes(**price_yaxis_options)
    fig.update_yaxes(title_text="RSI", row=2, col=1, range=[0, 100])
    fig.update_yaxes(title_text="涨幅%", row=3, col=1)
    fig.update_yaxes(title_text="年化%", row=4, col=1)
    st.plotly_chart(
        fig,
        use_container_width=True,
        config=_chart_export_config(
            f"{download_name}_走势分析",
            height=LARGE_CHART_HEIGHT,
            scroll_zoom=True,
        ),
    )
    st.caption("下载图片：将鼠标移到图表右上角，点击相机图标即可保存高清 PNG。")

with tabs[1]:
    df = result.dataframe.copy()
    price_axis_type = resolve_price_axis_type(df, price_axis_mode, price_columns=("price", "running_peak"))
    drawdown_info = {
        "峰值日": summary.get("最大回撤峰值日"),
        "谷底日": summary.get("最大回撤谷底日"),
    }
    current_drawdown_info = calculate_current_drawdown_info(df)
    current_status = str(current_drawdown_info.get("当前修复状态") or "-")
    current_tooltip = (
        f"当前回撤峰值日：{current_drawdown_info.get('当前回撤峰值日', '-')}"
        f"；当前谷底日：{current_drawdown_info.get('当前谷底日', '-')}"
    )
    _drawdown_metric_grid(
        [
            ("最大回撤", _format_metric(summary.get("最大回撤(%)"), "%"), "历史最大回撤"),
            ("谷底日期", str(drawdown_info["谷底日"] or "-"), f"最大回撤峰值日：{drawdown_info['峰值日'] or '-'}"),
            ("当前回撤", _format_metric(current_drawdown_info.get("当前回撤(%)"), "%"), "最新交易日相对历史高点的回撤"),
            ("当前回撤时间", _format_metric(current_drawdown_info.get("当前回撤时间"), "天"), "从当前回撤峰值日至最新交易日"),
            ("修复状态", current_status, current_tooltip),
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
    apply_plotly_layout(dd_fig, height=SECONDARY_CHART_HEIGHT)
    dd_fig.update_xaxes(hoverformat="%Y-%m-%d")
    dd_fig.update_yaxes(
        title_text="价格（对数）" if price_axis_type == "log" else "价格",
        type=price_axis_type,
        row=1,
        col=1,
    )
    dd_fig.update_yaxes(title_text="回撤%", row=2, col=1)
    st.plotly_chart(
        dd_fig,
        use_container_width=True,
        config=_chart_export_config(
            f"{download_name}_回撤分析",
            height=SECONDARY_CHART_HEIGHT,
        ),
    )
    st.caption("下载图片：将鼠标移到图表右上角，点击相机图标即可保存高清 PNG。")

    st.subheader("回撤波段")
    if result.drawdown_periods.empty:
        st.info("没有发现独立回撤波段。")
    else:
        st.dataframe(result.drawdown_periods, use_container_width=True, hide_index=True)
        _download_dataframe(
            result.drawdown_periods,
            label="下载回撤波段 CSV",
            file_name=f"{download_name}_回撤波段",
        )

    st.subheader("年度最大回撤")
    if result.yearly_drawdowns.empty:
        st.info("没有年度回撤数据。")
    else:
        st.dataframe(result.yearly_drawdowns, use_container_width=True, hide_index=True)
        _download_dataframe(
            result.yearly_drawdowns,
            label="下载年度最大回撤 CSV",
            file_name=f"{download_name}_年度最大回撤",
        )

with tabs[2]:
    summary_df = pd.DataFrame(
        [{"指标": key, "数值": _format_metric(value)} for key, value in summary.items()]
    )
    st.dataframe(summary_df, use_container_width=True, hide_index=True)
    _download_dataframe(
        summary_df,
        label="下载摘要 CSV",
        file_name=f"{download_name}_摘要",
    )

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
    indicator_df = result.dataframe[display_cols].sort_values("date", ascending=False)
    st.dataframe(indicator_df, use_container_width=True, hide_index=True)
    _download_dataframe(
        indicator_df,
        label="下载指标数据 CSV",
        file_name=f"{download_name}_指标数据",
    )

with tabs[4]:
    st.dataframe(source_df, use_container_width=True)
    _download_dataframe(
        source_df,
        label="下载原始数据 CSV",
        file_name=f"{download_name}_原始数据",
    )
