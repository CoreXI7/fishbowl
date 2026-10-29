// ---------- Plex now playing (via Tautulli) ----------
function streamCard(s) {
  const ep = s.media_type === "episode", tc = s.transcode_decision === "transcode", pct = +s.progress_percent || 0;
  const title = ep ? s.grandparent_title : s.title, sub = ep ? `S${s.parent_media_index}·E${s.media_index} — ${s.title}` : (s.year || "");
  const icon = s.state === "paused" ? "⏸" : s.state === "buffering" ? "⏳" : "▶";
  const mode = tc && s.video_decision !== "transcode" ? `<span class="chip good">Audio transcode</span>`
    : tc ? `<span class="chip mid">Transcode${s.transcode_hw_encoding ? " (HW)" : " (CPU!)"}</span>`
    : s.transcode_decision === "copy" ? `<span class="chip good">Direct Stream</span>` : `<span class="chip good">Direct Play</span>`;
  const res = s.stream_video_full_resolution || s.video_full_resolution;
  const left = s.duration ? " · " + eta(Math.max(0, (s.duration - s.view_offset) / 1000)) + " left" : "";
  return `<div class="stream">${s.art ? `<img src="${esc(s.art)}" alt="" loading="lazy">` : `<div class="ph"></div>`}
    <div class="b"><div class="t" title="${esc(s.full_title)}">${esc(title)}</div><div class="sub">${esc(sub)}</div>
      <div class="sub">👤 ${esc(s.friendly_name)} · ${esc(s.product)} on ${esc(s.player)}</div>
      <div class="bar"><i style="width:${pct}%"></i></div>
      <div class="sub">${icon} ${esc(s.state)} · ${pct}%${left}</div>
      <div class="chips">${mode}${res ? `<span class="chip">${esc(res)}</span>` : ""}<span class="chip">${(s.bandwidth / 1000).toFixed(1)} Mbps</span>
        <span class="chip ${s.location === "wan" ? "" : "good"}">${s.location === "wan" ? "remote" : "home"}</span></div></div></div>`;
}

async function loadPlex() {
  if (!CFG.features.plex) return;
  try {
    const d = await getJSON("/api/plex");
    const summary = `${(+d.bandwidth / 1000 || 0).toFixed(1)} Mbps · ${esc(d.transcodes)} transcoding`;
    // Only a VIDEO transcode can use the GPU; audio-only transcodes (e.g. AC3 -> AAC) are always CPU and cheap.
    const cpuTranscode = d.sessions.some(s => s.video_decision === "transcode" && !s.transcode_hw_encoding);
    kpi("k-plex", cpuTranscode ? "warn" : "ok", d.stream_count ? `${d.stream_count} watching` : "idle", d.stream_count ? summary : "nobody streaming");
    setText("plex-n", d.stream_count ? `${d.stream_count} stream${d.stream_count > 1 ? "s" : ""} · ${summary}` : "");
    if (setHTML("plex", d.sessions.length ? d.sessions.map(streamCard).join("") : `<div class="empty">Nobody's watching right now. 🍿</div>`))
      requestAnimationFrame(plexHint);
  } catch (e) {
    setHTML("plex", `<div class="err">Tautulli: ${esc(e.message)}</div>`);
    kpi("k-plex", "warn", "?", "Tautulli unreachable");
  }
}

// More streams than fit (wall mode)? "+N more" gives Now Playing the whole row and moves Requests aside;
// clicking it again (or the Plex tile, in wall mode) brings Requests back. Remembered per browser.
let plexWide = false;
try { plexWide = localStorage.getItem("plexWide") === "1"; } catch (e) {}
function setPlexWide(on) {
  plexWide = on;
  document.querySelector(".duo").classList.toggle("plex-wide", on);
  try { localStorage.setItem("plexWide", on ? "1" : "0"); } catch (e) {}
  plexHint();
}
function plexHint() {
  const bottom = $("plex").getBoundingClientRect().bottom;   // the list's own box (it scrolls on the wall)
  const cut = [...$("plex").querySelectorAll(".stream")].filter(el => el.getBoundingClientRect().bottom > bottom + 1).length;
  $("plex-more").hidden = !cut && !plexWide;
  setText("plex-more", plexWide ? `${cut ? `+${cut} more · ` : ""}show requests` : `+${cut} more ▸`);
}
setPlexWide(plexWide);
$("plex-more").addEventListener("click", () => setPlexWide(!plexWide));
$("plex").addEventListener("scroll", plexHint, {passive: true});
$("k-plex").addEventListener("click", e => { if (isWall()) { e.preventDefault(); setPlexWide(!plexWide); } });
// recount whenever the box or the list changes size (window resize, posters and fonts settling after a refresh)
if ("ResizeObserver" in window) { const ro = new ResizeObserver(() => plexHint()); ro.observe($("s-plex")); ro.observe($("plex")); }
else addEventListener("resize", () => requestAnimationFrame(plexHint));

