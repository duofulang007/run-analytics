# run-analytics 维护说明

这是本地跑步数据看板。页面是 `index.html`、`app.js`、`styles.css`、`toolrail.js`；导入与统计在 `tools/`；`.apex/` 是 ControlPod 模块入口。修改前先检查 `git status`，保留已有改动。运行环境由本仓库 `setup.ps1` 创建在 `.venv`，不依赖 ControlPod 的 Python 环境。

## 数据边界

- `data.js`、`data.prev.js`、`local/`、下载包和浏览器 profile 含个人数据，不提交仓库。不要在日志或测试结果中输出邮箱凭证和解压密码。
- 导入仅打开小米导出包里的跑步记录与逐分钟数据成员。替换 `data.js` 前保留备份；场次数减少需提示，最新日期倒退需拒绝替换。
- 训练负荷和可比跑结论是估算与线索，不写成确定的健康诊断。统计口径集中在 `tools/metrics.py`，页面只展示结果。
- 清理先预演再确认，并保留 `data.js`。本地服务只监听 `127.0.0.1`，API 保持 token 验证和静态文件白名单。
- ControlPod 适配器使用本仓库脚本及 `.venv`；清单与适配器一同维护，不把业务代码移入 ControlPod。

## 验证

```powershell
node tools/check.js
.\.venv\Scripts\python.exe tools/metrics.py --selftest
.\.venv\Scripts\python.exe tools/serve.py --selftest
.\.venv\Scripts\python.exe tools/tasks.py --selftest
```

导入或邮件链路变动时，再运行相应的 `tools/test_*.py`。真实页面布局、静态直开与工具栏操作需在浏览器核验；逻辑自检不能替代视觉验证。
