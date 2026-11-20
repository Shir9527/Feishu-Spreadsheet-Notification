#!/bin/bash
# 表盯盯 · 一键启动（缺什么自己装什么；装完把配置页放到后台，窗口可以关）
cd "$(dirname "$0")" || exit 1

say() { printf "\n\033[1m%s\033[0m\n" "$1"; }
ok()  { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn(){ printf "  \033[33m!\033[0m %s\n" "$1"; }
bad() { printf "  \033[31m✗\033[0m %s\n" "$1"; }
pause(){ read -r -p "按回车退出..." _; }

LOG="$HOME/.tablewatch/ui.log"
mkdir -p "$HOME/.tablewatch"

say "表盯盯 · 环境自检"

PY="$(command -v python3)"
if [ -z "$PY" ]; then
  bad "没找到 python3"
  echo "  macOS 装一下：https://www.python.org/downloads/"
  pause; exit 1
fi
ok "python3：$("$PY" -V 2>&1)"
[ -f app.py ] || { bad "同目录没有 app.py"; pause; exit 1; }

if ! command -v npm >/dev/null 2>&1; then
  warn "没找到 npm（Node.js），飞书 CLI 需要它"
  if command -v brew >/dev/null 2>&1; then
    say "用 Homebrew 自动安装 Node.js（可能要输开机密码）"
    brew install node || true
  else
    echo "  即将打开 Node.js 下载页，装 LTS 版后重新双击本文件"
    open "https://nodejs.org/zh-cn/download" 2>/dev/null || true
    pause; exit 1
  fi
fi
command -v npm >/dev/null 2>&1 && ok "npm：$(npm -v 2>/dev/null)"

if ! command -v lark-cli >/dev/null 2>&1; then
  say "安装飞书 CLI（约 1 分钟）"
  if ! npm install -g @larksuite/cli; then
    warn "普通权限装不上，改用管理员权限（会要开机密码）"
    if ! sudo npm install -g @larksuite/cli; then bad "安装失败"; pause; exit 1; fi
  fi
fi
if command -v lark-cli >/dev/null 2>&1; then
  ok "lark-cli：$(lark-cli --version 2>/dev/null | head -1)"
else
  bad "装完仍找不到 lark-cli，重开终端再试"; pause; exit 1
fi

logged_in() { lark-cli contact +get-user --as user >/dev/null 2>&1; }

if ! logged_in; then
  say "第 1 步：配置应用凭证（会打开浏览器）"
  lark-cli config init --new || warn "这一步没成功，可稍后重试"
  say "第 2 步：用你的飞书账号授权"
  lark-cli auth login --recommend || warn "授权没完成"
  if ! logged_in; then bad "还没登录成功，再双击一次把第 2 步走完"; pause; exit 1; fi
fi
ok "已登录你的飞书账号（只用于读表）"

say "启动配置页（放后台跑，这个窗口可以关）"
nohup "$PY" app.py >> "$LOG" 2>&1 &
disown 2>/dev/null || true
sleep 2

URL="http://127.0.0.1:8799"
if curl -s --max-time 3 "$URL/api/health" >/dev/null 2>&1; then
  ok "配置页已就绪：$URL"
  open "$URL" 2>/dev/null || true
  echo
  echo "  这个窗口现在可以关，后台检查不受影响。"
  echo "  以后改配置，重新双击本文件（不会重复启动）。"
  echo "  日志：$LOG"
  sleep 2
  exit 0
else
  bad "配置页没起来，看日志：$LOG"
  tail -5 "$LOG" 2>/dev/null
  pause; exit 1
fi
