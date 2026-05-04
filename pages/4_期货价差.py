import html

import pandas as pd
import plotly.express as px
import streamlit as st

from core.cache import load_dataset, save_dataset
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
st.caption("输入多个期货合约，选择基准合约，计算“基准 - 其他”的绝对价差。")


def format_cell(value) -> str:
    if pd.isna(value):
        return "-"
    if isinstance(value, (int, float)):
        return f"{float(value):.4f}".rstrip("0").rstrip(".")
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    return str(value)


def format_cache_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value).replace("T", " ")


def cache_matches_contracts(df: pd.DataFrame, contracts: list[str], base_contract: str) -> bool:
    if df is None or df.empty or "date" not in df.columns:
        return False
    required_columns = [f"{base_contract}_close"]
    required_columns.extend(
        f"spread_{base_contract}_vs_{contract}"
        for contract in contracts
        if contract != base_contract
    )
    return all(column in df.columns for column in required_columns)


def centered_table(df: pd.DataFrame) -> None:
    headers = "".join(f"<th>{html.escape(str(col))}</th>" for col in df.columns)
    rows = []
    for _, row in df.iterrows():
        cells = "".join(f"<td>{html.escape(format_cell(row[col]))}</td>" for col in df.columns)
        rows.append(f"<tr>{cells}</tr>")
    st.markdown(
        f"""
        <style>
        .centered-futures-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.92rem;
        }}
        .centered-futures-table th,
        .centered-futures-table td {{
            text-align: center;
            padding: 0.45rem 0.6rem;
            border-bottom: 1px solid rgba(49, 51, 63, 0.12);
            white-space: nowrap;
        }}
        .centered-futures-table th {{
            font-weight: 600;
            background: rgba(49, 51, 63, 0.04);
        }}
        </style>
        <table class="centered-futures-table">
            <thead><tr>{headers}</tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
        """,
        unsafe_allow_html=True,
    )

with st.sidebar:
    st.subheader("参数")
    contracts_text = st.text_area("合约代码", value="IM2605 IM2606", height=90)
    api_key = st.text_input(
        "TickFlow API Key",
        value="",
        type="password",
        placeholder="可选；填入后使用实时更新的日线",
    )
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
    force_refresh = st.checkbox("联网更新数据", value=False)
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

cache_symbol = f"futures_spread_{base_contract}"
cached_df, cache_meta = load_dataset(cache_symbol, "akshare", "futures_spread")
errors = []

if cached_df is not None and not force_refresh and cache_matches_contracts(cached_df, contracts, base_contract):
    spread_df = cached_df.copy()
    spread_df["date"] = pd.to_datetime(spread_df["date"], errors="coerce")
    st.info(f"已使用本地缓存，缓存时间：{format_cache_time(cache_meta['last_update_time'])}")
else:
    if cached_df is not None and not force_refresh:
        st.warning("本地缓存与当前合约组合不匹配，已重新联网获取。")
    with st.spinner("正在获取期货数据并计算价差..."):
        data, errors = fetch_contracts(
            contracts,
            max_workers=int(max_workers),
            api_key=api_key,
        )
        try:
            spread_df = calculate_spreads(data, base_contract)
        except Exception as exc:
            st.error(f"计算失败：{exc}")
            if errors:
                st.warning("部分合约获取失败：" + " | ".join(errors))
            st.stop()

if errors:
    st.warning("部分合约获取失败：" + " | ".join(errors))

available_contracts = [
    contract
    for contract in contracts
    if f"{contract}_close" in spread_df.columns
]
summary_df = build_spread_summary(spread_df, available_contracts, base_contract)

if save_to_cache and (force_refresh or cached_df is None or not cache_matches_contracts(cached_df, contracts, base_contract)):
    save_dataset(
        symbol=cache_symbol,
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
            )
    centered_table(summary_df.drop(columns=["最新占比(%)"], errors="ignore"))

tabs = st.tabs(["价差图", "明细数据"])
other_contracts = [contract for contract in available_contracts if contract != base_contract]

with tabs[0]:
    spread_cols = [f"spread_{base_contract}_vs_{contract}" for contract in other_contracts]
    chart_df = spread_df[["date", *spread_cols]].melt(
        id_vars="date",
        var_name="价差对",
        value_name="价差",
    ).dropna(subset=["价差"])
    label_map = {
        f"spread_{base_contract}_vs_{contract}": f"{base_contract} - {contract}"
        for contract in other_contracts
    }
    chart_df["价差对"] = chart_df["价差对"].map(label_map)
    fig = px.line(chart_df, x="date", y="价差", color="价差对", title="绝对价差（基准 - 其他）")
    fig.update_layout(hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)

with tabs[1]:
    detail_df = spread_df.drop(
        columns=[col for col in spread_df.columns if col.endswith("_pct")],
        errors="ignore",
    ).sort_values("date", ascending=False)
    centered_table(detail_df)
    csv_bytes = spread_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button(
        "下载价差数据 CSV",
        data=csv_bytes,
        file_name=f"{base_contract}_spread.csv",
        mime="text/csv",
    )
