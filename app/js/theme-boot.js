// Paint in the theme this browser picked before the page renders (the server bakes in the default one).
try { var t = localStorage.getItem("theme"); if (t && /^[a-z0-9-]{1,24}$/.test(t)) document.documentElement.dataset.theme = t; } catch (e) {}
