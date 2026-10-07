"""Read snapshots or apply generated line protocol to the existing InfluxDB 1.x."""
import argparse
import gzip
import json
import urllib.parse
import urllib.request
from pathlib import Path

MEASUREMENTS = ['ActivityGPS', 'ActivitySummary', 'BodyComposition', 'HuaweiMilestones',
                'DailyStats', 'HeartRateIntraday', 'StressIntraday', 'SleepSummary',
                'SleepIntraday', 'StepsIntraday', 'SpO2Intraday', 'RestingHeartRateIntraday',
                'HuaweiSportIntraday', 'HuaweiDailySnapshot']

def query(host, sql):
    url = host + '/query?' + urllib.parse.urlencode({'db': 'GarminStats', 'q': sql, 'epoch': 'ms'})
    with urllib.request.urlopen(url, timeout=180) as response:
        data = json.load(response)
    for result in data.get('results', []):
        if 'error' in result:
            raise RuntimeError(result['error'])
    return data

def main():
    p = argparse.ArgumentParser()
    p.add_argument('mode', choices=['snapshot', 'write', 'verify'])
    p.add_argument('--host', required=True)
    p.add_argument('--path', required=True)
    a = p.parse_args()
    dest = Path(a.path)
    if a.mode == 'snapshot':
        if (dest / 'counts.json').exists():
            raise SystemExit('Snapshot already exists; choose a fresh backup directory')
        dest.mkdir(parents=True, exist_ok=True)
        baseline = {}
        for m in MEASUREMENTS:
            baseline[m] = query(a.host, f'SELECT count(*) FROM "{m}" GROUP BY "Source"')
            data = query(a.host, f'SELECT * FROM "{m}" WHERE "Source" = \'huawei\'')
            (dest / (m + '.json')).write_text(json.dumps(data, ensure_ascii=False))
        (dest / 'counts.json').write_text(json.dumps(baseline, ensure_ascii=False, indent=2))
        (dest / 'field_keys.json').write_text(json.dumps(query(a.host, 'SHOW FIELD KEYS'), ensure_ascii=False, indent=2))
        print('Snapshot saved:', str(dest), flush=True)
    elif a.mode == 'write':
        total = 0
        with gzip.open(dest, 'rb') as f:
            batch = []
            for line in f:
                batch.append(line)
                if len(batch) >= 10000:
                    write_batch(a.host, batch)
                    total += len(batch)
                    batch = []
                    if total % 100000 == 0:
                        print('Written:', total, flush=True)
            if batch:
                write_batch(a.host, batch)
                total += len(batch)
        print('Total written:', total, flush=True)
    else:
        data = {}
        for m in MEASUREMENTS:
            data[m] = query(a.host, f'SELECT count(*) FROM "{m}" GROUP BY "Source"')
        dest.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        print('Verification saved:', str(dest), flush=True)

def write_batch(host, lines):
    req = urllib.request.Request(host + '/write?db=GarminStats&precision=ms',
                                 data=b''.join(lines), method='POST')
    with urllib.request.urlopen(req, timeout=180) as response:
        if response.status != 204:
            raise RuntimeError('Unexpected write status: ' + str(response.status))

if __name__ == '__main__':
    main()
