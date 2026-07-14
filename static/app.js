// ─── helpers ─────────────────────────────────────────────────────────
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

function toast(msg, ms = 1800) {
	let el = $("#toast");
	if (!el) {
		el = document.createElement("div");
		el.id = "toast";
		el.className = "toast";
		el.setAttribute("role", "status");
		el.setAttribute("aria-live", "polite");
		el.setAttribute("aria-atomic", "true");
		document.body.appendChild(el);
	}
	el.textContent = msg;
	el.classList.add("show");
	clearTimeout(toast._t);
	toast._t = setTimeout(() => el.classList.remove("show"), ms);
}

const FOCUSABLE_SELECTOR = [
	"a[href]",
	"button:not([disabled])",
	"input:not([disabled])",
	"select:not([disabled])",
	"textarea:not([disabled])",
	"[tabindex]:not([tabindex='-1'])",
].join(",");

const modalState = { active: null, returnFocus: null };
const authBackgroundSelector = ".topbar, .layout, body > .modal, #toast";

function openModal(modal, initialFocus) {
	if (!modal) return;
	modalState.active = modal;
	modalState.returnFocus = document.activeElement;
	modal.classList.remove("hidden");
	document.body.classList.add("modal-open");
	requestAnimationFrame(() => {
		const target = initialFocus || $(FOCUSABLE_SELECTOR, modal);
		target?.focus();
	});
}

function closeModal(modal = modalState.active) {
	if (!modal) return;
	modal.classList.add("hidden");
	if (modalState.active === modal) {
		const returnFocus = modalState.returnFocus;
		modalState.active = null;
		modalState.returnFocus = null;
		document.body.classList.remove("modal-open");
		if (returnFocus instanceof HTMLElement && returnFocus.isConnected) {
			returnFocus.focus();
		}
	}
}

function trapTabKey(event, container) {
	if (event.key !== "Tab") return false;
	const focusable = $$(FOCUSABLE_SELECTOR, container).filter((el) => el.offsetParent !== null);
	if (!focusable.length) {
		event.preventDefault();
		return true;
	}
	const first = focusable[0];
	const last = focusable[focusable.length - 1];
	if (!container.contains(document.activeElement)) {
		event.preventDefault();
		(event.shiftKey ? last : first).focus();
	} else if (event.shiftKey && document.activeElement === first) {
		event.preventDefault();
		last.focus();
	} else if (!event.shiftKey && document.activeElement === last) {
		event.preventDefault();
		first.focus();
	}
	return true;
}

document.addEventListener("keydown", (event) => {
	const authOverlay = $("#auth-overlay");
	if (authOverlay && !authOverlay.classList.contains("hidden")) {
		trapTabKey(event, authOverlay);
		return;
	}
	const modal = modalState.active;
	if (!modal || modal.classList.contains("hidden")) return;
	if (event.key === "Escape") {
		event.preventDefault();
		closeModal(modal);
		return;
	}
	trapTabKey(event, modal);
});

function getAdminKey() {
	return sessionStorage.getItem("admin_key") || "";
}

async function httpError(response) {
	const raw = await response.text().catch(() => "");
	let message = raw;
	try {
		const parsed = JSON.parse(raw);
		message = parsed.detail || parsed.message || raw;
	} catch (e) {}
	return new Error(`${response.status}: ${String(message).slice(0, 240)}`);
}

async function api(method, path, body) {
	const opts = {
		method,
		headers: {
			"Content-Type": "application/json",
			"X-Admin-Key": getAdminKey(),
		},
	};
	if (body !== undefined) opts.body = JSON.stringify(body);
	const r = await fetch(path, opts);
	if (r.status === 401) {
		sessionStorage.removeItem("admin_key");
		showAuthOverlay();
		throw new Error("unauthorized");
	}
	if (!r.ok) {
		throw await httpError(r);
	}
	return r.json();
}

async function apiResponse(method, path, body) {
	const opts = {
		method,
		headers: {
			"Content-Type": "application/json",
			"X-Admin-Key": getAdminKey(),
		},
	};
	if (body !== undefined) opts.body = JSON.stringify(body);
	const r = await fetch(path, opts);
	if (r.status === 401) {
		sessionStorage.removeItem("admin_key");
		showAuthOverlay();
		throw new Error("unauthorized");
	}
	if (!r.ok) {
		throw await httpError(r);
	}
	return r;
}

function fmt(n) {
	if (n === null || n === undefined) return "—";
	return n.toLocaleString();
}

function fmtTime(t) {
	if (!t) return "—";
	const d = new Date(t * 1000);
	return d.toLocaleString();
}

function fmtMoney(n) {
	if (n === null || n === undefined || n === "") return "—";
	const value = Number(n);
	if (!Number.isFinite(value)) return "—";
	return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function fmtSaleMoney(minor, currency = "CNY") {
	if (minor === null || minor === undefined || minor === "") return "—";
	const value = Number(minor);
	if (!Number.isFinite(value)) return "—";
	return new Intl.NumberFormat("zh-CN", {
		style: "currency",
		currency: currency || "CNY",
		minimumFractionDigits: 2,
	}).format(value / 100);
}

function downloadText(text, filename) {
	const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
	const url = URL.createObjectURL(blob);
	const anchor = document.createElement("a");
	anchor.href = url;
	anchor.download = filename;
	document.body.appendChild(anchor);
	anchor.click();
	anchor.remove();
	URL.revokeObjectURL(url);
}

const PROVIDERS = Object.freeze({
	openai: { className: "openai", label: "OPENAI" },
	anthropic: { className: "anthropic", label: "ANTHROPIC" },
	gemini: { className: "gemini", label: "GEMINI" },
	aws_bedrock: { className: "aws-bedrock", label: "AWS BEDROCK" },
});

function providerBadge(p) {
	const provider = PROVIDERS[p] || { className: "unknown", label: "未知" };
	return `<span class="badge ${provider.className}">${provider.label}</span>`;
}

function effectiveCheckStatus(row) {
	const status = row?.status || row?.current_check_status || row?.latest_check_status;
	const summary = row?.extra?.model_summary || {};
	const throttledRegions = Array.isArray(summary.throttled_regions)
		? summary.throttled_regions
		: [];
	// Historical Bedrock rows may still be persisted as "error" by the old
	// aggregator. A throttled runtime call proves model authorization, so render
	// those saved results consistently as quota-limited without rewriting history.
	if (row?.provider === "aws_bedrock" && status === "error" && throttledRegions.length) {
		return "no_quota";
	}
	return status;
}

function statusBadge(s) {
	const map = {
		valid: "✅ 有效",
		no_quota: "⚠ 无额度",
		invalid: "❌ 失效",
		error: "⚠ 错误",
		pending: "⏳ 待测",
		checking: "🔄 检测中",
	};
	const text = map[s] || s || "—";
	return `<span class="badge badge-status s-${s || "pending"}">${text}</span>`;
}

function stockStatusBadge(s) {
	const map = {
		pending_check: "待检测",
		in_stock: "可售",
		reserved: "已预留",
		sold: "已售出",
		returned: "已退回",
		no_quota: "无额度",
		invalid: "失效",
		quarantined: "隔离",
		archived: "归档",
	};
	const text = map[s] || s || "—";
	return `<span class="badge badge-status inv-${s || "unknown"}">${text}</span>`;
}

function vaultInventoryControl(row) {
	if (!row.is_in_inventory) {
		if (!row.is_callable) {
			return `<button class="btn btn-small" type="button" disabled title="最新检测没有真实模型调用成功">需复检</button>`;
		}
		return `<button class="btn btn-small btn-inbound v-row-inbound" type="button" aria-label="将 ${escapeHtml(row.api_key_short || `第 ${row.id} 行`)} 入库">未入库</button>`;
	}
	const statusMap = {
		pending_check: "待检测",
		in_stock: "可售",
		reserved: "已预留",
		sold: "已售出",
		returned: "已退回",
		no_quota: "无额度",
		invalid: "失效",
		quarantined: "隔离",
		archived: "归档",
	};
	const statusText = statusMap[row.inventory_status] || row.inventory_status || "已登记";
	return `<span class="badge badge-status vault-inventory-linked">已入库 · ${escapeHtml(statusText)}</span>`;
}

function tierBadge(t) {
	if (!t) return "—";
	const cls = String(t).toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9_-]/g, "");
	return `<span class="tier ${cls}">${escapeHtml(t)}</span>`;
}

function escapeHtml(v) {
	return String(v ?? "").replace(/[&<>"']/g, (c) => ({
		"&": "&amp;",
		"<": "&lt;",
		">": "&gt;",
		'"': "&quot;",
		"'": "&#39;",
	})[c]);
}

function chip(label, className = "", title = "") {
	const cls = className ? ` ${className}` : "";
	const titleAttr = title ? ` title="${escapeHtml(title)}"` : "";
	return `<span class="chip${cls}"${titleAttr}>${escapeHtml(label)}</span>`;
}

function modelGroupsTitle(groups) {
	if (!groups) return "";
	return Object.entries(groups)
		.filter(([, models]) => Array.isArray(models) && models.length)
		.map(([group, models]) => `${group}: ${models.join(", ")}`)
		.join("\n");
}

function modelGroupFor(model, groups) {
	for (const [group, models] of Object.entries(groups || {})) {
		if (Array.isArray(models) && models.includes(model)) return group;
	}
	return "model";
}

const OPENAI_DISPLAY_TARGETS = [
	{ label: "gpt-5.6", prefixes: ["gpt-5.6"] },
	{ label: "gpt-5.5", prefixes: ["gpt-5.5", "gpt5.5"] },
	{ label: "gpt-image-2", prefixes: ["gpt-image-2"] },
	{ label: "sora-2", prefixes: ["sora-2"] },
];

function flattenModelGroups(groups) {
	const out = [];
	for (const models of Object.values(groups || {})) {
		if (Array.isArray(models)) out.push(...models);
	}
	return Array.from(new Set(out.filter(Boolean)));
}

function targetMatches(model, prefix) {
	const m = String(model || "").toLowerCase();
	const p = String(prefix || "").toLowerCase();
	return m === p || m.startsWith(`${p}-`) || m.startsWith(`${p}.`);
}

function findTargetModel(models, prefixes) {
	for (const prefix of prefixes) {
		const exact = models.find((model) => String(model).toLowerCase() === prefix);
		if (exact) return exact;
	}
	for (const prefix of prefixes) {
		const matched = models.find((model) => targetMatches(model, prefix));
		if (matched) return matched;
	}
	return null;
}

function displayTargetsFromSupported(supported) {
	if (Array.isArray(supported.display_targets)) return supported.display_targets;

	const groups = supported.groups || {};
	const models = flattenModelGroups(groups);
	return OPENAI_DISPLAY_TARGETS.map((target) => {
		const model = findTargetModel(models, target.prefixes);
		return {
			label: target.label,
			model,
			supported: Boolean(model),
			group: model ? modelGroupFor(model, groups) : null,
		};
	});
}

function openaiModelChips(e) {
	const supported = e.supported_models;
	if (!supported) return [];

	const groups = supported.groups || {};
	const displayTargets = displayTargetsFromSupported(supported);
	if (!displayTargets.length) return [];

	const allCount = supported.all_count ?? flattenModelGroups(groups).length;
	const title = modelGroupsTitle(groups);
	const supportedCount = displayTargets.filter((target) => target.supported).length;
	const chips = displayTargets.map((target) => {
		const cls = target.supported ? `on model-chip model-${target.group || "model"}` : "off model-chip";
		const targetTitle = target.model && target.model !== target.label
			? `${target.label}: ${target.model}\n${title}`
			: title;
		return chip(target.label, cls, targetTitle);
	});

	const remaining = Math.max(allCount - supportedCount, 0);
	if (remaining > 0) chips.push(chip(`+${remaining}`, "model-more", title));
	return chips;
}

function targetModelChips(supported) {
	if (!supported) return [];

	const displayTargets = Array.isArray(supported.display_targets)
		? supported.display_targets
		: [];
	if (!displayTargets.length) return [];

	const title = Array.isArray(supported.models_preview)
		? supported.models_preview.join("\n")
		: "";
	const supportedCount = displayTargets.filter((target) => target.supported).length;
	const chips = displayTargets.map((target) => {
		const targetTitle = target.model && target.model !== target.label
			? `${target.label}: ${target.model}${target.display_name ? `\n${target.display_name}` : ""}${title ? `\n${title}` : ""}`
			: title;
		return chip(target.label, target.supported ? "on model-chip" : "off model-chip", targetTitle);
	});

	const allCount = supported.all_count ?? 0;
	const remaining = Math.max(allCount - supportedCount, 0);
	if (remaining > 0) chips.push(chip(`+${remaining}`, "model-more", title));
	return chips;
}

function bedrockDetailChipList(extra, compact = false) {
	const chips = [];
	const identity = extra.identity || {};
	const isDeep = extra.check_mode === "bedrock_deep" || extra.check_mode === "deep";
	const mode = isDeep
		? "全区域深检"
		: "快速检测";
	chips.push(chip(mode, isDeep ? "aws-deep" : ""));

	const credentialStatus = extra.credential_status || extra.credentials_status || identity.status;
	if (credentialStatus) {
		const normalizedCredentialStatus = String(credentialStatus).toLowerCase();
		const credentialLabels = {
			verified: "STS 已验证",
			bedrock_verified: "Bedrock 已验证",
			sts_unavailable: "STS 未验证",
			invalid: "凭证失效",
			invalid_format: "格式错误",
		};
		chips.push(chip(
			credentialLabels[normalizedCredentialStatus] || `凭证 ${credentialStatus}`,
			["valid", "verified", "bedrock_verified"].includes(normalizedCredentialStatus) ? "on" : "off",
		));
	}

	const accountId = extra.account || extra.account_id || identity.account_id || identity.account;
	if (accountId) chips.push(chip(`账户 ${accountId}`, "", String(accountId)));
	const principal = extra.principal || extra.principal_arn || extra.arn || identity.principal_arn || identity.arn;
	if (principal && !compact) chips.push(chip("IAM 主体", "", String(principal)));

	const checkedRegions = extra.checked_regions || extra.regions_checked || [];
	const regionResults = extra.region_results || extra.regions || {};
	const regionNames = Array.isArray(checkedRegions)
		? checkedRegions
		: Object.keys(regionResults || {});
	const regionCount = regionNames.length || (Number.isFinite(Number(checkedRegions)) ? Number(checkedRegions) : 0);
	if (regionCount) chips.push(chip(`已扫描 ${regionCount} 区域`, "", regionNames.join("\n")));

	const modelSummary = extra.model_summary || extra.models_summary || {};
	const modelList = Array.isArray(modelSummary)
		? modelSummary
		: modelSummary.supported_models || modelSummary.models || modelSummary.opus_versions || extra.opus_versions || [];
	const modelCount = Array.isArray(modelList)
		? modelList.length
		: Number(modelSummary.supported_model_count || modelSummary.count || extra.models_count || 0);
	const profilesFound = Number(modelSummary.profiles_found || 0);
	const discoveredRegions = Array.isArray(modelSummary.supported_regions)
		? modelSummary.supported_regions
		: [];
	const successfulVersions = Array.isArray(modelSummary.successful_versions)
		? modelSummary.successful_versions
		: [];
	const successfulRegions = Array.isArray(modelSummary.successful_regions)
		? modelSummary.successful_regions
		: [];
	const throttledRegions = Array.isArray(modelSummary.throttled_regions)
		? modelSummary.throttled_regions
		: [];
	const throttledVersions = Array.isArray(modelSummary.throttled_versions)
		? modelSummary.throttled_versions
		: [];
	const throttledModels = Array.isArray(modelSummary.throttled_models)
		? modelSummary.throttled_models
		: [];
	const latestModel = modelSummary.latest || modelSummary.latest_model || extra.latest_model || extra.probe_model;
	if (successfulRegions.length) {
		chips.push(chip(`可调用区域 ${successfulRegions.length}`, "on", successfulRegions.join("\n")));
	}
	if (discoveredRegions.length && !compact) {
		chips.push(chip(`发现区域 ${discoveredRegions.length}`, "", discoveredRegions.join("\n")));
	}
	if (modelCount) {
		chips.push(chip(`Opus 模型 ×${modelCount}`, "", Array.isArray(modelList) ? modelList.join("\n") : ""));
	} else if (latestModel) chips.push(chip(String(latestModel), "on", String(latestModel)));
	else if (successfulVersions.length) {
		chips.push(chip(`可调用 Opus ×${successfulVersions.length}`, "on", successfulVersions.join("\n")));
	}
	if (profilesFound && !compact) chips.push(chip(`Profiles ${profilesFound}`));
	if (throttledRegions.length) {
		const versionLabel = throttledVersions.length
			? `Opus ${throttledVersions.join("/")}`
			: "Opus";
		const throttleTitle = [
			"已通过 Bedrock 模型鉴权，但当前受到调用频率或额度限制。",
			`受限区域：\n${throttledRegions.join("\n")}`,
			throttledModels.length ? `受限模型：\n${throttledModels.join("\n")}` : "",
		].filter(Boolean).join("\n\n");
		chips.push(chip(
			`${versionLabel} 限流 · ${throttledRegions.length} 区域`,
			"warn",
			throttleTitle,
		));
	}
	if (!compact && extra.invocation_verification === "inconclusive") {
		chips.push(chip(
			"仅发现模型，未调用成功",
			"off",
			"这条历史结果没有完成真实模型调用，必须重新检测成功后才能入库",
		));
	}
	if (!compact && extra.invocation_verification === "failed") {
		chips.push(chip(
			"InvokeModel 调用失败",
			"off",
			extra.runtime_restriction === "operation_not_allowed"
				? "AWS 拒绝模型运行时调用（Operation not allowed）"
				: "没有任何 Opus 模型完成真实 InvokeModel 调用",
		));
	}

	const quotas = extra.quotas || extra.service_quotas;
	if (quotas && !compact) {
		const quotaCount = Array.isArray(quotas) ? quotas.length : Object.keys(quotas).length;
		chips.push(chip(`配额 ${quotaCount}`, "", `${quotaCount} 条 Service Quotas 结果`));
	}
	const failures = extra.partial_failures || extra.failures || [];
	const failureCount = Array.isArray(failures) ? failures.length : Object.keys(failures || {}).length;
	if (failureCount) chips.push(chip(`部分失败 ${failureCount}`, "off", `${failureCount} 个区域或步骤失败`));
	if (extra.proxy_used && !compact) chips.push(chip("SOCKS5 代理", "on", "本次 AWS Bedrock 检测已通过 SOCKS5 代理发送"));
	else if (extra.proxy_unavailable && !compact) chips.push(chip("代理池无可用节点", "off", "为避免意外直连，本次检测已停止"));
	else if (extra.proxy_ignored && !compact) chips.push(chip("直连（代理未生效）", "off", "本次 AWS Bedrock 检测未使用所选代理"));
	return chips;
}

function detailChips(r) {
	const e = r.extra || {};
	const chips = [];
	if (r.provider === "openai") {
		const dynamicModelChips = openaiModelChips(e);
		if (dynamicModelChips.length) {
			chips.push(...dynamicModelChips);
		} else {
			if (typeof e.has_gpt_5_5 !== "undefined") {
				chips.push(chip("gpt-5.5", e.has_gpt_5_5 ? "on" : "off"));
			}
			if (typeof e.has_gpt_image_2 !== "undefined") {
				chips.push(chip("gpt-image-2", e.has_gpt_image_2 ? "on" : "off"));
			}
			if (typeof e.has_sora_2 !== "undefined") {
				chips.push(chip("sora-2", e.has_sora_2 ? "on" : "off"));
			}
			if (e.models_count) chips.push(chip(`📦 ${e.models_count}`));
		}
		if (e.org_id) {
			chips.push(chip("🆔", "", e.org_id));
		}
		if (e.tier_reason) {
			chips.push(chip(`? ${e.tier_reason}`, "off", e.tier_reason));
		}
		if (e.burst_probe) {
			const bp = e.burst_probe;
			chips.push(
				chip(
					`burst ${bp.rpm_estimate ?? "?"}`,
					"",
					`burst probe: ok=${bp.ok} 429=${bp.hit_429} est=${bp.rpm_estimate}`,
				),
			);
		}
	} else if (r.provider === "gemini") {
		const dynamicModelChips = targetModelChips(e.supported_models);
		if (dynamicModelChips.length) {
			chips.push(...dynamicModelChips);
		} else if (e.models_error) {
			chips.push(chip("models unavailable", "off", e.models_error));
		} else if (e.probe_model) {
			chips.push(chip(e.probe_model));
		}
		if (e.burst && e.burst.ceiling_hit)
			chips.push(chip("429 hit", "on"));
	} else if (r.provider === "anthropic") {
		const dynamicModelChips = targetModelChips(e.supported_models);
		if (dynamicModelChips.length) {
			chips.push(...dynamicModelChips);
		} else if (e.models_error) {
			chips.push(chip("models unavailable", "off", e.models_error));
		} else if (e.probe_model) {
			chips.push(chip("retest for models", "off", e.probe_model));
		}
		if (e.source) chips.push(chip(e.source, "", e.probe_model || ""));
	} else if (r.provider === "aws_bedrock") {
		chips.push(...bedrockDetailChipList(e));
	}
	if (r.error) {
		const quotaLimited = effectiveCheckStatus(r) === "no_quota";
		chips.push(chip(
			`${quotaLimited ? "⚠" : "❗"} ${r.error.slice(0, 36)}`,
			quotaLimited ? "warn" : "off",
			r.error,
		));
	}
	return `<div class="detail-chips">${chips.join("")}</div>`;
}

function inventorySupportedModelChips(r) {
	const e = r.extra || {};
	let chips = [];
	if (r.provider === "openai") {
		chips = openaiModelChips(e);
		if (!chips.length) {
			if (typeof e.has_gpt_5_5 !== "undefined")
				chips.push(chip("gpt-5.5", e.has_gpt_5_5 ? "on" : "off"));
			if (typeof e.has_gpt_image_2 !== "undefined")
				chips.push(chip("gpt-image-2", e.has_gpt_image_2 ? "on" : "off"));
			if (typeof e.has_sora_2 !== "undefined")
				chips.push(chip("sora-2", e.has_sora_2 ? "on" : "off"));
			if (e.models_count) chips.push(chip(`models ${e.models_count}`));
		}
	} else if (r.provider === "anthropic") {
		chips = targetModelChips(e.supported_models);
		if (!chips.length && e.models_error) chips.push(chip("models unavailable", "off", e.models_error));
	} else if (r.provider === "gemini") {
		chips = targetModelChips(e.supported_models);
		if (!chips.length && e.models_error) chips.push(chip("models unavailable", "off", e.models_error));
		if (!chips.length && e.probe_model) chips.push(chip(e.probe_model));
	} else if (r.provider === "aws_bedrock") {
		chips = bedrockDetailChipList(e, true);
	}
	return chips.length
		? `<div class="detail-chips">${chips.join("")}</div>`
		: `<span class="muted">—</span>`;
}

// ─── state ───────────────────────────────────────────────────────────
const state = {
	keys: [],
	selected: new Set(),
	jobId: null,
	jobPollTimer: null,
	deepBusy: false,
};

function getFilters() {
	return {
		provider: $("#f-provider").value || null,
		status: $("#f-status").value || null,
		tier: $("#f-tier").value || null,
	};
}

function resetCheckFilters() {
	$("#f-provider").value = "";
	$("#f-status").value = "";
	$("#f-tier").value = "";
}

// ─── render ──────────────────────────────────────────────────────────
function render() {
	const tbody = $("#keys-table tbody");
	const empty = $("#empty");
	tbody.innerHTML = "";
	if (!state.keys.length) {
		empty.classList.remove("hidden");
	} else {
		empty.classList.add("hidden");
	}

	let valid = 0,
		noQuota = 0,
		invalid = 0,
		pending = 0;
	for (const r of state.keys) {
		const displayStatus = effectiveCheckStatus(r);
		if (displayStatus === "valid") valid++;
		else if (displayStatus === "no_quota") noQuota++;
		else if (displayStatus === "invalid") invalid++;
		else pending++;

		const tr = document.createElement("tr");
		tr.dataset.id = r.id;
		tr.innerHTML = `
      <td class="col-cb"><label class="checkbox-hitarea"><input type="checkbox" class="cb-row" aria-label="选择 ${escapeHtml(r.api_key_short || `第 ${r.id} 行`)}" ${state.selected.has(r.id) ? "checked" : ""}></label></td>
      <td>${providerBadge(r.provider)}</td>
      <td class="key-cell masked" title="完整 Key 需选中后复制">${escapeHtml(r.api_key_short)}</td>
      <td>${statusBadge(displayStatus)}</td>
      <td>${tierBadge(r.tier)}</td>
      <td>${fmt(r.rpm)}</td>
      <td>${fmt(r.tpm)}</td>
      <td>${detailChips(r)}</td>
      <td>${fmtTime(r.checked_at)}</td>
    `;
		tbody.appendChild(tr);
	}
	$("#stats").textContent =
		`总数 ${state.keys.length} · ✅ ${valid} · ⚠ ${noQuota} · ❌ ${invalid} · ⏳ ${pending}`;
	syncCheckSelectionControls();
}

async function loadKeys() {
	const f = getFilters();
	const params = new URLSearchParams();
	for (const k in f) if (f[k]) params.set(k, f[k]);
	const data = await api("GET", `/api/keys?${params.toString()}`);
	state.keys = data.keys || [];
	const visibleIds = new Set(state.keys.map((r) => r.id));
	state.selected.forEach((id) => {
		if (!visibleIds.has(id)) state.selected.delete(id);
	});
	render();
}

// ─── selection ───────────────────────────────────────────────────────
function selectedKeyObjs() {
	return state.keys.filter((r) => state.selected.has(r.id));
}

function syncCheckSelectionControls() {
	const selected = selectedKeyObjs();
	const hasRows = state.keys.length > 0;
	const allVisible = hasRows && selected.length === state.keys.length;
	$("#cb-all").checked = allVisible;
	$("#cb-all").indeterminate = selected.length > 0 && !allVisible;
	$("#cb-all").disabled = !hasRows;
	$("#btn-select-all").disabled = !hasRows;
	$("#btn-select-all").textContent = allVisible ? "取消全选" : "全选";
	["#btn-copy", "#btn-retest", "#btn-delete"].forEach((selector) => {
		$(selector).disabled = selected.length === 0;
	});
	const deepButton = $("#btn-bedrock-deep");
	const hasSelectedBedrock = selected.some((r) => r.provider === "aws_bedrock");
	deepButton.disabled = state.deepBusy || !hasSelectedBedrock;
	deepButton.textContent = state.deepBusy ? "正在提交深检…" : "Bedrock 深检";
	deepButton.setAttribute("aria-busy", String(state.deepBusy));
}

async function requestSecretText(endpoint, ids, format = "txt") {
	const response = await apiResponse("POST", endpoint, { ids, format });
	return response.text();
}

function secretTextLineCount(text) {
	return text.split("\n").filter((line) => line.trim()).length;
}

async function copySecretText(text) {
	if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
		try {
			await navigator.clipboard.writeText(text);
			return;
		} catch (_) {
			// Some browsers lose clipboard permission/focus after an async request
			// or native confirm dialog. Fall through to the selection-based copy.
		}
	}

	const activeElement = document.activeElement;
	const textarea = document.createElement("textarea");
	textarea.value = text;
	textarea.readOnly = true;
	textarea.setAttribute("aria-hidden", "true");
	textarea.style.position = "fixed";
	textarea.style.left = "-9999px";
	textarea.style.top = "0";
	textarea.style.opacity = "0";
	document.body.appendChild(textarea);
	textarea.focus();
	textarea.select();
	textarea.setSelectionRange(0, textarea.value.length);
	let copied = false;
	try {
		copied = document.execCommand("copy");
	} finally {
		textarea.remove();
		if (activeElement && typeof activeElement.focus === "function") activeElement.focus();
	}
	if (!copied) throw new Error("浏览器阻止了剪贴板访问，请允许剪贴板权限后重试");
}

function bedrockIds(rows) {
	return rows.filter((row) => row.provider === "aws_bedrock").map((row) => row.id);
}

function confirmBedrockDeep(count, skipped = 0, useProxy = false) {
	const skippedText = skipped ? `\n另有 ${skipped} 个非 Bedrock 项不会处理。` : "";
	const connectionText = useProxy
		? "本次检测将使用已勾选的 SOCKS5 代理池。"
		: "本次检测将直连 AWS。";
	return confirm(
		`确认深检 ${count} 个 AWS Bedrock Key？\n将扫描全部已知 Bedrock 区域，仅发现并调用 Claude Opus；真实模型调用可能产生费用。${connectionText}${skippedText}`,
	);
}

// ─── job polling ─────────────────────────────────────────────────────
async function pollJob() {
	if (!state.jobId) return;
	try {
		const j = await api("GET", `/api/jobs/${state.jobId}`);
		const detailTotal = Number(j.detail_total || 0);
		const detailDone = Number(j.detail_done || 0);
		const hasDetailProgress = detailTotal > 0;
		const progressTotal = hasDetailProgress ? detailTotal : Number(j.total || 0);
		const progressDone = hasDetailProgress ? detailDone : Number(j.done || 0);
		const pct = progressTotal
			? Math.min(100, Math.round((progressDone / progressTotal) * 100))
			: 0;
		$("#job-bar-fill").style.width = pct + "%";
		if (hasDetailProgress) {
			const current = j.detail_label ? ` · 当前 ${j.detail_label}` : "";
			$("#job-text").textContent =
				`Job #${j.id} · Key ${j.done}/${j.total} · 检测步骤 ${detailDone}/${detailTotal} (${pct}%)${current} · ${j.status}`;
		} else {
			$("#job-text").textContent =
				`Job #${j.id} · ${j.done}/${j.total} (${pct}%) · ${j.status}`;
		}

		// refresh keys table every poll
		await loadKeys();
		// also refresh vault count badge (and vault list if currently viewing it)
		refreshVaultBadge();
		refreshInventoryBadge();
		if (!$("#view-vault").classList.contains("hidden")) await loadVault();
		if (!$("#view-inventory").classList.contains("hidden")) await loadInventory();

		if (["done", "cancelled"].includes(j.status)) {
			clearInterval(state.jobPollTimer);
			state.jobPollTimer = null;
			state.jobId = null;
			toast(j.status === "done" ? "✅ 检测完成" : "检测任务已中止，请重新提交");
			setTimeout(() => $("#job-status").classList.add("hidden"), 2500);
		}
	} catch (e) {
		console.error(e);
	}
}

function startPolling(jobId) {
	state.jobId = jobId;
	$("#job-status").classList.remove("hidden");
	$("#job-bar-fill").style.width = "0%";
	$("#job-text").textContent = `Job #${jobId} · 启动中…`;
	if (state.jobPollTimer) clearInterval(state.jobPollTimer);
	state.jobPollTimer = setInterval(pollJob, 1500);
	pollJob();
}

// ─── handlers ────────────────────────────────────────────────────────
$("#btn-import").addEventListener("click", async () => {
	const text = $("#keys-input").value.trim();
	if (!text) return toast("请粘贴 Key");
	const concurrency = parseInt($("#concurrency").value, 10) || 4;
	const useProxy = $("#use-proxy").checked;
	try {
		const r = await api("POST", "/api/keys/import", {
			text,
			concurrency,
			use_proxy: useProxy,
		});
		const bd = Object.entries(r.breakdown || {})
			.map(([k, v]) => `${k}:${v}`)
			.join(" / ");
		toast(`已导入 ${r.imported} 个 (${bd})`);
		$("#keys-input").value = "";
		resetCheckFilters();
		state.selected.clear();
		await loadKeys();
		startPolling(r.job_id);
	} catch (e) {
		toast("导入失败：" + e.message);
	}
});

$("#btn-refresh").addEventListener("click", loadKeys);

$$("#f-provider, #f-status, #f-tier").forEach((el) =>
	el.addEventListener("change", loadKeys),
);

$("#cb-all").addEventListener("change", (e) => {
	const checked = e.target.checked;
	state.keys.forEach((r) =>
		checked ? state.selected.add(r.id) : state.selected.delete(r.id),
	);
	render();
});

$("#btn-select-all").addEventListener("click", () => {
	if (state.keys.length && state.keys.every((r) => state.selected.has(r.id))) state.selected.clear();
	else state.keys.forEach((r) => state.selected.add(r.id));
	render();
});

$("#keys-table tbody").addEventListener("click", (e) => {
	const tr = e.target.closest("tr");
	if (!tr) return;
	const id = parseInt(tr.dataset.id, 10);
	if (e.target.classList.contains("cb-row")) {
		if (e.target.checked) state.selected.add(id);
		else state.selected.delete(id);
		syncCheckSelectionControls();
	}
});

$("#btn-copy").addEventListener("click", async () => {
	const ids = selectedKeyObjs().map((r) => r.id);
	if (!ids.length) return toast("请先选择");
	try {
		const text = await requestSecretText("/api/keys/export", ids);
		await copySecretText(text);
		toast(`已复制 ${secretTextLineCount(text)} 行（${ids.length} 个 Key，操作已审计）`);
	} catch (e) {
		toast("复制失败：" + e.message);
	}
});

$("#btn-retest").addEventListener("click", async () => {
	const ids = [...state.selected];
	if (!ids.length) return toast("请先选择");
	const concurrency = parseInt($("#concurrency").value, 10) || 4;
	const useProxy = $("#use-proxy").checked;
	try {
		const r = await api("POST", "/api/keys/recheck", {
			ids,
			concurrency,
			use_proxy: useProxy,
			mode: "quick",
		});
		toast(`已重测 ${r.queued} 个`);
		if (r.job_id) startPolling(r.job_id);
	} catch (e) {
		toast("失败：" + e.message);
	}
});

$("#btn-bedrock-deep").addEventListener("click", async () => {
	if (state.deepBusy) return;
	const selected = selectedKeyObjs();
	const ids = bedrockIds(selected);
	if (!ids.length) return toast("请先选择 AWS Bedrock Key");
	const useProxy = $("#use-proxy").checked;
	if (!confirmBedrockDeep(ids.length, selected.length - ids.length, useProxy)) return;
	state.deepBusy = true;
	syncCheckSelectionControls();
	try {
		const r = await api("POST", "/api/keys/recheck", {
			ids,
			concurrency: Math.min(parseInt($("#concurrency").value, 10) || 2, 2),
			use_proxy: useProxy,
			mode: "bedrock_deep",
		});
		toast(`已加入 Bedrock 深检 ${r.queued} 个${r.skipped ? `，跳过 ${r.skipped} 个` : ""}`);
		if (r.job_id) startPolling(r.job_id);
	} catch (e) {
		toast("深检失败：" + e.message);
	} finally {
		state.deepBusy = false;
		syncCheckSelectionControls();
	}
});

$("#btn-delete").addEventListener("click", async () => {
	const ids = [...state.selected];
	if (!ids.length) return toast("请先选择");
	if (!confirm(`确认删除 ${ids.length} 个 Key？`)) return;
	try {
		const r = await api("POST", "/api/keys/delete", { ids, concurrency: 1 });
		toast(`已删除 ${r.deleted}`);
		state.selected.clear();
		await loadKeys();
	} catch (e) {
		toast("失败：" + e.message);
	}
});

// ─── vault ───────────────────────────────────────────────────────────
const VAULT_MODES = {
	vault: {
		title: "🗝️ 密钥库",
		description: "所有检测通过的有效 Key 会自动归档到这里",
		empty: "密钥库为空。检测通过的 Key 会自动加入密钥库。",
	},
};

const vaultState = {
	keys: [],
	selected: new Set(),
	inboundIds: [],
	mode: "vault",
	loadSeq: 0,
	deepBusy: false,
};

function currentVaultMode() {
	return VAULT_MODES[vaultState.mode] || VAULT_MODES.vault;
}

function updateVaultModeContent() {
	const mode = currentVaultMode();
	$("#v-view-title").textContent = mode.title;
	$("#v-view-description").textContent = mode.description;
	$("#v-empty").textContent = mode.empty;
	$("#view-vault").setAttribute("aria-labelledby", `tab-${vaultState.mode}`);
}

function getVaultFilters() {
	return {
		provider: $("#v-provider").value || null,
		tier: $("#v-tier").value || null,
		legacy_sale_candidate: $("#v-legacy-sale").value || null,
	};
}

function updateVaultBadges(stats = {}) {
	$("#vault-count-badge").textContent = stats.total ?? 0;
}

function selectedVisibleVaultRows() {
	return vaultState.keys.filter((r) => vaultState.selected.has(r.id));
}

function selectedVisibleVaultIds() {
	return selectedVisibleVaultRows().map((r) => r.id);
}

function selectedPendingInboundVaultIds() {
	return selectedVisibleVaultRows()
		.filter((r) => !r.is_in_inventory && r.is_callable)
		.map((r) => r.id);
}

function syncVaultSelectionControls() {
	const selectedCount = selectedVisibleVaultRows().length;
	const hasRows = vaultState.keys.length > 0;
	const allVisible = hasRows && selectedCount === vaultState.keys.length;
	const checkbox = $("#v-cb-all");
	checkbox.checked = allVisible;
	checkbox.indeterminate = selectedCount > 0 && !allVisible;
	checkbox.disabled = !hasRows;
	$("#v-btn-select-all").disabled = !hasRows;
	$("#v-btn-select-all").textContent = allVisible ? "取消全选" : "全选";
	[
		"#v-btn-copy",
		"#v-btn-retest",
		"#v-btn-export",
		"#v-btn-delete",
	].forEach((selector) => {
		$(selector).disabled = selectedCount === 0;
	});
	const pendingInboundCount = selectedPendingInboundVaultIds().length;
	const inboundButton = $("#v-btn-inbound");
	inboundButton.disabled = pendingInboundCount === 0;
	inboundButton.textContent = pendingInboundCount
		? `选中入库 (${pendingInboundCount})`
		: "选中入库";
	const deepButton = $("#v-btn-bedrock-deep");
	deepButton.disabled = vaultState.deepBusy || !selectedVisibleVaultRows().some(
		(row) => row.provider === "aws_bedrock",
	);
	deepButton.textContent = vaultState.deepBusy ? "正在提交深检…" : "Bedrock 深检";
	deepButton.setAttribute("aria-busy", String(vaultState.deepBusy));
}

function renderVault() {
	const tbody = $("#vault-table tbody");
	const empty = $("#v-empty");
	tbody.innerHTML = "";
	if (!vaultState.keys.length) empty.classList.remove("hidden");
	else empty.classList.add("hidden");

	for (const r of vaultState.keys) {
		const tr = document.createElement("tr");
		tr.dataset.id = r.id;
		tr.innerHTML = `
      <td class="col-cb"><label class="checkbox-hitarea"><input type="checkbox" class="v-cb-row" aria-label="选择 ${escapeHtml(r.api_key_short || `第 ${r.id} 行`)}" ${vaultState.selected.has(r.id) ? "checked" : ""}></label></td>
      <td>${providerBadge(r.provider)}</td>
      <td class="key-cell masked" title="完整 Key 需选中后复制">${escapeHtml(r.api_key_short)}</td>
      <td>${tierBadge(r.tier)}</td>
      <td>${fmt(r.rpm)}</td>
      <td>${fmt(r.tpm)}</td>
      <td>${detailChips(r)}</td>
      <td><span class="chip">×${r.check_count ?? 0}</span></td>
      <td>${fmtTime(r.first_verified_at)}</td>
      <td>${fmtTime(r.last_verified_at)}</td>
      <td>${vaultInventoryControl(r)}</td>
      <td><input class="v-note" data-id="${r.id}" value="${escapeHtml(r.note || "")}" placeholder="备注…"></td>
    `;
		tbody.appendChild(tr);
	}
	syncVaultSelectionControls();
}

async function loadVault() {
	const loadSeq = ++vaultState.loadSeq;
	const f = getVaultFilters();
	const params = new URLSearchParams();
	for (const k in f) if (f[k]) params.set(k, f[k]);
	const data = await api("GET", `/api/vault?${params.toString()}`);
	if (loadSeq !== vaultState.loadSeq) return;
	vaultState.keys = data.keys || [];
	const visibleIds = new Set(vaultState.keys.map((r) => r.id));
	vaultState.selected.forEach((id) => {
		if (!visibleIds.has(id)) vaultState.selected.delete(id);
	});
	const s = data.stats || {};
	const byProv = Object.entries(s.by_provider || {})
		.map(([k, v]) => `${k}:${v}`)
		.join(" · ");
	const currentCount = data.count ?? vaultState.keys.length;
	$("#v-stats").textContent = `当前 ${currentCount} 个 · 全库 ${s.total ?? 0} 个${byProv ? ` · ${byProv}` : ""}`;
	updateVaultBadges(s);
	renderVault();
}

$("#vault-table tbody").addEventListener("click", (e) => {
	const tr = e.target.closest("tr");
	if (!tr) return;
	const id = parseInt(tr.dataset.id, 10);
	if (e.target.closest(".v-row-inbound")) {
		openInboundModal([id]);
		return;
	}
	if (e.target.classList.contains("v-cb-row")) {
		if (e.target.checked) vaultState.selected.add(id);
		else vaultState.selected.delete(id);
		syncVaultSelectionControls();
	}
});

$("#vault-table tbody").addEventListener("change", async (e) => {
	if (e.target.classList.contains("v-note")) {
		const id = parseInt(e.target.dataset.id, 10);
		try {
			await api("POST", `/api/vault/note/${id}`, { note: e.target.value });
			toast("备注已保存");
			await loadVault();
		} catch (err) {
			toast("保存失败：" + err.message);
		}
	}
});

$("#v-cb-all").addEventListener("change", (e) => {
	const checked = e.target.checked;
	vaultState.keys.forEach((r) =>
		checked ? vaultState.selected.add(r.id) : vaultState.selected.delete(r.id),
	);
	renderVault();
});

$("#v-btn-select-all").addEventListener("click", () => {
	const allVisible = vaultState.keys.length > 0
		&& vaultState.keys.every((r) => vaultState.selected.has(r.id));
	if (allVisible) vaultState.keys.forEach((r) => vaultState.selected.delete(r.id));
	else vaultState.keys.forEach((r) => vaultState.selected.add(r.id));
	renderVault();
});

$("#v-btn-copy").addEventListener("click", async () => {
	const ids = selectedVisibleVaultIds();
	if (!ids.length) return toast("请先选择");
	try {
		const text = await requestSecretText("/api/vault/export", ids);
		await copySecretText(text);
		toast(`已复制 ${secretTextLineCount(text)} 行（${ids.length} 个 Key，操作已审计）`);
	} catch (e) {
		toast("复制失败：" + e.message);
	}
});

$("#v-btn-retest").addEventListener("click", async () => {
	const ids = selectedVisibleVaultIds();
	if (!ids.length) return toast("请先选择");
	const concurrency = parseInt($("#concurrency").value, 10) || 4;
	const useProxy = $("#use-proxy").checked;
	try {
		const r = await api("POST", "/api/vault/recheck", {
			ids,
			concurrency,
			use_proxy: useProxy,
			mode: "quick",
		});
		toast(`已重测 ${r.queued} 个（结果回写密钥库）`);
		// Switch to check view to follow progress.
		switchTab("check");
		if (r.job_id) startPolling(r.job_id);
	} catch (e) {
		toast("失败：" + e.message);
	}
});

$("#v-btn-bedrock-deep").addEventListener("click", async () => {
	if (vaultState.deepBusy) return;
	const selected = selectedVisibleVaultRows();
	const ids = bedrockIds(selected);
	if (!ids.length) return toast("请先选择 AWS Bedrock Key");
	const useProxy = $("#use-proxy").checked;
	if (!confirmBedrockDeep(ids.length, selected.length - ids.length, useProxy)) return;
	vaultState.deepBusy = true;
	syncVaultSelectionControls();
	try {
		const r = await api("POST", "/api/vault/recheck", {
			ids,
			concurrency: Math.min(parseInt($("#concurrency").value, 10) || 2, 2),
			use_proxy: useProxy,
			mode: "bedrock_deep",
		});
		toast(`已加入 Bedrock 深检 ${r.queued} 个${r.skipped ? `，跳过 ${r.skipped} 个` : ""}`);
		switchTab("check");
		$("#tab-check").focus();
		if (r.job_id) startPolling(r.job_id);
	} catch (e) {
		toast("深检失败：" + e.message);
	} finally {
		vaultState.deepBusy = false;
		syncVaultSelectionControls();
	}
});

$("#v-btn-delete").addEventListener("click", async () => {
	const ids = selectedVisibleVaultIds();
	if (!ids.length) return toast("请先选择");
	if (!confirm(`确认从密钥库删除 ${ids.length} 个 Key？`)) return;
	try {
		const r = await api("POST", "/api/vault/delete", { ids, concurrency: 1 });
		toast(`已删除 ${r.deleted}`);
		vaultState.selected.clear();
		await loadVault();
	} catch (e) {
		toast("失败：" + e.message);
	}
});

function openInboundModal(requestedIds = selectedPendingInboundVaultIds()) {
	const visiblePendingIds = new Set(
		vaultState.keys.filter((r) => !r.is_in_inventory).map((r) => r.id),
	);
	const ids = [...new Set(requestedIds)].filter((id) => visiblePendingIds.has(id));
	if (!ids.length) return toast("选中的 Key 均已入库");
	vaultState.inboundIds = ids;
	$("#inbound-title").textContent = `密钥入库（${ids.length} 个）`;
	$("#inbound-supplier").value = "";
	$("#inbound-cost").value = "";
	$("#inbound-tags").value = "";
	$("#inbound-note").value = "";
	$("#inbound-error").textContent = "请填写供应商";
	$("#inbound-error").classList.add("hidden");
	openModal($("#inbound-modal"), $("#inbound-supplier"));
}

function closeInboundModal() {
	closeModal($("#inbound-modal"));
	vaultState.inboundIds = [];
}

$("#v-btn-inbound").addEventListener("click", () => openInboundModal());
$("#inbound-close").addEventListener("click", closeInboundModal);
$("#inbound-cancel").addEventListener("click", closeInboundModal);
$("#inbound-modal").addEventListener("click", (e) => {
	if (e.target.id === "inbound-modal") closeInboundModal();
});

$("#inbound-submit").addEventListener("click", async () => {
	const ids = [...vaultState.inboundIds];
	if (!ids.length) return;
	const supplierName = $("#inbound-supplier").value.trim();
	if (!supplierName) {
		$("#inbound-error").classList.remove("hidden");
		$("#inbound-supplier").focus();
		return;
	}
	const costRaw = $("#inbound-cost").value.trim();
	const totalCost = costRaw ? Number(costRaw) : null;
	if (costRaw && !Number.isFinite(totalCost)) {
		$("#inbound-error").textContent = "总成本格式不正确";
		$("#inbound-error").classList.remove("hidden");
		return;
	}
	const submitButton = $("#inbound-submit");
	const originalLabel = submitButton.textContent;
	submitButton.disabled = true;
	submitButton.textContent = "正在入库…";
	submitButton.setAttribute("aria-busy", "true");
	try {
		const r = await api("POST", "/api/vault/inbound", {
			ids,
			supplier_name: supplierName,
			total_cost: totalCost,
			tags: $("#inbound-tags").value.trim() || null,
			note: $("#inbound-note").value.trim() || null,
		});
		toast(`已入库 ${r.inbounded} 个，跳过 ${r.skipped || 0} 个`);
		vaultState.selected.clear();
		closeInboundModal();
		await loadVault();
		await refreshInventoryBadge();
		if (!$("#view-inventory").classList.contains("hidden")) await loadInventory();
	} catch (e) {
		$("#inbound-error").textContent = "入库失败：" + e.message;
		$("#inbound-error").classList.remove("hidden");
	} finally {
		submitButton.disabled = false;
		submitButton.textContent = originalLabel;
		submitButton.setAttribute("aria-busy", "false");
	}
});

$("#v-btn-export").addEventListener("click", async () => {
	const ids = selectedVisibleVaultIds();
	if (!ids.length) return toast("请先选择");
	try {
		const text = await requestSecretText("/api/vault/export", ids);
		downloadText(text, `vault-keys-${Date.now()}.txt`);
		toast("导出已开始（操作已审计）");
	} catch (e) {
		toast("导出失败：" + e.message);
	}
});

$$("#v-provider, #v-tier, #v-legacy-sale").forEach((el) => {
	el.addEventListener("change", () => {
		vaultState.selected.clear();
		syncVaultSelectionControls();
		loadVault();
	});
});

// ─── inventory ──────────────────────────────────────────────────────
const INVENTORY_MODES = {
	inventory: {
		saleView: "all",
		title: "库存",
		description: "从密钥库正式入库后的 Key 会显示在这里",
		empty: "库存为空。请先在密钥库中选中有效 Key 入库。",
	},
	sellable: {
		saleView: "sellable",
		title: "可售出",
		description: "仅显示正式入库、库存状态为可售且最近检测有效的 Key",
		empty: "暂无满足库存状态和质检条件的可售 Key。",
	},
	sold: {
		saleView: "sold",
		title: "已售出",
		description: "正式销售记录中的已售库存，可查看买家、售价和售出时间",
		empty: "暂无已售出的库存。",
	},
};

const inventoryState = {
	keys: [],
	selected: new Set(),
	stats: {},
	busy: false,
	mode: "inventory",
	loadSeq: 0,
};

function currentInventoryMode() {
	return INVENTORY_MODES[inventoryState.mode] || INVENTORY_MODES.inventory;
}

function updateInventoryModeContent() {
	const mode = currentInventoryMode();
	$("#i-view-title").textContent = mode.title;
	$("#i-view-description").textContent = mode.description;
	$("#i-empty").textContent = mode.empty;
	$("#view-inventory").setAttribute("aria-labelledby", `tab-${inventoryState.mode}`);
	const soldView = inventoryState.mode === "sold";
	const sellableView = inventoryState.mode === "sellable";
	$("#i-btn-sell").classList.toggle("hidden", soldView);
	$("#i-btn-return").classList.toggle("hidden", sellableView);
	["#i-btn-reserve", "#i-btn-restore", "#i-btn-quarantine", "#i-btn-archive"].forEach((selector) => {
		$(selector).classList.toggle("hidden", soldView);
	});
	$("#i-btn-export").textContent = soldView ? "导出售出 Key" : "导出选中";
}

function getInventoryFilters() {
	return {
		sale_view: currentInventoryMode().saleView,
		provider: $("#i-provider").value || null,
		stock_status: $("#i-status").value || null,
		tier: $("#i-tier").value || null,
		supplier_id: $("#i-supplier").value || null,
		batch_id: $("#i-batch").value || null,
		risk_flag: $("#i-risk").value || null,
	};
}

function setSelectOptions(selectEl, options, currentValue) {
	selectEl.innerHTML = options
		.map(({ value, label }) => `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`)
		.join("");
	if (options.some((opt) => String(opt.value) === String(currentValue))) {
		selectEl.value = currentValue;
	}
}

function renderInventoryFilterOptions(stats = {}) {
	const suppliers = stats.suppliers || [];
	const batches = stats.batches || [];
	const riskFlags = stats.risk_flags || [];
	const supplierValue = $("#i-supplier").value;
	const batchValue = $("#i-batch").value;
	const riskValue = $("#i-risk").value;

	setSelectOptions(
		$("#i-supplier"),
		[
			{ value: "", label: "全部" },
			...suppliers.map((s) => ({
				value: s.id,
				label: `${s.name} (${s.count})`,
			})),
		],
		supplierValue,
	);
	setSelectOptions(
		$("#i-batch"),
		[
			{ value: "", label: "全部" },
			...batches.map((b) => ({
				value: b.id,
				label: `${b.name} (${b.count})`,
			})),
		],
		batchValue,
	);
	setSelectOptions(
		$("#i-risk"),
		[
			{ value: "", label: "全部" },
			{ value: "__empty__", label: "无标记" },
			...riskFlags.map((r) => ({
				value: r.risk_flag,
				label: `${r.risk_flag} (${r.count})`,
			})),
		],
		riskValue,
	);
}

function renderInventoryStats(stats = {}) {
	inventoryState.stats = stats;
	renderInventoryFilterOptions(stats);
	const byStatus = stats.by_status || {};
	const bySaleState = stats.by_sale_state || {};
	const items = [
		["总库存", stats.total ?? 0],
		["可售", bySaleState.sellable ?? byStatus.in_stock ?? 0],
		["预留", byStatus.reserved || 0],
		["已售", byStatus.sold || 0],
		["已退回", byStatus.returned || 0],
		["无额度", byStatus.no_quota || 0],
		["失效", byStatus.invalid || 0],
		["隔离", byStatus.quarantined || 0],
		["归档", byStatus.archived || 0],
	];
	$("#inventory-stats").innerHTML = items
		.map(([label, value]) => `<span class="metric"><b>${fmt(value)}</b>${escapeHtml(label)}</span>`)
		.join("");
	$("#inventory-count-badge").textContent = stats.total ?? 0;
	$("#sellable-count-badge").textContent = bySaleState.sellable ?? byStatus.in_stock ?? 0;
	$("#sold-count-badge").textContent = bySaleState.sold ?? byStatus.sold ?? 0;
}

function updateInventorySelectionStats() {
	const selected = selectedInventoryObjs();
	const count = selected.length;
	$("#i-selection-stats").textContent = count ? `已选择 ${count} 个` : "未选择";
	const allVisible = inventoryState.keys.length > 0 && inventoryState.keys.every((r) => inventoryState.selected.has(r.id));
	$("#i-cb-all").checked = allVisible;
	$("#i-cb-all").indeterminate = count > 0 && !allVisible;
	$("#i-cb-all").disabled = inventoryState.keys.length === 0;
	$("#i-btn-select-all").disabled = inventoryState.keys.length === 0 || inventoryState.busy;
	$("#i-btn-select-all").textContent = allVisible ? "取消全选" : "全选";
	const hasSelection = count > 0;
	[
		"#i-btn-recheck",
		"#i-btn-copy",
		"#i-btn-export",
	].forEach((selector) => {
		$(selector).disabled = !hasSelection || inventoryState.busy;
	});
	const setActionEligibility = (selector, eligible, reason) => {
		const button = $(selector);
		button.disabled = inventoryState.busy || !hasSelection || !eligible;
		button.title = hasSelection && !eligible ? reason : "";
	};
	const reserveEligible = hasSelection && selected.every((row) =>
		row.stock_status === "in_stock"
		&& (row.current_check_status || row.latest_check_status) === "valid"
	);
	setActionEligibility(
		"#i-btn-reserve",
		reserveEligible,
		"预留仅接受库存状态为可售且最近质检有效的 Key",
	);
	const restoreEligible = hasSelection && selected.every((row) =>
		!["in_stock", "sold"].includes(row.stock_status)
		&& (row.current_check_status || row.latest_check_status) === "valid"
	);
	setActionEligibility(
		"#i-btn-restore",
		restoreEligible,
		"恢复可售仅接受非可售/非已售且最近质检有效的库存",
	);
	setActionEligibility(
		"#i-btn-quarantine",
		hasSelection && selected.every((row) => !["sold", "quarantined"].includes(row.stock_status)),
		"隔离不接受已售出或已经隔离的库存",
	);
	setActionEligibility(
		"#i-btn-archive",
		hasSelection && selected.every((row) => !["sold", "archived"].includes(row.stock_status)),
		"归档不接受已售出或已经归档的库存",
	);
	const allSellable = hasSelection && selected.every((row) => {
		const checkStatus = row.current_check_status || row.latest_check_status;
		return ["in_stock", "reserved"].includes(row.stock_status) && checkStatus === "valid";
	});
	$("#i-btn-sell").disabled = !allSellable || inventoryState.busy;
	$("#i-btn-sell").title = hasSelection && !allSellable
		? "售出仅接受可售/已预留且最近检测有效的库存"
		: "";
	const allSold = hasSelection && selected.every((row) => row.stock_status === "sold");
	$("#i-btn-return").disabled = !allSold || inventoryState.busy;
	$("#i-btn-return").title = hasSelection && !allSold ? "退回仅接受已售出库存" : "";
	$("#i-btn-bedrock-deep").disabled =
		inventoryState.busy || !selected.some((row) => row.provider === "aws_bedrock");
}

function selectedInventoryObjs() {
	return inventoryState.keys.filter((r) => inventoryState.selected.has(r.id));
}

function renderInventory() {
	const tbody = $("#inventory-table tbody");
	const empty = $("#i-empty");
	tbody.innerHTML = "";
	if (!inventoryState.keys.length) empty.classList.remove("hidden");
	else empty.classList.add("hidden");

	for (const r of inventoryState.keys) {
		const tr = document.createElement("tr");
		tr.dataset.id = r.id;
		tr.className = `stock-row stock-${String(r.stock_status || "unknown").replace(/[^a-z0-9_-]/gi, "")}`;
		const costText = r.unit_cost !== null && r.unit_cost !== undefined
			? fmtMoney(r.unit_cost)
			: fmtMoney(r.batch_total_cost);
		const checkStatus = effectiveCheckStatus(r);
		tr.innerHTML = `
      <td class="col-cb"><label class="checkbox-hitarea"><input type="checkbox" class="i-cb-row" aria-label="选择 ${escapeHtml(r.api_key_short || `第 ${r.id} 行`)}" ${inventoryState.selected.has(r.id) ? "checked" : ""}></label></td>
      <td>${providerBadge(r.provider)}</td>
      <td class="key-cell masked" title="完整 Key 需通过选中复制/导出">${escapeHtml(r.api_key_short)}</td>
      <td>${stockStatusBadge(r.stock_status)}</td>
      <td>${tierBadge(r.tier)}</td>
      <td>${fmt(r.rpm)}</td>
      <td>${fmt(r.tpm)}</td>
      <td>${inventorySupportedModelChips(r)}</td>
      <td>${escapeHtml(r.batch_name || "—")}</td>
      <td>${costText}</td>
      <td>${escapeHtml(r.supplier_name || "—")}</td>
      <td>${escapeHtml(r.buyer || "—")}</td>
      <td class="money-cell">${fmtSaleMoney(r.unit_price_minor, r.currency)}</td>
      <td>${fmtTime(r.sold_at)}</td>
      <td><input class="table-input i-meta" data-field="tags" value="${escapeHtml(r.tags || "")}" placeholder="${escapeHtml(r.batch_tags || "标签")}"></td>
      <td>${checkStatus ? `${statusBadge(checkStatus)} ${fmtTime(r.latest_check_at || r.last_checked_at)}` : fmtTime(r.last_checked_at)}</td>
      <td><input class="table-input i-meta" data-field="note" value="${escapeHtml(r.note || "")}" placeholder="备注"></td>
      <td><button class="btn btn-small i-detail" type="button">详情</button></td>
    `;
		tbody.appendChild(tr);
	}
	updateInventorySelectionStats();
}

async function loadInventory() {
	const loadSeq = ++inventoryState.loadSeq;
	const f = getInventoryFilters();
	const params = new URLSearchParams();
	for (const k in f) if (f[k]) params.set(k, f[k]);
	const data = await api("GET", `/api/inventory?${params.toString()}`);
	if (loadSeq !== inventoryState.loadSeq) return;
	inventoryState.keys = data.keys || [];
	const visibleIds = new Set(inventoryState.keys.map((r) => r.id));
	inventoryState.selected.forEach((id) => {
		if (!visibleIds.has(id)) inventoryState.selected.delete(id);
	});
	renderInventoryStats(data.stats || {});
	renderInventory();
}

function setInventoryBusy(busy) {
	inventoryState.busy = busy;
	updateInventorySelectionStats();
}

async function runInventoryBulkAction(action, confirmText) {
	const ids = [...inventoryState.selected];
	if (!ids.length) return toast("请先选择库存");
	if (confirmText && !confirm(confirmText(ids.length))) return;
	setInventoryBusy(true);
	try {
		const r = await api("POST", "/api/inventory/status", { ids, action });
		toast(`已更新 ${r.updated} 个，跳过 ${r.skipped || 0} 个`);
		inventoryState.selected.clear();
		await loadInventory();
	} catch (e) {
		toast("操作失败：" + e.message);
	} finally {
		setInventoryBusy(false);
	}
}

async function recheckInventorySelected() {
	const ids = [...inventoryState.selected];
	if (!ids.length) return toast("请先选择库存");
	if (!confirm(`确认复检 ${ids.length} 个库存 Key？归档项会跳过。`)) return;
	const concurrency = parseInt($("#concurrency").value, 10) || 4;
	const useProxy = $("#use-proxy").checked;
	setInventoryBusy(true);
	try {
		const r = await api("POST", "/api/inventory/recheck", {
			ids,
			concurrency,
			use_proxy: useProxy,
			mode: "quick",
		});
		toast(`已加入复检 ${r.queued} 个，跳过 ${r.skipped || 0} 个`);
		inventoryState.selected.clear();
		await loadInventory();
		if (r.job_id) startPolling(r.job_id);
	} catch (e) {
		toast("复检失败：" + e.message);
	} finally {
		setInventoryBusy(false);
	}
}

async function deepCheckInventorySelected() {
	const selected = selectedInventoryObjs();
	const ids = bedrockIds(selected);
	if (!ids.length) return toast("请先选择 AWS Bedrock Key");
	const useProxy = $("#use-proxy").checked;
	if (!confirmBedrockDeep(ids.length, selected.length - ids.length, useProxy)) return;
	setInventoryBusy(true);
	try {
		const r = await api("POST", "/api/inventory/recheck", {
			ids,
			concurrency: Math.min(parseInt($("#concurrency").value, 10) || 2, 2),
			use_proxy: useProxy,
			mode: "bedrock_deep",
		});
		toast(`已加入 Bedrock 深检 ${r.queued} 个${r.skipped ? `，跳过 ${r.skipped} 个` : ""}`);
		inventoryState.selected.clear();
		await loadInventory();
		if (r.job_id) startPolling(r.job_id);
	} catch (e) {
		toast("深检失败：" + e.message);
	} finally {
		setInventoryBusy(false);
	}
}

async function exportInventorySelected(
	format = "txt",
	endpoint = "/api/inventory/export",
	requireConfirmation = true,
) {
	const ids = [...inventoryState.selected];
	if (!ids.length) return toast("请先选择库存");
	if (requireConfirmation && !confirm(`确认导出 ${ids.length} 个完整 Key？`)) return null;
	const response = await apiResponse("POST", endpoint, { ids, format });
	return format === "json" ? response.json() : response.text();
}

async function copyInventorySelected() {
	try {
		const text = await exportInventorySelected("txt", "/api/inventory/export", false);
		if (!text) return;
		await copySecretText(text);
		toast(`已复制 ${secretTextLineCount(text)} 行完整 Key`);
	} catch (e) {
		toast("复制失败：" + e.message);
	}
}

async function downloadInventorySelected() {
	try {
		const isSalesExport = inventoryState.mode === "sold";
		const endpoint = isSalesExport ? "/api/sales/export" : "/api/inventory/export";
		const text = await exportInventorySelected("txt", endpoint);
		if (!text) return;
		downloadText(text, `${isSalesExport ? "sales" : "inventory-keys"}-${Date.now()}.txt`);
		toast("导出已开始（操作已审计）");
	} catch (e) {
		toast("导出失败：" + e.message);
	}
}

function openSaleModal() {
	const selected = selectedInventoryObjs();
	if (!selected.length) return toast("请先选择要售出的库存");
	const eligible = selected.every((row) => {
		const checkStatus = row.current_check_status || row.latest_check_status;
		return ["in_stock", "reserved"].includes(row.stock_status) && checkStatus === "valid";
	});
	if (!eligible) return toast("仅可售出可售/已预留且最近检测有效的库存");
	$("#sale-buyer").value = "";
	$("#sale-unit-price").value = "";
	$("#sale-external-ref").value = "";
	$("#sale-note").value = "";
	$("#sale-error").textContent = "";
	$("#sale-error").classList.add("hidden");
	$("#sale-selection-summary").textContent = `将售出 ${selected.length} 个库存 Key；该操作会写入销售记录和库存流水。`;
	openModal($("#sale-modal"), $("#sale-buyer"));
}

function closeSaleModal() {
	closeModal($("#sale-modal"));
}

async function submitSale() {
	const ids = selectedInventoryObjs().map((row) => row.id);
	const buyer = $("#sale-buyer").value.trim();
	if (!buyer) {
		$("#sale-error").textContent = "请填写买家";
		$("#sale-error").classList.remove("hidden");
		$("#sale-buyer").focus();
		return;
	}
	const price = $("#sale-unit-price").value.trim();
	if (price && !/^\d+(?:\.\d{1,2})?$/.test(price)) {
		$("#sale-error").textContent = "售价必须是非负数，最多保留两位小数";
		$("#sale-error").classList.remove("hidden");
		$("#sale-unit-price").focus();
		return;
	}
	$("#sale-submit").disabled = true;
	try {
		const result = await api("POST", "/api/inventory/sell", {
			ids,
			buyer,
			unit_price: price || null,
			currency: "CNY",
			external_ref: $("#sale-external-ref").value.trim() || null,
			note: $("#sale-note").value.trim() || null,
		});
		toast(`已售出 ${result.sold ?? ids.length} 个库存`);
		inventoryState.selected.clear();
		closeSaleModal();
		await loadInventory();
		await refreshInventoryBadge();
	} catch (e) {
		$("#sale-error").textContent = "售出失败：" + e.message;
		$("#sale-error").classList.remove("hidden");
	} finally {
		$("#sale-submit").disabled = false;
	}
}

function openReturnModal() {
	const selected = selectedInventoryObjs();
	if (!selected.length) return toast("请先选择要退回的库存");
	if (!selected.every((row) => row.stock_status === "sold")) {
		return toast("退回仅接受已售出库存");
	}
	$("#return-reason").value = "";
	$("#return-error").textContent = "";
	$("#return-error").classList.add("hidden");
	$("#return-selection-summary").textContent = `将退回 ${selected.length} 个已售库存，并保留原销售历史。`;
	openModal($("#return-modal"), $("#return-reason"));
}

function closeReturnModal() {
	closeModal($("#return-modal"));
}

async function submitReturn() {
	const ids = selectedInventoryObjs().map((row) => row.id);
	const reason = $("#return-reason").value.trim();
	if (!reason) {
		$("#return-error").textContent = "请填写退回原因";
		$("#return-error").classList.remove("hidden");
		$("#return-reason").focus();
		return;
	}
	$("#return-submit").disabled = true;
	try {
		const result = await api("POST", "/api/inventory/return", { ids, reason });
		toast(`已退回 ${result.returned ?? ids.length} 个库存`);
		inventoryState.selected.clear();
		closeReturnModal();
		await loadInventory();
		await refreshInventoryBadge();
	} catch (e) {
		$("#return-error").textContent = "退回失败：" + e.message;
		$("#return-error").classList.remove("hidden");
	} finally {
		$("#return-submit").disabled = false;
	}
}

async function saveInventoryMeta(tr) {
	const id = parseInt(tr.dataset.id, 10);
	const payload = {};
	$$(".i-meta", tr).forEach((input) => {
		payload[input.dataset.field] = input.value.trim() || null;
	});
	const row = inventoryState.keys.find((r) => r.id === id);
	payload.risk_flag = row?.risk_flag || null;
	try {
		await api("POST", `/api/inventory/meta/${id}`, payload);
		if (row) Object.assign(row, payload);
		toast("库存信息已保存");
		await refreshInventoryBadge();
	} catch (e) {
		toast("保存失败：" + e.message);
	}
}

function closeInventoryDetail() {
	closeModal($("#inventory-detail-modal"));
}

function renderInventoryDetail(data) {
	const item = data.item || {};
	const checks = data.check_runs || [];
	const movements = data.movements || [];
	const sales = data.sales || [];
	const checkRows = checks.length
		? checks.map((r) => `
	        <tr>
	          <td>${fmtTime(r.checked_at)}</td>
	          <td>${statusBadge(effectiveCheckStatus(r))}</td>
          <td>${tierBadge(r.tier)}</td>
          <td>${fmt(r.rpm)}</td>
          <td>${fmt(r.tpm)}</td>
          <td>${escapeHtml(r.error || r.source || "—")}</td>
        </tr>
      `).join("")
		: `<tr><td colspan="6">暂无检测历史</td></tr>`;
	const movementRows = movements.length
		? movements.map((m) => `
        <tr>
          <td>${fmtTime(m.created_at)}</td>
          <td>${escapeHtml(m.movement_type)}</td>
          <td>${stockStatusBadge(m.from_status)}</td>
          <td>${stockStatusBadge(m.to_status)}</td>
          <td>${escapeHtml(m.reason || "—")}</td>
        </tr>
      `).join("")
		: `<tr><td colspan="5">暂无库存流水</td></tr>`;
	const saleRows = sales.length
		? sales.map((sale) => `
        <tr>
          <td>${fmtTime(sale.sold_at)}</td>
          <td>${stockStatusBadge(sale.status)}</td>
          <td>${escapeHtml(sale.buyer || "—")}</td>
          <td>${fmtSaleMoney(sale.unit_price_minor, sale.currency)}</td>
          <td>${escapeHtml(sale.external_ref || "—")}</td>
          <td>${fmtTime(sale.returned_at)}</td>
        </tr>
      `).join("")
		: `<tr><td colspan="6">暂无销售历史</td></tr>`;
	$("#inventory-detail-body").innerHTML = `
    <div class="detail-grid">
      <div><b>Key</b><span>${escapeHtml(item.api_key_short || "—")}</span></div>
      <div><b>厂商</b><span>${providerBadge(item.provider)}</span></div>
      <div><b>库存状态</b><span>${stockStatusBadge(item.stock_status)}</span></div>
      <div><b>等级</b><span>${tierBadge(item.tier)}</span></div>
      <div><b>供应商</b><span>${escapeHtml(item.supplier_name || "—")}</span></div>
      <div><b>批次</b><span>${escapeHtml(item.batch_name || "—")}</span></div>
      <div><b>成本</b><span>${fmtMoney(item.unit_cost)}</span></div>
      <div><b>风险</b><span>${escapeHtml(item.risk_flag || "—")}</span></div>
	      <div><b>最近质检</b><span>${statusBadge(effectiveCheckStatus(item))}</span></div>
      <div><b>买家</b><span>${escapeHtml(item.buyer || "—")}</span></div>
      <div><b>售价</b><span>${fmtSaleMoney(item.unit_price_minor, item.currency)}</span></div>
      <div><b>售出时间</b><span>${fmtTime(item.sold_at)}</span></div>
    </div>
    <h3>检测历史</h3>
    <div class="detail-table-wrap">
      <table class="mini-table">
        <thead><tr><th>时间</th><th>结果</th><th>等级</th><th>RPM</th><th>TPM</th><th>说明</th></tr></thead>
        <tbody>${checkRows}</tbody>
      </table>
    </div>
    <h3>库存流水</h3>
    <div class="detail-table-wrap">
      <table class="mini-table">
        <thead><tr><th>时间</th><th>动作</th><th>从</th><th>到</th><th>原因</th></tr></thead>
        <tbody>${movementRows}</tbody>
      </table>
    </div>
    <h3>销售历史</h3>
    <div class="detail-table-wrap">
      <table class="mini-table">
        <thead><tr><th>售出时间</th><th>状态</th><th>买家</th><th>售价</th><th>参考号</th><th>退回时间</th></tr></thead>
        <tbody>${saleRows}</tbody>
      </table>
    </div>
  `;
}

async function openInventoryDetail(id) {
	try {
		$("#inventory-detail-body").innerHTML = `<div class="empty">加载中…</div>`;
		openModal($("#inventory-detail-modal"), $("#inventory-detail-close"));
		const data = await api("GET", `/api/inventory/${id}`);
		renderInventoryDetail(data);
	} catch (e) {
		$("#inventory-detail-body").innerHTML = `<div class="auth-error">加载失败：${escapeHtml(e.message)}</div>`;
	}
}

$$("#i-provider, #i-status, #i-tier, #i-supplier, #i-batch, #i-risk").forEach((el) =>
	el.addEventListener("change", loadInventory),
);

$("#i-cb-all").addEventListener("change", (e) => {
	const checked = e.target.checked;
	inventoryState.keys.forEach((r) =>
		checked ? inventoryState.selected.add(r.id) : inventoryState.selected.delete(r.id),
	);
	renderInventory();
});

$("#i-btn-select-all").addEventListener("click", () => {
	if (inventoryState.keys.length && inventoryState.keys.every((r) => inventoryState.selected.has(r.id))) {
		inventoryState.selected.clear();
	} else {
		inventoryState.keys.forEach((r) => inventoryState.selected.add(r.id));
	}
	renderInventory();
});

$("#inventory-table tbody").addEventListener("click", (e) => {
	const tr = e.target.closest("tr");
	if (!tr) return;
	const id = parseInt(tr.dataset.id, 10);
	if (e.target.classList.contains("i-cb-row")) {
		if (e.target.checked) inventoryState.selected.add(id);
		else inventoryState.selected.delete(id);
		updateInventorySelectionStats();
	} else if (e.target.classList.contains("i-detail")) {
		openInventoryDetail(id);
	}
});

$("#inventory-table tbody").addEventListener("change", (e) => {
	if (e.target.classList.contains("i-meta")) {
		const tr = e.target.closest("tr");
		if (tr) saveInventoryMeta(tr);
	}
});

$("#i-btn-recheck").addEventListener("click", recheckInventorySelected);
$("#i-btn-bedrock-deep").addEventListener("click", deepCheckInventorySelected);
$("#i-btn-reserve").addEventListener("click", () =>
	runInventoryBulkAction("reserve", (n) => `确认预留 ${n} 个可售库存？`),
);
$("#i-btn-restore").addEventListener("click", () =>
	runInventoryBulkAction("restore_to_stock", (n) => `确认把 ${n} 个最近有效的库存恢复为可售？`),
);
$("#i-btn-quarantine").addEventListener("click", () =>
	runInventoryBulkAction("quarantine", (n) => `确认隔离 ${n} 个库存？`),
);
$("#i-btn-archive").addEventListener("click", () =>
	runInventoryBulkAction("archive", (n) => `确认归档 ${n} 个库存？归档后默认不会复检。`),
);
$("#i-btn-copy").addEventListener("click", copyInventorySelected);
$("#i-btn-export").addEventListener("click", downloadInventorySelected);
$("#i-btn-sell").addEventListener("click", openSaleModal);
$("#i-btn-return").addEventListener("click", openReturnModal);
$("#sale-close").addEventListener("click", closeSaleModal);
$("#sale-cancel").addEventListener("click", closeSaleModal);
$("#sale-submit").addEventListener("click", submitSale);
$("#return-close").addEventListener("click", closeReturnModal);
$("#return-cancel").addEventListener("click", closeReturnModal);
$("#return-submit").addEventListener("click", submitReturn);
$("#sale-modal").addEventListener("click", (e) => {
	if (e.target.id === "sale-modal") closeSaleModal();
});
$("#return-modal").addEventListener("click", (e) => {
	if (e.target.id === "return-modal") closeReturnModal();
});
$("#inventory-detail-close").addEventListener("click", closeInventoryDetail);
$("#inventory-detail-modal").addEventListener("click", (e) => {
	if (e.target.id === "inventory-detail-modal") closeInventoryDetail();
});

// ─── tab switching ───────────────────────────────────────────────────
let activeTab = "check";

function switchTab(name) {
	const vaultMode = name === "vault";
	const inventoryMode = INVENTORY_MODES[name] ? name : null;
	const tabChanged = activeTab !== name;
	if (tabChanged && (activeTab === "vault" || vaultMode)) {
		vaultState.selected.clear();
	}
	if (tabChanged && (INVENTORY_MODES[activeTab] || inventoryMode)) {
		inventoryState.selected.clear();
	}

	$$(".tab").forEach((t) => {
		const selected = t.dataset.tab === name;
		t.classList.toggle("active", selected);
		t.setAttribute("aria-selected", String(selected));
		t.tabIndex = selected ? 0 : -1;
	});
	$("#view-check").classList.toggle("hidden", name !== "check");
	$("#view-vault").classList.toggle("hidden", !vaultMode);
	$("#view-inventory").classList.toggle("hidden", !inventoryMode);
	activeTab = name;

	if (vaultMode) {
		vaultState.mode = "vault";
		updateVaultModeContent();
		syncVaultSelectionControls();
		loadVault();
	} else if (tabChanged) {
		syncVaultSelectionControls();
	}
	if (inventoryMode) {
		const modeChanged = inventoryState.mode !== inventoryMode;
		inventoryState.mode = inventoryMode;
		if (modeChanged) {
			inventoryState.keys = [];
			$("#i-status").value = "";
			renderInventory();
		}
		updateInventoryModeContent();
		updateInventorySelectionStats();
		loadInventory();
	} else if (tabChanged) {
		updateInventorySelectionStats();
	}
}
const tabs = $$(".tab");
tabs.forEach((t) => {
	t.addEventListener("click", () => switchTab(t.dataset.tab));
	t.addEventListener("keydown", (e) => {
		const currentIndex = tabs.indexOf(e.currentTarget);
		let nextIndex = null;
		if (e.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length;
		if (e.key === "ArrowLeft") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
		if (e.key === "Home") nextIndex = 0;
		if (e.key === "End") nextIndex = tabs.length - 1;
		if (nextIndex === null) return;
		e.preventDefault();
		tabs[nextIndex].focus();
		tabs[nextIndex].click();
	});
});

async function refreshVaultBadge() {
	try {
		const r = await api("GET", "/api/vault?");
		updateVaultBadges(r.stats || {});
	} catch (e) {}
}

async function refreshInventoryBadge() {
	try {
		const r = await api("GET", "/api/inventory?sale_view=all");
		const stats = r.stats || {};
		const saleState = stats.by_sale_state || {};
		$("#inventory-count-badge").textContent = stats.total ?? r.count ?? 0;
		$("#sellable-count-badge").textContent = saleState.sellable ?? 0;
		$("#sold-count-badge").textContent = saleState.sold ?? stats.by_status?.sold ?? 0;
	} catch (e) {}
}

// ─── auth gate ──────────────────────────────────────────────────────
function setAuthBackgroundLocked(locked) {
	$$(authBackgroundSelector).forEach((element) => {
		element.inert = locked;
	});
	document.body.classList.toggle("auth-open", locked);
}

function showAuthOverlay() {
	if (modalState.active) closeModal(modalState.active);
	const overlay = $("#auth-overlay");
	overlay.classList.remove("hidden");
	overlay.setAttribute("aria-hidden", "false");
	setAuthBackgroundLocked(true);
	$("#auth-input").value = "";
	$("#auth-error").classList.add("hidden");
	requestAnimationFrame(() => $("#auth-input").focus());
}

function hideAuthOverlay() {
	const overlay = $("#auth-overlay");
	overlay.classList.add("hidden");
	overlay.setAttribute("aria-hidden", "true");
	setAuthBackgroundLocked(false);
	if (overlay.contains(document.activeElement)) $("#tab-check").focus();
}

async function attemptLogin() {
	const key = $("#auth-input").value.trim();
	if (!key) return;
	try {
		const r = await fetch("/api/auth", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ key }),
		});
		if (r.ok) {
			sessionStorage.setItem("admin_key", key);
			hideAuthOverlay();
			bootApp();
		} else {
			$("#auth-error").classList.remove("hidden");
			$("#auth-input").select();
		}
	} catch (e) {
		$("#auth-error").classList.remove("hidden");
	}
}

$("#auth-btn").addEventListener("click", attemptLogin);
$("#auth-input").addEventListener("keydown", (e) => {
	if (e.key === "Enter") attemptLogin();
});

async function bootApp() {
	await loadKeys();
	await refreshVaultBadge();
	await refreshInventoryBadge();
	try {
		const r = await api("GET", "/api/jobs/running");
		if (r.jobs && r.jobs.length) startPolling(r.jobs[0].id);
	} catch (e) {}
}

syncCheckSelectionControls();
syncVaultSelectionControls();
updateInventoryModeContent();
updateInventorySelectionStats();
setAuthBackgroundLocked(true);

// Auto-login if session key still valid, otherwise show overlay
(async () => {
	if (getAdminKey()) {
		try {
			await api("GET", "/api/keys");
			hideAuthOverlay();
			bootApp();
		} catch (e) {
			showAuthOverlay();
		}
	} else {
		showAuthOverlay();
	}
})();
