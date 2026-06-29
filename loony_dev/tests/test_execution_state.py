"""Tests for the execution-state substrate writer (issue #267).

Covers the storage seam, the projection-grade event schema, atomic snapshot
writes, the actor-from-config resolution (no ``Repo`` needed), the single
base_dir source for the agent turn-boundary heartbeat, and the closed-vocab
validation — plus a light call-site smoke for the orchestrator event sequence.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from loony_dev import execution_state as es
from loony_dev import pipeline_log, session_registry


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.repo = "acme/widgets"

    def _event(self, **kw) -> es.ExecutionEvent:
        defaults = dict(type="phase_enter", what="x", actor="trixy", target={"repo": self.repo})
        defaults.update(kw)
        return es.ExecutionEvent(**defaults)

    def _state(self, **kw) -> es.LiveState:
        defaults = dict(
            pipeline_key="issue-7", repo=self.repo, stage="Implementing",
            current_skill="implement-issue", state="running", live=True,
        )
        defaults.update(kw)
        return es.LiveState(**defaults)


class PathLocatorTestCase(_Base):
    def test_events_and_snapshot_live_beside_pipeline_log(self) -> None:
        ev = es.events_path(self.base, self.repo, "issue-7")
        snap = es.snapshot_path(self.base, self.repo, "issue-7")
        # Same pipelines dir + slug as the #220 log, just different suffix.
        logs_dir = pipeline_log.pipeline_logs_dir(self.base, "acme", "widgets")
        slug = session_registry.task_slug("issue-7")
        self.assertEqual(ev, logs_dir / f"{slug}.events.jsonl")
        self.assertEqual(snap, logs_dir / f"{slug}.state.json")

    def test_locators_are_forward_deterministic(self) -> None:
        self.assertEqual(
            es.events_path(self.base, self.repo, "issue-7"),
            es.events_path(self.base, self.repo, "issue-7"),
        )


class EventRoundTripTestCase(_Base):
    def test_append_then_tail_is_ordered_typed_and_parseable(self) -> None:
        for i in range(5):
            es.append_event(self.base, self.repo, "issue-7", self._event(what=f"step-{i}"))
        tail = es.tail_events(self.base, self.repo, "issue-7", 5)
        self.assertEqual([e.what for e in tail], [f"step-{i}" for i in range(5)])
        # Each line is independently json.loads-able.
        raw = es.events_path(self.base, self.repo, "issue-7").read_text().splitlines()
        self.assertEqual(len(raw), 5)
        for line in raw:
            obj = json.loads(line)
            self.assertIn("ts", obj)
            self.assertIn(obj["type"], es.EVENT_TYPES)

    def test_tail_returns_last_n_only(self) -> None:
        for i in range(10):
            es.append_event(self.base, self.repo, "issue-7", self._event(what=f"s{i}"))
        tail = es.tail_events(self.base, self.repo, "issue-7", 3)
        self.assertEqual([e.what for e in tail], ["s7", "s8", "s9"])

    def test_tail_missing_log_is_empty(self) -> None:
        self.assertEqual(es.tail_events(self.base, self.repo, "issue-404", 5), [])

    def test_append_writes_key_sidecar(self) -> None:
        es.append_event(self.base, self.repo, "issue-7", self._event())
        sidecar = pipeline_log.pipeline_key_sidecar_path(self.base, "acme", "widgets", "issue-7")
        self.assertTrue(sidecar.is_file())
        self.assertEqual(sidecar.read_text(), "issue-7")

    def test_malformed_lines_are_skipped_not_raised(self) -> None:
        es.append_event(self.base, self.repo, "issue-7", self._event(what="good"))
        path = es.events_path(self.base, self.repo, "issue-7")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
            fh.write("\n")
        tail = es.tail_events(self.base, self.repo, "issue-7", 10)
        self.assertEqual([e.what for e in tail], ["good"])


class CrossPipelineMergeTestCase(_Base):
    def test_events_merge_sort_by_ts_across_pipelines(self) -> None:
        # Interleave appends to two pipelines; ts is monotonic ISO-8601 UTC, so a
        # plain sort of the union reconstructs a single ordered activity stream.
        order = []
        for i in range(4):
            key = "issue-1" if i % 2 == 0 else "issue-2"
            ev = self._event(type="turn_complete", what=f"{key}:{i}")
            es.append_event(self.base, self.repo, key, ev)
            order.append(ev.what)
            time.sleep(0.001)
        a = es.tail_events(self.base, self.repo, "issue-1", 10)
        b = es.tail_events(self.base, self.repo, "issue-2", 10)
        merged = sorted([*a, *b], key=lambda e: e.ts)
        self.assertEqual([e.what for e in merged], order)


class SnapshotAtomicityTestCase(_Base):
    def test_snapshot_round_trips(self) -> None:
        es.write_snapshot(self.base, self.repo, "issue-7", self._state(attempt=2, linked_pr=99))
        got = es.read_snapshot(self.base, self.repo, "issue-7")
        assert got is not None
        self.assertEqual(got.pipeline_key, "issue-7")
        self.assertEqual(got.current_skill, "implement-issue")
        self.assertEqual(got.attempt, 2)
        self.assertEqual(got.linked_pr, 99)
        self.assertEqual(got.state, "running")

    def test_read_missing_snapshot_is_none(self) -> None:
        self.assertIsNone(es.read_snapshot(self.base, self.repo, "issue-404"))

    def test_concurrent_reader_never_sees_torn_file(self) -> None:
        # A reader hammering the snapshot while a writer rewrites it many times
        # must only ever observe complete, valid JSON — never a partial file.
        es.write_snapshot(self.base, self.repo, "issue-7", self._state())
        path = es.snapshot_path(self.base, self.repo, "issue-7")
        stop = threading.Event()
        errors: list[Exception] = []
        # A barrier across all 4 threads guarantees the readers are live and the
        # writer loop has not started until everyone is at the line — so the test
        # actually exercises the torn-read window rather than (potentially) racing
        # the writer to completion before any reader runs. ``read_count`` then
        # proves at least one read genuinely overlapped the writes.
        started = threading.Barrier(4)
        read_count = 0
        read_count_lock = threading.Lock()

        def writer() -> None:
            started.wait()
            for i in range(200):
                try:
                    es.write_snapshot(
                        self.base, self.repo, "issue-7", self._state(attempt=i + 1),
                    )
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)
            stop.set()

        def reader() -> None:
            nonlocal read_count
            started.wait()
            while not stop.is_set():
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                if not text:
                    continue
                try:
                    json.loads(text)
                    with read_count_lock:
                        read_count += 1
                except ValueError as exc:  # a torn file
                    errors.append(exc)

        threads = [threading.Thread(target=writer), *[threading.Thread(target=reader) for _ in range(3)]]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertGreater(read_count, 0)
        # Temp file is gone after the final replace.
        self.assertFalse(path.with_name(path.name + ".tmp").exists())


class BumpSnapshotTestCase(_Base):
    def test_bump_preserves_other_fields_and_advances_heartbeat(self) -> None:
        es.write_snapshot(self.base, self.repo, "issue-7", self._state(attempt=3, linked_pr=42))
        before = es.read_snapshot(self.base, self.repo, "issue-7")
        assert before is not None
        time.sleep(0.002)
        es._bump_snapshot(self.base, self.repo, "issue-7")
        after = es.read_snapshot(self.base, self.repo, "issue-7")
        assert after is not None
        # Heartbeat advanced…
        self.assertGreater(after.last_heartbeat, before.last_heartbeat)
        # …everything else preserved.
        self.assertEqual(after.attempt, 3)
        self.assertEqual(after.linked_pr, 42)
        self.assertEqual(after.current_skill, "implement-issue")
        self.assertEqual(after.stage, "Implementing")

    def test_bump_can_flip_terminal_state(self) -> None:
        es.write_snapshot(self.base, self.repo, "issue-7", self._state())
        es._bump_snapshot(self.base, self.repo, "issue-7", state="idle", live=False)
        after = es.read_snapshot(self.base, self.repo, "issue-7")
        assert after is not None
        self.assertEqual(after.state, "idle")
        self.assertFalse(after.live)

    def test_bump_missing_snapshot_is_noop(self) -> None:
        es._bump_snapshot(self.base, self.repo, "issue-404")  # must not raise
        self.assertIsNone(es.read_snapshot(self.base, self.repo, "issue-404"))


class ListActiveTestCase(_Base):
    def test_only_running_or_live_snapshots_returned(self) -> None:
        es.write_snapshot(self.base, self.repo, "issue-1", self._state(pipeline_key="issue-1"))
        es.write_snapshot(
            self.base, self.repo, "issue-2",
            self._state(pipeline_key="issue-2", state="idle", live=False),
        )
        es.write_snapshot(
            self.base, "acme/other", "issue-3",
            self._state(pipeline_key="issue-3", repo="acme/other"),
        )
        active = es.list_active(self.base)
        keys = sorted(s.pipeline_key for s in active)
        self.assertEqual(keys, ["issue-1", "issue-3"])

    def test_empty_base_dir_is_empty_list(self) -> None:
        self.assertEqual(es.list_active(self.base), [])


class DefensiveParsingTestCase(_Base):
    def test_event_target_must_be_dict(self) -> None:
        with self.assertRaises(ValueError):
            es.ExecutionEvent(type="error", what="x", actor="a", target=["not", "a", "dict"])

    def test_event_from_dict_coerces_bad_target_to_empty(self) -> None:
        ev = es.ExecutionEvent.from_dict(
            {"type": "error", "what": "x", "actor": "a", "target": "oops", "ts": "t"}
        )
        self.assertEqual(ev.target, {})

    def test_snapshot_live_string_false_is_not_active(self) -> None:
        # A torn ``live`` written as the string "false" must read as idle.
        es.write_snapshot(self.base, self.repo, "issue-7", self._state(state="idle", live=False))
        path = es.snapshot_path(self.base, self.repo, "issue-7")
        data = json.loads(path.read_text())
        data["live"] = "false"  # non-bool — bool("false") would be True
        path.write_text(json.dumps(data))
        snap = es.read_snapshot(self.base, self.repo, "issue-7")
        assert snap is not None
        self.assertFalse(snap.live)
        self.assertEqual(es.list_active(self.base), [])

    def test_write_snapshot_rejects_identity_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            es.write_snapshot(
                self.base, self.repo, "issue-7", self._state(pipeline_key="issue-99"),
            )
        with self.assertRaises(ValueError):
            es.write_snapshot(
                self.base, "acme/other", "issue-7", self._state(),  # repo mismatch
            )

    def test_tail_collects_n_valid_despite_trailing_garbage(self) -> None:
        for i in range(3):
            es.append_event(self.base, self.repo, "issue-7", self._event(what=f"v{i}"))
        path = es.events_path(self.base, self.repo, "issue-7")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("{garbage\n{also garbage\n")
        # Asking for 2 still returns the 2 most-recent *valid* events.
        tail = es.tail_events(self.base, self.repo, "issue-7", 2)
        self.assertEqual([e.what for e in tail], ["v1", "v2"])


class ClosedVocabTestCase(_Base):
    def test_invalid_event_type_rejected(self) -> None:
        with self.assertRaises(ValueError):
            es.ExecutionEvent(type="bogus", what="x", actor="a", target={})

    def test_invalid_state_tone_rejected(self) -> None:
        with self.assertRaises(ValueError):
            es.ExecutionEvent(type="error", what="x", actor="a", target={}, state_tone="loud")

    def test_invalid_stage_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._state(stage="Frobnicating")

    def test_invalid_state_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._state(state="zombie")


class CurrentSkillMapTestCase(_Base):
    def test_implement_phase_skill_is_never_none(self) -> None:
        # Regression guard (blocker #1): the headline implement phase must carry a
        # skill even though IssueTask leaves command_name = None.
        self.assertEqual(es.skill_for_task_type("implement_issue"), "implement-issue")
        self.assertEqual(es.stage_for_task_type("implement_issue"), "Implementing")
        state = self._state(current_skill=es.skill_for_task_type("implement_issue"))
        self.assertIsNotNone(state.current_skill)

    def test_all_instrumented_task_types_map_to_a_skill(self) -> None:
        for task_type in (
            "plan_issue", "implement_issue", "address_review",
            "resolve_conflicts", "fix_ci",
        ):
            self.assertIsNotNone(es.skill_for_task_type(task_type), task_type)

    def test_skill_and_stage_maps_cover_the_same_task_types(self) -> None:
        # The two maps must stay in sync: a skill with no matching stage reads as
        # schema drift on the orchestrator path and drops the running snapshot.
        self.assertEqual(
            set(es.SKILL_BY_TASK_TYPE), set(es.STAGE_BY_TASK_TYPE),
        )

    def test_cleanup_stuck_is_uninstrumented(self) -> None:
        # ``cleanup_stuck`` has no worktree/pipeline; it is intentionally in
        # neither map so it is never (half-)instrumented.
        self.assertIsNone(es.skill_for_task_type("cleanup_stuck"))
        self.assertIsNone(es.stage_for_task_type("cleanup_stuck"))


class NeedsYouTestCase(_Base):
    def test_derived_in_review_and_in_error_and_blocked(self) -> None:
        self.assertTrue(es.derive_needs_you("running", "In Review"))
        self.assertTrue(es.derive_needs_you("failed", "Implementing"))
        self.assertTrue(es.derive_needs_you("crashed", "Implementing"))
        self.assertTrue(es.derive_needs_you("running", "Conflicts"))
        self.assertFalse(es.derive_needs_you("running", "Implementing"))

    def test_needs_you_is_recomputed_not_trusted(self) -> None:
        # A hand-passed needs_you is overwritten by the derivation.
        state = self._state(state="running", stage="Implementing", needs_you=True)
        self.assertFalse(state.needs_you)


class ActorResolutionTestCase(_Base):
    def test_bot_from_worker_section_without_repo_instance(self) -> None:
        # Blocker #2: actor resolution must work with NO Repo instance, and read
        # the nested ``[worker]`` section — the shape a worker config file uses.
        from loony_dev import config

        original = config.settings
        config.settings = config.Settings({"worker": {"bot_name": "trixy-07"}})
        try:
            self.assertEqual(es.resolve_actor(es.ACTOR_BOT), "trixy-07")
        finally:
            config.settings = original

    def test_bot_from_flat_cli_override(self) -> None:
        # ``--bot-name`` is a registered CLI option, so it lands as a flattened
        # top-level key (no ``[worker]`` section). The nested+flat lookup must
        # still honour it — matching how ``Repo`` resolves ``bot_name``.
        from loony_dev import config

        original = config.settings
        config.settings = config.Settings({"bot_name": "trixy-cli"})
        try:
            self.assertEqual(es.resolve_actor(es.ACTOR_BOT), "trixy-cli")
        finally:
            config.settings = original

    def test_capo_from_worker_section(self) -> None:
        from loony_dev import config

        original = config.settings
        config.settings = config.Settings({"worker": {"capo_name": "el-capo"}})
        try:
            self.assertEqual(es.resolve_actor(es.ACTOR_CAPO), "el-capo")
        finally:
            config.settings = original

    def test_capo_default_is_not_a_literal_in_callsites(self) -> None:
        from loony_dev import config

        original = config.settings
        config.settings = config.Settings({})
        try:
            self.assertEqual(es.resolve_actor(es.ACTOR_CAPO), "capo")
            self.assertEqual(es.resolve_actor(es.ACTOR_HUMAN), "human")
            self.assertEqual(es.resolve_actor(es.ACTOR_SYSTEM), "system")
            # An unknown kind is a programmer error and raises, never a silent default.
            with self.assertRaises(ValueError):
                es.resolve_actor("anything-unknown")
        finally:
            config.settings = original


class TargetForTestCase(_Base):
    def test_issue_and_pr_targets(self) -> None:
        self.assertEqual(es.target_for(self.repo, "issue-7"), {"repo": self.repo, "issue": 7})
        self.assertEqual(es.target_for(self.repo, "pr-12"), {"repo": self.repo, "pr": 12})
        self.assertEqual(es.target_for(self.repo, "weird"), {"repo": self.repo})


class StreamEventsTestCase(_Base):
    def test_stream_yields_existing_then_follows_new(self) -> None:
        es.append_event(self.base, self.repo, "issue-7", self._event(what="a"))
        es.append_event(self.base, self.repo, "issue-7", self._event(what="b"))
        gen = es.stream_events(self.base, self.repo, "issue-7", poll_interval=0.01)

        def next_event(timeout: float = 2.0) -> es.ExecutionEvent:
            # Bound each pull so a regression in the follow-generator surfaces as a
            # quick failure instead of hanging the whole suite on a blocked next().
            result: dict[str, object] = {}

            def consume() -> None:
                try:
                    result["event"] = next(gen)
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    result["error"] = exc

            t = threading.Thread(target=consume, daemon=True)
            t.start()
            t.join(timeout)
            self.assertFalse(t.is_alive(), "stream_events() did not yield in time")
            if "error" in result:
                raise result["error"]  # type: ignore[misc]
            return result["event"]  # type: ignore[return-value]

        got = [next_event().what, next_event().what]
        self.assertEqual(got, ["a", "b"])
        # A later append is followed.
        es.append_event(self.base, self.repo, "issue-7", self._event(what="c"))
        self.assertEqual(next_event().what, "c")
        gen.close()


class BestEffortTestCase(_Base):
    def test_unwritable_dir_failure_is_swallowed_by_callsite_wrapper(self) -> None:
        # The module's append_event itself raises on a hard FS error; the
        # *call-sites* swallow it. Emulate the call-site wrapper contract: a write
        # under a path made unwritable does not crash the caller.
        bad = self.base / "nope"
        bad.write_text("not a dir")  # base/.logs path will collide with a file
        try:
            es.append_event(bad, self.repo, "issue-7", self._event())
        except OSError:
            pass  # expected; call-sites wrap this in try/except (see orchestrator)
        # The point: a torn write never corrupts an existing good pipeline.
        es.append_event(self.base, self.repo, "issue-7", self._event(what="ok"))
        self.assertEqual(
            [e.what for e in es.tail_events(self.base, self.repo, "issue-7", 1)], ["ok"],
        )


class AgentHeartbeatTestCase(_Base):
    """The turn-boundary heartbeat uses the threaded base_dir, not config (fix #3)."""

    def _agent(self):
        from loony_dev.agents.coding import CodingAgent

        agent = CodingAgent(repo=self.repo)
        agent.base_dir = self.base
        return agent

    def test_turn_boundary_emits_events_and_bumps_heartbeat(self) -> None:
        from loony_dev import config, pipeline_log

        agent = self._agent()
        # Seed a running snapshot so the heartbeat has something to bump.
        es.write_snapshot(self.base, self.repo, "issue-7", self._state())
        before = es.read_snapshot(self.base, self.repo, "issue-7")
        assert before is not None

        # Stub the raw runner so no real ``claude -p`` is spawned.
        agent._run_claude_cli_inner = lambda *a, **k: ("ok", "", 0)  # type: ignore[method-assign]

        original = config.settings
        config.settings = config.Settings({})  # base_dir unset → property would raise
        time.sleep(0.002)
        try:
            with pipeline_log.pipeline_log_context("issue-7"):
                out = agent._run_claude_cli("hi", cwd=self.base, session_id="s")
        finally:
            config.settings = original
        self.assertEqual(out, ("ok", "", 0))

        types = [e.type for e in es.tail_events(self.base, self.repo, "issue-7", 10)]
        self.assertEqual(types, ["turn_start", "turn_complete"])
        after = es.read_snapshot(self.base, self.repo, "issue-7")
        assert after is not None
        self.assertGreater(after.last_heartbeat, before.last_heartbeat)

    def test_failed_turn_emits_error_event(self) -> None:
        from loony_dev import pipeline_log

        agent = self._agent()
        agent._run_claude_cli_inner = lambda *a, **k: ("", "boom", 1)  # type: ignore[method-assign]
        with pipeline_log.pipeline_log_context("issue-9"):
            agent._run_claude_cli("hi", cwd=self.base, session_id="s")
        types = [e.type for e in es.tail_events(self.base, self.repo, "issue-9", 10)]
        self.assertEqual(types, ["turn_start", "error"])

    def test_no_active_pipeline_is_noop(self) -> None:
        agent = self._agent()
        agent._run_claude_cli_inner = lambda *a, **k: ("ok", "", 0)  # type: ignore[method-assign]
        # No pipeline_log_context bound → contextvar is None → no writes.
        agent._run_claude_cli("hi", cwd=self.base, session_id="s")
        self.assertEqual(es.list_active(self.base), [])


class OrchestratorCallSiteTestCase(_Base):
    """Light smoke: a dispatched task lands the expected event + snapshot sequence."""

    def test_dispatch_records_event_sequence_and_terminal_snapshot(self) -> None:
        from unittest.mock import MagicMock, patch

        from loony_dev.models import TaskResult
        from loony_dev.orchestrator import Orchestrator

        repo = MagicMock()
        repo.name = self.repo
        repo.owner = "acme"
        git = MagicMock()
        git.work_dir = self.base
        git.default_branch = "main"
        git.list_worktrees.return_value = []

        agent = MagicMock()
        agent.name = "coding"
        agent.execute.return_value = TaskResult(success=True, output="", summary="done")

        task = MagicMock()
        task.worktree_key = "issue-7"
        task.task_type = "implement_issue"
        task.target_branch = None
        task.describe.return_value = "do work"

        with patch.object(Orchestrator, "_prune_stale_worktrees"):
            orch = Orchestrator(repo=repo, git=git, agents=[agent], interval=60, base_dir=self.base)
        # Run against the base checkout (no PipelineSession worktree to materialize):
        # force ps=None so the smoke stays filesystem-only.
        with patch.object(orch, "_pipeline_session_for", return_value=None):
            orch._execute_task(agent, task)

        types = [e.type for e in es.tail_events(self.base, self.repo, "issue-7", 20)]
        self.assertEqual(types, ["dispatched", "phase_enter", "terminal"])
        snap = es.read_snapshot(self.base, self.repo, "issue-7")
        assert snap is not None
        self.assertEqual(snap.state, "idle")
        self.assertFalse(snap.live)
        self.assertEqual(snap.current_skill, "implement-issue")
        self.assertEqual(snap.stage, "Implementing")


if __name__ == "__main__":
    unittest.main()
