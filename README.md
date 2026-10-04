# 广东省能源平台能耗数据推送系统（gd-energy-push）

把企业 MyEMS 能源管理系统的日电量数据，按《广东省能源局能源管理中心平台能耗在线监测数据接入企业操作指南》的接口规范，自动清洗、校验并上报至省平台（gdems.drc.gd.gov.cn）。

## 核心能力

- **省平台接口对接**：企业注册、版本校验、基础配置上传/下载、能耗数据上报、数据回读核验，共 6 个 REST 接口；HTTP 客户端带指数退避重试、超时与 TLS 校验配置
- **三级数据源降级策略**：空间小时汇总 → 电表小时汇总 → 累计电能差值（窗口函数 SQL），逐级尝试直到通过质量检查
- **数据质量防线**：最少小时数校验、负值/零值/累计倒走拒绝、dataCode 白名单校验（必须匹配平台 `downloadConfigData` 下发的采集项配置），检查不通过即拒绝生成报文
- **两段式安全上传**：默认只生成报文不上传，`--confirm-upload` 显式确认后才调用上报接口；上报后自动回读平台核验数据可查
- **幂等断点补传（catch-up）**：上报前先查平台是否已有该日数据，本地状态文件记录进度，可安全补传历史缺报数据
- **多客户复用**：客户配置全部 JSON 化（`customers/example.json`），新客户只需填企业信息、MyEMS 空间/电表 ID 与 dataCode
- **零依赖本地 Web 控制台**（`web_ui.py`，仅用 Python 标准库）：选择客户、诊断数据、生成报文、一键试跑与确认上传

## 架构

```text
MyEMS MySQL ──> energy_push_system.py（诊断/清洗/质检/补传）
                    │
                    ├──> web_ui.py（本地控制台，可选）
                    │
                    └──> gd_energy_push.py（省平台协议、重试、校验）──> gdems.drc.gd.gov.cn
```

## 快速开始

```bash
pip install pymysql
export GD_ENERGY_MYSQL_PASSWORD="数据库密码"   # Windows: set GD_ENERGY_MYSQL_PASSWORD=...

# 1. 复制客户配置模板并填写
cp customers/example.json customers/my_customer.json

# 2. 诊断客户 MyEMS 数据（空间、电表、小时汇总是否可用）
python energy_push_system.py --customer customers/my_customer.json diagnose

# 3. 试跑某一天的日报文（只生成，不上传）
python energy_push_system.py --customer customers/my_customer.json run-daily --stat-date 2026-06-04

# 4. 确认无误后正式上报
python energy_push_system.py --customer customers/my_customer.json run-daily --stat-date 2026-06-04 --confirm-upload
```

命令行子命令一览：`diagnose` / `prepare-daily` / `run-daily` / `prepare-period` / `run-period`（月、年） / `catch-up`（幂等补传）。

---

以下为省平台接入的操作细节与注意事项。

# 广东省能源平台数据推送试跑方案

这个目录是一个可配置的推送骨架，用来把客户数据库里的能耗数据按《广东省能源局能源管理中心平台能耗在线监测数据接入企业操作指南》定义的接口格式传到省平台。

## 推荐流程

1. 确认客户的 `enterpriseCode`、`region`、`deviceId`。
   - 如果以前已经有服务商传过，这个客户大概率已经注册过。
   - 没有 `deviceId` 时，可以先调用 `register`，或向平台/原服务商确认。
2. 调用 `download-config` 下载平台已有基础配置，重点确认 `collectItemConfig` 里有哪些采集项。
3. 把客户数据库里的点位或能源类型映射到平台 `dataCode`。
4. 先执行 `dry-run`，只生成 JSON 报文，不上传。
5. 抽查报文无误后执行 `upload`。
6. 上传后执行 `verify`，通过采集数据下载接口核验平台是否可查。

## 平台接口

脚本默认使用正式地址：

`https://gdems.drc.gd.gov.cn/emc/gov-online-docking`

HTTP 请求默认绕过系统环境变量中的代理（包括 FlClash 设置的
`HTTP_PROXY`/`HTTPS_PROXY`），直接访问省平台。若某个客户必须通过代理访问，
在其 `platform` 配置中设置 `"use_environment_proxy": true`。

已实现的接口：

- `register`: `/energy-interface/register`
- `version-check`: `/energy-interface/ent/versionCheck`
- `download-config`: `/energy-interface/ent/downloadConfigData`
- `dry-run` / `upload`: `/energy-interface/ent/uploadEnergyData`
- `verify`: `/energy-interface/ent/downloadEnergyData`

## 配置文件

复制一份配置：

```powershell
Copy-Item .\gd_energy_push\config.example.json .\gd_energy_push\config.local.json
```

然后修改：

- `enterprise.enterpriseCode`: 客户 18 位统一社会信用代码。
- `enterprise.region`: 客户区县级行政区划代码，6 位。
- `enterprise.deviceId`: 平台注册返回或已有的设备序列号。
- `database.kind`: `odbc` 或 `sqlite`。
- `database.connection_string`: 客户数据库连接串，`odbc` 模式需要安装 `pyodbc`。
- `database.sql`: 从客户数据库取数的 SQL。
- `mapping.csv_path`: 点位到平台 dataCode 的映射表。

SQL 至少要返回：

- `data_value`: 采集值，非负数。
- `stat_date`: 数据统计周期起始时间，格式最终会规范成 `yyyy-MM-dd HH:mm:ss`。
- `data_code` 或 `source_key`：
  - 如果 SQL 直接返回 `data_code`，脚本会直接使用。
  - 如果返回 `source_key`，脚本会从 CSV 映射到 `dataCode`。

推荐 SQL 返回列示例：

```sql
SELECT
  meter_code AS source_key,
  energy_value AS data_value,
  stat_time AS stat_date
FROM dbo.energy_daily
WHERE stat_time = ? AND stat_type = ?
```

## dataCode 映射

`data_code_mapping.example.csv` 的核心列：

```csv
source_key,dataCode,name,inputType,statType,scope
ELEC_TOTAL,00-00-0000-023300-11,全厂-二次能源-电力-购进已消费,1,1,1
```

常用全厂数据项：

- 电力购进已消费：`00-00-0000-023300-11`
- 天然气购进已消费：`00-00-0000-011500-11`
- 蒸汽购进已消费：`00-00-0000-023200-11`
- 柴油购进已消费：`00-00-0000-022100-11`
- 原煤购进已消费：`00-00-0000-010100-11`

正式上传前，建议用 `download-config` 结果校验这些 `dataCode` 是否已经在平台基础配置里存在。

## 命令示例

下载平台已有基础配置：

```powershell
python .\gd_energy_push\gd_energy_push.py --config .\gd_energy_push\config.local.json download-config --out .\gd_energy_push\platform_config.json
```

生成某一天日数据上传报文，但不上传：

```powershell
python .\gd_energy_push\gd_energy_push.py --config .\gd_energy_push\config.local.json dry-run --stat-type 1 --stat-date 2026-05-14 --platform-config .\gd_energy_push\platform_config.json
```

上传某一天日数据：

```powershell
python .\gd_energy_push\gd_energy_push.py --config .\gd_energy_push\config.local.json upload --stat-type 1 --stat-date 2026-05-14 --platform-config .\gd_energy_push\platform_config.json --out .\gd_energy_push\upload_result.json
```

上传后核验：

```powershell
python .\gd_energy_push\gd_energy_push.py --config .\gd_energy_push\config.local.json verify --stat-type 1 --stat-date 2026-05-14 --out .\gd_energy_push\verify_result.json
```

## 重要注意

- 文档说采集数据上传前必须先完成注册和基础信息上传。既然客户以前已经传过，第一步应优先下载基础配置确认 `deviceId` 和采集项是否仍有效。
- `uploadEnergyData.data[].dataCode` 必须和基础信息里的 7 个配置字段组合一致：`processCode`、`processUnitCode`、`equipmentCode`、`equipmentUnitCode`、`energyClassCode`、`energyTypeCode`、`dataUsageCode`。
- 文档返回了 `secretKey`，但未定义签名或加密算法。这个脚本按 PDF 示例直接 POST JSON，不使用 `secretKey`。如果平台技术支持要求加签/加密，需要再补协议细节。
- 正式环境建议保留 `verify_tls=true`。只有联调时遇到证书链问题，才临时改成 `false`。
