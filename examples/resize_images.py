"""Resize a folder of images to 800px wide on whichever device has ffmpeg.

Uploads every image, submits one `media` job (one task per file, requires the
`ffmpeg` capability), then downloads the resized outputs.

    python examples/resize_images.py <folder> [out-folder] [--width 800]
"""

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk"))

from phonecluster import Cluster, ClusterError, JobFailed  # noqa: E402

EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")


def main(argv):
    args = list(argv)
    width = 800
    if "--width" in args:
        index = args.index("--width")
        width = int(args[index + 1])
        del args[index:index + 2]
    if not args:
        print(__doc__.strip())
        return 1
    folder = args[0]
    out_dir = args[1] if len(args) > 1 else os.path.join(folder, "resized")

    images = sorted(name for name in os.listdir(folder)
                    if name.lower().endswith(EXTENSIONS))
    if not images:
        print("no images in %s" % folder)
        return 1

    cluster = Cluster()
    if not any("ffmpeg" in (d.get("caps") or []) and d.get("status") == "online"
               for d in cluster.devices()):
        print("warning: no online device reports the 'ffmpeg' capability;")
        print("         run scripts/get-ffmpeg.ps1 and start the PC worker.")

    file_ids = []
    for name in images:
        file_id = cluster.files.upload(os.path.join(folder, name))
        file_ids.append(file_id)
        print("uploaded %-30s -> %s" % (name, file_id))

    job = cluster.submit("media", {
        "op": "resize", "inputs": file_ids, "width": width, "quality": 3,
    }, name="resize %d images" % len(file_ids), requires=["ffmpeg"], timeout=600)
    print("\njob %s running on %d file(s)..." % (job.id, len(file_ids)))
    job.wait()

    written = job.download_outputs(out_dir)
    print("\ndownloaded %d file(s) to %s" % (len(written), out_dir))
    for path in written:
        print("  %s (%d bytes)" % (os.path.basename(path), os.path.getsize(path)))

    for file_id in file_ids:
        cluster.files.delete(file_id)
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
