"""
出牌特殊操作处理模块
处理出牌时的特殊操作（如选择目标等）
"""

import time
from typing import Any

from src.game.drag_utils import human_like_drag
from src.game.policy.effects import get_card_effect_steps

from src.config.strategy_effects import normalize_effect_steps_to_ops
from src.game.effects import EffectEngine, HandCardContext

class CardPlaySpecialActions:
    """出牌特殊操作处理类"""
    
    def __init__(self, device_state: Any):
        self.device_state = device_state
        self._extra_cost_bonus = 0
        self._should_not_consume_cost = False
        self._should_remove_from_hand = False
        self._force_post_play_hand_refresh = False
        self._preplay_origin_tag_attempted = False
        self._preplay_origin_tag_succeeded = False
    
    def play_single_card(self, card):
        """打出单张牌"""
        self._extra_cost_bonus = 0
        self._should_not_consume_cost = False
        self._should_remove_from_hand = False
        self._force_post_play_hand_refresh = False
        self._preplay_origin_tag_attempted = False
        self._preplay_origin_tag_succeeded = False

        center_x, center_y = card['center']
        target_x = center_x + 40
        card_name = card.get('name', '')
        # 爆能变体可以使用独立配置键。
        cfg_key = card.get('_config_key') or card.get('config_key') or card_name

        # 优先执行配置驱动的效果：桥接模式激活时优先尝试 on_play_bridge，未命中则平滑回退 on_play。
        from src.bridge.snapshot_adapter import is_bridge_mode_active
        is_bridge_active = is_bridge_mode_active(self.device_state)

        steps = None
        used_trigger = "on_play"
        if is_bridge_active:
            steps = get_card_effect_steps(
                self.device_state.config, card_name=str(cfg_key), trigger="on_play_bridge"
            )
            if (not steps) and str(cfg_key) != str(card_name):
                steps = get_card_effect_steps(
                    self.device_state.config, card_name=str(card_name), trigger="on_play_bridge"
                )
            if steps:
                used_trigger = "on_play_bridge"
                try:
                    self.device_state.logger.info(f"[{card_name}] 触发仅桥接(读内存)出牌特殊效果配置")
                except Exception:
                    pass

        if not steps:
            steps = get_card_effect_steps(
                self.device_state.config, card_name=str(cfg_key), trigger="on_play"
            )
            if (not steps) and str(cfg_key) != str(card_name):
                steps = get_card_effect_steps(
                    self.device_state.config, card_name=str(card_name), trigger="on_play"
                )
            used_trigger = "on_play"
        ops = normalize_effect_steps_to_ops(steps)

        if ops:
            pre_action_followers = None
            pre_action_count = None
            if self._ops_require_pre_action_our_followers(ops):
                pre_action_followers, pre_action_count = self._scan_pre_action_our_followers()

        # 先执行普通出牌拖拽，再运行出牌后的效果操作。
            self._default_card_play(center_x, center_y, target_x)
            time.sleep(0.2)

            source_pos = None
            source_uid = None
            if self._ops_require_source_origin(ops):
                self._preplay_origin_tag_attempted = True
                source_pos = self._tag_played_follower_origin(
                    card_name=str(card_name or ""),
                    cfg_key=str(cfg_key or ""),
                )
                if source_pos is not None:
                    self._preplay_origin_tag_succeeded = True
                    try:
                        runtime = getattr(self.device_state, "battle_runtime_state", None)
                        if runtime is not None and hasattr(runtime, "get_ours_uid"):
                            source_uid = runtime.get_ours_uid(
                                source_pos,
                                fallback_name=str(card_name or ""),
                            )
                    except Exception:
                        source_uid = None

            ctx = HandCardContext(
                device_state=self.device_state,
                card_name=str(card_name),
                cfg_key=str(cfg_key),
                card_center=(int(center_x), int(center_y)),
                play_target=(int(target_x), 400),
                follower_pos=source_pos,
                follower_uid=int(source_uid) if source_uid is not None else None,
                card=dict(card or {}),
                pre_action_our_followers=pre_action_followers,
                pre_action_our_follower_count=pre_action_count,
            )
            run_result = EffectEngine.run_ops(ops, ctx=ctx, trigger_id=used_trigger)
            self._force_post_play_hand_refresh = bool(
                getattr(ctx, "force_post_play_hand_refresh", False)
            )

            try:
                bonus = int(getattr(ctx, "extra_cost_bonus", 0) or 0)
            except Exception:
                bonus = 0
            self._extra_cost_bonus = int(bonus)

            # 敌方目标（随从/护符）选择失败时统一按“不消耗费用、本回合忽略该卡”处理。
            fail_kinds = list(getattr(ctx, "select_targets_fail_kinds", []) or [])
            success_kinds = set(str(k) for k in list(getattr(ctx, "select_targets_success_kinds", []) or []))
            enemy_target_failed = any(
                str(k) in ("enemy_follower", "enemy_amulet") and str(k) not in success_kinds
                for k in fail_kinds
            )
            if enemy_target_failed:
                self.device_state.logger.info(
                    f"[{card_name}] 敌方随从目标选择失败，回退为本回合忽略且不耗费"
                )
                try:
                    from src.game.effects.operations import OperationExecutor

                    OperationExecutor.cancel_action(ctx)
                except Exception:
                    pass
                self._should_not_consume_cost = True
                self._should_remove_from_hand = True
                return False

            if run_result.aborted:
                self.device_state.logger.warning(
                    f"[{card_name}] on_play effects aborted，回退为本回合忽略且不耗费"
                )
                self._should_not_consume_cost = True
                self._should_remove_from_hand = True
                return False

        else:
            # 普通卡牌，正常打出
            self._default_card_play(center_x, center_y, target_x)

        return True

    @staticmethod
    def _ops_require_source_origin(ops) -> bool:
        """判断 ``on_play`` 操作是否需要精确的来源随从位置。"""

        for step in list(ops or []):
            if not isinstance(step, dict):
                continue
            if str(step.get("op") or "") in ("buff", "buff_attack_times"):
                return True
        return False

    @staticmethod
    def _ops_require_pre_action_our_followers(ops) -> bool:
        for step in list(ops or []):
            if isinstance(step, dict) and str(step.get("op") or "") == "select_option_by_our_followers":
                return True
        return False

    def _scan_pre_action_our_followers(self):
        try:
            game_manager = getattr(self.device_state, "game_manager", None)
            if game_manager is None:
                return None, None
            screenshot = self.device_state.take_screenshot()
            if screenshot is None:
                return None, None
            followers = game_manager.scan_our_followers(
                screenshot,
                extra_shots=0,
                with_names=True,
            )
            followers_list = list(followers or [])
            try:
                runtime = getattr(self.device_state, "battle_runtime_state", None)
                if runtime is not None and hasattr(runtime, "sync_ours"):
                    runtime.sync_ours(followers_list)
            except Exception:
                pass
            try:
                self.device_state.logger.info(f"[Effect] pre_action_our_followers count={len(followers_list)}")
            except Exception:
                pass
            return followers_list, len(followers_list)
        except Exception as e:
            try:
                self.device_state.logger.warning(f"[Effect] pre_action_our_followers scan failed: {e}")
            except Exception:
                pass
            return None, None

    def _tag_played_follower_origin(self, *, card_name: str, cfg_key: str):
        """尝试用配置键标记刚打出的随从。"""

        runtime = getattr(self.device_state, "battle_runtime_state", None)
        game_manager = getattr(self.device_state, "game_manager", None)
        if runtime is None or game_manager is None:
            return None

        try:
            max_wait_s = 3.0
            interval_s = 0.2
            deadline = time.time() + float(max_wait_s)

            while True:
                screenshot = self.device_state.take_screenshot()
                if screenshot is not None:
                    followers = game_manager.scan_our_followers(
                        screenshot,
                        extra_shots=0,
                        sort_desc=True,
                        shot_delay_range=(0.05, 0.10),
                        with_names=True,
                    )

                    if followers:
                        runtime.sync_ours(followers)
                        pos = runtime.mark_latest_play_origin(
                            card_name=str(card_name or ""),
                            cfg_key=str(cfg_key or ""),
                        )
                        if pos is not None:
                            return pos

                now = time.time()
                if now >= deadline:
                    break
                self.device_state.sleep(min(float(interval_s), max(0.0, deadline - now)))

            return None
        except Exception:
            return None

    def _default_card_play(self, center_x, center_y, target_x):
        """默认卡牌打出"""
        u2_device = self.device_state.require_u2_device()
        if u2_device is None:
            return
        human_like_drag(u2_device, center_x, center_y, target_x, 400)
