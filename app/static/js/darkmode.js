/**
 * darkmode.js — Dark / Light mode toggle with localStorage persistence.
 */

(function () {
  const KEY = 'cx-theme';

  // ── Apply theme immediately (before paint) ─────────────────────────────
  function applyTheme(dark) {
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    syncToggleUI(dark);
  }

  // ── Sync all toggle UIs ────────────────────────────────────────────────
  function syncToggleUI(dark) {
    // Desktop navbar icon button
    const navBtn = document.getElementById('darkToggleBtn');
    if (navBtn) {
      navBtn.innerHTML = dark ? sunIcon() : moonIcon();
      navBtn.title = dark ? 'Switch to Light Mode' : 'Switch to Dark Mode';
    }

    // Side menu switch
    const checkbox = document.getElementById('smDarkToggle');
    if (checkbox) checkbox.checked = dark;
  }

  // ── Toggle ─────────────────────────────────────────────────────────────
  window.toggleDarkMode = function () {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    const next = !isDark;
    localStorage.setItem(KEY, next ? 'dark' : 'light');
    applyTheme(next);
  };

  // ── SVG icons ──────────────────────────────────────────────────────────
  function moonIcon() {
    return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
    </svg>`;
  }
  function sunIcon() {
    return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="5"/>
      <line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/>
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
      <line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/>
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
    </svg>`;
  }

  // ── Init on DOMContentLoaded ───────────────────────────────────────────
  function init() {
    const saved = localStorage.getItem(KEY);
    // default: respect OS preference if no saved choice
    const prefersDark = saved
      ? saved === 'dark'
      : window.matchMedia('(prefers-color-scheme: dark)').matches;
    applyTheme(prefersDark);

    // Listen to checkbox change (side menu)
    const checkbox = document.getElementById('smDarkToggle');
    if (checkbox) {
      checkbox.addEventListener('change', function () {
        const dark = this.checked;
        localStorage.setItem(KEY, dark ? 'dark' : 'light');
        applyTheme(dark);
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
