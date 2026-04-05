"""
账号业务服务层：对路由层暴露稳定的查询/聚合接口。
"""

from __future__ import annotations

from ..repositories.account_repository import iter_query_in_batches


def stream_accounts(query, *, batch_size: int = 200):
    return iter_query_in_batches(query, batch_size=batch_size)
