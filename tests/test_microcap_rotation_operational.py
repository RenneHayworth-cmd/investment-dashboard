import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from services.microcap_rotation_engine import initialize,execute,validate_batch
from services.microcap_rotation_store import Store
from services.microcap_rotation import update_simulation
from services.microcap_rotation_auto import AutomaticProvider
from tests.test_microcap_rotation import batch,history,Provider

class OperationalTests(unittest.TestCase):
    def state(self):
        return initialize("B",batch("2026-09-07"),history("2026-09-07"))
    def test_missing_one_price_does_not_veto_other_fills(self):
        b=batch("2026-09-08"); b["skip_issues"]=True; b["events_complete"]=False
        del b["quotes"]["600000"]
        validate_batch(b,b["date"],"2026-09-07")
        r=execute("B",self.state(),b,history(b["date"]))
        self.assertEqual(len(r["fills"]),19)
        self.assertEqual(len(r["failures"]),1)
        self.assertTrue(r["provisional"])
        self.assertNotIn("600000",r["positions"])
    def test_missing_held_price_is_reference_not_fake_fill(self):
        s=execute("B",self.state(),batch("2026-09-08"),history("2026-09-08"))
        b=batch("2026-09-09"); b["skip_issues"]=True
        del b["quotes"]["600000"]
        r=execute("B",s,b,history(b["date"]))
        self.assertTrue(r["positions"]["600000"]["stale"])
        self.assertIn("缺当日",r["positions"]["600000"]["valuation_note"])
        self.assertEqual(r["fills"],[])
    def test_unknown_qualification_is_disclosed(self):
        b=batch("2026-09-08"); b["skip_issues"]=True
        b["quotes"]["600000"]["limit_up"]=None
        r=execute("B",self.state(),b,history(b["date"]))
        self.assertEqual(len(r["fills"]),20)
        self.assertTrue(r["provisional"])
        fill=next(f for f in r["fills"] if f["code"]=="600000")
        self.assertIn("未核验",fill["qualification_note"])
    def test_confirmed_limit_still_skips(self):
        b=batch("2026-09-08"); b["skip_issues"]=True
        b["quotes"]["600000"]["limit_up"]=True
        r=execute("B",self.state(),b,history(b["date"]))
        self.assertNotIn("600000",r["positions"])
    def test_missing_snapshot_does_not_liquidate_retained_stocks(self):
        s=execute("B",self.state(),batch("2026-09-08"),history("2026-09-08"))
        for d in ["2026-09-09","2026-09-10","2026-09-11"]:
            b=batch(d); b["skip_issues"]=True; b["constituents"]=[]
            validate_batch(b,d,"2026-09-07")
            s=execute("B",s,b,history(d))
        self.assertEqual(s["plan"]["sells"],[])
        self.assertEqual(len(s["plan"]["targets"]),20)
    def test_launch_executes_today_from_previous_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/"test.db")
            store.enable("2026-09-21T08:00:00+08:00")
            b=batch("2026-09-18"); b["index_history"]=history(b["date"])
            states={s:initialize(s,b,history(b["date"])) for s in "ABCD"}
            store.save_launch(dict(start_date="2026-09-21",reference_date=b["date"],batch=b,states=states))
            with patch("services.microcap_rotation.settled",return_value="2026-09-18"):
                waiting=update_simulation(db_path=store.path,provider=Provider(),log_job=False)
                self.assertEqual(waiting["status"],"等待收盘")
                self.assertIsNone(waiting["last_complete"])
                self.assertEqual(store.days(),[])
            with patch("services.microcap_rotation.settled",return_value="2026-09-21"):
                update_simulation(db_path=store.path,provider=Provider(),log_job=False)
                rows=store.days()
                self.assertEqual({r["date"] for r in rows},{"2026-09-21"})
                self.assertEqual(len(rows),4)
                self.assertEqual(next(r for r in rows if r["strategy"]=="A")["equity"],"220000.00")
                self.assertEqual(len(next(r for r in rows if r["strategy"]=="B")["fills"]),20)
                update_simulation(db_path=store.path,provider=Provider(),log_job=False)
                self.assertEqual(store.days(),rows)
    def test_auto_cache_preflight_never_fetches(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=AutomaticProvider(Path(tmp)/"evidence",allow_fetch=False)
            with patch.object(p,"history",side_effect=history),patch("services.microcap_rotation_auto.raw_snapshot",return_value=[]),patch("services.microcap_rotation_auto.CacheProvider.batch",side_effect=ValueError("no evidence")),patch("services.microcap_rotation_auto.ak_call") as fetch:
                result=p.batch("2026-09-18")
                fetch.assert_not_called()
                self.assertEqual(result["quotes"],{})
                self.assertEqual(list(Path(tmp).iterdir()),[])

if __name__=="__main__": unittest.main()
