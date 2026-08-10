import { useEffect, useState } from 'react'
import { X, Star, Globe, OctagonX, Ban } from 'lucide-react'
import { StatusPill, VerifyBadge, Spinner } from './ui.jsx'
import { getLead, stopLead, suppressLead } from '../lib/api.js'
import { fmtDate } from '../lib/format.js'

const STEP_LABEL = ['Step 1 — first email (day 0)', 'Step 2 — reminder', 'Step 3 — goodbye']

export default function LeadDrawer({ placeKey, onClose, onChanged }) {
  const [lead, setLead] = useState(null)
  const [tab, setTab] = useState('emails')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    setLead(null)
    getLead(placeKey).then(setLead).catch((e) => setError(e.message))
  }, [placeKey])

  // reviews_text is stored as a JSON string; tolerate both shapes.
  const reviews = (() => {
    const raw = lead?.reviews_text
    if (!raw) return []
    if (Array.isArray(raw)) return raw
    try { return JSON.parse(raw) || [] } catch { return [] }
  })()

  const act = async (fn, confirmText) => {
    if (!window.confirm(confirmText)) return
    setBusy(true)
    try {
      await fn(placeKey)
      const fresh = await getLead(placeKey)
      setLead(fresh)
      if (onChanged) onChanged()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60" />
      <div
        className="absolute right-0 top-0 h-full w-full max-w-md bg-panel border-l border-white/10 p-6 overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-4">
          <h2 className="text-lg font-bold pr-4">{lead ? lead.name : 'Loading…'}</h2>
          <button onClick={onClose} className="text-muted hover:text-ink"><X className="w-5 h-5" /></button>
        </div>

        {error && <div className="text-sm text-bad mb-3">{error}</div>}
        {!lead ? (
          <Spinner />
        ) : (
          <>
            <div className="space-y-1.5 text-sm mb-4">
              <div className="flex items-center gap-2">
                <VerifyBadge status={lead.email_status} />
                <span className="text-ink">{lead.email || 'no email'}</span>
              </div>
              <div className="flex items-center gap-3 text-muted text-xs">
                {lead.rating != null && (
                  <span className="inline-flex items-center gap-1">
                    <Star className="w-3 h-3 text-warn" /> {lead.rating} ({lead.reviews ?? 0})
                  </span>
                )}
                {lead.website && (
                  <a href={lead.website} target="_blank" rel="noreferrer"
                     className="inline-flex items-center gap-1 hover:text-ink">
                    <Globe className="w-3 h-3" /> website
                  </a>
                )}
                {lead.status && <StatusPill status={lead.status} />}
              </div>
              {lead.stop_reason && (
                <div className="text-xs text-bad">stopped: {lead.stop_reason}</div>
              )}
            </div>

            <div className="flex gap-1 p-1 rounded-xl bg-black/25 border border-white/10 mb-4 w-fit">
              {['emails', 'reviews', 'history'].map((t) => (
                <button key={t} onClick={() => setTab(t)}
                  className={`px-3 py-1.5 rounded-lg text-xs font-semibold capitalize ${
                    tab === t ? 'bg-brand text-white' : 'text-muted hover:text-ink'}`}>
                  {t}
                </button>
              ))}
            </div>

            {tab === 'emails' && (
              lead.emails?.steps ? (
                <div className="space-y-3">
                  <div className="text-xs text-muted">
                    Greeting: <span className="text-ink font-medium">
                      Hi {lead.emails.greeting_name || ','}{lead.emails.greeting_name ? ',' : ''}
                    </span>
                  </div>
                  {lead.emails.steps.map((s, i) => (
                    <div key={i} className="bg-white/5 border border-white/10 rounded-xl p-4">
                      <div className="text-[10px] uppercase tracking-widest text-muted mb-1">{STEP_LABEL[i]}</div>
                      <div className="text-sm font-semibold mb-2">{s.subject}</div>
                      <div className="text-xs text-ink/80 whitespace-pre-wrap leading-relaxed">{s.body}</div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-sm text-muted">
                  No emails generated yet — this lead is skipped by the sender
                  until the AI writes its sequence.
                </div>
              )
            )}

            {tab === 'reviews' && (
              reviews.length ? (
                <div className="space-y-2">
                  <p className="text-xs text-muted">
                    What the AI writes from — captured from Google Maps.
                  </p>
                  {reviews.map((r, i) => (
                    <div key={i} className="bg-white/5 border border-white/10 rounded-xl p-3">
                      <div className="flex items-center gap-2 mb-1 text-xs">
                        <span className="text-warn">
                          {r.stars ? '★'.repeat(Math.round(r.stars)) : '—'}
                        </span>
                        {r.when && <span className="text-muted">{r.when}</span>}
                        {r.owner_replied && (
                          <span className="ml-auto text-[10px] px-1.5 py-0.5 rounded bg-good/15 text-good">
                            owner replied
                          </span>
                        )}
                      </div>
                      <div className="text-xs text-ink/80 leading-relaxed">{r.text}</div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-sm text-muted">
                  No reviews captured. Turn on “Customer reviews” in the scraper's
                  field list before scraping to collect them.
                </div>
              )
            )}

            {tab === 'history' && (
              lead.history?.length ? (
                <div className="space-y-2">
                  {lead.history.map((h, i) => (
                    <div key={i} className="bg-white/5 border border-white/10 rounded-xl p-3 text-xs">
                      <div className="flex justify-between">
                        <span className="font-semibold">step {h.step + 1} · {h.status}{h.dry_run ? ' (dry run)' : ''}</span>
                        <span className="font-mono text-muted">{fmtDate(h.sent_at)}</span>
                      </div>
                      {h.subject && <div className="text-muted mt-1">{h.subject}</div>}
                      {h.error && <div className="text-bad mt-1">{h.error}</div>}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-sm text-muted">Nothing sent to this lead yet.</div>
              )
            )}

            <div className="flex gap-2 mt-6 pt-4 border-t border-white/10">
              <button
                disabled={busy || lead.status !== 'active'}
                onClick={() => act(stopLead, 'Stop this lead’s sequence? No more emails will be sent to them.')}
                className="flex-1 inline-flex items-center justify-center gap-2 px-3 py-2 rounded-xl border border-bad/40 text-bad text-xs font-semibold hover:bg-bad/10 disabled:opacity-40">
                <OctagonX className="w-4 h-4" /> Stop sequence
              </button>
              <button
                disabled={busy || !lead.email}
                onClick={() => act(suppressLead, 'Suppress this email permanently? It can never be contacted again.')}
                className="flex-1 inline-flex items-center justify-center gap-2 px-3 py-2 rounded-xl border border-white/15 text-muted text-xs font-semibold hover:bg-white/5 disabled:opacity-40">
                <Ban className="w-4 h-4" /> Suppress email
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
