"""Current pouch headspace summary for a packed blend lot."""


def lot_oxygen_pct(samples):
    return sum(row['oxygen_pct'] for row in samples) / len(samples)
