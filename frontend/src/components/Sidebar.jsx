import { useEffect, useState } from 'react'
import { NavLink } from 'react-router-dom'
import { LayoutDashboard, Send, MailCheck, GitBranch, ArrowLeft } from 'lucide-react'
import { getHealth, getCampaigns } from '../lib/api.js'

const NAV = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/campaigns', label: 'Campaigns', icon: Send, badge: true },
  { to: '/sender', label: 'Email Sender', icon: MailCheck },
  { to: '/sequences', label: 'Sequences', icon: GitBranch },
]

export default function Sidebar() {
  const [dryRun, setDryRun] = useState(null)
  const [campaignCount, setCampaignCount] = useState(null)

  useEffect(() => {
    getHealth().then((h) => setDryRun(h.dry_run)).catch(() => {})
    getCampaigns().then((c) => setCampaignCount(c.length)).catch(() => {})
  }, [])

  return (
    <aside className="fixed left-0 top-0 h-full w-60 border-r border-white/10 bg-panel/90 backdrop-blur-md p-4 flex flex-col justify-between z-40">
      <div className="space-y-6">
        <div className="flex items-center gap-2 px-2 py-3 border-b border-white/10">
          {/* Product name — rename here to brand your own install. */}
          <span className="text-xl font-extrabold italic tracking-tight text-ink">
            Out<span className="text-brand not-italic">Lead</span>
          </span>
          <span className="text-[10px] font-mono tracking-wider text-muted uppercase bg-white/5 border border-white/10 px-1.5 py-0.5 rounded ml-auto">
            OUTREACH
          </span>
        </div>
        <nav className="space-y-1.5">
          {NAV.map(({ to, label, icon: Icon, badge, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-sm transition-all ${
                  isActive
                    ? 'bg-brand/10 text-brand border border-brand/20'
                    : 'text-muted hover:text-ink hover:bg-white/5 border border-transparent'
                }`
              }
            >
              <Icon className="w-4 h-4" /> {label}
              {badge && campaignCount != null && (
                <span className="ml-auto text-xs px-2 py-0.5 rounded-full bg-white/10 text-ink font-mono">
                  {campaignCount}
                </span>
              )}
            </NavLink>
          ))}
        </nav>
      </div>
      <div className="space-y-3 pt-4 border-t border-white/10">
        <a href="/" className="flex items-center gap-2 text-xs text-muted hover:text-ink transition-colors px-2">
          <ArrowLeft className="w-3.5 h-3.5" /> Back to Scraper
        </a>
        <div className="flex items-center justify-between bg-white/5 p-2.5 rounded-xl border border-white/10">
          <span className="text-xs text-muted font-mono">Engine:</span>
          {dryRun === null ? (
            <span className="text-xs font-mono text-muted">…</span>
          ) : dryRun ? (
            <span className="inline-flex items-center gap-1.5 text-xs font-mono font-semibold px-2.5 py-0.5 rounded-full bg-white/10 text-muted">
              <span className="w-1.5 h-1.5 rounded-full bg-muted" />DRY RUN
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 text-xs font-mono font-semibold px-2.5 py-0.5 rounded-full bg-brand/15 text-brand border border-brand/30">
              <span className="w-1.5 h-1.5 rounded-full bg-brand animate-pulse" />LIVE MODE
            </span>
          )}
        </div>
      </div>
    </aside>
  )
}
