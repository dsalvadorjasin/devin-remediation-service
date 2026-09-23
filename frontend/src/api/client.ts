import { READ_API, type StatusResponse } from './types'

/**
 * Base URL of the read API. Empty by default so requests are same-origin and
 * go through the Vite dev proxy / the nginx proxy in docker-compose. Set
 * VITE_API_BASE_URL to point the SPA at a remote API.
 */
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? ''

export async function fetchStatus(signal?: AbortSignal): Promise<StatusResponse> {
  const res = await fetch(`${API_BASE_URL}${READ_API.status}`, { signal })
  if (!res.ok) throw new Error(`GET /status failed: ${res.status}`)
  return (await res.json()) as StatusResponse
}
