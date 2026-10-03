"""B站 API 客户端。

**出网范围**：只访问 ``*.bilibili.com`` 与 ``*.hdslb.com``（字幕/封面 CDN）。

工程上的几个关键处理：

* **-412 风控**：必须带浏览器 UA + Referer；另外先取一次 ``buvid3``
  指纹 cookie，能显著降低被拦概率。
* **限速**：请求之间强制最小间隔，避免高频触发风控。
* **缓存**：元数据/字幕落盘缓存，重复处理同一视频不再出网。
* **密钥不外泄**：SESSDATA 只作为 Cookie 头发送，绝不写日志/写文件。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from app.bili.subtitles import (
    SubtitleTrack,
    normalize_subtitle_url,
    parse_subtitle_body,
    parse_subtitle_tracks,
    parse_view_points,
    pick_best_track,
)
from app.bili.urls import VideoRef
from app.config import Settings
from app.errors import BiliApiError, InvalidVideoUrl, NoTranscriptAvailable
from app.schemas import Chapter, Transcript, TranscriptSegment, VideoMeta, VideoPage

logger = logging.getLogger(__name__)

_API_VIEW = "https://api.bilibili.com/x/web-interface/view"
_API_PLAYER = "https://api.bilibili.com/x/player/v2"
_API_TAGS = "https://api.bilibili.com/x/tag/archive/tags"
_API_NAV = "https://api.bilibili.com/x/web-interface/nav"
_API_FINGER = "https://api.bilibili.com/x/frontend/finger/spi"

#: B站业务错误码 -> 人话
_CODE_HINTS: dict[int, str] = {
    -101: "账号未登录（SESSDATA 缺失或已失效，需要重新获取）",
    -400: "请求参数错误",
    -403: "访问权限不足（可能需要登录，或该视频有观看限制）",
    -404: "视频不存在或已被删除",
    -412: "触发风控被拦截（请求过频或行为异常）",
    -509: "请求过于频繁，已被限流，请稍后再试",
    # -352 是 B站明确的限流信号，yt-dlp 源码中把它当作「稍后重试」处理
    -352: "请求被限流（-352），需要退避后重试",
    62002: "视频不可见（可能已删除或设为私密）",
    62004: "稿件审核中，暂时无法访问",
}

#: 遇到这些错误码应当退避重试（而不是直接失败）
_RETRYABLE_CODES = frozenset({-352, -412, -509, -799})


class BiliClient:
    """B站接口封装。用完请调用 :meth:`aclose`。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self._rate_lock = asyncio.Lock()
        self._last_request = 0.0
        self._buvid: str | None = None
        self._logged_in: bool | None = None

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.bili_timeout, connect=10.0),
                follow_redirects=True,
                headers=self._base_headers(),
            )
        return self._client

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.settings.bili_user_agent,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        cookies: list[str] = []
        if self._buvid:
            cookies.append(f"buvid3={self._buvid}")
        if self.settings.has_bili_login:
            # 仅在此处拼接，绝不落到日志或文件
            cookies.append(f"SESSDATA={self.settings.bili_sessdata}")
        if cookies:
            headers["Cookie"] = "; ".join(cookies)
        return headers

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> BiliClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # 限速与缓存
    # ------------------------------------------------------------------
    async def _throttle(self) -> None:
        """保证两次请求之间至少间隔 ``bili_min_interval`` 秒。"""
        interval = self.settings.bili_min_interval
        if interval <= 0:
            return
        async with self._rate_lock:
            now = time.monotonic()
            wait = interval - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    def _cache_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.settings.cache_dir / f"{digest}.json"

    def _cache_read(self, key: str) -> Any | None:
        ttl = self.settings.cache_ttl_hours
        if ttl <= 0:
            return None
        path = self._cache_path(key)
        if not path.exists():
            return None
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours > ttl:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _cache_write(self, key: str, value: Any) -> None:
        if self.settings.cache_ttl_hours <= 0:
            return
        try:
            self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_path(key).write_text(
                json.dumps(value, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            logger.debug("写缓存失败（不影响主流程）：%s", exc)

    # ------------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------------
    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        cache_key: str | None = None,
        use_cache: bool = True,
        allow_codes: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        """GET 一个 B站 JSON 接口，带缓存 / 限速 / 退避重试。"""
        if use_cache and cache_key:
            cached = self._cache_read(cache_key)
            if cached is not None:
                logger.debug("命中缓存：%s", cache_key)
                return cached

        client = self._get_client()
        last_error: Exception | None = None

        for attempt in range(self.settings.bili_max_retries + 1):
            await self._throttle()
            try:
                resp = await client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self.settings.bili_max_retries:
                    raise BiliApiError(url, None, f"网络请求失败：{exc}") from exc
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

            if resp.status_code == 412:
                last_error = BiliApiError(
                    url, -412, _CODE_HINTS[-412]
                )
                if attempt >= self.settings.bili_max_retries:
                    raise last_error
                # 风控：刷新 buvid 并退避
                self._buvid = None
                await self._ensure_buvid(force=True)
                await asyncio.sleep(3.0 * (attempt + 1))
                continue

            if resp.status_code >= 500:
                last_error = BiliApiError(url, resp.status_code, "服务端错误")
                if attempt >= self.settings.bili_max_retries:
                    raise last_error
                await asyncio.sleep(2.0 * (attempt + 1))
                continue

            try:
                data = resp.json()
            except ValueError as exc:
                raise BiliApiError(url, None, f"返回内容不是 JSON：{resp.text[:200]}") from exc

            if not isinstance(data, dict):
                raise BiliApiError(url, None, f"返回结构异常：{str(data)[:200]}")

            code = data.get("code")
            if code != 0 and code not in allow_codes:
                hint = _CODE_HINTS.get(code, "")
                msg = data.get("message") or data.get("msg") or ""
                detail = f"{msg} {hint}".strip()

                # -352 / -412 / -509 这类是限流或风控，退避重试而不是直接失败
                if code in _RETRYABLE_CODES and attempt < self.settings.bili_max_retries:
                    wait = 2.0 * (2**attempt)  # 2s, 4s, 8s
                    logger.warning(
                        "B站返回 %s（%s），%.0fs 后重试（第 %d/%d 次）",
                        code,
                        detail,
                        wait,
                        attempt + 1,
                        self.settings.bili_max_retries,
                    )
                    last_error = BiliApiError(url, code, detail)
                    if code in (-412, -509):
                        # 风控类：刷新设备指纹再试
                        self._buvid = None
                        await self._ensure_buvid(force=True)
                    await asyncio.sleep(wait)
                    continue

                raise BiliApiError(url, code, detail)

            if use_cache and cache_key:
                self._cache_write(cache_key, data)
            return data

        raise BiliApiError(url, None, f"重试耗尽：{last_error}")

    # ------------------------------------------------------------------
    async def _ensure_buvid(self, *, force: bool = False) -> str | None:
        """获取 buvid3 指纹 cookie，降低触发 -412 的概率。"""
        if self._buvid and not force:
            return self._buvid
        try:
            data = await self._get_json(_API_FINGER, use_cache=False)
            b3 = (data.get("data") or {}).get("b_3")
            if isinstance(b3, str) and b3:
                self._buvid = b3
                logger.debug("已获取 buvid3 指纹")
        except Exception as exc:  # noqa: BLE001 - 拿不到指纹也应继续尝试
            logger.debug("获取 buvid 失败（不影响主流程）：%s", exc)
        return self._buvid

    # ------------------------------------------------------------------
    # 短链展开
    # ------------------------------------------------------------------
    async def resolve_short_url(self, code: str) -> str:
        """把 b23.tv 短链展开成真实 URL，返回其中的 BV 号。"""
        short = code if code.startswith("http") else f"https://b23.tv/{code.lstrip('/')}"
        client = self._get_client()
        try:
            resp = await client.get(short, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise InvalidVideoUrl(short, f"短链展开失败：{exc}") from exc

        final = str(resp.url)
        import re

        m = re.search(r"(BV[0-9A-Za-z]{10})", final) or re.search(
            r"(BV[0-9A-Za-z]{10})", resp.text[:5000]
        )
        if not m:
            raise InvalidVideoUrl(
                short, f"短链展开后没找到 BV 号（最终地址：{final}）"
            )
        return m.group(1)

    # ------------------------------------------------------------------
    # 元数据
    # ------------------------------------------------------------------
    async def get_video_meta(self, ref: VideoRef) -> VideoMeta:
        """拉取视频元数据，并按 ``ref.page`` 定位到具体分P。"""
        params: dict[str, Any] = {}
        if ref.bvid:
            params["bvid"] = ref.bvid
        elif ref.aid is not None:
            params["aid"] = ref.aid
        else:
            raise InvalidVideoUrl(ref.raw, "缺少 bvid 与 aid")

        data = await self._get_json(
            _API_VIEW,
            params=params,
            cache_key=f"view:{ref.bvid or ref.aid}",
        )
        v = data.get("data")
        if not isinstance(v, dict):
            raise BiliApiError(_API_VIEW, data.get("code"), "缺少 data 字段")

        pages_raw = v.get("pages") or []
        pages: list[VideoPage] = []
        for i, p in enumerate(pages_raw):
            if not isinstance(p, dict):
                continue
            pages.append(
                VideoPage(
                    cid=int(p.get("cid") or 0),
                    page=int(p.get("page") or i + 1),
                    title=str(p.get("part") or ""),
                    duration=int(p.get("duration") or 0),
                )
            )

        page_no = max(1, ref.page)
        selected: VideoPage | None = None
        for p in pages:
            if p.page == page_no:
                selected = p
                break
        if selected is None and pages:
            page_no = pages[0].page
            selected = pages[0]

        owner = v.get("owner") or {}
        pubdate = None
        ts = v.get("pubdate")
        if isinstance(ts, (int, float)) and ts > 0:
            from datetime import datetime

            pubdate = datetime.fromtimestamp(ts)

        return VideoMeta(
            bvid=str(v.get("bvid") or ref.bvid or ""),
            aid=int(v["aid"]) if isinstance(v.get("aid"), int) else ref.aid,
            title=str(v.get("title") or "").strip(),
            part_title=(selected.title if selected else ""),
            desc=str(v.get("desc") or "").strip(),
            owner_name=str(owner.get("name") or ""),
            owner_mid=owner.get("mid") if isinstance(owner.get("mid"), int) else None,
            duration=int(selected.duration if selected else v.get("duration") or 0),
            cover_url=str(v.get("pic") or ""),
            pubdate=pubdate,
            cid=selected.cid if selected else None,
            page=page_no,
            total_pages=max(1, len(pages)),
            pages=pages,
        )

    async def get_chapters(self, aid: int, cid: int) -> list[Chapter]:
        """获取章节（优先 B站 AI 生成的 ``view_points``）。

        走的是 ``player/v2``，和字幕同一个接口 ——
        缓存键相同，所以**不会多一次网络请求**。
        实测未登录时字幕拿不到，但章节能拿到。

        失败不致命：拿不到就是空列表，笔记照常生成。
        """
        try:
            data = await self._get_json(
                _API_PLAYER,
                params={"aid": aid, "cid": cid},
                cache_key=f"player:{aid}:{cid}",
            )
        except Exception as exc:  # noqa: BLE001 - 章节是锦上添花
            logger.debug("获取章节失败：%s", exc)
            return []
        chapters = parse_view_points(data)
        if chapters:
            logger.info("拿到 %d 个章节（B站 AI 划分）", len(chapters))
        return chapters

    async def get_tags(self, bvid: str) -> list[str]:
        """拉取视频标签。失败不致命，返回空列表。"""
        try:
            data = await self._get_json(
                _API_TAGS,
                params={"bvid": bvid},
                cache_key=f"tags:{bvid}",
                allow_codes=(-404,),
            )
        except Exception as exc:  # noqa: BLE001 - 标签是锦上添花
            logger.debug("获取标签失败：%s", exc)
            return []
        out: list[str] = []
        for t in data.get("data") or []:
            if isinstance(t, dict):
                name = str(t.get("tag_name") or "").strip()
                if name:
                    out.append(name)
        return out

    # ------------------------------------------------------------------
    # 字幕
    # ------------------------------------------------------------------
    async def get_subtitle_tracks(self, aid: int, cid: int) -> list[SubtitleTrack]:
        """获取字幕轨道列表。未登录时通常为空（会走 Whisper 兜底）。"""
        data = await self._get_json(
            _API_PLAYER,
            params={"aid": aid, "cid": cid},
            cache_key=f"player:{aid}:{cid}",
        )
        return parse_subtitle_tracks(data)

    async def fetch_subtitle(self, track: SubtitleTrack) -> list[TranscriptSegment]:
        """下载并解析字幕内容。"""
        url = normalize_subtitle_url(track.url)
        # 字幕存在 CDN 上，走同一条限速与重试逻辑
        data = await self._get_json(
            url,
            cache_key=f"subtitle:{url}",
            allow_codes=(),
        )
        segments = parse_subtitle_body(data)
        if not segments:
            raise NoTranscriptAvailable(f"字幕 {track.label} 内容为空")
        return segments

    async def try_get_subtitle(self, aid: int, cid: int) -> Transcript | None:
        """尝试拿字幕。拿不到返回 None，由调用方决定是否转写音频。"""
        try:
            tracks = await self.get_subtitle_tracks(aid, cid)
        except BiliApiError as exc:
            logger.warning("获取字幕轨道失败，将尝试 Whisper 兜底：%s", exc)
            return None

        if not tracks:
            logger.info(
                "该视频没有可用字幕轨道%s",
                "" if self.settings.has_bili_login else "（未配置 SESSDATA，B站AI字幕通常需要登录）",
            )
            return None

        track = pick_best_track(tracks)
        if track is None:
            return None

        try:
            segments = await self.fetch_subtitle(track)
        except Exception as exc:  # noqa: BLE001 - 单条轨道失败就换下一条
            logger.warning("下载字幕 %s 失败：%s", track.label, exc)
            return None

        logger.info("已获取字幕：%s（%d 条）", track.label, len(segments))
        return Transcript(
            segments=segments,
            source=track.source,
            language=track.lan,
            note=f"字幕轨道 {track.label}",
        )

    # ------------------------------------------------------------------
    # 登录状态自检
    # ------------------------------------------------------------------
    async def check_login(self) -> tuple[bool, str]:
        """检查 SESSDATA 是否有效 —— 用于 doctor 自检。"""
        if not self.settings.has_bili_login:
            return False, "未配置 BILI_SESSDATA：可能拿不到 B站AI字幕（会自动转写音频兜底）"
        try:
            data = await self._get_json(_API_NAV, use_cache=False)
        except BiliApiError as exc:
            return False, f"登录态校验失败：{exc}"
        d = data.get("data") or {}
        if d.get("isLogin"):
            self._logged_in = True
            return True, f"B站登录态有效（用户：{d.get('uname', '未知')}）"
        self._logged_in = False
        return False, "SESSDATA 已失效或无效，请重新获取"
