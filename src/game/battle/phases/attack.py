"""攻击阶段包装器。"""

from __future__ import annotations


class AttackPhase:
    def __init__(self, actions):
        self.actions = actions

    def run(self, enemy_check) -> None:
        ds = self.actions.device_state
        ds.logger.info("[Phase] attack")

    # 攻击阶段优先执行全新严格扫描，避免旧缓存造成漏检；基础策略为三帧带名称采样，
    # 重试由下层实现。
        our_followers = self.actions._refresh_our_followers(
            sort_desc=True,
            extra_shots=2,
            retries=0,
            with_names=True,
            allow_cached_fallback=False,
        )

        # 严格扫描完全无结果时，仅在视觉模式下把缓存随从作为最后回退；使用内存检测时直接以内存为准。
        if not our_followers:
            try:
                from src.bridge.helper import get_memory_adapter
                mem_adapter = get_memory_adapter()
                if not (mem_adapter and mem_adapter.is_available()):
                    fm = getattr(self.actions, "follower_manager", None)
                    is_fresh = bool(
                        fm is not None
                        and hasattr(fm, "is_fresh")
                        and fm.is_fresh(max_age_seconds=0.8)
                    )
                    if is_fresh and fm is not None and hasattr(fm, "get_positions_sorted"):
                        our_followers = fm.get_positions_sorted(sort_desc=True)
                    elif is_fresh and fm is not None:
                        our_followers = sorted(
                            (fm.get_positions() or []), key=lambda f: int(f[0]), reverse=True
                        )
                    else:
                        our_followers = []
            except Exception:
                our_followers = []

        self.actions.perform_follower_attacks(enemy_check, all_followers=our_followers)
