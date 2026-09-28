import { lazy, Suspense, useEffect, useRef, useState } from 'react'

import AppNavigation from './components/AppNavigation'
import PlanSummary from './components/PlanSummary'
import { PlanDescription } from './components/PlanDescription'
import StatusCard from './components/StatusCard'
import PowerFlow from './components/PowerFlow'
import DebugPanel from './components/DebugPanel'
import MetricsPanel from './components/MetricsPanel'
import PlanPage from './pages/PlanPage'
import ChartsPage from './pages/ChartsPage'
import { useStoredState } from './hooks/useStoredState'

const AppsEditorPage = lazy(() => import('./pages/AppsEditorPage'))
const DocsPage = lazy(() => import('./pages/DocsPage'))
const LogPage = lazy(() => import('./pages/LogPage'))
const ComponentsPage = lazy(() => import('./pages/ComponentsPage'))
const BrowsePage = lazy(() => import('./pages/BrowsePage'))
const InternalsPage = lazy(() => import('./pages/InternalsPage'))
const ConfigPage = lazy(() => import('./pages/ConfigPage'))
const ComparePage = lazy(() => import('./pages/ComparePage'))
const AnnualPage = lazy(() => import('./pages/AnnualPage'))
const ChatPage = lazy(() => import('./pages/ChatPage'))
const AppsPage = lazy(() => import('./pages/AppsPage'))
const EntitiesPage = lazy(() => import('./pages/EntitiesPage'))
const DiscoveryPage = lazy(() => import('./pages/DiscoveryPage'))
const CardsPage = lazy(() => import('./pages/CardsPage'))

import type { PlanData } from './types/plan'
import type { PredbatStatus } from './types/status'
import type { PowerFlowData } from './types/powerFlow'

import './App.css'

type ApiSource = 'plan' | 'status' | 'powerFlow'

/** Coordinates dashboard polling and refreshes the plan after a calculation completes. */
function App() {
  const pathPage = window.location.pathname.replace(/\/+$/, '').split('/').pop() ?? 'dash'
  const currentPage = new URLSearchParams(window.location.search).get('page') ?? pathPage

  const API_FAILURE_THRESHOLD = 2

  const API_SOURCE_NAMES: Record<ApiSource, string> = {
    plan: 'Plan data',
    status: 'Status',
    powerFlow: 'Power flow'
  }

  /*
   * Dashboard data.
   */

  const previousCalculatingRef = useRef<boolean | null>(null)

  const [planData, setPlanData] = useState<PlanData | null>(null)

  const [statusData, setStatusData] = useState<PredbatStatus | null>(null)

  const [powerFlowData, setPowerFlowData] = useState<PowerFlowData | null>(null)

  const [navigationCollapsed, setNavigationCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem('predbat-navigation-collapsed') === 'true'
    } catch {
      return false
    }
  })

  useEffect(() => {
    try {
      localStorage.setItem('predbat-navigation-collapsed', String(navigationCollapsed))
    } catch {
      // Navigation preference is non-critical.
    }
  }, [navigationCollapsed])

  const [navigationLayout, setNavigationLayout] = useStoredState<'side' | 'horizontal'>(
    'predbat-navigation-layout',
    'horizontal',
    ['side', 'horizontal']
  )

  /*
   * Errors from the three polling APIs.
   *
   * Each endpoint gets its own error so that recovery of one
   * endpoint doesn't accidentally clear another endpoint's
   * warning.
   */
  const [apiErrors, setApiErrors] = useState<Partial<Record<ApiSource, string>>>({})

  /*
   * Used when the dashboard cannot complete its initial load.
   *
   * This is different from the non-blocking warning shown when
   * already-loaded dashboard data becomes temporarily stale.
   */
  const [initialError, setInitialError] = useState<string | null>(null)

  /*
   * Control errors are shown immediately.
   *
   * Unlike polling, a failed user action should not require two
   * consecutive failures before the user is told about it.
   */
  const [controlError, setControlError] = useState<string | null>(null)

  /*
   * Consecutive failure count for each endpoint.
   *
   * useRef is used because changing these counters does not
   * itself need to trigger a render.
   */
  const failedFetchesRef = useRef<Record<ApiSource, number>>({
    plan: 0,
    status: 0,
    powerFlow: 0
  })

  /*
   * Record a successful request.
   *
   * Only clear the error belonging to this API source.
   */
  function recordFetchSuccess(source: ApiSource) {
    failedFetchesRef.current[source] = 0

    setApiErrors((current) => {
      if (!current[source]) {
        return current
      }

      const next = {
        ...current
      }

      delete next[source]

      return next
    })
  }

  /*
   * Record a failed request.
   *
   * A single missed poll is ignored because Predbat may simply
   * be restarting or recomputing.
   *
   * After two consecutive failures we show the user a warning.
   */
  function recordFetchFailure(source: ApiSource, error: unknown) {
    failedFetchesRef.current[source] += 1

    const failureCount = failedFetchesRef.current[source]

    console.error(`Unable to fetch Predbat ${API_SOURCE_NAMES[source]}:`, error)

    if (failureCount < API_FAILURE_THRESHOLD) {
      return
    }

    const detail = error instanceof Error ? error.message : 'Unknown error'

    setApiErrors((current) => ({
      ...current,

      [source]: `${API_SOURCE_NAMES[source]} could not be refreshed. ${detail}`
    }))
  }

  /*
   * Shared GET request helper.
   *
   * fetch() does NOT throw for HTTP 404, 500 or 503 responses,
   * so response.ok must be checked explicitly.
   *
   * JSON parsing errors are also caught here.
   */
  async function fetchJson<T>(source: ApiSource, url: string): Promise<T> {
    try {
      const response = await fetch(url, {
        cache: 'no-store'
      })

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`)
      }

      const data = (await response.json()) as T

      recordFetchSuccess(source)

      return data
    } catch (error) {
      recordFetchFailure(source, error)

      throw error
    }
  }

  /*
   * Fetch the Predbat plan.
   */
  async function fetchPlan() {
    const data = await fetchJson<PlanData>('plan', './api/plan_data')

    setPlanData(data)

    return data
  }

  async function fetchStatus() {
    const data = await fetchJson<PredbatStatus>('status', './api/status')

    /*
     * Remember whether Predbat was calculating on the
     * previous status poll.
     */
    const previousCalculating = previousCalculatingRef.current

    /*
     * Store the latest state ready for the next poll.
     */
    previousCalculatingRef.current = data.calculating

    setStatusData(data)

    /*
     * Predbat has just completed a calculation.
     *
     * Re-fetch the plan once so the UI immediately gets
     * the newly calculated rows.
     */
    if (previousCalculating === true && data.calculating === false) {
      fetchPlan().catch(() => {
        /*
         * fetchPlan() already handles/logs its own
         * API failure.
         */
      })
    }
  }

  /*
   * Fetch live Power Flow data.
   */
  async function fetchPowerFlow() {
    const data = await fetchJson<PowerFlowData>('powerFlow', './api/power_flow')

    setPowerFlowData(data)
  }

  /*
   * Update a Predbat control.
   *
   * Control failures are reported immediately because the user
   * has deliberately requested an action.
   */
  async function updateControl(control: string, value: string | boolean) {
    try {
      const response = await fetch('./api/dashboard_control', {
        method: 'POST',

        headers: {
          'Content-Type': 'application/json'
        },

        body: JSON.stringify({
          control,
          value
        })
      })

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`)
      }

      /*
       * The actual control request succeeded.
       */
      setControlError(null)
    } catch (error) {
      console.error('Unable to update Predbat control:', error)

      const detail = error instanceof Error ? error.message : 'Unknown error'

      setControlError(`Predbat could not update the requested setting. ${detail}`)

      return
    }

    /*
     * Re-read Predbat's real state after a successful change.
     *
     * Keep this outside the control try/catch. If refreshing
     * Status fails, that is a Status API problem rather than a
     * failed control change.
     */
    fetchStatus().catch(() => {
      /*
       * fetchStatus() already records/logs its own failure.
       */
    })
  }

  /*
   * Manual retry used by the error notice.
   */
  async function retryAll() {
    setControlError(null)

    const results = await Promise.allSettled([fetchPlan(), fetchStatus(), fetchPowerFlow()])

    /*
     * Plan and Status are the two essential APIs needed to draw
     * the main dashboard.
     */
    const planSucceeded = results[0].status === 'fulfilled'

    const statusSucceeded = results[1].status === 'fulfilled'

    if (planSucceeded && statusSucceeded) {
      setInitialError(null)
    }
  }

  useEffect(() => {
    let cancelled = false

    /*
     * Initial load is slightly different from polling.
     *
     * Promise.allSettled() allows all three requests to complete
     * independently. A Power Flow failure therefore doesn't stop
     * Plan or Status from loading.
     */
    async function initialLoad() {
      const results = await Promise.allSettled([fetchPlan(), fetchStatus(), fetchPowerFlow()])

      if (cancelled) {
        return
      }

      const planFailed = results[0].status === 'rejected'

      const statusFailed = results[1].status === 'rejected'

      /*
       * Plan and Status are essential.
       *
       * Power Flow is optional, so a failure there should not
       * prevent the rest of the dashboard appearing.
       */
      if (planFailed || statusFailed) {
        setInitialError(
          'Predbat dashboard data could not be loaded. Predbat may still be starting.'
        )
      } else {
        setInitialError(null)
      }
    }

    initialLoad()

    /*
     * Status is live operational data.
     */
    const statusTimer = window.setInterval(() => {
      fetchStatus().catch(() => {
        /*
         * Error handling is performed by fetchJson().
         */
      })
    }, 5000)

    /*
     * The plan changes less frequently.
     */
    const planTimer = window.setInterval(() => {
      fetchPlan().catch(() => {
        /*
         * Error handling is performed by fetchJson().
         */
      })
    }, 30000)

    /*
     * Power Flow is live data.
     */
    const powerFlowTimer = window.setInterval(() => {
      fetchPowerFlow().catch(() => {
        /*
         * Error handling is performed by fetchJson().
         */
      })
    }, 5000)

    return () => {
      cancelled = true

      window.clearInterval(statusTimer)
      window.clearInterval(planTimer)
      window.clearInterval(powerFlowTimer)
    }
  }, [])

  /*
   * Select the highest-priority warning to display.
   *
   * A failed control action comes first because it was initiated
   * directly by the user.
   */
  const apiWarning =
    controlError ?? apiErrors.plan ?? apiErrors.status ?? apiErrors.powerFlow ?? null

  const updating = statusData?.updating === true
  const calculating = statusData?.calculating === true
  const active = updating || calculating
  const activityOverlay = active ? (
    <div className={`plan-calculating-overlay ${updating ? '' : 'is-non-blocking'}`} role="status" aria-live="polite">
      <div className="plan-calculating-message">
        <span className="plan-calculating-spinner" aria-hidden="true" />

        <div>
          <strong>{updating ? 'Updating Predbat' : 'Recalculating plan'}</strong>

          <span>{updating ? 'Downloading and installing the selected version…' : 'Further changes will be included in the next calculation.'}</span>
        </div>
      </div>
    </div>
  ) : null

  if (currentPage === 'apps_editor' || currentPage === 'docs' || currentPage === 'log' || currentPage === 'components' || currentPage === 'discovery' || currentPage === 'cards' || currentPage === 'browse' || currentPage === 'internals' || currentPage === 'config' || currentPage === 'compare' || currentPage === 'annual' || currentPage === 'chat' || currentPage === 'apps' || currentPage === 'entity') {
    return (
      <>
        <div
          className={`app-shell ${navigationCollapsed ? 'navigation-collapsed' : ''} ${navigationLayout === 'horizontal' ? 'navigation-horizontal' : ''} ${updating ? 'is-calculating' : ''}`}
          inert={updating}
          aria-busy={updating}
        >
          <AppNavigation
            collapsed={navigationCollapsed}
            onCollapsedChange={setNavigationCollapsed}
            layout={navigationLayout}
            onLayoutChange={setNavigationLayout}
            calculating={active}
            batterySoc={powerFlowData?.soc_percent ?? null}
            chatEnabled={statusData?.chat_enabled ?? false}
            version={statusData?.version ?? ''}
          />
          <div className="app-content">
            <main>
              <Suspense fallback={<div>Loading…</div>}>
                {currentPage === 'apps_editor' ? <AppsEditorPage /> : currentPage === 'docs' ? <DocsPage /> : currentPage === 'components' ? <ComponentsPage /> : currentPage === 'discovery' ? <DiscoveryPage /> : currentPage === 'cards' ? <CardsPage /> : currentPage === 'browse' ? <BrowsePage /> : currentPage === 'internals' ? <InternalsPage /> : currentPage === 'config' ? <ConfigPage /> : currentPage === 'compare' ? <ComparePage /> : currentPage === 'annual' ? <AnnualPage /> : currentPage === 'chat' ? <ChatPage /> : currentPage === 'apps' ? <AppsPage /> : currentPage === 'entity' ? <EntitiesPage /> : <LogPage />}
              </Suspense>
            </main>
          </div>
        </div>

        {activityOverlay}
      </>
    )
  }

  /*
   * If the initial load couldn't obtain the essential data,
   * show a useful error rather than leaving "Loading..." forever.
   *
   * Polling continues in the background, so the dashboard can
   * recover automatically once Predbat becomes available.
   */
  if (initialError && (!planData || !statusData)) {
    return (
      <main className="dashboard-loading">
        <div className="dashboard-load-error">
          <strong>Unable to load Predbat</strong>

          <span>{initialError}</span>

          <button type="button" onClick={retryAll}>
            Retry
          </button>
        </div>
      </main>
    )
  }

  /*
   * Normal first-load state.
   */
  if (!planData || !statusData) {
    return <main>Loading Predbat dashboard...</main>
  }

  return (
    <>
      <div
        className={`app-shell ${navigationCollapsed ? 'navigation-collapsed' : ''} ${navigationLayout === 'horizontal' ? 'navigation-horizontal' : ''} ${updating ? 'is-calculating' : ''}`}
        inert={updating}
        aria-busy={updating}
      >
        <AppNavigation
          collapsed={navigationCollapsed}
          onCollapsedChange={setNavigationCollapsed}
          layout={navigationLayout}
          onLayoutChange={setNavigationLayout}
          calculating={active}
          batterySoc={powerFlowData?.soc_percent ?? null}
          chatEnabled={statusData.chat_enabled}
          version={statusData.version}
        />

        <div className="app-content">
          <main className={currentPage === 'plan' || currentPage === 'charts' ? undefined : 'dashboard-main'}>
            {currentPage === 'plan' ? (
              <PlanPage
                plan={planData.plan}
                yesterday={planData.yesterday}
                baseline={planData.baseline}
                overrides={planData.overrides}
                debugEnabled={statusData.debug_enable}
                onOverrideSubmitted={() => {
                  fetchStatus().catch(() => {
                    /*
                     * fetchStatus() already handles its API error.
                     */
                  })
                }}
              />
            ) : currentPage === 'charts' ? (
              <ChartsPage plan={planData.plan} loadMlEnabled={statusData.load_ml_enabled} />
            ) : (
              <>
                {apiWarning && (
                  <div className="api-error-notice" role="alert">
                    {apiWarning}
                  </div>
                )}

                <StatusCard
                  status={statusData.status}
                  mode={statusData.mode}
                  version={statusData.version}
                  lastUpdated={statusData.last_updated}
                  lastStarted={statusData.last_started}
                  configOk={statusData.config_ok}

                  active={statusData.active}
                  readOnly={statusData.read_only}
                  debugEnabled={statusData.debug_enable}

                  onModeChange={(value) => updateControl('mode', value)}

                  onActiveChange={(value) => updateControl('active', value)}

                  onReadOnlyChange={(value) => updateControl('set_read_only', value)}

                  onDebugChange={(value) => updateControl('debug_enable', value)}
                />

                <PlanSummary plan={planData.plan} />

                <PlanDescription description={planData.plan?.description} />

                {powerFlowData && (
                  <PowerFlow data={powerFlowData} numCars={planData.plan.num_cars} />
                )}

                <MetricsPanel />

                {statusData.debug_enable && (
                  <DebugPanel
                    planData={planData}
                    statusData={statusData}
                    powerFlowData={powerFlowData}
                  />
                )}
              </>
            )}
          </main>
        </div>
      </div>

      {activityOverlay}
    </>
  )
}

export default App
