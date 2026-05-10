/**
 * CDK Vaults — 管理后台逻辑
 */
(() => {
    const $ = (s) => document.querySelector(s);
    const $$ = (s) => document.querySelectorAll(s);
    const API = '/api';
    let TOKEN = localStorage.getItem('cdk_token') || '';
    let toastTimer = null;

    function ensureUiLayer() {
        let toastRoot = $('#toast-root');
        if (!toastRoot) {
            toastRoot = document.createElement('div');
            toastRoot.id = 'toast-root';
            toastRoot.className = 'toast-root';
            document.body.appendChild(toastRoot);
        }
        return toastRoot;
    }

    function toast(message, type = 'error') {
        const root = ensureUiLayer();
        root.innerHTML = `<div class="app-toast toast-${type}">${esc(message)}</div>`;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { root.innerHTML = ''; }, 3200);
    }

    async function copyText(text) {
        if (!text) return false;
        if (navigator.clipboard?.writeText && window.isSecureContext) {
            try {
                await navigator.clipboard.writeText(text);
                return true;
            } catch {
                // Fall through to the textarea fallback below.
            }
        }

        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.setAttribute('readonly', '');
        textarea.style.position = 'fixed';
        textarea.style.left = '-9999px';
        textarea.style.top = '0';
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        textarea.setSelectionRange(0, textarea.value.length);

        try {
            return document.execCommand('copy');
        } catch {
            return false;
        } finally {
            textarea.remove();
        }
    }

    function confirmDialog({ title = '确认操作', message = '', detail = '', confirmText = '确认', cancelText = '取消', danger = true } = {}) {
        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'confirm-overlay';
            overlay.innerHTML = `
                <div class="confirm-card" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
                    <div class="confirm-icon ${danger ? 'danger' : ''}">!</div>
                    <div class="confirm-content">
                        <h3 id="confirm-title">${esc(title)}</h3>
                        <p>${esc(message)}</p>
                        ${detail ? `<div class="confirm-detail">${esc(detail)}</div>` : ''}
                    </div>
                    <div class="confirm-actions">
                        <button class="ghost-btn" data-action="cancel">${esc(cancelText)}</button>
                        <button class="primary-btn ${danger ? 'danger-btn' : ''}" data-action="confirm">${esc(confirmText)}</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);
            const previousOverflow = document.body.style.overflow;
            document.body.style.overflow = 'hidden';

            const cleanup = (result) => {
                overlay.remove();
                document.body.style.overflow = previousOverflow;
                document.removeEventListener('keydown', onKey);
                resolve(result);
            };
            const onKey = (e) => {
                if (e.key === 'Escape') cleanup(false);
                if (e.key === 'Enter') cleanup(true);
            };
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay || e.target.dataset.action === 'cancel') cleanup(false);
                if (e.target.dataset.action === 'confirm') cleanup(true);
            });
            document.addEventListener('keydown', onKey);
            overlay.querySelector('[data-action="cancel"]').focus();
        });
    }

    // ── 通用请求 ──────────────────────────────────
    async function api(path, opts = {}) {
        const headers = { ...opts.headers };
        if (TOKEN) headers['Authorization'] = `Bearer ${TOKEN}`;
        if (!(opts.body instanceof FormData)) headers['Content-Type'] = 'application/json';
        const res = await fetch(API + path, { ...opts, headers });
        if (res.status === 401) { logout(); throw new Error('未授权'); }
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || '请求失败');
        return data;
    }

    // ── 登录 ──────────────────────────────────────
    const loginOverlay = $('#login-overlay');
    const app = $('#app');

    async function tryAutoLogin() {
        if (!TOKEN) return;
        try {
            await api('/admin/verify');
            showApp();
        } catch { TOKEN = ''; localStorage.removeItem('cdk_token'); }
    }

    $('#login-btn').addEventListener('click', doLogin);
    $('#login-password').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });

    async function doLogin() {
        const pw = $('#login-password').value;
        if (!pw) return;
        try {
            const data = await fetch(API + '/admin/login', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: pw }),
            }).then(r => r.json());
            if (data.access_token) {
                TOKEN = data.access_token;
                localStorage.setItem('cdk_token', TOKEN);
                showApp();
            } else {
                showLoginError(data.detail || '登录失败');
            }
        } catch { showLoginError('网络错误'); }
    }

    function showLoginError(msg) {
        const el = $('#login-error');
        el.textContent = msg; el.classList.remove('hidden');
    }

    function showApp() {
        loginOverlay.classList.add('hidden');
        app.classList.remove('hidden');
        loadDashboard();
    }

    function logout() {
        TOKEN = ''; localStorage.removeItem('cdk_token');
        loginOverlay.classList.remove('hidden');
        app.classList.add('hidden');
        $('#login-password').value = '';
        $('#login-error').classList.add('hidden');
    }
    $('#logout-btn').addEventListener('click', logout);

    // ── 导航 ──────────────────────────────────────
    $$('.nav-item').forEach(btn => {
        btn.addEventListener('click', () => {
            $$('.nav-item').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            $$('.page').forEach(p => p.classList.remove('active'));
            $(`#page-${btn.dataset.page}`).classList.add('active');
            const loaders = {
                dashboard: loadDashboard,
                categories: loadCategories,
                assets: loadAssets,
                cdks: loadCDKs,
                'upload-logs': loadUploadLogs,
                logs: loadLogs,
            };
            loaders[btn.dataset.page]?.();
        });
    });

    // ── 数据概览 ──────────────────────────────────
    const noticeEnabled = $('#notice-enabled');
    const noticeContent = $('#notice-content');
    const noticeStatus = $('#notice-status');
    const noticeSaveBtn = $('#notice-save-btn');

    function updateNoticeStatus() {
        const enabled = noticeEnabled.checked;
        const hasContent = noticeContent.value.trim().length > 0;
        noticeStatus.textContent = enabled
            ? (hasContent ? '已开启，兑换页正在展示' : '已开启，但内容为空不会展示')
            : '未开启';
        noticeStatus.classList.toggle('active', enabled && hasContent);
    }

    async function loadNoticeSettings() {
        try {
            const notice = await api('/admin/notice');
            noticeEnabled.checked = notice.enabled === true;
            noticeContent.value = notice.content || '';
            updateNoticeStatus();
        } catch (e) {
            console.error(e);
        }
    }

    noticeEnabled.addEventListener('change', updateNoticeStatus);
    noticeContent.addEventListener('input', updateNoticeStatus);
    noticeSaveBtn.addEventListener('click', async () => {
        try {
            const notice = await api('/admin/notice', {
                method: 'PUT',
                body: JSON.stringify({
                    enabled: noticeEnabled.checked,
                    content: noticeContent.value,
                }),
            });
            noticeEnabled.checked = notice.enabled === true;
            noticeContent.value = notice.content || '';
            updateNoticeStatus();
            toast('兑换页通知已保存', 'success');
        } catch (e) {
            toast(e.message);
        }
    });

    async function loadDashboard() {
        try {
            loadNoticeSettings();
            const s = await api('/admin/stats');
            const gapClass = Number(s.asset_gap || 0) > 0 ? 'rose' : 'emerald';
            $('#stats-grid').innerHTML = `
                <div class="stat-card"><div class="stat-label">未兑换资产</div><div class="stat-value emerald">${s.unredeemed_assets}</div><div class="stat-hint">当前可进入兑换池的资产</div></div>
                <div class="stat-card"><div class="stat-label">已兑换资产</div><div class="stat-value amber">${s.redeemed_assets}</div><div class="stat-hint">有兑换记录或已标记兑换</div></div>
                <div class="stat-card"><div class="stat-label">CDK 剩余额度</div><div class="stat-value blue">${s.cdk_remaining_quota}</div><div class="stat-hint">可用 CDK 未兑换额度合计</div></div>
                <div class="stat-card"><div class="stat-label">资产缺口</div><div class="stat-value ${gapClass}">${s.asset_gap}</div><div class="stat-hint">CDK 剩余额度 - 未兑换资产</div></div>
            `;
            if (s.recent_redemptions.length) {
                $('#recent-logs').innerHTML = `<table><thead><tr><th>CDK</th><th>分类</th><th>资产</th><th>IP</th><th>时间</th></tr></thead><tbody>${
                    s.recent_redemptions.map(r => `<tr><td class="code-text">${r.cdk_code}</td><td>${esc(r.category_name)||'<span style="color:var(--text-3)">未分类</span>'}</td><td>${esc(r.asset_name)}</td><td>${r.ip_address||'-'}</td><td>${fmtTime(r.redeemed_at)}</td></tr>`).join('')
                }</tbody></table>`;
            } else {
                $('#recent-logs').innerHTML = '<div class="empty-state">暂无兑换记录</div>';
            }
        } catch (e) { console.error(e); }
    }

    const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];

    function normalizePagedData(data) {
        const items = Array.isArray(data) ? data : (data.items || []);
        return {
            items,
            total: data.total ?? items.length,
            page: data.page || 1,
            page_size: data.page_size || items.length || PAGE_SIZE_OPTIONS[1],
            pages: data.pages || 1,
        };
    }

    function renderTablePagination({ id, tableId, data, pageHandler, pageSizeHandler, pageSize }) {
        let el = $(`#${id}`);
        if (!el) {
            el = document.createElement('div');
            el.id = id;
            el.className = 'pagination-bar';
            $(`#${tableId}`).parentElement.appendChild(el);
        }

        let btns = '';
        btns += `<button class="page-btn${data.page <= 1 ? ' disabled' : ''}" onclick="window.${pageHandler}(${data.page - 1})" ${data.page <= 1 ? 'disabled' : ''}>‹</button>`;
        for (const p of getPageRange(data.page, data.pages)) {
            if (p === '...') btns += '<span class="page-dots">…</span>';
            else btns += `<button class="page-btn${p === data.page ? ' active' : ''}" onclick="window.${pageHandler}(${p})">${p}</button>`;
        }
        btns += `<button class="page-btn${data.page >= data.pages ? ' disabled' : ''}" onclick="window.${pageHandler}(${data.page + 1})" ${data.page >= data.pages ? 'disabled' : ''}>›</button>`;

        const sizeOptions = PAGE_SIZE_OPTIONS.map(size =>
            `<option value="${size}" ${size === pageSize ? 'selected' : ''}>${size} / 页</option>`
        ).join('');
        el.innerHTML = `
            <span class="pagination-info">共 ${data.total} 项，第 ${data.page}/${data.pages} 页</span>
            <div class="pagination-controls">
                <label class="page-size-control">每页 <select class="page-size-select" onchange="window.${pageSizeHandler}(this.value)">${sizeOptions}</select></label>
                <div class="page-btns">${btns}</div>
            </div>
        `;
    }

    // ── 分类管理 ──────────────────────────────────
    let editingCatId = null;
    let catPage = 1;
    let catPageSize = 20;
    let selectedCatIds = new Set();
    let catDeleteReasons = new Map();

    async function loadCategories(page) {
        if (page) catPage = page;
        selectedCatIds.clear();
        catDeleteReasons.clear();
        updateCategoryBatchBar();
        try {
            const data = normalizePagedData(await api(`/categories?paged=1&page=${catPage}&page_size=${catPageSize}`));
            const list = data.items || [];
            list.forEach(c => {
                if (c.name === 'Codex') catDeleteReasons.set(c.id, '内置 Codex 分类不可删除');
            });
            if (!list.length) {
                $('#cats-table').innerHTML = '<div class="empty-state">暂无分类，点击右上角添加</div>';
                renderTablePagination({ id: 'cats-pagination', tableId: 'cats-table', data, pageHandler: '_catPage', pageSizeHandler: '_catPageSize', pageSize: catPageSize });
                return;
            }
            $('#cats-table').innerHTML = `<table><thead><tr><th style="width:36px"><input type="checkbox" id="cat-select-all"></th><th>ID</th><th>颜色</th><th>名称</th><th>描述</th><th>资产数</th><th>排序</th><th>操作</th></tr></thead><tbody>${
                list.map(c => {
                    const canDelete = c.name !== 'Codex';
                    return `<tr>
                    <td><input type="checkbox" class="cat-chk" value="${c.id}" ${canDelete ? '' : 'disabled'}></td>
                    <td>${c.id}</td>
                    <td><span style="display:inline-block;width:16px;height:16px;border-radius:4px;background:${esc(c.color)};vertical-align:middle"></span></td>
                    <td><strong>${esc(c.name)}</strong></td>
                    <td>${esc(c.description)||'-'}</td>
                    <td>${c.asset_count}</td>
                    <td>${c.sort_order}</td>
                    <td style="display:flex;gap:6px">
                        <button class="icon-btn" onclick="window._editCat(${c.id})">编辑</button>
                        <button class="icon-btn danger${canDelete ? '' : ' disabled'}" onclick="window.${canDelete ? '_deleteCat' : '_explainCatDelete'}(${c.id})">删除</button>
                    </td>
                </tr>`;
                }).join('')
            }</tbody></table>`;
            renderTablePagination({ id: 'cats-pagination', tableId: 'cats-table', data, pageHandler: '_catPage', pageSizeHandler: '_catPageSize', pageSize: catPageSize });
            bindCategoryCheckboxes();
        } catch (e) { console.error(e); }
    }

    function bindCategoryCheckboxes() {
        const selectAll = $('#cat-select-all');
        const chks = $$('.cat-chk');
        selectAll?.addEventListener('change', () => {
            chks.forEach(c => { if (!c.disabled) c.checked = selectAll.checked; });
            syncSelectedCategories();
        });
        chks.forEach(c => c.addEventListener('change', () => {
            syncSelectedCategories();
            const enabled = [...chks].filter(c => !c.disabled);
            selectAll.checked = enabled.length && enabled.every(c => c.checked);
        }));
    }

    function syncSelectedCategories() {
        selectedCatIds.clear();
        $$('.cat-chk:checked:not(:disabled)').forEach(c => selectedCatIds.add(parseInt(c.value)));
        updateCategoryBatchBar();
    }

    function updateCategoryBatchBar() {
        let bar = $('#cat-batch-action-bar');
        if (!bar) {
            bar = document.createElement('div');
            bar.id = 'cat-batch-action-bar';
            bar.className = 'batch-bar hidden';
            bar.innerHTML = '<span id="cat-batch-count"></span><button id="cat-batch-delete-btn" class="icon-btn danger">批量删除</button>';
            $('#cats-table').parentElement.insertBefore(bar, $('#cats-table'));
            $('#cat-batch-delete-btn').addEventListener('click', doCategoryBatchDelete);
        }
        if (selectedCatIds.size > 0) {
            bar.classList.remove('hidden');
            $('#cat-batch-count').textContent = `已选 ${selectedCatIds.size} 项`;
        } else {
            bar.classList.add('hidden');
        }
    }

    async function doCategoryBatchDelete() {
        const ids = [...selectedCatIds];
        if (!ids.length) return;
        const ok = await confirmDialog({
            title: '批量删除分类',
            message: `确定删除选中的 ${ids.length} 个分类？`,
            detail: '关联资产会变为“未分类”。',
            confirmText: '批量删除',
        });
        if (!ok) return;
        try {
            const res = await api('/categories/delete-batch', { method: 'POST', body: JSON.stringify({ ids }) });
            if (res.blocked?.length) toast(`已删除 ${res.deleted} 个，${res.blocked.length} 个不可删除`, 'warning');
            else toast(`已删除 ${res.deleted} 个分类`, 'success');
            cachedCategories = [];
            loadCategories();
        } catch (e) { toast(e.message); }
    }

    window._catPage = (p) => loadCategories(p);
    window._catPageSize = (size) => { catPageSize = parseInt(size) || 20; catPage = 1; loadCategories(); };

    // 缓存分类列表供编辑使用
    let cachedCategories = [];
    async function fetchCategories() {
        try { cachedCategories = await api('/categories'); } catch { cachedCategories = []; }
        return cachedCategories;
    }

    window._editCat = async (id) => {
        const cats = cachedCategories.length ? cachedCategories : await fetchCategories();
        const cat = cats.find(c => c.id === id);
        if (!cat) return;
        editingCatId = id;
        $('#cat-form-title').textContent = '编辑分类';
        $('#cat-name').value = cat.name;
        $('#cat-desc').value = cat.description;
        $('#cat-color').value = cat.color;
        $('#cat-sort').value = cat.sort_order;
        $('#cat-form').classList.remove('hidden');
    };

    window._deleteCat = async (id) => {
        const ok = await confirmDialog({
            title: '删除分类',
            message: '确定删除该分类？',
            detail: '关联资产会变为“未分类”。',
            confirmText: '删除',
        });
        if (!ok) return;
        try { await api(`/categories/${id}`, { method: 'DELETE' }); cachedCategories = []; loadCategories(); } catch (e) { toast(e.message); }
    };

    window._explainCatDelete = (id) => {
        toast(catDeleteReasons.get(id) || '该分类不能删除', 'warning');
    };

    $('#add-cat-btn').addEventListener('click', () => {
        editingCatId = null;
        $('#cat-form-title').textContent = '新增分类';
        $('#cat-name').value = ''; $('#cat-desc').value = '';
        $('#cat-color').value = '#8b5cf6'; $('#cat-sort').value = '0';
        $('#cat-form').classList.remove('hidden');
    });
    $('#cat-cancel-btn').addEventListener('click', () => $('#cat-form').classList.add('hidden'));

    $('#cat-save-btn').addEventListener('click', async () => {
        const name = $('#cat-name').value.trim();
        if (!name) return toast('请输入分类名称', 'warning');
        const body = {
            name,
            description: $('#cat-desc').value,
            color: $('#cat-color').value,
            sort_order: parseInt($('#cat-sort').value) || 0,
        };
        try {
            if (editingCatId) {
                await api(`/categories/${editingCatId}`, { method: 'PUT', body: JSON.stringify(body) });
            } else {
                await api('/categories', { method: 'POST', body: JSON.stringify(body) });
            }
            $('#cat-form').classList.add('hidden');
            editingCatId = null;
            cachedCategories = [];
            loadCategories();
        } catch (e) { toast(e.message); }
    });

    // ── 填充分类下拉框 ────────────────────────────
    async function populateCategorySelects() {
        const cats = await fetchCategories();
        const opts = cats.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
        $('#asset-category-id').innerHTML = `<option value="">-- 未分类 --</option>${opts}`;
    }

    // ── 资产管理 ──────────────────────────────────
    let editingAssetId = null;
    let isCodexMode = false;
    let codexFiles = []; // 暂存多文件
    let assetPage = 1;
    let assetPageSize = 20;
    let selectedAssetIds = new Set();
    let assetDeleteReasons = new Map();
    let assetMeta = new Map();
    const CODEX_EXPORT_FORMATS = [
        { value: 'text', name: '文本格式', desc: '邮箱----GPT密码----邮箱密码' },
        { value: 'cpa', name: 'CPA 格式', desc: '单个 JSON，多个 ZIP' },
        { value: 'sub2api_single', name: 'Sub2API 合并', desc: '合并为单个 JSON 文件' },
        { value: 'auth_json', name: 'auth.json 格式', desc: '单个 auth.json，多个 ZIP' },
    ];

    function renderAssetUsageStatus(asset) {
        const redeemed = Number(asset.redeemed_count || 0);
        if (redeemed > 0) return `<span class="badge badge-used">已兑换 ${redeemed}</span>`;
        return '<span class="badge badge-active">未兑换</span>';
    }

    function renderAssetStatusAction(asset) {
        const redeemed = Number(asset.redeemed_count || 0) > 0;
        return `<button class="icon-btn" onclick="window._toggleAssetRedeemStatus(${asset.id}, ${redeemed ? 'false' : 'true'})">${redeemed ? '设未兑换' : '设已兑换'}</button>`;
    }

    function isCodexFileAsset(asset) {
        return asset.type === 'file' && asset.category_name === 'Codex';
    }

    function renderAssetExportAction(asset) {
        if (!isCodexFileAsset(asset)) return '';
        return `<button class="icon-btn" onclick="window._exportAsset(${asset.id})">导出</button>`;
    }

    async function loadAssets(page) {
        if (page) assetPage = page;
        await populateCategorySelects();
        selectedAssetIds.clear();
        assetDeleteReasons.clear();
        assetMeta.clear();
        updateBatchBar();
        try {
            const data = normalizePagedData(await api(`/assets?page=${assetPage}&page_size=${assetPageSize}`));
            const list = data.items || [];
            list.forEach(a => {
                assetMeta.set(a.id, {
                    name: a.name,
                    canDelete: a.can_delete !== false,
                    canExport: isCodexFileAsset(a),
                });
                if (a.can_delete === false) {
                    assetDeleteReasons.set(a.id, a.delete_block_reason || '该资产已有 CDK 或兑换记录，不能删除');
                }
            });
            if (!list.length && data.total === 0) {
                $('#assets-table').innerHTML = '<div class="empty-state">暂无资产，点击右上角添加</div>';
                renderTablePagination({ id: 'assets-pagination', tableId: 'assets-table', data, pageHandler: '_assetPage', pageSizeHandler: '_assetPageSize', pageSize: assetPageSize });
                return;
            }
            $('#assets-table').innerHTML = `<table><thead><tr>
                <th style="width:36px"><input type="checkbox" id="asset-select-all"></th>
                <th>ID</th><th>名称</th><th>类型</th><th>分类</th><th>状态</th><th>创建时间</th><th>操作</th>
            </tr></thead><tbody>${
                list.map(a => {
                    const canDelete = a.can_delete !== false;
                    return `<tr>
                    <td><input type="checkbox" class="asset-chk" value="${a.id}"></td>
                    <td>${a.id}</td><td>${esc(a.name)}</td>
                    <td><span class="badge badge-${a.type}">${a.type}</span></td>
                    <td>${esc(a.category_name)||'<span style="color:var(--text-3)">未分类</span>'}</td>
                    <td>${renderAssetUsageStatus(a)}</td>
                    <td>${fmtTime(a.created_at)}</td>
                    <td style="display:flex;gap:6px">
                        <button class="icon-btn" onclick="window._viewAsset(${a.id})">查看</button>
                        ${renderAssetExportAction(a)}
                        ${renderAssetStatusAction(a)}
                        <button class="icon-btn danger${canDelete ? '' : ' disabled'}" onclick="window.${canDelete ? '_deleteAsset' : '_explainAssetDelete'}(${a.id})">删除</button>
                    </td>
                </tr>`;
                }).join('')
            }</tbody></table>`;

            renderTablePagination({ id: 'assets-pagination', tableId: 'assets-table', data, pageHandler: '_assetPage', pageSizeHandler: '_assetPageSize', pageSize: assetPageSize });
            bindAssetCheckboxes();
        } catch (e) { console.error(e); }
    }

    function getPageRange(cur, total) {
        if (total <= 7) return Array.from({length:total},(_,i)=>i+1);
        const r = [];
        r.push(1);
        if (cur > 3) r.push('...');
        for (let i = Math.max(2, cur-1); i <= Math.min(total-1, cur+1); i++) r.push(i);
        if (cur < total-2) r.push('...');
        r.push(total);
        return r;
    }

    window._assetPage = (p) => loadAssets(p);
    window._assetPageSize = (size) => { assetPageSize = parseInt(size) || 20; assetPage = 1; loadAssets(); };

    // ── 勾选逻辑 ─────────────────────────────────
    function bindAssetCheckboxes() {
        const selectAll = $('#asset-select-all');
        const chks = $$('.asset-chk');
        selectAll?.addEventListener('change', () => {
            chks.forEach(c => { c.checked = selectAll.checked; });
            syncSelected();
        });
        chks.forEach(c => c.addEventListener('change', () => {
            syncSelected();
            selectAll.checked = chks.length && [...chks].every(c => c.checked);
        }));
    }

    function syncSelected() {
        selectedAssetIds.clear();
        $$('.asset-chk:checked').forEach(c => selectedAssetIds.add(parseInt(c.value)));
        updateBatchBar();
    }

    function updateBatchBar() {
        let bar = $('#batch-action-bar');
        if (!bar) {
            bar = document.createElement('div');
            bar.id = 'batch-action-bar';
            bar.className = 'batch-bar hidden';
            bar.innerHTML = `
                <span id="batch-count"></span>
                <select id="batch-export-format" class="page-size-select">${codexFormatOptions('text')}</select>
                <button id="batch-export-btn" class="icon-btn">导出</button>
                <button id="batch-delete-btn" class="icon-btn danger">🗑 批量删除</button>
            `;
            const listEl = $('#assets-list');
            listEl.insertBefore(bar, listEl.firstChild);
            $('#batch-export-btn').addEventListener('click', doBatchExport);
            $('#batch-delete-btn').addEventListener('click', doBatchDelete);
        }
        if (selectedAssetIds.size > 0) {
            bar.classList.remove('hidden');
            $('#batch-count').textContent = `已选 ${selectedAssetIds.size} 项`;
            const allExportable = [...selectedAssetIds].every(id => assetMeta.get(id)?.canExport);
            $('#batch-export-btn').disabled = !allExportable;
            $('#batch-export-btn').title = allExportable ? '' : '只能导出 Codex 文件资产';
        } else {
            bar.classList.add('hidden');
        }
    }

    function codexFormatOptions(selected = 'text') {
        return CODEX_EXPORT_FORMATS.map(f =>
            `<option value="${f.value}" ${f.value === selected ? 'selected' : ''}>${f.name}</option>`
        ).join('');
    }

    function validateCodexExportIds(ids) {
        const invalid = ids.filter(id => !assetMeta.get(id)?.canExport);
        if (invalid.length) {
            const names = invalid.slice(0, 3).map(id => assetMeta.get(id)?.name || `#${id}`).join('、');
            toast(`只能导出 Codex 文件资产：${names}${invalid.length > 3 ? ' 等' : ''}`, 'warning');
            return false;
        }
        return true;
    }

    async function doBatchExport() {
        const ids = [...selectedAssetIds];
        if (!ids.length || !validateCodexExportIds(ids)) return;
        const format = $('#batch-export-format')?.value || 'text';
        await exportCodexAssets(ids, format);
    }

    function chooseCodexExportFormat(count = 1) {
        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'confirm-overlay';
            const options = CODEX_EXPORT_FORMATS.map((f, idx) => `
                <label style="display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border:1px solid var(--border);border-radius:10px;background:rgba(255,255,255,0.02);cursor:pointer">
                    <input type="radio" name="asset-export-format" value="${f.value}" ${idx === 0 ? 'checked' : ''} style="margin-top:3px">
                    <span>
                        <strong style="display:block;color:var(--text-1);font-size:0.9rem">${f.name}</strong>
                        <small style="display:block;color:var(--text-3);margin-top:3px">${f.desc}</small>
                    </span>
                </label>
            `).join('');
            overlay.innerHTML = `
                <div class="confirm-card" role="dialog" aria-modal="true" aria-labelledby="export-title">
                    <div class="confirm-icon">↧</div>
                    <div class="confirm-content">
                        <h3 id="export-title">导出 Codex 资产</h3>
                        <p>请选择 ${count} 个资产的导出格式</p>
                        <div style="display:grid;gap:8px;margin-top:12px">${options}</div>
                    </div>
                    <div class="confirm-actions">
                        <button class="ghost-btn" data-action="cancel">取消</button>
                        <button class="primary-btn" data-action="confirm">导出</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);
            const previousOverflow = document.body.style.overflow;
            document.body.style.overflow = 'hidden';

            const cleanup = (value) => {
                overlay.remove();
                document.body.style.overflow = previousOverflow;
                document.removeEventListener('keydown', onKey);
                resolve(value);
            };
            const onKey = (e) => {
                if (e.key === 'Escape') cleanup('');
                if (e.key === 'Enter') cleanup(overlay.querySelector('input[name="asset-export-format"]:checked')?.value || 'text');
            };
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay || e.target.dataset.action === 'cancel') cleanup('');
                if (e.target.dataset.action === 'confirm') {
                    cleanup(overlay.querySelector('input[name="asset-export-format"]:checked')?.value || 'text');
                }
            });
            document.addEventListener('keydown', onKey);
            overlay.querySelector('[data-action="cancel"]').focus();
        });
    }

    function filenameFromDisposition(disposition, fallback) {
        const utf8 = disposition.match(/filename\*=UTF-8''([^;]+)/i);
        if (utf8) return decodeURIComponent(utf8[1].replace(/"/g, ''));
        const ascii = disposition.match(/filename="?([^";]+)"?/i);
        return ascii ? ascii[1] : fallback;
    }

    async function exportCodexAssets(ids, format) {
        try {
            const headers = { 'Content-Type': 'application/json' };
            if (TOKEN) headers['Authorization'] = `Bearer ${TOKEN}`;
            const res = await fetch(API + '/assets/export-codex', {
                method: 'POST',
                headers,
                body: JSON.stringify({ asset_ids: ids, format }),
            });

            if (!res.ok) {
                let message = '导出失败';
                try {
                    const data = await res.json();
                    message = data.detail || message;
                } catch {}
                throw new Error(message);
            }

            if (format === 'text') {
                const data = await res.json();
                saveBlob(
                    new Blob([data.text || ''], { type: 'text/plain;charset=utf-8' }),
                    data.filename || 'codex_accounts.txt',
                );
            } else {
                const blob = await res.blob();
                const filename = filenameFromDisposition(
                    res.headers.get('content-disposition') || '',
                    ids.length === 1 ? 'codex_export.json' : 'codex_export.zip',
                );
                saveBlob(blob, filename);
            }
            toast(`已导出 ${ids.length} 个资产`, 'success');
        } catch (e) {
            toast(e.message || '导出失败');
        }
    }

    window._exportAsset = async (id) => {
        if (!validateCodexExportIds([id])) return;
        const format = await chooseCodexExportFormat(1);
        if (!format) return;
        await exportCodexAssets([id], format);
    };

    async function doBatchDelete() {
        const ids = [...selectedAssetIds];
        if (!ids.length) return;
        const ok = await confirmDialog({
            title: '批量删除资产',
            message: `确定删除选中的 ${ids.length} 个资产？`,
            detail: '只有未兑换的资产可以删除。',
            confirmText: '批量删除',
        });
        if (!ok) return;
        try {
            const res = await api('/assets/delete-batch', { method: 'POST', body: JSON.stringify({ ids }) });
            if (res.blocked?.length) {
                toast(`已删除 ${res.deleted} 个，${res.blocked.length} 个因已有 CDK 或兑换记录未删除`, 'warning');
            } else {
                toast(`已删除 ${res.deleted} 个资产`, 'success');
            }
            loadAssets();
        } catch (e) { toast(e.message); }
    }

    window._deleteAsset = async (id) => {
        const ok = await confirmDialog({
            title: '删除资产',
            message: '确定删除该资产？',
            detail: '只有未兑换的资产可以删除。',
            confirmText: '删除',
        });
        if (!ok) return;
        try { await api(`/assets/${id}`, { method: 'DELETE' }); loadAssets(); } catch (e) { toast(e.message); }
    };

    window._explainAssetDelete = (id) => {
        toast(assetDeleteReasons.get(id) || '该资产已有 CDK 或兑换记录，不能删除', 'warning');
    };

    window._toggleAssetRedeemStatus = async (id, redeemed) => {
        const ok = await confirmDialog({
            title: redeemed ? '标记为已兑换' : '标记为未兑换',
            message: redeemed ? '确定将该资产标记为已兑换？' : '确定将该资产改回未兑换？',
            detail: redeemed
                ? '该资产会从可兑换库存中移除，不绑定任何 CDK。'
                : '该操作会清除该资产关联的兑换记录、下载链接和 CDK 消耗记录，并重新放回库存。',
            confirmText: redeemed ? '设为已兑换' : '设为未兑换',
            danger: !redeemed,
        });
        if (!ok) return;
        try {
            await api(`/assets/${id}/redeem-status`, {
                method: 'PUT',
                body: JSON.stringify({ redeemed }),
            });
            toast(redeemed ? '已标记为已兑换' : '已标记为未兑换', 'success');
            loadAssets();
        } catch (e) {
            toast(e.message);
        }
    };

    // ── 资产预览模态框 ────────────────────────────
    const assetModal = $('#asset-modal');
    const modalTitle = $('#modal-title');
    const modalMeta = $('#modal-meta');
    const modalContent = $('#modal-content');
    const modalBody = $('#modal-body');
    const modalCopyBtn = $('#modal-copy-btn');
    const modalDownloadBtn = $('#modal-download-btn');
    let currentModalText = '';
    let currentModalAsset = null;

    $('#modal-close-btn').addEventListener('click', closeModal);
    assetModal.addEventListener('click', (e) => { if (e.target === assetModal) closeModal(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

    modalCopyBtn.addEventListener('click', async () => {
        if (await copyText(currentModalText)) {
            modalCopyBtn.textContent = '✅ 已复制';
            setTimeout(() => modalCopyBtn.textContent = '📋 复制', 2000);
        } else {
            toast('复制失败，请手动选择内容复制', 'warning');
        }
    });

    modalDownloadBtn.addEventListener('click', downloadCurrentModalAsset);

    function safeDownloadName(name, fallback = 'asset') {
        return (name || fallback).replace(/[\\/:*?"<>|]+/g, '_').trim() || fallback;
    }

    function withExtension(name, ext) {
        if (!ext) return name;
        return name.toLowerCase().endsWith(ext.toLowerCase()) ? name : `${name}${ext}`;
    }

    function saveBlob(blob, filename) {
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    async function downloadCurrentModalAsset() {
        if (!currentModalAsset) return;
        const asset = currentModalAsset;
        modalDownloadBtn.disabled = true;
        try {
            if (asset.type === 'file') {
                const headers = {};
                if (TOKEN) headers['Authorization'] = `Bearer ${TOKEN}`;
                const res = await fetch(`/api/assets/${asset.id}/file`, { headers });
                if (!res.ok) throw new Error('下载失败');
                const blob = await res.blob();
                const pathExt = (asset.file_path || '').match(/\.[A-Za-z0-9]{1,12}$/)?.[0] || '';
                const filename = withExtension(safeDownloadName(asset.name, `asset-${asset.id}`), pathExt);
                saveBlob(blob, filename);
            } else {
                const filename = withExtension(safeDownloadName(asset.name, `asset-${asset.id}`), '.txt');
                saveBlob(new Blob([currentModalText || ''], { type: 'text/plain;charset=utf-8' }), filename);
            }
            toast('已开始下载', 'success');
        } catch (e) {
            toast(e.message || '下载失败', 'warning');
        } finally {
            modalDownloadBtn.disabled = false;
        }
    }

    function closeModal() {
        assetModal.classList.add('hidden');
        document.body.style.overflow = '';
        currentModalText = '';
        currentModalAsset = null;
    }

    window._viewAsset = async (id) => {
        try {
            const asset = await api(`/assets/${id}`);
            currentModalAsset = asset;
            modalTitle.textContent = asset.name;
            modalCopyBtn.textContent = '📋 复制';
            modalDownloadBtn.textContent = '下载';
            modalDownloadBtn.disabled = false;

            const typeLabels = { text: '📝 文本', file: '📁 文件', link: '🔗 链接' };
            modalMeta.innerHTML = `<span class="badge badge-${asset.type}">${typeLabels[asset.type] || asset.type}</span>`
                + (asset.category_name ? ` <span style="color:var(--text-3);margin-left:8px">${esc(asset.category_name)}</span>` : '');

            if (asset.type === 'text') {
                currentModalText = asset.content || '';
                modalContent.textContent = currentModalText;
                modalCopyBtn.classList.remove('hidden');
            } else if (asset.type === 'link') {
                currentModalText = asset.content || '';
                modalContent.innerHTML = `<a href="${esc(asset.content)}" target="_blank" rel="noopener" style="color:var(--blue);word-break:break-all">${esc(asset.content)}</a>`;
                modalCopyBtn.classList.remove('hidden');
            } else if (asset.type === 'file' && asset.file_path) {
                modalContent.textContent = '加载中...';
                modalCopyBtn.classList.add('hidden');
                try {
                    const headers = {};
                    if (TOKEN) headers['Authorization'] = `Bearer ${TOKEN}`;
                    const res = await fetch(`/api/assets/${id}/file`, { headers });
                    if (!res.ok) throw new Error('无法加载文件内容');
                    const text = await res.text();
                    // 尝试格式化 JSON
                    try {
                        const json = JSON.parse(text);
                        currentModalText = JSON.stringify(json, null, 2);
                    } catch {
                        currentModalText = text;
                    }
                    modalContent.textContent = currentModalText;
                    modalCopyBtn.classList.remove('hidden');
                } catch {
                    modalContent.textContent = '无法加载文件内容';
                }
            }

            assetModal.classList.remove('hidden');
            document.body.style.overflow = 'hidden';
        } catch (e) {
            toast('加载资产失败: ' + e.message);
        }
    };

    $('#add-asset-btn').addEventListener('click', async () => {
        editingAssetId = null;
        await populateCategorySelects();
        $('#asset-form-title').textContent = '新增资产';
        $('#asset-name').value = ''; $('#asset-desc').value = '';
        $('#asset-content').value = ''; $('#asset-category-id').value = '';
        $('#asset-type').value = 'text';
        codexFiles = [];
        updateCodexMode();
        toggleAssetType();
        $('#asset-form').classList.remove('hidden');
    });
    $('#asset-cancel-btn').addEventListener('click', () => {
        $('#asset-form').classList.add('hidden');
        codexFiles = [];
    });
    $('#asset-type').addEventListener('change', toggleAssetType);
    $('#asset-category-id').addEventListener('change', updateCodexMode);

    // ── Codex 模式切换 ────────────────────────────
    function updateCodexMode() {
        const catId = $('#asset-category-id').value;
        const cat = cachedCategories.find(c => c.id === parseInt(catId));
        const newCodex = cat && cat.name === 'Codex';

        if (newCodex !== isCodexMode) {
            isCodexMode = newCodex;
            codexFiles = [];
            renderCodexFileList();

            if (isCodexMode) {
                // 锁定为文件类型
                $('#asset-type').value = 'file';
                $('#asset-type').disabled = true;
                $('#asset-name').disabled = true;
                $('#asset-name').value = '';
                $('#asset-name').placeholder = '自动从 JSON email 提取';
                $('#codex-mode-hint').classList.remove('hidden');
                // 显示批量上传区，隐藏普通区域
                $('#asset-content-group').classList.add('hidden');
                $('#asset-file-group').classList.add('hidden');
                $('#asset-files-group').classList.remove('hidden');
            } else {
                $('#asset-type').disabled = false;
                $('#asset-name').disabled = false;
                $('#asset-name').placeholder = '输入资产名称';
                $('#codex-mode-hint').classList.add('hidden');
                $('#asset-files-group').classList.add('hidden');
                toggleAssetType();
            }
        }
    }

    function toggleAssetType() {
        if (isCodexMode) return; // Codex 模式下不切换
        const t = $('#asset-type').value;
        $('#asset-content-group').classList.toggle('hidden', t === 'file');
        $('#asset-file-group').classList.toggle('hidden', t !== 'file');
        $('#asset-files-group').classList.add('hidden');
        $('#asset-content-label').textContent = t === 'link' ? '链接URL' : '文本内容';
        $('#asset-content').placeholder = t === 'link' ? 'https://...' : '输入文本内容...';
    }

    // ── Codex 拖拽 + 选择 ─────────────────────────
    const dropZone = $('#codex-drop-zone');
    const filesInput = $('#asset-files');

    dropZone?.addEventListener('click', () => filesInput.click());
    dropZone?.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('drag-over'); });
    dropZone?.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
    dropZone?.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
        addCodexFiles(e.dataTransfer.files);
    });
    filesInput?.addEventListener('change', () => {
        addCodexFiles(filesInput.files);
        filesInput.value = ''; // 允许重复选择
    });

    function addCodexFiles(fileList) {
        for (const f of fileList) {
            // 去重
            if (!codexFiles.some(e => e.name === f.name && e.size === f.size)) {
                codexFiles.push(f);
            }
        }
        renderCodexFileList();
    }

    function renderCodexFileList() {
        const el = $('#codex-file-list');
        if (!el) return;
        if (!codexFiles.length) { el.innerHTML = ''; return; }
        el.innerHTML = codexFiles.map((f, i) =>
            `<div class="codex-file-item">
                <span class="codex-file-name">📄 ${esc(f.name)}</span>
                <span class="codex-file-size">${(f.size/1024).toFixed(1)} KB</span>
                <button class="codex-file-remove" onclick="window._removeCodexFile(${i})">✕</button>
            </div>`
        ).join('');
    }

    window._removeCodexFile = (i) => {
        codexFiles.splice(i, 1);
        renderCodexFileList();
    };

    function showAssetWriteResult(result, label = '资产') {
        const created = Number(result?.created || 0);
        const skipped = Number(result?.skipped || 0);
        if (skipped > 0) {
            toast(`已创建 ${created} 个，跳过 ${skipped} 个重复${label}`, 'warning');
        } else {
            toast(`已创建 ${created} 个${label}`, 'success');
        }
    }

    // ── 保存资产 ──────────────────────────────────
    $('#asset-save-btn').addEventListener('click', async () => {
        const categoryId = $('#asset-category-id').value;

        if (isCodexMode) {
            // Codex 批量上传
            if (!codexFiles.length) return toast('请选择至少一个 JSON 文件', 'warning');
            const fd = new FormData();
            for (const f of codexFiles) fd.append('files', f);
            fd.append('category_id', categoryId || '0');
            fd.append('description', $('#asset-desc').value);

            try {
                const headers = {};
                if (TOKEN) headers['Authorization'] = `Bearer ${TOKEN}`;
                const res = await fetch(API + '/assets/upload-batch', { method: 'POST', headers, body: fd });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || '上传失败');
                showAssetWriteResult(data);
                $('#asset-form').classList.add('hidden');
                codexFiles = [];
                loadAssets();
            } catch (e) { toast(e.message); }
            return;
        }

        // 普通保存
        const t = $('#asset-type').value;
        const name = $('#asset-name').value.trim();
        if (!name) return toast('请输入资产名称', 'warning');
        try {
            if (t === 'file') {
                const file = $('#asset-file').files[0];
                if (!file) return toast('请选择文件', 'warning');
                const fd = new FormData();
                fd.append('file', file); fd.append('name', name);
                fd.append('description', $('#asset-desc').value);
                fd.append('category_id', categoryId || '0');
                const headers = {}; if (TOKEN) headers['Authorization'] = `Bearer ${TOKEN}`;
                const res = await fetch(API + '/assets/upload', { method: 'POST', headers, body: fd });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || '上传失败');
                showAssetWriteResult(data);
            } else {
                const data = await api('/assets', { method: 'POST', body: JSON.stringify({
                    name, type: t, description: $('#asset-desc').value,
                    content: $('#asset-content').value,
                    category_id: categoryId ? parseInt(categoryId) : null,
                })});
                showAssetWriteResult(data);
            }
            $('#asset-form').classList.add('hidden');
            loadAssets();
        } catch (e) { toast(e.message); }
    });

    // ── CDK 管理 ──────────────────────────────────
    let cdkPage = 1;
    let cdkPageSize = 20;
    let selectedCdkIds = new Set();
    let cdkDeleteReasons = new Map();
    let cdkCodes = new Map();
    let cdkCanDelete = new Map();

    async function loadCDKs(page) {
        if (page) cdkPage = page;
        const currentFilterCategory = $('#cdk-filter-category').value;
        const currentFormCategory = $('#cdk-category-id')?.value || '';
        await populateCDKSelects({
            filterCategoryId: currentFilterCategory,
            formCategoryId: currentFormCategory,
        });
        selectedCdkIds.clear();
        cdkDeleteReasons.clear();
        cdkCodes.clear();
        cdkCanDelete.clear();
        updateCDKBatchBar();
        const categoryId = $('#cdk-filter-category').value;
        const status = $('#cdk-filter-status').value;
        let path = `/cdks?paged=1&page=${cdkPage}&page_size=${cdkPageSize}&`;
        if (categoryId) path += `category_id=${categoryId}&`;
        if (status) path += `status=${status}&`;
        try {
            const data = normalizePagedData(await api(path));
            const list = data.items || [];
            list.forEach(c => {
                cdkCodes.set(c.id, c.code);
                cdkCanDelete.set(c.id, c.can_delete !== false);
                if (c.can_delete === false) {
                    cdkDeleteReasons.set(c.id, c.delete_block_reason || 'CDK 已有兑换记录，不能删除');
                }
            });
            if (!list.length) {
                $('#cdks-table').innerHTML = '<div class="empty-state">暂无CDK，点击右上角生成</div>';
                renderTablePagination({ id: 'cdks-pagination', tableId: 'cdks-table', data, pageHandler: '_cdkPage', pageSizeHandler: '_cdkPageSize', pageSize: cdkPageSize });
                return;
            }
            $('#cdks-table').innerHTML = `<table><thead><tr><th style="width:36px"><input type="checkbox" id="cdk-select-all"></th><th>兑换码</th><th>分类</th><th>状态</th><th>已兑/资产数</th><th>备注</th><th>操作</th></tr></thead><tbody>${
                list.map(c => {
                    const canDelete = c.can_delete !== false;
                    return `<tr>
                    <td><input type="checkbox" class="cdk-chk" value="${c.id}"></td>
                    <td class="code-text">${esc(c.code)}</td><td>${esc(c.category_name||'未分类')}</td>
                    <td><span class="badge badge-${c.status}">${statusLabel(c.status)}</span></td>
                    <td>${c.used_count}/${c.max_uses}</td><td>${esc(c.note)||'-'}</td>
                    <td style="display:flex;gap:6px">
                        ${c.status==='active'?`<button class="icon-btn" onclick="window._toggleCDK(${c.id},'disabled')">禁用</button>`:
                          c.status==='disabled'?`<button class="icon-btn" onclick="window._toggleCDK(${c.id},'active')">启用</button>`:''}
                        <button class="icon-btn danger${canDelete ? '' : ' disabled'}" onclick="window.${canDelete ? '_deleteCDK' : '_explainCDKDelete'}(${c.id})">删除</button>
                    </td>
                </tr>`;
                }).join('')
            }</tbody></table>`;
            renderTablePagination({ id: 'cdks-pagination', tableId: 'cdks-table', data, pageHandler: '_cdkPage', pageSizeHandler: '_cdkPageSize', pageSize: cdkPageSize });
            bindCDKCheckboxes();
        } catch (e) { console.error(e); }
    }

    function bindCDKCheckboxes() {
        const selectAll = $('#cdk-select-all');
        const chks = $$('.cdk-chk');
        selectAll?.addEventListener('change', () => {
            chks.forEach(c => { c.checked = selectAll.checked; });
            syncSelectedCDKs();
        });
        chks.forEach(c => c.addEventListener('change', () => {
            syncSelectedCDKs();
            selectAll.checked = chks.length && [...chks].every(c => c.checked);
        }));
    }

    function syncSelectedCDKs() {
        selectedCdkIds.clear();
        $$('.cdk-chk:checked').forEach(c => selectedCdkIds.add(parseInt(c.value)));
        updateCDKBatchBar();
    }

    function updateCDKBatchBar() {
        let bar = $('#cdk-batch-action-bar');
        if (!bar) {
            bar = document.createElement('div');
            bar.id = 'cdk-batch-action-bar';
            bar.className = 'batch-bar hidden';
            bar.innerHTML = '<span id="cdk-batch-count"></span><button id="cdk-batch-copy-btn" class="icon-btn">复制</button><button id="cdk-batch-delete-btn" class="icon-btn danger">批量删除</button>';
            $('#cdks-table').parentElement.insertBefore(bar, $('#cdks-table'));
            $('#cdk-batch-copy-btn').addEventListener('click', doCDKBatchCopy);
            $('#cdk-batch-delete-btn').addEventListener('click', doCDKBatchDelete);
        }
        if (selectedCdkIds.size > 0) {
            bar.classList.remove('hidden');
            $('#cdk-batch-count').textContent = `已选 ${selectedCdkIds.size} 项`;
            const deletableCount = [...selectedCdkIds].filter(id => cdkCanDelete.get(id) !== false).length;
            const deleteBtn = $('#cdk-batch-delete-btn');
            deleteBtn.disabled = deletableCount === 0;
            deleteBtn.classList.toggle('disabled', deletableCount === 0);
            deleteBtn.textContent = deletableCount === selectedCdkIds.size ? '批量删除' : `删除可删 ${deletableCount} 项`;
        } else {
            bar.classList.add('hidden');
        }
    }

    async function doCDKBatchCopy() {
        const codes = [...selectedCdkIds].map(id => cdkCodes.get(id)).filter(Boolean);
        if (!codes.length) return;
        const copied = await copyText(codes.join('\n'));
        toast(
            copied ? `已复制 ${codes.length} 个 CDK` : '复制失败，请手动选择内容复制',
            copied ? 'success' : 'warning',
        );
    }

    async function doCDKBatchDelete() {
        const ids = [...selectedCdkIds].filter(id => cdkCanDelete.get(id) !== false);
        if (!ids.length) return toast('已选 CDK 都有兑换记录，不能删除', 'warning');
        const skipped = selectedCdkIds.size - ids.length;
        const ok = await confirmDialog({
            title: '批量删除 CDK',
            message: `确定删除 ${ids.length} 个可删除 CDK？`,
            detail: skipped > 0 ? `另有 ${skipped} 个已有兑换记录，将不会删除。` : '',
            confirmText: '批量删除',
        });
        if (!ok) return;
        try {
            const res = await api('/cdks/delete-batch', { method: 'POST', body: JSON.stringify({ ids }) });
            if (res.blocked?.length) toast(`已删除 ${res.deleted} 个，${res.blocked.length} 个因已有兑换记录未删除`, 'warning');
            else toast(`已删除 ${res.deleted} 个 CDK`, 'success');
            loadCDKs();
        } catch (e) { toast(e.message); }
    }

    window._cdkPage = (p) => loadCDKs(Math.max(1, p));
    window._cdkPageSize = (size) => { cdkPageSize = parseInt(size) || 20; cdkPage = 1; loadCDKs(); };

    window._toggleCDK = async (id, status) => {
        try { await api(`/cdks/${id}/status`, { method:'PUT', body: JSON.stringify({status}) }); loadCDKs(); }
        catch (e) { toast(e.message); }
    };
    window._deleteCDK = async (id) => {
        const ok = await confirmDialog({
            title: '删除 CDK',
            message: '确定删除这个 CDK？',
            confirmText: '删除',
        });
        if (!ok) return;
        try { await api(`/cdks/${id}`, { method:'DELETE' }); loadCDKs(); } catch (e) { toast(e.message); }
    };

    window._explainCDKDelete = (id) => {
        toast(cdkDeleteReasons.get(id) || 'CDK 已有兑换记录，不能删除', 'warning');
    };

    $('#cdk-filter-category').addEventListener('change', () => { cdkPage = 1; loadCDKs(); });
    $('#cdk-filter-status').addEventListener('change', () => { cdkPage = 1; loadCDKs(); });

    $('#gen-cdk-btn').addEventListener('click', () => {
        populateCDKSelects(); $('#cdk-form').classList.remove('hidden');
    });
    $('#cdk-cancel-btn').addEventListener('click', () => $('#cdk-form').classList.add('hidden'));

    $('#cdk-save-btn').addEventListener('click', async () => {
        const categoryId = parseInt($('#cdk-category-id').value);
        if (!categoryId) return toast('请选择分类', 'warning');
        try {
            const generated = await api('/cdks/generate', { method: 'POST', body: JSON.stringify({
                category_id: categoryId, count: parseInt($('#cdk-count').value) || 1,
                prefix: $('#cdk-prefix').value || 'CDK',
                max_uses: parseInt($('#cdk-max-uses').value) || 1,
                note: $('#cdk-note').value,
            })});
            const codes = (generated || []).map(c => c.code).filter(Boolean);
            const copied = await copyText(codes.join('\n'));
            $('#cdk-form').classList.add('hidden');
            cdkPage = 1;
            loadCDKs();
            toast(
                copied
                    ? `已生成 ${codes.length} 个 CDK，并已复制到剪贴板`
                    : `已生成 ${codes.length} 个 CDK，但浏览器阻止了自动复制`,
                copied ? 'success' : 'warning',
            );
        } catch (e) { toast(e.message); }
    });

    async function populateCDKSelects({ filterCategoryId = '', formCategoryId = '' } = {}) {
        try {
            const cats = await fetchCategories();
            const catOpts = cats.map(c => `<option value="${c.id}">${esc(c.name)} (${c.asset_count})</option>`).join('');
            $('#cdk-category-id').innerHTML = `<option value="">-- 选择分类 --</option>${catOpts}`;
            $('#cdk-filter-category').innerHTML = `<option value="">全部分类</option>${catOpts}`;
            $('#cdk-category-id').value = formCategoryId;
            $('#cdk-filter-category').value = filterCategoryId;
        } catch {}
    }

    // ── 兑换记录 ──────────────────────────────────
    let logPage = 1;
    let logPageSize = 20;

    async function loadLogs(page) {
        if (page) logPage = page;
        try {
            const data = normalizePagedData(await api(`/admin/logs?paged=1&page=${logPage}&limit=${logPageSize}`));
            const list = data.items || [];
            if (!list.length) {
                $('#logs-table').innerHTML = '<div class="empty-state">暂无兑换记录</div>';
                renderTablePagination({ id: 'logs-pagination', tableId: 'logs-table', data, pageHandler: '_logPage', pageSizeHandler: '_logPageSize', pageSize: logPageSize });
                return;
            }
            $('#logs-table').innerHTML = `<table><thead><tr><th>CDK</th><th>分类</th><th>资产</th><th>IP</th><th>User Agent</th><th>时间</th></tr></thead><tbody>${
                list.map(r => `<tr><td class="code-text">${r.cdk_code}</td><td>${esc(r.category_name)||'<span style="color:var(--text-3)">未分类</span>'}</td><td>${esc(r.asset_name)}</td><td>${r.ip_address||'-'}</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${esc(r.user_agent||'-')}</td><td>${fmtTime(r.redeemed_at)}</td></tr>`).join('')
            }</tbody></table>`;
            renderTablePagination({ id: 'logs-pagination', tableId: 'logs-table', data, pageHandler: '_logPage', pageSizeHandler: '_logPageSize', pageSize: logPageSize });
        } catch (e) { console.error(e); }
    }

    window._logPage = (p) => loadLogs(Math.max(1, p));
    window._logPageSize = (size) => { logPageSize = parseInt(size) || 20; logPage = 1; loadLogs(); };

    // ── 上传记录 ──────────────────────────────────
    let uploadLogPage = 1;
    let uploadLogPageSize = 20;

    async function loadUploadLogs(page) {
        if (page) uploadLogPage = page;
        try {
            const data = normalizePagedData(await api(`/admin/upload-logs?paged=1&page=${uploadLogPage}&limit=${uploadLogPageSize}`));
            const list = data.items || [];
            if (!list.length) {
                $('#upload-logs-table').innerHTML = '<div class="empty-state">暂无上传记录</div>';
                renderTablePagination({ id: 'upload-logs-pagination', tableId: 'upload-logs-table', data, pageHandler: '_uploadLogPage', pageSizeHandler: '_uploadLogPageSize', pageSize: uploadLogPageSize });
                return;
            }
            $('#upload-logs-table').innerHTML = `<table><thead><tr><th>时间</th><th>状态</th><th>来源</th><th>ID</th><th>资产</th><th>类型</th><th>分类</th><th>原文件</th><th>大小</th><th>说明</th></tr></thead><tbody>${
                list.map(r => `<tr>
                    <td>${fmtTime(r.created_at)}</td>
                    <td><span class="badge ${uploadStatusClass(r.status)}">${esc(uploadStatusLabel(r.status))}</span></td>
                    <td>${esc(uploadSourceLabel(r.source))}</td>
                    <td>${r.asset_id || '-'}</td>
                    <td>${esc(r.asset_name) || '-'}</td>
                    <td>${assetTypeBadge(r.asset_type)}</td>
                    <td>${esc(r.category_name) || '<span style="color:var(--text-3)">未分类</span>'}</td>
                    <td>${esc(r.original_filename) || '-'}</td>
                    <td>${formatBytes(r.file_size)}</td>
                    <td>${esc(r.message) || '-'}</td>
                </tr>`).join('')
            }</tbody></table>`;
            renderTablePagination({ id: 'upload-logs-pagination', tableId: 'upload-logs-table', data, pageHandler: '_uploadLogPage', pageSizeHandler: '_uploadLogPageSize', pageSize: uploadLogPageSize });
        } catch (e) { console.error(e); }
    }

    window._uploadLogPage = (p) => loadUploadLogs(Math.max(1, p));
    window._uploadLogPageSize = (size) => { uploadLogPageSize = parseInt(size) || 20; uploadLogPage = 1; loadUploadLogs(); };

    // ── 工具 ──────────────────────────────────────
    function statusLabel(s) { return { active:'可用', used:'已用', disabled:'已禁用', expired:'已过期' }[s] || s; }
    function uploadStatusLabel(s) { return { created:'已新增', skipped:'已跳过', failed:'失败' }[s] || s || '-'; }
    function uploadStatusClass(s) { return { created:'badge-active', skipped:'badge-used', failed:'badge-disabled' }[s] || 'badge-used'; }
    function uploadSourceLabel(s) {
        return {
            manual_create: '手动创建',
            single_upload: '单文件上传',
            batch_upload: '批量上传',
            password_upload: '密码上传',
        }[s] || s || '-';
    }
    function assetTypeBadge(type) {
        if (!type) return '-';
        const label = { file:'文件', text:'文本', link:'链接' }[type] || type;
        const cls = { file:'badge-file', text:'badge-text', link:'badge-link' }[type] || 'badge-used';
        return `<span class="badge ${cls}">${esc(label)}</span>`;
    }
    function formatBytes(bytes) {
        const n = Number(bytes || 0);
        if (!n) return '-';
        if (n < 1024) return `${n} B`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        return `${(n / 1024 / 1024).toFixed(1)} MB`;
    }
    function esc(s) { if (!s) return ''; const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
    function fmtTime(s) { if (!s) return '-'; try { return new Date(s).toLocaleString('zh-CN'); } catch { return s; } }

    // ── 启动 ──────────────────────────────────────
    tryAutoLogin();
})();
