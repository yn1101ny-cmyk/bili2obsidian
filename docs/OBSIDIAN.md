# 在 Obsidian 里用

这个项目从设计上就是**为 Obsidian 准备的**，只是它不依赖 Obsidian：

* 输出是**纯 Markdown + YAML frontmatter**，没有私有格式、没有数据库、不联网；
* 笔记里的时间戳是标准 Markdown 链接（`[12:34](https://www.bilibili.com/video/BVxxx?t=754)`），
  在 Obsidian 里点一下就能跳回 B站对应秒数；
* 标签写在 frontmatter 的 `tags:` 里，Obsidian 会自动识别。

所以「接上 Obsidian」不需要装任何插件，只需要让**笔记文件出现在库目录里**。
下面三种接法，从最省事到最干净。

---

## 一、最快：把笔记目录放进库里（3 步）

1. 在库里建一个专门文件夹，比如 `_mynote`。

   ⚠️ **不要用点开头的名字**（如 `.mynote`）——Obsidian 会直接忽略以 `.` 开头的文件夹。

2. 打开项目里的 `.env`，把数据目录指过去：

   ```ini
   DATA_DIR=D:\MyVault\_mynote
   ```

3. 重新启动（双击 `启动笔记.bat`）。之后每生成一篇笔记，它就在
   `D:\MyVault\_mynote\notes\` 里，Obsidian 立刻能搜到。

### ⚠️ 这一种做法要注意「顺带被同步的东西」

`DATA_DIR` 下面不只有笔记：

| 子目录 | 内容 | 体积 |
| --- | --- | --- |
| `notes/` | 生成的 Markdown 笔记 | 几十 KB |
| `media/` | 下载的音频 | 每视频几 MB |
| `cache/` | 元数据、字幕、抽帧缓存 | 每视频几 MB |
| `models/` | Whisper 模型权重 | 几百 MB ～ 2 GB |

把它们一起放进库，会被 Obsidian 全文索引、被同步盘（OneDrive / 坚果云 / Obsidian Sync）
一起同步上去。两种对策，选一个：

* **在 Obsidian 里排除**：设置 → 文件与链接 → 已排除的文件，加入
  `_mynote/models`、`_mynote/media`、`_mynote/cache`；用 Obsidian Sync 的话，
  在 Sync 设置里排除同样三个文件夹。
* **只把 notes 放进库**（推荐，见下）。

---

## 二、推荐：数据留在库外，只把 `notes` 链接进库

模型、音频、缓存放库外，库里只出现笔记文件。Windows 上用「目录联结」一步搞定：

```powershell
# 1) 数据目录留在库外
#    .env:  DATA_DIR=D:\MyNoteAppData

# 2) 把它的 notes 子目录链接成库里的一个文件夹
mklink /J "D:\MyVault\视频笔记" "D:\MyNoteAppData\notes"
```

`mklink /J` 是 NTFS 目录联结，不需要管理员权限，Obsidian 与资源管理器都当普通文件夹。
删掉库里的链接不会删数据（删的是链接本身）。

Linux / macOS 用符号链接：

```bash
ln -s /home/me/mynote-data/notes "/home/me/MyVault/视频笔记"
```

---

## 三、零配置：生成完自己搬

不想动配置就每次手动搬：

```powershell
python -m scripts.mynote "https://www.bilibili.com/video/BVxxxxxxxxx"
# 命令会打印生成的 Markdown 路径，复制进库即可
```

适合偶尔用一次；天天用还是前两种省事。

---

## 四、生成出来的笔记长什么样

frontmatter（全部字段都可用于 Dataview 查询）：

| 字段 | 例子 | 说明 |
| --- | --- | --- |
| `title` | 如何用 xxx 做 yyy | 笔记标题 |
| `goal` | 看完能自己写好一个 FastAPI 健康检查 | 这篇要让你做到什么 |
| `bvid` | BV1xxxxxxxxx | B站视频号，方便回跳与去重 |
| `source_url` | https://www.bilibili.com/video/BV… | 原视频 |
| `up` | 某某某 | UP 主 |
| `duration` | 12:34 | 时长 |
| `transcript_source` | B站AI字幕 / 本地Whisper转写 | 转写来源（可信度自评的依据） |
| `transcript_confidence` | 高 / 中 / 低 | 转写可信度 |
| `chapters` | 00:00 开场 … | 视频自带的分P/章节（有才有） |
| `created` | 2026-10-16 22:41:03 | 生成时间 |
| `tags` | bilibili, 视频笔记, … | 固定带 `bilibili` 与 `视频笔记`，其余由模型给 |

正文结构：`# 标题` → UP/时长/转写来源信息条 → 🎯 读完能做到 → 时间戳大纲 →
重点 → 能立刻照做的东西 → 存疑与待验证。**凡是自动转写来的内容，
笔记里会明确标注「可能有同音字/专有名词错误」**。

---

## 五、Dataview 起步

装了 [Dataview](https://github.com/blacksmithgu/obsidian-dataview) 之后，
在任意笔记里粘一段就能用（路径改成你自己的文件夹）：

````markdown
```dataview
TABLE duration AS 时长, up AS UP主, transcript_confidence AS 可信度, created AS 生成时间
FROM "_mynote/notes"
SORT created DESC
LIMIT 20
```
````

几个常用的：

````markdown
```dataview
# 只看 B站 AI 字幕（准确度最高的那一档）
TABLE up AS UP主, duration AS 时长
FROM #视频笔记
WHERE transcript_source = "B站AI字幕"
SORT created DESC
```

```dataview
# 按 UP 主分组，看看谁的内容你收得最多
GROUP BY up
FROM #视频笔记
```

```dataview
# 待读：Goal 写了但还没读的（自己读完把 status 改成 done 之类即可）
LIST goal
FROM #视频笔记
WHERE !contains(tags, "已读")
```
````

---

## 六、和 Obsidian 插件配合

| 你想要 | 用什么 | 说明 |
| --- | --- | --- |
| 把笔记汇成清单/看板 | Dataview | 只读查询，最省心 |
| 一键触发「把这个链接做成笔记」 | Templater / QuickAdd | 需要在 user function 里调用外部命令（`child_process`），只有桌面版可用 |
| 让 AI 直接在库里读写笔记 | Local REST API + MCP | 我自己的库就是这么用的：Obsidian 的 Local REST API 插件开 HTTP 端口，DSH/Claude 这类客户端通过 MCP 连上去读写 |
| 笔记里嵌关键画面截图 | `scripts/v_shots.py` | 截图落到 `附件/转写/<bvid>/shots/`，笔记里写 `![[shot_xxxx.jpg|560]]` |

> **想让 AI 直接在 Obsidian 里写笔记**（而不是在外面生成再丢进来）：
> 装 Obsidian 的 **Local REST API** 插件 → 开启 HTTP 端口 → 在支持 MCP 的
> AI 客户端里把 `http://127.0.0.1:<端口>/mcp/` 配上。这条链路和 MyNoteApp
> 是互补的：MyNoteApp 负责「视频 → 文字稿/截图/初稿」，
> MCP 负责「AI 在你的库里整理、归档、双链」。

---

## 七、两个进阶脚本（可选，不装也能用主流程）

```powershell
# 1) 抓文字稿：优先用 B站官方字幕（要 BILI_SESSDATA），拿不到才回落本地 Whisper
python -m scripts.v_extract "https://www.bilibili.com/video/BVxxxxxxxxx" --outdir "D:\MyVault\附件\转写\BVxxxxxxxxx"

# 2) 挑关键画面截图（复用上一步的抽帧缓存，几秒完成）
python -m scripts.v_shots BVxxxxxxxxx --outdir "D:\MyVault\附件\转写\BVxxxxxxxxx"
```

产物结构与笔记里的引用方式：

```
附件/转写/BVxxxxxxxxx/
├─ 转写原文.txt        带 [mm:ss] 时间戳
├─ 画面文字.txt        屏幕上的文字（OCR）
├─ 元数据.json         标题/UP/时长/标签
└─ shots/
   ├─ shot_BVxxxxxxxxx_01_0000s.jpg
   ├─ overview_BVxxxxxxxxx.jpg     4 列总览图
   └─ 截图清单.md                  每张图 + 它上面的画面文字
```

在笔记里引用（Obsidian 的嵌入语法）：

```markdown
![[附件/转写/BVxxxxxxxxx/shots/shot_BVxxxxxxxxx_03_0215s.jpg|560]]
```

---

## 八、常见坑

* **点开头的文件夹会被忽略**。`.mynote` 在 Obsidian 里根本看不见，用 `_mynote` 或 `mynote`。
* **同步盘别放模型**。`models/` 有几百 MB 到 2 GB，放进同步盘会拖慢所有设备（见第一节表格）。
* **中文文件名 + Git**：如果库用 Git 管理，建议
  `git config core.quotepath false`，否则中文名在 `git status` 里显示成八进制转义。
* **同名文件**：项目用「标题 + 清理过的文件名」命名，同名会覆盖；重要笔记建议在 Obsidian 里
  改成你自己习惯的文件名（链接不会断，因为 Obsidian 按链接重写）。
* **时间戳链接**：是标准 Markdown 链接，不是 wiki 链接，任何 Markdown 阅读器都能用，
  在 Obsidian 里也能点（桌面版会在浏览器打开对应秒数）。
* **别把 `data/` 提交进库的 Git 仓库**：笔记库的 `.gitignore` 里加上 `_mynote/models/`、
  `_mynote/media/`、`_mynote/cache/`（`notes/` 是否纳入版本管理由你决定）。
