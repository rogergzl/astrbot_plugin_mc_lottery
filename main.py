import os
import json
import random
import asyncio
import time
from collections import defaultdict
from typing import Dict, List, Set, Optional, Tuple

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from pathlib import Path
import astrbot.api.message_components as Comp
from ._transport import rcon_command


class GroupState:
    """单个群聊的抽奖状态"""
    def __init__(self):
        self.lottery_enabled: bool = False
        self.only_online: bool = True
        self.interval_minutes: int = 10
        self.round_interval_hours: int = 6
        self.round_winners: Dict[str, Tuple[str, str, str]] = {}
        self.auto_task: Optional[asyncio.Task] = None
        self.next_round_task: Optional[asyncio.Task] = None
        self.last_umo: Optional[str] = None
        self.notify_enabled: bool = True
        self.round_number: int = 1
        self._initialized: bool = False


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        data_dir = str(Path(get_astrbot_data_path()) / "plugin_data" / self.name)
        os.makedirs(data_dir, exist_ok=True)
        self.bind_file = self.config.get("bind_file", os.path.join(data_dir, "bindings.json"))

        self.bindings: Dict[str, str] = {}
        self.auto_lottery_users: Set[str] = set()
        self.auto_redeem_users: Set[str] = set()
        self.prize_queue: List[dict] = []
        self.round_history: Dict[str, List[dict]] = {}
        self.redeem_history: Dict[str, List[dict]] = {}

        self.group_states: Dict[str, GroupState] = defaultdict(GroupState)

        self._default_auto_interval: int = self.config.get("default_auto_interval", 10)
        self._default_round_interval_hours: int = self.config.get("default_round_interval_hours", 6)

        self._file_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()

        task = asyncio.create_task(self.load_data())
        task.add_done_callback(self._on_load_data_done)

    # ==============================================================
    # 配置解析
    # ==============================================================
    def _parse_json_config(self, raw, default=None):
        if default is None:
            default = {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("[MC抽奖] 配置 JSON 解析失败: %s", raw[:100])
                return default
        return default

    # ==============================================================
    # 权限判断
    # ==============================================================
    def _is_admin(self, event: AstrMessageEvent, group_id: str) -> bool:
        sender_id = event.get_sender_id()
        super_admin = str(self.config.get("super_admin", "")).strip()
        if super_admin and sender_id == super_admin:
            return True
        group_admins = self._parse_json_config(self.config.get("group_admins", ""))
        admin_str = group_admins.get(group_id, "")
        admins = [a.strip() for a in str(admin_str).split(",") if a.strip()]
        return sender_id in admins

    # ==============================================================
    # 奖品配置
    # ==============================================================
    def _get_prizes(self, group_id: str) -> dict:
        group_prizes = self._parse_json_config(self.config.get("group_prizes", ""))
        if group_id in group_prizes:
            return group_prizes[group_id]
        return self.config.get("default_prizes", {})

    # ==============================================================
    # RCON 配置
    # ==============================================================
    def _get_rcon_config(self, group_id: str):
        rcon = self.config.get("rcon", {})
        if not isinstance(rcon, dict):
            rcon = {}
        host = rcon.get("host", "127.0.0.1")
        port = int(rcon.get("port", 25575))
        password = rcon.get("password", "")
        group_overrides = self._parse_json_config(rcon.get("group_overrides", ""))
        if group_id in group_overrides:
            override = group_overrides[group_id]
            if isinstance(override, dict):
                host = override.get("host", host)
                port = int(override.get("port", port))
                password = override.get("password", password)
        return host, port, password

    # ==============================================================
    # 持久化
    # ==============================================================
    def _on_load_data_done(self, task: asyncio.Task):
        try:
            task.result()
        except Exception as e:
            logger.error("[MC抽奖] 初始化加载数据失败: %s", e)

    async def load_data(self):
        async with self._file_lock:
            try:
                if os.path.exists(self.bind_file):
                    with open(self.bind_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.bindings = data.get("bindings", {})
                    self.auto_lottery_users = set(data.get("auto_lottery_users", []))
                    self.auto_redeem_users = set(data.get("auto_redeem_users", []))
                    self.prize_queue = data.get("prize_queue", [])
                    loaded_history = data.get("round_history", {})
                    if isinstance(loaded_history, dict):
                        self.round_history = loaded_history
                    loaded_redeem = data.get("redeem_history", {})
                    if isinstance(loaded_redeem, dict):
                        self.redeem_history = loaded_redeem
                    logger.info("[MC抽奖] 已加载绑定 %d 条，历史轮次 %d 群，奖品队列 %d 条",
                                len(self.bindings), len(self.round_history), len(self.prize_queue))
                else:
                    logger.info("[MC抽奖] 数据文件不存在，初始化为空")
            except Exception as e:
                logger.error("[MC抽奖] 加载数据失败: %s", e)

    async def save_data(self):
        async with self._file_lock:
            try:
                data = {
                    "bindings": self.bindings,
                    "auto_lottery_users": list(self.auto_lottery_users),
                    "auto_redeem_users": list(self.auto_redeem_users),
                    "prize_queue": self.prize_queue,
                    "round_history": self.round_history,
                    "redeem_history": self.redeem_history,
                }
                with open(self.bind_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error("[MC抽奖] 保存数据失败: %s", e)

    # ==============================================================
    # umo 缓存
    # ==============================================================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def cache_umo(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if group_id:
            self.group_states[group_id].last_umo = event.unified_msg_origin

    # ==============================================================
    # 在线玩家
    # ==============================================================
    async def get_online_players(self, group_id: str) -> List[str]:
        host, port, password = self._get_rcon_config(group_id)
        if not host or not password:
            logger.warning("[MC抽奖] RCON 未配置，无法查询在线玩家")
            return []
        try:
            raw = await rcon_command(host, port, password, "list")
            if ":" in raw:
                names_part = raw.split(":", 1)[1].strip()
                names = [n.strip() for n in names_part.split(",") if n.strip()]
                return names
        except Exception as e:
            logger.error("[MC抽奖] 查询在线玩家失败 (群 %s): %s", group_id, e)
        return []

    # ==============================================================
    # 核心：个人抽奖
    # ==============================================================
    async def _draw_one(self, group_id: str, qq_id: str) -> Optional[Tuple[str, str, str, str]]:
        """单次个人抽奖，返回 (tier_name, prize_desc, command, mc_id) 或 None"""
        state = self.group_states[group_id]
        if qq_id not in self.bindings:
            return None
        mc_id = self.bindings[qq_id]
        if state.only_online:
            online = await self.get_online_players(group_id)
            if mc_id not in online:
                return None
        prizes_config = self._get_prizes(group_id)
        if not prizes_config or not isinstance(prizes_config, dict):
            return None
        for tier_name, tier_cfg in prizes_config.items():
            if not isinstance(tier_cfg, dict):
                continue
            prob = tier_cfg.get("probability", 0)
            cnt = tier_cfg.get("count", 0)
            if cnt <= 0:
                continue
            if random.random() <= prob:
                cmd = tier_cfg.get("command", "").replace("{player}", mc_id)
                return (tier_name, tier_cfg.get("prizes", ""), cmd, mc_id)
        return None

    async def _execute_prize(self, qq_id: str, command: str, group_id: str, prize_name: str):
        host, port, password = self._get_rcon_config(group_id)
        if not host or not password:
            logger.error("[MC抽奖] RCON 未配置 (群 %s)", group_id)
            return
        try:
            result = await rcon_command(host, port, password, command)
            logger.info("[MC抽奖] RCON 执行成功 (群 %s): %s -> %s", group_id, command, result)
        except Exception as e:
            logger.error("[MC抽奖] RCON 执行失败 (群 %s): %s", group_id, e)
            umo = await self._get_umo(group_id)
            if umo:
                chain = MessageChain([Comp.Plain(
                    u"⚠️ 奖品 [%s] 执行失败：%s\n命令：%s" % (prize_name, str(e), command)
                )])
                await self.context.send_message(umo, chain)
            return
        umo = await self._get_umo(group_id)
        if umo:
            notify = MessageChain([Comp.Plain(
                u"🎁 QQ %s 的奖品 [%s] 已执行：%s" % (qq_id, prize_name, command)
            )])
            await self.context.send_message(umo, notify)

    async def _get_umo(self, group_id: str) -> Optional[str]:
        return self.group_states[group_id].last_umo

    async def _send_notify(self, group_id: str, message: str):
        state = self.group_states[group_id]
        if not state.notify_enabled:
            return
        umo = await self._get_umo(group_id)
        if umo:
            chain = MessageChain([Comp.Plain(message)])
            await self.context.send_message(umo, chain)

    # ==============================================================
    # 全局开奖（管理员 /开奖 或自动触发）
    # ==============================================================
    async def _global_draw(self, group_id: str, include_all: bool = False):
        state = self.group_states[group_id]
        if not state.lottery_enabled:
            return
        online = await self.get_online_players(group_id)
        async with self._state_lock:
            eligible = []
            for qq_id, mc_id in self.bindings.items():
                if qq_id in state.round_winners:
                    continue
                if not include_all and qq_id not in self.auto_lottery_users:
                    continue
                if state.only_online and mc_id not in online:
                    continue
                eligible.append((qq_id, mc_id))
            if not eligible:
                logger.info("[MC抽奖] 群 %s 无符合条件玩家参与全局开奖", group_id)
                return
            prizes_config = self._get_prizes(group_id)
            if not prizes_config:
                return
            new_winners: Dict[str, Tuple[str, str, str]] = {}
            already_used_mc = set()
            for tier_name, tier_cfg in prizes_config.items():
                if not isinstance(tier_cfg, dict):
                    continue
                prob = tier_cfg.get("probability", 0)
                cnt = tier_cfg.get("count", 0)
                cmd_tmpl = tier_cfg.get("command", "")
                prize_desc = tier_cfg.get("prizes", "")
                drawn = 0
                attempts = 0
                max_attempts = len(eligible) * 3
                while drawn < cnt and attempts < max_attempts:
                    attempts += 1
                    if random.random() > prob:
                        continue
                    qq_id, mc_id = random.choice(eligible)
                    if mc_id in already_used_mc:
                        continue
                    already_used_mc.add(mc_id)
                    command = cmd_tmpl.replace("{player}", mc_id)
                    new_winners[qq_id] = (tier_name, prize_desc, command)
                    drawn += 1
            auto_exec = []
            if not new_winners:
                logger.info("[MC抽奖] 本轮全局开奖无人中奖")
            else:
                for qq_id, (tn, pd, cmd) in new_winners.items():
                    state.round_winners[qq_id] = (tn, pd, cmd)
                now = time.time()
                for qq_id, (tn, pd, cmd) in new_winners.items():
                    mc_id = self.bindings.get(qq_id, "?")
                    prize_name = "[%s] %s" % (tn, pd)
                    if qq_id in self.auto_redeem_users:
                        self._add_redeem_record(qq_id, mc_id, prize_name, cmd, group_id, "auto")
                        auto_exec.append((qq_id, cmd, prize_name))
                    else:
                        self.prize_queue.append({
                            "qq_id": qq_id, "mc_id": mc_id,
                            "command": cmd,
                            "prize_name": prize_name,
                            "group_id": group_id, "timestamp": now,
                        })
                await self.save_data()
        for qq_id, cmd, name in auto_exec:
            await self._execute_prize(qq_id, cmd, group_id, name)
        logger.info("[MC抽奖] 群 %s 全局开奖产生 %d 名中奖者", group_id, len(new_winners))
        umo = await self._get_umo(group_id)
        if umo:
            if include_all:
                all_winners = dict(state.round_winners)
                if all_winners:
                    lines = [u"🎉 本轮开奖汇总（共 %d 人中奖）：" % len(all_winners)]
                    for qq_id, (tn, pd, _) in all_winners.items():
                        mc_id = self.bindings.get(qq_id, "?")
                        lines.append(u"  - @%s(%s) 获得 [%s] %s" % (qq_id, mc_id, tn, pd))
                    lines.append(u"使用 /抽奖兑换 可手动领取奖励")
                    chain = MessageChain([Comp.Plain("\n".join(lines))])
                    await self.context.send_message(umo, chain)
                else:
                    await self.context.send_message(umo,
                        MessageChain([Comp.Plain(u"🎉 本轮开奖汇总：暂无中奖者")]
                    ))
            elif new_winners:
                lines = [u"🎉 自动开奖结果："]
                for qq_id, (tn, pd, _) in new_winners.items():
                    mc_id = self.bindings.get(qq_id, "?")
                    lines.append(u"  - @%s(%s) 获得 [%s] %s" % (qq_id, mc_id, tn, pd))
                lines.append(u"使用 /抽奖兑换 可手动领取奖励")
                chain = MessageChain([Comp.Plain("\n".join(lines))])
                await self.context.send_message(umo, chain)
        if new_winners:
            self._schedule_next_round(group_id)
        if state.round_interval_hours > 0:
            await self._send_notify(group_id, u"⏱️ 本轮将在 %d 小时后自动结束，管理员 /抽奖重置 可提前开启下一轮" % state.round_interval_hours)

    def _schedule_next_round(self, group_id: str):
        state = self.group_states[group_id]
        if state.next_round_task and not state.next_round_task.done():
            state.next_round_task.cancel()
        hours = state.round_interval_hours
        if hours <= 0:
            return
        if not state.lottery_enabled:
            return

        async def _next():
            await asyncio.sleep(hours * 3600)
            logger.info("[MC抽奖] 群 %s 到达下一轮时间，归档并开启下一轮", group_id)
            async with self._state_lock:
                self._archive_round(group_id)
                state.round_winners.clear()
                await self.save_data()
            old_round = state.round_number
            state.round_number += 1
            await self._send_notify(group_id, u"🔔 第 %d 轮已结束，第 %d 轮开始！/自动抽奖 /自动兑奖\n⏱️ 自动开奖间隔 %d 分钟，本轮将在 %d 小时后自动结束" % (old_round, state.round_number, state.interval_minutes, state.round_interval_hours))
            await self._global_draw(group_id)

        state.next_round_task = asyncio.create_task(_next())
        logger.info("[MC抽奖] 群 %s 已预约 %d 小时后自动开启下一轮", group_id, hours)

    def _archive_round(self, group_id: str):
        """将当前轮次中奖记录归档到历史，并裁剪旧记录"""
        state = self.group_states[group_id]
        if not state.round_winners:
            return
        max_rounds = self.config.get("max_history_rounds", 10)
        if max_rounds <= 0:
            return
        entries = []
        for qq_id, (tn, pd, cmd) in state.round_winners.items():
            mc_id = self.bindings.get(qq_id, "?")
            entries.append({
                "qq_id": qq_id,
                "mc_id": mc_id,
                "tier": tn,
                "prize": pd,
                "command": cmd,
            })
        round_record = {
            "timestamp": time.time(),
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "winners": entries,
        }
        if group_id not in self.round_history:
            self.round_history[group_id] = []
        self.round_history[group_id].append(round_record)
        self._prune_history(group_id, max_rounds)

    def _prune_history(self, group_id: str, max_rounds: int):
        """裁剪历史记录，只保留最近 max_rounds 轮"""
        if group_id not in self.round_history:
            return
        history = self.round_history[group_id]
        if len(history) > max_rounds:
            self.round_history[group_id] = history[-max_rounds:]

    def _add_redeem_record(self, qq_id: str, mc_id: str, prize_name: str, command: str, group_id: str, mode: str):
        max_rounds = self.config.get("max_history_rounds", 10)
        if max_rounds <= 0:
            return
        if group_id not in self.redeem_history:
            self.redeem_history[group_id] = []
        self.redeem_history[group_id].append({
            "qq_id": qq_id,
            "mc_id": mc_id,
            "prize_name": prize_name,
            "command": command,
            "group_id": group_id,
            "mode": mode,
            "timestamp": time.time(),
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self._prune_redeem_history(group_id, max_rounds)

    def _prune_redeem_history(self, group_id: str, max_rounds: int):
        if group_id not in self.redeem_history:
            return
        history = self.redeem_history[group_id]
        if len(history) > max_rounds:
            self.redeem_history[group_id] = history[-max_rounds:]

    # ==============================================================
    # 自动任务
    # ==============================================================
    async def _start_auto(self, group_id: str):
        state = self.group_states[group_id]
        if state.auto_task and not state.auto_task.done():
            return
        state.auto_task = asyncio.create_task(self._auto_loop(group_id))
        logger.info("[MC抽奖] 群 %s 自动开奖已启动", group_id)

    async def _stop_auto(self, group_id: str):
        state = self.group_states[group_id]
        if state.auto_task and not state.auto_task.done():
            state.auto_task.cancel()
            state.auto_task = None
        if state.next_round_task and not state.next_round_task.done():
            state.next_round_task.cancel()
            state.next_round_task = None

    async def _auto_loop(self, group_id: str):
        state = self.group_states[group_id]
        while state.lottery_enabled:
            await asyncio.sleep(state.interval_minutes * 60)
            if not state.lottery_enabled:
                break
            try:
                await self._global_draw(group_id)
            except Exception as e:
                logger.error("[MC抽奖] 自动开奖异常 (群 %s): %s", group_id, e)

    # ==============================================================
    # 命令
    # ==============================================================

    @filter.command("抽奖绑定")
    async def cmd_bind(self, event: AstrMessageEvent, mc_id: str = ""):
        """绑定 QQ 与 Minecraft ID"""
        qq_id = event.get_sender_id()
        if not mc_id:
            yield event.plain_result("❌ 用法：/抽奖绑定 <你的Minecraft ID>")
            return
        mc_id = mc_id.strip()
        if not mc_id:
            yield event.plain_result("❌ MC ID 不能为空")
            return
        async with self._state_lock:
            self.bindings[qq_id] = mc_id
            await self.save_data()
        yield event.plain_result("✅ 绑定成功！QQ %s → MC %s" % (qq_id, mc_id))

    @filter.command("自动抽奖")
    async def cmd_auto_lottery(self, event: AstrMessageEvent, action: str = ""):
        """开启/关闭代理抽奖（自动参与全局开奖）"""
        qq_id = event.get_sender_id()
        action = action.strip()
        async with self._state_lock:
            if action == "开":
                if qq_id in self.auto_lottery_users:
                    msg = "✅ 已处于代理抽奖状态"
                else:
                    self.auto_lottery_users.add(qq_id)
                    msg = "✅ 已开启代理抽奖，后续每轮全局开奖你都会参与"
            elif action == "关":
                if qq_id not in self.auto_lottery_users:
                    msg = "✅ 代理抽奖未开启"
                else:
                    self.auto_lottery_users.remove(qq_id)
                    msg = "✅ 已关闭代理抽奖，你将不再自动参与每轮全局开奖"
            else:
                msg = "❌ 用法：/自动抽奖 开 或 /自动抽奖 关"
            await self.save_data()
        yield event.plain_result(msg)

    @filter.command("自动兑奖")
    async def cmd_auto_redeem(self, event: AstrMessageEvent, action: str = ""):
        """开启/关闭中奖后自动兑换"""
        qq_id = event.get_sender_id()
        action = action.strip()
        async with self._state_lock:
            if action == "开":
                if qq_id in self.auto_redeem_users:
                    msg = "✅ 已处于自动兑奖状态"
                else:
                    self.auto_redeem_users.add(qq_id)
                    msg = "✅ 已开启自动兑奖，中奖后奖励将立即执行"
            elif action == "关":
                if qq_id not in self.auto_redeem_users:
                    msg = "✅ 自动兑奖未开启"
                else:
                    self.auto_redeem_users.remove(qq_id)
                    msg = "✅ 已关闭自动兑奖，中奖后需手动 /抽奖兑换"
            else:
                msg = "❌ 用法：/自动兑奖 开 或 /自动兑奖 关"
            await self.save_data()
        yield event.plain_result(msg)

    @filter.command("抽奖兑换")
    async def cmd_redeem(self, event: AstrMessageEvent):
        """手动领取一条待发放奖品"""
        qq_id = event.get_sender_id()
        group_id = event.get_group_id()
        if not qq_id or not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令")
            return
        idx = None
        async with self._state_lock:
            for i, prize in enumerate(self.prize_queue):
                if prize["qq_id"] == qq_id and prize.get("group_id") == group_id:
                    idx = i
                    break
            if idx is None:
                yield event.plain_result("❌ 你当前没有待领取的奖品")
                return
            prize = self.prize_queue.pop(idx)
            self._add_redeem_record(qq_id, prize.get("mc_id", "?"), prize["prize_name"],
                                    prize["command"], group_id, "manual")
            await self.save_data()
        await self._execute_prize(qq_id, prize["command"], prize["group_id"], prize["prize_name"])
        yield event.plain_result("🎁 奖品已执行，请检查游戏")

    @filter.command("抽奖帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示所有命令帮助"""
        yield event.plain_result(
            "🎰 MC抽奖插件 帮助\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "【账号】\n"
            "  /抽奖绑定 <MC_ID>\n"
            "【参与设置】\n"
            "  /自动抽奖 开|关 — 开关代理抽奖\n"
            "  /自动兑奖 开|关 — 开关自动兑奖\n"
            "【抽奖】\n"
            "  /抽奖 — 个人抽奖（每轮每人最多中一次）\n"
            "  /抽奖兑换 — 手动领取奖品\n"
            "【管理命令（管理员）】\n"
            "  /抽奖开启 — 开启抽奖并启动自动开奖\n"
            "  /抽奖关闭 — 关闭所有抽奖\n"
            "  /开奖 — 立即全局开奖（对所有已绑定用户）\n"
            "  /抽奖重置 — 清除本轮中奖记录，重新开始\n"
            "  /抽奖间隔 <分钟> — 设置自动开奖间隔\n"
            "  /抽奖间隔小时 <小时> — 设置开奖后X小时自动下一轮\n"
            "  /抽奖在线 开|关 — 仅在线玩家开关\n"
            "  /抽奖通知 开|关 — 通知提醒开关\n"
            "  /抽奖列表 — 查看绑定人数与详情\n"
            "  /抽奖历史 [轮次数] — 查看历史开奖记录\n"
            "  /抽奖兑换记录 [条数] — 查看历史兑奖记录\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "管理员: 超级管理员 / 分群白名单"
        )

    @filter.command("抽奖列表")
    async def cmd_list(self, event: AstrMessageEvent):
        """管理员查看绑定人数与详情"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令")
            return
        if not self._is_admin(event, group_id):
            yield event.plain_result("❌ 你没有管理员权限")
            return
        state = self.group_states[group_id]
        async with self._state_lock:
            total = len(self.bindings)
            if total == 0:
                yield event.plain_result("📊 当前没有任何绑定记录")
                return
            lines = [u"📊 总绑定人数: %d" % total, "━━━━━━━━━━━━━━━━━━"]
            for qq_id, mc_id in self.bindings.items():
                agent = "✓" if qq_id in self.auto_lottery_users else "✗"
                redeem = "✓" if qq_id in self.auto_redeem_users else "✗"
                won = state.round_winners.get(qq_id)
                if won:
                    lines.append(u"  QQ:%s → MC:%s  代理:%s 兑换:%s 🏆[%s]%s" % (qq_id, mc_id, agent, redeem, won[0], won[1]))
                else:
                    lines.append(u"  QQ:%s → MC:%s  代理:%s 兑换:%s" % (qq_id, mc_id, agent, redeem))
            lines.append("━━━━━━━━━━━━━━━━━━")
            lines.append(u"本轮中奖: %d人 | 待发奖品: %d条" % (len(state.round_winners), len(self.prize_queue)))
            yield event.plain_result("\n".join(lines))

    @filter.command("抽奖历史")
    async def cmd_history(self, event: AstrMessageEvent, rounds: str = ""):
        """查看历史轮次开奖记录（默认仅管理员，配置中可切换）"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令")
            return
        if self.config.get("history_admin_only", True):
            if not self._is_admin(event, group_id):
                yield event.plain_result("❌ 仅管理员可查看历史记录")
                return
        history = self.round_history.get(group_id, [])
        if not history:
            yield event.plain_result("📋 暂无历史开奖记录")
            return
        try:
            n = int(rounds) if rounds.strip() else 5
        except ValueError:
            n = 5
        if n < 1:
            n = 1
        recent = history[-n:]
        lines = [u"📋 最近 %d 轮开奖记录：" % len(recent)]
        for i, rd in enumerate(recent):
            ts = rd.get("time_str", "?")
            winners = rd.get("winners", [])
            lines.append(u"────────────────────")
            lines.append(u"第 %d 轮  %s  (共 %d 人中奖)" % (i + 1, ts, len(winners)))
            for w in winners:
                lines.append(u"  @%s(%s) → [%s] %s" % (w["qq_id"], w["mc_id"], w.get("tier", ""), w.get("prize", "")))
        yield event.plain_result("\n".join(lines))

    @filter.command("抽奖兑换记录")
    async def cmd_redeem_history(self, event: AstrMessageEvent, show_count: str = ""):
        """查看兑奖记录（默认仅管理员，配置中可切换）"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令")
            return
        if self.config.get("history_admin_only", True):
            if not self._is_admin(event, group_id):
                yield event.plain_result("❌ 仅管理员可查看兑奖记录")
                return
        history = self.redeem_history.get(group_id, [])
        if not history:
            yield event.plain_result("📋 暂无兑奖记录")
            return
        try:
            n = int(show_count) if show_count.strip() else 5
        except ValueError:
            n = 5
        if n < 1:
            n = 1
        recent = history[-n:]
        lines = [u"📋 最近 %d 条兑奖记录：" % len(recent)]
        for i, rd in enumerate(recent):
            ts = rd.get("time_str", "?")
            mode = u"自动" if rd.get("mode") == "auto" else u"手动"
            lines.append(u"────────────────────")
            lines.append(u"%s  [%s]  @%s(%s)" % (ts, mode, rd.get("qq_id", "?"), rd.get("mc_id", "?")))
            lines.append(u"  → %s" % rd.get("prize_name", ""))
        yield event.plain_result("\n".join(lines))

    @filter.command("抽奖")
    async def cmd_personal_draw(self, event: AstrMessageEvent):
        """个人抽奖——每轮每人最多中奖一次"""
        group_id = event.get_group_id()
        qq_id = event.get_sender_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令")
            return
        state = self.group_states[group_id]
        if not state.lottery_enabled:
            yield event.plain_result("❌ 抽奖未开启，请联系管理员使用 /抽奖开启")
            return
        if qq_id not in self.bindings:
            yield event.plain_result("❌ 请先使用 /抽奖绑定 <MC_ID> 绑定账号")
            return
        async with self._state_lock:
            if qq_id in state.round_winners:
                tn, pd, _ = state.round_winners[qq_id]
                yield event.plain_result(
                    u"🔔 你本轮已中奖：[%s] %s，不能重复抽奖！\n"
                    u"使用 /抽奖兑换 领取，或等待管理员 /抽奖重置 开启下一轮。"
                    % (tn, pd)
                )
                return
        online = await self.get_online_players(group_id)
        if state.only_online and self.bindings[qq_id] not in online:
            yield event.plain_result("❌ 你不在线，无法参与抽奖")
            return
        result = await self._draw_one(group_id, qq_id)
        if result is None:
            yield event.plain_result("😔 很遗憾，你没有中奖，下次加油！")
            return
        tn, pd, cmd, mc_id = result
        prize_name = "[%s] %s" % (tn, pd)
        async with self._state_lock:
            state.round_winners[qq_id] = (tn, pd, cmd)
            auto_exec = qq_id in self.auto_redeem_users
            if auto_exec:
                self._add_redeem_record(qq_id, mc_id, prize_name, cmd, group_id, "auto")
            else:
                self.prize_queue.append({
                    "qq_id": qq_id, "mc_id": mc_id,
                    "command": cmd,
                    "prize_name": prize_name,
                    "group_id": group_id, "timestamp": time.time(),
                })
            await self.save_data()
        if auto_exec:
            await self._execute_prize(qq_id, cmd, group_id, prize_name)
        yield event.plain_result(
            u"🎉 恭喜中奖！[%s] %s\n" % (tn, pd)
            + (u"奖励已自动执行！" if auto_exec else u"使用 /抽奖兑换 领取奖励")
        )

    @filter.command("开奖")
    async def cmd_global_draw(self, event: AstrMessageEvent):
        """管理员立即全局开奖"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令")
            return
        if not self._is_admin(event, group_id):
            yield event.plain_result("❌ 仅管理员可执行此操作")
            return
        state = self.group_states[group_id]
        if not state.lottery_enabled:
            yield event.plain_result("❌ 请先使用 /抽奖开启 开启抽奖功能")
            return
        yield event.plain_result("🎰 正在全局开奖，请稍候...")
        await self._global_draw(group_id, include_all=True)

    @filter.command("抽奖重置")
    async def cmd_reset(self, event: AstrMessageEvent):
        """管理员重置本轮抽奖记录，重新开始"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令")
            return
        if not self._is_admin(event, group_id):
            yield event.plain_result("❌ 仅管理员可执行此操作")
            return
        state = self.group_states[group_id]
        async with self._state_lock:
            self._archive_round(group_id)
            state.round_winners.clear()
            await self.save_data()
        if state.next_round_task and not state.next_round_task.done():
            state.next_round_task.cancel()
            state.next_round_task = None
        state.round_number += 1
        if state.round_interval_hours > 0:
            yield event.plain_result(
                u"♻️ 已归档本轮并重置。\n🔔 第 %d 轮抽奖开始！\n💡 /自动抽奖 /自动兑奖\n⏱️ 本轮将在 %d 小时后自动结束" % (state.round_number, state.round_interval_hours)
            )
        else:
            yield event.plain_result(
                u"♻️ 已归档本轮并重置。\n🔔 第 %d 轮抽奖开始！\n💡 /自动抽奖 /自动兑奖\n⏱️ 无自动关闭，需管理员 /抽奖重置" % state.round_number
            )
        await self._send_notify(group_id, u"🔔 第 %d 轮抽奖开始！发送 /抽奖 参与 | /自动抽奖 /自动兑奖" % state.round_number)

    @filter.command("抽奖开启")
    async def cmd_lottery_on(self, event: AstrMessageEvent):
        """管理员开启抽奖并启动自动开奖"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令")
            return
        if not self._is_admin(event, group_id):
            yield event.plain_result("❌ 仅管理员可执行此操作")
            return
        state = self.group_states[group_id]
        if state.lottery_enabled:
            yield event.plain_result("✅ 抽奖已处于开启状态")
            return
        if not state._initialized:
            state.interval_minutes = self._default_auto_interval
            state.round_interval_hours = self._default_round_interval_hours
            state._initialized = True
        state.lottery_enabled = True
        state.last_umo = event.unified_msg_origin
        await self._start_auto(group_id)
        yield event.plain_result(
            "✅ 已开启抽奖功能。\n"
            "• 用户 /抽奖 可个人抽奖\n"
            "• 管理员 /开奖 可全局开奖\n"
            "• 自动开奖间隔 %d 分钟" % state.interval_minutes
        )
        await self._send_notify(group_id, u"🔔 第 %d 轮抽奖已开始！发送 /抽奖 参与 | /自动抽奖 /自动兑奖\n⏱️ 自动开奖间隔 %d 分钟，本轮将在 %d 小时后自动结束" % (state.round_number, state.interval_minutes, state.round_interval_hours))

    @filter.command("抽奖关闭")
    async def cmd_lottery_off(self, event: AstrMessageEvent):
        """管理员关闭所有抽奖"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令")
            return
        if not self._is_admin(event, group_id):
            yield event.plain_result("❌ 仅管理员可执行此操作")
            return
        state = self.group_states[group_id]
        if not state.lottery_enabled:
            yield event.plain_result("✅ 抽奖已处于关闭状态")
            return
        state.lottery_enabled = False
        await self._stop_auto(group_id)
        yield event.plain_result("❌ 已关闭抽奖功能。所有自动任务已停止。")

    @filter.command("抽奖间隔")
    async def cmd_interval(self, event: AstrMessageEvent, minute: str = ""):
        """管理员设置自动开奖间隔（分钟）"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令")
            return
        if not self._is_admin(event, group_id):
            yield event.plain_result("❌ 仅管理员可执行此操作")
            return
        state = self.group_states[group_id]
        try:
            m = int(minute)
            if m < 1 or m > 1440:
                raise ValueError
        except ValueError:
            yield event.plain_result("❌ 请提供有效的分钟数（1-1440）")
            return
        state.interval_minutes = m
        if state.lottery_enabled:
            await self._stop_auto(group_id)
            await self._start_auto(group_id)
        yield event.plain_result("⏱️ 自动开奖间隔已设为 %d 分钟" % m)

    @filter.command("抽奖间隔小时")
    async def cmd_round_interval(self, event: AstrMessageEvent, hour: str = ""):
        """管理员设置开奖后几小时自动开启下一轮"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令")
            return
        if not self._is_admin(event, group_id):
            yield event.plain_result("❌ 仅管理员可执行此操作")
            return
        state = self.group_states[group_id]
        try:
            h = int(hour)
            if h < 0 or h > 48:
                raise ValueError
        except ValueError:
            yield event.plain_result("❌ 请提供有效的小时数（0-48，0表示关闭自动下一轮）")
            return
        state.round_interval_hours = h
        if h == 0:
            yield event.plain_result("⏱️ 已关闭自动下一轮，需管理员手动 /抽奖重置 开启新一轮")
        else:
            yield event.plain_result("⏱️ 开奖后 %d 小时将自动开启下一轮" % h)

    @filter.command("抽奖在线")
    async def cmd_only_online(self, event: AstrMessageEvent, action: str = ""):
        """管理员设置仅在线玩家可参与"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令")
            return
        if not self._is_admin(event, group_id):
            yield event.plain_result("❌ 仅管理员可执行此操作")
            return
        action = action.strip()
        if action == "开":
            self.group_states[group_id].only_online = True
            yield event.plain_result("✅ 已设置仅在线玩家参与抽奖")
        elif action == "关":
            self.group_states[group_id].only_online = False
            yield event.plain_result("✅ 已设置所有已绑定玩家均可参与")
        else:
            yield event.plain_result("❌ 用法：/抽奖在线 开 或 /抽奖在线 关")

    @filter.command("抽奖通知")
    async def cmd_notify(self, event: AstrMessageEvent, action: str = ""):
        """管理员开关通知提醒"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用此命令")
            return
        if not self._is_admin(event, group_id):
            yield event.plain_result("❌ 仅管理员可执行此操作")
            return
        action = action.strip()
        if action == "开":
            self.group_states[group_id].notify_enabled = True
            yield event.plain_result("✅ 已开启通知提醒")
        elif action == "关":
            self.group_states[group_id].notify_enabled = False
            yield event.plain_result("✅ 已关闭通知提醒")
        else:
            yield event.plain_result("❌ 用法：/抽奖通知 开 或 /抽奖通知 关")

    async def terminate(self):
        for gid in list(self.group_states.keys()):
            await self._stop_auto(gid)
        async with self._state_lock:
            await self.save_data()
        logger.info("[MC抽奖] 插件已终止，数据已保存")
