import { useEffect, useState } from 'react'
import { BellRing, MailOpen, OctagonX } from 'lucide-react'
import { Panel, PageHeader } from '../components/ui.jsx'
import LogConsole from '../components/LogConsole.jsx'
import { getStats, getHealth, getAttention } from '../lib/api.js'
import { fmtDate } from '../lib/format.js'
import { useStream } from '../lib/useStream.js'

const KPIS = [
  ['Qualified', (s) => s.qualified, ''],
  ['Active sequences', (s) => s.active_sequences, 'text-brand'],
  ['Sent today', (s) => `${s.sent_today} / ${s.daily_cap}`, ''],
  ['Replies', (s) => s.replied, 'text-good'],
  ['Completed', (s) => s.completed, ''],
  ['Suppressed', (s) => s.suppressed, 'text-bad'],
]

export default function Dashboard() {
  const [stats, setStats] = useState(null)
  const [health, setHealth] = useState(null)
  const [attention, setAttention] = useState([])
  const { lines, running } = useStream(null)   // any run kind shows here

  useEffect(() => {
    const load = () => {
      getStats().then(setStats).catch(() => {})
      getAttention().then(setAttention).catch(() => {})
    }
    load()
    getHealth().then(setHealth).catch(() => {})
    const t = setInterval(load, 20000)
    return () => clearInterval(t)
  }, [])

  return (
    <>
      <PageHeader title="Dashboard" subtitle="Everything the outreach engine is doing, at a glance.">
        {health && (
          <div className="flex gap-2">
            <span className="font-mono text-xs px-3 py-1.5 rounded-full bg-white/5 border border-white/10 text-muted">
              AI budget: <span className="text-ink">${health.llm_spent_usd.toFixed(2)} / ${health.llm_budget_usd.toFixed(2)}</span>
              {!health.llm_key_set && <span className="text-bad"> (no key)</span>}
            </span>
            <span className="font-mono text-xs px-3 py-1.5 rounded-full bg-white/5 border border-white/10 text-muted">
              Verifier credits: <span className="text-ink">{health.reoon_credits ?? '—'}</span>
            </span>
          </div>
        )}
      </PageHeader>

      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3 mb-6">
        {KPIS.map(([label, fn, cls]) => (
          <Panel key={label} className="p-4">
            <div className="text-[10px] uppercase tracking-widest font-semibold text-muted">{label}</div>
            <div className={`text-2xl font-extrabold mt-1 ${cls}`}>{stats ? fn(stats) : '…'}</div>
            {label === 'Sent today' && stats && (
              <div className="h-1 mt-2 rounded-full bg-white/10 overflow-hidden">
                <div className="h-full bg-brand" style={{ width: `${Math.min(100, (stats.sent_today / stats.daily_cap) * 100)}%` }} />
              </div>
            )}
          </Panel>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_360px] gap-4">
        <LogConsole lines={lines} running={running}
          empty="Idle — sends and passes will stream here live." />

        <Panel className="p-5">
          <div className="flex items-center gap-2 mb-3">
            <BellRing className="w-4 h-4 text-brand" />
            <h2 className="text-xs font-bold uppercase tracking-widest text-muted">Needs attention</h2>
          </div>
          <div className="space-y-2 overflow-y-auto max-h-[440px]">
            {attention.length === 0 ? (
              <span className="text-sm text-muted">Nothing yet.</span>
            ) : (
              attention.map((r) => (
                <div key={r.place_key + r.updated_at} className="flex items-start gap-2.5 bg-white/5 border border-white/10 rounded-xl p-3">
                  {r.status === 'replied'
                    ? <MailOpen className="w-4 h-4 mt-0.5 text-good shrink-0" />
                    : <OctagonX className="w-4 h-4 mt-0.5 text-bad shrink-0" />}
                  <div className="min-w-0">
                    <div className="text-sm font-medium truncate">{r.name}</div>
                    <div className="text-xs text-muted truncate">
                      {r.email} · {r.status === 'replied'
                        ? 'replied — read it in your inbox'
                        : `stopped: ${r.stop_reason || ''}`}
                    </div>
                    <div className="font-mono text-[10px] text-muted/70 mt-0.5">{fmtDate(r.updated_at)}</div>
                  </div>
                </div>
              ))
            )}
          </div>
        </Panel>
      </div>
    </>
  )
}
