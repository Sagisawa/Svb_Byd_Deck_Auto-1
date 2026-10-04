"""Memory-based Combat Executor.

Executes CombatPlans with memory feedback verification.
Executes evolutions and drags according to the optimized combat plan,
verifying each step against tracker memory.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from src.bridge.snapshot_adapter import SnapshotAdapter
from src.config import settings
from src.game.battle.memory_combat_planner import CombatAction, CombatPlan
from src.game.drag_utils import human_like_drag

logger = logging.getLogger(__name__)


class MemoryCombatExecutor:
    """Executes a CombatPlan using device actions with memory feedback."""

    def __init__(self, game_actions: Any, snapshot_adapter: Optional[SnapshotAdapter] = None) -> None:
        self.actions = game_actions
        self.device_state = getattr(game_actions, "device_state", None)
        if snapshot_adapter is not None:
            self.adapter = snapshot_adapter
        else:
            from src.bridge.helper import get_memory_adapter
            self.adapter = get_memory_adapter()

    def execute(self, plan: CombatPlan) -> bool:
        """Executes the combat plan step by step with memory feedback.

        Returns True if execution completed successfully.
        """
        if not plan or not plan.actions:
            logger.info("[MemoryCombatExecutor] No actions to execute.")
            return True

        logger.info(
            f"[MemoryCombatExecutor] Executing combat plan ({len(plan.actions)} actions, "
            f"expected face damage: {plan.total_face_damage}, is_lethal: {plan.is_lethal})"
        )

        for step_idx, action in enumerate(plan.actions):
            logger.info(
                f"[MemoryCombatExecutor] Step {step_idx + 1}/{len(plan.actions)}: "
                f"{action.action_type} - {action.source_name} -> {action.target_name or 'Leader'}"
            )

            if action.action_type in ("evolve_normal", "evolve_super"):
                success = self._execute_evolution(action)
                if not success:
                    logger.warning(
                        f"[MemoryCombatExecutor] Failed to evolve {action.source_name}, proceeding with attacks"
                    )
            elif action.action_type == "attack_leader":
                self._execute_attack_leader(action)
            elif action.action_type == "attack_follower":
                self._execute_attack_follower(action)
            else:
                logger.warning(f"[MemoryCombatExecutor] Unknown action type: {action.action_type}")

        logger.info("[MemoryCombatExecutor] Combat plan execution completed.")
        return True

    def _execute_evolution(self, action: CombatAction) -> bool:
        """Executes normal or super evolution on the specified follower."""
        device = self.actions._require_u2_device()
        pos = action.source_pos

        logger.info(f"[MemoryCombatExecutor] Clicking follower at {pos} for evolution")
        device.click(pos[0], pos[1])
        self.device_state.sleep(0.4)

        screenshot = self.device_state.take_screenshot()
        if screenshot is None:
            logger.warning("[MemoryCombatExecutor] Screenshot failed during evolution")
            self._close_panel_safely()
            return False

        screenshot_np = np.array(screenshot)
        screenshot_cv = cv2.cvtColor(screenshot_np, cv2.COLOR_RGB2BGR)

        cfg = getattr(self.device_state, "config", None)
        is_super = (action.action_type == "evolve_super")

        evolve_ok = False
        if is_super:
            evolve_ok = self.actions._try_apply_super_evolution(
                screenshot_cv,
                pos,
                follower_name=action.source_name,
                follower_type="planned",  # Prevents legacy auto-drag in super evolution
                evolve_uid=action.source_uid,
                all_followers=[],
                runtime_cfg=cfg,
            )
        else:
            evolve_ok = self.actions._try_apply_normal_evolution(
                screenshot_cv,
                pos,
                follower_name=action.source_name,
                evolve_uid=action.source_uid,
                all_followers=[],
                runtime_cfg=cfg,
            )

        self._close_panel_safely()

        # Memory verification
        if self.adapter and self.adapter.is_available():
            evo_info = self.adapter.get_evolution_info()
            logger.info(
                f"[MemoryCombatExecutor] Post-evolve memory check: EP={evo_info.get('ep')}, SEP={evo_info.get('sep')}"
            )

        return evolve_ok

    def _execute_attack_leader(self, action: CombatAction) -> None:
        """Drags friendly follower to attack the enemy leader."""
        device = self.actions._require_u2_device()
        src_x, src_y = action.source_pos
        tgt_x, tgt_y = action.target_pos or (646, 64)

        # Apply slight human-like randomness
        target_x = tgt_x + random.randint(-4, 4)
        target_y = tgt_y + random.randint(-4, 4)

        logger.info(f"[MemoryCombatExecutor] Dragging {action.source_name} ({src_x}, {src_y}) -> Face ({target_x}, {target_y})")
        human_like_drag(
            device,
            src_x,
            src_y,
            target_x,
            target_y,
            duration=random.uniform(*settings.get_human_like_drag_duration_range()),
        )

        self._record_attack_spent(action)
        self._trigger_on_attack_effect(action)
        self.device_state.sleep(0.6)

    def _execute_attack_follower(self, action: CombatAction) -> None:
        """Drags friendly follower to attack an enemy follower."""
        device = self.actions._require_u2_device()
        src_x, src_y = action.source_pos
        if not action.target_pos:
            logger.warning("[MemoryCombatExecutor] Missing target_pos for follower attack")
            return

        tgt_x, tgt_y = action.target_pos
        target_x = tgt_x + random.randint(-4, 4)
        target_y = tgt_y + random.randint(-4, 4)

        logger.info(
            f"[MemoryCombatExecutor] Dragging {action.source_name} ({src_x}, {src_y}) -> "
            f"{action.target_name} ({target_x}, {target_y})"
        )
        human_like_drag(
            device,
            src_x,
            src_y,
            target_x,
            target_y,
            duration=random.uniform(*settings.get_human_like_drag_duration_range()),
        )

        self._record_attack_spent(action)
        self._trigger_on_attack_effect(action)
        self.device_state.sleep(1.3)

    def _record_attack_spent(self, action: CombatAction) -> None:
        """Updates internal battle runtime and slot tracker for spent attack."""
        runtime = getattr(self.actions, "battle_runtime", None)
        if runtime is not None and hasattr(runtime, "mark_our_attack_spent"):
            try:
                runtime.mark_our_attack_spent(
                    action.source_pos,
                    follower_uid=action.source_uid,
                    fallback_name=str(action.source_name or ""),
                )
            except Exception:
                pass
        try:
            self.actions._mark_recent_attack_slot(action.source_pos)
        except Exception:
            pass

    def _trigger_on_attack_effect(self, action: CombatAction) -> None:
        """Triggers configured on_attack / on_attack_bridge card effects if applicable."""
        if hasattr(self.actions, "_run_on_attack_effects"):
            try:
                self.actions._run_on_attack_effects(action.source_name, action.source_pos)
            except Exception:
                pass

    def _close_panel_safely(self) -> None:
        """Clicks blank panel to close any follower details dialog."""
        try:
            self.actions._click_blank_panel(sleep_seconds=0.3)
        except Exception:
            pass
