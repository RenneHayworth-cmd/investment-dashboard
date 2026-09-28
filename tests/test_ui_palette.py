import inspect
import re
import tomllib
import unittest
from pathlib import Path

import pandas as pd

from core import ui
from core.ui import (
    DOWN_COLOR,
    DOWN_TEXT_COLOR,
    FLAT_COLOR,
    PRIMARY_COLOR,
    UP_COLOR,
    UP_TEXT_COLOR,
    pnl_color,
)

ROOT = Path(__file__).resolve().parents[1]


class PnlColorTests(unittest.TestCase):
    def test_sign_maps_to_red_up_green_down(self):
        self.assertEqual(pnl_color(1.5), UP_COLOR)
        self.assertEqual(pnl_color(-0.01), DOWN_COLOR)
        self.assertEqual(pnl_color(0), FLAT_COLOR)
        self.assertEqual(pnl_color(None), FLAT_COLOR)
        self.assertEqual(pnl_color(pd.NA), FLAT_COLOR)
        self.assertEqual(pnl_color("abc"), FLAT_COLOR)

    def test_custom_palette(self):
        self.assertEqual(pnl_color(2, up=UP_TEXT_COLOR, down=DOWN_TEXT_COLOR, flat="x"), UP_TEXT_COLOR)
        self.assertEqual(pnl_color(-2, up=UP_TEXT_COLOR, down=DOWN_TEXT_COLOR, flat="x"), DOWN_TEXT_COLOR)
        self.assertEqual(pnl_color(0, flat="x"), "x")


class PaletteConsistencyTests(unittest.TestCase):
    def test_primary_colour_is_not_the_rise_colour(self):
        self.assertNotIn(PRIMARY_COLOR.lower(), {UP_COLOR.lower(), DOWN_COLOR.lower()})
        css = inspect.getsource(ui.apply_global_style)
        self.assertIn(f"--ui-primary: {PRIMARY_COLOR};", css)

    def test_local_streamlit_config_matches_primary_colour(self):
        # .streamlit/ is gitignored, so the file only exists on machines that configured it.
        path = ROOT / ".streamlit" / "config.toml"
        if not path.exists():
            self.skipTest(".streamlit/config.toml not present (gitignored local config)")
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(config["theme"]["primaryColor"].lower(), PRIMARY_COLOR.lower())

    def test_css_variables_match_python_text_colours(self):
        # Strip comments the way a browser does (first "*/" closes the comment). A stray "*/"
        # inside a comment once turned the rest into an invalid declaration that swallowed --ui-up.
        css = re.sub(r"/\*.*?\*/", "", inspect.getsource(ui.apply_global_style), flags=re.S)
        self.assertNotIn("*/", css)
        root_block = re.search(r":root\s*\{(.*?)\}", css, flags=re.S).group(1)
        declarations = [part.strip() for part in root_block.split(";") if part.strip()]
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                self.assertRegex(declaration, r"^--[a-z0-9-]+:\s*\S")
        self.assertIn(f"--ui-up: {UP_TEXT_COLOR}", declarations)
        self.assertIn(f"--ui-down: {DOWN_TEXT_COLOR}", declarations)

    def test_migrated_files_have_no_hard_coded_rise_fall_colours(self):
        literal = re.compile(r"190,\s*18,\s*60|22,\s*101,\s*52|254,\s*226,\s*226|220,\s*252,\s*231|#ef4444|#22c55e|#166534")
        migrated = [
            "components/position/cards_tables.py", "components/position_table.py",
            "components/live_record/tables.py", "components/live_record/account.py",
            "components/live_record/dashboard.py", "components/microcap_live/dashboard.py",
            "components/position/performance.py", "core/return_calendar.py",
        ]
        for name in migrated:
            with self.subTest(file=name):
                self.assertIsNone(literal.search((ROOT / name).read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
