// Thin REST client. All paths go through the /api dev proxy -> FastAPI.
const BASE = '/api';

async function request(method, path, body) {
  const res = await fetch(BASE + path, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}

const enc = encodeURIComponent;

export const api = {
  health: () => request('GET', '/health'),
  options: () => request('GET', '/options'),
  presets: () => request('GET', '/presets'),
  getPreset: (name) => request('GET', `/presets/${enc(name)}`),
  savePreset: (name, params) => request('PUT', `/presets/${enc(name)}`, params),
  deletePreset: (name) => request('DELETE', `/presets/${enc(name)}`),
  run: (params) => request('POST', '/run', params),
  listArtifacts: () => request('GET', '/artifacts'),
  detail: (stem) => request('GET', `/artifacts/${enc(stem)}`),
  regenerate: (stem, body = {}) => request('POST', `/artifacts/${enc(stem)}/regenerate`, body),
  edit: (stem, newTags) => request('POST', `/artifacts/${enc(stem)}/edit`, { new_tags: newTags }),
  remove: (stem) => request('DELETE', `/artifacts/${enc(stem)}`),
  review: (stem, status, note) => request('POST', `/artifacts/${enc(stem)}/review`, { status, note }),
  imageUrl: (stem) => `${BASE}/image/${enc(stem)}`,
};
