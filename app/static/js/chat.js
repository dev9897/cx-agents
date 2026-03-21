/**
 * chat.js — Message sending, receiving, typing indicator, order confirmation.
 *
 * Depends on: app.js, renderers.js
 *
 * Product data, cart data, and suggestions come as structured JSON from the backend.
 * No text parsing for products — the backend extracts them from tool results.
 */

// ── Suggestion block cleanup ────────────────────────────────────────────────

function stripSuggestionsBlock(text) {
  return text.replace(/\[SUGGESTIONS\]\s*\{.*?\}\s*\[\/SUGGESTIONS\]/s, '').trimEnd();
}

// ── Send ─────────────────────────────────────────────────────────────────────

async function sendMessage() {
  const input = document.getElementById('userInput');
  const text  = input.value.trim();
  if (!text || App.isLoading) return;
  input.value = '';
  input.style.height = 'auto';
  doSend(text);
}

function sendQuick(text) {
  if (!App.isLoading) doSend(text);
}

async function doSend(text) {
  hideWelcome();
  appendMsg('user', text);
  setLoading(true);
  const typing = appendTyping();

  try {
    const body = { message: text, user_id: App.currentUser || 'anonymous' };
    if (App.sessionId) body.session_id = App.sessionId;

    const r = await fetch(`${API}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    typing.remove();

    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: 'Server error' }));
      appendError(err.detail || `HTTP ${r.status}`);
      return;
    }

    const d = await r.json();
    App.sessionId    = d.session_id;
    App.totalTokens += d.tokens_used || 0;
    App.turnCount    = d.turn || App.turnCount + 1;

    if (d.awaiting_approval) {
      showOrderConfirmDirect(d.reply);
    } else {
      const msgDiv = appendMsg('agent', d.reply, d);
      appendSuggestions(msgDiv, d.suggestions);
    }

    updateSidebar(d);
    syncCartFromResponse(d);

    if (d.authenticated && d.username && !App.currentUser) {
      App.currentUser = d.username;
      App.currentUserEmail = d.username.includes('@') ? d.username : `${d.username}@store.local`;
      updateAuthUI(d.username);
      updateSidebarUser(d.username);
    }
  } catch {
    typing.remove();
    appendError('Could not reach the agent. Is the server running?');
  } finally {
    setLoading(false);
  }
}

// ── Quick checkout (2-click) ────────────────────────────────────────────────

async function quickCheckout(btn) {
  const addrSelect = document.getElementById('checkoutAddr');
  const paySelect = document.getElementById('checkoutPay');
  const addrIdx = addrSelect ? parseInt(addrSelect.value, 10) : 0;

  // Read payment type and original index from selected option
  let payType = 'sap';
  let payIdx = 0;
  let stripePaymentMethodId = null;
  if (paySelect) {
    const opt = paySelect.options[paySelect.selectedIndex];
    payType = opt ? (opt.dataset.type || 'sap') : 'sap';
    payIdx = opt ? parseInt(opt.dataset.index || '0', 10) : 0;
    if (payType === 'stripe') {
      const stripePays = App.stripeCards || [];
      const card = stripePays[payIdx];
      stripePaymentMethodId = card ? card.id : null;
    }
  }

  // Disable cart buttons while preparing
  const cartCard = btn.closest('.cart-card');
  if (cartCard) cartCard.querySelectorAll('button').forEach(b => b.disabled = true);

  try {
    // Step 1: Prepare checkout (set address + delivery mode + payment)
    const r = await fetch(`${API}/checkout/prepare`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: App.sessionId,
        address_index: addrIdx,
        payment_index: payIdx,
        payment_type: payType,
      }),
    });
    const d = await r.json();

    if (!d.success) {
      appendError(`Checkout failed: ${d.error || 'Unknown error'}`);
      if (cartCard) cartCard.querySelectorAll('button').forEach(b => b.disabled = false);
      return;
    }

    // Store payment context for the place step
    d._paymentType = payType;
    d._stripePaymentMethodId = stripePaymentMethodId;

    // Step 2: Show confirmation popup
    showQuickCheckoutConfirm(d);

  } catch (e) {
    appendError('Could not prepare checkout. Is the server running?');
    if (cartCard) cartCard.querySelectorAll('button').forEach(b => b.disabled = false);
  }
}

function showQuickCheckoutConfirm(data) {
  // Remove any existing confirmation overlay
  const existing = document.getElementById('quickCheckoutOverlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'quickCheckoutOverlay';
  overlay.className = 'qc-overlay';

  const cart = data.cart || {};
  const addr = data.address || {};
  const pay = data.payment || {};

  const addrLine = addr.line1
    ? `${addr.firstName || ''} ${addr.lastName || ''}, ${addr.line1}, ${addr.town || ''} ${addr.postalCode || ''}`
    : 'No address';
  let payLine;
  if (pay.type === 'stripe') {
    payLine = pay.brand ? `${pay.brand.toUpperCase()} ****${pay.last4 || ''}` : 'Stripe card';
  } else {
    payLine = pay.cardType ? `${pay.cardType} ****${(pay.cardNumber || '').slice(-4)}` : 'SAP payment';
  }

  let itemsHTML = '';
  if (cart.entries && cart.entries.length > 0) {
    cart.entries.forEach(e => {
      itemsHTML += `<div class="qc-item">
        <span class="qc-item-name">${esc(e.product_name)}</span>
        <span class="qc-item-qty">x${e.quantity}</span>
        <span class="qc-item-price">${esc(e.total || '')}</span>
      </div>`;
    });
  }

  overlay.innerHTML = `<div class="qc-card">
    <div class="qc-header">
      <span class="qc-header-icon">&#9889;</span>
      <span class="qc-header-title">Confirm Order</span>
    </div>
    <div class="qc-body">
      ${itemsHTML ? `<div class="qc-items">${itemsHTML}</div>` : ''}
      <div class="qc-detail-row">
        <div class="qc-detail-icon">&#127968;</div>
        <div>
          <div class="qc-detail-label">Delivery</div>
          <div class="qc-detail-value">${esc(addrLine)}</div>
        </div>
      </div>
      <div class="qc-detail-row">
        <div class="qc-detail-icon">&#128179;</div>
        <div>
          <div class="qc-detail-label">Payment</div>
          <div class="qc-detail-value">${esc(payLine)}</div>
        </div>
      </div>
      ${cart.total ? `<div class="qc-total">
        <span>Total</span>
        <span class="qc-total-value">${esc(cart.total)}</span>
      </div>` : ''}
    </div>
    <div class="qc-actions">
      <button class="qc-btn-confirm" onclick="confirmQuickCheckout()">
        <span>&#128274;</span> Confirm &amp; Pay${cart.total ? ' ' + esc(cart.total) : ''}
      </button>
      <button class="qc-btn-cancel" onclick="cancelQuickCheckout()">Cancel</button>
    </div>
  </div>`;

  // Store payment context for the place step
  overlay.dataset.paymentType = data._paymentType || 'sap';
  overlay.dataset.stripePaymentMethodId = data._stripePaymentMethodId || '';

  document.body.appendChild(overlay);
  // Close on overlay click
  overlay.addEventListener('click', e => {
    if (e.target === overlay) cancelQuickCheckout();
  });
}

async function confirmQuickCheckout() {
  const overlay = document.getElementById('quickCheckoutOverlay');
  if (!overlay) return;

  // Disable buttons and show processing
  overlay.querySelectorAll('button').forEach(b => b.disabled = true);
  const confirmBtn = overlay.querySelector('.qc-btn-confirm');
  if (confirmBtn) confirmBtn.textContent = 'Placing order...';

  try {
    const paymentType = overlay.dataset.paymentType || 'sap';
    const stripePaymentMethodId = overlay.dataset.stripePaymentMethodId || '';

    const placeBody = {
      session_id: App.sessionId,
      payment_type: paymentType,
    };
    if (paymentType === 'stripe' && stripePaymentMethodId) {
      placeBody.stripe_payment_method_id = stripePaymentMethodId;
    }

    const r = await fetch(`${API}/checkout/place`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(placeBody),
    });

    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: `HTTP ${r.status}` }));
      overlay.remove();
      appendError(`Order failed: ${err.detail || err.error || JSON.stringify(err)}`);
      document.querySelectorAll('.cart-card button').forEach(b => b.disabled = false);
      return;
    }

    const d = await r.json();
    overlay.remove();

    if (d.success) {
      // Show success — wrapped separately so a rendering glitch can't mask order success
      handleOrderSuccess(d);
    } else {
      appendError(`Order failed: ${d.error || 'Unknown error'}`);
      document.querySelectorAll('.cart-card button').forEach(b => b.disabled = false);
    }
  } catch (e) {
    console.error('confirmQuickCheckout error:', e);
    const ov = document.getElementById('quickCheckoutOverlay');
    if (ov) ov.remove();
    appendError('Failed to place order. Please try again.');
    document.querySelectorAll('.cart-card button').forEach(b => b.disabled = false);
  }
}

function handleOrderSuccess(d) {
  try {
    // Clear cart state
    App.cartData = { items: [], total: null, id: null, orderCode: d.order_code };
    updateCartUI();
    markStep('step-order', 'done');

    // Remove existing cart cards
    document.querySelectorAll('.bubble .cart-card').forEach(el => {
      const msgEl = el.closest('.msg');
      if (msgEl) msgEl.remove();
    });

    // Show order success card
    const orderText = `Total: ${d.total || 'N/A'}. Status: ${d.status || 'processing'}.`;
    appendMsg('agent', orderText, { order_code: d.order_code });
  } catch (e) {
    console.error('handleOrderSuccess rendering error:', e);
    // Order was placed even if rendering fails — show basic success
    appendMsg('agent', `Order placed successfully! Order code: ${d.order_code || 'unknown'}`);
  }
}

function cancelQuickCheckout() {
  const overlay = document.getElementById('quickCheckoutOverlay');
  if (overlay) overlay.remove();
  // Re-enable cart buttons
  document.querySelectorAll('.cart-card button').forEach(b => b.disabled = false);
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('quickCheckoutOverlay')) cancelQuickCheckout();
});

// ── Cart sync from response ─────────────────────────────────────────────────

function syncCartFromResponse(d) {
  if (d.cart_id && d.cart_id !== App.cartData.id) App.cartData.id = d.cart_id;
  if (d.order_code) App.cartData.orderCode = d.order_code;
  if (d.saved_addresses && d.saved_addresses.length > 0) App.savedAddresses = d.saved_addresses;
  if (d.sap_payment_details && d.sap_payment_details.length > 0) App.sapPaymentDetails = d.sap_payment_details;

  // Sync sidebar cart from structured cart data
  if (d.cart && d.cart.entries && d.cart.entries.length > 0) {
    App.cartData.items = d.cart.entries.map(e => ({
      name: e.product_name,
      qty: e.quantity,
      price: e.total || e.base_price || '',
    }));
    App.cartData.total = d.cart.total || null;
    App.cartData.id = d.cart.cart_id || App.cartData.id;
    updateCartUI();
  }
}

// ── Order confirmation ───────────────────────────────────────────────────────

function showOrderConfirmDirect(replyText) {
  const msgs   = document.getElementById('messages');
  const div    = document.createElement('div');
  div.className = 'msg agent';

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = 'AI';

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = buildCheckoutConfirmHTML(replyText || '');

  div.appendChild(avatar);
  div.appendChild(bubble);
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

async function approveOrder(approved, btn) {
  const card = btn.closest('.checkout-card') || btn.closest('.confirm-banner');
  if (card) {
    card.querySelectorAll('button').forEach(b => b.disabled = true);
    if (approved) card.classList.add('processing');
  }

  const typing = appendTyping();
  try {
    const r = await fetch(`${API}/chat/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: App.sessionId, approved }),
    });
    typing.remove();
    const d = await r.json();

    if (card) card.remove();

    const msgDiv = appendMsg('agent', d.reply, d);
    appendSuggestions(msgDiv, d.suggestions);

    if (d.order_code) {
      App.cartData.orderCode = d.order_code;
      markStep('step-order', 'done');
    }
  } catch {
    typing.remove();
    if (card) {
      card.classList.remove('processing');
      card.querySelectorAll('button').forEach(b => b.disabled = false);
    }
    appendError('Approval request failed. Please try again.');
  }
}

// ── DOM helpers ──────────────────────────────────────────────────────────────

function hideWelcome() {
  const w = document.getElementById('welcome');
  if (w) w.remove();
}

function appendMsg(role, text, data) {
  const msgs   = document.getElementById('messages');
  const div    = document.createElement('div');
  div.className = `msg ${role}`;

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'agent' ? 'AI' : 'U';

  const bubble = document.createElement('div');
  bubble.className = 'bubble';

  if (role === 'agent') {
    text = stripSuggestionsBlock(text);

    // Remove previous structured cards (replace, not stack)
    if (data && (data.products || data.product_detail || data.cart)) {
      removeExistingCards(data);
    }

    if (data && data.order_code) {
      // Order success card
      bubble.innerHTML = buildOrderSuccessHTML(text, data.order_code);

    } else if (data && data.product_detail && data.product_detail.code) {
      // Product detail card
      const intro = extractIntro(text);
      if (intro) bubble.innerHTML = `<div class="pd-intro-text">${formatText(intro)}</div>`;
      bubble.innerHTML += buildProductDetailCard(data.product_detail);

      // If we also have cart data, append cart card below
      if (data.cart && data.cart.entries && data.cart.entries.length > 0) {
        bubble.innerHTML += buildCartCardHTML(data.cart);
      }

    } else if (data && data.products && data.products.length > 0) {
      // Product cards with optional intro text
      const intro = extractIntro(text);
      bubble.innerHTML = buildProductCardsHTML(intro, data.products, '');

      // If we also have cart data, append cart card below products
      if (data.cart && data.cart.entries && data.cart.entries.length > 0) {
        bubble.innerHTML += buildCartCardHTML(data.cart);
      }

    } else if (data && data.cart && data.cart.entries && data.cart.entries.length > 0) {
      // Cart card with intro text
      const intro = extractIntro(text);
      if (intro) bubble.innerHTML = `<div class="cart-intro-text">${formatText(intro)}</div>`;
      bubble.innerHTML += buildCartCardHTML(data.cart);

    } else {
      bubble.innerHTML = formatText(text);
    }
  } else {
    bubble.innerHTML = formatText(text);
  }

  div.appendChild(avatar);
  div.appendChild(bubble);
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
  return div;
}

function removeExistingCards(data) {
  const msgs = document.getElementById('messages');
  if (!msgs) return;

  // Remove previous product cards if new search or detail replaces them
  if (data.products && data.products.length > 0) {
    msgs.querySelectorAll('.bubble .product-cards-grid').forEach(el => {
      const msgEl = el.closest('.msg');
      if (msgEl) msgEl.remove();
    });
  }

  // Remove previous detail card if new detail replaces it
  if (data.product_detail && data.product_detail.code) {
    msgs.querySelectorAll('.bubble .pd-card').forEach(el => {
      const msgEl = el.closest('.msg');
      if (msgEl) msgEl.remove();
    });
  }

  // Remove previous cart card if new cart data replaces it
  if (data.cart && data.cart.entries && data.cart.entries.length > 0) {
    msgs.querySelectorAll('.bubble .cart-card').forEach(el => {
      const msgEl = el.closest('.msg');
      if (msgEl) msgEl.remove();
    });
  }
}

function extractIntro(text) {
  return text.split('\n')
    .filter(l => {
      const t = l.trim();
      return t && !/^#{1,4}\s+/.test(t) && !/^\s*[-•]\s+/.test(t) && !/^\s*\d+[.)]\s+/.test(t);
    })
    .slice(0, 2)
    .join('\n')
    .trim();
}

function appendSuggestions(msgDiv, suggestions) {
  if (!suggestions || !suggestions.length || !msgDiv) return;
  const bubble = msgDiv.querySelector('.bubble');
  if (bubble) bubble.appendChild(buildSuggestionButtons(suggestions));
}

function appendTyping() {
  const msgs = document.getElementById('messages');
  const div  = document.createElement('div');
  div.className = 'msg agent';
  div.innerHTML = `<div class="avatar">AI</div>
    <div class="bubble"><div class="typing"><span></span><span></span><span></span></div></div>`;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
  return div;
}

function appendError(msg) {
  const msgs = document.getElementById('messages');
  const div  = document.createElement('div');
  div.className = 'msg agent';
  div.innerHTML = `<div class="avatar">AI</div><div class="error-bubble">${esc(msg)}</div>`;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}
