# 这个项目里值得带走的东西

一份「踩过才知道」的清单。每条都尽量写清楚**现象 → 原因 → 怎么办**，
因为光记结论很容易在下次换个场景就失效。

标注说明：
* 📐 **原理** —— 换个项目也成立
* 🕳️ **坑** —— 我实际踩了才发现
* 🔢 **实测数据** —— 这台机器上量出来的，别的机器会有出入

---

# 一、Web 开发

## 1.1 分层：按「职责」而不是按「文件类型」分目录

```
app/
  bili/       抓 B站数据（唯一允许出网的地方之一）
  media/      下载器（音频 / 低清视频）
  asr/        语音转写
  vision/     画面识别（抽帧 + OCR）
  notes/      把转写变成笔记（提示词 / 管线 / 渲染）
  llm/        模型提供方（本地 Ollama / 可选云端）
  storage/    SQLite + 文件仓库
  web/        HTTP 层（路由 / 模板 / 静态资源）
```

📐 **关键是「谁依赖谁」有方向**：`web → notes → llm`，
反过来不行。

🕳️ 有个具体体现：`notes/` 需要知道「画面上有什么文字」，
但我**没有**让它去 `import vision`，而是定义了一个朴素类型：

```python
# app/notes/pipeline.py
ScreenBlock = tuple[float, str]   # (时间戳秒, 文本)
```

由上层（`app/pipeline.py`）负责把 `OnScreenText` 转成它。
这样 `notes/` 不用知道 OCR 的存在，测试里直接塞元组就行，
也不用为了测笔记渲染去装 400MB 的 OCR 依赖。

## 1.2 Web 层安全：靠一条「不变量」而不是到处转义

🕳️ 常规做法是「在哪显示就在哪转义」，但很容易漏。

📐 这个项目反过来：**渲染器保证不产出任何原始 HTML**，
于是 Web 层可以放心地对整份文档做**统一转义**。

```python
# app/notes/render.py —— 有测试守着这条不变量
def test_output_contains_no_raw_html(note, meta, transcript):
    md = render_note_markdown(note, meta, transcript)
    assert not re.search(r"<[a-zA-Z/][^>]*>", md), "渲染器产出了原始 HTML"
```

🕳️ 但统一转义也有个坑：**`html.escape()` 会把 `>` 也转义**，
而 `>` 是 Markdown 的引用语法（`> 提示`），一转义全废。
所以只能转义 `&` 和 `<`：

```python
def _escape_for_markdown(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;")   # 注意：没有 >
```

另外链接协议要白名单（`javascript:` 一律拦掉）。

## 1.3 前后端交互：长任务用「提交 + 轮询」，不要一个请求等到底

笔记生成要几分钟，HTTP 请求挂那么久体验很差、还容易被中间层掐断。

📐 拆成两个接口：

```
POST /api/jobs          → 立刻返回 {id, stage, progress}
GET  /api/jobs/{id}     → 前端每秒轮询一次
POST /api/jobs/{id}/cancel
```

服务端用一个阶段状态机描述进度，**阶段名和文案放在枚举里**，
前端直接用，不用自己拼字符串：

```python
class JobStage(str, Enum):
    RESOLVING = "resolving"       # 解析视频信息
    TRANSCRIBING = "transcribing"
    VISION = "vision"             # 识别画面文字
    SUMMARIZING = "summarizing"
    ...

    @property
    def label(self) -> str:
        return {"resolving": "解析视频信息", ...}[self.value]
```

🕳️ 前端刷新页面后进度就丢了。所以启动时**主动查一次有没有在跑的任务**
并接上进度 —— 否则用户一刷新就以为白跑了。

## 1.4 CSS：设计令牌 + 把设计规范写成测试

📐 颜色、间距、尺寸全部用 CSS 变量，不要在几十个地方写魔法数字：

```css
:root {
  --primary: #F97316;  --bg: #FFFBF5;  --text: #1C1917;  --border: #E7E5E4;

  /* 8pt grid：所有间距都是 8 的倍数 */
  --sp-1: 8px;  --sp-2: 16px;  --sp-3: 24px;  --sp-4: 32px;
  --sp-6: 48px; --sp-8: 64px; --sp-12: 96px;

  --header-h: 64px;      /* 吸顶栏高度，别的地方要引用它 */
  --sidebar-w: 240px;
}
```

🕳️ **「规范」只写在文档里必然会烂掉。** 把配色和网格做成测试：

```python
def test_spacing_declarations_snap_to_eight():
    """padding / margin / gap 里的 px 必须是 8 的倍数。"""
    for prop in ("padding", "margin", "gap"):
        for value in 找出所有声明(css, prop):
            for px in 提取长度(value):
                assert px % 8 == 0, f"{prop}: {value} 不在 8pt 网格上"
```

我随手写个 `padding: 10px`，测试立刻就红了。这比 code review 可靠。

## 1.5 吸顶元素（sticky）：三个必踩的坑

### 坑一：锚点跳转会被吸顶栏盖住

🕳️ 点左侧导航跳到某小节，标题正好藏在吸顶栏后面。

📐 一行 CSS 解决，**高度用变量而不是写死**：

```css
.markdown h2 { scroll-margin-top: calc(var(--header-h) + var(--sp-3)); }
```

窄屏时上面还多一条横向吸顶导航，所以要覆盖：

```css
@media (max-width: 1024px) {
  .markdown h2 { scroll-margin-top: calc(var(--header-h) + var(--sp-8)); }
}
```

### 坑二：窄屏时侧栏会压住内容

📐 宽屏是「左导航 + 右内容」两栏；窄屏要让它**从 sticky 侧栏变回横向胶囊导航**，
并且给不透明背景 —— 否则内容会从它下面透出来。

### 坑三：半透明吸顶栏会让内容「透出来」

🕳️ 顶栏用了 `rgba(255,251,245,0.88)` + 毛玻璃。标题往上滚到顶栏下面时
**能透出来**，和「MyNoteApp」「首页」这些字混在一起 ——
看起来就是**文字重叠**。

📐 毛玻璃好看，但吸顶栏的透明度**至少要 0.95**。我改成 0.97，
视觉上还是有层次，但已经看不出底下的内容了。同样加了测试守着。

## 1.6 滚动监听：别用 IntersectionObserver 收尾

📐 常见做法是 `IntersectionObserver`。但它有个经典问题：
**滚到页面底部时，最后几个小节可能永远不触发**，
左侧导航的最后一个条目点不亮。

改用「激活线」判定更简单也更可靠：

```js
function updateActive() {
  const line = headerHeight() + 24;      // 顶栏下方 24px 处
  let current = headings[0];
  for (const h of headings) {
    if (h.getBoundingClientRect().top - line <= 0) current = h;
    else break;                          // 落在激活线上方的最后一个标题
  }
  // 滚到底时强制高亮最后一项
  const atBottom = innerHeight + scrollY >= documentElement.scrollHeight - 4;
  if (atBottom) current = headings[headings.length - 1];
  setActive(current.id);
}
```

配合 `requestAnimationFrame` 节流，`scroll` 监听用 `{ passive: true }`。

点击时**立刻**高亮，不要等滚动结束才更新，否则有明显延迟感。

## 1.7 🔢 布局验证：用 CDP 量，不要用眼睛看截图

📐 这是我觉得最值的一条方法。截图只能看个大概，重叠、溢出、
行高被压扁这类问题很容易漏；而且我用的模型**根本看不了图片**。

改用 Chrome DevTools 协议**直接量 `getBoundingClientRect()`**：

```python
# 连上无头 Edge 的调试端口，然后
await ws.send(json.dumps({
    "id": 1, "method": "Runtime.evaluate",
    "params": {"expression": SCAN_ALL, "returnByValue": True},
}))
```

`SCAN_ALL` 在页面里做四件事：

1. 取所有**含自有文字**的元素，两两做几何相交检测（跳过祖先/后代）
2. 检测内容溢出容器（`scrollWidth > clientWidth`）
3. 检测行高小于字号（文字会被压扁重叠）
4. 检测相邻且完全相同的标题（同一段文字渲染两遍）

实测输出：

```
（已扫描 171 个文字元素，其中文档流内 159 个；h1 × 1）
✅ 无文字重叠   ✅ 顶栏不透明（alpha=0.97）
✅ 无内容溢出   ✅ 无行高异常   ✅ 无重复标题
```

🕳️ **两个必须注意的细节，否则会误报：**

* **吸顶浮层不算重叠。** 顶栏和吸顶导航**设计上就是要浮在内容之上**。
  要跳过「位于 `position: sticky/fixed` 祖先内」的元素。
  （但反过来要检查它的**背景是否够不透明** —— 那才是真问题。）
* **横向滚动的容器要往上找祖先。** 胶囊导航里溢出的是内层 `<ul>`，
  负责滚动的是外层容器。只看元素自身会误报一片。

## 1.8 🕳️ Windows 上跑无头浏览器做检查的三个坑

**坑一：`--window-size` 不等于视口宽度。**
Windows 上 Edge 有最小窗口宽度，传 `390` 实际会变成 `489` ——
于是「窄屏检查」根本没在窄屏下跑，数据全是假的。
必须用 CDP 的 `Emulation.setDeviceMetricsOverride` 强制视口。

**坑二：`Popen.terminate()` 只杀父进程。**
渲染器 / GPU / 网络等子进程会留下来，占着 `--user-data-dir` 目录。
攒到 **17 个**之后，新的无头实例就再也起不来，
报「调试端口没起来」而且看不出原因。要用 `taskkill /F /T /PID`。

**坑三：`--user-data-dir` 必须是绝对路径。**
传相对路径时 Edge 按它自己的工作目录解析，同样起不来。
而且要**每次用不同的目录**，避免上一次没退干净时互相干扰。

## 1.9 🕳️ 同一个标题被渲染了两遍

用户反馈「标题出现重叠文字」，查下来是：

* 笔记 Markdown 文件开头有 `# 标题`（给 Obsidian 用）
* 页面上又用 `{{ record.title }}` 渲染了一次

结果页面里出现**两个一模一样的 `<h1>`**，上下紧挨着。

📐 教训：**模板和内容同时描述同一个东西时，一定要想清楚谁负责显示。**
这里选择在 Web 渲染时剥掉正文里的一级标题（文件本身不动，别的工具还要用）：

```python
def strip_leading_heading(markdown: str) -> str:
    """只剥紧跟在 frontmatter 之后的第一行 `# xxx`。"""
```

注意 `##` 小节、正文中间的一级标题、`#标签`（# 后没空格）都不能误伤。

## 1.10 🕳️ Windows 脚本的编码是一场噩梦

**`.bat` 必须纯 ASCII。** cmd 按系统代码页解析批处理文件，
中文很容易乱码甚至导致命令解析失败。中文提示全放到 `.ps1`：

```bat
chcp 65001 >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch.ps1"
```

**但 `.ps1` 必须带 UTF-8 BOM。** Windows PowerShell 5.1 对
**没有 BOM** 的脚本按系统代码页（GBK）读取，UTF-8 的中文会变成乱码，
而且**乱码字节能直接破坏字符串解析** —— 实测报出「字符串缺少终止符」，
整个脚本语法都不成立。

验证方法：

```python
raw = Path("scripts/launch.ps1").read_bytes()
assert raw.startswith(b"\xef\xbb\xbf"), "缺 BOM，PowerShell 5.1 会把中文读成乱码"
```

---

# 二、AI 部署

## 2.1 本地推理栈：版本匹配比性能调优重要得多

这个项目用到两条本地推理链路：

| 用途 | 技术栈 | 关键约束 |
|---|---|---|
| 语音转写 | faster-whisper + ctranslate2 + CUDA | **必须 `compute_type="float16"`** |
| 画面 OCR | RapidOCR + ONNXRuntime | **必须显式设线程数** |
| 生成笔记 | Ollama | **必须设 `num_predict`** |

🕳️ **三个「必须」全是我踩出来的**，下面分别说。

## 2.2 🕳️ CUDA 算力级别：`compute_type` 不能交给 `auto`

RTX 5060 Laptop 是 Blackwell（sm_120）。用 `compute_type="auto"` 时
ctranslate2 会选 int8，直接崩：

```
CUBLAS_STATUS_NOT_SUPPORTED
```

📐 新卡（新算力级别）上要**显式指定 `float16`**，别信 `auto`。
另外 `av`（PyAV）要钉在 `>=11,<19` —— PyAV 19 移除了
faster-whisper 用到的 `metadata_errors` 参数。

🔢 效果：20 分钟视频 GPU 转写 **27~36 秒**（33~45 倍实时）。

## 2.3 🕳️ ONNXRuntime 的线程数：用满核心反而最慢

这是最反直觉的一条。同一台机器（Intel 8P+16E 混合架构）跑 OCR：

| `intra_op_num_threads` | 每帧耗时 |
|---|---|
| 24（= 逻辑核数） | **2.881 s** |
| 8（≈ 性能核数） | **0.820 s** |

🔢 **差 3.5 倍。** 原因是线程池栅栏被 E-core 拖住。

📐 教训：**混合架构 CPU 上不要用 `os.cpu_count()` 去设线程数**，
那是逻辑核数。要按物理性能核来，保守取一半并夹在合理区间：

```python
def suggested_threads() -> int:
    logical = os.cpu_count() or 4
    return max(4, min(12, logical // 2))
```

而且这个参数**必须显式传**，默认值不一定是好的：

```python
RapidOCR(params={"EngineConfig.onnxruntime.intra_op_num_threads": self.threads})
```

## 2.4 🕳️ 生成长度必须有上限（`num_predict`）

**现象**：一个 19 分 50 秒的视频走单次生成，**跑了 731 秒然后超时失败**，
什么都没产出。

**原因**：Ollama 请求里没设 `num_predict`，生成长度只受 `num_ctx` 约束。
小模型一旦陷入重复，会一直吐到吃满上下文。

排除了其他可能：
* 提示词 7,208 tokens、schema 1,118 tokens、`num_ctx` 16,384 → **只占 44%，不是溢出**
* 直接调用同一段生成逻辑是成功的 → 不是代码逻辑问题

**修法**：加上 `num_predict` 上限。最坏情况从「跑十分钟然后失败」
变成「很快返回一段被截断的内容」—— 而截断的 JSON 通常还能解析出**部分**结果，
好过彻底失败。

```python
"options": {
    "temperature": temperature,
    "num_ctx": self.num_ctx,
    "num_predict": self.num_predict,   # ⚠️ 不要设成 -1
}
```

**补充（上限别设太小）**：一开始上限是 4096，够防“吐到超时”，但对**中文 JSON**
仍然偏紧 —— 实测一条 6 分半的视频，模型写到 `actionables` 中途就撞上限，
两次尝试都没能给出合法 JSON（`LLMOutputInvalid`）。现在默认 8192。
上限是“防失控”的护栏，不是“省时间”的开关：定得太低，正常长度的笔记也会
被一刀截断。

## 2.5 `num_ctx` 与显存：大窗口不一定跑得动

📐 上下文窗口不是免费的。开 16,384 的窗口时，KV cache 本身就要几个 GB。
8GB 显存上 8B 模型 + 大窗口很可能**溢出到 CPU**，
速度掉一个数量级，表现就是「莫名其妙变慢 / 超时」。

排查方法：`ollama ps` 看模型是不是掉到 CPU 上跑了。

## 2.6 结构化输出：用 JSON Schema 约束解码

Ollama 的 `format` 参数可以直接传 JSON Schema，
模型会用语法约束解码，输出结构可靠得多。

🕳️ **但 pydantic 生成的 schema 带 `$ref`，Ollama 支持不稳定。**
要自己展平：

```python
def flatten_schema(model: type[BaseModel]) -> dict:
    """把 $defs / $ref 展平成自包含 schema。"""
```

📐 展平时有三个必须注意的点：

**一、不许过滤 `properties` 下面的键。** 这些是**属性名**，不是注释。
我曾经把 schema 关键字和属性名混在一起过滤，
结果一个叫 `title` 的字段被整条删掉了。属性名只对它的**子 schema** 递归处理。

**二、必填字段只收紧「非空字段」。** 把全部字段都设成必填，
模型对本来可以留空的字段（比如 `example`）无话可说，
只好**复读术语凑数**。

```python
out["required"] = [p for p, sub in props.items() if not _allows_null(sub)]
```

**三、让模型做它擅长的事。** 时间戳要求模型把 `[mm:ss]` 换算成秒数时，
它会给出 2/3/8/12/15 这种与原文完全对不上的小数字
（20 分钟的视频，大纲时间点全落在前 30 秒）。
改成「**原样抄写这个标记**」再自己解析，准确率立刻上来：

```python
_SCHEMA_OVERRIDES = {
    "timestamp": {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "description": '原样沿用 [mm:ss] 标记，例如 "12:34"。不要换算成秒数。',
    },
}
```

## 2.7 提示词工程：这个项目里最值钱的几条

### 一、先让模型想清楚「读者要什么」，再组织内容

🕳️ 直接让模型「总结这个视频」，它会按**时间顺序复述** ——
产出一篇什么都没漏、但什么重点都没有的笔记：

> 「使用 Codex 作为通用编程 Agent，需配置验证环境插件」
> 「开发流程分为环境搭建、产品设计…五步」

📐 拆成两步。第一步**只用元信息**（标题 / 简介 / 标签 / 章节，
不看正文，所以很快）推断：

```python
class FocusPlan(BaseModel):
    video_kind: str        # 教程 / 评测 / 解说
    audience_goal: str     # 冲着这个标题点进来的人最想得到什么
    focus: list[str]       # 3-6 个关注点，按重要性排序
    must_extract: list[str]# 必须逐字抓下来的东西
    ignore: list[str]      # 可以忽略的（寒暄、口播）
```

这份判断贯穿后续所有环节 —— **map 的每一块和最终汇总都带上它**。
不带的话，各块各抽各的，汇总时得到一堆与读者目标无关的碎片。

📐 **降级路径要能构造。** 这个 `FocusPlan` 一开始字段没有默认值，
结果推断失败时的兜底 `return FocusPlan()` **自己会抛异常** ——
一个「锦上添花」的步骤反过来把整个流程搞挂了。所有兜底路径都要能无参构造。

### 二、别忘了「非音频信号」

只把标题和时长喂给模型是浪费。真正有用的是：

* **简介** —— 教程类常把大纲、链接甚至提示词写在这里
* **标签** —— 主题词
* **B站 AI 章节**（`view_points`）—— **未登录就能拿到**的现成主干结构

📐 而且它和字幕共用同一个 `player/v2` 缓存键，**不多一次网络请求**。

### 三、🕳️ 提示词里的「举例」会被照抄

我写了「好例子：『能自己写出让 agent 稳定执行多步任务的提示词』」，
结果 8B 模型把这个例子**原样抄进了输出**，`goal` 和 `one_liner` 变成同一句话。

📐 两个教训：
1. 举例只能说「风格」，要明确写「不要照抄下面括号里的措辞」
2. **小模型不听话时，不要一直加强提示词，用代码做确定性后处理**

第三条尤其重要。我让模型**不要**在可复制内容前加「标题：」前缀，
在提示词里反复强调也改不掉（`验证Git状态：git status`）。
最后是用代码剥掉的：

```python
def strip_label_prefix(content: str, title: str) -> str:
    """只在**前缀确实等于标题**时才剥离。

    不能见冒号就切 —— 提示词正文本来就常有冒号
    （「请按以下格式输出：1. 结论 2. 理由」），一刀切会切掉有用的内容。
    """
```

### 四、输出质量要有门槛，重试要「一次说清所有问题」

```python
def has_substance(self) -> bool:
    """有实质内容：至少要有重点或行动项之一。

    比 is_thin() 更严 —— 实测模型会产出一堆「介绍了…」「提到了…」
    的复述式要点，看着不空，但读者拿不走任何东西。
    """
    return bool(self.focus_points) or bool(self.actionables)
```

🕳️ 重试提示词最初是「发现一个问题就返回」，
后来改成**把所有问题一次列全** —— 一次重试的命中率明显更高：

```python
return "；".join(reasons)   # 内容过少；缺重点和行动项；时间戳不可用
```

## 2.8 长文本：按「体量」而不是「条数」做预算控制

🕳️ 56 分钟的视频，8 个分段的中间结果拼起来**超过上下文窗口**，
模型随即陷入异常并一直跑到超时。

📐 原来只按条数设上限（12 条）远远不够。改成**估算提示词 token 数**，
超预算就先归并一轮：

```python
def _split_by_budget(self, partials, budget, ...) -> list[list[PartialNotes]]:
    """单条就超预算时也会自成一批 —— 否则会切不动、陷入死循环。"""
```

🕳️ 还有个子坑：为了让提示词变小，我用「紧凑模式」渲染中间结果，
但**紧凑得不够**（单条就超预算），于是每条各成一批，归并完全失效。
紧凑模式必须真的足够紧凑。

## 2.9 幻觉防线：拿不准的地方，只提问不给答案

🕳️ 模型会**自信地编造**术语全称和作品出处，而且同一篇笔记里
每次运行编的都不一样（实测同一个词三次运行给出三种「全称」）。

📐 原设计有个「补充背景知识」小节，本意是帮读者理解，
实际上变成了幻觉的温床。改成**待查清单，只给问题不给答案**：

> 与其给你一个可能错的解释，不如把该查什么列清楚。

对应的提示词约束：

```
- follow_ups：视频默认你懂、但其实没交代的点，列成待查问题。
  ⛔ 只提问，不要给答案。你没有可靠的外部知识，编答案会害了读者。
```

📐 另外**让模型标出信息来源**：视频里讲过的标 `video`，
模型自己补的标 `model`，渲染时打上「✅ 视频原话」/「➕ 模型补充」徽章。
读者一眼能看出哪些需要核实。

## 2.10 视觉信息：让模型「看见」屏幕上的字

🕳️ **光靠音频会丢关键信息。** 同一个 AI 编程教程视频，仅凭音频：

| | 仅音频 | 加画面识别 |
|---|---|---|
| 查状态 | `Gate status` ❌ | `git status` ✅ |
| 初始化 | `初始划为一个get仓酷` ❌ | `git init` ✅ |
| 音频没念过的 | — | `git diff --check`、`npm start` ✅ |

同音字是一方面，更关键的是**屏幕上有音频里根本没说的东西**。

### 抽帧的时间戳：别用 `fps=1/N`

🕳️ `-vf fps=1/15` 抽出的帧，真实时间戳是 `t_i = (i - 0.5) * N`，
**不是** `(i-1) * N`。15 秒间隔就错 **7.5 秒**。

更阴险的是 `fps=1/15,showinfo` 会打印 `[0.0, 15.0, 30.0, 45.0]` ——
**看着完全正确但全是假的**，因为 fps 滤镜会重写 PTS。

📐 改用**逐点定位**，时间戳就是自己传进去的值，不存在推算误差：

```python
["-ss", f"{timestamp:.3f}", "-i", video, "-frames:v", "1"]
```

稀疏采样时这反而比单遍解码更快（不用解码整段视频）。

### 画面去重：不要用感知哈希

OCR 是整条链路最贵的步骤（约 0.8 秒/帧），而教程视频大部分帧画面几乎不变。
所以 OCR 之前要先去重。🕳️ 但 **dHash 区分不出「新增一行文字」和「翻页」**：

| 方法 | 同内容(仅噪声) | 新出现一行文字 | 翻页 |
|---|---|---|---|
| dHash 16×16 | 0~1 | **3** | **5** |
| 全图 MAD | 2.86 | 11.58 | 30.74 |
| **8×8 分块 MAD** | **0 块** | **14 块** | **32 块** |

dHash 把「新增一行文字」判成 3、把「翻页」判成 5，都低于常见阈值，
于是**新内容会被当成重复帧丢掉**。分块 MAD 只要约 4ms/帧，比 OCR 便宜 200 倍。

### 字幕剔除：用已有的 ASR 结果

📐 画面底部的字幕和音频转写**完全重复**，直接塞进提示词等于喂两遍。

通用 OCR 方案只能靠「字幕总在画面底部某个位置」这种启发式，
对花字就失效了。而**我们手里正好有音频转写** ——
把 OCR 出的每一行和转写做归一化比对，命中就丢掉：

```python
def is_subtitle_like(line: str, transcript_norm: str) -> bool:
    norm = normalize(line)          # 去标点、转小写
    if norm in transcript_norm:     # 先看子串包含（快且准）
        return True
    return 相似度(norm, 滑窗片段) >= 0.75   # 再用相似度兜住轻微差异
```

### 离线是硬要求

RapidOCR 的模型（约 36MB）**打包在 wheel 里**，从
`site-packages/rapidocr/models/` 读取并做 SHA256 校验。

📐 验证方法不是「看文档说它离线」，而是**把代理指向死端口**：

```python
os.environ["HTTP_PROXY"] = os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
# 仍然正常识别 → 确认零网络请求
```

⚠️ 顺带一提：旧的 `rapidocr-onnxruntime` 声明 `Requires-Python <3.13`，
在 Python 3.13 上装不了。要用新的 `rapidocr`（纯 Python wheel）。

## 2.11 隐私边界：把承诺写成测试，不是写在文档里

📐 这个项目有一条自动化守卫：**AST 扫描 `app/` 下所有 `.py`，
任何引用了网络库的文件必须在白名单里**。

```python
ALLOWED_NETWORK_MODULES = {
    "app/bili/client.py": "访问 B站 API 取元数据、章节与字幕",
    "app/media/downloader.py": "经 yt-dlp 下载音频与低清视频",
    "app/llm/ollama_provider.py": "调用本机 Ollama（回环地址）",
    ...
}
```

🕳️ **我踩过这个坑**：加了视频下载和 OCR 之后忘了更新这个白名单 ——
**新增的出网行为处在监控之外**。守卫的价值就在于它强迫你「显式确认」。

📐 还有两条值得学的写法：

**给「有下载能力但默认离线」的库一个显式名分。** 我把 `rapidocr`
加进了监控列表，并在白名单里写清楚理由：

```python
# ⚠️ 这条是**显式确认**，不是「它在联网」：
# rapidocr 的模型打包在 wheel 里，实测零网络请求。
# 列进来是因为该库**自带模型下载逻辑** —— 一旦行为变了，守卫能让我们看见。
"app/vision/ocr.py": "RapidOCR 本地推理；模型随包提供，默认路径不联网",
```

**再断言一次「它不是靠伪装离线的」**：

```python
def test_vision_reads_no_env_proxy_workarounds():
    """确认我们没有为了「看起来离线」而偷偷改代理设置。"""
    assert "HTTP_PROXY" not in source
```

---

# 三、工程方法（跨领域的）

## 3.1 🔢 检查工具本身要有「牙」

一个永远返回「没问题」的检查，比没有检查更糟 —— 它会给你虚假的安全感。

📐 所以给关键检查都配一个**元测试**：故意造一个坏输入，确认检查能抓出来。

```python
def test_the_scan_actually_finds_network_imports():
    """防止扫描逻辑本身失效（比如 ast 用错）导致测试永远通过。"""
    client = APP_DIR / "bili" / "client.py"
    assert _network_imports(client), "扫描没能发现 httpx，扫描逻辑有问题"


def test_syntax_check_actually_detects_errors(tmp_path):
    broken = tmp_path / "broken.ps1"
    broken.write_text("Write-Host 'unterminated\nif ($true) {\n")
    assert _parse_errors(broken), "语法检查没能发现故意写坏的文件"
```

我实测验证过：坏文件退出码 1 并报出 `The string is missing the terminator`，
好文件退出码 0。

## 3.2 🕳️ 调研结论要自己复现一条

调研子代理给了很详实的报告，其中一条说：

> 「不带 `--impersonate Chrome-136` 会被 B站 412 拒绝」

📐 我自己一测，**结论正好相反**：

| impersonate | 结果 |
|---|---|
| 不带 | ✅ 15 个格式 |
| `chrome` | ✅ 15 个格式 |
| `chrome-136` | ❌ **No video formats found** |

而且「不带」和「带 chrome」单独都能用，一旦和一堆其他选项组合起来，
带 impersonate 的那个反而失败。

📐 教训：**报告里的每一条都可能过时或环境相关。**
最后我没有赌单一路径，改成「先按默认来，失败了再回退到显式指纹」，
两次尝试都不成才报错。

## 3.3 🕳️ 报错信息要能直接指导下一步

服务超时时不要只丢一个 `TimeoutError`。这个项目的做法是把
**常见原因和对策**直接写进异常：

```
Ollama 响应超时（600.0s）。
常见原因与对策：
  1) 提示词超过了上下文窗口（当前 num_ctx=16384）—— 可调大 LLM_NUM_CTX
  2) 模型太大、显存不足在跑 CPU。可用 ollama ps 查看
  3) 单纯就是慢。可调大 OLLAMA_TIMEOUT，或换更小的模型
```

## 3.4 「优雅降级」要真的降级

画面识别、章节获取、字幕获取这些都是**增强项**。
任何一步失败都不该让整篇笔记生不出来：

```python
except Exception as exc:      # noqa: BLE001 - 画面识别是增强项
    logger.warning("画面识别失败（不影响笔记生成）：%s", exc)
    notices.append(f"⚠️ 画面识别失败，本次只依据音频内容：{exc}")
    return []
```

📐 而且降级要**让用户知道**。失败静默吞掉的话，
用户只会觉得「结果质量不稳定」，不知道发生了什么。

## 3.5 🕳️ 改了代码记得重启服务

这一条害我查了半天：

**现象**：通过 Web 界面提交任务，日志里**完全没有画面识别阶段**，
而且偶尔超时。

**排查**：服务进程启动于 `01:33:33`，而 vision 代码创建于 `01:38:41` ——
**跑的是旧代码**。

📐 教训：`uvicorn` 不带 `--reload` 时不会自动重载。
改完代码要重启，否则会拿新代码的行为去判断旧代码的输出。

---

# 四、如果你想复现这套东西，建议的顺序

1. **先跑通最小闭环**：一个链接进去、一段文字出来。别一上来就上 GPU。
2. **把隐私边界先立起来**（网络白名单测试）。后加的话很难补。
3. **本地模型先用小参数跑通**，确认结构化输出稳定，再考虑换大模型。
4. **提示词按「先想目标、再组织内容」两步拆**，效果比堆字数明显。
5. **画面/OCR 是可选增强**，设计成装不上也能用。
6. **验证用工具量，不用眼睛看**。CDP 那套值得单独花半天搭起来。

---

# 五、这些结论的可靠性

诚实说明一下：

* 🔢 **实测数据**都来自这台机器（RTX 5060 Laptop 8GB / Intel 8P+16E / 32GB），
  换机器会有出入，主要看**数量级**。
* 📐 **原理性**的结论（sticky 语义、`$ref` 展平、编码规则）跨项目成立。
* ⚠️ **有一条我没能完全坐实**：那个 731 秒的超时，我归因到
  「`num_predict` 没设上限」，但**没有 100% 复现确认**。
  直接调用同一段逻辑是成功的，重启服务后复测也通过。
  如果再次遇到超时，先看 `ollama ps` 确认模型有没有掉到 CPU 上跑。
* ⚠️ **B站的行为会随版本变化**。字幕未登录拿不到、`view_points` 未登录能拿到，
  这些今天成立不代表明天成立。写代码时要留降级路径。
