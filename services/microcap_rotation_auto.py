"""Automatic TickFlow/AkShare inputs; missing optional evidence is disclosed."""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from services.microcap_rotation_data import CacheProvider,snapshot_records
from services.microcap_rotation_policy import now,settled,ETF,DataGap
from services.microcap_rotation_store import dumps
from services.fund_analysis import infer_tickflow_symbol

def ak_call(name,kwargs,timeout=18):
    source="""import contextlib,io,json,sys
import akshare as ak
with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    frame=getattr(ak,sys.argv[1])(**json.loads(sys.argv[2]))
print(frame.to_json(orient='records',date_format='iso',force_ascii=False))
"""
    r=subprocess.run([sys.executable,"-c",source,name,json.dumps(kwargs)],capture_output=True,text=True,timeout=timeout)
    if r.returncode:
        raise DataGap(name+" 数据源暂不可用")
    return json.loads(r.stdout)

def raw_snapshot(day):
    from services.microcap import load_microcap_constituent_snapshots
    f,_=load_microcap_constituent_snapshots()
    return [] if f is None or f.empty else snapshot_records(f.loc[f["快照日期"].astype(str)==day])

def basic_batch(day,history,rows):
    return dict(date=day,index_code="BK1158",index_close=history[-1]["close"],formal=True,
        source="工作台正式BK1158收盘",retrieved_at=now().isoformat(timespec="seconds"),
        snapshot_date=day,snapshot_source="BK1158已保存当日收盘成分/名称筛ST",
        snapshot_retrieved_at=min((r["retrieved_at"] for r in rows),default=day+" 15:10:00"),
        constituents=[dict(code=r["code"],name=r["name"],market_cap=r["market_cap"],
            is_st="ST" in r["name"].upper(),asof=day,eligibility_source="当日BK1158成分名称") for r in rows],
        raw_snapshot=rows,quotes={},events=[],events_complete=False,
        events_source="自动采集；缺项继续模拟",skip_issues=True,warnings=[],index_history=history)

class AutomaticProvider(CacheProvider):
    def __init__(self,evidence_dir=None,allow_fetch=False):
        super().__init__(evidence_dir)
        self.required=set()
        self.allow_fetch=allow_fetch

    def refresh(self,day,required=()):
        self.required=set(required)
        try:
            self.history(day)
        except Exception:
            from services.update_tasks import run_index_ma20_update
            run_index_ma20_update(index_names=["微盘股"])

    def batch(self,day):
        try:
            result=super().batch(day)
            result.update(skip_issues=True,warnings=[])
            return result
        except (DataGap,ValueError,KeyError):
            pass
        history=self.history(day)
        result=basic_batch(day,history,raw_snapshot(day))
        if not result["constituents"]:
            result["warnings"].append("当日成分缺失：执行此前计划，暂不生成新周度名单")
        folder=self.evidence_dir.parent/"microcap_rotation_auto"
        path=folder/(day+".json")
        saved=json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        quotes=dict(saved.get("quotes",{}))
        if not self.allow_fetch:
            result["quotes"]=quotes
            result["warnings"].append("缓存预检：缺项不联网")
            return result
        from tickflow import TickFlow
        key=os.environ.get("TICKFLOW_API_KEY","")
        client=TickFlow(api_key=key,timeout=12,max_retries=0) if key else TickFlow.free()
        needed=sorted(self.required)
        metadata={}
        snapshots={}
        if needed and day==now().date().isoformat():
            try:
                metadata={r["symbol"]:r for r in client.instruments.get([infer_tickflow_symbol(c) for c in needed])}
            except Exception:
                result["warnings"].append("证券元数据未取得；有价格的成交标注为资格未完全核验")
        if needed and day==now().date().isoformat():
            try:
                snapshots={r["symbol"]:r for r in client.quotes.get(symbols=[infer_tickflow_symbol(c) for c in needed])}
            except Exception:
                pass
        def get_quote(code):
            symbol=infer_tickflow_symbol(code)
            old=quotes.get(code)
            if old and old.get("formal") and old.get("close") and old.get("date")==day:
                return code,old
            close=volume=None
            errors=[]
            source="TickFlow未复权日线"
            try:
                frame=client.klines.get(symbol,period="1d",count=8,adjust="none",as_dataframe=True)
                frame=frame.loc[pd.to_datetime(frame.trade_date).dt.strftime("%Y-%m-%d")==day]
                if len(frame)==1:
                    close=float(frame.close.iloc[0])
                    volume=float(frame.volume.iloc[0]) if "volume" in frame else None
            except Exception as exc:
                errors.append("TickFlow日线："+type(exc).__name__)
            if close is None and symbol not in snapshots and day==now().date().isoformat():
                try:
                    single=client.quotes.get(symbols=[symbol])
                    for item in single:
                        if item.get("symbol")==symbol:
                            snapshots[symbol]=item
                except Exception as exc:
                    errors.append("TickFlow收盘快照："+type(exc).__name__)
            if close is None and symbol in snapshots:
                snapshot=snapshots[symbol]
                stamp=pd.Timestamp(snapshot["timestamp"],unit="ms",tz="UTC").tz_convert("Asia/Shanghai")
                if stamp.strftime("%Y-%m-%d")==day and stamp.hour>=15:
                    close=float(snapshot["last_price"]); volume=float(snapshot["volume"])
                    source="TickFlow当日15点后收盘快照"
            if close is None:
                try:
                    name="fund_etf_hist_em" if code==ETF else "stock_zh_a_hist"
                    data=ak_call(name,dict(symbol=code,period="daily",start_date=day.replace("-",""),end_date=day.replace("-",""),adjust=""))
                    matched=[r for r in data if str(r.get("日期",""))[:10]==day]
                    if len(matched)==1:
                        close=float(matched[0]["收盘"]); volume=float(matched[0]["成交量"])
                        source="AkShare东方财富未复权日线"
                except Exception as exc:
                    errors.append("AkShare日线："+type(exc).__name__)
            import math
            if close is not None and (not math.isfinite(close) or close<=0):
                close=None
            if volume is not None and not math.isfinite(volume):
                volume=None
            meta=metadata.get(symbol,{})
            ext=meta.get("ext") or {}
            up,down=ext.get("limit_up"),ext.get("limit_down")
            return code,dict(date=day,close=close,adjustment="none",formal=close is not None,source=source,
                halted=None if volume is None else volume==0,
                halt_source="当日日线成交量为零（保守跳过）" if volume==0 else "",
                limit_up=close>=float(up)-1e-8 if close is not None and up else None,
                limit_down=close<=float(down)+1e-8 if close is not None and down else None,
                eligible=("ST" not in str(meta["name"]).upper()) if meta.get("name") else None,
                eligibility_source="TickFlow同日证券元数据" if meta else "未核验",
                min_buy=200 if code.startswith(("688","689")) else 100,metadata=meta,volume=volume,source_errors=errors)
        with ThreadPoolExecutor(max_workers=2) as pool:
            quotes.update(dict(pool.map(get_quote,needed)))
        client.close()
        result["quotes"]=quotes
        result["warnings"].append("权益事件未完全核验：继续模拟，收益仅为参考")
        folder.mkdir(parents=True,exist_ok=True)
        temp=path.with_suffix(".tmp")
        temp.write_text(dumps(result),encoding="utf-8")
        temp.replace(path)
        return result
