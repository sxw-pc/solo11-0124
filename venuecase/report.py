"""Evidence-facing report reconstructed from durable entities."""
import json
from pathlib import Path
from .store import connect
from .policy import TABLES,EXTRA_TABLES

def report(db_path,out=None):
    db=connect(db_path);t=TABLES
    notices=[dict(x) for x in db.execute(f"SELECT * FROM {t['notice']} ORDER BY id")]
    has_splits=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='split_request'").fetchone()
    cases=[]
    for row in db.execute(f"SELECT * FROM {t['case']} ORDER BY id"):
        result=db.execute(f"SELECT * FROM {t['assessment']} WHERE case_id=? ORDER BY id DESC LIMIT 1",
                          (row["id"],)).fetchone()
        obligations=[dict(x) for x in db.execute(f"SELECT * FROM {t['obligation']} WHERE assessment_id=? ORDER BY target",
                     (result["id"],))] if result else []
        for ob in obligations:
            ob["receipts"]=[dict(x) for x in db.execute(f"SELECT event_key,at,amount,verdict,reason FROM {t['receipt']} WHERE obligation_id=? ORDER BY event_key",(ob["id"],))]
        case_block={"case":row["id"],"source":row["source_id"],"input_hash":row["input_hash"],
                    "state":result["state"] if result else None,
                    "outcome":result["outcome"] if result else None,
                    "revision":result["source_revision"] if result else None,
                    "stale":bool(result["stale"]) if result else None,
                    "detail":json.loads(result["detail"]) if result else None,
                    "obligations":obligations}
        if has_splits:
            splits=[dict(x) for x in db.execute("SELECT * FROM split_request WHERE case_id=? ORDER BY split_id,version",
                                                 (row["id"],))]
            for split in splits:
                split["members"]=json.loads(split["members"])
                split["scope"]=json.loads(split["scope"])
                split["intents"]=json.loads(split["intents"])
                split["authorized"]=bool(split["authorized"])
            case_block["splits"]=splits
            case_block["member_outcomes"]=_member_outcomes(db,obligations,t)
        cases.append(case_block)
    evidence=[dict(x) for x in db.execute("SELECT source_id,revision,digest,captured_at FROM source_event ORDER BY source_id,revision")]
    conflicts=[dict(x) for x in db.execute("SELECT source_id,revision,digest,reason FROM source_conflict ORDER BY id")]
    auxiliary={name:[dict(x) for x in db.execute(f"SELECT * FROM {name} ORDER BY 1")]
               for name in EXTRA_TABLES if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone()}
    db.close()
    result={"notices":notices,"cases":cases,"source_evidence":evidence,"source_conflicts":conflicts,
            "auxiliary":auxiliary}
    if out:
        target=Path(out);target.parent.mkdir(parents=True,exist_ok=True)
        temp=target.with_name(target.name+".pending")
        temp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        temp.replace(target)
    return result

def _member_outcomes(db,obligations,t):
    """One row per member: cumulative headcount can never hide an individual."""
    outcomes=[]
    for ob in obligations:
        meta=json.loads(ob["meta"])
        if meta.get("member") is None:continue
        valid=db.execute(f"SELECT COALESCE(sum(amount),0) FROM {t['receipt']} "
                         f"WHERE obligation_id=? AND verdict='valid'",(ob["id"],)).fetchone()[0]
        receipts=[r["event_key"] for r in db.execute(
            f"SELECT event_key FROM {t['receipt']} WHERE obligation_id=? ORDER BY event_key",(ob["id"],))]
        outcomes.append({"booking_id":meta["booking_id"],"member":meta["member"],"slot":meta["slot"],
                         "kind":meta["kind"],"split_id":meta["split_id"],"split_version":meta["split_version"],
                         "state":ob["state"],"valid_amount":valid,"receipts":receipts})
    return outcomes
