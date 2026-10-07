"""Prepare a Huawei Health export for GarminStats; stdlib only, no database writes.

One format is selected for mirror datasets. Existing Huawei identities are reused.
Unspecified minute distance/calorie units remain raw. Missing GPS coordinate
systems use the GCJ02 default documented in the export field dictionary.
"""
import argparse
import collections
import gzip
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
try:
    from .huawei_xls import extract
except ImportError:
    from huawei_xls import extract

UTC = timezone.utc
BASE_TAGS = {'Database_Name': 'GarminStats', 'Device': 'HUAWEI (import)', 'Source': 'huawei'}
STAGES = {'PROFESSIONAL_SLEEP_DEEP': 0., 'PROFESSIONAL_SLEEP_SHALLOW': 1.,
          'PROFESSIONAL_SLEEP_DREAM': 2., 'PROFESSIONAL_SLEEP_WAKE': 3.,
          'PROFESSIONAL_SLEEP_NOON': 1.}
SPORTS = {258: 'running', 264: 'treadmill_running', 259: 'cycling', 262: 'lap_swimming'}

def decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value

def zone(value):
    s = str(value).strip('"\t ').replace(':', '')
    if len(s) != 5 or s[0] not in '+-':
        raise ValueError('Unknown timezone: ' + repr(value))
    return timezone(timedelta(minutes=(int(s[1:3])*60+int(s[3:5]))*(1 if s[0]=='+' else -1)))

def day_time(ms, tz):
    dt = datetime.fromtimestamp(ms/1000, tz=zone(tz))
    return int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()*1000)

def rows(path):
    if not path.exists():
        return
    for sheet in extract(path):
        if not sheet['rows']:
            continue
        header = sheet['rows'][0]
        for row in sheet['rows'][1:]:
            yield dict(zip(header, row))

def previous(path, name):
    if not path or not (path/(name+'.json')).exists():
        return []
    data = json.loads((path/(name+'.json')).read_text())
    return [dict(zip(s['columns'], v)) for r in data.get('results', [])
            for s in r.get('series', []) for v in s.get('values', [])]

class Store:
    def __init__(self, path, resume=False):
        self.db = sqlite3.connect(path)
        if not resume:
            self.db.execute('CREATE TABLE points (measurement TEXT, tags TEXT, time INTEGER, fields TEXT, rank INTEGER, PRIMARY KEY(measurement,tags,time)) WITHOUT ROWID')
            self.db.execute('CREATE TABLE sleep (time INTEGER PRIMARY KEY, end INTEGER, stage TEXT, rank INTEGER)')
        self.db.execute('CREATE TABLE IF NOT EXISTS tracks (measurement TEXT, tags TEXT, time INTEGER, fields TEXT, rank INTEGER, PRIMARY KEY(measurement,tags,time)) WITHOUT ROWID')
        self.skipped = collections.Counter()
        self.conflicts = collections.Counter()
    def add(self, m, ms, fields, tags=None, rank=0):
        if not ms or ms < 946684800000:
            raise ValueError('Invalid point timestamp')
        self._add('points', m, ms, fields, tags, rank)
    def add_track(self, start_ms, ordinal, fields, tags, rank=0):
        # This is a storage identity/order, NOT a satellite timestamp. Keep it
        # outside the millisecond table so missing timestamps never become GPS.
        self._add('tracks', 'HuaweiActivityTrack', start_ms*1000000+ordinal,
                  fields, tags, rank)
    def _add(self, table, m, stamp, fields, tags, rank):
        fs = {k:v for k,v in fields.items() if v is not None and
              not (isinstance(v, float) and not math.isfinite(v))}
        if not fs:
            return
        ts = json.dumps(tags or BASE_TAGS, sort_keys=True, ensure_ascii=False)
        old = self.db.execute(f'SELECT fields, rank FROM {table} WHERE measurement=? AND tags=? AND time=?',(m,ts,stamp)).fetchone()
        if old:
            prior = json.loads(old[0])
            if any(k in prior and prior[k] != v for k,v in fs.items()):
                self.conflicts[m] += 1
            if rank < old[1]:
                fs = {**fs, **prior}
                rank = old[1]
            else:
                fs = {**prior, **fs}
        self.db.execute(f'INSERT OR REPLACE INTO {table} VALUES(?,?,?,?,?)',
                        (m,ts,int(stamp),json.dumps(fs,ensure_ascii=False),int(rank)))
    def sleep(self, start, end, stage, rank):
        old = self.db.execute('SELECT rank FROM sleep WHERE time=?',(start,)).fetchone()
        if not old or rank >= old[0]:
            self.db.execute('INSERT OR REPLACE INTO sleep VALUES(?,?,?,?)',(start,end,stage,rank))
    def commit(self):
        self.db.commit()

def health(root, store, before, types=None):
    old_body = {r['time']:r for r in previous(before, 'BodyComposition')}
    keep_types = types if types is not None else {7,8,9,11,16,300002}
    for path in sorted((root/'Health detail data & description').glob('*.json')):
        records = json.loads(path.read_text())
        for r in records:
            if r['type'] not in keep_types:
                continue
            if r.get('subUser') is not None:
                store.skipped['health_subuser_records'] += 1
                continue
            rank = r.get('version', 0)
            for p in r['samplePoints']:
                ms, end, key = p['startTime'],p['endTime'],p['key']
                v = decode(p.get('value'))
                if key == 'DATA_POINT_DYNAMIC_HEARTRATE':
                    if 0 < float(v) < 255:
                        store.add('HeartRateIntraday',ms,{'HeartRate':int(v)},rank=rank)
                    else:
                        store.skipped['invalid_heart_rate']+=1
                elif key in ('DATA_POINT_REST_HEARTRATE','DATA_POINT_NEW_REST_HEARTRATE'):
                    if 0 < float(v) < 255:
                        store.add('RestingHeartRateIntraday',ms,{'restingHeartRate':int(v)},rank=rank)
                elif key in STAGES:
                    if end <= ms:
                        raise ValueError('Non-positive sleep interval')
                    store.sleep(ms,end,key,rank)
                elif key == 'STRESS_DATA' and isinstance(v,dict):
                    score=v.get('score')
                    if score is not None and 0 <= score <= 100:
                        store.add('StressIntraday',ms,{'stressLevel':int(score)},rank=rank)
                elif key == 'BLOOD_OXYGEN_SATURATION' and isinstance(v,dict):
                    spo2=v.get('avgSaturation')
                    if spo2 is not None and 0 < spo2 <= 100:
                        store.add('SpO2Intraday',ms,{'spo2':float(spo2)},rank=rank)
                elif key == 'WEIGHT_BODYFAT_BROAD' and isinstance(v,dict):
                    weight=v.get('bodyWeight')
                    if weight is None or not 1 <= weight <= 500:
                        store.skipped['invalid_weight']+=1
                        continue
                    mapping={'bodyWeight':('weight',1000), 'bodyFatRate':('bodyFat',1),
                             'boneSalt':('boneMass',1000), 'skeletalMusclelMass':('muscleMass',1000),
                             'muscleMass':('softLeanMass',1000), 'moistureRate':('bodyWater',1),
                             'visceralFatLevel':('visceralFat',1)}
                    fields={target:float(v[k])*scale for k,(target,scale) in mapping.items() if v.get(k) is not None}
                    for k in ['bmi','bodyAge','bodyScore','basalMetabolism','proteinRate','impedance']:
                        if v.get(k) is not None:
                            fields[k]=float(v[k])
                    tags={**BASE_TAGS,'Frequency':'Intraday','SourceType':'HUAWEI_EXPORT'}
                    if ms in old_body:
                        tags={k:old_body[ms][k] for k in ['Database_Name','Device','Frequency','Source','SourceType'] if old_body[ms].get(k) is not None}
                    store.add('BodyComposition',ms,fields,tags,rank)
                elif key == 'SPORT_GOAL_ACHIEVEMENT_DATA' and isinstance(v,dict):
                    if v.get('stepUserValue') is not None:
                        store.add('DailyStats',day_time(ms,r['timeZone']),{'totalSteps':int(v['stepUserValue'])},rank=rank)
        del records
        store.commit()
        print('Health:',path.name,flush=True)

def sleep_stages(store):
    current=None
    for ms,end,key,rank in store.db.execute('SELECT * FROM sleep ORDER BY time'):
        if current and current[1]==ms and current[2]==key:
            current[1]=end
            continue
        if current:
            emit_sleep(store,current)
        current=[ms,end,key]
    if current:
        emit_sleep(store,current)
    store.commit()

def emit_sleep(store, interval):
    ms,end,key=interval
    store.add('SleepIntraday',ms,{'SleepStageLevel':STAGES[key],
               'SleepStageSeconds':int((end-ms)/1000),'HuaweiSleepStage':key})

def daily(root,store):
    source=root/'SportsHealth data & desciption'/'user health daily statistics.xls'
    records=list(rows(source))
    professional=[]
    for r in records:
        v=decode(r.get('totalInfo'));kind=int(r['type'])
        if kind==9 and isinstance(v,dict) and v.get('wakeupTime') and v.get('fallAsleepTime'):
            professional.append((v['fallAsleepTime'],v['wakeupTime']))
    for r in records:
        kind=int(r['type']);v=decode(r.get('totalInfo'))
        if not isinstance(v,dict):
            store.skipped['daily_unparsed_totalInfo']+=1
            continue
        if kind in (3,9):
            end=v.get('wakeupTime') or v.get('endTime');start=v.get('fallAsleepTime') or v.get('startTime')
            if not end or not start or end<=start:
                store.skipped['sleep_summary_invalid_time']+=1
                continue
            if kind==3 and any(a<end and start<b for a,b in professional):
                store.skipped['basic_sleep_overlaps_professional']+=1
                continue
            if kind==9:
                mapping={'allSleepTime':'sleepTimeSeconds','deepSleepTime':'deepSleepSeconds',
                         'lightSleepTime':'lightSleepSeconds','dreamTime':'remSleepSeconds',
                         'awakeTime':'awakeSleepSeconds','daySleepTime':'noonSleepSeconds'}
                if v['allSleepTime'] != sum(v.get(k,0) for k in ['deepSleepTime','lightSleepTime','dreamTime']):
                    raise ValueError('Sleep stage total mismatch')
                fields={target:int(v[k]*60) for k,target in mapping.items() if k in v}
                if v.get('sleepScore',0)>0:
                    fields['sleepScore']=int(v['sleepScore'])
                if v.get('wakeupCnt') is not None:
                    fields['awakeCount']=int(v['wakeupCnt'])
            else:
                mapping={'totalDuration':'sleepTimeSeconds','deepDuration':'deepSleepSeconds',
                         'lightDuration':'lightSleepSeconds','awakeDuration':'awakeSleepSeconds'}
                fields={target:int(v[k]*60) for k,target in mapping.items() if k in v}
                if v.get('awakeTimes') is not None:
                    fields['awakeCount']=int(v['awakeTimes'])
            fields['HuaweiSleepStartTime']=int(start)
            store.add('SleepSummary',end,fields,rank=kind)
            continue
        if kind not in (7,16,11):
            continue
        generate=r.get('generateTime')
        if not generate or generate=='0':
            store.skipped['daily_invalid_generateTime']+=1
            continue
        dt=datetime.strptime(str(generate),'%Y%m%d %H:%M:%S').replace(tzinfo=UTC)
        stamp=int(dt.timestamp()*1000)
        if kind==7:
            mapping={'maxHeartRate':'maxHeartRate','minHeartRate':'minHeartRate','avgRestingHeartRate':'restingHeartRate'}
            fields={target:int(v[k]) for k,target in mapping.items() if v.get(k,0)>0}
        elif kind==16:
            fields={}
            if v.get('avgSaturation',0)>0:fields['averageSpo2']=float(v['avgSaturation'])
            if v.get('minSaturation',0)>0:fields['lowestSpo2']=int(v['minSaturation'])
        else:
            fields={target:float(v[k]) for k,target in {'meanScore':'HuaweiMeanStress','minScore':'HuaweiMinStress','maxScore':'HuaweiMaxStress'}.items() if v.get(k) is not None}
        # generateTime is a UTC generation timestamp, not a measurement day.
        # Preserve these snapshots separately instead of inventing daily dates.
        fields['totalInfoRaw']=r['totalInfo']
        store.add('HuaweiDailySnapshot',stamp,fields,
                  {**BASE_TAGS,'HuaweiSnapshotType':str(kind)},rank=stamp)
    store.commit()

def minutes(root,store):
    path=root/'Sport per minute merged data & description'/'sport per minute merged data.json'
    if not path.exists():return
    for day in json.loads(path.read_text()):
        for r in day['sportDataUserData']:
            fields={'durationSeconds':int((r['endTime']-r['startTime'])/1000)}
            for k in ['steps','distance','calorie','altitude','floor','duration','count']:
                vals=[v[k] for v in r['sportBasicInfos'] if v.get(k) is not None]
                if vals:fields[k+'Raw']=float(sum(vals))
            tags={**BASE_TAGS,'HuaweiDeviceCode':str(r['deviceCode']),'HuaweiSportType':str(r['sportType'])}
            store.add('HuaweiSportIntraday',r['startTime'],fields,tags,r['version'])
            if fields.get('stepsRaw') is not None:
                # Multiple device sources overlap. Keep exact source observations
                # outside StepsIntraday, whose dashboard SUM would double count.
                store.add('HuaweiSportIntraday',r['startTime'],{'StepsCount':int(fields['stepsRaw'])},tags,r['version'])
        store.commit()

def gcj_to_wgs(lon,lat):
    if not (72.004 <= lon <= 137.8347 and .8293 <= lat <= 55.8271):return lon,lat
    x,y=lon-105,lat-35
    dlat=-100+2*x+3*y+.2*y*y+.1*x*y+.2*math.sqrt(abs(x))
    dlat+=(20*math.sin(6*x*math.pi)+20*math.sin(2*x*math.pi))*2/3
    dlat+=(20*math.sin(y*math.pi)+40*math.sin(y*math.pi/3))*2/3
    dlat+=(160*math.sin(y*math.pi/12)+320*math.sin(y*math.pi/30))*2/3
    dlon=300+x+2*y+.1*x*x+.1*x*y+.1*math.sqrt(abs(x))
    dlon+=(20*math.sin(6*x*math.pi)+20*math.sin(2*x*math.pi))*2/3
    dlon+=(20*math.sin(x*math.pi)+40*math.sin(x*math.pi/3))*2/3
    dlon+=(150*math.sin(x*math.pi/12)+300*math.sin(x*math.pi/30))*2/3
    rad=lat/180*math.pi;magic=1-.00669342162296594323*math.sin(rad)**2
    dlat=dlat*180/((6378245*(1-.00669342162296594323))/(magic*math.sqrt(magic))*math.pi)
    dlon=dlon*180/(6378245/math.sqrt(magic)*math.cos(rad)*math.pi)
    return lon-dlon,lat-dlat

def coordinate_system(value):
    # Motion export: Field description row 19, default GCJ02, deprecated field.
    if value is None or str(value).strip().lower() in ('', '(null)', 'null'):
        return 'GCJ02', 'export_default'
    if value in ('GCJ02', 'WGS84'):
        return value, 'explicit'
    return 'unknown', 'raw_unconfirmed'

def valid_coordinate(lat, lon):
    # Repeated export outlier (90,-80) would draw every local route to the pole.
    # Exclude this exact pair, not all high-latitude or long-distance tracks.
    return (math.isfinite(lat) and math.isfinite(lon) and
            -90<=lat<=90 and -180<=lon<=180 and
            (lat,lon) not in ((0.,0.),(90.,-80.)))

def map_coordinates(lat, lon, system):
    if system == 'GCJ02':
        wlon,wlat=gcj_to_wgs(lon,lat)
        return {'Latitude':wlat,'Longitude':wlon,'LatitudeGCJ':lat,'LongitudeGCJ':lon}
    if system == 'WGS84':
        # Iteratively invert GCJ->WGS rather than assuming a single shift is exact.
        glon,glat=lon,lat
        for _ in range(4):
            wlon,wlat=gcj_to_wgs(glon,glat)
            glon+=lon-wlon;glat+=lat-wlat
        return {'Latitude':lat,'Longitude':lon,'LatitudeGCJ':glat,'LongitudeGCJ':glon}
    # Approved display fallback; provenance remains unknown, no false claim.
    return {'Latitude':lat,'Longitude':lon,'LatitudeGCJ':lat,'LongitudeGCJ':lon}

def satellite_time(value, start, end):
    try:
        stamp=float(value)
    except (TypeError,ValueError):
        return None
    if not math.isfinite(stamp) or stamp <= 0:
        return None
    if start-86400000<=stamp*1000<=end+86400000:
        stamp*=1000
    return int(stamp) if start-86400000<=stamp<=end+86400000 else None

def emit_track(store, start, points, tags, rank=0):
    # Original k is an index, not elapsed time (Tag description row 8).
    # Tie-break by source order for duplicate k; storage ordinal stays unique.
    points=sorted(points,key=lambda p:(p.get('TrackPointIndex',0),p.get('SourceOrder',0)))
    tags={**tags,'MapEligible':'true' if len(points)>=2 else 'false'}
    for ordinal,fields in enumerate(points):
        store.add_track(start,ordinal,fields,tags,rank)

def motion(root,store,before,gps_only=False):
    old_summaries={r['ActivityID']:r for r in previous(before,'ActivitySummary') if r.get('activityType')!='No Activity'}
    old_tracks=collections.defaultdict(list)
    for old in previous(before,'ActivityGPS'):
        if old.get('Latitude') is not None and old.get('Longitude') is not None:
            old_tracks[old['ActivityID']].append(old)
    old_gps=set(old_tracks)
    old_ends={r['ActivityID']:r['time'] for r in previous(before,'ActivitySummary') if r.get('activityType')=='No Activity'}
    identities=set()
    for path in sorted((root/'Motion path detail data & description').glob('*.json')):
        for r in json.loads(path.read_text()):
            s=decode(r.get('summaryData'));start,end=r['startTime'],r['endTime'];aid=str(start//1000)
            if not isinstance(s,dict):continue
            identities.add(aid)
            code=s.get('sportType');kind=SPORTS.get(code,'huawei_'+str(code))
            # Older GPX imports used the first track-point time as identity.
            # Match only a unique running session with the same distance/duration.
            candidates=[o for o in old_summaries.values() if code==258 and
                        o.get('activityType')=='running' and abs(o['time']-start)<=90000 and
                        abs(o.get('distance',-99999)-s.get('totalDistance',0))<=2 and
                        abs(o.get('elapsedDuration',-99999)-s.get('totalTime',0)/1000)<=2]
            if aid not in old_summaries and len(candidates)==1:
                aid=candidates[0]['ActivityID']
                store.skipped['legacy_gpx_identity_reused']+=1
            dt=datetime.fromtimestamp(start/1000,UTC)
            selector=dt.strftime('%Y%m%dT%H%M%SUTC-')+kind
            tags={**BASE_TAGS,'ActivityID':aid,'ActivitySelector':selector}
            name=dt.strftime('%Y%m%d ')+kind
            old=old_summaries.get(aid)
            if old:
                tags={k:old[k] for k in ['Database_Name','Device','Source','ActivityID','ActivitySelector'] if old.get(k) is not None}
                name,kind=old['activityName'],old['activityType']
            summary_start=old['time'] if old else start
            summary_end=old_ends.get(aid,end) if old else end
            fields={'Activity_ID':int(aid),'Device_ID':0,'activityName':name,'activityType':kind,
                    'HuaweiSportType':int(code),'distance':float(s.get('totalDistance',0)),
                    'elapsedDuration':float(s.get('totalTime',end-start))/1000,
                    'movingDuration':float(s.get('totalTime',end-start))/1000,
                    'calories':float(s.get('totalCalories',0))/1000}
            for key,target in [('avgHeartRate','averageHR'),('maxHeartRate','maxHR')]:
                if s.get(key,0)>0:fields[target]=float(s[key])
            if s.get('avgPace',0)>0:fields['averageSpeed']=1000/float(s['avgPace'])
            # Never substitute sentinel values for missing elevation.
            if s.get('mTotalDescent',-1)>=0:fields['elevationLoss']=float(s['mTotalDescent'])/10
            if s.get('creepingWave',-1)>=0:fields['elevationGain']=float(s['creepingWave'])/10
            if old:
                for k in list(fields):
                    if old.get(k) is not None:
                        # Influx JSON renders integral-valued floats as ints.
                        fields[k]=float(old[k]) if isinstance(fields[k],float) else old[k]
            if not gps_only:
                store.add('ActivitySummary',summary_start,fields,tags,r.get('version',0))
                store.add('ActivitySummary',summary_end,{'Activity_ID':int(aid),'Device_ID':0,'activityName':'END','activityType':'No Activity'},tags,r.get('version',0))
            if aid in old_gps:
                store.skipped['existing_gps_activity_preserved']+=1
                continue
            coord,provenance=coordinate_system(s.get('coordinate'))
            track=[]
            for order,line in enumerate(r.get('detailData','').splitlines()):
                parts=dict(p.split('=',1) for p in line.removeprefix('DETAIL_NULL').split(';') if '=' in p)
                tp=parts.get('tp');f={'Activity_ID':int(aid),'ActivityName':kind}
                if tp=='lbs':
                    stamp=satellite_time(parts.get('t'),start,end)
                    try:lat,lon=float(parts['lat']),float(parts['lon'])
                    except (KeyError,ValueError):
                        store.skipped['gps_invalid_coordinate']+=1
                        continue
                    if not valid_coordinate(lat,lon):
                        store.skipped['gps_invalid_coordinate']+=1
                        continue
                    f.update({'LatitudeRaw':lat,'LongitudeRaw':lon,'CoordinateSystem':coord,
                              'CoordinateSystemSource':provenance,**map_coordinates(lat,lon,coord)})
                    if parts.get('alt') is not None and -500<float(parts['alt'])<9000:f['Altitude']=float(parts['alt'])
                    try:index=int(parts.get('k',order))
                    except ValueError:index=order
                    track.append({**f,'TrackPointIndex':index,'SourceOrder':order,
                                  'SatelliteTimeKnown':stamp is not None,'SatelliteTimeMs':stamp})
                    if stamp is None:
                        store.skipped['gps_map_only_without_satellite_time']+=1
                        continue
                    f['DurationSeconds']=(stamp-start)/1000
                elif tp=='h-r':
                    if gps_only:continue
                    stamp=float(parts.get('k',0));val=float(parts.get('v',0))
                    if not (start-300000<=stamp<=end+300000 and 0<val<255):continue
                    f['HeartRate']=val
                else:
                    continue
                if gps_only:
                    f={k:v for k,v in f.items() if k in ('Latitude','Longitude','LatitudeGCJ','LongitudeGCJ',
                                                        'LatitudeRaw','LongitudeRaw','CoordinateSystem','CoordinateSystemSource')}
                store.add('ActivityGPS',int(stamp),f,tags,r.get('version',0))
            emit_track(store,start,track,tags,r.get('version',0))
        store.commit()
    # Include all preserved GPX routes, also the activity absent from this export.
    for aid,points in old_tracks.items():
        old=old_summaries.get(aid)
        if not old:continue
        tags={k:old[k] for k in ['Database_Name','Device','Source','ActivityID','ActivitySelector'] if old.get(k) is not None}
        route=[]
        for i,p in enumerate(sorted(points,key=lambda p:p['time'])):
            lat,lon=float(p['Latitude']),float(p['Longitude'])
            if not valid_coordinate(lat,lon):continue
            fs=map_coordinates(lat,lon,'WGS84')
            for k in ['LatitudeGCJ','LongitudeGCJ']:
                if p.get(k) is not None:fs[k]=float(p[k])
            route.append({**fs,'LatitudeRaw':lat,'LongitudeRaw':lon,'CoordinateSystem':'WGS84',
                          'CoordinateSystemSource':'legacy','TrackPointIndex':i,'SourceOrder':i,
                          'SatelliteTimeKnown':True,'SatelliteTimeMs':int(p['time']),
                          'Activity_ID':int(aid),'ActivityName':old['activityType']})
        emit_track(store,int(old['time']),route,tags)
    store.commit()
    store.identities=len(identities)

def medals(root,store,before):
    old={r.get('medal'):r for r in previous(before,'HuaweiMilestones') if r.get('medal')}
    for r in rows(root/'SportsHealth data & desciption'/'user medal info.xls'):
        if float(r.get('gainCount') or 0)<=0 or not r.get('takeDate'):continue
        medal=str(r['medalType'])+'_'+str(r['medalLevel'])
        if medal in old:continue
        date=str(r['takeDate']).strip('"\t ')
        try:ms=int(datetime.fromisoformat(date.replace('Z','+00:00')).timestamp()*1000)
        except ValueError:
            store.skipped['medal_invalid_date']+=1;continue
        title='华为勋章 '+medal
        tags={'Database_Name':'GarminStats','Source':'huawei','kind':'medal','medal':medal}
        store.add('HuaweiMilestones',ms,{'title':title,'text':'导出勋章代码 '+medal,'value':float(r['medalLevel'])},tags)
    store.commit()

def line_token(s):
    return str(s).replace('\\','\\\\').replace(' ','\\ ').replace(',','\\,').replace('=','\\=').replace('\n',' ')

def field_token(v):
    if isinstance(v,bool):return str(v).lower()
    if isinstance(v,int):return str(v)+'i'
    if isinstance(v,float):return repr(v)
    return json.dumps(str(v),ensure_ascii=False)

def export(store,out):
    counts={};field_counts={}
    with gzip.open(out/'points.lp.gz','wt',encoding='utf-8') as f:
        for m,tags,ms,fields in store.db.execute('SELECT measurement,tags,time,fields FROM points ORDER BY measurement,tags,time'):
            tags=json.loads(tags);fields=json.loads(fields)
            header=line_token(m)+''.join(','+line_token(k)+'='+line_token(v) for k,v in sorted(tags.items()))
            f.write(header+' '+','.join(line_token(k)+'='+field_token(v) for k,v in sorted(fields.items()))+' '+str(ms)+'\n')
    for m,c,lo,hi in store.db.execute('SELECT measurement,count(*),min(time),max(time) FROM points GROUP BY measurement'):
        counts[m]={'points':c,'first_utc':datetime.fromtimestamp(lo/1000,UTC).isoformat(),'last_utc':datetime.fromtimestamp(hi/1000,UTC).isoformat()}
        fs=collections.Counter()
        for (data,) in store.db.execute('SELECT fields FROM points WHERE measurement=?',(m,)):
            fs.update(json.loads(data).keys())
        field_counts[m]=dict(fs)
    report={'measurements':counts,'field_counts':field_counts,'skipped':dict(store.skipped),
            'value_conflicts_resolved_by_version':dict(store.conflicts),
            'activities':store.identities,'total_points':sum(c['points'] for c in counts.values()),
            'line_protocol_sha256':hashlib.sha256((out/'points.lp.gz').read_bytes()).hexdigest()}
    track_fields=collections.Counter()
    track_count=0;eligible=set();all_routes=set()
    with gzip.open(out/'tracks.ns.lp.gz','wt',encoding='utf-8') as f:
        for m,tags,ns,fields in store.db.execute('SELECT measurement,tags,time,fields FROM tracks ORDER BY tags,time'):
            tags=json.loads(tags);fields=json.loads(fields)
            header=line_token(m)+''.join(','+line_token(k)+'='+line_token(v) for k,v in sorted(tags.items()))
            f.write(header+' '+','.join(line_token(k)+'='+field_token(v) for k,v in sorted(fields.items()))+' '+str(ns)+'\n')
            track_count+=1;track_fields.update(fields.keys())
            all_routes.add(tags['ActivityID'])
            if tags['MapEligible']=='true':eligible.add(tags['ActivityID'])
    report['tracks']={'measurement':'HuaweiActivityTrack','precision':'ns','points':track_count,
                      'activities':len(all_routes),'eligible_activities':len(eligible),
                      'field_counts':dict(track_fields),
                      'line_protocol_sha256':hashlib.sha256((out/'tracks.ns.lp.gz').read_bytes()).hexdigest(),
                      'storage_time_is_satellite_time':False}
    (out/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--export-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--before',type=Path)
    p.add_argument('--gps-only',action='store_true',help='Prepare only GPS repairs and ordered map tracks')
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    db=a.output/'normalized.sqlite'
    if db.exists():raise SystemExit('Use a fresh output directory; existing normalized.sqlite is preserved')
    store=Store(db)
    if not a.gps_only:
        health(a.export_root,store,a.before)
        sleep_stages(store)
        daily(a.export_root,store)
        minutes(a.export_root,store)
    motion(a.export_root,store,a.before,gps_only=a.gps_only)
    if not a.gps_only:medals(a.export_root,store,a.before)
    export(store,a.output)

if __name__=='__main__':main()
