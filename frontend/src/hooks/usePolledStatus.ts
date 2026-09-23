import { useEffect, useState } from 'react'
import { fetchStatus } from '../api/client'
import type { TaskView } from '../api/types'

export interface PolledStatus {
  rows: TaskView[]
  lastUpdated: Date | null
  error: Error | null
}

/**
 * Polls GET /status immediately, then `pollMs` after each request settles
 * (requests never overlap). The in-flight request is aborted on unmount.
 * On failure the last good `rows` are kept and `error` is set.
 */
export function usePolledStatus(pollMs: number): PolledStatus {
  const [state, setState] = useState<PolledStatus>({ rows: [], lastUpdated: null, error: null })

  useEffect(() => {
    const controller = new AbortController()
    const { signal } = controller
    let timer: ReturnType<typeof setTimeout> | undefined

    const tick = async () => {
      try {
        const rows = await fetchStatus(signal)
        if (signal.aborted) return
        setState({ rows, lastUpdated: new Date(), error: null })
      } catch (err) {
        if (signal.aborted) return
        const error = err instanceof Error ? err : new Error(String(err))
        setState((prev) => ({ ...prev, error }))
      }
      timer = setTimeout(() => void tick(), pollMs)
    }

    void tick()
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [pollMs])

  return state
}
