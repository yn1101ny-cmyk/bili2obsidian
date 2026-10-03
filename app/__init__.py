"""MyNoteApp —— 本地优先的「B站视频 → 可理解笔记」应用。

数据安全约束（贯穿全部模块）：
  * 所有用户数据落在本机 ``data/`` 目录，不上传。
  * 出网请求仅允许出现在 ``app.bili.client`` / ``app.media.downloader`` /
    ``app.llm.*`` / ``app.asr.whisper_engine``（模型下载）四处，
    由 ``tests/test_privacy.py`` 静态强制校验。
  * 云端 LLM 默认关闭，需显式设置 ``ALLOW_CLOUD=true`` 才会启用。
"""

__version__ = "0.1.0"
