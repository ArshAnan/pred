import type { Alert, Company, Fact, Health, MarketView, RunBody, RunResult, RunSummary } from './types'

export class ApiError extends Error {
  code: string
  constructor(code: string, message: string) {
    super(message)
    this.code = code
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, init)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = (body as { error?: { code?: string; message?: string } }).error
    throw new ApiError(error?.code || 'E_INTERNAL', error?.message || response.statusText)
  }
  return body as T
}

export function getHealth() {
  return request<Health>('/health')
}

export function getCompanies() {
  return request<Company[]>('/companies')
}

export function getFacts(ticker: string, asOf: string | null) {
  const query = asOf ? `?as_of=${encodeURIComponent(asOf)}` : ''
  return request<Fact[]>(`/companies/${encodeURIComponent(ticker)}/facts${query}`)
}

export function getMarkets(factor: string) {
  return request<MarketView[]>(`/markets?factor=${encodeURIComponent(factor)}`)
}

export function getAlerts() {
  return request<Alert[]>('/alerts')
}

export function getRuns() {
  return request<RunSummary[]>('/runs?limit=8')
}

export function getRun(runId: string) {
  return request<RunResult>(`/runs/${encodeURIComponent(runId)}`)
}

export function createRun(body: RunBody) {
  return request<RunResult>('/runs', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': crypto.randomUUID(),
    },
    body: JSON.stringify(body),
  })
}

export function sendTestAlert() {
  return request<{ delivered: boolean }>('/admin/alerts/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
}
