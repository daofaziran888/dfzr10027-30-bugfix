"""分析工坊 · 数据分析与数据应用平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.store import reset_store, store

app = FastAPI(title="分析工坊 · 数据分析与数据应用平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()


@app.post("/api/test/reset")
def reset_test_state() -> dict[str, object]:
    """重置内存仓库到种子状态，仅供本地/E2E 测试做用例隔离，生产环境禁用。"""
    if not _is_test_env():
        raise HTTPException(status_code=404, detail="接口不存在")
    reset_store()
    return {"ok": True, "message": "内存数据已重置为种子状态"}


def _is_test_env() -> bool:
    import os

    return os.getenv("APP_ENV", "local") in {"local", "test", "dev"}
