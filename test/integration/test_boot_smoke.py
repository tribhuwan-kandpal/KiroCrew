"""Does the real gateway boot, serve, restart and stop -- in this process?

This file is the floor the rest of ``test/integration/`` stands on. If it is
red, every other file here is red for the same reason, so keep it tiny and
keep every assertion about the HARNESS rather than about a feature.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import signal

import pytest
from integration import conftest as harness

from kiro_crew.slack.gateway import GatewayOrchestrator


def _signal_handlers() -> dict[int, object]:
    return {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}


def test_run_has_no_finally_around_shutdown_wait() -> None:
    """The boot helper cancels ``run()`` at ``await shutdown_event.wait()`` to
    skip ``os._exit``. That only works while nothing between the wait and the
    exit runs on cancellation -- i.e. no ``finally`` wraps the wait. Pin it."""
    source = inspect.getsource(GatewayOrchestrator.run)
    wait_at = source.index("await shutdown_event.wait()")
    # run() also calls _shutdown_and_exit EARLIER (the memory-preparation
    # early stop); the one that matters is the exit that follows the wait.
    exit_at = source.index("await self._shutdown_and_exit(", wait_at)
    between = source[wait_at:exit_at]
    assert "finally" not in between and "except" not in between, (
        "run() grew a handler between the shutdown wait and os._exit; the "
        "integration boot helper's cancel would now reach os._exit and kill pytest"
    )


@pytest.mark.asyncio
async def test_boots_and_serves_health(gateway_boot) -> None:
    async with gateway_boot() as gw:
        body = await gw.get_json("/api/health", auth=False)
        assert isinstance(body, dict)
        assert gw.port > 0
        assert gw.state is not None


@pytest.mark.asyncio
async def test_token_guards_the_api(gateway_boot) -> None:
    async with gateway_boot() as gw:
        ok = await gw.get("/api/sessions")
        assert ok.status == 200, await ok.text()
        denied = await gw.get("/api/sessions", auth=False)
        assert denied.status in (401, 403), await denied.text()


@pytest.mark.asyncio
async def test_restart_reboots_on_the_same_home(gateway_boot) -> None:
    async with gateway_boot() as gw:
        home = gw.home
        marker = home / "integration-restart-marker"
        marker.write_text("survives", encoding="utf-8")

        await gw.restart()

        assert gw.home == home
        assert marker.read_text(encoding="utf-8") == "survives"
        body = await gw.get_json("/api/health", auth=False)
        assert isinstance(body, dict)


@pytest.mark.asyncio
async def test_a_boot_leaves_the_process_as_it_found_it(gateway_boot) -> None:
    """Startup writes ``KIROCREW_BOUND_PORT``/``_HOST``, installs signal
    handlers and takes the memory-preparation fence; a finished boot must have
    undone all three, or the next test inherits the address of a gateway that
    is already gone and a fence that refuses its own boot."""
    environ_before = dict(os.environ)
    handlers_before = _signal_handlers()

    async with gateway_boot() as gw:
        assert os.environ.get("KIROCREW_BOUND_PORT") == str(gw.port)

    assert dict(os.environ) == environ_before
    assert _signal_handlers() == handlers_before
    assert not harness.memory_fence_held()


@pytest.mark.asyncio
async def test_a_failed_boot_leaves_nothing_running(integration_home) -> None:
    """A boot that misses its deadline reaps its own ``run()`` task and puts
    the process back, so the next test starts clean."""
    environ_before = dict(os.environ)
    handlers_before = _signal_handlers()
    tasks_before = {t for t in asyncio.all_tasks() if not t.done()}

    with pytest.raises(RuntimeError, match="did not serve HTTP"):
        async with harness.booted_gateway(integration_home, boot_secs=0.01):
            pass  # pragma: no cover -- the boot must not get this far

    leaked = {t for t in asyncio.all_tasks() if not t.done() and t not in tasks_before} - {
        asyncio.current_task()
    }
    assert not [t.get_name() for t in leaked]
    assert dict(os.environ) == environ_before
    assert _signal_handlers() == handlers_before
    assert not harness.memory_fence_held()
