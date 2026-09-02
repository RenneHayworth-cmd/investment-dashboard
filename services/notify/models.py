from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo


class Priority(str, Enum):
    MIN = "min"  # 1
    LOW = "low"  # 2
    DEFAULT = "default"  # 3
    HIGH = "high"  # 4
    URGENT = "urgent"  # 5

    @classmethod
    def from_str(cls, value: str | int | Priority | None) -> Priority:
        if value is None:
            return cls.DEFAULT
        if isinstance(value, cls):
            return value
        val_str = str(value).strip().lower()
        if "." in val_str:
            val_str = val_str.split(".")[-1]
        mapping = {
            "1": cls.MIN,
            "min": cls.MIN,
            "2": cls.LOW,
            "low": cls.LOW,
            "3": cls.DEFAULT,
            "default": cls.DEFAULT,
            "normal": cls.DEFAULT,
            "4": cls.HIGH,
            "high": cls.HIGH,
            "5": cls.URGENT,
            "urgent": cls.URGENT,
            "max": cls.URGENT,
            "emergency": cls.URGENT,
        }
        return mapping.get(val_str, cls.DEFAULT)

    @property
    def level(self) -> int:
        levels = {
            Priority.MIN: 1,
            Priority.LOW: 2,
            Priority.DEFAULT: 3,
            Priority.HIGH: 4,
            Priority.URGENT: 5,
        }
        return levels[self]


class ChannelType(str, Enum):
    SERVERCHAN = "serverchan"
    NTFY = "ntfy"
    WINDOWS_TOAST = "windows_toast"
    PUSHPLUS = "pushplus"
    WECOM = "wecom"
    AUTO = "auto"

    @classmethod
    def from_str(cls, value: str | ChannelType) -> ChannelType:
        if isinstance(value, cls):
            return value
        val = str(value).strip().lower()
        if "." in val:
            val = val.split(".")[-1]
        for member in cls:
            if member.value == val:
                return member
        return cls.AUTO


@dataclass
class NotificationAction:
    action_type: str = "view"  # view, http, broadcast
    label: str = ""
    url: str = ""
    clear: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action_type,
            "label": self.label,
            "url": self.url,
            "clear": self.clear,
        }


@dataclass
class NotificationMessage:
    topic: str = "alerts"
    title: str = ""
    body: str = ""
    priority: Priority = Priority.DEFAULT
    tags: list[str] = field(default_factory=list)
    click_url: str = ""
    actions: list[NotificationAction] = field(default_factory=list)
    channels: list[ChannelType] = field(default_factory=lambda: [ChannelType.AUTO])
    created_at: str = field(
        default_factory=lambda: datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    )
    id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["priority"] = self.priority.value
        d["channels"] = [c.value if isinstance(c, ChannelType) else str(c) for c in self.channels]
        d["actions"] = [a.to_dict() if isinstance(a, NotificationAction) else a for a in self.actions]
        return d


@dataclass(frozen=True)
class DeliveryResult:
    channel: str
    success: bool
    message: str
    response_data: dict[str, Any] = field(default_factory=dict)
