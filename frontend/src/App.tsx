import { useCallback, useEffect, useState } from 'react'
import { getHealth } from './api'
import { Alerts } from './pages/Alerts'
import { Dashboard } from './pages/Dashboard'
import { Exposure } from './pages/Exposure'
import { Peers } from './pages/Peers'
import { Replay } from './pages/Replay'
import type { Health, RunResult } from './types'

type Page = 'dashboard' | 'exposure' | 'peers' | 'alerts' | 'replay'

const MENU: { id: Page; label: string; group: string }[] = [
  { id: 'dashboard', label: 'Question', group: 'Decision' },
  { id: 'exposure', label: 'Exposure', group: 'Decision' },
  { id: 'peers', label: 'Peers', group: 'Decision' },
  { id: 'alerts', label: 'Alerts', group: 'Watch' },
  { id: 'replay', label: 'Replay', group: 'Watch' },
]

export default function App() {
  const [page, setPage] = useState<Page>('dashboard')
  const [run, setRun] = useState<RunResult | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const [offline, setOffline] = useState(false)
  const remember = useCallback((next: RunResult) => setRun(next), [])

  useEffect(() => {
    getHealth().then(setHealth).catch(() => setOffline(true))
  }, [])

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="mark">A</span>
          <div>
            <strong>AirScen</strong>
            <small>Scenario desk</small>
          </div>
        </div>
        {['Decision', 'Watch'].map((group) => (
          <nav key={group}>
            <p className="nav-label">{group}</p>
            {MENU.filter((item) => item.group === group).map((item) => (
              <button
                key={item.id}
                type="button"
                className={page === item.id ? 'nav active' : 'nav'}
                onClick={() => setPage(item.id)}
              >
                {item.label}
              </button>
            ))}
          </nav>
        ))}
        <p className="side-status">
          {offline ? 'API unreachable' : health ? `API ${health.status} · LLM ${health.llm.mode}` : 'Checking API'}
        </p>
      </aside>
      <main>
        {offline && <p className="banner">Start the API with make api, then reload this page.</p>}
        {page === 'dashboard' && <Dashboard run={run} onRun={remember} />}
        {page === 'exposure' && <Exposure />}
        {page === 'peers' && <Peers run={run} onOpen={() => setPage('dashboard')} />}
        {page === 'alerts' && <Alerts />}
        {page === 'replay' && <Replay onRun={remember} />}
      </main>
    </div>
  )
}
