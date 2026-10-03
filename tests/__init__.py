"""测试包。

有这个 ``__init__.py``，pytest 才会把项目根目录加入 ``sys.path``，
从而既能 ``import app``，也能在测试间共享 ``conftest`` 里的假实现。
"""
