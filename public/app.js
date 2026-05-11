/**
 * CDK Vaults — 兑换前台逻辑
 * 自动识别 CDK 分类 → 普通兑换 / Codex 导出格式下载
 */
(() => {
    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => document.querySelectorAll(sel);

    const input = $('#cdk-input');
    const redeemBtn = $('#redeem-btn');
    const btnText = $('#redeem-btn .btn-text');
    const errorMsg = $('#error-msg');
    const redeemSection = $('#redeem-section');
    const resultSection = $('#result-section');
    const resultBody = $('#result-body');
    const resultName = $('#result-asset-name');
    const backBtn = $('#back-btn');
    const formatSelector = $('#format-selector');
    const quotaPanel = $('#quota-panel');
    const quotaRemaining = $('#quota-remaining');
    const quotaInventory = $('#quota-inventory');
    const quantityInput = $('#redeem-quantity');
    const quantityMinus = $('#quantity-minus');
    const quantityPlus = $('#quantity-plus');
    const siteNotice = $('#site-notice');
    const siteNoticeContent = $('#site-notice-content');

    let isCodexMode = false;
    let detectTimer = null;
    let lastDetectedCode = '';
    let detectedRemaining = 0;
    let detectedTotal = 0;
    let detectedInventory = 0;
    let detectedQuantityLimit = 0;
    let detectedAlreadyRedeemed = false;
    let detectedReexportCount = 0;
    let isSubmitting = false;
    let publicEvents = null;
    let publicRefreshTimer = null;

    // ── 格式选择器交互 ───────────────────────────
    $$('.format-option input').forEach(radio => {
        radio.addEventListener('change', () => {
            $$('.format-option').forEach(o => o.classList.remove('active'));
            radio.closest('.format-option').classList.add('active');
            updateActionText();
        });
    });

    // ── 输入处理 + 自动检测 ───────────────────────
    input.addEventListener('input', () => {
        const codes = parseCodes();
        updateRedeemButton(codes);
        hideError();
        scheduleDetect(codes);
    });

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey && !redeemBtn.disabled) {
            e.preventDefault();
            doRedeem();
        }
    });

    redeemBtn.addEventListener('click', doRedeem);
    backBtn.addEventListener('click', showRedeemSection);
    quantityInput.addEventListener('input', clampQuantity);
    quantityMinus.addEventListener('click', () => stepQuantity(-1));
    quantityPlus.addEventListener('click', () => stepQuantity(1));

    async function loadSiteNotice() {
        try {
            const res = await fetch('/api/redeem/notice');
            if (!res.ok) return;
            const notice = await res.json();
            const content = (notice.content || '').trim();
            if (notice.enabled && content) {
                siteNoticeContent.textContent = content;
                siteNotice.classList.remove('hidden');
            } else {
                siteNotice.classList.add('hidden');
            }
        } catch (_) {
            siteNotice.classList.add('hidden');
        }
    }

    function startPublicEvents() {
        if (!window.EventSource || publicEvents) return;
        publicEvents = new EventSource('/api/events/public-stream');
        publicEvents.addEventListener('update', (event) => {
            try {
                const data = JSON.parse(event.data || '{}');
                handlePublicUpdate(data.resources || []);
            } catch (_) {
                // Ignore malformed SSE payloads; the next event will recover.
            }
        });
        publicEvents.onerror = () => {
            // Native EventSource reconnects automatically.
        };
    }

    function handlePublicUpdate(resources) {
        const set = new Set(resources);
        if (set.has('notice')) loadSiteNotice();
        if (set.has('inventory') || set.has('assets') || set.has('cdks')) {
            clearTimeout(publicRefreshTimer);
            publicRefreshTimer = setTimeout(refreshCurrentDetection, 250);
        }
    }

    async function refreshCurrentDetection() {
        if (isSubmitting) return;
        const codes = parseCodes();
        if (!codes.length) return;
        try {
            await detectCodes(codes, { force: true });
        } catch (_) {
            // Keep the current UI if the transient refresh fails.
        }
    }

    // ── 解析 CDK 列表 ────────────────────────────
    function parseCodes() {
        return input.value
            .split(/[\n,;]+/)
            .map(s => s.trim())
            .filter(s => s.length >= 4);
    }

    // ── 自动检测 CDK 分类 (debounced) ────────────
    function getDetectKey(codes) {
        return codes.join('\n');
    }

    function applyDetectResult(codes, data) {
        const detectKey = getDetectKey(codes);
        lastDetectedCode = detectKey;
        detectedAlreadyRedeemed = data.already_redeemed === true;
        detectedReexportCount = detectedAlreadyRedeemed ? (parseInt(data.reexport_count) || 0) : 0;
        setCodexMode(data.is_codex === true);
        updateDetectHintText();
        updateActionText();
        if (data.found) {
            setQuotaInfo(
                data.remaining_count || 0,
                data.total_count || 0,
                true,
                data.inventory_count ?? data.remaining_count ?? 0,
                data.quantity_limit,
            );
        } else {
            detectedAlreadyRedeemed = false;
            detectedReexportCount = 0;
            setQuotaInfo(0, 0, false, 0, 0);
        }
    }

    async function detectCodes(codes, { force = false } = {}) {
        if (!codes.length) return null;
        const detectKey = getDetectKey(codes);
        if (!force && detectKey === lastDetectedCode) {
            return { found: true, is_codex: isCodexMode };
        }

        const res = await fetch('/api/redeem/detect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ codes }),
        });
        if (!res.ok) throw new Error('识别兑换码失败');
        const data = await res.json();
        applyDetectResult(codes, data);
        return data;
    }

    function scheduleDetect(codes) {
        clearTimeout(detectTimer);
        if (!codes.length) {
            setCodexMode(false);
            setQuotaInfo(0, 0, false, 0, 0);
            detectedAlreadyRedeemed = false;
            detectedReexportCount = 0;
            lastDetectedCode = '';
            return;
        }

        const detectKey = getDetectKey(codes);
        if (detectKey === lastDetectedCode) return;

        detectTimer = setTimeout(async () => {
            try {
                await detectCodes(codes);
            } catch (_) {
                // 静默失败
            }
        }, 350);
    }

    const detectHint = $('#detect-hint');
    const detectHintText = $('#detect-hint span:last-child');

    function updateDetectHintText() {
        if (!isCodexMode) return;
        if (detectedAlreadyRedeemed) {
            const codes = parseCodes();
            detectHintText.innerHTML = `${codes.length > 1 ? '这些 CDK 已全部兑换完成' : '该 CDK 已兑换过'}，可重新选择导出格式再次导出${detectedReexportCount ? ` ${detectedReexportCount} 个资产` : ''}`;
            return;
        }
        detectHintText.innerHTML = '已识别为 <strong>Codex</strong> 账号卡密，请选择导出格式';
    }

    function setCodexMode(codex) {
        if (codex === isCodexMode) return;
        isCodexMode = codex;
        if (codex) {
            detectHint.classList.remove('hidden');
            detectHint.classList.add('slide-in');
            formatSelector.classList.remove('hidden');
            formatSelector.classList.add('slide-in');
            updateDetectHintText();
            updateActionText();
        } else {
            detectHint.classList.add('hidden');
            detectHint.classList.remove('slide-in');
            formatSelector.classList.add('hidden');
            formatSelector.classList.remove('slide-in');
            updateActionText();
        }
    }

    function updateActionText() {
        if (!isCodexMode) {
            btnText.textContent = '兑换';
            return;
        }
        btnText.textContent = '兑换';
    }

    function setQuotaInfo(remaining, total, forceShow = false, inventory = remaining, quantityLimit = null) {
        detectedRemaining = Math.max(0, parseInt(remaining) || 0);
        detectedTotal = Math.max(detectedRemaining, parseInt(total) || 0);
        detectedInventory = Math.max(0, parseInt(inventory) || 0);
        detectedQuantityLimit = quantityLimit === null || quantityLimit === undefined
            ? Math.min(detectedRemaining, detectedInventory)
            : Math.max(0, parseInt(quantityLimit) || 0);
        if (forceShow || detectedTotal > 0) {
            quotaPanel.classList.remove('hidden');
            quotaRemaining.textContent = `${detectedRemaining} / ${detectedTotal}`;
            quotaInventory.textContent = String(detectedInventory);
            const redeemLimit = detectedAlreadyRedeemed ? 0 : detectedQuantityLimit;
            const empty = redeemLimit <= 0;
            quantityInput.min = empty ? '0' : '1';
            quantityInput.max = String(empty ? 0 : redeemLimit);
            if (empty) {
                quantityInput.value = '0';
            } else {
                if ((parseInt(quantityInput.value) || 1) > redeemLimit) {
                    quantityInput.value = String(redeemLimit);
                }
                if ((parseInt(quantityInput.value) || 0) < 1) quantityInput.value = '1';
            }
            quantityInput.disabled = empty;
            quantityMinus.disabled = empty;
            quantityPlus.disabled = empty;
        } else {
            quotaPanel.classList.add('hidden');
            quotaRemaining.textContent = `0 / ${detectedTotal}`;
            quotaInventory.textContent = '0';
            detectedQuantityLimit = 1;
            quantityInput.min = '1';
            quantityInput.max = '1';
            quantityInput.value = '1';
            quantityInput.disabled = false;
            quantityMinus.disabled = false;
            quantityPlus.disabled = false;
        }
        updateRedeemButton(parseCodes());
    }

    function clampQuantity() {
        const knownQuota = detectedTotal > 0;
        const limit = knownQuota ? detectedQuantityLimit : (parseInt(quantityInput.max) || 1);
        if (knownQuota && limit <= 0) {
            quantityInput.value = '0';
            return;
        }
        const max = Math.max(1, limit);
        let value = parseInt(quantityInput.value) || 1;
        value = Math.min(Math.max(value, 1), max);
        quantityInput.value = String(value);
    }

    function stepQuantity(delta) {
        const current = parseInt(quantityInput.value) || 1;
        quantityInput.value = String(current + delta);
        clampQuantity();
    }

    function getRedeemQuantity() {
        clampQuantity();
        return parseInt(quantityInput.value) || 1;
    }

    function updateRedeemButton(codes = parseCodes()) {
        const detectComplete = codes.length > 0 && getDetectKey(codes) === lastDetectedCode;
        redeemBtn.disabled = isSubmitting
            || codes.length === 0
            || !detectComplete
            || (!detectedAlreadyRedeemed && detectedTotal > 0 && (detectedRemaining <= 0 || detectedQuantityLimit <= 0));
    }

    // ── 兑换逻辑 ─────────────────────────────────
    async function doRedeem() {
        const codes = parseCodes();
        if (!codes.length) return;

        setLoading(true);
        hideError();

        try {
            const detected = await detectCodes(codes, { force: true });
            const shouldUseCodex = detected?.is_codex === true;
            if (shouldUseCodex) {
                await doCodexRedeem(codes);
            } else if (codes.length === 1) {
                await doNormalRedeem(codes[0]);
            } else {
                // 多码但非 Codex → 逐个兑换(取第一个)
                await doNormalRedeem(codes[0]);
            }
        } catch (err) {
            showError('网络错误，请稍后重试');
        } finally {
            setLoading(false);
        }
    }

    async function doCodexRedeem(codes) {
        const format = $('input[name="export-format"]:checked')?.value || 'cpa';
        const quantity = getRedeemQuantity();
        const res = await fetch('/api/redeem/codex', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ codes, format, quantity }),
        });

        if (!res.ok) {
            const errData = await res.json();
            showError(errData.detail || '兑换失败');
            return;
        }

        const redeemedCount = parseInt(res.headers.get('x-redeemed-count')) || quantity * codes.length;
        const remainingCount = parseInt(res.headers.get('x-remaining-count')) || 0;
        const reexported = res.headers.get('x-reexported') === '1';
        const skippedIncompatibleCount = parseInt(res.headers.get('x-skipped-incompatible-count')) || 0;

        if (format === 'text') {
            const data = await res.json();
            setQuotaInfo(
                data.remaining_count ?? remainingCount,
                detectedTotal,
                true,
                data.inventory_count ?? 0,
                0,
            );
            showTextResult({
                text: data.text || '',
                filename: data.filename || 'codex_accounts.txt',
                count: data.redeemed_count || redeemedCount,
                remainingCount: data.remaining_count ?? remainingCount,
                reexported: data.reexported === true || reexported,
                skippedIncompatibleCount,
            });
            return;
        }

        // 文件下载
        const blob = await res.blob();
        const disposition = res.headers.get('content-disposition') || '';
        let filename = 'download';
        const match = disposition.match(/filename="?([^"]+)"?/);
        if (match) filename = match[1];

        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        a.click();
        URL.revokeObjectURL(a.href);

        const inventoryCount = parseInt(res.headers.get('x-inventory-count')) || 0;
        setQuotaInfo(remainingCount, detectedTotal, true, inventoryCount);
        showDownloadResult(redeemedCount, filename, format, remainingCount, reexported, skippedIncompatibleCount);
    }

    async function doNormalRedeem(code) {
        const quantity = getRedeemQuantity();
        const res = await fetch('/api/redeem', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code, quantity }),
        });
        const data = await res.json();
        if (!res.ok) {
            showError(data.detail || '兑换失败');
            return;
        }
        setQuotaInfo(
            data.remaining_count ?? 0,
            data.total_count || detectedTotal,
            true,
            data.inventory_count ?? 0,
        );
        showResult(data);
    }

    // ── 显示下载结果 ─────────────────────────────
    function formatSkippedHint(count) {
        return count > 0 ? ` · 已跳过 ${count} 个不符合当前格式的账号` : '';
    }

    function showDownloadResult(count, filename, format, remainingCount = 0, reexported = false, skippedIncompatibleCount = 0) {
        const fmtLabels = {
            cpa: 'CPA 格式（OAuth JSON）',
            sub2api_single: 'Sub2API 合并文件（需 access_token）',
            auth_json: 'auth.json 格式（Codex 原始 Oauth 格式）',
            sub2api_multi: 'auth.json 格式（Codex 原始 Oauth 格式）',
            text: '文本格式（邮箱/GPT密码/邮箱密码）',
        };
        resultName.textContent = reexported
            ? `已重新导出 ${count} 个资产`
            : `已兑换 ${count} 个资产 · 剩余 ${remainingCount} 个${formatSkippedHint(skippedIncompatibleCount)}`;
        resultBody.innerHTML = `
            <div class="download-success">
                <div class="dl-icon">⬇</div>
                <div class="dl-info">
                    <div class="dl-filename">${esc(filename)}</div>
                    <div class="dl-meta">格式: ${fmtLabels[format] || format}</div>
                </div>
            </div>
        `;
        redeemSection.classList.add('hidden');
        resultSection.classList.remove('hidden');
    }

    function showTextResult({ text, filename, count, remainingCount = 0, reexported = false, skippedIncompatibleCount = 0 }) {
        resultName.textContent = reexported
            ? `已重新导出 ${count} 个资产`
            : `已兑换 ${count} 个资产 · 剩余 ${remainingCount} 个${formatSkippedHint(skippedIncompatibleCount)}`;

        const box = document.createElement('div');
        box.className = 'text-export-result';

        const notice = document.createElement('div');
        notice.className = 'text-export-notice';
        notice.innerHTML = '<strong>文本格式</strong><span>每行一个账号：邮箱----GPT密码----邮箱密码</span>';

        const toolbar = document.createElement('div');
        toolbar.className = 'text-export-toolbar';

        const meta = document.createElement('div');
        meta.className = 'text-export-meta';
        meta.textContent = filename;

        const copyBtn = document.createElement('button');
        copyBtn.className = 'text-export-btn';
        copyBtn.textContent = '复制';
        copyBtn.addEventListener('click', async () => {
            await copyText(text);
            copyBtn.textContent = '已复制';
            setTimeout(() => copyBtn.textContent = '复制', 1800);
        });

        const downloadBtn = document.createElement('button');
        downloadBtn.className = 'text-export-btn secondary';
        downloadBtn.textContent = '下载 TXT';
        downloadBtn.addEventListener('click', () => downloadTextFile(text, filename));

        const pre = document.createElement('pre');
        pre.className = 'text-export-content';
        pre.textContent = text || '没有可导出的文本';

        toolbar.append(meta, copyBtn, downloadBtn);
        box.append(notice, toolbar, pre);
        resultBody.innerHTML = '';
        resultBody.appendChild(box);
        redeemSection.classList.add('hidden');
        resultSection.classList.remove('hidden');
    }

    function downloadTextFile(text, filename) {
        const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = filename || 'codex_accounts.txt';
        a.click();
        URL.revokeObjectURL(a.href);
    }

    async function copyText(text) {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(text);
            return;
        }
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        textarea.remove();
    }

    // ── 显示普通结果 ─────────────────────────────
    function showResult(data) {
        const assets = data.assets?.length ? data.assets : (data.asset ? [data.asset] : []);
        resultName.textContent = `已兑换 ${assets.length} 个资产 · 剩余 ${data.remaining_count || 0} 个`;
        resultBody.innerHTML = '';

        const list = document.createElement('div');
        list.className = 'asset-list';
        assets.forEach(asset => list.appendChild(renderAssetResult(asset)));
        resultBody.appendChild(list);

        redeemSection.classList.add('hidden');
        resultSection.classList.remove('hidden');
    }

    function renderAssetResult(asset) {
        const item = document.createElement('div');
        item.className = 'asset-result-item';

        const title = document.createElement('div');
        title.className = 'asset-result-name';
        title.textContent = asset.name;
        item.appendChild(title);

        if (asset.description) {
            const desc = document.createElement('div');
            desc.className = 'asset-result-desc';
            desc.textContent = asset.description;
            item.appendChild(desc);
        }

        if (asset.type === 'text') {
            const pre = document.createElement('div');
            pre.className = 'asset-text-content';
            pre.textContent = asset.content || '';
            item.appendChild(pre);

            const copyBtn = document.createElement('button');
            copyBtn.textContent = '📋 复制内容';
            copyBtn.style.cssText = 'margin-top: 12px; padding: 8px 16px; background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: 8px; color: var(--text-secondary); cursor: pointer; font-size: 0.85rem;';
            copyBtn.addEventListener('click', () => {
                navigator.clipboard.writeText(asset.content || '');
                copyBtn.textContent = '✅ 已复制';
                setTimeout(() => copyBtn.textContent = '📋 复制内容', 2000);
            });
            item.appendChild(copyBtn);
        }

        if (asset.type === 'file' && asset.download_url) {
            const a = document.createElement('a');
            a.className = 'asset-file-download';
            a.href = asset.download_url;
            a.download = asset.name;
            a.textContent = '⬇ 下载文件: ' + asset.name;
            item.appendChild(a);
        }

        if (asset.type === 'link' && asset.content) {
            const a = document.createElement('a');
            a.className = 'asset-link';
            a.href = asset.content;
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
            a.textContent = '🔗 打开链接: ' + asset.content;
            item.appendChild(a);
        }

        return item;
    }

    function showRedeemSection() {
        resultSection.classList.add('hidden');
        redeemSection.classList.remove('hidden');
        input.focus();
        updateRedeemButton(parseCodes());
        scheduleDetect(parseCodes());
    }

    // ── 工具函数 ─────────────────────────────────
    function setLoading(loading) {
        const text = redeemBtn.querySelector('.btn-text');
        const loader = redeemBtn.querySelector('.btn-loader');
        isSubmitting = loading;
        if (loading) {
            text.classList.add('hidden');
            loader.classList.remove('hidden');
            redeemBtn.disabled = true;
            input.disabled = true;
        } else {
            text.classList.remove('hidden');
            loader.classList.add('hidden');
            input.disabled = false;
            updateRedeemButton(parseCodes());
        }
    }

    function showError(msg) {
        errorMsg.textContent = msg;
        errorMsg.classList.remove('hidden');
    }

    function hideError() {
        errorMsg.classList.add('hidden');
    }

    function esc(s) {
        if (!s) return '';
        const d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }

    // 自动聚焦
    loadSiteNotice();
    startPublicEvents();
    input.focus();
})();
