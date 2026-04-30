from datetime import datetime
import html

import pandas as pd
import streamlit as st

from core.cache import load_dataset
from core.db import init_db
from services.index_ma20 import build_summary
from services.update_tasks import run_index_ma20_update


st.set_page_config(page_title="指数监控", layout="wide")
init_db()

st.title("指数监控")

if "index_auto_update_done" not in st.session_state:
    st.session_state.index_auto_update_done = False


def format_update_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value.replace("T", " ")


def format_number(value) -> str:
    if pd.isna(value):
        return "-"
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}".rstrip("0").rstrip(".")
    return str(value)


def centered_table(df: pd.DataFrame) -> None:
    headers = "".join(f"<th>{html.escape(str(col))}</th>" for col in df.columns)
    rows = []
    for _, row in df.iterrows():
        cells = "".join(f"<td>{html.escape(format_number(row[col]))}</td>" for col in df.columns)
        rows.append(f"<tr>{cells}</tr>")
    st.markdown(
        f"""
        <style>
        .centered-summary-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.92rem;
        }}
        .centered-summary-table th,
        .centered-summary-table td {{
            text-align: center;
            padding: 0.45rem 0.6rem;
            border-bottom: 1px solid rgba(49, 51, 63, 0.12);
            white-space: nowrap;
        }}
        .centered-summary-table th {{
            font-weight: 600;
            background: rgba(49, 51, 63, 0.04);
        }}
        </style>
        <table class="centered-summary-table">
            <thead><tr>{headers}</tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
        """,
        unsafe_allow_html=True,
    )

with st.sidebar:
    st.subheader("更新设置")
    api_key = st.text_input(
        "API Key",
        value="",
        type="password",
        placeholder="可选；留空使用免费历史数据或环境变量",
    )
    days = st.number_input("展示最近天数", min_value=10, max_value=365, value=30, step=5)
    force_refresh = st.checkbox("强制重新获取", value=False)
    update_clicked = st.button("更新指数 MA20 数据", type="primary")

if update_clicked:
    progress = st.progress(0)
    status_box = st.empty()

    def show_progress(index_name: str, idx: int, total: int, status: str) -> None:
        if status == "running":
            progress.progress((idx - 1) / total)
            status_box.info(f"正在获取 {index_name} ({idx}/{total})...")
        elif status == "success":
            progress.progress(idx / total)
            status_box.success(f"{index_name} 获取完成")
        elif status == "empty":
            progress.progress(idx / total)
            status_box.warning(f"{index_name} 无数据")
        else:
            progress.progress(idx / total)
            status_box.warning(f"{index_name} 获取失败")

    result = run_index_ma20_update(
        api_key=api_key,
        days=int(days),
        cache_source="auto",
        use_fresh_cache=not force_refresh,
        progress_callback=show_progress,
    )
    if result.status == "success":
        if result.errors:
            st.warning(result.message)
        else:
            st.success(result.message)
        st.rerun()
    else:
        st.error(result.message)

if not update_clicked and not st.session_state.index_auto_update_done:
    st.session_state.index_auto_update_done = True
    with st.spinner("正在检查今日指数数据..."):
        auto_result = run_index_ma20_update(
            api_key="",
            days=int(days),
            cache_source="auto",
            use_fresh_cache=True,
        )
    if auto_result.status == "success":
        st.rerun()
    else:
        st.warning(auto_result.message)

report_df = None
for source in ("auto", "manual"):
    report_df, meta = load_dataset(
        "index_ma20_latest",
        source,
        "index_ma20_report",
    )
    if report_df is not None:
        st.caption(f"更新时间：{format_update_time(meta['last_update_time'])}")
        break

if report_df is not None:
    summary_df = build_summary(report_df)
    if not summary_df.empty:
        summary_date = summary_df["日期"].max()
        st.subheader(f"最新摘要 · {summary_date}")
        metric_cols = st.columns(min(4, len(summary_df)))
        for idx, row in summary_df.iterrows():
            with metric_cols[idx % len(metric_cols)]:
                st.metric(
                    label=f"{row['指数']}  {row['代码']}",
                    value=f"{row['收盘价']:.2f}",
                    delta=f"{row['当日涨跌幅(%)']:+.2f}%",
                    delta_color="inverse",
                )

        display_summary_df = summary_df.drop(columns=["代码", "日期", "前收盘价"], errors="ignore")
        centered_table(display_summary_df)

    with st.expander("查看完整分列数据", expanded=False):
        st.dataframe(report_df, use_container_width=True, hide_index=True)
else:
    st.info("还没有缓存数据。可以先点击左侧按钮自动更新，或上传已有 CSV。")
