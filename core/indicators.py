import pandas as pd


def add_ma20(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    result = result.sort_values("trade_date").reset_index(drop=True)
    result["close"] = pd.to_numeric(result["close"], errors="coerce")
    result["MA20"] = result["close"].rolling(20).mean()
    result["deviation_pct"] = (result["close"] - result["MA20"]) / result["MA20"] * 100
    return result

