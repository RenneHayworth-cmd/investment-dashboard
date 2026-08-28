from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
import re
from threading import Lock
from zoneinfo import ZoneInfo

import pandas as pd

from core.cache import load_dataset, save_dataset
from services.index_ma20 import (
    INDEX_CONFIG,
    INDEX_FINAL_HISTORY_SOURCE,
    INDEX_SOURCE_CORRECTION_SOURCE,
    fetch_yahoo_chart_payload,
    missing_recent_market_trade_dates,
    raw_cache_symbol,
    source_correction_start,
)
from services.market_calendar import (
    get_market_window,
    is_market_trading_day,
    latest_settled_trade_date,
    previous_trading_day,
)
from services.index_sources_sina import fetch_sina_hk_realtime_quote
from services.index_sources_mx import fetch_mx_realtime_quote


EASTMONEY_QUOTE_SECIDS = {
    "上证指数": "1.000001",
    "创业板指": "0.399006",
    "沪深300": "1.000300",
    "中证500": "1.000905",
    "中证1000": "1.000852",
    "中证2000": "2.932000",
    "微盘股": "90.BK1158",
    "科创50": "1.000688",
    "中证红利低波": "2.H30269",
    "国证自由现金流": "0.980092",
    "恒生科技": "124.HSTECH",
    "恒生港股通高息低波": "124.HSHYLV",
    "标普500": "100.SPX",
    "纳斯达克100": "100.NDX",
    "日经225": "100.N225",
    "韩国KOSPI": "100.KS11",
}

YAHOO_QUOTE_SYMBOLS = {
    "纳斯达克综合": "^IXIC",
    "VIX恐慌指数": "^VIX",
}

FUTURES_QUOTE_SYMBOLS = {
    "中证500期货主连": "IC0",
    "中证1000期货主连": "IM0",
    "铁矿石主连": "I0",
    "沪金主连": "AU0",
    "沪银主连": "AG0",
    "原油主连": "SC0",
}

EASTMONEY_FUTURES_QUOTE_SECIDS = {
    "原油主连": "142.scm",
}

FUTURES_MAIN_CONTRACT_PRODUCTS = {
    "中证500期货主连": ("IC0", "中证500指数期货"),
    "中证1000期货主连": ("IM0", "中证1000股指期货"),
    "铁矿石主连": ("I0", "铁矿石"),
    "沪金主连": ("AU0", "黄金"),
    "沪银主连": ("AG0", "白银"),
    "原油主连": ("SC0", "原油"),
}

FUTURES_MAIN_CONTRACT_CACHE_SYMBOL = "index_futures_main_contracts"
FUTURES_MAIN_CONTRACT_CACHE_SOURCE = "index_metadata"
LUNCH_QUOTE_MARKETS = {"A股", "港股", "日本"}
FUTURES_LUNCH_WINDOWS = {
    "IC0": (time(11, 30), time(13, 0)),
    "IM0": (time(11, 30), time(13, 0)),
}

FUTURES_TRADING_SESSIONS = {
    "IC0": (
        (time(9, 30), time(11, 30)),
        (time(13, 0), time(15, 0)),
    ),
    "IM0": (
        (time(9, 30), time(11, 30)),
        (time(13, 0), time(15, 0)),
    ),
    "I0": (
        (time(9, 0), time(10, 15)),
        (time(10, 30), time(11, 30)),
        (time(13, 30), time(15, 0)),
        (time(21, 0), time(23, 0)),
    ),
    "AU0": (
        (time(0, 0), time(2, 30)),
        (time(9, 0), time(10, 15)),
        (time(10, 30), time(11, 30)),
        (time(13, 30), time(15, 0)),
        (time(21, 0), time(23, 59, 59)),
    ),
    "AG0": (
        (time(0, 0), time(2, 30)),
        (time(9, 0), time(10, 15)),
        (time(10, 30), time(11, 30)),
        (time(13, 30), time(15, 0)),
        (time(21, 0), time(23, 59, 59)),
    ),
    "SC0": (
        (time(0, 0), time(2, 30)),
        (time(9, 0), time(10, 15)),
        (time(10, 30), time(11, 30)),
        (time(13, 30), time(15, 0)),
        (time(21, 0), time(23, 59, 59)),
    ),
}

POST_CLOSE_DAILY_DELAY = timedelta(minutes=10)
INDEX_SOURCE_LABELS = {
    "akshare_cn": "AkShare A股日线",
    "akshare_cni": "国证官方日线",
    "akshare_csindex": "中证官方日线",
    "akshare_us": "AkShare 美股日线",
    "akshare_hk": "AkShare 港股日线",
    "akshare_global": "AkShare 全球指数日线",
    "akshare_futures_main": "AkShare 期货主连日线",
    "eastmoney_kline": "东方财富日线",
    "cboe_vix": "CBOE 官方日线",
}
_RUNTIME_QUOTE_CACHE: dict[str, dict[str, object]] = {}
_RUNTIME_QUOTE_CACHE_LOCK = Lock()


def _supported_realtime_index_names() -> set[str]:
    return set(EASTMONEY_QUOTE_SECIDS) | set(YAHOO_QUOTE_SYMBOLS) | set(FUTURES_QUOTE_SYMBOLS)


def remember_runtime_realtime_quotes(quotes: dict[str, dict[str, object]]) -> None:
    """Keep card-only quotes in process memory so a browser refresh can restore them."""
    if not quotes:
        return
    with _RUNTIME_QUOTE_CACHE_LOCK:
        for index_name, quote in quotes.items():
            _RUNTIME_QUOTE_CACHE[str(index_name)] = dict(quote)


def load_runtime_realtime_quotes() -> dict[str, dict[str, object]]:
    """Return an isolated copy of the transient card quote cache."""
    with _RUNTIME_QUOTE_CACHE_LOCK:
        return {
            index_name: dict(quote)
            for index_name, quote in _RUNTIME_QUOTE_CACHE.items()
        }


def _is_supported_market_lunch(index_name: str, *, now: datetime | None = None) -> bool:
    config = INDEX_CONFIG.get(index_name, {})
    market_name = str(config.get("market_group") or "")
    if market_name not in LUNCH_QUOTE_MARKETS:
        return False
    market = get_market_window(market_name)
    if market is None:
        return False
    market_now = now.astimezone(ZoneInfo(market.timezone)) if now else datetime.now(ZoneInfo(market.timezone))
    if not is_market_trading_day(market, market_now):
        return False
    futures_symbol = str(config.get("futures_symbol") or config.get("code") or "").upper()
    if futures_symbol in FUTURES_TRADING_SESSIONS:
        lunch_start, lunch_end = FUTURES_LUNCH_WINDOWS.get(
            futures_symbol,
            (time(11, 30), time(13, 30)),
        )
        return lunch_start <= market_now.time() < lunch_end
    if len(market.sessions) < 2:
        return False
    return bool(
        any(
            previous_end <= market_now.time() < next_start
            for (_, previous_end), (next_start, _) in zip(market.sessions, market.sessions[1:])
        )
    )


def manual_quote_request_names(
    completed_lunch_keys: set[str] | None = None,
    *,
    now: datetime | None = None,
) -> tuple[set[str], dict[str, str]]:
    """Select quotes for one manual refresh and deduplicate the mainland lunch close."""
    completed = completed_lunch_keys or set()
    names: set[str] = set()
    lunch_keys: dict[str, str] = {}
    for index_name in _supported_realtime_index_names():
        if quote_is_active_for_display(index_name, now=now):
            names.add(index_name)
        if not _is_supported_market_lunch(index_name, now=now):
            continue
        market_name = str(INDEX_CONFIG.get(index_name, {}).get("market_group") or "")
        market = get_market_window(market_name)
        market_now = now.astimezone(ZoneInfo(market.timezone)) if now else datetime.now(ZoneInfo(market.timezone))
        key = f"{index_name}:{market_now.date().isoformat()}:lunch"
        if key not in completed:
            names.add(index_name)
            lunch_keys[index_name] = key
    return names, lunch_keys


def quote_is_visible_for_manual_display(
    index_name: str,
    quote: dict[str, object],
    *,
    now: datetime | None = None,
) -> bool:
    if not (
        quote_is_active_for_display(index_name, now=now)
        or _is_supported_market_lunch(index_name, now=now)
    ):
        return False
    quote_time = quote.get("quote_time")
    if not isinstance(quote_time, datetime):
        return False
    market_name = str(INDEX_CONFIG.get(index_name, {}).get("market_group") or "")
    market = get_market_window(market_name)
    market_now = now.astimezone(ZoneInfo(market.timezone)) if now else datetime.now(ZoneInfo(market.timezone))
    quote_market = quote_time.astimezone(ZoneInfo(market.timezone)) if quote_time.tzinfo else quote_time
    return quote_market.date() == market_now.date()


def format_index_display_name(index_name: str, contract_names: dict[str, str] | None = None) -> str:
    contract = str((contract_names or {}).get(index_name) or "").strip().upper()
    return f"{index_name}（{contract}）" if contract else index_name


def _infer_main_contract_symbol(
    realtime_df: pd.DataFrame | None,
    main_symbol: str,
    *,
    reference_price: float | None = None,
    reference_position: float | None = None,
    reference_volume: float | None = None,
) -> str | None:
    if realtime_df is None or realtime_df.empty or "symbol" not in realtime_df.columns:
        return None

    data = realtime_df.copy()
    data["_symbol"] = data["symbol"].astype(str).str.strip().str.upper()
    prefix_match = re.match(r"[A-Z]+", str(main_symbol).upper())
    if prefix_match is None:
        return None
    contract_pattern = re.compile(rf"^{re.escape(prefix_match.group(0))}\d{{4}}$")
    candidates = data[data["_symbol"].map(lambda value: bool(contract_pattern.fullmatch(value)))].copy()
    if candidates.empty:
        return None

    candidates["_trade"] = pd.to_numeric(
        candidates["trade"] if "trade" in candidates.columns else pd.Series(index=candidates.index, dtype=float),
        errors="coerce",
    )
    candidates["_position"] = pd.to_numeric(
        candidates["position"] if "position" in candidates.columns else pd.Series(index=candidates.index, dtype=float),
        errors="coerce",
    ).fillna(-1)
    candidates["_volume"] = pd.to_numeric(
        candidates["volume"] if "volume" in candidates.columns else pd.Series(index=candidates.index, dtype=float),
        errors="coerce",
    ).fillna(-1)
    price = pd.to_numeric(reference_price, errors="coerce")
    if pd.isna(price):
        main_rows = data[data["_symbol"] == str(main_symbol).upper()]
        if not main_rows.empty:
            price = pd.to_numeric(main_rows.iloc[0].get("trade"), errors="coerce")
    sort_columns = []
    ascending = []
    if not pd.isna(price):
        candidates["_price_diff"] = (candidates["_trade"] - float(price)).abs()
        sort_columns.append("_price_diff")
        ascending.append(True)
    for reference, column, diff_column in (
        (reference_position, "_position", "_position_diff"),
        (reference_volume, "_volume", "_volume_diff"),
    ):
        numeric_reference = pd.to_numeric(reference, errors="coerce")
        if pd.isna(numeric_reference):
            continue
        candidates[diff_column] = (candidates[column] - float(numeric_reference)).abs()
        sort_columns.append(diff_column)
        ascending.append(True)
    sort_columns.extend(["_position", "_volume"])
    ascending.extend([False, False])
    candidates = candidates.sort_values(sort_columns, ascending=ascending)
    return str(candidates.iloc[0]["_symbol"])


def fetch_futures_main_contract_names(
    quotes: dict[str, dict[str, object]] | None = None,
) -> dict[str, str]:
    import akshare as ak

    resolved: dict[str, str] = {}
    for index_name, (main_symbol, product_name) in FUTURES_MAIN_CONTRACT_PRODUCTS.items():
        if quotes is not None and index_name not in quotes:
            continue
        reference_price = None if quotes is None else quotes[index_name].get("price")
        reference_position = None if quotes is None else quotes[index_name].get("position")
        reference_volume = None if quotes is None else quotes[index_name].get("volume")
        try:
            contracts = ak.futures_zh_realtime(symbol=product_name)
            contract = _infer_main_contract_symbol(
                contracts,
                main_symbol,
                reference_price=reference_price,
                reference_position=reference_position,
                reference_volume=reference_volume,
            )
        except Exception:
            contract = None
        if contract:
            resolved[index_name] = contract
    return resolved


def load_futures_main_contract_names() -> dict[str, str]:
    cached, _ = load_dataset(
        FUTURES_MAIN_CONTRACT_CACHE_SYMBOL,
        FUTURES_MAIN_CONTRACT_CACHE_SOURCE,
        "futures_main_contracts",
    )
    if cached is None or cached.empty or not {"index_name", "contract"}.issubset(cached.columns):
        return {}
    return {
        str(row["index_name"]): str(row["contract"]).strip().upper()
        for _, row in cached.dropna(subset=["index_name", "contract"]).iterrows()
    }


def save_futures_main_contract_names(contract_names: dict[str, str]) -> dict[str, str]:
    merged = load_futures_main_contract_names()
    merged.update({name: str(contract).strip().upper() for name, contract in contract_names.items() if contract})
    if not merged:
        return merged
    updated_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    rows = [
        {
            "index_name": index_name,
            "contract": contract,
            "date": updated_at.strftime("%Y-%m-%d"),
            "updated_at": updated_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
        for index_name, contract in sorted(merged.items())
    ]
    save_dataset(
        symbol=FUTURES_MAIN_CONTRACT_CACHE_SYMBOL,
        name="指数监控期货主连当前合约",
        source=FUTURES_MAIN_CONTRACT_CACHE_SOURCE,
        data_type="futures_main_contracts",
        df=pd.DataFrame(rows),
    )
    return merged


def _market_is_open(market_name: str, now: datetime | None = None) -> bool:
    market = get_market_window(market_name)
    if market is None:
        return False
    market_now = now.astimezone(ZoneInfo(market.timezone)) if now else datetime.now(ZoneInfo(market.timezone))
    if not is_market_trading_day(market, market_now):
        return False
    return any(start <= market_now.time() <= end for start, end in market.sessions)


def _futures_market_is_open(symbol: str, now: datetime | None = None) -> bool:
    market_now = now.astimezone(ZoneInfo("Asia/Shanghai")) if now else datetime.now(ZoneInfo("Asia/Shanghai"))
    market = get_market_window("A股")
    if market is None:
        return False
    current = market_now.time()
    sessions = FUTURES_TRADING_SESSIONS.get(symbol.upper(), ())
    if not sessions:
        return False
    if current <= time(2, 30):
        previous_night = market_now - timedelta(days=1)
        if not is_market_trading_day(market, previous_night):
            return False
    elif not is_market_trading_day(market, market_now):
        return False
    return any(start <= current <= end for start, end in sessions)


def _daily_update_target(
    index_name: str,
    *,
    now: datetime | None = None,
) -> date | None:
    config = INDEX_CONFIG.get(index_name, {})
    market = get_market_window(str(config.get("market_group") or ""))
    if market is None:
        return None
    market_now = now.astimezone(ZoneInfo(market.timezone)) if now else datetime.now(ZoneInfo(market.timezone))
    target_date = latest_settled_trade_date(
        market,
        market_now,
        settlement_delay=POST_CLOSE_DAILY_DELAY,
    )

    futures_symbol = str(config.get("futures_symbol") or config.get("code") or "").upper()
    if (
        futures_symbol in FUTURES_TRADING_SESSIONS
        and _futures_market_is_open(futures_symbol, now=now)
        and target_date == market_now.date()
    ):
        return previous_trading_day(market, market_now.date())
    return target_date


def daily_update_eligible_index_names(*, now: datetime | None = None) -> set[str]:
    """Return indexes with a safe completed-session target."""
    return {
        index_name
        for index_name in INDEX_CONFIG
        if _daily_update_target(index_name, now=now) is not None
    }


def quote_is_active_for_display(index_name: str, *, now: datetime | None = None) -> bool:
    """Keep cached intraday quotes only while that instrument is trading."""
    config = INDEX_CONFIG.get(index_name, {})
    futures_symbol = str(config.get("futures_symbol") or config.get("code") or "").upper()
    if futures_symbol in FUTURES_TRADING_SESSIONS:
        return _futures_market_is_open(futures_symbol, now=now)
    return _market_is_open(str(config.get("market_group") or ""), now=now)


def find_pending_post_close_index_names(
    *,
    now: datetime | None = None,
    index_names: set[str] | None = None,
) -> set[str]:
    """Find indexes missing a locally confirmed completed-session row."""
    selected = index_names or set(INDEX_CONFIG)
    pending: set[str] = set()
    for index_name in selected:
        config = INDEX_CONFIG.get(index_name)
        if config is None:
            continue
        target_date = _daily_update_target(index_name, now=now)
        if target_date is None:
            continue
        finalized_raw, _ = load_dataset(
            raw_cache_symbol(index_name, config),
            INDEX_FINAL_HISTORY_SOURCE,
            "index_daily_raw",
        )
        latest_date = pd.NaT
        if finalized_raw is not None and not finalized_raw.empty and "trade_date" in finalized_raw.columns:
            latest_date = pd.to_datetime(finalized_raw["trade_date"], errors="coerce").max()
        if pd.isna(latest_date) or latest_date.date() < target_date:
            pending.add(index_name)
            continue
        if missing_recent_market_trade_dates(
            finalized_raw,
            str(config.get("market_group") or ""),
            target_date,
        ):
            pending.add(index_name)
            continue
        correction_start = source_correction_start(config)
        if correction_start is None or target_date < correction_start.date():
            continue
        correction_raw, _ = load_dataset(
            raw_cache_symbol(index_name, config),
            INDEX_SOURCE_CORRECTION_SOURCE,
            "index_daily_raw",
        )
        correction_latest = pd.NaT
        if correction_raw is not None and not correction_raw.empty and "trade_date" in correction_raw.columns:
            correction_latest = pd.to_datetime(correction_raw["trade_date"], errors="coerce").max()
        if pd.isna(correction_latest) or correction_latest.date() < target_date:
            pending.add(index_name)
    return pending


def index_update_source_labels(index_name: str, *, tickflow_enabled: bool = True) -> tuple[str, str]:
    """Describe the normal update source and an independent verification source."""
    config = INDEX_CONFIG.get(index_name, {})
    uses_tickflow = bool(tickflow_enabled and config.get("tickflow_symbol"))
    primary = "TickFlow 日线" if uses_tickflow else INDEX_SOURCE_LABELS.get(
        str(config.get("source") or ""),
        str(config.get("source") or "未知来源"),
    )
    if uses_tickflow:
        verifier = INDEX_SOURCE_LABELS.get(str(config.get("source") or ""), "独立公开日线")
    elif config.get("yahoo_symbol"):
        verifier = "Yahoo Finance 日线"
    elif index_name == "VIX恐慌指数":
        verifier = "Yahoo Finance 日线"
    elif index_name == "恒生科技":
        verifier = "Yahoo Finance 日线"
    elif index_name == "恒生港股通高息低波":
        verifier = "恒生指数公司官方收盘"
    else:
        verifier = "暂无独立日线复核源"
    return primary, verifier


def build_pending_index_update_preview(
    *,
    now: datetime | None = None,
    index_names: set[str] | None = None,
    tickflow_enabled: bool = True,
) -> pd.DataFrame:
    """Build a cache-only update preview without requesting market data."""
    pending = find_pending_post_close_index_names(now=now, index_names=index_names)
    rows: list[dict[str, object]] = []
    for index_name, config in INDEX_CONFIG.items():
        if index_name not in pending:
            continue
        target_date = _daily_update_target(index_name, now=now)
        finalized_raw, _ = load_dataset(
            raw_cache_symbol(index_name, config),
            INDEX_FINAL_HISTORY_SOURCE,
            "index_daily_raw",
        )
        latest_date = pd.NaT
        if finalized_raw is not None and not finalized_raw.empty and "trade_date" in finalized_raw.columns:
            latest_date = pd.to_datetime(finalized_raw["trade_date"], errors="coerce").max()
        missing_dates = (
            missing_recent_market_trade_dates(
                finalized_raw,
                str(config.get("market_group") or ""),
                target_date,
            )
            if target_date is not None
            else []
        )
        correction_start = source_correction_start(config)
        correction_missing = False
        if correction_start is not None and target_date is not None and target_date >= correction_start.date():
            correction_raw, _ = load_dataset(
                raw_cache_symbol(index_name, config),
                INDEX_SOURCE_CORRECTION_SOURCE,
                "index_daily_raw",
            )
            correction_latest = pd.NaT
            if correction_raw is not None and not correction_raw.empty and "trade_date" in correction_raw.columns:
                correction_latest = pd.to_datetime(correction_raw["trade_date"], errors="coerce").max()
            correction_missing = pd.isna(correction_latest) or correction_latest.date() < target_date
        primary_source, verification_source = index_update_source_labels(
            index_name,
            tickflow_enabled=tickflow_enabled,
        )
        reasons = []
        if pd.isna(latest_date) or (target_date is not None and latest_date.date() < target_date):
            reasons.append("最新正式日未补齐")
        if missing_dates:
            reasons.append("最近20个交易日有缺口")
        if correction_missing:
            reasons.append("来源校正待补齐")
        rows.append(
            {
                "指数": index_name,
                "市场": str(config.get("market_group") or ""),
                "当前正式日": "无" if pd.isna(latest_date) else latest_date.date().isoformat(),
                "目标交易日": "" if target_date is None else target_date.isoformat(),
                "缺失交易日": "、".join(day.isoformat() for day in missing_dates) or "无中间缺口",
                "待更新原因": "；".join(reasons) or "正式缓存待检查",
                "主更新源": primary_source,
                "复核源": verification_source,
            }
        )
    return pd.DataFrame(rows)


def _normalize_timestamp(value, timezone_name: str) -> datetime:
    timestamp = pd.to_numeric(value, errors="coerce")
    if pd.isna(timestamp):
        return datetime.now(ZoneInfo(timezone_name))
    timestamp_value = float(timestamp)
    if timestamp_value > 10_000_000_000:
        timestamp_value /= 1000
    return datetime.fromtimestamp(timestamp_value, tz=timezone.utc).astimezone(ZoneInfo(timezone_name))


def _fetch_eastmoney_quote(index_name: str, secid: str) -> dict[str, object] | None:
    if index_name == "恒生港股通高息低波":
        sina_quote = fetch_sina_hk_realtime_quote("HSHYLV")
        if sina_quote is not None:
            return sina_quote
    import requests

    config = INDEX_CONFIG.get(index_name, {})
    market_name = str(config.get("market_group") or "A股")
    market = get_market_window(market_name)
    timezone_name = market.timezone if market is not None else "Asia/Shanghai"
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://quote.eastmoney.com/",
        "User-Agent": "Mozilla/5.0",
    }
    params = {
        "secid": secid,
        "fields": "f43,f47,f57,f58,f60,f86,f133,f170",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fltt": "2",
        "invt": "2",
    }
    for trust_env in (False, True):
        session = requests.Session()
        session.trust_env = trust_env
        for host in ("push2.eastmoney.com", "36.push2.eastmoney.com", "push2delay.eastmoney.com"):
            try:
                response = session.get(
                    f"https://{host}/api/qt/stock/get",
                    params=params,
                    headers=headers,
                    timeout=3,
                )
                response.raise_for_status()
                quote = response.json().get("data") or {}
            except Exception:
                continue
            price = pd.to_numeric(quote.get("f43"), errors="coerce")
            if pd.isna(price):
                continue
            previous_close = pd.to_numeric(quote.get("f60"), errors="coerce")
            change_pct = pd.to_numeric(quote.get("f170"), errors="coerce")
            quote_time = _normalize_timestamp(quote.get("f86"), timezone_name)
            volume = pd.to_numeric(quote.get("f47"), errors="coerce")
            position = pd.to_numeric(quote.get("f133"), errors="coerce")
            return {
                "price": float(price),
                "previous_close": None if pd.isna(previous_close) else float(previous_close),
                "change_pct": None if pd.isna(change_pct) else float(change_pct),
                "volume": None if pd.isna(volume) else float(volume),
                "position": None if pd.isna(position) else float(position),
                "quote_time": quote_time,
                "source": "东方财富",
            }
    if index_name == "恒生港股通高息低波":
        config = INDEX_CONFIG[index_name]
        try:
            return fetch_mx_realtime_quote(
                str(config["mx_query_name"]),
                str(config["mx_expected_code"]),
                "港股",
            )
        except Exception:
            return None
    return None


def _fetch_yahoo_quote(index_name: str, symbol: str) -> dict[str, object] | None:
    try:
        payload = fetch_yahoo_chart_payload(
            symbol,
            {"range": "1d", "interval": "1m"},
            timeout=8,
        )
        results = payload.get("chart", {}).get("result") or []
        if not results:
            return None
        meta = results[0].get("meta") or {}
        price = pd.to_numeric(meta.get("regularMarketPrice"), errors="coerce")
        if pd.isna(price):
            return None
        previous_close = pd.to_numeric(
            meta.get("chartPreviousClose", meta.get("previousClose")),
            errors="coerce",
        )
        timezone_name = str(meta.get("exchangeTimezoneName") or "America/New_York")
        quote_time = _normalize_timestamp(meta.get("regularMarketTime"), timezone_name)
        change_pct = None
        if not pd.isna(previous_close) and float(previous_close) != 0:
            change_pct = (float(price) / float(previous_close) - 1) * 100
        return {
            "price": float(price),
            "previous_close": None if pd.isna(previous_close) else float(previous_close),
            "change_pct": change_pct,
            "quote_time": quote_time,
            "source": "Yahoo",
        }
    except Exception:
        return None


def _fetch_futures_quote(index_name: str, symbol: str) -> dict[str, object] | None:
    eastmoney_secid = EASTMONEY_FUTURES_QUOTE_SECIDS.get(index_name)
    if eastmoney_secid:
        return _fetch_eastmoney_quote(index_name, eastmoney_secid)
    try:
        import akshare as ak

        product = FUTURES_MAIN_CONTRACT_PRODUCTS.get(index_name)
        if symbol.upper() in {"IC0", "IM0"} and product is not None:
            realtime_df = ak.futures_zh_realtime(symbol=product[1])
            if realtime_df is None or realtime_df.empty or "symbol" not in realtime_df.columns:
                return None
            main_rows = realtime_df[
                realtime_df["symbol"].astype(str).str.strip().str.upper() == symbol.upper()
            ]
            if main_rows.empty:
                return None
            latest = main_rows.iloc[0]
            price = pd.to_numeric(latest.get("trade"), errors="coerce")
            if pd.isna(price):
                return None
            previous_close = pd.to_numeric(latest.get("preclose"), errors="coerce")
            change_pct = None
            if not pd.isna(previous_close) and float(previous_close) != 0:
                change_pct = (float(price) / float(previous_close) - 1) * 100
            quote_time = pd.to_datetime(
                f"{latest.get('tradedate', '')} {latest.get('ticktime', '')}",
                errors="coerce",
            )
            if pd.isna(quote_time):
                quote_time = datetime.now(ZoneInfo("Asia/Shanghai"))
            else:
                quote_time = quote_time.to_pydatetime().replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            volume = pd.to_numeric(latest.get("volume"), errors="coerce")
            position = pd.to_numeric(latest.get("position"), errors="coerce")
            return {
                "price": float(price),
                "previous_close": None if pd.isna(previous_close) else float(previous_close),
                "change_pct": change_pct,
                "volume": None if pd.isna(volume) else float(volume),
                "position": None if pd.isna(position) else float(position),
                "quote_time": quote_time,
                "source": "AkShare",
            }

        spot_df = ak.futures_zh_spot(symbol=symbol, market="CF", adjust="0")
        if spot_df is None or spot_df.empty:
            return None
        latest = spot_df.iloc[0]
        price = None
        for column in ("current_price", "最新价", "price", "last_close"):
            if column in spot_df.columns:
                candidate = pd.to_numeric(latest.get(column), errors="coerce")
                if not pd.isna(candidate):
                    price = float(candidate)
                    break
        if price is None:
            return None
        return {
            "price": price,
            # Sina's futures spot endpoint can echo current_price in last_close.
            # Let the summary use the previous completed daily close instead.
            "previous_close": None,
            "change_pct": None,
            "volume": None
            if "volume" not in spot_df.columns or pd.isna(pd.to_numeric(latest.get("volume"), errors="coerce"))
            else float(pd.to_numeric(latest.get("volume"), errors="coerce")),
            "position": None
            if "hold" not in spot_df.columns or pd.isna(pd.to_numeric(latest.get("hold"), errors="coerce"))
            else float(pd.to_numeric(latest.get("hold"), errors="coerce")),
            "quote_time": datetime.now(ZoneInfo("Asia/Shanghai")),
            "source": "AkShare",
        }
    except Exception:
        return None


def fetch_realtime_index_quotes(
    *,
    now: datetime | None = None,
    max_workers: int = 8,
    force_index_names: set[str] | None = None,
) -> dict[str, dict[str, object]]:
    forced = force_index_names or set()
    force_only = force_index_names is not None
    tasks: list[tuple[str, object, tuple[str, str]]] = []
    for index_name, secid in EASTMONEY_QUOTE_SECIDS.items():
        market_name = str(INDEX_CONFIG.get(index_name, {}).get("market_group") or "")
        if index_name in forced or (not force_only and _market_is_open(market_name, now=now)):
            tasks.append((index_name, _fetch_eastmoney_quote, (index_name, secid)))
    for index_name, symbol in YAHOO_QUOTE_SYMBOLS.items():
        market_name = str(INDEX_CONFIG.get(index_name, {}).get("market_group") or "")
        if index_name in forced or (not force_only and _market_is_open(market_name, now=now)):
            tasks.append((index_name, _fetch_yahoo_quote, (index_name, symbol)))
    for index_name, symbol in FUTURES_QUOTE_SYMBOLS.items():
        if index_name in forced or (not force_only and _futures_market_is_open(symbol, now=now)):
            tasks.append((index_name, _fetch_futures_quote, (index_name, symbol)))

    if not tasks:
        return {}

    quotes: dict[str, dict[str, object]] = {}
    workers = max(1, min(int(max_workers), len(tasks)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetcher, *args): index_name
            for index_name, fetcher, args in tasks
        }
        for future in as_completed(futures):
            index_name = futures[future]
            try:
                quote = future.result()
            except Exception:
                quote = None
            if quote is not None:
                quotes[index_name] = quote
    return quotes


def find_final_close_quote_names(
    summary_df: pd.DataFrame,
    attempted_keys: set[str],
    *,
    now: datetime | None = None,
) -> tuple[set[str], set[str]]:
    candidates: set[str] = set()
    keys: set[str] = set()
    if summary_df is None or summary_df.empty or "指数" not in summary_df.columns:
        return candidates, keys

    supported = set(EASTMONEY_QUOTE_SECIDS) | set(YAHOO_QUOTE_SYMBOLS)
    for index_name in summary_df["指数"].dropna().astype(str):
        if index_name not in supported:
            continue
        market_name = str(INDEX_CONFIG.get(index_name, {}).get("market_group") or "")
        market = get_market_window(market_name)
        if market is None:
            continue
        market_now = now.astimezone(ZoneInfo(market.timezone)) if now else datetime.now(ZoneInfo(market.timezone))
        if not is_market_trading_day(market, market_now):
            continue
        if market_now.time() <= market.sessions[-1][1]:
            continue
        attempt_key = f"{index_name}:{market_now.date().isoformat()}"
        if attempt_key in attempted_keys:
            continue
        candidates.add(index_name)
        keys.add(attempt_key)
    return candidates, keys


def apply_realtime_quotes_to_summary(
    summary_df: pd.DataFrame,
    quotes: dict[str, dict[str, object]],
) -> pd.DataFrame:
    result = summary_df.copy()
    result["实时来源"] = "缓存"
    result["实时时间"] = pd.NaT
    if result.empty or not quotes:
        return result

    for index_name, quote in quotes.items():
        mask = result["指数"].astype(str) == index_name
        if not mask.any():
            continue
        price = pd.to_numeric(quote.get("price"), errors="coerce")
        if pd.isna(price):
            continue
        previous_close = pd.to_numeric(quote.get("previous_close"), errors="coerce")
        if pd.isna(previous_close):
            previous_close = pd.to_numeric(result.loc[mask, "前收盘价"], errors="coerce").iloc[0]
        change_pct = pd.to_numeric(quote.get("change_pct"), errors="coerce")
        if pd.isna(change_pct) and not pd.isna(previous_close) and float(previous_close) != 0:
            change_pct = (float(price) / float(previous_close) - 1) * 100
        quote_time = quote.get("quote_time")
        cached_date = pd.to_datetime(result.loc[mask, "日期"], errors="coerce").max()
        if isinstance(quote_time, datetime) and not pd.isna(cached_date):
            if quote_time.date() < pd.Timestamp(cached_date).date():
                continue
        result.loc[mask, "收盘价"] = float(price)
        if not pd.isna(previous_close):
            result.loc[mask, "前收盘价"] = float(previous_close)
        if not pd.isna(change_pct):
            result.loc[mask, "当日涨跌幅(%)"] = float(change_pct)
        if isinstance(quote_time, datetime):
            result.loc[mask, "日期"] = quote_time.strftime("%Y-%m-%d")
            result.loc[mask, "实时时间"] = pd.Timestamp(quote_time.replace(tzinfo=None))
        result.loc[mask, "实时来源"] = str(quote.get("source") or "实时")
    return result
