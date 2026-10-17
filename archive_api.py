"""The protoContent archive's operator routes — a router of its own, mounted beside the data
router under ``/api/plugins/campaign`` (behind the host's default-deny operator auth).

* ``GET  /archive`` — archived / queued / retrying.
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
    from pydantic import BaseModel

    from . import archive, surface

    router = APIRouter()

    class Backfill(BaseModel):
        campaign_id: int = 0

    @router.get("/archive")
    def _status() -> JSONResponse:
        return JSONResponse(archive.status())

    @router.post("/archive/backfill")
    def _backfill(body: Backfill) -> JSONResponse:
        return JSONResponse({"queued": archive.backfill(body.campaign_id)})

    @router.post("/archive/drain")
    def _drain() -> JSONResponse:
        try:
            return JSONResponse(archive.drain(surface.config()))
        except Exception as e:  # noqa: BLE001 — drain redacts its own errors; this is the last net
            log.exception("[campaign] archive pass failed")
            raise HTTPException(500, f"archive pass failed ({type(e).__name__}) — see the agent log") from None

    return router
