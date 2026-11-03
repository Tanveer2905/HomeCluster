"""Count the primes in a range using the built-in `prime_range` plugin.

    python examples/prime_range.py [start] [end] [--list]
    python examples/prime_range.py 1 2000000
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk"))

from phonecluster import Cluster, ClusterError, JobFailed  # noqa: E402


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    want_list = "--list" in argv
    start = int(args[0]) if args else 1
    end = int(args[1]) if len(args) > 1 else 1000000

    cluster = Cluster()
    chunk = max(1000, (end - start) // 20)
    print("counting primes in [%d, %d) with chunks of %d" % (start, end, chunk))

    began = time.time()
    job = cluster.submit("prime_range", {
        "start": start, "end": end, "chunk_size": chunk,
        "return_list": want_list,
    }, name="primes %d-%d" % (start, end), timeout=900).wait()
    elapsed = time.time() - began

    result = job.result() or {}
    print("\n%d primes in %.1fs across %d tasks"
          % (result.get("count") or 0, elapsed, job.data.get("total_tasks") or 0))
    primes = result.get("primes")
    if primes:
        print("first 20: %s" % primes[:20])
        print("last 5:   %s" % primes[-5:])
    else:
        print("(pass --list to get the primes themselves)")
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
