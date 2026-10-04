#!/usr/bin/env python3
"""
Reusable Guangdong EMC push runner for MyEMS customers.

This file is the business-level runner: it diagnoses MyEMS data, prepares the
daily energy payload, and delegates platform HTTP details to gd_energy_push.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import gd_energy_push as core


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
PERIOD_STAT_TYPES = {"daily": "1", "monthly": "2", "yearly": "3"}


class SystemErrorWithHint(Exception):
    pass


def load_customer(path: Path) -> Dict[str, Any]:
    data = core.load_json(path)
    required = ["platform", "enterprise", "myems", "upload_item"]
    missing = [key for key in required if key not in data]
    if missing:
        raise SystemErrorWithHint(f"客户配置缺少字段: {', '.join(missing)}")
    return data


def resolve_runs_dir(config: Mapping[str, Any]) -> Path:
    runs_dir = str(config.get("runtime", {}).get("runs_dir", "gd_energy_push/runs"))
    return Path(runs_dir)


def write_run_json(config: Mapping[str, Any], label: str, data: Any) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    customer_key = str(config.get("customer_key", "customer"))
    path = resolve_runs_dir(config) / customer_key / f"{stamp}_{label}.json"
    core.save_json(path, to_jsonable(data))
    return path


def to_jsonable(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.strftime(TIME_FORMAT) if isinstance(value, dt.datetime) else value.isoformat()
    try:
        import decimal
        if isinstance(value, decimal.Decimal):
            return float(value)
    except Exception:
        pass
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [to_jsonable(v) for v in value]
    return value


def mysql_db_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    db = dict(config.get("myems", {}).get("database", {}))
    if db.get("kind") != "mysql":
        raise SystemErrorWithHint("当前业务运行器只支持 MyEMS MySQL 数据源")
    return db


def query_mysql(config: Mapping[str, Any], sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    db = mysql_db_config(config)
    db["sql"] = sql
    return core.query_mysql(db, params)


def period_window_utc(
    config: Mapping[str, Any], stat_date: str, period_type: str = "daily"
) -> Tuple[str, str, str]:
    local_start = dt.datetime.strptime(core.normalize_stat_date(stat_date), TIME_FORMAT)
    if period_type == "daily":
        local_end = local_start + dt.timedelta(days=1)
    elif period_type == "monthly":
        if local_start.day != 1:
            raise SystemErrorWithHint("Monthly stat_date must be the first day of a month")
        if local_start.month == 12:
            local_end = local_start.replace(year=local_start.year + 1, month=1)
        else:
            local_end = local_start.replace(month=local_start.month + 1)
    elif period_type == "yearly":
        if local_start.month != 1 or local_start.day != 1:
            raise SystemErrorWithHint("Yearly stat_date must be January 1")
        local_end = local_start.replace(year=local_start.year + 1)
    else:
        raise SystemErrorWithHint(f"Unsupported period type: {period_type}")
    offset_hours = int(config.get("myems", {}).get("timezone_offset_hours", 8))
    utc_start = local_start - dt.timedelta(hours=offset_hours)
    utc_end = local_end - dt.timedelta(hours=offset_hours)
    return (
        local_start.strftime(TIME_FORMAT),
        utc_start.strftime(TIME_FORMAT),
        utc_end.strftime(TIME_FORMAT),
    )


def date_window_utc(config: Mapping[str, Any], stat_date: str) -> Tuple[str, str, str]:
    return period_window_utc(config, stat_date, "daily")


def minimum_period_hours(config: Mapping[str, Any], utc_start: str, utc_end: str) -> int:
    start = dt.datetime.strptime(utc_start, TIME_FORMAT)
    end = dt.datetime.strptime(utc_end, TIME_FORMAT)
    days = max(1, (end - start).days)
    return int(config["quality"].get("minimum_daily_hours", 23)) * days


def id_list(values: Sequence[Any]) -> str:
    ids = [str(int(v)) for v in values]
    if not ids:
        raise SystemErrorWithHint("客户配置 myems.meter_ids 为空")
    return ", ".join(ids)


def diagnose(config: Mapping[str, Any]) -> Dict[str, Any]:
    myems = config["myems"]
    root_space_id = int(myems["root_space_id"])
    meters = id_list(myems.get("meter_ids", []))

    result: Dict[str, Any] = {
        "customer": config.get("customer_name"),
        "checkedAt": dt.datetime.now().strftime(TIME_FORMAT),
        "rootSpaceId": root_space_id,
        "meterIds": myems.get("meter_ids", []),
        "checks": {}
    }

    result["checks"]["space"] = query_mysql(config, """
        SELECT id, name, parent_space_id, is_input_counted, latitude, longitude
        FROM myems_system_db.tbl_spaces
        WHERE id = %s
    """, [root_space_id])

    result["checks"]["meters"] = query_mysql(config, f"""
        SELECT m.id, m.name, m.is_counted, m.energy_category_id, c.name AS category_name
        FROM myems_system_db.tbl_meters m
        LEFT JOIN myems_system_db.tbl_energy_categories c ON c.id = m.energy_category_id
        WHERE m.id IN ({meters})
        ORDER BY m.id
    """)

    result["checks"]["data_sources"] = query_mysql(config, f"""
        SELECT DISTINCT ds.id, ds.name, ds.protocol, ds.last_seen_datetime_utc, ds.connection
        FROM myems_system_db.tbl_data_sources ds
        JOIN myems_system_db.tbl_points p ON p.data_source_id = ds.id
        JOIN myems_system_db.tbl_meters_points mp ON mp.point_id = p.id
        WHERE mp.meter_id IN ({meters})
        ORDER BY ds.id
    """)

    result["checks"]["energy_points"] = query_mysql(config, f"""
        SELECT m.id AS meter_id, m.name AS meter_name, p.id AS point_id, p.name AS point_name,
               p.units, p.object_type, ev.utc_date_time AS latest_time, ev.actual_value AS latest_value
        FROM myems_system_db.tbl_meters m
        JOIN myems_system_db.tbl_meters_points mp ON mp.meter_id = m.id
        JOIN myems_system_db.tbl_points p ON p.id = mp.point_id
        LEFT JOIN myems_historical_db.tbl_energy_value_latest ev ON ev.point_id = p.id
        WHERE m.id IN ({meters})
          AND LOWER(COALESCE(p.units, '')) = 'kwh'
          AND p.object_type = 'ENERGY_VALUE'
        ORDER BY m.id, p.id
    """)

    result["checks"]["analog_latest"] = query_mysql(config, f"""
        SELECT m.id AS meter_id, m.name AS meter_name, p.id AS point_id, p.name AS point_name,
               p.units, p.object_type, av.utc_date_time AS latest_time, av.actual_value AS latest_value
        FROM myems_system_db.tbl_meters m
        JOIN myems_system_db.tbl_meters_points mp ON mp.meter_id = m.id
        JOIN myems_system_db.tbl_points p ON p.id = mp.point_id
        LEFT JOIN myems_historical_db.tbl_analog_value_latest av ON av.point_id = p.id
        WHERE m.id IN ({meters})
        ORDER BY av.utc_date_time DESC
        LIMIT 50
    """)

    result["checks"]["meter_hourly"] = query_mysql(config, f"""
        SELECT h.meter_id, COUNT(*) AS rows_count, MIN(h.start_datetime_utc) AS min_utc,
               MAX(h.start_datetime_utc) AS max_utc,
               SUM(CASE WHEN h.actual_value <> 0 THEN 1 ELSE 0 END) AS nonzero_rows,
               MAX(CASE WHEN h.actual_value <> 0 THEN h.start_datetime_utc END) AS latest_nonzero_utc
        FROM myems_energy_db.tbl_meter_hourly h
        WHERE h.meter_id IN ({meters})
        GROUP BY h.meter_id
        ORDER BY h.meter_id
    """)

    result["checks"]["space_hourly"] = query_mysql(config, """
        SELECT space_id, energy_category_id, COUNT(*) AS rows_count,
               MIN(start_datetime_utc) AS min_utc, MAX(start_datetime_utc) AS max_utc,
               SUM(CASE WHEN actual_value <> 0 THEN 1 ELSE 0 END) AS nonzero_rows,
               MAX(CASE WHEN actual_value <> 0 THEN start_datetime_utc END) AS latest_nonzero_utc
        FROM myems_energy_db.tbl_space_input_category_hourly
        WHERE space_id = %s
        GROUP BY space_id, energy_category_id
        ORDER BY energy_category_id
    """, [root_space_id])

    return result


def result_ok(value: Optional[float], row_count: int, minimum_rows: int, warnings: List[str]) -> bool:
    if value is None:
        return False
    if value < 0:
        warnings.append("计算结果为负数，拒绝生成上传报文")
        return False
    if value <= 0:
        warnings.append("计算结果为 0，拒绝生成上传报文")
        return False
    if row_count < minimum_rows:
        warnings.append(f"小时记录不足，当前 {row_count} 条，要求至少 {minimum_rows} 条")
        return False
    return True


def try_space_hourly(
    config: Mapping[str, Any], stat_date: str, period_type: str = "daily"
) -> Dict[str, Any]:
    root_space_id = int(config["myems"]["root_space_id"])
    category_id = int(config["myems"].get("energy_category_id", 1))
    local_start, utc_start, utc_end = period_window_utc(config, stat_date, period_type)
    rows = query_mysql(config, """
        SELECT COUNT(*) AS rows_count,
               SUM(actual_value) AS data_value,
               MIN(start_datetime_utc) AS min_utc,
               MAX(start_datetime_utc) AS max_utc
        FROM myems_energy_db.tbl_space_input_category_hourly
        WHERE space_id = %s
          AND energy_category_id = %s
          AND start_datetime_utc >= %s
          AND start_datetime_utc < %s
    """, [root_space_id, category_id, utc_start, utc_end])
    row = rows[0] if rows else {}
    warnings: List[str] = []
    value = row.get("data_value")
    value_float = float(value) if value is not None else None
    row_count = int(row.get("rows_count") or 0)
    ok = result_ok(value_float, row_count, minimum_period_hours(config, utc_start, utc_end), warnings)
    return {
        "strategy": "space_input_category_hourly",
        "ok": ok,
        "statDate": local_start,
        "dataValue": value_float,
        "rowCount": row_count,
        "windowUtc": {"start": utc_start, "end": utc_end},
        "raw": row,
        "warnings": warnings
    }


def try_meter_hourly(
    config: Mapping[str, Any], stat_date: str, period_type: str = "daily"
) -> Dict[str, Any]:
    meters = id_list(config["myems"].get("meter_ids", []))
    local_start, utc_start, utc_end = period_window_utc(config, stat_date, period_type)
    rows = query_mysql(config, f"""
        SELECT COUNT(*) AS rows_count,
               COUNT(DISTINCT meter_id) AS meter_count,
               SUM(actual_value) AS data_value,
               MIN(start_datetime_utc) AS min_utc,
               MAX(start_datetime_utc) AS max_utc
        FROM myems_energy_db.tbl_meter_hourly
        WHERE meter_id IN ({meters})
          AND start_datetime_utc >= %s
          AND start_datetime_utc < %s
    """, [utc_start, utc_end])
    row = rows[0] if rows else {}
    warnings: List[str] = []
    value = row.get("data_value")
    value_float = float(value) if value is not None else None
    row_count = int(row.get("rows_count") or 0)
    expected = minimum_period_hours(config, utc_start, utc_end) * len(config["myems"].get("meter_ids", []))
    ok = result_ok(value_float, row_count, expected, warnings)
    return {
        "strategy": "meter_hourly",
        "ok": ok,
        "statDate": local_start,
        "dataValue": value_float,
        "rowCount": row_count,
        "windowUtc": {"start": utc_start, "end": utc_end},
        "raw": row,
        "warnings": warnings
    }


def try_cumulative_energy_diff(
    config: Mapping[str, Any], stat_date: str, period_type: str = "daily"
) -> Dict[str, Any]:
    meters = id_list(config["myems"].get("meter_ids", []))
    local_start, utc_start, utc_end = period_window_utc(config, stat_date, period_type)
    rows = query_mysql(config, f"""
        WITH meter_points AS (
          SELECT DISTINCT m.id AS meter_id, m.name AS meter_name, p.id AS point_id
          FROM myems_system_db.tbl_meters m
          JOIN myems_system_db.tbl_meters_points mp ON mp.meter_id = m.id
          JOIN myems_system_db.tbl_points p ON p.id = mp.point_id
          WHERE m.id IN ({meters})
            AND m.is_counted = 1
            AND LOWER(COALESCE(p.units, '')) = 'kwh'
            AND p.object_type = 'ENERGY_VALUE'
        ),
        start_values AS (
          SELECT mp.point_id, ev.actual_value, ev.utc_date_time,
                 ROW_NUMBER() OVER (PARTITION BY mp.point_id ORDER BY ev.utc_date_time DESC) AS rn
          FROM meter_points mp
          JOIN myems_historical_db.tbl_energy_value ev ON ev.point_id = mp.point_id
          WHERE ev.utc_date_time <= %s
            AND ev.utc_date_time >= DATE_SUB(%s, INTERVAL 7 DAY)
            AND COALESCE(ev.is_bad, 0) = 0
        ),
        end_values AS (
          SELECT mp.point_id, ev.actual_value, ev.utc_date_time,
                 ROW_NUMBER() OVER (PARTITION BY mp.point_id ORDER BY ev.utc_date_time DESC) AS rn
          FROM meter_points mp
          JOIN myems_historical_db.tbl_energy_value ev ON ev.point_id = mp.point_id
          WHERE ev.utc_date_time <= %s
            AND ev.utc_date_time >= DATE_SUB(%s, INTERVAL 7 DAY)
            AND COALESCE(ev.is_bad, 0) = 0
        )
        SELECT COUNT(mp.point_id) AS point_count,
               SUM(CASE WHEN ev.actual_value >= sv.actual_value THEN ev.actual_value - sv.actual_value ELSE NULL END) AS data_value,
               SUM(CASE WHEN sv.actual_value IS NULL OR ev.actual_value IS NULL THEN 1 ELSE 0 END) AS missing_pairs,
               SUM(CASE WHEN ev.actual_value < sv.actual_value THEN 1 ELSE 0 END) AS negative_pairs
        FROM meter_points mp
        LEFT JOIN start_values sv ON sv.point_id = mp.point_id AND sv.rn = 1
        LEFT JOIN end_values ev ON ev.point_id = mp.point_id AND ev.rn = 1
    """, [utc_start, utc_start, utc_end, utc_end])
    row = rows[0] if rows else {}
    warnings: List[str] = []
    value = row.get("data_value")
    value_float = float(value) if value is not None else None
    point_count = int(row.get("point_count") or 0)
    missing_pairs = int(row.get("missing_pairs") or 0)
    negative_pairs = int(row.get("negative_pairs") or 0)
    if point_count == 0:
        warnings.append("未找到 ENERGY_VALUE/kWh 累计电能点")
    if missing_pairs:
        warnings.append(f"有 {missing_pairs} 个累计电能点缺少起止读数")
    if negative_pairs:
        warnings.append(f"有 {negative_pairs} 个累计电能点出现倒走")
    if value_float is not None and value_float <= 0:
        warnings.append("计算结果为 0，拒绝生成上传报文")
    ok = point_count > 0 and missing_pairs == 0 and negative_pairs == 0 and value_float is not None and value_float > 0
    return {
        "strategy": "cumulative_energy_diff",
        "ok": ok,
        "statDate": local_start,
        "dataValue": value_float,
        "pointCount": point_count,
        "windowUtc": {"start": utc_start, "end": utc_end},
        "raw": row,
        "warnings": warnings
    }


def prepare_period(
    config: Mapping[str, Any], stat_date: str, period_type: str = "daily"
) -> Dict[str, Any]:
    if period_type not in PERIOD_STAT_TYPES:
        raise SystemErrorWithHint(f"Unsupported period type: {period_type}")
    _, _, period_end_utc = period_window_utc(config, stat_date, period_type)
    if dt.datetime.strptime(period_end_utc, TIME_FORMAT) > dt.datetime.utcnow():
        raise SystemErrorWithHint(f"The requested {period_type} period is not complete yet")
    strategies = list(config.get("myems", {}).get("strategies", []))
    attempts = []
    functions = {
        "space_input_category_hourly": try_space_hourly,
        "meter_hourly": try_meter_hourly,
        "cumulative_energy_diff": try_cumulative_energy_diff,
    }
    selected = None
    for strategy in strategies:
        if strategy not in functions:
            attempts.append({"strategy": strategy, "ok": False, "warnings": ["系统暂未实现该策略"]})
            continue
        attempt = functions[strategy](config, stat_date, period_type)
        attempts.append(attempt)
        if attempt["ok"]:
            selected = attempt
            break

    payload = None
    if selected:
        item = dict(config["upload_item"])
        item.update({
            "dataValue": selected["dataValue"],
            "statType": PERIOD_STAT_TYPES[period_type],
            "statDate": selected["statDate"],
            "uploadDate": dt.datetime.now().strftime(TIME_FORMAT)
        })
        core.validate_energy_item({
            "dataCode": item["dataCode"],
            "dataValue": core.normalize_decimal_string(item["dataValue"]),
            "inputType": str(item["inputType"]),
            "statType": str(item["statType"]),
            "statDate": core.normalize_stat_date(item["statDate"]),
            "scope": str(item["scope"]),
            "valid": core.normalize_bool_string(item["valid"]),
            "uploadDate": core.normalize_stat_date(item["uploadDate"]),
        })
        payload = {
            "enterpriseCode": config["enterprise"]["enterpriseCode"],
            "data": [{
                "dataCode": item["dataCode"],
                "dataValue": core.normalize_decimal_string(item["dataValue"]),
                "inputType": str(item["inputType"]),
                "statType": str(item["statType"]),
                "statDate": core.normalize_stat_date(item["statDate"]),
                "scope": str(item["scope"]),
                "valid": core.normalize_bool_string(item["valid"]),
                "uploadDate": core.normalize_stat_date(item["uploadDate"]),
            }],
            "deviceId": config["enterprise"]["deviceId"]
        }

    return {
        "customer": config.get("customer_name"),
        "preparedAt": dt.datetime.now().strftime(TIME_FORMAT),
        "periodType": period_type,
        "selectedStrategy": selected["strategy"] if selected else None,
        "attempts": attempts,
        "payload": payload,
        "canUpload": payload is not None
    }


def prepare_daily(config: Mapping[str, Any], stat_date: str) -> Dict[str, Any]:
    return prepare_period(config, stat_date, "daily")


def download_platform_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    payload = {
        "enterpriseCode": config["enterprise"]["enterpriseCode"],
        "deviceId": config["enterprise"]["deviceId"],
    }
    response = core.http_post_json(config, core.endpoint_url(config, "download_config"), payload)
    core.assert_success(response)
    return response


def validate_payload_against_platform(payload: Mapping[str, Any], platform_config: Mapping[str, Any]) -> None:
    allowed = set()
    for config_item in platform_config.get("collectItemConfig") or []:
        if not isinstance(config_item, Mapping):
            continue
        configured_code = core.data_code_from_config_item(config_item)
        configured_stat_type = str(config_item.get("statType", "")).strip()
        if configured_code and configured_stat_type:
            allowed.add((configured_code, configured_stat_type))
    for item in payload.get("data", []):
        data_code = item.get("dataCode")
        stat_type = str(item.get("statType", "")).strip()
        if (data_code, stat_type) not in allowed:
            raise SystemErrorWithHint(f"省平台 collectItemConfig 中没有 dataCode: {data_code}")


def upload_payload(config: Mapping[str, Any], payload: Mapping[str, Any]) -> Dict[str, Any]:
    response = core.http_post_json(config, core.endpoint_url(config, "upload_energy"), payload)
    core.assert_success(response)
    return response


def default_stat_date_yesterday() -> str:
    return (dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")


def default_period_stat_date(period_type: str) -> str:
    now = dt.datetime.now()
    if period_type == "daily":
        result = now - dt.timedelta(days=1)
    elif period_type == "monthly":
        current_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        result = (current_month - dt.timedelta(days=1)).replace(day=1)
    elif period_type == "yearly":
        current_year = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        result = current_year.replace(year=current_year.year - 1)
    else:
        raise SystemErrorWithHint(f"Unsupported period type: {period_type}")
    return result.strftime(TIME_FORMAT)


def catch_up_state_path(config: Mapping[str, Any]) -> Path:
    customer_key = str(config.get("customer_key", "customer"))
    return resolve_runs_dir(config) / customer_key / "catch_up_state.json"


def load_catch_up_state(config: Mapping[str, Any]) -> Dict[str, Any]:
    path = catch_up_state_path(config)
    if not path.exists():
        return {"confirmed": {"daily": [], "monthly": []}}
    state = core.load_json(path)
    confirmed = state.setdefault("confirmed", {})
    confirmed.setdefault("daily", [])
    confirmed.setdefault("monthly", [])
    return state


def save_catch_up_state(config: Mapping[str, Any], state: Mapping[str, Any]) -> None:
    data = dict(state)
    data["updatedAt"] = dt.datetime.now().strftime(TIME_FORMAT)
    core.save_json(catch_up_state_path(config), to_jsonable(data))


def add_month(value: dt.datetime) -> dt.datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


def catch_up_targets(config: Mapping[str, Any], period_type: str) -> List[str]:
    settings = config.get("catch_up", {})
    now = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    targets: List[str] = []
    if period_type == "daily":
        start_text = str(settings.get("daily_start_date", "2026-07-02"))
        current = dt.datetime.strptime(core.normalize_stat_date(start_text), TIME_FORMAT)
        end = now - dt.timedelta(days=1)
        while current <= end:
            targets.append(current.strftime("%Y-%m-%d"))
            current += dt.timedelta(days=1)
        return targets
    if period_type == "monthly":
        start_text = str(settings.get("monthly_start_date", "2026-07-01"))
        current = dt.datetime.strptime(core.normalize_stat_date(start_text), TIME_FORMAT)
        if current.day != 1:
            raise SystemErrorWithHint("catch_up.monthly_start_date must be the first day of a month")
        current_month = now.replace(day=1)
        while current < current_month:
            targets.append(current.strftime("%Y-%m-%d"))
            current = add_month(current)
        return targets
    raise SystemErrorWithHint(f"Unsupported catch-up period type: {period_type}")


def download_period_data(
    config: Mapping[str, Any], period_type: str, stat_date: str
) -> Dict[str, Any]:
    payload = {
        "enterpriseCode": config["enterprise"]["enterpriseCode"],
        "deviceId": config["enterprise"]["deviceId"],
        "statType": PERIOD_STAT_TYPES[period_type],
        "statDate": core.normalize_stat_date(stat_date),
    }
    response = core.http_post_json(config, core.endpoint_url(config, "download_energy"), payload)
    core.assert_success(response)
    return response


def platform_contains_item(
    config: Mapping[str, Any], period_type: str, stat_date: str, response: Mapping[str, Any]
) -> bool:
    expected_code = str(config["upload_item"]["dataCode"])
    expected_type = PERIOD_STAT_TYPES[period_type]
    expected_date = core.normalize_stat_date(stat_date)
    for item in response.get("data") or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("dataCode")) != expected_code:
            continue
        if str(item.get("statType")) != expected_type:
            continue
        if core.normalize_stat_date(item.get("statDate")) == expected_date:
            return True
    return False


def command_diagnose(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    data = diagnose(config)
    path = write_run_json(config, "diagnose", data)
    print(core.json_dumps(to_jsonable({"saved": str(path), "summary": summarize_diagnose(data)})))
    return 0


def summarize_diagnose(data: Mapping[str, Any]) -> Dict[str, Any]:
    checks = data.get("checks", {})
    return {
        "meters": len(checks.get("meters", [])),
        "dataSources": len(checks.get("data_sources", [])),
        "energyPoints": len(checks.get("energy_points", [])),
        "meterHourlyGroups": len(checks.get("meter_hourly", [])),
        "spaceHourlyGroups": len(checks.get("space_hourly", []))
    }


def command_prepare(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    data = prepare_daily(config, args.stat_date or default_stat_date_yesterday())
    path = write_run_json(config, "prepare", data)
    print(core.json_dumps(to_jsonable({"saved": str(path), "canUpload": data["canUpload"], "selectedStrategy": data["selectedStrategy"], "payload": data["payload"], "attempts": data["attempts"]})))
    return 0 if data["canUpload"] else 3


def command_prepare_period(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    stat_date = args.stat_date or default_period_stat_date(args.period_type)
    data = prepare_period(config, stat_date, args.period_type)
    path = write_run_json(config, f"prepare_{args.period_type}", data)
    print(core.json_dumps(to_jsonable({
        "saved": str(path),
        "periodType": args.period_type,
        "canUpload": data["canUpload"],
        "selectedStrategy": data["selectedStrategy"],
        "payload": data["payload"],
        "attempts": data["attempts"],
    })))
    return 0 if data["canUpload"] else 3


def command_run_daily(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    stat_date = args.stat_date or default_stat_date_yesterday()
    prepared = prepare_daily(config, stat_date)
    prepare_path = write_run_json(config, "prepare", prepared)
    result: Dict[str, Any] = {"prepareFile": str(prepare_path), "canUpload": prepared["canUpload"]}
    if not prepared["canUpload"]:
        result["message"] = "当前没有通过质量检查的日电量数据，未上传"
        print(core.json_dumps(to_jsonable(result)))
        return 3

    platform_config = None
    if config.get("runtime", {}).get("download_platform_config_before_upload", True):
        platform_config = download_platform_config(config)
        platform_path = write_run_json(config, "platform_config", platform_config)
        validate_payload_against_platform(prepared["payload"], platform_config)
        result["platformConfigFile"] = str(platform_path)

    if not args.confirm_upload:
        result["message"] = "已生成并校验报文；未加 --confirm-upload，所以没有真正上传"
        result["payload"] = prepared["payload"]
        print(core.json_dumps(to_jsonable(result)))
        return 0

    response = upload_payload(config, prepared["payload"])
    upload_path = write_run_json(config, "upload_response", response)
    result["uploaded"] = True
    result["uploadResponseFile"] = str(upload_path)
    result["response"] = response
    print(core.json_dumps(to_jsonable(result)))
    return 0


def command_run_period(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    stat_date = args.stat_date or default_period_stat_date(args.period_type)
    prepared = prepare_period(config, stat_date, args.period_type)
    prepare_path = write_run_json(config, f"prepare_{args.period_type}", prepared)
    result: Dict[str, Any] = {
        "periodType": args.period_type,
        "prepareFile": str(prepare_path),
        "canUpload": prepared["canUpload"],
    }
    if not prepared["canUpload"]:
        result["message"] = "No complete period data passed quality checks; nothing was uploaded."
        print(core.json_dumps(to_jsonable(result)))
        return 3

    if config.get("runtime", {}).get("download_platform_config_before_upload", True):
        platform_config = download_platform_config(config)
        platform_path = write_run_json(config, f"platform_config_{args.period_type}", platform_config)
        validate_payload_against_platform(prepared["payload"], platform_config)
        result["platformConfigFile"] = str(platform_path)

    if not args.confirm_upload:
        result["message"] = "Payload prepared and validated; --confirm-upload was not supplied."
        result["payload"] = prepared["payload"]
        print(core.json_dumps(to_jsonable(result)))
        return 0

    response = upload_payload(config, prepared["payload"])
    upload_path = write_run_json(config, f"upload_response_{args.period_type}", response)
    result["uploaded"] = True
    result["uploadResponseFile"] = str(upload_path)
    result["response"] = response
    print(core.json_dumps(to_jsonable(result)))
    return 0


def catch_up_one_period(
    config: Mapping[str, Any], period_type: str, confirm_upload: bool
) -> Dict[str, Any]:
    state = load_catch_up_state(config)
    confirmed = set(str(value) for value in state["confirmed"][period_type])
    targets = catch_up_targets(config, period_type)
    statuses: List[Dict[str, Any]] = []
    platform_config = None

    for stat_date in targets:
        if stat_date in confirmed:
            continue
        response = download_period_data(config, period_type, stat_date)
        if platform_contains_item(config, period_type, stat_date, response):
            status = "already_present"
        else:
            prepared = prepare_period(config, stat_date, period_type)
            if not prepared["canUpload"]:
                statuses.append({
                    "statDate": stat_date,
                    "status": "pending_source_data",
                    "attempts": prepared["attempts"],
                })
                continue
            if platform_config is None:
                platform_config = download_platform_config(config)
            validate_payload_against_platform(prepared["payload"], platform_config)
            if not confirm_upload:
                statuses.append({
                    "statDate": stat_date,
                    "status": "missing_dry_run",
                    "payload": prepared["payload"],
                })
                continue
            upload_response = upload_payload(config, prepared["payload"])
            verify_response = download_period_data(config, period_type, stat_date)
            if not platform_contains_item(config, period_type, stat_date, verify_response):
                raise SystemErrorWithHint(
                    f"Upload returned success but platform verification is empty: {period_type} {stat_date}"
                )
            status = "uploaded_and_verified"
            write_run_json(config, f"catch_up_{period_type}_{stat_date}_upload", upload_response)

        confirmed.add(stat_date)
        state["confirmed"][period_type] = sorted(confirmed)
        save_catch_up_state(config, state)
        statuses.append({"statDate": stat_date, "status": status})

    return {
        "periodType": period_type,
        "targetCount": len(targets),
        "previouslyConfirmedCount": len([value for value in targets if value in confirmed]) - len(
            [item for item in statuses if item["status"] in {"already_present", "uploaded_and_verified"}]
        ),
        "checked": statuses,
        "pendingCount": len([item for item in statuses if item["status"].startswith("pending")]),
    }


def command_catch_up(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    period_types = ["daily", "monthly"] if args.period_type == "all" else [args.period_type]
    results = [catch_up_one_period(config, period_type, args.confirm_upload) for period_type in period_types]
    audit_path = write_run_json(config, "catch_up", {"results": results})
    print(core.json_dumps(to_jsonable({"auditFile": str(audit_path), "results": results})))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reusable MyEMS to Guangdong EMC push system.")
    parser.add_argument("--customer", required=True, help="客户配置 JSON，例如 gd_energy_push/customers/weifu.json")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("diagnose", help="检查客户 MyEMS 数据、点位、汇总表状态")

    prepare = sub.add_parser("prepare-daily", help="清洗并生成某天 uploadEnergyData 报文，不上传")
    prepare.add_argument("--stat-date", help="本地自然日，例如 2026-06-04；默认昨天")

    run_daily = sub.add_parser("run-daily", help="每日任务：生成报文、校验平台配置，可选确认上传")
    run_daily.add_argument("--stat-date", help="本地自然日，例如 2026-06-04；默认昨天")
    run_daily.add_argument("--confirm-upload", action="store_true", help="真正调用省平台 uploadEnergyData")
    prepare_period_parser = sub.add_parser(
        "prepare-period", help="Prepare a monthly or yearly payload without uploading"
    )
    prepare_period_parser.add_argument("--period-type", required=True, choices=["monthly", "yearly"])
    prepare_period_parser.add_argument("--stat-date", help="Period start: YYYY-MM-01 or YYYY-01-01")

    run_period = sub.add_parser(
        "run-period", help="Prepare, validate, and optionally upload monthly or yearly data"
    )
    run_period.add_argument("--period-type", required=True, choices=["monthly", "yearly"])
    run_period.add_argument("--stat-date", help="Period start: YYYY-MM-01 or YYYY-01-01")
    run_period.add_argument("--confirm-upload", action="store_true")

    catch_up = sub.add_parser(
        "catch-up", help="Find missing daily/monthly platform records and safely backfill them"
    )
    catch_up.add_argument("--period-type", default="all", choices=["daily", "monthly", "all"])
    catch_up.add_argument("--confirm-upload", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_customer(Path(args.customer))
        if args.command == "diagnose":
            return command_diagnose(args, config)
        if args.command == "prepare-daily":
            return command_prepare(args, config)
        if args.command == "run-daily":
            return command_run_daily(args, config)
        if args.command == "prepare-period":
            return command_prepare_period(args, config)
        if args.command == "run-period":
            return command_run_period(args, config)
        if args.command == "catch-up":
            return command_catch_up(args, config)
    except (core.PushError, SystemErrorWithHint) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
