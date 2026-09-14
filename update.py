#!/usr/bin/env python3
"""
Regenerate the DGC Governance Leaderboard (index.html) from live ClickUp data.

Run by GitHub Actions every weekday at 7:00 AM Central.
Needs one environment variable: CLICKUP_API_TOKEN (a pk_... personal token).

Everything about the design lives in this file. Editing index.html directly
will work until the next scheduled run overwrites it. Edit this instead.

Design: the Governance Leaderboard.
  A program readout at the top, a ranked domain list below it, and each
  domain opens to show its topics. Departments sit on a toggle.

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

try:
    from zoneinfo import ZoneInfo
except ImportError:           # Python < 3.9
    ZoneInfo = None

# ---------------------------------------------------------------- settings

SPACE_ID = "90176662768"          # DGC Hub
API = "https://api.clickup.com/api/v2"
OUTPUT = "index.html"

# Folder name -> the name shown on the page. Unlisted topics use the folder
# name as-is, so a new topic folder works with no edit here.
DISPLAY_NAMES = {
    "Property": "Properties",
}

# Folder name -> the short label used on the program readout rows.
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

# Real Chicago time, so the stamp stays correct across daylight saving.
# Falls back to a fixed CDT offset only if the tz database is missing.
try:
    CENTRAL = ZoneInfo("America/Chicago") if ZoneInfo else timezone(timedelta(hours=-5))
except Exception:
    CENTRAL = timezone(timedelta(hours=-5))

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


# ---------------------------------------------------------------- logo
# DGC mark, embedded so index.html stays a single self-contained file.
# White background removed; shown at 104px tall, stored at 2.5x for sharp
# rendering on high-resolution screens.
LOGO = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAALkAAAEECAMAAABDUGVxAAADAFBMVEVHcExih5sBMGYALmQCDUkjQXd/rJzv//wrNnxYZYz+/v/m/fcBMGb39vf6+/sALGICMWd0hKX07/P9/v7o6u8BYbdAXIm9ydYAgJfj6O/9/P60wMlYRJBEYY00RJL+/v12rT50qT2YoL+iqshnTptMZI4hQnN0gKa8wNACZ7wAe5MzTnt8qUuOnLyGmry0uM5LYox5j7QoSHcwRpUBMmowTnyrvNCPmLozUX+ZpMAEPHhTaZFnd6EaPGgCQoEpS3wyUH5wXaEpSnptgKZbb5au0ZUCUaB8iq1CU6BjSJShyN0CSZH09vlabpphdZxjdp0BLWA9n3OCdrBxpTqJgrU4VYEFWYk/W4hfmMyWnrxAVoNUP4snRnRQa5dxgqdDnq4lRXQdQnVgo8KJuNFBW4UfYbAyUYFHZpBDSZqDj7BOSpsvTXxOpGZ0rUYaOmgheMYFTYOSkbp4s8tSkc1vXKAnlHeYxHVKjcsleMKTv2tKY4sWkYRlr7hseKdIYoskRXOjyoeEtVNuqENYaalGo588hMY0fsSDtlQ5VIEuV6qHgLamzYyMu2Fdb5OIuVpsdalTpbJLYo1PpLKCsNR5bKlXksyXwnDa5eBldq5+a6sxlahVZaUZhps2gsgFjI+FtllEXY0ikqYqkKJzYqGMul8EZpCTv2w7nZc6Un5FVZxKXKVPXaJfV6YWOWo+WYVup80sZJo+ZrSQwoTU2+QneLVMd61SiKstiqIrcLc2Un9aqXogZJlrs4cVPXAyS3Mlj6EYVprR29pAX49FooYBMGUBMWcBMWYBMmgBL2QBMGZ0qT0AY7lyqDsAYbgzQ5ABfpYBMGhWQo40RZMALGIAfZQAZLsBgJk2RZQLNWoBL2dhRo9aRJAyVIcBgJYDM2oRO3EDNG5XRJFeRI8ALmYdQHIEasMUOmwWP3Z6tEAGOHIyRpmBukoIiaBaR5kgRXoOcspgTJ0Fg55vVqIAKVw4TaAZbsARjqYgSoR9skyLvFdNVqkTl5kuP40KcJcABGVHcEybVZ5MAAABAHRSTlMABP78AQQDAQMCFAL8DQX9/l4RBxr8ry38Ih0l/K/8Cv79Rzv+ivFBGPv8kP0wP0WYTt/+/N01U8Qg/nlS6v7w0db6b2NA/mn+/j7+KKSVevv+nvyI2v7PeVtm/P2ygbbp+GlOwPzpwvxc/rb+/vf8/mZctO/9hqHsm7n9hqanzGT9/crPwtDwsv50UNfE44yl65Ztt4uxOL3N3OP84v3N2Pntv7n+yJj34fDL/digktj7dGuxmWXJ9Kfp7LF19tb9Uv7R//////////////////////////////////////////////////////////////////////////////8AceFEtgAAOAxJREFUeNrUmV1IG+kax9+Yj5lxRh012fhBERRdG4itZRo1FZW6KNRWvTjGFrR+tUpZ2PXYi5Yi3av24ty0p0s5IC3sKZRzrlovtLuLcE4vMjMZgjgQDQmUSSBxQ0AUKeqVcJ53JmqsiaR7qtUnyUAmycxv/vN/n/d5nyD05YNEiCi+kZ9fZ9benJYgDKikq74qFgjEmsfyCxF1asAR6mp2KhFZyfLI4VBLLTKeEnCq8E3Ix3EsL/Kiwul90UunA91A0r+HGVbiRZEHcpFn8kKXToXXSVQZ50ReEjA6hudZfaz2FKCTqCbKiILE8xhaCy48WkARJ52com/5GMEtaNTaVuHiXSdedApZV/Mkj5sXVGpBE539eKEAnXDRjajLyUhut6BBa5oLQl60HJlOrtwmAtv8TJgTsNhCQm81mFAr0kHCpE+c8ISJTExC9C8yq4LzCbPASxS5eGUip1O6EzSjUkYVpvhGNaJR7t9lRtrTXL0EyOmY3IAaHIWqpSjDSVBb1bCkrrO3efUGogx0b4SRJEEQdkanqnt3AOZRkujc6ulvt2rwX1tubJLi9gfNsXgkwkDqM6KHHr3kFjD6bnYR3N0B7HP6VlgRQ329Nx00jAjTVxSeBOHMf+3/EFOCHMdw3O/IaEQ3A3pe1Xw3BEnwRSsgt+QMcvA9Md601XuzGAv/lcYrltvRObrlDAMPJ/uC8Z4CgwlZ+/QKD36RtMEZxPKzvp5Cgw7VbSmyEo7H4+Gw80P/SC4c4yuw43td3g/YwMFFwvHYT2++s9MGRBGQFjE5To1YeMktqUmRNBDVdZX1zbEwZJp43AnCt5cd/7KDAp/Ujm05406nMy4Ghuq77EBBEHgSta/KLEyifEJ4QGeCvWWJuoWuzn84GIqHnfiH49Otx8xOwMnKxzqa8OmdTbH6LmtSciRRV6A7S5CDILkgSUFZ0n+8bFfX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"


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
.shell{max-width:1180px;margin:0 auto}
.tnum{font-variant-numeric:tabular-nums}

/* ---- header ---- */
.head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:20px 32px;margin-bottom:32px}
.brand{height:104px;width:auto;flex:none;display:block}
@media (max-width:600px){.brand{height:76px}}
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

/* ---- program readout ---- */
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

/* ---- domain rows ---- */
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

/* ---- open domain panel ---- */
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
  <header class="head">
    <div>
      <p class="eyebrow">The Scion Group &middot; Data Governance Council</p>
      <h1>Governance Leaderboard</h1>
      <p class="sub">Task-weighted, participating combinations only &middot; snapshot <span id="stamp"></span></p>
    </div>
    <img class="brand" id="brand" alt="Data Governance Council">
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
        <p class="label">All projects</p>
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

  /* ---- program readout ---- */
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

  /* ---- domains + open panel ---- */
  function buildDomains(){
    var D = phase();
    var programPct = pct(D.program.done, D.program.total);

    var html = '<div class="sec-head">' +
        '<h2>Domains, all projects combined</h2>' +
        '<p>Click a domain to see it by project</p>' +
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
          '<p class="label">' + esc(d.name) + ' by project</p>' +
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
      ' Refreshed automatically from ClickUp once a day, seven days a week. ' +
      'The snapshot time above is the last completed refresh.';

    var host = $('view');
    host.innerHTML = (state.view === 'domains') ? buildDomains() : buildDepartments();

    Array.prototype.forEach.call(host.querySelectorAll('.drow'), function(row){
      row.querySelector('.dhead').addEventListener('click', function(){
        var name = row.getAttribute('data-domain');
        state.open = (state.open === name) ? null : name;
        render();
      });
    });

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

  $('brand').src = DATA.logo;
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
    data["logo"] = LOGO

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
