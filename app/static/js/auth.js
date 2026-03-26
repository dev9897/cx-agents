/**
 * auth.js — Login modal, authentication state, login/logout logic.
 */

function openLogin() {
  if (App.currentUser) {
    if (confirm(`Sign out of ${App.currentUser}?`)) doLogout();
    return;
  }
  document.getElementById('loginError').classList.remove('show');
  document.getElementById('loginUser').value = '';
  document.getElementById('loginPass').value = '';
  document.getElementById('loginModal').classList.add('open');
  setTimeout(() => document.getElementById('loginUser').focus(), 250);
}

function closeLogin() {
  document.getElementById('loginModal').classList.remove('open');
}

function handleOverlayClick(e) {
  if (e.target === document.getElementById('loginModal')) closeLogin();
}

function showLoginError(msg) {
  const el = document.getElementById('loginError');
  el.textContent = msg;
  el.classList.add('show');
}

function updateAuthUI(username) {
  const btn   = document.getElementById('authBtn');
  const label = document.getElementById('authBtnLabel');
  const pill  = document.getElementById('userPill');
  const name  = document.getElementById('userPillName');
  if (username) {
    btn.classList.add('logged-in');
    label.textContent  = username;
    pill.style.display = 'flex';
    name.textContent   = username;
  } else {
    btn.classList.remove('logged-in');
    label.textContent  = 'Sign In';
    pill.style.display = 'none';
  }
}

async function doLogin() {
  const username = document.getElementById('loginUser').value.trim();
  const password = document.getElementById('loginPass').value;
  const btn      = document.getElementById('loginBtn');
  const spinner  = document.getElementById('loginSpinner');
  const label    = document.getElementById('loginBtnLabel');

  if (!username || !password) {
    showLoginError('Please enter your username and password.');
    return;
  }

  btn.disabled = true;
  spinner.style.display = 'block';
  label.textContent = 'Signing in...';
  document.getElementById('loginError').classList.remove('show');

  try {
    const body = { username, password };
    if (App.sessionId) body.session_id = App.sessionId;
    const r = await fetch(`${API}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) {
      showLoginError(d.detail || 'Login failed. Please check your credentials.');
      return;
    }

    App.sessionId   = d.session_id;
    App.currentUser = d.username;
    App.currentUserEmail = d.email || (username.includes('@') ? username : `${username}@store.local`);
    App.currentUserFirstName = d.first_name || '';
    App.currentUserLastName = d.last_name || '';
    App.savedAddresses = d.saved_addresses || [];
    App.sapPaymentDetails = d.sap_payment_details || [];
    App.stripeCards = d.stripe_cards || [];
    closeLogin();
    updateAuthUI(d.username);

    // Open chat widget so user sees welcome + recommendations
    if (typeof openChatWidget === 'function') openChatWidget();
    if (typeof switchCwTab === 'function') switchCwTab('chat');

    appendMsg(
      'agent',
      `Welcome back, **${d.username}**! You're now signed in. How can I help you today? You can browse cameras, memory cards, accessories, or any electronics from the catalog.`
    );
    updateSidebarUser(d.username);

    // Fetch personalized recommendations after login
    if (typeof fetchRecommendations === 'function') {
      setTimeout(() => fetchRecommendations(), 500);
    }
  } catch {
    showLoginError('Could not reach the server. Please try again.');
  } finally {
    btn.disabled = false;
    spinner.style.display = 'none';
    label.textContent = 'Sign In';
    document.getElementById('loginPass').value = '';
  }
}

async function doLogout() {
  if (!App.sessionId) return;
  try {
    await fetch(
      `${API}/auth/logout?session_id=${encodeURIComponent(App.sessionId)}`,
      { method: 'POST' }
    );
  } catch {}
  App.currentUser = null;
  App.currentUserEmail = null;
  updateAuthUI(null);
  updateSidebarUser(null);
  appendMsg('agent', 'You have been signed out. You can continue browsing the electronics catalog as a guest.');
}

// ── Key listeners ────────────────────────────────────────────────────────────

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeLogin();
});

document.addEventListener('DOMContentLoaded', () => {
  const passInput = document.getElementById('loginPass');
  if (passInput) {
    passInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') doLogin();
    });
  }
});
