"""期货实盘手工成交、资金与盯市盈亏表单。"""

from __future__ import annotations

from datetime import date, time, timedelta

import pandas as pd
import streamlit as st

from services import futures_live_trading as futures_live
from services import futures_spread


def render_daily_pnl_override_form() -> None:
    with st.expander("同花顺日盈亏补录", expanded=False):
        completed_date = futures_spread.completed_futures_daily_cutoff().date()
        account = futures_live.latest_monthly_account()
        statement_end = (
            pd.Timestamp(account["statement_end_date"]).date()
            if account is not None
            else None
        )
        minimum_date = (
            statement_end.replace(day=1)
            if statement_end is not None
            else completed_date
        )
        if minimum_date > completed_date:
            st.caption(
                f"当前没有不晚于 {completed_date} 的已完成交易日可供补录。"
            )
            return
        if statement_end is not None:
            st.caption(
                "月结单不含逐日盯市明细；月内缺少正式结算价时仍可补录。"
                "同日完整正式结果生成后会自动核对并接管。"
            )
        with st.form("futures_live_daily_pnl_form", clear_on_submit=True):
            override_columns = st.columns([2, 2, 4])
            override_date = override_columns[0].date_input(
                "交易日期",
                value=completed_date,
                min_value=minimum_date,
                max_value=completed_date,
            )
            override_amount = override_columns[1].number_input(
                "同花顺当日盈亏", value=0.0, step=100.0, format="%.2f"
            )
            override_notes = override_columns[2].text_input("备注（可选）")
            override_submitted = st.form_submit_button("保存日盈亏", type="primary")
        if override_submitted:
            try:
                futures_live.add_manual_daily_pnl(
                    trade_date=override_date,
                    pnl_amount=override_amount,
                    notes=override_notes,
                )
                st.success("同花顺当日盈亏已保存。")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))


def render_daily_pnl_reconciliation() -> None:
    daily_overrides = futures_live.list_futures_daily_pnl_overrides()
    if daily_overrides.empty:
        return
    st.subheader("盯市差异核对")
    override_display = daily_overrides.rename(
        columns={
            "id": "编号",
            "trade_date": "交易日期",
            "pnl_amount": "同花顺当日盈亏",
            "formal_pnl": "正式结算盈亏",
            "difference": "正式减手工",
            "source": "来源",
            "reconciliation_status": "核对状态",
            "resolution": "采用口径",
            "notes": "备注",
        }
    )
    st.dataframe(
        override_display[
            [
                "编号", "交易日期", "同花顺当日盈亏", "正式结算盈亏",
                "正式减手工", "来源", "核对状态", "采用口径", "备注",
            ]
        ],
        width="stretch",
        hide_index=True,
        column_config={
            "同花顺当日盈亏": st.column_config.NumberColumn(format="%.2f"),
            "正式结算盈亏": st.column_config.NumberColumn(format="%.2f"),
            "正式减手工": st.column_config.NumberColumn(format="%.2f"),
        },
    )
    mismatch = daily_overrides[
        daily_overrides["reconciliation_status"].isin(["待核对", "待确认"])
    ]
    if not mismatch.empty:
        review_columns = st.columns([3, 1, 1, 3])
        review_id = review_columns[0].selectbox(
            "待核对日期",
            mismatch["id"].astype(int).tolist(),
            format_func=lambda value: str(
                mismatch.loc[mismatch["id"].eq(value), "trade_date"].iloc[0]
            ),
            label_visibility="collapsed",
        )
        selected = mismatch.loc[mismatch["id"].eq(review_id)].iloc[0]
        formal_available = pd.notna(selected.get("formal_pnl"))
        if review_columns[1].button(
            "采用手工" if formal_available else "确认同花顺"
        ):
            futures_live.resolve_manual_daily_pnl(int(review_id), "采用手工")
            st.rerun()
        if review_columns[2].button("采用正式", disabled=not formal_available):
            futures_live.resolve_manual_daily_pnl(int(review_id), "采用正式")
            st.rerun()
    delete_override_columns = st.columns([3, 1, 4])
    delete_override_id = delete_override_columns[0].selectbox(
        "删除补录",
        daily_overrides["id"].astype(int).tolist(),
        format_func=lambda value: (
            f"#{value} "
            f"{daily_overrides.loc[daily_overrides['id'].eq(value), 'trade_date'].iloc[0]}"
        ),
        label_visibility="collapsed",
        key="futures_live_delete_daily_pnl",
    )
    if delete_override_columns[1].button("删除补录"):
        futures_live.delete_manual_daily_pnl(int(delete_override_id))
        st.success("手工日盈亏已删除。")
        st.rerun()


def render_manual_trade_form(account: pd.Series | dict[str, object]) -> None:
    with st.expander("手工录入月末后成交", expanded=False):
        minimum_date = pd.Timestamp(account["statement_end_date"]).date() + timedelta(days=1)
        default_date = max(date.today(), minimum_date)
        with st.form("futures_live_manual_trade_form", clear_on_submit=True):
            first = st.columns(4)
            trade_date_value = first[0].date_input(
                "成交日期", value=default_date, min_value=minimum_date
            )
            trade_time_value = first[1].time_input("成交时间", value=time(9, 30))
            asset_type_value = first[2].selectbox("类型", futures_live.ASSET_TYPES)
            contract_value = first[3].text_input(
                "合约", placeholder="例如 I2609 或 I2609P730"
            )
            second = st.columns(4)
            buy_sell_value = second[0].selectbox("买卖", futures_live.BUY_SELL_VALUES)
            open_close_value = second[1].selectbox("开平", futures_live.OPEN_CLOSE_VALUES)
            price_value = second[2].number_input(
                "成交价格", min_value=0.0, value=0.0, step=0.1, format="%.4f"
            )
            quantity_value = second[3].number_input(
                "手数", min_value=1, value=1, step=1
            )
            third = st.columns(4)
            turnover_value = third[0].number_input(
                "成交额/权利金", min_value=0.0, value=0.0, step=100.0, format="%.2f"
            )
            fee_value = third[1].number_input(
                "实际手续费", min_value=0.0, value=0.0, step=0.01, format="%.2f"
            )
            broker_trade_id_value = third[2].text_input("成交序号（可选）")
            strategy_value = third[3].text_input("策略（可选）")
            fill_close_pnl = st.checkbox("填写实际平仓盈亏")
            close_pnl_value = st.number_input(
                "实际平仓盈亏",
                value=0.0,
                step=100.0,
                disabled=not fill_close_pnl,
            )
            notes_value = st.text_area("备注", height=72)
            submitted = st.form_submit_button("保存成交", type="primary")
        if submitted:
            try:
                futures_live.add_manual_trade(
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


def render_manual_cash_flow_form(account: pd.Series | dict[str, object]) -> None:
    with st.expander("手工录入月末后资金流水", expanded=False):
        minimum_flow_date = (
            pd.Timestamp(account["statement_end_date"]).date() + timedelta(days=1)
        )
        default_flow_date = max(date.today(), minimum_flow_date)
        with st.form("futures_live_manual_cash_flow_form", clear_on_submit=True):
            flow_columns = st.columns([2, 2, 2, 4])
            flow_date_value = flow_columns[0].date_input(
                "发生日期", value=default_flow_date, min_value=minimum_flow_date
            )
            flow_type_value = flow_columns[1].selectbox(
                "类型", futures_live.CASH_FLOW_TYPES
            )
            flow_amount_value = flow_columns[2].number_input(
                "金额", min_value=0.0, value=0.0, step=100.0, format="%.2f"
            )
            flow_notes_value = flow_columns[3].text_input("备注（可选）")
            flow_submitted = st.form_submit_button("保存资金流水", type="primary")
        if flow_submitted:
            try:
                futures_live.add_manual_cash_flow(
                    flow_date=flow_date_value,
                    entry_type=flow_type_value,
                    amount=flow_amount_value,
                    notes=flow_notes_value,
                )
                st.success("资金流水已保存，收益率基数将同步更新。")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
