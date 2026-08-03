#!/usr/bin/env bash
# llm-quota-watchdog installer
# Usage: curl -fsSL https://raw.githubusercontent.com/toolazytoname/llm-quota-watchdog/main/install.sh | bash
set -euo pipefail

INSTALL_DIR="$HOME/.local/share/llm-quota-watchdog"
BIN_DIR="$HOME/.local/bin"
REPO_RAW="https://raw.githubusercontent.com/toolazytoname/llm-quota-watchdog/main"

echo "==> llm-quota-watchdog installer"

command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || { echo "ERROR: python >= 3.8 required"; exit 1; }

mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$HOME/.config/llm-quota-watchdog"

echo "==> downloading quota_watchdog.py"
curl -fsSL "$REPO_RAW/quota_watchdog.py" -o "$INSTALL_DIR/quota_watchdog.py"
chmod +x "$INSTALL_DIR/quota_watchdog.py"
ln -sf "$INSTALL_DIR/quota_watchdog.py" "$BIN_DIR/llm-quota-watchdog"

if [ ! -f "$INSTALL_DIR/config.json" ]; then
  echo "==> creating config from example"
  curl -fsSL "$REPO_RAW/config.example.json" -o "$INSTALL_DIR/config.json"
fi

# --- interactive setup (skip when piped without tty) ---
if [ -t 0 ]; then
  echo
  read -rp "Bark push URL (e.g. https://api.day.app/YOUR_KEY/, empty to skip): " BARK
  [ -n "$BARK" ] && python3 - "$INSTALL_DIR/config.json" "$BARK" <<'EOF'
import json, sys
p, v = sys.argv[1], sys.argv[2]
c = json.load(open(p)); c["bark_url"] = v; json.dump(c, open(p, "w"), indent=2, ensure_ascii=False)
EOF

  read -rp "Kimi for Coding API key (sk-..., empty to skip): " KIMI
  if [ -n "$KIMI" ]; then
    echo "$KIMI" > "$HOME/.config/llm-quota-watchdog/kimi-key"
    chmod 600 "$HOME/.config/llm-quota-watchdog/kimi-key"
  fi

  read -rp "GLM Coding Plan API key (id.secret, empty to skip): " GLM
  if [ -n "$GLM" ]; then
    echo "$GLM" > "$HOME/.config/llm-quota-watchdog/glm-key"
    chmod 600 "$HOME/.config/llm-quota-watchdog/glm-key"
  fi

  read -rp "CLIProxyAPI auth dir [~/.cli-proxy-api]: " AUTHDIR
  if [ -n "$AUTHDIR" ]; then
    python3 - "$INSTALL_DIR/config.json" "$AUTHDIR" <<'EOF'
import json, sys
p, v = sys.argv[1], sys.argv[2]
c = json.load(open(p)); c["cliproxyapi_auth_dir"] = v; json.dump(c, open(p, "w"), indent=2, ensure_ascii=False)
EOF
  fi

  read -rp "Install cron jobs (hourly alerts + daily 9:05 summary + hourly page)? [Y/n]: " CRON
  if [ "${CRON:-Y}" != "n" ] && [ "${CRON:-Y}" != "N" ]; then
    ( crontab -l 2>/dev/null | grep -v llm-quota-watchdog || true
      echo "47 * * * * $BIN_DIR/llm-quota-watchdog watchdog --config $INSTALL_DIR/config.json"
      echo "5 9 * * * $BIN_DIR/llm-quota-watchdog watchdog --summary --config $INSTALL_DIR/config.json"
      echo "12 * * * * $BIN_DIR/llm-quota-watchdog page --config $INSTALL_DIR/config.json"
    ) | crontab -
    echo "==> cron installed"
  fi
fi

echo
echo "Done! Next steps:"
echo "  1. edit config:  $INSTALL_DIR/config.json"
echo "  2. test alerts:  $BIN_DIR/llm-quota-watchdog watchdog --summary --config $INSTALL_DIR/config.json"
echo "  3. generate page: $BIN_DIR/llm-quota-watchdog page --config $INSTALL_DIR/config.json"
echo "     then serve $INSTALL_DIR/www/ with any web server (nginx, caddy, python3 -m http.server)"
