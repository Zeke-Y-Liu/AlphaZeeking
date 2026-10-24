"""真实后端 E2E（回归）：登录 → 数据 → 行情（含 M2-3 部分返回）→ 评估 → 回测 → 调仓 → 执行单 → runs。

用法：core/.venv/bin/python scripts/e2e_real.py
前置：三件套已启动且数据全绿（六项验证全 pass，否则回测会被 VERIFY_GATE 拦截）。
"""
import sys
import time

import httpx

BASE = "http://localhost:8081"
results: list[tuple[str, str, str]] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


c = httpx.Client(base_url=BASE, timeout=60)

r = c.post("/api/auth/login", json={"username": "demo", "password": "alphapocket"})
step("登录", r.status_code == 200, f"{r.status_code}")
c.headers["Authorization"] = f"Bearer {r.json()['access']}"

r = c.get("/api/v1/data/status")
d = r.json()
has = sum(1 for s in d["securities"] if s["has_bars"])
step("数据状态", r.status_code == 200 and has == len(d["securities"]) and d["as_of"],
     f"as_of={d['as_of']} bars={has}/{len(d['securities'])}")

items = c.get("/api/v1/data/verifications?limit=6").json()["items"]
step("六项验证", len(items) == 6 and all(i.get("status") == "pass" for i in items),
     " ".join(f"{i.get('check')}={i.get('status')}" for i in items))

r = c.get("/api/v1/market/quotes", params={"secids": "600519.SH,00700.HK,AAPL.US"})
qmap = {q["secid"]: q for q in r.json().get("quotes", [])}
ok = all(s in qmap and qmap[s].get("price") for s in ("600519.SH", "00700.HK", "AAPL.US"))
step("实时行情", r.status_code == 200 and ok,
     " ".join(f"{s}={qmap.get(s, {}).get('price')}({qmap.get(s, {}).get('currency')})" for s in qmap))

r = c.get("/api/v1/market/quotes", params={"secids": "600519.SH,FOO.XX"})
b = r.json()
m2 = r.status_code == 200 and [q["secid"] for q in b["quotes"]] == ["600519.SH"] \
     and [e["secid"] for e in b.get("errors", [])] == ["FOO.XX"]
step("行情容错（M2-3 部分返回）", m2,
     f"quotes={[q['secid'] for q in b.get('quotes', [])]} errors={[e['secid'] for e in b.get('errors', [])]}")

r = c.get("/api/v1/portfolios")
pfs = r.json()
pfs = pfs.get("items", []) if isinstance(pfs, dict) else pfs
pids = [p["pid"] for p in pfs]
step("组合列表", r.status_code == 200 and len(pids) >= 2, f"{pids}")
PID = "cn4" if "cn4" in pids else pids[0]

r = c.post(f"/api/v1/portfolios/{PID}/review")
job = r.json().get("job_id") if r.status_code == 202 else None
step("评估提交", job is not None, f"{r.status_code} job={job}")
review_done, run_uid = False, None
for _ in range(120):
    time.sleep(2)
    jr = c.get(f"/api/v1/jobs/{job}").json()
    if jr.get("status") in ("done", "failed"):
        review_done = jr["status"] == "done"
        run_uid = (jr.get("result") or {}).get("run_uid")
        if not review_done:
            print("  review error:", str(jr.get("error"))[:300])
        break
step("评估完成", review_done, f"run={run_uid}")

bt_job = None
for _ in range(30):
    r = c.post(f"/api/v1/portfolios/{PID}/backtest", json={"start": "2024-01-01"})
    if r.status_code == 202:
        bt_job = r.json().get("job_id")
        break
    time.sleep(5)
step("回测提交", bt_job is not None, f"{r.status_code} job={bt_job}")
bt_done, bt_s = False, {}
if bt_job:
    for _ in range(180):
        time.sleep(2)
        jr = c.get(f"/api/v1/jobs/{bt_job}").json()
        if jr.get("status") in ("done", "failed"):
            bt_done = jr["status"] == "done"
            bt_s = jr.get("result") or {}
            if not bt_done:
                print("  backtest error:", str(jr.get("error"))[:300])
            break
bt_sum = bt_s.get("summary") or bt_s
step("回测完成", bt_done, f"n_days={bt_sum.get('n_days')} rebalances={bt_sum.get('n_rebalances')}")

plan_done, pj = False, None
for _ in range(30):
    r = c.post(f"/api/v1/portfolios/{PID}/rebalance-plan")
    if r.status_code == 202:
        pj = r.json().get("job_id")
        break
    time.sleep(5)
if pj:
    for _ in range(120):
        time.sleep(2)
        jr = c.get(f"/api/v1/jobs/{pj}").json()
        if jr.get("status") in ("done", "failed"):
            plan_done = jr["status"] == "done"
            break
step("调仓计划完成", plan_done, f"job={pj}")

if plan_done:
    plans = c.get(f"/api/v1/portfolios/{PID}/plans").json()
    plans = plans.get("items", []) if isinstance(plans, dict) else plans
    pid_num = plans[0]["id"] if plans and isinstance(plans[0].get("id"), int) else None
    r = c.post(f"/api/v1/plans/{pid_num}/exec-orders", json={"capital_cny": 100000})
    eo = r.json() or {}
    exec_id, exec_uid = eo.get("id"), eo.get("exec_uid")
    orders = (eo.get("sheet") or {}).get("orders", [])
    step("执行单生成", r.status_code in (200, 201) and exec_id,
         f"exec={exec_uid} n_orders={len(orders)}")
    r = c.get(f"/api/v1/exec-orders/{exec_id}")
    step("执行单详情", r.status_code == 200 and r.json().get("status") == "draft",
         f"status={r.json().get('status')}")
    r = c.post(f"/api/v1/exec-orders/{exec_id}/confirm", json={"ack_discipline": True})
    step("执行单确认", r.status_code in (200, 201), f"{r.status_code}")
    fills = [{"secid": o["secid"], "price": o.get("ref_price") or o.get("price") or 1.0,
              "shares": o.get("order_shares") or o.get("shares") or 0}
             for o in orders if o.get("secid") and (o.get("order_shares") or o.get("shares"))]
    r = c.post(f"/api/v1/exec-orders/{exec_id}/fill", json={"fills": fills})
    final = c.get(f"/api/v1/exec-orders/{exec_id}").json()
    step("成交登记与终态", final.get("status") == "executed",
         f"status={final.get('status')} n_fills={len(fills)}")
else:
    step("执行单生成", False, "调仓未完成")
    step("执行单详情", False, "-")
    step("执行单确认", False, "-")
    step("成交登记与终态", False, "-")

runs = c.get("/api/v1/runs").json()
runs = runs.get("items", []) if isinstance(runs, dict) else runs
types = {x.get("type") for x in runs}
step("runs 全景", {"review", "backtest", "rebalance_plan", "exec_orders", "data_sync"} <= types,
     f"n={len(runs)} types={sorted(types)}")

print("\n===== E2E 汇总 =====")
npass = sum(1 for _, s, _ in results if s == "PASS")
for name, s, detail in results:
    print(f"{s}  {name}  {detail}")
print(f"\n{npass}/{len(results)} PASS")
sys.exit(0 if npass == len(results) else 1)
