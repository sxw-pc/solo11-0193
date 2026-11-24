"""Sealed-bid auction award for a listed tea lot.

Only a bid that names the lot and clears the reserve price can win; ties
break toward the earlier submitted minute. Awarding a winner never moves
money by itself -- bank capture and escrow release live in
``escrow_capture`` and ``escrow_remit``.
"""


def winner(lot, bids):
    """Return the winning bid dict, or ``None`` when nothing clears reserve."""
    eligible = [b for b in bids
                if b['lot'] == lot['id'] and b['amount'] >= lot['reserve_price']]
    if not eligible:
        return None
    return max(eligible, key=lambda b: (b['amount'], -b['submitted_minute']))


def award(lot, bids):
    """Return a versioned award record, or ``None`` when there is no winner.

    The record pins the winning buyer so bank postings and delivery
    signatures can later be matched to the actual award.
    """
    bid = winner(lot, bids)
    if bid is None:
        return None
    return {
        'version': 1,
        'lot': lot['id'],
        'seller': lot['seller'],
        'lot_kg': lot['kg'],
        'winning_bid': bid['id'],
        'buyer': bid['buyer'],
        'award_amount': bid['amount'],
        'reserve_price': lot['reserve_price'],
    }
