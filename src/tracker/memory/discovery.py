"""Read-only discovery of live IL2CPP objects by their runtime type name."""

from __future__ import annotations

import struct
import re
from typing import Iterator, Mapping

from .battle import (
    read_battle_model,
    read_battle_root,
    read_il2cpp_type_name,
    read_reference_collection,
)
from .win32 import MEM_PRIVATE, ProcessReader


DEFAULT_SCAN_CHUNK = 4 * 1024 * 1024


def _runtime_type_name_matches(value: str, short_name: str) -> bool:
    """Accept both namespaced and namespace-stripped IL2CPP type names."""
    return value == short_name or value.endswith("." + short_name)


def _find_runtime_string_addresses(
    reader: ProcessReader,
    module,
    value: str,
    *,
    maximum_hits: int,
    fallback_all_memory: bool = False,
) -> tuple[int, ...]:
    """Find a metadata string in the image, then in mapped runtime memory.

    Most IL2CPP builds copy type names into ``GameAssembly.dll``.  The CN
    player can leave them in a metadata mapping instead, so the broader scan
    is opt-in and only used when the fast image scan has no hits.
    """
    pattern = value.encode() + b"\0"
    addresses = tuple(
        find_pattern_in_range(
            reader,
            pattern,
            module.base_address,
            module.size,
            maximum_hits=maximum_hits,
        )
    )
    if addresses or not fallback_all_memory:
        return addresses
    return tuple(find_pattern(reader, pattern, maximum_hits=maximum_hits))


def find_pattern_in_range(
    reader: ProcessReader,
    pattern: bytes,
    address: int,
    size: int,
    *,
    maximum_hits: int = 256,
) -> Iterator[int]:
    """Find a byte sequence in one known readable address range."""
    overlap = max(0, len(pattern) - 1)
    offset = 0
    carry = b""
    hits = 0
    while offset < size:
        amount = min(DEFAULT_SCAN_CHUNK, size - offset)
        block = reader.read(address + offset, amount)
        data = carry + block
        data_base = address + offset - len(carry)
        search_from = 0
        while True:
            index = data.find(pattern, search_from)
            if index < 0:
                break
            yield data_base + index
            hits += 1
            if hits >= maximum_hits:
                return
            search_from = index + 1
        carry = data[-overlap:] if overlap else b""
        offset += amount


def find_pattern(
    reader: ProcessReader,
    pattern: bytes,
    *,
    maximum_hits: int = 256,
    memory_type: int | None = None,
) -> Iterator[int]:
    """Find a byte sequence in committed readable memory."""
    if not pattern:
        raise ValueError("pattern must not be empty")
    hits = 0
    overlap = max(0, len(pattern) - 1)
    for region in reader.iter_memory_regions():
        if not region.readable:
            continue
        if memory_type is not None and region.type != memory_type:
            continue
        offset = 0
        carry = b""
        while offset < region.size:
            amount = min(DEFAULT_SCAN_CHUNK, region.size - offset)
            try:
                block = reader.read(region.base_address + offset, amount)
            except OSError:
                break
            data = carry + block
            data_base = region.base_address + offset - len(carry)
            search_from = 0
            while True:
                index = data.find(pattern, search_from)
                if index < 0:
                    break
                yield data_base + index
                hits += 1
                if hits >= maximum_hits:
                    return
                search_from = index + 1
            carry = data[-overlap:] if overlap else b""
            offset += amount


def find_pointer_references(
    reader: ProcessReader,
    address: int,
    *,
    maximum_hits: int = 1024,
) -> Iterator[int]:
    return find_pattern(
        reader,
        struct.pack("<Q", address),
        maximum_hits=maximum_hits,
        memory_type=MEM_PRIVATE,
    )


def find_pointer_references_many(
    reader: ProcessReader,
    addresses: tuple[int, ...],
    *,
    maximum_hits: int = 4096,
) -> Iterator[tuple[int, int]]:
    """Find references to several pointers with one pass over private memory."""
    if not addresses:
        return
    patterns = {struct.pack("<Q", address): address for address in addresses}
    matcher = re.compile(b"|".join(re.escape(pattern) for pattern in patterns))
    overlap = 7
    hits = 0
    for region in reader.iter_memory_regions():
        if not region.writable or region.type != MEM_PRIVATE:
            continue
        offset = 0
        carry = b""
        while offset < region.size:
            amount = min(DEFAULT_SCAN_CHUNK, region.size - offset)
            try:
                block = reader.read(region.base_address + offset, amount)
            except OSError:
                break
            data = carry + block
            data_base = region.base_address + offset - len(carry)
            for match in matcher.finditer(data):
                pattern = match.group(0)
                yield data_base + match.start(), patterns[pattern]
                hits += 1
                if hits >= maximum_hits:
                    return
            carry = data[-overlap:]
            offset += amount


def find_class_instances(
    reader: ProcessReader,
    class_pointer_rvas: Mapping[str, int],
    *,
    module_name: str = "GameAssembly.dll",
    maximum_hits: int = 8192,
) -> dict[str, tuple[int, ...]]:
    """Find candidate instances of several versioned classes in one heap pass."""
    module = reader.module(module_name)
    class_to_key: dict[int, str] = {}
    for key, rva in class_pointer_rvas.items():
        class_address = reader.read_u64(module.base_address + rva)
        if class_address:
            class_to_key[class_address] = key
    found: dict[str, set[int]] = {key: set() for key in class_pointer_rvas}
    for candidate, class_address in find_pointer_references_many(
        reader,
        tuple(class_to_key),
        maximum_hits=maximum_hits,
    ):
        key = class_to_key.get(class_address)
        if key is not None:
            found[key].add(candidate)
    return {key: tuple(sorted(values)) for key, values in found.items()}


def find_il2cpp_classes(
    reader: ProcessReader,
    name: str,
    namespace: str | None,
    *,
    module_name: str = "GameAssembly.dll",
) -> tuple[int, ...]:
    """Resolve Il2CppClass pointers from runtime C strings.

    A namespace narrows the scan for the official build.  A few regional
    clients strip or rename presentation namespaces while retaining the
    server model classes; passing ``None`` falls back to the class-name-only
    scan in that case.
    """
    module = reader.module(module_name)
    if namespace is None:
        name_addresses = _find_runtime_string_addresses(
            reader,
            module,
            name,
            maximum_hits=512,
            fallback_all_memory=True,
        )
        classes: set[int] = set()
        for reference, _ in find_pointer_references_many(
            reader,
            name_addresses,
            maximum_hits=4096,
        ):
            candidate = reference - 0x10
            try:
                if reader.read_c_string(reader.read_u64(candidate + 0x10), maximum=256) == name:
                    classes.add(candidate)
            except (OSError, ValueError):
                continue
        return tuple(sorted(classes))
    # Namespace strings are normally unique while short class names can occur in
    # dozens of symbols. Resolve classes from Il2CppClass.namespace at +0x18.
    namespace_addresses = _find_runtime_string_addresses(
        reader, module, namespace, maximum_hits=256
    )
    classes: set[int] = set()
    for reference, _ in find_pointer_references_many(
        reader,
        namespace_addresses,
        maximum_hits=1024,
    ):
        candidate = reference - 0x18
        try:
            name_address = reader.read_u64(candidate + 0x10)
            if reader.read_c_string(name_address, maximum=256) == name:
                classes.add(candidate)
        except (OSError, ValueError):
            continue
    return tuple(sorted(classes))


def find_battle_models(
    reader: ProcessReader,
    *,
    class_pointer_rva: int | None = None,
    module_name: str = "GameAssembly.dll",
    runtime_names_only: bool = False,
) -> tuple[int, ...]:
    """Find valid active BattleModel instances and reject stale/reused objects."""
    if class_pointer_rva is not None:
        module = reader.module(module_name)
        class_address = reader.read_u64(module.base_address + class_pointer_rva)
        classes = (class_address,) if class_address else ()
    elif runtime_names_only:
        classes = find_il2cpp_classes(
            reader,
            "BattleModel",
            None,
            module_name=module_name,
        )
    else:
        classes = find_il2cpp_classes(
            reader,
            "BattleModel",
            "Wizard2.Presentation.Battle",
            module_name=module_name,
        )
        if not classes:
            # Regional builds can omit the presentation namespace from the
            # native metadata even though the class remains discoverable.
            classes = find_il2cpp_classes(
                reader,
                "BattleModel",
                None,
                module_name=module_name,
            )
    models: set[int] = set()
    for candidate, _ in find_pointer_references_many(
        reader,
        classes,
        maximum_hits=4096,
    ):
        try:
            if not _runtime_type_name_matches(
                read_il2cpp_type_name(reader, candidate), "BattleModel"
            ):
                continue
            snapshot = read_battle_model(reader, candidate)
            root = snapshot.get("root")
            if isinstance(root, dict) and len(root.get("players", ())) == 2:
                models.add(candidate)
        except (OSError, ValueError, LookupError):
            continue
    return tuple(sorted(models))


def find_battle_roots(
    reader: ProcessReader,
    *,
    module_name: str = "GameAssembly.dll",
    runtime_names_only: bool = False,
    maximum_hits: int = 10000,
) -> tuple[int, ...]:
    """Find live ``BattleRootMpo`` objects used by Puzzle/teaching battles.

    Puzzle battles are hosted by ``BattlePuzzleModel`` rather than the normal
    ``BattleModel``.  The latter is the only object that exposes the regular
    root property, so scanning only BattleModel instances makes an active
    puzzle look like no battle at all.  ``BattleRootMpo`` is shared by both
    modes and has a stable MessagePack-object layout; validate the decoded
    root before returning it to avoid stale heap objects.
    """
    classes = (
        find_il2cpp_classes(reader, "BattleRootMpo", None, module_name=module_name)
        if runtime_names_only
        else find_il2cpp_classes(
            reader,
            "BattleRootMpo",
            "Wizard2.ServerShared.MessagePackObjects",
            module_name=module_name,
        )
    )
    if not classes:
        classes = find_il2cpp_classes(
            reader,
            "BattleRootMpo",
            None,
            module_name=module_name,
        )
    if not classes:
        return ()
    roots: set[int] = set()
    for candidate, _ in find_pointer_references_many(
        reader,
        classes,
        maximum_hits=maximum_hits,
    ):
        try:
            if not _runtime_type_name_matches(
                read_il2cpp_type_name(reader, candidate), "BattleRootMpo"
            ):
                continue
            root = read_battle_root(reader, candidate)
            if len(root.players) != 2:
                continue
            # Reject released/stale roots while allowing the mulligan turn 0.
            if not all(0 <= player.turn <= 99 for player in root.players):
                continue
            if len({player.unique_id for player in root.players}) != 2:
                continue
            roots.add(candidate)
        except (OSError, ValueError, LookupError):
            continue
    return tuple(sorted(roots))


def find_battle_view_server_data(
    reader: ProcessReader,
    *,
    module_name: str = "GameAssembly.dll",
    runtime_names_only: bool = False,
    expected_player_addresses: tuple[int, int] | None = None,
) -> tuple[int, ...]:
    """Find live BattleViewServerData objects, optionally matching one BattleRoot."""
    classes = (
        find_il2cpp_classes(reader, "BattleViewServerData", None, module_name=module_name)
        if runtime_names_only
        else find_il2cpp_classes(
            reader,
            "BattleViewServerData",
            "Wizard2.View",
            module_name=module_name,
        )
    )
    if not classes:
        classes = find_il2cpp_classes(
            reader,
            "BattleViewServerData",
            None,
            module_name=module_name,
        )
    values: set[int] = set()
    for candidate, _ in find_pointer_references_many(
        reader,
        classes,
        maximum_hits=4096,
    ):
        try:
            if not _runtime_type_name_matches(
                read_il2cpp_type_name(reader, candidate), "BattleViewServerData"
            ):
                continue
            players_collection = reader.read_u64(candidate + 0x10)
            players = read_reference_collection(reader, players_collection, maximum=2)
            if len(players) != 2 or not all(
                read_il2cpp_type_name(reader, player).endswith("BattleStatePlayerMpo")
                for player in players
            ):
                continue
            if expected_player_addresses is not None and players != expected_player_addresses:
                continue
            values.add(candidate)
        except (OSError, ValueError):
            continue
    return tuple(sorted(values))


# The rank-result view is created after the terminal BattleModel snapshot.  Its
# managed data is not part of BattleRootMpo: BattleResultModel.FinishData
# points to RankBattleFinishData (+0x58), whose RankMatchResult (+0x60) owns a
# ClassRatingResult (+0x20).  These offsets are the public IL2CPP layouts for
# the supported Windows builds.  Keep the decoder best-effort so a result page
# from another battle mode or a released object is simply ignored.
def read_battle_result_class_rating(
    reader: ProcessReader,
    result_model_address: int,
) -> dict[str, int] | None:
    """Read the class-rating result attached to one live BattleResultModel.

    The returned mapping contains the rating before/after the match and their
    signed change.  ``None`` means the model is not showing a ranked result or
    the short-lived managed objects have already been released.
    """
    if not result_model_address:
        return None
    try:
        if not _runtime_type_name_matches(
            read_il2cpp_type_name(reader, result_model_address),
            "BattleResultModel",
        ):
            return None
        finish_data = reader.read_u64(result_model_address + 0x58)
        finish_name = read_il2cpp_type_name(reader, finish_data)
        if not finish_name.endswith("RankBattleFinishData"):
            return None
        rank_result = reader.read_u64(finish_data + 0x60)
        if not read_il2cpp_type_name(reader, rank_result).endswith("RankMatchResultData"):
            return None
        rating = reader.read_u64(rank_result + 0x20)
        if not read_il2cpp_type_name(reader, rating).endswith("ClassRatingResult"):
            return None
        before = reader.read_i32(rating + 0x10)
        after = reader.read_i32(rating + 0x14)
    except (OSError, ValueError, LookupError):
        return None
    # CR is a non-negative integer in the current ranked format.  Reject
    # stale/freed objects whose arbitrary bytes happen to look like pointers.
    if not (0 <= before <= 100_000 and 0 <= after <= 100_000):
        return None
    return {"before": before, "after": after, "change": after - before}


def find_battle_result_class_rating(
    reader: ProcessReader,
    *,
    module_name: str = "GameAssembly.dll",
    runtime_names_only: bool = False,
    maximum_hits: int = 4096,
) -> tuple[int, dict[str, int]] | None:
    """Locate the live BattleResultModel and decode its class-rating result.

    Discovery is intentionally explicit and caller-controlled: the private
    heap scan is only needed after a terminal battle snapshot, never during the
    normal per-frame board polling loop.
    """
    classes = (
        find_il2cpp_classes(
            reader,
            "BattleResultModel",
            None,
            module_name=module_name,
        )
        if runtime_names_only
        else find_il2cpp_classes(
            reader,
            "BattleResultModel",
            "Wizard2.Presentation",
            module_name=module_name,
        )
    )
    if not classes:
        classes = find_il2cpp_classes(
            reader,
            "BattleResultModel",
            None,
            module_name=module_name,
        )
    if not classes:
        return None
    for candidate, _ in find_pointer_references_many(
        reader,
        classes,
        maximum_hits=maximum_hits,
    ):
        result = read_battle_result_class_rating(reader, candidate)
        if result is not None:
            return candidate, result
    return None
