# llm-quota-watchdog

**One dashboard + smart push alerts for all your LLM coding-plan quotas.**
Claude Pro/Max · Codex Plus/Pro · Kimi for Coding

[中文文档](README_CN.md)

Python 3.8+, **stdlib only, zero dependencies**. Static HTML output, no database, no daemon.

---

## Why

Subscription coding plans (Claude Pro/Max, ChatGPT Codex, Kimi for Coding) all have **5-hour and weekly quota windows**, but:

- none of them has a "remaining quota" page you can check in one place,
- relay panels (new-api, CPA-Manager-Plus, …) only show relay-side traffic, not the upstream subscription windows,
- Kimi for Coding has no documented quota API at all.

llm-quota-watchdog polls the same endpoints the official CLIs use, renders a single static page, and pushes alerts before you burn through a window — or when you're wasting one.

## What you get

### Dashboard (static HTML, regenerate on cron)

Per account: 5-hour / weekly progress bars with exact percentages and reset countdowns, daily burn vs. fair-share budget, plan expiry countdown. Dark, mobile-friendly.

<!-- add docs/screenshot.png -->

### Push alerts (Bark / ntfy)

| Alert | Trigger |
|---|---|
| 🔴 Nearly used up | 5h window ≥ 80%, weekly ≥ 90% |
| 🟠 Burning too fast | usage 15 points ahead of time pace in the weekly window |
| 🟠 Daily overspend | today's burn ≥ 1.5× daily fair share (weekly ÷ 7) |
| 🟡 Use it or lose it | half the window gone but usage 30+ points behind · or ≤26h to reset with ≤60% used |
| 🟢 Refilled | window reset detected (usage dropped 30+ points) |
| 📅 Plan expiring | 7 / 3 / 1 days before a manually-configured plan expiry date |
| 📊 Daily summary | full report once a day, pushed regardless |

Every alert fires **once per window per cycle** (state-file dedup) — no spam.
Accounts on a big plan you don't want to be nagged about can be listed in `relaxed_accounts` (they keep only the "nearly used up" alert).

## Data sources

The tool calls the exact endpoints the official CLIs use:

| Provider | Endpoint | Credentials |
|---|---|---|
| Claude | `api.anthropic.com/api/oauth/usage` | OAuth token from CLIProxyAPI auth file (auto-discovered) |
| Codex | `chatgpt.com/backend-api/wham/usage` | OAuth token + account id from CLIProxyAPI auth file (auto-discovered) |
| Kimi for Coding | `api.kimi.com/coding/v1/usages` | your coding-plan API key (`sk-...`, from the kimi.com console) |

If you run [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), just point `cliproxyapi_auth_dir` at its auth dir — every Claude/Codex account is picked up automatically, no extra config.

> Kimi's **monthly overall quota** (the one in the web console, mixing Kimi chat + Code) is not exposed by any API. The dashboard supports a manually-updated snapshot for it (`monthly_snapshot`).

## Quickstart

```bash
curl -fsSL https://raw.githubusercontent.com/toolazytoname/llm-quota-watchdog/main/install.sh | bash
```

The installer downloads the script, creates `~/.local/share/llm-quota-watchdog/config.json`, asks for your Bark URL / Kimi key / auth dir, and installs cron jobs:

```
47 * * * *  watchdog            # hourly silent check, pushes only when a rule fires
 5 9 * * *  watchdog --summary  # daily full report at 09:05 server local time
12 * * * *  page                # hourly static page regeneration
```

Test it:

```bash
llm-quota-watchdog watchdog --summary --config ~/.local/share/llm-quota-watchdog/config.json
llm-quota-watchdog page --config ~/.local/share/llm-quota-watchdog/config.json
```

Serve the page with any web server, e.g. nginx:

```nginx
location /quota/ {
    auth_basic "quota";
    auth_basic_user_file /etc/nginx/.htpasswd-quota;
    alias /home/YOU/.local/share/llm-quota-watchdog/www/;
}
```

> ⚠️ The dashboard reveals your subscription usage — put it behind auth or on a private network.

## Manual install

```bash
mkdir -p ~/.local/share/llm-quota-watchdog
cp quota_watchdog.py ~/.local/share/llm-quota-watchdog/
cp config.example.json ~/.local/share/llm-quota-watchdog/config.json
$EDITOR ~/.local/share/llm-quota-watchdog/config.json
```

## Configuration

See [config.example.json](config.example.json) — every key has a sane default. Highlights:

| Key | Meaning |
|---|---|
| `bark_url` / `ntfy_url` | push channel(s); either, both, or none |
| `cliproxyapi_auth_dir` | directory with CLIProxyAPI OAuth `*.json` files; Claude/Codex auto-discovered |
| `accounts` | manual accounts, e.g. Kimi (`api_key` or `api_key_file`), or explicitly-labelled Claude/Codex |
| `relaxed_accounts` | labels that only get the "nearly used up" alert |
| `plan_expiry` | `{"Kimi Coding": "2026-08-22"}` → countdown on page + expiry alerts |
| `monthly_snapshot` | manually-maintained monthly quota shown on the page |
| `thresholds` | every alert threshold is tunable |
| `timezone_offset_hours` | display/report timezone (default UTC+8) |

## FAQ

**Does polling these endpoints risk my account?**
The endpoints are the ones the official CLIs call. Once an hour from the same IP that already serves your traffic is well within normal client behavior. That said, using subscription OAuth tokens through any third-party proxy is outside the vendors' intended use — that's a pre-existing risk of your setup, not something this tool meaningfully adds to.

**Why does my Codex account only show a weekly window?**
For Plus/ProLite plans, `wham/usage` returns the weekly limit as `primary_window`. The tool labels windows by their actual reset distance, so what you see is what your plan actually has.

**Kimi weekly shows 100 requests — is that requests or %?**
The API returns `used`/`limit` counts; the tool converts to percentages.

**My push notifications never arrive.**
Make sure your Bark/ntfy URL is reachable from the host (`curl` it). Bark keys rotate if you reinstall the app — update `bark_url` after reinstalling.

## Security notes

- All credentials stay on your machine: OAuth files are read in place; the Kimi key lives in a `chmod 600` file.
- `config.json` may contain secrets — it is gitignored; never commit it.
- The tool makes outbound calls only to the three official endpoints plus your push channel.

## Roadmap / contributing

PRs welcome: Server酱/Telegram push channels, more providers (Gemini CLI, Qwen Code, Antigravity), Docker image, i18n page.

## License

[MIT](LICENSE)
