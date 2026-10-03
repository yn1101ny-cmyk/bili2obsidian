"""把 Markdown 渲染成**安全**的 HTML。

为什么能这么简单就安全：``app/notes/render.py`` 刻意不产出任何原始 HTML
（曾经用过 ``<sub>``，现在全换成 Markdown 原生斜体）。因此这里可以对整份
文档做**统一转义**，再交给 Markdown 转换器 —— 转义之后不可能再有标签
被浏览器解析，从根上杜绝注入。

这一点很重要，因为笔记里混着三类不可信文本：
  * 视频标题与 UP 主名（来自 B 站，用户无法控制）；
  * 模型生成的正文（可能复述标题里的怪字符）；
  * 转写文本（ASR 输出，什么都可能有）。

链接单独处理：Markdown 生成的 ``<a href>`` 是我们自己放进去的，
但 href 里可能含 ``javascript:`` 之类，所以再过一遍 ``_safe_url``。
"""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: 允许出现在链接里的协议
_ALLOWED_SCHEMES = frozenset({"http", "https", "mailto"})

#: Markdown 扩展：
#:   extra        —— 表格、围栏代码块、删除线等（索引页的表格要用）
#:   sane_lists   —— 列表缩进行为更符合直觉
#:   nl2br        —— 单换行也断行；转写全文是一行一句，靠它排版
_EXTENSIONS = ["extra", "sane_lists", "nl2br"]

_HREF_RE = re.compile(r'(<a\s[^>]*href=")([^"]*)(")', re.IGNORECASE)


def safe_url(url: str) -> str:
    """只放行安全协议的链接，其余一律替换成 ``#``。

    判断依据是**协议**而不是字符串前缀：没有协议的相对链接
    （``index.md`` / ``/abs`` / ``#anchor``）是安全的，直接放行；
    有协议就必须在白名单里。
    """
    candidate = (url or "").strip()
    if not candidate:
        return "#"
    try:
        scheme = urlparse(candidate).scheme.lower()
    except ValueError:
        return "#"
    if not scheme:
        # 相对链接：没有协议就没有注入空间
        return candidate
    if scheme in _ALLOWED_SCHEMES:
        return candidate
    logger.warning("拦截了不安全协议的链接：%s", candidate[:80])
    return "#"


def _escape_for_markdown(text: str) -> str:
    """转义会构成 HTML 标签的字符，同时**保留 Markdown 语法**。

    ⚠️ 只转义 ``&`` 和 ``<``，**不转义 ``>``**：

    * 要形成标签必须有 ``<``，把它转掉就不会有任何标签被解析；
    * 而 ``>`` 是 Markdown 的引用块语法 —— 一起转掉的话
      ``> ⚠️ 提示`` 会变成普通段落，我们笔记里所有的警示块都会失效。

    （``html.escape`` 默认连 ``>`` 也转，所以这里不能用它。）
    """
    return text.replace("&", "&amp;").replace("<", "&lt;")


def _sanitize_hrefs(rendered: str) -> str:
    """把渲染结果里所有 ``href`` 过一遍白名单。"""
    return _HREF_RE.sub(
        lambda m: f"{m.group(1)}{html.escape(safe_url(html.unescape(m.group(2))), quote=True)}{m.group(3)}",
        rendered,
    )


def render_markdown(text: str) -> str:
    """Markdown -> 安全 HTML。

    :param text: 原始 Markdown（可能含不可信内容）
    :returns: 可安全插入页面的 HTML 片段
    """
    if not text:
        return ""

    try:
        import markdown as md
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确指引
        raise RuntimeError(
            "未安装 markdown。请执行：pip install -r requirements.txt"
        ) from exc

    # 关键一步：先转义会构成标签的字符，再做 Markdown 转换。
    # 之后即使原文里写了 <script>，也只会以纯文本形式显示出来。
    escaped = _escape_for_markdown(text)

    rendered = md.markdown(
        escaped,
        extensions=_EXTENSIONS,
        output_format="html",
    )
    return _sanitize_hrefs(rendered)


def strip_frontmatter(text: str) -> str:
    """去掉开头的 YAML frontmatter，避免在页面上重复显示元信息。"""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4 :].lstrip("\n")


# ---------------------------------------------------------------------------
# 目录（左侧导航用）
# ---------------------------------------------------------------------------

_H2_RE = re.compile(r"<h2(\s[^>]*)?>(.*?)</h2>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

#: 目录项用的短标题长度上限
_TOC_TITLE_LIMIT = 20


def strip_leading_heading(markdown: str) -> str:
    """去掉正文开头的那个 ``# 标题``。

    笔记 Markdown 文件的第一行是 ``# 标题``（给 Obsidian 之类工具用的），
    但网页顶部已经用 ``<h1>`` 显示标题了（旁边还有来源、时间、操作按钮）——
    正文再渲染一遍就会变成**两个一模一样的标题上下叠着**，看着像文字重叠。

    所以只剥掉**紧跟在 frontmatter 之后的第一行一级标题**，
    文件本身保持不动；正文中间出现的 ``#`` 也不会被误伤。
    """
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # 只有形如 `# 标题` 的一级标题才剥；`##` 是正文小节，必须保留
        if re.match(r"^#\s+\S", stripped):
            remainder = lines[index + 1 :]
            # 连带去掉紧随其后的空行，避免开头留一大块空白
            while remainder and not remainder[0].strip():
                remainder.pop(0)
            return "\n".join(remainder)
        break  # 第一行有效内容不是一级标题，就什么都不动
    return markdown


def add_heading_anchors(rendered: str) -> tuple[str, list[dict[str, str]]]:
    """给渲染结果里的 ``<h2>`` 加上锚点 id，并返回目录。

    左侧导航 + 滚动监听需要「段落可寻址」。
    因为我们完全控制笔记的 Markdown 结构（小节一律用 ``##``），
    所以直接对 ``<h2>`` 做处理是可靠的。

    :returns: ``(替换后的 HTML, [{"id", "title"}, ...])``
    """
    toc: list[dict[str, str]] = []
    counter = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        anchor = f"sec-{counter}"
        inner = match.group(2)

        # 目录里显示纯文本（去掉可能的标签），并且要反转义 ——
        # 模板会再做一次转义，不反转义会出现 &amp;amp; 这种双重转义
        title = html.unescape(_TAG_RE.sub("", inner)).strip()
        if len(title) > _TOC_TITLE_LIMIT:
            title = title[: _TOC_TITLE_LIMIT - 1] + "…"
        toc.append({"id": anchor, "title": title})

        return f'<h2 id="{anchor}">{inner}</h2>'

    return _H2_RE.sub(replace, rendered), toc


def render_note_body(markdown: str) -> tuple[str, list[dict[str, str]]]:
    """笔记正文一步到位：去 frontmatter → 去重复标题 → 转安全 HTML → 加锚点。

    ⚠️ 顺序有讲究：必须**先去掉正文里的一级标题**再渲染，
    否则页面顶部和正文会出现两个相同的 ``<h1>``，看起来就是标题重叠。
    """
    body = render_markdown(
        strip_leading_heading(strip_frontmatter(markdown))
    )
    return add_heading_anchors(body)


__all__ = [
    "render_markdown",
    "render_note_body",
    "add_heading_anchors",
    "strip_frontmatter",
    "strip_leading_heading",
    "safe_url",
]
