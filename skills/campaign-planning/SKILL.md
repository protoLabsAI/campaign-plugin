---
name: campaign-planning
description: >-
  Use to turn a launch brief into a Campaign Studio plan for ANY product or app — the goal
  and the math behind its target, lanes, a shot list that says who records what, a dated
  schedule, a channel plan, a do-not list, and the decisions only the operator can make.
  Triggers: "plan the launch", "we're launching X next week", "make a campaign for",
  "what media do we need", "launch plan", "brand and launch", "plan a release campaign".
tools: [campaign_create, campaign_update, campaign_get, campaign_lane, campaign_asset_add, campaign_milestone, campaign_decision, campaign_status, campaign_limits, social_brand_kit, web_search, fetch_url, current_time, show_component]
---

# Brief → plan

The plan is the contract between you and the operator. Everything after it — scripts, takes,
renders, copy — is checked against it, so it has to be specific, and every number in it has
to be either the operator's or sourced.

## 0. Read before writing

- `current_time()` — every date in the plan is relative to today, not your training data.
- `social_brand_kit()` if Social Studio is enabled — who the audience is, the proof points you
  may cite, the words to avoid. If it isn't enabled, ask the operator for the one-line
  positioning and the audience instead of inventing them.
- Open the target app (agent_browser's `browser_open` + `browser_snapshot`) to see what
  actually exists. A plan that films a feature that isn't shipped is a plan that slips.

## 1. Goal & math — never invent a number

`campaign_create(name, product, goal, target_metric, launch_window, target_url, …)`.

- **One target metric.** "GitHub stars in 14 days", "installs from the README", "waitlist
  signups". Not three.
- **goal_math** shows how the target was derived: baseline × lift, or funnel steps. If you
  don't have the baseline, write `baseline: UNKNOWN — ask operator` and make it a decision.
  A plausible-looking number you made up is worse than a blank, because people plan around it.
- **assumptions**: one per line, each with a source URL and the date you read it —
  `- Launch posts on <surface> get most of their traffic in the first ~24h (source: <url>, read 2026-10-01)`.
  Trending/launch mechanics (when a list resets, what ranks) are *researched* notes: search,
  read the primary source, date it. Two independent sources or it's labelled as a guess.

## 2. Lanes — one angle each

`campaign_lane(campaign_id, name, pitch, audience)` for 2–4 lanes. A lane is an angle a
specific audience cares about ("plugin authors: install from a URL in one click"), not a
channel. Each lane gets ONE hero asset later (`hero_asset_id`).

## 3. Shot list — who records what

For every lane, `campaign_asset_add(campaign_id, kind, title, lane, spec, owner, limit_id)`:

- `kind`: clip (mp4), gif, still, card, copy_ref (a Social Studio queue post — put its id in spec).
- `spec`: the beat in one sentence, what must be legible, the intended aspect, and where it
  ships. "15–30s hero" style length targets are fine as *intent*, labelled as such.
- `owner`: **agent** for anything a deterministic browser take can show; **operator** for
  anything that needs a human (a face, a voice, a physical device, a logged-in third-party
  account). Say why when it's the operator.
- `limit_id`: the hard limit it must meet where it ships (`campaign_limits` lists them with
  sources). Only hard limits — don't encode a soft norm as a limit.

## 4. Schedule

`campaign_milestone(campaign_id, title, date, workstream, owner)` for: scripts written, takes
recorded, renders reviewed, copy drafted (Social Studio), operator approvals, launch day,
day-after check. Put the operator's approvals on their own milestones — that's where launches stall.

## 5. Channel plan + do-not list

`campaign_update(campaign_id, channel_plan=…, do_not=…)`.

- **channel_plan**: lane → where it ships → which asset → which copy (by Social Studio post id
  once drafted). Copy itself is Social Studio's job (`social_*` tools); don't draft it here.
- **do_not**: the lines this campaign won't cross — e.g. no fabricated metrics, no screenshots
  with real user data, no posting from the agent, no claims not in the brand kit's proof points.

## 6. Decisions for the operator

Everything you can't decide alone becomes `campaign_decision(campaign_id, question, options,
recommendation)` — with your recommendation and its reason. Typical: the target number when
there's no baseline, which lane leads, launch date, whether a paid-plan video limit applies.
When they answer in chat, record it: `campaign_decision(campaign_id, decision_id=N, answer=…)`.

## 7. Show it

`campaign_status(campaign_id)` → render its payloads with `show_component("keyvalue", …)` and
`show_component("table", …)`. For a narrative plan document, render `campaign_get` as an
artifact (`show_artifact`). Then ask for the open decisions — one message, numbered.
