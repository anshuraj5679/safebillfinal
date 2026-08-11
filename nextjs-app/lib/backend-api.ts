import type { NextRequest } from 'next/server'

const DEFAULT_BACKEND_ORIGIN = 'http://localhost:8000'
const DEFAULT_TIMEOUT_MS = 90000

function normalizeBaseUrl(raw: string): string {
  const trimmed = raw.replace(/\/+$/, '')
  if (trimmed.endsWith('/api/v1')) return trimmed
  if (trimmed.endsWith('/api')) return `${trimmed}/v1`
  return `${trimmed}/api/v1`
}

const configuredBaseUrl =
  process.env.BACKEND_API_BASE_URL ||
  process.env.NEXT_PUBLIC_API_BASE_URL ||
  DEFAULT_BACKEND_ORIGIN

const backendBaseUrl = normalizeBaseUrl(configuredBaseUrl)
const backendServiceToken =
  process.env.BACKEND_API_SERVICE_TOKEN ||
  process.env.BACKEND_API_TOKEN ||
  (process.env.NODE_ENV === 'development' ? 'safebill-analyst-token' : '')
const timeoutMs = Number(process.env.BACKEND_API_TIMEOUT_MS || DEFAULT_TIMEOUT_MS)
const ACCESS_TOKEN_COOKIE = 'sb_access_token'

export class BackendApiError extends Error {
  status: number
  payload: unknown

  constructor(message: string, status: number, payload: unknown) {
    super(message)
    this.name = 'BackendApiError'
    this.status = status
    this.payload = payload
  }
}

function toErrorMessage(payload: unknown): string | null {
  if (typeof payload === 'string') return payload
  if (!payload || typeof payload !== 'object') return null

  const record = payload as Record<string, unknown>
  const detail = record.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object') {
    const nested = detail as Record<string, unknown>
    const candidates = [nested.message, nested.error, nested.reason, nested.detail]
    for (const candidate of candidates) {
      if (typeof candidate === 'string' && candidate.trim()) {
        return candidate
      }
    }
    try {
      return JSON.stringify(detail)
    } catch {
      return String(detail)
    }
  }

  const candidates = [record.error, record.message, record.reason]
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim()) {
      return candidate
    }
  }

  return null
}

function buildHeaders(
  body: BodyInit | null | undefined,
  headers?: HeadersInit,
  authToken?: string | null
): Headers {
  const merged = new Headers(headers)
  const resolvedToken = (authToken || '').trim() || backendServiceToken
  if (resolvedToken) {
    merged.set('Authorization', `Bearer ${resolvedToken}`)
  }
  if (body && !(body instanceof FormData) && !merged.has('Content-Type')) {
    merged.set('Content-Type', 'application/json')
  }
  return merged
}

async function parseResponse(response: Response): Promise<unknown> {
  const contentType = response.headers.get('content-type') || ''
  if (contentType.includes('application/json')) {
    return response.json()
  }
  return response.text()
}

export async function backendApiFetch<T>(
  path: string,
  init: RequestInit = {},
  requestTimeoutMs: number = timeoutMs,
  authToken?: string | null
): Promise<T> {
  const preferredToken = (authToken || '').trim()
  const fallbackToken = backendServiceToken.trim()
  const allowDevAuthRetry = process.env.NODE_ENV !== 'production'
  const initialToken =
    preferredToken ||
    (authToken === undefined ? fallbackToken : '')
  if (!initialToken) {
    throw new BackendApiError('Missing backend authorization token', 401, { detail: 'Unauthorized' })
  }

  const controller = new AbortController()
  const timeoutHandle = setTimeout(() => controller.abort(), requestTimeoutMs)
  const doFetch = async (token: string): Promise<Response> =>
    fetch(`${backendBaseUrl}${path}`, {
      ...init,
      headers: buildHeaders(init.body, init.headers, token),
      cache: 'no-store',
      signal: controller.signal,
    })
  try {
    let response: Response
    try {
      response = await doFetch(initialToken)
    } catch (error) {
      const isAbortError =
        error instanceof Error &&
        (error.name === 'AbortError' || controller.signal.aborted)
      if (isAbortError) {
        throw new BackendApiError(
          `Backend request timed out after ${requestTimeoutMs}ms`,
          504,
          { detail: 'Gateway Timeout' }
        )
      }
      throw error
    }
    let payload = await parseResponse(response)

    if (
      !response.ok &&
      response.status === 401 &&
      allowDevAuthRetry &&
      preferredToken &&
      fallbackToken &&
      preferredToken !== fallbackToken
    ) {
      response = await doFetch(fallbackToken)
      payload = await parseResponse(response)
    }

    if (!response.ok) {
      const message = toErrorMessage(payload) || `Backend request failed with status ${response.status}`
      throw new BackendApiError(message, response.status, payload)
    }
    return payload as T
  } finally {
    clearTimeout(timeoutHandle)
  }
}

export async function backendPublicApiFetch<T>(
  path: string,
  init: RequestInit = {},
  requestTimeoutMs: number = timeoutMs
): Promise<T> {
  const controller = new AbortController()
  const timeoutHandle = setTimeout(() => controller.abort(), requestTimeoutMs)

  try {
    let response: Response
    try {
      response = await fetch(`${backendBaseUrl}${path}`, {
        ...init,
        headers: buildHeaders(init.body, init.headers, ''),
        cache: 'no-store',
        signal: controller.signal,
      })
    } catch (error) {
      const isAbortError =
        error instanceof Error &&
        (error.name === 'AbortError' || controller.signal.aborted)
      if (isAbortError) {
        throw new BackendApiError(
          `Backend request timed out after ${requestTimeoutMs}ms`,
          504,
          { detail: 'Gateway Timeout' }
        )
      }
      throw error
    }

    const payload = await parseResponse(response)
    if (!response.ok) {
      const message = toErrorMessage(payload) || `Backend request failed with status ${response.status}`
      throw new BackendApiError(message, response.status, payload)
    }

    return payload as T
  } finally {
    clearTimeout(timeoutHandle)
  }
}

function parseBearerToken(value: string | null): string | null {
  if (!value) return null
  const trimmed = value.trim()
  if (!trimmed) return null
  if (!trimmed.toLowerCase().startsWith('bearer ')) return null
  const token = trimmed.slice(7).trim()
  return token || null
}

export function resolveRequestAuthToken(request: NextRequest): string | null {
  const headerToken = parseBearerToken(request.headers.get('authorization'))
  if (headerToken) return headerToken

  const cookieToken = request.cookies.get(ACCESS_TOKEN_COOKIE)?.value?.trim()
  if (cookieToken) return cookieToken

  return null
}

export function withQuery(path: string, params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    search.set(key, String(value))
  })
  const queryString = search.toString()
  return queryString ? `${path}?${queryString}` : path
}
