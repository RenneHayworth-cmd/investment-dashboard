# Investment Dashboard

个人投资分析工作台。

## 本地运行

```powershell
cd "\\wsl.localhost\Ubuntu\home\renne\investment_dashboard"
py -m streamlit run app.py
```

## 数据缓存设计

- `data/raw/`: 原始行情 CSV
- `data/processed/`: 计算后的结果 CSV
- `output/`: 手动导出的文件
- `cache.db`: SQLite 元数据和任务记录

