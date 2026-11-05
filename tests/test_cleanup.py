"""Only the final result stays (SPEC §9.10): a finished job's in-between data is
cleaned an hour later, and deleting a job takes its logs and files with it."""

from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest

from coordinator.cleanup import CLEAN_AFTER

HOUR = 3600.0


def add_file(cluster: Any, data: bytes, name: str) -> str:
    return cluster.files.add_bytes(data, name)["id"]


def make_job(cluster: Any, job_id: str, status: str = "done", finished_ago: float = 2 * HOUR,
             params: Any = None, result: Any = None, tasks: List[Dict[str, Any]] = ()) -> None:
    stamp = time.time() - finished_ago
    cluster.db.insert("jobs", {"id": job_id, "name": job_id, "type": "media", "params": params or {},
                               "status": status, "created": stamp - 60, "finished": stamp,
                               "result": result, "options": {}})
    for i, task in enumerate(tasks):
        row = {"id": "%s-t%d" % (job_id, i), "job_id": job_id, "idx": i, "status": "done",
               "payload": {"impls": {"python": {"code": "x" * 1000}}, "meta": {"segment": i}},
               "result": {"bytes": 123}, "outputs": [], "finished_at": stamp, "checkpoint": {"at": 5}}
        row.update(task)
        cluster.db.insert("tasks", row)
        cluster.append_task_log(row["id"], ["working on %d" % i])


@pytest.fixture()
def video_job(cluster: Any) -> Dict[str, str]:
    """A split-video job: two pieces (in-between), one joined file (final), one input."""
    files = {
        "input": add_file(cluster, b"the original video", "in.mp4"),
        "piece0": add_file(cluster, b"piece zero", "seg0.mp4"),
        "piece1": add_file(cluster, b"piece one", "seg1.mp4"),
        "final": add_file(cluster, b"the joined video", "out.mp4"),
    }
    make_job(cluster, "videojob", params={"inputs": [files["input"]], "op": "transcode"},
             result=[{"file_id": files["final"], "name": "out.mp4"}],
             tasks=[{"outputs": [{"file_id": files["piece0"], "name": "seg0.mp4"}]},
                    {"outputs": [{"file_id": files["piece1"], "name": "seg1.mp4"}]},
                    {"check_of": "videojob-t0", "role": "check",
                     "outputs": [{"file_id": files["piece0"], "name": "seg0.mp4"}]}])
    return files


@pytest.mark.asyncio
async def test_an_hour_after_a_job_succeeded_only_its_final_result_stays(
        client: Any, cluster: Any, video_job: Dict[str, str]) -> None:
    assert cluster.cleanup.sweep() == 1
    tasks = cluster.db.select("tasks", "WHERE job_id='videojob' ORDER BY idx")
    assert [t["id"] for t in tasks] == ["videojob-t0", "videojob-t1"]   # the double-check copy is gone
    for task in tasks:
        assert task["result"] is None and task["checkpoint"] is None and task["outputs"] == []
        assert task["payload"] == {"meta": {"segment": task["idx"]}}     # what it was, not its input
        assert task["status"] == "done" and cluster.read_task_log(task["id"])  # history and logs stay
    assert cluster.files.meta(video_job["piece0"]) is None and cluster.files.meta(video_job["piece1"]) is None
    assert cluster.files.meta(video_job["final"]) and cluster.files.meta(video_job["input"])
    job = (await client.get("/api/jobs/videojob")).json()
    assert job["result"] == [{"file_id": video_job["final"], "name": "out.mp4"}] and job["cleaned"]
    assert cluster.cleanup.sweep() == 0   # once is enough


def test_recent_failed_and_cancelled_jobs_keep_everything(cluster: Any) -> None:
    piece = add_file(cluster, b"a piece", "p.bin")
    make_job(cluster, "recent", finished_ago=CLEAN_AFTER - 120, tasks=[{"outputs": [{"file_id": piece}]}])
    make_job(cluster, "broken", status="failed", tasks=[{"outputs": [{"file_id": piece}]}])
    make_job(cluster, "stopped", status="cancelled", tasks=[{}])
    assert cluster.cleanup.sweep() == 0
    assert all(t["result"] == {"bytes": 123} for t in cluster.db.select("tasks"))
    assert cluster.files.meta(piece) is not None
    # not before wrong answers can no longer reopen it
    cluster.config.set("verify", dict(cluster.config.get("verify") or {}, recheck_minutes=120))
    assert cluster.cleanup.delay() > 2 * HOUR


def test_a_file_something_else_uses_is_kept(cluster: Any) -> None:
    shared = add_file(cluster, b"shared bytes", "shared.bin")
    make_job(cluster, "first", tasks=[{"outputs": [{"file_id": shared}]}])
    make_job(cluster, "second", finished_ago=60, params={"inputs": [shared]})
    cluster.cleanup.sweep()
    assert cluster.files.meta(shared) is not None
    assert cluster.db.get("tasks", "first-t0")["outputs"] == []


DAY = 86400.0


@pytest.mark.asyncio
async def test_old_jobs_keep_only_their_final_result(client: Any, cluster: Any) -> None:
    given = add_file(cluster, b"an input file", "in.csv")
    final = add_file(cluster, b"the final file", "out.csv")
    piece = add_file(cluster, b"a piece of it", "piece.csv")
    old = 40 * DAY
    make_job(cluster, "old-done", finished_ago=old, params={"inputs": [given]},
             result={"file_id": final, "rows": 12}, tasks=[{"outputs": [{"file_id": piece}]}])
    make_job(cluster, "old-failed", status="failed", finished_ago=old, tasks=[{}])
    make_job(cluster, "old-cancelled", status="cancelled", finished_ago=old, tasks=[{}])
    make_job(cluster, "recent", finished_ago=10 * DAY, tasks=[{}])
    cluster.db.add_event("device_online", "long ago")
    cluster.db.execute("UPDATE events SET ts=? WHERE message='long ago'", (time.time() - old,))
    cluster.db.add_event("device_online", "today")

    assert cluster.cleanup.expire() == 3
    for job_id in ("old-done", "old-failed", "old-cancelled"):
        assert cluster.db.select("tasks", "WHERE job_id=?", (job_id,)) == []
        assert not cluster.task_log_path("%s-t0" % job_id).exists()
    job = (await client.get("/api/jobs/old-done")).json()
    assert job["result"] == {"file_id": final, "rows": 12} and job["expired"]   # the final result stays
    assert cluster.files.meta(final) is None and cluster.files.meta(piece) is None  # its files go
    assert cluster.files.meta(given) is not None                                   # what it was given stays
    assert len(cluster.db.select("tasks", "WHERE job_id='recent'")) == 1
    assert [e["message"] for e in cluster.db.select("events")] == ["today"]
    assert cluster.cleanup.expire() == 0


@pytest.mark.asyncio
async def test_the_age_is_a_setting_and_zero_keeps_everything(client: Any, cluster: Any) -> None:
    make_job(cluster, "ten-days", finished_ago=10 * DAY, tasks=[{}])
    assert (await client.get("/api/settings")).json()["cleanup"] == {"keep_days": 30}
    assert (await client.patch("/api/settings", json={"cleanup": {"keep_days": 0}})).status_code == 200
    assert cluster.cleanup.expire() == 0
    saved = (await client.patch("/api/settings", json={"cleanup": {"keep_days": 7}})).json()
    assert saved["cleanup"] == {"keep_days": 7}
    assert cluster.cleanup.expire() == 1


@pytest.mark.asyncio
async def test_deleting_a_job_takes_its_logs_and_files(client: Any, cluster: Any, video_job: Dict[str, str]) -> None:
    other = add_file(cluster, b"used by a template", "keep.bin")
    make_job(cluster, "another", tasks=[{"outputs": [{"file_id": other}]}])
    cluster.db.insert("templates", {"id": "tpl", "name": "t", "job_spec": {"params": {"inputs": [other]}},
                                    "created": time.time()})
    assert (await client.delete("/api/jobs/videojob")).status_code == 200
    assert cluster.db.select("tasks", "WHERE job_id='videojob'") == []
    assert not cluster.task_log_path("videojob-t0").exists()
    for name in ("piece0", "piece1", "final"):
        assert cluster.files.meta(video_job[name]) is None
    assert cluster.files.meta(video_job["input"]) is not None   # what it was given stays
    assert (await client.delete("/api/jobs/another")).status_code == 200
    assert cluster.files.meta(other) is not None                # a template still uses it
