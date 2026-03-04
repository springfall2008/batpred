# fmt: off
# pylint: disable=line-too-long
"""Self-contained metrics dashboard for PredBat.

Serves a single-page HTML dashboard at ``/metrics/dashboard`` that visualises
the Prometheus metrics exposed by :mod:`predbat_metrics`.  Data is embedded as
JSON on first load and auto-refreshed every 30 seconds via ``/metrics/json``.
"""

from predbat_metrics import PROMETHEUS_AVAILABLE, metrics


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PredBat Metrics Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg: #0f172a; --surface: #1e293b; --border: #334155;
  --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8;
  --green: #22c55e; --red: #ef4444; --amber: #f59e0b; --blue: #3b82f6;
}
[data-theme="light"] {
  --bg: #f1f5f9; --surface: #ffffff; --border: #cbd5e1;
  --text: #1e293b; --muted: #64748b; --accent: #0284c7;
  --green: #16a34a; --red: #dc2626; --amber: #d97706; --blue: #2563eb;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.5;
  padding: 1rem; max-width: 1200px; margin: 0 auto;
}
header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 1.5rem; flex-wrap: wrap; gap: 0.5rem;
}
header h1 { font-size: 1.5rem; font-weight: 700; }
header .controls { display: flex; gap: 0.75rem; align-items: center; }
#lastRefresh { font-size: 0.8rem; color: var(--muted); }
.theme-btn {
  background: var(--surface); border: 1px solid var(--border); color: var(--text);
  padding: 0.35rem 0.75rem; border-radius: 0.375rem; cursor: pointer; font-size: 0.85rem;
}
section { margin-bottom: 1.5rem; }
section h2 {
  font-size: 1.1rem; font-weight: 600; margin-bottom: 0.75rem;
  padding-bottom: 0.35rem; border-bottom: 1px solid var(--border);
}
.card-grid {
  display: grid; gap: 0.75rem;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
}
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 0.5rem; padding: 0.85rem; text-align: center;
}
.card .label { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.card .value { font-size: 1.6rem; font-weight: 700; margin-top: 0.25rem; }
.card .sub { font-size: 0.8rem; color: var(--muted); margin-top: 0.15rem; }
.status-ok { color: var(--green); }
.status-warn { color: var(--amber); }
.status-err { color: var(--red); }
.chart-row {
  display: grid; gap: 1rem;
  grid-template-columns: 280px 1fr;
}
.chart-box {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 0.5rem; padding: 1rem;
}
.chart-box canvas { max-width: 100%; }
table {
  width: 100%; border-collapse: collapse;
  background: var(--surface); border: 1px solid var(--border); border-radius: 0.5rem;
  overflow: hidden;
}
th, td { padding: 0.5rem 0.75rem; text-align: left; font-size: 0.85rem; }
th { background: var(--border); color: var(--text); font-weight: 600; }
td { border-top: 1px solid var(--border); }
.quota-bar {
  background: var(--border); border-radius: 0.25rem; height: 1.25rem;
  overflow: hidden; position: relative; margin-top: 0.25rem;
}
.quota-fill { height: 100%; border-radius: 0.25rem; transition: width 0.5s; }
.quota-text {
  position: absolute; top: 0; left: 0; right: 0; text-align: center;
  font-size: 0.75rem; line-height: 1.25rem; font-weight: 600;
}
@media (max-width: 640px) {
  .chart-row { grid-template-columns: 1fr; }
  .card-grid { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
}
</style>
</head>
<body>
<header>
  <h1>PredBat Metrics</h1>
  <div class="controls">
    <span id="lastRefresh"></span>
    <button class="theme-btn" onclick="toggleTheme()">Toggle Theme</button>
  </div>
</header>

<!-- Section 1: System Health -->
<section>
  <h2>System Health</h2>
  <div class="card-grid" id="healthCards"></div>
</section>

<!-- Section 2: Battery Status -->
<section>
  <h2>Battery Status</h2>
  <div class="chart-row">
    <div class="chart-box" style="display:flex;align-items:center;justify-content:center;">
      <canvas id="socChart" width="240" height="240"></canvas>
    </div>
    <div class="chart-box">
      <canvas id="rateChart" height="120"></canvas>
    </div>
  </div>
</section>

<!-- Section 3: Energy Today -->
<section>
  <h2>Energy Today</h2>
  <div class="chart-box">
    <canvas id="energyChart" height="100"></canvas>
  </div>
</section>

<!-- Section 4: Cost & Savings -->
<section>
  <h2>Cost &amp; Savings</h2>
  <div class="card-grid" id="costCards"></div>
</section>

<!-- Section 5: API & Solar -->
<section>
  <h2>API &amp; Solar Status</h2>
  <div id="apiTable"></div>
  <div style="margin-top:1rem;" class="card-grid" id="solarCards"></div>
</section>

<script>
var DATA = __METRICS_JSON__;
var socChart, rateChart, energyChart;

function fmt(v, dp) { return (typeof v === 'number') ? v.toFixed(dp === undefined ? 1 : dp) : '—'; }
function ago(ts) {
  if (!ts || ts <= 0) return 'Never';
  var s = Math.floor(Date.now()/1000 - ts);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm ago';
}

function statusClass(ok) { return ok ? 'status-ok' : 'status-err'; }

function extractVersion(up) {
  for (var k in up) {
    try { var o = eval('(' + k + ')'); if (o.version) return o.version; } catch(e) {}
  }
  return '?';
}
function isUp(up) {
  for (var k in up) { if (up[k] >= 1) return true; }
  return false;
}
function sumLabeled(obj) {
  var t = 0; for (var k in obj) t += obj[k]; return t;
}

function renderHealth(d) {
  var up = isUp(d.up), ver = extractVersion(d.up);
  var planOk = d.plan_valid >= 1;
  var configOk = d.config_valid >= 1;
  var cards = [
    {label:'Status', value: up ? 'UP' : 'DOWN', cls: statusClass(up), sub:'v' + ver},
    {label:'Config', value: configOk ? 'Valid' : 'Invalid', cls: statusClass(configOk), sub: d.config_warnings > 0 ? d.config_warnings + ' warnings' : 'No warnings'},
    {label:'Plan', value: planOk ? 'Valid' : 'Stale', cls: statusClass(planOk), sub: fmt(d.plan_age_minutes,0) + ' min old'},
    {label:'Last Update', value: ago(d.last_update_timestamp), cls:'', sub:''},
    {label:'Errors', value: fmt(sumLabeled(d.errors_total),0), cls: sumLabeled(d.errors_total) > 0 ? 'status-warn' : 'status-ok', sub:''},
    {label:'Data Age', value: fmt(d.data_age_days,1) + 'd', cls: d.data_age_days > 3 ? 'status-warn' : 'status-ok', sub:''},
  ];
  var h = '';
  cards.forEach(function(c) {
    h += '<div class="card"><div class="label">' + c.label + '</div>'
      + '<div class="value ' + c.cls + '">' + c.value + '</div>'
      + (c.sub ? '<div class="sub">' + c.sub + '</div>' : '') + '</div>';
  });
  document.getElementById('healthCards').innerHTML = h;
}

function renderCost(d) {
  var cards = [
    {label:'Cost Today', value: '\u00A3' + fmt(d.cost_today,2), cls:'status-err'},
    {label:'Savings (PV+Bat)', value: '\u00A3' + fmt(d.savings_today_pvbat,2), cls:'status-ok'},
    {label:'Savings (Actual)', value: '\u00A3' + fmt(d.savings_today_actual,2), cls:'status-ok'},
  ];
  var h = '';
  cards.forEach(function(c) {
    h += '<div class="card"><div class="label">' + c.label + '</div>'
      + '<div class="value ' + c.cls + '">' + c.value + '</div></div>';
  });
  document.getElementById('costCards').innerHTML = h;
}

function initSOCChart(d) {
  var ctx = document.getElementById('socChart').getContext('2d');
  var pct = d.battery_soc_percent || 0;
  socChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['SOC', 'Remaining'],
      datasets: [{
        data: [pct, 100 - pct],
        backgroundColor: [getComputedStyle(document.body).getPropertyValue('--green').trim(), getComputedStyle(document.body).getPropertyValue('--border').trim()],
        borderWidth: 0,
      }]
    },
    options: {
      cutout: '72%', responsive: false,
      plugins: {
        legend: {display: false},
        tooltip: {enabled: false},
      }
    },
    plugins: [{
      id: 'center-text',
      afterDraw: function(chart) {
        var w = chart.width, h = chart.height, ctx2 = chart.ctx;
        ctx2.save();
        ctx2.fillStyle = getComputedStyle(document.body).getPropertyValue('--text').trim();
        ctx2.font = 'bold 2rem sans-serif';
        ctx2.textAlign = 'center'; ctx2.textBaseline = 'middle';
        ctx2.fillText(fmt(pct,0) + '%', w/2, h/2 - 10);
        ctx2.font = '0.85rem sans-serif';
        ctx2.fillStyle = getComputedStyle(document.body).getPropertyValue('--muted').trim();
        ctx2.fillText(fmt(d.battery_soc_kwh,1) + ' / ' + fmt(d.battery_max_kwh,1) + ' kWh', w/2, h/2 + 18);
        ctx2.restore();
      }
    }]
  });
}

function updateSOCChart(d) {
  var pct = d.battery_soc_percent || 0;
  socChart.data.datasets[0].data = [pct, 100 - pct];
  socChart.options.plugins = socChart.options.plugins || {};
  socChart.update();
}

function initRateChart(d) {
  var ctx = document.getElementById('rateChart').getContext('2d');
  rateChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: ['Charge', 'Discharge'],
      datasets: [{
        data: [d.charge_rate_kw || 0, d.discharge_rate_kw || 0],
        backgroundColor: [getComputedStyle(document.body).getPropertyValue('--green').trim(), getComputedStyle(document.body).getPropertyValue('--amber').trim()],
        borderRadius: 4,
      }]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: {legend: {display: false}},
      scales: {
        x: {title: {display: true, text: 'kW', color: getComputedStyle(document.body).getPropertyValue('--muted').trim()}, grid: {color: getComputedStyle(document.body).getPropertyValue('--border').trim()}, ticks: {color: getComputedStyle(document.body).getPropertyValue('--muted').trim()}},
        y: {grid: {display: false}, ticks: {color: getComputedStyle(document.body).getPropertyValue('--text').trim()}}
      }
    }
  });
}

function initEnergyChart(d) {
  var ctx = document.getElementById('energyChart').getContext('2d');
  energyChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: ['Load', 'Import', 'Export', 'PV'],
      datasets: [{
        data: [d.load_today_kwh||0, d.import_today_kwh||0, d.export_today_kwh||0, d.pv_today_kwh||0],
        backgroundColor: [
          getComputedStyle(document.body).getPropertyValue('--blue').trim(),
          getComputedStyle(document.body).getPropertyValue('--red').trim(),
          getComputedStyle(document.body).getPropertyValue('--green').trim(),
          getComputedStyle(document.body).getPropertyValue('--amber').trim()
        ],
        borderRadius: 4,
      }]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: {legend: {display: false}},
      scales: {
        x: {title: {display: true, text: 'kWh', color: getComputedStyle(document.body).getPropertyValue('--muted').trim()}, grid: {color: getComputedStyle(document.body).getPropertyValue('--border').trim()}, ticks: {color: getComputedStyle(document.body).getPropertyValue('--muted').trim()}},
        y: {grid: {display: false}, ticks: {color: getComputedStyle(document.body).getPropertyValue('--text').trim()}}
      }
    }
  });
}

function renderAPI(d) {
  var services = {};
  for (var k in d.api_requests_total) {
    try { var o = eval('(' + k + ')'); services[o.service] = services[o.service] || {}; services[o.service].requests = d.api_requests_total[k]; } catch(e) {}
  }
  for (var k in d.api_failures_total) {
    try { var o = eval('(' + k + ')'); if (o.service) { services[o.service] = services[o.service] || {}; services[o.service].failures = (services[o.service].failures||0) + d.api_failures_total[k]; } } catch(e) {}
  }
  for (var k in d.api_last_success_timestamp) {
    try { var o = eval('(' + k + ')'); services[o.service] = services[o.service] || {}; services[o.service].last = d.api_last_success_timestamp[k]; } catch(e) {}
  }
  var names = Object.keys(services).sort();
  if (names.length === 0) {
    document.getElementById('apiTable').innerHTML = '<div class="card" style="text-align:center;">No API calls recorded yet</div>';
    return;
  }
  var h = '<table><thead><tr><th>Service</th><th>Requests</th><th>Failures</th><th>Last Success</th></tr></thead><tbody>';
  names.forEach(function(n) {
    var s = services[n];
    h += '<tr><td>' + n + '</td><td>' + fmt(s.requests||0,0) + '</td>'
      + '<td class="' + ((s.failures||0) > 0 ? 'status-warn' : '') + '">' + fmt(s.failures||0,0) + '</td>'
      + '<td>' + ago(s.last||0) + '</td></tr>';
  });
  h += '</tbody></table>';
  document.getElementById('apiTable').innerHTML = h;
}

function renderSolar(d) {
  var limit = d.solcast_api_limit || 0;
  var used = d.solcast_api_used || 0;
  var pct = limit > 0 ? Math.min(100, (used/limit)*100) : 0;
  var barColor = pct > 80 ? 'var(--red)' : pct > 50 ? 'var(--amber)' : 'var(--green)';

  var h = '<div class="card"><div class="label">Solcast API Quota</div>';
  h += '<div class="quota-bar"><div class="quota-fill" style="width:' + pct + '%;background:' + barColor + '"></div>';
  h += '<div class="quota-text">' + fmt(used,0) + ' / ' + fmt(limit,0) + '</div></div>';
  h += '<div class="sub" style="margin-top:0.4rem;">' + fmt(d.solcast_api_remaining,0) + ' remaining</div></div>';

  h += '<div class="card"><div class="label">PV Scaling (Worst)</div><div class="value">' + fmt(d.pv_scaling_worst,2) + '</div></div>';
  h += '<div class="card"><div class="label">PV Scaling (Best)</div><div class="value">' + fmt(d.pv_scaling_best,2) + '</div></div>';
  h += '<div class="card"><div class="label">PV Scaling (Total)</div><div class="value">' + fmt(d.pv_scaling_total,2) + '</div></div>';
  document.getElementById('solarCards').innerHTML = h;
}

function renderAll(d) {
  renderHealth(d);
  renderCost(d);
  renderAPI(d);
  renderSolar(d);
  if (!socChart) { initSOCChart(d); initRateChart(d); initEnergyChart(d); }
  else {
    updateSOCChart(d);
    rateChart.data.datasets[0].data = [d.charge_rate_kw||0, d.discharge_rate_kw||0];
    rateChart.update();
    energyChart.data.datasets[0].data = [d.load_today_kwh||0, d.import_today_kwh||0, d.export_today_kwh||0, d.pv_today_kwh||0];
    energyChart.update();
  }
  document.getElementById('lastRefresh').textContent = 'Updated: ' + new Date().toLocaleTimeString();
}

function toggleTheme() {
  var t = document.documentElement.getAttribute('data-theme');
  document.documentElement.setAttribute('data-theme', t === 'light' ? '' : 'light');
  localStorage.setItem('pbTheme', t === 'light' ? 'dark' : 'light');
  // Rebuild charts so colors update
  if (socChart) { socChart.destroy(); socChart = null; }
  if (rateChart) { rateChart.destroy(); rateChart = null; }
  if (energyChart) { energyChart.destroy(); energyChart = null; }
  renderAll(DATA);
}

(function() {
  if (localStorage.getItem('pbTheme') === 'light')
    document.documentElement.setAttribute('data-theme', 'light');
})();

renderAll(DATA);

setInterval(function() {
  fetch('/metrics/json')
    .then(function(r) { return r.json(); })
    .then(function(d) { DATA = d; renderAll(d); })
    .catch(function() {});
}, 30000);
</script>
</body>
</html>"""

FALLBACK_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>PredBat Metrics</title>
<style>body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;background:#0f172a;color:#e2e8f0;text-align:center;}</style>
</head><body><div><h1>Metrics Dashboard Unavailable</h1>
<p><code>prometheus_client</code> is not installed.</p>
<p>Install it with: <code>pip install prometheus_client</code></p></div></body></html>"""


async def metrics_dashboard_handler(request):
    """Serve the self-contained metrics dashboard at ``/metrics/dashboard``."""
    import json
    from aiohttp import web

    if not PROMETHEUS_AVAILABLE:
        return web.Response(text=FALLBACK_HTML, content_type="text/html")

    data_json = json.dumps(metrics().to_dict())
    html = DASHBOARD_HTML.replace("__METRICS_JSON__", data_json)
    return web.Response(text=html, content_type="text/html")
