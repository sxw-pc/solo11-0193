"""Cross-ledger projection from immutable evidence."""

from . import garden, leaf, blend


def calculate(events):
    certs,pickers,garden_results=garden.decide(events)
    leaf_results=leaf.decide(events,certs,pickers)
    blend_results=blend.decide(events,leaf_results)
    return {**garden_results,**leaf_results,**blend_results}
