import { useEffect, useRef, useState } from 'react'
import { ChevronDown, Check } from 'lucide-react'

// Themed dropdown — replaces native <select>, whose open option list is drawn
// by the OS and can't be styled (it rendered light-gray, ignoring the theme).
export default function Select({ value, onChange, options, placeholder = 'Select…' }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  const current = options.find((o) => o.value === value)

  useEffect(() => {
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [])

  return (
    <div className="relative" ref={ref}>
      <button type="button" onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between gap-2 bg-black/30 border border-white/10 rounded-xl py-2.5 px-3 text-sm text-left focus:outline-none focus:border-brand hover:border-white/20">
        <span className={current ? 'text-ink truncate' : 'text-muted'}>
          {current ? current.label : placeholder}
        </span>
        <ChevronDown className={`w-4 h-4 text-muted shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="absolute z-30 mt-1.5 w-full max-h-72 overflow-y-auto rounded-xl border border-white/10 bg-panel shadow-2xl shadow-black/50 py-1">
          {options.length === 0 && (
            <div className="px-3 py-2 text-sm text-muted">No options</div>
          )}
          {options.map((o) => (
            <button key={o.value} type="button"
              onClick={() => { onChange(o.value); setOpen(false) }}
              className={`w-full flex items-center gap-2 px-3 py-2 text-sm text-left transition-colors ${
                o.value === value ? 'bg-brand/10 text-brand' : 'text-ink hover:bg-white/5'}`}>
              <Check className={`w-3.5 h-3.5 shrink-0 ${o.value === value ? 'opacity-100' : 'opacity-0'}`} />
              <span className="truncate">{o.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
