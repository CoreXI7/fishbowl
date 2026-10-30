// Paint in the theme this browser picked before the page renders (the server bakes in the default one).
// A page brought back by Back (the back/forward cache) is reloaded, so it's never shown after signing out.
addEventListener("pageshow", function (e) { if (e.persisted) location.reload(); });
try { var t = localStorage.getItem("theme"); if (t && /^[a-z0-9-]{1,24}$/.test(t)) document.documentElement.dataset.theme = t; } catch (e) {}
