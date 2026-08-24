from __future__ import annotations

from contextlib import closing
from datetime import datetime

import pandas as pd

from core.db import get_conn, init_db
from services.futures_options_analysis import (
    fetch_option_from_akshare,
    normalize_option_symbol,
)
from services.futures_spread import completed_futures_daily_cutoff, fetch_futures_daily
from services.futures_live_models import normalize_contract
from services.futures_live_positions import (
    _option_contract_parts,
    build_estimated_positions,
    iron_ore_option_expiry_date,
)
from services.futures_live_repository import list_futures_live_trades, load_daily_closes
from services.futures_live_settlements import _update_position_settlements
from services.futures_live_statement_parser import _date_text, _number

def _save_daily_close_frame(
    asset_type: str,
    contract: str,
    data: pd.DataFrame,
    source: str,
    *,
    max_trade_date: str | None = None,
) -> int:
    if data is None or data.empty:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    saved = 0
    with closing(get_conn()) as conn:
        for record in data.to_dict("records"):
            trade_date = _date_text(record.get("date"))
            close = _number(record.get("close"))
            if not trade_date or close is None or close <= 0:
                continue
            if max_trade_date and trade_date > max_trade_date:
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO futures_daily_closes (
                    asset_type, contract, trade_date, close_price, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (asset_type, contract, trade_date, close, source, now),
            )
            saved += max(cursor.rowcount, 0)
        conn.commit()
    return saved


def update_position_daily_closes(
    *,
    api_key: str = "",
    force: bool = False,
    market_now: datetime | None = None,
) -> dict[str, object]:
    positions = build_estimated_positions()
    if positions.empty:
        return {"updated": 0, "skipped": 0, "errors": [], "target_date": None}
    positions = positions[
        pd.to_numeric(positions["estimated_quantity"], errors="coerce")
        .fillna(0)
        .gt(0)
    ]
    if positions.empty:
        return {"updated": 0, "skipped": 0, "errors": [], "target_date": None}
    target = completed_futures_daily_cutoff(market_now).strftime("%Y-%m-%d")
    cached = load_daily_closes()
    updated = skipped = 0
    errors: list[str] = []
    contracts = positions[["asset_type", "contract"]].drop_duplicates()
    for asset_type, contract in contracts.itertuples(index=False, name=None):
        existing = cached[
            cached["asset_type"].eq(asset_type) & cached["contract"].eq(contract)
        ]
        latest = existing["trade_date"].max() if not existing.empty else None
        if not force and latest is not None and str(latest) >= target:
            skipped += 1
            continue
        try:
            if asset_type == "期权":
                option_symbol = normalize_option_symbol(contract)
                data, source, is_chain = fetch_option_from_akshare(
                    option_symbol,
                    "1d",
                    5000,
                    prefer_realtime_snapshot=False,
                    market_now=market_now,
                )
                if is_chain:
                    raise RuntimeError("期权行情返回了期权链而不是合约日线。")
            else:
                data = fetch_futures_daily(
                    contract,
                    api_key=api_key,
                    prefer_realtime_snapshot=False,
                    market_now=market_now,
                )
                source = "TickFlow/AkShare期货日线"
            updated += _save_daily_close_frame(
                asset_type,
                contract,
                data,
                source,
                max_trade_date=target,
            )
        except Exception as exc:
            errors.append(f"{contract}：{exc}")
    settlement_result = _update_position_settlements(
        positions,
        target,
        force=force,
        market_now=market_now,
    )
    return {
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "settlement_updated": settlement_result["updated"],
        "settlement_skipped": settlement_result["skipped"],
        "settlement_errors": settlement_result["errors"],
        "target_date": target,
    }


def _historical_contract_requirements(
    *, market_now: datetime | None = None
) -> pd.DataFrame:
    trades = list_futures_live_trades(include_taken_over=False)
    if trades.empty:
        return pd.DataFrame()
    trades = trades.copy()
    trades["trade_date"] = pd.to_datetime(trades["trade_date"], errors="coerce")
    trades = trades.dropna(subset=["trade_date"])
    latest_target = completed_futures_daily_cutoff(market_now).strftime("%Y-%m-%d")
    current = build_estimated_positions()
    active_keys = set()
    if not current.empty:
        active = current[
            pd.to_numeric(current["estimated_quantity"], errors="coerce").fillna(0).gt(0)
        ]
        active_keys = set(zip(active["asset_type"], active["contract"]))
    requirements: dict[tuple[str, str], dict[str, object]] = {}
    for (asset_type, contract), group in trades.groupby(["asset_type", "contract"]):
        first_date = group["trade_date"].min().strftime("%Y-%m-%d")
        last_trade_date = group["trade_date"].max().strftime("%Y-%m-%d")
        target_date = latest_target if (asset_type, contract) in active_keys else last_trade_date
        if asset_type == "期权":
            expiry = iron_ore_option_expiry_date(str(contract))
            if expiry:
                target_date = max(last_trade_date, min(expiry, latest_target))
        requirements[(asset_type, contract)] = {
            "asset_type": asset_type,
            "contract": contract,
            "first_date": first_date,
            "target_date": target_date,
        }
    option_rows = [
        row for row in requirements.values() if row["asset_type"] == "期权"
    ]
    for row in option_rows:
        parts = _option_contract_parts(str(row["contract"]))
        if parts is None or parts["product"] != "I":
            continue
        key = ("期货", str(parts["underlying_contract"]))
        existing = requirements.get(key)
        if existing is None:
            requirements[key] = {
                "asset_type": "期货",
                "contract": key[1],
                "first_date": row["first_date"],
                "target_date": row["target_date"],
            }
        else:
            existing["first_date"] = min(
                str(existing["first_date"]), str(row["first_date"])
            )
            existing["target_date"] = max(
                str(existing["target_date"]), str(row["target_date"])
            )
    rows = list(requirements.values())
    return pd.DataFrame(rows).sort_values(["asset_type", "contract"]).reset_index(drop=True)


def update_traded_contract_daily_closes(
    *,
    api_key: str = "",
    force: bool = False,
    market_now: datetime | None = None,
) -> dict[str, object]:
    requirements = _historical_contract_requirements(market_now=market_now)
    if requirements.empty:
        return {"updated": 0, "skipped": 0, "errors": [], "contracts": 0}
    cached = load_daily_closes()
    updated = skipped = 0
    errors: list[str] = []
    for requirement in requirements.to_dict("records"):
        asset_type = str(requirement["asset_type"])
        contract = str(requirement["contract"])
        existing = cached[
            cached["asset_type"].eq(asset_type) & cached["contract"].eq(contract)
        ]
        has_coverage = (
            not existing.empty
            and str(existing["trade_date"].min()) <= str(requirement["first_date"])
            and str(existing["trade_date"].max()) >= str(requirement["target_date"])
        )
        if has_coverage and not force:
            skipped += 1
            continue
        try:
            if asset_type == "期权":
                data, source, is_chain = fetch_option_from_akshare(
                    normalize_option_symbol(contract),
                    "1d",
                    5000,
                    prefer_realtime_snapshot=False,
                    market_now=market_now,
                )
                if is_chain:
                    raise RuntimeError("期权行情返回了期权链而不是合约日线。")
            else:
                data = fetch_futures_daily(
                    contract,
                    api_key=api_key,
                    prefer_realtime_snapshot=False,
                    market_now=market_now,
                )
                source = "TickFlow/AkShare期货日线"
            updated += _save_daily_close_frame(
                asset_type,
                contract,
                data,
                source,
                max_trade_date=str(requirement["target_date"]),
            )
        except Exception as exc:
            errors.append(f"{contract}：{exc}")
    return {
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "contracts": len(requirements),
    }
