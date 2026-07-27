import { useEffect, useMemo, useState } from 'react'
import { Send } from 'lucide-react'
import { Panel, PageHeader } from '../components/ui.jsx'
import Select from '../components/Select.jsx'
import LogConsole from '../components/LogConsole.jsx'
import { getCampaigns, getStats, sendCampaign } from '../lib/api.js'
import { campaignName } from '../lib/format.js'
import { useStream } from '../lib/useStream.js'

const ORDERS = [
  ['latest', 'Latest first'],
  ['oldest', 'Oldest first'],
  ['score', 'Highest score first'],
]

export default function Sender() {
  const [campaigns, setCampaigns] = useState([])
  const [stats, setStats] = useState(null)
  const [query, setQuery] = useState('')
  const [count, setCount] = useState('')
  const [order, setOrder] = useState('latest')
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState(null)
  const { lines, running, attach, clear } = useStream(['outreach_campaign'])

  const refresh = () => {
    getCampaigns().then(setCampaigns).catch(() => {})
    getStats().then(setStats).catch(() => {})
  }
  useEffect(refresh, [])

  const selected = useMemo(
    () => campaigns.find((c) => c.query === query), [campaigns, query])
  const capLeft = stats ? Math.max(0, stats.daily_cap - stats.sent_today) : null
  const n = parseInt(count, 10) || 0
  const tooMany = selected && n > selected.due_now
  const overCap = capLeft != null && n > capLeft
  const ready = selected && n >= 1 && !tooMany && !overCap && !running

  const fire = async () => {
    setConfirming(false)
    setError(null)
    clear()
    try {
      await sendCampaign(query, n, order)
      attach(refresh)
    } catch (e) {
      setError(e.message)
    }
  }

  return (
    <>
      <PageHeader title="Email Sender"
        subtitle="Pick a campaign, choose how many, confirm — real emails go out." />

      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,540px)_1fr] gap-4 items-start">
        <Panel className="p-5 space-y-4">
          <div>
            <label className="text-xs font-semibold text-muted uppercase tracking-widest">1 · Campaign</label>
            <div className="mt-2">
              <Select value={query} onChange={setQuery} placeholder="Select a campaign…"
                options={campaigns.filter((c) => c.due_now > 0).map((c) => ({
                  value: c.query,
                  label: `${campaignName(c.query)} (${c.due_now} ready)`,
                }))} />
            </div>
          </div>

          <div>
            <label className="text-xs font-semibold text-muted uppercase tracking-widest">2 · How many</label>
            <input type="number" min="1" value={count}
              onChange={(e) => setCount(e.target.value)} placeholder="e.g. 20"
              className={`mt-2 w-full bg-black/30 border rounded-xl py-2.5 px-3 text-sm focus:outline-none ${
                tooMany || overCap ? 'border-bad text-bad' : 'border-white/10 focus:border-brand'}`} />
            <p className={`text-xs mt-1.5 ${tooMany || overCap ? 'text-bad' : 'text-muted'}`}>
              {tooMany ? `Only ${selected.due_now} ready in this campaign.`
                : overCap ? `Daily cap: only ${capLeft} sends left today.`
                : stats ? `${capLeft} of ${stats.daily_cap} daily sends left today.` : ''}
            </p>
          </div>

          <div>
            <label className="text-xs font-semibold text-muted uppercase tracking-widest">3 · Send order</label>
            <div className="flex gap-1 p-1 rounded-xl bg-black/25 border border-white/10 mt-2 w-fit">
              {ORDERS.map(([val, label]) => (
                <button key={val} onClick={() => setOrder(val)}
                  className={`px-3 py-1.5 rounded-lg text-xs font-semibold ${
                    order === val ? 'bg-brand text-white' : 'text-muted hover:text-ink'}`}>
                  {label}
                </button>
              ))}
            </div>
          </div>

          {selected && n >= 1 && !tooMany && !overCap && (
            <div className="bg-brand/5 border border-brand/20 rounded-xl p-3 text-sm">
              You're about to send <b>{n} REAL email{n === 1 ? '' : 's'}</b> to{' '}
              <b>{campaignName(query)}</b> ({ORDERS.find(([v]) => v === order)[1].toLowerCase()}).
            </div>
          )}
          {error && <div className="text-sm text-bad">{error}</div>}

          <button disabled={!ready} onClick={() => setConfirming(true)}
            className="w-full inline-flex items-center justify-center gap-2 py-3 rounded-xl bg-brand text-white font-bold text-sm hover:brightness-110 disabled:opacity-40 disabled:cursor-not-allowed">
            <Send className="w-4 h-4" /> Send Emails
          </button>
        </Panel>

        <LogConsole lines={lines} running={running} title="Sending progress"
          empty="Send progress will stream here." />
      </div>

      {confirming && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" onClick={() => setConfirming(false)}>
          <div className="absolute inset-0 bg-black/70" />
          <Panel className="relative p-6 max-w-sm w-full mx-4" onClick={(e) => e.stopPropagation()}>
            <h3 className="font-bold mb-2">Are you sure?</h3>
            <p className="text-sm text-muted mb-5">
              {n} real email{n === 1 ? '' : 's'} will be sent to {campaignName(query)}. This cannot be undone.
            </p>
            <div className="flex gap-2">
              <button onClick={() => setConfirming(false)}
                className="flex-1 py-2.5 rounded-xl border border-white/15 text-sm font-semibold text-muted hover:bg-white/5">
                Cancel
              </button>
              <button onClick={fire}
                className="flex-1 py-2.5 rounded-xl bg-brand text-white text-sm font-bold hover:brightness-110">
                Yes, send
              </button>
            </div>
          </Panel>
        </div>
      )}
    </>
  )
}
