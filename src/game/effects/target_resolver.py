"""Step3A ``select_targets`` 操作的目标解析器。

本模块仅供运行时使用，允许调用 cv、u2 与游戏管理器。
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Sequence, Tuple

from src.config.card_priorities import get_evolve_priority_cards
from src.config.game_constants import DEFAULT_ATTACK_RANDOM, DEFAULT_ATTACK_TARGET
from src.game.policy.targets import TargetSelector


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _parse_target_spec(target: Any) -> Tuple[str, str, Dict[str, Any]]:
    if not isinstance(target, dict):
        return "", "", {}
    kind = str(target.get("kind") or "")
    selector = str(target.get("selector") or "")
    params = target.get("params")
    if not isinstance(params, dict):
        params = {}
    return kind, selector, params


def _random_enemy_leader_target() -> Tuple[int, int]:
    x = int(DEFAULT_ATTACK_TARGET[0]) + random.randint(-DEFAULT_ATTACK_RANDOM, DEFAULT_ATTACK_RANDOM)
    y = int(DEFAULT_ATTACK_TARGET[1]) + random.randint(-DEFAULT_ATTACK_RANDOM, DEFAULT_ATTACK_RANDOM)
    return (x, y)


def _scan_enemy_followers(ds: Any, *, is_select_ui: bool) -> Tuple[Any, List[Any]]:
    screenshot = None
    try:
        screenshot = ds.take_screenshot()
    except Exception:
        screenshot = None

    if screenshot is None:
        return None, []

    try:
        enemy_followers = (
            ds.game_manager.scan_enemy_followers(screenshot, is_select=bool(is_select_ui))
            if ds.game_manager
            else []
        )
    except Exception:
        enemy_followers = []

    return screenshot, list(enemy_followers or [])


def _scan_ward_targets(
    ds: Any,
    *,
    selector: str,
    screenshot: Any,
    enemy_followers: Sequence[Any],
    is_select_ui: bool,
) -> List[Tuple[int, int]]:
    if selector != "ward_or_highest_hp":
        return []

    try:
        if ds.game_manager and hasattr(ds.game_manager, "scan_shield_targets_for_enemy_followers"):
            return ds.game_manager.scan_shield_targets_for_enemy_followers(
                screenshot,
                enemy_followers,
                is_select=bool(is_select_ui),
            )
        return ds.game_manager.scan_shield_targets() if ds.game_manager else []
    except Exception:
        return []


def _enemy_follower_fallback_flags(selector: str, params: Dict[str, Any]) -> Tuple[bool, bool]:
    try:
        if selector == "ward_or_highest_hp":
            allow_amulet_fallback = bool(params.get("allow_amulet_fallback", True))
        elif selector == "hp_leq_or_highest_hp":
            allow_amulet_fallback = bool(params.get("allow_amulet_fallback", False))
        else:
            allow_amulet_fallback = bool(params.get("allow_amulet_fallback", False))
        fallback_to_enemy_leader = bool(params.get("fallback_to_enemy_leader", False))
        return allow_amulet_fallback, fallback_to_enemy_leader
    except Exception:
        return False, False


def _pick_enemy_follower_targets(
    enemy_followers: Sequence[Any],
    *,
    selector: str,
    params: Dict[str, Any],
    n: int,
    distinct_xy: bool,
    wards: Sequence[Tuple[int, int]],
) -> List[Any]:
    picked: List[Any] = []

    # 优先过滤掉护符（ftype == "amulet" 或 is_amulet == True）
    # 以及具有魔免状态（has_cant_select == True）的目标，避免能力指定失败或误选护符
    candidates = [
        ef for ef in enemy_followers
        if not (len(ef) > 2 and str(ef[2]) == "amulet")
        and not (len(ef) > 5 and bool(ef[5]))
        and not (len(ef) > 8 and ef[8] is True)
    ]
    if not candidates and enemy_followers:
        # 如果过滤后无可选随从，能力无法选择任何敌方随从
        return []
    target_pool = candidates

    if selector in ("", "highest_hp"):
        if n <= 1:
            one = TargetSelector.enemy_follower_highest_hp(target_pool)
            if one is not None:
                picked = [one]
        else:
            picked = TargetSelector.enemy_followers_highest_hp(
                target_pool,
                n=n,
                distinct_xy=bool(distinct_xy),
            )

    elif selector == "lowest_hp":
        if target_pool:
            sorted_pool = sorted(target_pool, key=lambda f: _safe_int(f[3] if len(f) > 3 else 999, 999))
            picked = list(sorted_pool[:n])

    elif selector == "highest_atk":
        if target_pool:
            def _atk_key(f):
                if len(f) > 7:
                    return _safe_int(f[7], 0)
                return _safe_int(f[3] if len(f) > 3 else 0, 0)
            sorted_pool = sorted(target_pool, key=_atk_key, reverse=True)
            picked = list(sorted_pool[:n])

    elif selector == "hp_leq":
        max_hp = _safe_int(params.get("max_hp", 0), 0)
        if n <= 1:
            one = TargetSelector.enemy_follower_hp_leq(target_pool, max_hp=max_hp)
            if one is not None:
                picked = [one]
        else:
            picked = TargetSelector.enemy_followers_hp_leq(
                target_pool,
                max_hp=max_hp,
                n=n,
            )

    elif selector == "hp_leq_or_highest_hp":
        max_hp = _safe_int(params.get("max_hp", 0), 0)
        if n <= 1:
            one = TargetSelector.enemy_follower_hp_leq(target_pool, max_hp=max_hp)
            if one is None:
                one = TargetSelector.enemy_follower_highest_hp(target_pool)
            if one is not None:
                picked = [one]
        else:
            picked = TargetSelector.enemy_followers_hp_leq(
                target_pool,
                max_hp=max_hp,
                n=n,
            )
            if not picked:
                picked = TargetSelector.enemy_followers_highest_hp(
                    target_pool,
                    n=n,
                    distinct_xy=bool(distinct_xy),
                )

    elif selector == "ward_or_highest_hp":
        if n <= 1:
            one = TargetSelector.enemy_follower_ward_or_highest_hp(
                target_pool,
                list(wards or []),
            )
            if one is not None:
                picked = [one]
        else:
            ward_followers = TargetSelector.enemy_followers_in_wards(
                target_pool,
                list(wards or []),
            )
            source = ward_followers if ward_followers else list(target_pool or [])
            picked = TargetSelector.enemy_followers_highest_hp(
                source,
                n=n,
                distinct_xy=bool(distinct_xy),
            )

    return list(picked or [])


def _to_xy_targets(items: Sequence[Any]) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    for item in list(items or []):
        try:
            out.append((_safe_int(item[0], 0), _safe_int(item[1], 0)))
        except Exception:
            continue
    return out


def _resolve_enemy_follower_targets(
    ctx: Any,
    *,
    ds: Any,
    selector: str,
    params: Dict[str, Any],
    n: int,
    distinct_xy: bool,
    is_select_ui: bool,
) -> List[Tuple[int, int]]:
    allow_amulet_fallback, fallback_to_enemy_leader = _enemy_follower_fallback_flags(selector, params)

    screenshot, enemy_followers = _scan_enemy_followers(ds, is_select_ui=bool(is_select_ui))
    if screenshot is None:
        return []

    # 严格过滤出随从列表（排除护符）
    followers_only = [
        ef for ef in enemy_followers
        if not (len(ef) > 2 and str(ef[2]) == "amulet")
        and not (len(ef) > 5 and bool(ef[5]))
    ]

    wards = _scan_ward_targets(
        ds,
        selector=selector,
        screenshot=screenshot,
        enemy_followers=followers_only,
        is_select_ui=bool(is_select_ui),
    )

    if not followers_only and allow_amulet_fallback:
        try:
            from src.bridge.helper import get_memory_adapter
            mem_adapter = get_memory_adapter()
            if mem_adapter and mem_adapter.is_available():
                mem_amulets = mem_adapter.get_enemy_amulets()
                if mem_amulets:
                    return [(_safe_int(a["x"], 0), _safe_int(a["y"], 0)) for a in mem_amulets[:n]]
        except Exception:
            pass

        try:
            amulet_targets = ds.game_manager.card_can_choose_target_like_amulet() if ds.game_manager else []
        except Exception:
            amulet_targets = []

        if not amulet_targets:
            return []
        return _to_xy_targets(list(amulet_targets or [])[:n])

    if not followers_only and fallback_to_enemy_leader:
        return [_random_enemy_leader_target()]

    if not followers_only:
        return []

    picked = _pick_enemy_follower_targets(
        followers_only,
        selector=selector,
        params=params,
        n=n,
        distinct_xy=bool(distinct_xy),
        wards=wards,
    )
    return _to_xy_targets(picked)


def _scan_our_followers_for_target(ctx: Any, ds: Any) -> Sequence[Any]:
    our_followers: Sequence[Any] = []
    try:
        if getattr(ctx, "existing_followers", None) is not None:
            our_followers = list(getattr(ctx, "existing_followers") or [])
    except Exception:
        our_followers = []

    if our_followers:
        return our_followers

    screenshot = None
    try:
        screenshot = ds.take_screenshot()
    except Exception:
        screenshot = None
    if screenshot is None:
        return []

    try:
        return (
            ds.game_manager.scan_our_followers(
                screenshot,
                extra_shots=0,
                sort_desc=False,
                with_names=True,
            )
            if ds.game_manager
            else []
        )
    except Exception:
        return []


def _resolve_friendly_follower_targets(
    ctx: Any,
    *,
    ds: Any,
    selector: str,
    params: Dict[str, Any],
) -> List[Tuple[int, int]]:
    our_followers = _scan_our_followers_for_target(ctx, ds)
    # 严格过滤出随从列表（排除护符）
    our_followers = [
        f for f in our_followers
        if not (len(f) > 2 and str(f[2]) == "amulet")
        and not (len(f) > 5 and bool(f[5]))
    ]
    if not our_followers:
        return []

    exclude_self = bool(params.get("exclude_self", True))
    exclude_names: List[str] = []
    if exclude_self:
        try:
            follower_name = str(getattr(ctx, "follower_name", "") or "")
            if follower_name:
                exclude_names = [follower_name]
        except Exception:
            exclude_names = []

    # 优先在内存桥接模式下支持最高攻击力 / 最低血量选择
    if selector in ("highest_atk", "lowest_hp"):
        try:
            from src.bridge.helper import get_memory_adapter
            mem_adapter = get_memory_adapter()
            if mem_adapter and mem_adapter.is_available():
                our_details = mem_adapter.get_our_field_details()
                candidates = [
                    u for u in our_details
                    if not u.get("is_amulet") and u.get("name") not in exclude_names
                ]
                if candidates:
                    if selector == "highest_atk":
                        candidates.sort(key=lambda u: _safe_int(u.get("attack", 0), 0), reverse=True)
                    else:
                        candidates.sort(key=lambda u: _safe_int(u.get("hp", 1), 1))
                    top = candidates[0]
                    return [(_safe_int(top["x"], 0), _safe_int(top["y"], 0))]
        except Exception:
            pass

    if selector not in ("", "by_evolve_priority"):
        return []

    evolve_priority_cards = get_evolve_priority_cards(getattr(ds, "config", None))
    picked_follower = TargetSelector.friendly_follower_by_evolve_priority(
        our_followers,
        exclude_names=exclude_names,
        evolve_priority_cards=evolve_priority_cards,
    )
    if picked_follower is None:
        return []

    try:
        return [
            (
                _safe_int(picked_follower[0], 0),
                _safe_int(picked_follower[1], 0),
            )
        ]
    except Exception:
        return []


def _resolve_enemy_amulet_targets(
    ctx: Any,
    *,
    ds: Any,
    selector: str,
    params: Dict[str, Any],
    n: int,
) -> List[Tuple[int, int]]:
    """解析敌方护符目标位置。"""
    try:
        from src.bridge.helper import get_memory_adapter
        mem_adapter = get_memory_adapter()
        if mem_adapter and mem_adapter.is_available():
            amulets = mem_adapter.get_enemy_amulets()
            if amulets:
                if selector == "highest_countdown":
                    amulets.sort(key=lambda a: _safe_int(a.get("countdown", 0), 0), reverse=True)
                elif selector == "lowest_countdown":
                    amulets.sort(key=lambda a: _safe_int(a.get("countdown", 0), 0))
                else:  # any
                    amulets.sort(key=lambda a: _safe_int(a.get("x", 0), 0))
                return [(_safe_int(a["x"], 0), _safe_int(a["y"], 0)) for a in amulets[:n]]
    except Exception:
        pass

    # 图色兜底：若内存桥接不可用，尝试扫描似护符目标
    try:
        if ds.game_manager and hasattr(ds.game_manager, "card_can_choose_target_like_amulet"):
            raw_amulets = ds.game_manager.card_can_choose_target_like_amulet()
            if raw_amulets:
                return _to_xy_targets(list(raw_amulets)[:n])
    except Exception:
        pass
    return []


def _resolve_friendly_amulet_targets(
    ctx: Any,
    *,
    ds: Any,
    selector: str,
    params: Dict[str, Any],
    n: int,
) -> List[Tuple[int, int]]:
    """解析我方护符目标位置。"""
    try:
        from src.bridge.helper import get_memory_adapter
        mem_adapter = get_memory_adapter()
        if mem_adapter and mem_adapter.is_available():
            amulets = mem_adapter.get_our_amulets()
            if amulets:
                if selector == "highest_countdown":
                    amulets.sort(key=lambda a: _safe_int(a.get("countdown", 0), 0), reverse=True)
                elif selector == "lowest_countdown":
                    amulets.sort(key=lambda a: _safe_int(a.get("countdown", 0), 0))
                else:  # any
                    amulets.sort(key=lambda a: _safe_int(a.get("x", 0), 0))
                return [(_safe_int(a["x"], 0), _safe_int(a["y"], 0)) for a in amulets[:n]]
    except Exception:
        pass
    return []


def resolve_targets(
    ctx: Any,
    *,
    target: Any,
    count: int = 1,
    distinct_xy: bool = True,
    is_select_ui: bool = True,
) -> List[Tuple[int, int]]:
    """将 ``TargetSpec`` 字典解析为点击位置。"""

    ds = getattr(ctx, "device_state", None)
    if ds is None:
        return []

    kind, selector, params = _parse_target_spec(target)
    n = max(1, _safe_int(count, 1))

    if kind == "enemy_leader":
        return [_random_enemy_leader_target()]

    if kind == "enemy_follower":
        return _resolve_enemy_follower_targets(
            ctx,
            ds=ds,
            selector=selector,
            params=params,
            n=n,
            distinct_xy=bool(distinct_xy),
            is_select_ui=bool(is_select_ui),
        )

    if kind == "enemy_amulet":
        return _resolve_enemy_amulet_targets(
            ctx,
            ds=ds,
            selector=selector,
            params=params,
            n=n,
        )

    if kind == "friendly_follower":
        return _resolve_friendly_follower_targets(
            ctx,
            ds=ds,
            selector=selector,
            params=params,
        )

    if kind == "friendly_amulet":
        return _resolve_friendly_amulet_targets(
            ctx,
            ds=ds,
            selector=selector,
            params=params,
            n=n,
        )

    return []
