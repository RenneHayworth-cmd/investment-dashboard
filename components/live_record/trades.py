"""实盘记录的汇总、录入和成交明细组件。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from components.live_record.formatting import money
from core.ui import render_metric_grid
from services.live_trading import (
    add_live_trade,
    delete_live_trade,
    enrich_live_trades,
    summarize_live_trades,
)


def render_live_trade_summary(
    trades: pd.DataFrame,
    *,
    summarize_trades=summarize_live_trades,
) -> None:
    summary = summarize_trades(trades)
    render_metric_grid(
        [
            ("成交记录", str(summary["record_count"]), "已保存的实际成交笔数"),
            ("当前标的", str(summary["position_count"]), "当前数量大于零的标的数量"),
            ("累计买入金额", money(summary["buy_amount"]), "不含手续费的累计买入金额"),
            ("累计手续费", money(summary["fee_amount"]), "全部买卖成交手续费合计"),
            ("当前净投入", money(summary["net_investment"]), "买入支出减去卖出回款"),
        ]
    )


def render_live_trade_form(
    *,
    add_trade=add_live_trade,
    trade_date_value=None,
) -> None:
    st.subheader("新增成交")
    with st.form("live_trade_form", clear_on_submit=True):
        row1 = st.columns([1.1, 1.2, 1, 1.6, 1])
        with row1[0]:
            trade_date = st.date_input(
                "成交日期",
                value=(
                    datetime.now(ZoneInfo("Asia/Shanghai")).date()
                    if trade_date_value is None
                    else trade_date_value
                ),
            )
        with row1[1]:
            record_trade_time = st.checkbox("记录成交时间", value=False)
            trade_time = st.time_input(
                "成交时间",
                value=datetime.now(ZoneInfo("Asia/Shanghai")).time().replace(microsecond=0),
                disabled=not record_trade_time,
            )
        with row1[2]:
            side = st.selectbox("成交方向", ["买入", "卖出"])
        with row1[3]:
            symbol = st.text_input("代码", placeholder="例如：159501")
        with row1[4]:
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
            add_trade(
                trade_date=trade_date,
                trade_time=trade_time if record_trade_time else None,
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


def render_live_trade_details(
    trades: pd.DataFrame,
    *,
    enrich_trades=enrich_live_trades,
    delete_trade=delete_live_trade,
    title: str = "成交明细",
    allow_delete: bool = True,
    show_download: bool = True,
) -> None:
    st.subheader(title)
    if trades.empty:
        st.info("暂无成交记录。")
        return

    detail = enrich_trades(trades).rename(
        columns={
            "id": "记录ID",
            "trade_date": "成交日期",
            "trade_time": "成交时间",
            "symbol": "代码",
            "name": "标的名称",
            "side": "方向",
            "price": "成交价格",
            "quantity": "数量",
            "fee_rate_pct": "手续费率(%)",
            "gross_amount": "成交金额",
            "fee_amount": "手续费",
            "cash_amount": "实际收付金额",
            "realized_pnl": "本次已实现盈亏",
            "strategy": "策略说明",
            "notes": "备注",
            "created_at": "记录时间",
        }
    )
    detail = detail[
        [
            "记录ID",
            "成交日期",
            "成交时间",
            "代码",
            "标的名称",
            "方向",
            "成交价格",
            "数量",
            "手续费率(%)",
            "成交金额",
            "手续费",
            "实际收付金额",
            "本次已实现盈亏",
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
            "本次已实现盈亏": st.column_config.NumberColumn(format="%.2f"),
        },
    )
    if show_download:
        st.download_button(
            "导出成交记录",
            data=detail.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
            file_name="实盘成交记录.csv",
            mime="text/csv",
        )

    if not allow_delete:
        return
    with st.expander("删除误录记录"):
        trade_options = {}
        for row in trades.itertuples(index=False):
            raw_trade_time = getattr(row, "trade_time", None)
            trade_time_text = "" if pd.isna(raw_trade_time) else str(raw_trade_time)
            trade_options[int(row.id)] = (
                f"#{int(row.id)}｜{row.trade_date} {trade_time_text}｜"
                f"{row.symbol}｜{row.side} {int(row.quantity)}份 @ {float(row.price):.3f}"
            )
        selected_id = st.selectbox(
            "选择记录",
            options=list(trade_options),
            format_func=trade_options.get,
        )
        confirmed = st.checkbox(
            "确认删除所选成交记录",
            value=False,
            key=f"live_trade_delete_confirm_{selected_id}",
        )
        if st.button("删除所选记录", type="secondary", disabled=not confirmed):
            try:
                if delete_trade(int(selected_id)):
                    st.success("记录已删除。")
                    st.rerun()
                else:
                    st.error("记录不存在或已经删除。")
            except ValueError as exc:
                st.error(str(exc))


__all__ = [
    "render_live_trade_details",
    "render_live_trade_form",
    "render_live_trade_summary",
]
