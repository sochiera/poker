"""HTTP + websocket protocol, driven through the real ASGI app."""

from fastapi.testclient import TestClient

from planning_poker.app import create_app
from planning_poker.config import Settings
from planning_poker.store import MemoryStore


def build_client() -> TestClient:
    settings = Settings(room_ttl=3600, session_ttl=900)
    return TestClient(create_app(settings=settings, store=MemoryStore()))


def authenticate(socket, token):
    socket.send_json({"type": "auth", "token": token})
    return socket.receive_json()


def test_healthcheck_reports_the_active_store():
    with build_client() as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "store": "memory"}


def test_room_api_validation_and_missing_room():
    with build_client() as client:
        response = client.post("/api/rooms", json={"nickname": " "})
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_nickname"

        response = client.post("/api/rooms", content=b"not json")
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_json"

        response = client.post("/api/rooms/NOPE99/join", json={"nickname": "Ada"})
        assert response.status_code == 404

        response = client.post("/api/rooms/../etc/join", json={"nickname": "Ada"})
        assert response.status_code == 404


def test_realtime_vote_reveal_and_host_transfer():
    with build_client() as client:
        host_session = client.post("/api/rooms", json={"nickname": "Ada", "task": "API-7"}).json()
        code = host_session["code"]
        with client.websocket_connect(f"/ws/{code}") as host:
            assert authenticate(host, host_session["token"])["type"] == "welcome"
            assert host.receive_json()["state"]["participants"][0]["nickname"] == "Ada"

            guest_session = client.post(f"/api/rooms/{code}/join", json={"nickname": "Grace"}).json()
            with client.websocket_connect(f"/ws/{code}") as guest:
                assert authenticate(guest, guest_session["token"])["type"] == "welcome"
                guest.receive_json()
                host_join_state = host.receive_json()["state"]
                assert len(host_join_state["participants"]) == 2

                guest.send_json({"type": "vote", "value": 8})
                host_state = host.receive_json()["state"]
                guest.receive_json()
                grace = next(p for p in host_state["participants"] if p["nickname"] == "Grace")
                assert grace["hasVoted"] is True
                assert grace["vote"] is None

                guest.send_json({"type": "reveal"})
                assert guest.receive_json()["code"] == "host_only"

                host.send_json({"type": "reveal"})
                revealed_for_host = host.receive_json()["state"]
                revealed_for_guest = guest.receive_json()["state"]
                assert revealed_for_host["statistics"]["average"] == 8
                assert revealed_for_guest["revealed"] is True

                guest.send_json({"type": "vote", "value": 3})
                assert guest.receive_json()["code"] == "round_revealed"

                host.send_json({"type": "new_round", "task": "API-8"})
                assert host.receive_json()["state"]["task"] == "API-8"
                guest.receive_json()

            # The guest closing its tab is broadcast to whoever is left.
            after_leave = host.receive_json()["state"]
            assert len(after_leave["participants"]) == 1


def test_host_role_moves_on_when_the_host_leaves():
    with build_client() as client:
        host_session = client.post("/api/rooms", json={"nickname": "Ada"}).json()
        code = host_session["code"]
        guest_session = client.post(f"/api/rooms/{code}/join", json={"nickname": "Grace"}).json()
        with client.websocket_connect(f"/ws/{code}") as guest:
            authenticate(guest, guest_session["token"])
            guest.receive_json()
            with client.websocket_connect(f"/ws/{code}") as host:
                authenticate(host, host_session["token"])
                host.receive_json()
                assert guest.receive_json()["state"]["hostId"] == host_session["participantId"]
            assert guest.receive_json()["state"]["hostId"] == guest_session["participantId"]


def test_guest_can_claim_host_and_start_round_without_a_task_name():
    with build_client() as client:
        host_session = client.post("/api/rooms", json={"nickname": "Ada", "task": "Keep me"}).json()
        code = host_session["code"]
        guest_session = client.post(f"/api/rooms/{code}/join", json={"nickname": "Grace"}).json()
        with client.websocket_connect(f"/ws/{code}") as host, client.websocket_connect(f"/ws/{code}") as guest:
            authenticate(host, host_session["token"])
            host.receive_json()
            authenticate(guest, guest_session["token"])
            guest.receive_json()
            host.receive_json()

            guest.send_json({"type": "claim_host"})
            assert host.receive_json()["state"]["hostId"] == guest_session["participantId"]
            assert guest.receive_json()["state"]["hostId"] == guest_session["participantId"]

            guest.send_json({"type": "reveal"})
            host.receive_json()
            guest.receive_json()
            guest.send_json({"type": "new_round", "task": ""})
            assert host.receive_json()["state"]["task"] == "Keep me"
            assert guest.receive_json()["state"]["round"] == 2


def test_host_can_kick_guest_over_websocket():
    with build_client() as client:
        host_session = client.post("/api/rooms", json={"nickname": "Ada"}).json()
        code = host_session["code"]
        guest_session = client.post(f"/api/rooms/{code}/join", json={"nickname": "Grace"}).json()
        with client.websocket_connect(f"/ws/{code}") as host, client.websocket_connect(f"/ws/{code}") as guest:
            authenticate(host, host_session["token"])
            host.receive_json()
            authenticate(guest, guest_session["token"])
            guest.receive_json()
            host.receive_json()

            guest.send_json({"type": "kick", "participantId": host_session["participantId"]})
            assert guest.receive_json()["code"] == "host_only"

            host.send_json({"type": "kick", "participantId": guest_session["participantId"]})
            state = host.receive_json()["state"]
            assert [p["nickname"] for p in state["participants"]] == ["Ada"]


def test_websocket_rejects_bad_credentials_and_unknown_actions():
    with build_client() as client:
        session = client.post("/api/rooms", json={"nickname": "Ada"}).json()
        with client.websocket_connect(f"/ws/{session['code']}") as socket:
            assert authenticate(socket, "wrong")["code"] == "unauthorized"

        with client.websocket_connect(f"/ws/{session['code']}") as socket:
            socket.send_json({"type": "vote", "value": 5})  # no auth frame first
            assert socket.receive_json()["code"] == "unauthorized"

        with client.websocket_connect("/ws/nope") as socket:
            assert authenticate(socket, session["token"])["code"] == "room_not_found"

        with client.websocket_connect(f"/ws/{session['code']}") as socket:
            authenticate(socket, session["token"])
            socket.receive_json()
            socket.send_json({"type": "sabotage"})
            assert socket.receive_json()["code"] == "unknown_action"
            socket.send_json({"type": "vote", "value": "99"})
            assert socket.receive_json()["code"] == "invalid_vote"


def test_duplicate_nickname_is_rejected_while_the_owner_is_online():
    with build_client() as client:
        session = client.post("/api/rooms", json={"nickname": "Ada"}).json()
        code = session["code"]
        with client.websocket_connect(f"/ws/{code}") as socket:
            authenticate(socket, session["token"])
            socket.receive_json()
            response = client.post(f"/api/rooms/{code}/join", json={"nickname": "ada"})
            assert response.status_code == 409
            assert response.json()["error"] == "nickname_taken"
        assert client.post(f"/api/rooms/{code}/join", json={"nickname": "ada"}).status_code == 201


def test_pages_are_served():
    with build_client() as client:
        assert "Pointy" in client.get("/").text
        assert client.get("/room/ABC123").status_code == 200
        assert client.get("/static/room.js").status_code == 200


def test_room_page_assets_resolve_under_subpath():
    """`/poker/room/CODE` has no trailing slash, so `../../static` leaks to `/static`."""
    from urllib.parse import urljoin
    import re

    with build_client() as client:
        html = client.get("/room/ABC123").text
    page = "https://example.com/poker/room/ABC123"
    hrefs = re.findall(r'(?:href|src)="([^"]+)"', html)
    resolved = [urljoin(page, href) for href in hrefs]
    assert hrefs, "room page should reference assets"
    assert all(url.startswith("https://example.com/poker/") for url in resolved), resolved
    assert any("/poker/static/room.js" in url for url in resolved)
    assert any("/poker/static/styles.css" in url for url in resolved)


def test_websocket_history_finalize_remove_reestimate_and_export_projection():
    with build_client() as client:
        session = client.post("/api/rooms", json={"nickname": "Ada", "task": "API history"}).json()
        with client.websocket_connect(f"/ws/{session['code']}") as socket:
            assert authenticate(socket, session["token"])["type"] == "welcome"
            socket.receive_json()
            socket.send_json({"type": "reveal"})
            assert socket.receive_json()["state"]["revealed"] is True
            socket.send_json({"type": "finalize_round", "effort": "custom effort"})
            saved = socket.receive_json()["state"]
            entry = saved["history"][0]
            assert {"id", "task", "effort", "round", "createdAt", "updatedAt"} <= entry.keys()
            assert entry["effort"] == "custom effort"
            socket.send_json({"type": "new_round", "historyEntryId": entry["id"]})
            restarted = socket.receive_json()["state"]
            assert restarted["task"] == "API history" and restarted["round"] == 2
            assert restarted["history"] == [entry]
            socket.send_json({"type": "remove_history_entry", "historyEntryId": entry["id"]})
            assert socket.receive_json()["state"]["history"] == []
        script = client.get("/static/room.js").text
        assert "${entry.task}\\t${entry.effort}" in script
        assert "finalize_round" in script and "remove_history_entry" in script


def test_host_removes_history_entry():
    with build_client() as client:
        session = client.post("/api/rooms", json={"nickname": "Ada", "task": "Remove me"}).json()
        with client.websocket_connect(f"/ws/{session['code']}") as socket:
            authenticate(socket, session["token"])
            socket.receive_json()
            socket.send_json({"type": "reveal"})
            socket.receive_json()
            socket.send_json({"type": "finalize_round", "effort": "5"})
            entry = socket.receive_json()["state"]["history"][0]
            socket.send_json({"type": "remove_history_entry", "historyEntryId": entry["id"]})
            assert socket.receive_json()["state"]["history"] == []


def test_non_host_cannot_remove_history_entry():
    with build_client() as client:
        host_session = client.post("/api/rooms", json={"nickname": "Ada"}).json()
        code = host_session["code"]
        guest_session = client.post(f"/api/rooms/{code}/join", json={"nickname": "Grace"}).json()
        with client.websocket_connect(f"/ws/{code}") as host, client.websocket_connect(f"/ws/{code}") as guest:
            authenticate(host, host_session["token"])
            host.receive_json()
            authenticate(guest, guest_session["token"])
            guest.receive_json()
            host.receive_json()
            host.send_json({"type": "reveal"})
            host.receive_json()
            guest.receive_json()
            host.send_json({"type": "finalize_round", "effort": "5"})
            entry = host.receive_json()["state"]["history"][0]
            guest.receive_json()
            guest.send_json({"type": "remove_history_entry", "historyEntryId": entry["id"]})
            assert guest.receive_json()["code"] == "host_only"


def test_host_renames_current_task_via_estimating_field():
    with build_client() as client:
        host_session = client.post("/api/rooms", json={"nickname": "Ada", "task": "Before"}).json()
        code = host_session["code"]
        with client.websocket_connect(f"/ws/{code}") as host:
            authenticate(host, host_session["token"])
            host.receive_json()
            host.send_json({"type": "reveal"})
            host.receive_json()
            host.send_json({"type": "finalize_round", "effort": "5"})
            saved_entry = host.receive_json()["state"]["history"][0]

            guest_session = client.post(f"/api/rooms/{code}/join", json={"nickname": "Grace"}).json()
            with client.websocket_connect(f"/ws/{code}") as guest:
                authenticate(guest, guest_session["token"])
                guest.receive_json()
                host.receive_json()
                host.send_json({"type": "rename_task", "task": "  After   rename "})
                host_state = host.receive_json()["state"]
                guest_state = guest.receive_json()["state"]
                assert host_state["task"] == guest_state["task"] == "After rename"
                assert host_state["history"] == guest_state["history"] == [saved_entry]


def test_room_ui_paths_remain_safe_under_poker_subpath():
    with build_client() as client:
        script = client.get("/static/room.js").text
        html = client.get("/room/ABC123").text
    assert "const basePath = location.pathname.replace" in script
    assert "${basePath}/ws/${code}" in script and "${basePath}/api/rooms/" in script
    assert "../static/room.js" in html and "../static/styles.css" in html
