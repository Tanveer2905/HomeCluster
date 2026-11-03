"""Second step of the primes pipeline: turn the count into a sentence."""


def describe(item, ctx):
    count = int(item)
    ctx.log("received count %d from the first step" % count)
    return "there are %d primes below two million" % count
