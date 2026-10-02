const BASE = '/api'

async function request(path, options = {}) {
  const resp = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      const body = await resp.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch { /* ignore */ }
    const err = new Error(detail)
    err.status = resp.status
    throw err
  }
  return resp.json()
}

export const api = {
  meta: () => request('/meta'),
  recipes: () => request('/recipes'),
  recipe: (id) => request(`/recipes/${id}`),
  version: (id) => request(`/versions/${id}`),
  graph: (id) => request(`/versions/${id}/graph`),
  diff: (a, b) => request(`/diff/${a}/${b}`),
  exportUrl: (id, format) => `${BASE}/versions/${id}/export?format=${format}`,
  batches: () => request('/batches'),
  materials: () => request('/materials'),
  approve: (vid, reviewerId) =>
    request(`/versions/${vid}/approve`, { method: 'POST', body: JSON.stringify({ reviewer_id: reviewerId }) }),
  recall: (batchId, reason) =>
    request(`/batches/${batchId}/recall`, { method: 'POST', body: JSON.stringify({ reason }) }),
  createVersion: (payload) =>
    request('/versions', { method: 'POST', body: JSON.stringify(payload) }),
  createProposal: (payload) =>
    request('/proposals', { method: 'POST', body: JSON.stringify(payload) }),
  applyProposal: (id) => request(`/proposals/${id}/apply`, { method: 'POST' }),
  rejectProposal: (id) => request(`/proposals/${id}/reject`, { method: 'POST' }),
}
