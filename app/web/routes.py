"""路由：三个页面 + 一组 JSON API。

页面只用 Jinja2 渲染骨架，数据靠前端 fetch JSON 填充 ——
这样进度刷新不会整页重载，也便于以后换前端。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.bili.urls import parse_video_input
from app.errors import InvalidVideoUrl
from app.schemas import JobStage, NoteRecord
from app.web.markdown_render import render_markdown, strip_frontmatter
from app.web.paths import TEMPLATES_DIR

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class JobCreate(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class ClearRequest(BaseModel):
    #: 必须是这个字面量才执行，避免误触把用户数据删光
    confirm: str = ""


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def _record_json(r: NoteRecord) -> dict:
    return {
        "id": r.id,
        "bvid": r.bvid,
        "title": r.title,
        "one_liner": r.one_liner,
        "tags": r.tags,
        "transcript_source": r.transcript_source.value,
        "transcript_label": r.transcript_source.label,
        "confidence": r.transcript_source.confidence,
        "duration": r.duration,
        "created_at": r.created_at.strftime("%Y-%m-%d %H:%M"),
    }


def _job_json(job) -> dict:
    return {
        "id": job.id,
        "url": job.url,
        "stage": job.stage.value,
        "stage_label": job.stage.label,
        "progress": round(job.progress, 4),
        "message": job.message,
        "error": job.error,
        "bvid": job.bvid,
        "note_id": job.note_id,
        "log": job.log[-40:],
        "done": job.stage.is_terminal,
        "failed": job.stage is JobStage.FAILED,
        "cancelled": job.stage is JobStage.CANCELLED,
        "updated_at": job.updated_at.strftime("%H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def build_router() -> APIRouter:  # noqa: C901 - 路由集中在一处更好读
    router = APIRouter()

    # ======================= 页面 =======================

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"settings": request.app.state.settings},
        )

    @router.get("/notes/{note_id}", response_class=HTMLResponse, include_in_schema=False)
    async def note_page(request: Request, note_id: str):
        record = request.app.state.db.get_note(note_id)
        if record is None:
            raise HTTPException(status_code=404, detail="找不到这篇笔记")

        vault = request.app.state.vault
        markdown = vault.read_note_by_id(record)
        if not markdown:
            raise HTTPException(
                status_code=410,
                detail=f"笔记文件已不存在：{record.markdown_path}",
            )

        return templates.TemplateResponse(
            request=request,
            name="note.html",
            context={
                "settings": request.app.state.settings,
                "record": record,
                "body_html": render_markdown(strip_frontmatter(markdown)),
            },
        )

    @router.get("/checks", response_class=HTMLResponse, include_in_schema=False)
    async def checks_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="checks.html",
            context={"settings": request.app.state.settings},
        )

    # ======================= 任务 =======================

    @router.post("/api/jobs")
    async def create_job(request: Request, payload: JobCreate):
        url = payload.url.strip()
        # 先把链接解析一遍，格式不对就当场告诉用户，
        # 而不是排进队列、跑一半才失败
        try:
            parse_video_input(url)
        except InvalidVideoUrl as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        settings = request.app.state.settings
        if settings.llm_provider == "none":
            raise HTTPException(
                status_code=400,
                detail="当前 LLM_PROVIDER=none，无法生成笔记。请在 .env 里改为 ollama。",
            )

        job = request.app.state.jobs.submit(url)
        return JSONResponse(_job_json(job), status_code=201)

    @router.get("/api/jobs")
    async def list_jobs(request: Request, limit: int = 10):
        limit = max(1, min(limit, 100))
        jobs = request.app.state.db.list_jobs(limit)
        return [_job_json(j) for j in jobs]

    @router.get("/api/jobs/{job_id}")
    async def get_job(request: Request, job_id: str):
        job = request.app.state.db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return _job_json(job)

    @router.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(request: Request, job_id: str):
        ok = request.app.state.jobs.cancel(job_id)
        if not ok:
            raise HTTPException(
                status_code=409, detail="该任务已经结束或不在运行中"
            )
        return {"cancelled": True}

    # ======================= 笔记 =======================

    @router.get("/api/notes")
    async def list_notes(request: Request, limit: int = 50):
        limit = max(1, min(limit, 500))
        return [
            _record_json(r) for r in request.app.state.db.list_notes(limit)
        ]

    @router.get("/api/notes/{note_id}")
    async def get_note(request: Request, note_id: str):
        record = request.app.state.db.get_note(note_id)
        if record is None:
            raise HTTPException(status_code=404, detail="找不到这篇笔记")
        return _record_json(record)

    @router.get("/api/notes/{note_id}/markdown")
    async def get_note_markdown(request: Request, note_id: str):
        record = request.app.state.db.get_note(note_id)
        if record is None:
            raise HTTPException(status_code=404, detail="找不到这篇笔记")
        text = request.app.state.vault.read_note_by_id(record)
        return {"markdown": text}

    @router.delete("/api/notes/{note_id}")
    async def delete_note(request: Request, note_id: str):
        """删除一篇笔记（同时删掉磁盘上的 Markdown）。"""
        db = request.app.state.db
        record = db.get_note(note_id)
        if record is None:
            raise HTTPException(status_code=404, detail="找不到这篇笔记")

        file_removed = request.app.state.vault.delete_note_file(
            request.app.state.vault.note_path(record)
        )
        db.delete_note(note_id)
        return {"deleted": True, "file_removed": file_removed}

    # ======================= 自检与数据 =======================

    @router.get("/api/checks")
    async def api_checks(request: Request, full: bool = False):
        """环境自检。``full=1`` 时包含联网项（会请求 B站 与 Ollama）。"""
        from scripts.doctor import run_checks

        settings = request.app.state.settings
        report = await run_checks(settings, offline=not full)
        return {
            "status": report.worst,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "detail": c.detail,
                    "hint": c.hint,
                }
                for c in report.checks
            ],
            "config": settings.redacted(),
        }

    @router.get("/api/data/stats")
    async def data_stats(request: Request):
        vault = request.app.state.vault
        db = request.app.state.db
        stats = vault.stats()
        return {
            "notes": db.count_notes(),
            "summary": stats.human(),
            "media_bytes": stats.media_bytes,
            "cache_bytes": stats.cache_bytes,
            "model_bytes": stats.model_bytes,
            "total_bytes": stats.total_bytes,
        }

    @router.post("/api/data/clear")
    async def clear_data(request: Request, payload: ClearRequest):
        """清除本机全部数据。必须显式传 confirm=DELETE，避免误触。"""
        if payload.confirm != "DELETE":
            raise HTTPException(
                status_code=400,
                detail='为了防止误删，请在 confirm 字段里原样填写 "DELETE"。',
            )
        await request.app.state.jobs.shutdown()
        removed = request.app.state.vault.clear_all()
        # clear_all 会删掉 .db 文件，必须重置幂等标志重新建表，
        # 否则之后所有数据库操作都会报 no such table
        request.app.state.db.reset()
        return {"removed": removed, "cleared": sum(removed.values())}

    return router


__all__ = ["build_router", "templates"]
