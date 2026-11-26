import sys
import unittest
from fastapi.testclient import TestClient
from patentkb.main import app
from patentkb import config

class TestAuthAndRemote(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.orig_cfg = config.load_config()

    def tearDown(self):
        config.save_config(self.orig_cfg)

    def test_no_token_allows_access(self):
        cfg = dict(self.orig_cfg)
        cfg["server"]["auth_token"] = ""
        config.save_config(cfg)
        res = self.client.get("/api/wizard/status")
        self.assertEqual(res.status_code, 200)

    def test_token_blocks_unauthorized(self):
        cfg = dict(self.orig_cfg)
        cfg["server"]["auth_token"] = "secret123"
        config.save_config(cfg)
        res = self.client.get("/api/wizard/status")
        self.assertEqual(res.status_code, 401)
        self.assertIn("未授权", res.text)

    def test_token_via_header_success(self):
        cfg = dict(self.orig_cfg)
        cfg["server"]["auth_token"] = "secret123"
        config.save_config(cfg)
        res = self.client.get("/api/wizard/status", headers={"Authorization": "Bearer secret123"})
        self.assertEqual(res.status_code, 200)

    def test_token_via_query_param_success(self):
        cfg = dict(self.orig_cfg)
        cfg["server"]["auth_token"] = "secret123"
        config.save_config(cfg)
        res = self.client.get("/api/wizard/status?token=secret123")
        self.assertEqual(res.status_code, 200)

if __name__ == "__main__":
    unittest.main()
