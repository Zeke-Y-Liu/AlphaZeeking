#!/usr/bin/env python3
"""R2 实时上游数据可靠性检查（RG-4，--with-live）：同步 → 等待 → 复跑 R1。

链路：登录 core → ``POST /data/sync {mode: incremental}``（409=已有同步在跑，跟踪其
job_id）→ 轮询至 done/failed → 复跑 ``checks/data_r1.py`` 六项验证。任一环节失败退出 1。
用 urllib（stdlib），避免对 core venv 的依赖。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8082"
R1 = "/workspace/alphapocket-web/deploy/regression/checks/data_r1.py"
POLL_SECONDS = 1500
STEP = 5


def _req(path: str, payload: dict | None = None, token: str | None = None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST" if payload else "GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main() -> int:
    token = _req("/api/v1/auth/login",
                 {"username": "demo", "password": "alphapocket"})["access"]
    print("登录 OK")

    r = _req("/api/v1/data/sync", {"mode": "incremental"}, token)
    job_id = r.get("job_id")
    if not job_id:                      # 409：已有同步在跑 → 跟踪现役 job
        st = _req("/api/v1/data/status", token=token)["latest_sync"]
        job_id = st and st["job_id"]
        if not job_id:
            print("FAIL：无法获取同步 job（409 且无进行中任务）")
            return 1
        print(f"已有同步在跑，跟踪现役 job：{job_id}")

    waited = 0
    while waited < POLL_SECONDS:
        job = _req(f"/api/v1/jobs/{job_id}", token=token)
        if job["status"] in ("done", "failed"):
            break
        time.sleep(STEP)
        waited += STEP
    if job["status"] != "done":
        print(f"FAIL：同步未完成 status={job['status']} progress={job.get('progress')}"
              f" err={(job.get('error') or '')[:200]}")
        return 1
    print(f"同步完成：{job_id}（{waited}s）")

    return subprocess.run([sys.executable, R1], check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
