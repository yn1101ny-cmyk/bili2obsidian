"""FastAPI 应用工厂。

服务**只监听回环地址** —— 这不是约定，是 ``app/config.py`` 里的硬校验：
``host`` 填 ``0.0.0.0`` 之类的值会直接拒绝启动，从根上杜绝把笔记
暴露到局域网。

界面刻意做得极简：三个页面（首页 / 笔记页 / 自检页），
改配置直接编辑 ``.env``，不做设置界面。前端是原生 HTML + CSS + JS，
**不引入任何 CDN 资源**（离线可用，也不给页面增加出网点）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.pipeline import JobManager
from app.storage.db import Database
from app.storage.vault import Vault
from app.web.paths import STATIC_DIR
from app.web.routes import build_router

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造应用。传入 ``settings`` 便于测试注入隔离配置。"""
    settings = settings or get_settings()
    settings.ensure_dirs()

    db = Database(settings.db_path)
    db.init()
    # 修掉历史版本留下的重复记录（早期每次生成都用新 id）
    db.dedupe_notes()
    vault = Vault(settings)
    jobs = JobManager(settings, db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "MyNoteApp 启动：http://%s:%d（数据目录 %s）",
            settings.host,
            settings.port,
            settings.data_dir,
        )
        try:
            yield
        finally:
            # 关服务时把在跑的任务收掉，别留下半截的转写
            await jobs.shutdown()

    app = FastAPI(
        title="MyNoteApp",
        description="本地优先的 B站视频笔记工具",
        version="0.1.0",
        lifespan=lifespan,
        # 本地自用工具，不需要 API 文档页面
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.state.settings = settings
    app.state.db = db
    app.state.vault = vault
    app.state.jobs = jobs

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(build_router())

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """最小存活探针，不发任何请求、不碰数据库。"""
        return {"status": "ok"}

    return app
