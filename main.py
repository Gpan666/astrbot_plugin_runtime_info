# -*- coding: utf-8 -*-
"""astrbot_plugin_runtime_info —— 让 AI 查到自己的真实型号 / token 用量 / 缓存命中，
并提供一个 miuix 风格的 Web 仪表盘。

设计约束（针对本机器人的既有改造）：
  1. 工具按需调用：只在模型被问到时查库，不每轮查、不往 system_prompt 注入动态内容，
     避免把 prompt cache 打散（此前 prompt_cache_plugin 的改造）。
  2. on_llm_request 只做「抓型号挂到 event」，不修改 req.system_prompt / contexts / func_tool。
  3. 读数口径与 cost_control 的 WebUI /cache 面板一致：
     eff = max(cache_read, token_input_cached)，
     hit_rate = eff /(eff + 非缓存输入 + cache_creation)。
  4. 所有数据库访问只读（mode=ro + query_only），跨插件只依赖文件路径，不 import 对方代码。

Web 入口（两种，页面是同一份自包含 HTML）：
  A. AstrBot WebUI → 插件 → 运行信息 → 页面（pages/dashboard/index.html，bridge 自动带 JWT）
  B. 浏览器直接访问 http://<host>:<port>/api/plug/astrbot_plugin_runtime_info/page
     （需已登录 AstrBot WebUI；数据接口 /api/plug/astrbot_plugin_runtime_info/stats）
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star import Star
from astrbot.core.star.context import Context

try:  # 包内导入（AstrBot 以 package 方式加载插件）
    from . import stats as _stats
except ImportError:  # 兼容作为顶层模块加载 / 本地单测
    import stats as _stats  # type: ignore

PLUGIN_NAME = "astrbot_plugin_runtime_info"
CAPTURE_KEY = "_runtime_info_provider"
ROOT = Path(__file__).resolve().parent


class RuntimeInfoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config or {}

    # ------------------------------------------------------------ 生命周期
    async def initialize(self):
        self._ensure_plugin_page()
        self._register_routes()

    def _ensure_plugin_page(self) -> None:
        """单源维护：web/index.html 是唯一源文件，启动时同步到 pages/dashboard/。

        失败只记日志，不影响插件其它能力（用户也可手动放一份）。
        """
        if self.cfg.get("enable_page_sync", True) is False:
            return
        try:
            src = ROOT / "web" / "index.html"
            if not src.is_file():
                return
            dst = ROOT / "pages" / "dashboard" / "index.html"
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists() or dst.read_bytes() != src.read_bytes():
                shutil.copyfile(src, dst)
                logger.info(f"[{PLUGIN_NAME}] 已同步 Plugin Page → {dst}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 同步 Plugin Page 失败：{exc}")

    def _register_routes(self) -> None:
        reg = getattr(self.context, "register_web_api", None)
        if not callable(reg):
            logger.warning(f"[{PLUGIN_NAME}] 当前版本不支持 register_web_api，Web UI 不可用")
            return
        routes = [
            (f"/{PLUGIN_NAME}/stats", self.api_stats, ["GET"], "真实读数聚合"),
            (f"/{PLUGIN_NAME}/page", self.api_page, ["GET"], "miuix 风格仪表盘页面"),
            (f"/{PLUGIN_NAME}/health", self.api_health, ["GET"], "插件自检"),
        ]
        for route, handler, methods, desc in routes:
            try:
                reg(route, handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 注册 {route} 失败：{exc}")

    # ------------------------------------------------------------ 型号抓取
    def _current_provider(self, umo: str | None = None) -> dict | None:
        """默认/当前供应商的型号（Web 页面没有 event，用这个展示）。"""
        try:
            prov = self.context.get_using_provider(umo)
            if prov is None:
                return None
            meta = prov.meta()
            return {
                "id": getattr(meta, "id", None),
                "type": getattr(meta, "type", None),
                "model": getattr(meta, "model", None),
            }
        except Exception:  # noqa: BLE001
            return None

    @filter.on_llm_request(priority=1000000)
    async def capture_model(self, event: AstrMessageEvent, req: ProviderRequest):
        """只读抓本次真实型号挂到 event；绝不改 req.system_prompt。"""
        try:
            umo = event.unified_msg_origin
            info = self._current_provider(umo)
            model = getattr(req, "model", None) or (info or {}).get("model") or "未知"
            setattr(
                event,
                CAPTURE_KEY,
                (
                    (info or {}).get("id") or "未知",
                    (info or {}).get("type") or "未知",
                    model,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 抓取型号失败：{exc}")

    # ------------------------------------------------------------ 数据路径
    def _supplement_path(self) -> str | None:
        try:
            return str(Path(StarTools.get_data_dir(_stats.CC_NAME)) / "supplement.db")
        except Exception:  # noqa: BLE001
            return None

    def _main_db_path(self) -> str | None:
        custom = self.cfg.get("main_db_path")
        if custom:
            return str(custom)
        try:  # 官方 db 对象里通常能拿到 sqlite 路径
            db = self.context.get_db()
            url = getattr(getattr(db, "engine", None), "url", None)
            p = getattr(url, "database", None)
            if p and Path(p).is_file():
                return str(p)
        except Exception:  # noqa: BLE001
            pass
        try:  # 兜底：从插件数据目录上溯常见的 data_v4.db
            base = Path(StarTools.get_data_dir())
            for cand in (
                base / "data_v4.db",
                base.parent / "data_v4.db",
                base.parent.parent / "data_v4.db",
            ):
                if cand.is_file():
                    return str(cand)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _read(
        self, *, window: str | None = None, umo: str | None = None
    ) -> dict[str, Any]:
        window = window or self.cfg.get("default_window") or "24h"
        data = _stats.collect(
            self._supplement_path(),
            self._main_db_path(),
            window=window,
            umo=umo,
            recent_limit=int(self.cfg.get("recent_limit", 12) or 12),
        )
        data["current"] = self._current_provider()
        data["plugin"] = PLUGIN_NAME
        return data

    # ------------------------------------------------------------ LLM 工具
    @filter.llm_tool(name="get_runtime_info")
    async def get_runtime_info(
        self,
        event: AstrMessageEvent,
        scope: str = "session",
        window: str = "24h",
    ):
        """查询我当前真实使用的模型、实际消耗的 token 数与真实缓存命中率。
        仅当用户明确问「什么模型 / 用了多少 token / 缓存命中多少 / 额度消耗 / 是不是没命中缓存」时调用；
        闲聊、查游戏资料、要表情包、普通问答都不要调用。

        Args:
            scope(string): session=只统计当前会话，all=全部会话
            window(string): 24h / today / weekly / all，默认 24h
        """
        try:
            got = getattr(event, CAPTURE_KEY, None)
            if not got:
                info = self._current_provider(event.unified_msg_origin)
                if info:
                    got = (info.get("id"), info.get("type"), info.get("model") or "未知")
            umo = event.unified_msg_origin if scope != "all" else None
            data = self._read(window=window, umo=umo)
            return _stats.format_for_llm(data, got)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 工具查询失败：{exc}")
            return "运行信息暂时读不到。请如实告知用户读不到，不要猜型号、不要编数值。"

    # ------------------------------------------------------------ Web API
    @staticmethod
    def _param(name: str, default: str = "") -> str:
        try:
            from quart import request

            return request.args.get(name, default)
        except Exception:  # noqa: BLE001
            return default

    @staticmethod
    def _ok(data: Any = None, **extra: Any) -> dict[str, Any]:
        """刻意用 {success, data} 非标准信封，避开 dashboard 前端对 {status,data} 的自动解包。"""
        out: dict[str, Any] = {"success": True}
        if data is not None:
            out["data"] = data
        out.update(extra)
        return out

    async def api_stats(self, **_kwargs):
        """GET /api/plug/<plugin>/stats?window=24h&umo=..."""
        try:
            window = (self._param("window") or "").strip() or None
            umo = (self._param("umo") or "").strip() or None
            data = self._read(window=window, umo=umo)
            return self._ok(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] stats 失败：{exc}")
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    async def api_page(self, **_kwargs):
        """GET /api/plug/<plugin>/page —— 返回自包含 HTML。"""
        try:
            from quart import Response

            html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
            return Response(
                html,
                mimetype="text/html",
                headers={"Cache-Control": "no-store"},
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"页面读取失败：{exc}"}

    async def api_health(self, **_kwargs):
        return self._ok(
            {
                "plugin": PLUGIN_NAME,
                "supplement_found": bool(
                    self._supplement_path() and Path(self._supplement_path()).is_file()
                ),
                "main_db_found": bool(
                    self._main_db_path() and Path(self._main_db_path()).is_file()
                ),
                "page_found": (ROOT / "web" / "index.html").is_file(),
            }
        )
