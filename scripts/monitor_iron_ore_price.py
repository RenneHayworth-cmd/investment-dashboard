from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import logging
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.index_realtime import (  # noqa: E402
    _fetch_futures_quote,
    _futures_market_is_open,
    fetch_futures_main_contract_names,
    load_futures_main_contract_names,
)
from services.price_alerts import (  # noqa: E402
    process_price_alert,
    send_hermes_weixin_message,
)


INDEX_NAME = "铁矿石主连"
SYMBOL = "I0"
DEFAULT_THRESHOLD = 730.0
STATE_PATH = ROOT / "output" / "alerts" / "iron_ore_below_730.json"
LOCK_PATH = ROOT / "output" / "alerts" / "iron_ore_below_730.lock"
LOG_PATH = ROOT / "output" / "logs" / "iron_ore_price_alert.log"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="监控铁矿石主连价格并通过Hermes推送微信通知。")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="告警阈值，默认730。")
    parser.add_argument("--force", action="store_true", help="忽略交易时段限制，供手动检查使用。")
    parser.add_argument("--dry-run", action="store_true", help="不发送通知，也不修改告警状态。")
    parser.add_argument("--test-price", type=float, help="使用指定价格代替联网行情，供测试使用。")
    parser.add_argument("--test-notification", action="store_true", help="立即发送一条Hermes微信测试通知。")
    return parser.parse_args()


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )


@contextmanager
def single_instance_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def resolve_contract(quote: dict[str, object], now: datetime) -> str:
    cached = load_futures_main_contract_names()
    contract = str(cached.get(INDEX_NAME) or "").strip().upper()
    if contract and now.minute % 30 != 0:
        return contract
    try:
        refreshed = fetch_futures_main_contract_names({INDEX_NAME: quote})
    except Exception:
        refreshed = {}
    return str(refreshed.get(INDEX_NAME) or contract or SYMBOL).strip().upper()


def main() -> int:
    args = parse_args()
    configure_logging()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))

    if args.test_notification:
        send_hermes_weixin_message(
            "铁矿石价格监控测试",
            f"Hermes微信通知通道配置成功。\n\n测试时间：{now:%Y-%m-%d %H:%M:%S}",
        )
        print("成功：Hermes微信测试通知已发送。")
        return 0

    if not args.force and args.test_price is None and not _futures_market_is_open(SYMBOL, now=now):
        print("跳过：当前不在铁矿石期货交易时段。")
        return 0

    with single_instance_lock() as acquired:
        if not acquired:
            print("跳过：上一轮铁矿石价格检查尚未完成。")
            return 0
        if args.test_price is None:
            quote = _fetch_futures_quote(INDEX_NAME, SYMBOL)
            if quote is None:
                print("失败：铁矿石主连实时行情获取失败。")
                return 1
            price = float(quote["price"])
            quote_time = quote.get("quote_time")
            checked_at = quote_time if isinstance(quote_time, datetime) else now
            contract = resolve_contract(quote, now)
        else:
            price = float(args.test_price)
            checked_at = now
            contract = SYMBOL

        if args.dry_run:
            relation = "低于" if price < args.threshold else "未低于"
            print(f"试运行：{contract} {price:.1f}，{relation} {args.threshold:.1f}，未发送通知。")
            return 0

        is_simulation = args.test_price is not None

        def notify(title: str, description: str):
            if is_simulation:
                title = f"【模拟告警】{title}"
                description = description.replace(
                    "价格来自指数监控使用的铁矿石主连实时行情。",
                    "这是通过监控脚本发送的模拟测试；价格为用户指定值，并非实时行情。",
                )
            return send_hermes_weixin_message(title, description)

        if is_simulation:
            with TemporaryDirectory(prefix="iron_ore_alert_test_") as directory:
                result = process_price_alert(
                    price=price,
                    threshold=args.threshold,
                    contract=contract,
                    checked_at=checked_at,
                    state_path=Path(directory) / "state.json",
                    notify=notify,
                )
        else:
            result = process_price_alert(
                price=price,
                threshold=args.threshold,
                contract=contract,
                checked_at=checked_at,
                state_path=STATE_PATH,
                notify=notify,
            )
        logging.info("status=%s price=%.1f contract=%s", result.status, price, contract)
        print(result.message)
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        configure_logging()
        # Request exceptions can contain the credential-bearing URL, so log only the type.
        logging.error("iron_ore_alert_failed error_type=%s", type(exc).__name__)
        print(f"失败：铁矿石价格监控执行异常（{type(exc).__name__}）。")
        raise SystemExit(1) from None
