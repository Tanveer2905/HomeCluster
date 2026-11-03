"""Create an hourly benchmark schedule, list the schedules, then delete it.

Schedules submit a job spec on a 5-field cron (min hour dom mon dow),
evaluated in local time by the coordinator.

    python examples/schedule_demo.py            # create, list, run once, delete
    python examples/schedule_demo.py --keep     # leave it in place
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk"))

from phonecluster import Cluster, ClusterError  # noqa: E402


def show(cluster):
    rows = cluster.schedules.list()
    print("%-14s %-24s %-14s %-4s %s" % ("ID", "NAME", "CRON", "ON", "NEXT RUN"))
    for row in rows:
        next_run = row.get("next_run")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(next_run)) \
            if next_run else "-"
        print("%-14s %-24s %-14s %-4s %s"
              % (row.get("id"), (row.get("name") or "")[:24], row.get("cron"),
                 "yes" if row.get("enabled") else "no", when))
    if not rows:
        print("(no schedules)")
    return rows


def main(argv):
    keep = "--keep" in argv
    cluster = Cluster()

    job_spec = {
        "name": "hourly benchmark",
        "type": "benchmark",
        "params": {},
        "options": {"targets": "all", "timeout": 120},
    }
    created = cluster.schedules.create("hourly benchmark", "0 * * * *", job_spec)
    schedule_id = created.get("id") if isinstance(created, dict) else created
    print("created schedule %s (runs at minute 0 of every hour)\n" % schedule_id)

    show(cluster)

    print("\nfiring it once right now...")
    fired = cluster.schedules.run(schedule_id)
    print("  -> %s" % fired)

    if keep:
        print("\nkeeping schedule %s (delete it with: pc schedules rm %s)"
              % (schedule_id, schedule_id))
        return 0

    cluster.schedules.delete(schedule_id)
    print("\ndeleted schedule %s\n" % schedule_id)
    show(cluster)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ClusterError as exc:
        print("cluster error: %s" % exc)
        sys.exit(1)
