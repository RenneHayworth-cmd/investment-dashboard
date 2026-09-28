"""BK1158 top-20 rotation monitor for the independent microcap live ledger.

Ranking reuses the ABCD simulation rule (``microcap_rotation_engine.ranking``):
exclude ST and suspended constituents, then sort by total market cap and code.
Pure functions only; the page decides when to load or fetch snapshots.
"""
from __future__ import annotations

import pandas as pd

from services.microcap import normalize_microcap_constituent_snapshots
from services.microcap_live_trading import build_microcap_positions
from services.microcap_rotation_data import snapshot_records
from services.microcap_rotation_engine import ranking
from services.microcap_rotation_policy import sessions

TOP_N = 20
HISTORY_DAYS = 10
OUT_LABEL = "持仓掉出前20"
NEW_LABEL = "新进前20未买入"
RANK_COLUMNS = ["code", "name", "market_cap", "rank", "excluded_reason"]
OUT_COLUMNS = ["code", "name", "rank", "rank_label", "market_cap", "gap_to_cutoff_pct", "holding_days", "streak_days"]
NEW_COLUMNS = ["code", "name", "rank", "market_cap", "gap_to_cutoff_pct", "streak_days"]
HISTORY_COLUMNS = ["date", "type", "code", "name", "rank_label", "market_cap", "gap_to_cutoff_pct"]


def rank_snapshot(snapshot_df: pd.DataFrame | None) -> pd.DataFrame:
    """Rank one day's BK1158 snapshot; ST and suspended rows keep a reason, no rank."""
    frame = normalize_microcap_constituent_snapshots(snapshot_df)
    if frame.empty:
        result = pd.DataFrame(columns=RANK_COLUMNS)
        result.attrs.update(snapshot_date=None, snapshot_time=None)
        return result
    days = frame["快照日期"].unique()
    if len(days) != 1:
        raise ValueError("rank_snapshot 只接受单日快照。")
    day = str(days[0])
    rows = snapshot_records(frame)
    constituents = [
        dict(code=r["code"], name=r["name"], market_cap=r["market_cap"],
             is_st="ST" in r["name"].upper(), asof=day, halted=r.get("halted"))
        for r in rows
    ]
    order = ranking({"date": day, "constituents": constituents, "quotes": {}}, limit=None)
    rank_of = {code: index + 1 for index, code in enumerate(order)}
    records = []
    for row in constituents:
        reason = "ST" if row["is_st"] else "停牌" if row["code"] not in rank_of else ""
        records.append(dict(code=row["code"], name=row["name"], market_cap=float(row["market_cap"]),
                            rank=rank_of.get(row["code"], pd.NA), excluded_reason=reason))
    result = pd.DataFrame(records, columns=RANK_COLUMNS)
    result["_order"] = pd.to_numeric(result["rank"], errors="coerce").fillna(10**9)
    result = result.sort_values(["_order", "market_cap", "code"]).drop(columns="_order").reset_index(drop=True)
    result.attrs.update(snapshot_date=day, snapshot_time=str(frame["快照时间"].max()))
    return result


def _ledger_events(trades: pd.DataFrame | None, adjustments: pd.DataFrame | None, day: str) -> list[tuple]:
    events = []
    for row in (trades.to_dict("records") if trades is not None and not trades.empty else []):
        if str(row["trade_date"]) <= day:
            delta = int(row["quantity"]) * (1 if row["side"] == "买入" else -1)
            events.append((str(row["trade_date"]), str(row.get("trade_time") or ""), 1, int(row.get("id") or 0), str(row["symbol"]), delta))
    for row in (adjustments.to_dict("records") if adjustments is not None and not adjustments.empty else []):
        if str(row["event_date"]) <= day:
            events.append((str(row["event_date"]), str(row.get("event_time") or ""), 0, int(row.get("id") or 0), str(row["symbol"]), int(row["quantity_delta"])))
    return sorted(events)


def holdings_as_of(trades: pd.DataFrame | None, adjustments: pd.DataFrame | None, day: str) -> pd.DataFrame:
    """Closing holdings on ``day`` with the date the current position was opened."""
    day = str(day)
    trade_part = trades.loc[trades["trade_date"].astype(str) <= day] if trades is not None and not trades.empty else pd.DataFrame(columns=["trade_date"])
    adjustment_part = (adjustments.loc[adjustments["event_date"].astype(str) <= day]
                       if adjustments is not None and not adjustments.empty else pd.DataFrame(columns=["event_date"]))
    positions = build_microcap_positions(trade_part, adjustment_part)
    opened: dict[str, str] = {}
    quantity: dict[str, int] = {}
    for event_day, _, _, _, code, delta in _ledger_events(trades, adjustments, day):
        before = quantity.get(code, 0)
        quantity[code] = before + delta
        if before <= 0 < quantity[code]:
            opened[code] = event_day
    positions = positions.copy()
    positions["opened_date"] = positions["symbol"].astype(str).map(opened)
    return positions


def trading_days_since(start: str | None, end: str) -> int | None:
    """Trading days elapsed after ``start`` up to ``end`` (buy day itself counts as 0)."""
    if not start or str(start) > str(end):
        return None
    return max(len(list(sessions(str(start), str(end)))) - 1, 0)


def compare_with_holdings(ranked: pd.DataFrame, holdings: pd.DataFrame, *, top_n: int = TOP_N,
                          as_of: str | None = None) -> dict:
    """Split one ranked snapshot against holdings into out-of-top-N and not-yet-bought."""
    ranks = pd.to_numeric(ranked["rank"], errors="coerce") if not ranked.empty else pd.Series(dtype=float)
    top = ranked.loc[ranks <= top_n] if not ranked.empty else ranked
    top_codes = set(top["code"].astype(str))
    cutoff = float(top["market_cap"].iloc[-1]) if len(top) >= top_n else None
    held = holdings if holdings is not None else pd.DataFrame(columns=["symbol", "name", "opened_date"])
    held_codes = set(held["symbol"].astype(str))
    by_code = ranked.set_index("code") if not ranked.empty else pd.DataFrame()

    def gap(cap):
        return (float(cap) / cutoff - 1) * 100 if cutoff and pd.notna(cap) else pd.NA

    out_rows = []
    for row in held.itertuples(index=False):
        code = str(row.symbol)
        if code in top_codes:
            continue
        if code in by_code.index:
            item = by_code.loc[code]
            rank = item["rank"]
            label = f"第{int(rank)}名" if pd.notna(rank) else f"已剔除（{item['excluded_reason']}）"
            cap = float(item["market_cap"])
            name = item["name"]
        else:
            rank, label, cap, name = pd.NA, "不在快照前400名", pd.NA, row.name
        opened = getattr(row, "opened_date", None)
        out_rows.append(dict(code=code, name=name, rank=rank, rank_label=label, market_cap=cap,
                             gap_to_cutoff_pct=gap(cap),
                             holding_days=trading_days_since(opened, as_of) if as_of else pd.NA,
                             streak_days=pd.NA))
    new_rows = [
        dict(code=str(r.code), name=r.name, rank=int(r.rank), market_cap=float(r.market_cap),
             gap_to_cutoff_pct=gap(r.market_cap), streak_days=pd.NA)
        for r in top.itertuples(index=False) if str(r.code) not in held_codes
    ]
    out = pd.DataFrame(out_rows, columns=OUT_COLUMNS)
    if not out.empty:
        out["_order"] = pd.to_numeric(out["rank"], errors="coerce").fillna(10**9)
        out = out.sort_values(["_order", "code"]).drop(columns="_order").reset_index(drop=True)
    return dict(out=out, new=pd.DataFrame(new_rows, columns=NEW_COLUMNS), cutoff=cutoff,
                top_codes=[str(c) for c in top["code"]])


def _first_position_date(trades, adjustments) -> str | None:
    """History starts when the account first holds stock; a cash-only build day would list all 20 names."""
    dates = []
    for frame, column in ((trades, "trade_date"), (adjustments, "event_date")):
        if frame is not None and not frame.empty and column in frame:
            dates.extend(frame[column].dropna().astype(str).tolist())
    return min(dates) if dates else None


def build_rotation_monitor(snapshots: pd.DataFrame | None, trades: pd.DataFrame | None,
                           adjustments: pd.DataFrame | None,
                           *, days: int = HISTORY_DAYS, top_n: int = TOP_N) -> dict:
    """Latest-day comparison plus a per-day history over the last ``days`` snapshots."""
    frame = normalize_microcap_constituent_snapshots(snapshots)
    empty = dict(latest_date=None, snapshot_time=None, cutoff=None,
                 out=pd.DataFrame(columns=OUT_COLUMNS), new=pd.DataFrame(columns=NEW_COLUMNS),
                 history=pd.DataFrame(columns=HISTORY_COLUMNS))
    if frame.empty:
        return empty
    window = sorted(frame["快照日期"].astype(str).unique())[-days:]
    start = _first_position_date(trades, adjustments)
    daily = {}
    for day in window:
        ranked = rank_snapshot(frame.loc[frame["快照日期"].astype(str) == day])
        holdings = holdings_as_of(trades, adjustments, day)
        daily[day] = (ranked, compare_with_holdings(ranked, holdings, top_n=top_n, as_of=day))

    def streak(test) -> int:
        count = 0
        for day in reversed(window):
            if not test(day):
                break
            count += 1
        return count

    latest = window[-1]
    latest_ranked, latest_result = daily[latest]
    out, new = latest_result["out"].copy(), latest_result["new"].copy()
    if not out.empty:
        out["streak_days"] = [streak(lambda d, c=c: c in set(daily[d][1]["out"]["code"])) for c in out["code"]]
    if not new.empty:
        new["streak_days"] = [streak(lambda d, c=c: c in daily[d][1]["top_codes"]) for c in new["code"]]

    history_rows = []
    for day in window:
        if start is None or day < start:
            continue
        result = daily[day][1]
        for row in result["out"].itertuples(index=False):
            history_rows.append(dict(date=day, type=OUT_LABEL, code=row.code, name=row.name, rank_label=row.rank_label,
                                     market_cap=row.market_cap, gap_to_cutoff_pct=row.gap_to_cutoff_pct))
        for row in result["new"].itertuples(index=False):
            history_rows.append(dict(date=day, type=NEW_LABEL, code=row.code, name=row.name, rank_label=f"第{row.rank}名",
                                     market_cap=row.market_cap, gap_to_cutoff_pct=row.gap_to_cutoff_pct))
    history = pd.DataFrame(history_rows, columns=HISTORY_COLUMNS)
    if not history.empty:
        history = history.sort_values(["date", "type", "code"], ascending=[False, True, True]).reset_index(drop=True)
    return dict(latest_date=latest, snapshot_time=latest_ranked.attrs.get("snapshot_time"),
                cutoff=latest_result["cutoff"], out=out, new=new, history=history,
                window=window, history_start=start)


def rank_realtime(stocks_df: pd.DataFrame, day: str) -> pd.DataFrame:
    """Rank a transient intraday EastMoney fetch as if it were ``day``'s snapshot (never saved)."""
    return rank_snapshot(normalize_microcap_constituent_snapshots(stocks_df, snapshot_date=str(day)))


__all__ = [
    "TOP_N", "HISTORY_DAYS", "OUT_LABEL", "NEW_LABEL", "rank_snapshot", "holdings_as_of",
    "trading_days_since", "compare_with_holdings", "build_rotation_monitor", "rank_realtime",
]
