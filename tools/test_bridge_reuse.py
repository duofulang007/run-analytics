"""Regression checks for CPod reusing a bridge whose backend is out of date."""
import importlib.util
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import serve

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("run_analytics_runner", ROOT / ".apex" / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class BridgeReuseTest(unittest.TestCase):
    def test_running_service_reports_loaded_revision(self):
        server = serve._Bridge(("127.0.0.1", 0), serve.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            self.assertTrue(runner.bridge_matches(port, ROOT))
            with patch.object(runner, "source_revision", return_value="changed-source"):
                self.assertFalse(runner.bridge_matches(port, ROOT))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_active_import_prevents_automatic_restart(self):
        bootstrap = {"project": str(ROOT), "api": "/api", "token": "synthetic-test-token"}
        index = ("<script>window.__BRIDGE__ = " + json.dumps(bootstrap) + ";</script>").encode()
        status = json.dumps({"busy": {"refresh": True, "login": False}, "idle": False}).encode()
        with patch.object(runner.urllib.request, "urlopen", side_effect=[io.BytesIO(index), io.BytesIO(status)]) as requests:
            with self.assertRaisesRegex(RuntimeError, "刷新或登录仍在运行"):
                runner.retire_outdated_bridge(ROOT, 8917, ROOT / "local" / "test.log")
        self.assertEqual(requests.call_count, 2, "busy bridge must never receive quit")

    def test_unrelated_service_is_never_stopped(self):
        index = b'<script>window.__BRIDGE__ = {"project":"unrelated","api":"/api","token":"test"};</script>'
        with patch.object(runner.urllib.request, "urlopen", return_value=io.BytesIO(index)) as requests:
            with self.assertRaisesRegex(RuntimeError, "不是本项目"):
                runner.retire_outdated_bridge(ROOT, 8917, ROOT / "local" / "test.log")
        self.assertEqual(requests.call_count, 1)

    def test_stale_bridge_is_not_reused(self):
        events = []
        with patch.object(runner, "write_log"):
            folder = ROOT / "local" / "bridge-reuse-test"
            with patch.object(runner, "bridge_running", return_value=8917), \
                 patch.object(runner, "bridge_matches", return_value=False, create=True), \
                 patch.object(runner, "retire_outdated_bridge", side_effect=lambda *a: events.append("retire"), create=True), \
                 patch.object(runner, "pick_port", return_value=8917), \
                 patch.object(runner, "read_port", return_value=8917), \
                 patch.object(runner, "port_serving", return_value=True), \
                 patch.object(runner, "spawn_bridge", side_effect=lambda *a: events.append("spawn") or 123), \
                 patch.object(runner, "open_dashboard", side_effect=lambda *a: events.append("open") or "http://127.0.0.1:8917/"), \
                 patch.object(runner, "SERVE_PID_FILE", Path(folder) / "pid"), \
                 patch.object(runner, "SERVE_PORT_FILE", Path(folder) / "port"):
                result = runner.launch_bridge(ROOT, Path(folder) / "test.log")
        self.assertEqual(events, ["retire", "spawn", "open"])
        self.assertFalse(result["reused"])

    def test_current_bridge_is_reused(self):
        with patch.object(runner, "write_log"):
            folder = ROOT / "local" / "bridge-reuse-test"
            with patch.object(runner, "bridge_running", return_value=8917), \
                 patch.object(runner, "bridge_matches", return_value=True, create=True), \
                 patch.object(runner, "read_pid", return_value=123), \
                 patch.object(runner, "spawn_bridge") as spawn, \
                 patch.object(runner, "open_dashboard", return_value="http://127.0.0.1:8917/"):
                result = runner.launch_bridge(ROOT, Path(folder) / "test.log")
        self.assertTrue(result["reused"])
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
