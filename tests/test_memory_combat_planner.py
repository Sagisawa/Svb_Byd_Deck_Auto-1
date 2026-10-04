"""Unit tests for MemoryCombatPlanner."""

from __future__ import annotations

import unittest
from src.game.battle.memory_combat_planner import MemoryCombatPlanner


class TestMemoryCombatPlanner(unittest.TestCase):
    def test_empty_board(self):
        snapshot = {
            "our_followers": [],
            "enemy_followers": [],
            "enemy_leader": {"hp": 20, "x": 646, "y": 64},
            "evolution_info": {"can_evolve": False, "can_super_evolve": False},
        }
        plan = MemoryCombatPlanner.plan(snapshot)
        self.assertEqual(len(plan.actions), 0)
        self.assertEqual(plan.total_face_damage, 0)
        self.assertFalse(plan.is_lethal)
        self.assertEqual(plan.enemy_leader_remaining_hp, 20)

    def test_face_attack_no_wards(self):
        """No wards: all storm followers should hit face directly."""
        snapshot = {
            "our_followers": [
                {
                    "unique_id": 1,
                    "name": "StormUnitA",
                    "x": 500,
                    "y": 400,
                    "atk": 3,
                    "hp": 3,
                    "can_attack_leader": True,
                    "can_attack_field": True,
                    "attacks_left": 1,
                },
                {
                    "unique_id": 2,
                    "name": "StormUnitB",
                    "x": 600,
                    "y": 400,
                    "atk": 4,
                    "hp": 4,
                    "can_attack_leader": True,
                    "can_attack_field": True,
                    "attacks_left": 1,
                },
            ],
            "enemy_followers": [],
            "enemy_leader": {"hp": 15, "x": 646, "y": 64},
            "evolution_info": {"can_evolve": False, "can_super_evolve": False},
        }
        plan = MemoryCombatPlanner.plan(snapshot, allow_evolve=False)
        self.assertEqual(plan.total_face_damage, 7)
        self.assertEqual(plan.enemy_leader_remaining_hp, 8)
        self.assertEqual(len(plan.actions), 2)
        self.assertTrue(all(a.action_type == "attack_leader" for a in plan.actions))

    def test_ward_blocks_face_attack(self):
        """Ward must be killed by rush before storm can hit face."""
        snapshot = {
            "our_followers": [
                {
                    "unique_id": 1,
                    "name": "RushUnit",
                    "x": 500,
                    "y": 400,
                    "atk": 3,
                    "hp": 3,
                    "can_attack_leader": False,
                    "can_attack_field": True,
                    "attacks_left": 1,
                },
                {
                    "unique_id": 2,
                    "name": "StormUnit",
                    "x": 600,
                    "y": 400,
                    "atk": 5,
                    "hp": 5,
                    "can_attack_leader": True,
                    "can_attack_field": True,
                    "attacks_left": 1,
                },
            ],
            "enemy_followers": [
                {
                    "unique_id": 10,
                    "name": "WardUnit",
                    "x": 550,
                    "y": 200,
                    "atk": 2,
                    "hp": 3,
                    "has_guard": True,
                }
            ],
            "enemy_leader": {"hp": 10, "x": 646, "y": 64},
            "evolution_info": {"can_evolve": False, "can_super_evolve": False},
        }
        plan = MemoryCombatPlanner.plan(snapshot, allow_evolve=False)
        self.assertEqual(plan.total_face_damage, 5)
        self.assertEqual(plan.enemy_leader_remaining_hp, 5)
        self.assertTrue(plan.wards_cleared)
        self.assertEqual(len(plan.actions), 2)
        self.assertEqual(plan.actions[0].action_type, "attack_follower")
        self.assertEqual(plan.actions[0].target_uid, 10)
        self.assertEqual(plan.actions[1].action_type, "attack_leader")
        self.assertEqual(plan.actions[1].source_uid, 2)

    def test_normal_evolution_boosts_storm_face_damage(self):
        """Normal evolution adds ATK+2 to storm follower for lethal."""
        snapshot = {
            "our_followers": [
                {
                    "unique_id": 1,
                    "name": "StormUnit",
                    "x": 500,
                    "y": 400,
                    "atk": 3,
                    "hp": 3,
                    "can_attack_leader": True,
                    "can_attack_field": True,
                    "attacks_left": 1,
                    "can_evolve": True,
                }
            ],
            "enemy_followers": [],
            "enemy_leader": {"hp": 5, "x": 646, "y": 64},
            "evolution_info": {"can_evolve": True, "can_super_evolve": False, "ep": 1, "sep": 0},
        }
        plan = MemoryCombatPlanner.plan(snapshot, allow_evolve=True)
        self.assertEqual(plan.total_face_damage, 5)
        self.assertEqual(plan.enemy_leader_remaining_hp, 0)
        self.assertTrue(plan.is_lethal)
        self.assertEqual(plan.evolve_type, "normal")
        self.assertEqual(plan.evolved_uid, 1)
        self.assertEqual(plan.actions[0].action_type, "evolve_normal")
        self.assertEqual(plan.actions[1].action_type, "attack_leader")

    def test_super_evolution_boosts_storm_face_damage(self):
        """Super evolution adds ATK+3 to storm follower for lethal."""
        snapshot = {
            "our_followers": [
                {
                    "unique_id": 1,
                    "name": "StormUnit",
                    "x": 500,
                    "y": 400,
                    "atk": 3,
                    "hp": 3,
                    "can_attack_leader": True,
                    "can_attack_field": True,
                    "attacks_left": 1,
                    "can_super_evolve": True,
                }
            ],
            "enemy_followers": [],
            "enemy_leader": {"hp": 6, "x": 646, "y": 64},
            "evolution_info": {"can_evolve": False, "can_super_evolve": True, "ep": 0, "sep": 1},
        }
        plan = MemoryCombatPlanner.plan(snapshot, allow_evolve=True)
        self.assertEqual(plan.total_face_damage, 6)
        self.assertEqual(plan.enemy_leader_remaining_hp, 0)
        self.assertTrue(plan.is_lethal)
        self.assertEqual(plan.evolve_type, "super")
        self.assertEqual(plan.evolved_uid, 1)

    def test_fresh_follower_evolves_into_rush_to_clear_ward(self):
        """Fresh follower (cannot attack) evolves to gain Rush (+2 ATK) to clear 3 HP ward for Storm."""
        snapshot = {
            "our_followers": [
                {
                    "unique_id": 1,
                    "name": "FreshUnit",
                    "x": 500,
                    "y": 400,
                    "atk": 1,
                    "hp": 2,
                    "can_attack_leader": False,
                    "can_attack_field": False,
                    "attacks_left": 0,
                    "can_evolve": True,
                },
                {
                    "unique_id": 2,
                    "name": "StormUnit",
                    "x": 600,
                    "y": 400,
                    "atk": 4,
                    "hp": 4,
                    "can_attack_leader": True,
                    "can_attack_field": True,
                    "attacks_left": 1,
                    "can_evolve": True,
                },
            ],
            "enemy_followers": [
                {
                    "unique_id": 10,
                    "name": "WardUnit",
                    "x": 550,
                    "y": 200,
                    "atk": 2,
                    "hp": 3,
                    "has_guard": True,
                }
            ],
            "enemy_leader": {"hp": 4, "x": 646, "y": 64},
            "evolution_info": {"can_evolve": True, "can_super_evolve": False, "ep": 1, "sep": 0},
        }
        plan = MemoryCombatPlanner.plan(snapshot, allow_evolve=True)
        # Evolving FreshUnit allows it to kill Ward (1+2 = 3 ATK), letting StormUnit deal 4 face damage (lethal!)
        # If StormUnit evolved instead, StormUnit would have to kill the Ward, resulting in 0 face damage.
        self.assertEqual(plan.evolved_uid, 1)
        self.assertEqual(plan.evolve_type, "normal")
        self.assertEqual(plan.total_face_damage, 4)
        self.assertTrue(plan.is_lethal)

    def test_choice_between_evolving_rush_vs_storm(self):
        """Evolving 3 ATK Rush follower allows 1-shotting 5 HP Ward, preserving 5 ATK Storm for face."""
        snapshot = {
            "our_followers": [
                {
                    "unique_id": 1,
                    "name": "RushUnit",
                    "x": 500,
                    "y": 400,
                    "atk": 3,
                    "hp": 3,
                    "can_attack_leader": False,
                    "can_attack_field": True,
                    "attacks_left": 1,
                    "can_evolve": True,
                },
                {
                    "unique_id": 2,
                    "name": "StormUnit",
                    "x": 600,
                    "y": 400,
                    "atk": 5,
                    "hp": 5,
                    "can_attack_leader": True,
                    "can_attack_field": True,
                    "attacks_left": 1,
                    "can_evolve": True,
                },
            ],
            "enemy_followers": [
                {
                    "unique_id": 10,
                    "name": "BigWard",
                    "x": 550,
                    "y": 200,
                    "atk": 4,
                    "hp": 5,
                    "has_guard": True,
                }
            ],
            "enemy_leader": {"hp": 5, "x": 646, "y": 64},
            "evolution_info": {"can_evolve": True, "can_super_evolve": False, "ep": 1, "sep": 0},
        }
        plan = MemoryCombatPlanner.plan(snapshot, allow_evolve=True)
        self.assertEqual(plan.evolved_uid, 1)
        self.assertEqual(plan.total_face_damage, 5)
        self.assertTrue(plan.is_lethal)

    def test_bane_kills_high_hp_ward(self):
        """Bane unit 1-shots a 10 HP Ward, allowing Storm to hit face."""
        snapshot = {
            "our_followers": [
                {
                    "unique_id": 1,
                    "name": "BaneUnit",
                    "x": 500,
                    "y": 400,
                    "atk": 1,
                    "hp": 1,
                    "can_attack_leader": False,
                    "can_attack_field": True,
                    "attacks_left": 1,
                    "has_killer": True,
                },
                {
                    "unique_id": 2,
                    "name": "StormUnit",
                    "x": 600,
                    "y": 400,
                    "atk": 6,
                    "hp": 6,
                    "can_attack_leader": True,
                    "can_attack_field": True,
                    "attacks_left": 1,
                },
            ],
            "enemy_followers": [
                {
                    "unique_id": 10,
                    "name": "GiantWard",
                    "x": 550,
                    "y": 200,
                    "atk": 5,
                    "hp": 10,
                    "has_guard": True,
                }
            ],
            "enemy_leader": {"hp": 6, "x": 646, "y": 64},
            "evolution_info": {"can_evolve": False, "can_super_evolve": False},
        }
        plan = MemoryCombatPlanner.plan(snapshot, allow_evolve=False)
        self.assertEqual(plan.total_face_damage, 6)
        self.assertTrue(plan.is_lethal)
        self.assertTrue(plan.wards_cleared)

    def test_divine_shield_on_ward(self):
        """First hit on divine shield ward is absorbed; second hit kills ward."""
        snapshot = {
            "our_followers": [
                {
                    "unique_id": 1,
                    "name": "SmallRush",
                    "x": 450,
                    "y": 400,
                    "atk": 1,
                    "hp": 1,
                    "can_attack_leader": False,
                    "can_attack_field": True,
                    "attacks_left": 1,
                },
                {
                    "unique_id": 2,
                    "name": "MedRush",
                    "x": 550,
                    "y": 400,
                    "atk": 3,
                    "hp": 3,
                    "can_attack_leader": False,
                    "can_attack_field": True,
                    "attacks_left": 1,
                },
                {
                    "unique_id": 3,
                    "name": "BigStorm",
                    "x": 650,
                    "y": 400,
                    "atk": 5,
                    "hp": 5,
                    "can_attack_leader": True,
                    "can_attack_field": True,
                    "attacks_left": 1,
                },
            ],
            "enemy_followers": [
                {
                    "unique_id": 10,
                    "name": "ShieldedWard",
                    "x": 550,
                    "y": 200,
                    "atk": 2,
                    "hp": 3,
                    "has_guard": True,
                    "has_temp_shield": True,
                }
            ],
            "enemy_leader": {"hp": 5, "x": 646, "y": 64},
            "evolution_info": {"can_evolve": False, "can_super_evolve": False},
        }
        plan = MemoryCombatPlanner.plan(snapshot, allow_evolve=False)
        self.assertEqual(plan.total_face_damage, 5)
        self.assertTrue(plan.is_lethal)
        # SmallRush popped shield, MedRush killed, BigStorm hit face
        self.assertEqual(plan.actions[0].source_uid, 1)
        self.assertEqual(plan.actions[1].source_uid, 2)
        self.assertEqual(plan.actions[2].source_uid, 3)

    def test_multiple_wards_optimal_trading(self):
        """Optimal trading sequence with 2 wards and 3 followers."""
        snapshot = {
            "our_followers": [
                {
                    "unique_id": 1,
                    "name": "Attacker1",
                    "x": 450,
                    "y": 400,
                    "atk": 1,
                    "hp": 1,
                    "can_attack_leader": False,
                    "can_attack_field": True,
                    "attacks_left": 1,
                },
                {
                    "unique_id": 2,
                    "name": "Attacker2",
                    "x": 550,
                    "y": 400,
                    "atk": 4,
                    "hp": 4,
                    "can_attack_leader": False,
                    "can_attack_field": True,
                    "attacks_left": 1,
                },
                {
                    "unique_id": 3,
                    "name": "StormFollower",
                    "x": 650,
                    "y": 400,
                    "atk": 5,
                    "hp": 5,
                    "can_attack_leader": True,
                    "can_attack_field": True,
                    "attacks_left": 1,
                },
            ],
            "enemy_followers": [
                {
                    "unique_id": 10,
                    "name": "Ward1HP",
                    "x": 500,
                    "y": 200,
                    "atk": 1,
                    "hp": 1,
                    "has_guard": True,
                },
                {
                    "unique_id": 20,
                    "name": "Ward4HP",
                    "x": 600,
                    "y": 200,
                    "atk": 2,
                    "hp": 4,
                    "has_guard": True,
                },
            ],
            "enemy_leader": {"hp": 10, "x": 646, "y": 64},
            "evolution_info": {"can_evolve": False, "can_super_evolve": False},
        }
        plan = MemoryCombatPlanner.plan(snapshot, allow_evolve=False)
        self.assertEqual(plan.total_face_damage, 5)
        self.assertTrue(plan.wards_cleared)

    def test_fallback_protection_when_wards_cannot_be_cleared(self):
        """When wards have overwhelming HP, followers make best trades."""
        snapshot = {
            "our_followers": [
                {
                    "unique_id": 1,
                    "name": "Rush1",
                    "x": 500,
                    "y": 400,
                    "atk": 3,
                    "hp": 3,
                    "can_attack_leader": False,
                    "can_attack_field": True,
                    "attacks_left": 1,
                },
                {
                    "unique_id": 2,
                    "name": "Rush2",
                    "x": 600,
                    "y": 400,
                    "atk": 3,
                    "hp": 3,
                    "can_attack_leader": False,
                    "can_attack_field": True,
                    "attacks_left": 1,
                },
            ],
            "enemy_followers": [
                {
                    "unique_id": 10,
                    "name": "HugeWard",
                    "x": 550,
                    "y": 200,
                    "atk": 5,
                    "hp": 20,
                    "has_guard": True,
                }
            ],
            "enemy_leader": {"hp": 10, "x": 646, "y": 64},
            "evolution_info": {"can_evolve": False, "can_super_evolve": False},
        }
        plan = MemoryCombatPlanner.plan(snapshot, allow_evolve=False)
        self.assertEqual(plan.total_face_damage, 0)
        self.assertFalse(plan.wards_cleared)
        self.assertEqual(len(plan.actions), 2)
        # Both attacked the ward to deal max damage
        self.assertTrue(all(a.action_type == "attack_follower" for a in plan.actions))

    def test_ep_conservation_when_no_extra_damage(self):
        """Do not waste evolution if face damage and board outcome are already maximal."""
        snapshot = {
            "our_followers": [
                {
                    "unique_id": 1,
                    "name": "FreshUnit",
                    "x": 500,
                    "y": 400,
                    "atk": 2,
                    "hp": 2,
                    "can_attack_leader": False,
                    "can_attack_field": False,
                    "attacks_left": 0,
                    "can_evolve": True,
                }
            ],
            "enemy_followers": [],
            "enemy_leader": {"hp": 10, "x": 646, "y": 64},
            "evolution_info": {"can_evolve": True, "can_super_evolve": False, "ep": 1, "sep": 0},
        }
        plan = MemoryCombatPlanner.plan(snapshot, allow_evolve=True)
        # Fresh follower would only gain Rush (cannot hit face), so evolving it does 0 face damage
        # and wastes EP. The planner should NOT evolve it!
        self.assertIsNone(plan.evolve_type)
        self.assertEqual(plan.total_face_damage, 0)

    def test_windfury_attacks_twice(self):
        """Follower with 2 attacks attacks face twice."""
        snapshot = {
            "our_followers": [
                {
                    "unique_id": 1,
                    "name": "WindfuryStorm",
                    "x": 500,
                    "y": 400,
                    "atk": 4,
                    "hp": 4,
                    "can_attack_leader": True,
                    "can_attack_field": True,
                    "attacks_left": 2,
                }
            ],
            "enemy_followers": [],
            "enemy_leader": {"hp": 10, "x": 646, "y": 64},
            "evolution_info": {"can_evolve": False, "can_super_evolve": False},
        }
        plan = MemoryCombatPlanner.plan(snapshot, allow_evolve=False)
        self.assertEqual(plan.total_face_damage, 8)
        self.assertEqual(len(plan.actions), 2)
        self.assertTrue(all(a.action_type == "attack_leader" for a in plan.actions))

    def test_allow_evolve_disabled(self):
        """When allow_evolve=False, no evolution occurs even if EP is available."""
        snapshot = {
            "our_followers": [
                {
                    "unique_id": 1,
                    "name": "StormUnit",
                    "x": 500,
                    "y": 400,
                    "atk": 3,
                    "hp": 3,
                    "can_attack_leader": True,
                    "can_attack_field": True,
                    "attacks_left": 1,
                    "can_evolve": True,
                }
            ],
            "enemy_followers": [],
            "enemy_leader": {"hp": 5, "x": 646, "y": 64},
            "evolution_info": {"can_evolve": True, "can_super_evolve": False, "ep": 1, "sep": 0},
        }
        plan = MemoryCombatPlanner.plan(snapshot, allow_evolve=False)
        self.assertIsNone(plan.evolve_type)
        self.assertEqual(plan.total_face_damage, 3)

    def test_friendly_divine_shield_survives_retaliation(self):
        """Friendly follower with divine shield attacks high ATK ward, survives."""
        snapshot = {
            "our_followers": [
                {
                    "unique_id": 1,
                    "name": "ShieldedAttacker",
                    "x": 500,
                    "y": 400,
                    "atk": 5,
                    "hp": 1,
                    "can_attack_leader": False,
                    "can_attack_field": True,
                    "attacks_left": 1,
                    "has_temp_shield": True,
                }
            ],
            "enemy_followers": [
                {
                    "unique_id": 10,
                    "name": "DeadlyWard",
                    "x": 550,
                    "y": 200,
                    "atk": 10,
                    "hp": 5,
                    "has_guard": True,
                }
            ],
            "enemy_leader": {"hp": 10, "x": 646, "y": 64},
            "evolution_info": {"can_evolve": False, "can_super_evolve": False},
        }
        plan = MemoryCombatPlanner.plan(snapshot, allow_evolve=False)
        self.assertEqual(len(plan.actions), 1)
        self.assertEqual(plan.actions[0].details["retaliatory_damage"], 0)
        self.assertTrue(plan.actions[0].details["defender_died"])
        self.assertFalse(plan.actions[0].details["attacker_died"])



if __name__ == "__main__":
    unittest.main()
