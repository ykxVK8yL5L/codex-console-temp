"""
API 路由模块
"""

from fastapi import APIRouter

from .accounts import router as accounts_router
from .registration import router as registration_router
from .settings import router as settings_router
from .email import router as email_services_router
from .logs import router as logs_router
from .upload.codex2api_services import router as codex2api_services_router
from .upload.cpa_services import router as cpa_services_router
from .upload.new_api_services import router as new_api_services_router
from .upload.sub2api_services import router as sub2api_services_router
from .tasks import router as tasks_router

api_router = APIRouter()

# 注册各模块路由
api_router.include_router(accounts_router, prefix="/accounts", tags=["accounts"])
api_router.include_router(registration_router, prefix="/registration", tags=["registration"])
api_router.include_router(settings_router, prefix="/settings", tags=["settings"])
api_router.include_router(email_services_router, prefix="/email-services", tags=["email-services"])
api_router.include_router(logs_router, prefix="/logs", tags=["logs"])
api_router.include_router(codex2api_services_router, prefix="/codex2api-services", tags=["codex2api-services"])
api_router.include_router(cpa_services_router, prefix="/cpa-services", tags=["cpa-services"])
api_router.include_router(new_api_services_router, prefix="/new-api-services", tags=["new-api-services"])
api_router.include_router(sub2api_services_router, prefix="/sub2api-services", tags=["sub2api-services"])
api_router.include_router(tasks_router, prefix="/tasks", tags=["tasks"])
