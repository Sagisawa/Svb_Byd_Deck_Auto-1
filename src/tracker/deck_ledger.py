"""Local remaining-deck ledger reconciled against the authoritative deck count."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .memory.deck import DeckInfoSnapshot, card_family


@dataclass(frozen=True)
class LedgerRow:
    card_id: int
    initial: int
    remaining: int


class DeckLedger:
    """Track which known cards have left the player's original 40-card deck.

    The game only publishes a total deck count during battle.  Card identities are
    assigned when a self card first appears in a draw event, hand, or directly on
    the field.  Any unmatched count remains explicit instead of being guessed.
    """

    def __init__(self, deck: DeckInfoSnapshot) -> None:
        self.deck = deck
        self.initial = deck.counter()
        self.remaining = deck.counter()
        self._seen_uids: set[int] = set()
        self._seen_draw_events: set[tuple[int, int]] = set()
        self._seen_return_events: set[tuple[object, ...]] = set()
        # Keep the previous hand so the hide-only return response can still
        # be matched when the client omits the returned card identity.
        self._previous_hand_cards: dict[int, int] = {}
        self._last_deck_count: int | None = None
        self._unknown_removed = 0
        self._burned_cards = 0
        self._burned_card_ids: list[int] = []

    def record_burn(self, count: int = 1, card_ids: tuple[int, ...] = ()) -> None:
        """Record draws lost because the local hand was already full.

        Identified cards have already been consumed by ``_draw_cards`` during
        ``update``; the IDs here are retained for display and training output.
        """
        if count > 0:
            self._burned_cards += count
            for runtime_card_id in card_ids[:count]:
                card_id = self._deck_card_id(runtime_card_id)
                if card_id is not None:
                    self._burned_card_ids.append(card_id)

    @property
    def identified_removed(self) -> int:
        return sum(self.initial.values()) - sum(self.remaining.values())

    def _deck_card_id(self, runtime_card_id: int) -> int | None:
        if runtime_card_id in self.initial:
            return runtime_card_id
        family = card_family(runtime_card_id)
        matches = [card_id for card_id in self.initial if card_family(card_id) == family]
        return matches[0] if len(matches) == 1 else None

    def _consume(self, runtime_card_id: int) -> bool:
        card_id = self._deck_card_id(runtime_card_id)
        if card_id is None or self.remaining[card_id] <= 0:
            return False
        self.remaining[card_id] -= 1
        return True

    def _restore(self, runtime_card_id: int) -> bool:
        """Put one known card back into its original deck row.

        A return-to-deck response can arrive before the authoritative deck
        counter catches up.  The row is therefore capped at its original
        copy count; the next snapshot will reconcile the aggregate count.
        """
        card_id = self._deck_card_id(runtime_card_id)
        if card_id is None or self.remaining[card_id] >= self.initial[card_id]:
            return False
        self.remaining[card_id] += 1
        return True

    @staticmethod
    def _target_identity(value: object) -> tuple[int, int] | None:
        """Read a UID/card ID pair from the several response target shapes."""
        values: list[dict[str, object]] = []
        if isinstance(value, dict):
            values.append(value)
            for key in ("card", "after_card"):
                nested = value.get(key)
                if isinstance(nested, dict):
                    values.append(nested)
        uid = 0
        card_id = 0
        for item in values:
            for key in ("unique_id", "uid"):
                raw_uid = item.get(key)
                if isinstance(raw_uid, int) and not isinstance(raw_uid, bool) and raw_uid > 0:
                    uid = raw_uid
                    break
            if uid:
                break
        for item in values:
            for key in ("base_card_id", "card_id"):
                raw_card_id = item.get(key)
                if isinstance(raw_card_id, int) and not isinstance(raw_card_id, bool) and raw_card_id > 0:
                    card_id = raw_card_id
                    break
            if card_id:
                break
        return (uid, card_id) if card_id else None

    @staticmethod
    def _event_target_values(event: dict[str, object]) -> list[object]:
        values = event.get("targets") or event.get("cards")
        if not values and isinstance(event.get("card"), dict):
            values = [event["card"]]
        if isinstance(values, (list, tuple)):
            return list(values)
        # A few adapters expose a single target directly on the event rather
        # than through the decoded collection.
        if any(key in event for key in ("unique_id", "uid", "card_id", "base_card_id")):
            return [event]
        return []

    @staticmethod
    def _event_side(event: dict[str, object], target: object) -> bool:
        if isinstance(target, dict) and isinstance(target.get("is_ally"), bool):
            return bool(target["is_ally"])
        return event.get("is_ally") is True

    def _deck_return_cards(
        self,
        snapshot: dict[str, object],
        mine: dict[str, object],
    ) -> list[tuple[int, int]]:
        """Find self cards returned to the deck, without trusting the opponent."""
        events = snapshot.get("events", ())
        if not isinstance(events, (list, tuple)):
            events = ()
        returned: list[tuple[int, int]] = []
        returned_uids: set[int] = set()
        hidden_requests: list[tuple[int, dict[str, object], int]] = []
        known_types = {
            "BattleResponsePushDeck",
            "BattleResponseBounceIntoDeck",
            "BattleResponceReturnDeck",
            # Keep adapters that corrected the protocol typo compatible with
            # the same ledger path as the memory decoder.
            "BattleResponseReturnDeck",
        }
        for event_index, event in enumerate(events):
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "")
            if event_type == "BattleResponsePushDeckHide":
                if event.get("is_ally") is True:
                    count = event.get("push_num")
                    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                        hidden_requests.append((event_index, event, count))
                continue
            if event_type not in known_types:
                continue
            for target_index, target in enumerate(self._event_target_values(event)):
                if not self._event_side(event, target):
                    continue
                identity = self._target_identity(target)
                if identity is None:
                    continue
                uid, card_id = identity
                sequence = event.get("sequence")
                if not isinstance(sequence, int) or isinstance(sequence, bool):
                    sequence = None
                token = (
                    event_type,
                    sequence,
                    uid,
                    card_id,
                    target_index if uid <= 0 else None,
                )
                if token in self._seen_return_events:
                    continue
                self._seen_return_events.add(token)
                returned.append((uid, card_id))
                if uid > 0:
                    returned_uids.add(uid)

        # PushDeckHide deliberately carries only a count.  Match it against a
        # card that disappeared from our previous hand and is not now visible
        # on the field.  Only use the inference when the mapping is exact;
        # guessing among several cards would corrupt the named ledger rows.
        current_visible_uids = {
            uid for uid, _card_id in self._visible_cards(mine, ("hand", "field"))
        }
        candidates = [
            (uid, card_id)
            for uid, card_id in self._previous_hand_cards.items()
            if uid not in current_visible_uids and uid not in returned_uids
        ]
        for event_index, event, count in hidden_requests:
            available = [item for item in candidates if item[0] not in returned_uids]
            if len(available) != count:
                continue
            sequence = event.get("sequence")
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                sequence = None
            for target_index, (uid, card_id) in enumerate(available):
                token = ("BattleResponsePushDeckHide", sequence, uid, card_id)
                if token in self._seen_return_events:
                    continue
                self._seen_return_events.add(token)
                returned.append((uid, card_id))
                returned_uids.add(uid)
        return returned

    @staticmethod
    def _mine(snapshot: dict[str, object]) -> dict[str, object] | None:
        root = snapshot.get("root")
        if not isinstance(root, dict):
            return None
        players = root.get("players")
        if not isinstance(players, (list, tuple)) or not players or not isinstance(players[0], dict):
            return None
        return players[0]

    @staticmethod
    def _visible_cards(
        mine: dict[str, object],
        zones: tuple[str, ...] = ("hand", "field"),
    ) -> list[tuple[int, int]]:
        values: list[tuple[int, int]] = []
        for zone in zones:
            cards = mine.get(zone, ())
            if not isinstance(cards, (list, tuple)):
                continue
            for card in cards:
                if not isinstance(card, dict):
                    continue
                uid = card.get("unique_id")
                card_id = card.get("base_card_id") or card.get("card_id")
                if isinstance(uid, int) and uid > 0 and isinstance(card_id, int) and card_id > 0:
                    values.append((uid, card_id))
        return values

    def _draw_cards(self, snapshot: dict[str, object]) -> list[tuple[int, int]]:
        values: list[tuple[int, int]] = []
        events = snapshot.get("events", ())
        if not isinstance(events, (list, tuple)):
            return values
        for event in events:
            if not isinstance(event, dict) or not event.get("is_ally"):
                continue
            if event.get("type") not in {"BattleResponseDrawOpen", "BattleResponseDrawOpenWithEffect"}:
                continue
            sequence = event.get("sequence")
            cards = event.get("cards", ())
            if not isinstance(sequence, int) or not isinstance(cards, (list, tuple)):
                continue
            for card in cards:
                if not isinstance(card, dict):
                    continue
                uid = card.get("unique_id")
                card_id = card.get("base_card_id") or card.get("card_id")
                if not isinstance(uid, int) or not isinstance(card_id, int):
                    continue
                event_key = (sequence, uid)
                if event_key not in self._seen_draw_events:
                    self._seen_draw_events.add(event_key)
                    values.append((uid, card_id))
        return values

    @staticmethod
    def _token_field_uids(snapshot: dict[str, object]) -> tuple[set[int], bool]:
        """Return token-created field UIDs and whether token provenance is unknown.

        A response batch can contain both a generated token and a real
        ``PutCardFromDeck`` result.  The old all-or-nothing check discarded
        every field candidate whenever *any* token response was present, so a
        genuine direct summon in that batch was never charged to the ledger.
        When the decoder supplies target UIDs, exclude only those cards.  If a
        token response has no target list (for example an older client or a
        card that vanished between polls), retain the conservative behaviour
        and do not infer any field card from that batch.
        """
        events = snapshot.get("events", ())
        if not isinstance(events, (list, tuple)):
            return set(), False
        token_uids: set[int] = set()
        unknown = False
        for event in events:
            if (
                not isinstance(event, dict)
                or event.get("type") != "BattleResponsePutToken"
                or not bool(event.get("is_ally"))
            ):
                continue
            targets = event.get("targets")
            found_target = False
            if isinstance(targets, (list, tuple)):
                for target in targets:
                    if not isinstance(target, dict):
                        continue
                    uid = target.get("unique_id")
                    if isinstance(uid, int) and uid > 0:
                        token_uids.add(uid)
                        found_target = True
            if not found_target:
                unknown = True
        # Some client builds expose the provenance directly on the public
        # FieldCard even after the short-lived PutToken response has expired.
        # ``IsSameNameToken`` is particularly important for a generated card
        # whose ID also exists in the selected deck (for example 天晶魔手).
        # Treat that flag as stronger evidence than field appearance, while a
        # missing UID remains conservative and keeps the batch unidentified.
        mine = DeckLedger._mine(snapshot)
        field = mine.get("field") if isinstance(mine, dict) else None
        if isinstance(field, (list, tuple)):
            for card in field:
                if not isinstance(card, dict) or card.get("is_same_name_token") is not True:
                    continue
                uid = card.get("unique_id")
                if isinstance(uid, int) and uid > 0:
                    token_uids.add(uid)
                else:
                    unknown = True
        return token_uids, unknown

    @staticmethod
    def _token_hand_uids(snapshot: dict[str, object]) -> tuple[set[int], bool]:
        """Return hand UIDs created by ``HandToken`` responses.

        A generated copy can enter the hand before it is played.  Its card ID
        may be identical to a real deck card, so treating every visible hand
        card as a deck draw would silently lower the wrong ledger row.
        """
        events = snapshot.get("events", ())
        if not isinstance(events, (list, tuple)):
            return set(), False
        token_uids: set[int] = set()
        unknown = False
        for event in events:
            if (
                not isinstance(event, dict)
                or event.get("type") != "BattleResponseHandToken"
                or not bool(event.get("is_ally"))
            ):
                continue
            values = event.get("cards") or event.get("targets")
            found = False
            if isinstance(values, (list, tuple)):
                for value in values:
                    card = value.get("card") if isinstance(value, dict) and isinstance(value.get("card"), dict) else value
                    if not isinstance(card, dict):
                        continue
                    uid = card.get("unique_id")
                    if isinstance(uid, int) and uid > 0:
                        token_uids.add(uid)
                        found = True
            if not found and bool(event.get("is_ally")):
                unknown = True
        return token_uids, unknown

    @staticmethod
    def _direct_deck_field_provenance(
        snapshot: dict[str, object],
    ) -> tuple[set[int], Counter[int]]:
        """Return field UIDs/card IDs explicitly reported as deck summons.

        This is only used when a neighbouring ``PutToken`` response has no
        target UID.  In that case the token cannot safely be matched to a
        visible field card, but an explicit ``PutCardFromDeck`` target remains
        strong evidence and should not be discarded with the old batch-wide
        token guard.
        """
        events = snapshot.get("events", ())
        if not isinstance(events, (list, tuple)):
            return set(), Counter()
        uids: set[int] = set()
        card_counts: Counter[int] = Counter()
        for event in events:
            if (
                not isinstance(event, dict)
                or event.get("type") != "BattleResponsePutCardFromDeck"
                or not bool(event.get("is_ally"))
            ):
                continue
            values = event.get("cards") or event.get("targets")
            if not values and isinstance(event.get("card"), dict):
                values = [event["card"]]
            if not isinstance(values, (list, tuple)):
                continue
            for value in values:
                card = value.get("card") if isinstance(value, dict) and isinstance(value.get("card"), dict) else value
                if not isinstance(card, dict):
                    continue
                uid = card.get("unique_id")
                card_id = card.get("base_card_id") or card.get("card_id")
                if isinstance(uid, int) and uid > 0:
                    uids.add(uid)
                if isinstance(card_id, int) and card_id > 0:
                    card_counts[card_id] += 1
        return uids, card_counts

    def update(self, snapshot: dict[str, object]) -> dict[str, object]:
        mine = self._mine(snapshot)
        if mine is None or not isinstance(mine.get("deck_count"), int):
            return self.to_dict()
        deck_count = int(mine["deck_count"])
        target_removed = max(0, sum(self.initial.values()) - deck_count)
        visible_hand = self._visible_cards(mine, ("hand",))

        # Apply explicit return events before using the current deck count to
        # charge newly drawn cards.  A bounce-to-hand is intentionally absent
        # here: it has not re-entered the deck and must not restore a row.
        returned_cards = self._deck_return_cards(snapshot, mine)
        returned_uids = {uid for uid, _card_id in returned_cards if uid > 0}
        for uid, card_id in returned_cards:
            self._restore(card_id)
            # The same UID may legitimately be drawn again after a return.
            # Forgetting it here prevents the later draw from being skipped.
            if uid > 0:
                self._seen_uids.discard(uid)

        # During mulligan the game briefly exposes the cards being returned and
        # their replacements under different UIDs while the deck count still
        # represents only four cards outside the deck.  Assigning identities at
        # turn 0 permanently charges a replacement draw to the wrong card.  Wait
        # for the first real turn, when the hand and deck count are coherent.
        turn = mine.get("turn")
        if self._last_deck_count is None and isinstance(turn, int) and turn <= 0:
            self._unknown_removed = target_removed
            self._previous_hand_cards = dict(visible_hand)
            return self.to_dict(deck_count=deck_count)

        candidates: list[tuple[int, int]] = []
        draw_candidates = self._draw_cards(snapshot)
        candidates.extend(draw_candidates)
        draw_uids = {uid for uid, _card_id in draw_candidates if uid > 0}
        token_hand_uids, unknown_hand_token = self._token_hand_uids(snapshot)
        if not unknown_hand_token:
            candidates.extend((uid, card_id) for uid, card_id in visible_hand if uid not in token_hand_uids)

        if self._last_deck_count is None:
            # Current hand cards and play history do not overlap.  A current
            # field card usually also occurs in play history, so including the
            # field here would double-charge it when the tracker starts midgame.
            history = mine.get("played_card_ids", ())
            if isinstance(history, (list, tuple)):
                for index, item in enumerate(history):
                    if isinstance(item, (list, tuple)) and item and isinstance(item[0], int):
                        candidates.append((-(index + 1), item[0]))
        else:
            # Cards summoned directly from the deck never enter our hand, so a
            # newly observed field UID is useful evidence.  Consume public
            # draws and hand cards first; ``capacity`` below guarantees that
            # the named rows never exceed the authoritative deck decrease.
            # ``HandToken`` cards are generated and must not consume the
            # selected deck.  If the response omitted all target UIDs, avoid
            # guessing any visible hand card from that batch.
            # ``PutToken`` is the explicit provenance signal for generated
            # cards.  In particular, 希姆 can create 天晶魔手 with the same ID as
            # a real deck card.  Exclude only token UIDs when the response
            # identifies them, while still allowing a direct deck summon in
            # the same response batch to consume its named row.
            token_uids, unknown_token_provenance = self._token_field_uids(snapshot)
            field_cards = self._visible_cards(mine, ("field",))
            if not unknown_token_provenance:
                candidates.extend((uid, card_id) for uid, card_id in field_cards if uid not in token_uids)
            else:
                # If the token response is incomplete, use only the cards
                # explicitly named by a direct deck summon.  This preserves
                # the safe behavior for an unknown token while allowing a
                # direct summon in the same response batch to decrement the
                # selected deck's named row.
                direct_uids, direct_card_counts = self._direct_deck_field_provenance(snapshot)
                for uid, card_id in field_cards:
                    if uid in direct_uids:
                        candidates.append((uid, card_id))
                        if direct_card_counts.get(card_id, 0) > 0:
                            direct_card_counts[card_id] -= 1
                for uid, card_id in field_cards:
                    if uid in direct_uids:
                        continue
                    if direct_card_counts.get(card_id, 0) > 0:
                        candidates.append((uid, card_id))
                        direct_card_counts[card_id] -= 1

        capacity = max(0, target_removed - self.identified_removed)
        for uid, card_id in candidates:
            # A response can legally return a card and draw that same card
            # back immediately.  Permit the explicit draw event, while
            # ignoring a stale visible-hand copy from the return snapshot.
            if uid > 0 and uid in returned_uids and uid not in draw_uids:
                continue
            if uid > 0 and uid in self._seen_uids:
                continue
            # The authoritative deck count can lag the public field/draw
            # response by one poll.  Do not mark a UID as consumed while the
            # current capacity is zero; otherwise the later deck-count drop
            # would be forced into ``unknown_removed`` forever.
            if capacity > 0 and self._consume(card_id):
                if uid > 0:
                    self._seen_uids.add(uid)
                capacity -= 1

        self._unknown_removed = max(0, target_removed - self.identified_removed)
        self._previous_hand_cards = dict(visible_hand)
        self._last_deck_count = deck_count
        return self.to_dict(deck_count=deck_count)

    def to_dict(self, *, deck_count: int | None = None) -> dict[str, object]:
        rows = [
            LedgerRow(card_id=card.card_id, initial=card.count, remaining=self.remaining[card.card_id])
            for card in self.deck.cards
        ]
        return {
            "deck_id": self.deck.deck_id,
            "deck_name": self.deck.deck_name,
            "deck_format": self.deck.deck_format,
            "class_id": self.deck.class_id,
            "authoritative_deck_count": deck_count if deck_count is not None else self._last_deck_count,
            "identified_removed": self.identified_removed,
            "unknown_removed": self._unknown_removed,
            "burned_cards": self._burned_cards,
            "burned_card_ids": list(self._burned_card_ids),
            "rows": [row.__dict__ for row in rows],
        }
