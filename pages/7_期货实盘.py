from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.db import init_db
from core.ui import (
    DEFAULT_CHART_HEIGHT,
    apply_global_style,
    apply_plotly_layout,
    render_metric_grid,
    render_page_header,
)
from services.futures_live_trading import (
    ASSET_TYPES,
    BUY_SELL_VALUES,
    OPEN_CLOSE_VALUES,
    add_manual_trade,
    build_contract_pnl_history,
    build_current_position_pnl,
    build_daily_account_pnl,
    configured_statement_dir,
    delete_manual_trade,
    latest_monthly_account,
    list_futures_live_trades,
    list_monthly_accounts,
    list_statement_imports,
    summarize_futures_live_pnl,
    sync_statements,
    update_position_daily_closes,
)
from services.futures_spread import completed_futures_daily_cutoff


st.set_page_config(page_title="期货实盘", layout="wide")
init_db()
apply_global_style()

render_page_header(
    "期货实盘",
    "以月结单为正式账户与持仓依据，汇总全部成交，并用已完成交易日的收盘价和结算价更新盈亏。",
    eyebrow="Futures Live",
)


def format_money(value: object) -> str:
    return "-" if value is None or pd.isna(value) else f"{float(value):,.2f}"


def format_number(value: object, digits: int = 2) -> str:
    return "-" if value is None or pd.isna(value) else f"{float(value):,.{digits}f}"


def format_ratio(value: object) -> str:
    return "-" if value is None or pd.isna(value) else f"{float(value) * 100:.2f}%"


def decode_warnings(value: object) -> list[str]:
    if not value or pd.isna(value):
        return []
    try:
        parsed = json.loads(str(value))
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except Exception:
        return [str(value)]


st.subheader("数据更新")
source_columns = st.columns([4, 1, 1])
with source_columns[0]:
    statement_dir = st.text_input(
        "月结单目录",
        value=str(configured_statement_dir()),
        help="只读取匹配券商月结单命名的 .xls/.xlsx 文件，不修改源文件。",
    )
with source_columns[1]:
    st.write("")
    force_statement_sync = st.button("重新同步月结单", width="stretch")
with source_columns[2]:
    st.write("")
    force_close_refresh = st.button("强制更新收盘/结算价", width="stretch")

try:
    sync_result = sync_statements(statement_dir, force=force_statement_sync)
except Exception as exc:
    sync_result = None
    st.error(f"月结单同步失败：{exc}")

if sync_result is not None:
    status_text = (
        f"扫描 {sync_result.scanned} 份，导入 {sync_result.imported} 份，"
        f"跳过未变化 {sync_result.skipped} 份，失败 {sync_result.failed} 份。"
    )
    (st.warning if sync_result.failed else st.caption)(status_text)
    for message in sync_result.errors:
        st.error(message)

api_key = os.environ.get("TICKFLOW_API_KEY", "")
if force_close_refresh:
    with st.spinner("正在更新期货期权正式收盘价和结算价..."):
        refresh_result = update_position_daily_closes(api_key=api_key, force=True)
    st.session_state["futures_live_close_errors"] = refresh_result["errors"]
    st.session_state["futures_live_settlement_errors"] = refresh_result.get("settlement_errors", [])
    st.session_state["futures_live_close_target"] = refresh_result["target_date"]
    if refresh_result["errors"]:
        st.warning("部分合约更新失败，已保留原有正式收盘数据。")
    else:
        st.success(
            f"正式行情已更新，新增 {refresh_result['updated']} 条收盘记录、"
            f"{refresh_result.get('settlement_updated', 0)} 条结算记录。"
        )

account = latest_monthly_account()
if account is None:
    st.info("当前没有可用月结单。请确认目录后点击“重新同步月结单”。")
    st.stop()

pnl_summary = summarize_futures_live_pnl()
mark_summary = summarize_futures_live_pnl(
    valuation_mode="settlement",
    include_declaration_fee=False,
)
valuation_label = str(pnl_summary.get("valuation_date") or account["statement_end_date"])
mark_matches_close_date = (
    mark_summary.get("valuation_date")
    and mark_summary.get("valuation_date") == pnl_summary.get("valuation_date")
)
mark_pnl = mark_summary.get("net_pnl") if mark_matches_close_date else None
render_metric_grid(
    [
        ("最新月结单", str(account["statement_month"]), "最新成功导入的月结单月份"),
        ("客户权益", format_money(account.get("customer_equity")), "最新月结单正式客户权益"),
        ("当月盈亏", format_money(account.get("monthly_pnl")), "最新月结单当月平仓盈亏"),
        ("月末浮动盈亏", format_money(account.get("floating_pnl")), "最新月结单期货浮动盈亏"),
        ("风险度", format_ratio(account.get("risk_ratio")), "最新月结单风险度"),
        ("保证金", format_money(account.get("margin")), "最新月结单保证金占用"),
        ("可用资金", format_money(account.get("available_funds")), "最新月结单可用资金"),
        ("估值日期", valuation_label, "当前盈亏采用的最近共同正式收盘日期"),
        ("当日盈亏", format_money(pnl_summary.get("daily_pnl")), "估值日相对前一交易日的持仓盈亏变化"),
        ("已实现盈亏", format_money(pnl_summary.get("realized_pnl")), "期货平仓盈亏与期权已结清权利金盈亏"),
        ("最新浮动盈亏", format_money(pnl_summary.get("floating_pnl")), "预计持仓按正式收盘价计算的浮动盈亏"),
        ("累计手续费", format_money(pnl_summary.get("fee")), "月结单账户手续费加未接管手工手续费"),
        (
            "全部盈亏（盯市）",
            format_money(mark_pnl),
            "按结算价估值并扣除交易手续费，不含申报费；与同花顺期货通盯市口径一致",
        ),
        (
            "累计净盈亏（收盘）",
            format_money(pnl_summary.get("net_pnl")),
            "按收盘价估值，并扣除包含申报费在内的全部账户手续费",
        ),
    ]
)
if mark_matches_close_date:
    st.caption(
        f"盯市盈亏按 {mark_summary['valuation_date']} 结算价计算，扣交易手续费、不扣申报费；"
        "收盘口径按同日收盘价计算并扣除全部费用。"
    )
else:
    st.caption("盯市盈亏需等待所有当前合约的同日结算价齐全后显示。")
if float(pnl_summary.get("declaration_fee") or 0) > 0.05:
    st.caption(
        f"累计手续费已包含月结单确认的申报费 {format_money(pnl_summary['declaration_fee'])} 元。"
    )
if abs(float(pnl_summary.get("unallocated_fee") or 0)) > 0.05:
    st.warning(
        f"累计手续费仍有 {format_money(pnl_summary['unallocated_fee'])} 元未能与成交明细及申报费核对。"
    )

close_errors = st.session_state.get("futures_live_close_errors", [])
if close_errors:
    st.error("正式收盘价未全部更新：" + "；".join(close_errors))
settlement_errors = st.session_state.get("futures_live_settlement_errors", [])
if settlement_errors and not mark_matches_close_date:
    st.warning("正式结算价尚未全部更新：" + "；".join(settlement_errors))

st.subheader("当前持仓盈亏")
asset_filter = st.radio(
    "持仓类型",
    ["全部", *ASSET_TYPES],
    horizontal=True,
    label_visibility="collapsed",
    key="futures_live_position_filter",
)
current = build_current_position_pnl()
if asset_filter != "全部" and not current.empty:
    current = current[current["asset_type"].eq(asset_filter)]
if current.empty:
    st.info("当前没有持仓。")
else:
    current_display = current.rename(
        columns={
            "asset_type": "类型",
            "contract": "合约",
            "side": "多空",
            "official_quantity": "官方持仓",
            "post_month_change": "月末后变动",
            "estimated_quantity": "预计持仓",
            "average_price": "持仓均价",
            "latest_close": "最新收盘价",
            "valuation_date": "估值日期",
            "multiplier": "合约乘数",
            "daily_pnl": "当日盈亏",
            "realized_pnl": "已实现盈亏",
            "floating_pnl": "浮动盈亏",
            "fee": "累计手续费",
            "net_pnl": "累计净盈亏",
        }
    )
    st.dataframe(
        current_display,
        width="stretch",
        hide_index=True,
        column_config={
            "持仓均价": st.column_config.NumberColumn(format="%.4f"),
            "最新收盘价": st.column_config.NumberColumn(format="%.4f"),
            "合约乘数": st.column_config.NumberColumn(format="%.0f"),
            "当日盈亏": st.column_config.NumberColumn(format="%.2f"),
            "已实现盈亏": st.column_config.NumberColumn(format="%.2f"),
            "浮动盈亏": st.column_config.NumberColumn(format="%.2f"),
            "累计手续费": st.column_config.NumberColumn(format="%.2f"),
            "累计净盈亏": st.column_config.NumberColumn(format="%.2f"),
        },
    )

daily_pnl = build_daily_account_pnl()
if not daily_pnl.empty:
    st.subheader("账户盈亏趋势")
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=pd.to_datetime(daily_pnl["date"]),
            y=daily_pnl["net_pnl"],
            mode="lines+markers",
            name="累计净盈亏",
            line={"color": "#b91c1c", "width": 2.2},
        )
    )
    figure.add_trace(
        go.Bar(
            x=pd.to_datetime(daily_pnl["date"]),
            y=daily_pnl["daily_pnl"],
            name="当期盈亏变化",
            marker_color="#64748b",
            opacity=0.42,
        )
    )
    apply_plotly_layout(figure, height=DEFAULT_CHART_HEIGHT)
    figure.update_yaxes(title="盈亏（元）")
    st.plotly_chart(figure, width="stretch")

st.subheader("历史盈亏")
history_filter = st.radio(
    "历史类型",
    ["全部", *ASSET_TYPES],
    horizontal=True,
    label_visibility="collapsed",
    key="futures_live_history_filter",
)
history = build_contract_pnl_history()
if history_filter != "全部" and not history.empty:
    history = history[history["asset_type"].eq(history_filter)]
if history.empty:
    st.info("当前没有历史盈亏记录。")
else:
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

with st.expander("手工录入月末后成交", expanded=False):
    minimum_date = pd.Timestamp(account["statement_end_date"]).date() + timedelta(days=1)
    default_date = max(date.today(), minimum_date)
    with st.form("futures_live_manual_trade_form", clear_on_submit=True):
        first = st.columns(4)
        trade_date_value = first[0].date_input("成交日期", value=default_date, min_value=minimum_date)
        trade_time_value = first[1].time_input("成交时间", value=time(9, 30))
        asset_type_value = first[2].selectbox("类型", ASSET_TYPES)
        contract_value = first[3].text_input("合约", placeholder="例如 I2609 或 I2609P730")
        second = st.columns(4)
        buy_sell_value = second[0].selectbox("买卖", BUY_SELL_VALUES)
        open_close_value = second[1].selectbox("开平", OPEN_CLOSE_VALUES)
        price_value = second[2].number_input("成交价格", min_value=0.0, value=0.0, step=0.1, format="%.4f")
        quantity_value = second[3].number_input("手数", min_value=1, value=1, step=1)
        third = st.columns(4)
        turnover_value = third[0].number_input("成交额/权利金", min_value=0.0, value=0.0, step=100.0, format="%.2f")
        fee_value = third[1].number_input("实际手续费", min_value=0.0, value=0.0, step=0.01, format="%.2f")
        broker_trade_id_value = third[2].text_input("成交序号（可选）")
        strategy_value = third[3].text_input("策略（可选）")
        fill_close_pnl = st.checkbox("填写实际平仓盈亏")
        close_pnl_value = st.number_input("实际平仓盈亏", value=0.0, step=100.0, disabled=not fill_close_pnl)
        notes_value = st.text_area("备注", height=72)
        submitted = st.form_submit_button("保存成交", type="primary")
    if submitted:
        try:
            add_manual_trade(
                trade_date=trade_date_value,
                trade_time=trade_time_value.strftime("%H:%M:%S"),
                asset_type=asset_type_value,
                contract=contract_value,
                buy_sell=buy_sell_value,
                open_close=open_close_value,
                price=price_value,
                quantity=int(quantity_value),
                turnover=turnover_value or None,
                fee=fee_value,
                close_pnl=close_pnl_value if fill_close_pnl else None,
                broker_trade_id=broker_trade_id_value,
                strategy=strategy_value,
                notes=notes_value,
            )
            st.success("成交已保存。")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

st.subheader("成交明细")
trades = list_futures_live_trades(include_taken_over=True)
if trades.empty:
    st.info("当前没有成交记录。")
else:
    filter_columns = st.columns(5)
    month_options = ["全部", *sorted(trades["statement_month"].dropna().astype(str).unique(), reverse=True)]
    selected_month = filter_columns[0].selectbox("月份", month_options)
    contract_options = ["全部", *sorted(trades["contract"].dropna().astype(str).unique())]
    selected_contract = filter_columns[1].selectbox("合约", contract_options)
    selected_asset = filter_columns[2].selectbox("类型", ["全部", *ASSET_TYPES])
    selected_source = filter_columns[3].selectbox("来源", ["全部", "月结单", "手工"])
    status_options = ["全部", *sorted(trades["reconciliation_status"].dropna().astype(str).unique())]
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
            "id": "编号", "trade_date": "成交日期", "trade_time": "成交时间",
            "asset_type": "类型", "contract": "合约", "buy_sell": "买卖",
            "open_close": "开平", "price": "成交价", "quantity": "手数",
            "turnover": "成交额/权利金", "fee": "手续费", "close_pnl": "平仓盈亏",
            "source": "来源", "reconciliation_status": "核对状态",
            "broker_trade_id": "成交序号", "strategy": "策略", "notes": "备注",
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
            format_func=lambda value: f"#{value} {manual.loc[manual['id'].eq(value), 'contract'].iloc[0]}",
            label_visibility="collapsed",
        )
        if export_columns[2].button("删除所选手工成交"):
            try:
                delete_manual_trade(int(delete_id))
                st.success("手工成交已删除。")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

st.subheader("月度账户与导入状态")
account_history = list_monthly_accounts().rename(
    columns={
        "statement_month": "月份", "customer_equity": "客户权益",
        "monthly_pnl": "当月盈亏", "floating_pnl": "浮动盈亏",
        "monthly_fee": "手续费", "declaration_fee": "其中申报费",
        "margin": "保证金", "available_funds": "可用资金",
        "risk_ratio": "风险度", "deposits_withdrawals": "存取合计",
    }
)
account_history["风险度"] = pd.to_numeric(account_history["风险度"], errors="coerce") * 100
st.dataframe(
    account_history[
        ["月份", "客户权益", "当月盈亏", "浮动盈亏", "手续费", "其中申报费", "保证金", "可用资金", "风险度", "存取合计"]
    ].sort_values("月份", ascending=False),
    width="stretch",
    hide_index=True,
    column_config={"风险度": st.column_config.NumberColumn(format="%.2f%%")},
)
imports = list_statement_imports()
for record in imports.to_dict("records"):
    for message in decode_warnings(record.get("warnings")):
        st.warning(f"{record['file_name']}：{message}")
    if record.get("error_message"):
        st.error(f"{record['file_name']}：{record['error_message']}")
with st.expander("查看月结单导入记录", expanded=False):
    import_display = imports.rename(
        columns={
            "file_name": "文件", "statement_month": "月份", "imported_at": "导入时间",
            "status": "状态", "warnings": "对账提示", "error_message": "错误",
        }
    )
    st.dataframe(import_display, width="stretch", hide_index=True)

# 每个页面会话对同一目标交易日只自动尝试一次。成功写入后重跑页面展示新估值。
target_date = completed_futures_daily_cutoff().strftime("%Y-%m-%d")
auto_key = "futures_live_auto_close_target"
if st.session_state.get(auto_key) != target_date and not force_close_refresh:
    st.session_state[auto_key] = target_date
    with st.spinner(f"正在补齐截至 {target_date} 的正式收盘价和结算价..."):
        auto_result = update_position_daily_closes(api_key=api_key)
    st.session_state["futures_live_close_errors"] = auto_result["errors"]
    st.session_state["futures_live_settlement_errors"] = auto_result.get("settlement_errors", [])
    st.session_state["futures_live_close_target"] = auto_result["target_date"]
    if auto_result["updated"] > 0 or auto_result.get("settlement_updated", 0) > 0:
        st.rerun()
