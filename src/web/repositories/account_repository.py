"""
账号仓储层：封装常见查询与聚合。
"""

from __future__ import annotations

from typing import Iterator

from ...database.models import Account


def iter_query_in_batches(query, *, batch_size: int = 200) -> Iterator[Account]:
    """
    分批迭代 ORM Query，避免一次性 all() 全量加载。
    """
    safe_batch = max(50, min(1000, int(batch_size or 200)))
    offset = 0
    while True:
        rows = query.offset(offset).limit(safe_batch).all()
        if not rows:
            break
        for row in rows:
            yield row
        if len(rows) < safe_batch:
            break
        offset += safe_batch
