"""Pure next-session execution. No I/O, no implicit market assumptions."""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR
import math
from services.microcap_rotation_policy import (DataGap, INITIAL, FEE, ETF, money,
                                               ETF_FEE_RATE, transaction_fee,
                                               next_day, weekly_turn, sessions)

def positive(value, label):
    if isinstance(value, bool):
        raise DataGap(f"{label}必须是数值")
    try:
        x = float(value)
    except (ValueError, TypeError):
        raise DataGap(f"{label}缺失") from None
    if not math.isfinite(x) or x <= 0:
        raise DataGap(f"{label}无效")
    return x

def validate_batch(batch, day, enabled_day):
    if batch.get("date") != day or batch.get("index_code") != "BK1158":
        raise DataGap("批次日期或指数身份错误")
    if batch.get("snapshot_date") != day or day < enabled_day:
        raise DataGap("缺当日成分快照或快照早于启用日期")
    for key in ("source", "retrieved_at", "snapshot_source", "snapshot_retrieved_at"):
        if not batch.get(key):
            raise DataGap(f"输入缺来源字段：{key}")
    from datetime import datetime
    timestamp = datetime.fromisoformat(batch["snapshot_retrieved_at"])
    if timestamp.date().isoformat() != day or timestamp.hour < 15:
        raise DataGap("成分快照必须在当日收盘后采集")
    positive(batch.get("index_close"), "指数正式收盘")
    if batch.get("formal") is not True:
        raise DataGap("指数收盘未正式确认")
    rows = batch.get("constituents", [])
    symbols = [r.get("code") for r in rows]
    if (len(rows) < 20 and not batch.get("skip_issues")) or len(set(symbols)) != len(symbols):
        raise DataGap("成分不足20只或代码重复")
    for row in rows:
        code = row.get("code", "")
        if len(code) != 6 or not code.isdigit():
            raise DataGap("成分代码无效")
        positive(row.get("market_cap"), f"{code}市值")
        if type(row.get("is_st")) is not bool or not row.get("eligibility_source"):
            raise DataGap(f"{code} ST资格未核验")
        if row.get("asof") != day:
            raise DataGap(f"{code}资格或市值日期不符")
    if len([r for r in rows if not r["is_st"]]) < 20 and not batch.get("skip_issues"):
        raise DataGap("非ST成分不足20只")
    if not batch.get("skip_issues") and (batch.get("events_complete") is not True or not batch.get("events_source")):
        raise DataGap("权益事件核验未完成")
    seen = set()
    for event in batch.get("events", []):
        if not event.get("id") or event["id"] in seen or event.get("date") != day or not event.get("source"):
            raise DataGap("权益事件缺唯一编号、日期或来源")
        seen.add(event["id"])
        from datetime import date
        try:
            record = date.fromisoformat(event.get("record_date","")).isoformat()
        except (ValueError,TypeError):
            raise DataGap("权益登记日期无效") from None
        if not isinstance(event.get("code"),str) or len(event["code"])!=6 or not event["code"].isdigit():
            raise DataGap("权益事件证券代码无效")
        if record >= day:
            raise DataGap("权益登记日必须早于入账日")
        if event.get("kind") not in ("cash", "shares"):
            raise DataGap("不支持的权益事件；暂停待核对")
        value = event.get("cash_per_share") if event["kind"]=="cash" else event.get("new_shares_per_share")
        positive(value, "权益事件数额")

def ranking(batch):
    return [r["code"] for r in sorted((r for r in batch["constituents"] if not r["is_st"]),
                                     key=lambda r:(float(r["market_cap"]),r["code"]))[:20]]

def regime(history, previous=None):
    if len(history) < 121:
        raise DataGap("缺S0之前至少120个交易日正式指数历史")
    current = previous
    for i in range(14, len(history)):
        ma = sum(float(v["close"]) for v in history[i-14:i+1])/15
        p = positive(history[i]["close"], "历史指数收盘")
        if p > ma*1.025:
            current = "on"
        elif p < ma*.975:
            current = "off"
    return current

def quote(batch, code, previous_price=None):
    q = batch.get("quotes", {}).get(code)
    if not q or q.get("date") != batch["date"] or q.get("adjustment") != "none" or not q.get("source"):
        raise DataGap(f"{code} 缺当日正式未复权行情")
    if batch.get("skip_issues"):
        q = deepcopy(q)
        missing = [k for k in ("halted","limit_up","limit_down","eligible") if type(q.get(k)) is not bool]
        q["assumptions"] = "未核验："+"、".join(missing) if missing else ""
        for key in missing:
            q[key] = key=="eligible"
        q.setdefault("min_buy",200 if code.startswith(("688","689")) else 100)
    for key in ("halted", "limit_up", "limit_down", "eligible"):
        if type(q.get(key)) is not bool:
            raise DataGap(f"{code} {key}资格未知")
    if q.get("formal") is not True or q.get("close") is None and not q.get("halted"):
        raise DataGap(f"{code} 缺当日正式收盘价，等待补价")
    if not q.get("eligibility_source") and not batch.get("skip_issues"):
        raise DataGap(f"{code} 交易资格来源未核验")
    if type(q.get("min_buy")) is not int or q["min_buy"] < (200 if code.startswith(("688","689")) else 100):
        raise DataGap(f"{code} 最低申报数量未核验")
    if q["halted"]:
        if not q.get("halt_source"):
            raise DataGap(f"{code} 停牌证据缺失")
        p = positive(previous_price, f"{code}前值") if previous_price is not None else None
    else:
        p = positive(q.get("close"), f"{code}收盘")
    return q, Decimal(str(p)) if p is not None else None

def make_plan(strategy, state, batch, risk, previous=None, initial=False):
    day, following = batch["date"], next_day(batch["date"])
    mode = "stock" if strategy=="B" or risk=="on" else "etf" if strategy=="D" and risk=="off" else "cash"
    old_mode = previous.get("mode") if previous else None
    trigger = initial or mode != old_mode or (mode=="stock" and weekly_turn(day,following))
    ranking_ready = len(ranking(batch)) >= 20
    if mode=="stock" and not ranking_ready:
        trigger = False
    target = ranking(batch) if mode=="stock" and trigger else (
             previous.get("targets", []) if mode=="stock" and previous else [])
    held = state["positions"]
    buys = [c for c in target if c not in held] if trigger else []
    sells = [c for c in held if c not in (target if mode=="stock" else [ETF] if mode=="etf" else [])]
    return dict(signal_date=day, execution_date=following, risk=risk, mode=mode, targets=target,
                buys=buys, sells=sells, buy_etf=mode=="etf" and (trigger or bool(sells)),
                reason="首次配置" if initial else "状态变化" if mode!=old_mode else "周度轮动" if trigger else "持有/待卖复核")

def initialize(strategy, batch, history):
    risk = regime(history)
    state = dict(date=batch["date"], cash="0.00" if strategy=="A" else str(INITIAL), equity=str(INITIAL),
                 index_base=str(batch["index_close"]), positions={}, risk=risk, peak=str(INITIAL),
                 return_pct=0., drawdown_pct=0., cash_ratio=0. if strategy=="A" else 1.,
                 stock_ratio=0., etf_ratio=0., pnl="0.00", fills=[], failures=[], events=[])
    state["plan"] = None if strategy=="A" else make_plan(strategy,state,batch,risk,initial=True)
    return state

def max_etf_quantity(cash, price, lot=100):
    """Largest whole-lot ETF quantity whose amount plus commission fits cash."""
    cash, price = Decimal(str(cash)), Decimal(str(price))
    if price <= 0 or cash <= 0:
        return 0
    qty = int((cash / (price * (Decimal("1") + ETF_FEE_RATE) * lot))
              .to_integral_value(rounding=ROUND_FLOOR)) * lot
    while qty and money(price * qty) + transaction_fee(ETF, price * qty) > cash:
        qty -= lot
    while money(price * (qty + lot)) + transaction_fee(ETF, price * (qty + lot)) <= cash:
        qty += lot
    return qty

def execute(strategy, previous, batch, history, entitlements=None):
    day = batch["date"]
    if next_day(previous["date"]) != day:
        raise DataGap("日结必须连续，禁止跳过缺失交易日")
    state = deepcopy(previous)
    state.pop("strategy", None)
    state.pop("completed_at", None)
    state.update(date=day, fills=[], failures=[], events=[], warnings=list(batch.get("warnings",[])), provisional=bool(batch.get("skip_issues") and not batch.get("events_complete")))
    cash = money(state["cash"])
    positions = state["positions"]
    oldplan = state["plan"]
    if strategy=="A":
        eq = money(INITIAL*Decimal(str(batch["index_close"]))/Decimal(state["index_base"]))
    else:
        if oldplan["execution_date"] != day or oldplan["signal_date"] >= day:
            raise DataGap("计划与执行日不符")
        required = set(positions) | set(oldplan["buys"])
        required.update(e["code"] for e in batch.get("events", []) if e["kind"]=="shares" and (entitlements or {}).get(e["id"],0))
        if oldplan["buy_etf"]:
            required.add(ETF)
        prices = {}
        # Resolve every input before changing state. No invented suspension prices.
        for code in sorted(required):
            try:
                prices[code] = quote(batch,code,positions.get(code,{}).get("price"))
                if prices[code][0].get("assumptions"):
                    state["warnings"].append(code+" "+prices[code][0]["assumptions"])
                    state["provisional"] = True
            except DataGap as exc:
                if not batch.get("skip_issues"):
                    raise
                old_price = positions.get(code,{}).get("price")
                prices[code] = (dict(blocked_reason=str(exc),halted=False,limit_up=False,limit_down=False,
                                     eligible=False,assumptions=str(exc)),Decimal(old_price) if old_price else None)
                state["warnings"].append(code+" 缺价格/资格，跳过交易；已有持仓按前值参考估值")
                state["provisional"] = True
        for event in batch.get("events", []):
            qty = (entitlements or {}).get(event["id"])
            if qty is None:
                raise DataGap(f'权益事件 {event["id"]} 缺登记日持仓核对')
            amount = money(Decimal(str(qty))*Decimal(str(event.get("cash_per_share",0))))
            if event["kind"]=="cash":
                cash += amount
            elif qty:
                increment = Decimal(str(qty))*Decimal(str(event["new_shares_per_share"]))
                if increment != increment.to_integral_value():
                    raise DataGap("送转零碎股处理未核验")
                code = event["code"]
                if code not in positions:
                    positions[code] = dict(quantity=0,price=str(prices.get(code,(None,0))[1]),name=code)
                    prices[code] = quote(batch,code)
                positions[code]["quantity"] += int(increment)
            state["events"].append(dict(**event, entitlement_quantity=qty, amount=str(amount)))
        def fail(code, side, reason):
            state["failures"].append(dict(date=day,code=code,side=side,reason=reason))
        def fill(code, side, qty, price):
            nonlocal cash
            amount = money(price*qty)
            fee = transaction_fee(code, amount)
            cash += amount-fee if side=="sell" else -amount-fee
            if side=="sell":
                del positions[code]
            else:
                positions[code] = dict(quantity=qty,price=str(price),name=next((r.get("name",code) for r in batch["constituents"] if r["code"]==code),"红利低波ETF" if code==ETF else code))
            state["fills"].append(dict(date=day,code=code,side=side,quantity=qty,price=str(price),
                                       amount=str(amount),fee=str(fee),cash_after=str(cash),qualification_note=prices[code][0].get("assumptions","已核对可用行情与资格")))
        # Reconcile old intentions against the frozen final target.
        allowed = set(oldplan["targets"]) if oldplan["mode"]=="stock" else {ETF} if oldplan["mode"]=="etf" else set()
        for code in sorted(set(positions)-allowed):
            q,p = prices[code]
            if q.get("blocked_reason"):
                fail(code,"sell",q["blocked_reason"]+"；保留待卖")
                continue
            if q["halted"] or q["limit_down"]:
                fail(code,"sell","停牌" if q["halted"] else "跌停；保留待卖")
                continue
            fill(code,"sell",positions[code]["quantity"],p)
        for code in oldplan["buys"]:
            if code in positions:
                continue
            q,p = prices[code]
            if q.get("blocked_reason"):
                fail(code,"buy",q["blocked_reason"]+"；本轮跳过，补价后重算")
                continue
            if q["halted"]:
                fail(code,"buy","停牌；本次取消")
                continue
            qty = int((Decimal("10000")/p/100).quantize(Decimal("1"),rounding=ROUND_HALF_UP))*100
            reason = ("停牌" if q["halted"] else "涨停" if q["limit_up"] else
                      "交易资格不符" if not q["eligible"] else
                      "最低申报数量不符" if qty < q["min_buy"] else
                      "20只名额已占满" if len(set(positions)-{ETF}) >= 20 else
                      "现金不足" if money(p*qty)+transaction_fee(code, p*qty) > cash else "")
            if reason:
                fail(code,"buy",reason+"；本次取消")
            else:
                fill(code,"buy",qty,p)
        if oldplan["buy_etf"] and cash > transaction_fee(ETF, cash):
            q,p = prices[ETF]
            qty = max_etf_quantity(cash,p) if p is not None else 0
            if q.get("blocked_reason") or q["halted"] or q["limit_up"] or not q["eligible"]:
                fail(ETF,"buy","ETF停牌、涨停或资格不符；本次取消")
            elif qty >= q["min_buy"]:
                old_qty = positions.get(ETF,{}).get("quantity",0)
                fill(ETF,"buy",qty,p)
                positions[ETF]["quantity"] += old_qty
        value = Decimal(0)
        for code,pos in positions.items():
            q,p = prices[code]
            pos.update(price=str(p), stale=q["halted"] or bool(q.get("blocked_reason")), valuation_note=q.get("blocked_reason","正式收盘" if not q["halted"] else "停牌前值"), value=str(money(p*pos["quantity"])))
            value += money(pos["value"])
        eq = money(cash+value)
    if cash < 0 or any(p["quantity"] < 0 for p in positions.values()) or len(set(positions)-{ETF}) > 20:
        raise ArithmeticError("现金或持仓约束失败")
    stock = sum((money(p["value"]) for c,p in positions.items() if c!=ETF), Decimal(0))
    etf = money(positions.get(ETF,{}).get("value",0))
    peak = max(money(previous["peak"]),eq)
    risk = regime(history)
    state.update(cash=str(cash), equity=str(eq), peak=str(peak), risk=risk,
                 pnl=str(money(eq-money(previous["equity"]))),
                 return_pct=float(eq/INITIAL-1)*100, drawdown_pct=float(eq/peak-1)*100,
                 cash_ratio=float(cash/eq) if strategy!="A" else 0.,
                 stock_ratio=float(stock/eq), etf_ratio=float(etf/eq))
    state["plan"] = None if strategy=="A" else make_plan(strategy,state,batch,risk,oldplan)
    return state
