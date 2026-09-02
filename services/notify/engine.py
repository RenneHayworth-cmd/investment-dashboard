from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from services.notify.channels import (
    BaseChannel,
    ChannelType,
    NtfyBridgeChannel,
    PushPlusChannel,
    ServerChanChannel,
    WeComChannel,
    WindowsToastChannel,
)
from services.notify.models import DeliveryResult, NotificationAction, NotificationMessage, Priority

logger = logging.getLogger("services.notify.engine")

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "output" / "alerts" / "notifications.db"


class NotifyEngine:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

        self.channels: dict[ChannelType, BaseChannel] = {
            ChannelType.SERVERCHAN: ServerChanChannel(),
            ChannelType.NTFY: NtfyBridgeChannel(),
            ChannelType.WINDOWS_TOAST: WindowsToastChannel(),
            ChannelType.PUSHPLUS: PushPlusChannel(),
            ChannelType.WECOM: WeComChannel(),
        }

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT,
                    priority TEXT,
                    tags TEXT,
                    click_url TEXT,
                    actions TEXT,
                    channels TEXT,
                    results TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_state (
                    alert_key TEXT PRIMARY KEY,
                    state_json TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.commit()

    def register_channel(self, channel_type: ChannelType, channel: BaseChannel) -> None:
        self.channels[channel_type] = channel

    def send(
        self,
        title: str,
        body: str = "",
        *,
        topic: str = "alerts",
        priority: Priority | str = Priority.DEFAULT,
        tags: list[str] | None = None,
        click_url: str = "",
        actions: list[NotificationAction] | None = None,
        channels: list[ChannelType | str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[DeliveryResult]:
        p = Priority.from_str(priority) if isinstance(priority, str) else priority
        tag_list = tags or []
        action_list = actions or []
        msg_id = uuid.uuid4().hex[:12]
        now_str = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")

        # Resolve target channels
        target_channels: list[ChannelType] = []
        if channels:
            for c in channels:
                ctype = ChannelType.from_str(c) if isinstance(c, str) else c
                if ctype != ChannelType.AUTO and ctype not in target_channels:
                    target_channels.append(ctype)

        # If AUTO or empty: determine best available channels based on priority
        if not target_channels:
            # ServerChan as primary WeChat
            if self.channels[ChannelType.SERVERCHAN].is_configured():
                target_channels.append(ChannelType.SERVERCHAN)
            elif self.channels[ChannelType.PUSHPLUS].is_configured():
                target_channels.append(ChannelType.PUSHPLUS)
            elif self.channels[ChannelType.WECOM].is_configured():
                target_channels.append(ChannelType.WECOM)

            # If ntfy is configured, push to ntfy
            if self.channels[ChannelType.NTFY].is_configured():
                target_channels.append(ChannelType.NTFY)

            # High or Urgent priority automatically pops Windows Toast
            if p.level >= Priority.HIGH.level:
                target_channels.append(ChannelType.WINDOWS_TOAST)

        message = NotificationMessage(
            id=msg_id,
            topic=topic,
            title=title,
            body=body,
            priority=p,
            tags=tag_list,
            click_url=click_url,
            actions=action_list,
            channels=target_channels,
            created_at=now_str,
            metadata=metadata or {},
        )

        results: list[DeliveryResult] = []
        for ch_type in target_channels:
            channel_inst = self.channels.get(ch_type)
            if channel_inst:
                res = channel_inst.send(message)
                results.append(res)
            else:
                results.append(DeliveryResult(ch_type.value, False, f"通道 {ch_type.value} 未注册"))

        # Save to SQLite history
        self._save_record(message, results)
        return results

    def _save_record(self, message: NotificationMessage, results: list[DeliveryResult]) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO notifications (id, topic, title, body, priority, tags, click_url, actions, channels, results, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.id,
                        message.topic,
                        message.title,
                        message.body,
                        message.priority.value,
                        json.dumps(message.tags, ensure_ascii=False),
                        message.click_url,
                        json.dumps([a.to_dict() for a in message.actions], ensure_ascii=False),
                        json.dumps([c.value for c in message.channels], ensure_ascii=False),
                        json.dumps([asdict(r) for r in results], ensure_ascii=False),
                        message.created_at,
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.error("Failed to save notification record to DB: %s", e)

    def get_history(self, topic: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if topic:
                rows = conn.execute(
                    "SELECT * FROM notifications WHERE topic = ? ORDER BY created_at DESC LIMIT ?",
                    (topic, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            
            history = []
            for r in rows:
                history.append({
                    "id": r["id"],
                    "topic": r["topic"],
                    "title": r["title"],
                    "body": r["body"],
                    "priority": r["priority"],
                    "tags": json.loads(r["tags"] or "[]"),
                    "click_url": r["click_url"],
                    "actions": json.loads(r["actions"] or "[]"),
                    "channels": json.loads(r["channels"] or "[]"),
                    "results": json.loads(r["results"] or "[]"),
                    "created_at": r["created_at"],
                })
            return history
