import os
import shutil
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from patentkb.main import app
from patentkb import config, db

class TestDeviceManagementAndAccess(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.kb_dir = Path(self.tmp_dir) / "kb"
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_file = Path(self.tmp_dir) / "config.json"
        
        self.orig_env = os.environ.get("PATENTKB_CONFIG")
        os.environ["PATENTKB_CONFIG"] = str(self.cfg_file)
        
        cfg = config.load_config()
        cfg["kb_path"] = str(self.kb_dir.resolve())
        cfg["server"]["auth_token"] = "master_secret_888"
        config.save_config(cfg)
        
        db.reset()
        db.init(str(self.kb_dir.resolve()))
        self.client = TestClient(app)

    def tearDown(self):
        db.reset()
        if self.orig_env is not None:
            os.environ["PATENTKB_CONFIG"] = self.orig_env
        else:
            os.environ.pop("PATENTKB_CONFIG", None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_device_crud_and_remote_access(self):
        # 1. 新增测试设备（如员工：张三）
        dev = db.add_device("张三-离职测试机", "test_token_zhangsan", "研发部")
        dev_id = dev["id"]
        token = dev["token"]
        self.assertEqual(dev["is_active"], 1)

        # 2. 模拟远程客户端（Tailscale 或远程 IP）携带该 Token 访问
        remote_client = TestClient(app, client=("100.68.46.99", 52100))
        res = remote_client.get(f"/api/wizard/status?token={token}")
        self.assertEqual(res.status_code, 200)

        # 验证 last_active_at 与 last_ip 已记录
        updated_dev = db.get_device_by_token(token)
        self.assertIsNotNone(updated_dev["last_active_at"])
        self.assertEqual(updated_dev["last_ip"], "100.68.46.99")

        # 3. 普通设备无法越权访问管理员配置接口
        res_admin = remote_client.get(f"/api/admin/devices?token={token}")
        self.assertEqual(res_admin.status_code, 403)
        self.assertIn("无权访问监管后台设置", res_admin.text)

        # 4. 管理员在后台将员工「张三」标记为离职/禁用 (is_active = 0)
        db.set_device_active(dev_id, 0)
        dev_inactive = db.get_device_by_token(token)
        self.assertEqual(dev_inactive["is_active"], 0)

        # 5. 离职员工设备再次访问，必须立即被 403 阻断拒绝！
        res_blocked = remote_client.get(f"/api/wizard/status?token={token}")
        self.assertEqual(res_blocked.status_code, 403)
        self.assertIn("已被管理员限制或离职注销", res_blocked.text)

        # 6. 本地客户端（127.0.0.1）不受任何阻断，始终免密正常访问
        local_res = self.client.get("/api/wizard/status")
        self.assertEqual(local_res.status_code, 200)

        # 7. 管理员恢复授权 (is_active = 1)
        db.set_device_active(dev_id, 1)
        res_recovered = remote_client.get(f"/api/wizard/status?token={token}")
        self.assertEqual(res_recovered.status_code, 200)

        # 8. 清理测试设备
        db.delete_device(dev_id)
        self.assertIsNone(db.get_device_by_token(token))

    def test_admin_api_endpoints(self):
        # 1. 本地免密访问管理接口创建设备
        res_add = self.client.post("/api/admin/devices", json={
            "device_name": "李四-测试",
            "remark": "临时外包"
        })
        self.assertEqual(res_add.status_code, 200)
        data = res_add.json()
        dev = data["device"]
        dev_id = dev["id"]
        self.assertTrue(dev["token"].startswith("pkb_"))

        # 2. 本地查看列表
        res_list = self.client.get("/api/admin/devices")
        self.assertEqual(res_list.status_code, 200)
        devices = res_list.json()["devices"]
        self.assertTrue(any(d["id"] == dev_id for d in devices))

        # 3. 远程携带超级管理员主口令访问管理接口也允许
        remote_admin = TestClient(app, client=("100.68.46.99", 52200))
        res_remote_admin = remote_admin.get("/api/admin/devices?token=master_secret_888")
        self.assertEqual(res_remote_admin.status_code, 200)

        # 4. 切换状态为禁用
        res_toggle = self.client.post(f"/api/admin/devices/{dev_id}/toggle", json={"is_active": 0})
        self.assertEqual(res_toggle.status_code, 200)
        self.assertEqual(res_toggle.json()["is_active"], 0)

        # 5. 删除设备
        res_del = self.client.delete(f"/api/admin/devices/{dev_id}")
        self.assertEqual(res_del.status_code, 200)

if __name__ == "__main__":
    unittest.main()
