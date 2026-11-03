"""Map a Python function over 200 numbers on the cluster.

The simplest possible PhoneCluster program: the source of `square()` is
shipped to every worker with inspect.getsource, the items are split into
chunks of 20, and the results come back in item order.

    python examples/map_squares.py

Connection comes from PC_URL / PC_PIN or data/cli.json (see `pc login`).
"""

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk"))

from phonecluster import Cluster, ClusterError, JobFailed  # noqa: E402


def square(item, ctx):
    """Runs on the phones. `ctx.log()` shows up in `pc logs -f <job>`."""
    if item % 50 == 0:
        ctx.log("working on %d" % item)
    return item * item


def main():
    cluster = Cluster()
    print("cluster: %s" % cluster.url)

    job = cluster.map(square, list(range(1, 201)), chunk_size=20,
                      name="squares 1..200")
    print("submitted job %s, waiting..." % job.id)
    job.wait()

    results = job.result()
    print("got %d results" % len(results))
    print("first 10: %s" % results[:10])
    print("last 5:   %s" % results[-5:])
    assert results[9] == 100, "expected 10*10 == 100"

    for task in job.tasks():
        print("  task %-3s %-8s %-12s %.2fs"
              % (task.get("idx"), task.get("status"),
                 task.get("device_id") or "-", task.get("duration") or 0.0))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except JobFailed as exc:
        print("job failed: %s" % exc)
        sys.exit(1)
    except ClusterError as exc:
        print("cluster error: %s" % exc)
        sys.exit(1)
