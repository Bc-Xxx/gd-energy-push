#!/usr/bin/env python3
"""
Small local web UI for the Guangdong EMC push system.

It is intentionally dependency-free so it can run on a test PC or a server with
only Python installed. Bind to 127.0.0.1 by default; use a reverse proxy/VPN if
you need remote access on a cloud server.
"""

from __future__ import annotations

import argparse
import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Sequence
from urllib.parse import parse_qs, urlparse

import energy_push_system as system


ROOT = Path(__file__).resolve().parent
CUSTOMERS_DIR = ROOT / "customers"


def jsonable(value: Any) -> Any:
    return system.to_jsonable(value)


def load_customers() -> Dict[str, Path]:
    customers: Dict[str, Path] = {}
    for path in sorted(CUSTOMERS_DIR.glob("*.json")):
        try:
            data = system.load_customer(path)
            key = str(data.get("customer_key") or path.stem)
            customers[key] = path
        except Exception:
            continue
    return customers


def html_page() -> bytes:
    return HTML.encode("utf-8")


def read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("request body must be a JSON object")
    return data


def write_response(handler: BaseHTTPRequestHandler, status: int, content_type: str, body: bytes) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def write_json(handler: BaseHTTPRequestHandler, status: int, data: Any) -> None:
    body = json.dumps(jsonable(data), ensure_ascii=False, indent=2).encode("utf-8")
    write_response(handler, status, "application/json; charset=utf-8", body)


class Handler(BaseHTTPRequestHandler):
    server_version = "GdEnergyPushUI/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            write_response(self, 200, "text/html; charset=utf-8", html_page())
            return
        if parsed.path == "/api/customers":
            write_json(self, 200, self.api_customers())
            return
        if parsed.path == "/api/run-file":
            query = parse_qs(parsed.query)
            path = Path((query.get("path") or [""])[0])
            self.api_run_file(path)
            return
        write_response(self, 404, "text/plain; charset=utf-8", b"Not Found")

    def do_POST(self) -> None:
        try:
            body = read_json_body(self)
            if "password" in body and body["password"]:
                os.environ["GD_ENERGY_MYSQL_PASSWORD"] = str(body["password"])
            if self.path == "/api/diagnose":
                write_json(self, 200, self.api_diagnose(body))
                return
            if self.path == "/api/prepare":
                write_json(self, 200, self.api_prepare(body))
                return
            if self.path == "/api/run-daily":
                write_json(self, 200, self.api_run_daily(body))
                return
            write_response(self, 404, "text/plain; charset=utf-8", b"Not Found")
        except Exception as exc:
            write_json(self, 500, {
                "ok": False,
                "error": str(exc),
                "trace": traceback.format_exc(limit=6),
            })

    def customer_config(self, body: Dict[str, Any]) -> Dict[str, Any]:
        key = str(body.get("customer") or "weifu")
        customers = load_customers()
        if key not in customers:
            raise ValueError(f"unknown customer: {key}")
        return system.load_customer(customers[key])

    def api_customers(self) -> Dict[str, Any]:
        items = []
        for key, path in load_customers().items():
            data = system.load_customer(path)
            items.append({
                "key": key,
                "name": data.get("customer_name"),
                "enterpriseCode": data.get("enterprise", {}).get("enterpriseCode"),
                "deviceId": data.get("enterprise", {}).get("deviceId"),
                "rootSpaceId": data.get("myems", {}).get("root_space_id"),
                "meterIds": data.get("myems", {}).get("meter_ids", []),
                "dataCode": data.get("upload_item", {}).get("dataCode"),
                "path": str(path),
            })
        return {"ok": True, "customers": items}

    def api_diagnose(self, body: Dict[str, Any]) -> Dict[str, Any]:
        config = self.customer_config(body)
        data = system.diagnose(config)
        path = system.write_run_json(config, "diagnose", data)
        return {
            "ok": True,
            "saved": str(path),
            "summary": system.summarize_diagnose(data),
            "data": data,
        }

    def api_prepare(self, body: Dict[str, Any]) -> Dict[str, Any]:
        config = self.customer_config(body)
        stat_date = str(body.get("statDate") or system.default_stat_date_yesterday())
        data = system.prepare_daily(config, stat_date)
        path = system.write_run_json(config, "prepare", data)
        return {
            "ok": data["canUpload"],
            "saved": str(path),
            "message": "已生成可上传报文" if data["canUpload"] else "当前没有通过质量检查的日电量数据",
            "data": data,
        }

    def api_run_daily(self, body: Dict[str, Any]) -> Dict[str, Any]:
        config = self.customer_config(body)
        stat_date = str(body.get("statDate") or system.default_stat_date_yesterday())
        confirm_upload = bool(body.get("confirmUpload"))

        prepared = system.prepare_daily(config, stat_date)
        prepare_path = system.write_run_json(config, "prepare", prepared)
        result: Dict[str, Any] = {
            "ok": prepared["canUpload"],
            "prepareFile": str(prepare_path),
            "prepared": prepared,
        }
        if not prepared["canUpload"]:
            result["message"] = "当前没有通过质量检查的日电量数据，未上传"
            return result

        platform_config = system.download_platform_config(config)
        platform_path = system.write_run_json(config, "platform_config", platform_config)
        system.validate_payload_against_platform(prepared["payload"], platform_config)
        result["platformConfigFile"] = str(platform_path)

        if not confirm_upload:
            result["ok"] = True
            result["message"] = "已生成并校验报文；未勾选确认上传，所以没有真正上传"
            return result

        response = system.upload_payload(config, prepared["payload"])
        upload_path = system.write_run_json(config, "upload_response", response)
        result["ok"] = True
        result["uploaded"] = True
        result["uploadResponseFile"] = str(upload_path)
        result["response"] = response
        result["message"] = "省平台上传完成"
        return result

    def api_run_file(self, path: Path) -> None:
        if not path:
            write_json(self, 400, {"ok": False, "error": "missing path"})
            return
        resolved = path.resolve()
        runs_root = (ROOT / "runs").resolve()
        if runs_root not in [resolved, *resolved.parents]:
            write_json(self, 403, {"ok": False, "error": "only run files can be opened"})
            return
        if not resolved.exists():
            write_json(self, 404, {"ok": False, "error": "file not found"})
            return
        write_response(self, 200, "application/json; charset=utf-8", resolved.read_bytes())


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>广东省能源平台推送控制台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fa;
      --panel: #ffffff;
      --ink: #172033;
      --muted: #647087;
      --line: #dce3ec;
      --blue: #1769e0;
      --green: #147a52;
      --red: #ba2c2c;
      --amber: #9c6500;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    header {
      padding: 18px 24px;
      background: #ffffff;
      border-bottom: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
    }
    h1 { margin: 0; font-size: 22px; font-weight: 700; }
    main {
      width: min(1280px, calc(100% - 32px));
      margin: 18px auto 28px;
      display: grid;
      grid-template-columns: 360px 1fr;
      gap: 16px;
    }
    section, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    section { padding: 18px; }
    h2 { margin: 0 0 14px; font-size: 17px; }
    label { display: block; margin: 12px 0 6px; color: var(--muted); font-size: 13px; }
    input, select {
      width: 100%;
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      font-size: 14px;
      background: #fff;
      color: var(--ink);
    }
    .buttons {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-top: 16px;
    }
    button {
      height: 38px;
      border: 1px solid transparent;
      border-radius: 6px;
      background: var(--blue);
      color: #fff;
      font-size: 14px;
      font-weight: 650;
      cursor: pointer;
    }
    button.secondary { background: #fff; color: var(--blue); border-color: #9fc2f8; }
    button.danger { background: var(--red); }
    button:disabled { opacity: .55; cursor: wait; }
    .confirm {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 14px;
      color: var(--ink);
      font-size: 14px;
    }
    .confirm input { width: 18px; height: 18px; }
    .facts { display: grid; gap: 10px; margin-top: 12px; }
    .fact { padding: 10px; background: #f7f9fc; border: 1px solid var(--line); border-radius: 6px; }
    .fact b { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
    .tabs {
      display: flex;
      gap: 8px;
      padding: 10px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfe;
      border-radius: 8px 8px 0 0;
    }
    .tab {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      height: 34px;
      padding: 0 12px;
    }
    .tab.active { background: var(--ink); color: #fff; border-color: var(--ink); }
    .content { padding: 16px; }
    .status {
      min-height: 44px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #f7f9fc;
      color: var(--muted);
      margin-bottom: 14px;
    }
    .status.ok { border-color: #a8dbc6; background: #f1fbf6; color: var(--green); }
    .status.err { border-color: #efb2b2; background: #fff5f5; color: var(--red); }
    .status.warn { border-color: #ecd08a; background: #fff9e8; color: var(--amber); }
    pre {
      margin: 0;
      min-height: 520px;
      max-height: 68vh;
      overflow: auto;
      padding: 14px;
      background: #111827;
      color: #e8eef8;
      border-radius: 6px;
      font-size: 13px;
      line-height: 1.5;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .hint { color: var(--muted); font-size: 13px; line-height: 1.55; }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; }
      header { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <header>
    <h1>广东省能源平台推送控制台</h1>
    <div class="hint">本地工具；默认不会上传，勾选确认后才会调用省平台。</div>
  </header>
  <main>
    <section>
      <h2>客户与任务</h2>
      <label for="customer">客户</label>
      <select id="customer"></select>
      <div class="facts" id="facts"></div>
      <label for="password">数据库密码</label>
      <input id="password" type="password" autocomplete="off" placeholder="本次运行临时使用，不写入配置">
      <label for="statDate">统计日期</label>
      <input id="statDate" type="date">
      <div class="confirm">
        <input id="confirmUpload" type="checkbox">
        <span>确认正式上传到省平台</span>
      </div>
      <div class="buttons">
        <button class="secondary" id="diagnoseBtn">诊断数据</button>
        <button class="secondary" id="prepareBtn">生成报文</button>
        <button id="runBtn">每日试跑</button>
        <button class="danger" id="uploadBtn">确认上传</button>
      </div>
      <p class="hint">建议顺序：诊断数据 -> 生成报文 -> 每日试跑 -> 勾选确认后上传。</p>
    </section>
    <div class="panel">
      <div class="tabs">
        <button class="tab active" data-tab="summary">结果</button>
        <button class="tab" data-tab="json">JSON</button>
      </div>
      <div class="content">
        <div id="status" class="status">等待操作。</div>
        <pre id="output">请选择客户并开始诊断。</pre>
      </div>
    </div>
  </main>
  <script>
    const state = { customers: [], latest: null, tab: "summary" };
    const $ = (id) => document.getElementById(id);

    function yesterday() {
      const d = new Date();
      d.setDate(d.getDate() - 1);
      return d.toISOString().slice(0, 10);
    }

    function setBusy(busy) {
      for (const id of ["diagnoseBtn", "prepareBtn", "runBtn", "uploadBtn"]) $(id).disabled = busy;
    }

    function setStatus(text, kind = "") {
      $("status").className = "status" + (kind ? " " + kind : "");
      $("status").textContent = text;
    }

    function selectedCustomer() {
      return $("customer").value || "weifu";
    }

    function body(extra = {}) {
      return {
        customer: selectedCustomer(),
        password: $("password").value,
        statDate: $("statDate").value,
        ...extra
      };
    }

    async function post(url, payload) {
      setBusy(true);
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        state.latest = data;
        render();
        if (!res.ok || data.ok === false) setStatus(data.message || data.error || "执行未通过。", "warn");
        else setStatus(data.message || "执行完成。", "ok");
      } catch (err) {
        state.latest = { ok: false, error: String(err) };
        render();
        setStatus(String(err), "err");
      } finally {
        setBusy(false);
      }
    }

    function renderFacts() {
      const c = state.customers.find(x => x.key === selectedCustomer());
      if (!c) return;
      $("facts").innerHTML = `
        <div class="fact"><b>统一社会信用代码</b>${c.enterpriseCode || ""}</div>
        <div class="fact"><b>deviceId</b>${c.deviceId || ""}</div>
        <div class="fact"><b>MyEMS 空间/电表</b>${c.rootSpaceId || ""} / ${(c.meterIds || []).join(", ")}</div>
        <div class="fact"><b>dataCode</b>${c.dataCode || ""}</div>
      `;
    }

    function summary(data) {
      if (!data) return "暂无结果。";
      if (data.error) return data.error + (data.trace ? "\n\n" + data.trace : "");
      if (data.summary) return JSON.stringify(data.summary, null, 2);
      const prepared = data.data || data.prepared;
      if (prepared && prepared.attempts) {
        const lines = [];
        lines.push(`是否可上传：${prepared.canUpload ? "是" : "否"}`);
        lines.push(`选中策略：${prepared.selectedStrategy || "无"}`);
        lines.push("");
        for (const a of prepared.attempts) {
          lines.push(`[${a.ok ? "通过" : "未通过"}] ${a.strategy}`);
          if (a.dataValue !== null && a.dataValue !== undefined) lines.push(`  dataValue: ${a.dataValue}`);
          if (a.rowCount !== undefined) lines.push(`  rowCount: ${a.rowCount}`);
          if (a.pointCount !== undefined) lines.push(`  pointCount: ${a.pointCount}`);
          if (a.warnings && a.warnings.length) lines.push(`  warnings: ${a.warnings.join("；")}`);
        }
        if (prepared.payload) {
          lines.push("");
          lines.push("生成报文：");
          lines.push(JSON.stringify(prepared.payload, null, 2));
        }
        return lines.join("\n");
      }
      return JSON.stringify(data, null, 2);
    }

    function render() {
      $("output").textContent = state.tab === "summary"
        ? summary(state.latest)
        : JSON.stringify(state.latest || {}, null, 2);
    }

    async function init() {
      $("statDate").value = yesterday();
      const res = await fetch("/api/customers");
      const data = await res.json();
      state.customers = data.customers || [];
      $("customer").innerHTML = state.customers.map(c => `<option value="${c.key}">${c.name || c.key}</option>`).join("");
      renderFacts();
      $("customer").addEventListener("change", renderFacts);
      $("diagnoseBtn").onclick = () => post("/api/diagnose", body());
      $("prepareBtn").onclick = () => post("/api/prepare", body());
      $("runBtn").onclick = () => post("/api/run-daily", body({ confirmUpload: false }));
      $("uploadBtn").onclick = () => {
        if (!$("confirmUpload").checked) {
          setStatus("请先勾选“确认正式上传到省平台”。", "warn");
          return;
        }
        post("/api/run-daily", body({ confirmUpload: true }));
      };
      document.querySelectorAll(".tab").forEach(btn => {
        btn.onclick = () => {
          document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
          btn.classList.add("active");
          state.tab = btn.dataset.tab;
          render();
        };
      });
    }
    init();
  </script>
</body>
</html>
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local web UI for Guangdong energy push.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Web UI running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
