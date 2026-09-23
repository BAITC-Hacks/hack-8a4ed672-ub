let csrf = null;
export let user = null;
export function session(data) { csrf = data?.csrf_token || null; user = data?.user || null; }
export async function api(method, path, body, { signal } = {}) {
  const isForm = body instanceof FormData;
  const r = await fetch(`/api/v1${path}`, {
    method, signal, credentials: 'same-origin',
    headers: { ...(body !== undefined && !isForm ? { 'content-type': 'application/json' } : {}), ...(csrf ? { 'x-csrf-token': csrf } : {}) },
    body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
  });
  const data = r.status === 204 ? null : await r.json().catch(() => null);
  if (!r.ok) {
    if (r.status === 401 && path !== '/auth/login') { session(null); location.hash = '#/login'; }
    const detail = data?.detail;
    throw new Error(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(x => `${x.loc?.slice(1).join('.') || ''}: ${x.msg}`).join('; ') : `Ошибка сервера (${r.status}). Попробуйте ещё раз.`);
  }
  return data;
}
