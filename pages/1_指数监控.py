from datetime import datetime
import html
import os
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.cache import load_dataset
from core.db import init_db
from core.ui import DEFAULT_CHART_HEIGHT, apply_global_style, apply_plotly_layout, render_page_header
from services.index_ma20 import (
    INDEX_CONFIG,
    build_summary,
    fetch_index_history,
    filter_market_trading_dates,
    sanitize_index_report_market_dates,
)
from services.index_realtime import (
    apply_realtime_quotes_to_summary,
    fetch_futures_main_contract_names,
    fetch_realtime_index_quotes,
    find_pending_post_close_index_names,
    format_index_display_name,
    load_futures_main_contract_names,
    manual_quote_request_names,
    quote_is_visible_for_manual_display,
    save_futures_main_contract_names,
)
from services.market_calendar import MARKET_WINDOWS, is_market_trading_day, latest_completed_trade_date
from services.update_tasks import run_index_ma20_update


st.set_page_config(page_title="指数监控", layout="wide")
init_db()
apply_global_style()

render_page_header("指数监控", "跟踪主要指数、跨市场指数和期货主连相对 MA20 的位置与趋势。", eyebrow="Index Monitor")

INDEX_UPDATE_WORKERS = 8

if "index_update_notice" not in st.session_state:
    st.session_state.index_update_notice = None
if "selected_index_detail" not in st.session_state:
    st.session_state.selected_index_detail = None
if "index_update_timings" not in st.session_state:
    st.session_state.index_update_timings = []
if "index_realtime_quote_cache" not in st.session_state:
    st.session_state.index_realtime_quote_cache = {}
if "index_lunch_quote_keys" not in st.session_state:
    st.session_state.index_lunch_quote_keys = []
if "index_futures_main_contracts" not in st.session_state:
    st.session_state.index_futures_main_contracts = load_futures_main_contract_names()


def format_update_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value.replace("T", " ")


def format_number(value) -> str:
    if pd.isna(value):
        return "-"
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}".rstrip("0").rstrip(".")
    return str(value)


def display_index_name(index_name: str) -> str:
    return format_index_display_name(
        index_name,
        st.session_state.get("index_futures_main_contracts", {}),
    )


def get_query_index_detail() -> str | None:
    selected = st.session_state.get("selected_index_detail")
    if selected in INDEX_CONFIG:
        return selected

    value = st.query_params.get("index_detail")
    if isinstance(value, list):
        value = value[0] if value else None
    if not value:
        return None
    index_name = unquote(str(value))
    return index_name if index_name in INDEX_CONFIG else None


def select_index_detail(index_name: str) -> None:
    st.session_state.selected_index_detail = index_name
    st.query_params["index_detail"] = index_name


def clear_index_detail() -> None:
    st.session_state.selected_index_detail = None
    st.query_params.clear()


def load_index_detail(index_name: str, index_config: dict) -> pd.DataFrame | None:
    return fetch_index_history(index_name, index_config, days=10000)


def build_detail_dataframe(detail_df: pd.DataFrame, index_name: str) -> pd.DataFrame:
    close_col = f"{index_name}_收盘价"
    if detail_df is None or detail_df.empty or close_col not in detail_df.columns:
        return pd.DataFrame()

    result = detail_df[["日期", close_col]].copy()
    result.columns = ["日期", "收盘价"]
    result = filter_market_trading_dates(
        result,
        str(INDEX_CONFIG.get(index_name, {}).get("market_group") or ""),
        date_column="日期",
    )
    result["日期"] = pd.to_datetime(result["日期"], errors="coerce")
    result["收盘价"] = pd.to_numeric(result["收盘价"], errors="coerce")
    result = result.dropna(subset=["日期", "收盘价"]).sort_values("日期").reset_index(drop=True)
    if result.empty:
        return result
    for period in (5, 20, 60, 120, 250):
        result[f"MA{period}"] = result["收盘价"].rolling(window=period).mean()
    if INDEX_CONFIG.get(index_name, {}).get("show_ma20_deviation", True):
        result["偏离率(%)"] = (result["收盘价"] - result["MA20"]) / result["MA20"] * 100
    else:
        result["偏离率(%)"] = pd.NA
    result["日涨跌幅(%)"] = result["收盘价"].pct_change() * 100
    result["累计涨跌幅(%)"] = (result["收盘价"] / result["收盘价"].iloc[0] - 1) * 100
    result["历史高点"] = result["收盘价"].cummax()
    result["回撤(%)"] = (result["收盘价"] / result["历史高点"] - 1) * 100
    result["RSI(14)"] = calculate_rsi(result["收盘价"])
    return result


def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    delta = prices.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(~((loss == 0) & (gain > 0)), 100)
    rsi = rsi.where(~((loss == 0) & (gain == 0)), 50)
    return rsi


def filter_detail_range(detail: pd.DataFrame, range_label: str) -> pd.DataFrame:
    if detail.empty:
        return detail

    latest_date = detail["日期"].max()
    if range_label == "今年来":
        start_date = pd.Timestamp(year=latest_date.year, month=1, day=1)
    elif range_label == "近3年":
        start_date = latest_date - pd.DateOffset(years=3)
    elif range_label == "近5年":
        start_date = latest_date - pd.DateOffset(years=5)
    elif range_label == "近10年":
        start_date = latest_date - pd.DateOffset(years=10)
    elif range_label == "成立来":
        return detail
    else:
        start_date = latest_date - pd.DateOffset(years=1)
    return detail[detail["日期"] >= start_date].copy()


def period_return(detail: pd.DataFrame, periods: int) -> float | None:
    if len(detail) <= periods:
        return None
    current = detail.iloc[-1]["收盘价"]
    previous = detail.iloc[-periods - 1]["收盘价"]
    if pd.isna(current) or pd.isna(previous) or previous == 0:
        return None
    return (current / previous - 1) * 100


def format_pct(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:.2f}%"


def render_detail_summary(
    index_name: str,
    detail: pd.DataFrame,
    view_df: pd.DataFrame,
    range_label: str,
    current_drawdown: float,
    max_drawdown: float,
) -> None:
    config = INDEX_CONFIG.get(index_name, {})
    latest = detail.iloc[-1]
    first = detail.iloc[0]
    year_df = detail[detail["日期"] >= pd.Timestamp(year=latest["日期"].year, month=1, day=1)]
    ytd_return = None
    if len(year_df) >= 2 and year_df.iloc[0]["收盘价"] != 0:
        ytd_return = (latest["收盘价"] / year_df.iloc[0]["收盘价"] - 1) * 100
    summary_items = [
        ("指数名称", display_index_name(index_name)),
        ("指数代码", str(config.get("display_symbol", config.get("tickflow_symbol", config.get("code", "-"))))),
        ("市场分组", str(config.get("market_group", "-"))),
        ("数据源", str(config.get("source", "-"))),
        ("起始日期", f"{first['日期']:%Y-%m-%d}"),
        ("最新日期", f"{latest['日期']:%Y-%m-%d}"),
        ("数据行数", str(len(detail))),
        ("最新价格", format_number(latest["收盘价"])),
        ("20日涨幅(%)", format_number(period_return(detail, 20))),
        ("60日涨幅(%)", format_number(period_return(detail, 60))),
        ("YTD涨幅(%)", format_number(ytd_return)),
        ("RSI(14)", format_number(latest["RSI(14)"])),
        ("当前回撤(%)", format_number(current_drawdown)),
        ("最大回撤(%)", format_number(max_drawdown)),
        ("累计涨跌幅(%)", format_number(latest["累计涨跌幅(%)"])),
        ("当前区间", range_label),
        ("区间范围", f"{view_df.iloc[0]['日期']:%Y-%m-%d} 至 {view_df.iloc[-1]['日期']:%Y-%m-%d}"),
        ("区间样本", str(len(view_df))),
    ]
    summary_df = pd.DataFrame(summary_items, columns=["指标", "数值"])
    st.dataframe(summary_df, use_container_width=True, hide_index=True)


def render_index_detail(report_df: pd.DataFrame, index_name: str) -> None:
    st.divider()
    title_col, action_col = st.columns([5, 1])
    title_col.markdown(f"### {display_index_name(index_name)}")
    action_col.button("返回全部", key="clear_index_detail", on_click=clear_index_detail)

    with st.spinner(f"正在读取 {index_name} 的本地长历史数据..."):
        try:
            detail_df = load_index_detail(index_name, INDEX_CONFIG[index_name])
        except Exception as exc:
            st.warning(f"{index_name} 历史详情获取失败：{exc}")
            detail_df = None

    detail = build_detail_dataframe(detail_df, index_name)
    if detail.empty:
        detail = build_detail_dataframe(report_df, index_name)
        if not detail.empty:
            st.info("长历史缓存暂时不可用，当前先展示本地监控缓存。")
    if detail.empty:
        st.info("当前没有可展示的历史详情数据。")
        return

    latest = detail.iloc[-1]
    first = detail.iloc[0]
    max_drawdown = detail["回撤(%)"].min()
    current_drawdown = latest["回撤(%)"]

    st.caption(f"数据范围：{first['日期']:%Y-%m-%d} 至 {latest['日期']:%Y-%m-%d}，共 {len(detail)} 条")

    metric_cols = st.columns(6)
    metric_cols[0].metric("最新价格", format_number(latest["收盘价"]))
    metric_cols[1].metric("20日涨幅", format_pct(period_return(detail, 20)))
    metric_cols[2].metric("60日涨幅", format_pct(period_return(detail, 60)))
    metric_cols[3].metric("RSI(14)", format_number(latest["RSI(14)"]))
    metric_cols[4].metric("当前回撤", f"{current_drawdown:.2f}%")
    metric_cols[5].metric("最大回撤", f"{max_drawdown:.2f}%")

    range_label = st.segmented_control(
        "走势区间",
        options=["近一年", "今年来", "近3年", "近5年", "近10年", "成立来"],
        default="近一年",
        key=f"index_detail_range_{index_name}",
    )
    view_df = filter_detail_range(detail, range_label)
    if view_df.empty:
        view_df = detail

    view_max_drawdown = view_df["回撤(%)"].min()
    view_max_drawdown_date = view_df.loc[view_df["回撤(%)"].idxmin(), "日期"]

    price_axis = "log" if view_df["收盘价"].min() > 0 else "linear"
    trend_tab, drawdown_tab, summary_tab, table_tab = st.tabs(["走势", "回撤", "摘要", "数据"])
    with trend_tab:
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=view_df["日期"],
                y=view_df["收盘价"],
                mode="lines",
                name="收盘价",
                line={"width": 2},
            )
        )
        ma_colors = {
            5: "rgb(37,99,235)",
            20: "rgb(234,88,12)",
            60: "rgb(22,163,74)",
            120: "rgb(147,51,234)",
            250: "rgb(75,85,99)",
        }
        for period in (5, 20, 60, 120, 250):
            fig.add_trace(
                go.Scatter(
                    x=view_df["日期"],
                    y=view_df[f"MA{period}"],
                    mode="lines",
                    name=f"MA{period}",
                    line={"width": 1.3, "color": ma_colors[period]},
                )
            )
        apply_plotly_layout(fig, height=DEFAULT_CHART_HEIGHT)
        fig.update_layout(yaxis={"type": price_axis, "title": "价格"})
        st.plotly_chart(fig, use_container_width=True)

    with drawdown_tab:
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=view_df["日期"],
                y=view_df["回撤(%)"],
                mode="lines",
                fill="tozeroy",
                name="回撤",
                line={"color": "rgb(22,101,52)", "width": 1.8},
            )
        )
        max_drawdown_date_text = pd.Timestamp(view_max_drawdown_date).strftime("%Y-%m-%d")
        fig.add_vline(
            x=max_drawdown_date_text,
            line_dash="dot",
            line_color="rgba(190,18,60,.75)",
        )
        fig.add_annotation(
            x=max_drawdown_date_text,
            y=view_max_drawdown,
            text=f"区间最大回撤 {view_max_drawdown:.2f}%",
            showarrow=True,
            arrowhead=2,
            ax=36,
            ay=28,
        )
        apply_plotly_layout(fig, height=420, showlegend=False)
        fig.update_layout(yaxis_title="回撤(%)")
        st.plotly_chart(fig, use_container_width=True)

    with summary_tab:
        render_detail_summary(index_name, detail, view_df, range_label, current_drawdown, max_drawdown)

    with table_tab:
        display_df = view_df[
            ["日期", "收盘价", "MA5", "MA20", "MA60", "MA120", "MA250", "偏离率(%)", "日涨跌幅(%)", "RSI(14)", "回撤(%)"]
        ].copy()
        display_df["日期"] = display_df["日期"].dt.strftime("%Y-%m-%d")
        st.dataframe(display_df, use_container_width=True, hide_index=True)


def centered_table(df: pd.DataFrame) -> None:
    headers = "".join(f"<th>{html.escape(str(col))}</th>" for col in df.columns)
    rows = []
    for _, row in df.iterrows():
        cells = "".join(f"<td>{html.escape(format_number(row[col]))}</td>" for col in df.columns)
        rows.append(f"<tr>{cells}</tr>")
    st.markdown(
        f"""
        <style>
        .centered-summary-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.92rem;
        }}
        .centered-summary-table th,
        .centered-summary-table td {{
            text-align: center;
            padding: 0.45rem 0.6rem;
            border-bottom: 1px solid rgba(49, 51, 63, 0.12);
            white-space: nowrap;
        }}
        .centered-summary-table th {{
            font-weight: 600;
            background: rgba(49, 51, 63, 0.04);
        }}
        </style>
        <table class="centered-summary-table">
            <thead><tr>{headers}</tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
        """,
        unsafe_allow_html=True,
    )


def render_index_cards(summary_df: pd.DataFrame) -> None:
    rows = list(summary_df.iterrows())
    for start in range(0, len(rows), 4):
        columns = st.columns(4)
        for col, (_, row) in zip(columns, rows[start : start + 4]):
            with col:
                render_index_card(row)


def render_index_card(row: pd.Series) -> None:
    delta = pd.to_numeric(row["当日涨跌幅(%)"], errors="coerce")
    delta_class = "positive" if not pd.isna(delta) and delta >= 0 else "negative"
    arrow = "↑" if not pd.isna(delta) and delta >= 0 else "↓"
    index_name = str(row["指数"])
    display_name = display_index_name(index_name)
    detail_href = f"?index_detail={quote(index_name)}"
    realtime_source = str(row.get("实时来源", "缓存"))
    realtime_time = pd.to_datetime(row.get("实时时间"), errors="coerce")
    is_realtime = realtime_source != "缓存" and not pd.isna(realtime_time)
    realtime_class = "live" if is_realtime else "cached"
    delta_text = "-" if pd.isna(delta) else f"{float(delta):+.2f}%"
    card_html = (
        "<style>"
        "@keyframes index-quote-flash{0%{background:rgba(219,234,254,.88)}100%{background:rgba(255,255,255,.78)}}"
        ".index-card-single{min-height:12.25rem;border:1px solid rgba(49,51,63,.14);border-radius:8px;background:rgba(255,255,255,.78);padding:1.35rem 1.5rem;box-shadow:0 10px 26px rgba(15,23,42,.06);margin:.35rem 0 .75rem;}"
        ".index-card-single.live{animation:index-quote-flash .8s ease-out;}"
        ".index-card-single:hover{border-color:rgba(37,99,235,.42);box-shadow:0 14px 30px rgba(15,23,42,.11);}"
        ".index-card-title{min-height:2.4rem;font-size:1.18rem;font-weight:700;line-height:1.25;overflow-wrap:anywhere;}"
        ".index-card-title a{color:rgba(49,51,63,.72);text-decoration:none;}"
        ".index-card-title a:hover{color:rgb(37,99,235);text-decoration:none;}"
        ".index-card-code{min-height:1.65rem;margin-top:.22rem;color:rgba(49,51,63,.58);font-size:1rem;line-height:1.25;overflow-wrap:anywhere;}"
        ".index-card-value{margin-top:1.05rem;color:rgb(31,41,55);font-size:1.85rem;font-weight:650;line-height:1.08;font-variant-numeric:tabular-nums;white-space:normal;overflow-wrap:anywhere;}"
        ".index-card-delta{display:inline-flex;align-items:center;justify-content:center;gap:.35rem;margin-top:1.05rem;border-radius:999px;padding:.35rem .85rem;font-size:1.05rem;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap;}"
        ".index-card-delta.positive{color:rgb(190,18,60);background:rgba(254,226,226,.9);}"
        ".index-card-delta.negative{color:rgb(22,101,52);background:rgba(220,252,231,.9);}"
        "</style>"
        f'<div class="index-card-single {realtime_class}">'
        f'<div class="index-card-title"><a href="{detail_href}">{html.escape(display_name)}</a></div>'
        f'<div class="index-card-code">{html.escape(str(row["代码"]))}</div>'
        f'<div class="index-card-value">{float(row["收盘价"]):.2f}</div>'
        f'<div class="index-card-delta {delta_class}">'
        f"<span>{arrow}</span>"
        f"<span>{delta_text}</span>"
        "</div>"
        "</div>"
    )
    st.markdown(card_html, unsafe_allow_html=True)


def render_manual_index_cards(summary_df: pd.DataFrame) -> None:
    stored_quotes = dict(st.session_state.get("index_realtime_quote_cache", {}))
    display_quotes = {
        index_name: quote
        for index_name, quote in stored_quotes.items()
        if quote_is_visible_for_manual_display(index_name, quote)
    }
    realtime_summary = apply_realtime_quotes_to_summary(summary_df, display_quotes)
    render_index_cards(realtime_summary)


def build_freshness_items(summary_df: pd.DataFrame) -> list[dict]:
    market_windows = {market.name: market for market in MARKET_WINDOWS}
    items = []
    for _, row in summary_df.iterrows():
        index_name = str(row["指数"])
        market_name = INDEX_CONFIG.get(index_name, {}).get("market_group", "")
        market = market_windows.get(market_name)
        latest_date = pd.to_datetime(row["日期"], errors="coerce")
        if pd.isna(latest_date) or market is None:
            continue

        market_now = datetime.now(ZoneInfo(market.timezone))
        is_trading_day = is_market_trading_day(market, market_now)
        completed_day = latest_completed_trade_date(market, market_now)
        latest_day = latest_date.date()
        session_in_progress = (
            is_trading_day
            and market.sessions[0][0] <= market_now.time() <= market.sessions[-1][1]
        )

        if latest_day >= completed_day and session_in_progress:
            status = "交易中"
            status_class = "fresh"
        elif latest_day >= completed_day and not is_trading_day:
            status = "休市"
            status_class = "closed"
        elif latest_day >= completed_day:
            status = "已更新"
            status_class = "fresh"
        else:
            status = "待更新"
            status_class = "stale"

        items.append(
            {
                "index_name": index_name,
                "status": status,
                "status_class": status_class,
                "latest_day": latest_day,
            }
        )
    return items


def render_freshness_bar(summary_df: pd.DataFrame) -> list[str]:
    items = build_freshness_items(summary_df)
    rows = []
    for item in items:
        rows.append(
            '<div class="freshness-item">'
            f'<span class="freshness-name">{html.escape(display_index_name(item["index_name"]))}</span>'
            f'<span class="freshness-pill {item["status_class"]}">{item["status"]}</span>'
            f'<span class="freshness-date">{item["latest_day"]:%m-%d}</span>'
            "</div>"
        )

    if not rows:
        return []

    st.markdown(
        "<style>"
        ".freshness-strip{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:.45rem .75rem;padding:.1rem 0 .8rem;margin-top:-.15rem;}"
        ".freshness-item{display:flex;align-items:center;gap:.35rem;min-width:0;border-bottom:1px solid rgba(49,51,63,.14);padding:.25rem .05rem .35rem;font-size:.86rem;}"
        ".freshness-name{min-width:0;font-weight:600;color:rgba(49,51,63,.82);line-height:1.25;overflow-wrap:anywhere;}"
        ".freshness-date{color:rgba(49,51,63,.58);font-variant-numeric:tabular-nums;}"
        ".freshness-pill{flex:0 0 auto;border-radius:999px;padding:.12rem .45rem;font-size:.76rem;font-weight:700;line-height:1.25;}"
        ".freshness-pill.fresh{color:rgb(22,101,52);background:rgba(220,252,231,.9);}"
        ".freshness-pill.closed{color:rgb(75,85,99);background:rgba(243,244,246,.95);}"
        ".freshness-pill.stale{color:rgb(146,64,14);background:rgba(254,243,199,.95);}"
        "@media (max-width:1200px){.freshness-strip{grid-template-columns:repeat(4,minmax(0,1fr));}}"
        "@media (max-width:820px){.freshness-strip{grid-template-columns:repeat(2,minmax(0,1fr));}}"
        "@media (max-width:520px){.freshness-strip{grid-template-columns:1fr;}}"
        "</style>"
        f'<div class="freshness-strip">{"".join(rows)}</div>',
        unsafe_allow_html=True,
    )
    return [item["index_name"] for item in items if item["status"] == "待更新"]


def render_update_timings() -> None:
    if not st.session_state.index_update_timings:
        return
    timing_df = pd.DataFrame(st.session_state.index_update_timings)
    if timing_df.empty or "耗时(秒)" not in timing_df.columns:
        return
    timing_df = timing_df.sort_values("耗时(秒)", ascending=False).reset_index(drop=True)
    with st.expander("上次指数更新耗时", expanded=False):
        st.dataframe(timing_df, use_container_width=True, hide_index=True)


def run_single_index_update(index_name: str, api_key: str, days: int) -> None:
    with st.spinner(f"正在更新 {index_name}..."):
        result = run_index_ma20_update(
            api_key=api_key,
            days=int(days),
            cache_source="auto",
            use_fresh_cache=False,
            index_names=[index_name],
            max_workers=1,
        )
    st.session_state.index_update_timings = result.timings
    if result.status == "success":
        if result.errors:
            st.session_state.index_update_notice = ("warning", result.message)
        else:
            st.session_state.index_update_notice = ("success", result.message)
        st.rerun()
    else:
        st.error(result.message)


def render_single_index_update(stale_indexes: list[str], api_key: str, days: int) -> None:
    if not stale_indexes:
        return
    update_col, button_col = st.columns([3, 1])
    selected_index = update_col.selectbox(
        "只更新单个待更新指数",
        options=stale_indexes,
        key="single_index_update_target",
        label_visibility="collapsed",
    )
    if button_col.button("只更新选中指数", use_container_width=True):
        run_single_index_update(selected_index, api_key, days)


with st.sidebar:
    st.subheader("更新设置")
    api_key = st.text_input(
        "API Key",
        value=os.getenv("TICKFLOW_API_KEY", ""),
        type="password",
        placeholder="可选；留空使用免费历史数据或环境变量",
    )
    days = st.number_input("展示最近天数", min_value=10, max_value=365, value=30, step=5)
    update_clicked = st.button("更新指数数据", type="primary")

notice = st.session_state.pop("index_update_notice", None)
if notice:
    level, message = notice
    getattr(st, level)(message)

if update_clicked:
    completed_lunch_keys = set(st.session_state.get("index_lunch_quote_keys", []))
    quote_names, lunch_keys = manual_quote_request_names(completed_lunch_keys)
    quotes = fetch_realtime_index_quotes(
        max_workers=INDEX_UPDATE_WORKERS,
        force_index_names=quote_names,
    )
    if quotes:
        stored_quotes = dict(st.session_state.get("index_realtime_quote_cache", {}))
        stored_quotes.update(quotes)
        st.session_state.index_realtime_quote_cache = stored_quotes
        completed_lunch_keys.update(
            key for index_name, key in lunch_keys.items() if index_name in quotes
        )
        st.session_state.index_lunch_quote_keys = sorted(completed_lunch_keys)
        resolved_contracts = fetch_futures_main_contract_names(quotes)
        if resolved_contracts:
            st.session_state.index_futures_main_contracts = save_futures_main_contract_names(
                resolved_contracts
            )

    pending_indexes = find_pending_post_close_index_names()
    result = None
    if pending_indexes:
        progress = st.progress(0)
        status_box = st.empty()

        def show_progress(index_name: str, idx: int, total: int, status: str, elapsed_seconds: float | None = None) -> None:
            elapsed_text = "" if elapsed_seconds is None else f"，耗时 {elapsed_seconds:.2f} 秒"
            if status == "success":
                progress.progress(idx / total)
                status_box.success(f"{index_name} 获取完成{elapsed_text}，进度 {idx}/{total}")
            elif status == "stale":
                progress.progress(idx / total)
                status_box.info(f"{index_name} 已保存最新可得数据{elapsed_text}，进度 {idx}/{total}")
            elif status == "cached":
                progress.progress(idx / total)
                status_box.info(f"{index_name} 联网失败，已沿用缓存{elapsed_text}，进度 {idx}/{total}")
            elif status == "empty":
                progress.progress(idx / total)
                status_box.warning(f"{index_name} 无数据{elapsed_text}，进度 {idx}/{total}")
            else:
                progress.progress(idx / total)
                status_box.warning(f"{index_name} 获取失败{elapsed_text}，进度 {idx}/{total}")

        result = run_index_ma20_update(
            api_key=api_key,
            days=int(days),
            cache_source="auto",
            use_fresh_cache=False,
            progress_callback=show_progress,
            index_names=pending_indexes,
            max_workers=INDEX_UPDATE_WORKERS,
        )
        st.session_state.index_update_timings = result.timings

    message_parts = []
    if quotes:
        message_parts.append(f"已手动更新 {len(quotes)} 个盘中/午间报价")
    if result is not None:
        message_parts.append(result.message)
    if not message_parts:
        message_parts.append("当前没有需要联网更新的指数数据")
    level = "warning" if result is not None and (result.status != "success" or result.errors) else "success"
    st.session_state.index_update_notice = (level, "；".join(message_parts))
    st.rerun()

report_df = None
report_meta = None
for source in ("auto", "manual"):
    report_df, meta = load_dataset(
        "index_ma20_latest",
        source,
        "index_ma20_report",
    )
    if report_df is not None:
        report_meta = meta
        break

if report_df is not None and report_meta is not None:
    st.caption(f"更新时间：{format_update_time(report_meta['last_update_time'])}")

if report_df is not None:
    report_df = sanitize_index_report_market_dates(report_df)
    summary_df = build_summary(report_df)
    if not summary_df.empty:
        selected_index = get_query_index_detail()
        summary_date = summary_df["日期"].max()
        st.subheader(f"最新摘要 · {summary_date}")
        stale_indexes = render_freshness_bar(summary_df)
        pending_indexes = find_pending_post_close_index_names()
        render_single_index_update(
            [index_name for index_name in stale_indexes if index_name in pending_indexes],
            api_key,
            int(days),
        )
        render_update_timings()
        if selected_index:
            render_index_detail(report_df, selected_index)
        render_manual_index_cards(summary_df)

        display_summary_df = summary_df.drop(columns=["代码", "日期", "前收盘价"], errors="ignore")
        if "指数" in display_summary_df.columns:
            display_summary_df["指数"] = display_summary_df["指数"].map(display_index_name)
        if "偏离率(%)" in display_summary_df.columns:
            display_summary_df = display_summary_df.assign(
                _sort_deviation=pd.to_numeric(display_summary_df["偏离率(%)"], errors="coerce")
            ).sort_values("_sort_deviation", ascending=False, na_position="last")
            display_summary_df = display_summary_df.drop(columns=["_sort_deviation"])
        centered_table(display_summary_df)

    with st.expander("查看完整分列数据", expanded=False):
        st.dataframe(report_df, use_container_width=True, hide_index=True)
else:
    st.info("还没有缓存数据。可以先点击左侧按钮联网更新，或上传已有 CSV。")
