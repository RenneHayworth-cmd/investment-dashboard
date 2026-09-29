"""Manual trade, cash-flow, adjustment, and fee-setting forms."""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from services.microcap_live_trading import (
    ADJUSTMENT_TYPES,
    FLOW_TYPES,
    add_microcap_cash_flow,
    add_microcap_position_adjustment,
    add_microcap_trade,
    delete_microcap_cash_flow,
    delete_microcap_position_adjustment,
    delete_microcap_trade,
    get_microcap_fee_settings,
    list_microcap_cash_flows,
    list_microcap_position_adjustments,
    list_microcap_trades,
    update_microcap_fee_settings,
)

TZ = ZoneInfo("Asia/Shanghai")


def _render_delete_selector(frame, *, key, id_column, label_builder, delete_fn):
    if frame is None or frame.empty:
        return
    with st.expander("删除误录记录"):
        labels = {int(row[id_column]): label_builder(row) for _, row in frame.iterrows()}
        selected = st.selectbox("选择记录", list(labels), format_func=labels.get, key=f"{key}_selected")
        confirmed = st.checkbox("确认删除所选记录", key=f"{key}_confirm_{selected}")
        if st.button("删除", key=f"{key}_delete", disabled=not confirmed):
            try:
                delete_fn(selected)
                st.success("记录已删除。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))


TRADE_KEY = "microcap_live_trade_"
TRADE_FIELDS = ("date", "side", "symbol", "name", "price", "quantity", "commission_买入", "commission_卖出", "other_fee", "strategy", "notes")


def _save_trade_entry() -> None:
    state = st.session_state
    side = state[f"{TRADE_KEY}side"]
    try:
        add_microcap_trade(
            trade_date=state[f"{TRADE_KEY}date"], trade_time=None,
            symbol=state[f"{TRADE_KEY}symbol"], name=state[f"{TRADE_KEY}name"], side=side,
            price=state[f"{TRADE_KEY}price"], quantity=int(state[f"{TRADE_KEY}quantity"]),
            commission_amount=state[f"{TRADE_KEY}commission_{side}"],
            other_fee_amount=state[f"{TRADE_KEY}other_fee"],
            strategy=state[f"{TRADE_KEY}strategy"], notes=state[f"{TRADE_KEY}notes"],
        )
    except ValueError as exc:
        state[f"{TRADE_KEY}error"] = str(exc)
        return
    # Callbacks run before widgets are rebuilt, so dropping the keys resets the inputs.
    for field in TRADE_FIELDS:
        state.pop(f"{TRADE_KEY}{field}", None)
    state[f"{TRADE_KEY}saved"] = True


@st.fragment
def _render_trade_entry() -> None:
    # A fragment rather than st.form: the stamp-tax estimate and side-specific
    # commission must follow each input, without rerunning the whole page.
    settings = get_microcap_fee_settings()
    first = st.columns([1.1, 1, 1.6, 1.4])
    first[0].date_input("成交日期", value=datetime.now(TZ).date(), key=f"{TRADE_KEY}date")
    side = first[1].selectbox("方向", ["买入", "卖出"], key=f"{TRADE_KEY}side")
    first[2].text_input("A股代码", placeholder="六位代码，例如 600000", key=f"{TRADE_KEY}symbol")
    first[3].text_input("股票名称", key=f"{TRADE_KEY}name")
    second = st.columns([1.1, 1.1, 1.1, 1.1, 1.1, 1.8])
    price = second[0].number_input("成交价格", min_value=0.0, value=0.0, step=0.01, format="%.3f", key=f"{TRADE_KEY}price")
    quantity = second[1].number_input("成交股数", min_value=0, value=0, step=100, key=f"{TRADE_KEY}quantity")
    default_commission = settings["buy_commission"] if side == "买入" else settings["sell_commission"]
    second[2].number_input(
        "本笔佣金", min_value=0.0, value=float(default_commission), step=1.0, format="%.2f",
        key=f"{TRADE_KEY}commission_{side}",
    )
    estimated_tax = (
        float((Decimal(str(price)) * int(quantity) * Decimal(str(settings["stamp_tax_rate_pct"])) / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        if side == "卖出" else 0.0
    )
    second[3].number_input(
        "过户费等其他费用", min_value=0.0, value=0.0, step=0.01, format="%.2f", key=f"{TRADE_KEY}other_fee",
        help="交割单中佣金、印花税之外的费用，如过户费、其他杂费。",
    )
    second[4].metric("预计印花税", f"{estimated_tax:,.2f}元")
    second[5].text_input("策略/账户标签", placeholder="可选", key=f"{TRADE_KEY}strategy")
    st.text_input("备注", key=f"{TRADE_KEY}notes")
    st.button("保存成交", type="primary", key=f"{TRADE_KEY}submit", on_click=_save_trade_entry)
    error = st.session_state.pop(f"{TRADE_KEY}error", None)
    if error:
        st.error(error)
    if st.session_state.pop(f"{TRADE_KEY}saved", False):
        st.session_state[f"{TRADE_KEY}flash"] = True
        st.rerun()  # refresh the records table and account views outside this fragment


def render_microcap_trades() -> None:
    st.subheader("新增实际成交")
    if st.session_state.pop(f"{TRADE_KEY}flash", False):
        st.success("成交已写入微盘实盘独立账本。")
    _render_trade_entry()

    trades = list_microcap_trades()
    st.subheader("成交记录")
    if trades.empty:
        st.info("暂无成交记录。")
        return
    detail = trades.copy()
    detail["成交金额"] = (pd.to_numeric(detail["price"], errors="coerce") * pd.to_numeric(detail["quantity"], errors="coerce")).round(2)
    detail["总费用"] = (
        pd.to_numeric(detail["commission_amount"], errors="coerce") + pd.to_numeric(detail["stamp_tax_amount"], errors="coerce")
        + pd.to_numeric(detail["other_fee_amount"], errors="coerce").fillna(0)
    ).round(2)
    detail["现金变动"] = detail["成交金额"] + detail["总费用"]
    detail.loc[detail["side"].eq("卖出"), "现金变动"] = detail.loc[detail["side"].eq("卖出"), "成交金额"] - detail.loc[detail["side"].eq("卖出"), "总费用"]
    detail = detail.rename(columns={
        "id": "记录ID", "trade_date": "成交日期", "trade_time": "成交时间", "symbol": "代码", "name": "名称",
        "side": "方向", "price": "成交价", "quantity": "股数", "commission_amount": "佣金",
        "stamp_tax_amount": "印花税", "other_fee_amount": "其他费用", "strategy": "策略/标签", "notes": "备注", "source": "来源",
    })
    # Imported rows may still carry a time; keep it only for intraday ordering.
    detail = detail.sort_values(["成交日期", "成交时间", "记录ID"], ascending=False)
    cols = ["记录ID", "成交日期", "代码", "名称", "方向", "成交价", "股数", "成交金额", "佣金", "印花税", "其他费用", "总费用", "现金变动", "策略/标签", "来源", "备注"]
    st.dataframe(detail[cols], hide_index=True, width="stretch", column_config={"成交价": st.column_config.NumberColumn(format="%.3f")})
    st.download_button(
        "导出成交CSV", detail[cols].to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
        file_name="微盘实盘成交记录.csv", mime="text/csv", key="microcap_live_export_trades",
    )
    _render_delete_selector(
        trades, key="microcap_live_trade", id_column="id",
        label_builder=lambda r: f"#{int(r['id'])}｜{r['trade_date']}｜{r['symbol']} {r['side']} {int(r['quantity'])}股",
        delete_fn=delete_microcap_trade,
    )


def render_microcap_cash_flows() -> None:
    st.subheader("资金流水")
    with st.form("microcap_live_cash_flow_form", clear_on_submit=True):
        cols = st.columns([1.2, 1.4, 1.2, 1.5, 2.2])
        flow_date = cols[0].date_input("流水日期", value=datetime.now(TZ).date())
        entry_type = cols[1].selectbox("类型", list(FLOW_TYPES))
        amount = cols[2].number_input("金额", min_value=0.0, value=0.0, step=1000.0, format="%.2f")
        symbol = cols[3].text_input("关联股票代码", placeholder="仅分红/其他收支可填")
        notes = cols[4].text_input("备注")
        submitted = st.form_submit_button("保存资金流水", type="primary")
    if submitted:
        try:
            add_microcap_cash_flow(
                flow_date=flow_date, flow_time=None,
                entry_type=entry_type, amount=amount, symbol=symbol, notes=notes,
            )
            st.success("资金流水已保存。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    flows = list_microcap_cash_flows()
    if flows.empty:
        st.info("尚无资金流水。请先录入期初资金；如启用时已有股票，也可将期初现金设为0并录入期初持仓。")
    else:
        display = flows.drop(columns=["flow_time"], errors="ignore").rename(columns={"id": "记录ID", "flow_date": "日期", "entry_type": "类型", "amount": "金额", "symbol": "代码", "notes": "备注"})
        st.dataframe(display[["记录ID", "日期", "类型", "金额", "代码", "备注"]], hide_index=True, width="stretch")
        st.download_button("导出资金流水CSV", display.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"), file_name="微盘实盘资金流水.csv", mime="text/csv", key="microcap_live_export_cash")
        _render_delete_selector(
            flows, key="microcap_live_flow", id_column="id",
            label_builder=lambda r: f"#{int(r['id'])}｜{r['flow_date']}｜{r['entry_type']} {float(r['amount']):,.2f}元",
            delete_fn=delete_microcap_cash_flow,
        )


def render_microcap_position_adjustments() -> None:
    st.subheader("期初持仓与送转调整")
    with st.form("microcap_live_adjustment_form", clear_on_submit=True):
        cols = st.columns([1.2, 1.3, 1.1, 1.3, 1.6, 2])
        event_date = cols[0].date_input("生效日期", value=datetime.now(TZ).date())
        adjustment_type = cols[1].selectbox("调整类型", list(ADJUSTMENT_TYPES))
        symbol = cols[2].text_input("A股代码", placeholder="六位代码")
        name = cols[3].text_input("股票名称")
        quantity = cols[4].number_input("增加股数", min_value=0, value=0, step=100)
        cost_basis = cols[5].number_input("期初持仓总成本", min_value=0.0, value=0.0, step=1000.0, format="%.2f", disabled=adjustment_type == "送转")
        notes = st.text_input("备注（如送股/转增比例）")
        submitted = st.form_submit_button("保存持仓调整", type="primary")
    if submitted:
        try:
            add_microcap_position_adjustment(
                event_date=event_date, symbol=symbol, name=name, adjustment_type=adjustment_type,
                quantity_delta=int(quantity), cost_basis_delta=0 if adjustment_type == "送转" else cost_basis,
                notes=notes,
            )
            st.success("持仓调整已保存；送转不会增加持仓总成本。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    adjustments = list_microcap_position_adjustments()
    if adjustments.empty:
        st.info("暂无期初持仓或送转调整。")
    else:
        display = adjustments.rename(columns={"id": "记录ID", "event_date": "生效日期", "symbol": "代码", "name": "名称", "adjustment_type": "类型", "quantity_delta": "增加股数", "cost_basis_delta": "增加成本", "notes": "备注"})
        st.dataframe(display[["记录ID", "生效日期", "代码", "名称", "类型", "增加股数", "增加成本", "备注"]], hide_index=True, width="stretch")
        _render_delete_selector(
            adjustments, key="microcap_live_adjustment", id_column="id",
            label_builder=lambda r: f"#{int(r['id'])}｜{r['event_date']}｜{r['symbol']} {r['adjustment_type']} +{int(r['quantity_delta'])}股",
            delete_fn=delete_microcap_position_adjustment,
        )


def render_microcap_fee_settings() -> None:
    st.subheader("今后成交的默认费用")
    settings = get_microcap_fee_settings()
    with st.form("microcap_live_fee_settings_form"):
        cols = st.columns(3)
        buy = cols[0].number_input("默认买入佣金（元/笔）", min_value=0.0, value=float(settings["buy_commission"]), step=1.0, format="%.2f")
        sell = cols[1].number_input("默认卖出佣金（元/笔）", min_value=0.0, value=float(settings["sell_commission"]), step=1.0, format="%.2f")
        cols[2].metric("卖出印花税", f"万{float(settings['stamp_tax_rate_pct']) * 100:.0f}")
        submitted = st.form_submit_button("保存默认费用")
    if submitted:
        try:
            update_microcap_fee_settings(buy_commission=buy, sell_commission=sell)
            st.success("默认佣金已更新，仅影响之后新增的手工成交。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    st.caption("导入交割单时优先保留文件中的实际佣金和印花税；历史成交费用不会随默认值修改而变化。")


__all__ = [
    "render_microcap_trades", "render_microcap_cash_flows", "render_microcap_position_adjustments",
    "render_microcap_fee_settings",
]
