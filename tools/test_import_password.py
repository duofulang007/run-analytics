# -*- coding: utf-8 -*-
"""密码来源的回归测试：只认本次调用给的，绝不落盘。

为什么值得单测：密码文件这种东西一旦被加回来，故障是**静默**的 ——
用户换了密码（小米的导出密码会变），工具却拿旧值去解，反复报「密码不对」。
所以这里连"文件存在也必须被忽略"一起锁住。

跑法：
    python tools/test_import_password.py      # 直接跑，失败退出码 1
    或交给 pytest：pytest tools/test_import_password.py
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent


def _load_module():
    spec = importlib.util.spec_from_file_location("import_zip_under_test", HERE / "import_zip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(**kwargs):
    class Args:
        password = None
        no_prompt = False

        def __init__(self, **values):
            for key, value in values.items():
                setattr(self, key, value)

    return Args(**kwargs)


def test_dash_password_wins(monkeypatch=None):
    module = _load_module()
    os.environ["MIFITNESS_ZIP_PASSWORD"] = "from-env"
    try:
        assert module.resolve_password(_args(password="from-flag")) == "from-flag"
    finally:
        os.environ.pop("MIFITNESS_ZIP_PASSWORD", None)


def test_environment_variable_is_used():
    module = _load_module()
    os.environ["MIFITNESS_ZIP_PASSWORD"] = "from-env"
    try:
        assert module.resolve_password(_args()) == "from-env"
    finally:
        os.environ.pop("MIFITNESS_ZIP_PASSWORD", None)


def test_no_prompt_never_prompts_and_returns_none():
    module = _load_module()
    os.environ.pop("MIFITNESS_ZIP_PASSWORD", None)
    assert module.resolve_password(_args(no_prompt=True)) is None


def test_password_file_is_ignored_even_when_present(tmp_path: Path | None = None):
    """核心回归：本机放着密码文件也不能被读（密码会变，读了只会误导）。"""
    module = _load_module()
    os.environ.pop("MIFITNESS_ZIP_PASSWORD", None)

    assert not hasattr(module, "PASSWORD_FILE"), "密码文件常量不该再存在"

    # 就算磁盘上真躺着一个密码文件，也必须返回 None 而不是它的内容。
    decoy = Path(__file__).resolve().parent / "fixtures" / "zip-password-decoy.txt"
    created = False
    try:
        if not decoy.exists():
            decoy.parent.mkdir(parents=True, exist_ok=True)
            decoy.write_text("stale-old-password", encoding="utf-8")
            created = True
        assert module.resolve_password(_args(no_prompt=True)) is None
    finally:
        if created:
            decoy.unlink(missing_ok=True)


def _main() -> int:
    tests = [
        test_dash_password_wins,
        test_environment_variable_is_used,
        test_no_prompt_never_prompts_and_returns_none,
        test_password_file_is_ignored_even_when_present,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - 这是自检脚本
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"OK   {test.__name__}")
    print(f"FAIL_COUNT={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
