import re
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.db.site_oper import SiteOper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.http import RequestUtils


class ChdBitsAutoTask(_PluginBase):
    """CHDBits 任务系统监控与自动报名。"""

    plugin_name = "CHD自动抢任务"
    plugin_desc = "监控 CHDBits 任务名额（剩余/上限），定时轮询并按优先级自动报名"
    plugin_icon = "CHDBits.png"
    plugin_version = "1.0.2"
    plugin_author = "Kuanghom"
    author_url = "https://github.com/Kuanghom"
    plugin_config_prefix = "chdbitsautotask_"
    plugin_order = 26
    auth_level = 2

    LOG_TAG = "[ChdBitsAutoTask] "
    BASE_URL = "https://ptchdbits.co"
    TASK_PATH = "/selfassess.php"
    INFO_PATH = "/selfassessinfo.php"
    DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    DEFAULT_SLOT_LIMIT = 200
    TARGET_SITE_NAMES = ("彩虹岛", "CHD", "CHDBits", "chdbits")
    # id -> 元信息（页面会覆盖动态字段）；upload/download 单位 GB
    TASK_DEFS = {
        1: {"key": "Master", "title": "骨灰", "fee": 100000, "upload_gb": 2000, "download_gb": 600, "seed_points": 45000},
        2: {"key": "Ultimate", "title": "走火入魔", "fee": 80000, "upload_gb": 1500, "download_gb": 500, "seed_points": 40000},
        3: {"key": "Extreme", "title": "烧糊涂", "fee": 50000, "upload_gb": 1000, "download_gb": 400, "seed_points": 35000},
        4: {"key": "Veteran", "title": "高烧", "fee": 30000, "upload_gb": 500, "download_gb": 300, "seed_points": 30000},
        5: {"key": "Insane", "title": "中烧", "fee": 1000, "upload_gb": 300, "download_gb": 100, "seed_points": 20000},
    }
    TASK_KEY_TO_ID = {v["key"]: k for k, v in TASK_DEFS.items()}
    STATE_LABELS = {
        "idle": "可报名",
        "excess": "名额已满",
        "tasking": "任务进行中",
        "close": "系统关闭",
        "unknown": "未知",
    }

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "*/1 * * * *"
    _site_id: Optional[int] = None
    _cookie = ""
    _ua = DEFAULT_UA
    _use_proxy = True
    _site_name = ""
    _auto_claim = True
    _stop_on_hit = True
    _preferred_tasks: List[str] = ["Extreme", "Insane", "Veteran"]
    _min_bonus = 0
    _slot_limit = DEFAULT_SLOT_LIMIT
    _notify_available = True
    _notify_full_once = True
    _progress_notify = True
    _progress_interval_hours = 6
    _history_days = 90
    _scheduler: Optional[BackgroundScheduler] = None
    _run_lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._onlyonce = bool(config.get("onlyonce"))
        self._cron = (config.get("cron") or "*/1 * * * *").strip()
        self._site_id = self._normalize_site_id(config.get("site_id"))
        self._auto_claim = bool(config.get("auto_claim", True))
        self._stop_on_hit = bool(config.get("stop_on_hit", True))
        self._preferred_tasks = self._parse_preferred_tasks(config.get("preferred_tasks"))
        self._min_bonus = max(0, self._to_int(config.get("min_bonus"), 0))
        self._slot_limit = max(1, self._to_int(config.get("slot_limit"), self.DEFAULT_SLOT_LIMIT))
        self._notify_available = bool(config.get("notify_available", True))
        self._notify_full_once = bool(config.get("notify_full_once", True))
        self._progress_notify = bool(config.get("progress_notify", True))
        self._progress_interval_hours = max(1, self._to_int(config.get("progress_interval_hours"), 6))
        self._history_days = max(7, self._to_int(config.get("history_days"), 90))

        if not self._site_id:
            sites = self._list_chd_sites()
            if len(sites) == 1:
                self._site_id = int(sites[0].get("id"))
                config["site_id"] = self._site_id
                self.update_config(config)
                logger.info(f"{self.LOG_TAG}自动选中站点: {sites[0].get('name')}#{self._site_id}")

        if self._site_id:
            valid_ids = {int(s.get("id")) for s in self._list_chd_sites() if s.get("id") is not None}
            if valid_ids and self._site_id not in valid_ids:
                logger.warning(f"{self.LOG_TAG}已选站点 {self._site_id} 不在 CHD 站列表中，请重新选择")

        self.stop_service()
        if self._onlyonce and self._enabled:
            self._onlyonce = False
            config["onlyonce"] = False
            self.update_config(config)
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.run_once,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="CHD自动抢任务立即执行",
            )
            if self._scheduler.get_jobs():
                self._scheduler.start()
                logger.info(f"{self.LOG_TAG}已加入立即执行任务")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        return [
            {
                "id": "ChdBitsAutoTask.Run",
                "name": "CHD自动抢任务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.run_once,
                "kwargs": {},
            }
        ]

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown(wait=False)
                self._scheduler = None
        except Exception as e:
            logger.error(f"{self.LOG_TAG}停止调度器失败: {e}")

    # ------------------------------------------------------------------ #
    # 配置页 / 详情页
    # ------------------------------------------------------------------ #
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        version = getattr(settings, "VERSION_FLAG", "v1")
        cron_field = "VCronField" if version == "v2" else "VTextField"
        site_options = [
            {"title": site.get("name"), "value": site.get("id")}
            for site in self._list_chd_sites()
        ]
        site_alert = None
        if not site_options:
            site_alert = {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "text": "未在站点管理中找到 CHDBits（ptchdbits.co）。请先添加并配置 Cookie 后再选择。",
                },
            }

        task_items = [
            {"title": f"{v['key']}（{v['title']}，报名费约 {v['fee']}）", "value": v["key"]}
            for v in self.TASK_DEFS.values()
        ]
        form_content = [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "enabled",
                                    "label": "启用插件",
                                    "color": "primary",
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "notify",
                                    "label": "开启通知",
                                    "color": "info",
                                    "hint": "报名成功/失败、名额空出、已在任务中等",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "onlyonce",
                                    "label": "立即运行一次",
                                    "color": "warning",
                                },
                            }
                        ],
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VSelect",
                                "props": {
                                    "chips": True,
                                    "model": "site_id",
                                    "label": "选择站点",
                                    "items": site_options,
                                    "hint": "从站点管理读取 CHDBits 的 Cookie / UA / 代理",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    }
                ],
            },
        ]
        if site_alert:
            form_content.append(
                {
                    "component": "VRow",
                    "content": [{"component": "VCol", "props": {"cols": 12}, "content": [site_alert]}],
                }
            )
        form_content.extend(
            [
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "auto_claim",
                                        "label": "有名额时自动报名",
                                        "color": "success",
                                        "hint": "关闭则仅监控名额，不自动 POST 报名",
                                        "persistent-hint": True,
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "stop_on_hit",
                                        "label": "达成后停用",
                                        "color": "warning",
                                        "hint": "仅监控：发现有名额即停；自动报名：报名成功即停",
                                        "persistent-hint": True,
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "notify_available",
                                        "label": "名额空出时通知",
                                        "color": "info",
                                        "hint": "剩余从 0 变为 >0 时推送（防刷屏）",
                                        "persistent-hint": True,
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [
                                {
                                    "component": cron_field,
                                    "props": {
                                        "model": "cron",
                                        "label": "监控周期",
                                        "hint": "建议 */1 或 */2；名额紧俏时可更频繁",
                                        "persistent-hint": True,
                                    },
                                }
                            ],
                        },
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VSelect",
                                    "props": {
                                        "model": "preferred_tasks",
                                        "label": "报名优先级（从上到下尝试）",
                                        "multiple": True,
                                        "chips": True,
                                        "items": task_items,
                                        "hint": "按勾选顺序依次尝试；魔力不足或失败则试下一档。Master/Ultimate 多为 VIP/黄星专享。",
                                        "persistent-hint": True,
                                    },
                                }
                            ],
                        }
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "min_bonus",
                                        "label": "最低保留魔力",
                                        "type": "number",
                                        "hint": "报名后魔力需仍 ≥ 此值（会叠加报名费校验）",
                                        "persistent-hint": True,
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "slot_limit",
                                        "label": "名额上限",
                                        "type": "number",
                                        "hint": "普通会员默认 200；页面能解析到规则时会覆盖",
                                        "persistent-hint": True,
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "history_days",
                                        "label": "报名记录保留天数",
                                        "type": "number",
                                        "placeholder": "90",
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "progress_notify",
                                        "label": "任务进度定期通知",
                                        "color": "info",
                                        "hint": "已领取任务时，按间隔推送完成进度",
                                        "persistent-hint": True,
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "progress_interval_hours",
                                        "label": "进度通知间隔(小时)",
                                        "type": "number",
                                        "hint": "默认 6；首次发现进行中任务会立即通知一次",
                                        "persistent-hint": True,
                                    },
                                }
                            ],
                        },
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": (
                                            "名额为整站共用（普通会员上限通常 200，VIP 不限）。"
                                            "不可同时领取多个任务，领取后无法取消。"
                                            "插件只解析页面公开字段，无法拿到每个任务档位的独立剩余名额。"
                                        ),
                                    },
                                }
                            ],
                        }
                    ],
                },
            ]
        )
        return [
            {"component": "VForm", "content": form_content}
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "site_id": self._site_id,
            "auto_claim": True,
            "stop_on_hit": True,
            "notify_available": True,
            "notify_full_once": True,
            "progress_notify": True,
            "progress_interval_hours": 6,
            "cron": "*/1 * * * *",
            "preferred_tasks": ["Extreme", "Insane", "Veteran"],
            "min_bonus": 0,
            "slot_limit": self.DEFAULT_SLOT_LIMIT,
            "history_days": 90,
        }

    def get_page(self) -> List[dict]:
        last = self.get_data("last_status") or {}
        history = self.get_data("history") or []
        username = self.get_data("username") or "—"
        site_name = self.get_data("site_name") or self._site_name or "CHDBits"
        bonus = last.get("bonus")
        seed_points = last.get("seed_points")
        current = last.get("current")
        limit = last.get("limit") or self._slot_limit
        remaining = last.get("remaining")
        state = last.get("state") or "unknown"
        state_text = self.STATE_LABELS.get(state, state)
        updated = last.get("time") or "—"
        preferred = " → ".join(self._preferred_tasks) if self._preferred_tasks else "—"
        my_done = last.get("my_done") or {}
        tasks = last.get("tasks") or []
        progress = last.get("progress") or {}

        if remaining is None and current is not None and limit is not None:
            remaining = max(0, int(limit) - int(current))

        rem_color = "success" if (remaining or 0) > 0 else "error"
        state_color = {
            "idle": "success",
            "excess": "warning",
            "tasking": "info",
            "close": "error",
            "unknown": "secondary",
        }.get(state, "secondary")

        cards = [
            self._stat_card("站点", site_name, "primary"),
            self._stat_card("用户", username, "secondary"),
            self._stat_card(
                "剩余 / 上限",
                f"{remaining if remaining is not None else '—'} / {limit if limit is not None else '—'}",
                rem_color,
            ),
            self._stat_card("当前占用", str(current if current is not None else "—"), "info"),
            self._stat_card("状态", state_text, state_color),
            self._stat_card(
                "魔力",
                f"{bonus:,.1f}" if isinstance(bonus, (int, float)) else "—",
                "warning",
            ),
            self._stat_card(
                "做种积分",
                f"{seed_points:,.1f}" if isinstance(seed_points, (int, float)) else "—",
                "success",
            ),
            self._stat_card(
                "任务进度",
                progress.get("overall_text") or ("进行中" if state == "tasking" else "—"),
                "info" if state == "tasking" else "secondary",
            ),
        ]

        task_rows = []
        for t in tasks:
            task_rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": t.get("key", "—")},
                        {"component": "td", "text": t.get("title", "—")},
                        {"component": "td", "text": str(t.get("upload") or "—")},
                        {"component": "td", "text": str(t.get("download") or "—")},
                        {"component": "td", "text": str(t.get("seed_points") or "—")},
                        {"component": "td", "text": str(t.get("fee") or "—")},
                        {"component": "td", "text": str(t.get("reward") or "—")},
                        {
                            "component": "td",
                            "text": str(my_done.get(t.get("key"), "—")),
                        },
                    ],
                }
            )

        hist_rows = []
        for item in sorted(history, key=lambda x: x.get("time", ""), reverse=True)[:50]:
            ok = bool(item.get("success"))
            hist_rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": item.get("time", "—")},
                        {
                            "component": "td",
                            "content": [
                                {
                                    "component": "VChip",
                                    "props": {
                                        "color": "success" if ok else "error",
                                        "size": "small",
                                        "variant": "flat",
                                    },
                                    "text": "成功" if ok else "失败",
                                }
                            ],
                        },
                        {"component": "td", "text": item.get("task", "—")},
                        {"component": "td", "text": str(item.get("fee") or "—")},
                        {
                            "component": "td",
                            "text": f"{item.get('remaining', '—')} / {item.get('limit', '—')}",
                        },
                        {"component": "td", "text": item.get("message") or "—"},
                    ],
                }
            )

        progress_alert = None
        if state == "tasking" or progress:
            prog_lines = [
                f"任务：{progress.get('task_label') or '进行中'}",
                f"整体：{progress.get('overall_text') or '—'}",
            ]
            for key, label in (("upload", "上传"), ("download", "下载"), ("seed", "做种积分")):
                item = progress.get(key) or {}
                if item.get("text"):
                    prog_lines.append(f"{label}：{item['text']}")
            if progress.get("remain_text"):
                prog_lines.append(f"剩余时间：{progress.get('remain_text')}")
            prog_lines.append(f"更新：{updated}")
            progress_alert = {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "success",
                                    "variant": "tonal",
                                    "text": " | ".join(prog_lines),
                                },
                            }
                        ],
                    }
                ],
            }

        page = [
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [c]}
                    for c in cards
                ],
            },
        ]
        if progress_alert:
            page.append(progress_alert)
        page.extend(
            [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": (
                                        f"自动报名={'开' if self._auto_claim else '关'}；"
                                        f"达成后停用={'开' if self._stop_on_hit else '关'}；"
                                        f"进度通知={'开' if self._progress_notify else '关'}"
                                        f"（每{self._progress_interval_hours}小时）；"
                                        f"优先级：{preferred}；"
                                        f"Cron：{self._cron or '—'}；"
                                        f"最低保留魔力：{self._min_bonus}"
                                    ),
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal"},
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": "任务档位（页面解析）",
                                    },
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {
                                                "component": "VTable",
                                                "props": {"density": "compact"},
                                                "content": [
                                                    {
                                                        "component": "thead",
                                                        "content": [
                                                            {
                                                                "component": "tr",
                                                                "content": [
                                                                    {"component": "th", "text": "任务"},
                                                                    {"component": "th", "text": "别名"},
                                                                    {"component": "th", "text": "上传"},
                                                                    {"component": "th", "text": "下载"},
                                                                    {"component": "th", "text": "做种积分"},
                                                                    {"component": "th", "text": "报名费"},
                                                                    {"component": "th", "text": "奖励"},
                                                                    {"component": "th", "text": "我已完成"},
                                                                ],
                                                            }
                                                        ],
                                                    },
                                                    {
                                                        "component": "tbody",
                                                        "content": task_rows
                                                        or [
                                                            {
                                                                "component": "tr",
                                                                "content": [
                                                                    {
                                                                        "component": "td",
                                                                        "props": {"colspan": 8},
                                                                        "text": "暂无数据，请先运行一次",
                                                                    }
                                                                ],
                                                            }
                                                        ],
                                                    },
                                                ],
                                            }
                                        ],
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal"},
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": "报名记录",
                                    },
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {
                                                "component": "VTable",
                                                "props": {"density": "compact"},
                                                "content": [
                                                    {
                                                        "component": "thead",
                                                        "content": [
                                                            {
                                                                "component": "tr",
                                                                "content": [
                                                                    {"component": "th", "text": "时间"},
                                                                    {"component": "th", "text": "结果"},
                                                                    {"component": "th", "text": "任务"},
                                                                    {"component": "th", "text": "报名费"},
                                                                    {"component": "th", "text": "剩余/上限"},
                                                                    {"component": "th", "text": "说明"},
                                                                ],
                                                            }
                                                        ],
                                                    },
                                                    {
                                                        "component": "tbody",
                                                        "content": hist_rows
                                                        or [
                                                            {
                                                                "component": "tr",
                                                                "content": [
                                                                    {
                                                                        "component": "td",
                                                                        "props": {"colspan": 6},
                                                                        "text": "暂无报名记录",
                                                                    }
                                                                ],
                                                            }
                                                        ],
                                                    },
                                                ],
                                            }
                                        ],
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
        ]
        )
        return page

    @staticmethod
    def _stat_card(title: str, text: str, color: str) -> dict:
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "color": color},
            "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-2"}, "text": title},
                {"component": "VCardText", "props": {"class": "text-h6"}, "text": text},
            ],
        }

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def run_once(self):
        if not self._run_lock.acquire(blocking=False):
            logger.warning(f"{self.LOG_TAG}上一轮仍在执行，跳过本次")
            return
        try:
            ok, msg = self._load_site_auth()
            if not ok:
                logger.error(f"{self.LOG_TAG}{msg}")
                self._notify_event(
                    "error",
                    "加载站点失败",
                    [("原因", msg)],
                )
                return

            html = self._get(self.TASK_PATH)
            if not html:
                logger.error(f"{self.LOG_TAG}拉取任务页失败")
                self._notify_event(
                    "error",
                    "拉取任务页失败",
                    [("原因", "selfassess.php 请求失败，请检查 Cookie/代理")],
                )
                return

            info = self._parse_task_page(html)
            # 补充趋势图增量（进行中任务时更有用）
            chart = self._parse_progress_chart()
            if chart:
                info["chart"] = chart
            prev = self.get_data("last_status") or {}
            prev_remaining = prev.get("remaining")
            self._save_status(info)

            logger.info(
                f"{self.LOG_TAG}状态={info.get('state')} "
                f"占用={info.get('current')}/{info.get('limit')} "
                f"剩余={info.get('remaining')} "
                f"魔力={info.get('bonus')} 用户={info.get('username')}"
            )

            # 名额从满变为有空位
            if (
                self._notify_available
                and prev_remaining is not None
                and int(prev_remaining or 0) <= 0
                and int(info.get("remaining") or 0) > 0
            ):
                self._notify_event(
                    "available",
                    "任务名额空出",
                    [
                        ("剩余名额", f"{info.get('remaining')} / {info.get('limit')}"),
                        ("当前占用", str(info.get("current"))),
                        ("系统状态", self.STATE_LABELS.get(info.get("state"), info.get("state"))),
                        ("自动报名", "开" if self._auto_claim else "关"),
                    ],
                )

            state = info.get("state")
            if state == "tasking":
                logger.info(f"{self.LOG_TAG}已有进行中任务，跳过报名，检查进度通知")
                self._handle_tasking_progress(info, prev)
                return

            # 非任务中则清理活动任务缓存
            if self.get_data("active_task"):
                self.save_data("active_task", {})
                self.save_data("last_progress_notify_at", "")

            if state == "close":
                logger.warning(f"{self.LOG_TAG}任务系统已关闭")
                return

            if state == "excess" or int(info.get("remaining") or 0) <= 0:
                logger.info(f"{self.LOG_TAG}名额已满，继续等待")
                return

            if not self._auto_claim:
                logger.info(f"{self.LOG_TAG}仅监控模式：有名额但不报名")
                self._notify_event(
                    "available",
                    "仅监控：发现有名额",
                    [
                        ("剩余名额", f"{info.get('remaining')} / {info.get('limit')}"),
                        ("当前占用", str(info.get("current"))),
                        ("系统状态", self.STATE_LABELS.get(info.get("state"), info.get("state"))),
                    ],
                )
                if self._stop_on_hit:
                    self._disable_after_hit(
                        reason="仅监控：已发现有名额",
                        detail=(
                            f"剩余：{info.get('remaining')} / 上限：{info.get('limit')}\n"
                            f"当前占用：{info.get('current')}"
                        ),
                    )
                return

            self._try_claim(info)
        except Exception as e:
            logger.error(f"{self.LOG_TAG}执行异常: {e}", exc_info=True)
            self._notify_event("error", "执行异常", [("原因", str(e))])
        finally:
            self._run_lock.release()

    def _handle_tasking_progress(self, info: Dict[str, Any], prev: Dict[str, Any]):
        """任务进行中：维护进度并按间隔推送。"""
        progress = self._build_progress(info)
        info["progress"] = progress
        info["notified_tasking"] = True
        self._save_status(info)

        if not self._progress_notify:
            if not prev.get("notified_tasking"):
                self._notify_event(
                    "tasking",
                    "检测到任务进行中",
                    self._progress_fields(progress, info),
                )
            return

        last_ts = self.get_data("last_progress_notify_at")
        force_first = not prev.get("notified_tasking") or not last_ts
        due = force_first
        if last_ts and not force_first:
            try:
                last_dt = datetime.strptime(str(last_ts), "%Y-%m-%d %H:%M:%S")
                due = datetime.now() - last_dt >= timedelta(hours=self._progress_interval_hours)
            except Exception:
                due = True

        if not due:
            logger.debug(f"{self.LOG_TAG}进度通知未到间隔，跳过")
            return

        title = "任务进度汇报" if prev.get("notified_tasking") else "检测到任务进行中"
        self._notify_event("progress", title, self._progress_fields(progress, info))
        self.save_data("last_progress_notify_at", self._now_str())

    def _progress_fields(self, progress: Dict[str, Any], info: Dict[str, Any]) -> List[Tuple[str, str]]:
        fields: List[Tuple[str, str]] = [
            ("任务", progress.get("task_label") or "进行中"),
            ("整体进度", progress.get("overall_text") or "—"),
        ]
        for key, label in (
            ("upload", "上传增量"),
            ("download", "下载增量"),
            ("seed", "做种积分"),
        ):
            item = progress.get(key) or {}
            if item.get("text"):
                fields.append((label, item["text"]))
        if progress.get("start_time"):
            fields.append(("开始时间", str(progress.get("start_time"))))
        if progress.get("end_time"):
            fields.append(("结束时间", str(progress.get("end_time"))))
        if progress.get("remain_text"):
            fields.append(("剩余时间", str(progress.get("remain_text"))))
        fields.extend(
            [
                ("魔力", self._fmt_num(info.get("bonus"))),
                ("账户做种积分", self._fmt_num(info.get("seed_points"))),
                ("名额", f"{info.get('remaining')} / {info.get('limit')}"),
            ]
        )
        return fields

    def _build_progress(self, info: Dict[str, Any]) -> Dict[str, Any]:
        """综合页面解析 + 本地基线，估算任务完成情况。"""
        page_prog = info.get("page_progress") or {}
        active = self.get_data("active_task") or {}
        # 若无本地任务，尝试从最近成功报名恢复
        if not active.get("key"):
            for h in reversed(self.get_data("history") or []):
                if h.get("success") and h.get("task"):
                    key = h["task"]
                    tid = self.TASK_KEY_TO_ID.get(key)
                    meta = self.TASK_DEFS.get(tid or 0, {})
                    active = {
                        "key": key,
                        "title": meta.get("title"),
                        "task_id": tid,
                        "upload_gb": meta.get("upload_gb"),
                        "download_gb": meta.get("download_gb"),
                        "seed_points": meta.get("seed_points"),
                        "start_upload_gb": info.get("upload_gb"),
                        "start_download_gb": info.get("download_gb"),
                        "start_seed_points": info.get("seed_points"),
                        "start_time": h.get("time") or self._now_str(),
                        "source": "history",
                    }
                    self.save_data("active_task", active)
                    break

        # 首次进入 tasking 且仍无基线：记录当前总量为起点
        if info.get("state") == "tasking" and not active.get("start_upload_gb"):
            key = (page_prog or {}).get("task_key") or active.get("key")
            tid = self.TASK_KEY_TO_ID.get(key) if key else None
            meta = self.TASK_DEFS.get(tid or 0, {})
            active = {
                **active,
                "key": key or active.get("key") or "Unknown",
                "title": meta.get("title") or active.get("title"),
                "task_id": tid or active.get("task_id"),
                "upload_gb": active.get("upload_gb") or meta.get("upload_gb"),
                "download_gb": active.get("download_gb") or meta.get("download_gb"),
                "seed_points": active.get("seed_points") or meta.get("seed_points"),
                "start_upload_gb": info.get("upload_gb"),
                "start_download_gb": info.get("download_gb"),
                "start_seed_points": info.get("seed_points"),
                "start_time": active.get("start_time") or self._now_str(),
                "source": active.get("source") or "baseline",
            }
            self.save_data("active_task", active)

        # 优先用页面显式进度
        result: Dict[str, Any] = {
            "task_key": (page_prog or {}).get("task_key") or active.get("key"),
            "task_label": None,
            "start_time": (page_prog or {}).get("start_time") or active.get("start_time"),
            "end_time": (page_prog or {}).get("end_time"),
            "remain_text": (page_prog or {}).get("remain_text"),
            "source": "page" if page_prog else "baseline",
        }
        key = result["task_key"]
        meta = self.TASK_DEFS.get(self.TASK_KEY_TO_ID.get(key or ""), {})
        result["task_label"] = (
            f"{key}（{meta.get('title') or active.get('title') or ''}）".rstrip("（）")
            if key
            else "进行中"
        )

        def metric(name: str, cur: Optional[float], target: Optional[float], unit: str = "") -> Dict[str, Any]:
            if cur is None or target is None or float(target) <= 0:
                return {"text": "—", "pct": None, "cur": cur, "target": target}
            pct = max(0.0, min(100.0, float(cur) / float(target) * 100))
            bar = self._progress_bar(pct)
            return {
                "cur": cur,
                "target": target,
                "pct": pct,
                "text": f"{bar} {cur:.1f}{unit} / {target:g}{unit}（{pct:.1f}%）",
            }

        if page_prog and any(page_prog.get(k) for k in ("upload_cur", "download_cur", "seed_cur")):
            result["upload"] = metric(
                "upload",
                page_prog.get("upload_cur"),
                page_prog.get("upload_target") or active.get("upload_gb"),
                "G",
            )
            result["download"] = metric(
                "download",
                page_prog.get("download_cur"),
                page_prog.get("download_target") or active.get("download_gb"),
                "G",
            )
            result["seed"] = metric(
                "seed",
                page_prog.get("seed_cur"),
                page_prog.get("seed_target") or active.get("seed_points"),
                "",
            )
        else:
            # 用账户增量估算
            up = None
            down = None
            seed = None
            if info.get("upload_gb") is not None and active.get("start_upload_gb") is not None:
                up = max(0.0, float(info["upload_gb"]) - float(active["start_upload_gb"]))
            if info.get("download_gb") is not None and active.get("start_download_gb") is not None:
                down = max(0.0, float(info["download_gb"]) - float(active["start_download_gb"]))
            if info.get("seed_points") is not None and active.get("start_seed_points") is not None:
                seed = max(0.0, float(info["seed_points"]) - float(active["start_seed_points"]))
            # 图表最新值可作为补充
            chart = info.get("chart") or {}
            if up is None and chart.get("upload") is not None:
                up = float(chart["upload"])
            if down is None and chart.get("download") is not None:
                down = float(chart["download"])
            if seed is None and chart.get("seed") is not None:
                seed = float(chart["seed"])

            result["upload"] = metric("upload", up, active.get("upload_gb"), "G")
            result["download"] = metric("download", down, active.get("download_gb"), "G")
            result["seed"] = metric("seed", seed, active.get("seed_points"), "")
            result["source"] = "baseline+chart" if chart else "baseline"

        pcts = [
            x.get("pct")
            for x in (result.get("upload"), result.get("download"), result.get("seed"))
            if isinstance(x, dict) and x.get("pct") is not None
        ]
        if pcts:
            overall = sum(pcts) / len(pcts)
            result["overall_pct"] = overall
            result["overall_text"] = f"{self._progress_bar(overall)} {overall:.1f}%"
            result["completed"] = all(p >= 100 for p in pcts)
        else:
            result["overall_text"] = "暂无法计算"
            result["completed"] = False
        return result

    @staticmethod
    def _progress_bar(pct: float, width: int = 10) -> str:
        filled = int(round(max(0.0, min(100.0, pct)) / 100 * width))
        return "█" * filled + "░" * (width - filled)

    def _disable_after_hit(self, reason: str, detail: str = ""):
        """达成目标后关闭插件，停止后续 Cron。"""
        if not self._enabled:
            return
        self._enabled = False
        try:
            self.update_config(self.get_config_dict())
        except Exception as e:
            logger.error(f"{self.LOG_TAG}写回停用配置失败: {e}")
        self.stop_service()
        logger.info(f"{self.LOG_TAG}已停用插件：{reason}")
        fields = [("原因", reason)]
        if detail:
            fields.append(("详情", detail.replace("\n", " | ")))
        fields.append(("提示", "插件已自动关闭，需要时请手动重新启用"))
        self._notify_event("stop", "插件已停用", fields)

    def get_config_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": False,
            "site_id": self._site_id,
            "auto_claim": self._auto_claim,
            "stop_on_hit": self._stop_on_hit,
            "notify_available": self._notify_available,
            "notify_full_once": self._notify_full_once,
            "progress_notify": self._progress_notify,
            "progress_interval_hours": self._progress_interval_hours,
            "cron": self._cron,
            "preferred_tasks": self._preferred_tasks,
            "min_bonus": self._min_bonus,
            "slot_limit": self._slot_limit,
            "history_days": self._history_days,
        }

    def _try_claim(self, info: Dict[str, Any]):
        bonus = info.get("bonus")
        tasks_by_key = {t.get("key"): t for t in (info.get("tasks") or []) if t.get("key")}
        tried = []

        for key in self._preferred_tasks:
            task_id = self.TASK_KEY_TO_ID.get(key)
            if not task_id:
                continue
            meta = tasks_by_key.get(key) or {**self.TASK_DEFS[task_id], "id": task_id}
            fee = self._to_int(meta.get("fee"), self.TASK_DEFS[task_id]["fee"])
            if isinstance(bonus, (int, float)):
                if bonus < fee:
                    tried.append(f"{key}:魔力不足(需{fee})")
                    logger.info(f"{self.LOG_TAG}跳过 {key}：魔力 {bonus} < 报名费 {fee}")
                    continue
                if bonus - fee < self._min_bonus:
                    tried.append(f"{key}:低于保留魔力")
                    logger.info(
                        f"{self.LOG_TAG}跳过 {key}：报名后魔力 {bonus - fee} < 保留 {self._min_bonus}"
                    )
                    continue

            logger.info(f"{self.LOG_TAG}尝试报名 {key}(id={task_id}) 费用={fee}")
            ok, message = self._claim_task(task_id)
            record = {
                "time": self._now_str(),
                "success": ok,
                "task": key,
                "task_id": task_id,
                "fee": fee,
                "remaining": info.get("remaining"),
                "limit": info.get("limit"),
                "message": message,
            }
            self._append_history(record)

            if ok:
                defs = self.TASK_DEFS.get(task_id, {})
                self.save_data(
                    "active_task",
                    {
                        "key": key,
                        "title": meta.get("title") or defs.get("title"),
                        "task_id": task_id,
                        "upload_gb": defs.get("upload_gb"),
                        "download_gb": defs.get("download_gb"),
                        "seed_points": defs.get("seed_points"),
                        "start_upload_gb": info.get("upload_gb"),
                        "start_download_gb": info.get("download_gb"),
                        "start_seed_points": info.get("seed_points"),
                        "start_time": self._now_str(),
                        "fee": fee,
                        "source": "claim",
                    },
                )
                self.save_data("last_progress_notify_at", None)
                self._notify_event(
                    "success",
                    "任务报名成功",
                    [
                        ("任务", f"{key}（{meta.get('title') or ''}）"),
                        ("报名费", str(fee)),
                        ("报名前剩余", f"{info.get('remaining')} / {info.get('limit')}"),
                        ("魔力", self._fmt_num(bonus)),
                        ("说明", message),
                    ],
                )
                # 刷新一次状态
                html2 = self._get(self.TASK_PATH)
                if html2:
                    info2 = self._parse_task_page(html2)
                    self._save_status(info2)
                    # 用报名后的账户数据校正基线
                    active = self.get_data("active_task") or {}
                    if info2.get("upload_gb") is not None:
                        active["start_upload_gb"] = info2.get("upload_gb")
                    if info2.get("download_gb") is not None:
                        active["start_download_gb"] = info2.get("download_gb")
                    if info2.get("seed_points") is not None:
                        active["start_seed_points"] = info2.get("seed_points")
                    self.save_data("active_task", active)
                if self._stop_on_hit:
                    if self._progress_notify:
                        logger.info(f"{self.LOG_TAG}报名成功，保持启用以定期推送任务进度")
                    else:
                        self._disable_after_hit(
                            reason=f"报名成功：{key}",
                            detail=(
                                f"任务：{key}（{meta.get('title') or ''}）\n"
                                f"报名费：{fee}\n"
                                f"说明：{message}"
                            ),
                        )
                return

            tried.append(f"{key}:{message}")
            logger.warning(f"{self.LOG_TAG}报名 {key} 失败: {message}")
            # 名额瞬间被抢光则不必继续
            if "名额" in message or "已满" in message or "excess" in message.lower():
                break

        self._notify_event(
            "error",
            "任务报名失败",
            [("尝试结果", "；".join(tried) if tried else "无可用任务")],
        )

    def _claim_task(self, task_id: int) -> Tuple[bool, str]:
        data = {"id": str(task_id), "action": "order"}
        html = self._post(self.TASK_PATH, data=data)
        if html is None:
            return False, "POST 无响应"
        if "该页面必须在登录后才能访问" in html or "CHDBits :: 登录" in html:
            return False, "Cookie 失效"
        info = self._parse_task_page(html)
        state = info.get("state")
        if state == "tasking":
            return True, "已进入任务中"
        # 部分站点报名后仍短暂显示 idle，但会有成功提示
        for tip in ("报名成功", "领取成功", "任务已开始", "成功领取"):
            if tip in html:
                return True, tip
        if state == "excess":
            return False, "名额已满"
        if state == "close":
            return False, "任务系统关闭"
        if "不能同时领取多个任务" in html:
            return False, "已有进行中任务"
        if "抱歉您不是捐助用户" in html or "请选择其它任务" in html:
            return False, "无权限领取该档位"
        if "魔力" in html and ("不足" in html or "不够" in html):
            return False, "魔力不足"
        # 重新 GET 确认
        html2 = self._get(self.TASK_PATH)
        if html2:
            info2 = self._parse_task_page(html2)
            if info2.get("state") == "tasking":
                return True, "二次确认：已进入任务中"
            if info2.get("state") == "excess":
                return False, "二次确认：名额已满"
        return False, f"未确认成功(state={state})"

    # ------------------------------------------------------------------ #
    # 解析
    # ------------------------------------------------------------------ #
    def _parse_task_page(self, html: str) -> Dict[str, Any]:
        state_raw = self._match_one(r'var\s+state\s*=\s*"([^"]+)"', html) or ""
        state_map = {
            "": "idle",
            "ok": "idle",
            "open": "idle",
            "excess": "excess",
            "tasking": "tasking",
            "close": "close",
        }
        state = state_map.get(state_raw, "idle" if not state_raw else state_raw)

        current = self._match_int(r"任务系统当前人数[：:]\s*(\d+)\s*人", html)
        limit = self._match_int(r"接受任务上限人数为\s*(\d+)\s*人", html) or self._slot_limit
        remaining = None
        if current is not None and limit is not None:
            remaining = max(0, int(limit) - int(current))
        if state == "excess":
            remaining = 0

        username = None
        m_user = re.search(
            r"欢迎回来\s*,\s*<a[^>]*userdetails\.php\?id=(\d+)[^>]*>\s*([^<]+)\s*</a>",
            html,
            re.I,
        )
        if not m_user:
            m_user = re.search(
                r"欢迎回来[\s\S]{0,80}?userdetails\.php\?id=(\d+)[^>]*>\s*([^<]+)",
                html,
                re.I,
            )
        uid = None
        if m_user:
            uid = m_user.group(1)
            username = re.sub(r"<[^>]+>", "", m_user.group(2)).strip()

        bonus = self._parse_number(
            self._match_one(r"魔力值[\s\S]{0,120}?[：:]\s*([\d,\.]+)", html)
        )
        seed_points = self._parse_number(
            self._match_one(r"做种积分\s*[：:][\s\S]{0,40}?([\d,\.]+)", html)
        )
        upload_gb = self._parse_size_to_gb(
            self._match_one(r"上传量[：:]\s*</font>\s*([\d,\.]+\s*[TGMK]?B?)", html)
            or self._match_one(r"上传量[：:]\s*([\d,\.]+\s*[TGMK]?B?)", html)
        )
        download_gb = self._parse_size_to_gb(
            self._match_one(r"下载量[：:]\s*</font>\s*([\d,\.]+\s*[TGMK]?B?)", html)
            or self._match_one(r"下载量[：:]\s*([\d,\.]+\s*[TGMK]?B?)", html)
        )

        tasks = self._parse_task_cards(html)
        my_done = self._parse_my_done(html)
        page_progress = self._parse_page_progress(html)

        return {
            "time": self._now_str(),
            "state": state,
            "state_raw": state_raw,
            "current": current,
            "limit": limit,
            "remaining": remaining,
            "username": username,
            "uid": uid,
            "bonus": bonus,
            "seed_points": seed_points,
            "upload_gb": upload_gb,
            "download_gb": download_gb,
            "tasks": tasks,
            "my_done": my_done,
            "page_progress": page_progress,
        }

    def _parse_page_progress(self, html: str) -> Dict[str, Any]:
        """尽量从任务页解析显式进度（不同版本 HTML 可能不同）。"""
        if not html:
            return {}
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        result: Dict[str, Any] = {}

        for key in self.TASK_KEY_TO_ID.keys():
            if re.search(rf"(当前任务|正在进行|进行中的任务|任务类型)\s*[：: ]\s*{key}", text, re.I):
                result["task_key"] = key
                break
            if re.search(rf"<h2>\s*{key}\s*</h2>[\s\S]{{0,200}}(进度|已完成|剩余)", html, re.I):
                result["task_key"] = key
                break

        def pair(label: str) -> Tuple[Optional[float], Optional[float]]:
            patterns = [
                rf"{label}\s*[：: ]\s*([\d,\.]+)\s*([TGMK]?B|G)?\s*/\s*([\d,\.]+)\s*([TGMK]?B|G)?",
                rf"{label}[^/\d]{{0,12}}([\d,\.]+)\s*([TGMK]?B|G)?\s*/\s*([\d,\.]+)\s*([TGMK]?B|G)?",
            ]
            for p in patterns:
                m = re.search(p, text, re.I)
                if not m:
                    continue
                cur = self._to_gb(m.group(1), m.group(2))
                target = self._to_gb(m.group(3), m.group(4))
                return cur, target
            return None, None

        up_c, up_t = pair("上传")
        if up_c is not None:
            result["upload_cur"], result["upload_target"] = up_c, up_t
        down_c, down_t = pair("下载")
        if down_c is not None:
            result["download_cur"], result["download_target"] = down_c, down_t

        m_seed = re.search(
            r"做种积分\s*[：: ]\s*([\d,\.]+)\s*/\s*([\d,\.]+)",
            text,
            re.I,
        )
        if m_seed:
            result["seed_cur"] = self._parse_number(m_seed.group(1))
            result["seed_target"] = self._parse_number(m_seed.group(2))

        for label, field in (("开始时间", "start_time"), ("结束时间", "end_time"), ("剩余时间", "remain_text")):
            m = re.search(rf"{label}\s*[：: ]\s*([0-9\-:\s天小时分秒]+)", text)
            if m:
                result[field] = m.group(1).strip()

        return result

    def _parse_progress_chart(self) -> Dict[str, Optional[float]]:
        """从 selfassessinfo.php 折线图取最新非空增量。"""
        html = self._get(self.INFO_PATH)
        if not html:
            return {}
        result: Dict[str, Optional[float]] = {}
        mapping = {"做种积分": "seed", "上传": "upload", "下载": "download"}
        for name, key in mapping.items():
            m = re.search(
                rf"name:\s*'{re.escape(name)}'\s*,\s*data:\s*\[([^\]]*)\]",
                html,
            )
            if not m:
                continue
            nums = re.findall(r"[-+]?\d*\.?\d+", m.group(1))
            if nums:
                result[key] = float(nums[-1])
        return result

    @classmethod
    def _parse_size_to_gb(cls, text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        m = re.search(r"([\d,\.]+)\s*([TGMK]?B?)?", str(text), re.I)
        if not m:
            return None
        return cls._to_gb(m.group(1), m.group(2))

    @classmethod
    def _to_gb(cls, num: Any, unit: Optional[str]) -> Optional[float]:
        val = cls._parse_number(str(num) if num is not None else None)
        if val is None:
            return None
        u = (unit or "G").upper().replace("B", "")
        if u == "T":
            return val * 1024
        if u == "M":
            return val / 1024
        if u == "K":
            return val / (1024 * 1024)
        return val  # G / 默认

    def _parse_task_cards(self, html: str) -> List[Dict[str, Any]]:
        tasks: List[Dict[str, Any]] = []
        for m in re.finditer(
            r'<form[^>]*action=["\']?selfassess\.php["\']?[^>]*>([\s\S]*?)</form>',
            html,
            re.I,
        ):
            body = m.group(1)
            if not re.search(r'value=["\']order["\']', body, re.I):
                continue
            if not re.search(r'name=["\']action["\']', body, re.I):
                continue
            id_m = re.search(
                r'value=["\'](\d+)["\'][^>]*name=["\']id["\']', body, re.I
            ) or re.search(r'name=["\']id["\'][^>]*value=["\'](\d+)["\']', body, re.I)
            if not id_m:
                continue
            task_id = int(id_m.group(1))
            key_m = re.search(r"<h2>\s*([^<]+)\s*</h2>", body, re.I)
            title_m = re.search(r"<span>\s*([^<]+)\s*</span>", body, re.I)

            def grab(label: str) -> Optional[str]:
                mm = re.search(rf"{label}[：:]\s*<span>\s*([^<]+)\s*</span>", body)
                return mm.group(1).strip() if mm else None

            fee_text = grab("报名费") or ""
            fee = self._match_int(r"(\d+)", fee_text)
            base = self.TASK_DEFS.get(task_id, {})
            tasks.append(
                {
                    "id": task_id,
                    "key": (key_m.group(1).strip() if key_m else base.get("key") or str(task_id)),
                    "title": (title_m.group(1).strip() if title_m else base.get("title") or ""),
                    "upload": grab("上传指标"),
                    "download": grab("下载指标"),
                    "seed_points": grab("做种积分指标"),
                    "days": grab("任务期限"),
                    "reward": grab("奖励魔力"),
                    "penalty": grab("失败扣除魔力"),
                    "fee": fee if fee is not None else base.get("fee"),
                    "fee_text": fee_text or str(base.get("fee") or ""),
                }
            )
        uniq: Dict[int, Dict[str, Any]] = {}
        for t in tasks:
            uniq[int(t["id"])] = t
        return [uniq[k] for k in sorted(uniq.keys())]

    def _parse_my_done(self, html: str) -> Dict[str, int]:
        result: Dict[str, int] = {}
        # 页面表格：Master / 3 这种个人完成次数
        for key in self.TASK_KEY_TO_ID.keys():
            m = re.search(rf">{re.escape(key)}\s*</t[dh]>\s*<t[dh][^>]*>\s*(\d+)\s*<", html, re.I)
            if not m:
                m = re.search(rf">{re.escape(key)}\s+(\d+)\s*(?:\n|<)", html)
            if m:
                result[key] = int(m.group(1))
        return result

    # ------------------------------------------------------------------ #
    # HTTP / 站点
    # ------------------------------------------------------------------ #
    def _list_chd_sites(self) -> List[Dict[str, Any]]:
        try:
            helper = SitesHelper()
            all_sites = [
                site for site in (helper.get_indexers() or []) if not site.get("public")
            ] + self.__custom_sites()
        except Exception as e:
            logger.error(f"{self.LOG_TAG}读取站点列表失败: {e}")
            return []
        return [site for site in all_sites if self._is_chd_indexer(site)]

    def __custom_sites(self) -> List[Any]:
        custom_sites = []
        try:
            custom_sites_config = self.get_config("CustomSites")
            if custom_sites_config and custom_sites_config.get("enabled"):
                custom_sites = custom_sites_config.get("sites") or []
        except Exception as e:
            logger.debug(f"{self.LOG_TAG}读取 CustomSites 失败: {e}")
        return custom_sites

    @classmethod
    def _is_chd_indexer(cls, site: Dict[str, Any]) -> bool:
        name = (site.get("name") or "").strip()
        domain = (site.get("domain") or "").lower()
        url = (site.get("url") or "").lower()
        name_l = name.lower()
        for n in cls.TARGET_SITE_NAMES:
            if n.lower() in name_l or name == n:
                return True
        return (
            "ptchdbits" in domain
            or "chdbits" in domain
            or "ptchdbits.co" in url
            or "chdbits.co" in url
        )

    @staticmethod
    def _normalize_site_id(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        if isinstance(value, list):
            value = value[0] if value else None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _load_site_auth(self) -> Tuple[bool, str]:
        if not self._site_id:
            return False, "未选择站点，请在配置中选择 CHDBits"
        sites = self._list_chd_sites()
        site = next((s for s in sites if int(s.get("id")) == int(self._site_id)), None)
        if not site:
            try:
                db_site = SiteOper().get(self._site_id)
            except Exception as e:
                logger.error(f"{self.LOG_TAG}读取站点失败: {e}")
                return False, f"读取站点失败: {e}"
            if not db_site:
                return False, "站点不存在，请重新选择"
            site = {
                "id": db_site.id,
                "name": db_site.name,
                "url": db_site.url,
                "cookie": db_site.cookie,
                "ua": db_site.ua,
                "proxy": db_site.proxy,
                "domain": db_site.domain,
            }
        if not self._is_chd_indexer(site):
            return False, f"当前仅支持 CHDBits，已选：{site.get('name')}"
        cookie = (site.get("cookie") or "").strip()
        if not cookie:
            return False, f"站点「{site.get('name')}」未配置 Cookie，请先在站点管理中更新"
        self._cookie = cookie
        self._ua = (site.get("ua") or "").strip() or self.DEFAULT_UA
        self._use_proxy = bool(site.get("proxy"))
        self._site_name = site.get("name") or "CHDBits"
        if site.get("url"):
            self.BASE_URL = str(site.get("url")).rstrip("/")
        self.save_data("site_name", self._site_name)
        logger.info(
            f"{self.LOG_TAG}已加载站点 {self._site_name}#{self._site_id}，"
            f"代理={'开' if self._use_proxy else '关'}，地址={self.BASE_URL}"
        )
        return True, "ok"

    def _proxies(self) -> Optional[dict]:
        if not self._use_proxy:
            return None
        return settings.PROXY

    def _request_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": self._ua or self.DEFAULT_UA,
            "Referer": f"{self.BASE_URL}{self.TASK_PATH}",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    def _get(self, path: str) -> Optional[str]:
        url = path if path.startswith("http") else urljoin(self.BASE_URL + "/", path.lstrip("/"))
        res = RequestUtils(
            cookies=self._cookie,
            proxies=self._proxies(),
            timeout=30,
            headers=self._request_headers(),
        ).get_res(url=url)
        if not res or res.status_code != 200:
            logger.warning(f"{self.LOG_TAG}GET 失败 {url}: {getattr(res, 'status_code', None)}")
            return None
        text = res.text or ""
        if "该页面必须在登录后才能访问" in text or "CHDBits :: 登录" in text:
            logger.error(f"{self.LOG_TAG}站点 Cookie 已失效")
            return None
        return text

    def _post(self, path: str, data: Any) -> Optional[str]:
        url = path if path.startswith("http") else urljoin(self.BASE_URL + "/", path.lstrip("/"))
        headers = self._request_headers()
        # 页面表单是 multipart，但字段极少；urlencoded 通常可用，失败时再试 multipart
        res = RequestUtils(
            cookies=self._cookie,
            proxies=self._proxies(),
            timeout=30,
            headers=headers,
        ).post_res(url=url, data=data)
        if res is not None:
            return res.text or ""
        # fallback multipart
        try:
            import requests

            files = {k: (None, str(v)) for k, v in dict(data).items()}
            resp = requests.post(
                url,
                files=files,
                cookies=self._cookie_dict(),
                headers={k: v for k, v in headers.items() if k.lower() != "content-type"},
                proxies=self._proxies(),
                timeout=30,
            )
            return resp.text or ""
        except Exception as e:
            logger.warning(f"{self.LOG_TAG}POST multipart 失败: {e}")
            return None

    def _cookie_dict(self) -> Dict[str, str]:
        result = {}
        for part in (self._cookie or "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            result[k.strip()] = v.strip()
        return result

    # ------------------------------------------------------------------ #
    # 存储 / 通知 / 工具
    # ------------------------------------------------------------------ #
    def _save_status(self, info: Dict[str, Any]):
        if info.get("username"):
            self.save_data("username", info["username"])
        if info.get("uid"):
            self.save_data("uid", info["uid"])
        self.save_data("last_status", info)
        self.save_data("last_run", {"time": info.get("time"), "state": info.get("state")})

    def _append_history(self, record: Dict[str, Any]):
        history = self.get_data("history") or []
        history.append(record)
        cutoff = datetime.now() - timedelta(days=self._history_days)
        kept = []
        for item in history:
            try:
                t = datetime.strptime(item.get("time", ""), "%Y-%m-%d %H:%M:%S")
                if t >= cutoff:
                    kept.append(item)
            except Exception:
                kept.append(item)
        self.save_data("history", kept[-200:])

    def _send_notification(self, title: str, text: str):
        if not self._notify:
            return
        try:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title=title,
                text=text,
            )
        except Exception as e:
            logger.warning(f"{self.LOG_TAG}发送通知失败: {e}")

    def _notify_event(self, kind: str, subtitle: str, fields: List[Tuple[str, str]]):
        """统一美化通知：含用户名与分区样式。"""
        icons = {
            "success": "✅",
            "error": "❌",
            "available": "🟢",
            "progress": "📊",
            "tasking": "ℹ️",
            "stop": "⏹",
            "info": "ℹ️",
        }
        icon = icons.get(kind, "ℹ️")
        title = f"【{icon} CHD抢任务 · {subtitle}】"
        username = (
            (self.get_data("last_status") or {}).get("username")
            or self.get_data("username")
            or "—"
        )
        site_name = self.get_data("site_name") or self._site_name or "CHDBits"
        lines = [
            "📢 执行结果",
            "━━━━━━━━━━",
            f"🕐 时间：{self._now_str()}",
            f"👤 用户：{username}",
            f"🏷 站点：{site_name}",
            f"✨ 状态：{subtitle}",
        ]
        for label, value in fields:
            if value is None or value == "":
                continue
            lines.append(f"• {label}：{value}")
        lines.append("━━━━━━━━━━")
        self._send_notification(title, "\n".join(lines))

    @staticmethod
    def _fmt_num(value: Any) -> str:
        if isinstance(value, (int, float)):
            return f"{value:,.1f}"
        return "—" if value is None else str(value)

    def _parse_preferred_tasks(self, value: Any) -> List[str]:
        if not value:
            return ["Extreme", "Insane", "Veteran"]
        if isinstance(value, str):
            value = [x.strip() for x in value.split(",") if x.strip()]
        result = []
        for item in value:
            key = str(item).strip()
            if key in self.TASK_KEY_TO_ID and key not in result:
                result.append(key)
        return result or ["Extreme", "Insane", "Veteran"]

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        try:
            if value is None or value == "":
                return default
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _match_one(pattern: str, text: str) -> Optional[str]:
        m = re.search(pattern, text, re.I)
        return m.group(1).strip() if m else None

    @classmethod
    def _match_int(cls, pattern: str, text: str) -> Optional[int]:
        m = re.search(pattern, text, re.I)
        if not m:
            return None
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            return None

    @staticmethod
    def _parse_number(text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        try:
            return float(str(text).replace(",", "").strip())
        except ValueError:
            return None

    def _now_str(self) -> str:
        return datetime.now(tz=pytz.timezone(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S")
