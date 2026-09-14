#!/usr/bin/env python3
"""
Regenerate the DGC Governance Leaderboard (index.html) from live ClickUp data.

Run by GitHub Actions every weekday at 7:00 AM Central.
Needs one environment variable: CLICKUP_API_TOKEN (a pk_... personal token).

Everything about the design lives in this file. Editing index.html directly
will work until the next scheduled run overwrites it. Edit this instead.

Design: the three-altitude Governance Leaderboard.
  FL500 - the whole program in one number.
  FL200 - every domain rolled up across all topics, ranked.
  FL050 - one domain descended into its topics (click a domain row).

Two phase tabs:
  DG Lite  - steward governance tasks, scored by department and by domain.
  DG Heavy - Engineering and BI build tasks, domain level only.
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

# Folder name -> the name shown on the page. Unlisted topics use the folder
# name as-is, so a new topic folder works with no edit here.
DISPLAY_NAMES = {
    "Property": "Properties",
}

# Folder name -> the short label used on the FL500 readout rows.
SHORT_NAMES = {
    "Property": "Properties",
    "CFI Consolidation": "CFI",
    "Parking Consolidation": "Parking",
    "Non-Revenue Inventory": "NRI",
}

# Display order for topics. Anything not listed is appended alphabetically,
# so new topics appear without touching this list.
TOPIC_ORDER = [
    "Property",
    "CFI Consolidation",
    "Parking Consolidation",
    "Non-Revenue Inventory",
]

# The two phases, each read from the list of the same name in every topic folder.
PHASES = [
    {"id": "lite", "list": "DG Lite", "label": "DG Lite", "depts": True},
    {"id": "heavy", "list": "DG Heavy", "label": "DG Heavy", "depts": False},
]

# The full twelve-domain universe. A domain with no participating tasks in a
# phase is shown as "on the ground" rather than as 0%, because zero tasks is
# not the same thing as zero progress.
ALL_DOMAINS = [
    "Asset", "Commerce", "Customer", "Facilities", "Financials", "Investor",
    "Market", "Operations", "People", "Procurement", "Technology", "Utilities",
]

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


def is_done(task):
    """A task counts complete when its status sits in Done or Closed."""
    st = task.get("status") or {}
    return (st.get("type") or "").lower() in ("done", "closed")


def tally(tasks, field):
    """{name: {name, done, total}} for one custom field."""
    agg = {}
    for t in tasks:
        key = field_value(t, field)
        if not key:
            continue
        row = agg.setdefault(key, {"name": key, "done": 0, "total": 0})
        row["total"] += 1
        if is_done(t):
            row["done"] += 1
    return agg


def slug(name):
    keep = [c.lower() if c.isalnum() else "-" for c in name]
    return "".join(keep).strip("-")


def order_key(name):
    try:
        return (0, TOPIC_ORDER.index(name), "")
    except ValueError:
        return (1, 0, name.lower())


# ---------------------------------------------------------------- collect

def collect(token):
    """Read every topic folder's DG Lite and DG Heavy lists."""
    folders = api_get("/space/%s/folder" % SPACE_ID, {"archived": "false"}, token)
    topics = []
    for folder in folders.get("folders", []):
        lists = {l["name"]: l["id"] for l in folder.get("lists", [])}
        if not any(p["list"] in lists for p in PHASES):
            continue
        name = folder["name"]
        sys.stderr.write("  reading %s\n" % name)

        entry = {
            "folder": name,
            "key": slug(name),
            "name": DISPLAY_NAMES.get(name, name),
            "short": SHORT_NAMES.get(name, name.split()[0]),
            "phases": {},
        }
        for phase in PHASES:
            tasks = fetch_tasks(lists[phase["list"]], token) if phase["list"] in lists else []
            entry["phases"][phase["id"]] = {
                "done": sum(1 for t in tasks if is_done(t)),
                "total": len(tasks),
                "byDomain": tally(tasks, DOMAIN_FIELD),
                "byDept": tally(tasks, DEPT_FIELD) if phase["depts"] else {},
            }
        topics.append(entry)

    topics.sort(key=lambda p: order_key(p["folder"]))
    return topics


def shape_phase(topics, phase):
    """Turn one phase's tallies into the three altitudes the page renders."""
    pid = phase["id"]
    projects = [{"key": t["key"], "name": t["name"], "short": t["short"],
                 "done": t["phases"][pid]["done"], "total": t["phases"][pid]["total"]}
                for t in topics]

    seen = set()
    for t in topics:
        seen.update(k for k, v in t["phases"][pid]["byDomain"].items() if v["total"])
    known = [d for d in ALL_DOMAINS if d in seen]
    extra = sorted(d for d in seen if d not in ALL_DOMAINS)

    domains = []
    for name in known + extra:
        by_project, done, total = {}, 0, 0
        for t in topics:
            cell = t["phases"][pid]["byDomain"].get(name)
            if cell and cell["total"]:
                by_project[t["key"]] = {"done": cell["done"], "total": cell["total"]}
                done += cell["done"]
                total += cell["total"]
            else:
                by_project[t["key"]] = None
        if not total:
            continue
        domains.append({
            "name": name,
            "byProject": by_project,
            "done": done,
            "total": total,
            "live": sum(1 for v in by_project.values() if v),
        })
    domains.sort(key=lambda d: (-(d["done"] / d["total"]), d["name"]))

    departments = {}
    if phase["depts"]:
        for t in topics:
            rows = [r for r in t["phases"][pid]["byDept"].values() if r["total"]]
            rows.sort(key=lambda r: (-(r["done"] / r["total"]), r["name"]))
            departments[t["key"]] = rows

    return {
        "id": pid,
        "label": phase["label"],
        "depts": phase["depts"],
        "projects": projects,
        "domains": domains,
        "grounded": [d for d in ALL_DOMAINS if d not in seen],
        "departments": departments,
        "program": {"done": sum(p["done"] for p in projects),
                    "total": sum(p["total"] for p in projects)},
    }


def shape(topics):
    return {
        "order": [p["id"] for p in PHASES],
        "phases": {p["id"]: shape_phase(topics, p) for p in PHASES},
    }


# ---------------------------------------------------------------- design
# The whole page: stylesheet, markup, and client script. Edit here.
# Palette carried over verbatim from the Scion DGC design tokens.

CSS = r'''

:root{
  --bg:oklch(98.5% 0.005 85);
  --surface:oklch(100% 0.003 85);
  --surface-alt:oklch(96% 0.006 85);
  --text-primary:oklch(15% 0.01 85);
  --text-secondary:oklch(45% 0.01 85);
  --text-soft:oklch(52% 0.012 85);
  --text-muted:oklch(62% 0.008 85);
  --text-dim:oklch(55% 0.01 85);
  --border:oklch(90% 0.008 85);
  --border-subtle:oklch(94% 0.005 85);
  --accent:oklch(85% 0.17 85);
  --accent-deep:oklch(74% 0.16 78);
  --accent-dim:oklch(92% 0.08 85);
  --governance:oklch(58% 0.2 265);
  --governance-light:oklch(70% 0.16 265);
  --engineering:oklch(62% 0.2 30);
  --engineering-text:oklch(48% 0.18 30);
  --track:oklch(93% 0.005 85);
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
body{
  background:var(--bg);color:var(--text-primary);
  font-family:'Manrope',system-ui,sans-serif;font-size:16px;line-height:1.5;
  padding:40px 20px;
}
@media (min-width:640px){body{padding:56px 40px}}
.shell{max-width:1180px;margin:0 auto;display:flex;gap:24px}
.col{min-width:0;flex:1}
.tnum{font-variant-numeric:tabular-nums}

/* ---- altitude rail ---- */
.rail{position:relative;width:104px;flex:none;display:none}
@media (min-width:1024px){.rail{display:block}}
.rail-inner{position:sticky;top:48px;padding-left:4px}
.rail-line{position:absolute;left:10px;top:8px;bottom:8px;width:1px;background:var(--border)}
.rail-dot{
  position:absolute;left:6px;width:9px;height:9px;border-radius:50%;
  background:var(--accent);box-shadow:0 0 0 3px var(--accent-dim);
  transition:top .5s cubic-bezier(.22,1,.36,1);
}
.rail ul{list-style:none}
.rail li{padding-left:24px}
.rail li+li{margin-top:56px}
.rail .fl{font-size:13px;font-weight:800;letter-spacing:.08em;color:var(--text-dim);transition:color .3s}
.rail .nm{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--text-dim);transition:color .3s}
.rail li.on .fl{color:var(--text-primary)}
.rail li.on .nm{color:var(--text-soft)}

/* ---- header ---- */
.head{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:20px 32px;margin-bottom:32px}
.eyebrow{font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.18em;color:var(--text-soft)}
h1{font-size:38px;font-weight:800;line-height:1.05;letter-spacing:-.035em;margin-top:8px}
@media (min-width:640px){h1{font-size:46px}}
.sub{font-size:15px;font-weight:500;color:var(--text-secondary);margin-top:4px}
.tabs{display:flex;align-items:center;gap:4px;padding:4px;border-radius:999px;background:var(--surface);border:1px solid var(--border)}
.tabs button{
  font:inherit;font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:.1em;
  padding:8px 20px;border:0;border-radius:999px;background:transparent;color:var(--text-secondary);
  cursor:pointer;transition:background .15s,color .15s;white-space:nowrap;
}
.tabs button:hover:not(.on){background:var(--surface-alt)}
.tabs button.on{background:var(--text-primary);color:var(--bg)}

/* ---- bars ---- */
.bar{width:100%;border-radius:999px;background:var(--track);overflow:hidden}
.bar>span{display:block;height:100%;border-radius:999px;width:0;transition:width 1.1s cubic-bezier(.22,1,.36,1)}
.bar.governance>span{background:linear-gradient(90deg,var(--governance) 0%,var(--governance-light) 100%)}
.bar.overall>span{background:linear-gradient(90deg,var(--accent-deep) 0%,var(--accent) 100%)}
.bar.lag>span{background:linear-gradient(90deg,var(--engineering) 0%,oklch(74% 0.15 40) 100%)}
.ghost{width:100%;border-radius:999px;background-image:repeating-linear-gradient(115deg,var(--border) 0 4px,transparent 4px 9px)}

/* ---- FL500 readout ---- */
.readout{position:relative;overflow:hidden;border-radius:16px;background:var(--surface);border:1px solid var(--border);padding:28px 20px}
@media (min-width:640px){.readout{padding:32px 36px}}
.readout .sweep{
  position:absolute;top:0;bottom:0;width:33%;pointer-events:none;opacity:.55;
  background:linear-gradient(90deg,transparent,var(--accent-dim),transparent);
  animation:sweep 9s linear infinite;
}
@keyframes sweep{0%{transform:translateX(-140%)}100%{transform:translateX(340%)}}
.readout-grid{position:relative;display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:24px 40px}
.label{font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.16em;color:var(--text-soft)}
.huge{display:flex;align-items:flex-end;gap:16px;margin-top:8px}
.huge .n{font-size:86px;font-weight:800;line-height:.82;letter-spacing:-.045em;color:var(--text-primary)}
@media (min-width:640px){.huge .n{font-size:104px}}
.huge .p{max-width:15rem;padding-bottom:8px;font-size:15px;font-weight:500;line-height:1.35;color:var(--text-secondary)}
.topics{width:100%;max-width:24rem;flex:none}
.topics .rows{display:grid;gap:12px;margin-top:20px}
.trow{display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:0 16px}
.trow .nm{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text-secondary)}
.trow .track{width:112px}
@media (min-width:640px){.trow .track{width:160px}}
.trow .pc{width:40px;text-align:right;font-size:14px;font-weight:800;color:var(--text-primary)}

/* ---- section headers ---- */
.section{margin-top:40px}
.sec-head{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:4px 24px;margin-bottom:12px}
.sec-head h2{font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:.12em;color:var(--text-secondary)}
.sec-head p{font-size:13px;font-weight:500;color:var(--text-soft)}

/* ---- FL200 domain rows ---- */
.domains{overflow:hidden;border-radius:14px;background:var(--surface);border:1px solid var(--border)}
.drow+.drow{border-top:1px solid var(--border-subtle)}
.dhead{
  display:grid;grid-template-columns:1fr auto;align-items:center;gap:12px 20px;
  width:100%;padding:20px;text-align:left;border:0;background:transparent;
  font:inherit;color:inherit;cursor:pointer;transition:background .15s;
}
@media (min-width:640px){.dhead{grid-template-columns:16.5rem 1fr 5.5rem auto;padding:20px 32px}}
.dhead:hover{background:var(--surface-alt)}
.dhead:focus-visible{outline:none;background:var(--accent-dim)}
.dname{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;min-width:0}
@media (min-width:640px){.dname{flex-wrap:nowrap}}
.dname .t{font-size:21px;font-weight:800;letter-spacing:-.02em;color:var(--text-primary)}
.dname .flag{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--engineering-text);white-space:nowrap}
.dmid{order:3;grid-column:span 2}
@media (min-width:640px){.dmid{order:0;grid-column:auto}}
.dmid .cap{display:block;margin-top:8px;font-size:12px;font-weight:600;color:var(--text-soft)}
.dpct{text-align:right;font-size:30px;font-weight:800;line-height:1;letter-spacing:-.02em;color:var(--text-primary)}
.dright{display:flex;align-items:center;justify-content:flex-end;gap:16px}
.dfrac{display:none;width:64px;text-align:right;font-size:13px;font-weight:600;color:var(--text-soft)}
@media (min-width:640px){.dfrac{display:block}}
.chev{
  display:grid;place-items:center;width:28px;height:28px;flex:none;border-radius:50%;
  border:1px solid var(--border);color:var(--text-secondary);
  transition:transform .3s cubic-bezier(.22,1,.36,1);
}
.drow.open .chev{transform:rotate(180deg)}
.chev svg{width:14px;height:14px}

/* ---- FL050 descent ---- */
.descent{display:grid;grid-template-rows:0fr;transition:grid-template-rows .42s cubic-bezier(.22,1,.36,1)}
.drow.open .descent{grid-template-rows:1fr}
.descent-clip{overflow:hidden}
.descent-in{padding:20px;background:var(--surface-alt);border-top:1px solid var(--border-subtle)}
@media (min-width:640px){.descent-in{padding:20px 32px}}
.descent-in .label{margin-bottom:16px;letter-spacing:.14em}
.pgrid{display:grid;gap:16px 40px}
@media (min-width:640px){.pgrid{grid-template-columns:1fr 1fr}}
.pcell{display:grid;grid-template-columns:1fr auto;align-items:center;gap:0 16px}
.pcell .nm{font-size:14px;font-weight:700;color:var(--text-secondary)}
.pcell .pc{font-size:15px;font-weight:800;color:var(--text-primary)}
.pcell .pc.out{color:var(--text-soft)}
.pcell .line{grid-column:span 2;display:flex;align-items:center;gap:12px;margin-top:8px}
.pcell .fr{width:56px;flex:none;text-align:right;font-size:13px;font-weight:600;color:var(--text-soft)}

/* ---- on the ground ---- */
.ground{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin-top:20px}
.ground .label{letter-spacing:.12em}
.chip{border-radius:999px;padding:4px 12px;font-size:13px;font-weight:600;border:1px dashed var(--border);background:var(--surface);color:var(--text-soft)}

/* ---- departments ---- */
.dept-grid{display:grid;gap:16px;align-items:start}
@media (min-width:1024px){.dept-grid{grid-template-columns:1fr 1fr}}
.dept-card{border-radius:14px;background:var(--surface);border:1px solid var(--border);padding:20px}
@media (min-width:640px){.dept-card{padding:24px}}
.dept-card .top{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid var(--border-subtle)}
.dept-card .top h3{font-size:19px;font-weight:800;letter-spacing:-.02em;color:var(--text-primary)}
.dept-card .top .cnt{font-size:13px;font-weight:600;color:var(--text-soft)}
.dept-card .top .pc{font-size:26px;font-weight:800;line-height:1;letter-spacing:-.02em;color:var(--text-primary)}
.dept-card ul{list-style:none;display:grid;gap:16px}
.dept-row{display:grid;grid-template-columns:1fr auto;gap:0 16px}
.dept-row .nm{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--text-secondary)}
.dept-row .pc{font-size:15px;font-weight:800;color:var(--text-primary)}
.dept-row .line{grid-column:span 2;display:flex;align-items:center;gap:12px;margin-top:8px}
.dept-row .fr{width:56px;flex:none;text-align:right;font-size:13px;font-weight:600;color:var(--text-soft)}
.empty{font-size:14px;font-weight:500;color:var(--text-soft)}

/* ---- footer ---- */
footer{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px 24px;margin-top:40px;padding-top:20px;border-top:1px solid var(--border)}
footer p{font-size:13px;font-weight:500;color:var(--text-soft);max-width:62ch}
footer .badge{border-radius:999px;padding:4px 12px;font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.1em;background:var(--surface-alt);border:1px solid var(--border);color:var(--text-soft)}

@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}

/* ---- toggle bar ---- */
.bars{display:flex;flex-wrap:wrap;align-items:center;gap:14px 26px;margin-bottom:28px}
.tgroup{display:flex;align-items:center;gap:10px}
.tlabel{font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.14em;color:var(--text-soft)}
.tabs button:disabled{color:var(--text-muted);cursor:not-allowed;background:transparent}
.tabs.gold button.on{background:var(--accent);color:var(--text-primary)}
.dot-sep{width:4px;height:4px;border-radius:50%;background:var(--border);flex:none;display:none}
@media (min-width:640px){.dot-sep{display:block}}

/* ---- phase badge + notice ---- */
.pbadge{display:inline-block;border-radius:999px;padding:3px 11px;font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px}
.pbadge.lite{background:var(--accent-dim);color:oklch(42% 0.1 80)}
.pbadge.heavy{background:oklch(94% 0.045 30);color:var(--engineering-text)}
.notice{display:flex;gap:10px;margin-top:20px;padding:13px 16px;border-radius:10px;background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--accent);font-size:14px;font-weight:500;color:var(--text-secondary)}
.notice b{font-weight:800;color:var(--text-primary)}

'''


BODY = r'''
<div class="shell">
  <div class="rail" aria-hidden="true">
    <div class="rail-inner">
      <div style="position:relative">
        <div class="rail-line"></div>
        <div class="rail-dot" id="railDot" style="top:8px"></div>
        <ul id="railList">
          <li data-fl="FL500"><p class="fl">FL500</p><p class="nm">Program</p></li>
          <li data-fl="FL200"><p class="fl">FL200</p><p class="nm">Domains</p></li>
          <li data-fl="FL050"><p class="fl">FL050</p><p class="nm">In domain</p></li>
        </ul>
      </div>
    </div>
  </div>

  <div class="col">
    <header class="head">
      <div>
        <p class="eyebrow">The Scion Group &middot; Data Governance Council</p>
        <h1>Governance Leaderboard</h1>
        <p class="sub">Task-weighted, participating combinations only &middot; snapshot <span id="stamp"></span></p>
      </div>
    </header>

    <div class="bars">
      <div class="tgroup">
        <span class="tlabel">Phase</span>
        <div class="tabs gold" id="tabsPhase" role="tablist" aria-label="Choose a phase">
          <button type="button" role="tab" data-v="lite">DG Lite</button>
          <button type="button" role="tab" data-v="heavy">DG Heavy</button>
        </div>
      </div>
      <span class="dot-sep"></span>
      <div class="tgroup">
        <span class="tlabel">View</span>
        <div class="tabs" id="tabsView" role="tablist" aria-label="Choose a view">
          <button type="button" role="tab" data-v="domains">Domains</button>
          <button type="button" role="tab" data-v="departments">Departments</button>
        </div>
      </div>
    </div>

    <div class="readout">
      <div class="sweep"></div>
      <div class="readout-grid">
        <div>
          <p class="pbadge lite" id="phaseBadge">DG Lite</p>
          <p class="label">FL500 &middot; All projects</p>
          <div class="huge">
            <p class="n tnum" id="programPct">0%</p>
            <p class="p" id="programNote"></p>
          </div>
        </div>
        <div class="topics">
          <div class="bar overall" style="height:12px"><span id="programBar"></span></div>
          <div class="rows" id="topicRows"></div>
        </div>
      </div>
    </div>

    <div id="notice"></div>
    <div class="section" id="view"></div>

    <footer>
      <p id="footNote"></p>
      <p class="badge">DGC Hub</p>
    </footer>
  </div>
</div>

'''

APPJS = r'''
window.DGC_RENDER = function(DATA){
  var state = { phase:DATA.order[0], view:'domains', open:null };
  var $ = function(id){ return document.getElementById(id); };

  function phase(){ return DATA.phases[state.phase]; }
  function pct(done,total){ return total ? Math.round((done/total)*100) : 0; }
  function esc(s){
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  var WORDING = {
    lite:  { unit:'participating governance tasks',
             foot:'DG Lite counts the steward governance tasks in each participating department. ' +
                  'A task is complete when its status is in the Done or Closed category. ' +
                  'Non-participating department/domain combinations are excluded from both numerator and denominator.' },
    heavy: { unit:'Engineering and BI build tasks',
             foot:'DG Heavy counts the Engineering and BI build tasks for each domain. ' +
                  'A task is complete when its status is in the Done or Closed category. ' +
                  'DG Heavy runs at the domain level, so it has no department breakdown.' }
  };
  function wording(){ return WORDING[state.phase] || WORDING.lite; }

  /* fill every bar one frame late so the width transition actually runs */
  function animateBars(root){
    requestAnimationFrame(function(){
      Array.prototype.forEach.call(root.querySelectorAll('.bar>span[data-w]'), function(el,i){
        el.style.transitionDelay = (Math.min(i,12) * 0.06) + 's';
        el.style.width = el.getAttribute('data-w') + '%';
      });
    });
  }

  function bar(value, tone, thickness){
    return '<div class="bar ' + tone + '" style="height:' + thickness + 'px">' +
             '<span data-w="' + value + '"></span></div>';
  }
  function ghost(thickness){
    return '<div class="ghost" style="height:' + thickness + 'px"></div>';
  }

  /* ---- altitude rail ---- */
  function altitude(){
    if(state.view === 'departments') return 'FL500';
    return state.open ? 'FL050' : 'FL200';
  }
  function syncRail(){
    var fl = altitude();
    var tops = { FL500:'8px', FL200:'50%', FL050:'calc(100% - 26px)' };
    $('railDot').style.top = tops[fl];
    Array.prototype.forEach.call($('railList').children, function(li){
      li.classList.toggle('on', li.getAttribute('data-fl') === fl);
    });
  }

  /* ---- FL500 ---- */
  var countFrom = 0;
  function buildReadout(){
    var D = phase();
    var target = pct(D.program.done, D.program.total);
    var el = $('programPct'), from = countFrom, start = null, dur = 900;
    countFrom = target;
    function tick(now){
      if(start === null) start = now;
      var p = Math.min((now - start) / dur, 1);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(from + (target - from) * eased) + '%';
      if(p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);

    var badge = $('phaseBadge');
    badge.textContent = D.label;
    badge.className = 'pbadge ' + state.phase;

    $('programNote').textContent = D.program.done + ' of ' + D.program.total + ' ' +
      wording().unit + ' are done or closed.';

    var html = '';
    D.projects.forEach(function(p){
      var v = pct(p.done, p.total);
      html += '<div class="trow">' +
        '<p class="nm">' + esc(p.short) + '</p>' +
        '<div class="track">' + bar(v,'governance',5) + '</div>' +
        '<p class="pc tnum">' + v + '%</p>' +
      '</div>';
    });
    $('topicRows').innerHTML = html;
    $('programBar').style.width = '0%';
    $('programBar').setAttribute('data-w', target);
    animateBars(document.querySelector('.readout'));
  }

  /* ---- FL200 + FL050 ---- */
  function buildDomains(){
    var D = phase();
    var programPct = pct(D.program.done, D.program.total);

    var html = '<div class="sec-head">' +
        '<h2>FL200 &middot; Domains, all projects combined</h2>' +
        '<p>Click a domain to descend to project level</p>' +
      '</div><div class="domains">';

    D.domains.forEach(function(d){
      var v = pct(d.done, d.total);
      var lagging = v < programPct;
      var isOpen = state.open === d.name;
      var n = D.projects.length;

      html += '<div class="drow' + (isOpen ? ' open' : '') + '" data-domain="' + esc(d.name) + '">' +
        '<button type="button" class="dhead" aria-expanded="' + isOpen + '">' +
          '<span class="dname">' +
            '<span class="t">' + esc(d.name) + '</span>' +
            (lagging ? '<span class="flag">below program</span>' : '') +
          '</span>' +
          '<span class="dmid">' +
            bar(v, lagging ? 'lag' : 'governance', 10) +
            '<span class="cap">' + d.live + ' of ' + n + ' project' + (n === 1 ? '' : 's') + ' participating</span>' +
          '</span>' +
          '<span class="dpct tnum">' + v + '%</span>' +
          '<span class="dright">' +
            '<span class="dfrac tnum">' + d.done + '/' + d.total + '</span>' +
            '<span class="chev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></span>' +
          '</span>' +
        '</button>' +
        '<div class="descent"><div class="descent-clip"><div class="descent-in">' +
          '<p class="label">FL050 &middot; ' + esc(d.name) + ' by project</p>' +
          '<div class="pgrid">';

      D.projects.forEach(function(p){
        var cell = d.byProject[p.key];
        var cv = cell ? pct(cell.done, cell.total) : null;
        html += '<div class="pcell">' +
          '<p class="nm">' + esc(p.name) + '</p>' +
          '<p class="pc tnum' + (cell ? '' : ' out') + '">' + (cell ? cv + '%' : 'not in scope') + '</p>' +
          '<div class="line">' +
            (cell ? bar(cv,'governance',6) : ghost(6)) +
            '<span class="fr tnum">' + (cell ? cell.done + '/' + cell.total : '&mdash;') + '</span>' +
          '</div>' +
        '</div>';
      });

      html += '</div></div></div></div></div>';
    });

    html += '</div>';

    if(D.grounded.length){
      html += '<div class="ground"><span class="label">On the ground &middot; no participating combinations yet</span>';
      D.grounded.forEach(function(name){
        html += '<span class="chip">' + esc(name) + '</span>';
      });
      html += '</div>';
    }
    return html;
  }

  /* ---- departments ---- */
  function buildDepartments(){
    var D = phase();
    var html = '<div class="sec-head">' +
        '<h2>Departments &middot; by project</h2>' +
        '<p>Governance tasks only, participating domains only</p>' +
      '</div><div class="dept-grid">';

    D.projects.forEach(function(p){
      var rows = D.departments[p.key] || [];
      html += '<div class="dept-card" data-project="' + esc(p.key) + '">' +
        '<div class="top"><div>' +
          '<h3>' + esc(p.name) + '</h3>' +
          '<p class="cnt">' + rows.length + ' participating ' +
            (rows.length === 1 ? 'department' : 'departments') + '</p>' +
        '</div><p class="pc tnum">' + pct(p.done, p.total) + '%</p></div>';

      if(!rows.length){
        html += '<p class="empty">No participating departments yet.</p>';
      } else {
        html += '<ul>';
        rows.forEach(function(r){
          var v = pct(r.done, r.total);
          html += '<li class="dept-row">' +
            '<p class="nm">' + esc(r.name) + '</p>' +
            '<p class="pc tnum">' + v + '%</p>' +
            '<div class="line">' + bar(v,'governance',6) +
              '<span class="fr tnum">' + r.done + '/' + r.total + '</span>' +
            '</div>' +
          '</li>';
        });
        html += '</ul>';
      }
      html += '</div>';
    });
    return html + '</div>';
  }

  /* ---- render ---- */
  function syncTabs(){
    var D = phase();
    Array.prototype.forEach.call($('tabsPhase').children, function(b){
      var on = b.getAttribute('data-v') === state.phase;
      b.classList.toggle('on', on);
      b.setAttribute('aria-selected', on);
    });
    Array.prototype.forEach.call($('tabsView').children, function(b){
      var on = b.getAttribute('data-v') === state.view;
      b.classList.toggle('on', on);
      b.setAttribute('aria-selected', on);
      if(b.getAttribute('data-v') === 'departments'){
        b.disabled = !D.depts;
        b.title = D.depts ? '' : D.label + ' has no department breakdown';
      }
    });
  }

  function render(){
    var D = phase();
    if(!D.depts && state.view === 'departments') state.view = 'domains';

    syncTabs();

    $('notice').innerHTML = (!D.depts)
      ? '<div class="notice"><span><b>' + esc(D.label) + ' runs at the domain level.</b> ' +
        'It tracks Engineering and BI build tasks per domain, so there is no department breakdown here. ' +
        'Switch to DG Lite for the department view.</span></div>'
      : '';

    $('footNote').textContent = wording().foot +
      ' Rebuilt from live ClickUp data every weekday at 7:00 AM Central.';

    var host = $('view');
    host.innerHTML = (state.view === 'domains') ? buildDomains() : buildDepartments();

    Array.prototype.forEach.call(host.querySelectorAll('.drow'), function(row){
      row.querySelector('.dhead').addEventListener('click', function(){
        var name = row.getAttribute('data-domain');
        state.open = (state.open === name) ? null : name;
        render();
      });
    });

    syncRail();
    animateBars(host);
  }

  $('tabsPhase').addEventListener('click', function(e){
    var b = e.target.closest('button[data-v]');
    if(!b || b.disabled || b.getAttribute('data-v') === state.phase) return;
    state.phase = b.getAttribute('data-v');
    state.open = null;
    buildReadout();
    render();
  });

  $('tabsView').addEventListener('click', function(e){
    var b = e.target.closest('button[data-v]');
    if(!b || b.disabled || b.getAttribute('data-v') === state.view) return;
    state.view = b.getAttribute('data-v');
    state.open = null;
    render();
  });

  $('stamp').textContent = DATA.generatedLabel;
  buildReadout();
  render();
};

'''

# ---------------------------------------------------------------- render

FONTS = ("https://fonts.googleapis.com/css2"
         "?family=Manrope:wght@400;500;600;700;800&display=swap")


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
    return now.strftime("%a %b %d, %Y - %I:%M %p CT").replace(" 0", " ").lstrip("0")


def main():
    token = os.environ.get("CLICKUP_API_TOKEN")
    if not token:
        sys.exit("CLICKUP_API_TOKEN is not set. Add it under repo Settings > "
                 "Secrets and variables > Actions.")

    sys.stderr.write("Reading DGC Hub...\n")
    topics = collect(token)
    if not topics:
        sys.exit("No topic folders with a DG Lite or DG Heavy list were found.")

    now = datetime.now(timezone.utc).astimezone(CENTRAL)
    data = shape(topics)
    data["generatedAt"] = now.isoformat()
    data["generatedLabel"] = stamp(now)

    with open(OUTPUT, "w", encoding="utf-8") as fh:
        fh.write(render(data))

    sys.stderr.write("Wrote %s: %d topics.\n" % (OUTPUT, len(topics)))
    for pid in data["order"]:
        D = data["phases"][pid]
        sys.stderr.write(
            "  %-8s %d of %d tasks done, %d domains scored, %d on the ground.\n"
            % (D["label"], D["program"]["done"], D["program"]["total"],
               len(D["domains"]), len(D["grounded"])))


if __name__ == "__main__":
    main()
