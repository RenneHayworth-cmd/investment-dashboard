from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from core.ui import DEFAULT_CHART_HEIGHT, apply_plotly_layout, render_metric_grid
from services import position_analysis as position


def _money(value: object) -> str:
    parsed = pd.to_numeric(value, errors="coerce")
    return "-" if pd.isna(parsed) else f"{float(parsed):,.2f}元"


def render_position_timing_performance(
    formal_items: list[position.PositionItem],
) -> None:
    st.subheader("50万元ETF均线策略每日盈亏")
    st.caption("以下持仓、成交和盈亏均来自固定50万元均线策略模拟，不读取“实盘记录”。")
    result = position.build_position_timing_performance(formal_items)
    for warning in result.warnings:
        st.warning(warning)
    if result.errors:
        st.warning("；".join(result.errors))
        return
    if result.daily.empty:
        st.info("暂无完整正式日线可用于生成策略盈亏。")
        return

    summary = result.summary
    render_metric_grid(
        [
            ("最新估值日期", str(summary["最新估值日期"]), "仅使用正式前复权收盘日线"),
            ("策略资产", _money(summary["策略资产"]), "包含ETF持仓市值与现金"),
            ("累计盈亏", _money(summary["累计盈亏"]), "相对50万元初始资金"),
            (
                "累计收益率",
                f"{float(summary['累计收益率(%)']):.2f}%",
                "累计盈亏除以50万元初始资金",
            ),
            ("当前净值", f"{float(summary['当前净值']):.4f}", "2026-08-05净值设为1"),
        ]
    )

    daily = result.daily.copy()
    chart_dates = pd.to_datetime(daily["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    bar_colors = [
        "#dc2626" if value > 0 else "#16a34a" if value < 0 else "#9ca3af"
        for value in daily["每日盈亏"]
    ]
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Bar(
            x=chart_dates,
            y=daily["每日盈亏"],
            name="每日盈亏",
            marker_color=bar_colors,
            customdata=daily[["每日收益率(%)", "累计盈亏", "累计收益率(%)"]],
            hovertemplate=(
                "每日盈亏：%{y:,.2f}元"
                "<br>每日收益率：%{customdata[0]:.2f}%"
                "<br>累计盈亏：%{customdata[1]:,.2f}元"
                "<br>累计收益率：%{customdata[2]:.2f}%<extra></extra>"
            ),
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=chart_dates,
            y=daily["净值"],
            mode="lines+markers",
            name="净值",
            line={"color": "#2563eb", "width": 2.4},
            marker={"size": 5},
            customdata=daily[["每日收益率(%)", "累计收益率(%)"]],
            hovertemplate=(
                "净值：%{y:.4f}"
                "<br>每日收益率：%{customdata[0]:.2f}%"
                "<br>累计收益率：%{customdata[1]:.2f}%<extra></extra>"
            ),
        ),
        secondary_y=True,
    )
    apply_plotly_layout(figure, height=DEFAULT_CHART_HEIGHT)
    figure.update_xaxes(
        title_text="交易日",
        type="category",
        categoryorder="array",
        categoryarray=chart_dates.tolist(),
    )
    figure.update_yaxes(title_text="每日盈亏（元）", tickformat=",.0f", secondary_y=False)
    figure.update_yaxes(title_text="净值", tickformat=".4f", secondary_y=True)
    st.plotly_chart(
        figure,
        width="stretch",
        config={"displayModeBar": False},
        key="position_timing_performance_chart",
    )
    st.caption("横坐标仅排列正式日线中的实际交易日，周末和节假日已自动跳过。")

    holding_tab, trade_tab, daily_tab = st.tabs(
        ["策略持仓情况", "策略交易明细", "每日盈亏明细"]
    )
    with holding_tab:
        positions = result.positions.copy()
        if positions.empty:
            st.info("当前策略没有ETF持仓，资金全部或部分保持现金。")
        else:
            st.dataframe(
                positions,
                hide_index=True,
                width="stretch",
                column_config={
                    "持仓数量": st.column_config.NumberColumn(format="%d"),
                    "成本价": st.column_config.NumberColumn(format="%.4f"),
                    "最新价": st.column_config.NumberColumn(format="%.4f"),
                    "持仓市值": st.column_config.NumberColumn(format="%.2f 元"),
                    "浮动盈亏": st.column_config.NumberColumn(format="%.2f 元"),
                    "账户权重(%)": st.column_config.NumberColumn(format="%.2f%%"),
                    "累计手续费": st.column_config.NumberColumn(format="%.2f 元"),
                },
            )
        st.caption(
            f"当前持仓市值：{float(summary['当前持仓市值']):,.2f}元｜"
            f"现金：{float(summary['当前现金']):,.2f}元｜"
            f"仓位比例：{float(summary['当前仓位比例(%)']):.2f}%｜"
            f"策略资产：{float(summary['策略资产']):,.2f}元。"
        )
        if not result.components.empty:
            with st.expander("查看各策略袖套状态", expanded=False):
                st.dataframe(result.components, hide_index=True, width="stretch")

    with trade_tab:
        if result.trades.empty:
            st.info("策略区间内暂无模拟成交。")
        else:
            trades = result.trades.copy().reset_index(drop=True)
            trades["_顺序"] = trades.index
            trades["日期"] = pd.to_datetime(trades["日期"], errors="coerce")
            trades = trades.sort_values(["日期", "_顺序"], ascending=False)
            trades["日期"] = trades["日期"].dt.strftime("%Y-%m-%d")
            trades = trades.rename(
                columns={"标的名称": "来源策略名称", "代码": "来源袖套"}
            ).drop(columns="_顺序")
            st.dataframe(
                trades,
                hide_index=True,
                width="stretch",
                column_config={
                    "配置比例(%)": st.column_config.NumberColumn(format="%.2f%%"),
                    "成交价": st.column_config.NumberColumn(format="%.4f"),
                    "份额": st.column_config.NumberColumn(format="%d"),
                    "成交金额": st.column_config.NumberColumn(format="%.2f 元"),
                    "手续费": st.column_config.NumberColumn(format="%.2f 元"),
                    "本次交易盈亏金额": st.column_config.NumberColumn(format="%.2f 元"),
                    "本次交易盈亏率(%)": st.column_config.NumberColumn(format="%.2f%%"),
                    "现金余额": st.column_config.NumberColumn(format="%.2f 元"),
                },
            )
            st.caption("交易明细按日期倒序展示；现金余额为对应独立策略袖套的成交后余额。")

    with daily_tab:
        display = daily.copy()
        display["日期"] = pd.to_datetime(display["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
        st.dataframe(
            display,
            hide_index=True,
            width="stretch",
            column_config={
                "每日盈亏": st.column_config.NumberColumn(format="%.2f 元"),
                "每日收益率(%)": st.column_config.NumberColumn(format="%.2f%%"),
                "累计盈亏": st.column_config.NumberColumn(format="%.2f 元"),
                "累计收益率(%)": st.column_config.NumberColumn(format="%.2f%%"),
                "净值": st.column_config.NumberColumn(format="%.4f"),
                "账户资产": st.column_config.NumberColumn(format="%.2f 元"),
                "持仓市值": st.column_config.NumberColumn(format="%.2f 元"),
                "现金": st.column_config.NumberColumn(format="%.2f 元"),
            },
        )

    st.caption(
        f"初始手续费：{float(summary['初始手续费']):,.2f}元（单列，不计入8月5日盈亏）；"
        f"后续交易费用：{float(summary['后续交易费用']):,.2f}元（已计入盈亏）；"
        f"正式数据截止日：{summary['正式数据截止日']}。"
    )
    st.caption(
        "2026-08-05仅对当日出现“买入”的标的建仓；当日为“持有”的标的先等待一次空仓，"
        "当日为空仓的标的等待下一次买入。512890初始不建仓，只有510500、159967或159552"
        "真实建仓后再次卖出，才由对应袖套同日转入承接。全部计算仅使用正式前复权收盘价，"
        "盘中、午间和尾盘预览均不参与。"
    )


__all__ = ["render_position_timing_performance"]
