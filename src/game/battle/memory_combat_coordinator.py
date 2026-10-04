"""Memory Combat Coordinator.

Orchestrates memory-driven combat:
1. Obtains real-time game snapshot from SnapshotAdapter.
2. Invokes MemoryCombatPlanner to compute the optimal damage plan.
3. Executes the plan via MemoryCombatExecutor with memory feedback.
4. Performs post-combat verification and cleanup.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.bridge.snapshot_adapter import SnapshotAdapter, is_bridge_mode_active
from src.game.battle.memory_combat_executor import MemoryCombatExecutor
from src.game.battle.memory_combat_planner import MemoryCombatPlanner

logger = logging.getLogger(__name__)


class MemoryCombatCoordinator:
    """Coordinates memory-based combat optimization and execution."""

    def __init__(self, game_actions: Any, snapshot_adapter: Optional[SnapshotAdapter] = None) -> None:
        self.actions = game_actions
        self.device_state = getattr(game_actions, "device_state", None)
        if snapshot_adapter is not None:
            self.adapter = snapshot_adapter
        else:
            from src.bridge.helper import get_memory_adapter
            self.adapter = get_memory_adapter()

    def run(self, allow_evolve: bool = True) -> bool:
        """Executes memory-based combat optimization.

        Args:
            allow_evolve: Whether evolution actions should be considered.
                          True for perform_fullPlus_actions, False for perform_full_actions.

        Returns:
            bool: True if combat was successfully handled via memory reading mode.
                  False if memory reading mode is unavailable, prompting fallback to vision.
        """
        if not is_bridge_mode_active(self.device_state):
            return False

        if not self.adapter or not self.adapter.is_available():
            logger.debug("[MemoryCombatCoordinator] Memory adapter unavailable, falling back to vision")
            return False

        snapshot = self.adapter.get_combat_snapshot()
        if not snapshot:
            logger.debug("[MemoryCombatCoordinator] Failed to retrieve combat snapshot, falling back to vision")
            return False

        our_followers = snapshot.get("our_followers") or []
        if not our_followers:
            logger.info("[MemoryCombatCoordinator] No friendly followers on board, skipping combat")
            return True

        logger.info(
            f"[MemoryCombatCoordinator] Generating combat plan (allow_evolve={allow_evolve}, "
            f"friendly count={len(our_followers)}, enemy count={len(snapshot.get('enemy_followers') or [])})"
        )

        plan = MemoryCombatPlanner.plan(snapshot, allow_evolve=allow_evolve)

        if not plan.actions:
            logger.info("[MemoryCombatCoordinator] No viable combat actions found in plan")
            return True

        executor = MemoryCombatExecutor(self.actions, self.adapter)
        executor.execute(plan)

        # Post-combat memory check and follow-up
        self._wait_screen_stable(timeout=4.0, desc="战斗结算场面稳定")
        self._check_post_combat_follow_up()

        return True

    def _wait_screen_stable(self, timeout: float = 4.0, desc: str = "") -> bool:
        """Waits for screen animations to finish and stabilize using existing stillness detector."""
        if self.device_state is not None:
            wait_fn = getattr(self.device_state, "wait_for_screen_stable", None)
            if callable(wait_fn):
                try:
                    return bool(wait_fn(timeout=timeout, desc=desc))
                except Exception as e:
                    logger.warning(f"[MemoryCombatCoordinator] wait_for_screen_stable failed: {e}")
            sleep_fn = getattr(self.device_state, "sleep", None)
            if callable(sleep_fn):
                sleep_fn(0.3)
                return True
        return True

    def _check_post_combat_follow_up(self) -> None:
        """Checks if any remaining unspent friendly attacks can deal extra face damage."""
        if not self.adapter or not self.adapter.is_available():
            return

        snap = self.adapter.get_combat_snapshot()
        if not snap:
            return

        enemy_leader_hp = int(snap.get("enemy_leader", {}).get("hp", 0) or 0)
        if enemy_leader_hp <= 0:
            logger.info("[MemoryCombatCoordinator] Enemy leader defeated! (Lethal confirmed)")
            return

        # Check if any enemy wards are still alive
        enemy_followers = snap.get("enemy_followers") or []
        wards_exist = any(
            e.get("has_guard") and int(e.get("hp", 0) or 0) > 0 and not e.get("is_amulet")
            for e in enemy_followers
        )
        if wards_exist:
            return

        # If no wards exist, see if any friendly follower unexpectedly still has an attack left
        our_followers = snap.get("our_followers") or []
        extra_face_attackers = [
            f for f in our_followers
            if int(f.get("attacks_left", 0) or 0) > 0
            and bool(f.get("can_attack_leader"))
            and int(f.get("atk", 0) or 0) > 0
        ]

        if extra_face_attackers:
            logger.info(
                f"[MemoryCombatCoordinator] Found {len(extra_face_attackers)} remaining face attacker(s), executing follow-up"
            )
            follow_up_plan = MemoryCombatPlanner.plan(snap, allow_evolve=False)
            if follow_up_plan and follow_up_plan.actions:
                executor = MemoryCombatExecutor(self.actions, self.adapter)
                executor.execute(follow_up_plan)
                self._wait_screen_stable(timeout=4.0, desc="追击结算场面稳定")
