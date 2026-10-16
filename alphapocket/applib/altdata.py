"""二线数据源接入 — a-stock-data v3.10.1 摘取（《黑箱框架对照与实施计划》P1 补强）

定位：**二线 / 补充源**，**不替换** [datasource.py](file:///workspace/alphapocket/applib/datasource.py)
的 AKShare 主源。只补两处主源缺口：

  1. **申万行业分类变迁史** → ``dim/industry_sw.json``
     主源 ``dim/security.json`` 的 ``industry`` 是**静态标注**（"申万口径常识标注"），
     用今天的行业去套历史回测 = **典型前视偏差**（书中 ④ 号数据不同步）。本模块给出每只
     标的的**行业变迁史**，供 ``factors.industry_neutralize`` 取「当时」行业。
     实测坐实：601058 的申万行业在 2021-07-30 由 220000（化工）变更为 280000（汽车）。

  2. **研报来源** → 供 ``hypothesis.register``（R3）自动取来源
     - 东财研报：评级 / 目标价 / 三年 EPS 预测
     - 同花顺一致预期 EPS：机构一致预期（预测机构数 < 3 需谨慎）

来源与版本
----------
本模块摘取自 **a-stock-data v3.10.1（2026-10-07）** 的 §6.7 / §2.1 / §2.2 内嵌实现
（https://github.com/simonlin1212/a-stock-data ，Apache-2.0，仅覆盖代码、不覆盖数据）。
摘取时按本项目约定改造：显式 provenance、PIT 语义、纯函数与网络分离、可离线单测。
上游为**抓取公开接口**，无 SLA 与版本契约 → 仅作二线，断源时降级回主源并告警。

TLS 说明
--------
``www.swsresearch.com`` 在**中间人代理**环境下证书链校验失败（实测：默认 ``SSLError``，
``verify=False`` 则 HTTP 200）。故下载走「先安全校验、失败再降级」两段式，
并在 provenance 中**明示实际使用口径**（``ssl_verify`` 字段），不静默降级。

纪律
----
- PIT：``sw_industry_as_of`` 只取 ``start_date <= as_of`` 的最后一次调整，无前视；
- 只调用公开 HTTP 接口，不改上游；不写 DuckDB（维度数据入 repo，与 dim/ 同级）；
- 依赖：``requests`` + ``pandas`` + ``xlrd``（.xls 读取）+ ``lxml``（read_html）。
产物：``dim/industry_sw.json``；溯源追加至 ``market/provenance.json``。
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pandas as pd
import requests

PIN_SOURCE = "a-stock-data v3.10.1 (2026-10-07)"
SW_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
SW_REL = Path("dim") / "industry_sw.json"
PROV_REL = Path("market") / "provenance.json"
EM_REPORT_URL = "https://reportapi.eastmoney.com/report/list"
THS_EPS_URL = "https://basic.10jqka.com.cn/new/{code}/worth.html"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
# 申万官方列名 → 本项目字段
SW_COLUMNS = {"股票代码": "code", "计入日期": "start_date",
              "行业代码": "industry_code", "更新日期": "update_date"}


# ── 通用：两段式 HTTP（先安全校验，失败降级）──────────────
def _http_get(url: str, *, params: dict | None = None, headers: dict | None = None,
              timeout: int = 60, allow_insecure_ssl: bool = True) -> tuple[requests.Response, bool]:
    """GET → (响应, 是否降级为跳过证书校验)。

    先按默认证书校验请求；仅当抛 ``SSLError``（中间人代理导致）且允许降级时，
    才改用 ``verify=False`` 重试，并把降级事实回传给调用方登记进 provenance。
    """
    h = {**UA, **(headers or {})}
    try:
        return requests.get(url, params=params, headers=h, timeout=timeout), False
    except requests.exceptions.SSLError:
        if not allow_insecure_ssl:
            raise
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return requests.get(url, params=params, headers=h, timeout=timeout, verify=False), True


def six(code: str) -> str:
    """``601058.SH`` / ``SH601058`` / ``601058`` → ``601058``（纯 6 位）。"""
    s = str(code or "").strip().upper()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) < 6:
        raise ValueError(f"无法归一化为 6 位代码: {code!r}")
    return digits[-6:]


# ── ① 申万行业分类变迁史 ────────────────────────────────
def normalize_sw_table(df: pd.DataFrame) -> list[dict]:
    """申万原始表 → 规范化记录（纯函数，可离线单测）。

    关键处理：``股票代码`` 上游为整数（``1`` 而非 ``"000001"``）→ 必须 zfill(6)，
    否则 ``000001`` 会变成 ``"1"`` 而对不上；层级码按官方规范补 6 位
    （一级 480000 / 二级 480300）。
    """
    df = df.rename(columns=SW_COLUMNS)
    missing = {"code", "start_date", "industry_code"} - set(df.columns)
    if missing:
        raise RuntimeError(f"申万表结构变化，缺列 {sorted(missing)}；实际列={list(df.columns)}")
    out = []
    for r in df.itertuples(index=False):
        code = str(r.code).split(".")[0].strip().zfill(6)
        icode = str(r.industry_code).split(".")[0].strip().zfill(6)
        sd = pd.to_datetime(r.start_date, errors="coerce")
        if pd.isna(sd):
            continue
        out.append({"code": code, "start_date": sd.strftime("%Y-%m-%d"),
                    "industry_code": icode, "l1_code": icode[:2] + "0000",
                    "l2_code": icode[:4] + "00"})
    out.sort(key=lambda x: (x["code"], x["start_date"]))
    return out


def fetch_sw_industry(ctx, *, allow_insecure_ssl: bool = True) -> dict:
    """下载申万行业变迁史 → ``dim/industry_sw.json`` + provenance 登记。

    返回摘要（行数/标的数/行业数/时间跨度/实际 TLS 口径），供 CLI 与 artifact 使用。
    """
    t0 = time.time()
    resp, insecure = _http_get(SW_URL, timeout=120, allow_insecure_ssl=allow_insecure_ssl)
    if resp.status_code != 200 or len(resp.content) < 1000:
        raise RuntimeError(f"申万表下载异常: HTTP {resp.status_code}, {len(resp.content)} bytes")
    records = normalize_sw_table(pd.read_excel(io.BytesIO(resp.content)))
    if not records:
        raise RuntimeError("申万表解析为 0 条记录（上游结构可能已变）")

    payload = {
        "source": PIN_SOURCE,
        "upstream": "申万宏源研究 swsresearch.com（SwClass2021 官方分类）",
        "url": SW_URL,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ssl_verify": not insecure,          # True=默认安全校验；False=已降级（中间人环境）
        "note": "申万官方只发布行业代码、不发布中文名；东财/通达信行业名分类体系不同，不可混用",
        "n_records": len(records),
        "records": records,
    }
    p = ctx.local_repo / SW_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    prov = _register_provenance(ctx, {
        "dataset": "dim_industry_sw",
        "interface": f"swsresearch SwClass2021 XLS（摘取 {PIN_SOURCE} §6.7）",
        "upstream": "申万宏源研究 swsresearch.com",
        "params": {"ssl_verify": not insecure, "note": "行业变迁史，PIT 用；官方仅发布代码"},
        "file": str(SW_REL).replace("\\", "/"),
        "sha256": _sha256_file(p),
        "bytes": p.stat().st_size,
    })
    codes = {r["code"] for r in records}
    return {
        "dataset": "dim_industry_sw", "file": str(p), "n_records": len(records),
        "n_codes": len(codes), "n_l1": len({r["l1_code"] for r in records}),
        "n_l2": len({r["l2_code"] for r in records}),
        "start_date": min(r["start_date"] for r in records),
        "end_date": max(r["start_date"] for r in records),
        "ssl_verify": not insecure, "insecure_ssl_used": insecure,
        "elapsed_s": round(time.time() - t0, 1), "provenance": str(prov),
        "sha256": _sha256_file(p),
    }


def load_sw(ctx) -> list[dict]:
    """读取已落盘的申万行业变迁史记录；未同步 → 空表（不报错）。"""
    p = ctx.local_repo / SW_REL
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("records", [])


def sw_industry_as_of(records: list[dict], code: str, as_of: str, level: str = "l1") -> dict | None:
    """某标的在 ``as_of`` 日所属申万行业（纯函数，PIT）。

    取「不晚于 as_of 的最后一次调整」→ 只用 ``start_date <= as_of`` 的记录，
    绝不使用 ``as_of`` 之后才生效的分类，消除前视偏差。
    无记录（该日尚未上市/无归属）→ ``None``。
    """
    c = six(code)
    key = {"l1": "l1_code", "l2": "l2_code", "l3": "industry_code"}[level]
    hit = None
    for r in records:
        if r["code"] == c and r["start_date"] <= as_of:
            if hit is None or r["start_date"] >= hit["start_date"]:
                hit = r
    if hit is None:
        return None
    return {"code": c, "as_of": as_of, "level": level, "industry": hit[key],
            "industry_code": hit["industry_code"], "l1_code": hit["l1_code"],
            "l2_code": hit["l2_code"], "since": hit["start_date"]}


def industries_as_of(records: list[dict], secids: list[str], as_of: str,
                     level: str = "l1") -> dict[str, str | None]:
    """批量 PIT 行业映射 → 直接喂 ``factors.industry_neutralize(vals, industries)``。

    未收录/该日无归属 → ``None``（中性化时按"单只成组"处理，不误并行业）。
    """
    out: dict[str, str | None] = {}
    for sid in secids:
        r = sw_industry_as_of(records, sid, as_of, level)
        out[sid] = r["industry"] if r else None
    return out


# ── ② 研报来源（供 R3 假设登记通道）────────────────────
def research_reports(code: str, *, max_pages: int = 1,
                     allow_insecure_ssl: bool = True) -> list[dict]:
    """东财研报列表（评级 / 目标价 / 三年 EPS 预测）。

    ``code`` 只认纯 6 位数字（传 ``SH601058`` 会返回 0 条而不是报错）→ 先 ``six()`` 归一化。
    返回 ``[]`` 仅表示东财确无覆盖。
    """
    c = six(code)
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {"industryCode": "*", "pageSize": "100", "industry": "*",
                  "rating": "*", "ratingChange": "*",
                  "beginTime": "2000-01-01", "endTime": "2030-01-01",
                  "pageNo": str(page), "fields": "", "qType": "0",
                  "orgCode": "", "code": c, "rcode": "",
                  "p": str(page), "pageNum": str(page), "pageNumber": str(page)}
        resp, _ = _http_get(EM_REPORT_URL, params=params,
                            headers={"Referer": "https://data.eastmoney.com/"},
                            timeout=30, allow_insecure_ssl=allow_insecure_ssl)
        batch = (resp.json().get("data") or []) if resp.status_code == 200 else []
        if not batch:
            break
        for d in batch:
            info = d.get("infoCode") or ""
            rows.append({
                "date": str(d.get("publishDate") or "")[:10],
                "title": d.get("title") or "",
                "org": d.get("orgSName") or d.get("orgName") or "",
                "author": d.get("researcher") or "",
                "rating": d.get("emRatingName") or "",
                "rating_change": d.get("ratingChange") or "",
                "industry": d.get("indvInduName") or "",
                "eps_this_year": _f(d.get("predictThisYearEps")),
                "eps_next_year": _f(d.get("predictNextYearEps")),
                "eps_next_two_year": _f(d.get("predictNextTwoYearEps")),
                "target_price_low": _f(d.get("indvAimPriceL")),
                "target_price_high": _f(d.get("indvAimPriceT")),
                "info_code": info,
                "pdf_url": f"https://pdf.dfcfw.com/pdf/H3_{info}_1.pdf" if info else None,
            })
    return rows


def eps_consensus(code: str, *, allow_insecure_ssl: bool = True) -> list[dict]:
    """同花顺机构一致预期 EPS（年度 / 机构数 / 最小·均值·最大 / 行业平均）。"""
    c = six(code)
    resp, _ = _http_get(THS_EPS_URL.format(code=c),
                        headers={"Referer": "https://basic.10jqka.com.cn/"},
                        timeout=20, allow_insecure_ssl=allow_insecure_ssl)
    resp.encoding = "gbk"
    try:
        tables = pd.read_html(io.StringIO(resp.text))
    except ValueError:                      # 页面无可解析表格
        return []
    for t in tables:
        cols = [str(c) for c in t.columns]
        if any("均值" in c or "每股收益" in c for c in cols):
            return [{"year": str(r.get("年度")), "n_org": _i(r.get("预测机构数")),
                     "min": _f(r.get("最小值")), "mean": _f(r.get("均值")),
                     "max": _f(r.get("最大值")), "industry_mean": _f(r.get("行业平均数")),
                     "note": "预测机构数 < 3 需谨慎" if (_i(r.get("预测机构数")) or 0) < 3 else None}
                    for _, r in t.iterrows()]
    return []


# ── 内部工具 ───────────────────────────────────────────
def _f(x):
    try:
        v = float(x)
        return None if v != v else round(v, 4)      # NaN 过滤
    except (TypeError, ValueError):
        return None


def _i(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def _sha256_file(p: Path) -> str:
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _register_provenance(ctx, entry: dict) -> Path:
    """把二线源条目 upsert 进 ``market/provenance.json`` 的 sources（同 dataset 覆盖）。"""
    p = ctx.local_repo / PROV_REL
    prov = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"sources": []}
    srcs = [s for s in prov.get("sources", []) if s.get("dataset") != entry["dataset"]]
    srcs.append(entry)
    prov["sources"] = srcs
    prov["alt_source"] = PIN_SOURCE          # 明示含二线源，避免与主源混淆
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(prov, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
