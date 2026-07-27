import { useEffect, useRef, useState } from 'react'
import { getRunStatus } from './api.js'

// The one SSE implementation. Because this is a SPA, moving between pages
// doesn't kill the stream's usefulness — but a full browser reload can, so
// on mount we ask /api/status whether a run of interest is already going
// and reattach (the server replays the whole buffered run).
export function useStream(kinds) {
  const [lines, setLines] = useState([])
  const [running, setRunning] = useState(false)
  const esRef = useRef(null)

  const attach = (onDone) => {
    if (esRef.current) esRef.current.close()
    setRunning(true)
    const es = new EventSource('/stream')
    es.addEventListener('log', (e) =>
      setLines((prev) => [...prev.slice(-499), e.data]))
    es.addEventListener('done', () => {
      es.close()
      setRunning(false)
      if (onDone) onDone()
    })
    es.onerror = () => { es.close(); setRunning(false) }
    esRef.current = es
    return es
  }

  useEffect(() => {
    let cancelled = false
    getRunStatus().then((s) => {
      if (!cancelled && s.running && (!kinds || kinds.includes(s.kind))) {
        setLines(['↻ Reconnected to a run already in progress…'])
        attach()
      }
    }).catch(() => {})
    return () => { cancelled = true; if (esRef.current) esRef.current.close() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const clear = () => setLines([])
  return { lines, running, attach, clear }
}
