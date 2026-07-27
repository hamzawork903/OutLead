import { useEffect, useState } from 'react'
import { Plus, Trash2, Save, Info } from 'lucide-react'
import { Panel, PageHeader, Spinner } from '../components/ui.jsx'
import { getSequenceConfig, saveSequenceConfig } from '../lib/api.js'

const LABELS = ['First email', 'Reminder', 'Final email']
const MAX_DAYS = 60

// Gaps are held as STRINGS so the field can be emptied while typing
// (clamping on every keystroke made backspace impossible — the value
// snapped back to 1). Clamping happens on blur and before saving.
const clampGap = (raw) => Math.max(1, Math.min(MAX_DAYS, parseInt(raw, 10) || 1))
const toDelays = (gaps) =>
  gaps.reduce((acc, g) => [...acc, acc[acc.length - 1] + clampGap(g)], [0])

export default function Sequences() {
  const [gaps, setGaps] = useState(null)      // days between steps, as strings
  const [maxSteps, setMaxSteps] = useState(3)
  const [saved, setSaved] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    getSequenceConfig()
      .then((c) => {
        setGaps(c.delays.slice(1).map((d, i) => String(d - c.delays[i])))
        setMaxSteps(c.max_steps)
      })
      .catch((e) => setError(e.message))
  }, [])

  if (!gaps) return <Spinner />

  const delays = toDelays(gaps)

  const editGap = (i, raw) => {
    // Accept anything digit-ish (including "") so the field can be cleared.
    if (!/^\d{0,2}$/.test(raw)) return
    setGaps(gaps.map((g, idx) => (idx === i ? raw : g)))
    setSaved(null)
  }
  const normalizeGap = (i) =>
    setGaps(gaps.map((g, idx) => (idx === i ? String(clampGap(g)) : g)))

  const addStep = () => { setGaps([...gaps, '3']); setSaved(null) }
  const removeStep = (stepIdx) => {
    setGaps(gaps.filter((_, i) => i !== stepIdx - 1)); setSaved(null)
  }

  const save = async () => {
    setError(null)
    const clean = gaps.map((g) => String(clampGap(g)))
    setGaps(clean)
    try {
      const c = await saveSequenceConfig(toDelays(clean))
      setGaps(c.delays.slice(1).map((d, i) => String(d - c.delays[i])))
      setSaved('Saved — new timing applies to every future send.')
    } catch (e) { setError(e.message) }
  }

  return (
    <>
      <PageHeader title="Sequences" subtitle="Control when each email in the sequence goes out.">
        <button onClick={save}
          className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-brand text-white text-sm font-bold hover:brightness-110">
          <Save className="w-4 h-4" /> Save sequence
        </button>
      </PageHeader>

      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,540px)_1fr] gap-4 items-start">
        <div className="space-y-4">
          {saved && <div className="text-sm text-good">{saved}</div>}
          {error && <div className="text-sm text-bad">{error}</div>}

          <div className="space-y-0">
            {delays.map((day, i) => (
              <div key={i}>
                {i > 0 && (
                  <div className="flex items-center gap-3 py-1 ml-6 border-l-2 border-dashed border-white/15 pl-6 my-1">
                    <span className="text-xs text-muted">wait</span>
                    <input
                      type="text" inputMode="numeric" value={gaps[i - 1]}
                      onChange={(e) => editGap(i - 1, e.target.value)}
                      onBlur={() => normalizeGap(i - 1)}
                      onFocus={(e) => e.target.select()}
                      className="w-16 bg-black/30 border border-white/10 rounded-lg py-1 px-2 text-sm text-center focus:outline-none focus:border-brand" />
                    <span className="text-xs text-muted">
                      day{clampGap(gaps[i - 1]) === 1 ? '' : 's'}
                    </span>
                  </div>
                )}
                <Panel className="p-4 flex items-center gap-4">
                  <span className="w-9 h-9 rounded-full bg-brand/10 border border-brand/30 text-brand font-bold flex items-center justify-center shrink-0">
                    {i + 1}
                  </span>
                  <div className="flex-1">
                    <div className="font-semibold text-sm">{LABELS[i] || `Step ${i + 1}`}</div>
                    <div className="font-mono text-[11px] text-muted">Day {day}</div>
                  </div>
                  {i > 0 && (
                    <button onClick={() => removeStep(i)} title="Remove step"
                      className="text-muted hover:text-bad">
                      <Trash2 className="w-4 h-4" />
                    </button>
                  )}
                </Panel>
              </div>
            ))}
          </div>

          {delays.length < maxSteps && (
            <button onClick={addStep}
              className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl border border-white/15 text-sm font-semibold text-muted hover:text-ink hover:bg-white/5">
              <Plus className="w-4 h-4" /> Add step
            </button>
          )}
        </div>

        <div className="space-y-4">
          <Panel className="p-4 flex gap-3 text-sm text-muted">
            <Info className="w-4 h-4 mt-0.5 shrink-0 text-brand" />
            <span>
              Email content is written automatically per lead by the AI — this
              page only controls timing and how many of the {maxSteps} written
              steps are used. Changes apply to sends from now on;
              already-scheduled dates aren't rewritten.
            </span>
          </Panel>
          <Panel className="p-4">
            <h3 className="text-xs font-bold uppercase tracking-widest text-muted mb-2">
              What a lead experiences
            </h3>
            <ul className="text-sm text-muted space-y-1.5">
              {delays.map((d, i) => (
                <li key={i} className="flex gap-2">
                  <span className="font-mono text-brand shrink-0">Day {d}</span>
                  <span>{LABELS[i] || `Step ${i + 1}`}</span>
                </li>
              ))}
              <li className="flex gap-2 pt-1.5 border-t border-white/10 mt-1.5">
                <span className="text-good shrink-0">Any reply</span>
                <span>stops the sequence immediately</span>
              </li>
            </ul>
          </Panel>
        </div>
      </div>
    </>
  )
}
