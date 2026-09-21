import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from services.microcap_rotation_policy import sessions,DataGap,next_day
from services.microcap_rotation_store import Store,process_lock,digest
from services.microcap_rotation_engine import initialize,execute,validate_batch,quote
from services.microcap_rotation import initialize_simulation,update_simulation,read_strategy_view
from services.microcap_rotation_research import import_research

START="2026-09-07"

def history(day):
    return [dict(date=d,close=100+i) for i,d in enumerate(sessions("2026-01-05",day))]

def batch(day):
    codes=[f"{600000+i:06}" for i in range(21)]
    q={code:dict(date=day,close=10.,adjustment="none",source="test",formal=True,halted=False,
                limit_up=False,limit_down=False,eligible=True,min_buy=100,eligibility_source="test")
       for code in codes+["512890"]}
    q["512890"]["close"]=1.
    return dict(date=day,index_code="BK1158",index_close=history(day)[-1]["close"],formal=True,
                source="test",retrieved_at=day+"T16:00:00+08:00",snapshot_date=day,
                snapshot_source="test",snapshot_retrieved_at=day+" 15:05:00",
                constituents=[dict(code=c,name=c,market_cap=i+1,is_st=False,asof=day,eligibility_source="test") for i,c in enumerate(codes)],
                quotes=q,events_complete=True,events_source="test",events=[])

class Provider:
    def __init__(self):
        self.changed={}
        self.calls=[]
    def history(self,day):
        return history(day)
    def batch(self,day):
        self.calls.append(day)
        value=self.changed.get(day,batch(day))
        if isinstance(value,Exception):
            raise value
        return copy.deepcopy(value)
    def refresh(self,*args):
        raise AssertionError("unexpected network")

class EngineTests(unittest.TestCase):
    def initial(self,s="B"):
        return initialize(s,batch(START),history(START))
    def test_next_close_fixed_size_and_fees(self):
        s=self.initial()
        self.assertEqual(s["positions"],{})
        self.assertEqual(s["plan"]["execution_date"],"2026-09-08")
        day="2026-09-08"
        r=execute("B",s,batch(day),history(day))
        self.assertEqual(len(r["fills"]),20)
        self.assertEqual(r["cash"],"19960.00")
        self.assertEqual(r["equity"],"219960.00")
        self.assertEqual(r["plan"]["buys"],[])
        self.assertAlmostEqual(r["cash_ratio"]+r["stock_ratio"],1.)
    def test_holidays_and_weekly(self):
        self.assertEqual(next_day("2026-06-18"),"2026-06-22")
        s=self.initial()
        for d in sessions("2026-09-08","2026-09-11"):
            s=execute("B",s,batch(d),history(d))
        self.assertEqual(s["plan"]["execution_date"],"2026-09-14")
        self.assertEqual(s["plan"]["reason"],"周度轮动")
    def test_limit_buy_cancelled_and_no_chase(self):
        day="2026-09-08"; b=batch(day); b["quotes"]["600000"]["limit_up"]=True
        r=execute("B",self.initial(),b,history(day))
        self.assertEqual(len(r["positions"]),19)
        self.assertEqual(len(r["failures"]),1)
        r=execute("B",r,batch("2026-09-09"),history("2026-09-09"))
        self.assertNotIn("600000",r["positions"])
    def test_halted_new_buy_cancelled(self):
        b=batch("2026-09-08")
        b["quotes"]["600000"].update(halted=True,halt_source="公告",close=None)
        r=execute("B",self.initial(),b,history(b["date"]))
        self.assertEqual(len(r["positions"]),19)
    def test_unknown_missing_price_blocks(self):
        b=batch("2026-09-08"); del b["quotes"]["600000"]
        with self.assertRaises(DataGap):
            execute("B",self.initial(),b,history(b["date"]))
    def test_partial_exit_etf_and_risk_reversal(self):
        s=self.initial("D")
        b=batch("2026-09-08")
        h=history(b["date"]); h[-1]["close"]=50
        s=execute("D",s,b,h)
        self.assertEqual(s["plan"]["mode"],"etf")
        b=batch("2026-09-09"); b["quotes"]["600000"]["limit_down"]=True
        r=execute("D",s,b,history(b["date"]))
        self.assertIn("600000",r["positions"])
        self.assertIn("512890",r["positions"])
        self.assertGreater(r["stock_ratio"],0)
        self.assertEqual(r["plan"]["mode"],"stock")
        self.assertNotIn("600000",r["plan"]["sells"])
        r=execute("D",r,batch("2026-09-10"),history("2026-09-10"))
        self.assertNotIn("512890",r["positions"])
        self.assertEqual(len(r["positions"]),20)
    def test_dividend_and_split(self):
        s=execute("B",self.initial(),batch("2026-09-08"),history("2026-09-08"))
        b=batch("2026-09-09")
        b["events"]=[dict(id="cash1",kind="cash",code="600000",date=b["date"],record_date="2026-09-08",source="test",cash_per_share=.1),
                     dict(id="split1",kind="shares",code="600000",date=b["date"],record_date="2026-09-08",source="test",new_shares_per_share=.5)]
        validate_batch(b,b["date"],START)
        r=execute("B",s,b,history(b["date"]),{"cash1":1000,"split1":1000})
        self.assertEqual(r["cash"],"20060.00")
        self.assertEqual(r["positions"]["600000"]["quantity"],1500)
        self.assertEqual(r["equity"],"225060.00")
    def test_cash_insufficient_and_minimum_lot(self):
        s=self.initial(); s["cash"]="500.00"
        r=execute("B",s,batch("2026-09-08"),history("2026-09-08"))
        self.assertEqual(len(r["fills"]),0)
        self.assertEqual(len(r["failures"]),20)
        b=batch("2026-09-08")
        b["quotes"]["688000"]=dict(b["quotes"]["600000"])
        with self.assertRaises(DataGap):
            quote(b,"688000")
    def test_gate_dates_units_qualifications(self):
        for key,value in [("date","2026-09-06"),("formal",False),("events_complete",False),("snapshot_retrieved_at","2026-09-07 14:59:00")]:
            b=batch(START); b[key]=value
            with self.subTest(key=key),self.assertRaises(DataGap):
                validate_batch(b,START,START)
        b=batch(START); b["constituents"][0]["is_st"]=None
        with self.assertRaises(DataGap): validate_batch(b,START,START)
    def test_unknown_regime_does_not_guess(self):
        h=history(START)
        for row in h: row["close"]=100.
        s=initialize("D",batch(START),h)
        self.assertIsNone(s["risk"])
        self.assertEqual(s["plan"]["mode"],"cash")

class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Path(self.temp.name)/"test.db"
        self.provider=Provider()
        initialize_simulation(self.db,START+"T08:00:00+08:00")
    def tearDown(self):
        self.temp.cleanup()
    def run_to(self,date,**kwargs):
        return update_simulation(date,self.db,self.provider,log_job=False,**kwargs)
    def test_idempotence_and_preflight(self):
        self.run_to("2026-09-08")
        before=self.db.read_bytes()
        self.run_to("2026-09-08")
        self.assertEqual(len(Store(self.db).days()),8)
        self.run_to("2026-09-09",preflight=True)
        self.assertEqual(self.db.read_bytes(),before)
        self.assertEqual(len(Store(self.db).accounts()),4)
        initialize_simulation(self.db)
        self.assertEqual(len(Store(self.db).accounts()),4)
    def test_missing_day_stops_continuity_and_recovers(self):
        self.run_to(START)
        self.provider.changed["2026-09-08"]=DataGap("缺价格")
        r=self.run_to("2026-09-10")
        self.assertEqual(r["last_complete"],START)
        self.assertNotIn("2026-09-09",self.provider.calls)
        del self.provider.changed["2026-09-08"]
        self.run_to("2026-09-10")
        self.assertEqual(len(Store(self.db).days()),16)
    def test_transaction_abort_and_retry(self):
        store=Store(self.db)
        with store.connection(True) as c:
            c.execute("CREATE TRIGGER fail_day BEFORE INSERT ON microcap_rotation_daily WHEN NEW.strategy='C' BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(Exception): self.run_to(START)
        self.assertEqual(store.days(),[])
        self.assertIsNone(store.batch(START))
        with store.connection(True) as c: c.execute("DROP TRIGGER fail_day")
        self.run_to(START)
        self.assertEqual(len(store.days()),4)
    def test_lock_and_frozen_input_integrity(self):
        with process_lock(self.db):
            with self.assertRaises(RuntimeError): self.run_to(START)
        self.run_to(START)
        with Store(self.db).connection(True) as c:
            c.execute("UPDATE microcap_rotation_inputs SET hash='bad'")
        with self.assertRaises(ValueError): Store(self.db).batch(START)
    def test_no_live_trade_tables(self):
        self.run_to("2026-09-08")
        with Store(self.db).connection() as c:
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='live_trades'").fetchone())
    def test_read_empty_no_mutation(self):
        empty=Path(self.temp.name)/"empty.db"
        read_strategy_view(empty)
        self.assertFalse(empty.exists())

class AdapterTests(unittest.TestCase):
    def test_snapshot_frozen_before_gate_and_cache_replacement(self):
        import pandas as pd
        import json
        from services.microcap_rotation_data import CacheProvider
        from services.microcap_rotation_store import dumps
        rows=batch(START)["constituents"]
        frame=pd.DataFrame([{"快照日期":START,"快照时间":START+" 15:05:00","代码":r["code"],
                             "名称":r["name"],"总市值(亿元)":r["market_cap"]} for r in rows])
        with tempfile.TemporaryDirectory() as temp:
            provider=CacheProvider(Path(temp)/"evidence")
            with patch("services.microcap.load_microcap_constituent_snapshots",return_value=(frame,None)),patch.object(provider,"history",side_effect=history):
                provider.refresh(START)
                with self.assertRaises(DataGap): provider.batch(START)
                frozen=json.loads((Path(temp)/"microcap_rotation_snapshots"/(START+".json")).read_text())
                proof=dict(date=START,source="test",snapshot_source="test",snapshot_hash=frozen["hash"],
                           constituent_eligibility={r["code"]:dict(is_st=False,asof=START,source="test") for r in rows},
                           quotes=batch(START)["quotes"],events_complete=True,events_source="test",events=[])
                provider.evidence_dir.mkdir()
                path=provider.evidence_dir/(START+".json")
                path.write_text(dumps(proof))
                original=provider.batch(START)
                frame.loc[0,"总市值(亿元)"]=9999
                provider.refresh(START)
                self.assertEqual(provider.batch(START)["constituents"],original["constituents"])
                with patch("services.microcap.load_microcap_constituent_snapshots",return_value=(None,None)):
                    self.assertEqual(provider.batch(START)["constituents"],original["constituents"])
                proof["snapshot_hash"]="wrong"; path.write_text(dumps(proof))
                with self.assertRaises(DataGap): provider.batch(START)

class HistoricalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        value=os.environ.get("MICROCAP_RESEARCH_PACKAGE")
        if not value: raise unittest.SkipTest("设置MICROCAP_RESEARCH_PACKAGE运行真实交接包核验")
        cls.root=Path(value)
        cls.report=import_research(cls.root)
    def test_72_cells_and_corrected_cash(self):
        r=self.report
        self.assertEqual(len(r["comparison"]),18)
        self.assertEqual(len(r["differences"]),1)
        self.assertAlmostEqual(r["metrics"]["C"]["avg_cash_pct"],55.22191020380384)
        self.assertEqual(r["metrics"]["D"]["end_equity"],278180.5)
        self.assertEqual(r["metrics"]["D"]["end_equity"]-r["metrics"]["C"]["end_equity"],5580.5)
        with self.assertRaises(ValueError): import_research(self.root,strict_summary=True)
    def test_hash_and_units_tamper(self):
        import shutil
        from services.microcap_rotation_research import INPUTS
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in INPUTS:
                (root/name).parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(self.root/name,root/name)
            path=root/"01_核心对比与审计总表/daily_nav_abcd.csv"
            import pandas as pd
            frame=pd.read_csv(path); frame["D_cash_pos_pct"]/=100
            frame.to_csv(path,index=False)
            with self.assertRaises(ValueError): import_research(root,expected=self.report)
            with self.assertRaises(ValueError): import_research(root)
    def test_new_summary_error_is_rejected(self):
        import shutil
        import pandas as pd
        from services.microcap_rotation_research import INPUTS
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in INPUTS:
                (root/name).parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(self.root/name,root/name)
            path=root/"01_核心对比与审计总表/comparison_summary_abcd.csv"
            f=pd.read_csv(path)
            f.iloc[0,2]="999,999.00 元"
            f.to_csv(path,index=False)
            with self.assertRaises(ValueError): import_research(root)

    def test_archive_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/"test.db")
            store.save_research(self.report)
            self.assertEqual(store.research()["metrics"]["D"]["end_equity"],278180.5)

if __name__=="__main__": unittest.main()
