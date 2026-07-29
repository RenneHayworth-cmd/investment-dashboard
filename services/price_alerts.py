from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
from typing import Callable


SERVERCHAN_SENDKEY_ENV = "SERVERCHAN_SENDKEY"
SERVERCHAN_SENDKEY_FILE = Path.home() / ".config" / "investment_dashboard" / "serverchan_sendkey"


@dataclass
class PriceAlertState:
    below_threshold: bool = False
    last_price: float | None = None
    last_contract: str = ""
    last_check_at: str = ""
    last_alert_at: str = ""


@dataclass(frozen=True)
class PriceAlertResult:
    status: str
    message: str
    notified: bool
    below_threshold: bool


def load_serverchan_sendkey(path: Path = SERVERCHAN_SENDKEY_FILE) -> str:
    sendkey = str(os.environ.get(SERVERCHAN_SENDKEY_ENV) or "").strip()
    if sendkey:
        return sendkey
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def serverchan_endpoint(sendkey: str) -> str:
    normalized = str(sendkey).strip()
    if not normalized:
        raise ValueError("Server酱 SendKey 不能为空。")
    if normalized.startswith("sctp"):
        matched = re.match(r"^sctp(\d+)t", normalized)
        if matched is None:
            raise ValueError("Server酱³ SendKey 格式不正确。")
        return f"https://{matched.group(1)}.push.ft07.com/send/{normalized}.send"
    return f"https://sctapi.ftqq.com/{normalized}.send"


def send_serverchan_message(
    sendkey: str,
    title: str,
    description: str = "",
    *,
    timeout: float = 10,
) -> dict:
    import requests

    normalized_title = str(title).replace("\r", " ").replace("\n", " ").strip()
    if not normalized_title:
        raise ValueError("Server酱消息标题不能为空。")
    response = requests.post(
        serverchan_endpoint(sendkey),
        json={"title": normalized_title, "desp": str(description)},
        headers={"Content-Type": "application/json;charset=utf-8"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    code = payload.get("code")
    if str(code) != "0":
        message = str(payload.get("message") or payload.get("data") or "未知错误")
        raise RuntimeError(f"Server酱推送失败：{message}")
    return payload


def load_price_alert_state(path: Path) -> PriceAlertState:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return PriceAlertState()
    return PriceAlertState(
        below_threshold=bool(payload.get("below_threshold", False)),
        last_price=_optional_float(payload.get("last_price")),
        last_contract=str(payload.get("last_contract") or ""),
        last_check_at=str(payload.get("last_check_at") or ""),
        last_alert_at=str(payload.get("last_alert_at") or ""),
    )


def save_price_alert_state(path: Path, state: PriceAlertState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def process_price_alert(
    *,
    price: float,
    threshold: float,
    contract: str,
    checked_at: datetime,
    state_path: Path,
    notify: Callable[[str, str], object],
) -> PriceAlertResult:
    numeric_price = float(price)
    numeric_threshold = float(threshold)
    state = load_price_alert_state(state_path)
    is_below = numeric_price < numeric_threshold
    checked_text = checked_at.isoformat(timespec="seconds")

    if is_below and not state.below_threshold:
        title = f"铁矿石主连跌破 {numeric_threshold:g} 元/吨"
        description = (
            f"- 当前价格：**{numeric_price:.1f} 元/吨**\n"
            f"- 监控阈值：{numeric_threshold:.1f} 元/吨\n"
            f"- 当前合约：{contract or 'I0'}\n"
            f"- 行情时间：{checked_at:%Y-%m-%d %H:%M:%S}\n\n"
            "价格来自指数监控使用的铁矿石主连实时行情。"
        )
        notify(title, description)
        state.last_alert_at = checked_text
        status = "alerted"
        message = f"已推送：{contract or 'I0'} {numeric_price:.1f} < {numeric_threshold:.1f}"
        notified = True
    elif is_below:
        status = "below_suppressed"
        message = f"仍低于阈值，已抑制重复通知：{numeric_price:.1f}"
        notified = False
    elif state.below_threshold:
        status = "rearmed"
        message = f"价格已回到阈值上方，重新布防：{numeric_price:.1f}"
        notified = False
    else:
        status = "normal"
        message = f"价格未触发：{numeric_price:.1f}"
        notified = False

    state.below_threshold = is_below
    state.last_price = numeric_price
    state.last_contract = str(contract or "I0")
    state.last_check_at = checked_text
    save_price_alert_state(state_path, state)
    return PriceAlertResult(status, message, notified, is_below)


def _optional_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
