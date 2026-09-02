from __future__ import annotations

import argparse
import json
import logging
import urllib.parse
from typing import Any

from starlette.requests import Request
from services.notify.engine import NotifyEngine
from services.notify.models import NotificationAction, Priority

logger = logging.getLogger("services.notify.server")


def _decode_val(val: str | None) -> str:
    if not val:
        return ""
    try:
        return urllib.parse.unquote(val)
    except Exception:
        return str(val)


def create_app(engine: NotifyEngine | None = None):
    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
    except ImportError:
        raise RuntimeError("FastAPI 未安装，请运行 pip install fastapi uvicorn")

    app = FastAPI(title="Quant-Notify Hub", description="ntfy-compatible Notification Gateway for Quantitative Investment")
    notify_engine = engine or NotifyEngine()

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "quant-notify-hub"}

    @app.api_route("/{topic}", methods=["POST", "PUT"])
    async def publish_message(topic: str, request: Request):
        body_bytes = await request.body()
        content_type = request.headers.get("content-type", "")

        # Query parameters
        q_title = _decode_val(request.query_params.get("title") or request.query_params.get("t"))
        q_priority = request.query_params.get("priority") or request.query_params.get("p")
        q_tags = _decode_val(request.query_params.get("tags") or request.query_params.get("ta"))
        q_click = _decode_val(request.query_params.get("click"))

        # Headers fallback
        h_title = _decode_val(request.headers.get("title") or request.headers.get("Title"))
        h_priority = request.headers.get("priority") or request.headers.get("Priority")
        h_tags = _decode_val(request.headers.get("tags") or request.headers.get("Tags"))
        h_click = _decode_val(request.headers.get("click") or request.headers.get("Click"))
        actions_header = _decode_val(request.headers.get("actions") or request.headers.get("Actions"))

        msg_title = q_title or h_title or ""
        msg_priority = q_priority or h_priority or "default"
        tag_str = q_tags or h_tags or ""
        tag_list = [t.strip() for t in tag_str.split(",")] if tag_str else []
        click_url = q_click or h_click or ""
        msg_body = ""
        action_list: list[NotificationAction] = []

        if "application/json" in content_type:
            try:
                payload = json.loads(body_bytes.decode("utf-8"))
                msg_title = payload.get("title") or msg_title or topic
                msg_body = payload.get("message") or payload.get("body") or ""
                if "priority" in payload:
                    msg_priority = payload["priority"]
                if "tags" in payload:
                    t_val = payload["tags"]
                    tag_list = t_val if isinstance(t_val, list) else [t_val]
                if "click" in payload:
                    click_url = payload["click"]
                if "actions" in payload and isinstance(payload["actions"], list):
                    for a in payload["actions"]:
                        if isinstance(a, dict):
                            action_list.append(NotificationAction(
                                action_type=a.get("action", "view"),
                                label=a.get("label", ""),
                                url=a.get("url", ""),
                            ))
            except Exception:
                msg_body = body_bytes.decode("utf-8", errors="ignore")
        else:
            msg_body = body_bytes.decode("utf-8", errors="ignore")
            if not msg_title:
                msg_title = f"[{topic}] 通知"

        if actions_header:
            for item in actions_header.split(";"):
                parts = [p.strip() for p in item.split(",")]
                if len(parts) >= 3:
                    action_list.append(NotificationAction(action_type=parts[0], label=parts[1], url=parts[2]))

        results = notify_engine.send(
            title=msg_title,
            body=msg_body,
            topic=topic,
            priority=msg_priority,
            tags=tag_list,
            click_url=click_url,
            actions=action_list,
        )

        return {
            "topic": topic,
            "title": msg_title,
            "priority": msg_priority,
            "delivered": any(r.success for r in results),
            "results": [r.__dict__ for r in results],
        }

    @app.get("/{topic}/json")
    async def get_topic_history(topic: str, limit: int = 50):
        return notify_engine.get_history(topic=topic, limit=limit)

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="启动 Quant-Notify Hub ntfy 兼容推送服务。")
    parser.add_argument("--host", default="0.0.0.0", help="监听主机地址")
    parser.add_argument("--port", type=int, default=8080, help="监听端口 (默认 8080)")
    args = parser.parse_args()

    app = create_app()
    print(f"🚀 Quant-Notify Hub 正在启动: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
