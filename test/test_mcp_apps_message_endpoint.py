"""Tests for the MCP Apps ui/message delivery path (SEP-1865 return channel).

``POST /api/mcp-apps/message`` is dashboard-authoritative — unlike ``/call``
there is no gateway leg, so the callback-capability check, the message-shape
validation, the rate floor, and the slot injection are all covered here with
``aiohttp``'s test client against a fake state carrying a scripted slot.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_utils import MCP_APP_MESSAGE_KIND
from kiro_crew.dashboard.handlers import mcp_apps as mcp_apps_handlers
from kiro_crew.mcp_apps_render import load_spool
from kiro_crew.mcp_gateway import apps
from kiro_crew.mcp_gateway.apps import write_spool

pytestmark = pytest.mark.asyncio


@pytest.fixture
def spool_tmp(tmp_path, monkeypatch):
    d = tmp_path / "mcp-apps"
    monkeypatch.setenv(apps.SPOOL_ENV, str(d))
    return d


@pytest.fixture(autouse=True)
def _fresh_rate_floor(monkeypatch):
    """Each test starts with an empty per-spool rate map."""
    monkeypatch.setattr(mcp_apps_handlers, "_last_message_at", {})


def _spool_record(session_key: str = "dashboard:sess-msg") -> str:
    return write_spool(
        {
            "server": "user-message",
            "tool": "message_user",
            "session_key": session_key,
            "pool_digest": "digest",
            "html": "<html>app</html>",
            "csp": None,
            "permissions": None,
            "structured_content": None,
        }
    )


def _cbs(spool_id: str) -> str:
    rec = load_spool(spool_id) or {}
    return rec.get("callback_secret") or ""


def _msg_body(spool_id: str, secret: str, text: str = "clicked Acknowledge") -> dict:
    return {
        "spool_id": spool_id,
        "callback_secret": secret,
        "role": "user",
        "content": [{"type": "text", "text": text}],
    }


#: The session the test spool records are bound to.
_OWNER_HDR = {"X-Session-Key": "dashboard:sess-msg"}


class _FakeSlot:
    """The narrow slot surface the injection path drives."""

    def __init__(self, running: bool = False):
        self.running = running
        self._in_stage_execution = False
        self.is_restricted = False  # read by _is_restricted_session
        self._queue: list[dict] = []
        self.messages: list[dict] = []
        self.appended: list[tuple] = []
        self.task = None

    def queue_append(self, content: str, kind: str = "", meta=None, **_kw) -> str:
        self._queue.append({"content": content, "kind": kind, "meta": meta})
        return "q-1"

    def append(self, role, content, cls="", ts="", *, meta=None, **_kw):
        self.appended.append((role, content, cls, meta))
        return {"role": role, "content": content}


@pytest.fixture
def message_env(monkeypatch):
    """Client factory exposing ONLY the message route, over a fake state."""

    @web.middleware
    async def _identity(request, handler):
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])

    class _FakeState:
        _restricted_keys: set = set()
        _slots: dict = {}
        owner_id = ""
        pushed = 0

        def get_slot(self, key):
            return self._slots.get(key)

        def push_slots_update(self):
            type(self).pushed += 1

    state = _FakeState()
    app["state"] = state

    async def _no_rehydrate(_state, _key):
        return None

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_persistence.rehydrate_slot_from_history_async",
        _no_rehydrate,
    )

    app.router.add_post("/api/mcp-apps/message", mcp_apps_handlers.api_mcp_apps_message)

    def make_client() -> TestClient:
        return TestClient(TestServer(app))

    return make_client, state


async def test_message_rejects_non_owner_and_app_tokens(message_env, spool_tmp):
    """Same load-bearing identity gate as /call: app tokens and non-owner
    subjects are refused regardless of any client-set header."""
    make_client, _state = message_env
    spool_id = _spool_record()
    secret = _cbs(spool_id)
    async with make_client() as client:
        for hdrs in (
            {"X-Test-User": "someone-else", **_OWNER_HDR},
            {"X-Test-App": "some-app", **_OWNER_HDR},
        ):
            resp = await client.post(
                "/api/mcp-apps/message", headers=hdrs, json=_msg_body(spool_id, secret)
            )
            assert resp.status == 403
            body = await resp.json()
            assert "owner authorization" in body["error"]


async def test_message_rejects_restricted_session(message_env, spool_tmp):
    make_client, state = message_env
    spool_id = _spool_record()
    secret = _cbs(spool_id)
    state._restricted_keys = {"dashboard:incog-1"}
    async with make_client() as client:
        resp = await client.post(
            "/api/mcp-apps/message",
            headers={"X-Session-Key": "dashboard:incog-1"},
            json=_msg_body(spool_id, secret),
        )
        assert resp.status == 403
        assert "not available" in (await resp.json())["error"]


async def test_message_rejects_session_mismatch(message_env, spool_tmp):
    """A leaked spool id presented from another session is refused."""
    make_client, _state = message_env
    spool_id = _spool_record()
    secret = _cbs(spool_id)
    async with make_client() as client:
        resp = await client.post(
            "/api/mcp-apps/message",
            headers={"X-Session-Key": "dashboard:other"},
            json=_msg_body(spool_id, secret),
        )
        assert resp.status == 403
        assert "another session" in (await resp.json())["error"]


async def test_message_requires_callback_secret(message_env, spool_tmp):
    """The model-visible spool_id authorizes NOTHING: this endpoint is the
    authority (no gateway leg), so a wrong or missing secret is refused here."""
    make_client, _state = message_env
    spool_id = _spool_record()
    async with make_client() as client:
        for secret in ("", "wrong-secret"):
            body = _msg_body(spool_id, secret)
            if not secret:
                del body["callback_secret"]
            resp = await client.post("/api/mcp-apps/message", headers=_OWNER_HDR, json=body)
            assert resp.status == 403
            assert "callback capability" in (await resp.json())["error"]


async def test_message_unknown_spool_is_404(message_env, spool_tmp):
    make_client, _state = message_env
    async with make_client() as client:
        resp = await client.post(
            "/api/mcp-apps/message",
            headers=_OWNER_HDR,
            json=_msg_body("f" * 32, "whatever"),
        )
        assert resp.status == 404


async def test_message_validates_shape(message_env, spool_tmp):
    """role must be "user", content a non-empty array of TEXT blocks (non-text
    is refused rather than dropped), and the total text bounded."""
    make_client, _state = message_env
    spool_id = _spool_record()
    secret = _cbs(spool_id)
    base = {"spool_id": spool_id, "callback_secret": secret}
    async with make_client() as client:
        for extra in (
            {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
            {"role": "user", "content": []},
            {"role": "user", "content": "not a list"},
            {"role": "user", "content": [{"type": "image", "data": "..."}]},
            {"role": "user", "content": [{"type": "text", "text": "   "}]},
            {"role": "user", "content": [{"type": "text", "text": "y" * 20_000}]},
        ):
            resp = await client.post(
                "/api/mcp-apps/message", headers=_OWNER_HDR, json={**base, **extra}
            )
            assert resp.status == 400, extra


async def test_message_session_gone_is_409(message_env, spool_tmp):
    """No live slot and no rehydratable history → the app learns delivery
    failed instead of a phantom session being minted."""
    make_client, _state = message_env
    spool_id = _spool_record()
    secret = _cbs(spool_id)
    async with make_client() as client:
        resp = await client.post(
            "/api/mcp-apps/message", headers=_OWNER_HDR, json=_msg_body(spool_id, secret)
        )
        assert resp.status == 409
        assert "closed" in (await resp.json())["error"]


async def test_message_queues_behind_live_turn(message_env, spool_tmp):
    """A busy slot gets the message QUEUED (kind-tagged, provenance-wrapped),
    never a concurrent turn."""
    make_client, state = message_env
    spool_id = _spool_record()
    secret = _cbs(spool_id)
    slot = _FakeSlot(running=True)
    state._slots["sess-msg"] = slot
    try:
        async with make_client() as client:
            resp = await client.post(
                "/api/mcp-apps/message",
                headers=_OWNER_HDR,
                json=_msg_body(spool_id, secret, "user clicked Acknowledge"),
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["result"] == {"isError": False, "delivery": "queued"}
            assert len(slot._queue) == 1
            entry = slot._queue[0]
            assert entry["kind"] == MCP_APP_MESSAGE_KIND
            assert entry["content"].startswith(mcp_apps_handlers.APP_MESSAGE_PREFIX)
            assert "user-message/message_user" in entry["content"]
            assert "user clicked Acknowledge" in entry["content"]
            assert entry["content"].rstrip().endswith(mcp_apps_handlers.APP_MESSAGE_END)
            # Admission containment stamp: app messages are not structurally
            # exempt from _drop_stale_admissions, so the enqueue must record
            # the containment snapshot that held at admission.
            from kiro_crew.dashboard.session_control import QUEUED_CONTAINMENT_META_KEY

            assert QUEUED_CONTAINMENT_META_KEY in (entry.get("meta") or {})
            # The transcript twin rides along for the user to see.
            assert slot.appended and slot.appended[0][0] == "queued"
    finally:
        state._slots.clear()


async def test_message_starts_turn_on_idle_slot(message_env, spool_tmp, monkeypatch):
    """An idle slot gets an inject row + a dispatched turn attributed to the
    "app" actor."""
    make_client, state = message_env
    spool_id = _spool_record()
    secret = _cbs(spool_id)
    slot = _FakeSlot(running=False)
    state._slots["sess-msg"] = slot

    dispatched: dict = {}

    async def _fake_run_chat(_state, _slot, message, **kwargs):
        dispatched["message"] = message
        dispatched["actor"] = kwargs.get("_turn_actor")
        dispatched["synthetic"] = kwargs.get("_synthetic_payload")

    def _fake_spawn(_state, _slot, coro):
        import asyncio

        return asyncio.ensure_future(coro)

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)
    monkeypatch.setattr("kiro_crew.dashboard.turn_dispatch.spawn_guarded_turn", _fake_spawn)
    try:
        async with make_client() as client:
            resp = await client.post(
                "/api/mcp-apps/message",
                headers=_OWNER_HDR,
                json=_msg_body(spool_id, secret),
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["result"] == {"isError": False, "delivery": "turn"}
            assert slot.task is not None
            await slot.task
            assert dispatched["actor"] == "app"
            # App-authored, not user speech: the flag that suppresses the
            # linked-thread mirror.
            assert dispatched["synthetic"] is True
            assert dispatched["message"].startswith(mcp_apps_handlers.APP_MESSAGE_PREFIX)
            # The inject row carries the label in meta so a rehydrate keeps it.
            role, _content, _cls, meta = slot.appended[0]
            assert role == "inject"
            assert meta == {"injectKind": "mcp_app", "appLabel": "user-message/message_user"}
    finally:
        state._slots.clear()


async def test_message_rate_floor(message_env, spool_tmp):
    """A second delivery from the same app instance inside the floor is 429 —
    ui/message starts model turns, so it is spaced, not merely capped."""
    make_client, state = message_env
    spool_id = _spool_record()
    secret = _cbs(spool_id)
    slot = _FakeSlot(running=True)
    state._slots["sess-msg"] = slot
    try:
        async with make_client() as client:
            first = await client.post(
                "/api/mcp-apps/message", headers=_OWNER_HDR, json=_msg_body(spool_id, secret)
            )
            assert first.status == 200
            second = await client.post(
                "/api/mcp-apps/message", headers=_OWNER_HDR, json=_msg_body(spool_id, secret)
            )
            assert second.status == 429
    finally:
        state._slots.clear()


def test_queued_app_entries_are_synthetic_payloads():
    """A drained app-message entry must never mirror to a linked channel as the
    human's own words: the payload predicate classifies it as runner/app text
    structurally, by its enqueue-time kind."""
    from kiro_crew.dashboard.chat_utils import is_synthetic_payload_item

    assert is_synthetic_payload_item({"kind": MCP_APP_MESSAGE_KIND, "content": "x"})
    assert not is_synthetic_payload_item({"kind": "", "content": "x"})


async def test_message_bounds_backend_declared_labels(message_env, spool_tmp):
    """A hostile backend can declare a huge tool name; the 16 KiB cap bounds
    only the message text, so the label gets its own per-field bound before it
    is retained in the prompt/transcript/meta/audit."""
    make_client, state = message_env
    spool_id = _spool_record()
    # Corrupt the record's tool name to a hostile size, as interception would
    # have written it from a hostile backend's declaration.
    import json as _json

    from kiro_crew.mcp_gateway.apps import spool_dir

    path = spool_dir() / f"{spool_id}.json"
    rec = _json.loads(path.read_text())
    rec["tool"] = "t" * 100_000
    path.write_text(_json.dumps(rec))
    secret = rec["callback_secret"]

    slot = _FakeSlot(running=True)
    state._slots["sess-msg"] = slot
    try:
        async with make_client() as client:
            resp = await client.post(
                "/api/mcp-apps/message", headers=_OWNER_HDR, json=_msg_body(spool_id, secret)
            )
            assert resp.status == 200
            content = slot._queue[0]["content"]
            first_line = content.splitlines()[0]
            assert len(first_line) < 400  # banner line stays bounded
            assert "…" in first_line  # truncation is marked, not silent
    finally:
        state._slots.clear()
