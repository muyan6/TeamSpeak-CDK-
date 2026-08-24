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
            conn.execute("DELETE FROM trial_server_records")
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
        # 测试音乐机器人远程平台鉴权与信息抓取（若远程服务离线则跳过或模拟）
        ok, res = music_bot_client.get_all_bots()
        if ok:
            self.assertIsInstance(res, dict)
            self.assertIn("bots", res)
        else:
            self.assertIsInstance(res, str)

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

    def test_admin_password_management(self):
        # 1. 初始读取默认密码
        default_pwd = database.get_admin_password()
        self.assertTrue(len(default_pwd) >= 6)

        # 2. 修改为新密码
        database.set_admin_password("NewSecurePass888!")
        self.assertEqual(database.get_admin_password(), "NewSecurePass888!")

        # 3. 还原默认密码
        database.set_admin_password(default_pwd)
        self.assertEqual(database.get_admin_password(), default_pwd)

    def test_bot_config_management(self):
        # 1. 初始读取默认配置
        cfg = database.get_bot_config()
        self.assertIn("bot_panel_url", cfg)
        self.assertIn("bot_panel_user", cfg)
        self.assertIn("bot_panel_pass", cfg)

        # 2. 修改配置
        new_url = "http://127.0.0.1:23467"
        new_user = "test_admin"
        new_pass = "TestPass123"
        database.set_bot_config(new_url, new_user, new_pass)

        updated_cfg = database.get_bot_config()
        self.assertEqual(updated_cfg["bot_panel_url"], new_url)
        self.assertEqual(updated_cfg["bot_panel_user"], new_user)
        self.assertEqual(updated_cfg["bot_panel_pass"], new_pass)

        # 3. 恢复配置
        database.set_bot_config(cfg["bot_panel_url"], cfg["bot_panel_user"], cfg["bot_panel_pass"])

    def test_music_bot_test_connection(self):
        # 1. 正常连接测试
        cfg = database.get_bot_config()
        ok, msg, data = music_bot_client.test_connection(cfg["bot_panel_url"], cfg["bot_panel_user"], cfg["bot_panel_pass"])
        if ok:
            self.assertIn("bot_count", data)

        # 2. 错误密码测试
        ok_fail, msg_fail, _ = music_bot_client.test_connection(cfg["bot_panel_url"], cfg["bot_panel_user"], "WrongPasswordXYZ")
        self.assertFalse(ok_fail)

        # 3. 错误 URL 测试
        ok_inv, msg_inv, _ = music_bot_client.test_connection("not_a_valid_url", "user", "pass")
        self.assertFalse(ok_inv)

    def test_bot_config_client_sync_and_persistence(self):
        # 1. 验证修改配置与持久化
        saved = database.set_bot_config("http://127.0.0.1:9999/", "new_admin", "new_password")
        self.assertEqual(saved["bot_panel_url"], "http://127.0.0.1:9999")
        self.assertEqual(saved["bot_panel_user"], "new_admin")
        self.assertEqual(saved["bot_panel_pass"], "new_password")

        # 2. 验证 MusicBotClient 同步机制
        music_bot_client.reload_config()
        self.assertEqual(music_bot_client.base_url, "http://127.0.0.1:9999")
        self.assertEqual(music_bot_client.username, "new_admin")
        self.assertEqual(music_bot_client.password, "new_password")

        # 3. 恢复配置
        database.set_bot_config("http://103.71.69.156:23467", "huasjj", "Fanxing6")
        music_bot_client.reload_config()
        self.assertEqual(music_bot_client.base_url, "http://103.71.69.156:23467")
        self.assertEqual(music_bot_client.username, "huasjj")
        self.assertEqual(music_bot_client.password, "Fanxing6")

    def test_ts_instance_duration_expiration_and_renewal(self):
        # 1. 生成带有 1 个月时长的 TS 卡密
        ts_cdks = database.create_cdks(count=1, remark="TS月卡", cdk_type="teamspeak", duration_months=1)
        self.assertEqual(len(ts_cdks), 1)
        cdk = ts_cdks[0]
        cdk_info = database.get_cdk(cdk)
        self.assertEqual(cdk_info["duration_months"], 1)
        self.assertEqual(cdk_info["cdk_type"], "teamspeak")

        # 2. 创建一个已过期的 TS 实例
        inst = database.create_instance(
            instance_id=10,
            name="ts10",
            container_name="ts-teamspeak-10",
            dir_path="/data/teamspeak/ts10",
            voice_port=60010,
            file_port=20010,
            query_port=30010,
            tsdns_port=40010,
            admin_token="mock-token-10",
            cdk_code=cdk,
            duration_months=1,
            expire_at="2020-01-01 00:00:00",
            status="running"
        )
        self.assertIsNotNone(inst)
        self.assertEqual(inst["expire_at"], "2020-01-01 00:00:00")
        self.assertEqual(inst["duration_months"], 1)

        # 3. 扫描已到期的 TS 服务器
        expired = database.get_expired_active_instances()
        self.assertTrue(any(i["id"] == 10 for i in expired))

        # 4. 使用 1 个月进行续费
        renewed = database.renew_instance(10, 1)
        self.assertIsNotNone(renewed)
        self.assertEqual(renewed["status"], "running")
        self.assertGreater(renewed["expire_at"], "2026-01-01 00:00:00")

        # 5. 再次扫描已不再判定为过期
        expired_after = database.get_expired_active_instances()
        self.assertFalse(any(i["id"] == 10 for i in expired_after))

        # 6. 测试续费为永久有效
        perm_renewed = database.renew_instance(10, 0)
        self.assertEqual(perm_renewed["expire_at"], "permanent")

    def test_renew_instance_api_and_admin_list(self):
        try:
            from fastapi.testclient import TestClient
            import app as ts_app
            client = TestClient(ts_app.app)
        except Exception:
            self.skipTest("FastAPI TestClient 依赖不可用")

        # 1. 创建初始实例
        inst = database.create_instance(
            instance_id=20,
            name="ts20",
            container_name="ts-teamspeak-20",
            dir_path="/data/teamspeak/ts20",
            voice_port=60020,
            file_port=20020,
            query_port=30020,
            tsdns_port=40020,
            admin_token="mock-token-20",
            cdk_code="TS-INIT-CODE",
            duration_months=1,
            expire_at="2020-01-01 00:00:00",
            status="running"
        )

        # 2. 生成一张 3 个月的续费卡密
        renew_cdks = database.create_cdks(count=1, remark="3个月续费卡", cdk_type="teamspeak", duration_months=3)
        renew_cdk = renew_cdks[0]

        # 3. 请求 /api/renew-instance 接口
        resp = client.post("/api/renew-instance", json={"cdk": renew_cdk, "instance_id": 20})
        self.assertEqual(resp.status_code, 200)
        res_data = resp.json()
        self.assertTrue(res_data["success"])
        self.assertEqual(res_data["type"], "teamspeak")
        self.assertGreater(res_data["instance"]["expire_at"], "2026-01-01 00:00:00")

        # 4. 验证 CDK 状态变为 used
        cdk_info = database.get_cdk(renew_cdk)
        self.assertEqual(cdk_info["status"], "used")
        self.assertEqual(cdk_info["instance_id"], 20)

        # 5. 请求 /api/admin/instances 接口验证 days_left 属性
        admin_pwd = database.get_admin_password()
        admin_resp = client.get("/api/admin/instances", headers={"X-Admin-Password": admin_pwd})
        self.assertEqual(admin_resp.status_code, 200)
        admin_data = admin_resp.json()
        self.assertTrue(admin_data["success"])
        found = next((i for i in admin_data["instances"] if i["id"] == 20), None)
        self.assertIsNotNone(found)
        self.assertIn("days_left", found)
        self.assertFalse(found["is_expired"])

    def test_batch_delete_cdks_by_codes_and_filter(self):
        # 1. 批量创建不同时长与类型的 CDK
        # 3张月卡 (1个月)
        m_cdks = database.create_cdks(count=3, remark="月卡批次", cdk_type="teamspeak", duration_months=1)
        # 2张季卡 (3个月)
        q_cdks = database.create_cdks(count=2, remark="季卡批次", cdk_type="teamspeak", duration_months=3)
        # 2张年卡 (12个月) 音乐机器人
        y_cdks = database.create_cdks(count=2, remark="机器人年卡", cdk_type="music_bot", duration_months=12)
        # 2张永久卡 (0)
        p_cdks = database.create_cdks(count=2, remark="永久卡", cdk_type="teamspeak", duration_months=0)

        # 标记其中一张月卡为已使用
        database.bind_cdk_instance(m_cdks[0], 100)

        # 2. 测试根据 code 列表批量删除
        deleted_count = database.delete_cdks([m_cdks[1], m_cdks[2]])
        self.assertEqual(deleted_count, 2)
        self.assertIsNone(database.get_cdk(m_cdks[1]))
        self.assertIsNone(database.get_cdk(m_cdks[2]))

        # 3. 测试根据时长过滤批量删除 (删除所有季卡 duration_months=3)
        deleted_q = database.delete_cdks_by_filter(duration_months=3)
        self.assertEqual(deleted_q, 2)
        self.assertIsNone(database.get_cdk(q_cdks[0]))
        self.assertIsNone(database.get_cdk(q_cdks[1]))

        # 4. 测试按类型与时长联合过滤批量删除 (删除音乐机器人年卡)
        deleted_bot_y = database.delete_cdks_by_filter(cdk_type="music_bot", duration_months=12)
        self.assertEqual(deleted_bot_y, 2)
        self.assertIsNone(database.get_cdk(y_cdks[0]))

        # 5. 测试按使用状态过滤 (仅删除已使用状态)
        deleted_used = database.delete_cdks_by_filter(status="used")
        self.assertEqual(deleted_used, 1)  # 即 m_cdks[0]
        self.assertIsNone(database.get_cdk(m_cdks[0]))

        # 6. 测试删除永久卡 (duration_months=0)
        deleted_perm = database.delete_cdks_by_filter(duration_months=0)
        self.assertEqual(deleted_perm, 2)
        self.assertIsNone(database.get_cdk(p_cdks[0]))

    def test_batch_delete_instances_and_bots(self):
        # 1. 批量创建 TS 实例
        database.create_instance(31, "ts31", "ts-teamspeak-31", "/data/teamspeak/ts31", 60031, 20031, 30031, 40031, "token31")
        database.create_instance(32, "ts32", "ts-teamspeak-32", "/data/teamspeak/ts32", 60032, 20032, 30032, 40032, "token32")
        self.assertIsNotNone(database.get_instance_by_id(31))
        self.assertIsNotNone(database.get_instance_by_id(32))

        del_inst_count = database.delete_instances([31, 32])
        self.assertEqual(del_inst_count, 2)
        self.assertIsNone(database.get_instance_by_id(31))
        self.assertIsNone(database.get_instance_by_id(32))

        # 2. 批量创建机器人
        database.create_bot_instance("bot-del-1", "Bot1", "127.0.0.1", 9987, "Nick1", "CDK1", 1, "2026-01-01 00:00:00")
        database.create_bot_instance("bot-del-2", "Bot2", "127.0.0.1", 9987, "Nick2", "CDK2", 1, "2026-01-01 00:00:00")
        self.assertIsNotNone(database.get_bot_instance_by_id("bot-del-1"))
        self.assertIsNotNone(database.get_bot_instance_by_id("bot-del-2"))

        del_bot_count = database.delete_bot_instances(["bot-del-1", "bot-del-2"])
        self.assertEqual(del_bot_count, 2)
        self.assertIsNone(database.get_bot_instance_by_id("bot-del-1"))
        self.assertIsNone(database.get_bot_instance_by_id("bot-del-2"))

    def test_trial_cdk_generation_and_properties(self):
        # 1. 生成体验卡（is_trial=1）
        trial_cdks = database.create_cdks(count=2, remark="首月体验卡", cdk_type="music_bot", duration_months=1, is_trial=1)
        self.assertEqual(len(trial_cdks), 2)
        cdk = trial_cdks[0]

        info = database.get_cdk(cdk)
        self.assertIsNotNone(info)
        self.assertEqual(info["is_trial"], 1)
        self.assertEqual(info["duration_months"], 1)  # 体验卡时长为月卡 (1个月)
        self.assertEqual(info["cdk_type"], "music_bot")
        self.assertEqual(info["status"], "unused")

    def test_trial_server_normalization_and_detection(self):
        # 1. 规范化测试
        key1, addr1, port1, ip1, res_key1 = database.normalize_server_target("http://103.71.69.156:9987/")
        self.assertEqual(key1, "103.71.69.156:9987")
        self.assertEqual(port1, 9987)

        key2, addr2, port2, ip2, res_key2 = database.normalize_server_target("103.71.69.156", 60001)
        self.assertEqual(key2, "103.71.69.156:60001")
        self.assertEqual(port2, 60001)

        # 2. 初始状态未被记录
        used, rec = database.has_server_used_trial("103.71.69.156", 9987)
        self.assertFalse(used)
        self.assertIsNone(rec)

        # 3. 记录 9987 端口为已使用
        database.record_trial_server("103.71.69.156", 9987, "BOT-TRIAL-001", "music_bot", target_id="bot-1")

        # 4. 再次检测 9987 -> 判定为已使用
        used9987, rec9987 = database.has_server_used_trial("103.71.69.156", 9987)
        self.assertTrue(used9987)
        self.assertIsNotNone(rec9987)
        self.assertEqual(rec9987["cdk_code"], "BOT-TRIAL-001")

        # 5. 检测同一 IP 的不同端口 60001 -> 判定为未使用（独立服务器不拦截）
        used60001, rec60001 = database.has_server_used_trial("103.71.69.156", 60001)
        self.assertFalse(used60001)

    def test_trial_bot_redeem_and_renewal_api_flow(self):
        try:
            from unittest.mock import patch
            from fastapi.testclient import TestClient
            import app as ts_app
            client = TestClient(ts_app.app)
        except Exception:
            self.skipTest("FastAPI TestClient 依赖不可用")

        # 1. 生成 2 张音乐机器人体验卡和 1 张普通月卡
        trial_cdks = database.create_cdks(count=2, remark="体验卡批次", cdk_type="music_bot", is_trial=1)
        normal_cdks = database.create_cdks(count=1, remark="普通月卡", cdk_type="music_bot", duration_months=1, is_trial=0)

        trial1, trial2 = trial_cdks[0], trial_cdks[1]
        normal1 = normal_cdks[0]

        # 2. 验证体验卡查询接口返回 is_trial 及体验卡说明
        resp_query = client.post("/api/redeem", json={"cdk": trial1})
        self.assertEqual(resp_query.status_code, 200)
        q_data = resp_query.json()
        self.assertTrue(q_data["success"])
        self.assertTrue(q_data["is_trial"])
        self.assertIn("体验卡", q_data["duration_desc"])

        # 3. 第一次使用体验卡开通目标服务器 1.2.3.4:9987
        with patch.object(ts_app.music_bot_client, "create_bot", return_value=(True, {"id": "mock-bot-trial-1"})):
            resp1 = client.post("/api/redeem-bot", json={
                "cdk": trial1,
                "name": "体验机1",
                "serverAddress": "1.2.3.4",
                "serverPort": 9987,
                "nickname": "TrialBot"
            })
            self.assertEqual(resp1.status_code, 200)
            res1 = resp1.json()
            self.assertTrue(res1["success"])
            self.assertEqual(res1["instance"]["bot_id"], "mock-bot-trial-1")

        # 4. 再次尝试使用第二张体验卡为同一个服务器 1.2.3.4:9987 开通 -> 必须被拦截并提示退款
        with patch.object(ts_app.music_bot_client, "create_bot", return_value=(True, {"id": "mock-bot-trial-2"})):
            resp2 = client.post("/api/redeem-bot", json={
                "cdk": trial2,
                "name": "体验机2",
                "serverAddress": "1.2.3.4",
                "serverPort": 9987,
                "nickname": "TrialBot2"
            })
            self.assertEqual(resp2.status_code, 400)
            res2 = resp2.json()
            self.assertFalse(res2["success"])
            self.assertIn("只能使用一次体验卡，请联系退款", res2["message"])

        # 5. 同一 IP 不同端口 1.2.3.4:60001 使用第二张体验卡开通 -> 允许成功
        with patch.object(ts_app.music_bot_client, "create_bot", return_value=(True, {"id": "mock-bot-trial-port2"})):
            resp3 = client.post("/api/redeem-bot", json={
                "cdk": trial2,
                "name": "体验机端口2",
                "serverAddress": "1.2.3.4",
                "serverPort": 60001,
                "nickname": "TrialBotPort2"
            })
            self.assertEqual(resp3.status_code, 200)
            res3 = resp3.json()
            self.assertTrue(res3["success"])

        # 6. 生成第三张体验卡，尝试对已使用过体验卡的 mock-bot-trial-1 进行续费 -> 必须被拦截
        trial3 = database.create_cdks(count=1, remark="体验卡3", cdk_type="music_bot", is_trial=1)[0]
        resp_renew_fail = client.post("/api/renew-bot", json={"cdk": trial3, "bot_id": "mock-bot-trial-1"})
        self.assertEqual(resp_renew_fail.status_code, 400)
        self.assertIn("只能使用一次体验卡，请联系退款", resp_renew_fail.json()["message"])

        # 7. 使用普通月卡进行续费 -> 允许成功
        with patch.object(ts_app.music_bot_client, "start_bot", return_value=(True, {})):
            resp_renew_ok = client.post("/api/renew-bot", json={"cdk": normal1, "bot_id": "mock-bot-trial-1"})
            self.assertEqual(resp_renew_ok.status_code, 200)
            self.assertTrue(resp_renew_ok.json()["success"])

    def test_trial_records_admin_api_and_reset(self):
        try:
            from fastapi.testclient import TestClient
            import app as ts_app
            client = TestClient(ts_app.app)
        except Exception:
            self.skipTest("FastAPI TestClient 依赖不可用")

        admin_pwd = database.get_admin_password()

        # 1. 记录一条体验卡记录
        rec = database.record_trial_server("10.0.0.1", 9987, "BOT-TRIAL-TEST", "music_bot", target_id="bot-test")
        self.assertIsNotNone(rec.get("id"))

        # 2. 查询体验记录列表
        resp_list = client.get("/api/admin/trial-records", headers={"X-Admin-Password": admin_pwd})
        self.assertEqual(resp_list.status_code, 200)
        data = resp_list.json()
        self.assertTrue(data["success"])
        self.assertTrue(any(r["server_key"] == "10.0.0.1:9987" for r in data["records"]))

        # 3. 验证此时该服务器检测为已使用
        used, _ = database.has_server_used_trial("10.0.0.1", 9987)
        self.assertTrue(used)

        # 4. 管理员重置该服务器的体验记录
        rec_id = next(r["id"] for r in data["records"] if r["server_key"] == "10.0.0.1:9987")
        resp_del = client.delete(f"/api/admin/trial-records/{rec_id}", headers={"X-Admin-Password": admin_pwd})
        self.assertEqual(resp_del.status_code, 200)
        self.assertTrue(resp_del.json()["success"])

        # 5. 验证重置后该服务器可再次使用体验卡
        used_after, _ = database.has_server_used_trial("10.0.0.1", 9987)
        self.assertFalse(used_after)

    def test_permanent_renewal_protection(self):
        # 1. 创建永久 TS 实例并续费月卡 -> 仍保持 permanent
        inst = database.create_instance(99, "ts99", "ts-teamspeak-99", "/data/teamspeak/ts99", 60099, 20099, 30099, 40099, "tok99", expire_at="permanent")
        renewed = database.renew_instance(99, 1)
        self.assertEqual(renewed["expire_at"], "permanent")

        # 2. 创建永久机器人实例并续费月卡 -> 仍保持 permanent
        bot = database.create_bot_instance("bot-perm-1", "PermBot", "127.0.0.1", 9987, "PBot", "CDK-PERM", expire_at="permanent")
        renewed_bot = database.renew_bot_instance("bot-perm-1", 1)
        self.assertEqual(renewed_bot["expire_at"], "permanent")

    def test_used_cdk_fallthrough_protection(self):
        try:
            from fastapi.testclient import TestClient
            import app as ts_app
            client = TestClient(ts_app.app)
        except Exception:
            self.skipTest("FastAPI TestClient 依赖不可用")

        # 创建一个已使用的 CDK 但不创建对应的 instance
        cdks = database.create_cdks(count=1, remark="已用卡", cdk_type="teamspeak", duration_months=1)
        cdk = cdks[0]
        database.bind_cdk_instance(cdk, 99999) # 绑定一个不存在的实例

        resp = client.post("/api/redeem", json={"cdk": cdk})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("绑定的 TeamSpeak 实例已不存在", resp.json()["message"])

    def test_filter_delete_trial_and_duration_combination(self):
        # 创建 2 张普通月卡 (is_trial=0, duration=1), 2 张体验卡 (is_trial=1, duration=1), 2 张普通季卡 (is_trial=0, duration=3)
        c_m = database.create_cdks(count=2, remark="普通月卡", cdk_type="teamspeak", duration_months=1, is_trial=0)
        c_t = database.create_cdks(count=2, remark="体验卡", cdk_type="teamspeak", duration_months=1, is_trial=1)
        c_q = database.create_cdks(count=2, remark="普通季卡", cdk_type="teamspeak", duration_months=3, is_trial=0)

        # 仅删除普通月卡 (is_trial=0, duration_months=1)
        del_count = database.delete_cdks_by_filter(is_trial=0, duration_months=1)
        self.assertEqual(del_count, 2)
        # 验证体验卡和季卡仍然存在
        self.assertIsNotNone(database.get_cdk(c_t[0]))
        self.assertIsNotNone(database.get_cdk(c_q[0]))

if __name__ == "__main__":
    unittest.main()
