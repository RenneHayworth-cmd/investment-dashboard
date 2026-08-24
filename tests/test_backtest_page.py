from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest


PAGE = Path(__file__).parents[1] / "pages" / "3_策略回测.py"
NETWORK_CALLS = (
    "components.backtest.ma_timing.fetch_tickflow_fund_close",
    "components.backtest.portfolio_timing.fetch_tickflow_fund_close",
    "components.backtest.fund_rotation.fetch_tickflow_fund_close",
    "components.backtest.fund_rotation.fetch_eastmoney_fund_nav",
    "components.annual_etf_dynamic.fetch_annual_etf_raw_history",
    "components.annual_etf_dynamic.fetch_annual_dividends",
)


def _assert_clean(app: AppTest) -> None:
    assert not app.exception, [str(item.value) for item in app.exception]


class StrategyBacktestPageTests(unittest.TestCase):
    def test_default_render_is_network_free(self):
        with ExitStack() as stack:
            stack.enter_context(patch("core.db.init_db"))
            mocks = [
                stack.enter_context(
                    patch(
                        target,
                        side_effect=AssertionError(f"默认渲染不应联网：{target}"),
                    )
                )
                for target in NETWORK_CALLS
            ]
            app = AppTest.from_file(str(PAGE), default_timeout=20).run()

        _assert_clean(app)
        self.assertEqual(app.radio[0].value, "单标的MA20择时")
        self.assertTrue(any("默认用 512890" in item.value for item in app.info))
        for mock in mocks:
            mock.assert_not_called()

    def test_all_modes_render_without_implicit_network(self):
        with ExitStack() as stack:
            stack.enter_context(patch("core.db.init_db"))
            mocks = [
                stack.enter_context(
                    patch(
                        target,
                        side_effect=AssertionError(f"切换模式不应联网：{target}"),
                    )
                )
                for target in NETWORK_CALLS
            ]
            app = AppTest.from_file(str(PAGE), default_timeout=20).run()
            for mode in ("多ETF配置择时", "年度动态组合", "多基金动量轮动"):
                app.radio[0].set_value(mode)
                app.run()
                _assert_clean(app)

        for mock in mocks:
            mock.assert_not_called()

    def test_annual_mode_keeps_preflight_confirmation_and_run_sequence(self):
        preflight = SimpleNamespace(
            qualification=pd.DataFrame(
                columns=["year", "direction", "qualified"]
            ),
            errors=pd.DataFrame(),
        )
        with ExitStack() as stack:
            stack.enter_context(patch("core.db.init_db"))
            network_mocks = [
                stack.enter_context(
                    patch(
                        target,
                        side_effect=AssertionError(f"仅预检不应联网：{target}"),
                    )
                )
                for target in NETWORK_CALLS
            ]
            stack.enter_context(
                patch(
                    "components.annual_etf_dynamic._load_market_bundle",
                    return_value=({}, pd.DataFrame(), pd.DataFrame(), "测试缓存"),
                )
            )
            stack.enter_context(
                patch(
                    "components.annual_etf_dynamic._load_proxy_data",
                    return_value=({}, pd.DataFrame()),
                )
            )
            stack.enter_context(
                patch(
                    "components.annual_etf_dynamic.preflight_annual_candidates",
                    return_value=preflight,
                )
            )

            app = AppTest.from_file(str(PAGE), default_timeout=20).run()
            app.radio[0].set_value("年度动态组合")
            app.run()
            next(button for button in app.button if button.label.startswith("1.")).click()
            app.run()
            _assert_clean(app)

            network_button = next(
                button for button in app.button if button.label.startswith("2.")
            )
            run_button = next(
                button for button in app.button if button.label.startswith("3.")
            )
            self.assertTrue(network_button.disabled)
            self.assertTrue(run_button.disabled)
            confirmation = next(
                item
                for item in app.checkbox
                if item.label.startswith("我确认本次操作可以联网")
            )
            confirmation.set_value(True)
            app.run()
            _assert_clean(app)
            network_button = next(
                button for button in app.button if button.label.startswith("2.")
            )
            self.assertFalse(network_button.disabled)

        for mock in network_mocks:
            mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
