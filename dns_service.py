import json
import re
import urllib.request
import urllib.parse
import urllib.error
import hmac
import hashlib
import base64
import time
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional

# --- 辅助工具 ---

def clean_subdomain_prefix(prefix: str) -> str:
    """清理并规范化二级域名前缀"""
    return (prefix or "").strip().lower()

def validate_subdomain_format(prefix: str) -> Tuple[bool, str]:
    """校验二级域名前缀格式"""
    p = clean_subdomain_prefix(prefix)
    if not p:
        return False, "二级域名前缀不能为空"
    if len(p) < 2 or len(p) > 32:
        return False, "二级域名前缀长度必须在 2 到 32 个字符之间"
    if not re.match(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$", p):
        return False, "二级域名前缀只能包含小写字母、数字或中划线(-)，且不能以中划线开头或结尾"
    
    reserved = {"www", "admin", "api", "mail", "email", "pop3", "smtp", "imap", "node", "node1", "node2",
                "ts", "ts3", "ftp", "ssh", "ns1", "ns2", "dns", "dev", "test", "status", "panel"}
    if p in reserved:
        return False, f"前缀 [{p}] 为系统保留名称，不可使用"
    return True, ""


# --- 1. Cloudflare Provider ---

class CloudflareDnsProvider:
    API_BASE = "https://api.cloudflare.com/client/v4"

    @classmethod
    def test_connection(cls, token: str, zone_id: str) -> Tuple[bool, str]:
        if not token or not zone_id:
            return False, "Cloudflare API Token 和 Zone ID 不能为空"
        url = f"{cls.API_BASE}/zones/{zone_id.strip()}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token.strip()}",
                "Content-Type": "application/json",
                "User-Agent": "TS3-CDK-Manager/1.0"
            },
            method="GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("success"):
                    zone_name = data.get("result", {}).get("name", "")
                    return True, f"Cloudflare 鉴权成功！Zone 域名: {zone_name}"
                errors = data.get("errors", [])
                err_msg = errors[0].get("message", "未知错误") if errors else "请求未成功"
                return False, f"Cloudflare 验证失败: {err_msg}"
        except urllib.error.HTTPError as e:
            try:
                err_json = json.loads(e.read().decode("utf-8"))
                err_msg = err_json.get("errors", [{}])[0].get("message", e.reason)
            except Exception:
                err_msg = str(e.reason)
            return False, f"Cloudflare HTTP 错误 ({e.code}): {err_msg}"
        except Exception as e:
            return False, f"Cloudflare 连接异常: {str(e)}"

    @classmethod
    def create_srv_record(
        cls,
        token: str,
        zone_id: str,
        root_domain: str,
        subdomain_prefix: str,
        target_host: str,
        voice_port: int,
        priority: int = 0,
        weight: int = 5
    ) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
        """
        在 Cloudflare 创建 TeamSpeak 3 SRV 记录
        记录格式: _ts3._udp.<subdomain>.<root_domain> -> target_host:voice_port
        返回: (success, record_id, full_domain, error_msg)
        """
        sub_p = clean_subdomain_prefix(subdomain_prefix)
        root_d = root_domain.strip().lower().rstrip(".")
        full_subdomain = f"{sub_p}.{root_d}"

        # Cloudflare SRV payload
        # name: _ts3._udp.subdomain
        payload = {
            "type": "SRV",
            "data": {
                "service": "_ts3",
                "proto": "_udp",
                "name": sub_p,
                "priority": priority,
                "weight": weight,
                "port": int(voice_port),
                "target": target_host.strip().rstrip(".")
            },
            "ttl": 1  # 1 为 Auto
        }

        url = f"{cls.API_BASE}/zones/{zone_id.strip()}/dns_records"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token.strip()}",
                "Content-Type": "application/json",
                "User-Agent": "TS3-CDK-Manager/1.0"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("success"):
                    record_id = data.get("result", {}).get("id")
                    return True, str(record_id), full_subdomain, None
                errors = data.get("errors", [])
                err_msg = errors[0].get("message", "未知错误") if errors else "创建失败"
                return False, None, full_subdomain, f"Cloudflare 创建失败: {err_msg}"
        except urllib.error.HTTPError as e:
            try:
                err_json = json.loads(e.read().decode("utf-8"))
                err_msg = err_json.get("errors", [{}])[0].get("message", e.reason)
            except Exception:
                err_msg = str(e.reason)
            return False, None, full_subdomain, f"Cloudflare 创建错误 ({e.code}): {err_msg}"
        except Exception as e:
            return False, None, full_subdomain, f"Cloudflare 创建异常: {str(e)}"

    @classmethod
    def delete_record(cls, token: str, zone_id: str, record_id: str) -> Tuple[bool, Optional[str]]:
        if not record_id:
            return True, None
        url = f"{cls.API_BASE}/zones/{zone_id.strip()}/dns_records/{record_id.strip()}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token.strip()}",
                "Content-Type": "application/json",
                "User-Agent": "TS3-CDK-Manager/1.0"
            },
            method="DELETE"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("success"):
                    return True, None
                errors = data.get("errors", [])
                err_msg = errors[0].get("message", "未知错误") if errors else "删除失败"
                return False, f"Cloudflare 删除失败: {err_msg}"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True, None  # 已经不存在，视为成功
            return False, f"Cloudflare 删除错误 ({e.code}): {e.reason}"
        except Exception as e:
            return False, f"Cloudflare 删除异常: {str(e)}"


# --- 2. 阿里云 DNS (Alibaba Cloud DNS API) ---

class AliyunDnsProvider:
    ENDPOINT = "https://alidns.aliyuncs.com/"

    @classmethod
    def _sign(cls, params: Dict[str, str], access_key_secret: str, method: str = "GET") -> str:
        sorted_params = sorted(params.items(), key=lambda x: x[0])
        canonicalized_query = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
            for k, v in sorted_params
        )
        string_to_sign = f"{method}&{urllib.parse.quote('/', safe='')}&{urllib.parse.quote(canonicalized_query, safe='')}"
        key = f"{access_key_secret}&".encode("utf-8")
        signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
        return base64.b64encode(signature).decode("utf-8")

    @classmethod
    def _request(cls, action: str, access_key_id: str, access_key_secret: str, extra_params: Dict[str, Any]) -> Dict[str, Any]:
        params = {
            "Format": "JSON",
            "Version": "2015-01-09",
            "AccessKeyId": access_key_id.strip(),
            "SignatureMethod": "HMAC-SHA1",
            "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "SignatureVersion": "1.0",
            "SignatureNonce": str(int(time.time() * 1000)) + str(time.time_ns())[-4:],
            "Action": action,
            **extra_params
        }
        params["Signature"] = cls._sign(params, access_key_secret.strip(), "GET")
        url = f"{cls.ENDPOINT}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "TS3-CDK-Manager/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @classmethod
    def test_connection(cls, access_key_id: str, access_key_secret: str, root_domain: str) -> Tuple[bool, str]:
        if not access_key_id or not access_key_secret or not root_domain:
            return False, "阿里云 AccessKeyId、AccessKeySecret 和主域名不能为空"
        try:
            res = cls._request("DescribeDomainInfo", access_key_id, access_key_secret, {"DomainName": root_domain.strip()})
            if "DomainId" in res or "DomainName" in res:
                return True, f"阿里云 DNS 鉴权成功！主域名: {res.get('DomainName', root_domain)}"
            return False, f"阿里云 DNS 响应异常: {res.get('Message', '未获取到域名信息')}"
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read().decode("utf-8"))
                return False, f"阿里云 HTTP 错误 ({e.code}): {err_data.get('Message', e.reason)}"
            except Exception:
                return False, f"阿里云 HTTP 错误 ({e.code}): {e.reason}"
        except Exception as e:
            return False, f"阿里云 DNS 连接异常: {str(e)}"

    @classmethod
    def create_srv_record(
        cls,
        access_key_id: str,
        access_key_secret: str,
        root_domain: str,
        subdomain_prefix: str,
        target_host: str,
        voice_port: int,
        priority: int = 0,
        weight: int = 5
    ) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
        sub_p = clean_subdomain_prefix(subdomain_prefix)
        root_d = root_domain.strip().lower().rstrip(".")
        full_subdomain = f"{sub_p}.{root_d}"
        
        # 阿里云 SRV 记录 RR 格式: _ts3._udp.<subdomain>
        rr = f"_ts3._udp.{sub_p}"
        # Value 格式: <Priority> <Weight> <Port> <Target>
        value = f"{priority} {weight} {int(voice_port)} {target_host.strip().rstrip('.')}"

        try:
            res = cls._request(
                "AddDomainRecord",
                access_key_id,
                access_key_secret,
                {
                    "DomainName": root_d,
                    "RR": rr,
                    "Type": "SRV",
                    "Value": value
                }
            )
            record_id = res.get("RecordId")
            if record_id:
                return True, str(record_id), full_subdomain, None
            return False, None, full_subdomain, f"阿里云创建失败: {res.get('Message', '未返回 RecordId')}"
        except Exception as e:
            return False, None, full_subdomain, f"阿里云创建异常: {str(e)}"

    @classmethod
    def delete_record(cls, access_key_id: str, access_key_secret: str, record_id: str) -> Tuple[bool, Optional[str]]:
        if not record_id:
            return True, None
        try:
            cls._request("DeleteDomainRecord", access_key_id, access_key_secret, {"RecordId": record_id.strip()})
            return True, None
        except Exception as e:
            return False, f"阿里云删除异常: {str(e)}"


# --- 3. 腾讯云 DNSPod (Tencent Cloud DNSPod API 3.0) ---

class TencentDnsProvider:
    ENDPOINT = "dnspod.tencentcloudapi.com"

    @classmethod
    def _request_tc3(
        cls,
        secret_id: str,
        secret_key: str,
        action: str,
        payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        service = "dnspod"
        host = cls.ENDPOINT
        version = "2021-03-23"
        algorithm = "TC3-HMAC-SHA256"
        timestamp = int(time.time())
        date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")

        http_request_method = "POST"
        canonical_uri = "/"
        canonical_querystring = ""
        canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{host}\n"
        signed_headers = "content-type;host"
        payload_str = json.dumps(payload)
        hashed_request_payload = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
        canonical_request = f"{http_request_method}\n{canonical_uri}\n{canonical_querystring}\n{canonical_headers}\n{signed_headers}\n{hashed_request_payload}"

        credential_scope = f"{date}/{service}/tc3_request"
        hashed_canonical_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        string_to_sign = f"{algorithm}\n{timestamp}\n{credential_scope}\n{hashed_canonical_request}"

        def _hmac_sha256(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        secret_date = _hmac_sha256(f"TC3{secret_key.strip()}".encode("utf-8"), date)
        secret_service = _hmac_sha256(secret_date, service)
        secret_signing = _hmac_sha256(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        authorization = f"{algorithm} Credential={secret_id.strip()}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}"

        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json; charset=utf-8",
            "Host": host,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": version,
            "User-Agent": "TS3-CDK-Manager/1.0"
        }

        req = urllib.request.Request(f"https://{host}/", data=payload_str.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @classmethod
    def test_connection(cls, secret_id: str, secret_key: str, root_domain: str) -> Tuple[bool, str]:
        if not secret_id or not secret_key or not root_domain:
            return False, "腾讯云 SecretId、SecretKey 和主域名不能为空"
        try:
            res = cls._request_tc3(secret_id, secret_key, "DescribeDomain", {"Domain": root_domain.strip()})
            resp_data = res.get("Response", {})
            if "Error" in resp_data:
                return False, f"腾讯云 DNS 错误: {resp_data['Error'].get('Message', '鉴权失败')}"
            domain_info = resp_data.get("DomainInfo", {})
            return True, f"腾讯云 DNSPod 鉴权成功！主域名: {domain_info.get('Name', root_domain)}"
        except Exception as e:
            return False, f"腾讯云 DNS 连接异常: {str(e)}"

    @classmethod
    def create_srv_record(
        cls,
        secret_id: str,
        secret_key: str,
        root_domain: str,
        subdomain_prefix: str,
        target_host: str,
        voice_port: int,
        priority: int = 0,
        weight: int = 5
    ) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
        sub_p = clean_subdomain_prefix(subdomain_prefix)
        root_d = root_domain.strip().lower().rstrip(".")
        full_subdomain = f"{sub_p}.{root_d}"
        
        # 腾讯云 subDomain 格式: _ts3._udp.<subdomain>
        sub_domain = f"_ts3._udp.{sub_p}"
        # value: <weight> <port> <target> （注：腾讯云 priority 由独立字段 MX 传递或直接包含在 value）
        value = f"{weight} {int(voice_port)} {target_host.strip().rstrip('.')}"

        try:
            res = cls._request_tc3(
                secret_id,
                secret_key,
                "CreateRecord",
                {
                    "Domain": root_d,
                    "SubDomain": sub_domain,
                    "RecordType": "SRV",
                    "RecordLine": "默认",
                    "Value": value,
                    "MX": priority
                }
            )
            resp_data = res.get("Response", {})
            if "Error" in resp_data:
                return False, None, full_subdomain, f"腾讯云创建失败: {resp_data['Error'].get('Message')}"
            record_id = resp_data.get("RecordId")
            if record_id:
                return True, str(record_id), full_subdomain, None
            return False, None, full_subdomain, "腾讯云未返回有效 RecordId"
        except Exception as e:
            return False, None, full_subdomain, f"腾讯云创建异常: {str(e)}"

    @classmethod
    def delete_record(cls, secret_id: str, secret_key: str, root_domain: str, record_id: str) -> Tuple[bool, Optional[str]]:
        if not record_id:
            return True, None
        try:
            res = cls._request_tc3(
                secret_id,
                secret_key,
                "DeleteRecord",
                {
                    "Domain": root_domain.strip(),
                    "RecordId": int(record_id.strip())
                }
            )
            resp_data = res.get("Response", {})
            if "Error" in resp_data:
                return False, f"腾讯云删除失败: {resp_data['Error'].get('Message')}"
            return True, None
        except Exception as e:
            return False, f"腾讯云删除异常: {str(e)}"


# --- 统一调度管理器 (Unified DNS Service Manager) ---

class DnsService:
    validate_subdomain_format = staticmethod(validate_subdomain_format)
    clean_subdomain_prefix = staticmethod(clean_subdomain_prefix)

    @staticmethod
    def test_connection(cfg: Dict[str, Any]) -> Tuple[bool, str]:
        provider = (cfg.get("dns_provider") or "disabled").lower()
        if provider == "disabled":
            return True, "当前未启用 DNS 自动化绑定服务"
        
        root_domain = (cfg.get("dns_root_domain") or "").strip()
        if not root_domain:
            return False, "主域名 (Root Domain) 不能为空"

        if provider == "cloudflare":
            token = (cfg.get("dns_cf_token") or "").strip()
            zone_id = (cfg.get("dns_cf_zone_id") or "").strip()
            return CloudflareDnsProvider.test_connection(token, zone_id)

        elif provider == "aliyun":
            ak = (cfg.get("dns_aliyun_ak") or "").strip()
            sk = (cfg.get("dns_aliyun_sk") or "").strip()
            return AliyunDnsProvider.test_connection(ak, sk, root_domain)

        elif provider == "tencent":
            sid = (cfg.get("dns_tencent_id") or "").strip()
            skey = (cfg.get("dns_tencent_key") or "").strip()
            return TencentDnsProvider.test_connection(sid, skey, root_domain)

        return False, f"不支持的 DNS 服务商类型: {provider}"

    @staticmethod
    def create_ts_srv_record(
        subdomain_prefix: str,
        target_host: str,
        voice_port: int,
        dns_cfg: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
        """
        根据当前系统配置，自动向 DNS 服务商创建 SRV 记录
        返回: (success, record_id, full_domain, error_message)
        """
        # 校验前缀合法性
        valid, msg = validate_subdomain_format(subdomain_prefix)
        if not valid:
            return False, None, None, msg

        if dns_cfg is None:
            from database import get_dns_config
            dns_cfg = get_dns_config()

        if not dns_cfg.get("dns_enabled", False):
            # 未开启 DNS 服务，返回 None 记录
            return False, None, None, "系统未启用 DNS 自动化绑定功能"

        provider = (dns_cfg.get("dns_provider") or "disabled").lower()
        root_domain = (dns_cfg.get("dns_root_domain") or "").strip()
        configured_target = (dns_cfg.get("dns_target_host") or "").strip()
        final_target = configured_target or target_host

        if not root_domain:
            return False, None, None, "系统未配置主域名"

        if provider == "cloudflare":
            token = (dns_cfg.get("dns_cf_token") or "").strip()
            zone_id = (dns_cfg.get("dns_cf_zone_id") or "").strip()
            return CloudflareDnsProvider.create_srv_record(
                token, zone_id, root_domain, subdomain_prefix, final_target, voice_port
            )

        elif provider == "aliyun":
            ak = (dns_cfg.get("dns_aliyun_ak") or "").strip()
            sk = (dns_cfg.get("dns_aliyun_sk") or "").strip()
            return AliyunDnsProvider.create_srv_record(
                ak, sk, root_domain, subdomain_prefix, final_target, voice_port
            )

        elif provider == "tencent":
            sid = (dns_cfg.get("dns_tencent_id") or "").strip()
            skey = (dns_cfg.get("dns_tencent_key") or "").strip()
            return TencentDnsProvider.create_srv_record(
                sid, skey, root_domain, subdomain_prefix, final_target, voice_port
            )

        return False, None, None, f"未知的 DNS 服务商: {provider}"

    @staticmethod
    def delete_ts_srv_record(
        record_id: str,
        subdomain_prefix: Optional[str] = None,
        dns_cfg: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        删除指定的 DNS 记录
        """
        if not record_id:
            return True, None

        if dns_cfg is None:
            from database import get_dns_config
            dns_cfg = get_dns_config()

        provider = (dns_cfg.get("dns_provider") or "disabled").lower()
        root_domain = (dns_cfg.get("dns_root_domain") or "").strip()

        if provider == "cloudflare":
            token = (dns_cfg.get("dns_cf_token") or "").strip()
            zone_id = (dns_cfg.get("dns_cf_zone_id") or "").strip()
            return CloudflareDnsProvider.delete_record(token, zone_id, record_id)

        elif provider == "aliyun":
            ak = (dns_cfg.get("dns_aliyun_ak") or "").strip()
            sk = (dns_cfg.get("dns_aliyun_sk") or "").strip()
            return AliyunDnsProvider.delete_record(ak, sk, record_id)

        elif provider == "tencent":
            sid = (dns_cfg.get("dns_tencent_id") or "").strip()
            skey = (dns_cfg.get("dns_tencent_key") or "").strip()
            return TencentDnsProvider.delete_record(sid, skey, root_domain, record_id)

        return True, None


dns_service = DnsService()
