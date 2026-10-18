"""The Campaign Studio console view — an asset GALLERY, for browsing and nothing else.

Chat is where campaigns are planned and produced. This page does the one thing a chat
transcript can't: show every clip, GIF, still and card of a campaign at once, playable,
with its status, size and dimensions — and give the operator the approve/reject buttons
that only they hold.

Four-rules-compliant (plugin-views guide): served PUBLIC at ``/plugins/campaign/view``;
every byte of data and media comes from the GATED ``/api/plugins/campaign/*`` through the
DS kit's slug-aware authed fetch (media become blob: URLs, so nothing is ever public);
themed only from ``--pl-*`` tokens.
"""

from __future__ import annotations

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Campaign Studio</title>
<style>
  html,body{margin:0;background:var(--pl-color-bg,#111);color:var(--pl-color-fg,#eee);
    font-family:var(--pl-font-sans,system-ui);font-size:13px}
  .wrap{padding:var(--pl-space-4,12px) var(--pl-space-5,16px)}
  header{display:flex;align-items:center;gap:var(--pl-space-3,8px);flex-wrap:wrap;margin-bottom:var(--pl-space-3,10px)}
  h1{font-size:15px;font-weight:600;margin:0}
  .spacer{flex:1}
  select,input,textarea,button{font:inherit;font-size:12px;color:var(--pl-color-fg,#eee);
    background:var(--pl-color-bg-subtle,#181818);border:1px solid var(--pl-color-border,#2a2a2a);
    border-radius:var(--pl-radius-sm,6px);padding:4px 8px}
  button{cursor:pointer}
  button:hover{border-color:var(--pl-color-accent,#9b87f2)}
  button.ok{border-color:var(--pl-color-success,#30a46c);color:var(--pl-color-success,#30a46c)}
  button.no{border-color:var(--pl-color-danger,#e5484d);color:var(--pl-color-danger,#e5484d)}
  .chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:var(--pl-space-4,12px)}
  .chip{border:1px solid var(--pl-color-border,#2a2a2a);border-radius:999px;padding:2px 10px;cursor:pointer;
    font-size:11px;color:var(--pl-color-fg-muted,#999);background:transparent}
  .chip.on{color:var(--pl-color-fg,#eee);border-color:var(--pl-color-accent,#9b87f2);
    background:color-mix(in srgb, var(--pl-color-accent,#9b87f2) 14%, transparent)}
  .chip b{font-weight:600;margin-left:4px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:var(--pl-space-3,10px)}
  .card{background:var(--pl-color-bg-subtle,#181818);border:1px solid var(--pl-color-border,#2a2a2a);
    border-radius:var(--pl-radius-md,8px);overflow:hidden;display:flex;flex-direction:column}
  .prev{aspect-ratio:16/10;background:var(--pl-color-bg,#111);display:flex;align-items:center;justify-content:center;
    color:var(--pl-color-fg-muted,#999);font-size:11px}
  .prev img,.prev video{width:100%;height:100%;object-fit:contain;display:block}
  /* the chat's image chrome (DS MarkdownImage: .pl-markdown image-wrapper + .pl-md-img-actions),
     fitted to the card's preview box; the kit CSS supplies the action cluster + Lightbox */
  .prev.pl-markdown [data-streamdown="image-wrapper"]{display:block;width:100%;height:100%;margin:0}
  .prev.pl-markdown [data-streamdown="image"]{width:100%;height:100%;object-fit:contain;border:0;border-radius:0;cursor:zoom-in}
  .prev .pl-md-img-actions svg{width:14px;height:14px;display:block}
  .newtab-note a{color:var(--pl-color-accent,#9b87f2)}
  .body{padding:8px 10px;display:flex;flex-direction:column;gap:6px}
  .title{font-weight:600;line-height:1.3;word-break:break-word}
  .meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center;font-size:11px;color:var(--pl-color-fg-muted,#999);
    font-variant-numeric:tabular-nums}
  .st{border:1px solid var(--pl-color-border,#2a2a2a);border-radius:999px;padding:1px 7px;font-size:10px;letter-spacing:.03em}
  .st.ready_for_review{color:var(--pl-color-warning,#f5a524);border-color:var(--pl-color-warning,#f5a524)}
  .st.approved{color:var(--pl-color-success,#30a46c);border-color:var(--pl-color-success,#30a46c)}
  .st.rejected{color:var(--pl-color-danger,#e5484d);border-color:var(--pl-color-danger,#e5484d)}
  .st.stale{color:var(--pl-color-danger,#e5484d);border-color:var(--pl-color-danger,#e5484d);border-style:dashed}
  .st.archived{color:var(--pl-color-success,#30a46c);border-color:var(--pl-color-success,#30a46c);border-style:dashed}
  .st.superseded{color:var(--pl-color-fg-muted,#999);border-style:dashed;text-decoration:line-through}
  .card.superseded{opacity:.55}
  .toggle{display:flex;align-items:center;gap:4px;font-size:11px;color:var(--pl-color-fg-muted,#999);cursor:pointer}
  .toggle input{margin:0}
  .st.rendered,.st.captured{color:var(--pl-color-accent,#9b87f2);border-color:var(--pl-color-accent,#9b87f2)}
  .when{cursor:help;border-bottom:1px dotted var(--pl-color-border,#2a2a2a)}
  .warn{color:var(--pl-color-danger,#e5484d);font-size:11px}
  .note{color:var(--pl-color-fg-muted,#999);font-size:11px;white-space:pre-wrap}
  .actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:2px}
  .reject{display:none;flex-direction:column;gap:6px}
  .card.rejecting .reject{display:flex}
  .supersede{display:none;flex-direction:column;gap:6px}
  .card.superseding .supersede{display:flex}
  .reject textarea{min-height:44px;resize:vertical}
  .empty{color:var(--pl-color-fg-muted,#999);padding:16px 2px}
  #err{display:block;margin-bottom:10px}
  [hidden]{display:none !important}
</style>
<script>
  var BASE = location.pathname.split("/plugins/")[0];
  (function(){ var l=document.createElement("link"); l.rel="stylesheet";
    l.href=BASE+"/_ds/plugin-kit.css"; document.head.appendChild(l); })();
</script>
</head><body><div class="wrap">
  <header>
    <h1>Campaign Studio</h1>
    <select id="campaign" aria-label="Campaign"></select>
    <select id="lane" aria-label="Lane"><option value="">All lanes</option></select>
    <select id="kind" aria-label="Kind">
      <option value="">All kinds</option><option>clip</option><option>gif</option>
      <option>still</option><option>card</option><option>montage</option><option>copy_ref</option>
    </select>
    <select id="sort" aria-label="Sort">
      <option value="newest">Newest first</option><option value="oldest">Oldest first</option>
      <option value="id">Asset #</option>
    </select>
    <label class="toggle" title="Takes a retake replaced, and rows whose file retention pruned — out of the review queue and the counts">
      <input type="checkbox" id="show-superseded"> Show superseded <span id="sup-n"></span></label>
    <label class="toggle" title="Campaigns set aside with campaign_archive — their assets are untouched">
      <input type="checkbox" id="show-archived"> Show archived <span id="arch-n"></span></label>
    <span class="spacer"></span>
    <button id="refresh" title="Reload">Refresh</button>
  </header>
  <div id="err" class="pl-callout pl-callout--error" hidden></div>
  <div class="chips" id="chips"></div>
  <div class="grid" id="grid"></div>
  <div class="empty" id="empty" hidden></div>
</div>
<script type="module">
  let kit;
  try { kit = await import(BASE + "/_ds/plugin-kit.js"); }
  catch (e) { kit = { initPluginView(){}, apiFetch: (p, i) => fetch(BASE + p, i) }; }

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
  const fmtBytes = (n) => !n ? "" : n < 1e6 ? (n/1e3).toFixed(0) + " KB" : (n/1e6).toFixed(2) + " MB";
  const state = { campaign: "", lane: "", kind: "", status: "", sort: "newest", showSuperseded: false, showArchived: false, data: null };
  // ── time ── made_at = when the FILE was produced (shot/render folder stamp, else mtime);
  // created_at = when the row was planned. Sort and "x ago" use made_at, falling back to created_at.
  const ts = (iso) => (iso ? Date.parse(iso) : NaN);
  const madeTs = (a) => { const t = ts(a.made_at); return Number.isNaN(t) ? ts(a.created_at) : t; };
  const absTime = (iso) => { const t = ts(iso); return Number.isNaN(t) ? "" :
    new Date(t).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" }); };
  function span(ms){
    const s = Math.abs(ms) / 1000;
    if (s < 60) return Math.round(s) + " s";
    if (s < 3600) return Math.round(s / 60) + " min";
    if (s < 86400) return Math.round(s / 3600) + " h";
    return Math.round(s / 86400) + " d";
  }
  function relTime(t, now = Date.now()){
    if (Number.isNaN(t)) return "";
    const d = now - t;
    if (d < 45000) return "just now";   // also a clock a few seconds ahead
    if (d >= 30 * 86400000) return new Date(t).toLocaleDateString(undefined, { dateStyle: "medium" });
    return span(d) + " ago";
  }
  const relDelta = (ms) => Math.abs(ms) < 1000 ? "same time" : span(ms) + (ms > 0 ? " later" : " earlier");
  function whenTitle(a){
    const lines = [];
    if (a.made_at) lines.push(`${a.take_at ? "Rendered" : "Made"} ${absTime(a.made_at)}` +
      (a.made_from === "mtime" ? " (file time)" : ""));
    if (a.take_at) lines.push(`Take shot ${absTime(a.take_at)}`);
    if (a.created_at && (!a.made_at || Math.abs(ts(a.made_at) - ts(a.created_at)) >= 60000))
      lines.push(`Planned ${absTime(a.created_at)}`);
    return lines.join("\n");
  }
  // Superseded takes (a retake replaced them) are hidden unless the toggle is on, and never
  // count toward the review queue or the totals.
  // Pruned tombstones (retention deleted the file; the row stays so the chain still reads) are
  // out of play the same way.
  const retired = (a) => a.status === "superseded" || a.pruned;
  const blobs = new Map();   // asset id → object URL (revoked on campaign switch)

  async function api(path, init){
    const r = await kit.apiFetch("/api/plugins/campaign" + path, init);
    if (!r.ok) { let m = r.status + ""; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
    return r;
  }

  function showErr(msg){ const e = document.getElementById("err"); e.hidden = !msg; e.textContent = msg || ""; }

  // Only the newest load may write state: a slow response for a campaign (or archive toggle) the
  // operator has already left must not overwrite the one they are looking at.
  let loadGen = 0;
  async function loadCampaigns(){
    const gen = ++loadGen;
    const { campaigns, archived_hidden } = await api(`/campaigns${state.showArchived ? "?include_archived=true" : ""}`).then(r => r.json());
    if (gen !== loadGen) return;
    document.getElementById("arch-n").textContent = archived_hidden ? `(${archived_hidden})` : "";
    const sel = document.getElementById("campaign");
    sel.innerHTML = campaigns.map(c =>
      `<option value="${esc(c.id)}">${c.archived ? "[archived] " : ""}${esc(c.name)} — ${c.approved}/${c.assets} approved${c.review ? ` · ${c.review} to review` : ""}</option>`).join("");
    if (!campaigns.length) {
      state.data = null; document.getElementById("grid").innerHTML = ""; document.getElementById("chips").innerHTML = "";
      document.getElementById("empty").hidden = false;
      document.getElementById("empty").textContent = archived_hidden
        ? "Every campaign is archived — tick Show archived to browse them." : "No campaigns yet. Ask the agent to plan one.";
      return;
    }
    if (!state.campaign || !campaigns.some(c => String(c.id) === state.campaign)) state.campaign = String(campaigns[0].id);
    sel.value = state.campaign;
    await loadAssets();
  }

  async function loadAssets(){
    const gen = ++loadGen;
    const data = await api(`/campaigns/${state.campaign}/assets`).then(r => r.json());
    if (gen !== loadGen) return;
    state.data = data;
    const lane = document.getElementById("lane");
    lane.innerHTML = `<option value="">All lanes</option>` +
      state.data.lanes.map(l => `<option value="${esc(l.id)}">${esc(l.name)}</option>`).join("");
    lane.value = state.lane;
    render();
  }

  function visible(){
    const order = {
      newest: (x, y) => (madeTs(y) || 0) - (madeTs(x) || 0) || y.id - x.id,
      oldest: (x, y) => (madeTs(x) || 0) - (madeTs(y) || 0) || x.id - y.id,
      id: (x, y) => x.id - y.id,
    }[state.sort] || ((x, y) => x.id - y.id);
    return (state.data?.assets || []).filter(a =>
      (!state.lane || String(a.lane_id) === state.lane) &&
      (!state.kind || a.kind === state.kind) &&
      (!state.status || a.status === state.status) &&
      (state.showSuperseded || state.status === "superseded" || !retired(a))).sort(order);
  }

  function chips(){
    const all = (state.data?.assets || []).filter(a =>
      (!state.lane || String(a.lane_id) === state.lane) && (!state.kind || a.kind === state.kind));
    const counts = {};
    for (const a of all) if (state.showSuperseded || !retired(a) || a.status === "superseded") counts[a.status] = (counts[a.status] || 0) + 1;
    const nSup = all.filter(retired).length;
    document.getElementById("sup-n").textContent = nSup ? `(${nSup})` : "";
    const statuses = state.data.statuses.filter(s => s !== "superseded" || state.showSuperseded);
    if (!state.showSuperseded && state.status === "superseded") state.status = "";
    const el = document.getElementById("chips");
    el.innerHTML = [["", "All", state.showSuperseded ? all.length : all.length - nSup], ...statuses.map(s => [s, s.replace(/_/g, " "), counts[s] || 0])]
      .map(([v, label, n]) => `<button class="chip ${state.status === v ? "on" : ""}" data-status="${v}">${esc(label)}<b>${n}</b></button>`).join("");
  }

  function card(a){
    const el = document.createElement("div");
    el.className = "card" + (retired(a) ? " superseded" : ""); el.dataset.id = a.id;
    const all = state.data?.assets || [];
    const byId = (i) => all.find(x => x.id === i);
    // The retake chain, read by time: "#42 · 3 min later" / "replaces #41 · 2 h earlier".
    const rel = (o) => { const d = madeTs(o) - madeTs(a); return Number.isNaN(d) ? "" : ` · ${relDelta(d)}`; };
    const supBy = (a.superseded_by || []).map(i => { const o = byId(i); return "#" + i + (o ? rel(o) : ""); }).join(", ");
    const replaces = all.filter(o => (o.superseded_by || []).includes(a.id)).map(o => "#" + o.id + rel(o)).join(", ");
    const made = madeTs(a);
    const when = Number.isNaN(made) ? "" :
      `<span class="when" data-ts="${made}" data-pre="${a.take_at ? "rendered " : ""}" title="${esc(whenTitle(a))}">` +
      `${a.take_at ? "rendered " : ""}${esc(relTime(made))}</span>`;
    const dims = a.width ? `${a.width}×${a.height}` : "";
    const dur = a.duration_s ? `${a.duration_s.toFixed(1)}s` : "";
    const violated = /^(VIOLATES|DRAFT INPUTS)/.test(a.notes || "");
    el.innerHTML =
      `<div class="prev">${a.has_file ? "loading…" : a.pruned ? "file pruned (retention)" : (a.path ? "file unavailable" : "no file yet")}</div>` +
      `<div class="body">` +
        `<div class="title">${esc(a.title || "(untitled)")}</div>` +
        `<div class="meta"><span class="st ${esc(a.status)}">${esc(a.status.replace(/_/g, " "))}</span>` +
          (a.stale ? `<span class="st stale" title="Waiting for review 14+ days — never auto-deleted">stale</span>` : "") +
          (a.archived ? `<span class="st archived" title="${esc("Backed up to protoContent (" + a.archived.release_tag + ") " + absTime(a.archived.at))}">backed up</span>` : "") +
          `${when}<span>${esc(a.kind)}</span><span>#${a.id}</span>${dims ? `<span>${dims}</span>` : ""}` +
          `${dur ? `<span>${dur}</span>` : ""}${a.size_bytes ? `<span>${fmtBytes(a.size_bytes)}</span>` : ""}` +
          `${a.limit_id ? `<span title="hard limit">≤ ${esc(a.limit_id)}</span>` : ""}</div>` +
        (a.notes ? `<div class="${violated ? "warn" : "note"}">${esc(a.notes)}</div>` : "") +
        (a.status === "superseded" ? `<div class="note">Superseded${supBy ? ` by ${esc(supBy)}` : ""}${a.pruned ? " · file pruned" : ""}</div>` : "") +
        (a.pruned && a.status !== "superseded" ? `<div class="note">File pruned by retention — the row stays because another asset names it</div>` : "") +
        (replaces ? `<div class="note">Replaces ${esc(replaces)}</div>` : "") +
        (a.review_note ? `<div class="note">Review: ${esc(a.review_note)}</div>` : "") +
        `<div class="actions">` +
          (a.can_review && a.status !== "approved" ? `<button class="ok" data-act="approve">Approve</button>` : "") +
          (a.can_review && a.status !== "rejected" ? `<button class="no" data-act="reject">Reject…</button>` : "") +
          (a.status === "approved" ? `<button data-act="supersede">Supersede…</button>` : "") +
          (a.path ? `<button data-act="copy" title="relative to the campaign media folder">Copy path</button>` : "") +
        `</div>` +
        `<div class="reject"><textarea placeholder="What should change? (the agent reads this)"></textarea>` +
          `<div class="actions"><button class="no" data-act="reject-confirm">Reject</button>` +
          `<button data-act="reject-cancel">Cancel</button></div></div>` +
        `<div class="supersede"><input placeholder="Replaced by asset id(s), e.g. 42 43" aria-label="Replaced by">` +
          `<textarea placeholder="Why it's retired (optional)"></textarea>` +
          `<div class="actions"><button data-act="supersede-confirm">Supersede</button>` +
          `<button data-act="supersede-cancel">Cancel</button></div></div>` +
      `</div>`;
    el.addEventListener("click", (ev) => onAction(ev, a, el));
    if (a.has_file) observer.observe(el);
    return el;
  }

  // ── The chat's image viewer, reused (DS MarkdownImage + Lightbox via the kit CSS) ──────────────
  // Same markup, classes and icons as an image in chat: a hover action cluster (Download · View
  // fullscreen · Open in new tab) and the DS Lightbox. The preview is a private blob: URL that
  // can't leave this view, so "Open in new tab" opens a 15-minute signed link instead, minted by
  // the gated /assets/<id>/open-link and served by the public /plugins/campaign/open/<id>. In the
  // desktop app the shell hands that sidecar link to the system browser (protoAgent PR 4312).
  const ICON_DOWNLOAD = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 2.5v8M4.75 7.25 8 10.5l3.25-3.25M2.75 13.5h10.5"/></svg>';
  const ICON_FULLSCREEN = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 2.75H2.75V6M10 2.75h3.25V6M6 13.25H2.75V10M10 13.25h3.25V10"/></svg>';
  const ICON_NEWTAB = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 3.5H3.5v9h9V9M9.75 2.75h3.5v3.5M13 3 7.75 8.25"/></svg>';
  function imageMarkup(url, title){
    return `<div data-streamdown="image-wrapper">` +
      `<img data-streamdown="image" src="${url}" alt="${esc(title)}">` +
      `<div class="pl-md-img-actions">` +
        `<button type="button" data-img="download" title="Download image" aria-label="Download image">${ICON_DOWNLOAD}</button>` +
        `<button type="button" data-img="fullscreen" title="View fullscreen" aria-label="View fullscreen">${ICON_FULLSCREEN}</button>` +
        `<button type="button" data-img="newtab" title="Open in new tab" aria-label="Open in new tab">${ICON_NEWTAB}</button>` +
      `</div></div>`;
  }
  function wireImage(box, url, a){
    box.classList.add("pl-markdown");
    box.querySelector('[data-streamdown="image"]').addEventListener("click", () => openLightbox(url, a.title));
    box.querySelector('[data-img="fullscreen"]').addEventListener("click", () => openLightbox(url, a.title));
    box.querySelector('[data-img="download"]').addEventListener("click", () => {
      const name = String(a.path || "").split("/").pop() || `asset-${a.id}`;
      const link = document.createElement("a");
      link.href = url; link.download = name;
      document.body.appendChild(link); link.click(); link.remove();
    });
    // Mint ahead (hover / keyboard focus), so the click can open the tab synchronously: a
    // window.open after an await loses the click's user activation in Safari / WebKit and is
    // popup-blocked. A link is stateless (an HMAC), so minting one that's never used costs nothing.
    const wrap = box.querySelector('[data-streamdown="image-wrapper"]');
    wrap.addEventListener("pointerenter", () => { openLink(a.id).catch(() => {}); });
    wrap.addEventListener("focusin", () => { openLink(a.id).catch(() => {}); });
    box.querySelector('[data-img="newtab"]').addEventListener("click", () => openInNewTab(a, box.closest(".card")));
  }
  // asset id → { url, at, pending } — a minted link, reused while it has 10+ minutes left.
  const links = new Map();
  const LINK_FRESH_MS = 5 * 60000;
  function freshLink(id){ const l = links.get(id); return l && l.url && Date.now() - l.at < LINK_FRESH_MS ? l.url : ""; }
  function openLink(id){
    const fresh = freshLink(id);
    if (fresh) return Promise.resolve(fresh);
    const l = links.get(id);
    if (l && l.pending) return l.pending;
    const pending = api(`/assets/${id}/open-link`, { method: "POST" }).then(r => r.json()).then(j => {
      // Slug-aware: under the fleet hub the page is /agents/<slug>/plugins/campaign/view, and the
      // link must go through the same /agents/<slug> proxy to reach THIS agent.
      const url = location.origin + BASE + j.url;
      links.set(id, { url, at: Date.now() });
      return url;
    }, (e) => { links.delete(id); throw e; });
    links.set(id, { pending });
    return pending;
  }
  // In the desktop app the shell answers window.open itself (system browser) and denies the
  // webview a window, so a null return there is success — not a blocked pop-up.
  const IN_DESKTOP = Array.from(location.ancestorOrigins || []).some(o => o.startsWith("tauri:") || o === "http://tauri.localhost");
  function openTab(url){
    // No "noopener" feature: with it window.open ALWAYS returns null, so a blocked pop-up can't be
    // told apart. The opener is cut by hand instead (the opened page is an image, which runs no script).
    const w = window.open(url, "_blank");
    if (w) { try { w.opener = null; } catch {} }
    return !!w || IN_DESKTOP;
  }
  function blockedNote(card, a, url){
    let n = card.querySelector(".newtab-note");
    if (!n) { n = document.createElement("div"); n.className = "note newtab-note"; card.querySelector(".body").prepend(n); }
    n.innerHTML = `Your browser blocked the new tab. <a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open #${a.id} in a new tab</a> (link valid 15 min)`;
    n.querySelector("a").addEventListener("click", () => setTimeout(() => n.remove(), 0));
  }
  async function openInNewTab(a, card){
    const fresh = freshLink(a.id);
    if (fresh) { if (!openTab(fresh)) blockedNote(card, a, fresh); return; }
    let url;
    try { url = await openLink(a.id); }
    catch (e) { showErr(`Could not open #${a.id} in a new tab: ${e.message}`); return; }
    if (!openTab(url)) blockedNote(card, a, url);
  }
  // The DS Lightbox (overlays.tsx), as plain DOM: same classes, Escape / backdrop / × to close,
  // focus moved in and restored, body scroll locked while open.
  function openLightbox(url, title){
    const back = document.activeElement;
    const scrim = document.createElement("div");
    scrim.className = "pl-overlay pl-overlay--lightbox";
    scrim.innerHTML =
      `<div class="pl-lightbox" role="dialog" aria-modal="true" aria-label="${esc(title || "Image")}" tabindex="-1">` +
        `<img class="pl-lightbox__img" src="${url}" alt="${esc(title)}">` +
        `<button type="button" class="pl-iconbtn pl-lightbox__close" aria-label="Close">×</button>` +
      `</div>`;
    const prevOverflow = document.body.style.overflow;
    const close = () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
      scrim.remove();
      if (back && back.focus) back.focus();
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); close(); }
      else if (e.key === "Tab") { e.preventDefault(); scrim.querySelector(".pl-lightbox__close").focus(); }  // one focusable: trap
    };
    scrim.addEventListener("mousedown", (e) => { if (e.target === scrim) close(); });
    scrim.querySelector(".pl-lightbox__close").addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    document.body.appendChild(scrim);
    scrim.querySelector(".pl-lightbox__close").focus();
  }

  async function preview(el){
    const a = (state.data?.assets || []).find(x => String(x.id) === el.dataset.id);
    const box = el.querySelector(".prev");
    if (!a || box.dataset.done) return;
    box.dataset.done = "1";
    try {
      let url = blobs.get(a.id);
      if (!url) { url = URL.createObjectURL(await api(`/file/${a.id}`).then(r => r.blob())); blobs.set(a.id, url); }
      const isVideo = /\.(webm|mp4|mov)$/i.test(a.path || "");
      box.innerHTML = isVideo
        // #t=0.5 — a raw take's first frame is the blank page before navigation.
        ? `<video src="${url}#t=0.5" controls muted loop playsinline preload="metadata"></video>`
        : imageMarkup(url, a.title);
      if (!isVideo) wireImage(box, url, a);
    } catch (e) { box.textContent = "preview failed: " + e.message; }
  }
  const observer = new IntersectionObserver((entries) => {
    for (const en of entries) if (en.isIntersecting) { observer.unobserve(en.target); preview(en.target); }
  }, { rootMargin: "200px" });

  async function onAction(ev, a, el){
    const btn = ev.target.closest("button"); if (!btn) return;
    const act = btn.dataset.act;
    if (!act) return;  // e.g. the image action cluster's own buttons
    if (act === "copy") {
      if (!navigator.clipboard) { btn.textContent = a.path; return; }  // no clipboard off a secure origin: show it
      navigator.clipboard.writeText(a.path).then(() => { btn.textContent = "Copied"; setTimeout(() => btn.textContent = "Copy path", 1200); },
        () => { btn.textContent = "Copy failed"; });
      return;
    }
    if (act === "reject") { el.classList.add("rejecting"); el.querySelector(".reject textarea").focus(); return; }
    if (act === "reject-cancel") { el.classList.remove("rejecting"); return; }
    if (act === "supersede") { el.classList.add("superseding"); el.querySelector(".supersede input").focus(); return; }
    if (act === "supersede-cancel") { el.classList.remove("superseding"); return; }
    if (act === "supersede-confirm") {
      const ids = el.querySelector(".supersede input").value.split(/[\s,#]+/).filter(Boolean).map(Number);
      if (ids.some(n => !Number.isInteger(n) || n <= 0)) { showErr("Replacement ids must be asset numbers"); return; }
      btn.disabled = true;
      try {
        await api(`/assets/${a.id}/review`, { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision: "supersede", note: el.querySelector(".supersede textarea").value,
            superseded_by: ids }) });
        showErr("");
        await loadAssets();
      } catch (e) { showErr(`Could not supersede #${a.id}: ${e.message}`); btn.disabled = false; }
      return;
    }
    if (act === "approve" || act === "reject-confirm") {
      const decision = act === "approve" ? "approve" : "reject";
      const note = decision === "reject" ? el.querySelector(".reject textarea").value : "";
      btn.disabled = true;
      try {
        await api(`/assets/${a.id}/review`, { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision, note }) });
        showErr("");
        await loadAssets();
      } catch (e) { showErr(`Could not ${decision} #${a.id}: ${e.message}`); btn.disabled = false; }
    }
  }

  function render(){
    chips();
    const grid = document.getElementById("grid");
    grid.textContent = "";
    const rows = visible();
    for (const a of rows) grid.appendChild(card(a));
    const empty = document.getElementById("empty");
    empty.hidden = rows.length > 0;
    empty.textContent = (state.data?.assets || []).length ? "Nothing matches these filters." :
      "No assets in this campaign yet. Ask the agent to produce the shot list.";
  }

  document.getElementById("chips").addEventListener("click", (ev) => {
    const c = ev.target.closest(".chip"); if (!c) return; state.status = c.dataset.status; render(); });
  document.getElementById("campaign").addEventListener("change", (ev) => {
    for (const u of blobs.values()) URL.revokeObjectURL(u); blobs.clear(); links.clear();
    state.campaign = ev.target.value; state.lane = ""; loadAssets().catch(e => showErr(String(e))); });
  document.getElementById("sort").addEventListener("change", (ev) => { state.sort = ev.target.value; render(); });
  // Keep "x min ago" honest while the page sits open.
  setInterval(() => { for (const el of document.querySelectorAll(".when[data-ts]"))
    el.textContent = el.dataset.pre + relTime(Number(el.dataset.ts)); }, 60000);
  document.getElementById("show-archived").addEventListener("change", (ev) => {
    state.showArchived = ev.target.checked; loadCampaigns().catch(e => showErr("Could not load campaigns: " + e.message)); });
  document.getElementById("show-superseded").addEventListener("change", (ev) => {
    state.showSuperseded = ev.target.checked; render(); });
  document.getElementById("lane").addEventListener("change", (ev) => { state.lane = ev.target.value; render(); });
  document.getElementById("kind").addEventListener("change", (ev) => { state.kind = ev.target.value; render(); });
  document.getElementById("refresh").addEventListener("click", () => {
    for (const u of blobs.values()) URL.revokeObjectURL(u); blobs.clear(); links.clear();  // a re-render or prune changes files
    loadCampaigns().catch(e => showErr(String(e))); });

  let booted = false;
  function boot(){ if (booted) return; booted = true;
    loadCampaigns().catch(e => showErr("Could not load campaigns: " + e.message)); }
  kit.initPluginView(boot);
  setTimeout(boot, 800);
</script>
</body></html>
"""
