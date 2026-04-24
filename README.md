# 时间追踪

在本机把你在网上做的一切汇总成一份干净的 JSON 日志。

> 🌐 Language: **中文** · [English](README.en.md)

本项目把三路数据源合并成一份按天落地的 JSON：

- **浏览历史** —— 从 Google My Activity 抓取（覆盖所有已登录 Google 帐号的 Chrome / Safari 设备）
- **macOS app 使用时长** —— 读 `knowledgeC.db`
- **iPhone app 使用时长** —— 读 Biome `App.InFocus` SEGB（通过 iCloud 同步到 Mac）

还会基于夜间手机闲置时段自动算出睡眠时长，并可选地把当天结果上传到你自建的 HTTP API。

> **适用平台：仅 macOS。** `knowledgeC.db` 和 Biome 是 Apple 私有数据源。Windows 用户请看下文 [Windows 适配](#windows-适配)。

## 你会得到什么

每天 03:00（以及白天每 2 小时）自动运行，产物是：

```
outputs/daily/2026-04-23.json
```

结构如下：

```json
{
  "date": "2026-04-23",
  "updated_at": "2026-04-24T09:08:12+08:00",
  "items": [
    {
      "category": "工作",
      "title": "github.com",
      "start": "2026-04-23T10:23:45+08:00",
      "duration_seconds": 697,
      "detail": "https://github.com/user/repo/pull/123",
      "source": "browser"
    },
    {
      "category": "娱乐",
      "title": "抖音",
      "start": "2026-04-23T21:05:10+08:00",
      "duration_seconds": 1820,
      "detail": "com.ss.iphone.ugc.Aweme",
      "source": "iphone"
    },
    {
      "category": "睡眠",
      "title": "睡眠",
      "start": "2026-04-23T00:11:00+08:00",
      "duration_seconds": 29280,
      "detail": "sleep",
      "source": "calculated"
    }
  ]
}
```

分类（中文，可在 config 里改）：`学习 / 工作 / 娱乐 / 社交 / 购物 / 新闻 / 工具 / 其他 / 睡眠`。

数据来源（`source` 字段）：`browser` / `mac` / `iphone` / `calculated`。

完整的设计思路与数据源原理见 [`docs/BLOG.md`](docs/BLOG.md)。

---

## 一键上手

```bash
git clone https://github.com/Friday21/activity-tracker.git
cd activity-tracker-mac

./install.sh                                   # 创建 venv、装依赖、装 Playwright
.venv/bin/python3 scripts/setup_browser.py     # 首次登录 Google（会弹出 Chromium 窗口）

cp config.example.json config.json             # 然后按需编辑，见下文
./run_daily.sh                                 # 先手动跑一次试试

./schedule.sh install                          # 挂进 launchd，自动每天定时跑
```

产物在 `outputs/daily/YYYY-MM-DD.json`。

---

## 运行前提

1. **macOS 12 或更高**（已在 14 Sonoma 和 15 Sequoia 上测过）。
2. **Python 3.10+**（`python3 --version` 看一下）。
3. **Full Disk Access**（完全磁盘访问权限）。没这个权限，`knowledgeC.db` 会读不出来而且不会报错。

   System Settings → Privacy & Security → **Full Disk Access** → 把**真正执行脚本的二进制**加进去：
   - `Terminal.app` / `iTerm.app`（手动跑脚本时）
   - `/bin/zsh`（launchd 跑脚本时——就是 plist 里写的解释器）
   - 有些 macOS 版本还要把 `launchd` 本身加进去

   加完以后把这一项关了再打开，macOS 的 TCC 权限是按二进制哈希缓存的。

4. **有活动记录的 Google 帐号**。如果你在 Google 帐号里关掉了「网络与应用活动」（Web & App Activity），这套东西就没数据可抓，需要先到 [myactivity.google.com/activitycontrols](https://myactivity.google.com/activitycontrols) 打开。

---

## 配置

把 `config.example.json` 拷成 `config.json`，编辑：

```jsonc
{
  "timezone": "Asia/Shanghai",

  "upload": {
    "enabled": true,
    "endpoint": "https://your-api.example.com/api/time/upload/{open_id}/",
    "open_id": "在这里贴你的 OPEN_ID"
  },

  "notifications": {
    "telegram": {
      "bot_token": "",
      "chat_id": ""
    }
  },

  "top_domains_limit": 15,
  "top_urls_limit": 20,
  "default_gap_seconds": 30,
  "max_gap_seconds": 1800,
  "same_domain_continuation_bonus_seconds": 60,

  "categories": {
    "学习": { "domains": ["wikipedia.org", "..."], "keywords": ["docs", "..."] },
    "工作": { "domains": ["..."], "keywords": ["..."] }
  }
}
```

- **`upload.enabled: false`** 如果你只想本地落 JSON、不想上传远端。
- **`upload.open_id`** 千万别 commit 到 git！`config.json` 已经在 `.gitignore` 里。
- **`categories`** 默认的 8 个中文大类。可以自由改名或换成英文，后续代码不 care 具体名字。
- **`domain_categories.json`**（独立文件）—— 具体 domain 或 bundle id 的分类覆盖，比如 `"github.com": "工作"`。运行时遇到未分类的新 key 会累积到 `outputs/unclassified_domains.txt` 供你人工或用 LLM 批处理。

---

## pipeline 做了什么

`run_daily.sh` 按顺序跑 6 步，前一步失败不影响后面继续跑：

| # | 脚本 | 产物 |
|---|---|---|
| 1 | `scripts/fetch_screentime.py --days 7` | `outputs/screentime/YYYY-MM-DD.json` —— Mac + iPhone 事件合并后落盘 |
| 2 | `scripts/fetch_activity.py --headless` | `inputs/activity/capture-*.jsonl` —— 浏览器原始记录 |
| 3 | `src/analyze.py --day today` / `--day yesterday` | `outputs/data/YYYY-MM-DD.json` —— 去重、分类、估算停留时长后的浏览明细 |
| 4 | `scripts/build_daily_json.py` | `outputs/daily/YYYY-MM-DD.json` —— 合并后的统一日报 |
| 5 | `scripts/calc_sleep.py --all` | 往每份 daily JSON 里塞一条 `睡眠` item |
| 6 | `scripts/summary_from_report.py` | `outputs/daily/YYYY-MM-DD.summary.txt` —— 人读的文字版摘要 |
| 7 | `scripts/upload.py`（可选） | POST 到你自己的 API |

**静默时段**：脚本自己在 04:00–08:59 会跳过运行（除非设 `FORCE_RUN=1`）——那时候人在睡觉，抓浏览器只是白白耗 2 分钟。

---

## 定时执行

最简单：

```bash
./schedule.sh install      # 安装 launchd agent
./schedule.sh uninstall    # 卸载
./schedule.sh status       # 看下次触发时间
./schedule.sh run          # 立即触发一次
./schedule.sh logs         # 跟着看今天的日志
```

底层逻辑：用 [`launchd/com.user.activity-tracker.plist.template`](launchd/com.user.activity-tracker.plist.template) 模板写一份 plist，把项目绝对路径替换进去，再 `launchctl load` 到 `~/Library/LaunchAgents/`。

默认触发计划：**每天 03:00** + **09:00–23:00 每 2 小时一次**。

想用 cron 也行：

```cron
0 3,9,11,13,15,17,19,21,23 * * * cd /绝对路径/activity-tracker-mac && ./run_daily.sh
```

---

## 手动用法

拉最近 7 天的屏幕使用数据：

```bash
.venv/bin/python3 scripts/fetch_screentime.py --days 7
```

headless 模式抓 Google Activity（用已保存的登录态）：

```bash
.venv/bin/python3 scripts/fetch_activity.py --headless
```

分析某一天：

```bash
.venv/bin/python3 src/analyze.py --day 2026-04-23
.venv/bin/python3 src/analyze.py --day yesterday
```

把已有数据重新合并成 daily JSON：

```bash
.venv/bin/python3 scripts/build_daily_json.py
```

上传单独一天：

```bash
.venv/bin/python3 scripts/upload.py 2026-04-23
```

上传所有已生成的 daily JSON：

```bash
.venv/bin/python3 scripts/upload.py --all
```

---

## 上传协议

开启 `upload.enabled: true` 后，脚本会把每份 daily JSON POST 到：

```
POST {endpoint}
Content-Type: application/json

{ "items": [ ... ] }
```

其中 `{endpoint}` 会自动把配置里的 `{open_id}` 占位替换成真实的 open_id。

你自己的服务端应该：

- 接受 `{ items: [...] }` 格式
- 按 `(start, detail)` 判重（跟本地去重规则一致）
- 返回任意 JSON（脚本只负责把响应打到日志）

一个可参考的响应格式：

```json
{
  "code": 0,
  "msg": "上传成功",
  "data": { "total": 619, "created": 128, "updated": 0, "skipped": 491, "errors": 0 }
}
```

---

## 项目结构

```
activity-tracker-mac/
├── README.md                           ← 你正在看的（中文）
├── README.en.md                        ← English version
├── docs/BLOG.md                        ← 完整原理 / 架构博客
├── install.sh                          ← 一键安装（venv + 依赖 + playwright）
├── schedule.sh                         ← launchd agent 安装/卸载
├── run_daily.sh                        ← pipeline 入口
├── config.example.json
├── domain_categories.json              ← domain → 分类映射（可扩展）
├── requirements.txt
├── launchd/
│   └── com.user.activity-tracker.plist.template
├── scripts/
│   ├── fetch_screentime.py             ← knowledgeC.db + Biome 读取
│   ├── fetch_activity.py               ← Google My Activity 爬取
│   ├── setup_browser.py                ← 首次 Google 登录
│   ├── build_daily_json.py             ← 合并 browser + screentime
│   ├── calc_sleep.py                   ← 从 iPhone 空闲区间算睡眠
│   ├── summary_from_report.py          ← 生成人读摘要
│   ├── upload.py                       ← POST daily JSON 到你的 API
│   └── notify.py                       ← Telegram 推送（可选）
├── src/
│   └── analyze.py                      ← 浏览记录分析器
├── inputs/activity/                    ← Playwright 抓取结果（自动生成）
├── outputs/
│   ├── screentime/YYYY-MM-DD.json
│   ├── data/YYYY-MM-DD.json            ← 浏览记录明细
│   ├── daily/YYYY-MM-DD.json           ← ★ 每日最终产物
│   ├── daily/YYYY-MM-DD.summary.txt
│   ├── logs/
│   └── unclassified_domains.txt
└── examples/
    └── daily.example.json
```

---

## Windows 适配

三路数据源对应 Windows 的替代方案：

| 数据源 | macOS | Windows 替代 |
|---|---|---|
| 浏览记录 | `scripts/fetch_activity.py`（Google My Activity + Playwright） | **原脚本直接能用**，Playwright 是跨平台的 |
| 本机 app 使用 | `knowledgeC.db` + Biome | [ActivityWatch](https://activitywatch.net/)（开源，Windows 有安装包）—— 用它的 `aw-watcher-window` + `aw-watcher-afk` 两个 bucket。也可以直接读 `%LOCALAPPDATA%\ConnectedDevicesPlatform\...\ActivitiesCache.db`（Windows Timeline 数据）。 |
| iPhone app 使用 | Biome（通过 iCloud 同步到 Mac） | **Windows 上直接拿不到。** 退而求其次：iPhone 上写个 Shortcut，每天导出一次 Screen Time（只有每小时粒度）。 |

只要你的 Windows 适配器最终产出的 JSON **字段结构符合 `items[].{category, title, start, duration_seconds, detail, source}` 这份 schema**，后面的睡眠计算、上传、服务端去重全部都能复用。

欢迎 PR 贡献 Windows 适配器。

---

## 隐私

所有处理都在本地完成。脚本唯一会发出的网络请求是：

- Playwright 访问 `myactivity.google.com`（你自己的 Google 帐号）
- Telegram 通知（如果你配了）
- POST 到你自己的 API 端点（如果你配了）

**不会**有任何数据发往第三方服务。`.browser_profile/`、`config.json`、`inputs/`、`outputs/` 全部在 `.gitignore` 里。

---

## 故障排查

**`ERROR: cannot read knowledgeC.db`** —— 少了 Full Disk Access，见上文 [运行前提](#运行前提)。

**Playwright 抓出来 0 条** —— Google 登录 cookie 过期了，重新跑一次 `scripts/setup_browser.py`。

**iPhone 数据完全没有** —— iPhone 端 Screen Time 的 iCloud 同步要打开：Settings → [你的名字] → iCloud → Screen Time → ON，并且这台 Mac 至少和 iPhone 同步过一次。

**iPhone duration 数值偏小** —— `BIOME_MAX_GAP_SECONDS`（默认 180 秒）会把单次前台时长截到 3 分钟以内。如果某个 app 确实前台超过 3 分钟没有任何其他事件，它会被截断。想改就在 `scripts/fetch_screentime.py` 里改这个常量。

**Full Disk Access 加了还是读不到** —— 在 System Settings 里把这一项关了再打开，确认你加的**就是 launchd 调起来的那个二进制**（通常是 `/bin/zsh`，不是 Terminal）。

---

## License

MIT，见 [LICENSE](LICENSE)。

---

## 致谢

- `knowledgeC.db` 的表结构和字段解读大量参考了 [mac_apt](https://github.com/ydkhatri/mac_apt) 和 [APOLLO](https://github.com/mac4n6/APOLLO)。
- Google My Activity 的 Playwright 抓取思路借鉴社区里广为流传的若干做法。
