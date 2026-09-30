"""Exercise the real local HTTP boundary with synthetic runs only."""
import json
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import serve


class RadarApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = [{"id": str(i), "date": "2026-09-%02d" % (i + 1), "time": "18:00",
                     "distance": 6, "duration": 2400, "pace": 400, "hr": 150,
                     "zones": {"aer": 2400}, "paceSeries": [[m, 400] for m in range(1, 41)],
                     "hrSeries": [[m, 150] for m in range(1, 41)]} for i in range(6)]
        cls.loader = patch.object(serve.metrics, "load", return_value=("", {"runs": cls.runs}, ""))
        cls.loader.start()
        cls.server = serve._Bridge(("127.0.0.1", 0), serve.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.loader.stop()

    def post(self, body, token=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Bridge-Token"] = serve._TOKEN
        request = Request(self.base + "/api/radar/compare", json.dumps(body).encode(), headers)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            return response.code, json.load(response)

    def test_requires_token(self):
        self.assertEqual(self.post({"target": "4"}, token=False)[0], 403)

    def test_comparison_uses_only_prior_runs(self):
        status, body = self.post({"target": "4"})
        self.assertEqual(status, 200)
        self.assertEqual(body["comparison"]["ids"], ["0", "1", "2", "3"])
        self.assertEqual(body["comparison"]["score"], 50)
        self.assertEqual(body["comparison"]["review"]["headline"], "三项表现均在个人波动参考内")
        self.assertIn("对照 4 场记录", body["comparison"]["review"]["comment"])

    def test_custom_single_preserves_facts_without_total(self):
        status, body = self.post({"target": "4", "mode": "single", "ids": ["0"]})
        self.assertEqual(status, 200)
        self.assertIsNone(body["comparison"]["score"])
        self.assertEqual(body["comparison"]["axes"][0]["delta"], 0)

    def test_rejects_future_reference_and_dates(self):
        for body in ({"target": "4", "mode": "manual", "ids": ["5"]},
                     {"target": "4", "end": "2026-09-06"}):
            self.assertEqual(self.post(body)[0], 400)

    def test_static_whitelist(self):
        with urlopen(self.base + "/radar.js", timeout=3) as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base + "/local/mail_config.json", timeout=3)
        self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
