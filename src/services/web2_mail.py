"""
Web2 临时邮箱服务实现
基于 https://web2.temp-mail.org 接口
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..config.constants import OTP_CODE_PATTERN
from ..core.http_client import HTTPClient, RequestConfig


logger = logging.getLogger(__name__)


class Web2MailService(BaseEmailService):
    """web2.temp-mail.org 临时邮箱服务"""

    DEFAULT_BASE_URL = "https://web2.temp-mail.org"
    DEFAULT_PROXY_URL = "http://127.0.0.1:7890"
    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin": "https://web2.temp-mail.org",
        "Referer": "https://web2.temp-mail.org",
        "Accept": "application/json, text/plain, */*",
    }

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.WEB2, name)

        default_config = {
            "base_url": self.DEFAULT_BASE_URL,
            "timeout": 30,
            "max_retries": 3,
        }
        self.config = {**default_config, **(config or {})}
        self.config["base_url"] = str(self.config.get("base_url") or self.DEFAULT_BASE_URL).rstrip("/")

        http_config = RequestConfig(
            timeout=int(self.config.get("timeout") or 30),
            max_retries=int(self.config.get("max_retries") or 3),
        )
        self.http_client = HTTPClient(
            proxy_url=self.DEFAULT_PROXY_URL,
            config=http_config,
        )
        self._mailboxes_by_email: Dict[str, Dict[str, Any]] = {}
        self._mailboxes_by_token: Dict[str, Dict[str, Any]] = {}
        self._seen_message_ids: Dict[str, set[str]] = {}

    def _build_headers(self, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = dict(self.DEFAULT_HEADERS)
        if extra_headers:
            headers.update({key: value for key, value in extra_headers.items() if value is not None})
        return headers

    def _cache_mailbox(self, mailbox: Dict[str, Any]) -> None:
        email = str(mailbox.get("email") or mailbox.get("address") or "").strip().lower()
        token = str(mailbox.get("token") or mailbox.get("service_id") or "").strip()
        if email:
            self._mailboxes_by_email[email] = mailbox
        if token:
            self._mailboxes_by_token[token] = mailbox

    def _pick_first_value(self, payload: Dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    def _normalize_mailbox_payload(self, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, str):
            email = payload.strip()
            return {
                "email": email,
                "address": email,
                "token": "",
                "service_id": email,
                "raw_payload": payload,
            }

        if isinstance(payload, list):
            if not payload:
                return {}
            return self._normalize_mailbox_payload(payload[0])

        if not isinstance(payload, dict):
            return {}

        nested_payload = payload.get("data")
        if isinstance(nested_payload, (dict, list, str)):
            normalized_nested = self._normalize_mailbox_payload(nested_payload)
            if normalized_nested:
                merged = dict(payload)
                merged.update({k: v for k, v in normalized_nested.items() if v not in (None, "", [])})
                merged.setdefault("raw_payload", payload)
                return merged

        email = self._pick_first_value(
            payload,
            "email",
            "address",
            "mailbox",
            "mail",
            "addr",
            "email_address",
            "emailAddress",
        )
        token = self._pick_first_value(
            payload,
            "token",
            "mailbox_token",
            "mailboxToken",
            "access_token",
            "accessToken",
            "jwt",
            "id",
        )
        service_id = self._pick_first_value(
            payload,
            "service_id",
            "serviceId",
            "id",
            "mailbox_id",
            "mailboxId",
        ) or token or email

        normalized = dict(payload)
        normalized.update({
            "email": email,
            "address": email or self._pick_first_value(payload, "address", "mailbox", "mail"),
            "token": token,
            "service_id": service_id,
            "raw_payload": payload,
        })
        return normalized

    def _resolve_mailbox(self, email: Optional[str], email_id: Optional[str]) -> Optional[Dict[str, Any]]:
        token = str(email_id or "").strip()
        if token and token in self._mailboxes_by_token:
            return self._mailboxes_by_token[token]

        email_norm = str(email or "").strip().lower()
        if email_norm and email_norm in self._mailboxes_by_email:
            return self._mailboxes_by_email[email_norm]
        return None

    def _extract_message_id(self, message: Dict[str, Any]) -> str:
        for key in ("id", "_id", "messageId", "message_id", "createdAt", "created_at", "subject"):
            value = message.get(key)
            if value not in (None, ""):
                return str(value)
        return str(hash(str(message)))

    def _extract_code(self, message: Dict[str, Any], pattern: str) -> Optional[str]:
        subject = str(message.get("subject") or "").strip()
        if subject:
            match = re.search(pattern, subject)
            if match:
                return match.group(1)
            fallback = subject.split(" ")[-1].strip()
            if re.fullmatch(r"\d{4,8}", fallback):
                return fallback

        blob = "\n".join(
            str(message.get(key) or "")
            for key in ("subject", "text", "body", "html", "from")
        )
        match = re.search(pattern, blob)
        if match:
            return match.group(1)
        return None

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        try:
            response = self.http_client.post(
                f"{self.config['base_url']}/mailbox",
                headers=self._build_headers(),
                json={},
            )
            if response.status_code != 200:
                raise EmailServiceError(f"web2 创建邮箱失败，状态码: {response.status_code}")

            data = response.json()
            mailbox = self._normalize_mailbox_payload(data)

            email = str(mailbox.get("email") or mailbox.get("address") or "").strip()
            token = str(mailbox.get("token") or "").strip()
            service_id = str(mailbox.get("service_id") or token or email).strip()
            if not email:
                raise EmailServiceError(f"web2 返回数据不完整: {data}")

            mailbox.update({
                "email": email,
                "address": email,
                "token": token,
                "service_id": service_id,
                "created_at": time.time(),
            })
            self._cache_mailbox(mailbox)
            self.update_status(True)
            logger.info("Web2 邮箱创建成功: %s", email)
            return mailbox
        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"创建 Web2 邮箱失败: {e}")

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        mailbox = self._resolve_mailbox(email, email_id)
        if not mailbox:
            logger.warning("Web2 未找到邮箱缓存: %s", email)
            return None

        token = str(mailbox.get("token") or "").strip()
        if not token:
            logger.warning("Web2 邮箱缺少 token，尝试使用 service_id 兜底: %s", email)
            token = str(mailbox.get("service_id") or "").strip()
        if not token:
            logger.warning("Web2 邮箱缺少 token: %s", email)
            return None

        email_norm = str(email or mailbox.get("email") or "").strip().lower()
        seen_ids = self._seen_message_ids.setdefault(email_norm, set())
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                response = self.http_client.get(
                    f"{self.config['base_url']}/messages",
                    headers=self._build_headers({
                        "Cache-Control": "no-cache",
                        "Authorization": f"Bearer {token}",
                    }),
                )
                if response.status_code != 200:
                    time.sleep(3)
                    continue

                payload = response.json()
                if isinstance(payload, dict):
                    messages = payload.get("messages")
                    if messages is None:
                        messages = payload.get("data") or payload.get("items") or payload.get("results") or []
                elif isinstance(payload, list):
                    messages = payload
                else:
                    messages = []
                if not isinstance(messages, list):
                    time.sleep(3)
                    continue

                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    message_id = self._extract_message_id(message)
                    if message_id in seen_ids:
                        continue
                    seen_ids.add(message_id)

                    code = self._extract_code(message, pattern)
                    if code:
                        self.update_status(True)
                        logger.info("Web2 找到验证码: %s", code)
                        return code
            except Exception as e:
                logger.debug("Web2 轮询邮件失败: %s", e)

            time.sleep(3)

        logger.warning("Web2 等待验证码超时: %s", email)
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        return list(self._mailboxes_by_email.values())

    def delete_email(self, email_id: str) -> bool:
        token = str(email_id or "").strip()
        if not token:
            return False
        mailbox = self._mailboxes_by_token.pop(token, None)
        if not mailbox:
            return False
        email = str(mailbox.get("email") or "").strip().lower()
        if email:
            self._mailboxes_by_email.pop(email, None)
            self._seen_message_ids.pop(email, None)
        return True

    def check_health(self) -> bool:
        try:
            response = self.http_client.post(
                f"{self.config['base_url']}/mailbox",
                headers=self._build_headers(),
                json={},
                timeout=10,
            )
            healthy = response.status_code == 200
            self.update_status(healthy, None if healthy else EmailServiceError(f"status={response.status_code}"))
            return healthy
        except Exception as e:
            logger.warning("Web2 健康检查失败: %s", e)
            self.update_status(False, e)
            return False
