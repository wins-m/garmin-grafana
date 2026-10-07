import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"/"garmin_grafana"))
from huawei_import import Store, health, sleep_stages, day_time, motion, minutes, coordinate_system, map_coordinates, satellite_time, export

class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.store=Store(self.root/'data.sqlite')
    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()
    def records(self, name):
        return [(t,json.loads(f)) for t,f in self.store.db.execute('SELECT time,fields FROM points WHERE measurement=? ORDER BY time',(name,))]
    def test_mirror_latest_version_and_other_profile(self):
        folder=self.root/'Health detail data & description';folder.mkdir()
        start=1600000000000
        def rec(type_,key,value,version,sub=None):
            r={'type':type_,'version':version,'timeZone':'+0800','samplePoints':[{'startTime':start,'endTime':start+60000,'key':key,'value':value}]}
            if sub is not None:r['subUser']=sub
            return r
        data=[rec(7,'DATA_POINT_DYNAMIC_HEARTRATE',70,2),rec(7,'DATA_POINT_DYNAMIC_HEARTRATE',60,1),
              rec(500023,'DYNAMIC_HEART_RATE','{"bpm":70}',2),
              rec(8,'WEIGHT_BODYFAT_BROAD',{'bodyWeight':65,'conflictFlag':1},2),
              rec(8,'WEIGHT_BODYFAT_BROAD',{'bodyWeight':50},3,'other-profile')]
        (folder/'data.json').write_text(json.dumps(data))
        health(self.root,self.store,None)
        self.assertEqual(self.records('HeartRateIntraday'),[(start,{'HeartRate':70})])
        self.assertEqual(self.records('BodyComposition'),[(start,{'weight':65000.})])
        self.assertEqual(self.store.skipped['health_subuser_records'],1)
    def test_contiguous_sleep_without_crossing_gaps(self):
        start=1600000000000
        self.store.sleep(start,start+60000,'PROFESSIONAL_SLEEP_DEEP',1)
        self.store.sleep(start+60000,start+120000,'PROFESSIONAL_SLEEP_DEEP',1)
        self.store.sleep(start+180000,start+240000,'PROFESSIONAL_SLEEP_SHALLOW',1)
        sleep_stages(self.store)
        rows=self.records('SleepIntraday')
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0][1]['SleepStageSeconds'],120)
        self.assertEqual(rows[1][1]['SleepStageSeconds'],60)
    def test_midnight_offset(self):
        self.assertEqual(day_time(1600000000000,'+0800'),1599926400000)
    def test_overlapping_devices_never_double_count_standard_steps(self):
        folder=self.root/'Sport per minute merged data & description';folder.mkdir()
        data=[]
        for device,steps in [(1,10),(2,20)]:
            data.append({'startTime':1600000000000,'endTime':1600000060000,'deviceCode':device,
                         'sportType':5,'version':1,'sportBasicInfos':[{'steps':steps}]})
        (folder/'sport per minute merged data.json').write_text(json.dumps([{'sportDataUserData':data}]))
        minutes(self.root,self.store)
        self.assertEqual(self.records('StepsIntraday'),[])
        self.assertEqual(len(self.records('HuaweiSportIntraday')),2)
    def test_reuse_gpx_identity_without_changing_float_schema(self):
        start=1600000000000;folder=self.root/'Motion path detail data & description';folder.mkdir()
        record={'startTime':start,'endTime':start+120000,'summaryData':{'sportType':258,'totalDistance':400,'totalTime':120000,'totalCalories':25000},'detailData':'','version':1}
        (folder/'data.json').write_text(json.dumps([record]))
        before=self.root/'before';before.mkdir();old_time=start+2000;aid=str(old_time//1000)
        columns=['time','ActivityID','ActivitySelector','Database_Name','Device','Source','activityName','activityType','distance','elapsedDuration']
        values=[old_time,aid,'legacy-running','GarminStats','HUAWEI (import)','huawei','old-name','running',400,120]
        (before/'ActivitySummary.json').write_text(json.dumps({'results':[{'series':[{'columns':columns,'values':[values]}]}]}))
        motion(self.root,self.store,before)
        summary=self.records('ActivitySummary')[0]
        self.assertEqual(summary[0],old_time)
        self.assertEqual(summary[1]['activityName'],'old-name')
        self.assertIsInstance(summary[1]['distance'],float)
        self.assertEqual(self.store.skipped['legacy_gpx_identity_reused'],1)
    def test_gpx_identity_seconds_timestamp_and_height(self):
        start=1600000000000;end=start+120000
        folder=self.root/'Motion path detail data & description';folder.mkdir()
        summary={'sportType':258,'totalDistance':400,'totalTime':120000,'totalCalories':25000,
                 'coordinate':'WGS84','mTotalDescent':100,'creepingWave':200}
        detail=f'tp=lbs;k=0;lat=30;lon=120;alt=10;t={start/1000+1};\ntp=lbs;k=1;lat=30;lon=120;t=0;'
        (folder/'motion.json').write_text(json.dumps([{'startTime':start,'endTime':end,'summaryData':summary,'detailData':detail,'version':1}]))
        motion(self.root,self.store,None)
        fields=self.records('ActivitySummary')[0][1]
        self.assertEqual(fields['elevationGain'],20.)
        self.assertEqual(fields['elevationLoss'],10.)
        self.assertEqual(fields['calories'],25.)
        gps=self.records('ActivityGPS')
        self.assertEqual(gps[0][0],start+1000)
        self.assertEqual(gps[0][1]['Latitude'],30.)
        self.assertEqual(len(gps),1)
        tracks=[json.loads(f) for (f,) in self.store.db.execute('SELECT fields FROM tracks ORDER BY time')]
        self.assertEqual(len(tracks),2)
        self.assertFalse(tracks[1]['SatelliteTimeKnown'])
        self.assertNotIn('SatelliteTimeMs',tracks[1])
    def test_coordinate_dictionary_default_and_unconfirmed_fallback(self):
        for v in [None,'','(null)','null']:
            self.assertEqual(coordinate_system(v),('GCJ02','export_default'))
        self.assertEqual(coordinate_system('WGS84'),('WGS84','explicit'))
        self.assertEqual(coordinate_system('BD09'),('unknown','raw_unconfirmed'))
        mapped=map_coordinates(39.98,116.31,'GCJ02')
        self.assertEqual(mapped['LatitudeGCJ'],39.98)
        self.assertGreater(abs(mapped['Longitude']-116.31),.001)
        roundtrip=map_coordinates(mapped['Latitude'],mapped['Longitude'],'WGS84')
        self.assertAlmostEqual(roundtrip['LatitudeGCJ'],39.98,places=8)
        self.assertAlmostEqual(roundtrip['LongitudeGCJ'],116.31,places=8)
        self.assertEqual(map_coordinates(40.,116.,'unknown')['Longitude'],116.)
    def test_satellite_time_scales_and_missing(self):
        start=1600000000000;end=start+120000
        self.assertEqual(satellite_time(start/1000,start,end),start)
        self.assertEqual(satellite_time(start,start,end),start)
        for v in [0,-1,'nan','bad',None,start-90000000]:
            self.assertIsNone(satellite_time(v,start,end))
    def test_map_only_points_order_invalid_coordinates_and_replay(self):
        start=1600000000000;folder=self.root/'Motion path detail data & description';folder.mkdir()
        details='\n'.join(['tp=lbs;k=2;lat=40;lon=116;t=0;',
                           'tp=lbs;k=0;lat=40.001;lon=116.001;t=0;',
                           'tp=lbs;k=1;lat=0;lon=0;t=0;',
                           'tp=lbs;k=3;lat=nan;lon=116;t=0;',
                           'tp=lbs;k=4;lat=91;lon=116;t=0;'])
        (folder/'motion.json').write_text(json.dumps([{'startTime':start,'endTime':start+120000,
          'summaryData':{'sportType':258},'detailData':details}]))
        motion(self.root,self.store,None,gps_only=True)
        motion(self.root,self.store,None,gps_only=True)
        self.assertEqual(self.records('ActivityGPS'),[])
        self.assertEqual(self.records('ActivitySummary'),[])
        tracks=[(json.loads(t),ns,json.loads(f)) for t,ns,f in self.store.db.execute('SELECT tags,time,fields FROM tracks ORDER BY time')]
        self.assertEqual(len(tracks),2)
        self.assertEqual([p[2]['TrackPointIndex'] for p in tracks],[0,2])
        self.assertEqual([p[1] for p in tracks],[start*1000000,start*1000000+1])
        self.assertEqual(tracks[0][0]['MapEligible'],'true')
        self.assertEqual(tracks[0][2]['CoordinateSystemSource'],'export_default')
        export(self.store,self.root)
        import gzip
        lines=gzip.open(self.root/'tracks.ns.lp.gz','rt').read().splitlines()
        self.assertEqual(len(lines),2)
        self.assertTrue(lines[1].endswith(str(start*1000000+1)))
        self.assertEqual(json.loads((self.root/'manifest.json').read_text())['tracks']['precision'],'ns')
    def test_legacy_route_included_when_absent_from_export(self):
        before=self.root/'before';before.mkdir();start=1600000000000
        base={'ActivityID':str(start//1000),'ActivitySelector':'legacy-running','Database_Name':'GarminStats','Device':'HUAWEI (import)','Source':'huawei'}
        def save(name,rows):
            cols=list(rows[0]);data={'results':[{'series':[{'columns':cols,'values':[[r.get(k) for k in cols] for r in rows]}]}]}
            (before/(name+'.json')).write_text(json.dumps(data))
        save('ActivitySummary',[{**base,'time':start,'activityType':'running'}])
        save('ActivityGPS',[{**base,'time':start+i*1000,'Latitude':40.,'Longitude':116.+i*.001,'LatitudeGCJ':40.001,'LongitudeGCJ':116.006+i*.001} for i in range(2)])
        motion(self.root,self.store,before,gps_only=True)
        rows=[json.loads(f) for (f,) in self.store.db.execute('SELECT fields FROM tracks ORDER BY time')]
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['CoordinateSystemSource'],'legacy')
        self.assertEqual(rows[0]['LongitudeGCJ'],116.006)
        self.assertEqual(self.records('ActivityGPS'),[])

if __name__=='__main__':unittest.main()
