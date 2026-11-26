"""TikTok Content Posting API v2 (developers.tiktok.com/doc/content-posting-api-reference-*).

  POST /v2/post/publish/creator_info/query/      -> privacy_level_options, creator_username  (scope video.publish)
  POST /v2/post/publish/video/init/              direct post {post_info, source_info}        (scope video.publish)
  POST /v2/post/publish/inbox/video/init/        upload to the creator's inbox (draft)        (scope video.upload)
       source_info = {source: FILE_UPLOAD, video_size, chunk_size, total_chunk_count} -> {publish_id, upload_url}
  PUT  upload_url  Content-Range: bytes a-b/total, Content-Type video/mp4  -> 206 partial / 201 complete
  POST /v2/post/publish/status/fetch/ {publish_id} -> PROCESSING_UPLOAD|PROCESSING_DOWNLOAD|SEND_TO_USER_INBOX|
       PUBLISH_COMPLETE|FAILED, fail_reason, uploaded_bytes, publicaly_available_post_id (sic)
OAuth (Login Kit for desktop): www.tiktok.com/v2/auth/authorize/ (client_key, comma-separated scopes, PKCE S256
       with a HEX-encoded SHA-256 challenge),
       open.tiktokapis.com/v2/oauth/token/ (authorization_code / refresh_token).
Chunk rules (assumed from docs): chunks 5-64 MB, files < 5 MB in one chunk, total_chunk_count = floor(size/chunk),
the last chunk absorbs the remainder (<= 128 MB).
Unaudited clients: every direct post is forced to SELF_ONLY (viewable only by the creator) and the account may
have to be private -> publish_mode private_only, outcome "private". Inbox uploads -> outcome "draft" (the creator
finishes posting in the TikTok app).
There is no lookup by our idempotency key: publish_id is persisted before any byte is uploaded; if init may
have succeeded without a recorded publish_id, reconcile() reports uncertain (operator confirmation).
settings: mode inbox|direct (default inbox), privacy_level, audited, chunk_size, disable_comment/duet/stitch.
"""
from __future__ import annotations

from urllib.parse import urlencode

import httpx

from .base import DestinationAdapter, PublishJob, PublishResult, http_result, oauth_error

API = "https://open.tiktokapis.com/v2"
AUTH = "https://www.tiktok.com/v2/auth/authorize/"
MB = 1024 * 1024
_PERMANENT = {"scope_not_authorized", "unaudited_client_can_only_post_to_private_accounts",
              "privacy_level_option_mismatch", "invalid_params", "url_ownership_unverified",
              "spam_risk_user_banned_from_posting", "reached_active_user_cap"}


def chunk_plan(size: int, want: int = 10 * MB) -> tuple[int, int]:
    want = min(64 * MB, max(5 * MB, want))
    if size <= want or size < 5 * MB:
        return size, 1
    return want, size // want


class TikTokAdapter(DestinationAdapter):
    provider = "tiktok"

    @property
    def mode(self) -> str:
        return "direct" if self.settings.get("mode") == "direct" else "inbox"

    oauth_scopes = ["user.info.basic", "video.upload", "video.publish"]

    @staticmethod
    def challenge(verifier: str) -> str:
        import hashlib
        return hashlib.sha256(verifier.encode()).hexdigest()          # TikTok: hex, not base64url

    def _h(self, creds: dict) -> dict:
        return {"Authorization": f"Bearer {creds.get('access_token', '')}", "Content-Type": "application/json; charset=UTF-8"}

    def _err(self, r: httpx.Response, what: str) -> PublishResult | None:
        try:
            e = r.json().get("error") or {}
        except Exception:
            e = {}
        code = e.get("code", "ok" if r.status_code < 400 else "http")
        if code == "ok" and r.status_code < 400:
            return None
        msg = f"{what}: {code} {e.get('message', '')} (log_id {e.get('log_id', '-')})"
        if code in ("access_token_invalid", "token_expired") or r.status_code == 401:
            return PublishResult("auth_expired", detail=msg)
        if code == "rate_limit_exceeded" or r.status_code == 429:
            return PublishResult("rate_limited", retry_after=60.0, detail=msg)
        if code == "spam_risk_too_many_posts":
            return PublishResult("rate_limited", retry_after=3600.0, detail=msg + " (daily post cap)")
        if code in _PERMANENT:
            hint = {"unaudited_client_can_only_post_to_private_accounts": " - set the TikTok account to private or get the app audited",
                    "scope_not_authorized": " - reconnect and grant video.upload / video.publish"}.get(code, "")
            return PublishResult("failed", detail=msg + hint, permanent=True)
        return http_result(r, what, msg) or PublishResult("failed", detail=msg)

    async def creator_info(self, c, creds) -> tuple[dict, PublishResult | None]:
        r = await c.post(f"{API}/post/publish/creator_info/query/", headers=self._h(creds))
        bad = self._err(r, "creator_info")
        return ({} if bad else r.json()["data"]), bad

    def _privacy(self, options: list[str]) -> str | None:
        want = self.settings.get("privacy_level") or ("PUBLIC_TO_EVERYONE" if self.settings.get("audited") else "SELF_ONLY")
        if want in options:
            return want
        return "SELF_ONLY" if "SELF_ONLY" in options else None

    async def upload(self, job: PublishJob, creds: dict) -> PublishResult:
        size = job.video_path.stat().st_size
        if why := job.may_write():                         # checked right before the first write (also on a replay)
            return PublishResult("failed", permanent=True, expired=True, detail=why)
        chunk, count = chunk_plan(size, int(self.settings.get("chunk_size", 10 * MB)))
        source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
        async with self.client() as c:
            if self.mode == "direct":
                info, bad = await self.creator_info(c, creds)
                if bad:
                    return bad
                privacy = self._privacy(info.get("privacy_level_options") or [])
                if not privacy:
                    return PublishResult("failed", permanent=True, detail=f"no usable privacy level in {info}")
                body = {"post_info": {"title": (job.description or job.title)[:2200],
                                      "privacy_level": privacy,
                                      "disable_comment": bool(self.settings.get("disable_comment", False)),
                                      "disable_duet": bool(self.settings.get("disable_duet", False)),
                                      "disable_stitch": bool(self.settings.get("disable_stitch", False))},
                        "source_info": source}
                url, meta = f"{API}/post/publish/video/init/", {"privacy": privacy, "username": info.get("creator_username", "")}
            else:
                body, url, meta = {"source_info": source}, f"{API}/post/publish/inbox/video/init/", {"privacy": "INBOX"}
            if why := job.may_write():                     # at dispatch, after the awaited creator_info
                return PublishResult("failed", permanent=True, expired=True, detail=why)
            # persisted BEFORE init; upload_v=2 marks refs that record final_chunk_attempted (older refs do not)
            # A fresh init follows a definitive no-post result or an explicit confirmation.
            # Its polling handle must never refer back to the abandoned attempt.
            job.save(init_started=True, publish_id="", id="", uploaded=False, final_chunk_attempted=False,
                     upload_v=2, **meta)
            try:
                r = await c.post(url, json=body, headers=self._h(creds))
            except httpx.ConnectError:
                job.save(init_started=False)        # request never left this machine
                raise
            if bad := self._err(r, "publish init"):
                job.save(init_started=False)        # provider rejected it: nothing was created
                return bad
            data = r.json()["data"]
            job.save(publish_id=data["publish_id"])  # persisted BEFORE uploading bytes
            with open(job.video_path, "rb") as f:
                for i in range(count):
                    if why := job.may_write():             # before EVERY chunk, the final one included
                        return PublishResult("failed", permanent=True, expired=True, provider_ref=data["publish_id"],
                                             detail=f"{why}; stopped before chunk {i + 1}/{count}: TikTok cannot "
                                                    f"publish an incomplete upload")
                    start = i * chunk
                    data_b = f.read(chunk if i < count - 1 else size - start)
                    if i == count - 1:                     # persisted BEFORE the final bytes leave: from here on
                        job.save(final_chunk_attempted=True)   # TikTok may complete and publish this upload
                    end = start + len(data_b) - 1
                    r = await c.put(data["upload_url"], content=data_b, headers={
                        "Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{end}/{size}"})
                    if r.status_code not in (200, 201, 206):
                        return http_result(r, f"chunk {i + 1}/{count}") or PublishResult("failed", detail=r.text[:200])
            job.save(uploaded=True, id=data["publish_id"])
        return PublishResult("processing", provider_ref=data["publish_id"], detail=f"TikTok {self.mode} upload complete")

    def _status(self, job: PublishJob, d: dict, publish_id: str) -> PublishResult:
        st = d.get("status", "")
        if st in ("PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD"):
            return PublishResult("processing", provider_ref=publish_id, detail=f"TikTok {st}")
        if st == "SEND_TO_USER_INBOX" or (st == "PUBLISH_COMPLETE" and self.mode == "inbox"):
            return PublishResult("draft", provider_ref=publish_id, post_id=publish_id,
                                 detail="in the creator's TikTok inbox: open the TikTok app notification to finish posting")
        if st == "PUBLISH_COMPLETE":
            ids = d.get("publicaly_available_post_id") or d.get("publicly_available_post_id") or []
            pid = str(ids[0]) if ids else ""
            user = job.ref.get("username", "")
            url = f"https://www.tiktok.com/@{user}/video/{pid}" if pid and user else ""
            if job.ref.get("privacy") == "PUBLIC_TO_EVERYONE":
                return PublishResult("published", provider_ref=publish_id, post_id=pid or publish_id, url=url)
            return PublishResult("private", provider_ref=publish_id, post_id=pid or publish_id, url=url,
                                 detail=f"posted with privacy {job.ref.get('privacy')} (unaudited apps are limited to SELF_ONLY)")
        if st == "FAILED":
            reason = d.get("fail_reason", "")
            return PublishResult("failed", provider_ref=publish_id, detail=f"TikTok FAILED: {reason}",
                                 permanent="internal" not in reason, rejected="internal" not in reason)
        return PublishResult("processing", provider_ref=publish_id, detail=f"TikTok status {st or '?'}")

    async def _fetch(self, publish_id: str, creds: dict) -> tuple[dict, PublishResult | None]:
        async with self.client() as c:
            r = await c.post(f"{API}/post/publish/status/fetch/", json={"publish_id": publish_id}, headers=self._h(creds))
        bad = self._err(r, "status/fetch")
        return ({} if bad else r.json().get("data") or {}), bad

    async def poll(self, job: PublishJob, provider_ref: str, creds: dict) -> PublishResult:
        # Older resumed uploads can carry the abandoned polling handle beside the current publish_id.
        pid = job.ref.get("publish_id") or provider_ref
        if pid and job.ref.get("id") != pid:
            job.save(id=pid)
        d, bad = await self._fetch(pid, creds)
        return bad or self._status(job, d, pid)

    @staticmethod
    def final_possibly_sent(job: PublishJob) -> bool:
        """The final chunk may have been dispatched: recorded before it was sent, or every byte was acknowledged,
        or an older ref (before upload_v 2) that cannot tell: treated conservatively as possibly sent."""
        r = job.ref
        return bool(r.get("uploaded") or r.get("final_chunk_attempted") or
                    (r.get("publish_id") and r.get("upload_v") != 2))

    async def reconcile(self, job: PublishJob, creds: dict) -> PublishResult | None:
        pid = job.ref.get("publish_id")
        if pid:
            d, bad = await self._fetch(pid, creds)
            if bad:
                return bad if bad.state in ("auth_expired", "rate_limited") else PublishResult("uncertain", detail=bad.detail)
            res = self._status(job, d, pid)
            size = job.video_path.stat().st_size if job.video_path.exists() else 0
            if res.state == "failed":
                return None   # TikTok's own terminal FAILED: that attempt cannot become a post
            if self.final_possibly_sent(job) and res.state == "processing":
                # the final bytes may have reached TikTok: a nonterminal, missing or lagging byte count proves
                # nothing. Keep the publish id (receipt and duplicate hold stay) and only poll; never a new init,
                # across expiry and restarts, until TikTok reports a definitive outcome.
                job.save(id=pid)
                return PublishResult("processing", provider_ref=pid,
                                     detail=f"TikTok {d.get('status') or 'status unknown'} after a possible final "
                                            f"upload (uploaded_bytes {d.get('uploaded_bytes', 'absent')}): holding "
                                            f"and polling, never uploading again")
            if d.get("status") == "PROCESSING_UPLOAD" and not job.ref.get("uploaded") \
                    and int(d.get("uploaded_bytes") or 0) < size:
                return None   # the final chunk was never sent: that attempt cannot become a post
            job.save(id=pid)
            return res
        if job.ref.get("init_started"):
            return PublishResult("uncertain", detail="TikTok init may have succeeded but no publish_id was recorded; "
                                 "TikTok has no lookup by our key")
        return None

    async def account(self, creds: dict) -> dict:
        audited = bool(self.settings.get("audited"))
        async with self.client() as c:
            # open_id (user.info.basic, granted in both modes) is TikTok's stable per-app id for this account
            r = await c.get(f"{API}/user/info/", params={"fields": "open_id,display_name"}, headers=self._h(creds))
            if bad := self._err(r, "user/info"):
                raise PermissionError(bad.detail)
            user = r.json()["data"]["user"]
            name, extra = user.get("display_name", ""), ""
            if self.mode == "direct":
                info, bad = await self.creator_info(c, creds)
                if bad:
                    raise PermissionError(bad.detail)
                name, extra = info.get("creator_username", "") or name, f"privacy options {info.get('privacy_level_options')}"
        mode = "draft_only" if self.mode == "inbox" else ("public" if audited else "private_only")
        notes = {"draft_only": "Uploads land in the TikTok inbox; finish each post in the TikTok app.",
                 "private_only": "Unaudited TikTok app: direct posts are SELF_ONLY until the app passes TikTok's audit.",
                 "public": ""}[mode]
        return {"account": name, "account_id": str(user.get("open_id") or ""),
                "scopes": (creds.get("scope") or "").replace(",", " ").split(),
                "publish_mode": mode, "notes": (notes + " " + extra).strip()}

    async def refresh(self, creds: dict) -> dict | None:
        key = creds.get("client_key") or creds.get("client_id")
        if not (creds.get("refresh_token") and key):
            return None
        async with self.client() as c:
            r = await c.post(f"{API}/oauth/token/", data={"client_key": key, "client_secret": creds.get("client_secret", ""),
                                                          "grant_type": "refresh_token", "refresh_token": creds["refresh_token"]})
        d = r.json() if r.status_code == 200 else {}
        if not d.get("access_token"):
            self.last_refresh_error = oauth_error(r)
            return None
        return {**creds, **d}

    def authorize_url(self, app: dict, redirect_uri: str, state: str, code_challenge: str) -> str:
        return AUTH + "?" + urlencode({"client_key": app.get("client_key") or app["client_id"],
                                       "scope": ",".join(self.oauth_scopes), "response_type": "code",
                                       "redirect_uri": redirect_uri, "state": state,
                                       "code_challenge": code_challenge, "code_challenge_method": "S256"})

    async def exchange_code(self, app: dict, code: str, redirect_uri: str, code_verifier: str) -> dict:
        async with self.client() as c:
            r = await c.post(f"{API}/oauth/token/", data={
                "client_key": app.get("client_key") or app["client_id"], "client_secret": app.get("client_secret", ""),
                "code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri,
                "code_verifier": code_verifier})
        try:
            d = r.json()
        except ValueError:
            d = {}
        if not d.get("access_token"):
            raise ValueError(f"TikTok token exchange failed: HTTP {r.status_code} {d.get('error')} "
                             f"{d.get('error_description', '')}".strip())
        return d
