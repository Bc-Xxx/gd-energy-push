#!/usr/bin/env python3
"""
Guangdong Energy Management Center data push helper.

This script intentionally uses the Python standard library for HTTP and
SQLite. For SQL Server/MySQL/Oracle via ODBC, install pyodbc and set
database.kind to "odbc" in the config file.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import decimal
import json
import os
import re
import ssl
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


DATA_CODE_RE = re.compile(r"^\d{2}-\d{2}-\d{4}-\d{6}-\d{2}$")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

ENDPOINTS = {
    "register": "/energy-interface/register",
    "version_check": "/energy-interface/ent/versionCheck",
    "upload_config": "/energy-interface/ent/uploadConfigData",
    "upload_energy": "/energy-interface/ent/uploadEnergyData",
    "download_config": "/energy-interface/ent/downloadConfigData",
    "download_energy": "/energy-interface/ent/downloadEnergyData",
    "upload_metering": "/energy-interface/ent/uploadMeteringData",
}


class PushError(Exception):
    """Raised for expected validation or remote API failures."""


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise PushError(f"Config must be a JSON object: {path}")
    return data


def load_sql(db_config: Mapping[str, Any]) -> str:
    sql = db_config.get("sql")
    sql_file = db_config.get("sql_file")
    if sql and sql_file:
        raise PushError("database.sql and database.sql_file cannot both be set")
    if sql:
        return str(sql)
    if sql_file:
        path = Path(str(sql_file))
        with path.open("r", encoding="utf-8") as f:
            return f.read()
    raise PushError("database.sql or database.sql_file is required")


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def endpoint_url(config: Mapping[str, Any], endpoint: str) -> str:
    platform = config.get("platform", {})
    endpoint_overrides = platform.get("endpoints", {})
    if endpoint in endpoint_overrides:
        return str(endpoint_overrides[endpoint])
    base_url = normalize_base_url(str(platform.get("base_url", "")))
    if not base_url:
        raise PushError("platform.base_url is required")
    return f"{base_url}{ENDPOINTS[endpoint]}"


def build_http_opener(platform: Mapping[str, Any], context: Optional[ssl.SSLContext]):
    """Build an opener that bypasses inherited proxy settings by default."""
    handlers = []
    if not bool(platform.get("use_environment_proxy", False)):
        handlers.append(urllib.request.ProxyHandler({}))
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def http_post_json(config: Mapping[str, Any], url: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    platform = config.get("platform", {})
    timeout = int(platform.get("timeout_seconds", 30))
    retries = int(platform.get("max_retries", 2))
    verify_tls = bool(platform.get("verify_tls", True))

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": "gd-energy-push/0.1",
    }
    context = None if verify_tls else ssl._create_unverified_context()
    opener = build_http_opener(platform, context)

    last_error: Optional[BaseException] = None
    for attempt in range(1, retries + 2):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise PushError(f"Remote response is not a JSON object: {raw[:500]}")
            return parsed
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, PushError) as exc:
            last_error = exc
            if attempt <= retries:
                time.sleep(min(2 ** (attempt - 1), 8))
                continue
            break
    raise PushError(f"HTTP POST failed after {retries + 1} attempt(s): {last_error}")


def assert_success(response: Mapping[str, Any]) -> None:
    code = str(response.get("responseCode", ""))
    if code not in {"0", "E1001"}:
        msg = response.get("responseMessage", "")
        raise PushError(f"Platform returned {code}: {msg}")


def normalize_stat_date(value: Any) -> str:
    if value is None or value == "":
        raise PushError("statDate is required")
    if isinstance(value, dt.datetime):
        return value.strftime(TIME_FORMAT)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time()).strftime(TIME_FORMAT)
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return f"{text} 00:00:00"
    for fmt in (TIME_FORMAT, "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            return parsed.strftime(TIME_FORMAT)
        except ValueError:
            pass
    raise PushError(f"Invalid statDate, expected yyyy-MM-dd HH:mm:ss: {value}")


def now_string() -> str:
    return dt.datetime.now().strftime(TIME_FORMAT)


def normalize_bool_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "valid"}:
        return "true"
    if text in {"false", "0", "no", "n", "invalid"}:
        return "false"
    raise PushError(f"Invalid valid flag, expected true/false: {value}")


def normalize_decimal_string(value: Any) -> str:
    if value is None or value == "":
        raise PushError("dataValue is required")
    try:
        dec = decimal.Decimal(str(value).strip())
    except decimal.InvalidOperation as exc:
        raise PushError(f"Invalid dataValue: {value}") from exc
    if dec < 0:
        raise PushError(f"dataValue must be non-negative: {value}")
    return format(dec.normalize(), "f")


def pick(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] is not None and row[name] != "":
            return row[name]
    return default


def normalize_row_keys(row: Mapping[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for key, value in row.items():
        clean = str(key).strip()
        normalized[clean] = value
        snake = re.sub(r"(?<!^)([A-Z])", r"_\1", clean).lower()
        normalized.setdefault(snake, value)
    return normalized


def load_mapping(csv_path: Optional[Path]) -> Dict[str, Dict[str, str]]:
    if not csv_path:
        return {}
    if not csv_path.exists():
        raise PushError(f"Mapping file not found: {csv_path}")

    mapping: Dict[str, Dict[str, str]] = {}
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise PushError(f"Mapping file has no header: {csv_path}")
        for line_no, row in enumerate(reader, start=2):
            source_key = (row.get("source_key") or row.get("sourceKey") or "").strip()
            if not source_key:
                raise PushError(f"Mapping row {line_no} missing source_key")
            mapping[source_key] = {k: (v or "").strip() for k, v in row.items()}
    return mapping


def data_code_from_config_item(item: Mapping[str, Any]) -> Optional[str]:
    parts = [
        item.get("processCode"),
        item.get("processUnitCode"),
        item.get("equipmentCode"),
        item.get("equipmentUnitCode"),
        item.get("energyClassCode"),
        item.get("energyTypeCode"),
        item.get("dataUsageCode"),
    ]
    if any(part is None or str(part) == "" for part in parts):
        return None
    return (
        f"{parts[0]}-{parts[1]}-{parts[2]}{parts[3]}-"
        f"{parts[4]}{parts[5]}-{parts[6]}"
    )


def allowed_data_codes_from_download(downloaded_config: Mapping[str, Any]) -> set[str]:
    items = downloaded_config.get("collectItemConfig") or []
    allowed: set[str] = set()
    if not isinstance(items, list):
        return allowed
    for item in items:
        if isinstance(item, Mapping):
            code = data_code_from_config_item(item)
            if code:
                allowed.add(code)
    return allowed


def load_allowed_data_codes(path: Optional[Path]) -> set[str]:
    if not path:
        return set()
    data = load_json(path)
    return allowed_data_codes_from_download(data)


def query_sqlite(db_config: Mapping[str, Any], params: Sequence[Any]) -> List[Dict[str, Any]]:
    database = db_config.get("path")
    if not database:
        raise PushError("database.path is required for sqlite")
    sql = load_sql(db_config)
    with sqlite3.connect(str(database)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, list(params)).fetchall()
    return [dict(row) for row in rows]


def query_mysql(db_config: Mapping[str, Any], params: Sequence[Any]) -> List[Dict[str, Any]]:
    try:
        import pymysql  # type: ignore
    except ImportError as exc:
        raise PushError("pymysql is required for database.kind=mysql. Install it first.") from exc

    host = str(db_config.get("host", "127.0.0.1"))
    port = int(db_config.get("port", 3306))
    user = db_config.get("user")
    password = db_config.get("password")
    password_env = db_config.get("password_env")
    database = db_config.get("database")
    if password_env:
        password = os.environ.get(str(password_env))
    if not user:
        raise PushError("database.user is required for mysql")
    if password is None:
        raise PushError("database.password or database.password_env is required for mysql")

    sql = load_sql(db_config)
    connect_kwargs = {
        "host": host,
        "port": port,
        "user": str(user),
        "password": str(password),
        "charset": "utf8mb4",
        "connect_timeout": int(db_config.get("connect_timeout_seconds", 10)),
        "read_timeout": int(db_config.get("read_timeout_seconds", 120)),
        "cursorclass": pymysql.cursors.DictCursor,
    }
    if database:
        connect_kwargs["database"] = str(database)
    with pymysql.connect(**connect_kwargs) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, tuple(params))
            return list(cursor.fetchall())


def query_odbc(db_config: Mapping[str, Any], params: Sequence[Any]) -> List[Dict[str, Any]]:
    try:
        import pyodbc  # type: ignore
    except ImportError as exc:
        raise PushError("pyodbc is required for database.kind=odbc. Install it first.") from exc

    conn_string = db_config.get("connection_string")
    sql = load_sql(db_config)
    if not conn_string:
        raise PushError("database.connection_string is required for odbc")
    with pyodbc.connect(str(conn_string)) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, *list(params))
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def query_database(config: Mapping[str, Any], stat_date: str, stat_type: str) -> List[Dict[str, Any]]:
    db_config = config.get("database", {})
    kind = str(db_config.get("kind", "")).lower()
    param_names = db_config.get("parameters", ["stat_date", "stat_type"])
    values_by_name = {
        "stat_date": stat_date,
        "stat_type": stat_type,
    }
    try:
        params = [values_by_name[name] for name in param_names]
    except KeyError as exc:
        raise PushError(f"Unsupported database parameter name: {exc.args[0]}") from exc

    if kind == "sqlite":
        return query_sqlite(db_config, params)
    if kind == "mysql":
        return query_mysql(db_config, params)
    if kind == "odbc":
        return query_odbc(db_config, params)
    raise PushError("database.kind must be sqlite, mysql, or odbc")


def build_energy_item(
    row: Mapping[str, Any],
    mapping: Mapping[str, Mapping[str, str]],
    defaults: Mapping[str, Any],
    fallback_stat_date: str,
    fallback_stat_type: str,
) -> Dict[str, str]:
    normalized = normalize_row_keys(row)
    source_key = str(pick(normalized, "source_key", "sourceKey", "meter_code", "point_code", default="")).strip()
    mapped = mapping.get(source_key, {}) if source_key else {}

    merged: Dict[str, Any] = {}
    merged.update(defaults)
    merged.update(mapped)
    merged.update({k: v for k, v in normalized.items() if v is not None and v != ""})

    data_code = str(pick(merged, "dataCode", "data_code", default="")).strip()
    if not data_code:
        raise PushError(f"Missing dataCode for source_key={source_key or '<empty>'}")
    if not DATA_CODE_RE.fullmatch(data_code):
        raise PushError(f"Invalid dataCode format: {data_code}")

    item = {
        "dataCode": data_code,
        "dataValue": normalize_decimal_string(pick(merged, "dataValue", "data_value")),
        "inputType": str(pick(merged, "inputType", "input_type", default="1")).strip(),
        "statType": str(pick(merged, "statType", "stat_type", default=fallback_stat_type)).strip(),
        "statDate": normalize_stat_date(pick(merged, "statDate", "stat_date", default=fallback_stat_date)),
        "scope": str(pick(merged, "scope", default="1")).strip(),
        "valid": normalize_bool_string(pick(merged, "valid", default=True)),
        "uploadDate": normalize_stat_date(pick(merged, "uploadDate", "upload_date", default=now_string())),
    }
    validate_energy_item(item)
    return item


def validate_energy_item(item: Mapping[str, str]) -> None:
    if item["inputType"] not in {str(n) for n in range(1, 8)}:
        raise PushError(f"inputType must be 1-7: {item['inputType']}")
    if item["statType"] not in {"0", "1", "2", "3"}:
        raise PushError(f"statType must be 0-3: {item['statType']}")
    if item["scope"] not in {"1", "2", "3", "4"}:
        raise PushError(f"scope must be 1-4: {item['scope']}")
    normalize_stat_date(item["statDate"])
    normalize_stat_date(item["uploadDate"])


def build_energy_payload(
    config: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    mapping: Mapping[str, Mapping[str, str]],
    stat_date: str,
    stat_type: str,
    allowed_codes: set[str],
) -> Dict[str, Any]:
    enterprise = config.get("enterprise", {})
    enterprise_code = str(enterprise.get("enterpriseCode", "")).strip()
    device_id = str(enterprise.get("deviceId", "")).strip()
    if len(enterprise_code) != 18:
        raise PushError("enterprise.enterpriseCode must be 18 characters")
    if not device_id:
        raise PushError("enterprise.deviceId is required")

    defaults = config.get("defaults", {})
    data = [
        build_energy_item(row, mapping, defaults, stat_date, stat_type)
        for row in rows
    ]
    if not data:
        raise PushError("No rows returned from database")

    seen: set[Tuple[str, str, str]] = set()
    for item in data:
        key = (item["dataCode"], item["statType"], item["statDate"])
        if key in seen:
            raise PushError(f"Duplicate energy item for dataCode/statType/statDate: {key}")
        seen.add(key)
        if allowed_codes and item["dataCode"] not in allowed_codes:
            raise PushError(f"dataCode is not in downloaded platform config: {item['dataCode']}")

    return {
        "enterpriseCode": enterprise_code,
        "data": data,
        "deviceId": device_id,
    }


def batched(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def command_register(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    enterprise = config.get("enterprise", {})
    payload = {
        "ascription": enterprise.get("ascription", "ent"),
        "region": enterprise.get("region"),
        "enterpriseCode": enterprise.get("enterpriseCode"),
    }
    url = endpoint_url(config, "register")
    response = http_post_json(config, url, payload)
    print(json_dumps(response))
    assert_success(response)
    return 0


def command_version_check(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    enterprise = config.get("enterprise", {})
    payload = {"enterpriseCode": enterprise.get("enterpriseCode")}
    response = http_post_json(config, endpoint_url(config, "version_check"), payload)
    print(json_dumps(response))
    assert_success(response)
    return 0


def command_upload_config(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    payload = load_json(Path(args.payload_file))
    response = http_post_json(config, endpoint_url(config, "upload_config"), payload)
    print(json_dumps(response))
    assert_success(response)
    if args.out:
        save_json(Path(args.out), response)
    return 0


def command_download_config(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    enterprise = config.get("enterprise", {})
    payload = {
        "enterpriseCode": enterprise.get("enterpriseCode"),
        "deviceId": enterprise.get("deviceId"),
    }
    response = http_post_json(config, endpoint_url(config, "download_config"), payload)
    print(json_dumps(response))
    assert_success(response)
    if args.out:
        save_json(Path(args.out), response)
    return 0


def command_verify(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    enterprise = config.get("enterprise", {})
    payload = {
        "enterpriseCode": enterprise.get("enterpriseCode"),
        "deviceId": enterprise.get("deviceId"),
        "statType": str(args.stat_type),
        "statDate": normalize_stat_date(args.stat_date),
    }
    response = http_post_json(config, endpoint_url(config, "download_energy"), payload)
    print(json_dumps(response))
    assert_success(response)
    if args.out:
        save_json(Path(args.out), response)
    return 0


def load_rows_and_payload(args: argparse.Namespace, config: Mapping[str, Any]) -> Dict[str, Any]:
    stat_date = normalize_stat_date(args.stat_date)
    stat_type = str(args.stat_type)
    mapping_path = config.get("mapping", {}).get("csv_path")
    mapping = load_mapping(Path(mapping_path) if mapping_path else None)
    allowed_codes = load_allowed_data_codes(Path(args.platform_config)) if args.platform_config else set()
    rows = query_database(config, stat_date, stat_type)
    return build_energy_payload(config, rows, mapping, stat_date, stat_type, allowed_codes)


def command_dry_run(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    payload = load_rows_and_payload(args, config)
    print(json_dumps(payload))
    return 0


def command_upload(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    payload = load_rows_and_payload(args, config)
    batch_size = int(config.get("upload", {}).get("batch_size", 100))
    responses = []
    for chunk in batched(payload["data"], batch_size):
        batch_payload = {
            "enterpriseCode": payload["enterpriseCode"],
            "data": list(chunk),
            "deviceId": payload["deviceId"],
        }
        response = http_post_json(config, endpoint_url(config, "upload_energy"), batch_payload)
        assert_success(response)
        responses.append(response)
    result = {
        "responseCode": "0",
        "responseMessage": "LOCAL UPLOAD COMPLETE",
        "batches": len(responses),
        "items": len(payload["data"]),
        "responses": responses,
    }
    print(json_dumps(result))
    if args.out:
        save_json(Path(args.out), result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Push enterprise energy data to Guangdong EMC platform.")
    parser.add_argument("--config", required=True, help="Path to config JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("register", help="Call register endpoint")
    sub.add_parser("version-check", help="Call versionCheck endpoint")

    upload_config = sub.add_parser("upload-config", help="Upload enterprise info and collect item config")
    upload_config.add_argument("--payload-file", required=True, help="JSON file containing the full uploadConfigData payload")
    upload_config.add_argument("--out", help="Save response JSON")

    download_config = sub.add_parser("download-config", help="Download platform enterprise config")
    download_config.add_argument("--out", help="Save response JSON")

    dry_run = sub.add_parser("dry-run", help="Query database and print uploadEnergyData payload")
    dry_run.add_argument("--stat-date", required=True, help="yyyy-MM-dd or yyyy-MM-dd HH:mm:ss")
    dry_run.add_argument("--stat-type", default="1", choices=["0", "1", "2", "3"], help="0 real-time, 1 day, 2 month, 3 year")
    dry_run.add_argument("--platform-config", help="Downloaded config JSON used to validate dataCode")

    upload = sub.add_parser("upload", help="Query database and upload energy data")
    upload.add_argument("--stat-date", required=True, help="yyyy-MM-dd or yyyy-MM-dd HH:mm:ss")
    upload.add_argument("--stat-type", default="1", choices=["0", "1", "2", "3"], help="0 real-time, 1 day, 2 month, 3 year")
    upload.add_argument("--platform-config", help="Downloaded config JSON used to validate dataCode")
    upload.add_argument("--out", help="Save upload result JSON")

    verify = sub.add_parser("verify", help="Download uploaded energy data for a stat date")
    verify.add_argument("--stat-date", required=True, help="yyyy-MM-dd or yyyy-MM-dd HH:mm:ss")
    verify.add_argument("--stat-type", default="1", choices=["0", "1", "2", "3"])
    verify.add_argument("--out", help="Save response JSON")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_json(Path(args.config))
    try:
        if args.command == "register":
            return command_register(args, config)
        if args.command == "version-check":
            return command_version_check(args, config)
        if args.command == "upload-config":
            return command_upload_config(args, config)
        if args.command == "download-config":
            return command_download_config(args, config)
        if args.command == "dry-run":
            return command_dry_run(args, config)
        if args.command == "upload":
            return command_upload(args, config)
        if args.command == "verify":
            return command_verify(args, config)
    except PushError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
