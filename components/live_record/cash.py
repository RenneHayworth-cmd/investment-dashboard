"""实盘账户资金流水维护组件。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from services.live_trading import (
    LIVE_CASH_FLOW_TYPES,
    add_live_cash_flow,
    delete_live_cash_flow,
)


def render_live_cash_flow_form(
    *,
    add_cash_flow=add_live_cash_flow,
    flow_date_value=None,
) -> None:
    st.subheader("新增资金流水")
    with st.form("live_cash_flow_form", clear_on_submit=True):
        row1 = st.columns([1.15, 1.2, 1.2, 1.2])
        with row1[0]:
            flow_date = st.date_input(
                "流水日期",
                value=(
                    datetime.now(ZoneInfo("Asia/Shanghai")).date()
                    if flow_date_value is None
                    else flow_date_value
                ),
                key="live_cash_flow_date",
            )
        with row1[1]:
            record_flow_time = st.checkbox("记录流水时间", value=False)
            flow_time = st.time_input(
                "流水时间",
                value=datetime.now(ZoneInfo("Asia/Shanghai")).time().replace(microsecond=0),
                disabled=not record_flow_time,
            )
        with row1[2]:
            entry_type = st.selectbox("流水类型", list(LIVE_CASH_FLOW_TYPES))
        with row1[3]:
            amount = st.number_input(
                "金额",
                min_value=0.0,
                value=0.0,
                step=1000.0,
                format="%.2f",
            )
        row2 = st.columns([1, 2])
        with row2[0]:
            symbol = st.text_input(
                "关联ETF代码（可选）",
                placeholder="分红或其他收支可填写",
            )
        with row2[1]:
            notes = st.text_input("流水备注")
        submitted = st.form_submit_button("保存资金流水", type="primary")

    if submitted:
        try:
            add_cash_flow(
                flow_date=flow_date,
                flow_time=flow_time if record_flow_time else None,
                entry_type=entry_type,
                amount=amount,
                symbol=symbol,
                notes=notes,
            )
            st.success("资金流水已保存。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))


def render_live_cash_flow_details(
    cash_flows: pd.DataFrame,
    *,
    delete_cash_flow=delete_live_cash_flow,
) -> None:
    st.subheader("资金流水明细")
    if cash_flows is None or cash_flows.empty:
        st.info("暂无资金流水；请先录入期初资金。")
        return
    detail = cash_flows.rename(
        columns={
            "id": "记录ID",
            "flow_date": "流水日期",
            "flow_time": "流水时间",
            "entry_type": "流水类型",
            "amount": "金额",
            "symbol": "关联ETF",
            "notes": "备注",
        }
    )
    detail = detail[
        ["记录ID", "流水日期", "流水时间", "流水类型", "金额", "关联ETF", "备注"]
    ]
    st.dataframe(
        detail,
        width="stretch",
        hide_index=True,
        column_config={"金额": st.column_config.NumberColumn(format="%.2f")},
    )
    st.download_button(
        "导出资金流水",
        data=detail.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
        file_name="实盘账户资金流水.csv",
        mime="text/csv",
    )
    with st.expander("删除误录资金流水"):
        options = {}
        for row in cash_flows.itertuples(index=False):
            flow_time = "" if pd.isna(row.flow_time) else str(row.flow_time)
            options[int(row.id)] = (
                f"#{int(row.id)}｜{row.flow_date} {flow_time}｜"
                f"{row.entry_type}｜{float(row.amount):,.2f}元"
            )
        selected_id = st.selectbox(
            "选择资金流水",
            options=list(options),
            format_func=options.get,
            key="live_cash_flow_delete_id",
        )
        confirmed = st.checkbox(
            "确认删除所选资金流水",
            value=False,
            key=f"live_cash_flow_delete_confirm_{selected_id}",
        )
        if st.button(
            "删除所选资金流水",
            type="secondary",
            disabled=not confirmed,
        ):
            if delete_cash_flow(int(selected_id)):
                st.success("资金流水已删除。")
                st.rerun()
            else:
                st.error("记录不存在或已经删除。")


__all__ = ["render_live_cash_flow_details", "render_live_cash_flow_form"]
