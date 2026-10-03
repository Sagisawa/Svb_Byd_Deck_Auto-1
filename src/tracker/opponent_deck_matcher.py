"""Conservative opponent-deck identification from public play history.

The game does not expose an opponent's complete deck while a match is in
progress.  This module therefore treats identification as a candidate match,
not as an oracle: only cards that have actually been played are considered and
an automatic label is returned after enough evidence separates one profile
from the others.

Profiles are deliberately stored as a small JSON cache instead of being
downloaded in the polling thread.  The source site is client-rendered and can
require a browser challenge; a stale cache is preferable to blocking or
changing match tracking when the site is unavailable.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import gzip
import json
import os
from pathlib import Path
import sys
from typing import Iterable, Mapping

from .card_catalog import (
    card_class_id,
    canonical_card_id,
    get_card_metadata,
    is_legendary_card,
)


SCHEMA_VERSION = 1
DEFAULT_SOURCE_URL = "https://sva.hypd.asia/deck"
DEFAULT_META_REFRESH_VERSION = "tier-archetypes-v3"

# The labels/order used by WBArts' Tier / Meta panel.  Keeping the fallback
# map locally means an older bundled cache (which predates the tier field) is
# still rendered in the same groups as the website.
META_TIER_ORDER = ("T1", "T2", "T3", "T4", "其他")
META_ARCHETYPE_TIERS = {
    "中速梦": "T1",
    "跳费龙": "T1",
    "护符教": "T2",
    "造物超": "T2",
    "连击妖": "T2",
    "实验法": "T2",
    "谢幕梦": "T2",
    "旗皇": "T3",
    "进化教": "T3",
    "财宝皇": "T3",
    "脸龙": "T3",
    "协作皇": "T3",
    "增幅法": "T3",
    "快梦": "T3",
    "骰子教": "T4",
    "验牌梦": "T4",
    "节奏妖": "T4",
    "进化超": "T4",
    "OTK超": "T4",
    "宇宙超": "T4",
    "进化梦": "T4",
    "节奏进化妖": "T4",
    # The API currently exposes these builds outside the ranked T1–T3 rows.
    # They are still valid rotation archetypes and should remain visible in
    # the app instead of being hidden in the catch-all bucket.
    "进化妖": "T4",
    "中速皇": "T4",
    "魔神梦": "T4",
    "疾驰教": "T4",
    "疾驰妖": "T4",
}

# WBArts' tier panel also defines the order of archetypes within each tier.
# Keep it separate from the tier map so the UI can sort concrete builds by
# the same order instead of falling back to class/name ordering.  This is
# especially important when a source row is promoted into T4, whose entries
# are otherwise indistinguishable by tier alone.
META_ARCHETYPE_ORDER = (
    "中速梦",
    "跳费龙",
    "护符教",
    "造物超",
    "连击妖",
    "实验法",
    "谢幕梦",
    "旗皇",
    "进化教",
    "财宝皇",
    "脸龙",
    "协作皇",
    "增幅法",
    "快梦",
    "骰子教",
    "验牌梦",
    "节奏妖",
    "进化超",
    "OTK超",
    "宇宙超",
    "进化梦",
    "节奏进化妖",
    "进化妖",
    "中速皇",
    "魔神梦",
    "疾驰教",
    "疾驰妖",
)
_META_ARCHETYPE_ORDER_INDEX = {
    archetype: index for index, archetype in enumerate(META_ARCHETYPE_ORDER)
}

# WBArts currently serializes several archetype filters as ``local:<id>`` in
# its deck API even though the public Tier/Meta panel gives them Chinese
# labels.  Keep the current stable IDs as a first-pass translation; the card
# similarity fallback below still handles newly introduced IDs.
META_LOCAL_ARCHETYPE_LABELS = {
    "local:1": "造物超",
    "local:3": "中速梦",
    "local:4": "快梦",
    "local:5": "协作皇",
    "local:6": "跳费龙",
    "local:7": "实验法",
    "local:8": "脸龙",
    "local:9": "进化教",
    "local:10": "节奏妖",
    "local:11": "增幅法",
    "local:12": "护符教",
    "local:13": "财宝皇",
    "local:15": "谢幕梦",
    "local:17": "连击妖",
    "local:34": "旗皇",
    "local:2": "进化梦",
    "local:14": "验牌梦",
    "local:20": "节奏进化妖",
    "local:22": "进化超",
    "local:24": "骰子教",
    "local:26": "进化妖",
    "local:32": "疾驰教",
    "local:33": "宇宙超",
    "local:40": "中速皇",
    "local:42": "OTK超",
    "local:43": "魔神梦",
    "local:44": "疾驰妖",
}


def meta_tier_label(value: object = "", archetype: object = "") -> str:
    """Normalize a WBArts tier/category value to ``T1``…``T4``/``其他``."""
    if isinstance(value, Mapping):
        value = value.get("name") or value.get("label") or value.get("tier") or value.get("value")
    raw = str(value or "").strip()
    normalized = raw.casefold().replace("_", " ").replace("-", " ")
    if normalized in {"t1", "tier 1", "tier1", "1"}:
        return "T1"
    if normalized in {"t2", "tier 2", "tier2", "2"}:
        return "T2"
    if normalized in {"t3", "tier 3", "tier3", "3"}:
        return "T3"
    if normalized in {"t4", "tier 4", "tier4", "4"}:
        return "T4"
    if normalized in {
        "other",
        "others",
        "other meta",
        "unranked",
        "unranked tier",
        "其他",
        "其余",
    }:
        # A stale cache may have persisted the old catch-all label even though
        # its archetype is one of the site's known Tier entries.  Prefer the
        # current archetype map in that case so those builds return to their
        # proper T1–T4 section after an app restart.
        mapped = META_ARCHETYPE_TIERS.get(str(archetype or "").strip())
        if mapped:
            return mapped
        return "其他"
    return META_ARCHETYPE_TIERS.get(str(archetype or "").strip(), "其他")


def meta_archetype_label(archetype: object = "", name: object = "") -> str:
    """Return a stable type label, stripping known sample suffixes implicitly."""
    explicit = str(archetype or "").strip()
    candidate = str(name or "").strip()
    for value in (explicit, candidate):
        mapped = META_LOCAL_ARCHETYPE_LABELS.get(value.casefold())
        if mapped:
            return mapped
    # The site sometimes puts the sample suffix in ``archetype`` and
    # sometimes only in the display name.  Normalize both fields so a
    # cached/API row such as ``中速梦·构筑 A`` never leaks the suffix into
    # history or the Meta list.
    for value in (explicit, candidate):
        for known in META_ARCHETYPE_TIERS:
            if value == known or value.startswith(f"{known}·") or value.startswith(f"{known} "):
                return known
    return explicit or candidate


def meta_archetype_sort_key(
    archetype: object = "",
    name: object = "",
    tier: object = "",
) -> tuple[int, int, str, str]:
    """Return the stable order used by WBArts' Tier / Meta panel.

    ``tier`` remains authoritative when a source supplies one.  For older
    caches that omit it, the archetype fallback map supplies the current site
    tier.  Unknown/local labels stay at the end of their tier and retain a
    deterministic textual order.
    """
    label = meta_archetype_label(archetype, name)
    normalized_tier = meta_tier_label(tier, label)
    tier_index = (
        META_TIER_ORDER.index(normalized_tier)
        if normalized_tier in META_TIER_ORDER
        else len(META_TIER_ORDER)
    )
    archetype_index = _META_ARCHETYPE_ORDER_INDEX.get(label, len(META_ARCHETYPE_ORDER))
    return (
        tier_index,
        archetype_index,
        label.casefold(),
        str(name or "").strip().casefold(),
    )


def is_machine_meta_label(value: object) -> bool:
    """Return whether a source label is an internal, non-user-facing ID."""
    normalized = str(value or "").strip().casefold()
    return normalized.startswith(("local:", "archetype:", "deck:"))


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _played_card_ids(value: object) -> tuple[int, ...]:
    """Extract base card IDs from the reader's ``played_card_ids`` shape."""
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[int] = []
    for item in value:
        raw: object = item
        if isinstance(item, Mapping):
            raw = item.get("base_card_id") or item.get("card_id")
        elif isinstance(item, (list, tuple)) and item:
            raw = item[0]
        card_id = _int_or_none(raw)
        if card_id is None or card_id <= 0:
            continue
        result.append(canonical_card_id(card_id))
    return tuple(result)


def _counted_known_cards(value: object) -> Counter[int]:
    counts: Counter[int] = Counter()
    if not isinstance(value, (list, tuple)):
        return counts
    for item in value:
        if not isinstance(item, Mapping):
            continue
        raw_id = item.get("base_card_id") or item.get("card_id")
        try:
            card_id = canonical_card_id(int(raw_id))
        except (TypeError, ValueError):
            continue
        if card_id <= 0:
            continue
        raw_count = item.get("count", 1)
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = 1
        if count > 0:
            counts[card_id] = max(counts[card_id], count)
    return counts


def _merge_card_sources(sources: Iterable[Counter[int]]) -> tuple[int, ...]:
    merged: Counter[int] = Counter()
    for source in sources:
        for card_id, count in source.items():
            merged[card_id] = max(merged[card_id], count)
    result: list[int] = []
    for card_id in sorted(merged):
        result.extend([card_id] * merged[card_id])
    return tuple(result)


def _recent_played_card_ids(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    allowed_kinds = {"", "使用", "使用卡牌", "play", "played"}
    result: list[object] = []
    for item in value:
        if isinstance(item, Mapping):
            kind = str(item.get("kind") or "").strip().casefold()
            if kind not in allowed_kinds:
                continue
        result.append(item)
    return _played_card_ids(result)


def collect_public_played_card_ids(player: object) -> tuple[int, ...]:
    """Merge public cards known to have been played by one side."""
    if not isinstance(player, Mapping):
        return ()
    return _merge_card_sources((
        Counter(_played_card_ids(player.get("played_card_ids"))),
        Counter(_played_card_ids(player.get("_event_played_cards"))),
        Counter(_recent_played_card_ids(
            (player.get("opponent_hand_knowledge") or {}).get("recent_actions")
            if isinstance(player.get("opponent_hand_knowledge"), Mapping)
            else None
        )),
    ))


def collect_public_card_ids(player: object) -> tuple[int, ...]:
    """Merge public opponent-card evidence without counting one card twice.

    The permanent play list can lag behind a response event, while known hand
    cards and recent actions can overlap with both.  Treat each source as an
    observation of the same card quantity and take the per-card maximum.  This
    keeps the recognizer responsive during an animation without inflating a
    deck's apparent copy count.
    """
    if not isinstance(player, Mapping):
        return ()
    sources: list[Counter[int]] = [Counter(collect_public_played_card_ids(player))]
    knowledge = player.get("opponent_hand_knowledge")
    if isinstance(knowledge, Mapping):
        known = _counted_known_cards(knowledge.get("known_cards"))
        if known:
            sources.append(known)
    return _merge_card_sources(sources)


def _iter_session_log_lines(source: Path):
    """Yield lines from the active session log and its gzip archives."""
    candidates = [source]
    if source.suffix == ".jsonl":
        candidates.extend(sorted(source.parent.glob(f"{source.stem}.*.jsonl")))
        candidates.extend(sorted(source.parent.glob(f"{source.stem}.*.jsonl.gz")))
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        try:
            if candidate.suffix == ".gz":
                with gzip.open(candidate, "rt", encoding="utf-8") as handle:
                    yield from handle
            else:
                with candidate.open("r", encoding="utf-8") as handle:
                    yield from handle
        except (OSError, UnicodeError):
            # A log may be rotated while the backfill thread is scanning it.
            # Skip only the affected segment and keep the other segments.
            continue


def load_session_opponent_observations(
    path: Path | None = None,
    *,
    turn_observations: dict[str, tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]] | None = None,
) -> dict[str, tuple[int, ...]]:
    """Read terminal opponent-play observations from an app-session log.

    ``matches.json`` intentionally stores only the human-facing match summary.
    The address-free training stream cannot identify a summary row either, but
    the local app-session stream contains the terminal snapshot and therefore
    the stable match ID used by :mod:`match_history`.  New snapshots carry an
    opaque per-match ID, while the legacy ``terminal:*`` ID is also retained
    so existing rows benefit from the recognizer after an upgrade.  When
    ``turn_observations`` is supplied, the same scan also returns both
    players' public ``(card_id, turn)`` evidence for history backfill.
    Malformed or partially-written lines are ignored because the polling
    thread may append to the file while this function is running.  Rotated
    ``.jsonl.gz`` segments are included even when the active file is empty or
    is being created by the reader thread.
    """
    source = Path(path) if path is not None else Path("logs") / "app_session.jsonl"
    # Import lazily to keep the matcher usable by data-refresh scripts without
    # importing the history/UI modules at module import time.
    from .match_history import (
        extract_played_card_turns,
        orient_player_order,
        result_label,
        terminal_match_id,
    )

    observations: dict[str, tuple[int, ...]] = {}
    if source.is_file() or source.parent.is_dir():
        for line in _iter_session_log_lines(source):
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, Mapping):
                continue
            snapshot = payload.get("snapshot")
            if not isinstance(snapshot, Mapping):
                continue
            root = snapshot.get("root")
            players = root.get("players") if isinstance(root, Mapping) else None
            if not isinstance(players, (list, tuple)) or len(players) < 2:
                continue
            deck = snapshot.get("deck")
            expected_class = deck.get("class_id") if isinstance(deck, Mapping) else None
            players = orient_player_order(
                players,
                self_class_id=snapshot.get("self_class_id"),
                opponent_class_id=snapshot.get("opponent_class_id"),
                expected_self_class_id=expected_class,
                events=snapshot.get("events"),
            )
            mine, opponent = players[0], players[1]
            if not isinstance(mine, Mapping) or not isinstance(opponent, Mapping):
                continue
            result_code = _int_or_none(mine.get("result_code")) or 0
            self_life = _int_or_none(mine.get("life"))
            opponent_life = _int_or_none(opponent.get("life"))
            if result_label(result_code, self_life, opponent_life) not in {"胜利", "失败"}:
                continue
            address = str(snapshot.get("address") or payload.get("model") or "unknown")
            turn = _int_or_none(mine.get("turn"))
            deck_count = _int_or_none(mine.get("deck_count"))
            cemetery_count = _int_or_none(mine.get("cemetery_count"))
            played = mine.get("played_card_ids")
            destroyed = mine.get("destroyed_card_ids")
            legacy_match_id = terminal_match_id(
                address,
                result_code,
                turn,
                self_life,
                opponent_life,
                deck_count,
                cemetery_count,
                len(played) if isinstance(played, (list, tuple)) else 0,
                len(destroyed) if isinstance(destroyed, (list, tuple)) else 0,
            )
            raw_training_match_id = snapshot.get("training_match_id")
            training_match_id = (
                str(raw_training_match_id).strip()
                if isinstance(raw_training_match_id, str) and raw_training_match_id.strip()
                else ""
            )
            # New session snapshots carry the opaque per-match ID.  Keep the
            # legacy terminal ID too so rows written by older Tracker builds
            # continue to receive opponent-deck/card-turn backfill.
            match_ids = [legacy_match_id]
            if training_match_id and training_match_id != legacy_match_id:
                match_ids.insert(0, training_match_id)
            observed = collect_public_card_ids(opponent)
            for match_id in match_ids:
                if turn_observations is not None:
                    previous_self, previous_opponent = turn_observations.get(
                        match_id,
                        ((), ()),
                    )
                    merged_self = tuple(sorted(
                        set(previous_self) | set(extract_played_card_turns(mine)),
                        key=lambda item: (item[1], item[0]),
                    ))
                    merged_opponent = tuple(sorted(
                        set(previous_opponent) | set(extract_played_card_turns(opponent)),
                        key=lambda item: (item[1], item[0]),
                    ))
                    if merged_self or merged_opponent:
                        turn_observations[match_id] = (merged_self, merged_opponent)
                # A terminal snapshot can be emitted more than once.  Keep
                # the one with the most public evidence, never an earlier
                # short one.
                if len(observed) > len(observations.get(match_id, ())):
                    observations[match_id] = observed
    return observations


def _runtime_roots() -> tuple[Path, ...]:
    package_root = Path(__file__).resolve().parent
    runtime_root = Path(getattr(sys, "_MEIPASS", package_root.parent))
    executable_root = Path(sys.executable).resolve().parent
    return (package_root, runtime_root, executable_root, executable_root / "_internal")


def default_meta_decks_path() -> Path:
    """Return the user-editable cache path, falling back to packaged data."""
    override = os.environ.get("SHADOWVERSE_TRACKER_META_DECKS", "").strip()
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        user_path = Path(local_app_data) / "ShadowverseTracker" / "meta_decks.json"
        if user_path.is_file():
            return user_path
    for root in _runtime_roots():
        packaged = root / "data" / "meta_decks.json"
        if packaged.is_file():
            return packaged
        packaged = root / "shadowverse_tracker" / "data" / "meta_decks.json"
        if packaged.is_file():
            return packaged
    return Path(__file__).resolve().parent / "data" / "meta_decks.json"


def writable_meta_decks_path() -> Path:
    """Return the per-user destination used by cache refreshes."""
    override = os.environ.get("SHADOWVERSE_TRACKER_META_DECKS", "").strip()
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "ShadowverseTracker" / "meta_decks.json"
    return default_meta_decks_path()


def _normalise_cards(value: object) -> dict[int, int]:
    """Read either ``{card_id: count}`` or a list of IDs from JSON."""
    counts: Counter[int] = Counter()
    if isinstance(value, Mapping):
        items = value.items()
        for raw_id, raw_count in items:
            try:
                card_id = canonical_card_id(int(raw_id))
                count = int(raw_count)
            except (TypeError, ValueError):
                continue
            if card_id > 0 and count > 0:
                counts[card_id] += min(count, 3)
    elif isinstance(value, (list, tuple)):
        for raw_entry in value:
            raw_id = raw_entry
            count = 1
            if isinstance(raw_entry, Mapping):
                raw_id = (
                    raw_entry.get("base_card_id")
                    or raw_entry.get("baseCardId")
                    or raw_entry.get("card_id")
                    or raw_entry.get("cardId")
                    or raw_entry.get("id")
                )
                raw_count = (
                    raw_entry.get("count")
                    or raw_entry.get("quantity")
                    or raw_entry.get("num")
                    or raw_entry.get("amount")
                    or raw_entry.get("qty")
                )
                try:
                    count = min(max(int(raw_count), 1), 3)
                except (TypeError, ValueError):
                    count = 1
            elif isinstance(raw_entry, (list, tuple)) and raw_entry:
                raw_id = raw_entry[0]
                if len(raw_entry) > 1:
                    try:
                        count = min(max(int(raw_entry[1]), 1), 3)
                    except (TypeError, ValueError):
                        count = 1
            try:
                card_id = canonical_card_id(int(raw_id))
            except (TypeError, ValueError):
                continue
            if card_id > 0:
                counts[card_id] += count
    return dict(counts)


@dataclass(frozen=True)
class MetaDeckProfile:
    """One concrete 40-card build from a meta-deck source."""

    profile_id: str
    name: str
    class_id: int
    cards: Mapping[int, int]
    format: str = "rotation"
    archetype: str = ""
    source_url: str = DEFAULT_SOURCE_URL
    official_url: str = ""
    updated_at: str = ""
    tier: str = ""

    def __post_init__(self) -> None:
        # Runtime battle objects may carry style/evolution or CN-client IDs;
        # normalise profiles at construction too so hand-built/test profiles
        # behave exactly like JSON-loaded profiles.
        object.__setattr__(self, "cards", _normalise_cards(self.cards))
        canonical_archetype = meta_archetype_label(self.archetype, self.name)
        object.__setattr__(self, "archetype", canonical_archetype)
        object.__setattr__(self, "tier", meta_tier_label(self.tier, canonical_archetype))

    @property
    def display_name(self) -> str:
        """The stable website archetype label used in history/UI."""
        return str(self.archetype or self.name).strip()

    @property
    def card_ids(self) -> frozenset[int]:
        return frozenset(int(card_id) for card_id, count in self.cards.items() if int(count) > 0)

    @property
    def total_cards(self) -> int:
        return sum(max(0, int(count)) for count in self.cards.values())


def meta_core_card_candidates(profile: MetaDeckProfile) -> tuple[int, ...]:
    """Return the non-token, same-class legendary cards in one Meta build.

    Rarity comes from the bundled official card-list metadata.  Keeping the
    check here (rather than only in the Qt combo box) makes automatic core
    selection and persisted manual choices obey the same ``虹卡`` rule.
    """
    candidates = [
        int(card_id)
        for card_id, count in profile.cards.items()
        if int(count) > 0
        and is_legendary_card(int(card_id))
        and _card_class_id(int(card_id)) == int(profile.class_id)
        and not _is_token_card(int(card_id))
    ]
    return tuple(sorted(set(candidates)))


def _card_class_id(card_id: int) -> int:
    """Return the official class for a card, with an ID fallback.

    The compact effect catalog carries the authoritative class field.  The
    numeric ID layout is a useful fallback for an older/incomplete effect
    cache and still lets us reject neutral cards from core selection.
    """
    try:
        from .card_effects import get_card_effect

        effect = get_card_effect(card_id)
    except (ImportError, OSError, ValueError, TypeError):
        effect = None
    if effect is not None:
        try:
            return int(effect.class_id)
        except (TypeError, ValueError):
            pass
    try:
        return int(card_class_id(card_id))
    except (TypeError, ValueError):
        return -1


def meta_core_group_key(profile: MetaDeckProfile) -> str:
    """Return the stable key shared by concrete builds of one archetype.

    WBArts can publish several concrete lists for one archetype.  Their
    profile IDs are intentionally different, but a core-card choice should be
    shared by all of them.  Include the class so an identically named local
    deck in another class cannot accidentally inherit the choice.
    """
    label = str(profile.display_name or profile.archetype or profile.name).strip()
    return f"{int(profile.class_id)}:{label.casefold()}"


def _meta_core_group_members(
    profile: MetaDeckProfile,
    profiles: Iterable[MetaDeckProfile] = (),
) -> tuple[MetaDeckProfile, ...]:
    """Return all concrete profiles belonging to ``profile``'s archetype."""
    profiles_tuple = tuple(profiles)
    group_key = meta_core_group_key(profile)
    members = [
        item
        for item in profiles_tuple
        if meta_core_group_key(item) == group_key
    ]
    if not any(item.profile_id == profile.profile_id for item in members):
        members.insert(0, profile)
    # A caller may pass the selected profile twice; duplicate IDs would make
    # the "every concrete build" rule artificially stricter.
    unique: list[MetaDeckProfile] = []
    seen: set[str] = set()
    for item in members:
        if item.profile_id in seen:
            continue
        seen.add(item.profile_id)
        unique.append(item)
    return tuple(unique)


def meta_core_card_options_for_group(
    profile: MetaDeckProfile,
    profiles: Iterable[MetaDeckProfile] = (),
) -> tuple[int, ...]:
    """Return rainbow cards that can be chosen manually for an archetype.

    Automatic selection is deliberately narrower (see
    :func:`meta_core_card_candidates_for_group`), but a manual choice may be a
    rainbow card that occurs in only one of the concrete lists.  Keeping the
    union in the selector preserves that escape hatch while still applying
    the chosen card to every same-archetype profile.
    """
    members = _meta_core_group_members(profile, profiles)
    options: set[int] = set()
    for member in members:
        options.update(meta_core_card_candidates(member))
    return tuple(sorted(options))


def meta_core_card_candidates_for_group(
    profile: MetaDeckProfile,
    profiles: Iterable[MetaDeckProfile] = (),
) -> tuple[int, ...]:
    """Return automatically identifiable core cards for one archetype.

    A card is a candidate only when every concrete list in the archetype has
    at least two copies and no other archetype of the same class carries it.
    Returning no candidate when the exclusivity test fails is intentional:
    the caller then applies the documented highest-cost same-class rainbow
    fallback instead of presenting a misleading shared card as an automatic
    discriminator.
    """
    profiles_tuple = tuple(profiles)
    members = _meta_core_group_members(profile, profiles_tuple)
    candidate_sets = [set(meta_core_card_candidates(item)) for item in members]
    if not candidate_sets:
        return ()
    common = set.intersection(*candidate_sets)
    common = {
        card_id
        for card_id in common
        if all(int(member.cards.get(card_id, 0)) >= 2 for member in members)
    }
    if not common:
        return ()
    group_key = meta_core_group_key(profile)
    same_class_other = [
        item
        for item in profiles_tuple
        if int(item.class_id) == int(profile.class_id)
        and meta_core_group_key(item) != group_key
    ]
    exclusive = {
        card_id
        for card_id in common
        if not any(int(item.cards.get(card_id, 0)) > 0 for item in same_class_other)
    }
    return tuple(sorted(exclusive))


def meta_core_fallback_card_for_group(
    profile: MetaDeckProfile,
    profiles: Iterable[MetaDeckProfile] = (),
) -> int | None:
    """Choose the highest-cost same-class rainbow card for a group.

    This is used only when no common automatic candidate satisfies the
    cross-build rule.  It deliberately considers no neutral cards, and it may
    choose a card present in one concrete variant only so every same-archetype
    row still receives a stable visual/core value.
    """
    members = _meta_core_group_members(profile, profiles)
    options = meta_core_card_options_for_group(profile, members)
    if not options:
        return None

    def score(card_id: int) -> tuple[int, int, int, int]:
        metadata = get_card_metadata(card_id)
        cost = int(metadata.cost) if metadata is not None else 0
        counts = [int(member.cards.get(card_id, 0)) for member in members]
        return (cost, min(counts), sum(counts), -int(card_id))

    return max(options, key=score)


def _meta_core_override_value(
    profile: MetaDeckProfile,
    overrides: Mapping[object, object],
    members: Iterable[MetaDeckProfile],
) -> int | None:
    """Read a new group override or migrate an older profile-keyed value."""
    keys: list[str] = [meta_core_group_key(profile), profile.display_name]
    keys.extend(item.profile_id for item in members)
    seen: set[str] = set()
    for key in keys:
        key = str(key).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        raw = overrides.get(key)
        if raw in (None, ""):
            continue
        try:
            card_id = canonical_card_id(int(raw))
        except (TypeError, ValueError):
            continue
        if card_id > 0:
            return card_id
    return None


def _is_token_card(card_id: int) -> bool:
    """Read the official token flag without making card data mandatory."""
    try:
        # Imported lazily because card_effects itself imports card_catalog.
        from .card_effects import get_card_effect

        effect = get_card_effect(card_id)
    except (ImportError, OSError, ValueError, TypeError):
        return False
    if effect is not None:
        return bool(effect.is_token)
    # Generated/token IDs in WB are in the 900… namespace.  Treat an
    # unlisted 900… card conservatively as a token so an incomplete cache
    # cannot make the selector violate the rainbow-card rule.
    return str(abs(int(card_id))).startswith("900")


def select_meta_core_cards(
    profiles: Iterable[MetaDeckProfile],
    overrides: Mapping[object, object] | None = None,
) -> dict[str, int]:
    """Choose one shared rainbow core card for each Meta archetype.

    Several concrete lists can have the same archetype label.  Selection is
    therefore performed once per ``class + archetype`` group and the result is
    copied to every member.  Manual overrides accept both the new group key
    and the old profile-keyed JSON format, so existing user choices continue
    to work after the grouping change.  Groups without a qualifying common
    candidate fall back to their highest-cost same-class rainbow card.
    """
    profiles_tuple = tuple(profiles)
    override_values = overrides if isinstance(overrides, Mapping) else {}
    result: dict[str, int] = {}
    used_by_class: dict[int, set[int]] = {}

    groups: dict[str, list[MetaDeckProfile]] = {}
    for profile in profiles_tuple:
        groups.setdefault(meta_core_group_key(profile), []).append(profile)

    group_members = {
        key: tuple(members)
        for key, members in groups.items()
    }
    candidates_by_group = {
        key: meta_core_card_candidates_for_group(members[0], profiles_tuple)
        for key, members in group_members.items()
        if members
    }

    # Resolve manual choices once per group.  A manual card is allowed when it
    # appears in any concrete list; this is intentional because the user may
    # pick a representative card that is absent from a second published
    # variant, while the shared value still keeps the archetype consistent.
    manual_by_group: dict[str, int] = {}
    for key, members in group_members.items():
        raw_card = _meta_core_override_value(members[0], override_values, members)
        if raw_card is None:
            continue
        options = set(meta_core_card_options_for_group(members[0], members))
        if raw_card not in options:
            continue
        manual_by_group[key] = raw_card
        result.update({member.profile_id: raw_card for member in members})
        used_by_class.setdefault(int(members[0].class_id), set()).add(raw_card)

    # Count candidate occurrence by archetype (not by concrete list) so one
    # source publishing three variants does not outweigh a different type.
    frequency_by_class: dict[int, Counter[int]] = {}
    for key, members in group_members.items():
        frequencies = frequency_by_class.setdefault(int(members[0].class_id), Counter())
        frequencies.update(candidates_by_group.get(key, ()))

    pending = sorted(
        (
            (key, members)
            for key, members in group_members.items()
            if key not in manual_by_group
        ),
        key=lambda item: (
            len(candidates_by_group.get(item[0], ())),
            int(item[1][0].class_id),
            item[0],
        ),
    )
    for key, members in pending:
        candidates = candidates_by_group.get(key, ())
        if not candidates:
            fallback = meta_core_fallback_card_for_group(members[0], members)
            if fallback is None:
                continue
            result.update({member.profile_id: fallback for member in members})
            used_by_class.setdefault(int(members[0].class_id), set()).add(fallback)
            continue
        class_id = int(members[0].class_id)
        frequencies = frequency_by_class[class_id]
        used = used_by_class.setdefault(class_id, set())
        unused = [card_id for card_id in candidates if card_id not in used]
        pool = unused or list(candidates)

        def score(card_id: int) -> tuple[int, int, int, int, int]:
            metadata = get_card_metadata(card_id)
            cost = int(metadata.cost) if metadata is not None else 0
            counts = [int(member.cards.get(card_id, 0)) for member in members]
            # Lower occurrence among other archetypes is more distinguishing;
            # then prefer a card present in more copies across this group,
            # followed by a higher-cost headline card and stable ID order.
            return (
                int(frequencies.get(card_id, 0)),
                -min(counts),
                -sum(counts),
                -cost,
                int(card_id),
            )

        selected = min(pool, key=score)
        result.update({member.profile_id: selected for member in members})
        used.add(selected)
    return result


def meta_core_card_id(
    profile: MetaDeckProfile,
    profiles: Iterable[MetaDeckProfile],
    overrides: Mapping[object, object] | None = None,
) -> int | None:
    """Return the selected/manual core ID for ``profile`` when available."""
    return select_meta_core_cards(profiles, overrides).get(profile.profile_id)


def meta_profile_from_saved_deck(deck: object) -> MetaDeckProfile | None:
    """Convert one local ``SavedDeck`` into a Meta-page profile.

    The matcher intentionally accepts the repository object by duck typing so
    this module does not need to import the Qt-facing deck repository.  Local
    profiles are kept in memory and are never written into the WBArts cache;
    this keeps user decks available in Meta and opponent matching without
    mixing private data into the public cache.
    """
    key = str(getattr(deck, "key", "") or "").strip()
    name = str(getattr(deck, "name", "") or "").strip()
    if not key or not name:
        return None
    try:
        class_id = int(getattr(deck, "class_id", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not 0 <= class_id <= 7:
        return None
    format_value = getattr(deck, "format_version", "rotation")
    try:
        format_number = int(format_value)
    except (TypeError, ValueError):
        format_number = 0
    format_name = {1: "rotation", 2: "unlimited"}.get(
        format_number,
        str(format_value or "rotation").strip() or "rotation",
    )
    raw_cards = getattr(deck, "cards", ())
    if isinstance(raw_cards, Mapping):
        cards = _normalise_cards(raw_cards)
    else:
        entries: list[dict[str, int]] = []
        for card in raw_cards if isinstance(raw_cards, (list, tuple)) else ():
            try:
                card_id = int(getattr(card, "card_id", 0) or 0)
                count = int(getattr(card, "count", 0) or 0)
            except (TypeError, ValueError):
                continue
            if card_id > 0 and count > 0:
                entries.append({"card_id": card_id, "count": count})
        cards = _normalise_cards(entries)
    if not cards or sum(cards.values()) != 40:
        return None
    return MetaDeckProfile(
        profile_id=f"local-deck:{key}",
        name=name,
        class_id=class_id,
        cards=cards,
        format=format_name,
        archetype=name,
        source_url="local",
        updated_at="本地牌组",
        tier="",
    )


def canonicalize_meta_deck_labels(
    profiles: Iterable[MetaDeckProfile],
    references: Iterable[MetaDeckProfile] = (),
) -> tuple[MetaDeckProfile, ...]:
    """Replace WBArts ``local:*`` labels using the bundled archetype cache.

    WBArts' public list may expose an internal archetype identifier while the
    rendered site has the corresponding Chinese label in its Tier/Meta panel.
    The bundled cache contains the same concrete builds with stable labels, so
    card-list similarity provides a safe offline bridge after a refresh.  A
    label is only copied when the best same-class build is a clear match; an
    uncertain build remains visible by its original name rather than being
    assigned a misleading archetype.
    """
    current = tuple(profiles)
    known = tuple(
        profile for profile in references
        if profile.display_name and not is_machine_meta_label(profile.display_name)
    )
    if not known:
        return current

    def similarity(left: MetaDeckProfile, right: MetaDeckProfile) -> float:
        left_cards = left.cards
        right_cards = right.cards
        union = set(left_cards) | set(right_cards)
        if not union:
            return 0.0
        matched = sum(min(int(left_cards.get(card_id, 0)), int(right_cards.get(card_id, 0))) for card_id in union)
        total = sum(max(int(left_cards.get(card_id, 0)), int(right_cards.get(card_id, 0))) for card_id in union)
        return matched / max(1, total)

    result: list[MetaDeckProfile] = []
    for profile in current:
        if not is_machine_meta_label(profile.display_name):
            result.append(profile)
            continue
        candidates = [
            reference for reference in known
            if int(reference.class_id) == int(profile.class_id)
            and str(reference.format or "").casefold() == str(profile.format or "").casefold()
        ]
        ranked = sorted(
            ((similarity(profile, reference), reference) for reference in candidates),
            key=lambda item: (-item[0], item[1].profile_id),
        )
        if not ranked:
            result.append(profile)
            continue
        best_score, best = ranked[0]
        next_score = ranked[1][0] if len(ranked) > 1 else 0.0
        # Exact/near-exact build matches need no margin; looser matches must
        # beat the next candidate so a local ID is not mislabeled.
        clear_match = best_score >= 0.72 or (best_score >= 0.52 and best_score - next_score >= 0.04)
        if clear_match:
            result.append(replace(profile, archetype=best.display_name, tier=best.tier))
        else:
            result.append(profile)
    return tuple(result)


@dataclass(frozen=True)
class OpponentDeckMatch:
    """A ranked profile result for the cards observed so far."""

    profile: MetaDeckProfile
    confidence: float
    margin: float
    matched_cards: int
    observed_cards: int
    matched_distinct: int
    observed_distinct: int
    accepted: bool
    label_override: str = ""

    @property
    def label(self) -> str:
        return self.label_override or self.profile.name


@dataclass(frozen=True)
class OpponentPossibleCard:
    """A weighted estimate of one card still being in the opponent deck.

    The reader never exposes hidden opponent cards.  These values therefore
    describe the probability-weighted candidate set rather than an exact
    deck ledger.  estimated_deck_copies is scaled to the currently visible
    opponent deck count when that count is available.
    """

    card_id: int
    support_score: float
    expected_copies: float
    expected_unseen_copies: float
    estimated_deck_copies: float


@dataclass(frozen=True)
class OpponentDeckPrediction:
    """The complete live prediction used by the current-match UI."""

    candidates: tuple[OpponentDeckMatch, ...] = ()
    possible_cards: tuple[OpponentPossibleCard, ...] = ()
    confirmed: OpponentDeckMatch | None = None
    opponent_class_id: int | None = None
    opponent_deck_count: int | None = None
    observed_card_ids: tuple[int, ...] = ()
    observed_cards: int = 0
    observed_distinct: int = 0


def _profile_from_payload(item: object, index: int) -> MetaDeckProfile | None:
    if not isinstance(item, Mapping):
        return None
    raw_id = str(item.get("id") or item.get("profile_id") or f"profile-{index}").strip()
    name = str(item.get("name") or item.get("label") or "").strip()
    if not raw_id or not name:
        return None
    try:
        class_id = int(item.get("class_id"))
    except (TypeError, ValueError):
        return None
    if not 0 <= class_id <= 7:
        return None
    cards = _normalise_cards(item.get("cards"))
    if not cards or sum(cards.values()) <= 0:
        return None
    return MetaDeckProfile(
        profile_id=raw_id,
        name=name,
        class_id=class_id,
        cards=cards,
        format=str(item.get("format") or "rotation").strip() or "rotation",
        archetype=str(item.get("archetype") or "").strip(),
        source_url=str(item.get("source_url") or DEFAULT_SOURCE_URL).strip() or DEFAULT_SOURCE_URL,
        official_url=str(item.get("official_url") or "").strip(),
        updated_at=str(item.get("updated_at") or "").strip(),
        tier=meta_tier_label(item.get("tier") or item.get("category") or item.get("tier_name"), item.get("archetype")),
    )


def load_meta_deck_profiles(path: Path | None = None) -> tuple[MetaDeckProfile, ...]:
    """Load and validate cached profiles; malformed entries are ignored."""
    source = path or default_meta_decks_path()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ()
    raw_profiles = payload.get("profiles", ()) if isinstance(payload, Mapping) else payload
    if not isinstance(raw_profiles, list):
        return ()
    result: list[MetaDeckProfile] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_profiles):
        profile = _profile_from_payload(item, index)
        if profile is None or profile.profile_id in seen:
            continue
        seen.add(profile.profile_id)
        result.append(profile)
    return tuple(result)


def save_meta_deck_profiles(
    profiles: Iterable[MetaDeckProfile],
    path: Path | None = None,
    *,
    source_url: str = DEFAULT_SOURCE_URL,
    updated_at: str = "",
    checked_at: str = "",
    refresh_version: str = DEFAULT_META_REFRESH_VERSION,
) -> Path:
    """Write a validated cache suitable for a later offline match."""
    target = path or writable_meta_decks_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    items = []
    for profile in profiles:
        items.append({
            "id": profile.profile_id,
            "name": profile.name,
            "class_id": profile.class_id,
            "format": profile.format,
            "archetype": profile.archetype,
            "cards": {str(int(card_id)): int(count) for card_id, count in profile.cards.items() if int(count) > 0},
            "source_url": profile.source_url or source_url,
            "official_url": profile.official_url,
            "updated_at": profile.updated_at or updated_at,
            "tier": profile.tier,
        })
    temporary = target.with_suffix(target.suffix + ".tmp")
    document = {
        "schema_version": SCHEMA_VERSION,
        "source": source_url,
        "updated_at": updated_at,
        "profiles": items,
    }
    if checked_at:
        document["last_checked_at"] = checked_at
    if refresh_version:
        document["refresh_version"] = str(refresh_version)
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    return target


class OpponentDeckMatcher:
    """Rank cached profiles using public plays and revealed card evidence."""

    def __init__(
        self,
        profiles: Iterable[MetaDeckProfile] = (),
        *,
        min_observed_distinct: int = 3,
        min_confidence: float = 0.68,
        min_margin: float = 0.05,
    ) -> None:
        self.profiles = tuple(profiles)
        self.min_observed_distinct = max(1, int(min_observed_distinct))
        self.min_confidence = max(0.0, min(1.0, float(min_confidence)))
        self.min_margin = max(0.0, min(1.0, float(min_margin)))

    @staticmethod
    def _normalise_observed_card_ids(observed_card_ids: Iterable[int]) -> tuple[int, ...]:
        result: list[int] = []
        for raw_id in observed_card_ids:
            try:
                card_id = canonical_card_id(int(raw_id))
            except (TypeError, ValueError):
                continue
            if card_id > 0:
                result.append(card_id)
        return tuple(result)

    def _score(self, profile: MetaDeckProfile, observed: Counter[int]) -> tuple[float, int, int]:
        matched = 0
        matched_distinct = 0
        # Token/effect IDs are public but are not part of the saved deck list.
        # Ignore them in the denominator when no candidate contains them; they
        # otherwise make a correct archetype look falsely low-confidence.
        candidate_card_ids = {
            card_id
            for candidate in self.profiles
            if candidate.class_id == profile.class_id
            for card_id in candidate.cards
        }
        relevant_total = 0
        relevant_distinct = 0
        for card_id, observed_count in observed.items():
            if card_id not in candidate_card_ids:
                continue
            relevant_total += observed_count
            relevant_distinct += 1
            available = max(0, int(profile.cards.get(card_id, 0)))
            matched_count = min(observed_count, available)
            matched += matched_count
            if matched_count > 0:
                matched_distinct += 1
        if relevant_total <= 0:
            return 0.0, matched, matched_distinct
        coverage = matched / relevant_total
        distinct_coverage = matched_distinct / max(1, relevant_distinct)
        score = 0.72 * coverage + 0.28 * distinct_coverage
        return score, matched, matched_distinct

    def rank(
        self,
        observed_card_ids: Iterable[int],
        opponent_class_id: int | None = None,
    ) -> tuple[OpponentDeckMatch, ...]:
        observed: Counter[int] = Counter()
        for card_id in self._normalise_observed_card_ids(observed_card_ids):
            observed[card_id] += 1
        candidates = [
            profile for profile in self.profiles
            if opponent_class_id is None or profile.class_id == int(opponent_class_id)
        ]
        ranked: list[OpponentDeckMatch] = []
        ordered = sorted(
            ((self._score(profile, observed), profile) for profile in candidates),
            key=lambda value: (-value[0][0], value[1].profile_id),
        )
        candidate_card_ids = {
            card_id
            for profile in candidates
            for card_id in profile.cards
        }
        relevant_observed = Counter({
            card_id: count
            for card_id, count in observed.items()
            if card_id in candidate_card_ids
        })
        observed_total = sum(relevant_observed.values())
        observed_distinct = len(relevant_observed)
        # Variants of one archetype should not compete with each other for the
        # confidence margin.  The UI displays the archetype label, so compare
        # the best concrete build from each different archetype.
        best_score_by_archetype: dict[str, float] = {}
        for score_data, profile in ordered:
            archetype = str(profile.archetype or profile.name or "").strip()
            best_score_by_archetype[archetype] = max(
                best_score_by_archetype.get(archetype, 0.0),
                float(score_data[0]),
            )
        for index, (score_data, profile) in enumerate(ordered):
            score = float(score_data[0])
            matched = int(score_data[1])
            matched_distinct = int(score_data[2])
            archetype = str(profile.archetype or profile.name or "").strip()
            next_score = max(
                (
                    other_score
                    for other_archetype, other_score in best_score_by_archetype.items()
                    if other_archetype != archetype
                ),
                default=0.0,
            )
            ranked.append(OpponentDeckMatch(
                profile=profile,
                confidence=round(max(0.0, min(1.0, score)), 4),
                margin=round(max(0.0, float(score) - float(next_score)), 4),
                matched_cards=matched,
                observed_cards=observed_total,
                matched_distinct=matched_distinct,
                observed_distinct=observed_distinct,
                accepted=(
                    observed_distinct >= self.min_observed_distinct
                    and score >= self.min_confidence
                    and score - next_score >= self.min_margin
                ),
            ))
        return tuple(ranked)

    @staticmethod
    def _group_ranked_by_archetype(
        ranked: Iterable[OpponentDeckMatch],
    ) -> tuple[OpponentDeckMatch, ...]:
        """Collapse concrete builds so variants do not flood the UI.

        Meta caches often contain several published lists for one archetype.
        The best-scoring concrete list remains attached to the row so its
        card composition can drive the remaining-card estimate, while the
        user-facing label is the stable archetype name.
        """
        result: list[OpponentDeckMatch] = []
        seen: set[str] = set()
        for item in ranked:
            label = str(item.profile.display_name or item.profile.name).strip()
            if not label or label in seen:
                continue
            seen.add(label)
            result.append(replace(item, label_override=label))
        return tuple(result)

    def _match_ranked(
        self,
        ranked: tuple[OpponentDeckMatch, ...],
    ) -> OpponentDeckMatch | None:
        """Apply the conservative confirmation rule to an existing ranking."""
        if not ranked or not ranked[0].accepted:
            # Two cached builds can share every publicly played card.  In
            # that case an exact build would be a guess, but a shared
            # archetype is still useful to the match-history table.  Accept
            # the archetype only when the tied candidates all agree and the
            # next *different* archetype is clearly behind them.
            if not ranked:
                return None
            top = ranked[0]
            if top.observed_distinct < self.min_observed_distinct or top.confidence < self.min_confidence:
                return None
            archetype = str(top.profile.archetype or "").strip()
            if not archetype:
                return None
            tied = [
                item for item in ranked
                if abs(float(item.confidence) - float(top.confidence)) <= 0.0001
            ]
            if not tied or any(str(item.profile.archetype or "").strip() != archetype for item in tied):
                return None
            different = [
                item for item in ranked
                if str(item.profile.archetype or "").strip() != archetype
            ]
            next_score = max((float(item.confidence) for item in different), default=0.0)
            margin = round(float(top.confidence) - next_score, 4)
            if margin < self.min_margin:
                return None
            return replace(top, margin=margin, accepted=True, label_override=archetype)
        top = ranked[0]
        label = top.profile.display_name
        return replace(top, label_override=label) if label else top

    def prediction(
        self,
        observed_card_ids: Iterable[int],
        opponent_class_id: int | None = None,
        opponent_deck_count: int | None = None,
        *,
        max_candidates: int = 5,
        max_possible_cards: int = 6,
    ) -> OpponentDeckPrediction:
        """Return live candidates and weighted possible remaining cards.

        observed_card_ids must contain only public evidence.  Each profile is
        weighted by its current match score, then the expected unseen copy
        count is scaled to the opponent's visible deck count.  This mirrors
        the useful part of WBCapture's prediction view without pretending to
        know hidden hand/deck identities.
        """
        observed = self._normalise_observed_card_ids(observed_card_ids)
        ranked = self.rank(observed, opponent_class_id)
        grouped = self._group_ranked_by_archetype(ranked)
        try:
            candidate_limit = max(1, int(max_candidates))
        except (TypeError, ValueError):
            candidate_limit = 5
        # Before the first relevant public card there is no meaningful top-N:
        # showing arbitrary zero-score profiles would look like a prediction
        # and is less useful than an explicit waiting state.
        meaningful = tuple(item for item in grouped if item.confidence > 0.0)
        candidates = (
            meaningful[:candidate_limit]
            if ranked and ranked[0].observed_cards > 0
            else ()
        )
        confirmed = self._match_ranked(ranked)

        deck_count: int | None
        try:
            deck_count = int(opponent_deck_count) if opponent_deck_count is not None else None
        except (TypeError, ValueError):
            deck_count = None
        if deck_count is not None and deck_count < 0:
            deck_count = None

        possible: tuple[OpponentPossibleCard, ...] = ()
        observed_counts = Counter(observed)
        if candidates and observed:
            weights = [max(0.0, float(item.confidence)) for item in candidates]
            total_weight = sum(weights)
            if total_weight > 0.0:
                expected: dict[int, float] = {}
                unseen: dict[int, float] = {}
                support: dict[int, float] = {}
                for item, raw_weight in zip(candidates, weights):
                    weight = raw_weight / total_weight
                    for raw_card_id, raw_count in item.profile.cards.items():
                        try:
                            card_id = canonical_card_id(int(raw_card_id))
                            count = max(0, int(raw_count))
                        except (TypeError, ValueError):
                            continue
                        if card_id <= 0 or count <= 0:
                            continue
                        remaining = max(0, count - observed_counts.get(card_id, 0))
                        expected[card_id] = expected.get(card_id, 0.0) + weight * count
                        unseen[card_id] = unseen.get(card_id, 0.0) + weight * remaining
                        if remaining > 0:
                            support[card_id] = support.get(card_id, 0.0) + weight

                total_unseen = sum(unseen.values())
                scale = (
                    float(deck_count) / total_unseen
                    if deck_count is not None and deck_count > 0 and total_unseen > 0
                    else 1.0
                )
                rows = [
                    OpponentPossibleCard(
                        card_id=card_id,
                        support_score=round(max(0.0, min(1.0, support.get(card_id, 0.0))), 4),
                        expected_copies=round(expected_count, 4),
                        expected_unseen_copies=round(unseen_count, 4),
                        estimated_deck_copies=round(unseen_count * scale, 4),
                    )
                    for card_id, expected_count in expected.items()
                    for unseen_count in (unseen.get(card_id, 0.0),)
                    if unseen_count > 0.0
                ]
                rows.sort(
                    key=lambda item: (
                        -item.estimated_deck_copies,
                        -item.support_score,
                        -item.expected_unseen_copies,
                        item.card_id,
                    )
                )
                try:
                    possible_limit = max(1, int(max_possible_cards))
                except (TypeError, ValueError):
                    possible_limit = 6
                possible = tuple(rows[:possible_limit])

        return OpponentDeckPrediction(
            candidates=candidates,
            possible_cards=possible,
            confirmed=confirmed,
            opponent_class_id=(
                int(opponent_class_id)
                if isinstance(opponent_class_id, int) and not isinstance(opponent_class_id, bool)
                else None
            ),
            opponent_deck_count=deck_count,
            observed_card_ids=observed,
            observed_cards=len(observed),
            observed_distinct=len(set(observed)),
        )

    # "predict" is a short alias for callers that prefer the verb form.
    predict = prediction

    def match(
        self,
        observed_card_ids: Iterable[int],
        opponent_class_id: int | None = None,
    ) -> OpponentDeckMatch | None:
        return self._match_ranked(self.rank(observed_card_ids, opponent_class_id))


__all__ = [
    "DEFAULT_SOURCE_URL",
    "DEFAULT_META_REFRESH_VERSION",
    "MetaDeckProfile",
    "meta_profile_from_saved_deck",
    "META_ARCHETYPE_TIERS",
    "META_ARCHETYPE_ORDER",
    "META_LOCAL_ARCHETYPE_LABELS",
    "META_TIER_ORDER",
    "OpponentDeckMatch",
    "OpponentDeckPrediction",
    "OpponentDeckMatcher",
    "OpponentPossibleCard",
    "canonicalize_meta_deck_labels",
    "default_meta_decks_path",
    "is_machine_meta_label",
    "load_session_opponent_observations",
    "collect_public_card_ids",
    "collect_public_played_card_ids",
    "load_meta_deck_profiles",
    "save_meta_deck_profiles",
    "meta_tier_label",
    "meta_archetype_label",
    "meta_archetype_sort_key",
    "meta_core_card_candidates",
    "meta_core_card_candidates_for_group",
    "meta_core_card_options_for_group",
    "meta_core_fallback_card_for_group",
    "meta_core_group_key",
    "meta_core_card_id",
    "select_meta_core_cards",
    "writable_meta_decks_path",
]
