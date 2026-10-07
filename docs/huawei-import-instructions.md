# Huawei Health complete export import

This supplements the existing GarminStats database with Huawei export JSON and XLS
tables. The importer uses Python's standard library and does not fetch account data
or install dependencies. It prepares data locally before an explicit database write.

## Run

Use the same Python environment as the project. Obtain the InfluxDB HTTP endpoint
from your existing stack; an internal Docker endpoint is sufficient. Preserve both
the original Huawei export and the backup directory.

```sh
python src/garmin_grafana/huawei_db_tools.py snapshot \
  --host http://INFLUXDB_HOST:8086 --path ./imports/huawei/before

python src/garmin_grafana/huawei_import.py \
  --export-root /path/to/HUAWEI_HEALTH_EXPORT \
  --before ./imports/huawei/before --output ./imports/huawei/prepared

python src/garmin_grafana/validate_prepared.py \
  --before ./imports/huawei/before --output ./imports/huawei/prepared

python src/garmin_grafana/huawei_db_tools.py write \
  --host http://INFLUXDB_HOST:8086 --path ./imports/huawei/prepared/points.lp.gz

python src/garmin_grafana/huawei_db_tools.py verify \
  --host http://INFLUXDB_HOST:8086 --path ./imports/huawei/after-counts.json
```

Run the database commands on a machine that can reach the endpoint. For a remote
stack, prepare locally, transfer only the generated gzip file and database helper,
and run the snapshot/write/verify steps on the server. The helper targets the
existing `GarminStats` database; it does not restart any services. Replaying the
same line protocol uses the same measurement, tags and timestamp identities.

Snapshots include existing `Source=huawei` records, counts and field types.
Keep them for comparison and recovery. They contain private data and must not
be committed. Restore requires retaining the original measurement/tag identities
and field types; review the snapshot before performing any destructive rollback.

## Mapping and reconciliation

- Dynamic/resting heart rate, stress and oxygen: select legacy type 7/11/16 once;
  new type 500023/500024/500026/400021 is an export mirror, not another measurement.
  Coincident points prefer the newer export version.
- Sleep stages: type 9 keys map deep/light/dream/wake to existing levels 0/1/2/3.
  Duration comes from each point's timestamps; contiguous same-stage intervals
  are compressed without bridging gaps. Noon sleep stays identifiable.
- Sleep summaries: XLS `totalInfo` has actual scores and stage totals. Duration
  fields are interpreted as minutes, supported by stage sums and timestamp spans;
  the inner XLS fields do not explicitly declare units. Awake time remains separate.
  Missing/zero wake timestamps and zero scores do not become fabricated records.
  The summary duration is retained even when its timestamp span differs.
- Daily heart rate/oxygen/stress: the XLS field dictionary defines `generateTime`
  as UTC, but this is a generation date rather than an original measurement day.
  Preserve these records in `HuaweiDailySnapshot`, including `totalInfoRaw`, and
  do not assign them fabricated daily dates. The dashboard uses intraday timestamps
  for heart rate and resting heart rate. Zero resting heart rate is missing.
- Weight: source `bodyWeight` is kg; target weight and mass fields are grams.
  Existing Huawei body points reuse their tags. Nonempty top-level `subUser`
  profiles are excluded conservatively, including the undocumented `-1` value;
  no meaning is guessed for `conflictFlag`. Existing points not in the new export
  are retained in the database. `skeletalMusclelMass` maps to `muscleMass` and
  source `muscleMass` to `softLeanMass`, matching the established Huawei dashboard.
- Activities: source distance is meters, duration milliseconds, calories calories,
  elevation gain/loss decimeters. Targets use meters, seconds, kcal and meters.
  Explicit sport mappings are 258 running, 264 treadmill running, 259 cycling and
  262 pool swimming; undefined codes retain `huawei_CODE`. Older GPX imports use
  the first track-point timestamp. Reuse their identity only when a unique running
  session matches start within 90 seconds, distance within 2 m and duration within
  2 seconds. Existing summaries and GPS tracks remain intact.
- GPS `t`: exports contain epoch seconds and epoch milliseconds, identified by
  the independently recorded activity interval. Zero timestamps remain unknown.
  Only explicit WGS84/GCJ02 coordinates populate map fields; unspecified coordinates
  remain `LatitudeRaw`/`LongitudeRaw` with `CoordinateSystem=unknown`.
- Minute sports: multiple devices overlap. Preserve exact source observations
  in `HuaweiSportIntraday`, tagged by device and sport. Their distance/calorie/
  duration units are unspecified and kept as `*Raw` fields. Do not SUM their
  `StepsCount` across devices. Standard `DailyStats.totalSteps` uses only the
  explicit `SPORT_GOAL_ACHIEVEMENT_DATA.stepUserValue`, never overlapping sums.
- Medals: add missing earned medals by code, level and award date. Keep historical
  lifetime totals unchanged; medal thresholds cannot replace exact lifetime totals.

The manifest lists coverage, point counts and omissions. Other export content such
as diet, plans, reminders and intensity records is outside these dashboard mappings
and remains in the original export. No completeness claim is made for unspecified
units, profile ownership, missing GPS timestamps or missing coordinate systems.

## Checks

```sh
python tests/test_huawei_import.py
```

Before writing, validate types against the live database snapshot, verify sleep
stage sums and ensure unmerged minute data never enters `StepsIntraday`. After
writing, compare field counts by Source with the manifest. Garmin series have no
Huawei Source tag and must retain their existing points. The long-term dashboard
plots Huawei and Garmin daily series separately to avoid blending them.
