"""期货实盘趋势、历史明细与导出组件。"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from components.futures_live.formatting import decode_warnings
from core.return_calendar import render_return_calendar
from core.ui import (
    DEFAULT_CHART_HEIGHT,
    apply_plotly_layout,
    build_sparse_trading_date_ticks,
    filter_by_time_range,
)
from services import futures_live_trading as futures_live


def render_account_trend(
    close_daily_pnl: pd.DataFrame,
    settlement_daily_pnl: pd.DataFrame,
) -> None:
    st.subheader("账户盈亏趋势")
    pnl_mode = st.segmented_control(
        "盈亏口径",
        ["盯市", "收盘"],
        default="盯市",
        key="futures_live_pnl_mode",
        label_visibility="collapsed",
    )
    daily_pnl = settlement_daily_pnl if pnl_mode == "盯市" else close_daily_pnl
    if daily_pnl.empty:
        st.info(f"当前没有可展示的{pnl_mode}收益数据。")
        return
    cumulative_daily_pnl = daily_pnl[
        daily_pnl["status"].isin(["完整", "手工估算"])
        & pd.to_numeric(daily_pnl["net_pnl"], errors="coerce").notna()
    ].copy()
    amount_daily_pnl = daily_pnl[
        daily_pnl["status"].isin(["完整", "手工估算"])
        & pd.to_numeric(daily_pnl["daily_pnl"], errors="coerce").notna()
    ].copy()
    period = st.segmented_control(
        "时间范围",
        ["近1月", "近3月", "近1年", "全部"],
        default="全部",
        key="futures_live_period",
        label_visibility="collapsed",
    )
    view_cumulative = filter_by_time_range(cumulative_daily_pnl, date_column="date", period=period or "全部")
    view_amount = filter_by_time_range(amount_daily_pnl, date_column="date", period=period or "全部")
    chart_dates = pd.to_datetime(view_cumulative["date"], errors="coerce").dt.strftime("%Y-%m-%d")

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=chart_dates,
            y=view_cumulative["net_pnl"],
            mode="lines+markers",
            name=f"累计{pnl_mode}净盈亏",
            line={"color": "#b91c1c", "width": 2.2},
            hovertemplate=f"累计{pnl_mode}净盈亏: %{{y:.2f}}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Bar(
            x=pd.to_datetime(view_amount["date"], errors="coerce").dt.strftime("%Y-%m-%d"),
            y=view_amount["daily_pnl"],
            name=f"当日{pnl_mode}盈亏",
            marker_color="#64748b",
            opacity=0.42,
            hovertemplate=f"当日{pnl_mode}盈亏: %{{y:.2f}}<extra></extra>",
        )
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
    figure.update_yaxes(title="盈亏（元）", hoverformat=".2f", tickformat=".2f")
    st.plotly_chart(figure, width="stretch")
    incomplete = daily_pnl[daily_pnl["status"].eq("数据不完整")]
    if not incomplete.empty:
        latest_gap = incomplete.iloc[-1]
        st.warning(
            f"{latest_gap['date']} 暂未生成{pnl_mode}收益：{latest_gap['missing_contracts']}。"
            "已完成日期仍正常展示。"
        )
    manual_days = daily_pnl[daily_pnl["status"].eq("手工估算")]
    if not manual_days.empty:
        st.caption(
            "盯市曲线含同花顺手工补录："
            + "、".join(manual_days["date"].astype(str).tolist())
            + "；这些值只补足账户级当日盈亏，不会生成单合约结算价。"
        )
    futures_daily_returns = futures_live.build_futures_daily_returns(daily_pnl)
    render_return_calendar(
        futures_daily_returns,
        title=f"{pnl_mode}净收益日历",
        key_prefix=f"futures_live_return_calendar_{pnl_mode}",
        first_date=daily_pnl["date"].min(),
        caption=(
            f"收益金额为累计净盈亏（{pnl_mode}）的日变化；每日收益率以此前一交易日{pnl_mode}经济权益"
            "加当日正净入金为基数，周、月、年收益率按每日收益率复合。"
            + (
                "盯市口径扣成交手续费和行权手续费，不扣申报费及其他账户级费用。"
                if pnl_mode == "盯市"
                else "收盘口径扣除包含申报费在内的全部账户手续费。"
            )
            + "最新月结单之后的结果待月结单确认。"
        ),
    )


def render_contract_history() -> None:
    st.subheader("历史盈亏")
    history_filter = st.radio(
        "历史类型",
        ["全部", *futures_live.ASSET_TYPES],
        horizontal=True,
        label_visibility="collapsed",
        key="futures_live_history_filter",
    )
    history = futures_live.build_contract_pnl_history()
    if history_filter != "全部" and not history.empty:
        history = history[history["asset_type"].eq(history_filter)]
    if history.empty:
        st.info("当前没有历史盈亏记录。")
        return
    history_display = history.rename(
        columns={
            "asset_type": "类型",
            "contract": "合约",
            "status": "状态",
            "first_trade_date": "首次成交日",
            "last_trade_date": "最近成交日",
            "open_quantity": "累计开仓量",
            "close_quantity": "累计平仓量",
            "long_quantity": "剩余多仓",
            "short_quantity": "剩余空仓",
            "realized_pnl": "已实现盈亏",
            "floating_pnl": "浮动盈亏",
            "fee": "累计手续费",
            "net_pnl": "累计净盈亏",
            "valuation_date": "估值日期",
        }
    )
    st.dataframe(
        history_display,
        width="stretch",
        hide_index=True,
        column_config={
            "已实现盈亏": st.column_config.NumberColumn(format="%.2f"),
            "浮动盈亏": st.column_config.NumberColumn(format="%.2f"),
            "累计手续费": st.column_config.NumberColumn(format="%.2f"),
            "累计净盈亏": st.column_config.NumberColumn(format="%.2f"),
        },
    )


def render_cash_flows() -> None:
    st.subheader("资金流水明细")
    cash_flows = futures_live.list_futures_cash_flows(include_taken_over=True)
    if cash_flows.empty:
        st.info("当前没有资金流水记录。")
        return
    cash_flow_display = cash_flows.rename(
        columns={
            "id": "编号",
            "flow_date": "发生日期",
            "entry_type": "类型",
            "amount": "金额",
            "source": "来源",
            "reconciliation_status": "核对状态",
            "notes": "备注",
        }
    )
    st.dataframe(
        cash_flow_display[
            ["编号", "发生日期", "类型", "金额", "来源", "核对状态", "备注"]
        ],
        width="stretch",
        hide_index=True,
        column_config={"金额": st.column_config.NumberColumn(format="%.2f")},
    )
    manual_flows = cash_flows[cash_flows["source"].eq("手工")]
    if not manual_flows.empty:
        flow_delete_columns = st.columns([2, 1, 4])
        delete_flow_id = flow_delete_columns[0].selectbox(
            "删除手工资金流水",
            manual_flows["id"].astype(int).tolist(),
            format_func=lambda value: (
                f"#{value} "
                f"{manual_flows.loc[manual_flows['id'].eq(value), 'flow_date'].iloc[0]} "
                f"{manual_flows.loc[manual_flows['id'].eq(value), 'entry_type'].iloc[0]}"
            ),
            label_visibility="collapsed",
        )
        if flow_delete_columns[1].button("删除资金流水"):
            futures_live.delete_manual_cash_flow(int(delete_flow_id))
            st.success("手工资金流水已删除。")
            st.rerun()


def render_trade_history() -> None:
    st.subheader("成交明细")
    trades = futures_live.list_futures_live_trades(include_taken_over=True)
    if trades.empty:
        st.info("当前没有成交记录。")
        return
    filter_columns = st.columns(5)
    month_options = [
        "全部",
        *sorted(
            trades["statement_month"].dropna().astype(str).unique(), reverse=True
        ),
    ]
    selected_month = filter_columns[0].selectbox("月份", month_options)
    contract_options = [
        "全部", *sorted(trades["contract"].dropna().astype(str).unique())
    ]
    selected_contract = filter_columns[1].selectbox("合约", contract_options)
    selected_asset = filter_columns[2].selectbox(
        "类型", ["全部", *futures_live.ASSET_TYPES]
    )
    selected_source = filter_columns[3].selectbox(
        "来源", ["全部", "月结单", "手工"]
    )
    status_options = [
        "全部",
        *sorted(
            trades["reconciliation_status"].dropna().astype(str).unique()
        ),
    ]
    selected_status = filter_columns[4].selectbox("核对状态", status_options)
    filtered = trades.copy()
    if selected_month != "全部":
        filtered = filtered[filtered["statement_month"].eq(selected_month)]
    if selected_contract != "全部":
        filtered = filtered[filtered["contract"].eq(selected_contract)]
    if selected_asset != "全部":
        filtered = filtered[filtered["asset_type"].eq(selected_asset)]
    if selected_source != "全部":
        filtered = filtered[filtered["source"].eq(selected_source)]
    if selected_status != "全部":
        filtered = filtered[filtered["reconciliation_status"].eq(selected_status)]
    trade_display = filtered[
        [
            "id", "trade_date", "trade_time", "asset_type", "contract", "buy_sell",
            "open_close", "price", "quantity", "turnover", "fee", "close_pnl",
            "source", "reconciliation_status", "broker_trade_id", "strategy", "notes",
        ]
    ].rename(
        columns={
            "id": "编号",
            "trade_date": "成交日期",
            "trade_time": "成交时间",
            "asset_type": "类型",
            "contract": "合约",
            "buy_sell": "买卖",
            "open_close": "开平",
            "price": "成交价",
            "quantity": "手数",
            "turnover": "成交额/权利金",
            "fee": "手续费",
            "close_pnl": "平仓盈亏",
            "source": "来源",
            "reconciliation_status": "核对状态",
            "broker_trade_id": "成交序号",
            "strategy": "策略",
            "notes": "备注",
        }
    )
    st.dataframe(trade_display, width="stretch", hide_index=True)
    export_columns = st.columns([1, 1, 3])
    export_columns[0].download_button(
        "导出当前成交",
        data=trade_display.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"期货实盘成交_{datetime.now():%Y%m%d}.csv",
        mime="text/csv",
        width="stretch",
    )
    manual = trades[trades["source"].eq("手工")]
    if not manual.empty:
        delete_id = export_columns[1].selectbox(
            "删除手工成交",
            manual["id"].astype(int).tolist(),
            format_func=lambda value: (
                f"#{value} {manual.loc[manual['id'].eq(value), 'contract'].iloc[0]}"
            ),
            label_visibility="collapsed",
        )
        if export_columns[2].button("删除所选手工成交"):
            try:
                futures_live.delete_manual_trade(int(delete_id))
                st.success("手工成交已删除。")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))


def render_monthly_status() -> None:
    st.subheader("月度账户与导入状态")
    account_history = futures_live.list_monthly_accounts().rename(
        columns={
            "statement_month": "月份",
            "customer_equity": "客户权益",
            "monthly_pnl": "当月盈亏",
            "floating_pnl": "浮动盈亏",
            "monthly_fee": "手续费",
            "declaration_fee": "其中申报费",
            "margin": "保证金",
            "available_funds": "可用资金",
            "risk_ratio": "风险度",
            "deposits_withdrawals": "存取合计",
        }
    )
    account_history["风险度"] = (
        pd.to_numeric(account_history["风险度"], errors="coerce") * 100
    )
    st.dataframe(
        account_history[
            [
                "月份", "客户权益", "当月盈亏", "浮动盈亏", "手续费", "其中申报费",
                "保证金", "可用资金", "风险度", "存取合计",
            ]
        ].sort_values("月份", ascending=False),
        width="stretch",
        hide_index=True,
        column_config={"风险度": st.column_config.NumberColumn(format="%.2f%%")},
    )
    imports = futures_live.list_statement_imports()
    for record in imports.to_dict("records"):
        for message in decode_warnings(record.get("warnings")):
            st.warning(f"{record['file_name']}：{message}")
        if record.get("error_message"):
            st.error(f"{record['file_name']}：{record['error_message']}")
    with st.expander("查看月结单导入记录", expanded=False):
        import_display = imports.rename(
            columns={
                "file_name": "文件",
                "statement_month": "月份",
                "imported_at": "导入时间",
                "status": "状态",
                "warnings": "对账提示",
                "error_message": "错误",
            }
        )
        st.dataframe(import_display, width="stretch", hide_index=True)
