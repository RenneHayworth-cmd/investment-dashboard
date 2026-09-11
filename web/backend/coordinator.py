"""One server-owned writer, immutable response snapshots, no browser-owned quotes.

Run exactly one API worker. TickFlow cadence and lunch de-duplication come from
position_runtime. Network stages never run on an HTTP request thread.
"""
from datetime import datetime
import hashlib
import json
import pandas as pd
from threading import Event, Lock, Thread
from time import monotonic
from zoneinfo import ZoneInfo

from core.db import init_db
from core.cache import list_datasets
from services import position_models as models
from services import position_market as market
from services import position_derivatives as derivatives
from services import position_runtime as runtime
from services import position_timing as timing
from services.position_close_audit import fetch_audited_close
from services.position_sessions import latest_final_etf_trade_date, etf_final_close_ready, etf_intraday_quote_ready
from services.index_frames import missing_recent_market_trade_dates
from services.index_realtime import fetch_realtime_index_quotes
from services.update_tasks import run_index_ma20_update
from web.backend.adapter import build_dashboard, formal_current, instrument, json_value
from web.backend.security import redact


class Coordinator:
    def __init__(self, api_key="", clock=None):
        self.api_key = api_key
        self.clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
        self.lock = Lock()
        self.wake = Event()
        self.stopped = Event()
        self.thread = None
        self.payload = None
        self.signature = None
        self.items = []
        self.derivatives = []
        self.derivative_preview = {}
        self.derivative_preview_date = ""
        self.index_quotes = {}
        self.index_quote_date = ""
        self.attempts = {}
        self.source_errors = {}
        self.formal_errors = {}
        self.last_manual = float("-inf")
        self.manual_refresh_requested = False
        self.refreshing = False
        self.error = ""
        self.stage = "读取本地缓存"
        self.last_refresh = ""

    def start(self):
        init_db()
        self.thread = Thread(target=self._loop, name="position-refresh", daemon=True)
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)

    def request_refresh(self):
        with self.lock:
            if self.refreshing or monotonic() - self.last_manual < 30:
                return False
            self.last_manual = monotonic()
            self.manual_refresh_requested = True
            self.wake.set()
            return True

    def read(self):
        with self.lock:
            if self.payload is None:
                return None
            return {**self.payload, "refreshing": self.refreshing, "refresh_error": self.error,
                    "refresh_stage": self.stage, "last_refresh_at": self.last_refresh}

    def detail(self, code):
        # Only read last published detail snapshots, never live writer objects.
        with self.lock:
            return getattr(self, "details", {}).get(code)

    def _publish(self, now):
        displayed = [self.derivative_preview.get(item.code, item) if self.derivative_preview_date == str(now.date()) else item for item in self.derivatives]
        quotes = runtime.load_runtime_etf_quotes()
        signature = hashlib.sha256()
        for item in self.items + displayed:
            signature.update(json.dumps(instrument(item), sort_keys=True).encode())
            signature.update(pd.util.hash_pandas_object(item.dataframe, index=True).values.tobytes())
        phase = [str(now.date()), str(latest_final_etf_trade_date(now)),
                 runtime._runtime_quote_refresh_band(now), etf_final_close_ready(now),
                 etf_intraday_quote_ready(now)]
        signature.update(json.dumps(json_value([quotes, self.index_quotes, phase]), sort_keys=True).encode())
        signature.update(list_datasets().to_json().encode())
        digest = signature.digest()
        if self.signature == digest:
            return
        payload = build_dashboard(self.items, displayed, quotes,
                                  self.index_quotes if self.index_quote_date == str(now.date()) else {}, now)
        details = {instrument(item)["code"]: {**instrument(item), "history": json_value(item.dataframe)} for item in self.items + displayed}
        with self.lock:
            self.payload, self.details = payload, details
            self.signature = digest

    def _due(self, key, target, seconds=600):
        previous_target, stamp = self.attempts.get(key, (None, float("-inf")))
        if target != previous_target or monotonic() - stamp >= seconds:
            self.attempts[key] = (target, monotonic())
            return True
        return False

    def _load_local(self, now):
        self.items = [market.load_or_fetch_etf(code, allow_fetch=False, market_now=now) for code in models.DEFAULT_ETF_CODES]
        target = latest_final_etf_trade_date(now)
        for item in self.items:
            previous = self.formal_errors.get(models.normalize_etf_base_code(item.code))
            if previous and previous[0] == target and not formal_current(item, now):
                item.error = previous[1]
                item.source = previous[2]
        self.derivatives = [derivatives.load_or_fetch_futures_contract(code, allow_fetch=False, market_now=now) for code in models.DEFAULT_FUTURES_CONTRACTS]
        self.derivatives += [derivatives.load_or_fetch_spread(group, allow_fetch=False, market_now=now) for group in models.DEFAULT_SPREAD_GROUPS]
        self.derivatives += [derivatives.load_or_fetch_option(code, allow_fetch=False, market_now=now) for code in models.DEFAULT_OPTION_CODES]

    def _cycle(self):
        now = self.clock()
        with self.lock:
            manual_refresh = self.manual_refresh_requested
            self.manual_refresh_requested = False
        self.stage = "读取本地缓存"
        self._load_local(now)
        self._publish(now)
        if not self.api_key:
            self.error = "服务器未配置 TICKFLOW_API_KEY；仅显示已有缓存。"
            return
        errors = []
        band = runtime._runtime_quote_refresh_band(now)
        if band:
            self.stage = "刷新共享行情"
            before = runtime.load_runtime_etf_quote_state().get("last_attempt")
            try:
                runtime.refresh_runtime_etf_quotes(
                    models.DEFAULT_ETF_CODES,
                    api_key=self.api_key,
                    market_now=now,
                    force=manual_refresh,
                )
            except Exception as exc:
                self.source_errors["quotes"] = f"TickFlow实时行情：{redact(str(exc))}"
            else:
                self.source_errors.pop("quotes", None)
            self._publish(self.clock())
            after = runtime.load_runtime_etf_quote_state().get("last_attempt")
            if before != after:
                refreshed, failures = derivatives.refresh_position_derivative_items(self.derivatives, api_key=self.api_key, market_now=now)
                if self.derivative_preview_date != str(now.date()):
                    self.derivative_preview = {}
                for item in refreshed:
                    item.status = "盘中预览"
                    item.source += "（实时预览，不写入缓存）"
                self.derivative_preview.update({item.code: item for item in refreshed})
                self.derivative_preview_date = str(now.date())
                self.source_errors["derivatives"] = " | ".join(failures)
                try:
                    fetched = fetch_realtime_index_quotes(now=now, max_workers=2, force_index_names=set(timing.POSITION_INDEX_TIMING_STRATEGIES))
                    if self.index_quote_date != str(now.date()):
                        self.index_quotes = {}
                    self.index_quotes.update(fetched)
                    self.index_quote_date = str(now.date())
                    missing = set(timing.POSITION_INDEX_TIMING_STRATEGIES) - fetched.keys()
                    self.source_errors["index_quotes"] = "指数实时报价缺失：" + "、".join(sorted(missing)) if missing else ""
                except Exception as exc:
                    self.source_errors["index_quotes"] = "指数实时源：" + redact(str(exc))
                self._publish(self.clock())
        target = latest_final_etf_trade_date(now)
        for index, item in enumerate(self.items):
            if self.stopped.is_set():
                return
            code = models.normalize_etf_base_code(item.code)
            if not formal_current(item, now) and self._due("etf:" + code, target):
                self.stage = f"补齐正式日线 {code}"
                try:
                    self.items[index] = fetch_audited_close(code, fetcher=market.load_or_fetch_etf,
                        target_date=target, api_key=self.api_key, allow_fetch=True, save_to_cache=True, market_now=now)
                    latest = self.items[index]
                    if latest.error:
                        self.formal_errors[code] = (target, redact(latest.error), latest.source)
                    else:
                        self.formal_errors.pop(code, None)
                except Exception as exc:
                    errors.append(f"{code} 正式数据源：{redact(str(exc))}")
                self._publish(self.clock())
        if self._due("derivatives", target):
            self.stage = "检查期货与价差正式缓存"
            self.derivatives = [derivatives.load_or_fetch_futures_contract(code, api_key=self.api_key, market_now=now) for code in models.DEFAULT_FUTURES_CONTRACTS]
            self.derivatives += [derivatives.load_or_fetch_spread(group, api_key=self.api_key, market_now=now) for group in models.DEFAULT_SPREAD_GROUPS]
            self.derivatives += [derivatives.load_or_fetch_option(code, market_now=now) for code in models.DEFAULT_OPTION_CODES]
            self._publish(self.clock())
        # Only the two position references. Existing updater checks recent gaps
        # and appends completed dates; it does not update browser quote state.
        if self._due("indexes", target):
            self.stage = "检查两个指数参考的正式缓存"
            try:
                pending = {name for name in timing.POSITION_INDEX_TIMING_STRATEGIES
                           if missing_recent_market_trade_dates(
                               timing._load_position_index_timing_history(name), "A股", target)}
                if pending:
                    result = run_index_ma20_update(api_key=self.api_key,
                        index_names=pending, max_workers=2)
                    self.source_errors["index_formal"] = " | ".join(result.errors)
                    if result.status != "success":
                        self.source_errors["index_formal"] = "指数正式更新：" + result.message
                else:
                    self.source_errors.pop("index_formal", None)
            except Exception as exc:
                self.source_errors["index_formal"] = "指数正式源：" + redact(str(exc))
        if self.derivative_preview_date == str(now.date()) and not band and now.time() >= models.ETF_FINAL_CLOSE_READY_TIME:
            # A confirmed same-day derivative close supersedes its preview.
            for item in self.derivatives:
                if item.latest_date == str(now.date()) and not item.error:
                    self.derivative_preview.pop(item.code, None)
        self.error = redact(" | ".join(filter(None, errors + list(self.source_errors.values()))))
        self.last_refresh = self.clock().strftime("%Y-%m-%d %H:%M:%S")
        self._publish(self.clock())

    def _loop(self):
        while not self.stopped.is_set():
            self.wake.clear()
            self.refreshing = True
            try:
                self._cycle()
            except Exception as exc:
                self.error = "后台更新失败，保留已有数据：" + redact(str(exc))
            finally:
                self.refreshing = False
                self.stage = "等待下次检查"
            self.wake.wait(30)
