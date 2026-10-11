import { useEffect, useState } from 'react'
import { ApiError, getMarkets } from '../api'
import { num } from '../format'
import type { MarketView } from '../types'

export function Exposure() {
  const [rows, setRows] = useState<MarketView[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    Promise.all([getMarkets('fuel'), getMarkets('rates')])
      .then(([fuel, rates]) => {
        const relevant = [...fuel, ...rates].filter((row) => row.label?.relevant)
        relevant.sort((a, b) => (b.trust?.score ?? -1) - (a.trust?.score ?? -1))
        setRows(relevant)
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Markets are unavailable.'))
  }, [])

  const groups = ['fuel', 'rates'] as const

  return (
    <>
      <header className="page-head">
        <div>
          <p className="kicker">Exposure</p>
          <h1>Markets that move the answer</h1>
          <p className="lede">Fuel and Fed contracts labeled as relevant. Trust uses spread, depth, volume and time to close. A score under 0.40, or a quote older than five minutes, does not enter a run.</p>
        </div>
      </header>
      {error && <p className="banner">{error}</p>}
      {groups.map((factor) => {
        const items = rows.filter((row) => row.label?.factor === factor)
        return (
          <article className="card" key={factor}>
            <header className="card-head">
              <h2>{factor === 'fuel' ? 'Fuel' : 'Rates'}</h2>
              <p>{items.length} relevant {items.length === 1 ? 'market' : 'markets'}</p>
            </header>
            {items.length === 0 && <p className="muted">None labeled yet.</p>}
            <ul className="market-list">
              {items.slice(0, 24).map((row) => (
                <li key={`${row.meta.venue}-${row.meta.market_id}`}>
                  <div>
                    <strong>{row.meta.title}</strong>
                    <p className="muted">
                      {row.meta.venue} · yes moves {row.label?.yes_moves_factor}
                      {row.label?.label_source === 'llm' ? ' · LLM label, unvalidated' : ''}
                      {row.stale ? ' · stale quote' : ''}
                    </p>
                  </div>
                  <div className="market-score">
                    <span>{row.trust ? num(row.trust.score) : '—'}</span>
                    <small>trust</small>
                  </div>
                </li>
              ))}
            </ul>
          </article>
        )
      })}
    </>
  )
}
