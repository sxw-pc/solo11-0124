"""Group split: per-member entitlement adjudication and fulfillment.

Scenarios exercised:
  * four people across two replacement slots with real two-seat holds;
  * a higher-priority wait-list claim preempting one slot;
  * repeated scans (idempotent receipts and double-entry detection);
  * member-roster variants (reorder allowed, altered roster rejected);
  * opening-notice revision invalidation;
  * interrupted/replayed split and settle retries.
"""
import copy
import json
import pytest
from pathlib import Path
from venuecase.adoption import adopt
from venuecase.assessment import assess
from venuecase.settlement import settle
from venuecase.report import report
from venuecase.slots import configure, claim, release, apply_split
from venuecase.policy import TABLES
from venuecase.store import connect

FIX = Path(__file__).resolve().parent.parent / "fixtures"
MEMBERS = ["m1", "m2", "m3", "m4"]


def write(tmp_path, doc):
    path = tmp_path / (str(len(list(tmp_path.glob("*.json")))) + ".json")
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def group_case(case_id="group-4", at="2026-06-01T08:00:00Z"):
    return {
        "case_id": case_id,
        "source_id": "venue-1",
        "at": at,
        "facts": {
            "booking_id": "G4",
            "paid": True,
            "section": "hall-A",
            "slot": "S1",
            "people": 4,
            "tier": "standard",
            "settle_by": "2026-06-02T00:00:00Z",
        },
    }


def split_doc(version=1, authorized=True, intents=None, members=None,
              split_id="SP-G4", case_id="group-4"):
    members = members or MEMBERS
    if intents is None:
        intents = [
            {"member": "m1", "slot": "S2"},
            {"member": "m2", "slot": "S2"},
            {"member": "m3", "slot": "S3"},
            {"member": "m4", "slot": "S3"},
        ]
    return {
        "case_id": case_id,
        "split_id": split_id,
        "version": version,
        "booking_id": "G4",
        "members": members,
        "authorized": authorized,
        "scope": {"booking_id": "G4", "action": "capacity_reduction_split",
                  "slots": ["S2", "S3"]},
        "intents": intents,
    }


def member_receipt(event_key, member, slot, at="2026-06-01T11:00:00Z"):
    return {"event_key": event_key, "target": member, "at": at, "version": 1,
            "amount": 1, "kind": "rebook", "section": "hall-A", "slot": slot,
            "booking_id": "G4", "member": member}


def member_refund(event_key, member, at="2026-06-01T11:00:00Z"):
    return {"event_key": event_key, "target": member, "at": at, "version": 1,
            "amount": 1, "kind": "refund", "section": "hall-A", "slot": "S1",
            "booking_id": "G4", "member": member}


def settle_doc(receipts, at="2026-06-01T12:00:00Z"):
    return {"case_id": "group-4", "at": at, "receipts": receipts}


def prepare_group(tmp_path, capacities=(2, 2)):
    db = tmp_path / "state.db"
    adopt(db, FIX / "opening_normal.json")
    configure(db, slot="S2", section="hall-A", capacity=capacities[0])
    configure(db, slot="S3", section="hall-A", capacity=capacities[1])
    assess(db, write(tmp_path, group_case()))
    return db


def outcome_map(rep, case_id="group-4"):
    case = next(c for c in rep["cases"] if c["case"] == case_id)
    return {o["member"]: o for o in case["member_outcomes"]}


def test_unauthorized_split_rejects_two_by_two_and_owes_four_compensations(tmp_path):
    db = prepare_group(tmp_path)
    apply_split(db, split_doc(authorized=False))
    decision = assess(db, write(tmp_path, group_case()))
    assert decision["outcome"] == "split_unauthorized_full_compensation"
    assert len(decision["obligations"]) == 4
    assert sorted(o["target"] for o in decision["obligations"]) == MEMBERS
    assert all(json.loads(o["meta"])["kind"] == "refund" for o in decision["obligations"])
    # Even real held seats never become entry rights without authorization.
    assert all(m["state"] == "held" for m in
               report(db)["auxiliary"]["member_claim"])
    refunds = settle(db, write(tmp_path, settle_doc(
        [member_refund(f"refund-{m}", m) for m in MEMBERS])))
    assert refunds["status"] == "complete"
    assert all(o["state"] == "complete" and o["required"] == 1 for o in refunds["obligations"])


def test_authorized_split_assigns_two_real_seats_per_slot_and_settles_each_member(tmp_path):
    db = prepare_group(tmp_path)
    applied = apply_split(db, split_doc())
    assert applied["members"] == {m: "held" for m in MEMBERS}
    decision = assess(db, write(tmp_path, group_case()))
    assert decision["outcome"] == "split_seats_assigned"
    assert all(json.loads(o["meta"])["kind"] == "rebook" for o in decision["obligations"])
    slots = {o["target"]: json.loads(o["meta"])["slot"] for o in decision["obligations"]}
    assert {s for s in slots.values()} == {"S2", "S3"}
    receipts = [member_receipt(f"scan-{m}", m, slots[m]) for m in MEMBERS]
    result = settle(db, write(tmp_path, settle_doc(receipts)))
    assert result["status"] == "complete"
    rep = report(db)
    outcomes = outcome_map(rep)
    assert sorted(outcomes) == MEMBERS
    assert all(o["state"] == "complete" and o["valid_amount"] == 1 for o in outcomes.values())
    # The original four-person payment and the original opening notice revision
    # stay untouched: exactly one source event at revision 1 remains.
    case = next(c for c in rep["cases"] if c["case"] == "group-4")
    assert case["revision"] == 1
    assert len(rep["source_evidence"]) == 1
    assert rep["source_evidence"][0]["revision"] == 1
    assert json.loads(next(r for r in connect(db).execute(
        "SELECT facts FROM booking_right WHERE id='group-4'"))["facts"])["people"] == 4


def test_priority_preempts_one_slot_and_only_its_members_become_compensation(tmp_path):
    db = prepare_group(tmp_path)
    apply_split(db, split_doc())
    assess(db, write(tmp_path, group_case()))
    # Members m3 and m4 already enter on S3 before the preempting claim lands.
    settle(db, write(tmp_path, settle_doc([
        member_receipt("scan-m3", "m3", "S3"),
        member_receipt("scan-m4", "m4", "S3"),
    ])))
    preempt = claim(db, claim_id="CL-PRIO", slot="S2", booking_id="PX",
                    tier="priority", people=2)
    assert preempt["state"] == "held"
    assert set(preempt["displaced"]) == {"G4#m1#v1", "G4#m2#v1"}
    rep = report(db)
    assert rep["cases"][0]["stale"] is True
    waiting = {r["member"]: r["state"] for r in rep["auxiliary"]["member_claim"]}
    assert waiting == {"m1": "waiting", "m2": "waiting", "m3": "held", "m4": "held"}
    decision = assess(db, write(tmp_path, group_case()))
    assert decision["outcome"] == "split_partial_seats_assigned"
    kinds = {o["target"]: json.loads(o["meta"])["kind"] for o in decision["obligations"]}
    assert kinds == {"m1": "refund", "m2": "refund",
                     "m3": "rebook", "m4": "rebook"}
    # Completed entry on S3 is carried forward, never recast as compensation.
    first = settle(db, write(tmp_path, settle_doc(
        [member_refund("refund-m1", "m1"), member_refund("refund-m2", "m2")])))
    assert first["status"] == "complete"
    rep = report(db)
    outcomes = outcome_map(rep)
    assert outcomes["m3"]["kind"] == "rebook" and outcomes["m3"]["state"] == "complete"
    assert outcomes["m4"]["kind"] == "rebook" and outcomes["m4"]["state"] == "complete"
    assert outcomes["m1"]["kind"] == "refund" and outcomes["m1"]["state"] == "complete"
    assert outcomes["m2"]["kind"] == "refund" and outcomes["m2"]["state"] == "complete"
    # A refund attempt against an already-entered member is recorded invalid.
    bad = settle_doc([{**member_refund("refund-m3-again", "m3"), "slot": "S3"}])
    settle(db, write(tmp_path, bad))
    m3_ob = next(o for o in report(db)["cases"][0]["obligations"] if o["target"] == "m3")
    bad_receipt = next(r for r in m3_ob["receipts"] if r["event_key"] == "refund-m3-again")
    assert bad_receipt["verdict"] == "invalid"
    assert bad_receipt["reason"] == "wrong_entitlement_action"
    assert outcomes["m3"]["state"] == "complete"


def test_waiting_members_have_no_entry_right_and_full_seat_shortfall_compensates_all(tmp_path):
    db = prepare_group(tmp_path, capacities=(1, 1))
    applied = apply_split(db, split_doc())
    # Each slot only has one seat: second member on each slot is waiting.
    assert sorted(m for m, s in applied["members"].items() if s == "waiting") == ["m2", "m4"]
    decision = assess(db, write(tmp_path, group_case()))
    assert decision["outcome"] == "split_partial_seats_assigned"
    kinds = {o["target"]: json.loads(o["meta"])["kind"] for o in decision["obligations"]}
    assert kinds == {"m1": "rebook", "m2": "refund",
                     "m3": "rebook", "m4": "refund"}
    # An entry receipt for a waiting member cannot honor its refund duty.
    settle(db, write(tmp_path, settle_doc([member_receipt("scan-m2", "m2", "S2")])))
    m2_ob = next(o for o in report(db)["cases"][0]["obligations"] if o["target"] == "m2")
    assert m2_ob["receipts"][0]["verdict"] == "invalid"
    assert m2_ob["receipts"][0]["reason"] == "wrong_entitlement_action"


def test_repeated_scans_are_idempotent_but_double_entry_is_exposed_per_member(tmp_path):
    db = prepare_group(tmp_path)
    apply_split(db, split_doc())
    assess(db, write(tmp_path, group_case()))
    receipts = [member_receipt(f"scan-{m}", m,
                               "S2" if m in ("m1", "m2") else "S3") for m in MEMBERS]
    assert settle(db, write(tmp_path, settle_doc(receipts)))["status"] == "complete"
    # Replaying the exact same settlement batch changes nothing.
    assert settle(db, write(tmp_path, settle_doc(copy.deepcopy(receipts))))["status"] == "complete"
    rep = report(db)
    for member in MEMBERS:
        assert len(outcome_map(rep)[member]["receipts"]) == 1
    # A second, distinct entry scan for m1 cannot be hidden inside group totals.
    double = settle_doc([member_receipt("scan-m1-again", "m1", "S2")])
    result = settle(db, write(tmp_path, double))
    assert result["status"] == "over_complete"
    m1 = next(o for o in result["obligations"] if o["target"] == "m1")
    assert m1["state"] == "over_complete" and m1["valid_amount"] == 2
    others = [o for o in result["obligations"] if o["target"] != "m1"]
    assert all(o["state"] == "complete" and o["valid_amount"] == 1 for o in others)


def test_member_roster_variants_reorder_ok_altered_or_duplicated_rejected(tmp_path):
    db = prepare_group(tmp_path)
    doc = split_doc()
    apply_split(db, doc)
    reordered = split_doc(version=2, members=["m4", "m3", "m2", "m1"],
                          intents=list(reversed(doc["intents"])))
    apply_split(db, reordered)
    rep = report(db)
    versions = [s for s in rep["cases"][0]["splits"] if s["split_id"] == "SP-G4"]
    assert [s["version"] for s in versions] == [1, 2]
    assert versions[0]["state"] == "superseded" and versions[1]["state"] == "active"
    # A different opaque member on a later version is not the same contract.
    altered = split_doc(version=3, members=["m1", "m2", "m3", "mX"],
                        intents=[{"member": "m1", "slot": "S2"},
                                 {"member": "m2", "slot": "S2"},
                                 {"member": "m3", "slot": "S3"},
                                 {"member": "mX", "slot": "S3"}])
    with pytest.raises(ValueError, match="split_roster_conflict"):
        apply_split(db, altered)
    # Duplicated member within one request cannot occupy twice.
    dup = split_doc(version=3, members=["m1", "m1", "m3", "m4"],
                    intents=[{"member": "m1", "slot": "S2"},
                             {"member": "m1", "slot": "S2"},
                             {"member": "m3", "slot": "S3"},
                             {"member": "m4", "slot": "S3"}])
    with pytest.raises(ValueError, match="split_member_duplicated"):
        apply_split(db, dup)
    # The same member cannot be held by another split contract at all.
    other = split_doc(version=1, split_id="SP-OTHER", case_id="group-4")
    with pytest.raises(ValueError, match="member_held_elsewhere"):
        apply_split(db, other)


def test_notice_revision_invalidates_assessments_and_blocks_stale_settlement(tmp_path):
    db = prepare_group(tmp_path)
    apply_split(db, split_doc())
    assess(db, write(tmp_path, group_case()))
    next_doc = json.loads((FIX / "opening_normal.json").read_text(encoding="utf-8"))
    next_doc["revision"] = 2
    next_doc["payload"] = {"authority": "venue", "action": "capacity",
                           "section": "hall-A", "available": 10}
    adopt(db, write(tmp_path, next_doc))
    rep = report(db)
    assert rep["cases"][0]["stale"] is True
    with pytest.raises(ValueError, match="assessment_stale"):
        settle(db, write(tmp_path, settle_doc(
            [member_receipt(f"scan-{m}", m, "S2" if m in ("m1", "m2") else "S3")
             for m in MEMBERS])))
    updated = assess(db, write(tmp_path, group_case()))
    # The recomputed decision is explicitly bound to the new revision.
    assert updated["revision"] == 2
    receipts = []
    for ob in updated["obligations"]:
        meta = json.loads(ob["meta"])
        receipts.append(member_receipt(f"scan2-{meta['member']}", meta["member"], meta["slot"]))
    # Old revision-1 receipts must be rejected on the revision-2 obligations.
    stale = settle(db, write(tmp_path, settle_doc(receipts)))
    stale_rep = report(db)
    assert all(any(r["reason"] == "stale_opening_version" for r in o["receipts"])
               for o in stale_rep["cases"][0]["obligations"])
    for r in receipts:
        r["version"] = 2
        r["event_key"] = r["event_key"] + "-v2"
    assert settle(db, write(tmp_path, settle_doc(receipts)))["status"] == "complete"
    # Original payment and the superseded opening revision are both preserved.
    rep = report(db)
    revisions = sorted(e["revision"] for e in rep["source_evidence"])
    assert revisions == [1, 2]
    case = next(c for c in rep["cases"] if c["case"] == "group-4")
    con = connect(db)
    try:
        assert json.loads(con.execute(
            "SELECT facts FROM booking_right WHERE id='group-4'").fetchone()["facts"])["people"] == 4
    finally:
        con.close()
    assert case["input_hash"]


def test_interrupted_split_and_settle_retries_converge_to_one_outcome_each(tmp_path):
    db = prepare_group(tmp_path)
    doc = split_doc()
    first = apply_split(db, doc)
    # Replaying the identical split document after an interruption is a no-op.
    again = apply_split(db, copy.deepcopy(doc))
    assert again == first
    con = connect(db)
    assert con.execute("SELECT count(*) FROM split_request").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM member_claim").fetchone()[0] == 4
    assert con.execute("SELECT count(*) FROM slot_claim").fetchone()[0] == 4
    con.close()
    assess(db, write(tmp_path, group_case()))
    receipts = [member_receipt(f"scan-{m}", m,
                               "S2" if m in ("m1", "m2") else "S3") for m in MEMBERS]
    for _ in range(3):
        assert settle(db, write(tmp_path, settle_doc(copy.deepcopy(receipts))))["status"] == "complete"
    con = connect(db)
    assert con.execute(f"SELECT count(*) FROM {TABLES['receipt']}").fetchone()[0] == 4
    # Exactly one slot_claim row per member, no duplicated four-seat tickets.
    rows = con.execute("SELECT booking_id,people,state FROM slot_claim ORDER BY booking_id").fetchall()
    assert [(r["booking_id"], r["people"]) for r in rows] == [
        ("G4#m1#v1", 1), ("G4#m2#v1", 1), ("G4#m3#v1", 1), ("G4#m4#v1", 1)]
    assert all(r["state"] == "held" for r in rows)
    con.close()
    outcomes = outcome_map(report(db))
    assert len(outcomes) == 4
    assert all(o["valid_amount"] == 1 and o["state"] == "complete" for o in outcomes.values())


def test_released_priority_claim_promotes_member_back_to_a_real_hold(tmp_path):
    db = prepare_group(tmp_path)
    apply_split(db, split_doc())
    assess(db, write(tmp_path, group_case()))
    claim(db, claim_id="CL-PRIO", slot="S2", booking_id="PX", tier="priority", people=2)
    rep = report(db)
    assert {r["member"]: r["state"] for r in rep["auxiliary"]["member_claim"]} == {
        "m1": "waiting", "m2": "waiting", "m3": "held", "m4": "held"}
    decision = assess(db, write(tmp_path, group_case()))
    assert decision["outcome"] == "split_partial_seats_assigned"
    promoted = release(db, "CL-PRIO")["promoted"]
    assert set(promoted) <= {"G4#m1#v1", "G4#m2#v1"} and promoted
    rep = report(db)
    assert rep["cases"][0]["stale"] is True
    assert all(r["state"] == "held" for r in rep["auxiliary"]["member_claim"])
    decision = assess(db, write(tmp_path, group_case()))
    assert decision["outcome"] == "split_seats_assigned"
    assert all(json.loads(o["meta"])["kind"] == "rebook" for o in decision["obligations"])


def test_split_validation_rejects_party_size_and_intent_mismatches(tmp_path):
    db = prepare_group(tmp_path)
    bad_size = split_doc(members=["m1", "m2", "m3"],
                         intents=[{"member": "m1", "slot": "S2"},
                                  {"member": "m2", "slot": "S2"},
                                  {"member": "m3", "slot": "S3"}])
    with pytest.raises(ValueError, match="split_party_size_mismatch"):
        apply_split(db, bad_size)
    bad_intent = split_doc(intents=[{"member": "m1", "slot": "S2"},
                                    {"member": "m2", "slot": "S2"},
                                    {"member": "m3", "slot": "S3"}])
    with pytest.raises(ValueError, match="split_intents_required"):
        apply_split(db, bad_intent)
    bad_scope = split_doc()
    bad_scope["scope"] = {"booking_id": "OTHER", "action": "capacity_reduction_split"}
    with pytest.raises(ValueError, match="split_scope_missing"):
        apply_split(db, bad_scope)
