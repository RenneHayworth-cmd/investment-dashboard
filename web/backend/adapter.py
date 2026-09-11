"""JSON and view composition only. Never compute a trading signal here."""
from dataclasses import fields, is_dataclass
from datetime import date, datetime
import math

import numpy as np
import pandas as pd

from services import position_models as models
from services import position_performance as performance
from services import position_runtime as runtime
from services import position_sessions as sessions
from services import position_timing as timing
from web.backend.schemas import Dashboard
from web.backend.security import redact


def json_value(value):
    if isinstance(value, pd.DataFrame):
        return json_value(value.to_dict(orient="records"))
    if is_dataclass(value):
        return {field.name: json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_value(item) for item in value]
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, np.generic):
        return json_value(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        return redact(value)
    return value


def instrument(item):
    result = {field.name: json_value(getattr(item, field.name)) for field in fields(item) if field.name != "dataframe"}
    result["error"] = result["error"].replace("本地暂无缓存；点击「加载持仓信息」可联网补齐。", "暂无正式缓存，服务器将自动补齐；失败后间隔10分钟重试。")
    if item.category == "ETF":
        result["code"] = models.normalize_etf_base_code(item.code)
    return result


def formal_current(item, now):
    stamp = pd.to_datetime(item.latest_date, errors="coerce")
    return bool(item.formal_history_valid and pd.notna(stamp) and stamp.date() >= sessions.latest_final_etf_trade_date(now))


def build_dashboard(items, derivatives, quotes, index_quotes, now):
    """Keep formal objects untouched; preview objects retain their formal dataframe."""
    target = sessions.latest_final_etf_trade_date(now)
    current = runtime.filter_current_etf_realtime_quotes(quotes, market_now=now, retain_after_close=True)
    valuation_quotes = current
    if sessions.etf_final_close_ready(now):
        current = {code: quote for code, quote in current.items()
                   if any(models.normalize_etf_base_code(item.code) == code and not formal_current(item, now) for item in items)}
    preview_items = [runtime.apply_etf_realtime_quote_to_timing(
        item, current.get(models.normalize_etf_base_code(item.code), {}), market_now=now,
        allow_close_retention=now.time() >= models.ETF_REALTIME_TIMING_END_TIME,
    ) for item in items]
    result = performance.build_position_timing_performance(items, market_now=now)
    live = performance.build_position_timing_intraday_valuation(result, valuation_quotes, market_now=now)
    preview = performance.build_position_timing_trade_preview(items, current, market_now=now)
    band = runtime._runtime_quote_refresh_band(now)
    quote_times = [pd.Timestamp(quote["quote_time"]) for quote in current.values() if quote.get("quote_time")]
    preview_codes = [models.normalize_etf_base_code(item.code) for item in preview_items if item.status in {"盘中", "早盘预判", "午间预判", "实时预判", "收盘待确认"}]
    index_formal = json_value(timing.build_position_index_timing_table(market_now=now))
    missing_index_codes = [row["代码"] for row in index_formal if not row.get("数据截止日") or str(row["数据截止日"])[:10] < str(target)]
    data = dict(
        generated_at=now.strftime("%Y-%m-%d %H:%M:%S"), expected_formal_date=str(target),
        session=band[0] if band else "正式收盘阶段" if sessions.etf_final_close_ready(now) else "非报价刷新时段",
        formal_dates={models.normalize_etf_base_code(item.code): item.latest_date for item in items},
        formal_updated_at=max((item.cache_time for item in items), default=""),
        quote_time=max(quote_times).strftime("%Y-%m-%d %H:%M:%S") if quote_times else "",
        preview_codes=preview_codes,
        missing_formal_codes=[models.normalize_etf_base_code(item.code) for item in items if not formal_current(item, now)],
        missing_quote_codes=[models.normalize_etf_base_code(item.code) for item in items if models.normalize_etf_base_code(item.code) not in current] if band else [],
        missing_index_codes=missing_index_codes,
        items=[instrument(item) for item in preview_items],
        etf_formal=json_value(timing.build_etf_timing_table(items)),
        etf_preview=json_value(timing.build_etf_timing_table(preview_items)) if current else [],
        index_formal=index_formal,
        index_preview=json_value(timing.build_position_index_timing_table(realtime_quotes=index_quotes, market_now=now)) if index_quotes else [],
        guidance=json_value(timing.build_recent_position_operation_guidance(items, days=7)),
        strategy_parameters={"初始资金": performance.POSITION_TIMING_INITIAL_CAPITAL,
                             "开始日期": json_value(performance.POSITION_TIMING_START_DATE),
                             "单边费率": performance.POSITION_TIMING_TRANSACTION_COST,
                             "整手份数": performance.POSITION_TIMING_LOT_SIZE},
        strategy=json_value(result), strategy_live=json_value(live), trade_preview=json_value(preview),
        derivatives=[instrument(item) for item in derivatives if item.category != "期货价差"],
        spreads=[instrument(item) for item in derivatives if item.category == "期货价差"],
    )
    return Dashboard.model_validate(data).model_dump()
