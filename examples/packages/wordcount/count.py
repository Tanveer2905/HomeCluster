"""Task code for the wordcount package.

Each item is the index of one input file. ``ctx.inputs`` lists the local
paths of every file the job's ``inputs`` param referenced, in order.
"""

import collections
import re


def count_words(item, ctx):
    path = ctx.inputs[int(item)]
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    words = re.findall(r"[a-zA-Z']+", text.lower())
    top = collections.Counter(words).most_common(5)
    ctx.log("%s: %d words" % (path, len(words)))
    return {"file": int(item), "words": len(words), "top": top}
