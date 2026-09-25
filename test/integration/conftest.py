"""Integration-layer fixtures: the real gateway, booted in THIS process.

This directory is the middle layer of the test pyramid. A unit test in
``test/`` builds a bare ``web.Application()`` with a handful of routes and
mocks the rest; the E2E suites (``test/test_e2e_smoke.py``, ``test/e2e/``,
``test/test_playwright_e2e.py``) spawn ``kirocrew gateway`` as a subprocess
behind ``KIROCREW_E2E=1``. Neither answers "do the real modules work when
wired together?" -- that is what a test here answers:

* the real ``GatewayOrchestrator`` boot sequence (``run()``), not a hand-copied
  subset of it, so a new boot step is covered the day it lands;
* a real ``KIROCREW_HOME`` under ``tmp_path`` -- real session store, real
  config, real memory bindings, real agent-spec directory;
* the ONLY fake is the model: ``kiro_crew.testing.fake_acp_backend`` stands in
  for ``kiro-cli`` (tests MUST NOT spawn the real one);
* the HTTP client talks to the port the gateway actually bound, on loopback,
  from the same interpreter and event loop -- so an event-loop stall in one
  request is observable from another, and a "restart" is a second boot on the
  SAME home.

Why boot through ``run()`` and not through ``_init_dashboard()`` alone: the
bugs this layer exists for live in the seams BETWEEN boot steps -- memory
preparation vs workflow init, spec scanning vs tool policy, admission vs the
resource controller. A fixture that re-implements the sequence drifts from
the real one silently.

The one thing ``run()`` does that a test cannot survive is its exit:
``await shutdown_event.wait()`` is followed by ``_shutdown_and_exit`` ->
``os._exit``. The boot helper therefore never lets that ``await`` return: it
cancels the boot task once the dashboard is serving, then awaits the graceful
``_shutdown()`` itself. ``run()`` has no ``finally`` between the wait and the
exit, so the cancel cannot reach ``os._exit`` (pinned by
``test_boot_smoke.py::test_run_has_no_finally_around_shutdown_wait``).

Shape: an ``async with`` helper, not an async fixture
-----------------------------------------------------
By this repo's convention (``testing-conventions.md``, "Async tests") the boot
is an ``async with booted_gateway(home)`` block inside the test rather than an
``@pytest_asyncio.fixture``: the teardown has to AWAIT the shutdown on the
test's own loop, and the pinned pytest-asyncio does not run async fixture
teardown there. ``gateway_boot`` is the sync fixture that binds the helper to
an isolated home::

    @pytest.mark.asyncio
    async def test_x(gateway_boot):
        async with gateway_boot() as gw:
            body = await gw.get_json("/api/health", auth=False)

Opt-in
------
The layer runs only with ``KIROCREW_INTEGRATION=1`` (the ``integration`` job
in ``ci.yml`` sets it). A bare ``pytest`` collects these files and skips them:
a boot per test is too slow for the per-commit unit shards, and the Windows
shards must not pay for a POSIX signal-handler dance they do not need.

This directory is a package (``__init__.py``) so this file imports as
``integration.conftest``. The unit files import ``test/conftest.py`` by the
bare name ``conftest``; a second top-level ``conftest`` would shadow it.

The boot reaches no real channel
--------------------------------
``KiroCrewConfig.load_credentials`` merges every ``CREDENTIAL_KEYS`` name from
``os.environ``, and the orchestrator opens Slack/Discord/... transports for
any it finds. A developer with ``SLACK_APP_TOKEN`` exported would otherwise
have a TEST connect their real workspace. ``integration_home`` deletes every
recognised credential variable for the test's duration, so the boot always
sees the no-channel configuration.

Nothing survives the boot -- not a task, a thread, a handler or an env var
--------------------------------------------------------------------------
A boot that times out, is cancelled, or whose ``run()`` dies reaps its own
task and awaits ``_shutdown()`` before the error propagates. On every exit,
normal or not, the SIGINT/SIGTERM handlers ``run()`` installed go back, and
``os.environ`` is restored to the exact mapping the boot found: production
startup writes ``KIROCREW_BOUND_PORT`` / ``KIROCREW_BOUND_HOST`` (and may grow
more), and a stale value pointing at a finished gateway is exactly the kind of
residue that explains a later test's failure in a file that never booted one.

The memory-preparation worker is a THREAD behind a process-wide fence
(``kiro_crew.memory_startup``). ``_shutdown()`` cancels its awaiter, which
does not stop the thread, and ``MemoryStartup.begin()`` refuses while the
fence is held -- so a second boot on the same home (``restart()``) would die
with "Another gateway is still preparing memory." Teardown therefore waits
for the fence to drop before it returns.

Route coverage
--------------
Every request made through :class:`IntegrationGateway` is attributed to the
aiohttp ROUTE it resolved to (``/api/sessions/abc`` counts toward
``GET /api/sessions/{key}``). With ``KIROCREW_INTEGRATION_HITS_DIR`` set, each
pytest process writes its hit set plus the full registered-route list at
session end; ``scripts/check_integration_route_coverage.py`` unions them and
reports the share of registered routes this suite exercised. That share is
the layer's primary coverage metric (line coverage is secondary here: a route
served end-to-end proves the wiring, a line reached by a mock does not).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import pytest
from aiohttp import ClientSession, ClientTimeout

from kiro_crew import memory_startup, shutdown_event
from kiro_crew.config.loader import CREDENTIAL_KEYS
from kiro_crew.testing import fake_acp_backend

#: Opt-in switch for the whole directory (see module docstring, "Opt-in").
INTEGRATION_ENV = "KIROCREW_INTEGRATION"

#: How long the in-process boot may take before the helper gives up. The
#: subprocess harness budgets 5-15s for the same boot plus interpreter start;
#: in-process there is no interpreter start, but memory preparation and the
#: fake-ACP probe are real work. A test that needs a different deadline passes
#: ``boot_secs`` to :func:`booted_gateway` itself.
DEFAULT_BOOT_TIMEOUT_SECS = 60.0

#: How long teardown waits for the memory-preparation thread to drop its fence
#: after ``_shutdown()``. The worker has no stop check inside its longest steps
#: (store repair, index rebuild), so this is the bound on one of those.
MEMORY_FENCE_DRAIN_SECS = 30.0

#: Env var naming the directory the route-hit / route-registry dumps go to.
HITS_DIR_ENV = "KIROCREW_INTEGRATION_HITS_DIR"

#: Routes hit by every request made through :class:`IntegrationGateway`, and
#: the routes the real app registered, per process. xdist workers each write
#: their own file; the coverage script unions them.
_HIT_ROUTES: set[tuple[str, str]] = set()
_REGISTERED_ROUTES: set[tuple[str, str]] = set()

#: Type of what ``gateway_boot`` returns: call it for a fresh boot on the
#: fixture's home, ``async with`` the result.
GatewayBoot = Callable[[], "contextlib.AbstractAsyncContextManager[IntegrationGateway]"]

#: aiohttp's dynamic-segment token, ``{name}`` or ``{name:regex}``.
_ROUTE_TOKEN = re.compile(r"(\{[^}]*\})")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip this directory unless the layer was asked for."""
    if os.environ.get(INTEGRATION_ENV):
        return
    here = Path(__file__).resolve().parent
    skip = pytest.mark.skip(reason=f"integration layer; set {INTEGRATION_ENV}=1 to run")
    for item in items:
        if Path(str(item.path)).resolve().is_relative_to(here):
            item.add_marker(skip)


def _dump_dir() -> Path | None:
    raw = os.environ.get(HITS_DIR_ENV)
    if not raw:
        return None
    directory = Path(raw)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Flush this process's route-hit set and route registry for the script."""
    directory = _dump_dir()
    if directory is None or not _REGISTERED_ROUTES:
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    stem = f"{worker}-{os.getpid()}"
    (directory / f"hits-{stem}.json").write_text(
        json.dumps(sorted(list(pair) for pair in _HIT_ROUTES)), encoding="utf-8"
    )
    (directory / f"routes-{stem}.json").write_text(
        json.dumps(sorted(list(pair) for pair in _REGISTERED_ROUTES)), encoding="utf-8"
    )


def _canonical_path(resource: Any) -> str | None:
    info = resource.get_info()
    return info.get("path") or info.get("formatter") or None


def _token_pattern(token: str) -> str:
    """``{name}`` -> one path segment; ``{name:regex}`` -> that regex."""
    inner = token[1:-1]
    return "(" + (inner.split(":", 1)[1] if ":" in inner else "[^/]+") + ")"


def _path_matches(canonical: str, concrete: str) -> bool:
    """Match a concrete request path against an aiohttp resource path.

    Handles the literal, ``{name}`` and ``{name:regex}`` forms. The literal
    spans are regex-escaped so a metachar in a fixed segment (a ``.`` in a
    filename route) cannot over-match; only the tokens become groups. Static
    prefix mounts (``PrefixResource``) carry no ``path``/``formatter`` and are
    not counted as routes: serving a bundled asset is not a contract this
    layer is about.
    """
    if canonical == concrete:
        return True
    if "{" not in canonical:
        return False
    pattern = "".join(
        _token_pattern(piece) if _ROUTE_TOKEN.fullmatch(piece) else re.escape(piece)
        for piece in _ROUTE_TOKEN.split(canonical)
    )
    return re.fullmatch(pattern, concrete) is not None


def _record_registered_routes(app: Any) -> None:
    for resource in app.router.resources():
        canonical = _canonical_path(resource)
        if not canonical:
            continue
        for route in resource:
            if route.method in ("HEAD", "OPTIONS"):
                continue
            _REGISTERED_ROUTES.add((route.method, canonical))


@dataclass
class IntegrationGateway:
    """Handle on one in-process gateway boot.

    ``home`` outlives a ``restart()``; the orchestrator, port and token do not.
    """

    home: Path
    orchestrator: Any
    port: int
    token: str
    _run_task: "asyncio.Task[None]"
    _client: ClientSession
    _boot_secs: float

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def state(self) -> Any:
        """The live ``DashboardState`` -- for assertions on in-memory state."""
        return self.orchestrator.dashboard_state

    @property
    def app(self) -> Any:
        """The real ``web.Application`` the orchestrator built."""
        runner = self.orchestrator._dashboard_runner
        return runner.app if runner is not None else None

    def _note_hit(self, method: str, path: str) -> None:
        # "Hit" means REQUESTED, whatever the status came back: the metric
        # measures which contracts the suite exercised, and a 4xx a test
        # proves on purpose is one of them.
        app = self.app
        if app is None:
            return
        for resource in app.router.resources():
            canonical = _canonical_path(resource)
            if not canonical or not _path_matches(canonical, path):
                continue
            for route in resource:
                if route.method in (method, "*"):
                    _HIT_ROUTES.add((route.method, canonical))
                    return

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        auth: bool = True,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> Any:
        """One HTTP request against the live gateway. Returns the response.

        ``auth=True`` (default) sends the boot token as ``?token=``; pass
        ``auth=False`` to prove the 401/403 side of a contract.
        """
        url = f"{self.base_url}{path}"
        params = dict(kwargs.pop("params", {}) or {})
        if auth:
            params["token"] = self.token
        self._note_hit(method.upper(), path.split("?", 1)[0])
        return await self._client.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=ClientTimeout(total=timeout),
            **kwargs,
        )

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, json_body: Any = None, **kw: Any) -> Any:
        return await self.request("POST", path, json_body=json_body, **kw)

    async def put(self, path: str, json_body: Any = None, **kw: Any) -> Any:
        return await self.request("PUT", path, json_body=json_body, **kw)

    async def patch(self, path: str, json_body: Any = None, **kw: Any) -> Any:
        return await self.request("PATCH", path, json_body=json_body, **kw)

    async def delete(self, path: str, **kw: Any) -> Any:
        return await self.request("DELETE", path, **kw)

    async def get_json(self, path: str, *, expect: int = 200, **kw: Any) -> Any:
        resp = await self.get(path, **kw)
        body = await resp.text()
        assert resp.status == expect, f"GET {path} -> {resp.status}: {body[:500]}"
        return json.loads(body) if body else None

    async def post_json(
        self, path: str, json_body: Any = None, *, expect: int = 200, **kw: Any
    ) -> Any:
        resp = await self.post(path, json_body=json_body, **kw)
        body = await resp.text()
        assert resp.status == expect, f"POST {path} -> {resp.status}: {body[:500]}"
        return json.loads(body) if body else None

    async def shutdown(self) -> None:
        """Stop this boot gracefully (no ``os._exit``). ``home`` is kept."""
        await _teardown_boot(self.orchestrator, self._run_task)
        await self._client.close()

    async def restart(self) -> "IntegrationGateway":
        """Stop and boot again on the SAME home -- what ``kirocrew restart`` does.

        The handle is updated in place so the enclosing ``async with`` closes
        the new boot. Everything a real restart loses (in-memory state) is lost
        here too, which is the point.
        """
        await self.shutdown()
        fresh = await _boot(self.home, boot_secs=self._boot_secs)
        self.orchestrator = fresh.orchestrator
        self.port = fresh.port
        self.token = fresh.token
        self._run_task = fresh._run_task
        self._client = fresh._client
        return self


def _snapshot_signal_handlers() -> dict[int, Any]:
    return {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}


def _restore_signal_handlers(loop: asyncio.AbstractEventLoop, saved: dict[int, Any]) -> None:
    for sig, handler in saved.items():
        with contextlib.suppress(Exception):
            loop.remove_signal_handler(sig)
        with contextlib.suppress(Exception):
            signal.signal(sig, handler)


def _restore_environ(before: dict[str, str]) -> None:
    """Put ``os.environ`` back to exactly ``before`` -- added keys go, changed
    values revert. Startup writes ``KIROCREW_BOUND_PORT`` and friends and has
    no teardown for them; this is that teardown."""
    for key in set(os.environ) - set(before):
        del os.environ[key]
    for key, value in before.items():
        if os.environ.get(key) != value:
            os.environ[key] = value


def memory_fence_held() -> bool:
    """Whether a memory-preparation owner still holds the process-wide fence."""
    return memory_startup._active is not None


async def _drain_memory_fence() -> None:
    """Wait for the memory-preparation thread to release its fence.

    ``_shutdown()`` fences new work and cancels the awaiter, but the worker
    thread keeps running to the end of its current step and only then clears
    the module-level owner. Polled off the thread rather than joined: the
    orchestrator holds no handle on the thread, only on the ``to_thread``
    awaiter it already cancelled.
    """
    deadline = time.monotonic() + MEMORY_FENCE_DRAIN_SECS
    while memory_fence_held() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


async def _teardown_boot(orchestrator: Any, run_task: "asyncio.Task[None]") -> None:
    """Cancel the ``run()`` wait (skipping ``os._exit``) and clean up for real."""
    if not run_task.done():
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await run_task
    with contextlib.suppress(Exception):
        await asyncio.wait_for(orchestrator._shutdown(), timeout=30)
    runner = getattr(orchestrator, "_dashboard_runner", None)
    if runner is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(runner.cleanup(), timeout=15)
    await _drain_memory_fence()


async def _boot(home: Path, *, boot_secs: float) -> IntegrationGateway:
    """Boot one gateway on ``home`` and return a handle once it serves HTTP.

    Every exceptional exit -- the deadline, a ``run()`` that died, a
    cancellation from outside -- reaps the boot before the error propagates
    (module docstring, "Nothing survives the boot").
    """
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.dashboard.token_auth import MAX_SESSION_TTL_SECS, generate_token
    from kiro_crew.slack.gateway import GatewayOrchestrator

    shutdown_event.clear()

    cfg = KiroCrewConfig.load()
    orchestrator = GatewayOrchestrator(
        cfg,
        no_crons=True,
        no_open=True,
        port_override="auto",
        approval_mode="reads",
        test_mode=True,
    )
    run_task = asyncio.create_task(orchestrator.run(), name="integration-gateway-run")

    deadline = time.monotonic() + boot_secs
    client = ClientSession()
    try:
        while True:
            if run_task.done():
                exc = run_task.exception() if not run_task.cancelled() else None
                raise RuntimeError(f"gateway run() ended during boot: {exc!r}")
            port = getattr(orchestrator, "_dashboard_port", 0)
            if port and orchestrator.dashboard_state is not None:
                try:
                    async with client.get(
                        f"http://127.0.0.1:{port}/api/health", timeout=ClientTimeout(total=2)
                    ) as resp:
                        if resp.status < 500:
                            break
                except Exception:
                    pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"gateway did not serve HTTP within {boot_secs}s")
            await asyncio.sleep(0.05)
    except BaseException:
        await client.close()
        await _teardown_boot(orchestrator, run_task)
        raise

    handle = IntegrationGateway(
        home=home,
        orchestrator=orchestrator,
        port=int(orchestrator._dashboard_port),
        token=generate_token(
            orchestrator._owner_id or "local-startup", ttl_seconds=MAX_SESSION_TTL_SECS
        ),
        _run_task=run_task,
        _client=client,
        _boot_secs=boot_secs,
    )
    if handle.app is not None:
        _record_registered_routes(handle.app)
    return handle


@asynccontextmanager
async def booted_gateway(
    home: Path, *, boot_secs: float = DEFAULT_BOOT_TIMEOUT_SECS
) -> AsyncIterator[IntegrationGateway]:
    """The real gateway, booted in-process on ``home``, for one ``async with``.

    Every boot is a fresh boot, on purpose: the bugs this layer chases are
    state bugs, and a shared boot would let one test's residue explain
    another's failure. Boot cost (~2s here) is the price of that isolation.
    """
    loop = asyncio.get_running_loop()
    # Snapshot BEFORE the boot installs the gateway's own SIGINT/SIGTERM
    # handlers and writes its bound-address variables; restore the snapshots
    # after -- a restart() in between must not make the gateway's own state
    # the thing we "restore" to.
    saved_signals = _snapshot_signal_handlers()
    saved_environ = dict(os.environ)

    def _put_back() -> None:
        _restore_signal_handlers(loop, saved_signals)
        _restore_environ(saved_environ)
        shutdown_event.clear()

    try:
        handle = await _boot(home, boot_secs=boot_secs)
    except BaseException:
        _put_back()
        raise
    try:
        yield handle
    finally:
        await handle.shutdown()
        _put_back()


@pytest.fixture
def integration_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh, isolated ``KIROCREW_HOME`` with the fake model wired in.

    Mirrors ``kiro_crew.testing.harness.spawn_feature_gateway``'s environment
    so a test that passes here and fails in E2E differs only in the process
    boundary, never in configuration.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # Isolate the agent-spec home too: boot rewrites managed MCP specs under
    # ``kiro_agents_dir()``, which must never be the operator's ``~/.kiro/agents``.
    monkeypatch.setenv("KIRO_HOME", str(home / "kiro"))
    monkeypatch.setenv("KIROCREW_KIRO_BIN", str(fake_acp_backend.__file__))
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    # No channel credential may reach the boot (module docstring, "The boot
    # reaches no real channel"): the orchestrator would open the transport.
    for key in CREDENTIAL_KEYS:
        monkeypatch.delenv(key, raising=False)
    return home


@pytest.fixture
def gateway_boot(integration_home: Path) -> GatewayBoot:
    """``async with gateway_boot() as gw:`` -- a fresh boot on this test's home."""
    return lambda: booted_gateway(integration_home)
