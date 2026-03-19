const healthState = {
    pollHandle: null,
    isFetching: false,
    lastReport: null,
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
            <dt>${label}</dt>
            <dd>${value || "-"}</dd>
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
                    <span>${safeText(fact.label, "信息")}</span>
                    <strong>${safeText(String(fact.value ?? "-"), "-")}</strong>
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
                        <p class="panel-eyebrow">${categoryLabel(check.category)}</p>
                        <h3>${safeText(check.title, "未命名检查")}</h3>
                    </div>
                    <span class="state-pill state-pill--${tone} state-pill--compact">${healthStatusLabel(check.status)}</span>
                </div>
                <p class="health-card-summary">${safeText(check.summary, "暂无摘要")}</p>
                ${check.detail ? `<p class="health-card-detail">${safeText(check.detail, "")}</p>` : ""}
                ${renderFacts(check.facts)}
                ${check.action ? `
                    <div class="health-card-action">
                        <strong>下一步</strong>
                        <p>${safeText(check.action, "按当前提示处理即可。")}</p>
                    </div>
                ` : ""}
            </article>
        `;
    }).join("");
}

function renderReport(report) {
    healthState.lastReport = report;
    renderBanner(report);
    renderHero(report);
    renderOverview(report);
    renderChecks(report);
    renderRuntime(report);
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
            <p>${message}</p>
        </div>
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

function bindEvents() {
    $("health-refresh-btn").addEventListener("click", () => fetchHealth());
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
