"""Import concrete deck builds from the WBArts deck-square API.

The API is useful for refreshing the offline matcher cache, but it must not be
called from the 50 ms game polling loop.  WBArts is protected by Cloudflare on
some networks, so callers get a precise error and can keep using the last
known cache instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from typing import Iterable, Mapping

from .opponent_deck_matcher import (
    DEFAULT_META_REFRESH_VERSION,
    DEFAULT_SOURCE_URL,
    META_TIER_ORDER,
    MetaDeckProfile,
    _normalise_cards,
    canonicalize_meta_deck_labels,
    default_meta_decks_path,
    load_meta_deck_profiles,
    meta_archetype_label,
    meta_tier_label,
    save_meta_deck_profiles,
    writable_meta_decks_path,
)


class MetaDeckSourceError(RuntimeError):
    """A refresh failed without invalidating the existing local cache."""


META_LIST_URLS = (
    # Do not pin the environment in the preferred requests; WBArts changes
    # the active environment over time.  The env=10009 variants remain as a
    # compatibility fallback for older deployments of the site.
    "https://sva.hypd.asia/api/decks?format=rotation&sort=recommended&days=7",
    "https://sva.hypd.asia/api/decks?format=rotation&sort=recommended",
    "https://sva.hypd.asia/api/deck?format=rotation&sort=recommended&days=7",
    "https://sva.hypd.asia/api/deck?format=rotation&sort=recommended",
    "https://sva.hypd.asia/api/decks?format=rotation&env=10009&sort=recommended&days=7",
    "https://sva.hypd.asia/api/decks?format=rotation&env=10009&sort=recommended",
    "https://sva.hypd.asia/api/deck?format=rotation&env=10009&sort=recommended&days=7",
    "https://sva.hypd.asia/api/deck?format=rotation&env=10009&sort=recommended",
    "https://sva.hypd.asia/api/decks?format=1&env=10009&sort=recommended&days=7",
    "https://sva.hypd.asia/api/decks?format=1&env=10009&sort=recommended",
)

META_CLASS_IDS = tuple(range(1, 8))
RECOMMENDED_DECKS_PER_CLASS = 3
RECOMMENDED_DECKS_PER_ARCHETYPE = 2
# WBArts currently accepts the archetype query parameter only as a hint: for
# some IDs (notably the Royal T3 ``local:34`` row) the first results still
# belong to another archetype.  Fetch a bounded page and exact-filter locally.
ARCHETYPE_QUERY_LIMIT = 60
META_REFRESH_VERSION = DEFAULT_META_REFRESH_VERSION

# The Tier/Meta page currently exposes these archetypes in T1–T4.  They are
# kept as a compatibility fallback for older WBArts deployments that do not
# expose ``/api/tiers`` yet.  The endpoint remains authoritative when it is
# available, so a future rotation can change this list without a code update.
STATIC_TIER_ARCHETYPE_TARGETS = (
    ("local:3", 5, "中速梦", "T1"),
    ("local:6", 4, "跳费龙", "T1"),
    ("local:12", 6, "护符教", "T2"),
    ("local:1", 7, "造物超", "T2"),
    ("local:17", 1, "连击妖", "T2"),
    ("local:7", 3, "实验法", "T2"),
    ("local:15", 5, "谢幕梦", "T2"),
    ("local:34", 2, "旗皇", "T3"),
    ("local:9", 6, "进化教", "T3"),
    ("local:13", 2, "财宝皇", "T3"),
    ("local:8", 4, "脸龙", "T3"),
    ("local:5", 2, "协作皇", "T3"),
    ("local:11", 3, "增幅法", "T3"),
    ("local:4", 5, "快梦", "T3"),
    ("local:24", 6, "骰子教", "T4"),
    ("local:42", 7, "OTK超", "T4"),
    ("local:22", 7, "进化超", "T4"),
    ("local:14", 5, "验牌梦", "T4"),
    ("local:10", 1, "节奏妖", "T4"),
    ("local:33", 7, "宇宙超", "T4"),
    ("local:2", 5, "进化梦", "T4"),
    ("local:20", 1, "节奏进化妖", "T4"),
    ("local:26", 1, "进化妖", "T4"),
    ("local:44", 1, "疾驰妖", "T4"),
    ("local:43", 5, "魔神梦", "T4"),
    ("local:40", 2, "中速皇", "T4"),
    ("local:32", 6, "疾驰教", "T4"),
)

META_TIER_URLS = (
    "https://sva.hypd.asia/api/tiers?format=rotation",
    "https://sva.hypd.asia/api/tiers?format=1&env=10009",
)

_CLASS_ALIASES = {
    "forest": 1,
    "forestcraft": 1,
    "elf": 1,
    "精灵": 1,
    "sword": 2,
    "swordcraft": 2,
    "royal": 2,
    "皇家护卫": 2,
    "rune": 3,
    "runecraft": 3,
    "witch": 3,
    "巫师": 3,
    "dragon": 4,
    "dragoncraft": 4,
    "龙族": 4,
    "nightmare": 5,
    "abyss": 5,
    "abysscraft": 5,
    "梦魇": 5,
    "haven": 6,
    "havencraft": 6,
    "bishop": 6,
    "主教": 6,
    "portal": 7,
    "portalcraft": 7,
    "nemesis": 7,
    "超越者": 7,
}


def _class_id(value: object) -> int | None:
    if isinstance(value, Mapping):
        value = value.get("id") or value.get("class_id") or value.get("name")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = _CLASS_ALIASES.get(str(value or "").strip().casefold())
    return parsed if isinstance(parsed, int) and 0 <= parsed <= 7 else None


def _deck_id(value: str | int) -> str:
    raw = str(value).strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        parts = [part for part in urlsplit(raw).path.split("/") if part]
        if len(parts) < 2 or parts[0].casefold() != "deck":
            raise MetaDeckSourceError("WBArts 链接应为 /deck/<id>")
        raw = parts[1]
    if not raw.isdigit() or int(raw) <= 0:
        raise MetaDeckSourceError(f"无效的 WBArts 卡组编号：{raw or '(空)'}")
    return raw


def _request_json(url: str, *, timeout: float) -> object:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            "Referer": DEFAULT_SOURCE_URL,
            "Origin": "https://sva.hypd.asia",
            "User-Agent": "ShadowverseTracker meta-deck cache refresh",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (HTTPError, URLError, OSError) as exc:
        raise MetaDeckSourceError(f"WBArts 卡组请求失败：{exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MetaDeckSourceError(
            "WBArts 未返回 JSON（可能需要浏览器通过 Cloudflare 验证）；保留现有缓存"
        ) from exc


def _iter_deck_records(value: object) -> Iterable[Mapping[str, object]]:
    """Find deck records in the several list shapes used by WBArts builds."""
    if isinstance(value, Mapping):
        has_id = any(value.get(key) not in (None, "") for key in ("id", "deck_id", "deckId", "deck_code"))
        # List endpoints commonly return ID-only rows.  Keep those rows so
        # ``fetch_wbarts_meta_decks`` can request the concrete 40-card build
        # from the detail endpoint; wrappers without a deck identity are
        # traversed below instead of being mistaken for a profile.
        if has_id:
            yield value
        for key in ("deck", "decks", "items", "data", "results", "rows", "profiles"):
            child = value.get(key)
            if child is not None:
                yield from _iter_deck_records(child)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_deck_records(item)


def _record_id(value: Mapping[str, object]) -> str:
    for key in ("id", "deck_id", "deckId", "deck_code"):
        raw = value.get(key)
        if raw not in (None, ""):
            try:
                return _deck_id(str(raw))
            except MetaDeckSourceError:
                continue
    return ""


def _record_archetype(value: Mapping[str, object]) -> str:
    raw = (
        value.get("archetype")
        or value.get("archetype_name")
        or value.get("archetypeName")
        or value.get("deck_type")
        or value.get("deckType")
        or value.get("deck_category")
        or value.get("deckCategory")
        or value.get("meta_type")
        or value.get("metaType")
        or value.get("category_name")
        or value.get("categoryName")
        or value.get("meta_name")
        or value.get("metaName")
        or ""
    )
    if isinstance(raw, Mapping):
        raw = raw.get("name") or raw.get("label") or raw.get("title") or ""
    return str(raw or "").strip()


def _record_tier(value: Mapping[str, object]) -> str:
    raw = (
        value.get("tier")
        or value.get("tier_name")
        or value.get("tierName")
        or value.get("category")
        or value.get("category_name")
        or value.get("tier_id")
        or value.get("tierId")
        or value.get("rank")
        or value.get("meta_tier")
        or ""
    )
    if isinstance(raw, Mapping):
        raw = raw.get("name") or raw.get("label") or raw.get("tier") or raw.get("value") or ""
    return meta_tier_label(raw, _record_archetype(value))


def _record_class_id(value: Mapping[str, object]) -> int | None:
    raw = (
        value.get("class_id")
        or value.get("classId")
        or value.get("craft_id")
        or value.get("craftId")
        or value.get("craft")
        or value.get("class")
    )
    return _class_id(raw)


def _iter_tier_rows(value: object, tier_hint: str = "") -> Iterable[tuple[Mapping[str, object], str]]:
    """Yield archetype rows from the versioned WBArts tier response."""
    if isinstance(value, Mapping):
        row_id = value.get("id") or value.get("archetype")
        if row_id not in (None, "") and _record_class_id(value) is not None:
            yield value, tier_hint
        tiers = value.get("tiers")
        if isinstance(tiers, Mapping):
            for tier_name, rows in tiers.items():
                yield from _iter_tier_rows(rows, str(tier_name or tier_hint))
        for key in ("views", "combined", "items", "data", "results", "rows"):
            child = value.get(key)
            if child is not None:
                yield from _iter_tier_rows(child, tier_hint)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_tier_rows(item, tier_hint)


def _fetch_meta_tier_archetypes(*, timeout: float) -> tuple[tuple[str, int, str, str], ...]:
    """Read the current T1–T4 archetypes, with a stable offline fallback."""
    static_by_id = {item[0].casefold(): item for item in STATIC_TIER_ARCHETYPE_TARGETS}
    discovered: dict[str, tuple[str, int, str, str]] = {}
    for url in META_TIER_URLS:
        try:
            payload = _request_json(url, timeout=timeout)
        except MetaDeckSourceError:
            continue
        for row, tier_hint in _iter_tier_rows(payload):
            raw_id = row.get("id") or row.get("archetype")
            archetype_id = str(raw_id or "").strip()
            class_id = _record_class_id(row)
            tier = meta_tier_label(row.get("tier") or tier_hint, "")
            # The site puts currently unranked/legacy rotation archetypes in
            # ``其他``.  They are still useful Meta builds, so surface them in
            # the app's T4 section rather than dropping them during refresh.
            if tier == "其他":
                tier = "T4"
            if not archetype_id or class_id not in META_CLASS_IDS or tier not in {"T1", "T2", "T3", "T4"}:
                continue
            static = static_by_id.get(archetype_id.casefold())
            display_name = str(
                row.get("display_name")
                or row.get("displayName")
                or row.get("name")
                or row.get("label")
                or ""
            ).strip()
            display_name = meta_archetype_label(display_name, archetype_id)
            if static is not None:
                # Keep the app's canonical Chinese names even if a response
                # is served with an internal/local label.
                display_name = static[2]
            if not display_name:
                display_name = archetype_id
            discovered[archetype_id.casefold()] = (
                archetype_id,
                int(class_id),
                display_name,
                tier,
            )
        if discovered:
            break

    # Append known rows missing
    # from a partial/older response so a temporary API omission cannot make a
    # previously supported archetype disappear from the refresh target set.
    for archetype_id, class_id, display_name, tier in STATIC_TIER_ARCHETYPE_TARGETS:
        discovered.setdefault(
            archetype_id.casefold(),
            (archetype_id, class_id, display_name, tier),
        )
    order = {item[0].casefold(): index for index, item in enumerate(STATIC_TIER_ARCHETYPE_TARGETS)}
    return tuple(
        sorted(
            discovered.values(),
            key=lambda item: (order.get(item[0].casefold(), len(order)), item[0]),
        )
    )


def _meta_list_urls(
    class_id: int | None = None,
    *,
    archetype: str | None = None,
    limit: int | None = None,
) -> tuple[str, ...]:
    """Return API candidates with optional class/archetype filters."""
    if class_id is None and not archetype and limit is None:
        return META_LIST_URLS
    result: list[str] = []
    for url in META_LIST_URLS:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if class_id is not None:
            query["class_id"] = str(int(class_id))
        if archetype:
            query["archetype"] = str(archetype)
        if limit is None:
            limit = RECOMMENDED_DECKS_PER_ARCHETYPE if archetype else RECOMMENDED_DECKS_PER_CLASS
        requested_limit = max(1, int(limit))
        if archetype:
            requested_limit = max(requested_limit, ARCHETYPE_QUERY_LIMIT)
        query["limit"] = str(requested_limit)
        result.append(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)))
    return tuple(result)


def _fetch_meta_deck_list(
    *,
    timeout: float,
    class_id: int | None = None,
    archetype: str | None = None,
    limit: int | None = None,
) -> tuple[Mapping[str, object], ...]:
    last_error: MetaDeckSourceError | None = None
    for url in _meta_list_urls(class_id, archetype=archetype, limit=limit):
        try:
            payload = _request_json(url, timeout=timeout)
        except MetaDeckSourceError as exc:
            last_error = exc
            continue
        records: list[Mapping[str, object]] = []
        seen: set[str] = set()
        for item in _iter_deck_records(payload):
            deck_id = _record_id(item)
            if not deck_id or deck_id in seen:
                continue
            seen.add(deck_id)
            records.append(item)
        if records:
            return tuple(records)
    raise last_error or MetaDeckSourceError("WBArts 未返回可用的 Meta 卡组列表")


def parse_wbarts_deck_payload(
    payload: object,
    *,
    fallback_id: str | int = "",
    source_url: str = DEFAULT_SOURCE_URL,
    name: str = "",
    archetype_override: str = "",
    tier: str = "",
) -> MetaDeckProfile:
    """Convert ``GET /api/decks/<id>`` JSON into a matcher profile."""
    if not isinstance(payload, dict):
        raise MetaDeckSourceError("WBArts 返回的卡组数据不是 JSON 对象")
    deck = payload.get("deck", payload)
    for _ in range(3):
        if not isinstance(deck, dict) or any(
            deck.get(key) not in (None, "")
            for key in ("id", "deck_id", "deckId", "cards", "deck_cards", "card_list")
        ):
            break
        nested = next(
            (deck.get(key) for key in ("deck", "data", "result", "item") if isinstance(deck.get(key), dict)),
            None,
        )
        if not isinstance(nested, dict):
            break
        deck = nested
    if not isinstance(deck, dict):
        raise MetaDeckSourceError("WBArts 返回中缺少 deck 对象")
    profile_id = str(
        deck.get("id") or deck.get("deck_id") or deck.get("deckId") or fallback_id or ""
    ).strip()
    archetype = str(
        archetype_override
        or _record_archetype(deck)
        or deck.get("archetype")
        or deck.get("deck_type")
        or deck.get("category_name")
        or ""
    ).strip()
    deck_name = str(
        name
        or deck.get("name")
        or deck.get("title")
        or deck.get("label")
        or archetype
        or deck.get("source_name")
        or ""
    ).strip()
    class_id = _class_id(deck.get("class_id") or deck.get("classId") or deck.get("craft") or deck.get("class"))
    if class_id is None:
        raise MetaDeckSourceError("WBArts 卡组缺少有效职业编号")
    if not profile_id or not deck_name:
        raise MetaDeckSourceError("WBArts 卡组基本字段不完整")
    cards = (
        deck.get("cards")
        or deck.get("deck_cards")
        or deck.get("card_list")
        or deck.get("main_deck")
        or deck.get("decklist")
    )
    if isinstance(cards, Mapping):
        nested_cards = cards.get("cards") or cards.get("items") or cards.get("list")
        if isinstance(nested_cards, (dict, list, tuple)):
            cards = nested_cards
    if not isinstance(cards, (dict, list, tuple)):
        raise MetaDeckSourceError("WBArts 卡组缺少 cards 字段")
    profile = MetaDeckProfile(
        profile_id=f"wbarts-{profile_id}",
        name=deck_name,
        class_id=class_id,
        cards=_normalise_cards(cards),
        format=str(deck.get("format") or deck.get("format_id") or "rotation"),
        archetype=archetype or name,
        source_url=source_url,
        official_url=str(deck.get("official_url") or deck.get("portal_url") or ""),
        updated_at=str(deck.get("updated_at") or ""),
        tier=meta_tier_label(
            tier
            or deck.get("tier")
            or deck.get("tier_name")
            or deck.get("tierName")
            or deck.get("category")
            or deck.get("rank"),
            archetype or name,
        ),
    )
    if profile.total_cards != 40:
        raise MetaDeckSourceError(f"WBArts 卡组应为 40 张，实际为 {profile.total_cards} 张")
    return profile


def fetch_wbarts_deck(
    value: str | int,
    *,
    timeout: float = 15.0,
    name: str = "",
    archetype: str = "",
    tier: str = "",
) -> MetaDeckProfile:
    """Fetch one profile for a cache-refresh command, never for live polling."""
    deck_id = _deck_id(value)
    source_url = f"{DEFAULT_SOURCE_URL}/{deck_id}"
    last_error: MetaDeckSourceError | None = None
    for endpoint in (f"https://sva.hypd.asia/api/decks/{deck_id}", f"https://sva.hypd.asia/api/deck/{deck_id}"):
        try:
            payload = _request_json(endpoint, timeout=timeout)
            return parse_wbarts_deck_payload(
                payload,
                fallback_id=deck_id,
                source_url=source_url,
                name=name,
                archetype_override=archetype,
                tier=tier,
            )
        except MetaDeckSourceError as exc:
            last_error = exc
    raise last_error or MetaDeckSourceError("WBArts 卡组详情请求失败")


def fetch_wbarts_meta_decks(
    *,
    timeout: float = 15.0,
    max_profiles: int = 120,
    recommended_per_class: int = RECOMMENDED_DECKS_PER_CLASS,
    recommended_per_archetype: int = RECOMMENDED_DECKS_PER_ARCHETYPE,
    class_ids: Iterable[int] = META_CLASS_IDS,
) -> tuple[MetaDeckProfile, ...]:
    """Fetch concrete builds for the current Tier/Meta archetypes.

    When all seven classes are requested (the normal cache refresh), the
    current ``/api/tiers`` rows are queried one by one and each T1–T4
    archetype receives two builds.  This avoids the old class-only sampling
    bug where Royal/other classes could consume the whole limit before lower
    tier archetypes were seen.  Callers requesting a subset of classes retain
    the legacy class-query behavior for compatibility with older deployments.
    """
    profiles: list[MetaDeckProfile] = []
    seen: set[str] = set()
    failures: list[MetaDeckSourceError] = []
    covered_classes: set[int] = set()
    covered_archetypes: dict[str, int] = {}
    per_class = max(1, int(recommended_per_class))
    per_archetype = max(1, int(recommended_per_archetype))
    limit = max(1, int(max_profiles))

    def add_records(
        records: Iterable[Mapping[str, object]],
        expected_class: int | None,
        *,
        expected_archetype: str = "",
        expected_label: str = "",
        expected_tier: str = "",
        group_limit: int,
    ) -> None:
        added_for_class = 0
        archetype_key = str(expected_archetype or "").strip().casefold()
        current_for_archetype = covered_archetypes.get(archetype_key, 0) if archetype_key else 0
        # Keep a little headroom when an ID-only list contains rows from more
        # than one class; the detail response is the final class authority.
        for item in records:
            if len(profiles) >= limit or added_for_class >= group_limit:
                break
            listed_class = _record_class_id(item)
            if expected_class is not None and listed_class is not None and listed_class != expected_class:
                continue
            if archetype_key:
                listed_archetype = _record_archetype(item).casefold()
                if listed_archetype and listed_archetype != archetype_key:
                    continue
            deck_id = _record_id(item)
            if not deck_id or deck_id in seen:
                continue
            try:
                profile = parse_wbarts_deck_payload(
                    item,
                    fallback_id=deck_id,
                    source_url=f"{DEFAULT_SOURCE_URL}/{deck_id}",
                    name=_record_archetype(item),
                    archetype_override=expected_label,
                    tier=expected_tier or _record_tier(item),
                )
            except MetaDeckSourceError:
                try:
                    profile = fetch_wbarts_deck(
                        deck_id,
                        timeout=timeout,
                        name=_record_archetype(item),
                        archetype=expected_label,
                        tier=expected_tier or _record_tier(item),
                    )
                except MetaDeckSourceError:
                    continue
            if expected_class is not None and int(profile.class_id) != int(expected_class):
                continue
            if expected_label and profile.display_name != expected_label:
                continue
            seen.add(deck_id)
            profiles.append(profile)
            added_for_class += 1
            if expected_class is not None:
                covered_classes.add(int(expected_class))
            if archetype_key:
                current_for_archetype += 1
                covered_archetypes[archetype_key] = current_for_archetype

    normalized_classes: list[int] = []
    for value in class_ids:
        try:
            class_id = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= class_id <= 7 and class_id not in normalized_classes:
            normalized_classes.append(class_id)
    use_tier_targets = set(normalized_classes) == set(META_CLASS_IDS)
    tier_targets = _fetch_meta_tier_archetypes(timeout=timeout) if use_tier_targets else ()
    if tier_targets:
        for archetype_id, class_id, display_name, tier in tier_targets:
            if class_id not in normalized_classes or len(profiles) >= limit:
                continue
            try:
                records = _fetch_meta_deck_list(
                    timeout=timeout,
                    class_id=class_id,
                    archetype=archetype_id,
                    limit=per_archetype,
                )
            except MetaDeckSourceError as exc:
                failures.append(exc)
                continue
            add_records(
                records,
                class_id,
                expected_archetype=archetype_id,
                expected_label=display_name,
                expected_tier=tier,
                group_limit=per_archetype,
            )
    else:
        # Subset callers (and an unexpectedly empty tier response) keep the
        # previous class-level recommendation behavior.
        for class_id in normalized_classes:
            if len(profiles) >= limit:
                break
            try:
                records = _fetch_meta_deck_list(timeout=timeout, class_id=class_id)
            except MetaDeckSourceError as exc:
                failures.append(exc)
                continue
            add_records(records, class_id, group_limit=per_class)

    # Older deployments exposed only an unfiltered list endpoint.  Use it as
    # compatibility fallback and retain only the first recommended rows per
    # class/archetype; this path also lets a partial query failure still
    # populate the remaining classes when the service is available.
    if normalized_classes and (
        not profiles
        or any(class_id not in covered_classes for class_id in normalized_classes)
        or any(
            covered_archetypes.get(archetype_id.casefold(), 0) < per_archetype
            for archetype_id, class_id, _label, _tier in tier_targets
            if class_id in normalized_classes
        )
    ):
        try:
            records = _fetch_meta_deck_list(timeout=timeout)
        except MetaDeckSourceError as exc:
            failures.append(exc)
        else:
            if tier_targets:
                for archetype_id, class_id, display_name, tier in tier_targets:
                    if len(profiles) >= limit:
                        break
                    if class_id not in normalized_classes:
                        continue
                    if covered_archetypes.get(archetype_id.casefold(), 0) >= per_archetype:
                        continue
                    add_records(
                        records,
                        class_id,
                        expected_archetype=archetype_id,
                        expected_label=display_name,
                        expected_tier=tier,
                        group_limit=per_archetype,
                    )
            else:
                for class_id in normalized_classes:
                    if len(profiles) >= limit:
                        break
                    add_records(records, class_id, group_limit=per_class)

    if not profiles:
        raise failures[-1] if failures else MetaDeckSourceError(
            "WBArts Meta 列表中没有可用的 40 张牌组"
        )
    return tuple(sorted(profiles, key=lambda profile: (profile.class_id, profile.name, profile.profile_id)))


def _preserve_minimum_class_coverage(
    fresh: Iterable[MetaDeckProfile],
    existing: Iterable[MetaDeckProfile],
    *,
    minimum: int = 2,
) -> tuple[MetaDeckProfile, ...]:
    """Keep at least two concrete builds per class during partial refreshes."""
    result = list(fresh)
    seen = {profile.profile_id for profile in result}
    minimum = max(1, int(minimum))
    for class_id in range(1, 8):
        current = sum(1 for profile in result if int(profile.class_id) == class_id)
        if current >= minimum:
            continue
        for profile in existing:
            if profile.profile_id in seen or int(profile.class_id) != class_id:
                continue
            result.append(profile)
            seen.add(profile.profile_id)
            current += 1
            if current >= minimum:
                break
    return tuple(sorted(result, key=lambda profile: (META_TIER_ORDER.index(profile.tier) if profile.tier in META_TIER_ORDER else len(META_TIER_ORDER), profile.class_id, profile.display_name, profile.profile_id)))


def _merge_meta_profiles(
    fresh: Iterable[MetaDeckProfile],
    existing: Iterable[MetaDeckProfile],
) -> tuple[MetaDeckProfile, ...]:
    """Merge a refresh into the cache without dropping older archetypes.

    WBArts may temporarily return only the active T1–T4 rows (or a partial
    response after a rate limit).  Fresh rows win by profile ID, while older
    T4/other rows remain available offline until a later complete refresh.
    """
    merged: dict[str, MetaDeckProfile] = {
        profile.profile_id: profile for profile in existing
    }
    merged.update({profile.profile_id: profile for profile in fresh})
    return tuple(merged.values())


def _preserve_minimum_archetype_coverage(
    fresh: Iterable[MetaDeckProfile],
    existing: Iterable[MetaDeckProfile],
    *,
    minimum: int = RECOMMENDED_DECKS_PER_ARCHETYPE,
) -> tuple[MetaDeckProfile, ...]:
    """Keep at least ``minimum`` builds for every current T1–T4 archetype."""
    result = list(fresh)
    seen = {profile.profile_id for profile in result}
    minimum = max(1, int(minimum))
    target_labels = tuple(item[2] for item in STATIC_TIER_ARCHETYPE_TARGETS)
    for archetype in target_labels:
        current = sum(
            1
            for profile in result
            if profile.display_name == archetype
            and meta_tier_label(profile.tier, profile.archetype) in {"T1", "T2", "T3", "T4"}
        )
        if current >= minimum:
            continue
        for profile in existing:
            if profile.profile_id in seen or profile.display_name != archetype:
                continue
            if meta_tier_label(profile.tier, profile.archetype) not in {"T1", "T2", "T3", "T4"}:
                continue
            result.append(profile)
            seen.add(profile.profile_id)
            current += 1
            if current >= minimum:
                break
    return tuple(
        sorted(
            result,
            key=lambda profile: (
                META_TIER_ORDER.index(profile.tier)
                if profile.tier in META_TIER_ORDER
                else len(META_TIER_ORDER),
                profile.class_id,
                profile.display_name,
                profile.profile_id,
            ),
        )
    )


def refresh_wbarts_cache(
    deck_ids: list[str | int],
    *,
    path: Path | None = None,
    timeout: float = 15.0,
) -> tuple[MetaDeckProfile, ...]:
    """Refresh selected IDs and atomically save them beside the app cache."""
    existing = {profile.profile_id: profile for profile in load_meta_deck_profiles(path)}
    for deck_id in deck_ids:
        profile = fetch_wbarts_deck(deck_id, timeout=timeout)
        existing[profile.profile_id] = profile
    profiles = tuple(existing[key] for key in sorted(existing))
    save_meta_deck_profiles(
        profiles,
        path,
        source_url=DEFAULT_SOURCE_URL,
        refresh_version="",
    )
    return profiles


def _cache_document(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _checked_on_local_date(value: str, current: datetime) -> bool:
    """Compare an ISO check marker in the same timezone as ``current``."""
    try:
        checked = datetime.fromisoformat(value)
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        return checked.astimezone(current.tzinfo).date() == current.date()
    except (TypeError, ValueError, OverflowError):
        # Older cache files may contain only a date or a malformed marker.
        return value[:10] == current.date().isoformat()


def refresh_wbarts_meta_cache_daily(
    *,
    path: Path | None = None,
    timeout: float = 15.0,
    max_profiles: int = 120,
    now: datetime | None = None,
    force: bool = False,
    include_count: bool = False,
) -> tuple[tuple[MetaDeckProfile, ...], str] | tuple[tuple[MetaDeckProfile, ...], str, int]:
    """Refresh the Meta cache at most once per local calendar day.

    Returns ``(profiles, status)``.  ``status`` is ``updated``, ``skipped`` or
    a human-readable failure string.  ``force`` bypasses the once-per-day
    guard for the Meta page's manual update button.  When ``include_count`` is
    true, a third value reports how many fetched profiles changed the cache.
    A failed attempt is timestamped too, so repeatedly opening the app during
    a Cloudflare/network outage does not hammer WBArts; the previous cache
    remains available.
    """
    # The once-per-day boundary follows the user's local calendar rather than
    # UTC (otherwise an app opened around midnight could refresh twice on the
    # same local day or skip a day entirely).
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    target = Path(path) if path is not None else writable_meta_decks_path()
    source = target if target.is_file() else default_meta_decks_path()
    existing = load_meta_deck_profiles(source)
    bundled_path = Path(__file__).resolve().parent / "data" / "meta_decks.json"
    references = load_meta_deck_profiles(bundled_path) if bundled_path.is_file() else ()
    labelled_existing = canonicalize_meta_deck_labels(existing, references)
    document = _cache_document(source)
    today = current.date().isoformat()
    last_checked = str(document.get("last_checked_at") or "")
    refresh_version = str(document.get("refresh_version") or "")
    if not force and _checked_on_local_date(last_checked, current) and refresh_version == META_REFRESH_VERSION:
        if labelled_existing != existing:
            save_meta_deck_profiles(
                labelled_existing,
                target,
                source_url=DEFAULT_SOURCE_URL,
                updated_at=str(document.get("updated_at") or ""),
                checked_at=last_checked,
                refresh_version=META_REFRESH_VERSION,
            )
        if include_count:
            return labelled_existing, "skipped", 0
        return labelled_existing, "skipped"

    checked_at = current.astimezone(timezone.utc).isoformat()
    old_updated = str(document.get("updated_at") or "")
    # Persist the check marker before networking.  If the request fails, this
    # file still contains the prior profiles and will not retry until tomorrow.
    save_meta_deck_profiles(
        labelled_existing,
        target,
        source_url=DEFAULT_SOURCE_URL,
        updated_at=old_updated,
        checked_at=checked_at,
        refresh_version=META_REFRESH_VERSION,
    )
    try:
        fresh = canonicalize_meta_deck_labels(
            fetch_wbarts_meta_decks(timeout=timeout, max_profiles=max_profiles),
            references or labelled_existing,
        )
        profiles = _preserve_minimum_class_coverage(
            _preserve_minimum_archetype_coverage(
                _merge_meta_profiles(fresh, labelled_existing),
                labelled_existing,
            ),
            labelled_existing,
        )
    except MetaDeckSourceError as exc:
        if include_count:
            return labelled_existing, f"failed: {exc}", 0
        return labelled_existing, f"failed: {exc}"
    fresh_profiles = tuple(fresh)
    old_by_id = {profile.profile_id: profile for profile in labelled_existing}
    changed_count = sum(
        1
        for profile in fresh_profiles
        if old_by_id.get(profile.profile_id) != profile
    )
    save_meta_deck_profiles(
        profiles,
        target,
        source_url=DEFAULT_SOURCE_URL,
        updated_at=today,
        checked_at=checked_at,
        refresh_version=META_REFRESH_VERSION,
    )
    if include_count:
        return profiles, "updated", changed_count
    return profiles, "updated"


__all__ = [
    "META_CLASS_IDS",
    "META_REFRESH_VERSION",
    "RECOMMENDED_DECKS_PER_CLASS",
    "RECOMMENDED_DECKS_PER_ARCHETYPE",
    "STATIC_TIER_ARCHETYPE_TARGETS",
    "MetaDeckSourceError",
    "fetch_wbarts_deck",
    "fetch_wbarts_meta_decks",
    "parse_wbarts_deck_payload",
    "refresh_wbarts_cache",
    "refresh_wbarts_meta_cache_daily",
]
