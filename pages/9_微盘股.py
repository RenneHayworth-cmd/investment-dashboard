import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.cache import load_dataset, save_dataset
from core.db import init_db
from core.ui import apply_global_style, apply_plotly_layout, render_metric_card, render_page_header
from services.microcap import (
    build_microcap_snapshot_metrics,
    build_microcap_summary,
    fetch_microcap_history_metrics,
    fetch_microcap_stocks,
    load_microcap_constituent_snapshots,
    save_microcap_constituent_snapshot,
)


st.set_page_config(page_title="微盘股", layout="wide")
init_db()
apply_global_style()

render_page_header(
    "微盘股",
    "基于东方财富 BK1158 板块成分股，按总市值从小到大筛选微盘股名单。",
    eyebrow="Micro Cap",
)


def format_cache_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value).replace("T", " ")


def format_metric(value: object, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


with st.sidebar:
    st.subheader("筛选设置")
    top_n = st.number_input("展示数量", min_value=1, max_value=500, value=30, step=5)
    page_size = st.number_input("拉取数量", min_value=50, max_value=1000, value=400, step=50)
    history_days = st.number_input("历史交易日", min_value=60, max_value=2000, value=1250, step=50)
    incremental_days = st.number_input("增量补取交易日", min_value=20, max_value=300, value=80, step=10)
    history_workers = st.slider("历史并发数", min_value=1, max_value=16, value=4)
    save_to_cache = st.checkbox("保存到本地缓存", value=True)

action_cols = st.columns([1, 1, 4])
with action_cols[0]:
    fetch_clicked = st.button("联网获取微盘股数据", type="primary")
with action_cols[1]:
    history_clicked = st.button("计算历史指标")
with action_cols[2]:
    api_key = st.text_input(
        "TickFlow API Key",
        value=os.getenv("TICKFLOW_API_KEY", ""),
        type="password",
        placeholder="可选；如触发限流会自动切换免费历史服务",
    )
st.caption("历史指标默认取近 5 年约 1250 个交易日；每只股票日线会单独缓存，后续只补最近一段交易日。")

cached_df, cache_meta = load_dataset("microcap_bk1158", "eastmoney", "microcap")
source_df = None

if fetch_clicked:
    try:
        with st.spinner("正在从东方财富获取 BK1158 成分股..."):
            source_df = fetch_microcap_stocks(page_size=int(page_size))
        if save_to_cache:
            save_dataset(
                symbol="microcap_bk1158",
                name="BK1158 微盘股成分",
                source="eastmoney",
                data_type="microcap",
                df=source_df,
            )
            _, snapshot_date = save_microcap_constituent_snapshot(source_df, pool_count=400)
            st.success(f"微盘股数据已更新，并保存 {snapshot_date} 的成分快照。")
        else:
            st.success("微盘股数据已更新。")
    except Exception as exc:
        st.error(f"获取失败：{exc}")
        st.stop()
elif cached_df is not None and not cached_df.empty:
    source_df = cached_df
    st.info(f"已使用本地缓存，缓存时间：{format_cache_time(cache_meta.get('last_update_time') if cache_meta else None)}")
else:
    st.info("暂无本地缓存。点击「联网获取微盘股数据」后查看 BK1158 微盘股名单。")
    st.stop()

source_df = source_df.copy()
for column in ("最新价", "涨跌幅(%)", "总市值(亿元)"):
    if column in source_df.columns:
        source_df[column] = pd.to_numeric(source_df[column], errors="coerce")
if "排名" in source_df.columns:
    source_df["排名"] = pd.to_numeric(source_df["排名"], errors="coerce").astype("Int64")

display_df = source_df.head(int(top_n)).copy()
summary = build_microcap_summary(source_df, top_n=int(top_n))

history_period = f"400_{int(history_days)}_1d"
history_df, history_meta = load_dataset(
    "microcap_bk1158_history",
    "eastmoney",
    "microcap_history",
    period=history_period,
)
history_errors = []
if history_clicked:
    try:
        if len(source_df) < 400:
            with st.spinner("当前成分不足 400 条，正在重新获取 BK1158 前 400 只..."):
                source_df = fetch_microcap_stocks(page_size=400)
                if save_to_cache:
                    save_dataset(
                        symbol="microcap_bk1158",
                        name="BK1158 微盘股成分",
                        source="eastmoney",
                        data_type="microcap",
                        df=source_df,
                    )
                    save_microcap_constituent_snapshot(source_df, pool_count=400)
        progress_bar = st.progress(0)
        progress_text = st.empty()

        def update_history_progress(done: int, total: int, label: str) -> None:
            percent = int(done / total * 100) if total else 0
            progress_bar.progress(min(percent, 100))
            if done == 0:
                progress_text.caption(f"历史指标计算进度：0/{total}，正在准备逐股历史数据...")
            elif label:
                progress_text.caption(f"历史指标计算进度：{done}/{total}，刚处理：{label}")
            else:
                progress_text.caption(f"历史指标计算进度：{done}/{total}")

        with st.spinner("正在按历史收盘价计算每日中位数市值和微盘20均值..."):
            history_result = fetch_microcap_history_metrics(
                source_df,
                days=int(history_days),
                pool_count=400,
                micro_count=20,
                median_rank=200,
                max_workers=int(history_workers),
                tickflow_api_key=api_key,
                incremental_days=int(incremental_days),
                progress_callback=update_history_progress,
            )
        progress_bar.progress(100)
        progress_text.caption("历史指标计算进度：完成")
        history_df = history_result.dataframe
        history_errors = history_result.errors
        if save_to_cache:
            save_dataset(
                symbol="microcap_bk1158_history",
                name="BK1158 微盘股历史指标",
                source="eastmoney",
                data_type="microcap_history",
                period=history_period,
                df=history_df,
            )
        st.success("微盘股历史指标已计算完成。")
    except Exception as exc:
        st.error(f"历史指标计算失败：{exc}")
        st.stop()
elif history_df is not None and not history_df.empty:
    st.info(f"已加载历史指标缓存，缓存时间：{format_cache_time(history_meta.get('last_update_time') if history_meta else None)}")

snapshot_df, snapshot_meta = load_microcap_constituent_snapshots()
snapshot_metrics = build_microcap_snapshot_metrics(
    snapshot_df,
    pool_count=400,
    micro_count=20,
    median_rank=200,
)

metric_cols = st.columns(4)
with metric_cols[0]:
    render_metric_card("成分股数量", format_metric(summary.get("成分股数量")), "本次返回的有效 BK1158 成分股数量")
with metric_cols[1]:
    render_metric_card("展示数量", format_metric(summary.get("展示数量")), "当前表格展示的最小市值股票数量")
with metric_cols[2]:
    render_metric_card("中位数市值", format_metric(summary.get("中位数市值(亿元)"), " 亿元"), "按总市值升序取前 400 只中的第 200 名市值")
with metric_cols[3]:
    render_metric_card("微盘20均值", format_metric(summary.get("微盘20均值(亿元)"), " 亿元"), "按总市值升序取前 20 只计算的平均总市值")

tabs = st.tabs(["微盘股列表", "历史指标", "市值分布", "原始数据"])

with tabs[0]:
    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "最新价": st.column_config.NumberColumn(format="%.2f"),
            "涨跌幅(%)": st.column_config.NumberColumn(format="%.2f"),
            "总市值(亿元)": st.column_config.NumberColumn(format="%.2f"),
        },
    )
    csv_bytes = display_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button(
        "下载当前名单 CSV",
        data=csv_bytes,
        file_name="BK1158_microcap_top.csv",
        mime="text/csv",
    )

with tabs[1]:
    st.caption(
        "历史线为“当前成分回溯估算”，按历史收盘价与当前股本口径估算；"
        "从首次联网刷新起另存每日 BK1158 成分快照，图中圆点为“真实成分快照”口径。"
    )
    if snapshot_df is not None and not snapshot_df.empty:
        snapshot_dates = pd.to_datetime(snapshot_df["快照日期"], errors="coerce").dropna()
        st.info(
            f"真实成分快照覆盖：{snapshot_dates.min().strftime('%Y-%m-%d')} 至 "
            f"{snapshot_dates.max().strftime('%Y-%m-%d')}，共 {snapshot_dates.dt.date.nunique()} 个快照日；"
            f"缓存时间：{format_cache_time(snapshot_meta.get('last_update_time') if snapshot_meta else None)}"
        )
    else:
        st.info("尚无真实成分快照；下次点击「联网获取微盘股数据」并保存缓存时开始记录。")
    if (history_df is None or history_df.empty) and snapshot_metrics.empty:
        st.info("点击「计算历史指标」后，可查看每日第 200 名市值和微盘20均值历史走势。")
    else:
        history_view = history_df.copy() if history_df is not None else pd.DataFrame()
        if history_view.empty:
            history_view = pd.DataFrame(columns=snapshot_metrics.columns)
        history_view["日期"] = pd.to_datetime(history_view["日期"], errors="coerce")
        history_view["口径"] = "当前成分回溯估算"
        value_cols = ["第200名市值(亿元)", "微盘20均值(亿元)"]
        for column in value_cols:
            history_view[column] = pd.to_numeric(history_view[column], errors="coerce")
        combined_history = pd.concat([history_view, snapshot_metrics], ignore_index=True, sort=False)
        combined_history["日期"] = pd.to_datetime(combined_history["日期"], errors="coerce")
        latest_history = combined_history.dropna(subset=value_cols, how="all").sort_values(
            ["日期", "口径"], ascending=[True, True]
        ).tail(1)
        if not latest_history.empty:
            latest_row = latest_history.iloc[0]
            metric_row = st.columns(4)
            with metric_row[0]:
                render_metric_card("历史最新日期", latest_row["日期"].strftime("%Y-%m-%d"), "历史指标的最新交易日")
            with metric_row[1]:
                render_metric_card("第200名市值", format_metric(latest_row.get("第200名市值(亿元)"), " 亿元"), "前 400 只中每日第 200 名市值")
            with metric_row[2]:
                render_metric_card("微盘20均值", format_metric(latest_row.get("微盘20均值(亿元)"), " 亿元"), "每日最小 20 只市值均值")
            with metric_row[3]:
                render_metric_card("数据口径", str(latest_row.get("口径", "-")), "真实快照优先；历史区间保留估算口径")

        fig = go.Figure()
        if "第200名市值(亿元)" in history_view.columns:
            fig.add_trace(
                go.Scatter(
                    x=history_view["日期"],
                    y=history_view["第200名市值(亿元)"],
                    mode="lines",
                    name="第200名市值",
                )
            )
        if "微盘20均值(亿元)" in history_view.columns:
            fig.add_trace(
                go.Scatter(
                    x=history_view["日期"],
                    y=history_view["微盘20均值(亿元)"],
                    mode="lines",
                    name="微盘20均值",
                )
            )
        if not snapshot_metrics.empty:
            fig.add_trace(
                go.Scatter(
                    x=snapshot_metrics["日期"],
                    y=snapshot_metrics["第200名市值(亿元)"],
                    mode="markers",
                    name="第200名市值（真实快照）",
                    marker={"size": 9, "symbol": "circle"},
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=snapshot_metrics["日期"],
                    y=snapshot_metrics["微盘20均值(亿元)"],
                    mode="markers",
                    name="微盘20均值（真实快照）",
                    marker={"size": 9, "symbol": "diamond"},
                )
            )
        apply_plotly_layout(fig, height=560)
        st.plotly_chart(fig, use_container_width=True)

        st.dataframe(
            combined_history.sort_values(["日期", "口径"], ascending=[False, False]),
            use_container_width=True,
            hide_index=True,
            column_config={
                "第200名市值(亿元)": st.column_config.NumberColumn(format="%.2f"),
                "微盘20均值(亿元)": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        history_csv = combined_history.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button(
            "下载历史指标 CSV",
            data=history_csv,
            file_name="BK1158_microcap_history_metrics.csv",
            mime="text/csv",
        )
        if history_errors:
            with st.expander(f"部分股票历史数据获取失败（{len(history_errors)}）"):
                st.write("\n".join(history_errors[:50]))

with tabs[2]:
    chart_df = display_df[["名称", "总市值(亿元)"]].dropna().set_index("名称")
    if chart_df.empty:
        st.info("没有可展示的市值数据。")
    else:
        st.bar_chart(chart_df, use_container_width=True)

with tabs[3]:
    st.dataframe(
        source_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "最新价": st.column_config.NumberColumn(format="%.2f"),
            "涨跌幅(%)": st.column_config.NumberColumn(format="%.2f"),
            "总市值(亿元)": st.column_config.NumberColumn(format="%.2f"),
        },
    )
