import { useEffect, useRef } from 'react'
import { Terminal } from 'lucide-react'
import { Panel } from './ui.jsx'
import { logTone } from '../lib/format.js'

export default function LogConsole({ lines, running, title = 'Activity log', empty }) {
  const boxRef = useRef(null)
  useEffect(() => {
    const el = boxRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [lines])

  return (
    <Panel className="p-5 flex flex-col min-h-[300px]">
      <div className="flex items-center gap-2 mb-3">
        <Terminal className="w-4 h-4 text-brand" />
        <h2 className="text-xs font-bold uppercase tracking-widest text-muted">{title}</h2>
        <span className="ml-auto font-mono text-[10px] text-muted">
          {running ? 'streaming' : 'idle'}
        </span>
      </div>
      <div ref={boxRef} className="flex-1 overflow-y-auto bg-black/30 rounded-xl p-3 font-mono text-xs leading-relaxed max-h-[440px]">
        {lines.length === 0 ? (
          <span className="text-muted">{empty || 'Idle — runs will stream here live.'}</span>
        ) : (
          lines.map((t, i) => (
            <div key={i} className={logTone(t)}>{t || ' '}</div>
          ))
        )}
      </div>
    </Panel>
  )
}
