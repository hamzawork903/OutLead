import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ChevronLeft, Users } from 'lucide-react'
import { Panel, PageHeader, StatusPill, VerifyBadge, Spinner } from '../components/ui.jsx'
import LeadDrawer from '../components/LeadDrawer.jsx'
import { getCampaigns, getCampaignLeads } from '../lib/api.js'
import { campaignName, fmtDate } from '../lib/format.js'

// Stacked progress bar: sent / active / replied / stopped shares of total.
function ShareBar({ c }) {
  const seg = (n, cls) => (n > 0
    ? <div className={cls} style={{ width: `${(n / c.total) * 100}%` }} /> : null)
  return (
    <div className="h-1.5 rounded-full bg-white/10 overflow-hidden flex">
      {seg(c.replied, 'bg-good')}
      {seg(c.completed, 'bg-white/40')}
      {seg(c.active, 'bg-[#3b82f6]')}
      {seg(c.stopped, 'bg-bad')}
    </div>
  )
}

export default function Campaigns() {
  const [campaigns, setCampaigns] = useState(null)
  const [leads, setLeads] = useState(null)
  const [drawerKey, setDrawerKey] = useState(null)
  const [params, setParams] = useSearchParams()
  const selected = params.get('q')

  const loadCampaigns = () => getCampaigns().then(setCampaigns).catch(() => {})
  useEffect(() => { loadCampaigns() }, [])
  useEffect(() => {
    setLeads(null)
    if (selected) getCampaignLeads(selected).then(setLeads).catch(() => setLeads([]))
  }, [selected])

  if (selected) {
    const c = campaigns?.find((x) => x.query === selected)
    return (
      <>
        <button onClick={() => setParams({})}
          className="inline-flex items-center gap-1 text-xs text-muted hover:text-ink mb-3">
          <ChevronLeft className="w-4 h-4" /> All campaigns
        </button>
        <PageHeader title={campaignName(selected)}
          subtitle={c ? `${c.total} leads · ${c.active} active · ${c.replied} replied · ${c.due_now} ready to send` : ''} />
        <Panel className="overflow-hidden">
          {!leads ? (
            <div className="p-8"><Spinner /></div>
          ) : (
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="text-muted uppercase tracking-widest text-[10px] border-b border-white/10">
                  <th className="px-4 py-3">Business</th>
                  <th className="px-4 py-3">Email</th>
                  <th className="px-4 py-3">Score</th>
                  <th className="px-4 py-3">Step</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Next send</th>
                </tr>
              </thead>
              <tbody>
                {leads.map((l) => (
                  <tr key={l.place_key} onClick={() => setDrawerKey(l.place_key)}
                    className="border-b border-white/5 hover:bg-white/5 cursor-pointer">
                    <td className="px-4 py-3 font-medium max-w-[220px] truncate">{l.name}</td>
                    <td className="px-4 py-3 text-muted max-w-[220px] truncate">
                      <VerifyBadge status={l.email_status} /> {l.email}
                    </td>
                    <td className="px-4 py-3">{l.quality_score ?? '—'}</td>
                    <td className="px-4 py-3 font-mono text-xs">{l.current_step + 1}/3</td>
                    <td className="px-4 py-3"><StatusPill status={l.status} /></td>
                    <td className="px-4 py-3 font-mono text-xs text-muted">
                      {l.status === 'active' ? fmtDate(l.next_send_at) : (l.stop_reason || '—')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
        {drawerKey && (
          <LeadDrawer placeKey={drawerKey} onClose={() => setDrawerKey(null)}
            onChanged={() => { getCampaignLeads(selected).then(setLeads); loadCampaigns() }} />
        )}
      </>
    )
  }

  return (
    <>
      <PageHeader title="Campaigns" subtitle="One campaign per scrape — click one to see every lead inside it." />
      {!campaigns ? (
        <Spinner />
      ) : campaigns.length === 0 ? (
        <Panel className="p-8 text-muted text-sm">No campaigns yet — run a scrape with “Sync with outreach”.</Panel>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {campaigns.map((c) => (
            <Panel key={c.query} className="p-5 cursor-pointer hover:border-brand/40 transition-colors"
              onClick={() => setParams({ q: c.query })}>
              <div className="flex items-start justify-between gap-2 mb-1">
                <h3 className="font-bold leading-tight">{campaignName(c.query)}</h3>
                <span className="inline-flex items-center gap-1 text-xs text-muted shrink-0">
                  <Users className="w-3.5 h-3.5" /> {c.total}
                </span>
              </div>
              <div className="font-mono text-[10px] text-muted/70 mb-3">since {fmtDate(c.created_at)}</div>
              <ShareBar c={c} />
              <div className="flex flex-wrap gap-x-4 gap-y-1 mt-3 text-xs">
                <span className="text-[#7cb0ff]">{c.active} active</span>
                <span className="text-good font-semibold">{c.replied} replied</span>
                <span className="text-muted">{c.stopped} stopped</span>
                <span className="ml-auto text-ink font-semibold">{c.due_now} ready</span>
              </div>
            </Panel>
          ))}
        </div>
      )}
    </>
  )
}
