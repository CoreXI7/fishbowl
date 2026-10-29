// ---------- CPU / memory breakdown: live treemap (opens from the ⚡ breakdown button or the CPU tile) ----------
// U.hist keeps the last 60 samples per group for the sparkline in the detail pane.
const U = {mode: "cpu", sel: null, data: null, hist: {}, open: false};
const ukey = g => g.kind + ":" + g.name;
const UPAL = ["#6366f1", "#8b5cf6", "#ec4899", "#e11d48", "#ea580c", "#0d9488", "#0284c7", "#16a34a", "#a21caf", "#2563eb", "#c026d3", "#0891b2", "#7c3aed", "#db2777"];
const UHOST = ["#64748b", "#6b7c95", "#57708a", "#7a7f9e", "#5f7a7a"];
function hue(str) { let h = 0; for (const c of str) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h % 360; }
function ucolor(g) {   // stable colour per name: bright for containers, greys for host programs and the kernel
  if (g.kind === "kernel" || g.kind === "other") return "#6b7080";
  const h = hue(g.name);
  return g.kind === "container" ? UPAL[h % UPAL.length] : UHOST[h % UHOST.length];
}
function usageData(u) {
  const first = !U.data;
  U.data = u;
  if (first && /[?&]breakdown\b/.test(location.search)) setTimeout(openUsage, 0);   // deep link: /?breakdown
  for (const g of u.groups) {
    const a = U.hist[ukey(g)] || (U.hist[ukey(g)] = []);
    a.push(U.mode === "mem" ? g.mem : g.cpu);
    if (a.length > 60) a.shift();
  }
  if (U.open) renderUsage();
}
// Squarified treemap: lay rectangles out in rows that keep each tile as close to square as possible.
function squarify(items, x, y, w, h) {
  const out = [], total = items.reduce((a, b) => a + b.v, 0);
  if (!total || w <= 0 || h <= 0) return out;
  const scale = w * h / total, rest = items.map(i => ({...i, a: i.v * scale}));
  while (rest.length) {
    const side = Math.min(w, h), row = [];
    let worst = Infinity;
    while (rest.length) {   // keep adding tiles to this row while it makes the worst aspect ratio better
      const next = [...row, rest[0]], sum = next.reduce((a, b) => a + b.a, 0);
      const mx = Math.max(...next.map(r => r.a)), mn = Math.min(...next.map(r => r.a));
      const ratio = Math.max(side * side * mx / (sum * sum), sum * sum / (side * side * mn));
      if (row.length && ratio > worst) break;
      row.push(rest.shift());
      worst = ratio;
    }
    const sum = row.reduce((a, b) => a + b.a, 0), thick = sum / side;
    let off = 0;
    for (const r of row) {
      const len = r.a / thick;
      out.push(w >= h ? {...r, x: x, y: y + off, w: thick, h: len} : {...r, x: x + off, y: y, w: len, h: thick});
      off += len;
    }
    if (w >= h) { x += thick; w -= thick; } else { y += thick; h -= thick; }
  }
  return out;
}
const fmtCpu = (pct, n) => `${pct.toFixed(pct < 10 ? 1 : 0)}%` + (n ? ` · ${(pct * n / 100).toFixed(2)} cores` : "");
function spark(vals) {
  if (!vals || vals.length < 2) return "";
  const mx = Math.max(...vals, 0.0001), W = 260, H = 46;
  const pts = vals.map((v, i) => `${(i / (vals.length - 1) * W).toFixed(1)},${(H - v / mx * (H - 4) - 2).toFixed(1)}`).join(" ");
  return `<svg viewBox="0 0 ${W} ${H}" class="spark" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="2"/></svg>`;
}
function renderUsage() {
  const u = U.data;
  if (!u) return;
  const mem = U.mode === "mem", val = g => mem ? g.mem : g.cpu;
  const groups = u.groups.filter(g => val(g) > (mem ? 1024 * 1024 : 0.005));
  const busy = u.groups.reduce((a, g) => a + g.cpu, 0), memSum = u.groups.reduce((a, g) => a + g.mem, 0);
  setText("u-what", mem ? "memory" : "CPU");
  setText("u-sum", mem ? `${size(memSum)} in programs` : `${busy.toFixed(1)}% busy · ${(busy * u.ncpu / 100).toFixed(1)} of ${u.ncpu} threads`);

  // tiles are reused between refreshes (matched by data-k), so they glide to their new size instead of flickering
  const box = $("u-map"), rects = squarify(groups.map(g => ({g, v: val(g)})).sort((a, b) => b.v - a.v), 0, 0, box.clientWidth, box.clientHeight);
  const seen = new Set();
  for (const r of rects) {
    const k = ukey(r.g);
    seen.add(k);
    let el = box.querySelector(`[data-k="${CSS.escape(k)}"]`);
    if (!el) {
      el = document.createElement("button");
      el.className = "u-tile";
      el.dataset.k = k;
      el.style.left = r.x + r.w / 2 + "px";   // new tiles grow out of their centre
      el.style.top = r.y + r.h / 2 + "px";
      box.appendChild(el);
    }
    Object.assign(el.style, {left: r.x + "px", top: r.y + "px", width: Math.max(r.w - 3, 0) + "px", height: Math.max(r.h - 3, 0) + "px", background: ucolor(r.g)});
    el.classList.toggle("sel", U.sel === k);
    el.classList.toggle("tiny", r.w < 70 || r.h < 34);
    el.title = `${r.g.name} (${r.g.kind})\n${mem ? size(r.g.mem) : fmtCpu(r.g.cpu, u.ncpu)}`;
    setHTML(el, `<b>${esc(r.g.name)}</b><span>${mem ? size(r.g.mem) : fmtCpu(r.g.cpu)}</span>` + (r.h > 70 && r.g.top && r.g.top[0] ? `<small>${esc(r.g.top[0].name)}</small>` : ""));
  }
  box.querySelectorAll(".u-tile").forEach(el => { if (!seen.has(el.dataset.k)) el.remove(); });
  if (!rects.length) box.innerHTML = `<div class="empty" style="padding:20px">Nothing measurable right now.</div>`;

  // side list (top 25) and the detail pane for the selected tile
  const list = [...u.groups].sort((a, b) => val(b) - val(a)).slice(0, 25), top = val(list[0] || {cpu: 1, mem: 1}) || 1;
  setHTML("u-list", list.map(g => `<button class="u-row ${U.sel === ukey(g) ? "sel" : ""}" data-k="${esc(ukey(g))}">
      <i style="background:${ucolor(g)}"></i><span class="n">${esc(g.name)}</span><span class="v">${mem ? size(g.mem) : fmtCpu(g.cpu)}</span>
      <span class="b"><i style="width:${(100 * val(g) / top).toFixed(1)}%;background:${ucolor(g)}"></i></span></button>`).join(""));
  const g = u.groups.find(x => ukey(x) === U.sel);
  setHTML("u-detail", g ? `<h3><i style="background:${ucolor(g)}"></i>${esc(g.name)} <small class="mute">${esc(g.kind === "process" ? "host program" : g.kind)}</small></h3>
      ${kvRow("CPU", fmtCpu(g.cpu, u.ncpu))}${kvRow("Memory", size(g.mem))}${kvRow("Processes", g.procs)}
      <div class="u-spark" style="color:${ucolor(g)}">${spark(U.hist[ukey(g)])}<small class="mute">${mem ? "memory" : "CPU"}, last few minutes</small></div>
      ${g.top && g.top.length ? `<div class="mute" style="font-size:12px;margin-top:8px">busiest inside</div>` + g.top.map(t => kvRow(esc(t.name), fmtCpu(t.cpu))).join("") : ""}`
    : `<div class="mute">Click a tile or a row for details. Bigger tile = more ${mem ? "memory" : "CPU"}.</div>`);
}
function openUsage() {
  U.open = true;
  $("usage-ov").hidden = false;
  document.body.style.overflow = "hidden";
  requestAnimationFrame(() => requestAnimationFrame(renderUsage));   // measure the map after the window has laid out
  loadSystem();
}
function closeUsage() {
  U.open = false;
  $("usage-ov").hidden = true;
  unlockScroll();
}
// the page scrolls again only when no window (breakdown, live, security details) is still open
function unlockScroll() { if (!document.querySelector(".usage-ov:not([hidden])")) document.body.style.overflow = ""; }
$("usage-btn").addEventListener("click", openUsage);
$("k-sys").addEventListener("click", e => { if (U.data) { e.preventDefault(); openUsage(); } });
$("usage-close").addEventListener("click", closeUsage);
$("usage-ov").addEventListener("click", e => {
  if (e.target === $("usage-ov")) return closeUsage();   // click on the dimmed backdrop
  const t = e.target.closest("[data-k]");
  if (t) { U.sel = U.sel === t.dataset.k ? null : t.dataset.k; renderUsage(); }
  const m = e.target.closest("[data-um]");                 // CPU / Memory tabs
  if (m) {
    U.mode = m.dataset.um;
    U.hist = {};
    document.querySelectorAll("[data-um]").forEach(b => b.classList.toggle("on", b === m));
    $("u-map").innerHTML = "";
    renderUsage();
  }
});
addEventListener("keydown", e => { if (e.key === "Escape" && U.open) closeUsage(); });
// redraw whenever the map's real size changes (window resize, fonts/scrollbar settling after it opens)
if ("ResizeObserver" in window) new ResizeObserver(() => { if (U.open) renderUsage(); }).observe($("u-map"));
else addEventListener("resize", () => { if (U.open) renderUsage(); });

