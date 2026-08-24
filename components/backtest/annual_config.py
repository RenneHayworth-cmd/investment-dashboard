from pathlib import Path

from core.paths import OUTPUT_DIR


ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "config" / "annual_etf_registry_v1.csv"
WHITELIST_PATH = ROOT / "config" / "annual_etf_index_families.json"
RUNTIME_DIR = OUTPUT_DIR / "annual_etf_backtest"
AUDIT_MARKET_DIR = OUTPUT_DIR / "etf_portfolio_audit_20260727" / "market_data"

DIRECTION_LABELS = {
    "us_sp500": "标普500",
    "us_nasdaq": "纳斯达克100",
    "a_large": "A股核心宽基",
    "a_mid_small": "A股中小盘",
    "a_growth": "A股成长宽基",
    "smart_beta": "Smart Beta",
    "other_overseas": "非美国家或区域宽基",
    "gold": "黄金现货",
}
RESULT_TABLES = {
    "年度选择": "selections",
    "年度资格": "qualification",
    "参数遍历": "parameters",
    "实际交易": "trades",
    "实际迁移": "migrations",
    "方向贡献": "contribution",
    "年度收益": "yearly",
    "失败明细": "errors",
    "每日净值": "daily",
}


__all__ = [
    "AUDIT_MARKET_DIR",
    "DIRECTION_LABELS",
    "REGISTRY_PATH",
    "RESULT_TABLES",
    "ROOT",
    "RUNTIME_DIR",
    "WHITELIST_PATH",
]
