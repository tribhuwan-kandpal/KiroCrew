"""Tests for the cron reaper that force-kills zombie cron jobs."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.cron import (
    _JOB_TIMEOUT_SECS,
    CronJob,
    CronSchedule,
    CronService,
    _process_survived,
    _ProcessHandle,
    _RunClaim,
)
from kiro_crew.cron_history import CronHistoryStore


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.reset = AsyncMock()
    sessions._sessions = {}
    # No teardown in flight: the manager's torn-down table is empty, so a live-map
    # miss is a key with no process (``SessionManager.tearing_down``).
    sessions.tearing_down = MagicMock(return_value=None)
    return sessions


def _live_task() -> MagicMock:
    """A tracked task that has not finished (the reaper's deadline path)."""
    return MagicMock(done=MagicMock(return_value=False))


def _make_job(job_id: str = "job1", name: str = "test job") -> CronJob:
    return CronJob(
        id=job_id,
        name=name,
        message="do something",
        schedule=CronSchedule(kind="every", every_secs=300),
        created_ts=time.time(),
    )


class TestCronReaper:
    """Tests for the periodic reaper that force-kills zombie cron jobs."""

    @pytest.mark.asyncio
    async def test_reaper_kills_expired_job(self, tmp_path: object) -> None:
        """Reaper marks expired job as error and emits SEL event."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        sessions = _mock_sessions()
        svc._sessions = sessions

        job = _make_job("expired1")
        svc._jobs = [job]
        claim = svc._claims["expired1"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - _JOB_TIMEOUT_SECS - 120,
            task=_live_task(),
        )

        with patch("kiro_crew.sel.sel") as mock_sel, patch.object(svc, "_save"):
            await svc._force_reap("expired1", _JOB_TIMEOUT_SECS + 120, claim=claim)

        assert job.last_status == "error"
        assert "Reaped" in (job.last_error or "")
        assert svc._reaped_jobs.has("expired1", claim)
        assert "expired1" not in svc._claims  # released
        # ``ends_conversation``: the reaper has given up on the run, so its conversation
        # is over and its sub-agent runs end with it. Asserting the whole call keeps a
        # later edit from dropping that and leaving a reaped job's children running.
        sessions.reset.assert_awaited_once_with("cron:expired1", ends_conversation=True)
        mock_sel().log_tool_invocation.assert_called_once_with(
            session_key="cron:expired1",
            source="cron",
            tool_name="reaper_force_kill",
            outcome="reaped",
            metadata={
                "job_id": "expired1",
                "session_key": "cron:expired1",
                "elapsed": _JOB_TIMEOUT_SECS + 120,
            },
        )

    @pytest.mark.asyncio
    async def test_reaper_skips_jobs_within_deadline(self) -> None:
        """Reaper does not touch jobs still within the timeout."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        sessions = _mock_sessions()
        svc._sessions = sessions

        svc._claims["ok1"] = _RunClaim(trigger="scheduled", claimed_at=time.time() - 60)  # 60s old

        with patch("kiro_crew.sel.sel"), patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        # Should not have been reaped
        assert not svc._reaped_jobs._marks  # nothing reaped
        sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reaper_skips_done_tasks(self) -> None:
        """Reaper skips jobs whose asyncio task already completed (race guard)."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        done_task = MagicMock()
        done_task.done.return_value = True
        svc._claims["done1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=done_task
        )

        with patch("kiro_crew.sel.sel"), patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert not svc._reaped_jobs._marks  # nothing reaped
        assert "done1" not in svc._claims  # cleaned up

    @pytest.mark.asyncio
    async def test_reaper_handles_reset_timeout(self, tmp_path: object) -> None:
        """Reaper falls back to SIGKILL when reset() hangs."""
        sessions = _mock_sessions()

        async def hanging_reset(key: str) -> None:
            await asyncio.sleep(999)

        sessions.reset = hanging_reset

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = sessions

        job = _make_job("hang1")
        svc._jobs = [job]
        claim = svc._claims["hang1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )

        with patch("kiro_crew.sel.sel"), patch(
            "kiro_crew.cron._REAPER_RESET_TIMEOUT", 0.05
        ), patch.object(
            svc, "_sigkill_session", new_callable=AsyncMock
        ) as mock_kill, patch.object(svc, "_save"):
            await svc._force_reap("hang1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert job.last_status == "error"
        mock_kill.assert_awaited_once_with("cron:hang1", None)

    @pytest.mark.asyncio
    async def test_reaper_sigkill_on_reset_exception(self, tmp_path: object) -> None:
        """Reaper falls back to SIGKILL when reset() raises a non-timeout exception."""
        sessions = _mock_sessions()
        sessions.reset = AsyncMock(side_effect=RuntimeError("broken"))

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = sessions

        job = _make_job("exc1")
        svc._jobs = [job]
        claim = svc._claims["exc1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 10, task=_live_task()
        )

        with patch("kiro_crew.sel.sel"), patch.object(
            svc, "_sigkill_session", new_callable=AsyncMock
        ) as mock_kill, patch.object(svc, "_save"):
            await svc._force_reap("exc1", _JOB_TIMEOUT_SECS + 10, claim=claim)

        assert job.last_status == "error"
        mock_kill.assert_awaited_once_with("cron:exc1", None)

    @pytest.mark.asyncio
    async def test_reaper_cancels_asyncio_task(self, tmp_path: object) -> None:
        """Reaper cancels the running asyncio task for the job."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("cancel1")
        svc._jobs = [job]
        mock_task = MagicMock()
        mock_task.done.return_value = False
        claim = svc._claims["cancel1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 10, task=mock_task
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("cancel1", _JOB_TIMEOUT_SECS + 10, claim=claim)

        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_reaper_persists_state(self, tmp_path: object) -> None:
        """Reaper calls _save() after updating job state."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("persist1")
        svc._jobs = [job]
        claim = svc._claims["persist1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 10, task=_live_task()
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save") as mock_save:
            await svc._force_reap("persist1", _JOB_TIMEOUT_SECS + 10, claim=claim)

        mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_reaped_flag_prevents_merge(self, tmp_path: object) -> None:
        """When reaper kills a job, _run_job_isolated skips _merge_job_result."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("reaped1")
        svc._jobs = [job]
        claim = svc._claim_run("reaped1", "scheduled")
        svc._reaped_jobs.mark("reaped1", claim)

        with patch.object(svc, "_execute_with_timeout", new_callable=AsyncMock), patch.object(
            svc, "_merge_job_result"
        ) as mock_merge:
            await svc._run_job_isolated(job, claim)

        mock_merge.assert_not_called()
        assert not svc._reaped_jobs.has("reaped1", claim)  # cleaned up

    @pytest.mark.asyncio
    async def test_reaped_flag_prevents_merge_on_cancel(self) -> None:
        """Reaped job skips merge even when CancelledError propagates."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("reaped2")
        svc._jobs = [job]
        claim = svc._claim_run("reaped2", "scheduled")
        svc._reaped_jobs.mark("reaped2", claim)

        with patch.object(
            svc, "_execute_with_timeout", side_effect=asyncio.CancelledError
        ), patch.object(svc, "_merge_job_result") as mock_merge:
            with pytest.raises(asyncio.CancelledError):
                await svc._run_job_isolated(job, claim)

        mock_merge.assert_not_called()
        assert not svc._reaped_jobs.has("reaped2", claim)

    @pytest.mark.asyncio
    async def test_non_reaped_job_merges_normally(self) -> None:
        """Normal (non-reaped) job still merges results."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("normal1")

        with patch.object(svc, "_execute_with_timeout", new_callable=AsyncMock), patch.object(
            svc, "_merge_job_result"
        ) as mock_merge:
            await svc._run_job_isolated(job, svc._claim_run("normal1", "scheduled"))

        mock_merge.assert_called_once()
        (record,) = mock_merge.call_args.args
        # The record is the job as the run left it, plus this run's own
        # generation, which only a merge ever writes to the job.
        assert record.run_generation == 1
        assert job.run_generation == 0
        record.run_generation = 0
        assert record == job

    @pytest.mark.asyncio
    async def test_start_reaper_creates_task(self) -> None:
        """start_reaper creates a background asyncio task."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        sessions = _mock_sessions()

        svc.start_reaper(sessions)
        assert svc._reaper_task is not None
        assert svc._sessions is sessions

        # Cleanup
        svc._reaper_task.cancel()
        try:
            await svc._reaper_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_stop_cancels_reaper(self) -> None:
        """stop() cancels the reaper task."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc.start_reaper(_mock_sessions())
        assert svc._reaper_task is not None

        await svc.stop()
        assert svc._reaper_task is None

    @pytest.mark.asyncio
    async def test_force_reap_without_sessions(self, tmp_path: object) -> None:
        """_force_reap handles missing sessions gracefully."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = None

        job = _make_job("nosess1")
        svc._jobs = [job]
        claim = svc._claims["nosess1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 10
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("nosess1", _JOB_TIMEOUT_SECS + 10, claim=claim)

        assert job.last_status == "error"
        assert svc._reaped_jobs.has("nosess1", claim)

    @pytest.mark.asyncio
    async def test_job_start_time_tracked(self) -> None:
        """_run_job_isolated records and cleans up start time."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        job = _make_job("track1")

        start_captured: list[bool] = []

        async def capture_start(j: CronJob, claim: object = None) -> None:
            start_captured.append(svc.running_since("track1") is not None)

        with patch.object(svc, "_execute_with_timeout", side_effect=capture_start), patch.object(
            svc, "_merge_job_result"
        ):
            await svc._run_job_isolated(job, svc._claim_run("track1", "scheduled"))

        assert start_captured == [True]  # was tracked during execution
        assert "track1" not in svc._claims  # cleaned up after

    @pytest.mark.asyncio
    async def test_reaper_loop_invokes_force_reap_for_expired_job(self) -> None:
        """Reaper loop calls _force_reap for an expired, non-done job."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        svc._claims["exp1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )

        with patch.object(
            svc, "_force_reap", new_callable=AsyncMock
        ) as mock_reap, patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        mock_reap.assert_awaited_once()
        assert mock_reap.call_args[0][0] == "exp1"

    @pytest.mark.asyncio
    async def test_force_reap_cleans_up_executing_and_running_tasks(self, tmp_path: object) -> None:
        """_force_reap releases the job's claim -- tracked task and all -- directly."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("cleanup1")
        svc._jobs = [job]
        mock_task = MagicMock(done=MagicMock(return_value=False))
        claim = svc._claims["cleanup1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 10, task=mock_task
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("cleanup1", _JOB_TIMEOUT_SECS + 10, claim=claim)

        assert "cleanup1" not in svc._claims
        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_reaper_respects_custom_timeout_secs(self) -> None:
        """Reaper does not kill a job still within its custom timeout_secs."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("custom1")
        job.timeout_secs = 5400  # 90 min
        svc._jobs = [job]
        # Running for 2000s — past default 1800 but within custom 5400
        svc._claims["custom1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - 2000, task=_live_task()
        )

        with patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert not svc._reaped_jobs._marks  # nothing reaped

    @pytest.mark.asyncio
    async def test_reaper_kills_job_exceeding_custom_timeout(self, tmp_path: object) -> None:
        """Reaper kills a job that exceeds its custom timeout_secs."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("custom2")
        job.timeout_secs = 5400
        svc._jobs = [job]
        claim = svc._claims["custom2"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - 5500, task=_live_task()
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"), patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert svc._reaped_jobs.has("custom2", claim)
        assert job.last_status == "error"
        assert "exceeded 5400s deadline" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_reaper_enforces_floor_for_low_timeout(self) -> None:
        """Reaper uses _JOB_TIMEOUT_SECS as floor even if job.timeout_secs is lower."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("floor1")
        job.timeout_secs = 600  # below 1800 floor
        svc._jobs = [job]
        # Running for 1000s — past job.timeout_secs but within floor
        svc._claims["floor1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - 1000, task=_live_task()
        )

        with patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert not svc._reaped_jobs._marks  # nothing reaped

    @pytest.mark.asyncio
    async def test_reaper_caps_at_86400(self, tmp_path: object) -> None:
        """Reaper caps deadline at 86400 even if job.timeout_secs exceeds it."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("cap1")
        job.timeout_secs = 100000  # exceeds 86400 cap
        svc._jobs = [job]
        claim = svc._claims["cap1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - 86500, task=_live_task()
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"), patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert svc._reaped_jobs.has("cap1", claim)
        assert "exceeded 86400s deadline" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_reaper_falls_back_for_deleted_job(self, tmp_path: object) -> None:
        """Reaper uses the default deadline when the job is absent from self._jobs."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        # No job in self._jobs, but the claim still stands (race: job removed while running)
        svc._jobs = []
        claim = svc._claims["ghost1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"), patch(
            "asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert svc._reaped_jobs.has("ghost1", claim)


def _one_sweep() -> Any:
    """Patch ``asyncio.sleep`` so ``_reaper_loop`` runs one sweep then unwinds."""
    return patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError]))


class TestReaperMonotonicDeadline:
    """The backstop must measure elapsed runtime on the same clock as ``wait_for``.

    ``_execute_with_timeout`` arms ``asyncio.wait_for``, whose deadline runs on
    the event loop's monotonic clock. If the reaper decides on the wall clock the
    two deadlines disagree whenever the wall clock jumps (host suspend, NTP step)
    and the backstop pre-empts a run the primary path considers healthy.
    """

    @pytest.mark.asyncio
    async def test_reaper_ignores_wallclock_jump_from_host_sleep(self) -> None:
        """A wall-clock start older than the deadline is not reaped while the
        monotonic runtime is still short (the host slept mid-run)."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("slept1")
        svc._jobs = [job]
        # Wall clock says the job has been running past its deadline only
        # because the host was suspended; it has actually executed for 60s.
        svc._claims["slept1"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - _JOB_TIMEOUT_SECS - 600,
            started_monotonic=time.monotonic() - 60,
            task=_live_task(),
        )

        with patch.object(svc, "_force_reap", new_callable=AsyncMock) as mock_reap, _one_sweep():
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        mock_reap.assert_not_awaited()
        assert not svc._reaped_jobs._marks  # nothing reaped

    @pytest.mark.asyncio
    async def test_reaper_still_kills_genuine_overrun_on_monotonic_clock(self) -> None:
        """A job whose monotonic runtime exceeds the deadline is still reaped."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        job = _make_job("over1")
        svc._jobs = [job]
        svc._claims["over1"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60,
            started_monotonic=time.monotonic() - _JOB_TIMEOUT_SECS - 60,
            task=_live_task(),
        )

        with patch.object(svc, "_force_reap", new_callable=AsyncMock) as mock_reap, _one_sweep():
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        mock_reap.assert_awaited_once()
        assert mock_reap.call_args[0][0] == "over1"

    @pytest.mark.asyncio
    async def test_run_job_isolated_stamps_and_clears_monotonic_start(self) -> None:
        """_run_job_isolated stamps the monotonic start on the claim it releases."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        job = _make_job("mono1")
        claim = svc._claim_run("mono1", "scheduled")

        seen: list[bool] = []

        async def capture(j: CronJob, claim_arg: object = None) -> None:
            seen.append(claim.started_monotonic is not None)

        with patch.object(svc, "_merge_job_result"):
            with patch.object(svc, "_execute_with_timeout", side_effect=capture):
                await svc._run_job_isolated(job, claim)

        assert seen == [True]  # was tracked during execution
        assert "mono1" not in svc._claims  # cleaned up after

    @pytest.mark.asyncio
    async def test_force_reap_clears_monotonic_start(self, tmp_path: object) -> None:
        """_force_reap pops the claim, monotonic stamp and all, so a reap never repeats."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("reap1")
        svc._jobs = [job]
        claim = svc._claims["reap1"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60,
            started_monotonic=time.monotonic() - _JOB_TIMEOUT_SECS - 60,
            task=_live_task(),
        )

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"):
            await svc._force_reap("reap1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert "reap1" not in svc._claims

    @pytest.mark.asyncio
    async def test_force_reap_releases_the_jitter_stamp_its_fenced_finalizer_skips(
        self, tmp_path: object
    ) -> None:
        """A reaped run's jitter stamp is released by the reap itself.

        ``_run_job_isolated`` stamps the jitter on its claim and its ``finally``
        releases the claim only while the run still holds it (identity).
        ``_force_reap`` takes that claim before the finalizer runs, so the fence
        is False for a reaped run and the finalizer leaves it alone; the reap
        has to release the claim itself, or a one-shot that is reaped and then
        removed keeps its entry for the process lifetime.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("reapjit1")
        svc._jobs = [job]
        claim = svc._claims["reapjit1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60
        )

        async def reap_mid_run(job_arg: CronJob, claim_arg: Any = None) -> None:
            # The sweep fires while this run is in flight: it takes the claim
            # and cancels the task, whose CancelledError lands here.
            assert claim.jitter is not None  # stamped by the run itself
            await svc._force_reap("reapjit1", _JOB_TIMEOUT_SECS + 60, claim=claim)
            raise asyncio.CancelledError

        with (
            patch("kiro_crew.sel.sel"),
            patch.object(svc, "_save"),
            patch.object(svc, "_execute_with_timeout", side_effect=reap_mid_run),
            patch.object(svc, "_merge_job_result") as mock_merge,
        ):
            with pytest.raises(asyncio.CancelledError):
                await svc._run_job_isolated(job, claim)

        mock_merge.assert_not_called()
        assert "reapjit1" not in svc._claims, (
            "the reaped run's claim was orphaned: _force_reap took the claim, the "
            "finalizer's ownership fence then skipped its release, and the claim "
            f"still stands with jitter {claim.jitter!r}"
        )

    @pytest.mark.asyncio
    async def test_reaper_done_task_cleanup_clears_monotonic_start(self) -> None:
        """The done-task cleanup branch releases the whole claim, monotonic stamp included."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        svc._claims["done2"] = _RunClaim(
            trigger="scheduled",
            claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60,
            started_monotonic=time.monotonic() - _JOB_TIMEOUT_SECS - 60,
            task=MagicMock(done=MagicMock(return_value=True)),
        )

        with patch("kiro_crew.sel.sel"), _one_sweep():
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert "done2" not in svc._claims

    @pytest.mark.asyncio
    async def test_reaper_falls_back_to_wallclock_without_monotonic_stamp(
        self, tmp_path: object
    ) -> None:
        """A claim with no monotonic stamp (a run that has not stamped one)
        still reaps, on the wall-clock elapsed."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = _mock_sessions()

        job = _make_job("legacy1")
        svc._jobs = [job]
        claim = svc._claims["legacy1"] = _RunClaim(
            trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
        )
        assert claim.started_monotonic is None

        with patch("kiro_crew.sel.sel"), patch.object(svc, "_save"), _one_sweep():
            with pytest.raises(asyncio.CancelledError):
                await svc._reaper_loop()

        assert svc._reaped_jobs.has("legacy1", claim)


class TestReaperReleasesFinishedTask:
    """A finished task the sweep meets never ran its ``finally``: release it.

    ``_run_job_isolated``'s ``finally`` releases the run's claim before its task
    ends, so a task that is ``done()`` while its claim is still stored exited
    without reaching that ``finally``, and the claim -- the job's occupancy --
    is still standing. The due-scan (``_on_timer``), ``_next_wake_secs`` and
    ``run_job`` all skip a claimed job, so
    until something releases it the job silently misses every scheduled fire.
    The manual-run route releases it only when a user clicks Run; the sweep is
    the consumer that runs on its own, so it must do the same release -- and
    on the sweep that meets the finished task, not at the run's deadline,
    which is at least ``_JOB_TIMEOUT_SECS`` and up to a day away.
    """

    @pytest.mark.asyncio
    async def test_scheduled_fire_resumes_after_the_sweep_meets_a_finished_task(
        self, tmp_path: Path
    ) -> None:
        ran: list[str] = []

        async def callback(job: CronJob) -> None:
            ran.append(job.id)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        svc._sessions = _mock_sessions()
        await svc.start()
        try:
            svc.add_job("watch", "go", every_secs=60)
            job = svc._jobs[0]
            job.last_run_ts = time.time() - 120  # due now

            async def _died_before_cleanup() -> None:
                raise RuntimeError("run ended without reaching its finally")

            # What a run leaves behind when its task ends ahead of the
            # try/finally: its claim still stored, holding the finished task
            # and the stamps -- and well inside the deadline, so the sweep's
            # timeout path is not what releases it.
            stale = asyncio.get_running_loop().create_task(_died_before_cleanup())
            await asyncio.gather(stale, return_exceptions=True)
            assert stale.done()
            svc._claims[job.id] = _RunClaim(
                trigger="scheduled",
                claimed_at=time.time() - 60,
                started_monotonic=time.monotonic() - 60,
                jitter=0.0,
                task=stale,
            )

            # Control: while the leftovers stand, the due-scan skips the job.
            await svc._on_timer()
            assert svc._claims[job.id].task is stale
            assert ran == []

            with patch("kiro_crew.sel.sel"), _one_sweep():
                with pytest.raises(asyncio.CancelledError):
                    await svc._reaper_loop()

            assert job.id not in svc._claims, (
                "the sweep met a finished task and left the job claimed, "
                "so the due-scan keeps skipping every scheduled fire of it"
            )
            # Released, not reaped: the run was over, there was nothing to kill.
            assert not svc._reaped_jobs._marks
            svc._sessions.reset.assert_not_awaited()

            # The next tick fires the job again.
            await svc._on_timer()
            fresh = svc._claims[job.id].task
            assert fresh is not None and fresh is not stale
            await fresh
            assert ran == [job.id]
        finally:
            await svc.stop()


# ── A refused or failed SIGKILL is a kill failure, not a reap ──

# The start id the fake client records at spawn (``platform_compat.get_process_start_id``
# reads ``/proc/<pid>/stat`` field 22 on Linux); the kill re-reads it before signalling.
_START_ID = "4821903"


def _session_with_pid(svc: CronService, session_key: str, pid: int | None) -> MagicMock:
    """Register a session under ``session_key`` whose ACP client reports ``pid``.

    The client carries the start id the reaper's recycled-pid check compares, so
    a test whose start-id read answers the same value drives ``_sigkill_session``
    all the way to the group kill.
    """
    client = MagicMock()
    client._pid = pid
    client._child_pids = {}
    client._start_time = _START_ID
    session = MagicMock()
    session.provider._client = client
    svc._sessions._sessions[session_key] = session
    return client


def _handle_of(svc: CronService, session_key: str) -> _ProcessHandle:
    """The kill handle ``_force_reap`` / ``cancel`` take before the reset and hand to the kill."""
    handle = svc._session_process_handle(session_key)
    assert handle is not None, f"no session registered under {session_key}"
    return handle


def _torn_down_by_the_run(svc: CronService, session_key: str) -> MagicMock:
    """Move the session under ``session_key`` from the live map into the torn-down table.

    The shape of a run whose OWN teardown reset ran first: the session is out of
    the live map (the pop happens under the registry lock, before the awaits that
    can hang), and the session manager retains it for exactly the life of that
    teardown, where ``_session_process_handle`` reads it on a live-map miss.
    """
    session = svc._sessions._sessions.pop(session_key)
    svc._sessions.tearing_down = MagicMock(
        side_effect=lambda key: session if key == session_key else None
    )
    return session


@contextmanager
def _root_reads_back(start_id: str = _START_ID) -> Iterator[None]:
    """The root's start id reads back as ``start_id`` and the platform reports the pid alive.

    Both reads are pinned: ``_process_survived`` asks the platform for liveness
    after the identity check (Windows reads a creation time back for an exited
    child whose handle is still held), and an unpinned ``pid_exists`` on a made-up
    pid answers whatever the host happens to run -- not the same on every runner.
    """
    with (
        patch("kiro_crew.platform_compat.get_process_start_id", return_value=start_id),
        patch("kiro_crew.platform_compat.pid_exists", return_value=True),
    ):
        yield


def _kill_path_stubs() -> Any:
    """The child-tree probe, the root reads and the sweep stubbed so only the kill decides."""
    return (
        patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
        _root_reads_back(),
        patch("kiro_crew.acp.client._kill_escaped_children"),
    )


def _overdue_reap_fixture(tmp_path: object, job_id: str) -> tuple[CronService, CronJob, _RunClaim]:
    """A service whose session reset fails, so ``_force_reap`` reaches the SIGKILL."""
    svc = CronService(base_dir=None, on_job=AsyncMock())
    svc._history = CronHistoryStore(base_dir=tmp_path)
    svc._sessions = _mock_sessions()
    svc._sessions.reset = AsyncMock(side_effect=RuntimeError("reset failed"))
    job = _make_job(job_id)
    svc._jobs = [job]
    claim = svc._claims[job_id] = _RunClaim(
        trigger="scheduled", claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60, task=_live_task()
    )
    return svc, job, claim


def _audited_outcome(mock_sel: MagicMock) -> str:
    return mock_sel().log_tool_invocation.call_args.kwargs["outcome"]


class TestReaperRecordsAFailedSigkill:
    """The audit never says ``reaped`` for a run whose process group was not killed.

    ``_sigkill_session`` raises nothing -- the reap must still finish the claim
    it took -- but it REPORTS what stopped the kill: the broadcast guard's
    refusal of the pid, or the error the kill raised. ``_force_reap`` carries
    that into the run's terminal record (``…; kill failed: <reason>``) and
    audits ``reaper_force_kill`` as ``failed``, the same outcome a kill await
    that raised gets. Before this, both were a log line and the audit read
    ``reaped`` while the process group kept running.
    """

    @pytest.mark.asyncio
    async def test_a_refused_pid_is_audited_as_a_failed_kill_not_reaped(
        self, tmp_path: object
    ) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "refused1")
        _session_with_pid(svc, "cron:refused1", 4242)
        refusal = ValueError("kill_process_tree: refusing non-int/reserved pid 4242")
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=refusal),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await svc._force_reap("refused1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        # The guard refused the pid outright: nothing was safe to signal, and
        # nothing was.
        pid_kill.assert_not_awaited()
        assert (
            _audited_outcome(mock_sel) == "failed"
        ), "the SEL audit says the process group was reaped while it is still alive"
        assert (job.last_error or "").startswith("Reaped after")
        assert "; kill failed: ValueError: kill_process_tree: refusing" in (job.last_error or "")
        assert "4242" in (job.last_error or ""), "the record does not name the refused pid"
        # The run still ended for the record: a terminal row, the claim
        # released, the reap marked -- the failure is added, not substituted.
        runs, total = await svc._history.get_job_history("refused1")
        assert total == 1 and runs[0]["status"] == "timeout"
        assert "; kill failed: " in runs[0]["error"]
        assert "refused1" not in svc._claims
        assert svc._reaped_jobs.has("refused1", claim)
        assert job.last_status == "error"

    @pytest.mark.asyncio
    async def test_a_group_kill_that_raises_with_no_pid_fallback_is_a_failed_kill(
        self, tmp_path: object
    ) -> None:
        """EPERM on the group and on the pid: the process is there and unsignalled."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "eperm1")
        _session_with_pid(svc, "cron:eperm1", 4343)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=PermissionError("[Errno 1] Operation not permitted")),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=PermissionError("[Errno 1] Operation not permitted")),
            ),
        ):
            await svc._force_reap("eperm1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: PermissionError: " in (job.last_error or "")
        assert "eperm1" not in svc._claims

    @pytest.mark.asyncio
    async def test_a_kill_path_error_before_the_signal_is_a_failed_kill(
        self, tmp_path: object
    ) -> None:
        """The catch-all that only logged: a probe that raises left the group unsignalled."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "probe1")
        _session_with_pid(svc, "cron:probe1", 4444)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch(
                "kiro_crew.acp.client._get_child_pids",
                side_effect=RuntimeError("cannot schedule new futures after shutdown"),
            ),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("probe1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: RuntimeError: cannot schedule" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_delivered_sigkill_is_still_audited_as_reaped(self, tmp_path: object) -> None:
        svc, job, claim = _overdue_reap_fixture(tmp_path, "killed1")
        _session_with_pid(svc, "cron:killed1", 4545)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            await svc._force_reap("killed1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_awaited_once()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_group_that_is_already_gone_is_nothing_to_kill(self, tmp_path: object) -> None:
        """ProcessLookupError on the group AND the pid: the run's process exited on its own."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "gone1")
        _session_with_pid(svc, "cron:gone1", 4646)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=ProcessLookupError("[Errno 3] No such process")),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=ProcessLookupError("[Errno 3] No such process")),
            ),
        ):
            await svc._force_reap("gone1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_group_error_followed_by_a_gone_pid_keeps_the_group_error(self) -> None:
        """EPERM on the group names members it could not signal; a gone pid does not clear it."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:mixed", 4747)
        children, start_id, sweep = _kill_path_stubs()

        with (
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=PermissionError("[Errno 1] Operation not permitted")),
            ),
            patch(
                "kiro_crew.platform_compat.kill_pid_async",
                AsyncMock(side_effect=ProcessLookupError("[Errno 3] No such process")),
            ),
        ):
            failure = await svc._sigkill_session("cron:mixed", _handle_of(svc, "cron:mixed"))

        assert failure is not None and failure.startswith("PermissionError: ")

    @pytest.mark.asyncio
    async def test_a_pid_scoped_fallback_that_lands_is_a_delivered_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POSIX: the escaped-children sweep covers what the group signal missed, so the kill counts.

        The platform seam is pinned to POSIX: this rule is the one the two Windows
        tests below invert (there the root-only fallback cannot stand in for the
        tree walk), so left to the runner's own platform the same fixture reads
        as a failed kill on a Windows shard.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:fallback", 4848)
        children, start_id, sweep = _kill_path_stubs()
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)

        with (
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=PermissionError("[Errno 1] Operation not permitted")),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
        ):
            assert await svc._sigkill_session("cron:fallback", _handle_of(svc, "cron:fallback")) is None

    @pytest.mark.asyncio
    async def test_on_windows_a_root_only_fallback_does_not_clear_a_tree_kill_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``taskkill /T`` is the only tree walker on Windows and nothing sweeps there.

        A tree kill that raised followed by a root-only ``taskkill /PID`` that
        landed leaves the descendants standing, so the group error is kept --
        unlike POSIX, where the escaped-children sweep covers them.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:win-fallback", 4949)
        children, start_id, sweep = _kill_path_stubs()
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        with (
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=PermissionError("[taskkill rc=1] Access is denied.")),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
        ):
            failure = await svc._sigkill_session("cron:win-fallback", _handle_of(svc, "cron:win-fallback"))

        assert failure is not None and failure.startswith("PermissionError: [taskkill rc=1]")

    @pytest.mark.asyncio
    async def test_on_windows_a_tree_that_is_already_gone_is_nothing_to_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Windows rule keeps only real errors: rc 128 on the tree is a gone tree."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:win-gone", 5050)
        children, start_id, sweep = _kill_path_stubs()
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        with (
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(side_effect=ProcessLookupError("[taskkill rc=128] not found")),
            ),
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock(return_value=True)),
        ):
            assert await svc._sigkill_session("cron:win-gone", _handle_of(svc, "cron:win-gone")) is None

    @pytest.mark.asyncio
    async def test_no_handle_and_no_usable_pid_are_nothing_to_kill(self) -> None:
        """The early returns are not failures: there is no process group to answer for.

        No pre-reset handle (no session was live under the key before the
        reset), or a handle with no usable pid -- nothing names a process, so
        nothing is signalled.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:nopid", None)
        _session_with_pid(svc, "cron:pid1", 1)
        no_pid = _ProcessHandle(pid=None, start_id=_START_ID, child_pids={})

        with patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill:
            assert await svc._sigkill_session("cron:absent", None) is None
            assert await svc._sigkill_session("cron:absent", no_pid) is None
            assert await svc._sigkill_session("cron:nopid", _handle_of(svc, "cron:nopid")) is None
            assert await svc._sigkill_session("cron:pid1", _handle_of(svc, "cron:pid1")) is None

        tree_kill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_successor_under_the_key_is_not_the_run_s_process(self) -> None:
        """Only the pre-reset handle names the process; a session in the map now is a successor.

        The reset pops the run's session and awaits; a cold start (a sub-agent
        completion delivering into the key) can register a NEW session under the
        same key in that window. Reading the map at kill time would signal that
        successor and leave the run's own, hung process alive -- recorded reaped.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:succ", 5151)
        handle = _handle_of(svc, "cron:succ")
        # The reset popped the run's session; a successor process now holds the key.
        svc._sessions._sessions.pop("cron:succ")
        _session_with_pid(svc, "cron:succ", 8080)
        children, start_id, sweep = _kill_path_stubs()

        with (
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            assert await svc._sigkill_session("cron:succ", handle) is None

        tree_kill.assert_awaited_once_with(5151, platform_compat.SIGKILL)

    @pytest.mark.asyncio
    async def test_a_reset_that_hangs_after_popping_the_session_still_gets_the_kill(
        self, tmp_path: object
    ) -> None:
        """The reset pops the session from the map before it can hang; the kill must not need it.

        ``SessionLifecycle.reset`` removes the map entry under its lock and only
        then awaits the shutdown that can hang. Without a handle taken before the
        reset, the fallback looked the key up, found nothing, and the run was
        audited ``reaped`` while its process kept running.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "popped1")
        _session_with_pid(svc, "cron:popped1", 5151)

        async def _pop_then_hang(session_key: str, **_: Any) -> bool:
            svc._sessions._sessions.pop(session_key, None)
            raise asyncio.TimeoutError

        svc._sessions.reset = AsyncMock(side_effect=_pop_then_hang)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            await svc._force_reap("popped1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        assert "cron:popped1" not in svc._sessions._sessions, "the fixture did not pop the session"
        tree_kill.assert_awaited_once_with(5151, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_the_root_is_verified_by_its_recorded_start_id(self) -> None:
        """A root whose live start id matches the one the client recorded is ours: killed.

        The shared child verifier denies a pid with no recorded basename, and the
        root has none, so validating the root through it never let a real kill
        through. The root is compared by start id, the recycling detector itself.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:root", 5252)

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            assert await svc._sigkill_session("cron:root", _handle_of(svc, "cron:root")) is None

        tree_kill.assert_awaited_once_with(5252, platform_compat.SIGKILL)

    @pytest.mark.asyncio
    async def test_a_recycled_pid_is_nothing_to_kill_and_only_recorded_children_are_swept(
        self,
    ) -> None:
        """A live start id that differs from the recorded one means another process owns the pid now."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        client = _session_with_pid(svc, "cron:recycled", 5353)
        client._child_pids = {6161: ("111", b"node")}

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[7171]) as probe,
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="9999999"),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            assert await svc._sigkill_session("cron:recycled", _handle_of(svc, "cron:recycled")) is None

        tree_kill.assert_not_awaited()
        # Never read through a pid that is not ours: no fresh child probe, and
        # the sweep gets only the children the client had recorded.
        probe.assert_not_called()
        sweep.assert_called_once_with({6161: ("111", b"node")})

    @pytest.mark.asyncio
    async def test_a_live_pid_whose_identity_cannot_be_confirmed_is_a_failed_kill(self) -> None:
        """Alive but unverifiable is not gone: not signalled, and recorded as a kill failure."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:unread", 5454)
        client = _session_with_pid(svc, "cron:norecord", 5555)
        client._start_time = None

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            unread = await svc._sigkill_session("cron:unread", _handle_of(svc, "cron:unread"))
            with patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID):
                no_record = await svc._sigkill_session("cron:norecord", _handle_of(svc, "cron:norecord"))

        tree_kill.assert_not_awaited()
        assert unread == "pid 5454 is alive but could not be verified as this run's; not signalled"
        assert no_record == "pid 5555 is alive but could not be verified as this run's; not signalled"

    @pytest.mark.asyncio
    async def test_a_pid_that_has_exited_is_nothing_to_kill(self) -> None:
        """No start id AND no process behind the pid: it exited; only the children are swept."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:exited", 5656)

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]) as probe,
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            assert await svc._sigkill_session("cron:exited", _handle_of(svc, "cron:exited")) is None

        tree_kill.assert_not_awaited()
        probe.assert_not_called()
        sweep.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_root_that_exits_during_the_child_walk_is_not_signalled(self) -> None:
        """The start id is read again right before the signal; a changed one is a gone root.

        The child walk awaits, and the root can exit -- and its pid be handed to
        another process -- while it does. A ``killpg`` through the pid then would
        signal that process's group. The fresh children were read through the
        same pid, so only the children recorded before the reset are swept.
        """
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        client = _session_with_pid(svc, "cron:walk", 5757)
        client._child_pids = {6262: ("222", b"node")}

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[7272]),
            patch("kiro_crew.acp.client._capture_child_records", return_value={7272: ("333", b"sh")}),
            patch("kiro_crew.acp.client._kill_escaped_children") as sweep,
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=[_START_ID, "9999999"],
            ),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            assert await svc._sigkill_session("cron:walk", _handle_of(svc, "cron:walk")) is None

        tree_kill.assert_not_awaited()
        sweep.assert_called_once_with({6262: ("222", b"node")})

    @pytest.mark.asyncio
    async def test_a_reset_that_finds_no_session_still_kills_through_the_handle(
        self, tmp_path: object
    ) -> None:
        """``reset`` answers False when the key is already gone: it stopped nothing.

        A concurrent reset popped the entry between the reap's snapshot and the
        reset's lock. Whether that reset's shutdown lands is not this reap's to
        assume: the handle says the process is standing (its recorded start id
        reads back), so the kill goes through the handle and the record says
        reaped only once it has been signalled.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "unmapped1")
        _session_with_pid(svc, "cron:unmapped1", 5858)

        async def _already_popped(session_key: str, **_: Any) -> bool:
            svc._sessions._sessions.pop(session_key, None)
            return False

        svc._sessions.reset = AsyncMock(side_effect=_already_popped)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            await svc._force_reap("unmapped1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_awaited_once_with(5858, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_session_the_run_s_own_teardown_popped_before_the_snapshot_still_gets_the_kill(
        self, tmp_path: object
    ) -> None:
        """The run's own finally reset pops the session BEFORE the reap looks; the kill still lands.

        The ordinary shape of a run that hangs in its teardown: the run body's
        ``finally`` resets its session, the reset pops the map entry under the
        registry lock and then hangs in the provider shutdown, and only later does
        the reaper measure the run over its deadline. Its live-map lookup misses,
        its own reset answers False for the already-popped key, and without the
        torn-down table there was no handle -- nothing to verify, ``reaped``
        recorded, while the hung reset (which this reap's cancel of the run task
        is about to interrupt) still held the process. The handle is read from
        the session the manager retains for the life of that teardown, and the
        kill goes to the pre-pop pid.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "torn1")
        _session_with_pid(svc, "cron:torn1", 6363)
        _torn_down_by_the_run(svc, "cron:torn1")
        svc._sessions.reset = AsyncMock(return_value=False)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            await svc._force_reap("torn1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        assert "cron:torn1" not in svc._sessions._sessions, "the fixture left the session live"
        tree_kill.assert_awaited_once_with(6363, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_refused_kill_of_a_session_the_run_s_own_teardown_popped_is_a_failed_kill(
        self, tmp_path: object
    ) -> None:
        """Same pop-before-the-snapshot shape, kill refused: ``failed``, never ``reaped``.

        Before the torn-down table this run was audited ``reaped`` with a clean
        ``last_error`` and no kill attempted at all -- the audit this PR corrects,
        on the path a hung teardown takes every time.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "torn2")
        _session_with_pid(svc, "cron:torn2", 6464)
        _torn_down_by_the_run(svc, "cron:torn2")
        svc._sessions.reset = AsyncMock(return_value=False)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(
                    side_effect=ValueError("kill_process_tree: refusing non-int/reserved pid 6464")
                ),
            ),
        ):
            await svc._force_reap("torn2", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: ValueError: kill_process_tree: refusing" in (job.last_error or "")
        assert "6464" in (job.last_error or "")
        assert "torn2" not in svc._claims

    @pytest.mark.asyncio
    async def test_the_reap_reaches_the_process_of_a_teardown_the_real_manager_holds(
        self, tmp_path: object
    ) -> None:
        """End to end through ``SessionManager``: the run's reset hangs, the reap kills, the table empties.

        The run task resets its own session and hangs in the provider shutdown;
        the reaper arrives after that pop. Through the real manager the handle
        comes from the torn-down table, the kill goes to the pre-pop pid, the
        audit says ``reaped`` for a delivered kill -- and the reap's cancel of the
        run task ends the hung teardown, whose scope releases the entry: the
        table is empty once the teardown is over.
        """
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        def _factory(session_key: Any = None, agent: Any = None, channel_id: Any = None, **_: Any) -> Any:
            provider = AsyncMock()
            provider.start = AsyncMock()
            provider.memory_mode = "persistent"
            provider.is_process_alive = lambda: True
            provider.context_usage_pct = lambda: 0.0
            provider.context_window_tokens = lambda: 0
            provider.has_active_turn = lambda: False
            provider.runtime_info = lambda: (None, None)
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        provider, _, _ = await mgr.get_or_create("cron:real1")
        mgr.release("cron:real1")
        # Above the kernel's pid ceiling: even an unpatched probe cannot meet a
        # real process under it.
        pid = 2**22 + 6565
        client = MagicMock()
        client._pid = pid
        client._child_pids = {}
        client._start_time = _START_ID
        provider._client = client
        hang = asyncio.Event()

        async def _hung_shutdown() -> None:
            await hang.wait()

        provider.shutdown = AsyncMock(side_effect=_hung_shutdown)

        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._history = CronHistoryStore(base_dir=tmp_path)
        svc._sessions = mgr
        job = _make_job("real1")
        svc._jobs = [job]
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            # The run's own finally reset: pops the session, then hangs in the
            # provider shutdown. Reap only once it is IN the shutdown: the reap's
            # cancel of the run task must land there (deferred by ``reset`` past
            # its kill-and-sweep, then re-raised); a cancel landing one await
            # earlier, at the end-record crumb hop, is absorbed by design and the
            # teardown runs on into the hang.
            run_teardown = asyncio.create_task(mgr.reset("cron:real1"))
            try:
                for _ in range(400):
                    if not mgr.has_session("cron:real1") and provider.shutdown.await_count:
                        break
                    await asyncio.sleep(0.005)
                assert not mgr.has_session("cron:real1"), "the run's reset did not pop the session"
                assert provider.shutdown.await_count == 1, "the teardown never reached the shutdown"
                assert mgr.tearing_down("cron:real1") is not None
                claim = svc._claims["real1"] = _RunClaim(
                    trigger="scheduled",
                    claimed_at=time.time() - _JOB_TIMEOUT_SECS - 60,
                    task=run_teardown,
                )

                await svc._force_reap("real1", _JOB_TIMEOUT_SECS + 60, claim=claim)

                # The reap's kill went through the torn-down handle to the pre-pop
                # pid. Its cancel of the run task (``_finish_taken_claim``) lands
                # while the reap persists its record: ``reset`` defers the
                # cancellation past its own kill-and-sweep, so the resumed
                # teardown may signal the same pid once more before re-raising --
                # every kill here names the run's own process, none a successor's.
                assert tree_kill.await_args_list[0] == call(pid, platform_compat.SIGKILL)
                assert {awaited.args[0] for awaited in tree_kill.await_args_list} == {pid}
                assert _audited_outcome(mock_sel) == "reaped"
                assert "kill failed" not in (job.last_error or "")
                assert "real1" not in svc._claims
                # The cancel ends the hung teardown, and the scope the facade
                # opened around it releases the entry.
                with pytest.raises(asyncio.CancelledError):
                    await run_teardown
            finally:
                # A failed assertion must not leave the hung teardown pending
                # past the patches (its deferred kill-and-sweep would then run
                # unstubbed): release the hang, cancel, and let it finish here.
                hang.set()
                run_teardown.cancel()
                await asyncio.gather(run_teardown, return_exceptions=True)

        assert mgr.tearing_down("cron:real1") is None, "the torn-down entry outlived its teardown"

    @pytest.mark.asyncio
    async def test_a_process_that_survives_a_completed_reset_still_gets_the_kill(
        self, tmp_path: object
    ) -> None:
        """A reset that returned True is not proof the process is gone.

        The reset's own shutdown can fail without raising out of it. After every
        completed reset the handle is asked -- pid plus recorded start id -- and
        a process that still stands gets the fallback; its outcome, not the
        reset's boolean, is what the record says.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "survived1")
        _session_with_pid(svc, "cron:survived1", 5959)
        svc._sessions.reset = AsyncMock(return_value=True)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async", AsyncMock(return_value=True)
            ) as tree_kill,
        ):
            await svc._force_reap("survived1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_awaited_once_with(5959, platform_compat.SIGKILL)
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_survivor_the_kill_cannot_signal_is_a_failed_kill_not_reaped(
        self, tmp_path: object
    ) -> None:
        """The survivor's fallback is audited on its own outcome: refused here, so ``failed``."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "survived2")
        _session_with_pid(svc, "cron:survived2", 6060)
        svc._sessions.reset = AsyncMock(return_value=True)
        children, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            children,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                AsyncMock(
                    side_effect=ValueError("kill_process_tree: refusing non-int/reserved pid 6060")
                ),
            ),
        ):
            await svc._force_reap("survived2", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        assert _audited_outcome(mock_sel) == "failed"
        assert "; kill failed: ValueError: kill_process_tree: refusing" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_process_gone_after_a_completed_reset_is_nothing_to_kill(
        self, tmp_path: object
    ) -> None:
        """Control: the reset did its job -- no start id and no process behind the pid, no kill."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "gone1")
        _session_with_pid(svc, "cron:gone1", 6161)
        svc._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("gone1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_an_exited_pid_whose_start_id_still_reads_back_is_nothing_to_kill(
        self, tmp_path: object
    ) -> None:
        """Control: identity alone is not liveness -- an exited pid the platform confirms is no survivor.

        On Windows the creation time reads back through a query handle for as long
        as any handle to the exited process object is held (the transport's, until
        GC), so a just-exited child answers the recorded start id while
        ``pid_exists`` (which confirms the exit code) says gone. Without the
        liveness read every completed reset there logged a survivor and spawned
        two ``taskkill`` runs against a dead pid before recording ``reaped``.
        """
        svc, job, claim = _overdue_reap_fixture(tmp_path, "exited1")
        _session_with_pid(svc, "cron:exited1", 6767)
        svc._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
            patch("kiro_crew.platform_compat.kill_pid_async", AsyncMock()) as pid_kill,
        ):
            await svc._force_reap("exited1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        pid_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"
        assert "kill failed" not in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_a_pid_recycled_after_a_completed_reset_is_nothing_to_kill(
        self, tmp_path: object
    ) -> None:
        """Control: a different start id behind the pid means the run's process is gone."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "recycled1")
        _session_with_pid(svc, "cron:recycled1", 6262)
        svc._sessions.reset = AsyncMock(return_value=True)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="9999999"),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("recycled1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"

    @pytest.mark.asyncio
    async def test_a_reset_that_finds_no_session_and_no_handle_is_nothing_to_kill(
        self, tmp_path: object
    ) -> None:
        """Control: no session before the reset either -- the run had no process to answer for."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "nosess1")
        svc._sessions.reset = AsyncMock(return_value=False)

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("nosess1", elapsed=_JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"

    @pytest.mark.asyncio
    async def test_a_reset_that_succeeds_never_reaches_the_kill(self, tmp_path: object) -> None:
        """Control: the SIGKILL is the fallback, so a reset that returns keeps ``reaped``."""
        svc, job, claim = _overdue_reap_fixture(tmp_path, "reset1")
        svc._sessions.reset = AsyncMock()

        with (
            patch("kiro_crew.sel.sel") as mock_sel,
            patch.object(svc, "_save"),
            patch("kiro_crew.platform_compat.kill_process_tree_async", AsyncMock()) as tree_kill,
        ):
            await svc._force_reap("reset1", _JOB_TIMEOUT_SECS + 60, claim=claim)

        tree_kill.assert_not_awaited()
        assert _audited_outcome(mock_sel) == "reaped"


class TestProcessSurvived:
    """``_process_survived`` says False only on evidence the process is gone."""

    def _handle(self, pid: int | None = 7070, start_id: str | None = _START_ID) -> _ProcessHandle:
        return _ProcessHandle(pid=pid, start_id=start_id, child_pids={})

    def test_a_live_pid_whose_start_id_reads_back_survived(self) -> None:
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
        ):
            assert _process_survived(self._handle()) is True

    def test_an_exited_pid_whose_start_id_still_reads_back_did_not_survive(self) -> None:
        """Identity is not liveness: the Windows query handle reads a dead child's creation time."""
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=_START_ID),
            patch("kiro_crew.platform_compat.pid_exists", return_value=False),
        ):
            assert _process_survived(self._handle()) is False

    def test_a_recycled_pid_is_decided_by_identity_before_the_platform_is_asked(self) -> None:
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="9999999"),
            patch("kiro_crew.platform_compat.pid_exists") as exists,
        ):
            assert _process_survived(self._handle()) is False
        exists.assert_not_called()

    def test_a_live_pid_with_no_readable_start_id_survived(self) -> None:
        """Alive but unverifiable is the kill's to decide (a named failure), not a gone process."""
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
        ):
            assert _process_survived(self._handle()) is True

    def test_no_usable_pid_did_not_survive(self) -> None:
        with patch("kiro_crew.platform_compat.get_process_start_id") as start_id:
            assert _process_survived(self._handle(pid=None)) is False
        start_id.assert_not_called()


class TestTheHandleOfATornDownSession:
    """``_session_process_handle`` reads a session the run's own teardown popped."""

    def test_a_live_miss_falls_back_to_the_manager_s_torn_down_table(self) -> None:
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:torn", 7171)
        _torn_down_by_the_run(svc, "cron:torn")

        handle = svc._session_process_handle("cron:torn")

        assert handle is not None
        assert handle.pid == 7171 and handle.start_id == _START_ID

    def test_a_live_session_is_preferred_over_the_torn_down_table(self) -> None:
        """The table is consulted only on a miss; a live entry names the run's process."""
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()
        _session_with_pid(svc, "cron:live", 7272)
        svc._sessions.tearing_down = MagicMock(side_effect=AssertionError("live hit consulted the table"))

        handle = svc._session_process_handle("cron:live")

        assert handle is not None and handle.pid == 7272

    def test_a_key_with_neither_has_no_handle(self) -> None:
        svc = CronService(base_dir=None, on_job=AsyncMock())
        svc._sessions = _mock_sessions()

        assert svc._session_process_handle("cron:nothing") is None
        svc._sessions.tearing_down.assert_called_once_with("cron:nothing")
