// Thin fetch wrapper. The server sets the auth cookie; same-origin requests carry it.

export class ApiError extends Error {
  constructor(status, body) {
    super(errorText(body) || `Request failed (${status})`);
    this.status = status;
    this.body = body;
  }
  // Field errors from either {"detail": {"errors": {id: msg}}} or pydantic [{loc, msg}].
  get fields() {
    const d = this.body && this.body.detail;
    if (d && d.errors) return d.errors;
    if (Array.isArray(d)) {
      const out = {};
      for (const e of d) out[(e.loc || []).filter((x) => x !== 'body').join('.') || '_'] = e.msg;
      return out;
    }
    return {};
  }
}

function errorText(body) {
  if (!body) return '';
  if (typeof body === 'string') return body;
  const d = body.detail;
  if (typeof d === 'string') return d;
  if (d && typeof d.message === 'string') return d.message;
  if (Array.isArray(d)) return d.map((e) => `${(e.loc || []).filter((x) => x !== 'body').join('.')}: ${e.msg}`).join('; ');
  if (d && d.errors) return Object.values(d.errors).join(' ');
  return '';
}

let onUnauthorized = () => {};
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

export async function api(path, { method = 'GET', body, raw = false } = {}) {
  const opts = { method, credentials: 'same-origin', headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`/api${path}`, opts);
  if (res.status === 401) onUnauthorized();
  const type = res.headers.get('content-type') || '';
  const data = raw || !type.includes('json') ? await res.text() : await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

export const get = (p) => api(p);
export const put = (p, body) => api(p, { method: 'PUT', body });
export const post = (p, body) => api(p, { method: 'POST', body: body ?? {} });
export const del = (p) => api(p, { method: 'DELETE' });

// Lists come back as {key: [...]} from most services; accept a bare array too.
export function list(data, key) {
  if (Array.isArray(data)) return data;
  return (data && data[key]) || [];
}

export async function appState(key) {
  return (await get(`/app-state/${encodeURIComponent(key)}`)).value;
}
export function setAppState(key, value) {
  return put(`/app-state/${encodeURIComponent(key)}`, { value });
}
