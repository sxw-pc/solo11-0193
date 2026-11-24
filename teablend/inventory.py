"""Read a branch's last synchronized packed-tea stock."""

def available(snapshot, label):
    return snapshot.get(label, 0)
