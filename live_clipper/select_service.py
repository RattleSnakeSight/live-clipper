"""Select service: selection formats and rules over HTTP (/api/formats*, /api/rules*).

Formats and rules are validated by live_clipper.formats and stored in SQLite (namespace `select`). The
built-in
format is seeded on start and never overwritten; POST /api/formats/{id}/reset restores it explicitly. The
active
format is pushed into the selection core (formats.set_active) whenever it changes.
"""
from __future__ import annotations

import functools
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import formats

log = logging.getLogger(__name__)


def _error(e: Exception) -> JSONResponse:
    if isinstance(e, formats.FormatError):
        return JSONResponse(status_code=422, content={"detail": e.to_dict()})
    if isinstance(e, formats.NotFound):
        return JSONResponse(status_code=404, content={"detail": {"message": str(e), "errors": []}})
    return JSONResponse(status_code=409, content={"detail": {"message": str(e), "errors": []}})


_HANDLED = (formats.FormatError, formats.NotFound, formats.Conflict)


async def _body(request: Request) -> Any:
    raw = await request.body()
    return formats.parse_json(raw) if raw.strip() else None


class SelectService:
    name = "select"

    def __init__(self) -> None:
        self.ctx = None
        self.router = APIRouter(prefix="/api", tags=["select"])
        self._routes()

    # ------------------------------------------------------------------ lifecycle
    async def start(self, ctx) -> None:
        self.ctx = ctx
        added = formats.seed(ctx.db)
        if added:
            log.info("seeded built-in selection formats %s", added)
        self._sync_active()

    async def stop(self) -> None:
        formats.set_active(None)

    def status(self) -> dict:
        if self.ctx is None:
            return {"ready": False}
        db = self.ctx.db
        return {"ready": True, "active_format": formats.active_id(db),
                "formats": len(formats.list_formats(db)),
                "rules": len(formats.list_rules(db))}

    def _sync_active(self) -> None:
        db = self.ctx.db
        try:
            formats.set_active(formats.get_format_model(db, formats.active_id(db)))
        except formats.FormatError:
            log.exception("stored active format is invalid; using the built-in default")
            formats.set_active(None)

    def _changed(self, what: str, ident: str | None) -> None:
        self._sync_active()
        self.ctx.bus.publish("select.formats_changed", {"what": what, "id": ident})

    # ------------------------------------------------------------------ routes
    def _routes(self) -> None:
        r = self.router

        def guarded(fn):
            @functools.wraps(fn)
            async def wrapper(*a, **k):
                try:
                    return await fn(*a, **k)
                except _HANDLED as e:
                    return _error(e)
            return wrapper

        @r.get("/formats")
        @guarded
        async def list_formats():
            return {"active": formats.active_id(self.ctx.db), "formats": formats.list_formats(self.ctx.db)}

        @r.post("/formats", status_code=201)
        @guarded
        async def create_format(request: Request):
            doc = formats.create_format(self.ctx.db, await _body(request))
            self._changed("format", doc["id"])
            return doc

        @r.post("/formats/import", status_code=201)
        @guarded
        async def import_format(request: Request, overwrite: bool = False):
            doc = formats.import_format(self.ctx.db, await request.body(), overwrite=overwrite)
            self._changed("format", doc["id"])
            return doc

        @r.post("/formats/preview-effective")
        @guarded
        async def preview_effective(request: Request):
            body = await _body(request) or {}
            if not isinstance(body, dict):
                raise formats.FormatError("body must be an object {channel, context, clip, format_id}")
            return formats.resolve_effective(self.ctx.db, body.get("channel"), body.get("context") or {},
                                             body.get("clip") or {}, body.get("format_id"))

        @r.get("/formats/schema")
        async def format_schema():
            return formats.SelectionFormat.model_json_schema()

        @r.get("/formats/{fid}")
        @guarded
        async def get_format(fid: str):
            return formats.get_format(self.ctx.db, fid)

        @r.put("/formats/{fid}")
        @guarded
        async def update_format(fid: str, request: Request):
            doc = formats.update_format(self.ctx.db, fid, await _body(request))
            self._changed("format", fid)
            return doc

        @r.delete("/formats/{fid}")
        @guarded
        async def delete_format(fid: str):
            formats.delete_format(self.ctx.db, fid)
            self._changed("format", fid)
            return {"deleted": fid}

        @r.get("/formats/{fid}/export")
        @guarded
        async def export_format(fid: str):
            doc = formats.export_format(self.ctx.db, fid)
            return Response(json.dumps(doc, indent=1, ensure_ascii=False), media_type="application/json",
                            headers={"Content-Disposition":
                                     f'attachment; filename="{fid}.selection-format.json"'})

        @r.post("/formats/{fid}/reset")
        @guarded
        async def reset_format(fid: str):
            doc = formats.reset_format(self.ctx.db, fid)
            self._changed("format", fid)
            return doc

        @r.post("/formats/{fid}/duplicate", status_code=201)
        @guarded
        async def duplicate_format(fid: str, request: Request):
            body = await _body(request) or {}
            doc = formats.duplicate_format(self.ctx.db, fid, body.get("id"), body.get("name"))
            self._changed("format", doc["id"])
            return doc

        @r.post("/formats/{fid}/activate")
        @guarded
        async def activate_format(fid: str):
            formats.set_active_id(self.ctx.db, fid)
            self._changed("active", fid)
            return {"active": fid}

        @r.get("/rules")
        @guarded
        async def list_rules():
            return {"rules": formats.list_rules(self.ctx.db)}

        @r.post("/rules", status_code=201)
        @guarded
        async def create_rule(request: Request, position: int | None = None):
            doc = formats.create_rule(self.ctx.db, await _body(request), position)
            self._changed("rule", doc["id"])
            return doc

        @r.post("/rules/reorder")
        @guarded
        async def reorder_rules(request: Request):
            body = await _body(request) or {}
            ids = body.get("ids") if isinstance(body, dict) else body
            if not isinstance(ids, list):
                raise formats.FormatError("body must be {\"ids\": [rule ids in the new order]}")
            rules = formats.reorder_rules(self.ctx.db, [str(x) for x in ids])
            self._changed("rules", None)
            return {"rules": rules}

        @r.get("/rules/schema")
        async def rule_schema():
            return formats.Rule.model_json_schema()

        @r.get("/rules/{rid}")
        @guarded
        async def get_rule(rid: str):
            return formats.get_rule(self.ctx.db, rid)

        @r.put("/rules/{rid}")
        @guarded
        async def update_rule(rid: str, request: Request):
            doc = formats.update_rule(self.ctx.db, rid, await _body(request))
            self._changed("rule", rid)
            return doc

        @r.delete("/rules/{rid}")
        @guarded
        async def delete_rule(rid: str):
            formats.delete_rule(self.ctx.db, rid)
            self._changed("rule", rid)
            return {"deleted": rid}
