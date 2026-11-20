"""Business cases: a version-bound decision distinct from public-source adoption."""
from .source import read,digest,canonical,utc
from .store import connect
from .policy import TABLES,assess_case,assess_split
import json

def assess(db_path,input_path):
    doc=read(input_path)
    for key in ("case_id","source_id","at","facts"):
        if key not in doc: raise ValueError("case_missing_"+key)
    moment=utc(doc["at"]);fingerprint=digest(doc);t=TABLES;db=connect(db_path)
    db.execute("BEGIN IMMEDIATE")
    try:
        case=db.execute(f"SELECT * FROM {t['case']} WHERE id=?",(doc["case_id"],)).fetchone()
        if case and case["input_hash"]!=fingerprint:raise ValueError("case_identity_conflict")
        if not case:
            db.execute(f"INSERT INTO {t['case']} VALUES (?,?,?,?,?)",
                       (doc["case_id"],doc["source_id"],doc["at"],canonical(doc["facts"]),fingerprint))
        existing=db.execute(f"SELECT * FROM {t['assessment']} WHERE case_id=? ORDER BY id DESC LIMIT 1",
                            (doc["case_id"],)).fetchone()
        if existing and not existing["stale"]:
            result=_describe(db,existing["id"]);db.commit();return result
        notice=db.execute(f"SELECT * FROM {t['notice']} WHERE id=?",(doc["source_id"],)).fetchone()
        revision=db.execute(f"SELECT * FROM {t['revision']} WHERE source_id=? AND revision=?",
                            (doc["source_id"],notice["current_revision"])).fetchone() if notice else None
        split=_active_split(db,doc["case_id"])
        if not revision or notice["state"]=="conflict":
            decision={"state":"uncertain","outcome":"source_unresolved","detail":{"reason":"missing_or_conflicting_notice"},"obligations":[]}
        elif revision["state"] in ("cancelled","invalid"):
            decision={"state":"unaffected","outcome":"notice_not_active","detail":{"reason":revision["state"]},"obligations":[]}
        elif not (utc(revision["effective_from"])<=moment<utc(revision["effective_until"])):
            decision={"state":"unaffected","outcome":"outside_effective_window","detail":{"reason":"time_window"},"obligations":[]}
        elif split is not None:
            member_states=_member_states(db,split["split_id"])
            decision=assess_split(doc["facts"],json.loads(revision["payload"]),split,member_states)
        else:
            from .slots import held_for
            has_slots=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='slot_inventory'").fetchone()
            if has_slots and db.execute('SELECT 1 FROM slot_inventory').fetchone():
                facts=doc['facts']
                hold=held_for(db,facts.get('replacement_slot'),facts.get('booking_id'))
                doc={**doc,'facts':{**facts,'replacement_capacity':hold}}
            decision=assess_case(doc, json.loads(revision["payload"]))
        if not isinstance(decision.get("obligations"),list):raise ValueError("policy_obligations_required")
        cur=db.execute(f"INSERT INTO {t['assessment']}(case_id,source_revision,state,outcome,detail) VALUES (?,?,?,?,?)",
                       (doc["case_id"],revision["revision"] if revision else None,decision["state"],
                        decision["outcome"],canonical(decision["detail"])))
        assessment_id=cur.lastrowid
        for ob in decision["obligations"]:
            if ob["required"]<1:raise ValueError("invalid_obligation")
            ob_id=f"{assessment_id}:{ob['target']}"
            db.execute(f"INSERT INTO {t['obligation']} VALUES (?,?,?,?,?,?,?)",
                       (ob_id,assessment_id,ob["target"],utc(ob["deadline"]).isoformat(),
                        ob["required"],canonical(ob.get("meta",{})),"pending"))
        if existing:
            _carry_receipts(db,existing["id"],assessment_id,t)
        result=_describe(db,assessment_id);db.commit()
    except Exception:
        db.rollback();raise
    finally: db.close()
    return result

def _active_split(db,case_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='split_request'").fetchone():
        return None
    row=db.execute("SELECT * FROM split_request WHERE case_id=? AND state='active' "
                   "ORDER BY version DESC LIMIT 1",(case_id,)).fetchone()
    if not row:return None
    return {"split_id":row["split_id"],"version":row["version"],"booking_id":row["booking_id"],
            "members":json.loads(row["members"]),"authorized":bool(row["authorized"])}

def _member_states(db,split_id):
    return {row["member"]:{"state":row["state"],"slot":row["slot"],"claim_id":row["claim_id"]}
            for row in db.execute("SELECT * FROM member_claim WHERE split_id=?",(split_id,))}

def _carry_receipts(db,old_assessment_id,new_assessment_id,t):
    """Re-link still-valid fulfillment evidence to the recomputed obligations.

    A member already admitted through a real hold keeps that entry receipt when
    another member's seat is displaced: displacement recomputes rights but never
    reopens completed entry into a compensation obligation.
    """
    new_obs={row["target"]:row for row in
             db.execute(f"SELECT * FROM {t['obligation']} WHERE assessment_id=?",(new_assessment_id,))}
    for old in db.execute(f"SELECT * FROM {t['obligation']} WHERE assessment_id=?",(old_assessment_id,)):
        new=new_obs.get(old["target"])
        if not new:continue
        for receipt in db.execute(f"SELECT * FROM {t['receipt']} WHERE obligation_id=? AND verdict='valid'",
                                  (old["id"],)):
            payload=json.loads(receipt["payload"])
            if payload.get("kind")!=json.loads(new["meta"]).get("kind"):continue
            if payload.get("section")!=json.loads(new["meta"]).get("section"):continue
            if payload.get("slot")!=json.loads(new["meta"]).get("slot"):continue
            db.execute(f"UPDATE {t['receipt']} SET obligation_id=? WHERE event_key=?",
                       (new["id"],receipt["event_key"]))

def _describe(db,assessment_id):
    t=TABLES
    row=db.execute(f"SELECT * FROM {t['assessment']} WHERE id=?",(assessment_id,)).fetchone()
    obs=[dict(x) for x in db.execute(f"SELECT * FROM {t['obligation']} WHERE assessment_id=? ORDER BY target",(assessment_id,))]
    return {"assessment_id":assessment_id,"case":row["case_id"],"state":row["state"],
            "outcome":row["outcome"],"detail":json.loads(row["detail"]),
            "revision":row["source_revision"],"stale":bool(row["stale"]),"obligations":obs}
