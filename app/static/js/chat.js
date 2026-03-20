/**
 * chat.js — Message sending, receiving, typing indicator, order confirmation.
 *
 * Depends on: app.js, renderers.js
 *
 * Product data and suggestions come as structured JSON from the backend.
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
    parseCartFromReply(d);

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

// ── Cart parsing ─────────────────────────────────────────────────────────────

function parseCartFromReply(d) {
  if (d.cart_id && d.cart_id !== App.cartData.id) App.cartData.id = d.cart_id;
  if (d.order_code) App.cartData.orderCode = d.order_code;
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

    if (data && data.order_code) {
      // Order success — structured from backend
      bubble.innerHTML = buildOrderSuccessHTML(text, data.order_code);

    } else if (data && data.products && data.products.length > 0) {
      // Product cards — structured from backend, no text parsing
      const intro = text.split('\n').filter(l => {
        const t = l.trim();
        return t && !/^#{1,4}\s+/.test(t) && !/^\s*[-•]\s+/.test(t) && !/^\s*\d+[.)]\s+/.test(t);
      }).slice(0, 2).join('\n').trim();
      bubble.innerHTML = buildProductCardsHTML(intro, data.products, '');

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
