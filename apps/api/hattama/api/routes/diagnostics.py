from __future__ import annotations

from typing import Any

import anyio
from fastapi import APIRouter, Depends

from hattama.api.deps import require_role
from hattama.db.models import User

router = APIRouter(prefix="/api/v1", tags=["diagnostics"])


@router.get("/diagnostics")
async def diagnostics(_: User = Depends(require_role("admin", "secretary"))) -> dict[str, Any]:
    from hattama.diagnostics.doctor import collect

    return await anyio.to_thread.run_sync(collect)
