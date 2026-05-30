# astrbot_plugin_mc_lottery — MC 抽奖插件

[![GitHub](https://img.shields.io/badge/GitHub-rogergzl%2Fastrbot__plugin__mc__lottery-blue?logo=github)](https://github.com/rogergzl/astrbot_plugin_mc_lottery)
![Version](https://img.shields.io/badge/version-v1.2.4-green)

QQ 绑定 MC ID + 个人抽奖 + 全局开奖双模式，每轮每人最多中奖一次，通过 RCON 直连在服务器上自动执行奖励，实现群服双向联动。

## 安装

本目录作为 AstrBot 插件加载。依赖 `mrcon` 插件提供 RCON 连接能力。

## 架构

```
astrbot_plugin_mc_lottery/
├── main.py             # 插件入口（命令路由 + 抽奖逻辑 + 自动调度）
├── _transport.py       # RCON 协议传输层
├── _conf_schema.json   # 配置 Schema
├── metadata.yaml       # 元数据
└── README.md
```

**数据文件**（自动创建于 `data/plugin_data/astrbot_plugin_mc_lottery/`）：

| 文件 | 类型 | 说明 |
|------|------|------|
| `bindings.json` | JSON | QQ-MC ID 绑定、代理抽奖开关、自动兑换开关、奖品队列、历史轮次记录 |

## 全部命令速查

### 用户命令

| 命令 | 说明 |
|------|------|
| `/抽奖绑定 <MC_ID>` | 绑定 QQ 与 Minecraft ID |
| `/自动抽奖` | 切换代理抽奖（参与全局开奖） |
| `/抽奖自动兑换` | 切换中奖后自动兑换 |
| `/抽奖` | 个人抽奖（每轮每人最多中一次） |
| `/抽奖兑换` | 手动领取一条待发奖品 |
| `/抽奖帮助` | 显示全部命令帮助 |

### 管理命令（需管理员权限）

| 命令 | 说明 |
|------|------|
| `/抽奖开启` | 开启抽奖并启动自动开奖 |
| `/抽奖关闭` | 关闭所有抽奖 |
| `/开奖` | 立即全局开奖，对所有已绑定用户开奖并广播中奖名单 |
| `/抽奖重置` | 归档本轮记录并清除，开启新一轮 |
| `/抽奖间隔 <分钟>` | 设置自动开奖间隔（1-1440） |
| `/抽奖间隔小时 <小时>` | 设置开奖后几小时自动下一轮（0=关闭） |
| `/抽奖在线 开\|关` | 仅在线玩家参与开关 |
| `/抽奖列表` | 查看绑定详情与中奖状态 |
| `/抽奖历史 [轮次数]` | 查看历史开奖记录（默认仅管理员，可配置全员） |

---

## 配置说明

插件配置位于 `_conf_schema.json`，支持在 **AstrBot 管理面板**可视化编辑。

### 1. RCON 连接

```json
{
  "rcon": {
    "host": "127.0.0.1",
    "port": 25575,
    "password": "",
    "group_overrides": ""
  }
}
```

`group_overrides` 为分群独立 RCON 配置，JSON 格式：`{"群号": {"host":"...","port":25575,"password":"..."}}`。

### 2. 管理员

```json
{
  "super_admin": "你的QQ号",
  "group_admins": "{\"群号\": \"QQ1,QQ2\"}"
}
```

以下任一满足即为管理员：
1. `super_admin` 配置的 QQ 号
2. `group_admins` 中对应群号的 QQ 列表

### 3. 基础参数

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `bind_file` | string | `data/plugin_data/.../bindings.json` | 绑定数据存储路径 |
| `default_auto_interval` | int | 10 | 默认自动开奖间隔（分钟） |
| `default_round_interval_hours` | int | 6 | 开奖后几小时自动下一轮（0=关闭） |
| `max_history_rounds` | int | 10 | 保留最近多少轮历史（0=不保留） |
| `history_admin_only` | bool | true | 是否仅管理员可查看历史 |
| `default_only_online` | bool | true | 初始仅在线玩家参与 |

### 4. 奖品配置 `default_prizes`

按奖项等级分类，可视化表单编辑。`{player}` 会被替换为中奖玩家的 MC ID：

| 奖项 | prizes | probability | count | command |
|------|--------|-------------|-------|---------|
| 特等奖 | 钻石x64 | 0.05 | 1 | `give {player} minecraft:diamond 64` |
| 一等奖 | 附魔金苹果x1 | 0.15 | 2 | `give {player} minecraft:enchanted_golden_apple 1` |
| 二等奖 | 铁锭x16 | 0.3 | 3 | `give {player} minecraft:iron_ingot 16` |
| 参与奖 | 经验瓶x16 | 0.5 | 5 | `give {player} minecraft:experience_bottle 16` |

### 5. 分群配置

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `group_prizes` | JSON string | 按群号配置独立奖品池，未配置使用 `default_prizes` |
| `group_admins` | JSON string | 按群号配置管理员白名单 |

### 6. 抽奖轮次机制

每轮抽奖中**每个玩家最多中奖一次**，重复抽奖提示已中的奖品：

| 机制 | 说明 |
|------|------|
| 个人抽奖 | `/抽奖` 仅对当前用户执行抽奖判定 |
| 全局开奖 | `/开奖` 对所有已绑定用户开奖并广播（无需开启代理抽奖） |
| 自动开奖 | 按设定间隔自动执行全局开奖 |
| 自动下一轮 | 开奖后 X 小时自动清除本轮记录 |
| 重置 | `/抽奖重置` 归档本轮并立即开启新轮 |

---

> ⚠️ **免责声明**：本插件代码由 AI 辅助生成，亲测可用。功能更新随缘，不保证长期维护。如遇问题请提 [Issue](https://github.com/rogergzl/astrbot_plugin_mc_lottery/issues)。
