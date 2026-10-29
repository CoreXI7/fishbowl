// ---------- polling ----------
// Each panel refreshes on its own timer. A refresh never overlaps the previous one (a slow upstream can't pile up
// requests), timers stop while the tab is hidden, and everything refreshes at once when it's shown again.
const pollers = [];
function poll(fn, ms) {
  const p = {timer: 0, busy: false};
  p.run = async () => {
    clearTimeout(p.timer);
    if (p.busy) return;                 // still running: it reschedules itself when done
    p.busy = true;
    try { await fn(); } finally { p.busy = false; }
    if (!document.hidden) p.timer = setTimeout(p.run, ms);
  };
  pollers.push(p);
  p.run();
}
document.addEventListener("visibilitychange", () => pollers.forEach(p => document.hidden ? clearTimeout(p.timer) : p.run()));

(async () => {
  try { CFG = await getJSON("/api/config"); }
  catch (e) { setHTML("svc", `<div class="err">config: ${esc(e.message)}</div>`); }
  applyConfig();
  if (SD.open) renderSec();   // a /?security= deep link drew before the config (settings_ui) arrived
  bubbles();
  poll(loadSystem, 3000);     // CPU/RAM/temps/drives/security (the collector writes every 5 s)
  poll(loadQbit, 5000);
  poll(loadPlex, 10000);
  poll(loadServices, 30000);  // the server checks every service at most every 25 s anyway
  poll(loadRequests, 30000);
  tick();
  setInterval(tick, 1000);
  setInterval(mascotSay, 20000);
})();
