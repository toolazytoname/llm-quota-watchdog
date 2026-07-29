# llm-quota-watchdog

**一个页面 + 智能推送，看住你所有大模型编程套餐的额度。**
支持 Claude Pro/Max · Codex Plus/Pro · Kimi for Coding

[English](README.md)

Python 3.8+，**只用标准库，零依赖**。输出静态 HTML，无数据库、无守护进程。

---

## 为什么做这个

订阅制编程套餐（Claude Pro/Max、ChatGPT Codex、Kimi for Coding）都有 **5 小时和每周两套额度窗口**，但是：

- 没有任何一个地方能统一查看各家的剩余额度；
- 中转站面板（new-api、CPA-Manager-Plus 等）只能看到中转站侧的流量，看不到上游订阅的真实窗口；
- Kimi for Coding 根本没有文档化的额度 API（本项目的接口是从 kimi-cli 源码里挖出来的）。

llm-quota-watchdog 调用官方 CLI 自己使用的接口，生成一个静态页面，并在你烧穿窗口之前——或者浪费窗口之前——推送提醒。

## 功能

### 额度仪表盘（静态 HTML，cron 定时生成）

每个账号：5 小时 / 每周进度条、精确百分比、重置倒计时、用量 vs 时间节奏（偏快/偏慢）、套餐到期倒计时。深色主题，手机友好。

![dashboard](docs/screenshot.png)

### 推送告警（Bark / ntfy）

| 告警 | 触发条件 |
|---|---|
| 🔴 快用完 | 5h 窗口 ≥80%，周窗口 ≥90% |
| 🟠 用太快 | 周窗口用量超过时间进度 15 个点 |
| ⏱ 节奏指示 | 每个窗口都显示时间进度 vs 用量，偏快偏慢一眼可见 |
| 🟡 赶紧用 | 时间过半但用量落后 30 点 · 或 ≤26 小时重置且用量 ≤60% |
| 🟢 满血复活 | 检测到窗口重置（用量掉 30 点以上） |
| 📅 套餐到期 | 手动配置的到期日前 7/3/1 天 |
| 📊 每日汇总 | 每天一条完整报告，无论有无异常 |

每条告警**一个周期只推一次**（状态文件去重），不刷屏。
大套餐不想被念叨的账号可以放进 `relaxed_accounts`，只保留"快用完"。

## 数据来源

调用的是官方 CLI 自己用的接口：

| 提供商 | 接口 | 凭据 |
|---|---|---|
| Claude | `api.anthropic.com/api/oauth/usage` | CLIProxyAPI 认证文件里的 OAuth token（自动发现） |
| Codex | `chatgpt.com/backend-api/wham/usage` | CLIProxyAPI 认证文件里的 OAuth token + account id（自动发现） |
| Kimi for Coding | `api.kimi.com/coding/v1/usages` | 你的 coding 套餐 API key（kimi.com 控制台生成） |

如果你在用 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)，把 `cliproxyapi_auth_dir` 指向它的认证目录即可，所有 Claude/Codex 账号自动接入，零配置。

> Kimi 的**月度总配额**（网页控制台里混算 Kimi 聊天 + Code 的那个）没有任何 API 能拿，页面支持手动快照（`monthly_snapshot`）。

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
}
```

> ⚠️ 页面会暴露你的订阅用量，务必加密码或只放内网。

## 配置说明

见 [config.example.json](config.example.json)，所有 key 都有合理默认值。重点：

| 配置 | 说明 |
|---|---|
| `bark_url` / `ntfy_url` | 推送通道，可配任一或都配 |
| `cliproxyapi_auth_dir` | CLIProxyAPI 的 OAuth `*.json` 目录，Claude/Codex 自动发现 |
| `accounts` | 手动账号，如 Kimi（`api_key` 或 `api_key_file`），或自定义标签的 Claude/Codex |
| `relaxed_accounts` | 只保留"快用完"告警的账号标签 |
| `plan_expiry` | `{"Kimi Coding": "2026-08-22"}` → 页面倒计时 + 到期提醒 |
| `monthly_snapshot` | 手动维护的月度配额快照，显示在页面上 |
| `thresholds` | 所有告警阈值都可调 |
| `timezone_offset_hours` | 显示/报告时区（默认 UTC+8） |

## 常见问题

**轮询这些接口会增加封号风险吗？**
这些接口就是官方 CLI 自己调的，每小时一次、来自你正常流量的同一个 IP，和官方客户端行为一致。不过要说明：通过任何第三方代理使用订阅 OAuth token 本身就超出厂商设计用途——那是你现有用法固有的风险，这个工具不会明显增加它。

**为什么我的 Codex 账号只有周窗口？**
Plus/ProLite 套餐的 `wham/usage` 把周限额放在 `primary_window` 返回。工具按实际重置时长标注窗口，你看到的就是你套餐真实的样子。

**推送收不到？**
先在服务器上 `curl` 你的 Bark/ntfy 地址确认可达。Bark 重装 App 后 key 会变，记得更新 `bark_url`。

## 安全说明

- 所有凭据不出本机：OAuth 文件原地读取，Kimi key 存在 `chmod 600` 的文件里。
- `config.json` 可能含密钥——已被 gitignore，不要提交。
- 外发请求只有三个官方接口 + 你的推送通道。

## 路线图 / 贡献

欢迎 PR：Server酱/Telegram 推送通道、更多提供商（Gemini CLI、Qwen Code、Antigravity）、Docker 镜像、页面多语言。

##  License

[MIT](LICENSE)
