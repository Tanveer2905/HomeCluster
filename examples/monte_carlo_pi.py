"""Estimate pi with a Monte Carlo simulation spread over the phones.

Uses the built-in `monte_carlo_pi` plugin, which has both a Python and a JS
implementation, so Termux workers and browser tabs both contribute.

    python examples/monte_carlo_pi.py [samples] [chunks]
    python examples/monte_carlo_pi.py 20000000 40
"""

import math
import os
import sys
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk"))

from phonecluster import Cluster, ClusterError, JobFailed  # noqa: E402


def main(argv):
    samples = int(argv[0]) if argv else 4000000
    chunks = int(argv[1]) if len(argv) > 1 else 20

    cluster = Cluster()
    online = [d for d in cluster.devices() if d.get("status") == "online"]
    print("%d device(s) online, throwing %d darts in %d chunks"
          % (len(online), samples, chunks))

    started = time.time()
    job = cluster.submit("monte_carlo_pi", {"samples": samples, "chunks": chunks},
                         name="pi %d" % samples, timeout=900).wait()
    elapsed = time.time() - started

    result = job.result() or {}
    estimate = result.get("pi")
    print("\npi ~= %.6f   (error %.2e)" % (estimate, abs(estimate - math.pi)))
    print("inside %s of %s samples in %.1fs -> %.0f samples/s"
          % (result.get("inside"), result.get("samples"), elapsed,
             (result.get("samples") or 0) / max(elapsed, 0.001)))

    by_device = {}
    for task in job.tasks():
        by_device[task.get("device_id")] = by_device.get(task.get("device_id"), 0) + 1
    for device_id, count in sorted(by_device.items(), key=lambda kv: -kv[1]):
        print("  %-14s %d chunk(s)" % (device_id, count))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except JobFailed as exc:
        print("job failed: %s" % exc)
        sys.exit(1)
    except ClusterError as exc:
        print("cluster error: %s" % exc)
        sys.exit(1)
