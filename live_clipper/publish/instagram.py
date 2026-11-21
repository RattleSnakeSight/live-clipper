"""Instagram Reels via the Instagram Graph API content publishing flow (resumable upload).

  GET  /{ig-user-id}/content_publishing_limit?fields=quota_usage,config      (24h rolling post cap)
  POST /{ig-user-id}/media  media_type=REELS upload_type=resumable caption share_to_feed -> {id: container}
  POST https://rupload.facebook.com/ig-api-upload/{ver}/{container}  headers offset, file_size, Authorization: OAuth
  GET  /{container}?fields=status_code,status   IN_PROGRESS|FINISHED|ERROR|EXPIRED|PUBLISHED
  POST /{ig-user-id}/media_publish creation_id=<container> -> {id: media}
  GET  /{media}?fields=permalink,shortcode ; GET /{ig-user-id}/media?fields=id,caption,permalink,timestamp
Hosts: settings.login = "instagram" (Instagram Login, graph.instagram.com, scopes instagram_business_basic +
instagram_business_content_publish) or "facebook" (Facebook Login, graph.facebook.com, scopes instagram_basic,
instagram_content_publish, pages_show_list, pages_read_engagement, business_management). settings.api_version
defaults to v23.0 (assumption: any currently supported Graph version works). Graph calls send the token as
"Authorization: Bearer" (assumed accepted by both hosts) so it never appears in URLs.
There is no private/draft mode for API publishing: a published Reel is public immediately (publish_mode
"public"). Requires a professional (Business/Creator) account. Container ids are persisted before upload and
publish_started before media_publish; reconcile matches the "lc-<hash>" marker at the end of the caption.
"""
from __future__ import annotations

from urllib.parse import urlencode

from .base import DestinationAdapter, PublishJob, PublishResult, http_result, oauth_error

RUPLOAD = "https://rupload.facebook.com/ig-api-upload"
IG_SCOPES = ["instagram_business_basic", "instagram_business_content_publish"]
FB_SCOPES = ["instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement",
             "business_management"]


class InstagramAdapter(DestinationAdapter):
    provider = "instagram"

    @property
    def fb(self) -> bool:
        return self.settings.get("login") == "facebook"

    @property
    def ver(self) -> str:
        return self.settings.get("api_version", "v23.0")

    @property
    def base(self) -> str:
        return f"https://graph.{'facebook' if self.fb else 'instagram'}.com/{self.ver}"

    @property
    def oauth_scopes(self) -> list[str]:
        return FB_SCOPES if self.fb else IG_SCOPES

    def _h(self, creds: dict) -> dict:
        return {"Authorization": f"Bearer {creds.get('access_token', '')}"}

    def _uid(self, creds: dict) -> str:
        """Only the VERIFIED Instagram account (set by connect() after account() and the pin check). Credential or
        settings overrides (e.g. a stale ig_user_id from another account) are never used as a send target."""
        info = self.settings.get("account_info") or {}
        return str(info.get("account_id") or info.get("ig_user_id") or "")

    def _err(self, r, what: str) -> PublishResult | None:
        if r.status_code < 400:
            return None
        try:
            e = r.json().get("error") or {}
        except Exception:
            e = {}
        code, msg = e.get("code"), f"{what}: {e.get('message') or r.text[:200]} (code {e.get('code')}/{e.get('error_subcode')})"
        if code == 190 or r.status_code == 401:
            return PublishResult("auth_expired", detail=msg)
        if code in (4, 17, 32, 613) or e.get("error_subcode") == 2207042 or r.status_code == 429:
            return PublishResult("rate_limited", retry_after=3600.0 if e.get("error_subcode") == 2207042 else 300.0, detail=msg)
        if code in (1, 2) or e.get("is_transient"):
            return PublishResult("failed", detail=msg)
        return http_result(r, what, msg)

    async def _quota(self, c, creds) -> PublishResult | None:
        r = await c.get(f"{self.base}/{self._uid(creds)}/content_publishing_limit",
                        params={"fields": "quota_usage,config"}, headers=self._h(creds))
        if r.status_code != 200:
            return self._err(r, "content_publishing_limit") if r.status_code == 401 else None
        d = (r.json().get("data") or [{}])[0]
        total = (d.get("config") or {}).get("quota_total")
        if total and d.get("quota_usage", 0) >= total:
            return PublishResult("rate_limited", retry_after=3600.0, detail=f"Instagram publishing limit {d['quota_usage']}/{total} per 24h")
        return None

    async def upload(self, job: PublishJob, creds: dict) -> PublishResult:
        if why := job.may_write():                         # checked right before the first write (also on a replay)
            return PublishResult("failed", permanent=True, expired=True, detail=why)
        if not self._uid(creds):
            return PublishResult("failed", permanent=True, detail="no verified Instagram account: "
                                 "verify or reconnect the destination")
        if not self.fb and not self.settings.get("video_base_url"):
            return PublishResult("failed", permanent=True, detail="Instagram Login cannot upload local files: set "
                                 "video_base_url (public https folder serving the clips) or use Facebook Login")
        size = job.video_path.stat().st_size
        caption = f"{(job.description or job.title)[:2150]}\n\n{job.marker}"
        async with self.client() as c:
            if bad := await self._quota(c, creds):
                return bad
            if why := job.may_write():                         # at dispatch, after the awaited quota lookup
                return PublishResult("failed", permanent=True, expired=True, detail=why)
            form = {"media_type": "REELS", "caption": caption,
                    "share_to_feed": "true" if self.settings.get("share_to_feed", True) else "false"}
            if self.fb:
                form["upload_type"] = "resumable"
            else:       # Instagram Login: Meta fetches the file itself from a public URL
                form["video_url"] = f"{str(self.settings['video_base_url']).rstrip('/')}/{job.video_path.name}"
            r = await c.post(f"{self.base}/{self._uid(creds)}/media", headers=self._h(creds), data=form)
            if bad := self._err(r, "create container"):
                return bad
            cid = r.json()["id"]
            job.save(container=cid, publish_started=False, media_id="")   # persisted BEFORE uploading bytes
            if why := job.may_write():                         # an unpublished container can never become public
                return PublishResult("failed", permanent=True, expired=True, provider_ref=cid, detail=why)
            if self.fb:
                r = await c.post(f"{RUPLOAD}/{self.ver}/{cid}", content=job.video_path.read_bytes(), headers={
                    "Authorization": f"OAuth {creds.get('access_token', '')}", "offset": "0", "file_size": str(size)})
                if bad := self._err(r, "rupload"):
                    return bad
        return PublishResult("processing", provider_ref=cid, detail="uploaded; Instagram is processing the Reel")

    async def _permalink(self, c, creds, mid: str) -> PublishResult:
        r = await c.get(f"{self.base}/{mid}", params={"fields": "permalink,shortcode"}, headers=self._h(creds))
        link = r.json().get("permalink", "") if r.status_code == 200 else ""
        return PublishResult("published", provider_ref=mid, post_id=mid, url=link, detail="published as a Reel")

    async def poll(self, job: PublishJob, provider_ref: str, creds: dict) -> PublishResult:
        cid = job.ref.get("container") or provider_ref
        async with self.client() as c:
            if job.ref.get("media_id"):
                return await self._permalink(c, creds, job.ref["media_id"])
            r = await c.get(f"{self.base}/{cid}", params={"fields": "status_code,status"}, headers=self._h(creds))
            if bad := self._err(r, "container status"):
                return bad
            st = r.json().get("status_code")
            if st == "IN_PROGRESS":
                return PublishResult("processing", provider_ref=cid, detail="IN_PROGRESS")
            if st == "ERROR":
                return PublishResult("failed", permanent=True, rejected=True,
                                     detail=f"container ERROR: {r.json().get('status')}")
            if st in ("EXPIRED", "PUBLISHED"):
                return PublishResult("uncertain", detail=f"container {st}; reconciling")
            if not self._uid(creds):                           # never publish to an unverified/unpinned account
                return PublishResult("uncertain", detail="no verified Instagram account: not publishing the container")
            if bad := await self._quota(c, creds):
                return bad
            if why := job.may_write():                         # freshness at dispatch, after the awaited lookups
                if job.ref.get("publish_started"):             # an earlier media_publish may have gone through
                    return PublishResult("uncertain", keep_reconciling=True, provider_ref=cid,
                                         detail=f"{why}; an earlier publish request may have succeeded: reconciling "
                                                f"read-only, never publishing again")
                return PublishResult("failed", permanent=True, expired=True, provider_ref=cid,
                                     detail=f"{why}; the finished container was never published (no post exists)")
            job.save(publish_started=True)                     # persisted BEFORE media_publish
            r = await c.post(f"{self.base}/{self._uid(creds)}/media_publish", data={"creation_id": cid},
                             headers=self._h(creds))
            if r.status_code >= 400 and (r.json().get("error") or {}).get("code") == 9007:
                return PublishResult("processing", provider_ref=cid, detail="media not ready yet")
            if bad := self._err(r, "media_publish"):
                return bad
            mid = r.json()["id"]
            job.save(media_id=mid)
            return await self._permalink(c, creds, mid)

    late_public_after_accept = False                           # a container is public only after media_publish

    async def status(self, job: PublishJob, provider_ref: str, creds: dict) -> PublishResult:
        """READ-ONLY: never calls media_publish (a dashboard re-check must not publish anything)."""
        cid = job.ref.get("container") or provider_ref
        async with self.client() as c:
            if job.ref.get("media_id"):
                return await self._permalink(c, creds, job.ref["media_id"])
            r = await c.get(f"{self.base}/{cid}", params={"fields": "status_code,status"}, headers=self._h(creds))
            if bad := self._err(r, "container status"):
                return bad
            st = r.json().get("status_code")
        if st in ("IN_PROGRESS", "FINISHED"):
            return PublishResult("processing", provider_ref=cid, detail=f"container {st}; a re-check never publishes")
        if st == "ERROR":
            return PublishResult("failed", permanent=True, rejected=True,
                                 detail=f"container ERROR: {r.json().get('status')}")
        found = await self.reconcile(job, creds)               # PUBLISHED / EXPIRED: read-only lookup by marker
        return found if found is not None and found.state == "published" else \
            PublishResult("uncertain", detail=f"container {st}; no Reel found by its marker")

    async def reconcile(self, job: PublishJob, creds: dict) -> PublishResult | None:
        async with self.client() as c:
            if job.ref.get("media_id"):
                return await self._permalink(c, creds, job.ref["media_id"])
            if not self._uid(creds):                           # cannot look: never claim "not published"
                return PublishResult("uncertain", detail="no verified Instagram account to reconcile against")
            r = await c.get(f"{self.base}/{self._uid(creds)}/media", headers=self._h(creds),
                            params={"fields": "id,caption,permalink,timestamp", "limit": 50})
            if bad := self._err(r, "list media"):
                return bad if bad.state in ("auth_expired", "rate_limited") else PublishResult("uncertain", detail=bad.detail)
            for m in r.json().get("data", []):
                if job.marker in (m.get("caption") or ""):
                    job.save(media_id=m["id"])
                    return PublishResult("published", provider_ref=m["id"], post_id=m["id"], url=m.get("permalink", ""),
                                         detail="reconciled: found the earlier Reel by caption marker")
            cid = job.ref.get("container")
            if not cid:
                return None
            r = await c.get(f"{self.base}/{cid}", params={"fields": "status_code"}, headers=self._h(creds))
            st = r.json().get("status_code") if r.status_code == 200 else None
        if st in ("IN_PROGRESS", "FINISHED"):
            return PublishResult("processing", provider_ref=cid, detail=f"container {st}: continuing")
        if st == "PUBLISHED":
            return PublishResult("uncertain", detail="container PUBLISHED but the Reel was not found in recent media")
        return None   # ERROR / EXPIRED / gone: never published, safe to upload again

    async def account(self, creds: dict) -> dict:
        async with self.client() as c:
            if self.fb:
                r = await c.get(f"{self.base}/me/accounts", headers=self._h(creds),
                                params={"fields": "name,instagram_business_account{id,username}"})
                r.raise_for_status()
                pages = [p for p in r.json().get("data", []) if p.get("instagram_business_account")]
                if not pages:
                    raise ValueError("no Facebook Page with a linked Instagram professional account")
                # one sign-in can manage several Pages/Instagram accounts (e.g. EN and ES): never pick by order
                pin = str(self.settings.get("target_account_id") or "")
                found = [(p, p["instagram_business_account"]) for p in pages]
                pick = [x for x in found if str(x[1]["id"]) == pin] if pin else found
                avail = ", ".join(f"{ig.get('username') or p['name']} ({ig['id']})" for p, ig in found)
                if not pick:
                    raise ValueError(f"the chosen Instagram account {pin} is not among this sign-in's accounts: {avail}")
                if len(pick) > 1:
                    raise ValueError(f"this sign-in manages several Instagram accounts ({avail}); set "
                                     f"target_account_id to the one this destination posts to")
                page, ig = pick[0]
                name, uid = ig.get("username") or page["name"], str(ig["id"])
            else:
                r = await c.get(f"{self.base}/me", params={"fields": "user_id,username,account_type"}, headers=self._h(creds))
                r.raise_for_status()
                d = r.json()
                name, uid = d.get("username", ""), str(d.get("user_id") or d.get("id"))
        return {"account": name, "account_id": uid, "ig_user_id": uid,
                "scopes": (creds.get("scope") or "").replace(",", " ").split(),
                "publish_mode": "public",
                "notes": "Instagram API has no private or draft mode: published Reels are public immediately. "
                         "Needs a Business/Creator account."}

    def upload_scopes(self) -> list[str]:
        return [x for x in self.oauth_scopes if "publish" in x]        # granted_scopes() cannot confirm it: unknown

    async def refresh(self, creds: dict) -> dict | None:
        if not creds.get("access_token"):
            return None
        async with self.client() as c:
            if self.fb:
                if not creds.get("client_id"):
                    return None
                r = await c.get(f"{self.base}/oauth/access_token", params={
                    "grant_type": "fb_exchange_token", "client_id": creds["client_id"],
                    "client_secret": creds.get("client_secret", ""), "fb_exchange_token": creds["access_token"]})
            else:   # long-lived IG tokens refresh only while still valid (and >= 24h old)
                r = await c.get("https://graph.instagram.com/refresh_access_token",
                                params={"grant_type": "ig_refresh_token", "access_token": creds["access_token"]})
        d = r.json() if r.status_code == 200 else {}
        if not d.get("access_token"):
            self.last_refresh_error = oauth_error(r)
            return None
        return {**creds, **d}

    def authorize_url(self, app: dict, redirect_uri: str, state: str, code_challenge: str) -> str:
        q = {"client_id": app["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
             "scope": ",".join(self.oauth_scopes), "state": state}
        if self.fb:
            return f"https://www.facebook.com/{self.ver}/dialog/oauth?" + urlencode(q)
        return "https://www.instagram.com/oauth/authorize?" + urlencode(q)

    async def exchange_code(self, app: dict, code: str, redirect_uri: str, code_verifier: str) -> dict:
        async with self.client() as c:
            if self.fb:
                r = await c.get(f"{self.base}/oauth/access_token", params={
                    "client_id": app["client_id"], "client_secret": app.get("client_secret", ""),
                    "redirect_uri": redirect_uri, "code": code})
                r.raise_for_status()
                short, extra = r.json()["access_token"], {}
                r = await c.get(f"{self.base}/oauth/access_token", params={
                    "grant_type": "fb_exchange_token", "client_id": app["client_id"],
                    "client_secret": app.get("client_secret", ""), "fb_exchange_token": short})
            else:
                r = await c.post("https://api.instagram.com/oauth/access_token", data={
                    "client_id": app["client_id"], "client_secret": app.get("client_secret", ""),
                    "grant_type": "authorization_code", "redirect_uri": redirect_uri, "code": code})
                r.raise_for_status()
                d = r.json()
                d = (d.get("data") or [d])[0]
                short = d["access_token"]
                extra = {"ig_user_id": str(d.get("user_id", "")), "scope": ",".join(d.get("permissions") or [])}
                r = await c.get("https://graph.instagram.com/access_token", params={
                    "grant_type": "ig_exchange_token", "client_secret": app.get("client_secret", ""), "access_token": short})
        r.raise_for_status()
        return {**extra, **r.json()}
