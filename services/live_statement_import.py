"""从券商交割单与资金明细重建 ETF 实盘账本（微盘股除外）。

支持华宝（交割单 + 资金明细，两者同时提供）和银河（交割单自带资金流水）。
导入结果写入 live_trades / live_cash_flows，record_key 以 ``stmt:<券商>:`` 开头；
同一券商重新导入时，只替换新文件日期范围内的交割单记录。手工记录保留，
与交割单完全对应的手工记录被接管（沿用其策略说明和备注）。

记账口径：
- 逆回购、金自来本金视为现金，到期日按“购回发生金额 + 借出发生金额”记利息；
  截止日仍未购回的只扣借出手续费。
- LOF 场内申购按交割单申购行记买入；上海 LOF 的“调帐转入”只是份额到账，不重复计。
- 资金明细“委托清算预扣款”是另一渠道的申购，份额随后以“转托转入”进入场内，
  按预扣金额把转入份额分摊为逐笔买入。
- 可转债数量统一为张；中签缴款记为按面值买入上市后的转债代码。
- 两家券商之间转托管、撤指定、指定入账只是持仓换地方，不记账。
- A 股个股属于微盘实盘，不进入 ETF 账本。
"""

from __future__ import annotations

import io
import re
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from core import db
from services.live_price_history import is_convertible_bond

REPO_CODES_PREFIX = ("204", "131")
QUOTED_REPO_CODES = {"132001"}
BROKER_LABELS = {"hb": "华宝证券", "yh": "银河证券"}
STATEMENT_KEY_PREFIX = "stmt:"
PRICE_MATCH_TOLERANCE = 0.002


@dataclass
class BrokerImport:
    broker: str
    start: str
    end: str
    trades: list[dict] = field(default_factory=list)
    flows: list[dict] = field(default_factory=list)
    skipped_stock_rows: int = 0
    skipped_stock_amount: float = 0.0
    ignored_operations: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    broker_end_cash: float | None = None
    open_repo_principal: float = 0.0

    @property
    def label(self) -> str:
        return BROKER_LABELS[self.broker]

    def ledger_cash_change(self) -> float:
        cash = sum(_flow_cash(flow) for flow in self.flows)
        for trade in self.trades:
            gross = trade["price"] * trade["quantity"]
            cash += -(gross + trade["fee"]) if trade["side"] == "买入" else gross - trade["fee"]
        return cash

    def cash_difference(self) -> float | None:
        """账本现金 − (券商期末现金 + 未到期逆回购本金 − 微盘个股资金)。"""
        if self.broker_end_cash is None:
            return None
        expected = self.broker_end_cash + self.open_repo_principal - self.skipped_stock_amount
        return self.ledger_cash_change() - expected


def _flow_cash(flow: dict) -> float:
    return -flow["amount"] if flow["entry_type"] in {"资金转出", "其他支出"} else flow["amount"]


def read_statement_table(raw: bytes) -> pd.DataFrame:
    """券商导出的 .xls 实为 GB18030 制表符文本；也兼容 UTF-8。"""
    text = None
    for encoding in ("gb18030", "utf-8-sig"):
        try:
            text = bytes(raw).decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("无法识别交割单编码。")
    frame = pd.read_csv(io.StringIO(text), sep="\t", dtype=str)
    frame.columns = [str(column).strip() for column in frame.columns]
    frame = frame.loc[:, [column for column in frame.columns if column and not column.startswith("Unnamed")]]
    return frame.apply(lambda column: column.str.strip())


def detect_statement_kind(frame: pd.DataFrame) -> str:
    columns = set(frame.columns)
    if {"交收日期", "资金余额", "合同编号", "证券代码"} <= columns:
        return "yh_trades"
    if {"成交日期", "成交时间", "成交编号", "市场名称"} <= columns:
        return "hb_trades"
    if {"成交日期", "摘要", "资金余额", "发生金额"} <= columns:
        return "hb_cash"
    raise ValueError("无法识别文件类型：需要华宝交割单、华宝资金明细或银河交割单。")


def _num(value) -> float:
    try:
        return float(str(value).replace(",", "")) if value is not None and str(value) not in {"", "nan"} else 0.0
    except ValueError:
        return 0.0


def _date(value) -> str:
    text = str(value or "").strip()
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if re.fullmatch(r"\d{8}", text) else text


def _time(value) -> str | None:
    text = str(value or "").strip()
    return None if not text or text in {"nan", "00:00:00"} else text


def is_stock_code(code: str) -> bool:
    """A 股个股（含北交所），归微盘实盘。"""
    return len(code) == 6 and (code[:2] in {"00", "30", "60", "68", "92"} or code[0] in {"4", "8"})


def is_repo_code(code: str) -> bool:
    return code.startswith(REPO_CODES_PREFIX) or code in QUOTED_REPO_CODES


def _trade(broker, key, day, time, code, name, side, quantity, gross, fee, notes="") -> dict:
    quantity = int(round(quantity))
    if quantity <= 0:
        raise ValueError(f"{code} {day} 成交数量无效")
    return {
        "broker": broker,
        "record_key": key,
        "trade_date": day,
        "trade_time": time,
        "symbol": code,
        "name": name or code,
        "side": side,
        "price": gross / quantity,
        "quantity": quantity,
        "fee": max(0.0, round(fee, 2)),
        "strategy": "",
        "notes": notes,
    }


def _flow(broker, key, day, time, entry_type, amount, symbol="", notes="") -> dict:
    return {
        "broker": broker,
        "record_key": key,
        "flow_date": day,
        "flow_time": time,
        "entry_type": entry_type,
        "amount": round(abs(float(amount)), 2),
        "symbol": symbol or None,
        "notes": notes,
    }


class _KeyMaker:
    def __init__(self, broker: str):
        self.broker = broker
        self.seen: dict[str, int] = defaultdict(int)

    def __call__(self, *parts) -> str:
        base = STATEMENT_KEY_PREFIX + ":".join([self.broker, *[str(part) for part in parts]])
        self.seen[base] += 1
        return base if self.seen[base] == 1 else f"{base}#{self.seen[base]}"


def _trade_quantity(code: str, quantity: float, gross: float, price: float) -> float:
    """可转债数量统一为张：成交金额 ÷ 每张价格。"""
    if is_convertible_bond(code) and price > 0 and gross > 0:
        return round(gross / price)
    return quantity


def _repo_interest(rows: pd.DataFrame, *, date_column: str, broker: str, keys: _KeyMaker) -> tuple[list[dict], float, list[str]]:
    """按合同编号配对借出/购回；收益记在购回日，未到期借出只扣手续费。"""
    income: dict[str, float] = defaultdict(float)
    warnings: list[str] = []
    open_principal = 0.0
    for contract, group in rows.groupby("合同编号", sort=False):
        amounts = group["发生金额"].map(_num)
        lends = group[amounts < 0]
        returns = group[amounts > 0]
        if len(lends) == 1 and len(returns) == 1:
            income[_date(returns.iloc[0][date_column])] += float(amounts.sum())
        elif len(lends) == 1 and returns.empty:
            lend = lends.iloc[0]
            open_principal += _num(lend["成交金额"])
            income[_date(lend[date_column])] += _num(lend["发生金额"]) + _num(lend["成交金额"])
        else:
            warnings.append(f"逆回购合同 {contract} 无法配对（{len(group)} 行），未计收益。")
    label = "金自来/逆回购" if broker == "yh" else "逆回购"
    flows = []
    for day, amount in sorted(income.items()):
        if abs(amount) < 0.005:
            continue
        entry_type = "利息" if amount > 0 else "其他支出"
        flows.append(_flow(broker, keys("repo", day), day, None, entry_type, amount, notes=f"{label}净收益（已扣手续费）"))
    return flows, open_principal, warnings


def build_huabao_import(trades_frame: pd.DataFrame, cash_frame: pd.DataFrame) -> BrokerImport:
    keys = _KeyMaker("hb")
    rows = trades_frame.copy()
    rows["_order"] = rows["成交日期"] + rows["成交时间"].fillna("")
    rows = rows.iloc[::-1].sort_values("_order", kind="stable")
    cash = cash_frame.iloc[::-1].reset_index(drop=True)
    dates = pd.concat([rows["成交日期"], cash["成交日期"]]).dropna()
    result = BrokerImport("hb", _date(dates.min()), _date(dates.max()))

    # 资金明细：期末现金 = 最早一行的余额回推 + 全部发生金额。
    amounts = cash["发生金额"].map(_num)
    result.broker_end_cash = round(_num(cash.iloc[0]["资金余额"]) - amounts.iloc[0] + amounts.sum(), 2)

    bond_pay_dates: dict[tuple[str, float], str] = {}
    prepaid: dict[str, list[dict]] = defaultdict(list)
    for index, row in cash.iterrows():
        op, summary, day, time = row["操作"], str(row["摘要"]), _date(row["成交日期"]), _time(row["成交时间"])
        amount = _num(row["发生金额"])
        if op in {"银行转证券", "证券转银行"}:
            result.flows.append(
                _flow("hb", keys("bank", day, time, op, f"{abs(amount):.2f}"), day, time,
                      "资金转入" if amount > 0 else "资金转出", amount, notes=f"华宝{op}")
            )
        elif op == "季度结息":
            result.flows.append(_flow("hb", keys("interest", day), day, time, "利息", amount, notes="华宝季度结息"))
        elif op == "股票分红":
            code = (re.findall(r"\d{6}", summary) or [""])[0]
            result.flows.append(_flow("hb", keys("dividend", day, code), day, time, "现金分红", amount, code, "华宝分红到账"))
        elif op in {"债券兑息", "债券兑息扣税"}:
            code = (re.findall(r"\d{6}", summary) or [""])[0]
            result.flows.append(
                _flow("hb", keys("coupon", day, code, op), day, time,
                      "其他收入" if amount > 0 else "其他支出", amount, code, f"可转债{op}")
            )
        elif op == "发行中签扣款":
            code = (re.findall(r"\((\d{6})\)", summary) or re.findall(r"\d{6}", summary) or [""])[0]
            bond_pay_dates[(code, round(abs(amount), 2))] = day
        elif op == "基金申购划出" and "预扣款" in summary:
            code = (re.findall(r"\d{6}", summary) or [""])[0]
            prepaid[code].append({"date": day, "time": time, "amount": abs(amount), "index": index})

    # 交割单：成交、申购、中签缴款、转托转入。
    later_custody_in = rows[rows["操作"].eq("托管转入")]
    for _, row in rows.iterrows():
        op, code, name = row["操作"], str(row["证券代码"]), row["证券名称"]
        day, time = _date(row["成交日期"]), _time(row["成交时间"])
        gross, net = _num(row["成交金额"]), _num(row["发生金额"])
        quantity, price = _num(row["成交数量"]), _num(row["成交均价"])
        if is_repo_code(code) or op in {"回购融券", "逆回购购回"}:
            continue
        if op in {"买入", "卖出", "盘后定价买入", "基金申购"}:
            if is_stock_code(code):
                result.skipped_stock_rows += 1
                result.skipped_stock_amount += net
                continue
            if quantity <= 0 or gross <= 0:
                # 申购未成功（份额与金额为 0）
                result.ignored_operations[f"{op}（0份）"] = result.ignored_operations.get(f"{op}（0份）", 0) + 1
                continue
            side = "卖出" if op == "卖出" else "买入"
            fee = abs(abs(net) - gross)
            qty = _trade_quantity(code, quantity, gross, price)
            result.trades.append(
                _trade("hb", keys("t", day, row["合同编号"], row["成交编号"], code, side), day, time, code, name,
                       side, qty, gross, fee, "LOF场内申购" if op == "基金申购" else "")
            )
        elif op == "缴中签款" and gross > 0:
            listed = code
            after = later_custody_in[
                (later_custody_in["成交日期"] >= row["成交日期"])
                & later_custody_in["证券代码"].map(is_convertible_bond)
            ]
            if not after.empty:
                listed = str(after.iloc[0]["证券代码"])
            elif re.fullmatch(r"\d{6}", str(name)):
                listed = str(name)
            pay_day = bond_pay_dates.get((code, round(gross, 2)), day)
            result.trades.append(
                _trade("hb", keys("bond", pay_day, code), pay_day, None, listed, name, "买入",
                       quantity, gross, 0.0, f"可转债中签缴款（申购代码{code}）")
            )
        elif op == "转托转入":
            remaining = int(round(quantity))
            payments = [item for item in prepaid.get(code, []) if item["date"] <= day and not item.get("used")]
            if payments:
                total = sum(item["amount"] for item in payments)
                shares = [int(remaining * item["amount"] / total) for item in payments]
                for position in range(remaining - sum(shares)):
                    shares[position % len(shares)] += 1
                for item, share in zip(payments, shares):
                    item["used"] = True
                    if share <= 0:
                        continue
                    result.trades.append(
                        _trade("hb", keys("prepaid", item["date"], code, item["index"]), item["date"], item["time"],
                               code, name, "买入", share, item["amount"], 0.0,
                               f"其他渠道申购（预扣款），份额于{day}转托转入")
                    )
            else:
                value = gross if gross > 0 else 0.0
                result.warnings.append(f"{day} {code} 转托转入 {remaining} 份没有找到对应预扣款，按转入市值计为收益。")
                if value > 0:
                    result.trades.append(
                        _trade("hb", keys("custody", day, code), day, None, code, name, "买入", remaining, value, 0.0, "转托转入")
                    )
                    result.flows.append(_flow("hb", keys("custody-income", day, code), day, None, "其他收入", value, code, "转托转入份额"))
        elif op not in {"调帐转入", "托管转入", "托管转出", "撤指", "指定", "配号", "中签通知", "缴中签款", "红利", "债券兑息"}:
            result.ignored_operations[op] = result.ignored_operations.get(op, 0) + 1

    unused = [item for items in prepaid.values() for item in items if not item.get("used")]
    if unused:
        result.warnings.append(f"有 {len(unused)} 笔申购预扣款尚未见到转托转入份额，暂按现金支出处理。")
        for item in unused:
            result.flows.append(_flow("hb", keys("prepaid-pending", item["date"], item["index"]), item["date"], item["time"],
                                      "其他支出", item["amount"], notes="申购预扣款（份额未到账）"))

    repo_rows = rows[rows["证券代码"].map(is_repo_code)]
    repo_flows, open_principal, repo_warnings = _repo_interest(repo_rows, date_column="成交日期", broker="hb", keys=keys)
    result.flows.extend(repo_flows)
    result.open_repo_principal = open_principal
    result.warnings.extend(repo_warnings)
    return result


def build_yinhe_import(frame: pd.DataFrame) -> BrokerImport:
    keys = _KeyMaker("yh")
    rows = frame.reset_index(drop=True)
    result = BrokerImport("yh", _date(rows["交收日期"].min()), _date(rows["交收日期"].max()))
    amounts = rows["发生金额"].map(_num)
    result.broker_end_cash = round(_num(rows.iloc[-1]["资金余额"]), 2)
    for index, row in rows.iterrows():
        op, code, name = row["操作"], str(row["证券代码"]) if str(row["证券代码"]) != "nan" else "", row["证券名称"]
        day = _date(row["交收日期"])
        gross, net, quantity, price = _num(row["成交金额"]), amounts.iloc[index], _num(row["成交数量"]), _num(row["成交均价"])
        if code and is_repo_code(code):
            continue
        if op in {"证券买入", "证券卖出", "开放基金申购"}:
            if is_stock_code(code):
                result.skipped_stock_rows += 1
                result.skipped_stock_amount += net
                continue
            side = "卖出" if op == "证券卖出" else "买入"
            qty = _trade_quantity(code, quantity, gross, price)
            result.trades.append(
                _trade("yh", keys("t", day, row["合同编号"], code, side, int(qty)), day, None, code, name, side,
                       qty, gross, abs(abs(net) - gross))
            )
        elif op in {"银行转证券", "证券转银行"}:
            result.flows.append(
                _flow("yh", keys("bank", day, op, f"{abs(net):.2f}"), day, None,
                      "资金转入" if net > 0 else "资金转出", net, notes=f"银河{op}")
            )
        elif op == "利息归本":
            result.flows.append(_flow("yh", keys("interest", day), day, None, "利息", net, notes="银河利息归本"))
        elif op == "红利入账":
            if is_stock_code(code):
                result.skipped_stock_rows += 1
                result.skipped_stock_amount += net
            else:
                result.flows.append(_flow("yh", keys("dividend", day, code), day, None, "现金分红", net, code, "银河红利入账"))
        elif op not in {"指定交易", "撤销指定", "指定入账"}:
            result.ignored_operations[op] = result.ignored_operations.get(op, 0) + 1
    repo_rows = rows[rows["证券代码"].fillna("").map(is_repo_code)]
    repo_flows, open_principal, repo_warnings = _repo_interest(repo_rows, date_column="交收日期", broker="yh", keys=keys)
    result.flows.extend(repo_flows)
    result.open_repo_principal = open_principal
    result.warnings.extend(repo_warnings)
    return result


def _names_from_ledger(conn) -> dict[str, str]:
    rows = conn.execute("SELECT symbol, name FROM live_trades ORDER BY id").fetchall()
    return {str(symbol): str(name) for symbol, name in rows if name}


def _fee_rate(trade: dict) -> float:
    gross = trade["price"] * trade["quantity"]
    return round(trade["fee"] / gross * 100, 10) if gross > 0 else 0.0


def preview_statement_import(files: list[tuple[str, bytes]]) -> dict[str, object]:
    """解析上传文件并给出导入预览；不写数据库。"""
    frames: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    for file_name, raw in files:
        try:
            frame = read_statement_table(raw)
            kind = detect_statement_kind(frame)
            if kind in frames:
                errors.append(f"{file_name}：重复上传了同类文件。")
            frames[kind] = frame
        except Exception as exc:
            errors.append(f"{file_name}：{exc}")
    imports: list[BrokerImport] = []
    if ("hb_trades" in frames) != ("hb_cash" in frames):
        errors.append("华宝需要同时上传交割单和资金明细。")
    elif "hb_trades" in frames:
        imports.append(build_huabao_import(frames["hb_trades"], frames["hb_cash"]))
    if "yh_trades" in frames:
        imports.append(build_yinhe_import(frames["yh_trades"]))
    if not imports and not errors:
        errors.append("没有可导入的交割单。")
    return {"imports": imports, "errors": errors, "ok": bool(imports) and not errors}


def _price_close(a: float, b: float) -> bool:
    return abs(a - b) <= max(PRICE_MATCH_TOLERANCE, abs(b) * 0.0005)


def plan_takeovers(imports: list[BrokerImport], manual_trades: pd.DataFrame, manual_flows: pd.DataFrame) -> dict[str, object]:
    """手工记录与交割单记录一一对应时由交割单接管，并继承策略说明和备注。"""
    taken_trades: dict[int, dict] = {}
    taken_flows: set[int] = set()
    pending_trades: list[int] = []
    pending_flows: list[int] = []
    new_trades = [trade for item in imports for trade in item.trades]
    new_flows = [flow for item in imports for flow in item.flows]
    ranges = [(item.start, item.end) for item in imports]

    def in_range(day: str) -> bool:
        return any(start <= day <= end for start, end in ranges)

    used: set[int] = set()
    for row in manual_trades.itertuples(index=False):
        if not in_range(str(row.trade_date)):
            continue
        match = next(
            (
                position
                for position, trade in enumerate(new_trades)
                if position not in used
                and trade["trade_date"] == str(row.trade_date)
                and trade["symbol"] == str(row.symbol)
                and trade["side"] == str(row.side)
                and trade["quantity"] == int(row.quantity)
                and _price_close(trade["price"], float(row.price))
            ),
            None,
        )
        if match is None:
            pending_trades.append(int(row.id))
            continue
        used.add(match)
        taken_trades[int(row.id)] = new_trades[match]
        new_trades[match]["strategy"] = str(row.strategy or "") if pd.notna(row.strategy) else ""
        manual_note = str(row.notes or "") if pd.notna(row.notes) else ""
        new_trades[match]["notes"] = "；".join(part for part in (new_trades[match]["notes"], manual_note) if part)
    used_flows: set[int] = set()
    for row in manual_flows.itertuples(index=False):
        if not in_range(str(row.flow_date)):
            continue
        match = next(
            (
                position
                for position, flow in enumerate(new_flows)
                if position not in used_flows
                and flow["flow_date"] == str(row.flow_date)
                and abs(flow["amount"] - float(row.amount)) <= 0.01
                and (flow["entry_type"] == str(row.entry_type)
                     or {flow["entry_type"], str(row.entry_type)} <= {"期初资金", "资金转入"})
            ),
            None,
        )
        if match is None:
            pending_flows.append(int(row.id))
            continue
        used_flows.add(match)
        taken_flows.add(int(row.id))
    return {
        "taken_trade_ids": sorted(taken_trades),
        "taken_flow_ids": sorted(taken_flows),
        "pending_trade_ids": pending_trades,
        "pending_flow_ids": pending_flows,
    }


def _manual_records(conn) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = pd.read_sql_query(
        "SELECT * FROM live_trades WHERE record_key IS NULL OR record_key NOT LIKE 'stmt:%'", conn
    )
    flows = pd.read_sql_query(
        "SELECT * FROM live_cash_flows WHERE record_key IS NULL OR record_key NOT LIKE 'stmt:%'", conn
    )
    return trades, flows


def describe_takeovers(imports: list[BrokerImport]) -> dict[str, object]:
    db.init_db()
    with closing(db.get_conn()) as conn:
        manual_trades, manual_flows = _manual_records(conn)
    plan = plan_takeovers(imports, manual_trades, manual_flows)
    return {
        **plan,
        "pending_trades": manual_trades[manual_trades["id"].isin(plan["pending_trade_ids"])],
        "pending_flows": manual_flows[manual_flows["id"].isin(plan["pending_flow_ids"])],
    }


def _normalize_opening(conn) -> None:
    """账户只保留一笔期初资金：最早的一笔外部转入。"""
    conn.execute("UPDATE live_cash_flows SET entry_type='资金转入' WHERE entry_type='期初资金'")
    first = conn.execute(
        """
        SELECT id FROM live_cash_flows WHERE entry_type='资金转入'
        ORDER BY flow_date, COALESCE(flow_time, ''), id LIMIT 1
        """
    ).fetchone()
    if first:
        conn.execute("UPDATE live_cash_flows SET entry_type='期初资金' WHERE id=?", (first[0],))


def commit_statement_import(imports: list[BrokerImport]) -> dict[str, int]:
    """写入交割单记录：替换同券商同日期范围内的旧交割单记录，接管对应的手工记录。"""
    from services.live_trading import build_live_positions

    db.init_db()
    now = datetime.now().isoformat(timespec="seconds")
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        names = _names_from_ledger(conn)
        manual_trades, manual_flows = _manual_records(conn)
        plan = plan_takeovers(imports, manual_trades, manual_flows)
        removed = 0
        for item in imports:
            prefix = f"{STATEMENT_KEY_PREFIX}{item.broker}:%"
            removed += conn.execute(
                "DELETE FROM live_trades WHERE record_key LIKE ? AND trade_date BETWEEN ? AND ?",
                (prefix, item.start, item.end),
            ).rowcount
            removed += conn.execute(
                "DELETE FROM live_cash_flows WHERE record_key LIKE ? AND flow_date BETWEEN ? AND ?",
                (prefix, item.start, item.end),
            ).rowcount
        for trade_id in plan["taken_trade_ids"]:
            conn.execute("DELETE FROM live_trades WHERE id=?", (trade_id,))
        for flow_id in plan["taken_flow_ids"]:
            conn.execute("DELETE FROM live_cash_flows WHERE id=?", (flow_id,))
        inserted_trades = inserted_flows = 0
        for item in imports:
            for trade in item.trades:
                conn.execute(
                    """
                    INSERT INTO live_trades (
                        record_key, trade_date, trade_time, symbol, name, side, price, quantity,
                        fee_rate_pct, strategy, notes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade["record_key"], trade["trade_date"], trade["trade_time"], trade["symbol"],
                        names.get(trade["symbol"], trade["name"]), trade["side"], trade["price"],
                        trade["quantity"], _fee_rate(trade), trade["strategy"], trade["notes"], now,
                    ),
                )
                inserted_trades += 1
            for flow in item.flows:
                conn.execute(
                    """
                    INSERT INTO live_cash_flows (
                        record_key, flow_date, flow_time, entry_type, amount, symbol, notes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        flow["record_key"], flow["flow_date"], flow["flow_time"], flow["entry_type"],
                        flow["amount"], flow["symbol"], flow["notes"], now,
                    ),
                )
                inserted_flows += 1
        _normalize_opening(conn)
        trades = pd.read_sql_query("SELECT * FROM live_trades", conn)
        build_live_positions(trades)  # 卖出超过持仓时抛错并回滚
        conn.commit()
        return {
            "inserted_trades": inserted_trades,
            "inserted_flows": inserted_flows,
            "replaced_statement_records": removed,
            "taken_over_trades": len(plan["taken_trade_ids"]),
            "taken_over_flows": len(plan["taken_flow_ids"]),
            "pending_manual_records": len(plan["pending_trade_ids"]) + len(plan["pending_flow_ids"]),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


__all__ = [
    "BrokerImport",
    "build_huabao_import",
    "build_yinhe_import",
    "commit_statement_import",
    "describe_takeovers",
    "detect_statement_kind",
    "is_stock_code",
    "plan_takeovers",
    "preview_statement_import",
    "read_statement_table",
]
