# -*- coding: utf-8 -*-
"""清理这条自动化链路的中间产物：下载的包 + 已处理邮件记录。

删什么
    * 搜索目录里的小米导出包（名字按 `import_zip.PACKAGE_NAME_RE`）
    * `local/mail_state.json`（已处理邮件 UID + 上次导入时间）
不删什么
    * 看板数据 `data.js` / `data.prev.js` —— 那是你的跑步记录，跟这个清理无关
    * 别的下载文件（名字口径只认小米的包）
    * 邮箱凭证 `local/mail_config.json`

**分两步，删之前必须能看见**：`collect()` 只列不删（预演），`apply()` 才真删。
界面层负责「先把清单给用户看，再让他确认」—— 删数据不可逆，不能一键就下去。

为什么要连 `mail_state.json` 一起清：清掉之后旧邮件才能被重新处理。不然
「已处理过」的 UID 会让脚本一直跳过它们，表现成「跑了但没反应」。

命令行用法（默认只预演，看得见才敢删）：
    python tools/cleanup.py           # 预演
    python tools/cleanup.py --yes     # 确认删除
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import import_zip  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_DIR / "local" / "mail_state.json"


def describe_state(path: Path) -> str:
    """给状态文件一句人话摘要，用在预演里。读不动就如实说读不动。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        return "读不动（%s）" % type(exc).__name__
    handled = len(payload.get("handled_uids") or [])
    last = payload.get("last_success") or "无记录"
    return "已处理 %d 封邮件 · 上次导入 %s" % (handled, last)


def collect(search_dirs=None, state_path=None):
    """预演：返回 (要删的包列表, 要删的状态文件或 None)。**只看不删。**"""
    dirs = tuple(search_dirs) if search_dirs is not None else tuple(import_zip.SEARCH_DIRS)
    state = Path(state_path) if state_path is not None else STATE_PATH

    packages = []
    for folder in dirs:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        for item in folder.glob("*.zip"):
            if import_zip.PACKAGE_NAME_RE.search(item.name):
                packages.append(item)
    packages.sort(key=lambda item: str(item))
    return packages, (state if state.is_file() else None)


def apply(packages, state):
    """真的删。返回 (删掉的路径清单, 删不掉的路径清单)。"""
    removed, failed = [], []
    targets = list(packages) + ([state] if state is not None else [])
    for item in targets:
        try:
            Path(item).unlink()
            removed.append(str(item))
        except OSError:
            failed.append(str(item))
    return removed, failed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="清理下载的包与已处理邮件记录")
    parser.add_argument("--yes", action="store_true", help="确认删除（不给就只预演）")
    args = parser.parse_args(argv)

    packages, state = collect()
    if not packages and state is None:
        print("没有需要清理的东西：搜索目录里没有小米导出包，也没有已处理邮件记录。")
        return 0

    print("将删除下面这些（看板数据 data.js 不会被动）：")
    for item in packages:
        print("    [包]   %s（%.1f MB）" % (item, item.stat().st_size / 1024 / 1024))
    if state is not None:
        print("    [记录] %s（%s）" % (state, describe_state(state)))

    if not args.yes:
        print()
        print("以上是**预演**，什么都没删。确认要删就加 --yes。")
        return 0

    removed, failed = apply(packages, state)
    print()
    print("删掉 %d 项。" % len(removed))
    for path in removed:
        print("    %s" % path)
    if failed:
        print("有 %d 项删不掉（可能被占用）：" % len(failed))
        for path in failed:
            print("    %s" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
