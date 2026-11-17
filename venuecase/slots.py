"""Finite replacement-session holds and priority displacement."""
import json
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
        inventory=db.execute('SELECT * FROM slot_inventory WHERE slot=?',(slot,)).fetchone()
        if not inventory or people>inventory['capacity']:raise ValueError('slot_capacity_unavailable')
        old=db.execute('SELECT * FROM slot_claim WHERE id=?',(claim_id,)).fetchone()
        if old:
            if (old['slot'],old['booking_id'],old['tier'],old['people'])!=(slot,booking_id,tier,people):
                raise ValueError('slot_claim_conflict')
            state=old['state'];displaced=[]
        else:
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
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
    return {'claim':claim_id,'state':state,'displaced':displaced}

def release(db_path,claim_id):
    db=connect(db_path);db.executescript(SCHEMA);db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT * FROM slot_claim WHERE id=?',(claim_id,)).fetchone()
        if not row or row['state']!='held':raise ValueError('slot_claim_not_held')
        db.execute("UPDATE slot_claim SET state='released' WHERE id=?",(claim_id,))
        capacity=db.execute('SELECT capacity FROM slot_inventory WHERE slot=?',(row['slot'],)).fetchone()[0]
        held=db.execute("SELECT COALESCE(sum(people),0) FROM slot_claim WHERE slot=? AND state='held'",(row['slot'],)).fetchone()[0]
        promoted=[]
        for candidate in db.execute("SELECT * FROM slot_claim WHERE slot=? AND state='waiting' ORDER BY CASE tier WHEN 'priority' THEN 0 ELSE 1 END,id",(row['slot'],)):
            if held+candidate['people']<=capacity:
                db.execute("UPDATE slot_claim SET state='held' WHERE id=?",(candidate['id'],))
                held+=candidate['people'];promoted.append(candidate['booking_id'])
        _stale_bookings(db,row['slot'],[row['booking_id'],*promoted])
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
    return {'claim':claim_id,'state':'released','promoted':promoted}

def _stale_bookings(db,slot,bookings):
    t=TABLES
    for case in db.execute(f'SELECT id,facts FROM {t["case"]}'):
        facts=json.loads(case['facts'])
        if facts.get('replacement_slot')==slot and facts.get('booking_id') in bookings:
            db.execute(f'UPDATE {t["assessment"]} SET stale=1 WHERE case_id=?',(case['id'],))
            db.execute(f"UPDATE {t['obligation']} SET state='stale' WHERE assessment_id IN "
                       f"(SELECT id FROM {t['assessment']} WHERE case_id=?)",(case['id'],))

def held_for(db,slot,booking_id):
    row=db.execute("SELECT people FROM slot_claim WHERE slot=? AND booking_id=? AND state='held'",(slot,booking_id)).fetchone()
    return row['people'] if row else 0
