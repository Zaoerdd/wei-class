const state = {
    pollHandle: null,
    qrInstance: null,
    isFetching: false,
    lastStatus: null,
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

function truncate(text, length = 58) {
    if (!text) {
        return "-";
    }
    return text.length > length ? `${text.slice(0, length)}...` : text;
}

function extractOpenId(rawValue) {
    const raw = (rawValue || "").trim();
    if (!raw) {
        return "";
    }

    const directMatch = raw.match(/\b[a-fA-F0-9]{32}\b/);
    if (directMatch) {
        return directMatch[0];
    }

    const urlMatch = raw.match(/[?&]openid=([a-fA-F0-9]{32})/i);
    if (urlMatch) {
        return urlMatch[1];
    }

    return "";
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

function signTypeLabel(item) {
    if (!item) {
        return "等待任务";
    }
    if (item.isQR) {
        return "二维码签到";
    }
    if (item.isGPS) {
        return "GPS 签到";
    }
    return "普通签到";
}

function getUiTone(status) {
    const openidStatus = status.openid_status || {};
    const resultMeta = status.result_meta || {};

    if (resultMeta.is_error) {
        return {
            tone: "danger",
            pill: "需要处理",
            title: "签到流程出现异常",
            description: safeText(resultMeta.frontend_message || openidStatus.last_error, "请检查微信状态，或改用手动 OpenID。"),
        };
    }

    if (resultMeta.sign_completed) {
        return {
            tone: "success",
            pill: "已完成",
            title: "签到任务已完成",
            description: safeText(resultMeta.frontend_message, "本次签到已经提交完成。"),
        };
    }

    if (resultMeta.qr_ready) {
        return {
            tone: "running",
            pill: "等待扫码",
            title: "二维码已准备好",
            description: "请直接使用右侧二维码完成签到。",
        };
    }

    if (status.active_sign_count > 0) {
        return {
            tone: "warning",
            pill: "处理中",
            title: `检测到 ${status.active_sign_count} 个签到任务`,
            description: "系统正在自动处理当前任务，请保持页面打开。",
        };
    }

    if (openidStatus.is_refreshing) {
        return {
            tone: "running",
            pill: "获取中",
            title: "正在自动获取 OpenID",
            description: "请保持微信和微助教页面可访问，获取成功后会自动进入监听。",
        };
    }

    if (openidStatus.openid) {
        return {
            tone: "success",
            pill: "监听中",
            title: "已进入自动监听",
            description: "OpenID 已就绪，系统会继续等待新的签到任务。",
        };
    }

    if (openidStatus.last_error) {
        return {
            tone: "danger",
            pill: "自动获取失败",
            title: "自动获取 OpenID 未成功",
            description: "你可以检查微信窗口状态，或者在下方直接手动输入 OpenID。",
        };
    }

    return {
        tone: "idle",
        pill: "准备中",
        title: "等待自动化服务就绪",
        description: "服务启动后会自动尝试获取 OpenID，并持续监听签到状态。",
    };
}

function renderBanner(status) {
    const banner = $("notice-banner");
    const openidStatus = status.openid_status || {};
    const resultMeta = status.result_meta || {};
    let message = "";
    let toneClass = "";

    if (resultMeta.is_error) {
        message = safeText(resultMeta.frontend_message, "当前任务处理失败，请检查微信状态或重新刷新。");
        toneClass = "notice-banner--danger";
    } else if (openidStatus.last_error) {
        message = "自动获取 OpenID 失败，建议检查微信窗口或使用页面下方的手动输入。";
        toneClass = "notice-banner--warning";
    } else if (resultMeta.qr_ready) {
        message = "二维码已经准备好，直接在结果面板扫码即可。";
        toneClass = "notice-banner--warning";
    }

    if (!message) {
        banner.className = "notice-banner hidden";
        banner.textContent = "";
        return;
    }

    banner.className = `notice-banner ${toneClass}`;
    banner.textContent = message;
}

function renderHero(status) {
    const openidStatus = status.openid_status || {};
    const tone = getUiTone(status);

    $("overall-pill").className = `state-pill state-pill--${tone.tone}`;
    $("overall-pill").textContent = tone.pill;
    $("overall-title").textContent = tone.title;
    $("overall-description").textContent = tone.description;
    $("collector-method").textContent = collectorLabel(openidStatus.collector_method);
    $("openid-source").textContent = sourceLabel(openidStatus.current_source);
    $("refresh-time").textContent = formatTime(openidStatus.last_refresh_at);
}

function renderOverview(status) {
    const openidStatus = status.openid_status || {};
    const resultMeta = status.result_meta || {};
    const profile = status.profile || openidStatus.profile || {};
    const currentSign = status.current_sign || null;

    $("openid-summary").textContent = openidStatus.openid_masked || "等待自动获取";
    $("openid-note").textContent = openidStatus.openid
        ? `最近更新时间：${formatTime(openidStatus.last_refresh_at)}`
        : "自动获取失败时，可以手动粘贴 OpenID 或完整链接。";

    const profileParts = [profile.name, profile.class_name, profile.student_number].filter(Boolean);
    $("profile-summary").textContent = profileParts.length ? profileParts.join(" / ") : "尚未识别";
    $("profile-note").textContent = profile.college_name || profile.department_name || "登录后会自动识别学生信息。";

    $("task-summary").textContent = currentSign
        ? `${currentSign.name || "未命名任务"} · ${signTypeLabel(currentSign)}`
        : "暂无任务";
    $("task-note").textContent = status.active_sign_count > 0
        ? `当前共有 ${status.active_sign_count} 个任务待处理`
        : "发现任务后会自动刷新到这里。";

    if (resultMeta.sign_completed) {
        $("result-summary").textContent = "签到完成";
        $("result-note").textContent = safeText(resultMeta.frontend_message, "本次签到已经成功提交。");
    } else if (resultMeta.qr_ready) {
        $("result-summary").textContent = "二维码已就绪";
        $("result-note").textContent = "请在结果面板直接扫码完成签到。";
    } else if (resultMeta.is_error) {
        $("result-summary").textContent = "处理异常";
        $("result-note").textContent = safeText(resultMeta.frontend_message, "请刷新或重新获取 OpenID。");
    } else {
        $("result-summary").textContent = "等待中";
        $("result-note").textContent = "二维码、自动签到和异常提示都会集中展示。";
    }
}

function renderTimeline(status) {
    const openidStatus = status.openid_status || {};
    const resultMeta = status.result_meta || {};
    const steps = [
        {
            title: "获取 OpenID",
            description: openidStatus.openid ? "当前 OpenID 已经就绪。" : "系统正在自动获取或等待你手动输入。",
            done: Boolean(openidStatus.openid),
            active: Boolean(openidStatus.is_refreshing),
        },
        {
            title: "进入监听",
            description: openidStatus.openid ? "已进入自动监听状态。" : "需要先拿到有效 OpenID。",
            done: Boolean(openidStatus.openid),
            active: Boolean(openidStatus.openid && status.active_sign_count === 0),
        },
        {
            title: "发现签到",
            description: status.active_sign_count > 0 ? `当前发现 ${status.active_sign_count} 个任务。` : "还没有检测到新的签到任务。",
            done: status.active_sign_count > 0,
            active: status.active_sign_count > 0 && !resultMeta.result_ready,
        },
        {
            title: "输出结果",
            description: resultMeta.sign_completed
                ? "签到已完成。"
                : resultMeta.qr_ready
                    ? "二维码已生成。"
                    : resultMeta.is_error
                        ? "流程遇到异常。"
                        : "等待生成结果。",
            done: Boolean(resultMeta.result_ready),
            active: Boolean(resultMeta.result_ready && !resultMeta.sign_completed && !resultMeta.is_error),
        },
    ];

    $("timeline").innerHTML = steps.map((step, index) => {
        const classNames = ["timeline-step"];
        if (step.done) {
            classNames.push("is-done");
        }
        if (step.active) {
            classNames.push("is-active");
        }
        return `
            <article class="${classNames.join(" ")}">
                <span class="timeline-index">${index + 1}</span>
                <strong>${step.title}</strong>
                <p>${step.description}</p>
            </article>
        `;
    }).join("");
}

function buildSignMeta(item, resultMeta, isCurrent) {
    const tags = [];
    tags.push(`<span class="tag tag--accent">${signTypeLabel(item)}</span>`);
    if (isCurrent) {
        tags.push('<span class="tag">当前展示</span>');
    }
    if (resultMeta?.sign_completed) {
        tags.push('<span class="tag tag--accent">已完成</span>');
    } else if (resultMeta?.qr_ready) {
        tags.push('<span class="tag tag--warning">等待扫码</span>');
    } else if (resultMeta?.is_error) {
        tags.push('<span class="tag tag--danger">处理异常</span>');
    } else {
        tags.push('<span class="tag">处理中</span>');
    }
    return tags.join("");
}

function renderActiveSigns(status) {
    const signsContainer = $("active-signs");
    const taskBadge = $("task-count-badge");
    const activeSigns = Array.isArray(status.active_signs) ? status.active_signs : [];
    const currentSign = status.current_sign || null;
    const resultMeta = status.result_meta || {};

    taskBadge.textContent = `${activeSigns.length} 个任务`;

    if (!activeSigns.length) {
        signsContainer.innerHTML = `
            <div class="empty-state">
                <strong>当前没有签到任务</strong>
                <p>系统会持续轮询签到状态，一旦检测到新任务会自动显示在这里。</p>
            </div>
        `;
        return;
    }

    signsContainer.innerHTML = activeSigns.map((item) => {
        const isCurrent = currentSign && item.courseId === currentSign.courseId && item.signId === currentSign.signId;
        const cardClass = isCurrent ? "sign-card sign-card--current" : "sign-card";
        const relatedMeta = isCurrent ? resultMeta : null;
        return `
            <article class="${cardClass}">
                <div class="card-topline">
                    <div>
                        <h3 class="card-title">${item.name || "未命名任务"}</h3>
                        <p class="card-subtitle">${item.courseName || "未提供课程名"}${item.teacherName ? ` · ${item.teacherName}` : ""}</p>
                    </div>
                    <div class="tag-row">${buildSignMeta(item, relatedMeta, isCurrent)}</div>
                </div>
                <div class="meta-grid">
                    <span class="meta-chip">courseId: ${item.courseId ?? "-"}</span>
                    <span class="meta-chip">signId: ${item.signId ?? "-"}</span>
                    ${item.startTime ? `<span class="meta-chip">开始: ${item.startTime}</span>` : ""}
                    ${item.endTime ? `<span class="meta-chip">结束: ${item.endTime}</span>` : ""}
                </div>
            </article>
        `;
    }).join("");
}

function clearQrCode() {
    const qrNode = $("qrcode");
    if (qrNode) {
        qrNode.innerHTML = "";
    }
    state.qrInstance = null;
}

function renderQrCode(url) {
    clearQrCode();
    if (!url) {
        return;
    }
    const qrNode = $("qrcode");
    if (!qrNode || typeof QRCode === "undefined") {
        return;
    }
    state.qrInstance = new QRCode(qrNode, {
        text: url,
        width: 220,
        height: 220,
        correctLevel: QRCode.CorrectLevel.H,
    });
}

function showFeedback(message, type = "") {
    const feedback = $("manual-feedback");
    feedback.textContent = message || "";
    feedback.className = "form-feedback";
    if (type === "error") {
        feedback.classList.add("is-error");
    }
    if (type === "success") {
        feedback.classList.add("is-success");
    }
}

function renderResult(status) {
    const resultPanel = $("result-panel");
    const resultMeta = status.result_meta || {};
    const qrUrl = resultMeta.qr_url || status.qr_url || "";
    const currentSign = status.current_sign || null;

    const title = resultMeta.sign_completed
        ? "签到已经完成"
        : resultMeta.qr_ready
            ? "二维码已经生成"
            : resultMeta.is_error
                ? "任务处理出现异常"
                : currentSign
                    ? "任务正在处理中"
                    : "等待新任务";

    const description = resultMeta.sign_completed
        ? safeText(resultMeta.frontend_message, "本次签到已经处理完成。")
        : resultMeta.qr_ready
            ? "请直接使用下面的二维码扫码签到，也可以复制链接在其他设备打开。"
            : resultMeta.is_error
                ? safeText(resultMeta.frontend_message, "请刷新页面，必要时重新获取 OpenID。")
                : currentSign
                    ? "任务已经识别到，系统正在等待二维码或自动提交结果。"
                    : "还没有可展示的二维码或结果。";

    resultPanel.innerHTML = `
        <div class="result-card">
            <div>
                <h3>${title}</h3>
                <p>${description}</p>
            </div>
            <div class="qr-shell ${qrUrl ? "" : "hidden"}">
                <div class="qr-frame">
                    <div id="qrcode"></div>
                </div>
                <div class="qr-link">${qrUrl || "-"}</div>
                <button class="btn btn-subtle" type="button" id="copy-qr-btn">复制二维码链接</button>
            </div>
            <div class="meta-grid">
                ${currentSign ? `<span class="meta-chip">${signTypeLabel(currentSign)}</span>` : ""}
                ${resultMeta.sign_rank ? `<span class="meta-chip">签到名次: ${resultMeta.sign_rank}</span>` : ""}
                ${resultMeta.task_name ? `<span class="meta-chip">任务: ${resultMeta.task_name}</span>` : ""}
            </div>
        </div>
    `;

    if (qrUrl) {
        renderQrCode(qrUrl);
        const copyBtn = $("copy-qr-btn");
        if (copyBtn) {
            copyBtn.addEventListener("click", async () => {
                try {
                    await navigator.clipboard.writeText(qrUrl);
                    showFeedback("二维码链接已复制。", "success");
                } catch (error) {
                    showFeedback("复制失败，请手动复制下方链接。", "error");
                }
            });
        }
    } else {
        clearQrCode();
    }
}

function renderRuntime(status) {
    const openidStatus = status.openid_status || {};
    const resultMeta = status.result_meta || {};
    const details = [
        ["当前 OpenID", openidStatus.openid_masked || "未获取"],
        ["来源", sourceLabel(openidStatus.current_source)],
        ["获取方式", collectorLabel(openidStatus.collector_method)],
        ["上次刷新", formatTime(openidStatus.last_refresh_at)],
        ["下次刷新", formatTime(openidStatus.next_refresh_at)],
        ["缓存回退", openidStatus.used_file_fallback ? "是" : "否"],
        ["当前链接", truncate(openidStatus.current_url || "", 72)],
        ["结果消息", safeText(resultMeta.frontend_message || status.message, "等待结果")],
    ];

    if (openidStatus.last_error) {
        details.push(["最近错误", safeText(openidStatus.last_error, "请检查微信窗口或改用手动输入")]);
    }

    $("runtime-details").innerHTML = details.map(([label, value]) => `
        <div class="detail-row">
            <dt>${label}</dt>
            <dd>${value || "-"}</dd>
        </div>
    `).join("");
}

function renderStudents(status) {
    const container = $("signed-students");
    const resultMeta = status.result_meta || {};
    const students = Array.isArray(resultMeta.signed_students) ? resultMeta.signed_students : [];

    if (!students.length) {
        container.innerHTML = `
            <div class="empty-state">
                <strong>暂无签到成员数据</strong>
                <p>如果当前任务支持返回签到名单，这里会自动按顺序显示。</p>
            </div>
        `;
        return;
    }

    container.innerHTML = students.map((student, index) => `
        <article class="student-card">
            <div class="student-main">
                <strong>${student.name || "未命名成员"}</strong>
                <span>${student.student_number || "无学号"}${student.distance != null ? ` · ${student.distance}m` : ""}</span>
            </div>
            <div class="student-rank">${student.rank || index + 1}</div>
        </article>
    `).join("");
}

function updateLogoutVisibility(status) {
    const logoutButton = $("logout-btn");
    const openidStatus = status.openid_status || {};
    if (openidStatus.openid) {
        logoutButton.classList.remove("hidden");
    } else {
        logoutButton.classList.add("hidden");
    }
}

function renderStatus(status) {
    state.lastStatus = status;
    renderBanner(status);
    renderHero(status);
    renderOverview(status);
    renderTimeline(status);
    renderActiveSigns(status);
    renderResult(status);
    renderRuntime(status);
    renderStudents(status);
    updateLogoutVisibility(status);
}

async function fetchStatus(showLoadingFeedback = false) {
    if (state.isFetching) {
        return;
    }
    state.isFetching = true;

    if (showLoadingFeedback) {
        showFeedback("正在刷新状态...");
    }

    try {
        const response = await fetch("/qr_code", { cache: "no-store" });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        const status = await response.json();
        renderStatus(status);
        if (showLoadingFeedback) {
            showFeedback("");
        }
    } catch (error) {
        showFeedback("无法连接本地服务，请确认已运行一键启动脚本。", "error");
    } finally {
        state.isFetching = false;
    }
}

async function submitManualOpenId(event) {
    event.preventDefault();
    const rawValue = $("openid-input").value;
    const openid = extractOpenId(rawValue);

    if (!openid) {
        showFeedback("请输入 32 位 OpenID，或粘贴带 openid= 的完整链接。", "error");
        return;
    }

    showFeedback("正在启用这个 OpenID...");

    try {
        const response = await fetch("/api/login", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({ openid }),
        });
        const payload = await response.json();
        if (!response.ok || !payload.success) {
            throw new Error(payload.message || "manual login failed");
        }
        $("openid-input").value = openid;
        showFeedback("OpenID 已启用，页面正在刷新状态。", "success");
        await fetchStatus();
    } catch (error) {
        showFeedback("这个 OpenID 无法使用，可能已失效，请重新获取后再试。", "error");
    }
}

async function logoutCurrentSession() {
    showFeedback("正在停止当前监听...");
    try {
        const response = await fetch("/api/logout", {
            method: "POST",
        });
        if (!response.ok) {
            throw new Error("logout failed");
        }
        showFeedback("已停止当前监听，系统会重新等待可用 OpenID。", "success");
        await fetchStatus();
    } catch (error) {
        showFeedback("停止监听失败，请稍后重试。", "error");
    }
}

function bindEvents() {
    $("refresh-btn").addEventListener("click", () => fetchStatus(true));
    $("manual-form").addEventListener("submit", submitManualOpenId);
    $("logout-btn").addEventListener("click", logoutCurrentSession);
}

function startPolling() {
    if (state.pollHandle) {
        clearInterval(state.pollHandle);
    }
    state.pollHandle = window.setInterval(() => {
        fetchStatus(false);
    }, 3000);
}

document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    fetchStatus(true);
    startPolling();
});
