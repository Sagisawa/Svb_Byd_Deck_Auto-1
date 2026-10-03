"""Background, read-only battle-state polling service.

The service deliberately owns no debugger integration and exposes no process
write/injection operations.  A UI can start it once and receive snapshots while
the game continues normally.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Callable

from .deck_ledger import DeckLedger
from .memory.battle import read_battle_model, read_battle_root_snapshot
from .memory.deck import DeckInfoSnapshot
from .memory.discovery import (
    find_battle_models,
    find_battle_result_class_rating,
    find_battle_roots,
    find_battle_view_server_data,
    read_battle_result_class_rating,
)
from .memory.win32 import ProcessInfo, ProcessReader, find_process_candidates
from .opponent_hand import OpponentKnownHand
from .card_catalog import canonical_card_id
from .match_history import (
    infer_local_player_index,
    orient_class_ids,
    orient_player_order,
    player_unique_ids,
    result_label,
)
from .training_data import (
    TrainingMatchRecorder,
    TrainingUploadQueue,
    compact_event_records,
    default_upload_queue_path,
)
from .versioning import VersionProfile, verify_process_version


SESSION_LOG_SCHEMA_VERSION = 2
DEFAULT_SESSION_LOG_MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class TrackerConfig:
    # The official Steam build and the China build have different executable
    # names.  Keep ``process_name`` as the backwards-compatible preferred
    # value, then try the known China names when automatic discovery is used.
    process_name: str = "ShadowverseWB.exe"
    process_aliases: tuple[str, ...] = (
        "MuMu模拟器x影之诗高清版.exe",
        # Some Windows APIs expose the on-disk Unity player suffix instead of
        # the PE's display name.  It is harmless to include both spellings.
        "MuMu模拟器x影之诗高清版.o",
    )
    model_address: int = 0
    pid: int | None = None
    interval: float = 0.25
    output_path: Path | None = None
    selected_deck: DeckInfoSnapshot | None = None
    selected_deck_key: str | None = None
    selected_core_card_id: int | None = None
    reveal_opponent_hand: bool = True
    # The compact per-match stream is independent from the optional local
    # win/loss history.  ``None`` disables the file but the in-memory recorder
    # still remains available to callers that inspect snapshots.
    training_output_path: Path | None = None
    training_upload_queue_path: Path | None = None
    training_upload_url: str | None = None
    training_upload_enabled: bool = False
    training_upload_token: str | None = None
    # ``auto`` keeps the historical behaviour.  The UI can constrain
    # discovery to one client when both Steam and the China emulator are
    # installed at the same time.
    client_mode: str = "auto"
    # ``app_session.jsonl`` is a diagnostic/backfill stream, not the complete
    # training archive.  Keep the active file bounded and gzip old segments
    # instead of deleting them.  A non-positive value disables rotation.
    # Keep this new option at the end so existing positional configs retain
    # their original field order.
    session_log_max_bytes: int = DEFAULT_SESSION_LOG_MAX_BYTES

    @property
    def process_candidates(self) -> tuple[str, ...]:
        """Names tried by automatic process discovery, in preference order."""
        mode = str(self.client_mode or "auto").strip().casefold()
        if mode in {"steam", "global"}:
            names = (self.process_name,)
        elif mode in {"cn", "china", "国服"}:
            names = self.process_aliases
        else:
            names = (self.process_name, *self.process_aliases)
        return tuple(
            dict.fromkeys(
                name.strip()
                for name in names
                if name and name.strip()
            )
        )


def without_addresses(value: object) -> object:
    """Remove managed-object addresses from a snapshot for stable comparison."""
    if isinstance(value, dict):
        return {
            key: without_addresses(item)
            for key, item in value.items()
            if key != "address"
        }
    if isinstance(value, list):
        return [without_addresses(item) for item in value]
    if isinstance(value, tuple):
        return tuple(without_addresses(item) for item in value)
    return value


def _optional_card_id(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return None
    if candidate <= 0:
        return None
    return canonical_card_id(candidate)


def _session_card_id(value: object) -> int | None:
    """Extract a canonical card ID from one reader/JSON card shape."""
    raw = value
    if isinstance(value, Mapping):
        raw = (
            value.get("base_card_id")
            or value.get("baseCardId")
            or value.get("card_id")
            or value.get("cardId")
            or value.get("id")
        )
    elif isinstance(value, (list, tuple)) and value:
        raw = value[0]
    return _optional_card_id(raw)


def _compact_session_card(value: object) -> dict[str, object] | None:
    """Keep only identity fields needed for ownership and public-card scans."""
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        unique_id = value.get("unique_id")
        try:
            unique_id = int(unique_id) if unique_id is not None else None
        except (TypeError, ValueError):
            unique_id = None
        if isinstance(unique_id, int) and unique_id > 0:
            result["unique_id"] = unique_id
        card_id = _session_card_id(value)
        if card_id is not None:
            result["base_card_id"] = card_id
        if "hidden" in value:
            result["hidden"] = bool(value.get("hidden"))
        return result or None
    card_id = _session_card_id(value)
    return {"base_card_id": card_id} if card_id is not None else None


def _compact_session_cards(value: object) -> list[dict[str, object]]:
    if isinstance(value, Mapping):
        value = (value,)
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, object]] = []
    for item in value:
        compact = _compact_session_card(item)
        if compact is not None:
            result.append(compact)
    return result


def _compact_session_turn_entries(value: object) -> list[dict[str, int]]:
    """Compact ``(card_id, turn)`` observations while accepting old shapes."""
    if isinstance(value, Mapping):
        value = value.get("self") or value.get("opponent") or ()
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, int]] = []
    for item in value:
        card_id = _session_card_id(item)
        raw_turn: object = None
        if isinstance(item, Mapping):
            raw_turn = item.get("turn")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            raw_turn = item[1]
        try:
            turn = int(raw_turn)
        except (TypeError, ValueError):
            continue
        if card_id is not None and turn > 0:
            result.append({"card_id": card_id, "turn": turn})
    return result


def _compact_session_actions(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        compact: dict[str, object] = {}
        kind = item.get("kind")
        if kind is not None:
            compact["kind"] = str(kind)
        card_id = _session_card_id(item)
        if card_id is not None:
            compact["card_id"] = card_id
        for key in ("turn", "count"):
            raw = item.get(key)
            try:
                parsed = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, int):
                compact[key] = parsed
        if compact:
            result.append(compact)
    return result


def _compact_session_knowledge(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, object] = {}
    known_cards = value.get("known_cards")
    compact_known: list[dict[str, int]] = []
    if isinstance(known_cards, (list, tuple)):
        for item in known_cards:
            if not isinstance(item, Mapping):
                continue
            card_id = _session_card_id(item)
            try:
                count = int(item.get("count", 1))
            except (TypeError, ValueError):
                count = 1
            if card_id is not None and count > 0:
                compact_known.append({"card_id": card_id, "count": count})
    if compact_known:
        result["known_cards"] = compact_known
    actions = _compact_session_actions(value.get("recent_actions"))
    if actions:
        result["recent_actions"] = actions
    evolution_events = _compact_session_turn_entries(value.get("recent_evolution_events"))
    if evolution_events:
        result["recent_evolution_events"] = evolution_events
    return result or None


def _compact_session_player(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for key in (
        "unique_id",
        "result_code",
        "life",
        "max_life",
        "turn",
        "deck_count",
        "cemetery_count",
    ):
        raw = value.get(key)
        if isinstance(raw, bool) or raw is None:
            continue
        try:
            result[key] = int(raw)
        except (TypeError, ValueError):
            continue
    if isinstance(value.get("is_first_side"), bool):
        result["is_first_side"] = value["is_first_side"]
    for key in ("hand", "field", "crests", "extra_crests", "special_action_cards"):
        cards = _compact_session_cards(value.get(key))
        if cards:
            result[key] = cards
    for key in ("played_card_ids", "destroyed_card_ids"):
        cards = _compact_session_cards(value.get(key))
        if cards:
            # The matcher and history helpers accept this compact mapping
            # shape, while duplicates continue to represent copy counts.
            result[key] = cards
        elif isinstance(value.get(key), (list, tuple)):
            result[key] = []
    turns = value.get("_played_card_turns")
    if isinstance(turns, (list, tuple)):
        compact_turns: list[int] = []
        for raw in turns:
            try:
                compact_turns.append(int(raw))
            except (TypeError, ValueError):
                continue
        if compact_turns:
            result["_played_card_turns"] = compact_turns
    for key in ("_event_played_cards", "played_card_turns"):
        entries = _compact_session_turn_entries(value.get(key))
        if entries:
            result[key] = entries
    actions = _compact_session_actions(value.get("_recent_actions"))
    if actions:
        result["_recent_actions"] = actions
    knowledge = _compact_session_knowledge(value.get("opponent_hand_knowledge"))
    if knowledge is not None:
        result["opponent_hand_knowledge"] = knowledge
    return result


def _compact_session_event(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, object] = {}
    event_type = value.get("type")
    if event_type is not None:
        result["type"] = str(event_type)
    for key in (
        "is_ally",
        "unique_id",
        "from_unique_id",
        "card_unique_id",
        "attacker_unique_id",
        "sequence",
        "turn",
    ):
        raw = value.get(key)
        if key == "is_ally":
            if isinstance(raw, bool):
                result[key] = raw
            continue
        if raw is None:
            continue
        try:
            result[key] = int(raw)
        except (TypeError, ValueError):
            continue
    for key in ("card_id", "from_card_id", "after_play_card_id", "evolved_card_id"):
        card_id = _session_card_id(value.get(key))
        if card_id is not None:
            result[key] = card_id
    raw_results = value.get("result_codes")
    if isinstance(raw_results, (list, tuple)):
        result_codes: list[int] = []
        for raw in raw_results[:2]:
            try:
                result_codes.append(int(raw))
            except (TypeError, ValueError):
                continue
        if len(result_codes) >= 2:
            result["result_codes"] = result_codes[:2]
    for key in ("card", "evolved_card", "fusion_card"):
        card = _compact_session_card(value.get(key))
        if card is not None:
            result[key] = card
    cards = _compact_session_cards(value.get("cards"))
    if cards:
        result["cards"] = cards
    return result or None


def _compact_session_deck(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, object] = {}
    for key in (
        "deck_key",
        "key",
        "deck_name",
        "name",
    ):
        raw = value.get(key)
        if raw is not None and str(raw).strip():
            result[key] = str(raw)
    for key in ("class_id", "deck_format", "format_version", "core_card_id"):
        raw = value.get(key)
        try:
            parsed = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, int):
            result[key] = parsed
    core = _session_card_id(value.get("core"))
    if core is not None:
        result["core"] = core
    return result or None


def compact_session_snapshot(snapshot: Mapping[str, object]) -> dict[str, object]:
    """Return the small, backfill-compatible representation used by v2 logs.

    ``training_matches.jsonl`` remains the complete replay/training stream.
    This diagnostic stream only needs terminal identity, ownership evidence,
    public played cards, and play turns.  Keeping the function separate also
    lets old full-snapshot logs continue to be read unchanged.
    """
    result: dict[str, object] = {}
    for key in (
        "address",
        "current_turn",
        "self_class_id",
        "opponent_class_id",
        "actual_self_class_id",
        "actual_opponent_class_id",
        "deck_format",
        "battle_mode",
        "training_match_id",
        "ownership_resolved",
        "deck_mismatch",
        "class_mismatch",
        "format_mismatch",
    ):
        if key not in snapshot:
            continue
        value = snapshot.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
    root = snapshot.get("root")
    if isinstance(root, Mapping):
        compact_root: dict[str, object] = {}
        if isinstance(root.get("is_ally_turn"), bool):
            compact_root["is_ally_turn"] = root["is_ally_turn"]
        players = root.get("players")
        if isinstance(players, (list, tuple)):
            compact_root["players"] = [_compact_session_player(item) for item in players[:2]]
        result["root"] = compact_root
    events = snapshot.get("events")
    if isinstance(events, (list, tuple)):
        result["events"] = [
            compact for item in events
            if (compact := _compact_session_event(item)) is not None
        ]
    deck = _compact_session_deck(snapshot.get("deck"))
    if deck is not None:
        result["deck"] = deck
    knowledge = _compact_session_knowledge(snapshot.get("opponent_hand_knowledge"))
    if knowledge is not None:
        result["opponent_hand_knowledge"] = knowledge
    class_rating = snapshot.get("class_rating")
    if isinstance(class_rating, Mapping):
        compact_rating: dict[str, int] = {}
        for key in ("before", "change", "after", "current", "cr_before", "cr_change", "cr_after"):
            raw = class_rating.get(key)
            try:
                parsed = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, int):
                compact_rating[key] = parsed
        if compact_rating:
            result["class_rating"] = compact_rating
    metadata = snapshot.get("training_metadata")
    if isinstance(metadata, Mapping):
        compact_metadata: dict[str, object] = {}
        for key in (
            "self_core_card_id",
            "opponent_core_card_id",
            "opponent_deck_profile_id",
            "opponent_deck_name",
        ):
            value = metadata.get(key)
            if isinstance(value, (str, int)):
                compact_metadata[key] = value
        if compact_metadata:
            result["training_metadata"] = compact_metadata
    return result


class TrackerService:
    """Poll a BattleModel on a worker thread and publish semantic changes."""

    def __init__(
        self,
        config: TrackerConfig,
        *,
        on_snapshot: Callable[[dict[str, object]], None],
        on_error: Callable[[Exception], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_deck: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        if config.interval <= 0:
            raise ValueError("interval must be positive")
        self.config = config
        self.on_snapshot = on_snapshot
        self.on_error = on_error
        self.on_status = on_status
        self.on_deck = on_deck
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._previous: object | None = None
        self._output_handle = None
        self._output_bytes = 0
        self._training_output_handle = None
        self._pid: int | None = None
        self._model_address = config.model_address
        self._battle_root_address = 0
        self._root_only_mode = False
        # A result/puzzle screen can expose only BattleRootMpo for a short
        # time.  Keep probing for the regular BattleModel while displaying
        # that root; otherwise the reader would remain in root-only mode
        # forever and miss the next online match.
        self._root_only_next_model_probe_at = 0.0
        self._battle_view_server_data_address = 0
        self._server_data_discovery_attempted = False
        self._server_data_next_retry_at = 0.0
        self._battle_result_model_address = 0
        self._class_rating_result: dict[str, int] | None = None
        self._class_rating_next_retry_at = 0.0
        # Result-page discovery scans the private heap and can take several
        # seconds.  Keep it off the live board polling thread so a terminal
        # animation cannot freeze the last playable turn.
        self._class_rating_probe_lock = threading.RLock()
        self._class_rating_probe_in_progress = False
        self._class_rating_probe_generation = 0
        self._class_rating_probe_attempts = 0
        self._class_rating_probe_result: tuple[int, dict[str, int]] | None = None
        self._class_rating_pending_snapshot: dict[str, object] | None = None
        self._class_rating_pending_snapshot_valid = False
        self._class_rating_pending_since = 0.0
        self._class_rating_ready_snapshot: dict[str, object] | None = None
        self._class_rating_last_fingerprint: tuple[int, int] | None = None
        self._deck_lock = threading.RLock()
        self._selected_deck = config.selected_deck
        self._selected_deck_key = config.selected_deck_key
        self._selected_core_card_id = _optional_card_id(config.selected_core_card_id)
        self._ledger = DeckLedger(config.selected_deck) if config.selected_deck else None
        self._opponent_known_hand = OpponentKnownHand()
        self._self_action_tracker = OpponentKnownHand()
        self._last_result_code: int | None = None
        self._last_turn: int | None = None
        self._last_terminal_observation = False
        self._last_player_unique_ids: tuple[int | None, int | None] | None = None
        self._local_player_unique_id: int | None = None
        self._played_history_lengths = [0, 0]
        self._played_history_turns: list[list[int]] = [[], []]
        self._training_initial_hands: list[list[int] | None] = [None, None]
        self._training_final_hands: list[list[int] | None] = [None, None]
        self._training_initial_self_cards_by_uid: dict[int, int] | None = None
        self._training_final_self_uids: set[int] | None = None
        self._training_self_selected_uids: set[int] = set()
        self._training_self_replaced: list[int] = []
        self._training_opponent_replaced_count: int | None = None
        self._training_opponent_mulligan_seen = False
        self._training_mulligan_events: list[dict[str, object]] = []
        self._training_seen_event_tokens: set[tuple[object, ...]] = set()
        self._event_play_history: list[list[dict[str, int]]] = [[], []]
        self._seen_play_event_tokens: set[tuple[object, ...]] = set()
        self._last_self_deck_count: int | None = None
        self._last_self_hand_size: int | None = None
        self._last_self_hand_uids: set[int] | None = None
        self._self_draw_history: list[dict[str, object]] = []
        self._seen_self_draw_event_tokens: set[tuple[object, ...]] = set()
        self._training_ui_event_history: list[list[dict[str, object]]] = [[], []]
        self._seen_training_ui_event_tokens: set[tuple[object, ...]] = set()
        # The UI can switch decks while the polling thread is emitting a
        # snapshot.  Keep recorder finalization and ingestion atomic so a
        # record can never contain half of two deck boundaries.
        self._training_lock = threading.RLock()
        self._training_recorder = TrainingMatchRecorder()
        self._training_match_finished = False
        self._training_terminal_pending = False
        self._training_match_id: str | None = None
        self._training_metadata: dict[str, object] = {}
        if self._selected_core_card_id is not None:
            self._training_metadata["self_core_card_id"] = self._selected_core_card_id
        self._training_upload = (
            TrainingUploadQueue(
                config.training_upload_queue_path or default_upload_queue_path(),
                endpoint=config.training_upload_url,
                enabled=config.training_upload_enabled,
                token=config.training_upload_token,
            )
            if config.training_upload_url or config.training_upload_queue_path
            else None
        )

    def set_selected_deck(
        self,
        deck: DeckInfoSnapshot | None,
        deck_key: str | None = None,
        core_card_id: int | None = None,
    ) -> None:
        """Switch the local ledger without interrupting battle-state polling."""
        selected_core = _optional_card_id(core_card_id)
        with self._deck_lock:
            same_deck = self._selected_deck == deck and self._selected_deck_key == deck_key
            if same_deck and self._selected_core_card_id == selected_core:
                return
            if same_deck:
                self._selected_core_card_id = selected_core
                with self._training_lock:
                    if selected_core is None:
                        self._training_metadata.pop("self_core_card_id", None)
                    else:
                        self._training_metadata["self_core_card_id"] = selected_core
                    self._training_recorder.update_metadata(self._training_metadata)
                return
        # A deck change is a new provenance boundary for training data.  Do
        # not let the tail of a game played with the old deck get attached to
        # the newly selected list.
        self._finish_training_match(complete=False)
        self._invalidate_class_rating_probe()
        with self._deck_lock:
            self._selected_deck = deck
            self._selected_deck_key = deck_key
            self._selected_core_card_id = selected_core
            self._ledger = DeckLedger(deck) if deck else None
            self._previous = None
            self._reset_match_observation_state()
        if self.on_deck:
            self.on_deck(deck.to_dict() if deck else {})
        if self.on_status:
            if deck:
                self.on_status(f"已切换牌组：{deck.deck_name}（{deck.total_cards} 张）")
            else:
                self.on_status("未选择本地牌组；对局状态仍会继续读取")

    def set_selected_core_card(self, core_card_id: int | None) -> None:
        """Update the selected deck's core without resetting a live match."""
        selected_core = _optional_card_id(core_card_id)
        with self._deck_lock:
            self._selected_core_card_id = selected_core
        with self._training_lock:
            if selected_core is None:
                self._training_metadata.pop("self_core_card_id", None)
            else:
                self._training_metadata["self_core_card_id"] = selected_core
            # The UI can recognize an opponent build after the last board
            # snapshot.  Updating the active record here makes sure that late
            # metadata is still present in the upload line.
            self._training_recorder.update_metadata(self._training_metadata)

    def set_training_metadata(self, metadata: Mapping[str, object] | None) -> None:
        """Replace UI-only match metadata and update the active record."""
        values = dict(metadata) if isinstance(metadata, Mapping) else {}
        with self._training_lock:
            # Keep the local core selected in TrackerConfig even if the UI only
            # sends opponent recognition fields in a later refresh.
            if self._selected_core_card_id is not None:
                values.setdefault("self_core_card_id", self._selected_core_card_id)
            self._training_metadata = values
            self._training_recorder.update_metadata(values)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="svwb-reader", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._invalidate_class_rating_probe()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None
        # Preserve a partially observed game as a replayable record when the
        # user closes the tracker or reconnects before a terminal response.
        self._finish_training_match(complete=False)
        if self._output_handle is not None:
            self._output_handle.close()
            self._output_handle = None
        if self._training_output_handle is not None:
            self._training_output_handle.close()
            self._training_output_handle = None

    @staticmethod
    def _archive_session_log(path: Path) -> bool:
        """Gzip one completed session-log segment without losing its source."""
        temporary: Path | None = None
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                return False
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            archive = path.with_name(f"{path.stem}.{stamp}{path.suffix}.gz")
            suffix = 1
            while archive.exists():
                archive = path.with_name(
                    f"{path.stem}.{stamp}-{suffix}{path.suffix}.gz"
                )
                suffix += 1
            temporary = archive.with_name(f"{archive.name}.{os.getpid()}.tmp")
            with path.open("rb") as source, gzip.open(temporary, "wb", compresslevel=6) as target:
                shutil.copyfileobj(source, target)
            os.replace(temporary, archive)
            path.unlink()
            return True
        except (OSError, ValueError):
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
            return False

    def _prepare_session_log(self, path: Path) -> None:
        """Start a fresh active log when the previous segment is over the cap."""
        limit = int(self.config.session_log_max_bytes or 0)
        if limit <= 0:
            return
        try:
            oversized = path.is_file() and path.stat().st_size > limit
        except OSError:
            oversized = False
        if oversized:
            self._archive_session_log(path)

    def _rotate_session_log(self) -> None:
        """Close, archive, and reopen the active session log."""
        path = self.config.output_path
        handle = self._output_handle
        if path is None or handle is None:
            return
        try:
            handle.flush()
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass
        self._output_handle = None
        if not self._archive_session_log(path):
            try:
                self._output_handle = path.open("a", encoding="utf-8", buffering=1)
                self._output_bytes = path.stat().st_size
            except OSError as exc:
                self._output_bytes = 0
                if self.on_error:
                    try:
                        self.on_error(exc)
                    except Exception:
                        pass
            return
        try:
            self._output_handle = path.open("a", encoding="utf-8", buffering=1)
            self._output_bytes = path.stat().st_size
        except OSError as exc:
            self._output_bytes = 0
            if self.on_error:
                try:
                    self.on_error(exc)
                except Exception:
                    pass

    def _write_session_record(self, record: dict[str, object]) -> None:
        """Write a compact line and rotate before it exceeds the configured cap."""
        if self._output_handle is None:
            return
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        payload_bytes = len(payload.encode("utf-8"))
        limit = int(self.config.session_log_max_bytes or 0)
        if (
            limit > 0
            and self._output_bytes > 0
            and self._output_bytes + payload_bytes > limit
        ):
            self._rotate_session_log()
        if self._output_handle is None:
            return
        try:
            self._output_handle.write(payload)
            self._output_handle.flush()
            self._output_bytes += payload_bytes
        except OSError as exc:  # pragma: no cover - filesystem dependent
            if self.on_error:
                try:
                    self.on_error(exc)
                except Exception:
                    pass

    @staticmethod
    def _snapshot_result(snapshot: dict[str, object]) -> str:
        root = snapshot.get("root")
        players = root.get("players") if isinstance(root, dict) else None
        if not isinstance(players, (list, tuple)) or len(players) < 2:
            return "结束"
        mine, opponent = players[0], players[1]
        if not isinstance(mine, dict) or not isinstance(opponent, dict):
            return "结束"
        result_code = mine.get("result_code") if isinstance(mine.get("result_code"), int) else 0
        return result_label(
            result_code,
            mine.get("life") if isinstance(mine.get("life"), int) else None,
            opponent.get("life") if isinstance(opponent.get("life"), int) else None,
        )

    @staticmethod
    def _root_snapshot_is_plausible(snapshot: dict[str, object]) -> bool:
        """Reject released BattleRoot objects whose bytes look terminal."""
        root = snapshot.get("root")
        players = root.get("players") if isinstance(root, dict) else None
        if not isinstance(players, (list, tuple)) or len(players) < 2:
            return False
        first_two = players[:2]
        if not all(isinstance(player, dict) for player in first_two):
            return False
        unique_ids = {player.get("unique_id") for player in first_two}
        if unique_ids != {1, 2}:
            return False
        for player in first_two:
            if not isinstance(player, dict):
                return False
            turn = player.get("turn")
            deck_count = player.get("deck_count")
            life = player.get("life")
            max_life = player.get("max_life")
            pp = player.get("pp")
            max_pp = player.get("max_pp")
            cemetery = player.get("cemetery_count")
            if not isinstance(turn, int) or not 0 <= turn <= 99:
                return False
            if not isinstance(deck_count, int) or not 0 <= deck_count <= 60:
                return False
            if not isinstance(life, int) or not -100 <= life <= 1000:
                return False
            if not isinstance(max_life, int) or not 1 <= max_life <= 1000:
                return False
            if not isinstance(pp, int) or not 0 <= pp <= 100:
                return False
            if not isinstance(max_pp, int) or not 0 <= max_pp <= 100:
                return False
            if not isinstance(cemetery, int) or not 0 <= cemetery <= 1000:
                return False
        return True

    def _finish_training_match(self, *, complete: bool | None = None) -> None:
        with self._training_lock:
            # A terminal result is held briefly while the result-page probe
            # looks for CR.  If the game boundary arrives first, it is still a
            # completed match rather than an interrupted partial record.
            if complete is False and self._training_terminal_pending:
                complete = True
            record = self._training_recorder.finish(complete=complete)
            if record is not None:
                self._training_terminal_pending = False
        if record is None:
            return
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        if self._training_output_handle is not None:
            try:
                self._training_output_handle.write(payload + "\n")
                self._training_output_handle.flush()
            except OSError:
                # A diagnostic log must never stop the read-only polling loop.
                pass
        if self._training_upload is not None:
            self._training_upload.enqueue(record)

    def _reset_match_observation_state(self, *, reset_ledger: bool = False) -> None:
        """Clear all per-match inference state at a deck/model boundary."""
        if reset_ledger:
            self._ledger = DeckLedger(self._selected_deck) if self._selected_deck else None
        self._opponent_known_hand.reset()
        self._self_action_tracker.reset()
        self._played_history_lengths = [0, 0]
        self._played_history_turns = [[], []]
        self._training_initial_hands = [None, None]
        self._training_final_hands = [None, None]
        self._training_initial_self_cards_by_uid = None
        self._training_final_self_uids = None
        self._training_self_selected_uids = set()
        self._training_self_replaced = []
        self._training_opponent_replaced_count = None
        self._training_opponent_mulligan_seen = False
        self._training_mulligan_events = []
        self._training_seen_event_tokens = set()
        self._event_play_history = [[], []]
        self._seen_play_event_tokens = set()
        self._last_turn = None
        self._last_result_code = None
        self._last_terminal_observation = False
        self._last_player_unique_ids = None
        self._local_player_unique_id = None
        self._last_self_deck_count = None
        self._last_self_hand_size = None
        self._last_self_hand_uids = None
        self._self_draw_history = []
        self._seen_self_draw_event_tokens = set()
        self._training_ui_event_history = [[], []]
        self._seen_training_ui_event_tokens = set()
        with self._training_lock:
            self._training_metadata = {}
            if self._selected_core_card_id is not None:
                self._training_metadata["self_core_card_id"] = self._selected_core_card_id
        self._root_only_next_model_probe_at = 0.0
        self._battle_result_model_address = 0
        self._class_rating_result = None
        self._class_rating_next_retry_at = 0.0
        self._training_match_finished = False
        self._training_terminal_pending = False
        self._training_match_id = None

    def _invalidate_class_rating_probe(self) -> None:
        """Discard a result probe when its deck/process context is obsolete."""
        with self._class_rating_probe_lock:
            self._class_rating_probe_generation += 1
            self._class_rating_probe_result = None
            self._class_rating_pending_snapshot = None
            self._class_rating_pending_snapshot_valid = False
            self._class_rating_pending_since = 0.0
            self._class_rating_ready_snapshot = None
            self._class_rating_probe_attempts = 0
            self._class_rating_next_retry_at = 0.0

    @staticmethod
    def _class_rating_snapshot_signature(snapshot: dict[str, object]) -> tuple[object, ...]:
        root = snapshot.get("root")
        players = root.get("players") if isinstance(root, dict) else ()
        values: list[object] = [snapshot.get("address"), snapshot.get("current_turn")]
        if isinstance(players, (list, tuple)):
            for player in players[:2]:
                if isinstance(player, dict):
                    values.extend(
                        (player.get("result_code"), player.get("life"), player.get("turn"))
                    )
        return tuple(values)

    def _remember_class_rating_snapshot(
        self,
        snapshot: dict[str, object],
        *,
        valid: bool,
    ) -> None:
        """Keep the terminal frame long enough for a late result-page read."""
        if self._snapshot_result(snapshot) not in {"胜利", "失败"}:
            return
        signature = self._class_rating_snapshot_signature(snapshot)
        with self._class_rating_probe_lock:
            previous = self._class_rating_pending_snapshot
            previous_signature = (
                self._class_rating_snapshot_signature(previous)
                if previous is not None
                else None
            )
            if previous_signature != signature:
                self._class_rating_probe_attempts = 0
                self._class_rating_next_retry_at = 0.0
            self._class_rating_pending_snapshot = dict(snapshot)
            self._class_rating_pending_snapshot_valid = valid
            self._class_rating_pending_since = time.monotonic()

    @staticmethod
    def _normalise_class_rating_result(value: object) -> dict[str, int] | None:
        if not isinstance(value, dict):
            return None
        try:
            before = int(value["before"])
            after = int(value["after"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0 <= before <= 100_000 and 0 <= after <= 100_000):
            return None
        return {"before": before, "after": after, "change": after - before}

    def _accept_class_rating_result(self, value: object) -> dict[str, int] | None:
        result = self._normalise_class_rating_result(value)
        if result is None:
            return None
        fingerprint = (result["before"], result["after"])
        with self._class_rating_probe_lock:
            # A released BattleResultModel often remains in memory after the
            # next game starts.  Do not attach that same result a second time;
            # let the bounded background retry wait for the new rating object.
            if self._class_rating_last_fingerprint == fingerprint:
                return None
            self._class_rating_last_fingerprint = fingerprint
        return result

    def _queue_pending_class_rating_update(
        self,
        result: dict[str, int],
        current_snapshot: dict[str, object] | None,
    ) -> None:
        with self._class_rating_probe_lock:
            pending = self._class_rating_pending_snapshot
            if pending is None:
                return
            pending = dict(pending)
            valid = self._class_rating_pending_snapshot_valid
            self._class_rating_pending_snapshot = None
            self._class_rating_pending_snapshot_valid = False
            self._class_rating_pending_since = 0.0
        if not valid:
            pending = self._build_class_rating_terminal_snapshot(pending, result)
        else:
            pending["class_rating"] = dict(result)
        # Keep the latest terminal frame available for the UI/history update.
        # A current terminal frame is emitted by the normal path as well; the
        # queued copy is what covers the model-release gap between frames.
        if current_snapshot is not None and pending.get("address") == current_snapshot.get("address"):
            # For a valid current terminal frame the direct snapshot already
            # carries the result.  Invalid root-only frames still need the
            # sanitised copy so they are deliberately queued below.
            if valid:
                return
        with self._class_rating_probe_lock:
            self._class_rating_ready_snapshot = pending

    def _build_class_rating_terminal_snapshot(
        self,
        source: dict[str, object],
        result: dict[str, int],
    ) -> dict[str, object]:
        """Create a minimal trustworthy terminal frame from a CR result.

        A released root can contain arbitrary bytes (including a fake life=0)
        after a ranked game.  The CR result is still authoritative, so use it
        to recover the outcome without persisting the corrupt player fields.
        """
        change = result.get("change", 0)
        result_code = 101 if change >= 0 else 106
        turn = source.get("current_turn") if isinstance(source.get("current_turn"), int) else 0
        with self._deck_lock:
            selected = self._selected_deck
            selected_key = self._selected_deck_key
            selected_core_card_id = self._selected_core_card_id
        self_class = selected.class_id if selected is not None else None
        deck_format = selected.deck_format if selected is not None else None
        deck = selected.to_dict() if selected is not None else None
        if isinstance(deck, dict) and selected_key:
            deck["deck_key"] = selected_key
        if isinstance(deck, dict) and selected_core_card_id is not None:
            deck["core_card_id"] = selected_core_card_id
        snapshot: dict[str, object] = {
            "address": source.get("address") or "rating-result",
            "self_class_id": self_class,
            "opponent_class_id": None,
            "deck_format": deck_format,
            "battle_mode": "ranked",
            "current_turn": turn,
            "class_rating": dict(result),
            "self_core_card_id": selected_core_card_id,
            "events": [],
            "legal_actions": None,
            "deck_mismatch": False,
            "class_mismatch": False,
            "format_mismatch": False,
            "root": {
                "address": source.get("address") or "rating-result",
                "is_ally_turn": False,
                "players": [
                    {
                        "unique_id": 1,
                        "result_code": result_code,
                        "life": 1,
                        "max_life": 1,
                        "turn": turn,
                        "is_first_side": None,
                        "hand": [],
                        "field": [],
                        "played_card_ids": [],
                        "destroyed_card_ids": [],
                    },
                    {
                        "unique_id": 2,
                        "result_code": 0,
                        "life": 1,
                        "max_life": 1,
                        "turn": turn,
                        "is_first_side": None,
                        "hand": [],
                        "field": [],
                        "played_card_ids": [],
                        "destroyed_card_ids": [],
                    },
                ],
            },
        }
        if isinstance(deck, dict):
            snapshot["deck"] = deck
        return snapshot

    def _flush_class_rating_update(self) -> None:
        with self._class_rating_probe_lock:
            snapshot = self._class_rating_ready_snapshot
            self._class_rating_ready_snapshot = None
        if snapshot is not None:
            self._emit(snapshot)

    def _schedule_class_rating_probe(self, profile: VersionProfile) -> None:
        """Run the expensive result-model scan away from the poller."""
        pid = self._pid
        if not pid:
            return
        with self._class_rating_probe_lock:
            now = time.monotonic()
            if (
                self._class_rating_probe_in_progress
                or self._class_rating_probe_result is not None
                or self._class_rating_probe_attempts >= 3
                or now < self._class_rating_next_retry_at
            ):
                return
            self._class_rating_probe_attempts += 1
            self._class_rating_probe_in_progress = True
            generation = self._class_rating_probe_generation
        module_name = profile.module_name
        runtime_names_only = profile.dynamic_discovery

        def probe() -> None:
            found: tuple[int, dict[str, int]] | None = None
            try:
                with ProcessReader(pid) as probe_reader:
                    found = find_battle_result_class_rating(
                        probe_reader,
                        module_name=module_name,
                        runtime_names_only=runtime_names_only,
                    )
            except Exception:
                # The result page may disappear while the heap is being
                # scanned.  A later bounded retry can catch the next object.
                found = None
            with self._class_rating_probe_lock:
                if generation == self._class_rating_probe_generation:
                    self._class_rating_probe_result = found
                    self._class_rating_next_retry_at = time.monotonic() + 0.75
                self._class_rating_probe_in_progress = False

        threading.Thread(
            target=probe,
            name="svwb-cr-probe",
            daemon=True,
        ).start()

    def _take_class_rating_probe_result(self) -> tuple[int, dict[str, int]] | None:
        with self._class_rating_probe_lock:
            found = self._class_rating_probe_result
            self._class_rating_probe_result = None
            return found

    def _attach_class_rating_result(
        self,
        reader: ProcessReader,
        profile: VersionProfile,
        snapshot: dict[str, object],
        *,
        snapshot_valid: bool = True,
    ) -> None:
        """Attach the post-match CR delta without slowing normal polling.

        The rating is returned by the result API and appears in a short-lived
        ``BattleResultModel`` object after the board's terminal result code.
        We therefore probe only terminal snapshots, cache the discovered model
        pointer, and retry at a modest interval while the result page is still
        being populated.
        """
        terminal = (
            snapshot.get("ownership_resolved", True) is not False
            and self._snapshot_result(snapshot) in {"胜利", "失败"}
        )
        result: dict[str, int] | None = None
        if self._class_rating_result is not None:
            result = dict(self._class_rating_result)
        elif terminal and self._battle_result_model_address:
            try:
                result = read_battle_result_class_rating(
                    reader,
                    self._battle_result_model_address,
                )
            except Exception:
                result = None
            if result is None:
                self._battle_result_model_address = 0

        # A background heap scan may finish between two board polls.  Consume
        # it here without ever making the live reader wait for the scan.
        if result is None:
            found = self._take_class_rating_probe_result()
            if found is not None:
                result = self._accept_class_rating_result(found[1])
                if result is not None:
                    self._battle_result_model_address = found[0]

        if result is not None:
            self._class_rating_result = dict(result)
            if terminal:
                snapshot["class_rating"] = dict(result)
            self._queue_pending_class_rating_update(result, snapshot if terminal else None)
            return

        if not terminal:
            return
        self._remember_class_rating_snapshot(snapshot, valid=snapshot_valid)
        self._schedule_class_rating_probe(profile)

    def _emit(self, snapshot: dict[str, object]) -> None:
        root = snapshot.get("root")
        if root is None:
            return
        with self._training_lock:
            training_metadata = dict(self._training_metadata)
        if training_metadata:
            # Keep this in the semantic envelope so a late opponent-deck
            # recognition or core change can refresh the recorder even when
            # the board itself is unchanged.
            snapshot["training_metadata"] = training_metadata
        semantic = without_addresses({
            "root": root,
            # LegalActions is not duplicated in the BattleRoot object.  It
            # changes when PP/EP, mode availability, attack targets, or
            # activation legality changes, so omitting it would suppress
            # meaningful replay checkpoints between otherwise identical
            # board snapshots.
            "legal_actions": snapshot.get("legal_actions"),
            "current_turn": snapshot.get("current_turn"),
            "self_class_id": snapshot.get("self_class_id"),
            "opponent_class_id": snapshot.get("opponent_class_id"),
            "deck_mismatch": snapshot.get("deck_mismatch"),
            "class_mismatch": snapshot.get("class_mismatch"),
            "format_mismatch": snapshot.get("format_mismatch"),
            "actual_self_class_id": snapshot.get("actual_self_class_id"),
            "actual_opponent_class_id": snapshot.get("actual_opponent_class_id"),
            "deck_ledger": snapshot.get("deck_ledger"),
            "opponent_hand_knowledge": snapshot.get("opponent_hand_knowledge"),
            "class_rating": snapshot.get("class_rating"),
            "training_observation": snapshot.get("training_observation"),
            "training_metadata": snapshot.get("training_metadata"),
        })
        if semantic == self._previous:
            return
        self._previous = semantic
        # Build the compact match stream before handing the snapshot to the UI
        # callback.  It is intentionally independent of the local match-history
        # checkbox: training collection is automatic and can be uploaded by an
        # explicitly configured endpoint.
        with self._training_lock:
            if (
                snapshot.get("ownership_resolved", True) is not False
                and not self._training_match_finished
            ):
                try:
                    self._training_recorder.ingest(snapshot)
                    if self._training_match_id is None:
                        self._training_match_id = self._training_recorder.match_id
                except Exception as exc:  # pragma: no cover - live decoder boundary
                    # Training capture is auxiliary.  A new response field or
                    # an unusual card should never prevent the dashboard from
                    # receiving the board snapshot.
                    if self.on_error:
                        try:
                            self.on_error(exc)
                        except Exception:
                            pass
            training_match_id = (
                self._training_match_id
                if snapshot.get("ownership_resolved", True) is not False
                else None
            )
        if training_match_id:
            # This is an opaque per-match ID, not a process address.  It lets
            # both UIs distinguish identical terminal states and lets the
            # optional session-log backfill update the right history row.
            snapshot["training_match_id"] = training_match_id
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pid": self._pid,
            "model": f"0x{self._model_address:016X}",
            "snapshot": snapshot,
        }
        if self._output_handle is not None:
            try:
                self._output_handle.write(
                    json.dumps(record, ensure_ascii=False, default=str) + "\n"
                )
                self._output_handle.flush()
            except OSError as exc:  # pragma: no cover - filesystem dependent
                if self.on_error:
                    try:
                        self.on_error(exc)
                    except Exception:
                        pass
        try:
            self.on_snapshot(snapshot)
        except Exception as exc:  # pragma: no cover - UI callback boundary
            # A rendering error must not terminate the memory reader.  The
            # next semantic snapshot can still refresh the UI once the view
            # has recovered, and the error remains visible in the status pill.
            if self.on_error:
                try:
                    self.on_error(exc)
                except Exception:
                    pass
        if (
            snapshot.get("ownership_resolved", True) is not False
            and self._snapshot_result(snapshot) in {"胜利", "失败"}
        ):
            with self._training_lock:
                if not self._training_match_finished:
                    rating = self._normalise_class_rating_result(snapshot.get("class_rating"))
                    if rating is not None:
                        self._finish_training_match(complete=True)
                        self._training_match_finished = True
                    else:
                        # Keep the recorder alive for the short interval in
                        # which BattleResultModel publishes the CR.  This
                        # avoids uploading a record that cannot later be
                        # amended when the result page arrives after the
                        # terminal board frame.
                        self._training_terminal_pending = True

    def _attach_deck_state(self, snapshot: dict[str, object]) -> None:
        # Keep the values straight from BattleInfo before applying the
        # selected-deck orientation fallback.  The fallback is useful when a
        # terminal response returns the two users in reverse order, but it
        # must never turn a stale UI selection into a fictitious local class.
        observed_self_class_id = snapshot.get("self_class_id")
        observed_opponent_class_id = snapshot.get("opponent_class_id")
        snapshot["observed_self_class_id"] = observed_self_class_id
        snapshot["observed_opponent_class_id"] = observed_opponent_class_id

        def _class_id(value: object) -> int | None:
            if isinstance(value, bool) or value is None:
                return None
            try:
                candidate = int(value)
            except (TypeError, ValueError):
                return None
            return candidate if 0 <= candidate <= 7 else None

        selected_class = _class_id(self._selected_deck.class_id) if self._selected_deck else None
        observed_self = _class_id(observed_self_class_id)
        observed_opponent = _class_id(observed_opponent_class_id)
        observed_classes = {value for value in (observed_self, observed_opponent) if value is not None}
        class_mismatch = (
            selected_class is not None
            and len(observed_classes) >= 1
            and selected_class not in observed_classes
        )
        selected_format = (
            int(self._selected_deck.deck_format)
            if self._selected_deck is not None
            and isinstance(self._selected_deck.deck_format, int)
            and self._selected_deck.deck_format in (1, 2)
            else None
        )
        observed_format = (
            int(snapshot.get("deck_format"))
            if isinstance(snapshot.get("deck_format"), int)
            and snapshot.get("deck_format") in (1, 2)
            else None
        )
        format_mismatch = (
            selected_format is not None
            and observed_format is not None
            and selected_format != observed_format
        )
        deck_mismatch = class_mismatch or format_mismatch
        snapshot["deck_mismatch"] = deck_mismatch
        snapshot["class_mismatch"] = class_mismatch
        snapshot["format_mismatch"] = format_mismatch
        if deck_mismatch:
            # BattleInfo's first user is the best local-side evidence when the
            # chosen deck is not one of the two observed classes.  Expose it
            # explicitly and leave the pair untouched below.
            if class_mismatch:
                snapshot["actual_self_class_id"] = observed_self
                snapshot["actual_opponent_class_id"] = observed_opponent
            else:
                actual_self, actual_opponent = orient_class_ids(
                    observed_self_class_id,
                    observed_opponent_class_id,
                    selected_class,
                )
                snapshot["actual_self_class_id"] = actual_self
                snapshot["actual_opponent_class_id"] = actual_opponent
            snapshot["expected_self_class_id"] = selected_class
            snapshot["actual_deck_format"] = observed_format
            snapshot["expected_deck_format"] = selected_format

        root = snapshot.get("root")
        ownership_resolved = True
        if isinstance(root, dict):
            # Keep adapters and replay fixtures that construct a root directly
            # consistent with the memory reader's public projection.  Server
            # unique IDs are not ownership markers; terminal result arrays and
            # side-tagged public events decide which entry is local.
            players = root.get("players")
            if self._last_terminal_observation and isinstance(players, (list, tuple)):
                raw_is_terminal = any(
                    isinstance(player, dict)
                    and (
                        isinstance(player.get("result_code"), int)
                        and player.get("result_code") in {101, 105, 106}
                        or (
                            isinstance(player.get("life"), int)
                            and player.get("life") <= 0
                        )
                    )
                    for player in players[:2]
                )
                if not raw_is_terminal:
                    # The next game's server IDs may be reused in the reverse
                    # order.  Never carry a prior match's cached ownership
                    # across the terminal-to-active boundary.
                    self._local_player_unique_id = None
            inferred_local_index = infer_local_player_index(
                players,
                events=snapshot.get("events"),
            )
            cached_local_index: int | None = None
            if inferred_local_index is None and self._local_player_unique_id is not None:
                matching_indexes = [
                    index
                    for index, player in enumerate(players[:2])
                    if isinstance(player, dict)
                    and player.get("unique_id") == self._local_player_unique_id
                ] if isinstance(players, (list, tuple)) else []
                if len(matching_indexes) == 1:
                    cached_local_index = matching_indexes[0]
            local_index = (
                inferred_local_index
                if inferred_local_index is not None
                else cached_local_index
            )
            if isinstance(snapshot.get("events"), (list, tuple)):
                ownership_resolved = (
                    local_index is not None
                )
            snapshot["ownership_resolved"] = ownership_resolved
            ordered_players = orient_player_order(
                players,
                self_class_id=snapshot.get("self_class_id"),
                opponent_class_id=snapshot.get("opponent_class_id"),
                expected_self_class_id=selected_class if not class_mismatch else None,
                events=snapshot.get("events"),
                local_player_index=local_index,
            )
            if ordered_players is not players:
                root["players"] = ordered_players
        mine: dict[str, object] | None = None
        if ownership_resolved and isinstance(root, dict):
            players = root.get("players")
            if isinstance(players, (list, tuple)) and players and isinstance(players[0], dict):
                mine = players[0]
        with self._deck_lock:
            selected_core_card_id = self._selected_core_card_id
            if mine is not None:
                turn = mine.get("turn")
                result_code = mine.get("result_code")
                players = root.get("players") if isinstance(root, dict) else ()
                opponent = (
                    players[1]
                    if isinstance(players, (list, tuple))
                    and len(players) >= 2
                    and isinstance(players[1], dict)
                    else {}
                )
                current_terminal = result_label(
                    result_code if isinstance(result_code, int) else 0,
                    mine.get("life") if isinstance(mine.get("life"), int) else None,
                    opponent.get("life") if isinstance(opponent.get("life"), int) else None,
                ) in {"胜利", "失败"}
                current_player_unique_ids = player_unique_ids(players)
                is_new_match = (
                    isinstance(turn, int)
                    and self._last_turn is not None
                    and turn + 2 < self._last_turn
                ) or (
                    isinstance(result_code, int)
                    and result_code == 0
                    and self._last_result_code not in (None, 0)
                ) or (
                    self._last_terminal_observation and not current_terminal
                ) or (
                    self._last_terminal_observation
                    and current_terminal
                    and current_player_unique_ids is not None
                    and self._last_player_unique_ids is not None
                    and all(value is not None for value in current_player_unique_ids)
                    and all(value is not None for value in self._last_player_unique_ids)
                    and current_player_unique_ids != self._last_player_unique_ids
                )
                if is_new_match:
                    # Finalize an unfinished previous game before the first
                    # snapshot of the new game is attached to the recorder.
                    self._finish_training_match(complete=False)
                    self._reset_match_observation_state(reset_ledger=True)
                self._last_turn = turn if isinstance(turn, int) else self._last_turn
                self._last_result_code = (
                    result_code if isinstance(result_code, int) else self._last_result_code
                )
                self._last_terminal_observation = current_terminal
                self._last_player_unique_ids = current_player_unique_ids
                raw_local_unique_id = mine.get("unique_id")
                if isinstance(raw_local_unique_id, int) and raw_local_unique_id > 0:
                    self._local_player_unique_id = raw_local_unique_id
            if self._selected_deck is not None and not class_mismatch:
                # BattleInfo's two user entries can be returned in the
                # opposite order from the local public player on some
                # terminal snapshots.  Normalize the semantic fields before
                # the UI, history, opponent matcher, and training recorder
                # consume this snapshot.
                self_class_id, opponent_class_id = orient_class_ids(
                    snapshot.get("self_class_id"),
                    snapshot.get("opponent_class_id"),
                    self._selected_deck.class_id,
                )
                snapshot["self_class_id"] = self_class_id
                snapshot["opponent_class_id"] = opponent_class_id
                deck_info = self._selected_deck.to_dict()
                if self._selected_deck_key:
                    deck_info["deck_key"] = self._selected_deck_key
                if selected_core_card_id is not None:
                    deck_info["core_card_id"] = selected_core_card_id
                snapshot["deck"] = deck_info
                if selected_core_card_id is not None:
                    snapshot["self_core_card_id"] = selected_core_card_id
            elif self._selected_deck is not None:
                # Keep the selected deck metadata for the UI/ledger, while
                # preserving the observed classes for mismatch handling.
                deck_info = self._selected_deck.to_dict()
                if self._selected_deck_key:
                    deck_info["deck_key"] = self._selected_deck_key
                if selected_core_card_id is not None:
                    deck_info["core_card_id"] = selected_core_card_id
                snapshot["deck"] = deck_info
                if selected_core_card_id is not None:
                    snapshot["self_core_card_id"] = selected_core_card_id
            if self._ledger is not None and ownership_resolved:
                snapshot["deck_ledger"] = self._ledger.update(snapshot)
            if ownership_resolved and isinstance(root, dict):
                players = root.get("players")
                if (
                    isinstance(players, (list, tuple))
                    and len(players) >= 2
                    and isinstance(players[1], dict)
                ):
                    for index, player in enumerate(players[:2]):
                        if not isinstance(player, dict):
                            continue
                        history = player.get("played_card_ids", ())
                        items = list(history) if isinstance(history, (list, tuple)) else []
                        if len(items) < self._played_history_lengths[index]:
                            self._played_history_lengths[index] = 0
                            self._played_history_turns[index] = []
                        turn = player.get("turn")
                        if not isinstance(turn, int) or turn <= 0:
                            turn = snapshot.get("current_turn", 0)
                        for _item in items[self._played_history_lengths[index]:]:
                            self._played_history_turns[index].append(turn if isinstance(turn, int) else 0)
                        self._played_history_lengths[index] = len(items)
                        player["_played_card_turns"] = list(self._played_history_turns[index])
                    if isinstance(players[0], dict) and isinstance(players[0].get("turn"), int):
                        snapshot["current_turn"] = players[0]["turn"]
                    self._capture_public_play_events(snapshot, players)
                    self._self_action_tracker.update(snapshot, players[0])
                    mine_actions = self._self_action_tracker.to_training_dict().get("recent_actions", [])
                    players[0]["_recent_actions"] = mine_actions
                    self._opponent_known_hand.update(snapshot, players[1])
                    snapshot["opponent_hand_knowledge"] = self._opponent_known_hand.to_training_dict()
                    players[1]["opponent_hand_knowledge"] = snapshot["opponent_hand_knowledge"]
                    self._update_training_observation(snapshot, players)
                    self._capture_self_draws_and_burns(snapshot, players[0])
                    if self._ledger is not None:
                        snapshot["deck_ledger"] = self._ledger.to_dict()

    def _capture_self_draws_and_burns(self, snapshot: dict[str, object], mine: dict[str, object]) -> None:
        hand = mine.get("hand")
        hand_size = len(hand) if isinstance(hand, (list, tuple)) else None
        deck_count = mine.get("deck_count")
        turn = mine.get("turn")
        if not isinstance(turn, int) or turn <= 0:
            turn = snapshot.get("current_turn", 0)
        current_cards = {
            int(card["unique_id"]): card
            for card in hand if isinstance(card, dict) and isinstance(card.get("unique_id"), int) and int(card["unique_id"]) > 0
        } if isinstance(hand, (list, tuple)) else {}
        current_uids = set(current_cards)
        deck_drop = (
            self._last_self_deck_count - deck_count
            if isinstance(deck_count, int)
            and isinstance(self._last_self_deck_count, int)
            and deck_count < self._last_self_deck_count
            else 0
        )
        if deck_drop:
            draws = deck_drop
            gained = max(0, (hand_size or 0) - (self._last_self_hand_size or 0))
            burned = max(0, draws - gained) if (self._last_self_hand_size or 0) >= 9 else 0

            # The latest BattleEvents ReactiveProperty retains the public draw
            # response even after the short-lived current-response list is
            # cleared.  A card absent from a full hand is the overdrawn card.
            named_burns = 0
            burned_card_ids: list[int] = []
            recorded_draw_uids: set[int] = set()
            events = snapshot.get("events", ())
            if isinstance(events, (list, tuple)):
                for event in events:
                    if (
                        not isinstance(event, dict)
                        or not event.get("is_ally")
                        or event.get("type") not in {"BattleResponseDrawOpen", "BattleResponseDrawOpenWithEffect"}
                    ):
                        continue
                    cards = event.get("cards", ())
                    if not isinstance(cards, (list, tuple)):
                        continue
                    for card in cards:
                        if not isinstance(card, dict):
                            continue
                        uid = card.get("unique_id")
                        card_id = card.get("base_card_id") or card.get("card_id")
                        if not isinstance(uid, int) or not isinstance(card_id, int) or card_id <= 0:
                            continue
                        # Response objects may have a new managed address on
                        # the next poll; sequence + UID remains stable.
                        token = (event.get("sequence"), uid)
                        if token in self._seen_self_draw_event_tokens:
                            continue
                        if uid not in current_uids and named_burns < burned:
                            self._seen_self_draw_event_tokens.add(token)
                            named_burns += 1
                            burned_card_ids.append(canonical_card_id(card_id))
                            self._self_draw_history.append({
                                "turn": turn,
                                "kind": "爆牌",
                                "card_id": canonical_card_id(card_id),
                                "count": 1,
                            })
                        elif uid in current_uids:
                            self._seen_self_draw_event_tokens.add(token)
                            # Cards inserted by the opening redraw belong to
                            # the final four-card baseline, not to T1 draws.
                            if not (
                                turn <= 1
                                and self._training_final_self_uids is not None
                                and uid in self._training_final_self_uids
                            ):
                                recorded_draw_uids.add(uid)
                                self._self_draw_history.append({
                                    "turn": turn,
                                    "kind": "抽取",
                                    "card_id": canonical_card_id(card_id),
                                })
            if burned > named_burns:
                self._self_draw_history.append({"turn": turn, "kind": "爆牌", "count": burned - named_burns})
            if burned and self._ledger is not None:
                self._ledger.record_burn(burned, tuple(burned_card_ids))

            # Fall back to hand-UID differences when a public draw response is
            # genuinely unavailable.  On T1, subtract the post-mulligan four
            # card baseline before doing that comparison.
            if self._last_self_hand_uids is not None:
                new_uids = current_uids - self._last_self_hand_uids
                if turn <= 1 and self._training_final_self_uids is not None:
                    new_uids -= self._training_final_self_uids
                remaining_draws = max(0, draws - burned - len(recorded_draw_uids))
                for uid in list(new_uids - recorded_draw_uids)[:remaining_draws]:
                    card = current_cards.get(uid)
                    card_id = card.get("base_card_id") or card.get("card_id") if isinstance(card, dict) else None
                    if isinstance(card_id, int) and card_id > 0:
                        self._self_draw_history.append({
                            "turn": turn,
                            "kind": "抽取",
                            "card_id": canonical_card_id(card_id),
                        })
        if isinstance(deck_count, int):
            self._last_self_deck_count = deck_count
        if isinstance(hand_size, int):
            self._last_self_hand_size = hand_size
        self._last_self_hand_uids = current_uids
        mine["_draw_history"] = list(self._self_draw_history)

    @staticmethod
    def _hand_ids(player: dict[str, object]) -> list[int]:
        hand = player.get("hand")
        if not isinstance(hand, (list, tuple)):
            return []
        values: list[int] = []
        for card in hand:
            if not isinstance(card, dict):
                continue
            value = card.get("base_card_id") or card.get("card_id")
            if isinstance(value, int) and value > 0:
                values.append(canonical_card_id(value))
        return values

    def _update_training_observation(self, snapshot: dict[str, object], players: list[object] | tuple[object, ...]) -> None:
        player_dicts = [player for player in players[:2] if isinstance(player, dict)]
        if len(player_dicts) < 2:
            return
        mine_turn = player_dicts[0].get("turn")
        mine_hand = player_dicts[0].get("hand")
        mine_cards = [card for card in mine_hand if isinstance(card, dict)] if isinstance(mine_hand, (list, tuple)) else []
        current_mine = self._hand_ids(player_dicts[0])
        current_uids = {
            int(card["unique_id"])
            for card in mine_cards
            if isinstance(card.get("unique_id"), int) and int(card["unique_id"]) > 0
        }
        if (
            self._training_initial_hands[0] is None
            and mine_turn == 0
            and len(current_mine) == 4
            and len(current_uids) == 4
        ):
            self._training_initial_hands[0] = list(current_mine)
            self._training_initial_self_cards_by_uid = {
                int(card["unique_id"]): canonical_card_id(int(card.get("base_card_id") or card.get("card_id")))
                for card in mine_cards
                if isinstance(card.get("unique_id"), int)
                and isinstance(card.get("base_card_id") or card.get("card_id"), int)
            }
        events = snapshot.get("events", ())
        if isinstance(events, (list, tuple)):
            for event in events:
                if (
                    not isinstance(event, dict)
                    or event.get("type") not in {"BattleResponseMulligan", "BattleModelMulliganSelection"}
                ):
                    continue
                # The game commonly gives both mulligan responses sequence 0.
                # Sequence-only de-duplication therefore discarded one side.
                fingerprint_token = event.get("selection_fingerprint")
                if isinstance(fingerprint_token, list):
                    fingerprint_token = tuple(fingerprint_token)
                # The response address is not stable while the game swaps
                # animation objects between polls. Use semantic fields so a
                # repeated snapshot cannot append another mulligan action.
                token = (
                    event.get("type"), event.get("sequence"),
                    event.get("is_ally"), event.get("change_card_flags"), fingerprint_token,
                )
                if token in self._training_seen_event_tokens:
                    continue
                self._training_seen_event_tokens.add(token)
                explicit_count = event.get("replaced_count")
                changed = event.get("change_card_flags")
                if isinstance(explicit_count, int) and 0 <= explicit_count <= 4:
                    count = explicit_count
                elif isinstance(changed, int) and changed:
                    count = bin(int(changed) & 0xF).count("1")
                else:
                    draw_num = event.get("draw_num")
                    count = int(draw_num) if isinstance(draw_num, int) and 0 < draw_num <= 4 else 0
                is_ally = bool(event.get("is_ally"))
                item = {"side": "self" if is_ally else "opponent", "replaced_count": count}
                self._training_mulligan_events.append(item)
                if is_ally:
                    fingerprint = event.get("selection_fingerprint")
                    if isinstance(fingerprint, (list, tuple)):
                        self._training_self_selected_uids = {
                            int(value) for value in fingerprint if isinstance(value, int) and value > 0
                        }
                else:
                    self._training_opponent_mulligan_seen = True
                    self._training_opponent_replaced_count = count
                if not is_ally and self._training_final_hands[1] is None:
                    self._training_final_hands[1] = self._hand_ids(player_dicts[1])

        def finalize_self_opening(cards: list[dict[str, object]]) -> None:
            if len(cards) != 4:
                return
            final_ids: list[int] = []
            final_uids: set[int] = set()
            for card in cards:
                uid = card.get("unique_id")
                card_id = card.get("base_card_id") or card.get("card_id")
                if not isinstance(uid, int) or uid <= 0 or not isinstance(card_id, int) or card_id <= 0:
                    return
                final_uids.add(uid)
                final_ids.append(canonical_card_id(card_id))
            self._training_final_self_uids = final_uids
            self._training_final_hands[0] = final_ids
            initial_by_uid = self._training_initial_self_cards_by_uid or {}
            replaced = [
                card_id
                for uid, card_id in initial_by_uid.items()
                if uid in self._training_self_selected_uids
            ]
            if not replaced and self._training_self_selected_uids:
                # Defensive fallback for snapshots that lacked UIDs in the
                # first opening-hand read.
                remaining = list(final_ids)
                for card_id in self._training_initial_hands[0] or []:
                    if card_id in remaining:
                        remaining.remove(card_id)
                    else:
                        replaced.append(card_id)
            self._training_self_replaced = replaced

        if self._training_initial_hands[0] is not None and self._training_final_hands[0] is None:
            if mine_turn == 0 and len(mine_cards) == 4:
                initial_uids = set(self._training_initial_self_cards_by_uid or {})
                selection_finished = (
                    not self._training_self_selected_uids
                    or self._training_self_selected_uids.isdisjoint(current_uids)
                )
                if current_uids != initial_uids and selection_finished:
                    finalize_self_opening(mine_cards)
            elif isinstance(mine_turn, int) and mine_turn >= 1 and len(mine_cards) >= 5:
                turn_draw_uids: set[int] = set()
                if isinstance(events, (list, tuple)):
                    for event in events:
                        if (
                            isinstance(event, dict)
                            and event.get("is_ally")
                            and event.get("type") in {"BattleResponseDrawOpen", "BattleResponseDrawOpenWithEffect"}
                            and event.get("is_turn_start_draw")
                        ):
                            cards = event.get("cards", ())
                            if isinstance(cards, (list, tuple)):
                                turn_draw_uids.update(
                                    int(card["unique_id"])
                                    for card in cards
                                    if isinstance(card, dict) and isinstance(card.get("unique_id"), int)
                                )
                opening_cards = [card for card in mine_cards if card.get("unique_id") not in turn_draw_uids]
                if len(opening_cards) != 4 and mine_turn == 1:
                    # Hand order retains the four redraw results and appends the
                    # ordinary first-turn draw.  This recovers cleanly even if
                    # the tracker attached after the draw response was cleared.
                    opening_cards = mine_cards[:4]
                finalize_self_opening(opening_cards)
        player_dicts[0]["mulligan_summary"] = {
            "initial_hand": self._training_initial_hands[0] or [],
            "replaced_cards": self._training_self_replaced,
            "final_hand": self._training_final_hands[0] or (current_mine if mine_turn == 0 else []),
        }
        player_dicts[1]["mulligan_summary"] = {
            "replaced_count": self._training_opponent_replaced_count if self._training_opponent_mulligan_seen else None,
        }
        snapshot["training_observation"] = self._build_training_observation(snapshot, player_dicts)

    def _capture_public_play_events(self, snapshot: dict[str, object], players: list[object] | tuple[object, ...]) -> None:
        """Keep public play responses for the recent-record panel.

        The model's permanent play-history list is sometimes populated only
        after an animation has completed.  The public response is available at
        the moment the card is used, so retaining it prevents the UI from
        missing a card in that interval.
        """
        events = snapshot.get("events")
        if not isinstance(events, (list, tuple)):
            return
        turn = snapshot.get("current_turn")
        if not isinstance(turn, int) or turn <= 0:
            turn = 0
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "")
            if event_type == "BattleResponsePlayOpen":
                card_id = event.get("card_id")
                if isinstance(card_id, int) and card_id > 0:
                    token = (event.get("sequence"), card_id, event.get("is_ally"))
                    if token not in self._seen_play_event_tokens:
                        self._seen_play_event_tokens.add(token)
                        side = 0 if bool(event.get("is_ally")) else 1
                        self._event_play_history[side].append({"turn": turn, "card_id": canonical_card_id(card_id)})

            # Keep a bounded, side-specific copy for the human recent-record
            # panel.  The authoritative unbounded-per-match copy is written by
            # TrainingMatchRecorder; this one is only a rendering convenience.
            fingerprint = json.dumps(
                without_addresses(event),
                ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"),
            )
            token = (event_type, event.get("sequence"), fingerprint)
            if token in self._seen_training_ui_event_tokens:
                continue
            self._seen_training_ui_event_tokens.add(token)
            compact = compact_event_records(event, turn)
            for item in compact:
                side = item.get("s") if isinstance(item.get("s"), int) else -1
                if side not in (0, 1):
                    continue
                self._training_ui_event_history[side].append(item)
                # Prevent an unusually noisy response stream from growing the
                # UI snapshot indefinitely while retaining enough context for
                # recent history.
                del self._training_ui_event_history[side][:-120]
        for index, player in enumerate(players[:2]):
            if isinstance(player, dict):
                player["_event_played_cards"] = list(self._event_play_history[index])
                player["_training_events"] = list(self._training_ui_event_history[index])

    def _build_training_observation(self, snapshot: dict[str, object], players: list[dict[str, object]]) -> dict[str, object]:
        mine, opponent = players[0], players[1]
        result_code = mine.get("result_code") if isinstance(mine.get("result_code"), int) else 0
        result = result_label(result_code, mine.get("life") if isinstance(mine.get("life"), int) else None, opponent.get("life") if isinstance(opponent.get("life"), int) else None)
        return {
            "schema_version": 1,
            "turn": mine.get("turn"),
            "self_class_id": snapshot.get("self_class_id"),
            "opponent_class_id": snapshot.get("opponent_class_id"),
            "is_first": mine.get("is_first_side"),
            "result": result,
            "result_code": result_code,
            "mulligan": {
                "self_initial_hand": self._training_initial_hands[0] or [],
                "self_replaced_cards": self._training_self_replaced,
                "self_final_starting_hand": self._training_final_hands[0] or (
                    self._hand_ids(mine) if mine.get("turn") == 0 else []
                ),
                "opponent_replaced_count": self._training_opponent_replaced_count,
                "events": list(self._training_mulligan_events),
            },
            "played_card_turns": {
                "self": [
                    {"turn": turn, "card_id": item[0] if isinstance(item, (list, tuple)) and item else item}
                    for turn, item in zip(self._played_history_turns[0], list(mine.get("played_card_ids", ())))
                ],
                "opponent": [
                    {"turn": turn, "card_id": item[0] if isinstance(item, (list, tuple)) and item else item}
                    for turn, item in zip(self._played_history_turns[1], list(opponent.get("played_card_ids", ())))
                ],
            },
            "recent_history": {
                "self_played": [item[0] if isinstance(item, (list, tuple)) and item else item for item in mine.get("played_card_ids", ())] if isinstance(mine.get("played_card_ids"), (list, tuple)) else [],
                "opponent_played": [item[0] if isinstance(item, (list, tuple)) and item else item for item in opponent.get("played_card_ids", ())] if isinstance(opponent.get("played_card_ids"), (list, tuple)) else [],
                "self_destroyed": [item[0] if isinstance(item, (list, tuple)) and item else item for item in mine.get("destroyed_card_ids", ())] if isinstance(mine.get("destroyed_card_ids"), (list, tuple)) else [],
                "opponent_destroyed": [item[0] if isinstance(item, (list, tuple)) and item else item for item in opponent.get("destroyed_card_ids", ())] if isinstance(opponent.get("destroyed_card_ids"), (list, tuple)) else [],
                "opponent_evolutions": (snapshot.get("opponent_hand_knowledge") or {}).get("recent_evolution_events", []) if isinstance(snapshot.get("opponent_hand_knowledge"), dict) else [],
            },
            "events": [
                *self._training_ui_event_history[0],
                *self._training_ui_event_history[1],
            ],
            "opponent_hand_knowledge": snapshot.get("opponent_hand_knowledge"),
        }

    def _open_supported_reader(self) -> tuple[ProcessReader, VersionProfile, ProcessInfo]:
        """Open a running build and select the matching hash-verified profile.

        The China client and the Steam client may be installed side by side,
        and the China launcher can leave both a wrapper and a Unity player
        process visible to the OS.  Trying each configured name until its
        GameAssembly profile verifies avoids attaching to an unrelated
        process merely because it happens to be listed first.
        """
        if self.config.pid:
            info = ProcessInfo(self.config.pid, self.config.process_name)
            reader = ProcessReader(info.pid)
            try:
                return reader, verify_process_version(reader), info
            except Exception:
                reader.close()
                raise

        candidates = find_process_candidates(self.config.process_candidates)
        failures: list[str] = []
        for info in candidates:
            reader: ProcessReader | None = None
            try:
                reader = ProcessReader(info.pid)
                profile = verify_process_version(reader)
                return reader, profile, info
            except Exception as exc:
                if reader is not None:
                    reader.close()
                failures.append(f"{info.name} (PID {info.pid})：{exc}")
        detail = "；".join(failures)
        raise RuntimeError(f"未找到可读取的支持版本进程{('：' + detail) if detail else ''}")

    def _run(self) -> None:
        # Diagnostic logs are optional.  A protected working directory or a
        # transient file lock must not abort the reader thread before it has
        # even attempted to attach to the game (which otherwise looks like a
        # tracker failure with no useful status in the UI).
        self._output_handle = None
        self._training_output_handle = None
        for attribute, path in (
            ("_output_handle", self.config.output_path),
            ("_training_output_handle", self.config.training_output_path),
        ):
            if path is None:
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open("a", encoding="utf-8", buffering=1)
            except OSError as exc:
                if self.on_error:
                    self.on_error(exc)
                continue
            setattr(self, attribute, handle)
        # Retry queued records once per reader start.  A failed request leaves
        # the queue untouched and never blocks game-state polling for long.
        if self._training_upload is not None:
            self._training_upload.flush()
        while not self._stop.is_set():
            try:
                reader, profile, process = self._open_supported_reader()
                self._pid = process.pid
                with reader:
                    if self.on_status:
                        build_label = (
                            "国服"
                            if process.name.casefold()
                            in {
                                "mumu模拟器x影之诗高清版.exe",
                                "mumu模拟器x影之诗高清版.o",
                            }
                            else "Steam"
                        )
                        if profile.auto_compatible:
                            self.on_status(
                                f"已连接{build_label}进程 {process.name}；检测到游戏小版本更新，"
                                f"{profile.game_version}，核心结构校验通过"
                            )
                        else:
                            self.on_status(
                                f"已连接{build_label}进程 {process.name}；版本 {profile.game_version} 校验通过"
                            )
                    consecutive_errors = 0
                    # The China client has no stable presentation-layer
                    # BattleViewServerData pointer.  Do not enter the
                    # fallback all-memory scan from the polling thread; its
                    # BattleRootMpo contains the legality projections needed
                    # by the tracker and is decoded during each snapshot.
                    if profile.dynamic_discovery:
                        self._server_data_discovery_attempted = True
                    while not self._stop.is_set():
                        # A result-model heap scan may have completed while the
                        # game was transitioning between BattleModel objects.
                        # Publish its late CR update before attempting another
                        # potentially expensive discovery pass.
                        self._flush_class_rating_update()
                        if self._model_address <= 0 and not self._root_only_mode:
                            if self.on_status:
                                self.on_status("正在自动寻找对局对象…")
                            models = find_battle_models(
                                reader,
                                class_pointer_rva=(
                                    profile.battle_model_class_pointer_rva or None
                                ),
                                module_name=profile.module_name,
                                runtime_names_only=profile.dynamic_discovery,
                            )
                            if not models:
                                # Puzzle/teaching battles expose a valid
                                # BattleRootMpo through BattlePuzzleModel but
                                # do not create the normal BattleModel object.
                                # Fall back to the shared root so the UI can
                                # still display a trustworthy board snapshot.
                                roots = find_battle_roots(
                                    reader,
                                    module_name=profile.module_name,
                                    runtime_names_only=profile.dynamic_discovery,
                                )
                                if roots:
                                    # A newly discovered root may belong to a
                                    # different game after the process or
                                    # BattleModel was recreated.  Finalize any
                                    # partial record before changing the
                                    # connection identity so events can never
                                    # leak across matches.
                                    if self._training_recorder.active:
                                        self._finish_training_match(complete=False)
                                    self._battle_root_address = roots[-1]
                                    self._root_only_mode = True
                                    self._previous = None
                                    with self._deck_lock:
                                        self._reset_match_observation_state(reset_ledger=True)
                                    self._root_only_next_model_probe_at = time.monotonic() + 0.5
                                    if self.on_status:
                                        self.on_status(
                                            f"已连接解密/教学对局根对象 0x{self._battle_root_address:X}"
                                        )
                                else:
                                    if self.on_status:
                                        self.on_status("尚未进入对局，等待后自动重试")
                                    self._stop.wait(2.0)
                                    continue
                            else:
                                if self._training_recorder.active:
                                    self._finish_training_match(complete=False)
                                self._model_address = models[-1]
                                self._battle_root_address = 0
                                self._root_only_mode = False
                                self._root_only_next_model_probe_at = 0.0
                                self._battle_view_server_data_address = 0
                                self._server_data_discovery_attempted = profile.dynamic_discovery
                                self._server_data_next_retry_at = 0.0
                                self._previous = None
                                with self._deck_lock:
                                    self._reset_match_observation_state(reset_ledger=True)
                                if self.on_status:
                                    self.on_status(f"已自动连接 0x{self._model_address:X}")
                        try:
                            if self._root_only_mode:
                                # The root-only fallback is also used while a
                                # result screen is alive.  A new online game
                                # creates a regular BattleModel afterwards;
                                # Probe discovery only before the first turn or
                                # after a terminal/reset root.  A full private
                                # heap scan can take seconds on a live client;
                                # doing it every 1.5 seconds used to starve the
                                # root reader immediately after the first play.
                                # Active root-only games must be polled first so
                                # their turn/PP/field state remains live.
                                snapshot = read_battle_root_snapshot(
                                    reader,
                                    self._battle_root_address,
                                    reveal_opponent_hand=self.config.reveal_opponent_hand,
                                )
                                snapshot_valid = self._root_snapshot_is_plausible(snapshot)
                                if not snapshot_valid:
                                    # A released root can contain arbitrary
                                    # bytes that look like a terminal life=0.
                                    # Keep it only as CR context; never emit it
                                    # to the UI/history as a real game.
                                    self._attach_class_rating_result(
                                        reader,
                                        profile,
                                        snapshot,
                                        snapshot_valid=False,
                                    )
                                    self._flush_class_rating_update()
                                    consecutive_errors = 0
                                else:
                                    self._attach_deck_state(snapshot)
                                    self._attach_class_rating_result(reader, profile, snapshot)
                                    self._flush_class_rating_update()
                                    self._emit(snapshot)
                                root = snapshot.get("root")
                                players = root.get("players") if isinstance(root, dict) else ()
                                turn_values = [
                                    player.get("turn")
                                    for player in players[:2]
                                    if isinstance(player, dict)
                                ] if isinstance(players, (list, tuple)) else []
                                current_turn = snapshot.get("current_turn")
                                if not snapshot_valid:
                                    current_turn = 0
                                elif not isinstance(current_turn, int):
                                    current_turn = max(
                                        (value for value in turn_values if isinstance(value, int)),
                                        default=0,
                                    )
                                terminal = (
                                    not snapshot_valid
                                    or self._snapshot_result(snapshot) in {"胜利", "失败"}
                                )
                                probe_allowed = (
                                    terminal
                                    or current_turn <= 0
                                )
                                if probe_allowed and time.monotonic() >= self._root_only_next_model_probe_at:
                                    self._root_only_next_model_probe_at = time.monotonic() + 1.5
                                    try:
                                        models = find_battle_models(
                                            reader,
                                            class_pointer_rva=(
                                                profile.battle_model_class_pointer_rva or None
                                            ),
                                            module_name=profile.module_name,
                                            runtime_names_only=profile.dynamic_discovery,
                                        )
                                    except (OSError, ValueError, LookupError):
                                        models = ()
                                    if models:
                                        if self._training_recorder.active:
                                            self._finish_training_match(complete=False)
                                        self._model_address = models[-1]
                                        self._battle_root_address = 0
                                        self._root_only_mode = False
                                        self._root_only_next_model_probe_at = 0.0
                                        self._battle_view_server_data_address = 0
                                        self._server_data_discovery_attempted = profile.dynamic_discovery
                                        self._server_data_next_retry_at = 0.0
                                        self._previous = None
                                        with self._deck_lock:
                                            self._reset_match_observation_state(reset_ledger=True)
                                        if self.on_status:
                                            self.on_status(f"已自动连接 0x{self._model_address:X}")
                                consecutive_errors = 0
                                if self._root_only_mode:
                                    self._stop.wait(self.config.interval)
                                    continue
                                # A regular BattleModel was found during the
                                # probe; fall through and publish its first
                                # full snapshot without waiting another poll.
                            if (
                                not profile.dynamic_discovery
                                and
                                not self._server_data_discovery_attempted
                                and time.monotonic() >= self._server_data_next_retry_at
                            ):
                                initial = read_battle_model(reader, self._model_address)
                                root = initial.get("root")
                                players = root.get("players") if isinstance(root, dict) else None
                                player_addresses: tuple[int, int] | None = None
                                if isinstance(players, (list, tuple)) and len(players) == 2:
                                    raw_addresses = [
                                        player.get("address") if isinstance(player, dict) else None
                                        for player in players
                                    ]
                                    if all(isinstance(value, str) for value in raw_addresses):
                                        player_addresses = (
                                            int(raw_addresses[0], 16),
                                            int(raw_addresses[1], 16),
                                        )
                                server_data = find_battle_view_server_data(
                                    reader,
                                    module_name=profile.module_name,
                                    runtime_names_only=profile.dynamic_discovery,
                                    expected_player_addresses=player_addresses,
                                )
                                if server_data:
                                    self._battle_view_server_data_address = server_data[-1]
                                    self._server_data_discovery_attempted = True
                                else:
                                    self._server_data_next_retry_at = time.monotonic() + 2.0
                            snapshot = read_battle_model(
                                reader,
                                self._model_address,
                                reveal_opponent_hand=self.config.reveal_opponent_hand,
                                battle_view_server_data_address=(
                                    self._battle_view_server_data_address or None
                                ),
                                read_root_legal_actions=profile.dynamic_discovery,
                            )
                            self._attach_deck_state(snapshot)
                            self._attach_class_rating_result(reader, profile, snapshot)
                            self._emit(snapshot)
                            consecutive_errors = 0
                        except (OSError, ValueError, LookupError, IndexError, TypeError) as exc:
                            consecutive_errors += 1
                            if self.on_error:
                                try:
                                    self.on_error(exc)
                                except Exception:
                                    pass
                            if self.config.model_address <= 0 and consecutive_errors >= 4:
                                self._model_address = 0
                                self._battle_root_address = 0
                                self._root_only_mode = False
                                self._root_only_next_model_probe_at = 0.0
                                self._battle_view_server_data_address = 0
                                self._server_data_discovery_attempted = profile.dynamic_discovery
                                self._server_data_next_retry_at = 0.0
                                consecutive_errors = 0
                        except Exception as exc:  # pragma: no cover - live client boundary
                            # Unknown DTOs and transient managed-object races
                            # are expected when the game updates its scene.  Do
                            # not let one decoder edge case kill the polling
                            # loop; after a few failures rediscover the model.
                            consecutive_errors += 1
                            if self.on_error:
                                try:
                                    self.on_error(exc)
                                except Exception:
                                    pass
                            if self.config.model_address <= 0 and consecutive_errors >= 4:
                                self._model_address = 0
                                self._battle_root_address = 0
                                self._root_only_mode = False
                                self._root_only_next_model_probe_at = 0.0
                                self._battle_view_server_data_address = 0
                                self._server_data_discovery_attempted = profile.dynamic_discovery
                                self._server_data_next_retry_at = 0.0
                                consecutive_errors = 0
                        self._stop.wait(self.config.interval)
            except Exception as exc:
                if self.on_status:
                    self.on_status(f"等待游戏启动或重新连接：{exc}")
                self._model_address = 0 if self.config.model_address <= 0 else self.config.model_address
                self._battle_root_address = 0
                self._root_only_mode = False
                self._root_only_next_model_probe_at = 0.0
                self._battle_view_server_data_address = 0
                self._server_data_discovery_attempted = False
                self._server_data_next_retry_at = 0.0
                self._stop.wait(2.0)
