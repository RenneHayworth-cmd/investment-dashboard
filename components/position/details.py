"""持仓 ETF、期货、价差与期权详情视图。"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from components.position.cards_tables import metric_row, render_summary_table
from components.position.formatting import (
    clear_position_detail,
    display_position_code,
    filter_range,
    format_metric_for_item,
    format_number,
    format_pct,
    position_key,
    rolling_annual_label,
    round_numeric_columns,
)
from core.ui import (
    DEFAULT_CHART_HEIGHT,
    LARGE_CHART_HEIGHT,
    SECONDARY_CHART_HEIGHT,
    apply_plotly_layout,
    render_metric_grid,
)
from services import fund_analysis as fund
from services import position_analysis as position


def render_etf_detail(item: position.PositionItem) -> None:
    df = item.dataframe.copy()
    if df.empty:
        st.info(item.error or "当前没有可展示的 ETF 数据。")
        return
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "price"]).sort_values("date")
    latest = df.iloc[-1]
    metric_row(
        [
            ("最新价", item.metrics.get("最新价"), "", 3),
            ("日涨跌", item.metrics.get("日涨跌(%)"), "%", 2),
            ("20日涨跌", item.metrics.get("20日涨跌(%)"), "%", 2),
            ("60日涨跌", item.metrics.get("60日涨跌(%)"), "%", 2),
            ("MA20偏离", item.metrics.get("MA20偏离(%)"), "%", 2),
            ("价格百分位", item.metrics.get("价格百分位"), "", 2),
        ]
    )

    range_label = st.segmented_control(
        "走势区间",
        options=["近一年", "今年来", "近3年", "近5年", "成立来"],
        default="近一年",
        key=f"position_range_{position_key(item)}",
    )
    view_df = filter_range(df, "date", range_label)
    if view_df.empty:
        view_df = df

    trend_tab, drawdown_tab, summary_tab, table_tab = st.tabs(
        ["走势", "回撤", "摘要", "数据"]
    )
    with trend_tab:
        rsi_col = next(
            (col for col in df.columns if col.startswith("rsi_")), "rsi_14"
        )
        rolling_label = rolling_annual_label(df)
        fig = make_subplots(
            rows=4,
            cols=1,
            shared_xaxes=True,
            row_heights=[0.50, 0.18, 0.16, 0.16],
            vertical_spacing=0.04,
            subplot_titles=("走势与均线", "RSI", "20日涨幅", rolling_label),
        )
        fig.add_trace(
            go.Scatter(
                x=view_df["date"],
                y=view_df["price"],
                mode="lines",
                name="价格",
                line={"width": 2},
            ),
            row=1,
            col=1,
        )
        ma_colors = {
            20: "#eab308",
            60: "#2563eb",
            120: "#dc2626",
            250: "#059669",
        }
        for period, color in ma_colors.items():
            ma_col = f"ma_{period}"
            if ma_col in view_df.columns:
                fig.add_trace(
                    go.Scatter(
                        x=view_df["date"],
                        y=view_df[ma_col],
                        mode="lines",
                        name=f"MA{period}",
                        line={"width": 1.3, "color": color},
                    ),
                    row=1,
                    col=1,
                )
        if rsi_col in view_df.columns:
            fig.add_trace(
                go.Scatter(
                    x=view_df["date"],
                    y=view_df[rsi_col],
                    mode="lines",
                    name="RSI",
                ),
                row=2,
                col=1,
            )
            fig.add_hline(y=70, line_dash="dash", line_color="#dc2626", row=2, col=1)
            fig.add_hline(y=30, line_dash="dash", line_color="#059669", row=2, col=1)
        if "return_20d_pct" in view_df.columns:
            fig.add_trace(
                go.Scatter(
                    x=view_df["date"],
                    y=view_df["return_20d_pct"],
                    mode="lines",
                    name="20日涨幅(%)",
                ),
                row=3,
                col=1,
            )
            fig.add_hline(y=0, line_color="#6b7280", row=3, col=1)
        if "rolling_annual_return_pct" in view_df.columns:
            fig.add_trace(
                go.Scatter(
                    x=view_df["date"],
                    y=view_df["rolling_annual_return_pct"],
                    mode="lines",
                    name=rolling_label,
                    line={"color": "#7c3aed"},
                ),
                row=4,
                col=1,
            )
            fig.add_hline(y=0, line_color="#6b7280", row=4, col=1)
        apply_plotly_layout(fig, height=LARGE_CHART_HEIGHT)
        fig.update_xaxes(hoverformat="%Y-%m-%d")
        fig.update_xaxes(
            rangeslider={"visible": True, "thickness": 0.06}, row=4, col=1
        )
        fig.update_layout(
            yaxis={
                "type": (
                    "log"
                    if view_df["price"].min() > 0 and len(df) >= 252 * 3
                    else "linear"
                ),
                "title": "价格",
            }
        )
        fig.update_yaxes(title_text="RSI", row=2, col=1, range=[0, 100])
        fig.update_yaxes(title_text="涨幅%", row=3, col=1)
        fig.update_yaxes(title_text="年化%", row=4, col=1)
        st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})

    with drawdown_tab:
        if "drawdown_pct" not in df.columns:
            st.info("当前数据没有回撤字段。")
        else:
            max_drawdown_info = fund.calculate_max_drawdown_info(df)
            current_drawdown_info = fund.calculate_current_drawdown_info(df)
            current_status = str(current_drawdown_info.get("当前修复状态") or "-")
            render_metric_grid(
                [
                    (
                        "最大回撤",
                        format_pct(max_drawdown_info.get("回撤深度(%)")),
                        "历史最大回撤",
                    ),
                    (
                        "谷底日期",
                        str(max_drawdown_info.get("谷底日期") or "-"),
                        f"峰值日期：{max_drawdown_info.get('峰值日期', '-')}",
                    ),
                    (
                        "当前回撤",
                        format_pct(current_drawdown_info.get("当前回撤(%)")),
                        "最新交易日相对历史高点的回撤",
                    ),
                    (
                        "当前回撤时间",
                        format_number(
                            current_drawdown_info.get("当前回撤时间"),
                            digits=0,
                            suffix="天",
                        ),
                        "从当前回撤峰值日至最新交易日",
                    ),
                    (
                        "修复状态",
                        current_status,
                        f"当前回撤峰值日：{current_drawdown_info.get('当前回撤峰值日', '-')}；当前谷底日：{current_drawdown_info.get('当前谷底日', '-')}",
                    ),
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
                go.Scatter(
                    x=df["date"],
                    y=df["price"],
                    mode="lines",
                    name="价格",
                    line={"color": "#2563eb"},
                ),
                row=1,
                col=1,
            )
            if "running_peak" in df.columns:
                dd_fig.add_trace(
                    go.Scatter(
                        x=df["date"],
                        y=df["running_peak"],
                        mode="lines",
                        name="历史峰值",
                        line={"color": "#6b7280", "dash": "dash"},
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
                        line={"color": "rgba(255,255,255,0)"},
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
                    line={"color": "#dc2626"},
                ),
                row=2,
                col=1,
            )
            dd_fig.add_hline(y=0, line_color="#6b7280", row=2, col=1)
            trough_date = max_drawdown_info.get("谷底日期")
            if trough_date:
                trough_ts = pd.Timestamp(trough_date)
                trough_row = df[df["date"] == trough_ts]
                if not trough_row.empty:
                    dd_fig.add_trace(
                        go.Scatter(
                            x=[trough_row.iloc[0]["date"]],
                            y=[trough_row.iloc[0]["drawdown_pct"]],
                            mode="markers",
                            name="最大回撤谷底",
                            marker={"color": "#16a34a", "size": 10},
                        ),
                        row=2,
                        col=1,
                    )
            apply_plotly_layout(dd_fig, height=SECONDARY_CHART_HEIGHT)
            dd_fig.update_xaxes(hoverformat="%Y-%m-%d")
            dd_fig.update_yaxes(
                title_text=(
                    "价格（对数）"
                    if df["price"].min() > 0 and len(df) >= 252 * 3
                    else "价格"
                ),
                type=(
                    "log"
                    if df["price"].min() > 0 and len(df) >= 252 * 3
                    else "linear"
                ),
                row=1,
                col=1,
            )
            dd_fig.update_yaxes(title_text="回撤%", row=2, col=1)
            st.plotly_chart(dd_fig, use_container_width=True)

            drawdown_periods = fund.extract_drawdown_periods(df)
            yearly_drawdowns = fund.calculate_yearly_drawdowns(df)
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
        summary_rows = [
            ("类别", item.category),
            ("代码", display_position_code(item)),
            ("名称", item.name),
            ("最新日期", item.latest_date),
            ("数据来源", item.source),
            ("缓存时间", item.cache_time or "-"),
            ("最新价", format_metric_for_item(item, "最新价")),
            ("日涨跌(%)", format_number(item.metrics.get("日涨跌(%)"))),
            ("20日涨跌(%)", format_number(item.metrics.get("20日涨跌(%)"))),
            ("年化波动(%)", format_number(item.metrics.get("年化波动(%)"))),
            (
                rolling_annual_label(df),
                format_number(latest.get("rolling_annual_return_pct"), suffix="%"),
            ),
            ("当前区间", range_label),
            ("区间样本", len(view_df)),
        ]
        render_summary_table(summary_rows)

    with table_tab:
        display_cols = [
            col
            for col in [
                "date",
                "price",
                "daily_return_pct",
                "return_20d_pct",
                "return_60d_pct",
                "rolling_annual_return_pct",
                "ma_20",
                "ma_60",
                "ma_120",
                "ma_250",
                "drawdown_pct",
            ]
            if col in view_df.columns
        ]
        rsi_col = next(
            (col for col in view_df.columns if col.startswith("rsi_")), None
        )
        if rsi_col:
            display_cols.insert(4, rsi_col)
        table_df = view_df[display_cols].sort_values("date", ascending=False).copy()
        table_df["date"] = table_df["date"].dt.strftime("%Y-%m-%d")
        st.dataframe(table_df, use_container_width=True, hide_index=True)


def render_spread_detail(item: position.PositionItem) -> None:
    df = item.dataframe.copy()
    if df.empty:
        st.info(item.error or "当前没有可展示的价差数据。")
        return
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    spread_cols = [
        col
        for col in df.columns
        if col.startswith("spread_") and not col.endswith("_pct")
    ]
    if not spread_cols:
        st.info("没有可绘制的价差列。")
        return
    spread_col = spread_cols[0]
    metric_row(
        [
            ("最新价差", item.metrics.get("最新价差"), "", 1),
            ("价差日变化", item.metrics.get("价差日变化"), "", 1),
            ("最新占比", item.metrics.get("最新占比(%)"), "%", 1),
            ("平均价差", item.metrics.get("平均价差"), "", 1),
        ]
    )
    range_label = st.segmented_control(
        "走势区间",
        options=["近一年", "今年来", "近3年", "近5年", "成立来"],
        default="近一年",
        key=f"position_range_{position_key(item)}",
    )
    view_df = filter_range(df, "date", range_label)
    if view_df.empty:
        view_df = df

    spread_tab, price_tab, summary_tab, table_tab = st.tabs(
        ["价差", "合约价格", "摘要", "数据"]
    )
    with spread_tab:
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=view_df["date"],
                y=view_df[spread_col],
                mode="lines",
                name=item.code,
                line={"width": 2},
            )
        )
        apply_plotly_layout(fig, height=DEFAULT_CHART_HEIGHT)
        fig.update_layout(yaxis_title="价差")
        st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})

    with price_tab:
        close_cols = [col for col in view_df.columns if col.endswith("_close")]
        fig = go.Figure()
        for col in close_cols:
            fig.add_trace(
                go.Scatter(
                    x=view_df["date"],
                    y=view_df[col],
                    mode="lines",
                    name=col.replace("_close", ""),
                    line={"width": 1.7},
                )
            )
        apply_plotly_layout(fig, height=DEFAULT_CHART_HEIGHT)
        fig.update_layout(yaxis_title="收盘价")
        st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})

    with summary_tab:
        summary_rows = [
            ("类别", item.category),
            ("价差对", item.code),
            ("名称", item.name),
            ("最新日期", item.latest_date),
            ("数据来源", item.source),
            ("缓存时间", item.cache_time or "-"),
            ("最新价差", format_metric_for_item(item, "最新价差")),
            ("价差日变化", format_metric_for_item(item, "价差日变化")),
            ("最新占比(%)", format_metric_for_item(item, "最新占比(%)")),
            ("平均价差", format_metric_for_item(item, "平均价差")),
            ("最大价差", format_metric_for_item(item, "最大价差")),
            ("最小价差", format_metric_for_item(item, "最小价差")),
        ]
        render_summary_table(summary_rows)

    with table_tab:
        table_df = view_df.drop(
            columns=[col for col in view_df.columns if col.startswith("_")],
            errors="ignore",
        ).sort_values("date", ascending=False)
        table_df = round_numeric_columns(table_df, digits=1)
        table_df["date"] = table_df["date"].dt.strftime("%Y-%m-%d")
        st.dataframe(table_df, use_container_width=True, hide_index=True)


def render_option_detail(
    item: position.PositionItem,
    instrument_label: str = "期权",
) -> None:
    df = item.dataframe.copy()
    if df.empty:
        st.info(item.error or f"当前没有可展示的{instrument_label}数据。")
        return
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date")
    metric_row(
        [
            ("最新收盘", item.metrics.get("最新收盘"), "", 1),
            ("日涨跌", item.metrics.get("日涨跌(%)"), "%", 1),
            ("20日涨跌", item.metrics.get("20日涨跌(%)"), "%", 1),
            ("20日波动", item.metrics.get("20日波动(%)"), "%", 1),
            ("成交量", item.metrics.get("最新成交量"), "", 0),
            ("持仓量", item.metrics.get("最新持仓量"), "", 0),
        ]
    )
    range_label = st.segmented_control(
        "走势区间",
        options=["近一年", "今年来", "近3年", "近5年", "成立来"],
        default="近一年",
        key=f"position_range_{position_key(item)}",
    )
    view_df = filter_range(df, "date", range_label)
    if view_df.empty:
        view_df = df

    trend_tab, activity_tab, summary_tab, table_tab = st.tabs(
        ["走势", "成交持仓", "摘要", "数据"]
    )
    with trend_tab:
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=view_df["date"],
                y=view_df["close"],
                mode="lines",
                name="收盘价",
                line={"width": 2},
            )
        )
        ma_colors = {
            5: "#d97706",
            20: "#2563eb",
            60: "#dc2626",
            120: "#059669",
        }
        for period, color in ma_colors.items():
            ma_col = f"ma_{period}"
            if ma_col in view_df.columns:
                fig.add_trace(
                    go.Scatter(
                        x=view_df["date"],
                        y=view_df[ma_col],
                        mode="lines",
                        name=f"MA{period}",
                        line={"width": 1.25, "color": color},
                    )
                )
        apply_plotly_layout(fig, height=DEFAULT_CHART_HEIGHT)
        fig.update_layout(yaxis_title=f"{instrument_label}价格")
        st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})

    with activity_tab:
        fig = go.Figure()
        has_activity = False
        if "volume" in view_df.columns:
            has_activity = True
            fig.add_trace(
                go.Bar(
                    x=view_df["date"],
                    y=view_df["volume"],
                    name="成交量",
                    marker_color="#94a3b8",
                )
            )
        if "open_interest" in view_df.columns:
            has_activity = True
            fig.add_trace(
                go.Scatter(
                    x=view_df["date"],
                    y=view_df["open_interest"],
                    mode="lines",
                    name="持仓量",
                    line={"width": 2, "color": "#2563eb"},
                    yaxis="y2",
                )
            )
            fig.update_layout(
                yaxis2={"overlaying": "y", "side": "right", "title": "持仓量"}
            )
        if has_activity:
            apply_plotly_layout(fig, height=420)
            fig.update_layout(yaxis_title="成交量")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info(f"当前{instrument_label}数据源未提供成交量和持仓量。")

    with summary_tab:
        summary_rows = [
            ("类别", item.category),
            ("代码", item.code),
            ("名称", item.name),
            ("最新日期", item.latest_date),
            ("数据来源", item.source),
            ("缓存时间", item.cache_time or "-"),
            ("最新收盘", format_metric_for_item(item, "最新收盘")),
            ("日涨跌(%)", format_metric_for_item(item, "日涨跌(%)")),
            ("20日波动(%)", format_metric_for_item(item, "20日波动(%)")),
            ("价格百分位", format_metric_for_item(item, "价格百分位")),
            ("最新成交量", format_metric_for_item(item, "最新成交量")),
            ("最新持仓量", format_metric_for_item(item, "最新持仓量")),
        ]
        render_summary_table(summary_rows)

    with table_tab:
        table_df = view_df.drop(
            columns=[col for col in view_df.columns if col.startswith("_")],
            errors="ignore",
        ).sort_values("date", ascending=False)
        table_df = round_numeric_columns(
            table_df,
            digits=1,
            integer_columns=("volume", "open_interest"),
        )
        table_df["date"] = table_df["date"].dt.strftime("%Y-%m-%d")
        st.dataframe(table_df, use_container_width=True, hide_index=True)


def render_position_detail(item: position.PositionItem) -> None:
    st.divider()
    title_col, action_col = st.columns([5, 1])
    title_col.markdown(f"### {item.name}")
    action_col.button(
        "返回全部",
        key="clear_position_detail",
        on_click=clear_position_detail,
    )
    st.caption(
        f"{item.category} · {display_position_code(item)} · "
        f"最新日期：{item.latest_date or '-'} · 来源：{item.source or '-'}"
    )
    if item.error and item.status != "无缓存":
        st.warning(item.error)

    if item.category == "ETF":
        render_etf_detail(item)
    elif item.category == "期货价差":
        render_spread_detail(item)
    elif item.category == "期货":
        render_option_detail(item, instrument_label="期货")
    else:
        render_option_detail(item)
