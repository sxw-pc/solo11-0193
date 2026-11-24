"""Current remittance wrongly treats authorization as settled funds."""
from .auction_award import winner


def releasable(lot, bids, payment):
    award = winner(lot, bids)
    return min(award['amount'], payment['authorized']) if award else 0
