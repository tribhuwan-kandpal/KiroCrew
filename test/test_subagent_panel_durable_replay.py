"""The subagent panel's durable rebuild source.

The panel's live source is gateway memory, so a replacement gateway process has
nothing to replay for the runs it never tracked. These tests pin the persisted
fallback that answers for them, and -- just as importantly -- pin what it must
refuse to invent: a slot-tracked native card, a run whose memory mode is not
persistent, a second copy of a run the live manager still holds, and a failure
for a run the tombstone records as a user stop.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.chat_utils import subagent_event_slot
from kiro_crew.dashboard.handlers import messaging
from kiro_crew.dashboard.state import (
    NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
    PERSISTED_SUBAGENT_REPLAY_KEEP,
    PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS,
)
from kiro_crew.dashboard.ws import build_persisted_subagent_frame
from kiro_crew.dashboard.ws_event_scope import (
    persisted_replay_denial_reason,
    persisted_snapshot_admits,
    slot_owner_snapshot,
)
from kiro_crew.subagent_persistence import (
    _PANEL_AGENT_CAP,
    _PANEL_APP_CAP,
    _PANEL_CANDIDATE_MULTIPLE,
    _PANEL_ERROR_CAP,
    _PANEL_ID_CAP,
    _PANEL_PARENT_SESSION_CAP,
    _PANEL_RESULT_CAP,
    _PANEL_TASK_CAP,
    _PANEL_TRUNC_MARKER,
    PanelRecords,
    classify_persisted_ending,
    read_panel_records,
)

DAY = 86_400.0
CORRUPT = "__corrupt__"
TRUNC = _PANEL_TRUNC_MARKER


@pytest.fixture()
def agent_root(tmp_path, monkeypatch):
    """Point persistence at a registry below this test's temp directory."""
    root = tmp_path / "subagents"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", root)
    return root


def write_record(
    root,
    agent_id: str,
    *,
    task: str = "summarise the changelog",
    agent: str = "kirocrew",
    parent_session: str = "dashboard:chat-1",
    started: float | None = None,
    memory_mode: str = "persistent",
    app: str = "",
    tombstone_cause: str | None = "delivered",
    outcome: str | None = None,
    detail: str | None = None,
    died: float | None = None,
    result: str | None = None,
    mtime: float | None = None,
) -> None:
    """Write one run folder the way a real run leaves it behind.

    ``tombstone_cause=None`` writes no tombstone; ``CORRUPT`` writes one that is
    present and unparseable. Those are different states and the reader tells them
    apart, so the helper has to be able to produce both.
    """
    moment = time.time()
    folder = root / agent_id
    folder.mkdir(parents=True, exist_ok=True)
    state = {
        "id": agent_id,
        "task": task,
        "agent": agent,
        "parent_session": parent_session,
        "started": moment - 120 if started is None else started,
        "status": "running",
        "turns": 2,
        "memory_mode": memory_mode,
        "execution_context": {"memory_mode": memory_mode},
        "app": app,
        "updated_at": moment - 60,
    }
    (folder / "state.json").write_text(json.dumps(state), encoding="utf-8")
    if result is not None:
        (folder / "result.txt").write_text(result, encoding="utf-8")
    if tombstone_cause == CORRUPT:
        (folder / "tombstone.json").write_text("{not json", encoding="utf-8")
    elif tombstone_cause is not None:
        tombstone: dict = {
            "id": agent_id,
            "cause": tombstone_cause,
            "recovery_action": "delivered",
            "started": state["started"],
            "died": moment - 60 if died is None else died,
        }
        if outcome is not None:
            tombstone["outcome"] = outcome
        if detail is not None:
            tombstone["detail"] = detail
        (folder / "tombstone.json").write_text(json.dumps(tombstone), encoding="utf-8")
    if mtime is not None:
        os.utime(folder, (mtime, mtime))


def ids(records) -> list[str]:
    return [record["id"] for record in records]


def panel(**kwargs):
    """The records list alone, for cases that do not assert on the overflow."""
    return read_panel_records(**kwargs).records


class TestLiveStateWins:
    """The safety boundary: a disk record never displaces a tracked run.

    This is the property the whole fallback rests on. If disk could speak for an
    id the manager holds, a stale folder would overwrite the live card of a run
    still streaming -- turning a rebuild aid into a source of wrong answers.
    """

    def test_excluded_id_is_not_returned(self, agent_root):
        write_record(agent_root, "liveone")
        write_record(agent_root, "deadone")
        records = panel(
            keep=PERSISTED_SUBAGENT_REPLAY_KEEP,
            max_age_secs=DAY,
            exclude_ids={"liveone"},
        )
        assert ids(records) == ["deadone"]

    def test_every_id_excluded_yields_nothing(self, agent_root):
        write_record(agent_root, "aaa111")
        write_record(agent_root, "bbb222")
        records = panel(
            keep=PERSISTED_SUBAGENT_REPLAY_KEEP,
            max_age_secs=DAY,
            exclude_ids={"aaa111", "bbb222"},
        )
        assert records == []

    def test_exclusion_is_by_id_not_by_folder_order(self, agent_root):
        """A newer excluded folder must not shadow an older admissible one."""
        moment = time.time()
        write_record(agent_root, "newlive", mtime=moment - 10)
        write_record(agent_root, "olddead", mtime=moment - 1000)
        records = panel(
            keep=PERSISTED_SUBAGENT_REPLAY_KEEP,
            max_age_secs=DAY,
            exclude_ids={"newlive"},
        )
        assert ids(records) == ["olddead"]

    def test_an_excluded_folder_is_never_even_read(self, agent_root, monkeypatch):
        """Exclusion happens during the walk, so a live run costs no state read."""
        from kiro_crew import subagent_persistence

        write_record(agent_root, "liveone")
        reads: list[str] = []
        real = subagent_persistence.read_state
        monkeypatch.setattr(
            subagent_persistence,
            "read_state",
            lambda aid: (reads.append(aid), real(aid))[1],
        )
        assert panel(keep=10, max_age_secs=DAY, exclude_ids={"liveone"}) == []
        assert reads == []


class TestNativeCardsAreNotInvented:
    """Native slot-tracked cards have no durable record anywhere.

    ``create_agent_folder`` is reached only from the manager's admission pump, so
    a native run writes no folder. The fallback discovers records by walking
    folders, and these pin that the walk cannot conjure one: a native card that
    vanished with its gateway stays gone rather than reappearing as a card the
    panel cannot address.
    """

    def test_empty_registry_yields_nothing(self, agent_root):
        records = panel(keep=PERSISTED_SUBAGENT_REPLAY_KEEP, max_age_secs=DAY)
        assert records == []

    def test_manager_record_is_found_while_native_id_is_absent(self, agent_root):
        """A folder-backed run appears; a native id with no folder never does."""
        write_record(agent_root, "manager1")
        records = panel(keep=PERSISTED_SUBAGENT_REPLAY_KEEP, max_age_secs=DAY)
        assert ids(records) == ["manager1"]
        assert not any(record["id"].startswith("native:") for record in records)

    def test_a_folder_holding_only_a_result_is_not_a_record(self, agent_root):
        """No ``state.json`` means no identity, so there is nothing to replay."""
        folder = agent_root / "resultonly"
        folder.mkdir()
        (folder / "result.txt").write_text("orphan output", encoding="utf-8")
        records = panel(keep=PERSISTED_SUBAGENT_REPLAY_KEEP, max_age_secs=DAY)
        assert records == []


class TestNonPersistentRunsAreNotInvented:
    """A non-persistent run keeps its state in memory and writes no folder.

    The mode check is belt and braces on top of that: a folder whose record
    spells ``incognito`` or ``temporary`` is skipped explicitly, so the exclusion
    holds however the folder came to exist. Failing closed costs one card;
    failing open would put a private run's task text on screen.
    """

    @pytest.mark.parametrize("mode", ["incognito", "temporary", "ephemeral", ""])
    def test_non_persistent_mode_is_skipped(self, agent_root, mode):
        write_record(agent_root, "private1", memory_mode=mode)
        records = panel(keep=PERSISTED_SUBAGENT_REPLAY_KEEP, max_age_secs=DAY)
        assert records == []

    def test_mode_missing_from_the_record_is_skipped(self, agent_root):
        """An unstated mode is not assumed persistent."""
        folder = agent_root / "nomode"
        folder.mkdir()
        (folder / "state.json").write_text(
            json.dumps(
                {
                    "id": "nomode",
                    "task": "t",
                    "parent_session": "dashboard:chat-1",
                    "started": time.time() - 60,
                }
            ),
            encoding="utf-8",
        )
        records = panel(keep=PERSISTED_SUBAGENT_REPLAY_KEEP, max_age_secs=DAY)
        assert records == []

    def test_nested_execution_mode_alone_is_honoured(self, agent_root):
        """The mode is read from the execution record when the top level omits it."""
        folder = agent_root / "nested"
        folder.mkdir()
        (folder / "state.json").write_text(
            json.dumps(
                {
                    "id": "nested",
                    "task": "t",
                    "parent_session": "dashboard:chat-1",
                    "started": time.time() - 60,
                    "execution_context": {"memory_mode": "incognito"},
                }
            ),
            encoding="utf-8",
        )
        records = panel(keep=PERSISTED_SUBAGENT_REPLAY_KEEP, max_age_secs=DAY)
        assert records == []


class TestEndingClassification:
    """One classifier answers for both the list and the single-card read.

    The tombstone carries the run's own ``outcome``, so reading only ``cause``
    reports a routine user stop as a failure -- and the run's specific reason
    lives in ``detail`` while ``cause`` is a coarse bucket.
    """

    def test_a_recorded_user_stop_stays_a_stop(self, agent_root):
        write_record(agent_root, "stop111", tombstone_cause="user_stop", outcome="stopped")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "stopped"
        assert record["stopped"] is True
        assert record["error"] == ""

    def test_a_recorded_outcome_outranks_the_cause_bucket(self, agent_root):
        """``cause`` says reaped; the outcome the run recorded says stopped."""
        write_record(agent_root, "rank111", tombstone_cause="reaped", outcome="stopped")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "stopped"

    def test_a_recorded_failure_prefers_its_detail_over_the_bucket(self, agent_root):
        write_record(
            agent_root,
            "det111",
            tombstone_cause="error",
            outcome="failed",
            detail="provider returned 503",
        )
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "failed"
        assert record["error"] == "provider returned 503"

    def test_a_failure_without_detail_names_its_cause(self, agent_root):
        write_record(agent_root, "orph111", tombstone_cause="gateway_restart")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "failed"
        assert record["error"] == "Orphaned: gateway_restart"

    def test_delivered_reads_as_completed(self, agent_root):
        write_record(agent_root, "done111", tombstone_cause="delivered")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "completed"
        assert record["error"] == ""
        assert record["stopped"] is False

    def test_an_unreadable_tombstone_is_an_unknown_cause(self, agent_root):
        """An ending WAS recorded and cannot be read -- not the same as none."""
        write_record(agent_root, "corr111", tombstone_cause=CORRUPT)
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "failed"
        assert record["error"] == "Orphaned (unknown cause)"

    def test_no_tombstone_yields_no_card_rather_than_an_invented_outcome(self, agent_root):
        """A run with no recorded ending has no outcome to show.

        Calling it ``completed`` would put a green terminal card on the panel for
        a run the restart killed, contradicting the orphan notice injected for
        that same run. The reconciler yields between folders, so a tab
        reconnecting mid-scan observes this state rather than racing past it.
        """
        write_record(agent_root, "none111", tombstone_cause=None)
        assert panel(keep=10, max_age_secs=DAY) == []
        assert classify_persisted_ending(agent_root / "none111") == ("", "", False)

    def test_a_card_appears_once_the_reconciler_writes_the_ending(self, agent_root):
        """The folder is not lost -- it is waited for, then read normally."""
        write_record(agent_root, "none222", tombstone_cause=None)
        assert panel(keep=10, max_age_secs=DAY) == []
        write_record(agent_root, "none222", tombstone_cause="gateway_restart")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "failed"
        assert record["error"] == "Orphaned: gateway_restart"

    def test_an_unrecognised_outcome_falls_back_to_the_cause(self, agent_root):
        write_record(agent_root, "junk111", tombstone_cause="gateway_restart", outcome="banana")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "failed"
        assert record["error"] == "Orphaned: gateway_restart"

    def test_classifier_is_callable_on_its_own_for_the_single_card_read(self, agent_root):
        """The single-card endpoint shares this exact function, not a copy."""
        write_record(agent_root, "shar111", tombstone_cause="user_stop", outcome="stopped")
        assert classify_persisted_ending(agent_root / "shar111") == ("stopped", "", True)

    def test_classifier_takes_the_folder_so_one_response_resolves_it_once(self, agent_root):
        """Taking a path, not an id, is what lets a caller share its resolution."""
        write_record(agent_root, "path111", tombstone_cause="error", detail="boom")
        assert classify_persisted_ending(agent_root / "path111") == ("failed", "boom", False)
        assert classify_persisted_ending(agent_root / "absent") == ("", "", False)

    def test_elapsed_spans_start_to_death(self, agent_root):
        moment = time.time()
        write_record(agent_root, "span111", started=moment - 300, died=moment - 60)
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["elapsed"] == pytest.approx(240.0, abs=1.0)

    def test_death_before_start_yields_no_negative_elapsed(self, agent_root):
        moment = time.time()
        write_record(agent_root, "skew111", started=moment - 60, died=moment - 300)
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["elapsed"] == 0.0

    def test_task_and_agent_travel_with_the_record(self, agent_root):
        write_record(agent_root, "idy111", task="audit the gate", agent="kirocrew-worker")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["task"] == "audit the gate"
        assert record["agent"] == "kirocrew-worker"


class TestOwnership:
    def test_record_without_a_parent_is_dropped(self, agent_root):
        """An empty slot routes nowhere, and older clients read it as the active tab."""
        write_record(agent_root, "noown1", parent_session="")
        records = panel(keep=10, max_age_secs=DAY)
        assert records == []


class TestRetainedFieldsAreBounded:
    """A row count is not a memory bound: 50 rows of an unbounded task is unbounded.

    Every retained string carries its own named cap, and a value that was cut ends
    in the marker -- which travels inside the value to every reader, so no separate
    flag has to be carried and kept in step with it.
    """

    def test_task_is_clamped_and_says_it_was_cut(self, agent_root):
        write_record(agent_root, "big111", task="t" * (_PANEL_TASK_CAP + 500))
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert len(record["task"]) < _PANEL_TASK_CAP + 100
        assert record["task"].startswith("t" * 100)
        assert record["task"].endswith(TRUNC)

    def test_agent_is_clamped(self, agent_root):
        write_record(agent_root, "big222", agent="a" * (_PANEL_AGENT_CAP + 500))
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert len(record["agent"]) < _PANEL_AGENT_CAP + 100
        assert record["agent"].endswith(TRUNC)

    def test_error_detail_is_clamped(self, agent_root):
        write_record(
            agent_root,
            "big333",
            tombstone_cause="error",
            outcome="failed",
            detail="e" * (_PANEL_ERROR_CAP + 500),
        )
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert len(record["error"]) < _PANEL_ERROR_CAP + 100
        assert record["error"].endswith(TRUNC)

    def test_result_is_clamped_and_says_it_was_cut(self, agent_root):
        write_record(agent_root, "big444", result="x" * (_PANEL_RESULT_CAP + 5000))
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert len(record["result"]) < _PANEL_RESULT_CAP + 100
        assert record["result"].endswith(TRUNC)

    def test_a_result_exactly_at_the_cap_is_not_marked_cut(self, agent_root):
        write_record(agent_root, "exact1", result="x" * _PANEL_RESULT_CAP)
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert record["result"] == "x" * _PANEL_RESULT_CAP
        assert not record["result"].endswith(TRUNC)

    def test_ordinary_fields_carry_no_marker(self, agent_root):
        write_record(agent_root, "small1", task="short", result="also short")
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert record["task"] == "short"
        assert record["result"] == "also short"

    def test_the_scan_working_set_is_bounded_by_keep_not_folder_count(
        self, agent_root, monkeypatch
    ):
        """Unreadable candidates must not let the walk grow with the registry."""
        from kiro_crew import subagent_persistence

        moment = time.time()
        for index in range(60):
            write_record(
                agent_root,
                f"skip{index:04d}",
                memory_mode="incognito",
                mtime=moment - index,
            )
        reads: list[str] = []
        real = subagent_persistence.read_state
        monkeypatch.setattr(
            subagent_persistence,
            "read_state",
            lambda aid: (reads.append(aid), real(aid))[1],
        )
        records = panel(keep=2, max_age_secs=DAY)
        assert records == []
        assert len(reads) <= 2 * _PANEL_CANDIDATE_MULTIPLE


class TestBounds:
    def test_keep_caps_the_burst(self, agent_root):
        moment = time.time()
        for index in range(8):
            write_record(agent_root, f"agent{index:03d}", mtime=moment - index)
        records = panel(keep=3, max_age_secs=DAY)
        assert len(records) == 3

    def test_keep_of_zero_reads_nothing(self, agent_root):
        write_record(agent_root, "any111")
        assert panel(keep=0, max_age_secs=DAY) == []

    def test_newest_folders_are_preferred_when_the_cap_bites(self, agent_root):
        moment = time.time()
        write_record(agent_root, "newest", mtime=moment - 5)
        write_record(agent_root, "middle", mtime=moment - 500)
        write_record(agent_root, "oldest", mtime=moment - 5000)
        records = panel(keep=2, max_age_secs=DAY)
        assert ids(records) == ["newest", "middle"]

    def test_records_past_the_age_bound_are_dropped(self, agent_root):
        stale = time.time() - (3 * DAY)
        write_record(agent_root, "stale1", started=stale, died=stale, mtime=stale)
        records = panel(keep=10, max_age_secs=DAY)
        assert records == []

    def test_a_recently_touched_folder_is_still_judged_on_its_recorded_times(self, agent_root):
        """The folder's mtime orders the walk; the run's own times decide the window.

        A late write inside a folder -- a result chunk, a tombstone -- moves its
        mtime without moving the run. Folder mtime therefore cannot be the age
        decision, and this pins the check that is.
        """
        stale = time.time() - (3 * DAY)
        write_record(agent_root, "touched", started=stale, died=stale, mtime=time.time() - 5)
        records = panel(keep=10, max_age_secs=DAY)
        assert records == []

    def test_folders_outside_the_window_are_never_opened(self, agent_root, monkeypatch):
        """The age filter runs during the walk, so a stale folder costs no read."""
        from kiro_crew import subagent_persistence

        moment = time.time()
        write_record(agent_root, "fresh01", started=moment - 30, died=moment - 10, mtime=moment - 1)
        stale = moment - (5 * DAY)
        for index in range(30):
            write_record(
                agent_root,
                f"old{index:04d}",
                started=stale,
                died=stale,
                mtime=stale - index,
            )
        reads: list[str] = []
        real = subagent_persistence.read_state
        monkeypatch.setattr(
            subagent_persistence,
            "read_state",
            lambda aid: (reads.append(aid), real(aid))[1],
        )
        records = panel(keep=10, max_age_secs=DAY)
        assert ids(records) == ["fresh01"]
        assert reads == ["fresh01"]

    def test_a_record_inside_the_day_survives_the_native_hour(self, agent_root):
        """The whole reason this bound is its own number rather than the native TTL.

        A run that ended two hours before the gateway was replaced is outside the
        native terminal TTL and still inside this one. Sharing the hour-long
        constant would leave the panel empty in exactly the case the fallback
        exists to serve.
        """
        two_hours_ago = time.time() - 7200
        assert 7200 > NATIVE_SUBAGENT_TERMINAL_TTL_SECS
        write_record(
            agent_root, "twohr1", started=two_hours_ago, died=two_hours_ago, mtime=two_hours_ago
        )
        records = panel(
            keep=PERSISTED_SUBAGENT_REPLAY_KEEP,
            max_age_secs=PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS,
        )
        assert ids(records) == ["twohr1"]

    def test_configured_bounds_are_the_approved_policy(self):
        assert PERSISTED_SUBAGENT_REPLAY_KEEP == 50
        assert PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS == DAY


class TestResultText:
    def test_result_is_withheld_unless_asked_for(self, agent_root):
        write_record(agent_root, "res111", result="the answer")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert "result" not in record

    def test_result_is_read_when_asked_for(self, agent_root):
        write_record(agent_root, "res222", result="the answer")
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert record["result"] == "the answer"

    def test_missing_result_file_is_empty_not_an_error(self, agent_root):
        write_record(agent_root, "res333")
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert record["result"] == ""

    def test_a_sensitive_result_path_is_not_read(self, agent_root, monkeypatch):
        """The same guard the single-agent read applies, at the one place a file opens."""
        write_record(agent_root, "res555", result="secret material")
        monkeypatch.setattr("kiro_crew.subagent_persistence.is_sensitive_path", lambda path: True)
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert record["result"] == ""


class TestCorruptFolders:
    def test_unreadable_state_is_skipped_without_losing_the_rest(self, agent_root):
        moment = time.time()
        broken = agent_root / "broken"
        broken.mkdir()
        (broken / "state.json").write_text("{not json", encoding="utf-8")
        os.utime(broken, (moment - 1, moment - 1))
        write_record(agent_root, "intact", mtime=moment - 2)
        records = panel(keep=10, max_age_secs=DAY)
        assert ids(records) == ["intact"]

    def test_a_file_in_the_registry_is_not_a_record(self, agent_root):
        (agent_root / "stray.json").write_text("{}", encoding="utf-8")
        write_record(agent_root, "intact")
        records = panel(keep=10, max_age_secs=DAY)
        assert ids(records) == ["intact"]

    def test_absent_registry_reads_as_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path / "never-created"
        )
        assert panel(keep=10, max_age_secs=DAY) == []


class TestReusedSlotKeyOwnership:
    """A slot key an app's run was recorded under can later belong to another app.

    Slot keys are caller-supplied and are not namespaced by app, and the per-frame
    scope gate authorizes against the slot's CURRENT owner -- which for a reused
    key is not the owner the run belonged to. Within the replay window that hands
    the new owner the previous owner's task, agent name and error text, delivered
    once with no recall. So the run's own recorded app is compared here.
    """

    def slot(self, app: str):
        return SimpleNamespace(_app=app)

    def state(self, slots: dict):
        """A state whose ``get_slot`` behaves like the real one, plus a raw map."""
        return SimpleNamespace(_slots=slots, get_slot=slots.get)

    def test_a_slot_under_construction_denies(self):
        """``get_slot`` answers None for one, and admission must not use it."""
        state = SimpleNamespace(_slots={"chat-1": self.slot("")}, get_slot=lambda name: None)
        assert persisted_replay_denial_reason(state, "chat-1", {"app": ""}) == "slot_missing"

    @pytest.mark.parametrize(
        "record_app, slot_app, allowed",
        [
            ("", "", True),
            ("appA", "appA", True),
            ("appA", "appB", False),
            ("appA", "", False),
            ("", "appB", False),
        ],
    )
    def test_the_recorded_app_must_equal_the_slots_current_owner(
        self, record_app, slot_app, allowed
    ):
        state = self.state({"chat-1": self.slot(slot_app)})
        record = {"id": "a", "app": record_app, "parent_session": "dashboard:chat-1"}
        assert (persisted_replay_denial_reason(state, "chat-1", record) == "") is allowed

    def test_a_missing_slot_denies_because_there_is_no_owner_to_compare(self):
        state = self.state({})
        assert persisted_replay_denial_reason(state, "chat-1", {"app": ""}) == "slot_missing"

    def test_an_empty_slot_key_denies(self):
        state = self.state({"chat-1": self.slot("")})
        assert persisted_replay_denial_reason(state, "", {"app": ""}) == "slot_missing"

    def test_an_empty_slot_key_denies_even_if_a_slot_is_registered_under_it(self):
        """The empty key is refused on its own, not merely by finding no slot.

        A record whose parent resolves to nothing carries an empty slot, and a
        lookup would otherwise hand it whatever sits under that key.
        """
        state = self.state({"": self.slot("")})
        assert persisted_replay_denial_reason(state, "", {"app": ""}) == "slot_missing"

    def test_a_missing_app_key_on_the_record_reads_as_no_app(self):
        """An older record with no app field is a run no app owns."""
        state = self.state({"chat-1": self.slot("")})
        assert persisted_replay_denial_reason(state, "chat-1", {"id": "a"}) == ""
        state_app = self.state({"chat-1": self.slot("appB")})
        assert (
            persisted_replay_denial_reason(state_app, "chat-1", {"id": "a"})
            == "persisted_owner_mismatch"
        )

    def test_a_slot_without_the_attribute_reads_as_no_app(self):
        state = self.state({"chat-1": SimpleNamespace()})
        assert persisted_replay_denial_reason(state, "chat-1", {"app": ""}) == ""
        assert (
            persisted_replay_denial_reason(state, "chat-1", {"app": "appA"})
            == "persisted_owner_mismatch"
        )


class TestRecordedApp:
    def test_the_records_app_comes_from_the_run_state(self, agent_root):
        write_record(agent_root, "app111", app="my-app")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["app"] == "my-app"

    def test_a_run_no_app_owns_carries_an_empty_app(self, agent_root):
        write_record(agent_root, "app222")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["app"] == ""

    def test_the_nested_execution_app_is_used_when_the_top_level_omits_it(self, agent_root):
        folder = agent_root / "app333"
        folder.mkdir()
        (folder / "state.json").write_text(
            json.dumps(
                {
                    "id": "app333",
                    "task": "t",
                    "parent_session": "dashboard:chat-1",
                    "started": time.time() - 60,
                    "memory_mode": "persistent",
                    "execution_context": {"memory_mode": "persistent", "app": "nested-app"},
                }
            ),
            encoding="utf-8",
        )
        (folder / "tombstone.json").write_text(
            json.dumps({"cause": "delivered", "died": time.time()}), encoding="utf-8"
        )
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["app"] == "nested-app"

    def test_an_implausible_app_id_drops_the_record_rather_than_clamping_it(self, agent_root):
        """A clamped id would compare unequal while looking like a value."""
        write_record(agent_root, "app444", app="a" * (_PANEL_APP_CAP + 1))
        assert panel(keep=10, max_age_secs=DAY) == []

    def test_an_app_id_exactly_at_the_cap_is_kept_whole(self, agent_root):
        write_record(agent_root, "app555", app="a" * _PANEL_APP_CAP)
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["app"] == "a" * _PANEL_APP_CAP


class TestEqualityKeysAreRefusedNotClamped:
    """Three keys are compared for equality, so an oversized one refuses the record.

    ``id`` addresses the folder, ``app`` is matched against a slot's current
    owner, and ``parent_session`` is matched against a caller and resolved into a
    slot key. Clamping any of them would produce a value that still reads as a
    key while comparing unequal, so each is checked and the record dropped.
    """

    def test_an_oversized_parent_session_drops_the_record(self, agent_root):
        write_record(
            agent_root, "ps111", parent_session="dashboard:" + "k" * _PANEL_PARENT_SESSION_CAP
        )
        assert panel(keep=10, max_age_secs=DAY) == []

    def test_a_parent_session_exactly_at_the_cap_is_kept_whole(self, agent_root):
        key = "d" * _PANEL_PARENT_SESSION_CAP
        write_record(agent_root, "ps222", parent_session=key)
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["parent_session"] == key

    def test_an_oversized_id_drops_the_record(self, agent_root, monkeypatch):
        """The id is bounded by the filesystem in practice; the check does not rely on that."""
        from kiro_crew import subagent_persistence

        write_record(agent_root, "id111")
        monkeypatch.setattr(subagent_persistence, "_PANEL_ID_CAP", 3)
        assert panel(keep=10, max_age_secs=DAY) == []

    def test_an_oversized_app_drops_the_record(self, agent_root):
        write_record(agent_root, "ap111", app="a" * (_PANEL_APP_CAP + 1))
        assert panel(keep=10, max_age_secs=DAY) == []

    def test_every_key_within_its_cap_is_admitted(self, agent_root):
        write_record(agent_root, "ok111", app="app", parent_session="dashboard:chat-1")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["id"] == "ok111"
        assert len(record["id"]) <= _PANEL_ID_CAP


class TestOverflowIsCountedNotHidden:
    """A cut tail must not read as a population that never held those runs.

    A rebuild showing 50 of 51 eligible runs is indistinguishable from one showing
    all 50 there were, so what the row cap left out is counted and returned with
    the records, and each consumer says it once per rebuild.
    """

    def seed(self, root, count: int) -> None:
        moment = time.time()
        for index in range(count):
            write_record(root, f"ov{index:04d}", mtime=moment - index)

    def test_no_overflow_when_everything_fits(self, agent_root):
        self.seed(agent_root, 3)
        result = read_panel_records(keep=10, max_age_secs=DAY)
        assert len(result.records) == 3
        assert result.overflow == 0
        assert result.overflow_is_lower_bound is False

    def test_overflow_counts_exactly_what_the_cap_cut(self, agent_root):
        self.seed(agent_root, 7)
        result = read_panel_records(keep=4, max_age_secs=DAY)
        assert len(result.records) == 4
        assert result.overflow == 3

    def test_one_past_the_cap_is_reported(self, agent_root):
        """The case the finding named: 51 eligible, 50 shown."""
        self.seed(agent_root, 6)
        result = read_panel_records(keep=5, max_age_secs=DAY)
        assert len(result.records) == 5
        assert result.overflow == 1

    def test_overflow_counts_only_admissible_records(self, agent_root):
        """A folder past the cap that would have been skipped is not overflow."""
        moment = time.time()
        write_record(agent_root, "keep01", mtime=moment - 1)
        write_record(agent_root, "keep02", mtime=moment - 2)
        write_record(agent_root, "private", memory_mode="incognito", mtime=moment - 3)
        write_record(agent_root, "noowner", parent_session="", mtime=moment - 4)
        result = read_panel_records(keep=2, max_age_secs=DAY)
        assert ids(result.records) == ["keep01", "keep02"]
        assert result.overflow == 0

    def test_an_excluded_id_past_the_cap_is_not_overflow(self, agent_root):
        """A run the live manager holds is not something the cap withheld."""
        moment = time.time()
        write_record(agent_root, "shown1", mtime=moment - 1)
        write_record(agent_root, "livee1", mtime=moment - 2)
        result = read_panel_records(keep=1, max_age_secs=DAY, exclude_ids={"livee1"})
        assert ids(result.records) == ["shown1"]
        assert result.overflow == 0

    def test_a_record_outside_the_age_window_is_not_overflow(self, agent_root):
        moment = time.time()
        write_record(agent_root, "fresh1", mtime=moment - 1)
        stale = moment - (3 * DAY)
        write_record(agent_root, "stale1", started=stale, died=stale, mtime=stale)
        result = read_panel_records(keep=1, max_age_secs=DAY)
        assert ids(result.records) == ["fresh1"]
        assert result.overflow == 0

    def test_keep_of_zero_reports_no_overflow_rather_than_guessing(self, agent_root):
        self.seed(agent_root, 3)
        result = read_panel_records(keep=0, max_age_secs=DAY)
        assert result == PanelRecords([], 0, False)

    def test_overflow_is_flagged_as_a_floor_when_the_candidate_window_fills(
        self, agent_root, monkeypatch
    ):
        """Past the scan's own window the count can only be a lower bound."""
        from kiro_crew import subagent_persistence

        monkeypatch.setattr(subagent_persistence, "_PANEL_CANDIDATE_MULTIPLE", 1)
        self.seed(agent_root, 6)
        result = read_panel_records(keep=2, max_age_secs=DAY)
        assert len(result.records) == 2
        assert result.overflow > 0
        assert result.overflow_is_lower_bound is True

    def test_a_full_window_without_overflow_is_not_called_a_floor(self, agent_root):
        """The floor flag tracks the overflow, not the window on its own."""
        self.seed(agent_root, 3)
        result = read_panel_records(keep=10, max_age_secs=DAY)
        assert result.overflow == 0
        assert result.overflow_is_lower_bound is False

    def test_the_result_read_is_skipped_for_records_past_the_cap(self, agent_root, monkeypatch):
        """Counting overflow must not pay for output nobody will see."""
        from kiro_crew import subagent_persistence

        moment = time.time()
        for index in range(4):
            write_record(agent_root, f"rs{index:04d}", result="payload", mtime=moment - index)
        reads: list[str] = []
        real = subagent_persistence._panel_result_text
        monkeypatch.setattr(
            subagent_persistence,
            "_panel_result_text",
            lambda d: (reads.append(d.name), real(d))[1],
        )
        result = read_panel_records(keep=2, max_age_secs=DAY, include_result=True)
        assert len(result.records) == 2
        assert result.overflow == 2
        assert reads == ["rs0000", "rs0001"]


class TestPersistedListingDenialsAreAudited:
    """An authorization refusal that emits nothing leaves no artifact to review.

    The repo's own scope gate audits every decision through ``_audit_deny``, and
    these two refusals are decisions of the same kind, so they go through it too.
    Each carries its own reason: a truncation must not borrow an ownership
    refusal's reason, and an ownership refusal must not read as a scope one.
    """

    def audited(self, monkeypatch) -> list[tuple[str, str, str]]:
        calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            messaging,
            "_audit_deny",
            lambda app, event, reason: calls.append((app, event, reason)),
        )
        return calls

    def warnings(self, monkeypatch) -> list[str]:
        """Collect the handler's own WARNING lines.

        The package logger does not propagate to root, so caplog sees nothing;
        the call itself is what this pins anyway.
        """
        lines: list[str] = []
        monkeypatch.setattr(
            messaging.logger,
            "warning",
            lambda msg, *a, **k: lines.append(str(msg) % a if a else str(msg)),
        )
        return lines

    def request(self, state, caller: str, app: str = ""):
        headers = {"X-Session-Key": caller}
        return SimpleNamespace(
            app={"state": state},
            headers=headers,
            query={},
            get=lambda key, default=None: (app if key == "app" and app else default),
        )

    def state(self, slot_app: str):
        manager = SimpleNamespace(all_agents=[])
        slots = {"chat-1": SimpleNamespace(_app=slot_app)}
        return SimpleNamespace(subagents=manager, _slots=slots, get_slot=slots.get)

    def run_listing(
        self, monkeypatch, agent_root, *, slot_app: str, caller: str, scope: object, app: str = ""
    ):
        """Drive the listing end to end over one persisted record."""
        calls = self.audited(monkeypatch)

        async def fake_scope(request, op):
            return scope, None

        monkeypatch.setattr(messaging, "internal_memory_scope", fake_scope)
        state = self.state(slot_app)
        payload = asyncio.get_event_loop().run_until_complete(
            messaging.api_spawn_list(self.request(state, caller, app))
        )
        return calls, json.loads(payload.text or "{}")

    def test_the_audit_identity_is_the_app_never_the_session_key(self, agent_root, monkeypatch):
        """The dedup registry behind the audit is keyed on it and never evicted.

        A per-run session id would leave one permanent entry per subagent run, which
        is the unbounded growth the retention rule forbids.
        """
        write_record(agent_root, "aud111", app="appA", parent_session="dashboard:chat-1")
        calls, _body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="appB",
            caller="subagent:ephemeral-run-id",
            scope=object(),
            app="owning-app",
        )
        assert [app for app, _event, _reason in calls] == ["owning-app"]
        assert "subagent:ephemeral-run-id" not in [app for app, _e, _r in calls]

    def test_an_absent_app_audits_under_a_fixed_literal_not_the_caller(
        self, agent_root, monkeypatch
    ):
        write_record(agent_root, "aud222", app="appA", parent_session="dashboard:chat-1")
        calls, _body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="appB",
            caller="subagent:another-run-id",
            scope=object(),
        )
        assert [app for app, _event, _reason in calls] == ["<owner>"]

    def test_an_owner_mismatch_is_audited_and_the_record_withheld(self, agent_root, monkeypatch):
        write_record(agent_root, "own111", app="appA", parent_session="dashboard:chat-1")
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="appB",
            caller="dashboard:chat-1",
            scope=object(),
        )
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_owner_mismatch"]

    def test_a_scope_mismatch_carries_its_own_reason(self, agent_root, monkeypatch):
        write_record(agent_root, "scp111", parent_session="dashboard:chat-1")
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="",
            caller="dashboard:other",
            scope=object(),
        )
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_scope_mismatch"]

    def test_an_admitted_record_is_listed_and_audits_nothing(self, agent_root, monkeypatch):
        write_record(agent_root, "ok1111", parent_session="dashboard:chat-1")
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="",
            caller="dashboard:chat-1",
            scope=object(),
        )
        assert [entry["id"] for entry in body["agents"]] == ["ok1111"]
        assert calls == []

    def test_truncation_is_reported_and_audited_under_its_own_reason(
        self, agent_root, monkeypatch, caplog
    ):
        moment = time.time()
        for index in range(3):
            write_record(
                agent_root,
                f"tr{index:04d}",
                parent_session="dashboard:chat-1",
                mtime=moment - index,
            )
        monkeypatch.setattr(messaging, "PERSISTED_SUBAGENT_REPLAY_KEEP", 1)
        warned = self.warnings(monkeypatch)
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="",
            caller="dashboard:chat-1",
            scope=None,
        )
        assert len(body["agents"]) == 1
        assert [reason for _app, _event, reason in calls] == ["persisted_replay_truncated"]
        # Said out loud to the OPERATOR, once. The client gets no count, because
        # no panel acts on one; silence about a cut tail is what is forbidden.
        said = [line for line in warned if "truncated" in line]
        assert len(said) == 1
        # The COUNT is the payload of the report; a line that says only that
        # something was cut is the same silence in a longer sentence.
        assert "2 eligible" in said[0]
        assert "persisted_overflow" not in body

    def test_a_saturated_window_is_reported_even_at_a_zero_count(self, agent_root, monkeypatch):
        """The case the count itself cannot see.

        Every candidate is rejected, so the row cap never fires and its count
        stays zero -- while admissible older folders were never inspected.
        Gating the report on the count alone calls that "nothing was cut".
        """
        moment = time.time()
        for index in range(12):
            write_record(
                agent_root,
                f"sw{index:04d}",
                parent_session="dashboard:other",
                mtime=moment - index,
            )
        monkeypatch.setattr(messaging, "PERSISTED_SUBAGENT_REPLAY_KEEP", 2)
        warned = self.warnings(monkeypatch)
        calls, body = self.run_listing(
            monkeypatch, agent_root, slot_app="", caller="dashboard:chat-1", scope=object()
        )
        assert body["agents"] == []
        assert "persisted_replay_truncated" in [reason for _a, _e, reason in calls]
        said = [line for line in warned if "truncated" in line]
        assert len(said) == 1
        assert "scan window saturated" in said[0]

    def test_no_report_at_all_when_nothing_was_cut(self, agent_root, monkeypatch):
        write_record(agent_root, "fit111", parent_session="dashboard:chat-1")
        warned = self.warnings(monkeypatch)
        calls, body = self.run_listing(
            monkeypatch, agent_root, slot_app="", caller="dashboard:chat-1", scope=None
        )
        assert "persisted_overflow" not in body
        assert calls == []
        assert warned == []


class TestRedactionRunsBeforeClamping:
    """Clamping first cuts a credential's tail off and defeats the match.

    A value cut at the cap loses the suffix its pattern needs, so the consumers'
    own redaction cannot match the fragment that survives into the record. The
    native card path already requires this order; the persisted path follows it.
    """

    #: A value the redactors do match, so the ordering is testable at all.
    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def straddling_task(self, cap: int) -> str:
        """A task whose credential starts before the cap and ends after it."""
        return "a" * (cap - 12) + self.SECRET + "b" * 50

    def test_a_credential_straddling_the_task_cap_is_redacted(self, agent_root):
        write_record(agent_root, "sec111", task=self.straddling_task(_PANEL_TASK_CAP))
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert self.SECRET not in record["task"]
        # A clamp-first ordering would leave this recognizable head behind.
        assert "AKIA" not in record["task"]

    def test_a_credential_straddling_the_result_cap_is_redacted(self, agent_root):
        write_record(agent_root, "sec222", result=self.straddling_task(_PANEL_RESULT_CAP))
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert self.SECRET not in record["result"]
        assert "AKIA" not in record["result"]

    def test_a_credential_in_the_error_detail_is_redacted(self, agent_root):
        write_record(
            agent_root,
            "sec333",
            tombstone_cause="error",
            outcome="failed",
            detail=f"provider rejected {self.SECRET}",
        )
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert self.SECRET not in record["error"]

    def test_a_credential_in_the_agent_name_is_redacted(self, agent_root):
        write_record(agent_root, "sec444", agent=self.SECRET)
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert self.SECRET not in record["agent"]

    def test_ordinary_text_survives_redaction_unchanged(self, agent_root):
        write_record(agent_root, "sec555", task="summarise the changelog")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["task"] == "summarise the changelog"

    def test_the_result_read_stays_bounded_despite_the_margin(self, agent_root):
        """The margin is finite: a huge file is still not read whole."""
        write_record(agent_root, "sec666", result="z" * (_PANEL_RESULT_CAP * 20))
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert len(record["result"]) <= _PANEL_RESULT_CAP + len(TRUNC)


class TestSlotAbsentIsNotAnOwnershipBreach:
    """A lazily hydrated slot is ordinary, and must not be filed as a breach.

    Slots hydrate from history, so a reconnect can arrive before the slot exists.
    The live gate calls that ``slot_missing``; reporting it as an ownership
    mismatch would file every cold-start reconnect as a security event and dilute
    the stream a real cross-owner refusal has to stand out in.
    """

    def test_a_missing_slot_reports_slot_missing(self):
        state = SimpleNamespace(_slots={}, get_slot=lambda name: None)
        assert persisted_replay_denial_reason(state, "chat-1", {"app": ""}) == "slot_missing"

    def test_an_owner_difference_reports_an_ownership_mismatch(self):
        slots = {"chat-1": SimpleNamespace(_app="appB")}
        state = SimpleNamespace(_slots=slots, get_slot=slots.get)
        reason = persisted_replay_denial_reason(state, "chat-1", {"app": "appA"})
        assert reason == "persisted_owner_mismatch"

    def test_the_two_reasons_are_distinct(self):
        """The whole point: an operator can tell them apart in the audit stream."""
        empty = SimpleNamespace(_slots={}, get_slot=lambda name: None)
        slots = {"chat-1": SimpleNamespace(_app="appB")}
        owned = SimpleNamespace(_slots=slots, get_slot=slots.get)
        assert persisted_replay_denial_reason(empty, "chat-1", {"app": "appA"}) != (
            persisted_replay_denial_reason(owned, "chat-1", {"app": "appA"})
        )


class TestTheCallersFilterRunsBeforeTheCap:
    """Filtering after the cap lets a foreign record spend the caller's budget.

    Two harms, not one: a record the caller may not see occupies a slot its own
    runs need, so the caller's newest run vanishes while a stranger's is counted;
    and the overflow figure then describes a population that is not the caller's,
    which tells them how many foreign runs exist.
    """

    def mine(self, record: dict) -> bool:
        return record["app"] == "mine"

    def test_a_foreign_record_does_not_occupy_a_cap_slot(self, agent_root):
        moment = time.time()
        write_record(agent_root, "theirs1", app="theirs", mtime=moment - 1)
        write_record(agent_root, "theirs2", app="theirs", mtime=moment - 2)
        write_record(agent_root, "mine001", app="mine", mtime=moment - 3)
        result = read_panel_records(keep=2, max_age_secs=DAY, admit=self.mine)
        # Without the filter running first, the two foreign records would fill
        # the cap and this caller would see nothing of its own.
        assert ids(result.records) == ["mine001"]

    def test_a_foreign_record_is_not_counted_as_overflow(self, agent_root):
        moment = time.time()
        write_record(agent_root, "mine001", app="mine", mtime=moment - 1)
        for index in range(4):
            write_record(agent_root, f"their{index}", app="theirs", mtime=moment - 2 - index)
        result = read_panel_records(keep=1, max_age_secs=DAY, admit=self.mine)
        assert ids(result.records) == ["mine001"]
        assert result.overflow == 0

    def test_overflow_counts_only_the_callers_own_withheld_runs(self, agent_root):
        moment = time.time()
        for index in range(3):
            write_record(agent_root, f"mine{index:03d}", app="mine", mtime=moment - index)
        write_record(agent_root, "theirs1", app="theirs", mtime=moment - 10)
        result = read_panel_records(keep=1, max_age_secs=DAY, admit=self.mine)
        assert len(result.records) == 1
        assert result.overflow == 2

    def test_no_admit_filter_admits_everything(self, agent_root):
        write_record(agent_root, "any001", app="theirs")
        result = read_panel_records(keep=10, max_age_secs=DAY)
        assert ids(result.records) == ["any001"]

    def test_the_result_read_is_still_skipped_past_the_cap(self, agent_root, monkeypatch):
        from kiro_crew import subagent_persistence

        moment = time.time()
        for index in range(3):
            write_record(
                agent_root, f"mine{index:03d}", app="mine", result="payload", mtime=moment - index
            )
        reads: list[str] = []
        real = subagent_persistence._panel_result_text
        monkeypatch.setattr(
            subagent_persistence,
            "_panel_result_text",
            lambda d: (reads.append(d.name), real(d))[1],
        )
        result = read_panel_records(keep=1, max_age_secs=DAY, include_result=True, admit=self.mine)
        assert result.overflow == 2
        assert reads == ["mine000"]


class TestASaturatedScanWindowIsReportedOnItsOwn:
    """The candidate window is a SECOND bound, and it closes before admission.

    It keeps the newest folders by mtime, then validity and the caller's filter
    reject some of them. So a window filled entirely by records that are then
    rejected leaves admissible older folders uninspected while the row-cap count
    stays zero. Reporting only on a nonzero count calls that "nothing was cut".
    """

    def write_many(self, agent_root, count: int, *, app: str = "mine") -> None:
        moment = time.time()
        for index in range(count):
            write_record(agent_root, f"sat{index:04d}", app=app, mtime=moment - index)

    def test_saturation_is_flagged_even_when_the_row_count_is_zero(self, agent_root):
        # Every candidate is rejected, so overflow cannot see what was left out.
        self.write_many(agent_root, 12, app="theirs")
        result = read_panel_records(
            keep=2, max_age_secs=DAY, admit=lambda record: record["app"] == "mine"
        )
        assert result.records == []
        assert result.overflow == 0
        assert result.overflow_is_lower_bound is True

    def test_an_unsaturated_window_is_not_flagged(self, agent_root):
        self.write_many(agent_root, 2)
        result = read_panel_records(keep=10, max_age_secs=DAY)
        assert len(result.records) == 2
        assert result.overflow == 0
        assert result.overflow_is_lower_bound is False

    def test_the_flag_survives_alongside_a_real_count(self, agent_root):
        self.write_many(agent_root, 12)
        result = read_panel_records(keep=2, max_age_secs=DAY)
        assert len(result.records) == 2
        assert result.overflow > 0
        assert result.overflow_is_lower_bound is True


class TestAnAppTokenCannotReadAnotherAppsRuns:
    """An app caller and the dashboard owner both arrive with ``scope is None``.

    ``internal_memory_scope`` answers that for a non-internal caller AND for a
    verified session whose execution record is empty, so scope alone cannot tell
    an app token from the owner. The ``internal_auth`` flag can, and the app bound
    hangs off that rather than off scope.
    """

    def audited(self, monkeypatch) -> list[tuple[str, str, str]]:
        calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            messaging,
            "_audit_deny",
            lambda app, event, reason: calls.append((app, event, reason)),
        )
        return calls

    def request(self, state, *, internal: bool, app: str):
        return SimpleNamespace(
            app={"state": state},
            headers={"X-Session-Key": "dashboard:chat-1"},
            query={},
            get=lambda key, default=None: (
                True if key == "internal_auth" and internal else (app if key == "app" else default)
            ),
        )

    def state(self):
        manager = SimpleNamespace(all_agents=[])
        slots = {"chat-1": SimpleNamespace(_app="")}
        return SimpleNamespace(subagents=manager, _slots=slots, get_slot=slots.get)

    def listing(self, monkeypatch, *, internal: bool, app: str):
        calls = self.audited(monkeypatch)

        async def fake_scope(request, op):
            return None, None

        monkeypatch.setattr(messaging, "internal_memory_scope", fake_scope)
        payload = asyncio.get_event_loop().run_until_complete(
            messaging.api_spawn_list(self.request(self.state(), internal=internal, app=app))
        )
        return calls, json.loads(payload.text or "{}")

    def test_an_app_token_is_refused_another_apps_record(self, agent_root, monkeypatch):
        write_record(agent_root, "own111", app="theirs", parent_session="dashboard:chat-1")
        calls, body = self.listing(monkeypatch, internal=True, app="mine")
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_app_mismatch"]

    def test_an_app_token_still_sees_its_own_record(self, agent_root, monkeypatch):
        write_record(agent_root, "own222", app="mine", parent_session="dashboard:chat-1")
        calls, body = self.listing(monkeypatch, internal=True, app="mine")
        assert [entry["id"] for entry in body["agents"]] == ["own222"]
        assert calls == []

    def test_the_owner_is_not_narrowed_by_the_app_bound(self, agent_root, monkeypatch):
        """The owner is not app-authenticated, so the bound does not apply.

        Narrowing the owner here would empty the panel on the cold start this
        whole change exists to fix: a record's app is whatever spawned the run,
        which need not match the app of the tab now reading it.
        """
        write_record(agent_root, "own333", app="theirs", parent_session="dashboard:chat-1")
        calls, body = self.listing(monkeypatch, internal=False, app="")
        assert [entry["id"] for entry in body["agents"]] == ["own333"]
        assert calls == []


class TestOwnershipIsRecheckedOnTheLoop:
    """The snapshot sizes the cap; the loop decides delivery.

    Slot keys are caller-supplied and are not namespaced by app, so another app
    can reclaim one WHILE the off-loop scan runs. A decision taken inside the
    worker thread would then be read from state the socket does not describe,
    so the admission the scan uses is a snapshot taken on the loop beforehand and
    every surviving record is checked again on the loop before its frame is kept.

    This exercises the two real functions in the order the replay calls them,
    with the owner changing in between.
    """

    def snapshot(self, state) -> dict:
        return slot_owner_snapshot(state)

    def admitted_by(self, snapshot: dict, record: dict) -> bool:
        return persisted_snapshot_admits(
            snapshot, subagent_event_slot(str(record.get("parent_session") or "")), record
        )

    def state(self, app: str):
        slots = {"chat-1": SimpleNamespace(_app=app)}
        return SimpleNamespace(_slots=slots, get_slot=slots.get)

    def record(self, app: str) -> dict:
        return {"id": "race111", "app": app, "parent_session": "dashboard:chat-1"}

    def test_an_owner_flip_mid_scan_is_caught_by_the_loop_recheck(self):
        state = self.state("appA")
        record = self.record("appA")
        taken = self.snapshot(state)
        assert self.admitted_by(taken, record) is True
        # App B reclaims the slot while the scan is still running.
        state._slots["chat-1"]._app = "appB"
        reason = persisted_replay_denial_reason(
            state, subagent_event_slot(str(record["parent_session"])), record
        )
        assert reason == "persisted_owner_mismatch"

    def test_a_stable_owner_survives_both_checks(self):
        state = self.state("appA")
        record = self.record("appA")
        taken = self.snapshot(state)
        assert self.admitted_by(taken, record) is True
        assert (
            persisted_replay_denial_reason(
                state, subagent_event_slot(str(record["parent_session"])), record
            )
            == ""
        )

    def test_the_snapshot_refuses_a_slot_it_does_not_hold(self):
        taken = self.snapshot(self.state("appA"))
        other = {"id": "race222", "app": "appA", "parent_session": "dashboard:chat-9"}
        assert self.admitted_by(taken, other) is False

    def test_the_snapshot_refuses_a_foreign_app_so_the_cap_is_not_spent(self):
        taken = self.snapshot(self.state("appA"))
        assert self.admitted_by(taken, self.record("appB")) is False


class TestReplayFrame:
    """The frame shape the panel's reducers consume."""

    def plain(self, text: str) -> str:
        return text

    def test_frame_is_a_terminal_subagent_event(self):
        frame = build_persisted_subagent_frame(
            {
                "id": "abc123",
                "task": "audit the gate",
                "agent": "kirocrew-worker",
                "parent_session": "dashboard:chat-1",
                "started": 100.0,
                "elapsed": 42.0,
                "outcome": "completed",
                "error": "",
                "stopped": False,
            },
            redact=self.plain,
        )
        assert frame["type"] == "subagent_done"
        data = frame["data"]
        assert data["id"] == "abc123"
        assert data["slot"] == "chat-1"
        assert data["elapsed"] == 42.0
        assert data["outcome"] == "completed"
        assert data["task"] == "audit the gate"
        assert data["agent"] == "kirocrew-worker"
        assert data["stopped"] is False

    def test_a_recorded_stop_reaches_the_frame(self):
        frame = build_persisted_subagent_frame(
            {
                "id": "a",
                "parent_session": "dashboard:chat-1",
                "outcome": "stopped",
                "error": "",
                "stopped": True,
            },
            redact=self.plain,
        )
        assert frame["data"]["stopped"] is True
        assert frame["data"]["outcome"] == "stopped"

    def test_no_error_is_sent_as_null_not_an_empty_string(self):
        frame = build_persisted_subagent_frame(
            {"id": "a", "parent_session": "dashboard:chat-1", "outcome": "completed", "error": ""},
            redact=self.plain,
        )
        assert frame["data"]["error"] is None

    def test_an_error_travels_through_the_callers_redactor(self):
        frame = build_persisted_subagent_frame(
            {
                "id": "a",
                "parent_session": "dashboard:chat-1",
                "outcome": "failed",
                "error": "Orphaned: gateway_restart",
            },
            redact=lambda text: text.upper(),
        )
        assert frame["data"]["error"] == "ORPHANED: GATEWAY_RESTART"

    def test_task_and_agent_travel_through_the_callers_redactor(self):
        frame = build_persisted_subagent_frame(
            {
                "id": "a",
                "parent_session": "dashboard:chat-1",
                "outcome": "completed",
                "error": "",
                "task": "secret",
                "agent": "worker",
            },
            redact=lambda text: f"[{text}]",
        )
        assert frame["data"]["task"] == "[secret]"
        assert frame["data"]["agent"] == "[worker]"

    def test_a_record_with_no_parent_yields_an_ownerless_frame(self):
        """Which the replay's own owner filter then drops, as it does for live frames."""
        frame = build_persisted_subagent_frame(
            {"id": "a", "parent_session": "", "outcome": "completed", "error": ""},
            redact=self.plain,
        )
        assert frame["data"]["slot"] == ""
