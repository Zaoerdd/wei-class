const POLL_MS = 3000;
const POLL_MAX_MS = 30000;
const HISTORY_REFRESH_MS = 30000;

const state = {
    pollHandle: null,
    qrInstance: null,
    isFetching: false,
    lastStatus: null,
    lastStatusJson: "",
    lastHistoryJson: "",
    lastHistoryAt: 0,
    pollFailures: 0,
    delayLoaded: false,
    controlBusy: false,
};

const mojibakePattern = /[�锛鎴姝鏈褰娌鑾彇鍒閿辫缃粶璇]/;

function $(id) {
    return document.getElementById(id);
}

function qrAutoLabel(meta) {
    return ({queued: "等待自动打开", sending: "正在自动打开", confirming: "确认签到中",
        manual: "需要手动处理", cancelled: "自动处理已停止"})[meta.qr_auto_state] || "";
}

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[char]);
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
        return "微信图像识别";
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
        return "本机缓存";
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

    if (openidStatus.listening_enabled === false) {
        return {tone: "idle", pill: "已暂停", title: "自动签到已暂停",
            description: openidStatus.is_refreshing ? "正在重新识别登录，完成后仍然保持暂停。" : "点上面的按钮继续；暂停期间不会处理新签到。"};
    }

    if (resultMeta.is_error) {
        return {
            tone: "danger",
            pill: "需要处理",
            title: "这次没处理成功",
            description: safeText(resultMeta.frontend_message || openidStatus.last_error, "往下面“先检查一遍”看一眼，或者用备用方式手动登录。"),
        };
    }

    if (resultMeta.sign_completed) {
        return {
            tone: "success",
            pill: "已完成",
            title: "这次签到完成了",
            description: safeText(resultMeta.frontend_message, "已经提交成功。"),
        };
    }

    if (resultMeta.qr_ready) {
        return {
            tone: "running",
            pill: qrAutoLabel(resultMeta) || "等待扫码",
            title: qrAutoLabel(resultMeta) || "二维码好了",
            description: resultMeta.qr_auto_state ? safeText(resultMeta.frontend_message) : "用右边的二维码扫一下就能签。",
        };
    }

    if (status.active_sign_count > 0) {
        return {
            tone: "warning",
            pill: "处理中",
            title: `有 ${status.active_sign_count} 个签到要处理`,
            description: "正在自动处理，先别关这个页面。",
        };
    }

    if (openidStatus.is_refreshing) {
        return {
            tone: "running",
            pill: "识别中",
            title: "正在识别你的微信登录",
            description: "会自己切到微信找微助教，几秒就好，先别关微信。",
        };
    }

    if (openidStatus.has_openid) {
        return {
            tone: "success",
            pill: "在等签到",
            title: "正在帮你盯着",
            description: openidStatus.qr_auto_enabled ? "遇到二维码签到会自动打开，并发到文件传输助手。" : "有签到会立刻处理，不用一直看着。",
        };
    }

    if (openidStatus.last_error) {
        return {
            tone: "danger",
            pill: "没识别成功",
            title: "登录没识别出来",
            description: openidStatus.refresh_failures && openidStatus.next_refresh_at
                ? `系统会在 ${formatTime(openidStatus.next_refresh_at)} 再试一次；期间会自动切到微信搜索微助教。`
                : "先看看微信有没有登录，或者用下面的备用方式粘贴链接。",
        };
    }

    return {
        tone: "idle",
        pill: "准备中",
        title: "正在准备",
        description: "马上就好，先别关页面。",
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
        message = openidStatus.listening_enabled === false
            ? "登录没识别成功，现在仍是暂停状态。看看微信有没有登录，再点上面的“重新识别登录”。"
            : openidStatus.has_openid
                ? "这次没识别成功，先用着之前的登录；万一失效会自动再试。"
                : "登录没识别成功，系统会自动再试；也可以看看微信再点“重新识别登录”。";
        toneClass = "notice-banner--warning";
    } else if (resultMeta.qr_ready && !resultMeta.sign_completed) {
        message = resultMeta.qr_auto_state ? safeText(resultMeta.frontend_message) : "二维码已经准备好，直接在结果面板扫码即可。";
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
    $("refresh-time").textContent = formatTime(openidStatus.last_refresh_at);
}

function renderOverview(status) {
    const openidStatus = status.openid_status || {};
    const resultMeta = status.result_meta || {};
    const profile = status.profile || openidStatus.profile || {};
    const currentSign = status.current_sign || null;

    $("openid-summary").textContent = openidStatus.has_openid ? "已登录" : "还没登录";
    $("openid-note").textContent = openidStatus.has_openid
        ? `最近更新：${formatTime(openidStatus.last_refresh_at)}`
        : "自动识别你的账号，不用手动填。";

    const profileParts = [profile.name, profile.class_name, profile.student_number].filter(Boolean);
    $("profile-summary").textContent = profileParts.length ? profileParts.join(" / ") : "还没识别";
    $("profile-note").textContent = profile.college_name || profile.department_name || "登录后会自动识别学生信息。";

    $("task-summary").textContent = currentSign
        ? `${currentSign.name || "未命名任务"} · ${signTypeLabel(currentSign)}`
        : "暂无任务";
    $("task-note").textContent = status.active_sign_count > 0
        ? `当前共有 ${status.active_sign_count} 个任务待处理`
        : "有任务会自动出现在这里。";

    if (resultMeta.sign_completed) {
        $("result-summary").textContent = "签到完成";
        $("result-note").textContent = safeText(resultMeta.frontend_message, "本次签到已经成功提交。");
    } else if (resultMeta.qr_ready) {
        $("result-summary").textContent = qrAutoLabel(resultMeta) || "二维码已就绪";
        $("result-note").textContent = resultMeta.qr_auto_state ? safeText(resultMeta.frontend_message) : "请在结果面板直接扫码完成签到。";
    } else if (resultMeta.is_error) {
        $("result-summary").textContent = "处理异常";
        $("result-note").textContent = safeText(resultMeta.frontend_message, "刷新一下，或者点“重新识别登录”。");
    } else {
        $("result-summary").textContent = "等待中";
        $("result-note").textContent = "二维码和签到结果都在这里看。";
    }
}

function renderTimeline(status) {
    const openidStatus = status.openid_status || {};
    const resultMeta = status.result_meta || {};
    const steps = [
        {
            title: "识别登录",
            description: openidStatus.has_openid ? "已经认出你的账号。" : "正在识别，或者等你手动粘贴链接。",
            done: Boolean(openidStatus.has_openid),
            active: Boolean(openidStatus.is_refreshing),
        },
        {
            title: "开始等待",
            description: status.is_running ? "已经在盯着了。" : "还没开始。",
            done: Boolean(status.is_running),
            active: Boolean(status.is_running && status.active_sign_count === 0),
        },
        {
            title: "发现签到",
            description: status.active_sign_count > 0 ? `现在有 ${status.active_sign_count} 个任务。` : "还没遇到新的签到。",
            done: status.active_sign_count > 0,
            active: status.active_sign_count > 0 && !resultMeta.result_ready,
        },
        {
            title: "处理完成",
            description: resultMeta.sign_completed
                ? "签到已完成。"
                : resultMeta.qr_ready
                    ? "二维码已经生成。"
                    : resultMeta.is_error
                        ? "这一步出错了。"
                        : "还在等结果。",
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
                <strong>${escapeHtml(step.title)}</strong>
                <p>${escapeHtml(step.description)}</p>
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
        tags.push(`<span class="tag tag--warning">${qrAutoLabel(resultMeta) || "等待扫码"}</span>`);
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
                <strong>现在没有签到</strong>
                <p>${status.openid_status?.listening_enabled === false ? "现在是暂停状态，点上面的按钮才会继续检测。" : "有新任务会自动显示在这里。"}</p>
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
                        <h3 class="card-title">${escapeHtml(item.name || "未命名任务")}</h3>
                        <p class="card-subtitle">${escapeHtml(item.courseName || "未提供课程名")}${item.teacherName ? ` · ${escapeHtml(item.teacherName)}` : ""}</p>
                    </div>
                    <div class="tag-row">${buildSignMeta(item, relatedMeta, isCurrent)}</div>
                </div>
                <div class="meta-grid">
                    <span class="meta-chip">courseId: ${escapeHtml(item.courseId ?? "-")}</span>
                    <span class="meta-chip">signId: ${escapeHtml(item.signId ?? "-")}</span>
                    ${item.startTime ? `<span class="meta-chip">开始: ${escapeHtml(item.startTime)}</span>` : ""}
                    ${item.endTime ? `<span class="meta-chip">结束: ${escapeHtml(item.endTime)}</span>` : ""}
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
            ? (qrAutoLabel(resultMeta) || "二维码已经生成")
            : resultMeta.is_error
                ? "任务处理出现异常"
                : currentSign
                    ? "任务正在处理中"
                    : "等着就行";

    const description = resultMeta.sign_completed
        ? safeText(resultMeta.frontend_message, "本次签到已经处理完成。")
        : resultMeta.qr_ready
            ? (resultMeta.qr_auto_state ? safeText(resultMeta.frontend_message) : "请直接使用下面的二维码扫码签到，也可以复制链接在其他设备打开。")
            : resultMeta.is_error
                ? safeText(resultMeta.frontend_message, "刷新一下页面；还不行就重新识别登录。")
                : currentSign
                    ? "任务已经识别到，系统正在等待二维码或自动提交结果。"
                    : "有签到会自动出现在这里。";

    resultPanel.innerHTML = `
        <div class="result-card">
            <div>
                <h3>${escapeHtml(title)}</h3>
                <p>${escapeHtml(description)}</p>
            </div>
            <div class="qr-shell ${qrUrl ? "" : "hidden"}">
                <div class="qr-frame">
                    <div id="qrcode"></div>
                </div>
                <div class="qr-link">${escapeHtml(qrUrl || "-")}</div>
                <button class="btn btn-subtle" type="button" id="copy-qr-btn">复制二维码链接</button>
            </div>
            <div class="meta-grid">
                ${currentSign ? `<span class="meta-chip">${signTypeLabel(currentSign)}</span>` : ""}
                ${resultMeta.sign_rank ? `<span class="meta-chip">签到名次: ${escapeHtml(resultMeta.sign_rank)}</span>` : ""}
                ${resultMeta.task_name ? `<span class="meta-chip">任务: ${escapeHtml(resultMeta.task_name)}</span>` : ""}
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
        ["自动获取", openidStatus.listening_enabled === false
            ? "已暂停"
            : openidStatus.next_refresh_at ? `${formatTime(openidStatus.next_refresh_at)} 重试` : "OpenID 失效时自动获取"],
        ["使用缓存", openidStatus.used_file_fallback ? "是" : "否"],
        ["当前链接", truncate(openidStatus.current_url || "", 72)],
        ["结果消息", safeText(resultMeta.frontend_message || status.message, "等待结果")],
    ];

    if (openidStatus.last_error) {
        details.push(["最近错误", safeText(openidStatus.last_error, "请检查微信窗口或改用手动输入")]);
    }

    $("runtime-details").innerHTML = details.map(([label, value]) => `
        <div class="detail-row">
            <dt>${escapeHtml(label)}</dt>
            <dd>${escapeHtml(value || "-")}</dd>
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
                <strong>${escapeHtml(student.name || "未命名成员")}</strong>
                <span>${escapeHtml(student.student_number || "无学号")}${student.distance != null ? ` · ${escapeHtml(student.distance)}m` : ""}</span>
            </div>
            <div class="student-rank">${escapeHtml(student.rank || index + 1)}</div>
        </article>
    `).join("");
}

function updateLogoutVisibility(status) {
    const logoutButton = $("logout-btn");
    const openidStatus = status.openid_status || {};
    if (openidStatus.has_openid) {
        logoutButton.classList.remove("hidden");
    } else {
        logoutButton.classList.add("hidden");
    }
}

function renderStatus(status) {
    const refreshFinished = state.lastStatus?.openid_status?.is_refreshing && !status.openid_status?.is_refreshing;
    state.lastStatus = status;
    if (refreshFinished) {
        $("control-feedback").textContent = status.openid_status.last_error
            ? "获取未成功，请查看下方运行信息中的错误原因。"
            : (status.openid_status.listening_enabled ? "登录已更新，继续帮你盯着。" : "登录已更新，但现在是暂停状态。");
    }
    renderBanner(status);
    renderHero(status);
    renderOverview(status);
    renderTimeline(status);
    renderActiveSigns(status);
    renderResult(status);
    renderRuntime(status);
    renderStudents(status);
    updateLogoutVisibility(status);
    renderSignDelay(status);
    renderControls();
}

function renderControls() {
    const status = state.lastStatus?.openid_status;
    const toggle = $("listen-toggle-btn");
    toggle.textContent = status?.listening_enabled ? "暂停自动签到" : "开始自动签到";
    toggle.disabled = !status || state.controlBusy;
    $("openid-refresh-btn").textContent = status?.is_refreshing ? "正在识别…" : "重新识别登录";
    $("openid-refresh-btn").disabled = !status || state.controlBusy || status.is_refreshing;
}

async function runControl(path, body) {
    if (state.controlBusy) return;
    state.controlBusy = true;
    renderControls();
    const feedback = $("control-feedback");
    feedback.textContent = "正在执行…";
    try {
        const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.message || "操作未完成");
        feedback.textContent = payload.message;
        await fetchStatus();
    } catch (error) {
        feedback.textContent = error.message || "无法连接服务，请稍后重试";
    } finally {
        state.controlBusy = false;
        renderControls();
    }
}

async function fetchHistory() {
    const container = $("sign-history");
    try {
        const response = await fetch("/api/sign_history", {cache: "no-store"});
        if (!response.ok) throw new Error("history unavailable");
        const text = await response.text();
        state.lastHistoryAt = Date.now();
        if (text === state.lastHistoryJson) return;
        state.lastHistoryJson = text;
        const history = JSON.parse(text);
        $("history-count").textContent = `${history.total} 条`;
        container.innerHTML = history.records.length ? history.records.map(row => `
            <article class="sign-card">
                <div class="card-topline"><div>
                    <h3 class="card-title">${escapeHtml(row.course_name || row.task_name || "未命名签到")}</h3>
                    <p class="card-subtitle">${escapeHtml(row.task_name || "")} · ${escapeHtml(row.student_name || "本人")}</p>
                </div><span class="tag tag--accent">签到成功</span></div>
                <div class="meta-grid">
                    <span class="meta-chip">确认时间：${escapeHtml(formatTime(row.confirmed_at))}</span>
                    <span class="meta-chip">${escapeHtml(row.sign_type || "签到")}</span>
                    ${row.sign_rank ? `<span class="meta-chip">第 ${escapeHtml(row.sign_rank)} 名</span>` : ""}
                </div><p>${escapeHtml(row.message || "已确认成功")}</p>
            </article>`).join("") : '<div class="empty-state"><strong>暂无成功记录</strong><p>从现在起，本工具确认成功的签到会自动保存在这里。</p></div>';
    } catch (error) {
        container.textContent = "成功记录暂时无法读取，下次刷新时会重试。";
    }
}

async function fetchStatus(showLoadingFeedback = false, force = false) {
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
        const text = await response.text();
        const status = JSON.parse(text);
        // Redraw only on change: a full redraw every poll makes the QR code flicker,
        // drops focus and keeps screen readers re-announcing the page.
        const changed = force || text !== state.lastStatusJson;
        const wasCompleted = Boolean(state.lastStatus?.result_meta?.sign_completed);
        if (changed) {
            state.lastStatusJson = text;
            renderStatus(status);
        }
        const justCompleted = !wasCompleted && Boolean(status.result_meta?.sign_completed);
        if (force || justCompleted || Date.now() - state.lastHistoryAt >= HISTORY_REFRESH_MS) {
            await fetchHistory();
        }
        state.pollFailures = 0;
        if (showLoadingFeedback) {
            showFeedback("");
        }
    } catch (error) {
        state.pollFailures = Math.min(state.pollFailures + 1, 4);
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
        showFeedback("请粘贴微助教链接（也支持 32 位 OpenID）。", "error");
        return;
    }

    showFeedback("正在用这个链接登录...");

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
        showFeedback("登录已切换，正在刷新状态。", "success");
        await fetchStatus();
    } catch (error) {
        showFeedback("这个登录用不了，可能已经过期。重新识别一次再试。", "error");
    }
}

async function logoutCurrentSession() {
    showFeedback("正在暂停...");
    try {
        const response = await fetch("/api/logout", {
            method: "POST",
        });
        if (!response.ok) {
            throw new Error("logout failed");
        }
        showFeedback("已退出并暂停。点上面的按钮才会继续。", "success");
        await fetchStatus();
    } catch (error) {
        showFeedback("暂停失败，稍后再试一次。", "error");
    }
}

function renderSignDelay(status) {
    // Fill the inputs once from the server; never overwrite what the user is typing.
    const delay = status.openid_status?.sign_delay;
    if (!delay || state.delayLoaded) {
        return;
    }
    $("delay-min").value = delay.min;
    $("delay-max").value = delay.max;
    state.delayLoaded = true;
}

async function saveSignDelay(event) {
    event.preventDefault();
    const feedback = $("delay-feedback");
    const minimum = Number($("delay-min").value || 0);
    const maximum = Number($("delay-max").value || 0);
    try {
        const response = await fetch("/api/sign_delay", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({min: minimum, max: maximum}),
        });
        const payload = await response.json();
        feedback.textContent = payload.message || (response.ok ? "已保存" : "保存失败");
        feedback.className = `form-feedback ${response.ok && payload.success ? "is-success" : "is-error"}`;
    } catch (error) {
        feedback.textContent = "无法连接服务，请稍后重试";
        feedback.className = "form-feedback is-error";
    }
}

const HEALTH_STATUS_LABELS = {
    ok: "正常",
    warn: "注意",
    fail: "异常",
    skip: "不适用",
};

function renderHealth(report) {
    const checks = Array.isArray(report?.checks) ? report.checks : [];
    $("health-list").innerHTML = checks.map((check) => `
        <div class="health-item health-item--${escapeHtml(check.status)}">
            <span class="health-dot" aria-hidden="true"></span>
            <div class="health-body">
                <div class="health-head">
                    <strong>${escapeHtml(check.label)}</strong>
                    <span class="health-tag">${escapeHtml(HEALTH_STATUS_LABELS[check.status] || check.status)}</span>
                </div>
                <p class="health-detail">${escapeHtml(check.detail)}</p>
                ${check.hint ? `<p class="health-hint">${escapeHtml(check.hint)}</p>` : ""}
            </div>
        </div>
    `).join("");
}

async function runHealthCheck() {
    const button = $("health-btn");
    const feedback = $("health-feedback");
    button.disabled = true;
    button.textContent = "检查中…";
    feedback.textContent = "";
    feedback.className = "form-feedback";
    try {
        const response = await fetch("/api/health", {cache: "no-store"});
        const report = await response.json();
        renderHealth(report);
        const failing = report.checks.filter((check) => check.status === "fail").length;
        const warning = report.checks.filter((check) => check.status === "warn").length;
        feedback.textContent = failing
            ? `${failing} 项异常，${warning} 项需要注意`
            : warning ? `全部通过，${warning} 项需要注意` : "全部通过";
        feedback.className = `form-feedback ${failing ? "is-error" : "is-success"}`;
    } catch (error) {
        feedback.textContent = "无法连接本地服务，请确认服务是否在运行";
        feedback.className = "form-feedback is-error";
    } finally {
        button.disabled = false;
        button.textContent = "重新检查";
    }
}

function bindEvents() {
    $("health-btn").addEventListener("click", runHealthCheck);
    $("delay-form").addEventListener("submit", saveSignDelay);
    $("listen-toggle-btn").addEventListener("click", () => runControl("/api/listening", {enabled: !state.lastStatus?.openid_status?.listening_enabled}));
    $("openid-refresh-btn").addEventListener("click", () => runControl("/api/openid/refresh"));
    $("refresh-btn").addEventListener("click", () => fetchStatus(true, true));
    $("manual-form").addEventListener("submit", submitManualOpenId);
    $("logout-btn").addEventListener("click", logoutCurrentSession);
}

function schedulePoll() {
    clearTimeout(state.pollHandle);
    if (document.hidden) {
        return;  // resumes from visibilitychange
    }
    // Back off while the local service is unreachable, up to POLL_MAX_MS.
    const delay = Math.min(POLL_MAX_MS, POLL_MS * 2 ** state.pollFailures);
    state.pollHandle = window.setTimeout(async () => {
        await fetchStatus(false);
        schedulePoll();
    }, delay);
}

document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    fetchStatus(true).then(schedulePoll);
    document.addEventListener("visibilitychange", () => {
        if (document.hidden) {
            clearTimeout(state.pollHandle);
        } else {
            fetchStatus(false).then(schedulePoll);
        }
    });
});
