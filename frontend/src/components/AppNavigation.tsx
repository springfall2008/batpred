import { useEffect, useState } from 'react'

import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'

import {
  faBars,
  faCircleCheck,
  faCircleNotch,
  faBatteryEmpty,
  faBatteryQuarter,
  faBatteryHalf,
  faBatteryThreeQuarters,
  faBatteryFull,
  faBookOpen,
  faCalendarDays,
  faChartLine,
  faChevronLeft,
  faChevronRight,
  faCode,
  faComments,
  faFileCode,
  faGear,
  faGaugeHigh,
  faHouse,
  faList,
  faMicrochip,
  faRightLeft,
  faScroll,
  faSitemap,
  faServer,
  faSliders,
  faXmark,
  faCircleHalfStroke,
  faMoon,
  faSun,
  faTableColumns,
} from '@fortawesome/free-solid-svg-icons'

import {
  faGithub
} from '@fortawesome/free-brands-svg-icons'

import type { IconDefinition } from '@fortawesome/fontawesome-svg-core'

import batLogoLight from '../assets/bat_logo_light.png'
import batLogoDark from '../assets/bat_logo_dark.png'

import './AppNavigation.css'
import { MODERN_UI_VERSION } from '../version'
import { getNextThemePreference, readThemePreference, resolveTheme, type ThemePreference } from './theme'

type AppNavigationProps = {
  collapsed: boolean
  onCollapsedChange: (collapsed: boolean) => void
  layout: 'side' | 'horizontal'
  onLayoutChange: (layout: 'side' | 'horizontal') => void

  calculating: boolean
  batterySoc: number | null
  chatEnabled: boolean

  version: string
}

type NavigationItem = {
  label: string
  href: string
  icon: IconDefinition
  external?: boolean
}

type NavigationGroup = {
  label: string
  items: NavigationItem[]
}

/*
 * Predbat's existing server-rendered pages.
 *
 * Relative URLs are deliberate. Predbat can run behind
 * Home Assistant ingress, so "/plan" is not necessarily
 * the correct browser URL while "./plan" is.
 */
const navigationGroups: NavigationGroup[] = [
  {
    label: 'Predbat',
    items: [
      {
        label: 'Dashboard',
        href: './dash',
        icon: faHouse
      },
      {
        label: 'Plan',
        href: './plan',
        icon: faCalendarDays
      },
      {
        label: 'Overview',
        href: './overview',
        icon: faGaugeHigh
      },
      {
        label: 'Charts',
        href: './charts',
        icon: faChartLine
      },
      {
        label: 'Entities',
        href: './dash?page=entity',
        icon: faList
      },
      {
        label: 'Compare',
        href: './compare',
        icon: faRightLeft
      },
      {
        label: 'What If',
        href: './annual',
        icon: faBatteryHalf
      },
      {
        label: 'Chat',
        href: './chat',
        icon: faComments
      }
    ]
  },

  {
    label: 'Configuration',
    items: [
      {
        label: 'Config',
        href: './config',
        icon: faSliders
      },
      {
        label: 'Apps',
        href: './apps',
        icon: faGear
      },
      {
        label: 'Editor',
        href: './apps_editor',
        icon: faCode
      },
      {
        label: 'Components',
        href: './components',
        icon: faMicrochip
      }
    ]
  },

  {
    label: 'System',
    items: [
      {
        label: 'Log',
        href: './log',
        icon: faScroll
      },
      {
        label: 'Browse',
        href: './browse',
        icon: faFileCode
      },
      {
        label: 'Discovery',
        href: './discovery',
        icon: faSitemap
      },
      {
        label: 'Cards',
        href: './dash?page=cards',
        icon: faTableColumns
      },
      {
        label: 'Internals',
        href: './internals',
        icon: faServer
      },
      {
        label: 'Docs',
        href: './docs',
        icon: faBookOpen
      }
    ]
  },

  {
    label: 'Support',
    items: [
      {
        label: 'GitHub',
        href: 'https://github.com/springfall2008/batpred',
        icon: faGithub,
        external: true,
      }
    ]
  }
]

const horizontalShortcuts = navigationGroups[0].items.slice(0, 3)

function getBatteryIcon(soc: number) {
  if (soc >= 88) {
    return faBatteryFull
  }

  if (soc >= 63) {
    return faBatteryThreeQuarters
  }

  if (soc >= 38) {
    return faBatteryHalf
  }

  if (soc >= 13) {
    return faBatteryQuarter
  }

  return faBatteryEmpty
}

export default function AppNavigation({
  collapsed,
  onCollapsedChange,
  layout,
  onLayoutChange,
  calculating,
  batterySoc,
  chatEnabled,
  version
}: AppNavigationProps) {
  /*
   * Mobile menu always starts closed.
   *
   * Unlike desktop collapse, we deliberately don't persist
   * this because opening the site on mobile should never
   * immediately cover the dashboard.
   */
  const [mobileOpen, setMobileOpen] = useState(false)

  const [batFlying, setBatFlying] = useState(false)

  function flyBat() {
    /*
     * Ignore additional clicks while the bat is already flying.
     */
    if (batFlying) {
      return
    }

    setBatFlying(true)

    /*
     * This duration should match the CSS animation duration.
     */
    window.setTimeout(() => {
      setBatFlying(false)
    }, 4500)
  }

  /*
   * Prevent the page underneath scrolling while the
   * mobile navigation drawer is open.
   */
  useEffect(() => {
    if (!mobileOpen) {
      return
    }

    const originalOverflow = document.body.style.overflow

    document.body.style.overflow = 'hidden'

    return () => {
      document.body.style.overflow = originalOverflow
    }
  }, [mobileOpen])

  const [theme, setTheme] = useState<ThemePreference>(() => {
    try {
      return readThemePreference(localStorage.getItem('predbat-theme'))
    } catch {
      // Theme preference is non-critical.
      return 'auto'
    }
  })
  const [systemDark, setSystemDark] = useState(() => window.matchMedia('(prefers-color-scheme: dark)').matches)
  const resolvedTheme = resolveTheme(theme, systemDark)

  useEffect(() => {
    const query = window.matchMedia('(prefers-color-scheme: dark)')
    const updateSystemTheme = (event: MediaQueryListEvent) => setSystemDark(event.matches)

    query.addEventListener('change', updateSystemTheme)

    return () => query.removeEventListener('change', updateSystemTheme)
  }, [])

  useEffect(() => {
    document.documentElement.dataset.theme = resolvedTheme
    document.querySelector('meta[name="theme-color"]')?.setAttribute(
      'content',
      resolvedTheme === 'dark' ? '#111827' : '#f3f4f6'
    )

    try {
      localStorage.setItem('predbat-theme', theme)
    } catch {
      // Theme preference is non-critical.
    }
  }, [resolvedTheme, theme])

  const nextTheme = getNextThemePreference(theme)
  const themeLabel = theme === 'light' ? 'Light' : theme === 'dark' ? 'Dark' : 'Auto'
  const nextThemeLabel = nextTheme === 'light' ? 'Light' : nextTheme === 'dark' ? 'Dark' : 'Auto'

  const pathPage = window.location.pathname.replace(/\/+$/, '').split('/').pop() ?? 'dash'
  const currentPage = new URLSearchParams(window.location.search).get('page') ?? pathPage

  return (
    <>
      {/*
       * Mobile top bar.
       */}
      <header className="mobile-app-header">
        <button
          type="button"
          className="mobile-menu-button"
          aria-label="Open navigation"
          aria-expanded={mobileOpen}
          onClick={() => setMobileOpen(true)}
        >
          <FontAwesomeIcon icon={faBars} />
        </button>

        <img
          src={resolvedTheme === 'dark' ? batLogoDark : batLogoLight}
          alt="Predbat"
          width="512"
          height="177"
          className="mobile-app-logo"
        />
      </header>

      {/*
       * Semi-transparent background behind the drawer.
       */}
      {mobileOpen && (
        <button
          type="button"
          className="navigation-backdrop"
          aria-label="Close navigation"
          onClick={() => setMobileOpen(false)}
        />
      )}

      <aside
        className={[
          'app-navigation',
          collapsed ? 'is-collapsed' : '',
          layout === 'horizontal' ? 'is-horizontal' : '',
          mobileOpen ? 'is-mobile-open' : ''
        ]
          .filter(Boolean)
          .join(' ')}
      >
        <div className="navigation-header">
          <div className="navigation-brand">
            <div className="navigation-brand-main">
              <button
                type="button"
                className="navigation-logo-button"
                aria-label="Predbat"
                title="Predbat"
                onClick={flyBat}
              >
                <img
                  src={resolvedTheme === 'dark' ? batLogoDark : batLogoLight}
                  alt=""
                  width="512"
                  height="177"
                  className="navigation-logo-image"
                />
              </button>

              <span className="navigation-brand-name">Predbat</span>
            </div>

            <div className="navigation-live-status">
              <span
                className={['navigation-plan-status', calculating ? 'is-calculating' : 'is-ready']
                  .filter(Boolean)
                  .join(' ')}
                title={calculating ? 'Predbat is calculating' : 'Plan generated'}
              >
                <FontAwesomeIcon
                  icon={calculating ? faCircleNotch : faCircleCheck}
                  spin={calculating}
                />
              </span>

              {batterySoc !== null && (
                <span
                  className="navigation-battery-status"
                  title={`Battery state of charge: ${Math.round(batterySoc)}%`}
                >
                  <FontAwesomeIcon icon={getBatteryIcon(batterySoc)} />

                  <span className="navigation-battery-soc">{Math.round(batterySoc)}%</span>
                </span>
              )}
            </div>
          </div>

          {/*
           * Desktop collapse button.
           */}
          <button
            type="button"
            className="navigation-collapse-button"
            aria-label={collapsed ? 'Expand navigation' : 'Collapse navigation'}
            onClick={() => onCollapsedChange(!collapsed)}
          >
            <FontAwesomeIcon icon={collapsed ? faChevronRight : faChevronLeft} />
          </button>

          {/*
           * Mobile close button.
           */}
          <button
            type="button"
            className="navigation-mobile-close"
            aria-label="Close navigation"
            onClick={() => setMobileOpen(false)}
          >
            <FontAwesomeIcon icon={faXmark} />
          </button>
        </div>

        <nav className="navigation-menu">
          <div className="navigation-horizontal-shortcuts">
            {horizontalShortcuts.map((item) => {
              const active = item.href === `./${currentPage}` || item.href.endsWith(`?page=${currentPage}`)

              return (
                <a
                  key={item.href}
                  href={item.href}
                  aria-current={active ? 'page' : undefined}
                  className={['navigation-item', active ? 'is-active' : '']
                    .filter(Boolean)
                    .join(' ')}
                >
                  <span className="navigation-item-icon">
                    <FontAwesomeIcon icon={item.icon} />
                  </span>

                  <span className="navigation-item-label">{item.label}</span>
                </a>
              )
            })}
          </div>

          {navigationGroups.map((group) => (
            <details
              className="navigation-group"
              key={group.label}
              name={layout === 'horizontal' && !mobileOpen ? 'predbat-navigation' : undefined}
              open={layout === 'side' || mobileOpen || undefined}
            >
              <summary className="navigation-group-label">{group.label}</summary>

              <div className="navigation-group-items">
                {group.items.filter((item) => item.label !== 'Chat' || chatEnabled).map((item) => {
                  const active = item.href === `./${currentPage}` || item.href.endsWith(`?page=${currentPage}`)

                  return (
                    <a
                      key={item.href}
                      href={item.href}
                      target={item.external ? '_blank' : undefined}
                      rel={item.external ? 'noreferrer' : undefined}
                      aria-current={active ? 'page' : undefined}
                      className={[
                        'navigation-item',
                        horizontalShortcuts.includes(item) ? 'navigation-horizontal-shortcut-source' : '',
                        active ? 'is-active' : ''
                      ]
                        .filter(Boolean)
                        .join(' ')}
                      title={collapsed && layout === 'side' ? item.label : undefined}
                      onClick={() => setMobileOpen(false)}
                    >
                      <span className="navigation-item-icon">
                        <FontAwesomeIcon icon={item.icon} />
                      </span>

                      <span className="navigation-item-label">{item.label}</span>
                    </a>
                  )
                })}
              </div>
            </details>
          ))}
        </nav>

        <footer className="navigation-footer">
          <div className="navigation-footer-actions">
            <button
              type="button"
              className="navigation-layout-button"
              aria-label={layout === 'side' ? 'Use top navigation' : 'Use side navigation'}
              title={layout === 'side' ? 'Top navigation' : 'Side navigation'}
              onClick={() => onLayoutChange(layout === 'side' ? 'horizontal' : 'side')}
            >
              <FontAwesomeIcon icon={layout === 'side' ? faBars : faTableColumns} />

              <span className="navigation-layout-label">{layout === 'side' ? 'Top' : 'Side'}</span>
            </button>

            <button
              type="button"
              className="navigation-theme-button"
              aria-label={`Theme is ${themeLabel}. Switch to ${nextThemeLabel.toLowerCase()} mode`}
              title={theme === 'auto' ? `Auto follows browser settings. Switch to ${nextThemeLabel.toLowerCase()} mode` : `Switch to ${nextThemeLabel.toLowerCase()} mode`}
              onClick={() => setTheme(getNextThemePreference)}
            >
              <FontAwesomeIcon icon={theme === 'light' ? faSun : theme === 'dark' ? faMoon : faCircleHalfStroke} />

              <span className="navigation-theme-label">{themeLabel}</span>
            </button>
          </div>

          {layout === 'side' && (
            <span className="navigation-version" title={`Predbat ${version} · Modern UI ${MODERN_UI_VERSION}`}>
              {version} · UI {MODERN_UI_VERSION}
            </span>
          )}
        </footer>
      </aside>

      {batFlying && (
        <div className="predbat-flying-bat" aria-hidden="true">
          <img src={resolvedTheme === 'dark' ? batLogoDark : batLogoLight} alt="" width="512" height="177" />
        </div>
      )}
    </>
  )
}
