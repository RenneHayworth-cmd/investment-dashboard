from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.fund_analysis import FUND_ADJUST_FORWARD_ADDITIVE  # noqa: E402
from services.market_calendar import get_market_window, is_market_trading_day  # noqa: E402
from services import position_analysis as position  # noqa: E402
from services.price_alerts import (  # noqa: E402
    SERVERCHAN_SENDKEY_FILE,
    load_serverchan_sendkey,
    send_hermes_weixin_message,
    send_serverchan_message,
)


ALERT_SLOTS = ((9, 45), (11, 45), (14, 45), (14, 50), (14, 54))
STATE_PATH = ROOT / "output" / "alerts" / "position_timing_trade_alert.json"
LOCK_PATH = ROOT / "output" / "alerts" / "position_timing_trade_alert.lock"
LOG_PATH = ROOT / "output" / "logs" / "position_timing_trade_alert.log"


@dataclass
class PositionTimingNotificationState:
    trade_date: str = ""
    notified_slots: list[str] = field(default_factory=list)
    no_action_at_1450: bool = False
    last_outcome: str = ""
    last_notification_at: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="在A股盘中按固定50万元ETF均线策略发送可执行交易通知。"
    )
    parser.add_argument("--force", action="store_true", help="忽略通知时刻限制，供手工检查使用。")
    parser.add_argument("--dry-run", action="store_true", help="计算并打印结果，不发送通知或修改状态。")
    parser.add_argument(
        "--test-notification",
        action="store_true",
        help="立即通过Server酱和Hermes微信发送一条测试通知。",
    )
    return parser.parse_args()


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )


def alert_slot(now: datetime) -> str | None:
    return now.strftime("%H:%M") if (now.hour, now.minute) in ALERT_SLOTS else None


def load_notification_state(path: Path = STATE_PATH) -> PositionTimingNotificationState:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return PositionTimingNotificationState()
    return PositionTimingNotificationState(
        trade_date=str(payload.get("trade_date") or ""),
        notified_slots=[str(value) for value in payload.get("notified_slots") or []],
        no_action_at_1450=bool(payload.get("no_action_at_1450", False)),
        last_outcome=str(payload.get("last_outcome") or ""),
        last_notification_at=str(payload.get("last_notification_at") or ""),
    )


def save_notification_state(
    state: PositionTimingNotificationState,
    path: Path = STATE_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def should_suppress_notification(
    state: PositionTimingNotificationState,
    *,
    outcome: str,
    slot: str,
) -> bool:
    """After the 14:50 no-action notice, suppress only later no-action messages."""
    return (
        outcome == "no_action"
        and state.no_action_at_1450
        and slot > "14:50"
    )


def send_notification_channels(
    sendkey: str,
    title: str,
    description: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Send through both WeChat paths without one failure blocking the other."""
    sent_channels: list[str] = []
    errors: list[str] = []

    if sendkey:
        try:
            send_serverchan_message(sendkey, title, description)
            sent_channels.append("Server酱")
        except Exception as exc:
            errors.append(f"Server酱推送失败（{type(exc).__name__}）")
    else:
        errors.append(f"Server酱未配置SendKey（{SERVERCHAN_SENDKEY_FILE}）")

    try:
        send_hermes_weixin_message(title, description)
        sent_channels.append("Hermes微信")
    except Exception as exc:
        errors.append(f"Hermes微信推送失败（{type(exc).__name__}）")

    if not sent_channels:
        raise RuntimeError("；".join(errors))
    return tuple(sent_channels), tuple(errors)


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


def _load_formal_items(*, api_key: str, market_now: datetime):
    required_codes = list(position.ETF_PORTFOLIO_WEIGHTS_PCT) + [
        position.POSITION_TIMING_PARKING_SYMBOL
    ]

    def load_one(code: str):
        return position.load_or_fetch_etf(
            code,
            api_key=api_key,
            count=5000,
            adjust=FUND_ADJUST_FORWARD_ADDITIVE,
            allow_fetch=True,
            force_refresh=False,
            save_to_cache=True,
            allow_unfinished_session=False,
            market_now=market_now,
        )

    item_by_code = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(load_one, code): code for code in required_codes}
        for future in as_completed(futures):
            code = futures[future]
            try:
                item_by_code[code] = future.result()
            except Exception as exc:
                item_by_code[code] = position.PositionItem(
                    "ETF",
                    code,
                    position.ETF_DISPLAY_NAMES.get(code, code),
                    "失败",
                    error=f"{type(exc).__name__}: {exc}",
                    formal_history_valid=False,
                )
    return [item_by_code[code] for code in required_codes]


def format_notification(preview, *, slot: str) -> tuple[str, str, str]:
    common = [
        f"- 策略基准：固定50万元ETF均线策略",
        f"- 正式持仓基准日：{preview.formal_date or '-'}",
        f"- 盘中预判日：{preview.preview_date or '-'}",
        f"- 行情时间：{preview.quote_time or '-'}",
    ]
    if preview.errors:
        title = f"ETF均线策略提醒失败 {slot}"
        description = "\n".join(
            ["无法生成可靠的交易数量，本轮不发送买卖建议。", "", *common, "", "### 原因"]
            + [f"- {error}" for error in preview.errors]
        )
        return title, description, "error"

    if preview.actions.empty:
        first_slot = slot == "14:50"
        title = (
            "ETF均线策略：今日无需操作"
            if first_slot
            else f"ETF均线策略：{slot}当前无需操作"
        )
        description = "\n".join(
            [
                f"{slot}盘中行情下，目标持仓与上一正式交易日策略持仓一致。",
                "",
                *common,
                "",
                (
                    "14:54仍会继续检查行情；若仍无需操作，不再重复通知。"
                    if first_slot
                    else "后续时点仍会继续检查行情；若信号变化，将发送最新交易建议。"
                ),
            ]
        )
        return title, description, "no_action"

    lines = ["### 交易建议（先卖后买）"]
    for row in preview.actions.itertuples(index=False):
        lines.append(
            f"- **{row.操作}** {row.代码} {row.基金名称}："
            f"**{int(row.数量):,}股**，参考价 {float(row.参考价):.4f}，"
            f"预计金额 {float(row.预计金额):,.2f}元"
        )
        if str(row.原因 or "").strip():
            lines.append(f"  - 原因：{row.原因}")
    title = f"ETF均线策略交易提醒 {slot}"
    description = "\n".join(
        [
            "以下数量为盘中行情下的目标持仓净变化，已合并各策略袖套对同一ETF的买卖。",
            "",
            *common,
            "",
            *lines,
            "",
            "这是盘中行情下的策略预判；价格仍可能变化，请以当次提醒和实际成交为准。",
        ]
    )
    return title, description, "action"


def main() -> int:
    args = parse_args()
    configure_logging()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    sendkey = load_serverchan_sendkey()

    if args.test_notification:
        sent_channels, delivery_errors = send_notification_channels(
            sendkey,
            "ETF均线策略交易提醒测试",
            (
                "这是均线择时定时脚本发送的双通道测试消息。\n\n"
                f"测试时间：{now:%Y-%m-%d %H:%M:%S}"
            ),
        )
        if delivery_errors:
            logging.warning("notification_partial_failure errors=%s", " | ".join(delivery_errors))
        print(f"成功：ETF均线策略测试通知已发送（{'、'.join(sent_channels)}）。")
        if delivery_errors:
            print(f"警告：{'；'.join(delivery_errors)}")
        return 0

    market = get_market_window("A股")
    if market is None or not is_market_trading_day(market, now):
        print("跳过：当前不是A股交易日。")
        return 0

    slot = alert_slot(now)
    if slot is None and not args.force:
        print("跳过：当前不在09:45、11:45、14:45、14:50、14:54通知时刻。")
        return 0
    slot = slot or now.strftime("%H:%M")
    trade_date = now.date().isoformat()
    state = load_notification_state()
    if state.trade_date != trade_date:
        state = PositionTimingNotificationState(trade_date=trade_date)
    if slot in state.notified_slots and not args.force:
        print(f"跳过：{slot}通知已经发送。")
        return 0
    api_key = str(os.environ.get("TICKFLOW_API_KEY") or "").strip()
    with single_instance_lock() as acquired:
        if not acquired:
            print("跳过：上一轮ETF均线策略提醒尚未完成。")
            return 0

        if not api_key:
            preview = position.PositionTimingTradePreviewResult(
                preview_date=trade_date,
                errors=["计划任务环境未加载 TICKFLOW_API_KEY。"],
            )
        else:
            try:
                items = _load_formal_items(api_key=api_key, market_now=now)
                required_codes = list(position.ETF_PORTFOLIO_WEIGHTS_PCT) + [
                    position.POSITION_TIMING_PARKING_SYMBOL
                ]
                quotes = position.fetch_tickflow_etf_quotes(
                    required_codes,
                    api_key=api_key,
                    market_now=now,
                )
                preview = position.build_position_timing_trade_preview(
                    items,
                    quotes,
                    market_now=now,
                )
            except Exception as exc:
                preview = position.PositionTimingTradePreviewResult(
                    preview_date=trade_date,
                    errors=[f"行情或正式缓存加载失败（{type(exc).__name__}）。"],
                )

        title, description, outcome = format_notification(preview, slot=slot)
        if args.dry_run:
            print(f"试运行：{title}\n{description}")
            return 0

        if should_suppress_notification(state, outcome=outcome, slot=slot):
            state.notified_slots = sorted(set(state.notified_slots + [slot]))
            save_notification_state(state)
            print(f"跳过：{slot}仍无需操作，已抑制重复通知。")
            return 0

        sent_channels, delivery_errors = send_notification_channels(
            sendkey,
            title,
            description,
        )
        state.notified_slots = sorted(set(state.notified_slots + [slot]))
        if outcome == "no_action" and slot == "14:50":
            state.no_action_at_1450 = True
        state.last_outcome = outcome
        state.last_notification_at = now.isoformat(timespec="seconds")
        save_notification_state(state)
        logging.info(
            "status=%s slot=%s action_count=%d channels=%s",
            outcome,
            slot,
            len(preview.actions),
            ",".join(sent_channels),
        )
        if delivery_errors:
            logging.warning("notification_partial_failure errors=%s", " | ".join(delivery_errors))
        print(f"成功：{title}（已发送：{'、'.join(sent_channels)}）")
        if delivery_errors:
            print(f"警告：{'；'.join(delivery_errors)}")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        configure_logging()
        logging.error("position_timing_alert_failed error_type=%s", type(exc).__name__)
        print(f"失败：ETF均线策略提醒执行异常（{type(exc).__name__}）。")
        raise SystemExit(1) from None
