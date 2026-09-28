import base64
import json
import unittest

import numpy as np
from streamlit.testing.v1 import AppTest


def _values(data):
    """Plotly 6 serialises numeric arrays as base64 typed arrays; decode them to a list."""
    if isinstance(data, dict) and "bdata" in data:
        return np.frombuffer(base64.b64decode(data["bdata"]), dtype=np.dtype(data["dtype"])).tolist()
    return list(data)


def _render_trend():
    import pandas as pd

    from components.futures_live.history import render_account_trend

    daily = pd.DataFrame({
        "date": ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21"],
        "net_pnl": [100.0, 50.0, 250.0, 250.0],
        "daily_pnl": [100.0, -50.0, 200.0, None],
        "economic_equity": [10100.0, 10050.0, 10250.0, 10250.0],
        "return_base": [10000.0, 10100.0, 10050.0, 10250.0],
        "daily_return_pct": [1.0, -0.4950495, 1.9900498, None],
        "status": ["完整", "完整", "手工估算", "数据不完整"],
        "missing_contracts": ["", "", "", "IM2610"],
        "source": ["", "", "", ""],
    })
    render_account_trend(daily, daily)


class FuturesAccountTrendTests(unittest.TestCase):
    def test_trend_uses_nav_line_and_red_green_daily_bars(self):
        from core.ui import DOWN_COLOR, PRIMARY_COLOR, UP_COLOR

        app = AppTest.from_function(_render_trend, default_timeout=30).run()
        self.assertEqual(list(app.exception), [])
        spec = json.loads(app.get("plotly_chart")[0].proto.spec)
        nav, bars = spec["data"]
        self.assertEqual(nav["name"], "账户净值")
        self.assertEqual(nav["line"]["color"], PRIMARY_COLOR)
        self.assertEqual(nav["x"], ["2026-09-16", "2026-09-17", "2026-09-18"])  # incomplete day excluded
        nav_values = _values(nav["y"])
        self.assertAlmostEqual(nav_values[0], 1.01)
        self.assertAlmostEqual(nav_values[-1], 1.01 * (1 - 0.004950495) * (1 + 0.019900498))
        self.assertEqual(_values(bars["y"]), [100.0, -50.0, 200.0])
        self.assertEqual(bars["type"], "bar")
        self.assertEqual(bars["yaxis"], "y2")
        self.assertEqual(bars["marker"]["color"], [UP_COLOR, DOWN_COLOR, UP_COLOR])
        self.assertIn("累计盯市净盈亏", bars["hovertemplate"])
        self.assertTrue(any("净值按下方收益日历" in caption.value for caption in app.caption))
        self.assertTrue(any("IM2610" in warning.value for warning in app.warning))


if __name__ == "__main__":
    unittest.main()
