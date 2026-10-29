// ---------- Seerr requests (approve / decline right from the dashboard) ----------
function requestCard(r) {
  const poster = r.poster && r.poster.startsWith("data:") ? r.poster : "https://image.tmdb.org/t/p/w154" + r.poster;
  const id = esc(r.id);                   // ids go into attributes only; the click handler reads data-id
  return `<div class="req" id="r${id}">
    ${r.poster ? `<img loading="lazy" src="${esc(poster)}" alt="">` : `<div class="ph"></div>`}
    <div class="b"><div><b>${esc(r.title)}</b> ${r.year ? `<span class="mute">(${esc(r.year)})</span>` : ""}<span class="tag">${r.type === "tv" ? "TV" : "Movie"}</span>${r.is4k ? '<span class="tag">4K</span>' : ""}</div>
      <div class="mute" style="font-size:12px">${esc(r.user)} · ${ago(r.created)}${r.seasons.length ? " · season " + r.seasons.map(esc).join(", ") : ""}</div>
      <div class="ov">${esc(r.overview)}</div>
      <button class="ok" data-act="approve" data-id="${id}">Approve</button> <button class="no" data-act="decline" data-id="${id}">Decline</button>
      <span class="err" id="e${id}"></span></div></div>`;
}
$("req").addEventListener("click", e => {
  const b = e.target.closest("button[data-act]");
  if (b) act(b.dataset.id, b.dataset.act, b);
});

async function loadRequests() {
  if (!CFG.features.seerr) return;
  try {
    const d = await getJSON("/api/requests"), n = d.pending.length;
    setHTML("req", n ? d.pending.map(requestCard).join("") : `<div class="empty">No pending requests. 🎉</div>`);
    setText("req-n", n ? String(n) : "");
    kpi("k-req", n ? "warn" : "ok", n ? `${n} waiting` : "none", n ? esc(d.pending.slice(0, 2).map(r => r.title).join(", ")) : "all caught up 🎉");
  } catch (e) {
    setHTML("req", `<div class="err">Seerr: ${esc(e.message)}</div>`);
    kpi("k-req", "warn", "?", "Seerr unreachable");
  }
}

async function act(id, action, btn) {
  if (action === "decline" && !confirm("Decline this request?")) return;
  const buttons = btn.parentElement.querySelectorAll("button");
  buttons.forEach(b => b.disabled = true);
  try {
    await postJSON(`/api/requests/${encodeURIComponent(id)}/${action}`);
    $("r" + id).style.opacity = .4;
    setTimeout(loadRequests, 600);
  } catch (e) {
    $("e" + id).textContent = " " + e.message;
    buttons.forEach(b => b.disabled = false);
  }
}

