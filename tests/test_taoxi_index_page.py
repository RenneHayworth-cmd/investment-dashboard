from pathlib import Path
import json
import unittest

from streamlit.testing.v1 import AppTest


PAGE = Path(__file__).resolve().parents[1] / "pages" / "1_指数监控.py"


class TaoxiIndexPageTests(unittest.TestCase):
    def test_detail_uses_standard_index_layout_with_custom_extensions(self):
        app = AppTest.from_file(str(PAGE), default_timeout=30)
        app.session_state["selected_index_detail"] = "桃囍微盘"
        app.run()

        self.assertEqual([], list(app.exception))
        self.assertEqual(
            ["最新价格", "20日涨幅", "60日涨幅", "RSI(14)", "当前回撤", "最大回撤"],
            [item.label for item in app.metric],
        )
        self.assertEqual(["走势区间"], [item.label for item in app.segmented_control])
        labels = [item.label for item in app.tabs]
        self.assertEqual(["走势", "回撤", "摘要", "数据", "成分"], labels[:5])
        self.assertIn("当前生效20只", labels)
        self.assertIn("下一日待生效20只", labels)

        trend_figure = json.loads(app.get("plotly_chart")[0].proto.spec)
        self.assertEqual(
            ["收盘点位", "MA5", "MA10", "MA15", "MA20", "MA30", "MA60", "MA120", "MA250"],
            [trace["name"] for trace in trend_figure["data"]],
        )
        self.assertNotIn("shapes", trend_figure.get("layout", {}))

        data_table = next(table for table in app.dataframe if "输入哈希" in table.value.columns)
        self.assertEqual(["日期", "close", "日涨跌幅(%)", "open"], list(data_table.value.columns[:4]))
        column_config = json.loads(data_table.proto.columns)
        numeric_columns = [
            "close", "日涨跌幅(%)", "open", "MA5", "MA10", "MA15", "MA20", "MA30", "MA60", "MA120", "MA250",
            "偏离率(%)", "RSI(14)", "回撤(%)",
        ]
        self.assertTrue(all(column_config[column]["type_config"]["format"] == "%.2f" for column in numeric_columns))
        latest = data_table.value.iloc[0]
        self.assertEqual(float(latest["close"]), round(float(latest["close"]), 2))
        self.assertEqual(float(latest["open"]), round(float(latest["open"]), 2))


if __name__ == "__main__":
    unittest.main()
