# run-analytics 维护说明

这是本地跑步数据看板。页面是 `index.html`、`app.js`、`styles.css`、`radar.js`、`toolrail.js`；导入与统计在 `tools/`；`.apex/` 是 ControlPod 模块入口。修改前先检查 `git status`，保留已有改动。运行环境由本仓库 `setup.ps1` 创建在 `.venv`，不依赖 ControlPod 的 Python 环境。

## 数据边界

- `data.js`、`data.prev.js`、`local/`、下载包和浏览器 profile 含个人数据，不提交仓库。不要在日志或测试结果中输出邮箱凭证和解压密码。
- 导入仅打开小米导出包里的跑步记录与逐分钟数据成员。替换 `data.js` 前保留备份；场次数减少需提示，最新日期倒退需拒绝替换。
- 训练负荷和可比跑结论是估算与线索，不写成确定的健康诊断。统计口径集中在 `tools/metrics.py`，页面只展示结果。
- 清理先预演再确认，并保留 `data.js`。本地服务只监听 `127.0.0.1`，API 保持 token 验证和静态文件白名单。
- ControlPod 适配器使用本仓库脚本及 `.venv`；清单与适配器一同维护，不把业务代码移入 ControlPod。

## 对比与服务版本

- 六维对比算法统一在 `tools/metrics.py`，由预计算数据和 `POST /api/radar/compare` 共用；`radar.js` 只处理交互与展示。只使用本场之前的对照，条件轴不计入总分，样本或核心指标不足时保留原始事实并说明原因。
- 静态页面使用预计算范围；自定义条件需要本地服务。断开时显示原因和重连入口，避免静默使用旧结果。
- `tools/bridge_version.py` 的指纹覆盖后端程序文件；新增相关文件时维护指纹清单。适配器复用服务前核对身份与版本，只有确认属于本项目且没有活跃任务时才可正常关闭旧服务。

## 验证

```powershell
node tools/check.js
node tools/test_radar_ui.js
.\.venv\Scripts\python.exe tools/metrics.py --selftest
.\.venv\Scripts\python.exe tools/serve.py --selftest
.\.venv\Scripts\python.exe tools/tasks.py --selftest
.\.venv\Scripts\python.exe -m unittest discover -s tools -p 'test_*.py'
```

真实页面布局、静态直开与工具栏操作需在浏览器核验；逻辑自检不能替代视觉验证。改统计时先用自检或 `--check` 核对，避免为验证直接覆盖个人 `data.js`。
