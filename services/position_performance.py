from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as datetime_time
from zoneinfo import ZoneInfo

import pandas as pd

from services.fund_rotation import (
    PORTFOLIO_EMPTY_ACTIVATION_AFTER_PRIMARY_ENTRY,
    PORTFOLIO_INITIAL_ENTRY_FRESH_BUY,
    PORTFOLIO_STRATEGY_HALF_TIMING,
    PORTFOLIO_STRATEGY_TIMING,
    PortfolioTimingAllocation,
    RotationInput,
    run_portfolio_timing_backtest,
)
from services.market_calendar import get_market_window, is_market_trading_day
from services.position_models import (
    ETF_512890_ACTIVE_TRANSFER_SOURCE_CODES,
    ETF_DISPLAY_NAMES,
    ETF_PORTFOLIO_WEIGHTS_PCT,
    ETF_POSITION_STRATEGIES,
    ETF_TIMING_STRATEGIES,
    PositionItem,
    normalize_etf_base_code,
)
from services.position_sessions import filter_final_etf_rows, latest_final_etf_trade_date


POSITION_TIMING_START_DATE = pd.Timestamp("2026-08-05")
POSITION_TIMING_INITIAL_CAPITAL = 500_000.0
POSITION_TIMING_TRANSACTION_COST = 0.00006
POSITION_TIMING_LOT_SIZE = 100
POSITION_TIMING_PARKING_SYMBOL = "512890"


@dataclass
class PositionTimingPerformanceResult:
    daily: pd.DataFrame = field(default_factory=pd.DataFrame)
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    positions: pd.DataFrame = field(default_factory=pd.DataFrame)
    components: pd.DataFrame = field(default_factory=pd.DataFrame)
    summary: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class PositionTimingTradePreviewResult:
    actions: pd.DataFrame = field(default_factory=pd.DataFrame)
    formal_date: str = ""
    preview_date: str = ""
    quote_time: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


POSITION_TIMING_TRADE_ACTION_COLUMNS = [
    "操作",
    "代码",
    "基金名称",
    "数量",
    "参考价",
    "预计金额",
    "原因",
]


def _empty_result(*, error: str) -> PositionTimingPerformanceResult:
    return PositionTimingPerformanceResult(errors=[error])


def _formal_price_history(
    item: PositionItem,
    *,
    market_now: datetime,
) -> pd.DataFrame:
    if item.dataframe is None or item.dataframe.empty:
        return pd.DataFrame(columns=["trade_date", "close"])
    data = item.dataframe.copy()
    if not {"date", "price"}.issubset(data.columns):
        return pd.DataFrame(columns=["trade_date", "close"])
    data = filter_final_etf_rows(
        data,
        date_column="date",
        market_now=market_now,
        # load_or_fetch_etf has already consumed the confirmation marker before
        # returning a formally validated PositionItem. Its analysis dataframe no
        # longer carries underscore-prefixed metadata.
        require_current_confirmation=False,
    )
    if data is None or data.empty:
        return pd.DataFrame(columns=["trade_date", "close"])
    data = data[["date", "price"]].rename(
        columns={"date": "trade_date", "price": "close"}
    )
    data["trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce").dt.normalize()
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    return (
        data.dropna(subset=["trade_date", "close"])
        .loc[lambda frame: frame["close"] > 0]
        .sort_values("trade_date")
        .drop_duplicates("trade_date", keep="last")
        .reset_index(drop=True)
    )


def _expected_a_share_sessions(
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> list[pd.Timestamp]:
    market = get_market_window("A股")
    if market is None:
        return [
            pd.Timestamp(day).normalize()
            for day in pd.date_range(start_date, end_date, freq="B")
        ]
    return [
        pd.Timestamp(day).normalize()
        for day in pd.date_range(start_date, end_date, freq="D")
        if is_market_trading_day(
            market,
            datetime.combine(pd.Timestamp(day).date(), datetime_time(12, 0)),
        )
    ]


def _build_allocations() -> list[PortfolioTimingAllocation]:
    allocations: list[PortfolioTimingAllocation] = []
    active_parking_sources = set(ETF_512890_ACTIVE_TRANSFER_SOURCE_CODES)
    for code, weight_pct in ETF_PORTFOLIO_WEIGHTS_PCT.items():
        ma_period, threshold_pct = ETF_TIMING_STRATEGIES[code]
        is_half_timing = ETF_POSITION_STRATEGIES.get(code) == "半仓持有半仓择时"
        allocations.append(
            PortfolioTimingAllocation(
                symbol=code,
                name=ETF_DISPLAY_NAMES.get(code, code),
                weight_pct=float(weight_pct),
                strategy=(
                    PORTFOLIO_STRATEGY_HALF_TIMING
                    if is_half_timing
                    else PORTFOLIO_STRATEGY_TIMING
                ),
                ma_period=int(ma_period),
                threshold_pct=float(threshold_pct),
                initial_entry_policy=PORTFOLIO_INITIAL_ENTRY_FRESH_BUY,
                empty_position_symbol=(
                    POSITION_TIMING_PARKING_SYMBOL
                    if code in active_parking_sources
                    else ""
                ),
                empty_position_activation=PORTFOLIO_EMPTY_ACTIVATION_AFTER_PRIMARY_ENTRY,
            )
        )
    return allocations


def _run_fixed_position_timing_backtest(
    histories: dict[str, pd.DataFrame],
    *,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    initial_capital: float,
    transaction_cost: float,
    lot_size: int,
):
    required_codes = list(ETF_PORTFOLIO_WEIGHTS_PCT) + [POSITION_TIMING_PARKING_SYMBOL]
    funds = [
        RotationInput(
            symbol=code,
            name=ETF_DISPLAY_NAMES.get(code, code),
            dataframe=histories[code],
            trade_lot_size=lot_size,
            apply_slippage=False,
        )
        for code in required_codes
    ]
    return run_portfolio_timing_backtest(
        funds=funds,
        allocations=_build_allocations(),
        initial_capital=float(initial_capital),
        transaction_cost=float(transaction_cost),
        lot_size=int(lot_size),
        start_date=start_date,
        end_date=end_date,
    )


def _strategy_position_quantities(trades: pd.DataFrame | None) -> dict[str, int]:
    quantities: dict[str, float] = {}
    if trades is None or trades.empty:
        return {}
    for row in trades.itertuples(index=False):
        symbol = normalize_etf_base_code(getattr(row, "交易标的", ""))
        quantity = pd.to_numeric(getattr(row, "份额", 0), errors="coerce")
        operation = str(getattr(row, "操作", ""))
        if not symbol or pd.isna(quantity) or operation not in {"买入", "卖出"}:
            continue
        quantities[symbol] = quantities.get(symbol, 0.0) + (
            float(quantity) if operation == "买入" else -float(quantity)
        )
    return {
        symbol: int(round(quantity))
        for symbol, quantity in quantities.items()
        if abs(quantity) >= 0.5
    }


def _preview_action_reasons(
    trades: pd.DataFrame,
    *,
    symbol: str,
    preview_date: pd.Timestamp,
) -> str:
    if trades is None or trades.empty:
        return "盘中目标持仓发生变化"
    dates = pd.to_datetime(trades.get("日期"), errors="coerce").dt.normalize()
    symbols = trades.get("交易标的", pd.Series(index=trades.index, dtype="object"))
    symbols = symbols.astype(str).map(normalize_etf_base_code)
    matching = trades.loc[dates.eq(preview_date) & symbols.eq(symbol)]
    reasons: list[str] = []
    for row in matching.itertuples(index=False):
        sleeve = normalize_etf_base_code(getattr(row, "代码", ""))
        reason = str(getattr(row, "原因", "") or "").strip()
        text = f"{sleeve}袖套：{reason}" if sleeve and sleeve != symbol else reason
        if text and text not in reasons:
            reasons.append(text)
    return "；".join(reasons) or "盘中目标持仓发生变化"


def build_position_timing_trade_preview(
    items: list[PositionItem],
    quotes: dict[str, dict[str, object]],
    *,
    market_now: datetime | None = None,
    start_date: str | pd.Timestamp = POSITION_TIMING_START_DATE,
    initial_capital: float = POSITION_TIMING_INITIAL_CAPITAL,
    transaction_cost: float = POSITION_TIMING_TRANSACTION_COST,
    lot_size: int = POSITION_TIMING_LOT_SIZE,
) -> PositionTimingTradePreviewResult:
    """Compare the last formal strategy holdings with a transient same-day quote preview."""
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    market = get_market_window("A股")
    if market is None or not is_market_trading_day(market, market_now):
        return PositionTimingTradePreviewResult(errors=["当前不是A股交易日。"])

    start = pd.Timestamp(start_date).normalize()
    preview_date = pd.Timestamp(market_now.date())
    formal_date = pd.Timestamp(latest_final_etf_trade_date(market_now)).normalize()
    if formal_date >= preview_date:
        return PositionTimingTradePreviewResult(errors=["当前交易日已进入正式收盘阶段，不再生成盘中交易预判。"])
    if formal_date < start:
        return PositionTimingTradePreviewResult(
            errors=[f"正式数据尚未到达策略开始日 {start:%Y-%m-%d}。"]
        )

    required_codes = list(ETF_PORTFOLIO_WEIGHTS_PCT) + [POSITION_TIMING_PARKING_SYMBOL]
    item_by_code = {
        normalize_etf_base_code(item.code): item
        for item in items
        if item.category == "ETF"
    }
    missing_items = [code for code in required_codes if code not in item_by_code]
    if missing_items:
        return PositionTimingTradePreviewResult(
            errors=[f"缺少正式ETF缓存：{'、'.join(missing_items)}。"]
        )
    invalid_items = [
        code for code in required_codes if not item_by_code[code].formal_history_valid
    ]
    if invalid_items:
        return PositionTimingTradePreviewResult(
            errors=[f"以下ETF前复权缓存校验未通过：{'、'.join(invalid_items)}。"]
        )

    histories = {
        code: _formal_price_history(item_by_code[code], market_now=market_now)
        for code in required_codes
    }
    empty_histories = [code for code, history in histories.items() if history.empty]
    if empty_histories:
        return PositionTimingTradePreviewResult(
            errors=[f"以下ETF没有可用正式日线：{'、'.join(empty_histories)}。"]
        )

    expected_sessions = _expected_a_share_sessions(start, formal_date)
    available_dates = {
        code: set(pd.to_datetime(history["trade_date"], errors="coerce").dropna())
        for code, history in histories.items()
    }
    for session in expected_sessions:
        missing_on_date = [
            code for code in required_codes if session not in available_dates[code]
        ]
        if missing_on_date:
            return PositionTimingTradePreviewResult(
                errors=[
                    f"{session:%Y-%m-%d} 正式日线不完整（{'、'.join(missing_on_date)}），"
                    "不能生成可执行交易通知。"
                ]
            )

    normalized_quotes = {
        normalize_etf_base_code(code): dict(quote)
        for code, quote in (quotes or {}).items()
    }
    missing_quotes: list[str] = []
    quote_times: list[pd.Timestamp] = []
    preview_histories: dict[str, pd.DataFrame] = {}
    for code in required_codes:
        quote = normalized_quotes.get(code, {})
        quote_time = pd.to_datetime(quote.get("quote_time"), errors="coerce")
        quote_price = pd.to_numeric(quote.get("price"), errors="coerce")
        if (
            pd.isna(quote_time)
            or quote_time.date() != market_now.date()
            or pd.isna(quote_price)
            or float(quote_price) <= 0
        ):
            missing_quotes.append(code)
            continue
        quote_times.append(pd.Timestamp(quote_time))
        history = histories[code].copy()
        history = history.loc[
            pd.to_datetime(history["trade_date"], errors="coerce").dt.normalize()
            != preview_date
        ]
        preview_histories[code] = pd.concat(
            [
                history,
                pd.DataFrame(
                    {"trade_date": [preview_date], "close": [float(quote_price)]}
                ),
            ],
            ignore_index=True,
        )
    if missing_quotes:
        return PositionTimingTradePreviewResult(
            formal_date=formal_date.strftime("%Y-%m-%d"),
            preview_date=preview_date.strftime("%Y-%m-%d"),
            errors=[f"以下ETF缺少当日有效实时行情：{'、'.join(missing_quotes)}。"],
        )

    try:
        formal_backtest = _run_fixed_position_timing_backtest(
            histories,
            start_date=start,
            end_date=formal_date,
            initial_capital=initial_capital,
            transaction_cost=transaction_cost,
            lot_size=lot_size,
        )
        preview_backtest = _run_fixed_position_timing_backtest(
            preview_histories,
            start_date=start,
            end_date=preview_date,
            initial_capital=initial_capital,
            transaction_cost=transaction_cost,
            lot_size=lot_size,
        )
    except Exception as exc:
        return PositionTimingTradePreviewResult(
            formal_date=formal_date.strftime("%Y-%m-%d"),
            preview_date=preview_date.strftime("%Y-%m-%d"),
            errors=[f"组合策略盘中预判失败：{exc}"],
        )

    formal_quantities = _strategy_position_quantities(formal_backtest.trades)
    preview_quantities = _strategy_position_quantities(preview_backtest.trades)
    preview_trades = _normalize_trade_display_names(preview_backtest.trades)
    action_rows: list[dict[str, object]] = []
    for code in sorted(set(formal_quantities) | set(preview_quantities)):
        delta = preview_quantities.get(code, 0) - formal_quantities.get(code, 0)
        if delta == 0:
            continue
        price = float(normalized_quotes[code]["price"])
        action_rows.append(
            {
                "操作": "买入" if delta > 0 else "卖出",
                "代码": code,
                "基金名称": ETF_DISPLAY_NAMES.get(code, code),
                "数量": abs(int(delta)),
                "参考价": round(price, 4),
                "预计金额": round(abs(int(delta)) * price, 2),
                "原因": _preview_action_reasons(
                    preview_trades,
                    symbol=code,
                    preview_date=preview_date,
                ),
            }
        )
    actions = pd.DataFrame(action_rows, columns=POSITION_TIMING_TRADE_ACTION_COLUMNS)
    if not actions.empty:
        actions["_order"] = actions["操作"].map({"卖出": 0, "买入": 1})
        actions = actions.sort_values(["_order", "代码"]).drop(columns="_order").reset_index(drop=True)

    latest_quote_time = max(quote_times) if quote_times else pd.NaT
    return PositionTimingTradePreviewResult(
        actions=actions,
        formal_date=formal_date.strftime("%Y-%m-%d"),
        preview_date=preview_date.strftime("%Y-%m-%d"),
        quote_time=(
            latest_quote_time.strftime("%Y-%m-%d %H:%M:%S")
            if pd.notna(latest_quote_time)
            else ""
        ),
    )


POSITION_TIMING_POSITION_COLUMNS = [
    "基金名称",
    "代码",
    "持仓数量",
    "成本价",
    "最新价",
    "持仓市值",
    "浮动盈亏",
    "账户权重(%)",
    "来源袖套",
    "累计手续费",
]


def _normalize_trade_display_names(trades: pd.DataFrame) -> pd.DataFrame:
    """Apply the fixed full-name mapping to every simulated traded symbol."""
    if trades is None or trades.empty:
        return pd.DataFrame() if trades is None else trades.copy()
    result = trades.copy()
    if "交易标的" not in result.columns:
        return result
    symbols = result["交易标的"].astype(str).map(normalize_etf_base_code)
    result["交易标的"] = symbols
    if "交易标的名称" not in result.columns:
        result["交易标的名称"] = symbols
    mapped_names = symbols.map(ETF_DISPLAY_NAMES)
    existing_names = result["交易标的名称"].astype("string")
    result["交易标的名称"] = mapped_names.fillna(existing_names).fillna(symbols)
    return result


def _build_current_positions(
    trades: pd.DataFrame,
    histories: dict[str, pd.DataFrame],
    *,
    valuation_date: pd.Timestamp,
    account_assets: float,
) -> pd.DataFrame:
    """Rebuild current simulated holdings from each independent strategy sleeve."""
    if trades is None or trades.empty:
        return pd.DataFrame(columns=POSITION_TIMING_POSITION_COLUMNS)

    ledger = trades.copy().reset_index(drop=True)
    ledger["_date"] = pd.to_datetime(ledger.get("日期"), errors="coerce").dt.normalize()
    ledger["_order"] = ledger.index
    ledger = ledger.sort_values(["_date", "_order"], kind="stable")
    states: dict[tuple[str, str], dict[str, float]] = {}
    names: dict[str, str] = {}
    fees: dict[str, float] = {}
    for row in ledger.itertuples(index=False):
        sleeve = str(getattr(row, "代码", "") or "")
        symbol = normalize_etf_base_code(getattr(row, "交易标的", ""))
        if not symbol:
            continue
        quantity_value = pd.to_numeric(getattr(row, "份额", 0), errors="coerce")
        gross_value = pd.to_numeric(getattr(row, "成交金额", 0), errors="coerce")
        fee_value = pd.to_numeric(getattr(row, "手续费", 0), errors="coerce")
        quantity = 0.0 if pd.isna(quantity_value) else float(quantity_value)
        gross_amount = 0.0 if pd.isna(gross_value) else float(gross_value)
        fee = 0.0 if pd.isna(fee_value) else float(fee_value)
        operation = str(getattr(row, "操作", ""))
        names[symbol] = ETF_DISPLAY_NAMES.get(symbol) or str(
            getattr(row, "交易标的名称", "") or symbol
        )
        fees[symbol] = fees.get(symbol, 0.0) + fee
        key = (sleeve, symbol)
        state = states.setdefault(key, {"quantity": 0.0, "cost_basis": 0.0})
        if operation == "买入":
            state["quantity"] += quantity
            state["cost_basis"] += gross_amount + fee
        elif operation == "卖出" and state["quantity"] > 0:
            sold_quantity = min(quantity, state["quantity"])
            average_cost = state["cost_basis"] / state["quantity"]
            state["quantity"] -= sold_quantity
            state["cost_basis"] -= average_cost * sold_quantity
            if state["quantity"] < 1e-8:
                state["quantity"] = 0.0
                state["cost_basis"] = 0.0

    aggregated: dict[str, dict[str, object]] = {}
    for (sleeve, symbol), state in states.items():
        if state["quantity"] <= 0:
            continue
        item = aggregated.setdefault(
            symbol,
            {"quantity": 0.0, "cost_basis": 0.0, "sleeves": set()},
        )
        item["quantity"] = float(item["quantity"]) + state["quantity"]
        item["cost_basis"] = float(item["cost_basis"]) + state["cost_basis"]
        item["sleeves"].add(sleeve)

    rows: list[dict[str, object]] = []
    for symbol, state in aggregated.items():
        history = histories.get(symbol, pd.DataFrame())
        matching = history[
            pd.to_datetime(history.get("trade_date"), errors="coerce").dt.normalize()
            == valuation_date
        ]
        latest_price = (
            float(pd.to_numeric(matching.iloc[-1]["close"], errors="coerce"))
            if not matching.empty
            else float("nan")
        )
        quantity = float(state["quantity"])
        cost_basis = float(state["cost_basis"])
        market_value = quantity * latest_price
        rows.append(
            {
                "基金名称": names.get(symbol, ETF_DISPLAY_NAMES.get(symbol, symbol)),
                "代码": symbol,
                "持仓数量": int(round(quantity)),
                "成本价": cost_basis / quantity,
                "最新价": latest_price,
                "持仓市值": market_value,
                "浮动盈亏": market_value - cost_basis,
                "账户权重(%)": (
                    market_value / account_assets * 100 if account_assets > 0 else pd.NA
                ),
                "来源袖套": "、".join(sorted(state["sleeves"])),
                "累计手续费": fees.get(symbol, 0.0),
            }
        )
    if not rows:
        return pd.DataFrame(columns=POSITION_TIMING_POSITION_COLUMNS)
    result = pd.DataFrame(rows, columns=POSITION_TIMING_POSITION_COLUMNS)
    money_columns = ["成本价", "最新价", "持仓市值", "浮动盈亏", "累计手续费"]
    result[money_columns] = result[money_columns].round(4)
    result["账户权重(%)"] = pd.to_numeric(
        result["账户权重(%)"], errors="coerce"
    ).round(2)
    return result.sort_values("持仓市值", ascending=False).reset_index(drop=True)


def build_position_timing_performance(
    items: list[PositionItem],
    *,
    start_date: str | pd.Timestamp = POSITION_TIMING_START_DATE,
    initial_capital: float = POSITION_TIMING_INITIAL_CAPITAL,
    transaction_cost: float = POSITION_TIMING_TRANSACTION_COST,
    lot_size: int = POSITION_TIMING_LOT_SIZE,
    market_now: datetime | None = None,
) -> PositionTimingPerformanceResult:
    """Build the fixed holdings strategy from validated formal ETF closes only."""
    market_now = market_now or datetime.now(ZoneInfo("Asia/Shanghai"))
    start = pd.Timestamp(start_date).normalize()
    required_codes = list(ETF_PORTFOLIO_WEIGHTS_PCT) + [POSITION_TIMING_PARKING_SYMBOL]
    item_by_code = {
        normalize_etf_base_code(item.code): item
        for item in items
        if item.category == "ETF"
    }

    missing_items = [code for code in required_codes if code not in item_by_code]
    if missing_items:
        return _empty_result(error=f"缺少正式ETF缓存：{'、'.join(missing_items)}。")
    invalid_items = [
        code for code in required_codes if not item_by_code[code].formal_history_valid
    ]
    if invalid_items:
        return _empty_result(
            error=f"以下ETF前复权缓存校验未通过，策略已停止：{'、'.join(invalid_items)}。"
        )

    histories: dict[str, pd.DataFrame] = {
        code: _formal_price_history(item_by_code[code], market_now=market_now)
        for code in required_codes
    }
    empty_histories = [code for code, history in histories.items() if history.empty]
    if empty_histories:
        return _empty_result(error=f"以下ETF没有可用正式日线：{'、'.join(empty_histories)}。")

    target_date = pd.Timestamp(latest_final_etf_trade_date(market_now)).normalize()
    if target_date < start:
        return _empty_result(error=f"正式数据尚未到达策略开始日 {start:%Y-%m-%d}。")
    expected_sessions = _expected_a_share_sessions(start, target_date)
    if not expected_sessions:
        return _empty_result(error="策略区间内没有A股交易日。")

    available_dates = {
        code: set(history["trade_date"].tolist()) for code, history in histories.items()
    }
    first_gap_date: pd.Timestamp | None = None
    first_gap_codes: list[str] = []
    for session in expected_sessions:
        missing_on_date = [
            code for code in required_codes if session not in available_dates[code]
        ]
        if missing_on_date:
            first_gap_date = session
            first_gap_codes = missing_on_date
            break

    warnings: list[str] = []
    usable_sessions = expected_sessions
    if first_gap_date is not None:
        usable_sessions = [session for session in expected_sessions if session < first_gap_date]
        warnings.append(
            f"{first_gap_date:%Y-%m-%d} 正式日线不完整（{'、'.join(first_gap_codes)}），"
            "结果已停止在前一完整交易日。"
        )
    if not usable_sessions or usable_sessions[0] != start:
        return PositionTimingPerformanceResult(
            warnings=warnings,
            errors=[f"开始日 {start:%Y-%m-%d} 的正式日线不完整，无法建立初始组合。"],
        )
    if len(usable_sessions) < 2:
        return PositionTimingPerformanceResult(
            warnings=warnings,
            errors=["完整交易日不足2天，暂无法生成每日盈亏曲线。"],
        )
    end = usable_sessions[-1]

    funds = [
        RotationInput(
            symbol=code,
            name=ETF_DISPLAY_NAMES.get(code, code),
            dataframe=histories[code],
            trade_lot_size=lot_size,
            apply_slippage=False,
        )
        for code in required_codes
    ]
    try:
        backtest = run_portfolio_timing_backtest(
            funds=funds,
            allocations=_build_allocations(),
            initial_capital=float(initial_capital),
            transaction_cost=float(transaction_cost),
            lot_size=int(lot_size),
            start_date=start,
            end_date=end,
        )
    except Exception as exc:
        return PositionTimingPerformanceResult(
            warnings=warnings,
            errors=[f"组合策略计算失败：{exc}"],
        )

    trades = _normalize_trade_display_names(backtest.trades)
    raw_start_assets = float(backtest.nav_data.iloc[0]["账户净值"])
    setup_fee = max(0.0, round(float(initial_capital) - raw_start_assets, 2))
    total_fee = float(backtest.summary.get("累计总成本", 0) or 0)
    later_fee = max(0.0, total_fee - setup_fee)

    daily = backtest.nav_data[
        ["日期", "账户净值", "策略持仓市值", "策略现金"]
    ].copy()
    daily["账户资产"] = pd.to_numeric(daily["账户净值"], errors="coerce") + setup_fee
    daily["持仓市值"] = pd.to_numeric(daily["策略持仓市值"], errors="coerce")
    daily["现金"] = pd.to_numeric(daily["策略现金"], errors="coerce") + setup_fee
    daily["每日盈亏"] = daily["账户资产"].diff().fillna(0.0)
    prior_assets = daily["账户资产"].shift(1)
    daily["每日收益率(%)"] = (daily["每日盈亏"] / prior_assets * 100).fillna(0.0)
    daily["累计盈亏"] = daily["账户资产"] - float(initial_capital)
    daily["累计收益率(%)"] = daily["累计盈亏"] / float(initial_capital) * 100
    daily["净值"] = daily["账户资产"] / float(initial_capital)
    daily.loc[daily.index[0], ["每日盈亏", "每日收益率(%)", "累计盈亏", "累计收益率(%)"]] = 0.0
    daily.loc[daily.index[0], "账户资产"] = float(initial_capital)
    daily.loc[daily.index[0], "净值"] = 1.0
    daily = daily[
        [
            "日期",
            "每日盈亏",
            "每日收益率(%)",
            "累计盈亏",
            "累计收益率(%)",
            "净值",
            "账户资产",
            "持仓市值",
            "现金",
        ]
    ]
    money_columns = ["每日盈亏", "累计盈亏", "账户资产", "持仓市值", "现金"]
    daily[money_columns] = daily[money_columns].round(2)
    daily["每日收益率(%)"] = daily["每日收益率(%)"].round(8)
    daily["累计收益率(%)"] = daily["累计收益率(%)"].round(6)
    daily["净值"] = daily["净值"].round(8)

    first_day_trades = trades[
        pd.to_datetime(trades.get("日期"), errors="coerce").dt.normalize() == start
    ] if not trades.empty else pd.DataFrame()
    initial_symbols = sorted(
        set(
            first_day_trades.loc[
                first_day_trades.get("操作").eq("买入"), "交易标的"
            ].astype(str)
        )
    ) if not first_day_trades.empty else []
    latest = daily.iloc[-1]
    positions = _build_current_positions(
        trades,
        histories,
        valuation_date=pd.Timestamp(latest["日期"]).normalize(),
        account_assets=float(latest["账户资产"]),
    )
    summary = {
        "开始日期": start.strftime("%Y-%m-%d"),
        "正式数据截止日": pd.Timestamp(latest["日期"]).strftime("%Y-%m-%d"),
        "最新估值日期": pd.Timestamp(latest["日期"]).strftime("%Y-%m-%d"),
        "策略资产": float(latest["账户资产"]),
        "累计盈亏": float(latest["累计盈亏"]),
        "累计收益率(%)": float(latest["累计收益率(%)"]),
        "当前净值": float(latest["净值"]),
        "当前持仓市值": float(latest["持仓市值"]),
        "当前现金": float(latest["现金"]),
        "当前仓位比例(%)": (
            float(latest["持仓市值"]) / float(latest["账户资产"]) * 100
            if float(latest["账户资产"]) > 0
            else 0.0
        ),
        "初始手续费": round(setup_fee, 2),
        "后续交易费用": round(later_fee, 2),
        "累计交易费用": round(total_fee, 2),
        "初始建仓代码": initial_symbols,
    }
    return PositionTimingPerformanceResult(
        daily=daily,
        trades=trades,
        positions=positions,
        components=backtest.component_results.copy(),
        summary=summary,
        warnings=warnings,
    )


__all__ = [
    "POSITION_TIMING_START_DATE",
    "POSITION_TIMING_INITIAL_CAPITAL",
    "POSITION_TIMING_TRANSACTION_COST",
    "POSITION_TIMING_LOT_SIZE",
    "POSITION_TIMING_PARKING_SYMBOL",
    "POSITION_TIMING_TRADE_ACTION_COLUMNS",
    "PositionTimingPerformanceResult",
    "PositionTimingTradePreviewResult",
    "build_position_timing_performance",
    "build_position_timing_trade_preview",
]
