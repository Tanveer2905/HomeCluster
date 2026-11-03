"""Scheduler behaviour (SPEC §9), driven one phase at a time."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest

from coordinator.db import new_id, now
from coordinator.scheduler import resolve_refs, walk_path

MAPPER = "def fn(item, ctx):\n    return item\n"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def add_device(
    state: Any,
    name: str = "phone",
    caps: Optional[List[str]] = None,
    slots: int = 1,
    bench_score: Optional[float] = None,
    battery: Optional[int] = None,
    charging: Optional[bool] = None,
    temp: Optional[float] = None,
    platform: str = "termux",
    status: str = "online",
    paused: int = 0,
    settings: Optional[Dict[str, Any]] = None,
    created: Optional[float] = None,
    cores: int = 8,
) -> Dict[str, Any]:
    device_id = new_id()
    state.db.insert(
        "devices",
        {
            "id": device_id,
            "name": name,
            "platform": platform,
            "token_hash": new_id() * 5,
            "caps": caps if caps is not None else ["python", "shell", "termux", "storage", "service"],
            "info": {"cpu_count": cores},
            "status": status,
            "paused": paused,
            "slots": slots,
            "battery": battery,
            "charging": None if charging is None else int(charging),
            "temp": temp,
            "storage_free_mb": 8192,
            "bench_score": bench_score,
            "settings": settings or {},
            "last_seen": now(),
            "created": created if created is not None else now(),
            "removed": 0,
        },
    )
    return state.db.get("devices", device_id)


def submit_map(
    state: Any,
    items: List[Any],
    chunk_size: int = 1,
    priority: int = 0,
    created: Optional[float] = None,
    **options: Any,
) -> Dict[str, Any]:
    job = state.submit_job(
        {
            "type": "python_map",
            "name": "map",
            "priority": priority,
            "params": {"code": MAPPER, "items": items, "chunk_size": chunk_size},
            "options": options,
        }
    )
    if created is not None:
        state.db.update("jobs", {"id": job["id"]}, {"created": created})
    return state.db.get("jobs", job["id"])


def drain(state: Any, device_id: str) -> List[Dict[str, Any]]:
    queue = state.queues.get(device_id)
    out: List[Dict[str, Any]] = []
    while queue is not None and not queue.empty():
        out.append(queue.get_nowait())
    return out


def tasks_of(state: Any, job_id: str) -> List[Dict[str, Any]]:
    return state.db.select("tasks", "WHERE job_id=? ORDER BY idx", (job_id,))


# --------------------------------------------------------------------------
# split
# --------------------------------------------------------------------------

def test_split_creates_tasks_and_starts_the_job(state: Any) -> None:
    job = submit_map(state, [1, 2, 3], chunk_size=1)
    assert job["status"] == "pending"
    state.scheduler.split_pending()
    job = state.db.get("jobs", job["id"])
    assert job["status"] == "running"
    assert job["total_tasks"] == 3
    tasks = tasks_of(state, job["id"])
    assert [t["idx"] for t in tasks] == [0, 1, 2]
    assert all(t["status"] == "queued" for t in tasks)
    assert all(t["max_attempts"] == 3 for t in tasks)


def test_split_failure_fails_the_job(state: Any) -> None:
    job = state.submit_job({"type": "python_map", "params": {"code": "", "items": [1]}})
    state.scheduler.split_pending()
    job = state.db.get("jobs", job["id"])
    assert job["status"] == "failed"
    assert "split failed" in job["error"]


def test_unknown_job_type_is_rejected(state: Any) -> None:
    with pytest.raises(KeyError):
        state.submit_job({"type": "does_not_exist", "params": {}})


def test_empty_split_completes_the_job(state: Any) -> None:
    job = submit_map(state, [])
    state.scheduler.split_pending()
    state.scheduler.finish_jobs()
    job = state.db.get("jobs", job["id"])
    assert job["status"] == "done"
    assert job["result"] == []


# --------------------------------------------------------------------------
# ordering (SPEC §9.3)
# --------------------------------------------------------------------------

def test_higher_priority_jobs_go_first(state: Any) -> None:
    device = add_device(state, slots=1)
    old = submit_map(state, ["low"], created=now() - 100)
    urgent = submit_map(state, ["high"], priority=5, created=now())
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 1
    assert tasks_of(state, urgent["id"])[0]["status"] == "assigned"
    assert tasks_of(state, old["id"])[0]["status"] == "queued"
    assert drain(state, device["id"])[0]["task"]["job_id"] == urgent["id"]


def test_equal_priority_falls_back_to_job_age(state: Any) -> None:
    add_device(state, slots=1)
    older = submit_map(state, ["a"], created=now() - 500)
    newer = submit_map(state, ["b"], created=now() - 10)
    state.scheduler.split_pending()
    state.scheduler.assign()
    assert tasks_of(state, older["id"])[0]["status"] == "assigned"
    assert tasks_of(state, newer["id"])[0]["status"] == "queued"


def test_within_a_job_tasks_go_in_idx_order(state: Any) -> None:
    device = add_device(state, slots=3)
    job = submit_map(state, [1, 2, 3], chunk_size=1)
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 3
    commands = drain(state, device["id"])
    assert [c["task"]["idx"] for c in commands] == [0, 1, 2]
    assert [c["task"]["attempt"] for c in commands] == [1, 1, 1]


def test_free_slots_limit_assignment(state: Any) -> None:
    add_device(state, slots=2)
    job = submit_map(state, [1, 2, 3, 4], chunk_size=1)
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 2
    assert state.scheduler.assign() == 0  # no free slots left
    statuses = [t["status"] for t in tasks_of(state, job["id"])]
    assert statuses == ["assigned", "assigned", "queued", "queued"]


def test_devices_are_visited_fastest_first(state: Any) -> None:
    slow = add_device(state, name="slow", bench_score=1.0, slots=1)
    fast = add_device(state, name="fast", bench_score=99.0, slots=1)
    unbenched = add_device(state, name="new", bench_score=None, slots=1)
    job = submit_map(state, ["only-one"], chunk_size=1)
    state.scheduler.split_pending()
    state.scheduler.assign()
    assert tasks_of(state, job["id"])[0]["device_id"] == fast["id"]
    assert not drain(state, slow["id"])
    assert not drain(state, unbenched["id"])


def test_unbenchmarked_devices_come_last(state: Any) -> None:
    unbenched = add_device(state, name="new", bench_score=None, slots=1)
    slow = add_device(state, name="slow", bench_score=0.5, slots=1)
    job = submit_map(state, ["x"], chunk_size=1)
    state.scheduler.split_pending()
    state.scheduler.assign()
    assert tasks_of(state, job["id"])[0]["device_id"] == slow["id"]
    assert unbenched["id"] != slow["id"]


# --------------------------------------------------------------------------
# eligibility
# --------------------------------------------------------------------------

def test_devices_without_the_impl_kind_are_skipped(state: Any) -> None:
    add_device(state, name="browser", caps=["js", "browser"], platform="browser")
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 0
    assert tasks_of(state, job["id"])[0]["status"] == "queued"


def test_requires_must_be_a_subset_of_caps(state: Any) -> None:
    plain = add_device(state, name="plain", caps=["python", "shell"])
    job = submit_map(state, [1], requires=["ffmpeg"])
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 0
    state.db.update("devices", {"id": plain["id"]}, {"caps": ["python", "shell", "ffmpeg"]})
    assert state.scheduler.assign() == 1


def test_targets_list_pins_the_job(state: Any) -> None:
    first = add_device(state, name="a")
    second = add_device(state, name="b")
    job = submit_map(state, [1], targets=[second["id"]])
    state.scheduler.split_pending()
    state.scheduler.assign()
    assert tasks_of(state, job["id"])[0]["device_id"] == second["id"]
    assert not drain(state, first["id"])


def test_targets_all_makes_one_task_per_device(state: Any) -> None:
    first = add_device(state, name="a")
    second = add_device(state, name="b")
    job = state.submit_job(
        {"type": "shell", "params": {"cmd": "echo hi"}, "options": {"targets": "all"}}
    )
    state.scheduler.split_pending()
    tasks = tasks_of(state, job["id"])
    assert len(tasks) == 2
    assert {t["payload"]["device_id"] for t in tasks} == {first["id"], second["id"]}
    state.scheduler.assign()
    assert {t["device_id"] for t in tasks_of(state, job["id"])} == {first["id"], second["id"]}


def test_targets_all_waits_for_a_device(state: Any) -> None:
    job = state.submit_job(
        {"type": "shell", "params": {"cmd": "echo hi"}, "options": {"targets": "all"}}
    )
    state.scheduler.split_pending()
    assert state.db.get("jobs", job["id"])["status"] == "pending"
    add_device(state, name="late")
    state.scheduler.split_pending()
    assert state.db.get("jobs", job["id"])["status"] == "running"


def test_paused_devices_get_nothing(state: Any) -> None:
    add_device(state, paused=1)
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 0


def test_offline_devices_get_nothing(state: Any) -> None:
    add_device(state, status="offline")
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 0


# --------------------------------------------------------------------------
# health gating (SPEC §9.3)
# --------------------------------------------------------------------------

def test_low_battery_blocks_assignment(state: Any) -> None:
    device = add_device(state, battery=10, charging=False)
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 0
    state.db.update("devices", {"id": device["id"]}, {"charging": 1})
    assert state.scheduler.assign() == 1


def test_battery_at_the_threshold_is_healthy(state: Any) -> None:
    device = add_device(state, battery=20, charging=False)
    assert state.scheduler.healthy(state.db.get("devices", device["id"])) is True
    state.db.update("devices", {"id": device["id"]}, {"battery": 19})
    assert state.scheduler.healthy(state.db.get("devices", device["id"])) is False


def test_unknown_battery_is_healthy(state: Any) -> None:
    device = add_device(state, battery=None, charging=None)
    assert state.scheduler.healthy(state.db.get("devices", device["id"])) is True


def test_thermal_hysteresis(state: Any) -> None:
    scheduler = state.scheduler
    device = add_device(state, temp=30.0)

    def health(temp: float) -> bool:
        state.db.update("devices", {"id": device["id"]}, {"temp": temp})
        return scheduler.healthy(state.db.get("devices", device["id"]))

    # Balanced: no new work from 40 C, back to normal at 36 C
    assert health(30.0) is True
    assert health(39.9) is True
    assert health(40.0) is False         # at the limit -> resting
    assert device["id"] in scheduler.hot
    assert health(38.0) is False         # below the limit but above the resume point
    assert health(36.5) is False
    assert health(36.0) is True          # <= resume -> cooled down
    assert device["id"] not in scheduler.hot


def test_per_device_threshold_overrides_can_only_be_stricter(state: Any) -> None:
    # a laxer battery floor than Balanced (20 %) is ignored
    device = add_device(state, battery=10, charging=False, settings={"min_battery": 5})
    assert state.scheduler.healthy(state.db.get("devices", device["id"])) is False
    state.db.update("devices", {"id": device["id"]}, {"battery": 30, "settings": {"min_battery": 50}})
    assert state.scheduler.healthy(state.db.get("devices", device["id"])) is False
    state.db.update("devices", {"id": device["id"]}, {"settings": {"min_battery": 25}})
    assert state.scheduler.healthy(state.db.get("devices", device["id"])) is True
    # a lower temperature limit works, a higher one does not
    state.db.update("devices", {"id": device["id"]}, {"temp": 38.0, "settings": {"max_temp": 37.0}})
    assert state.scheduler.healthy(state.db.get("devices", device["id"])) is False
    state.scheduler.hot.clear()
    state.db.update("devices", {"id": device["id"]}, {"temp": 41.0, "settings": {"max_temp": 50.0}})
    assert state.scheduler.healthy(state.db.get("devices", device["id"])) is False


def test_settings_change_the_thresholds(state: Any) -> None:
    device = add_device(state, battery=30, charging=False)
    assert state.scheduler.healthy(state.db.get("devices", device["id"])) is True
    state.save_settings({"scheduler": {"min_battery": 50}})
    assert state.scheduler.healthy(state.db.get("devices", device["id"])) is False


# --------------------------------------------------------------------------
# fault tolerance (SPEC §9.4)
# --------------------------------------------------------------------------

def test_offline_device_requeues_its_tasks(state: Any) -> None:
    device = add_device(state)
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    state.scheduler.assign()
    assert tasks_of(state, job["id"])[0]["status"] == "assigned"

    state.db.update(
        "devices", {"id": device["id"]}, {"last_seen": now() - 999}
    )
    assert state.scheduler.mark_offline_devices() == [device["id"]]
    task = tasks_of(state, job["id"])[0]
    assert task["status"] == "queued"
    assert task["device_id"] is None
    assert task["attempts"] == 1
    assert task["history"][0]["error"] == "device went offline"
    assert state.db.get("devices", device["id"])["status"] == "offline"


def test_lease_expiry_requeues_and_cancels(state: Any) -> None:
    device = add_device(state)
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    state.scheduler.assign()
    drain(state, device["id"])
    task = tasks_of(state, job["id"])[0]
    state.db.update("tasks", {"id": task["id"]}, {"lease_expires": now() - 1})

    assert state.scheduler.expire_leases() == [task["id"]]
    assert tasks_of(state, job["id"])[0]["status"] == "queued"
    assert drain(state, device["id"])[0] == {"cmd": "cancel", "task_id": task["id"]}


def test_failure_requeues_until_max_attempts(state: Any) -> None:
    device = add_device(state)
    job = submit_map(state, [1], max_attempts=2)
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]

    status, _body = state.scheduler.task_done(
        task["id"], device, {"ok": False, "error": "boom"}
    )
    assert status == 200
    assert tasks_of(state, job["id"])[0]["status"] == "queued"

    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    assert task["attempts"] == 2
    state.scheduler.task_done(task["id"], device, {"ok": False, "error": "boom again"})
    task = tasks_of(state, job["id"])[0]
    assert task["status"] == "failed"
    assert task["error"] == "boom again"
    assert len(task["history"]) == 2

    state.scheduler.finish_jobs()
    finished = state.db.get("jobs", job["id"])
    assert finished["status"] == "failed"
    assert finished["failed_tasks"] == 1
    assert "boom again" in finished["error"]


def test_stale_attempt_failure_is_ignored_and_success_accepted(state: Any) -> None:
    """Same device got the task back after a requeue (attempt 2); a report
    tagged attempt 1 is *late*: a failure is ignored, a success completes
    the task and cancels the redo."""
    device = add_device(state)
    job = submit_map(state, [1], max_attempts=3)
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    drain(state, device["id"])

    state.scheduler.requeue(task, "device went offline")
    state.scheduler.assign()
    assert tasks_of(state, job["id"])[0]["attempts"] == 2
    drain(state, device["id"])

    status, body = state.scheduler.task_done(
        task["id"], device, {"ok": False, "error": "old failure", "attempt": 1}
    )
    assert status == 200 and body.get("ignored") == "late failure"
    assert tasks_of(state, job["id"])[0]["status"] == "assigned"

    status, body = state.scheduler.task_done(
        task["id"], device, {"ok": True, "result": 1, "attempt": 1}
    )
    assert status == 200 and body.get("late") is True
    assert tasks_of(state, job["id"])[0]["status"] == "done"
    assert drain(state, device["id"]) == [{"cmd": "cancel", "task_id": task["id"]}]

    # the redo's own report is now a duplicate
    status, body = state.scheduler.task_done(
        task["id"], device, {"ok": True, "result": 2, "attempt": 2}
    )
    assert status == 409


def test_future_attempt_number_is_rejected(state: Any) -> None:
    device = add_device(state)
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    status, body = state.scheduler.task_done(
        task["id"], device, {"ok": True, "result": 1, "attempt": 7}
    )
    assert status == 409 and body["error"] == "stale attempt"
    assert tasks_of(state, job["id"])[0]["status"] == "assigned"


def test_result_from_another_device_is_rejected(state: Any) -> None:
    owner = add_device(state, name="owner")
    stranger = add_device(state, name="stranger", status="offline")
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    assert task["device_id"] == owner["id"]

    status, body = state.scheduler.task_done(task["id"], stranger, {"ok": True, "result": 1})
    assert status == 409
    assert "not assigned" in body["error"]


def test_late_success_after_requeue_is_accepted(state: Any) -> None:
    """A phone that went offline mid-task delivers its result from its outbox."""
    device = add_device(state)
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    state.scheduler.requeue(task, "device went offline")
    assert tasks_of(state, job["id"])[0]["status"] == "queued"

    status, body = state.scheduler.task_done(
        task["id"], device, {"ok": True, "result": 42, "attempt": 1}
    )
    assert status == 200
    assert body.get("late") is True
    done = tasks_of(state, job["id"])[0]
    assert done["status"] == "done"
    assert done["result"] == 42
    assert done["device_id"] == device["id"]
    kinds = [e["kind"] for e in state.db.select("events", "WHERE task_id=?", (task["id"],))]
    assert "task_late_result" in kinds


def test_late_success_cancels_the_duplicate_attempt(state: Any) -> None:
    first = add_device(state, name="first")
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    assert task["device_id"] == first["id"]
    drain(state, first["id"])

    # first drops off; second picks the task up
    state.db.update("devices", {"id": first["id"]}, {"last_seen": now() - 999})
    state.scheduler.mark_offline_devices()
    second = add_device(state, name="second")
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    assert task["device_id"] == second["id"]
    assert task["attempts"] == 2
    drain(state, second["id"])

    # first comes back with the finished work
    status, body = state.scheduler.task_done(
        task["id"], first, {"ok": True, "result": "from first", "attempt": 1}
    )
    assert status == 200 and body.get("late") is True
    done = tasks_of(state, job["id"])[0]
    assert done["status"] == "done"
    assert done["result"] == "from first"
    assert done["device_id"] == first["id"]
    assert drain(state, second["id"]) == [{"cmd": "cancel", "task_id": task["id"]}]

    # second's own report is now a duplicate
    status, _ = state.scheduler.task_done(
        task["id"], second, {"ok": True, "result": "from second", "attempt": 2}
    )
    assert status == 409


def test_late_failure_is_ignored(state: Any) -> None:
    device = add_device(state)
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    state.scheduler.requeue(task, "lease expired")

    status, body = state.scheduler.task_done(
        task["id"], device, {"ok": False, "error": "boom", "attempt": 1}
    )
    assert status == 200
    assert body.get("ignored") == "late failure"
    again = tasks_of(state, job["id"])[0]
    assert again["status"] == "queued"
    assert again["attempts"] == 1  # not counted as another attempt


def test_late_result_from_a_device_that_never_ran_it_is_rejected(state: Any) -> None:
    device = add_device(state, name="owner")
    stranger = add_device(state, name="stranger", status="offline")
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    state.scheduler.requeue(task, "device went offline")
    assert task["device_id"] == device["id"]

    status, body = state.scheduler.task_done(task["id"], stranger, {"ok": True, "result": 1})
    assert status == 409
    assert "not assigned" in body["error"]


def test_checkpoint_is_stored_and_resent_on_reassignment(state: Any) -> None:
    """SPEC §6.5: progress saved by attempt 1 reaches attempt 2's task command."""
    device = add_device(state)
    job = submit_map(state, [1, 2, 3])
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    assert "checkpoint" not in drain(state, device["id"])[0]["task"]

    status, _ = state.scheduler.task_checkpoint(task["id"], device, {"n": 3, "out": [1]}, 1)
    assert status == 200
    assert tasks_of(state, job["id"])[0]["checkpoint"] == {"n": 3, "out": [1]}

    # stale attempt / wrong device / not running -> rejected
    assert state.scheduler.task_checkpoint(task["id"], device, {"x": 1}, 7)[0] == 409
    other = add_device(state, name="other")
    assert state.scheduler.task_checkpoint(task["id"], other, {"x": 1}, 1)[0] == 409
    assert state.scheduler.task_checkpoint("nope", device, {"x": 1}, None)[0] == 404

    state.scheduler.requeue(task, "device went offline")
    assert tasks_of(state, job["id"])[0]["checkpoint"] == {"n": 3, "out": [1]}  # survives requeue
    state.scheduler.assign()
    cmds = drain(state, device["id"]) + drain(state, other["id"])
    resumed = [c for c in cmds if c.get("cmd") == "task"][0]["task"]
    assert resumed["checkpoint"] == {"n": 3, "out": [1]}

    # a finished task drops its checkpoint
    holder = state.db.get("tasks", task["id"])
    state.scheduler.task_done(task["id"], state.db.get("devices", holder["device_id"]),
                              {"ok": True, "result": [1, 4, 9], "attempt": 2})
    assert tasks_of(state, job["id"])[0]["checkpoint"] is None


def test_checkpoint_size_limit(state: Any) -> None:
    device = add_device(state)
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    big = {"blob": "x" * (300 * 1024)}
    status, body = state.scheduler.task_checkpoint(task["id"], device, big, 1)
    assert status == 413 and "larger" in body["error"]
    assert tasks_of(state, job["id"])[0]["checkpoint"] is None


def test_streamed_results_go_to_parts_then_one_file(state: Any) -> None:
    """SPEC §6.6: options.stream_results writes items as tasks finish."""
    device = add_device(state, slots=2)
    job = submit_map(state, [1, 2, 3, 4], chunk_size=2, stream_results=True)
    state.scheduler.split_pending()
    state.scheduler.assign()
    tasks = tasks_of(state, job["id"])
    assert len(tasks) == 2
    state.scheduler.task_done(tasks[1]["id"], device, {"ok": True, "result": [9, 16], "attempt": 1})
    # second chunk landed first: its part exists, SQLite holds only a count
    assert state.results.progress(job)["parts"] == 1
    assert tasks_of(state, job["id"])[1]["result"] == {"streamed": 2}
    so_far = b"".join(state.results.iter_lines(state.db.get("jobs", job["id"])))
    assert so_far == b"9\n16\n"

    state.scheduler.task_done(tasks[0]["id"], device, {"ok": True, "result": [1, 4], "attempt": 1})
    state.scheduler.finish_jobs()
    finished = state.db.get("jobs", job["id"])
    assert finished["status"] == "done"
    result = finished["result"]
    assert result["count"] == 4 and result["tasks"] == 2
    data = state.files.read(result["results_file"])
    assert data == b"1\n4\n9\n16\n"  # idx order, not arrival order
    assert state.results.progress(job)["parts"] == 0  # parts cleaned up
    assert b"".join(state.results.iter_lines(finished)) == data


def test_done_twice_is_rejected(state: Any) -> None:
    device = add_device(state)
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    assert state.scheduler.task_done(task["id"], device, {"ok": True, "result": 1})[0] == 200
    assert state.scheduler.task_done(task["id"], device, {"ok": True, "result": 1})[0] == 409


def test_done_for_an_unknown_task(state: Any) -> None:
    device = add_device(state)
    status, _ = state.scheduler.task_done("ffffffffffff", device, {"ok": True})
    assert status == 404


def test_cancelled_result_is_swallowed(state: Any) -> None:
    device = add_device(state)
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    status, body = state.scheduler.task_done(
        task["id"], device, {"ok": False, "error": "cancelled"}
    )
    assert status == 200
    assert body["ignored"] == "cancelled"
    assert tasks_of(state, job["id"])[0]["status"] == "cancelled"


def test_task_started_marks_running(state: Any) -> None:
    device = add_device(state)
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    assert state.scheduler.task_started(task["id"], device) is True
    assert tasks_of(state, job["id"])[0]["status"] == "running"
    assert state.scheduler.task_started(task["id"], device) is True  # idempotent
    assert state.scheduler.task_started("ffffffffffff", device) is False


# --------------------------------------------------------------------------
# fail_fast, cancel, retry
# --------------------------------------------------------------------------

def test_fail_fast_cancels_the_rest(state: Any) -> None:
    device = add_device(state, slots=1)
    job = submit_map(state, [1, 2, 3], chunk_size=1, max_attempts=1, fail_fast=True)
    state.scheduler.split_pending()
    state.scheduler.assign()
    first = tasks_of(state, job["id"])[0]
    state.scheduler.task_done(first["id"], device, {"ok": False, "error": "nope"})
    statuses = [t["status"] for t in tasks_of(state, job["id"])]
    assert statuses == ["failed", "cancelled", "cancelled"]
    state.scheduler.finish_jobs()
    assert state.db.get("jobs", job["id"])["status"] == "failed"


def test_without_fail_fast_the_rest_keeps_going(state: Any) -> None:
    device = add_device(state, slots=1)
    job = submit_map(state, [1, 2], chunk_size=1, max_attempts=1)
    state.scheduler.split_pending()
    state.scheduler.assign()
    first = tasks_of(state, job["id"])[0]
    state.scheduler.task_done(first["id"], device, {"ok": False, "error": "nope"})
    assert [t["status"] for t in tasks_of(state, job["id"])] == ["failed", "queued"]


def test_cancel_job_cancels_tasks_and_notifies_the_device(state: Any) -> None:
    device = add_device(state, slots=1)
    job = submit_map(state, [1, 2], chunk_size=1)
    state.scheduler.split_pending()
    state.scheduler.assign()
    running = tasks_of(state, job["id"])[0]
    drain(state, device["id"])

    assert state.scheduler.cancel_job(job["id"]) is True
    assert state.db.get("jobs", job["id"])["status"] == "cancelled"
    assert [t["status"] for t in tasks_of(state, job["id"])] == ["cancelled", "cancelled"]
    assert drain(state, device["id"]) == [{"cmd": "cancel", "task_id": running["id"]}]
    assert state.scheduler.cancel_job(job["id"]) is False


def test_cancel_task(state: Any) -> None:
    device = add_device(state)
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    drain(state, device["id"])
    assert state.scheduler.cancel_task(task["id"]) is True
    assert drain(state, device["id"])[0]["cmd"] == "cancel"
    assert state.scheduler.cancel_task(task["id"]) is False


def test_retry_requeues_failed_tasks(state: Any) -> None:
    device = add_device(state)
    job = submit_map(state, [1], max_attempts=1)
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    state.scheduler.task_done(task["id"], device, {"ok": False, "error": "nope"})
    state.scheduler.finish_jobs()
    assert state.db.get("jobs", job["id"])["status"] == "failed"

    assert state.scheduler.retry_job(job["id"]) is True
    retried = state.db.get("jobs", job["id"])
    assert retried["status"] == "running"
    assert retried["failed_tasks"] == 0
    task = tasks_of(state, job["id"])[0]
    assert task["status"] == "queued"
    assert task["attempts"] == 0
    assert state.scheduler.retry_job("ffffffffffff") is False


# --------------------------------------------------------------------------
# completion + reduce
# --------------------------------------------------------------------------

def test_job_completes_with_the_reduced_result(state: Any) -> None:
    device = add_device(state, slots=4)
    job = submit_map(state, [1, 2, 3], chunk_size=1)
    state.scheduler.split_pending()
    state.scheduler.assign()
    for index, task in enumerate(tasks_of(state, job["id"])):
        state.scheduler.task_done(
            task["id"], device, {"ok": True, "result": [index + 1], "duration": 0.1}
        )
    state.scheduler.finish_jobs()
    finished = state.db.get("jobs", job["id"])
    assert finished["status"] == "done"
    assert finished["result"] == [1, 2, 3]
    assert finished["done_tasks"] == 3
    assert finished["finished"] is not None


def test_reduce_failure_fails_the_job(state: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    device = add_device(state)
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    state.scheduler.assign()
    task = tasks_of(state, job["id"])[0]
    state.scheduler.task_done(task["id"], device, {"ok": True, "result": [1]})

    plugin = state.plugins.require("python_map")
    monkeypatch.setattr(
        plugin, "_reduce", lambda params, results, ctx: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    state.scheduler.finish_jobs()
    assert "reduce failed" in state.db.get("jobs", job["id"])["error"]


# --------------------------------------------------------------------------
# pipelines and $from_job (SPEC §5, §9.1)
# --------------------------------------------------------------------------

def test_walk_path() -> None:
    data = {"a": {"b": [10, 20]}}
    assert walk_path(data, "a.b.1") == 20
    assert walk_path(data, None) is data
    assert walk_path(data, "a.missing") is None
    assert walk_path(data, "a.b.9") is None
    assert walk_path(5, "a") is None


def test_resolve_refs_uses_the_upstream_result(state: Any) -> None:
    job = submit_map(state, [1])
    state.db.update("jobs", {"id": job["id"]}, {"result": {"a": {"b": 42}}, "status": "done"})
    resolved = resolve_refs(
        {"x": {"$from_job": job["id"], "path": "a.b"}, "y": [1, {"z": 2}]}, state.db, {}
    )
    assert resolved == {"x": 42, "y": [1, {"z": 2}]}
    with pytest.raises(ValueError):
        resolve_refs({"x": {"$from_job": "nope"}}, state.db, {})


def test_pipeline_promotes_and_resolves(state: Any) -> None:
    device = add_device(state, slots=4)
    pipeline = state.submit_pipeline(
        "demo",
        [
            {"key": "a", "type": "python_map",
             "params": {"code": MAPPER, "items": [1, 2], "chunk_size": 2}},
            {"key": "b", "type": "python_map", "depends_on": ["a"],
             "params": {"code": MAPPER, "items": {"$from_job": "a"}, "chunk_size": 5}},
        ],
    )
    first, second = pipeline["jobs"]["a"], pipeline["jobs"]["b"]
    assert state.db.get("jobs", second)["status"] == "waiting"
    assert state.db.get("jobs", second)["pipeline_id"] == pipeline["pipeline_id"]

    state.scheduler.split_pending()
    state.scheduler.assign()
    for task in tasks_of(state, first):
        state.scheduler.task_done(task["id"], device, {"ok": True, "result": [7, 8]})
    state.scheduler.finish_jobs()
    assert state.db.get("jobs", first)["result"] == [7, 8]

    state.scheduler.promote_waiting()
    promoted = state.db.get("jobs", second)
    assert promoted["status"] == "pending"
    assert promoted["params"]["items"] == [7, 8]


def test_pipeline_dependency_failure_fails_the_dependent(state: Any) -> None:
    pipeline = state.submit_pipeline(
        None,
        [
            {"key": "a", "type": "python_map", "params": {"code": MAPPER, "items": [1]}},
            {"key": "b", "type": "python_map", "depends_on": ["a"],
             "params": {"code": MAPPER, "items": [1]}},
        ],
    )
    state.scheduler.fail_job(pipeline["jobs"]["a"], "boom")
    state.scheduler.promote_waiting()
    dependent = state.db.get("jobs", pipeline["jobs"]["b"])
    assert dependent["status"] == "failed"
    assert dependent["error"] == "dependency failed"


def test_unknown_dependency_fails_the_job(state: Any) -> None:
    job = state.submit_job(
        {"type": "python_map", "depends_on": ["ffffffffffff"],
         "params": {"code": MAPPER, "items": [1]}}
    )
    state.scheduler.promote_waiting()
    assert state.db.get("jobs", job["id"])["status"] == "failed"


def test_waiting_job_stays_waiting(state: Any) -> None:
    pipeline = state.submit_pipeline(
        None,
        [
            {"key": "a", "type": "python_map", "params": {"code": MAPPER, "items": [1]}},
            {"key": "b", "type": "python_map", "depends_on": ["a"],
             "params": {"code": MAPPER, "items": [1]}},
        ],
    )
    state.scheduler.promote_waiting()
    assert state.db.get("jobs", pipeline["jobs"]["b"])["status"] == "waiting"


def test_empty_pipeline_is_rejected(state: Any) -> None:
    with pytest.raises(ValueError):
        state.submit_pipeline("x", [])


# --------------------------------------------------------------------------
# devices
# --------------------------------------------------------------------------

def test_remove_device_requeues_and_notifies(state: Any) -> None:
    device = add_device(state)
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    state.scheduler.assign()
    drain(state, device["id"])

    assert state.scheduler.remove_device(device["id"]) is True
    commands = drain(state, device["id"])
    assert commands[0] == {"cmd": "removed"}
    assert state.db.get("devices", device["id"])["removed"] == 1
    assert tasks_of(state, job["id"])[0]["status"] == "queued"
    assert state.scheduler.remove_device(device["id"]) is False


def test_available_devices_reports_free_slots(state: Any) -> None:
    device = add_device(state, slots=3)
    job = submit_map(state, [1, 2], chunk_size=1)
    state.scheduler.split_pending()
    state.scheduler.assign()
    free = state.scheduler.available_devices()
    assert len(free) == 1
    assert free[0]["_free"] == 1


# --------------------------------------------------------------------------
# per-device roles and limits (SPEC §9.3)
# --------------------------------------------------------------------------

SERVE = "def serve(request, ctx):\n    return 'ok'\n"


def submit_service(state: Any, device_id: str, name: str = "svc") -> Dict[str, Any]:
    return state.submit_job({"type": "service", "params": {"name": name, "code": SERVE},
                             "options": {"targets": [device_id]}})


def test_jobs_role_off_keeps_the_device_out_of_normal_tasks(state: Any) -> None:
    device = add_device(state, settings={"roles": {"jobs": False}})
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 0
    state.db.update("devices", {"id": device["id"]}, {"settings": {"roles": {"jobs": True}}})
    assert state.scheduler.assign() == 1
    assert tasks_of(state, job["id"])[0]["device_id"] == device["id"]


def test_cpu_limit_sets_the_number_of_parallel_tasks(state: Any) -> None:
    add_device(state, slots=16, cores=8, settings={"cpu_pct": 50})
    job = submit_map(state, list(range(6)), chunk_size=1)
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 4                  # half of 8 cores
    state.scheduler.cancel_job(job["id"])
    add_device(state, name="all-in", slots=16, cores=8)
    job = submit_map(state, list(range(12)), chunk_size=1)
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 4 + 7              # 8 cores, one always left idle


def test_a_device_hosting_a_model_runs_one_task_beside_it(state: Any) -> None:
    device = add_device(state, slots=4)
    submit_service(state, device["id"])
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 1                  # the service, outside the job slots
    job = submit_map(state, [1, 2, 3], chunk_size=1)
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 1
    assert [t["status"] for t in tasks_of(state, job["id"])] == ["assigned", "queued", "queued"]


def test_a_pc_hosting_a_model_keeps_half_its_slots(state: Any) -> None:
    pc = add_device(state, name="pc", platform="windows", slots=11, cores=12)
    submit_service(state, pc["id"])
    state.scheduler.split_pending()
    state.scheduler.assign()
    job = submit_map(state, list(range(10)), chunk_size=1)
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 5                  # 11 // 2


def test_services_need_the_ai_role(state: Any) -> None:
    device = add_device(state, slots=2, settings={"roles": {"ai": False}})
    service = submit_service(state, device["id"])
    job = submit_map(state, [1])
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 1                  # the normal task only
    assert tasks_of(state, service["id"])[0]["status"] == "queued"
    assert tasks_of(state, job["id"])[0]["status"] == "assigned"


def test_models_wait_for_charging_or_half_a_battery(state: Any) -> None:
    device = add_device(state, battery=40, charging=False)
    service = submit_service(state, device["id"])
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 0
    state.db.update("devices", {"id": device["id"]}, {"charging": 1})
    assert state.scheduler.assign() == 1
    assert tasks_of(state, service["id"])[0]["device_id"] == device["id"]


def test_a_hot_device_hands_its_work_back_without_spending_attempts(state: Any) -> None:
    device = add_device(state, slots=2)
    job = submit_map(state, [1])
    service = submit_service(state, device["id"])
    state.scheduler.split_pending()
    state.scheduler.assign()
    drain(state, device["id"])
    state.db.update("devices", {"id": device["id"]}, {"temp": 43.5})
    assert state.scheduler.shed_unsafe() == 2
    for task in tasks_of(state, job["id"]) + tasks_of(state, service["id"]):
        assert task["status"] == "queued" and task["attempts"] == 0
        assert "too hot" in task["error"]
    assert {c["cmd"] for c in drain(state, device["id"])} == {"cancel"}
    assert state.scheduler.assign() == 0                  # resting until 36 C
    state.db.update("devices", {"id": device["id"]}, {"temp": 36.0})
    assert state.scheduler.assign() == 2


def test_switching_ai_off_moves_models_but_lets_tasks_finish(state: Any) -> None:
    device = add_device(state, slots=2)
    job = submit_map(state, [1])
    service = submit_service(state, device["id"])
    state.scheduler.split_pending()
    state.scheduler.assign()
    state.db.update("devices", {"id": device["id"]}, {"settings": {"roles": {"ai": False}}})
    assert state.scheduler.shed_unsafe() == 1
    assert tasks_of(state, service["id"])[0]["status"] == "queued"
    assert tasks_of(state, job["id"])[0]["status"] == "assigned"
    # the master switch does the same
    state.db.update("devices", {"id": device["id"]}, {"settings": {}, "paused": 1})
    assert state.scheduler.assign() == 0


# --------------------------------------------------------------------------
# speed (SPEC §9.5)
# --------------------------------------------------------------------------

def test_a_slow_device_is_skipped_when_fast_ones_finish_first(state: Any) -> None:
    fast = add_device(state, name="pc", bench_score=100.0, slots=2)
    slow = add_device(state, name="phone", bench_score=1.0, slots=1)
    job = submit_map(state, [1, 2, 3], chunk_size=1)
    state.scheduler.split_pending()
    assert state.scheduler.assign() == 2
    assert {t["device_id"] for t in tasks_of(state, job["id"]) if t["device_id"]} == {fast["id"]}
    assert not drain(state, slow["id"])
    # a long job: the phone's help is worth it
    big = submit_map(state, list(range(400)), chunk_size=1)
    state.scheduler.split_pending()
    state.scheduler.assign()
    assert any(t["device_id"] == slow["id"] for t in tasks_of(state, big["id"]))


def _straggler(state: Any) -> Any:
    """A fast device that finished its task and a slow one still on the other."""
    fast = add_device(state, name="pc", slots=1)
    slow = add_device(state, name="phone", slots=1)
    job = submit_map(state, [1, 2], chunk_size=1, timeout=600)
    state.scheduler.split_pending()
    state.scheduler.assign()
    tasks = tasks_of(state, job["id"])
    mine = next(t for t in tasks if t["device_id"] == fast["id"])
    theirs = next(t for t in tasks if t["device_id"] == slow["id"])
    state.scheduler.task_done(mine["id"], fast, {"ok": True, "result": [1], "duration": 1.0})
    state.db.update("tasks", {"id": theirs["id"]}, {"status": "running", "started_at": time.time() - 100})
    drain(state, fast["id"])
    drain(state, slow["id"])
    return fast, slow, job, theirs


def test_an_idle_device_runs_a_backup_and_the_first_result_wins(state: Any) -> None:
    fast, slow, job, task = _straggler(state)
    assert state.scheduler.backup_stragglers() == 1
    copy = drain(state, fast["id"])[0]["task"]
    assert copy["id"] == task["id"] and copy["backup"] is True
    assert state.scheduler.backup_stragglers() == 0       # one copy per task, and the PC is busy now
    status, body = state.scheduler.task_done(task["id"], fast, {"ok": True, "result": [2], "duration": 1.0,
                                                                "attempt": copy["attempt"]})
    assert status == 200 and body["ok"]
    done = tasks_of(state, job["id"])[1]
    assert done["status"] == "done" and done["device_id"] == fast["id"]
    assert done["history"][-1]["backup_won_by"] == fast["id"]
    assert drain(state, slow["id"]) == [{"cmd": "cancel", "task_id": task["id"]}]
    # the slow device's late answer changes nothing
    assert state.scheduler.task_done(task["id"], slow, {"ok": True, "result": [9]})[0] == 409
    state.scheduler.finish_jobs()
    finished = state.db.get("jobs", job["id"])
    assert finished["status"] == "done" and finished["result"] == [1, 2]
    report = finished["report"]
    assert report["backups"] == {"started": 1, "won": 1}
    assert report["fastest_device"]["name"] == "pc" and report["single_device"] is True


def test_when_the_original_wins_the_backup_is_cancelled(state: Any) -> None:
    fast, slow, job, task = _straggler(state)
    state.scheduler.backup_stragglers()
    drain(state, fast["id"])
    state.scheduler.task_done(task["id"], slow, {"ok": True, "result": [2], "duration": 100.0})
    assert drain(state, fast["id"]) == [{"cmd": "cancel", "task_id": task["id"]}]
    assert task["id"] not in state.scheduler.backups
    assert tasks_of(state, job["id"])[1]["device_id"] == slow["id"]


def test_a_failed_backup_leaves_the_original_running(state: Any) -> None:
    fast, slow, job, task = _straggler(state)
    state.scheduler.backup_stragglers()
    status, body = state.scheduler.task_done(task["id"], fast, {"ok": False, "error": "boom"})
    assert body.get("ignored") == "backup failed"
    current = tasks_of(state, job["id"])[1]
    assert current["status"] == "running" and current["device_id"] == slow["id"]


def test_the_backup_takes_over_when_the_original_device_leaves(state: Any) -> None:
    fast, slow, job, task = _straggler(state)
    state.scheduler.backup_stragglers()
    state.db.update("devices", {"id": slow["id"]}, {"last_seen": 1.0})
    state.scheduler.mark_offline_devices()
    current = tasks_of(state, job["id"])[1]
    assert current["status"] == "running" and current["device_id"] == fast["id"]
    assert current["attempts"] == 1                       # nothing was lost


def test_jobs_with_side_effects_never_get_backups(state: Any) -> None:
    fast = add_device(state, name="pc", slots=1)
    slow = add_device(state, name="phone", slots=1)
    job = state.submit_job({"type": "shell", "params": {"cmd": "echo hi"}})
    assert job["options"]["backups"] is False
    other = submit_map(state, [1], timeout=600)
    state.scheduler.split_pending()
    state.scheduler.assign()
    shell_task = tasks_of(state, job["id"])[0]
    map_task = tasks_of(state, other["id"])[0]
    holder = shell_task["device_id"]
    worker = fast if map_task["device_id"] == fast["id"] else slow
    state.scheduler.task_done(map_task["id"], worker, {"ok": True, "result": [1], "duration": 0.5})
    state.db.update("tasks", {"id": shell_task["id"]}, {"status": "running", "started_at": time.time() - 100})
    assert holder != worker["id"]
    assert state.scheduler.backup_stragglers() == 0


def test_speed_report_compares_with_the_fastest_device_alone(state: Any) -> None:
    pc = add_device(state, name="pc", slots=2)
    phone = add_device(state, name="phone", slots=1)
    job = submit_map(state, [1, 2, 3, 4], chunk_size=1)
    state.scheduler.split_pending()
    state.scheduler.assign()
    tasks = tasks_of(state, job["id"])
    for task in tasks:
        holder = pc if task["device_id"] == pc["id"] else phone
        duration = 1.0 if holder is pc else 4.0
        state.scheduler.task_done(task["id"], holder, {"ok": True, "result": [task["idx"]], "duration": duration})
    queued = [t for t in tasks_of(state, job["id"]) if t["status"] == "queued"]
    for task in queued:
        state.db.update("tasks", {"id": task["id"]}, {"status": "done", "device_id": pc["id"], "duration": 1.0,
                                                      "result": [task["idx"]]})
    state.db.update("jobs", {"id": job["id"]}, {"started": time.time() - 2.0})
    state.scheduler.finish_jobs()
    report = state.db.get("jobs", job["id"])["report"]
    assert report["fastest_device"]["name"] == "pc"
    # 4 tasks at 1 s on 2 slots = 2 s alone
    assert report["fastest_device"]["alone_s"] == pytest.approx(2.0)
    assert report["speedup"] > 0 and {d["name"] for d in report["devices"]} <= {"pc", "phone"}
