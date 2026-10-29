// ---------- wall mode: ?wall in the URL, the 🖥 button, or the W key ----------
let idleTimer, reloadTimer;
const isWall = () => document.body.classList.contains("wall");
function wake() {   // in wall mode, show the cursor (and the button) while the mouse moves; hide them after 3 s of stillness
  document.body.classList.remove("idle");
  clearTimeout(idleTimer);
  if (isWall()) idleTimer = setTimeout(() => document.body.classList.add("idle"), 3000);
}
function setWall(on) {
  document.body.classList.toggle("wall", on);
  $("wall-btn").title = on ? "Leave wall mode (W)" : "Wall mode for a TV or spare screen (W)";
  $("wall-btn").setAttribute("aria-pressed", on);
  const url = new URL(location.href);
  if (on) url.searchParams.set("wall", ""); else url.searchParams.delete("wall");
  history.replaceState(null, "", url.pathname + url.search.replace("wall=", "wall").replace(/\?$/, "") + url.hash);
  clearTimeout(reloadTimer);
  if (on) reloadTimer = setTimeout(() => location.reload(), 6 * 3600 * 1000);   // wall display: fresh page every 6 h clears browser memory
  wake();
  tick();
}
["mousemove", "mousedown", "touchstart", "keydown"].forEach(e => addEventListener(e, wake, {passive: true}));
$("wall-btn").addEventListener("click", () => setWall(!isWall()));
addEventListener("keydown", e => { if (e.key.toLowerCase() === "w" && noTyping(e)) setWall(!isWall()); });
if (/[?&#]wall\b/.test(location.search + location.hash)) setWall(true);

// header clock (wall mode shows weekday + minutes only, so it only really changes once a minute there)
function tick() {
  setText("p-time", isWall() ? new Date().toLocaleString([], {weekday: "short", hour: "numeric", minute: "2-digit"}) : new Date().toLocaleTimeString());
}

