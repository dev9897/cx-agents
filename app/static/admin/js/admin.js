/**
 * Admin Panel — Single-page app for agent management.
 */

// ── Auth ─────────────────────────────────────────────────────────────────────

const adminAuth = {
    key: localStorage.getItem('admin_api_key') || '',

    async login() {
        const input = document.getElementById('adminKeyInput');
        const key = input.value.trim();
        if (!key) return;

        try {
            const res = await fetch('/admin/overview', {
                headers: { 'X-Admin-Key': key }
            });
            if (res.ok) {
                this.key = key;
                localStorage.setItem('admin_api_key', key);
                document.getElementById('authOverlay').style.display = 'none';
                document.getElementById('adminLayout').style.display = 'grid';
                pages.dashboard.load();
            } else {
                document.getElementById('authError').textContent = 'Invalid API key';
                document.getElementById('authError').style.display = 'block';
            }
        } catch (e) {
            document.getElementById('authError').textContent = 'Connection failed';
            document.getElementById('authError').style.display = 'block';
        }
    },

    init() {
        if (this.key) {
            // Auto-login with stored key
            fetch('/admin/overview', {
                headers: { 'X-Admin-Key': this.key }
            }).then(res => {
                if (res.ok) {
                    document.getElementById('authOverlay').style.display = 'none';
                    document.getElementById('adminLayout').style.display = 'grid';
                    pages.dashboard.load();
                } else {
                    localStorage.removeItem('admin_api_key');
                    this.key = '';
                }
            }).catch(() => {});
        }

        // Enter key to login
        document.getElementById('adminKeyInput').addEventListener('keydown', e => {
            if (e.key === 'Enter') this.login();
        });
    }
};

// ── API helper ───────────────────────────────────────────────────────────────

async function api(path, opts = {}) {
    const res = await fetch(`/admin${path}`, {
        ...opts,
        headers: {
            'X-Admin-Key': adminAuth.key,
            'Content-Type': 'application/json',
            ...(opts.headers || {}),
        }
    });
    if (res.status === 401) {
        localStorage.removeItem('admin_api_key');
        location.reload();
        return null;
    }
    return res.json();
}

// ── Navigation ───────────────────────────────────────────────────────────────

function navigateTo(pageId) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));

    const page = document.getElementById(`page-${pageId}`);
    if (page) page.classList.add('active');

    const nav = document.querySelector(`.nav-item[data-page="${pageId}"]`);
    if (nav) nav.classList.add('active');

    // Load page data
    if (pages[pageId] && pages[pageId].load) {
        pages[pageId].load();
    }
}

document.querySelectorAll('.nav-item[data-page]').forEach(item => {
    item.addEventListener('click', () => navigateTo(item.dataset.page));
});

// ── Helpers ──────────────────────────────────────────────────────────────────

function formatNumber(n) {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
    return String(n);
}

function formatTime(isoStr) {
    if (!isoStr) return '-';
    const d = new Date(isoStr);
    return d.toLocaleTimeString();
}

function truncate(s, max = 60) {
    if (!s || s.length <= max) return s || '-';
    return s.substring(0, max) + '...';
}

function estimateCost(input, output) {
    // Claude Haiku pricing: $0.25/M input, $1.25/M output
    return ((input * 0.25 + output * 1.25) / 1_000_000).toFixed(4);
}

// ── Pages ────────────────────────────────────────────────────────────────────

const pages = {

    // ── Dashboard ────────────────────────────────────────────────────────
    dashboard: {
        async load() {
            const [overview, audit] = await Promise.all([
                api('/overview'),
                api('/audit?limit=10'),
            ]);
            if (!overview) return;

            document.getElementById('dashboardStats').innerHTML = `
                <div class="stat-card accent">
                    <div class="label">Active Sessions</div>
                    <div class="value">${overview.sessions.active}</div>
                    <div class="unit">${overview.sessions.total_turns} total turns</div>
                </div>
                <div class="stat-card info">
                    <div class="label">Total Tokens</div>
                    <div class="value">${formatNumber(overview.tokens.total_input + overview.tokens.total_output)}</div>
                    <div class="unit">in: ${formatNumber(overview.tokens.total_input)} / out: ${formatNumber(overview.tokens.total_output)}</div>
                </div>
                <div class="stat-card success">
                    <div class="label">Est. Cost</div>
                    <div class="value">$${overview.tokens.estimated_cost_usd}</div>
                    <div class="unit">USD (Claude Haiku pricing)</div>
                </div>
                <div class="stat-card ${overview.errors.circuit_breaker === 'open' ? 'danger' : 'success'}">
                    <div class="label">Circuit Breaker</div>
                    <div class="value">${overview.errors.circuit_breaker === 'open' ? 'OPEN' : 'OK'}</div>
                    <div class="unit">${overview.errors.total} errors total</div>
                </div>
                <div class="stat-card">
                    <div class="label">Redis</div>
                    <div class="value" style="font-size:20px">${overview.infrastructure.redis}</div>
                </div>
                <div class="stat-card">
                    <div class="label">Uptime</div>
                    <div class="value" style="font-size:20px">${Math.floor(overview.infrastructure.uptime_seconds / 60)}m</div>
                </div>
            `;

            if (audit && audit.entries) {
                document.getElementById('dashboardAudit').innerHTML = audit.entries.map(e => `
                    <tr>
                        <td class="mono">${formatTime(e.ts)}</td>
                        <td><span class="badge ${e.event.includes('ERROR') ? 'danger' : e.event.includes('BLOCK') ? 'warning' : 'info'}">${e.event}</span></td>
                        <td class="mono">${e.user_id || '-'}</td>
                        <td>${truncate(e.details, 80)}</td>
                    </tr>
                `).join('');
            }

            // Update status indicator
            document.getElementById('statusDot').className = 'status-dot online';
            document.getElementById('statusText').textContent = 'Connected';
        }
    },

    // ── Sessions ─────────────────────────────────────────────────────────
    sessions: {
        async load() {
            const data = await api('/sessions');
            if (!data) return;

            document.getElementById('sessionsTable').innerHTML = data.sessions.length === 0
                ? '<tr><td colspan="8" style="text-align:center;color:var(--text-muted)">No active sessions</td></tr>'
                : data.sessions.map(s => `
                    <tr>
                        <td class="mono">${s.session_id.substring(0, 8)}...</td>
                        <td>${s.username || s.user_id}</td>
                        <td>${s.turn_count}</td>
                        <td class="mono">${formatNumber(s.total_input_tokens)}</td>
                        <td class="mono">${formatNumber(s.total_output_tokens)}</td>
                        <td class="mono">${s.cart_id ? s.cart_id.substring(0, 8) + '...' : '-'}</td>
                        <td>${s.last_error ? '<span class="badge danger">Error</span>' : '<span class="badge success">OK</span>'}</td>
                        <td><button class="btn sm" onclick="pages.sessions.inspect('${s.session_id}')">Inspect</button></td>
                    </tr>
                `).join('');
        },

        async inspect(sessionId) {
            const data = await api(`/sessions/${sessionId}`);
            if (!data) return;
            document.getElementById('sessionDetail').textContent = JSON.stringify(data, null, 2);
            document.getElementById('sessionDetailPanel').style.display = 'block';
        }
    },

    // ── Graph ────────────────────────────────────────────────────────────
    graph: {
        async load() {
            const data = await api('/graph');
            if (!data) return;
            this.draw(data);
        },

        draw(data) {
            const canvas = document.getElementById('graphCanvas');
            const ctx = canvas.getContext('2d');
            const W = canvas.width;
            const H = canvas.height;

            ctx.clearRect(0, 0, W, H);

            // Layout nodes in a flow
            const nodeList = data.nodes.filter(n => n.id !== '__start__' && n.id !== '__end__');
            const positions = {};

            // Position __start__ at top, __end__ at bottom
            positions['__start__'] = { x: W / 2, y: 40 };
            positions['__end__'] = { x: W / 2, y: H - 40 };

            // Main flow: agent -> human_approval -> tools -> sync -> loop_breaker
            const flowOrder = ['agent', 'human_approval', 'tools', 'sync', 'loop_breaker'];
            const existing = flowOrder.filter(n => nodeList.some(nl => nl.id === n));

            // Position flow nodes
            const startY = 100;
            const endY = H - 100;
            const stepY = (endY - startY) / Math.max(existing.length - 1, 1);

            existing.forEach((id, i) => {
                // Stagger horizontally for readability
                let xOffset = 0;
                if (id === 'human_approval') xOffset = -140;
                if (id === 'loop_breaker') xOffset = 140;
                positions[id] = {
                    x: W / 2 + xOffset,
                    y: startY + i * stepY,
                };
            });

            // Draw edges
            ctx.strokeStyle = '#3a3d50';
            ctx.lineWidth = 2;
            data.edges.forEach(edge => {
                const from = positions[edge.source];
                const to = positions[edge.target];
                if (!from || !to) return;

                ctx.beginPath();
                ctx.setLineDash(edge.conditional ? [6, 4] : []);

                if (edge.conditional) {
                    ctx.strokeStyle = '#6366f1';
                } else {
                    ctx.strokeStyle = '#3a3d50';
                }

                // Curved lines for non-straight edges
                const dx = to.x - from.x;
                if (Math.abs(dx) > 50) {
                    const cpx = from.x + dx * 0.5;
                    const cpy = from.y + (to.y - from.y) * 0.5;
                    ctx.moveTo(from.x, from.y);
                    ctx.quadraticCurveTo(cpx, cpy, to.x, to.y);
                } else {
                    ctx.moveTo(from.x, from.y);
                    ctx.lineTo(to.x, to.y);
                }
                ctx.stroke();

                // Arrow head
                const angle = Math.atan2(to.y - from.y, to.x - from.x);
                const arrowLen = 8;
                ctx.beginPath();
                ctx.setLineDash([]);
                ctx.fillStyle = ctx.strokeStyle;
                ctx.moveTo(to.x, to.y);
                ctx.lineTo(
                    to.x - arrowLen * Math.cos(angle - 0.4),
                    to.y - arrowLen * Math.sin(angle - 0.4)
                );
                ctx.lineTo(
                    to.x - arrowLen * Math.cos(angle + 0.4),
                    to.y - arrowLen * Math.sin(angle + 0.4)
                );
                ctx.fill();
            });

            // Draw nodes
            Object.entries(positions).forEach(([id, pos]) => {
                const isStart = id === '__start__';
                const isEnd = id === '__end__';

                // Node box
                const label = isStart ? 'START' : isEnd ? 'END' : id;
                ctx.font = '600 13px -apple-system, sans-serif';
                const textWidth = ctx.measureText(label).width;
                const boxW = textWidth + 32;
                const boxH = 36;

                ctx.fillStyle = '#1a1d27';
                ctx.strokeStyle = isStart ? '#22c55e' : isEnd ? '#ef4444' :
                    id === 'agent' ? '#6366f1' : '#2a2d3e';
                ctx.lineWidth = 2;
                ctx.setLineDash([]);

                // Rounded rect
                const r = 8;
                const x = pos.x - boxW / 2;
                const y = pos.y - boxH / 2;
                ctx.beginPath();
                ctx.moveTo(x + r, y);
                ctx.lineTo(x + boxW - r, y);
                ctx.arcTo(x + boxW, y, x + boxW, y + r, r);
                ctx.lineTo(x + boxW, y + boxH - r);
                ctx.arcTo(x + boxW, y + boxH, x + boxW - r, y + boxH, r);
                ctx.lineTo(x + r, y + boxH);
                ctx.arcTo(x, y + boxH, x, y + boxH - r, r);
                ctx.lineTo(x, y + r);
                ctx.arcTo(x, y, x + r, y, r);
                ctx.closePath();
                ctx.fill();
                ctx.stroke();

                // Label
                ctx.fillStyle = isStart ? '#22c55e' : isEnd ? '#ef4444' :
                    id === 'agent' ? '#818cf8' : '#e4e6f0';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText(label, pos.x, pos.y);
            });
        }
    },

    // ── Tokens & Cost ────────────────────────────────────────────────────
    tokens: {
        async load() {
            const [metrics, sessions] = await Promise.all([
                api('/metrics'),
                api('/sessions'),
            ]);
            if (!metrics) return;

            document.getElementById('tokenStats').innerHTML = `
                <div class="stat-card accent">
                    <div class="label">Total Input Tokens</div>
                    <div class="value">${formatNumber(metrics.tokens.total_input)}</div>
                </div>
                <div class="stat-card info">
                    <div class="label">Total Output Tokens</div>
                    <div class="value">${formatNumber(metrics.tokens.total_output)}</div>
                </div>
                <div class="stat-card success">
                    <div class="label">Avg Input/Session</div>
                    <div class="value">${formatNumber(metrics.tokens.avg_input_per_session)}</div>
                </div>
                <div class="stat-card warning">
                    <div class="label">Total Est. Cost</div>
                    <div class="value">$${estimateCost(metrics.tokens.total_input, metrics.tokens.total_output)}</div>
                </div>
            `;

            if (sessions) {
                document.getElementById('tokenTable').innerHTML = sessions.sessions.map(s => {
                    const cost = estimateCost(s.total_input_tokens, s.total_output_tokens);
                    return `
                        <tr>
                            <td class="mono">${s.session_id.substring(0, 12)}...</td>
                            <td>${s.username || s.user_id}</td>
                            <td>${s.turn_count}</td>
                            <td class="mono">${formatNumber(s.total_input_tokens)}</td>
                            <td class="mono">${formatNumber(s.total_output_tokens)}</td>
                            <td class="mono">$${cost}</td>
                        </tr>
                    `;
                }).join('');
            }
        }
    },

    // ── Logs ─────────────────────────────────────────────────────────────
    logs: {
        eventSource: null,

        load() {
            this.reconnect();
        },

        reconnect() {
            if (this.eventSource) {
                this.eventSource.close();
            }

            const level = document.getElementById('logLevel').value;
            const viewer = document.getElementById('logViewer');

            this.eventSource = new EventSource(
                `/admin/logs/stream?level=${level}&key=${adminAuth.key}`
            );

            this.eventSource.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    const line = document.createElement('div');
                    line.className = 'log-line ' + this.detectLevel(data.log);
                    line.textContent = data.log;
                    viewer.appendChild(line);

                    // Keep max 500 lines
                    while (viewer.children.length > 500) {
                        viewer.removeChild(viewer.firstChild);
                    }

                    if (document.getElementById('logAutoScroll').checked) {
                        viewer.scrollTop = viewer.scrollHeight;
                    }
                } catch (e) {}
            };

            this.eventSource.onerror = () => {
                // Will auto-reconnect
            };
        },

        clear() {
            document.getElementById('logViewer').innerHTML = '';
        },

        detectLevel(text) {
            if (/\bDEBUG\b/.test(text)) return 'debug';
            if (/\bWARN/.test(text)) return 'warning';
            if (/\bERROR\b/.test(text)) return 'error';
            if (/\bCRIT/.test(text)) return 'critical';
            return 'info';
        }
    },

    // ── Audit ────────────────────────────────────────────────────────────
    audit: {
        async load() {
            const filter = document.getElementById('auditEventFilter').value;
            const params = filter ? `?event=${filter}&limit=200` : '?limit=200';
            const data = await api(`/audit${params}`);
            if (!data) return;

            document.getElementById('auditTable').innerHTML = data.entries.map(e => `
                <tr>
                    <td class="mono">${formatTime(e.ts)}</td>
                    <td><span class="badge ${e.event.includes('ERROR') ? 'danger' : e.event.includes('BLOCK') || e.event.includes('REJECT') ? 'warning' : 'info'}">${e.event}</span></td>
                    <td class="mono">${e.user_id || '-'}</td>
                    <td style="max-width:400px;overflow:hidden;text-overflow:ellipsis">${truncate(e.details, 120)}</td>
                </tr>
            `).join('') || '<tr><td colspan="4" style="text-align:center;color:var(--text-muted)">No events</td></tr>';
        }
    },

    // ── Tools ────────────────────────────────────────────────────────────
    tools: {
        async load() {
            const data = await api('/tools');
            if (!data) return;

            document.getElementById('toolsList').innerHTML = `
                <div style="margin-bottom:16px;font-size:13px;color:var(--text-secondary)">
                    ${data.count} tools registered
                </div>
                ${data.tools.map(t => `
                    <div class="tool-card">
                        <div class="tool-name">${t.name}</div>
                        <div class="tool-desc">${t.description || 'No description'}</div>
                    </div>
                `).join('')}
            `;
        }
    },

    // ── Features ─────────────────────────────────────────────────────────
    features: {
        async load() {
            const data = await api('/features');
            if (!data) return;

            const items = Object.entries(data.config).map(([name, info]) => `
                <div class="tool-card">
                    <div style="display:flex;align-items:center;justify-content:space-between">
                        <div class="tool-name">${name}</div>
                        <span class="badge ${info.enabled ? 'success' : 'danger'}">${info.enabled ? 'Active' : 'Disabled'}</span>
                    </div>
                    <div class="tool-desc">${info.description || ''}</div>
                </div>
            `);

            document.getElementById('featuresList').innerHTML = `
                <div style="margin-bottom:16px;font-size:13px;color:var(--text-secondary)">
                    Active: ${data.active.join(', ') || 'none'}
                </div>
                ${items.join('')}
            `;
        }
    },

    // ── Controls ─────────────────────────────────────────────────────────
    controls: {
        async load() {
            const config = await api('/config');
            if (!config) return;

            document.getElementById('configPanel').innerHTML = `
                <div class="control-row">
                    <label>Log Level</label>
                    <select id="cfgLogLevel">
                        ${['DEBUG','INFO','WARNING','ERROR'].map(l =>
                            `<option value="${l}" ${config.observability?.log_level === l ? 'selected' : ''}>${l}</option>`
                        ).join('')}
                    </select>
                    <button class="btn sm primary" onclick="pages.controls.update('log_level', document.getElementById('cfgLogLevel').value)">Apply</button>
                </div>
                <div class="control-row">
                    <label>Max Tool Loops</label>
                    <input type="number" id="cfgMaxLoops" value="${config.resilience?.max_tool_loops_per_turn || 5}" min="1" max="20" style="width:80px">
                    <button class="btn sm primary" onclick="pages.controls.update('max_tool_loops_per_turn', document.getElementById('cfgMaxLoops').value)">Apply</button>
                </div>
                <div class="control-row">
                    <label>Max Context Msgs</label>
                    <input type="number" id="cfgMaxMsgs" value="${config.resilience?.max_messages_in_context || 50}" min="10" max="200" style="width:80px">
                    <button class="btn sm primary" onclick="pages.controls.update('max_messages_in_context', document.getElementById('cfgMaxMsgs').value)">Apply</button>
                </div>
                <div class="control-row">
                    <label>Temperature</label>
                    <input type="number" id="cfgTemp" value="${config.claude?.temperature || 0}" min="0" max="1" step="0.1" style="width:80px">
                    <button class="btn sm primary" onclick="pages.controls.update('temperature', document.getElementById('cfgTemp').value)">Apply</button>
                </div>
            `;

            document.getElementById('fullConfig').textContent = JSON.stringify(config, null, 2);
        },

        async update(key, value) {
            const body = {};
            body[key] = value;
            const result = await api('/config', {
                method: 'POST',
                body: JSON.stringify(body),
            });
            if (result && result.updated) {
                alert('Updated: ' + result.updated.join(', '));
            }
        },

        async clearCache() {
            const result = await api('/cache/clear', { method: 'POST' });
            if (result) {
                document.getElementById('cacheResult').textContent =
                    `Cleared ${result.cleared} entries`;
            }
        }
    }
};

// ── Init ─────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    adminAuth.init();

    // Auto-refresh dashboard every 30s
    setInterval(() => {
        const dashboard = document.getElementById('page-dashboard');
        if (dashboard.classList.contains('active')) {
            pages.dashboard.load();
        }
    }, 30000);
});
