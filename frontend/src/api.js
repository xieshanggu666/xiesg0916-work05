const BASE = '/api'

async function req(path, opts = {}) {
  const r = await fetch(BASE + path, opts)
  if (!r.ok) {
    let detail
    try { detail = (await r.json()).detail } catch { detail = r.statusText }
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
    err.status = r.status
    err.detail = detail
    throw err
  }
  return r.json()
}

export const api = {
  listImages: () => req('/images'),
  uploadImage: (name, file) => {
    const fd = new FormData(); fd.append('file', file)
    return req(`/images?name=${encodeURIComponent(name)}`, { method: 'POST', body: fd })
  },
  replaceImage: (id, file) => {
    const fd = new FormData(); fd.append('file', file)
    return req(`/images/${id}/replace`, { method: 'POST', body: fd })
  },
  imageUrl: (id, rev) => `${BASE}/images/${id}/file${rev ? `?revision=${rev}` : ''}`,
  listAnnotations: (params = {}) =>
    req('/annotations?' + new URLSearchParams(params).toString()),
  getAnnotation: (id) => req(`/annotations/${id}`),
  createAnnotation: (imageId, label) =>
    req(`/images/${imageId}/annotations`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label }),
    }),
  maskUrl: (id, v) => `${BASE}/annotations/${id}/versions/${v}/mask.png`,
  saveMask: async (id, blob, author, baseVersion, resolution) => {
    const fd = new FormData(); fd.append('file', blob, 'mask.png')
    const q = new URLSearchParams({ author, base_version: baseVersion })
    if (resolution) q.set('resolution', resolution)
    const r = await fetch(`${BASE}/annotations/${id}/mask?${q}`, { method: 'PUT', body: fd })
    const body = await r.json()
    if (r.status === 409) { const e = new Error('conflict'); e.status = 409; e.detail = body.detail; throw e }
    if (!r.ok) throw new Error(JSON.stringify(body.detail))
    return body
  },
  submit: (id, actor, version) =>
    req(`/annotations/${id}/submit`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ actor, expected_version: version }) }),
  approve: (id, actor, version) =>
    req(`/annotations/${id}/review/approve`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ actor, expected_version: version }) }),
  reject: (id, actor, version, regions) =>
    req(`/annotations/${id}/review/reject`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ actor, expected_version: version, regions }) }),
  migrate: (id, actor) =>
    req(`/annotations/${id}/migrate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ actor }) }),
  invalidate: (id) => req(`/annotations/${id}/invalidate`, { method: 'POST' }),
  listExports: () => req('/exports'),
  getExport: (id) => req(`/exports/${id}`),
  createExport: (name) =>
    req('/exports', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }),
  retryExport: (id) => req(`/exports/${id}/retry`, { method: 'POST' }),
}
