# fmt: off
# pylint: disable=line-too-long
"""Metrics dashboard helpers for PredBat.

Provides :func:`get_metrics_dashboard_css` and :func:`get_metrics_dashboard_body`
which are called from the main :class:`WebInterface` handler so the dashboard is
rendered inside the standard PredBat navigation shell.

Dark/light mode is derived automatically from the ``body.dark-mode`` class used
by the rest of the PredBat web UI - no separate theme toggle is needed.
"""
def get_metrics_dashboard_css():
    """Return scoped CSS for the metrics dashboard component."""
    return """<style>
/* Metrics dashboard CSS - all vars and rules scoped to avoid conflicts */
/* Light mode is default (matches predbat default); dark overrides applied via html.dark-mode */
:root {
  --md-bg: #f1f5f9; --md-surface: #ffffff; --md-border: #cbd5e1;
  --md-text: #1e293b; --md-muted: #64748b;
  --md-green: #16a34a; --md-red: #dc2626; --md-amber: #d97706; --md-blue: #2563eb;
}
html.dark-mode {
  --md-bg: #0f172a; --md-surface: #1e293b; --md-border: #334155;
  --md-text: #e2e8f0; --md-muted: #94a3b8;
  --md-green: #22c55e; --md-red: #ef4444; --md-amber: #f59e0b; --md-blue: #3b82f6;
}
.metrics-dash {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  color: var(--md-text); line-height: 1.5;
  padding: 1rem 1rem 2rem; max-width: 1200px;
}
.metrics-dash *, .metrics-dash *::before, .metrics-dash *::after { box-sizing: border-box; }
.metrics-dash section { margin-bottom: 1.5rem; }
.metrics-dash section h2 {
  font-size: 1.1rem; font-weight: 600; margin-bottom: 0.75rem;
  padding-bottom: 0.35rem; border-bottom: 1px solid var(--md-border);
  color: var(--md-text); display: block;
}
.md-last-refresh { font-size: 0.8rem; color: var(--md-muted); margin-bottom: 1rem; }
.md-card-grid {
  display: grid; gap: 0.75rem;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
}
.md-card {
  background: var(--md-surface); border: 1px solid var(--md-border);
  border-radius: 0.5rem; padding: 0.85rem; text-align: center;
}
.md-card .md-label { font-size: 0.75rem; color: var(--md-muted); text-transform: uppercase; letter-spacing: 0.05em; }
.md-card .md-value { font-size: 1.6rem; font-weight: 700; margin-top: 0.25rem; }
.md-card .md-sub   { font-size: 0.8rem;  color: var(--md-muted); margin-top: 0.15rem; }
.md-status-ok   { color: var(--md-green); }
.md-status-warn { color: var(--md-amber); }
.md-status-err  { color: var(--md-red);   }
.md-chart-row {
  display: grid; gap: 1rem;
  grid-template-columns: 280px 1fr;
}
.md-chart-box {
  background: var(--md-surface); border: 1px solid var(--md-border);
  border-radius: 0.5rem; padding: 1rem;
}
.md-chart-box canvas { max-width: 100%; }
.metrics-dash table {
  width: 100%; border-collapse: collapse;
  background: var(--md-surface); border: 1px solid var(--md-border);
  border-radius: 0.5rem; overflow: hidden;
}
.metrics-dash th, .metrics-dash td { padding: 0.5rem 0.75rem; text-align: left; font-size: 0.85rem; }
.metrics-dash th { background: var(--md-border); color: var(--md-text); font-weight: 600; }
.metrics-dash td { border-top: 1px solid var(--md-border); }
.md-quota-bar {
  background: var(--md-border); border-radius: 0.25rem; height: 1.25rem;
  overflow: hidden; position: relative; margin-top: 0.25rem;
}
.md-quota-fill  { height: 100%; border-radius: 0.25rem; transition: width 0.5s; }
.md-quota-text  {
  position: absolute; top: 0; left: 0; right: 0; text-align: center;
  font-size: 0.75rem; line-height: 1.25rem; font-weight: 600;
}
@media (max-width: 640px) {
  .md-chart-row { grid-template-columns: 1fr; }
  .md-card-grid { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
}
</style>
"""


def get_metrics_dashboard_body(data_json):
    """Return the metrics dashboard HTML body fragment with *data_json* embedded."""
    return (
        r"""<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<div class="metrics-dash" id="metricsDash">
<p class="md-last-refresh" id="mdLastRefresh"></p>

<!-- Section 1: System Health -->
<section>
  <h2>System Health</h2>
  <div class="md-card-grid" id="mdHealthCards"></div>
</section>

<!-- Section 2: Battery Status -->
<section>
  <h2>Battery Status</h2>
  <div class="md-chart-row">
    <div class="md-chart-box" style="display:flex;align-items:center;justify-content:center;">
      <canvas id="mdSocChart" width="240" height="240"></canvas>
    </div>
    <div class="md-chart-box">
      <canvas id="mdPowerChart" height="200"></canvas>
    </div>
  </div>
</section>

<!-- Section 3: Energy Today -->
<section>
  <h2>Energy Today</h2>
  <div class="md-chart-box">
    <canvas id="mdEnergyChart" height="160"></canvas>
  </div>
</section>

<!-- Section 4: Cost & Savings -->
<section>
  <h2>Cost &amp; Savings</h2>
  <div class="md-card-grid" id="mdCostCards"></div>
</section>

<!-- Section 5: API & Solar -->
<section>
  <h2>API &amp; Solar Status</h2>
  <div id="mdApiTable"></div>
  <div style="margin-top:1rem;" class="md-card-grid" id="mdSolarCards"></div>
</section>
</div>

<script>
(function() {
var MD_DATA = """
        + data_json
        + r""";
var mdSocChart, mdPowerChart, mdEnergyChart;

/* Read a CSS custom property from :root (works for --md-* vars defined above) */
function mdVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function mdFmt(v, dp) { return (typeof v === 'number') ? v.toFixed(dp === undefined ? 1 : dp) : '\u2014'; }
function mdAgo(ts) {
  if (!ts || ts <= 0) return 'Never';
  var s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm ago';
}
function mdStatusClass(ok) { return ok ? 'md-status-ok' : 'md-status-err'; }
function mdExtractVersion(up) {
  for (var k in up) { try { var o = eval('(' + k + ')'); if (o.version) return o.version; } catch (e) {} }
  return '?';
}
function mdIsUp(up) { for (var k in up) { if (up[k] >= 1) return true; } return false; }
function mdSumLabeled(obj) { var t = 0; for (var k in obj) t += obj[k]; return t; }

function mdRenderHealth(d) {
  var up = mdIsUp(d.up), ver = mdExtractVersion(d.up);
  var planOk = d.plan_valid >= 1, configOk = d.config_valid >= 1;
  var cards = [
    {label:'Status',      value: up ? 'UP' : 'DOWN',              cls: mdStatusClass(up),       sub: 'v' + ver},
    {label:'Config',      value: configOk ? 'Valid' : 'Invalid',  cls: mdStatusClass(configOk), sub: d.config_warnings > 0 ? d.config_warnings + ' warnings' : 'No warnings'},
    {label:'Plan',        value: planOk ? 'Valid' : 'Stale',      cls: mdStatusClass(planOk),   sub: mdFmt(d.plan_age_minutes, 0) + ' min old'},
    {label:'Last Update', value: mdAgo(d.last_update_timestamp),  cls: '', sub: ''},
    {label:'Errors',      value: mdFmt(mdSumLabeled(d.errors_total), 0), cls: mdSumLabeled(d.errors_total) > 0 ? 'md-status-warn' : 'md-status-ok', sub: ''},
    {label:'Data Age',    value: mdFmt(d.data_age_days, 1) + 'd', cls: d.data_age_days > 3 ? 'md-status-warn' : 'md-status-ok', sub: ''},
  ];
  var h = '';
  cards.forEach(function (c) {
    h += '<div class="md-card"><div class="md-label">' + c.label + '</div>'
      + '<div class="md-value ' + c.cls + '">' + c.value + '</div>'
      + (c.sub ? '<div class="md-sub">' + c.sub + '</div>' : '') + '</div>';
  });
  document.getElementById('mdHealthCards').innerHTML = h;
}

function mdRenderCost(d) {
  var curr = d.currency_symbol || '\u00A3';
  var cards = [
    {label:'Cost Today',       value: curr + mdFmt(d.cost_today / 100, 2),           cls: 'md-status-err'},
    {label:'Savings Yesterday (PV+Bat)', value: curr + mdFmt(d.savings_today_pvbat / 100, 2),  cls: 'md-status-ok'},
    {label:'Savings Yesterday (PredBat)', value: curr + mdFmt(d.savings_today_predbat / 100, 2), cls: 'md-status-ok'},
    {label:'Cost Yesterday (Actual)', value: curr + mdFmt(d.savings_today_actual / 100, 2), cls: 'md-status-ok'},
  ];
  var h = '';
  cards.forEach(function (c) {
    h += '<div class="md-card"><div class="md-label">' + c.label + '</div>'
      + '<div class="md-value ' + c.cls + '">' + c.value + '</div></div>';
  });
  document.getElementById('mdCostCards').innerHTML = h;
}

function mdInitSOCChart(d) {
  var ctx = document.getElementById('mdSocChart').getContext('2d');
  var pct = d.battery_soc_percent || 0;
  mdSocChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['SOC', 'Remaining'],
      datasets: [{ data: [pct, 100 - pct], backgroundColor: [mdVar('--md-green'), mdVar('--md-border')], borderWidth: 0 }]
    },
    options: { cutout: '72%', responsive: false, plugins: { legend: {display:false}, tooltip: {enabled:false} } },
    plugins: [{
      id: 'md-center-text',
      afterDraw: function (chart) {
        var w = chart.width, h = chart.height, c2 = chart.ctx;
        c2.save();
        c2.fillStyle = mdVar('--md-text');
        c2.font = 'bold 2rem sans-serif';
        c2.textAlign = 'center'; c2.textBaseline = 'middle';
        c2.fillText(mdFmt(pct, 0) + '%', w / 2, h / 2 - 10);
        c2.font = '0.85rem sans-serif';
        c2.fillStyle = mdVar('--md-muted');
        c2.fillText(mdFmt(d.battery_soc_kwh, 1) + ' / ' + mdFmt(d.battery_max_kwh, 1) + ' kWh', w / 2, h / 2 + 18);
        c2.restore();
      }
    }]
  });
}

function mdUpdateSOCChart(d) {
  var pct = d.battery_soc_percent || 0;
  mdSocChart.data.datasets[0].data = [pct, 100 - pct];
  mdSocChart.update();
}

function mdPowerData(d) {
  var bp = d.battery_power || 0;
  var gp = d.grid_power || 0;
  return {
    labels: ['Bat Charge', 'Bat Discharge', 'Load', 'PV', 'Grid Import', 'Grid Export'],
    values: [
      Math.max(0, -bp),
      Math.max(0,  bp),
      d.load_power || 0,
      d.pv_power   || 0,
      Math.max(0,  gp),
      Math.max(0, -gp),
    ],
    colors: [mdVar('--md-green'), mdVar('--md-amber'), mdVar('--md-blue'), '#eab308', mdVar('--md-red'), '#0d9488'],
  };
}

function mdInitPowerChart(d) {
  var ctx = document.getElementById('mdPowerChart').getContext('2d');
  var pd = mdPowerData(d);
  mdPowerChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: pd.labels,
      datasets: [{ data: pd.values, backgroundColor: pd.colors, borderRadius: 4 }]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: {legend:{display:false}},
      scales: {
        x: {title:{display:true,text:'kW',color:mdVar('--md-muted')}, grid:{color:mdVar('--md-border')}, ticks:{color:mdVar('--md-muted')}, min: 0},
        y: {grid:{display:false}, ticks:{color:mdVar('--md-text')}}
      }
    }
  });
}

function mdInitEnergyChart(d) {
  var ctx = document.getElementById('mdEnergyChart').getContext('2d');
  mdEnergyChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: ['Load', 'Import', 'Export', 'PV'],
      datasets: [{ data: [d.load_today_kwh||0, d.import_today_kwh||0, d.export_today_kwh||0, d.pv_today_kwh||0], backgroundColor: [mdVar('--md-blue'), mdVar('--md-red'), mdVar('--md-green'), mdVar('--md-amber')], borderRadius: 4 }]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: {legend:{display:false}},
      scales: {
        x: {title:{display:true,text:'kWh',color:mdVar('--md-muted')}, grid:{color:mdVar('--md-border')}, ticks:{color:mdVar('--md-muted')}},
        y: {grid:{display:false}, ticks:{color:mdVar('--md-text')}}
      }
    }
  });
}

function mdRenderAPI(d) {
  var services = d.api_services || {};
  var names = Object.keys(services).sort();
  if (names.length === 0) { document.getElementById('mdApiTable').innerHTML = '<div class="md-card" style="text-align:center;">No API calls recorded yet</div>'; return; }
  var h = '<table><thead><tr><th>Service</th><th>Requests</th><th>Failures</th><th>Last Success</th></tr></thead><tbody>';
  names.forEach(function (n) {
    var s = services[n];
    h += '<tr><td>' + n + '</td><td>' + mdFmt(s.requests || 0, 0) + '</td>'
      + '<td class="' + ((s.failures || 0) > 0 ? 'md-status-warn' : '') + '">' + mdFmt(s.failures || 0, 0) + '</td>'
      + '<td>' + mdAgo(s.last_success || 0) + '</td></tr>';
  });
  h += '</tbody></table>';
  document.getElementById('mdApiTable').innerHTML = h;
}

function mdRenderSolar(d) {
  var limit = d.solcast_api_limit || 0, used = d.solcast_api_used || 0;
  var pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
  var barColor = pct > 80 ? 'var(--md-red)' : pct > 50 ? 'var(--md-amber)' : 'var(--md-green)';
  var h = '<div class="md-card"><div class="md-label">Solcast API Quota</div>';
  h += '<div class="md-quota-bar"><div class="md-quota-fill" style="width:' + pct + '%;background:' + barColor + '"></div>';
  h += '<div class="md-quota-text">' + mdFmt(used, 0) + ' / ' + mdFmt(limit, 0) + '</div></div>';
  h += '<div class="md-sub" style="margin-top:0.4rem;">' + mdFmt(d.solcast_api_remaining, 0) + ' remaining</div></div>';
  h += '<div class="md-card"><div class="md-label">PV Scaling (Worst)</div><div class="md-value">' + mdFmt(d.pv_scaling_worst, 2) + '</div></div>';
  h += '<div class="md-card"><div class="md-label">PV Scaling (Best)</div><div class="md-value">' + mdFmt(d.pv_scaling_best, 2) + '</div></div>';
  h += '<div class="md-card"><div class="md-label">PV Scaling (Total)</div><div class="md-value">' + mdFmt(d.pv_scaling_total, 2) + '</div></div>';
  document.getElementById('mdSolarCards').innerHTML = h;
}

function mdRebuildCharts(d) {
  if (mdSocChart)    { mdSocChart.destroy();    mdSocChart    = null; }
  if (mdPowerChart)  { mdPowerChart.destroy();  mdPowerChart  = null; }
  if (mdEnergyChart) { mdEnergyChart.destroy(); mdEnergyChart = null; }
  mdInitSOCChart(d); mdInitPowerChart(d); mdInitEnergyChart(d);
}

function mdRenderAll(d) {
  mdRenderHealth(d);
  mdRenderCost(d);
  mdRenderAPI(d);
  mdRenderSolar(d);
  if (!mdSocChart) {
    mdInitSOCChart(d); mdInitPowerChart(d); mdInitEnergyChart(d);
  } else {
    mdUpdateSOCChart(d);
    var pd = mdPowerData(d);
    mdPowerChart.data.datasets[0].data = pd.values;
    mdPowerChart.update();
    mdEnergyChart.data.datasets[0].data = [d.load_today_kwh||0, d.import_today_kwh||0, d.export_today_kwh||0, d.pv_today_kwh||0];
    mdEnergyChart.update();
  }
  document.getElementById('mdLastRefresh').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
}

/* Rebuild charts whenever dark mode is toggled so Chart.js picks up new CSS vars */
(function () {
  var observer = new MutationObserver(function () { mdRebuildCharts(MD_DATA); });
  observer.observe(document.body, { attributes: true, attributeFilter: ['class'] });
})();

mdRenderAll(MD_DATA);

setInterval(function () {
  fetch('./metrics/json')
    .then(function (r) { return r.json(); })
    .then(function (d) { MD_DATA = d; mdRenderAll(d); })
    .catch(function () {});
}, 30000);
})();
</script>
"""
    )
