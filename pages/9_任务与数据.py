import os

import pandas as pd
import streamlit as st

from core.cache import list_datasets
from core.db import init_db, list_jobs
from core.ui import apply_global_style, render_page_header
from services.index_ma20 import INDEX_REPORT_DISPLAY_DAYS
from services.index_realtime import build_pending_index_update_preview
from services.update_tasks import run_index_ma20_update, verify_updated_index_data


st.set_page_config(page_title="任务与数据", layout="wide")
init_db()
apply_global_style()

render_page_header("任务与数据", "手工维护正式指数数据，并查看本地缓存和历史任务记录。", eyebrow="Data")


def _format_times(df: pd.DataFrame, columns: tuple[str, ...]) -> pd.DataFrame:
    result = df.copy()
    for column in columns:
        if column not in result.columns:
            continue
        values = pd.to_datetime(result[column], errors="coerce")
        result[column] = values.dt.strftime("%Y-%m-%d %H:%M:%S").fillna(
            result[column].astype(str).str.replace("T", " ", regex=False)
        )
    return result


st.subheader("手工更新正式指数数据")
st.caption("仅补齐已经完成、但本地正式缓存缺失的交易日；不会抓取或保存盘中卡片报价。")
api_key = st.text_input(
    "TickFlow API Key",
    value=os.getenv("TICKFLOW_API_KEY", ""),
    type="password",
    placeholder="可选；留空使用免费历史数据或环境变量",
)
preview_state_key = "formal_index_update_preview"
verification_state_key = "formal_index_update_verification"


def _preview_signature(preview: pd.DataFrame) -> list[tuple[str, ...]]:
    if preview is None or preview.empty:
        return []
    columns = ("指数", "目标交易日", "缺失交易日", "待更新原因", "主更新源", "复核源")
    return [tuple(str(row[column]) for column in columns) for _, row in preview.iterrows()]


if st.button("更新缺失的正式指数数据", type="primary"):
    preview = build_pending_index_update_preview(tickflow_enabled=bool(api_key.strip()))
    st.session_state[preview_state_key] = preview
    st.session_state.pop(verification_state_key, None)
    if preview.empty:
        st.success("当前没有缺失的正式指数数据。")

preview = st.session_state.get(preview_state_key)
if isinstance(preview, pd.DataFrame) and not preview.empty:
    st.info(f"本次预计更新 {len(preview)} 个指数。以下仅为本地缓存检查结果，尚未联网或写入数据。")
    st.dataframe(preview, use_container_width=True, hide_index=True)
    confirm_column, cancel_column, _ = st.columns([1, 1, 4])
    confirm_update = confirm_column.button("确认更新并复核", type="primary")
    cancel_update = cancel_column.button("取消", key="cancel_formal_index_update")

    if cancel_update:
        st.session_state.pop(preview_state_key, None)
        st.rerun()

    if confirm_update:
        current_preview = build_pending_index_update_preview(tickflow_enabled=bool(api_key.strip()))
        if _preview_signature(current_preview) != _preview_signature(preview):
            st.session_state[preview_state_key] = current_preview
            if current_preview.empty:
                st.success("待更新范围已变化，当前已没有缺失的正式指数数据。")
            else:
                st.warning("待更新清单已变化，已刷新为最新结果。请核对后再次确认。")
        else:
            pending_indexes = set(preview["指数"].astype(str))
            target_dates = dict(zip(preview["指数"].astype(str), preview["目标交易日"].astype(str)))
            progress = st.progress(0)
            status_box = st.empty()

            def show_progress(index_name, index, total, status, elapsed_seconds=None):
                progress.progress(index / total)
                elapsed = "" if elapsed_seconds is None else f"，耗时 {elapsed_seconds:.2f} 秒"
                status_box.info(f"{index_name}：{status}{elapsed}，进度 {index}/{total}")

            result = run_index_ma20_update(
                api_key=api_key,
                days=INDEX_REPORT_DISPLAY_DAYS,
                cache_source="auto",
                use_fresh_cache=False,
                progress_callback=show_progress,
                index_names=pending_indexes,
                max_workers=4,
            )
            status_box.info("正式数据更新已结束，正在通过独立日线源复核目标交易日收盘价……")
            verification = verify_updated_index_data(
                pending_indexes,
                target_dates,
                api_key=api_key,
                max_workers=4,
            )
            st.session_state[verification_state_key] = verification
            st.session_state.pop(preview_state_key, None)
            progress.empty()
            status_box.empty()
            if result.status == "success" and not result.errors:
                st.success(result.message)
            elif result.status == "success":
                st.warning(result.message)
            else:
                st.error(result.message)

verification = st.session_state.get(verification_state_key)
if isinstance(verification, pd.DataFrame) and not verification.empty:
    st.markdown("#### 最近一次更新复核")
    st.caption("偏差按两个来源同一交易日的收盘价计算；0.20%以内标记为一致。复核结果仅供检查，不会覆盖正式缓存。")
    st.dataframe(verification, use_container_width=True, hide_index=True)

st.subheader("数据集")
datasets = _format_times(list_datasets(), ("last_update_time",))
datasets = datasets.rename(
    columns={
        "symbol": "代码",
        "name": "名称",
        "source": "来源",
        "data_type": "数据类型",
        "period": "周期",
        "last_trade_date": "最新交易日",
        "last_update_time": "更新时间",
        "row_count": "行数",
        "status": "状态",
        "file_path": "文件路径",
    }
)
st.dataframe(datasets, use_container_width=True, hide_index=True)

st.subheader("任务记录")
jobs = _format_times(list_jobs(), ("started_at", "finished_at"))
jobs = jobs.rename(
    columns={
        "id": "任务编号",
        "job_name": "任务名称",
        "status": "状态",
        "started_at": "开始时间",
        "finished_at": "结束时间",
        "message": "说明",
    }
)
st.dataframe(jobs, use_container_width=True, hide_index=True)
