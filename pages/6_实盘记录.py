import html
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from core.db import init_db
from core.ui import (
    DEFAULT_CHART_HEIGHT,
    apply_global_style,
    apply_plotly_layout,
    render_metric_grid,
    render_page_header,
)
from services.live_trading import (
    add_live_trade,
    append_live_symbol_pnl_total,
    build_live_daily_pnl,
    build_live_position_performance,
    build_live_symbol_pnl_history,
    delete_live_trade,
    enrich_live_trades,
    live_close_refresh_due,
    list_live_trades,
    summarize_live_position_performance,
    summarize_live_trades,
)
from services.position_analysis import (
    latest_final_etf_trade_date,
    load_or_fetch_etf,
)


st.set_page_config(page_title="实盘记录", layout="wide")
init_db()
apply_global_style()

render_page_header(
    "实盘记录",
    "记录实际成交、手续费和持仓成本，与策略回测结果分开核算。",
    eyebrow="Live Trading",
)


def money(value: object) -> str:
    return f"{float(value):,.2f}"


def format_live_number(value: object, digits: int = 2, prefix: str = "") -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{prefix}{float(value):,.{digits}f}"


def render_live_positions_table(positions: pd.DataFrame) -> None:
    headers = [
        "标的名称",
        "代码",
        "市值",
        "现价",
        "持仓数量",
        "成本",
        "当日盈亏",
        "累计盈亏",
        "仓位",
        "已实现盈亏",
        "累计手续费",
    ]

    def pnl_cell(amount: object, rate: object) -> str:
        if amount is None or pd.isna(amount) or rate is None or pd.isna(rate):
            return '<td class="live-pnl-cell">-</td>'
        amount_value = float(amount)
        rate_value = float(rate)
        value_class = (
            "live-pnl-positive"
            if amount_value > 0
            else "live-pnl-negative"
            if amount_value < 0
            else ""
        )
        return (
            f'<td class="live-pnl-cell {value_class}">'
            f'<div>{html.escape(format_live_number(amount_value))}</div>'
            f'<div class="live-pnl-rate">{html.escape(format_live_number(rate_value))}%</div>'
            "</td>"
        )

    total = summarize_live_position_performance(positions)
    total_market_value = pd.to_numeric(total["market_value"], errors="coerce")
    rows: list[str] = []
    for row in positions.itertuples(index=False):
        market_value = pd.to_numeric(row.market_value, errors="coerce")
        weight_pct = (
            float(market_value) / float(total_market_value) * 100
            if not pd.isna(market_value)
            and not pd.isna(total_market_value)
            and float(total_market_value) > 0
            else pd.NA
        )
        cells = [
            f"<td>{html.escape(str(row.name))}</td>",
            f"<td>{html.escape(str(row.symbol))}</td>",
            f"<td>{html.escape(format_live_number(row.market_value))}</td>",
            f"<td>{html.escape(format_live_number(row.latest_price, 3))}</td>",
            f"<td>{int(row.quantity):,}</td>",
            f"<td>{html.escape(format_live_number(row.average_cost, 3))}</td>",
            pnl_cell(row.daily_pnl, row.daily_return_pct),
            pnl_cell(row.cumulative_pnl, row.cumulative_return_pct),
            f"<td>{html.escape(format_live_number(weight_pct))}%</td>"
            if not pd.isna(weight_pct)
            else "<td>-</td>",
            f"<td>{html.escape(format_live_number(row.realized_pnl))}</td>",
            f"<td>{html.escape(format_live_number(row.fee_amount))}</td>",
        ]
        rows.append(f"<tr>{''.join(cells)}</tr>")

    total_cells = [
        '<td class="live-total-label">合计</td>',
        "<td>-</td>",
        f"<td>{html.escape(format_live_number(total['market_value']))}</td>",
        "<td>-</td>",
        "<td>-</td>",
        "<td>-</td>",
        pnl_cell(total["daily_pnl"], total["daily_return_pct"]),
        pnl_cell(total["cumulative_pnl"], total["cumulative_return_pct"]),
        "<td>100.00%</td>"
        if not pd.isna(total_market_value) and float(total_market_value) > 0
        else "<td>-</td>",
        f"<td>{html.escape(format_live_number(total['realized_pnl']))}</td>",
        f"<td>{html.escape(format_live_number(total['fee_amount']))}</td>",
    ]
    rows.append(f'<tr class="live-position-total">{"".join(total_cells)}</tr>')

    header_html = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    st.markdown(
        f"""
        <style>
        .live-position-table-scroll {{
            width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }}
        .live-position-table {{
            width: 100%;
            min-width: 1120px;
            border-collapse: collapse;
            font-size: 0.9rem;
        }}
        .live-position-table th,
        .live-position-table td {{
            padding: 0.5rem 0.55rem;
            border-bottom: 1px solid rgba(49, 51, 63, 0.12);
            text-align: center;
            white-space: nowrap;
        }}
        .live-position-table th {{
            background: rgba(49, 51, 63, 0.04);
            font-weight: 600;
        }}
        .live-position-table .live-position-total td {{
            border-top: 2px solid rgba(49, 51, 63, 0.22);
            background: rgba(49, 51, 63, 0.035);
            font-weight: 600;
        }}
        .live-position-table .live-total-label {{
            font-weight: 700;
        }}
        .live-position-table .live-pnl-cell {{
            line-height: 1.25;
            font-variant-numeric: tabular-nums;
        }}
        .live-position-table .live-pnl-rate {{
            margin-top: 0.16rem;
            font-size: 0.82rem;
            opacity: 0.82;
        }}
        .live-position-table .live-pnl-positive {{
            color: rgb(190, 18, 60);
            font-weight: 600;
        }}
        .live-position-table .live-pnl-negative {{
            color: rgb(22, 101, 52);
            font-weight: 600;
        }}
        </style>
        <div class="live-position-table-scroll">
            <table class="live-position-table">
                <thead><tr>{header_html}</tr></thead>
                <tbody>{''.join(rows)}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_live_symbol_history_table(history: pd.DataFrame) -> None:
    date_columns = {"首次交易日", "最近交易日", "估值日期"}
    pnl_columns = {"已实现盈亏", "未实现盈亏", "累计盈亏", "累计盈亏率(%)"}
    numeric_columns = {
        "累计买入成本",
        "累计卖出回款",
        "当前市值",
        "已实现盈亏",
        "未实现盈亏",
        "累计盈亏",
        "累计盈亏率(%)",
        "累计手续费",
    }

    def cell_html(column: str, value: object) -> str:
        cell_class = ""
        if column in date_columns:
            date_value = pd.to_datetime(value, errors="coerce")
            text = "-" if pd.isna(date_value) else pd.Timestamp(date_value).strftime("%Y-%m-%d")
        elif column == "当前数量":
            number = pd.to_numeric(value, errors="coerce")
            text = "-" if pd.isna(number) else f"{int(number):,}"
        elif column in numeric_columns:
            number = pd.to_numeric(value, errors="coerce")
            text = "-" if pd.isna(number) else f"{float(number):,.2f}"
            if column in pnl_columns and not pd.isna(number):
                cell_class = (
                    "live-history-positive"
                    if float(number) > 0
                    else "live-history-negative"
                    if float(number) < 0
                    else ""
                )
        else:
            text = "-" if value is None or pd.isna(value) else str(value)
        return f'<td class="{cell_class}">{html.escape(text)}</td>'

    headers = "".join(f"<th>{html.escape(str(column))}</th>" for column in history.columns)
    rows: list[str] = []
    for _, row in history.iterrows():
        row_class = "live-symbol-history-total" if str(row.get("标的名称", "")) == "合计" else ""
        cells = "".join(cell_html(str(column), row[column]) for column in history.columns)
        rows.append(f'<tr class="{row_class}">{cells}</tr>')
    st.markdown(
        f"""
        <style>
        .live-symbol-history-scroll {{
            width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }}
        .live-symbol-history-table {{
            width: 100%;
            min-width: 1680px;
            border-collapse: collapse;
            font-size: 0.88rem;
        }}
        .live-symbol-history-table th,
        .live-symbol-history-table td {{
            padding: 0.5rem 0.55rem;
            border-bottom: 1px solid rgba(49, 51, 63, 0.12);
            text-align: center;
            white-space: nowrap;
        }}
        .live-symbol-history-table th {{
            background: rgba(49, 51, 63, 0.04);
            font-weight: 600;
        }}
        .live-symbol-history-table .live-symbol-history-total td {{
            border-top: 2px solid rgba(49, 51, 63, 0.22);
            background: rgba(49, 51, 63, 0.035);
            font-weight: 600;
        }}
        .live-symbol-history-table .live-history-positive {{
            color: rgb(190, 18, 60);
            font-weight: 600;
        }}
        .live-symbol-history-table .live-history-negative {{
            color: rgb(22, 101, 52);
            font-weight: 600;
        }}
        </style>
        <div class="live-symbol-history-scroll">
            <table class="live-symbol-history-table">
                <thead><tr>{headers}</tr></thead>
                <tbody>{''.join(rows)}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


trades = list_live_trades()
summary = summarize_live_trades(trades)
render_metric_grid(
    [
        ("成交记录", str(summary["record_count"]), "已保存的实际成交笔数"),
        ("当前标的", str(summary["position_count"]), "当前数量大于零的标的数量"),
        ("累计买入金额", money(summary["buy_amount"]), "不含手续费的累计买入金额"),
        ("累计手续费", money(summary["fee_amount"]), "全部买卖成交手续费合计"),
        ("当前净投入", money(summary["net_investment"]), "买入支出减去卖出回款"),
    ]
)

@st.fragment(run_every="120s")
def render_daily_close_pnl() -> None:
    current_trades = list_live_trades()
    if current_trades.empty:
        st.subheader("当前实盘持仓")
        st.info("暂无实盘持仓。")
        st.subheader("每日收盘盈亏")
        st.info("录入成交后，将按正式收盘价生成每日盈亏走势。")
        return

    market_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    target_date = latest_final_etf_trade_date(market_now)
    attempt_key = "live_pnl_close_last_attempt"
    attempt_target_key = "live_pnl_close_last_target"
    market_now_naive = market_now.replace(tzinfo=None)
    network_refresh_due = live_close_refresh_due(
        target_date=target_date,
        market_now=market_now,
        last_attempt=st.session_state.get(attempt_key),
        last_target_date=st.session_state.get(attempt_target_key),
    )

    price_histories: dict[str, pd.DataFrame] = {}
    update_failures: list[str] = []
    data_warnings: list[str] = []
    symbols = sorted(current_trades["symbol"].dropna().astype(str).unique())
    for symbol in symbols:
        item = load_or_fetch_etf(
            symbol,
            api_key=os.getenv("TICKFLOW_API_KEY", ""),
            count=5000,
            adjust=None,
            allow_fetch=network_refresh_due,
            force_refresh=False,
            save_to_cache=True,
            market_now=market_now,
        )
        if item.dataframe is not None and not item.dataframe.empty:
            price_histories[symbol] = item.dataframe
        item_date = pd.to_datetime(item.latest_date, errors="coerce")
        if item.error:
            detail = f"{symbol}：{item.error}"
            if network_refresh_due and item.status in {"失败", "缓存"}:
                update_failures.append(detail)
            else:
                data_warnings.append(detail)
        elif pd.isna(item_date) or item_date.date() < target_date:
            data_warnings.append(
                f"{symbol}：正式收盘数据最新到{item.latest_date or '-'}，目标为{target_date}"
            )
    if network_refresh_due:
        st.session_state[attempt_key] = market_now_naive.isoformat()
        st.session_state[attempt_target_key] = str(target_date)

    failure_state_key = "live_pnl_close_failures"
    if network_refresh_due:
        if update_failures:
            st.session_state[failure_state_key] = {
                "target_date": str(target_date),
                "details": update_failures,
            }
        elif not data_warnings:
            st.session_state.pop(failure_state_key, None)
    persisted_failure = st.session_state.get(failure_state_key, {})
    if (
        not update_failures
        and str(persisted_failure.get("target_date", "")) == str(target_date)
    ):
        update_failures = list(persisted_failure.get("details") or [])

    if update_failures:
        st.warning(
            "收盘价更新失败，当前继续使用本地缓存；"
            "页面保持打开时将在10分钟后重试："
            + "；".join(update_failures)
        )
    if data_warnings:
        st.warning("正式收盘数据尚未完全补齐：" + "；".join(data_warnings))

    st.subheader("当前实盘持仓")
    position_performance = build_live_position_performance(
        current_trades,
        price_histories,
    )
    if position_performance.empty:
        st.info("暂无实盘持仓。")
    else:
        render_live_positions_table(position_performance)
        st.caption(
            "当日盈亏按本次与上一个正式收盘估值的累计盈亏差额计算；"
            "当日新增买入成本计入当日收益率分母。"
        )

    daily_pnl = build_live_daily_pnl(current_trades, price_histories)
    if daily_pnl.empty:
        st.subheader("每日收盘盈亏")
        st.info("尚无可用于完整估值的正式收盘数据。")
        return

    latest = daily_pnl.iloc[-1]
    valuation_date = pd.Timestamp(latest["date"]).strftime("%Y-%m-%d")
    st.subheader(f"每日收盘盈亏（{valuation_date}）")
    render_metric_grid(
        [
            ("持仓市值", money(latest["market_value"]), "按当日不复权收盘价计算"),
            ("未实现盈亏", money(latest["unrealized_pnl"]), "持仓市值减剩余成本"),
            ("已实现盈亏", money(latest["realized_pnl"]), "已扣除卖出手续费"),
            ("总盈亏", money(latest["total_pnl"]), "已实现与未实现盈亏合计"),
            ("累计收益率", f"{float(latest['return_pct']):.2f}%", "总盈亏除以累计买入成本"),
        ]
    )

    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Scatter(
            x=daily_pnl["date"],
            y=daily_pnl["total_pnl"],
            mode="lines+markers",
            name="总盈亏",
            line={"color": "#dc2626", "width": 2.4},
            marker={"size": 5},
            customdata=daily_pnl[
                ["market_value", "cost_basis", "realized_pnl", "unrealized_pnl"]
            ],
            hovertemplate=(
                "总盈亏：%{y:,.2f}<br>持仓市值：%{customdata[0]:,.2f}"
                "<br>剩余成本：%{customdata[1]:,.2f}<br>已实现盈亏：%{customdata[2]:,.2f}"
                "<br>未实现盈亏：%{customdata[3]:,.2f}<extra></extra>"
            ),
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=daily_pnl["date"],
            y=daily_pnl["return_pct"],
            mode="lines",
            name="累计收益率",
            line={"color": "#0f766e", "width": 2, "dash": "dot"},
            hovertemplate="累计收益率：%{y:.2f}%<extra></extra>",
        ),
        secondary_y=True,
    )
    figure.add_hline(y=0, line_width=1, line_color="rgba(87,83,78,0.45)")
    apply_plotly_layout(figure, height=DEFAULT_CHART_HEIGHT)
    figure.update_yaxes(title_text="盈亏金额（元）", secondary_y=False)
    figure.update_yaxes(title_text="累计收益率（%）", secondary_y=True)
    st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
    st.caption(
        "由实盘成交记录与不复权正式日线重算；买入手续费计入成本，"
        "卖出按移动平均成本确认盈亏。交易日15:05后自动检查当天收盘数据。"
    )

    with st.expander("每日盈亏明细"):
        daily_display = daily_pnl.rename(
            columns={
                "date": "日期",
                "market_value": "持仓市值",
                "cost_basis": "剩余成本",
                "realized_pnl": "已实现盈亏",
                "unrealized_pnl": "未实现盈亏",
                "total_pnl": "总盈亏",
                "cumulative_buy_cost": "累计买入成本",
                "net_investment": "净投入",
                "return_pct": "累计收益率(%)",
            }
        )
        st.dataframe(
            daily_display.sort_values("日期", ascending=False),
            width="stretch",
            hide_index=True,
            column_config={
                "日期": st.column_config.DateColumn(format="YYYY-MM-DD"),
                "持仓市值": st.column_config.NumberColumn(format="%.2f"),
                "剩余成本": st.column_config.NumberColumn(format="%.2f"),
                "已实现盈亏": st.column_config.NumberColumn(format="%.2f"),
                "未实现盈亏": st.column_config.NumberColumn(format="%.2f"),
                "总盈亏": st.column_config.NumberColumn(format="%.2f"),
                "累计买入成本": st.column_config.NumberColumn(format="%.2f"),
                "净投入": st.column_config.NumberColumn(format="%.2f"),
                "累计收益率(%)": st.column_config.NumberColumn(format="%.2f%%"),
            },
        )
render_daily_close_pnl()

st.subheader("新增成交")
with st.form("live_trade_form", clear_on_submit=True):
    row1 = st.columns([1.1, 1, 1.6, 1])
    with row1[0]:
        trade_date = st.date_input(
            "成交日期",
            value=datetime.now(ZoneInfo("Asia/Shanghai")).date(),
        )
    with row1[1]:
        side = st.selectbox("成交方向", ["买入", "卖出"])
    with row1[2]:
        symbol = st.text_input("代码", placeholder="例如：159501")
    with row1[3]:
        quantity = st.number_input("数量", min_value=0, value=0, step=100)

    row2 = st.columns([1.4, 1, 1, 2])
    with row2[0]:
        name = st.text_input("标的名称")
    with row2[1]:
        price = st.number_input(
            "成交价格",
            min_value=0.0,
            value=0.0,
            step=0.001,
            format="%.3f",
        )
    with row2[2]:
        fee_rate_pct = st.number_input(
            "手续费率(%)",
            min_value=0.0,
            value=0.006,
            step=0.001,
            format="%.4f",
        )
    with row2[3]:
        strategy = st.text_input("策略说明")
    notes = st.text_input("备注")
    submitted = st.form_submit_button("保存成交", type="primary")

if submitted:
    try:
        add_live_trade(
            trade_date=trade_date,
            symbol=symbol,
            name=name,
            side=side,
            price=price,
            quantity=int(quantity),
            fee_rate_pct=fee_rate_pct,
            strategy=strategy,
            notes=notes,
        )
        st.success("成交记录已保存。")
        st.rerun()
    except ValueError as exc:
        st.error(str(exc))

st.subheader("成交明细")
if trades.empty:
    st.info("暂无成交记录。")
else:
    detail = enrich_live_trades(trades).rename(
        columns={
            "id": "记录ID",
            "trade_date": "成交日期",
            "symbol": "代码",
            "name": "标的名称",
            "side": "方向",
            "price": "成交价格",
            "quantity": "数量",
            "fee_rate_pct": "手续费率(%)",
            "gross_amount": "成交金额",
            "fee_amount": "手续费",
            "cash_amount": "实际收付金额",
            "strategy": "策略说明",
            "notes": "备注",
            "created_at": "记录时间",
        }
    )
    detail = detail[
        [
            "记录ID",
            "成交日期",
            "代码",
            "标的名称",
            "方向",
            "成交价格",
            "数量",
            "手续费率(%)",
            "成交金额",
            "手续费",
            "实际收付金额",
            "策略说明",
            "备注",
        ]
    ]
    st.dataframe(
        detail,
        width="stretch",
        hide_index=True,
        column_config={
            "成交价格": st.column_config.NumberColumn(format="%.3f"),
            "数量": st.column_config.NumberColumn(format="%d"),
            "手续费率(%)": st.column_config.NumberColumn(format="%.4f%%"),
            "成交金额": st.column_config.NumberColumn(format="%.2f"),
            "手续费": st.column_config.NumberColumn(format="%.2f"),
            "实际收付金额": st.column_config.NumberColumn(format="%.2f"),
        },
    )
    st.download_button(
        "导出成交记录",
        data=detail.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
        file_name="实盘成交记录.csv",
        mime="text/csv",
    )

    with st.expander("删除误录记录"):
        trade_options = {
            int(row.id): (
                f"#{int(row.id)}｜{row.trade_date}｜{row.symbol}｜{row.side} "
                f"{int(row.quantity)}份 @ {float(row.price):.3f}"
            )
            for row in trades.itertuples(index=False)
        }
        selected_id = st.selectbox(
            "选择记录",
            options=list(trade_options),
            format_func=trade_options.get,
        )
        if st.button("删除所选记录", type="secondary"):
            try:
                if delete_live_trade(int(selected_id)):
                    st.success("记录已删除。")
                    st.rerun()
                else:
                    st.error("记录不存在或已经删除。")
            except ValueError as exc:
                st.error(str(exc))


@st.fragment(run_every="120s")
def render_live_symbol_pnl_history() -> None:
    st.subheader("历史盈亏")
    all_trades = list_live_trades()
    if all_trades.empty:
        st.info("暂无可汇总的历史成交。")
        return

    market_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    price_histories: dict[str, pd.DataFrame] = {}
    symbols = sorted(all_trades["symbol"].dropna().astype(str).unique())
    for symbol in symbols:
        item = load_or_fetch_etf(
            symbol,
            api_key=os.getenv("TICKFLOW_API_KEY", ""),
            count=5000,
            adjust=None,
            allow_fetch=False,
            force_refresh=False,
            save_to_cache=False,
            market_now=market_now,
        )
        if item.dataframe is not None and not item.dataframe.empty:
            price_histories[symbol] = item.dataframe

    history = append_live_symbol_pnl_total(
        build_live_symbol_pnl_history(all_trades, price_histories)
    )
    history_display = history.rename(
        columns={
            "name": "标的名称",
            "symbol": "代码",
            "status": "状态",
            "first_trade_date": "首次交易日",
            "last_trade_date": "最近交易日",
            "quantity": "当前数量",
            "cumulative_buy_cost": "累计买入成本",
            "cumulative_sell_proceeds": "累计卖出回款",
            "market_value": "当前市值",
            "realized_pnl": "已实现盈亏",
            "unrealized_pnl": "未实现盈亏",
            "total_pnl": "累计盈亏",
            "return_pct": "累计盈亏率(%)",
            "fee_amount": "累计手续费",
            "valuation_date": "估值日期",
        }
    )
    history_display = history_display[
        [
            "标的名称",
            "代码",
            "状态",
            "首次交易日",
            "最近交易日",
            "估值日期",
            "当前数量",
            "累计买入成本",
            "累计卖出回款",
            "当前市值",
            "已实现盈亏",
            "未实现盈亏",
            "累计盈亏",
            "累计盈亏率(%)",
            "累计手续费",
        ]
    ]
    render_live_symbol_history_table(history_display)
    st.caption(
        "包含当前持仓和已清仓标的；买入成本含买入手续费，"
        "卖出回款已扣除卖出手续费。"
    )


render_live_symbol_pnl_history()
