# 客户配置模板

这个目录用于存放“其他客户”的配置模板。这里的 JSON 不会被图形化界面自动加载。

如果要启用某个客户：

1. 复制对应模板到 `gd_energy_push/customers/`。
2. 修改文件名，例如 `my_customer.json`。
3. 填写真实的省平台信息：
   - `enterprise.region`
   - `enterprise.enterpriseCode`
   - `enterprise.deviceId`
   - `upload_item.dataCode`
4. 启动 UI 后选择该客户测试。

不要在没有确认 `enterpriseCode + deviceId + dataCode` 的情况下上传。
