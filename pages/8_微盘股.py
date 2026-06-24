import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.cache import load_dataset, save_dataset
from core.db import init_db
from core.ui import apply_global_style, apply_plotly_layout, render_metric_card, render_page_header
from services.microcap import (
    build_microcap_snapshot_metrics,
    build_microcap_summary,
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
    refresh_online = st.checkbox("联网刷新数据", value=False)
    save_to_cache = st.checkbox("保存到本地缓存", value=True)

fetch_clicked = st.button("加载微盘股数据", type="primary")
st.caption("默认优先使用本地缓存；勾选「联网刷新数据」或暂无缓存时才会联网获取，并可保存当日真实成分快照。")

cached_df, cache_meta = load_dataset("microcap_bk1158", "eastmoney", "microcap")
source_df = None

should_fetch_online = fetch_clicked and (refresh_online or cached_df is None or cached_df.empty)

if should_fetch_online:
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
            st.success(f"微盘股数据已联网更新，并保存 {snapshot_date} 的成分快照。")
        else:
            st.success("微盘股数据已联网更新。")
    except Exception as exc:
        st.error(f"获取失败：{exc}")
        st.stop()
elif cached_df is not None and not cached_df.empty:
    source_df = cached_df
    cache_time = format_cache_time(cache_meta.get('last_update_time') if cache_meta else None)
    if fetch_clicked and not refresh_online:
        st.success(f"已使用本地缓存，缓存时间：{cache_time}。如需重新联网，请勾选「联网刷新数据」。")
    else:
        st.info(f"已使用本地缓存，缓存时间：{cache_time}")
else:
    st.info("暂无本地缓存。点击「加载微盘股数据」会联网获取 BK1158 微盘股名单。")
    st.stop()

source_df = source_df.copy()
for column in ("最新价", "涨跌幅(%)", "总市值(亿元)"):
    if column in source_df.columns:
        source_df[column] = pd.to_numeric(source_df[column], errors="coerce")
if "排名" in source_df.columns:
    source_df["排名"] = pd.to_numeric(source_df["排名"], errors="coerce").astype("Int64")

display_df = source_df.head(int(top_n)).copy()
summary = build_microcap_summary(source_df, top_n=int(top_n))

snapshot_df, snapshot_meta = load_microcap_constituent_snapshots()
snapshot_metrics = build_microcap_snapshot_metrics(
    snapshot_df,
    pool_count=400,
    micro_count=20,
    median_rank=200,
)
if not snapshot_metrics.empty:
    snapshot_metrics["日期"] = pd.to_datetime(snapshot_metrics["日期"], errors="coerce").dt.normalize()
    snapshot_metrics["日期显示"] = snapshot_metrics["日期"].dt.strftime("%Y-%m-%d")

metric_cols = st.columns(5)
with metric_cols[0]:
    render_metric_card("成分股数量", format_metric(summary.get("成分股数量")), "本次返回的有效 BK1158 成分股数量")
with metric_cols[1]:
    render_metric_card(
        "可交易股票数",
        format_metric(summary.get("可交易股票数")),
        f"均值和中位数计算前会剔除停牌股；本次剔除 {format_metric(summary.get('停牌剔除数'))} 只",
    )
with metric_cols[2]:
    render_metric_card("展示数量", format_metric(summary.get("展示数量")), "当前表格展示的最小市值股票数量")
with metric_cols[3]:
    render_metric_card("中位数市值", format_metric(summary.get("中位数市值(亿元)"), " 亿元"), "剔除停牌股后，按总市值升序取前 400 只中的第 200 名市值")
with metric_cols[4]:
    render_metric_card("微盘20均值", format_metric(summary.get("微盘20均值(亿元)"), " 亿元"), "剔除停牌股后，按总市值升序取前 20 只计算的平均总市值")

tabs = st.tabs(["微盘股列表", "真实成分快照", "原始数据"])

with tabs[0]:
    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "最新价": st.column_config.NumberColumn(format="%.2f"),
            "涨跌幅(%)": st.column_config.NumberColumn(format="%.2f"),
            "成交量": st.column_config.NumberColumn(format="%.0f"),
            "成交额": st.column_config.NumberColumn(format="%.0f"),
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
    st.caption("这里仅展示联网刷新时保存下来的 BK1158 真实成分快照，不再混入当前成分历史回溯估算。")
    if snapshot_df is not None and not snapshot_df.empty:
        snapshot_dates = pd.to_datetime(snapshot_df["快照日期"], errors="coerce").dropna()
        st.info(
            f"真实成分快照覆盖：{snapshot_dates.min().strftime('%Y-%m-%d')} 至 "
            f"{snapshot_dates.max().strftime('%Y-%m-%d')}，共 {snapshot_dates.dt.date.nunique()} 个快照日；"
            f"缓存时间：{format_cache_time(snapshot_meta.get('last_update_time') if snapshot_meta else None)}"
        )
    else:
        st.info("尚无真实成分快照；点击「联网获取微盘股数据」并保存缓存后开始记录。")

    if snapshot_metrics.empty:
        st.info("暂无真实成分快照指标。")
    else:
        latest_row = snapshot_metrics.sort_values("日期").tail(1).iloc[0]
        metric_row = st.columns(4)
        with metric_row[0]:
            render_metric_card("快照最新日期", latest_row["日期"].strftime("%Y-%m-%d"), "真实成分快照的最新日期")
        with metric_row[1]:
            render_metric_card("有效股票数", format_metric(latest_row.get("有效股票数")), "剔除停牌股后的快照样本数")
        with metric_row[2]:
            render_metric_card("第200名市值", format_metric(latest_row.get("第200名市值(亿元)"), " 亿元"), "真实快照中第 200 名市值")
        with metric_row[3]:
            render_metric_card("微盘20均值", format_metric(latest_row.get("微盘20均值(亿元)"), " 亿元"), "真实快照中最小 20 只市值均值")

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=snapshot_metrics["日期显示"],
                y=snapshot_metrics["第200名市值(亿元)"],
                mode="lines+markers",
                name="第200名市值（真实快照）",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=snapshot_metrics["日期显示"],
                y=snapshot_metrics["微盘20均值(亿元)"],
                mode="lines+markers",
                name="微盘20均值（真实快照）",
            )
        )
        apply_plotly_layout(fig, height=520)
        fig.update_xaxes(type="category", tickmode="array", tickvals=snapshot_metrics["日期显示"].tolist())
        st.plotly_chart(fig, use_container_width=True)

        display_snapshot_metrics = snapshot_metrics.sort_values("日期", ascending=False).copy()
        if "日期显示" in display_snapshot_metrics.columns:
            display_snapshot_metrics["日期"] = display_snapshot_metrics["日期显示"]
            display_snapshot_metrics = display_snapshot_metrics.drop(columns=["日期显示"])
        st.dataframe(
            display_snapshot_metrics,
            use_container_width=True,
            hide_index=True,
            column_config={
                "第200名市值(亿元)": st.column_config.NumberColumn(format="%.2f"),
                "微盘20均值(亿元)": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        snapshot_csv = display_snapshot_metrics.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button(
            "下载真实成分快照 CSV",
            data=snapshot_csv,
            file_name="BK1158_microcap_snapshot_metrics.csv",
            mime="text/csv",
        )

with tabs[2]:
    st.dataframe(
        source_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "最新价": st.column_config.NumberColumn(format="%.2f"),
            "涨跌幅(%)": st.column_config.NumberColumn(format="%.2f"),
            "成交量": st.column_config.NumberColumn(format="%.0f"),
            "成交额": st.column_config.NumberColumn(format="%.0f"),
            "总市值(亿元)": st.column_config.NumberColumn(format="%.2f"),
        },
    )
