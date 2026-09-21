"""Cache adapter and explicit evidence inbox; fetching never writes the ledger."""
import json
from pathlib import Path
import pandas as pd
from services.microcap_rotation_policy import DataGap, now, settled, sessions
from services.microcap_rotation_store import dumps, digest
from services.microcap_rotation_engine import positive

def inbox():
    from core.paths import RAW_DIR
    return RAW_DIR/"microcap_rotation_evidence"

def save_evidence(raw,folder=None):
    evidence=json.loads(raw)
    day=str(evidence.get("date",""))
    from datetime import date
    if date.fromisoformat(day).isoformat()!=day or day>settled():
        raise DataGap("证据日期必须是已完成交易日")
    if not evidence.get("source"):
        raise DataGap("证据必须注明来源")
    folder=Path(folder or inbox())
    folder.mkdir(parents=True,exist_ok=True)
    dest=folder/(day+".json")
    temp=dest.with_suffix(".tmp")
    temp.write_text(dumps(evidence),encoding="utf-8")
    temp.replace(dest)
    return day

def snapshot_records(frame):
    rows=[]
    for _,r in frame.sort_values("代码").iterrows():
        rows.append(dict(code=str(r["代码"]).zfill(6),name=str(r["名称"]),
                         market_cap=float(r["总市值(亿元)"]),snapshot_date=str(r["快照日期"]),
                         retrieved_at=str(r["快照时间"])))
    return rows

def evidence_template(day):
    from services.microcap import load_microcap_constituent_snapshots
    f,_=load_microcap_constituent_snapshots()
    if f is None or f.empty:
        raise DataGap("缺成分快照")
    f=f.loc[f["快照日期"].astype(str)==day]
    if f.empty:
        raise DataGap(day+" 缺成分快照")
    raw=snapshot_records(f)
    frozen=inbox().parent/"microcap_rotation_snapshots"/(day+".json")
    if frozen.exists():
        raw=json.loads(frozen.read_text(encoding="utf-8"))["rows"]
    return dict(date=day,source="",snapshot_hash=digest(raw),snapshot_source="",
                constituent_eligibility={r["code"]:dict(is_st=None,asof=day,source="") for r in raw},
                quotes={code:dict(date=day,close=None,adjustment="none",formal=None,source="",
                                   eligible=None,eligibility_source="",halted=None,limit_up=None,
                                   limit_down=None,min_buy=None,halt_source="")
                        for code in [r["code"] for r in raw]+["512890"]},
                events_complete=None,events_source="",events=[])


class CacheProvider:
    def __init__(self,evidence_dir=None):
        self.evidence_dir=Path(evidence_dir) if evidence_dir else inbox()

    def history(self,day):
        from services.microcap import load_microcap_index_series
        f=load_microcap_index_series()
        if f.empty:
            raise DataGap("缺BK1158正式指数历史")
        f=f.rename(columns={"日期_dt":"date","微盘股指数":"close"})
        f["date"]=pd.to_datetime(f.date).dt.strftime("%Y-%m-%d")
        # Keep complete warmup: restarting must not reset hysteresis in a long band.
        f=f.loc[f.date<=day].sort_values("date")
        if f.empty or f.date.iloc[-1]!=day or f.date.duplicated().any():
            raise DataGap(f"{day} 缺BK1158正式收盘")
        if list(sessions(f.date.iloc[0],day))!=f.date.tolist():
            raise DataGap("指数预热历史存在交易日缺口")
        for v in f.close:
            positive(v,"指数正式收盘")
        return f.to_dict("records")

    def batch(self,day):
        frozen=self.evidence_dir.parent/"microcap_rotation_snapshots"/(day+".json")
        if frozen.exists():
            captured=json.loads(frozen.read_text(encoding="utf-8"))
            raw=captured["rows"]
            if digest(raw)!=captured["hash"]:
                raise DataGap("独立冻结快照哈希不符")
        else:
            from services.microcap import load_microcap_constituent_snapshots
            f,_=load_microcap_constituent_snapshots()
            if f is None or f.empty:
                raise DataGap(f"{day} 缺当日成分快照")
            f=f.loc[f["快照日期"].astype(str)==day].copy()
            if f.empty:
                raise DataGap(f"{day} 缺当日成分快照，禁止用今天成分回填")
            raw=snapshot_records(f)
        path=self.evidence_dir/(day+".json")
        if not path.exists():
            raise DataGap(f"{day} 缺交易资格/权益事件证据文件；请在数据与运行导入核验记录")
        evidence=json.loads(path.read_text(encoding="utf-8-sig"))
        if evidence.get("date")!=day:
            raise DataGap("证据文件日期错误")
        if evidence.get("snapshot_hash")!=digest(raw) or not evidence.get("snapshot_source"):
            raise DataGap("成分快照哈希或来源缺失，需核验原始行情日期；不能只勾选通过")
        eligibility=evidence.get("constituent_eligibility",{})
        rows=[]
        for r in raw:
            code=r["code"]
            proof=eligibility.get(code,{})
            rows.append(dict(code=code,name=r["name"],market_cap=r["market_cap"],
                             is_st=proof.get("is_st"),eligibility_source=proof.get("source"),asof=proof.get("asof")))
        history=self.history(day)
        return dict(date=day,index_code="BK1158",index_close=history[-1]["close"],formal=True,
                    source="工作台正式BK1158收盘",retrieved_at=now().isoformat(timespec="seconds"),
                    snapshot_date=day,snapshot_source=evidence["snapshot_source"],
                    snapshot_retrieved_at=min(r["retrieved_at"] for r in raw),constituents=rows,
                    quotes=evidence.get("quotes",{}),events=evidence.get("events",[]),
                    events_complete=evidence.get("events_complete"),events_source=evidence.get("events_source"),
                    evidence=evidence,raw_snapshot=raw)

    def refresh(self,day,required=()):
        from services.microcap import (fetch_microcap_stocks,save_microcap_constituent_snapshot,
                                       load_microcap_constituent_snapshots)
        f,_=load_microcap_constituent_snapshots()
        has=(self.evidence_dir.parent/"microcap_rotation_snapshots"/(day+".json")).exists() or (f is not None and not f.empty and (f["快照日期"].astype(str)==day).any())
        if not has and day==now().date().isoformat() and day<=settled():
            fresh=fetch_microcap_stocks(page_size=400,retries=1)
            if set(fresh["日期"].astype(str))!={day}:
                raise DataGap("成分行情日期未全部对齐；拒绝保存")
            save_microcap_constituent_snapshot(fresh,pool_count=400)
        if not has:
            f,_=load_microcap_constituent_snapshots()
        if f is not None and not f.empty:
            current=f.loc[f["快照日期"].astype(str)==day]
            frozen=self.evidence_dir.parent/"microcap_rotation_snapshots"/(day+".json")
            if not current.empty and not frozen.exists():
                raw=snapshot_records(current)
                captured=dict(rows=raw,hash=digest(raw),captured_at=now().isoformat(timespec="seconds"),
                              source="eastmoney/BK1158/shared_snapshot")
                frozen.parent.mkdir(parents=True,exist_ok=True)
                temp=frozen.with_suffix(".tmp")
                temp.write_text(dumps(captured),encoding="utf-8")
                temp.replace(frozen)
        try:
            self.history(day)
        except DataGap:
            from services.update_tasks import run_index_ma20_update
            run_index_ma20_update(index_names=["微盘股"])
        path=self.evidence_dir/(day+".json")
        if not path.exists():
            return
        evidence=json.loads(path.read_text(encoding="utf-8-sig"))
        changed=False
        for code in sorted(set(required)):
            q=evidence.get("quotes",{}).get(code)
            if not q or q.get("halted") or q.get("close") is not None:
                continue
            from services.fund_analysis import fetch_tickflow_fund_close,infer_tickflow_symbol,FUND_ADJUST_NONE
            from core.cache import load_dataset,save_dataset
            symbol=infer_tickflow_symbol(code)
            key="microcap_rotation_raw_"+symbol
            frame,_=load_dataset(key,"tickflow","microcap_rotation_close")
            found=pd.DataFrame() if frame is None else frame.loc[pd.to_datetime(frame["日期"]).dt.strftime("%Y-%m-%d")==day]
            if found.empty:
                fresh=fetch_tickflow_fund_close(symbol,count=30,adjust=FUND_ADJUST_NONE)
                fresh=fresh.loc[pd.to_datetime(fresh["日期"]).dt.strftime("%Y-%m-%d")<=settled()]
                frame=fresh if frame is None else pd.concat([frame,fresh]).drop_duplicates("日期",keep="first")
                save_dataset(symbol=key,name=code,source="tickflow",data_type="microcap_rotation_close",df=frame)
                found=frame.loc[pd.to_datetime(frame["日期"]).dt.strftime("%Y-%m-%d")==day]
            if len(found)==1:
                q["close"]=float(found["收盘价"].iloc[0])
                q["source"]="TickFlow未复权正式日线"
                changed=True
        if changed:
            save_evidence(dumps(evidence),self.evidence_dir)
