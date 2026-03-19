const healthState = {
    pollHandle: null,
    isFetching: false,
    lastReport: null,
    templateCapture: {
        busy: false,
        outcome: null,
    },
};

const mojibakePattern = /[�锛鎴姝鏈褰娌鑾彇鍒閿辫缃粶璇]/;

function $(id) {
    return document.getElementById(id);
}

function looksLikeMojibake(text) {
    if (!text) {
        return false;
    }
    return mojibakePattern.test(String(text));
}

function safeText(text, fallback = "等待中") {
    if (!text) {
        return fallback;
    }
    return looksLikeMojibake(text) ? fallback : String(text);
}

function escapeHtml(text) {
    return String(text ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

function formatTime(value) {
    if (!value) {
        return "-";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
        return value;
    }
    return date.toLocaleString("zh-CN", {
        hour12: false,
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
    });
}

function collectorLabel(method) {
    if (method === "uiautomation") {
        return "微信界面自动识别";
    }
    if (method === "cv") {
        return "CV + 代理抓取";
    }
    return method || "未知";
}

function sourceLabel(source) {
    if (source === "collector") {
        return "自动获取";
    }
    if (source === "manual") {
        return "手动输入";
    }
    if (source === "file") {
        return "缓存回退";
    }
    return "等待中";
}

function healthStatusLabel(status) {
    if (status === "pass") {
        return "通过";
    }
    if (status === "warn") {
        return "注意";
    }
    if (status === "fail") {
        return "阻塞";
    }
    if (status === "skip") {
        return "不适用";
    }
    return "未知";
}

function healthStatusTone(status) {
    if (status === "pass") {
        return "success";
    }
    if (status === "warn") {
        return "warning";
    }
    if (status === "fail") {
        return "danger";
    }
    return "idle";
}

function categoryLabel(category) {
    if (category === "runtime") {
        return "运行状态";
    }
    return "环境检查";
}

function templateRoleLabel(role) {
    if (role === "session") {
        return "会话模板";
    }
    if (role === "menu_button") {
        return "底部按钮模板";
    }
    if (role === "menu_item") {
        return "菜单项模板";
    }
    if (role === "close") {
        return "关闭按钮模板";
    }
    return role || "未命名模板";
}

function templateSourceTone(source) {
    if (source === "missing") {
        return "danger";
    }
    if (source === "override" || source === "configured") {
        return "warning";
    }
    if (source === "default") {
        return "success";
    }
    return "idle";
}

function templateCaptureSourceLabel(source) {
    if (source === "matched-template") {
        return "根据现有模板定位后截图";
    }
    if (source === "fallback-region") {
        return "按窗口区域兜底截图";
    }
    return "自动截图";
}

function renderBanner(report) {
    const banner = $("health-banner");
    const summary = report.summary || {};
    const tone = summary.tone || "idle";
    const shouldShow = summary.overall_status !== "ready";

    if (!shouldShow) {
        banner.className = "notice-banner hidden";
        banner.textContent = "";
        return;
    }

    const toneClass = tone === "danger"
        ? "notice-banner--danger"
        : "notice-banner--warning";
    banner.className = `notice-banner ${toneClass}`;
    banner.textContent = safeText(summary.next_action, "请优先处理列表里的阻塞项。");
}

function renderHero(report) {
    const summary = report.summary || {};
    $("health-overall-pill").className = `state-pill state-pill--${summary.tone || "idle"}`;
    $("health-overall-pill").textContent = summary.overall_status === "ready"
        ? "已就绪"
        : summary.overall_status === "blocked"
            ? "待修复"
            : "需关注";
    $("health-overall-title").textContent = safeText(summary.title, "环境体检");
    $("health-overall-description").textContent = safeText(summary.description, "正在检查本机部署状态。");
    $("health-collector-method").textContent = collectorLabel(summary.collector_method);
    $("health-generated-at").textContent = formatTime(report.generated_at);
    $("health-next-action").textContent = safeText(summary.next_action, "等待体检结果");
}

function renderOverview(report) {
    const summary = report.summary || {};
    const counts = summary.counts || {};
    const openidStatus = (report.runtime_state || {}).openid_status || {};

    $("health-pass-count").textContent = String(counts.pass || 0);
    $("health-warn-count").textContent = String(counts.warn || 0);
    $("health-fail-count").textContent = String(counts.fail || 0);
    $("health-openid").textContent = openidStatus.openid_masked || "尚未获取";
    $("health-openid-note").textContent = openidStatus.openid
        ? `当前来源：${sourceLabel(openidStatus.current_source)}，上次刷新：${formatTime(openidStatus.last_refresh_at)}`
        : "当前还没有可用 OpenID，优先看运行状态和微信窗口检查。";
}

function renderRuntime(report) {
    const runtimeState = report.runtime_state || {};
    const summary = runtimeState.summary || {};
    const openidStatus = runtimeState.openid_status || {};
    const pipelineStatus = runtimeState.pipeline_status || {};
    const details = [
        ["运行方式", collectorLabel(summary.collector_method)],
        ["当前来源", sourceLabel(openidStatus.current_source)],
        ["当前 OpenID", openidStatus.openid_masked || "未获取"],
        ["状态消息", safeText(summary.status_message, "等待中")],
        ["上次刷新", formatTime(openidStatus.last_refresh_at)],
        ["下次刷新", formatTime(openidStatus.next_refresh_at)],
        ["缓存回退", openidStatus.used_file_fallback ? "是" : "否"],
        ["活跃任务", `${pipelineStatus.active_sign_count || 0} 个`],
    ];

    if (openidStatus.last_error) {
        details.push(["最近错误", safeText(openidStatus.last_error, "请查看上方检查项")]);
    }

    $("health-runtime-details").innerHTML = details.map(([label, value]) => `
        <div class="detail-row">
            <dt>${escapeHtml(label)}</dt>
            <dd>${escapeHtml(value || "-")}</dd>
        </div>
    `).join("");
}

function renderFacts(facts) {
    if (!Array.isArray(facts) || !facts.length) {
        return "";
    }

    return `
        <div class="health-facts">
            ${facts.map((fact) => `
                <div class="health-fact">
                    <span>${escapeHtml(safeText(fact.label, "信息"))}</span>
                    <strong>${escapeHtml(safeText(String(fact.value ?? "-"), "-"))}</strong>
                </div>
            `).join("")}
        </div>
    `;
}

function renderChecks(report) {
    const checks = Array.isArray(report.checks) ? report.checks : [];
    $("health-check-count").textContent = `${checks.length} 项`;

    if (!checks.length) {
        $("health-check-grid").innerHTML = `
            <div class="empty-state">
                <strong>还没有拿到体检结果</strong>
                <p>如果页面长时间为空，请先确认本地服务已正常启动。</p>
            </div>
        `;
        return;
    }

    $("health-check-grid").innerHTML = checks.map((check) => {
        const tone = healthStatusTone(check.status);
        return `
            <article class="health-card health-card--${tone}">
                <div class="health-card-top">
                    <div>
                        <p class="panel-eyebrow">${escapeHtml(categoryLabel(check.category))}</p>
                        <h3>${escapeHtml(safeText(check.title, "未命名检查"))}</h3>
                    </div>
                    <span class="state-pill state-pill--${tone} state-pill--compact">${healthStatusLabel(check.status)}</span>
                </div>
                <p class="health-card-summary">${escapeHtml(safeText(check.summary, "暂无摘要"))}</p>
                ${check.detail ? `<p class="health-card-detail">${escapeHtml(safeText(check.detail, ""))}</p>` : ""}
                ${renderFacts(check.facts)}
                ${check.action ? `
                    <div class="health-card-action">
                        <strong>下一步</strong>
                        <p>${escapeHtml(safeText(check.action, "按当前提示处理即可。"))}</p>
                    </div>
                ` : ""}
            </article>
        `;
    }).join("");
}

function renderTemplateStatus(report) {
    const templateStatus = report.template_status || {};
    const templates = Array.isArray(templateStatus.templates) ? templateStatus.templates : [];
    const counts = templateStatus.counts || {};
    const summary = templateStatus.summary || "等待模板状态";

    $("health-template-count").textContent = `${templates.length} 张`;
    $("health-template-summary").textContent = summary;

    if (!templates.length) {
        $("health-template-grid").innerHTML = `
            <div class="empty-state">
                <strong>还没有模板状态</strong>
                <p>如果当前不是新版微信模式，也可以先查看默认模板目录和本机覆盖目录。</p>
            </div>
        `;
        return;
    }

    $("health-template-grid").innerHTML = templates.map((item) => {
        const tone = templateSourceTone(item.source);
        const facts = [
            { label: "当前来源", value: item.source_label || "未知" },
            { label: "文件名", value: item.filename || "-" },
            { label: "默认模板", value: item.default_exists ? "存在" : "缺失" },
            { label: "本机覆盖", value: item.override_exists ? "存在" : "缺失" },
            { label: "更新时间", value: item.updated_at ? formatTime(item.updated_at) : "-" },
        ];
        const detail = item.resolved_path || item.default_path || item.override_path || "-";
        const cardSummary = item.exists
            ? `当前使用 ${item.source_label || "模板"}`
            : "当前还没有可用模板文件";
        return `
            <article class="health-card health-card--${tone}">
                <div class="health-card-top">
                    <div>
                        <p class="panel-eyebrow">模板角色</p>
                        <h3>${escapeHtml(templateRoleLabel(item.role))}</h3>
                    </div>
                    <span class="state-pill state-pill--${tone} state-pill--compact">${escapeHtml(item.source_label || "未知")}</span>
                </div>
                <p class="health-card-summary">${escapeHtml(cardSummary)}</p>
                <p class="health-card-detail">${escapeHtml(detail)}</p>
                ${renderFacts(facts)}
            </article>
        `;
    }).join("");

    if ((counts.missing || 0) > 0) {
        $("health-template-summary").textContent = `${summary} 当前缺失 ${counts.missing} 张模板。`;
    }
}

function renderTemplateCapture(report) {
    const summary = report.summary || {};
    const method = summary.collector_method;
    const captureState = healthState.templateCapture;
    const supported = method === "cv";
    const openButton = $("template-capture-open-btn");
    const visibleButton = $("template-capture-visible-btn");
    const pill = $("health-template-capture-pill");
    const status = $("health-template-capture-status");
    const results = $("health-template-capture-results");

    openButton.disabled = captureState.busy || !supported;
    visibleButton.disabled = captureState.busy || !supported;
    openButton.textContent = captureState.busy ? "正在采集..." : "已经在聊天页，开始采集";
    visibleButton.textContent = captureState.busy ? "正在采集..." : "只在左侧可见，帮我点开后采集";

    let outcome = captureState.outcome;
    if (!supported) {
        outcome = {
            tone: "idle",
            pill: "不可用",
            title: "当前不是 cv 模式",
            message: "只有新版微信的 cv 模式才需要自动采集本机模板。",
            results: [],
        };
    } else if (!outcome) {
        outcome = {
            tone: "idle",
            pill: "待开始",
            title: "尚未开始自动采集",
            message: "确认微信窗口可见后，再选择当前所处状态开始采集。",
            results: [],
        };
    }

    pill.textContent = outcome.pill || "待开始";
    status.className = `capture-status capture-status--${outcome.tone || "idle"}`;
    status.innerHTML = `
        <strong>${escapeHtml(safeText(outcome.title, "自动采集本机模板"))}</strong>
        <p>${escapeHtml(safeText(outcome.message, "等待开始"))}</p>
    `;

    if (Array.isArray(outcome.results) && outcome.results.length) {
        results.className = "capture-result-list";
        results.innerHTML = outcome.results.map((item) => {
            const size = item.image_size || {};
            const width = size.width || "-";
            const height = size.height || "-";
            return `
                <article class="capture-result-card">
                    <div>
                        <p class="panel-eyebrow">模板角色</p>
                        <h3>${escapeHtml(templateRoleLabel(item.role))}</h3>
                    </div>
                    <p class="capture-result-path">${escapeHtml(safeText(item.path, "-"))}</p>
                    <div class="capture-result-meta">
                        <span>${escapeHtml(templateCaptureSourceLabel(item.capture_source))}</span>
                        <strong>${escapeHtml(`${width} x ${height}`)}</strong>
                    </div>
                </article>
            `;
        }).join("");
        return;
    }

    results.className = "capture-result-list hidden";
    results.innerHTML = "";
}

function renderReport(report) {
    healthState.lastReport = report;
    renderBanner(report);
    renderHero(report);
    renderOverview(report);
    renderChecks(report);
    renderRuntime(report);
    renderTemplateStatus(report);
    renderTemplateCapture(report);
}

function renderLoadError(message) {
    $("health-overall-pill").className = "state-pill state-pill--danger";
    $("health-overall-pill").textContent = "无法连接";
    $("health-overall-title").textContent = "环境体检页暂时无法获取结果";
    $("health-overall-description").textContent = message;
    $("health-next-action").textContent = "先确认本地服务已启动";
    $("health-banner").className = "notice-banner notice-banner--danger";
    $("health-banner").textContent = message;
    $("health-check-grid").innerHTML = `
        <div class="empty-state">
            <strong>无法连接本地服务</strong>
            <p>${escapeHtml(message)}</p>
        </div>
    `;
    $("health-template-grid").innerHTML = `
        <div class="empty-state">
            <strong>模板状态暂时不可用</strong>
            <p>${escapeHtml(message)}</p>
        </div>
    `;
    $("template-capture-open-btn").disabled = true;
    $("template-capture-visible-btn").disabled = true;
    $("health-template-capture-pill").textContent = "不可用";
    $("health-template-capture-status").className = "capture-status capture-status--danger";
    $("health-template-capture-status").innerHTML = `
        <strong>自动采集入口暂时不可用</strong>
        <p>${escapeHtml(message)}</p>
    `;
}

async function fetchHealth() {
    if (healthState.isFetching) {
        return;
    }
    healthState.isFetching = true;

    try {
        const response = await fetch("/api/health", { cache: "no-store" });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        const report = await response.json();
        renderReport(report);
    } catch (error) {
        renderLoadError("无法连接 /api/health，请确认服务已通过一键启动脚本或 start_web_app.ps1 启动。");
    } finally {
        healthState.isFetching = false;
    }
}

function parseDownloadFilename(disposition) {
    if (!disposition) {
        return "wei-class-support-bundle.zip";
    }

    const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    if (utf8Match && utf8Match[1]) {
        return decodeURIComponent(utf8Match[1]);
    }

    const plainMatch = disposition.match(/filename="?([^";]+)"?/i);
    if (plainMatch && plainMatch[1]) {
        return plainMatch[1];
    }

    return "wei-class-support-bundle.zip";
}

async function exportSupportBundle() {
    const button = $("health-export-btn");
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "正在导出...";

    try {
        const response = await fetch("/api/support_bundle", { cache: "no-store" });
        if (!response.ok) {
            let message = `HTTP ${response.status}`;
            try {
                const payload = await response.json();
                if (payload && payload.message) {
                    message = payload.message;
                }
            } catch (error) {
                message = `HTTP ${response.status}`;
            }
            throw new Error(message);
        }

        const blob = await response.blob();
        const filename = parseDownloadFilename(response.headers.get("Content-Disposition"));
        const downloadUrl = window.URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = downloadUrl;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.URL.revokeObjectURL(downloadUrl);
    } catch (error) {
        const message = error instanceof Error
            ? error.message
            : "导出诊断包失败，请先确认本地服务运行正常。";
        window.alert(message);
    } finally {
        button.disabled = false;
        button.textContent = originalText;
    }
}

function updateTemplateCaptureOutcome(outcome) {
    healthState.templateCapture.outcome = outcome;
    if (healthState.lastReport) {
        renderTemplateCapture(healthState.lastReport);
    }
}

async function runTemplateCapture(chatState) {
    if (healthState.templateCapture.busy) {
        return;
    }

    healthState.templateCapture.busy = true;
    updateTemplateCaptureOutcome({
        tone: "warning",
        pill: "采集中",
        title: "正在自动采集本机模板",
        message: "请保持微信窗口可见，不要手动切换聊天页或最小化微信。",
        results: [],
    });

    try {
        const response = await fetch("/api/template_capture", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({
                chat_state: chatState,
                overwrite: true,
            }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || !payload.success) {
            throw new Error(payload.message || `HTTP ${response.status}`);
        }

        const capture = payload.capture || {};
        updateTemplateCaptureOutcome({
            tone: "success",
            pill: "已完成",
            title: "本机模板采集完成",
            message: safeText(payload.message, "现在可以回到上面的模板状态区确认结果。"),
            results: Array.isArray(capture.saved_templates) ? capture.saved_templates : [],
        });
        await fetchHealth();
    } catch (error) {
        const message = error instanceof Error
            ? error.message
            : "自动采集本机模板失败，请先检查微信窗口和当前聊天页状态。";
        updateTemplateCaptureOutcome({
            tone: "danger",
            pill: "失败",
            title: "本机模板采集失败",
            message,
            results: [],
        });
    } finally {
        healthState.templateCapture.busy = false;
        if (healthState.lastReport) {
            renderTemplateCapture(healthState.lastReport);
        }
    }
}

function bindEvents() {
    $("health-refresh-btn").addEventListener("click", () => fetchHealth());
    $("health-export-btn").addEventListener("click", () => exportSupportBundle());
    $("template-capture-open-btn").addEventListener("click", () => runTemplateCapture("open"));
    $("template-capture-visible-btn").addEventListener("click", () => runTemplateCapture("visible"));
}

function startPolling() {
    if (healthState.pollHandle) {
        clearInterval(healthState.pollHandle);
    }
    healthState.pollHandle = window.setInterval(() => {
        fetchHealth();
    }, 10000);
}

document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    fetchHealth();
    startPolling();
});
