from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import platform
import re
import subprocess
from typing import Any
import requests

from services.notify.models import ChannelType, DeliveryResult, NotificationMessage, Priority

logger = logging.getLogger("services.notify.channels")

CONFIG_DIR = Path.home() / ".config" / "investment_dashboard"


def _read_config_file(filename: str, env_var: str = "") -> str:
    if env_var:
        val = os.environ.get(env_var, "").strip()
        if val:
            return val
    file_path = CONFIG_DIR / filename
    try:
        return file_path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""


class BaseChannel:
    channel_type: ChannelType

    def is_configured(self) -> bool:
        raise NotImplementedError

    def send(self, message: NotificationMessage, timeout: float = 10.0) -> DeliveryResult:
        raise NotImplementedError


class ServerChanChannel(BaseChannel):
    channel_type = ChannelType.SERVERCHAN

    def __init__(self, sendkey: str = ""):
        self._sendkey = sendkey or _read_config_file("serverchan_sendkey", "SERVERCHAN_SENDKEY")

    def is_configured(self) -> bool:
        return bool(self._sendkey)

    def _get_endpoint(self, sendkey: str) -> str:
        normalized = sendkey.strip()
        if normalized.startswith("sctp"):
            match = re.match(r"^sctp(\d+)t", normalized)
            if match:
                return f"https://{match.group(1)}.push.ft07.com/send/{normalized}.send"
        return f"https://sctapi.ftqq.com/{normalized}.send"

    def send(self, message: NotificationMessage, timeout: float = 10.0) -> DeliveryResult:
        if not self.is_configured():
            return DeliveryResult(self.channel_type.value, False, "ServerChan SendKey 未配置")
        try:
            url = self._get_endpoint(self._sendkey)
            title = message.title.replace("\r", " ").replace("\n", " ").strip()
            if message.tags:
                title = f"[{' '.join(message.tags)}] {title}"
            
            body = message.body
            if message.click_url:
                body = f"{body}\n\n[🔗 点击查看详情]({message.click_url})"

            resp = requests.post(
                url,
                json={"title": title, "desp": body},
                headers={"Content-Type": "application/json;charset=utf-8"},
                timeout=timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
            if str(payload.get("code")) == "0":
                return DeliveryResult(self.channel_type.value, True, "推送成功", payload)
            err_msg = str(payload.get("message") or payload.get("data") or "未知错误")
            return DeliveryResult(self.channel_type.value, False, f"ServerChan推送失败: {err_msg}", payload)
        except Exception as e:
            logger.warning("ServerChan send failed: %s", e)
            return DeliveryResult(self.channel_type.value, False, f"网络请求异常: {e}")


class NtfyBridgeChannel(BaseChannel):
    channel_type = ChannelType.NTFY

    def __init__(self, server_url: str = "", default_topic: str = ""):
        self.server_url = server_url or _read_config_file("ntfy_server", "NTFY_SERVER") or "https://ntfy.sh"
        self.default_topic = default_topic or _read_config_file("ntfy_topic", "NTFY_TOPIC")

    def is_configured(self) -> bool:
        return bool(self.default_topic)

    def send(self, message: NotificationMessage, timeout: float = 10.0) -> DeliveryResult:
        topic = message.topic or self.default_topic
        if not topic:
            return DeliveryResult(self.channel_type.value, False, "ntfy topic 未指定且无默认配置")

        url = f"{self.server_url.rstrip('/')}/{topic}"
        headers: dict[str, str] = {
            "Title": message.title.encode("utf-8").decode("latin1", errors="ignore") if message.title else "Notification",
            "Priority": str(message.priority.level),
        }
        if message.tags:
            headers["Tags"] = ",".join(message.tags)
        if message.click_url:
            headers["Click"] = message.click_url
        if message.actions:
            action_strs = []
            for a in message.actions:
                action_strs.append(f"{a.action_type}, {a.label}, {a.url}")
            headers["Actions"] = "; ".join(action_strs)

        try:
            resp = requests.post(
                url,
                data=message.body.encode("utf-8"),
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()
            payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            return DeliveryResult(self.channel_type.value, True, "ntfy 推送成功", payload)
        except Exception as e:
            logger.warning("ntfy send failed: %s", e)
            return DeliveryResult(self.channel_type.value, False, f"ntfy 推送异常: {e}")


class PushPlusChannel(BaseChannel):
    channel_type = ChannelType.PUSHPLUS

    def __init__(self, token: str = ""):
        self._token = token or _read_config_file("pushplus_token", "PUSHPLUS_TOKEN")

    def is_configured(self) -> bool:
        return bool(self._token)

    def send(self, message: NotificationMessage, timeout: float = 10.0) -> DeliveryResult:
        if not self.is_configured():
            return DeliveryResult(self.channel_type.value, False, "PushPlus Token 未配置")
        try:
            url = "http://www.pushplus.plus/send"
            data = {
                "token": self._token,
                "title": message.title,
                "content": message.body.replace("\n", "<br/>"),
                "template": "html",
            }
            resp = requests.post(url, json=data, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("code") == 200:
                return DeliveryResult(self.channel_type.value, True, "PushPlus 推送成功", payload)
            return DeliveryResult(self.channel_type.value, False, f"PushPlus 失败: {payload.get('msg')}", payload)
        except Exception as e:
            return DeliveryResult(self.channel_type.value, False, f"PushPlus 请求异常: {e}")


class WeComChannel(BaseChannel):
    channel_type = ChannelType.WECOM

    def __init__(self, webhook_url: str = ""):
        self._webhook = webhook_url or _read_config_file("wecom_webhook", "WECOM_WEBHOOK")

    def is_configured(self) -> bool:
        return bool(self._webhook)

    def send(self, message: NotificationMessage, timeout: float = 10.0) -> DeliveryResult:
        if not self.is_configured():
            return DeliveryResult(self.channel_type.value, False, "企业微信 Webhook 未配置")
        try:
            content = f"### {message.title}\n\n{message.body}"
            if message.click_url:
                content += f"\n\n[点击查看详情]({message.click_url})"
            payload = {"msgtype": "markdown", "markdown": {"content": content}}
            resp = requests.post(self._webhook, json=payload, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode") == 0:
                return DeliveryResult(self.channel_type.value, True, "企业微信推送成功", data)
            return DeliveryResult(self.channel_type.value, False, f"企业微信错误: {data.get('errmsg')}", data)
        except Exception as e:
            return DeliveryResult(self.channel_type.value, False, f"企业微信请求异常: {e}")


class WindowsToastChannel(BaseChannel):
    channel_type = ChannelType.WINDOWS_TOAST

    def is_configured(self) -> bool:
        # Accessible on Windows or from WSL with powershell.exe
        return True

    def send(self, message: NotificationMessage, timeout: float = 5.0) -> DeliveryResult:
        clean_title = message.title.replace('"', '\"')
        clean_body = message.body.replace('"', '\"').replace("\n", " ")
        if len(clean_body) > 120:
            clean_body = clean_body[:117] + "..."

        ps_script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$template = @"
<toast>
    <visual>
        <binding template="ToastGeneric">
            <text>{clean_title}</text>
            <text>{clean_body}</text>
        </binding>
    </visual>
    <audio src="ms-winsoundevent:Notification.Default"/>
</toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Quant Investment Alert").Show($toast)
"""
        try:
            # Check if running inside WSL or native Windows
            cmd = ["powershell.exe", "-NoProfile", "-Command", ps_script]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if res.returncode == 0:
                return DeliveryResult(self.channel_type.value, True, "Windows Toast 弹窗已触发")
            return DeliveryResult(self.channel_type.value, False, f"PowerShell Toast 执行失败: {res.stderr.strip()}")
        except Exception as e:
            return DeliveryResult(self.channel_type.value, False, f"Windows Toast 触发失败: {e}")
