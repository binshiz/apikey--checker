// ─── helpers ─────────────────────────────────────────────────────────
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

function toast(msg, ms = 1800) {
	let el = document.querySelector(".toast");
	if (!el) {
		el = document.createElement("div");
		el.className = "toast";
		document.body.appendChild(el);
	}
	el.textContent = msg;
	el.classList.add("show");
	clearTimeout(toast._t);
	toast._t = setTimeout(() => el.classList.remove("show"), ms);
}

function getAdminKey() {
	return sessionStorage.getItem("admin_key") || "";
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
		const t = await r.text().catch(() => "");
		throw new Error(`${r.status}: ${t.slice(0, 200)}`);
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
		const t = await r.text().catch(() => "");
		throw new Error(`${r.status}: ${t.slice(0, 200)}`);
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

function providerBadge(p) {
	const cls = p || "unknown";
	const label = p ? p.toUpperCase() : "未知";
	return `<span class="badge ${cls}">${label}</span>`;
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

function tierBadge(t) {
	if (!t) return "—";
	const cls = t.toLowerCase().replace(/\s+/g, "-");
	return `<span class="tier ${cls}">${t}</span>`;
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
		if (e.probe_model) chips.push(chip(e.probe_model));
		if (e.burst && e.burst.ceiling_hit)
			chips.push(chip("429 hit", "on"));
		if (e.models_count) chips.push(chip(`📦 ${e.models_count}`));
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
	}
	if (r.error)
		chips.push(chip(`❗ ${r.error.slice(0, 24)}`, "off", r.error));
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
		if (e.probe_model) chips.push(chip(e.probe_model));
		if (e.models_count) chips.push(chip(`models ${e.models_count}`));
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
		if (r.status === "valid") valid++;
		else if (r.status === "no_quota") noQuota++;
		else if (r.status === "invalid") invalid++;
		else pending++;

		const tr = document.createElement("tr");
		tr.dataset.id = r.id;
		tr.innerHTML = `
      <td class="col-cb"><input type="checkbox" class="cb-row" ${state.selected.has(r.id) ? "checked" : ""}></td>
      <td>${providerBadge(r.provider)}</td>
      <td class="key-cell" title="点击复制">${r.api_key_short}</td>
      <td>${statusBadge(r.status)}</td>
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
}

async function loadKeys() {
	const f = getFilters();
	const params = new URLSearchParams();
	for (const k in f) if (f[k]) params.set(k, f[k]);
	const data = await api("GET", `/api/keys?${params.toString()}`);
	state.keys = data.keys;
	render();
}

// ─── selection ───────────────────────────────────────────────────────
function selectedKeyObjs() {
	return state.keys.filter((r) => state.selected.has(r.id));
}

// ─── job polling ─────────────────────────────────────────────────────
async function pollJob() {
	if (!state.jobId) return;
	try {
		const j = await api("GET", `/api/jobs/${state.jobId}`);
		const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
		$("#job-bar-fill").style.width = pct + "%";
		$("#job-text").textContent =
			`Job #${j.id} · ${j.done}/${j.total} (${pct}%) · ${j.status}`;

		// refresh keys table every poll
		await loadKeys();
		// also refresh vault count badge (and vault list if currently viewing it)
		refreshVaultBadge();
		refreshInventoryBadge();
		if (!$("#view-vault").classList.contains("hidden")) await loadVault();
		if (!$("#view-inventory").classList.contains("hidden")) await loadInventory();

		if (j.status === "done") {
			clearInterval(state.jobPollTimer);
			state.jobPollTimer = null;
			state.jobId = null;
			toast("✅ 检测完成");
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
	if (state.selected.size === state.keys.length) state.selected.clear();
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
	} else if (e.target.classList.contains("key-cell")) {
		const r = state.keys.find((k) => k.id === id);
		if (r) {
			navigator.clipboard.writeText(r.api_key).then(() => toast("已复制 Key"));
		}
	}
});

$("#btn-copy").addEventListener("click", () => {
	const objs = selectedKeyObjs();
	if (!objs.length) {
		// fall back to all visible
		const all = state.keys.map((r) => r.api_key).join("\n");
		if (!all) return toast("没有可复制的 Key");
		navigator.clipboard
			.writeText(all)
			.then(() => toast(`复制了 ${state.keys.length} 个（全部）`));
		return;
	}
	navigator.clipboard
		.writeText(objs.map((r) => r.api_key).join("\n"))
		.then(() => toast(`已复制 ${objs.length} 个 Key`));
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
		});
		toast(`已重测 ${r.queued} 个`);
		startPolling(r.job_id);
	} catch (e) {
		toast("失败：" + e.message);
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
const vaultState = { keys: [], selected: new Set() };

function getVaultFilters() {
	return {
		provider: $("#v-provider").value || null,
		tier: $("#v-tier").value || null,
	};
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
      <td class="col-cb"><input type="checkbox" class="v-cb-row" ${vaultState.selected.has(r.id) ? "checked" : ""}></td>
      <td>${providerBadge(r.provider)}</td>
      <td class="key-cell" title="点击复制">${r.api_key_short}</td>
      <td>${tierBadge(r.tier)}</td>
      <td>${fmt(r.rpm)}</td>
      <td>${fmt(r.tpm)}</td>
      <td>${detailChips(r)}</td>
      <td><span class="chip">×${r.check_count}</span></td>
      <td>${fmtTime(r.first_verified_at)}</td>
      <td>${fmtTime(r.last_verified_at)}</td>
      <td><input class="v-note" data-id="${r.id}" value="${(r.note || "").replace(/"/g, "&quot;")}" placeholder="备注…"></td>
    `;
		tbody.appendChild(tr);
	}
}

async function loadVault() {
	const f = getVaultFilters();
	const params = new URLSearchParams();
	for (const k in f) if (f[k]) params.set(k, f[k]);
	const data = await api("GET", `/api/vault?${params.toString()}`);
	vaultState.keys = data.keys;
	const s = data.stats || {};
	const byProv = Object.entries(s.by_provider || {})
		.map(([k, v]) => `${k}:${v}`)
		.join(" · ");
	$("#v-stats").textContent = `共 ${s.total ?? 0} 个 · ${byProv}`;
	$("#vault-count-badge").textContent = s.total ?? 0;
	renderVault();
}

$("#vault-table tbody").addEventListener("click", (e) => {
	const tr = e.target.closest("tr");
	if (!tr) return;
	const id = parseInt(tr.dataset.id, 10);
	if (e.target.classList.contains("v-cb-row")) {
		if (e.target.checked) vaultState.selected.add(id);
		else vaultState.selected.delete(id);
	} else if (e.target.classList.contains("key-cell")) {
		const r = vaultState.keys.find((k) => k.id === id);
		if (r)
			navigator.clipboard.writeText(r.api_key).then(() => toast("已复制 Key"));
	}
});

$("#vault-table tbody").addEventListener("change", async (e) => {
	if (e.target.classList.contains("v-note")) {
		const id = parseInt(e.target.dataset.id, 10);
		try {
			await api("POST", `/api/vault/note/${id}`, { note: e.target.value });
			toast("备注已保存");
		} catch (err) {
			toast("保存失败");
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
	if (vaultState.selected.size === vaultState.keys.length)
		vaultState.selected.clear();
	else vaultState.keys.forEach((r) => vaultState.selected.add(r.id));
	renderVault();
});

$("#v-btn-copy").addEventListener("click", () => {
	const ids = [...vaultState.selected];
	const objs = ids.length
		? vaultState.keys.filter((r) => vaultState.selected.has(r.id))
		: vaultState.keys;
	if (!objs.length) return toast("没有可复制的 Key");
	navigator.clipboard
		.writeText(objs.map((r) => r.api_key).join("\n"))
		.then(() => toast(`已复制 ${objs.length} 个`));
});

$("#v-btn-retest").addEventListener("click", async () => {
	const ids = [...vaultState.selected];
	if (!ids.length) return toast("请先选择");
	const concurrency = parseInt($("#concurrency").value, 10) || 4;
	const useProxy = $("#use-proxy").checked;
	try {
		const r = await api("POST", "/api/vault/recheck", {
			ids,
			concurrency,
			use_proxy: useProxy,
		});
		toast(`已重测 ${r.queued} 个（结果回写密钥库）`);
		// Switch to check view to follow progress.
		switchTab("check");
		startPolling(r.job_id);
	} catch (e) {
		toast("失败：" + e.message);
	}
});

$("#v-btn-delete").addEventListener("click", async () => {
	const ids = [...vaultState.selected];
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

function openInboundModal() {
	const ids = [...vaultState.selected];
	if (!ids.length) return toast("请先选择要入库的 Key");
	$("#inbound-supplier").value = "";
	$("#inbound-cost").value = "";
	$("#inbound-tags").value = "";
	$("#inbound-note").value = "";
	$("#inbound-error").textContent = "请填写供应商";
	$("#inbound-error").classList.add("hidden");
	$("#inbound-modal").classList.remove("hidden");
	setTimeout(() => $("#inbound-supplier").focus(), 0);
}

function closeInboundModal() {
	$("#inbound-modal").classList.add("hidden");
}

$("#v-btn-inbound").addEventListener("click", openInboundModal);
$("#inbound-close").addEventListener("click", closeInboundModal);
$("#inbound-cancel").addEventListener("click", closeInboundModal);
$("#inbound-modal").addEventListener("click", (e) => {
	if (e.target.id === "inbound-modal") closeInboundModal();
});

$("#inbound-submit").addEventListener("click", async () => {
	const ids = [...vaultState.selected];
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
	$("#inbound-submit").disabled = true;
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
		$("#inbound-submit").disabled = false;
	}
});

$("#v-btn-export").addEventListener("click", () => {
	const f = getVaultFilters();
	const params = new URLSearchParams({ format: "txt", token: getAdminKey() });
	for (const k in f) if (f[k]) params.set(k, f[k]);
	window.open(`/api/vault/export?${params.toString()}`, "_blank");
});

$$("#v-provider, #v-tier").forEach((el) =>
	el.addEventListener("change", loadVault),
);

// ─── inventory ──────────────────────────────────────────────────────
const inventoryState = { keys: [], selected: new Set(), stats: {}, busy: false };

function getInventoryFilters() {
	return {
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
	const items = [
		["总库存", stats.total ?? 0],
		["可售", byStatus.in_stock || 0],
		["预留", byStatus.reserved || 0],
		["无额度", byStatus.no_quota || 0],
		["失效", byStatus.invalid || 0],
		["隔离", byStatus.quarantined || 0],
		["归档", byStatus.archived || 0],
	];
	$("#inventory-stats").innerHTML = items
		.map(([label, value]) => `<span class="metric"><b>${fmt(value)}</b>${escapeHtml(label)}</span>`)
		.join("");
	$("#inventory-count-badge").textContent = stats.total ?? 0;
}

function updateInventorySelectionStats() {
	const count = inventoryState.selected.size;
	$("#i-selection-stats").textContent = count ? `已选择 ${count} 个` : "未选择";
	const allVisible = inventoryState.keys.length > 0 && inventoryState.keys.every((r) => inventoryState.selected.has(r.id));
	$("#i-cb-all").checked = allVisible;
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
		const costText = r.unit_cost !== null && r.unit_cost !== undefined
			? fmtMoney(r.unit_cost)
			: fmtMoney(r.batch_total_cost);
		tr.innerHTML = `
      <td class="col-cb"><input type="checkbox" class="i-cb-row" ${inventoryState.selected.has(r.id) ? "checked" : ""}></td>
      <td>${providerBadge(r.provider)}</td>
      <td class="key-cell" title="完整 Key 需通过选中复制/导出">${escapeHtml(r.api_key_short)}</td>
      <td>${stockStatusBadge(r.stock_status)}</td>
      <td>${tierBadge(r.tier)}</td>
      <td>${fmt(r.rpm)}</td>
      <td>${fmt(r.tpm)}</td>
      <td>${inventorySupportedModelChips(r)}</td>
      <td>${escapeHtml(r.batch_name || "—")}</td>
      <td>${costText}</td>
      <td>${escapeHtml(r.supplier_name || "—")}</td>
      <td><input class="table-input i-meta" data-field="tags" value="${escapeHtml(r.tags || "")}" placeholder="${escapeHtml(r.batch_tags || "标签")}"></td>
      <td>${r.latest_check_status ? `${statusBadge(r.latest_check_status)} ${fmtTime(r.latest_check_at)}` : fmtTime(r.last_checked_at)}</td>
      <td><input class="table-input i-meta" data-field="note" value="${escapeHtml(r.note || "")}" placeholder="备注"></td>
      <td><button class="btn btn-small i-detail" type="button">详情</button></td>
    `;
		tbody.appendChild(tr);
	}
	updateInventorySelectionStats();
}

async function loadInventory() {
	const f = getInventoryFilters();
	const params = new URLSearchParams();
	for (const k in f) if (f[k]) params.set(k, f[k]);
	const data = await api("GET", `/api/inventory?${params.toString()}`);
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
	$$(".inventory-actions button").forEach((btn) => {
		btn.disabled = busy;
	});
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

async function exportInventorySelected(format = "txt") {
	const ids = [...inventoryState.selected];
	if (!ids.length) return toast("请先选择库存");
	if (!confirm(`确认导出 ${ids.length} 个完整 Key？`)) return null;
	const response = await apiResponse("POST", "/api/inventory/export", { ids, format });
	return format === "json" ? response.json() : response.text();
}

async function copyInventorySelected() {
	try {
		const text = await exportInventorySelected("txt");
		if (!text) return;
		await navigator.clipboard.writeText(text);
		toast(`已复制 ${text.split("\n").filter(Boolean).length} 个完整 Key`);
	} catch (e) {
		toast("复制失败：" + e.message);
	}
}

async function downloadInventorySelected() {
	try {
		const text = await exportInventorySelected("txt");
		if (!text) return;
		const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
		const url = URL.createObjectURL(blob);
		const a = document.createElement("a");
		a.href = url;
		a.download = `inventory-keys-${Date.now()}.txt`;
		document.body.appendChild(a);
		a.click();
		a.remove();
		URL.revokeObjectURL(url);
		toast("导出已开始");
	} catch (e) {
		toast("导出失败：" + e.message);
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
	$("#inventory-detail-modal").classList.add("hidden");
}

function renderInventoryDetail(data) {
	const item = data.item || {};
	const checks = data.check_runs || [];
	const movements = data.movements || [];
	const checkRows = checks.length
		? checks.map((r) => `
        <tr>
          <td>${fmtTime(r.checked_at)}</td>
          <td>${statusBadge(r.status)}</td>
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
  `;
}

async function openInventoryDetail(id) {
	try {
		$("#inventory-detail-body").innerHTML = `<div class="empty">加载中…</div>`;
		$("#inventory-detail-modal").classList.remove("hidden");
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
$("#inventory-detail-close").addEventListener("click", closeInventoryDetail);
$("#inventory-detail-modal").addEventListener("click", (e) => {
	if (e.target.id === "inventory-detail-modal") closeInventoryDetail();
});

// ─── tab switching ───────────────────────────────────────────────────
function switchTab(name) {
	$$(".tab").forEach((t) =>
		t.classList.toggle("active", t.dataset.tab === name),
	);
	$("#view-check").classList.toggle("hidden", name !== "check");
	$("#view-vault").classList.toggle("hidden", name !== "vault");
	$("#view-inventory").classList.toggle("hidden", name !== "inventory");
	if (name === "vault") loadVault();
	if (name === "inventory") loadInventory();
}
$$(".tab").forEach((t) =>
	t.addEventListener("click", () => switchTab(t.dataset.tab)),
);

async function refreshVaultBadge() {
	try {
		const r = await api("GET", "/api/vault?");
		$("#vault-count-badge").textContent = r.stats?.total ?? 0;
	} catch (e) {}
}

async function refreshInventoryBadge() {
	try {
		const r = await api("GET", "/api/inventory?");
		$("#inventory-count-badge").textContent = r.stats?.total ?? 0;
	} catch (e) {}
}

// ─── auth gate ──────────────────────────────────────────────────────
function showAuthOverlay() {
	$("#auth-overlay").classList.remove("hidden");
	$("#auth-input").value = "";
	$("#auth-error").classList.add("hidden");
	$("#auth-input").focus();
}

function hideAuthOverlay() {
	$("#auth-overlay").classList.add("hidden");
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
