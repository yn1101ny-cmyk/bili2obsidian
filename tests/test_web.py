"""Web 层测试。

最关键的一组是 **XSS 防护**：笔记里混着三类不可信文本
（B站视频标题、模型输出、ASR 转写），任何一处夹带 HTML/脚本
都不能被浏览器执行。

用 FastAPI 的 TestClient 跑，**不启动真实服务器、不联网**。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.schemas import NoteRecord, TranscriptSource
from app.storage.db import Database
from app.storage.vault import Vault
from app.web.markdown_render import render_markdown, safe_url, strip_frontmatter
from app.web.server import create_app


@pytest.fixture
def client(settings) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


# ===========================================================================
# Markdown 渲染安全性
# ===========================================================================


def test_escapes_script_tag():
    """⛔ 核心安全测试。

    视频标题里带 <script> 是完全可能的，绝不能被执行。
    """
    html = render_markdown("# 标题\n\n<script>alert(1)</script>\n")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_escapes_img_onerror():
    html = render_markdown('<img src=x onerror="alert(1)">')
    assert "<img" not in html
    assert "onerror" not in html or "&lt;img" in html


def test_escapes_iframe():
    html = render_markdown('<iframe src="http://evil"></iframe>')
    assert "<iframe" not in html


def test_escapes_event_handler_in_normal_text():
    """带 onclick 的标签必须被转义成纯文本，不能变成真元素。"""
    html = render_markdown('正文 <a href="#" onclick="steal()">点我</a>')
    # 标签本身被转义 ⇒ 浏览器只会当文字显示，onclick 不会生效
    assert "&lt;a href=" in html
    assert "<a href=" not in html


def test_keeps_blockquote_syntax():
    """⛔ 回归守卫：转义时**不能连 ``>`` 一起转**。

    ``>`` 是 Markdown 的引用块标记，我们笔记里所有的警示块都靠它。
    用 ``html.escape`` 会把 ``>`` 转成 ``&gt;``，引用块全部退化成普通段落。
    """
    html = render_markdown("> ⚠️ 本节由模型补充，未经验证\n")
    assert "<blockquote>" in html
    assert "本节由模型补充" in html


def test_blocks_javascript_url():
    html = render_markdown("[点我](javascript:alert(1))")
    assert "javascript:" not in html


def test_blocks_data_url():
    html = render_markdown("[点我](data:text/html;base64,PHNjcmlwdD4=)")
    assert "data:text/html" not in html


def test_keeps_normal_https_links():
    html = render_markdown("[原视频](https://www.bilibili.com/video/BV1xx)")
    assert 'href="https://www.bilibili.com/video/BV1xx"' in html


def test_keeps_bilibili_timestamp_links():
    html = render_markdown("[02:23](https://www.bilibili.com/video/BV1?t=143)")
    assert "?t=143" in html


def test_allows_relative_links():
    html = render_markdown("[索引](index.md)")
    assert "index.md" in html


def test_renders_headings_and_lists():
    html = render_markdown("# 大标题\n\n## 小节\n\n- 一\n- 二\n")
    assert "<h1>" in html
    assert "<h2>" in html
    assert "<li>" in html


def test_renders_tables():
    md = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    html = render_markdown(md)
    assert "<table>" in html


def test_renders_blockquote():
    html = render_markdown("> ⚠️ 注意\n")
    assert "<blockquote>" in html


def test_our_sections_survive_escaping():
    """我们自己的 Markdown 里只有斜体、引用、标题、列表，
    转义之后必须照常渲染出来。"""
    md = "## 🎯 一句话总结\n\n**结论**\n\n*✅ 视频原话*\n\n> ⚠️ 提示\n"
    html = render_markdown(md)
    assert "🎯 一句话总结" in html
    assert "<strong>结论</strong>" in html
    assert "<em>✅ 视频原话</em>" in html
    assert "<blockquote>" in html


def test_empty_input():
    assert render_markdown("") == ""


# ---------------------------------------------------------------------------
# safe_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://127.0.0.1:8765/x",
        "mailto:a@b.c",
        "#anchor",
        "/abs/path",
        "./rel",
    ],
)
def test_safe_url_allows(url):
    assert safe_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "  javascript:alert(1)  ",
        "data:text/html,x",
        "vbscript:msgbox",
        "file:///etc/passwd",
        "",
    ],
)
def test_safe_url_blocks(url):
    assert safe_url(url) == "#"


# ---------------------------------------------------------------------------
# frontmatter 剥离
# ---------------------------------------------------------------------------


def test_strip_frontmatter_removes_block():
    text = "---\ntitle: x\nbvid: y\n---\n\n# 正文\n"
    assert strip_frontmatter(text).strip() == "# 正文"


def test_strip_frontmatter_noop_without_block():
    text = "# 正文\n\n内容"
    assert strip_frontmatter(text) == text


def test_strip_frontmatter_unterminated_left_alone():
    text = "---\ntitle: x\n\n# 没有结束标记"
    assert strip_frontmatter(text) == text


# ===========================================================================
# 页面
# ===========================================================================


def test_index_page(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "贴一个 B站链接" in res.text


def test_index_has_no_external_resources(client):
    """⛔ 页面不能引用任何外部资源 —— 离线可用，也不增加出网点。"""
    html = client.get("/").text
    assert "http://" not in html.replace("http://127.0.0.1", "")
    assert "https://" not in html
    assert "cdn" not in html.lower()


def test_checks_page(client):
    res = client.get("/checks")
    assert res.status_code == 200
    assert "环境自检" in res.text


def test_healthz(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_static_css_served(client):
    res = client.get("/static/app.css")
    assert res.status_code == 200
    assert "--accent" in res.text


def test_static_js_served(client):
    res = client.get("/static/app.js")
    assert res.status_code == 200
    assert "/api/jobs" in res.text


# ===========================================================================
# 任务 API
# ===========================================================================


def test_create_job_rejects_bad_url(client):
    res = client.post("/api/jobs", json={"url": "这不是链接"})
    assert res.status_code == 400
    assert "无法解析" in res.json()["detail"]


def test_create_job_rejects_empty_url(client):
    assert client.post("/api/jobs", json={"url": ""}).status_code == 422


def test_create_job_rejects_when_llm_disabled(client, settings):
    settings.llm_provider = "none"
    res = client.post("/api/jobs", json={"url": "BV1GJ411x7h7"})
    assert res.status_code == 400
    assert "LLM_PROVIDER" in res.json()["detail"]


def test_create_job_accepts_valid_url_without_running_pipeline(
    client, settings, monkeypatch
):
    """只验证路由行为：把 JobManager.submit 换掉，避免真的跑流水线。"""
    from app.schemas import Job, JobStage

    # settings fixture 默认 llm_provider=none，路由会拒；这里打开
    settings.llm_provider = "ollama"

    def fake_submit(url):
        return Job(id="fakejob1", url=url, stage=JobStage.QUEUED, message="已加入队列")

    monkeypatch.setattr(client.app.state.jobs, "submit", fake_submit)

    res = client.post("/api/jobs", json={"url": "BV1GJ411x7h7"})
    assert res.status_code == 201
    body = res.json()
    assert body["id"] == "fakejob1"
    assert body["stage"] == "queued"
    assert body["stage_label"] == "排队中"
    assert body["done"] is False


def test_get_unknown_job_404(client):
    assert client.get("/api/jobs/nope").status_code == 404


def test_list_jobs_empty(client):
    assert client.get("/api/jobs").json() == []


def test_cancel_unknown_job_409(client):
    assert client.post("/api/jobs/nope/cancel").status_code == 409


# ===========================================================================
# 笔记 API
# ===========================================================================


def _make_note(settings, *, title="测试笔记", markdown="# 标题\n\n正文\n"):
    db = Database(settings.db_path)
    db.init()
    vault = Vault(settings)
    vault.ensure()

    path = vault.build_note_path("BV1GJ411x7h7", title)
    vault.write_note(path, markdown)
    record = NoteRecord(
        id="note1",
        bvid="BV1GJ411x7h7",
        title=title,
        one_liner="一句话总结",
        transcript_source=TranscriptSource.WHISPER,
        markdown_path=str(path),
    )
    db.save_note(record)
    return record


def test_list_notes_empty(client):
    assert client.get("/api/notes").json() == []


def test_list_notes_after_creating(client, settings):
    _make_note(settings)
    notes = client.get("/api/notes").json()
    assert len(notes) == 1
    assert notes[0]["title"] == "测试笔记"
    assert notes[0]["transcript_label"] == "本地Whisper转写"
    assert notes[0]["confidence"] == "中"


def test_get_note(client, settings):
    _make_note(settings)
    body = client.get("/api/notes/note1").json()
    assert body["bvid"] == "BV1GJ411x7h7"


def test_get_unknown_note_404(client):
    assert client.get("/api/notes/nope").status_code == 404


def test_note_markdown_endpoint(client, settings):
    _make_note(settings, markdown="# 标题\n\n正文内容\n")
    body = client.get("/api/notes/note1/markdown").json()
    assert "正文内容" in body["markdown"]


def test_note_page_renders(client, settings):
    _make_note(settings, markdown="# 我的标题\n\n正文内容\n")
    res = client.get("/notes/note1")
    assert res.status_code == 200
    assert "我的标题" in res.text
    assert "正文内容" in res.text


def test_note_page_strips_frontmatter(client, settings):
    md = "---\ntitle: x\nbvid: BV1\n---\n\n# 正文\n"
    _make_note(settings, markdown=md)
    html = client.get("/notes/note1").text
    # frontmatter 不该出现在页面上
    assert "bvid: BV1" not in html


def test_note_page_escapes_dangerous_title(client, settings):
    """⛔ 视频标题来自 B站，可能含 HTML。"""
    _make_note(settings, title="<script>alert(1)</script>", markdown="# 正文\n")
    res = client.get("/notes/note1")
    assert "<script>alert(1)</script>" not in res.text


def test_note_page_escapes_dangerous_body(client, settings):
    _make_note(settings, markdown="# 正文\n\n<script>alert(1)</script>\n")
    res = client.get("/notes/note1")
    assert "<script>alert(1)</script>" not in res.text


def test_note_page_404_for_unknown(client):
    assert client.get("/notes/nope").status_code == 404


def test_note_page_410_when_file_missing(client, settings):
    record = _make_note(settings)
    Vault(settings).note_path(record).unlink()
    assert client.get("/notes/note1").status_code == 410


def test_delete_note(client, settings):
    _make_note(settings)
    res = client.request("DELETE", "/api/notes/note1")
    assert res.status_code == 200
    assert res.json()["deleted"] is True
    assert client.get("/api/notes").json() == []


def test_delete_unknown_note_404(client):
    assert client.request("DELETE", "/api/notes/nope").status_code == 404


# ===========================================================================
# 自检与数据
# ===========================================================================


def test_api_checks_offline(client):
    body = client.get("/api/checks").json()
    assert body["status"] in ("ok", "warn", "fail")
    names = [c["name"] for c in body["checks"]]
    assert "数据目录" in names
    # 离线模式不该出现联网项
    assert "B站接口" not in names


def test_api_checks_redacts_secrets(client, settings):
    settings.bili_sessdata = "SUPER-SECRET-SESSDATA"
    body = client.get("/api/checks").json()
    import json as _json

    blob = _json.dumps(body, ensure_ascii=False)
    assert "SUPER-SECRET-SESSDATA" not in blob
    assert body["config"]["bili_sessdata"] == "***已设置***"


def test_data_stats(client, settings):
    _make_note(settings)
    body = client.get("/api/data/stats").json()
    assert body["notes"] == 1


def test_clear_data_requires_confirm(client):
    res = client.post("/api/data/clear", json={"confirm": ""})
    assert res.status_code == 400
    assert "DELETE" in res.json()["detail"]


def test_clear_data_with_wrong_confirm(client):
    res = client.post("/api/data/clear", json={"confirm": "delete"})
    assert res.status_code == 400


def test_clear_data_works_with_exact_confirm(client, settings):
    _make_note(settings)
    res = client.post("/api/data/clear", json={"confirm": "DELETE"})
    assert res.status_code == 200
    assert client.get("/api/notes").json() == []


# ===========================================================================
# 服务暴露面
# ===========================================================================


def test_app_refuses_to_start_on_non_loopback(tmp_path):
    """⛔ 数据安全底线：不允许绑定到局域网/公网地址。"""
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, host="0.0.0.0", data_dir=tmp_path / "d")
