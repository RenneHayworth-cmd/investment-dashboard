from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, time
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from core.cache import load_dataset, save_dataset
from core.paths import RAW_DIR


INDEX_NAME = "桃囍微盘"
INDEX_CODE = "TXWP20"
VERSION = "taoxi_microcap_v1"
BASE_DATE = pd.Timestamp("2026-01-05")
BASE_VALUE = 1000.0
OBSERVED_RETURN_START = pd.Timestamp("2026-06-23")
AUTHORIZED_ESTIMATE_BRIDGE_DATES = {pd.Timestamp("2026-08-19")}
SNAPSHOT_PATH = RAW_DIR / "eastmoney" / "microcap_bk1158_constituent_snapshots_1d.csv"
DEFAULT_V3_DIR = Path(__file__).resolve().parents[1] / "output" / "microcap_union_estimate_20260101_20260929_v3"
ROUNDING_TOLERANCE_PCT = 0.011

HISTORY_SOURCE = f"{VERSION}_history"
CONSTITUENT_SOURCE = f"{VERSION}_constituents"
INPUT_SOURCE = f"{VERSION}_inputs"
QUALITY_SOURCE = f"{VERSION}_quality"
BAR_SOURCE = f"{VERSION}_formal_bars"

_RUNTIME_LOCK = Lock()
_RUNTIME_QUOTE: dict[str, object] | None = None


class TaoxiDataError(RuntimeError):
    pass


def _codes(values: pd.Series) -> pd.Series:
    return values.astype(str).str.replace(r"\.0$", "", regex=True).str.split(".").str[-1].str.zfill(6)


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "是", "停牌", "yes"}


def classify_snapshot_halt_status(snapshot: pd.DataFrame) -> pd.DataFrame:
    """Return 正常/停牌/未知 without treating a missing value as zero."""
    frame = snapshot.copy()
    if frame.empty:
        frame["停牌状态"] = pd.Series(dtype=str)
        frame["停牌依据"] = pd.Series(dtype=str)
        return frame
    volume = pd.to_numeric(frame.get("成交量"), errors="coerce")
    amount = pd.to_numeric(frame.get("成交额"), errors="coerce")
    complete = volume.notna() & amount.notna()
    coverage = float(complete.mean())
    explicit = frame.get("是否停牌", pd.Series(False, index=frame.index)).map(_truthy)
    zero = volume.eq(0) | amount.eq(0)
    both_blank = volume.isna() & amount.isna()
    normal = volume.gt(0) & amount.gt(0) & ~explicit

    frame["停牌状态"] = "未知"
    frame["停牌依据"] = "成交字段不足，须补查正式日线"
    frame.loc[normal, ["停牌状态", "停牌依据"]] = ["正常", "快照成交量和成交额均大于0"]
    frame.loc[zero, ["停牌状态", "停牌依据"]] = ["停牌", "快照成交量或成交额为0"]
    frame.loc[explicit, ["停牌状态", "停牌依据"]] = ["停牌", "快照明确停牌标记"]
    inferred = both_blank & (coverage >= 0.95) & ~explicit
    frame.loc[inferred, ["停牌状态", "停牌依据"]] = [
        "停牌", f"全体成交字段完整率{coverage:.1%}，该股成交量和成交额均为空"
    ]
    frame.attrs["成交字段完整率"] = coverage
    return frame


def _normalize_bars(bars: pd.DataFrame) -> pd.DataFrame:
    frame = bars.copy()
    required = {"date", "code", "open", "close", "preclose", "pctChg", "tradestatus"}
    missing = required - set(frame.columns)
    if missing:
        raise TaoxiDataError(f"v3日线缺少字段：{','.join(sorted(missing))}")
    frame["代码"] = _codes(frame["code"])
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    for column in ("open", "close", "preclose", "pctChg", "tradestatus", "volume", "amount", "isST"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame.duplicated(["date", "代码"]).any():
        raise TaoxiDataError("v3日线存在重复证券日期")
    return frame


def _daily_status(row: pd.Series | None) -> tuple[str, str]:
    if row is None:
        return "未知", "正式日线缺失"
    status = pd.to_numeric(row.get("tradestatus"), errors="coerce")
    volume = pd.to_numeric(row.get("volume"), errors="coerce")
    amount = pd.to_numeric(row.get("amount"), errors="coerce")
    if status == 0 or volume == 0 or amount == 0:
        return "停牌", "正式日线交易状态或成交字段为0"
    if status == 1 and (pd.isna(volume) or volume > 0) and (pd.isna(amount) or amount > 0):
        return "正常", "正式日线交易状态正常"
    return "未知", "正式日线交易状态无法确认"


def select_snapshot_constituents(snapshot: pd.DataFrame, daily_bars: pd.DataFrame) -> pd.DataFrame:
    if snapshot is None or len(snapshot) != 400:
        raise TaoxiDataError("真实快照必须恰好包含400只股票")
    frame = snapshot.copy()
    frame["代码"] = _codes(frame["代码"])
    frame["总市值(亿元)"] = pd.to_numeric(frame["总市值(亿元)"], errors="coerce")
    frame = classify_snapshot_halt_status(frame).sort_values(["总市值(亿元)", "代码"], kind="stable")
    bars = _normalize_bars(daily_bars) if not daily_bars.empty else daily_bars
    bar_map = {str(row["代码"]): row for _, row in bars.iterrows()}
    for idx in frame.index[frame["停牌状态"].eq("未知")]:
        status, evidence = _daily_status(bar_map.get(str(frame.at[idx, "代码"])))
        frame.at[idx, "停牌状态"] = status
        frame.at[idx, "停牌依据"] = evidence
    if frame["停牌状态"].eq("未知").any():
        candidates = frame.loc[frame["停牌状态"].ne("停牌")].head(20)
        if candidates["停牌状态"].eq("未知").any():
            codes = "、".join(candidates.loc[candidates["停牌状态"].eq("未知"), "代码"].tolist())
            raise TaoxiDataError(f"候选股停牌状态未知：{codes}")
    selected = frame[frame["停牌状态"].eq("正常")].head(20).copy()
    if len(selected) != 20:
        raise TaoxiDataError(f"正常候选不足20只：{len(selected)}")
    selected["rank"] = range(1, 21)
    selected["weight"] = 0.05
    return selected


def validate_v3_directory(v3_dir: Path) -> dict:
    request_path = v3_dir / "request.json"
    report_path = v3_dir / "report.json"
    if not request_path.exists() or not report_path.exists():
        raise TaoxiDataError("缺少v3请求或验收报告")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    fields = set(str(request.get("fields") or "").split(","))
    if request.get("schema_version") != 3 or not {"preclose", "pctChg", "open", "close"}.issubset(fields):
        raise TaoxiDataError("历史估算不是包含preclose和pctChg的v3口径")
    if report.get("publication_gate") != "通过" or report.get("failures"):
        raise TaoxiDataError("v3历史估算未通过发布门禁")
    return request


def load_v3_inputs(v3_dir: Path = DEFAULT_V3_DIR) -> tuple[pd.DataFrame, pd.DataFrame, list[pd.Timestamp]]:
    validate_v3_directory(v3_dir)
    top = pd.read_csv(v3_dir / "daily_top20.csv", dtype={"代码": str, "code": str})
    frames = [pd.read_csv(path, dtype=str) for path in sorted((v3_dir / "bars").glob("*.csv"))]
    if not frames:
        raise TaoxiDataError("v3目录没有日线文件")
    calendar = pd.read_csv(v3_dir / "calendar.csv", dtype=str)
    sessions = pd.to_datetime(
        calendar.loc[calendar["is_trading_day"].eq("1"), "calendar_date"], errors="coerce"
    ).dropna().sort_values().tolist()
    return top, _normalize_bars(pd.concat(frames, ignore_index=True)), sessions


def _required_formal_bar_codes_by_day(
    snapshots: pd.DataFrame,
    sessions: list[pd.Timestamp],
    base_latest: pd.Timestamp,
    latest_snapshot: pd.Timestamp,
) -> dict[pd.Timestamp, set[str]]:
    """Return the candidate codes whose daily rows are required for each new session."""
    candidate_codes: dict[pd.Timestamp, set[str]] = {}
    for snapshot_date, group in snapshots.groupby("快照日期"):
        ranked = group.copy()
        ranked["总市值(亿元)"] = pd.to_numeric(ranked["总市值(亿元)"], errors="coerce")
        candidate_codes[pd.Timestamp(snapshot_date).normalize()] = set(
            ranked.sort_values(["总市值(亿元)", "代码"], kind="stable").head(60)["代码"]
        )
    snapshot_dates = sorted(candidate_codes)
    required: dict[pd.Timestamp, set[str]] = {}
    for session in sorted(pd.Timestamp(day).normalize() for day in sessions):
        if session <= base_latest or session > latest_snapshot:
            continue
        previous_dates = [day for day in snapshot_dates if day < session]
        codes = set(candidate_codes.get(session, set()))
        if previous_dates:
            codes.update(candidate_codes[max(previous_dates)])
        if codes:
            required[session] = codes
    return required


def refresh_incremental_formal_bars(
    bars: pd.DataFrame,
    snapshots: pd.DataFrame,
    sessions: list[pd.Timestamp],
) -> pd.DataFrame:
    """Fetch only the newly required observed-era stock bars; never rewrites prior rows."""
    cached, _ = load_dataset(VERSION, BAR_SOURCE, "taoxi_stock_daily")
    base_bars = _normalize_bars(bars)
    combined = base_bars.copy()
    if cached is not None and not cached.empty:
        combined = pd.concat([combined, _normalize_bars(cached)], ignore_index=True)
        combined = combined.drop_duplicates(["date", "代码"], keep="first")
    snapshots = snapshots.copy()
    snapshots["快照日期"] = pd.to_datetime(snapshots["快照日期"], errors="coerce").dt.normalize()
    snapshots["代码"] = _codes(snapshots["代码"])
    latest_snapshot = snapshots["快照日期"].max()
    base_latest = base_bars["date"].max()
    if pd.isna(latest_snapshot) or pd.isna(base_latest) or latest_snapshot <= base_latest:
        return combined
    required_by_day = _required_formal_bar_codes_by_day(
        snapshots, sessions, pd.Timestamp(base_latest).normalize(), pd.Timestamp(latest_snapshot).normalize()
    )
    existing_pairs = set(zip(combined["date"], combined["代码"]))
    missing_pairs = {
        (day, code)
        for day, codes in required_by_day.items()
        for code in codes
        if (day, code) not in existing_pairs
    }
    if not missing_pairs:
        return combined
    missing_sessions = sorted({day for day, _ in missing_pairs})
    codes = sorted({code for _, code in missing_pairs})
    try:
        import baostock as bs
    except ImportError as exc:
        raise TaoxiDataError("缺少baostock，无法补取桃囍微盘正式个股日线") from exc
    login = bs.login()
    if login.error_code != "0":
        raise TaoxiDataError(f"BaoStock登录失败：{login.error_msg}")
    frames: list[pd.DataFrame] = []
    failures: list[str] = []
    fields = "date,code,open,close,preclose,pctChg,volume,amount,adjustflag,tradestatus,isST"
    try:
        for code in codes:
            symbol = ("sh." if code.startswith("6") else "sz.") + code
            result = bs.query_history_k_data_plus(symbol, fields,
                start_date=min(missing_sessions).strftime("%Y-%m-%d"),
                end_date=max(missing_sessions).strftime("%Y-%m-%d"), frequency="d", adjustflag="3")
            if result.error_code != "0":
                failures.append(f"{code}:{result.error_msg}")
                continue
            frame = result.get_data()
            if not frame.empty:
                frames.append(frame)
    finally:
        bs.logout()
    incremental = _normalize_bars(pd.concat(frames, ignore_index=True)) if frames else pd.DataFrame()
    fetched_pairs = set() if incremental.empty else set(zip(incremental["date"], incremental["代码"]))
    still_missing = missing_pairs - fetched_pairs
    fallback_frames: list[pd.DataFrame] = []
    for day in sorted({day for day, _ in still_missing}):
        day_codes = sorted(code for pair_day, code in still_missing if pair_day == day)
        day_snapshot = snapshots[snapshots["快照日期"].eq(day)]
        try:
            fallback_frames.append(_fetch_eastmoney_completed_stock_rows(day_codes, day, day_snapshot))
        except Exception as exc:
            failures.append(f"{day.date()}:{exc}")
    fallback = pd.concat(fallback_frames, ignore_index=True) if fallback_frames else pd.DataFrame()
    if not fallback.empty:
        incremental = fallback if incremental.empty else pd.concat([incremental, fallback], ignore_index=True)
    candidate = combined if incremental.empty else pd.concat([combined, incremental], ignore_index=True)
    candidate = candidate.drop_duplicates(["date", "代码"], keep="first")
    covered_pairs = set(zip(candidate["date"], candidate["代码"]))
    unresolved = sorted(missing_pairs - covered_pairs)
    if unresolved:
        missing_text = "、".join(f"{day.date()} {code}" for day, code in unresolved[:5])
        failure_text = "；".join(failures[:5])
        detail = f"；来源错误：{failure_text}" if failure_text else ""
        raise TaoxiDataError(f"正式日线补取不完整：{missing_text}{detail}")
    stored_new = incremental[[column for column in incremental.columns if column != "代码"]].copy()
    stored = stored_new if cached is None or cached.empty else pd.concat([cached, stored_new], ignore_index=True)
    stored["date"] = pd.to_datetime(stored["date"], errors="coerce")
    stored = stored.drop_duplicates(["date", "code"], keep="first").sort_values(["date", "code"])
    save_dataset(VERSION, "桃囍微盘增量正式个股日线", BAR_SOURCE, "taoxi_stock_daily", stored)
    combined = pd.concat([combined, incremental], ignore_index=True)
    return combined.drop_duplicates(["date", "代码"], keep="first").sort_values(["date", "代码"])


def _fetch_eastmoney_completed_stock_rows(
    codes: list[str],
    trade_date: pd.Timestamp,
    snapshot: pd.DataFrame,
) -> pd.DataFrame:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    target = pd.Timestamp(trade_date).date()
    if target > now.date() or (target == now.date() and now.time() < time(15, 5)):
        raise TaoxiDataError(f"{target}尚未达到正式收盘确认时间")
    params = {"fltt": 2, "invt": 2, "fields": "f2,f3,f5,f6,f12,f14,f17,f18,f124",
        "secids": ",".join(_secid(code) for code in codes)}
    headers = {"Accept": "application/json,text/plain,*/*", "Referer": "https://quote.eastmoney.com/",
        "User-Agent": "Mozilla/5.0"}
    payload = None
    last_error: Exception | None = None
    for trust_env in (False, True):
        session = requests.Session()
        session.trust_env = trust_env
        try:
            for url in (
                "https://push2.eastmoney.com/api/qt/ulist.np/get",
                "https://36.push2.eastmoney.com/api/qt/ulist.np/get",
                "https://pushguest.eastmoney.com/api/qt/ulist.np/get",
            ):
                try:
                    response = session.get(url, params=params, headers=headers, timeout=12)
                    response.raise_for_status()
                    rows = ((response.json().get("data") or {}).get("diff") or [])
                    if rows:
                        payload = rows
                        break
                except Exception as exc:
                    last_error = exc
            if payload:
                break
        finally:
            session.close()
    if not payload:
        raise TaoxiDataError(f"东方财富收盘个股行情失败：{last_error}")
    snapshot_map = snapshot.set_index("代码") if not snapshot.empty else pd.DataFrame()
    result = []
    for row in payload:
        code = str(row.get("f12") or "").zfill(6)
        stamp = pd.to_numeric(row.get("f124"), errors="coerce")
        quote_time = None if pd.isna(stamp) else datetime.fromtimestamp(float(stamp), ZoneInfo("Asia/Shanghai"))
        if code not in codes or quote_time is None or quote_time.date() != target or quote_time.time() < time(15, 0):
            continue
        opened, closed, preclose, pct = (
            pd.to_numeric(row.get(key), errors="coerce") for key in ("f17", "f2", "f18", "f3")
        )
        volume, amount = (pd.to_numeric(row.get(key), errors="coerce") for key in ("f5", "f6"))
        if any(pd.isna(value) or float(value) <= 0 for value in (opened, closed, preclose)) or pd.isna(pct):
            continue
        if not snapshot_map.empty and code in snapshot_map.index:
            snapshot_pct = pd.to_numeric(snapshot_map.loc[code].get("涨跌幅(%)"), errors="coerce")
            if not pd.isna(snapshot_pct) and abs(float(snapshot_pct) - float(pct)) > ROUNDING_TOLERANCE_PCT:
                raise TaoxiDataError(f"{target} {code}东方财富两份收盘涨跌幅不一致")
        result.append({"date": pd.Timestamp(target), "code": ("sh." if code.startswith("6") else "sz.") + code,
            "代码": code, "open": opened, "close": closed, "preclose": preclose, "pctChg": pct,
            "volume": volume, "amount": amount, "adjustflag": 3,
            "tradestatus": 0 if volume == 0 or amount == 0 else 1,
            "isST": int("ST" in str(row.get("f14") or "").upper())})
    if not result:
        raise TaoxiDataError(f"东方财富未返回{target}收盘确认行情")
    return _normalize_bars(pd.DataFrame(result))


def _hash_rows(rows: list[dict[str, object]]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _snapshot_is_close_confirmed(group: pd.DataFrame, sessions: list[pd.Timestamp] | None = None) -> bool:
    if "快照时间" not in group.columns:
        return False
    stamps = pd.to_datetime(group["快照时间"], errors="coerce").dropna()
    dates = pd.to_datetime(group.get("快照日期"), errors="coerce").dropna()
    if stamps.empty or dates.empty:
        return False
    captured = stamps.min()
    trade_date = dates.iloc[0].normalize()
    if captured.normalize() == trade_date:
        return captured.time() >= time(15, 0)
    if captured.normalize() < trade_date:
        return False
    intervening = [day for day in (sessions or []) if trade_date < pd.Timestamp(day).normalize() <= captured.normalize()]
    return not intervening


def build_taoxi_series(
    estimate_top20: pd.DataFrame,
    snapshots: pd.DataFrame,
    bars: pd.DataFrame,
    sessions: list[pd.Timestamp],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    bars = _normalize_bars(bars)
    sessions = sorted(pd.Timestamp(day).normalize() for day in sessions)
    next_session = dict(zip(sessions, sessions[1:]))
    selections: list[dict[str, object]] = []

    estimates = estimate_top20.copy()
    estimates["date"] = pd.to_datetime(estimates["date"], errors="coerce").dt.normalize()
    estimate_code_column = estimates["代码"] if "代码" in estimates.columns else estimates["code"]
    estimates["代码"] = _codes(estimate_code_column)
    historical_estimates = estimates[estimates["date"].le("2026-06-19")]
    for selection_date, group in historical_estimates.groupby("date", sort=True):
        if len(group) != 20 or selection_date not in next_session:
            raise TaoxiDataError(f"历史估算选样不完整：{selection_date.date()}")
        for rank, (_, row) in enumerate(group.sort_values("strategy_rank").iterrows(), 1):
            selections.append({"selection_date": selection_date, "effective_date": next_session[selection_date],
                "代码": row["代码"], "名称": row.get("名称", ""), "rank": rank, "weight": .05,
                "selection_source": "历史估算", "停牌状态": "正常", "停牌依据": "v3日线选样门禁"})

    for selection_date in sorted(AUTHORIZED_ESTIMATE_BRIDGE_DATES):
        if selection_date not in next_session:
            continue
        group = estimates[estimates["date"].eq(selection_date)].sort_values("strategy_rank")
        if len(group) != 20:
            raise TaoxiDataError(f"授权估算补缺名单不完整：{selection_date.date()}")
        for rank, (_, row) in enumerate(group.iterrows(), 1):
            selections.append({"selection_date": selection_date, "effective_date": next_session[selection_date],
                "代码": row["代码"], "名称": row.get("名称", ""), "rank": rank, "weight": .05,
                "selection_source": "历史估算补缺", "停牌状态": "正常",
                "停牌依据": "用户授权使用v3历史估算补充无效收盘快照"})

    snapshots = snapshots.copy()
    snapshots["快照日期"] = pd.to_datetime(snapshots["快照日期"], errors="coerce").dt.normalize()
    snapshots["代码"] = _codes(snapshots["代码"])
    for selection_date, group in snapshots[snapshots["快照日期"].ge("2026-06-22")].groupby("快照日期", sort=True):
        if selection_date not in next_session:
            continue
        if not _snapshot_is_close_confirmed(group, sessions):
            continue
        daily = bars[bars["date"].eq(selection_date)]
        selected = select_snapshot_constituents(group, daily)
        for _, row in selected.iterrows():
            selections.append({"selection_date": selection_date, "effective_date": next_session[selection_date],
                "代码": row["代码"], "名称": row.get("名称", ""), "rank": int(row["rank"]), "weight": .05,
                "selection_source": "真实快照", "停牌状态": row["停牌状态"], "停牌依据": row["停牌依据"],
                "snapshot_captured_at": row.get("快照时间", ""),
                "snapshot_backfill": pd.Timestamp(row.get("快照时间")).normalize() > selection_date})
    constituents = pd.DataFrame(selections).sort_values(["effective_date", "rank"]).reset_index(drop=True)
    if constituents.empty:
        raise TaoxiDataError("没有可用选样记录")
    selection_hashes = {
        day: _hash_rows(group[["代码", "rank", "weight", "selection_source"]].to_dict("records"))
        for day, group in constituents.groupby("effective_date")
    }
    constituents["input_hash"] = constituents["effective_date"].map(selection_hashes)

    snapshot_by_day = {
        day: group.set_index("代码")
        for day, group in snapshots.groupby("快照日期")
        if _snapshot_is_close_confirmed(group, sessions)
    }
    bar_by_key = {(row["date"], row["代码"]): row for _, row in bars.iterrows()}
    history = [{"trade_date": BASE_DATE, "open": BASE_VALUE, "close": BASE_VALUE,
        "source_stage": "历史估算", "quality_status": "通过", "input_hash": _hash_rows([{"base": BASE_VALUE}])}]
    input_rows: list[dict[str, object]] = []
    quality_rows: list[dict[str, object]] = []
    previous_close = BASE_VALUE
    last_available = bars["date"].max()
    for trade_date in [day for day in sessions if BASE_DATE < day <= last_available]:
        effective = constituents[constituents["effective_date"].eq(trade_date)]
        if len(effective) != 20:
            quality_rows.append({"trade_date": trade_date, "effective_count": len(effective),
                "weight_sum": float(pd.to_numeric(effective.get("weight"), errors="coerce").sum()) if not effective.empty else 0.0,
                "open_input_count": 0, "close_input_count": 0, "unknown_halt_count": 0,
                "quality_status": "停止发布：缺少T-1收盘确认名单", "input_hash": ""})
            break
        day_snapshot = snapshot_by_day.get(trade_date)
        day_inputs: list[dict[str, object]] = []
        for _, member in effective.sort_values("rank").iterrows():
            code = member["代码"]
            bar = bar_by_key.get((trade_date, code))
            snap = None if day_snapshot is None or code not in day_snapshot.index else day_snapshot.loc[code]
            snap_status = None
            if snap is not None:
                classified = classify_snapshot_halt_status(snapshot_by_day[trade_date].reset_index())
                matched = classified[classified["代码"].eq(code)]
                snap_status = None if matched.empty else matched.iloc[0]
            halt_status, halt_evidence = _daily_status(bar)
            if snap_status is not None and snap_status["停牌状态"] in {"正常", "停牌"}:
                halt_status, halt_evidence = snap_status["停牌状态"], snap_status["停牌依据"]
            if halt_status == "未知":
                raise TaoxiDataError(f"{trade_date.date()} {code}停牌状态未知")
            if halt_status == "停牌":
                open_ratio = close_ratio = 1.0
                open_price = close_price = preclose = None
                pct = 0.0
            else:
                if bar is None:
                    raise TaoxiDataError(f"{trade_date.date()} {code}缺少正式日线")
                open_price, close_price, preclose = (float(bar[key]) for key in ("open", "close", "preclose"))
                if min(open_price, close_price, preclose) <= 0:
                    raise TaoxiDataError(f"{trade_date.date()} {code}价格字段无效")
                open_ratio = open_price / preclose
                calculated_pct = (close_price / preclose - 1) * 100
                bar_pct = float(bar["pctChg"])
                if abs(bar_pct - calculated_pct) > ROUNDING_TOLERANCE_PCT:
                    raise TaoxiDataError(f"{trade_date.date()} {code}日线涨跌幅交叉验证失败")
                pct = bar_pct
                if member["selection_source"] == "真实快照" and snap is not None:
                    snapshot_pct = pd.to_numeric(snap.get("涨跌幅(%)"), errors="coerce")
                    if not pd.isna(snapshot_pct):
                        if abs(float(snapshot_pct) - calculated_pct) > ROUNDING_TOLERANCE_PCT:
                            raise TaoxiDataError(f"{trade_date.date()} {code}快照涨跌幅交叉验证失败")
                        pct = float(snapshot_pct)
                close_ratio = 1 + pct / 100
            day_inputs.append({"trade_date": trade_date, "代码": code, "名称": member.get("名称", ""),
                "rank": int(member["rank"]), "weight": float(member["weight"]), "selection_date": member["selection_date"],
                "selection_source": member["selection_source"], "open": open_price, "close": close_price,
                "preclose": preclose, "pctChg": pct, "open_ratio": open_ratio, "close_ratio": close_ratio,
                "停牌状态": halt_status, "停牌依据": halt_evidence})
        input_hash = _hash_rows(day_inputs)
        open_point = previous_close * sum(row["open_ratio"] * row["weight"] for row in day_inputs)
        close_point = previous_close * sum(row["close_ratio"] * row["weight"] for row in day_inputs)
        stage = str(effective["selection_source"].iloc[0])
        history.append({"trade_date": trade_date, "open": open_point, "close": close_point,
            "source_stage": stage, "quality_status": "通过", "input_hash": input_hash})
        input_rows.extend(day_inputs)
        quality_rows.append({"trade_date": trade_date, "effective_count": 20, "weight_sum": 1.0,
            "open_input_count": 20, "close_input_count": 20, "unknown_halt_count": 0,
            "quality_status": "通过", "input_hash": input_hash})
        previous_close = close_point
    return pd.DataFrame(history), constituents, pd.DataFrame(input_rows), pd.DataFrame(quality_rows)


def _assert_published_history_unchanged(history: pd.DataFrame) -> None:
    existing, _ = load_dataset(VERSION, HISTORY_SOURCE, "taoxi_index_history")
    if existing is None or existing.empty:
        return
    required = {"trade_date", "open", "close", "source_stage", "quality_status", "input_hash"}
    if not required.issubset(existing.columns) or not required.issubset(history.columns):
        raise TaoxiDataError("已发布历史或新序列缺少不可变字段")
    old = existing[list(required)].copy()
    new = history[list(required)].copy()
    for frame in (old, new):
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.normalize()
        if frame["trade_date"].isna().any() or frame["trade_date"].duplicated().any():
            raise TaoxiDataError("桃囍微盘历史日期无效或重复")
    old = old.set_index("trade_date").sort_index()
    new = new.set_index("trade_date").sort_index()
    missing_dates = old.index.difference(new.index)
    if not missing_dates.empty:
        raise TaoxiDataError(f"新序列缺少已发布日期：{missing_dates[0].date()}")
    for trade_date, old_row in old.iterrows():
        new_row = new.loc[trade_date]
        for column in ("open", "close"):
            old_value = pd.to_numeric(old_row[column], errors="coerce")
            new_value = pd.to_numeric(new_row[column], errors="coerce")
            if pd.isna(old_value) or pd.isna(new_value) or not math.isclose(
                float(old_value), float(new_value), rel_tol=1e-12, abs_tol=1e-9
            ):
                raise TaoxiDataError(f"{trade_date.date()}已发布{column}点位发生变化，拒绝覆盖")
        for column in ("source_stage", "quality_status", "input_hash"):
            if str(old_row[column]) != str(new_row[column]):
                raise TaoxiDataError(f"{trade_date.date()}已发布{column}发生变化，拒绝覆盖")


def publish_taoxi_datasets(history: pd.DataFrame, constituents: pd.DataFrame, inputs: pd.DataFrame, quality: pd.DataFrame) -> None:
    published_dates = set(pd.to_datetime(history["trade_date"], errors="coerce").dropna()) - {BASE_DATE}
    published_quality = quality[pd.to_datetime(quality["trade_date"], errors="coerce").isin(published_dates)]
    if (history.empty or constituents.empty or published_quality.empty
            or len(published_quality) != len(published_dates)
            or not published_quality["quality_status"].eq("通过").all()):
        raise TaoxiDataError("桃囍微盘未通过发布门禁")
    _assert_published_history_unchanged(history)
    save_dataset(VERSION, "桃囍微盘正式开收盘", HISTORY_SOURCE, "taoxi_index_history", history)
    save_dataset(VERSION, "桃囍微盘每日成分", CONSTITUENT_SOURCE, "taoxi_index_constituents", constituents)
    save_dataset(VERSION, "桃囍微盘逐股行情输入", INPUT_SOURCE, "taoxi_index_inputs", inputs)
    save_dataset(VERSION, "桃囍微盘质量门禁", QUALITY_SOURCE, "taoxi_index_quality", quality)
    raw = history[["trade_date", "close"]].copy()
    save_dataset(VERSION, "桃囍微盘累计日线", "index_history", "index_daily_raw", raw)
    save_dataset(VERSION, "桃囍微盘收盘确认日线", "index_final_history", "index_daily_raw", raw)


def rebuild_taoxi_index(v3_dir: Path = DEFAULT_V3_DIR, snapshot_path: Path = SNAPSHOT_PATH):
    top, bars, sessions = load_v3_inputs(v3_dir)
    snapshots = pd.read_csv(snapshot_path, dtype={"代码": str})
    bars = refresh_incremental_formal_bars(bars, snapshots, sessions)
    result = build_taoxi_series(top, snapshots, bars, sessions)
    publish_taoxi_datasets(*result)
    return result


def load_taoxi_history() -> tuple[pd.DataFrame | None, dict | None]:
    return load_dataset(VERSION, HISTORY_SOURCE, "taoxi_index_history")


def load_taoxi_constituents() -> tuple[pd.DataFrame | None, dict | None]:
    return load_dataset(VERSION, CONSTITUENT_SOURCE, "taoxi_index_constituents")


def load_taoxi_quality() -> tuple[pd.DataFrame | None, dict | None]:
    return load_dataset(VERSION, QUALITY_SOURCE, "taoxi_index_quality")


def _secid(code: str) -> str:
    return ("1." if str(code).startswith("6") else "0.") + str(code)


def _tencent_constituent_rows(response, codes: list[str]) -> list[dict]:
    """Map one Tencent stock batch to the fields used by the intraday calculation."""
    expected = {("sh" if code.startswith("6") else "sz", code) for code in codes}
    rows = []
    for market, code, payload in re.findall(
        r'v_(sh|sz)(\d{6})="([^"]*)"', response.content.decode("gbk", errors="replace")
    ):
        fields = payload.split("~")
        if (market, code) not in expected or len(fields) < 38 or fields[2] != code:
            continue
        try:
            stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(
                tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp()
        except ValueError:
            stamp = None
        # Units differ between providers; volume/amount are used only to identify zero trades.
        rows.append({"f12": code, "f2": fields[3], "f18": fields[4], "f17": fields[5],
            "f5": fields[6], "f6": fields[37], "f124": stamp})
    return rows


def _wind_constituent_rows(codes: list[str]) -> list[dict]:
    """Map one Wind snapshot batch to the EastMoney-style fields used below."""
    from services.wind_source import fetch_wind_quotes, wind_stock_code

    by_wind = {wind_stock_code(code): code for code in codes}
    frame = fetch_wind_quotes("stock_data", list(by_wind))
    rows = []
    for _, row in frame.iterrows():
        quote_time = row.get("quote_time")
        stamp = None if pd.isna(quote_time) else pd.Timestamp(quote_time)
        if stamp is not None and stamp.tzinfo is None:
            stamp = stamp.tz_localize("Asia/Shanghai")
        rows.append({"f12": by_wind.get(str(row["wind_code"])), "f2": row.get("price"),
            "f18": row.get("previous_close"), "f17": row.get("open"), "f5": row.get("volume"),
            "f6": row.get("amount"), "f124": None if stamp is None else stamp.timestamp()})
    return rows


def _calculate_intraday_quote(
    rows: list[dict], codes: list[str], previous_close: float, current: datetime, source: str,
) -> dict[str, object]:
    by_code = {}
    for row in rows:
        code = str(row.get("f12") or "").zfill(6)
        if code in codes:
            if code in by_code:
                raise TaoxiDataError(f"盘中行情代码重复：{code}")
            by_code[code] = row
    ratios, quote_times = [], []
    for code in codes:
        row = by_code.get(code)
        if row is None:
            raise TaoxiDataError(f"盘中行情缺少{code}")
        latest, opened, preclose = (pd.to_numeric(row.get(key), errors="coerce") for key in ("f2", "f17", "f18"))
        volume, amount = (pd.to_numeric(row.get(key), errors="coerce") for key in ("f5", "f6"))
        halted = (not pd.isna(volume) and volume == 0) or (not pd.isna(amount) and amount == 0)
        if halted:
            ratios.append(1.0)
        elif any(pd.isna(value) or not math.isfinite(float(value)) or float(value) <= 0
                 for value in (latest, opened, preclose)):
            raise TaoxiDataError(f"盘中行情字段不完整：{code}")
        else:
            ratios.append(float(latest) / float(preclose))
        stamp = pd.to_numeric(row.get("f124"), errors="coerce")
        try:
            quote_time = datetime.fromtimestamp(float(stamp), ZoneInfo("Asia/Shanghai"))
        except (ValueError, OverflowError, OSError):
            quote_time = None
        if not halted and (quote_time is None or quote_time.date() != current.date()
                           or quote_time.timestamp() > current.timestamp() + 300):
            raise TaoxiDataError(f"盘中行情日期或时间无效：{code}")
        if quote_time is not None and quote_time.timestamp() <= current.timestamp() + 300:
            quote_times.append(quote_time)
    mean_ratio = sum(ratios) / 20
    return {"price": previous_close * mean_ratio, "previous_close": previous_close,
        "change_pct": (mean_ratio - 1) * 100,
        "quote_time": max(quote_times) if quote_times else current,
        "source": source, "constituent_count": 20}


def fetch_taoxi_intraday_quote(now: datetime | None = None) -> dict[str, object]:
    current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    history, _ = load_taoxi_history()
    constituents, _ = load_taoxi_constituents()
    if history is None or history.empty or constituents is None or constituents.empty:
        raise TaoxiDataError("桃囍微盘正式缓存尚未建立")
    latest_close_row = history.sort_values("trade_date").iloc[-1]
    previous_close = float(latest_close_row["close"])
    effective_date = pd.Timestamp(current.date())
    members = constituents[pd.to_datetime(constituents["effective_date"]).eq(effective_date)].sort_values("rank").copy()
    # CSV 读回时纯数字代码会成为整数，须恢复六位字符串才能匹配实时源。
    members["代码"] = _codes(members["代码"])
    if len(members) != 20:
        raise TaoxiDataError("当天没有20只已确认生效成分")
    codes = members["代码"].tolist()
    if len(set(codes)) != 20 or any(not re.fullmatch(r"[036]\d{5}", code) for code in codes):
        raise TaoxiDataError("当天成分代码无效或重复")
    selection_dates = pd.to_datetime(members["selection_date"], errors="coerce").dropna().dt.normalize().unique()
    latest_formal_date = pd.Timestamp(latest_close_row["trade_date"]).normalize()
    if len(selection_dates) != 1 or pd.Timestamp(selection_dates[0]).normalize() != latest_formal_date:
        raise TaoxiDataError("正式点位链存在缺口，禁止基于陈旧收盘计算盘中点位")
    params = {"fltt": 2, "invt": 2, "fields": "f2,f12,f17,f18,f5,f6,f124",
        "secids": ",".join(_secid(code) for code in codes)}
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    tencent_url = "https://qt.gtimg.cn/q=" + ",".join(
        ("sh" if code.startswith("6") else "sz") + code for code in codes
    )
    headers = {"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
    session = requests.Session()
    session.trust_env = False
    result = None
    failures = []
    try:
        # Try an independent direct source before falling back to environment proxies.
        # A partial, malformed or stale response is also a failed attempt, never a partial index.
        for use_proxy in (False, True):
            get = requests.get if use_proxy else session.get
            for provider, endpoint in (("东方财富", url), ("腾讯", tencent_url)):
                try:
                    response = get(endpoint, params=params if provider == "东方财富" else None,
                        headers=headers, timeout=(3, 5))
                    response.raise_for_status()
                    rows = (((response.json().get("data") or {}).get("diff") or [])
                            if provider == "东方财富" else _tencent_constituent_rows(response, codes))
                    result = _calculate_intraday_quote(
                        rows, codes, previous_close, current, f"{provider}20只成分批量实时行情"
                    )
                    break
                except (requests.RequestException, TaoxiDataError, ValueError, TypeError, AttributeError) as exc:
                    route = "代理" if use_proxy else "直连"
                    failures.append(f"{provider}{route}：{str(exc)[:180]}")
            if result is not None:
                break
    finally:
        session.close()
    if result is None:
        from services.wind_source import wind_api_key

        if wind_api_key():
            try:
                result = _calculate_intraday_quote(
                    _wind_constituent_rows(codes), codes, previous_close, current, "万得Wind20只成分批量实时行情"
                )
            except Exception as exc:
                failures.append(f"万得Wind：{str(exc)[:180]}")
    if result is None:
        raise TaoxiDataError("20只成分实时行情均失败：" + "；".join(failures))
    with _RUNTIME_LOCK:
        global _RUNTIME_QUOTE
        _RUNTIME_QUOTE = dict(result)
    return result


def load_runtime_taoxi_quote() -> dict[str, object] | None:
    with _RUNTIME_LOCK:
        return None if _RUNTIME_QUOTE is None else dict(_RUNTIME_QUOTE)


__all__ = [
    "INDEX_NAME", "INDEX_CODE", "VERSION", "TaoxiDataError", "classify_snapshot_halt_status",
    "select_snapshot_constituents", "validate_v3_directory", "build_taoxi_series",
    "publish_taoxi_datasets", "rebuild_taoxi_index", "load_taoxi_history",
    "load_taoxi_constituents", "load_taoxi_quality", "fetch_taoxi_intraday_quote", "load_runtime_taoxi_quote",
]
