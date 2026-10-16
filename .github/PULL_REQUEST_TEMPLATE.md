<!-- 一个 PR 只做一件事；改动大就先开 issue 聊。 -->

## 这个 PR 做了什么

<!-- 一两句说清结果，比如「字幕拿不到时不再直接抛异常，改成回落到 Whisper」 -->

## 为什么

<!-- 触发它的真实场景/报错。为什么选这个做法，而不是另一种。 -->

## 怎么验证的

<!-- 贴命令和结果，比如： -->
<!-- .\.venv\Scripts\python.exe -m pytest -q  →  22 passed -->
<!-- .\.venv\Scripts\python.exe -m scripts.smoke_transcribe "BV..."  →  转写 15 段 / 39s -->

## 自查

- [ ] `.\.venv\Scripts\python.exe -m pytest -q` 全绿
- [ ] 新增/改动的行为有测试覆盖（只改文案可以不带）
- [ ] 没有硬编码个人路径（盘符、用户名）
- [ ] 没有把密钥、cookie、`.env` 提交进去
- [ ] 如果新增了外发请求：已更新 `docs/NETWORK.md` 与出网白名单测试
- [ ] 如果改了用户可见行为：已更新 `README.md` / `docs/INSTALL.md` / `CHANGELOG.md`
