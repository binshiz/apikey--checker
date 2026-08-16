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
	azure_openai: { className: "azure-openai", label: "AZURE OPENAI" },
	anthropic: { className: "anthropic", label: "ANTHROPIC" },
	gemini: { className: "gemini", label: "GEMINI" },
	aws_bedrock: { className: "aws-bedrock", label: "AWS BEDROCK" },
	gcp_service_account: { className: "gcp-service-account", label: "GCP SERVICE ACCOUNT" },
	openrouter: { className: "openrouter", label: "OPENROUTER" },
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

function bedrockGatewayMappingChip(mapping) {
	if (!mapping || Array.isArray(mapping) || typeof mapping !== "object") return "";
	const entries = Object.entries(mapping).filter(([alias, target]) => alias && target);
	if (!entries.length) return "";
	const mappingJson = JSON.stringify(Object.fromEntries(entries), null, 2);
	return `<button type="button" class="chip chip-action bedrock-gateway-copy" data-gateway-json="${escapeHtml(mappingJson)}" title="${escapeHtml(`模型成功路由汇总；每项至少在一个区域成功，不保证适用于所有区域：\n${mappingJson}`)}">模型汇总 JSON ×${entries.length}</button>`;
}

function bedrockGatewayGroupChip(group) {
	const regions = Array.isArray(group?.regions) ? group.regions : [];
	if (!regions.length) return "";
	const mapping = group?.mapping && !Array.isArray(group.mapping) && typeof group.mapping === "object"
		? group.mapping
		: {};
	const routeGroups = Array.isArray(group?.route_groups) ? group.route_groups : [];
	const mappingPayload = Object.keys(mapping).length
		? mapping
		: { route_groups: routeGroups };
	const mappingJson = JSON.stringify(mappingPayload, null, 2);
	const groupLabel = group.kind === "fable_5" ? "Fable 5 组" : "其他模型组";
	const gatewayLabel = Object.keys(mapping).length
		? `公共网关 ${Object.keys(mapping).length} 模型`
		: `兼容网关 ${routeGroups.length} 组`;
	return `<button type="button" class="chip ${group.kind === "fable_5" ? "on " : ""}chip-action bedrock-gateway-copy" data-gateway-json="${escapeHtml(mappingJson)}" title="${escapeHtml(`区域：\n${regions.join("\n")}\n\n点击复制与这些区域兼容的网关映射：\n${mappingJson}`)}">${groupLabel} ${regions.length} 区域 · ${gatewayLabel}</button>`;
}

function bedrockPrimaryModelLabel(alias) {
	return String(alias || "")
		.replace(/^claude-fable-/, "Fable ")
		.replace(/^claude-opus-/, "Opus ")
		.replaceAll("-", ".");
}

function bedrockRegionModelChip(byRegion, primaryModel, primaryRegions, regionGroups) {
	if (!byRegion || Array.isArray(byRegion) || typeof byRegion !== "object") return "";
	const regions = Object.entries(byRegion).filter(([, mapping]) =>
		mapping && !Array.isArray(mapping) && typeof mapping === "object" && Object.keys(mapping).length
	);
	if (!regions.length) return "";
	const payload = {
		primary_model: primaryModel || null,
		primary_regions: Array.isArray(primaryRegions) ? primaryRegions : [],
		export_groups: Array.isArray(regionGroups) ? regionGroups : [],
		models_by_region: Object.fromEntries(regions),
	};
	const payloadJson = JSON.stringify(payload, null, 2);
	return `<button type="button" class="chip chip-action bedrock-gateway-copy" data-gateway-json="${escapeHtml(payloadJson)}" title="${escapeHtml(`点击复制完整的区域 → 模型支持矩阵：\n${payloadJson}`)}">区域模型 JSON ×${regions.length}</button>`;
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

function azureOpenAIModelChips(e, compact = false) {
	const probes = e.target_model_probes || {};
	const labels = ["gpt-5.5", "gpt-5.6"];
	if (Object.keys(probes).length) {
		return labels.map((label) => {
			const probe = probes[label] || {};
			const attempts = Array.isArray(probe.attempts) ? probe.attempts : [];
			const tried = attempts.map((item) => {
				const suffix = item.http_status
					? `HTTP ${item.http_status}${item.error_code ? ` ${item.error_code}` : ""}`
					: item.status || "unknown";
				return `${item.deployment || "?"}: ${suffix}`;
			});
			const catalog = Array.isArray(probe.catalog_models) ? probe.catalog_models : [];
			const title = [
				probe.deployment ? `部署名：${probe.deployment}` : "",
				probe.response_model ? `实际模型：${probe.response_model}` : "",
				catalog.length ? `目录可见：\n${catalog.join("\n")}` : "",
				tried.length && !compact ? `探测记录：\n${tried.join("\n")}` : "",
				probe.custom_deployment_name_required
					? "若 Azure 中使用了自定义部署名，仅凭资源 Key 无法自动发现。"
					: "",
			].filter(Boolean).join("\n\n");
			const presentation = {
				callable: [`${label} 可调用`, "on", title],
				rate_limited: [`${label} 已部署·限流`, "warn", title],
				deployment_not_found: [`${label} 未发现部署`, "off", title],
				unverified: [`${label} 调用未确认`, "warn", title],
				authentication_failed: [`${label} 鉴权失败`, "off", title],
				access_denied: [`${label} 访问拒绝`, "off", title],
				request_rejected: [`${label} 请求被拒`, "off", title],
			};
			return chip(...(presentation[probe.status] || [`${label} 未验证`, "off", title]));
		});
	}

	// Old saved results only proved catalog visibility. Render that distinction
	// explicitly until the row is rechecked with runtime probing.
	const targets = displayTargetsFromSupported(e.model_catalog || e.supported_models || {});
	return targets
		.filter((target) => labels.includes(target.label))
		.map((target) => chip(
			target.supported ? `${target.label} 仅目录可见` : `${target.label} 目录未见`,
			target.supported ? "warn" : "off",
			target.model || "尚未执行真实部署调用，请重新检测。",
		));
}

function targetModelChips(supported, excludedLabels = []) {
	if (!supported) return [];

	const allDisplayTargets = Array.isArray(supported.display_targets)
		? supported.display_targets
		: [];
	const excluded = new Set(excludedLabels);
	const displayTargets = allDisplayTargets.filter((target) => !excluded.has(target.label));
	if (!displayTargets.length) return [];

	const title = Array.isArray(supported.models_preview)
		? supported.models_preview.join("\n")
		: "";
	const supportedCount = allDisplayTargets.filter((target) => target.supported).length;
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

function gcpModelProbeChips(extra, compact = false) {
	const geminiResults = Array.isArray(extra.model_probe_results)
		? extra.model_probe_results
		: [];
	const geminiSupported = Array.isArray(extra.supported_models)
		? extra.supported_models
		: [];
	const claudeResults = Array.isArray(extra.claude_model_probe_results)
		? extra.claude_model_probe_results
		: [];
	const claudeSupported = Array.isArray(extra.claude_supported_models)
		? extra.claude_supported_models
		: [];

	if (compact) {
		const visible = geminiSupported.slice(0, 2).map((model) =>
			chip(model, "on model-chip", "Vertex AI Gemini generateContent 真实调用成功")
		);
		visible.push(...claudeSupported.slice(0, 2).map((model) =>
			chip(model, "on model-chip", "Vertex AI Claude countTokens 权限检测通过（非生成调用）")
		));
		const allSupported = [...geminiSupported, ...claudeSupported];
		if (allSupported.length > visible.length) {
			visible.push(chip(
				`+${allSupported.length - visible.length}`,
				"model-more",
				`Gemini\n${geminiSupported.join("\n") || "—"}\n\nClaude\n${claudeSupported.join("\n") || "—"}`,
			));
		}
		if (!visible.length && extra.token_exchange === "success") {
			visible.push(chip("无模型权限证明", "warn", "OAuth 有效，但没有任何目标 Gemini 调用成功或 Claude 权限检测通过。"));
		}
		return visible;
	}

	const labels = {
		callable: ["可调用", "on"],
		permission_granted: ["权限通过", "on"],
		rate_limited: ["限流", "warn"],
		permission_denied: ["无权限", "off"],
		not_found: ["不可用", "off"],
		request_rejected: ["请求被拒", "off"],
		authentication_failed: ["鉴权失败", "off"],
		network_error: ["网络错误", "warn"],
		upstream_error: ["上游错误", "warn"],
		http_error: ["HTTP 错误", "off"],
		error: ["检测错误", "warn"],
	};
	const probeChip = (probe, provider, method) => {
		const [statusText, className] = labels[probe.status] || ["未确认", "warn"];
		const http = probe.http_status ? `HTTP ${probe.http_status}` : "未收到 HTTP 响应";
		return chip(
			`${probe.model} · ${statusText}`,
			`${className} model-chip`,
			`Vertex AI ${extra.vertex_location || "global"} · ${provider} · ${method}\n${http}`,
		);
	};
	const chips = [];
	if (geminiResults.length) {
		chips.push(...geminiResults.map((probe) =>
			probeChip(probe, "Gemini", "generateContent 真实调用")
		));
	} else if (extra.token_exchange === "success") {
		chips.push(chip("Gemini 需复检", "warn", "该记录尚未执行 Vertex AI Gemini 真实生成调用。"));
	}
	if (claudeResults.length) {
		chips.push(...claudeResults.map((probe) =>
			probeChip(probe, "Claude", "countTokens 权限检测（非生成调用）")
		));
	} else if (extra.token_exchange === "success") {
		chips.push(chip("Claude 需复检", "warn", "该记录尚未执行 Vertex AI Claude countTokens 权限检测。"));
	}
	return chips;
}

function bedrockDetailChipList(extra, compact = false) {
	const chips = [];
	const identity = extra.identity || {};
	const credentialType = extra.credential_type || "";
	const isBearerApiKey = credentialType === "bedrock_api_key";
	const isDeep = extra.check_mode === "bedrock_deep" || extra.check_mode === "deep";
	const mode = isDeep
		? "全区域深检"
		: isBearerApiKey
			? "API Key 快速检测"
			: "快速检测";
	chips.push(chip(mode, isDeep ? "aws-deep" : ""));
	if (isBearerApiKey) chips.push(chip("Bearer API Key", "aws-deep"));

	const credentialStatus = extra.credential_status || extra.credentials_status || identity.status;
	if (credentialStatus) {
		const normalizedCredentialStatus = String(credentialStatus).toLowerCase();
		const credentialLabels = {
			verified: "STS 已验证",
			bedrock_verified: "Bedrock 已验证",
			sts_unavailable: "STS 未验证",
			access_denied: "全部区域拒绝访问",
			throttled: "API Key 已验证 · 限流",
			unverified: "API Key 未验证",
			invalid: "凭证失效",
			invalid_format: "格式错误",
		};
		const credentialTone = normalizedCredentialStatus === "throttled"
			? "warn"
			: ["valid", "verified", "bedrock_verified"].includes(normalizedCredentialStatus)
				? "on"
				: "off";
		chips.push(chip(
			credentialLabels[normalizedCredentialStatus] || `凭证 ${credentialStatus}`,
			credentialTone,
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
	const primaryModel = extra.gateway_primary_model || "";
	const primaryRegions = Array.isArray(extra.gateway_primary_regions)
		? extra.gateway_primary_regions
		: [];
	const gatewayRegionGroups = Array.isArray(extra.gateway_region_groups)
		? extra.gateway_region_groups
		: [];
	if (primaryModel && primaryRegions.length) {
		chips.push(chip(
			`首组 ${bedrockPrimaryModelLabel(primaryModel)} · ${primaryRegions.length} 区域`,
			"on",
			`复制时优先列出以下真实调用成功区域：\n${primaryRegions.join("\n")}`,
		));
	}
	for (const group of gatewayRegionGroups) {
		const groupChip = bedrockGatewayGroupChip(group);
		if (groupChip) chips.push(groupChip);
	}
	const gatewayMappingChip = bedrockGatewayMappingChip(extra.gateway_mapping);
	if (gatewayMappingChip) chips.push(gatewayMappingChip);
	const regionModelChip = bedrockRegionModelChip(
		extra.gateway_mappings_by_region,
		primaryModel,
		primaryRegions,
		gatewayRegionGroups,
	);
	if (regionModelChip) chips.push(regionModelChip);
	const authorizedRegions = Array.isArray(modelSummary.authorized_regions)
		? modelSummary.authorized_regions
		: [];
	const deniedRegions = Array.isArray(modelSummary.denied_regions)
		? modelSummary.denied_regions
		: [];
	const apiThrottledRegions = Array.isArray(modelSummary.api_throttled_regions)
		? modelSummary.api_throttled_regions
		: [];
	if (isBearerApiKey && authorizedRegions.length) {
		chips.push(chip(
			`目录授权区域 ${authorizedRegions.length}`,
			"on",
			`以下区域的 Claude 模型目录接口认证成功；真实调用成功区域请以“首组”和“任意模型成功区域”为准：\n${authorizedRegions.join("\n")}`,
		));
	}
	if (isBearerApiKey && deniedRegions.length && !compact) {
		chips.push(chip(
			`无权限区域 ${deniedRegions.length}`,
			"off",
			deniedRegions.join("\n"),
		));
	}
	if (isBearerApiKey && apiThrottledRegions.length) {
		chips.push(chip(
			`区域接口限流 ${apiThrottledRegions.length}`,
			"warn",
			apiThrottledRegions.join("\n"),
		));
	}
	const catalogModelCount = Number(modelSummary.catalog_model_count || 0);
	if (isBearerApiKey && catalogModelCount && !compact) {
		const catalogModels = Array.isArray(modelSummary.catalog_models)
			? modelSummary.catalog_models
			: [];
		chips.push(chip(
			`模型目录 ×${catalogModelCount}`,
			"",
			catalogModels.join("\n"),
		));
	}
	const fable5 = !Array.isArray(modelSummary) && modelSummary.fable_5 && typeof modelSummary.fable_5 === "object"
		? modelSummary.fable_5
		: null;
	if (fable5) {
		const discovered = Array.isArray(fable5.discovered_models) ? fable5.discovered_models : [];
		const successful = Array.isArray(fable5.successful_models) ? fable5.successful_models : [];
		const successfulFableRegions = Array.isArray(fable5.successful_regions) ? fable5.successful_regions : [];
		const throttled = Array.isArray(fable5.throttled_models) ? fable5.throttled_models : [];
		const failures = Array.isArray(fable5.failure_codes) ? fable5.failure_codes : [];
		const fableTitle = [
			discovered.length ? `发现模型：\n${discovered.join("\n")}` : "",
			successful.length ? `成功调用：\n${successful.join("\n")}` : "",
			successfulFableRegions.length ? `成功区域：\n${successfulFableRegions.join("\n")}` : "",
			throttled.length ? `限流模型：\n${throttled.join("\n")}` : "",
			failures.length ? `错误码：\n${failures.join("\n")}` : "",
		].filter(Boolean).join("\n\n");
		const fablePresentation = {
			supported: [`Fable 5 可调用 · ${successfulFableRegions.length} 区域`, "on", fableTitle],
			throttled: ["Fable 5 可用 · 限流", "warn", fableTitle],
			data_retention_required: [
				"Fable 5 需配置数据保留",
				"warn",
				"AWS 要求先将 Bedrock 数据保留模式设为 provider_data_share；本工具不会自动修改该账户设置。",
			],
			not_supported: ["Fable 5 无调用权限", "off", fableTitle],
			not_discovered: ["Fable 5 未发现", "off", "本次扫描没有发现 Claude Fable 5 模型或 inference profile。"],
			not_invoked: ["Fable 5 未完成调用", "off", fableTitle],
			check_failed: ["Fable 5 未确认", "off", fableTitle],
		};
		const presentation = fablePresentation[fable5.status];
		if (presentation) chips.push(chip(...presentation));
	}
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
		chips.push(chip(
			`任意模型成功区域 ${successfulRegions.length}`,
			"",
			`这是所有成功模型的区域并集，不用于复制主模型 Key：\n${successfulRegions.join("\n")}`,
		));
	}
	if (!isBearerApiKey && discoveredRegions.length && !compact) {
		chips.push(chip(`发现区域 ${discoveredRegions.length}`, "", discoveredRegions.join("\n")));
	}
	if (modelCount) {
		chips.push(chip(`Claude 模型 ×${modelCount}`, "", Array.isArray(modelList) ? modelList.join("\n") : ""));
	} else if (latestModel) chips.push(chip(String(latestModel), "on", String(latestModel)));
	else if (successfulVersions.length) {
		chips.push(chip(`可调用 Opus ×${successfulVersions.length}`, "on", successfulVersions.join("\n")));
	}
	if (profilesFound && !compact) chips.push(chip(`Profiles ${profilesFound}`));
	if (throttledRegions.length) {
		const versionLabel = throttledVersions.length
			? `Claude Opus ${throttledVersions.join("/")}`
			: "Claude";
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
					: "没有任何目标 Claude 模型完成真实 InvokeModel 调用",
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

function openRouterChips(extra, compact = false) {
	const chips = [];
	if (extra.credential_status === "valid" && typeof extra.is_free_tier === "boolean") {
		const accountLabel = extra.is_free_tier ? "OpenRouter Free" : "OpenRouter Paid";
		chips.push(chip(
			accountLabel,
			"on",
			"由 OpenRouter 当前 Key 信息接口返回",
		));
	}

	if (extra.is_management_key || extra.is_provisioning_key) {
		const keyType = extra.is_management_key ? "Management Key" : "Provisioning Key";
		chips.push(chip(keyType, "off", "该凭证不能作为普通推理 API Key 入库"));
	}

	if (extra.account_balance !== undefined && extra.account_balance !== null) {
		const balance = Number(extra.account_balance);
		const creditsTitle = [
			extra.account_total_credits !== undefined
				? `账号总额度：$${fmtMoney(extra.account_total_credits)}`
				: "",
			extra.account_total_usage !== undefined
				? `账号已使用：$${fmtMoney(extra.account_total_usage)}`
				: "",
			`账号剩余额度：$${fmtMoney(extra.account_balance)}`,
			"数据来自 OpenRouter /api/v1/credits。",
		].filter(Boolean).join("\n");
		if (compact) {
			chips.push(chip(
				`账号余额 $${fmtMoney(extra.account_balance)}`,
				Number.isFinite(balance) && balance <= 0 ? "warn" : "on",
				creditsTitle,
			));
		} else {
			if (extra.account_total_credits !== undefined) {
				chips.push(chip(
					`账号总额度 $${fmtMoney(extra.account_total_credits)}`,
					"",
					creditsTitle,
				));
			}
			if (extra.account_total_usage !== undefined) {
				chips.push(chip(
					`账号已使用 $${fmtMoney(extra.account_total_usage)}`,
					"",
					creditsTitle,
				));
			}
			chips.push(chip(
				`账号剩余额度 $${fmtMoney(extra.account_balance)}`,
				Number.isFinite(balance) && balance <= 0 ? "warn" : "on",
				creditsTitle,
			));
		}
	} else if (extra.credits_status && extra.credits_status !== "success") {
		chips.push(chip(
			"账号额度读取失败",
			"off",
			extra.credits_error || extra.credits_status,
		));
	}

	const keyLimitConfigured = extra.key_limit_configured === true
		|| (extra.limit !== undefined && extra.limit !== null);
	const keyLimitKnownUnconfigured = extra.key_limit_configured === false
		|| (
			extra.credential_status === "valid"
			&& !extra.is_management_key
			&& extra.limit === undefined
			&& extra.limit_remaining === undefined
		);
	if (extra.limit_remaining !== undefined && extra.limit_remaining !== null) {
		const remaining = Number(extra.limit_remaining);
		const limitText = extra.limit !== undefined && extra.limit !== null
			? `Key 限额 $${fmtMoney(extra.limit)}`
			: "Key 未设置固定限额";
		chips.push(chip(
			`剩余 $${fmtMoney(extra.limit_remaining)}`,
			Number.isFinite(remaining) && remaining <= 0 ? "warn" : "",
			`${limitText}${extra.limit_reset ? ` · ${extra.limit_reset} 重置` : ""}`,
		));
	} else if (keyLimitConfigured && extra.limit !== undefined && extra.limit !== null) {
		chips.push(chip(
			`Key 限额 $${fmtMoney(extra.limit)} · 剩余未知`,
			"warn",
			"OpenRouter 没有返回 limit_remaining。",
		));
	} else if (keyLimitKnownUnconfigured) {
		chips.push(chip(
			"Key 未设消费上限",
			"",
			"该 Key 的 limit 和 limit_remaining 均为空；账号额度以单独的 /api/v1/credits 查询结果为准。",
		));
	}

	if (!compact && extra.usage !== undefined && extra.usage !== null) {
		const usageTitle = [
			extra.usage_daily !== undefined ? `今日：$${fmtMoney(extra.usage_daily)}` : "",
			extra.usage_weekly !== undefined ? `本周：$${fmtMoney(extra.usage_weekly)}` : "",
			extra.usage_monthly !== undefined ? `本月：$${fmtMoney(extra.usage_monthly)}` : "",
		].filter(Boolean).join("\n");
		chips.push(chip(`累计用量 $${fmtMoney(extra.usage)}`, "", usageTitle));
	}

	if (!compact && extra.expires_at) {
		const expiry = new Date(extra.expires_at);
		const validDate = !Number.isNaN(expiry.getTime());
		const label = validDate ? expiry.toLocaleDateString() : extra.expires_at;
		const expired = validDate && expiry.getTime() <= Date.now();
		chips.push(chip(`到期 ${label}`, expired ? "off" : "", extra.expires_at));
	}

	const opus5 = extra.opus5_probe || {};
	const opus5Presentation = {
		callable: ["Claude Opus 5 可调用", "on model-chip", opus5.resolved_model || opus5.model || ""],
		rate_limited: ["Opus 5 已授权 · 限流", "warn", opus5.error || "HTTP 429"],
		no_quota: ["Opus 5 无付费额度", "warn", opus5.error || "HTTP 402"],
		access_denied: ["Opus 5 无权限", "off", opus5.error || "HTTP 403"],
		model_unavailable: ["Opus 5 模型不可用", "off", opus5.error || "HTTP 404"],
		authentication_failed: ["Opus 5 鉴权失败", "off", opus5.error || "HTTP 401"],
		timeout: ["Opus 5 探测超时", "off", opus5.error || "timeout"],
		network_error: ["Opus 5 网络错误", "off", opus5.error || "network error"],
		upstream_error: ["Opus 5 上游错误", "off", opus5.error || "upstream error"],
		request_rejected: ["Opus 5 请求被拒", "off", opus5.error || "request rejected"],
	};
	if (opus5Presentation[opus5.status]) {
		chips.push(chip(...opus5Presentation[opus5.status]));
	}

	const fable5 = extra.fable5_probe || {};
	const fable5Presentation = {
		callable: ["Claude Fable 5 可调用", "on model-chip", fable5.resolved_model || fable5.model || ""],
		rate_limited: ["Fable 5 已授权 · 限流", "warn", fable5.error || "HTTP 429"],
		no_quota: ["Fable 5 无付费额度", "warn", fable5.error || "HTTP 402"],
		access_denied: ["Fable 5 无权限", "off", fable5.error || "HTTP 403"],
		model_unavailable: ["Fable 5 模型不可用", "off", fable5.error || "HTTP 404"],
		authentication_failed: ["Fable 5 鉴权失败", "off", fable5.error || "HTTP 401"],
		timeout: ["Fable 5 探测超时", "off", fable5.error || "timeout"],
		network_error: ["Fable 5 网络错误", "off", fable5.error || "network error"],
		upstream_error: ["Fable 5 上游错误", "off", fable5.error || "upstream error"],
		request_rejected: ["Fable 5 请求被拒", "off", fable5.error || "request rejected"],
	};
	if (fable5Presentation[fable5.status]) {
		chips.push(chip(...fable5Presentation[fable5.status]));
	}

	if (extra.invocation_verification === "success") {
		if (opus5.status !== "callable" && fable5.status !== "callable") {
			const resolved = extra.resolved_model || extra.requested_model || "openrouter/free";
			const tokenUsage = extra.token_usage || {};
			const usageTitle = tokenUsage.total_tokens !== undefined
				? `本次探测使用 ${tokenUsage.total_tokens} tokens`
				: "最小免费生成调用成功";
			chips.push(chip(`Key 可调用 · ${resolved}`, "on model-chip", usageTitle));
		}
	} else if (extra.invocation_verification === "quota_limited") {
		chips.push(chip("免费探测限流/无额度", "warn"));
	} else if (extra.invocation_verification && extra.invocation_verification !== "not_applicable") {
		chips.push(chip(
			`调用未通过 · ${extra.invocation_verification}`,
			"off",
			extra.failure_stage || "runtime_probe",
		));
	}
	return chips;
}

function anthropicOpus5ProbeChips(extra) {
	const probe = extra.opus5_probe || {};
	const presentation = {
		callable: ["Claude Opus 5 可调用", "on model-chip", probe.resolved_model || probe.model || ""],
		rate_limited: ["Opus 5 已授权 · 限流", "warn", probe.error || "HTTP 429"],
		no_quota: ["Opus 5 无额度", "warn", probe.error || "账户没有可用额度"],
		access_denied: ["Opus 5 无权限", "off", probe.error || "HTTP 403"],
		model_unavailable: ["Opus 5 模型不可用", "off", probe.error || "HTTP 404"],
		authentication_failed: ["Opus 5 鉴权失败", "off", probe.error || "HTTP 401"],
		timeout: ["Opus 5 探测超时", "off", probe.error || "timeout"],
		network_error: ["Opus 5 网络错误", "off", probe.error || "network error"],
		upstream_error: ["Opus 5 上游错误", "off", probe.error || "upstream error"],
		request_rejected: ["Opus 5 请求被拒", "off", probe.error || "request rejected"],
	};
	return presentation[probe.status] ? [chip(...presentation[probe.status])] : [];
}

function anthropicQuotaChips(extra, compact = false) {
	const chips = [];
	const creditPresentation = {
		available: [
			"调用额度可用 · 金额未知",
			"on",
			"最小付费调用已成功。普通 Anthropic API Key 不提供美元余额查询，因此只能确认当前可调用。",
		],
		depleted: [
			"调用额度不足",
			"warn",
			"Anthropic 返回了 credit balance、billing 或 payment required 类错误。",
		],
	};
	if (creditPresentation[extra.credit_status]) {
		chips.push(chip(...creditPresentation[extra.credit_status]));
	}

	const windows = [];
	const baseWindow = extra.rate_limit_window || {};
	if (baseWindow.limits && typeof baseWindow.limits === "object") {
		windows.push({
			model: baseWindow.model || extra.probe_model || "探测模型",
			limits: baseWindow.limits,
		});
	}
	const opusProbe = extra.opus5_probe || {};
	if (opusProbe.rate_limits && typeof opusProbe.rate_limits === "object") {
		windows.push({
			model: opusProbe.resolved_model || opusProbe.model || "Claude Opus 5",
			limits: opusProbe.rate_limits,
		});
	}

	const dimensions = [
		["requests", "请求"],
		["input_tokens", "输入 Token"],
		["output_tokens", "输出 Token"],
		["tokens", "Token"],
	];
	for (const windowInfo of windows) {
		const modelLabel = String(windowInfo.model).includes("opus-5")
			? "Opus 5"
			: compact ? "当前窗" : "探测模型";
		for (const [field, label] of dimensions) {
			const value = windowInfo.limits[field];
			if (!value || value.remaining === undefined || value.remaining === null) continue;
			const remaining = Number(value.remaining);
			const limit = Number(value.limit);
			const hasLimit = Number.isFinite(limit);
			const resetText = value.reset ? `\n重置时间：${value.reset}` : "";
			const title = [
				`模型：${windowInfo.model}`,
				`${label}当前窗口剩余：${fmt(remaining)}`,
				hasLimit ? `窗口上限：${fmt(limit)}` : "",
				"这是限速窗口的瞬时剩余量，不是账户美元余额。",
			].filter(Boolean).join("\n") + resetText;
			chips.push(chip(
				`${modelLabel} ${label}余 ${fmt(remaining)}${hasLimit ? `/${fmt(limit)}` : ""}`,
				Number.isFinite(remaining) && remaining <= 0 ? "warn" : "",
				title,
			));
		}
	}
	return chips;
}

function openaiRateLimitChips(extra) {
	const chips = [];
	const windows = Array.isArray(extra.rate_limit_windows)
		? extra.rate_limit_windows
		: extra.rate_limit_window
			? [extra.rate_limit_window]
			: [];
	const dimensions = [
		["requests", "RPM"],
		["tokens", "TPM"],
		["project_tokens", "项目 TPM"],
	];

	for (const windowInfo of windows) {
		const limits = windowInfo?.limits;
		if (!limits || typeof limits !== "object") continue;
		for (const [field, label] of dimensions) {
			const value = limits[field];
			if (!value || typeof value !== "object") continue;
			const remaining = Number(value.remaining);
			const limit = Number(value.limit);
			const hasRemaining = Number.isFinite(remaining);
			const hasLimit = Number.isFinite(limit);
			if (!hasRemaining && !hasLimit && !value.reset) continue;

			let windowText = label;
			if (hasRemaining) {
				windowText += ` 余 ${fmt(remaining)}${hasLimit ? `/${fmt(limit)}` : ""}`;
			} else if (hasLimit) {
				windowText += ` ${fmt(limit)}`;
			} else if (value.reset) {
				windowText += ` 重置 ${value.reset}`;
			}
			const endpoint = windowInfo.endpoint || "runtime";
			const title = [
				`模型：${windowInfo.model || "未知"}`,
				`接口：${endpoint}`,
				hasLimit ? `${label} 上限：${fmt(limit)}` : "",
				hasRemaining ? `${label} 当前剩余：${fmt(remaining)}` : "",
				value.reset ? `重置时间：${value.reset}` : "",
				"这是当前限流窗口，不是账户美元余额。",
			].filter(Boolean).join("\n");
			chips.push(chip(
				windowText,
				hasRemaining && remaining <= 0 ? "warn" : "",
				title,
			));
		}
	}
	return chips;
}

function openai429Chips(extra) {
	const chips = [];
	const quotaPresentation = {
		insufficient_quota: ["调用额度不足", "账户没有可用于模型调用的额度。"],
		credit_balance_exhausted: ["预付余额耗尽", "OpenAI 返回 credit_balance_exhausted。"],
		organization_spend_limit_exceeded: ["组织消费上限已达到", "需要调整组织 Spend Limit。"],
		project_spend_limit_exceeded: ["项目消费上限已达到", "需要调整项目 Spend Limit。"],
		organization_usage_limit_exceeded: ["组织 Usage Limit 已达到", "需要申请更高的组织 Usage Limit。"],
	};
	if (extra.quota_reason && quotaPresentation[extra.quota_reason]) {
		const [label, title] = quotaPresentation[extra.quota_reason];
		chips.push(chip(label, "warn", title));
	}

	const observation = extra.rate_limit_observation;
	if (observation && typeof observation === "object") {
		const metric = observation.dimension === "tokens"
			? "TPM"
			: observation.dimension === "requests"
				? "RPM"
				: "限流";
		const limit = Number(observation.limit);
		const used = Number(observation.used);
		const requested = Number(observation.requested);
		const title = [
			`模型：${observation.model || "未知"}`,
			`接口：${observation.endpoint || "runtime"}`,
			Number.isFinite(limit) ? `${metric} 上限：${fmt(limit)}` : "",
			Number.isFinite(used) ? `已使用：${fmt(used)}` : "",
			Number.isFinite(requested) ? `本次请求：${fmt(requested)}` : "",
			observation.retry_after || extra.retry_after
				? `建议重试：${observation.retry_after || extra.retry_after} 后`
				: "",
			extra.request_id ? `请求 ID：${extra.request_id}` : "",
			"该数值从 429 错误正文提取，不是稳定的官方 Header。",
		].filter(Boolean).join("\n");
		chips.push(chip(
			`429 正文 ${metric}${Number.isFinite(limit) ? ` ${fmt(limit)}` : ""}`,
			"warn",
			title,
		));
	} else if (
		extra.invocation_verification === "rate_limited"
		&& extra.retry_after
	) {
		chips.push(chip(
			`${extra.retry_after} 后重试`,
			"warn",
			`OpenAI 返回 Retry-After/重试时间；这只能说明等待时长，不能推算 TPM。${extra.request_id ? `\n请求 ID：${extra.request_id}` : ""}`,
		));
	}
	return chips;
}

function openaiTierReasonChip(extra) {
	if (extra.tier_reason_code === "rate_limited_retry_after") {
		const retryAfter = extra.retry_after || "稍后";
		return chip(
			`有效 · 探测收到 429，${retryAfter} 后重试`,
			"warn",
			`Key 鉴权有效；本轮最小模型调用探测收到 HTTP 429，服务端建议 ${retryAfter} 后重试。这不代表整条 Key 当前全面限流。`,
		);
	}
	const presentations = {
		header_estimate: ["等级为 Header 估算", "warn"],
		official_tier_unavailable: ["官方等级未知 · 已取得限流窗口", "off"],
		rate_limit_observed_from_error: ["官方等级未知 · 已观测限流上限", "warn"],
		partial_probe_rate_limited: ["有效 · 部分探测收到 429", "warn"],
		rate_limited_without_window: ["有效 · 探测收到 429，范围未知", "warn"],
		probe_model_unavailable: ["有效 · 探测模型不可调用", "off"],
		rate_limit_window_unavailable: ["有效 · OpenAI 未返回限流窗口", "off"],
	};
	if (presentations[extra.tier_reason_code]) {
		const [label, tone] = presentations[extra.tier_reason_code];
		const rateLimitProbeReason = ["partial_probe_rate_limited", "rate_limited_without_window"]
			.includes(extra.tier_reason_code)
			? "Key 鉴权有效；本轮至少一次最小模型调用探测收到 HTTP 429，但无法据此判断整条 Key 当前全面限流。"
			: "";
		return chip(label, tone, rateLimitProbeReason || extra.tier_reason || label);
	}
	const legacyReasons = {
		"no rate-limit signal from any source": "Key 有效，但 OpenAI 未返回限流窗口",
		"no probe model accessible": "Key 有效，但探测模型不可调用",
		"burst succeeded, low confidence": "等级来自旧版并发探测，可信度较低",
	};
	const reason = legacyReasons[extra.tier_reason] || extra.tier_reason;
	return reason ? chip(reason, "off", reason) : "";
}

function detailChips(r) {
	const e = r.extra || {};
	const chips = [];
	if (r.provider === "azure_openai") {
		if (e.chat_completions_url || e.endpoint) {
			chips.push(chip("Azure 端点", "on", e.chat_completions_url || e.endpoint));
		}
		if (e.api_version) chips.push(chip(`API ${e.api_version}`));
		if (e.models_list_rate_limited) chips.push(chip("模型目录限流", "warn"));
		chips.push(...azureOpenAIModelChips(e));
		if (e.models_count) chips.push(chip(`目录模型 ${e.models_count}`));
	} else if (r.provider === "openrouter") {
		chips.push(...openRouterChips(e));
	} else if (r.provider === "openai") {
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
		chips.push(...openaiRateLimitChips(e));
		chips.push(...openai429Chips(e));
		if (e.tier_reason) {
			chips.push(openaiTierReasonChip(e));
		}
		if (e.burst_probe) {
			const bp = e.burst_probe;
			chips.push(
				chip(
					`旧版并发估算 ${bp.rpm_estimate ?? "?"}`,
					"warn",
					`历史检测数据：成功=${bp.ok}，触发429=${bp.hit_429}，估算=${bp.rpm_estimate}`,
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
		const dynamicModelChips = targetModelChips(
			e.supported_models,
			e.opus5_probe ? ["opus-5"] : [],
		);
		if (dynamicModelChips.length) {
			chips.push(...dynamicModelChips);
		} else if (e.models_error) {
			chips.push(chip("models unavailable", "off", e.models_error));
		} else if (e.probe_model) {
			chips.push(chip("retest for models", "off", e.probe_model));
		}
		if (e.source) chips.push(chip(e.source, "", e.probe_model || ""));
		chips.push(...anthropicOpus5ProbeChips(e));
		chips.push(...anthropicQuotaChips(e));
	} else if (r.provider === "aws_bedrock") {
		chips.push(...bedrockDetailChipList(e));
	} else if (r.provider === "gcp_service_account") {
		if (e.project_id) chips.push(chip(`项目 ${e.project_id}`, "on", e.project_id));
		if (e.client_email) chips.push(chip("服务账号", "", e.client_email));
		if (e.private_key_id_suffix) chips.push(chip(`Key …${e.private_key_id_suffix}`));
		if (e.token_exchange === "success") {
			chips.push(chip(
				"OAuth Token 已签发",
				"on",
				e.token_expiry
					? `Access Token 到期时间：${new Date(e.token_expiry).toLocaleString()}`
					: "Google 已成功签发 Access Token",
			));
		}
		chips.push(...gcpModelProbeChips(e));
		chips.push(chip(
			"Vertex Gemini + Claude",
			"warn",
			"Gemini 通过最小 generateContent 真实调用验证；Claude 通过无生成费用的 countTokens 验证模型权限。不代表拥有 Google Play、IAM 或其他 GCP API 权限。",
		));
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
	if (r.provider === "azure_openai") {
		chips = azureOpenAIModelChips(e, true);
	} else if (r.provider === "openrouter") {
		chips = openRouterChips(e, true);
	} else if (r.provider === "openai") {
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
		chips = targetModelChips(
			e.supported_models,
			e.opus5_probe ? ["opus-5"] : [],
		);
		if (!chips.length && e.models_error) chips.push(chip("models unavailable", "off", e.models_error));
		chips.push(...anthropicOpus5ProbeChips(e));
		chips.push(...anthropicQuotaChips(e, true));
	} else if (r.provider === "gemini") {
		chips = targetModelChips(e.supported_models);
		if (!chips.length && e.models_error) chips.push(chip("models unavailable", "off", e.models_error));
		if (!chips.length && e.probe_model) chips.push(chip(e.probe_model));
	} else if (r.provider === "aws_bedrock") {
		chips = bedrockDetailChipList(e, true);
	} else if (r.provider === "gcp_service_account") {
		if (e.project_id) chips.push(chip(e.project_id, "on", e.client_email || ""));
		if (e.token_exchange === "success") chips.push(chip("OAuth 有效", "on"));
		chips.push(...gcpModelProbeChips(e, true));
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

const GCP_FILE_MAX_BYTES = 64 * 1024;
const GCP_FILE_MAX_COUNT = 100;

function setGcpFileError(message = "") {
	const error = $("#gcp-file-error");
	error.textContent = message;
	error.classList.toggle("hidden", !message);
}

function selectedGcpFiles() {
	return Array.from($("#gcp-files").files || []);
}

function updateGcpFileSummary() {
	const files = selectedGcpFiles();
	$("#gcp-files-clear").classList.toggle("hidden", files.length === 0);
	$("#gcp-file-summary").classList.toggle("hidden", files.length === 0);
	if (!files.length) {
		$("#gcp-file-summary").textContent = "";
		return;
	}
	const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
	$("#gcp-file-summary").textContent =
		`已选择 ${files.length} 个文件，共 ${(totalBytes / 1024).toFixed(1)} KiB`;
}

function gcpFileValidationError(message) {
	const error = new Error(message);
	error.gcpFileValidation = true;
	return error;
}

function validateGcpServiceAccountObject(value, filename) {
	const required = [
		"project_id",
		"private_key_id",
		"private_key",
		"client_email",
		"client_id",
		"token_uri",
	];
	if (!value || Array.isArray(value) || typeof value !== "object") {
		throw gcpFileValidationError(`${filename}：内容必须是 JSON 对象`);
	}
	if (value.type !== "service_account") {
		throw gcpFileValidationError(`${filename}：type 必须是 service_account`);
	}
	const missing = required.find((field) =>
		typeof value[field] !== "string" || !value[field].trim()
	);
	if (missing) {
		throw gcpFileValidationError(`${filename}：缺少字段 ${missing}`);
	}
	const privateKey = value.private_key.trim();
	if (
		!privateKey.startsWith("-----BEGIN PRIVATE KEY-----")
		|| !privateKey.endsWith("-----END PRIVATE KEY-----")
	) {
		throw gcpFileValidationError(`${filename}：private_key 不是有效的 PEM 格式`);
	}
	return value;
}

async function readSelectedGcpServiceAccounts() {
	const files = selectedGcpFiles();
	if (files.length > GCP_FILE_MAX_COUNT) {
		throw gcpFileValidationError(`一次最多选择 ${GCP_FILE_MAX_COUNT} 个 JSON 文件`);
	}
	const accounts = [];
	for (const file of files) {
		if (file.size > GCP_FILE_MAX_BYTES) {
			throw gcpFileValidationError(`${file.name}：文件超过 64 KiB`);
		}
		let parsed;
		try {
			parsed = JSON.parse(await file.text());
		} catch (error) {
			throw gcpFileValidationError(`${file.name}：不是有效 JSON`);
		}
		accounts.push(validateGcpServiceAccountObject(parsed, file.name));
	}
	return accounts;
}

$("#gcp-files").addEventListener("change", async () => {
	updateGcpFileSummary();
	setGcpFileError();
	try {
		await readSelectedGcpServiceAccounts();
	} catch (error) {
		setGcpFileError(error.message);
	}
});

$("#gcp-files-clear").addEventListener("click", () => {
	$("#gcp-files").value = "";
	setGcpFileError();
	updateGcpFileSummary();
	$("#gcp-files").focus();
});

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

document.addEventListener("click", async (event) => {
	const button = event.target.closest(".bedrock-gateway-copy");
	if (!button) return;
	event.preventDefault();
	event.stopPropagation();
	try {
		await copySecretText(button.dataset.gatewayJson || "");
		toast("已复制 Bedrock 网关映射 JSON");
	} catch (error) {
		toast("复制失败：" + error.message);
	}
});

function confirmBedrockDeep(rows, skipped = 0, useProxy = false) {
	const count = rows.length;
	const bearerCount = rows.filter((row) =>
		row?.extra?.credential_type === "bedrock_api_key"
		|| String(row?.api_key_short || "").startsWith("ABSK"),
	).length;
	const sigv4Count = count - bearerCount;
	const skippedText = skipped ? `\n另有 ${skipped} 个非 Bedrock 项不会处理。` : "";
	const connectionText = useProxy
		? "本次检测将使用已勾选的 SOCKS5 代理池。"
		: "本次检测将直连 AWS。";
	const actionText = sigv4Count
		? `其中 ${sigv4Count} 个 AWS Access Key 将扫描全部已知 Bedrock 区域。${bearerCount ? `\n另有 ${bearerCount} 个 ABSK API Key 将扫描 AWS 官方支持 API Key 的区域。` : ""}`
		: "ABSK API Key 将扫描 AWS 官方支持 API Key 的全部区域。";
	return confirm(
		`确认深检 ${count} 个 AWS Bedrock Key？\n${actionText}\n两种凭证都会发现 Claude Fable 5 与 Opus，并发送最小 InvokeModel 请求来生成区域模型重定向；真实调用可能产生费用。若账户已为 Fable 5 开启 provider_data_share，探测输入“.”及输出可能按 AWS 规则保留并共享。\n${connectionText}${skippedText}`,
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
	const files = selectedGcpFiles();
	if (!text && !files.length) return toast("请粘贴 Key 或选择 GCP JSON 文件");
	const concurrency = parseInt($("#concurrency").value, 10) || 4;
	const useProxy = $("#use-proxy").checked;
	const importButton = $("#btn-import");
	if (importButton.disabled) return;
	const originalLabel = importButton.textContent;
	importButton.disabled = true;
	$("#gcp-files").disabled = true;
	$("#gcp-files-clear").disabled = true;
	importButton.setAttribute("aria-busy", "true");
	importButton.textContent = "正在提交…";
	setGcpFileError();
	try {
		const serviceAccounts = await readSelectedGcpServiceAccounts();
		const r = await api("POST", "/api/keys/import", {
			text,
			service_accounts: serviceAccounts,
			concurrency,
			use_proxy: useProxy,
		});
		const bd = Object.entries(r.breakdown || {})
			.map(([k, v]) => `${k}:${v}`)
			.join(" / ");
		toast(`已导入 ${r.imported} 个 (${bd})`);
		$("#keys-input").value = "";
		$("#gcp-files").value = "";
		updateGcpFileSummary();
		resetCheckFilters();
		state.selected.clear();
		await loadKeys();
		startPolling(r.job_id);
	} catch (e) {
		if (e.gcpFileValidation) {
			setGcpFileError(e.message);
			$("#gcp-files").focus();
		}
		toast("导入失败：" + e.message);
	} finally {
		importButton.disabled = false;
		$("#gcp-files").disabled = false;
		$("#gcp-files-clear").disabled = false;
		importButton.setAttribute("aria-busy", "false");
		importButton.textContent = originalLabel;
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
		const text = await requestSecretText("/api/keys/export", ids, "bundle");
		await copySecretText(text);
		const hasBedrock = selectedKeyObjs().some((row) => row.provider === "aws_bedrock");
		toast(`已复制 ${ids.length} 个 Key${hasBedrock ? "，并附带分组 Bedrock 公共网关映射" : ""}（操作已审计）`);
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
	const bedrockRows = selected.filter((row) => row.provider === "aws_bedrock");
	const ids = bedrockRows.map((row) => row.id);
	if (!ids.length) return toast("请先选择 AWS Bedrock Key");
	const useProxy = $("#use-proxy").checked;
	if (!confirmBedrockDeep(bedrockRows, selected.length - ids.length, useProxy)) return;
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
		const text = await requestSecretText("/api/vault/export", ids, "bundle");
		await copySecretText(text);
		const hasBedrock = selectedVisibleVaultRows().some((row) => row.provider === "aws_bedrock");
		toast(`已复制 ${ids.length} 个 Key${hasBedrock ? "，并附带分组 Bedrock 公共网关映射" : ""}（操作已审计）`);
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
	const bedrockRows = selected.filter((row) => row.provider === "aws_bedrock");
	const ids = bedrockRows.map((row) => row.id);
	if (!ids.length) return toast("请先选择 AWS Bedrock Key");
	const useProxy = $("#use-proxy").checked;
	if (!confirmBedrockDeep(bedrockRows, selected.length - ids.length, useProxy)) return;
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
	const bedrockRows = selected.filter((row) => row.provider === "aws_bedrock");
	const ids = bedrockRows.map((row) => row.id);
	if (!ids.length) return toast("请先选择 AWS Bedrock Key");
	const useProxy = $("#use-proxy").checked;
	if (!confirmBedrockDeep(bedrockRows, selected.length - ids.length, useProxy)) return;
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
		const text = await exportInventorySelected("bundle", "/api/inventory/export", false);
		if (!text) return;
		await copySecretText(text);
		const selected = selectedInventoryObjs();
		const hasBedrock = selected.some((row) => row.provider === "aws_bedrock");
		toast(`已复制 ${selected.length} 个完整 Key${hasBedrock ? "，并附带分组 Bedrock 公共网关映射" : ""}`);
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
