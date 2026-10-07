"""Verify generated point types and source-separation before database writes."""
import argparse
import collections
import json
import sqlite3
from pathlib import Path
from huawei_import import previous

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--before',type=Path,required=True)
    a=p.parse_args()
    db=sqlite3.connect(a.output/'normalized.sqlite')
    schema={}
    for result in json.loads((a.before/'field_keys.json').read_text())['results']:
        for series in result.get('series',[]):
            schema[series['name']]={v[0]:v[1] for v in series['values']}
    types=collections.defaultdict(set)
    professional=0
    for m,tags,ms,data in db.execute('SELECT measurement,tags,time,fields FROM points'):
        assert json.loads(tags)['Source']=='huawei'
        assert 946684800000<ms<4102444800000
        fields=json.loads(data)
        for key,value in fields.items():
            typ='integer' if isinstance(value,int) else 'float' if isinstance(value,float) else 'string'
            types[(m,key)].add(typ)
            expected=schema.get(m,{}).get(key)
            assert expected is None or expected==typ,(m,key,expected,typ)
        if m=='SleepSummary' and 'remSleepSeconds' in fields:
            assert fields['sleepTimeSeconds']==sum(fields.get(k,0) for k in ['deepSleepSeconds','lightSleepSeconds','remSleepSeconds'])
            professional+=1
    assert all(len(t)==1 for t in types.values())
    assert db.execute("SELECT count(*) FROM points WHERE measurement='StepsIntraday'").fetchone()[0]==0
    official_steps=db.execute("SELECT count(*) FROM points WHERE measurement='DailyStats' AND json_extract(fields,'$.totalSteps') IS NOT NULL").fetchone()[0]
    assert official_steps<=619
    body=previous(a.before,'BodyComposition')
    absent=0
    for old in body:
        rows=db.execute("SELECT tags,fields FROM points WHERE measurement='BodyComposition' AND time=?",(old['time'],)).fetchall()
        if not rows:
            absent+=1
            continue
        tags,fields=map(json.loads,rows[0])
        assert tags['Device']==old['Device'] and tags['SourceType']==old['SourceType']
        for key in ['weight','bodyFat','muscleMass','boneMass']:
            if fields.get(key) is not None and old.get(key) is not None:
                tolerance=1. if key in ['weight','muscleMass','boneMass'] else .01
                assert abs(fields[key]-old[key])<=tolerance,(old['time'],key,fields[key],old[key])
    manifest=json.loads((a.output/'manifest.json').read_text())
    result={'schema_type_conflicts':0,'professional_sleep_summaries_checked':professional,
            'official_daily_steps_points':official_steps,'existing_body_points_matched':len(body)-absent,
            'existing_body_points_not_in_payload_but_retained_in_database':absent,
            'legacy_gpx_identities_reused':manifest['skipped'].get('legacy_gpx_identity_reused',0),
            'no_unmerged_steps_in_standard_measurement':True,
            'generateTime_timezone_evidence':'SportsHealth field description, user health daily statistics, row 8: UTC'}
    (a.output/'validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
