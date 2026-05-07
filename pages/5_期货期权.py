import html
import os
import re

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from core.cache import load_dataset, save_dataset
from core.db import init_db
from services.fund_analysis import (
    calculate_current_drawdown_info,
    calculate_yearly_drawdowns,
    extract_drawdown_periods,
)
from services.futures_spread import CONTRACT_PREFIXES
from services.futures_options_analysis import (
    DATA_TYPE_AUTO,
    DATA_TYPE_FUTURES,
    DATA_TYPE_OPTIONS,
    add_indicators,
    build_summary,
    fetch_futures_option_data,
    infer_current_main_contract,
    is_main_continuous_symbol,
    normalize_main_continuous_symbol,
    should_fetch_options,
)


st.set_page_config(page_title="期货期权", layout="wide")
init_db()

st.title("期货期权")
st.caption("输入期货或期权合约，查看日线走势、均线、涨跌幅、波动率和成交持仓摘要。")

st.markdown(
    """
    <style>
    div[data-testid="stMetric"] * {
        white-space: normal !important;
        overflow: visible !important;
        text-overflow: clip !important;
        overflow-wrap: anywhere;
    }
    div[data-testid="stMetric"] label {
        font-size: clamp(0.85rem, 1.1vw, 1rem) !important;
        line-height: 1.25 !important;
    }
    div[data-testid="stMetricValue"] {
        font-size: clamp(1.05rem, 1.7vw, 1.55rem) !important;
        line-height: 1.2 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def format_value(value, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def format_cache_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value).replace("T", " ")


def cache_key(symbol: str, data_type: str, period: str, count: int) -> str:
    safe_symbol = symbol.strip().replace(".", "_").replace("/", "_")
    return f"futures_option_{safe_symbol}_{data_type}_{period}_{count}"


def display_symbol(raw_symbol: str) -> str:
    return normalize_main_continuous_symbol(raw_symbol) or raw_symbol.strip()


def display_data_kind(symbol: str, data_kind: str) -> str:
    if data_kind not in {"期货", "期货主连"}:
        return data_kind
    match = re.match(r"^([A-Za-z]+)", symbol)
    if not match:
        return data_kind
    product = match.group(1).upper()
    return CONTRACT_PREFIXES.get(product, data_kind)


def centered_table(df: pd.DataFrame) -> None:
    headers = "".join(f"<th>{html.escape(str(col))}</th>" for col in df.columns)
    rows = []
    for _, row in df.iterrows():
        cells = "".join(f"<td>{html.escape(format_value(row[col]))}</td>" for col in df.columns)
        rows.append(f"<tr>{cells}</tr>")
    st.markdown(
        f"""
        <style>
        .centered-market-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.92rem;
        }}
        .centered-market-table th,
        .centered-market-table td {{
            text-align: center;
            padding: 0.45rem 0.6rem;
            border-bottom: 1px solid rgba(49, 51, 63, 0.12);
            white-space: nowrap;
        }}
        .centered-market-table th {{
            font-weight: 600;
            background: rgba(49, 51, 63, 0.04);
        }}
        </style>
        <table class="centered-market-table">
            <thead><tr>{headers}</tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
        """,
        unsafe_allow_html=True,
    )


def build_drawdown_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    drawdown_df = df[["date", "close"]].copy()
    drawdown_df.columns = ["date", "price"]
    drawdown_df["price"] = pd.to_numeric(drawdown_df["price"], errors="coerce")
    drawdown_df = drawdown_df.dropna(subset=["date", "price"]).sort_values("date").reset_index(drop=True)
    running_peak = drawdown_df["price"].cummax()
    drawdown_df["running_peak"] = running_peak
    drawdown_df["drawdown_pct"] = (drawdown_df["price"] / running_peak - 1) * 100
    drawdown_df["drawdown_pct"] = drawdown_df["drawdown_pct"].round(2)
    return drawdown_df


def render_drawdown_metrics(drawdown_df: pd.DataFrame) -> None:
    current_info = calculate_current_drawdown_info(drawdown_df)
    max_drawdown = drawdown_df["drawdown_pct"].min() if not drawdown_df.empty else float("nan")
    trough_date = "-"
    if not drawdown_df.empty and pd.notna(max_drawdown):
        trough_row = drawdown_df.loc[drawdown_df["drawdown_pct"].idxmin()]
        trough_date = pd.Timestamp(trough_row["date"]).strftime("%Y-%m-%d")
    first_row = st.columns(4)
    with first_row[0]:
        st.metric("最大回撤", format_value(max_drawdown, "%"))
    with first_row[1]:
        st.metric("谷底日期", trough_date)
    with first_row[2]:
        st.metric("当前回撤", format_value(current_info.get("当前回撤(%)"), "%"))
    with first_row[3]:
        st.metric("当前回撤时间", format_value(current_info.get("当前回撤时间"), "天"))
    second_row = st.columns(4)
    with second_row[0]:
        recovery_status = "已修复" if current_info.get("当前回撤是否已修复") else "未修复"
        st.metric("修复状态", recovery_status)


def build_info_summary_df(
    symbol: str,
    kind_label: str,
    source: str,
    period: str,
    count: int,
    summary: dict[str, object],
    df: pd.DataFrame,
) -> pd.DataFrame:
    rows = [
        {"指标": "合约代码", "数值": symbol},
        {"指标": "合约品种", "数值": kind_label},
        {"指标": "K线周期", "数值": period},
        {"指标": "获取条数", "数值": count},
        {"指标": "数据来源", "数值": source},
        {"指标": "最新日期", "数值": summary.get("最新日期", "-")},
    ]
    if "date" in df.columns and not df.empty:
        dates = pd.to_datetime(df["date"], errors="coerce").dropna()
        if not dates.empty:
            rows.append({"指标": "数据范围", "数值": f"{dates.min():%Y-%m-%d} → {dates.max():%Y-%m-%d}"})
    if "current_main_contract" in df.columns and df["current_main_contract"].notna().any():
        rows.append({"指标": "当前主力合约", "数值": str(df["current_main_contract"].dropna().iloc[-1])})

    for key in ("最新收盘", "20日涨跌幅(%)", "20日波动率(%)", "价格百分位", "数据行数"):
        if key in summary:
            rows.append({"指标": key, "数值": format_value(summary.get(key))})
    return pd.DataFrame(rows)


with st.sidebar:
    st.subheader("参数")
    raw_symbol = st.text_input("合约代码", value="IM2606", placeholder="例如 IM2606、IM0、IM主连、mo2606C5800")
    data_type = st.selectbox("数据类型", options=[DATA_TYPE_AUTO, DATA_TYPE_FUTURES, DATA_TYPE_OPTIONS], index=0)
    period = st.selectbox("K线周期", options=["1d", "1w", "1M", "1Q", "1Y"], index=0)
    count = st.number_input("获取条数", min_value=20, max_value=10000, value=500, step=100)
    ma_periods = st.multiselect("均线周期", options=[5, 10, 20, 60, 120], default=[5, 20, 60])
    api_key = st.text_input("TickFlow API Key", value=os.getenv("TICKFLOW_API_KEY", ""), type="password")
    use_free = st.checkbox("使用免费历史数据服务", value=True)
    force_refresh = st.checkbox("联网更新数据", value=False)
    save_to_cache = st.checkbox("分析后保存到本地缓存", value=True)
    analyze_clicked = st.button("获取数据并分析", type="primary")

if not analyze_clicked:
    st.info("输入期货或期权合约后点击左侧「获取数据并分析」。期货主连可输入 IM0、I0、AU0，或 IM主连、I主连、AU主连。期权支持股指期权 io/ho/mo 和铁矿石期权 i。")
    st.stop()

if not raw_symbol.strip():
    st.warning("请输入合约代码。")
    st.stop()

if not should_fetch_options(raw_symbol, data_type) and not is_main_continuous_symbol(raw_symbol) and not use_free and not api_key:
    st.warning("使用 TickFlow 完整服务时需要填写 API Key，或勾选免费历史数据服务。")
    st.stop()

symbol_for_cache = display_symbol(raw_symbol)
symbol_key = cache_key(symbol_for_cache, data_type, period, int(count))
cached_df, cache_meta = load_dataset(symbol_key, "market", "futures_option", period=period)
result = None

if cached_df is not None and not force_refresh:
    st.info(f"已使用本地缓存，缓存时间：{format_cache_time(cache_meta['last_update_time'])}")
    is_chain = "date" not in cached_df.columns or "close" not in cached_df.columns
    result_df = cached_df.copy()
    if not is_chain:
        result_df["date"] = pd.to_datetime(result_df["date"], errors="coerce")
        if "current_main_contract" not in result_df.columns and is_main_continuous_symbol(raw_symbol):
            try:
                current_main = infer_current_main_contract(display_symbol(raw_symbol))
            except Exception:
                current_main = None
            if current_main:
                result_df["current_main_contract"] = current_main
        result_df = add_indicators(result_df, ma_periods)
    summary = {"数据行数": len(result_df)}
    if not is_chain and not result_df.empty:
        summary = build_summary(result_df)
    source = "本地缓存"
    data_kind = "期权链" if is_chain else ("期权日线" if should_fetch_options(raw_symbol, data_type) else ("期货主连" if is_main_continuous_symbol(raw_symbol) else "期货"))
else:
    with st.spinner("正在获取行情数据..."):
        try:
            result = fetch_futures_option_data(
                raw_symbol=raw_symbol,
                data_type=data_type,
                period=period,
                count=int(count),
                api_key=api_key,
                use_free=use_free,
                ma_periods=ma_periods,
            )
        except Exception as exc:
            st.error(f"获取失败：{exc}")
            st.stop()
    result_df = result.dataframe
    summary = result.summary
    source = result.source
    data_kind = result.data_kind
    is_chain = result.is_chain
    if save_to_cache:
        save_dataset(
            symbol=symbol_key,
            name=f"{display_symbol(raw_symbol)} 期货期权数据",
            source="market",
            data_type="futures_option",
            period=period,
            df=result_df,
        )

normalized_display_symbol = display_symbol(raw_symbol)
kind_label = display_data_kind(normalized_display_symbol, data_kind)
st.subheader(f"{normalized_display_symbol} · {kind_label}")
st.caption(f"数据来源：{source}")

if is_chain:
    st.info("当前输入是期权月份，展示期权链表格；输入具体期权合约（例如 mo2606C5800）可查看日线走势。")
    st.dataframe(result_df, use_container_width=True, hide_index=True)
    csv_bytes = result_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button("下载期权链 CSV", data=csv_bytes, file_name=f"{display_symbol(raw_symbol)}_option_chain.csv", mime="text/csv")
    st.stop()

metric_items = [
    ("最新收盘", summary.get("最新收盘")),
    ("20日涨跌", summary.get("20日涨跌幅(%)"), "%"),
    ("20日波动", summary.get("20日波动率(%)"), "%"),
    ("价格百分位", summary.get("价格百分位")),
]
for idx, item in enumerate(metric_items):
    label = item[0]
    value = item[1]
    suffix = item[2] if len(item) > 2 else ""
    if idx % 4 == 0:
        metric_cols = st.columns(4)
    with metric_cols[idx % 4]:
        st.metric(label, format_value(value, suffix))

show_drawdown = data_kind in {"期货", "期货主连"}
tab_names = ["走势"]
if show_drawdown:
    tab_names.append("回撤分析")
tab_names.append("摘要")
tab_names.append("明细数据")
tabs = st.tabs(tab_names)

with tabs[0]:
    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.62, 0.20, 0.18],
        vertical_spacing=0.05,
        subplot_titles=("走势与均线", "日涨跌幅", "成交量"),
    )
    close_hovertemplate = "%{x|%Y-%m-%d}<br>收盘价=%{y}<extra></extra>"
    close_customdata = None
    if "current_main_contract" in result_df.columns:
        close_customdata = result_df[["current_main_contract"]]
        close_hovertemplate = "%{x|%Y-%m-%d}<br>收盘价=%{y}<br>当前主力=%{customdata[0]}<extra></extra>"
    fig.add_trace(
        go.Scatter(
            x=result_df["date"],
            y=result_df["close"],
            mode="lines",
            name="收盘价",
            line=dict(width=2),
            customdata=close_customdata,
            hovertemplate=close_hovertemplate,
        ),
        row=1,
        col=1,
    )
    ma_colors = {5: "#d97706", 10: "#7c3aed", 20: "#2563eb", 60: "#dc2626", 120: "#059669"}
    for period_item in ma_periods:
        ma_col = f"ma_{period_item}"
        if ma_col in result_df.columns:
            fig.add_trace(
                go.Scatter(
                    x=result_df["date"],
                    y=result_df[ma_col],
                    mode="lines",
                    name=f"MA{period_item}",
                    line=dict(width=1.3, color=ma_colors.get(period_item)),
                ),
                row=1,
                col=1,
            )
    fig.add_trace(
        go.Bar(x=result_df["date"], y=result_df["daily_return_pct"], name="日涨跌幅(%)", marker_color="#64748b"),
        row=2,
        col=1,
    )
    if "volume" in result_df.columns:
        fig.add_trace(
            go.Bar(x=result_df["date"], y=result_df["volume"], name="成交量", marker_color="#94a3b8"),
            row=3,
            col=1,
        )
    else:
        fig.add_trace(
            go.Scatter(x=result_df["date"], y=[0] * len(result_df), mode="lines", name="无成交量数据", line=dict(color="#cbd5e1")),
            row=3,
            col=1,
        )
    fig.add_hline(y=0, line_color="#6b7280", row=2, col=1)
    fig.update_layout(height=820, hovermode="x unified", legend=dict(orientation="h"))
    fig.update_xaxes(hoverformat="%Y-%m-%d", rangeslider=dict(visible=True, thickness=0.06), row=3, col=1)
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="涨跌%", row=2, col=1)
    fig.update_yaxes(title_text="成交量", row=3, col=1)
    st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})

summary_tab = tabs[2] if show_drawdown else tabs[1]
detail_tab = tabs[3] if show_drawdown else tabs[2]

if show_drawdown:
    with tabs[1]:
        drawdown_df = build_drawdown_dataframe(result_df)
        drawdown_periods = extract_drawdown_periods(drawdown_df)
        yearly_drawdowns = calculate_yearly_drawdowns(drawdown_df)
        render_drawdown_metrics(drawdown_df)

        dd_fig = make_subplots(
            rows=2,
            cols=1,
            shared_xaxes=True,
            row_heights=[0.62, 0.38],
            vertical_spacing=0.06,
            subplot_titles=("价格、历史峰值与回撤区域", "回撤曲线"),
        )
        dd_fig.add_trace(
            go.Scatter(x=drawdown_df["date"], y=drawdown_df["price"], mode="lines", name="价格", line=dict(color="#2563eb")),
            row=1,
            col=1,
        )
        dd_fig.add_trace(
            go.Scatter(x=drawdown_df["date"], y=drawdown_df["running_peak"], mode="lines", name="历史峰值", line=dict(color="#6b7280", dash="dash")),
            row=1,
            col=1,
        )
        dd_fig.add_trace(
            go.Scatter(
                x=drawdown_df["date"].tolist() + drawdown_df["date"].tolist()[::-1],
                y=drawdown_df["running_peak"].tolist() + drawdown_df["price"].tolist()[::-1],
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
            go.Scatter(x=drawdown_df["date"], y=drawdown_df["drawdown_pct"], mode="lines", fill="tozeroy", name="回撤(%)", line=dict(color="#dc2626")),
            row=2,
            col=1,
        )
        dd_fig.add_hline(y=0, line_color="#6b7280", row=2, col=1)
        dd_fig.update_layout(height=720, hovermode="x unified", legend=dict(orientation="h"))
        dd_fig.update_xaxes(hoverformat="%Y-%m-%d")
        dd_fig.update_yaxes(title_text="价格", row=1, col=1)
        dd_fig.update_yaxes(title_text="回撤%", row=2, col=1)
        st.plotly_chart(dd_fig, use_container_width=True)

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
    summary_df = build_info_summary_df(
        symbol=normalized_display_symbol,
        kind_label=kind_label,
        source=source,
        period=period,
        count=int(count),
        summary=summary,
        df=result_df,
    )
    st.dataframe(summary_df, use_container_width=True, hide_index=True)

with detail_tab:
    detail_df = result_df.sort_values("date", ascending=False)
    st.dataframe(detail_df, use_container_width=True, hide_index=True)
    csv_bytes = result_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button("下载行情数据 CSV", data=csv_bytes, file_name=f"{display_symbol(raw_symbol)}_market.csv", mime="text/csv")
