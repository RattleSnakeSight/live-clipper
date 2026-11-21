"""Publishing adapter contract.

Adapters never raise for provider-side outcomes; they return a PublishResult. Transport errors
(timeouts, dropped connections) propagate as httpx exceptions: the worker treats them as ambiguous
and reconciles before ever uploading again.

Semantics of reconcile():
  None                       -> the provider definitely has no post for this idempotency key: safe to upload
  PublishResult(uncertain)   -> cannot tell; an operator must confirm before a re-upload
  any other PublishResult    -> found an earlier upload; the worker adopts it (no second upload)

Adapters call job.checkpoint({...}) to persist provider references (upload session, publish_id,
container id) BEFORE the network call that would make them matter, so a crash is reconcilable.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

STATES = {"accepted", "processing", "published", "draft", "private", "failed", "rate_limited",
          "auth_expired", "uncertain"}
FINAL_STATES = {"published", "draft", "private"}


@dataclass
class PublishJob:
    delivery_id: int
    idempotency_key: str
    video_path: Path
    title: str
    description: str = ""
    language: str = "en"
    srt_path: Path | None = None
    settings: dict = field(default_factory=dict)     # destination settings (non-secret)
    ref: dict = field(default_factory=dict)          # persisted provider references
    checkpoint: Callable[[dict], None] = lambda patch: None
    # consulted by adapters IMMEDIATELY before each controllable write (first upload request, resumed upload,
    # Instagram media_publish); a non-empty answer means: write nothing (e.g. the destination was disabled meanwhile)
    may_write: Callable[[], str | None] = lambda: None
    media_fp: str = ""        # fingerprint of the exact bytes being delivered ("v<version>:<size>:<sha256 prefix>")

    @property
    def marker(self) -> str:
        """Short idempotency marker embedded in captions/descriptions/tags (not a hashtag)."""
        return ref_marker(self.idempotency_key)

    def save(self, **patch: Any) -> None:
        self.ref.update(patch)
        self.checkpoint(dict(patch))


def ref_marker(key: str) -> str:
    return f"lc-{key[:12]}"


def idempotency_key(dedupe_key: str, destination_id: int, language: str) -> str:
    return hashlib.sha256(f"{dedupe_key}|{destination_id}|{language}".encode()).hexdigest()


@dataclass
class PublishResult:
    state: str
    provider_ref: str = ""
    post_id: str = ""
    url: str = ""
    retry_after: float | None = None
    detail: str = ""
    permanent: bool = False
    keep_reconciling: bool = False   # ambiguous (e.g. every byte sent, answer lost): reconcile again, never re-upload
    # the provider itself reported the uploaded object terminally failed / rejected / deleted (a provider status,
    # never an HTTP error such as 403/404): only this releases a delivery the provider had already accepted
    rejected: bool = False
    # when the PROVIDER says the post became public (UTC seconds), if its answer carries that; None = not reported
    public_at: float | None = None
    # the adapter refused a write because of the freshness policy and guarantees that nothing of this delivery can
    # become public without a further app write (nothing was sent, or Instagram's container was never published)
    expired: bool = False

    def __post_init__(self):
        assert self.state in STATES, self.state


class DestinationAdapter:
    """One instance per destination (settings = that destination's non-secret settings)."""
    provider = "base"
    outcome_label: str | None = None      # e.g. "sandbox": overrides the outcome shown to operators
    oauth_scopes: list[str] = []
    timeout = 60.0
    last_refresh_error: str = ""             # why the last refresh returned nothing (provider error code; no secrets)

    def __init__(self, settings: dict | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 app_settings: Any = None):
        self.settings = dict(settings or {})
        self.transport = transport
        self.app_settings = app_settings

    def client(self, **kw) -> httpx.AsyncClient:
        t = float(self.settings.get("timeout", self.timeout))
        return httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(t, connect=min(t, 15.0)), **kw)

    async def upload(self, job: PublishJob, creds: dict) -> PublishResult:
        raise NotImplementedError

    # True: once the provider accepted an upload it can become public LATER WITHOUT any further app write (YouTube
    # processing, TikTok direct post). False: a separate app-controlled publish step is needed (Instagram
    # media_publish), so refusing that step guarantees no post.
    late_public_after_accept = True

    async def status(self, job: PublishJob, provider_ref: str, creds: dict) -> PublishResult:
        """READ-ONLY state of an accepted upload (dashboard re-check). Providers whose poll() is already read-only
        use it; a provider whose poll can publish (Instagram) overrides this."""
        return await self.poll(job, provider_ref, creds)

    async def poll(self, job: PublishJob, provider_ref: str, creds: dict) -> PublishResult:
        raise NotImplementedError

    async def reconcile(self, job: PublishJob, creds: dict) -> PublishResult | None:
        raise NotImplementedError

    async def account(self, creds: dict) -> dict:
        """{account, scopes, publish_mode: public|private_only|draft_only, notes}"""
        raise NotImplementedError

    async def refresh(self, creds: dict) -> dict | None:
        """Return updated creds (new access token) or None if refresh is impossible."""
        return None

    def upload_scopes(self) -> list[str]:
        """OAuth scopes an upload needs (compared with granted_scopes by the read-only probe)."""
        return []

    async def granted_scopes(self, creds: dict) -> list[str] | None:
        """Scopes the provider says this access token really has (read-only), or None when it cannot tell."""
        return None

    @staticmethod
    def challenge(verifier: str) -> str:
        """PKCE S256 code challenge (base64url, no padding). TikTok overrides with its hex form."""
        import base64
        return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    # OAuth (authorization-code flow + PKCE). app = {client_id, client_secret}
    def authorize_url(self, app: dict, redirect_uri: str, state: str, code_challenge: str) -> str:
        raise NotImplementedError(f"{self.provider} has no OAuth flow; paste tokens instead")

    async def exchange_code(self, app: dict, code: str, redirect_uri: str, code_verifier: str) -> dict:
        raise NotImplementedError


def http_result(r: httpx.Response, what: str, detail: str = "") -> PublishResult | None:
    """Map a non-success HTTP response to a PublishResult (None if the response is a success)."""
    if r.status_code < 400:
        return None
    msg = f"{what}: HTTP {r.status_code} {detail or r.text[:300]}".strip()
    if r.status_code == 401:
        return PublishResult("auth_expired", detail=msg)
    if r.status_code == 429:
        return PublishResult("rate_limited", retry_after=retry_after(r), detail=msg)
    if r.status_code >= 500 or r.status_code in (408, 409):
        return PublishResult("failed", detail=msg)
    return PublishResult("failed", detail=msg, permanent=True)


def oauth_error(r: httpx.Response) -> str:
    """Provider's OAuth error code/description from a failed token response (never echoes tokens)."""
    try:
        j = r.json()
    except ValueError:
        j = {}
    err = j.get("error") if isinstance(j, dict) else None
    if isinstance(err, dict):                                  # Graph API style
        err, desc = err.get("type") or err.get("code"), err.get("message", "")
    else:
        desc = (j.get("error_description") or j.get("message") or "") if isinstance(j, dict) else ""
    return f"HTTP {r.status_code} {err or ''} {desc}".strip()[:240]


def retry_after(r: httpx.Response, default: float = 60.0) -> float:
    """Seconds to wait from Retry-After (seconds or HTTP date) or, failing that, X-RateLimit-Reset."""
    import time
    v = r.headers.get("retry-after")
    if v is not None:
        try:
            return max(0.0, float(v))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(v).timestamp() - time.time())
            except (TypeError, ValueError):
                return default
    reset = r.headers.get("x-ratelimit-reset")
    if reset:
        try:
            x = float(reset)
            return max(0.0, x - time.time()) if x > 1e9 else max(0.0, x)
        except ValueError:
            pass
    return default
