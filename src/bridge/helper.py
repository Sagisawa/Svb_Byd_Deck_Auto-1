from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def is_memory_reader_enabled() -> bool:
    try:
        from src.config.settings import get_runtime_config
        cfg = get_runtime_config()
        if isinstance(cfg, dict):
            mem_cfg = cfg.get("memory_reader")
            if isinstance(mem_cfg, dict):
                return bool(mem_cfg.get("enabled", True))
        # Fallback to reading config.json
        from src.config.paths import get_config_path
        p = get_config_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
                mem_cfg = data.get("memory_reader")
                if isinstance(mem_cfg, dict):
                    return bool(mem_cfg.get("enabled", True))
    except Exception:
        pass
    return True


def get_memory_adapter() -> Optional[Any]:
    if not is_memory_reader_enabled():
        return None

    try:
        from src.bridge.tracker_bridge import get_global_tracker_bridge
        from src.bridge.snapshot_adapter import SnapshotAdapter

        bridge = get_global_tracker_bridge()
        if not bridge.is_running():
            bridge.start()

        return SnapshotAdapter(bridge)
    except Exception as exc:
        logger.debug("get_memory_adapter error: %s", exc)
    return None
