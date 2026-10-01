"""HTTP surface — two routers, two prefixes (plugin-view rule 1).

* ``/plugins/campaign/view`` — the gallery page, PUBLIC (an iframe navigation carries no bearer).
* ``/api/plugins/campaign/*`` — data, media and the review action, behind the host's
  default-deny operator auth. The page reaches them through the DS kit's authed fetch and
  turns media into blob: URLs, so no media URL is ever public.

The file route serves an asset only when its resolved path (symlinks followed) sits inside
the plugin's media root — a row pointing anywhere else is refused, whatever wrote it.

Approve/reject lives ONLY here: there is no agent tool for it. It's the operator's call.
"""

# NOTE: no `from __future__ import annotations` here — the route signatures annotate with
# classes imported inside the builder (the host-free rule), and FastAPI resolves string
# annotations against MODULE globals, where those names don't exist.
from typing import Any, Callable

MEDIA_TYPES = {
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".gif": "image/gif",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
# Anything else in the media dir (an .html or .svg someone dropped in and attached) is served
# as an opaque download — never as a document that could run script on the console's origin.
SAFE_HEADERS = {
    "Cache-Control": "private, max-age=60",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src 'self' data: blob:; media-src 'self' blob:; sandbox",
}


def build_view_router():
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    from .view import PAGE

    router = APIRouter()

    @router.get("/view", response_class=HTMLResponse)
    async def _view() -> HTMLResponse:
        return HTMLResponse(PAGE)

    return router


def build_data_router(emit: Callable[[str, dict], Any] | None = None):
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import FileResponse, JSONResponse
    from pydantic import BaseModel

    from . import paths, store

    router = APIRouter()

    class Review(BaseModel):
        decision: str
        note: str = ""

    def _public_asset(a: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "id", "campaign_id", "lane_id", "kind", "title", "owner", "status", "path", "size_bytes", "width",
            "height", "duration_s", "limit_id", "parent_id", "notes", "review_note", "reviewed_at", "updated",
        )  # fmt: skip
        out = {k: a.get(k) for k in keys}
        out["has_file"] = bool(a.get("path")) and paths.is_contained(a["path"])
        out["can_review"] = a.get("status") in ("ready_for_review", "approved", "rejected")
        return out

    @router.get("/campaigns")
    async def _campaigns() -> JSONResponse:
        rows = []
        for c in store.list_campaigns(include_archived=True):
            assets = store.list_assets(c["id"])
            rows.append(
                {
                    "id": c["id"],
                    "name": c["name"],
                    "status": c["status"],
                    "launch_window": c["launch_window"],
                    "assets": len(assets),
                    "review": sum(1 for a in assets if a["status"] == "ready_for_review"),
                    "approved": sum(1 for a in assets if a["status"] == "approved"),
                }
            )
        return JSONResponse({"campaigns": rows})

    @router.get("/campaigns/{campaign_id}/assets")
    async def _assets(campaign_id: int) -> JSONResponse:
        if store.get_campaign(campaign_id) is None:
            raise HTTPException(404, "no such campaign")
        return JSONResponse(
            {
                "lanes": [{"id": ln["id"], "name": ln["name"]} for ln in store.lanes(campaign_id)],
                "assets": [_public_asset(a) for a in store.list_assets(campaign_id)],
                "statuses": list(store.ASSET_STATUSES),
            }
        )

    @router.get("/file/{asset_id}")
    async def _file(asset_id: int):
        a = store.get_asset(asset_id)
        if a is None or not a.get("path"):
            raise HTTPException(404, "no file for this asset")
        if not paths.is_contained(a["path"]):
            # Either missing, or outside the media root (a traversal / symlink escape).
            raise HTTPException(403, "file is outside the campaign media directory or missing")
        from pathlib import Path

        p = Path(a["path"]).resolve()
        media = MEDIA_TYPES.get(p.suffix.lower())
        if media is None:
            return FileResponse(
                p,
                media_type="application/octet-stream",
                filename=p.name,  # Content-Disposition: attachment
                headers=SAFE_HEADERS,
            )
        return FileResponse(p, media_type=media, headers=SAFE_HEADERS)

    @router.post("/assets/{asset_id}/review")
    async def _review(asset_id: int, body: Review) -> JSONResponse:
        try:
            a = store.review(asset_id, body.decision, body.note)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if emit:
            try:
                emit("asset_reviewed", {"campaign_id": a["campaign_id"], "asset_id": a["id"], "status": a["status"]})
            except Exception:  # noqa: BLE001 — the bus is chrome
                pass
        return JSONResponse({"asset": _public_asset(a)})

    return router
