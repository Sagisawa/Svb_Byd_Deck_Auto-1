from __future__ import annotations

import csv
import logging
import os
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class CardNameResolver:
    """Resolves card_id to Chinese card name and base attributes using SV_WB_Cards.csv."""

    _instance: Optional[CardNameResolver] = None

    def __init__(self, csv_path: Optional[str] = None) -> None:
        self._id_to_name: Dict[int, str] = {}
        self._id_to_info: Dict[int, Dict[str, object]] = {}
        self._load_csv(csv_path)

    @classmethod
    def get_instance(cls, csv_path: Optional[str] = None) -> CardNameResolver:
        if cls._instance is None:
            cls._instance = cls(csv_path)
        return cls._instance

    def _find_default_csv(self) -> Optional[str]:
        candidates = [
            os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")), "quanka", "SV_WB_Cards", "SV_WB_Cards.csv"),
            r"D:\auto\Svb_Byd_Deck_Auto-main\quanka\SV_WB_Cards\SV_WB_Cards.csv",
            r"D:\auto\SephiesDeckLab-1.0.0\src\shadowverse_tracker\data\SV_WB_Cards.csv",
        ]
        for p in candidates:
            if os.path.isfile(p):
                return p
        return None

    def _load_csv(self, csv_path: Optional[str]) -> None:
        path = csv_path or self._find_default_csv()
        if not path or not os.path.isfile(path):
            logger.warning("CardNameResolver: SV_WB_Cards.csv not found at %s", path)
            return

        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        cid_str = row.get("card_id")
                        if not cid_str:
                            continue
                        cid = int(cid_str)
                        name = row.get("name", "").strip()
                        cost_str = row.get("cost", "0")
                        cost = int(cost_str) if cost_str.isdigit() else 0
                        atk_str = row.get("atk", "0")
                        atk = int(atk_str) if atk_str.isdigit() else 0
                        life_str = row.get("life", "0")
                        life = int(life_str) if life_str.isdigit() else 0

                        self._id_to_name[cid] = name
                        self._id_to_info[cid] = {
                            "name": name,
                            "cost": cost,
                            "atk": atk,
                            "life": life,
                            "card_type": row.get("card_type", ""),
                            "class_id": row.get("class_id", ""),
                        }
                    except Exception:
                        continue
            logger.info("CardNameResolver: loaded %d cards from %s", len(self._id_to_name), path)
        except Exception as exc:
            logger.error("CardNameResolver: failed to load CSV: %s", exc)

    def get_name(self, card_id: int, fallback: str = "") -> str:
        if not card_id:
            return fallback
        cid = int(card_id)
        if cid in self._id_to_name:
            return self._id_to_name[cid]
        # 影之诗 IL2CPP 中，随从进化、超进化或衍生物状态的 card_id 末位常为 1 或 2（例如 10552111 / 10752111）
        # 原始基础卡牌 ID 末位统一为 0（如 10552110 / 10752110）
        base_cid = (cid // 10) * 10
        if base_cid in self._id_to_name:
            return self._id_to_name[base_cid]
        return fallback or f"Card_{card_id}"

    def get_info(self, card_id: int) -> Dict[str, object]:
        if not card_id:
            return {}
        cid = int(card_id)
        if cid in self._id_to_info:
            return self._id_to_info[cid]
        base_cid = (cid // 10) * 10
        return self._id_to_info.get(base_cid, {})
