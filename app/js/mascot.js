// ---------- mascot, bubbles, confetti ----------
let lastDown = 0;   // how many services are down (the mascot gets sad)
function setMood(down) {
  lastDown = down;
  setText("quote", down ? CFG.tagline_down : CFG.tagline);
  $("mascot").classList.toggle("sad", !!down);
  if (down) setText("say", `${CFG.tagline_down} (${down} down)`);
  else if ($("say").dataset.sad) setText("say", CFG.greeting || CFG.quotes[0] || "All good!");
  $("say").dataset.sad = down ? "1" : "";
}
function mascotSay() {
  if (lastDown || !CFG.mascot) return;
  const plex = $("plex-n").textContent, req = parseInt($("req-n").textContent) || 0;
  const extra = [plex && `Now playing: ${plex}`, req && `${req} request${req > 1 ? "s" : ""} waiting for you!`].filter(Boolean);
  const pool = CFG.quotes.concat(extra, extra);   // live facts twice: they come up more often than plain quotes
  if (pool.length) $("say").textContent = pool[Math.floor(Math.random() * pool.length)];
}
function bubbles() {
  const box = $("bubbles");
  if (!box) return;
  box.textContent = "";   // redrawn when background motion is switched on or off
  if (reducedMotion()) return;
  for (let i = 0; i < 18; i++) {
    const b = document.createElement("i"), s = 14 + Math.random() * 46;
    Object.assign(b.style, {width: s + "px", height: s + "px", left: Math.random() * 100 + "%",
                            animationDuration: 14 + Math.random() * 20 + "s", animationDelay: -Math.random() * 30 + "s"});
    box.appendChild(b);
  }
}
// "🎵🥬💙" rains a random one of them per drop (graphemes, so flags and skin tones stay whole)
function pickEmoji(str) {
  const parts = "Segmenter" in Intl ? [...new Intl.Segmenter().segment(str || "")].map(x => x.segment).filter(x => x.trim()) : Array.from(str || "");
  return parts.length ? parts[Math.floor(Math.random() * parts.length)] : "🫧";
}
function confetti() {
  if (reducedMotion()) return;
  for (let i = 0; i < 16; i++) setTimeout(() => {
    const m = document.createElement("div");
    m.className = "confetti";
    m.textContent = pickEmoji(CFG.click_emoji);
    m.style.left = Math.random() * 96 + "vw";
    m.style.animationDuration = 2.2 + Math.random() * 1.8 + "s";
    document.body.appendChild(m);
    setTimeout(() => m.remove(), 4500);
  }, i * 90);
}
$("title").addEventListener("click", confetti);
$("mascot-img").addEventListener("click", () => { confetti(); mascotSay(); });

