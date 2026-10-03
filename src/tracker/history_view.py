"""Small, deterministic helpers for the recent-record panels.

The memory reader exposes the same action through several views: the
permanent ``played_card_ids`` list, a short action ledger, and transient public
response events.  Rendering each view independently made the old panels show
duplicates and low-level response noise.  This module merges them into one
bounded, chronological timeline for both UI shells.
"""

from __future__ import annotations

from collections import Counter
from typing import Callable


CardLabel = Callable[[object], str]


def _card_id(value: object) -> int | None:
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], int) and value[0] > 0:
        return int(value[0])
    return None


def _turn(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _timeline_line(turn: int, kind: str, card_id: int | None, card_label: CardLabel) -> str:
    suffix = f" {card_label(card_id)}" if card_id is not None else ""
    prefix = f"T{turn}：" if turn > 0 else ""
    if not kind:
        return prefix + (card_label(card_id) if card_id is not None else "")
    return f"{prefix}{kind}{suffix}"


def _mulligan_lines(
    player: dict[str, object],
    card_label: CardLabel,
    *,
    opponent: bool = False,
) -> list[str]:
    summary = player.get("mulligan_summary")
    if not isinstance(summary, dict):
        return []
    if opponent:
        count = summary.get("replaced_count")
        return [f"起手换牌：{count} 张"] if isinstance(count, int) and count >= 0 else []
    initial = summary.get("initial_hand")
    if not isinstance(initial, (list, tuple)) or not initial:
        return []
    result = ["起手：" + "、".join(card_label(value) for value in initial)]
    replaced = summary.get("replaced_cards")
    if isinstance(replaced, (list, tuple)) and replaced:
        result.append("换出：" + "、".join(card_label(value) for value in replaced))
    final_hand = summary.get("final_hand")
    if isinstance(final_hand, (list, tuple)) and final_hand:
        result.append("换后：" + "、".join(card_label(value) for value in final_hand))
    return result


def recent_history_lines(
    mine: dict[str, object],
    opponent: dict[str, object],
    knowledge: object,
    card_label: CardLabel,
    *,
    limit: int = 20,
) -> tuple[list[str], list[str]]:
    """Return clean recent-record lines for the two players.

    Structured action ledgers are authoritative.  Permanent play history and
    public response history are only used as fallbacks or to fill a newer
    action that has not reached the ledger yet.  Low-level ``BattleResponse``
    records are intentionally not rendered here; they remain available in the
    training snapshot but are not useful as a human-facing recent record.
    """

    def build_timeline(
        player: dict[str, object],
        actions: object,
        evolution_events: object = (),
    ) -> list[str]:
        entries: list[tuple[int, int, int, str, tuple[object, ...]]] = []
        seen_entries: set[tuple[object, ...]] = set()
        play_counts: Counter[tuple[int, int]] = Counter()
        valid_action_count = 0
        latest_action_turn = 0
        insertion = 0

        def add(
            turn: int,
            order: int,
            kind: str,
            card_id: int | None,
            *,
            key: tuple[object, ...] | None = None,
            counts_as_play: bool = False,
        ) -> None:
            nonlocal insertion
            if not kind and card_id is None:
                return
            entry_key = key or (turn, order, kind, card_id)
            if entry_key in seen_entries:
                return
            seen_entries.add(entry_key)
            entries.append((turn, order, insertion, _timeline_line(turn, kind, card_id, card_label), entry_key))
            insertion += 1
            if counts_as_play and card_id is not None:
                play_counts[(turn, card_id)] += 1

        if isinstance(actions, (list, tuple)):
            for index, item in enumerate(actions):
                if not isinstance(item, dict):
                    continue
                card_id = _card_id(item.get("card_id"))
                kind = item.get("kind")
                if card_id is None or not isinstance(kind, str) or not kind.strip():
                    continue
                turn = _turn(item.get("turn"))
                order = item.get("order") if isinstance(item.get("order"), int) else index
                add(
                    turn,
                    order,
                    kind.strip(),
                    card_id,
                    key=("action", turn, order, card_id, kind.strip()),
                    counts_as_play=kind.strip() == "使用",
                )
                valid_action_count += 1
                latest_action_turn = max(latest_action_turn, turn)

        # ``played_card_ids`` is a fallback for old snapshots that predate the
        # structured action ledger.  Do not merge it when actions are present:
        # doing so is the main source of duplicate "使用" lines.
        if valid_action_count == 0:
            played = player.get("played_card_ids", ())
            turns = list(player.get("_played_card_turns", ())) if isinstance(player.get("_played_card_turns"), (list, tuple)) else []
            values = list(played) if isinstance(played, (list, tuple)) else []
            for index, value in enumerate(values[-20:], start=max(0, len(values) - 20)):
                card_id = _card_id(value)
                if card_id is None:
                    continue
                turn = _turn(turns[index]) if index < len(turns) else 0
                add(
                    turn,
                    1000 + index,
                    "使用",
                    card_id,
                    key=("played", turn, index, card_id),
                    counts_as_play=True,
                )

        # A public PlayOpen can arrive one poll before the permanent history or
        # action ledger.  Only fill events from the latest action turn onward;
        # older events are already represented by the bounded action ledger.
        event_history = player.get("_event_played_cards")
        if isinstance(event_history, (list, tuple)):
            for index, event in enumerate(event_history):
                if not isinstance(event, dict):
                    continue
                card_id = _card_id(event.get("card_id"))
                if card_id is None:
                    continue
                turn = _turn(event.get("turn"))
                if valid_action_count and turn < latest_action_turn:
                    continue
                if play_counts[(turn, card_id)] > 0:
                    play_counts[(turn, card_id)] -= 1
                    continue
                add(
                    turn,
                    500 + index,
                    "",
                    card_id,
                    key=("event", turn, card_id, index),
                    counts_as_play=True,
                )

        # Draws and burns have no entry in the play ledger, so merge them into
        # the same timeline instead of appending an unsorted block at the end.
        draw_history = player.get("_draw_history")
        if isinstance(draw_history, (list, tuple)):
            for index, item in enumerate(draw_history[-20:], start=max(0, len(draw_history) - 20)):
                if not isinstance(item, dict):
                    continue
                turn = _turn(item.get("turn"))
                kind = str(item.get("kind") or "")
                card_id = _card_id(item.get("card_id"))
                if kind == "爆牌":
                    if card_id is not None:
                        add(turn, -100000 + index, "爆牌", card_id, key=("burn", turn, index, card_id))
                    else:
                        add(turn, -100000 + index, f"爆牌 {item.get('count', 1)} 张", None, key=("burn", turn, index, item.get("count", 1)))
                elif card_id is not None:
                    add(turn, -100000 + index, "抽取", card_id, key=("draw", turn, index, card_id))

        # Normally evolution actions are already in ``recent_actions``.  If a
        # snapshot contains only the older evolution list, retain it as a
        # fallback and avoid adding an exact duplicate when both are present.
        if isinstance(evolution_events, (list, tuple)):
            action_evolutions = {
                (item.get("turn"), _card_id(item.get("card_id")), item.get("kind"))
                for item in actions
                if isinstance(item, dict) and isinstance(item.get("kind"), str) and "进化" in item.get("kind", "")
            } if isinstance(actions, (list, tuple)) else set()
            if valid_action_count == 0 or not action_evolutions:
                for index, item in enumerate(evolution_events[-10:], start=max(0, len(evolution_events) - 10)):
                    if not isinstance(item, dict):
                        continue
                    card_id = _card_id(item.get("card_id"))
                    if card_id is None:
                        continue
                    turn = _turn(item.get("turn"))
                    kind = str(item.get("kind") or "进化")
                    marker = (turn, card_id, kind)
                    if marker in action_evolutions:
                        continue
                    add(turn, 2000 + index, kind, card_id, key=("evolution", turn, card_id, kind, index))

        entries.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in entries[-max(1, limit):]]

    knowledge_dict = knowledge if isinstance(knowledge, dict) else {}
    mine_lines = build_timeline(mine, mine.get("_recent_actions"))
    opponent_lines = build_timeline(
        opponent,
        knowledge_dict.get("recent_actions", ()),
        knowledge_dict.get("recent_evolution_events", ()),
    )
    mine_lines = _mulligan_lines(mine, card_label) + mine_lines
    opponent_lines = _mulligan_lines(opponent, card_label, opponent=True) + opponent_lines
    return mine_lines or ["（暂无）"], opponent_lines or ["（暂无记录）"]


__all__ = ["recent_history_lines"]
