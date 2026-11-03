"""Distributed word count: upload a text file, map over line ranges, reduce here.

Each task downloads the same input file (the coordinator's content-addressed
store hands it to the worker) and counts words in its own slice, so nothing
big travels through the job parameters.

    python examples/wordcount.py [text-file] [--top 20]

With no argument it generates a small sample file in the system temp dir.
"""

import collections
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk"))

from phonecluster import Cluster, ClusterError, JobFailed  # noqa: E402

SAMPLE = """the quick brown fox jumps over the lazy dog
the dog barks and the fox runs away
a cluster of old phones counts the words
the phones are slow but there are many of them
""" * 200


def count_words(item, ctx):
    """item = [start_line, line_count]; returns {word: count} for that slice."""
    start, count = item
    counts = {}
    with open(ctx.inputs[0], "r", errors="replace") as handle:
        for number, line in enumerate(handle):
            if number < start:
                continue
            if number >= start + count:
                break
            for word in line.lower().split():
                word = word.strip(".,!?;:\"'()[]{}")
                if word:
                    counts[word] = counts.get(word, 0) + 1
    ctx.log("lines %d..%d -> %d distinct words" % (start, start + count, len(counts)))
    return counts


def main(argv):
    path = None
    top = 20
    args = list(argv)
    if "--top" in args:
        index = args.index("--top")
        top = int(args[index + 1])
        del args[index:index + 2]
    if args:
        path = args[0]
    if not path:
        path = os.path.join(tempfile.gettempdir(), "phonecluster_wordcount.txt")
        with open(path, "w") as handle:
            handle.write(SAMPLE)
        print("no file given, wrote a sample to %s" % path)

    with open(path, "r", errors="replace") as handle:
        total_lines = sum(1 for _ in handle)
    print("%s: %d lines" % (path, total_lines))

    cluster = Cluster()
    file_id = cluster.files.upload(path)
    print("uploaded as %s" % file_id)

    per_task = max(1, total_lines // 8)
    ranges = [[start, per_task] for start in range(0, total_lines, per_task)]
    job = cluster.map(count_words, ranges, chunk_size=1, inputs=[file_id],
                      name="wordcount %s" % os.path.basename(path))
    print("job %s: %d slices" % (job.id, len(ranges)))
    job.wait()

    # reduce client-side: the job result is one dict per item, in item order
    totals = collections.Counter()
    for partial in job.result():
        totals.update(partial or {})
    print("\n%d distinct words, %d total\n" % (len(totals), sum(totals.values())))
    for word, count in totals.most_common(top):
        print("%8d  %s" % (count, word))

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
