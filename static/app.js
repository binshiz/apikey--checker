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

function fmt(n) {
	if (n === null || n === undefined) return "—";
	return n.toLocaleString();
}

function fmtTime(t) {
	if (!t) return "—";
	const d = new Date(t * 1000);
	return d.toLocaleString();
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

function tierBadge(t) {
	if (!t) return "—";
	const cls = t.toLowerCase().replace(/\s+/g, "-");
	return `<span class="tier ${cls}">${t}</span>`;
}

function detailChips(r) {
	const e = r.extra || {};
	const chips = [];
	if (r.provider === "openai") {
		if (typeof e.has_gpt_5_5 !== "undefined") {
			chips.push(
				`<span class="chip ${e.has_gpt_5_5 ? "on" : "off"}">gpt-5.5</span>`,
			);
		}
		if (typeof e.has_gpt_image_2 !== "undefined") {
			chips.push(
				`<span class="chip ${e.has_gpt_image_2 ? "on" : "off"}">gpt-image-2</span>`,
			);
		}
		if (typeof e.has_sora_2 !== "undefined") {
			chips.push(
				`<span class="chip ${e.has_sora_2 ? "on" : "off"}">sora-2</span>`,
			);
		}
		if (e.models_count) {
			chips.push(`<span class="chip">📦 ${e.models_count}</span>`);
		}
		if (e.org_id) {
			chips.push(`<span class="chip" title="${e.org_id}">🆔</span>`);
		}
		if (e.tier_reason) {
			chips.push(
				`<span class="chip off" title="${e.tier_reason}">? ${e.tier_reason}</span>`,
			);
		}
		if (e.burst_probe) {
			const bp = e.burst_probe;
			chips.push(
				`<span class="chip" title="burst probe: ok=${bp.ok} 429=${bp.hit_429} est=${bp.rpm_estimate}">burst ${bp.rpm_estimate ?? "?"}</span>`,
			);
		}
	} else if (r.provider === "gemini") {
		if (e.probe_model) chips.push(`<span class="chip">${e.probe_model}</span>`);
		if (e.burst && e.burst.ceiling_hit)
			chips.push(`<span class="chip on">429 hit</span>`);
		if (e.models_count)
			chips.push(`<span class="chip">📦 ${e.models_count}</span>`);
	} else if (r.provider === "anthropic") {
		if (e.source) chips.push(`<span class="chip">${e.source}</span>`);
		if (e.probe_model) chips.push(`<span class="chip">${e.probe_model}</span>`);
	}
	if (r.error)
		chips.push(
			`<span class="chip off" title="${r.error}">❗ ${r.error.slice(0, 24)}</span>`,
		);
	return `<div class="detail-chips">${chips.join("")}</div>`;
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
		if (!$("#view-vault").classList.contains("hidden")) await loadVault();

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

$("#v-btn-export").addEventListener("click", () => {
	const f = getVaultFilters();
	const params = new URLSearchParams({ format: "txt", token: getAdminKey() });
	for (const k in f) if (f[k]) params.set(k, f[k]);
	window.open(`/api/vault/export?${params.toString()}`, "_blank");
});

$$("#v-provider, #v-tier").forEach((el) =>
	el.addEventListener("change", loadVault),
);

// ─── tab switching ───────────────────────────────────────────────────
function switchTab(name) {
	$$(".tab").forEach((t) =>
		t.classList.toggle("active", t.dataset.tab === name),
	);
	$("#view-check").classList.toggle("hidden", name !== "check");
	$("#view-vault").classList.toggle("hidden", name !== "vault");
	if (name === "vault") loadVault();
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
