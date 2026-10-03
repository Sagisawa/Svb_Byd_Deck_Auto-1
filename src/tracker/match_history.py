"""Local match history and per-deck opponent-class statistics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone, tzinfo
import json
import os
from pathlib import Path
import time
from typing import Mapping

from .card_catalog import canonical_card_id


SCHEMA_VERSION = 6

# The order used by the official deck format.  Keep the numeric ID in records
# as well, so an updated translation can be applied without losing history.
CLASS_NAMES = {
    0: "中立",
    1: "精灵",
    2: "皇家护卫",
    3: "巫师",
    4: "龙族",
    5: "梦魇",
    6: "主教",
    7: "超越者",
}


# ``BattleRootMpo.players`` is a server collection, not an ownership-aware
# local/opponent collection.  The following responses carry an ``is_ally``
# bit whose owner can be matched to a public card/unique-id in that collection.
# Keep the list deliberately narrow: target/effect responses may mention an
# enemy card while still being emitted by an allied action.
_SIDE_EVIDENCE_EVENT_TYPES = frozenset({
    "BattleResponsePlayOpen",
    "BattleResponsePutCardFromHand",
    "BattleResponseCastSpellFromHand",
    "BattleResponseFusion",
    "BattleResponseEvolve",
    "BattleResponseSuperEvolve",
    "BattleResponseAttack",
    "BattleResponseActivation",
    "BattleResponseDrawOpen",
    "BattleResponseDrawOpenWithEffect",
    "BattleResponseSkillEffectPrev",
    "BattleResponseSkillEffect",
    "BattleResponseSkillEffectEach",
})

_CARD_ID_SIDE_FALLBACK_EVENT_TYPES = frozenset({
    "BattleResponsePlayOpen",
    "BattleResponsePutCardFromHand",
    "BattleResponseCastSpellFromHand",
    "BattleResponseFusion",
    "BattleResponseEvolve",
    "BattleResponseSuperEvolve",
    "BattleResponseAttack",
    "BattleResponseActivation",
    "BattleResponseSkillEffectPrev",
    "BattleResponseSkillEffect",
    "BattleResponseSkillEffectEach",
})


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _canonical_positive_card_id(value: object) -> int | None:
    parsed = _positive_int(value)
    if parsed is None:
        return None
    try:
        return canonical_card_id(parsed)
    except (TypeError, ValueError):
        return parsed


def _event_result_codes(event: object) -> tuple[int, int] | None:
    if not isinstance(event, Mapping):
        return None
    raw = event.get("result_codes")
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    first = _positive_int(raw[0])
    second = _positive_int(raw[1])
    if first is None or second is None:
        return None
    return first, second


def _player_card_evidence(player: object) -> tuple[set[int], set[int], int, int]:
    """Return public card/UID evidence and visible/hidden hand counts."""
    if not isinstance(player, Mapping):
        return set(), set(), 0, 0
    card_ids: set[int] = set()
    unique_ids: set[int] = set()
    visible_hand = 0
    hidden_hand = 0

    def add_card(value: object) -> None:
        card_id = _canonical_positive_card_id(value)
        if card_id is not None:
            card_ids.add(card_id)

    def add_entry(item: object, *, hand: bool = False) -> None:
        nonlocal visible_hand, hidden_hand
        if not isinstance(item, Mapping):
            if isinstance(item, (list, tuple)) and item:
                add_card(item[0])
            return
        if hand and bool(item.get("hidden")):
            hidden_hand += 1
        raw_unique_id = _positive_int(item.get("unique_id"))
        if raw_unique_id is not None:
            unique_ids.add(raw_unique_id)
        raw_card_id = item.get("base_card_id") or item.get("card_id")
        card_id = _canonical_positive_card_id(raw_card_id)
        if card_id is not None:
            card_ids.add(card_id)
            if hand and not bool(item.get("hidden")):
                visible_hand += 1

    for key in ("hand", "field", "crests", "extra_crests", "special_action_cards"):
        values = player.get(key)
        if isinstance(values, Mapping):
            values = (values,)
        if not isinstance(values, (list, tuple)):
            continue
        for item in values:
            add_entry(item, hand=key == "hand")
    for key in ("played_card_ids", "destroyed_card_ids"):
        values = player.get(key)
        if not isinstance(values, (list, tuple)):
            continue
        for item in values:
            add_entry(item)
    return card_ids, unique_ids, visible_hand, hidden_hand


def _event_source_evidence(event: Mapping[str, object]) -> tuple[set[int], set[int]]:
    """Extract only source-card evidence from a side-tagged response."""
    card_ids: set[int] = set()
    unique_ids: set[int] = set()

    for key in ("unique_id", "from_unique_id", "card_unique_id", "attacker_unique_id"):
        value = _positive_int(event.get(key))
        if value is not None:
            unique_ids.add(value)
    for key in ("card_id", "from_card_id", "after_play_card_id", "evolved_card_id"):
        value = _canonical_positive_card_id(event.get(key))
        if value is not None:
            card_ids.add(value)

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            raw_unique_id = _positive_int(value.get("unique_id"))
            if raw_unique_id is not None:
                unique_ids.add(raw_unique_id)
            raw_card_id = value.get("base_card_id") or value.get("card_id")
            card_id = _canonical_positive_card_id(raw_card_id)
            if card_id is not None:
                card_ids.add(card_id)
            for key in ("card", "cards", "evolved_card", "fusion_card"):
                if key in value:
                    visit(value.get(key))
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    # These are card payloads.  Deliberately do not recurse through targets or
    # arbitrary effect fields, which could identify the opposing side.
    for key in ("card", "cards", "evolved_card", "fusion_card"):
        if key in event:
            visit(event.get(key))
    return card_ids, unique_ids


def infer_local_player_index(
    players: object,
    *,
    events: object = None,
    local_player_index: object = None,
) -> int | None:
    """Infer which server player entry belongs to the local user.

    Unique IDs ``1`` and ``2`` are instance IDs assigned by the game server;
    they are not ownership markers and can swap between matches.  Terminal
    result arrays and side-tagged public responses are the reliable markers,
    followed by the visible local hand.  ``None`` means that the caller should
    retain its legacy positional fallback.
    """
    if not isinstance(players, (list, tuple)) or len(players) != 2:
        return None
    if not isinstance(local_player_index, bool):
        try:
            explicit = int(local_player_index) if local_player_index is not None else None
        except (TypeError, ValueError):
            explicit = None
        if explicit in (0, 1):
            return explicit

    player_results = tuple(
        _positive_int(player.get("result_code")) if isinstance(player, Mapping) else None
        for player in players
    )
    event_values = events if isinstance(events, (list, tuple)) else (events,)
    # BattleResponseBattleEnd.result_codes is ordered local, opponent.  Use
    # the newest unambiguous terminal response first.
    for event in reversed(event_values):
        result_codes = _event_result_codes(event)
        if result_codes is None:
            continue
        first, second = result_codes
        if (
            player_results[0] == first
            and player_results[1] == second
            and first != second
        ):
            return 0
        if (
            player_results[1] == first
            and player_results[0] == second
            and first != second
        ):
            return 1

    evidence = [_player_card_evidence(player) for player in players]
    votes = [0, 0]
    for event in event_values:
        if not isinstance(event, Mapping) or not isinstance(event.get("is_ally"), bool):
            continue
        event_type = str(event.get("type") or "")
        if event_type not in _SIDE_EVIDENCE_EVENT_TYPES:
            continue
        event_cards, event_unique_ids = _event_source_evidence(event)
        if not event_cards and not event_unique_ids:
            continue
        candidates: list[int] = []
        for index, (player_cards, player_unique_ids, _visible, _hidden) in enumerate(evidence):
            if event_unique_ids & player_unique_ids:
                candidates.append(index)
        if len(candidates) != 1 and event_type in _CARD_ID_SIDE_FALLBACK_EVENT_TYPES:
            candidates = [
                index
                for index, (player_cards, _player_unique_ids, _visible, _hidden) in enumerate(evidence)
                if event_cards & player_cards
            ]
        if len(candidates) != 1:
            continue
        owner_index = candidates[0]
        local_index = owner_index if event.get("is_ally") else 1 - owner_index
        votes[local_index] += 1
    if votes[0] > votes[1] and votes[0] > 0:
        return 0
    if votes[1] > votes[0] and votes[1] > 0:
        return 1

    visible = [(item[2], item[3]) for item in evidence]
    if visible[0][0] > 0 and visible[1][0] == 0 and visible[1][1] > 0:
        return 0
    if visible[1][0] > 0 and visible[0][0] == 0 and visible[0][1] > 0:
        return 1
    return None


def default_history_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "ShadowverseTracker" / "matches.json"
    return Path.home() / ".shadowverse_tracker" / "matches.json"


def class_name(class_id: int | None) -> str:
    if class_id is None:
        return "未知职业"
    return CLASS_NAMES.get(int(class_id), f"职业 {class_id}")


def orient_player_order(
    players: object,
    *,
    self_class_id: object = None,
    opponent_class_id: object = None,
    expected_self_class_id: object = None,
    events: object = None,
    local_player_index: object = None,
) -> object:
    """Put the inferred local player first.

    The server's player unique IDs are not ownership markers.  Prefer explicit
    local-side evidence from terminal/public responses, then visible-hand
    evidence, and retain the selected-class fallback for old snapshots.
    Incomplete snapshots remain untouched.
    """
    if not isinstance(players, (list, tuple)) or len(players) != 2:
        return players
    local_index = infer_local_player_index(
        players,
        events=events,
        local_player_index=local_player_index,
    )
    should_swap = local_index == 1
    if local_index is None:
        def _class_id(value: object) -> int | None:
            if isinstance(value, bool) or value is None:
                return None
            try:
                candidate = int(value)
            except (TypeError, ValueError):
                return None
            return candidate if 0 <= candidate <= 7 else None

        expected = _class_id(expected_self_class_id)
        current_self = _class_id(self_class_id)
        current_opponent = _class_id(opponent_class_id)
        should_swap = (
            expected is not None
            and current_self is not None
            and current_opponent == expected
            and current_self != expected
        )
    if not should_swap:
        return players
    ordered = (players[1], players[0])
    return list(ordered) if isinstance(players, list) else ordered


def format_timestamp_local(value: object, *, tz: tzinfo | None = None) -> str:
    """Format a stored ISO timestamp in the user's local time zone.

    New records are persisted as UTC-aware ISO strings.  Older records may be
    naive strings, so those are deliberately treated as already-local values
    instead of silently shifting them a second time.
    """
    raw = str(value or "").strip()
    if not raw:
        return "—"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return raw.replace("T", " ")[:16]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(tz) if tz is not None else parsed.astimezone()
    return parsed.strftime("%Y-%m-%d %H:%M")


def format_timestamp_day_month(value: object, *, tz: tzinfo | None = None) -> str:
    """Format a stored timestamp as the compact ``dd/mm`` history label."""
    raw = str(value or "").strip()
    if not raw:
        return "—"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        # Keep malformed/legacy values visible instead of hiding a row.  A
        # valid ISO value always takes the compact path above.
        return raw.replace("T", " ")[:10]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(tz) if tz is not None else parsed.astimezone()
    return parsed.strftime("%d/%m")


def format_timestamp_time_day_month(value: object, *, tz: tzinfo | None = None) -> str:
    """Format a detailed timestamp as 24-hour ``HH:MM · DD/MM``."""
    raw = str(value or "").strip()
    if not raw:
        return "—"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        # Keep malformed/legacy values visible instead of hiding a detail row.
        return raw.replace("T", " ")[:16]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(tz) if tz is not None else parsed.astimezone()
    return parsed.strftime("%H:%M · %d/%m")


def player_unique_ids(players: object) -> tuple[int | None, int | None] | None:
    """Return the two player instance IDs when a root exposes them."""
    if not isinstance(players, (list, tuple)) or len(players) < 2:
        return None
    values: list[int | None] = []
    for player in players[:2]:
        raw = player.get("unique_id") if isinstance(player, Mapping) else None
        if isinstance(raw, bool) or raw is None:
            values.append(None)
            continue
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            values.append(None)
            continue
        values.append(parsed if parsed > 0 else None)
    return values[0], values[1]


def orient_class_ids(
    self_class_id: object,
    opponent_class_id: object,
    expected_self_class_id: object = None,
) -> tuple[int | None, int | None]:
    """Orient the reader's two class IDs to the selected local deck.

    The BattleInfo user collection is not always ordered like the public
    player collection.  When the selected deck gives us an authoritative local
    class, use it to correct that occasional reversal while retaining the
    reader values as the fallback.
    """
    def _as_id(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            candidate = int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        return candidate if candidate is not None and 0 <= candidate <= 7 else None

    current_self = _as_id(self_class_id)
    current_opponent = _as_id(opponent_class_id)
    expected = _as_id(expected_self_class_id)
    if expected is None:
        return current_self, current_opponent
    if current_opponent == expected and current_self != expected:
        return expected, current_self
    return expected, current_opponent


def result_label(result_code: int, self_life: int | None, opponent_life: int | None) -> str:
    """Classify terminal snapshots conservatively.

    Life reaching zero or the known victory code is conclusive.  Other
    non-zero codes are retained as ``结束`` until their meaning is confirmed,
    rather than silently polluting win-rate statistics.
    """
    if opponent_life is not None and opponent_life <= 0:
        return "胜利"
    if self_life is not None and self_life <= 0:
        return "失败"
    if result_code == 101:
        return "胜利"
    # 105 is the opponent-surrender terminal result.  Both life totals can
    # remain positive, so it cannot be inferred from the board alone.
    if result_code == 105:
        return "胜利"
    # Shadowverse WB uses 106 for the local player's surrender result.  Life
    # totals remain non-zero in this case, so it must be handled explicitly.
    if result_code == 106:
        return "失败"
    return "结束"


def _normalise_played_card_ids(value: object) -> tuple[int, ...]:
    """Store public played-card observations in a compact stable shape."""
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[int] = []
    for item in value:
        raw: object = item
        if isinstance(item, dict):
            raw = item.get("base_card_id") or item.get("card_id")
        elif isinstance(item, (list, tuple)) and item:
            raw = item[0]
        try:
            card_id = canonical_card_id(int(raw))
        except (TypeError, ValueError):
            continue
        if card_id > 0:
            result.append(card_id)
    return tuple(result)


def _normalise_played_card_turns(value: object) -> tuple[tuple[int, int], ...]:
    """Normalize public ``(card_id, turn)`` observations from JSON or logs."""
    if not isinstance(value, (list, tuple)):
        return ()
    result: set[tuple[int, int]] = set()
    for item in value:
        raw_card: object = None
        raw_turn: object = None
        if isinstance(item, Mapping):
            raw_card = item.get("base_card_id") or item.get("card_id")
            raw_turn = item.get("turn")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            raw_card, raw_turn = item[0], item[1]
        try:
            card_id = canonical_card_id(int(raw_card))
            turn = int(raw_turn)
        except (TypeError, ValueError):
            continue
        if card_id > 0 and turn > 0:
            result.add((card_id, turn))
    return tuple(sorted(result, key=lambda item: (item[1], item[0])))


def extract_played_card_turns(player: object) -> tuple[tuple[int, int], ...]:
    """Extract public card-play turns from a player snapshot.

    The reader exposes the same information through three compatible paths:
    the structured public PlayOpen events, a parallel turn list attached to
    ``played_card_ids``, and the recent action ledger.  Merge them so a
    terminal frame that catches one path between polls still records the
    earliest reliable turn.  Only explicit play/use entries are accepted;
    hidden hand cards and draw/evolution bookkeeping are never inferred.
    """
    if not isinstance(player, Mapping):
        return ()
    result: set[tuple[int, int]] = set()

    def add(raw_card: object, raw_turn: object) -> None:
        try:
            card_id = canonical_card_id(int(raw_card))
            turn = int(raw_turn)
        except (TypeError, ValueError):
            return
        if card_id > 0 and turn > 0:
            result.add((card_id, turn))

    played = player.get("played_card_ids")
    turns = player.get("_played_card_turns")
    if isinstance(played, (list, tuple)):
        for index, item in enumerate(played):
            raw_card: object = item
            raw_turn: object = turns[index] if isinstance(turns, (list, tuple)) and index < len(turns) else None
            if isinstance(item, Mapping):
                raw_card = item.get("base_card_id") or item.get("card_id")
                raw_turn = item.get("turn", raw_turn)
            elif isinstance(item, (list, tuple)) and item:
                # Some reader versions expose ``played_card_ids`` entries as
                # tuples whose second value is not a turn.  The parallel
                # ``_played_card_turns`` list is authoritative for that
                # shape, so never guess from the tuple's second slot.
                raw_card = item[0]
            add(raw_card, raw_turn)

    for key in ("_event_played_cards", "played_card_turns"):
        entries = player.get(key)
        if isinstance(entries, Mapping):
            entries = entries.get("self") or entries.get("opponent") or ()
        if not isinstance(entries, (list, tuple)):
            continue
        for item in entries:
            if isinstance(item, Mapping):
                add(
                    item.get("base_card_id") or item.get("card_id"),
                    item.get("turn"),
                )
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                add(item[0], item[1])

    actions = player.get("_recent_actions")
    if isinstance(actions, (list, tuple)):
        for item in actions:
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("kind") or "").strip().casefold()
            if kind not in {"使用", "使用卡牌", "play", "played"}:
                continue
            add(item.get("base_card_id") or item.get("card_id"), item.get("turn"))
    return tuple(sorted(result, key=lambda item: (item[1], item[0])))


def first_play_turn(
    card_id: object,
    observations: object,
) -> int | None:
    """Return the first public turn on which ``card_id`` was played."""
    try:
        target = canonical_card_id(int(card_id))
    except (TypeError, ValueError):
        return None
    turns = _normalise_played_card_turns(observations)
    matching = [turn for observed_id, turn in turns if observed_id == target]
    return min(matching) if matching else None


def _optional_int(value: object) -> int | None:
    """Parse an optional integer field without discarding the whole record."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def terminal_match_id(
    model_address: str,
    result_code: int,
    turn: int | None,
    self_life: int | None,
    opponent_life: int | None,
    deck_count: int | None,
    cemetery_count: int | None,
    played_count: int,
    destroyed_count: int,
) -> str:
    """Build a stable identity for one terminal BattleModel snapshot."""
    return (
        f"terminal:{model_address}:{result_code}:{turn}:{self_life}:"
        f"{opponent_life}:{deck_count}:{cemetery_count}:{played_count}:{destroyed_count}"
    )


@dataclass(frozen=True)
class MatchRecord:
    match_id: str
    timestamp: str
    deck_key: str
    deck_name: str
    self_class_id: int | None
    opponent_class_id: int | None
    opponent_class: str
    result: str
    result_code: int
    turn: int | None
    # Capture the local deck's format at match time so history filters remain
    # correct even if the saved deck is later edited or removed.
    deck_format: int | None = None
    is_first: bool | None = None
    opponent_deck_name: str = ""
    # Ranked results expose the post-match class-rating delta separately from
    # the win/loss result.  Older records and non-ranked modes keep this None.
    cr_change: int | None = None
    # The class rating after the ranked result.  This is kept separately from
    # the delta so a later refresh can show the exact rating reached.
    current_cr: int | None = None
    # Only public cards that the opponent has played are retained.  Hidden
    # cards are never inferred or persisted as if they were known.
    opponent_played_card_ids: tuple[int, ...] = ()
    # Keep all public play turns, not only the currently selected core card.
    # This lets the history page recalculate the displayed core turn when a
    # Meta/deck core selection changes later.
    self_played_card_turns: tuple[tuple[int, int], ...] = ()
    opponent_played_card_turns: tuple[tuple[int, int], ...] = ()


class MatchHistory:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_history_path()
        self.records: list[MatchRecord] = []

    def load(self) -> "MatchHistory":
        if not self.path.exists():
            return self
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or int(value.get("schema_version", 0)) not in (1, 2, 3, 4, 5, SCHEMA_VERSION):
            raise ValueError("不支持的对局记录格式")
        migrated = int(value.get("schema_version", 0)) != SCHEMA_VERSION
        records = value.get("records", ())
        if not isinstance(records, list):
            raise ValueError("本地对局记录已损坏")
        parsed: list[MatchRecord] = []
        for item in records:
            if not isinstance(item, dict):
                continue
            try:
                result_code = int(item.get("result_code", 0))
                result = str(item["result"])
                if result == "结束" and result_code in {105, 106}:
                    result = "胜利" if result_code == 105 else "失败"
                    migrated = True
                opponent_class_id = int(item["opponent_class_id"]) if item.get("opponent_class_id") is not None else None
                opponent_class = str(item.get("opponent_class") or "未知职业")
                canonical_opponent_class = class_name(opponent_class_id) if opponent_class_id is not None else opponent_class
                if opponent_class_id is not None and opponent_class != canonical_opponent_class:
                    migrated = True
                    opponent_class = canonical_opponent_class
                cr_change = _optional_int(item.get("cr_change"))
                current_cr = _optional_int(item.get("current_cr"))
                if current_cr is None:
                    # Accept the temporary name used by early CR prototypes.
                    current_cr = _optional_int(item.get("cr_after"))
                deck_format = _optional_int(item.get("deck_format"))
                if deck_format not in {1, 2}:
                    deck_format = None
                self_played_card_turns = _normalise_played_card_turns(
                    item.get("self_played_card_turns")
                    or item.get("self_core_played_card_turns")
                    or ()
                )
                opponent_played_card_turns = _normalise_played_card_turns(
                    item.get("opponent_played_card_turns")
                    or item.get("opponent_core_played_card_turns")
                    or ()
                )
                opponent_played_card_ids = _normalise_played_card_ids(
                    item.get("opponent_played_card_ids")
                    or item.get("opponent_observed_card_ids")
                    or ()
                )
                if not opponent_played_card_ids and opponent_played_card_turns:
                    opponent_played_card_ids = tuple(
                        card_id for card_id, _turn in opponent_played_card_turns
                    )
                parsed.append(MatchRecord(
                    match_id=str(item["match_id"]),
                    timestamp=str(item["timestamp"]),
                    deck_key=str(item["deck_key"]),
                    deck_name=str(item["deck_name"]),
                    self_class_id=int(item["self_class_id"]) if item.get("self_class_id") is not None else None,
                    opponent_class_id=opponent_class_id,
                    opponent_class=opponent_class,
                    result=result,
                    result_code=result_code,
                    turn=int(item["turn"]) if item.get("turn") is not None else None,
                    deck_format=deck_format,
                    is_first=bool(item["is_first"]) if item.get("is_first") is not None else None,
                    opponent_deck_name=str(
                        item.get("opponent_deck_name")
                        or item.get("opponent_deck")
                        or ""
                    ).strip(),
                    cr_change=cr_change,
                    current_cr=current_cr,
                    opponent_played_card_ids=opponent_played_card_ids,
                    self_played_card_turns=self_played_card_turns,
                    opponent_played_card_turns=opponent_played_card_turns,
                ))
            except (KeyError, TypeError, ValueError):
                continue
        self.records = parsed
        if migrated:
            self.save()
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Include the process ID in the temporary name.  Two tracker windows
        # may be open during an upgrade/test; sharing ``matches.json.tmp``
        # lets one writer replace or remove the other writer's temp file and
        # leaves the in-memory row looking saved when it was not.
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{id(self):x}.tmp"
        )
        payload = json.dumps({
            "schema_version": SCHEMA_VERSION,
            "records": [asdict(record) for record in self.records],
        }, ensure_ascii=False, indent=2)
        temporary.write_text(payload, encoding="utf-8")
        try:
            # Windows can briefly hold the destination while another process
            # is refreshing the page.  A short bounded retry avoids dropping
            # an otherwise valid terminal result on that transient lock.
            for attempt in range(4):
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    if attempt >= 3:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def add(self, record: MatchRecord) -> bool:
        if any(existing.match_id == record.match_id for existing in self.records):
            return False
        self.records.append(record)
        try:
            self.save()
        except Exception:
            # Never leave a row visible in the current process when its disk
            # write failed; otherwise a restart silently loses it and makes
            # the tracker appear to have missed the match.
            self.records.pop()
            raise
        return True

    def delete(self, match_id: str) -> bool:
        """Delete exactly one saved match by its stable match ID.

        A match ID is generated from the terminal snapshot and is the only
        identifier used by the detailed-history UI.  Filtering by deck, class,
        or timestamp would risk deleting a different match when two games
        share the same visible values, so keep this operation deliberately
        exact and persist only after a matching row is found.
        """
        target = str(match_id or "").strip()
        if not target:
            return False
        for index, record in enumerate(self.records):
            if record.match_id != target:
                continue
            removed = self.records.pop(index)
            try:
                self.save()
            except Exception:
                # Keep the in-memory view aligned with the file if a disk
                # write fails; callers can surface the original error safely.
                self.records.insert(index, removed)
                raise
            return True
        return False

    def update_opponent_deck(self, match_id: str, name: str) -> bool:
        """Persist the user-entered opponent deck label for one match."""
        clean_name = str(name or "").strip()
        for index, record in enumerate(self.records):
            if record.match_id != match_id:
                continue
            if record.opponent_deck_name == clean_name:
                return False
            self.records[index] = replace(record, opponent_deck_name=clean_name)
            try:
                self.save()
            except Exception:
                self.records[index] = record
                raise
            return True
        return False

    def update_cr_change(self, match_id: str, value: object) -> bool:
        """Persist a newly available ranked CR delta for one match."""
        if isinstance(value, bool) or value is None:
            return False
        try:
            cr_change = int(value)
        except (TypeError, ValueError):
            return False
        for index, record in enumerate(self.records):
            if record.match_id != match_id:
                continue
            if record.cr_change == cr_change:
                return False
            self.records[index] = replace(record, cr_change=cr_change)
            try:
                self.save()
            except Exception:
                self.records[index] = record
                raise
            return True
        return False

    def update_cr_values(
        self,
        match_id: str,
        change: object = None,
        current: object = None,
    ) -> bool:
        """Persist ranked CR delta and post-match rating when they arrive late."""
        parsed_change = _optional_int(change)
        parsed_current = _optional_int(current)
        if parsed_change is None and parsed_current is None:
            return False
        for index, record in enumerate(self.records):
            if record.match_id != match_id:
                continue
            next_change = record.cr_change if parsed_change is None else parsed_change
            next_current = record.current_cr if parsed_current is None else parsed_current
            if record.cr_change == next_change and record.current_cr == next_current:
                return False
            self.records[index] = replace(
                record,
                cr_change=next_change,
                current_cr=next_current,
            )
            try:
                self.save()
            except Exception:
                self.records[index] = record
                raise
            return True
        return False

    def update_opponent_played_cards(self, match_id: str, card_ids: object) -> bool:
        """Persist public opponent cards without changing a manual label."""
        observed = _normalise_played_card_ids(card_ids)
        for index, record in enumerate(self.records):
            if record.match_id != match_id:
                continue
            if record.opponent_played_card_ids == observed:
                return False
            self.records[index] = replace(record, opponent_played_card_ids=observed)
            try:
                self.save()
            except Exception:
                self.records[index] = record
                raise
            return True
        return False

    def update_played_card_turns(
        self,
        match_id: str,
        self_turns: object = None,
        opponent_turns: object = None,
    ) -> bool:
        """Merge later public play-turn evidence into an existing row.

        Terminal snapshots can be repeated while the final PlayOpen event is
        still being attached to the model.  Merge rather than replace so a
        short intermediate snapshot can never erase an already known turn.
        """
        parsed_self = set(_normalise_played_card_turns(self_turns))
        parsed_opponent = set(_normalise_played_card_turns(opponent_turns))
        if not parsed_self and not parsed_opponent:
            return False
        for index, record in enumerate(self.records):
            if record.match_id != match_id:
                continue
            next_self = tuple(sorted(
                set(_normalise_played_card_turns(record.self_played_card_turns)) | parsed_self,
                key=lambda item: (item[1], item[0]),
            ))
            next_opponent = tuple(sorted(
                set(_normalise_played_card_turns(record.opponent_played_card_turns)) | parsed_opponent,
                key=lambda item: (item[1], item[0]),
            ))
            next_ids = record.opponent_played_card_ids
            if next_opponent:
                observed_ids = tuple(card_id for card_id, _turn in next_opponent)
                next_ids = tuple(dict.fromkeys((*next_ids, *observed_ids)))
            if (
                next_self == record.self_played_card_turns
                and next_opponent == record.opponent_played_card_turns
                and next_ids == record.opponent_played_card_ids
            ):
                return False
            self.records[index] = replace(
                record,
                self_played_card_turns=next_self,
                opponent_played_card_turns=next_opponent,
                opponent_played_card_ids=next_ids,
            )
            try:
                self.save()
            except Exception:
                self.records[index] = record
                raise
            return True
        return False

    def reconcile_deck_class_ids(self, deck_class_ids: Mapping[str, object]) -> int:
        """Correct class orientation using the saved deck's known class.

        A BattleInfo snapshot can occasionally return its two class IDs in the
        opposite order from the public player list.  Saved deck metadata is a
        reliable local-side anchor, so swap only when the opponent slot holds
        that expected class; unrelated or unknown rows are left untouched.
        """
        changed = False
        updates = 0
        for index, record in enumerate(self.records):
            expected = deck_class_ids.get(record.deck_key)
            local_class_id, opponent_class_id = orient_class_ids(
                record.self_class_id,
                record.opponent_class_id,
                expected,
            )
            opponent_class = class_name(opponent_class_id)
            if (
                local_class_id == record.self_class_id
                and opponent_class_id == record.opponent_class_id
                and opponent_class == record.opponent_class
            ):
                continue
            self.records[index] = replace(
                record,
                self_class_id=local_class_id,
                opponent_class_id=opponent_class_id,
                opponent_class=opponent_class,
            )
            changed = True
            updates += 1
        if changed:
            self.save()
        return updates

    def auto_match_opponent_decks(
        self,
        matcher: object,
        observations: object = None,
    ) -> int:
        """Fill blank opponent-deck labels from public-card observations.

        ``matcher`` is intentionally duck-typed here to keep this persistence
        module independent of the optional meta-deck source.  User-entered
        labels are never overwritten.  ``observations`` may provide terminal
        match IDs from an older app-session log; the longest observation wins.
        The return value is the number of newly labelled rows.
        """
        external: dict[str, object] = observations if isinstance(observations, dict) else {}
        changed = False
        labels_added = 0
        for index, record in enumerate(self.records):
            observed = record.opponent_played_card_ids
            if not observed and record.opponent_played_card_turns:
                observed = tuple(
                    card_id for card_id, _turn in _normalise_played_card_turns(
                        record.opponent_played_card_turns
                    )
                )
            candidate = _normalise_played_card_ids(external.get(record.match_id))
            if len(candidate) > len(observed):
                observed = candidate
            updated = record
            if observed != record.opponent_played_card_ids:
                updated = replace(updated, opponent_played_card_ids=observed)
                changed = True
            if not updated.opponent_deck_name and observed:
                try:
                    match = matcher.match(observed, updated.opponent_class_id)
                except (AttributeError, TypeError, ValueError):
                    match = None
                label = str(getattr(match, "label", "") or "").strip() if match is not None else ""
                if label:
                    updated = replace(updated, opponent_deck_name=label)
                    changed = True
                    labels_added += 1
            if updated != record:
                self.records[index] = updated
        if changed:
            self.save()
        return labels_added

    def clear_deck(self, deck_key: str) -> int:
        """Delete all locally saved match records for one deck."""
        before = len(self.records)
        self.records = [record for record in self.records if record.deck_key != deck_key]
        removed = before - len(self.records)
        if removed:
            self.save()
        return removed

    def clear_all(self) -> int:
        """Delete all locally saved match records and return the count."""
        removed = len(self.records)
        if not removed:
            return 0
        self.records = []
        self.save()
        return removed

    def for_deck(self, deck_key: str) -> list[MatchRecord]:
        return [record for record in self.records if record.deck_key == deck_key]

    def stats(
        self,
        deck_key: str | None = None,
        opponent_class: str | None = None,
        opponent_deck: object = None,
    ) -> dict[str, object]:
        # A record labelled ``结束`` is retained for diagnostics but is not a
        # completed result. In normal operation 105/106 are migrated above,
        # so all displayed games have a win or loss.
        def matches_opponent_deck(record: MatchRecord) -> bool:
            if opponent_deck is None or opponent_deck == "":
                return True
            stored = str(record.opponent_deck_name or "").strip()
            if stored == "（双击输入）":
                # Older Qt builds displayed this placeholder in the editable
                # cell; treat it exactly like an unrecognised/blank label.
                stored = ""
            if opponent_deck == "__other__":
                return not stored
            # The Qt selector passes a tuple of raw labels when one displayed
            # Meta archetype represents several historical spellings (for
            # example a concrete build name and its canonical archetype).
            if isinstance(opponent_deck, (list, tuple, set, frozenset)):
                return stored in {str(value).strip() for value in opponent_deck}
            return stored == str(opponent_deck).strip()

        records = [
            record for record in self.records
            if (deck_key is None or record.deck_key == deck_key)
            and (opponent_class is None or record.opponent_class == opponent_class)
            and matches_opponent_deck(record)
            and record.result in {"胜利", "失败"}
        ]
        grouped: dict[str, dict[str, int | float | dict[str, int | float]]] = {}
        for record in records:
            group = grouped.setdefault(record.opponent_class, {
                "total": 0, "wins": 0, "losses": 0, "finished": 0,
                "first": {"total": 0, "wins": 0, "losses": 0, "finished": 0},
                "second": {"total": 0, "wins": 0, "losses": 0, "finished": 0},
            })
            group["total"] += 1
            order = group["first"] if record.is_first else group["second"] if record.is_first is False else None
            if order is not None:
                order["total"] += 1
            if record.result == "胜利":
                group["wins"] += 1
                group["finished"] += 1
                if order is not None:
                    order["wins"] += 1
                    order["finished"] += 1
            elif record.result == "失败":
                group["losses"] += 1
                group["finished"] += 1
                if order is not None:
                    order["losses"] += 1
                    order["finished"] += 1
        for group in grouped.values():
            finished = int(group["finished"])
            group["win_rate"] = round(int(group["wins"]) * 100 / finished, 1) if finished else 0.0
            for key in ("first", "second"):
                order = group[key]
                finished = int(order["finished"])
                order["win_rate"] = round(int(order["wins"]) * 100 / finished, 1) if finished else 0.0
        wins = sum(int(group["wins"]) for group in grouped.values())
        losses = sum(int(group["losses"]) for group in grouped.values())
        finished = wins + losses
        orders = {}
        for key in ("first", "second"):
            order_wins = sum(int(group[key]["wins"]) for group in grouped.values())
            order_losses = sum(int(group[key]["losses"]) for group in grouped.values())
            order_finished = order_wins + order_losses
            orders[key] = {
                "wins": order_wins,
                "losses": order_losses,
                "finished": order_finished,
                "win_rate": round(order_wins * 100 / order_finished, 1) if order_finished else 0.0,
            }
        return {
            "total": len(records),
            "wins": wins,
            "losses": losses,
            "finished": finished,
            "win_rate": round(wins * 100 / finished, 1) if finished else 0.0,
            "first": orders["first"],
            "second": orders["second"],
            "by_class": grouped,
        }


__all__ = [
    "CLASS_NAMES",
    "MatchHistory",
    "MatchRecord",
    "class_name",
    "default_history_path",
    "format_timestamp_local",
    "format_timestamp_time_day_month",
    "infer_local_player_index",
    "player_unique_ids",
    "orient_class_ids",
    "orient_player_order",
    "result_label",
    "terminal_match_id",
]
