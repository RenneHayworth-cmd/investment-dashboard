import os
import html

import pandas as pd
import streamlit as st

from core.cache import load_dataset, save_dataset
from core.db import init_db
from services.correlation_analysis import (
    calculate_price_correlation,
    delete_correlation_results,
    list_correlation_results,
    normalize_price_dataframe,
    parse_symbols,
    save_correlation_results,
)
from services.fund_analysis import fetch_tickflow_fund_close, infer_tickflow_symbol, read_uploaded_table
from services.futures_options_analysis import DATA_TYPE_FUTURES, fetch_futures_option_data
from services.futures_spread import CONTRACT_PREFIXES
from services.us_stock_analysis import fetch_tickflow_us_daily, infer_us_symbol, parse_us_symbols


st.set_page_config(page_title="相关性分析", layout="wide")
init_db()

st.title("相关性分析")
st.caption("按共同交易日期对齐不同标的收盘价，计算 Pearson 相关系数 r。支持上传文件、A 股 ETF、美股和期货主连。")


def format_cache_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value).replace("T", " ")


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=True, encoding="utf-8-sig").encode("utf-8-sig")


with st.sidebar:
    st.subheader("数据来源")
    uploaded_files = st.file_uploader(
        "上传 CSV/Excel",
        type=["csv", "xlsx", "xls"],
        accept_multiple_files=True,
    )
    cn_codes = st.text_area(
        "A股ETF代码",
        value="159915 512890",
        height=76,
        placeholder="例如：159915 512890，或 510300.SH",
    )
    us_codes = st.text_area(
        "美股代码",
        value="",
        height=76,
        placeholder="例如：AAPL MSFT SPY COWZ",
    )
    futures_codes = st.text_area(
        "期货主连代码",
        value="",
        height=76,
        placeholder="例如：IM0 I0 AU0，或 IM主连 I主连",
    )

    st.subheader("参数")
    count = st.number_input("日线条数", min_value=60, max_value=10000, value=2500, step=100)
    adjust_option = st.selectbox("股票/ETF复权", options=["前复权", "后复权", "不复权"], index=0)
    correlation_method = st.selectbox("计算方式", options=["收盘价相关", "日收益率相关"], index=0)
    range_option = st.selectbox("计算区间", options=["全区间", "最近1年", "最近3年", "最近5年", "自定义"], index=0)
    custom_start = None
    custom_end = None
    if range_option == "自定义":
        custom_start = st.date_input("开始日期", value=pd.Timestamp.today() - pd.DateOffset(years=3))
        custom_end = st.date_input("结束日期", value=pd.Timestamp.today())
    api_key = st.text_input("TickFlow API Key", value=os.getenv("TICKFLOW_API_KEY", ""), type="password")
    force_refresh = st.checkbox("联网更新数据", value=False)
    calculate_clicked = st.button("计算相关系数", type="primary")

left_col, right_col = st.columns([3, 1.35])
history_box = right_col.empty()


def render_results_panel() -> None:
    with history_box.container():
        st.subheader("相关性分析结果")
        saved_df = list_correlation_results()
        if not saved_df.empty:
            render_saved_results(saved_df)
            delete_options = {
                f"{row['标的A']} / {row['标的B']}  {row['相关系数r']:.4f}  {row['相关性']}": int(row["id"])
                for _, row in saved_df.iterrows()
            }
            selected = st.multiselect(
                "删除结果",
                options=list(delete_options.keys()),
                key="correlation_delete_selection",
            )
            if st.button("删除选中结果", key="correlation_delete_button") and selected:
                delete_correlation_results([delete_options[item] for item in selected])
                st.rerun()
        else:
            st.info("计算完成后会保存到这里，下次打开页面仍会展示。")


def render_saved_results(df: pd.DataFrame) -> None:
    rows = []
    for _, row in df.iterrows():
        asset_a = format_asset_label_html(row["标的A"])
        asset_b = format_asset_label_html(row["标的B"])
        rows.append(
            "<tr>"
            f"<td>{asset_a}</td>"
            f"<td>{asset_b}</td>"
            f"<td class=\"corr-r\">{float(row['相关系数r']):.4f}</td>"
            f"<td>{html.escape(str(row['相关性']))}</td>"
            "</tr>"
        )
    st.markdown(
        f"""
        <style>
        .correlation-results-table {{
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
            font-size: 0.84rem;
        }}
        .correlation-results-table th,
        .correlation-results-table td {{
            border-bottom: 1px solid rgba(49, 51, 63, 0.12);
            padding: 0.45rem 0.25rem;
            text-align: left;
            vertical-align: top;
            white-space: normal;
            overflow-wrap: anywhere;
            word-break: break-word;
        }}
        .correlation-results-table th {{
            color: rgba(49, 51, 63, 0.72);
            font-weight: 600;
        }}
        .correlation-results-table .asset-code {{
            display: block;
            color: rgba(49, 51, 63, 0.68);
            margin-top: 0.12rem;
        }}
        .correlation-results-table .corr-r {{
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }}
        </style>
        <table class="correlation-results-table">
            <thead>
                <tr>
                    <th style="width:34%;">标的A</th>
                    <th style="width:34%;">标的B</th>
                    <th style="width:16%;">r</th>
                    <th style="width:16%;">相关性</th>
                </tr>
            </thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
        """,
        unsafe_allow_html=True,
    )


def format_asset_label_html(value: object) -> str:
    text = str(value)
    text = format_futures_asset_text(text)
    parts = text.rsplit(" ", 1)
    if len(parts) == 2 and "." in parts[1]:
        return f"{html.escape(parts[0])}<span class=\"asset-code\">{html.escape(parts[1])}</span>"
    return html.escape(text)


def format_futures_asset_text(text: str) -> str:
    parts = text.split()
    if len(parts) == 2 and parts[0] == parts[1]:
        return parts[0]
    if len(parts) >= 2 and parts[-1].upper().endswith("0") and "期货主连" in text:
        return futures_product_name(parts[-1])
    return text


def futures_product_name(code: str) -> str:
    product = str(code).strip().upper()
    if product.endswith("0"):
        product = product[:-1]
    return CONTRACT_PREFIXES.get(product, product)


def apply_date_range(items: list, option: str, start_date, end_date) -> list:
    if not items or option == "全区间":
        return items

    max_date = max(pd.to_datetime(item.dataframe["date"]).max() for item in items)
    if option == "最近1年":
        start = max_date - pd.DateOffset(years=1)
        end = max_date
    elif option == "最近3年":
        start = max_date - pd.DateOffset(years=3)
        end = max_date
    elif option == "最近5年":
        start = max_date - pd.DateOffset(years=5)
        end = max_date
    else:
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        if start > end:
            raise ValueError("自定义区间的开始日期不能晚于结束日期。")

    filtered = []
    for item in items:
        data = item.dataframe.copy()
        dates = pd.to_datetime(data["date"], errors="coerce")
        data = data[(dates >= start) & (dates <= end)].reset_index(drop=True)
        filtered.append(type(item)(symbol=item.symbol, name=item.name, dataframe=data))
    return filtered


if not calculate_clicked:
    render_results_panel()
    with left_col:
        st.info("在左侧任意输入 A股ETF、美股、期货主连或上传文件，至少两个标的即可混合计算相关系数。上传文件需要包含日期列和收盘价/净值列。")
    st.stop()

items = []
errors = []
adjust_map = {"前复权": "forward", "后复权": "backward", "不复权": None}
adjust_value = adjust_map[adjust_option]
adjust_key = adjust_value or "none"

try:
    source_names = []
    for uploaded_file in uploaded_files:
        try:
            raw_df = read_uploaded_table(uploaded_file.getvalue(), uploaded_file.name)
            items.append(normalize_price_dataframe(raw_df, fallback_name=uploaded_file.name))
            source_names.append("上传文件")
        except Exception as exc:
            errors.append(f"{uploaded_file.name}: {exc}")

    for code in parse_symbols(cn_codes):
        try:
            symbol = infer_tickflow_symbol(code)
            cache_symbol = f"correlation_cn_{symbol}_{adjust_key}"
            cache_period = f"{int(count)}_1d"
            cached_df, cache_meta = load_dataset(
                cache_symbol,
                "tickflow_correlation",
                "cn_etf_correlation_raw",
                period=cache_period,
            )
            if cached_df is not None and not force_refresh:
                raw_df = cached_df
                st.info(
                    f"{symbol} 已使用本地缓存，缓存时间："
                    f"{format_cache_time(cache_meta.get('last_update_time') if cache_meta else None)}"
                )
            else:
                with st.spinner(f"正在通过 TickFlow 拉取 {symbol} 日线..."):
                    raw_df = fetch_tickflow_fund_close(
                        symbol=symbol,
                        api_key=api_key,
                        count=int(count),
                        adjust=adjust_value,
                    )
                save_dataset(
                    cache_symbol,
                    f"{symbol} {adjust_option}",
                    "tickflow_correlation",
                    "cn_etf_correlation_raw",
                    raw_df,
                    period=cache_period,
                )
            items.append(normalize_price_dataframe(raw_df, fallback_name=symbol, fallback_symbol=symbol))
            source_names.append("A股ETF")
        except Exception as exc:
            errors.append(f"{code}: {exc}")

    for code in parse_us_symbols(us_codes):
        try:
            symbol = infer_us_symbol(code)
            cache_symbol = f"correlation_us_{symbol}_{adjust_key}"
            cache_period = f"{int(count)}_1d"
            cached_df, cache_meta = load_dataset(
                cache_symbol,
                "tickflow_correlation",
                "us_correlation_raw",
                period=cache_period,
            )
            if cached_df is not None and not force_refresh:
                raw_df = cached_df
                st.info(
                    f"{symbol} 已使用本地缓存，缓存时间："
                    f"{format_cache_time(cache_meta.get('last_update_time') if cache_meta else None)}"
                )
            else:
                with st.spinner(f"正在通过 TickFlow 拉取 {symbol} 日线..."):
                    raw_df = fetch_tickflow_us_daily(
                        symbol=symbol,
                        api_key=api_key,
                        count=int(count),
                        adjust=adjust_value,
                    )
                save_dataset(
                    cache_symbol,
                    f"{symbol} {adjust_option}",
                    "tickflow_correlation",
                    "us_correlation_raw",
                    raw_df,
                    period=cache_period,
                )
            items.append(normalize_price_dataframe(raw_df, fallback_name=symbol, fallback_symbol=symbol))
            source_names.append("美股")
        except Exception as exc:
            errors.append(f"{code}: {exc}")

    for code in parse_symbols(futures_codes):
        try:
            cache_symbol = f"correlation_futures_{code.strip()}"
            cache_period = f"{int(count)}_1d"
            cached_df, cache_meta = load_dataset(
                cache_symbol,
                "akshare_correlation",
                "futures_main_correlation_raw",
                period=cache_period,
            )
            if cached_df is not None and not force_refresh:
                raw_df = cached_df
                st.info(
                    f"{code} 已使用本地缓存，缓存时间："
                    f"{format_cache_time(cache_meta.get('last_update_time') if cache_meta else None)}"
                )
            else:
                with st.spinner(f"正在获取 {code} 期货主连日线..."):
                    result = fetch_futures_option_data(
                        raw_symbol=code,
                        data_type=DATA_TYPE_FUTURES,
                        period="1d",
                        count=int(count),
                        api_key=api_key,
                        use_free=True,
                        ma_periods=[],
                    )
                    raw_df = result.dataframe
                futures_name = futures_product_name(code)
                save_dataset(
                    cache_symbol,
                    futures_name,
                    "akshare_correlation",
                    "futures_main_correlation_raw",
                    raw_df,
                    period=cache_period,
                )
            futures_name = futures_product_name(code)
            items.append(normalize_price_dataframe(raw_df, fallback_name=futures_name, fallback_symbol=futures_name))
            source_names.append("期货主连")
        except Exception as exc:
            errors.append(f"{code}: {exc}")

    if errors:
        st.warning("部分标的未能解析：\n\n" + "\n".join(errors))
    if len(items) < 2:
        st.error("至少需要成功获取或上传 2 个标的。")
        st.stop()

    items = apply_date_range(items, range_option, custom_start, custom_end)
    method_value = "return" if correlation_method == "日收益率相关" else "price"
    result = calculate_price_correlation(items, method=method_value)
except Exception as exc:
    st.error(f"相关性计算出错：{exc}")
    st.stop()

summary = result.summary
save_correlation_results(
    result.pair_table,
    summary,
    source_summary=f"{' / '.join(dict.fromkeys(source_names))}；{range_option}；{correlation_method}",
)
render_results_panel()

with left_col:
    metric_cols = st.columns(4)
    with metric_cols[0]:
        st.metric("标的数量", summary.get("标的数量"))
    with metric_cols[1]:
        st.metric("共同日期数", summary.get("共同日期数"))
    with metric_cols[2]:
        st.metric("平均相关系数r", summary.get("平均相关系数r"))
    with metric_cols[3]:
        st.metric("时间区间", f"{summary.get('开始日期')} → {summary.get('结束日期')}")

    st.subheader("相关系数矩阵")
    st.dataframe(result.correlation_matrix, use_container_width=True)

    st.subheader("两两相关性")
    st.dataframe(result.pair_table, use_container_width=True, hide_index=True)

    download_cols = st.columns(2)
    with download_cols[0]:
        st.download_button(
            "下载相关矩阵 CSV",
            data=to_csv_bytes(result.correlation_matrix),
            file_name="correlation_matrix.csv",
            mime="text/csv",
        )
    with download_cols[1]:
        st.download_button(
            "下载对齐价格 CSV",
            data=result.aligned_prices.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
            file_name="correlation_aligned_prices.csv",
            mime="text/csv",
        )
