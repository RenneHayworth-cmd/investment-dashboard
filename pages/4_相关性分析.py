import os

import pandas as pd
import streamlit as st

from core.cache import list_datasets, load_dataset, save_dataset
from core.db import init_db
from services.correlation_analysis import (
    calculate_price_correlation,
    delete_correlation_results,
    list_correlation_results,
    normalize_price_dataframe,
    parse_symbols,
    save_correlation_results,
)
from services.fund_analysis import (
    FUND_ADJUSTMENT_OPTIONS,
    FUND_ADJUSTMENT_VALUES,
    build_fund_cache_symbol,
    fetch_tickflow_fund_close,
    infer_tickflow_symbol,
    read_uploaded_table,
)
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
    st.subheader("分析设置")
    count = st.number_input("日线条数", min_value=60, max_value=10000, value=2500, step=100)
    adjust_option = st.selectbox(
        "股票/ETF复权",
        options=list(FUND_ADJUSTMENT_OPTIONS),
        index=0,
    )
    correlation_method = st.selectbox("计算方式", options=["收盘价相关", "日收益率相关"], index=1)
    force_refresh = st.checkbox("联网更新数据", value=False)

with st.form("correlation_data_form"):
    st.subheader("数据来源")
    uploaded_files = st.file_uploader(
        "上传 CSV/Excel",
        type=["csv", "xlsx", "xls"],
        accept_multiple_files=True,
    )
    col1, col2, col3 = st.columns(3)
    with col1:
        cn_codes = st.text_area(
            "A股ETF代码",
            value="159915 512890",
            height=96,
            placeholder="例如：159915 512890，或 510300.SH",
        )
    with col2:
        us_codes = st.text_area(
            "美股代码",
            value="",
            height=96,
            placeholder="例如：AAPL MSFT SPY COWZ",
        )
    with col3:
        futures_codes = st.text_area(
            "期货主连代码",
            value="",
            height=96,
            placeholder="例如：IM0 I0 AU0，或 IM主连 I主连",
        )
    api_key = st.text_input("TickFlow API Key", value=os.getenv("TICKFLOW_API_KEY", ""), type="password")
    calculate_clicked = st.form_submit_button("计算相关系数", type="primary")

def render_results_panel() -> None:
    st.subheader("相关性分析结果")
    saved_df = list_correlation_results(limit=2000)
    if not saved_df.empty:
        completed_count = auto_complete_missing_cached_results(saved_df, correlation_method)
        if completed_count:
            st.toast(f"已用本地缓存自动补全 {completed_count} 个相关系数。")
            st.rerun()
        render_saved_results(saved_df)
    else:
        st.info("计算完成后会保存到这里，下次打开页面仍会展示。")


def render_saved_results(df: pd.DataFrame) -> None:
    groups = build_saved_groups(df)
    if not groups:
        st.info("还没有可展示的历史结果。")
        return

    for group in groups:
        st.markdown(f"#### {group['label']}")
        detail_cols = st.columns(4)
        detail_cols[0].caption(f"区间：{group['date_range']}")
        detail_cols[1].caption(f"共同日期数：{group['common_days']}")
        detail_cols[2].caption(f"计算方式：{group['method_summary']}")
        detail_cols[3].caption(f"已合并标的数：{len(group['assets'])}")

        st.dataframe(build_saved_matrix(group["data"]), use_container_width=True)
        if st.button("删除这个矩阵", key=f"delete_correlation_group_{group['key']}"):
            delete_correlation_results(group["ids"])
            st.rerun()


def build_saved_groups(df: pd.DataFrame) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], dict[str, object]] = {}
    seen_pairs_by_group: dict[tuple[str, str], set[tuple[str, str]]] = {}

    for _, row in df.sort_values("id", ascending=False).iterrows():
        source_summary = display_value(row.get("计算说明"))
        asset_a = format_asset_label_text(row["标的A"])
        asset_b = format_asset_label_text(row["标的B"])
        category_a = classify_asset_category(asset_a, source_summary)
        category_b = classify_asset_category(asset_b, source_summary)
        category = category_a if category_a == category_b else "跨资产"
        group_key = category
        pair_key = tuple(sorted((asset_a, asset_b)))

        group = groups.setdefault(
            group_key,
            {
                "key": category,
                "label": category,
                "category": category,
                "ids": [],
                "assets": [],
                "data_rows": [],
                "created_at": None,
            },
        )
        group["ids"].append(int(row["id"]))
        for asset in (asset_a, asset_b):
            if asset not in group["assets"]:
                group["assets"].append(asset)

        row_time = pd.to_datetime(row.get("计算时间"), errors="coerce")
        current_time = pd.to_datetime(group["created_at"], errors="coerce")
        if group["created_at"] is None or (not pd.isna(row_time) and (pd.isna(current_time) or row_time > current_time)):
            group["created_at"] = row.get("计算时间")

        seen_pairs = seen_pairs_by_group.setdefault(group_key, set())
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        group["data_rows"].append(row)

    result = []
    for group in groups.values():
        data = pd.DataFrame(group["data_rows"])
        if data.empty:
            continue
        group["data"] = data
        group["date_range"] = summarize_date_range(data)
        group["common_days"] = summarize_common_days(data)
        group["method_summary"] = summarize_methods(data)
        group["key"] = f"{group['key']}_{max(group['ids'])}"
        result.append(group)

    return sorted(result, key=lambda item: category_sort_key(item["category"]))


def auto_complete_missing_cached_results(df: pd.DataFrame, method_label: str) -> int:
    cached_items = load_cached_correlation_items()
    if not cached_items:
        return 0

    completed = 0
    for group in build_saved_groups(df):
        assets = list(group["assets"])
        existing_pairs = {
            tuple(sorted((format_asset_label_text(row["标的A"]), format_asset_label_text(row["标的B"]))))
            for _, row in group["data"].iterrows()
        }
        category = group["category"]
        source_summary = f"{category_to_source_summary(category)}；{method_label}"
        method_value = "return" if method_label == "日收益率相关" else "price"
        for left_index, asset_a in enumerate(assets):
            for asset_b in assets[left_index + 1 :]:
                pair_key = tuple(sorted((asset_a, asset_b)))
                if pair_key in existing_pairs:
                    continue
                item_a = cached_items.get(asset_a)
                item_b = cached_items.get(asset_b)
                if item_a is None or item_b is None:
                    continue
                try:
                    result = calculate_price_correlation([item_a, item_b], method=method_value)
                    save_correlation_results(result.pair_table, result.summary, source_summary=source_summary)
                    existing_pairs.add(pair_key)
                    completed += 1
                except Exception:
                    continue
    return completed


def load_cached_correlation_items() -> dict[str, object]:
    try:
        datasets = list_datasets()
    except Exception:
        return {}
    if datasets.empty:
        return {}

    datasets = datasets[
        datasets["source"].isin(["tickflow_correlation", "akshare_correlation"])
        & datasets["data_type"].isin(["cn_etf_correlation_raw", "us_correlation_raw", "futures_main_correlation_raw"])
    ].copy()
    if datasets.empty:
        return {}
    datasets["last_update_time"] = pd.to_datetime(datasets["last_update_time"], errors="coerce")
    datasets = datasets.sort_values("last_update_time", ascending=False)

    items = {}
    for _, row in datasets.iterrows():
        try:
            raw_df = pd.read_csv(row["file_path"])
            category = source_to_category_from_dataset(row["source"], row["data_type"])
            symbol = cached_symbol_from_dataset(str(row["symbol"]), category)
            name = cached_name_from_dataset(str(row["name"]), symbol, category)
            item = normalize_price_dataframe(raw_df, fallback_name=name, fallback_symbol=symbol)
            label = format_asset_label_text(correlation_item_label(item))
            items.setdefault(label, item)
        except Exception:
            continue
    return items


def source_to_category_from_dataset(source: str, data_type: str) -> str:
    if source == "akshare_correlation" or data_type == "futures_main_correlation_raw":
        return "期货主连"
    if data_type == "us_correlation_raw":
        return "美股"
    if data_type == "cn_etf_correlation_raw":
        return "A股ETF/股票"
    return "其他"


def cached_symbol_from_dataset(symbol: str, category: str) -> str:
    if category == "A股ETF/股票" and symbol.startswith("correlation_cn_"):
        clean = symbol.removeprefix("correlation_cn_v2_").removeprefix("correlation_cn_")
        for suffix in sorted((f"_{mode}" for mode in FUND_ADJUSTMENT_VALUES), key=len, reverse=True):
            if clean.endswith(suffix):
                return clean[: -len(suffix)]
        return clean
    if category == "美股" and symbol.startswith("correlation_us_"):
        clean = symbol.removeprefix("correlation_us_v2_").removeprefix("correlation_us_")
        for suffix in sorted((f"_{mode}" for mode in FUND_ADJUSTMENT_VALUES), key=len, reverse=True):
            if clean.endswith(suffix):
                return clean[: -len(suffix)]
        return clean
    if category == "期货主连" and symbol.startswith("correlation_futures_"):
        return futures_product_name(symbol.removeprefix("correlation_futures_"))
    return symbol


def cached_name_from_dataset(name: str, symbol: str, category: str) -> str:
    for suffix in (f" {label}" for label in FUND_ADJUSTMENT_OPTIONS):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    if category == "期货主连":
        return futures_product_name(symbol)
    return name or symbol


def correlation_item_label(item: object) -> str:
    name = str(getattr(item, "name", "")).strip()
    symbol = str(getattr(item, "symbol", "")).strip()
    if not name:
        return symbol
    if not symbol or name == symbol:
        return name
    return f"{name} {symbol}".strip()


def category_to_source_summary(category: str) -> str:
    mapping = {
        "A股ETF/股票": "A股ETF",
        "美股": "美股",
        "期货主连": "期货主连",
        "上传文件": "上传文件",
        "跨资产": "跨资产",
    }
    return mapping.get(category, category)


def classify_asset_category(asset: str, source_summary: str) -> str:
    source = source_summary.split("；", 1)[0]
    source_parts = [part.strip() for part in source.split("/") if part.strip()]
    if len(source_parts) == 1:
        mapped = source_to_category(source_parts[0])
        if mapped:
            return mapped

    text = asset.strip()
    upper = text.upper()
    if is_futures_asset(text):
        return "期货主连"
    if ".US" in upper:
        return "美股"
    if ".SH" in upper or ".SZ" in upper or (upper[:6].isdigit() and len(upper) >= 6):
        return "A股ETF/股票"
    if "上传文件" in source:
        return "上传文件"
    return "其他"


def source_to_category(source: str) -> str | None:
    mapping = {
        "A股ETF": "A股ETF/股票",
        "美股": "美股",
        "期货主连": "期货主连",
        "上传文件": "上传文件",
    }
    return mapping.get(source)


def is_futures_asset(asset: str) -> bool:
    normalized = format_futures_asset_text(asset)
    if normalized in CONTRACT_PREFIXES.values():
        return True
    for part in asset.upper().split():
        if part.endswith("0") and part[:-1] in CONTRACT_PREFIXES:
            return True
    return False


def category_sort_key(category: str) -> int:
    order = ["A股ETF/股票", "美股", "期货主连", "上传文件", "跨资产", "其他"]
    try:
        return order.index(category)
    except ValueError:
        return len(order)


def summarize_date_range(df: pd.DataFrame) -> str:
    ranges = {
        (display_value(row["开始日期"]), display_value(row["结束日期"]))
        for _, row in df.iterrows()
    }
    if len(ranges) == 1:
        start_date, end_date = next(iter(ranges))
        return f"{start_date} → {end_date}"
    return "各 pair 自动共同区间"


def summarize_common_days(df: pd.DataFrame) -> str:
    values = {display_value(value) for value in df["共同日期数"].tolist()}
    if len(values) == 1:
        return next(iter(values))
    return "各 pair 不同"


def summarize_methods(df: pd.DataFrame) -> str:
    methods = {extract_correlation_method(row.get("计算说明")) for _, row in df.iterrows()}
    methods.discard("相关系数")
    if len(methods) == 1:
        return next(iter(methods))
    if methods:
        return "各 pair 不同"
    return "历史记录"


def extract_correlation_method(source_summary: object) -> str:
    text = display_value(source_summary)
    if "日收益率相关" in text:
        return "日收益率相关"
    if "收盘价相关" in text:
        return "收盘价相关"
    return "相关系数"


def display_value(value: object) -> str:
    if value is None:
        return "-"
    if pd.isna(value):
        return "-"
    text = str(value).strip()
    return text or "-"


def build_saved_matrix(df: pd.DataFrame) -> pd.DataFrame:
    assets = []
    for _, row in df.iterrows():
        for column in ("标的A", "标的B"):
            asset = format_asset_label_text(row[column])
            if asset not in assets:
                assets.append(asset)

    matrix = pd.DataFrame(index=assets, columns=assets, dtype=float)
    for asset in assets:
        matrix.loc[asset, asset] = 1.0
    for _, row in df.iterrows():
        asset_a = format_asset_label_text(row["标的A"])
        asset_b = format_asset_label_text(row["标的B"])
        value = round(float(row["相关系数r"]), 4)
        matrix.loc[asset_a, asset_b] = value
        matrix.loc[asset_b, asset_a] = value
    return matrix.astype(object).where(pd.notna(matrix), "-")


def format_asset_label_text(value: object) -> str:
    return format_futures_asset_text(str(value))


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


if not calculate_clicked:
    render_results_panel()
    st.info("在数据来源区域任意输入 A股ETF、美股、期货主连或上传文件，至少两个标的即可混合计算相关系数。上传文件需要包含日期列和收盘价/净值列。")
    st.stop()

items = []
errors = []
adjust_value = FUND_ADJUSTMENT_OPTIONS[adjust_option]

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
            cache_symbol = build_fund_cache_symbol("correlation_cn", symbol, adjust_value)
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
            cache_symbol = build_fund_cache_symbol("correlation_us", symbol, adjust_value)
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

    method_value = "return" if correlation_method == "日收益率相关" else "price"
    result = calculate_price_correlation(items, method=method_value)
except Exception as exc:
    st.error(f"相关性计算出错：{exc}")
    st.stop()

summary = result.summary
save_correlation_results(
    result.pair_table,
    summary,
    source_summary=f"{' / '.join(dict.fromkeys(source_names))}；{correlation_method}",
)

metric_cols = st.columns(4)
with metric_cols[0]:
    st.metric("标的数量", summary.get("标的数量"))
with metric_cols[1]:
    st.metric("共同日期数", summary.get("共同日期数"))
with metric_cols[2]:
    st.metric("平均相关系数r", summary.get("平均相关系数r"))
with metric_cols[3]:
    st.metric("时间区间", f"{summary.get('开始日期')} → {summary.get('结束日期')}")

render_results_panel()

with st.expander("本次计算明细", expanded=False):
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
