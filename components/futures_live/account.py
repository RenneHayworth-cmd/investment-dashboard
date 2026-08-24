"""期货实盘账户摘要、期权到期和当前持仓组件。"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from components.futures_live.formatting import format_money, format_ratio
from core.ui import render_metric_grid
from services import futures_live_trading as futures_live


def render_account_summary(account: pd.Series | dict[str, object]) -> None:
    pnl_summary = futures_live.summarize_futures_live_pnl()
    mark_summary = futures_live.summarize_futures_live_pnl(
        valuation_mode="settlement",
        include_declaration_fee=False,
    )
    valuation_label = str(
        pnl_summary.get("valuation_date") or account["statement_end_date"]
    )
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


def render_option_expiry() -> None:
    expiry_candidates = futures_live.list_option_expiry_candidates()
    expiry_events = futures_live.list_option_expiry_events()
    if not expiry_candidates.empty or not expiry_events.empty:
        st.subheader("期权到期处理")
    if not expiry_candidates.empty:
        candidate_display = expiry_candidates.rename(
            columns={
                "option_contract": "期权合约",
                "option_side": "持仓方向",
                "quantity": "手数",
                "expiry_date": "到期日",
                "underlying_contract": "标的合约",
                "strike": "执行价",
                "settlement_price": "标的结算价",
                "expected_outcome": "预判结果",
                "expected_futures_side": "预计期货方向",
                "status": "状态",
            }
        )
        candidate_display.insert(0, "确认", False)
        candidate_display["确认结果"] = candidate_display["预判结果"].where(
            candidate_display["预判结果"].isin(futures_live.OPTION_EXPIRY_OUTCOMES),
            "作废",
        )
        edited_expiry = st.data_editor(
            candidate_display,
            width="stretch",
            hide_index=True,
            disabled=[
                "期权合约", "持仓方向", "手数", "到期日", "标的合约",
                "执行价", "标的结算价", "预判结果", "预计期货方向", "状态",
            ],
            column_config={
                "确认": st.column_config.CheckboxColumn("确认"),
                "确认结果": st.column_config.SelectboxColumn(
                    "确认结果",
                    options=list(futures_live.OPTION_EXPIRY_OUTCOMES),
                    required=True,
                ),
                "执行价": st.column_config.NumberColumn(format="%.2f"),
                "标的结算价": st.column_config.NumberColumn(format="%.2f"),
            },
            key="futures_live_expiry_editor",
        )
        if st.button("确认所选到期结果", type="primary"):
            selected = edited_expiry[edited_expiry["确认"]]
            unavailable = selected[~selected["状态"].eq("待确认")]
            if selected.empty:
                st.warning("请先选择需要确认的期权合约。")
            elif not unavailable.empty:
                st.warning("只有已取得正式标的结算价的到期合约可以确认。")
            else:
                try:
                    for record in selected.to_dict("records"):
                        futures_live.confirm_option_expiry_event(
                            option_contract=record["期权合约"],
                            outcome=record["确认结果"],
                            quantity=int(record["手数"]),
                        )
                    st.success("到期结果已确认，预计持仓和收益将按确认结果更新。")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
        if expiry_candidates["status"].eq("等待结算价").any():
            st.info("到期日标的期货正式结算价尚未取得，暂不生成到期后的正式收益。")
        elif expiry_candidates["status"].eq("待到期").any():
            nearest = expiry_candidates["expiry_date"].min()
            st.caption(
                f"最近到期日为 {nearest}；提前平仓并手工录入后，对应合约会自动退出到期清单。"
            )
    if not expiry_events.empty:
        with st.expander("查看已确认到期记录", expanded=False):
            event_display = expiry_events.rename(
                columns={
                    "id": "编号",
                    "event_date": "到期日",
                    "option_contract": "期权合约",
                    "outcome": "结果",
                    "quantity": "手数",
                    "underlying_contract": "标的合约",
                    "futures_side": "期货方向",
                    "strike": "执行价",
                    "settlement_price": "标的结算价",
                    "reconciliation_status": "核对状态",
                    "source": "来源",
                    "notes": "备注",
                }
            )
            st.dataframe(
                event_display[
                    [
                        "编号", "到期日", "期权合约", "结果", "手数", "标的合约",
                        "期货方向", "执行价", "标的结算价", "核对状态", "来源", "备注",
                    ]
                ],
                width="stretch",
                hide_index=True,
            )
            manual_events = expiry_events[expiry_events["source"].eq("手工")]
            if not manual_events.empty:
                event_delete_columns = st.columns([2, 1, 4])
                delete_event_id = event_delete_columns[0].selectbox(
                    "删除手工到期记录",
                    manual_events["id"].astype(int).tolist(),
                    format_func=lambda value: (
                        f"#{value} "
                        f"{manual_events.loc[manual_events['id'].eq(value), 'option_contract'].iloc[0]}"
                    ),
                    label_visibility="collapsed",
                )
                if event_delete_columns[1].button("删除到期记录"):
                    futures_live.delete_manual_option_expiry_event(int(delete_event_id))
                    st.success("手工到期记录已删除。")
                    st.rerun()


def render_current_positions() -> None:
    st.subheader("当前持仓盈亏")
    asset_filter = st.radio(
        "持仓类型",
        ["全部", *futures_live.ASSET_TYPES],
        horizontal=True,
        label_visibility="collapsed",
        key="futures_live_position_filter",
    )
    current = futures_live.build_current_position_pnl()
    if asset_filter != "全部" and not current.empty:
        current = current[current["asset_type"].eq(asset_filter)]
    if current.empty:
        st.info("当前没有持仓。")
        return
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
