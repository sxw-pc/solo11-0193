"""Append-only per-box transport capture, receipt and exception claims.

``ShipmentCapture`` is the write side of the adjudication in
:mod:`teablend.delivery_close`.  It enforces the four hard rules:

* custody only moves on dual-signed handovers -- a driver's unilateral gate
  scan is preserved as an announcement but never as the customer receipt;
* sound delivery requires a gap-free 4-12 C record (min/max summaries stay
  "pending verification");
* partial receipt needs written customer consent and the undelivered box is
  closed through a damage claim; once that claim is paid the box and its
  disposition are locked to carrier subrogation, so a later recovery needs a
  fresh three-party ownership confirmation and a new sale contract -- never a
  second original-order delivery or charge;
* late scans, temperature corrections and rejections are appended, never
  overwritten: old decisions/notices survive and a new responsibility version
  and receipt notice is generated without backdating the actual signature.
"""

from copy import deepcopy
from datetime import timedelta

from .delivery_close import close_case
from .shipment_scan import parse_ts

CARRIER = 'CARRIER-X'
FACTORY = 'TEA-FACTORY'


class CaptureError(ValueError):
    """Base class for capture-rule violations."""


class EvidenceConflict(CaptureError):
    """Same evidence id replayed with different content."""


class DeliveryBlocked(CaptureError):
    """Box cannot be accepted as sound fresh leaf."""


class ClaimSettledLock(CaptureError):
    """Paid claim locked the parcel against original-order fulfillment."""


class ConfirmationRequired(CaptureError):
    """Three-party confirmation is incomplete."""


class DoublePresentment(CaptureError):
    """An order/parcel charge was presented twice."""


class ShipmentCapture:
    def __init__(self, data=None, *, shipment=None):
        data = data or {}
        self.shipment = dict(shipment or data.get('shipment') or {})
        self.shipment.setdefault('id', data.get('shipment_id', 'SHIPMENT'))
        self.events = list(data.get('events', []) or [])
        self.handovers = list(data.get('handovers', []) or [])
        self.temperature = list(data.get('temperature', []) or [])
        self.temperature_readings = list(
            data.get('temperature_readings', []) or [])
        self.temperature_corrections = list(
            data.get('temperature_corrections', []) or [])
        self.inspections = list(data.get('inspections', []) or [])
        self.receipts = list(data.get('receipts', []) or [])
        self.rejections = list(data.get('rejections', []) or [])
        self.claims = list(data.get('claims', []) or [])
        self.recoveries = list(data.get('recoveries', []) or [])
        self.ownership_confirmations = list(
            data.get('ownership_confirmations', []) or [])
        self.extra = {k: v for k, v in data.items()
                      if k not in {
                          'shipment', 'events', 'handovers', 'temperature',
                          'temperature_readings', 'temperature_corrections',
                          'inspections', 'receipts', 'rejections', 'claims',
                          'recoveries', 'ownership_confirmations'}}

        self.versions = []
        self.notices = []
        self.charges = []
        self._charged = {}
        self._seq = 0
        self._seen_ids = {}
        for collection in (self.events, self.handovers, self.temperature,
                           self.temperature_readings, self.inspections,
                           self.receipts, self.rejections, self.claims,
                           self.recoveries, self.ownership_confirmations):
            for row in collection:
                if row.get('id'):
                    self._seen_ids[row['id']] = deepcopy(row)

        # Seed snapshot: the raw scan/min-max evidence must already show that
        # a lone gate scan does not close the shipment.
        self._snapshot('capture_opened')

    # ------------------------------------------------------------------ ids
    def _next_id(self, prefix):
        self._seq += 1
        return f'{prefix}-{self._seq:03d}'

    def _put(self, collection, row, prefix):
        row = dict(row)
        if not row.get('id'):
            row['id'] = self._next_id(prefix)
        rid = row['id']
        if rid in self._seen_ids:
            if self._canonical(self._seen_ids[rid]) != self._canonical(row):
                raise EvidenceConflict(
                    f'evidence {rid} already exists with different content; '
                    'submit a correction/revision instead of overwriting')
            return self._seen_ids[rid]
        self._seen_ids[rid] = deepcopy(row)
        collection.append(row)
        return row

    @staticmethod
    def _canonical(row):
        import json
        return json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)

    # ------------------------------------------------------------- evidence
    def add_event(self, row, *, recorded_at=None):
        """Append a raw scan/event (idempotent by id)."""
        if recorded_at is not None:
            row = dict(row, recorded_at=recorded_at)
        return self._put(self.events, row, 'EVT')

    def append_late_event(self, row, *, recorded_at, revised_by='carrier'):
        """Append a scan that arrives after a decision and re-version.

        The scan keeps its own earlier ``at`` (business time) while
        ``recorded_at`` proves it arrived late.  Existing signatures and
        decisions are never backdated; a new version/notice is produced.
        """
        row = dict(row, late=True, recorded_at=recorded_at,
                   revised_by=revised_by)
        stored = self.add_event(row)
        snap = self._snapshot(
            'late_event_recorded', at=recorded_at,
            notice_kind='evidence_revision',
            notice_detail={'late_event_id': stored.get('id'),
                           'parcel': stored.get('parcel'),
                           'stage': stored.get('stage'),
                           'event_at': stored.get('at'),
                           'recorded_at': recorded_at})
        return snap

    def add_handover(self, row):
        return self._put(self.handovers, row, 'HO')

    def add_temperature_readings(self, rows):
        stored = []
        for row in rows:
            stored.append(self._put(self.temperature_readings, row, 'TEMP'))
        return stored

    def correct_temperature(self, correction):
        """Append a temperature correction; original reading is preserved."""
        correction = dict(correction)
        correction.setdefault('id', self._next_id('TCORR'))
        if not correction.get('corrects'):
            raise CaptureError('temperature correction needs "corrects" id')
        if correction['id'] in self._seen_ids:
            return self._put(self.temperature_corrections, correction, 'TCORR')
        self._put(self.temperature_corrections, correction, 'TCORR')
        at = correction.get('at')
        return self._snapshot(
            'temperature_corrected', at=at,
            notice_kind='temperature_revision',
            notice_detail={'corrects': correction['corrects'],
                           'parcel': correction.get('parcel'),
                           'at': at})

    def inspect_at_hub(self, row):
        """Record physical re-inspection (used by the route-A assessment)."""
        row = dict(row)
        row.setdefault('physical_verified', True)
        return self._put(self.inspections, row, 'INSP')

    # ------------------------------------------------- effective temp series
    def _effective_readings(self):
        effective = {r['id']: deepcopy(r) for r in self.temperature_readings
                     if r.get('id')}
        for corr in self.temperature_corrections:
            target = corr.get('corrects')
            if target in effective:
                if corr.get('celsius') is not None:
                    effective[target]['celsius'] = corr['celsius']
                if corr.get('at') is not None and corr.get('correct_at'):
                    effective[target]['at'] = corr['correct_at']
                effective[target]['corrected_by'] = corr.get('by')
        return list(effective.values())

    # ---------------------------------------------------------------- case
    def case(self):
        data = dict(self.extra)
        data.update({
            'shipment': self.shipment,
            'events': self.events,
            # Receipts are normalized into chain-closing handovers by
            # delivery_close.close_case; do not duplicate them here.
            'handovers': self.handovers,
            'temperature': self.temperature,
            'temperature_readings': self._effective_readings(),
            'inspections': self.inspections,
            'receipts': self.receipts,
            'rejections': self.rejections,
            'claims': self.claims,
            'recoveries': self.recoveries,
            'ownership_confirmations': self.ownership_confirmations,
        })
        return data

    def close(self):
        return close_case(self.case())

    # ------------------------------------------------------------- receipts
    def _active_claim(self, parcel, states=('awarded', 'paid')):
        return next((c for c in self.claims
                     if c.get('parcel') == parcel
                     and (c.get('state') or c.get('status')) in states), None)

    def _paid_claim(self, parcel):
        return self._active_claim(parcel, ('paid',))

    def _assert_not_claim_locked(self, parcel):
        claim = self._active_claim(parcel)
        if claim is not None:
            paid = (claim.get('state') or claim.get('status')) == 'paid'
            if paid:
                raise ClaimSettledLock(
                    f'{parcel} has a paid damage claim and is locked against '
                    'original-order delivery; recover and re-confirm '
                    'ownership, then sell under a new contract')
            raise ClaimSettledLock(
                f'{parcel} has an awarded non-delivery claim; its '
                'disposition is on hold and it cannot be signed against the '
                'original shipment before payment/recovery proceedings')

    def sound_check(self, parcel, carrier_party=None):
        """Return (ok, reasons) for accepting one box as sound fresh leaf.

        When ``carrier_party`` is supplied it must be the custodian produced
        by the signed handover chain, otherwise the receipt signature would
        close a custody gap (e.g. a driver scanning at the gate while the box
        is still legally held at HUB-2).
        """
        detail = self.close()['parcels_detail'].get(parcel, {})
        reasons = []
        if detail.get('temperature_state') != 'qualified':
            temp = detail.get('temperature') or {}
            codes = [i['code'] for i in temp.get('issues', [])]
            reasons.append('temperature_not_qualified:' + ','.join(codes)
                           or detail.get('temperature_state', 'unknown'))
        if not detail.get('custody_chain_complete'):
            reasons.append('custody_chain_incomplete:' + ','.join(
                detail.get('custody_issue_codes', [])))
        custodian = detail.get('custodian')
        if carrier_party is not None and custodian not in (None, carrier_party):
            reasons.append(
                f'delivery_party_custody_gap:{carrier_party}!={custodian}')
        if self._active_claim(parcel):
            reasons.append('claim_disposition_locked')
        return (not reasons), reasons

    @staticmethod
    def _party_map(parcels, carrier_party):
        if isinstance(carrier_party, str) or carrier_party is None:
            return {p: carrier_party for p in parcels}
        return dict(carrier_party)

    def sign_receipt(self, parcels, *, at, customer_sign, carrier_sign,
                     location='CUSTOMER_GATE', partial=False,
                     consent_ref=None, receipt_id_prefix='RCPT',
                     carrier_party=None):
        """Record the actual per-box dual signature.

        This always preserves evidence of what was signed; use
        :meth:`accept_full` / :meth:`accept_partial` when the workflow must
        refuse non-qualified boxes.  ``at`` is the real signature time and is
        never rewritten by late evidence.  ``carrier_party`` may be a single
        custodian id or a ``{parcel: custodian}`` mapping for the last mile.
        """
        if isinstance(parcels, str):
            parcels = [parcels]
        if not customer_sign or not carrier_sign:
            raise CaptureError(
                'customer receipt requires both carrier and customer '
                'signatures')
        if str(customer_sign).strip() == str(carrier_sign).strip():
            raise CaptureError(
                'dual signature requires two distinct signatories')
        party_map = self._party_map(parcels, carrier_party)
        stored = []
        for parcel in parcels:
            self._assert_not_claim_locked(parcel)
            row = {
                'id': f'{receipt_id_prefix}-{parcel}',
                'parcel': parcel,
                'at': at,
                'location': location,
                'customer_sign': customer_sign,
                'carrier_sign': carrier_sign,
                'carrier_party': party_map.get(parcel),
                'carrier': self.shipment.get('carrier', CARRIER),
                'partial': partial,
                'consent_ref': consent_ref,
            }
            stored.append(self._put(self.receipts, row, 'RCPT'))
        kind = 'partial_receipt_signed' if partial else 'receipt_signed'
        snap = self._snapshot(
            kind, at=at,
            notice_kind='partial_receipt' if partial else 'full_receipt',
            notice_detail={'parcels': list(parcels),
                           'partial': partial,
                           'consent_ref': consent_ref,
                           'signed_at': at})
        return snap

    def accept_partial(self, parcels, *, at, customer_sign, carrier_sign,
                       consent_ref, location='CUSTOMER_GATE',
                       carrier_party=None):
        """Route B entry: written consent + only sound boxes accepted."""
        if not self.shipment.get('partial_credit_allowed', False):
            raise DeliveryBlocked(
                'shipment contract does not allow partial receipt')
        if not consent_ref:
            raise ConfirmationRequired(
                'partial receipt requires written consent reference')
        if isinstance(parcels, str):
            parcels = [parcels]
        party_map = self._party_map(parcels, carrier_party)
        for parcel in parcels:
            ok, reasons = self.sound_check(
                parcel, carrier_party=party_map.get(parcel))
            if not ok:
                raise DeliveryBlocked(
                    f'cannot accept {parcel} as sound fresh leaf: '
                    + '; '.join(reasons))
        return self.sign_receipt(
            parcels, at=at, customer_sign=customer_sign,
            carrier_sign=carrier_sign, location=location, partial=True,
            consent_ref=consent_ref, carrier_party=party_map)

    def accept_full(self, *, at, customer_sign, carrier_sign,
                    location='CUSTOMER_GATE', carrier_party=None):
        """Route A close: every box proven sound and dually signed."""
        parcels = self.shipment.get('parcels', [])
        party_map = self._party_map(parcels, carrier_party)
        blocked = {}
        for parcel in parcels:
            ok, reasons = self.sound_check(
                parcel, carrier_party=party_map.get(parcel))
            if not ok:
                blocked[parcel] = reasons
        if blocked:
            raise DeliveryBlocked(f'shipment not fully deliverable: {blocked}')
        return self.sign_receipt(
            parcels, at=at, customer_sign=customer_sign,
            carrier_sign=carrier_sign, location=location,
            carrier_party=party_map)

    def reject(self, parcel, *, at, reason, rejected_by='FACTORY-B',
               sign=None):
        """Formal customer rejection; signed-receipt evidence is preserved."""
        row = {'parcel': parcel, 'at': at, 'reason': reason,
               'rejected_by': rejected_by, 'sign': sign or f'{rejected_by}-REJ'}
        stored = self._put(self.rejections, row, 'REJ')
        return self._snapshot(
            'customer_rejected', at=at, notice_kind='rejection',
            notice_detail={'parcel': parcel, 'reason': reason,
                           'rejection_id': stored.get('id')})

    # --------------------------------------------------------------- claims
    def open_claim(self, parcel, *, amount_cny, at,
                   reason='carrier_non_delivery', claim_id=None):
        row = {'id': claim_id, 'parcel': parcel, 'reason': reason,
               'amount_cny': amount_cny, 'state': 'opened', 'opened_at': at}
        return self._put(self.claims, row, 'CLM')

    def _transition_claim(self, parcel, *, state, at, amount_cny=None,
                          payment_ref=None, claim_id=None):
        claim = self._find_claim(parcel, claim_id)
        if claim is None:
            raise CaptureError(f'no claim for {parcel}')
        if amount_cny is not None:
            claim['amount_cny'] = amount_cny
        if state == 'awarded':
            claim['state'] = 'awarded'
            claim['awarded_at'] = at
        elif state == 'paid':
            claim['state'] = 'paid'
            claim['paid_at'] = at
            if payment_ref:
                claim['payment_ref'] = payment_ref
        # Replace stored row with mutated canonical copy.
        self._seen_ids[claim['id']] = deepcopy(claim)
        for idx, row in enumerate(self.claims):
            if row['id'] == claim['id']:
                self.claims[idx] = claim
        return claim

    def _find_claim(self, parcel, claim_id=None):
        if claim_id:
            return next((c for c in self.claims if c['id'] == claim_id), None)
        rows = [c for c in self.claims if c.get('parcel') == parcel]
        return rows[-1] if rows else None

    def award_claim(self, parcel, *, at, amount_cny=None, claim_id=None):
        claim = self._transition_claim(
            parcel, state='awarded', at=at, amount_cny=amount_cny,
            claim_id=claim_id)
        self._snapshot('claim_awarded', at=at, notice_kind='claim_award',
                       notice_detail=self._claim_notice(claim))
        return claim

    def pay_claim(self, parcel, *, at, payment_ref, amount_cny=None,
                  claim_id=None):
        claim = self._transition_claim(
            parcel, state='paid', at=at, amount_cny=amount_cny,
            payment_ref=payment_ref, claim_id=claim_id)
        snap = self._snapshot('claim_paid', at=at, notice_kind='claim_paid',
                              notice_detail=self._claim_notice(claim))
        return snap

    def settle_non_delivery(self, parcel, *, amount_cny, at, payment_ref,
                            reason='carrier_non_delivery'):
        """Route B claim close: open -> awarded -> paid in one legal action."""
        claim = self.open_claim(parcel, amount_cny=amount_cny, at=at,
                                reason=reason)
        self._transition_claim(parcel, state='awarded', at=at,
                               amount_cny=amount_cny, claim_id=claim['id'])
        self._transition_claim(parcel, state='paid', at=at,
                                payment_ref=payment_ref,
                                claim_id=claim['id'])
        snap = self._snapshot('non_delivery_claim_settled', at=at,
                              notice_kind='claim_paid',
                              notice_detail=self._claim_notice(
                                  self._find_claim(parcel)))
        return snap

    @staticmethod
    def _claim_notice(claim):
        return {'claim_id': claim.get('id'), 'parcel': claim.get('parcel'),
                'state': claim.get('state'),
                'amount_cny': claim.get('amount_cny'),
                'payment_ref': claim.get('payment_ref'),
                'paid_at': claim.get('paid_at')}

    # --------------------------------------------------- route A assessment
    def assess_recovery(self, parcel, *, at,
                        required_transport_minutes=None):
        """Decide whether route A (find BOX-2 and fully deliver) is lawful.

        A raw ``transfer`` scan is never enough: there must be a physical
        re-inspection at the holding hub confirming the box is present and
        sound, and the remaining cold transport must fit the contractual
        delivery deadline.
        """
        detail = self.close()['parcels_detail'].get(parcel, {})
        inspections = [i for i in self.inspections
                       if i.get('parcel') == parcel]
        inspection = sorted(inspections, key=lambda i: i.get('at', ''))[-1] \
            if inspections else None
        transfer_seen = any(
            e.get('parcel') == parcel and e.get('stage') == 'transfer'
            for e in self.events) or any(
            h.get('parcel') == parcel for h in self.handovers)

        reasons = []
        physical_ok = bool(
            inspection and inspection.get('physical_verified')
            and inspection.get('result') == 'present_sound')
        if not inspection:
            reasons.append('missing_physical_reinspection')
        elif not inspection.get('physical_verified'):
            reasons.append('reinspection_not_physical')
        elif inspection.get('result') != 'present_sound':
            reasons.append(f"inspection_{inspection.get('result')}")

        deadline = self.shipment.get('delivery_deadline')
        eta = None
        if deadline and required_transport_minutes is not None:
            eta_dt = parse_ts(at) + timedelta(
                minutes=float(required_transport_minutes))
            eta = eta_dt.isoformat(timespec='minutes')
            if eta_dt > parse_ts(deadline):
                reasons.append('remaining_transport_time_insufficient')
        elif required_transport_minutes is None and deadline:
            reasons.append('remaining_transport_time_not_assessed')

        # The current cold-chain state is reported; completing the record is
        # enforced later by accept_full/accept_partial rather than treated as
        # the physical-recovery decision itself.
        temp_state = detail.get('temperature_state')

        time_ok = 'remaining_transport_time_insufficient' not in reasons
        assessed_ok = physical_ok and not reasons
        return {
            'parcel': parcel,
            'route_a_available': assessed_ok,
            'can_start_recovery': physical_ok and time_ok,
            'physical_reinspection': inspection,
            'transfer_scan_seen': transfer_seen,
            'current_temperature_state': temp_state,
            'assessed_at': at,
            'eta': eta,
            'deadline': deadline,
            'reasons': reasons,
        }

    # ------------------------------------------------- recovery of paid box
    def recover_found_parcel(self, parcel, *, at, location, condition,
                             recovered_by, recovery_id=None):
        """Record that a claim-paid box was physically found again."""
        claim = self._paid_claim(parcel)
        if claim is None:
            raise ClaimSettledLock(
                f'{parcel} recovery procedure only applies after a paid '
                'damage claim')
        row = {'id': recovery_id, 'parcel': parcel, 'at': at,
               'location': location, 'condition': condition,
               'recovered_by': recovered_by}
        stored = self._put(self.recoveries, row, 'RCV')
        snap = self._snapshot(
            'compensated_parcel_recovered', at=at,
            notice_kind='parcel_recovered',
            notice_detail={'parcel': parcel, 'location': location,
                           'condition': condition,
                           'recovery_id': stored.get('id'),
                           'instruction': 'ownership and disposition must '
                                          'be re-confirmed by carrier, '
                                          'customer and factory'})
        return snap

    def confirm_recovered_ownership(self, parcel, *, at, disposition,
                                    carrier_confirm=None,
                                    customer_confirm=None,
                                    factory_confirm=None,
                                    owner=None, new_order_id=None,
                                    salvage_amount_cny=None,
                                    confirmation_id=None):
        """Three-party re-confirmation after a compensated box is recovered."""
        valid = {'carrier_salvage', 'return_to_factory',
                 'customer_repurchase'}
        if disposition not in valid:
            raise CaptureError(
                f'disposition must be one of {sorted(valid)}')
        parties = {'carrier': carrier_confirm, 'customer': customer_confirm,
                   'factory': factory_confirm}
        missing = [name for name, sign in parties.items()
                   if sign in (None, '', False)]
        if missing:
            raise ConfirmationRequired(
                'ownership reconfirmation missing: ' + ','.join(missing))
        if disposition == 'customer_repurchase':
            if not new_order_id or new_order_id == self.shipment.get('id'):
                raise CaptureError(
                    'customer repurchase needs a NEW order id distinct from '
                    'the original shipment')

        owner = owner or {
            'carrier_salvage': self.shipment.get('carrier', CARRIER),
            'return_to_factory': self.shipment.get('shipper', FACTORY),
            'customer_repurchase': self.shipment.get('customer'),
        }[disposition]
        row = {'id': confirmation_id, 'parcel': parcel, 'at': at,
               'disposition': disposition, 'owner': owner,
               'parties': {k: str(v) for k, v in parties.items()},
               'new_order_id': new_order_id,
               'salvage_amount_cny': salvage_amount_cny,
               # Re-confirmed salvage still can never become SHIP-1 delivery.
               'locked': disposition != 'customer_repurchase'}
        stored = self._put(self.ownership_confirmations, row, 'OWNC')

        charge = None
        if disposition == 'customer_repurchase':
            charge = self.present_charge(
                new_order_id, parcel, salvage_amount_cny or 0, at=at,
                kind='recovered_repurchase',
                idempotency_key=stored['id'])

        snap = self._snapshot(
            'ownership_reconfirmed', at=at,
            notice_kind='ownership_reconfirmed',
            notice_detail={'parcel': parcel, 'owner': owner,
                           'disposition': disposition,
                           'new_order_id': new_order_id,
                           'charge': charge})
        return snap

    def deliver_recovered_against_original(self, parcel):
        """Explicitly forbidden: paid-claim box as original-order delivery."""
        raise ClaimSettledLock(
            f'{parcel} was compensated and its title settled; it cannot be '
            f'delivered twice under {self.shipment.get("id")}')

    # --------------------------------------------------------------- billing
    def present_charge(self, order_id, parcel, amount_cny, *, at,
                       kind='delivery', idempotency_key=None):
        """Present a charge; duplicate presentment for one line is rejected.

        An exact idempotent replay returns the original charge.  Any second,
        distinct presentment of the same ``(order_id, parcel)`` line is a
        duplicate collection attempt and raises.
        """
        key = (order_id, parcel)
        if key in self._charged:
            prior = self._charged[key]
            if (idempotency_key and prior.get('idempotency_key')
                    == idempotency_key and prior['amount_cny'] == amount_cny):
                return deepcopy(prior)
            raise DoublePresentment(
                f'{parcel} already charged once under {order_id}; '
                'double collection is barred')
        row = {'id': self._next_id('CHG'), 'order_id': order_id,
               'parcel': parcel, 'amount_cny': float(amount_cny), 'at': at,
               'kind': kind, 'idempotency_key': idempotency_key}
        self.charges.append(row)
        self._charged[key] = row
        return deepcopy(row)

    # -------------------------------------------------------------- versions
    def _snapshot(self, reason, *, at=None, notice_kind=None,
                  notice_detail=None):
        decision = deepcopy(self.close())
        version = len(self.versions) + 1
        snapshot = {'version': version, 'reason': reason,
                    'created_at': at, 'decision': decision}
        self.versions.append(snapshot)
        if notice_kind:
            notice = self._build_notice(
                version, notice_kind, at, decision, notice_detail or {})
            self.notices.append(notice)
            snapshot['notice'] = deepcopy(notice)
        return deepcopy(snapshot)

    @staticmethod
    def _build_notice(version, kind, at, decision, detail):
        parcels = decision['parcels_detail']
        return {
            'version': version,
            'kind': kind,
            'shipment': decision['shipment'],
            'generated_at': at,
            'status': decision['status'],
            'received_sound_parcels': decision['received_sound_parcels'],
            'claim_parcels': decision['claim_parcels'],
            'holds': decision['holds'],
            'parcel_responsibility': {
                pid: {'responsible_party': p['responsible_party'],
                      'disposition': p['disposition'],
                      'received_at': p['received_at'],
                      'temperature_state': p['temperature_state']}
                for pid, p in parcels.items()},
            'detail': detail,
        }

    def version(self, number):
        return deepcopy(self.versions[number - 1])

    def history(self):
        return [{'version': v['version'], 'reason': v['reason'],
                 'created_at': v['created_at'],
                 'status': v['decision']['status']}
                for v in self.versions]


# Convenience aliases for callers preferring an explicit class name.
DeliveryCapture = ShipmentCapture
DeliveryLedger = ShipmentCapture
Ledger = ShipmentCapture


def capture_shipment(data):
    return ShipmentCapture(data)
