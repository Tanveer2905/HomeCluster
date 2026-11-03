"""Catching wrong answers from old hardware (SPEC §9.9).

Old phones can be wrong without crashing: a worn memory chip, an overheated
CPU, a buggy runtime.  So a share of the tasks of *exact* job types (plugins
with ``VERIFY = True``: maps, number crunching, WebAssembly) runs a second time
on a different device, using the same runtime kind (Python vs Python, JS vs
JS), and the two answers are compared:

* **same** -- both devices gain trust;
* **different** -- a third device runs it once more and decides.  The device
  that disagreed loses trust, its task results of the last
  ``verify.recheck_minutes`` are computed again (finished jobs included, which
  then finish again with the corrected result), results it put in the cache
  are dropped, and the user gets a notification.  It keeps working, but half
  of its tasks are checked until its trust recovers;
* **wrong ``verify.strikes`` times within a day** -- it gets no more jobs
  (still connected, services and storage keep going) until the user presses
  *Trust again*;
* **all three different** -- the work itself is not repeatable (it reads the
  clock, uses unseeded randomness): nobody is blamed, and that kind of work is
  not checked any more.

With no third device to decide, the disagreement is reported and nobody
loses trust.  A check that finds no free device within ``WAIT_SECONDS`` is
dropped: checking never holds a job up for long.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Dict, List, Optional, Tuple

from .cache import result_digest
from .db import new_id, now

log = logging.getLogger("coordinator.verify")

#: trust score below this: "checking more"
SUSPECT_BELOW = 0.75
#: what one agreement / one wrong answer does to the score
AGREE_GAIN = 0.05
WRONG_LOSS = 0.3
#: a queued check nobody can take is dropped after this long
WAIT_SECONDS = 120.0
#: where the speed keys of work that is not repeatable are remembered
UNREPEATABLE_SETTING = "verify_unrepeatable"
#: impl preference of each worker kind, to infer what ran when a worker does not say
WORKER_ORDER = ("python", "shell", "js", "wasm")
BROWSER_ORDER = ("wasm", "js", "python")


def fresh_trust() -> Dict[str, Any]:
    return {"score": 1.0, "checks": 0, "agreed": 0, "wrong": 0, "strikes": [], "benched": False}


def trust_of(device: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    trust = fresh_trust()
    saved = (device or {}).get("trust")
    if isinstance(saved, dict):
        trust.update(saved)
    return trust


def benched(device: Optional[Dict[str, Any]]) -> bool:
    """Too many wrong answers lately: no normal tasks unless pinned to it."""
    return bool(trust_of(device).get("benched"))


class Verifier:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.db = state.db
        self.random = random.Random()

    @property
    def scheduler(self) -> Any:
        return self.state.scheduler

    def _setting(self, key: str, default: Any) -> Any:
        return self.state.setting("verify.%s" % key, default)

    # ------------------------------------------------------------- trust
    def view(self, device: Dict[str, Any]) -> Dict[str, Any]:
        trust = trust_of(device)
        if trust.get("benched"):
            label, note = "no jobs", "gave wrong answers %d times lately; press Trust again to use it" \
                % len(trust.get("strikes") or [])
        elif float(trust["score"]) < SUSPECT_BELOW:
            label, note = "checking more", "gave a wrong answer; half of its tasks are checked"
        else:
            label, note = "trusted", None
        return {"score": round(float(trust["score"]), 2), "checks": int(trust["checks"]),
                "agreed": int(trust["agreed"]), "wrong": int(trust["wrong"]),
                "benched": bool(trust.get("benched")), "state": label, "note": note,
                "last_wrong": trust.get("last_wrong")}

    def _save(self, device_id: str, trust: Dict[str, Any]) -> None:
        self.db.update("devices", {"id": device_id}, {"trust": trust})
        self.state.publish_device(device_id)

    def agree(self, device_id: Optional[str]) -> None:
        device = self.db.get("devices", device_id or "")
        if device is None:
            return
        trust = trust_of(device)
        trust["checks"] = int(trust["checks"]) + 1
        trust["agreed"] = int(trust["agreed"]) + 1
        trust["score"] = round(min(1.0, float(trust["score"]) + AGREE_GAIN), 3)
        self._save(device["id"], trust)

    def wrong(self, device_id: str) -> Dict[str, Any]:
        device = self.db.get("devices", device_id) or {"id": device_id}
        trust = trust_of(device)
        stamp = now()
        trust["checks"] = int(trust["checks"]) + 1
        trust["wrong"] = int(trust["wrong"]) + 1
        trust["score"] = round(max(0.0, float(trust["score"]) - WRONG_LOSS), 3)
        trust["strikes"] = [t for t in (trust.get("strikes") or []) if t > stamp - 86400] + [stamp]
        trust["last_wrong"] = stamp
        newly = False
        if len(trust["strikes"]) >= int(self._setting("strikes", 3) or 3) and not trust.get("benched"):
            trust["benched"] = True
            newly = True
        self._save(device_id, trust)
        return {"trust": trust, "benched_now": newly}

    def reset(self, device_id: str) -> Dict[str, Any]:
        """The user trusts the device again: jobs resume, checks stay frequent
        until it proves itself (score 0.5)."""
        device = self.db.get("devices", device_id)
        if device is None:
            raise KeyError(device_id)
        trust = trust_of(device)
        trust.update({"benched": False, "strikes": [], "score": 0.5})
        self._save(device_id, trust)
        self.state.event("wrong_answer", "%s is trusted again" % device.get("name"), device_id=device_id)
        self.scheduler.wake()
        return self.view(self.db.get("devices", device_id) or device)

    # ---------------------------------------------------------- sampling
    def unrepeatable(self) -> Dict[str, float]:
        value = self.db.get_setting(UNREPEATABLE_SETTING, {})
        return value if isinstance(value, dict) else {}

    def _mark_unrepeatable(self, key: str) -> None:
        known = self.unrepeatable()
        known[key] = now()
        self.db.set_setting(UNREPEATABLE_SETTING, known)

    def wanted(self, job: Dict[str, Any], plugin: Any) -> bool:
        """May tasks of this job be checked at all?"""
        options = job.get("options") or {}
        choice = options.get("verify")
        if choice is False or choice == 0:
            return False
        if choice is None and (plugin is None or not plugin.verifiable):
            return False
        if options.get("targets") == "all":
            return False
        key = options.get("_speed_key") or job.get("type")
        return str(key) not in self.unrepeatable()

    def share(self, job: Dict[str, Any], device: Dict[str, Any]) -> float:
        choice = (job.get("options") or {}).get("verify")
        if isinstance(choice, (int, float)) and not isinstance(choice, bool):
            base = max(0.0, min(1.0, float(choice)))
        else:
            base = float(self._setting("share", 0.05) or 0.0)
        if float(trust_of(device)["score"]) < SUSPECT_BELOW:
            base = max(base, float(self._setting("suspect_share", 0.5) or 0.0))
        return base

    @staticmethod
    def impl_of(task: Dict[str, Any], device: Dict[str, Any], said: Optional[str]) -> Optional[str]:
        impls = (task.get("payload") or {}).get("impls") or {}
        if said and said in impls:
            return str(said)
        caps = set(device.get("caps") or [])
        order = BROWSER_ORDER if device.get("platform") == "browser" else WORKER_ORDER
        for kind in order:
            if kind in impls and kind in caps:
                return kind
        return None

    def maybe_check(self, job: Dict[str, Any], plugin: Any, task: Dict[str, Any],
                    device: Dict[str, Any], body: Dict[str, Any], impl: Optional[str]) -> Optional[str]:
        """After a task finished well: run it again elsewhere?  Returns the
        check task's id when one was queued."""
        payload = task.get("payload") or {}
        if payload.get("service") or payload.get("device_id") or task.get("reused") or not impl:
            return None
        if not self.wanted(job, plugin):
            return None
        if self.random.random() >= self.share(job, device):
            return None
        digest = result_digest(body.get("result"), body.get("outputs"))
        self.db.update("tasks", {"id": task["id"]}, {"result_hash": digest, "impl": impl})
        return self._queue(job, task, impl, "check", [device["id"]])

    def _queue(self, job: Dict[str, Any], task: Dict[str, Any], impl: str, role: str,
               avoid: List[str]) -> Optional[str]:
        payload = dict(task.get("payload") or {})
        payload["impls"] = {impl: (payload.get("impls") or {})[impl]}
        payload["avoid"] = list(dict.fromkeys(avoid))
        payload["check_since"] = now()
        probe = {"payload": payload, "_job_options": job.get("options") or {}, "job_id": job["id"]}
        if not any(self.scheduler.eligible(probe, d) for d in self.scheduler.job_pool()):
            return None  # nobody else could run it
        row = {
            "id": new_id(), "job_id": job["id"], "idx": task["idx"], "payload": payload,
            "status": "queued", "device_id": None, "attempts": 0, "max_attempts": 2,
            "history": [], "fingerprint": task.get("fingerprint"), "role": role,
            "check_of": task["id"], "impl": impl,
        }
        self.db.insert("tasks", row)
        self.scheduler.wake()
        return row["id"]

    # ---------------------------------------------------------- results
    def role_done(self, job: Dict[str, Any], role_task: Dict[str, Any], device: Dict[str, Any],
                  body: Dict[str, Any]) -> str:
        """A check or decide task finished: compare and act.  Returns the outcome."""
        original = self.db.get("tasks", role_task.get("check_of") or "")
        digest = result_digest(body.get("result"), body.get("outputs"))
        self.db.update("tasks", {"id": role_task["id"]}, {"result_hash": digest})
        if original is None or original.get("status") != "done" or not original.get("result_hash"):
            return "ignored"
        if role_task.get("role") == "check":
            return self._compare(job, original, role_task, device, digest)
        return self._decide(job, original, role_task, device, digest)

    def _compare(self, job: Dict[str, Any], original: Dict[str, Any], check: Dict[str, Any],
                 device: Dict[str, Any], digest: str) -> str:
        if digest == original["result_hash"]:
            self.db.update("tasks", {"id": original["id"]}, {"verified": 1})
            self.agree(original.get("device_id"))
            self.agree(device["id"])
            return "agreed"
        names = self._names(original.get("device_id"), device["id"])
        third = self._queue(job, original, str(check.get("impl") or original.get("impl")), "decide",
                            [str(original.get("device_id")), device["id"]])
        if third is None:
            self._dispute(job, original, "%s and %s got different results and no third device "
                          "can decide; add a device or run the job again" % names)
            return "disputed"
        self.state.event(
            "task_checked", "%s and %s got different results for task %s; a third device decides"
            % (names + (original["id"],)), job_id=job["id"], task_id=original["id"])
        return "deciding"

    def _decide(self, job: Dict[str, Any], original: Dict[str, Any], decider: Dict[str, Any],
                device: Dict[str, Any], digest: str) -> str:
        check = self._check_of(original["id"])
        if check is None:
            return "ignored"
        if digest == original["result_hash"]:
            wrong_id, right = str(check.get("device_id")), [original.get("device_id"), device["id"]]
            self.db.update("tasks", {"id": original["id"]}, {"verified": 1})
        elif digest == check.get("result_hash"):
            wrong_id, right = str(original.get("device_id")), [check.get("device_id"), device["id"]]
            self._correct(job, original, check)
        else:
            key = str((job.get("options") or {}).get("_speed_key") or job.get("type"))
            self._mark_unrepeatable(key)
            self.db.update("tasks", {"id": original["id"]}, {"verified": -1})
            self.state.event(
                "work_not_repeatable",
                "%s: three devices got three different results, so this work is not repeatable "
                "(clock, randomness?); it is not checked any more and nobody is blamed"
                % job.get("name"), job_id=job["id"], task_id=original["id"])
            return "unrepeatable"
        for device_id in right:
            self.agree(device_id)
        self.convict(wrong_id, job, original)
        return "wrong:%s" % wrong_id

    def _correct(self, job: Dict[str, Any], original: Dict[str, Any], check: Dict[str, Any]) -> None:
        """The original device was wrong: its task takes the checked answer."""
        result = check.get("result")
        stored = result
        if self.state.results.enabled(job):
            stored = {"streamed": self.state.results.write_part(job, int(original["idx"]), result)}
        history = list(original.get("history") or [])
        history.append({"device_id": original.get("device_id"), "ts": now(),
                        "error": "wrong answer, replaced by the result of two other devices"})
        self.db.update("tasks", {"id": original["id"]}, {
            "result": stored, "outputs": check.get("outputs") or [], "verified": 1,
            "result_hash": check.get("result_hash"), "history": history,
        })
        if original.get("fingerprint") and self.state.cache.mode(job, self.state.plugins.get(job["type"])) != "off":
            self.state.cache.put(original["fingerprint"], "task", str(job.get("type")), result,
                                 check.get("outputs") or [], [check.get("device_id")], job["id"])
        self.state.publish_task(original["id"])

    def _dispute(self, job: Dict[str, Any], original: Dict[str, Any], message: str) -> None:
        self.db.update("tasks", {"id": original["id"]}, {"verified": -1})
        self.state.event("task_disputed", "%s: %s" % (job.get("name"), message),
                         job_id=job["id"], task_id=original["id"])
        self.state.notify("wrong_answer", "Results disagree in %s: %s" % (job.get("name"), message),
                          {"job_id": job["id"], "task_id": original["id"]})

    def _check_of(self, task_id: str) -> Optional[Dict[str, Any]]:
        rows = self.db.select("tasks", "WHERE check_of=? AND role='check' AND status='done' "
                              "ORDER BY finished_at DESC LIMIT 1", (task_id,))
        return rows[0] if rows else None

    def _names(self, *device_ids: Any) -> Tuple[str, ...]:
        out = []
        for device_id in device_ids:
            device = self.db.get("devices", str(device_id or "")) or {}
            out.append(str(device.get("name") or device_id))
        return tuple(out)

    def role_failed(self, job: Dict[str, Any], role_task: Dict[str, Any], reason: str) -> None:
        """A check could not run: no verdict, nobody blamed."""
        if role_task.get("role") == "decide":
            original = self.db.get("tasks", role_task.get("check_of") or "")
            if original is not None:
                self._dispute(job, original, "the deciding run failed (%s)" % reason)

    # ------------------------------------------------------- consequences
    def convict(self, device_id: str, job: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
        """``device_id`` gave a wrong answer: less trust, its recent work again."""
        outcome = self.wrong(device_id)
        name = self._names(device_id)[0]
        redo = self.recheck_recent(device_id, exclude=task["id"])
        message = "%s gave a wrong answer in %s" % (name, job.get("name"))
        if redo["tasks"]:
            message += "; its last %d task(s) in %d job(s) are being computed again" % (
                redo["tasks"], len(redo["jobs"]))
        if outcome["benched_now"]:
            message += "; it gave wrong answers %d times today, so it gets no more jobs until you " \
                       "press Trust again" % len(outcome["trust"]["strikes"])
        self.state.event("wrong_answer", message, device_id=device_id, job_id=job["id"],
                         task_id=task["id"], data=redo)
        self.state.notify("wrong_answer", message, {"device_id": device_id, "job_id": job["id"]})
        return redo

    def recheck_recent(self, device_id: str, exclude: Optional[str] = None) -> Dict[str, Any]:
        """Compute again what ``device_id`` finished lately for repeatable
        jobs; finished jobs open again and finish with the new results."""
        since = now() - float(self._setting("recheck_minutes", 60) or 0) * 60
        dropped = self.state.cache.forget_device(device_id, since)
        rows = self.db.select(
            "tasks", "WHERE device_id=? AND status='done' AND role IS NULL AND reused=0 "
            "AND finished_at>=? AND COALESCE(verified,0)<>1", (device_id, since))
        jobs: Dict[str, List[Dict[str, Any]]] = {}
        for task in rows:
            if task["id"] == exclude:
                continue
            jobs.setdefault(task["job_id"], []).append(task)
        redone = 0
        reopened: List[str] = []
        for job_id, tasks in jobs.items():
            job = self.db.get("jobs", job_id)
            plugin = self.state.plugins.get((job or {}).get("type") or "")
            if job is None or job["status"] not in ("running", "done") or not self.wanted(job, plugin):
                continue
            if job["status"] == "done":
                self._reopen(job)
                reopened.append(job_id)
            for task in tasks:
                self.scheduler.redo(task, avoid=device_id)
                redone += 1
            self.state.publish_job(job_id)
        if reopened:
            self.state.event("job_rechecked", "%d finished job(s) are computed again without %s"
                             % (len(reopened), self._names(device_id)[0]), device_id=device_id,
                             data={"jobs": reopened})
        self.scheduler.wake()
        return {"tasks": redone, "jobs": sorted(jobs), "reopened": reopened, "cache_dropped": dropped}

    def _reopen(self, job: Dict[str, Any]) -> None:
        options = dict(job.get("options") or {})
        options["_rechecked"] = int(options.get("_rechecked") or 0) + 1
        self.db.update("jobs", {"id": job["id"]}, {
            "status": "running", "finished": None, "result": None, "error": None, "options": options})

    # ----------------------------------------------------------- upkeep
    def expire(self) -> int:
        """Drop checks nobody could take in time; they never hold a job up long."""
        stamp = now()
        dropped = 0
        for task in self.db.select("tasks", "WHERE role IS NOT NULL AND status='queued'"):
            since = float((task.get("payload") or {}).get("check_since") or stamp)
            if stamp - since < WAIT_SECONDS:
                continue
            self.db.update("tasks", {"id": task["id"]}, {
                "status": "cancelled", "finished_at": stamp, "error": "no other device was free to check it"})
            job = self.db.get("jobs", task["job_id"]) or {"id": task["job_id"]}
            self.role_failed(job, task, "no device was free")
            self.state.publish_task(task["id"])
            dropped += 1
        if dropped:
            self.scheduler.wake()
        return dropped

    def summary(self, job_id: str) -> Dict[str, Any]:
        """Checks of one job, for its speed report."""
        rows = self.db.rows(
            "SELECT verified, COUNT(*) AS n FROM tasks WHERE job_id=? AND role IS NULL "
            "AND verified IS NOT NULL GROUP BY verified", (job_id,))
        counts = {int(r["verified"]): int(r["n"]) for r in rows}
        checks = int(self.db.scalar(
            "SELECT COUNT(*) FROM tasks WHERE job_id=? AND role='check' AND status='done'",
            (job_id,), default=0))
        corrected = int(self.db.scalar(
            "SELECT COUNT(*) FROM tasks WHERE job_id=? AND role='decide' AND status='done'",
            (job_id,), default=0))
        return {"checked": checks, "confirmed": counts.get(1, 0), "decided": corrected,
                "disputed": counts.get(-1, 0)}
