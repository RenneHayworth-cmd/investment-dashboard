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

    def test_remaining_missing_prices_report_gap_even_when_day_exists(self):
        result=update_simulation(db_path=self.store.path,provider=self.provider,refresh=True,log_job=False)
        self.assertEqual(result["status"],"待补数据")
        self.assertTrue(result["gaps"])

    def test_formal_price_repairs_valuation_without_hiding_unknown_limits(self):
        self.provider.changed["2026-09-21"]=copy.deepcopy(self.fresh)
        self.provider.changed["2026-09-21"]["quotes"]["600000"].update(limit_up=None,limit_down=None)
        result=update_simulation(db_path=self.store.path,provider=self.provider,refresh=True,log_job=False)
        self.assertIn("600000",result["price_repair"]["codes"])
        self.assertEqual(result["status"],"待补数据")
        row=next(r for r in self.store.days() if r["strategy"]=="B")
        self.assertTrue(row["provisional"])
        self.assertTrue(any("未核验" in w for w in row["warnings"]))
        self.assertFalse(row["positions"]["600000"].get("stale"))
        again=update_simulation(db_path=self.store.path,provider=self.provider,refresh=True,log_job=False)
        self.assertNotIn("price_repair",again)

class SourceRetryTests(unittest.TestCase):
    def test_close_snapshots_use_five_symbol_batches_without_daily_requests(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from unittest.mock import Mock
        from services.microcap_rotation_auto import AutomaticProvider
        client=Mock()
        symbols=[f"60000{i}.SH" for i in range(6)]
        client.instruments.get.return_value=[dict(symbol=s,name="股票",ext=dict(limit_up=11.,limit_down=9.)) for s in symbols]
        stamp=int(datetime(2026,9,21,15,1,tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()*1000)
        client.quotes.get.side_effect=lambda symbols: [dict(symbol=s,timestamp=stamp,last_price=10.,volume=1000) for s in symbols]
        with tempfile.TemporaryDirectory() as tmp:
            p=AutomaticProvider(Path(tmp)/"evidence",allow_fetch=True)
            p.required={s[:6] for s in symbols}
            with patch("tickflow.TickFlow",return_value=client),patch.dict("os.environ",{"TICKFLOW_API_KEY":"test-only"}),patch.object(p,"history",side_effect=history),patch("services.microcap_rotation_auto.CacheProvider.batch",side_effect=ValueError()),patch("services.microcap_rotation_auto.raw_snapshot",return_value=[]),patch("services.microcap_rotation_auto.now",return_value=datetime(2026,9,21,16,tzinfo=ZoneInfo("Asia/Shanghai"))):
                result=p.batch("2026-09-21")
        self.assertEqual([len(c.kwargs["symbols"]) for c in client.quotes.get.call_args_list],[5,1])
        client.klines.get.assert_not_called()
        self.assertTrue(all(q["formal"] and not q["limit_up"] for q in result["quotes"].values()))

    def historical_batch(self, client, data, originals=None, required=None):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from services.microcap_rotation_auto import AutomaticProvider
        with tempfile.TemporaryDirectory() as tmp:
            p=AutomaticProvider(Path(tmp)/"evidence",allow_fetch=True)
            p.required=required or {"688793"}
            p.original_quotes=originals or {}
            with patch("tickflow.TickFlow",return_value=client),patch.dict("os.environ",{"TICKFLOW_API_KEY":"test-only"}),patch.object(p,"history",side_effect=history),patch("services.microcap_rotation_auto.CacheProvider.batch",side_effect=ValueError()),patch("services.microcap_rotation_auto.raw_snapshot",return_value=[]),patch("services.microcap_rotation_auto.now",return_value=datetime(2026,10,2,16,tzinfo=ZoneInfo("Asia/Shanghai"))),patch("services.microcap_rotation_auto.ak_raw_history",return_value=data):
                return p.batch("2026-09-30")

    def test_backup_close_and_previous_close_determine_limits(self):
        import pandas as pd
        from unittest.mock import Mock
        client=Mock(); client.klines.get.side_effect=TimeoutError
        data=pd.DataFrame({"date":pd.to_datetime(["2026-09-29","2026-09-30"]),"close":[10.,12.],"volume":[100.,200.]})
        data.attrs["market_data_source"]="腾讯"
        q=self.historical_batch(client,data)["quotes"]["688793"]
        self.assertTrue(q["formal"])
        self.assertTrue(q["limit_up"])
        self.assertFalse(q["limit_down"])

    def test_original_session_limits_survive_historical_repair(self):
        import pandas as pd
        from unittest.mock import Mock
        client=Mock(); client.klines.get.side_effect=TimeoutError
        data=pd.DataFrame({"date":pd.to_datetime(["2026-09-30"]),"close":[18.32],"volume":[200.]})
        data.attrs["market_data_source"]="腾讯"
        original={"688793":dict(date="2026-09-30",metadata=dict(name="倍轻松",ext=dict(limit_up=21.54,limit_down=14.36)))}
        q=self.historical_batch(client,data,original)["quotes"]["688793"]
        self.assertTrue(q["formal"])
        self.assertFalse(q["limit_up"])
        self.assertFalse(q["limit_down"])
        self.assertEqual(q["metadata"],original["688793"]["metadata"])

    def test_rate_limit_stops_repeated_requests_and_uses_backups(self):
        import pandas as pd
        from unittest.mock import Mock
        client=Mock()
        client.klines.get.side_effect=type("RateLimitError",(Exception,),{})("limited")
        data=pd.DataFrame({"date":pd.to_datetime(["2026-09-29","2026-09-30"]),"close":[10.,10.2],"volume":[100.,200.]})
        data.attrs["market_data_source"]="腾讯"
        result=self.historical_batch(client,data,required={f"60000{i}" for i in range(6)})
        self.assertLessEqual(client.klines.get.call_count,2)
        self.assertTrue(all(q["formal"] and q["limit_up"] is False for q in result["quotes"].values()))

    def test_backup_requests_previous_session_and_reaches_wind(self):
        import pandas as pd
        from services.microcap_rotation_auto import ak_raw_history
        data=pd.DataFrame({"date":pd.to_datetime(["2026-09-29","2026-09-30"]),"close":[10.,10.2],"volume":[100.,200.]})
        with patch("services.akshare_sources.raw_security_sources",return_value=[]) as registry,patch("services.wind_source.wind_api_key",return_value="test-only"),patch("services.wind_source.fetch_wind_daily_bars",return_value=data) as wind:
            result=ak_raw_history("688793","2026-09-30")
            self.assertEqual(registry.call_args.args[2:4],("2026-09-29","2026-09-30"))
            self.assertEqual(wind.call_args.args,("stock_data","688793.SH","2026-09-29","2026-09-30"))
            self.assertEqual(wind.call_args.kwargs["aftype"],"2")
            self.assertEqual(result.attrs["source"],"wind")

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
