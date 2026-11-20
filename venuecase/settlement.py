"""Independent fulfillment records over the obligations a business case created."""
import json
from .source import read,digest,canonical,utc
from .store import connect
from .policy import TABLES,receipt_verdict

def settle(db_path,input_path):
    doc=read(input_path)
    if "case_id" not in doc or "at" not in doc or not isinstance(doc.get("receipts"),list):
        raise ValueError("settlement_input_required")
    now=utc(doc["at"]);t=TABLES;db=connect(db_path)
    db.execute("BEGIN IMMEDIATE")
    try:
        assessment=db.execute(f"SELECT * FROM {t['assessment']} WHERE case_id=? ORDER BY id DESC LIMIT 1",
                              (doc["case_id"],)).fetchone()
        if not assessment:raise ValueError("assess_first")
        if assessment["stale"]:raise ValueError("assessment_stale")
        obligations=db.execute(f"SELECT * FROM {t['obligation']} WHERE assessment_id=? ORDER BY target",
                               (assessment["id"],)).fetchall()
        targets=[x["target"] for x in obligations]
        if len(targets)!=len(set(targets)):raise ValueError("duplicate_obligation_target")
        for receipt in doc["receipts"]:
            for key in ("event_key","target","at","version","amount"):
                if key not in receipt:raise ValueError("receipt_missing_"+key)
            receipt_time=utc(receipt["at"]).isoformat()
            ob=next((x for x in obligations if x["target"]==receipt["target"]),None)
            if not ob:raise ValueError("receipt_outside_obligation")
            fp=digest(receipt)
            old=db.execute(f"SELECT digest FROM {t['receipt']} WHERE event_key=?",(receipt["event_key"],)).fetchone()
            if old:
                if old["digest"]!=fp:raise ValueError("receipt_identity_conflict")
                continue
            verdict,reason=receipt_verdict(receipt,dict(ob),assessment["source_revision"])
            db.execute(f"INSERT INTO {t['receipt']} VALUES (?,?,?,?,?,?,?,?,?)",
                       (receipt["event_key"],ob["id"],receipt_time,receipt["version"],receipt["amount"],
                        canonical(receipt),fp,verdict,reason))
        results=[]
        for ob in obligations:
            valid=db.execute(f"SELECT COALESCE(sum(amount),0) FROM {t['receipt']} WHERE obligation_id=? AND verdict='valid' AND at<=?",
                             (ob["id"],ob["deadline"])).fetchone()[0]
            late=db.execute(f"SELECT COALESCE(sum(amount),0) FROM {t['receipt']} WHERE obligation_id=? AND verdict='valid' AND at>?",
                            (ob["id"],ob["deadline"])).fetchone()[0]
            if valid>ob["required"]:
                state="over_complete"
            elif valid==ob["required"]:
                state="complete"
            elif now>utc(ob["deadline"]):
                state="overdue"
            elif valid>0:
                state="partial"
            else:
                state="pending"
            db.execute(f"UPDATE {t['obligation']} SET state=? WHERE id=?",(state,ob["id"]))
            results.append({"target":ob["target"],"state":state,"valid_amount":valid,"late_amount":late,
                            "required":ob["required"],"deadline":ob["deadline"]})
        states={x["state"] for x in results}
        overall=("no_obligation" if not results else
                 ("complete" if states=={"complete"} else
                  ("over_complete" if "over_complete" in states else
                   ("overdue" if "overdue" in states else
                    ("partial" if states & {"partial","complete"} else "pending")))))
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
    return {"case":doc["case_id"],"status":overall,"obligations":results}
