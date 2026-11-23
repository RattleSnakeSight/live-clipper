// Thin fetch wrapper. The server sets the auth cookie; same-origin requests carry it.

export class ApiError extends Error {
  constructor(status, body) {
    super(errorText(body) || `Request failed (${status})`);
    this.status = status;
    this.body = body;
  }
  // Field errors by dotted path from {"detail": {"errors": {id: msg}}} (setup), {"detail": {"errors": [{loc,
  // msg}]}} (formats: loc "a.b"; presets: pydantic loc ["a", "b"]) or a bare pydantic list.
  get fields() {
    const d = this.body && this.body.detail;
    const list = Array.isArray(d) ? d : d && Array.isArray(d.errors) ? d.errors : null;
    if (list) {
      const out = {};
      for (const e of list) out[locPath(e.loc) || '_'] = e.msg;
      return out;
    }
    if (d && d.errors && typeof d.errors === 'object') return d.errors;
    return {};
  }
}

const locPath = (loc) => (Array.isArray(loc) ? loc.filter((x) => x !== 'body').join('.') : String(loc ?? ''));
const listText = (l) => l.map((e) => `${locPath(e.loc)}: ${e.msg}`).join('; ');

function errorText(body) {
  if (!body) return '';
  if (typeof body === 'string') return body;
  const d = body.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return listText(d);
  if (d && typeof d.message === 'string') return d.message;
  if (d && typeof d.detail === 'string') return d.fix ? `${d.detail}. ${d.fix}.` : d.detail; // source sign-in errors
  if (d && Array.isArray(d.errors)) return listText(d.errors);
  if (d && d.errors) return Object.values(d.errors).join(' ');
  return '';
}

let onUnauthorized = () => {};
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }
let onLocked = () => {};
export function setLockedHandler(fn) { onLocked = fn; } // 423: the update gate is closed

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
  if (res.status === 423 && data && data.update) onLocked(data.update);
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
