import os
import shutil
import unittest
from pathlib import Path

# 设置测试环境变量
test_db = str(Path(__file__).parent.resolve() / "test_teamspeak.db")
os.environ["DB_PATH"] = test_db
test_data_dir = str(Path(__file__).parent.resolve() / "test_data")
os.environ["TS_DATA_DIR"] = test_data_dir

import database
import port_manager
import docker_service

class TestTeamSpeakManager(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        database.init_db()

    def setUp(self):
        with database.get_connection() as conn:
            conn.execute("DELETE FROM cdks")
            conn.execute("DELETE FROM instances")
            conn.commit()
        if os.path.exists(test_data_dir):
            shutil.rmtree(test_data_dir, ignore_errors=True)

    def tearDown(self):
        if os.path.exists(test_data_dir):
            shutil.rmtree(test_data_dir, ignore_errors=True)

    @classmethod
    def tearDownClass(cls):
        try:
            if os.path.exists(test_db):
                os.remove(test_db)
        except Exception:
            pass

    def test_cdk_generation_and_validation(self):
        cdks = database.create_cdks(count=3, remark="测试CDK")
        self.assertEqual(len(cdks), 3)
        self.assertTrue(cdks[0].startswith("TS-"))

        info = database.get_cdk(cdks[0])
        self.assertIsNotNone(info)
        self.assertEqual(info["status"], "unused")
        self.assertEqual(info["remark"], "测试CDK")

    def test_port_allocation_sequence(self):
        # 第一次分配（对应 ts1）
        id1, ports1 = port_manager.allocate_ports_for_instance()
        self.assertEqual(id1, 1)
        self.assertEqual(ports1["voice"], 60001)
        self.assertEqual(ports1["file"], 20001)
        self.assertEqual(ports1["query"], 30001)
        self.assertEqual(ports1["tsdns"], 40001)

        # 模拟 ts1 已写入数据库
        database.create_instance(
            instance_id=1,
            name="ts1",
            container_name="ts-teamspeak-1",
            dir_path="/data/teamspeak/ts1",
            voice_port=ports1["voice"],
            file_port=ports1["file"],
            query_port=ports1["query"],
            tsdns_port=ports1["tsdns"]
        )

        # 第二次分配（对应 ts2）
        id2, ports2 = port_manager.allocate_ports_for_instance()
        self.assertEqual(id2, 2)
        self.assertEqual(ports2["voice"], 60002)
        self.assertEqual(ports2["file"], 20002)
        self.assertEqual(ports2["query"], 30002)
        self.assertEqual(ports2["tsdns"], 40002)

    def test_compose_yaml_generation(self):
        ports = {"voice": 60001, "file": 20001, "query": 30001, "tsdns": 40001}
        yaml_str = docker_service.generate_compose_yaml_content(1, ports)
        self.assertIn("teamspeak1:", yaml_str)
        self.assertIn("container_name: ts-teamspeak-1", yaml_str)
        self.assertIn('"60001:9987/udp"', yaml_str)
        self.assertIn('"20001:30033"', yaml_str)
        self.assertIn('"30001:10011"', yaml_str)
        self.assertIn('"40001:41144"', yaml_str)
        self.assertIn("./data:/var/ts3server", yaml_str)

    def test_token_extraction(self):
        mock_log = """
        2026-08-22 06:18:22.000000|INFO    |ServerLibPriv |   |Server Version: 3.13.7
        ------------------------------------------------------------------
        ServerAdmin privilege key created, please use the following key
        token=h1Y6ZqQxYpW7bT9s8uN2kL4vF3aC1eG0iJ8oR5mP
        ------------------------------------------------------------------
        2026-08-22 06:18:22.500000|INFO    |VirtualServer |1  |listening on 0.0.0.0:9987
        """
        token = docker_service.extract_admin_token_from_logs(mock_log)
        self.assertEqual(token, "h1Y6ZqQxYpW7bT9s8uN2kL4vF3aC1eG0iJ8oR5mP")

    def test_cdk_reuse_and_binding(self):
        cdks = database.create_cdks(count=1, remark="独享CDK")
        code = cdks[0]
        
        # 绑定实例
        database.create_instance(
            instance_id=1,
            name="ts1",
            container_name="ts-teamspeak-1",
            dir_path="/data/teamspeak/ts1",
            voice_port=60001,
            file_port=20001,
            query_port=30001,
            tsdns_port=40001,
            admin_token="my-token-123"
        )
        database.bind_cdk_instance(code, 1)

        cdk_info = database.get_cdk(code)
        self.assertEqual(cdk_info["status"], "used")
        self.assertEqual(cdk_info["instance_id"], 1)

        instance = database.get_instance_by_id(cdk_info["instance_id"])
        self.assertEqual(instance["name"], "ts1")
        self.assertEqual(instance["admin_token"], "my-token-123")

    def test_port_collision_skip(self):
        # 预先占用 ts1 的端口
        database.create_instance(
            instance_id=1,
            name="ts1",
            container_name="ts-teamspeak-1",
            dir_path="/data/teamspeak/ts1",
            voice_port=60001,
            file_port=20001,
            query_port=30001,
            tsdns_port=40001
        )
        # 预先占用 ts2 的端口
        database.create_instance(
            instance_id=2,
            name="ts2",
            container_name="ts-teamspeak-2",
            dir_path="/data/teamspeak/ts2",
            voice_port=60002,
            file_port=20002,
            query_port=30002,
            tsdns_port=40002
        )

        # 此时新分配应该直接拿到 ts3 和对应端口
        next_id, next_ports = port_manager.allocate_ports_for_instance()
        self.assertEqual(next_id, 3)
        self.assertEqual(next_ports["voice"], 60003)
        self.assertEqual(next_ports["file"], 20003)
        self.assertEqual(next_ports["query"], 30003)
        self.assertEqual(next_ports["tsdns"], 40003)

if __name__ == "__main__":
    unittest.main()
