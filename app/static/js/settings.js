/**
 * settings.js — Settings modal with Stripe Elements card management and address CRUD.
 */

const Settings = {
  stripe: null,
  elements: null,
  cardElement: null,
  activeTab: 'cards',
};

// ── Open / Close ─────────────────────────────────────────────────────────────

function openSettings() {
  document.getElementById('settingsModal').classList.add('open');
  Settings.activeTab = 'cards';
  switchSettingsTab('cards');
  loadSavedCards();
}

function closeSettings() {
  document.getElementById('settingsModal').classList.remove('open');
  if (Settings.cardElement) {
    Settings.cardElement.unmount();
    Settings.cardElement = null;
  }
}

function handleSettingsOverlayClick(e) {
  if (e.target === document.getElementById('settingsModal')) closeSettings();
}

// ── Tabs ─────────────────────────────────────────────────────────────────────

function switchSettingsTab(tab) {
  Settings.activeTab = tab;

  document.querySelectorAll('.settings-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tab);
  });
  document.querySelectorAll('.settings-panel').forEach(p => {
    p.classList.toggle('active', p.id === `panel-${tab}`);
  });

  if (tab === 'cards') loadSavedCards();
  if (tab === 'addresses') loadSavedAddresses();
}

// ── Cards ────────────────────────────────────────────────────────────────────

async function loadSavedCards() {
  const container = document.getElementById('cardsList');
  if (!getUserEmail()) {
    container.innerHTML = '<div class="empty-state"><div class="empty-icon">&#128179;</div><p>Sign in to manage your payment methods.</p></div>';
    document.getElementById('addCardSection').style.display = 'none';
    return;
  }

  container.innerHTML = '<div class="empty-state"><p>Loading...</p></div>';
  document.getElementById('addCardSection').style.display = 'block';

  try {
    const r = await fetch(`${API}/payment/cards?email=${encodeURIComponent(getUserEmail())}`);
    const d = await r.json();
    const cards = d.cards || [];

    if (cards.length === 0) {
      container.innerHTML = '<div class="empty-state"><div class="empty-icon">&#128179;</div><p>No saved cards yet. Add one below.</p></div>';
    } else {
      container.innerHTML = '';
      cards.forEach(card => {
        container.appendChild(buildCardElement(card));
      });
    }
  } catch {
    container.innerHTML = '<div class="empty-state"><p>Failed to load cards.</p></div>';
  }
}

function buildCardElement(card) {
  const div = document.createElement('div');
  div.className = 'saved-card';
  div.dataset.pmId = card.id;

  const brandClass = (card.brand || '').toLowerCase().replace(/\s+/g, '');
  const brandIcon  = getBrandIcon(card.brand);

  div.innerHTML = `
    <div class="card-brand-icon ${brandClass}">${brandIcon}</div>
    <div class="card-info">
      <div class="card-brand">${esc(card.brand || 'Card')} &bull;&bull;&bull;&bull; ${esc(card.last4)}</div>
      <div class="card-expiry">Expires ${String(card.exp_month).padStart(2, '0')}/${card.exp_year}</div>
    </div>
    <button class="card-remove" onclick="removeCard('${esc(card.id)}')" title="Remove card">&#10005;</button>`;

  return div;
}

function getBrandIcon(brand) {
  const b = (brand || '').toLowerCase();
  if (b.includes('visa'))       return '<span style="font-weight:700;color:#1a1f71">VISA</span>';
  if (b.includes('mastercard')) return '<span style="font-weight:700;color:#eb001b">MC</span>';
  if (b.includes('amex'))       return '<span style="font-weight:700;color:#006fcf">AMEX</span>';
  return '<span style="font-weight:700;color:var(--muted)">CARD</span>';
}

async function removeCard(pmId) {
  if (!confirm('Remove this card?')) return;
  try {
    await fetch(`${API}/payment/cards/${encodeURIComponent(pmId)}`, { method: 'DELETE' });
    loadSavedCards();
  } catch {
    alert('Failed to remove card.');
  }
}

// ── Add Card (Stripe Elements) ───────────────────────────────────────────────

async function initStripeElements() {
  const addBtn  = document.getElementById('addCardBtn');
  const formWrap = document.getElementById('cardFormWrap');

  if (formWrap.style.display === 'block') {
    formWrap.style.display = 'none';
    addBtn.textContent = '+ Add New Card';
    if (Settings.cardElement) {
      Settings.cardElement.unmount();
      Settings.cardElement = null;
    }
    return;
  }

  addBtn.textContent = 'Cancel';
  formWrap.style.display = 'block';
  document.getElementById('cardFormError').textContent = '';

  // Load Stripe.js if not already loaded
  if (!Settings.stripe) {
    try {
      const r = await fetch(`${API}/payment/config`);
      const d = await r.json();
      if (!d.publishable_key) {
        document.getElementById('cardFormError').textContent = 'Stripe is not configured.';
        return;
      }
      Settings.stripe = Stripe(d.publishable_key);
    } catch {
      document.getElementById('cardFormError').textContent = 'Could not load Stripe configuration.';
      return;
    }
  }

  Settings.elements = Settings.stripe.elements();
  Settings.cardElement = Settings.elements.create('card', {
    style: {
      base: {
        fontSize: '15px',
        fontFamily: "'Inter', -apple-system, sans-serif",
        color: '#1b2537',
        '::placeholder': { color: '#8993a4' },
      },
      invalid: { color: '#c23934' },
    },
  });

  Settings.cardElement.mount('#stripeCardElement');

  Settings.cardElement.on('focus', () => {
    document.querySelector('.stripe-element-wrap').classList.add('focused');
  });
  Settings.cardElement.on('blur', () => {
    document.querySelector('.stripe-element-wrap').classList.remove('focused');
  });
  Settings.cardElement.on('change', e => {
    const errEl = document.getElementById('cardFormError');
    errEl.textContent = e.error ? e.error.message : '';
    document.querySelector('.stripe-element-wrap').classList.toggle('error', !!e.error);
  });
}

async function saveCard() {
  if (!Settings.stripe || !Settings.cardElement) return;

  const saveBtn = document.getElementById('saveCardBtn');
  const errEl   = document.getElementById('cardFormError');
  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving...';
  errEl.textContent = '';

  try {
    // Create SetupIntent on backend
    const sir = await fetch(`${API}/payment/setup-intent`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: App.sessionId || '', email: getUserEmail(), name: App.currentUser }),
    });

    if (!sir.ok) {
      const errData = await sir.json().catch(() => ({}));
      errEl.textContent = errData.detail || `Setup failed (HTTP ${sir.status})`;
      return;
    }

    const sid = await sir.json();
    if (!sid.client_secret) {
      errEl.textContent = sid.detail || 'Could not create setup intent.';
      return;
    }

    // Confirm card setup with Stripe.js (may trigger 3DS/OTP)
    const result = await Settings.stripe.confirmCardSetup(
      sid.client_secret,
      { payment_method: { card: Settings.cardElement } }
    );

    if (result.error) {
      errEl.textContent = result.error.message;
      return;
    }

    const status = result.setupIntent && result.setupIntent.status;
    if (status === 'succeeded') {
      // Reset form
      document.getElementById('cardFormWrap').style.display = 'none';
      document.getElementById('addCardBtn').textContent = '+ Add New Card';
      if (Settings.cardElement) {
        Settings.cardElement.unmount();
        Settings.cardElement = null;
      }
      loadSavedCards();
    } else {
      errEl.textContent = `Card setup status: ${status || 'unknown'}. Please try again.`;
    }
  } catch (e) {
    console.error('saveCard error:', e);
    errEl.textContent = e.message || 'An unexpected error occurred.';
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = 'Save Card';
  }
}

// ── Addresses ────────────────────────────────────────────────────────────────

async function loadSavedAddresses() {
  const container = document.getElementById('addressesList');
  if (!getUserEmail()) {
    container.innerHTML = '<div class="empty-state"><div class="empty-icon">&#127968;</div><p>Sign in to manage your addresses.</p></div>';
    document.getElementById('addAddressSection').style.display = 'none';
    return;
  }

  container.innerHTML = '<div class="empty-state"><p>Loading...</p></div>';
  document.getElementById('addAddressSection').style.display = 'block';

  try {
    const r = await fetch(`${API}/payment/addresses?email=${encodeURIComponent(getUserEmail())}`);
    const d = await r.json();
    const addresses = d.addresses || [];

    if (addresses.length === 0) {
      container.innerHTML = '<div class="empty-state"><div class="empty-icon">&#127968;</div><p>No saved addresses yet.</p></div>';
    } else {
      container.innerHTML = '';
      addresses.forEach(addr => {
        container.appendChild(buildAddressElement(addr));
      });
    }
  } catch {
    container.innerHTML = '<div class="empty-state"><p>Failed to load addresses.</p></div>';
  }
}

function buildAddressElement(addr) {
  const div = document.createElement('div');
  div.className = 'saved-address';

  const line2 = addr.line2 ? `${esc(addr.line2)}, ` : '';
  const state = addr.state ? `${esc(addr.state)} ` : '';

  div.innerHTML = `
    <div class="address-icon">&#127968;</div>
    <div class="address-info">
      <div class="address-label">${esc(addr.label || addr.name)}</div>
      <div class="address-detail">${esc(addr.line1)}, ${line2}${esc(addr.city)}, ${state}${esc(addr.postal_code)} ${esc(addr.country)}</div>
    </div>
    <button class="address-remove" onclick="removeAddress('${esc(addr.id)}')" title="Remove address">&#10005;</button>`;

  return div;
}

async function removeAddress(addrId) {
  if (!confirm('Remove this address?')) return;
  try {
    await fetch(
      `${API}/payment/addresses/${encodeURIComponent(addrId)}?email=${encodeURIComponent(getUserEmail())}`,
      { method: 'DELETE' }
    );
    loadSavedAddresses();
  } catch {
    alert('Failed to remove address.');
  }
}

function toggleAddressForm() {
  const form = document.getElementById('addressFormWrap');
  const btn  = document.getElementById('addAddressBtn');
  if (form.style.display === 'block') {
    form.style.display = 'none';
    btn.textContent = '+ Add New Address';
  } else {
    form.style.display = 'block';
    btn.textContent = 'Cancel';
    document.getElementById('addrName').focus();
  }
}

async function saveAddress() {
  const fields = ['addrName', 'addrLine1', 'addrCity', 'addrPostal', 'addrCountry'];
  const vals = {};
  for (const fid of fields) {
    vals[fid] = document.getElementById(fid).value.trim();
    if (!vals[fid]) {
      alert('Please fill in all required fields.');
      return;
    }
  }

  const saveBtn = document.getElementById('saveAddressBtn');
  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving...';

  try {
    const body = {
      email: getUserEmail(),
      address: {
        name: vals.addrName,
        line1: vals.addrLine1,
        line2: document.getElementById('addrLine2').value.trim(),
        city: vals.addrCity,
        state: document.getElementById('addrState').value.trim(),
        postal_code: vals.addrPostal,
        country: vals.addrCountry,
      },
    };

    await fetch(`${API}/payment/addresses`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    // Reset form
    document.getElementById('addressFormWrap').style.display = 'none';
    document.getElementById('addAddressBtn').textContent = '+ Add New Address';
    fields.forEach(f => document.getElementById(f).value = '');
    document.getElementById('addrLine2').value = '';
    document.getElementById('addrState').value = '';
    loadSavedAddresses();
  } catch {
    alert('Failed to save address.');
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = 'Save Address';
  }
}

// ── Keyboard ─────────────────────────────────────────────────────────────────

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('settingsModal').classList.contains('open')) {
    closeSettings();
  }
});
