/**
 * Storefront — product listing, search, detail, and cart page.
 */

// ── State ────────────────────────────────────────────────────────────────────

let storeCurrentPage = 0;
let storeCurrentQuery = '';
let storeTotalPages = 0;

// ── View switching ───────────────────────────────────────────────────────────

function switchView(viewId) {
    // Hide all page views
    document.getElementById('view-store').style.display = 'none';
    document.getElementById('view-cart-page').style.display = 'none';

    // Remove active from nav
    document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));

    // If chat requested, open the widget instead
    if (viewId === 'chat') {
        openChatWidget();
        return;
    }

    // Show selected view
    const view = document.getElementById(`view-${viewId}`);
    if (view) view.style.display = 'block';

    // Activate nav link
    const nav = document.querySelector(`.nav-link[data-view="${viewId}"]`);
    if (nav) nav.classList.add('active');

    // Load data for view
    if (viewId === 'store') {
        if (!document.getElementById('productGrid').querySelector('.product-card')) {
            storeSearch();
        }
    } else if (viewId === 'cart-page') {
        loadCartPage();
    }
}

// ── Chat Widget Toggle ──────────────────────────────────────────────────────

function toggleChatWidget() {
    const widget = document.getElementById('chatWidget');
    const bubble = document.getElementById('chatToggleBubble');
    const icon = document.getElementById('chatBubbleIcon');

    if (widget.classList.contains('open')) {
        widget.classList.remove('open');
        bubble.classList.remove('active');
        icon.innerHTML = '&#128172;';
    } else {
        openChatWidget();
    }
}

function openChatWidget() {
    const widget = document.getElementById('chatWidget');
    const bubble = document.getElementById('chatToggleBubble');
    const icon = document.getElementById('chatBubbleIcon');

    widget.classList.add('open');
    bubble.classList.add('active');
    icon.innerHTML = '&#10005;';

    // Focus input after animation
    setTimeout(() => {
        const input = document.getElementById('userInput');
        if (input) input.focus();
    }, 350);
}

function toggleChatWidgetSize() {
    const widget = document.getElementById('chatWidget');
    widget.classList.toggle('expanded');
}

// ── Widget Tab Switching ────────────────────────────────────────────────────

function switchCwTab(tabId) {
    document.querySelectorAll('.cw-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.cw-panel').forEach(p => p.classList.remove('active'));

    const tab = document.querySelector(`.cw-tab[data-cwtab="${tabId}"]`);
    const panel = document.getElementById(`cwPanel-${tabId}`);
    if (tab) tab.classList.add('active');
    if (panel) panel.classList.add('active');

    if (tabId === 'chat') {
        setTimeout(() => {
            const input = document.getElementById('userInput');
            if (input) input.focus();
        }, 100);
    }
}

// ── Product Search ───────────────────────────────────────────────────────────

async function storeSearch(page = 0) {
    const input = document.getElementById('storeSearchInput');
    const query = input ? input.value.trim() : '';
    storeCurrentQuery = query;
    storeCurrentPage = page;

    const grid = document.getElementById('productGrid');
    grid.innerHTML = '<div class="store-loading">Searching...</div>';

    try {
        const params = new URLSearchParams({
            q: query,
            page: page,
            page_size: 20,
        });
        const res = await fetch(`/store/products?${params}`);
        const data = await res.json();

        storeTotalPages = data.pagination?.totalPages || 0;

        // Update results info
        const info = document.getElementById('storeResultsInfo');
        const total = data.pagination?.totalResults || 0;
        info.textContent = total > 0
            ? `${total} products found${query ? ` for "${query}"` : ''}`
            : 'No products found';

        // Render products
        if (data.products && data.products.length > 0) {
            grid.innerHTML = data.products.map(p => `
                <div class="product-card" onclick="openProductDetail('${p.code}')">
                    <div class="product-card-image">
                        ${p.image
                            ? `<img src="${p.image}" alt="${escapeHtml(p.name)}" loading="lazy" onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%22200%22 height=%22200%22><rect fill=%22%23f0f0f0%22 width=%22200%22 height=%22200%22/><text x=%2250%25%22 y=%2250%25%22 text-anchor=%22middle%22 dy=%22.3em%22 fill=%22%23999%22 font-size=%2214%22>No Image</text></svg>'">`
                            : '<div class="no-image">No Image</div>'}
                    </div>
                    <div class="product-card-body">
                        <div class="product-card-name">${escapeHtml(p.name)}</div>
                        <div class="product-card-price">${p.price}</div>
                        <div class="product-card-stock ${p.stock === 'inStock' ? 'in-stock' : 'out-stock'}">
                            ${p.stock === 'inStock' ? 'In Stock' : p.stock === 'lowStock' ? 'Low Stock' : 'Out of Stock'}
                        </div>
                        ${p.averageRating > 0 ? `
                            <div class="product-card-rating">
                                ${'&#9733;'.repeat(Math.round(p.averageRating))}${'&#9734;'.repeat(5 - Math.round(p.averageRating))}
                                <span>(${p.numberOfReviews})</span>
                            </div>
                        ` : ''}
                    </div>
                    <button class="product-card-add" onclick="event.stopPropagation();addToCartFromStore('${p.code}','${escapeHtml(p.name)}')">
                        Add to Cart
                    </button>
                </div>
            `).join('');
        } else {
            grid.innerHTML = '<div class="store-empty">No products found. Try a different search.</div>';
        }

        // Render pagination
        renderPagination();
    } catch (e) {
        grid.innerHTML = '<div class="store-empty">Failed to load products. Check your connection.</div>';
    }
}

function storeSearchCategory(category) {
    const input = document.getElementById('storeSearchInput');
    input.value = category;

    // Update chip active state
    document.querySelectorAll('.store-chip').forEach(c => c.classList.remove('active'));
    event.target.classList.add('active');

    storeSearch();
}

function renderPagination() {
    const container = document.getElementById('storePagination');
    if (storeTotalPages <= 1) {
        container.innerHTML = '';
        return;
    }

    let html = '';
    if (storeCurrentPage > 0) {
        html += `<button class="page-btn" onclick="storeSearch(${storeCurrentPage - 1})">&#8592; Prev</button>`;
    }

    const start = Math.max(0, storeCurrentPage - 2);
    const end = Math.min(storeTotalPages, storeCurrentPage + 3);
    for (let i = start; i < end; i++) {
        html += `<button class="page-btn ${i === storeCurrentPage ? 'active' : ''}" onclick="storeSearch(${i})">${i + 1}</button>`;
    }

    if (storeCurrentPage < storeTotalPages - 1) {
        html += `<button class="page-btn" onclick="storeSearch(${storeCurrentPage + 1})">Next &#8594;</button>`;
    }

    container.innerHTML = html;
}

// ── Product Detail ───────────────────────────────────────────────────────────

async function openProductDetail(code) {
    const overlay = document.getElementById('productDetailOverlay');
    const content = document.getElementById('productDetailContent');
    overlay.style.display = 'flex';
    content.innerHTML = '<div class="store-loading">Loading product details...</div>';

    try {
        const res = await fetch(`/store/products/${code}`);
        const p = await res.json();

        // Get primary product image
        const primaryImg = p.images?.find(i => i.imageType === 'PRIMARY' && i.format === 'product')
            || p.images?.find(i => i.imageType === 'PRIMARY')
            || p.images?.[0];

        content.innerHTML = `
            <div class="pd-layout">
                <div class="pd-images">
                    ${primaryImg
                        ? `<img src="${primaryImg.url}" alt="${escapeHtml(p.name)}" class="pd-main-image" onerror="this.style.display='none'">`
                        : '<div class="no-image" style="height:300px;display:flex;align-items:center;justify-content:center">No Image</div>'}
                    ${p.images && p.images.length > 1 ? `
                        <div class="pd-thumbnails">
                            ${p.images.filter(i => i.format === 'thumbnail').slice(0, 6).map(i =>
                                `<img src="${i.url}" class="pd-thumb" onclick="document.querySelector('.pd-main-image').src='${i.url.replace('thumbnail', 'product')}'" onerror="this.style.display='none'">`
                            ).join('')}
                        </div>
                    ` : ''}
                </div>
                <div class="pd-info">
                    <h2>${escapeHtml(p.name)}</h2>
                    <div class="pd-code">SKU: ${p.code}</div>
                    ${p.categories?.length ? `<div class="pd-categories">${p.categories.map(c => `<span class="pd-cat">${escapeHtml(c)}</span>`).join('')}</div>` : ''}
                    <div class="pd-price">${p.price}</div>
                    <div class="pd-stock ${p.stock === 'inStock' ? 'in-stock' : 'out-stock'}">
                        ${p.stock === 'inStock' ? 'In Stock' : p.stock === 'lowStock' ? 'Low Stock' : 'Out of Stock'}
                        ${p.stockLevel > 0 ? ` (${p.stockLevel} available)` : ''}
                    </div>
                    ${p.averageRating > 0 ? `
                        <div class="pd-rating">
                            ${'&#9733;'.repeat(Math.round(p.averageRating))}${'&#9734;'.repeat(5 - Math.round(p.averageRating))}
                            <span>${p.averageRating.toFixed(1)} (${p.numberOfReviews} reviews)</span>
                        </div>
                    ` : ''}
                    <div class="pd-actions">
                        <button class="pd-add-btn" onclick="addToCartFromStore('${p.code}','${escapeHtml(p.name)}')">
                            &#128722; Add to Cart
                        </button>
                        <button class="pd-chat-btn" onclick="askAgentAbout('${escapeHtml(p.name)}')">
                            &#128172; Ask Agent
                        </button>
                    </div>
                    ${p.summary ? `<div class="pd-summary">${p.summary}</div>` : ''}
                    ${p.description ? `<div class="pd-description">${p.description}</div>` : ''}
                </div>
            </div>
        `;
    } catch (e) {
        content.innerHTML = '<div class="store-empty">Failed to load product details.</div>';
    }
}

function closeProductDetail() {
    document.getElementById('productDetailOverlay').style.display = 'none';
}

// ── Add to cart (via agent) ──────────────────────────────────────────────────

function addToCartFromStore(code, name) {
    openChatWidget();
    switchCwTab('chat');
    const input = document.getElementById('userInput');
    input.value = `Add ${name} (${code}) to my cart`;
    sendMessage();
}

function askAgentAbout(name) {
    closeProductDetail();
    openChatWidget();
    switchCwTab('chat');
    const input = document.getElementById('userInput');
    input.value = `Tell me about ${name}`;
    sendMessage();
}

// ── Cart page ────────────────────────────────────────────────────────────────

function loadCartPage() {
    const container = document.getElementById('cartPageContent');
    // Pull cart data from the sidebar cart (rendered by the chat)
    const cartItems = document.getElementById('cartItems');
    const cartTotal = document.getElementById('cartTotalPrice');

    if (!cartItems || cartItems.querySelector('.cart-empty')) {
        container.innerHTML = `
            <div class="cart-page-empty">
                <div style="font-size:48px;margin-bottom:16px">&#128722;</div>
                <h3>Your cart is empty</h3>
                <p>Browse the store and add some products to get started.</p>
                <button class="store-search-btn" onclick="switchView('store')" style="margin-top:16px">Browse Store</button>
            </div>
        `;
        return;
    }

    // Clone cart items for the cart page
    const itemsClone = cartItems.cloneNode(true);
    const total = cartTotal ? cartTotal.textContent : '';

    container.innerHTML = `
        <div class="cart-page-items">${itemsClone.innerHTML}</div>
        ${total ? `<div class="cart-page-total"><strong>Total: ${total}</strong></div>` : ''}
        <div class="cart-page-actions">
            <button class="store-search-btn" onclick="switchView('store')">Continue Shopping</button>
            <button class="pd-add-btn" onclick="openChatWidget();switchCwTab('chat');sendQuick('I want to checkout')">
                &#128179; Checkout with Agent
            </button>
        </div>
    `;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
              .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}
