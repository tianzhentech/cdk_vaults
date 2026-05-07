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
    const quantityInput = $('#redeem-quantity');
    const quantityMinus = $('#quantity-minus');
    const quantityPlus = $('#quantity-plus');

    let isCodexMode = false;
    let detectTimer = null;
    let lastDetectedCode = '';
    let detectedRemaining = 0;
    let detectedTotal = 0;

    // ── 格式选择器交互 ───────────────────────────
    $$('.format-option input').forEach(radio => {
        radio.addEventListener('change', () => {
            $$('.format-option').forEach(o => o.classList.remove('active'));
            radio.closest('.format-option').classList.add('active');
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

    // ── 解析 CDK 列表 ────────────────────────────
    function parseCodes() {
        return input.value
            .split(/[\n,;]+/)
            .map(s => s.trim())
            .filter(s => s.length >= 4);
    }

    // ── 自动检测 CDK 分类 (debounced) ────────────
    function scheduleDetect(codes) {
        clearTimeout(detectTimer);
        if (!codes.length) {
            setCodexMode(false);
            setQuotaInfo(0, 0);
            lastDetectedCode = '';
            return;
        }

        const firstCode = codes[0];
        if (firstCode === lastDetectedCode) return;

        detectTimer = setTimeout(async () => {
            try {
                const res = await fetch('/api/redeem/detect', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ code: firstCode }),
                });
                if (!res.ok) return;
                const data = await res.json();
                lastDetectedCode = firstCode;
                setCodexMode(data.is_codex === true);
                if (data.found) setQuotaInfo(data.remaining_count || 0, data.total_count || 0);
                else setQuotaInfo(0, 0);
            } catch (_) {
                // 静默失败
            }
        }, 350);
    }

    const detectHint = $('#detect-hint');

    function setCodexMode(codex) {
        if (codex === isCodexMode) return;
        isCodexMode = codex;
        if (codex) {
            detectHint.classList.remove('hidden');
            detectHint.classList.add('slide-in');
            formatSelector.classList.remove('hidden');
            formatSelector.classList.add('slide-in');
            btnText.textContent = '下载';
        } else {
            detectHint.classList.add('hidden');
            detectHint.classList.remove('slide-in');
            formatSelector.classList.add('hidden');
            formatSelector.classList.remove('slide-in');
            btnText.textContent = '兑换';
        }
    }

    function setQuotaInfo(remaining, total) {
        detectedRemaining = Math.max(0, parseInt(remaining) || 0);
        detectedTotal = Math.max(detectedRemaining, parseInt(total) || 0);
        if (detectedRemaining > 0) {
            quotaPanel.classList.remove('hidden');
            quotaRemaining.textContent = `${detectedRemaining} / ${detectedTotal}`;
            quantityInput.max = String(detectedRemaining);
            if ((parseInt(quantityInput.value) || 1) > detectedRemaining) {
                quantityInput.value = String(detectedRemaining);
            }
            if ((parseInt(quantityInput.value) || 0) < 1) quantityInput.value = '1';
        } else {
            quotaPanel.classList.add('hidden');
            quotaRemaining.textContent = `0 / ${detectedTotal}`;
            quantityInput.max = '1';
            quantityInput.value = '1';
        }
        updateRedeemButton(parseCodes());
    }

    function clampQuantity() {
        const max = Math.max(1, detectedRemaining || 1);
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
        redeemBtn.disabled = codes.length === 0 || (detectedTotal > 0 && detectedRemaining <= 0);
    }

    // ── 兑换逻辑 ─────────────────────────────────
    async function doRedeem() {
        const codes = parseCodes();
        if (!codes.length) return;

        setLoading(true);
        hideError();

        try {
            if (isCodexMode) {
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

        // 文件下载
        const blob = await res.blob();
        const disposition = res.headers.get('content-disposition') || '';
        let filename = 'download';
        const match = disposition.match(/filename="?([^"]+)"?/);
        if (match) filename = match[1];
        const redeemedCount = parseInt(res.headers.get('x-redeemed-count')) || quantity * codes.length;
        const remainingCount = parseInt(res.headers.get('x-remaining-count')) || 0;

        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        a.click();
        URL.revokeObjectURL(a.href);

        setQuotaInfo(remainingCount, detectedTotal);
        showDownloadResult(redeemedCount, filename, format, remainingCount);
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
        setQuotaInfo(data.remaining_count || 0, data.total_count || detectedTotal);
        showResult(data);
    }

    // ── 显示下载结果 ─────────────────────────────
    function showDownloadResult(count, filename, format, remainingCount = 0) {
        const fmtLabels = {
            cpa: 'CPA 原格式',
            sub2api_single: 'Sub2API 合并文件',
            sub2api_multi: 'Sub2API 独立文件',
        };
        resultName.textContent = `已兑换 ${count} 个资产 · 剩余 ${remainingCount} 个`;
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
    input.focus();
})();
