import json
from pathlib import Path
from venuecase.adoption import adopt
from venuecase.assessment import assess
from venuecase.settlement import settle
from venuecase.report import report
from venuecase.slots import configure,claim,release

FIX=Path(__file__).resolve().parent.parent/'fixtures'

def changed(tmp_path,name,mutate):
    doc=json.loads((FIX/name).read_text(encoding='utf-8'));mutate(doc)
    path=tmp_path/(str(len(list(tmp_path.glob('*.json'))))+'.json')
    path.write_text(json.dumps(doc),encoding='utf-8');return path

def test_wrong_section_and_unpaid_booking_have_distinct_outcomes(tmp_path):
    db=tmp_path/'state.db';adopt(db,FIX/'opening_normal.json')
    other=changed(tmp_path,'booking_normal.json',lambda d:(d.update(case_id='other'),d['facts'].update(section='hall-B')))
    assert assess(db,other)['outcome']=='original_entry_valid'
    unpaid=changed(tmp_path,'booking_normal.json',lambda d:(d.update(case_id='unpaid'),d['facts'].update(paid=False)))
    assert assess(db,unpaid)['outcome']=='booking_unproven'

def test_priority_claim_displaces_standard_right_and_rebuilds_assessment(tmp_path):
    normal=json.loads((FIX/'slot_normal.json').read_text(encoding='utf-8'))
    conflict=json.loads((FIX/'slot_conflict.json').read_text(encoding='utf-8'))
    db=tmp_path/'state.db';adopt(db,FIX/'opening_normal.json');configure(db,**normal['slot'])
    assert claim(db,**normal['standard'])['state']=='held'
    assert assess(db,FIX/'booking_normal.json')['outcome']=='replacement_slot_assigned'
    priority=claim(db,**conflict['priority'])
    assert priority['displaced']==['B1']
    assert report(db)['cases'][0]['stale'] is True
    assert assess(db,FIX/'booking_normal.json')['outcome']=='entry_right_unfulfilled'
    assert release(db,'CL-B2')['promoted']==['B1']
    assert assess(db,FIX/'booking_normal.json')['outcome']=='replacement_slot_assigned'

def test_wrong_slot_or_action_cannot_redeem_rebooked_right(tmp_path):
    db=tmp_path/'state.db';adopt(db,FIX/'opening_normal.json');assess(db,FIX/'booking_normal.json')
    assert settle(db,FIX/'entry_duplicate.json')['status']=='overdue'
    assert report(db)['cases'][0]['obligations'][0]['receipts'][0]['verdict']=='invalid'
    wrong_section=changed(tmp_path,'entry_normal.json',lambda d:d['receipts'][0].update(event_key='wrong-section',section='hall-B'))
    assert settle(db,wrong_section)['status']=='pending'
    corrected=changed(tmp_path,'entry_normal.json',lambda d:d['receipts'][0].update(event_key='scan-correct'))
    assert settle(db,corrected)['status']=='complete'

def test_capacity_change_can_keep_original_entry_right(tmp_path):
    notice=changed(tmp_path,'opening_normal.json',lambda d:d['payload'].update(action='capacity',available=3))
    db=tmp_path/'state.db';adopt(db,notice)
    assert assess(db,FIX/'booking_normal.json')['outcome']=='reduced_capacity_honors_booking'
