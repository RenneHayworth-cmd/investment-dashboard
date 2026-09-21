import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from services.microcap_rotation import update_simulation
from services.microcap_rotation_engine import initialize
from services.microcap_rotation_store import Store
from tests.test_microcap_rotation import Provider,batch,history

class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/"repair.db")
        self.store.enable("2026-09-21T08:00:00+08:00")
        b=batch("2026-09-18")
        h=history(b["date"]); h[-1]["close"]=50.
        b["index_history"]=h
        self.store.save_launch(dict(start_date="2026-09-21",reference_date=b["date"],batch=b,
                                   states={s:initialize(s,b,h) for s in "ABCD"}))
        self.provider=Provider()
        self.provider.refresh=lambda *args:None
        self.fresh=batch("2026-09-21")
        self.fresh["skip_issues"]=True
        missing=copy.deepcopy(self.fresh)
        for code in ("600000","512890"):
            missing["quotes"][code].update(close=None,formal=False)
        self.provider.changed["2026-09-21"]=missing
        self.clock=patch("services.microcap_rotation.settled",return_value="2026-09-21")
        self.clock.start()
        update_simulation(db_path=self.store.path,provider=self.provider,log_job=False)
    def tearDown(self):
        self.clock.stop(); self.temp.cleanup()
    def test_late_prices_replay_and_archive_once(self):
        before=self.store.days()
        self.assertEqual(len(next(r for r in before if r["strategy"]=="B")["fills"]),19)
        self.assertEqual(len(next(r for r in before if r["strategy"]=="D")["fills"]),0)
        self.provider.changed["2026-09-21"]=self.fresh
        r=update_simulation(db_path=self.store.path,provider=self.provider,refresh=True,log_job=False)
        self.assertEqual(r["status"],"已补价重算")
        rows=self.store.days()
        self.assertEqual(len(next(r for r in rows if r["strategy"]=="B")["fills"]),20)
        d=next(r for r in rows if r["strategy"]=="D")
        self.assertEqual(len(d["fills"]),1)
        self.assertEqual(d["equity"],"219986.81")
        self.assertEqual(d["positions"]["512890"]["quantity"],219900)
        etf_fill=next(f for f in d["fills"] if f["code"]=="512890")
        self.assertEqual(etf_fill["fee"],"13.19")
        with self.store.connection() as c:
            archive=c.execute("SELECT payload FROM microcap_rotation_revisions").fetchall()
            self.assertEqual(len(archive),1)
            self.assertIn("等待补价",archive[0][0])
        update_simulation(db_path=self.store.path,provider=self.provider,refresh=True,log_job=False)
        self.assertEqual(rows,self.store.days())
    def test_revision_failure_rolls_back_original_day(self):
        before=self.store.days()
        old=self.store.batch("2026-09-21")
        self.provider.changed["2026-09-21"]=self.fresh
        with self.store.connection(True) as c:
            c.execute("CREATE TRIGGER fail_repair BEFORE INSERT ON microcap_rotation_daily WHEN NEW.strategy='C' BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(Exception):
            update_simulation(db_path=self.store.path,provider=self.provider,refresh=True,log_job=False)
        self.assertEqual(before,self.store.days())
        self.assertEqual(old,self.store.batch("2026-09-21"))

class SourceRetryTests(unittest.TestCase):
    def test_single_quote_recovers_failed_bulk_and_daily(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from unittest.mock import Mock
        from services.microcap_rotation_auto import AutomaticProvider
        client=Mock()
        client.instruments.get.return_value=[dict(symbol="600000.SH",name="股票",ext=dict(limit_up=11.,limit_down=9.))]
        client.klines.get.side_effect=TimeoutError
        stamp=int(datetime(2026,9,21,15,0,1,tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()*1000)
        client.quotes.get.side_effect=[TimeoutError(),[dict(symbol="600000.SH",timestamp=stamp,last_price=10.,volume=1000)]]
        with tempfile.TemporaryDirectory() as tmp:
            p=AutomaticProvider(Path(tmp)/"evidence",allow_fetch=True)
            p.required={"600000"}
            with patch("tickflow.TickFlow",return_value=client),patch.dict("os.environ",{"TICKFLOW_API_KEY":"test-only"}),patch.object(p,"history",side_effect=history),patch("services.microcap_rotation_auto.CacheProvider.batch",side_effect=ValueError()),patch("services.microcap_rotation_auto.raw_snapshot",return_value=[]),patch("services.microcap_rotation_auto.now",return_value=datetime(2026,9,21,16,tzinfo=ZoneInfo("Asia/Shanghai"))),patch("services.microcap_rotation_auto.ak_call") as ak:
                result=p.batch("2026-09-21")
                self.assertEqual(result["quotes"]["600000"]["close"],10.)
                self.assertTrue(result["quotes"]["600000"]["formal"])
                self.assertIn("收盘快照",result["quotes"]["600000"]["source"])
                ak.assert_not_called()

if __name__=="__main__": unittest.main()
