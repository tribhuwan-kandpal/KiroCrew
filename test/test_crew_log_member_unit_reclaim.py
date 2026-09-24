"""Reclaiming the crew log of a member the roster does not hold.

The authorization is the roster and nothing else: a member's unit is keyed by its
slug, so the only safe question is whether a live member still derives that key.
Neither an age nor a size takes part, which is what makes the refusals the weight
of this file -- keeping a deleted member's log costs disk, while removing a LIVE
member's log destroys a history nothing can rebuild.

So the two directions are pinned side by side: a delete collects the unit, and no
shape of this path touches a unit the roster still claims -- a surviving
namesake, a sibling member, a crew whose name the roster grammar rejects, or a
config that cannot be read at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.resolution import reset_degraded_observations
from kiro_crew.crew_log.schema import KIND_MEMBER
from kiro_crew.crew_log.store import REMOVE_ABSENT, REMOVE_OWNED, REMOVE_REMOVED, crew_log_dir
from kiro_crew.dashboard.handlers import agents as agents_mod
from kiro_crew.eventlog import service as service_mod
from kiro_crew.eventlog.types import ACTIVITY_RECORD, MEMBER_CONFIG

GONE = "retired"
LIVE = "keeper"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one.

    The loader's degradation observations are cleared on both sides too: they are
    deliberately sticky for the life of a process, so the malformed-config case
    below would otherwise deny every later test in the same interpreter.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    reset_degraded_observations()
    service_mod.set_service(None)
    yield
    service_mod.set_service(None)
    reset_degraded_observations()


def _write_roster(home: Path, **members: dict) -> None:
    """Write ``config.json`` naming *members*, each value that member's record."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"agents": members}), encoding="utf-8")


def _captured() -> KiroCrewConfig:
    """The config as the delete handler captured it -- while the record still existed.

    The slug is resolved from this, so the tests that need a DIFFERENT roster at
    guard time take this first and rewrite ``config.json`` afterwards.
    """
    return KiroCrewConfig.load()


def _seed_log(slug: str, name: str, *, model: str = "m1") -> None:
    """Create *slug*'s member crew log and put one real event in it.

    Through the service, not by writing bytes: the unit has to be the one the
    product writes -- header, projections directory and all -- or a removal that
    left something behind would pass here and fail on a real member.
    """
    svc = service_mod.get_service()
    svc.ensure(slug, name)
    svc.append(slug, MEMBER_CONFIG, {"model": model, "changed": ["model"]})


def _unit(slug: str) -> Path:
    return crew_log_dir(KIND_MEMBER, slug)


# --- the unit is collected once its owner is gone ----------------------------


def test_delete_reclaims_the_departed_member_unit(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    _seed_log(GONE, GONE)
    assert _unit(GONE).is_dir()

    captured = _captured()
    _write_roster(home)  # the delete committed: the record is gone
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert not _unit(GONE).exists()


def test_a_member_with_an_explicit_id_is_reclaimed_by_that_id(tmp_path):
    """The unit is keyed by the persisted ``member_id``, not by the folded name."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {"member_id": "mem-7"}})
    _seed_log("mem-7", GONE)
    _seed_log(GONE, "a different member")  # what the NAME alone would fold to

    captured = _captured()
    _write_roster(home)
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert not _unit("mem-7").exists()
    # The name fold is a different unit and was never this member's history.
    assert _unit(GONE).is_dir()


def test_reclaim_forgets_the_slug_so_a_later_read_answers_empty(tmp_path):
    """The service must not answer for a member whose files it just removed."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    _seed_log(GONE, GONE)
    svc = service_mod.get_service()
    assert svc.snapshot(GONE)["asOfSeq"] >= 0

    captured = _captured()
    _write_roster(home)
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert svc.snapshot(GONE) == {"asOfSeq": -1, "values": {}}
    assert svc.last_seq(GONE) == -1


def test_a_member_that_never_wrote_a_log_is_not_an_error(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    captured = _captured()
    _write_roster(home)

    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)  # must not raise

    assert not _unit(GONE).exists()


# --- a LIVE member's log is never taken by this path -------------------------


def test_a_live_member_is_never_reclaimed(tmp_path):
    """The safety boundary: the roster still holds the name, so nothing goes.

    The handler only reaches this after committing the delete, so a roster that
    still names the member means a same-name record was committed in the window --
    and that record's own unit is THIS one, because the slug is the key.
    """
    home = tmp_path / "home"
    _write_roster(home, **{LIVE: {}})
    _seed_log(LIVE, LIVE)
    before = (_unit(LIVE) / "log.jsonl").read_bytes()

    agents_mod._reclaim_deleted_member_crew_log(LIVE, _captured())

    assert _unit(LIVE).is_dir()
    assert (_unit(LIVE) / "log.jsonl").read_bytes() == before


def test_a_recreated_namesake_keeps_the_unit(tmp_path):
    """Recreation between the commit and the removal is what the re-decision is for."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    _seed_log(GONE, GONE)
    captured = _captured()

    # The delete committed, then a same-name member was created before the removal
    # got to decide. It derives the same slug, so this unit is now its history.
    _write_roster(home, **{GONE: {}})
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert _unit(GONE).is_dir()
    assert (_unit(GONE) / "log.jsonl").read_text(encoding="utf-8").strip()


def test_deleting_one_member_leaves_its_siblings_untouched(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}, LIVE: {}})
    _seed_log(GONE, GONE)
    _seed_log(LIVE, LIVE)

    captured = _captured()
    _write_roster(home, **{LIVE: {}})
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert not _unit(GONE).exists()
    assert _unit(LIVE).is_dir()


def test_a_live_member_whose_name_the_roster_grammar_rejects_is_still_claimed(tmp_path):
    """Existence is not addressability: an ungrammatical name can be a live crew.

    The create route checks a crew name for credential shape only, so the roster
    view's grammar filter must not decide this -- filtering such a crew out would
    report a live owner as gone and hand its history to the removal.
    """
    home = tmp_path / "home"
    odd = "Crew Member!"
    _write_roster(home, **{odd: {"member_id": "mem-odd"}})
    _seed_log("mem-odd", odd)

    agents_mod._reclaim_deleted_member_crew_log(odd, _captured())

    assert _unit("mem-odd").is_dir()


def test_an_unreadable_roster_removes_nothing(tmp_path):
    """Fails closed: no roster to prove the owner is gone means the log stays."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    _seed_log(GONE, GONE)
    captured = _captured()

    (home / "config.json").write_text("{ not json", encoding="utf-8")
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert _unit(GONE).is_dir()


# --- the service door's own contract ----------------------------------------


def test_the_guard_refusal_is_reported_as_owned_not_removed(tmp_path):
    """The re-decision's refusal is reported rather than swallowed."""
    _write_roster(tmp_path / "home", **{LIVE: {}})
    _seed_log(LIVE, LIVE)

    status = service_mod.get_service().remove_unit(LIVE, still_unclaimed=lambda: False)

    assert status == REMOVE_OWNED
    assert _unit(LIVE).is_dir()


def test_the_predicate_is_asked_under_the_hold_and_a_true_answer_removes(tmp_path):
    """The same call with a true predicate removes, so the refusal above IS the guard."""
    _write_roster(tmp_path / "home", **{GONE: {}})
    _seed_log(GONE, GONE)
    asked: list[bool] = []

    def still_unclaimed() -> bool:
        # The unit is intact when the guard runs: the removal happens after it.
        asked.append(_unit(GONE).is_dir())
        return True

    status = service_mod.get_service().remove_unit(GONE, still_unclaimed=still_unclaimed)

    assert status == REMOVE_REMOVED
    assert asked == [True]
    assert not _unit(GONE).exists()


# --- the claim predicate itself ---------------------------------------------


@pytest.mark.parametrize(
    "roster, slug, claimed",
    [
        ({LIVE: {}}, LIVE, True),
        ({LIVE: {}}, GONE, False),
        ({}, LIVE, False),
        ({LIVE: {"member_id": "mem-1"}}, "mem-1", True),
        # An explicit id, not the name, is what derives the unit.
        ({LIVE: {"member_id": "mem-1"}}, LIVE, False),
    ],
)
def test_claim_predicate_reads_the_roster_by_persisted_identity(tmp_path, roster, slug, claimed):
    _write_roster(tmp_path / "home", **roster)
    assert agents_mod._member_slug_is_claimed(slug) is claimed


# --- the decision happens under the cross-process hold -----------------------
#
# The guard re-reads the roster, so on its own it is a snapshot: between that
# read and the unlink, another PROCESS allocating a member id derives the same
# slug and addresses the same unit. The namespace lock is the only seam those
# allocators share, so the removal has to be decided inside it, and nothing
# rebuilds a crew log if it is not.


@pytest.fixture
def _owner_caller(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _delete_request(name: str):
    from unittest.mock import MagicMock

    from aiohttp import web

    request = MagicMock(spec=web.Request)
    request.method = "DELETE"
    request.match_info = {"name": name}
    # No dashboard state: the handler's session and refresh hooks are all
    # None-guarded, which keeps the test on the delete path itself.
    request.app = {"state": None}
    return request


def _namespace_lock_is_held() -> bool:
    """Whether THIS thread holds the memory-store namespace lock.

    Read from the lock's own reentrancy bookkeeping, which records the roots a
    thread is holding, so this answers about the real hold rather than a stand-in
    for it.
    """
    from kiro_crew import memory_stores

    roots = getattr(memory_stores._NAMESPACE_LOCK_STATE, "roots", None) or set()
    return memory_stores.memory_stores_root().resolve() in roots


@pytest.mark.asyncio
async def test_the_delete_route_reclaims_the_unit_while_holding_the_namespace_lock(
    tmp_path, monkeypatch, _owner_caller
):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"agents": {LIVE: {}, GONE: {}}, "default_agent": LIVE}), encoding="utf-8"
    )
    _seed_log(GONE, GONE)
    assert _unit(GONE).is_dir()

    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agent_delete

    held: list[bool] = []
    real = agents_mod._reclaim_deleted_member_crew_log

    def _spy(name: str, cfg: KiroCrewConfig) -> None:
        held.append(_namespace_lock_is_held())
        real(name, cfg)

    monkeypatch.setattr(agents_mod, "_reclaim_deleted_member_crew_log", _spy)

    resp = await api_kirocrew_agent_delete(_delete_request(GONE))

    assert resp.status == 200
    assert GONE not in KiroCrewConfig.load().agents
    assert held == [True]
    assert not _unit(GONE).exists()


def test_no_reclaim_call_sits_outside_a_namespace_lock_hold():
    """Structural, because the defect this pins is placement rather than logic.

    A call moved back out of the hold still passes every behavioural case above:
    the removal is correct, only unprotected. So every mention of the reclaim
    inside the delete route -- called directly, or handed to a thread runner as a
    value -- has to be lexically inside a function the lock decorates.
    """
    import ast
    import inspect

    source = inspect.getsource(agents_mod)
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def _is_lock_decorated(node: ast.AST) -> bool:
        decorators = getattr(node, "decorator_list", [])
        return any("memory_store_namespace_lock" in ast.dump(d) for d in decorators)

    route = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "api_kirocrew_agent_delete"
    )
    mentions = [
        n
        for n in ast.walk(route)
        if isinstance(n, ast.Name) and n.id == "_reclaim_deleted_member_crew_log"
    ]
    assert mentions, "the delete route must reclaim the departed member's crew log"
    for mention in mentions:
        holders = []
        walker: ast.AST | None = mention
        while walker is not None and walker is not route:
            if isinstance(walker, (ast.FunctionDef, ast.AsyncFunctionDef)):
                holders.append(_is_lock_decorated(walker))
            walker = parents.get(walker)
        assert any(holders), f"line {mention.lineno} reclaims outside the namespace lock"


# --- the legacy source goes with the unit ------------------------------------
#
# The rows are the member's own pre-log history and they live OUTSIDE the unit,
# while the marker recording that they were folded lives inside it. Taking the
# unit alone would leave the history on disk and re-arm the fold, so the next
# fresh `ensure` -- in this process or any other writer's -- would read the source
# again and rebuild the log from it.


def _legacy_activity(slug: str, name: str, home: Path, *, rows: int = 2) -> Path:
    """Write the pre-fold legacy activity file `ensure` folds on a fresh create."""
    member_dir = home / "members" / slug
    member_dir.mkdir(parents=True, exist_ok=True)
    path = member_dir / "activity.jsonl"
    path.write_text(
        "".join(
            json.dumps({"ts": f"2026-01-0{i + 1}T00:00:00Z", "kind": "message", "name": name})
            + "\n"
            for i in range(rows)
        ),
        encoding="utf-8",
    )
    return path


def _event_type(event) -> str:
    """An event's type, whichever shape the reader hands back."""
    if isinstance(event, dict):
        return str(event.get("type", ""))
    return str(getattr(event, "type", ""))


def _legacy_names(slug: str, home: Path) -> list[Path]:
    member_dir = home / "members" / slug
    return [
        member_dir / "activity.jsonl",
        member_dir / "activity.jsonl.1",
        member_dir / "activity.jsonl.migrated",
        member_dir / "activity.jsonl.migrated.1",
    ]


def test_the_reclaim_takes_the_legacy_activity_source_too(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    _seed_log(GONE, GONE)
    for path in _legacy_names(GONE, home):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    assert all(p.exists() for p in _legacy_names(GONE, home))

    captured = _captured()
    _write_roster(home)  # the delete committed: the record is gone
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert not _unit(GONE).exists()
    assert [p.name for p in _legacy_names(GONE, home) if p.exists()] == []


def test_an_append_after_the_reclaim_cannot_refold_a_history(tmp_path):
    """The point of taking the source: a late append rebuilds nothing."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    _seed_log(GONE, GONE)
    _legacy_activity(GONE, GONE, home, rows=3)

    captured = _captured()
    _write_roster(home)
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    # The queued append, arriving on its worker after the removal.
    service_mod.get_service().ensure(GONE, GONE)

    events = service_mod.get_service().history(GONE, limit=200)
    folded = [e for e in events if _event_type(e) == ACTIVITY_RECORD]
    assert folded == []
    # Vacuously-empty is not the claim: a live member's fold DOES produce these,
    # so the same read on a member whose source survived must find them.
    _write_roster(home, **{LIVE: {}})
    _legacy_activity(LIVE, LIVE, home, rows=3)
    service_mod.get_service().ensure(LIVE, LIVE)
    live = service_mod.get_service().history(LIVE, limit=200)
    assert [e for e in live if _event_type(e) == ACTIVITY_RECORD] != []


def test_a_live_members_legacy_activity_is_never_taken(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}, LIVE: {}})
    _seed_log(GONE, GONE)
    _seed_log(LIVE, LIVE)
    live_legacy = _legacy_activity(LIVE, LIVE, home)

    captured = _captured()
    _write_roster(home, **{LIVE: {}})
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert live_legacy.exists()
    assert _unit(LIVE).is_dir()


def test_a_refused_reclaim_leaves_the_legacy_source_alone(tmp_path):
    """The source goes only when the unit went: a guard refusal touches nothing."""
    home = tmp_path / "home"
    _write_roster(home, **{LIVE: {}})
    _seed_log(LIVE, LIVE)
    legacy = _legacy_activity(LIVE, LIVE, home)

    status = service_mod.get_service().remove_unit(LIVE, still_unclaimed=lambda: False)

    assert status == REMOVE_OWNED
    assert legacy.exists()
    assert _unit(LIVE).is_dir()


def test_a_symlinked_legacy_activity_is_refused_not_followed(tmp_path):
    """That directory is agent-writable, so a name there is not proof of its target."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    _seed_log(GONE, GONE)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    link = home / "members" / GONE / "activity.jsonl"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    captured = _captured()
    _write_roster(home)
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert not _unit(GONE).exists()
    assert outside.exists()


def test_a_linked_member_directory_cannot_reach_a_peers_history(tmp_path):
    """The directory name is checked AS WRITTEN, before anything resolves it.

    `member_dir` resolves and then only containment-checks, so a link named for the
    departing member but pointing at a LIVE peer's directory passes that check and
    hands back the peer's real files -- where a link test on the leaves is false.
    That peer's activity file is the sole copy of its pre-log history whenever its
    own fold has not run yet, so the removal has to refuse on the unresolved name.
    """
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}, LIVE: {}})
    _seed_log(GONE, GONE)
    _seed_log(LIVE, LIVE)
    victim = _legacy_activity(LIVE, LIVE, home, rows=4)
    victim_bytes = victim.read_bytes()

    # The departing member's own directory is replaced by a link to the peer's.
    departing = home / "members" / GONE
    if departing.exists():
        for child in departing.iterdir():
            child.unlink()
        departing.rmdir()
    departing.symlink_to(home / "members" / LIVE, target_is_directory=True)

    captured = _captured()
    _write_roster(home, **{LIVE: {}})
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert victim.exists()
    assert victim.read_bytes() == victim_bytes
    assert _unit(LIVE).is_dir()


def _report_as_junction(monkeypatch, target: Path) -> None:
    """Make *target* read the way a Windows junction does: redirecting, not a link.

    A junction redirects like the symlink below it, but ``is_symlink`` answers
    False for one, so a caller testing only that follows it into the target. Linux
    grows no junctions, so the two predicates are steered for this one path and
    delegate everywhere else -- the redirect itself is real.
    """
    real_symlink = Path.is_symlink
    real_junction = Path.is_junction

    def is_symlink(self):
        return False if self == target else real_symlink(self)

    def is_junction(self):
        return True if self == target else real_junction(self)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    monkeypatch.setattr(Path, "is_junction", is_junction)


def test_a_junction_named_member_directory_cannot_reach_a_peers_history(tmp_path, monkeypatch):
    """The same swap in its Windows spelling, which `is_symlink` reads as a directory."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}, LIVE: {}})
    _seed_log(GONE, GONE)
    _seed_log(LIVE, LIVE)
    victim = _legacy_activity(LIVE, LIVE, home, rows=4)
    victim_bytes = victim.read_bytes()

    departing = home / "members" / GONE
    if departing.exists():
        for child in departing.iterdir():
            child.unlink()
        departing.rmdir()
    departing.symlink_to(home / "members" / LIVE, target_is_directory=True)

    captured = _captured()
    _write_roster(home, **{LIVE: {}})
    _report_as_junction(monkeypatch, departing)
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert victim.exists()
    assert victim.read_bytes() == victim_bytes


def test_neither_legacy_link_test_asks_only_about_symlinks():
    """Structural, because no junction exists on the platform this runs on.

    A host that cannot grow the thing under test cannot show the predicate was
    consulted, so the spelling is read off the source instead.
    """
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(service_mod._remove_legacy_activity))
    tree = ast.parse(source)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "is_symlink" not in called, "a link test here must answer for a junction too"
    names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "is_link" in names


# --- a member whose history is ONLY in the legacy source ----------------------
# The store answers ABSENT when there is no unit to remove, and the guard it
# would have called never runs, so the roster is asked again here.


def test_a_member_with_only_legacy_activity_still_loses_it(tmp_path):
    """No unit was ever written, so that source IS the departed member's history."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    legacy = _legacy_activity(GONE, GONE, home, rows=3)

    captured = _captured()
    _write_roster(home)
    agents_mod._reclaim_deleted_member_crew_log(GONE, captured)

    assert not _unit(GONE).exists()
    assert not legacy.exists()


def test_a_live_member_with_no_unit_keeps_its_legacy_activity(tmp_path):
    """The safety edge of the absent branch: the roster still claims this slug."""
    home = tmp_path / "home"
    _write_roster(home, **{LIVE: {}})
    legacy = _legacy_activity(LIVE, LIVE, home, rows=3)

    status = service_mod.get_service().remove_unit(LIVE, still_unclaimed=lambda: False)

    assert status == REMOVE_ABSENT
    assert legacy.exists()


def test_an_unreadable_roster_keeps_a_unitless_members_legacy_activity(tmp_path):
    """The predicate raising is not an answer, so the source stays."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    legacy = _legacy_activity(GONE, GONE, home, rows=3)

    def explode() -> bool:
        raise OSError("roster unreadable")

    status = service_mod.get_service().remove_unit(GONE, still_unclaimed=explode)

    assert status == REMOVE_ABSENT
    assert legacy.exists()
