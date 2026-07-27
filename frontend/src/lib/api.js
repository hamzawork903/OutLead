// One tiny client for every Flask endpoint. Throws Error(message) on any
// non-2xx so pages can show the backend's own error text (fail loud).

export async function api(path, opts) {
  const res = await fetch(path, opts)
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`)
  return data
}

export const post = (path, body) =>
  api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  })

export const getStats = () => api('/api/outreach/stats')
export const getHealth = () => api('/api/outreach/health')
export const getCampaigns = () => api('/api/outreach/campaigns')
export const getCampaignLeads = (query) =>
  api(`/api/outreach/campaign-leads?query=${encodeURIComponent(query)}`)
export const getLead = (placeKey) => api(`/api/lead/${encodeURIComponent(placeKey)}`)
export const stopLead = (placeKey) => post(`/api/lead/${encodeURIComponent(placeKey)}/stop`)
export const suppressLead = (placeKey) => post(`/api/lead/${encodeURIComponent(placeKey)}/suppress`)
export const getAttention = () => api('/api/outreach/attention')
export const getSequenceConfig = () => api('/api/sequence-config')
export const saveSequenceConfig = (delays) => post('/api/sequence-config', { delays })
export const sendCampaign = (query, count, order) =>
  post('/api/outreach/send-campaign', { query, count, order })
export const getRunStatus = () => api('/api/status')
