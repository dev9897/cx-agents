/**
 * renderers.js — HTML builders for rich UI components.
 *
 * Each function takes structured data and returns an HTML string
 * or a DOM element ready for insertion.
 */

// ── Product cards ────────────────────────────────────────────────────────────

function buildProductCardsHTML(intro, products, outro) {
  let html = '';
  if (intro) html += `<div class="product-cards-intro">${formatText(intro)}</div>`;

  const hasCategories = products.some(p => p.category);

  if (hasCategories) {
    const groups = [];
    let curCat = null, curGroup = null;
    products.forEach(p => {
      const cat = p.category || 'Other';
      if (cat !== curCat) {
        curCat = cat;
        curGroup = { category: cat, items: [] };
        groups.push(curGroup);
      }
      curGroup.items.push(p);
    });

    groups.forEach(g => {
      html += `<div class="product-category-label">${esc(g.category)}</div>`;
      html += '<div class="product-cards-wrap"><div class="product-cards">';
      g.items.forEach(p => { html += buildProductCard(p); });
      html += '</div>';
      if (g.items.length > 2) html += '<div class="product-scroll-hint">&#8592; scroll for more &#8594;</div>';
      html += '</div>';
    });
  } else {
    html += '<div class="product-cards-wrap"><div class="product-cards">';
    products.forEach(p => { html += buildProductCard(p); });
    html += '</div>';
    if (products.length > 2) html += '<div class="product-scroll-hint">&#8592; scroll for more &#8594;</div>';
    html += '</div>';
  }

  if (outro) html += `<div style="margin-top:8px">${formatText(outro)}</div>`;
  return html;
}

function buildProductCard(p) {
  const stockClass = getStockClass(p.stock);
  const stockLabel = getStockLabel(p.stock);
  const ratingHTML = p.rating ? buildRatingStars(p.rating) : '';

  const safeName = esc(p.name).replace(/'/g, "\\'");
  const addMsg   = p.code ? `Add product ${esc(p.code)} to my cart` : `Add ${safeName} to my cart`;
  const detailMsg = p.code ? `Show details for product ${esc(p.code)}` : `Show me details for ${safeName}`;

  return `<div class="product-card">
    <div class="product-card-name">${esc(p.name)}</div>
    ${p.code ? `<div class="product-card-code">${esc(p.code)}</div>` : ''}
    ${p.price ? `<div class="product-card-price">${esc(p.price)}</div>` : ''}
    <div class="product-card-meta">
      ${stockLabel ? `<span class="product-card-stock ${stockClass}">${stockLabel}</span>` : ''}
      ${ratingHTML}
    </div>
    <div class="product-card-actions">
      <button class="product-card-btn add-cart" onclick="sendQuick('${addMsg}')">Add to Cart</button>
      <button class="product-card-btn details" onclick="sendQuick('${detailMsg}')">Details</button>
    </div>
  </div>`;
}

// ── Order success ────────────────────────────────────────────────────────────

function buildOrderSuccessHTML(text, orderCode) {
  let cleanText = text
    .replace(/order\s*(?:has been|was)?\s*(?:placed|confirmed)\s*successfully?!?\s*/i, '')
    .replace(/^[\s!.]+/, '')
    .trim();

  let html = '<div class="order-success-card">';
  html += '<div class="order-success-check">&#10003;</div>';
  html += '<div class="order-success-title">Order Placed!</div>';
  if (orderCode) html += `<div class="order-success-code">#${esc(orderCode)}</div>`;
  if (cleanText) html += `<div class="order-success-text">${formatText(cleanText)}</div>`;
  html += '<div class="order-success-actions">';
  if (orderCode) html += `<button class="order-success-btn" onclick="sendQuick('Show me order ${esc(orderCode)}')">View Order Details</button>`;
  html += `<button class="order-success-btn" onclick="sendQuick('Show me what products you have')">Continue Shopping</button>`;
  html += '</div></div>';
  return html;
}

// ── Checkout confirmation card ───────────────────────────────────────────────

function parseCheckoutSummary(text) {
  const summary = { items: [], total: null, card: null };

  const cardMatch = text.match(/(visa|mastercard|amex|discover|card)\s*(?:[·•*]{2,}\s*|ending\s+in\s+|[·•*]+)(\d{4})/i);
  if (cardMatch) summary.card = { brand: cardMatch[1], last4: cardMatch[2] };

  const totalMatch = text.match(/total[:\s]*\$?([\d,]+\.?\d*)/i);
  if (totalMatch) summary.total = '$' + totalMatch[1];

  const itemPattern = /(?:^|\n)\s*(?:\d+[.)]|[-•])\s+\*?\*?(.+?)\*?\*?\s*(?:[-–—:]\s*)?(?:\$[\d,]+\.?\d*)?.*?(?:\$[\d,]+\.?\d*)/gm;
  let match;
  while ((match = itemPattern.exec(text)) !== null) {
    const line = match[0].trim();
    const nameMatch  = line.match(/(?:\d+[.)]|[-•])\s+\*?\*?(.+?)\*?\*?\s*[-–—]/);
    const priceMatch = line.match(/\$([\d,]+\.?\d*)/);
    if (nameMatch && priceMatch) {
      summary.items.push({ name: nameMatch[1].replace(/\*\*/g, '').trim(), price: '$' + priceMatch[1] });
    }
  }
  return summary;
}

function buildCheckoutConfirmHTML(agentText) {
  const summary = parseCheckoutSummary(agentText);

  let html = '<div class="checkout-card">';
  html += '<div class="checkout-card-header">';
  html += '<span class="checkout-icon">&#9889;</span>';
  html += '<span class="checkout-title">One-Click Checkout</span>';
  html += '<span class="checkout-badge">ACP</span>';
  html += '</div>';

  html += '<div class="checkout-card-body">';

  if (summary.items.length > 0) {
    html += '<div class="checkout-card-items">';
    summary.items.forEach(item => {
      html += `<div class="checkout-card-item">
        <span class="checkout-card-item-name">${esc(item.name)}</span>
        <span class="checkout-card-item-price">${esc(item.price)}</span>
      </div>`;
    });
    html += '</div>';
  }

  if (summary.card) {
    const icon = getBrandIcon(summary.card.brand);
    html += `<div class="checkout-card-row">
      <div class="checkout-card-row-icon">${icon}</div>
      <div>
        <div class="checkout-card-row-label">Payment</div>
        <div class="checkout-card-row-value">${esc(summary.card.brand)} <span class="mono">&bull;&bull;&bull;&bull; ${esc(summary.card.last4)}</span></div>
      </div>
    </div>`;
  }

  if (summary.total) {
    html += `<div class="checkout-card-total">
      <span class="checkout-card-total-label">Total</span>
      <span class="checkout-card-total-value">${esc(summary.total)}</span>
    </div>`;
  }

  if (!summary.items.length && !summary.card && !summary.total) {
    html += '<div class="checkout-card-note">Your saved card will be charged and the order placed immediately.</div>';
  }

  html += '</div>'; // end body

  html += '<div class="checkout-card-actions">';
  html += `<button class="btn-pay" onclick="approveOrder(true, this)">
    <span class="lock-icon">&#128274;</span> Confirm &amp; Pay${summary.total ? ' ' + esc(summary.total) : ''}
  </button>`;
  html += '<button class="btn-cancel-order" onclick="approveOrder(false, this)">Cancel</button>';
  html += '</div></div>';
  return html;
}

// ── Suggestion buttons (structured from LLM) ────────────────────────────────

function buildSuggestionButtons(suggestions) {
  const wrap = document.createElement('div');
  wrap.className = 'action-buttons';

  suggestions.forEach(s => {
    const el = document.createElement('button');
    el.className = `action-btn action-btn-${s.primary ? 'primary' : 'secondary'}`;
    el.textContent = s.label;
    el.addEventListener('click', () => {
      wrap.querySelectorAll('button').forEach(b => { b.disabled = true; b.classList.add('used'); });
      el.classList.add('selected');
      sendQuick(s.value);
    });
    wrap.appendChild(el);
  });

  return wrap;
}

// ── Shared helpers ───────────────────────────────────────────────────────────

function getStockClass(stock) {
  if (!stock) return '';
  const s = stock.toLowerCase();
  if (s.includes('in stock') || s === 'instock') return 'in-stock';
  if (s.includes('low') || s.includes('limited')) return 'low-stock';
  if (s.includes('out') || s.includes('unavailable')) return 'out-of-stock';
  return 'in-stock';
}

function getStockLabel(stock) {
  if (!stock) return '';
  const s = stock.toLowerCase();
  if (s.includes('in stock') || s === 'instock') return 'In Stock';
  if (s.includes('low') || s.includes('limited')) return 'Low Stock';
  if (s.includes('out')) return 'Out of Stock';
  return stock;
}

function buildRatingStars(rating) {
  const full  = Math.floor(rating);
  const half  = rating - full >= 0.5 ? 1 : 0;
  const empty = 5 - full - half;
  let stars = '';
  for (let i = 0; i < full; i++)  stars += '&#9733;';
  for (let i = 0; i < half; i++)  stars += '&#9733;';
  for (let i = 0; i < empty; i++) stars += '&#9734;';
  return `<span class="product-card-rating"><span class="stars">${stars}</span><span class="val">${rating.toFixed(1)}</span></span>`;
}
