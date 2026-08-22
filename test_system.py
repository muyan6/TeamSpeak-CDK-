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
from music_bot_service import music_bot_client

class TestTeamSpeakManager(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        database.init_db()

    def setUp(self):
        with database.get_connection() as conn:
            conn.execute("DELETE FROM cdks")
            conn.execute("DELETE FROM instances")
            conn.execute("DELETE FROM bot_instances")
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
        cdks = database.create_cdks(count=3, remark="测试CDK", cdk_type="teamspeak")
        self.assertEqual(len(cdks), 3)
        self.assertTrue(cdks[0].startswith("TS-"))

        info = database.get_cdk(cdks[0])
        self.assertIsNotNone(info)
        self.assertEqual(info["status"], "unused")
        self.assertEqual(info["cdk_type"], "teamspeak")
        self.assertEqual(info["remark"], "测试CDK")

    def test_music_bot_cdk_generation_and_duration(self):
        bot_cdks = database.create_cdks(count=2, remark="机器人月卡", cdk_type="music_bot", duration_months=1)
        self.assertEqual(len(bot_cdks), 2)
        self.assertTrue(bot_cdks[0].startswith("BOT-"))

        info = database.get_cdk(bot_cdks[0])
        self.assertIsNotNone(info)
        self.assertEqual(info["cdk_type"], "music_bot")
        self.assertEqual(info["duration_months"], 1)
        self.assertEqual(info["status"], "unused")

    def test_bot_instance_crud(self):
        bot_cdks = database.create_cdks(count=1, remark="季卡", cdk_type="music_bot", duration_months=3)
        cdk = bot_cdks[0]

        bot = database.create_bot_instance(
            bot_id="uuid-test-12345",
            name="测试音乐机",
            server_address="103.71.69.156",
            server_port=60001,
            nickname="TestBot",
            cdk_code=cdk,
            duration_months=3,
            expire_at="2026-11-22 16:00:00",
            default_channel="音乐频道"
        )
        self.assertIsNotNone(bot)
        self.assertEqual(bot["name"], "测试音乐机")
        self.assertEqual(bot["duration_months"], 3)

        database.bind_cdk_bot(cdk, "uuid-test-12345")
        updated_cdk = database.get_cdk(cdk)
        self.assertEqual(updated_cdk["status"], "used")
        self.assertEqual(updated_cdk["bot_id"], "uuid-test-12345")

        found_bot = database.get_bot_instance_by_cdk(cdk)
        self.assertIsNotNone(found_bot)
        self.assertEqual(found_bot["bot_id"], "uuid-test-12345")

        database.update_bot_instance_status("uuid-test-12345", "stopped")
        stopped_bot = database.get_bot_instance_by_id("uuid-test-12345")
        self.assertEqual(stopped_bot["status"], "stopped")

        del_ok = database.delete_bot_instance("uuid-test-12345")
        self.assertTrue(del_ok)
        self.assertIsNone(database.get_bot_instance_by_id("uuid-test-12345"))

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

    def test_token_and_credentials_extraction(self):
        mock_log = """
        2026-08-22 06:18:22.000000|INFO    |ServerLibPriv |   |Server Version: 3.13.7
        ------------------------------------------------------------------
        ServerAdmin privilege key created, please use the following key
        token=h1Y6ZqQxYpW7bT9s8uN2kL4vF3aC1eG0iJ8oR5mP
        ------------------------------------------------------------------
        ------------------------------------------------------------------
        ServerQuery account created
        loginname= "serveradmin" , password= "SecretPassword123!"
        apikey= "AbCdEf123456"
        ------------------------------------------------------------------
        2026-08-22 06:18:22.500000|INFO    |VirtualServer |1  |listening on 0.0.0.0:9987
        """
        creds = docker_service.extract_credentials_from_logs(mock_log)
        self.assertEqual(creds["admin_token"], "h1Y6ZqQxYpW7bT9s8uN2kL4vF3aC1eG0iJ8oR5mP")
        self.assertEqual(creds["query_password"], "SecretPassword123!")
        self.assertEqual(creds["query_apikey"], "AbCdEf123456")

    def test_music_bot_client_connectivity(self):
        # 测试音乐机器人远程平台鉴权与信息抓取
        ok, res = music_bot_client.get_all_bots()
        self.assertTrue(ok)
        self.assertIsInstance(res, dict)
        self.assertIn("bots", res)

    def test_bot_expiration_and_renewal(self):
        # 1. 模拟一个已经超期的机器人
        bot = database.create_bot_instance(
            bot_id="uuid-expire-test",
            name="即将过期机",
            server_address="127.0.0.1",
            server_port=60001,
            nickname="OldBot",
            cdk_code="BOT-OLD-CDK",
            duration_months=1,
            expire_at="2020-01-01 00:00:00",
            status="active"
        )
        # 2. 检查超期扫描函数能够精确命中
        expired = database.get_expired_active_bots()
        self.assertTrue(any(b["bot_id"] == "uuid-expire-test" for b in expired))

        # 3. 使用 1 个月卡进行续费
        renewed = database.renew_bot_instance("uuid-expire-test", 1)
        self.assertIsNotNone(renewed)
        self.assertEqual(renewed["status"], "active")
        self.assertGreater(renewed["expire_at"], "2026-01-01 00:00:00")

        # 4. 再次扫描已不再判定为过期
        expired_after = database.get_expired_active_bots()
        self.assertFalse(any(b["bot_id"] == "uuid-expire-test" for b in expired_after))

if __name__ == "__main__":
    unittest.main()
