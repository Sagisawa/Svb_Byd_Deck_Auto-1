"""Integration tests for MemoryCombatCoordinator and SnapshotAdapter."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.bridge.snapshot_adapter import SnapshotAdapter, is_bridge_mode_active
from src.game.battle.memory_combat_coordinator import MemoryCombatCoordinator
from src.game.battle.memory_combat_executor import MemoryCombatExecutor


class TestMemoryCombatIntegration(unittest.TestCase):
    def setUp(self):
        self.mock_bridge = MagicMock()
        self.mock_bridge.is_fresh.return_value = True
        self.resolver = MagicMock()
        self.resolver.get_name.side_effect = lambda cid: f"Card_{cid}"
        self.adapter = SnapshotAdapter(bridge=self.mock_bridge, resolver=self.resolver)

    def test_get_evolution_info_with_data(self):
        raw_snap = {
            "root": {
                "players": [
                    {
                        "evolve_points": 2,
                        "super_evolve_points": 1,
                        "field": [{"unique_id": 101, "card_id": 1, "life": 3, "attack": 2, "cost": 2}],
                    },
                    {
                        "field": [],
                    },
                ],
                "legal_actions": {
                    "can_evolve_cards": [101],
                    "can_super_evolve_cards": [101],
                },
            }
        }
        self.mock_bridge.get_snapshot.return_value = raw_snap
        info = self.adapter.get_evolution_info()
        self.assertEqual(info["ep"], 2)
        self.assertEqual(info["sep"], 1)
        self.assertTrue(info["can_evolve"])
        self.assertTrue(info["can_super_evolve"])
        self.assertIn(101, info["can_evolve_cards"])
        self.assertIn(101, info["can_super_evolve_cards"])

    def test_get_combat_snapshot_parsing(self):
        raw_snap = {
            "root": {
                "players": [
                    {
                        "turn": 5,
                        "life": 18,
                        "evolve_points": 1,
                        "super_evolve_points": 0,
                        "field": [
                            {
                                "unique_id": 101,
                                "card_id": 1001,
                                "life": 3,
                                "max_life": 3,
                                "attack": 3,
                                "cost": 3,
                                "card_type": 1,
                                "can_attack_leader": True,
                                "can_attack_field": True,
                            }
                        ],
                    },
                    {
                        "life": 15,
                        "field": [
                            {
                                "unique_id": 201,
                                "card_id": 2001,
                                "life": 2,
                                "attack": 2,
                                "cost": 2,
                                "card_type": 1,
                                "has_guard": True,
                            }
                        ],
                    },
                ],
                "legal_actions": {
                    "can_attack_leader_cards": [101],
                    "can_attack_field_cards": [101],
                    "can_evolve_cards": [101],
                },
            }
        }
        self.mock_bridge.get_snapshot.return_value = raw_snap
        combat_snap = self.adapter.get_combat_snapshot()
        self.assertIsNotNone(combat_snap)
        self.assertEqual(len(combat_snap["our_followers"]), 1)
        self.assertEqual(combat_snap["our_followers"][0]["unique_id"], 101)
        self.assertTrue(combat_snap["our_followers"][0]["can_attack_leader"])
        self.assertTrue(combat_snap["our_followers"][0]["can_evolve"])

        self.assertEqual(len(combat_snap["enemy_followers"]), 1)
        self.assertEqual(combat_snap["enemy_followers"][0]["unique_id"], 201)
        self.assertTrue(combat_snap["enemy_followers"][0]["has_guard"])
        self.assertEqual(combat_snap["enemy_leader"]["hp"], 15)

    def test_coordinator_skips_when_bridge_inactive(self):
        mock_actions = MagicMock()
        mock_actions.device_state._bridge_active = False
        coordinator = MemoryCombatCoordinator(mock_actions, snapshot_adapter=self.adapter)
        result = coordinator.run(allow_evolve=True)
        self.assertFalse(result)

    def test_coordinator_handles_combat_when_bridge_active(self):
        mock_actions = MagicMock()
        mock_actions.device_state._bridge_active = True
        mock_u2 = MagicMock()
        mock_actions._require_u2_device.return_value = mock_u2

        raw_snap = {
            "root": {
                "players": [
                    {
                        "turn": 5,
                        "life": 20,
                        "evolve_points": 0,
                        "super_evolve_points": 0,
                        "field": [
                            {
                                "unique_id": 101,
                                "card_id": 1001,
                                "life": 4,
                                "attack": 4,
                                "cost": 4,
                                "card_type": 1,
                                "can_attack_leader": True,
                                "can_attack_field": True,
                            }
                        ],
                    },
                    {
                        "life": 10,
                        "field": [],
                    },
                ],
                "legal_actions": {
                    "can_attack_leader_cards": [101],
                },
            }
        }
        self.mock_bridge.get_snapshot.return_value = raw_snap

        coordinator = MemoryCombatCoordinator(mock_actions, snapshot_adapter=self.adapter)
        result = coordinator.run(allow_evolve=False)
        self.assertTrue(result)
        # Verify swipe was invoked on device
        mock_u2.swipe.assert_called()

    def test_get_evolution_info_fallback_when_legal_actions_empty(self):
        raw_snap = {
            "root": {
                "players": [
                    {
                        "evolve_points": 2,
                        "super_evolve_points": 1,
                        "field": [],
                    },
                ],
                "legal_actions": {},
            }
        }
        self.mock_bridge.get_snapshot.return_value = raw_snap
        info = self.adapter.get_evolution_info()
        self.assertEqual(info["ep"], 2)
        self.assertEqual(info["sep"], 1)
        self.assertTrue(info["can_evolve"])
        self.assertTrue(info["can_super_evolve"])

    def test_get_combat_snapshot_detects_storm_and_rush(self):
        raw_snap = {
            "root": {
                "players": [
                    {
                        "turn": 4,
                        "life": 20,
                        "evolve_points": 1,
                        "super_evolve_points": 0,
                        "field": [
                            {
                                "unique_id": 101,
                                "card_id": 1001,
                                "life": 3,
                                "attack": 2,
                                "buff": {"quick": True},  # Storm
                            },
                            {
                                "unique_id": 102,
                                "card_id": 1002,
                                "life": 2,
                                "attack": 2,
                                "buff": {"rush": True},  # Rush
                            },
                        ],
                    },
                    {
                        "life": 15,
                        "field": [
                            {
                                "unique_id": 201,
                                "card_id": 2001,
                                "life": 2,
                                "attack": 2,
                                "has_guard": True,
                            }
                        ],
                    },
                ],
                "legal_actions": {
                    "can_attack_field_cards": [101, 102],
                    # Notice: can_attack_leader_cards is EMPTY because of the enemy ward (201)
                    "can_attack_leader_cards": [],
                },
            }
        }
        self.mock_bridge.get_snapshot.return_value = raw_snap
        snap = self.adapter.get_combat_snapshot()
        self.assertIsNotNone(snap)
        followers = {f["unique_id"]: f for f in snap["our_followers"]}
        self.assertTrue(followers[101]["has_storm"])
        self.assertTrue(followers[101]["can_face_inherent"])
        self.assertTrue(followers[102]["has_rush"])
        self.assertFalse(followers[102]["can_face_inherent"])


if __name__ == "__main__":
    unittest.main()
