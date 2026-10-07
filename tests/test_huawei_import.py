import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"/"garmin_grafana"))
from huawei_import import Store, health, sleep_stages, day_time, motion, minutes

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

if __name__=='__main__':unittest.main()
