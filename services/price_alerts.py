from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Callable

from services.alert_delivery import channel_enabled, DeliveryRejected, DeliveryUncertain


SERVERCHAN_SENDKEY_ENV = "SERVERCHAN_SENDKEY"
SERVERCHAN_SENDKEY_FILE = Path.home() / ".config" / "investment_dashboard" / "serverchan_sendkey"
HERMES_SEND_BIN_ENV = "HERMES_SEND_BIN"
DEFAULT_HERMES_SEND_BIN = Path.home() / ".local" / "bin" / "hermes"


@dataclass
class PriceAlertState:
    below_threshold: bool = False
    last_price: float | None = None
    last_contract: str = ""
    last_check_at: str = ""
    last_alert_at: str = ""
    # Lowest ladder level already alerted (e.g. 695.0); None when no alert is active.
    alert_level: float | None = None


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
    if not channel_enabled("fangtang"):
        return {"skipped": True, "reason": "方糖渠道已禁用"}
    import requests

    normalized_title = str(title).replace("\r", " ").replace("\n", " ").strip()
    if not normalized_title:
        raise DeliveryRejected("Server酱消息标题不能为空。")
    try:
        endpoint = serverchan_endpoint(sendkey)
    except ValueError:
        raise DeliveryRejected("Server酱 SendKey 缺失或格式无效") from None
    response = requests.post(
        endpoint,
        json={"title": normalized_title, "desp": str(description)},
        headers={"Content-Type": "application/json;charset=utf-8"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("code") is None:
        raise DeliveryUncertain("Server酱未返回有效的送达确认")
    code = payload.get("code")
    if str(code) != "0":
        raise DeliveryRejected("Server酱 API 明确拒绝发送")
    return payload


WXPUSHER_ENDPOINT = "https://wxpusher.zjiecode.com/api/send/message"
# 极简推送: an SPT identifies the recipient itself, so no appToken/UID is involved.
WXPUSHER_SPT_ENDPOINT = "https://wxpusher.zjiecode.com/api/send/message/simple-push"
WXPUSHER_CONFIG_DIR = Path.home() / ".config" / "investment_dashboard"
WXPUSHER_OK = 1000
WXPUSHER_MAX_CONTENT_CHARS = 40_000
WXPUSHER_MAX_CONTENT_BYTES = 65_535
WXPUSHER_MAX_SUMMARY_CHARS = 100
WXPUSHER_MAX_UIDS = 2000
WXPUSHER_MAX_TOPICS = 5


@dataclass(frozen=True)
class WxPusherConfig:
    app_token: str = ""  # an appToken (AT_...) or a simple-push token (SPT_...)
    uids: tuple[str, ...] = ()
    topic_ids: tuple[int, ...] = ()

    @property
    def is_spt(self) -> bool:
        return self.app_token.startswith("SPT_")


def _wxpusher_value(env_name: str, filename: str, directory: Path) -> str:
    value = str(os.environ.get(env_name) or "").strip()
    if value:
        return value
    try:
        return (directory / filename).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_wxpusher_config(directory: Path | None = None) -> WxPusherConfig:
    """appToken plus recipients from env (preferred) or ~/.config files; never logged."""
    directory = directory or WXPUSHER_CONFIG_DIR
    token = _wxpusher_value("WXPUSHER_APP_TOKEN", "wxpusher_app_token", directory)
    uids = tuple(
        item for item in re.split(r"[,\s]+", _wxpusher_value("WXPUSHER_UIDS", "wxpusher_uids", directory)) if item
    )
    topics = []
    for item in re.split(r"[,\s]+", _wxpusher_value("WXPUSHER_TOPIC_IDS", "wxpusher_topic_ids", directory)):
        if not item:
            continue
        if not item.isdigit():
            raise DeliveryRejected("WXPUSHER_TOPIC_IDS 必须是数字，多个用逗号分隔")
        topics.append(int(item))
    return WxPusherConfig(token, uids, tuple(topics))


def send_wxpusher_message(
    config: WxPusherConfig,
    title: str,
    description: str = "",
    *,
    timeout: float = 10,
) -> dict:
    """POST one markdown message; accepted only when every recipient's task was created.

    WxPusher has no title field: the summary is the notification preview and the
    body carries the title as a heading. code 1000 means the send task was created.
    """
    if not channel_enabled("wxpusher"):
        return {"skipped": True, "reason": "WxPusher渠道已禁用"}
    import requests

    normalized_title = str(title).replace("\r", " ").replace("\n", " ").strip()
    if not normalized_title:
        raise DeliveryRejected("WxPusher消息标题不能为空。")
    if not config.app_token:
        raise DeliveryRejected("WXPUSHER_APP_TOKEN 未配置")
    if not config.is_spt:
        if not config.uids and not config.topic_ids:
            raise DeliveryRejected("WXPUSHER_UIDS 或 WXPUSHER_TOPIC_IDS 至少配置一项")
        if len(config.uids) > WXPUSHER_MAX_UIDS or len(config.topic_ids) > WXPUSHER_MAX_TOPICS:
            raise DeliveryRejected("WxPusher收件人数量超过单次上限")
    body = str(description).strip()
    content = f"## {normalized_title}\n\n{body}" if body else normalized_title
    if len(content) > WXPUSHER_MAX_CONTENT_CHARS or len(content.encode("utf-8")) > WXPUSHER_MAX_CONTENT_BYTES:
        raise DeliveryRejected("WxPusher消息超过长度上限")

    request_body: dict = {
        "content": content,
        "summary": normalized_title[:WXPUSHER_MAX_SUMMARY_CHARS],
        "contentType": 3,
    }
    if config.is_spt:
        request_body["spt"] = config.app_token
    else:
        request_body["appToken"] = config.app_token
        if config.uids:
            request_body["uids"] = list(config.uids)
        if config.topic_ids:
            request_body["topicIds"] = list(config.topic_ids)
    response = requests.post(
        WXPUSHER_SPT_ENDPOINT if config.is_spt else WXPUSHER_ENDPOINT,
        json=request_body,
        headers={"Content-Type": "application/json;charset=utf-8"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("code") is None:
        raise DeliveryUncertain("WxPusher未返回有效的送达确认")
    if str(payload.get("code")) != str(WXPUSHER_OK):
        raise DeliveryRejected("WxPusher API 明确拒绝发送")
    results = payload.get("data")
    if not isinstance(results, list) or not results:
        raise DeliveryUncertain("WxPusher未返回发送任务明细")
    accepted = [isinstance(item, dict) and str(item.get("code")) == str(WXPUSHER_OK) for item in results]
    if not any(accepted):
        raise DeliveryRejected("WxPusher拒绝了所有收件人")
    if not all(accepted):
        # Some recipients queued, some did not: a blind retry would duplicate.
        raise DeliveryUncertain("WxPusher部分收件人发送失败，需核对后处理")
    return payload


def send_hermes_weixin_message(
    title: str,
    description: str = "",
    *,
    timeout: float = 60,
) -> dict:
    if not channel_enabled("wechat"):
        return {"skipped": True, "reason": "微信渠道已禁用"}
    normalized_title = str(title).replace("\r", " ").replace("\n", " ").strip()
    if not normalized_title:
        raise ValueError("Hermes微信消息标题不能为空。")
    normalized_description = str(description).strip()
    message = (
        f"{normalized_title}\n\n{normalized_description}"
        if normalized_description
        else normalized_title
    )
    configured_bin = str(os.environ.get(HERMES_SEND_BIN_ENV) or "").strip()
    if configured_bin:
        hermes_bin = configured_bin
    elif DEFAULT_HERMES_SEND_BIN.is_file():
        hermes_bin = str(DEFAULT_HERMES_SEND_BIN)
    else:
        hermes_bin = str(shutil.which("hermes") or "")
    if not hermes_bin:
        raise RuntimeError("找不到 Hermes 命令，请配置 HERMES_SEND_BIN。")

    try:
        completed = subprocess.run(
            [hermes_bin, "send", "--to", "weixin", "--json", message],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Hermes微信推送超时（{timeout:g}秒）。") from exc

    raw_output = completed.stdout.strip()
    if completed.returncode != 0:
        detail = completed.stderr.strip() or raw_output or f"退出代码 {completed.returncode}"
        raise RuntimeError(f"Hermes微信推送失败：{detail}")
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        detail = raw_output or completed.stderr.strip() or "未返回结果"
        raise RuntimeError(f"Hermes微信推送返回异常：{detail}") from exc
    if not isinstance(payload, dict) or not payload.get("success"):
        raise RuntimeError(f"Hermes微信推送未确认成功：{payload}")
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
        alert_level=_optional_float(payload.get("alert_level")),
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
    step: float = 0.0,
) -> PriceAlertResult:
    """Ladder alert below ``threshold``: 700, then 695, 690, ... when ``step`` is 5.

    Each ladder level alerts once. Price hovering around a level already alerted, or
    between two levels, stays quiet. A gap through several levels sends one alert for
    the lowest level crossed. The ladder resets to ``threshold`` once price recovers to
    ``threshold + step`` or higher. ``step=0`` keeps the single-level behaviour: alert
    once below ``threshold`` and re-arm at ``threshold``.
    """
    numeric_price = float(price)
    numeric_threshold = float(threshold)
    numeric_step = max(0.0, float(step))
    reset_level = numeric_threshold + numeric_step
    state = load_price_alert_state(state_path)
    checked_text = checked_at.isoformat(timespec="seconds")
    active = state.below_threshold
    alert_level = state.alert_level if state.alert_level is not None else (numeric_threshold if active else None)

    def lowest_crossed_level() -> float:
        if numeric_step <= 0:
            return numeric_threshold
        # Largest k with price < threshold - k * step.
        k = max(0, math.ceil((numeric_threshold - numeric_price) / numeric_step) - 1)
        return numeric_threshold - k * numeric_step

    next_level = (
        numeric_threshold if not active
        else (alert_level - numeric_step if numeric_step > 0 else None)
    )
    if next_level is not None and numeric_price < next_level:
        level = lowest_crossed_level()
        following = level - numeric_step
        title = f"铁矿石主连跌破 {level:g} 元/吨"
        ladder_note = (
            f"下一次提醒：跌破 {following:g} 元/吨；价格回升到 {reset_level:g} 元/吨及以上后，"
            f"从 {numeric_threshold:g} 重新开始提醒。"
            if numeric_step > 0
            else "价格回到阈值及以上后，再次跌破会重新提醒。"
        )
        description = (
            f"- 当前价格：**{numeric_price:.1f} 元/吨**\n"
            f"- 本次触发：{level:.1f} 元/吨（监控起点 {numeric_threshold:.1f}）\n"
            f"- 当前合约：{contract or 'I0'}\n"
            f"- 行情时间：{checked_at:%Y-%m-%d %H:%M:%S}\n\n"
            f"{ladder_note}\n\n"
            "价格来自指数监控使用的铁矿石主连实时行情。"
        )
        notify(title, description)
        state.last_alert_at = checked_text
        status = "alerted"
        message = f"已推送：{contract or 'I0'} {numeric_price:.1f} < {level:g}"
        notified = True
        active, alert_level = True, level
    elif active and numeric_price >= reset_level:
        status = "rearmed"
        message = f"价格已回升到 {reset_level:g} 及以上，从 {numeric_threshold:g} 重新布防：{numeric_price:.1f}"
        notified = False
        active, alert_level = False, None
    elif active:
        status = "below_suppressed" if numeric_price < numeric_threshold else "rearm_pending"
        waiting_for = f"下一档 {next_level:g}" if next_level is not None else f"回到 {reset_level:g}"
        message = f"已提醒至 {alert_level:g}，等待{waiting_for}：{numeric_price:.1f}"
        notified = False
    else:
        status = "normal"
        message = f"价格未触发：{numeric_price:.1f}"
        notified = False

    state.below_threshold = active
    state.alert_level = alert_level
    state.last_price = numeric_price
    state.last_contract = str(contract or "I0")
    state.last_check_at = checked_text
    save_price_alert_state(state_path, state)
    return PriceAlertResult(status, message, notified, numeric_price < numeric_threshold)


def _optional_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
