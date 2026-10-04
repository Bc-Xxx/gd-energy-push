WITH RECURSIVE
params AS (
  SELECT
    /* Set this to the customer's MyEMS root space_id. */
    0 AS root_space_id,
    CAST(%s AS DATETIME) AS stat_date_local
),
time_window AS (
  SELECT
    root_space_id,
    stat_date_local,
    DATE_SUB(stat_date_local, INTERVAL 8 HOUR) AS start_utc,
    DATE_ADD(DATE_SUB(stat_date_local, INTERVAL 8 HOUR), INTERVAL 1 DAY) AS end_utc
  FROM params
),
tree AS (
  SELECT s.id AS root_space_id, s.name AS root_space_name, s.id AS space_id
  FROM myems_system_db.tbl_spaces s
  JOIN params p ON p.root_space_id = s.id
  UNION ALL
  SELECT tree.root_space_id, tree.root_space_name, s.id AS space_id
  FROM tree
  JOIN myems_system_db.tbl_spaces s ON s.parent_space_id = tree.space_id
),
meter_points AS (
  SELECT DISTINCT
    tree.root_space_id,
    tree.root_space_name,
    m.id AS meter_id,
    m.name AS meter_name,
    p.id AS point_id
  FROM tree
  JOIN myems_system_db.tbl_spaces_meters sm ON sm.space_id = tree.space_id
  JOIN myems_system_db.tbl_meters m ON m.id = sm.meter_id
  JOIN myems_system_db.tbl_meters_points mp ON mp.meter_id = m.id
  JOIN myems_system_db.tbl_points p ON p.id = mp.point_id
  WHERE m.energy_category_id = 1
    AND m.is_counted = 1
    AND LOWER(p.units) = 'kwh'
    AND p.object_type = 'ENERGY_VALUE'
),
start_values AS (
  SELECT
    mp.point_id,
    ev.actual_value,
    ev.utc_date_time,
    ROW_NUMBER() OVER (PARTITION BY mp.point_id ORDER BY ev.utc_date_time DESC) AS rn
  FROM meter_points mp
  JOIN time_window tw ON tw.root_space_id = mp.root_space_id
  JOIN myems_historical_db.tbl_energy_value ev ON ev.point_id = mp.point_id
  WHERE ev.utc_date_time <= tw.start_utc
    AND ev.utc_date_time >= DATE_SUB(tw.start_utc, INTERVAL 7 DAY)
    AND COALESCE(ev.is_bad, 0) = 0
),
end_values AS (
  SELECT
    mp.point_id,
    ev.actual_value,
    ev.utc_date_time,
    ROW_NUMBER() OVER (PARTITION BY mp.point_id ORDER BY ev.utc_date_time DESC) AS rn
  FROM meter_points mp
  JOIN time_window tw ON tw.root_space_id = mp.root_space_id
  JOIN myems_historical_db.tbl_energy_value ev ON ev.point_id = mp.point_id
  WHERE ev.utc_date_time <= tw.end_utc
    AND ev.utc_date_time >= DATE_SUB(tw.end_utc, INTERVAL 7 DAY)
    AND COALESCE(ev.is_bad, 0) = 0
),
diffs AS (
  SELECT
    mp.root_space_id,
    mp.root_space_name,
    mp.meter_id,
    mp.meter_name,
    mp.point_id,
    sv.actual_value AS start_value,
    ev.actual_value AS end_value,
    ev.actual_value - sv.actual_value AS diff_value
  FROM meter_points mp
  LEFT JOIN start_values sv ON sv.point_id = mp.point_id AND sv.rn = 1
  LEFT JOIN end_values ev ON ev.point_id = mp.point_id AND ev.rn = 1
)
SELECT
  'ELEC_TOTAL' AS source_key,
  ROUND(SUM(CASE WHEN diff_value >= 0 AND diff_value < 10000000 THEN diff_value ELSE 0 END), 4) AS data_value,
  DATE_FORMAT((SELECT stat_date_local FROM params), '%%Y-%%m-%%d %%H:%%i:%%s') AS stat_date
FROM diffs
HAVING data_value IS NOT NULL;
