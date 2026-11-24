import copy
import json
from pathlib import Path

import pytest

from teablend.energy_capture import EnergyCapture
from teablend.energy_chargeback import (
    PLANT_ACCOUNT,
    AllocationBoundaryOpen,
    allocate,
    allocate_costs,
    nominal_allocate,
    verify_reconciliation,
)
from teablend.meter_ticks import (
    MeterIdentityError,
    RolloverEvidenceError,
    kwh,
    raw_delta,
    reconcile_readings,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "energy-meter.json"


@pytest.fixture(scope="module")
def data():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_seed_records_unsafe_negative_register_and_nominal_allocation(data):
    """The checked-in seed really does expose -9,960 and negative costs."""
    assert raw_delta(data["readings"]) == -9960
    assert kwh(data["readings"]) == 40

    seed_nominal = nominal_allocate(data["readings"], data["jobs"])
    assert seed_nominal == {"BATCH-A": -5976.0, "BATCH-B": -3984.0}

    changed = copy.deepcopy(data["readings"])
    changed[2]["register_kwh"] = 40
    assert raw_delta(changed) == -9950
    assert nominal_allocate(changed, data["jobs"])["BATCH-A"] == -5970.0


def test_signed_readings_restore_26_and_14_kwh_periods(data):
    meter = reconcile_readings(
        data,
        require_signed_readings=True,
        require_signed_rollover=True,
    )

    assert [(item["start_minute"], item["end_minute"], item["kwh"]) for item in meter["intervals"]] == [
        (540, 600, 26),
        (600, 660, 14),
    ]
    assert meter["total_kwh"] == 40
    assert all(item["kwh"] >= 0 for item in meter["intervals"])
    assert meter["intervals"][0]["rollover_count"] == 1


def test_rollover_count_is_evidence_rather_than_silently_zeroed():
    previous = {"id": "a", "at_minute": 540, "register_kwh": 9990}
    current = {"id": "b", "at_minute": 600, "register_kwh": 16}

    assert reconcile_readings([previous, current])["total_kwh"] == 26

    with pytest.raises(RolloverEvidenceError):
        reconcile_readings([previous, current], allow_inferred_rollover=False)

    two_wraps = [
        {
            "kind": "rollover",
            "start_minute": 540,
            "end_minute": 600,
            "count": 2,
            "signed_by": "meter clerk",
            "signature": "SIG",
        }
    ]
    result = reconcile_readings([previous, current], events=two_wraps, require_signed_rollover=True)
    assert result["intervals"][0]["kwh"] == 10026
    assert result["intervals"][0]["rollover_count"] == 2


def test_replacement_is_not_treated_as_a_dial_wrap(data):
    example = data["late_replacement_example"]
    readings = [data["readings"][1], *example["additional_readings"]]

    meter = reconcile_readings(
        readings,
        dial_max_kwh=10000,
        meter_events=[example["meter_event"]],
        require_signed_readings=True,
    )
    assert [item["kwh"] for item in meter["intervals"]] == [8, 20]
    assert meter["total_kwh"] == 28

    with pytest.raises(MeterIdentityError):
        reconcile_readings(readings, dial_max_kwh=10000)


def test_time_of_use_boundaries_produce_52_and_14_yuan(data):
    result = allocate_costs(
        data,
        require_signed_contract=True,
        require_exact_tariff_boundaries=True,
    )

    assert result["total_grid_kwh"] == 40
    assert [(p["start_minute"], p["end_minute"], p["grid_kwh"], p["yuan_per_kwh"]) for p in result["periods"]] == [
        (540, 600, 26, 2),
        (600, 660, 14, 1),
    ]
    assert [p["grid_amount_yuan"] for p in result["periods"]] == [52, 14]
    assert result["utility_invoice_yuan"] == 66
    assert result["total_yuan"] == 66
    assert result["balanced"] is True

    for period in result["periods"]:
        conservation = period["conservation"]
        assert conservation["balanced"] is True
        assert conservation["grid_kwh"] == conservation["submeter_total_kwh"] + conservation["loss_kwh"]


def test_plant_overhead_route_charges_4_yuan_loss_at_high_rate_once(data):
    result = allocate_costs(data, route="plant_overhead", require_signed_contract=True)

    assert result["allocations"] == {"BATCH-A": 44, "BATCH-B": 14, PLANT_ACCOUNT: 8}
    assert result["batch_allocations"] == {"BATCH-A": 44, "BATCH-B": 14}
    assert result["plant_yuan"] == 8
    assert result["total_yuan"] == 66

    high = result["periods"][0]
    assert high["submeter_kwh"] == {"BATCH-A": 22}
    assert high["loss_kwh"] == 4
    assert high["loss_kwh_by_account"] == {PLANT_ACCOUNT: 4}
    assert high["loss_allocations_yuan"] == {PLANT_ACCOUNT: 8}
    assert "BATCH-A" not in high["loss_allocations_yuan"]


def test_actual_run_route_assigns_high_period_loss_to_a(data):
    result = allocate_costs(
        data,
        route="actual_run",
        require_signed_contract=True,
        require_signed_runtime=True,
    )

    assert result["allocations"] == {"BATCH-A": 52, "BATCH-B": 14, PLANT_ACCOUNT: 0}
    assert result["total_yuan"] == 66
    high = result["periods"][0]
    assert high["loss_kwh_by_account"] == {"BATCH-A": 4}
    assert high["loss_allocations_yuan"] == {"BATCH-A": 8}


def test_both_contract_routes_reconcile_to_same_invoice(data):
    verified = verify_reconciliation(
        data,
        routes=("plant_overhead", "actual_run"),
        require_signed_contract=True,
    )
    assert verified["ok"] is True
    assert verified["total_yuan"] == 66
    assert verified["routes"]["plant_overhead"] == {"BATCH-A": 44, "BATCH-B": 14, PLANT_ACCOUNT: 8}
    assert verified["routes"]["actual_run"] == {"BATCH-A": 52, "BATCH-B": 14, PLANT_ACCOUNT: 0}

    # Convenience entry points accept the fixture or historical arguments.
    assert allocate(data, "plant_overhead")["BATCH-A"] == 44
    assert (
        allocate(
            data["readings"],
            data["jobs"],
            data["tariff"],
            route="actual_run",
            runtime_evidence=data["runtime_records"],
        )["BATCH-A"]
        == 52
    )


def test_actual_run_route_requires_period_runtime_evidence(data):
    broken = copy.deepcopy(data)
    broken["runtime_records"] = []
    with pytest.raises(AllocationBoundaryOpen):
        allocate_costs(broken, route="actual_run", require_signed_runtime=True)


def test_late_calibration_preserves_original_bill_and_posts_delta(data):
    ledger = EnergyCapture(data, route="plant_overhead")
    original = ledger.postings[0]

    revised = ledger.apply_calibration(data["late_calibration_example"])

    assert original["allocations"] == {"BATCH-A": 44, "BATCH-B": 14, PLANT_ACCOUNT: 8}
    assert ledger.original_allocations() == original["allocations"]
    assert revised["version"] == 2
    assert revised["based_on_version"] == 1
    assert revised["responsible_party"] == "calibration_laboratory"
    assert revised["utility_adjustment_yuan"] == 1
    assert revised["original_invoice_yuan"] == 66
    assert revised["current_invoice_yuan"] == 67
    assert revised["allocations"] == {"BATCH-A": 44, "BATCH-B": 14, PLANT_ACCOUNT: 9}

    adjustment = next(entry for entry in revised["entries"] if entry["line_key"] == "P600-660:PLANT:line_loss")
    assert adjustment["amount_yuan"] == 1
    assert adjustment["responsible_party"] == "calibration_laboratory"

    # The old V1 rows are still present; no in-place overwrite occurred.
    v1_keys = {entry["line_id"] for entry in original["entries"]}
    stored_ids = {entry["line_id"] for entry in ledger.entries() if entry["line_id"].startswith("V1:")}
    assert v1_keys == stored_ids
    audit = ledger.audit()
    assert audit["ok"] is True
    assert audit["original_invoice"]["amount_yuan"] == 66
    assert audit["current_invoice_yuan"] == 67


def test_late_replacement_work_order_posts_14_yuan_responsible_delta(data):
    ledger = EnergyCapture(data, route="plant_overhead")
    revised = ledger.record_replacement(data["late_replacement_example"])

    assert revised["version"] == 2
    assert revised["work_order_id"] == "WO-METER-LATE-002"
    assert revised["responsible_party"] == "meter_maintenance"
    assert revised["utility_adjustment_yuan"] == 14
    assert revised["current_invoice_yuan"] == 80
    assert revised["allocations"] == {"BATCH-A": 44, "PLANT": 22, "BATCH-B": 14}

    # Original reported cost and bill remain exactly as first posted.
    assert ledger.original_allocations() == {"BATCH-A": 44, "BATCH-B": 14, "PLANT": 8}
    assert ledger.postings[0]["invoice"]["amount_yuan"] == 66
    assert ledger.audit()["ok"] is True


def test_payment_dispute_is_recorded_without_erasing_cost(data):
    ledger = EnergyCapture(data, route="plant_overhead")
    event = ledger.register_dispute(
        {"dispute_id": "DISP-1", "reason": "utility refuses wrap calculation", "responsible_party": "utility"}
    )
    assert event["kind"] == "invoice_disputed"
    assert len(ledger.disputes()) == 1
    assert ledger.current_allocations() == {"BATCH-A": 44, "BATCH-B": 14, "PLANT": 8}
    assert ledger.audit()["ok"] is True


def test_signed_reset_is_separate_from_dial_rollover():
    readings = [
        {"id": "BEFORE", "at_minute": 540, "meter_id": "G1", "epoch": 0, "register_kwh": 500, "signed_by": "clerk", "signature": "S1"},
        {"id": "PRE-RESET", "at_minute": 600, "sequence": 0, "meter_id": "G1", "epoch": 0, "register_kwh": 700, "signed_by": "clerk", "signature": "S2"},
        {"id": "POST-RESET", "at_minute": 600, "sequence": 1, "meter_id": "G1", "epoch": 1, "register_kwh": 0, "signed_by": "clerk", "signature": "S3"},
        {"id": "AFTER", "at_minute": 660, "meter_id": "G1", "epoch": 1, "register_kwh": 30, "signed_by": "clerk", "signature": "S4"},
    ]
    event = {
        "id": "RESET-WO-1",
        "kind": "reset",
        "meter_id": "G1",
        "at_minute": 600,
        "old_epoch": "0",
        "new_epoch": "1",
        "signed_by": "meter shop",
        "signature": "SIG-RESET",
    }
    result = reconcile_readings(readings, meter_events=[event], require_signed_readings=True)
    assert [item["kwh"] for item in result["intervals"]] == [200, 30]
    assert result["total_kwh"] == 230


def test_contract_route_can_change_only_through_a_versioned_reclassification(data):
    ledger = EnergyCapture(data, route="plant_overhead")
    revised = ledger.reallocate_loss("actual_run")

    assert ledger.postings[0]["allocations"] == {"BATCH-A": 44, "BATCH-B": 14, "PLANT": 8}
    assert revised["allocations"] == {"BATCH-A": 52, "BATCH-B": 14, "PLANT": 0}
    assert sum(entry["amount_yuan"] for entry in revised["entries"]) == 0
    assert ledger.audit()["ok"] is True


def test_late_calibration_can_keep_original_invoice_in_responsibility_variance(data):
    ledger = EnergyCapture(data, route="plant_overhead")
    revised = ledger.apply_calibration(
        {
            "id": "CAL-PRESERVE",
            "reading_id": "R2",
            "correction_delta_kwh": 1,
            "responsible_party": "calibration_laboratory",
        },
        preserve_invoice=True,
    )

    assert revised["current_invoice_yuan"] == 66
    assert revised["utility_adjustment_yuan"] == 0
    assert revised["allocations"]["PLANT"] == 9
    assert revised["allocations"]["RESPONSIBILITY_VARIANCE"] == -1
    assert ledger.original_allocations()["PLANT"] == 8
    assert ledger.audit()["ok"] is True


def test_late_third_line_reopens_run_boundary_but_retains_a_direct_charge(data):
    ledger = EnergyCapture(data, route="actual_run")
    assert ledger.original_allocations() == {"BATCH-A": 52, "BATCH-B": 14, "PLANT": 0}

    revised = ledger.reopen_loss_boundary(data["late_third_line_example"])

    assert revised["status"] == "boundary_reopened"
    assert revised["open_boundaries"][0]["context"]["unknown_lines"] == [{"line_id": "LINE-C"}]
    assert revised["allocations"] == {"BATCH-A": 44, "BATCH-B": 14, "PLANT": 8}

    # Version 1 still reports A=52. Version 2 only reverses A's loss layer;
    # the signed 22 kWh * 2 yuan direct charge remains 44.
    assert ledger.postings[0]["allocations"]["BATCH-A"] == 52
    adjustments = {(entry["account"], entry["kind"]): entry["amount_yuan"] for entry in revised["entries"]}
    assert adjustments[("BATCH-A", "adjustment:line_loss")] == -8
    assert adjustments[(PLANT_ACCOUNT, "adjustment:line_loss")] == 8
    assert not any(entry["account"] == "BATCH-A" and entry["kind"] == "adjustment:direct_energy" for entry in revised["entries"])

    audit = ledger.audit()
    assert audit["ok"] is True
    assert audit["current_allocations"]["BATCH-A"] == 44
    assert audit["current_invoice_yuan"] == 66
