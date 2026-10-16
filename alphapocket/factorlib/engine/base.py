"""引擎基类与输出规范化。"""
from __future__ import annotations

from datetime import date


class EngineError(Exception):
    """引擎执行/定义错误。"""


# 行：dt, secid, value（value=None 表示该截面无值，物化层跳过）；PIT 元数据可选（sql/qlib 自带）
Row = dict


def _to_date(d) -> date:
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def normalize_rows(rows: list[Row]) -> list[Row]:
    """规范化引擎输出：dt/used_period/used_announce 统一为 date；value 保留原值（None 合法）。"""
    out = []
    for r in rows:
        if not r.get("dt") or not r.get("secid"):
            raise EngineError(f"输出行缺少 dt/secid：{r!r}")
        out.append({
            "dt": _to_date(r["dt"]),
            "secid": str(r["secid"]),
            "value": r.get("value"),
            "used_period": _to_date(r["used_period"]) if r.get("used_period") else None,
            "used_announce": _to_date(r["used_announce"]) if r.get("used_announce") else None,
        })
    return out
