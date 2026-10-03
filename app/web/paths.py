"""Web 层的路径常量。

单独放一个模块是为了**避免循环导入**：
``server.py`` 需要 ``routes.py``，而 ``routes.py`` 也需要模板目录，
两边都从这里取就不会互相 import。
"""

from __future__ import annotations

from pathlib import Path

#: app/web/
WEB_DIR = Path(__file__).resolve().parent
#: app/web/templates/
TEMPLATES_DIR = WEB_DIR / "templates"
#: app/web/static/
STATIC_DIR = WEB_DIR / "static"

__all__ = ["WEB_DIR", "TEMPLATES_DIR", "STATIC_DIR"]
