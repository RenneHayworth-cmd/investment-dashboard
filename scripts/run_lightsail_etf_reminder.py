"""Lightsail entry point: ETF only, short lived, never invoke Hermes."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    # Deployment invariant, independent of an accidentally enabled environment.
    os.environ["REMINDER_NODE"] = "lightsail"
    os.environ["ENABLE_WECHAT"] = "false"
    from services.alert_delivery import flag
    from scripts import monitor_position_timing_trades as monitor
    if flag("REMINDER_CHECK_NOW"):
        if not flag("REMINDER_DRY_RUN"):
            raise ValueError("REMINDER_CHECK_NOW 仅允许 dry-run")
        sys.argv.append("--force")
    try:
        return monitor.main()
    except Exception as exc:
        print(f"ETF提醒任务失败（{type(exc).__name__}），未输出数据源或凭证。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
