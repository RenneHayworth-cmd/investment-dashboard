from datetime import date

import pandas as pd

from services import live_price_history
from services.live_categories import summarize_live_pnl_by_category
from services.live_statement_import import (
    commit_statement_import,
    preview_statement_import,
)

HB_TRADE_HEADER = [
    "成交日期", "成交时间", "证券代码", "证券名称", "操作", "成交数量", "成交编号", "成交均价", "成交金额",
    "余额", "股票余额", "发生金额", "手续费", "印花税", "其他杂费", "合同编号", "市场名称",
]
HB_CASH_HEADER = ["成交日期", "摘要", "操作", "资金余额", "发生金额", "本次金额", "币种", "成交时间"]
YH_HEADER = [
    "证券代码", "证券名称", "操作", "成交数量", "成交均价", "成交金额", "股票余额", "发生金额", "手续费", "印花税",
    "其他杂费", "资金余额", "合同编号", "交收日期", "证券中文全称", "佣金", "过户费", "清算费(B股)", " 币种",
]


def _tsv(header, rows) -> bytes:
    lines = ["\t".join(header) + "\t"] + ["\t".join(str(value) for value in row) + "\t" for row in rows]
    return ("\r\n".join(lines) + "\r\n").encode("gb18030")


def _hb_trade(day, time, code, name, op, qty, deal, price, gross, net, fee, contract, balance=0):
    return [day, time, code, name, op, qty, deal, price, gross, 0, balance, net, fee, 0, 0, contract, "深圳Ａ股"]


HB_TRADES = [
    # 交割单按时间倒序导出
    _hb_trade("20260305", "00:00:00", "123282", "震裕转02", "托管转入", 10, "", 0, 0, 0, 0, 0, 10),
    _hb_trade("20260304", "00:00:00", "161125", "标普500LOF", "转托转入", 4, "", 0, 11.6, 0, 0, 0, 4),
    _hb_trade("20260303", "00:00:00", "370953", "震裕发债", "缴中签款", 10, "", 100, 1000, -1000, 0, 0, 10),
    _hb_trade("20260303", "15:25:00", "204001", "GC001", "回购融券", 30, "r2", 1.0, 3000, -3000.03, 0.03, "R2"),
    _hb_trade("20260303", "00:00:00", "204001", "GC001", "逆回购购回", 50, "r1", 1.5, 5000.80, 5000.80, 0, "R1"),
    _hb_trade("20260302", "15:25:00", "204001", "GC001", "回购融券", 50, "r1", 1.5, 5000, -5000.05, 0.05, "R1"),
    _hb_trade("20260302", "00:00:00", "160723", "嘉实原油LOF", "基金申购", 0, "失败", 1.4, 0, 0, 0, "F1"),
    _hb_trade("20260302", "00:00:00", "501018", "南方原油", "调帐转入", 80, "", 0, 0, 0, 0, 0, 80),
    _hb_trade("20260302", "00:00:00", "501018", "南方原油", "基金申购", 80, "", 1.2, 96.00, -96.10, 0, "S1"),
    _hb_trade("20260302", "00:00:00", "161125", "标普500LOF", "基金申购", 3, "", 2.9, 8.70, -8.71, 0, "L1"),
    _hb_trade("20260302", "14:50:00", "159501", "纳指ETF", "买入", 1000, "b1", 2.0, 2000, -2000.12, 0.12, "B1"),
]
HB_CASH_ROWS = [
    # 资金明细按时间倒序，余额为该行之后的余额
    ["20260320", "123282兑息扣税:本金0.00元,利息10.00元", "债券兑息扣税", 3943.79, -2.0, 3943.79, "人民币", "20:03:13"],
    ["20260320", "A123456789领123282兑息10张*1.0000", "债券兑息", 3945.79, 10.0, 3945.79, "人民币", "20:03:13"],
    ["20260318", "159501红利到帐", "股票分红", 3935.79, 50.0, 3935.79, "人民币", "19:00:00"],
    ["20260303", "申购中签款(370953) 10股*100.000", "发行中签扣款", 3885.79, -1000.0, 3885.79, "人民币", "18:49:01"],
    ["20260303", "融券", "逆回购融券", 4885.79, -3000.03, 4885.79, "人民币", "18:48:00"],
    ["20260303", "购回", "逆回购融券购回", 7885.82, 5000.80, 7885.82, "人民币", "18:48:00"],
    ["20260302", "融券", "逆回购融券", 2885.02, -5000.05, 2885.02, "人民币", "18:48:00"],
    ["20260302", "161125基金申购委托清算预扣款", "基金申购划出", 7885.07, -10.0, 7885.07, "人民币", "18:40:00"],
    ["20260302", "南方原油申购", "基金申购划出", 7895.07, -96.10, 7895.07, "人民币", "18:40:00"],
    ["20260302", "上日LOF基金申购161125确认金额", "基金申购划出", 7991.17, -8.71, 7991.17, "人民币", "18:40:00"],
    ["20260302", "买纳指ETF", "证券买入清算", 7999.88, -2000.12, 7999.88, "人民币", "18:40:00"],
    ["20260302", "本地:中行存管转帐转入", "银行转证券", 10000.0, 10000.0, 10000.0, "人民币", "09:00:00"],
]


def _yh(code, name, op, qty, price, gross, net, fee, balance, contract, day):
    return [code, name, op, qty, price, gross, 0, net, fee, 0, 0, balance, contract, day, name, fee, 0, 0, "人民币"]


YH_ROWS = [
    _yh("", "", "银行转证券", 0, 0, 0, 50000, 0, 50000, "", "20260302"),
    _yh("511360", "短融ETF", "证券买入", 100, 113.0, 11300, -11300, 0, 38700, "C1", "20260302"),
    _yh("132001", "金自来", "报价融券回购", 100, 100, 10000, -10000, 0, 28700, "Q1", "20260302"),
    _yh("600000", "示例股份", "证券买入", 1000, 10.0, 10000, -10005, 5, 18695, "M1", "20260303"),
    _yh("132001", "金自来", "报价融券购回", 100, 100.01, 10001, 10000.9, 0.1, 28695.9, "Q1", "20260304"),
    _yh("", "", "利息归本", 0, 0, 0, 1.2, 0, 28697.1, "", "20260320"),
    _yh("600000", "示例股份", "红利入账", 1000, 0, 25, 25, 0, 28722.1, "", "20260320"),
]


def _files():
    return [
        ("table华宝.xls", _tsv(HB_TRADE_HEADER, HB_TRADES)),
        ("table.xls", _tsv(HB_CASH_HEADER, HB_CASH_ROWS)),
        ("table银河.xls", _tsv(YH_HEADER, YH_ROWS)),
    ]


def test_huabao_import_covers_trades_lof_bonds_repo_and_reconciles_cash():
    preview = preview_statement_import(_files())
    assert preview["ok"], preview["errors"]
    huabao = next(item for item in preview["imports"] if item.broker == "hb")
    trades = {(trade["symbol"], trade["notes"] or "成交"): trade for trade in huabao.trades}

    assert trades[("159501", "成交")]["fee"] == 0.12
    assert trades[("161125", "LOF场内申购")]["quantity"] == 3
    assert trades[("501018", "LOF场内申购")]["quantity"] == 80  # 调帐转入不重复计
    assert not any(trade["symbol"] == "160723" for trade in huabao.trades)  # 0 份申购跳过
    prepaid = next(trade for trade in huabao.trades if "预扣款" in trade["notes"])
    assert (prepaid["symbol"], prepaid["quantity"], prepaid["trade_date"]) == ("161125", 4, "2026-03-02")
    bond = next(trade for trade in huabao.trades if trade["notes"].startswith("可转债中签"))
    assert (bond["symbol"], bond["quantity"], bond["price"], bond["trade_date"]) == ("123282", 10, 100.0, "2026-03-03")

    repo = {flow["flow_date"]: flow for flow in huabao.flows if "逆回购" in flow["notes"]}
    assert repo["2026-03-03"]["amount"] == round(0.75 - 0.03, 2)
    assert huabao.open_repo_principal == 3000.0
    assert huabao.broker_end_cash == 3943.79
    coupons = {flow["symbol"] for flow in huabao.flows if "兑息" in flow["notes"]}
    assert coupons == {"123282"}
    assert abs(huabao.cash_difference()) < 0.005


def test_yinhe_import_skips_microcap_stocks_and_pairs_quoted_repo():
    preview = preview_statement_import(_files())
    yinhe = next(item for item in preview["imports"] if item.broker == "yh")

    assert [trade["symbol"] for trade in yinhe.trades] == ["511360"]
    assert yinhe.skipped_stock_rows == 2
    assert {flow["notes"] for flow in yinhe.flows} >= {"银河银行转证券", "银河利息归本"}
    repo = next(flow for flow in yinhe.flows if "金自来" in flow["notes"])
    assert repo["amount"] == 0.9
    assert abs(yinhe.cash_difference()) < 0.005


def test_huabao_requires_both_files():
    preview = preview_statement_import(_files()[:1])

    assert not preview["ok"]
    assert any("同时上传" in error for error in preview["errors"])


def test_commit_takes_over_matching_manual_records_and_reimports_idempotently(tmp_path, monkeypatch):
    monkeypatch.setattr("core.db.DB_PATH", tmp_path / "cache.db")
    from core.db import init_db, get_conn

    init_db()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO live_trades (trade_date, trade_time, symbol, name, side, price, quantity, fee_rate_pct,"
            " strategy, notes, created_at) VALUES ('2026-03-02', NULL, '159501', '纳指ETF嘉实', '买入', 2.0, 1000,"
            " 0.006, 'MA25/2%', '手工记录', 'x')"
        )
        conn.execute(
            "INSERT INTO live_cash_flows (flow_date, entry_type, amount, notes, created_at)"
            " VALUES ('2026-03-05', '资金转出', 100.0, '划入其他账户', 'x')"
        )

    imports = preview_statement_import(_files())["imports"]
    first = commit_statement_import(imports)
    second = commit_statement_import(imports)

    with get_conn() as conn:
        trades = pd.read_sql_query("SELECT * FROM live_trades", conn)
        flows = pd.read_sql_query("SELECT * FROM live_cash_flows", conn)
    etf = trades[trades["symbol"].eq("159501")]
    assert first["taken_over_trades"] == 1
    assert second["replaced_statement_records"] == first["inserted_trades"] + first["inserted_flows"]
    assert len(etf) == 1
    assert etf.iloc[0]["strategy"] == "MA25/2%"
    assert etf.iloc[0]["name"] == "纳指ETF嘉实"
    assert flows["notes"].eq("划入其他账户").sum() == 1
    opening = flows[flows["entry_type"].eq("期初资金")]
    assert len(opening) == 1 and opening.iloc[0]["flow_date"] == "2026-03-02"


def test_price_windows_stop_at_the_closing_sale():
    trades = pd.DataFrame(
        [
            {"id": 1, "trade_date": "2026-03-02", "trade_time": None, "symbol": "161125", "side": "买入", "quantity": 3},
            {"id": 2, "trade_date": "2026-03-10", "trade_time": None, "symbol": "161125", "side": "卖出", "quantity": 3},
            {"id": 3, "trade_date": "2026-03-02", "trade_time": None, "symbol": "159501", "side": "买入", "quantity": 100},
        ]
    )

    windows = live_price_history.required_price_windows(trades, date(2026, 9, 30))

    assert windows["161125"] == (date(2026, 3, 2), date(2026, 3, 10))
    assert windows["159501"] == (date(2026, 3, 2), date(2026, 9, 30))


def test_bond_history_uses_face_value_before_listing(monkeypatch):
    listed = pd.DataFrame({"date": ["2026-03-09", "2026-03-10"], "price": [150.0, 152.0]})
    monkeypatch.setattr(live_price_history, "load_dataset", lambda *args, **kwargs: (listed, {}))

    history, error = live_price_history.load_bond_history(
        "123282",
        window=(date(2026, 3, 3), date(2026, 3, 10)),
        allow_fetch=False,
        save_to_cache=False,
        completed_date=date(2026, 3, 10),
    )

    assert error is None
    assert history["date"].tolist() == ["2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09", "2026-03-10"]
    assert history["price"].tolist()[:4] == [100.0] * 4


def test_category_summary_groups_symbols_and_income():
    history = pd.DataFrame(
        [
            {"symbol": "159501", "realized_pnl": 10.0, "unrealized_pnl": 5.0},
            {"symbol": "161125", "realized_pnl": 3.0, "unrealized_pnl": 0.0},
            {"symbol": "123282", "realized_pnl": 500.0, "unrealized_pnl": 0.0},
            {"symbol": "511360", "realized_pnl": 2.0, "unrealized_pnl": 1.0},
        ]
    )
    flows = pd.DataFrame(
        [
            {"entry_type": "利息", "amount": 4.0, "symbol": None, "notes": "逆回购净收益"},
            {"entry_type": "其他收入", "amount": 108.0, "symbol": "110081", "notes": "兑息"},
            {"entry_type": "其他支出", "amount": 21.6, "symbol": "110081", "notes": "扣税"},
            {"entry_type": "资金转入", "amount": 1000.0, "symbol": None, "notes": "转账"},
        ]
    )

    summary = summarize_live_pnl_by_category(history, flows).set_index("类别")

    assert summary.loc["ETF", "累计盈亏"] == 15.0
    assert summary.loc["LOF套利", "累计盈亏"] == 3.0
    assert abs(summary.loc["可转债", "累计盈亏"] - 586.4) < 1e-9
    assert summary.loc["现金管理", "累计盈亏"] == 7.0
    assert abs(summary.loc["合计", "累计盈亏"] - 611.4) < 1e-9
