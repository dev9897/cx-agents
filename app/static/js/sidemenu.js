/**
 * sidemenu.js — Mobile side navigation drawer
 * Handles open/close, state sync with main navbar, and keyboard/swipe dismissal.
 */

// ── Open / Close ──────────────────────────────────────────────────────────────

function openSideMenu() {
  const overlay = document.getElementById('sideMenuOverlay');
  const menu    = document.getElementById('sideMenu');
  if (!overlay || !menu) return;

  overlay.classList.add('open');
  menu.classList.add('open');
  document.body.style.overflow = 'hidden';

  // Re-render Lucide icons inside the drawer in case they weren't ready on load
  if (window.lucide) lucide.createIcons({ scope: menu });
}

function closeSideMenu() {
  const overlay = document.getElementById('sideMenuOverlay');
  const menu    = document.getElementById('sideMenu');
  if (!overlay || !menu) return;

  overlay.classList.remove('open');
  menu.classList.remove('open');
  document.body.style.overflow = '';
}

// Close on Escape key
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeSideMenu();
});

// ── State sync helpers ────────────────────────────────────────────────────────

/**
 * Call whenever cart count changes to keep side menu badge in sync.
 * @param {number} count
 */
function syncSideMenuCart(count) {
  const el = document.getElementById('smCartCount');
  if (el) el.textContent = count;
}

/**
 * Call whenever token usage changes.
 * @param {string} text  e.g. "1,234 tokens"
 */
function syncSideMenuTokens(text) {
  const el = document.getElementById('smTokenBadge');
  if (el) el.textContent = text;
}

/**
 * Call whenever connection status changes.
 * @param {boolean} online
 */
function syncSideMenuStatus(online) {
  const pill = document.getElementById('smStatusPill');
  const text = document.getElementById('smStatusText');
  if (pill) pill.className = 'sm-stat-card' + (online ? ' online' : '');
  if (text) text.textContent = online ? 'Online' : 'Offline';
}

/**
 * Call whenever auth state changes.
 * @param {string|null} username  null when logged out
 */
function syncSideMenuAuth(username) {
  const btn   = document.getElementById('smAuthBtn');
  const label = document.getElementById('smAuthBtnLabel');
  if (!btn || !label) return;
  if (username) {
    btn.classList.add('logged-in');
    label.textContent = username;
  } else {
    btn.classList.remove('logged-in');
    label.textContent = 'Sign In';
  }
}

/**
 * Call whenever the active view changes.
 * @param {string} viewId  'store' | 'cart-page'
 */
function syncSideMenuActiveView(viewId) {
  document.querySelectorAll('.sm-nav-btn').forEach(btn => btn.classList.remove('active'));
  const map = { 'store': 'smBtnStore', 'cart-page': 'smBtnCart' };
  const el = document.getElementById(map[viewId]);
  if (el) el.classList.add('active');
}
