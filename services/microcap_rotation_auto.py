"""Automatic TickFlow/AkShare inputs; missing optional evidence is disclosed."""
import json
import math
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
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


def ak_raw_history(code, day):
    """Run the common registry with a process deadline for each interface."""
    from services.akshare_sources import raw_security_sources
    from services.market_fallback import MarketSource, fetch_market_fallback, normalize_daily_prices
    from services.market_calendar import get_market_window, previous_trading_day
    from datetime import date
    from services.wind_source import wind_api_key, fetch_wind_daily_bars, wind_stock_code

    start = previous_trading_day(get_market_window("A股"), date.fromisoformat(day)).isoformat()

    class BoundedAkShare:
        def __getattr__(self, name):
            return lambda **kwargs: pd.DataFrame(ak_call(name, kwargs))

    sources = raw_security_sources(BoundedAkShare(), infer_tickflow_symbol(code), start, day, fund=code == ETF)
    if wind_api_key():
        sources.append(MarketSource("万得Wind", lambda: fetch_wind_daily_bars(
            "stock_data", wind_stock_code(code), start, day, aftype="2"), "wind"))
    return fetch_market_fallback(
        sources,
        normalize_daily_prices, date_column="date", price_column="close", target_date=day, allow_partial=False,
    )

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
            is_st="ST" in r["name"].upper(),asof=day,eligibility_source="当日BK1158成分名称",
            halted=r.get("halted"),halt_source=r.get("halt_source")) for r in rows],
        raw_snapshot=rows,quotes={},events=[],events_complete=True,
        events_source="本策略口径：不考虑分红送转",skip_issues=True,warnings=[],index_history=history)

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
            result.update(skip_issues=True,warnings=[],events=[],
                          events_complete=True,
                          events_source="本策略口径：不考虑分红送转")
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
        for code, original in getattr(self,"original_quotes",{}).items():
            if original.get("date")==day and original.get("metadata"):
                quotes[code]=dict(quotes.get(code,original),metadata=original["metadata"])
        if not self.allow_fetch:
            result["quotes"]=quotes
            result["events"]=[]
            result["events_complete"]=True
            result["events_source"]="本策略口径：不考虑分红送转"
            result["warnings"].append("缓存预检：缺项不联网")
            return result
        from tickflow import TickFlow
        key=os.environ.get("TICKFLOW_API_KEY","")
        client=TickFlow(api_key=key,timeout=12,max_retries=0) if key else TickFlow.free()
        from services.microcap_rotation_engine import execution_targets
        result["quotes"]=quotes
        selected=set(self.required)
        for plan in getattr(self,"selection_plans",[]):
            selected.update(execution_targets(plan,result))
        needed=sorted(selected)
        metadata={}
        snapshots={}
        limited=Event()
        def tickflow_call(call, *args, **kwargs):
            if limited.is_set():
                raise DataGap("TickFlow本轮已限流，使用备用源")
            try:
                return call(*args, **kwargs)
            except Exception as exc:
                if type(exc).__name__ == "RateLimitError":
                    limited.set()
                raise
        constituent_names={str(r.get("code")):str(r.get("name", "")) for r in result.get("constituents", [])}
        if needed and day==now().date().isoformat():
            try:
                metadata={r["symbol"]:r for r in tickflow_call(client.instruments.get,[infer_tickflow_symbol(c) for c in needed])}
            except Exception:
                result["warnings"].append("证券元数据未取得；有价格的成交标注为资格未完全核验")
        if needed and day==now().date().isoformat():
            for offset in range(0,len(needed),5):
                try:
                    snapshots.update({r["symbol"]:r for r in tickflow_call(client.quotes.get,symbols=[infer_tickflow_symbol(c) for c in needed[offset:offset+5]])})
                except Exception:
                    if limited.is_set():
                        break
        def snapshot_values(symbol):
            try:
                snapshot=snapshots[symbol]
                stamp=pd.Timestamp(snapshot["timestamp"],unit="ms",tz="UTC").tz_convert("Asia/Shanghai")
                price=float(snapshot["last_price"]); volume=float(snapshot["volume"])
                if (stamp.strftime("%Y-%m-%d")==day and stamp.hour>=15
                        and math.isfinite(price) and price>0 and math.isfinite(volume) and volume>=0):
                    return price,volume
            except (KeyError,TypeError,ValueError,OverflowError):
                pass
            return None,None
        def get_quote(code):
            symbol=infer_tickflow_symbol(code)
            old=quotes.get(code)
            if old and old.get("formal") and old.get("close") and old.get("date")==day \
                    and all(type(old.get(k)) is bool for k in ("halted", "limit_up", "limit_down", "eligible")):
                return code,old
            close=volume=previous_close=None
            errors=[]
            source="TickFlow未复权日线"
            if symbol in snapshots:
                close,volume=snapshot_values(symbol)
                if close is not None:
                    source="TickFlow当日15点后收盘快照"
            try:
                if close is None:
                    frame=tickflow_call(client.klines.get,symbol,period="1d",count=8,adjust="none",as_dataframe=True)
                    dates=pd.to_datetime(frame.trade_date).dt.strftime("%Y-%m-%d")
                    day_frame=frame.loc[dates==day]
                    prior_frame=frame.loc[dates<day].sort_values("trade_date")
                    if len(prior_frame):
                        previous_close=float(prior_frame.close.iloc[-1])
                    if len(day_frame)==1:
                        close=float(day_frame.close.iloc[0])
                        volume=float(day_frame.volume.iloc[0]) if "volume" in day_frame else None
            except Exception as exc:
                errors.append("TickFlow日线："+type(exc).__name__)
            if close is None and symbol not in snapshots and day==now().date().isoformat():
                try:
                    single=tickflow_call(client.quotes.get,symbols=[symbol])
                    for item in single:
                        if item.get("symbol")==symbol:
                            snapshots[symbol]=item
                except Exception as exc:
                    errors.append("TickFlow收盘快照："+type(exc).__name__)
            if close is None and symbol in snapshots:
                close,volume=snapshot_values(symbol)
                if close is not None:
                    source="TickFlow当日15点后收盘快照"
            meta=metadata.get(symbol,{}) or (old.get("metadata",{}) if old and old.get("date")==day else {})
            ext=meta.get("ext") or {}
            if close is None or (not previous_close and not (ext.get("limit_up") and ext.get("limit_down"))):
                try:
                    data=ak_raw_history(code,day)
                    prior=data.loc[data["date"].dt.strftime("%Y-%m-%d").lt(day)].sort_values("date")
                    if not prior.empty:
                        previous_close=float(prior.iloc[-1]["close"])
                    matched=data.loc[data["date"].dt.strftime("%Y-%m-%d").eq(day)].to_dict("records")
                    if close is None and len(matched)==1:
                        close=float(matched[0]["close"])
                        volume=float(matched[0]["volume"]) if "volume" in matched[0] else None
                        source=data.attrs["market_data_source"]+"未复权日线"
                except Exception as exc:
                    errors.append("AkShare日线："+type(exc).__name__)
            if close is not None and (not math.isfinite(close) or close<=0):
                close=None
            if volume is not None and not math.isfinite(volume):
                volume=None
            # Original metadata is dated with the saved quote, never today's limits.
            up,down=ext.get("limit_up"),ext.get("limit_down")
            halted=None if volume is None else volume==0
            limit_source="TickFlow同日证券元数据" if up and down else ""
            if close is not None and previous_close and not (up and down):
                # Fallback for a metadata outage: compare the formal close with
                # the board limit rounded to the instrument tick size.
                from decimal import Decimal, ROUND_HALF_UP
                ratio=Decimal("0.20") if code.startswith(("300","301","688","689")) else Decimal("0.10")
                tick=Decimal("0.001") if code==ETF else Decimal("0.01")
                prev=Decimal(str(previous_close)); last=Decimal(str(close))
                expected_up=(prev*(Decimal("1")+ratio)/tick).quantize(Decimal("1"),rounding=ROUND_HALF_UP)*tick
                expected_down=(prev*(Decimal("1")-ratio)/tick).quantize(Decimal("1"),rounding=ROUND_HALF_UP)*tick
                up=abs(last-expected_up) <= tick/2
                down=abs(last-expected_down) <= tick/2
                limit_source="前收盘与板块涨跌幅规则推算"
            name=str(meta.get("name") or constituent_names.get(code) or "")
            if meta.get("name"):
                eligible="ST" not in name.upper()
                eligibility_source="TickFlow同日证券元数据"
            elif close is not None and volume is not None and volume>0:
                eligible=(code==ETF or "ST" not in name.upper())
                eligibility_source="正式收盘、成交量及当日非ST成分快照推断"
            else:
                eligible=None
                eligibility_source="未核验"
            note=""
            if limit_source and limit_source!="TickFlow同日证券元数据":
                note="涨跌停状态由前收盘与板块规则推算"
            if eligibility_source!="TickFlow同日证券元数据":
                note=(note+"；" if note else "")+"交易资格由正式成交量和非ST成分快照推断"
            return code,dict(date=day,close=close,adjustment="none",formal=close is not None,source=source,
                halted=halted,halt_source="当日日线成交量为零（保守跳过）" if volume==0 else "",
                limit_up=up if isinstance(up,bool) else close>=float(up)-1e-8 if close is not None and up else None,
                limit_down=down if isinstance(down,bool) else close<=float(down)+1e-8 if close is not None and down else None,
                eligible=eligible,eligibility_source=eligibility_source,
                qualification_note=note,min_buy=200 if code.startswith(("688","689")) else 100,
                metadata=meta,volume=volume,source_errors=errors)
        with ThreadPoolExecutor(max_workers=2) as pool:
            quotes.update(dict(pool.map(get_quote,needed)))
        client.close()
        result["quotes"]=quotes
        result["events"]=[]
        result["events_complete"]=True
        result["events_source"]="本策略口径：不考虑分红送转"
        folder.mkdir(parents=True,exist_ok=True)
        temp=path.with_suffix(".tmp")
        temp.write_text(dumps(result),encoding="utf-8")
        temp.replace(path)
        return result
