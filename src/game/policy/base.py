"""策略接口。

当前只引入最小对战策略钩子，使现有逻辑可在不改变外部行为的前提下逐步迁移。
"""

from __future__ import annotations

from typing import Any, Protocol

from src.config.card_priorities import is_evolve_priority_card
from src.config.strategy_effects import card_effect_has_op


class _BattleActionsLike(Protocol):
    device_state: Any
    follower_manager: Any

    def _scan_enemy_ATK(self, screenshot: Any) -> Any: ...


class BattlePolicy(Protocol):
    name: str

    def should_evolve(self, actions: _BattleActionsLike) -> bool: ...


class LegacyBattlePolicy:
    """保留当前硬编码进化决策的策略。"""

    name = "legacy"

    @staticmethod
    def _allows_empty_evolve_for_active_trigger(ds: Any, follower_name: Any) -> bool:
        name = str(follower_name or "")
        if not name:
            return True
        config = getattr(ds, "config", None)

        from src.bridge.snapshot_adapter import is_bridge_mode_active
        bridge_active = is_bridge_mode_active(ds)
        from src.game.policy.effects import get_card_effect_steps

        def _has_disallow(trig: str) -> bool:
            if bridge_active:
                b_trig = f"{trig}_bridge"
                if get_card_effect_steps(config, card_name=name, trigger=b_trig):
                    return card_effect_has_op(
                        config,
                        card_name=name,
                        trigger=b_trig,
                        op_id="disallow_empty_evolve",
                    )
            return card_effect_has_op(
                config,
                card_name=name,
                trigger=trig,
                op_id="disallow_empty_evolve",
            )

        if getattr(ds, "super_evolution_point", 0) > 0 and not _has_disallow("on_super_evolve"):
            return True

        if getattr(ds, "evolution_point", 0) > 0 and not _has_disallow("on_evolve"):
            return True

        return False

    def should_evolve(self, actions: _BattleActionsLike) -> bool:
        ds = actions.device_state

        # 必须拥有可用进化点。
        if not (getattr(ds, "evolution_point", 0) > 0 or getattr(ds, "super_evolution_point", 0) > 0):
            return False

        # 条件一：敌方场上存在随从。
        from src.bridge.helper import get_memory_adapter
        mem_adapter = get_memory_adapter()
        if mem_adapter and mem_adapter.is_available():
            raw_enemies = mem_adapter.get_enemy_followers() or []
            mem_enemies = [
                e for e in raw_enemies
                if not (len(e) > 2 and str(e[2]) == "amulet")
                and not (len(e) > 5 and bool(e[5]))
            ]
            if mem_enemies:
                ds.logger.info(f"[内存] 检测到敌方随从({len(mem_enemies)}个)，满足进化/超进化条件")
                return True
        else:
            cached_enemy_presence = getattr(actions, "_cached_enemy_presence_for_evolve", None)
            if cached_enemy_presence is not None:
                if bool(cached_enemy_presence):
                    ds.logger.info("复用缓存检测到敌方随从，满足进化/超进化条件")
                    return True
            else:
                screenshot = ds.take_screenshot()
                if screenshot:
                    try:
                        enemy_followers = actions._scan_enemy_ATK(screenshot)
                    except Exception:
                        enemy_followers = []
                    if enemy_followers:
                        ds.logger.info("检测到敌方随从，满足进化/超进化条件")
                        return True

        # 条件二：我方存在绿色标记的疾驰随从。
        if mem_adapter and mem_adapter.is_available():
            our_followers = mem_adapter.get_our_followers() or []
        else:
            try:
                manager = getattr(actions, "follower_manager", None)
                if manager is None or not hasattr(manager, "get_positions"):
                    our_followers = []
                else:
                    our_followers = manager.get_positions() or []
            except Exception:
                our_followers = []
        green_followers = [
            f
            for f in our_followers
            if len(f) > 2
            and f[2] == "green"
            and self._allows_empty_evolve_for_active_trigger(ds, f[3] if len(f) > 3 else None)
        ]
        if green_followers:
            ds.logger.info("检测到我方疾驰随从，满足进化/超进化条件")
            return True

        # 条件三：存在配置了进化优先级的随从。
        for follower in our_followers:
            if len(follower) > 2 and str(follower[2]) == "amulet":
                continue
            if len(follower) > 5 and bool(follower[5]):
                continue
            follower_name = follower[3] if len(follower) > 3 else None
            if follower_name and is_evolve_priority_card(
                follower_name, getattr(ds, "config", None)
            ) and self._allows_empty_evolve_for_active_trigger(
                ds, follower_name
            ):
                ds.logger.info(f"检测到优先进化随从[{follower_name}]，满足进化/超进化条件")
                return True

        return False
