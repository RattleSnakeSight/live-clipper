"""Sandbox destination: talks HTTP to the local fake provider (sandbox_server). Outcomes are labelled
"test" so nobody mistakes a test post for a real one. Settings: base_url (default: the in-process
sandbox server), privacy public|private|draft, timeout, poll_interval."""
from __future__ import annotations

from urllib.parse import urlencode

from .base import DestinationAdapter, PublishJob, PublishResult, http_result

_STATE = {"processing": "processing", "published": "published", "private": "private", "draft": "draft"}


class SandboxAdapter(DestinationAdapter):
    provider = "test"
    outcome_label = "test"
    oauth_scopes = ["video.upload", "video.publish"]
    timeout = 30.0

    @property
    def base(self) -> str:
        if self.settings.get("base_url"):
            return self.settings["base_url"].rstrip("/")
        from .sandbox_server import ensure_sandbox
        return ensure_sandbox(self.app_settings).url   # app_settings = state directory

    def _h(self, creds: dict) -> dict:
        return {"Authorization": f"Bearer {creds.get('access_token', '')}"}

    def _res(self, v: dict) -> PublishResult:
        st = _STATE.get(v.get("status"), "uncertain")
        return PublishResult(st, provider_ref=v["id"], post_id=v["id"] if st != "processing" else "",
                             url=v.get("url", ""), detail=f"test video {v['id']} {v.get('status')}")

    async def upload(self, job: PublishJob, creds: dict) -> PublishResult:
        if why := job.may_write():
            return PublishResult("failed", permanent=True, expired=True, detail=why)
        q = urlencode({"title": job.title, "tag": job.marker, "privacy": job.settings.get("privacy", "public")})
        async with self.client() as c:
            r = await c.post(f"{self.base}/v1/videos?{q}", content=job.video_path.read_bytes(),
                             headers={**self._h(creds), "Content-Type": "video/mp4"})
        if bad := http_result(r, "sandbox upload"):
            return bad
        v = r.json()
        job.save(id=v["id"])
        return self._res(v)

    async def poll(self, job: PublishJob, provider_ref: str, creds: dict) -> PublishResult:
        async with self.client() as c:
            r = await c.get(f"{self.base}/v1/videos/{provider_ref}", headers=self._h(creds))
        if r.status_code == 404:
            return PublishResult("failed", detail="test video disappeared", permanent=True)
        return http_result(r, "sandbox status") or self._res(r.json())

    async def reconcile(self, job: PublishJob, creds: dict) -> PublishResult | None:
        async with self.client() as c:
            r = await c.get(f"{self.base}/v1/videos", params={"tag": job.marker}, headers=self._h(creds))
        if bad := http_result(r, "sandbox list"):
            return bad if bad.state in ("auth_expired", "rate_limited") else PublishResult("uncertain", detail=bad.detail)
        items = r.json()["items"]
        if not items:
            return None
        res = self._res(items[0])
        res.detail = f"reconciled: found earlier upload {items[0]['id']}" + (
            f" (+{len(items) - 1} duplicates!)" if len(items) > 1 else "")
        return res

    async def account(self, creds: dict) -> dict:
        async with self.client() as c:
            r = await c.get(f"{self.base}/v1/me", headers=self._h(creds))
        if r.status_code == 401:
            raise PermissionError("sandbox token rejected")
        r.raise_for_status()
        d = r.json()
        return {"account": d["account"], "account_id": str(d.get("id") or d["account"]), "scopes": d["scopes"],
                "publish_mode": "public",
                "notes": "Test destination: nothing is posted, nothing leaves this machine."}

    async def refresh(self, creds: dict) -> dict | None:
        if not creds.get("refresh_token"):
            return None
        async with self.client() as c:
            r = await c.post(f"{self.base}/oauth/token",
                             data={"grant_type": "refresh_token", "refresh_token": creds["refresh_token"]})
        if r.status_code != 200:
            return None
        return {**creds, **r.json()}

    def authorize_url(self, app: dict, redirect_uri: str, state: str, code_challenge: str) -> str:
        return f"{self.base}/oauth/authorize?" + urlencode({
            "client_id": app.get("client_id", "sandbox"), "redirect_uri": redirect_uri, "state": state,
            "response_type": "code", "code_challenge": code_challenge, "code_challenge_method": "S256"})

    async def exchange_code(self, app: dict, code: str, redirect_uri: str, code_verifier: str) -> dict:
        async with self.client() as c:
            r = await c.post(f"{self.base}/oauth/token", data={
                "grant_type": "authorization_code", "code": code, "code_verifier": code_verifier,
                "redirect_uri": redirect_uri})
        r.raise_for_status()
        return r.json()

    async def issue_test_token(self) -> dict:
        """Auto-connect helper for the default sandbox destination (local admin endpoint)."""
        async with self.client() as c:
            r = await c.post(f"{self.base}/_admin/issue_token")
        r.raise_for_status()
        return r.json()
