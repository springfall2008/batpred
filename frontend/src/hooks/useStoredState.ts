import { useEffect, useState, type Dispatch, type SetStateAction } from 'react'

type StoredValue = number | string

export function resolveStoredValue<T extends StoredValue>(stored: string | null, fallback: T, allowed: readonly T[]): T {
  return allowed.find((value) => String(value) === stored) ?? fallback
}

/** Keep a validated chart preference in local storage. */
export function useStoredState<T extends StoredValue>(key: string, fallback: T, allowed: readonly T[]): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    try {
      return resolveStoredValue(localStorage.getItem(key), fallback, allowed)
    } catch {
      return fallback
    }
  })

  useEffect(() => {
    try {
      localStorage.setItem(key, String(value))
    } catch {
      // Browsers may disable storage; the in-memory preference still works.
    }
  }, [key, value])

  return [value, setValue]
}
