"""真实数据源接入（AKShare 公开接口）→ remote/repo（云端替身）

V4-α 真实化：以 AKShare 免费公开接口为唯一数据源，替换 seed 模拟数据。
- 行情：新浪 stock_zh_a_daily（qfq 前复权 + raw 不复权，双份留存供复权校验）
- 交叉源：腾讯 stock_zh_a_hist_tx（独立行情源，供跨源验证）
- 财务：新浪 stock_financial_analysis_indicator（2019 起逐报告期）
- 分红：新浪 stock_history_dividend_detail（除权除息日 + 每股派息）
- 交易日历：新浪 tool_trade_date_hist_sina
- 基金（ETF/LOF，multi5 组合）：新浪 fund_etf_hist_sina（对 ETF 与 LOF 均适用；无复权接口，
  序列即不复权 raw）；交叉源为腾讯 stock_zh_a_hist_tx（独立源，对 ETF/LOF 同样适用；
  东财 push2his 接口在当前网络出口不可达，弃用）

V4-β 多市场扩展（2026-10-06）：universe 支持 A股 / 港股 / 美股三类 secid（按后缀分类）。
- 港股（``00700.HK``）：日K 新浪 stock_hk_daily（qfq + raw）；交叉源 腾讯 web.ifzq.gtimg.cn
  （hk{code}，不复权 HKD）；港股通名单 雪球港股通筛选（market=HK&type=ggt），失败降级
  ggt_static 静态快照
- 美股（``AAPL.US``）：日K 新浪 stock_us_daily（qfq + raw，无 amount 列）；交叉源 腾讯
  us{TICKER}.OQ/.N/.A（依次尝试纳斯达克/纽交所/美交所）

V4-β 财务因子补充（2026-10-07）：港股/美股接入东财财务与分红接口，补齐引擎 A 的
估值（PE/PB）/质量（ROE·利润率）/成长（营收·净利同比）/红利（股息率）因子。
- 港股财务：东财 stock_financial_hk_analysis_indicator_em（年度 + 报告期两次合并，
  含 BPS / BASIC_EPS / EPS_TTM / ROE_YEARLY / 同比 / 毛利率）；分红
  stock_hk_dividend_payout_em（除净日 + 分红方案文本；取括注前主方案的现金「每股派」
  并按币种折 HKD，送股/分派他股等非现金分派不计入）
- 美股财务：东财 stock_financial_us_analysis_indicator_em（单季报→近4季 TTM EPS；年报
  ROE）+ stock_financial_us_report_em（资产负债表归母股东权益→BPS，股数=归母净利/基本EPS）；
  美股无公开分红接口（AKShare 未封装）→ 红利因子不适用；东财未收录部分银行/保险类美股
  主要指标（上游返回空）→ 该类标的财务因子不适用（价格类照常，透明登记于拉取告警）
- 币种归一：港股按报价币种 HKD 折算（财务/分红原币可能为人民币/美元）；美股按 USD 折算。
  折算经新浪 fx_shkdcny/fx_susdcny 人民币中轴，用同步日即期汇率（历史区间用同一汇率，
  属透明简化）。
- PIT：港股/美股报告期统一取「报告期 + 90 天」为可用人时点（港股主板年报/中期披露窗口
  3/2 个月；美股 10-K/10-Q 披露窗口 60-90/40 天，取 90 天更保守）；precomputed
  ``eps_ttm``（港股 EPS_TTM 列 / 美股近 4 季合计）优先于引擎 A 的 YTD 递推。
- 实时行情：新浪 hq.sinajs.cn 批量（sh/sz{code}、hk{code}、gb_{ticker}），腾讯 qt.gtimg.cn 兜底
- 汇率：新浪 hq.sinajs.cn fx_shkdcny / fx_susdcny（港元/美元兑人民币）
- 交易日历：A股 新浪日历不变；港股/美股 由本市场 K 线日期并集推导
  （trade_calendar_hk.json / trade_calendar_us.json，供日历校验按市场分组）

纪律：仅调用公开函数（无下划线内部函数）；全部数据附溯源 provenance（接口/参数/
行数/sha256）；财务报告期按监管披露截止日保守推算可用人时点（PIT）。
"""
from __future__ import annotations
import json, re, time, hashlib, datetime as dt
from pathlib import Path

# ── 组合定义（用户指定四标的；未提供权重 → 等权 25%）──────────────
CN4 = [
    {"code": "601058", "secid": "601058.SH", "sina": "sh601058", "name": "赛轮集团", "industry": "汽车零部件·轮胎"},
    {"code": "002014", "secid": "002014.SZ", "sina": "sz002014", "name": "永新股份", "industry": "塑料软包装"},
    {"code": "600660", "secid": "600660.SH", "sina": "sh600660", "name": "福耀玻璃", "industry": "汽车零部件·汽车玻璃"},
    {"code": "002027", "secid": "002027.SZ", "sina": "sz002027", "name": "分众传媒", "industry": "广告传媒"},
]

# ── 组合定义二：multi5 多资产五标的（ETF/LOF；用户 2026-10-04 指定，等权 20%）──
# 注：A股无纳斯达克综合指数 ETF，以纳指100ETF(513100) 近似（行业标注 QDII-纳指100）
MULTI5 = [
    {"code": "510300", "secid": "510300.SH", "sina": "sh510300", "name": "沪深300ETF",
     "industry": "A股宽基指数", "ftype": "etf", "settle": "T+1", "cost": "ETF 免印花税·佣金约万2.5"},
    {"code": "501018", "secid": "501018.SH", "sina": "sh501018", "name": "南方原油LOF",
     "industry": "原油(QDII)", "ftype": "lof", "settle": "T+0(跨境LOF)", "cost": "LOF 免印花税·佣金约万2.5"},
    {"code": "518880", "secid": "518880.SH", "sina": "sh518880", "name": "黄金ETF",
     "industry": "黄金", "ftype": "etf", "settle": "T+0", "cost": "ETF 免印花税·佣金约万2.5"},
    {"code": "513100", "secid": "513100.SH", "sina": "sh513100", "name": "纳指ETF",
     "industry": "美股指数(QDII-纳指100)", "ftype": "etf", "settle": "T+0(跨境)", "cost": "ETF 免印花税·佣金约万2.5"},
    {"code": "511260", "secid": "511260.SH", "sina": "sh511260", "name": "十年国债ETF",
     "industry": "国债", "ftype": "etf", "settle": "T+0", "cost": "ETF 免印花税·佣金约万2.5"},
]

# 财务报告期 → 可用人时点（PIT）：按监管/交易所披露截止日保守推后
PIT_LAG_DAYS = {3: 30, 6: 62, 9: 31, 12: 120}

FIN_COLS = {
    "eps_ytd":  "摊薄每股收益(元)",
    "bps":      "每股净资产_调整前(元)",   # 调整后列对部分标的（如002027）上游数据异常，统一取调整前
    "roe":      "净资产收益率(%)",
    "roe_w":    "加权净资产收益率(%)",
    "rev_g":    "主营业务收入增长率(%)",
    "np_g":     "净利润增长率(%)",
    "margin":   "主营业务利润率(%)",
}

# A 股三表关键科目（P1.2 品质因子数据底座 D5）：{入库字段: (报表, 上游列名)}
# 供 qua_leverage（偿债）/qua_accrual（应计质量）/qua_fcf（自由现金流率）三因子；
# 仅 A 股适用（港股/美股三表口径不一，暂缺 → 该族因子不适用，透明登记）。
A_STMT_COLS = {
    "revenue":      ("利润表", "营业总收入"),
    "net_profit":   ("利润表", "净利润"),
    "total_assets": ("资产负债表", "资产总计"),
    "total_liab":   ("资产负债表", "负债合计"),
    "ocf":          ("现金流量表", "经营活动产生的现金流量净额"),
    "capex":        ("现金流量表", "购建固定资产、无形资产和其他长期资产所支付的现金"),
}

# ── 多市场（A股/港股/美股）公共定义与工具 ─────────────────────────

MARKET_CCY = {"A": "CNY", "HK": "HKD", "US": "USD"}

# 港股/美股财务报告期 → 可用人时点（PIT）保守披露窗口（统一 90 天，见模块 docstring）
FOREIGN_PIT_LAG_DAYS = {"HK": 90, "US": 90}

# 分红方案文本中的币种别名 → 规范币种
_DIV_CCY_ALIAS = {"港币": "HKD", "港元": "HKD", "人民币": "CNY", "美元": "USD"}

# 港股通标的静态快照（兜底；在线源为雪球港股通筛选）
from applib.ggt_static import GGT_SNAPSHOT_DATE, GGT_STATIC  # noqa: E402

_SINA_HQ_URL = "https://hq.sinajs.cn/list="
_TX_QT_URL = "https://qt.gtimg.cn/q="
_TX_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_HQ_HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}


def classify_secid(secid: str) -> str | None:
    """secid 后缀 → 市场：``.SH``/``.SZ``→A，``.HK``→HK，``.US``→US；无法识别 None。"""
    s = str(secid or "").strip().upper()
    if s.endswith((".SH", ".SZ")):
        return "A"
    if s.endswith(".HK"):
        return "HK"
    if s.endswith(".US"):
        return "US"
    return None


def _a_meta_from_secid(secid: str) -> dict | None:
    """未知 A 股/基金 secid → 抓取元数据（sina 符号/是否基金）；格式非法返回 None。

    基金判定沿用注册口径：``5`` 开头→sh 基金、``1`` 开头→sz 基金（ETF/LOF）。
    """
    code, _, suf = secid.partition(".")
    if not re.fullmatch(r"\d{6}", code) or suf not in ("SH", "SZ"):
        return None
    prefix = "sh" if suf == "SH" else "sz"
    is_fund = (suf == "SH" and code[0] == "5") or (suf == "SZ" and code[0] == "1")
    return {"code": code, "secid": secid, "sina": f"{prefix}{code}", "name": code,
            "industry": None, "is_fund": is_fund}


def _sina_hq_symbol(secid: str) -> str | None:
    """secid → 新浪实时行情符号（sh/sz{code}、hk{code}、gb_{ticker}）。"""
    mkt = classify_secid(secid)
    if mkt == "A":
        code, _, suf = secid.partition(".")
        return f"{'sh' if suf == 'SH' else 'sz'}{code}"
    if mkt == "HK":
        return f"hk{secid[:-3]}"
    if mkt == "US":
        return f"gb_{secid[:-3].lower()}"
    return None


def _parse_sina_hq(symbol: str, fields: list[str]) -> dict | None:
    """解析新浪 hq 单标的返回（GBK 逗号分隔）；按符号前缀分市场归一化。

    返回 ``{name, price, open, prev_close, high, low, chg, pct, volume, amount,
    time, currency}``；字段不足/价格为空返回 None。
    """

    def _f(i):
        return fields[i].strip() if len(fields) > i else ""

    if symbol.startswith(("sh", "sz")):          # A股
        price = _num(_f(3))
        if not price:
            return None
        prev = _num(_f(2))
        return {"name": _f(0), "price": price, "open": _num(_f(1)), "prev_close": prev,
                "high": _num(_f(4)), "low": _num(_f(5)), "volume": _num(_f(8)),
                "amount": _num(_f(9)), "time": f"{_f(30)} {_f(31)}".strip(),
                "pct": round((price / prev - 1) * 100, 4) if prev else None,
                "chg": (price - prev) if prev else None,
                "currency": "CNY"}
    if symbol.startswith("hk"):                  # 港股
        price = _num(_f(6))
        if not price:
            return None
        return {"name": _f(1) or _f(0), "price": price, "open": _num(_f(2)),
                "prev_close": _num(_f(3)), "high": _num(_f(4)), "low": _num(_f(5)),
                "chg": _num(_f(7)), "pct": _num(_f(8)), "volume": _num(_f(12)),
                "amount": _num(_f(11)), "time": f"{_f(17)} {_f(18)}".strip(),
                "currency": "HKD"}
    if symbol.startswith("gb_"):                 # 美股
        price = _num(_f(1))
        if not price:
            return None
        return {"name": _f(0), "price": price, "open": _num(_f(5)),
                "prev_close": _num(_f(26)) or None, "high": _num(_f(6)), "low": _num(_f(7)),
                "chg": _num(_f(4)), "pct": _num(_f(2)), "volume": _num(_f(10)),
                "amount": _num(_f(11)), "time": _f(3), "currency": "USD"}
    return None


def _parse_tx_quote(symbol: str, text: str) -> dict | None:
    """解析腾讯实时行情兜底（A股 q=sh601058 / 港股 q=hk00700 / 美股 q=s_usAAPL）。

    A股/港股共用布局：``[3]价 [4]昨收 [5]开 [6]量 [30]时间 [31]涨跌 [32]涨跌%``；
    美股简版：``[3]价 [4]涨跌 [5]涨跌% [6]量 [7]额``（无昨收，由 价−涨跌 推导）。
    """
    m = re.search(r'="([^"]*)"', text)
    if not m:
        return None
    f = m.group(1).split("~")
    try:
        if symbol.startswith("s_us"):
            price = _num(f[3])
            if not price:
                return None
            chg = _num(f[4])
            return {"name": f[1], "price": price,
                    "prev_close": (price - chg) if chg is not None else None,
                    "chg": chg, "pct": _num(f[5]), "volume": _num(f[6]), "amount": _num(f[7]),
                    "currency": "USD"}
        price = _num(f[3])
        if not price:
            return None
        is_hk = symbol.startswith("hk")
        return {"name": f[1], "price": price, "open": _num(f[5]), "prev_close": _num(f[4]),
                "chg": _num(f[31]), "pct": _num(f[32]), "volume": _num(f[6]),
                "time": f[30] if len(f) > 30 else None,
                "currency": "HKD" if is_hk else "CNY"}
    except (IndexError, ValueError):
        return None


def fetch_realtime_quotes(secids) -> dict:
    """批量实时行情：新浪 hq 主源（单请求批量）→ 腾讯逐标的兜底。

    入参 secid 列表（A股/港股/美股混合）；返回 ``{secid: {...归一化行情}}``，
    无法识别/双源均失败的 secid 不出现在返回中。永不抛异常（数据尽力而为）。
    """
    import requests

    out: dict[str, dict] = {}
    secids = [str(s).strip().upper() for s in secids if s]
    sym2secid = {}
    for s in secids:
        sym = _sina_hq_symbol(s)
        if sym and s not in out:
            sym2secid[sym] = s
    if not sym2secid:
        return out

    # ① 新浪批量（一次 HTTP；GBK 编码）
    missing = set(sym2secid.values())
    try:
        r = requests.get(_SINA_HQ_URL + ",".join(sym2secid), headers=_HQ_HEADERS, timeout=6)
        r.encoding = "gbk"
        for m in re.finditer(r'var hq_str_(\w+)="([^"]*)"', r.text):
            sym, payload = m.group(1), m.group(2)
            if sym in sym2secid and payload:
                q = _parse_sina_hq(sym, payload.split(","))
                if q:
                    secid = sym2secid.pop(sym)
                    q["source"] = "sina"
                    out[secid] = q
                    missing.discard(secid)
    except Exception:                           # noqa: BLE001 - 主源失败走兜底
        pass

    # ② 腾讯兜底（逐标的；仅补新浪缺失的）
    for secid in sorted(missing):
        mkt = classify_secid(secid)
        if mkt == "A":
            tx_sym = _sina_hq_symbol(secid)
        elif mkt == "HK":
            tx_sym = f"hk{secid[:-3]}"
        elif mkt == "US":
            tx_sym = f"s_us{secid[:-3]}"
        else:
            continue
        try:
            r = requests.get(_TX_QT_URL + tx_sym, headers=_HQ_HEADERS, timeout=6)
            r.encoding = "gbk"
            q = _parse_tx_quote(tx_sym, r.text)
            if q:
                q["source"] = "tencent"
                out[secid] = q
        except Exception:                       # noqa: BLE001 - 兜底也失败则跳过
            continue
    return out


def fetch_fx() -> dict:
    """港元/美元兑人民币汇率（新浪 hq fx_shkdcny / fx_susdcny）。

    返回 ``{"HKD_CNY": float, "USD_CNY": float, "as_of": "YYYY-MM-DD", "time": str,
    "source": "sina"}``；失败时返回占位（None 值 + error 字段），绝不抛异常。
    """
    import requests

    out = {"HKD_CNY": None, "USD_CNY": None, "as_of": dt.date.today().isoformat(),
           "time": None, "source": "sina"}
    try:
        r = requests.get(_SINA_HQ_URL + "fx_shkdcny,fx_susdcny", headers=_HQ_HEADERS, timeout=6)
        r.encoding = "gbk"
        for m in re.finditer(r'var hq_str_fx_s(\w+)="([^"]*)"', r.text):
            key, fields = m.group(1), m.group(2).split(",")
            if len(fields) > 8 and fields[8]:
                out["HKD_CNY" if key == "hkdcny" else "USD_CNY"] = _num(fields[8])
                if not out["time"]:
                    out["time"] = fields[0]
    except Exception as exc:                    # noqa: BLE001 - 汇率失败不阻断同步
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def fetch_ggt_components() -> tuple[list[str], str]:
    """港股通标的代码清单（5 位字符串，升序去重）。

    主源：雪球港股通筛选（market=HK&type=ggt，逐页 90 只）；失败降级
    ``ggt_static`` 静态快照。返回 ``(codes, source)``，绝不抛异常。
    """
    import requests

    codes: list[str] = []
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        s.get("https://xueqiu.com/hq", timeout=8)          # 种 cookie
        for page in range(1, 21):                           # 上限 ~1800 只，防御性封顶
            r = s.get("https://stock.xueqiu.com/v5/stock/screener/quote/list.json"
                      f"?page={page}&size=90&order=desc&order_by=market_capital&market=HK&type=ggt",
                      timeout=8)
            lst = (r.json().get("data") or {}).get("list") or []
            if not lst:
                break
            codes += [str(x.get("symbol", "")).zfill(5) for x in lst if x.get("symbol")]
        codes = sorted({c for c in codes if re.fullmatch(r"\d{5}", c)})
        if codes:
            return codes, "xueqiu"
    except Exception:                                       # noqa: BLE001 - 降级静态快照
        pass
    return list(GGT_STATIC), f"static_snapshot@{GGT_SNAPSHOT_DATE}"


def _tx_http(param: str, tries: int = 3, retry_empty: bool = False) -> list:
    """腾讯K线接口底层请求（含内部重试与退避；突发限流时响应会随机失败）。

    返回 K 线行列表（可能为空）；``tries`` 次仍失败返回空列表。
    限流的失败形态有两种：抛异常 / 返回空 ``day`` 列表（合法场景如上市前窗口也会为空），
    ``retry_empty=True`` 时对空列表同样重试（除息日分页提取用，窗口按理非空）。
    """
    import requests

    for i in range(tries):
        try:
            time.sleep(0.3)                                 # 温和限速，规避突发限流
            r = requests.get(_TX_KLINE_URL, params={"param": param},
                             headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                             timeout=10)
            d = (r.json().get("data") or {})
            for v in d.values():
                if isinstance(v, dict):
                    rows = v.get("day") or v.get("qfqday") or []
                    if rows or not retry_empty or i == tries - 1:
                        return rows
                    break                                   # 空列表且需重试 → 走退避
            else:
                if retry_empty and i < tries - 1:
                    time.sleep(1.0 * (i + 1))
                    continue
                return []
        except Exception:                                   # noqa: BLE001 - 限流/网络抖动 → 退避重试
            pass
        if i < tries - 1:
            time.sleep(1.0 * (i + 1))
    return []


def _tx_rows_to_bars(rows: list) -> list[dict]:
    """腾讯 K 线行 → ``[{dt, close}]``。"""
    return [{"dt": _iso(row[0]), "close": _num(row[2])} for row in rows if _iso(row[0])]


def _tx_rows_to_ex_dates(rows: list) -> list[str]:
    """腾讯 K 线行 → 除权除息日（事件元数据 ``FHcontent`` 分红 / ``ggContent`` 股本变动）。

    仅回购（``HGcontent``）不影响复权，不计入。
    """
    out = []
    for row in rows:
        dt_ = _iso(row[0])
        ev = row[6] if len(row) > 6 and isinstance(row[6], dict) else None
        if dt_ and ev and (ev.get("FHcontent") or ev.get("ggContent")):
            out.append(str(ev.get("cqr") or dt_))
    return out


def _tx_kline(symbol: str, count: int = 130, fq: str = "", retry_empty: bool = False) -> dict:
    """腾讯日K（交叉源）：``{"bars": [{dt, close}], "ex_dates": [除权除息日]}``。

    A股 ``sh601058`` / 港股 ``hk00700`` / 美股 ``usAAPL.OQ``（美股需交易所后缀）。
    注意 param 必须以逗号结尾（无 fq）或 ``qfq`` 结尾。
    """
    tail = ",qfq" if fq == "qfq" else ","
    rows = _tx_http(f"{symbol},day,,,{count}{tail}", retry_empty=retry_empty)
    return {"bars": _tx_rows_to_bars(rows), "ex_dates": _tx_rows_to_ex_dates(rows)}


def _tx_us_kline(ticker: str, count: int = 130, fq: str = "") -> dict:
    """腾讯美股日K：依次尝试 .OQ(纳斯达克)/.N(纽交所)/.A(美交所)。

    错误交易所后缀会返回 1-2 根幽灵K线 → 以 ≥30 根为有效门槛。
    """
    for suffix in (".OQ", ".N", ".A"):
        res = _tx_kline(f"us{ticker}{suffix}", count, fq)
        if len(res["bars"]) >= 30:
            return res
    return {"bars": [], "ex_dates": []}


def _tx_ex_dates(symbol: str, start_date: str, *, us_ticker: bool = False) -> tuple[list[str], str]:
    """腾讯 qfq 事件元数据 → 除权除息日全集（start_date 至今，2 年窗口分页）。

    单次接口上限 800 根 → 按 2 年日期窗口分页拉取并合并去重；
    美股（``us_ticker=True``）先探测有效交易所后缀再分页。
    返回 ``(ex_dates, tencent_symbol)``；失败返回空列表（符号原样返回）。
    """
    sym = symbol
    if us_ticker:
        for suf in (".OQ", ".N", ".A"):
            rows = _tx_http(f"us{symbol}{suf},day,,,{130},qfq")
            if len(rows) >= 30:
                sym = f"us{symbol}{suf}"
                break
        else:
            return [], sym
    try:
        d0 = dt.date.fromisoformat(str(start_date)[:10])
    except ValueError:
        d0 = dt.date.today() - dt.timedelta(days=365 * 6)
    today = dt.date.today()
    ex: set[str] = set()
    w_start = d0
    while w_start <= today:
        w_end = min(w_start + dt.timedelta(days=730), today)
        rows = _tx_http(f"{sym},day,{w_start.isoformat()},{w_end.isoformat()},800,qfq",
                        retry_empty=True)
        ex.update(_tx_rows_to_ex_dates(rows))
        w_start = w_end + dt.timedelta(days=1)
    return sorted(ex), sym


def _tx_us_series(ticker: str, start_date: str) -> tuple[str, list, list[str]]:
    """腾讯美股主序列：探测交易所后缀 → 2 年窗口分页拉全历史（含事件元数据）。

    返回 ``(tencent_symbol, rows, ex_dates)``；全部失败返回 ``("", [], [])``。
    外层两轮重试（探测调用带 ``retry_empty``，规避限流随机失败）。
    """
    for _attempt in range(2):
        sym = ""
        for suf in (".OQ", ".N", ".A"):
            rows = _tx_http(f"us{ticker}{suf},day,,,{130},qfq", retry_empty=True)
            if len(rows) >= 30:
                sym = f"us{ticker}{suf}"
                break
        if not sym:
            time.sleep(2)
            continue
        try:
            d0 = dt.date.fromisoformat(str(start_date)[:10])
        except ValueError:
            d0 = dt.date.today() - dt.timedelta(days=365 * 6)
        all_rows: list = []
        ex_set: set[str] = set()
        w = d0
        while w <= dt.date.today():
            wend = min(w + dt.timedelta(days=730), dt.date.today())
            rows = _tx_http(f"{sym},day,{w.isoformat()},{wend.isoformat()},800,qfq",
                            retry_empty=True)
            all_rows.extend(rows)
            ex_set.update(_tx_rows_to_ex_dates(rows))
            w = wend + dt.timedelta(days=1)
        if len(all_rows) >= 30:
            return sym, all_rows, sorted(ex_set)
        time.sleep(2)                       # 分页整体失败 → 外层重试一轮
    return "", [], []


def _patch_ex_dates_from_divergence(
    qfq_recs: list[dict], raw_rows: list[dict], ex_dates: list[str], tx_symbol: str
) -> list[str]:
    """除息日定向补漏（抗腾讯限流导致的整窗丢失）。

    思路：新浪 qfq 与 raw 日涨跌幅的**背离日**即疑似除权除息日（复权在该日生效）。
    对不在 ``ex_dates`` 的背离日，仅拉取该日 ±7 天的腾讯 qfq 小窗口确认事件元数据
    （``FHcontent``/``ggContent``）——确认才豁免；未确认的日期留给 adjust_check 如实
    报错（非循环论证：豁免依据始终是腾讯独立元数据，而非背离本身）。
    """
    ex = set(ex_dates)
    raw = {r["dt"]: r["close"] for r in raw_rows if r.get("dt")}
    raw_dts = sorted(raw)
    prev = None
    missing: list[str] = []
    for r in qfq_recs:
        d, pct = r.get("dt"), r.get("pct")
        if prev and d and pct is not None and prev in raw and d in raw and raw[prev]:
            rp = raw[d] / raw[prev] - 1
            if abs(pct - rp) > 0.005 and d not in ex:
                missing.append(d)
        if d:
            prev = d
    for d in missing:
        c = dt.date.fromisoformat(d)
        rows = _tx_http(f"{tx_symbol},day,{(c - dt.timedelta(days=7)).isoformat()},"
                        f"{(c + dt.timedelta(days=7)).isoformat()},30,qfq", retry_empty=True)
        ex.update(x for x in _tx_rows_to_ex_dates(rows) if x == d)
    return sorted(ex)


# ── 港股/美股财务与分红（V4-β 补充引擎 A 因子）────────────────────

def _ccy_rate_to_cny(ccy, fx: dict) -> float | None:
    """币种 → 兑人民币汇率（元/单位）；未知币种或汇率缺失返回 None。"""
    c = str(ccy or "").strip()
    if c in ("CNY", "RMB", "人民币", "元"):
        return 1.0
    if c in ("HKD", "港元", "港币"):
        return fx.get("HKD_CNY")
    if c in ("USD", "美元"):
        return fx.get("USD_CNY")
    return None


def _ccy_to(amount, ccy, target: str, fx: dict) -> float | None:
    """金额由 ``ccy`` 折算为 ``target``（经人民币中轴）；无法折算时原值返回。"""
    if amount is None:
        return None
    r_src, r_tgt = _ccy_rate_to_cny(ccy, fx), _ccy_rate_to_cny(target, fx)
    if not r_src or not r_tgt:
        return amount
    return amount * r_src / r_tgt


def _pit_announce(period: str, lag_days: int) -> str:
    """报告期 + 保守披露窗口 = 可用人时点（PIT）"""
    return (dt.date.fromisoformat(period) + dt.timedelta(days=lag_days)).isoformat()


def _parse_hk_div_text(text: str, fx: dict) -> float | None:
    """港股「分红方案」文本 → 每股派息（折算为 HKD）；解析失败/非现金分派返回 None。

    以括注前的主方案为准：主方案须为「每股派（现金）」方计入现金股息率——送股、
    分派他股（如「每10股分派1股美团B类普通股股份(相当于每股派18.13港元)」）等
    非现金分派一律返回 None，避免以「相当于」估值虚增股息率。金额按币种折 HKD
    （港股以 HKD 报价）。
    """
    if not text:
        return None
    main = re.split(r"[（(]", text, maxsplit=1)[0]      # 主方案（截去括注/特殊说明）
    m = re.search(r"每股派\s*(?:(?:股息|分红|红利|现金|派|息|特别|中期|末期|的|每股))*\s*"
                  r"(港币|港元|人民币|美元)?\s*([0-9]+(?:\.[0-9]+)?)", main)
    if not m:
        return None
    return _ccy_to(_num(m.group(2)), _DIV_CCY_ALIAS.get(m.group(1) or "港币", "HKD"), "HKD", fx)


def _fetch_hk_financials(ak, code: str, fx: dict, fin_start_year: str) -> tuple[list, list]:
    """港股 → ``(reports, dividends)``（引擎 A 财务/分红输入；价格币种 HKD）。"""
    rep_map: dict[str, dict] = {}
    for ind in ("报告期", "年度"):        # 报告期给近 9 期（含季报），年度补全更长历史
        try:
            df = _retry(lambda ind=ind: ak.stock_financial_hk_analysis_indicator_em(symbol=code, indicator=ind))
        except Exception as exc:          # noqa: BLE001 - 单接口失败不阻断（另一模式仍可补）
            print(f"  ⚠ {code} 港股财务({ind})拉取失败：{type(exc).__name__}: {exc}")
            continue
        for _, r in df.iterrows():
            period = _iso(r.get("REPORT_DATE"))
            if not period:
                continue
            ccy = r.get("CURRENCY") or "港元"
            rep_map[period] = {
                "period": period,
                "announce_date": _pit_announce(period, FOREIGN_PIT_LAG_DAYS["HK"]),
                "eps_ytd": _ccy_to(_num(r.get("BASIC_EPS")), ccy, "HKD", fx),   # 累计 YTD
                "eps_ttm": _ccy_to(_num(r.get("EPS_TTM")), ccy, "HKD", fx),     # 上游直出 TTM
                "bps": _ccy_to(_num(r.get("BPS")), ccy, "HKD", fx),
                "roe": _num(r.get("ROE_YEARLY")), "roe_w": _num(r.get("ROE_AVG")),
                "rev_g": _num(r.get("OPERATE_INCOME_YOY")),
                "np_g": _num(r.get("HOLDER_PROFIT_YOY")),
                "margin": _num(r.get("GROSS_PROFIT_RATIO")),
                "ccy": str(ccy),
            }
    lo = f"{fin_start_year}-01-01"
    reports = sorted((v for v in rep_map.values() if v["period"] >= lo), key=lambda x: x["period"])

    dividends = []
    try:
        dv = _retry(lambda: ak.stock_hk_dividend_payout_em(symbol=code))
        for _, r in dv.iterrows():
            ex = _iso(r.get("除净日"))
            dps = _parse_hk_div_text(str(r.get("分红方案") or ""), fx) if ex else None
            if ex and dps is not None:
                dividends.append({"ex_date": ex, "announce": _iso(r.get("最新公告日期")), "dps": dps})
    except Exception as exc:              # noqa: BLE001 - 分红失败不阻断（红利因子降级为 None）
        print(f"  ⚠ {code} 港股分红拉取失败：{type(exc).__name__}: {exc}")
    return reports, sorted(dividends, key=lambda x: x["ex_date"])


def _fetch_us_financials(ak, ticker: str, fx: dict, fin_start_year: str) -> tuple[list, list]:
    """美股 → ``(reports, dividends=[])``（价格币种 USD）。

    - TTM EPS：东财「单季报」近 4 个连续单季摊薄 EPS 之和（跨财年，规避非日历财年）；
    - BPS：年报资产负债表「归属于母公司股东权益」/ 股数，股数 = 归母净利润 / 基本 EPS；
    - 美股无 AKShare 公开分红接口 → 红利因子不适用（dividends 为空列表）。
    """
    def _ind(mode: str):
        try:
            return _retry(lambda: ak.stock_financial_us_analysis_indicator_em(symbol=ticker, indicator=mode))
        except Exception as exc:          # noqa: BLE001
            print(f"  ⚠ {ticker} 美股财务({mode})拉取失败：{type(exc).__name__}: {exc}")
            return None

    q = _ind("单季报")
    if q is None or len(q) == 0:
        return [], []
    rows = []
    for _, r in q.iterrows():
        period = _iso(r.get("REPORT_DATE"))
        if period:
            rows.append({"period": period, "eps_q": _num(r.get("BASIC_EPS")),
                         "rev_g": _num(r.get("OPERATE_INCOME_YOY")),
                         "np_g": _num(r.get("PARENT_HOLDER_NETPROFIT_YOY")),
                         "margin": _num(r.get("GROSS_PROFIT_RATIO")),
                         "ccy": str(r.get("CURRENCY_ABBR") or "USD")})
    rows.sort(key=lambda x: x["period"])
    for i, row in enumerate(rows):        # 近 4 个连续单季之和（不足/含空值 → None）
        win = [w["eps_q"] for w in rows[max(0, i - 3):i + 1]]
        row["eps_ttm"] = sum(win) if len(win) == 4 and all(v is not None for v in win) else None

    bps_by_ann, roe_by_ann = {}, {}
    ann = _ind("年报")
    if ann is not None and len(ann):
        eq: dict[str, float] = {}
        try:
            bs = _retry(lambda: ak.stock_financial_us_report_em(
                stock=ticker, symbol="资产负债表", indicator="年报"))
            for _, r in bs.iterrows():
                if str(r.get("STD_ITEM_CODE")) == "004013999":   # 归属于母公司股东权益
                    p = _iso(r.get("REPORT_DATE"))
                    if p:
                        eq[p] = _num(r.get("AMOUNT"))
        except Exception as exc:          # noqa: BLE001 - 缺 BS → BPS 不适用（PB 因子降级）
            print(f"  ⚠ {ticker} 美股资产负债表拉取失败：{type(exc).__name__}: {exc}")
        for _, r in ann.iterrows():
            p = _iso(r.get("REPORT_DATE"))
            if not p:
                continue
            roe_by_ann[p] = _num(r.get("ROE_AVG"))
            eps, npf, e = _num(r.get("BASIC_EPS")), _num(r.get("PARENT_HOLDER_NETPROFIT")), eq.get(p)
            if e and eps and npf and eps > 0 and npf > 0:
                bps_by_ann[p] = _ccy_to(e * eps / npf, r.get("CURRENCY_ABBR") or "USD", "USD", fx)

    def _latest(m: dict, p: str):
        c = [x for x in m if x <= p]      # 上游按报告期倒序返回：须取最大期而非末位
        return m[max(c)] if c else None

    lo = f"{fin_start_year}-01-01"
    reports = []
    for row in rows:
        p, ccy = row["period"], row["ccy"]
        if p < lo:
            continue
        reports.append({
            "period": p,
            "announce_date": _pit_announce(p, FOREIGN_PIT_LAG_DAYS["US"]),
            "eps_ytd": _ccy_to(row["eps_q"], ccy, "USD", fx),        # 单季摊薄 EPS（PE 用 eps_ttm）
            "eps_ttm": _ccy_to(row["eps_ttm"], ccy, "USD", fx),
            "bps": _latest(bps_by_ann, p),                            # 最近年报 BPS（跨季沿用）
            "roe": _latest(roe_by_ann, p), "roe_w": None,
            "rev_g": row["rev_g"], "np_g": row["np_g"], "margin": row["margin"],
            "ccy": ccy,
        })
    return reports, []


def _iso(x) -> str | None:
    if x is None:
        return None
    s = str(x)[:10]
    return s if len(s) == 10 and s[4] == "-" else None


def _num(x) -> float | None:
    try:
        f = float(x)
        return f if f == f else None      # NaN → None
    except (TypeError, ValueError):
        return None


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _retry(fn, n=3, wait=2.0):
    last = None
    for i in range(n):
        try:
            return fn()
        except Exception as e:            # 上游偶发限流/超时：退避重试
            last = e
            time.sleep(wait * (i + 1))
    raise RuntimeError(f"akshare 重试 {n} 次仍失败：{last}")


def _announce_date(period: str) -> str:
    d = dt.date.fromisoformat(period)
    return (d + dt.timedelta(days=PIT_LAG_DAYS[d.month])).isoformat()


def _fetch_market_cap(ak, universe, start: str) -> dict:
    """总市值历史序列（P0.3 敞口报告的规模维度）。

    来源：百度股市通估值（``ak.stock_zh_valuation_baidu``，indicator='总市值'，period='全部'）。
    仅 A 股股票适用（百度估值接口覆盖 A 股；基金/港股/美股无此口径）。
    返回 ``{secid: [[dt, 总市值(亿元)], …]}``（自 start 起）；失败标的跳过并透明告警。
    """
    out: dict[str, list] = {}
    for u in universe:
        try:
            df = _retry(lambda u=u: ak.stock_zh_valuation_baidu(
                symbol=u["code"], indicator="总市值", period="全部"))
        except Exception as e:                # noqa: BLE001 - 市值缺失不阻断同步
            print(f"  ⚠ {u['name']} {u['secid']}：总市值拉取失败（{e}），规模维度不适用")
            continue
        series = [[_iso(r["date"]), _num(r["value"])]
                  for _, r in df.iterrows() if _iso(r["date"]) and _iso(r["date"]) >= start]
        series = [x for x in series if x[1] is not None]
        if series:
            out[u["secid"]] = series
            print(f"  ✓ {u['name']} {u['secid']}：总市值 {len(series)} 点（最新 {series[-1][1]:.1f} 亿）")
        else:
            print(f"  ⚠ {u['name']} {u['secid']}：总市值空序列，跳过")
    return out


def _fetch_a_statements(ak, sina: str) -> dict[str, dict]:
    """A 股三表关键科目（P1.2 品质因子数据底座 D5）。

    来源：新浪 ``ak.stock_financial_report_sina``（利润表/资产负债表/现金流量表）。
    返回 ``{period(YYYY-MM-DD): {字段…}}``；单表失败 → 该表字段缺省（透明降级，不阻断同步）。
    """
    out: dict[str, dict] = {}
    for sym in ("利润表", "资产负债表", "现金流量表"):
        cols = {k: c for k, (s, c) in A_STMT_COLS.items() if s == sym}
        try:
            df = _retry(lambda s=sym: ak.stock_financial_report_sina(stock=sina, symbol=s))
        except Exception:                     # noqa: BLE001 - 单表缺失不阻断
            continue
        if df is None or len(df) == 0:
            continue
        for _, r in df.iterrows():
            p = str(r.get("报告日"))
            if len(p) != 8 or not p.isdigit():     # 新浪该接口 报告日 为 YYYYMMDD
                continue
            period = f"{p[:4]}-{p[4:6]}-{p[6:8]}"
            d = out.setdefault(period, {})
            for k, c in cols.items():
                if d.get(k) is None:
                    d[k] = _num(r.get(c))
    return out


def fetch_all(ctx, start: str = "2021-01-01", fin_start_year: str = "2019",
              universe=None, incremental_from=None) -> dict:
    """拉取行情/财务/分红/日历 → remote/repo（云端替身）。

    T0.3 参数化（默认行为与历史逐字节一致）：
    - universe=None       → 默认全量 CN4 + MULTI5（现行为不变）
    - universe=[secid,…]  → 仅拉取这些 secid（在 CN4+MULTI5 定义中按 secid 匹配；
                            未匹配的 secid 跳过并在返回体 universe_missing 中登记）
    - incremental_from={secid: "YYYY-MM-DD"} → 该 secid 行情自「次日」起增量拉取；
                            未列出的 secid 仍按 start 全量
    """
    import akshare as ak
    today = dt.date.today().isoformat()
    repo = ctx.repo_dir
    if repo.exists():
        from .writeguard import snapshot_guard
        guard_snap = snapshot_guard(repo)   # T-R4 写保护：rmtree 前抢救用户登记面+历史K线
        import shutil
        shutil.rmtree(repo)               # 真实数据切换：整体重建云端 main（α）
    else:
        guard_snap = {}
    for sub in ("market", "factors", "portfolio", "dim"):
        (repo / sub).mkdir(parents=True, exist_ok=True)

    # ── universe 选择与市场分类（universe=None → 默认 CN4+MULTI5，保持现行为）──
    all_defs = CN4 + MULTI5
    if universe is None:
        requested = [u["secid"] for u in all_defs]
    else:
        try:
            requested = [str(s) for s in universe]
        except TypeError:
            return {"fetched": False, "error": f"universe 必须是 secid 列表：{universe!r}"}
    known = {u["secid"]: u for u in all_defs}
    universe_missing = []
    a_stocks, a_funds, hk_stocks, us_stocks = [], [], [], []
    for secid in requested:
        if secid in known:
            u = known[secid]
            (a_funds if "ftype" in u else a_stocks).append(u)
            continue
        mkt = classify_secid(secid)
        if mkt == "A":
            meta = _a_meta_from_secid(secid)
            if meta is None:
                universe_missing.append(secid)
                continue
            (a_funds if meta["is_fund"] else a_stocks).append(meta)
        elif mkt == "HK":
            hk_stocks.append({"code": secid[:-3], "secid": secid})
        elif mkt == "US":
            us_stocks.append({"code": secid[:-3], "secid": secid})
        else:
            universe_missing.append(secid)
    universe_fetched = ([u["secid"] for u in a_stocks] + [u["secid"] for u in a_funds]
                        + [u["secid"] for u in hk_stocks] + [u["secid"] for u in us_stocks])

    # 未知标的（无内置名称）批量取实时行情补名称（尽力而为，失败回退 code）
    unnamed = [u for u in (*a_stocks, *a_funds, *hk_stocks, *us_stocks)
               if not u.get("name") or u["name"] == u["code"]]
    if unnamed:
        try:
            names = {s: q.get("name") for s, q in
                     fetch_realtime_quotes([u["secid"] for u in unnamed]).items()}
            for u in unnamed:
                if names.get(u["secid"]):
                    u["name"] = names[u["secid"]]
        except Exception:                       # noqa: BLE001 - 名称缺失不阻断同步
            pass

    def _start_for(secid: str) -> str:
        # 增量：自上次最后交易日的「次日」起拉取；未列出 → 全量 start
        if incremental_from and secid in incremental_from and incremental_from[secid]:
            d = dt.date.fromisoformat(str(incremental_from[secid])[:10])
            return (d + dt.timedelta(days=1)).isoformat()
        return start

    prov = {"fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
            "akshare_version": ak.__version__, "today": today,
            "universe": list(requested),
            "sources": [], "pit_rules": {
                "financial_report_lag_days": PIT_LAG_DAYS,
                "foreign_report_lag_days": FOREIGN_PIT_LAG_DAYS,
                "note": "报告期+保守披露窗口=可用人时点（A股 Q1→+30d，H1→+62d，Q3→+31d，FY→+120d；"
                        "港股/美股统一 +90d，覆盖港交所年报3个月/中期2个月与美股 10-K/10-Q 披露窗口）；"
                        "α 未逐笔核对公告日，取监管截止日兜底"},
            "methods": {
                "pe_ttm": "close/TTM摊薄EPS（PIT 阶梯函数，报告期按 announce_date 对齐）；A股 TTM EPS=上年FY+本年YTD−上年同期YTD，"
                          "港股/美股直接取上游客报 eps_ttm（港股 EPS_TTM 列；美股近 4 个连续单季合计）",
                "pb": "close/最新可用人报告期每股净资产；A股取新浪'调整前'列（'调整后'对部分标的异常，弃用），"
                      "港股取东财 BPS、美股取年报归母股东权益/股数（股数=归母净利/基本EPS）",
                "div_yield_ttm": "近365日除权除息（闭区间[as_of-365d, as_of]）每股派息合计/close",
                "vol": "日收益率样本标准差(ddof=1)年化×√252",
                "mom_12_1": "close[t-21]/close[t-252]−1（剔除最近一月）",
                "mom_20d": "close[-1]/close[-20]−1（20收盘窗口）",
                "ma20_break": "close/MA20−1，±2% 确认带",
                "fund_bars": "ETF/LOF（multi5）：新浪 fund_etf_hist_sina（对 ETF/LOF 均适用）无复权接口，"
                             "序列即不复权 raw；无财报/分红数据 → 估值/质量/成长因子与股息率不适用，动量/波动照常；"
                             "交叉源为腾讯 stock_zh_a_hist_tx（东财接口当前网络出口不可达，弃用）",
                "hk_bars": "港股：新浪 stock_hk_daily（qfq+raw，HKD 计价）；腾讯 web.ifzq.gtimg.cn 交叉源（不复权 HKD）；"
                           "港股通名单：雪球港股通筛选（降级 ggt_static 快照）",
                "us_bars": "美股：新浪 stock_us_daily（qfq+raw，USD 计价，无 amount 列）；腾讯 us{TICKER}.OQ/.N/.A 交叉源",
                "hk_fin": "港股财务/分红：东财 stock_financial_hk_analysis_indicator_em（年度+报告期合并；"
                          "BASIC_EPS/EPS_TTM/BPS/ROE_YEARLY/营收·净利同比/毛利率）+ "
                          "stock_hk_dividend_payout_em（除净日+每股派息，取括注前主方案的现金「每股派」并折 HKD，"
                          "送股/分派他股等非现金分派不计入）；财务原币（常为人民币）经即期汇率折 HKD 对齐报价币种",
                "us_fin": "美股财务：东财 stock_financial_us_analysis_indicator_em（单季报近 4 季合计得 TTM EPS；"
                          "年报 ROE）+ stock_financial_us_report_em（资产负债表归母股东权益→BPS，股数=归母净利/基本EPS）；"
                          "美股无公开分红接口（AKShare 未封装）→ 股息率不适用；"
                          "东财对部分银行/保险类美股未收录主要指标（上游返回空）→ 该类标的财务因子不适用（价格类照常）"},
                "market_cap": "总市值（P0.3 敞口报告规模维度）：百度股市通估值 stock_zh_valuation_baidu"
                              "（indicator='总市值'，period='全部'，单位亿元），仅 A 股股票适用",
            "note": "永新=永新股份(002014.SZ)；cn4 未提供成本与权重 → 等权名义组合(每标的25%)；"
                    "multi5 五资产等权 20%×5，纳指以纳指100ETF(513100)近似（A股无综指ETF）；"
                    "V4-β 支持 A股/港股/美股 混合 universe（按 secid 后缀 .SH/.SZ/.HK/.US 分类）"}

    def _reg(dataset, interface, upstream, params, rows, relpath):
        p = repo / relpath
        (repo / relpath).parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8") if relpath.endswith(".json") else None
        prov["sources"].append({"dataset": dataset, "interface": interface, "upstream": upstream,
                                "params": params, "file": relpath, "sha256": _sha256_file(p),
                                "bytes": p.stat().st_size})

    # ── ① 交易日历 ──
    cal = _retry(lambda: ak.tool_trade_date_hist_sina())
    dates = [_iso(d) for d in cal["trade_date"].tolist()]
    _reg("dwd_trade_calendar", "ak.tool_trade_date_hist_sina()", "新浪财经", {}, dates, "market/trade_calendar.json")

    # 汇率（港元/美元兑人民币）：港股/美股财务与分红币种归一需要，提前取一次复用（③ 落盘）
    fx = fetch_fx()

    # ── ② A股股票：行情(qfq/raw) + 交叉源(腾讯) + 财务 + 分红（CN4 已知 + 用户注册标的）──
    bars_qfq, bars_raw, cross_tx, fundamentals = {}, {}, {}, {}
    universe_failed: list[str] = []          # 上游整体失败（0 根 K 线）的标的：不入库、透明登记
    for u in a_stocks:
        s0 = _start_for(u["secid"])       # T0.3：增量时自次日；否则 = start（默认不变）
        qfq = _retry(lambda: ak.stock_zh_a_daily(symbol=u["sina"], start_date=s0.replace("-", ""),
                                                 end_date=today.replace("-", ""), adjust="qfq"))
        raw = _retry(lambda: ak.stock_zh_a_daily(symbol=u["sina"], start_date=s0.replace("-", ""),
                                                 end_date=today.replace("-", ""), adjust=""))
        tq = (dt.date.today() - dt.timedelta(days=120)).isoformat().replace("-", "")
        tx = _retry(lambda: ak.stock_zh_a_hist_tx(symbol=u["sina"], start_date=tq, end_date=today.replace("-", "")))

        recs, prev = [], None
        for _, r in qfq.iterrows():
            c = _num(r["close"])
            pct = (c / prev - 1) if (prev and c and prev) else None
            recs.append({"dt": _iso(r["date"]), "open": _num(r["open"]), "high": _num(r["high"]),
                         "low": _num(r["low"]), "close": c, "volume": int(r["volume"]),
                         "amount": _num(r["amount"]), "pct": pct})
            prev = c
        bars_qfq[u["secid"]] = recs
        bars_raw[u["secid"]] = [{"dt": _iso(r["date"]), "close": _num(r["close"])} for _, r in raw.iterrows()]
        cross_tx[u["secid"]] = [{"dt": _iso(r["date"]), "close": _num(r["close"])} for _, r in tx.iterrows()]

        fin = _retry(lambda: ak.stock_financial_analysis_indicator(symbol=u["code"], start_year=fin_start_year))
        stmts = _fetch_a_statements(ak, u["sina"])     # P1.2/D5：三表关键科目（按报告期并入）
        reports = []
        for _, r in fin.iterrows():
            period = _iso(r["日期"])
            if not period:
                continue
            reports.append({"period": period, "announce_date": _announce_date(period),
                            **{k: _num(r.get(c)) for k, c in FIN_COLS.items()},
                            **(stmts.get(period) or {})})
        dvd = _retry(lambda: ak.stock_history_dividend_detail(symbol=u["code"], indicator="分红"))
        dividends = [{"ex_date": _iso(r["除权除息日"]), "announce": _iso(r["公告日期"]),
                      "dps": (_num(r["派息"]) or 0) / 10, "song": _num(r["送股"]), "zhuang": _num(r["转增"])}
                     for _, r in dvd.iterrows()
                     if _iso(r["除权除息日"]) and str(r.get("进度")) == "实施"]
        fundamentals[u["secid"]] = {"reports": sorted(reports, key=lambda x: x["period"]),
                                    "dividends": sorted(dividends, key=lambda x: x["ex_date"])}
        print(f"  ✓ {u['name']} {u['secid']}：bars={len(recs)} raw={len(bars_raw[u['secid']])} "
              f"tx={len(cross_tx[u['secid']])} 报告期={len(reports)} 三表期={len(stmts)} 分红={len(dividends)}")

    # ── ②' 基金标的（MULTI5 已知 + 用户注册 ETF/LOF）：新浪 fund_etf_hist_sina + 腾讯交叉源 ──
    tq8 = (dt.date.today() - dt.timedelta(days=120)).isoformat().replace("-", "")
    corp_actions = []   # 份额折算/拆分登记（价格断层保护）
    for u in a_funds:
        s0 = _start_for(u["secid"])       # T0.3：增量时自次日；否则 = start（默认不变）
        hist = _retry(lambda: ak.fund_etf_hist_sina(symbol=u["sina"]))
        has_amt = "amount" in getattr(hist, "columns", [])
        recs, prev = [], None
        for _, r in hist.iterrows():
            c = _num(r["close"])
            pct = (c / prev - 1) if (prev and c and prev) else None
            if pct is not None and abs(pct) > 0.33:
                # ETF/LOF 单日涨跌幅远小于 33%：此类跳变为份额折算/拆分，非真实收益 →
                # pct 置 None（持有人份额同比例变化、市值连续，组合净值按 0 收益处理）
                corp_actions.append({"secid": u["secid"], "dt": _iso(r["date"]),
                                     "prev_close": prev, "close": c, "chg": round(pct, 4),
                                     "note": "疑似份额折算/拆分（新浪无复权接口），该日 pct 置 None"})
                pct = None
            v = _num(r["volume"])
            recs.append({"dt": _iso(r["date"]), "open": _num(r["open"]), "high": _num(r["high"]),
                         "low": _num(r["low"]), "close": c, "volume": int(v) if v is not None else None,
                         "amount": _num(r["amount"]) if has_amt else None, "pct": pct})
            prev = c
        recs = [r for r in recs if r["dt"] and r["dt"] >= s0]   # 全量历史 → 起始日切片（pct 链完整保留）
        bars_qfq[u["secid"]] = recs                                # 基金无复权接口：主序列即 raw
        tx = _retry(lambda: ak.stock_zh_a_hist_tx(symbol=u["sina"], start_date=tq8,
                                                  end_date=today.replace("-", "")))
        cross_tx[u["secid"]] = [{"dt": _iso(r["date"]), "close": _num(r["close"])}
                                for _, r in tx.iterrows() if _iso(r["date"])]
        print(f"  ✓ {u['name']} {u['secid']}：bars={len(recs)}（sina 不复权） tx_cross={len(cross_tx[u['secid']])}")

    # ── ②'' 港股标的：新浪 stock_hk_daily（qfq+raw，HKD）+ 腾讯交叉源（raw + 除息事件）──
    adjust_ex_dates: dict[str, list] = {}
    for u in hk_stocks:
        s0 = _start_for(u["secid"])
        qfq = _retry(lambda u=u: ak.stock_hk_daily(symbol=u["code"], adjust="qfq"))
        raw = _retry(lambda u=u: ak.stock_hk_daily(symbol=u["code"], adjust=""))
        if qfq is None or len(qfq) == 0:
            universe_failed.append(u["secid"])
            print(f"  ⚠ {u['name']} {u['secid']}：新浪港股K线为空，跳过（universe_failed）")
            continue
        recs, prev = [], None
        for _, r in qfq.iterrows():
            c = _num(r["close"])
            pct = (c / prev - 1) if (prev and c and prev) else None
            v = _num(r["volume"])
            recs.append({"dt": _iso(r["date"]), "open": _num(r["open"]), "high": _num(r["high"]),
                         "low": _num(r["low"]), "close": c,
                         "volume": int(v) if v is not None else None,
                         "amount": _num(r["amount"]), "pct": pct})
            prev = c
        recs = [r for r in recs if r["dt"] and r["dt"] >= s0]   # 全量历史 → 起始日切片（pct 链完整保留）
        bars_qfq[u["secid"]] = recs
        bars_raw[u["secid"]] = [{"dt": _iso(r["date"]), "close": _num(r["close"])}
                                for _, r in raw.iterrows() if _iso(r["date"]) and _iso(r["date"]) >= s0]
        tx_raw = _tx_kline(f"hk{u['code']}", 130, retry_empty=True)
        cross_tx[u["secid"]] = tx_raw["bars"]
        # 除息日：腾讯 qfq 事件元数据（FHcontent 分红 / ggContent 股本变动 的 cqr），
        # 2 年窗口分页拉取覆盖全同步窗口；窗口被限流丢失时按背离日定向补漏
        ex, tx_sym = _tx_ex_dates(f"hk{u['code']}", s0)
        if tx_sym:
            ex = _patch_ex_dates_from_divergence(recs, bars_raw[u["secid"]], ex, tx_sym)
        if ex:
            adjust_ex_dates[u["secid"]] = ex
        # 财务/分红（东财）：补齐引擎 A 估值/质量/成长/红利因子（币种归一 → HKD）
        reports, dividends = _fetch_hk_financials(ak, u["code"], fx, fin_start_year)
        fundamentals[u["secid"]] = {"reports": reports, "dividends": dividends}
        print(f"  ✓ {u['name']} {u['secid']}：hk bars={len(recs)} raw={len(bars_raw[u['secid']])} "
              f"tx_cross={len(cross_tx[u['secid']])} 除息日={len(adjust_ex_dates.get(u['secid'], []))} "
              f"报告期={len(reports)} 分红={len(dividends)}")

    # ── ②''' 美股标的：主序列 腾讯K线（raw 口径，分页拉全历史）+ 新浪 raw 对账 ──
    # 数据源选择说明（V4-β）：新浪美股 qfq 对部分 ticker 存在非除息日的系统性噪声
    # （如 JPM，qfq/raw 因子逐日漂移 ±0.1~1.2%）；腾讯美股序列与新浪 raw 日涨跌幅
    # 全窗口零背离（实测 1444 点）。故美股主序列取腾讯（同 ETF/LOF「无复权接口即
    # raw」先例，动量/波动因子不受影响，估值因子本就不适用），新浪 raw 作独立对账源；
    # 腾讯整体失败时降级新浪 raw 为主序列（此时复权对账退化为同源，透明登记）。
    for u in us_stocks:
        s0 = _start_for(u["secid"])
        # 财务（东财）：补齐引擎 A 估值/质量/成长因子（美股无公开分红接口 → 红利因子不适用）
        us_reports, us_dividends = _fetch_us_financials(ak, u["code"], fx, fin_start_year)
        raw = _retry(lambda u=u: ak.stock_us_daily(symbol=u["code"], adjust=""))
        sina_raw = [{"dt": _iso(r["date"]), "close": _num(r["close"])}
                    for _, r in raw.iterrows() if _iso(r["date"]) and _iso(r["date"]) >= s0]
        tx_sym, tx_rows, ex_dates_us = _tx_us_series(u["code"], s0)
        if tx_rows:
            bars_raw[u["secid"]] = sina_raw
            cross_tx[u["secid"]] = _tx_rows_to_bars(tx_rows[-130:])
            if ex_dates_us:
                adjust_ex_dates[u["secid"]] = ex_dates_us
        else:
            # 降级：新浪 raw 兼作主序列与 raw 对（adjust 对账退化为同源；cross_source 仍双源）
            print(f"  ⚠ {u['secid']} 腾讯主序列拉取失败，降级新浪 raw 为主序列")
            bars_raw[u["secid"]] = sina_raw
            cross_tx[u["secid"]] = _tx_rows_to_bars([])
            if sina_raw:
                recs_fallback, prev_f = [], None
                rawmap = {r["dt"]: r["close"] for r in sina_raw}
                for d in sorted(rawmap):
                    c = rawmap[d]
                    recs_fallback.append({"dt": d, "open": None, "high": None, "low": None,
                                          "close": c, "volume": None, "amount": None,
                                          "pct": (c / prev_f - 1) if prev_f else None})
                    prev_f = c
                bars_qfq[u["secid"]] = recs_fallback
                fundamentals[u["secid"]] = {"reports": us_reports, "dividends": us_dividends}
                print(f"  ✓ {u['name']} {u['secid']}：us bars={len(recs_fallback)}(sina raw 降级) "
                      f"raw={len(sina_raw)} tx_cross=0 报告期={len(us_reports)}")
                continue
            else:
                universe_failed.append(u["secid"])
                continue
        recs, prev = [], None
        for row in tx_rows:
            dt_, c = _iso(row[0]), _num(row[2])
            if not dt_ or c is None:
                continue
            pct = (c / prev - 1) if (prev and c and prev) else None
            v = _num(row[5]) if len(row) > 5 else None
            recs.append({"dt": dt_, "open": _num(row[1]), "high": _num(row[3]),
                         "low": _num(row[4]), "close": c,
                         "volume": int(v) if v is not None else None,
                         "amount": None, "pct": pct})           # 腾讯美股K线无成交额列
            prev = c
        bars_qfq[u["secid"]] = recs
        fundamentals[u["secid"]] = {"reports": us_reports, "dividends": us_dividends}
        print(f"  ✓ {u['name']} {u['secid']}：us bars={len(recs)}(tx:{tx_sym}) raw={len(sina_raw)}(sina) "
              f"tx_cross={len(cross_tx[u['secid']])} 除息日={len(ex_dates_us)} 报告期={len(us_reports)}")

    _reg("dwd_daily_bar", "ak.stock_zh_a_daily/stock_hk_daily(qfq) + 腾讯usK线(raw口径) + ak.fund_etf_hist_sina", "新浪财经/腾讯证券",
         {"start": start, "end": today, "note": "A股/港股 qfq 前复权；美股 腾讯序列（raw 口径，新浪美股qfq部分标的存在噪声，弃用）；ETF/LOF 无复权接口（raw）"}, bars_qfq, "market/daily_bars.json")
    _reg("dwd_daily_bar_raw", "ak.stock_zh_a_daily/stock_hk_daily/stock_us_daily(adjust='')", "新浪财经",
         {"start": start, "end": today, "note": "A股/港股/美股股票 raw（基金无复权对，由 cross_source 双源覆盖）；美股 raw 兼作腾讯主序列的独立对账源"}, bars_raw, "market/daily_bars_raw.json")
    _reg("cross_check_tx", "ak.stock_zh_a_hist_tx() + web.ifzq.gtimg.cn(hk/us)", "腾讯证券",
         {"window_days": 120, "note": "A股（股票与基金）走 stock_zh_a_hist_tx；港股 hk{code}、美股 us{TICKER}.OQ/.N/.A 走腾讯K线接口"}, cross_tx, "market/cross_check_tx.json")
    if adjust_ex_dates:
        # 港股/美股除权除息日（腾讯 qfq 事件元数据 FHcontent/ggContent 的 cqr）：
        # adjust_check 经典 qfq-vs-raw 对账的豁免名单（回购 HGcontent 不影响复权，不计入）
        _reg("adjust_ex_dates", "web.ifzq.gtimg.cn(hk/us, qfq 事件元数据)", "腾讯证券",
             {"note": "港股/美股除权除息日（分红/送转/拆股），供 adjust_check 豁免"},
             adjust_ex_dates, "market/adjust_ex_dates.json")
    _reg("dwd_financial_report", "ak.stock_financial_analysis_indicator(A股) + "
         "stock_financial_hk_analysis_indicator_em(港股) + stock_financial_us_analysis_indicator_em(美股) + "
         "stock_financial_report_sina(A股三表：利润表/资产负债表/现金流量表)",
         "新浪财经/东方财富",
         {"start_year": fin_start_year,
          "note": "A股 eps_ytd 累计口径（引擎按 YTD 递推 TTM）；港股/美股附带 eps_ttm 上游直出 TTM，"
                  "港股财务原币经即期汇率折 HKD、美股折 USD 对齐各自报价币种；"
                  "港股分红 stock_hk_dividend_payout_em，美股无公开分红接口（dividends 为空）；"
                  "A股三表科目（D5/P1.2）：revenue/net_profit/total_assets/total_liab/ocf/capex 按报告期并入，"
                  "PIT 同 report 时点（报告期+披露窗口），供品质族 qua_leverage/qua_accrual/qua_fcf"},
         fundamentals, "factors/fundamental_values.json")

    # ── ③ 维度表 / 组合 / 因子注册中心 / 港股通名单 / 汇率 / 港美日历 ──

    # 港股通名单（雪球在线 → 静态快照降级；标记写入 dim 与 ggt_components.json）
    ggt_codes, ggt_source = fetch_ggt_components()
    ggt_set = set(ggt_codes)

    def _dim_rows():
        rows = []
        for u in a_stocks:
            rows.append({"secid": u["secid"], "name": u["name"], "market": "A", "asset_type": "STOCK",
                         "industry": u.get("industry"), "currency": "CNY", "hk_connect": None,
                         "source": u.get("source") or "组合定义（行业为申万口径常识标注）"})
        for u in a_funds:
            rows.append({"secid": u["secid"], "name": u["name"], "market": "A",
                         "asset_type": "ETF" if u.get("ftype") == "etf" else "LOF",
                         "industry": u.get("industry"), "currency": "CNY", "hk_connect": None,
                         "source": u.get("source") or "组合定义二 multi5（多资产）"})
        for u in hk_stocks:
            rows.append({"secid": u["secid"], "name": u["name"], "market": "HK", "asset_type": "STOCK",
                         "industry": u.get("industry"), "currency": "HKD",
                         "hk_connect": u["code"] in ggt_set,
                         "source": "V4-β 港股 universe"})
        for u in us_stocks:
            rows.append({"secid": u["secid"], "name": u["name"], "market": "US", "asset_type": "STOCK",
                         "industry": u.get("industry"), "currency": "USD", "hk_connect": None,
                         "source": "V4-β 美股 universe"})
        return rows

    (repo / "dim" / "security.json").write_text(json.dumps(_dim_rows(), ensure_ascii=False, indent=2),
                                                encoding="utf-8")
    _reg("dim_ggt_components", "雪球港股通筛选 market=HK&type=ggt（降级 ggt_static 快照）", "雪球/静态快照",
         {"source": ggt_source}, {"codes": ggt_codes, "source": ggt_source,
                                  "count": len(ggt_codes)}, "market/ggt_components.json")

    # 总市值（P0.3 敞口报告规模维度；仅 A 股股票适用）
    market_cap = _fetch_market_cap(ak, a_stocks, start)
    if market_cap:
        _reg("dim_market_cap", "ak.stock_zh_valuation_baidu(indicator='总市值', period='全部')", "百度股市通",
             {"start": start, "unit": "亿元",
              "note": "总市值历史序列（P0.3 敞口报告规模维度）；仅 A 股股票适用；"
                      "供月末再平衡时点取用（按最近可用点前向取值）"},
             market_cap, "dim/market_cap.json")

    # 港股/美股交易日历：本市场 K 线日期并集推导（A股日历来自新浪，见 ①）
    for mkt, group in (("hk", hk_stocks), ("us", us_stocks)):
        days = sorted({r["dt"] for u in group for r in bars_qfq.get(u["secid"], []) if r["dt"]})
        if days:
            _reg(f"dwd_trade_calendar_{mkt}", f"K线日期并集推导（{mkt} 市场）", "新浪财经",
                 {"note": "由本市场全部标的 K 线日期并集推导，供日历校验按市场分组"},
                 days, f"market/trade_calendar_{mkt}.json")
    (repo / "portfolio" / "cn4.json").write_text(json.dumps({
        "id": "cn4", "name": "cn4 · 赛轮/永新/福耀/分众", "base_ccy": "CNY",
        "strategy_binding": "cn4_quality_v1",
        "note": "用户指定四标的（永新=永新股份002014.SZ）；未提供权重 → 等权名义组合 25%×4",
        "positions": [{"secid": u["secid"], "name": u["name"], "market": "A",
                       "weight": 25.0, "trade_ccy": "CNY"} for u in CN4]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    (repo / "portfolio" / "multi5.json").write_text(json.dumps({
        "id": "multi5", "name": "multi5 · 沪深300/南方原油/黄金/纳指/十年国债", "base_ccy": "CNY",
        "strategy_binding": "multi_asset_v1",
        "note": "五资产等权名义组合 20%×5（A股宽基/原油/黄金/美股/国债）；全部 A 股上市、人民币计价"
                "（QDII 为人民币份额，无直接汇率敞口，存在场内溢价/折价风险）；"
                "A股无纳斯达克综合指数ETF，以纳指100ETF(513100)近似",
        "positions": [{"secid": u["secid"], "name": u["name"], "market": "A",
                       "asset_type": "ETF" if u["ftype"] == "etf" else "LOF",
                       "weight": 20.0, "trade_ccy": "CNY",
                       "settle": u["settle"], "cost": u["cost"]} for u in MULTI5]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    alpha_factors = [("val_pe_ttm", "市盈率TTM"), ("val_pb", "市净率"), ("val_pe_pct", "PE五年分位"),
                     ("val_pb_pct", "PB五年分位"), ("div_yield_ttm", "股息率TTM"),
                     ("qua_roe_ttm", "ROE TTM"), ("qua_margin", "主营业务利润率"),
                     ("grow_rev_yoy", "营收同比"), ("grow_np_yoy", "净利同比"),
                     ("mom_12_1", "12-1月动量"), ("vol_250", "250日年化波动")]
    signal_factors = [("mom_20d", "20日动量"), ("vol_20d", "20日年化波动"), ("ma20_break", "MA20突破"),
                      ("high20_touch", "20日新高"), ("dist_high20", "距20日高点")]
    # V4-β：A股/港股/美股 均计算引擎 A/B 因子（港股/美股财务因子见 methods.hk_fin/us_fin）
    registry = {"factors":
                [{"id": f, "name": n, "type": "alpha", "status": "active", "version": "v1.0",
                  "universe": ["CN", "HK", "US"]}
                 for f, n in alpha_factors] +
                [{"id": f, "name": n, "type": "signal", "status": "active", "version": "v1.0",
                  "universe": ["CN", "HK", "US"], "horizon_days": 20} for f, n in signal_factors]}
    (repo / "factors" / "registry.json").write_text(json.dumps(registry, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
    (repo / "market" / "fx.json").write_text(json.dumps({
        **fetch_fx(),
        "note": "V4-β 多市场组合需要：港元/美元持仓按 HKD_CNY/USD_CNY 折算人民币口径；"
                "港股通标的实际以港币报价、人民币结算（存在汇率与结算价差）"}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # ── ④ 溯源登记（自哈希最后登记）──
    # T-R4 写保护恢复：守护面合并回写（用户字段逐项保留/orphan 标记/K线历史不丢）
    guard_summary = {}
    if guard_snap:
        from .writeguard import restore_guard
        guard_summary = restore_guard(repo, guard_snap)
    if corp_actions:
        prov["fund_corporate_actions"] = corp_actions
    (repo / "market" / "provenance.json").write_text(
        json.dumps(prov, ensure_ascii=False, indent=2), encoding="utf-8")

    # 数据切换 → 远端基线重置（git 语义：新 main，旧 refs 作废）
    for f in (ctx.refs_file, ctx.crs_file):
        f.unlink(missing_ok=True)
    # T0.3：返回体增量扩展（既有字段 universe/start/fetched 保持原值不变）
    per_secid = {s: {"bars": len(arr), "last_dt": arr[-1]["dt"] if arr else None}
                 for s, arr in bars_qfq.items()}
    return {"fetched": True, "universe": [u["secid"] for u in CN4], "start": start,
            "datasets": [s["file"] for s in prov["sources"]],
            "provenance": str(repo / "market" / "provenance.json"),
            "as_of_bars": max((r["dt"] for arr in bars_qfq.values() for r in arr), default=None),
            "universe_fetched": universe_fetched, "universe_missing": universe_missing,
            "universe_failed": universe_failed,
            "market_cap_secids": sorted(market_cap),
            "incremental_from": dict(incremental_from) if incremental_from else {},
            "per_secid": per_secid,
            "write_guard": guard_summary}


# ══ 引擎C · 宏观流动性数据层（T-C1）══════════════════════════════════════════
# 定位（H-0006 纪律，最高红线）：引擎C = 市场条件变量（regime）数据底座。
# - 不决策：本层只产数据，不产生买卖信号、不参与权重（T-C2 计算层/T-C3 工具层承接）
# - 透明降级：单指标拉取失败不阻断整体；数据缺失 → unavailable，不进 data_risk/门禁
# - provenance：每指标登记来源机构名，可追溯
# - 增量防丢（carry_old 合并策略）：空窗口/拉取失败 → 该指标旧数据完整保留；
#   成功拉取 → union 合并（同 dt 新值覆盖旧值=点位修正，永不物理删除已落盘点）
#
# 组件化两步走（盘点确认 §4.3 规则 3）：数据集定义先以模块常量单点声明；
# 组件化重构 T-R6 迁移至 applib/datasets/ DatasetSpec（产物等价验证后收编本定义）。
# 边界：fetch_all 整体重建 repo 会清掉本文件 → 同步编排须在 fetch_all 之后调用
# 本函数重拉（T-C4 集成层衔接）；本函数自身只 upsert 单文件，不做 rmtree。

_HK_RATES_REL = "market/hk_rates.json"   # H-0006 三件套产物（本会话缺失 → HK 恒降级）

_MACRO_DATASET = {
    "id": "macro_liquidity",
    "path": "market/macro_liquidity.json",
    "merge_policy": "carry_old",
    # 旧会话基准：18 探 12 中（AKShare 1.19.1）；本会话实测 11 指标命中，
    # 港股 3 件套依赖 H-0006 资产（hk_rates.json 缺失 → requires 标记，恒 unavailable）
    "sources": [
        # ── A 股市场（6 指标）──
        {"id": "cn_m2_yoy", "market": "A", "dim": "D2", "name": "M2 同比", "unit": "%",
         "institution": "中国人民银行", "interface": "macro_china_money_supply", "params": {},
         "date": {"col": "月份", "format": "cn_month"},
         "value": {"col": "货币和准货币(M2)-同比增长"}},
        {"id": "cn_srzg", "market": "A", "dim": "D2", "name": "社融规模增量(当月)", "unit": "亿元",
         "institution": "中国人民银行", "interface": "macro_china_shrzgm", "params": {},
         "date": {"col": "月份", "format": "yyyymm"},
         "value": {"col": "社会融资规模增量"}},
        {"id": "shibor_1w", "market": "A", "dim": "D3", "name": "Shibor 1周", "unit": "%",
         "institution": "全国银行间同业拆借中心", "interface": "macro_china_shibor_all", "params": {},
         "date": {"col": "日期", "format": "date"}, "value": {"col": "1W-定价"}},
        {"id": "lpr_1y", "market": "A", "dim": "D3", "name": "LPR 1年", "unit": "%",
         "institution": "全国银行间同业拆借中心", "interface": "macro_china_lpr", "params": {},
         "date": {"col": "TRADE_DATE", "format": "date"}, "value": {"col": "LPR1Y"}},
        {"id": "fr007", "market": "A", "dim": "D3", "name": "FR007 质押式回购定盘利率", "unit": "%",
         "institution": "全国银行间同业拆借中心", "interface": "repo_rate_hist",
         "params": {"window_days": 240},      # 实测上游窗口上限 270~365 天间 → 240 天分页
         "date": {"col": "date", "format": "date"}, "value": {"col": "FR007"}, "paged": True},
        {"id": "cn_us_spread_10y", "market": "A", "dim": "D4", "name": "中美 10Y 国债利差",
         "unit": "pct", "institution": "中国债券信息网/美国财政部（东方财富汇总）",
         "interface": "bond_zh_us_rate", "params": {},
         "date": {"col": "日期", "format": "date"},
         "value": {"cols_sub": ["中国国债收益率10年", "美国国债收益率10年"]},
         "note": "利差=中债10Y−美债10Y；美方点位滞后于中方 → 序列截至美方最后可用点"},
        # ── 美股市场（5 指标）──
        {"id": "us_fed_rate", "market": "US", "dim": "D1", "name": "美联储联邦基金利率", "unit": "%",
         "institution": "美联储（金十数据汇总）", "interface": "macro_bank_usa_interest_rate", "params": {},
         "date": {"col": "日期", "format": "date"}, "value": {"col": "今值", "dropna": True},
         "note": "未来议息会议行今值为空（dropna 跳过）"},
        {"id": "us_10y_2y", "market": "US", "dim": "D3", "name": "美债 10Y-2Y 利差", "unit": "pct",
         "institution": "美国财政部（东方财富汇总）", "interface": "bond_zh_us_rate", "params": {},
         "date": {"col": "日期", "format": "date"},
         "value": {"col": "美国国债收益率10年-2年", "dropna": True}},
        {"id": "us_cpi_yoy", "market": "US", "dim": "D1", "name": "美国 CPI 同比", "unit": "%",
         "institution": "美国劳工统计局（金十数据汇总）", "interface": "macro_usa_cpi_yoy", "params": {},
         "date": {"col": "时间", "format": "date"}, "value": {"col": "现值", "dropna": True}},
        {"id": "us_nonfarm", "market": "US", "dim": "D1", "name": "美国非农就业人数变动", "unit": "万人",
         "institution": "美国劳工统计局（金十数据汇总）", "interface": "macro_usa_non_farm", "params": {},
         "date": {"col": "日期", "format": "date"}, "value": {"col": "今值", "dropna": True}},
        {"id": "us_gdp", "market": "US", "dim": "D1", "name": "美国 GDP 年化季环比", "unit": "%",
         "institution": "美国经济分析局（金十数据汇总）", "interface": "macro_usa_gdp_monthly", "params": {},
         "date": {"col": "日期", "format": "date"}, "value": {"col": "今值", "dropna": True}},
        # ── 港股市场（3 指标）：H-0006 资产缺失 → requires 标记，恒 unavailable 透明降级 ──
        {"id": "hk_hibor_1m", "market": "HK", "dim": "D3", "name": "HIBOR 1月", "unit": "%",
         "institution": "香港银行公会", "interface": "hk_rates_file", "requires": "H-0006"},
        {"id": "hk_hibor_3m", "market": "HK", "dim": "D3", "name": "HIBOR 3月", "unit": "%",
         "institution": "香港银行公会", "interface": "hk_rates_file", "requires": "H-0006"},
        {"id": "hk_usdcnh", "market": "HK", "dim": "D4", "name": "港汇 USD/CNH", "unit": "pct",
         "institution": "香港金管局/财资市场公会", "interface": "hk_rates_file", "requires": "H-0006"},
    ],
}


def _macro_month_cn(x) -> str | None:
    """「2008年02月份」→ 2008-02-01（月度值站位口径：dt=月初；T-C2 scorer 负责 12 月滚动+前值站位）"""
    m = re.search(r"(\d{4})年(\d{1,2})月", str(x))
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-01" if m else None


def _macro_month_compact(x) -> str | None:
    """「202603」→ 2026-03-01"""
    m = re.search(r"^(\d{4})(\d{2})$", str(x).strip())
    return f"{m.group(1)}-{m.group(2)}-01" if m else None


def _macro_repo_rate_hist(ak, start: str, end: str, window_days: int):
    """repo_rate_hist 分页拉取（实测上游窗口上限在 270~365 天之间，240 天保守分页）"""
    import pandas as pd
    e = dt.date.fromisoformat(end)
    cur = dt.date.fromisoformat(start)
    frames = []
    while cur <= e:
        w_end = min(cur + dt.timedelta(days=window_days - 1), e)
        f = _retry(lambda c=cur, we=w_end: ak.repo_rate_hist(      # noqa: B023
            start_date=c.strftime("%Y%m%d"), end_date=we.strftime("%Y%m%d")), n=2, wait=0.5)
        if f is not None and len(f):
            frames.append(f)
        cur = w_end + dt.timedelta(days=1)
    return pd.concat(frames, ignore_index=True) if frames else None


def _macro_call_iface(ak, spec: dict, start: str, end: str, cache: dict):
    """接口分派（列名/参数/顺序全部 2026-10-11 实测后写死，见 _MACRO_DATASET 注记）；
    共享接口（bond_zh_us_rate）单次拉取进程内缓存。"""
    name = spec["interface"]
    if name in cache:
        return cache[name]
    if name == "macro_china_money_supply":
        df = _retry(ak.macro_china_money_supply, n=2, wait=0.5)
    elif name == "macro_china_shrzgm":
        df = _retry(ak.macro_china_shrzgm, n=2, wait=0.5)
    elif name == "macro_china_shibor_all":
        df = _retry(ak.macro_china_shibor_all, n=2, wait=0.5)
    elif name == "macro_china_lpr":
        df = _retry(ak.macro_china_lpr, n=2, wait=0.5)
    elif name == "bond_zh_us_rate":
        df = _retry(ak.bond_zh_us_rate, n=2, wait=0.5)
    elif name == "macro_bank_usa_interest_rate":
        df = _retry(ak.macro_bank_usa_interest_rate, n=2, wait=0.5)
    elif name == "macro_usa_cpi_yoy":
        df = _retry(ak.macro_usa_cpi_yoy, n=2, wait=0.5)
    elif name == "macro_usa_non_farm":
        df = _retry(ak.macro_usa_non_farm, n=2, wait=0.5)
    elif name == "macro_usa_gdp_monthly":
        df = _retry(ak.macro_usa_gdp_monthly, n=2, wait=0.5)
    elif name == "repo_rate_hist":
        df = _macro_repo_rate_hist(ak, start, end, spec.get("params", {}).get("window_days", 240))
    else:
        raise KeyError(f"未登记的宏观接口：{name}")
    cache[name] = df
    return df


def _macro_rows_from_df(spec: dict, df) -> list[dict]:
    """DataFrame → [{dt, value}]（升序去重、同 dt 后值覆盖；None 值行跳过）"""
    dc, fmt = spec["date"]["col"], spec["date"]["format"]
    vcol, vsub = spec["value"].get("col"), spec["value"].get("cols_sub")
    seen: dict[str, float] = {}
    for _, r in df.iterrows():
        if fmt == "cn_month":
            d = _macro_month_cn(r[dc])
        elif fmt == "yyyymm":
            d = _macro_month_compact(r[dc])
        else:
            d = _iso(r[dc])
        if not d:
            continue
        if vsub is not None:                      # cols_sub：两列均可用才产点（利差）
            a, b = _num(r.get(vsub[0])), _num(r.get(vsub[1]))
            if a is None or b is None:
                continue
            val = round(a - b, 6)
        else:
            val = _num(r.get(vcol))
        if val is None:                           # 缺值行跳过（透明：不造点；scorer 端前值站位）
            continue
        seen[d] = val
    return [{"dt": d, "value": seen[d]} for d in sorted(seen)]


def _hk_rows_from_file(repo: Path, spec: dict) -> list[dict] | None:
    """H-0006 三件套（hk_rates.json）→ 指标序列；文件缺失/无法解析/键缺失 → None（降级）"""
    p = repo / _HK_RATES_REL
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:                              # noqa: BLE001 - 损坏文件按缺失降级
        return None
    key = spec["id"]
    seq = data.get(key) or data.get("series", {}).get(key) if isinstance(data, dict) else None
    out: dict[str, float] = {}
    for r in seq or []:
        d, v = _iso(r.get("dt")), _num(r.get("value"))
        if d and v is not None:
            out[d] = v
    return [{"dt": d, "value": out[d]} for d in sorted(out)] or None


def fetch_macro_liquidity(ctx, start: str = "2015-01-01", end: str | None = None) -> dict:
    """引擎C T-C1：三市场宏观指标拉取 → remote/repo/market/macro_liquidity.json（carry_old 合并）。

    - 14 指标声明（A 6 / US 5 / HK 3）：本会话实测 11 指标命中（AKShare 1.19.1）；
      港股 3 件套依赖 H-0006 资产（hk_rates.json 缺失）→ unavailable 透明降级
    - carry_old（红线4）：单指标失败/空窗口 → 旧数据完整保留（status=carried_old），
      黑名单登记；永不物理删除已落盘点；成功拉取 → union 合并（同 dt 新值覆盖）
    - 单指标失败不阻断整体（透明降级，不进 data_risk/六项门禁）
    - 月度指标 dt=月初站位（防前视的 12 月滚动/前值站位在 T-C2 scorer 层）
    - 纪律：不决策（纯数据层）；provenance 机构名逐指标登记
    """
    import akshare as ak
    end = end or dt.date.today().isoformat()
    repo = ctx.repo_dir
    fpath = repo / _MACRO_DATASET["path"]
    old = None
    if fpath.exists():
        try:
            old = json.loads(fpath.read_text())
        except Exception:                          # noqa: BLE001 - 损坏旧文件按无旧数据处理
            old = None
    old_ind = (old or {}).get("indicators", {})

    iface_cache: dict = {}
    indicators, warns, blacklist = {}, [], []
    for spec in _MACRO_DATASET["sources"]:
        sid = spec["id"]
        old_rows = old_ind.get(sid, {}).get("series") or []
        try:
            if spec.get("requires") == "H-0006":
                rows = _hk_rows_from_file(repo, spec)
                if rows is None:
                    raise FileNotFoundError(
                        f"{_HK_RATES_REL} 缺失（H-0006 数据资产未迁移，任务解耦）")
            else:
                df = _macro_call_iface(ak, spec, start, end, iface_cache)
                if df is None or len(df) == 0:
                    raise RuntimeError("上游返回空窗口")
                rows = _macro_rows_from_df(spec, df)
        except Exception as e:                     # noqa: BLE001 - 单指标失败不阻断整体
            reason = f"{type(e).__name__}: {e}"
            kind = "asset_missing" if spec.get("requires") else "upstream_fail"
            warns.append({"indicator": sid, "stage": "fetch", "error": reason[:200]})
            blacklist.append({"indicator": sid, "interface": spec["interface"],
                              "kind": kind, "reason": reason[:200],
                              "ts": dt.datetime.now().isoformat(timespec="seconds")})
            if old_rows:                           # carry_old：旧数据完整保留
                indicators[sid] = {**_macro_meta(spec), "status": "carried_old",
                                   "rows": len(old_rows),
                                   "first_dt": old_rows[0]["dt"], "last_dt": old_rows[-1]["dt"],
                                   "series": old_rows}
            else:                                  # 无旧可保 → 空序列（unavailable 语义）
                indicators[sid] = {**_macro_meta(spec), "status": "unavailable",
                                   "rows": 0, "first_dt": None, "last_dt": None, "series": []}
            continue
        # 成功拉取 → union 合并（增量防丢：旧点全保留；同 dt 新值覆盖=点位修正）
        merged: dict[str, float] = {r["dt"]: r["value"] for r in old_rows}
        merged.update({r["dt"]: r["value"] for r in rows})
        series = [{"dt": d, "value": merged[d]} for d in sorted(merged)]
        indicators[sid] = {**_macro_meta(spec), "status": "ok", "rows": len(series),
                           "first_dt": series[0]["dt"], "last_dt": series[-1]["dt"],
                           "series": series}

    as_of = max((v["last_dt"] for v in indicators.values() if v["last_dt"]), default=None)
    payload = {"dataset": _MACRO_DATASET["id"], "merge_policy": _MACRO_DATASET["merge_policy"],
               "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
               "akshare_version": ak.__version__, "start": start, "end": end,
               "as_of": as_of,   # 数据自描述：各指标最新可用 dt（≈最新交易日，T-C3 工具缺省口径）
               "indicators": indicators, "blacklist": blacklist, "warnings": warns,
               "discipline": {"decision_free": True,
                              "note": "引擎C 数据层：不产生买卖信号、不参与权重决策；"
                                      "数据缺失透明降级，不进 data_risk/六项验证门禁；"
                                      "月度指标 dt=月初站位，前值站位防前视由 T-C2 scorer 层处理"}}
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    n_ok = sum(1 for v in indicators.values() if v["status"] == "ok")
    n_carried = sum(1 for v in indicators.values() if v["status"] == "carried_old")
    n_unavail = sum(1 for v in indicators.values() if v["status"] == "unavailable")
    print(f"  ✓ macro_liquidity：{n_ok} ok / {n_carried} carried_old / {n_unavail} unavailable "
          f"（黑名单 {len(blacklist)} 条，as_of={as_of}）")
    return {"fetched": True, "dataset": _MACRO_DATASET["id"],
            "file": _MACRO_DATASET["path"], "merge_policy": _MACRO_DATASET["merge_policy"],
            "indicators": {sid: {"rows": v["rows"], "status": v["status"],
                                 "last_dt": v["last_dt"]} for sid, v in indicators.items()},
            "n_ok": n_ok, "n_carried_old": n_carried, "n_unavailable": n_unavail,
            "blacklist": blacklist, "as_of": as_of}


def _macro_meta(spec: dict) -> dict:
    """指标元数据（provenance：机构名可追溯；口径/接口/维度供 T-C2 scorer 与文档消费）"""
    return {"market": spec["market"], "dim": spec["dim"], "name": spec["name"],
            "unit": spec["unit"], "institution": spec["institution"],
            "interface": spec["interface"], **({"note": spec["note"]} if spec.get("note") else {})}
