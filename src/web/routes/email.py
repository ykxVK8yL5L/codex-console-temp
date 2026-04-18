"""
邮箱服务配置 API 路由
"""

import json
import logging
import urllib.parse
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from curl_cffi import requests as cffi_requests

from ...database import crud
from ...database.session import get_db
from ...database.models import EmailService as EmailServiceModel
from ...database.models import Account as AccountModel
from ...database.models import RegistrationTask as RegistrationTaskModel
from ...config.settings import get_settings
from ...services import EmailServiceFactory, EmailServiceType
from ..task_manager import task_manager

logger = logging.getLogger(__name__)
router = APIRouter()


# ============== Pydantic Models ==============


class EmailServiceCreate(BaseModel):
    """创建邮箱服务请求"""

    service_type: str
    name: str
    config: Dict[str, Any]
    enabled: bool = True
    priority: int = 0


class EmailServiceUpdate(BaseModel):
    """更新邮箱服务请求"""

    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


class EmailServiceResponse(BaseModel):
    """??????"""

    id: int
    service_type: str
    name: str
    enabled: bool
    priority: int
    config: Optional[Dict[str, Any]] = None  # ??????????
    registration_status: Optional[str] = None
    registered_account_id: Optional[int] = None
    last_used: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class EmailServiceListResponse(BaseModel):
    """邮箱服务列表响应"""

    total: int
    services: List[EmailServiceResponse]


class ServiceTestResult(BaseModel):
    """服务测试结果"""

    success: bool
    message: str
    details: Optional[Dict[str, Any]] = None


class OutlookBatchImportRequest(BaseModel):
    """Outlook 批量导入请求"""

    data: str  # 多行数据，每行格式: 邮箱----密码 或 邮箱----密码----client_id----refresh_token
    enabled: bool = True
    priority: int = 0


class OutlookBatchImportResponse(BaseModel):
    """Outlook 批量导入响应"""

    total: int
    success: int
    failed: int
    accounts: List[Dict[str, Any]]
    errors: List[str]


class OutlookOAuthCallbackRequest(BaseModel):
    """Outlook OAuth 回调换取 refresh token 请求"""

    callback_url: str
    client_id: str
    redirect_uri: str = "http://localhost:8080"


class OutlookOAuthCallbackResponse(BaseModel):
    """Outlook OAuth 回调换取 refresh token 响应"""

    refresh_token: str
    access_token: Optional[str] = None
    expires_in: Optional[int] = None
    scope: Optional[str] = None


# ============== Helper Functions ==============

# 敏感字段列表，返回响应时需要过滤
SENSITIVE_FIELDS = {
    "password",
    "api_key",
    "refresh_token",
    "access_token",
    "admin_token",
    "admin_password",
    "custom_auth",
}


def normalize_email_service_config(
    service_type: str, config: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """兼容历史配置字段，避免不同入口写入的键名不一致。"""
    normalized = dict(config or {})

    if service_type in {"temp_mail", "cloudmail", "freemail"}:
        if normalized.get("default_domain") and not normalized.get("domain"):
            normalized["domain"] = normalized.pop("default_domain")

    if service_type == "web2":
        if not normalized.get("base_url"):
            normalized["base_url"] = "https://web2.temp-mail.org"

    if service_type == "outlook":
        email = str(normalized.get("email") or "").strip().lower()
        password = str(normalized.get("password") or "")
        client_id = str(normalized.get("client_id") or "").strip()
        refresh_token = str(normalized.get("refresh_token") or "").strip()

        normalized["email"] = email
        normalized["password"] = password
        normalized["client_id"] = client_id
        normalized["refresh_token"] = refresh_token

    return normalized


def filter_sensitive_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """过滤敏感配置信息"""
    if not config:
        return {}

    filtered = {}
    for key, value in config.items():
        if key in SENSITIVE_FIELDS:
            # 敏感字段不返回，但标记是否存在
            filtered[f"has_{key}"] = bool(value)
        else:
            filtered[key] = value

    # 为 Outlook 计算是否有 OAuth
    if config.get("client_id") and config.get("refresh_token"):
        filtered["has_oauth"] = True

    return filtered


def parse_outlook_import_line(line: str) -> Dict[str, Any]:
    """解析 Outlook / Hotmail / Live 批量导入行。"""
    normalized_line = str(line or "").strip().lstrip("\ufeff")
    parts = [part.strip() for part in normalized_line.split("----")]

    if len(parts) < 2:
        raise ValueError("格式错误，至少需要邮箱和密码")

    email = str(parts[0] or "").strip().lower()
    password = str(parts[1] or "").strip()

    if not email or "@" not in email:
        raise ValueError(f"无效的邮箱地址: {parts[0].strip()}")

    if not password:
        raise ValueError("密码不能为空")

    config = {
        "email": email,
        "password": password,
    }

    if len(parts) >= 4:
        client_id = str(parts[2] or "").strip()
        refresh_token = str(parts[3] or "").strip()
        if client_id and refresh_token:
            config["client_id"] = client_id
            config["refresh_token"] = refresh_token

    return config


def service_to_response(service: EmailServiceModel) -> EmailServiceResponse:
    """?????????"""
    normalized_config = normalize_email_service_config(
        service.service_type, service.config
    )
    registration_status = None
    registered_account_id = None
    if service.service_type == "outlook":
        email = str(normalized_config.get("email") or service.name or "").strip()
        normalized_email = email.lower()
        if email:
            with get_db() as db:
                account = (
                    db.query(AccountModel)
                    .filter(func.lower(AccountModel.email) == normalized_email)
                    .first()
                )
            if account:
                registration_status = "registered"
                registered_account_id = account.id
            else:
                registration_status = "unregistered"

    return EmailServiceResponse(
        id=service.id,
        service_type=service.service_type,
        name=service.name,
        enabled=service.enabled,
        priority=service.priority,
        config=filter_sensitive_config(normalized_config),
        registration_status=registration_status,
        registered_account_id=registered_account_id,
        last_used=service.last_used.isoformat() if service.last_used else None,
        created_at=service.created_at.isoformat() if service.created_at else None,
        updated_at=service.updated_at.isoformat() if service.updated_at else None,
    )


# ============== API Endpoints ==============


@router.get("/stats")
async def get_email_services_stats():
    """获取邮箱服务统计信息"""
    with get_db() as db:
        # 按类型统计
        type_stats = (
            db.query(EmailServiceModel.service_type, func.count(EmailServiceModel.id))
            .group_by(EmailServiceModel.service_type)
            .all()
        )

        # 启用数量
        enabled_count = (
            db.query(func.count(EmailServiceModel.id))
            .filter(EmailServiceModel.enabled == True)
            .scalar()
        )

        settings = get_settings()
        tempmail_enabled = bool(settings.tempmail_enabled)
        yyds_enabled = bool(
            settings.yyds_mail_enabled
            and settings.yyds_mail_api_key
            and settings.yyds_mail_api_key.get_secret_value()
        )

        stats = {
            "outlook_count": 0,
            "custom_count": 0,
            "tempmail_builtin_count": 0,
            "yyds_mail_count": 0,
            "web2_count": 0,
            "temp_mail_count": 0,
            "duck_mail_count": 0,
            "freemail_count": 0,
            "imap_mail_count": 0,
            "cloudmail_count": 0,
            "luckmail_count": 0,
            "tempmail_available": tempmail_enabled or yyds_enabled,
            "yyds_mail_available": yyds_enabled,
            "enabled_count": enabled_count,
        }

        for service_type, count in type_stats:
            if service_type == "outlook":
                stats["outlook_count"] = count
            elif service_type == "moe_mail":
                stats["custom_count"] = count
            elif service_type == "tempmail":
                stats["tempmail_builtin_count"] = count
            elif service_type == "yyds_mail":
                stats["yyds_mail_count"] = count
            elif service_type == "web2":
                stats["web2_count"] = count
            elif service_type == "temp_mail":
                stats["temp_mail_count"] = count
            elif service_type == "duck_mail":
                stats["duck_mail_count"] = count
            elif service_type == "freemail":
                stats["freemail_count"] = count
            elif service_type == "imap_mail":
                stats["imap_mail_count"] = count
            elif service_type == "cloudmail":
                stats["cloudmail_count"] = count
            elif service_type == "luckmail":
                stats["luckmail_count"] = count

        return stats


@router.get("/types")
async def get_service_types():
    """获取支持的邮箱服务类型"""
    return {
        "types": [
            {
                "value": "tempmail",
                "label": "Tempmail.lol",
                "description": "官方内置临时邮箱渠道，通过全局配置使用",
                "config_fields": [
                    {
                        "name": "base_url",
                        "label": "API 地址",
                        "default": "https://api.tempmail.lol/v2",
                        "required": False,
                    },
                    {
                        "name": "timeout",
                        "label": "超时时间",
                        "default": 30,
                        "required": False,
                    },
                ],
            },
            {
                "value": "yyds_mail",
                "label": "YYDS Mail",
                "description": "官方内置临时邮箱渠道，使用 X-API-Key 创建邮箱并轮询消息",
                "config_fields": [
                    {
                        "name": "base_url",
                        "label": "API 地址",
                        "default": "https://maliapi.215.im/v1",
                        "required": False,
                    },
                    {
                        "name": "api_key",
                        "label": "API Key",
                        "required": True,
                        "secret": True,
                    },
                    {
                        "name": "default_domain",
                        "label": "默认域名",
                        "required": False,
                        "placeholder": "public.example.com",
                    },
                    {
                        "name": "timeout",
                        "label": "超时时间",
                        "default": 30,
                        "required": False,
                    },
                ],
            },
            {
                "value": "outlook",
                "label": "Outlook / Hotmail",
                "description": "Microsoft 邮箱（Outlook / Hotmail / Live），需要配置账户信息",
                "config_fields": [
                    {"name": "email", "label": "邮箱地址", "required": True},
                    {"name": "password", "label": "密码", "required": True},
                    {
                        "name": "client_id",
                        "label": "OAuth Client ID",
                        "required": False,
                    },
                    {
                        "name": "refresh_token",
                        "label": "OAuth Refresh Token",
                        "required": False,
                    },
                ],
            },
            {
                "value": "moe_mail",
                "label": "MoeMail",
                "description": "自定义域名邮箱服务",
                "config_fields": [
                    {"name": "base_url", "label": "API 地址", "required": True},
                    {"name": "api_key", "label": "API Key", "required": True},
                    {"name": "default_domain", "label": "默认域名", "required": False},
                ],
            },
            {
                "value": "temp_mail",
                "label": "Temp-Mail（自部署）",
                "description": "自部署 Cloudflare Worker 临时邮箱，admin 模式管理",
                "config_fields": [
                    {
                        "name": "base_url",
                        "label": "Worker 地址",
                        "required": True,
                        "placeholder": "https://mail.example.com",
                    },
                    {
                        "name": "admin_password",
                        "label": "Admin 密码",
                        "required": True,
                        "secret": True,
                    },
                    {
                        "name": "custom_auth",
                        "label": "Custom Auth（可选）",
                        "required": False,
                        "secret": True,
                    },
                    {
                        "name": "domain",
                        "label": "邮箱域名",
                        "required": True,
                        "placeholder": "example.com",
                    },
                    {
                        "name": "enable_prefix",
                        "label": "启用前缀",
                        "required": False,
                        "default": True,
                    },
                ],
            },
            {
                "value": "web2",
                "label": "Web2 Temp Mail",
                "description": "web2.temp-mail.org 临时邮箱，固定使用本地 7890 代理",
                "config_fields": [
                    {
                        "name": "base_url",
                        "label": "API 地址",
                        "required": False,
                        "default": "https://web2.temp-mail.org",
                    },
                    {
                        "name": "timeout",
                        "label": "超时时间",
                        "required": False,
                        "default": 30,
                    },
                    {
                        "name": "max_retries",
                        "label": "最大重试次数",
                        "required": False,
                        "default": 3,
                    },
                ],
            },
            {
                "value": "duck_mail",
                "description": "DuckMail 接口邮箱服务，支持 API Key 私有域名访问",
                "config_fields": [
                    {
                        "name": "base_url",
                        "label": "API 地址",
                        "required": True,
                        "placeholder": "https://api.duckmail.sbs",
                    },
                    {
                        "name": "default_domain",
                        "label": "默认域名",
                        "required": True,
                        "placeholder": "duckmail.sbs",
                    },
                    {
                        "name": "api_key",
                        "label": "API Key",
                        "required": False,
                        "secret": True,
                    },
                    {
                        "name": "password_length",
                        "label": "随机密码长度",
                        "required": False,
                        "default": 12,
                    },
                ],
            },
            {
                "value": "freemail",
                "label": "Freemail",
                "description": "Freemail 自部署 Cloudflare Worker 临时邮箱服务",
                "config_fields": [
                    {
                        "name": "base_url",
                        "label": "API 地址",
                        "required": True,
                        "placeholder": "https://freemail.example.com",
                    },
                    {
                        "name": "admin_token",
                        "label": "Admin Token",
                        "required": True,
                        "secret": True,
                    },
                    {
                        "name": "domain",
                        "label": "邮箱域名",
                        "required": False,
                        "placeholder": "example.com",
                    },
                ],
            },
            {
                "value": "cloudmail",
                "label": "CloudMail",
                "description": "CloudMail 自部署 Cloudflare Worker 邮箱服务，使用管理口令创建邮箱并轮询验证码",
                "config_fields": [
                    {
                        "name": "base_url",
                        "label": "API 地址",
                        "required": True,
                        "placeholder": "https://cloudmail.example.com",
                    },
                    {
                        "name": "admin_password",
                        "label": "Admin 密码",
                        "required": True,
                        "secret": True,
                    },
                    {
                        "name": "domain",
                        "label": "邮箱域名",
                        "required": True,
                        "placeholder": "example.com",
                    },
                    {
                        "name": "enable_prefix",
                        "label": "启用前缀",
                        "required": False,
                        "default": True,
                    },
                    {
                        "name": "timeout",
                        "label": "超时时间",
                        "required": False,
                        "default": 30,
                    },
                ],
            },
            {
                "value": "imap_mail",
                "label": "IMAP 邮箱",
                "description": "标准 IMAP 协议邮箱（Gmail/QQ/163等），仅用于接收验证码，强制直连",
                "config_fields": [
                    {
                        "name": "host",
                        "label": "IMAP 服务器",
                        "required": True,
                        "placeholder": "imap.gmail.com",
                    },
                    {
                        "name": "port",
                        "label": "端口",
                        "required": False,
                        "default": 993,
                    },
                    {
                        "name": "use_ssl",
                        "label": "使用 SSL",
                        "required": False,
                        "default": True,
                    },
                    {"name": "email", "label": "邮箱地址", "required": True},
                    {
                        "name": "password",
                        "label": "密码/授权码",
                        "required": True,
                        "secret": True,
                    },
                ],
            },
            {
                "value": "luckmail",
                "label": "LuckMail",
                "description": "LuckMail 接码服务（下单 + 轮询验证码）",
                "config_fields": [
                    {
                        "name": "base_url",
                        "label": "平台地址",
                        "required": False,
                        "default": "https://mails.luckyous.com/",
                    },
                    {
                        "name": "api_key",
                        "label": "API Key",
                        "required": True,
                        "secret": True,
                    },
                    {
                        "name": "project_code",
                        "label": "项目编码",
                        "required": False,
                        "default": "openai",
                    },
                    {
                        "name": "email_type",
                        "label": "邮箱类型",
                        "required": False,
                        "default": "ms_graph",
                    },
                    {
                        "name": "preferred_domain",
                        "label": "优先域名",
                        "required": False,
                        "placeholder": "outlook.com",
                    },
                    {
                        "name": "poll_interval",
                        "label": "轮询间隔(秒)",
                        "required": False,
                        "default": 3.0,
                    },
                ],
            },
        ]
    }


@router.get("", response_model=EmailServiceListResponse)
async def list_email_services(
    service_type: Optional[str] = Query(None, description="服务类型筛选"),
    enabled_only: bool = Query(False, description="只显示启用的服务"),
):
    """获取邮箱服务列表"""
    with get_db() as db:
        query = db.query(EmailServiceModel)

        if service_type:
            query = query.filter(EmailServiceModel.service_type == service_type)

        if enabled_only:
            query = query.filter(EmailServiceModel.enabled == True)

        services = query.order_by(
            EmailServiceModel.priority.asc(), EmailServiceModel.id.asc()
        ).all()

        return EmailServiceListResponse(
            total=len(services), services=[service_to_response(s) for s in services]
        )


@router.get("/{service_id}", response_model=EmailServiceResponse)
async def get_email_service(service_id: int):
    """获取单个邮箱服务详情"""
    with get_db() as db:
        service = (
            db.query(EmailServiceModel)
            .filter(EmailServiceModel.id == service_id)
            .first()
        )
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")
        return service_to_response(service)


def _build_inbox_config(db, service_type, email: str) -> dict:
    """根据账号邮箱服务类型从数据库构建服务配置（不传 proxy_url）"""
    from ...database.models import EmailService as EmailServiceModel
    from ...services import EmailServiceType as EST

    if service_type == EST.TEMPMAIL:
        settings = get_settings()
        return {
            "base_url": settings.tempmail_base_url,
            "timeout": settings.tempmail_timeout,
            "max_retries": settings.tempmail_max_retries,
        }

    if service_type == EST.YYDS_MAIL:
        settings = get_settings()
        return {
            "base_url": settings.yyds_mail_base_url,
            "api_key": (
                settings.yyds_mail_api_key.get_secret_value()
                if settings.yyds_mail_api_key
                else ""
            ),
            "default_domain": settings.yyds_mail_default_domain,
            "timeout": settings.yyds_mail_timeout,
            "max_retries": settings.yyds_mail_max_retries,
        }

    if service_type == EST.MOE_MAIL:
        # 按域名后缀匹配，找不到则取 priority 最小的
        domain = email.split("@")[1] if "@" in email else ""
        services = (
            db.query(EmailServiceModel)
            .filter(
                EmailServiceModel.service_type == "moe_mail",
                EmailServiceModel.enabled == True,
            )
            .order_by(EmailServiceModel.priority.asc())
            .all()
        )
        svc = None
        for s in services:
            cfg = s.config or {}
            if cfg.get("default_domain") == domain or cfg.get("domain") == domain:
                svc = s
                break
        if not svc and services:
            svc = services[0]
        if not svc:
            return None
        cfg = svc.config.copy()
        if "api_url" in cfg and "base_url" not in cfg:
            cfg["base_url"] = cfg.pop("api_url")
        return cfg

    # 其余服务类型：直接按 service_type 查数据库
    type_map = {
        EST.TEMP_MAIL: "temp_mail",
        EST.DUCK_MAIL: "duck_mail",
        EST.FREEMAIL: "freemail",
        EST.IMAP_MAIL: "imap_mail",
        EST.OUTLOOK: "outlook",
        EST.LUCKMAIL: "luckmail",
    }
    db_type = type_map.get(service_type)
    if not db_type:
        return None

    query = db.query(EmailServiceModel).filter(
        EmailServiceModel.service_type == db_type, EmailServiceModel.enabled == True
    )
    if service_type == EST.OUTLOOK:
        # 按 config.email 匹配账号 email
        services = query.all()
        svc = next(
            (s for s in services if (s.config or {}).get("email") == email), None
        )
    else:
        svc = query.order_by(EmailServiceModel.priority.asc()).first()

    if not svc:
        return None
    cfg = svc.config.copy() if svc.config else {}
    if "api_url" in cfg and "base_url" not in cfg:
        cfg["base_url"] = cfg.pop("api_url")
    return cfg


@router.post("/{service_id}/inbox-code")
async def get_account_inbox_code(service_id: int):
    """查询账号邮箱收件箱最新验证码"""
    from ...services import EmailServiceFactory, EmailServiceType

    with get_db() as db:
        service = (
            db.query(EmailServiceModel)
            .filter(EmailServiceModel.id == service_id)
            .first()
        )
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        try:
            service_type = EmailServiceType(service.service_type)
        except ValueError:
            return {"success": False, "error": "不支持的邮箱服务类型"}

        config = _build_inbox_config(db, service_type, service.config.get("email"))
        if config is None:
            return {"success": False, "error": "未找到可用的邮箱服务配置"}

        try:
            svc = EmailServiceFactory.create(service_type, config)
            code = svc.get_verification_code(config.get("email"), timeout=12)
        except Exception as e:
            return {"success": False, "error": str(e)}

        if not code:
            return {"success": False, "error": "未收到验证码邮件"}

        return {"success": True, "code": code, "email": config.get("email")}


@router.get("/{service_id}/full")
async def get_email_service_full(service_id: int):
    """获取单个邮箱服务完整详情（包含敏感字段，用于编辑）"""
    with get_db() as db:
        service = (
            db.query(EmailServiceModel)
            .filter(EmailServiceModel.id == service_id)
            .first()
        )
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        return {
            "id": service.id,
            "service_type": service.service_type,
            "name": service.name,
            "enabled": service.enabled,
            "priority": service.priority,
            "config": normalize_email_service_config(
                service.service_type, service.config
            ),  # 返回完整配置
            "last_used": service.last_used.isoformat() if service.last_used else None,
            "created_at": (
                service.created_at.isoformat() if service.created_at else None
            ),
            "updated_at": (
                service.updated_at.isoformat() if service.updated_at else None
            ),
        }


@router.post("", response_model=EmailServiceResponse)
async def create_email_service(request: EmailServiceCreate):
    """创建邮箱服务配置"""
    # 验证服务类型
    try:
        EmailServiceType(request.service_type)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"无效的服务类型: {request.service_type}"
        )

    with get_db() as db:
        normalized_config = normalize_email_service_config(
            request.service_type, request.config
        )
        normalized_name = str(request.name or "").strip()
        if request.service_type == "outlook":
            normalized_name = (
                str(normalized_config.get("email") or normalized_name).strip().lower()
            )
            if normalized_name:
                normalized_config["email"] = normalized_name
            if not normalized_config.get("email"):
                raise HTTPException(status_code=400, detail="Outlook 邮箱地址不能为空")
            if not normalized_config.get("password") and not (
                normalized_config.get("client_id")
                and normalized_config.get("refresh_token")
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Outlook 账户至少需要密码，或提供 Client ID + Refresh Token",
                )
            if normalized_config.get("refresh_token") and not normalized_config.get(
                "client_id"
            ):
                raise HTTPException(
                    status_code=400, detail="已提供 Refresh Token，但缺少 Client ID"
                )

        # 检查名称是否重复
        if request.service_type == "outlook":
            existing = (
                db.query(EmailServiceModel)
                .filter(
                    EmailServiceModel.service_type == "outlook",
                    func.lower(EmailServiceModel.name) == normalized_name,
                )
                .first()
            )
        else:
            existing = (
                db.query(EmailServiceModel)
                .filter(EmailServiceModel.name == normalized_name)
                .first()
            )
        if existing:
            raise HTTPException(status_code=400, detail="服务名称已存在")

        service = EmailServiceModel(
            service_type=request.service_type,
            name=normalized_name,
            config=normalized_config,
            enabled=request.enabled,
            priority=request.priority,
        )
        db.add(service)
        db.commit()
        db.refresh(service)

        return service_to_response(service)


@router.patch("/{service_id}", response_model=EmailServiceResponse)
async def update_email_service(service_id: int, request: EmailServiceUpdate):
    """更新邮箱服务配置"""
    with get_db() as db:
        service = (
            db.query(EmailServiceModel)
            .filter(EmailServiceModel.id == service_id)
            .first()
        )
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        update_data = {}
        if request.name is not None:
            update_data["name"] = request.name
        if request.config is not None:
            # 合并配置而不是替换
            current_config = normalize_email_service_config(
                service.service_type, service.config
            )
            merged_config = {**current_config, **request.config}
            if service.service_type == "outlook":
                update_data["config"] = normalize_email_service_config(
                    service.service_type, merged_config
                )
            else:
                # 移除空值
                merged_config = {k: v for k, v in merged_config.items() if v}
                update_data["config"] = normalize_email_service_config(
                    service.service_type, merged_config
                )
        if request.enabled is not None:
            update_data["enabled"] = request.enabled
        if request.priority is not None:
            update_data["priority"] = request.priority

        if service.service_type == "outlook":
            final_name = (
                str(update_data.get("name") or service.name or "").strip().lower()
            )
            final_config = update_data.get("config") or normalize_email_service_config(
                service.service_type, service.config
            )
            final_email = str(final_config.get("email") or final_name).strip().lower()
            final_password = str(final_config.get("password") or "")
            final_client_id = str(final_config.get("client_id") or "").strip()
            final_refresh_token = str(final_config.get("refresh_token") or "").strip()

            if not final_email:
                raise HTTPException(status_code=400, detail="Outlook 邮箱地址不能为空")
            if not final_password and not (final_client_id and final_refresh_token):
                raise HTTPException(
                    status_code=400,
                    detail="Outlook 账户至少需要密码，或提供 Client ID + Refresh Token",
                )
            if final_refresh_token and not final_client_id:
                raise HTTPException(
                    status_code=400, detail="已提供 Refresh Token，但缺少 Client ID"
                )

            duplicate_query = db.query(EmailServiceModel).filter(
                EmailServiceModel.service_type == "outlook",
                func.lower(EmailServiceModel.name) == final_email,
                EmailServiceModel.id != service_id,
            )
            if duplicate_query.first():
                raise HTTPException(status_code=400, detail="服务名称已存在")

            final_config["email"] = final_email
            update_data["config"] = normalize_email_service_config(
                service.service_type, final_config
            )
            update_data["name"] = final_email

        for key, value in update_data.items():
            setattr(service, key, value)

        db.commit()
        db.refresh(service)

        return service_to_response(service)


def _get_email_service_delete_impact(db, service_id: int) -> Dict[str, int]:
    """获取删除邮箱服务前的关联注册任务影响统计"""
    total_reference_count = (
        db.query(func.count(RegistrationTaskModel.id))
        .filter(RegistrationTaskModel.email_service_id == service_id)
        .scalar()
        or 0
    )
    running_reference_count = (
        db.query(func.count(RegistrationTaskModel.id))
        .filter(
            RegistrationTaskModel.email_service_id == service_id,
            RegistrationTaskModel.status == "running",
        )
        .scalar()
        or 0
    )
    pending_reference_count = (
        db.query(func.count(RegistrationTaskModel.id))
        .filter(
            RegistrationTaskModel.email_service_id == service_id,
            RegistrationTaskModel.status == "pending",
        )
        .scalar()
        or 0
    )
    return {
        "total_reference_count": total_reference_count,
        "active_reference_count": running_reference_count,
        "running_reference_count": running_reference_count,
        "pending_reference_count": pending_reference_count,
        "deletable_task_count": max(total_reference_count - running_reference_count, 0),
    }


@router.get("/{service_id}/delete-impact")
async def get_email_service_delete_impact(service_id: int):
    """获取删除邮箱服务前的影响统计"""
    with get_db() as db:
        service = (
            db.query(EmailServiceModel)
            .filter(EmailServiceModel.id == service_id)
            .first()
        )
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        impact = _get_email_service_delete_impact(db, service_id)
        impact["service_id"] = service.id
        impact["service_name"] = service.name
        return impact


@router.delete("/{service_id}")
async def delete_email_service(service_id: int):
    """删除邮箱服务配置"""
    with get_db() as db:
        service = (
            db.query(EmailServiceModel)
            .filter(EmailServiceModel.id == service_id)
            .first()
        )
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")
        impact = _get_email_service_delete_impact(db, service_id)
        running_reference_count = impact["running_reference_count"]
        if running_reference_count > 0:
            raise HTTPException(
                status_code=409,
                detail=f"服务 {service.name} 仍被 {running_reference_count} 个执行中注册任务引用，无法删除。请先取消或删除相关任务后重试。",
            )

        pending_task_uuids = [
            task_uuid
            for (task_uuid,) in db.query(RegistrationTaskModel.task_uuid)
            .filter(
                RegistrationTaskModel.email_service_id == service_id,
                RegistrationTaskModel.status == "pending",
            )
            .all()
        ]
        for task_uuid in pending_task_uuids:
            task_manager.cancel_task(task_uuid)

        deleted_task_count = (
            db.query(RegistrationTaskModel)
            .filter(RegistrationTaskModel.email_service_id == service_id)
            .delete(synchronize_session=False)
        )

        try:
            db.delete(service)
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="该邮箱服务仍被其他数据引用，暂时不能删除。请先解除引用后重试。",
            ) from exc

        message = f"服务 {service.name} 已删除"
        if deleted_task_count > 0:
            message = (
                f"已删除 {deleted_task_count} 条关联注册记录，并删除服务 {service.name}"
            )

        return {"success": True, "message": message}


@router.post("/{service_id}/test", response_model=ServiceTestResult)
async def test_email_service(service_id: int):
    """测试邮箱服务是否可用"""
    with get_db() as db:
        service = (
            db.query(EmailServiceModel)
            .filter(EmailServiceModel.id == service_id)
            .first()
        )
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        try:
            service_type = EmailServiceType(service.service_type)
            email_service = EmailServiceFactory.create(
                service_type,
                normalize_email_service_config(service.service_type, service.config),
                name=service.name,
            )

            health = email_service.check_health()

            if health:
                return ServiceTestResult(
                    success=True,
                    message="服务连接正常",
                    details=(
                        email_service.get_service_info()
                        if hasattr(email_service, "get_service_info")
                        else None
                    ),
                )
            else:
                return ServiceTestResult(success=False, message="服务连接失败")

        except Exception as e:
            logger.error(f"测试邮箱服务失败: {e}")
            return ServiceTestResult(success=False, message=f"测试失败: {str(e)}")


@router.post("/{service_id}/enable")
async def enable_email_service(service_id: int):
    """启用邮箱服务"""
    with get_db() as db:
        service = (
            db.query(EmailServiceModel)
            .filter(EmailServiceModel.id == service_id)
            .first()
        )
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        service.enabled = True
        db.commit()

        return {"success": True, "message": f"服务 {service.name} 已启用"}


@router.post("/{service_id}/disable")
async def disable_email_service(service_id: int):
    """禁用邮箱服务"""
    with get_db() as db:
        service = (
            db.query(EmailServiceModel)
            .filter(EmailServiceModel.id == service_id)
            .first()
        )
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        service.enabled = False
        db.commit()

        return {"success": True, "message": f"服务 {service.name} 已禁用"}


@router.post("/reorder")
async def reorder_services(service_ids: List[int]):
    """重新排序邮箱服务优先级"""
    with get_db() as db:
        for index, service_id in enumerate(service_ids):
            service = (
                db.query(EmailServiceModel)
                .filter(EmailServiceModel.id == service_id)
                .first()
            )
            if service:
                service.priority = index

        db.commit()

        return {"success": True, "message": "优先级已更新"}


@router.post("/outlook/batch-import", response_model=OutlookBatchImportResponse)
async def batch_import_outlook(request: OutlookBatchImportRequest):
    """
    批量导入 Outlook / Hotmail / Live 邮箱账户。

    支持两种格式：
    - 格式一（密码认证）：邮箱----密码
    - 格式二（XOAUTH2 认证）：邮箱----密码----client_id----refresh_token

    每行一个账户，使用四个连字符（----）分隔字段。
    """
    lines = request.data.splitlines()
    valid_lines = [
        line
        for line in lines
        if str(line or "").strip() and not str(line or "").strip().startswith("#")
    ]
    total = len(valid_lines)
    success = 0
    failed = 0
    accounts = []
    errors = []

    with get_db() as db:
        for i, raw_line in enumerate(lines):
            line = str(raw_line or "").strip()

            # 跳过空行和注释
            if not line or line.startswith("#"):
                continue

            try:
                config = parse_outlook_import_line(line)
            except ValueError as exc:
                failed += 1
                errors.append(f"行 {i + 1}: {exc}")
                continue

            email = config["email"]

            existing = (
                db.query(EmailServiceModel)
                .filter(
                    EmailServiceModel.service_type == "outlook",
                    func.lower(EmailServiceModel.name) == email,
                )
                .first()
            )

            if existing:
                failed += 1
                errors.append(f"行 {i + 1}: 邮箱已存在: {email}")
                continue

            try:
                service = EmailServiceModel(
                    service_type="outlook",
                    name=email,
                    config=config,
                    enabled=request.enabled,
                    priority=request.priority,
                )
                db.add(service)
                db.commit()
                db.refresh(service)

                accounts.append(
                    {
                        "id": service.id,
                        "email": email,
                        "has_oauth": bool(
                            config.get("client_id") and config.get("refresh_token")
                        ),
                        "name": email,
                    }
                )
                success += 1

            except Exception as e:
                failed += 1
                errors.append(f"行 {i + 1}: 创建失败: {str(e)}")
                db.rollback()

    return OutlookBatchImportResponse(
        total=total, success=success, failed=failed, accounts=accounts, errors=errors
    )


@router.delete("/outlook/batch")
async def batch_delete_outlook(service_ids: List[int]):
    """批量删除 Outlook 邮箱服务"""
    deleted = 0
    with get_db() as db:
        for service_id in service_ids:
            service = (
                db.query(EmailServiceModel)
                .filter(
                    EmailServiceModel.id == service_id,
                    EmailServiceModel.service_type == "outlook",
                )
                .first()
            )
            if service:
                db.delete(service)
                deleted += 1
        db.commit()

    return {"success": True, "deleted": deleted, "message": f"已删除 {deleted} 个服务"}


@router.post("/outlook/exchange-callback", response_model=OutlookOAuthCallbackResponse)
async def exchange_outlook_callback(request: OutlookOAuthCallbackRequest):
    """提交 Outlook OAuth 回调 URL，换取 refresh token。"""
    callback_url = str(request.callback_url or "").strip()
    client_id = str(request.client_id or "").strip()
    redirect_uri = str(request.redirect_uri or "http://localhost:8080").strip()

    if not callback_url:
        raise HTTPException(status_code=400, detail="回调 URL 不能为空")
    if not client_id:
        raise HTTPException(status_code=400, detail="Client ID 不能为空")

    parsed = urllib.parse.urlparse(callback_url)
    query = urllib.parse.parse_qs(parsed.query or "")
    error = str((query.get("error") or [""])[0] or "").strip()
    if error:
        error_description = str(
            (query.get("error_description") or [""])[0] or ""
        ).strip()
        raise HTTPException(
            status_code=400,
            detail=f"OAuth 授权失败: {error}{f' - {error_description}' if error_description else ''}",
        )

    code = str((query.get("code") or [""])[0] or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="回调 URL 中缺少 code 参数")

    token_endpoint = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    token_payload = {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }

    try:
        response = cffi_requests.post(
            token_endpoint,
            data=token_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
            impersonate="chrome",
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.error("Outlook OAuth 回调换 token 失败: %s", exc)
        raise HTTPException(status_code=400, detail=f"换取 Token 失败: {exc}") from exc

    refresh_token = str(data.get("refresh_token") or "").strip()
    if not refresh_token:
        raise HTTPException(status_code=400, detail="响应中未返回 refresh_token")

    return OutlookOAuthCallbackResponse(
        refresh_token=refresh_token,
        access_token=str(data.get("access_token") or "").strip() or None,
        expires_in=int(data.get("expires_in") or 0) or None,
        scope=str(data.get("scope") or "").strip() or None,
    )


# ============== 临时邮箱测试 ==============


class TempmailTestRequest(BaseModel):
    """临时邮箱测试请求"""

    provider: str = "tempmail"
    api_url: Optional[str] = None
    api_key: Optional[str] = None


@router.post("/test-tempmail")
async def test_tempmail_service(request: TempmailTestRequest):
    """测试临时邮箱服务是否可用"""
    try:
        settings = get_settings()
        provider = str(request.provider or "tempmail").strip().lower()

        if provider == "yyds_mail":
            base_url = request.api_url or settings.yyds_mail_base_url
            api_key = request.api_key
            if api_key is None and settings.yyds_mail_api_key:
                api_key = settings.yyds_mail_api_key.get_secret_value()

            config = {
                "base_url": base_url,
                "api_key": api_key or "",
                "default_domain": settings.yyds_mail_default_domain,
                "timeout": settings.yyds_mail_timeout,
                "max_retries": settings.yyds_mail_max_retries,
            }
            service = EmailServiceFactory.create(EmailServiceType.YYDS_MAIL, config)
            success_message = "YYDS Mail 连接正常"
            fail_message = "YYDS Mail 连接失败"
        else:
            base_url = request.api_url or settings.tempmail_base_url
            config = {
                "base_url": base_url,
                "timeout": settings.tempmail_timeout,
                "max_retries": settings.tempmail_max_retries,
            }
            service = EmailServiceFactory.create(EmailServiceType.TEMPMAIL, config)
            success_message = "临时邮箱连接正常"
            fail_message = "临时邮箱连接失败"

        # 检查服务健康状态
        health = service.check_health()

        if health:
            return {"success": True, "message": success_message}
        else:
            return {"success": False, "message": fail_message}

    except Exception as e:
        logger.error(f"测试临时邮箱失败: {e}")
        return {"success": False, "message": f"测试失败: {str(e)}"}
