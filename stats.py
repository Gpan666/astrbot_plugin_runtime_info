# -*- coding: utf-8 -*-
"""真实读数聚合（纯标准库，不依赖 AstrBot，可单独在本地跑测试）。

数据源优先级：
  1. cost_control 补充库 ``plugin_data/astrbot_plugin_cost_control/supplement.db``
     表 ``cost_supplements``，含 cache_read / cache_creation 细分 —— 与 WebUI
     ``/cache`` 面板同口径，为准。
  2. AstrBot 主库 ``data_v4.db`` 的 ``provider_stats`` 表 —— 无缓存细分字段，
     命中率只能用 ``token_input_cached/(cached+other)`` 近似，作为兜底。

口径（与 cost_control/cache_diag.py 保持一致）：
  effective_cache_read = max(cache_read, token_input_cached)   # 聚合后再取 max
  hit_rate = eff / (eff + 非缓存输入 + cache_creation)
  只看 cache_read 会低报：部分 provider 回报 0/None 而宿主已解析出真实命中。

所有连接均为只读（mode=ro + query_only），不写库、不锁库。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CC_NAME = "astrbot_plugin_cost_control"
UTC = timezone.utc
CN_TZ = timezone(timedelta(hours=8))
FMT = "%Y-%m-%d %H:%M:%S"
WINDOW_HOURS = {"24h": 24, "today": 0, "weekly": 168, "all": 0, "7d": 168}


def effective_cache_read(cache_read: Any, input_cached: Any) -> int | None:
    vals = [int(v) for v in (cache_read, input_cached) if v is not None]
    return max(vals) if vals else None


def hit_rate(cache_read: Any, input_other: Any, cache_creation: Any) -> float:
    cr = int(cache_read or 0)
    io = int(input_other or 0)
    cc = int(cache_creation or 0)
    denom = cr + io + cc
    return -1.0 if denom <= 0 else round(cr * 100.0 / denom, 2)


def window_since(window: str, now: datetime | None = None) -> str | None:
    """返回 ISO 风格的 UTC 下界字符串（与 created_at 的文本存储可直接比较）。"""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if window == "today":
        local_midnight = now.astimezone(CN_TZ).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return local_midnight.astimezone(UTC).strftime(FMT)
    hours = WINDOW_HOURS.get(window, 24)
    if hours <= 0 and window != "all":
        hours = 24
    if window == "all" or hours <= 0:
        return None
    return (now - timedelta(hours=hours)).strftime(FMT)


def _pick_source(supplement_path: Any, main_db_path: Any) -> tuple[Path, str, bool] | None:
    """返回 (路径, 表名, 是否含缓存细分字段)。找不到返回 None。"""
    p = Path(supplement_path) if supplement_path else None
    if p and p.is_file():
        try:
            with sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=3) as c:
                names = {
                    r[0]
                    for r in c.execute(
                        "select name from sqlite_master where type='table'"
                    )
                }
            if "cost_supplements" in names:
                return p, "cost_supplements", True
        except Exception:  # noqa: BLE001
            pass
    p2 = Path(main_db_path) if main_db_path else None
    if p2 and p2.is_file():
        try:
            with sqlite3.connect(f"file:{p2}?mode=ro", uri=True, timeout=3) as c:
                names = {
                    r[0]
                    for r in c.execute(
                        "select name from sqlite_master where type='table'"
                    )
                }
            if "provider_stats" in names:
                return p2, "provider_stats", False
        except Exception:  # noqa: BLE001
            pass
    return None


def _expr(has_cache: bool) -> tuple[str, str]:
    """聚合列表达式与逐条有效命中表达式。"""
    if has_cache:
        cr = "coalesce(sum(cache_read),0)"
        cc = "coalesce(sum(cache_creation),0)"
        eff_row = (
            "max(coalesce(cache_read,0), coalesce(token_input_cached,0))"
        )
    else:
        cr = "0"
        cc = "0"
        eff_row = "coalesce(token_input_cached,0)"
    return cr, cc


def _stat_from_row(row: tuple, has_cache: bool, label: str | None = None) -> dict:
    """把 SELECT 结果统一成前端/工具共用的结构。

    行顺序固定为：
      [分组键(可选), n, io, ic, out, cc, cr, avg_rate, last_at]
    """
    if label is not None:
        n, io, ic, out, cc, cr, avg_rate, last_at = row[1:]
    else:
        n, io, ic, out, cc, cr, avg_rate, last_at = row
    io, ic, out = int(io or 0), int(ic or 0), int(out or 0)
    eff = effective_cache_read(cr, ic)
    out_d = {
        "requests": int(n or 0),
        "input_other": io,
        "input_cached": ic,
        "output": out,
        "cache_creation": int(cc or 0),
        "cache_read": int(cr or 0),
        "effective_read": eff,
        "total": io + ic + out,
        "hit_rate": hit_rate(eff, io, cc),
        "hit_rate_avg": round(float(avg_rate), 2) if avg_rate is not None else -1.0,
        "as_of": last_at,
    }
    if label is not None:
        out_d["name"] = str(label or "未知")
    return out_d


def collect(
    supplement_path: Any = None,
    main_db_path: Any = None,
    *,
    window: str = "24h",
    umo: str | None = None,
    now: datetime | None = None,
    recent_limit: int = 12,
    group_limit: int = 8,
) -> dict[str, Any]:
    """聚合一次完整读数。任何异常都返回 ok=False，不抛出。"""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    picked = _pick_source(supplement_path, main_db_path)
    if picked is None:
        return {
            "ok": False,
            "error": "找不到 cost_control 补充库，也没有主库 provider_stats 可兜底",
            "generated_at": now.isoformat(),
        }
    path, table, has_cache = picked
    cr_expr, cc_expr = _expr(has_cache)
    eff_row = (
        "max(coalesce(cache_read,0), coalesce(token_input_cached,0))"
        if has_cache
        else "coalesce(token_input_cached,0)"
    )
    avg_expr = (
        f"avg(case when ({eff_row} + coalesce(token_input_other,0) "
        f"+ {('coalesce(cache_creation,0)' if has_cache else '0')}) > 0 "
        f"then {eff_row} * 100.0 / ({eff_row} + coalesce(token_input_other,0) "
        f"+ {('coalesce(cache_creation,0)' if has_cache else '0')}) end)"
    )
    sums = (
        f"count(*), coalesce(sum(token_input_other),0), "
        f"coalesce(sum(token_input_cached),0), coalesce(sum(token_output),0), "
        f"{cc_expr}, {cr_expr}, {avg_expr}, max(created_at)"
    )

    since = window_since(window, now)
    where, args = [], []
    if since:
        where.append("created_at >= ?")
        args.append(since)
    if umo:
        where.append("umo = ?")
        args.append(umo)
    clause = (" where " + " and ".join(where)) if where else ""

    result: dict[str, Any] = {
        "ok": True,
        "source": "supplement" if has_cache else "provider_stats_fallback",
        "source_detail": str(path),
        "has_cache_detail": has_cache,
        "generated_at": now.isoformat(),
        "window": window,
        "scope": "session" if umo else "all",
        "umo": umo,
        "since": since,
    }

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.row_factory = None

            total = _stat_from_row(
                conn.execute(
                    f"select {sums} from {table}{clause}", args
                ).fetchone(),
                has_cache,
            )
            result.update(total)

            for key, col, limit in (
                ("by_model", "provider_model", group_limit),
                ("by_session", "umo", group_limit),
            ):
                rows = conn.execute(
                    f"select {col}, {sums} from {table}{clause} "
                    f"group by {col} order by count(*) desc limit ?",
                    [*args, limit],
                ).fetchall()
                result[key] = [
                    _stat_from_row(r, has_cache, r[0]) for r in rows
                ]

            # 时间趋势（UTC 日，倒序取回后反转成升序）
            trows = conn.execute(
                f"select substr(created_at,1,10) as d, {sums} from {table}{clause} "
                f"group by d order by d desc limit 14",
                args,
            ).fetchall()
            result["timeline"] = [
                dict(_stat_from_row(r, has_cache, r[0]), date=r[0])
                for r in reversed(trows)
            ]

            # 最近请求明细
            recent_cols = (
                "provider_model, umo, token_input_other, token_input_cached, "
                "token_output, "
                + ("cache_creation, cache_read, " if has_cache else "0, 0, ")
                + "created_at"
            )
            rrows = conn.execute(
                f"select {recent_cols} from {table}{clause} "
                f"order by created_at desc limit ?",
                [*args, recent_limit],
            ).fetchall()
            recent = []
            for r in rrows:
                model, s_umo, io, ic, out, cc, cr, ts = r
                eff = effective_cache_read(cr, ic)
                recent.append(
                    {
                        "at": ts,
                        "model": str(model or "未知"),
                        "umo": str(s_umo or ""),
                        "input_other": int(io or 0),
                        "input_cached": int(ic or 0),
                        "output": int(out or 0),
                        "effective_read": eff,
                        "hit_rate": hit_rate(eff, io, cc),
                    }
                )
            result["recent"] = recent

            # 会话下拉（不受 window / scope 限制）
            srows = conn.execute(
                f"select umo, count(*) from {table} group by umo "
                f"order by count(*) desc limit 20"
            ).fetchall()
            result["sessions"] = [
                {"umo": str(r[0] or ""), "requests": int(r[1] or 0)}
                for r in srows
            ]

            last = recent[0] if recent else None
            if last:
                result["last"] = {
                    "at": last["at"],
                    "model": last["model"],
                    "hit_rate": last["hit_rate"],
                    "input_other": last["input_other"],
                    "input_cached": last["input_cached"],
                }
            else:
                result["last"] = None
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"读数查询失败：{type(exc).__name__}: {exc}",
            "generated_at": now.isoformat(),
            "source_detail": str(path),
        }

    result["note"] = (
        "命中率 = 有效缓存命中输入 /(有效缓存命中输入 + 非缓存输入 + 缓存写入)；"
        "有效命中取 max(cache_read, token_input_cached)。"
        "用量在每轮响应结束后才入库，本轮尚未计入。"
    )
    return result


def format_for_llm(data: dict[str, Any], model_info: tuple | None) -> str:
    """把读数格式化为给模型看的文本（带口径，避免把累计说成本轮）。"""
    lines: list[str] = []
    if model_info:
        pid, ptype, model = model_info
        lines.append(f"当前会话实际模型：{model}")
        lines.append(f"供应商实例 ID：{pid} · 接口类型：{ptype}")
    else:
        lines.append("当前会话实际模型：暂时读不到，如实说明，不要猜型号。")

    if not data.get("ok"):
        lines.append(f"用量统计暂时读不到（{data.get('error', '未知原因')}）。")
        lines.append("如实说读不到，不要编造任何数值。")
        return "\n".join(lines)

    win = {"24h": "近 24 小时", "today": "今天", "weekly": "近 7 天", "all": "全部历史", "7d": "近 7 天"}.get(
        data.get("window", "24h"), "所选窗口"
    )
    scope = "当前会话" if data.get("scope") == "session" else "全部会话"
    t = data
    lines.append(
        f"{win}·{scope}（截至 {t.get('as_of') or '—'} UTC，已完成 {t.get('requests', 0):,} 次请求）："
    )
    lines.append(
        f"非缓存输入 {t.get('input_other', 0):,} · 缓存命中输入 {t.get('input_cached', 0):,} "
        f"· 输出 {t.get('output', 0):,} · 合计 {t.get('total', 0):,} token"
    )
    rate = t.get("hit_rate", -1)
    lines.append(
        f"真实缓存命中率 {rate:.1f}%" if rate >= 0 else "缓存命中率：本窗口无数据"
    )
    last = t.get("last")
    if last and last.get("hit_rate", -1) >= 0:
        lines.append(
            f"最近一次已完成请求命中率 {last['hit_rate']:.1f}%"
            f"（{last.get('model')}，{last.get('at')} UTC）"
        )
    lines.append("口径：" + t.get("note", ""))
    lines.append("注意：以上全部是已完成请求的历史读数，不含本轮正在生成的回复；"
                 "回答时不要说成「本次/这轮用了多少」。")
    return "\n".join(lines)
