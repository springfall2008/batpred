export type ThemePreference = 'light' | 'dark' | 'auto'
export type ResolvedTheme = Exclude<ThemePreference, 'auto'>

export function readThemePreference(stored: string | null): ThemePreference {
  return stored === 'light' || stored === 'dark' || stored === 'auto' ? stored : 'auto'
}

export function resolveTheme(preference: ThemePreference, systemDark: boolean): ResolvedTheme {
  return preference === 'auto' ? (systemDark ? 'dark' : 'light') : preference
}

export function getNextThemePreference(preference: ThemePreference): ThemePreference {
  return preference === 'light' ? 'dark' : preference === 'dark' ? 'auto' : 'light'
}
