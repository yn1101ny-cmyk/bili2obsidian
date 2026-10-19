# 用程序获取 B 站视频字幕与音频 —— 技术调研报告

**调研方式**：一手实测（本机直接请求 B 站接口）+ 官方/社区文档 + 二手来源交叉验证
**实测时间**：B 站服务器返回 `Date: Sat, 03 Oct 2026 14:28:06 GMT`（注意：实际时间晚于提问中假设的「2025 年末」，因此本报告采信的信息比预期更新）
**实测环境**：Windows / curl 8.10.1 / yt-dlp 2026.08.19 / 未登录、家庭宽带 IP

**可信度标记**
- ✅ **已实测确认** —— 我本人用真实请求验证过，附原始返回
- 📄 **文档确认** —— 官方或权威社区文档明确记载，我未直接复现
- ⚠️ **二手 / 未复现 / 不确定** —— 来自博客或社区，或我尝试但未能复现

---

## A. 视频元数据

### A1. `x/web-interface/view` 是否仍可用？返回哪些字段？

✅ **已实测确认：完全可用。**

```
GET https://api.bilibili.com/x/web-interface/view?bvid=BV1GJ411x7h7
→ HTTP/1.1 200 OK
→ {"code":0,"message":"OK","ttl":1,"data":{...}}
```

实测返回的有用字段（BV1GJ411x7h7）：

| 字段 | 实测值示例 | 说明 |
|---|---|---|
| `data.bvid` / `data.aid` | `BV1GJ411x7h7` / `80433022` | 视频双 ID |
| `data.title` | `【官方 MV】Never Gonna Give You Up - Rick Astley` | 标题 |
| `data.desc` | `-` | 简介（另有 `desc_v2[].raw_text`） |
| `data.owner.mid` / `.name` / `.face` | `486906719` / 索尼音乐中国 / 头像 URL | UP 主 |
| `data.duration` | `213`（秒） | **整稿**总时长 |
| `data.cid` | `137649199` | 默认 P 的 cid（**取字幕/音频必需**） |
| `data.pages[]` | 见 A4 | 分 P 列表 |
| `data.dimension` | `{width:1920,height:1080,rotate:0}` | 尺寸 |
| `data.pubdate` / `ctime` | `1577835803` | 发布时间戳 |
| `data.videos` | `1` | 分 P 数量 |
| `data.stat` | `view/danmaku/reply/favorite/coin/like...` | 互动数据 |
| `data.subtitle` | `{allow_submit:false,list:[...]}` | **字幕元数据列表**（见 B6） |
| `data.rights` | `{download:1,hd5:1,...}` | 权限位 |

> 注意：`view` 接口的 `data.subtitle` 用的是 `list` 数组；而 `player/v2` 用的是 `data.subtitle.subtitles`。**两者字段名不同，别混用。**

来源：本机实测（2026-10-03）；字段语义参考 [bilibili-API-collect（pskdje 镜像，已归档）](https://github.com/pskdje/bilibili-API-collect)

---

### A2. 必须带哪些请求头？不带的后果是什么（-412？）

✅ **已实测确认：`view` 接口对 UA / Referer 并不敏感。** 我用三种方式请求，全部 `HTTP 200 / code 0`：

```bash
# 1) 完全不发 User-Agent
curl -H "User-Agent:" "https://api.bilibili.com/x/web-interface/view?bvid=BV1GJ411x7h7"
# → 200, code=0

# 2) 只发 curl 默认 UA
curl "https://api.bilibili.com/x/web-interface/view?bvid=BV1GJ411x7h7"
# → 200, code=0

# 3) 发一个错误 Referer
curl -A "<Chrome UA>" -e "https://example.com/" "https://api.bilibili.com/.../view?bvid=BV1GJ411x7h7"
# → 200, code=0
```

**但 `-412` 是真实存在的风控返回**，只是触发条件不是「缺 UA」这么简单，而是 **IP 信誉 + 请求频率 + 行为序列**综合判定：

- 📄 社区文档记录了 `-412 请求被拦截` 与 `-352` 两种风控码，以及 `v_voucher`（gaia 验证）机制：[v_voucher 验证文档](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/misc/sign/v_voucher.md)
- ⚠️ 我在**低频**实测（全程几十次请求）中**没有复现 `-412`**，所以无法给出精确的触发阈值。

**结论/实践建议**：`view` 接口裸调通常能通，但**请一律带上**下面这套头，成本为零且能显著降低被风控概率（yt-dlp 也这么做）：

```http
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36
Referer: https://www.bilibili.com/
```

来源：本机实测；[B站API风控深度解析（CSDN，AIGC 辅助生成，谨慎采信）](https://blog.csdn.net/gitblog_01078/article/details/159138055)

---

### A3. 短链 `b23.tv/xxx` 如何解析成 BV 号？

✅ **已实测确认：标准 HTTP 302 重定向，`Location` 头就是答案。**

```bash
$ curl -s -o /dev/null -D - -A "<Chrome UA>" "https://b23.tv/BV1Tct1ztE26"
HTTP/1.1 302 Found
Location: https://www.bilibili.com/video/BV1Tct1ztE26
```

实测的完整重定向链（`curl -L`）：

```
b23.tv/{code}
  → 302 Found   Location: https://www.bilibili.com/video/BV1GJ411x7h7
  → 301 Moved   Location: /video/BV1GJ411x7h7/
              + Set-Cookie: buvid3=...; b_nut=...; innersign=
  → 200 OK      （最终 HTML 播放页）
```

几个实测要点：
1. `b23.tv/BVxxxx` 和 `b23.tv/av80433022` 两种形式都能直接 302 到对应播放页。
2. **无效短码不会 302**：实测 `https://b23.tv/aUqRmx`（虚构码）返回 `200` 且 `url_effective` 仍是 `b23.tv/aUqRmx`（是一个 HTML 页面，无 `Location`）。所以**必须判断状态码/有无 Location，不能盲取**。
3. 解析时会下发 `buvid3`（设备指纹 cookie），后续请求带上它有助于降低风控。

**可直接运行的解析代码（不跟随重定向，只读 Location）：**

```python
import re, requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")

def resolve_bvid(url: str) -> str:
    """把 b23.tv 短链 / 各种 B 站链接统一解析成 BV 号。"""
    if m := BV_RE.search(url):
        return m.group(1)                     # 链接里已含 BV 号，直接取
    r = requests.get(url, headers={"User-Agent": UA},
                     allow_redirects=False, timeout=10)
    loc = r.headers.get("Location", "")       # 302 的 Location
    if m := BV_RE.search(loc):
        return m.group(1)
    if r.status_code == 200:
        # 兜底：跟随重定向后再从最终 URL 或 HTML 里找
        r2 = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        if m := BV_RE.search(str(r2.url)):
            return m.group(1)
    raise ValueError(f"无法从 {url} 解析出 BV 号（http={r.status_code}）")
```

来源：本机实测。

---

### A4. 多分P视频 / 合集系列的 `pages` 数据结构

✅ **已实测确认。** 我实测了一个 **132 分P** 的视频 `BV1xq6gBgESU`（`videos: 132`，`data.pages` 实测长度 = 132）。

`data.pages[]` 每个元素的**真实结构**：

```json
{
  "cid": 35645754123,
  "page": 1,
  "from": "vupload",
  "part": "课程介绍",
  "duration": 218,
  "vid": "",
  "weblink": "",
  "dimension": { "width": 3840, "height": 2160, "rotate": 0 },
  "first_frame": "http://i2.hdslb.com/bfs/storyff/_0000128hxghir7ze822vw96duzlfv8b_firsti.jpg",
  "ctime": 1769511495
}
```

要点：
- **`cid` 是关键**：每个分P 有独立 `cid`，取字幕（`player/v2`）和音频（`playurl`）都必须用**对应分P 的 cid**。
- `pages[i].page` 从 1 开始；`pages[i].part` 是分P 标题。
- 还有 `weblink`（互动视频用）、`first_frame`（首帧图，部分视频才有）。
- 单P视频 `pages` 长度为 1。

**合集 / 系列（两种不同概念）**

- 📄 **系列 / 视频列表**：`GET https://api.bilibili.com/x/polymer/web-space/home/seasons_series?mid={mid}&page_num=1&page_size=30`
  ✅ 已实测可用（`code:0`），返回 `data.items_lists.{seasons_list[], series_list[]}`，每个 `series_list[]` 元素含 `meta{...}` 和 `archives[]{aid, bvid, title, duration, pubdate, stat{...}, pic, ...}`。
  文档：[合集和视频列表信息](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/video/collection.md)

- 📄 **合集（UGC season）**：`view` 接口在视频属于合集时会额外返回 `data.ugc_season`，结构为 `ugc_season.sections[].episodes[]`（每个 episode 含 `aid/bvid/cid/title` 和其分P 信息）。
  ⚠️ **我未能实测到**：我抽查的几个视频都没命中 `ugc_season`（需要恰好找到一个属于合集的稿件）。此结构来自文档，请以实际返回为准。

来源：本机实测（132P 视频）；[bilibili-API-collect 合集文档](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/video/collection.md)

---

## B. 字幕（重点）

### B5. `x/player/v2?aid=&cid=` 的 `data.subtitle.subtitles` 结构

✅ **已实测确认接口可用**（`HTTP 200 / code 0`），但未登录返回空数组。完整结构（实测原始片段）：

```json
{
  "code": 0, "message": "OK", "ttl": 1,
  "data": {
    "aid": 114992196164102,
    "bvid": "BV1Tct1ztE26",
    "cid": 31563845307,
    "login_mid": 0,
    "is_owner": false,
    "now_time": 1791037708,
    "need_login_subtitle": true,          // ★ 未登录时为 true
    "subtitle": {
      "allow_submit": false,
      "lan": "",
      "lan_doc": "",
      "subtitles": [],                     // ★ 未登录时为空
      "subtitle_position": null,
      "font_size_type": 0
    },
    "view_points": [ ... ],                // AI 章节，未登录也能拿到
    "options": { "is_360": false, "without_vip": false }
  }
}
```

**登录后有内容时，`subtitles[]` 里每个元素的真实结构**（来自实测过该字段的社区代码）：

```json
{
  "id": 1497922385058359296,
  "lan": "ai-zh",
  "lan_doc": "中文（自动生成）",
  "is_lock": false,
  "subtitle_url": "//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/18552159371560156670a544e45bb1c1fcbc749444766bcfdce1?auth_key=1726370540-6200821378ad42a7a48c21fe4b226486-0-5b557de4cbb342c3e46e5f78c068b28f",
  "type": 1,
  "id_str": "1497922385058359296",
  "ai_type": 0,
  "ai_status": 2
}
```

字段含义：`lan` = 语言代码（`ai-zh`/`ai-en`/`zh-CN`/`zh-Hans`/`zh-Hant`/`en-US`/`ja`/`ko`…）；`lan_doc` = 展示名；`subtitle_url` = 字幕 JSON 地址；`type`/`ai_type`/`ai_status` 标识 AI 生成；`is_lock` 表示锁定。

来源：[BILIBILI 字幕提取教程简略版（CSDN）](https://blog.csdn.net/wizardforcel/article/details/142281926)；本机实测空态结构。

---

### B6. ★最关键：获取字幕是否需要登录 Cookie？未登录 `subtitles` 是否为空？

✅✅ **已实测确认：必须登录。未登录时 `subtitles` 恒为空数组，且 `need_login_subtitle: true`。**

我做了 5 组对照实测：

| # | 请求 | 结果 |
|---|---|---|
| 1 | `player/v2?aid=&cid=`（AI字幕视频 BV1Tct1ztE26，带 UA+Referer） | `code:0`，`need_login_subtitle:true`，`subtitles:[]` |
| 2 | `player/wbi/v2?aid=&cid=`（同上） | 同上，`subtitles:[]` |
| 3 | `player/v2?bvid=&cid=`（同上） | 同上，`subtitles:[]` |
| 4 | **`player/wbi/v2` + 完整正确的 WBI 签名（w_rid/wts）** | 仍 `need_login_subtitle:true`，`subtitles:[]` |
| 5 | `player/v2`（**普通用户 CC 字幕**视频 BV1GJ411x7h7，该视频在 view 接口里列出了 12 条 CC 字幕） | `need_login_subtitle:true`，`subtitles:[]` |

**核心结论：**

1. **未登录 → 字幕数组一定为空**，与视频类型、分P、参数写法都无关。
2. **WBI 签名不能替代登录**（第 4 组证明）。签名只解决「接口访问权限」，不解决「内容授权」。
3. **CC 字幕（用户上传）和 AI 字幕（B站自动生成）在 API 层面权限相同**：未登录都拿不到（第 5 组）。
4. 未登录时 `view` 接口确实会**列出字幕的元数据**（`data.subtitle.list`，含 `lan`/`lan_doc`/`author`），但**`subtitle_url` 全部为 `""`** —— 也就是说只能知道「有哪些字幕」，拿不到内容。
   > 实测 BV1GJ411x7h7 的 `view.data.subtitle.list` 有 12 条（`zh-CN`/`zh-Hans`/`zh-Hant`/`zh-HK`/`en-US`/`ja`/`ko`/`de-DE`/`ru`/`iw`/`ca`/`ase`），每条 `"subtitle_url": ""`。而同一视频 `player/v2` 返回 `subtitles:[]`。

**第三方独立印证**：yt-dlp 2026.08.19 在无 Cookie 时直接告警：
```
WARNING: [BiliBili] Subtitles are only available when logged in. Use --cookies-from-browser or --cookies for the authentication.
```

⚠️ **一个诚实的保留**：B站**网页播放器**在未登录状态下，有时仍能显示部分普通用户上传的 CC 字幕（走的是前端其它路径 / 播放器内部接口）。我**未能**通过上述公开 API 复现这一点。若你的场景强依赖免登录 CC 字幕，建议用浏览器 DevTools 抓一下实际请求做二次确认。

来源：本机实测（5 组对照，2026-10-03）；yt-dlp 2026.08.19 实跑输出；[BILIBILI 字幕提取教程（CSDN）明确写「需要 COOKIE，否则为空」](https://blog.csdn.net/wizardforcel/article/details/142281926)

---

### B7. `subtitle_url` 指向的 JSON 结构 & 需要什么请求头

📄 + ✅ **结构已双重确认。** `subtitle_url` 指向 `aisubtitle.hdslb.com`（CDN），返回 JSON：

```json
{
  "font_size": 0.4,
  "font_color": "#FFFFFF",
  "background_alpha": 0.5,
  "background_color": "#9C27B0",
  "Stroke": "none",
  "type": "AIsubtitle",
  "lang": "zh",
  "version": "v1.7.0.4",
  "body": [
    { "from": 1.86,    "to": 4.46,    "sid": 1, "location": 2, "content": "那你们有没有背着露露",           "music": 0.0 },
    { "from": 5.82,    "to": 13.25,   "sid": 2, "location": 2, "content": "偷偷的偷偷的看一些不该看的东西呢", "music": 0.0 },
    { "from": 15.759,  "to": 17.999,  "sid": 3, "location": 2, "content": "啊我猜一定有吧",                 "music": 0.0 },
    { "from": 32.0,    "to": 35.0,    "sid": 5, "location": 2, "content": "可是你们的房间",               "music": 0.0 }
  ]
}
```

**→ 是的，就是你猜的 `body: [{from, to, content}]`**（`from`/`to` 单位是**秒**，浮点；另有 `sid`/`location`/`music` 等可选字段）。

这一点有**代码级铁证**：yt-dlp 的 `json2srt()` 就是按这三个字段解析的（我读了本机安装的源码 `D:\pycharm\Lib\site-packages\yt_dlp\extractor\bilibili.py` 第 247–253 行）：

```python
def json2srt(self, json_data):
    srt_data = ''
    for idx, line in enumerate(json_data.get('body') or []):
        srt_data += (f'{idx + 1}\n'
                     f'{srt_subtitles_timecode(line["from"])} --> {srt_subtitles_timecode(line["to"])}\n'
                     f'{line["content"]}\n\n')
    return srt_data
```

**请求它需要什么头？**
- ⚠️ **未实测**（我没有登录态，拿不到有效 `subtitle_url`）。
- 它是 CDN 资源，URL 自带 `auth_key=` 签名参数（**时效性由 auth_key 控制**）。
- **稳妥做法：带上和 API 一样的 `Referer: https://www.bilibili.com/` 和浏览器 UA**，与音视频流一致（与 B 站 CDN 的 referer 鉴权策略保持一致，见 C11 实测）。
- **URL 拼接坑**：社区代码拿到的是 `//aisubtitle.hdslb.com/...`（协议相对），需要补 `https:`；但 yt-dlp 现在是**直接把 `s['subtitle_url']` 传给下载器**（源码 L274），说明**当前接口已返回绝对 URL**。→ 代码里请做兼容判断：

```python
url = s["subtitle_url"]
if url.startswith("//"):
    url = "https:" + url
```

来源：[CSDN 字幕提取教程（含真实 JSON 片段）](https://blog.csdn.net/wizardforcel/article/details/142281926)；yt-dlp 源码 `json2srt()`（本机安装版 2026.08.19）

---

### B8. 有没有不需要登录就能拿 AI 字幕的方法？SESSDATA 从哪来、怎么用、什么风险？

✅ **已实测确认：我没找到任何可靠的免登录方法。** 我把可能的旁路都试了一遍：

| 尝试的免登录旁路 | 实测结果 |
|---|---|
| `player/v2` 未登录 | ❌ `subtitles: []` |
| `player/wbi/v2` **+ 正确 WBI 签名**、未登录 | ❌ `subtitles: []`（签名≠登录） |
| `view` 接口的 `data.subtitle.list` | ⚠️ 只有元数据，`subtitle_url` 全为 `""` |
| `x/web-interface/view/conclusion/get`（**AI 视频总结**，内容含字幕式分段文本）无签名 | ❌ `{"code":-403,"message":"访问权限不足"}` |
| 同上，**带正确 WBI 签名** | ❌ `{"code":-101,"message":"账号未登录"}` |
| `x/player/playurl` 加 `try_look=1`（未登录高画质） | ✅ 能拿高画质**视频流**，但**不影响字幕** |

剩下唯一未登录可得的「类文本」数据：
- `player/v2` 的 `data.view_points[]`（AI 章节标题 + 时间点 + 缩略图，实测有 8 段）
- 弹幕 XML：`https://comment.bilibili.com/{cid}.xml`（✅ 实测 `200`，约 47KB，gzip 压缩）

**→ 结论：要字幕就必须登录。没有例外。**

**SESSDATA 从哪来：**
1. 浏览器登录 bilibili.com
2. F12 → Application/存储 → Cookies → `https://www.bilibili.com`
3. 找到 `SESSDATA`，复制其「值」

或直接在 DevTools Console 里取（📄 参考 [获取B站登录账号的SESSDATA](https://www.bilibili.com/read/cv25451423)）。

**怎么用：**

```bash
# curl：直接作为 Cookie 头
curl -s "https://api.bilibili.com/x/player/wbi/v2?aid=114992196164102&cid=31563845307" \
  -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36" \
  -e "https://www.bilibili.com/" \
  -b "SESSDATA=你的SESSDATA值"
```

```python
SESSION_COOKIE = {"SESSDATA": "你的SESSDATA值"}   # 只读操作，仅此一项够
# 充电专属/付费内容需要三件套：SESSDATA + bili_jct + DedeUserID
```

**风险（务必重视）：**
1. 🚨 **SESSDATA 等价于登录凭证**：拿到它的人可以直接以你的身份登录 B站账号，**不需要密码**。📄 [知乎：可以利用 SESSDATA 直接绕过验证登录该账户](https://www.zhihu.com/question/497366662/answer/2701544727)
2. ⏰ **有效期约 1 个月**：📄 同上来源称「B站为一个月」；但另有实测反馈**实际失效时间远早于 Cookie 自身时间戳**（📄 [bili-auth issue #6](https://github.com/icyux/bili-auth/issues/6)）。→ 不要把有效期硬编码，务必处理 `code:-101` 并提示重新登录。
3. 🚫 **绝对不要**提交到 Git、写进前端代码、贴到公共 CI 日志、或交给第三方服务。
4. 🛡️ **强烈建议用小号**执行自动化任务；一旦触发风控，封的是这个账号。
5. 📄 B站有账号安全公告与撞库风险提示：[部分账号存在撞库被盗风险](https://www.bilibili.com/blackboard/activity-Bk7Lt8J0e.html)

来源：本机实测（全部旁路）；[知乎 SESSDATA 说明](https://www.zhihu.com/question/497366662/answer/2701544727)；[获取SESSDATA 教程](https://www.bilibili.com/read/cv25451423)；[bili-auth issue #6](https://github.com/icyux/bili-auth/issues/6)

---

### B9. yt-dlp 是否支持抓 B站字幕？走哪个接口？现在还工作吗？

✅✅ **已实测确认：支持，且截至 yt-dlp 2026.08.19 仍在维护、功能正常（但必须登录）。**

**实测命令与输出：**

```bash
$ python -m yt_dlp --ignore-config -v --list-subs --skip-download \
    "https://www.bilibili.com/video/BV1Tct1ztE26/"
[BiliBili] Extracting URL: https://www.bilibili.com/video/BV1Tct1ztE26/
[BiliBili] BV1Tct1ztE26: Downloading webpage
[BiliBili] BV1Tct1ztE26: Extracting videos in anthology
[BiliBili] BV1Tct1ztE26: Downloading wbi sign
[BiliBili] BV1Tct1ztE26: Downloading video formats for cid 31563845307
[BiliBili] BV1Tct1ztE26: Extracting subtitle info 31563845307
WARNING: [BiliBili] Subtitles are only available when logged in. Use --cookies-from-browser or --cookies for the authentication.
[info] Available subtitles for BV1Tct1ztE26:
Language Formats
danmaku  xml
```

**`--write-subs` 实测（无 Cookie，必然失败）：**
```bash
$ python -m yt_dlp --ignore-config --skip-download --write-subs --sub-langs "ai-zh" \
    "https://www.bilibili.com/video/BV1Tct1ztE26/"
WARNING: [BiliBili] Subtitles are only available when logged in. ...
[info] There are no subtitles for the requested languages
```

**它内部走哪个接口？** —— 我读了本机安装的 yt-dlp 源码（`yt_dlp/extractor/bilibili.py` 第 255–276 行），铁证如下：

```python
def _get_subtitles(self, video_id, cid, aid=None):
    subtitles = {
        'danmaku': [{'ext': 'xml',
                     'url': f'https://comment.bilibili.com/{cid}.xml'}],
    }
    video_info = self._download_json(
        'https://api.bilibili.com/x/player/wbi/v2', video_id,          # ★ 就是这个接口
        query={'aid': aid, 'cid': cid} if aid else {'bvid': video_id, 'cid': cid},
        note=f'Extracting subtitle info {cid}', headers=self._HEADERS)
    if traverse_obj(video_info, ('data', 'need_login_subtitle')):       # ★ 探测登录标志
        self.report_warning(
            f'Subtitles are only available when logged in. {self._login_hint()}', only_once=True)
    for s in traverse_obj(video_info, (
            'data', 'subtitle', 'subtitles', lambda _, v: v['subtitle_url'] and v['lan'])):
        subtitles.setdefault(s['lan'], []).append({
            'ext': 'srt',
            'data': self.json2srt(self._download_json(s['subtitle_url'], video_id)),  # ★ aisubtitle JSON → SRT
        })
    return subtitles
```

要点：
- 接口：**`https://api.bilibili.com/x/player/wbi/v2`**（WBI 签名的版本，不是老的 `/x/player/v2`）
- 请求头：`_HEADERS = {'Referer': 'https://www.bilibili.com/'}`（源码 L50）
- 弹幕被当作一种「字幕轨道」暴露为 `danmaku`/`xml`
- 语言代码直接取自 API 的 `lan` 字段（`ai-zh`/`ai-en`/`zh-CN`…）

**参数：**
```bash
# 列出可用字幕
yt-dlp --list-subs "URL"
# 下载字幕（注意：AI 字幕必须用 --write-subs，不是 --write-auto-subs，见下）
yt-dlp --write-subs --sub-langs "ai-zh,zh-CN,zh-Hans,ai-en,en-US" --skip-download "URL"
# 下载并嵌入到视频
yt-dlp --write-subs --embed-subs --sub-langs "ai-zh,zh-CN" "URL"
# 带浏览器登录态（关键）
yt-dlp --cookies-from-browser firefox --list-subs "URL"
yt-dlp --cookies-from-browser chrome  --write-subs --sub-langs "ai-zh" "URL"
# 或用导出的 cookies.txt
yt-dlp --cookies cookies.txt --write-subs --sub-langs "ai-zh" "URL"
```

**`--cookies-from-browser` 能用吗？** ✅ 能。📄 社区 issue 的完整日志显示：带 `--cookies-f firefox` 后 `--list-subs` 成功列出了 `ai-en`/`ai-zh`/`danmaku`（而不带则没有 AI 字幕）——见 [yt-dlp issue #14463](https://github.com/yt-dlp/yt-dlp/issues/14463)。这与我实测的「不带 Cookie 就没有 AI 字幕」完全吻合。

**这个功能现在还工作吗？（2025–2026 已知 issue）**

| Issue | 现状 | 说明 |
|---|---|---|
| [#14463](https://github.com/yt-dlp/yt-dlp/issues/14463)（2025-09-26 开，**仍 open**） | ⚠️ **真实存在但非致命** | ① B站 AI 字幕**没有被归入 `--write-auto-subs`**（不像 YouTube），所以只想下 AI 字幕时**必须用 `--write-subs`**；② 嵌入时语言码会变成 `ai-en`/`ai-zh` 而非标准 `eng`/`zho`；③ 嵌入字幕不写 `handler_name` 轨道名 |
| [#6357](https://github.com/yt-dlp/yt-dlp/issues/6357) | ⚠️ | 多分P视频**只下载第一P 的字幕**（历史 issue） |
| extractor 维护状态 | ✅ **活跃** | 最近一次相关提交：2026-07-03 `[ie/bilibili] Fix API extraction (#13730)` |
| ⚠️ 另注意 | — | 社区有反馈 B站 改策略后**必须带 cookies 才能下载**：[Reddit 讨论](https://www.reddit.com/r/youtubedl/comments/1stksfh/can_you_tell_me_why_ytdlp_on_my_end_can_no_longer/) |

**结论**：yt-dlp 抓 B站字幕**当前有效**，但（a）必须带登录 Cookie，（b）AI 字幕要用 `--write-subs` 而非 `--write-auto-subs`，（c）多分P 场景需自己按分P 循环。

来源：本机实测（yt-dlp 2026.08.19）；本机 yt-dlp 源码；[issue #14463](https://github.com/yt-dlp/yt-dlp/issues/14463)；[issue #6357](https://github.com/yt-dlp/yt-dlp/issues/6357)

---

## C. 音频下载（字幕缺失时的兜底）

### C10. yt-dlp 下载 B站音频的推荐参数

✅ **已实测端到端成功。** 我实际下载了一个音频文件：

```bash
$ python -m yt_dlp --ignore-config -f 30280 -o "dl_%(id)s.%(ext)s" \
    "https://www.bilibili.com/video/BV1GJ411x7h7/"
[download] 100% of 5.16MiB in 00:00:01 at 3.96MiB/s
$ ls dl_*
dl_BV1GJ411x7h7_pNA.m4a   5408198
```

**推荐参数（按场景）：**

```bash
# ① 只要音频、不转码（最快，得到 .m4a/AAC）—— 最推荐
yt-dlp -f bestaudio -o "%(title)s.%(ext)s" "URL"

# ② 明确要 192K 最高普通音质（B站 id 30280）
yt-dlp -f 30280 -o "%(title)s.%(ext)s" "URL"

# ③ 转成 mp3（需要 ffmpeg）
yt-dlp -x --audio-format mp3 --audio-quality 0 -o "%(title)s.%(ext)s" "URL"

# ④ 转成 m4a 并内嵌元数据/封面
yt-dlp -x --audio-format m4a --embed-metadata --embed-thumbnail "URL"

# ⑤ 带登录态（解锁更高音质/会员内容）
yt-dlp --cookies-from-browser edge -f bestaudio "URL"

# ⑥ 只挑 m4a，避免格式歧义
yt-dlp -f "bestaudio[ext=m4a]/bestaudio" "URL"
```

**实测的格式表（`yt-dlp -F`，未登录，4K 视频 BV1Tct1ztE26）：**
```
ID     EXT RESOLUTION |  TBR PROTO | VCODEC          ACODEC      ABR
30216  m4a audio only |   66k https | audio only      mp4a.40.2   66k
30232  m4a audio only |  112k https | audio only      mp4a.40.2  112k
30280  m4a audio only |  212k https | audio only      mp4a.40.2  212k
100048 mp4 1280x720   | 1094k https | avc1.640028    video only
100050 mp4 1920x1080  | 1755k https | avc1.640032    video only
```

> ⚠️ **`--embed-thumbnail` / `-x` 需要 ffmpeg**。我的实测环境**没有 ffmpeg**，所以只验证了「不转码的 m4a 下载」（成功）；转码类参数未实测。

来源：本机实测（音频下载成功）；yt-dlp 官方 [README](https://github.com/yt-dlp/yt-dlp)

---

### C11. 直接调 `playurl` 拿 DASH 音频流 vs yt-dlp

✅ **已实测：两种方式都能拿到音频，我验证了 URL、时效和 Referer 校验。**

**实测结论（关键）：**

```bash
# 拿 DASH 音频流地址（未登录，带 UA + Referer）
curl -s "https://api.bilibili.com/x/player/playurl?bvid=BV1Tct1ztE26&cid=31563845307&qn=127&fnver=0&fnval=4048&fourk=1" \
  -A "<Chrome UA>" -e "https://www.bilibili.com/"
# → code:0, data.dash.audio[] 含 id=30216/30232/30280

# ★ Referer 校验实测（对 audio 的 baseUrl 发 Range 请求）
带 Referer:     → HTTP 206 ✅
不带 Referer:   → HTTP 403 ❌   ← 铁证：CDN 强制校验 Referer
```

- ✅ **音频 URL 有 Referer 校验**：不带 `Referer: https://www.bilibili.com/` → **403**；带上 → **206**。
- ✅ **音频 URL 有时效性**：URL 里带 `deadline=1791044919`，当次请求的 `now_time` 为 `1791037708`，差值 **7211 秒 ≈ 2 小时**。📄 官方文档同样写明「**url 有效时间为 120min**」。
- ✅ **`platform=html5` 可绕开 Referer 鉴权**：实测 `fnval=1&platform=html5&high_quality=1` 返回的是 **MP4（durl）**，其 URL **不带 Referer 也能下**（实测 `206`）。适合「不方便设置 Referer」的场景，代价是音视频复用、画质受限。
- ⚠️ **接口新旧**：📄 官方文档把 `/x/player/playurl` 标记为**旧链接**，推荐 `/x/player/wbi/playurl`（需 WBI 签名）。但我实测**两者都不签名也返回 200/code 0**。→ **这条随时可能收紧，属于「不确定」风险点。**
- ✅ **yt-dlp 用的正是 `wbi/playurl`**（源码 L228–230），参数 `fnval=4048`，并额外带 `try_look=1`（未登录时）和一套设备指纹参数 `dm_img_inter` 等。

**优劣对比：**

| 维度 | 直接调 playurl | yt-dlp |
|---|---|---|
| 依赖 | 无（纯 requests） | 需安装 yt-dlp（转码还需 ffmpeg） |
| WBI 签名 | 新接口需要自己实现（旧接口当前免签名） | ✅ 内置，自动处理 |
| Referer 校验 | 必须自己加，否则 403 | ✅ 内置 `Referer` 头 |
| URL 时效 | 2 小时，需自己判断重取 | ✅ 自动 |
| 失败重试 / 风控 | 需自己实现 | ✅ 较完善 |
| 音视频合并/转码 | ❌ 需自己做（要 ffmpeg） | ✅ `-x` 一条命令 |
| 分P / 合集批量 | 需自己遍历 cid | ✅ 原生支持 |
| 精细控制 | ✅ 可直接拿 `dolby`/`flac`/`cur_language`（AI 原声翻译） | ⚠️ 有限 |
| 稳定性 | 接口一变就崩 | ✅ 社区持续修（2026-07 还在修） |

**可直接运行的直连代码（含 Referer、时效、音质选择）：**

```python
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}

def get_audio_urls(bvid: str, cid: int, sessdata: str | None = None):
    api = "https://api.bilibili.com/x/player/playurl"
    params = {"bvid": bvid, "cid": cid, "qn": 127,
              "fnver": 0, "fnval": 4048, "fourk": 1}
    headers = dict(HEADERS)
    if sessdata:
        headers["Cookie"] = f"SESSDATA={sessdata}"
    r = requests.get(api, params=params, headers=headers, timeout=15).json()
    if r["code"] != 0:
        raise RuntimeError(f"playurl 失败: {r['code']} {r['message']}")
    out = []
    for a in r["data"]["dash"]["audio"]:
        out.append({"id": a["id"],                 # 30216/30232/30280
                    "bandwidth": a["bandwidth"],   # bps
                    "url": a["baseUrl"]})          # 记得\字面里是 \u0026，json 解析后自动还原成 &
    return out

def download_audio(url: str, path: str):
    # ★ 必须带 Referer，否则 403
    with requests.get(url, headers=HEADERS, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        with open(path, "wb") as f:
            for chunk in resp.iter_content(1 << 16):
                f.write(chunk)
```

来源：本机实测（Referer 403/206、deadline≈+2h、html5 无鉴权）；[bilibili-API-collect 视频流 URL 文档](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/video/videostream_url.md)

---

### C12. 未登录能拿到什么音质？登录后有什么提升？

✅ **已实测确认（这是本次调研最有价值的发现之一）：**

**音频：未登录就能拿到最高普通音质 30280（192K）。**

实测 `playurl`（未登录，4K 视频 BV1Tct1ztE26）返回的 `data.dash.audio[]`：

| 音质代码 | 含义 | 实测 bandwidth (bps) | 实测 TBR | 未登录可用 |
|---|---|---|---|---|
| `30216` | 64K | 65,695 | 66k | ✅ |
| `30232` | 132K | 111,797 | 112k | ✅ |
| `30280` | **192K** | 211,542 | 212k | ✅ **可用** |
| `30250` | 杜比全景声 | — | — | ❌ 需大会员 |
| `30251` | Hi-Res 无损 | — | — | ❌ 实测 `"flac": null` |

**视频：未登录默认只有 360P/480P，但 `try_look=1` 可以解锁 720P/1080P！**

实测对照（同一视频，未登录）：

| 请求 | 返回的 video id | 实测分辨率 |
|---|---|---|
| `qn=16/32/64/80/120`（**不加** try_look） | `32, 16` | **480P, 360P**（qn 参数在 DASH 下被忽略） |
| `qn=80&...&` **`try_look=1`** | **`80, 64, 32, 16`** | **1080P, 720P, 480P, 360P** ✅ |

这与官方文档记载的 `try_look`（「未登录高画质」：为 1 时可以不登录拉到 64 和 80 清晰度）**完全吻合**，而且 yt-dlp 正是这么干的（源码 L359/L833 传 `try_look: 1`）—— 这解释了为什么我实测 `yt-dlp -F` 在**未登录**时也能列出 720P 和 1080P。

**登录后的提升（📄 官方文档）：**

| qn | 分辨率 | 要求 |
|---|---|---|
| 16 / 32 | 360P / 480P | 免登录 |
| 64 | 720P | **登录**（未登录靠 `try_look=1` 可绕过，见上） |
| 74 | 720P60 | 登录 |
| 80 | 1080P | **登录** |
| 100 | 智能修复 | 大会员 |
| 112 / 116 | 1080P+ / 1080P60 | **大会员** |
| 120 / 125 / 126 / 127 | 4K / HDR / 杜比视界 / 8K | **大会员** |
| 30250 / 30251 | 杜比音频 / Hi-Res 无损 | **大会员** |

**实测的登录提示**（yt-dlp 未登录时）：
```
[BiliBili] Format(s) 4K 超高清, 1080P 高码率 are missing; you have to become a premium member
           to download them. Use --cookies-from-browser or --cookies for the authentication.
```

> **对你的场景（字幕兜底 → 音频 ASR）的结论：音频完全不需要登录。** 未登录即可拿到 192K AAC，足够做语音识别。

来源：本机实测（audio id 表、try_look 对照、flac=null）；[bilibili-API-collect 视频流 URL 文档：音质代码表 / qn 表 / try_look](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/video/videostream_url.md)

---

### C13. 反爬限流？高频请求会怎样？推荐间隔/重试策略？

✅ **已实测 + 源码确认了限流错误码：`-352`。**

- ✅ **yt-dlp 源码把 `-352` 当作「限流」明确处理**（`bilibili.py` L231–241）：
  ```python
  code = traverse_obj(playurl_raw, ('code', {lambda x: x * -1}))
  ...
  expected = code in (401, 352)
  if expected:
      msg += ', please wait and try later'
  ```
  → **`-352` = 请求被限流，官方语义就是「等会儿再试」**。
- 📄 `-412 请求被拦截` 是另一类风控返回（更偏「IP/行为被判恶意」）。⚠️ **我在低频实测中未复现**。
- 📄 B站还有 `v_voucher`（gaia 验证码）机制，触发后需走验证流程：[v_voucher 验证文档](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/misc/sign/v_voucher.md)
- 📄 风控决策链（社区总结）：`IP 信誉 → 请求频率 → 行为模式 → 设备指纹 → API 权限`：[B站API风控深度解析](https://blog.csdn.net/gitblog_01078/article/details/159138055)

**推荐的请求间隔 / 重试策略**（综合一手实测与成熟开源实现）：

| 策略 | 建议值 | 依据 |
|---|---|---|
| 普通 API（view/player）请求间隔 | **1–2 秒**，加随机抖动 | 社区经验 |
| 批量下载场景间隔 | **2–8 秒随机** | ✅ 参考实现 [bilibili-subtitle-downloader](https://github.com/lululu811/bilibili-subtitle-downloader) 的「反爬处理」章节 |
| 单 IP 每分钟请求数 | **≤ 30** | ⚠️ 二手来源（CSDN，AIGC 辅助生成），**谨慎采信** |
| `-352` 重试 | **指数退避 + 抖动**，最多 3 次 | 参考实现：`-352` 自动等待重试（最多 3 次） |
| 连续失败熔断 | 连续 3 次 `-352` → 暂停 5 分钟 | ⚠️ 二手来源 |
| 降级信号 | 响应变慢（>3s）或字段缺失，也视为风控征兆 | ⚠️ 二手来源 |
| 会话管理 | 复用 `requests.Session`，带 `buvid3` cookie | ✅ 短链实测会下发 `buvid3` |

**可直接用的退避重试实现：**

```python
import random, time, requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
sess = requests.Session()
sess.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
sess.get("https://www.bilibili.com/", timeout=10)   # 先拿 buvid3 等 cookie

def api_get(url, params=None, max_retries=3, min_gap=1.5):
    """带节流 + 指数退避的 B 站 API 请求。"""
    for attempt in range(max_retries):
        time.sleep(min_gap + random.uniform(0, 1.0))       # 节流 + 抖动
        r = sess.get(url, params=params, timeout=15)
        if r.status_code == 412:                            # 风控拦截
            time.sleep(30 * (attempt + 1))
            continue
        try:
            j = r.json()
        except ValueError:
            time.sleep(3 * (attempt + 1)); continue
        code = j.get("code", 0)
        if code == 0:
            return j["data"]
        if code in (-352, -412, -509):                      # 限流 / 风控
            wait = (2 ** attempt) + random.uniform(0, 1)
            print(f"[限流 code={code}] 第 {attempt+1} 次重试，等待 {wait:.1f}s")
            time.sleep(wait)
            continue
        if code == -101:
            raise RuntimeError("未登录 / SESSDATA 已失效，请重新登录")
        raise RuntimeError(f"API 错误 code={code} message={j.get('message')}")
    raise RuntimeError(f"重试 {max_retries} 次仍失败: {url}")
```

来源：本机实测（`-352` 语义来自本机 yt-dlp 源码 L231–241）；[bilibili-subtitle-downloader 反爬处理](https://github.com/lululu811/bilibili-subtitle-downloader)；[B站API风控解析（二手，谨慎）](https://blog.csdn.net/gitblog_01078/article/details/159138055)；[v_voucher 文档](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/misc/sign/v_voucher.md)

---

## D. 风险与法律

### D14. 主要失效风险点

| 风险 | 严重度 | 实证 |
|---|---|---|
| **接口路径变更** | 🔴 高 | 已发生过：字幕接口从 `/x/player/v2` → **`/x/player/wbi/v2`**；取流从 `/x/player/playurl` → **`/x/player/wbi/playurl`**。📄 [社区反馈](https://github.com/pupa880/bilibili-subtitle-firefox)明确写「B站 API 更新，字幕获取接口从 /x/player/v2 改为 /x/player/wbi/v2，导致旧版本无法正确获取字幕」 |
| **WBI 签名强制化** | 🔴 高 | 2023 年起大量接口引入 `w_rid`/`wts`。目前 `player/v2`、`playurl`（旧路径）实测仍可免签名，但**随时可能收紧**（⚠️ 不确定） |
| **登录门槛提高** | 🟠 中高 | 字幕**已经**强制登录（本报告 B6 实测）。未来可能对音频也加登录要求 |
| **风控升级（AI 行为分析 + 设备指纹）** | 🟠 中高 | 已有 `v_voucher`(gaia)、`dm_img_inter` 设备指纹、`buvid3`。yt-dlp 现在都要伪造 `dm_img_inter`（源码 L205–210） |
| **`subtitle_url` 签名/时效变化** | 🟡 中 | URL 带 `auth_key`，是时效签名；CDN 域名（`aisubtitle.hdslb.com`）也可能变 |
| **SESSDATA 失效 / 账号被封** | 🟠 中高 | 有效期约 1 个月且可能更早失效；自动化触发风控会牵连账号 |
| **`subtitle_url` 协议相对 → 绝对的变化** | 🟢 低 | 社区旧代码写 `"https:" + url`，yt-dlp 现在直接用，说明格式变过 |

### D15. 官方 / 更稳妥的替代方案？

❌ **已实测确认：哔哩哔哩官方开放平台不提供字幕/音频获取能力。**

我查了官方文档中心（[openhome.bilibili.com/doc](https://openhome.bilibili.com/doc)），其 OPEN API 能力清单只有：
> 账号授权、用户管理、**视频管理**（稿件的发布、删除和查询）、数据开放、专栏管理、直播能力

**没有字幕 API，也没有音频下载 API。** 且申请需先完成身份认证（「请完成身份信息填写，审核通过后即可创建应用」，倾向企业/机构开发者）。📄 [开放平台开发者服务协议](https://open.bilibili.com/agreement/developer-service) 明确 B站对接口与内容享有知识产权。

**更稳妥的替代路径（按推荐度）：**

1. 🥇 **UP 主自助**：视频作者可通过 B站创作中心管理/下载自己的稿件与字幕（自己内容的合法路径）。
2. 🥈 **人工 + 浏览器**：对少量视频，直接在网页播放器开启字幕，配合浏览器扩展导出 SRT（有多个现成扩展，如 [Bilibili subtitle json2srt](https://chromewebstore.google.com/detail/bilibili-subtitle-json2sr/pcbfldjnfclglfkjbbdlkpommgigbdeb)）——但不适合自动化。
3. 🥉 **官方「视频 AI 总结」**（📄 需登录）：接口 `x/web-interface/view/conclusion/get`，返回结构化摘要。我实测**未登录返回 `-101 账号未登录`**。适合「要内容不要逐句字幕」的场景。
4. ⚠️ **降低采集频率 + 用小号 Cookie**，并把「抓取失败」当作正常分支处理。

**法律与合规提醒（务必评估）**：B站用户协议禁止未经许可的爬取与内容再分发；下载的音视频/字幕受**著作权**保护。个人学习研究用途与公开再分发/商业用途的法律风险差别极大。批量抓取还可能触发《反不正当竞争法》相关风险。**本报告仅为技术可行性调研，不构成合规建议。**

来源：本机实测（conclusion 接口 -403/-101）；[哔哩哔哩开放平台文档](https://openhome.bilibili.com/doc)；[开发者服务协议](https://open.bilibili.com/agreement/developer-service)

---

## 结论与推荐做法

### 一句话结论

> **音频：未登录就能拿到（192K AAC，无需任何凭证）。字幕：必须登录，没有免登录办法。**
> **最稳健方案 = yt-dlp（带浏览器 Cookie）优先，失败降级到直连 API + WBI 签名，字幕仍拿不到就下音频走本地 ASR。**

### 推荐降级链

```
┌─ 第 0 步：解析 BV 号 ──────────────────────────────────┐
│  b23.tv 短链 → 读 302 的 Location → 正则取 BV[0-9A-Za-z]{10}  │
│  再调 view 接口 → 拿 aid / cid / pages[]（分P 用各自 cid）      │
└──────────────────────────────────────────────────────┘
                        ↓
┌─ 第 1 步（首选）：yt-dlp + 浏览器 Cookie 抓字幕 ──────────┐
│  yt-dlp --cookies-from-browser <浏览器> \                  │
│         --write-subs --sub-langs "ai-zh,zh-CN,zh-Hans,ai-en,en-US" \
│         --skip-download "URL"                              │
│  ✅ 成功 → 得到 .srt，结束。                                │
│  ⚠️ 注意用 --write-subs（AI 字幕不算 --write-auto-subs）     │
└──────────────────────────────────────────────────────┘
                        ↓ 无字幕 / Cookie 不可用
┌─ 第 2 步：自研直连 API 兜底（带 SESSDATA + WBI 签名）──────┐
│  GET /x/player/wbi/v2?bvid=&cid=  （Cookie: SESSDATA=…）   │
│  → data.subtitle.subtitles[] → 取 lan 匹配 ai-zh/zh        │
│  → 下载 subtitle_url 的 JSON（补 https:，带 Referer）        │
│  → body[].from/to/content 转 SRT                          │
│  检查 data.need_login_subtitle：为 true 说明登录态失效        │
└──────────────────────────────────────────────────────┘
                        ↓ 仍无字幕
┌─ 第 3 步（兜底，且完全免登录）：下音频 → 本地 ASR ─────────┐
│  ★ 音频不需要登录！                                         │
│  yt-dlp -f bestaudio -o "%(title)s.%(ext)s" "URL"          │
│  或直连 playurl 取 dash.audio 里的 30280（192K）             │
│  → 用 faster-whisper / whisper.cpp 本地转写                 │
│  ⚠️ 直连时下载音频必须带 Referer，否则 403                    │
└──────────────────────────────────────────────────────┘
                        ↓ 连音频都不行（付费/充电专属）
┌─ 第 4 步：降级为「准文本」───────────────────────────────┐
│  · data.view_points[]  → AI 章节标题 + 时间点（免登录）      │
│  · comment.bilibili.com/{cid}.xml → 弹幕（免登录）          │
│  · x/web-interface/view/conclusion/get → AI 总结（需登录）   │
└──────────────────────────────────────────────────────┘
```

### 最佳实践清单

1. **优先用 yt-dlp，别自己造轮子**。它已经处理了 WBI 签名、Referer、`try_look`、设备指纹、`-352` 限流。安装：`pip install -U yt-dlp`（务必用**最新版**，extractor 2026-07 还在修）。转码需另装 ffmpeg。
2. **字幕一定带 Cookie**：`--cookies-from-browser` 最省事，`--cookies cookies.txt` 最适合服务器。
3. **用 `--write-subs` 而不是 `--write-auto-subs`** 抓 AI 字幕（B站的 AI 字幕没被归到 auto 类别）。
4. **音频不需要登录**：`-f bestaudio` 即可，未登录就有 192K AAC。
5. **直连 API 时**：一定带 `User-Agent` + `Referer: https://www.bilibili.com/`（音频 URL 不带 Referer = 403），并处理 2 小时 URL 过期。
6. **未登录想拿高清视频流**：加 `try_look=1`（实测可拿 1080P）。
7. **节流**：请求间隔 1–8 秒随机；`-352` 指数退避重试 3 次；`-101` 提示重新登录；`-412` 停手冷却。
8. **安全**：SESSDATA 只放环境变量/本地文件，**绝不进 Git**；用**小号**；给「登录失效」设计好降级分支。
9. **多分P/合集**：`view.data.pages[]` 逐P 用各自 `cid` 取字幕与音频；注意 yt-dlp 有「只下第一P 字幕」的历史 issue（#6357），批量场景建议自己按 cid 循环。

### 明确区分：哪些是「已确认可用」，哪些是「不确定」

**✅ 已实测确认可用（2026-10-03）**
- `x/web-interface/view`：正常，返回 title/desc/owner/duration/cid/aid/pages/stat/subtitle 元数据
- `view` 接口对 UA/Referer 不敏感（三种方式都 200）
- `b23.tv` 短链 302 + `Location` 头解析
- 132 分P 视频的 `data.pages[]` 完整结构
- `x/player/v2`、`x/player/wbi/v2` 均可用（HTTP 200 / code 0）
- **未登录时 `subtitles: []` + `need_login_subtitle: true`**（AI 字幕和 CC 字幕都一样）
- **WBI 签名正确也无济于事**（签名 ≠ 登录）
- `x/player/playurl` 未登录返回 DASH，音频 `30216/30232/30280` 全部可用
- CDN 音频 URL **不带 Referer → 403，带 → 206**
- URL 时效 ≈ 2 小时（`deadline - now_time ≈ 7211s`）
- `platform=html5` 的 MP4 流**无 Referer 鉴权**
- **`try_look=1` 未登录解锁 1080P/720P**
- yt-dlp 2026.08.19：`--list-subs`、`--write-subs`、`-f 30280` 音频下载（实测下载出 5.4MB m4a）
- yt-dlp 内部走的接口 = `x/player/wbi/v2`（源码级确认）
- 官方开放平台**无**字幕/音频能力
- `conclusion/get` AI 总结接口需登录（-101 / 无签名 -403）

**⚠️ 不确定 / 未复现 / 可能失效**
- 我**未复现** `-412`（低频测试下没触发）——不代表它不存在
- **`subtitle_url` 的实际请求头要求**未实测（无登录态）
- `view.data.ugc_season`（合集）结构**未实测到实例**（来自文档）
- `/x/player/playurl` 免签名可用，但**官方已标记为「旧链接」**，随时可能强制 WBI
- yt-dlp 转码参数（`-x --audio-format mp3`）**未实测**（环境无 ffmpeg）
- 限流具体阈值（30 次/分钟等）来自**二手来源**，非一手验证
- B站网页未登录显示部分 CC 字幕的现象，**未能用公开 API 复现**
- yt-dlp issue #14463（AI 字幕未归入 auto、语言码 `ai-xx`）**仍 open**，行为可能变化

---

## 2026-10-19 · 字幕「错位」事故与修复

### 症状

连续 3 条视频拿到「不是这条视频」的 B站 AI 字幕：

- `BV1TJxCzSEEZ p27`（黑马 Java 字符运算，4:05）→ 拿到一首流行歌的歌词（11 段，末段 4:14，长度恰好≈全片，骗过了纯时长闸门）；
- `BV1NKaV6bE7a`（何思远方，19:57）→ 拿到一条校园 vlog 的内容（覆盖率 39%）；
- `BV1mUbyzbEqi`（漫士，23:36）→ 拿到无关内容（覆盖率 58%）。

### 证据：同参数连续调用，结果不一致

| 视频 | aid | cid | 第 1 次轨道数 | 第 2 次轨道数 |
| --- | --- | --- | --- | --- |
| BV1TJxCzSEEZ p27 | 115344316368291 | 32954189495 | 1（内容=歌词） | 1（仍错） |
| BV1NKaV6bE7a | 117348774255126 | 42279110464 | 0 | 1（内容正确） |
| BV1mUbyzbEqi | 114909165655080 | 31258839810 | 6（含 en/ja/es/ar/pt） | 1（内容正确） |

⇒ `x/player/v2` 对同一 `(aid, cid)` **不保证确定性返回**：轨道数量、语言集合、指向的字幕记录都会变。
（aid/cid 本身是对的：已用 `x/player/pagelist?bvid=` 交叉验证，149 个分P 的第 27 个 cid 完全一致。）

### 关键观察：AI 字幕 URL 里编码了 `<aid><cid>`

`subtitle_url` 形如
`//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/<aid><cid><hash>?auth_key=...`

- 正确：`117348774255126` + `42279110464` → `.../prod/11734877425512642279110464ad0f...`
- 错位：黑马 p27 的 URL 前缀是 `113367540829731...`，**不等于** `11534431636829132954189495`
  ⇒ 拿到的是别人家视频的字幕记录。

人工字幕走 `bfs/subtitle/<hash>`，没有这个前缀 ⇒ 无法据此校验（标"未校验"，不拒绝）。

### 修复（`scripts/v_extract.py` + `app/bili/client.py`）

1. **归属校验**：AI 字幕 URL 必须包含 `f"{aid}{cid}"`；不匹配 ⇒ 丢弃并重试。
2. **覆盖率**：末段 < 全长 × 0.8 ⇒ 丢弃（`SUBTITLE_MIN_COVERAGE`）。
3. **歌词闸门**：含 `♪/♫/♬` 的段占比 ≥ 30% ⇒ 判为 BGM 歌词，丢弃（`looks_like_lyrics`）。
4. **重试 3 次**，第 2 次起 `use_cache=False`（`get_subtitle_tracks` 新增该参数）——本地缓存里可能正是那条错的。
5. 每次失败原因累积进 `subtitle.note`，便于事后诊断。
6. 音频侧另一起：CDN 会返回 `HTTP 567`；`_FORMAT_SELECTOR` 已改为 `ba[abr<=64000]/ba[abr<=132000]/…` + 重试 5 次，
   下载失败也不再中断任务（记 `transcript_error`，继续跑 OCR）。

### 验证（同一批视频回放）

| 视频 | 结果 |
| --- | --- |
| 黑马 p27 | 字幕被归属校验拦下（note 写明缺 `11534431636829132954189495`）→ 回落 Whisper（1,278 字，内容正确） |
| 何思远方 | 重试后命中正确字幕（64 段 / 6,952 字，开头「如果你把100张人脸数字化叠加…」） |
| 漫士 | 重试后命中正确字幕（60 段 / 6,626 字，开头「打开手机刷刷短视频…」） |

顺带一条：字幕命中时通常比 Whisper 更全（漫士：6,626 字 vs Whisper 4,656 字），所以"优先字幕"的策略仍然正确，
只要把校验补齐。

### 要记住的一句

**不要假设「命中字幕」= 「字幕对」**：B站 AI 字幕既可能对不上视频，也可能是 BGM 的歌词。
归属（URL 里的 aid+cid）+ 覆盖 + 歌词，三道闸缺一不可。

