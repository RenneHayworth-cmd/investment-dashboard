import pandas as pd
import plotly.express as px
import streamlit as st

from core.cache import save_dataset
from core.db import init_db
from services.futures_spread import (
    build_spread_summary,
    calculate_spreads,
    contract_name,
    fetch_contracts,
    parse_contracts,
)


st.set_page_config(page_title="期货价差", layout="wide")
init_db()

st.title("期货价差")
st.caption("输入多个期货合约，选择基准合约，计算“基准 - 其他”的绝对价差和百分比价差。")

with st.sidebar:
    st.subheader("参数")
    contracts_text = st.text_area("合约代码", value="IM2605 IM2606", height=90)
    contracts = parse_contracts(contracts_text)
    if contracts:
        base_contract = st.selectbox(
            "基准合约",
            options=contracts,
            format_func=contract_name,
        )
    else:
        base_contract = ""
    max_workers = st.slider("并发请求数", min_value=1, max_value=8, value=5)
    save_to_cache = st.checkbox("分析后保存到本地缓存", value=True)
    analyze_clicked = st.button("获取数据并分析", type="primary")

if not contracts:
    st.info("请输入至少两个合约代码，例如：IM2605 IM2606。")
    st.stop()

if len(contracts) < 2:
    st.warning("至少需要两个合约才能计算价差。")
    st.stop()

if not analyze_clicked:
    st.info("设置合约和基准后，点击左侧「获取数据并分析」。")
    st.stop()

with st.spinner("正在获取期货数据并计算价差..."):
    data, errors = fetch_contracts(contracts, max_workers=int(max_workers))
    try:
        spread_df = calculate_spreads(data, base_contract)
    except Exception as exc:
        st.error(f"计算失败：{exc}")
        if errors:
            st.warning("部分合约获取失败：" + " | ".join(errors))
        st.stop()

if errors:
    st.warning("部分合约获取失败：" + " | ".join(errors))

available_contracts = [contract for contract in contracts if contract in data]
summary_df = build_spread_summary(spread_df, available_contracts, base_contract)

if save_to_cache:
    save_dataset(
        symbol=f"futures_spread_{base_contract}",
        name=f"{contract_name(base_contract)} 期货价差",
        source="akshare",
        data_type="futures_spread",
        df=spread_df,
    )

st.subheader("统计汇总")
if summary_df.empty:
    st.warning("没有可展示的价差统计。")
else:
    metric_cols = st.columns(min(4, len(summary_df)))
    for idx, row in summary_df.iterrows():
        with metric_cols[idx % len(metric_cols)]:
            st.metric(
                label=row["价差对"],
                value=f"{row['最新价差']:.2f}",
                delta=f"{row['最新占比(%)']:+.2f}%",
            )
    st.dataframe(summary_df, use_container_width=True, hide_index=True)

tabs = st.tabs(["价差图", "百分比价差", "明细数据"])
other_contracts = [contract for contract in available_contracts if contract != base_contract]

with tabs[0]:
    spread_cols = [f"spread_{base_contract}_vs_{contract}" for contract in other_contracts]
    chart_df = spread_df[["date", *spread_cols]].melt(
        id_vars="date",
        var_name="价差对",
        value_name="价差",
    )
    label_map = {
        f"spread_{base_contract}_vs_{contract}": f"{base_contract} - {contract}"
        for contract in other_contracts
    }
    chart_df["价差对"] = chart_df["价差对"].map(label_map)
    fig = px.line(chart_df, x="date", y="价差", color="价差对", title="绝对价差（基准 - 其他）")
    fig.update_layout(hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)

with tabs[1]:
    pct_cols = [f"spread_{base_contract}_vs_{contract}_pct" for contract in other_contracts]
    chart_df = spread_df[["date", *pct_cols]].melt(
        id_vars="date",
        var_name="价差对",
        value_name="价差占比(%)",
    )
    label_map = {
        f"spread_{base_contract}_vs_{contract}_pct": f"{base_contract} - {contract}"
        for contract in other_contracts
    }
    chart_df["价差对"] = chart_df["价差对"].map(label_map)
    fig = px.line(chart_df, x="date", y="价差占比(%)", color="价差对", title="百分比价差")
    fig.update_layout(hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)

with tabs[2]:
    st.dataframe(spread_df.sort_values("date", ascending=False), use_container_width=True, hide_index=True)
    csv_bytes = spread_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button(
        "下载价差数据 CSV",
        data=csv_bytes,
        file_name=f"{base_contract}_spread.csv",
        mime="text/csv",
    )
