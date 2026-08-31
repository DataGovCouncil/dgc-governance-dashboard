# DGC Governance Leaderboard

A single-page leaderboard showing data governance progress across every active
DGC Hub topic. Hosted free on GitHub Pages, refreshed automatically from ClickUp.

## What's in here

| File | What it is |
| --- | --- |
| `index.html` | The dashboard. This is the actual web page. Generated, do not hand-edit. |
| `update.py` | Reads ClickUp and rewrites `index.html`. **The design lives in this file.** |
| `.github/workflows/update-dashboard.yml` | The robot's schedule: every day at 7:00 AM Central. |

## One-time setup

1. Push these files to a repo (`dgc-dashboard`).
2. **Settings > Secrets and variables > Actions > New repository secret**
   Name: `CLICKUP_API_TOKEN`  ·  Value: your `pk_...` token from
   ClickUp > avatar > Settings > Apps.
3. **Settings > Actions > General > Workflow permissions** > Read and write permissions > Save.
4. **Settings > Pages** > Deploy from a branch > `main` / `(root)` > Save.
5. **Actions tab > Update DGC Governance Leaderboard > Run workflow** to test it now.

Your URL will be `https://YOURUSERNAME.github.io/dgc-dashboard/`.

## How it reads your data

`update.py` walks the DGC Hub space (`90176662768`), finds every folder that
contains a `DG Lite` or `DG Heavy` list, and pulls every top-level task with its
`Department` and `Domain` custom fields. **New topics appear automatically** the
next morning. No code change needed.

Task statuses collapse into three buckets:

- **Done** — any status in ClickUp's Done or Closed category
- **In flight** — any active status (In Progress, In Review, Submitted, Testing...)
- **Open** — To Do, Not Started, Backlog

## Score

Score is weighted progress, not raw completion:

```
score = (done + in_flight / 2) / total
```

A finished task counts full, a task in flight counts half. This keeps the board
moving while work is underway instead of sitting at 0% until the very end.

## Changing the design

Everything visual is inside `update.py`, in the `CSS`, `BODY`, and `APPJS`
strings near the middle of the file. Edit there and rerun the workflow.
Editing `index.html` directly works until 7:00 AM tomorrow, when it gets
overwritten.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Numbers never change | Actions tab shows a red X. Usually the secret name isn't exactly `CLICKUP_API_TOKEN`, or workflow permissions are still read-only. |
| 404 on the Pages URL | Wait 2-3 minutes, then confirm `index.html` sits at the top level of the repo. |
| Actions tab is empty | The workflow file must be at `.github/workflows/update-dashboard.yml` exactly. |
| Runs an hour early in winter | Change the cron from `0 12` to `0 13`. UTC doesn't observe daylight saving. |
