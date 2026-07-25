import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import ServiceInfo
from app.schemas.types import EventType


class TransmissionTrackerLabel(_PluginBase):
    """根据 tracker 为 Transmission 种子添加标签。"""

    plugin_name = "Transmission Tracker 标签"
    plugin_desc = "根据种子 tracker 地址，按配置规则自动为 Transmission 添加标签（支持 / 分隔多标签）"
    # 相对文件名：与官方插件一致，走前端本地 ./plugin_icon/（勿用 raw.githubusercontent.com）
    plugin_icon = "Transmission_A.png"
    plugin_version = "1.0.2"
    plugin_author = "Kuanghom"
    author_url = "https://github.com/Kuanghom"
    plugin_config_prefix = "TransmissionTrackerLabel_"
    plugin_order = 10
    auth_level = 1

    LOG_TAG = "[TransmissionTrackerLabel] "

    _label_rules_default = """tracker.open.cd         后/十二大
tracker.m-team.cc       馒头/十二大
on.springsunday.net     不可说/十二大
tracker.ptchdbits.co    岛/十二大
tracker.rainbowisland.co    岛/十二大
chdbits.xyz             岛/十二大
t.hdhome.org            家园/十二大
tracker.hhanclub.net    憨憨/十二大
tracker.hdsky.me        空/十二大
ourbits.club            我堡/ob/十二大
tracker.pterclub.net    猫/十二大
tracker.keepfrds.com    月月/十二大
tracker.totheglory.im   听听歌/十二大
t.audiences.me          观众/十二大
tracker.cinefiles.info  观众/十二大
t.ubits.club            UB
tracker.qingwapt.org    青蛙
tracker.qingwapt.com    青蛙
tracker.qingwa.pro      青蛙
crabpt.vip              蟹黄堡
tracker.agsvpt.cn       末日
www.nicept.net          老师
sewerpt.com             下水道
www.pttime.org          PTTime
tracker.ptcafe.club     咖啡
ptfans.cc               PTFans
t.hddolby.com           杜比
tracker.hdtime.org      时光
tracker.piggo.me        猪猪
tracker.kame.gay        龟
kamept.com              龟
si-qi.xyz               思齐
discfan.net             蝶粉
ptzone.xyz              PTZONE
tracker.hdarea.club     好大
tracker.happyfappy.org  happyfappy
announce.haidan.video   海胆
announce.haidan.cc      海胆
zmpt.cc                 织梦
tracker.cspt.top        财神
tracker.cspt.cc         财神
tracker.cspt.date       财神
tracker.cyanbug.net     大青虫
ptsbao.club             烧包
jpopsuki.eu             鸡婆婆
tracker.luckpt.de       Lucky
t.pthome.org            铂金家
tracker.novahd.top      novahd
pt.lajidui.top          垃圾堆
cspt.top                财神
rousi.pro               肉丝
tracker.ptskit.org      拾刻
52pt.site               52PT
ptl.gs                  劳改所
relay01.ptl.gs          劳改所
bilibili.download       Railgun
cangbao.ge              藏宝阁
raingfh.top             雨
t.hdhome.org            聆音
tracker.xingyungept.org 星陨阁
tracker.hdkyl.in        麒麟
www.hdkylin.top         麒麟
longpt.org              龙宝
# 格式说明：tracker关键字    标签1/标签2/标签3
# 空行和以 # 开头的行会被忽略"""

    _enabled = False
    _onlyonce = False
    _cron = "0 */6 * * *"
    _notify = False
    _downloaders: Optional[List[str]] = None
    _label_rules_str = ""
    _label_rules: List[Tuple[str, List[str]]] = []
    _scheduler: Optional[BackgroundScheduler] = None
    _last_result: Dict[str, Any] = {}

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._cron = config.get("cron") or "0 */6 * * *"
        self._notify = bool(config.get("notify"))
        self._downloaders = config.get("downloaders")
        self._label_rules_str = config.get("label_rules_str") or self._label_rules_default

        if "label_rules_str" not in config:
            config["label_rules_str"] = self._label_rules_default
            self.update_config(config)

        self._label_rules = self._parse_label_rules(self._label_rules_str)
        self.stop_service()

        if self._onlyonce and self._enabled:
            self._onlyonce = False
            config["onlyonce"] = False
            self.update_config(config)
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.apply_labels,
                trigger="date",
                run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                + datetime.timedelta(seconds=3),
                name="Transmission Tracker 标签立即执行",
            )
            if self._scheduler.get_jobs():
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/tr_tracker_label",
                "event": EventType.PluginAction,
                "desc": "按 tracker 规则为 Transmission 打标签",
                "category": "下载器",
                "data": {"action": "tr_tracker_label"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        return [
            {
                "id": "TransmissionTrackerLabel.Apply",
                "name": "Transmission Tracker 标签同步",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.apply_labels,
                "kwargs": {},
            }
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event):
        event_data = event.event_data or {}
        if event_data.get("action") != "tr_tracker_label":
            return
        self.post_message(
            channel=event_data.get("channel"),
            title=f"{self.LOG_TAG}开始执行远程命令...",
            userid=event_data.get("user"),
        )
        result = self.apply_labels()
        self.post_message(
            channel=event_data.get("channel"),
            title=f"{self.LOG_TAG}执行完成：更新 {result.get('updated', 0)} 个，跳过 {result.get('skipped', 0)} 个",
            userid=event_data.get("user"),
        )

    @staticmethod
    def _parse_label_rules(rules_str: str) -> List[Tuple[str, List[str]]]:
        rules: List[Tuple[str, List[str]]] = []
        for line_no, raw_line in enumerate(rules_str.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                logger.warning(f"{TransmissionTrackerLabel.LOG_TAG}第 {line_no} 行格式无效，已跳过: {raw_line!r}")
                continue
            tracker_key, labels_text = parts
            labels = [label.strip() for label in labels_text.split("/") if label.strip()]
            if not labels:
                logger.warning(f"{TransmissionTrackerLabel.LOG_TAG}第 {line_no} 行未配置标签，已跳过")
                continue
            rules.append((tracker_key.lower(), labels))
        return rules

    @staticmethod
    def _get_trackers(torrent: Any) -> List[str]:
        urls: List[str] = []
        try:
            for tracker in torrent.trackers or []:
                announce = getattr(tracker, "announce", "")
                if announce:
                    urls.append(announce)
            for announce in getattr(torrent, "tracker_list", []) or []:
                if announce:
                    urls.append(announce)
        except Exception as err:
            logger.error(f"{TransmissionTrackerLabel.LOG_TAG}获取 tracker 失败: {err}")
        return urls

    @staticmethod
    def _labels_for_torrent(tracker_urls: List[str], rules: List[Tuple[str, List[str]]]) -> List[str]:
        matched: List[str] = []
        seen: set[str] = set()
        lowered_urls = [url.lower() for url in tracker_urls]
        for tracker_key, labels in rules:
            if any(tracker_key in url for url in lowered_urls):
                for label in labels:
                    if label not in seen:
                        seen.add(label)
                        matched.append(label)
        return matched

    @property
    def service_infos(self) -> Optional[Dict[str, ServiceInfo]]:
        if not self._downloaders:
            logger.warning(f"{self.LOG_TAG}尚未配置下载器，请检查配置")
            return None

        services = DownloaderHelper().get_services(name_filters=self._downloaders)
        if not services:
            logger.warning(f"{self.LOG_TAG}获取下载器实例失败，请检查配置")
            return None

        active_services: Dict[str, ServiceInfo] = {}
        for service_name, service_info in services.items():
            if not DownloaderHelper().is_downloader(service_type="transmission", service=service_info):
                logger.warning(f"{self.LOG_TAG}下载器 {service_name} 不是 Transmission，已跳过")
                continue
            if service_info.instance.is_inactive():
                logger.warning(f"{self.LOG_TAG}下载器 {service_name} 未连接，请检查配置")
                continue
            active_services[service_name] = service_info

        if not active_services:
            logger.warning(f"{self.LOG_TAG}没有可用的 Transmission 下载器")
            return None
        return active_services

    def apply_labels(self) -> Dict[str, Any]:
        result = {
            "updated": 0,
            "skipped": 0,
            "errors": 0,
            "details": [],
            "time": datetime.datetime.now(tz=pytz.timezone(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S"),
        }

        if not self._label_rules:
            logger.warning(f"{self.LOG_TAG}标签规则为空，请检查配置")
            self._last_result = result
            return result

        services = self.service_infos
        if not services:
            self._last_result = result
            return result

        for service_name, service_info in services.items():
            downloader = service_info.instance
            torrents, error = downloader.get_torrents()
            if error or not torrents:
                logger.warning(f"{self.LOG_TAG}下载器 {service_name} 获取种子失败或没有种子")
                continue

            logger.info(f"{self.LOG_TAG}下载器 {service_name} 共 {len(torrents)} 个种子")
            for torrent in torrents:
                tracker_urls = self._get_trackers(torrent)
                target_labels = self._labels_for_torrent(tracker_urls, self._label_rules)
                if not target_labels:
                    result["skipped"] += 1
                    continue

                current_labels = list(getattr(torrent, "labels", []) or [])
                merged_labels = list(dict.fromkeys(current_labels + target_labels))
                if merged_labels == current_labels:
                    result["skipped"] += 1
                    continue

                added = [label for label in merged_labels if label not in current_labels]
                try:
                    ok = downloader.set_torrent_tag(
                        ids=torrent.hashString,
                        tags=target_labels,
                        org_tags=current_labels,
                    )
                    if ok:
                        result["updated"] += 1
                        detail = f"{torrent.name} -> {', '.join(added)}"
                        result["details"].append(detail)
                        logger.info(f"{self.LOG_TAG}[{service_name}] {detail}")
                    else:
                        result["errors"] += 1
                except Exception as err:
                    result["errors"] += 1
                    logger.error(f"{self.LOG_TAG}设置标签失败 {torrent.name}: {err}")

        self._last_result = result
        summary = (
            f"更新 {result['updated']} 个，跳过 {result['skipped']} 个"
            + (f"，失败 {result['errors']} 个" if result["errors"] else "")
        )
        logger.info(f"{self.LOG_TAG}执行完成：{summary}")

        if self._notify and result["updated"] > 0:
            self.post_message(
                title=f"{self.plugin_name} 执行完成",
                text=summary,
            )

        return result

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        try:
            downloader_options = [
                {"title": config.name, "value": config.name}
                for config in DownloaderHelper().get_configs().values()
            ]
        except Exception as err:
            logger.error(f"{self.LOG_TAG}获取下载器列表失败: {err}")
            downloader_options = []

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "执行后通知"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "onlyonce", "label": "立即运行一次"},
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "downloaders",
                                            "label": "Transmission 下载器",
                                            "multiple": True,
                                            "chips": True,
                                            "items": downloader_options,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "定时任务 (Cron)",
                                            "placeholder": "0 */6 * * *",
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
                                            "text": "每行一条规则：tracker关键字 + 空格 + 标签（多个标签用 / 分隔）。例如：ourbits.club  我堡/ob/十二大",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "label_rules_str",
                                            "label": "Tracker 标签规则",
                                            "rows": 18,
                                            "placeholder": "tracker.open.cd    后/十二大",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "cron": "0 */6 * * *",
            "downloaders": [],
            "label_rules_str": self._label_rules_default,
        }

    def get_page(self) -> List[dict]:
        result = self._last_result or {}
        details = result.get("details") or []
        detail_text = "\n".join(details[:20]) if details else "暂无执行记录"
        if len(details) > 20:
            detail_text += f"\n... 共 {len(details)} 条"

        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": (
                        f"最近执行: {result.get('time', '尚未执行')}\n"
                        f"更新: {result.get('updated', 0)} | "
                        f"跳过: {result.get('skipped', 0)} | "
                        f"失败: {result.get('errors', 0)}"
                    ),
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "success",
                    "variant": "tonal",
                    "text": detail_text,
                },
            },
        ]

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as err:
            logger.error(f"{self.LOG_TAG}停止服务失败: {err}")
