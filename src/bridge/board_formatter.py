from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.bridge.card_name_resolver import CardNameResolver

logger = logging.getLogger(__name__)

CLASS_NAMES: Dict[int, str] = {
    1: "精灵",
    2: "皇家",
    3: "巫师",
    4: "龙族",
    5: "梦魇",
    6: "主教",
    7: "复仇者",
}


@dataclass
class FormattedUnit:
    index: int
    name: str
    cost: int
    attack: Optional[int] = None
    life: Optional[int] = None
    is_amulet: bool = False
    countdown: Optional[int] = None
    evolve_state: int = 0
    keywords: List[str] = field(default_factory=list)
    can_attack_leader: bool = False
    can_attack_field: bool = False
    has_attacked: bool = False
    is_opponent: bool = False

    def to_display_string(self) -> str:
        idx_prefix = f"[敌{self.index}]" if self.is_opponent else f"[{self.index}]"
        cost_str = f"{self.cost}费" if self.cost is not None else "?费"
        kw_str = " " + "".join(f"【{kw}】" for kw in self.keywords) if self.keywords else ""

        if self.is_amulet:
            cd_str = f"倒数={self.countdown}" if self.countdown is not None and self.countdown >= 0 else "护符"
            return f"{idx_prefix} {cost_str} {self.name}  [护符 · {cd_str}]{kw_str}"
        else:
            atk = self.attack if self.attack is not None else "?"
            hp = self.life if self.life is not None else "?"
            evo_str = f"  进化={self.evolve_state}" if self.evolve_state else ""
            return f"{idx_prefix} {cost_str} {self.name}  {atk}/{hp}{evo_str}{kw_str}"


@dataclass
class FormattedBoardState:
    is_in_match: bool = False
    turn: Optional[int] = None
    is_mulligan: bool = False
    our_hp: Optional[int] = None
    our_max_hp: Optional[int] = None
    our_pp: Optional[int] = None
    our_max_pp: Optional[int] = None
    our_ep: Optional[int] = None
    our_sep: Optional[int] = None
    enemy_hp: Optional[int] = None
    enemy_max_hp: Optional[int] = None
    enemy_class_name: str = "未知"
    enemy_hand_count: int = 0
    our_followers: List[FormattedUnit] = field(default_factory=list)
    enemy_followers: List[FormattedUnit] = field(default_factory=list)
    our_hand_count: int = 0


_CARD_RULES_CACHE: Optional[Dict[str, Any]] = None


def _load_card_rules() -> Dict[str, Any]:
    global _CARD_RULES_CACHE
    if _CARD_RULES_CACHE is not None:
        return _CARD_RULES_CACHE

    candidates = [
        Path(__file__).resolve().parent.parent / "tracker" / "data" / "card_rules.json",
        Path(__file__).resolve().parent.parent.parent / "src" / "tracker" / "data" / "card_rules.json",
    ]
    for c in candidates:
        if c.is_file():
            try:
                _CARD_RULES_CACHE = json.loads(c.read_text(encoding="utf-8"))
                return _CARD_RULES_CACHE
            except Exception:
                pass
    _CARD_RULES_CACHE = {}
    return _CARD_RULES_CACHE


def _extract_static_keywords(card_id: int) -> Set[str]:
    if card_id <= 0:
        return set()
    # Evolved and token cards canonical id
    base_id = card_id
    if card_id > 100000000:
        base_id = (card_id // 10) * 10
    rules = _load_card_rules().get(str(base_id), {})
    if not isinstance(rules, dict):
        return set()
    static = rules.get("static", {})
    if not isinstance(static, dict):
        return set()
    kw: Set[str] = set()
    if static.get("has_storm"):
        kw.add("疾驰")
    if static.get("has_rush"):
        kw.add("突进")
    return kw


def format_board_state(snapshot: Optional[Dict[str, Any]]) -> FormattedBoardState:
    """Format full battle-state snapshot into clean, UI-ready domain objects."""
    if not snapshot or not isinstance(snapshot, dict):
        return FormattedBoardState(is_in_match=False)

    players = snapshot.get("root", {}).get("players")
    if not isinstance(players, (list, tuple)) or len(players) == 0:
        return FormattedBoardState(is_in_match=False)

    mine = players[0] if isinstance(players[0], dict) else {}
    opponent = players[1] if len(players) > 1 and isinstance(players[1], dict) else {}

    turn = mine.get("turn")
    is_mulligan = (turn == 0) or (mine.get("is_end_mulligan") is False)

    enemy_class_id = opponent.get("class_id") or snapshot.get("opponent_class_id", 0)
    enemy_class_name = CLASS_NAMES.get(int(enemy_class_id or 0), "未知")

    resolver = CardNameResolver.get_instance()

    legal_actions = snapshot.get("legal_actions") or {}
    attack_leader_cards = set(legal_actions.get("can_attack_leader_cards") or [])
    attack_field_cards = set(legal_actions.get("can_attack_field_cards") or [])
    attacked_cards = set(legal_actions.get("attacked_cards") or [])

    keyword_flags = (
        ("has_guard", "守护"),
        ("has_last_word", "谢幕曲"),
        ("has_sneak", "潜行"),
        ("has_cant_be_attacked", "无法被攻击"),
        ("has_cant_select", "无法被选中为目标"),
        ("has_killer", "必杀"),
        ("has_bane", "必杀"),
        ("has_drain", "虹吸"),
        ("has_cant_attack", "无法攻击"),
    )
    aliases = {
        "storm": "疾驰",
        "rush": "突进",
        "ward": "守护",
        "bane": "必杀",
        "ambush": "潜行",
        "last_words": "谢幕曲",
    }

    def _parse_side(raw_cards: Any, is_opponent: bool) -> List[FormattedUnit]:
        if not isinstance(raw_cards, (list, tuple)):
            return []
        cards = [c for c in raw_cards if isinstance(c, dict)]
        if is_opponent:
            cards = list(reversed(cards))

        units: List[FormattedUnit] = []
        for idx, c in enumerate(cards, start=1):
            cid = c.get("card_id", 0)
            base_cid = c.get("base_card_id", 0)
            name = resolver.get_name(cid) or (resolver.get_name(base_cid) if base_cid else "")
            if not name:
                name = f"未知卡牌({cid})"

            cost = c.get("cost", 0)
            card_type = int(c.get("card_type", 1) or 1)
            countdown = c.get("countdown")
            is_amulet = (card_type in (2, 3)) or (countdown is not None and int(countdown) > 0)

            atk = c.get("attack")
            hp = c.get("life")
            evo = int(c.get("evolve_state", 0) or 0)

            uid = c.get("unique_id", 0)
            can_atk_ldr = (uid in attack_leader_cards) or bool(c.get("can_attack_leader"))
            can_atk_fld = (uid in attack_field_cards) or bool(c.get("can_attack_field"))
            has_atk = (uid in attacked_cards) or bool(c.get("has_attacked"))

            keywords = _extract_static_keywords(cid or base_cid)
            for flag, label in keyword_flags:
                if c.get(flag):
                    keywords.add(label)

            buff = c.get("buff")
            if isinstance(buff, dict):
                if buff.get("quick"):
                    keywords.add("疾驰")
                if buff.get("rush"):
                    keywords.add("突进")

            statuses = c.get("statuses", c.get("keywords"))
            if isinstance(statuses, (list, tuple, set)):
                keywords.update(aliases.get(str(item).casefold(), str(item)) for item in statuses)

            # 攻击权限完全由游戏底层实时驱动：
            if can_atk_ldr:
                keywords.add("疾驰")
                keywords.discard("突进")
            elif can_atk_fld:
                keywords.add("突进")
                keywords.discard("疾驰")
            else:
                keywords.discard("疾驰")
                keywords.discard("突进")

            sorted_kw = sorted(list(keywords))

            units.append(
                FormattedUnit(
                    index=idx,
                    name=name,
                    cost=cost,
                    attack=atk,
                    life=hp,
                    is_amulet=is_amulet,
                    countdown=countdown,
                    evolve_state=evo,
                    keywords=sorted_kw,
                    can_attack_leader=can_atk_ldr,
                    can_attack_field=can_atk_fld,
                    has_attacked=has_atk,
                    is_opponent=is_opponent,
                )
            )
        return units

    our_followers = _parse_side(mine.get("field", []), is_opponent=False)
    enemy_followers = _parse_side(opponent.get("field", []), is_opponent=True)

    our_hand = mine.get("hand", [])
    enemy_hand = opponent.get("hand", [])

    return FormattedBoardState(
        is_in_match=True,
        turn=turn,
        is_mulligan=is_mulligan,
        our_hp=mine.get("life"),
        our_max_hp=mine.get("max_life", 20),
        our_pp=mine.get("pp"),
        our_max_pp=mine.get("max_pp"),
        our_ep=mine.get("evolve_points"),
        our_sep=mine.get("super_evolve_points"),
        enemy_hp=opponent.get("life"),
        enemy_max_hp=opponent.get("max_life", 20),
        enemy_class_name=enemy_class_name,
        enemy_hand_count=len(enemy_hand) if isinstance(enemy_hand, (list, tuple)) else 0,
        our_followers=our_followers,
        enemy_followers=enemy_followers,
        our_hand_count=len(our_hand) if isinstance(our_hand, (list, tuple)) else 0,
    )
