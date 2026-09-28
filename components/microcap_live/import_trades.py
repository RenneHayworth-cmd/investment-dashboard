"""CSV/XLS/XLSX import preview and atomic commit UI."""
import hashlib

import pandas as pd
import streamlit as st

from services.microcap_live_trading import (
    commit_microcap_trade_import,
    delete_microcap_import_batch,
    inspect_microcap_import_columns,
    list_microcap_import_batches,
    preview_microcap_trade_import,
)

FIELD_LABELS = {
    "trade_date": "成交日期（必填）", "symbol": "股票代码（必填）", "name": "股票名称",
    "side": "买卖方向（必填）", "price": "成交价格（必填）", "quantity": "成交数量（必填）",
    "trade_time": "成交时间", "commission_amount": "佣金", "stamp_tax_amount": "印花税",
    "total_fee": "总费用", "strategy": "策略/标签", "notes": "备注",
}
ALIASES = {
    "trade_date": ("成交日期", "日期", "发生日期", "交收日期", "成交时间"),
    "symbol": ("证券代码", "股票代码", "代码", "证券编号"),
    "name": ("证券名称", "股票名称", "名称"),
    "side": ("买卖标志", "买卖方向", "操作", "方向", "业务名称"),
    "price": ("成交价格", "成交均价", "价格", "成交价"),
    "quantity": ("成交数量", "成交股数", "数量", "发生数量"),
    "trade_time": ("成交时间", "时间"),
    "commission_amount": ("佣金", "交易佣金", "手续费"),
    "stamp_tax_amount": ("印花税", "印花税额"),
    "total_fee": ("总费用", "费用合计", "交易费用", "手续费合计"),
    "strategy": ("策略", "标签"),
    "notes": ("备注", "说明"),
}


def _default_column(field, headers):
    for alias in ALIASES.get(field, ()):
        for header in headers:
            if alias == header or alias in header:
                if field == "trade_time" and alias == "成交时间" and "成交日期" in header:
                    continue
                return header
    return "（不导入）"


def render_microcap_import() -> None:
    st.subheader("导入券商交割单")
    st.caption("支持CSV、XLS、XLSX。先映射字段并预览；存在错误行或账本余额/持仓校验失败时，整批不写入。")
    uploaded = st.file_uploader("选择成交文件", type=["csv", "xls", "xlsx"], key="microcap_live_import_file")
    if uploaded is not None:
        raw = uploaded.getvalue()
        file_name = uploaded.name
        file_hash = hashlib.sha256(raw).hexdigest()
        try:
            inspected = inspect_microcap_import_columns(raw, file_name)
            headers = inspected["columns"]
        except Exception as exc:
            st.error(f"文件读取失败：{exc}")
            headers = []
        if headers:
            form_key = f"microcap_live_import_mapping_{file_hash[:10]}"
            with st.form(form_key):
                st.markdown("**字段映射**")
                fields = list(FIELD_LABELS)
                columns = st.columns(3)
                mapping = {}
                for index, field in enumerate(fields):
                    choice = columns[index % 3].selectbox(
                        FIELD_LABELS[field], ["（不导入）"] + headers,
                        index=(headers.index(_default_column(field, headers)) + 1) if _default_column(field, headers) in headers else 0,
                        key=f"{form_key}_{field}",
                    )
                    if choice != "（不导入）":
                        mapping[field] = choice
                make_preview = st.form_submit_button("生成导入预览", type="primary")
            if make_preview:
                preview = preview_microcap_trade_import(raw, file_name, mapping)
                st.session_state["microcap_live_import_preview"] = preview
            preview = st.session_state.get("microcap_live_import_preview")
            if preview and preview.get("file_hash") == file_hash:
                if preview.get("duplicate"):
                    st.warning("该文件已导入，系统不会重复写入。")
                elif preview.get("errors"):
                    for error in preview["errors"]:
                        st.error(error)
                elif preview.get("ok"):
                    st.success(f"预览通过：{len(preview['rows'])}笔成交，费用优先采用文件实际值。")
                    for item in preview.get("ignored_rows", []):
                        st.warning(
                            f"第{item['source_row']}行“{item['description']}”是现金分红，不会作为成交导入；"
                            "请在“资金与权益事件”中单独登记。"
                        )
                    table = pd.DataFrame(preview["rows"])
                    table["gross_amount"] = (table["price"] * table["quantity"]).round(2)
                    table["total_fee"] = table["commission_amount"] + table["stamp_tax_amount"]
                    st.dataframe(
                        table.rename(columns={"source_row": "源行号", "trade_date": "成交日期", "trade_time": "成交时间", "symbol": "代码", "name": "名称", "side": "方向", "price": "价格", "quantity": "数量", "gross_amount": "成交金额", "commission_amount": "佣金", "stamp_tax_amount": "印花税", "total_fee": "总费用", "strategy": "策略/标签", "notes": "备注"}),
                        hide_index=True, width="stretch",
                    )
                    if st.button("确认导入整批成交", type="primary", key=f"microcap_live_import_commit_{file_hash[:10]}"):
                        try:
                            result = commit_microcap_trade_import(preview)
                            st.success(f"已导入{result['row_count']}笔成交。")
                            st.session_state.pop("microcap_live_import_preview", None)
                            st.rerun()
                        except ValueError as exc:
                            st.error(f"导入已回滚：{exc}")

    batches = list_microcap_import_batches()
    st.subheader("导入批次")
    if batches.empty:
        st.info("暂无导入批次。")
    else:
        display = batches.rename(columns={"id": "批次", "file_name": "文件名", "file_size": "字节数", "imported_at": "导入时间", "row_count": "成交笔数"})
        st.dataframe(display[["批次", "文件名", "字节数", "导入时间", "成交笔数"]], hide_index=True, width="stretch")
        with st.expander("删除整批导入记录"):
            labels = {int(row.id): f"#{int(row.id)}｜{row.file_name}｜{int(row.row_count)}笔" for row in batches.itertuples(index=False)}
            selected = st.selectbox("选择导入批次", list(labels), format_func=labels.get, key="microcap_live_import_batch_delete_select")
            confirmed = st.checkbox("确认删除该批次及其成交", key=f"microcap_live_import_batch_delete_confirm_{selected}")
            if st.button("删除导入批次", disabled=not confirmed, key="microcap_live_import_batch_delete"):
                try:
                    delete_microcap_import_batch(selected)
                    st.success("导入批次已删除。")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))


__all__ = ["render_microcap_import"]
