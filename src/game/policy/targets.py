"""目标选择辅助函数。

集中旧版目标选择逻辑，避免卡牌和进化特殊动作各自重复实现
``max(valid_targets, ...)`` 变体。函数保持纯粹，不执行设备、视觉或配置 IO；兼容
当前代码使用的旧数据形态，并尽量维持原有选择语义。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.utils.card_filename import normalize_config_key


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _enemy_hp_key(follower: Sequence[Any]) -> int:
    # 旧版敌方随从元组结构：``(x, y, type, hp_str)``。
    try:
        if len(follower) > 2 and str(follower[2]) == "amulet":
            return -1
        if len(follower) > 5 and bool(follower[5]):
            return -1
        hp = follower[3]
    except Exception:
        return 0

    if isinstance(hp, str):
        return _safe_int(hp, 0) if hp.isdigit() else 0
    return _safe_int(hp, 0)


def _follower_xy(follower: Sequence[Any]) -> Tuple[int, int]:
    return _safe_int(follower[0], 0), _safe_int(follower[1], 0)


class TargetSelector:
    """兼容旧数据形态的选择器集合。"""

    @staticmethod
    def enemy_follower_highest_hp(enemy_followers: Sequence[Sequence[Any]]) -> Optional[Sequence[Any]]:
        """选择生命值最高的敌方随从。

        沿用旧语义：非数字生命值按 0 处理；所有排序键相同时保留首个目标。
        """

        if not enemy_followers:
            return None
        return max(enemy_followers, key=_enemy_hp_key)

    @staticmethod
    def enemy_followers_in_wards(
        enemy_followers: Sequence[Sequence[Any]],
        ward_positions: Sequence[Sequence[Any]],
        *,
        x_tolerance: int = 50,
    ) -> List[Sequence[Any]]:
        """按横轴距离筛选与守护位置匹配的敌方随从。"""

        if not enemy_followers or not ward_positions:
            return []

        wards_x = []
        for w in ward_positions:
            try:
                wards_x.append(_safe_int(w[0], 0))
            except Exception:
                continue
        if not wards_x:
            return []

        out: List[Sequence[Any]] = []
        tol = max(1, int(x_tolerance))
        for f in enemy_followers:
            try:
                fx = _safe_int(f[0], 0)
            except Exception:
                continue
            if any(abs(fx - wx) < tol for wx in wards_x):
                out.append(f)
        return out

    @staticmethod
    def enemy_follower_highest_hp_in_wards(
        enemy_followers: Sequence[Sequence[Any]],
        ward_positions: Sequence[Sequence[Any]],
        *,
        x_tolerance: int = 50,
    ) -> Optional[Sequence[Any]]:
        """只在守护随从中选择生命值最高者。"""

        ward_followers = TargetSelector.enemy_followers_in_wards(
            enemy_followers,
            ward_positions,
            x_tolerance=int(x_tolerance),
        )
        if not ward_followers:
            return None
        return max(ward_followers, key=_enemy_hp_key)

    @staticmethod
    def enemy_followers_highest_hp(
        enemy_followers: Sequence[Sequence[Any]],
        *,
        n: int = 2,
        distinct_xy: bool = True,
    ) -> List[Sequence[Any]]:
        """按生命值降序选择前 N 个敌方随从。"""

        if not enemy_followers:
            return []

        ordered = sorted(enemy_followers, key=_enemy_hp_key, reverse=True)
        picked: List[Sequence[Any]] = []
        seen: set[Tuple[int, int]] = set()

        for f in ordered:
            if len(picked) >= max(0, int(n)):
                break
            if distinct_xy:
                xy = _follower_xy(f)
                if xy in seen:
                    continue
                seen.add(xy)
            picked.append(f)

        return picked

    @staticmethod
    def enemy_follower_ward_or_highest_hp(
        enemy_followers: Sequence[Sequence[Any]],
        ward_positions: Sequence[Sequence[Any]],
        *,
        x_tolerance: int = 50,
    ) -> Optional[Sequence[Any]]:
        """优先选生命值最高的守护随从，否则回退到生命值最高的普通随从。"""

        ward_pick = TargetSelector.enemy_follower_highest_hp_in_wards(
            enemy_followers,
            ward_positions,
            x_tolerance=int(x_tolerance),
        )
        if ward_pick is not None:
            return ward_pick
        return TargetSelector.enemy_follower_highest_hp(enemy_followers)

    @staticmethod
    def enemy_follower_hp_leq(
        enemy_followers: Sequence[Sequence[Any]],
        *,
        max_hp: int,
    ) -> Optional[Sequence[Any]]:
        """在生命值不高于 ``max_hp`` 的目标中选择生命值最高者。

        为保持现有筛选语义，只接受生命值为纯数字的目标。
        """

        if not enemy_followers:
            return None

        limit = _safe_int(max_hp, 0)
        candidates: List[Sequence[Any]] = []
        for f in enemy_followers:
            if len(f) > 2 and str(f[2]) == "amulet":
                continue
            if len(f) > 5 and bool(f[5]):
                continue
            try:
                hp = f[3]
            except Exception:
                continue
            if not (isinstance(hp, str) and hp.isdigit()):
                continue
            if _safe_int(hp, 0) <= limit:
                candidates.append(f)

        if not candidates:
            return None
        return max(candidates, key=_enemy_hp_key)

    @staticmethod
    def enemy_followers_hp_leq(
        enemy_followers: Sequence[Sequence[Any]],
        *,
        max_hp: int,
        n: int = 2,
    ) -> List[Sequence[Any]]:
        """在生命值不高于 ``max_hp`` 的目标中按生命值选择前 N 个。"""

        limit = _safe_int(max_hp, 0)
        candidates: List[Sequence[Any]] = []
        for f in enemy_followers:
            if len(f) > 2 and str(f[2]) == "amulet":
                continue
            if len(f) > 5 and bool(f[5]):
                continue
            try:
                hp = f[3]
            except Exception:
                continue
            if not (isinstance(hp, str) and hp.isdigit()):
                continue
            if _safe_int(hp, 0) <= limit:
                candidates.append(f)

        ordered = sorted(candidates, key=_enemy_hp_key, reverse=True)
        return ordered[: max(0, int(n))]

    @staticmethod
    def friendly_follower_by_evolve_priority(
        our_followers: Sequence[Sequence[Any]],
        *,
        exclude_names: Iterable[str] = (),
        evolve_priority_cards: Optional[Dict[str, Any]] = None,
    ) -> Optional[Sequence[Any]]:
        """按配置的进化优先级选择我方随从。

        沿用 ``EvolutionSpecialActions`` 的旧语义：排除指定名称后优先选择有名称的
        随从，按配置优先级从小到大、再按横坐标排序；没有合适命名随从时回退到
        首个未识别名称的随从。
        """

        if not our_followers:
            return None

        exclude = set(exclude_names or [])
        priority_cfg = evolve_priority_cards or {}

        exclude_norm = {normalize_config_key(str(n or "")) for n in list(exclude or [])}
        priority_norm: Dict[str, Any] = {}
        for k, v in dict(priority_cfg or {}).items():
            nk = normalize_config_key(str(k or ""))
            if not nk:
                continue
            if nk not in priority_norm:
                priority_norm[nk] = v

        def _prio(name: Any) -> int:
            if not isinstance(name, str) or not name:
                return 999
            cfg = priority_norm.get(normalize_config_key(name))
            if isinstance(cfg, dict):
                return _safe_int(cfg.get("priority", 999), 999)
            return 999

        named: List[Sequence[Any]] = []
        unnamed: List[Sequence[Any]] = []
        for f in our_followers:
            if len(f) > 2 and str(f[2]) == "amulet":
                continue
            if len(f) > 5 and bool(f[5]):
                continue
            name = None
            if len(f) > 3:
                name = f[3]
            if isinstance(name, str) and name and normalize_config_key(name) not in exclude_norm:
                named.append(f)
            elif name is None:
                unnamed.append(f)

        if named:
            return sorted(named, key=lambda f: (_prio(f[3]), _safe_int(f[0], 0)))[0]
        if unnamed:
            return unnamed[0]
        return None
