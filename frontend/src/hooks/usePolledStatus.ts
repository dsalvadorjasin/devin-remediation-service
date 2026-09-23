import { useEffect, useState } from 'react'
import { fetchStatus } from '../api/client'
import type { TaskView } from '../api/types'

export interface PolledStatus {
  rows: TaskView[]
  lastUpdated: Date | null
  error: Error | null
}

const REQUEST_TIMEOUT_MS = 15_000

/**
 * Polls GET /status immediately, then `pollMs` after each request settles
 * (requests never overlap). Each attempt is aborted after REQUEST_TIMEOUT_MS
 * and on unmount. On failure the last good `rows` are kept and `error` is set.
 */
export function usePolledStatus(pollMs: number): PolledStatus {
  const [state, setState] = useState<PolledStatus>({ rows: [], lastUpdated: null, error: null })

  useEffect(() => {
    let disposed = false
    let active: AbortController | null = null
    let nextTimer: ReturnType<typeof setTimeout> | undefined

    const tick = async () => {
      const controller = new AbortController()
      active = controller
      const timeout = setTimeout(() => controller.abort(new Error('GET /status timed out')), REQUEST_TIMEOUT_MS)
      try {
        const rows = await fetchStatus(controller.signal)
        if (disposed) return
        setState({ rows, lastUpdated: new Date(), error: null })
      } catch (err) {
        if (disposed) return
        const reason = controller.signal.aborted ? controller.signal.reason : err
        const error = reason instanceof Error ? reason : new Error(String(reason))
        setState((prev) => ({ ...prev, error }))
      } finally {
        clearTimeout(timeout)
        active = null
      }
      nextTimer = setTimeout(() => void tick(), pollMs)
    }

    void tick()
    return () => {
      disposed = true
      clearTimeout(nextTimer)
      active?.abort()
    }
  }, [pollMs])

  return state
}
