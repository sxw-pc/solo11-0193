"""Current plot-level pest pressure from trap counts."""


def pressure(plot):
    counts = plot['trap_counts']
    return sum(counts) / len(counts)
