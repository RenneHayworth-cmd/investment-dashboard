import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv(override=False)
from web.backend.security import install_log_redaction
install_log_redaction()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from web.backend.coordinator import Coordinator
from web.backend.schemas import Dashboard


def create_app(coordinator=None, *, start_worker=True):
    state = coordinator or Coordinator(os.environ.get("TICKFLOW_API_KEY", ""))

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            state.start()
        yield
        if start_worker:
            state.close()

    app = FastAPI(title="持仓分析 API", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.coordinator = state

    @app.middleware("http")
    async def headers_and_origin(request: Request, call_next):
        if request.method == "POST":
            if request.headers.get("x-position-client") != "web":
                return JSONResponse({"detail": "无效的刷新请求"}, status_code=403)
            origin = request.headers.get("origin")
            expected = os.environ.get("WEB_ORIGIN", "")
            if origin and expected and origin != expected:
                return JSONResponse({"detail": "请求来源不被允许"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        return JSONResponse({"detail": "服务暂时不可用，请稍后重试；已有行情缓存已保留。"}, status_code=500)

    @app.get("/api/health")
    def health():
        snapshot = state.read()
        return {"status": "ok", "ready": snapshot is not None, "refreshing": state.refreshing}

    def snapshot():
        result = state.read()
        if result is None:
            raise HTTPException(503, "正在读取本地缓存，请稍候")
        return result

    @app.get("/api/dashboard", response_model=Dashboard)
    def dashboard():
        return snapshot()

    @app.get("/api/timing/etf")
    def etf():
        data = snapshot()
        return {"formal": data["etf_formal"], "preview": data["etf_preview"], "preview_codes": data["preview_codes"]}

    @app.get("/api/timing/index")
    def index():
        data = snapshot()
        return {"formal": data["index_formal"], "preview": data["index_preview"]}

    @app.get("/api/guidance/recent")
    def guidance():
        return snapshot()["guidance"]

    @app.get("/api/strategy/{section}")
    def strategy(section: str):
        data = snapshot()
        if section == "trade-preview":
            return data["trade_preview"]
        key = {"summary": "summary", "positions": "positions", "performance": "daily", "trades": "trades"}.get(section)
        if key is None:
            raise HTTPException(404, "未找到策略视图")
        return {"data": data["strategy"][key], "warnings": data["strategy"]["warnings"], "errors": data["strategy"]["errors"]}

    @app.get("/api/derivatives")
    def derivative():
        return snapshot()["derivatives"]

    @app.get("/api/spreads")
    def spreads():
        return snapshot()["spreads"]

    @app.get("/api/instruments/{code}")
    def detail(code: str):
        data = state.detail(code)
        if data is None:
            raise HTTPException(404, "未找到标的缓存")
        return data

    @app.post("/api/refresh", status_code=202)
    def refresh():
        accepted = state.request_refresh()
        return {"accepted": accepted, "message": "已安排后台检查" if accepted else "复用正在进行或刚完成的刷新；行情按原有时段更新"}

    return app


app = create_app()
