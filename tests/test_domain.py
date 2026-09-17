"""Rules only: no store, no event loop, an explicit clock."""

import pytest

from planning_poker import domain
from planning_poker.domain import DomainError


LEASE = 45.0
SESSION_TTL = 900.0


def room_with_two(now=1000.0):
    room, host = domain.new_room("Ada", "PROJ-1 Login", now)
    domain.connect(room, host, "c-host", now, LEASE)
    guest = domain.add_participant(room, "Linus", now, max_participants=50)
    domain.connect(room, guest, "c-guest", now, LEASE)
    return room, host, guest


def test_create_join_and_hidden_votes():
    now = 1000.0
    room, host, guest = room_with_two(now)
    domain.vote(room, host, 5, now)
    domain.vote(room, guest, "?", now)

    state = domain.snapshot(room, now)
    assert state["task"] == "PROJ-1 Login"
    assert [p["nickname"] for p in state["participants"]] == ["Ada", "Linus"]
    assert all(p["hasVoted"] for p in state["participants"])
    assert all(p["vote"] is None for p in state["participants"])
    assert state["statistics"] is None


def test_reveal_exposes_voters_for_each_card():
    now = 1000.0
    room, host, guest = room_with_two(now)
    domain.vote(room, host, 8, now)
    domain.vote(room, guest, 8, now)
    skipper = domain.add_participant(room, "Grace", now, 50)
    domain.connect(room, skipper, "c-3", now, LEASE)
    domain.vote(room, skipper, "☕", now)

    # Before the reveal every vote stays hidden, so no card has any voters.
    hidden = domain.snapshot(room, now)
    assert all(p["vote"] is None for p in hidden["participants"])

    # After the reveal the voters of a card are the participants whose
    # vote equals that card — this is what the room shows under it.
    domain.reveal(room, host, now)
    state = domain.snapshot(room, now)
    voters_of = lambda card: [p["nickname"] for p in state["participants"]
                              if p["hasVoted"] and p["vote"] == card]
    assert voters_of(8) == ["Ada", "Linus"]
    assert voters_of("☕") == ["Grace"]
    for card in domain.CARDS:
        if card not in (8, "☕"):
            assert voters_of(card) == []


def test_reveal_statistics_ignore_non_numeric_votes():
    now = 1000.0
    room, host, guest = room_with_two(now)
    third = domain.add_participant(room, "Grace", now, 50)
    domain.connect(room, third, "c-3", now, LEASE)
    domain.vote(room, host, 3, now)
    domain.vote(room, guest, 8, now)
    domain.vote(room, third, "☕", now)
    domain.reveal(room, host, now)

    state = domain.snapshot(room, now)
    assert [p["vote"] for p in state["participants"]] == [3, 8, "☕"]
    assert state["statistics"] == {"average": 5.5, "median": 5.5, "highest": 8, "lowest": 3}


def test_no_numeric_votes_has_no_statistics():
    now = 1000.0
    room, host, guest = room_with_two(now)
    domain.vote(room, host, "?", now)
    domain.vote(room, guest, "☕", now)
    domain.reveal(room, host, now)
    assert domain.snapshot(room, now)["statistics"] is None


def test_only_host_can_reveal_or_start_round():
    now = 1000.0
    room, host, guest = room_with_two(now)
    with pytest.raises(DomainError, match="Only the host") as error:
        domain.reveal(room, guest, now)
    assert error.value.code == "host_only"

    domain.reveal(room, host, now)
    with pytest.raises(DomainError) as error:
        domain.new_round(room, guest, "Nope", now)
    assert error.value.code == "host_only"


def test_vote_can_change_before_but_not_after_reveal():
    now = 1000.0
    room, host, _ = room_with_two(now)
    domain.vote(room, host, 3, now)
    domain.vote(room, host, 13, now)
    assert host.vote == 13
    domain.reveal(room, host, now)
    with pytest.raises(DomainError) as error:
        domain.vote(room, host, 5, now)
    assert error.value.code == "round_revealed"


def test_new_round_resets_votes_and_changes_task():
    now = 1000.0
    room, host, guest = room_with_two(now)
    domain.vote(room, host, 2, now)
    domain.vote(room, guest, 3, now)
    domain.reveal(room, host, now)
    domain.new_round(room, host, "  PROJ-2   Checkout ", now)
    assert room.task == "PROJ-2 Checkout"
    assert room.round_number == 2
    assert room.revealed is False
    assert all(p.vote is None for p in room.participants.values())


def test_new_round_without_task_keeps_the_current_name():
    now = 1000.0
    room, host, _ = room_with_two(now)
    domain.reveal(room, host, now)
    domain.new_round(room, host, "   ", now)
    assert room.task == "PROJ-1 Login"
    assert room.round_number == 2
    assert room.revealed is False


def test_anyone_can_claim_host():
    now = 1000.0
    room, host, guest = room_with_two(now)
    domain.claim_host(room, guest, now)
    assert room.host_id == guest.id
    domain.reveal(room, guest, now)
    assert room.revealed is True
    domain.claim_host(room, host, now)
    assert room.host_id == host.id


def test_host_can_kick_a_guest():
    now = 1000.0
    room, host, guest = room_with_two(now)
    with pytest.raises(DomainError) as error:
        domain.kick(room, guest, host.id, now)
    assert error.value.code == "host_only"
    with pytest.raises(DomainError) as error:
        domain.kick(room, host, host.id, now)
    assert error.value.code == "cannot_kick_self"
    domain.kick(room, host, guest.id, now)
    assert guest.id not in room.participants
    assert [p["nickname"] for p in domain.snapshot(room, now)["participants"]] == ["Ada"]


def test_duplicate_active_nickname_and_validation():
    now = 1000.0
    room, host = domain.new_room("Ada", "Task", now)
    domain.connect(room, host, "c1", now, LEASE)
    with pytest.raises(DomainError) as error:
        domain.add_participant(room, " ada ".strip(), now, 50)
    assert error.value.code == "nickname_taken"
    for bad in ["", "   ", None, 42, "x" * 33]:
        with pytest.raises(DomainError) as error:
            domain.validate_nickname(bad)
        assert error.value.code == "invalid_nickname"


def test_nickname_is_reusable_once_the_owner_left():
    now = 1000.0
    room, host = domain.new_room("Ada", "Task", now)
    domain.connect(room, host, "c1", now, LEASE)
    domain.disconnect(room, host, "c1", now)
    twin = domain.add_participant(room, "Ada", now, 50)
    assert twin.id != host.id


def test_room_capacity_is_enforced():
    now = 1000.0
    room, _ = domain.new_room("Ada", "Task", now)
    domain.add_participant(room, "Linus", now, max_participants=2)
    with pytest.raises(DomainError) as error:
        domain.add_participant(room, "Grace", now, max_participants=2)
    assert error.value.code == "room_full"


def test_disconnect_removes_vote_and_transfers_host():
    now = 1000.0
    room, host, guest = room_with_two(now)
    domain.vote(room, host, 8, now)
    domain.disconnect(room, host, "c-host", now)
    state = domain.snapshot(room, now)
    assert [p["nickname"] for p in state["participants"]] == ["Linus"]
    assert state["hostId"] == guest.id
    assert host.vote is None


def test_second_tab_keeps_the_participant_online():
    now = 1000.0
    room, host, _ = room_with_two(now)
    domain.connect(room, host, "c-host-2", now, LEASE)
    domain.disconnect(room, host, "c-host", now)
    assert host.is_active(now)
    assert room.host_id == host.id


def test_creator_keeps_host_role_if_guest_connects_first():
    now = 1000.0
    room, creator = domain.new_room("Ada", "Task", now)
    guest = domain.add_participant(room, "Grace", now, 50)
    domain.connect(room, guest, "c-guest", now, LEASE)
    assert room.host_id == creator.id
    domain.connect(room, creator, "c-host", now, LEASE)
    assert room.host_id == creator.id


def test_reconnect_uses_same_identity_without_duplicate():
    now = 1000.0
    room, host, _ = room_with_two(now)
    domain.disconnect(room, host, "c-host", now)
    resumed = domain.find_by_token(room, host.token)
    domain.connect(room, resumed, "c-host-new", now + 5, LEASE)
    assert resumed.id == host.id
    assert [p["id"] for p in domain.snapshot(room, now + 5)["participants"]].count(host.id) == 1


def test_invalid_token_is_rejected():
    now = 1000.0
    room, _, _ = room_with_two(now)
    for bad in ["nope", "", None]:
        with pytest.raises(DomainError) as error:
            domain.find_by_token(room, bad)
        assert error.value.status == 401


@pytest.mark.parametrize("bad_vote", [-1, 4, 34, "5", None, True, 5.0, [5]])
def test_rejects_values_outside_card_deck(bad_vote):
    now = 1000.0
    room, host, _ = room_with_two(now)
    with pytest.raises(DomainError) as error:
        domain.vote(room, host, bad_vote, now)
    assert error.value.code == "invalid_vote"


@pytest.mark.parametrize("bad_code", ["", "abc", "TOOLONG", "AB!23X", None, 7])
def test_room_codes_are_validated_before_use(bad_code):
    with pytest.raises(DomainError) as error:
        domain.normalise_code(bad_code)
    assert error.value.status == 404


def test_lowercase_room_code_is_accepted():
    assert domain.normalise_code(" ab12cd ") == "AB12CD"


def test_expired_connection_lease_drops_presence():
    now = 1000.0
    room, host, guest = room_with_two(now)
    domain.vote(room, host, 5, now)
    later = now + LEASE + 1
    # The guest keeps its lease alive, the host's process died without notice.
    domain.heartbeat(room, guest, "c-guest", later, LEASE)
    assert domain.prune(room, later, SESSION_TTL) is True
    assert [p["nickname"] for p in domain.snapshot(room, later)["participants"]] == ["Linus"]
    assert room.host_id == guest.id
    assert host.vote is None


def test_sessions_expire_after_the_session_ttl():
    now = 1000.0
    room, host, guest = room_with_two(now)
    domain.disconnect(room, host, "c-host", now)
    domain.heartbeat(room, guest, "c-guest", now + SESSION_TTL - 1, LEASE)
    domain.prune(room, now + SESSION_TTL - 1, SESSION_TTL)
    assert host.id in room.participants  # still reconnectable
    domain.heartbeat(room, guest, "c-guest", now + SESSION_TTL + 1, LEASE)
    domain.prune(room, now + SESSION_TTL + 1, SESSION_TTL)
    assert host.id not in room.participants


def test_room_survives_a_serialisation_round_trip():
    now = 1000.0
    room, host, guest = room_with_two(now)
    domain.vote(room, host, 13, now)
    domain.reveal(room, host, now)
    restored = domain.room_from_dict(domain.room_to_dict(room))
    assert domain.snapshot(restored, now) == domain.snapshot(room, now)
    assert domain.find_by_token(restored, guest.token).id == guest.id


def test_host_finalizes_revealed_round_with_median_or_custom_effort():
    now = 1000.0
    room, host, guest = room_with_two(now)
    domain.vote(room, host, 3, now)
    domain.vote(room, guest, 8, now)
    domain.reveal(room, host, now)
    entry = domain.finalize_round(room, host, str(domain.statistics(room, now)["median"]), now)
    assert (entry.task, entry.effort, entry.round_number) == ("PROJ-1 Login", "5.5", 1)


def test_host_removes_history_entry():
    now = 1000.0
    room, host, guest = room_with_two(now)
    domain.reveal(room, host, now)
    entry = domain.finalize_round(room, host, "custom", now)
    domain.remove_history_entry(room, host, entry.id, now + 1)
    assert room.history == []


def test_non_host_cannot_remove_history_entry():
    now = 1000.0
    room, host, guest = room_with_two(now)
    domain.reveal(room, host, now)
    entry = domain.finalize_round(room, host, "custom", now)
    with pytest.raises(DomainError) as error:
        domain.remove_history_entry(room, guest, entry.id, now)
    assert error.value.code == "host_only"


def test_host_renames_current_task_via_estimating_field():
    now = 1000.0
    room, host, _ = room_with_two(now)
    domain.reveal(room, host, now)
    entry = domain.finalize_round(room, host, "8", now)
    domain.rename_task(room, host, "  PROJ-2   Checkout ", now + 1)
    assert room.task == "PROJ-2 Checkout"
    assert entry.task == "PROJ-1 Login"


def test_finalizing_same_round_twice_is_rejected_without_duplicate_history():
    now = 1000.0
    room, host, _ = room_with_two(now)
    domain.reveal(room, host, now)
    domain.finalize_round(room, host, "5", now)
    with pytest.raises(DomainError) as error:
        domain.finalize_round(room, host, "8", now)
    assert error.value.code == "round_already_finalized"
    assert len(room.history) == 1


def test_reestimate_history_task_starts_new_round_without_overwriting_entry():
    now = 1000.0
    room, host, _ = room_with_two(now)
    domain.reveal(room, host, now)
    entry = domain.finalize_round(room, host, "8", now)
    domain.reestimate_history(room, host, entry.id, now + 1)
    assert room.task == entry.task and room.round_number == 2 and not room.revealed
    assert [(item.id, item.effort) for item in room.history] == [(entry.id, "8")]


def test_public_snapshot_history_excludes_session_tokens_and_votes():
    now = 1000.0
    room, host, guest = room_with_two(now)
    domain.vote(room, host, 5, now)
    domain.vote(room, guest, 8, now)
    domain.reveal(room, host, now)
    domain.finalize_round(room, host, "6.5", now)
    public = domain.snapshot(room, now)
    assert public["history"] == [{"id": room.history[0].id, "task": "PROJ-1 Login", "effort": "6.5", "round": 1, "createdAt": now, "updatedAt": now}]
    assert host.token not in str(public) and guest.token not in str(public)
    assert "connections" not in str(public["history"]) and "vote" not in str(public["history"])
