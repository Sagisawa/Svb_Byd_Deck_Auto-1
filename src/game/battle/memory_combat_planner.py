"""Memory-based Combat & Damage Optimizer Planner.

Jointly optimizes evolution decisions and attack sequences to maximize face damage
to the enemy leader within a single turn, adhering to Shadowverse combat rules:
- Wards must be destroyed before enemy leader can be attacked.
- Freshly summoned followers without Storm cannot attack face; evolving them grants Rush.
- Followers with Storm or that persisted from previous turn can attack face; evolving them adds +2/+2 (normal) or +3/+3 (super) directly to face damage.
- Handles Bane (Killer), Divine Shield (Temp Shield), retaliatory combat damage, and fallback board clearing when wards cannot be cleared.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CombatAction:
    """Represents a single atomic action in the combat plan."""

    action_type: str  # "evolve_normal" | "evolve_super" | "attack_leader" | "attack_follower"
    source_uid: int
    source_name: str
    source_pos: Tuple[int, int]
    target_uid: Optional[int] = None
    target_name: Optional[str] = None
    target_pos: Optional[Tuple[int, int]] = None
    damage_dealt: int = 0
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CombatPlan:
    """Complete plan of evolution and attacks for the turn."""

    actions: List[CombatAction]
    total_face_damage: int
    is_lethal: bool
    enemy_leader_remaining_hp: int
    wards_cleared: bool
    evolved_uid: Optional[int] = None
    evolve_type: Optional[str] = None  # None | "normal" | "super"
    score: float = 0.0
    notes: str = ""


@dataclass
class SimUnit:
    """Represents a combat unit during search simulation."""

    uid: int
    name: str
    x: int
    y: int
    atk: int
    hp: int
    max_hp: int = 1
    is_amulet: bool = False
    can_attack_leader: bool = False
    can_attack_field: bool = False
    can_face_inherent: bool = False
    has_storm: bool = False
    has_rush: bool = False
    has_attacked: bool = False
    attacks_left: int = 1
    has_guard: bool = False  # Ward
    has_killer: bool = False  # Bane
    has_temp_shield: bool = False  # Divine Shield
    has_cant_be_attacked: bool = False
    has_cant_attack: bool = False
    has_sneak: bool = False  # Ambush / Stealth
    evolve_state: int = 0  # 0: none, 1: normal, 2: super
    can_evolve: bool = False
    can_super_evolve: bool = False

    def clone(self) -> SimUnit:
        return SimUnit(
            uid=self.uid,
            name=self.name,
            x=self.x,
            y=self.y,
            atk=self.atk,
            hp=self.hp,
            max_hp=self.max_hp,
            is_amulet=self.is_amulet,
            can_attack_leader=self.can_attack_leader,
            can_attack_field=self.can_attack_field,
            can_face_inherent=self.can_face_inherent,
            has_storm=self.has_storm,
            has_rush=self.has_rush,
            has_attacked=self.has_attacked,
            attacks_left=self.attacks_left,
            has_guard=self.has_guard,
            has_killer=self.has_killer,
            has_temp_shield=self.has_temp_shield,
            has_cant_be_attacked=self.has_cant_be_attacked,
            has_cant_attack=self.has_cant_attack,
            has_sneak=self.has_sneak,
            evolve_state=self.evolve_state,
            can_evolve=self.can_evolve,
            can_super_evolve=self.can_super_evolve,
        )


class MemoryCombatPlanner:
    """Joint solver for evolution and attack sequence to maximize damage."""

    DEFAULT_FACE_POS = (646, 64)

    @classmethod
    def plan(
        cls,
        combat_snapshot: Dict[str, Any],
        *,
        allow_evolve: bool = True,
        max_search_states: int = 10000,
    ) -> CombatPlan:
        """Computes the optimal combat plan given a snapshot from SnapshotAdapter."""
        our_units_raw = combat_snapshot.get("our_followers") or []
        enemy_units_raw = combat_snapshot.get("enemy_followers") or []
        enemy_leader_info = combat_snapshot.get("enemy_leader") or {}
        enemy_leader_hp = int(enemy_leader_info.get("hp", 20) or 20)
        face_x = int(enemy_leader_info.get("x", cls.DEFAULT_FACE_POS[0]))
        face_y = int(enemy_leader_info.get("y", cls.DEFAULT_FACE_POS[1]))
        face_pos = (face_x, face_y)

        evo_info = combat_snapshot.get("evolution_info") or {}
        can_normal_evo_global = bool(evo_info.get("can_evolve", False))
        can_super_evo_global = bool(evo_info.get("can_super_evolve", False))

        # Parse units
        our_units: List[SimUnit] = []
        for u in our_units_raw:
            if u.get("is_amulet"):
                continue

            can_face_inherent = bool(
                u.get("can_face_inherent", u.get("can_attack_leader", False))
            )
            has_storm = bool(u.get("has_storm", False))
            has_rush = bool(u.get("has_rush", False))
            has_atk = bool(u.get("has_attacked", False))

            if has_storm:
                can_face_inherent = True

            our_units.append(
                SimUnit(
                    uid=int(u.get("unique_id", 0)),
                    name=str(u.get("name", "")),
                    x=int(u.get("x", 0)),
                    y=int(u.get("y", 0)),
                    atk=int(u.get("atk", 0)),
                    hp=int(u.get("hp", 1)),
                    max_hp=int(u.get("max_hp", u.get("hp", 1))),
                    is_amulet=False,
                    can_attack_leader=can_face_inherent,
                    can_attack_field=bool(u.get("can_attack_field", False)) or can_face_inherent,
                    can_face_inherent=can_face_inherent,
                    has_storm=has_storm,
                    has_rush=has_rush,
                    has_attacked=has_atk,
                    attacks_left=int(u.get("attacks_left", 0 if has_atk else 1)),
                    has_guard=bool(u.get("has_guard", False)),
                    has_killer=bool(u.get("has_killer", False)),
                    has_temp_shield=bool(u.get("has_temp_shield", False)),
                    has_cant_be_attacked=bool(u.get("has_cant_be_attacked", False)),
                    has_cant_attack=bool(u.get("has_cant_attack", False)),
                    has_sneak=bool(u.get("has_sneak", False)),
                    evolve_state=int(u.get("evolve_state", 0)),
                    can_evolve=bool(u.get("can_evolve", False)) and can_normal_evo_global,
                    can_super_evolve=bool(u.get("can_super_evolve", False)) and can_super_evo_global,
                )
            )

        enemy_units: List[SimUnit] = []
        for u in enemy_units_raw:
            if u.get("is_amulet"):
                continue
            enemy_units.append(
                SimUnit(
                    uid=int(u.get("unique_id", 0)),
                    name=str(u.get("name", "")),
                    x=int(u.get("x", 0)),
                    y=int(u.get("y", 0)),
                    atk=int(u.get("atk", 0)),
                    hp=int(u.get("hp", 1)),
                    max_hp=int(u.get("hp", 1)),
                    is_amulet=False,
                    can_attack_leader=False,
                    can_attack_field=False,
                    can_face_inherent=False,
                    has_storm=False,
                    has_rush=False,
                    has_attacked=False,
                    attacks_left=0,
                    has_guard=bool(u.get("has_guard", False)),
                    has_killer=bool(u.get("has_killer", False)),
                    has_temp_shield=bool(u.get("has_temp_shield", False)),
                    has_cant_be_attacked=bool(u.get("has_cant_be_attacked", False)),
                    has_cant_attack=False,
                    has_sneak=bool(u.get("has_sneak", False)),
                )
            )

        if not our_units:
            return CombatPlan(
                actions=[],
                total_face_damage=0,
                is_lethal=(enemy_leader_hp <= 0),
                enemy_leader_remaining_hp=enemy_leader_hp,
                wards_cleared=cls._are_wards_cleared(enemy_units),
                notes="No friendly followers available",
            )

        # Generate candidate evolution branches:
        # Candidate format: (target_uid, evo_type) where evo_type is None | "normal" | "super"
        evo_candidates: List[Tuple[Optional[int], Optional[str]]] = [(None, None)]

        if allow_evolve:
            for u in our_units:
                if u.can_super_evolve:
                    evo_candidates.append((u.uid, "super"))
                if u.can_evolve:
                    evo_candidates.append((u.uid, "normal"))

        best_plan: Optional[CombatPlan] = None
        best_score = -float("inf")

        for evo_target_uid, evo_type in evo_candidates:
            # Clone state for simulation
            sim_ours = [u.clone() for u in our_units]
            sim_enemy = [u.clone() for u in enemy_units]
            evo_action: Optional[CombatAction] = None

            if evo_target_uid is not None and evo_type is not None:
                target_u = next((u for u in sim_ours if u.uid == evo_target_uid), None)
                if target_u is None:
                    continue

                bonus = 3 if evo_type == "super" else 2
                target_u.atk += bonus
                target_u.hp += bonus
                target_u.max_hp += bonus
                target_u.evolve_state = 2 if evo_type == "super" else 1

                # If follower already had face attack capability (storm or previous turn),
                # it keeps it and gains +bonus directly to face damage!
                # If fresh without storm, it gains Rush (attacks followers/wards, NOT leader).
                if target_u.can_face_inherent:
                    target_u.can_face_inherent = True
                    target_u.can_attack_leader = True
                    target_u.can_attack_field = True
                    if not target_u.has_attacked:
                        target_u.attacks_left = max(1, target_u.attacks_left)
                else:
                    target_u.can_attack_field = True
                    target_u.can_attack_leader = False
                    target_u.can_face_inherent = False
                    if not target_u.has_attacked:
                        target_u.attacks_left = max(1, target_u.attacks_left)

                evo_action = CombatAction(
                    action_type="evolve_" + evo_type,
                    source_uid=target_u.uid,
                    source_name=target_u.name,
                    source_pos=(target_u.x, target_u.y),
                    details={"bonus": bonus, "mode": evo_type},
                )

            # Solve attack sequence for this state
            actions, face_dmg, final_leader_hp, wards_cleared, final_ours, final_enemy = (
                cls._solve_attacks(
                    sim_ours,
                    sim_enemy,
                    enemy_leader_hp=enemy_leader_hp,
                    face_pos=face_pos,
                    max_states=max_search_states,
                )
            )

            # Check if evolution was actually utilized or beneficial
            # If an evolution occurred, prepend evo_action to actions
            all_actions = list(actions)
            if evo_action is not None:
                # Place evolution before that follower's first attack, or at the start
                all_actions.insert(0, evo_action)

            # Calculate score
            score = cls._evaluate_plan(
                face_damage=face_dmg,
                initial_leader_hp=enemy_leader_hp,
                final_leader_hp=final_leader_hp,
                wards_cleared=wards_cleared,
                evo_type=evo_type,
                evo_utilized=(evo_action is not None and any(a.source_uid == evo_target_uid for a in actions)),
                surviving_ours=final_ours,
                surviving_enemy=final_enemy,
            )

            plan = CombatPlan(
                actions=all_actions,
                total_face_damage=face_dmg,
                is_lethal=(final_leader_hp <= 0),
                enemy_leader_remaining_hp=final_leader_hp,
                wards_cleared=wards_cleared,
                evolved_uid=evo_target_uid,
                evolve_type=evo_type,
                score=score,
            )

            if score > best_score or best_plan is None:
                best_score = score
                best_plan = plan

        return best_plan or CombatPlan(
            actions=[],
            total_face_damage=0,
            is_lethal=False,
            enemy_leader_remaining_hp=enemy_leader_hp,
            wards_cleared=False,
        )

    @classmethod
    def _are_wards_cleared(cls, enemy_units: List[SimUnit]) -> bool:
        """Returns True if no alive enemy units with Ward exist."""
        return not any(
            u.has_guard and u.hp > 0 and not u.is_amulet and not u.has_cant_be_attacked and not u.has_sneak
            for u in enemy_units
        )

    @classmethod
    def _solve_attacks(
        cls,
        ours: List[SimUnit],
        enemy: List[SimUnit],
        *,
        enemy_leader_hp: int,
        face_pos: Tuple[int, int],
        max_states: int,
    ) -> Tuple[List[CombatAction], int, int, bool, List[SimUnit], List[SimUnit]]:
        """DFS / Branch & Bound search for the best attack sequence."""
        best_actions: List[CombatAction] = []
        best_face_dmg = -1
        best_final_leader_hp = enemy_leader_hp
        best_wards_cleared = False
        best_surviving_ours = ours
        best_surviving_enemy = enemy
        best_eval_score = -float("inf")

        states_explored = 0

        def dfs(
            current_ours: List[SimUnit],
            current_enemy: List[SimUnit],
            current_leader_hp: int,
            current_actions: List[CombatAction],
            current_face_dmg: int,
        ):
            nonlocal best_actions, best_face_dmg, best_final_leader_hp, best_wards_cleared
            nonlocal best_surviving_ours, best_surviving_enemy, best_eval_score, states_explored

            states_explored += 1
            if states_explored > max_states:
                return

            active_wards = [
                e for e in current_enemy
                if e.has_guard and e.hp > 0 and not e.is_amulet and not e.has_cant_be_attacked and not e.has_sneak
            ]
            wards_cleared = (len(active_wards) == 0)

            # Available attackers
            avail_attackers = [
                u for u in current_ours
                if u.hp > 0 and u.attacks_left > 0 and not u.has_cant_attack and (u.can_attack_leader or u.can_attack_field or u.can_face_inherent)
            ]

            # Evaluate current state as a potential baseline/stop point
            current_eval = cls._evaluate_attack_state(
                face_damage=current_face_dmg,
                leader_remaining_hp=current_leader_hp,
                wards_cleared=wards_cleared,
                surviving_ours=current_ours,
                surviving_enemy=current_enemy,
            )
            if current_eval > best_eval_score:
                best_eval_score = current_eval
                best_actions = list(current_actions)
                best_face_dmg = current_face_dmg
                best_final_leader_hp = current_leader_hp
                best_wards_cleared = wards_cleared
                best_surviving_ours = [u.clone() for u in current_ours]
                best_surviving_enemy = [e.clone() for e in current_enemy]

            # Pruning 1: Greedy completion when NO wards exist
            if wards_cleared:
                # If no wards exist, all attackers with can_face_inherent should directly hit face!
                # Remaining rush-only attackers can trade with remaining enemy followers or stop.
                sim_actions = list(current_actions)
                sim_ours = [u.clone() for u in current_ours]
                sim_enemy = [e.clone() for e in current_enemy]
                sim_leader_hp = current_leader_hp
                sim_face_dmg = current_face_dmg

                # 1. Face attackers hit face
                face_attackers = [
                    u for u in sim_ours
                    if u.hp > 0 and u.attacks_left > 0 and not u.has_cant_attack and u.can_face_inherent
                ]
                # Order face attackers from highest attack to lowest
                face_attackers.sort(key=lambda u: u.atk, reverse=True)

                for fa in face_attackers:
                    while fa.attacks_left > 0:
                        dmg = max(0, fa.atk)
                        sim_leader_hp -= dmg
                        sim_face_dmg += dmg
                        fa.attacks_left -= 1
                        fa.has_attacked = True
                        sim_actions.append(
                            CombatAction(
                                action_type="attack_leader",
                                source_uid=fa.uid,
                                source_name=fa.name,
                                source_pos=(fa.x, fa.y),
                                target_pos=face_pos,
                                damage_dealt=dmg,
                                details={"target": "leader"},
                            )
                        )

                # 2. Rush-only attackers (can't hit face) trade with alive enemy followers if beneficial
                rush_attackers = [
                    u for u in sim_ours
                    if u.hp > 0 and u.attacks_left > 0 and not u.has_cant_attack and u.can_attack_field and not u.can_face_inherent
                ]

                for ra in rush_attackers:
                    while ra.attacks_left > 0 and ra.hp > 0:
                        alive_enemies = [
                            e for e in sim_enemy
                            if e.hp > 0 and not e.is_amulet and not e.has_cant_be_attacked and not e.has_sneak
                        ]
                        if not alive_enemies:
                            break
                        # Pick enemy: prefer killable high-ATK enemy, or lowest HP enemy
                        target_e = cls._pick_best_rush_trade_target(ra, alive_enemies)
                        if not target_e:
                            break

                        # Execute combat
                        combat_act = cls._simulate_combat(ra, target_e)
                        sim_actions.append(combat_act)

                # Evaluate state
                eval_score = cls._evaluate_attack_state(
                    face_damage=sim_face_dmg,
                    leader_remaining_hp=sim_leader_hp,
                    wards_cleared=True,
                    surviving_ours=sim_ours,
                    surviving_enemy=sim_enemy,
                )

                if eval_score > best_eval_score:
                    best_eval_score = eval_score
                    best_actions = sim_actions
                    best_face_dmg = sim_face_dmg
                    best_final_leader_hp = sim_leader_hp
                    best_wards_cleared = True
                    best_surviving_ours = sim_ours
                    best_surviving_enemy = sim_enemy

                return

            # Wards exist! Can any friendly attacker hit?
            if not avail_attackers:
                return

            # Upper Bound Pruning:
            # Maximum potential face damage from this branch:
            # sum of ATK of all remaining surviving followers that have can_face_inherent
            potential_face_boost = sum(
                max(0, u.atk) * u.attacks_left for u in current_ours if u.hp > 0 and u.can_face_inherent
            )
            # If even with all remaining face attackers hitting face we cannot beat best_face_dmg,
            # and wards_cleared is already achieved in best, prune!
            if best_wards_cleared and (current_face_dmg + potential_face_boost < best_face_dmg):
                return

            # Branching: Each available attacker can attack an active ward
            # Sort attackers to test promising branches first (e.g. Bane, or high ATK, or exact lethal on ward)
            sorted_attackers = sorted(
                avail_attackers,
                key=lambda u: (u.has_killer, u.atk > 0, -u.atk),
                reverse=True,
            )

            branch_tested = False
            for attacker in sorted_attackers:
                for ward in list(active_wards):
                    branch_tested = True

                    # Copy state for branch
                    next_ours = [u.clone() for u in current_ours]
                    next_enemy = [e.clone() for e in current_enemy]
                    next_attacker = next(u for u in next_ours if u.uid == attacker.uid)
                    next_ward = next(e for e in next_enemy if e.uid == ward.uid)

                    combat_act = cls._simulate_combat(next_attacker, next_ward)
                    next_actions = current_actions + [combat_act]

                    dfs(
                        next_ours,
                        next_enemy,
                        current_leader_hp,
                        next_actions,
                        current_face_dmg,
                    )

            if not branch_tested:
                eval_score = cls._evaluate_attack_state(
                    face_damage=current_face_dmg,
                    leader_remaining_hp=current_leader_hp,
                    wards_cleared=False,
                    surviving_ours=current_ours,
                    surviving_enemy=current_enemy,
                )
                if eval_score > best_eval_score:
                    best_eval_score = eval_score
                    best_actions = current_actions
                    best_face_dmg = current_face_dmg
                    best_final_leader_hp = current_leader_hp
                    best_wards_cleared = False
                    best_surviving_ours = current_ours
                    best_surviving_enemy = current_enemy

        dfs(ours, enemy, enemy_leader_hp, [], 0)
        return (
            best_actions,
            best_face_dmg,
            best_final_leader_hp,
            best_wards_cleared,
            best_surviving_ours,
            best_surviving_enemy,
        )

    @classmethod
    def _simulate_combat(cls, attacker: SimUnit, defender: SimUnit) -> CombatAction:
        """Simulates simultaneous combat between attacker and defender, mutating them in-place."""
        attacker.attacks_left -= 1
        attacker.has_attacked = True

        # Attacker deals damage to defender
        dmg_to_def = 0
        if defender.has_temp_shield:
            # Shield pops, absorbs hit
            defender.has_temp_shield = False
            dmg_to_def = 0
        else:
            dmg_to_def = max(0, attacker.atk)
            if attacker.has_killer and dmg_to_def > 0:
                defender.hp = 0
            else:
                defender.hp -= dmg_to_def

        # Defender retaliates on attacker
        dmg_to_att = 0
        if attacker.has_temp_shield:
            attacker.has_temp_shield = False
            dmg_to_att = 0
        else:
            dmg_to_att = max(0, defender.atk)
            if defender.has_killer and dmg_to_att > 0:
                attacker.hp = 0
            else:
                attacker.hp -= dmg_to_att

        return CombatAction(
            action_type="attack_follower",
            source_uid=attacker.uid,
            source_name=attacker.name,
            source_pos=(attacker.x, attacker.y),
            target_uid=defender.uid,
            target_name=defender.name,
            target_pos=(defender.x, defender.y),
            damage_dealt=dmg_to_def,
            details={
                "retaliatory_damage": dmg_to_att,
                "defender_died": (defender.hp <= 0),
                "attacker_died": (attacker.hp <= 0),
            },
        )

    @classmethod
    def _pick_best_rush_trade_target(cls, attacker: SimUnit, enemies: List[SimUnit]) -> Optional[SimUnit]:
        """Chooses best non-ward enemy for a rush follower to trade with."""
        if not enemies:
            return None

        # Prefer targets that attacker can kill without dying,
        # then targets attacker can kill,
        # then highest ATK targets.
        def trade_rank(e: SimUnit):
            can_kill = (attacker.atk >= e.hp or attacker.has_killer) and not e.has_temp_shield
            survives = (attacker.hp > e.atk and not e.has_killer) or attacker.has_temp_shield
            return (can_kill and survives, can_kill, e.atk, -e.hp)

        return max(enemies, key=trade_rank)

    @classmethod
    def _evaluate_attack_state(
        cls,
        face_damage: int,
        leader_remaining_hp: int,
        wards_cleared: bool,
        surviving_ours: List[SimUnit],
        surviving_enemy: List[SimUnit],
    ) -> float:
        """Evaluates an attack sequence endpoint during DFS."""
        is_lethal = (leader_remaining_hp <= 0)
        ours_alive_hp = sum(max(0, u.hp) for u in surviving_ours if u.hp > 0)
        enemy_alive_hp = sum(max(0, e.hp) for e in surviving_enemy if e.hp > 0)

        if is_lethal:
            # Overkill damage bonus up to 30
            return 1_000_000.0 + min(face_damage, 30) * 10.0 + ours_alive_hp

        score = (
            face_damage * 10_000.0
            + (5_000.0 if wards_cleared else 0.0)
            - enemy_alive_hp * 20.0
            + ours_alive_hp * 2.0
        )
        return float(score)

    @classmethod
    def _evaluate_plan(
        cls,
        face_damage: int,
        initial_leader_hp: int,
        final_leader_hp: int,
        wards_cleared: bool,
        evo_type: Optional[str],
        evo_utilized: bool,
        surviving_ours: List[SimUnit],
        surviving_enemy: List[SimUnit],
    ) -> float:
        """Final plan ranking across evolution branches."""
        base_score = cls._evaluate_attack_state(
            face_damage=face_damage,
            leader_remaining_hp=final_leader_hp,
            wards_cleared=wards_cleared,
            surviving_ours=surviving_ours,
            surviving_enemy=surviving_enemy,
        )

        is_lethal = (final_leader_hp <= 0)

        ep_cost = 0.0
        if is_lethal:
            # Conserve EP when lethal is reached!
            # Never waste EP or SEP on overkill if lethal is already achieved without it!
            if evo_type == "super":
                ep_cost = 20_000.0
            elif evo_type == "normal":
                ep_cost = 10_000.0
        else:
            if evo_type == "super":
                ep_cost = 3.0
            elif evo_type == "normal":
                ep_cost = 1.0

        # Penalize wasting evolution if it didn't do anything
        if evo_type is not None and not evo_utilized:
            ep_cost += 50.0

        return base_score - ep_cost
