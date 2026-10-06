import { formatApiError } from './apiError'

export const API_BASE = import.meta.env.VITE_API_BASE ?? ''
const fragment = new URLSearchParams(window.location.hash.slice(1))
const bootstrapToken = fragment.get('session')
if (bootstrapToken) {
  sessionStorage.setItem('mail-group-session', bootstrapToken)
  history.replaceState(null, '', window.location.pathname + window.location.search)
}

export async function apiFetch(path: string, options: RequestInit = {}): Promise<Response> {
  const headers = new Headers(options.headers)
  const token = sessionStorage.getItem('mail-group-session')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  // Desktop authorization is injected by the isolated main process, scoped to its backend.
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers, signal: options.signal ?? AbortSignal.timeout(60000) })
  if (!response.ok) throw new Error(formatApiError(await response.text(), response.status))
  return response
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (typeof options.body === 'string') headers.set('Content-Type', 'application/json')
  return (await apiFetch(path, { ...options, headers })).json() as Promise<T>
}

export async function requestForm<T>(path: string, body: FormData): Promise<T> {
  return (await apiFetch(path, { method: 'POST', body })).json() as Promise<T>
}
