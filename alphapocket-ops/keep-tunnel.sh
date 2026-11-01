#!/usr/bin/env bash
# AlphaPocket 公网隧道守护 v3（watchdog + 持久化版）
#
# 教训：v1/v2 的脚本、日志、密钥全放 /tmp 与 ~，沙箱重置全丢。
# v3 全部落 /workspace 持久区：
#   脚本 /workspace/alphapocket-ops/keep-tunnel.sh
#   密钥 /workspace/.ssh/id_ed25519_lhr
#   日志 /workspace/alphapocket-logs/tunnel.log
#
# 用法：keep-tunnel.sh [anon|account]
#   anon     匿名隧道 nokey@localhost.run（免密钥，重连必换子域名）
#   account  账号隧道 dcab3d61-b069-4d8f-8cae-73b0c4a1bbc5@localhost.run
#            （需公钥已绑定到该账号；registration 存活期间子域名稳定）
#
# watchdog：ssh 后台运行，每 20s 探测公网 healthz，连续 3 次失败杀 ssh 重连
# （localhost.run 服务端 registration 可能丢失而 TCP 层仍活，客户端无法自感）。
MODE="${1:-anon}"
OPS=/workspace/alphapocket-ops
LOG=/workspace/alphapocket-logs/tunnel.log
KEY=/workspace/.ssh/id_ed25519_lhr
PROXY=http://127.0.0.1:18080
FAIL_LIMIT=3
PROBE_INTERVAL=20
mkdir -p /workspace/alphapocket-logs

case "$MODE" in
  account) SSH_TARGET="dcab3d61-b069-4d8f-8cae-73b0c4a1bbc5@localhost.run"; SSH_KEY=(-i "$KEY") ;;
  anon)    SSH_TARGET="nokey@localhost.run"; SSH_KEY=() ;;
  *) echo "usage: $0 [anon|account]" >&2; exit 1 ;;
esac
echo "[keep-tunnel] mode=$MODE start $(date '+%F %T')" >> "$LOG"

while true; do
  # 前置：等本机 BFF 就绪，避免隧道建好后 watchdog 误判僵尸
  until curl -sf -m 3 http://localhost:8081/healthz >/dev/null 2>&1; do
    echo "[keep-tunnel] waiting for local bff :8081 …" >> "$LOG"
    sleep 10
  done

  start_line=$(wc -l < "$LOG" 2>/dev/null || echo 0)
  ssh -o ProxyCommand='corkscrew 127.0.0.1 18080 %h %p' \
      -o StrictHostKeyChecking=no -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=4 \
      "${SSH_KEY[@]}" \
      -R 80:localhost:8081 \
      "$SSH_TARGET" >> "$LOG" 2>&1 &
  SSH_PID=$!

  # 等待子域名出现在日志新增部分（最多 90s）
  url=""
  for _ in $(seq 1 45); do
    url=$(tail -n +"$((start_line + 1))" "$LOG" | grep -oE 'https://[a-z0-9]+\.lhr\.life' | tail -1)
    [ -n "$url" ] && break
    sleep 2
  done
  if [ -z "$url" ]; then
    echo "[keep-tunnel] no url within 90s, kill ssh pid=$SSH_PID and retry" >> "$LOG"
    kill "$SSH_PID" 2>/dev/null
    wait "$SSH_PID" 2>/dev/null
    sleep 5
    continue
  fi
  echo "[keep-tunnel] tunnel up: $url (pid $SSH_PID, $(date '+%F %T'))" >> "$LOG"

  # watchdog
  fails=0
  while kill -0 "$SSH_PID" 2>/dev/null; do
    code=$(curl -x "$PROXY" -s -o /dev/null -w '%{http_code}' -m 15 "$url/healthz")
    if [ "$code" = "200" ]; then
      fails=0
    else
      fails=$((fails + 1))
      echo "[keep-tunnel] healthz=$code fails=$fails/$FAIL_LIMIT ($url)" >> "$LOG"
      if [ "$fails" -ge "$FAIL_LIMIT" ]; then
        echo "[keep-tunnel] watchdog: zombie tunnel, kill ssh pid=$SSH_PID" >> "$LOG"
        kill "$SSH_PID" 2>/dev/null
        break
      fi
    fi
    sleep "$PROBE_INTERVAL"
  done

  wait "$SSH_PID" 2>/dev/null
  echo "[keep-tunnel] ssh exited, retrying in 5s" >> "$LOG"
  sleep 5
done
