# llm-quota-watchdog

**一个页面 + 智能推送，看住你所有大模型编程套餐的额度。**
支持 Claude Pro/Max · Codex Plus/Pro · Kimi for Coding · GLM Coding Plan

[English](README.md)

Python 3.8+，**只用标准库，零依赖**。输出静态 HTML，无数据库、无守护进程。

---

## 为什么做这个

订阅制编程套餐（Claude Pro/Max、ChatGPT Codex、Kimi for Coding、智谱 GLM Coding Plan）都有 **5 小时和每周两套额度窗口**，但是：

- 没有任何一个地方能统一查看各家的剩余额度；
- 中转站面板（new-api、CPA-Manager-Plus 等）只能看到中转站侧的流量，看不到上游订阅的真实窗口；
- Kimi for Coding 根本没有文档化的额度 API（本项目的接口是从 kimi-cli 源码里挖出来的）。

llm-quota-watchdog 调用官方 CLI 自己使用的接口，生成一个静态页面，并在你烧穿窗口之前——或者浪费窗口之前——推送提醒。

## 功能

### 额度仪表盘（静态 HTML，cron 定时生成）

每个账号：5 小时 / 每周 / 月度（手动快照）进度条、精确百分比、柱状图上的时间进度刻度线、重置倒计时、用量 vs 时间节奏（偏快/偏慢）、套餐到期倒计时。自适应满宽图表让所有轨道便于横向比较，并显示跨平台容量档位；账号多时可切紧凑或极简视图。

顶部一行摘要直接告诉你现在该关心谁：`5/5 正常，额度最高 Codex Pro 7天 100%，4天后重置`，有账号逼近上限时整条变橙。

![llm-quota-watchdog Claude Codex Kimi GLM 编程套餐统一额度仪表盘](docs/screenshot.png)

*当前横向仪表盘，已开启隐私模式并使用演示数据。*

除**刷新全部**外，页面顶栏还有两个显示控制：

- **隐私模式**：一键隐藏邮箱副标题、各账号更新时间、页时间戳，方便截图分享时不泄漏可识别信息。只对当前标签页生效，刷新即恢复，按钮会变橙提醒你别忘了关。
- **显示设置**（纯前端，存在浏览器 localStorage 里，不影响别人看到的页面，也不需要任何后端）：

| 能调什么 | 选项 |
|---|---|
| 主题 | **跟随系统** / 深色 / 浅色（默认白天浅、晚上深，按你的系统自动切；也可手动锁定） |
| 密度 | **横向图表**（默认，满宽使用率条 + 独立容量档位）/ 紧凑图表 / 极简单行 |
| 账号 | 同供应商默认相邻；可直接拖动账号行，或在设置里用 ↑↓ 调整顺序，并可逐个显示或隐藏 |
| 排序 | **快到期且未用完**（默认：按每张卡最长周期的重置时间从近到远，已用完的沉后，短周期不干扰）/ 按浪费速度 / 按用量高低 / 自定义顺序；自动排序仍保持同供应商相邻 |
| 显示内容 | 健康徽章、卡片副标题、重置时间、节奏提示、更新时间、套餐到期、顶部摘要，逐项开关 |
| 自动刷新 | 关闭 / 5 分钟 ~ 3 小时 |
| 备份 | 复制配置 / 下载文件 / 粘贴导入 / 上传文件、一键恢复默认 |

![llm-quota-watchdog 编程套餐额度极简视图](docs/screenshot-mini.png)

*极简密度把每个账号压成一行，同时保留关键额度信号。*

> 想截图分享又不想露邮箱？顶栏点一下「隐私模式」即可。

页面上还有"全部刷新"按钮和每张卡片自己的刷新链接。这些都是纯前端链接，指向 `/refresh`（可带 `?account=<label>`）——本仓库不附带这个端点的服务端实现，不接的话按钮点了就是 404，不影响页面本身。想接的话见下文[「可选：按需刷新」](#可选按需刷新)。

### 推送告警（Bark / ntfy）

| 告警 | 触发条件 |
|---|---|
| 🔴 快用完 | 5h 窗口 ≥80%，周窗口 ≥90%（消息附带重置倒计时，方便安排下阶段使用） |
| 🟠 用太快 | 周窗口用量超过时间进度 15 个点 |
| ⏱ 节奏指示 | 每个窗口都显示时间进度 vs 用量，偏快偏慢一眼可见 |
| 🟡 赶紧用 | 时间过半但用量落后 30 点 · 或 ≤26 小时重置且用量 ≤60% |
| 🟢 满血复活 | 检测到窗口重置（用量掉 30 点以上） |
| 📅 套餐到期 | 手动配置的到期日前 7/3/1 天 |
| 📊 每日汇总 | 每天一条完整报告，无论有无异常 |

每条告警**一个周期只推一次**（状态文件去重），不刷屏。
大套餐不想被念叨的账号可以放进 `relaxed_accounts`，只保留"快用完"。

### 可选：CLIProxyAPI 认证文件健康检测

如果你在用 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)，在配置里设置 `cliproxyapi_management_key_file` 指向一个存有管理 API key 的文件，就能解锁：

- 每张卡片上的 🟢/🔴/🟡 健康徽章（异常时页面顶部还会多一条汇总横幅），信号来自 CLIProxyAPI 自己的本地管理 API，而不是"额度请求失败就猜 token 坏了"——更准确，且额度请求本身还没报错之前就能发现
- `check-auth` 子命令：只有异常账号集合**发生变化**时才推送（token 过期→推一次；恢复→再推一次；其余时候静默）

这是一次**本地调用**（默认 `http://127.0.0.1:8317/...`），不受下面轮询频率那条"别对上游 API 太频繁"的顾虑影响。不过 `check-auth` 仍然建议放在自己独立的**低频** cron 行里（一天一次足够判断"token 是不是又过期了"）——刻意没有并进每小时的 `watchdog` 循环，因为 token 过期一小时后还是过期，没必要重复提醒。

不配置 `cliproxyapi_management_key_file` 的话，这一整块都不会生效：没有徽章、没有横幅，`check-auth` 静默跳过。完全可选。

## 数据来源

调用的是官方 CLI 自己用的接口：

| 提供商 | 接口 | 凭据 |
|---|---|---|
| Claude | `api.anthropic.com/api/oauth/usage` | CLIProxyAPI 认证文件里的 OAuth token（自动发现） |
| Codex | `chatgpt.com/backend-api/wham/usage` | CLIProxyAPI 认证文件里的 OAuth token + account id（自动发现） |
| Kimi for Coding | `api.kimi.com/coding/v1/usages` | 你的 coding 套餐 API key（kimi.com 控制台生成） |
| GLM Coding Plan | `open.bigmodel.cn/api/monitor/usage/quota/limit` | 你的智谱 API key（Authorization 头直接放，**不带 Bearer**） |

如果你在用 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)，把 `cliproxyapi_auth_dir` 指向它的认证目录即可，所有 Claude/Codex 账号自动接入，零配置。

> Kimi 的**月度总配额**（网页控制台里混算 Kimi 聊天 + Code 的那个）没有文档化的 API，但网页控制台本身调用了一个内部 RPC（`kimi.com/apiv2/.../GetSubscription`），鉴权用的是网页登录态 token 而不是 coding API key。给 Kimi 账号配置 `monthly_web_token_file` 后，仪表盘会在每次 `watchdog` 运行时自动刷新这个数字，快用完时还会告警——见下文[Kimi 月度配额 token](#kimi-月度配额-token)。不配的话就退化成手动维护快照（`monthly_snapshot`）。

## 快速开始

```bash
curl -fsSL https://raw.githubusercontent.com/toolazytoname/llm-quota-watchdog/main/install.sh | bash
```

安装脚本会下载主程序、生成 `~/.local/share/llm-quota-watchdog/config.json`、询问 Bark 地址 / Kimi key / 认证目录，并安装 cron：

```
47 * * * *  watchdog            # 每小时静默检查，只在触发规则时推送
 5 9 * * *  watchdog --summary  # 每天 9:05（服务器本地时间）推全量报告
12 * * * *  page                # 每小时重新生成静态页面
```

如果配置了 `cliproxyapi_management_key_file`，再加一条可选的每日健康检测（为什么单独一条低频的、不并进 `watchdog`，见上文"可选：CLIProxyAPI 认证文件健康检测"一节）：

```
0 18 * * *  check-auth          # 一天一次就够，状态不变不推送
```

测试：

```bash
llm-quota-watchdog watchdog --summary --config ~/.local/share/llm-quota-watchdog/config.json
llm-quota-watchdog page --config ~/.local/share/llm-quota-watchdog/config.json
```

用任意 Web 服务器托管页面，比如 nginx：

```nginx
location /quota/ {
    auth_basic "quota";
    auth_basic_user_file /etc/nginx/.htpasswd-quota;
    alias /home/YOU/.local/share/llm-quota-watchdog/www/;
    add_header Cache-Control "no-cache, must-revalidate" always;
}
```

> ⚠️ 页面会暴露你的订阅用量，务必加密码或只放内网。
>
> ⚠️ `Cache-Control` 这行不要省。没有它的话，页面响应只有 `Last-Modified`/`ETag`，浏览器会按 HTTP 规范做启发式缓存——点"刷新"按钮（见下文[可选：按需刷新](#可选按需刷新)）时，fetch 跟随重定向可能直接命中浏览器本地缓存而不是真的打到服务器，即便页面其实已经重新生成好了，界面看起来还是像"点了没反应"。

## 配置说明

见 [config.example.json](config.example.json)，所有 key 都有合理默认值。重点：

| 配置 | 说明 |
|---|---|
| `bark_url` / `ntfy_url` | 推送通道，可配任一或都配 |
| `bark_url_file` / `ntfy_url_file` | 同上，但从文件读（key 不落进 config.json），配了文件就以文件为准 |
| `page_title` | 页面标题，默认「大模型额度监控」 |
| `cliproxyapi_auth_dir` | CLIProxyAPI 的 OAuth `*.json` 目录，Claude/Codex 自动发现 |
| `accounts` | 显式账号列表，也决定卡片默认顺序。每项可配 `label`（卡片标题）、`sub`（副标题，如邮箱或套餐档位）、`quota_factor`（同供应商真实倍率）、`capacity_index`（跨平台近似容量指数）、`quota_label`/`quota_labels`（原生额度文案）、`api_key`/`api_key_file`（Kimi/GLM）或 `auth_file`（Claude/Codex） |
| `relaxed_accounts` | 只保留"快用完"告警的账号标签 |
| `plan_expiry` | `{"Kimi Coding": "2026-08-22"}` → 页面倒计时 + 到期提醒（key 用账号 label） |
| `monthly_snapshot` | 月度配额快照，显示在页面上（key 用账号 label）；没配 `monthly_web_token_file` 的账号需手动维护，配了的会自动刷新 |
| `thresholds` | 所有告警阈值都可调 |
| `timezone_offset_hours` | 显示/报告时区（默认 UTC+8） |
| `page_state_file` | 页面缓存：存每个账号最后一次拉取的结果，`page --account` 靠它只刷一个账号 |
| `cliproxyapi_management_key_file` | 可选，配置后解锁认证文件健康徽章 + `check-auth`（见上文） |
| `cliproxyapi_management_url` | CLIProxyAPI 管理 API 地址，默认 `http://127.0.0.1:8317/v0/management/auth-files` |

`accounts` 留空也能跑：Claude/Codex 会从 `cliproxyapi_auth_dir` 自动发现，卡片标题就是认证文件名。想要好看的标题和副标题，再显式写进 `accounts` 即可——显式配过的认证文件不会被重复自动发现，所以两个 Codex 账号只写一个也不会漏掉另一个。

所有使用率轨道都保持满宽，只表达 0–100% 的已用比例，因此小套餐不会再被压成难以辨认的短线。`quota_factor` 表示**同一供应商内的真实额度倍率，不是价格倍率**。`capacity_index` 是把不同供应商放在一起的人工近似指数，并不把 Codex messages、GLM credits 和 Kimi units 伪装成同一种 token；未配置原生额度文案时，页面会明确显示“跨平台≈N×”。套餐体量在轨道旁用三级短标识帮助扫视（不高于 1×、不高于 6×、高于 6×），同时保留配置中的准确倍率或原生额度文案。每个账号会把可用的最长周期排在最前面。`quota_label` 用于通用原生额度文案；如果 5 小时与周窗口的绝对数不同，用 `quota_labels: {"5h": "12,000 credits / 5小时", "7d": "60,000 credits / 周"}` 分别显示。

## 新增 / 换掉一个账号

这事没有网页表单可用，以后也不会有——API key 是凭据，不是显示偏好。浏览器里的设置面板（⚙️ 按钮）只碰 `localStorage`，管的是卡片顺序、主题这类东西，从来看不到、也不会发送或存储任何 key。加账号或换 key 是服务器端的、纯命令行操作：

1. **key 单独存成一个文件，绝不写进 `config.json`。** 直接当命令行参数敲会留在 shell 历史里，用 heredoc 或重定向：
   ```bash
   cd ~/.local/share/llm-quota-watchdog   # 换成你实际部署的目录
   cat > .glm-key-newaccount <<'EOF'
   <把 key 粘贴到这里>
   EOF
   chmod 600 .glm-key-newaccount
   ```
2. **在 `config.json` 的 `accounts` 里加一项：**
   ```json
   {"label": "GLM Coding (小套餐)", "sub": "可选副标题", "api_key_file": ".glm-key-newaccount"}
   ```
   Claude/Codex 这种走 CLIProxyAPI 的账号，用 `"auth_file": "some-account.json"`（`cliproxyapi_auth_dir` 里的文件名）代替 `api_key_file`——不需要单独的 key 文件，OAuth token 已经在 CLIProxyAPI 手里。
3. **手动跑一次 `page`**，等下一次 cron 之前先确认新卡片渲染正常：
   ```bash
   llm-quota-watchdog page --config config.json --account "GLM Coding (小套餐)"
   ```
4. **换 key**：同样两步——原地覆盖 key 文件（第 1 步）再跑一次 `page`；文件名没变的话 `config.json` 不用动。
5. **删账号**：删掉 `accounts` 里对应项和它的 key 文件，同时检查 `relaxed_accounts` / `plan_expiry` / `monthly_snapshot` 里有没有引用它的旧 label（这几处都是按 label 字符串匹配的，改名后要一起改）。

## Kimi 月度配额 token

Kimi 的 coding-plan API key 只能拿到 5h/7d 滚动窗口——网页控制台（`kimi.com/membership/subscription?tab=quota`）上那个月度总量，走的是一个内部 RPC，鉴权用另一套网页登录 token，不是 API key。

1. 浏览器登录 kimi.com，打开开发者工具 → Network，刷新配额页面，找到 `GetSubscription` 这个请求，复制它的 `Authorization: Bearer <token>` 请求头里的 token（只要 token 本身，不带 `Bearer ` 前缀）。
2. 单独存成一个文件，规则跟 API key 一样（单独文件、`chmod 600`、绝不写进 `config.json`）：
   ```bash
   cat > .kimi-web-token <<'EOF'
   <把 token 粘贴到这里>
   EOF
   chmod 600 .kimi-web-token
   ```
3. 在 `config.json` 里给 Kimi 账号加 `monthly_web_token_file`：
   ```json
   {"type": "kimi", "label": "Kimi Coding", "api_key_file": ".kimi-key", "monthly_web_token_file": ".kimi-web-token"}
   ```
4. 手动跑一次 `watchdog` 确认拿到了真实百分比（看日志，或者页面上这个账号的 `monthly_snapshot` 那行会显示"自动更新于 <日期>"而不是"手动更新于"）。

这个 token 是普通的浏览器会话 token，会过期（实测能撑好几个月）。过期后每次 `watchdog` 都会刷新失败，你会收到一次性的**【Token失效】**告警，提示重复第 1 步。在你更新之前，页面会保留上次成功抓到的数字，不会静默清零或把卡片弄坏——也不影响 5h/7d 窗口（那部分走 API key，照常刷新）。

## 部分刷新

`page` 默认重新拉取所有账号。加上 `--account`（可重复）则只重新拉取指定账号，其余直接用 `page_state_file` 里的缓存渲染：

```bash
llm-quota-watchdog page --account "Kimi Coding" --account "GLM Coding"
```

顺带还有两个好处：某个账号拉取失败时，卡片保留上次的数字并标红，而不是变成空白；每张卡片会显示自己的"更新于 HH:MM"。

## 可选：按需刷新

页面上的刷新按钮只是指向 `/refresh` / `/refresh?account=<label>` 的链接，具体接什么服务端自己定。给个最小示例：一个只监听本地的小 HTTP 服务，收到请求就重新跑一遍 `page`（带 `--account` 时只刷那个账号），跑完 302 跳回首页，套一层跟主站相同的 basic auth 反代出去，不裸露公网：

```python
# refresh_server.py —— 只监听 127.0.0.1
import http.server, socketserver, subprocess, urllib.parse

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        account = (qs.get("account") or [None])[0]
        cmd = ["llm-quota-watchdog", "page"]
        if account:
            cmd += ["--account", account]   # 只重新拉这个账号，其余用缓存
        subprocess.run(cmd, timeout=60)
        self.send_response(302); self.send_header("Location", "/"); self.end_headers()

with socketserver.ThreadingTCPServer(("127.0.0.1", 8791), Handler) as httpd:
    httpd.serve_forever()
```

nginx 反代：

```nginx
location = /refresh {
    auth_basic "quota";
    auth_basic_user_file /etc/nginx/.htpasswd-quota;
    proxy_pass http://127.0.0.1:8791/refresh;
}
```

如果担心页面被连续点击触发重复请求，自己加个去抖（比如上次运行 <20 秒内直接跳过）。

## 常见问题

**轮询这些接口会增加封号风险吗？**
这些接口就是官方 CLI 自己调的，每小时一次、来自你正常流量的同一个 IP，和官方客户端行为一致。不过要说明：通过任何第三方代理使用订阅 OAuth token 本身就超出厂商设计用途——那是你现有用法固有的风险，这个工具不会明显增加它。

**为什么我的 Codex 账号只有周窗口？**
Plus/ProLite 套餐的 `wham/usage` 把周限额放在 `primary_window` 返回。工具按实际重置时长标注窗口，你看到的就是你套餐真实的样子。

**推送收不到？**
先在服务器上 `curl` 你的 Bark/ntfy 地址确认可达。Bark 重装 App 后 key 会变，记得更新 `bark_url`。

**点了刷新按钮没反应，数字就是不变？**
`curl -sI` 查一下首页的响应头有没有 `Cache-Control`。如果只有 `Last-Modified`/`ETag`，浏览器会当成可以启发式缓存——刷新按钮的 `fetch()` 跟随重定向时可能直接命中浏览器本地缓存，而不是真的打到服务器，哪怕页面其实已经重新生成好了。给托管页面的 `location` 块加上 `add_header Cache-Control "no-cache, must-revalidate" always;`（见前面的 nginx 示例）就会强制每次都重新校验。

## 安全说明

- 所有凭据不出本机：OAuth 文件原地读取，Kimi key 存在 `chmod 600` 的文件里。
- `config.json` 可能含密钥——已被 gitignore，不要提交。
- 外发请求只有各家官方用量接口 + 你的推送通道。

## 路线图 / 贡献

欢迎 PR：Server酱/Telegram 推送通道、更多提供商（Gemini CLI、Qwen Code、Antigravity）、Docker 镜像、页面多语言。

##  License

[MIT](LICENSE)
