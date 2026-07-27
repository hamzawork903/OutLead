// Small shared atoms used across pages.

export const Panel = ({ className = '', children, ...rest }) => (
  <section className={`bg-panel/85 border border-white/10 rounded-2xl ${className}`} {...rest}>
    {children}
  </section>
)

export const PageHeader = ({ title, subtitle, children }) => (
  <div className="flex items-start justify-between mb-6 flex-wrap gap-3">
    <div>
      <h1 className="text-2xl font-extrabold tracking-tight">{title}</h1>
      {subtitle && <p className="text-sm text-muted mt-1">{subtitle}</p>}
    </div>
    {children}
  </div>
)

const PILL = {
  active: 'bg-[#3b82f6]/15 text-[#7cb0ff]',
  replied: 'bg-good/15 text-good',
  stopped: 'bg-bad/15 text-bad',
  completed: 'bg-white/10 text-muted',
}

export const StatusPill = ({ status }) => (
  <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold uppercase ${PILL[status] || 'bg-white/10 text-muted'}`}>
    {status}
  </span>
)

// Mailbox verification verdict for an email (Reoon + MX pipeline).
export const VerifyBadge = ({ status }) =>
  status === 'valid' ? (
    <span className="text-good" title="inbox verified">✓</span>
  ) : status === 'risky' ? (
    <span className="text-warn" title="catch-all / risky">~</span>
  ) : (
    <span className="text-muted" title="unverified">?</span>
  )

export const Spinner = () => (
  <span className="inline-block w-4 h-4 rounded-full border-2 border-white/20 border-t-brand animate-spin align-middle" />
)
