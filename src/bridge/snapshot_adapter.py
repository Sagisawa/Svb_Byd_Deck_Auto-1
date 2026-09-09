from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from src.bridge.card_name_resolver import CardNameResolver
from src.bridge.tracker_bridge import TrackerBridge, get_global_tracker_bridge
from src.game.domain.models import ObservedGameState

logger = logging.getLogger(__name__)

_warned_incomplete_snapshot = False


class SnapshotAdapter:
    """Adapts SephiesDeckLab snapshots to domain models used by Svb_Byd_Deck_Auto."""

    # Normalization scale for 1280x720 16:9 viewport
    VIEWPORT_W = 1280
    VIEWPORT_H = 720

    # 完整快照的卡牌至少会携带其中一项字段；精简诊断日志（例如
    # ShadowverseTracker v2 的 app_session.jsonl）只保留 unique_id/base_card_id，
    # 缺 cost/life/card_type 等字段，若当作可用会把默认值误当成真实数据。
    _CARD_FIELD_MARKERS = ("cost", "card_type", "life", "attack", "countdown", "has_guard")

    def __init__(self, bridge: Optional[TrackerBridge] = None, resolver: Optional[CardNameResolver] = None) -> None:
        self.bridge = bridge or get_global_tracker_bridge()
        self.resolver = resolver or CardNameResolver.get_instance()

    def get_latest_snapshot(self, force_refresh: bool = True) -> Optional[Dict[str, Any]]:
        snap = self.bridge.get_snapshot(force_refresh=force_refresh)
        if not snap or not isinstance(snap, dict):
            return None
        return snap

    @classmethod
    def _cards_have_snapshot_fields(cls, cards: Any) -> bool:
        """卡牌列表为空视为通过；非空时要求存在携带完整字段的卡牌对象。"""
        if not isinstance(cards, list) or not cards:
            return True
        for card in cards:
            if isinstance(card, dict):
                return any(key in card for key in cls._CARD_FIELD_MARKERS)
        return False

    @classmethod
    def _players_have_snapshot_fields(cls, players: List[Any]) -> bool:
        """校验我方手牌/场面与敌方场面是否为完整快照数据。

        只检查适配器实际读取的列表，避免误判对手隐藏手牌等无关字段。
        """
        ours = players[0] if isinstance(players[0], dict) else {}
        if not cls._cards_have_snapshot_fields(ours.get("hand")):
            return False
        if not cls._cards_have_snapshot_fields(ours.get("field")):
            return False
        if len(players) >= 2 and isinstance(players[1], dict):
            if not cls._cards_have_snapshot_fields(players[1].get("field")):
                return False
        return True

    def is_available(self, max_age: float = 6.0) -> bool:
        """检查内存快照是否可用且新鲜。

        若用户选择了不存在的日志路径、或者日志文件已过时超过 max_age 秒，
        则返回 False，以便脚本所有流程自动、安全地回退到传统图色识别。
        精简版日志（缺少完整卡牌字段）同样返回 False，避免费用/血量/守护等
        字段以默认值被当成真实数据使用。
        """
        if not self.bridge.is_fresh(max_age=max_age):
            return False
        snap = self.bridge.get_snapshot(force_refresh=False)
        if not snap or not isinstance(snap, dict):
            return False
        players = snap.get("root", {}).get("players")
        if not isinstance(players, list) or len(players) == 0:
            return False
        if not self._players_have_snapshot_fields(players):
            global _warned_incomplete_snapshot
            if not _warned_incomplete_snapshot:
                _warned_incomplete_snapshot = True
                logger.warning(
                    "内存快照缺少完整卡牌字段（疑似精简版 app_session.jsonl），已回退到图色识别"
                )
            return False
        return True

    # -------------------------------------------------------------------------
    # Turn, side, and leader resources
    # -------------------------------------------------------------------------
    def get_turn(self) -> Optional[int]:
        if not self.is_available():
            return None
        snap = self.get_latest_snapshot()
        if not snap:
            return None
        players = snap.get("root", {}).get("players", [])
        if players and isinstance(players[0], dict):
            return players[0].get("turn")
        return snap.get("current_turn")

    def is_second_player(self) -> Optional[bool]:
        if not self.is_available():
            return None
        snap = self.get_latest_snapshot()
        if not snap:
            return None
        players = snap.get("root", {}).get("players", [])
        if players and isinstance(players[0], dict):
            is_first = players[0].get("is_first_side")
            if is_first is not None:
                return not is_first
        return None

    def get_pp_status(self) -> Tuple[Optional[int], Optional[int]]:
        """Returns (pp_current, pp_maximum)."""
        if not self.is_available():
            return None, None
        snap = self.get_latest_snapshot()
        if not snap:
            return None, None
        players = snap.get("root", {}).get("players", [])
        if players and isinstance(players[0], dict):
            mine = players[0]
            return mine.get("pp"), mine.get("max_pp")
        return None, None

    def get_ep_status(self) -> Tuple[Optional[int], Optional[int]]:
        """Returns (ep_current, sep_current)."""
        if not self.is_available():
            return None, None
        snap = self.get_latest_snapshot()
        if not snap:
            return None, None
        players = snap.get("root", {}).get("players", [])
        if players and isinstance(players[0], dict):
            mine = players[0]
            return mine.get("evolve_points"), mine.get("super_evolve_points")
        return None, None

    def get_leader_hp(self) -> Tuple[Optional[int], Optional[int]]:
        """Returns (our_leader_hp, enemy_leader_hp)."""
        if not self.is_available():
            return None, None
        snap = self.get_latest_snapshot()
        if not snap:
            return None, None
        players = snap.get("root", {}).get("players", [])
        if len(players) >= 2:
            our_hp = players[0].get("life") if isinstance(players[0], dict) else None
            enemy_hp = players[1].get("life") if isinstance(players[1], dict) else None
            return our_hp, enemy_hp
        return None, None

    def is_mulligan_phase(self) -> bool:
        """Returns True if the current snapshot indicates mulligan phase (turn 0)."""
        if not self.is_available():
            return False
        snap = self.get_latest_snapshot()
        if not snap:
            return False
        players = snap.get("root", {}).get("players", [])
        if players and isinstance(players[0], dict):
            mine = players[0]
            turn = mine.get("turn")
            is_end = mine.get("is_end_mulligan")
            if turn == 0:
                return True
            if is_end is False:
                return True
        return False

    def get_mulligan_cards(self) -> List[Dict[str, Any]]:
        """Returns the 4 mulligan cards if currently in mulligan phase."""
        if not self.is_available():
            return []
        snap = self.get_latest_snapshot()
        if not snap:
            return []
        players = snap.get("root", {}).get("players", [])
        if not players or not isinstance(players[0], dict):
            return []
        mine = players[0]
        turn = mine.get("turn")
        is_end = mine.get("is_end_mulligan")
        hand_cards = [c for c in mine.get("hand", []) if isinstance(c, dict)]
        if len(hand_cards) == 4 and (turn == 0 or is_end is False):
            mulligan_x = [281, 483, 686, 888]
            result = []
            for idx, c in enumerate(hand_cards[:4]):
                cid = c.get("card_id", 0) or c.get("base_card_id", 0)
                name = self.resolver.get_name(cid)
                x = mulligan_x[idx] if idx < len(mulligan_x) else 500
                y = 506
                result.append({
                    "center": (x, y),
                    "cost": c.get("cost", 0),
                    "name": name,
                    "enhance_costs": c.get("enhance_costs") or [],
                    "confidence": 1.0,
                    "template_name": name,
                    "unique_id": c.get("unique_id", 0),
                    "card_id": cid,
                    "card_type": c.get("card_type", 1),
                })
            return result
        return []

    # -------------------------------------------------------------------------
    # Hand cards
    # -------------------------------------------------------------------------
    def get_hand_cards(self) -> List[Dict[str, Any]]:
        """Returns hand cards in the format expected by HandCardManager & GameActions.

        Format:
        [{
            'center': (x, y),
            'cost': int,
            'name': str,
            'enhance_costs': [int, ...],
            'confidence': 1.0,
            'template_name': str,
            'unique_id': int,
            'card_id': int,
            'card_type': int,
        }]
        """
        if not self.is_available():
            return []

        snap = self.get_latest_snapshot()
        if not snap:
            return []

        players = snap.get("root", {}).get("players", [])
        if not players or not isinstance(players[0], dict):
            return []

        hand_cards = [c for c in players[0].get("hand", []) if isinstance(c, dict)]
        total = len(hand_cards)
        turn = players[0].get("turn", 1)
        is_end = players[0].get("is_end_mulligan", True)

        result: List[Dict[str, Any]] = []

        # Turn 0 mulligan screen: 4 fixed slots centered around x=584.5, y=506
        if turn == 0 or (total == 4 and is_end is False):
            mulligan_x = [281, 483, 686, 888]
            for idx, c in enumerate(hand_cards[:4]):
                cid = c.get("card_id", 0) or c.get("base_card_id", 0)
                name = self.resolver.get_name(cid)
                x = mulligan_x[idx] if idx < len(mulligan_x) else 500
                y = 506
                base_c = c.get("cost", 0)
                result.append({
                    "center": (x, y),
                    "cost": base_c,
                    "base_cost": base_c,
                    "name": name,
                    "enhance_costs": c.get("enhance_costs") or [],
                    "crystal_costs": list(c.get("crystal_costs") or []),
                    "accelerate_costs": list(c.get("accelerate_costs") or []),
                    "confidence": 1.0,
                    "template_name": name,
                    "unique_id": c.get("unique_id", 0),
                    "card_id": cid,
                    "card_type": c.get("card_type", 1),
                })
            return result

        # In-match expanded hand cards
        # SVPositionEngine: u = 0.5375 + d * spacing_u, v = 0.8950 + 0.0020 * (d**2)
        spacing_px = max(60.0, 132.2 - (total - 6) * 14.8) if total > 1 else 0.0
        legal = snap.get("legal_actions") or snap.get("root", {}).get("legal_actions") or {}
        can_crystal_set = set(legal.get("can_crystal_play_cards") or ())
        can_accelerate_set = set(legal.get("can_accelerate_play_cards") or ())
        pp_curr = players[0].get("pp")

        for idx, c in enumerate(hand_cards):
            cid = c.get("card_id", 0) or c.get("base_card_id", 0)
            name = self.resolver.get_name(cid)
            uid = c.get("unique_id", 0)
            base_cost = c.get("cost", 0)
            crystal_costs = list(c.get("crystal_costs") or [])
            accelerate_costs = list(c.get("accelerate_costs") or [])
            enhance_costs = list(c.get("enhance_costs") or [])

            # 计算当前形态下的动态显示/打出费用（当 PP 不足出本体时，检查是否可结晶或激奏打出）
            active_cost = base_cost
            if pp_curr is not None and base_cost > pp_curr:
                if (uid in can_crystal_set or any(cc <= pp_curr for cc in crystal_costs)) and crystal_costs:
                    valid_crystals = [cc for cc in crystal_costs if cc <= pp_curr]
                    if valid_crystals:
                        active_cost = max(valid_crystals)
                elif (uid in can_accelerate_set or any(ac <= pp_curr for ac in accelerate_costs)) and accelerate_costs:
                    valid_accels = [ac for ac in accelerate_costs if ac <= pp_curr]
                    if valid_accels:
                        active_cost = max(valid_accels)

            if total <= 1:
                cx = int(0.5375 * self.VIEWPORT_W)
                cy = int(0.8950 * self.VIEWPORT_H)
            else:
                d = idx - (total - 1) / 2.0
                cx = int((0.5375 + d * (spacing_px / 1280.0)) * self.VIEWPORT_W)
                cy = int((0.8950 + 0.0020 * (d ** 2)) * self.VIEWPORT_H)

            result.append({
                "center": (cx, cy),
                "cost": active_cost,
                "base_cost": base_cost,
                "name": name,
                "enhance_costs": enhance_costs,
                "crystal_costs": crystal_costs,
                "accelerate_costs": accelerate_costs,
                "can_crystal": uid in can_crystal_set,
                "can_accelerate": uid in can_accelerate_set,
                "confidence": 1.0,
                "template_name": name,
                "unique_id": uid,
                "card_id": cid,
                "card_type": c.get("card_type", 1),
            })
        return result

    # -------------------------------------------------------------------------
    # Field followers (Ours & Enemy)
    # -------------------------------------------------------------------------
    def get_our_followers(self) -> Optional[List[Tuple[int, int, str, Optional[str]]]]:
        """Returns our followers in (x, y, follower_type, follower_name) format.

        若内存不可用返回 None（促使调用方回退视觉识别）；若可用且场上无随从返回 []。
        """
        if not self.is_available():
            return None

        snap = self.get_latest_snapshot()
        if not snap:
            return None

        players = snap.get("root", {}).get("players", [])
        if not players or not isinstance(players[0], dict):
            return None

        mine = players[0]
        field_cards = [c for c in mine.get("field", []) if isinstance(c, dict)]
        total = min(max(len(field_cards), 1), 5) if field_cards else 0
        if total == 0:
            return []

        legal_actions = snap.get("legal_actions") or {}
        attack_leader_cards = set(legal_actions.get("can_attack_leader_cards") or [])
        attack_field_cards = set(legal_actions.get("can_attack_field_cards") or [])
        attacked_cards = set(legal_actions.get("attacked_cards") or [])

        result: List[Tuple[int, int, str, Optional[str]]] = []
        for idx, card in enumerate(field_cards):
            d = idx - (total - 1) / 2.0
            u = 0.4984 + d * 0.1172
            v = 0.5417
            x = int(u * self.VIEWPORT_W)
            y = int(v * self.VIEWPORT_H)

            uid = card.get("unique_id", 0)
            cid = card.get("card_id", 0)
            base_cid = card.get("base_card_id", 0)
            name = self.resolver.get_name(cid) or (self.resolver.get_name(base_cid) if base_cid else "")

            card_type = int(card.get("card_type", 1) or 1)
            countdown = int(card.get("countdown", 0) or 0)
            is_amulet = (card_type in (2, 3)) or (countdown > 0)

            # Determine follower_type ("green" | "yellow" | "amulet" | "normal")
            if is_amulet:
                ftype = "amulet"
            elif uid in attacked_cards:
                ftype = "normal"
            elif uid in attack_leader_cards:
                ftype = "green"  # Storm / can attack face
            elif uid in attack_field_cards:
                ftype = "yellow"  # Rush / can attack followers only
            else:
                ftype = "normal"

            result.append((x, y, ftype, name))
        return result

    def get_our_field_details(self) -> List[Dict[str, Any]]:
        """Returns rich details of our on-field followers and amulets."""
        snap = self.get_latest_snapshot()
        if not snap:
            return []

        players = snap.get("root", {}).get("players", [])
        if not players or not isinstance(players[0], dict):
            return []

        mine = players[0]
        field_cards = [c for c in mine.get("field", []) if isinstance(c, dict)]
        total = min(max(len(field_cards), 1), 5) if field_cards else 0
        if total == 0:
            return []

        legal_actions = snap.get("legal_actions") or {}
        attack_leader_cards = set(legal_actions.get("can_attack_leader_cards") or [])
        attack_field_cards = set(legal_actions.get("can_attack_field_cards") or [])
        attacked_cards = set(legal_actions.get("attacked_cards") or [])

        result: List[Dict[str, Any]] = []
        for idx, card in enumerate(field_cards):
            d = idx - (total - 1) / 2.0
            u = 0.4984 + d * 0.1172
            v = 0.5417
            x = int(u * self.VIEWPORT_W)
            y = int(v * self.VIEWPORT_H)

            uid = card.get("unique_id", 0)
            cid = card.get("card_id", 0)
            base_cid = card.get("base_card_id", 0)
            name = self.resolver.get_name(cid) or (self.resolver.get_name(base_cid) if base_cid else "")

            card_type = int(card.get("card_type", 1) or 1)
            countdown = int(card.get("countdown", 0) or 0)
            is_amulet = (card_type in (2, 3)) or (countdown > 0)

            result.append({
                "x": x,
                "y": y,
                "unique_id": uid,
                "card_id": cid,
                "name": name,
                "card_type": card_type,
                "is_amulet": is_amulet,
                "countdown": countdown,
                "hp": int(card.get("life", 1) or 1),
                "attack": int(card.get("attack", 0) or 0),
                "can_attack_leader": uid in attack_leader_cards,
                "can_attack_field": uid in attack_field_cards,
                "attacked": uid in attacked_cards,
            })
        return result

    def get_our_amulets(self) -> List[Dict[str, Any]]:
        """Returns all friendly amulets currently on field."""
        return [unit for unit in self.get_our_field_details() if unit.get("is_amulet")]

    def get_enemy_field_details(self) -> List[Dict[str, Any]]:
        """Returns rich details of enemy on-field followers and amulets with position."""
        snap = self.get_latest_snapshot()
        if not snap:
            return []

        players = snap.get("root", {}).get("players", [])
        if len(players) < 2 or not isinstance(players[1], dict):
            return []

        enemy = players[1]
        field_cards = [c for c in enemy.get("field", []) if isinstance(c, dict)]
        field_cards = list(reversed(field_cards))
        total = len(field_cards)
        if total == 0:
            return []

        result: List[Dict[str, Any]] = []
        for idx, card in enumerate(field_cards):
            if total <= 1:
                u = 0.4984
            else:
                d = idx - (total - 1) / 2.0
                u = 0.4984 + d * 0.1156
            v = 0.3000
            x = int(u * self.VIEWPORT_W)
            y = int(v * self.VIEWPORT_H)

            cid = card.get("card_id", 0)
            base_cid = card.get("base_card_id", 0)
            name = self.resolver.get_name(cid) or (self.resolver.get_name(base_cid) if base_cid else "")

            card_type = int(card.get("card_type", 1) or 1)
            countdown = int(card.get("countdown", 0) or 0)
            is_amulet = (card_type in (2, 3)) or (countdown > 0)
            has_guard = bool(card.get("has_guard", False))
            has_cant_select = bool(card.get("has_cant_select", False))
            has_cant_be_attacked = bool(card.get("has_cant_be_attacked", False))

            result.append({
                "x": x,
                "y": y,
                "unique_id": card.get("unique_id", 0),
                "card_id": cid,
                "name": name,
                "card_type": card_type,
                "is_amulet": is_amulet,
                "countdown": countdown,
                "hp": int(card.get("life", 1) or 1),
                "attack": int(card.get("attack", 0) or 0),
                "has_guard": has_guard,
                "has_cant_select": has_cant_select,
                "has_cant_be_attacked": has_cant_be_attacked,
            })
        return result

    def get_enemy_amulets(self) -> List[Dict[str, Any]]:
        """Returns all enemy amulets currently on field."""
        return [unit for unit in self.get_enemy_field_details() if unit.get("is_amulet")]

    def get_enemy_followers(self, include_amulets: bool = False) -> Optional[List[Tuple[Any, ...]]]:
        """Returns enemy followers in (x, y, ftype, hp, name, is_amulet, countdown, attack, has_cant_select, has_guard) format.

        若内存不可用返回 None（促使调用方回退视觉识别）；若可用且场上无敌方随从返回 []。
        若 include_amulets=False（默认），严格过滤排除护符，只返回真正的敌方随从。
        若 include_amulets=True，返回场上全部敌方单位（随从及护符，用于预览UI等）。
        """
        if not self.is_available():
            return None

        units = self.get_enemy_field_details()
        result: List[Tuple[Any, ...]] = []
        for u in units:
            if not include_amulets and u.get("is_amulet"):
                continue
            ftype = "amulet" if u.get("is_amulet") else "normal"
            result.append((
                u["x"],
                u["y"],
                ftype,
                str(u["hp"]),
                u["name"],
                u["is_amulet"],
                u["countdown"],
                u["attack"],
                u["has_cant_select"],
                u["has_guard"],
            ))
        return result

    def get_shield_targets(self) -> Optional[List[Tuple[int, int]]]:
        """Returns coordinates of enemy followers with Ward (has_guard == True).

        若内存不可用返回 None（促使调用方回退视觉识别）；若可用且场上无守护随从返回 []。
        """
        if not self.is_available():
            return None

        snap = self.get_latest_snapshot()
        if not snap:
            return None

        players = snap.get("root", {}).get("players", [])
        if len(players) < 2 or not isinstance(players[1], dict):
            return None

        enemy = players[1]
        field_cards = [c for c in enemy.get("field", []) if isinstance(c, dict)]
        field_cards = list(reversed(field_cards))
        total = len(field_cards)
        if total == 0:
            return []

        result: List[Tuple[int, int]] = []
        for idx, card in enumerate(field_cards):
            if card.get("has_guard"):
                d = idx - (total - 1) / 2.0
                u = 0.4984 + d * 0.1156
                v = 0.3000
                x = int(u * self.VIEWPORT_W)
                y = int(v * self.VIEWPORT_H)
                result.append((x, y))
        return result

    # -------------------------------------------------------------------------
    # Domain ObservedGameState
    # -------------------------------------------------------------------------
    def build_observed_game_state(self, note: str = "memory_bridge") -> Optional[ObservedGameState]:
        if not self.is_available():
            return None

        turn = self.get_turn()
        is_second = self.is_second_player()
        pp_curr, pp_max = self.get_pp_status()
        ep_curr, sep_curr = self.get_ep_status()

        return ObservedGameState(
            turn=turn,
            is_second_player=is_second,
            pp_available=pp_curr,
            ep=ep_curr,
            sep=sep_curr,
            hand=self.get_hand_cards(),
            board_ours=self.get_our_followers(),
            board_enemy=self.get_enemy_followers(),
            ward_enemy=self.get_shield_targets(),
            ui={},
            note=note,
        )


def is_bridge_mode_active(device_state: Any = None) -> bool:
    """检查桥接/内存直读模式当前是否可用活跃。"""
    try:
        if device_state is not None:
            if hasattr(device_state, "_bridge_active"):
                return bool(getattr(device_state, "_bridge_active"))
            adapter = getattr(device_state, "snapshot_adapter", None)
            if adapter and hasattr(adapter, "is_available"):
                return bool(adapter.is_available())
            bridge = getattr(device_state, "tracker_bridge", None)
            if bridge and hasattr(bridge, "is_fresh"):
                return bool(bridge.is_fresh())
            cfg = getattr(device_state, "config", None)
            if isinstance(cfg, dict):
                mem_cfg = cfg.get("memory_reader")
                if isinstance(mem_cfg, dict) and mem_cfg.get("enabled") is False:
                    return False
        from src.bridge.helper import get_memory_adapter
        adapter = get_memory_adapter()
        return bool(adapter and adapter.is_available())
    except Exception:
        return False

