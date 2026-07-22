# MoviePilot-Plugins

MoviePilot V2 插件仓库。

## 插件列表

| 插件 | 说明 |
|------|------|
| Transmission Tracker 标签 | 根据 tracker 为 Transmission 种子自动添加标签 |
| 蜂巢签到(修复版) | 自动完成蜂巢论坛每日签到，支持历史记录与 PT 人生数据更新 |
| 空论坛掷骰子下注 | HDSky 掷骰子论坛自动下注，支持固定/随机/智能策略与盈亏汇总 |

## 仓库结构

```text
.
├── package.v2.json
└── plugins.v2/
    ├── transmissiontrackerlabel/
    │   └── __init__.py
    ├── fengchaosigninfix/
    │   └── __init__.py
    └── hdskydicebet/
        └── __init__.py
```

## 发布到 GitHub

1. 在 GitHub 新建仓库（例如 `MoviePilot-Plugins`）
2. 将本仓库内容推送上去：

```bash
git init
git add package.v2.json plugins.v2 README.md
git commit -m "Add TransmissionTrackerLabel plugin v1.0.0"
git branch -M main
git remote add origin https://github.com/你的用户名/MoviePilot-Plugins.git
git push -u origin main
```

3. 确认 GitHub 上能访问：
   - `https://raw.githubusercontent.com/你的用户名/MoviePilot-Plugins/main/package.v2.json`
   - `https://raw.githubusercontent.com/你的用户名/MoviePilot-Plugins/main/plugins.v2/transmissiontrackerlabel/__init__.py`

## 在 MoviePilot 中加载

1. 打开 **设置 → 插件 → 插件市场设置**
2. 在「输入插件仓库地址」中填入：

```text
https://github.com/你的用户名/MoviePilot-Plugins
```

3. 点击 **+** 添加，然后 **保存**
4. 回到 **插件市场**，刷新列表
5. 搜索 **Transmission Tracker 标签** 或 **TransmissionTrackerLabel**
6. 点击 **安装**，配置下载器和标签规则后启用

## 配置说明

### Transmission Tracker 标签

规则格式（每行一条）：

```text
tracker关键字    标签1/标签2/标签3
ourbits.club     我堡/ob/十二大
tracker.hdsky.me 空/十二大
```

- 空行和以 `#` 开头的行会被忽略
- tracker 为子串匹配
- 新标签会追加到现有标签，不会删除已有标签

### 空论坛掷骰子下注

1. 在 **站点管理** 中确保已添加「天空」（hdsky.me）并配置好 Cookie
2. 插件配置里 **选择站点** → 选天空（通过 `SitesHelper.get_indexers()` 读取，白名单过滤天空/hdsky）
3. 选择下注模式：
   - **固定**：始终下注指定类型（豹子/顺子/大/小）
   - **随机**：每轮随机一种类型
   - **智能**：按三骰子古典概型（216 种等可能）对比近期开奖频率，结合理论期望自动选边
4. 下注金额：`100` ~ `100000`
5. 可选限制：每日最大下注次数、每日观影券次数（评论出现「观影随机续期奖励」计 1 次，按自然天）
6. 建议执行周期：`*/3 * * * *`
7. 插件详情页可查看下注记录，以及日/周/月魔力盈亏汇总

Cookie / UA / 代理均从所选站点读取，无需再手动粘贴 Cookie。回复格式遵循论坛规则：`类型 + 空格 + 金额`，例如 `大 1000`。已开奖/锁定帖不会下注。

## 更新插件

1. 修改 `plugins.v2/transmissiontrackerlabel/__init__.py` 中的 `plugin_version`
2. 同步更新 `package.v2.json` 中的 `version` 和 `history`
3. 推送到 GitHub
4. 在 MoviePilot 插件市场点击更新/重装
