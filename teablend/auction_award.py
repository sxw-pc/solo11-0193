"""Simple sealed-bid award on a listed tea lot."""


def winner(lot, bids):
    eligible = [b for b in bids if b['lot'] == lot['id'] and b['amount'] >= lot['reserve_price']]
    return max(eligible, key=lambda b: (b['amount'], -b['submitted_minute'])) if eligible else None
