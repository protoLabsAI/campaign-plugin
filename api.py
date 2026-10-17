"""HTTP surface — two routers, two prefixes (plugin-view rule 1).

* ``/plugins/campaign/view`` — the gallery page, PUBLIC (an iframe navigation carries no bearer).
* ``/api/plugins/campaign/*`` — data, media and the review action, behind the host's
  default-deny operator auth. The page reaches them through the DS kit's authed fetch and
  turns media into blob: URLs, so no media URL is ever public.

The file route serves an asset only when its resolved path (symlinks followed) sits inside
the plugin's media root — a row pointing anywhere else is refused, whatever wrote it.

Approve/reject lives ONLY here: there is no agent tool for it. It's the operator's call. So
does superseding an APPROVED asset (decision ``supersede``) — the agent may supersede only
what isn't approved.
"""

# NOTE: no `from __future__ import annotations` here — the route signatures annotate with
# classes imported inside the builder (the host-free rule), and FastAPI resolves string
# annotations against MODULE globals, where those names don't exist.
import logging
from typing import Any, Callable, Literal

log = logging.getLogger("protoagent.plugins.campaign")

MEDIA_TYPES = {
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".gif": "image/gif",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
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
        decision: Literal["approve", "reject", "supersede"]
        note: str = ""
        superseded_by: list[int] = []

    def _rel_path(raw: str) -> str:
        from pathlib import Path

        if not raw:
            return ""
        try:
            return str(Path(raw).expanduser().resolve().relative_to(paths.media_root().resolve()))
        except (OSError, RuntimeError, ValueError):
            return Path(raw).name

    def _public_asset(a: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "id", "campaign_id", "lane_id", "kind", "title", "owner", "status", "path", "size_bytes", "width",
            "height", "duration_s", "limit_id", "parent_id", "notes", "review_note", "reviewed_at", "updated",
        )  # fmt: skip
        out = {k: a.get(k) for k in keys}
        out["superseded_by"] = list(a.get("superseded_by") or [])
        # created_at / updated_at (the row), made_at (the file: folder stamp or mtime), take_at.
        out.update(store.asset_times(a))
        out["has_file"] = bool(a.get("path")) and paths.is_contained(a["path"])
        # Never the raw filesystem path: relative to the media dir (the gallery's "Copy path"),
        # or just the file name for anything outside it.
        out["path"] = _rel_path(a.get("path") or "")
        out["can_review"] = a.get("status") in ("ready_for_review", "approved", "rejected")
        # Retention: a pruned tombstone (its file went; the row stays so a chain still reads), and
        # a ready-for-review asset nobody has looked at in STALE_REVIEW_DAYS (never auto-deleted).
        from . import retention

        out["pruned"] = store.is_pruned(a)
        age = retention.age_days(_utcnow(), a.get("updated"), a.get("created"))
        out["stale"] = a.get("status") == "ready_for_review" and age is not None and age >= retention.STALE_REVIEW_DAYS
        return out

    def _utcnow():
        from datetime import UTC, datetime

        return datetime.now(UTC)

    @router.get("/campaigns")
    async def _campaigns() -> JSONResponse:
        rows = []
        for c in store.list_campaigns(include_archived=True):
            every = store.list_assets(c["id"])
            assets = [a for a in every if not store.is_retired(a)]  # superseded takes don't count
            rows.append(
                {
                    "id": c["id"],
                    "name": c["name"],
                    "status": c["status"],
                    "launch_window": c["launch_window"],
                    "assets": len(assets),
                    "review": sum(1 for a in assets if a["status"] == "ready_for_review"),
                    "approved": sum(1 for a in assets if a["status"] == "approved"),
                    "superseded": len(every) - len(assets),
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
        from pathlib import Path

        # Resolve ONCE and serve exactly what was checked — no second resolution in between.
        try:
            p = Path(a["path"]).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            p = None
        if p is None or not paths.is_contained(p):
            # Either missing, or outside the media root (a traversal / symlink escape).
            raise HTTPException(403, "file is outside the campaign media directory or missing")
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
            a = store.review(asset_id, body.decision, body.note, body.superseded_by)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if emit:
            try:
                emit("asset_reviewed", {"campaign_id": a["campaign_id"], "asset_id": a["id"], "status": a["status"]})
            except Exception:  # noqa: BLE001 — the bus is chrome
                pass
        return JSONResponse({"asset": _public_asset(a)})

    # Retention (retention.py): the last scheduled report, a fresh dry run, and the operator's
    # own real run — which works whatever retention_mode says (the operator IS the authority the
    # mode stands in for). Never touches an approved or ready-for-review asset. Sync routes: the
    # plan stats every file and the prune unlinks, so they run on FastAPI's threadpool.
    class Prune(BaseModel):
        dry_run: bool = True
        campaign_id: int = 0

    @router.get("/retention")
    def _retention(campaign_id: int = 0) -> JSONResponse:
        from . import retention

        return JSONResponse({"last": retention.last_report(), "plan": retention.plan(campaign_id=campaign_id)})

    @router.post("/retention/prune")
    def _prune(body: Prune) -> JSONResponse:
        from . import retention

        if body.dry_run:
            return JSONResponse({**retention.plan(campaign_id=body.campaign_id), "dry_run": True})
        report = retention.execute(campaign_id=body.campaign_id)
        if emit:
            try:
                emit("campaign_changed", {"campaign_id": body.campaign_id, "pruned": len(report["done"])})
            except Exception:  # noqa: BLE001 — the bus is chrome
                pass
        return JSONResponse(report)

    # Video jobs (campaign_produce_video): progress + gates + where the files are, for a
    # dashboard or an API caller polling a job it started. Files download through /file/{id}.
    @router.get("/videos")
    async def _videos() -> JSONResponse:
        from . import produce

        # A malformed record is skipped (and logged) rather than 500-ing the whole listing.
        jobs = []
        for j in produce.list_jobs(50):
            try:
                if not isinstance(j, dict) or not j.get("id"):
                    raise ValueError("no id")
                jobs.append(_public_job(j))
            except Exception as e:  # noqa: BLE001 — one bad record never takes the listing down
                log.warning("[campaign] skipping a malformed video job record: %s", e)
        return JSONResponse({"jobs": jobs})

    @router.get("/videos/{job_id}")
    async def _video(job_id: str) -> JSONResponse:
        from . import produce

        job = produce.load(job_id)
        if job is None:
            raise HTTPException(404, "no such video job")
        try:
            return JSONResponse(_public_job(job))
        except Exception as e:  # noqa: BLE001
            # The same record the listing skips: a client-visible 422, never a server error.
            log.warning("[campaign] video job %s is malformed: %s", job_id, e)
            raise HTTPException(422, "this video job's record is malformed") from None

    def _public_job(job: dict[str, Any]) -> dict[str, Any]:
        from . import produce

        d = job.get("delivery") or {}
        return {
            "id": job.get("id", ""),
            "title": (job.get("inputs") or {}).get("title", ""),
            "status": job.get("status", "unknown"),
            "stage": job.get("stage"),
            "progress": produce.progress(job),
            "attempt": job.get("attempt", 0),
            "gates": job.get("gates") or [],
            "reasons": job.get("reasons") or [],
            "assets": d.get("assets") or {},
            # Only integer asset ids make a /file/{id} URL (that route takes an int).
            "downloads": {
                k: f"/api/plugins/campaign/file/{int(aid)}"
                for k, aid in (d.get("assets") or {}).items()
                if (isinstance(aid, int) and not isinstance(aid, bool)) or (isinstance(aid, str) and aid.isdigit())
            },
            "links": d.get("links") or {},
            "created": job.get("created"),
            "updated": job.get("updated"),
        }

    return router
