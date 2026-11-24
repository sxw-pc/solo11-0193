"""Consumer-valid package life, physical repack identity and channel correction.

Verification map (the five required themes):

1. ``test_01_seed_mean_misplaces_lot_into_wholesale`` -- the seed arithmetic
   mean / nominal-month chain really does say wholesale for BL-1, while the
   corrected per-pouch engine blocks that release.
2. ``test_02_two_legal_routes_a_full_repack_and_b_short_local`` -- route A
   (all 100 nitrogen repacks -> <=10 months -> wholesale only with >=6 at
   arrival) and route B (no repack -> <=2 months -> local only with >=1).
3. ``test_03_retest_identity_closed_loop_and_no_lot_release`` -- per-pouch
   unpack/N2/reseal/retest/new-code closure, old vs new code exclusivity,
   single-pouch retest never releases the whole case.
4. ``test_04_late_heat_log_retains_history_and_overturns`` -- heat history
   survives repack and a late 28 C log cuts the term / kills evidence.
5. ``test_05_channel_revocation_and_customer_correction`` -- sent wholesale
   promises overturned by late heat / high-O2 retest / stranding produce
   revocation plus customer correction evidence, and full inspection of
   more leaking pouches recomputes by physical code.

Run: python -B -m pytest -q -p no:cacheprovider
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from teablend.seal_atmosphere import (
    PackageLot,
    ReadingValidationError,
    lot_oxygen_pct,
    normalize_reading,
    oxygen_upper_pct,
    sampling_design,
    untested_oxygen_bound,
)
from teablend.shelf_life import assess_lot, months, remaining_months
from teablend.ship_decision import (
    ChannelCommitmentError,
    channel,
    commit_wholesale,
    deliver_correction,
    evaluate,
    fulfill_order,
    legal_routes,
    reconcile_commitments,
    record_commitment,
    route_a,
    route_b,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "package-life.json"

PACKED_AT = datetime(2026, 11, 25, 10, 0)
HEAT_END = datetime(2026, 11, 28, 10, 0)
REPACK_AT = datetime(2026, 11, 28, 14, 0)
LOCAL_ARRIVAL = datetime(2026, 11, 29, 10, 0)      # 4 elapsed days, route B fine
WHOLESALE_ARRIVAL = datetime(2026, 12, 5, 14, 0)   # 7 days after repack
COMMIT_AT = datetime(2026, 11, 28, 15, 0)
LATE_HEAT_ENTRY = {
    "start": "2026-11-28T10:00:00",
    "end": "2026-11-30T10:00:00",
    "celsius": 28,
}


@pytest.fixture()
def package_life():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Theme 1: seed mis-placement into wholesale
# ---------------------------------------------------------------------------

def test_01_seed_mean_misplaces_lot_into_wholesale(package_life):
    samples = package_life["headspace_samples"]

    # The exact seed characterisation: mean 2.37 %, nominal 12 months,
    # naive channel wholesale; all-5.0 flips nominal to 3 / local.
    assert round(lot_oxygen_pct(samples), 2) == 2.37
    assert months(samples) == 12
    assert channel(samples) == "wholesale"
    high = [{**row, "oxygen_pct": 5.0} for row in samples]
    assert months(high) == 3
    assert channel(high) == "local"

    # Every reading now carries pouch id, instrument error and sampling range.
    for row in samples:
        assert row["pouch"]
        assert row["instrument_error_pct"] >= 0
        assert row["sampling_range"]

    # The 4.8 % pouch is not averaged away: its conservative upper bound is
    # 5.0 % (4.8 + 0.2 instrument error) and stays identifiable as PK-03.
    uppers = {row["pouch"]: oxygen_upper_pct(row) for row in samples}
    assert uppers["PK-03"] == pytest.approx(5.0)
    assert max(uppers.values()) == 5.0

    design = sampling_design(package_life)
    assert untested_oxygen_bound(samples, design) == pytest.approx(5.0)

    # The corrected engine refuses wholesale for the untouched seed case.
    decision = evaluate(package_life, arrival_at=LOCAL_ARRIVAL)
    assert decision["channel"] == "local"
    assert decision["wholesale"]["allowed"] is False
    assert decision["wholesale"]["eligible_units"] == 0
    assert decision["local"]["allowed"] is True
    assert decision["oxygen_upper_pct"] == pytest.approx(5.0)
    assert decision["heat_days_at_28c"] == pytest.approx(3.0)
    assert decision["granted_months"] == 2
    # The 12-month nominal seed number is retained for traceability only and
    # is never used as the customer arrival remaining life.
    assert decision["seed_nominal_months"] == 12
    assert decision["nominal_months"] == 12
    assert decision["remaining_months"] < 2

    # Dict dispatch compares against the corrected granted / channel values.
    assert months(package_life) == 2
    assert channel(package_life) == "local"

    # A corrected wholesale commitment is refused rather than quietly stored.
    lot = PackageLot(package_life)
    with pytest.raises(ChannelCommitmentError):
        commit_wholesale(lot, customer="Wholesale Buyer", arrival_at=WHOLESALE_ARRIVAL)
    assert lot.commitments == []

    # But an already-sent legacy promise can be recorded and must then be
    # revoked with customer correction evidence (theme 5 below).
    legacy = record_commitment(
        lot, customer="Legacy Wholesale", customer_order="OLD-1",
        at=HEAT_END, arrival_at=LOCAL_ARRIVAL,
    )
    assert legacy["status"] == "sent"
    assert legacy["forced"] is True
    corrections = reconcile_commitments(lot, at=HEAT_END, reason="seed_mean_misplacement")
    assert len(corrections) == 1
    assert corrections[0]["reason"] == "seed_mean_misplacement"
    assert lot.commitments[0]["status"] == "revoked"


@pytest.mark.parametrize(
    "row",
    [
        {"oxygen_pct": 1.2, "instrument_error_pct": 0.2, "sampling_range": "x"},
        {"pouch": "PK-09", "oxygen_pct": 1.2, "sampling_range": "x"},
        {"pouch": "PK-09", "oxygen_pct": 1.2, "instrument_error_pct": 0.2},
    ],
)
def test_01b_reading_requires_code_error_and_range(row):
    with pytest.raises(ReadingValidationError):
        normalize_reading(row)


# ---------------------------------------------------------------------------
# Theme 2: the two legal routes
# ---------------------------------------------------------------------------

def test_02_route_b_no_repack_short_life_local_only(package_life):
    plan = route_b(package_life, elapsed_days=4)
    assert plan["route"] == "B"
    assert plan["repack"] is False
    assert plan["codes_preserved"] is True
    assert plan["max_months"] == 2
    assert plan["granted_months"] == 2
    assert plan["remaining_months"] == pytest.approx(2 - 4 / 30)
    assert plan["channel"] == "local"
    assert plan["legal"] is True
    assert plan["wholesale"]["allowed"] is False
    assert plan["local"]["eligible_units"] == 100
    # Original codes are retained for every physical pouch.
    assert plan["original_codes"] == [f"PK-{i:02d}" if i < 100 else "PK-100" for i in range(1, 101)]
    assert plan["new_codes"] == []

    # Local arrival threshold is >= 1 month: 30 elapsed days is inclusive,
    # day 31 fails. Wholesale (>=6) is impossible on the as-packed evidence.
    assert route_b(package_life, elapsed_days=30)["local"]["allowed"] is True
    assert route_b(package_life, elapsed_days=31)["local"]["allowed"] is False
    assert route_b(package_life, elapsed_days=29)["channel"] == "local"

    # Fast local arrival on the real timeline also works.
    timed = route_b(package_life, arrival_at=LOCAL_ARRIVAL)
    assert timed["local"]["allowed"] is True
    assert timed["remaining_months"] == pytest.approx(2 - 4 / 30, abs=1e-9)


def test_02_route_a_full_repack_grants_ten_and_wholesale(package_life):
    lot = PackageLot(package_life)
    plan = route_a(lot, at=REPACK_AT, arrival_at=WHOLESALE_ARRIVAL)

    assert plan["route"] == "A"
    assert plan["all_pouches_repacked"] is True
    assert plan["repacked_count"] == 100
    assert plan["repack_capacity_consumed"] == 100
    assert len(plan["new_codes"]) == 100
    assert plan["max_months"] == 10
    assert plan["remaining_months"] == pytest.approx(10 - 7 / 30, abs=1e-9)
    assert plan["wholesale_recommit_allowed"] is True
    assert plan["channel"] == "wholesale"
    assert plan["wholesale"]["allowed"] is True
    assert plan["wholesale"]["eligible_units"] == 100
    assert plan["heat_history_retained"] is True
    # The original three hot days still sit on the ledger after repack.
    assert plan["heat_days_at_28c"] == pytest.approx(3.0)

    # Wholesale needs six at arrival: 120 post-repack days is the inclusive
    # boundary, 121 days fails and routes nowhere wholesale.
    assert route_a(lot, execute=False, elapsed_days=120)["wholesale"]["allowed"] is True
    late = route_a(lot, execute=False, elapsed_days=121)
    assert late["wholesale"]["allowed"] is False
    assert late["wholesale_recommit_allowed"] is False
    assert 5 < late["remaining_months"] < 6

    # Route A supports a real wholesale commitment afterwards.
    commitment = commit_wholesale(
        lot, customer="Serious Wholesale", customer_order="PO-A",
        at=COMMIT_AT, arrival_at=WHOLESALE_ARRIVAL,
    )
    assert commitment["status"] == "sent"
    assert commitment["promised_remaining_months"] >= 6


def test_02_remaining_life_is_monotone_with_time(package_life):
    lot = PackageLot(package_life)
    route_b_values = [evaluate(lot, elapsed_days=d)["remaining_months"] for d in (0, 3, 8, 20, 30, 45, 80)]
    for earlier, later in zip(route_b_values, route_b_values[1:]):
        assert earlier >= later
    assert route_b_values[-1] == 0

    route_a(lot, at=REPACK_AT)
    a_values = [evaluate(lot, elapsed_days=d)["remaining_months"] for d in (0, 30, 90, 119, 120, 121, 400)]
    for earlier, later in zip(a_values, a_values[1:]):
        assert earlier >= later
    assert a_values[-1] == 0

    # The pure helper is monotone too.
    assert remaining_months(2, storage_days=3, transport_days=2) < remaining_months(2)


def test_02_same_box_supports_two_coexisting_legal_routes(package_life):
    routes = legal_routes(package_life, at=REPACK_AT, elapsed_days=7)
    # Route A spends repack capacity and earns new codes + wholesale.
    assert routes["A"]["channel"] == "wholesale"
    assert routes["A"]["repacked_count"] == 100
    assert routes["A"]["max_months"] == 10
    # Route B keeps original codes and stays local / short life.
    assert routes["B"]["channel"] == "local"
    assert routes["B"]["max_months"] == 2
    assert routes["B"]["repack"] is False
    # The route projection must not mutate the caller's fixture into a
    # repacked state: a fresh assessment still sees zero repacks.
    fresh = PackageLot(package_life)
    assert fresh.passed_repack_count == 0
    assert evaluate(fresh, elapsed_days=4)["channel"] == "local"


# ---------------------------------------------------------------------------
# Theme 3: retest identity and closed loop
# ---------------------------------------------------------------------------

def test_03_single_pouch_closure_identity_and_no_lot_release(package_life):
    lot = PackageLot(package_life)
    record = lot.repack(
        "PK-03", at=REPACK_AT, oxygen_pct=1.8, instrument_error_pct=0.2,
        operator="op-7",
    )

    # Real per-bag closed loop: unpack, nitrogen, reseal, retest, new code.
    assert record["passed"] is True
    assert record["closed_loop"] is True
    assert record["original_code"] == "PK-03"
    assert record["new_code"] == "PK-03-R1"
    for step in ("unpacked", "nitrogen_filled", "resealed", "retested"):
        assert record["steps"][step]["done"] is True
        assert record["steps"][step]["at"]
    assert record["retest"]["oxygen_upper_pct"] == pytest.approx(2.0)
    assert record["heat_history_retained"] is True

    # Old code and new code are never simultaneously sellable.
    assert lot.is_sellable("PK-03-R1") is True
    assert lot.is_sellable("PK-03") is False
    assert "PK-03" not in lot.sellable_codes()
    assert "PK-03-R1" in lot.sellable_codes()
    assert "PK-03" in lot.retired_codes()
    # Both identifiers resolve to the same physical pouch.
    assert lot.resolve("PK-03") is lot.resolve("PK-03-R1")

    # One closed pouch does not release the whole 100-pouch case.
    decision = evaluate(lot, arrival_at=WHOLESALE_ARRIVAL)
    assert decision["repacked_count"] == 1
    assert decision["all_repacked"] is False
    assert decision["wholesale"]["allowed"] is False
    assert decision["wholesale"]["eligible_units"] == 1
    # Mixed processes are tracked per physical pouch.
    assert decision["pouches"]["PK-03"]["granted_months"] == 10
    assert decision["pouches"]["PK-01"]["granted_months"] == 2
    # Untested PK-50 still carries the design bound, not an averaged value.
    assert decision["pouches"]["PK-50"]["oxygen_upper_pct"] == pytest.approx(5.0)


def test_03_single_seal_retest_without_repack_does_not_release_box(package_life):
    lot = PackageLot(package_life)
    lot.add_reading({
        "pouch": "PK-03", "oxygen_pct": 1.6, "instrument_error_pct": 0.2,
        "sampling_range": "PK-03 seal integrity retest, full headspace",
        "measured_at": "2026-11-28T11:00:00",
    })
    decision = evaluate(lot)
    # Better reading on one sealed pouch changes that pouch only; there is no
    # nitrogen repack process, so the lot stays on the <=2 month evidence.
    assert decision["all_repacked"] is False
    assert decision["granted_months"] == 2
    assert decision["wholesale"]["allowed"] is False
    assert decision["channel"] == "local"
    # Other pouches remain at the conservative sampling-design bound.
    assert decision["pouches"]["PK-04"]["oxygen_upper_pct"] == pytest.approx(5.0)


def test_03_repack_all_replaces_every_sellable_code(package_life):
    lot = PackageLot(package_life)
    lot.repack_all(at=REPACK_AT, oxygen_pct=1.8, instrument_error_pct=0.2)

    assert len(lot.sellable_codes()) == 100
    original_codes = set(lot.codes)
    sellable = set(lot.sellable_codes())
    assert original_codes.isdisjoint(sellable)
    assert all(code.endswith("-R1") for code in sellable)
    # No identity conflict and no original/new overlap.
    lot._assert_identity_invariant()
    assert lot.all_repacked is True


def test_03_failed_retest_quarantines_new_code(package_life):
    lot = PackageLot(package_life)
    record = lot.repack("PK-01", at=REPACK_AT, oxygen_pct=2.4, instrument_error_pct=0.2)
    assert record["passed"] is False
    assert record["retest_passed"] is False
    assert lot.is_sellable("PK-01") is False
    assert lot.is_sellable("PK-01-R1") is False
    assert lot.resolve("PK-01")["status"] == "quarantined_repack_failed"
    decision = evaluate(lot)
    assert decision["pouches"]["PK-01"]["blocked"] is True
    assert "PK-01" not in decision["local"]["eligible_codes"]


# ---------------------------------------------------------------------------
# Theme 4: late heat log
# ---------------------------------------------------------------------------

def test_04_repack_keeps_heat_history_and_late_log_cuts_term(package_life):
    lot = PackageLot(package_life)
    route_a(lot, at=REPACK_AT, arrival_at=WHOLESALE_ARRIVAL)
    assert assess_lot(lot)["heat_days_at_28c"] == pytest.approx(3.0)

    # A late log adds two more real 28 C days: cumulative 5 > evidence 3,
    # so even the nitrogen evidence no longer applies and term collapses.
    lot.add_heat_log(LATE_HEAT_ENTRY)
    reassessed = assess_lot(lot, arrival_at=WHOLESALE_ARRIVAL)
    assert reassessed["heat_days_at_28c"] == pytest.approx(5.0)
    assert reassessed["granted_months"] == 0
    assert reassessed["remaining_months"] == 0
    assert any("exceeds" in reason for reason in reassessed["gate_reasons"])
    decision = evaluate(lot, arrival_at=WHOLESALE_ARRIVAL)
    assert decision["wholesale"]["allowed"] is False
    assert decision["local"]["allowed"] is False
    assert decision["channel"] is None


def test_04_transport_stranding_monotonically_reduces_arrival_life(package_life):
    lot = PackageLot(package_life)
    route_a(lot, at=REPACK_AT)
    on_time = evaluate(lot, elapsed_days=7)
    stranded = evaluate(lot, elapsed_days=130)
    assert stranded["remaining_months"] < on_time["remaining_months"]
    assert on_time["wholesale"]["allowed"] is True
    assert stranded["wholesale"]["allowed"] is False
    assert stranded["remaining_months"] == pytest.approx(10 - 130 / 30)


# ---------------------------------------------------------------------------
# Theme 5: channel revocation and customer correction
# ---------------------------------------------------------------------------

def test_05_late_heat_log_revokes_wholesale_and_corrects_customer(package_life):
    lot = PackageLot(package_life)
    route_a(lot, at=REPACK_AT, arrival_at=WHOLESALE_ARRIVAL)
    commitment = commit_wholesale(
        lot, customer="East Wholesale Ltd", customer_order="PO-42",
        at=COMMIT_AT, arrival_at=WHOLESALE_ARRIVAL,
    )
    assert commitment["status"] == "sent"
    promised = commitment["promised_remaining_months"]
    assert promised >= 6

    lot.add_heat_log(LATE_HEAT_ENTRY)
    corrections = reconcile_commitments(lot, at=datetime(2026, 11, 30, 12))

    assert len(corrections) == 1
    correction = corrections[0]
    assert correction["kind"] == "wholesale_revocation_and_customer_correction"
    assert correction["reason"] == "late_heat_log"
    assert correction["commitment_id"] == commitment["id"]
    assert correction["customer"] == "East Wholesale Ltd"
    assert correction["customer_order"] == "PO-42"
    assert "commitment_revoked" in correction["actions"]
    assert "customer_correction_notice_issued" in correction["actions"]
    assert correction["notice"] and "East Wholesale Ltd" in correction["notice"]
    assert correction["previous"]["channel"] == "wholesale"
    assert correction["previous"]["promised_minimum_remaining_months"] == 6
    assert correction["revised"]["wholesale_allowed"] is False
    assert correction["revised"]["heat_days_at_28c"] == pytest.approx(5.0)

    # The commitment row is revoked and linked to the correction; the old
    # promised value is preserved as evidence instead of being overwritten.
    assert lot.commitments[0]["status"] == "revoked"
    assert lot.commitments[0]["promised_remaining_months"] == promised
    assert lot.commitments[0]["correction_ids"] == [correction["correction_id"]]

    # Reconciliation is append-only / idempotent.
    assert reconcile_commitments(lot) == []
    assert len(lot.corrections) == 1

    # Customer delivery evidence can be recorded.
    delivered = deliver_correction(
        lot, correction["correction_id"], at=datetime(2026, 11, 30, 13), via="registered_email"
    )
    assert delivered["delivery"]["status"] == "delivered"
    assert delivered["delivery"]["delivered_at"] is not None


def test_05_high_oxygen_retest_triggers_correction(package_life):
    lot = PackageLot(package_life)
    route_a(lot, at=REPACK_AT, arrival_at=datetime(2026, 12, 1))
    commit_wholesale(
        lot, customer="North Buyer", customer_order="PO-7",
        at=COMMIT_AT, arrival_at=datetime(2026, 12, 1),
    )

    # Late QC finds the PK-03 physical pouch (new code PK-03-R1) still high.
    lot.add_reading({
        "pouch": "PK-03-R1", "oxygen_pct": 3.1, "instrument_error_pct": 0.2,
        "sampling_range": "PK-03-R1 full pouch QC remeasurement",
        "measured_at": "2026-11-29T09:00:00",
    })
    decision = evaluate(lot, arrival_at=datetime(2026, 12, 1))
    assert decision["pouches"]["PK-03"]["blocked"] is True
    assert decision["wholesale"]["allowed"] is False
    assert decision["wholesale"]["eligible_units"] == 99

    corrections = reconcile_commitments(lot, at=datetime(2026, 11, 29, 10))
    assert len(corrections) == 1
    assert corrections[0]["reason"] == "high_oxygen_retest"
    assert lot.commitments[0]["status"] == "revoked"
    assert corrections[0]["revised"]["eligible_units"]["wholesale"] == 99


def test_05_transport_stranding_correction_uses_arrival_remaining(package_life):
    lot = PackageLot(package_life)
    route_a(lot, at=REPACK_AT)
    commit_wholesale(
        lot, customer="West Wholesale", customer_order="PO-8",
        at=COMMIT_AT, arrival_at=WHOLESALE_ARRIVAL,
    )
    stranded_arrival = REPACK_AT + timedelta(days=130)
    corrections = reconcile_commitments(
        lot, at=stranded_arrival, arrival_at=stranded_arrival, reason="transport_stranding"
    )
    assert len(corrections) == 1
    revised = corrections[0]["revised"]
    assert revised["wholesale_allowed"] is False
    assert revised["remaining_months"] == pytest.approx(10 - 130 / 30)
    assert lot.commitments[0]["status"] == "revoked"


def test_05_full_inspection_more_leakers_recomputes_by_physical_code(package_life):
    lot = PackageLot(package_life)
    # Complete measurement of previously untested pouches uncovers two more
    # leaking / out-of-evidence pouches (upper bound > as-packed 5.0 %).
    for code, value in (("PK-07", 5.4), ("PK-42", 5.3)):
        lot.add_reading({
            "pouch": code, "oxygen_pct": value, "instrument_error_pct": 0.2,
            "sampling_range": f"{code} full-pouch inspection probe",
        })

    decision = evaluate(lot, elapsed_days=4)
    assert decision["pouches"]["PK-07"]["oxygen_upper_pct"] == pytest.approx(5.6)
    assert decision["pouches"]["PK-07"]["blocked"] is True
    assert decision["pouches"]["PK-42"]["blocked"] is True
    assert "PK-07" not in decision["local"]["eligible_codes"]
    assert "PK-42" not in decision["local"]["eligible_codes"]
    assert lot.is_sellable("PK-07") is False
    assert "PK-07" not in lot.sellable_codes()
    assert "PK-01" in lot.sellable_codes()
    assert decision["local"]["eligible_units"] == 98
    assert decision["local"]["whole_lot_allowed"] is False

    # The 98 sound original codes can still fulfil a 98-unit local order, but
    # a 100-unit order cannot be completed.
    order_98 = fulfill_order(lot, "local", units=98, arrival_at=LOCAL_ARRIVAL)
    assert order_98["complete"] is True
    assert order_98["units_fulfilled"] == 98
    order_100 = fulfill_order(lot, "local", units=100, arrival_at=LOCAL_ARRIVAL)
    assert order_100["complete"] is False
    assert order_100["units_fulfilled"] == 98
    # Fulfilment references concrete physical / active codes.
    assert set(order_98["pouch_codes"]).issubset(set(lot.sellable_codes()))
