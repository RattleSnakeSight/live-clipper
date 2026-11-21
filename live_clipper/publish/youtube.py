"""YouTube Data API v3 (Shorts) via resumable videos.insert.

Endpoints (developers.google.com/youtube/v3/guides/using_resumable_upload_protocol):
  POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status
       headers X-Upload-Content-Length / X-Upload-Content-Type, JSON {snippet, status} -> Location = session URI
  PUT  <session> Content-Range: bytes a-b/total  -> 308 (Range: bytes=0-b) | 200/201 video resource
  PUT  <session> Content-Range: bytes */total    -> upload status query (resume after crash)
  GET  /youtube/v3/videos?part=status,processingDetails&id=   (processing poll)
  GET  /youtube/v3/channels?part=contentDetails&mine=true -> relatedPlaylists.uploads; playlistItems.list
OAuth: accounts.google.com/o/oauth2/v2/auth (PKCE S256, access_type=offline), oauth2.googleapis.com/token.
Scopes: youtube.upload (insert) + youtube.readonly (channels/playlistItems/videos.list for poll and reconcile;
assumption: youtube.upload alone is not accepted by those list calls).
Privacy: settings.privacy (public | unlisted | private; default public, always sent explicitly). Google's videos.insert reference (checked 2026-11) says "Videos uploaded from unverified API projects are not
restricted to private viewing mode"; the compliance audit governs extra quota. Only privacyStatus "public" counts as a
public post: "unlisted" is reachable by link but not public, so it is reported as "private". Shorts: vertical <= 3 min;
"#Shorts" in title/description is a hint, not required.
Idempotency marker: snippet.tags contains "lc-<hash>" and the description's last line "ref lc-<hash>".
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlencode

import httpx

from .base import DestinationAdapter, PublishJob, PublishResult, http_result, oauth_error

UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
API = "https://www.googleapis.com/youtube/v3"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
PARTS = "snippet,status,paidProductPlacementDetails"
_QUOTA = {"quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}


def _when(v) -> float | None:
    """snippet.publishedAt (RFC 3339) -> UTC seconds; anything unreadable -> None."""
    try:
        t = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t.timestamp() if t.tzinfo is not None else None


def _nz(d: dict) -> dict:
    """Desktop OAuth clients are public: the secret is optional, never send an empty one."""
    return {k: v for k, v in d.items() if v not in (None, "")}


def _clean(s: str) -> str:
    return s.replace("<", "").replace(">", "")


class YouTubeAdapter(DestinationAdapter):
    provider = "youtube"
    oauth_scopes = SCOPES

    def _h(self, creds: dict) -> dict:
        return {"Authorization": f"Bearer {creds.get('access_token', '')}"}

    def _err(self, r: httpx.Response, what: str) -> PublishResult | None:
        if r.status_code < 400:
            return None
        try:
            reasons = {e.get("reason") for e in r.json()["error"].get("errors", [])}
        except Exception:
            reasons = set()
        if r.status_code == 403 and reasons & _QUOTA:
            return PublishResult("rate_limited", retry_after=3600.0, detail=f"{what}: quota/limit {sorted(reasons)}")
        return http_result(r, what)

    def _privacy(self) -> str:
        return self.settings.get("privacy") or "public"

    def _result(self, v: dict) -> PublishResult:
        vid, st = v.get("id", ""), v.get("status") or {}
        up = st.get("uploadStatus", "uploaded")
        proc = (v.get("processingDetails") or {}).get("processingStatus")
        if up in ("failed", "rejected", "deleted"):
            why = st.get("failureReason") or st.get("rejectionReason") or up
            return PublishResult("failed", provider_ref=vid, detail=f"YouTube {up}: {why}", permanent=True,
                                 rejected=True)
        if up != "processed" and proc not in ("succeeded",):
            return PublishResult("processing", provider_ref=vid, detail=f"YouTube {up}/{proc or 'pending'}")
        priv = st.get("privacyStatus", "private")
        url = f"https://www.youtube.com/shorts/{vid}"
        if priv != "public":                   # private or unlisted: delivered, but not a public post
            why = (" (public was requested; YouTube kept it non-public: check the channel's upload defaults and "
                   "status)" if self._privacy() == "public" else "")
            what = "unlisted (reachable by link, not public)" if priv == "unlisted" else "private"
            return PublishResult("private", provider_ref=vid, post_id=vid, url=url, detail=f"uploaded as {what}{why}")
        return PublishResult("published", provider_ref=vid, post_id=vid, url=url, detail="YouTube public",
                             public_at=_when((v.get("snippet") or {}).get("publishedAt")))

    def metadata(self, job: PublishJob) -> dict:
        title = _clean(job.title).strip()[:100]
        if "#shorts" not in title.lower() and len(title) + 8 <= 100:
            title += " #Shorts"
        desc = _clean(job.description or job.title)
        if "#shorts" not in desc.lower():
            desc += "\n\n#Shorts"
        desc = f"{desc}\nref {job.marker}"[:5000]
        snip = {"title": title, "description": desc, "categoryId": str(self.settings.get("category_id", "20")),
                "tags": [*self.settings.get("tags", []), job.marker][:30],
                "defaultLanguage": job.language, "defaultAudioLanguage": self.settings.get("audio_language", job.language)}
        return {"snippet": snip, "status": {"privacyStatus": self._privacy(), "selfDeclaredMadeForKids": False,
                                            "embeddable": True},
                # since 2026-11-16 omitting this no longer means "no paid placement": always declare it
                "paidProductPlacementDetails": {"hasPaidProductPlacement": False}}

    @staticmethod
    def _next_offset(r: httpx.Response) -> int:
        m = re.match(r"bytes=0-(\d+)", r.headers.get("range", ""))
        return int(m.group(1)) + 1 if m else 0

    async def _session_status(self, c, job, creds, size) -> tuple[str, object]:
        r = await c.put(job.ref["session"], headers={**self._h(creds), "Content-Range": f"bytes */{size}"})
        if r.status_code in (200, 201):
            return "done", r.json()
        if r.status_code == 308:
            return "partial", self._next_offset(r)
        if r.status_code in (404, 410):
            return "gone", None
        return "error", self._err(r, "upload status")

    async def upload(self, job: PublishJob, creds: dict) -> PublishResult:
        size = job.video_path.stat().st_size
        if why := job.may_write():                         # checked right before the first write (also on a replay)
            return PublishResult("failed", permanent=True, expired=True, detail=why)
        chunk = max(256 * 1024, int(self.settings.get("chunk_size", 8 * 1024 * 1024)) // (256 * 1024) * 256 * 1024)
        async with self.client() as c:
            offset = 0
            if job.ref.get("session") and job.media_fp and job.ref.get("session_fp") != job.media_fp:
                # a resumable session is bound to the bytes it started with; never append other bytes to it
                return PublishResult("uncertain", keep_reconciling=True,
                                     detail="stored upload session belongs to a different media version; reconciling")
            if job.ref.get("session"):
                kind, val = await self._session_status(c, job, creds, size)
                if kind == "done":
                    job.save(id=val["id"])
                    return self._result(val)
                if kind == "error":
                    return val
                offset = val if kind == "partial" else 0
                if kind == "gone":
                    job.save(session="")
            if not job.ref.get("session"):
                if why := job.may_write():                 # at dispatch, after any awaited preflight
                    return PublishResult("failed", permanent=True, expired=True, detail=why)
                r = await c.post(UPLOAD, params={"uploadType": "resumable", "part": PARTS},
                                 json=self.metadata(job),
                                 headers={**self._h(creds), "X-Upload-Content-Length": str(size),
                                          "X-Upload-Content-Type": "video/mp4"})
                if bad := self._err(r, "videos.insert init"):
                    return bad
                job.save(session=r.headers["location"], session_fp=job.media_fp, sent_all=False)
            with open(job.video_path, "rb") as f:
                while True:
                    f.seek(offset)
                    data = f.read(chunk)
                    if why := job.may_write():             # before EVERY chunk, the final one included
                        if job.ref.get("sent_all"):        # an earlier final request may have created the video
                            return PublishResult("uncertain", keep_reconciling=True,
                                                 detail=f"{why}; every byte was sent before: reconciling, never "
                                                        f"uploading again")
                        return PublishResult("failed", permanent=True, expired=True,
                                             detail=f"{why}; the upload session was left incomplete (YouTube cannot "
                                                    f"create a video from it)")
                    if offset + len(data) >= size:
                        job.save(sent_all=True)     # from here on an unanswered request may have created the video
                    r = await c.put(job.ref["session"], content=data, headers={
                        **self._h(creds), "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{size}"})
                    if r.status_code == 308:
                        offset = self._next_offset(r)
                        continue
                    if r.status_code in (200, 201):
                        v = r.json()
                        job.save(id=v["id"])
                        return self._result(v)
                    return self._err(r, "upload chunk") or PublishResult("uncertain", detail=f"HTTP {r.status_code}")

    async def _videos(self, c, creds, ids: list[str]) -> tuple[list[dict], PublishResult | None]:
        r = await c.get(f"{API}/videos", params={"part": "snippet,status,processingDetails", "id": ",".join(ids)},
                        headers=self._h(creds))
        return (r.json().get("items", []), None) if r.status_code == 200 else ([], self._err(r, "videos.list"))

    async def stats(self, ids: list[str], creds: dict):
        """Read-only: {video_id: {"views", "likes", "channel_id", "privacy"}} (a hidden like count stays None), or the
        PublishResult of an error. videos.list costs one quota unit for up to 50 ids, whatever channel they are on."""
        async with self.client() as c:
            r = await c.get(f"{API}/videos", params={"part": "statistics,snippet,status", "id": ",".join(ids[:50])},
                            headers=self._h(creds))
        if r.status_code != 200:
            return self._err(r, "videos.list statistics") or PublishResult("uncertain", detail=f"HTTP {r.status_code}")
        out = {}
        for it in r.json().get("items", []):
            st = it.get("statistics") or {}
            out[str(it.get("id"))] = {**{k: (float(st[s]) if str(st.get(s, "")).isdigit() else None)
                                         for k, s in (("views", "viewCount"), ("likes", "likeCount"))},
                                      "published_at": _when((it.get("snippet") or {}).get("publishedAt")),
                                      "channel_id": str((it.get("snippet") or {}).get("channelId") or ""),
                                      "privacy": str((it.get("status") or {}).get("privacyStatus") or "")}
        return out

    async def poll(self, job: PublishJob, provider_ref: str, creds: dict) -> PublishResult:
        async with self.client() as c:
            items, bad = await self._videos(c, creds, [provider_ref])
        if bad:
            return bad
        return self._result(items[0]) if items else PublishResult("processing", provider_ref=provider_ref,
                                                                    detail="not yet visible in videos.list")

    async def reconcile(self, job: PublishJob, creds: dict) -> PublishResult | None:
        async with self.client() as c:
            if job.ref.get("id"):
                items, bad = await self._videos(c, creds, [job.ref["id"]])
                if bad:
                    return bad if bad.state in ("auth_expired", "rate_limited") else PublishResult("uncertain", detail=bad.detail)
                if items:
                    return self._result(items[0])
                # YouTube already gave us this video id: not being listed (privacy lag, another channel's token,
                # removal) proves nothing, so never fall through to "not found -> upload again"
                return PublishResult("uncertain", keep_reconciling=True, provider_ref=job.ref["id"],
                                     detail=f"YouTube accepted video {job.ref['id']} but videos.list does not show it; "
                                            "holding it (never uploading again)")
            if job.ref.get("session"):
                kind, val = await self._session_status(c, job, creds, job.video_path.stat().st_size
                                                       if job.video_path.exists() else 0)
                if kind == "done":
                    job.save(id=val["id"])
                    return self._result(val)
                if kind == "partial" and not job.ref.get("sent_all"):
                    return None          # session never completed: upload() resumes it, no duplicate possible
            r = await c.get(f"{API}/channels", params={"part": "contentDetails", "mine": "true"}, headers=self._h(creds))
            if bad := self._err(r, "channels.list"):
                return bad if bad.state in ("auth_expired", "rate_limited") else PublishResult("uncertain", detail=bad.detail)
            uploads = r.json()["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
            r = await c.get(f"{API}/playlistItems", params={"part": "contentDetails", "playlistId": uploads,
                                                             "maxResults": 50}, headers=self._h(creds))
            if bad := self._err(r, "playlistItems.list"):
                return bad if bad.state in ("auth_expired", "rate_limited") else PublishResult("uncertain", detail=bad.detail)
            ids = [i["contentDetails"]["videoId"] for i in r.json().get("items", [])]
            if not ids:
                return self._unfound(job)
            items, bad = await self._videos(c, creds, ids)
            if bad:
                return PublishResult("uncertain", detail=bad.detail)
        for v in items:
            sn = v.get("snippet") or {}
            if job.marker in (sn.get("tags") or []) or job.marker in (sn.get("description") or ""):
                job.save(id=v["id"])
                res = self._result(v)
                res.detail = "reconciled: " + res.detail
                return res
        return self._unfound(job)

    @staticmethod
    def _unfound(job: PublishJob) -> PublishResult | None:
        """Nothing matching on the channel. If every byte was already sent, the upload may have finished and only
        the answer got lost (the uploads playlist can lag): keep reconciling instead of uploading again."""
        if job.ref.get("sent_all"):
            return PublishResult("uncertain", keep_reconciling=True,
                                 detail="all bytes were sent earlier but the video is not visible yet; still reconciling")
        return None

    async def account(self, creds: dict) -> dict:
        async with self.client() as c:
            r = await c.get(f"{API}/channels", params={"part": "snippet", "mine": "true"}, headers=self._h(creds))
        r.raise_for_status()
        items = r.json().get("items") or []
        if not items:
            raise ValueError("this Google account has no YouTube channel")
        public = self._privacy() == "public"
        return {"account": items[0]["snippet"]["title"], "account_id": str(items[0].get("id") or ""),
                "scopes": (creds.get("scope") or "").split(),
                "publish_mode": "public" if public else "private_only",
                "notes": "" if public else f"Uploads are {self._privacy()} (destination setting privacy). "
                         "Only public uploads count as published."}

    def upload_scopes(self) -> list[str]:
        return [SCOPES[0]]                                   # youtube.upload

    async def granted_scopes(self, creds: dict) -> list[str] | None:
        """Google's own token introspection (read-only; the token goes in the POST body, never in a URL)."""
        async with self.client() as c:
            r = await c.post("https://oauth2.googleapis.com/tokeninfo", data={"access_token": creds.get("access_token", "")})
        if r.status_code != 200:
            return None
        return str(r.json().get("scope") or "").split()

    async def refresh(self, creds: dict) -> dict | None:
        if not (creds.get("refresh_token") and creds.get("client_id")):
            return None
        async with self.client() as c:
            r = await c.post(TOKEN, data=_nz({"client_id": creds["client_id"],
                                              "client_secret": creds.get("client_secret"),
                                              "refresh_token": creds["refresh_token"], "grant_type": "refresh_token"}))
        if r.status_code != 200:
            self.last_refresh_error = oauth_error(r)
            return None
        return {**creds, **r.json()}

    def authorize_url(self, app: dict, redirect_uri: str, state: str, code_challenge: str) -> str:
        return AUTH + "?" + urlencode({
            "client_id": app["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
            "scope": " ".join(SCOPES), "access_type": "offline", "include_granted_scopes": "true",
            "prompt": "consent", "state": state, "code_challenge": code_challenge, "code_challenge_method": "S256"})

    async def exchange_code(self, app: dict, code: str, redirect_uri: str, code_verifier: str) -> dict:
        async with self.client() as c:
            r = await c.post(TOKEN, data=_nz({"code": code, "client_id": app["client_id"],
                                              "client_secret": app.get("client_secret"),
                                              "redirect_uri": redirect_uri, "grant_type": "authorization_code",
                                              "code_verifier": code_verifier}))
        if r.status_code != 200:
            raise ValueError(f"Google token exchange failed: {oauth_error(r)}")
        return r.json()
