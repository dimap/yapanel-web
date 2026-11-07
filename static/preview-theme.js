// The gallery shares the panel's origin-wide appearance preferences.
// Run in <head> before painting, and follow changes from an open settings tab.
(() => {
  function onAccent(hex) {
    const n = String(hex || '').replace('#', '');
    if (!/^[0-9a-f]{6}$/i.test(n)) return '#FFFFFF';
    const c = [0, 2, 4].map(i => {
      const v = parseInt(n.slice(i, i + 2), 16) / 255;
      return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
    });
    return .2126 * c[0] + .7152 * c[1] + .0722 * c[2] > .4 ? '#16161D' : '#FFFFFF';
  }
  function apply() {
    try {
      const root = document.documentElement;
      const theme = localStorage.getItem('ui.theme');
      const accent = localStorage.getItem('ui.accent') || '';
      if (theme === 'light' || theme === 'dark') root.dataset.theme = theme;
      else delete root.dataset.theme;
      for (const [key, value] of [
        ['--accent', accent],
        ['--accent-ink', accent && `color-mix(in srgb, ${accent} 72%, var(--text))`],
        ['--accent-soft', accent && `color-mix(in srgb, ${accent} 15%, var(--card))`],
        ['--on-accent', accent && onAccent(accent)],
      ]) value ? root.style.setProperty(key, value) : root.style.removeProperty(key);
      window.dispatchEvent(new Event('themechange'));
    } catch { /* Storage may be unavailable; keep the system theme. */ }
  }
  apply();
  window.addEventListener('storage', event => {
    if (event.key === null || event.key === 'ui.theme' || event.key === 'ui.accent') apply();
  });
  window.addEventListener('pageshow', apply);
})();
