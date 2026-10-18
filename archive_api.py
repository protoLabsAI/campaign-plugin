"""The protoContent archive's operator routes — a router of its own, mounted beside the data
router under ``/api/plugins/campaign`` (behind the host's default-deny operator auth).

* ``GET  /archive`` — archived / queued / retrying, and the active auth (app / token / none).
* ``POST /archive/backfill`` — queue every approved, non-superseded asset (``campaign_id`` 0 = all).
* ``POST /archive/drain`` — run one pass now. SYNC route: FastAPI runs it on its threadpool, so
  uploads never block the event loop.

No route here approves anything; approval stays in the data router's review route. The archive
finds new approvals on its own (``archive.reconcile``), so that route needs no hook.
"""

# NOTE: no `from __future__ import annotations` — FastAPI resolves route annotations against
# module globals, and the pydantic model lives inside the builder (the host-free rule).
import logging

log = logging.getLogger("protoagent.plugins.campaign")


def build_archive_router():
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field

    from . import archive, surface

    router = APIRouter()

    class Backfill(BaseModel):
        campaign_id: int = Field(0, ge=0)  # 0 = every campaign

    def _guarded(what: str, fn):
        try:
            return JSONResponse(fn())
        except Exception:  # noqa: BLE001 — the archive redacts its own errors; this is the last net
            log.exception("[campaign] archive %s failed", what)
            raise HTTPException(500, f"the archive {what} failed — see the agent log") from None

    @router.get("/archive")
    def _status() -> JSONResponse:
        return _guarded("status", lambda: archive.status(surface.config()))

    @router.post("/archive/backfill")
    def _backfill(body: Backfill) -> JSONResponse:
        return _guarded("backfill", lambda: {"queued": archive.backfill(body.campaign_id)})

    @router.post("/archive/drain")
    def _drain() -> JSONResponse:
        return _guarded("pass", lambda: archive.drain(surface.config()))

    return router
