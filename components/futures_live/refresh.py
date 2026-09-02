"""期货实盘月结单同步、正式行情刷新和会话自动补齐。"""

from __future__ import annotations

from dataclasses import dataclass
import os

import pandas as pd
import streamlit as st

from components.futures_live.formatting import has_unresolved_price_gaps
from services import futures_live_trading as futures_live
from services import futures_spread


@dataclass(frozen=True)
class FuturesLiveRefreshState:
    api_key: str
    force_close_refresh: bool


def render_data_update() -> FuturesLiveRefreshState:
    st.subheader("数据更新")
    source_columns = st.columns([4, 1, 1])
    with source_columns[0]:
        statement_dir = st.text_input(
            "月结单目录",
            value=str(futures_live.configured_statement_dir()),
            help="只读取匹配券商月结单命名的 .xls/.xlsx 文件，不修改源文件。",
        )
    with source_columns[1]:
        st.write("")
        force_statement_sync = st.button("重新同步月结单", width="stretch")
    with source_columns[2]:
        st.write("")
        force_close_refresh = st.button("强制更新收盘/结算价", width="stretch")

    try:
        sync_result = futures_live.sync_statements(
            statement_dir,
            force=force_statement_sync,
        )
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
        with st.spinner("正在补齐全部成交合约历史行情及当前持仓结算价..."):
            history_refresh = futures_live.update_traded_contract_daily_closes(
                api_key=api_key,
                force=True,
            )
            settlement_history_refresh = (
                futures_live.update_traded_contract_daily_settlements(force=True)
            )
            refresh_result = futures_live.update_position_daily_closes(
                api_key=api_key,
                force=True,
            )
        close_refresh_errors = list(
            dict.fromkeys([*history_refresh["errors"], *refresh_result["errors"]])
        )
        st.session_state["futures_live_close_errors"] = close_refresh_errors
        st.session_state["futures_live_settlement_errors"] = list(
            dict.fromkeys(
                [
                    *settlement_history_refresh["errors"],
                    *refresh_result.get("settlement_errors", []),
                ]
            )
        )
        st.session_state["futures_live_settlement_conflicts"] = (
            settlement_history_refresh["conflicts"]
        )
        st.session_state["futures_live_close_target"] = refresh_result["target_date"]
        if close_refresh_errors:
            st.warning("部分合约更新失败，已保留原有正式收盘数据。")
        else:
            st.success(
                f"正式行情已更新，新增 {history_refresh['updated'] + refresh_result['updated']} 条收盘记录、"
                f"{settlement_history_refresh['updated'] + refresh_result.get('settlement_updated', 0)} 条结算记录。"
            )
    return FuturesLiveRefreshState(
        api_key=api_key,
        force_close_refresh=force_close_refresh,
    )


def render_refresh_status(
    close_daily_pnl: pd.DataFrame,
    settlement_daily_pnl: pd.DataFrame,
) -> None:
    if not has_unresolved_price_gaps(close_daily_pnl):
        st.session_state["futures_live_close_errors"] = []
    if not has_unresolved_price_gaps(settlement_daily_pnl):
        st.session_state["futures_live_settlement_errors"] = []

    close_errors = st.session_state.get("futures_live_close_errors", [])
    if close_errors:
        st.error("正式收盘价未全部更新：" + "；".join(close_errors))
    settlement_errors = st.session_state.get("futures_live_settlement_errors", [])
    if settlement_errors:
        display_errors = [
            (
                "铁矿石期权历史结算价：大商所匿名旧接口已停用并返回 412；"
                "新版门户 API 需要已注册用户凭据，当前未配置"
                if "412 Client Error" in str(message)
                and "dayQuotes" in str(message)
                else str(message)
            )
            for message in settlement_errors
        ]
        st.warning("正式结算价尚未全部更新：" + "；".join(display_errors))
        daily_overrides = futures_live.list_futures_daily_pnl_overrides()
        pending_manual = (
            daily_overrides[
                daily_overrides["reconciliation_status"].isin(["待确认", "采用手工"])
            ]
            if not daily_overrides.empty
            else daily_overrides
        )
        if not pending_manual.empty:
            st.caption(
                "缺失日期已保留同花顺账户级日盈亏："
                + "、".join(
                    sorted(pending_manual["trade_date"].astype(str).unique())
                )
                + "；收益日历不会留空，但这些值不生成或伪装成单合约正式结算价。"
            )
    settlement_conflicts = st.session_state.get(
        "futures_live_settlement_conflicts", []
    )
    if settlement_conflicts:
        st.warning("结算价核对发现差异，已保留原正式值：" + "；".join(settlement_conflicts))


def run_session_auto_refresh(
    account: pd.Series | dict[str, object],
    refresh_state: FuturesLiveRefreshState,
) -> None:
    """保持原页面末尾的两段一次性自动更新及其先后顺序。"""
    target_date = futures_spread.completed_futures_daily_cutoff().strftime("%Y-%m-%d")
    history_auto_key = "futures_live_history_close_target"
    history_auto_value = f"{account['statement_month']}|{target_date}"
    if (
        st.session_state.get(history_auto_key) != history_auto_value
        and not refresh_state.force_close_refresh
    ):
        st.session_state[history_auto_key] = history_auto_value
        with st.spinner("正在补齐全部历史成交合约的正式收盘价和结算价..."):
            history_auto_result = futures_live.update_traded_contract_daily_closes(
                api_key=refresh_state.api_key
            )
            settlement_history_auto_result = (
                futures_live.update_traded_contract_daily_settlements()
            )
        st.session_state["futures_live_history_close_errors"] = history_auto_result[
            "errors"
        ]
        st.session_state["futures_live_history_settlement_errors"] = (
            settlement_history_auto_result["errors"]
        )
        st.session_state["futures_live_settlement_conflicts"] = (
            settlement_history_auto_result["conflicts"]
        )
        if (
            history_auto_result["updated"] > 0
            or settlement_history_auto_result["updated"] > 0
            or history_auto_result["errors"]
            or settlement_history_auto_result["errors"]
            or settlement_history_auto_result["conflicts"]
        ):
            st.rerun()

    auto_key = "futures_live_auto_close_target"
    if (
        st.session_state.get(auto_key) != target_date
        and not refresh_state.force_close_refresh
    ):
        st.session_state[auto_key] = target_date
        with st.spinner(f"正在补齐截至 {target_date} 的正式收盘价和结算价..."):
            auto_result = futures_live.update_position_daily_closes(
                api_key=refresh_state.api_key
            )
        st.session_state["futures_live_close_errors"] = list(
            dict.fromkeys(
                [
                    *st.session_state.get("futures_live_history_close_errors", []),
                    *auto_result["errors"],
                ]
            )
        )
        st.session_state["futures_live_settlement_errors"] = list(
            dict.fromkeys(
                [
                    *st.session_state.get(
                        "futures_live_history_settlement_errors", []
                    ),
                    *auto_result.get("settlement_errors", []),
                ]
            )
        )
        st.session_state["futures_live_close_target"] = auto_result["target_date"]
        if (
            auto_result["updated"] > 0
            or auto_result.get("settlement_updated", 0) > 0
            or auto_result["errors"]
            or auto_result.get("settlement_errors", [])
        ):
            st.rerun()
