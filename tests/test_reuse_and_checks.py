"""Never compute twice (SPEC 9.8) and catching wrong answers (SPEC 9.9)."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from coordinator import cache as cache_module
from coordinator import verify as verify_module
from coordinator.db import now
from test_scheduler import MAPPER, add_device, drain, submit_map, tasks_of


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def caching(state: Any) -> None:
    state.config.set("cache.enabled", True)


def run_queued(state: Any, device: Dict[str, Any], answer: Any = None) -> List[Dict[str, Any]]:
    """Finish every task the scheduler sent to ``device``: result = the items
    squared, or ``answer(items)`` when given."""
    finished = []
    for command in drain(state, device["id"]):
        if command.get("cmd") != "task":
            continue
        task = command["task"]
        items = task["payload"]["impls"]["python"]["args"].get("items") or []
        result = answer(items) if answer else [i * i for i in items]
        status, body = state.scheduler.task_done(task["id"], device, {
            "ok": True, "result": result, "duration": 0.2, "attempt": task["attempt"], "impl": "python"})
        assert status == 200, body
        finished.append(task)
    return finished


def run_job(state: Any, job: Dict[str, Any], device: Dict[str, Any]) -> Dict[str, Any]:
    for _ in range(20):
        state.scheduler.split_pending()
        state.scheduler.assign()
        run_queued(state, device)
        state.scheduler.finish_jobs()
        row = state.db.get("jobs", job["id"])
        if row["status"] in ("done", "failed"):
            return row
    raise AssertionError("job did not finish")


# --------------------------------------------------------------------------
# never compute twice
# --------------------------------------------------------------------------

def test_the_same_job_again_is_answered_from_the_cache(state: Any) -> None:
    caching(state)
    phone = add_device(state, "phone")
    first = run_job(state, submit_map(state, [1, 2, 3], chunk_size=1), phone)
    assert first["result"] == [1, 4, 9]

    again = submit_map(state, [1, 2, 3], chunk_size=1)
    state.scheduler.split_pending()
    again = state.db.get("jobs", again["id"])
    assert again["status"] == "done" and again["result"] == [1, 4, 9]
    assert again["total_tasks"] == 0 and tasks_of(state, again["id"]) == []
    assert again["report"]["cached"] is True and again["report"]["from_job"] == first["id"]
    assert drain(state, phone["id"]) == []
    kinds = [e["kind"] for e in state.db.select("events", "WHERE job_id=?", (again["id"],))]
    assert "job_cached" in kinds
    assert state.cache.stats()["hits"] >= 1


def test_only_changed_tasks_run_again(state: Any) -> None:
    caching(state)
    phone = add_device(state, "phone")
    run_job(state, submit_map(state, [1, 2, 3], chunk_size=1), phone)

    changed = submit_map(state, [1, 2, 5], chunk_size=1)
    state.scheduler.split_pending()
    tasks = tasks_of(state, changed["id"])
    assert [t["reused"] for t in tasks] == [1, 1, 0]
    assert [t["status"] for t in tasks] == ["done", "done", "queued"]
    assert state.db.get("jobs", changed["id"])["done_tasks"] == 2
    done = run_job(state, changed, phone)
    assert done["result"] == [1, 4, 25]
    assert done["report"]["reused_tasks"] == 2


def test_dynamic_jobs_are_reused_whole(state: Any) -> None:
    caching(state)
    phone = add_device(state, "phone", bench_score=5)
    spec = {"type": "prime_range", "params": {"start": 2, "end": 20000}}

    def primes(items: Any) -> Any:  # prime_range has no items: answer from the payload
        return None

    job = state.submit_job(dict(spec))
    for _ in range(50):
        state.scheduler.split_pending()
        state.scheduler.assign()
        for command in drain(state, phone["id"]):
            task = command["task"]
            args = task["payload"]["impls"]["python"]["args"]
            count = sum(1 for n in range(max(2, args["start"]), args["end"])
                        if all(n % d for d in range(2, int(n ** 0.5) + 1)))
            state.scheduler.task_done(task["id"], phone, {"ok": True, "result": {"count": count},
                                                          "duration": 0.6, "attempt": task["attempt"]})
        state.scheduler.finish_jobs()
        if state.db.get("jobs", job["id"])["status"] == "done":
            break
    assert state.db.get("jobs", job["id"])["result"] == {"count": 2262}
    again = state.submit_job(dict(spec))
    state.scheduler.split_pending()
    assert state.db.get("jobs", again["id"])["result"] == {"count": 2262}
    assert state.db.get("jobs", again["id"])["status"] == "done"


def test_cache_false_runs_again_and_refresh_replaces(state: Any) -> None:
    caching(state)
    phone = add_device(state, "phone")
    run_job(state, submit_map(state, [2], chunk_size=1), phone)

    off = submit_map(state, [2], chunk_size=1, cache=False)
    state.scheduler.split_pending()
    assert [t["status"] for t in tasks_of(state, off["id"])] == ["queued"]
    run_job(state, off, phone)

    fresh = submit_map(state, [2], chunk_size=1, cache="refresh")
    state.scheduler.split_pending()
    assert [t["status"] for t in tasks_of(state, fresh["id"])] == ["queued"]
    state.scheduler.assign()
    run_queued(state, phone, answer=lambda items: ["new"])
    state.scheduler.finish_jobs()
    assert state.db.get("jobs", fresh["id"])["result"] == ["new"]
    # the fresh answer replaced the stored one
    later = submit_map(state, [2], chunk_size=1)
    state.scheduler.split_pending()
    assert state.db.get("jobs", later["id"])["result"] == ["new"]


def test_shell_and_targets_all_are_never_cached(state: Any) -> None:
    caching(state)
    for spec in ({"type": "shell", "params": {"cmd": "date"}},
                 {"type": "python_map", "params": {"code": MAPPER, "items": [1]},
                  "options": {"targets": "all"}}):
        job = state.submit_job(spec)
        assert state.cache.mode(state.db.get("jobs", job["id"]),
                                state.plugins.get(spec["type"])) == "off"


def test_an_entry_whose_file_is_gone_is_not_reused(state: Any) -> None:
    caching(state)
    meta = state.files.add_bytes(b"output bytes", "out.txt")
    state.cache.put("k" * 64, "task", "media", {"ok": 1}, [{"file_id": meta["id"], "name": "out.txt"}])
    assert state.cache.get("k" * 64) is not None
    state.files.delete(meta["id"])
    assert state.cache.get("k" * 64) is None
    assert state.db.get("cache", "k" * 64, id_col="key") is None


def test_results_a_wrong_device_stored_are_dropped(state: Any) -> None:
    caching(state)
    state.cache.put("a" * 64, "task", "python_map", [1], [], ["bad"], "j1")
    state.cache.put("b" * 64, "job", "python_map", [2], [], ["good", "bad"], "j2")
    state.cache.put("c" * 64, "job", "python_map", [3], [], ["good"], "j3")
    assert state.cache.forget_device("bad", since=0) == 2
    assert [e["key"][0] for e in state.cache.listing()] == ["c"]


def test_fingerprints_ignore_operational_options_and_key_order() -> None:
    a = cache_module.job_key("python_map", "v1", {"code": "x", "items": [1]}, {"timeout": 5, "cache": True})
    b = cache_module.job_key("python_map", "v1", {"items": [1], "code": "x"}, {"timeout": 99})
    c = cache_module.job_key("python_map", "v2", {"items": [1], "code": "x"}, {})
    assert a == b != c
    t1 = cache_module.task_key({"impls": {"python": {"code": "c", "args": {"a": 1}}}, "inputs": ["f"],
                                "timeout": 5, "meta": {"x": 1}})
    t2 = cache_module.task_key({"impls": {"python": {"args": {"a": 1}, "code": "c"}}, "inputs": ["f"]})
    assert t1 == t2


def test_scheduled_jobs_skip_the_cache(state: Any) -> None:
    import asyncio

    row = state.create_schedule("nightly", "0 3 * * *", {"type": "python_map",
                                                         "params": {"code": MAPPER, "items": [1]}})
    job_id = asyncio.run(state.schedules.fire(row))
    assert state.db.get("jobs", job_id)["options"]["cache"] is False


def test_pipeline_run_again_reuses_every_unchanged_step(state: Any) -> None:
    caching(state)
    phone = add_device(state, "phone")
    steps = [
        {"key": "a", "type": "python_map", "params": {"code": MAPPER, "items": [1, 2], "chunk_size": 1}},
        {"key": "b", "type": "python_map", "depends_on": ["a"],
         "params": {"code": MAPPER, "items": {"$from_job": "a"}, "chunk_size": 1}},
    ]
    first = state.submit_pipeline("p", [dict(s) for s in steps])
    for _ in range(30):
        state.scheduler.promote_waiting()
        run_job(state, {"id": first["jobs"]["a"]}, phone)
        state.scheduler.promote_waiting()
        if state.db.get("jobs", first["jobs"]["b"])["status"] == "pending":
            run_job(state, {"id": first["jobs"]["b"]}, phone)
            break
    assert state.db.get("jobs", first["jobs"]["b"])["result"] == [1, 16]

    second = state.submit_pipeline("p", [dict(s) for s in steps])
    state.scheduler.split_pending()
    state.scheduler.promote_waiting()
    state.scheduler.split_pending()
    for key in ("a", "b"):
        job = state.db.get("jobs", second["jobs"][key])
        assert job["status"] == "done" and job["report"]["cached"] is True
    assert drain(state, phone["id"]) == []


# --------------------------------------------------------------------------
# catching wrong answers
# --------------------------------------------------------------------------

def checked_job(state: Any, items: List[Any]) -> Dict[str, Any]:
    return submit_map(state, items, chunk_size=1, verify=1.0)


def step(state: Any) -> None:
    state.scheduler.split_pending()
    state.scheduler.assign()


def test_an_agreeing_check_builds_trust(state: Any) -> None:
    a = add_device(state, "a", bench_score=30)
    b = add_device(state, "b", bench_score=20)
    job = checked_job(state, [3])
    step(state)
    run_queued(state, a)
    check = [t for t in tasks_of(state, job["id"]) if t["role"] == "check"]
    assert len(check) == 1 and check[0]["payload"]["avoid"] == [a["id"]]
    assert list(check[0]["payload"]["impls"]) == ["python"]
    state.scheduler.finish_jobs()
    assert state.db.get("jobs", job["id"])["status"] == "running"  # waits for the check
    state.scheduler.assign()
    assert drain(state, a["id"]) == []  # never on the device that did it
    run_queued(state, b)
    state.scheduler.finish_jobs()
    done = state.db.get("jobs", job["id"])
    assert done["status"] == "done" and done["result"] == [9]
    assert done["total_tasks"] == 1 and done["done_tasks"] == 1
    assert done["report"]["checks"]["confirmed"] == 1
    for device in (a, b):
        trust = verify_module.trust_of(state.db.get("devices", device["id"]))
        assert trust["agreed"] == 1 and trust["wrong"] == 0


def test_a_wrong_device_is_caught_corrected_and_its_recent_work_redone(state: Any) -> None:
    a = add_device(state, "a", bench_score=30)
    b = add_device(state, "b", bench_score=20)
    c = add_device(state, "c", bench_score=10)
    # earlier work by "a" in a finished job (not sampled for a check)
    earlier = submit_map(state, [4], chunk_size=1)
    step(state)
    run_queued(state, a)
    state.scheduler.finish_jobs()
    assert state.db.get("jobs", earlier["id"])["status"] == "done"

    job = checked_job(state, [7])
    step(state)
    run_queued(state, a, answer=lambda items: [999])  # a is wrong
    state.scheduler.assign()
    run_queued(state, b)                               # b says 49
    decide = [t for t in tasks_of(state, job["id"]) if t["role"] == "decide"]
    assert len(decide) == 1 and set(decide[0]["payload"]["avoid"]) == {a["id"], b["id"]}
    state.scheduler.assign()
    assert drain(state, a["id"]) == [] and drain(state, b["id"]) == []  # the decider is a third device
    run_queued(state, c)                               # c agrees with b
    state.scheduler.finish_jobs()

    done = state.db.get("jobs", job["id"])
    assert done["status"] == "done" and done["result"] == [49]
    original = [t for t in tasks_of(state, job["id"]) if t["role"] is None][0]
    assert original["verified"] == 1 and "wrong answer" in original["history"][-1]["error"]
    trust_a = verify_module.trust_of(state.db.get("devices", a["id"]))
    assert trust_a["wrong"] == 1 and trust_a["score"] < verify_module.SUSPECT_BELOW
    assert verify_module.trust_of(state.db.get("devices", b["id"]))["agreed"] == 1
    assert verify_module.trust_of(state.db.get("devices", c["id"]))["agreed"] == 1
    # the finished earlier job opened again; its task runs again, never on "a"
    reopened = state.db.get("jobs", earlier["id"])
    assert reopened["status"] == "running"
    redo = tasks_of(state, earlier["id"])[0]
    assert redo["status"] == "queued" and a["id"] in redo["payload"]["avoid"]
    events = [e["kind"] for e in state.db.select("events")]
    assert "wrong_answer" in events and "job_rechecked" in events
    # a gets checked more often now
    assert state.verifier.share({"options": {}}, state.db.get("devices", a["id"])) == 0.0  # tests turn it off
    state.config.set("verify.suspect_share", 0.5)
    assert state.verifier.share({"options": {}}, state.db.get("devices", a["id"])) == 0.5


def test_three_different_answers_mean_the_work_is_not_repeatable(state: Any) -> None:
    a = add_device(state, "a", bench_score=30)
    b = add_device(state, "b", bench_score=20)
    c = add_device(state, "c", bench_score=10)
    job = checked_job(state, [1])
    step(state)
    run_queued(state, a, answer=lambda items: ["a"])
    state.scheduler.assign()
    run_queued(state, b, answer=lambda items: ["b"])
    state.scheduler.assign()
    run_queued(state, c, answer=lambda items: ["c"])
    state.scheduler.finish_jobs()
    assert state.db.get("jobs", job["id"])["status"] == "done"
    for device in (a, b, c):
        assert verify_module.trust_of(state.db.get("devices", device["id"]))["wrong"] == 0
    key = state.db.get("jobs", job["id"])["options"]["_speed_key"]
    assert key in state.verifier.unrepeatable()
    again = checked_job(state, [2])
    step(state)
    run_queued(state, a)
    assert [t for t in tasks_of(state, again["id"]) if t["role"]] == []


def test_without_a_third_device_a_disagreement_is_reported_not_punished(state: Any) -> None:
    a = add_device(state, "a", bench_score=30)
    b = add_device(state, "b", bench_score=20)
    job = checked_job(state, [5])
    step(state)
    run_queued(state, a, answer=lambda items: [1])
    state.scheduler.assign()
    run_queued(state, b)
    state.scheduler.finish_jobs()
    assert state.db.get("jobs", job["id"])["status"] == "done"
    original = [t for t in tasks_of(state, job["id"]) if t["role"] is None][0]
    assert original["verified"] == -1
    assert verify_module.trust_of(state.db.get("devices", a["id"]))["wrong"] == 0
    assert "task_disputed" in [e["kind"] for e in state.db.select("events")]


def test_repeated_wrong_answers_bench_the_device_until_trusted_again(state: Any) -> None:
    a = add_device(state, "a")
    for _ in range(3):
        state.verifier.wrong(a["id"])
    device = state.db.get("devices", a["id"])
    assert device["trust"]["benched"] is True
    assert state.verifier.view(device)["state"] == "no jobs"
    loose = {"payload": {"impls": {"python": {}}}, "_job_options": {}}
    pinned = {"payload": {"impls": {"python": {}}, "device_id": a["id"]}, "_job_options": {}}
    assert not state.scheduler.eligible(loose, device)
    assert state.scheduler.eligible(pinned, device)
    view = state.verifier.reset(a["id"])
    assert view["benched"] is False and view["score"] == 0.5
    assert state.scheduler.eligible(loose, state.db.get("devices", a["id"]))


def test_a_check_nobody_can_take_is_dropped(state: Any) -> None:
    a = add_device(state, "a", bench_score=30)
    b = add_device(state, "b", bench_score=20)
    job = checked_job(state, [5])
    step(state)
    run_queued(state, a)
    state.db.update("devices", {"id": b["id"]}, {"status": "offline"})
    check = [t for t in tasks_of(state, job["id"]) if t["role"] == "check"][0]
    payload = dict(check["payload"], check_since=now() - verify_module.WAIT_SECONDS - 1)
    state.db.update("tasks", {"id": check["id"]}, {"payload": payload})
    assert state.verifier.expire() == 1
    state.scheduler.finish_jobs()
    assert state.db.get("jobs", job["id"])["status"] == "done"


def test_checks_only_compare_the_same_runtime(state: Any) -> None:
    py = add_device(state, "py", bench_score=30)
    add_device(state, "browser", caps=["js", "browser"], platform="browser", bench_score=20)
    job = state.submit_job({"type": "monte_carlo_pi", "params": {"samples": 1000, "chunks": 1},
                            "options": {"verify": 1.0}})
    step(state)
    for command in drain(state, py["id"]):
        task = command["task"]
        state.scheduler.task_done(task["id"], py, {"ok": True, "result": {"inside": 785, "samples": 1000},
                                                   "duration": 0.1, "impl": "python"})
    # the only other device runs JS, whose random numbers differ: no check
    assert [t for t in tasks_of(state, job["id"]) if t["role"]] == []


def test_trust_and_cache_endpoints(state: Any) -> None:
    a = add_device(state, "a")
    assert state.verifier.view(a)["state"] == "trusted"
    state.verifier.wrong(a["id"])
    assert state.verifier.view(state.db.get("devices", a["id"]))["state"] == "checking more"


@pytest.mark.asyncio
async def test_rerun_fresh_and_cache_api(client: Any, cluster: Any) -> None:
    cluster.config.set("cache.enabled", True)
    job = (await client.post("/api/jobs", json={"type": "python_map",
                                                "params": {"code": MAPPER, "items": [1]}})).json()
    again = (await client.post("/api/jobs/%s/rerun" % job["id"], json={"fresh": True})).json()
    assert again["id"] != job["id"] and again["options"]["cache"] == "refresh"
    plain = (await client.post("/api/jobs/%s/rerun" % again["id"])).json()
    assert "cache" not in plain["options"]
    info = (await client.get("/api/cache")).json()
    assert info["stats"]["enabled"] is True
    assert (await client.delete("/api/cache")).json()["ok"] is True
    trust = (await client.get("/api/trust")).json()
    assert trust == []
