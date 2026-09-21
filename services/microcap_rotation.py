"""Stable ABCD facade shared by Streamlit and the scheduled updater."""
from pathlib import Path
from services.microcap_rotation_policy import (VERSION,POLICY,NAMES,ETF,DataGap,settled,sessions,next_day,now)
from services.microcap_rotation_store import Store,process_lock
from services.microcap_rotation_data import CacheProvider,save_evidence
from services.microcap_rotation_engine import validate_batch,initialize,execute
from services.microcap_rotation_research import import_research as calculate_research

__all__=["VERSION","POLICY","NAMES","import_research","initialize_simulation",
         "update_simulation","read_strategy_view","save_evidence"]

def initialize_simulation(db_path=None,enabled_at=None):
    with process_lock(db_path):
        store=Store(db_path)
        accounts=store.enable(enabled_at)
        if db_path is None and not store.launch() and not store.days():
            prepare_launch(store,accounts[0]["enabled_at"][:10])
        return accounts

def prepare_launch(store,start):
    from datetime import date
    from services.market_calendar import previous_trading_day,get_market_window
    from services.microcap_rotation_auto import raw_snapshot,basic_batch
    reference=previous_trading_day(get_market_window("A股"),date.fromisoformat(start)).isoformat()
    provider=CacheProvider()
    history=provider.history(reference)
    batch=basic_batch(reference,history,raw_snapshot(reference))
    if len(batch["constituents"])<20:
        raise DataGap("首次名单缺失，尚不能准备首笔模拟")
    validate_batch(batch,reference,reference)
    states={s:initialize(s,batch,history) for s in "ABCD"}
    launch=dict(start_date=next_day(reference),reference_date=reference,batch=batch,states=states,
                note="首日使用前一交易日已保存名单及信号；今天收盘执行，A从首日收盘归一化")
    store.save_launch(launch)
    return launch

def import_research(root,db_path=None,strict_summary=False,expected=None):
    report=calculate_research(root,expected=expected,strict_summary=strict_summary)
    with process_lock(db_path):
        report["archive_id"]=Store(db_path).save_research(report)
    return report

def read_strategy_view(db_path=None):
    store=Store(db_path)
    days=store.days()
    latest={r["strategy"]:r for r in days}
    return dict(version=VERSION,policy=POLICY,accounts=store.accounts(),daily=days,
                latest=latest,launch=store.launch(),research=store.research(),input_manifest=store.input_manifest(),target_date=settled())

def _run(store,provider,target,preflight,refresh):
    accounts=store.accounts()
    if not accounts:
        gaps=[]
        if preflight:
            try:
                candidate=provider.batch(target)
                validate_batch(candidate,target,target)
                warmup=provider.history(target)
                if len(warmup)<121:
                    raise DataGap("缺至少120个交易日预热")
            except (DataGap,ValueError) as exc:
                gaps.append(dict(date=target,reason=str(exc)))
        return dict(status="未启用",message="请先在页面启用每日模拟"+("；"+gaps[0]["reason"] if gaps else ""),
                    completed=[],gaps=gaps)
    if len(accounts)!=4:
        raise DataGap("四账户初始化不完整")
    enabled=min(a["enabled_at"][:10] for a in accounts)
    rows=store.days()
    latest={r["strategy"]:r for r in rows}
    launch=store.launch()
    if not latest and launch:
        latest=launch["states"]
    if latest and (set(latest)!=set("ABCD") or len({r["date"] for r in latest.values()})!=1):
        raise DataGap("四账户日结未对齐")
    start=next_day(latest["A"]["date"]) if latest else enabled
    completed,gaps=[],[]
    for day in sessions(start,target):
        try:
            required=set()
            for r in latest.values():
                required.update(r["positions"])
                plan=r["plan"] or {}
                required.update(plan.get("buys",[]))
                if plan.get("buy_etf"):
                    required.add(ETF)
            if hasattr(provider,"required"):
                provider.required=set(required)
            if refresh and not preflight:
                provider.refresh(day,required)
            batch=provider.batch(day)
            validate_batch(batch,day,enabled)
            if latest:
                prev_batch=(store.batch(latest["A"]["date"]) if rows else launch["batch"]) if not preflight or not completed else previous_batch
                history=list(prev_batch["index_history"])+[dict(date=day,close=batch["index_close"])]
            else:
                history=provider.history(day)
            if len(history)<121 or history[-1]["date"]!=day or list(sessions(history[0]["date"],day))!=[h["date"] for h in history]:
                raise DataGap("正式指数预热必须连续且包括S0之前120个交易日")
            if abs(float(history[-1]["close"])-float(batch["index_close"])) > 1e-8:
                raise DataGap("批次指数与预热末日收盘不一致")
            batch["index_history"]=history
            states={}
            for strategy in "ABCD":
                if latest:
                    entitlements={}
                    for event in batch.get("events",[]):
                        record=event["record_date"]
                        if not rows or record < rows[0]["date"]:
                            qty=0
                        else:
                            found=[r for r in rows if r["strategy"]==strategy and r["date"]==record]
                            if not found:
                                raise DataGap("权益登记日持仓记录缺失")
                            qty=found[0]["positions"].get(event["code"],{}).get("quantity",0)
                        entitlements[event["id"]]=qty
                    previous=dict(latest[strategy])
                    if not rows and launch and strategy=="A":
                        previous["index_base"]=str(batch["index_close"])
                    states[strategy]=execute(strategy,previous,batch,history,entitlements)
                else:
                    states[strategy]=initialize(strategy,batch,history)
            if not preflight:
                store.commit_day(batch,states)
            latest={s:dict(strategy=s,**state) for s,state in states.items()}
            rows.extend(latest.values())
            previous_batch=batch
            completed.append(day)
        except (DataGap,ValueError,ArithmeticError) as exc:
            gaps.append(dict(date=day,reason=str(exc)))
            # Prior to S0, the first input-complete close may start the accounts.
            if latest:
                break
    if not rows and launch and target<launch["start_date"]:
        return dict(status="等待收盘",completed=[],gaps=[],last_complete=None,
                    message=f"四账户已启用，计划在{launch['start_date']}收盘模拟执行；当前没有实际模拟成交")
    return dict(status="待补数据" if gaps else "预检通过" if preflight else "已更新" if completed else "已是最新",
                completed=completed,gaps=gaps,last_complete=latest.get("A",{}).get("date"),
                message="；".join(g["date"]+" "+g["reason"] for g in gaps) or "四账户同日结算；重复日期跳过")

def repair_latest_missing_prices(store,provider):
    """Append an audit revision and replay original plans, never today's new ranking."""
    from copy import deepcopy
    from services.microcap_rotation_engine import positive
    rows=store.days()
    if not rows:
        return None
    day=max(r["date"] for r in rows)
    batch=store.batch(day)
    if not batch.get("skip_issues"):
        return None
    missing=[]
    for code,q in batch.get("quotes",{}).items():
        try:
            positive(q.get("close"),"收盘")
            if q.get("formal") is not True:
                missing.append(code)
        except DataGap:
            missing.append(code)
    if not missing:
        return None
    provider.required=set(missing)
    provider.refresh(day,missing)
    fresh=provider.batch(day)
    updated=deepcopy(batch)
    improved=[]
    for code in missing:
        q=fresh.get("quotes",{}).get(code,{})
        try:
            positive(q.get("close"),"收盘")
        except DataGap:
            continue
        if q.get("formal") is not True or q.get("date")!=day or q.get("adjustment")!="none":
            continue
        q=deepcopy(q)
        original=batch["quotes"].get(code,{})
        if not q.get("metadata") and original.get("metadata"):
            # Prior-day limits remain tied to the original trading day, not today's metadata.
            q["metadata"]=original["metadata"]
            q["eligible"]=original.get("eligible")
            q["eligibility_source"]=original.get("eligibility_source")
            ext=original["metadata"].get("ext") or {}
            for field,comparison in (("limit_up",lambda a,b:a>=b-1e-8),("limit_down",lambda a,b:a<=b+1e-8)):
                if ext.get(field):
                    q[field]=comparison(float(q["close"]),float(ext[field]))
        updated["quotes"][code]=q
        improved.append(code)
    if not improved:
        return None
    older=[r for r in rows if r["date"]<day]
    launch=store.launch()
    if older:
        previous={r["strategy"]:r for r in older}
    elif launch:
        previous=deepcopy(launch["states"])
        previous["A"]["index_base"]=str(batch["index_close"])
    else:
        raise DataGap("缺启动快照，不能安全补价重算")
    states={}
    for strategy in "ABCD":
        entitlements={}
        for event in batch.get("events",[]):
            matched=[r for r in older if r["strategy"]==strategy and r["date"]==event["record_date"]]
            entitlements[event["id"]]=matched[0]["positions"].get(event["code"],{}).get("quantity",0) if matched else 0
        states[strategy]=execute(strategy,previous[strategy],updated,updated["index_history"],entitlements)
    updated["price_revision"]=dict(at=now().isoformat(timespec="seconds"),codes=sorted(improved),
                                   reason="补齐当日正式收盘价后重放原计划，非次日追单")
    store.commit_day(updated,states,revision_reason=updated["price_revision"]["reason"])
    return dict(date=day,codes=sorted(improved))


def update_simulation(target=None,db_path=None,provider=None,preflight=False,refresh=False,log_job=True):
    target=target or settled()
    if target>settled():
        raise DataGap("禁止推进至尚未正式收盘日期")
    store=Store(db_path)
    if provider is None:
        from services.microcap_rotation_auto import AutomaticProvider
        provider=AutomaticProvider(allow_fetch=refresh and not preflight)
    if preflight:
        return _run(store,provider,target,True,False)
    with process_lock(db_path):
        job=None
        if log_job and db_path is None:
            from core.db import init_db,start_job
            init_db()
            job=start_job("微盘20 ABCD每日模拟")
        try:
            repaired=repair_latest_missing_prices(store,provider) if refresh else None
            result=_run(store,provider,target,False,refresh)
            if repaired:
                result["price_repair"]=repaired
                result["status"]="已补价重算"
                result["message"]=f"{repaired['date']}补齐{len(repaired['codes'])}只价格并重算，原日结已归档；"+result["message"]
            if job is not None:
                from core.db import finish_job
                finish_job(job,"failed" if result["gaps"] else "success",result["message"])
            return result
        except Exception as exc:
            if job is not None:
                from core.db import finish_job
                finish_job(job,"failed",str(exc))
            raise
