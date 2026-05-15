from datetime import datetime
import html
import os
import subprocess
import sys

import pandas as pd
import streamlit as st

from core.cache import load_dataset
from core.db import init_db
from core.paths import OUTPUT_DIR, ensure_dirs
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


def is_today_cache(meta: dict | None) -> bool:
    last_update_time = (meta or {}).get("last_update_time")
    if not last_update_time:
        return False
    try:
        return datetime.fromisoformat(last_update_time).date() == datetime.now().date()
    except ValueError:
        return False


def start_background_update_once(api_key: str, days: int) -> bool:
    ensure_dirs()
    log_path = OUTPUT_DIR / "index_auto_update.log"
    cmd = [
        sys.executable,
        "-m",
        "services.background_updater",
        "--once",
        "--days",
        str(days),
    ]
    if api_key:
        cmd.extend(["--api-key", api_key])
    try:
        with log_path.open("ab") as log_file:
            subprocess.Popen(
                cmd,
                cwd=str(OUTPUT_DIR.parent),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return True
    except Exception:
        return False


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


def render_index_cards(summary_df: pd.DataFrame) -> None:
    cards = []
    for _, row in summary_df.iterrows():
        delta = pd.to_numeric(row["当日涨跌幅(%)"], errors="coerce")
        delta_class = "positive" if delta >= 0 else "negative"
        arrow = "↑" if delta >= 0 else "↓"
        cards.append(
            '<div class="index-card">'
            f'<div class="index-card-title">{html.escape(str(row["指数"]))}</div>'
            f'<div class="index-card-code">{html.escape(str(row["代码"]))}</div>'
            f'<div class="index-card-value">{float(row["收盘价"]):.2f}</div>'
            f'<div class="index-card-delta {delta_class}">'
            f"<span>{arrow}</span>"
            f"<span>{delta:+.2f}%</span>"
            "</div>"
            "</div>"
        )

    st.markdown(
        "<style>"
        ".index-card-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1rem;margin:0.4rem 0 1rem;}"
        ".index-card{min-height:12.25rem;border:1px solid rgba(49,51,63,.14);border-radius:8px;background:rgba(255,255,255,.78);padding:1.35rem 1.5rem;box-shadow:0 10px 26px rgba(15,23,42,.06);}"
        ".index-card-title{min-height:2.4rem;color:rgba(49,51,63,.72);font-size:1.18rem;font-weight:700;line-height:1.25;overflow-wrap:anywhere;}"
        ".index-card-code{min-height:1.65rem;margin-top:.22rem;color:rgba(49,51,63,.58);font-size:1rem;line-height:1.25;overflow-wrap:anywhere;}"
        ".index-card-value{margin-top:1.05rem;color:rgb(31,41,55);font-size:1.85rem;font-weight:650;line-height:1.08;font-variant-numeric:tabular-nums;white-space:normal;overflow-wrap:anywhere;}"
        ".index-card-delta{display:inline-flex;align-items:center;gap:.35rem;margin-top:1.05rem;border-radius:999px;padding:.35rem .85rem;font-size:1.05rem;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap;}"
        ".index-card-delta.positive{color:rgb(190,18,60);background:rgba(254,226,226,.9);}"
        ".index-card-delta.negative{color:rgb(22,101,52);background:rgba(220,252,231,.9);}"
        "@media (max-width:980px){.index-card-grid{grid-template-columns:repeat(2,minmax(0,1fr));}}"
        "@media (max-width:640px){.index-card-grid{grid-template-columns:1fr;}}"
        "</style>"
        f'<div class="index-card-grid">{"".join(cards)}</div>',
        unsafe_allow_html=True,
    )


with st.sidebar:
    st.subheader("更新设置")
    api_key = st.text_input(
        "API Key",
        value=os.getenv("TICKFLOW_API_KEY", ""),
        type="password",
        placeholder="可选；留空使用免费历史数据或环境变量",
    )
    days = st.number_input("展示最近天数", min_value=10, max_value=365, value=30, step=5)
    max_workers = st.number_input("并发数", min_value=1, max_value=8, value=4, step=1)
    force_refresh = st.checkbox("强制重新获取", value=False)
    update_clicked = st.button("更新指数数据", type="primary")

if update_clicked:
    progress = st.progress(0)
    status_box = st.empty()

    def show_progress(index_name: str, idx: int, total: int, status: str) -> None:
        if status == "success":
            progress.progress(idx / total)
            status_box.success(f"{index_name} 获取完成，进度 {idx}/{total}")
        elif status == "empty":
            progress.progress(idx / total)
            status_box.warning(f"{index_name} 无数据，进度 {idx}/{total}")
        else:
            progress.progress(idx / total)
            status_box.warning(f"{index_name} 获取失败，进度 {idx}/{total}")

    result = run_index_ma20_update(
        api_key=api_key,
        days=int(days),
        cache_source="auto",
        use_fresh_cache=not force_refresh,
        progress_callback=show_progress,
        max_workers=int(max_workers),
    )
    if result.status == "success":
        if result.errors:
            st.warning(result.message)
        else:
            st.success(result.message)
        st.rerun()
    else:
        st.error(result.message)

report_df = None
report_meta = None
for source in ("auto", "manual"):
    report_df, meta = load_dataset(
        "index_ma20_latest",
        source,
        "index_ma20_report",
    )
    if report_df is not None:
        report_meta = meta
        break

if not update_clicked and not st.session_state.index_auto_update_done and not is_today_cache(report_meta):
    st.session_state.index_auto_update_done = True
    if start_background_update_once(api_key=api_key or os.getenv("TICKFLOW_API_KEY", ""), days=int(days)):
        st.info("今日指数数据正在后台更新。当前先显示本地缓存，稍后刷新页面即可查看最新数据。")
    else:
        st.warning("后台更新启动失败，可以点击左侧按钮手动更新。")

if report_df is not None and report_meta is not None:
    st.caption(f"更新时间：{format_update_time(report_meta['last_update_time'])}")

if report_df is not None:
    summary_df = build_summary(report_df)
    if not summary_df.empty:
        summary_date = summary_df["日期"].max()
        st.subheader(f"最新摘要 · {summary_date}")
        render_index_cards(summary_df)

        display_summary_df = summary_df.drop(columns=["代码", "日期", "前收盘价"], errors="ignore")
        if "偏离率(%)" in display_summary_df.columns:
            display_summary_df = display_summary_df.assign(
                _sort_deviation=pd.to_numeric(display_summary_df["偏离率(%)"], errors="coerce")
            ).sort_values("_sort_deviation", ascending=False, na_position="last")
            display_summary_df = display_summary_df.drop(columns=["_sort_deviation"])
        centered_table(display_summary_df)

    with st.expander("查看完整分列数据", expanded=False):
        st.dataframe(report_df, use_container_width=True, hide_index=True)
else:
    st.info("还没有缓存数据。可以先点击左侧按钮自动更新，或上传已有 CSV。")
