/**
 * app.js — Global state, initialization, health check, and utility functions.
 */

const API = '';

const App = {
  sessionId:   null,
  totalTokens: 0,
  turnCount:   0,
  isLoading:   false,
  currentUser: null,
  currentUserEmail: null,
  cartData:    { items: [], total: null, id: null, orderCode: null },
  savedAddresses: [],
  sapPaymentDetails: [],
  stripeCards: [],
  features: {
    recommendations: { enabled: false },
    image_search: { enabled: false },
    audio_search: { enabled: false },
  },
};

// ── Utilities ────────────────────────────────────────────────────────────────

/** Returns the best available email for the current user. */
function getUserEmail() {
  return App.currentUserEmail || App.currentUser || null;
}

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function formatText(text) {
  let html = esc(text);
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(
    /`([^`]+)`/g,
    '<code style="font-family:monospace;font-size:12px;background:#f1f5f9;padding:2px 6px;border-radius:4px;border:1px solid #e2e8f0">$1</code>'
  );
  html = html.replace(/\n/g, '<br>');
  return html;
}

// ── Health check ─────────────────────────────────────────────────────────────

function setStatus(online) {
  const pill = document.getElementById('statusPill');
  pill.className = 'status-pill' + (online ? ' online' : '');
  const span = pill.querySelector('span');
  if (span) span.textContent = online ? 'Online' : 'Offline';
  // sync side menu (sidemenu.js)
  if (typeof syncSideMenuStatus === 'function') syncSideMenuStatus(online);
}

async function checkHealth() {
  try {
    const r = await fetch(`${API}/health`);
    const d = await r.json();
    setStatus(d.status === 'ok' && d.circuit_breaker === 'closed');
  } catch {
    setStatus(false);
  }
}

// ── Sidebar helpers ──────────────────────────────────────────────────────────

function updateSidebarUser(username) {
  document.getElementById('sideUser').textContent = username || 'guest';
}

function markStep(id, state) {
  const el = document.getElementById(id);
  if (el) el.className = `step ${state}`;
}

function updateSidebar(d) {
  document.getElementById('sessionId').textContent =
    d.session_id ? d.session_id.slice(0, 8) + '...' : '\u2014';
  document.getElementById('sideTurns').textContent = App.turnCount;
  App.totalTokens = d.tokens_used || App.totalTokens;
  const tokenText = `${App.totalTokens.toLocaleString()} tokens`;
  document.getElementById('tokenBadge').textContent = tokenText;
  if (typeof syncSideMenuTokens === 'function') syncSideMenuTokens(tokenText);

  const r = (d.reply || '').toLowerCase();
  if (r.includes('found') || r.includes('product') || r.includes('result') || r.includes('here'))
    markStep('step-search', 'done');
  if (d.cart_id) markStep('step-cart', 'done');
  if (r.includes('delivery address') || r.includes('shipping address'))
    markStep('step-address', 'active');
  if (r.includes('address') && r.includes('set'))
    { markStep('step-address', 'done'); markStep('step-payment', 'active'); }
  if (r.includes('payment') && r.includes('set'))
    { markStep('step-payment', 'done'); markStep('step-order', 'active'); }
  if (d.order_code)
    markStep('step-order', 'done');
}

function setLoading(v) {
  App.isLoading = v;
  const sendBtn = document.getElementById('sendBtn');
  const userInput = document.getElementById('userInput');
  if (sendBtn) sendBtn.disabled = v;
  if (userInput) {
    userInput.disabled = v;
    if (!v) userInput.focus();
  }
}

// ── Cart UI ──────────────────────────────────────────────────────────────────

function updateCartUI() {
  const countEl = document.getElementById('cartCount');
  const itemsEl = document.getElementById('cartItems');
  const emptyEl = document.getElementById('cartEmpty');
  const totalEl = document.getElementById('cartTotal');
  const priceEl = document.getElementById('cartTotalPrice');

  const count = App.cartData.items.length;
  countEl.textContent = count;

  // Sync widget cart badge and nav cart count
  const cwBadge = document.getElementById('cwCartBadge');
  if (cwBadge) cwBadge.textContent = count;
  const navCount = document.getElementById('navCartCount');
  if (navCount) navCount.textContent = count;
  if (typeof syncSideMenuCart === 'function') syncSideMenuCart(count);

  if (App.cartData.items.length === 0) {
    emptyEl.style.display = 'block';
    totalEl.style.display = 'none';
    itemsEl.innerHTML = '';
    itemsEl.appendChild(emptyEl);
  } else {
    emptyEl.style.display = 'none';
    itemsEl.innerHTML = '';
    App.cartData.items.forEach(item => {
      const div = document.createElement('div');
      div.className = 'cart-item';
      div.innerHTML = `<span class="cart-item-name">${esc(item.name)}</span>
        <span class="cart-item-qty">x${item.qty}</span>
        <span class="cart-item-price">${esc(item.price)}</span>`;
      itemsEl.appendChild(div);
    });
    if (App.cartData.total) {
      totalEl.style.display = 'flex';
      priceEl.textContent = App.cartData.total;
    }
  }
}

// ── Initialization ───────────────────────────────────────────────────────────

// ── Feature discovery ────────────────────────────────────────────────────────

async function loadFeatures() {
  try {
    const r = await fetch(`${API}/features`);
    if (r.ok) {
      const features = await r.json();
      App.features = features;

      // Show/hide input action buttons based on feature availability
      // Buttons are always visible; only hide if backend explicitly marks as disabled
      const imageBtn = document.getElementById('imageSearchBtn');
      const audioBtn = document.getElementById('audioSearchBtn');
      if (imageBtn && features.image_search?.enabled === false) imageBtn.style.display = 'none';
      if (audioBtn && features.audio_search?.enabled === false) audioBtn.style.display = 'none';
    }
  } catch {
    // Features endpoint not available — hide smart search buttons
  }
}

function initApp() {
  checkHealth();
  setInterval(checkHealth, 30000);
  loadFeatures();

  // Auto-resize textarea
  const input = document.getElementById('userInput');
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  // Load store as the default view
  if (typeof storeSearch === 'function') {
    storeSearch();
  }

  // Close widget on Escape
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      const widget = document.getElementById('chatWidget');
      if (widget && widget.classList.contains('open')) {
        toggleChatWidget();
      }
    }
  });
}

document.addEventListener('DOMContentLoaded', initApp);
