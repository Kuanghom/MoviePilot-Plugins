import math
import re
import random
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, quote

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


class HdskyDiceBet(_PluginBase):
    """HDSky 空论坛（掷骰子）自动下注插件。"""

    plugin_name = "空论坛掷骰子下注"
    plugin_desc = "自动参与 HDSky 掷骰子论坛下注；轻量历史压大小、倍投、开奖格子与评分短讯归档"
    plugin_icon = "hdskydicebet.png"
    plugin_version = "1.0.12"
    plugin_author = "Kuanghom"
    author_url = "https://github.com/Kuanghom"
    plugin_config_prefix = "hdskydicebet_"
    plugin_order = 25
    auth_level = 2

    LOG_TAG = "[HdskyDiceBet] "
    BASE_URL = "https://hdsky.me"
    FORUM_ID = 71
    DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    BET_TYPES = ("豹子", "顺子", "大", "小")
    # 官方近似赔率（净利润倍数，几乎不抽水）
    ODDS = {"大": 1.29, "小": 1.29, "顺子": 7.8, "豹子": 33.0}
    # 三枚骰子古典概型（豹子 > 顺子 > 大小）
    CLASSICAL_COUNT = {"豹子": 6, "顺子": 24, "大": 93, "小": 93}
    CLASSICAL_TOTAL = 216
    # 智能主注仅大小；高赔为可选加注
    SMART_BASE_TYPES = ("大", "小")
    SMART_EXTRA_TYPES = ("顺子", "豹子")
    # 连开大于 2（即 >=3）才强反压
    SMART_REVERSE_STREAK = 3
    # 轻量历史近窗长度
    SMART_LIGHT_WINDOW = 20
    # 单侧 z 偏低阈值（约 15%）；罕见事件另有 k=0 / 半期望 兜底
    SMART_Z_THRESHOLD = -1.04
    SMART_EXTRA_MIN_ROUNDS = 20
    RATING_PM_RE = re.compile(r"管理员在您的帖子#\d+评分")
    RATING_PM_BOX_NAME = "论坛投注"
    TOPIC_TITLE_RE = re.compile(
        r"本轮开奖时间:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"
        r"(?:\s*【\s*(豹子|顺子|大|小)\s+([\d,]+)\s*】)?"
    )
    BET_BODY_RE = re.compile(
        r"^(豹子|顺子|大|小)\s+(?:\u00a0|\s)*(\d+(?:\.\d+)?[wW]?)\s*$",
        re.I,
    )
    RESULT_IN_TITLE_RE = re.compile(r"【\s*(豹子|顺子|大|小)\s+")
    # 群聊区同款：按站名白名单过滤（天空）
    TARGET_SITE_NAMES = ("天空",)

    _enabled = False
    _notify = False
    _onlyonce = False
    _cron = "*/3 * * * *"
    _site_id: Optional[int] = None
    _cookie = ""
    _ua = DEFAULT_UA
    _use_proxy = True
    _site_name = ""
    _bet_mode = "smart"  # fixed / random / smart / martingale
    _fixed_types: List[str] = ["大"]
    _bet_amount = 100
    _amount_by_type: Dict[str, int] = {}
    _reply_interval = 30
    _max_daily_bets: Optional[int] = None
    _max_daily_tickets: Optional[int] = None
    _smart_history_rounds = 50
    _smart_allow_shunzi = False
    _smart_allow_baozi = False
    _martingale_start_side = "大"
    _cleanup_rating_pm = True
    _history_days = 90
    _username = ""
    _scheduler: Optional[BackgroundScheduler] = None
    _run_lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._cron = (config.get("cron") or "*/3 * * * *").strip()
        self._site_id = self._normalize_site_id(config.get("site_id"))
        self._bet_mode = config.get("bet_mode") or "smart"
        self._fixed_types = self._parse_fixed_types(config)
        self._bet_amount = self._clamp_amount(config.get("bet_amount", 100))
        self._amount_by_type = self._parse_amount_by_type(config)
        self._reply_interval = self._clamp_interval(config.get("reply_interval", 30))
        self._max_daily_bets = self._to_optional_int(config.get("max_daily_bets"))
        self._max_daily_tickets = self._to_optional_int(config.get("max_daily_tickets"))
        self._smart_history_rounds = max(10, int(config.get("smart_history_rounds") or 50))
        self._smart_allow_shunzi = bool(config.get("smart_allow_shunzi"))
        self._smart_allow_baozi = bool(config.get("smart_allow_baozi"))
        side = (config.get("martingale_start_side") or "大").strip()
        self._martingale_start_side = side if side in ("大", "小") else "大"
        self._cleanup_rating_pm = bool(config.get("cleanup_rating_pm", True))
        self._history_days = max(7, int(config.get("history_days") or 90))
        self._username = (self.get_data("username") or "").strip()

        # 未配置时，若站点管理里只有一个天空站，则自动选中
        if not self._site_id:
            hdsky_sites = self._list_hdsky_sites()
            if len(hdsky_sites) == 1:
                self._site_id = int(hdsky_sites[0].get("id"))
                config["site_id"] = self._site_id
                self.update_config(config)
                logger.info(f"{self.LOG_TAG}自动选中站点: {hdsky_sites[0].get('name')}#{self._site_id}")

        # 过滤已删除站点
        if self._site_id:
            valid_ids = {int(s.get("id")) for s in self._list_hdsky_sites() if s.get("id") is not None}
            if valid_ids and self._site_id not in valid_ids:
                logger.warning(f"{self.LOG_TAG}已选站点 {self._site_id} 不在可用天空站列表中，请重新选择")


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
                name="空论坛掷骰子立即执行",
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
                "id": "HdskyDiceBet.Run",
                "name": "空论坛掷骰子下注",
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
            for site in self._list_hdsky_sites()
        ]
        site_alert = None
        if not site_options:
            site_alert = {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "text": "未在站点管理中找到天空（hdsky.me）。请先添加并配置 Cookie 后再选择。",
                },
            }
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
                                    "hint": "下注成功/失败、开奖盈亏、达每日次数或观影券上限停止时推送",
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
                                    "hint": "从站点管理读取天空（HDSky）的 Cookie / UA / 代理",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    }
                ],
            },
        ]
        if site_alert:
            form_content.append({"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [site_alert]}]})
        form_content.extend(
            [
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VSelect",
                                    "props": {
                                        "model": "bet_mode",
                                        "label": "下注模式",
                                        "items": [
                                            {"title": "固定类型(可多选同帖多注)", "value": "fixed"},
                                            {"title": "随机类型", "value": "random"},
                                            {"title": "智能下注(轻量历史压大小)", "value": "smart"},
                                            {"title": "倍投(固定一侧，开出换边)", "value": "martingale"},
                                        ],
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VSelect",
                                    "props": {
                                        "model": "fixed_types",
                                        "label": "下注类型",
                                        "multiple": True,
                                        "chips": True,
                                        "items": [
                                            {"title": t, "value": t} for t in self.BET_TYPES
                                        ],
                                        "hint": "固定/随机模式用；智能模式主注固定在「大/小」，不受此项限制",
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
                                        "model": "bet_amount",
                                        "label": "默认下注金额",
                                        "type": "number",
                                        "hint": "范围 100 ~ 100000；下方未单独填写的类型用此金额",
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
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "smart_allow_shunzi",
                                        "label": "智能允许加注顺子",
                                        "color": "primary",
                                        "hint": "仅智能模式：近期偏冷（含长时间未出）时额外下一注顺子",
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
                                        "model": "smart_allow_baozi",
                                        "label": "智能允许加注豹子",
                                        "color": "primary",
                                        "hint": "仅智能模式：近期偏冷（含长时间未出）时额外下一注豹子",
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
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "density": "compact",
                                        "text": "智能：连开≥3反压，否则压近窗短缺侧。倍投：固定一侧，输×2，开出后换边并回默认金额。",
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
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VSelect",
                                    "props": {
                                        "model": "martingale_start_side",
                                        "label": "倍投起始侧",
                                        "items": [
                                            {"title": "大", "value": "大"},
                                            {"title": "小", "value": "小"},
                                        ],
                                        "hint": "仅倍投模式：无状态时的起始方向；开出后自动换另一侧",
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
                                        "model": "cleanup_rating_pm",
                                        "label": "评分短讯已读并归档",
                                        "color": "primary",
                                        "hint": "将「管理员在您的帖子#…评分」标已读并移至「论坛投注」短讯箱（没有则自动创建）",
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
                            "props": {"cols": 12, "md": 3},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "amount_大",
                                        "label": "大 · 金额",
                                        "type": "number",
                                        "placeholder": "默认金额",
                                        "hint": "留空则用默认下注金额",
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
                                    "component": "VTextField",
                                    "props": {
                                        "model": "amount_小",
                                        "label": "小 · 金额",
                                        "type": "number",
                                        "placeholder": "默认金额",
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "amount_顺子",
                                        "label": "顺子 · 金额",
                                        "type": "number",
                                        "placeholder": "默认金额",
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "amount_豹子",
                                        "label": "豹子 · 金额",
                                        "type": "number",
                                        "placeholder": "默认金额",
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
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": cron_field,
                                    "props": {
                                        "model": "cron",
                                        "label": "执行周期",
                                        "placeholder": "*/3 * * * *",
                                        "hint": "建议每 2~5 分钟检查一轮",
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
                                        "model": "reply_interval",
                                        "label": "同帖多注间隔(秒)",
                                        "type": "number",
                                        "hint": "同一帖连续回复间隔，默认 30，避免刷帖限制",
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
                                        "model": "max_daily_bets",
                                        "label": "每日最大下注次数",
                                        "placeholder": "不填则不限制",
                                        "hint": "按自然天统计已成功下注次数（多注各计 1 次）",
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
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "max_daily_tickets",
                                        "label": "每日观影券次数上限",
                                        "placeholder": "不填则不限制",
                                        "hint": "评论获得「观影随机续期奖励」按自然天累计，达上限则停止下注",
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
                                        "model": "smart_history_rounds",
                                        "label": "智能策略参考历史轮数",
                                        "type": "number",
                                        "hint": "默认 50；智能加注顺子/豹子与开奖格子参考轮数",
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
                                        "label": "本地下注记录保留天数",
                                        "type": "number",
                                        "placeholder": "90",
                                    },
                                }
                            ],
                        },
                    ],
                },
            ]
        )
        return [
            {
                "component": "VForm",
                "content": form_content,
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "site_id": self._site_id,
            "bet_mode": "smart",
            "fixed_types": ["大"],
            "bet_amount": 100,
            "amount_大": "",
            "amount_小": "",
            "amount_顺子": "",
            "amount_豹子": "",
            "smart_allow_shunzi": False,
            "smart_allow_baozi": False,
            "martingale_start_side": "大",
            "cleanup_rating_pm": True,
            "reply_interval": 30,
            "cron": "*/3 * * * *",
            "max_daily_bets": "",
            "max_daily_tickets": "",
            "smart_history_rounds": 50,
            "history_days": 90,
        }

    def get_page(self) -> List[dict]:
        history = self.get_data("history") or []
        last_run = self.get_data("last_run") or {}
        username = self.get_data("username") or self._username or "—"
        site_name = self.get_data("site_name") or self._site_name or "天空"
        today = self._today_str()
        day_pl = self._summarize_pl(history, "day")
        week_pl = self._summarize_pl(history, "week")
        month_pl = self._summarize_pl(history, "month")
        all_pl = self._summarize_pl(history, "all")
        tickets_today = int((self.get_data("tickets_by_day") or {}).get(today, 0) or 0)
        bets_today = self._count_bets_on(history, today)
        pending_cnt = sum(1 for h in history if h.get("profit") is None)
        win_rate = (
            f"{(all_pl.get('wins', 0) / all_pl.get('settled', 1) * 100):.0f}%"
            if all_pl.get("settled")
            else "—"
        )
        next_run = self._next_cron_time()

        def pl_color(v: float) -> str:
            if v > 0:
                return "success"
            if v < 0:
                return "error"
            return "secondary"

        mode_map = {
            "fixed": "固定",
            "random": "随机",
            "smart": "智能",
            "martingale": "倍投",
            "manual": "手动",
        }
        rows = []
        for item in sorted(history, key=lambda x: x.get("time", ""), reverse=True)[:100]:
            profit = item.get("profit")
            if profit is None:
                status_text, status_color = "待结算", "warning"
                profit_text = "—"
            elif int(profit) > 0:
                status_text, status_color = "盈利", "success"
                profit_text = f"+{int(profit)}"
            elif int(profit) < 0:
                status_text, status_color = "亏损", "error"
                profit_text = str(int(profit))
            else:
                status_text, status_color = "持平", "secondary"
                profit_text = "0"
            rows.append(
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
                                        "color": status_color,
                                        "size": "small",
                                        "variant": "flat",
                                    },
                                    "text": status_text,
                                }
                            ],
                        },
                        {
                            "component": "td",
                            "content": [
                                {
                                    "component": "a",
                                    "props": {
                                        "href": item.get("url") or "#",
                                        "target": "_blank",
                                    },
                                    "text": f"#{item.get('topic_id', '')}",
                                }
                            ],
                        },
                        {
                            "component": "td",
                            "text": f"{item.get('bet_type', '')} {item.get('amount', '')}",
                        },
                        {
                            "component": "td",
                            "text": mode_map.get(str(item.get("mode")), str(item.get("mode") or "—")),
                        },
                        {"component": "td", "text": str(item.get("result") or "—")},
                        {
                            "component": "td",
                            "content": [
                                {
                                    "component": "span",
                                    "props": {
                                        "class": f"text-{status_color}"
                                        if profit is not None
                                        else ""
                                    },
                                    "text": profit_text,
                                }
                            ],
                        },
                        {
                            "component": "td",
                            "text": "🎫" if item.get("got_ticket") else "—",
                        },
                    ],
                }
            )

        draw_history = self.get_data("draw_history") or []
        mg = self.get_data("martingale") or {}
        mg_text = (
            f"倍投状态：{mg.get('side') or self._martingale_start_side} "
            f"{mg.get('amount') or self._bet_amount}"
            if (self._bet_mode or "").lower() == "martingale" or mg
            else ""
        )

        page_blocks = [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "class": "mb-2"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "d-flex flex-wrap ga-4"},
                                        "content": [
                                            {
                                                "component": "div",
                                                "text": f"站点：{site_name}",
                                            },
                                            {"component": "div", "text": f"用户：{username}"},
                                            {
                                                "component": "div",
                                                "text": f"上次执行：{last_run.get('time', '—')}",
                                            },
                                            {
                                                "component": "div",
                                                "text": f"下次周期：{next_run}",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-medium-emphasis"},
                                                "text": str(last_run.get("message") or ""),
                                            },
                                            *(
                                                [
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-primary"},
                                                        "text": mg_text,
                                                    }
                                                ]
                                                if mg_text
                                                else []
                                            ),
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            {
                "component": "VRow",
                "content": [
                    self._metric_card(
                        "今日盈亏",
                        f"{day_pl.get('profit', 0):+d}",
                        f"已结算 {day_pl.get('settled', 0)} / 下注 {day_pl.get('bets', 0)}",
                        pl_color(day_pl.get("profit", 0)),
                        "mdi-calendar-today",
                    ),
                    self._metric_card(
                        "本周盈亏",
                        f"{week_pl.get('profit', 0):+d}",
                        f"胜 {week_pl.get('wins', 0)} / 负 {week_pl.get('losses', 0)}",
                        pl_color(week_pl.get("profit", 0)),
                        "mdi-calendar-week",
                    ),
                    self._metric_card(
                        "本月盈亏",
                        f"{month_pl.get('profit', 0):+d}",
                        f"胜 {month_pl.get('wins', 0)} / 负 {month_pl.get('losses', 0)}",
                        pl_color(month_pl.get("profit", 0)),
                        "mdi-calendar-month",
                    ),
                    self._metric_card(
                        "今日下注",
                        str(bets_today)
                        + (f" / {self._max_daily_bets}" if self._max_daily_bets else ""),
                        f"待结算 {pending_cnt} 笔",
                        "primary",
                        "mdi-dice-multiple",
                    ),
                    self._metric_card(
                        "累计胜率",
                        win_rate,
                        f"胜 {all_pl.get('wins', 0)} / 负 {all_pl.get('losses', 0)}",
                        "info",
                        "mdi-chart-line",
                    ),
                    self._metric_card(
                        "今日观影券",
                        str(tickets_today)
                        + (
                            f" / {self._max_daily_tickets}"
                            if self._max_daily_tickets
                            else ""
                        ),
                        "自然天累计",
                        "warning",
                        "mdi-ticket-confirmation",
                    ),
                ],
            },
            self._build_draw_history_card(draw_history),
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "outlined"},
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "props": {"class": "d-flex align-center"},
                                        "content": [
                                            {
                                                "component": "VIcon",
                                                "props": {"class": "mr-2"},
                                                "text": "mdi-history",
                                            },
                                            {"component": "span", "text": "空论坛下注历史"},
                                        ],
                                    },
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {
                                                "component": "VTable",
                                                "props": {
                                                    "hover": True,
                                                    "density": "compact",
                                                },
                                                "content": [
                                                    {
                                                        "component": "thead",
                                                        "content": [
                                                            {
                                                                "component": "tr",
                                                                "content": [
                                                                    {"component": "th", "text": "时间"},
                                                                    {"component": "th", "text": "状态"},
                                                                    {"component": "th", "text": "帖子"},
                                                                    {"component": "th", "text": "下注"},
                                                                    {"component": "th", "text": "模式"},
                                                                    {"component": "th", "text": "开奖"},
                                                                    {"component": "th", "text": "盈亏"},
                                                                    {"component": "th", "text": "观影券"},
                                                                ],
                                                            }
                                                        ],
                                                    },
                                                    {
                                                        "component": "tbody",
                                                        "content": rows
                                                        or [
                                                            {
                                                                "component": "tr",
                                                                "content": [
                                                                    {
                                                                        "component": "td",
                                                                        "props": {"colspan": 8},
                                                                        "text": "暂无下注记录",
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
        return page_blocks

    def _build_draw_history_card(self, draw_history: List[dict]) -> dict:
        """最近 50 局开奖格子（样式对齐蜂巢签到日历）。"""
        # 存的是新→旧，展示时左→右、上→下为旧→新更易读走势
        items = list(reversed(draw_history[:50]))
        cols = 10
        color_map = {
            "大": ("rgba(76, 175, 80, 0.18)", "#2E7D32"),
            "小": ("rgba(33, 150, 243, 0.18)", "#1565C0"),
            "顺子": ("rgba(255, 152, 0, 0.18)", "#EF6C00"),
            "豹子": ("rgba(244, 67, 54, 0.18)", "#C62828"),
        }
        grid_rows = []
        if not items:
            grid_rows.append(
                {
                    "component": "div",
                    "props": {"class": "text-medium-emphasis text-caption"},
                    "text": "暂无开奖缓存，执行一次任务后自动刷新",
                }
            )
        else:
            for i in range(0, len(items), cols):
                chunk = items[i : i + cols]
                cells = []
                for idx, entry in enumerate(chunk):
                    result = str(entry.get("result") or "?")
                    short = {"大": "大", "小": "小", "顺子": "顺", "豹子": "豹"}.get(
                        result, result[:1]
                    )
                    bg, fg = color_map.get(result, ("rgba(158,158,158,0.12)", "#616161"))
                    seq = i + idx + 1
                    cells.append(
                        {
                            "component": "div",
                            "props": {
                                "class": "d-flex justify-center align-center",
                                "style": f"width: {100 / cols}%;",
                            },
                            "content": [
                                {
                                    "component": "div",
                                    "props": {
                                        "class": "d-flex flex-column justify-center align-center",
                                        "style": (
                                            f"width: 36px; height: 36px; border-radius: 6px; "
                                            f"background-color: {bg}; color: {fg}; margin: 2px;"
                                        ),
                                        "title": (
                                            f"#{entry.get('topic_id', '')} "
                                            f"{entry.get('draw_time') or ''} {result}"
                                        ),
                                    },
                                    "content": [
                                        {
                                            "component": "div",
                                            "props": {
                                                "style": "font-size: 0.6rem; line-height: 1; opacity: 0.65;"
                                            },
                                            "text": str(seq),
                                        },
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "font-weight-bold",
                                                "style": "font-size: 0.85rem; line-height: 1.1;",
                                            },
                                            "text": short,
                                        },
                                    ],
                                }
                            ],
                        }
                    )
                # 末行补空位对齐
                while len(cells) < cols:
                    cells.append(
                        {
                            "component": "div",
                            "props": {"style": f"width: {100 / cols}%; height: 40px;"},
                        }
                    )
                grid_rows.append(
                    {
                        "component": "div",
                        "props": {"class": "d-flex justify-space-between mb-1"},
                        "content": cells,
                    }
                )

        legend = {
            "component": "div",
            "props": {
                "class": "d-flex justify-center mt-2 flex-wrap",
                "style": "font-size: 0.75rem; gap: 10px;",
            },
            "content": [
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center"},
                    "content": [
                        {
                            "component": "div",
                            "props": {
                                "style": "width: 8px; height: 8px; border-radius: 50%; "
                                "background-color: #4CAF50; margin-right: 4px;"
                            },
                        },
                        {"component": "span", "text": "大"},
                    ],
                },
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center"},
                    "content": [
                        {
                            "component": "div",
                            "props": {
                                "style": "width: 8px; height: 8px; border-radius: 50%; "
                                "background-color: #2196F3; margin-right: 4px;"
                            },
                        },
                        {"component": "span", "text": "小"},
                    ],
                },
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center"},
                    "content": [
                        {
                            "component": "div",
                            "props": {
                                "style": "width: 8px; height: 8px; border-radius: 50%; "
                                "background-color: #FF9800; margin-right: 4px;"
                            },
                        },
                        {"component": "span", "text": "顺子"},
                    ],
                },
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center"},
                    "content": [
                        {
                            "component": "div",
                            "props": {
                                "style": "width: 8px; height: 8px; border-radius: 50%; "
                                "background-color: #F44336; margin-right: 4px;"
                            },
                        },
                        {"component": "span", "text": "豹子"},
                    ],
                },
            ],
        }
        grid_rows.append(legend)

        return {
            "component": "VRow",
            "content": [
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 6},
                    "content": [
                        {
                            "component": "VCard",
                            "props": {"variant": "outlined", "class": "mb-2"},
                            "content": [
                                {
                                    "component": "VCardTitle",
                                    "props": {
                                        "class": "d-flex align-center justify-center",
                                        "style": "position: relative;",
                                    },
                                    "content": [
                                        {
                                            "component": "VIcon",
                                            "props": {
                                                "class": "mr-2",
                                                "style": "position: absolute; left: 16px;",
                                            },
                                            "text": "mdi-dice-5",
                                        },
                                        {
                                            "component": "span",
                                            "text": f"开奖历史（最近 {len(items)} / 50 局）",
                                        },
                                    ],
                                },
                                {
                                    "component": "VCardText",
                                    "content": grid_rows,
                                },
                            ],
                        }
                    ],
                }
            ],
        }

    @staticmethod
    def _metric_card(
        title: str, value: str, subtitle: str, color: str, icon: str
    ) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 12, "sm": 6, "md": 4, "lg": 2},
            "content": [
                {
                    "component": "VCard",
                    "props": {"variant": "tonal", "color": color, "class": "mb-2"},
                    "content": [
                        {
                            "component": "VCardText",
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "d-flex align-center mb-1"},
                                    "content": [
                                        {
                                            "component": "VIcon",
                                            "props": {"size": "small", "class": "mr-1"},
                                            "text": icon,
                                        },
                                        {
                                            "component": "span",
                                            "props": {"class": "text-caption"},
                                            "text": title,
                                        },
                                    ],
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-h5 font-weight-bold"},
                                    "text": value,
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption text-medium-emphasis"},
                                    "text": subtitle,
                                },
                            ],
                        }
                    ],
                }
            ],
        }

    @staticmethod
    def _summary_card(title: str, summary: Dict[str, Any], color: str) -> dict:
        profit = summary.get("profit", 0)
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": 3},
            "content": [
                {
                    "component": "VCard",
                    "props": {"variant": "tonal", "color": color},
                    "content": [
                        {
                            "component": "VCardText",
                            "content": [
                                {"component": "div", "props": {"class": "text-subtitle-2"}, "text": title},
                                {
                                    "component": "div",
                                    "props": {"class": "text-h5"},
                                    "text": f"{profit:+d}",
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption"},
                                    "text": f"下注 {summary.get('bets', 0)} 次 | "
                                    f"已结算 {summary.get('settled', 0)} | "
                                    f"胜 {summary.get('wins', 0)} 负 {summary.get('losses', 0)}",
                                },
                            ],
                        }
                    ],
                }
            ],
        }

    # ------------------------------------------------------------------ #
    # 通知（风格对齐蜂巢签到）
    # ------------------------------------------------------------------ #
    def _send_notification(self, title: str, text: str):
        if not self._notify:
            return
        self.post_message(
            mtype=NotificationType.SiteMessage,
            title=title,
            text=text,
        )

    def _notify_bet_success(self, record: Dict[str, Any]):
        today = self._today_str()
        history = self.get_data("history") or []
        day_pl = self._summarize_pl(history, "day")
        bets_today = self._count_bets_on(history, today)
        tickets_today = int((self.get_data("tickets_by_day") or {}).get(today, 0))
        mode_map = {"fixed": "固定", "random": "随机", "smart": "智能", "martingale": "倍投", "manual": "手动"}
        mode_text = mode_map.get(str(record.get("mode")), str(record.get("mode")))
        limit_bet = f" / {self._max_daily_bets}" if self._max_daily_bets else ""
        limit_ticket = f" / {self._max_daily_tickets}" if self._max_daily_tickets else ""
        self._send_notification(
            title="【✅ 空论坛下注成功】",
            text=(
                f"📢 执行结果\n"
                f"━━━━━━━━━━\n"
                f"🕐 时间：{record.get('time') or self._now_str()}\n"
                f"✨ 状态：下注成功\n"
                f"🎲 类型：{record.get('bet_type')} {record.get('amount')}\n"
                f"🧠 模式：{mode_text}\n"
                f"📌 帖子：#{record.get('topic_id')}\n"
                f"⏳ 开奖：{record.get('draw_time') or '—'}\n"
                f"━━━━━━━━━━\n"
                f"📊 今日统计\n"
                f"🧾 下注：{bets_today}{limit_bet} 次\n"
                f"🎫 观影券：{tickets_today}{limit_ticket}\n"
                f"💰 今日盈亏：{day_pl.get('profit', 0):+d}\n"
                f"━━━━━━━━━━"
            ),
        )

    def _notify_bet_failure(self, topic_id: str, reason: str):
        self._send_notification(
            title="【❌ 空论坛下注失败】",
            text=(
                f"📢 执行结果\n"
                f"━━━━━━━━━━\n"
                f"🕐 时间：{self._now_str()}\n"
                f"❌ 状态：下注失败\n"
                f"📌 帖子：#{topic_id}\n"
                f"💬 原因：{reason}\n"
                f"━━━━━━━━━━"
            ),
        )

    def _notify_settlement(self, item: Dict[str, Any]):
        profit = int(item.get("profit") or 0)
        won = profit > 0
        title = "【🎉 空论坛开奖盈利】" if won else (
            "【💔 空论坛开奖亏损】" if profit < 0 else "【ℹ️ 空论坛已开奖】"
        )
        status = "猜中盈利" if won else ("未中亏损" if profit < 0 else "已结算")
        day_pl = self._summarize_pl(self.get_data("history") or [], "day")
        week_pl = self._summarize_pl(self.get_data("history") or [], "week")
        self._send_notification(
            title=title,
            text=(
                f"📢 执行结果\n"
                f"━━━━━━━━━━\n"
                f"🕐 时间：{self._now_str()}\n"
                f"✨ 状态：{status}\n"
                f"🎲 下注：{item.get('bet_type')} {item.get('amount')}\n"
                f"🏆 开奖：{item.get('result') or '—'}\n"
                f"💵 本局盈亏：{profit:+d}\n"
                f"📌 帖子：#{item.get('topic_id')}\n"
                f"━━━━━━━━━━\n"
                f"📊 汇总\n"
                f"📅 今日盈亏：{day_pl.get('profit', 0):+d}\n"
                f"🗓️ 本周盈亏：{week_pl.get('profit', 0):+d}\n"
                f"🎫 观影券：{'是' if item.get('got_ticket') else '否'}\n"
                f"━━━━━━━━━━"
            ),
        )

    def _notify_daily_limit_stop(self, kind: str, current: int, limit: int):
        """
        因每日下注次数 / 观影券达上限停止时通知。
        kind: bets | tickets；同一自然日每种原因只发一次，避免 cron 刷屏。
        """
        today = self._today_str()
        state = dict(self.get_data("limit_stop_notified") or {})
        if state.get("date") != today:
            state = {"date": today}
        if state.get(kind):
            return
        state[kind] = True
        self.save_data("limit_stop_notified", state)

        if kind == "bets":
            reason = "每日下注次数已达上限"
            detail = f"🧾 今日下注：{current} / {limit} 次"
            title = "【⏸️ 空论坛停止下注】"
        else:
            reason = "每日观影券次数已达上限"
            detail = f"🎫 今日观影券：{current} / {limit}"
            title = "【⏸️ 空论坛停止下注】"

        day_pl = self._summarize_pl(self.get_data("history") or [], "day")
        self._send_notification(
            title=title,
            text=(
                f"📢 执行结果\n"
                f"━━━━━━━━━━\n"
                f"🕐 时间：{self._now_str()}\n"
                f"⏸️ 状态：已停止自动下注\n"
                f"💬 原因：{reason}\n"
                f"{detail}\n"
                f"💰 今日盈亏：{day_pl.get('profit', 0):+d}\n"
                f"━━━━━━━━━━\n"
                f"ℹ️ 明日计数重置后将自动恢复\n"
                f"━━━━━━━━━━"
            ),
        )
        logger.info(f"{self.LOG_TAG}已通知达上限停止 kind={kind} {current}/{limit}")

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def run_once(self):
        if not self._run_lock.acquire(blocking=False):
            logger.warning(f"{self.LOG_TAG}上一次任务仍在执行，跳过")
            return
        try:
            next_run = self._next_cron_time()
            logger.info(f"{self.LOG_TAG}====== 开始执行 ======")
            logger.info(
                f"{self.LOG_TAG}配置: mode={self._bet_mode} types={self._fixed_types} "
                f"default_amount={self._bet_amount} amounts={self._amount_by_type} "
                f"interval={self._reply_interval}s "
                f"smart_extra=顺子:{self._smart_allow_shunzi}/豹子:{self._smart_allow_baozi} "
                f"martingale_side={self._martingale_start_side} "
                f"cleanup_pm={self._cleanup_rating_pm} "
                f"site_id={self._site_id} cron={self._cron} 下次周期≈{next_run}"
            )
            message = self._run_internal()
            self.save_data(
                "last_run",
                {
                    "time": self._now_str(),
                    "message": message,
                    "next_run": next_run,
                },
            )
            logger.info(f"{self.LOG_TAG}执行结果: {message}")
            logger.info(f"{self.LOG_TAG}====== 结束，下次周期≈{next_run} ======")
        except Exception as e:
            logger.error(f"{self.LOG_TAG}执行异常: {e}", exc_info=True)
            self.save_data(
                "last_run",
                {"time": self._now_str(), "message": f"异常: {e}"},
            )
            self._send_notification(
                title="【❌ 空论坛下注异常】",
                text=(
                    f"📢 执行结果\n"
                    f"━━━━━━━━━━\n"
                    f"🕐 时间：{self._now_str()}\n"
                    f"❌ 状态：执行异常\n"
                    f"💬 原因：{e}\n"
                    f"━━━━━━━━━━"
                ),
            )
        finally:
            self._run_lock.release()

    def _run_internal(self) -> str:
        ok, msg = self._load_site_auth()
        if not ok:
            return msg
        if not self._ensure_username():
            return "站点 Cookie 无效或无法识别用户名"
        logger.info(f"{self.LOG_TAG}当前用户={self._username}")

        # 先同步未结算记录与观影券
        self._sync_pending_results()
        self._refresh_today_tickets()

        if self._cleanup_rating_pm:
            try:
                pm_msg = self._cleanup_rating_messages()
                if pm_msg:
                    logger.info(f"{self.LOG_TAG}评分短讯处理: {pm_msg}")
            except Exception as e:
                logger.warning(f"{self.LOG_TAG}评分短讯处理失败: {e}")

        today = self._today_str()
        history = self.get_data("history") or []
        bets_today = self._count_bets_on(history, today)
        tickets_today = int((self.get_data("tickets_by_day") or {}).get(today, 0))
        logger.info(
            f"{self.LOG_TAG}今日统计: 下注={bets_today}"
            f"{'/' + str(self._max_daily_bets) if self._max_daily_bets else ''} "
            f"观影券={tickets_today}"
            f"{'/' + str(self._max_daily_tickets) if self._max_daily_tickets else ''}"
        )

        if self._max_daily_bets is not None and bets_today >= self._max_daily_bets:
            self._notify_daily_limit_stop("bets", bets_today, self._max_daily_bets)
            return f"已达每日下注上限 {self._max_daily_bets}"
        if self._max_daily_tickets is not None and tickets_today >= self._max_daily_tickets:
            self._notify_daily_limit_stop(
                "tickets", tickets_today, self._max_daily_tickets
            )
            return f"已达每日观影券上限 {self._max_daily_tickets}（今日 {tickets_today}）"

        topics = self._list_forum_topics(pages=2)
        self._refresh_draw_history(topics)
        open_topics = [t for t in topics if t.get("open")]
        logger.info(
            f"{self.LOG_TAG}论坛主题: 解析={len(topics)}，可下注={len(open_topics)}，"
            f"已开奖={sum(1 for t in topics if t.get('result'))}"
        )
        for t in topics[:8]:
            logger.debug(
                f"{self.LOG_TAG}  topic#{t.get('topic_id')} open={t.get('open')} "
                f"locked={t.get('locked')} result={t.get('result')} draw={t.get('draw_time')}"
            )
        if not open_topics:
            return "当前没有可下注帖子"

        # 倍投：有未结算倍投单时不再追新帖，避免同轮多注破坏倍投链
        mode = (self._bet_mode or "").lower()
        if mode == "martingale" and self._martingale_has_pending():
            return "倍投有未结算注单，等待开奖后再下"

        # 优先最早开奖的开放帖
        open_topics.sort(key=lambda x: x.get("draw_time") or "")
        acted = []
        for topic in open_topics:
            if self._max_daily_bets is not None and self._count_bets_on(
                self.get_data("history") or [], today
            ) >= self._max_daily_bets:
                self._notify_daily_limit_stop(
                    "bets",
                    self._count_bets_on(self.get_data("history") or [], today),
                    self._max_daily_bets,
                )
                break
            if self._max_daily_tickets is not None:
                tickets_now = int((self.get_data("tickets_by_day") or {}).get(today, 0))
                if tickets_now >= self._max_daily_tickets:
                    self._notify_daily_limit_stop(
                        "tickets", tickets_now, self._max_daily_tickets
                    )
                    break
            # 二次确认帖内是否已下注 / 是否已锁定
            detail = self._fetch_topic_detail(topic["topic_id"])
            if not detail:
                continue
            if detail.get("locked") or detail.get("result"):
                logger.debug(f"{self.LOG_TAG}主题#{topic['topic_id']} 已锁定/已开奖，跳过")
                continue

            # 帖内已有自己的楼层：补记本地，并据此决定还缺哪些类型
            forum_types = set()
            self_bets = detail.get("self_bets") or []
            if not self_bets and detail.get("self_bet"):
                self_bets = [detail["self_bet"]]
            for sb in self_bets:
                bt = sb.get("bet_type")
                if not bt:
                    continue
                forum_types.add(bt)
                self._remember_existing_bet(topic, sb)

            plans = self._resolve_bet_plans(topics)
            local_types = self._bet_types_on_topic(topic["topic_id"])
            done_types = local_types | forum_types
            # 智能/倍投同帖只保留一侧主注，避免大/小对冲
            if mode in ("smart", "martingale") and (
                done_types & set(self.SMART_BASE_TYPES)
            ):
                plans = [(t, a) for t, a in plans if t not in self.SMART_BASE_TYPES]
            todo = [(t, a) for t, a in plans if t not in done_types]
            if not todo:
                logger.debug(
                    f"{self.LOG_TAG}主题#{topic['topic_id']} 计划类型均已下注 "
                    f"done={sorted(done_types)} plans={[p[0] for p in plans]}"
                )
                continue

            logger.info(
                f"{self.LOG_TAG}准备下注 主题#{topic['topic_id']} => "
                f"{', '.join(f'{t} {a}' for t, a in todo)} "
                f"(开奖 {topic.get('draw_time')}, 间隔 {self._reply_interval}s)"
            )
            for idx, (bet_type, amount) in enumerate(todo):
                if self._max_daily_bets is not None and self._count_bets_on(
                    self.get_data("history") or [], today
                ) >= self._max_daily_bets:
                    self._notify_daily_limit_stop(
                        "bets",
                        self._count_bets_on(self.get_data("history") or [], today),
                        self._max_daily_bets,
                    )
                    acted.append(f"达每日上限，停止后续多注@#{topic['topic_id']}")
                    break
                if idx > 0 and self._reply_interval > 0:
                    logger.info(
                        f"{self.LOG_TAG}同帖多注等待 {self._reply_interval}s "
                        f"后继续 {bet_type} {amount}"
                    )
                    time.sleep(self._reply_interval)
                ok, msg = self._post_bet(topic["topic_id"], bet_type, amount)
                if ok:
                    record = {
                        "time": self._now_str(),
                        "date": today,
                        "topic_id": topic["topic_id"],
                        "draw_time": topic.get("draw_time"),
                        "url": (
                            f"{self.BASE_URL}/forums.php?action=viewtopic"
                            f"&forumid={self.FORUM_ID}&topicid={topic['topic_id']}"
                        ),
                        "bet_type": bet_type,
                        "amount": amount,
                        "mode": self._bet_mode,
                        "result": None,
                        "profit": None,
                        "got_ticket": False,
                        "status": "pending",
                        "settle_notified": False,
                    }
                    self._append_history(record)
                    acted.append(f"{bet_type} {amount} @#{topic['topic_id']}")
                    self._notify_bet_success(record)
                    # 倍投一轮只下一帖
                    if mode == "martingale":
                        break
                else:
                    acted.append(f"失败#{topic['topic_id']}:{bet_type}:{msg}")
                    logger.warning(
                        f"{self.LOG_TAG}下注失败 topic={topic['topic_id']} "
                        f"{bet_type} {amount}: {msg}"
                    )
                    self._notify_bet_failure(topic["topic_id"], f"{bet_type}: {msg}")
                    # 失败则不再继续同帖后续类型，避免间隔后仍撞限制
                    break
            if mode == "martingale" and acted:
                break

        if not acted:
            return "有开放帖，但均已下注或不可投"
        return "；".join(acted)

    # ------------------------------------------------------------------ #
    # HTTP / 解析
    # ------------------------------------------------------------------ #
    def _list_hdsky_sites(self) -> List[Dict[str, Any]]:
        """对齐群聊区：SitesHelper.get_indexers() + 自定义站，再按天空白名单过滤。"""
        try:
            helper = SitesHelper()
            all_sites = [
                site for site in (helper.get_indexers() or []) if not site.get("public")
            ] + self.__custom_sites()
        except Exception as e:
            logger.error(f"{self.LOG_TAG}读取站点列表失败: {e}")
            return []
        filtered = [site for site in all_sites if self._is_hdsky_indexer(site)]
        logger.debug(
            f"{self.LOG_TAG}站点列表: 全部非公开={len(all_sites)}，天空候选={len(filtered)}，"
            f"名称={[s.get('name') for s in filtered]}"
        )
        return filtered

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
    def _is_hdsky_indexer(cls, site: Dict[str, Any]) -> bool:
        name = (site.get("name") or "").strip()
        domain = (site.get("domain") or "").lower()
        url = (site.get("url") or "").lower()
        if name in cls.TARGET_SITE_NAMES or "天空" in name:
            return True
        return "hdsky" in domain or "hdsky.me" in url

    @staticmethod
    def _is_hdsky_site(site: Any) -> bool:
        # 兼容旧 Site 对象判断
        if isinstance(site, dict):
            return HdskyDiceBet._is_hdsky_indexer(site)
        domain = (getattr(site, "domain", None) or "").lower()
        url = (getattr(site, "url", None) or "").lower()
        name = getattr(site, "name", None) or ""
        return (
            "hdsky" in domain
            or "hdsky.me" in url
            or name.strip() == "天空"
            or "天空" in name
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
        """从 SitesHelper indexer 加载天空站 Cookie / UA / 代理 / 地址。"""
        if not self._site_id:
            return False, "未选择站点，请在配置中选择天空"
        sites = self._list_hdsky_sites()
        site = next((s for s in sites if int(s.get("id")) == int(self._site_id)), None)
        if not site:
            # 兜底：直接 SiteOper
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
        if not self._is_hdsky_indexer(site):
            return False, f"当前仅支持天空（hdsky.me），已选：{site.get('name')}"
        cookie = (site.get("cookie") or "").strip()
        if not cookie:
            return False, f"站点「{site.get('name')}」未配置 Cookie，请先在站点管理中更新"
        self._cookie = cookie
        self._ua = (site.get("ua") or "").strip() or self.DEFAULT_UA
        self._use_proxy = bool(site.get("proxy"))
        self._site_name = site.get("name") or "天空"
        if site.get("url"):
            self.BASE_URL = str(site.get("url")).rstrip("/")
        self.save_data("site_name", self._site_name)
        logger.info(
            f"{self.LOG_TAG}已加载站点 {self._site_name}#{self._site_id}，"
            f"代理={'开' if self._use_proxy else '关'}，地址={self.BASE_URL}，"
            f"Cookie长度={len(self._cookie)}，UA={self._ua[:40]}..."
        )
        return True, "ok"

    def _proxies(self) -> Optional[dict]:
        if not self._use_proxy:
            return None
        return settings.PROXY

    def _request_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": self._ua or self.DEFAULT_UA,
            "Referer": f"{self.BASE_URL}/forums.php?action=viewforum&forumid={self.FORUM_ID}",
        }

    def _get(self, path: str) -> Optional[str]:
        url = path if path.startswith("http") else urljoin(self.BASE_URL + "/", path.lstrip("/"))
        headers = self._request_headers()
        logger.debug(f"{self.LOG_TAG}GET => {url}")
        res = RequestUtils(
            cookies=self._cookie,
            proxies=self._proxies(),
            timeout=30,
            headers=headers,
        ).get_res(url=url)
        if not res or res.status_code != 200:
            logger.warning(f"{self.LOG_TAG}GET 失败 {url}: {getattr(res, 'status_code', None)}")
            return None
        text = res.text or ""
        logger.debug(f"{self.LOG_TAG}GET <= {url} status={res.status_code} bytes={len(text)}")
        if "该页面必须在登录后才能访问" in text or "<title>HDSky :: 登录" in text:
            logger.error(f"{self.LOG_TAG}站点 Cookie 已失效，请到站点管理更新天空 Cookie")
            return None
        return text

    def _post(self, path: str, data: Any) -> Optional[str]:
        url = path if path.startswith("http") else urljoin(self.BASE_URL + "/", path.lstrip("/"))
        headers = self._request_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        if isinstance(data, dict):
            preview = str(data.get("body") or list(data.keys()))[:80]
        else:
            preview = str(data)[:80]
        logger.debug(f"{self.LOG_TAG}POST => {url} body={preview}")
        res = RequestUtils(
            cookies=self._cookie,
            proxies=self._proxies(),
            timeout=30,
            headers=headers,
        ).post_res(url=url, data=data)
        if not res:
            logger.warning(f"{self.LOG_TAG}POST 无响应 {url}")
            return None
        text = res.text or ""
        logger.debug(f"{self.LOG_TAG}POST <= {url} status={res.status_code} bytes={len(text)}")
        return text

    def _ensure_username(self) -> bool:
        html = self._get(f"/forums.php?action=viewforum&forumid={self.FORUM_ID}")
        if not html:
            return False
        m = re.search(
            r"欢迎回来\s*,\s*<span[^>]*>\s*<a[^>]*userdetails\.php\?id=(\d+)[^>]*>\s*<b>([^<]+)</b>",
            html,
        )
        if not m:
            m = re.search(r"userdetails\.php\?id=(\d+)[^>]*>\s*<b>([^<]+)</b>", html)
        if not m:
            return False
        self._username = m.group(2).strip()
        self.save_data("username", self._username)
        self.save_data("uid", m.group(1))
        return True

    def _list_forum_topics(self, pages: int = 2) -> List[Dict[str, Any]]:
        topics: List[Dict[str, Any]] = []
        seen = set()
        for page in range(pages):
            html = self._get(
                f"/forums.php?action=viewforum&forumid={self.FORUM_ID}&page={page}"
            )
            if not html:
                break
            for topic in self._parse_forum_list(html):
                if topic["topic_id"] in seen:
                    continue
                seen.add(topic["topic_id"])
                topics.append(topic)
        return topics

    def _parse_forum_list(self, html: str) -> List[Dict[str, Any]]:
        results = []
        # 每行主题大致在 <tr>...topicid=...本轮开奖...
        row_re = re.compile(
            r'<tr>\s*<td class="rowfollow"[^>]*>.*?'
            r'(?:class="(locked|lockednew|unlocked|unlockednew)"[^>]*>).*?'
            r'href="[^"]*topicid=(\d+)[^"]*"\s*>(.*?)</a>',
            re.S | re.I,
        )
        for m in row_re.finditer(html):
            lock_cls, topic_id, title_html = m.group(1), m.group(2), m.group(3)
            title = re.sub(r"<[^>]+>", "", title_html)
            title = re.sub(r"\s+", " ", title).strip()
            tm = self.TOPIC_TITLE_RE.search(title)
            if not tm:
                continue
            draw_time, result_type, dice = tm.group(1), tm.group(2), tm.group(3)
            locked = lock_cls.startswith("locked") or bool(result_type) or ("锁定" in title)
            openable = (not locked) and (not result_type) and self._is_before_draw(draw_time)
            results.append(
                {
                    "topic_id": topic_id,
                    "title": title,
                    "draw_time": draw_time,
                    "result": result_type,
                    "dice": dice,
                    "locked": locked,
                    "open": openable,
                }
            )
        return results

    def _fetch_topic_detail(self, topic_id: str, max_pages: int = 5) -> Optional[Dict[str, Any]]:
        first = self._get(
            f"/forums.php?action=viewtopic&forumid={self.FORUM_ID}&topicid={topic_id}"
        )
        if not first:
            return None
        title_m = re.search(r'<span id="top">(.*?)</span>', first, re.S)
        title = re.sub(r"<[^>]+>", "", title_m.group(1) if title_m else "")
        title = re.sub(r"\s+", " ", title).strip()
        tm = self.TOPIC_TITLE_RE.search(title)
        result = tm.group(2) if tm else None
        dice = tm.group(3) if tm else None
        locked = ("锁定" in title) or bool(result) or ("compose" not in first)
        pages = self._topic_page_count(first)
        pages = min(pages, max_pages)
        logger.debug(
            f"{self.LOG_TAG}主题#{topic_id} title={title[:60]} result={result} "
            f"locked={locked} pages={pages} username={self._username}"
        )
        html_all = first
        for page in range(1, pages):
            more = self._get(
                f"/forums.php?action=viewtopic&forumid={self.FORUM_ID}&topicid={topic_id}&page={page}"
            )
            if more:
                html_all += more

        self_bets: List[Dict[str, Any]] = []
        got_ticket = False
        profit_total = None
        if self._username:
            for post in self._parse_posts(html_all):
                if post["username"] != self._username:
                    continue
                logger.debug(
                    f"{self.LOG_TAG}主题#{topic_id} 找到自己的楼层 pid={post.get('pid')} "
                    f"bet={post.get('bet_type')} {post.get('amount')} "
                    f"profit={post.get('settle_profit')} ticket={post.get('got_ticket')}"
                )
                if post.get("bet_type"):
                    self_bets.append(
                        {
                            "bet_type": post["bet_type"],
                            "amount": post["amount"],
                            "profit": post.get("settle_profit"),
                            "got_ticket": post.get("got_ticket", False),
                        }
                    )
                if post.get("got_ticket"):
                    got_ticket = True
                if post.get("settle_profit") is not None:
                    profit_total = (profit_total or 0) + post["settle_profit"]
        else:
            logger.debug(f"{self.LOG_TAG}主题#{topic_id} 未设置 username，跳过楼层匹配")

        # 兼容旧字段：self_bet 取最后一条
        self_bet = self_bets[-1] if self_bets else None
        if self_bet and got_ticket:
            self_bet = dict(self_bet)
            self_bet["got_ticket"] = True

        return {
            "topic_id": topic_id,
            "title": title,
            "result": result,
            "dice": dice,
            "locked": locked,
            "self_bet": self_bet,
            "self_bets": self_bets,
            "got_ticket": got_ticket,
            "profit": profit_total,
            "pages": pages,
        }

    @staticmethod
    def _topic_page_count(html: str) -> int:
        """解析主题分页。HTML 中多为 &amp;page=N，需同时兼容。"""
        pages = {0}
        for m in re.finditer(r"(?:[?&]|&amp;)page=(\d+)", html, re.I):
            pages.add(int(m.group(1)))
        return max(pages) + 1

    def _parse_posts(self, html: str) -> List[Dict[str, Any]]:
        posts = []
        # 以 pidXXXX 表头切分
        parts = re.split(r'<table id="(pid\d+)"', html)
        # parts: [before, id1, chunk1, id2, chunk2, ...]
        for i in range(1, len(parts), 2):
            pid = parts[i]
            chunk = parts[i + 1] if i + 1 < len(parts) else ""
            user_m = re.search(
                r"userdetails\.php\?id=\d+[^>]*>\s*<b>([^<]+)</b>",
                chunk,
            )
            body_m = re.search(rf'id="{pid}body">(.*?)</div>', chunk, re.S)
            if not user_m or not body_m:
                continue
            username = user_m.group(1).strip()
            body_html = body_m.group(1)
            body_text = re.sub(r"<br\s*/?>", "\n", body_html)
            body_text = re.sub(r"<[^>]+>", "", body_text)
            first_line = next((ln.strip() for ln in body_text.splitlines() if ln.strip()), "")
            bet_type, amount = None, None
            bm = self.BET_BODY_RE.match(first_line)
            if bm:
                bet_type = bm.group(1)
                amount = self._parse_amount(bm.group(2))

            # 评分在 body 后的橙色块；理由需精确匹配，避免多条评分粘连误判
            rating_block = ""
            rb = re.search(r"\[评分\](.*?)</div>", chunk, re.S)
            if rb:
                rating_block = re.sub(r"<br\s*/?>", "\n", rb.group(1), flags=re.I)
                rating_block = re.sub(r"<[^>]+>", " ", rating_block)
            settle_profit = None
            got_ticket = False
            for rm in re.finditer(
                r"([+-]\d[\d,]*)\s*评分理由\s*[:：]\s*(观影随机续期奖励|兑奖)",
                rating_block,
            ):
                value = int(rm.group(1).replace(",", ""))
                reason = rm.group(2).strip()
                if reason == "观影随机续期奖励":
                    got_ticket = True
                elif reason == "兑奖":
                    settle_profit = (settle_profit or 0) + value

            posts.append(
                {
                    "pid": pid,
                    "username": username,
                    "bet_type": bet_type,
                    "amount": amount,
                    "settle_profit": settle_profit,
                    "got_ticket": got_ticket,
                }
            )
        return posts

    def _post_bet(self, topic_id: str, bet_type: str, amount: int) -> Tuple[bool, str]:
        body = f"{bet_type} {amount}"
        html = self._post(
            "/forums.php?action=post",
            data={"id": topic_id, "type": "reply", "body": body},
        )
        if html is None:
            return False, "请求失败"
        if "该页面必须在登录后才能访问" in html:
            return False, "Cookie 失效"
        # 成功后通常会跳回主题；再读一次确认对应类型是否出现
        detail = self._fetch_topic_detail(topic_id, max_pages=2)
        if detail:
            for sb in detail.get("self_bets") or []:
                if sb.get("bet_type") == bet_type:
                    return True, "ok"
            if detail.get("self_bet") and detail["self_bet"].get("bet_type") == bet_type:
                return True, "ok"
        # 有些站点 post 后直接带上自己的回复
        if self._username and self._username in (html or "") and bet_type in (html or ""):
            return True, "ok"
        if "错误" in (html or "") and "登录" not in (html or ""):
            err = re.search(r"<h1[^>]*>错误[:：]?(.*?)</h1>", html or "", re.S)
            return False, re.sub(r"<[^>]+>", "", err.group(1)).strip() if err else "发帖错误"
        # 宽松成功：没有明显错误页
        if html and "compose" in html and body not in html:
            # 仍在发帖页，可能失败
            return False, "仍停留在发帖页"
        return True, "ok"

    # ------------------------------------------------------------------ #
    # 智能下注（主注大小 + 可选高赔显著性加注）
    # ------------------------------------------------------------------ #
    def _candidate_types(self) -> List[str]:
        types = [t for t in self._fixed_types if t in self.BET_TYPES]
        return types or list(self.BET_TYPES)

    def _amount_for(self, bet_type: str) -> int:
        return int(self._amount_by_type.get(bet_type) or self._bet_amount)

    def _resolve_bet_plans(self, recent_topics: List[Dict[str, Any]]) -> List[Tuple[str, int]]:
        """返回本轮要对帖子下注的 (类型, 金额) 列表。固定模式可多注；智能可主注+加注；倍投单注。"""
        mode = (self._bet_mode or "smart").lower()
        if mode == "fixed":
            return [(t, self._amount_for(t)) for t in self._candidate_types()]
        if mode == "random":
            t = random.choice(self._candidate_types())
            return [(t, self._amount_for(t))]
        if mode == "martingale":
            return self._martingale_resolve_plans()
        return self._smart_resolve_plans(recent_topics)

    def _choose_bet_type(self, recent_topics: List[Dict[str, Any]]) -> str:
        plans = self._resolve_bet_plans(recent_topics)
        return plans[0][0] if plans else "大"

    def _collect_result_entries(
        self, recent_topics: List[Dict[str, Any]], limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """收集最近已开奖条目（论坛列表顺序≈新→旧）。"""
        limit = limit or self._smart_history_rounds
        entries: List[Dict[str, Any]] = []
        seen_topics = set()
        for t in recent_topics:
            tid, res = t.get("topic_id"), t.get("result")
            if not res or not tid or tid in seen_topics:
                continue
            seen_topics.add(tid)
            entries.append(
                {
                    "topic_id": tid,
                    "result": res,
                    "draw_time": t.get("draw_time") or "",
                }
            )
        if len(entries) < limit:
            for t in self._list_forum_topics(pages=5):
                tid, res = t.get("topic_id"), t.get("result")
                if not res or not tid or tid in seen_topics:
                    continue
                seen_topics.add(tid)
                entries.append(
                    {
                        "topic_id": tid,
                        "result": res,
                        "draw_time": t.get("draw_time") or "",
                    }
                )
                if len(entries) >= limit:
                    break
        return entries[:limit]

    def _collect_result_history(self, recent_topics: List[Dict[str, Any]]) -> List[str]:
        """收集最近 N 轮已开奖结果（论坛列表顺序≈新→旧）。"""
        return [e["result"] for e in self._collect_result_entries(recent_topics)]

    def _refresh_draw_history(self, recent_topics: List[Dict[str, Any]]):
        entries = self._collect_result_entries(recent_topics, limit=50)
        if entries:
            self.save_data("draw_history", entries)

    @staticmethod
    def _cold_streak(results: List[str], bet_type: str) -> int:
        streak = 0
        for r in results:
            if r == bet_type:
                break
            streak += 1
        return streak

    def _p_theo(self, bet_type: str) -> float:
        return self.CLASSICAL_COUNT[bet_type] / self.CLASSICAL_TOTAL

    def _proportion_z(self, count: int, n: int, p0: float) -> float:
        """相对理论概率 p0 的频率 z 分数（偏低为负）。"""
        if n <= 0 or p0 <= 0 or p0 >= 1:
            return 0.0
        se = math.sqrt(p0 * (1.0 - p0) / n)
        if se <= 0:
            return 0.0
        return (count / n - p0) / se

    @staticmethod
    def _weighted_freq(seq: List[str], target: str, decay: float) -> float:
        """指数衰减频率：下标 0 为最新。"""
        w_sum = w_hit = 0.0
        for i, r in enumerate(seq):
            w = decay ** i
            w_sum += w
            if r == target:
                w_hit += w
        return (w_hit / w_sum) if w_sum > 0 else 0.5

    def _smart_choose_base(self, results: List[str]) -> str:
        """
        轻量历史主注（仅大/小）：
        1) 连开 ≥3 强制反压
        2) 否则压近窗（默认 20）相对短缺侧；持平随机
        """
        size_results = [r for r in results if r in self.SMART_BASE_TYPES]
        if not size_results:
            pick = random.choice(list(self.SMART_BASE_TYPES))
            logger.info(f"{self.LOG_TAG}智能主注={pick} (无大小样本，随机)")
            return pick

        run_side = size_results[0]
        run_len = 0
        for r in size_results:
            if r != run_side:
                break
            run_len += 1
        if run_len >= self.SMART_REVERSE_STREAK:
            pick = "小" if run_side == "大" else "大"
            logger.info(
                f"{self.LOG_TAG}智能主注={pick} (连开{run_side}×{run_len}反压) "
                f"样本大小={len(size_results)}/{len(results)}"
            )
            return pick

        window = size_results[: self.SMART_LIGHT_WINDOW]
        da = sum(1 for r in window if r == "大")
        xi = sum(1 for r in window if r == "小")
        if da < xi:
            pick = "大"
            reason = f"近{len(window)}短缺大({da}<{xi})"
        elif xi < da:
            pick = "小"
            reason = f"近{len(window)}短缺小({xi}<{da})"
        else:
            pick = random.choice(["大", "小"])
            reason = f"近{len(window)}持平({da}:{xi})，随机"

        logger.info(
            f"{self.LOG_TAG}智能主注={pick} ({reason}) "
            f"样本大小={len(size_results)}/{len(results)}"
        )
        return pick

    def _should_add_extra(self, bet_type: str, results: List[str]) -> Tuple[bool, str]:
        """
        是否加注高赔类型：
        - 优先：冷连达到约一个期望间隔（顺子≈9，豹子≈33）→ 连压直到开出
        - 其次：频率明显偏低（z / 零次 / 半期望）且冷连尚可
        说明：独立骰子下不能制造正期望，只是用户允许高赔时的触发器。
        """
        p0 = self._p_theo(bet_type)
        n = len(results)
        min_n = max(self.SMART_EXTRA_MIN_ROUNDS, int(math.ceil(0.8 / p0)))
        if n < min_n:
            return False, f"样本不足 n={n}<{min_n}"
        k = sum(1 for r in results if r == bet_type)
        expected = n * p0
        z = self._proportion_z(k, n, p0)
        streak = self._cold_streak(results, bet_type)
        # 约 1 个期望间隔未出即开始连压（顺子~9，豹子~33）
        gap = max(5, int(math.ceil(0.9 / p0)))

        if streak >= gap:
            return True, f"冷连连压 streak={streak}>={gap} k={k}/{n} E={expected:.1f}"

        cold_enough = (
            z <= self.SMART_Z_THRESHOLD
            or (k == 0 and expected >= 1.2)
            or (expected >= 2 and k <= expected * 0.5)
        )
        soft_streak = max(3, int(math.ceil(0.35 / p0)))
        if cold_enough and streak >= soft_streak:
            return True, f"频率偏冷 z={z:.2f} streak={streak} k={k}/{n} E={expected:.1f}"
        return False, (
            f"不加注 streak={streak}/{gap} z={z:.2f} k={k}/{n} E={expected:.1f}"
        )

    def _smart_resolve_plans(self, recent_topics: List[Dict[str, Any]]) -> List[Tuple[str, int]]:
        """
        智能计划：
        - 必有一注主注：大 或 小
        - 若用户勾选，再按历史显著性决定是否追加顺子/豹子（可 0~2 注）
        """
        results = self._collect_result_history(recent_topics)
        base = self._smart_choose_base(results)
        plans: List[Tuple[str, int]] = [(base, self._amount_for(base))]

        extras_enabled = []
        if self._smart_allow_shunzi:
            extras_enabled.append("顺子")
        if self._smart_allow_baozi:
            extras_enabled.append("豹子")

        for extra in extras_enabled:
            ok, reason = self._should_add_extra(extra, results)
            if ok:
                plans.append((extra, self._amount_for(extra)))
                logger.info(f"{self.LOG_TAG}智能加注 {extra}: {reason}")
            else:
                logger.info(f"{self.LOG_TAG}智能不加注 {extra}: {reason}")

        logger.info(
            f"{self.LOG_TAG}智能计划={', '.join(f'{t} {a}' for t, a in plans)} "
            f"样本={len(results)} emp={dict(Counter(results))}"
        )
        return plans

    # ------------------------------------------------------------------ #
    # 倍投
    # ------------------------------------------------------------------ #
    def _martingale_has_pending(self) -> bool:
        history = self.get_data("history") or []
        return any(
            (h.get("mode") or "").lower() == "martingale" and h.get("profit") is None
            for h in history
        )

    def _martingale_resolve_plans(self) -> List[Tuple[str, int]]:
        state = dict(self.get_data("martingale") or {})
        side = state.get("side") or self._martingale_start_side or "大"
        if side not in self.SMART_BASE_TYPES:
            side = "大"
        amount = state.get("amount")
        if amount is None:
            amount = self._bet_amount
        amount = self._clamp_amount(amount)
        # 持久化，便于详情页展示
        if state.get("side") != side or state.get("amount") != amount:
            state.update({"side": side, "amount": amount, "loss_streak": int(state.get("loss_streak") or 0)})
            self.save_data("martingale", state)
        logger.info(f"{self.LOG_TAG}倍投计划={side} {amount} streak={state.get('loss_streak', 0)}")
        return [(side, amount)]

    def _update_martingale_after_settle(self, item: Dict[str, Any]):
        if (item.get("mode") or "").lower() != "martingale":
            return
        state = dict(self.get_data("martingale") or {})
        side = item.get("bet_type") or state.get("side") or self._martingale_start_side or "大"
        if side not in self.SMART_BASE_TYPES:
            side = "大"
        try:
            profit = int(item.get("profit"))
            prev_amount = int(item.get("amount") or self._bet_amount)
        except (TypeError, ValueError):
            return
        if profit > 0:
            # 开出（猜中）后换另一侧，金额回默认
            new_side = "小" if side == "大" else "大"
            new_state = {
                "side": new_side,
                "amount": self._bet_amount,
                "loss_streak": 0,
            }
            logger.info(
                f"{self.LOG_TAG}倍投开出：{side}→换边{new_side}，金额回默认 {self._bet_amount}"
            )
        else:
            doubled = self._clamp_amount(prev_amount * 2)
            new_state = {
                "side": side,
                "amount": doubled,
                "loss_streak": int(state.get("loss_streak") or 0) + 1,
            }
            logger.info(
                f"{self.LOG_TAG}倍投未中：保持{side}，金额 {prev_amount}→{doubled} "
                f"streak={new_state['loss_streak']}"
            )
        self.save_data("martingale", new_state)

    # ------------------------------------------------------------------ #
    # 评分短讯：已读 + 移至「论坛投注」
    # ------------------------------------------------------------------ #
    def _cleanup_rating_messages(self) -> str:
        box_no = self._ensure_rating_pm_box()
        if box_no is None:
            return "无法确认/创建短讯箱「论坛投注」"

        msg_ids = self._list_inbox_rating_message_ids(max_pages=8)
        if not msg_ids:
            return "收件箱无评分短讯"

        marked = self._pm_mark_read(msg_ids)
        moved = self._pm_move_to_box(msg_ids, box_no)
        return f"评分短讯 {len(msg_ids)} 条：已读={marked} 移至论坛投注={moved}"

    def _ensure_rating_pm_box(self) -> Optional[int]:
        boxes = self._parse_pm_boxes()
        for num, name in boxes.items():
            if name == self.RATING_PM_BOX_NAME:
                return num
        # 没有可创建：走短讯箱管理 add
        logger.info(f"{self.LOG_TAG}短讯箱无「{self.RATING_PM_BOX_NAME}」，尝试创建")
        self._get(
            "/messages.php?action=editmailboxes2&action2=add"
            f"&new1={quote(self.RATING_PM_BOX_NAME)}"
        )
        boxes = self._parse_pm_boxes()
        for num, name in boxes.items():
            if name == self.RATING_PM_BOX_NAME:
                logger.info(f"{self.LOG_TAG}已创建短讯箱「{self.RATING_PM_BOX_NAME}」 box={num}")
                return num
        logger.warning(f"{self.LOG_TAG}创建短讯箱「{self.RATING_PM_BOX_NAME}」后仍未找到")
        return None

    def _parse_pm_boxes(self) -> Dict[int, str]:
        """从收件箱页解析自定义短讯箱 boxnumber→name。"""
        html = self._get("/messages.php?action=viewmailbox&box=1")
        if not html:
            html = self._get("/messages.php")
        if not html:
            return {}
        boxes: Dict[int, str] = {}
        for m in re.finditer(
            r'<option[^>]*value=["\']?(\d+)["\']?[^>]*>([^<]+)</option>',
            html,
            re.I,
        ):
            num = int(m.group(1))
            name = re.sub(r"\s+", "", m.group(2)).strip()
            # 跳过收件箱/发件箱等系统项常见文案
            if name in ("收件箱", "发件箱", "系统箱", "Inbox", "Sentbox", "请选择", ""):
                continue
            if num >= 2:
                boxes[num] = name
        return boxes

    def _list_inbox_rating_message_ids(self, max_pages: int = 5) -> List[str]:
        ids: List[str] = []
        seen = set()
        for page in range(max_pages):
            html = self._get(f"/messages.php?action=viewmailbox&box=1&page={page}")
            if not html:
                if page == 0:
                    html = self._get("/messages.php")
                if not html:
                    break
            page_ids = self._parse_rating_pm_ids(html)
            if not page_ids and page > 0:
                break
            for mid in page_ids:
                if mid in seen:
                    continue
                seen.add(mid)
                ids.append(mid)
            # 本页没有更多分页痕迹则停
            if page > 0 and f"page={page + 1}" not in html and f"page={page+1}" not in html:
                # 仍可能有评分短讯在后面，但若本页零命中可提前停
                if not page_ids:
                    break
        return ids

    def _parse_rating_pm_ids(self, html: str) -> List[str]:
        ids: List[str] = []
        # 主题链接：messages.php?action=viewmessage&id=123
        for m in re.finditer(
            r'messages\.php\?action=viewmessage&(?:amp;)?id=(\d+)[^>]*>([^<]*)<',
            html,
            re.I,
        ):
            mid, subject = m.group(1), m.group(2)
            subject = re.sub(r"\s+", "", subject)
            if self.RATING_PM_RE.search(subject):
                ids.append(mid)
        if ids:
            return ids
        # 兜底：checkbox 与邻近主题文本
        for m in re.finditer(
            r'name=["\']messages\[\]["\'][^>]*value=["\']?(\d+)["\']?',
            html,
            re.I,
        ):
            mid = m.group(1)
            # 在前后 500 字符窗口找主题
            start = max(0, m.start() - 500)
            window = html[start : m.end() + 80]
            if self.RATING_PM_RE.search(re.sub(r"\s+", "", window)):
                ids.append(mid)
        return ids

    def _pm_mark_read(self, message_ids: List[str]) -> int:
        if not message_ids:
            return 0
        ok = 0
        for i in range(0, len(message_ids), 40):
            batch = message_ids[i : i + 40]
            data: List[Tuple[str, str]] = [
                ("action", "moveordel"),
                ("markread", "设为已读"),
            ]
            for mid in batch:
                data.append(("messages[]", str(mid)))
            html = self._post("/messages.php", data)
            if html is not None:
                ok += len(batch)
        return ok

    def _pm_move_to_box(self, message_ids: List[str], box_no: int) -> int:
        if not message_ids:
            return 0
        ok = 0
        for i in range(0, len(message_ids), 40):
            batch = message_ids[i : i + 40]
            data: List[Tuple[str, str]] = [
                ("action", "moveordel"),
                ("box", str(box_no)),
                ("move", "移至"),
            ]
            for mid in batch:
                data.append(("messages[]", str(mid)))
            html = self._post("/messages.php", data)
            if html is not None:
                ok += len(batch)
        return ok

    # ------------------------------------------------------------------ #
    # 记录 / 同步 / 汇总
    # ------------------------------------------------------------------ #
    def _bet_types_on_topic(self, topic_id: str) -> set:
        history = self.get_data("history") or []
        return {
            h.get("bet_type")
            for h in history
            if str(h.get("topic_id")) == str(topic_id) and h.get("bet_type")
        }

    def _already_bet_topic_type(self, topic_id: str, bet_type: str) -> bool:
        return bet_type in self._bet_types_on_topic(topic_id)

    def _already_bet_topic(self, topic_id: str) -> bool:
        """兼容：主题下是否已有任意本地下注记录。"""
        return bool(self._bet_types_on_topic(topic_id))

    def _remember_existing_bet(self, topic: Dict[str, Any], self_bet: Dict[str, Any]):
        bet_type = self_bet.get("bet_type")
        if not bet_type:
            return
        if self._already_bet_topic_type(topic["topic_id"], bet_type):
            return
        record = {
            "time": self._now_str(),
            "date": self._today_str(),
            "topic_id": topic["topic_id"],
            "draw_time": topic.get("draw_time"),
            "url": (
                f"{self.BASE_URL}/forums.php?action=viewtopic"
                f"&forumid={self.FORUM_ID}&topicid={topic['topic_id']}"
            ),
            "bet_type": bet_type,
            "amount": self_bet.get("amount"),
            "mode": "manual",
            "result": topic.get("result"),
            "profit": self_bet.get("profit"),
            "got_ticket": bool(self_bet.get("got_ticket")),
            "status": "settled" if self_bet.get("profit") is not None else "pending",
            "settle_notified": False,
        }
        self._append_history(record)
        logger.info(
            f"{self.LOG_TAG}主题#{topic['topic_id']} 补记已有下注 {bet_type} "
            f"{self_bet.get('amount')}"
        )

    def _append_history(self, record: Dict[str, Any]):
        history = self.get_data("history") or []
        history.append(record)
        # 清理过期
        cutoff = (datetime.now() - timedelta(days=self._history_days)).strftime("%Y-%m-%d")
        history = [h for h in history if (h.get("date") or "") >= cutoff]
        self.save_data("history", history)

    def _calc_settle_profit(
        self, bet_type: Optional[str], amount: Optional[int], result: Optional[str]
    ) -> Optional[int]:
        if not bet_type or not result or amount is None:
            return None
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return None
        if bet_type == result:
            return int(round(amount * self.ODDS.get(bet_type, 0)))
        return -amount

    def _sync_pending_results(self):
        history = self.get_data("history") or []
        changed = False
        tickets_by_day = dict(self.get_data("tickets_by_day") or {})
        newly_settled: List[Dict[str, Any]] = []
        pending_items = [
            item
            for item in history
            if not (
                item.get("status") == "settled"
                and item.get("profit") is not None
                and item.get("settle_notified")
            )
        ]
        logger.info(
            f"{self.LOG_TAG}开始同步未结算记录：history={len(history)}，待处理={len(pending_items)}"
        )

        # 论坛列表可用于快速拿开奖结果（即使帖内翻页失败也能结算）
        forum_map = {}
        try:
            for t in self._list_forum_topics(pages=2):
                forum_map[str(t.get("topic_id"))] = t
            logger.debug(
                f"{self.LOG_TAG}论坛列表缓存 {len(forum_map)} 条，"
                f"含结果={sum(1 for v in forum_map.values() if v.get('result'))}"
            )
        except Exception as e:
            logger.warning(f"{self.LOG_TAG}预拉论坛列表失败: {e}")

        for item in history:
            already_settled = (
                item.get("status") == "settled" and item.get("profit") is not None
            )
            if already_settled and item.get("settle_notified"):
                continue
            topic_id = item.get("topic_id")
            if not topic_id:
                continue
            was_pending = item.get("profit") is None
            need_notify = already_settled and not item.get("settle_notified")

            detail = self._fetch_topic_detail(str(topic_id), max_pages=5)
            forum_info = forum_map.get(str(topic_id)) or {}
            result = None
            if detail and detail.get("result"):
                result = detail["result"]
            elif forum_info.get("result"):
                result = forum_info["result"]
                logger.debug(
                    f"{self.LOG_TAG}主题#{topic_id} 从论坛列表取得开奖结果={result}"
                )

            if result:
                item["result"] = result

            matched_floor = None
            if detail:
                floors = detail.get("self_bets") or []
                if not floors and detail.get("self_bet"):
                    floors = [detail["self_bet"]]
                matched_floor = self._match_self_bet_floor(item, floors)

                if matched_floor and matched_floor.get("profit") is not None:
                    item["profit"] = matched_floor["profit"]
                    item["status"] = "settled"
                    changed = True
                    logger.info(
                        f"{self.LOG_TAG}主题#{topic_id} 兑奖评分结算 "
                        f"{item.get('bet_type')} 盈亏={item['profit']}"
                    )
                elif (
                    not floors
                    and detail.get("profit") is not None
                    and item.get("profit") is None
                    and len([h for h in history if str(h.get("topic_id")) == str(topic_id)]) <= 1
                ):
                    # 仅单注主题可安全使用汇总 profit
                    item["profit"] = detail["profit"]
                    item["status"] = "settled"
                    changed = True

                ticket_hit = bool(
                    (matched_floor and matched_floor.get("got_ticket"))
                    or detail.get("got_ticket")
                    or (detail.get("self_bet") and detail["self_bet"].get("got_ticket"))
                )
                if ticket_hit and not item.get("got_ticket"):
                    # 同帖多注只给一笔记观影券，避免重复累计
                    sibling_has_ticket = any(
                        str(h.get("topic_id")) == str(topic_id)
                        and h is not item
                        and h.get("got_ticket")
                        for h in history
                    )
                    if not sibling_has_ticket:
                        item["got_ticket"] = True
                        day = item.get("date") or self._today_str()
                        tickets_by_day[day] = int(tickets_by_day.get(day, 0)) + 1
                        changed = True

            # 已开奖但评分未刷出 / 翻页未找到楼层：用本地下注记录 + 开奖类型兜底
            if item.get("profit") is None and result:
                locked = bool(
                    (detail and detail.get("locked"))
                    or forum_info.get("locked")
                    or result
                )
                if locked:
                    bet_type = item.get("bet_type")
                    amount = item.get("amount")
                    if matched_floor:
                        bet_type = matched_floor.get("bet_type") or bet_type
                        if matched_floor.get("amount") is not None:
                            amount = matched_floor.get("amount")
                    profit = self._calc_settle_profit(bet_type, amount, result)
                    if profit is not None:
                        item["profit"] = profit
                        item["status"] = "settled"
                        item["result"] = result
                        changed = True
                        logger.info(
                            f"{self.LOG_TAG}主题#{topic_id} 兜底结算：下注={bet_type} {amount} "
                            f"开奖={result} 盈亏={profit}"
                        )

            if (was_pending or need_notify) and item.get("profit") is not None:
                if not item.get("settle_notified"):
                    newly_settled.append(dict(item))
                    item["settle_notified"] = True
                    changed = True

        if changed:
            self.save_data("history", history)
            self.save_data("tickets_by_day", tickets_by_day)
        logger.info(f"{self.LOG_TAG}同步完成，新结算通知 {len(newly_settled)} 条")
        for item in newly_settled:
            self._update_martingale_after_settle(item)
            self._notify_settlement(item)

    def _refresh_today_tickets(self):
        """按自然天统计自己评论中的观影券次数。"""
        today = self._today_str()
        history = self.get_data("history") or []
        known_ids = {str(h.get("topic_id")) for h in history if h.get("got_ticket")}
        count = sum(1 for h in history if h.get("date") == today and h.get("got_ticket"))

        # 仅在配置了观影券上限时额外扫今日已结帖，补录非本插件下注获得的券
        if self._max_daily_tickets is not None:
            topics = self._list_forum_topics(pages=2)
            checked = 0
            for t in topics:
                if not (t.get("draw_time") or "").startswith(today):
                    continue
                if not t.get("result") and not t.get("locked"):
                    continue
                tid = str(t["topic_id"])
                if tid in known_ids:
                    continue
                checked += 1
                if checked > 12:
                    break
                detail = self._fetch_topic_detail(tid, max_pages=2)
                if detail and detail.get("got_ticket"):
                    count += 1
                    known_ids.add(tid)

        tickets_by_day = dict(self.get_data("tickets_by_day") or {})
        tickets_by_day[today] = count
        cutoff = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        tickets_by_day = {k: v for k, v in tickets_by_day.items() if k >= cutoff}
        self.save_data("tickets_by_day", tickets_by_day)

    def _summarize_pl(self, history: List[dict], period: str) -> Dict[str, Any]:
        today = date.today()
        if period == "day":
            start = today
        elif period == "week":
            start = today - timedelta(days=today.weekday())
        elif period == "month":
            start = today.replace(day=1)
        else:
            start = date(1970, 1, 1)
        start_s = start.strftime("%Y-%m-%d")
        bets = 0
        settled = 0
        wins = 0
        losses = 0
        profit = 0
        for h in history:
            d = h.get("date") or ""
            if d < start_s:
                continue
            bets += 1
            if h.get("profit") is None:
                continue
            settled += 1
            p = int(h["profit"])
            profit += p
            if p > 0:
                wins += 1
            elif p < 0:
                losses += 1
        return {
            "bets": bets,
            "settled": settled,
            "wins": wins,
            "losses": losses,
            "profit": profit,
        }

    @staticmethod
    def _count_bets_on(history: List[dict], day: str) -> int:
        return sum(1 for h in history if h.get("date") == day)

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clamp_amount(value: Any) -> int:
        try:
            amount = int(float(value))
        except (TypeError, ValueError):
            amount = 100
        return max(100, min(100000, amount))

    @classmethod
    def _clamp_interval(cls, value: Any) -> int:
        try:
            n = int(float(value))
        except (TypeError, ValueError):
            n = 30
        return max(0, min(600, n))

    @classmethod
    def _parse_fixed_types(cls, config: dict) -> List[str]:
        raw = config.get("fixed_types")
        if raw is None or raw == "" or raw == []:
            legacy = config.get("fixed_type") or "大"
            raw = [legacy] if isinstance(legacy, str) else list(legacy or [])
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.split(",") if x.strip()]
        types = [t for t in raw if t in cls.BET_TYPES]
        # 保序去重
        seen = set()
        ordered = []
        for t in types:
            if t not in seen:
                seen.add(t)
                ordered.append(t)
        return ordered or ["大"]

    @classmethod
    def _parse_amount_by_type(cls, config: dict) -> Dict[str, int]:
        mapping: Dict[str, int] = {}
        for t in cls.BET_TYPES:
            raw = config.get(f"amount_{t}")
            if raw is None or str(raw).strip() == "":
                continue
            mapping[t] = cls._clamp_amount(raw)
        return mapping

    @staticmethod
    def _match_self_bet_floor(
        item: Dict[str, Any], floors: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """按类型(+金额)匹配帖内自己的下注楼层。"""
        if not floors:
            return None
        bet_type = item.get("bet_type")
        amount = item.get("amount")
        typed = [f for f in floors if f.get("bet_type") == bet_type]
        if not typed:
            return None
        if amount is not None:
            for f in typed:
                try:
                    if int(f.get("amount")) == int(amount):
                        return f
                except (TypeError, ValueError):
                    continue
        return typed[0]

    @staticmethod
    def _to_optional_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            n = int(value)
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_amount(text: str) -> int:
        text = (text or "").strip().lower().replace(",", "")
        if text.endswith("w"):
            return int(float(text[:-1]) * 10000)
        return int(float(text))

    def _is_before_draw(self, draw_time: str) -> bool:
        try:
            tz = pytz.timezone(settings.TZ)
            dt = tz.localize(datetime.strptime(draw_time, "%Y-%m-%d %H:%M:%S"))
            return datetime.now(tz) < dt
        except Exception:
            return True

    def _next_cron_time(self) -> str:
        try:
            if not self._cron:
                return "—"
            tz = pytz.timezone(settings.TZ)
            trigger = CronTrigger.from_crontab(self._cron, timezone=settings.TZ)
            nxt = trigger.get_next_fire_time(None, datetime.now(tz=tz))
            return nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else "—"
        except Exception as e:
            logger.debug(f"{self.LOG_TAG}计算下次周期失败: {e}")
            return "—"

    def _now_str(self) -> str:
        return datetime.now(tz=pytz.timezone(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S")

    def _today_str(self) -> str:
        return datetime.now(tz=pytz.timezone(settings.TZ)).strftime("%Y-%m-%d")
