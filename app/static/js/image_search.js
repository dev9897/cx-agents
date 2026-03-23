/**
 * image_search.js — Image upload, camera capture, and visual product search.
 *
 * Depends on: app.js, renderers.js, chat.js
 *
 * Flow:
 *   1. User clicks camera button or selects image file
 *   2. Image preview shown in input bar
 *   3. User clicks "Search" → image sent to /image-search/base64 endpoint
 *   4. Results rendered as product cards
 */

// ── State ───────────────────────────────────────────────────────────────────

let _pendingImage = null; // { file: File, dataUrl: string }

// ── Trigger upload ──────────────────────────────────────────────────────────

function triggerImageUpload() {
  document.getElementById('imageFileInput').click();
}

function handleImageSelect(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;

  if (!file.type.startsWith('image/')) {
    appendError('Please select an image file (JPEG, PNG, WebP).');
    return;
  }

  if (file.size > 10 * 1024 * 1024) {
    appendError('Image too large. Maximum size is 10MB.');
    return;
  }

  // Read and show preview
  const reader = new FileReader();
  reader.onload = function (e) {
    _pendingImage = { file, dataUrl: e.target.result };
    showImagePreview(file.name, file.size, e.target.result);
  };
  reader.readAsDataURL(file);

  // Reset input so same file can be selected again
  event.target.value = '';
}

// ── Preview ─────────────────────────────────────────────────────────────────

function showImagePreview(name, size, dataUrl) {
  const bar = document.getElementById('imagePreviewBar');
  document.getElementById('imagePreviewThumb').src = dataUrl;
  document.getElementById('imagePreviewName').textContent = name;
  document.getElementById('imagePreviewSize').textContent = formatFileSize(size);
  bar.classList.add('active');
}

function cancelImagePreview() {
  _pendingImage = null;
  document.getElementById('imagePreviewBar').classList.remove('active');
}

function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// ── Submit ──────────────────────────────────────────────────────────────────

async function submitImageSearch() {
  if (!_pendingImage) return;

  const { dataUrl, file } = _pendingImage;
  cancelImagePreview();
  hideWelcome();

  // Show user message with image thumbnail
  const thumbHtml = `<div class="image-search-query">
    <img src="${dataUrl}" alt="Search image"/>
    <span>Searching for products matching this image...</span>
  </div>`;
  const msgs = document.getElementById('messages');
  const userDiv = document.createElement('div');
  userDiv.className = 'msg user';
  userDiv.innerHTML = `<div class="avatar">U</div><div class="bubble">${thumbHtml}</div>`;
  msgs.appendChild(userDiv);

  // Show processing indicator
  const typing = appendTyping();

  try {
    const r = await fetch(`${API}/image-search/base64`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image: dataUrl }),
    });

    typing.remove();

    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: 'Image search failed' }));
      appendError(err.detail || `Image search error: HTTP ${r.status}`);
      return;
    }

    const data = await r.json();

    if (data.success && data.products && data.products.length > 0) {
      // Render as product cards (reuse existing renderer)
      appendMsg('agent', data.message || 'Here are visually similar products:', {
        products: data.products,
      });
    } else {
      appendMsg('agent', data.message || 'No matching products found. Try a different image or use text search.');
    }
  } catch (e) {
    typing.remove();
    appendError('Image search failed. Please try again.');
    console.error('Image search error:', e);
  }
}
