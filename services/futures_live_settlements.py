from __future__ import annotations

from contextlib import closing
from datetime import datetime
import re

import pandas as pd

from core.db import get_conn, init_db
from services.futures_options_analysis import fetch_option_from_akshare, normalize_option_symbol
from services.futures_spread import completed_futures_daily_cutoff, fetch_futures_daily
from services.futures_live_calendar import _futures_trading_dates
from services.futures_live_models import normalize_contract
from services.futures_live_positions import (
    _option_contract_parts,
    build_estimated_positions,
)
from services.futures_live_repository import load_daily_closes
from services.futures_live_statement_parser import _date_text, _number


# Lazily resolved to avoid an import cycle. The compatibility facade may replace
# this dependency at call time so legacy patch paths keep working.
_historical_contract_requirements = None


def _infer_previous_settlement(
    latest_price: object,
    change_pct: object,
    *,
    tick_size: float = 0.1,
) -> float | None:
    latest = _number(latest_price)
    change = _number(change_pct)
    if latest is None or latest <= 0 or change is None or change <= -100:
        return None
    estimate = latest / (1 + change / 100)
    center = round(estimate / tick_size)
    candidates = [tick_size * value for value in range(center - 3, center + 4) if value > 0]
    if not candidates:
        return None
    best = min(
        candidates,
        key=lambda value: abs(round((latest - value) / value * 100, 2) - change),
    )
    implied = round((latest - best) / best * 100, 2)
    return round(best, 10) if abs(implied - change) <= 0.01 else None


def _save_settlement_price(
    asset_type: str,
    contract: str,
    trade_date: str,
    settlement_price: float,
    source: str,
) -> int:
    with closing(get_conn()) as conn:
        cursor = conn.execute(
            """
            UPDATE futures_daily_closes
            SET settlement_price=?, settlement_source=?, updated_at=?
            WHERE asset_type=? AND contract=? AND trade_date=?
              AND settlement_price IS NULL
            """,
            (
                settlement_price,
                source,
                datetime.now().isoformat(timespec="seconds"),
                asset_type,
                contract,
                trade_date,
            ),
        )
        conn.commit()
        return max(cursor.rowcount, 0)


def _save_daily_settlement_frame(
    asset_type: str,
    contract: str,
    data: pd.DataFrame,
    source: str,
    *,
    min_trade_date: str | None = None,
    max_trade_date: str | None = None,
) -> dict[str, object]:
    if data is None or data.empty:
        return {"updated": 0, "conflicts": []}
    now = datetime.now().isoformat(timespec="seconds")
    updated = 0
    conflicts: list[str] = []
    with closing(get_conn()) as conn:
        for record in data.to_dict("records"):
            trade_date = _date_text(record.get("date"))
            settlement = _number(record.get("settlement"))
            close = _number(record.get("close"))
            if (
                not trade_date
                or settlement is None
                or settlement < 0
                or (asset_type == "期货" and settlement <= 0)
            ):
                continue
            if min_trade_date and trade_date < min_trade_date:
                continue
            if max_trade_date and trade_date > max_trade_date:
                continue
            existing = conn.execute(
                """
                SELECT close_price, settlement_price
                FROM futures_daily_closes
                WHERE asset_type=? AND contract=? AND trade_date=?
                """,
                (asset_type, contract, trade_date),
            ).fetchone()
            if existing is None:
                if close is None or close < 0:
                    continue
                conn.execute(
                    """
                    INSERT INTO futures_daily_closes (
                        asset_type, contract, trade_date, close_price,
                        settlement_price, source, settlement_source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset_type,
                        contract,
                        trade_date,
                        close,
                        settlement,
                        source,
                        source,
                        now,
                    ),
                )
                updated += 1
                continue
            existing_settlement = _number(existing[1])
            if existing_settlement is None:
                conn.execute(
                    """
                    UPDATE futures_daily_closes
                    SET settlement_price=?, settlement_source=?, updated_at=?
                    WHERE asset_type=? AND contract=? AND trade_date=?
                      AND settlement_price IS NULL
                    """,
                    (
                        settlement,
                        source,
                        now,
                        asset_type,
                        contract,
                        trade_date,
                    ),
                )
                updated += 1
            elif abs(existing_settlement - settlement) > 1e-8:
                conflicts.append(
                    f"{contract} {trade_date} 已有结算价 {existing_settlement:g}，"
                    f"新来源返回 {settlement:g}，已保留原值"
                )
        conn.commit()
    return {"updated": updated, "conflicts": conflicts}


def _update_position_settlements(
    positions: pd.DataFrame,
    target_date: str,
    *,
    force: bool = False,
    market_now: datetime | None = None,
) -> dict[str, object]:
    cached = load_daily_closes()
    contracts = positions[["asset_type", "contract"]].drop_duplicates()
    needed: list[tuple[str, str]] = []
    skipped = 0
    for asset_type, contract in contracts.itertuples(index=False, name=None):
        existing = cached[
            cached["asset_type"].eq(asset_type)
            & cached["contract"].eq(contract)
            & cached["trade_date"].eq(target_date)
        ]
        if (
            not existing.empty
            and pd.notna(existing.iloc[-1].get("settlement_price"))
        ):
            skipped += 1
        else:
            needed.append((asset_type, contract))
    if not needed:
        return {"updated": 0, "skipped": skipped, "errors": []}

    import akshare as ak

    updated = 0
    errors: list[str] = []
    target_compact = target_date.replace("-", "")
    cffex_contracts = {
        contract
        for asset_type, contract in needed
        if asset_type == "期货" and re.match(r"^(IC|IF|IH|IM|T|TF|TS|TL)\d", contract)
    }
    cffex_daily = pd.DataFrame()
    if cffex_contracts:
        try:
            cffex_daily = ak.get_futures_daily(
                start_date=target_compact,
                end_date=target_compact,
                market="CFFEX",
            )
        except Exception as exc:
            errors.extend(f"{contract}结算价：{exc}" for contract in sorted(cffex_contracts))

    option_chains: dict[str, pd.DataFrame] = {}
    current_day = (market_now or datetime.now()).date()
    for asset_type, contract in needed:
        try:
            settlement = None
            source = ""
            if asset_type == "期货" and contract in cffex_contracts:
                rows = cffex_daily[
                    cffex_daily["symbol"].astype(str).str.upper().eq(contract)
                ] if not cffex_daily.empty else pd.DataFrame()
                if not rows.empty:
                    settlement = _number(rows.iloc[-1].get("settle"))
                    source = "中金所日行情结算价"
            elif asset_type == "期货":
                raw = ak.futures_zh_daily_sina(symbol=contract.lower())
                raw_dates = pd.to_datetime(raw.get("date"), errors="coerce")
                rows = raw[raw_dates.dt.strftime("%Y-%m-%d").eq(target_date)]
                if not rows.empty:
                    settlement = _number(rows.iloc[-1].get("settle"))
                    source = "新浪期货日线结算价"
            else:
                matched = re.match(r"^([A-Z]+\d{4})[CP]\d+$", contract)
                if not matched or not contract.startswith("I"):
                    raise RuntimeError("当前仅支持铁矿石期权结算价。")
                if pd.Timestamp(target_date).date() >= current_day:
                    raise RuntimeError("当日结算价需在下一交易日行情发布后确认。")
                underlying = matched.group(1).lower()
                if underlying not in option_chains:
                    option_chains[underlying] = ak.option_commodity_contract_table_sina(
                        symbol="铁矿石期权",
                        contract=underlying,
                    )
                chain = option_chains[underlying]
                rows = chain[
                    chain["看跌合约-看跌期权合约"]
                    .astype(str)
                    .str.upper()
                    .eq(contract)
                ]
                if not rows.empty:
                    row = rows.iloc[-1]
                    settlement = _infer_previous_settlement(
                        row.get("看跌合约-最新价"),
                        row.get("看跌合约-涨跌"),
                    )
                    source = "新浪期权链反推昨结算价"
            if settlement is None or settlement <= 0:
                raise RuntimeError("未取得有效结算价。")
            updated += _save_settlement_price(
                asset_type,
                contract,
                target_date,
                settlement,
                source,
            )
        except Exception as exc:
            message = f"{contract}结算价：{exc}"
            if message not in errors:
                errors.append(message)
    return {"updated": updated, "skipped": skipped, "errors": errors}


def _fetch_futures_settlement_history(
    contract: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[pd.DataFrame, str]:
    import akshare as ak

    raw = ak.futures_zh_daily_sina(symbol=contract.lower())
    source = "新浪期货日线结算价"
    if raw is None or raw.empty:
        raise RuntimeError(f"{source}未返回数据。")
    required = {"date", "close", "settle"}
    if not required.issubset(raw.columns):
        raise RuntimeError("新浪期货日线缺少日期、收盘价或结算价字段。")
    result = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["date"], errors="coerce"),
            "close": pd.to_numeric(raw["close"], errors="coerce"),
            "settlement": pd.to_numeric(raw["settle"], errors="coerce"),
        }
    ).dropna(subset=["date", "settlement"])
    return result, source


def _fetch_cffex_settlement_history(
    contracts: set[str],
    start_date: str,
    end_date: str,
) -> tuple[pd.DataFrame, str]:
    from io import BytesIO, StringIO
    import zipfile

    import requests

    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    months = pd.period_range(start=start, end=end, freq="M")
    rows: list[dict[str, object]] = []
    headers = {"User-Agent": "Mozilla/5.0"}
    for month in months:
        month_text = month.strftime("%Y%m")
        response = requests.get(
            f"http://www.cffex.com.cn/sj/historysj/{month_text}/zip/{month_text}.zip",
            headers=headers,
            timeout=(3.05, 10),
        )
        response.raise_for_status()
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            for file_name in archive.namelist():
                matched = re.search(r"(\d{8})_1\.csv$", file_name)
                if not matched:
                    continue
                trade_date = pd.Timestamp(matched.group(1))
                if trade_date < start or trade_date > end:
                    continue
                with archive.open(file_name) as file_object:
                    raw = pd.read_csv(
                        StringIO(file_object.read().decode("gb2312"))
                    )
                if raw.shape[1] < 10:
                    continue
                raw_symbols = raw.iloc[:, 0].astype(str).str.strip().str.upper()
                symbols = raw_symbols.map(
                    lambda value: normalize_contract(
                        value,
                        "期权" if re.search(r"-[CP]-", value) else "期货",
                    )
                )
                selected = raw[symbols.isin(contracts)]
                for selected_index in selected.index:
                    rows.append(
                        {
                            "date": trade_date,
                            "contract": symbols.loc[selected_index],
                            "close": _number(raw.loc[selected_index].iloc[8]),
                            "settlement": _number(raw.loc[selected_index].iloc[9]),
                        }
                    )
    result = pd.DataFrame(rows)
    if result.empty:
        raise RuntimeError("中金所历史日行情未返回所需合约。")
    return result, "中金所历史日行情结算价"


def _parse_dce_option_settlement_payload(
    payload: object,
    trade_date: str,
    contracts: set[str],
) -> pd.DataFrame:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("大商所期权日行情返回格式异常。")
    rows: list[dict[str, object]] = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        try:
            contract = normalize_contract(item.get("contractId"), "期权")
        except ValueError:
            continue
        if contract not in contracts:
            continue
        settlement = _number(item.get("clearPrice"))
        close = _number(item.get("close"))
        if settlement is None or settlement < 0:
            continue
        rows.append(
            {
                "date": trade_date,
                "contract": contract,
                "close": close,
                "settlement": settlement,
            }
        )
    return pd.DataFrame(rows)


def _fetch_dce_option_settlements_for_date(
    trade_date: str,
    contracts: set[str],
) -> tuple[pd.DataFrame, str]:
    import requests

    request_payload = {
        "contractId": "",
        "lang": "zh",
        "optionSeries": "",
        "statisticsType": 0,
        "tradeDate": trade_date.replace("-", ""),
        "tradeType": "2",
        "varietyId": "i",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.dce.com.cn/",
    }
    failures: list[str] = []
    for url in (
        "https://www.dce.com.cn/dcereport/publicweb/dailystat/dayQuotes",
        "http://www.dce.com.cn/dcereport/publicweb/dailystat/dayQuotes",
    ):
        try:
            response = requests.post(
                url,
                json=request_payload,
                headers=headers,
                timeout=(3.05, 8),
            )
            response.raise_for_status()
            frame = _parse_dce_option_settlement_payload(
                response.json(), trade_date, contracts
            )
            return frame, "大商所期权日行情结算价"
        except Exception as exc:
            failures.append(str(exc))
    detail = failures[-1] if failures else "未知错误"
    raise RuntimeError(f"大商所期权日行情不可用：{detail}")


def update_traded_contract_daily_settlements(
    *,
    force: bool = False,
    market_now: datetime | None = None,
) -> dict[str, object]:
    requirements_loader = _historical_contract_requirements
    if requirements_loader is None:
        from services.futures_live_prices import (
            _historical_contract_requirements as requirements_loader,
        )

    requirements = requirements_loader(market_now=market_now)
    if requirements.empty:
        return {
            "updated": 0,
            "skipped": 0,
            "errors": [],
            "conflicts": [],
            "contracts": 0,
        }
    cached = load_daily_closes()
    existing_settlements = {
        (str(row.asset_type), str(row.contract), str(row.trade_date))
        for row in cached.itertuples(index=False)
        if pd.notna(row.settlement_price)
    }
    updated = skipped = 0
    errors: list[str] = []
    conflicts: list[str] = []

    option_requirements: list[dict[str, object]] = []
    cffex_requirements: list[dict[str, object]] = []
    for requirement in requirements.to_dict("records"):
        asset_type = str(requirement["asset_type"])
        contract = str(requirement["contract"])
        first_date = str(requirement["first_date"])
        target_date = str(requirement["target_date"])
        if asset_type == "期权" and re.match(r"^(IO|HO|MO)\d", contract):
            cffex_requirements.append(requirement)
            continue
        if asset_type == "期权":
            option_requirements.append(requirement)
            continue
        if re.match(r"^(IC|IF|IH|IM|T|TF|TS|TL)\d", contract):
            cffex_requirements.append(requirement)
            continue
        required_days = _futures_trading_dates(
            pd.Timestamp(first_date).date(), pd.Timestamp(target_date).date()
        )
        missing_days = [
            day
            for day in required_days
            if (asset_type, contract, day) not in existing_settlements
        ]
        if not missing_days and not force:
            skipped += 1
            continue
        try:
            frame, source = _fetch_futures_settlement_history(
                contract,
                start_date=first_date,
                end_date=target_date,
            )
            saved = _save_daily_settlement_frame(
                asset_type,
                contract,
                frame,
                source,
                min_trade_date=first_date,
                max_trade_date=target_date,
            )
            updated += int(saved["updated"])
            conflicts.extend(saved["conflicts"])
        except Exception as exc:
            errors.append(f"{contract}历史结算价：{exc}")

    pending_cffex: list[dict[str, object]] = []
    for requirement in cffex_requirements:
        asset_type = str(requirement["asset_type"])
        contract = str(requirement["contract"])
        required_days = _futures_trading_dates(
            pd.Timestamp(requirement["first_date"]).date(),
            pd.Timestamp(requirement["target_date"]).date(),
        )
        missing_days = [
            day
            for day in required_days
            if (asset_type, contract, day) not in existing_settlements
        ]
        if not missing_days and not force:
            skipped += 1
        else:
            pending_cffex.append(requirement)
    if pending_cffex:
        cffex_contracts = {str(item["contract"]) for item in pending_cffex}
        cffex_start = min(str(item["first_date"]) for item in pending_cffex)
        cffex_end = max(str(item["target_date"]) for item in pending_cffex)
        try:
            cffex_frame, cffex_source = _fetch_cffex_settlement_history(
                cffex_contracts,
                cffex_start,
                cffex_end,
            )
            for requirement in pending_cffex:
                asset_type = str(requirement["asset_type"])
                contract = str(requirement["contract"])
                contract_frame = cffex_frame[cffex_frame["contract"].eq(contract)]
                saved = _save_daily_settlement_frame(
                    asset_type,
                    contract,
                    contract_frame,
                    cffex_source,
                    min_trade_date=str(requirement["first_date"]),
                    max_trade_date=str(requirement["target_date"]),
                )
                updated += int(saved["updated"])
                conflicts.extend(saved["conflicts"])
                if contract_frame.empty:
                    errors.append(f"{contract}历史结算价：中金所未返回该合约。")
        except Exception as exc:
            errors.append(f"中金所历史结算价：{exc}")

    option_days: dict[str, set[str]] = {}
    for requirement in option_requirements:
        contract = str(requirement["contract"])
        parts = _option_contract_parts(contract)
        if parts is None or parts["product"] != "I":
            errors.append(f"{contract}历史结算价：当前仅支持铁矿石期权。")
            continue
        first_date = str(requirement["first_date"])
        target_date = str(requirement["target_date"])
        required_days = _futures_trading_dates(
            pd.Timestamp(first_date).date(), pd.Timestamp(target_date).date()
        )
        missing_days = [
            day
            for day in required_days
            if ("期权", contract, day) not in existing_settlements
        ]
        if not missing_days and not force:
            skipped += 1
            continue
        for day in required_days if force else missing_days:
            option_days.setdefault(day, set()).add(contract)

    for day in sorted(option_days):
        contracts = option_days[day]
        try:
            frame, source = _fetch_dce_option_settlements_for_date(day, contracts)
        except Exception as exc:
            errors.append(f"铁矿石期权 {day} 起历史结算价：{exc}")
            break
        returned = set(frame["contract"].astype(str)) if not frame.empty else set()
        for contract in sorted(contracts):
            contract_frame = frame[frame["contract"].eq(contract)] if not frame.empty else frame
            if contract not in returned:
                errors.append(f"{contract} {day}：大商所未返回正式结算价。")
                continue
            saved = _save_daily_settlement_frame(
                "期权",
                contract,
                contract_frame,
                source,
                min_trade_date=day,
                max_trade_date=day,
            )
            updated += int(saved["updated"])
            conflicts.extend(saved["conflicts"])
    return {
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "conflicts": conflicts,
        "contracts": len(requirements),
    }
