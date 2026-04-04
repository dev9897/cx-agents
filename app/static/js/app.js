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
    '<code style="font-family:var(--font-mono);font-size:12px;background:var(--bg);padding:2px 6px;border-radius:4px;border:1px solid var(--border)">$1</code>'
  );
  html = html.replace(/\n/g, '<br>');
  return html;
}

// ── Health check ─────────────────────────────────────────────────────────────

function setStatus(online) {
  const pill = document.getElementById('statusPill');
  pill.className = 'status-pill' + (online ? ' online' : '');
  pill.querySelector('span').textContent = online ? 'connected' : 'offline';
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
  document.getElementById('tokenBadge').textContent =
    `${App.totalTokens.toLocaleString()} tokens`;

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
      const imageBtn = document.getElementById('imageSearchBtn');
      const audioBtn = document.getElementById('audioSearchBtn');
      if (imageBtn) imageBtn.style.display = features.image_search?.enabled ? 'flex' : 'none';
      if (audioBtn) audioBtn.style.display = features.audio_search?.enabled ? 'flex' : 'none';
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











// /**
//  * app.js — Global state, initialization, health check, and utility functions.
//  */

// const API = '';

// const App = {
//   sessionId:   null,
//   totalTokens: 0,
//   turnCount:   0,
//   isLoading:   false,
//   currentUser: null,
//   currentUserEmail: null,
//   cartData:    { items: [], total: null, id: null, orderCode: null },
//   savedAddresses: [],
//   sapPaymentDetails: [],
//   stripeCards: [],
//   features: {
//     recommendations: { enabled: false },
//     image_search: { enabled: false },
//     audio_search: { enabled: false },
//   },
// };

// // ── Utilities ────────────────────────────────────────────────────────────────

// /** Returns the best available email for the current user. */
// function getUserEmail() {
//   return App.currentUserEmail || App.currentUser || null;
// }

// function esc(s) {
//   return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
// }

// function formatText(text) {
//   let html = esc(text);
//   html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
//   html = html.replace(
//     /`([^`]+)`/g,
//     '<code style="font-family:var(--font-mono);font-size:12px;background:var(--bg);padding:2px 6px;border-radius:4px;border:1px solid var(--border)">$1</code>'
//   );
//   html = html.replace(/\n/g, '<br>');
//   return html;
// }

// // ── Health check ─────────────────────────────────────────────────────────────

// function setStatus(online) {
//   const pill = document.getElementById('statusPill');
//   pill.className = 'status-pill' + (online ? ' online' : '');
//   pill.querySelector('span').textContent = online ? 'connected' : 'offline';
// }

// async function checkHealth() {
//   try {
//     const r = await fetch(`${API}/health`);
//     const d = await r.json();
//     setStatus(d.status === 'ok' && d.circuit_breaker === 'closed');
//   } catch {
//     setStatus(false);
//   }
// }

// // ── Sidebar helpers ──────────────────────────────────────────────────────────

// function updateSidebarUser(username) {
//   document.getElementById('sideUser').textContent = username || 'guest';
// }

// function markStep(id, state) {
//   const el = document.getElementById(id);
//   if (el) el.className = `step ${state}`;
// }

// function updateSidebar(d) {
//   document.getElementById('sessionId').textContent =
//     d.session_id ? d.session_id.slice(0, 8) + '...' : '\u2014';
//   document.getElementById('sideTurns').textContent = App.turnCount;
//   App.totalTokens = d.tokens_used || App.totalTokens;
//   document.getElementById('tokenBadge').textContent =
//     `${App.totalTokens.toLocaleString()} tokens`;

//   const r = (d.reply || '').toLowerCase();
//   if (r.includes('found') || r.includes('product') || r.includes('result') || r.includes('here'))
//     markStep('step-search', 'done');
//   if (d.cart_id) markStep('step-cart', 'done');
//   if (r.includes('delivery address') || r.includes('shipping address'))
//     markStep('step-address', 'active');
//   if (r.includes('address') && r.includes('set'))
//     { markStep('step-address', 'done'); markStep('step-payment', 'active'); }
//   if (r.includes('payment') && r.includes('set'))
//     { markStep('step-payment', 'done'); markStep('step-order', 'active'); }
//   if (d.order_code)
//     markStep('step-order', 'done');
// }

// function setLoading(v) {
//   App.isLoading = v;
//   const sendBtn = document.getElementById('sendBtn');
//   const userInput = document.getElementById('userInput');
//   if (sendBtn) sendBtn.disabled = v;
//   if (userInput) {
//     userInput.disabled = v;
//     if (!v) userInput.focus();
//   }
// }

// // ── Cart UI ──────────────────────────────────────────────────────────────────

// function updateCartUI() {
//   const countEl = document.getElementById('cartCount');
//   const itemsEl = document.getElementById('cartItems');
//   const emptyEl = document.getElementById('cartEmpty');
//   const totalEl = document.getElementById('cartTotal');
//   const priceEl = document.getElementById('cartTotalPrice');

//   const count = App.cartData.items.length;
//   countEl.textContent = count;

//   // Sync widget cart badge and nav cart count
//   const cwBadge = document.getElementById('cwCartBadge');
//   if (cwBadge) cwBadge.textContent = count;
//   const navCount = document.getElementById('navCartCount');
//   if (navCount) navCount.textContent = count;

//   if (App.cartData.items.length === 0) {
//     emptyEl.style.display = 'block';
//     totalEl.style.display = 'none';
//     itemsEl.innerHTML = '';
//     itemsEl.appendChild(emptyEl);
//   } else {
//     emptyEl.style.display = 'none';
//     itemsEl.innerHTML = '';
//     App.cartData.items.forEach(item => {
//       const div = document.createElement('div');
//       div.className = 'cart-item';
//       div.innerHTML = `<span class="cart-item-name">${esc(item.name)}</span>
//         <span class="cart-item-qty">x${item.qty}</span>
//         <span class="cart-item-price">${esc(item.price)}</span>`;
//       itemsEl.appendChild(div);
//     });
//     if (App.cartData.total) {
//       totalEl.style.display = 'flex';
//       priceEl.textContent = App.cartData.total;
//     }
//   }
// }

// // ── Feature discovery ────────────────────────────────────────────────────────

// async function loadFeatures() {
//   try {
//     const r = await fetch(`${API}/features`);
//     if (r.ok) {
//       const features = await r.json();
//       App.features = features;

//       // Show/hide input action buttons based on feature availability
//       const imageBtn = document.getElementById('imageSearchBtn');
//       const audioBtn = document.getElementById('audioSearchBtn');
//       if (imageBtn) imageBtn.style.display = features.image_search?.enabled ? 'flex' : 'none';
//       if (audioBtn) audioBtn.style.display = features.audio_search?.enabled ? 'flex' : 'none';
//     }
//   } catch {
//     // Features endpoint not available — hide smart search buttons
//   }
// }

// // ── Initialization ───────────────────────────────────────────────────────────

// function initApp() {
//   checkHealth();
//   setInterval(checkHealth, 30000);
//   loadFeatures();

//   // Auto-resize textarea
//   const input = document.getElementById('userInput');
//   input.addEventListener('input', () => {
//     input.style.height = 'auto';
//     input.style.height = Math.min(input.scrollHeight, 140) + 'px';
//   });
//   input.addEventListener('keydown', e => {
//     if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
//   });

//   // Load store as the default view
//   if (typeof storeSearch === 'function') {
//     storeSearch();
//   }

//   // Close widget on Escape
//   document.addEventListener('keydown', e => {
//     if (e.key === 'Escape') {
//       const widget = document.getElementById('chatWidget');
//       if (widget && widget.classList.contains('open')) {
//         toggleChatWidget();
//       }
//     }
//   });
// }

// document.addEventListener('DOMContentLoaded', initApp);

// // ═══════════════════════════════════════════════════════════════════
// //  MODAL HELPERS
// //  Index.html ke inline onclick handlers yahan call karte hain
// // ═══════════════════════════════════════════════════════════════════

// function openModal(id) {
//   const el = document.getElementById(id);
//   if (!el) return;
//   el.classList.add('open');
//   if (typeof lucide !== 'undefined') lucide.createIcons();
// }

// function closeModal(id) {
//   const el = document.getElementById(id);
//   if (el) el.classList.remove('open');
// }

// // ── Login modal ──────────────────────────────────────────────────────────────

// function openLogin() {
//   // Support both old HTML (loginModal with .open class)
//   // and new HTML (loginModal with display toggle)
//   const modal = document.getElementById('loginModal');
//   if (!modal) return;
//   modal.style.display = 'flex';
//   modal.classList.add('open');
//   // Clear previous errors
//   const err = document.getElementById('loginError');
//   if (err) err.textContent = '';
// }

// function closeLogin() {
//   const modal = document.getElementById('loginModal');
//   if (!modal) return;
//   modal.style.display = 'none';
//   modal.classList.remove('open');
// }

// function handleOverlayClick(e) {
//   if (e.target === e.currentTarget) closeLogin();
// }

// // ── Settings modal ───────────────────────────────────────────────────────────

// function openSettings() {
//   const modal = document.getElementById('settingsModal');
//   if (!modal) return;
//   modal.style.display = 'flex';
//   modal.classList.add('open');
// }

// function closeSettings() {
//   const modal = document.getElementById('settingsModal');
//   if (!modal) return;
//   modal.style.display = 'none';
//   modal.classList.remove('open');
// }

// function handleSettingsOverlayClick(e) {
//   if (e.target === e.currentTarget) closeSettings();
// }

// // ── Settings tabs (new HTML: panel-cards / panel-addresses) ──────────────────

// function switchSettingsTab(tab) {
//   document.querySelectorAll('.settings-tab').forEach(btn => {
//     btn.classList.toggle('active', btn.dataset.tab === tab);
//   });
//   document.querySelectorAll('.settings-panel').forEach(panel => {
//     panel.classList.toggle('active', panel.id === 'panel-' + tab);
//   });
// }

// // ── Settings stabs (old HTML: spanel-cards / spanel-addresses) ───────────────

// function switchStab(tab) {
//   ['cards', 'addresses'].forEach(t => {
//     const panel = document.getElementById('spanel-' + t);
//     const btn   = document.getElementById('stab-' + t);
//     if (panel) panel.style.display = t === tab ? 'block' : 'none';
//     if (btn)   btn.classList.toggle('active', t === tab);
//   });
//   if (typeof lucide !== 'undefined') lucide.createIcons();
// }

// // ── Address form toggle ───────────────────────────────────────────────────────

// function toggleAddressForm() {
//   const w = document.getElementById('addressFormWrap');
//   if (!w) return;
//   const isHidden = w.style.display === 'none' || w.style.display === '';
//   w.style.display = isHidden ? 'flex' : 'none';
//   w.style.flexDirection = 'column';
//   w.style.gap = '12px';
// }

// // ── View switcher ─────────────────────────────────────────────────────────────

// function switchView(view) {
//   // Hide all views
//   document.querySelectorAll('[id^="view-"]').forEach(el => {
//     el.style.display = 'none';
//   });

//   // Show target view
//   const target = document.getElementById('view-' + view);
//   if (target) target.style.display = 'block';

//   // Update nav active state
//   document.querySelectorAll('.nav-link, .nav-btn').forEach(el => {
//     const isActive = el.dataset.view === view ||
//       (el.id === 'navStore' && view === 'store') ||
//       (el.id === 'navCart'  && view === 'cart-page');
//     el.classList.toggle('active', isActive);
//   });

//   // Render cart page content if switching to cart
//   if (view === 'cart-page' && typeof renderCartPage === 'function') {
//     renderCartPage();
//   }

//   if (typeof lucide !== 'undefined') lucide.createIcons();
// }

// // ── Chat widget tab switcher ──────────────────────────────────────────────────

// function switchCwTab(tab) {
//   document.querySelectorAll('.cw-tab').forEach(btn => {
//     btn.classList.toggle('active', btn.dataset.cwtab === tab);
//   });
//   document.querySelectorAll('.cw-panel').forEach(panel => {
//     const isActive = panel.id === 'cwPanel-' + tab;
//     panel.style.display = isActive ? 'flex' : 'none';
//     if (isActive) panel.style.flexDirection = 'column';
//   });
//   if (typeof lucide !== 'undefined') lucide.createIcons();
// }

// // ── Chat widget open/close ────────────────────────────────────────────────────

// function toggleChatWidget() {
//   const widget = document.getElementById('chatWidget');
//   const bubble = document.getElementById('chatToggleBubble');
//   const icon   = document.getElementById('chatBubbleIcon');
//   if (!widget) return;

//   const isOpen = widget.classList.toggle('open');

//   if (bubble) bubble.classList.toggle('open', isOpen);
//   if (icon)   icon.innerHTML = isOpen ? '&#10005;' : '&#128172;';

//   if (isOpen) {
//     // Default to chat tab
//     switchCwTab('chat');
//     const input = document.getElementById('userInput');
//     if (input) setTimeout(() => input.focus(), 300);
//   }
// }

// // ── Product detail helpers ────────────────────────────────────────────────────

// function closeProductDetail() {
//   // New HTML style (display none)
//   const overlay = document.getElementById('productDetailOverlay');
//   if (overlay) overlay.style.display = 'none';

//   // Old HTML style (class toggle)
//   const detailOverlay = document.getElementById('detailOverlay');
//   if (detailOverlay) detailOverlay.classList.remove('open');
// }