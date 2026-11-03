"""Store a file across the phones' spare space, read it back, verify sha256.

The coordinator splits the file into chunks, replicates them onto online
devices that report the `storage` capability, and streams them back on
demand. Nothing is kept on the PC except a small LRU cache.

    python examples/storage_demo.py [file] [--replication 2]

With no file it generates 3 MB of random data in the system temp dir.
"""

import hashlib
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk"))

from phonecluster import Cluster, ClusterError  # noqa: E402


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def main(argv):
    args = list(argv)
    replication = None
    if "--replication" in args:
        index = args.index("--replication")
        replication = int(args[index + 1])
        del args[index:index + 2]

    path = args[0] if args else None
    temporary = False
    if not path:
        path = os.path.join(tempfile.gettempdir(), "phonecluster_blob.bin")
        with open(path, "wb") as handle:
            handle.write(os.urandom(3 * 1024 * 1024))
        temporary = True
        print("no file given, generated %s (3 MB)" % path)

    original = sha256_of(path)
    print("%s\n  size   %d bytes\n  sha256 %s"
          % (path, os.path.getsize(path), original))

    cluster = Cluster()
    holders = [d for d in cluster.devices()
               if d.get("status") == "online" and "storage" in (d.get("caps") or [])]
    print("\n%d online device(s) can hold chunks" % len(holders))
    if not holders:
        print("(the coordinator will refuse or degrade the blob without them)")

    print("uploading into the pool...")
    blob_id = cluster.storage.put(path, replication=replication)
    print("blob %s" % blob_id)

    for _ in range(30):
        status = cluster.storage.status(blob_id)
        state = status.get("status")
        print("  status: %s" % state)
        if state in ("ready", "degraded", "lost"):
            break
        time.sleep(1.0)

    dest = path + ".restored"
    print("\ndownloading it back to %s" % dest)
    cluster.storage.get(blob_id, dest)
    restored = sha256_of(dest)
    print("  sha256 %s" % restored)

    if restored == original:
        print("\nOK - the round trip is byte-identical")
        code = 0
    else:
        print("\nMISMATCH - the blob came back different!")
        code = 1

    cluster.storage.delete(blob_id)
    print("deleted blob %s" % blob_id)
    os.remove(dest)
    if temporary:
        os.remove(path)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ClusterError as exc:
        print("cluster error: %s" % exc)
        sys.exit(1)
