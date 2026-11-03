"""A two-stage pipeline: count primes, then square that count on the cluster.

The second job stays `waiting` until the first is `done`; the coordinator then
resolves the `$from_job` reference in its params (SPEC section 5) and runs it.

    python examples/pipeline_demo.py [start] [end]
"""

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk"))

from phonecluster import Cluster, ClusterError, JobFailed  # noqa: E402
from phonecluster.client import function_source  # noqa: E402


def square(item, ctx):
    """Runs on a worker: squares the prime count handed over by stage one."""
    ctx.log("squaring %r" % (item,))
    return item * item


def main(argv):
    start = int(argv[0]) if argv else 1
    end = int(argv[1]) if len(argv) > 1 else 200000

    cluster = Cluster()
    pipeline = cluster.pipeline([
        {
            "key": "primes",
            "type": "prime_range",
            "name": "count primes %d-%d" % (start, end),
            "params": {"start": start, "end": end,
                       "chunk_size": max(1000, (end - start) // 10)},
        },
        {
            "key": "squared",
            "type": "python_map",
            "name": "square the count",
            "depends_on": ["primes"],
            "params": {
                "code": function_source(square),
                "fn_name": "square",
                # resolved by the coordinator once "primes" is done
                "items": [{"$from_job": "primes", "path": "count"}],
                "chunk_size": 1,
            },
        },
    ], name="primes then squares")

    print("pipeline %s" % pipeline.id)
    for key, job in sorted(pipeline.jobs.items()):
        print("  %-8s -> %s" % (key, job.id))

    pipeline.wait()
    results = pipeline.results()
    count = (results["primes"] or {}).get("count")
    squared = (results["squared"] or [None])[0]
    print("\nprimes in [%d, %d): %s" % (start, end, count))
    print("that count squared:  %s" % squared)
    if count is not None and squared is not None:
        assert squared == count * count, "pipeline maths went wrong"
        print("checked: %d * %d == %d" % (count, count, squared))
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
