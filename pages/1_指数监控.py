import os
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from core.cache import list_datasets, load_dataset, save_dataset
from core.db import finish_job, init_db, start_job
from services.index_ma20 import INDEX_CONFIG, build_summary, fetch_one_index, merge_by_date


st.set_page_config(page_title="指数监控", layout="wide")
init_db()

st.title("指数监控")
st.caption("指数数据会缓存到本地 CSV，页面优先读取缓存。")

with st.sidebar:
    st.subheader("更新设置")
    api_key = st.text_input(
        "TickFlow API Key",
        value=os.getenv("TICKFLOW_API_KEY", ""),
        type="password",
    )
    days = st.number_input("展示最近天数", min_value=10, max_value=365, value=30, step=5)
    update_clicked = st.button("更新指数 MA20 数据", type="primary")
    import_latest_clicked = st.button("导入桌面最新指数CSV")

if import_latest_clicked:
    desktop = Path.home() / "Desktop"
    candidates = sorted(desktop.glob("指数MA20分析_分列版_*.csv"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        st.warning(f"桌面没有找到 指数MA20分析_分列版_*.csv：{desktop}")
    else:
        latest_file = candidates[-1]
        df = pd.read_csv(latest_file)
        save_dataset(
            symbol="index_ma20_latest",
            name="指数MA20分列结果",
            source="auto",
            data_type="index_ma20_report",
            df=df,
        )
        st.success(f"已导入：{latest_file}")
        st.rerun()

if update_clicked:
    job_id = start_job("更新指数MA20")
    try:
        st.warning("在线更新还在调试中。如果页面卡住，请先用原脚本生成CSV，再点「导入桌面最新指数CSV」。")
        progress = st.progress(0)
        status_box = st.empty()
        all_data = []
        errors = []
        total = len(INDEX_CONFIG)

        for idx, (index_name, index_config) in enumerate(INDEX_CONFIG.items(), start=1):
            status_box.info(f"正在获取 {index_name} ({idx}/{total})...")
            try:
                df = fetch_one_index(index_name, index_config, api_key=api_key, days=int(days))
                if df is not None and not df.empty:
                    all_data.append(df)
                    status_box.success(f"{index_name} 获取完成")
                else:
                    errors.append(f"{index_name}: 无数据")
                    status_box.warning(f"{index_name} 无数据")
            except Exception as exc:
                errors.append(f"{index_name}: {exc}")
                status_box.warning(f"{index_name} 获取失败：{exc}")
            progress.progress(idx / total)

        if not all_data:
            raise RuntimeError("未获取到任何指数数据。" + " | ".join(errors))

        report = merge_by_date(all_data)
        report.attrs["errors"] = errors
        save_dataset(
            symbol="index_ma20_latest",
            name="指数MA20分列结果",
            source="auto",
            data_type="index_ma20_report",
            df=report,
        )

        message = "更新成功"
        if errors:
            message += "；部分指数失败：" + " | ".join(errors)
            st.warning(message)
        else:
            st.success(message)
        finish_job(job_id, "success", message)
        st.rerun()
    except Exception as exc:
        finish_job(job_id, "failed", str(exc))
        st.error(f"更新失败：{exc}")

uploaded = st.file_uploader("导入指数 MA20 分列 CSV", type=["csv"])
if uploaded is not None:
    df = pd.read_csv(uploaded)
    save_dataset(
        symbol="index_ma20_latest",
        name="指数MA20分列结果",
        source="manual",
        data_type="index_ma20_report",
        df=df,
    )
    st.success("已保存到本地 CSV 缓存，并写入 SQLite 索引。")

datasets = list_datasets()
st.subheader("缓存状态")
st.dataframe(datasets, use_container_width=True, hide_index=True)

report_df = None
for source in ("auto", "manual"):
    report_df, meta = load_dataset(
        "index_ma20_latest",
        source,
        "index_ma20_report",
    )
    if report_df is not None:
        st.caption(f"当前展示数据源：{source}，更新时间：{meta['last_update_time']}")
        break

if report_df is not None:
    summary_df = build_summary(report_df)
    if not summary_df.empty:
        st.subheader("最新摘要")
        metric_cols = st.columns(min(4, len(summary_df)))
        for idx, row in summary_df.iterrows():
            with metric_cols[idx % len(metric_cols)]:
                st.metric(
                    label=f"{row['指数']} · {row['日期']}",
                    value=f"{row['收盘价']:.2f}",
                    delta=f"{row['偏离率(%)']:+.2f}% vs MA20",
                )

        fig_bar = px.bar(
            summary_df,
            x="指数",
            y="偏离率(%)",
            color="偏离率(%)",
            color_continuous_scale=["#2563eb", "#e5e7eb", "#dc2626"],
            title="各指数 MA20 偏离率",
        )
        fig_bar.update_layout(coloraxis_showscale=False)
        st.plotly_chart(fig_bar, use_container_width=True)

        st.dataframe(summary_df, use_container_width=True, hide_index=True)

    close_cols = [col for col in report_df.columns if col.endswith("_收盘价")]
    if close_cols and "日期" in report_df.columns:
        selected = st.selectbox("选择指数收盘价图表", close_cols)
        chart_df = report_df[["日期", selected]].dropna()
        fig = px.line(chart_df, x="日期", y=selected, title=selected)
        st.plotly_chart(fig, use_container_width=True)

    with st.expander("查看完整分列数据", expanded=False):
        st.dataframe(report_df, use_container_width=True, hide_index=True)
else:
    st.info("还没有缓存数据。可以先点击左侧按钮自动更新，或上传已有 CSV。")
