"""Open-hour changes, booking rights and entry/compensation rules."""
import json
TABLES={"notice":"opening_notice","revision":"venue_window","case":"booking_right",
"assessment":"rebooking_offer","obligation":"compensation_case","receipt":"entry_scan"}
DEMO={"adopt":"opening_normal.json","assess":"booking_normal.json","settle":"entry_normal.json"}
EXTRA_TABLES=["slot_inventory","slot_claim","split_request","member_claim"]
def validate_notice(doc):
    p=doc["payload"]
    if p.get("authority")!="venue":return "invalid","untrusted_venue_notice"
    if p.get("action")=="cancel":return "cancelled","closure_cancelled"
    if p.get("action") not in ("close","capacity") or not p.get("section"):
        return "invalid","invalid_opening_change"
    return "active","accepted_opening_change"
def assess_case(doc,p):
    f=doc["facts"];booking=f.get("booking_id")
    if not booking or not f.get("paid"):return {"state":"uncertain","outcome":"booking_unproven",
      "detail":{"reason":"payment_or_identity_missing"},"obligations":[]}
    if f.get("section")!=p["section"]:
        return {"state":"honored","outcome":"original_entry_valid","detail":{"section":f.get("section")},"obligations":[]}
    people=f.get("people",0)
    if people<1:return {"state":"uncertain","outcome":"party_size_unknown","detail":{},"obligations":[]}
    if p["action"]=="capacity" and p.get("available",0)>=people:
        state,outcome,kind="honored","reduced_capacity_honors_booking","entry"
    elif f.get("replacement_capacity",0)>=people and f.get("tier") in ("priority","standard"):
        state,outcome,kind="rebooked","replacement_slot_assigned","rebook"
    else:state,outcome,kind="compensate","entry_right_unfulfilled","refund"
    ob={"target":booking,"deadline":f["settle_by"],"required":people,
        "meta":{"kind":kind,"section":p["section"],"slot":f.get("replacement_slot") if kind=="rebook" else f.get("slot")}}
    return {"state":state,"outcome":outcome,"detail":{"booking":booking,"party_size":people,"kind":kind},"obligations":[ob]}
def assess_split(facts,p,split,member_states):
    """Per-member entitlement decision for an authorized group split.

    member_states maps an opaque member id to its current member_claim row.
    Only a member whose backing slot_claim is really held earns an entry
    obligation; waiting/released members earn a compensation obligation and
    an unauthorized split never converts holds into entry rights.
    """
    booking=facts.get("booking_id")
    if not booking or not facts.get("paid"):
        return {"state":"uncertain","outcome":"booking_unproven",
                "detail":{"reason":"payment_or_identity_missing"},"obligations":[]}
    people=facts.get("people",0);roster=list(split["members"])
    if people<1 or people!=len(roster):
        return {"state":"uncertain","outcome":"party_size_unknown","detail":{},"obligations":[]}
    authorized=bool(split["authorized"]);obligations=[];dispositions=[];held=0
    for member in roster:
        row=member_states.get(member)
        member_held=authorized and row is not None and row.get("state")=="held"
        if member_held:
            kind="rebook";slot=row["slot"];held+=1
        else:
            kind="refund";slot=facts.get("slot")
        meta={"kind":kind,"section":p["section"],"slot":slot,"booking_id":booking,
              "member":member,"split_id":split["split_id"],"split_version":split["version"]}
        obligations.append({"target":member,"deadline":facts["settle_by"],"required":1,"meta":meta})
        dispositions.append({"member":member,"slot":slot,"kind":kind,
                             "state":row.get("state") if row else "missing","held":bool(member_held)})
    if not authorized:
        state,outcome="compensate","split_unauthorized_full_compensation"
    elif held==people:
        state,outcome="rebooked","split_seats_assigned"
    elif held==0:
        state,outcome="compensate","entry_right_unfulfilled"
    else:
        state,outcome="rebooked","split_partial_seats_assigned"
    detail={"booking":booking,"party_size":people,"kind":"split","split_id":split["split_id"],
            "split_version":split["version"],"authorized":authorized,"held_members":held,
            "members":dispositions}
    return {"state":state,"outcome":outcome,"detail":detail,"obligations":obligations}
def receipt_verdict(r,ob,version):
    meta=json.loads(ob["meta"])
    if r["version"]!=version:return "invalid","stale_opening_version"
    if r.get("kind")!=meta["kind"]:return "invalid","wrong_entitlement_action"
    if r.get("section")!=meta["section"]:return "invalid","wrong_venue_section"
    if meta.get("member") is not None:
        if r.get("member")!=meta["member"]:return "invalid","wrong_group_member"
        if r.get("booking_id")!=meta["booking_id"]:return "invalid","wrong_booking_reference"
    if r["amount"]<1:return "invalid","empty_scan_or_refund"
    if meta["kind"]=="rebook" and r.get("slot")!=meta["slot"]:return "invalid","wrong_rebooking_slot"
    return "valid","right_honored_or_compensated"