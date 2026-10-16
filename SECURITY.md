# 安全策略

## 支持的版本

只有 `main` 分支的最新提交会被修复。这是个人项目，不维护历史版本的分支补丁。

## 报告问题

**请不要开公开 issue 报告安全问题**，改用 GitHub 的
[私密漏洞报告](https://docs.github.com/zh/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
（仓库页 → Security → Report a vulnerability）。

报告中请写清：

1. 受影响的版本或提交号（`git rev-parse HEAD`）；
2. 复现步骤，或最小可复现示例；
3. 影响面：能读到什么、能改到什么、是否需要本地权限；
4. 如果是密钥泄露，**不要贴出密钥本身**，只说明文件与位置。

会尽快确认；修复后会在 `CHANGELOG.md` 里写明。

## 使用这个项目时请自己注意的几件事

这个工具是**本地优先**的：默认只监听 `127.0.0.1`、默认用本机 Ollama、
云端大模型需要同时显式打开两个开关（`LLM_PROVIDER=openai` 且 `ALLOW_CLOUD=true`）。
但仍然有几条红线：

- **`.env` 永远不要提交、不要截图、不要贴群里。** 里面的 `BILI_SESSDATA` 是
  等价于登录态的账号凭证（有效期约 30 天），拿到它的人可以操作你的 B站账号。
  建议用小号；怀疑泄露就去 B站退出登录使旧凭证失效。
- 不要把 `HOST` 改成 `0.0.0.0` 暴露到公网：本项目没有鉴权，
  拿到地址的人都能用你的机器跑转写、读你的笔记目录。
- `data/` 里存着转写文本、画面文字、视频缓存与生成的笔记，
  里面可能有别人不想外传的内容；分享日志或贴 issue 前先看一眼。
- 第三方依赖（`faster-whisper`、`yt-dlp`、`rapidocr` 等）各有自己的许可证与
  安全公告，请自行跟进更新。
