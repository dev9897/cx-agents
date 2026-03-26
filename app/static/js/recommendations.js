/**
 * recommendations.js — Personalized product recommendations on login.
 *
 * Depends on: app.js, renderers.js, chat.js
 *
 * Flow:
 *   1. User logs in → fetchRecommendations() called
 *   2. GET /recommendations?session_id=xxx
 *   3. Renders a carousel of recommended products in the chat area
 */

// ── Fetch and display recommendations ───────────────────────────────────────

async function fetchRecommendations() {
  if (!App.sessionId || !App.currentUser) {
    console.log('[reco] skipped: no session or user');
    return;
  }

  try {
    console.log('[reco] fetching chat recommendations...');
    const r = await fetch(`${API}/recommendations?session_id=${encodeURIComponent(App.sessionId)}`);
    if (!r.ok) {
      console.warn('[reco] HTTP error:', r.status);
      return;
    }

    const data = await r.json();
    console.log('[reco] response:', data.success, data.recommendations?.length, 'items', data.message || '');
    if (!data.success || !data.recommendations || data.recommendations.length === 0) return;

    renderRecommendationCarousel(data.recommendations);
  } catch (e) {
    console.warn('[reco] fetch failed:', e);
  }
}

// ── Render carousel ─────────────────────────────────────────────────────────

function renderRecommendationCarousel(recommendations) {
  const msgs = document.getElementById('messages');
  if (!msgs) return;

  const div = document.createElement('div');
  div.className = 'msg agent';

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = 'AI';

  const bubble = document.createElement('div');
  bubble.className = 'bubble';

  let html = '<div class="reco-section">';
  html += '<div class="reco-header">';
  html += '<span class="reco-header-icon">&#9733;</span>';
  html += '<span class="reco-header-title">Top Picks for You</span>';
  html += `<span class="reco-header-subtitle">Based on your purchase history</span>`;
  html += '</div>';

  html += '<div class="reco-carousel">';
  recommendations.forEach(rec => {
    const name = stripHtml(rec.name || '');
    const safeName = esc(name).replace(/'/g, "\\'");
    const detailMsg = rec.code
      ? `Show details for product ${esc(rec.code)}`
      : `Show me details for ${safeName}`;

    const reasonLabel = _getReasonLabel(rec.reason);

    html += `<div class="reco-card" onclick="sendQuick('${detailMsg}')">`;

    // Image placeholder — products from Qdrant may not have image_url
    if (rec.image_url) {
      html += `<div class="reco-card-img"><img src="${esc(rec.image_url)}" alt="${esc(name)}" loading="lazy" onerror="this.parentElement.innerHTML='&#128247;'"/></div>`;
    } else {
      html += '<div class="reco-card-img">&#128247;</div>';
    }

    html += '<div class="reco-card-body">';
    html += `<div class="reco-card-name">${esc(name)}</div>`;
    if (rec.price) html += `<div class="reco-card-price">${esc(rec.price)}</div>`;
    if (reasonLabel) html += `<div class="reco-card-reason">${esc(reasonLabel)}</div>`;
    html += '</div></div>';
  });
  html += '</div>';

  if (recommendations.length > 2) {
    html += '<div class="reco-scroll-hint">&#8592; scroll for more &#8594;</div>';
  }

  html += '</div>';
  bubble.innerHTML = html;

  div.appendChild(avatar);
  div.appendChild(bubble);
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

function _getReasonLabel(reason) {
  switch (reason) {
    case 'collaborative': return 'Popular with similar buyers';
    case 'content': return 'Similar to your purchases';
    case 'both': return 'Top pick for you';
    case 'popular': return 'Trending now';
    default: return '';
  }
}

// ── Store page recommendations ──────────────────────────────────────────────

async function fetchStoreRecommendations() {
  if (!App.sessionId || !App.currentUser) {
    console.log('[reco-store] skipped: no session or user');
    return;
  }

  const section = document.getElementById('storeRecoSection');
  const carousel = document.getElementById('storeRecoCarousel');
  if (!section || !carousel) {
    console.warn('[reco-store] missing DOM elements');
    return;
  }

  try {
    console.log('[reco-store] fetching store recommendations...');
    const r = await fetch(`${API}/recommendations?session_id=${encodeURIComponent(App.sessionId)}`);
    if (!r.ok) {
      console.warn('[reco-store] HTTP error:', r.status);
      return;
    }

    const data = await r.json();
    if (!data.success || !data.recommendations || data.recommendations.length === 0) {
      section.style.display = 'none';
      return;
    }

    carousel.innerHTML = data.recommendations.map(rec => {
      const name = escapeHtml(rec.name || '');
      const reasonLabel = _getReasonLabel(rec.reason);
      return `
        <div class="store-reco-card" onclick="openProductDetail('${rec.code}')">
          <div class="store-reco-card-img">
            ${rec.image_url
              ? `<img src="${escapeHtml(rec.image_url)}" alt="${name}" loading="lazy" onerror="this.parentElement.innerHTML='&#128247;'">`
              : '&#128247;'}
          </div>
          <div class="store-reco-card-body">
            <div class="store-reco-card-name">${name}</div>
            ${rec.price ? `<div class="store-reco-card-price">${escapeHtml(rec.price)}</div>` : ''}
            ${reasonLabel ? `<div class="store-reco-card-reason">${escapeHtml(reasonLabel)}</div>` : ''}
          </div>
        </div>`;
    }).join('');

    section.style.display = 'block';
  } catch (e) {
    console.warn('Store recommendations fetch failed:', e);
  }
}
