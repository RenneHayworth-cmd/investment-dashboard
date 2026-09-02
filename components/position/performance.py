from __future__ import annotations

from datetime import datetime, time as datetime_time
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from components.position_table import (
    position_number_cell,
    position_pnl_cell,
    position_quantity_cell,
    position_text_cell,
    render_position_table,
)
from core.ui import (
    DEFAULT_CHART_HEIGHT,
    apply_plotly_layout,
    build_sparse_trading_date_ticks,
    filter_by_time_range,
    render_metric_grid,
)
from services import position_analysis as position


def _money(value: object) -> str:
    parsed = pd.to_numeric(value, errors="coerce")
    return "-" if pd.isna(parsed) else f"{float(parsed):,.2f}元"


def _strategy_quote_status(*, quote_time: pd.Timestamp, market_now: datetime) -> str:
    if datetime_time(11, 30) <= quote_time.time() < datetime_time(13, 0):
        return "午间"
    if (
        market_now.time() < datetime_time(15, 5)
        and quote_time.time() < datetime_time(15, 0)
    ):
        return "实时"
    return "缓存"


def _overlay_strategy_positions_with_realtime(
    positions: pd.DataFrame,
    quotes: dict[str, dict[str, object]] | None,
    *,
    formal_cash: float,
    market_now: datetime,
) -> tuple[pd.DataFrame, dict[str, object]]:
    """Overlay current quotes for display without changing the formal strategy."""
    display = positions.copy()
    display["行情状态"] = "正式收盘"
    display["行情时间"] = ""
    normalized_quotes = {
        position.normalize_etf_base_code(code): dict(quote)
        for code, quote in (quotes or {}).items()
        if isinstance(quote, dict)
    }
    realtime_count = 0
    quote_times: list[pd.Timestamp] = []
    for index, row in display.iterrows():
        code = position.normalize_etf_base_code(row["代码"])
        quote = normalized_quotes.get(code)
        if quote is None:
            continue
        quote_time = pd.to_datetime(quote.get("quote_time"), errors="coerce")
        latest_price = pd.to_numeric(quote.get("price"), errors="coerce")
        if (
            pd.isna(quote_time)
            or quote_time.date() != market_now.date()
            or pd.isna(latest_price)
            or float(latest_price) <= 0
        ):
            continue
        if quote_time.tzinfo is not None:
            quote_time = quote_time.tz_convert("Asia/Shanghai").tz_localize(None)
        quantity = int(row["持仓数量"])
        previous_close = pd.to_numeric(quote.get("previous_close"), errors="coerce")
        if pd.isna(previous_close) or float(previous_close) <= 0:
            previous_close = pd.to_numeric(row["最新价"], errors="coerce")
        cost_basis = float(row["成本价"]) * quantity
        market_value = float(latest_price) * quantity
        daily_base = (
            float(previous_close) * quantity
            if not pd.isna(previous_close) and float(previous_close) > 0
            else pd.NA
        )
        daily_pnl = (
            (float(latest_price) - float(previous_close)) * quantity
            if not pd.isna(previous_close) and float(previous_close) > 0
            else pd.NA
        )
        display.at[index, "最新价"] = float(latest_price)
        display.at[index, "持仓市值"] = market_value
        display.at[index, "当日盈亏"] = daily_pnl
        display.at[index, "当日收益基数"] = daily_base
        display.at[index, "当日收益率(%)"] = (
            float(daily_pnl) / float(daily_base) * 100
            if not pd.isna(daily_pnl) and not pd.isna(daily_base) and float(daily_base) > 0
            else pd.NA
        )
        display.at[index, "浮动盈亏"] = market_value - cost_basis
        display.at[index, "行情状态"] = _strategy_quote_status(
            quote_time=quote_time,
            market_now=market_now,
        )
        display.at[index, "行情时间"] = quote_time.strftime("%Y-%m-%d %H:%M:%S")
        realtime_count += 1
        quote_times.append(quote_time)

    market_value = float(
        pd.to_numeric(display["持仓市值"], errors="coerce").sum()
    )
    account_assets = float(formal_cash) + market_value
    display["账户权重(%)"] = (
        pd.to_numeric(display["持仓市值"], errors="coerce")
        / account_assets
        * 100
        if account_assets > 0
        else pd.NA
    )
    return display, {
        "持仓市值": market_value,
        "现金": float(formal_cash),
        "策略资产": account_assets,
        "仓位比例(%)": market_value / account_assets * 100 if account_assets > 0 else 0.0,
        "实时行情数量": realtime_count,
        "最新行情时间": max(quote_times).strftime("%Y-%m-%d %H:%M:%S") if quote_times else "",
    }


def _render_strategy_positions_table(
    positions: pd.DataFrame,
    *,
    valuation_date: object,
) -> None:
    headers = [
        "标的名称",
        "代码",
        "市值",
        "现价",
        "行情状态",
        "持仓数量",
        "成本",
        "当日盈亏",
        "浮动盈亏",
        "仓位",
        "来源袖套",
        "累计手续费",
    ]
    rows: list[list[str]] = []
    for _, row in positions.iterrows():
        cost_basis = float(row["成本价"]) * int(row["持仓数量"])
        pnl_rate = (
            float(row["浮动盈亏"]) / cost_basis * 100 if cost_basis > 0 else pd.NA
        )
        rows.append(
            [
                position_text_cell(row["基金名称"]),
                position_text_cell(row["代码"]),
                position_number_cell(row["持仓市值"]),
                position_number_cell(row["最新价"], digits=3),
                position_text_cell(
                    row.get("行情状态", "正式收盘"),
                    title=row.get("行情时间", "") or valuation_date,
                ),
                position_quantity_cell(row["持仓数量"]),
                position_number_cell(row["成本价"], digits=3),
                position_pnl_cell(row["当日盈亏"], row["当日收益率(%)"]),
                position_pnl_cell(row["浮动盈亏"], pnl_rate),
                position_number_cell(row["账户权重(%)"], suffix="%"),
                position_text_cell(row["来源袖套"]),
                position_number_cell(row["累计手续费"]),
            ]
        )
    total_market_value = pd.to_numeric(positions["持仓市值"], errors="coerce").sum()
    total_daily_pnl = pd.to_numeric(positions["当日盈亏"], errors="coerce").sum()
    total_daily_base = pd.to_numeric(
        positions["当日收益基数"], errors="coerce"
    ).sum()
    total_daily_return = (
        total_daily_pnl / total_daily_base * 100 if total_daily_base > 0 else pd.NA
    )
    total_floating_pnl = pd.to_numeric(positions["浮动盈亏"], errors="coerce").sum()
    total_cost_basis = total_market_value - total_floating_pnl
    total_pnl_rate = (
        total_floating_pnl / total_cost_basis * 100 if total_cost_basis > 0 else pd.NA
    )
    total_weight = pd.to_numeric(positions["账户权重(%)"], errors="coerce").sum()
    total_fee = pd.to_numeric(positions["累计手续费"], errors="coerce").sum()
    total_cells = [
        position_text_cell("合计", class_name="position-total-label"),
        position_text_cell("-"),
        position_number_cell(total_market_value),
        position_text_cell("-"),
        position_text_cell("-"),
        position_text_cell("-"),
        position_text_cell("-"),
        position_pnl_cell(total_daily_pnl, total_daily_return),
        position_pnl_cell(total_floating_pnl, total_pnl_rate),
        position_number_cell(total_weight, suffix="%"),
        position_text_cell("-"),
        position_number_cell(total_fee),
    ]
    render_position_table(headers, rows, total_cells=total_cells, min_width=1220)


def render_position_timing_performance(
    formal_items: list[position.PositionItem],
    *,
    realtime_quotes: dict[str, dict[str, object]] | None = None,
    market_now: datetime | None = None,
) -> None:
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    st.subheader("50万元ETF均线策略每日盈亏")
    st.caption(
        "以下持仓数量、成交和正式盈亏曲线均来自固定50万元均线策略模拟，不读取“实盘记录”；"
        "策略持仓表可叠加持仓分析的当日实时行情。"
    )
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

    period = st.segmented_control(
        "时间范围",
        ["近1月", "近3月", "近1年", "全部"],
        default="全部",
        key="position_timing_perf_period",
        label_visibility="collapsed",
    )
    daily = filter_by_time_range(result.daily.copy(), date_column="日期", period=period or "全部")
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
    tickvals, ticktext = build_sparse_trading_date_ticks(chart_dates.tolist(), max_ticks=7)
    figure.update_xaxes(
        title_text="交易日",
        type="category",
        categoryorder="array",
        categoryarray=chart_dates.tolist(),
        tickmode="array",
        tickvals=tickvals,
        ticktext=ticktext,
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
            positions, holding_summary = _overlay_strategy_positions_with_realtime(
                positions,
                realtime_quotes,
                formal_cash=float(summary["当前现金"]),
                market_now=market_now,
            )
            _render_strategy_positions_table(
                positions,
                valuation_date=summary["最新估值日期"],
            )
            realtime_count = int(holding_summary["实时行情数量"])
            if realtime_count:
                st.caption(
                    f"临时持仓市值：{float(holding_summary['持仓市值']):,.2f}元｜"
                    f"现金：{float(holding_summary['现金']):,.2f}元｜"
                    f"临时仓位比例：{float(holding_summary['仓位比例(%)']):.2f}%｜"
                    f"临时策略资产：{float(holding_summary['策略资产']):,.2f}元。"
                )
                st.caption(
                    f"已接入 {realtime_count}/{len(positions)} 个当前持仓的当日行情，"
                    f"最新行情时间：{holding_summary['最新行情时间']}；未覆盖标的回退正式收盘。"
                    "实时行情仅用于本表临时估值和当日盈亏，不改变策略信号、持仓数量、"
                    "交易明细或下方正式曲线。"
                )
            else:
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
                    "成交价": st.column_config.NumberColumn(format="%.3f"),
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
        "盘中、午间和尾盘行情仅用于上方策略持仓表临时估值，不参与正式信号、交易或曲线。"
    )


__all__ = ["render_position_timing_performance"]
