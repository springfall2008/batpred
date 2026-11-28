import { useEffect, useRef, useState } from 'react'
import type { CurvesChartData } from '../types/charts'

type PlotlyApi = { newPlot: (element: HTMLElement, traces: object[], layout: object, config: object) => Promise<unknown>, purge: (element: HTMLElement) => void }
declare global { interface Window { Plotly?: PlotlyApi } }

const plotlyUrl = 'https://cdn.jsdelivr.net/npm/plotly.js-gl3d-dist-min@2.35.2/plotly-gl3d.min.js'
let plotlyLoading: Promise<PlotlyApi> | undefined

function loadPlotly() {
  if (window.Plotly) return Promise.resolve(window.Plotly)
  if (!plotlyLoading) {
    plotlyLoading = new Promise<PlotlyApi>((resolve, reject) => {
      const script = document.createElement('script')
      script.src = plotlyUrl
      script.onload = () => window.Plotly ? resolve(window.Plotly) : reject(new Error('Plotly did not load'))
      script.onerror = () => reject(new Error('Unable to load the 3D chart library'))
      document.head.appendChild(script)
    }).catch((error) => { plotlyLoading = undefined; throw error })
  }
  return plotlyLoading
}

function GridChart({ grid, maxRate }: { grid: CurvesChartData['grids'][number], maxRate: number }) {
  const ref = useRef<HTMLDivElement>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    let plotly: PlotlyApi | undefined
    const element = ref.current
    if (!element) return
    loadPlotly().then((api) => {
      if (!active) return
      plotly = api
      const colour = getComputedStyle(element).color
      const axis = (title: string) => ({ title: { text: title, font: { color: colour } }, tickfont: { color: colour }, gridcolor: 'rgba(128,128,128,0.3)' })
      const traces: object[] = [{ type: 'surface', x: grid.soc, y: grid.temperature, z: grid.z, cmin: 0, cmax: maxRate, colorscale: 'Viridis', opacity: 0.85, name: `${grid.kind} grid` }]
      if (grid.evidence.length) traces.push({ type: 'scatter3d', mode: 'markers', x: grid.evidence.map((point) => point[0]), y: grid.evidence.map((point) => point[1]), z: grid.evidence.map((point) => point[2]), name: 'Evidence (last 90 days)', marker: { size: 3, color: '#e4572e' } })
      return api.newPlot(element, traces, { paper_bgcolor: 'rgba(0,0,0,0)', margin: { l: 0, r: 0, t: 0, b: 0 }, legend: { font: { color: colour } }, scene: { xaxis: axis('SoC %'), yaxis: axis('Battery temperature °C'), zaxis: { ...axis('Rate / rated'), range: [0, maxRate] } } }, { responsive: true })
    }).catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : 'Unable to display chart') })
    return () => { active = false; if (plotly) plotly.purge(element) }
  }, [grid, maxRate])
  return <>{error && <p role="alert">{error}</p>}<div ref={ref} className="curves-plot" aria-label={`${grid.kind} battery rate grid`} /></>
}

/** Display the active rate grids and the evidence used to learn them. */
export default function CurvesChart({ data }: { data: CurvesChartData }) {
  return <section className="curves-chart">
    <h2>Battery rate grids</h2>
    <p>Battery rates are shown as a fraction of the rated rate, by state of charge and battery temperature. Where there is not enough evidence, the configured curves are used.</p>
    <div className="curves-table-wrap"><table><thead><tr><th>Grid</th><th>Learned grid</th><th>Source</th><th>Cells</th><th>Evidence days</th><th>Temperatures</th><th>Learned at</th><th>Last scan</th></tr></thead><tbody>
      {data.grids.map((grid) => <tr key={grid.kind}><th>{grid.kind}</th><td>{grid.state === 'on' ? 'Yes' : grid.state ? 'No' : 'Not yet run'}</td>{['source', 'cells', 'days', 'temperature_range', 'updated', 'last_scan'].map((key) => <td key={key}>{Array.isArray(grid.attributes[key]) ? (grid.attributes[key] as unknown[]).join(' to ') + ' °C' : String(grid.attributes[key] ?? '—')}</td>)}</tr>)}
    </tbody></table></div>
    {data.grids.map((grid) => <section key={grid.kind}><h3>{grid.kind === 'charge' ? 'Charge' : 'Discharge'} rate grid</h3><GridChart grid={grid} maxRate={data.max_rate} />
      {!grid.evidence.length && <p>No {grid.kind} evidence yet. The grid uses the configured curves or full rated rate.</p>}
      <div className="curves-table-wrap"><table><thead><tr><th>°C / SoC %</th>{grid.table.soc.map((soc) => <th key={soc}>{soc}</th>)}</tr></thead><tbody>{grid.table.rows.map((row, index) => <tr key={index}><th>{grid.table.temperature?.[index] ?? 'All'}</th>{row.map((value, i) => <td key={i}>{value.toFixed(2)}</td>)}</tr>)}</tbody></table></div>
    </section>)}
  </section>
}
