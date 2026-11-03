"""Brute-force hash search across the cluster (built-in `hash_search` plugin).

Hunts for numbers whose SHA-256 (of "<salt><n>") starts with a hex prefix.
Every phone gets a slice of the search space; browser workers can join too
because the plugin ships both a Python and a JS implementation.

    python examples/hash_search.py [prefix] [count]
    python examples/hash_search.py 00000 2000000
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk"))

from phonecluster import Cluster, ClusterError, JobFailed  # noqa: E402


def main(argv):
    prefix = argv[0] if argv else "0000"
    count = int(argv[1]) if len(argv) > 1 else 1000000

    cluster = Cluster()
    print("searching %d candidates for sha256 starting with %r" % (count, prefix))

    started = time.time()
    job = cluster.submit("hash_search", {
        "algo": "sha256",
        "prefix": prefix,
        "start": 0,
        "count": count,
        "chunk_size": max(10000, count // 20),
        "salt": "phonecluster",
        "max_results": 20,
    }, name="hash_search %s" % prefix, timeout=900)
    print("job %s submitted" % job.id)

    while not job.refresh().done:
        print("  %s  %d/%d tasks" % (job.status, job.data.get("done_tasks") or 0,
                                     job.data.get("total_tasks") or 0))
        time.sleep(2.0)
    job.wait()

    elapsed = time.time() - started
    result = job.result() or {}
    matches = result.get("matches") or []
    searched = result.get("searched") or 0
    print("\nsearched %d candidates in %.1fs (%.0f/s) on %d tasks"
          % (searched, elapsed, searched / max(elapsed, 0.001),
             job.data.get("total_tasks") or 0))
    if not matches:
        print("no match for prefix %r - try a shorter prefix" % prefix)
    for match in matches:
        print("  n=%-12s %s" % (match.get("n"), match.get("hash")))
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
