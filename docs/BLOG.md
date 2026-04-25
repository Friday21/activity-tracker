# 用浏览器记录 + Mac Screen Time 搭一套「上网时间统计」

> 本文讲述如何通过 **Google My Activity** 抓取浏览记录、再结合 **macOS Screen Time（`knowledgeC.db` + Biome）** 的 app 使用数据，在 Mac 本地自动汇总成每日行为日志，并自动推送到自建 API。
>
> 适用平台：**macOS**（核心依赖 `knowledgeC.db` 与 Biome 的 `App.InFocus` 流）。
> Windows 用户：最后一节给出替代方案，只要最终上传的数据结构一致即可。

---

## 1. 动机

市面上做上网时间统计的方案通常有两个痛点：

1. **只看浏览器** —— 只统计 Chrome History，不知道 iPhone 上的抖音刷了多久。
2. **只看 Screen Time** —— Apple 的 Screen Time 能告诉你「Safari 用了 3 小时」，但不知道这 3 小时是在读 docs 还是在刷 X。

我想要一份**每天一份**的 JSON，里面同时含有：

- 今天打开过哪些网页（含标题、时间点）
- Mac 上每个 app 的前台时长
- iPhone 上每个 app 的前台时长
- 按「学习 / 工作 / 娱乐 / 社交 / 购物 / 新闻 / 工具 / 睡眠」分好类
- 自动计算昨晚的睡眠时长
- 自动推送到自建 API 供前端/大模型调用

最终效果：每天凌晨 3 点自动跑一次，一天一个 JSON 文件，同时上传到云端。

---

## 2. 架构总览

```
┌────────────────────────────────────────────────────────────┐
│  数据源                                                    │
│  ├── Google My Activity      （浏览器：跨设备浏览记录）   │
│  ├── ~/Library/Application    （Mac Screen Time）         │
│  │   Support/Knowledge/       （iPhone 部分 via iCloud）  │
│  │   knowledgeC.db                                         │
│  └── ~/Library/Biome/         （iPhone App.InFocus —— 提供│
│      streams/restricted/       真实前台时长，补上 knowledge│
│      App.InFocus/remote/       里 intents 是 0 秒的缺陷） │
└────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│  本地 Pipeline（run_daily.sh，每 2 小时 + 每天 03:00）     │
│  1. fetch_screentime.py   读 knowledgeC + Biome            │
│  2. fetch_activity.py     Playwright 登录 Google Activity  │
│  3. analyze.py            按 domain 分类 + 停留估算        │
│  4. build_daily_json.py   合并两路数据成一份 daily JSON    │
│  5. calc_sleep.py         从夜间 app 空白窗算睡眠          │
│  6. summary_from_report   生成文本日报                     │
│  7. notify.py             Telegram 推送（可选）            │
└────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│  outputs/daily/YYYY-MM-DD.json   （统一 schema）           │
└────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│  curl 上传到自建 API                                        │
│  POST /api/time/upload/<YOUR_OPEN_ID>/                     │
└────────────────────────────────────────────────────────────┘
```

---

## 3. 数据源一：Google My Activity

Chrome 本地 History SQLite 数据库有几个问题：

- 一台设备只能看到自己
- 切换 profile、无痕模式、iPhone Chrome 都不进来
- 同步到 Google 的数据反而更全

所以我们换个角度：**Google 自己在 [myactivity.google.com](https://myactivity.google.com) 已经聚合好了所有跨设备浏览行为**，直接从那儿抓。

实现：`scripts/fetch_activity.py` 用 Playwright 打开这个页面，向下滚动直到看到需要的历史日期，再解析 DOM 得到 `{url, title, visited_at, source, device}`。

首次需要人工登录（`scripts/setup_browser.py`），把 cookie 存到 `.browser_profile/`，之后 headless 自动复用。

示例产物（`inputs/activity/capture-*.jsonl`，每行一条）：

```json
{"url":"https://developer.chrome.com/docs","title":"Chrome Docs","visited_at":"2026-04-23T09:20:00+08:00","source":"google-activity","device":"MacBook Pro"}
{"url":"https://www.youtube.com/watch?v=abc","title":"Video","visited_at":"2026-04-23T10:10:00+08:00","source":"google-activity","device":"iPhone"}
```

### 去重

Activity 页面偶尔会重复同一条记录，统一做两步：

1. **URL 标准化**：小写 scheme/host、去 fragment、去追踪参数（`utm_*`, `fbclid`, `gclid` …）。
2. **事件去重键**：`normalized_url + minute_bucket + normalized_title`。

### 停留时间估算

浏览历史本身没有 duration，只有访问时间点。按相邻访问间隔估算：

```
duration = min( next_visit - this_visit, max_gap_seconds )
```

同域名连续访问给一个 bonus（`config.json` 里 `same_domain_continuation_bonus_seconds: 60`），避免把 YouTube 看到一半切标签就断档。

---

## 4. 数据源二：macOS Screen Time (`knowledgeC.db`)

macOS 把用户行为事件存在这里：

```
~/Library/Application Support/Knowledge/knowledgeC.db
```

这是一个普通的 SQLite，表结构大致是：

```
ZOBJECT              事件主表（stream / value / start / end）
ZSOURCE              事件来源（bundle_id / device_id）
ZSTRUCTUREDMETADATA  web usage 的 URL / title / domain
```

我们关心的 stream：

| stream | 含义 |
|---|---|
| `/app/usage` | Mac app 前台使用 |
| `/app/inFocus` | Mac 前台焦点 |
| `/app/webUsage` | Safari 网页使用 |
| `/safari/history` | Safari 历史 |
| `/app/intents` | iPhone/iPad Siri/Shortcut intents |
| `/notification/usage` | 通知事件 |

⚠️ 访问 `knowledgeC.db` 需要 **Full Disk Access**：
System Settings → Privacy & Security → Full Disk Access → 把终端 / iTerm / launchd 加进去。

读的时候要先把 db 拷贝到临时目录，避免锁：

```python
def _copy_db(src: Path) -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="knowledgeC_"))
    for suffix in ("", "-wal", "-shm"):
        s = Path(str(src) + suffix)
        if s.exists():
            shutil.copy2(s, tmpdir / (src.name + suffix))
    return tmpdir / src.name
```

时间戳是 **Apple epoch**（2001-01-01 UTC 起），转 UNIX 时要 `+ 978307200`。

### 区分 Mac / iPhone

`ZSOURCE.ZDEVICEID`：
- `NULL` → 本机 Mac
- 一个 UUID → iCloud 同步过来的另一台设备（iPhone / iPad / Watch）

想再细分是 iPhone 还是 iPad 只能通过 Biome 推断（下一节）。

---

## 5. 数据源三：Biome `App.InFocus`（iPhone 真实时长）

`knowledgeC.db` 里的 iPhone 数据主要是 `/app/intents`，这些事件的 **duration 全是 0**（它们本质是 intent 触发点，不是前台会话）。

想要 iPhone 上每个 app 实际用了多久，要去读 Biome：

```
~/Library/Biome/streams/restricted/App.InFocus/remote/<device-uuid>/*.segb
```

`.segb` 是 Apple 私有的事件流文件，格式大致是：

- 32 字节 header，后面是一串 protobuf 记录
- 每条记录里我们关心两个 tag：
  - `0x21`（field 4，64-bit fixed）→ Apple epoch 时间戳
  - `0x32`（field 6，length-delimited）→ 前台 bundle ID

扫描逻辑（精简版）：

```python
def _parse_segb_events(path: Path) -> list[tuple[float, str]]:
    data = path.read_bytes()
    if data[:4] != b"SEGB":
        return []
    events = []
    i = 32
    while i < len(data) - 12:
        if data[i] != 0x21:
            i += 1
            continue
        ts_val = struct.unpack_from("<d", data, i + 1)[0]
        # 在 ts_val 前后 200 字节窗口里找 bundle id
        ...
    return events
```

每个 UUID 目录对应一台设备。顶层最频繁出现的 bundle 如果是 `com.apple.carousel.*` 就是 Apple Watch，跳过。

一条 app 的时长 = **当前事件到下一个事件之间的时间差**，但上限 180 秒（避免熄屏期也被算成「微信用了 8 小时」）。

SpringBoard（`com.apple.SpringBoard.*`）作为切换边界，本身不入账。

这一路数据补齐后，iPhone 每个 app 终于有了真实 duration。

---

## 6. 合并成统一 JSON

`scripts/build_daily_json.py` 把两路数据合成一份：

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
      "category": "工作",
      "title": "VS Code",
      "start": "2026-04-23T14:02:00+08:00",
      "duration_seconds": 5420,
      "detail": "VS Code",
      "source": "mac"
    }
  ]
}
```

字段约定：

- `category` 中文八大类之一：`学习 / 工作 / 娱乐 / 社交 / 购物 / 新闻 / 工具 / 其他`，外加 `睡眠`。
- `title`：网页用 domain，app 用显示名。
- `detail`：网页用完整 URL，app 用 bundle ID 或 app 名。
- `source`：`browser` / `mac` / `iphone` / `calculated`（睡眠）。

**去重键：`(start, detail)`**。重复写入时后到的覆盖先到的（duration 会被新数据修正）。

### 分类规则

`config.json` 里定义 8 大类的 `domains` 关键词 + 通用 `keywords` 关键词；命中任一即归类。

`domain_categories.json` 是 **LLM 预分类的缓存**，key 是具体 domain 或 bundle id，value 是中文类目。形如：

```json
{
  "github.com": "工作",
  "com.ss.iphone.ugc.aweme": "娱乐",
  "bilibili.com": "娱乐"
}
```

跑分析时未命中的新 key 会写到顶层目录的 `unclassified_domains.txt`，我另外有个定时 Claude Agent 任务把它们批量分类回写。

---

## 7. 计算睡眠时长

`scripts/calc_sleep.py`：对每一天 D，扫描 iPhone 屏幕事件在 `[D-1 18:00 → D 14:00]` 区间内，找**最长的无活动间隔**，即昨夜睡眠。

小细节：

- 最短 gap 至少 2 小时才算睡眠。
- 凌晨闹钟事件（`com.apple.mobiletimer` 等）会把 gap 切断——要跳过这类事件。
- 结果写回 `outputs/daily/D.json`，category = `睡眠`。

---

## 8. 上传到自建 API

产物整理完后，用 curl POST 到服务端：

```bash
python3 -c "
import json
with open('outputs/daily/2026-04-23.json') as f:
    data = json.load(f)
print(json.dumps({'items': data['items']}))
" | curl -s -X POST \
  https://your-api.example.com/api/time/upload/<YOUR_OPEN_ID>/ \
  -H "Content-Type: application/json" \
  -d @-
```

> `<YOUR_OPEN_ID>` 是服务端签发给你的一次性上传凭证，放在配置文件里，不要写死在代码里，也不要上传到 GitHub。

服务端返回形如：

```json
{"code": 0, "msg": "上传成功",
 "data": {"total": 619, "created": 128, "updated": 0, "skipped": 491, "errors": 0}}
```

服务端也按 `(start, detail)` 判重，skipped 多是正常现象（数据早已存在）。

---

## 9. 定时执行

macOS 下两个思路：

### a) launchd（推荐）

写一份 `com.user.activity-tracker.plist` 放到 `~/Library/LaunchAgents/`：

```xml
<!-- 每 2 小时 + 每天 03:00 触发 -->
<key>StartCalendarInterval</key>
<array>
  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
  <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <dict><key>Hour</key><integer>11</integer><key>Minute</key><integer>0</integer></dict>
  <!-- ... 每 2 小时 ... -->
</array>
```

`launchctl load ~/Library/LaunchAgents/com.user.activity-tracker.plist` 即可。

### b) cron

```
0 3,9,11,13,15,17,19,21,23 * * * cd ~/activity-tracker && ./run_daily.sh
```

注意：`run_daily.sh` 自带 quiet-hours 保护，04:00–08:59 自动跳过，避免误触。

### c) Claude Code Scheduled Tasks

我自己是用 Claude Code 的 `scheduled-tasks` 跑的，好处是：
- 直接用自然语言描述触发条件与后续行为（包括 curl 上传、失败时做什么）
- 不用维护 plist / crontab
- 执行日志直接在 Claude 会话里能看到

---

## 10. Windows 用户怎么办？

核心差异：

| 能力 | macOS | Windows |
|---|---|---|
| 浏览器跨设备记录 | Google My Activity 抓取（同样适用） | **同样可用** |
| 本机 app 使用时长 | `knowledgeC.db` + Biome | 改用 Windows 的 **ActivitiesCache.db**（`%LOCALAPPDATA%\ConnectedDevicesPlatform\...\ActivitiesCache.db`） 或 [ActivityWatch](https://activitywatch.net/) |
| iPhone app 使用时长 | 需要 Mac + iCloud 同步才能读到 | Windows 没办法直接拿，只能用 iPhone 端的快捷指令导出或 Screen Time Export（app 一小时粒度） |

只要**最终产物符合 §6 的 JSON schema**（`items[].{category, title, start, duration_seconds, detail, source}`），后续上传逻辑完全通用。

给 Windows 用户的一条路径建议：

1. **浏览器侧**：直接复用本项目的 `fetch_activity.py`（Playwright 跨平台）。
2. **本机 app**：用 [ActivityWatch](https://activitywatch.net/)（开源，Windows 有 installer）采集 `afk` + `window` 两个 bucket。
3. **iPhone 侧**：接受「只能到每小时粒度」的现实，用 Shortcuts 每天导出一次 Screen Time。
4. 写一小段适配器把上面三路数据映射成 §6 的 schema，然后复用相同的 upload 逻辑。

---

## 11. 开源项目

本文对应的开源项目（Mac 版）：

👉 [`github.com/<你的用户名>/activity-tracker-mac`](https://github.com/)

下载后跑一遍 `./install.sh`，登录一次 Google，再 `./schedule.sh install` 就挂进 launchd 每天自动跑。数据全在本地，API 端地址和 openId 都放在 `config.json` 里自行填写。

---

## 12. 延伸阅读

- [mac-apt / knowledgeC schema](https://github.com/ydkhatri/mac_apt) —— 各 stream 和列的解读
- [Apple Biome reverse engineering](https://github.com/mac4n6/APOLLO) —— `.segb` 的早期解析工作
- [Google Takeout](https://takeout.google.com/) —— 如果不想跑 Playwright，也可以手动导出 Activity
