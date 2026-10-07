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

python src/garmin_grafana/huawei_db_tools.py write \
  --host http://INFLUXDB_HOST:8086 --path ./imports/huawei/prepared/tracks.ns.lp.gz \
  --precision ns

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
  the independently recorded activity interval. Zero timestamps remain unknown in `ActivityGPS`. The export field dictionary
  (Motion Field description row 19) defines missing `coordinate` as GCJ02; the
  deprecated field is not required. Null/empty export placeholders use this default.
  Explicit WGS84/GCJ02 values take precedence; unsupported nonempty values remain
  `CoordinateSystem=unknown`, with approved raw display fallback and an offset warning.
  `CoordinateSystemSource` records `explicit`, `export_default`, `raw_unconfirmed`
  or `legacy`. Raw coordinates remain available unchanged.
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
units, profile ownership, actual satellite timestamps when absent, or unsupported coordinate system semantics.

## Checks

```sh
python tests/test_huawei_import.py
```

Before writing, validate types against the live database snapshot, verify sleep
stage sums and ensure unmerged minute data never enters `StepsIntraday`. After
writing, compare field counts by Source with the manifest. Garmin series have no
Huawei Source tag and must retain their existing points. The long-term dashboard
plots Huawei and Garmin daily series separately to avoid blending them.

## Ordered activity maps and GPS-only repair

`HuaweiActivityTrack` is a map-only measurement, separate from measured `ActivityGPS`.
It includes valid coordinates without satellite time. The dictionary (Motion Tag
description row 8) defines `k` as point index, and `t` as satellite time. Points
sort by `TrackPointIndex`, then original `SourceOrder`. Its Influx timestamp is
activity start plus an ordinal **nanosecond**, a storage identity only. It must
never be used to calculate pace, speed, duration or interpolated heart rate.
`SatelliteTimeKnown` is always present; `SatelliteTimeMs` exists only when real
satellite time is valid. The dashboards omit storage time from the map tooltip.

`MapEligible=true` requires at least two valid coordinate points. Huawei maps
query this measurement in table format with explicit latitude/longitude fields,
marker layers. Amap uses GCJ02, and Garmin Stats uses WGS84. The dropdown
uses SHOW TAG VALUES and the Huawei MapEligible tag, excluding activities without
a drawable Huawei route. It must return label strings, never numeric point counts.
Grafana backend variable parsing may turn aggregate numeric frames into dropdown
options when tag labels are not exposed as string fields; SELECT count subqueries
are therefore unsuitable as activity selectors.
Garmin maps retain separate neutral coordinate markers and optional metric coloring;
missing heart rate or speed cannot hide the basic route. Missing timestamps are not
written into the real-time `ActivityGPS` measurement. Preserved legacy GPX routes
retain existing coordinates, tags and identities, including activities absent from
the complete export.

For an existing complete-export import, prepare only GPS repairs:

```sh
python src/garmin_grafana/huawei_import.py \
  --export-root /path/to/HUAWEI_HEALTH_EXPORT \
  --before ./imports/huawei/original-before \
  --output ./imports/huawei/gps-prepared --gps-only
```

Use the **original pre-import snapshot** for legacy GPX matching and preservation;
do not substitute the complete-import snapshot, whose GPS points include newer
activities. Separately snapshot the current database before applying the repair.
Validate field types and existing point identities. Write `points.lp.gz` with ms
precision, and `tracks.ns.lp.gz` with ns precision. The helper infers precision
from `.ns.lp.gz` and rejects an explicit mismatched precision. Replaying either
payload retains its identities. The GPS-only payload changes only coordinate and
coordinate-provenance fields, and creates the map-only measurement. It does not
rewrite summaries, heart rate, sleep, or body records.

Keep database snapshots, source exports, prepared payloads, and dashboard backups
out of Git. Track snapshots retain nanosecond precision. For recovery, restore
the two dashboard JSON backups. Removing the new track measurement restores the
pre-repair map data state. Restoring missing GPS fields exactly requires a scoped
Huawei ActivityGPS restore from the complete pre-repair snapshot, retaining its
field types and identities; merely replaying older fields does not remove newly
added Influx fields. Review that operation before a destructive rollback.

## Avoid stale route layers and polar outliers

The installed Grafana 12.0.1 Route layer returns on empty query results without
clearing its previous line. A source-specific route can therefore remain when
switching between Huawei and Garmin activities. Use marker layers, which clear
on empty results, rather than the beta Route layer. All Huawei map queries exclude
the repeated export outlier latitude=90, longitude=-80. The importer rejects that
exact pair for new real-time and map-only points and preserved GPX map copies.
Other high-latitude coordinates remain valid; raw exports and existing real-time
GPS observations are retained for audit. No broad geographic clipping is used.
