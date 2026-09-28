"""Rotation monitor tab: holdings outside BK1158 top-20 and new top-20 names not yet bought."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from components.microcap_live.dashboard import _market_open
from services.market_calendar import get_market_window, latest_settled_trade_date
from services.microcap import fetch_microcap_stocks, load_microcap_constituent_snapshots
from services.microcap_live_rotation import (
    HISTORY_DAYS,
    TOP_N,
    build_rotation_monitor,
    compare_with_holdings,
    holdings_as_of,
    rank_realtime,
)
from services.microcap_live_trading import list_microcap_position_adjustments, list_microcap_trades

TZ = ZoneInfo("Asia/Shanghai")
REALTIME_STATE_KEY = "microcap_live_rotation_realtime"


def _cap(value):
    return "-" if value is None or pd.isna(value) else f"{float(value):,.2f}"


def _gap(value):
    return "-" if value is None or pd.isna(value) else f"{float(value):+.2f}%"


def _days(value):
    return "-" if value is None or pd.isna(value) else f"{int(value)}"


def _out_display(frame: pd.DataFrame, *, streak: bool) -> pd.DataFrame:
    table = pd.DataFrame({
        "代码": frame["code"], "名称": frame["name"], "当前排名": frame["rank_label"],
        "总市值(亿元)": frame["market_cap"].map(_cap), "较第20名市值": frame["gap_to_cutoff_pct"].map(_gap),
        "买入后交易日": frame["holding_days"].map(_days),
    })
    if streak:
        table["连续掉出天数"] = frame["streak_days"].map(_days)
    return table


def _new_display(frame: pd.DataFrame, *, streak: bool) -> pd.DataFrame:
    table = pd.DataFrame({
        "代码": frame["code"], "名称": frame["name"], "排名": frame["rank"].map(lambda v: f"第{int(v)}名"),
        "总市值(亿元)": frame["market_cap"].map(_cap), "较第20名市值": frame["gap_to_cutoff_pct"].map(_gap),
    })
    if streak:
        table["连续在前20天数"] = frame["streak_days"].map(_days)
    return table


def _render_pair(out: pd.DataFrame, new: pd.DataFrame, *, streak: bool, key: str) -> None:
    # Stacked full-width tables: side-by-side columns were too narrow and forced horizontal scrolling.
    st.markdown(f"**持仓已掉出前{TOP_N}（{len(out)}只）**")
    if out.empty:
        st.caption("无：全部持仓仍在前20名内。")
    else:
        st.dataframe(_out_display(out, streak=streak), hide_index=True, width="stretch", key=f"{key}_out")
    st.markdown(f"**新进前{TOP_N}未买入（{len(new)}只）**")
    if new.empty:
        st.caption("无：前20名均已持有。")
    else:
        st.dataframe(_new_display(new, streak=streak), hide_index=True, width="stretch", key=f"{key}_new")


def _load_snapshots() -> pd.DataFrame:
    frame, _ = load_microcap_constituent_snapshots()
    return frame


def _render_realtime(trades, adjustments, now: datetime) -> None:
    st.markdown("#### 盘中实时排名")
    is_open = _market_open(now)
    if st.button("获取盘中实时排名", key="microcap_live_rotation_fetch", disabled=not is_open,
                 help="临时抓取东方财富BK1158实时市值并排名；不写入收盘快照。"):
        try:
            stocks = fetch_microcap_stocks(page_size=400, retries=1)
            st.session_state[REALTIME_STATE_KEY] = dict(
                ranked=rank_realtime(stocks, now.date().isoformat()), fetched_at=now.strftime("%Y-%m-%d %H:%M:%S"),
                quote_time=stocks.attrs.get("quote_time"), source_hosts=list(stocks.attrs.get("source_hosts") or []),
                delayed=bool(stocks.attrs.get("delayed")),
            )
        except Exception as exc:
            st.error(f"实时排名获取失败：{exc}")
    if not is_open:
        st.caption("非交易时段不提供实时排名；以最近一次收盘快照为准。")
        return
    state = st.session_state.get(REALTIME_STATE_KEY)
    if not state or not str(state.get("fetched_at", "")).startswith(now.date().isoformat()):
        st.caption("点击按钮后显示实时对比结果。")
        return
    today = now.date().isoformat()
    result = compare_with_holdings(state["ranked"], holdings_as_of(trades, adjustments, today), as_of=today)
    cutoff = f"{result['cutoff']:,.2f}亿元" if result["cutoff"] else "-"
    kind = "延时行情" if state.get("delayed") else "实时"
    hosts = "、".join(state.get("source_hosts") or []) or "-"
    st.caption(
        f"{kind}，未保存｜行情时间 {state.get('quote_time') or '-'}｜抓取时间 {state['fetched_at']}｜数据源 {hosts}｜"
        f"第20名市值 {cutoff}｜盘中市值随价格变动，边界附近的股票可能反复进出。"
    )
    if state.get("delayed"):
        st.warning("实时接口均未连通，本次数据来自东方财富延时行情（push2delay），排名可能滞后于盘面。")
    _render_pair(result["out"], result["new"], streak=False, key="microcap_live_rotation_rt")


def render_microcap_rotation_monitor() -> None:
    st.subheader(f"BK1158 前{TOP_N}名轮动监控")
    now = datetime.now(TZ)
    trades = list_microcap_trades()
    adjustments = list_microcap_position_adjustments()
    monitor = build_rotation_monitor(_load_snapshots(), trades, adjustments)
    if monitor["latest_date"] is None:
        st.warning("缺少BK1158收盘成分快照，暂不能比较排名。")
        return
    target = latest_settled_trade_date(get_market_window("A股"), now).isoformat()
    if monitor["latest_date"] < target:
        st.warning(f"最新收盘快照为 {monitor['latest_date']}，早于最近收盘日 {target}；请检查快照定时任务。")
    cutoff = f"{monitor['cutoff']:,.2f}亿元" if monitor["cutoff"] else "-"
    st.caption(
        f"收盘快照 {monitor['latest_date']}（采集于 {monitor['snapshot_time']}）｜第20名市值 {cutoff}｜"
        "口径同ABCD模拟：剔除ST与停牌，按总市值升序。持仓按当日收盘后的账本计算。"
    )
    _render_pair(monitor["out"], monitor["new"], streak=True, key="microcap_live_rotation_close")

    st.divider()
    _render_realtime(trades, adjustments, now)

    st.divider()
    history = monitor["history"]
    with st.expander(f"近{HISTORY_DAYS}个交易日明细（{len(history)}条）"):
        if history.empty:
            st.caption("首次持仓以来各快照日均无掉出或未买入的股票。")
        else:
            display = pd.DataFrame({
                "日期": history["date"], "类型": history["type"], "代码": history["code"], "名称": history["name"],
                "排名": history["rank_label"], "总市值(亿元)": history["market_cap"].map(_cap),
                "较第20名市值": history["gap_to_cutoff_pct"].map(_gap),
            })
            st.dataframe(display, hide_index=True, width="stretch", key="microcap_live_rotation_history")
            st.download_button(
                "导出明细CSV", display.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
                file_name="微盘实盘轮动监控.csv", mime="text/csv", key="microcap_live_rotation_export",
            )


__all__ = ["render_microcap_rotation_monitor"]
