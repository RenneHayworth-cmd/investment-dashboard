from __future__ import annotations

import time
from datetime import datetime
from threading import Lock
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from core.cache import load_dataset, save_dataset


EASTMONEY_CLIST_URLS = (
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://36.push2.eastmoney.com/api/qt/clist/get",
    "https://48.push2.eastmoney.com/api/qt/clist/get",
    "https://push2delay.eastmoney.com/api/qt/clist/get",
)
EASTMONEY_HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://quote.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

CONSTITUENT_SNAPSHOT_SYMBOL = "microcap_bk1158_constituent_snapshots"
CONSTITUENT_SNAPSHOT_SOURCE = "eastmoney"
CONSTITUENT_SNAPSHOT_DATA_TYPE = "microcap_constituent_snapshot"
_CACHE_WRITE_LOCK = Lock()


def _to_float(value) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_microcap_stocks(page_size: int = 500, retries: int = 3) -> pd.DataFrame:
    """Fetch EastMoney BK1158 constituents sorted by ascending total market cap."""
    requested_count = max(1, min(int(page_size), 1000))
    api_page_size = min(100, requested_count)
    retries = max(1, int(retries))
    last_error: Exception | None = None
    stocks = []
    seen_codes = set()
    total_count = None
    page = 1

    while len(stocks) < requested_count:
        params = {
            "pn": page,
            "pz": api_page_size,
            "po": 0,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f20",
            "fs": "b:BK1158",
            "fields": "f12,f14,f20,f2,f3,f5,f6,f124",
        }
        payload = None
        for attempt in range(retries):
            for trust_env in (False, True):
                session = requests.Session()
                session.trust_env = trust_env
                for url in EASTMONEY_CLIST_URLS:
                    try:
                        response = session.get(url, params=params, headers=EASTMONEY_HEADERS, timeout=15)
                        response.raise_for_status()
                        data = response.json()
                        if data.get("data", {}).get("diff"):
                            payload = data
                            break
                    except Exception as exc:
                        last_error = exc
                if payload is not None:
                    break
            if payload is not None:
                break
            if attempt < retries - 1:
                time.sleep(0.5)

        if payload is None:
            if stocks:
                break
            raise RuntimeError(f"东方财富 BK1158 获取失败：{last_error}")

        data = payload.get("data") or {}
        total_count = int(data.get("total") or 0) or total_count
        page_stocks = data.get("diff") or []
        if not page_stocks:
            break

        for stock in page_stocks:
            code = str(stock.get("f12", "")).strip()
            if not code or code in seen_codes:
                continue
            stocks.append(stock)
            seen_codes.add(code)
            if len(stocks) >= requested_count:
                break

        if total_count and page * api_page_size >= total_count:
            break
        page += 1

    fetched_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    rows = []
    for stock in stocks:
        market_cap = _to_float(stock.get("f20"))
        latest_price = _to_float(stock.get("f2"))
        change_pct = _to_float(stock.get("f3"))
        volume = _to_float(stock.get("f5"))
        turnover = _to_float(stock.get("f6"))
        quote_timestamp = _to_float(stock.get("f124"))
        quote_date = fetched_at.strftime("%Y-%m-%d")
        if quote_timestamp and quote_timestamp > 0:
            quote_date = datetime.fromtimestamp(quote_timestamp, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        if market_cap is None or market_cap <= 0:
            continue
        rows.append(
            {
                "代码": str(stock.get("f12", "")).strip(),
                "名称": str(stock.get("f14", "")).strip(),
                "最新价": latest_price,
                "涨跌幅(%)": change_pct,
                "成交量": volume,
                "成交额": turnover,
                "总市值(亿元)": round(market_cap / 1e8, 2),
                "是否停牌": bool((volume is not None and volume <= 0) or (turnover is not None and turnover <= 0)),
                "日期": quote_date,
                "更新时间": fetched_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    if not rows:
        raise RuntimeError("东方财富 BK1158 未返回有效市值数据。")

    result = pd.DataFrame(rows)
    result = result.sort_values("总市值(亿元)").reset_index(drop=True)
    result.insert(0, "排名", range(1, len(result) + 1))
    return result


def filter_active_microcap_stocks(df):
    """Return stocks with tradable quote data, excluding suspended names when detectable."""
    if df is None or df.empty:
        return pd.DataFrame()

    result = df.copy()
    for column in ("最新价", "总市值(亿元)", "成交量", "成交额"):
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    mask = pd.Series(True, index=result.index)
    if "最新价" in result.columns:
        mask &= result["最新价"].notna() & (result["最新价"] > 0)
    if "总市值(亿元)" in result.columns:
        mask &= result["总市值(亿元)"].notna() & (result["总市值(亿元)"] > 0)
    if "是否停牌" in result.columns:
        suspended = result["是否停牌"]
        if suspended.dtype == object:
            suspended = suspended.astype(str).str.lower().isin({"true", "1", "是", "停牌", "yes"})
        mask &= ~suspended.fillna(False).astype(bool)
    if "成交量" in result.columns:
        mask &= result["成交量"].isna() | (result["成交量"] > 0)
    if "成交额" in result.columns:
        mask &= result["成交额"].isna() | (result["成交额"] > 0)
    return result.loc[mask].copy()


def build_microcap_summary(
    df: pd.DataFrame,
    top_n: int = 30,
    average_count: int = 20,
    median_pool_count: int = 400,
) -> dict[str, object]:
    if df is None or df.empty:
        return {
            "成分股数量": 0,
            "展示数量": 0,
            "中位数市值(亿元)": None,
            "微盘20均值(亿元)": None,
            "可交易股票数": 0,
            "停牌剔除数": 0,
        }

    active_df = filter_active_microcap_stocks(df)
    top_df = df.head(max(1, int(top_n))).copy()
    average_df = active_df.head(max(1, int(average_count))).copy()
    average_market_caps = pd.to_numeric(average_df["总市值(亿元)"], errors="coerce").dropna()
    median_pool = active_df.head(max(1, int(median_pool_count))).copy()
    median_market_caps = pd.to_numeric(median_pool["总市值(亿元)"], errors="coerce").dropna().reset_index(drop=True)
    median_rank_index = min(199, len(median_market_caps) - 1) if not median_market_caps.empty else None
    return {
        "成分股数量": len(df),
        "可交易股票数": len(active_df),
        "停牌剔除数": max(0, len(df) - len(active_df)),
        "展示数量": len(top_df),
        "中位数市值(亿元)": round(float(median_market_caps.iloc[median_rank_index]), 2)
        if median_rank_index is not None
        else None,
        "微盘20均值(亿元)": round(float(average_market_caps.mean()), 2) if not average_market_caps.empty else None,
    }


def _normalize_stock_code(value: object) -> str:
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text.zfill(6) if text.isdigit() else text


def normalize_microcap_constituent_snapshots(
    df: pd.DataFrame | None,
    snapshot_date: str | None = None,
) -> pd.DataFrame:
    columns = [
        "快照日期",
        "快照时间",
        "排名",
        "代码",
        "名称",
        "最新价",
        "涨跌幅(%)",
        "成交量",
        "成交额",
        "总市值(亿元)",
        "是否停牌",
    ]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)

    result = df.copy()
    if snapshot_date:
        result["快照日期"] = snapshot_date
    elif "快照日期" not in result.columns and "日期" in result.columns:
        result["快照日期"] = result["日期"]
    if "快照时间" not in result.columns:
        if "更新时间" in result.columns:
            result["快照时间"] = result["更新时间"]
        else:
            result["快照时间"] = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")

    missing = {"快照日期", "代码", "名称", "总市值(亿元)"} - set(result.columns)
    if missing:
        raise ValueError(f"成分快照缺少必要列：{', '.join(sorted(missing))}")

    result["快照日期"] = pd.to_datetime(result["快照日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    result["快照时间"] = pd.to_datetime(result["快照时间"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    result["代码"] = result["代码"].map(_normalize_stock_code)
    result["名称"] = result["名称"].astype(str).str.strip()
    for column in ("最新价", "涨跌幅(%)", "成交量", "成交额", "总市值(亿元)"):
        if column not in result.columns:
            result[column] = pd.NA
        result[column] = pd.to_numeric(result[column], errors="coerce")
    if "是否停牌" not in result.columns:
        result["是否停牌"] = False
    else:
        suspended = result["是否停牌"]
        if suspended.dtype == object:
            suspended = suspended.astype(str).str.lower().isin({"true", "1", "是", "停牌", "yes"})
        result["是否停牌"] = suspended.fillna(False).astype(bool)
    result = result.dropna(subset=["快照日期", "总市值(亿元)"])
    result = result[(result["代码"] != "") & (result["总市值(亿元)"] > 0)]
    result = result.sort_values(["快照日期", "总市值(亿元)"])
    result["排名"] = result.groupby("快照日期").cumcount() + 1
    return result[columns].reset_index(drop=True)


def save_microcap_constituent_snapshot(
    stocks_df: pd.DataFrame,
    pool_count: int = 400,
) -> tuple[pd.DataFrame, str]:
    """Append or replace today's BK1158 constituent snapshot in the shared cache."""
    if stocks_df is None or stocks_df.empty:
        raise ValueError("没有可保存的微盘股成分数据。")

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    snapshot_date = now.strftime("%Y-%m-%d")
    if "日期" in stocks_df.columns:
        market_dates = pd.to_datetime(stocks_df["日期"], errors="coerce").dropna()
        if not market_dates.empty:
            snapshot_date = market_dates.max().strftime("%Y-%m-%d")
    elif "更新时间" in stocks_df.columns:
        fetched_times = pd.to_datetime(stocks_df["更新时间"], errors="coerce").dropna()
        if not fetched_times.empty:
            snapshot_date = fetched_times.max().strftime("%Y-%m-%d")

    fresh = normalize_microcap_constituent_snapshots(stocks_df, snapshot_date=snapshot_date)
    fresh = fresh.head(max(1, int(pool_count))).copy()
    cached_df, _ = load_dataset(
        CONSTITUENT_SNAPSHOT_SYMBOL,
        CONSTITUENT_SNAPSHOT_SOURCE,
        CONSTITUENT_SNAPSHOT_DATA_TYPE,
    )
    cached = normalize_microcap_constituent_snapshots(cached_df)
    cached = cached[cached["快照日期"] != snapshot_date]
    merged = pd.concat([cached, fresh], ignore_index=True)
    merged = merged.drop_duplicates(["快照日期", "代码"], keep="last")
    merged = normalize_microcap_constituent_snapshots(merged)

    with _CACHE_WRITE_LOCK:
        save_dataset(
            symbol=CONSTITUENT_SNAPSHOT_SYMBOL,
            name="BK1158 微盘股每日成分快照",
            source=CONSTITUENT_SNAPSHOT_SOURCE,
            data_type=CONSTITUENT_SNAPSHOT_DATA_TYPE,
            df=merged,
        )
    return merged, snapshot_date


def load_microcap_constituent_snapshots() -> tuple[pd.DataFrame, dict | None]:
    cached_df, meta = load_dataset(
        CONSTITUENT_SNAPSHOT_SYMBOL,
        CONSTITUENT_SNAPSHOT_SOURCE,
        CONSTITUENT_SNAPSHOT_DATA_TYPE,
    )
    return normalize_microcap_constituent_snapshots(cached_df), meta


def build_microcap_snapshot_metrics(
    snapshots_df: pd.DataFrame,
    pool_count: int = 400,
    micro_count: int = 20,
    median_rank: int = 200,
) -> pd.DataFrame:
    snapshots = normalize_microcap_constituent_snapshots(snapshots_df)
    metric_columns = [
        "日期",
        "有效股票数",
        f"第{median_rank}名市值(亿元)",
        f"第{median_rank}名股票",
        f"微盘{micro_count}均值(亿元)",
        "口径",
    ]
    if snapshots.empty:
        return pd.DataFrame(columns=metric_columns)

    rows = []
    pool_count = max(1, int(pool_count))
    micro_count = max(1, int(micro_count))
    median_rank = max(1, int(median_rank))
    for snapshot_date, group in snapshots.groupby("快照日期"):
        sorted_group = filter_active_microcap_stocks(group).sort_values("总市值(亿元)").head(pool_count).reset_index(drop=True)
        median_value = pd.NA
        median_stock = pd.NA
        if len(sorted_group) >= median_rank:
            median_row = sorted_group.iloc[median_rank - 1]
            median_value = median_row["总市值(亿元)"]
            median_stock = f"{median_row['代码']} {median_row['名称']}"
        micro_mean = pd.NA
        if len(sorted_group) >= micro_count:
            micro_mean = sorted_group.head(micro_count)["总市值(亿元)"].mean()
        rows.append(
            {
                "日期": pd.Timestamp(snapshot_date),
                "有效股票数": len(sorted_group),
                f"第{median_rank}名市值(亿元)": median_value,
                f"第{median_rank}名股票": median_stock,
                f"微盘{micro_count}均值(亿元)": micro_mean,
                "口径": "真实成分快照",
            }
        )

    metrics = pd.DataFrame(rows, columns=metric_columns).sort_values("日期").reset_index(drop=True)
    for column in (f"第{median_rank}名市值(亿元)", f"微盘{micro_count}均值(亿元)"):
        metrics[column] = pd.to_numeric(metrics[column], errors="coerce").round(2)
    return metrics
