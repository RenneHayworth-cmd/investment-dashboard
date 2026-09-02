from __future__ import annotations

from services.notify.channels import (
    BaseChannel,
    ChannelType,
    NtfyBridgeChannel,
    PushPlusChannel,
    ServerChanChannel,
    WeComChannel,
    WindowsToastChannel,
)
from services.notify.engine import NotifyEngine
from services.notify.models import (
    DeliveryResult,
    NotificationAction,
    NotificationMessage,
    Priority,
)

# Global default engine instance
_default_engine: NotifyEngine | None = None


def get_default_engine() -> NotifyEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = NotifyEngine()
    return _default_engine


def send_alert(
    title: str,
    body: str = "",
    *,
    topic: str = "alerts",
    priority: Priority | str = Priority.HIGH,
    tags: list[str] | None = None,
    click_url: str = "",
    channels: list[ChannelType | str] | None = None,
) -> list[DeliveryResult]:
    """快捷发送量化预警通知（默认 HIGH 优先级，自动触发微信+桌面弹窗）。"""
    return get_default_engine().send(
        title=title,
        body=body,
        topic=topic,
        priority=priority,
        tags=tags or ["warning"],
        click_url=click_url,
        channels=channels,
    )


def send_trade_signal(
    symbol: str,
    name: str,
    action: str,
    price: float,
    details: str = "",
    *,
    topic: str = "trades",
    click_url: str = "",
) -> list[DeliveryResult]:
    """快捷发送 ETF / 期货择时买卖信号通知。"""
    emoji_map = {
        "买入": "📈",
        "加仓": "📈",
        "满仓": "📈",
        "卖出": "📉",
        "降仓": "📉",
        "空仓": "📉",
        "转入512890": "🛡️",
    }
    emoji = emoji_map.get(action, "🔔")
    title = f"{emoji} {name}({symbol}) 触发【{action}】信号"
    body_text = f"- 标的代码：**{symbol} ({name})**\n- 触发动作：**{action}**\n- 最新市价：**{price:.3f} 元**\n"
    if details:
        body_text += f"- 策略明细：{details}\n"
    
    return get_default_engine().send(
        title=title,
        body=body_text,
        topic=topic,
        priority=Priority.URGENT if "卖出" in action or "买入" in action else Priority.HIGH,
        tags=["trade", symbol],
        click_url=click_url,
    )


__all__ = [
    "Priority",
    "ChannelType",
    "NotificationAction",
    "NotificationMessage",
    "DeliveryResult",
    "BaseChannel",
    "ServerChanChannel",
    "NtfyBridgeChannel",
    "PushPlusChannel",
    "WeComChannel",
    "WindowsToastChannel",
    "NotifyEngine",
    "get_default_engine",
    "send_alert",
    "send_trade_signal",
]
