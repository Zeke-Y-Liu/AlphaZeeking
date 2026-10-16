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
import json, logging, re, time, hashlib, datetime as dt
from pathlib import Path

from .datasets import (NON_SNAPSHOT_POLICIES, active_specs as _active_specs, get_spec,
                       get_spec_safe, spec_primary_interface, spec_primary_upstream)
from .datasets.merge import apply_policy, merge_bars as _merge_bars   # T-R10：合并语义真源迁入 datasets/merge.py

_log = logging.getLogger(__name__)

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
# M7 T-F1 扩展 8 科目：成本/利润总额/财务费用（EBIT 派生）、货币资金 + 四借款项
# （有息债务派生）——支撑 val_ev_ebitda(E)/qua_gross_margin/净现金口径；
# depreciation 缺口：新浪/东财现金流量表均无折旧摊销科目列（2026-10-11 实测）
# → EBITDA 不可精确算，val_ev_ebitda 于 T-F2 走缺口/降级登记（不 mock）。
A_STMT_COLS = {
    "revenue":      ("利润表", "营业总收入"),
    "cost_of_sales": ("利润表", "营业成本"),
    "total_profit": ("利润表", "利润总额"),
    "finance_expense": ("利润表", "财务费用"),
    "net_profit":   ("利润表", "净利润"),
    "total_assets": ("资产负债表", "资产总计"),
    "total_liab":   ("资产负债表", "负债合计"),
    "cash":         ("资产负债表", "货币资金"),
    "short_borrow": ("资产负债表", "短期借款"),
    "long_borrow":  ("资产负债表", "长期借款"),
    "bonds_payable": ("资产负债表", "应付债券"),
    "current_mature_debt": ("资产负债表", "一年内到期的非流动负债"),
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


def fetch_hk_rates(start_date: str = "2015-01-01") -> dict:
    """港美利率/汇率逐日序列（``carry_hk_us`` / ``peg_pressure`` 数据源，H-0006）。

    - HIBOR 1M：``ak.rate_interbank(香港银行同业拆借市场, Hibor港币, 1月)``（%）
    - 美联储目标利率：``ak.macro_bank_usa_interest_rate()``（今值，缺失以前值兜底；%）
    - USDHKD：``ak.currency_boc_sina`` 中行折算价交叉 USDCNY/HKDCNY（原表每 100 单位，比值不变）

    各序列按自身日期升序；合并序列取三者日期并集、逐字段前向填充（首值之前为 None）。
    返回 ``{"as_of", "series": [{dt, hibor1m, fed, usdhkd}], "source": {...}, "start"}``；
    失败带 ``error`` 字段，绝不抛异常（利率源不可达不阻断行情同步）。
    """
    import akshare as ak

    out = {"as_of": None, "series": [], "start": start_date,
           "source": {"hibor1m": "ak.rate_interbank(香港银行同业拆借市场/Hibor港币/1月)",
                      "fed": "ak.macro_bank_usa_interest_rate(今值, 前值兜底)",
                      "usdhkd": "ak.currency_boc_sina(美元/港币 中行折算价) 交叉 USDCNY/HKDCNY"}}
    end = dt.date.today().isoformat().replace("-", "")
    try:
        hib_df = _retry(lambda: ak.rate_interbank(market="香港银行同业拆借市场",
                                                  symbol="Hibor港币", indicator="1月"))
        fed_df = _retry(lambda: ak.macro_bank_usa_interest_rate())
        us_df = _retry(lambda: ak.currency_boc_sina(symbol="美元", start_date=start_date.replace("-", ""),
                                                    end_date=end))
        hk_df = _retry(lambda: ak.currency_boc_sina(symbol="港币", start_date=start_date.replace("-", ""),
                                                    end_date=end))
    except Exception as exc:                    # noqa: BLE001 - 利率源失败不阻断行情同步
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    hib = {_iso(r["报告日"]): _num(r["利率"]) for _, r in hib_df.iterrows()}
    hib = {d: v for d, v in hib.items() if d and v is not None and d >= start_date}
    fed = {}
    for _, r in fed_df.iterrows():
        d, v = _iso(r["日期"]), _num(r["今值"])
        if v is None:                                   # 今值缺失（未更新）→ 前值兜底
            v = _num(r["前值"])
        if d and v is not None and d >= start_date:
            fed[d] = v
    usd = {_iso(r["日期"]): _num(r["中行折算价"]) for _, r in us_df.iterrows()}
    hkd = {_iso(r["日期"]): _num(r["中行折算价"]) for _, r in hk_df.iterrows()}
    usdhkd = {d: usd[d] / hkd[d] for d in usd if d in hkd and usd[d] and hkd[d]}

    cur = {"hibor1m": None, "fed": None, "usdhkd": None}
    series = []
    for d in sorted(set(hib) | set(fed) | set(usdhkd)):
        if hib.get(d) is not None:
            cur["hibor1m"] = hib[d]
        if fed.get(d) is not None:
            cur["fed"] = fed[d]
        if usdhkd.get(d) is not None:
            cur["usdhkd"] = usdhkd[d]
        series.append({"dt": d, **cur})
    out["series"] = series
    out["as_of"] = series[-1]["dt"] if series else None
    return out


# ── 引擎C：宏观流动性数据源（T-C1，市场级条件变量）──────────────

# 12 指标元数据（红线 3：权威发布机构逐指标登记，随 meta 落盘 + provenance）
# T-R9 对应关系：本表 = datasets 注册表中 macro_liquidity 数据集（carry_old）的
# per-metric 拉取口径元信息（name/unit/freq/source/upstream/lag_note 是数据侧属性，
# 非维度声明，故不搬入 spec）；维度归属/启停/打分器在引擎注册表（factors/registry.json）。
_MACRO_META: dict[str, dict] = {
    "m2_yoy": {"name": "M2 同比", "unit": "%", "freq": "月",
               "source": "ak.macro_china_money_supply()", "upstream": "中国人民银行",
               "lag_note": "月度，次月中旬发布"},
    "tsf_new_12m": {"name": "社融 12 个月滚动增量", "unit": "亿元", "freq": "月",
                    "source": "ak.macro_china_shrzgm() 12月滚动求和", "upstream": "中国人民银行",
                    "lag_note": "月度，次月中旬发布"},
    "shibor_1m": {"name": "Shibor 1M", "unit": "%", "freq": "日",
                  "source": "ak.rate_interbank(上海银行同业拆借市场/Shibor人民币/1月)",
                  "upstream": "全国银行间同业拆借中心", "lag_note": "每日 11:00 发布"},
    "dr007": {"name": "DR007（存款类机构质押式回购定盘利率）", "unit": "%", "freq": "日",
              "source": "ak.repo_rate_hist() FDR007 列", "upstream": "全国银行间同业拆借中心",
              "lag_note": "每日收盘后发布"},
    "repo_tier_007": {"name": "回购分层利差（FR007−FDR007）", "unit": "百分点", "freq": "日",
                      "source": "ak.repo_rate_hist() FR007−FDR007 同日相减",
                      "upstream": "全国银行间同业拆借中心", "lag_note": "每日收盘后发布"},
    "m1_m2_gap": {"name": "M1−M2 同比剪刀差（资金活化）", "unit": "百分点", "freq": "月",
                  "source": "ak.macro_china_money_supply() 货币(M1)−货币和准货币(M2) 同比同月相减",
                  "upstream": "中国人民银行", "lag_note": "月度，次月中旬发布"},
    "lpr_1y": {"name": "LPR 1Y", "unit": "%", "freq": "月",
               "source": "ak.macro_china_lpr()", "upstream": "全国银行间同业拆借中心",
               "lag_note": "每月 20 日（节假日顺延）发布"},
    "rrr_big": {"name": "大型金融机构存款准备金率（调整后）", "unit": "%", "freq": "事件",
                "source": "ak.macro_china_reserve_requirement_ratio()", "upstream": "中国人民银行",
                "lag_note": "不定期（央行公告）"},
    "cn_us_10y": {"name": "中美 10Y 国债利差（美−中）", "unit": "百分点", "freq": "日",
                  "source": "ak.bond_zh_us_rate() 美债10Y−中债10Y 同日相减",
                  "upstream": "中债/美国财政部", "lag_note": "交易日更新"},
    "us_10y_2y": {"name": "美债 10Y-2Y 利差", "unit": "百分点", "freq": "日",
                  "source": "ak.bond_zh_us_rate()", "upstream": "美国财政部",
                  "lag_note": "交易日更新"},
    "fed_target": {"name": "美联储目标利率（下限）", "unit": "%", "freq": "事件",
                   "source": "ak.macro_bank_usa_interest_rate()（前值兜底）", "upstream": "美联储",
                   "lag_note": "FOMC 议息会议决议"},
    "us_cpi_yoy": {"name": "美国 CPI 同比", "unit": "%", "freq": "月",
                   "source": "ak.macro_usa_cpi_yoy()（时间/现值）", "upstream": "美国劳工统计局（BLS）",
                   "lag_note": "月度，次月中旬发布"},
    "us_cpi_surprise": {"name": "美国 CPI 月率预期差（实际−预期）", "unit": "百分点", "freq": "月",
                        "source": "ak.macro_usa_cpi_monthly() 美国CPI月率 今值−预测值",
                        "upstream": "美国劳工统计局（BLS）", "lag_note": "月度，次月中旬发布"},
    "us_nonfarm": {"name": "美国非农新增就业", "unit": "万人", "freq": "月",
                   "source": "ak.macro_usa_non_farm()", "upstream": "美国劳工统计局（BLS）",
                   "lag_note": "月度，次月首个周五发布"},
    "us_gdp_qoq": {"name": "美国 GDP 环比折年率", "unit": "%", "freq": "月",
                   "source": "ak.macro_usa_gdp_monthly()", "upstream": "美国经济分析局（BEA）",
                   "lag_note": "月度推算/季度核算"},
}


def _month_end_cn(x) -> str | None:
    """中文/紧凑月标签 → 当月最后一天 ISO（'2026年09月份'/'202603' → '2026-09-30'）。"""
    m = re.search(r"(\d{4})年(\d{1,2})月", str(x)) or re.fullmatch(r"(\d{4})(\d{2})", str(x).strip())
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    if not 1 <= mo <= 12:
        return None
    if mo == 12:
        return dt.date(y, 12, 31).isoformat()
    return (dt.date(y, mo + 1, 1) - dt.timedelta(days=1)).isoformat()


def _cn_date(x) -> str | None:
    """中文日期 → ISO（'2007年02月25日' → '2007-02-25'）。"""
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", str(x))
    return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat() if m else None


def fetch_macro_liquidity(start_date: str = "2021-01-01") -> dict:
    """宏观流动性 15 指标（引擎C 数据源，落盘 ``market/macro_liquidity.json``）。

    - A股 9：M2 同比 / 社融 12 月滚动增量 / Shibor 1M / DR007 / 回购分层利差
      （FR007−FDR007）/ M1−M2 剪刀差 / LPR 1Y / 大型机构存准率 / 中美 10Y 利差
    - 美股 4 + 背景板 2：Fed 目标利率 / 美 10Y-2Y 利差 / 美国 CPI 同比 / CPI 预期差 /
      非农新增 / GDP 月率；港股维度**不在此拉取**（引擎C 计算层直接读 H-0006 的
      ``market/hk_rates.json``，避免重复请求）
    - 逐指标独立容错：单源失败 → 该 series 置 []、meta 记 error，绝不抛异常（红线 2）

    返回 ``{"as_of", "series": {15 键: [{dt, v}]}, "meta", "start", "error"}``；
    series 皆升序、≥ start_date 过滤；dt 统一 ISO。
    """
    import akshare as ak

    out: dict = {"as_of": None, "series": {}, "meta": {}, "start": start_date, "error": None}
    s: dict[str, list] = {k: [] for k in _MACRO_META}
    meta: dict[str, dict] = {k: dict(v) for k, v in _MACRO_META.items()}

    def _run(key: str, fn) -> None:
        """单指标容错执行：解析 [(dt, v)] → 升序、≥start 过滤、落 series；失败 meta 记 error。"""
        try:
            rows = sorted((d, v) for d, v in fn() if d and v is not None and d >= start_date)
            s[key] = [{"dt": d, "v": v} for d, v in rows]
        except Exception as exc:                # noqa: BLE001 - 单源失败不阻断其余指标
            meta[key]["error"] = f"{type(exc).__name__}: {exc}"

    # ── A股 9 ──
    money_cache: list | None = None

    def _money():
        nonlocal money_cache
        if money_cache is None:
            money_cache = list(_retry(lambda: ak.macro_china_money_supply()).iterrows())
        return money_cache

    _run("m2_yoy", lambda: [(_month_end_cn(r["月份"]), _num(r["货币和准货币(M2)-同比增长"]))
                            for _, r in _money()])
    # M1−M2 剪刀差：同一次拉取派生（同月相减；任一侧缺失该月不落点）
    _run("m1_m2_gap", lambda: [(_month_end_cn(r["月份"]), round(m1 - m2, 4))
                               for _, r in _money()
                               if (m1 := _num(r["货币(M1)-同比增长"])) is not None
                               and (m2 := _num(r["货币和准货币(M2)-同比增长"])) is not None])

    def _tsf_12m():
        """社融增量 12 个月滚动和（信贷脉冲底座；全量窗口计算后由 _run 按 start 过滤）。"""
        df = _retry(lambda: ak.macro_china_shrzgm())
        by: dict[str, float | None] = {}
        for _, r in df.iterrows():
            d = _month_end_cn(r["月份"])
            if d:
                by[d] = _num(r["社会融资规模增量"])       # 同月重复以最后一条为准
        mon = sorted(by)
        rows = []
        for i, d in enumerate(mon):
            win = [by[m] for m in mon[max(0, i - 11): i + 1]]
            if len(win) == 12 and all(x is not None for x in win):
                rows.append((d, round(sum(win), 1)))
        return rows

    _run("tsf_new_12m", _tsf_12m)
    _run("shibor_1m", lambda: [(_iso(r["报告日"]), _num(r["利率"]))
                               for _, r in _retry(lambda: ak.rate_interbank(
                                   market="上海银行同业拆借市场",
                                   symbol="Shibor人民币", indicator="1月")).iterrows()])

    # 回购利率（DR007 / 分层利差：一次拉取两指标共用，各自容错）
    repo_cache: list | None = None

    def _repo():
        nonlocal repo_cache
        if repo_cache is None:
            # 上游单次请求仅可靠返回约一年内数据（更长区间解析报错）→ 按年分片合并
            this_year = dt.date.today().year
            rows: list = []
            for y in range(2020, this_year + 1):
                ed = dt.date.today().strftime("%Y%m%d") if y == this_year else f"{y}1231"
                try:
                    rows += list(_retry(lambda sd=f"{y}0101", ed=ed: ak.repo_rate_hist(
                        start_date=sd, end_date=ed)).iterrows())
                except Exception:              # noqa: BLE001 - 单年分片失败透明降级
                    print(f"  ⚠ repo_rate_hist {y} 年分片失败，跳过")
            rows.sort(key=lambda t: str(t[1].get("date")))
            repo_cache = rows
        return repo_cache

    _run("dr007", lambda: [(_iso(r["date"]), _num(r["FDR007"]))
                           for _, r in _repo() if _num(r["FDR007"]) is not None])
    _run("repo_tier_007", lambda: [(_iso(r["date"]), round(fr - fdr, 4))
                                   for _, r in _repo()
                                   if (fr := _num(r["FR007"])) is not None
                                   and (fdr := _num(r["FDR007"])) is not None])
    _run("lpr_1y", lambda: [(_iso(r["TRADE_DATE"]), _num(r["LPR1Y"]))
                            for _, r in _retry(lambda: ak.macro_china_lpr()).iterrows()])

    def _rrr_big():
        df = _retry(lambda: ak.macro_china_reserve_requirement_ratio())
        col_d = "生效时间" if "生效时间" in df.columns else "公布时间"   # 事件表：生效优先于公布
        # 原表新→旧排序（尾部最老），解析后按 dt 升序重排
        return sorted(((_cn_date(r[col_d]), _num(r["大型金融机构-调整后"]))
                       for _, r in df.iterrows()), key=lambda t: t[0] or "")

    _run("rrr_big", _rrr_big)

    # ── 中美国债利率（一次拉取两指标共用，各自容错）──
    bond_cache: list | None = None

    def _bond():
        nonlocal bond_cache
        if bond_cache is None:
            bond_cache = list(_retry(lambda: ak.bond_zh_us_rate()).iterrows())
        return bond_cache

    _run("cn_us_10y", lambda: [(_iso(r["日期"]), round(u - c, 4))
                               for _, r in _bond()
                               if (u := _num(r["美国国债收益率10年"])) is not None
                               and (c := _num(r["中国国债收益率10年"])) is not None])
    _run("us_10y_2y", lambda: [(_iso(r["日期"]), v)
                               for _, r in _bond()
                               if (v := _num(r["美国国债收益率10年-2年"])) is not None])

    def _fed_target():
        rows = []
        for _, r in _retry(lambda: ak.macro_bank_usa_interest_rate()).iterrows():
            d, v = _iso(r["日期"]), _num(r["今值"])
            if v is None:                       # 今值缺失（未更新）→ 前值兜底（H-0006 同款）
                v = _num(r["前值"])
            rows.append((d, v))
        return rows

    _run("fed_target", _fed_target)

    # ── 美国东财宏观表（按 商品 列过滤主序列；同接口多指标共用一次拉取）──
    _usa_cache: dict[str, list | Exception] = {}

    def _usa_main(fn, keys: list[str], label: str):
        """按 商品 过滤出主序列并返回 [(dt, 今值, 预测值)]；精确匹配优先，子串次之。

        接口失败缓存原异常（同接口其余指标同样失败、各自记 error）；商品未命中时
        打印实际候选（便于登记口径）并返回空序列。
        """
        cache_key = fn                                # 函数对象可哈希，同接口共享缓存
        if cache_key not in _usa_cache:
            try:
                _usa_cache[cache_key] = list(_retry(fn).iterrows())
            except Exception as exc:            # noqa: BLE001 - 缓存异常供同接口各指标复抛
                _usa_cache[cache_key] = exc
        val = _usa_cache[cache_key]
        if isinstance(val, Exception):
            raise val
        products = {str(r.get("商品")) for _, r in val}
        target = next((p for k in keys for p in products if k == p), None) \
            or next((p for k in keys for p in products if k in p), None)
        if target is None:
            print(f"  ⚠ {label}：商品列未匹配 {keys}，实际候选={sorted(products)[:8]}")
            return []
        return [(_iso(r.get("日期")), _num(r.get("今值")), _num(r.get("预测值")))
                for _, r in val if str(r.get("商品")) == target]

    # CPI 同比：独立接口（时间/现值，无预测值）；预期差用月率接口（美国CPI月率 今值−预测值）
    _run("us_cpi_yoy", lambda: [(_iso(r["时间"]), _num(r["现值"]))
                                for _, r in _retry(lambda: ak.macro_usa_cpi_yoy()).iterrows()])
    _run("us_cpi_surprise", lambda: [(d, round(v - f, 4))
                                     for d, v, f in _usa_main(ak.macro_usa_cpi_monthly,
                                                              ["美国CPI月率"], "us_cpi_surprise")
                                     if v is not None and f is not None])
    _run("us_nonfarm", lambda: [(d, v) for d, v, _ in
                                _usa_main(ak.macro_usa_non_farm,
                                          ["美国非农就业人数"], "us_nonfarm")])
    _run("us_gdp_qoq", lambda: [(d, v) for d, v, _ in
                                _usa_main(ak.macro_usa_gdp_monthly,
                                          ["美国国内生产总值(GDP)"], "us_gdp_qoq")])

    out["series"], out["meta"] = s, meta
    all_dts = [r["dt"] for rows in s.values() for r in rows]
    out["as_of"] = max(all_dts) if all_dts else None
    if not any(s.values()):
        out["error"] = "宏观流动性 15 指标全部拉取失败（详见 meta 各指标 error）"
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


# ── 增量合并（T-fix：增量窗口不再整体覆盖历史）──────────────────

# 可合并数据集 → repo 相对路径（快照/合并面；其余数据集全量重写语义不变）
# T-R9：由数据集注册表派生（声明即文档）——新增数据集只改 applib/datasets/ 的 spec，
# 此处自动接管；replace/registry_merge 有各自写入口（全量重写 / T-R6 merge_registry），不入快照面。
_MERGEABLE: dict[str, str] = {s.name: s.path for s in _active_specs()
                              if s.merge_policy not in NON_SNAPSHOT_POLICIES}


def _snapshot_mergeable(repo: Path) -> dict[str, dict]:
    """rmtree 前快照既有 repo 的可合并数据集（缺文件/损坏 → 空 dict，绝不阻断）。

    T-R10：数据集清单直接走 active_specs()（merge_policy 可合并者；
    replace/registry_merge 有各自写入口，不入快照面）。
    """
    out: dict[str, dict] = {}
    for s in _active_specs():
        if s.merge_policy in NON_SNAPSHOT_POLICIES:
            continue
        try:
            out[s.name] = json.loads((repo / s.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out[s.name] = {}
    return out


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
    # M7 T-F1 派生科目（原始科目可得即合成；部分缺失按可得项求和，全缺 → None）：
    # ebit = 利润总额 + 财务费用（新浪利润表无 EBIT 直列；财务费用为净支出口径，利息收入未抵减——
    #        近似口径如实声明，T-F2 消费方按「ebit 近似」登记）；
    # interest_debt = 短期借款 + 长期借款 + 应付债券 + 一年内到期非流动负债（四项可得者相加）。
    for d in out.values():
        tp, fe = d.get("total_profit"), d.get("finance_expense")
        d["ebit"] = None if (tp is None and fe is None) else (tp or 0) + (fe or 0)
        debts = [d.get(k) for k in ("short_borrow", "long_borrow", "bonds_payable", "current_mature_debt")]
        debts = [v for v in debts if v is not None]
        d["interest_debt"] = sum(debts) if debts else None
    return out


def _resolve_mode(mode: str | None, incremental_from) -> str:
    """fetch_all 拉取模式解析（T-R10）：显式 mode 校验 + 兼容层推导。

    - mode=None（缺省，web core data_sync.py 既有调用路径）→ 从参数推导：
      incremental_from 非空 → "incremental"，否则 "bootstrap"（= 改造前行为）；
    - 显式 mode ∈ {bootstrap, incremental, refresh}；非法 → ValueError（中文）。
    """
    if mode is None:
        return "incremental" if incremental_from else "bootstrap"
    if mode not in ("bootstrap", "incremental", "refresh"):
        raise ValueError(f"mode 非法：{mode!r}（可选 bootstrap | incremental | refresh）")
    return mode


def _reg_spec_lookup(dataset: str, relpath: str,
                     interface: str | None, upstream: str | None) -> tuple[str | None, str | None]:
    """_reg 的 provenance 登记口径解析（T-R11）。

    - spec 查询（datasets/ 声明即文档）：get_spec_safe 先按溯源名、未命中按 relpath；
    - 手工参数优先（spec 未覆盖的新数据集兼容口），未注册 → WARNING + 手工照常登记
      （透明降级红线：不得因注册表缺项阻断同步）。
    """
    spec = get_spec_safe(dataset, relpath)
    if spec is None:
        _log.warning("数据集 %s 未在 datasets/ 注册，走手工登记", dataset)
        return interface, upstream
    return interface or spec_primary_interface(spec), upstream or spec_primary_upstream(spec)


def fetch_all(ctx, start: str = "2021-01-01", fin_start_year: str = "2019",
              universe=None, incremental_from=None, mode: str | None = None,
              progress=None, job_id: str | None = None, allow_rebuild: bool = False) -> dict:
    """拉取行情/财务/分红/日历 → remote/repo（云端替身）。

    T0.3 参数化（默认行为与历史逐字节一致）：
    - universe=None       → 默认全量 CN4 + MULTI5（现行为不变）
    - universe=[secid,…]  → 仅拉取这些 secid（在 CN4+MULTI5 定义中按 secid 匹配；
                            未匹配的 secid 跳过并在返回体 universe_missing 中登记）
    - incremental_from={secid: "YYYY-MM-DD"} → 该 secid 行情自「次日」起增量拉取；
                            未列出的 secid 仍按 start 全量

    T-R10 模式显式化（mode=None 时从 incremental_from 推导，兼容层零改动）：
    - bootstrap   重建 + 全量拉取（忽略 incremental_from）
    - incremental 快照既有可合并数据集 + 各 secid 增量窗口 + apply_policy 合并
    - refresh     重建 + 指定窗口重拉（universe/secid 必填，不合并——强制重取）

    ``allow_rebuild``：显式重建授权。调用方持有**权威全集**（如 Web core 从 PG 取
    的 securities 全集，含 applib 内置清单外的标的）时置 True，放行 bootstrap/refresh
    的「显式 universe 子集」路径——守卫默认拒绝是为了拦住 CLI ``ap fetch --secid``
    误把 56 标的快照压成 1 标的；core 的 bootstrap 是全量重建授权场景，非误操作。
    """
    mode = _resolve_mode(mode, incremental_from)
    if mode == "refresh" and not universe:
        raise ValueError("refresh 模式必须提供 secid（universe=[secid, …]）")
    if mode != "incremental":          # bootstrap/refresh：忽略 incremental_from（全量/窗口重拉）
        incremental_from = None
    # 破坏性重写守卫（T-F10 收口）：bootstrap/refresh = rmtree 重建 + 仅写请求子集——
    # 子集 < 现存快照标的时，未列标的将整体丢失（T-F9 演练 `ap fetch --secid 00700.HK`
    # 首次踩中：56 标的快照被覆盖为 1 标的）。除全量 bootstrap（universe=None）外一律
    # 显式拒绝并指引 incremental 合并路径；CLI --secid 缺省模式已在命令层升级为 incremental。
    if mode in ("bootstrap", "refresh") and universe is not None and not allow_rebuild:
        raise ValueError(
            "破坏性重写守卫：bootstrap/refresh 会重建 repo 且仅写请求子集"
            f"（{len(universe) if hasattr(universe, '__len__') else '?'} 个 secid），"
            "未列标的数据将丢失——子集补拉请使用 mode=incremental"
            "（CLI：ap fetch --secid … 缺省即增量合并，或显式 --mode incremental）"
        )
    import akshare as ak
    today = dt.date.today().isoformat()
    repo = ctx.repo_dir
    # 增量合并（T-fix）：incremental 模式先快照既有可合并数据集，拉取完成后按
    # spec.merge_policy 分派合并（T-R10：语义真源在 datasets/merge.py）。
    # 旧行为：rmtree 后仅写增量窗口 → 历史丢失、空窗口 → 空快照，六项验证全红。
    merge_base = _snapshot_mergeable(repo) if mode == "incremental" else None
    # T-R6 写保护：注册表随下方 rmtree 整体消失——必须先原样取走（v1/v2/损坏均容忍），
    # 尾部 merge_registry 以此为「盘上用户改动」参与合并。T-R12 总验收实测回归：
    # 若 rmtree 后才读盘，用户的指标增删改会在每次同步后丢失。
    from .engines.base import load_registry_raw
    pre_registry = load_registry_raw(repo)
    if repo.exists():
        import shutil
        shutil.rmtree(repo)               # 真实数据切换：整体重建云端 main（α）
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
                "carry_hk_us": "港美息差（H-0006）= HIBOR 1M − 美联储目标利率（百分点）：越低（越负）"
                               "→ 美元资产相对港元资产利差越阔 → 套息外流压力越大；按 as_of 前向填充",
                "peg_pressure": "弱方兑换保证压力（H-0006）= (USDHKD − 7.75)/0.10：0=强方保证，1=弱方保证；"
                                "USDHKD 由中行折算价 USDCNY/HKDCNY 交叉；越高 → 港元越弱、外流压力越大",
            "note": "永新=永新股份(002014.SZ)；cn4 未提供成本与权重 → 等权名义组合(每标的25%)；"
                    "multi5 五资产等权 20%×5，纳指以纳指100ETF(513100)近似（A股无综指ETF）；"
                    "V4-β 支持 A股/港股/美股 混合 universe（按 secid 后缀 .SH/.SZ/.HK/.US 分类）"}

    def _reg(dataset, params, rows, relpath, interface=None, upstream=None):
        # T-R11：登记口径收敛为 spec 查询（解析逻辑见 _reg_spec_lookup）；provenance
        # sources[] 外部结构与 sha256 口径完全不变（provenance_check 契约）。
        interface, upstream = _reg_spec_lookup(dataset, relpath, interface, upstream)
        p = repo / relpath
        (repo / relpath).parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8") if relpath.endswith(".json") else None
        prov["sources"].append({"dataset": dataset, "interface": interface, "upstream": upstream,
                                "params": params, "file": relpath, "sha256": _sha256_file(p),
                                "bytes": p.stat().st_size})

    # ── ① 交易日历 ──
    cal = _retry(lambda: ak.tool_trade_date_hist_sina())
    dates = [_iso(d) for d in cal["trade_date"].tolist()]
    _reg("dwd_trade_calendar", {}, dates, "market/trade_calendar.json")

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
        us_rows = [r for _, r in raw.iterrows()
                   if _iso(r["date"]) and _iso(r["date"]) >= s0]
        sina_raw = [{"dt": _iso(r["date"]), "close": _num(r["close"])} for r in us_rows]
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
            if us_rows:
                # 新浪 raw 含完整 OHLCV，降级主序列据此重建（原样式仅取 close → high/low 缺失，
                # 使引擎 B 高点/低点窗口 max/min 空集报错）
                recs_fallback, prev_f = [], None
                by_dt = {_iso(r["date"]): r for r in us_rows}
                for d in sorted(by_dt):
                    row = by_dt[d]
                    c = _num(row["close"])
                    v = _num(row["volume"])
                    recs_fallback.append({"dt": d, "open": _num(row["open"]), "high": _num(row["high"]),
                                          "low": _num(row["low"]), "close": c,
                                          "volume": int(v) if v is not None else None,
                                          "amount": None,
                                          "pct": (c / prev_f - 1) if (prev_f and c) else None})
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

    # ── ②'''' 增量合并（T-fix → T-R10 策略化）：旧快照 + 新拉取按 merge_policy 分派 ──
    # 语义真源在 datasets/merge.py（逐行为对拍迁移）：行情类按 dt 并集（重叠日新胜 +
    # 窗口首根 pct 边界修复）；滚动窗口（腾讯交叉源）新非空用新、否则携带旧；
    # adjust_ex_dates 取并集；财务仅补缺失 secid（全量重写不降级）。
    # 效果：增量窗口为空/个别标的拉取失败 → 旧数据完整保留，验证状态不回退。
    if merge_base is not None:
        bars_qfq = apply_policy(get_spec("daily_bars"), merge_base.get("daily_bars"), bars_qfq)
        bars_raw = apply_policy(get_spec("daily_bars_raw"), merge_base.get("daily_bars_raw"), bars_raw)
        cross_tx = apply_policy(get_spec("cross_check_tx"), merge_base.get("cross_check_tx"), cross_tx)
        adjust_ex_dates = apply_policy(get_spec("adjust_ex_dates"),
                                       merge_base.get("adjust_ex_dates"), adjust_ex_dates)
        fundamentals = apply_policy(get_spec("fundamental_values"),
                                    merge_base.get("fundamental_values"), fundamentals)

    _reg("dwd_daily_bar",
         {"start": start, "end": today, "note": "A股/港股 qfq 前复权；美股 腾讯序列（raw 口径，新浪美股qfq部分标的存在噪声，弃用）；ETF/LOF 无复权接口（raw）"}, bars_qfq, "market/daily_bars.json")
    _reg("dwd_daily_bar_raw",
         {"start": start, "end": today, "note": "A股/港股/美股股票 raw（基金无复权对，由 cross_source 双源覆盖）；美股 raw 兼作腾讯主序列的独立对账源"}, bars_raw, "market/daily_bars_raw.json")
    _reg("cross_check_tx",
         {"window_days": 120, "note": "A股（股票与基金）走 stock_zh_a_hist_tx；港股 hk{code}、美股 us{TICKER}.OQ/.N/.A 走腾讯K线接口"}, cross_tx, "market/cross_check_tx.json")
    if adjust_ex_dates:
        # 港股/美股除权除息日（腾讯 qfq 事件元数据 FHcontent/ggContent 的 cqr）：
        # adjust_check 经典 qfq-vs-raw 对账的豁免名单（回购 HGcontent 不影响复权，不计入）
        _reg("adjust_ex_dates",
             {"note": "港股/美股除权除息日（分红/送转/拆股），供 adjust_check 豁免"},
             adjust_ex_dates, "market/adjust_ex_dates.json")
    _reg("dwd_financial_report",
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
    _reg("dim_ggt_components",
         {"source": ggt_source}, {"codes": ggt_codes, "source": ggt_source,
                                  "count": len(ggt_codes)}, "market/ggt_components.json")

    # 总市值（P0.3 敞口报告规模维度；仅 A 股股票适用）
    market_cap = _fetch_market_cap(ak, a_stocks, start)
    if merge_base is not None:            # 增量合并：拉取失败的标的携带旧序列（fill_missing）
        market_cap = apply_policy(get_spec("market_cap"),
                                  merge_base.get("market_cap"), market_cap)
    if market_cap:
        _reg("dim_market_cap",
             {"start": start, "unit": "亿元",
              "note": "总市值历史序列（P0.3 敞口报告规模维度）；仅 A 股股票适用；"
                      "供月末再平衡时点取用（按最近可用点前向取值）"},
             market_cap, "dim/market_cap.json")

    # 港股/美股交易日历：本市场 K 线日期并集推导（A股日历来自新浪，见 ①）
    for mkt, group in (("hk", hk_stocks), ("us", us_stocks)):
        days = sorted({r["dt"] for u in group for r in bars_qfq.get(u["secid"], []) if r["dt"]})
        if days:
            _reg(f"dwd_trade_calendar_{mkt}",
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
    # 因子注册表（T-R6 写保护 merge）：代码内置 defaults ⊕ 盘上用户改动——
    # 用户对 status/params/权重/锚点/启停的增删改在数据同步后完整保留（盘上独有条目
    # 标 orphan 不删）；盘上 v1（现网旧格式）仅继承 status；空盘/损坏 → 全量
    # defaults（等价现状）。注意「盘上」= rmtree 前快照 pre_registry（见函数头部）。
    from .engines.base import export_registry_v2, merge_registry
    merged_registry = merge_registry(pre_registry, export_registry_v2())
    (repo / "factors" / "registry.json").write_text(
        json.dumps(merged_registry, ensure_ascii=False, indent=2), encoding="utf-8")
    (repo / "market" / "fx.json").write_text(json.dumps({
        **fetch_fx(),
        "note": "V4-β 多市场组合需要：港元/美元持仓按 HKD_CNY/USD_CNY 折算人民币口径；"
                "港股通标的实际以港币报价、人民币结算（存在汇率与结算价差）"}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # 港美利率/汇率（carry_hk_us / peg_pressure，H-0006）：universe 含港股时拉取 regime 数据源
    if hk_stocks:
        hk_rates = fetch_hk_rates(start_date=start)
        if hk_rates.get("series"):
            _reg("macro_hk_rates",
                 {"start": start, "as_of": hk_rates["as_of"]},
                 hk_rates, "market/hk_rates.json")
        else:
            print(f"  ⚠ 港美利率序列拉取失败（carry_hk_us/peg_pressure 数据不可用）：{hk_rates.get('error')}")

    # 宏观流动性 12 指标（引擎C 市场级条件变量；与持仓 universe 无关，恒拉取）
    macro = fetch_macro_liquidity(start_date=start)
    if merge_base is not None:              # 增量：失败/空指标携带旧值（月频窗口常为空，防丢历史）
        macro = apply_policy(get_spec("macro_liquidity"),
                             merge_base.get("macro_liquidity"), macro)
    if macro.get("series") and any(macro["series"].values()):
        _reg("macro_liquidity",
             {"start": start, "as_of": macro["as_of"],
              "note": "引擎C 宏观流动性指标（市场级条件变量）；逐指标独立容错，单源失败仅该指标缺失"},
             macro, "market/macro_liquidity.json")
    else:
        print(f"  ⚠ 宏观流动性指标拉取失败（引擎C 数据不可用）：{macro.get('error')}")

    # ── ④ 溯源登记（自哈希最后登记）──
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
            "per_secid": per_secid}
