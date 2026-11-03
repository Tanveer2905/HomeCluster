"""Take a photo (or any Termux:API sensor reading) on every phone at once.

Needs Termux:API installed on the phones (`pkg install termux-api` plus the
Termux:API app); those devices report the `termux_api` capability.

    python examples/sensor_snapshot.py                 # photo from every phone
    python examples/sensor_snapshot.py battery
    python examples/sensor_snapshot.py location
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk"))

from phonecluster import Cluster, ClusterError, JobFailed  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")


def main(argv):
    sensor = argv[0] if argv else "photo"

    cluster = Cluster()
    phones = [d for d in cluster.devices()
              if d.get("status") == "online"
              and "termux_api" in (d.get("caps") or [])]
    if not phones:
        print("no online device has the 'termux_api' capability.")
        print("on each phone: pkg install termux-api (and install the Termux:API app)")
        return 1
    names = dict((d["id"], d.get("name")) for d in phones)
    print("asking %d phone(s) for %r: %s"
          % (len(phones), sensor, ", ".join(str(n) for n in names.values())))

    params = {"sensor": sensor, "duration": 5, "camera": 0}
    job = cluster.submit("sensor", params, name="sensor %s" % sensor,
                         targets=[d["id"] for d in phones],
                         requires=["termux_api"], timeout=120).wait()

    result = job.result()
    print("\nresult:")
    print(json.dumps(result, indent=2, default=str)[:2000])

    outputs = job.outputs()
    if outputs:
        written = job.download_outputs(OUT_DIR)
        print("\ndownloaded %d file(s) to %s" % (len(written), OUT_DIR))
        for path in written:
            print("  %s (%d bytes)" % (os.path.basename(path),
                                       os.path.getsize(path)))
    else:
        print("\n(this sensor produced no files, only the JSON above)")
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
