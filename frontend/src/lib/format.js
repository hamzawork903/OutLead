export const fmtDate = (ts) => (ts || '').replace('T', ' ').slice(0, 16)

export function campaignName(query) {
  if (!query) return 'Untitled'
  const parts = String(query).split(/\s+in\s+/i)
  const niche = parts[0].trim().replace(/\b\w/g, (c) => c.toUpperCase())
  return parts[1] ? `${niche} — ${parts[1].trim()}` : niche
}

export function logTone(t) {
  const l = (t || '').toLowerCase()
  if (/error|failed|not working/.test(l)) return 'text-bad'
  if (/\[warn\]|skip|refus/.test(l)) return 'text-warn'
  if (/sent|repli|enqueued|written|qualified/.test(l)) return 'text-good'
  return 'text-ink/70'
}
