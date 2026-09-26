import { useState } from 'react'

import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'

import {
  faBug,
  faDownload,
  faFileCode,
  faFileLines,
  faClockRotateLeft,
  faRotateRight,
  faDatabase,
  faChevronDown
} from '@fortawesome/free-solid-svg-icons'

import type { PlanData } from '../types/plan'
import type { PredbatStatus } from '../types/status'
import type { PowerFlowData } from '../types/powerFlow'

import './DebugPanel.css'

type DebugPanelProps = {
  planData: PlanData
  statusData: PredbatStatus
  powerFlowData: PowerFlowData | null
}

function DebugJson({ title, data }: { title: string; data: unknown }) {
  return (
    <details className="debug-panel-section">
      <summary>{title}</summary>

      <pre>{JSON.stringify(data, null, 2)}</pre>
    </details>
  )
}

export default function DebugPanel({ planData, statusData, powerFlowData }: DebugPanelProps) {
  const [restarting, setRestarting] = useState(false)

  const [restartMessage, setRestartMessage] = useState<string | null>(null)

  /*
   * Download either the live in-memory apps.yaml or the
   * apps.yaml file from disk.
   *
   * Predbat supports masked and unmasked versions of both.
   */
  function downloadApps(source: 'live' | 'file') {
    const includeCredentials = window.confirm(
      'Download apps.yaml with real credentials?\n\n' +
        'OK = full unmasked file\n' +
        'Cancel = masked file (credentials redacted)'
    )

    // Cancelling still downloads a file, but keeps credentials redacted.
    const masked = includeCredentials ? '0' : '1'

    const endpoint = source === 'live' ? './debug_apps_live' : './debug_apps'

    window.location.href = `${endpoint}?masked=${masked}`
  }

  /*
   * Restart Predbat using the existing restart endpoint.
   */
  async function restartPredbat() {
    const confirmed = window.confirm('Are you sure you want to restart Predbat?')

    if (!confirmed) {
      return
    }

    setRestarting(true)
    setRestartMessage(null)

    try {
      const response = await fetch('./api/restart', {
        method: 'POST',
        cache: 'no-store',
        headers: {
          'Content-Type': 'application/json'
        }
      })

      const result = await response.json()

      if (!response.ok || !result.success) {
        throw new Error(result.message || `HTTP ${response.status}`)
      }

      setRestartMessage('Restart initiated. Predbat will restart shortly.')
    } catch (error) {
      console.error('Unable to restart Predbat:', error)

      setRestartMessage(
        error instanceof Error ? `Restart failed: ${error.message}` : 'Restart failed.'
      )

      setRestarting(false)
    }
  }

  return (
    <section className="debug-panel">
      <div className="debug-panel-header">
        <FontAwesomeIcon icon={faBug} />

        <div>
          <strong>Debug</strong>

          <span>Diagnostic files and developer information</span>
        </div>
      </div>

      <div className="debug-panel-content">
        {/*
         * apps.yaml
         */}
        <div className="debug-action-row">
          <div className="debug-action-icon">
            <FontAwesomeIcon icon={faFileCode} />
          </div>

          <div className="debug-action-description">
            <strong>apps.yaml</strong>

            <span>Download Predbat configuration</span>
          </div>

          <div className="debug-action-buttons">
            <button type="button" onClick={() => downloadApps('live')}>
              <FontAwesomeIcon icon={faDownload} />
              Live
            </button>

            <button type="button" onClick={() => downloadApps('file')}>
              <FontAwesomeIcon icon={faDownload} />
              File
            </button>
          </div>
        </div>

        {/*
         * Predbat debug YAML
         */}
        <div className="debug-action-row">
          <div className="debug-action-icon">
            <FontAwesomeIcon icon={faBug} />
          </div>

          <div className="debug-action-description">
            <strong>Predbat debug YAML</strong>

            <span>Generate a diagnostic snapshot</span>
          </div>

          <a className="debug-action-button" href="./debug_yaml">
            <FontAwesomeIcon icon={faDownload} />
            Create
          </a>
        </div>

        {/*
         * Log file
         */}
        <div className="debug-action-row">
          <div className="debug-action-icon">
            <FontAwesomeIcon icon={faFileLines} />
          </div>

          <div className="debug-action-description">
            <strong>predbat.log</strong>

            <span>Download the current Predbat log</span>
          </div>

          <a className="debug-action-button" href="./debug_log">
            <FontAwesomeIcon icon={faDownload} />
            Download
          </a>
        </div>

        {/*
         * HTML plan
         */}
        <div className="debug-action-row">
          <div className="debug-action-icon">
            <FontAwesomeIcon icon={faFileCode} />
          </div>

          <div className="debug-action-description">
            <strong>predbat_plan.html</strong>

            <span>Download the current generated plan</span>
          </div>

          <a className="debug-action-button" href="./debug_plan">
            <FontAwesomeIcon icon={faDownload} />
            Download
          </a>
        </div>

        {/*
         * Debug history
         */}
        <div className="debug-action-row">
          <div className="debug-action-icon">
            <FontAwesomeIcon icon={faClockRotateLeft} />
          </div>

          <div className="debug-action-description">
            <strong>Debug history</strong>

            <span>Download all retained debug snapshots</span>
          </div>

          <a className="debug-action-button" href="./debug_history_download_all">
            <FontAwesomeIcon icon={faDownload} />
            Download all
          </a>
        </div>

        {/*
         * Restart
         */}
        <div className="debug-action-row debug-action-row-danger">
          <div className="debug-action-icon">
            <FontAwesomeIcon icon={faRotateRight} />
          </div>

          <div className="debug-action-description">
            <strong>Restart Predbat</strong>

            <span>Stop and restart the Predbat process</span>
          </div>

          <button
            type="button"
            className="debug-restart-button"
            disabled={restarting}
            onClick={restartPredbat}
          >
            <FontAwesomeIcon icon={faRotateRight} spin={restarting} />

            {restarting ? 'Restarting…' : 'Restart'}
          </button>
        </div>

        {restartMessage && <div className="debug-restart-message">{restartMessage}</div>}

        {/*
         * HA Companion warning retained from the original UI.
         */}
        <div className="debug-companion-note">
          <strong>Home Assistant Companion app</strong>

          <span>
            File downloads may not save correctly from the Companion app. Files can instead be found
            in
            <code>/config/debug/</code>.
          </span>
        </div>

        {/*
         * Raw data from the new dashboard APIs.
         *
         * This isn't part of the old dashboard but is useful
         * for development, so keep it as a secondary section.
         */}
        <details className="debug-api-data">
          <summary className="debug-api-summary">
            <div className="debug-api-summary-label">
              <FontAwesomeIcon icon={faDatabase} />
              Raw API data
            </div>

            <FontAwesomeIcon icon={faChevronDown} className="debug-api-chevron" />
          </summary>

          <div className="debug-api-data-content">
            <DebugJson title="Status API" data={statusData} />

            <DebugJson title="Plan API" data={planData} />

            <DebugJson title="Power Flow API" data={powerFlowData} />
          </div>
        </details>
      </div>
    </section>
  )
}
