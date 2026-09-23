import { useEffect, useState } from 'react'
import { fetchStatus } from '../api/client'
import type { TaskView } from '../api/types'

export interface PolledStatus {
  rows: TaskView[]
  lastUpdated: Date | null
  error: Error | null
}

/**
 * Polls GET /status immediately and then every `pollMs`. In-flight requests are
 * aborted on unmount. On failure the last good `rows` are kept and `error` is set.
 */
export function usePolledStatus(pollMs: number): PolledStatus {
  const [state, setState] = useState<PolledStatus>({ rows: [], lastUpdated: null, error: null })

  useEffect(() => {
    let controller: AbortController | null = null
    let disposed = false

    const tick = async () => {
      controller?.abort()
      controller = new AbortController()
      const { signal } = controller
      try {
        const rows = await fetchStatus(signal)
        if (disposed || signal.aborted) return
        setState({ rows, lastUpdated: new Date(), error: null })
      } catch (err) {
        if (disposed || signal.aborted) return
        const error = err instanceof Error ? err : new Error(String(err))
        setState((prev) => ({ ...prev, error }))
      }
    }

    void tick()
    const id = setInterval(() => void tick(), pollMs)
    return () => {
      disposed = true
      clearInterval(id)
      controller?.abort()
    }
  }, [pollMs])

  return state
}
