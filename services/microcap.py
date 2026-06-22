from __future__ import annotations

import time
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
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
EASTMONEY_KLINE_URLS = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://91.push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://45.push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://7.push2his.eastmoney.com/api/qt/stock/kline/get",
    "http://push2his.eastmoney.com/api/qt/stock/kline/get",
    "http://91.push2his.eastmoney.com/api/qt/stock/kline/get",
    "http://45.push2his.eastmoney.com/api/qt/stock/kline/get",
    "http://7.push2his.eastmoney.com/api/qt/stock/kline/get",
)

EASTMONEY_HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://quote.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

STOCK_HISTORY_SOURCE = "eastmoney"
STOCK_HISTORY_DATA_TYPE = "microcap_stock_daily"
CONSTITUENT_SNAPSHOT_SYMBOL = "microcap_bk1158_constituent_snapshots"
CONSTITUENT_SNAPSHOT_SOURCE = "eastmoney"
CONSTITUENT_SNAPSHOT_DATA_TYPE = "microcap_constituent_snapshot"
_CACHE_WRITE_LOCK = Lock()


@dataclass
class MicrocapHistoryResult:
    dataframe: pd.DataFrame
    raw_market_caps: pd.DataFrame
    errors: list[str]


@contextmanager
def without_proxy_env():
    proxy_keys = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    old_values = {key: os.environ.get(key) for key in proxy_keys}
    try:
        for key in proxy_keys:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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
            "fields": "f12,f14,f20,f2,f3,f124",
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
                "总市值(亿元)": round(market_cap / 1e8, 2),
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
        }

    top_df = df.head(max(1, int(top_n))).copy()
    average_df = df.head(max(1, int(average_count))).copy()
    average_market_caps = pd.to_numeric(average_df["总市值(亿元)"], errors="coerce").dropna()
    median_pool = df.head(max(1, int(median_pool_count))).copy()
    median_market_caps = pd.to_numeric(median_pool["总市值(亿元)"], errors="coerce").dropna().reset_index(drop=True)
    median_rank_index = min(199, len(median_market_caps) - 1) if not median_market_caps.empty else None
    return {
        "成分股数量": len(df),
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
        "总市值(亿元)",
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
    for column in ("最新价", "涨跌幅(%)", "总市值(亿元)"):
        if column not in result.columns:
            result[column] = pd.NA
        result[column] = pd.to_numeric(result[column], errors="coerce")
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
        sorted_group = group.sort_values("总市值(亿元)").head(pool_count).reset_index(drop=True)
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


def stock_secid(code: str) -> str:
    code = str(code).strip()
    market = "1" if code.startswith(("5", "6", "9")) else "0"
    return f"{market}.{code}"


def tickflow_stock_symbol(code: str) -> str:
    code = str(code).strip()
    if code.startswith(("5", "6", "9")):
        return f"{code}.SH"
    if code.startswith(("0", "1", "2", "3")):
        return f"{code}.SZ"
    if code.startswith(("8", "4")):
        return f"{code}.BJ"
    raise ValueError(f"无法推断 TickFlow 股票代码后缀：{code}")


def fetch_stock_daily_close(code: str, days: int = 260) -> pd.DataFrame:
    days = max(1, min(int(days), 5000))
    params = {
        "secid": stock_secid(code),
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "0",
        "end": "20500101",
        "lmt": str(days),
    }
    last_error: Exception | None = None
    payload = None
    for attempt in range(2):
        for trust_env in (False, True):
            session = requests.Session()
            session.trust_env = trust_env
            session.proxies = {} if not trust_env else session.proxies
            for url in EASTMONEY_KLINE_URLS:
                try:
                    response = session.get(url, params=params, headers=EASTMONEY_HEADERS, timeout=4)
                    response.raise_for_status()
                    data = response.json()
                    if data.get("data", {}).get("klines"):
                        payload = data
                        break
                except Exception as exc:
                    last_error = exc
            if payload is not None:
                break
        if payload is not None:
            break
        if attempt < 1:
            time.sleep(0.25)

    if payload is None:
        try:
            return fetch_stock_daily_close_from_akshare(code, days=days)
        except Exception as akshare_exc:
            try:
                return fetch_stock_daily_close_from_tickflow(code, days=days)
            except Exception as tickflow_exc:
                raise RuntimeError(
                    f"{code} 历史收盘价获取失败：{last_error}；"
                    f"AkShare兜底失败：{akshare_exc}；"
                    f"TickFlow兜底失败：{tickflow_exc}"
                ) from tickflow_exc

    rows = []
    for item in payload.get("data", {}).get("klines") or []:
        fields = str(item).split(",")
        if len(fields) < 3:
            continue
        rows.append({"日期": fields[0], "收盘价": fields[2]})

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError(f"{code} 历史收盘价为空。")
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    df["收盘价"] = pd.to_numeric(df["收盘价"], errors="coerce")
    df = df.dropna(subset=["日期", "收盘价"]).sort_values("日期").reset_index(drop=True)
    if df.empty:
        raise RuntimeError(f"{code} 历史收盘价解析后为空。")
    return df


def fetch_stock_daily_close_from_akshare(code: str, days: int = 260) -> pd.DataFrame:
    import akshare as ak

    days = max(1, min(int(days), 5000))
    start_date = (datetime.now() - timedelta(days=days * 3 + 30)).strftime("%Y%m%d")
    with without_proxy_env():
        raw_df = ak.stock_zh_a_hist(
            symbol=str(code).strip(),
            period="daily",
            start_date=start_date,
            end_date="20500101",
            adjust="",
        )
    if raw_df is None or raw_df.empty:
        raise RuntimeError("AkShare 未返回个股日线。")

    rename_map = {}
    for column in raw_df.columns:
        text = str(column).strip()
        if text in {"日期", "date", "trade_date"}:
            rename_map[column] = "日期"
        elif text in {"收盘", "收盘价", "close"}:
            rename_map[column] = "收盘价"
    df = raw_df.rename(columns=rename_map)
    if "日期" not in df.columns or "收盘价" not in df.columns:
        raise RuntimeError(f"AkShare 返回列无法识别：{list(raw_df.columns)}")
    df = df[["日期", "收盘价"]].copy()
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    df["收盘价"] = pd.to_numeric(df["收盘价"], errors="coerce")
    df = df.dropna(subset=["日期", "收盘价"]).sort_values("日期").tail(days).reset_index(drop=True)
    if df.empty:
        raise RuntimeError("AkShare 个股日线解析后为空。")
    return df


def fetch_stock_daily_close_from_tickflow(code: str, days: int = 260, api_key: str = "") -> pd.DataFrame:
    from tickflow import TickFlow

    symbol = tickflow_stock_symbol(code)
    client = TickFlow(api_key=api_key or os.getenv("TICKFLOW_API_KEY", "")) if api_key or os.getenv("TICKFLOW_API_KEY") else TickFlow.free()
    raw_df = client.klines.get(symbol, period="1d", count=max(1, int(days)), as_dataframe=True)
    return normalize_tickflow_daily_close(raw_df, days=days)


def fetch_stock_daily_close_from_tickflow_client(code: str, client, days: int = 260) -> pd.DataFrame:
    symbol = tickflow_stock_symbol(code)
    last_error: Exception | None = None
    raw_df = None
    for attempt in range(3):
        try:
            raw_df = client.klines.get(symbol, period="1d", count=max(1, int(days)), as_dataframe=True)
            break
        except Exception as exc:
            last_error = exc
            if "限流" in str(exc) or "速率限制" in str(exc):
                return fetch_stock_daily_close_from_tickflow(code, days=days, api_key="")
            if attempt < 2:
                time.sleep(0.4)
    if raw_df is None:
        if last_error is not None and ("限流" in str(last_error) or "速率限制" in str(last_error)):
            return fetch_stock_daily_close_from_tickflow(code, days=days, api_key="")
        raise RuntimeError(last_error or "TickFlow 未返回个股日线。")
    return normalize_tickflow_daily_close(raw_df, days=days)


def normalize_tickflow_daily_close(raw_df: pd.DataFrame, days: int = 260) -> pd.DataFrame:
    if raw_df is None or raw_df.empty:
        raise RuntimeError("TickFlow 未返回个股日线。")

    normalized = raw_df.copy()
    normalized.columns = [str(column).strip() for column in normalized.columns]
    if "trade_date" not in normalized.columns or "close" not in normalized.columns:
        raise RuntimeError(f"TickFlow 返回列无法识别：{list(raw_df.columns)}")
    df = normalized[["trade_date", "close"]].copy()
    df.columns = ["日期", "收盘价"]
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    df["收盘价"] = pd.to_numeric(df["收盘价"], errors="coerce")
    df = df.dropna(subset=["日期", "收盘价"]).sort_values("日期").tail(days).reset_index(drop=True)
    if df.empty:
        raise RuntimeError("TickFlow 个股日线解析后为空。")
    return df


def normalize_stock_daily_close(df: pd.DataFrame, days: int | None = None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["日期", "收盘价"])

    result = df.copy()
    result.columns = [str(column).strip() for column in result.columns]
    rename_map = {}
    for column in result.columns:
        text = str(column).strip()
        if text in {"日期", "date", "trade_date"}:
            rename_map[column] = "日期"
        elif text in {"收盘价", "收盘", "close"}:
            rename_map[column] = "收盘价"
    result = result.rename(columns=rename_map)
    if "日期" not in result.columns or "收盘价" not in result.columns:
        raise RuntimeError(f"历史日线缓存列无法识别：{list(df.columns)}")
    result = result[["日期", "收盘价"]].copy()
    result["日期"] = pd.to_datetime(result["日期"], errors="coerce")
    result["收盘价"] = pd.to_numeric(result["收盘价"], errors="coerce")
    result = result.dropna(subset=["日期", "收盘价"])
    result = result.sort_values("日期").drop_duplicates("日期", keep="last")
    if days is not None:
        result = result.tail(max(1, int(days)))
    return result.reset_index(drop=True)


def stock_history_cache_symbol(code: str) -> str:
    return f"microcap_stock_daily_{str(code).strip()}"


def merge_stock_daily_close(old_df: pd.DataFrame | None, new_df: pd.DataFrame, days: int) -> pd.DataFrame:
    old_norm = normalize_stock_daily_close(old_df) if old_df is not None else pd.DataFrame()
    new_norm = normalize_stock_daily_close(new_df)
    merged = pd.concat([old_norm, new_norm], ignore_index=True)
    return normalize_stock_daily_close(merged, days=days)


def fetch_stock_daily_close_preferred(
    code: str,
    days: int,
    tickflow_client=None,
) -> pd.DataFrame:
    if tickflow_client is not None:
        try:
            return fetch_stock_daily_close_from_tickflow_client(code, tickflow_client, days=days)
        except Exception:
            pass
    return fetch_stock_daily_close(code, days=days)


def load_or_fetch_stock_daily_close(
    code: str,
    name: str,
    days: int,
    incremental_days: int = 80,
    tickflow_client=None,
) -> pd.DataFrame:
    days = max(1, int(days))
    incremental_days = max(1, min(int(incremental_days), days))
    cache_symbol = stock_history_cache_symbol(code)
    cached_df, _ = load_dataset(
        cache_symbol,
        STOCK_HISTORY_SOURCE,
        STOCK_HISTORY_DATA_TYPE,
    )

    if cached_df is None or cached_df.empty:
        full_df = fetch_stock_daily_close_preferred(code, days=days, tickflow_client=tickflow_client)
        full_df = normalize_stock_daily_close(full_df, days=days)
        with _CACHE_WRITE_LOCK:
            save_dataset(
                symbol=cache_symbol,
                name=f"{code} {name} 微盘股历史日线",
                source=STOCK_HISTORY_SOURCE,
                data_type=STOCK_HISTORY_DATA_TYPE,
                df=full_df,
            )
        return full_df

    cached_norm = normalize_stock_daily_close(cached_df, days=days)
    required_cache_rows = max(1, int(days * 0.8))
    fetch_days = days if len(cached_norm) < required_cache_rows else incremental_days
    try:
        update_df = fetch_stock_daily_close_preferred(
            code,
            days=fetch_days,
            tickflow_client=tickflow_client,
        )
        merged = merge_stock_daily_close(cached_norm, update_df, days=days)
        with _CACHE_WRITE_LOCK:
            save_dataset(
                symbol=cache_symbol,
                name=f"{code} {name} 微盘股历史日线",
                source=STOCK_HISTORY_SOURCE,
                data_type=STOCK_HISTORY_DATA_TYPE,
                df=merged,
            )
        return merged
    except Exception:
        return cached_norm


def fetch_microcap_history_metrics(
    stocks_df: pd.DataFrame,
    days: int = 260,
    pool_count: int = 400,
    micro_count: int = 20,
    median_rank: int = 200,
    max_workers: int = 8,
    tickflow_api_key: str = "",
    incremental_days: int = 80,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> MicrocapHistoryResult:
    if stocks_df is None or stocks_df.empty:
        raise ValueError("请先获取微盘股成分数据。")

    pool = stocks_df.head(max(1, int(pool_count))).copy()
    pool["最新价"] = pd.to_numeric(pool["最新价"], errors="coerce")
    pool["总市值(亿元)"] = pd.to_numeric(pool["总市值(亿元)"], errors="coerce")
    pool = pool.dropna(subset=["代码", "名称", "最新价", "总市值(亿元)"])
    pool = pool[(pool["最新价"] > 0) & (pool["总市值(亿元)"] > 0)]
    if pool.empty:
        raise ValueError("微盘股成分数据缺少有效价格或市值。")

    max_workers = max(1, min(int(max_workers), len(pool)))
    raw_frames = []
    errors = []
    tickflow_client = None
    try:
        from tickflow import TickFlow

        api_key = tickflow_api_key or os.getenv("TICKFLOW_API_KEY", "")
        tickflow_client = TickFlow(api_key=api_key) if api_key else TickFlow.free()
    except Exception:
        tickflow_client = None

    def fetch_one(row: pd.Series) -> pd.DataFrame:
        code = str(row["代码"])
        name = str(row["名称"])
        market_cap_factor = float(row["总市值(亿元)"]) / float(row["最新价"])
        daily = load_or_fetch_stock_daily_close(
            code,
            name,
            days=days,
            incremental_days=incremental_days,
            tickflow_client=tickflow_client,
        )
        daily["代码"] = code
        daily["名称"] = name
        daily["总市值(亿元)"] = daily["收盘价"] * market_cap_factor
        return daily[["日期", "代码", "名称", "收盘价", "总市值(亿元)"]]

    completed = 0
    total = len(pool)
    if progress_callback:
        progress_callback(0, total, "")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(fetch_one, row): row for _, row in pool.iterrows()}
        for future in as_completed(future_map):
            row = future_map[future]
            label = f"{row.get('代码')} {row.get('名称')}"
            try:
                raw_frames.append(future.result())
            except Exception as exc:
                errors.append(f"{label}: {exc}")
            finally:
                completed += 1
                if progress_callback:
                    progress_callback(completed, total, label)

    if not raw_frames:
        raise RuntimeError("未获取到任何微盘股历史收盘价。" + " | ".join(errors[:5]))
    if len(raw_frames) < median_rank:
        raise RuntimeError(
            f"有效历史数据股票数不足：{len(raw_frames)} / {median_rank}。"
            f"请降低历史并发数后重试。部分失败：{' | '.join(errors[:5])}"
        )

    raw_df = pd.concat(raw_frames, ignore_index=True)
    raw_df["总市值(亿元)"] = pd.to_numeric(raw_df["总市值(亿元)"], errors="coerce")
    raw_df = raw_df.dropna(subset=["日期", "总市值(亿元)"])

    metric_rows = []
    micro_count = max(1, int(micro_count))
    median_rank = max(1, int(median_rank))
    for date, group in raw_df.groupby("日期"):
        sorted_group = group.sort_values("总市值(亿元)").reset_index(drop=True)
        if sorted_group.empty:
            continue
        median_value = pd.NA
        median_stock = pd.NA
        if len(sorted_group) >= median_rank:
            median_row = sorted_group.iloc[median_rank - 1]
            median_value = median_row["总市值(亿元)"]
            median_stock = f"{median_row['代码']} {median_row['名称']}"
        micro_mean = pd.NA
        if len(sorted_group) >= micro_count:
            micro_mean = sorted_group.head(micro_count)["总市值(亿元)"].mean()
        metric_rows.append(
            {
                "日期": date,
                "有效股票数": len(sorted_group),
                f"第{median_rank}名市值(亿元)": median_value,
                f"第{median_rank}名股票": median_stock,
                f"微盘{micro_count}均值(亿元)": micro_mean,
            }
        )

    metrics_df = pd.DataFrame(metric_rows).sort_values("日期").reset_index(drop=True)
    metric_value_cols = [f"第{median_rank}名市值(亿元)", f"微盘{micro_count}均值(亿元)"]
    for column in metric_value_cols:
        if column in metrics_df.columns:
            metrics_df[column] = pd.to_numeric(metrics_df[column], errors="coerce").round(2)
    valid_metric_col = f"第{median_rank}名市值(亿元)"
    if valid_metric_col in metrics_df.columns and metrics_df[valid_metric_col].dropna().empty:
        raise RuntimeError(
            f"没有任何交易日达到 {median_rank} 只有效股票，无法计算第 {median_rank} 名市值。"
            f"请缩短历史天数或降低历史并发数后重试。"
        )
    return MicrocapHistoryResult(metrics_df, raw_df, errors)
