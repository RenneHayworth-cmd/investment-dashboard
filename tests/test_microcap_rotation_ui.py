import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from services.microcap_rotation import initialize_simulation,update_simulation,read_strategy_view
from services.microcap_rotation_store import Store
from tests.test_microcap_rotation import START,Provider

APP="from components.microcap_rotation import render_rotation\nrender_rotation()"

class RotationUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Path(self.temp.name)/"ui.db"
    def tearDown(self):
        self.temp.cleanup()
    def test_empty_cache_does_not_enable_or_fetch(self):
        view=read_strategy_view(self.db)
        with patch("services.microcap_rotation.read_strategy_view",return_value=view),patch("services.microcap_rotation.initialize_simulation") as enable,patch("services.microcap_rotation.update_simulation") as update:
            app=AppTest.from_string(APP).run()
            self.assertFalse(app.exception)
            self.assertIn("启用每日模拟",[b.label for b in app.button])
            enable.assert_not_called(); update.assert_not_called()
        self.assertFalse(self.db.exists())
    def test_cards_detail_back_and_stale_state(self):
        initialize_simulation(self.db,START+"T08:00:00+08:00")
        update_simulation("2026-09-08",self.db,Provider(),log_job=False)
        view=read_strategy_view(self.db)
        with patch("services.microcap_rotation.read_strategy_view",return_value=view),patch("services.microcap_rotation.update_simulation") as update:
            app=AppTest.from_string(APP).run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.metric),4)
            self.assertTrue(app.warning)
            app.button[1].click().run()
            self.assertFalse(app.exception)
            self.assertIn("B ·",app.subheader[1].value)
            self.assertEqual(len(app.tabs),8)
            app.button[0].click().run()
            self.assertEqual(len(app.metric),4)
            update.assert_not_called()

    def test_cached_research_and_operations_render(self):
        import pandas as pd
        from services.microcap_rotation_research import import_research
        import os
        root=os.environ.get("MICROCAP_RESEARCH_PACKAGE")
        if not root:
            self.skipTest("需真实交接包路径")
        view=read_strategy_view(self.db)
        view["research"]=import_research(root)
        for mode in ("历史研究","数据与运行"):
            with self.subTest(mode=mode),patch("services.microcap_rotation.read_strategy_view",return_value=view),patch("streamlit.segmented_control",return_value=mode),patch("core.db.list_jobs",return_value=pd.DataFrame()),patch("services.microcap_rotation.update_simulation") as update:
                app=AppTest.from_string(APP).run()
                self.assertFalse(app.exception)
                self.assertFalse(app.error)
                update.assert_not_called()

if __name__=="__main__": unittest.main()
