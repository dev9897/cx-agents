/**
 * renderers.js — Pure HTML builders for UI components.
 *
 * No business logic here — only takes data and returns HTML/DOM elements.
 */

// ── Product Cards ───────────────────────────────────────────────────────────

function buildProductCardsHTML(intro, products, outro) {
  let html = '';
  if (intro) html += `<div class="product-cards-intro">${formatText(intro)}</div>`;

  html += '<div class="product-cards-grid">';
  products.forEach(p => { html += buildProductCard(p); });
  html += '</div>';
  if (products.length > 2) {
    html += '<div class="product-scroll-hint">&#8592; scroll for more &#8594;</div>';
  }

  if (outro) html += `<div style="margin-top:8px">${formatText(outro)}</div>`;
  return html;
}

/** Strip HTML tags from SAP response fields (e.g. <em class="search-results-...">). */
function stripHtml(str) {
  if (!str) return '';
  return str.replace(/<[^>]*>/g, '').trim();
}

function buildProductCard(p) {
  const name = stripHtml(p.name);
  const stockClass = getStockClass(p.stock);
  const stockLabel = getStockLabel(p.stock);
  const ratingHTML = p.rating ? buildRatingStars(p.rating) : '';

  const safeName = esc(name).replace(/'/g, "\\'");
  const addMsg = p.code
    ? `Add product ${esc(p.code)} to my cart`
    : `Add ${safeName} to my cart`;
  const detailMsg = p.code
    ? `Show details for product ${esc(p.code)}`
    : `Show me details for ${safeName}`;

  const imgHTML = p.image_url
    ? `<div class="product-card-img"><img src="${esc(p.image_url)}" alt="${esc(name)}" loading="lazy" onerror="this.parentElement.classList.add('no-img')"/></div>`
    : '<div class="product-card-img no-img"><span>&#128247;</span></div>';

  return `<div class="product-card">
    ${imgHTML}
    <div class="product-card-body">
      <div class="product-card-name">${esc(name)}</div>
      ${p.code ? `<div class="product-card-code">${esc(p.code)}</div>` : ''}
      <div class="product-card-price-row">
        ${p.price ? `<div class="product-card-price">${esc(p.price)}</div>` : ''}
        <div class="product-card-meta">
          ${stockLabel ? `<span class="product-card-stock ${stockClass}">${stockLabel}</span>` : ''}
        </div>
      </div>
      ${ratingHTML}
      <div class="product-card-actions">
        <button class="product-card-btn add-cart" onclick="sendQuick('${addMsg}')">
          <span class="btn-icon">&#128722;</span> Add to Cart
        </button>
        <button class="product-card-btn details" onclick="sendQuick('${detailMsg}')">Details</button>
      </div>
    </div>
  </div>`;
}


// ── Product Detail Card ─────────────────────────────────────────────────────

function buildProductDetailCard(pd) {
  if (!pd || !pd.code) return '';

  const name = stripHtml(pd.name || '');
  const stockClass = getStockClass(pd.stock);
  const stockLabel = getStockLabel(pd.stock);
  const ratingHTML = pd.rating ? buildRatingStars(pd.rating) : '';

  const imgHTML = pd.image_url
    ? `<div class="pd-card-img"><img src="${esc(pd.image_url)}" alt="${esc(name)}" loading="lazy" onerror="this.parentElement.classList.add('no-img')"/></div>`
    : '<div class="pd-card-img no-img"><span>&#128247;</span></div>';

  const safeName = esc(name).replace(/'/g, "\\'");
  const addMsg = `Add product ${esc(pd.code)} to my cart`;

  let categoriesHTML = '';
  if (pd.categories && pd.categories.length > 0) {
    categoriesHTML = '<div class="pd-card-categories">' +
      pd.categories.map(c => `<span class="pd-card-cat">${esc(c)}</span>`).join('') +
      '</div>';
  }

  return `<div class="pd-card">
    ${imgHTML}
    <div class="pd-card-body">
      <div class="pd-card-name">${esc(name)}</div>
      <div class="pd-card-code">${esc(pd.code)}</div>
      <div class="pd-card-price-row">
        ${pd.price ? `<div class="pd-card-price">${esc(pd.price)}</div>` : ''}
        ${stockLabel ? `<span class="product-card-stock ${stockClass}">${stockLabel}</span>` : ''}
      </div>
      ${ratingHTML}
      ${categoriesHTML}
      ${pd.description ? `<div class="pd-card-desc">${formatText(stripHtml(pd.description))}</div>` : ''}
      <div class="pd-card-actions">
        <button class="product-card-btn add-cart" onclick="sendQuick('${addMsg}')">
          <span class="btn-icon">&#128722;</span> Add to Cart
        </button>
        <button class="product-card-btn details" onclick="sendQuick('Search for ${safeName}')">Similar</button>
      </div>
    </div>
  </div>`;
}


// ── Cart Card (inline in chat) ──────────────────────────────────────────────

function buildCartCardHTML(cart) {
  if (!cart || !cart.entries || cart.entries.length === 0) return '';

  let html = '<div class="cart-card">';
  html += '<div class="cart-card-header"><span class="cart-card-icon">&#128722;</span>';
  html += `<span class="cart-card-title">Shopping Cart</span>`;
  html += `<span class="cart-card-count">${cart.item_count || cart.entries.length} items</span>`;
  html += '</div>';

  html += '<div class="cart-card-entries">';
  cart.entries.forEach(e => {
    const imgHTML = e.image_url
      ? `<img src="${esc(e.image_url)}" alt="${esc(e.product_name)}" loading="lazy" onerror="this.style.display='none'"/>`
      : '<span class="entry-no-img">&#128247;</span>';

    const safeCode = esc(e.product_code || '');
    const qty = e.quantity || 1;
    const decMsg = qty > 1
      ? `Update quantity of entry ${e.entry_number} to ${qty - 1} in my cart`
      : `Remove entry ${e.entry_number} from my cart`;
    const incMsg = `Update quantity of entry ${e.entry_number} to ${qty + 1} in my cart`;

    html += `<div class="cart-card-entry">
      <div class="cart-entry-img">${imgHTML}</div>
      <div class="cart-entry-info">
        <div class="cart-entry-name">${esc(e.product_name)}</div>
        ${e.base_price ? `<div class="cart-entry-unit-price">${esc(e.base_price)} each</div>` : ''}
      </div>
      <div class="cart-entry-controls">
        <div class="qty-control">
          <button class="qty-btn" onclick="sendQuick('${decMsg}')" title="Decrease">&#8722;</button>
          <span class="qty-value">${qty}</span>
          <button class="qty-btn" onclick="sendQuick('${incMsg}')" title="Increase">&#43;</button>
        </div>
        <div class="cart-entry-total">${esc(e.total || '')}</div>
      </div>
    </div>`;
  });
  html += '</div>';

  // Totals
  html += '<div class="cart-card-totals">';
  if (cart.sub_total) {
    html += `<div class="cart-total-row"><span>Subtotal</span><span>${esc(cart.sub_total)}</span></div>`;
  }
  if (cart.delivery_cost) {
    html += `<div class="cart-total-row"><span>Shipping</span><span>${esc(cart.delivery_cost)}</span></div>`;
  }
  if (cart.total_tax) {
    html += `<div class="cart-total-row"><span>Tax</span><span>${esc(cart.total_tax)}</span></div>`;
  }
  if (cart.total) {
    html += `<div class="cart-total-row total"><span>Total</span><span>${esc(cart.total)}</span></div>`;
  }
  html += '</div>';

  // 2-click checkout section (when user is logged in with saved data)
  const addrs = App.savedAddresses || [];
  const pays = App.sapPaymentDetails || [];
  const hasCheckoutData = App.currentUser && (addrs.length > 0 || pays.length > 0);

  if (hasCheckoutData) {
    html += '<div class="cart-checkout-section">';
    html += '<div class="cart-checkout-label">Quick Checkout</div>';

    if (addrs.length > 0) {
      html += '<div class="cart-checkout-row">';
      html += '<div class="cart-checkout-row-icon">&#127968;</div>';
      html += '<div class="cart-checkout-row-body">';
      html += '<div class="cart-checkout-row-title">Delivery Address</div>';
      html += `<select class="cart-checkout-select" id="checkoutAddr">`;
      addrs.forEach((a, i) => {
        const label = a.formattedAddress || `${a.line1}, ${a.town} ${a.postalCode}`;
        const def = a.defaultAddress ? ' (default)' : '';
        html += `<option value="${i}"${a.defaultAddress ? ' selected' : ''}>${esc(label)}${def}</option>`;
      });
      html += '</select></div></div>';
    }

    if (pays.length > 0) {
      html += '<div class="cart-checkout-row">';
      html += '<div class="cart-checkout-row-icon">&#128179;</div>';
      html += '<div class="cart-checkout-row-body">';
      html += '<div class="cart-checkout-row-title">Payment Method</div>';
      html += `<select class="cart-checkout-select" id="checkoutPay">`;
      pays.forEach((p, i) => {
        const label = `${p.cardType} ****${(p.cardNumber || '').slice(-4)} (${p.expiryMonth}/${p.expiryYear})`;
        const def = p.defaultPayment ? ' (default)' : '';
        html += `<option value="${i}"${p.defaultPayment ? ' selected' : ''}>${esc(label)}${def}</option>`;
      });
      html += '</select></div></div>';
    }

    html += '</div>';
  }

  // Actions
  html += '<div class="cart-card-actions">';
  if (hasCheckoutData) {
    html += `<button class="cart-action-btn primary" onclick="quickCheckout(this)">
      <span>&#9889;</span> Place Order${cart.total ? ' \u00b7 ' + esc(cart.total) : ''}
    </button>`;
  } else {
    html += `<button class="cart-action-btn primary" onclick="sendQuick('I want to checkout')">
      <span>&#128274;</span> Proceed to Checkout
    </button>`;
  }
  html += `<button class="cart-action-btn secondary" onclick="sendQuick('Show me what products you have')">
    Continue Shopping
  </button>`;
  html += '</div>';

  html += '</div>';
  return html;
}


// ── Order Success ───────────────────────────────────────────────────────────

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


// ── Checkout Confirmation Card ──────────────────────────────────────────────

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
    const nameMatch = line.match(/(?:\d+[.)]|[-•])\s+\*?\*?(.+?)\*?\*?\s*[-–—]/);
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

  html += '</div>';

  html += '<div class="checkout-card-actions">';
  html += `<button class="btn-pay" onclick="approveOrder(true, this)">
    <span class="lock-icon">&#128274;</span> Confirm &amp; Pay${summary.total ? ' ' + esc(summary.total) : ''}
  </button>`;
  html += '<button class="btn-cancel-order" onclick="approveOrder(false, this)">Cancel</button>';
  html += '</div></div>';
  return html;
}


// ── Suggestion Buttons ──────────────────────────────────────────────────────

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


// ── Shared Helpers ──────────────────────────────────────────────────────────

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
  const full = Math.floor(rating);
  const half = rating - full >= 0.5 ? 1 : 0;
  const empty = 5 - full - half;
  let stars = '';
  for (let i = 0; i < full; i++) stars += '&#9733;';
  for (let i = 0; i < half; i++) stars += '&#9733;';
  for (let i = 0; i < empty; i++) stars += '&#9734;';
  return `<div class="product-card-rating"><span class="stars">${stars}</span><span class="val">${rating.toFixed(1)}</span></div>`;
}

function getBrandIcon(brand) {
  const b = (brand || '').toLowerCase();
  if (b.includes('visa')) return '&#128179;';
  if (b.includes('master')) return '&#128179;';
  if (b.includes('amex')) return '&#128179;';
  return '&#128179;';
}
