"""首页账户总览：只读本地正式数据，合并 ETF、微盘、期货三个实盘账本。"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from core import db

ACCOUNT_LABELS = {"etf": "ETF实盘", "microcap": "微盘实盘", "futures": "期货实盘"}
# 盯市口径已扣成交及行权手续费；券商客户权益还会扣这些账户级费用。
FUTURES_BROKER_ONLY_FEE_TYPES = ("申报费", "账户费用")
COMBINED_COLUMNS = [
    "date",
    "pnl_amount",
    "return_base",
    "return_pct",
    "nav",
    "cumulative_return_pct",
    "cumulative_pnl",
    "confirmation_status",
]


@dataclass
class AccountSeries:
    """One ledger's completed daily valuations in a shared shape."""

    key: str
    daily: pd.DataFrame
    total_assets: float | None = None
    cumulative_pnl: float | None = None
    valuation_date: pd.Timestamp | None = None
    warnings: list[str] = field(default_factory=list)
    pre_ledger_pnl: float = 0.0
    pre_ledger_through: pd.Timestamp | None = None

    @property
    def label(self) -> str:
        return ACCOUNT_LABELS[self.key]

    @property
    def inception_pnl(self) -> float:
        """开户以来累计盈亏 = 建账前盈亏 + 账本累计盈亏。"""
        return float(self.cumulative_pnl or 0.0) + float(self.pre_ledger_pnl or 0.0)


def _account_frame(
    dates,
    pnl,
    base,
    *,
    confirmation=None,
) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(pd.Series(list(dates), dtype="object"), errors="coerce").dt.normalize(),
            "pnl_amount": pd.to_numeric(pd.Series(list(pnl), dtype="object"), errors="coerce"),
            "return_base": pd.to_numeric(pd.Series(list(base), dtype="object"), errors="coerce"),
        }
    )
    frame["confirmation_status"] = (
        pd.Series(list(confirmation), index=frame.index, dtype="object").fillna("正式").astype(str)
        if confirmation is not None
        else "正式"
    )
    return frame.dropna(subset=["date", "pnl_amount"]).sort_values("date").reset_index(drop=True)


def etf_account_series(account_daily: pd.DataFrame) -> AccountSeries:
    """ETF实盘账户口径：当日盈亏已剔除资金进出，分母为前一估值总资产加正净入金。"""
    if account_daily is None or account_daily.empty:
        return AccountSeries("etf", _account_frame([], [], []))
    daily = _account_frame(account_daily["date"], account_daily["daily_pnl"], account_daily["return_base"])
    latest = account_daily.iloc[-1]
    return AccountSeries(
        "etf",
        daily,
        total_assets=float(latest["total_assets"]),
        cumulative_pnl=float(latest["account_pnl"]),
        valuation_date=pd.Timestamp(latest["date"]).normalize(),
    )


def microcap_account_series(daily_returns: pd.DataFrame) -> AccountSeries:
    """微盘实盘：每行总资产 = 收益率分母 + 当日盈亏，首日分母为期初资金与期初持仓。"""
    if daily_returns is None or daily_returns.empty:
        return AccountSeries("microcap", _account_frame([], [], []))
    assets = pd.to_numeric(daily_returns["total_assets"], errors="coerce")
    pnl = pd.to_numeric(daily_returns["pnl_amount"], errors="coerce")
    daily = _account_frame(daily_returns["date"], pnl, assets - pnl)
    latest = daily_returns.iloc[-1]
    return AccountSeries(
        "microcap",
        daily,
        total_assets=float(latest["total_assets"]),
        cumulative_pnl=float(latest["total_pnl"]),
        valuation_date=pd.Timestamp(latest["date"]).normalize(),
    )


def _attributed_fees(valued_dates: pd.Series, account_fees: pd.DataFrame | None) -> pd.Series:
    """把账户级费用归到发生日当天或之后的第一个估值日；晚于最新估值日的暂不计入。"""
    dates = pd.to_datetime(valued_dates, errors="coerce").dt.normalize().reset_index(drop=True)
    attributed = pd.Series(0.0, index=dates.index)
    if account_fees is None or account_fees.empty or dates.empty:
        return attributed
    fee_dates = pd.to_datetime(account_fees["date"], errors="coerce").dt.normalize()
    amounts = pd.to_numeric(account_fees["amount"], errors="coerce").fillna(0.0)
    for fee_date, amount in zip(fee_dates, amounts):
        if pd.isna(fee_date):
            continue
        position = int(dates.searchsorted(fee_date, side="left"))
        if position < len(dates):
            attributed.iloc[position] += float(amount)
    return attributed


def futures_account_series(
    settlement_daily: pd.DataFrame,
    account_fees: pd.DataFrame | None = None,
) -> AccountSeries:
    """期货实盘按券商客户权益口径：盯市结果再扣申报费等账户级费用。

    盯市逐日数据沿用期货页的完整/手工估算日；`account_fees` 为 date/amount 两列的
    申报费、账户费用，计入发生日当天或之后的第一个估值日。
    """
    required = {"date", "status", "daily_pnl", "return_base", "economic_equity", "net_pnl"}
    if settlement_daily is None or settlement_daily.empty or not required.issubset(settlement_daily.columns):
        return AccountSeries("futures", _account_frame([], [], []))
    valued = settlement_daily[
        settlement_daily["status"].isin(["完整", "手工估算"])
        & pd.to_numeric(settlement_daily["daily_pnl"], errors="coerce").notna()
    ]
    if valued.empty:
        return AccountSeries("futures", _account_frame([], [], []))
    fees = _attributed_fees(valued["date"], account_fees)
    fees_before = fees.cumsum() - fees
    daily = _account_frame(
        valued["date"],
        pd.to_numeric(valued["daily_pnl"], errors="coerce").to_numpy() - fees.to_numpy(),
        pd.to_numeric(valued["return_base"], errors="coerce").to_numpy() - fees_before.to_numpy(),
        confirmation=valued.get("confirmation_status"),
    )
    total_fees = float(fees.sum())
    latest = valued.iloc[-1]
    warnings = []
    incomplete = settlement_daily[settlement_daily["status"].eq("数据不完整")]
    if not incomplete.empty and str(incomplete.iloc[-1]["date"]) > str(latest["date"]):
        warnings.append(f"{incomplete.iloc[-1]['date']} 盯市估值缺少结算价，暂按 {latest['date']} 展示。")
    return AccountSeries(
        "futures",
        daily,
        total_assets=float(latest["economic_equity"]) - total_fees,
        cumulative_pnl=float(latest["net_pnl"]) - total_fees,
        valuation_date=pd.Timestamp(latest["date"]).normalize(),
        warnings=warnings,
    )


def combined_start_date(accounts: list[AccountSeries]) -> pd.Timestamp | None:
    """合计从ETF实盘建账日起算：此前两家券商的股票账户不在账本中，只有期货不代表总账。

    微盘实盘资金由ETF实盘划出，不改变起点；没有ETF账本时退回最早的账户。
    """
    starts = {account.key: account.daily["date"].min() for account in accounts if not account.daily.empty}
    if not starts:
        return None
    return starts.get("etf", min(starts.values()))


def combine_account_series(accounts: list[AccountSeries], *, start: object = None) -> pd.DataFrame:
    """按日合并已估值账户：盈亏金额与收益率分母分别相加，再按日复合总净值。

    合计序列截止到所有已开户账户都完成估值的最后一天，避免某个账本缺正式数据时
    把其余账本的收益当作全部收益。某账户中间缺一天时，它的盈亏会体现在下一个估值日。
    """
    frames = [account.daily for account in accounts if not account.daily.empty]
    if not frames:
        return pd.DataFrame(columns=COMBINED_COLUMNS)
    end = min(frame["date"].max() for frame in frames)
    stacked = pd.concat(frames, ignore_index=True)
    stacked = stacked[stacked["date"].le(end)]
    if start is not None:
        start = pd.Timestamp(start).normalize()
        stacked = stacked[stacked["date"].ge(start)].copy()
        # 起算日收盘是合计净值的基准，当天已发生的盈亏属于起算之前。
        stacked.loc[stacked["date"].eq(start), "pnl_amount"] = 0.0
    if stacked.empty:
        return pd.DataFrame(columns=COMBINED_COLUMNS)
    pending = stacked["confirmation_status"].where(stacked["confirmation_status"].ne("正式"))
    combined = (
        stacked.assign(pending=pending)
        .groupby("date", as_index=False)
        .agg(
            pnl_amount=("pnl_amount", "sum"),
            return_base=("return_base", "sum"),
            confirmation_status=("pending", lambda values: "、".join(dict.fromkeys(values.dropna())) or "正式"),
        )
        .sort_values("date")
        .reset_index(drop=True)
    )
    valid = combined["return_base"].gt(0)
    combined["return_pct"] = (combined["pnl_amount"] / combined["return_base"] * 100).where(valid, 0.0)
    combined["nav"] = (1.0 + combined["return_pct"] / 100.0).cumprod()
    combined["cumulative_return_pct"] = (combined["nav"] - 1.0) * 100.0
    combined["cumulative_pnl"] = combined["pnl_amount"].cumsum()
    return combined[COMBINED_COLUMNS]


def load_pre_ledger_pnl() -> dict[str, dict[str, object]]:
    """各账本建账前的盈亏（开户至建账日收盘），由用户核对后录入。"""
    db.init_db()
    with closing(db.get_conn()) as conn:
        rows = conn.execute(
            "SELECT account, through_date, amount, notes FROM live_pre_ledger_pnl"
        ).fetchall()
    return {
        str(row[0]): {"through_date": str(row[1]), "amount": float(row[2]), "notes": row[3] or ""}
        for row in rows
    }


def save_pre_ledger_pnl(account: str, *, through_date: str, amount: float, notes: str = "") -> None:
    if account not in ACCOUNT_LABELS:
        raise ValueError(f"未知账户：{account}")
    db.init_db()
    with closing(db.get_conn()) as conn:
        conn.execute(
            """
            INSERT INTO live_pre_ledger_pnl (account, through_date, amount, notes, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(account) DO UPDATE SET
                through_date=excluded.through_date, amount=excluded.amount,
                notes=excluded.notes, updated_at=excluded.updated_at
            """,
            (account, str(pd.Timestamp(through_date).date()), float(amount), notes,
             datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()


def _futures_broker_only_fees() -> pd.DataFrame:
    from services.futures_live_models import exercise_fee_mask
    from services.futures_live_repository import list_futures_cash_flows

    flows = list_futures_cash_flows(include_taken_over=False)
    if flows is None or flows.empty:
        return pd.DataFrame(columns=["date", "amount"])
    # 旧版导入的行权手续费可能记为“账户费用”，盯市口径已扣，不能重复扣。
    fees = flows[flows["entry_type"].isin(FUTURES_BROKER_ONLY_FEE_TYPES) & ~exercise_fee_mask(flows)]
    return pd.DataFrame({"date": fees["flow_date"], "amount": pd.to_numeric(fees["amount"], errors="coerce").abs()})


def load_account_overview(*, market_now: datetime) -> dict[str, object]:
    """只读本地账本和正式收盘/结算缓存，不发起网络请求。"""
    accounts: list[AccountSeries] = []
    errors: dict[str, str] = {}

    try:
        from services.fund_analysis import FUND_ADJUST_NONE
        from services.live_trading import build_live_account_snapshot, list_live_cash_flows, list_live_trades
        from services.position_analysis import latest_final_etf_trade_date, load_or_fetch_etf

        trades = list_live_trades()
        histories = {}
        for symbol in sorted(trades["symbol"].dropna().astype(str).unique()) if not trades.empty else []:
            item = load_or_fetch_etf(symbol, adjust=FUND_ADJUST_NONE, allow_fetch=False, save_to_cache=False)
            if item.dataframe is not None and not item.dataframe.empty:
                histories[symbol] = item.dataframe
        snapshot = build_live_account_snapshot(
            trades,
            list_live_cash_flows(),
            histories,
            market_now=market_now,
            formal_target_date=latest_final_etf_trade_date(market_now),
        )
        account_daily = snapshot.get("formal_account_daily")
        accounts.append(etf_account_series(account_daily if isinstance(account_daily, pd.DataFrame) else pd.DataFrame()))
    except Exception as exc:  # 单个账本失败不影响其他账本
        errors["etf"] = str(exc)

    try:
        from services.microcap_live_market import load_microcap_histories
        from services.microcap_live_trading import (
            build_microcap_daily_returns,
            list_microcap_cash_flows,
            list_microcap_position_adjustments,
            list_microcap_trades,
            microcap_ledger_codes,
        )

        trades = list_microcap_trades()
        flows = list_microcap_cash_flows()
        adjustments = list_microcap_position_adjustments()
        histories, _ = load_microcap_histories(
            microcap_ledger_codes(trades, adjustments), allow_fetch=False, market_now=market_now
        )
        accounts.append(microcap_account_series(build_microcap_daily_returns(trades, flows, adjustments, histories)))
    except Exception as exc:
        errors["microcap"] = str(exc)

    try:
        from services.futures_live_trading import build_daily_account_pnl

        accounts.append(
            futures_account_series(build_daily_account_pnl(valuation_mode="settlement"), _futures_broker_only_fees())
        )
    except Exception as exc:
        errors["futures"] = str(exc)

    accounts = [account for account in accounts if not account.daily.empty]
    try:
        pre_ledger = load_pre_ledger_pnl()
    except Exception as exc:
        pre_ledger = {}
        errors["pre_ledger"] = str(exc)
    for account in accounts:
        record = pre_ledger.get(account.key)
        if record:
            account.pre_ledger_pnl = float(record["amount"])
            account.pre_ledger_through = pd.Timestamp(record["through_date"])
    start = combined_start_date(accounts)
    return {
        "accounts": accounts,
        "combined": combine_account_series(accounts, start=start),
        "start": start,
        "inception_pnl": sum(account.inception_pnl for account in accounts),
        "errors": errors,
    }


__all__ = [
    "ACCOUNT_LABELS",
    "AccountSeries",
    "combine_account_series",
    "combined_start_date",
    "etf_account_series",
    "futures_account_series",
    "load_account_overview",
    "load_pre_ledger_pnl",
    "microcap_account_series",
    "save_pre_ledger_pnl",
]
