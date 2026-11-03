"""Run one shell command on every online device and print each answer.

`targets: "all"` fans the job out to one task per online device. Windows
workers run `cmd_windows` instead of `cmd`, so the same job works on the PC
and on the phones.

    python examples/shell_all.py
    python examples/shell_all.py "df -h" "wmic logicaldisk get size,freespace"
"""

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk"))

from phonecluster import Cluster, ClusterError, JobFailed  # noqa: E402


def main(argv):
    command = argv[0] if argv else "uname -a"
    windows_command = argv[1] if len(argv) > 1 else "ver"

    cluster = Cluster()
    names = {}
    online = 0
    for device in cluster.devices():
        names[device.get("id")] = "%s (%s)" % (device.get("name"),
                                               device.get("platform"))
        if device.get("status") == "online":
            online += 1
    if not online:
        print("no devices are online - start a worker first (pc join)")
        return 1
    print("running %r on %d device(s)\n" % (command, online))

    job = cluster.shell(command, targets="all", cmd_windows=windows_command,
                        timeout=60).wait()

    result = job.result() or {}
    if not isinstance(result, dict):
        print(result)
        return 0
    for device_id, output in sorted(result.items()):
        print("=== %s [%s] ===" % (names.get(device_id, device_id), device_id))
        if isinstance(output, dict):
            print("exit code: %s" % output.get("exit_code"))
            text = (output.get("stdout") or "").rstrip()
            if text:
                print(text)
            error = (output.get("stderr") or "").rstrip()
            if error:
                print("stderr: %s" % error)
        else:
            print(output)
        print("")
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
