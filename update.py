#!/usr/bin/env python3
"""
Regenerate the DGC Governance Leaderboard (index.html) from live ClickUp data.

Run by GitHub Actions every morning at 7:00 AM Central.
Needs one environment variable: CLICKUP_API_TOKEN (a pk_... personal token).

Everything about the design lives in this file. Editing index.html directly
will work until the next scheduled run overwrites it. Edit this instead.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- settings

SPACE_ID = "90176662768"          # DGC Hub
API = "https://api.clickup.com/api/v2"
OUTPUT = "index.html"

# Folder name -> short label used by the topic filter buttons.
# Unlisted topics fall back to their first word, so new topics work untouched.
SHORT_NAMES = {
    "Property": "Property",
    "CFI Consolidation": "CFI",
    "Parking Consolidation": "Parking",
    "Non-Revenue Inventory": "Non-Rev",
}

# Statuses whose ClickUp "type" is custom but which really mean "untouched".
NOT_STARTED_NAMES = {
    "to do", "todo", "not started", "open", "backlog", "new", "planned",
}

DEPT_FIELD = "Department"
DOMAIN_FIELD = "Domain"

CENTRAL = timezone(timedelta(hours=-5))   # CDT; CST half the year, close enough
                                          # for a timestamp label.

# ---------------------------------------------------------------- api

def api_get(path, params=None, token=None, tries=4):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={
        "Authorization": token,
        "Content-Type": "application/json",
    })
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                time.sleep(8 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt < tries - 1:
                time.sleep(4 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def fetch_tasks(list_id, token):
    """Every top-level task in a list, closed ones included."""
    out, page = [], 0
    while True:
        data = api_get("/list/%s/task" % list_id, {
            "page": page,
            "subtasks": "false",
            "include_closed": "true",
            "archived": "false",
        }, token)
        chunk = data.get("tasks", [])
        out.extend(chunk)
        if data.get("last_page") or not chunk:
            break
        page += 1
        if page > 60:
            break
    return out


# ---------------------------------------------------------------- parsing

def field_value(task, field_name):
    """Read a dropdown or labels custom field back as a plain string."""
    for f in task.get("custom_fields", []):
        if f.get("name") != field_name:
            continue
        val = f.get("value")
        if val in (None, "", []):
            return None
        opts = (f.get("type_config") or {}).get("options") or []

        def label(v):
            for o in opts:
                if o.get("id") == v:
                    return o.get("name") or o.get("label")
            if isinstance(v, int) and 0 <= v < len(opts):
                o = opts[v]
                return o.get("name") or o.get("label")
            return None

        if isinstance(val, list):
            for v in val:
                if isinstance(v, dict):
                    v = v.get("id")
                got = label(v)
                if got:
                    return got
            return None
        return label(val) or (val if isinstance(val, str) else None)
    return None


def bucket(task):
    st = task.get("status") or {}
    kind = (st.get("type") or "").lower()
    name = (st.get("status") or "").lower().strip()
    if kind in ("done", "closed"):
        return "done"
    if name in NOT_STARTED_NAMES or kind == "open":
        return "ns"
    return "ip"


def tally(tasks, field):
    """{name: {total, ns, ip, done}} for one custom field."""
    agg = {}
    for t in tasks:
        key = field_value(t, field)
        if not key:
            continue
        row = agg.setdefault(key, {"name": key, "total": 0, "ns": 0, "ip": 0, "done": 0})
        row["total"] += 1
        row[bucket(t)] += 1
    return agg


def phase_block(tasks, want_dept):
    counts = {"total": len(tasks), "ns": 0, "ip": 0, "done": 0}
    for t in tasks:
        counts[bucket(t)] += 1
    by_dept = sorted(tally(tasks, DEPT_FIELD).values(), key=lambda r: r["name"]) if want_dept else []
    by_domain = sorted(tally(tasks, DOMAIN_FIELD).values(), key=lambda r: r["name"])
    counts["byDept"] = by_dept
    counts["byDomain"] = by_domain
    return counts


def slug(name):
    keep = [c.lower() if c.isalnum() else "-" for c in name]
    return "".join(keep).strip("-")


# ---------------------------------------------------------------- collect

FALLBACK_COLORS = ["#ff7800", "#2ecd6f", "#9b59b6", "#0f68f9",
                   "#e0457b", "#00a0b0", "#f2c94c", "#6b5b95"]


def collect(token):
    folders = api_get("/space/%s/folder" % SPACE_ID, {"archived": "false"}, token)
    projects = []
    for i, folder in enumerate(folders.get("folders", [])):
        lists = {l["name"]: l["id"] for l in folder.get("lists", [])}
        if "DG Lite" not in lists and "DG Heavy" not in lists:
            continue
        name = folder["name"]
        sys.stderr.write("  reading %s\n" % name)
        lite = fetch_tasks(lists["DG Lite"], token) if "DG Lite" in lists else []
        heavy = fetch_tasks(lists["DG Heavy"], token) if "DG Heavy" in lists else []
        projects.append({
            "key": slug(name),
            "name": name,
            "short": SHORT_NAMES.get(name, name.split()[0]),
            "color": folder.get("color") or FALLBACK_COLORS[i % len(FALLBACK_COLORS)],
            "phases": {
                "lite": phase_block(lite, want_dept=True),
                "heavy": phase_block(heavy, want_dept=False),
            },
        })
    projects.sort(key=lambda p: p["name"])
    return projects


def totals(projects):
    tasks = ip = done = 0
    for p in projects:
        for ph in p["phases"].values():
            tasks += ph["total"]
            ip += ph["ip"]
            done += ph["done"]
    return {"tasks": tasks, "inFlight": ip, "done": done,
            "topics": len(projects), "lists": len(projects) * 2}


# ---------------------------------------------------------------- design
# The whole page: stylesheet, markup, and client script. Edit here.

CSS = r'''
:root{
  --paper:#ffffff;
  --tint:#FBFAF5;
  --tint2:#F4F2EA;
  --line:#E8E4D7;
  --line-2:#D5D0BF;
  --ink:#15130C;
  --ink-2:#464233;
  --ink-3:#6E6857;
  --gold:#FFD135;
  --gold-deep:#7A5C00;
  --blue:#3B82C4;
  --teal:#16A070;
  --coral:#D06030;
  --violet:#8B5CC4;
  --green:#16904E;
  --red:#C43030;
  --shadow:0 1px 2px rgba(21,19,12,.04), 0 10px 28px -16px rgba(21,19,12,.22);
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
body{background:var(--paper);color:var(--ink);font-family:'Archivo',system-ui,sans-serif;font-size:16px;line-height:1.5}
.wrap{max-width:1200px;margin:0 auto;padding:0 32px}

/* ---- gold stripe ---- */
.stripe{position:relative;height:8px;background:var(--gold);overflow:hidden}
.stripe .sweep{position:absolute;top:0;left:-30%;width:30%;height:100%;background:linear-gradient(90deg,rgba(255,255,255,0),rgba(255,255,255,.85),rgba(255,255,255,0));animation:sweep 9s cubic-bezier(.5,0,.35,1) infinite}
@keyframes sweep{0%{left:-30%}55%{left:110%}100%{left:110%}}

/* ---- header ---- */
.header{display:flex;align-items:flex-start;justify-content:space-between;padding:30px 0 26px;border-bottom:1px solid var(--line);flex-wrap:wrap;gap:16px}
.header h1{font-size:clamp(26px,3.6vw,38px);font-weight:800;letter-spacing:-.03em;line-height:1.1;white-space:nowrap}
.header h1 span{color:var(--gold);-webkit-text-stroke:1.5px var(--ink)}
.header p{font-size:14.5px;color:var(--ink-2);margin-top:4px}
.stamp{
  display:inline-flex;align-items:center;gap:8px;
  border:1px solid var(--line-2);border-radius:999px;padding:6px 14px 6px 11px;
  background:var(--tint);font-family:'Azeret Mono',monospace;font-size:12px;font-weight:500;color:var(--ink-2);white-space:nowrap;
}
.stamp .dot{width:7px;height:7px;border-radius:50%;background:var(--gold);flex:none;box-shadow:0 0 0 0 rgba(255,209,53,.9);animation:pulse 2.8s ease-out infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(255,209,53,.9)}70%{box-shadow:0 0 0 8px rgba(255,209,53,0)}100%{box-shadow:0 0 0 0 rgba(255,209,53,0)}}

/* ---- toggles ---- */
.toggle-bar{display:flex;align-items:center;gap:22px;flex-wrap:wrap;padding:22px 0 0}
.toggle-group{display:flex;align-items:center;gap:10px}
.toggle-label{font-family:'Azeret Mono',monospace;font-size:12px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-3)}
.seg{display:inline-flex;border:1px solid var(--line-2);border-radius:2px;overflow:hidden;background:var(--paper)}
.seg button{font:inherit;font-size:13.5px;font-weight:600;padding:7px 16px;border:0;background:transparent;color:var(--ink-2);cursor:pointer;transition:background .15s,color .15s;white-space:nowrap}
.seg button+button{border-left:1px solid var(--line-2)}
.seg button:hover:not(:disabled):not(.on){background:var(--tint2)}
.seg button.on{background:var(--ink);color:#fff}
.seg.gold button.on{background:var(--gold);color:var(--ink)}
.seg button:disabled{color:#BAB4A2;cursor:not-allowed}
.divider-dot{width:4px;height:4px;border-radius:50%;background:var(--line-2)}

/* ---- notice ---- */
.notice{display:flex;margin-top:20px;padding:12px 16px;border-radius:3px;background:var(--tint);border:1px solid var(--line);border-left:3px solid var(--gold);font-size:14px;color:var(--ink-2)}
.notice strong{color:var(--ink);font-weight:600}

/* ---- hero cards ---- */
.hero-row{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;padding:26px 0 6px}
.hero-card{
  background:var(--paper);border:1px solid var(--line);border-radius:14px;
  padding:20px 22px 22px;position:relative;overflow:hidden;
  transition:transform .2s ease, box-shadow .2s ease, border-color .2s ease;
}
.hero-card:hover{transform:translateY(-2px);box-shadow:var(--shadow);border-color:var(--line-2)}
.hero-card::before{content:'';position:absolute;top:0;left:0;right:0;height:4px}
.hero-card[data-color="blue"]::before{background:var(--blue)}
.hero-card[data-color="teal"]::before{background:var(--teal)}
.hero-card[data-color="coral"]::before{background:var(--coral)}
.hero-card[data-color="violet"]::before{background:var(--violet)}
.phase-badge{
  display:inline-flex;align-items:center;gap:5px;
  font-family:'Azeret Mono',monospace;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  padding:3px 10px;border-radius:4px;margin-bottom:10px;
}
.phase-badge.lite{background:rgba(255,209,53,.18);color:var(--gold-deep)}
.phase-badge.heavy{background:rgba(208,96,48,.12);color:var(--coral)}
.hero-card .project-label{
  font-size:13px;font-weight:700;letter-spacing:-.01em;color:var(--ink);margin-bottom:6px;
}
.big-number{font-size:clamp(32px,4vw,44px);font-weight:800;font-variant-numeric:tabular-nums;line-height:1;margin-bottom:10px}
.hero-card[data-color="blue"] .big-number{color:var(--blue)}
.hero-card[data-color="teal"] .big-number{color:var(--teal)}
.hero-card[data-color="coral"] .big-number{color:var(--coral)}
.hero-card[data-color="violet"] .big-number{color:var(--violet)}
.dept-count{font-size:13px;color:var(--ink-3);font-weight:500}
.hero-ring{position:absolute;top:16px;right:16px;width:48px;height:48px}
.hero-ring circle{fill:none;stroke-width:5;stroke-linecap:round}
.hero-ring .track{stroke:var(--line)}
.hero-card[data-color="blue"] .hero-ring .fill{stroke:var(--blue)}
.hero-card[data-color="teal"] .hero-ring .fill{stroke:var(--teal)}
.hero-card[data-color="coral"] .hero-ring .fill{stroke:var(--coral)}
.hero-card[data-color="violet"] .hero-ring .fill{stroke:var(--violet)}

/* ---- controls bar ---- */
.controls-bar{display:flex;align-items:center;justify-content:space-between;margin-top:30px;margin-bottom:16px;flex-wrap:wrap;gap:12px}
.section-title{font-size:19px;font-weight:700;letter-spacing:-.02em}
.pill-group{display:inline-flex;border:1px solid var(--line-2);border-radius:2px;overflow:hidden;background:var(--paper)}
.pill{font:inherit;font-size:13px;font-weight:600;padding:7px 14px;border:0;background:transparent;color:var(--ink-2);cursor:pointer;transition:background .15s,color .15s}
.pill+.pill{border-left:1px solid var(--line-2)}
.pill:hover:not(.active){background:var(--tint2)}
.pill.active{background:var(--gold);color:var(--ink)}

/* ---- project cards ---- */
.project-grid{display:flex;flex-direction:column;gap:14px}
.project-card{background:var(--paper);border:1px solid var(--line);border-radius:14px;overflow:hidden;transition:border-color .2s}
.project-card:hover{border-color:var(--line-2)}
.project-card-header{display:flex;align-items:center;justify-content:space-between;padding:18px 24px;cursor:pointer;user-select:none;transition:background .12s}
.project-card-header:hover{background:var(--tint)}
.pch-left{display:flex;align-items:center;gap:14px}
.color-badge{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:800;color:#fff}
.project-card[data-project="property"] .color-badge{background:var(--blue)}
.project-card[data-project="cfi-consolidation"] .color-badge{background:var(--teal)}
.project-card[data-project="parking-consolidation"] .color-badge{background:var(--coral)}
.project-card[data-project="non-revenue-inventory"] .color-badge{background:var(--violet)}
.pch-info .pch-name{font-size:15px;font-weight:700;color:var(--ink)}
.pch-info .pch-sub{font-size:12.5px;color:var(--ink-3);margin-top:1px}
.pch-right{display:flex;align-items:center;gap:18px}
.pch-stats{display:flex;gap:14px;align-items:center}
.pch-stat{text-align:center}
.pch-stat .stat-val{font-size:16px;font-weight:800;font-variant-numeric:tabular-nums;color:var(--ink)}
.pch-stat .stat-val.g{color:var(--green)}
.pch-stat .stat-val.r{color:var(--red)}
.pch-stat .stat-label{font-family:'Azeret Mono',monospace;font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3)}
.chevron-btn{width:28px;height:28px;border-radius:8px;background:var(--tint2);border:none;display:flex;align-items:center;justify-content:center;cursor:pointer;transition:background .15s}
.chevron-btn:hover{background:var(--line)}
.chevron-btn svg{width:16px;height:16px;color:var(--ink-3);transition:transform .25s cubic-bezier(.22,1,.36,1)}
.project-card.open .chevron-btn svg{transform:rotate(180deg)}
.project-card-body{display:grid;grid-template-rows:0fr;transition:grid-template-rows .4s cubic-bezier(.22,1,.36,1)}
.project-card.open .project-card-body{grid-template-rows:1fr}
.project-card-body-inner{overflow:hidden}

/* ---- dept tiles ---- */
.dept-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;padding:4px 24px 22px}
.dept-tile{
  background:var(--tint);border:1px solid var(--line);border-radius:8px;
  padding:12px 14px;display:flex;flex-direction:column;gap:7px;
  transition:border-color .15s,background .15s;
}
.dept-tile:hover{border-color:var(--line-2);background:var(--tint2)}
.dept-tile-top{display:flex;align-items:center;justify-content:space-between}
.dept-tile-name{font-size:13px;font-weight:600;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dept-tile-pct{font-family:'Azeret Mono',monospace;font-size:13px;font-weight:700;font-variant-numeric:tabular-nums}
.dept-tile-bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden}
.dept-tile-bar-fill{height:100%;border-radius:3px;transition:width .5s cubic-bezier(.22,1,.36,1)}
.project-card[data-project="property"] .dept-tile-bar-fill{background:var(--blue)}
.project-card[data-project="property"] .dept-tile-pct{color:var(--blue)}
.project-card[data-project="cfi-consolidation"] .dept-tile-bar-fill{background:var(--teal)}
.project-card[data-project="cfi-consolidation"] .dept-tile-pct{color:var(--teal)}
.project-card[data-project="parking-consolidation"] .dept-tile-bar-fill{background:var(--coral)}
.project-card[data-project="parking-consolidation"] .dept-tile-pct{color:var(--coral)}
.project-card[data-project="non-revenue-inventory"] .dept-tile-bar-fill{background:var(--violet)}
.project-card[data-project="non-revenue-inventory"] .dept-tile-pct{color:var(--violet)}
.dept-tile.complete{border-color:rgba(22,144,78,.35)}
.dept-tile.complete .dept-tile-pct{color:var(--green) !important}
.dept-tile.complete .dept-tile-bar-fill{background:var(--green) !important}
.dept-tile.zero .dept-tile-pct{color:var(--red) !important}

/* ---- footer ---- */
footer{margin-top:46px;padding:22px 0 56px;border-top:1px solid var(--line);font-size:13px;color:var(--ink-3);line-height:1.7}
footer b{color:var(--ink-2);font-weight:600}

@media (max-width:960px){.hero-row{grid-template-columns:1fr 1fr}.wrap{padding:0 20px}}
@media (max-width:600px){.hero-row{grid-template-columns:1fr}.header h1{font-size:24px;white-space:normal}.dept-grid{grid-template-columns:1fr;padding:4px 16px 18px}.project-card-header{padding:14px 16px}.pch-stats{display:none}.toggle-bar{gap:12px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}

'''

BODY = r'''
<div class="stripe"><span class="sweep"></span></div>

<div class="wrap">
  <div class="header">
    <div>
      <h1>Governance <span>Leaderboard</span></h1>
      <p>Progress across all active DGC projects</p>
    </div>
    <div class="stamp"><span class="dot"></span><span id="stamp">Updated</span></div>
  </div>

  <div class="toggle-bar">
    <div class="toggle-group">
      <span class="toggle-label">Phase</span>
      <div class="seg gold" id="segPhase">
        <button data-v="lite">DG Lite</button>
        <button data-v="heavy">DG Heavy</button>
      </div>
    </div>
    <div class="divider-dot"></div>
    <div class="toggle-group">
      <span class="toggle-label">View</span>
      <div class="seg" id="segView">
        <button data-v="dept">Department</button>
        <button data-v="domain">Domain</button>
      </div>
    </div>
  </div>

  <div id="notice"></div>
  <div class="hero-row" id="heroRow"></div>

  <div class="controls-bar">
    <div class="section-title" id="sectionTitle">Department Breakdown</div>
    <div class="pill-group" id="segSort">
      <button class="pill" data-v="az">A&ndash;Z</button>
      <button class="pill" data-v="top">Top First</button>
      <button class="pill" data-v="need">Needs Work</button>
    </div>
  </div>

  <div class="project-grid" id="projectGrid"></div>

  <footer>
    <p>
      <b>Score</b> is weighted progress: a finished task counts full, a task in flight counts half, everything else counts zero.
      Regenerated from live ClickUp data every weekday at <b>7:00&nbsp;AM Central</b> by GitHub Actions.
    </p>
  </footer>
</div>

'''

APPJS = r'''
window.DGC_RENDER = function(DATA){
  var state = { phase:'lite', view:'dept', sort:'az' };
  var $ = function(id){ return document.getElementById(id); };

  var COLORS = {};
  DATA.projects.forEach(function(p){ COLORS[p.key] = p.color; });

  function score(a){ return a.total ? ((a.done + a.ip/2) / a.total) * 100 : 0; }
  function fmt(n){
    if(n === 0) return '0';
    if(n < 1) return n.toFixed(1);
    return Math.round(n).toString();
  }

  /* ---- hero cards ---- */
  var COLOR_MAP = {};
  var ABBREVS = {};
  DATA.projects.forEach(function(p,i){
    var palette = ['blue','teal','coral','violet'];
    COLOR_MAP[p.key] = palette[i % palette.length];
    ABBREVS[p.key] = p.short.substring(0,2).toUpperCase();
  });

  function buildHero(){
    var html = '';
    DATA.projects.forEach(function(p){
      var a = p.phases[state.phase];
      var pct = score(a);
      var circ = Math.PI * 36;
      var off = circ - (circ * pct / 100);
      var key = (state.view === 'dept') ? 'byDept' : 'byDomain';
      var items = a[key] || a.byDomain || [];
      var done = items.filter(function(r){ return r.total > 0 && r.done === r.total; }).length;
      var unit = (state.phase === 'heavy' || state.view === 'domain') ? 'domains' : 'departments';
      var color = COLOR_MAP[p.key];

      html += '<div class="hero-card" data-color="' + color + '">' +
        '<div class="phase-badge ' + (state.phase === 'lite' ? 'lite' : 'heavy') + '">' +
          (state.phase === 'lite' ? 'DG Lite' : 'DG Heavy') +
        '</div>' +
        '<div class="project-label">' + p.name + '</div>' +
        '<div class="big-number">' + fmt(pct) + '%</div>' +
        '<div class="dept-count">' + done + '/' + items.length + ' ' + unit + ' complete</div>' +
        '<svg class="hero-ring" viewBox="0 0 48 48">' +
          '<circle class="track" cx="24" cy="24" r="18"/>' +
          '<circle class="fill" cx="24" cy="24" r="18" stroke-dasharray="' + circ +
            '" stroke-dashoffset="' + off + '" transform="rotate(-90 24 24)"/>' +
        '</svg>' +
      '</div>';
    });
    $('heroRow').innerHTML = html;
  }

  /* ---- project cards with dept grids ---- */
  function sortedItems(items){
    var c = items.slice();
    if(state.sort === 'az') c.sort(function(a,b){ return a.name.localeCompare(b.name); });
    else if(state.sort === 'top') c.sort(function(a,b){ return score(b) - score(a) || a.name.localeCompare(b.name); });
    else c.sort(function(a,b){ return score(a) - score(b) || b.total - a.total || a.name.localeCompare(b.name); });
    return c;
  }

  function buildProjects(){
    var effectiveView = (state.phase === 'heavy') ? 'domain' : state.view;
    var key = (effectiveView === 'dept') ? 'byDept' : 'byDomain';
    var label = (effectiveView === 'dept') ? 'departments' : 'domains';

    $('sectionTitle').textContent = (effectiveView === 'dept') ? 'Department Breakdown' : 'Domain Breakdown';

    var html = '';
    DATA.projects.forEach(function(p){
      var a = p.phases[state.phase];
      var items = sortedItems(a[key] || a.byDomain || []);
      var done = items.filter(function(r){ return r.total > 0 && r.done === r.total; }).length;
      var zero = items.filter(function(r){ return r.done === 0 && r.ip === 0; }).length;
      var active = items.length - done - zero;
      var abbrev = ABBREVS[p.key];

      html += '<div class="project-card open" data-project="' + p.key + '">' +
        '<div class="project-card-header" onclick="this.closest(\'.project-card\').classList.toggle(\'open\')">' +
          '<div class="pch-left">' +
            '<div class="color-badge">' + abbrev + '</div>' +
            '<div class="pch-info">' +
              '<div class="pch-name">' + p.name + '</div>' +
              '<div class="pch-sub">' + items.length + ' ' + label + ' tracked</div>' +
            '</div>' +
          '</div>' +
          '<div class="pch-right">' +
            '<div class="pch-stats">' +
              '<div class="pch-stat"><div class="stat-val g">' + done + '</div><div class="stat-label">Done</div></div>' +
              '<div class="pch-stat"><div class="stat-val">' + active + '</div><div class="stat-label">Active</div></div>' +
              '<div class="pch-stat"><div class="stat-val r">' + zero + '</div><div class="stat-label">Not Started</div></div>' +
            '</div>' +
            '<button class="chevron-btn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg></button>' +
          '</div>' +
        '</div>' +
        '<div class="project-card-body"><div class="project-card-body-inner"><div class="dept-grid">';

      items.forEach(function(d){
        var s = score(d);
        var cls = (d.total > 0 && d.done === d.total) ? 'complete' : (d.done === 0 && d.ip === 0) ? 'zero' : '';
        html += '<div class="dept-tile ' + cls + '">' +
          '<div class="dept-tile-top">' +
            '<span class="dept-tile-name">' + d.name + '</span>' +
            '<span class="dept-tile-pct">' + fmt(s) + '%</span>' +
          '</div>' +
          '<div class="dept-tile-bar"><div class="dept-tile-bar-fill" style="width:' + s + '%"></div></div>' +
        '</div>';
      });

      html += '</div></div></div></div>';
    });
    $('projectGrid').innerHTML = html;
  }

  function buildNotice(){
    $('notice').innerHTML = (state.phase === 'heavy' && state.view === 'dept')
      ? '<div class="notice"><span><strong>DG Heavy runs at the domain level.</strong> ' +
        'It tracks Engineering and BI build tasks per domain, so there is no department breakdown in Heavy mode.</span></div>'
      : '';
  }

  function syncSegs(){
    [['segPhase','phase'],['segView','view'],['segSort','sort']].forEach(function(pair){
      Array.prototype.forEach.call($(pair[0]).querySelectorAll('button,[data-v]'), function(b){
        b.classList.toggle(b.classList.contains('pill') ? 'active' : 'on',
          b.getAttribute('data-v') === state[pair[1]]);
      });
    });
    var deptBtn = $('segView').querySelector('[data-v="dept"]');
    if(deptBtn) deptBtn.disabled = (state.phase === 'heavy');
  }

  function render(){
    if(state.phase === 'heavy') state.view = 'domain';
    syncSegs();
    buildHero();
    buildNotice();
    buildProjects();
  }

  /* ---- wire up ---- */
  [['segPhase','phase'],['segView','view'],['segSort','sort']].forEach(function(pair){
    $(pair[0]).addEventListener('click', function(e){
      var b = e.target.closest('button,[data-v]');
      if(!b || b.disabled) return;
      state[pair[1]] = b.getAttribute('data-v');
      render();
    });
  });

  $('stamp').textContent = 'Updated ' + DATA.generatedLabel;
  render();
};

'''

# ---------------------------------------------------------------- render

FONTS = ("https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700;800;900"
         "&family=Azeret+Mono:wght@400;500;600;700&display=swap")


def render(data):
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>Governance Leaderboard | The Scion Group</title>\n"
        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">\n"
        "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>\n"
        "<link href=\"" + FONTS + "\" rel=\"stylesheet\">\n"
        "<style>\n" + CSS + "\n</style>\n"
        "</head>\n<body>\n"
        + BODY +
        "\n<script>\n" + APPJS + "\n</script>\n"
        "<script>DGC_RENDER(" + json.dumps(data, separators=(",", ":")) + ");</script>\n"
        "</body>\n</html>\n"
    )


def stamp(now):
    return now.strftime("%b %d, %Y %I:%M %p CT").replace(" 0", " ").lstrip("0")


def main():
    token = os.environ.get("CLICKUP_API_TOKEN")
    if not token:
        sys.exit("CLICKUP_API_TOKEN is not set. Add it under repo Settings > "
                 "Secrets and variables > Actions.")

    sys.stderr.write("Reading DGC Hub...\n")
    projects = collect(token)
    if not projects:
        sys.exit("No topic folders with DG Lite / DG Heavy lists were found.")

    now = datetime.now(timezone.utc).astimezone(CENTRAL)
    data = {
        "generatedAt": now.isoformat(),
        "generatedLabel": stamp(now),
        "projects": projects,
        "totals": totals(projects),
    }
    with open(OUTPUT, "w", encoding="utf-8") as fh:
        fh.write(render(data))
    t = data["totals"]
    sys.stderr.write("Wrote %s: %d topics, %s tasks, %d in flight, %d done.\n"
                     % (OUTPUT, t["topics"], format(t["tasks"], ","),
                        t["inFlight"], t["done"]))


if __name__ == "__main__":
    main()
