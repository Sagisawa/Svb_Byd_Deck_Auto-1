"""SephiesDeckLab Bridge Package for Svb_Byd_Deck_Auto.

Provides integration components to consume memory-based game state snapshots
from SephiesDeckLab (via JSONL session log or direct memory reader).
"""

from src.bridge.tracker_bridge import TrackerBridge, get_global_tracker_bridge
from src.bridge.snapshot_adapter import SnapshotAdapter
from src.bridge.card_name_resolver import CardNameResolver

__all__ = [
    "TrackerBridge",
    "get_global_tracker_bridge",
    "SnapshotAdapter",
    "CardNameResolver",
]
