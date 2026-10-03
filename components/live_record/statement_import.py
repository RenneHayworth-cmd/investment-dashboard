"""ETF实盘交割单导入：上传、预览，二次确认后写入。"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from services.live_statement_import import (
    commit_statement_import,
    describe_takeovers,
    preview_statement_import,
)

PREVIEW_KEY = "live_statement_import_preview"


def _money(value: object) -> str:
    return "-" if value is None or pd.isna(value) else f"{float(value):,.2f}"


def render_statement_import() -> None:
    st.caption(
        "上传券商交割单重建 ETF实盘账本：华宝需同时上传交割单和资金明细，银河只需交割单。"
        "A股个股归微盘实盘，不会导入；同一券商重新导入只替换新文件日期范围内的交割单记录，"
        "与交割单完全对应的手工记录由交割单接管并保留策略说明和备注。"
    )
    uploads = st.file_uploader(
        "交割单文件（可多选）",
        type=["xls", "xlsx", "txt", "csv"],
        accept_multiple_files=True,
        key="live_statement_import_files",
    )
    if st.button("预览导入", disabled=not uploads, key="live_statement_import_preview_button"):
        preview = preview_statement_import([(item.name, item.getvalue()) for item in uploads])
        if preview["ok"]:
            preview["takeovers"] = describe_takeovers(preview["imports"])
        st.session_state[PREVIEW_KEY] = preview

    preview = st.session_state.get(PREVIEW_KEY)
    if not preview:
        return
    for error in preview["errors"]:
        st.error(error)
    if not preview["imports"]:
        return
    rows = []
    for item in preview["imports"]:
        difference = item.cash_difference()
        rows.append(
            {
                "券商": item.label,
                "日期范围": f"{item.start} 至 {item.end}",
                "成交笔数": f"{len(item.trades)}",
                "资金流水笔数": f"{len(item.flows)}",
                "跳过微盘个股": f"{item.skipped_stock_rows}",
                "期末现金": _money(item.broker_end_cash),
                "未到期逆回购": _money(item.open_repo_principal),
                "现金核对差额": _money(difference),
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    for item in preview["imports"]:
        for warning in item.warnings:
            st.warning(f"{item.label}：{warning}")
        if item.ignored_operations:
            st.caption(
                f"{item.label}未入账的操作："
                + "、".join(f"{name} {count} 笔" for name, count in item.ignored_operations.items())
            )
        difference = item.cash_difference()
        if difference is not None and abs(difference) > 0.05:
            st.warning(f"{item.label}现金核对差额 {difference:,.2f} 元，请检查是否有未识别的资金流水。")
    takeovers = preview.get("takeovers") or {}
    if takeovers:
        st.write(
            f"将接管手工成交 {len(takeovers['taken_trade_ids'])} 笔、手工流水 {len(takeovers['taken_flow_ids'])} 笔。"
        )
        pending_trades, pending_flows = takeovers["pending_trades"], takeovers["pending_flows"]
        if not pending_trades.empty or not pending_flows.empty:
            st.warning("以下手工记录在交割单日期范围内但没有对应交割单行，将原样保留，请核对后决定是否删除。")
            if not pending_trades.empty:
                st.dataframe(
                    pending_trades[["trade_date", "symbol", "name", "side", "price", "quantity", "notes"]].rename(
                        columns={"trade_date": "日期", "symbol": "代码", "name": "名称", "side": "方向",
                                 "price": "价格", "quantity": "数量", "notes": "备注"}
                    ),
                    hide_index=True,
                    width="stretch",
                )
            if not pending_flows.empty:
                st.dataframe(
                    pending_flows[["flow_date", "entry_type", "amount", "notes"]].rename(
                        columns={"flow_date": "日期", "entry_type": "类型", "amount": "金额", "notes": "备注"}
                    ),
                    hide_index=True,
                    width="stretch",
                )
    if preview["ok"] and st.button("确认导入", type="primary", key="live_statement_import_commit"):
        try:
            result = commit_statement_import(preview["imports"])
        except Exception as exc:
            st.error(f"导入失败，账本未修改：{exc}")
            return
        st.session_state.pop(PREVIEW_KEY, None)
        st.success(
            f"已导入成交 {result['inserted_trades']} 笔、资金流水 {result['inserted_flows']} 笔；"
            f"替换旧交割单记录 {result['replaced_statement_records']} 笔，接管手工记录 "
            f"{result['taken_over_trades'] + result['taken_over_flows']} 笔。"
        )


__all__ = ["render_statement_import"]
