"""Map queries must retain coordinates without optional metrics or fake time."""
import json
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]/'Grafana_Dashboard'

class GPSDashboardTests(unittest.TestCase):
    def test_maps_and_selectors(self):
        for name,ids in [('Health-Longterm-Dashboard',{12}),('Garmin-Grafana-Dashboard',{49,50})]:
            dashboard=json.loads((ROOT/(name+'.json')).read_text())
            variable=next(v for v in dashboard['templating']['list'] if v['name']=='ActivityGPS')
            self.assertIn('SHOW TAG VALUES',variable['definition'])
            self.assertIn('WITH KEY = "ActivitySelector"',variable['definition'])
            self.assertNotIn('count(',variable['definition'])
            self.assertEqual(variable['refresh'],2)
            for panel in dashboard['panels']:
                if panel['id'] not in ids:continue
                with self.subTest(name=name,panel=panel['id']):
                    h=next(t for t in panel['targets'] if t['refId']=='H')
                    self.assertTrue(h['rawQuery'])
                    self.assertEqual(h['resultFormat'],'table')
                    self.assertIn('"HuaweiActivityTrack"',h['query'])
                    self.assertIn('MapEligible',h['query'])
                    self.assertIn('${ActivityGPS:regex}',h['query'])
                    self.assertIn('CoordinateSystemSource',h['query'])
                    # Optional fields would drop rows in Influx's table parser.
                    for field in ['SatelliteTimeMs','HeartRate','Speed']:
                        self.assertNotIn(field,h['query'])
                    self.assertTrue(any(l['type']=='markers' and l['filterData']['options']=='H' for l in panel['options']['layers']))
                    self.assertFalse(any(t['id']=='joinByField' for t in panel['transformations']))
                    self.assertEqual(panel['transformations'][0]['options']['sort'][0]['field'],'TrackPointIndex')
                    self.assertIn('Time',panel['transformations'][1]['options']['exclude']['names'])
                    self.assertIn('raw_unconfirmed',panel['description'])
                    self.assertFalse(any(l['type']=='route' for l in panel['options']['layers']))
                    self.assertIn('"LatitudeRaw" != 90 OR "LongitudeRaw" != -80',h['query'])
                    if name.startswith('Health'):
                        self.assertIn('LatitudeGCJ',h['query'])
                    else:
                        self.assertNotIn('LatitudeGCJ',h['query'])
                        g=next(t for t in panel['targets'] if t['refId']=='G')
                        self.assertIn('Source',g['query'])
                        self.assertNotIn('HeartRate',g['query'])
                        self.assertNotIn('Speed',g['query'])

if __name__=='__main__':unittest.main()
