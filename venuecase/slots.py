"""Finite replacement-session holds, group splits and priority displacement."""
import json
from .source import digest
from .store import connect
from .policy import TABLES

SCHEMA="""
CREATE TABLE IF NOT EXISTS slot_inventory(
 slot TEXT PRIMARY KEY, section TEXT NOT NULL, capacity INTEGER NOT NULL CHECK(capacity>0));
CREATE TABLE IF NOT EXISTS slot_claim(
 id TEXT PRIMARY KEY, slot TEXT NOT NULL REFERENCES slot_inventory(slot),
 booking_id TEXT NOT NULL, tier TEXT NOT NULL, people INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('held','waiting','released')),
 UNIQUE(slot,booking_id));
CREATE TABLE IF NOT EXISTS split_request(
 split_id TEXT NOT NULL, version INTEGER NOT NULL, case_id TEXT NOT NULL,
 booking_id TEXT NOT NULL, people INTEGER NOT NULL, members TEXT NOT NULL,
 authorized INTEGER NOT NULL, scope TEXT NOT NULL, intents TEXT NOT NULL,
 input_hash TEXT NOT NULL, state TEXT NOT NULL
 CHECK(state IN ('active','superseded')),
 PRIMARY KEY(split_id,version));
CREATE TABLE IF NOT EXISTS member_claim(
 split_id TEXT NOT NULL, member TEXT NOT NULL, slot TEXT NOT NULL,
 claim_id TEXT NOT NULL, state TEXT NOT NULL
 CHECK(state IN ('held','waiting','released')),
 PRIMARY KEY(split_id,member));
"""
def configure(db_path,slot,section,capacity):
    if capacity<1:raise ValueError('invalid_slot_capacity')
    db=connect(db_path);db.executescript(SCHEMA)
    old=db.execute('SELECT * FROM slot_inventory WHERE slot=?',(slot,)).fetchone()
    if old and (old['section'],old['capacity'])!=(section,capacity):
        raise ValueError('slot_identity_conflict')
    with db:db.execute('INSERT OR IGNORE INTO slot_inventory VALUES (?,?,?)',(slot,section,capacity))
    db.close();return {'slot':slot,'capacity':capacity}

def claim(db_path,claim_id,slot,booking_id,tier,people):
    if tier not in ('priority','standard') or people<1:raise ValueError('invalid_slot_claim')
    db=connect(db_path);db.executescript(SCHEMA);db.execute('BEGIN IMMEDIATE')
    try:
        state,displaced=_claim_core(db,claim_id,slot,booking_id,tier,people)
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
    return {'claim':claim_id,'state':state,'displaced':displaced}

def _claim_core(db,claim_id,slot,booking_id,tier,people):
    """Idempotent finite-capacity hold, running inside an open transaction."""
    inventory=db.execute('SELECT * FROM slot_inventory WHERE slot=?',(slot,)).fetchone()
    if not inventory or people>inventory['capacity']:raise ValueError('slot_capacity_unavailable')
    old=db.execute('SELECT * FROM slot_claim WHERE id=?',(claim_id,)).fetchone()
    if old:
        if (old['slot'],old['booking_id'],old['tier'],old['people'])!=(slot,booking_id,tier,people):
            raise ValueError('slot_claim_conflict')
        return old['state'],[]
    held=db.execute("SELECT COALESCE(sum(people),0) FROM slot_claim WHERE slot=? AND state='held'",(slot,)).fetchone()[0]
    displaced=[]
    standard=db.execute("SELECT COALESCE(sum(people),0) FROM slot_claim WHERE slot=? AND state='held' AND tier='standard'",(slot,)).fetchone()[0]
    if held+people>inventory['capacity'] and tier=='priority' and held-standard+people<=inventory['capacity']:
        for row in db.execute("SELECT * FROM slot_claim WHERE slot=? AND state='held' AND tier='standard' ORDER BY id DESC",(slot,)):
            db.execute("UPDATE slot_claim SET state='waiting' WHERE id=?",(row['id'],))
            held-=row['people'];displaced.append(row['booking_id'])
            if held+people<=inventory['capacity']:break
    state='held' if held+people<=inventory['capacity'] else 'waiting'
    db.execute('INSERT INTO slot_claim VALUES (?,?,?,?,?,?)',(claim_id,slot,booking_id,tier,people,state))
    _stale_bookings(db,slot,[booking_id,*displaced])
    _reflect_claims(db,[slot])
    return state,displaced

def release(db_path,claim_id):
    db=connect(db_path);db.executescript(SCHEMA);db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT * FROM slot_claim WHERE id=?',(claim_id,)).fetchone()
        if not row or row['state']!='held':raise ValueError('slot_claim_not_held')
        db.execute("UPDATE slot_claim SET state='released' WHERE id=?",(claim_id,))
        promoted=_promote(db,row['slot'])
        _stale_bookings(db,row['slot'],[row['booking_id'],*promoted])
        _reflect_claims(db,[row['slot']])
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
    return {'claim':claim_id,'state':'released','promoted':promoted}

def _promote(db,slot):
    capacity=db.execute('SELECT capacity FROM slot_inventory WHERE slot=?',(slot,)).fetchone()[0]
    held=db.execute("SELECT COALESCE(sum(people),0) FROM slot_claim WHERE slot=? AND state='held'",(slot,)).fetchone()[0]
    promoted=[]
    for candidate in db.execute("SELECT * FROM slot_claim WHERE slot=? AND state='waiting' ORDER BY CASE tier WHEN 'priority' THEN 0 ELSE 1 END,id",(slot,)):
        if held+candidate['people']<=capacity:
            db.execute("UPDATE slot_claim SET state='held' WHERE id=?",(candidate['id'],))
            held+=candidate['people'];promoted.append(candidate['booking_id'])
    return promoted

def apply_split(db_path,doc):
    """Record an authorized-scope group split and hold one real seat per member.

    The booking contract is never duplicated: every member maps to exactly one
    backing slot_claim, so a four-person booking produces four one-seat holds
    rather than two copied four-seat tickets.
    """
    for key in ("case_id","split_id","version","booking_id","members","authorized","scope","intents"):
        if key not in doc:raise ValueError("split_missing_"+key)
    if not isinstance(doc["version"],int) or doc["version"]<1:raise ValueError("bad_split_version")
    members=doc["members"]
    if not isinstance(members,list) or not members or any(not isinstance(m,str) or not m for m in members):
        raise ValueError("split_members_required")
    if len(members)!=len(set(members)):raise ValueError("split_member_duplicated")
    scope=doc["scope"]
    if not isinstance(scope,dict) or scope.get("booking_id")!=doc["booking_id"] or not scope.get("action"):
        raise ValueError("split_scope_missing")
    intents=doc["intents"]
    if not isinstance(intents,list) or len(intents)!=len(members):raise ValueError("split_intents_required")
    for item in intents:
        if not isinstance(item,dict) or item.get("member") not in members or not item.get("slot"):
            raise ValueError("split_intent_invalid")
    if {i["member"] for i in intents}!={*members}:raise ValueError("split_intent_members_mismatch")
    by_member={i["member"]:i for i in intents}
    db=connect(db_path);t=TABLES;db.executescript(SCHEMA);db.execute('BEGIN IMMEDIATE')
    try:
        case=db.execute(f"SELECT facts FROM {t['case']} WHERE id=?",(doc["case_id"],)).fetchone()
        if not case:raise ValueError("assess_first")
        facts=json.loads(case["facts"])
        if facts.get("booking_id")!=doc["booking_id"]:raise ValueError("split_booking_mismatch")
        people=facts.get("people",0)
        if people!=len(members):raise ValueError("split_party_size_mismatch")
        prior=db.execute("SELECT * FROM split_request WHERE split_id=? ORDER BY version DESC LIMIT 1",
                         (doc["split_id"],)).fetchone()
        if prior:
            prior_members=json.loads(prior["members"])
            if sorted(prior_members)!=sorted(members):raise ValueError("split_roster_conflict")
            if doc["version"]==prior["version"]:
                if prior["input_hash"]!=digest(doc):raise ValueError("split_identity_conflict")
                return _split_snapshot(db,doc["split_id"])
            if doc["version"]<prior["version"]:raise ValueError("split_version_conflict")
            if prior["input_hash"]==digest(doc):raise ValueError("split_identity_conflict")
        duplicate=db.execute("""SELECT 1 FROM member_claim WHERE member IN (%s) AND split_id!=? LIMIT 1"""
                             % ",".join("?"*len(members)),(*members,doc["split_id"])).fetchone()
        if duplicate:raise ValueError("member_held_elsewhere")
        fingerprint=digest(doc)
        if prior:
            db.execute("UPDATE split_request SET state='superseded' WHERE split_id=? AND version=?",
                       (doc["split_id"],prior["version"]))
        db.execute("INSERT INTO split_request VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (doc["split_id"],doc["version"],doc["case_id"],doc["booking_id"],people,
                    json.dumps(members,ensure_ascii=False),1 if doc["authorized"] else 0,
                    json.dumps(scope,ensure_ascii=False),json.dumps(intents,ensure_ascii=False),
                    fingerprint,"active"))
        touched=[];member_states={}
        for member in members:
            slot=by_member[member]["slot"]
            claim_id=f"{doc['split_id']}:v{doc['version']}:{member}"
            # One real one-seat hold per member. The booking itself is never
            # duplicated: a distinct backing reference keeps (slot,booking_id)
            # unique while split_request/member_claim retain the real booking.
            backing_booking=f"{doc['booking_id']}#{member}#v{doc['version']}"
            state,_=_claim_core(db,claim_id,slot,backing_booking,"standard",1)
            touched.append(slot);member_states[member]=state
        db.execute("DELETE FROM member_claim WHERE split_id=?",(doc["split_id"],))
        for member in members:
            db.execute("INSERT INTO member_claim VALUES (?,?,?,?,?)",
                       (doc["split_id"],member,by_member[member]["slot"],
                        f"{doc['split_id']}:v{doc['version']}:{member}",member_states[member]))
        _stale_split_cases(db,[doc["case_id"]])
        snapshot=_split_snapshot(db,doc["split_id"])
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
    return snapshot

def _split_snapshot(db,split_id):
    row=db.execute("SELECT * FROM split_request WHERE split_id=? AND state='active' "
                   "ORDER BY version DESC LIMIT 1",(split_id,)).fetchone()
    members={r["member"]:r["state"] for r in
             db.execute("SELECT member,state FROM member_claim WHERE split_id=?",(split_id,))}
    return {"split_id":split_id,"version":row["version"],"authorized":bool(row["authorized"]),
            "members":members}

def _reflect_claims(db,slots):
    """Synchronize member-level claims with the real state of their backing holds."""
    if not slots:return
    marks=",".join("?"*len(slots))
    for row in db.execute(f"SELECT mc.rowid AS rid,sc.state AS state FROM member_claim mc "
                          f"JOIN slot_claim sc ON sc.id=mc.claim_id WHERE mc.slot IN ({marks})",slots):
        db.execute("UPDATE member_claim SET state=? WHERE rowid=?",(row["state"],row["rid"]))

def _stale_split_cases(db,case_ids):
    t=TABLES
    for case_id in case_ids:
        db.execute(f"UPDATE {t['assessment']} SET stale=1 WHERE case_id=?",(case_id,))
        db.execute(f"UPDATE {t['obligation']} SET state='stale' WHERE assessment_id IN "
                   f"(SELECT id FROM {t['assessment']} WHERE case_id=?)",(case_id,))

def _stale_bookings(db,slot,bookings):
    t=TABLES
    affected_cases=set()
    for case in db.execute(f'SELECT id,facts FROM {t["case"]}'):
        facts=json.loads(case['facts'])
        if facts.get('replacement_slot')==slot and facts.get('booking_id') in bookings:
            affected_cases.add(case['id'])
    marks=",".join("?"*len(bookings))
    for split in db.execute("SELECT split_id,case_id FROM split_request WHERE state='active'"):
        hit=db.execute(f"SELECT 1 FROM member_claim mc JOIN slot_claim sc ON sc.id=mc.claim_id "
                       f"WHERE mc.split_id=? AND sc.slot=? AND sc.booking_id IN ({marks}) LIMIT 1",
                       (split["split_id"],slot,*bookings)).fetchone()
        if hit:affected_cases.add(split["case_id"])
    _stale_split_cases(db,affected_cases)

def held_for(db,slot,booking_id):
    row=db.execute("SELECT people FROM slot_claim WHERE slot=? AND booking_id=? AND state='held'",(slot,booking_id)).fetchone()
    return row['people'] if row else 0
