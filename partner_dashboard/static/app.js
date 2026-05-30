/* UPJUNOO Partner Dashboard */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

let dashboardData = null;
let eventSource = null;
let lastJobStatus = null;
let notifiedComplete = false;
let selectedReportPath = "";

function reportQuery() {
  if (!selectedReportPath) return "";
  return `?report=${encodeURIComponent(selectedReportPath)}`;
}

function playSuccessSound() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.frequency.value = 880;
    osc.type = "sine";
    gain.gain.setValueAtTime(0.15, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.4);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + 0.4);
    setTimeout(() => {
      const o2 = ctx.createOscillator();
      const g2 = ctx.createGain();
      o2.connect(g2);
      g2.connect(ctx.destination);
      o2.frequency.value = 1174;
      g2.gain.setValueAtTime(0.12, ctx.currentTime);
      g2.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.5);
      o2.start();
      o2.stop(ctx.currentTime + 0.5);
    }, 180);
  } catch (_) {
    /* ignore */
  }
}

function showToast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("visible");
  setTimeout(() => t.classList.remove("visible"), 5000);
}

async function copyPhoneToClipboard(phone) {
  const text = (phone || "").trim();
  if (!text) {
    showToast("Numéro vide");
    return false;
  }
  try {
    await navigator.clipboard.writeText(text);
    showToast(`Numéro copié : ${text}`);
    return true;
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
      showToast(`Numéro copié : ${text}`);
      return true;
    } catch {
      showToast("Impossible de copier le numéro");
      return false;
    }
  }
}

function renderPhoneCell(d) {
  const phone = (d.phone || "").trim();
  if (!phone) return "—";
  const attr = phone.replace(/"/g, "&quot;");
  return `<span class="phone-cell">
    <span class="phone-cell-num">${escapeHtml(phone)}</span>
    <button type="button" class="btn-copy-phone" data-phone="${attr}" title="Copier le numéro">Copier</button>
  </span>`;
}

const JOB_KIND_LABELS = {
  nightly: "Rapports du soir",
  orchestrator: "Orchestrateur + sync paiements",
  activation: "Génération HTML",
  godseye: "Godseye (conducteurs en ligne)",
  zip: "Archives ZIP",
};

function formatCampaignProgress(job) {
  const cur = job.campaign_current;
  const total = job.campaign_total;
  if (cur != null && total != null) {
    return `Progression : ${cur} / ${total}`;
  }
  if (cur != null && job.campaign_end) {
    return `Campagne n°${cur} (plage 1–${job.campaign_end})`;
  }
  if (cur != null) {
    return `Étape campagne ${cur}`;
  }
  return "";
}

function setLaunchButtonsDisabled(disabled) {
  document.body.classList.toggle("job-running", disabled);
  [
    "#btn-nightly",
    "#btn-orchestrator",
    "#btn-run-nightly",
    "#btn-run-orch",
    "#btn-run-html",
    "#btn-run-zip",
    "#btn-godseye-download",
    "#btn-generate-chauffeurs",
  ].forEach(
    (sel) => {
      const el = $(sel);
      if (el) {
        el.disabled = disabled;
        el.classList.toggle("btn-launch", true);
      }
    },
  );
}

function updateJobBanner(job) {
  const banner = $("#job-banner");
  const badge = $("#tab-runs-badge");
  if (!banner) return;

  const running = job && job.status === "running";
  banner.hidden = !running;
  badge.hidden = !running;
  setLaunchButtonsDisabled(running);

  if (!running) return;

  const kindLabel = JOB_KIND_LABELS[job.kind] || job.kind;
  $("#job-banner-title").textContent = `${kindLabel} en cours`;
  const camp = formatCampaignProgress(job);
  $("#job-banner-detail").textContent = [job.phase_label, camp].filter(Boolean).join(" · ");
  const pct = job.progress_pct || 0;
  $("#job-banner-pct").textContent = `${pct}%`;
  $("#job-banner-fill").style.width = `${pct}%`;
}

function fmtDate(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString("fr-FR");
  } catch {
    return iso;
  }
}

function renderExecutiveSummaryBlocks(summary) {
  if (!summary) return "";
  function block(title, rows, gold) {
    const rowsHtml = rows
      .map(
        ([lbl, val]) =>
          `<div class="exec-row"><span>${escapeHtml(lbl)}</span><span>${escapeHtml(String(val))}</span></div>`,
      )
      .join("");
    return `<div class="exec-block${gold ? " gold" : ""}"><h3>${escapeHtml(title)}</h3>${rowsHtml}</div>`;
  }
  const v = summary.vehicles || {};
  const d = summary.drivers || {};
  const r = summary.recharge || {};
  const count = r.campaign_count || 1;
  const per = r.budget_per_campaign_display || "200 000 F";
  const budgetLbl =
    count > 1 ? `Budget global (${count} × ${per})` : `Budget campagne (${per})`;
  return (
    block("Véhicules", [
      ["Total enregistré", v.registered ?? 0],
      ["Total approuvé", v.approved ?? 0],
      ["Total en attente", v.pending ?? 0],
      ["Total refusé", v.rejected ?? 0],
    ]) +
    block("Conducteurs", [
      ["Total enregistré", d.registered ?? 0],
      ["Total approuvé", d.approved ?? 0],
      ["Total en attente", d.pending ?? 0],
      ["Total refusé", d.rejected ?? 0],
    ]) +
    block(
      "Recharge",
      [
        [budgetLbl, r.budget_global_display || "0 F"],
        ["Montant utilisé", r.montant_utilise_display || "0 F"],
        ["Reste", r.reste_display || "0 F"],
      ],
      true,
    )
  );
}

function renderExecutiveSummary(summary) {
  const card = $("#executive-summary-card");
  const box = $("#executive-summary");
  if (!card || !box || !summary) {
    if (card) card.style.display = "none";
    return;
  }
  card.style.display = "block";
  box.innerHTML = renderExecutiveSummaryBlocks(summary);
}

function chartHtml(label, pct) {
  const p = Math.max(0, Math.min(100, pct || 0));
  return `
    <div class="chart-box">
      <h4>${label}</h4>
      <div class="bar-track"><div class="bar-fill" style="width:${p}%"></div></div>
      <div class="chart-pct">${Math.round(p)}%</div>
    </div>`;
}

function renderOverview(data) {
  const empty = $("#overview-empty");
  const kpis = $("#overview-kpis");
  const charts = $("#overview-charts");
  const tbody = $("#table-campaigns tbody");

  if (!data?.ok || !data.global) {
    empty.style.display = "block";
    kpis.innerHTML = "";
    charts.innerHTML = "";
    tbody.innerHTML = "";
    return;
  }
  empty.style.display = "none";
  const warn = $("#overview-warn");
  if (data.skipped_invalid_report) {
    warn.style.display = "block";
    warn.textContent = `Export ignoré (vide / échec) : ${data.skipped_invalid_report}. Affichage du dernier export valide.`;
  } else if (data.export_valid === false) {
    warn.style.display = "block";
    warn.textContent =
      "Cet export ne contient aucun véhicule (scrape échoué — ex. coupure réseau). Choisissez un autre fichier dans la liste.";
  } else {
    warn.style.display = "none";
  }
  const g = data.global.totals;
  const t = data.global;

  renderExecutiveSummary(t.executive_summary);

  kpis.innerHTML = `
    <div class="kpi"><div class="val">${g.vehicles}</div><div class="lbl">Total véhicules</div></div>
    <div class="kpi"><div class="val">${g.vehicles_approved}</div><div class="lbl">Véhicules approuvés</div></div>
    <div class="kpi"><div class="val">${g.drivers_approved}</div><div class="lbl">Conducteurs approuvés</div></div>
    <div class="kpi gold-top"><div class="val">${g.drivers_pending}</div><div class="lbl">Conducteurs en attente</div></div>
    <div class="kpi gold-top"><div class="val">${g.recharge_active}</div><div class="lbl">Chauffeurs actifs</div></div>
    <div class="kpi gold-top"><div class="val">${g.recharge_budget_display}</div><div class="lbl">Budget recharges</div></div>
  `;

  charts.innerHTML =
    chartHtml("Taux d'approbation flotte (global)", g.pct_fleet_approval) +
    chartHtml("Campagnes actives", (t.campaigns.filter((c) => !c.empty).length / 20) * 100);

  tbody.innerHTML = (t.campaigns || [])
    .map(
      (c) => `
    <tr class="clickable" data-idx="${c.index}">
      <td>P${String(c.index).padStart(2, "0")}</td>
      <td>${escapeHtml(c.name)}</td>
      <td>${c.vehicles_count}</td>
      <td>${c.vehicles_approved}</td>
      <td>${c.drivers_count}</td>
      <td>${c.recharge_budget_display}</td>
      <td>${c.pct_fleet_approval}%</td>
    </tr>`,
    )
    .join("");

  tbody.querySelectorAll("tr").forEach((tr) => {
    tr.addEventListener("click", () => {
      const idx = tr.dataset.idx;
      switchTab("campaigns");
      $("#campaign-select").value = idx;
      renderCampaignDetail(parseInt(idx, 10));
    });
  });

  $("#header-meta").textContent = `Dernière maj : ${fmtDate(data.global.generated_at)} · ${data.report_path ? data.report_path.split(/[/\\]/).pop() : ""}`;
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}

function fillReportSelect(reports, currentPath) {
  const sel = $("#report-select");
  if (!sel) return;
  const list = reports || [];
  sel.innerHTML = "";
  for (const r of list) {
    const opt = document.createElement("option");
    opt.value = r.path;
    opt.textContent = r.valid
      ? `${r.name} (${r.vehicles} véh.)`
      : `${r.name} (vide — échec)`;
    if (r.path === currentPath) opt.selected = true;
    sel.appendChild(opt);
  }
  selectedReportPath = sel.value || currentPath || "";
  if (!selectedReportPath && list.length) {
    const best = list.find((r) => r.valid) || list[0];
    sel.value = best.path;
    selectedReportPath = best.path;
  }
}

async function renderCampaignDetail(index) {
  const box = $("#campaign-detail");
  if (!index || Number.isNaN(index)) {
    box.innerHTML = `<p class="empty-msg">Sélectionnez une campagne.</p>`;
    return;
  }
  try {
    const res = await fetch(`/api/campaign/${index}${reportQuery()}`);
    if (!res.ok) {
      let msg = `Erreur ${res.status}`;
      try {
        const err = await res.json();
        if (err.detail) msg = typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail);
      } catch (_) {
        /* ignore */
      }
      throw new Error(msg);
    }
    const data = await res.json();
    const m = data.metrics;
    const es = m.executive_summary;
    box.innerHTML = `
      <div class="campaign-detail-header">
        <h2 style="font-size:20px">Campagne ${m.index} — ${escapeHtml(m.name)}</h2>
        <p style="font-size:13px;color:var(--muted)">${escapeHtml(m.email)}</p>
      </div>
      ${
        es
          ? `<div class="card" style="margin-bottom:16px">
        <h2 style="font-size:16px;margin-bottom:12px">Executive <span class="gold">Summary</span></h2>
        <div class="exec-summary-dash">${renderExecutiveSummaryBlocks(es)}</div>
      </div>`
          : ""
      }
      <div class="kpi-grid">
        <div class="kpi"><div class="val">${m.vehicles_approved}</div><div class="lbl">Véhicules approuvés</div></div>
        <div class="kpi"><div class="val">${m.drivers_approved}</div><div class="lbl">Conducteurs approuvés</div></div>
        <div class="kpi"><div class="val">${m.drivers_pending}</div><div class="lbl">En attente</div></div>
        <div class="kpi gold-top"><div class="val">${m.recharge_count}</div><div class="lbl">Chauffeurs actifs</div></div>
        <div class="kpi gold-top"><div class="val">${m.recharge_budget_display}</div><div class="lbl">Budget rechargé</div></div>
      </div>
      <div class="chart-row">
        ${chartHtml("Taux d'approbation flotte", m.pct_fleet_approval)}
        ${chartHtml("Taux d'approbation conducteur", m.pct_driver_approval)}
      </div>
      <div class="card">
        <a class="btn btn-primary" href="/api/reports/html/file/campagnes/P${String(m.index).padStart(2, "0")}_rapport_activation.html" target="_blank" rel="noopener">
          Ouvrir rapport HTML
        </a>
      </div>
    `;
  } catch (e) {
    box.innerHTML = `<p class="empty-msg">${e.message}</p>`;
  }
}

function fillCampaignSelect(campaigns) {
  const sel = $("#campaign-select");
  sel.innerHTML = (campaigns || [])
    .map((c) => `<option value="${c.index}">P${String(c.index).padStart(2, "0")} — ${escapeHtml(c.name)}</option>`)
    .join("");
  if (sel.options.length) renderCampaignDetail(parseInt(sel.value, 10));
}

async function loadDashboard() {
  try {
    const url = selectedReportPath
      ? `/api/dashboard?report=${encodeURIComponent(selectedReportPath)}`
      : "/api/dashboard";
    const res = await fetch(url);
    dashboardData = await res.json();
    if (dashboardData.available_reports) {
      fillReportSelect(dashboardData.available_reports, dashboardData.report_path);
    }
    renderOverview(dashboardData);
    if (dashboardData.campaigns?.length) {
      fillCampaignSelect(
        dashboardData.campaigns.map((c) => ({ index: c.index, name: c.name })),
      );
    }
  } catch (e) {
    showToast("Erreur chargement : " + e.message);
  }
}

async function loadChauffeursActifsStatus() {
  const box = $("#chauffeurs-actifs-status");
  const link = $("#link-chauffeurs-xlsx");
  if (!box) return;
  try {
    const res = await fetch("/api/chauffeurs-actifs/status");
    const data = await res.json();
    const parts = [];
    if (!data.state_exists) {
      parts.push("⚠ state.json absent — lancez l'orchestrateur.");
    } else {
      parts.push("✓ state.json présent");
    }
    if (data.godseye) {
      parts.push(
        `Godseye : <strong>${escapeHtml(data.godseye.name)}</strong> (${data.godseye.modified})`,
      );
    } else {
      parts.push("Godseye : aucun export — téléchargez la liste « en ligne ».");
    }
    if (data.xlsx) {
      parts.push(
        `Excel : <strong>${escapeHtml(data.xlsx.name)}</strong> — ${escapeHtml(data.xlsx.modified_display || data.xlsx.modified)} (${data.xlsx.size_kb} Ko)`,
      );
      if (link) {
        link.href = data.xlsx.download_url || "/api/chauffeurs-actifs/download";
        link.download = data.xlsx.name;
        link.textContent = `Télécharger ${data.xlsx.name}`;
        link.style.display = "inline-flex";
      }
    } else if (link) {
      link.style.display = "none";
    }
    if (data.xlsx_history && data.xlsx_history.length > 1) {
      const hist = data.xlsx_history
        .slice(1, 6)
        .map(
          (h) =>
            `<a href="${h.download_url}" download="${escapeHtml(h.name)}" style="margin-right:10px">${escapeHtml(h.name)}</a> <span style="color:var(--muted);font-size:11px">${escapeHtml(h.modified_display)}</span>`,
        )
        .join("<br>");
      parts.push(`<span style="font-size:12px;margin-top:6px;display:block">Historique Excel :<br>${hist}</span>`);
    }
    box.innerHTML = parts.join("<br>");

    const archList = $("#list-chauffeurs-archives");
    if (archList) {
      archList.innerHTML = (data.archives || [])
        .map(
          (z) =>
            `<li style="margin-bottom:6px"><a href="${z.download_url}" download="${escapeHtml(z.name)}">${escapeHtml(z.name)}</a> <span style="color:var(--muted);font-size:12px">${escapeHtml(z.modified_display)} · ${z.size_kb} Ko</span></li>`,
        )
        .join("") || "<li>Aucune archive — générez un Excel pour en créer une.</li>";
    }
  } catch (e) {
    box.textContent = "Erreur statut : " + e.message;
  }
}

async function generateChauffeursExcel() {
  const btn = $("#btn-generate-chauffeurs");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/chauffeurs-actifs/generate", { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data.detail || data.error || `HTTP ${res.status}`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    const s = data.stats || {};
    const archMsg = s.archive_name ? ` · archive ${s.archive_name}` : "";
    showToast(
      `Excel généré (${s.output_name || "fichier"}) — ${s.generated_at || ""} — ${s.actifs} actifs, ${s.en_ligne} en ligne${archMsg}`,
    );
    loadChauffeursActifsStatus();
  } catch (e) {
    showToast("Génération : " + e.message);
  } finally {
    if (btn && !document.body.classList.contains("job-running")) btn.disabled = false;
  }
}

async function loadHtmlList() {
  try {
    const res = await fetch("/api/reports/html");
    const data = await res.json();
    $("#list-global-html").innerHTML = (data.global || [])
      .map((f) => `<li><a href="${f.path}" target="_blank">${f.name}</a></li>`)
      .join("") || "<li>Aucun rapport global</li>";
    $("#list-campaign-html").innerHTML = (data.campaigns || [])
      .map(
        (f) =>
          `<a class="btn btn-outline-dark" style="text-decoration:none" href="${f.path}" target="_blank">${f.name.replace("_rapport_activation.html", "")}</a>`,
      )
      .join("");
    const zipRes = await fetch("/api/reports/zip");
    const zipData = await zipRes.json();
    const zipList = $("#list-zip-files");
    if (zipList) {
      zipList.innerHTML = (zipData.files || [])
        .map(
          (z) =>
            `<li style="margin-bottom:6px"><a href="${z.path}" download="${z.name}">${z.name}</a> <span style="color:var(--muted);font-size:12px">${z.size_kb} Ko</span></li>`,
        )
        .join("") || "<li>Aucun ZIP — lancez « Rapports du soir » avec ZIP coché ou « ZIP seulement »</li>";
    }
  } catch (_) {
    /* ignore */
  }
}

function nightlyBody() {
  return {
    headed: $("#chk-headed").checked,
    skip_email: true,
    skip_zip: !$("#chk-zip").checked,
    lots: "1-10,11-20",
  };
}

function updateRunUI(job) {
  if (!job) return;
  lastJobStatus = job;
  const running = job.status === "running";
  $("#run-phase").textContent = job.phase_label || job.phase;
  $("#run-pct").textContent = `${job.progress_pct || 0}%`;
  $("#run-progress").style.width = `${job.progress_pct || 0}%`;
  $("#run-campaign").textContent = formatCampaignProgress(job);
  $("#btn-stop").disabled = !running;
  const btnJobStop = $("#btn-job-stop");
  if (btnJobStop) btnJobStop.disabled = !running;
  updateJobBanner(job);

  const console = $("#log-console");
  console.innerHTML = (job.logs || [])
    .slice(-120)
    .map((line) => {
      let cls = "log-line";
      if (/error|échec|failed/i.test(line)) cls += " err";
      if (/OK|terminé|succès/i.test(line)) cls += " ok";
      return `<div class="${cls}">${escapeHtml(line)}</div>`;
    })
    .join("");
  console.scrollTop = console.scrollHeight;

  if (job.status === "completed" && !notifiedComplete) {
    notifiedComplete = true;
    playSuccessSound();
    const zipDone = (job.logs || []).some((l) => /ZIP lot/i.test(l));
    showToast(
      zipDone
        ? "Terminé — rapports HTML et archives ZIP prêts"
        : "Extraction terminée — rapports du jour prêts",
    );
    loadDashboard();
    loadHtmlList();
    loadChauffeursActifsStatus();
    if (job.kind === "godseye" && job.status === "completed") {
      showToast("Godseye prêt — vous pouvez générer l'Excel chauffeurs actifs");
    }
    updateJobBanner(job);
  }
  if (job.status === "failed" || job.status === "cancelled") {
    updateJobBanner(null);
    setLaunchButtonsDisabled(false);
    $("#job-banner").hidden = true;
    $("#tab-runs-badge").hidden = true;
    if (job.status === "failed") showToast("Échec : " + (job.error || job.phase_label));
    if (job.status === "cancelled") showToast("Tâche arrêtée");
  }
  if (!running && job.status === "completed") {
    updateJobBanner(null);
    setLaunchButtonsDisabled(false);
  }
  if (running) notifiedComplete = false;
}

function connectSSE() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource("/api/runs/stream");
  eventSource.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "job" && msg.job) updateRunUI(msg.job);
    } catch (_) {
      /* ignore */
    }
  };
  eventSource.onerror = () => {
    setTimeout(connectSSE, 3000);
  };
}

async function pollCurrentRun() {
  try {
    const res = await fetch("/api/runs/current");
    const data = await res.json();
    if (data.job) {
      updateRunUI(data.job);
    } else if (!data.running) {
      setLaunchButtonsDisabled(false);
      const banner = $("#job-banner");
      if (banner) banner.hidden = true;
      const badge = $("#tab-runs-badge");
      if (badge) badge.hidden = true;
    }
  } catch (_) {
    /* ignore */
  }
}

async function startRun(endpoint, body) {
  notifiedComplete = false;
  const res = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json();
  if (!data.ok) {
    showToast(data.error || "Impossible de démarrer");
    return;
  }
  showToast("Tâche démarrée — vous pouvez continuer à naviguer (bandeau en haut)");
  setTimeout(pollCurrentRun, 500);
}

function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${name}`));
  if (name === "recharges") loadRechargesList();
}

let lastRechargeCompare = null;
let lastRechargesDrivers = [];

function updateMarkSelectedButton() {
  const btn = $("#btn-recharges-mark-selected");
  if (!btn) return;
  const n = $$("#table-recharges tbody input.recharge-row-cb:checked").length;
  btn.disabled = n === 0;
  btn.textContent = n > 0 ? `Marquer la sélection (${n})` : "Marquer la sélection";
}

function rechargeListQuery() {
  const view = $("#recharges-view-filter")?.value || "to_recharge";
  const partner = $("#recharges-list-partner")?.value || "";
  const params = new URLSearchParams({ view });
  if (partner) params.set("partner_index", partner);
  return params.toString();
}

function statusLabel(status, row) {
  const map = {
    to_recharge: '<span style="color:#b8860b">À recharger</span>',
    recharged: '<span style="color:#2e7d32">Rechargé</span>',
    invalid_phone: '<span style="color:#c62828">Tél. invalide</span>',
    pending: '<span style="color:var(--muted)">En attente</span>',
    unassigned: '<span style="color:#c62828">Non assigné</span>',
    not_on_fleet: '<span style="color:#ef6c00">Absent table flotte</span>',
    pending_assignment: '<span style="color:#ef6c00">Assigné — en attente</span>',
    other: '<span style="color:var(--muted)">—</span>',
  };
  if (row?.admin_reason_label && (status === "unassigned" || status === "not_on_fleet" || status === "pending_assignment")) {
    const color = status === "not_on_fleet" || status === "pending_assignment" ? "#ef6c00" : "#c62828";
    return `<span style="color:${color}">${escapeHtml(row.admin_reason_label)}</span>`;
  }
  return map[status] || escapeHtml(row?.admin_reason_label || status || "");
}

function renderPartnersPendingSelect(partners) {
  const sel = $("#recharges-partner-select");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML =
    '<option value="">— Choisir —</option>' +
    (partners || [])
      .map(
        (p) =>
          `<option value="${p.partner_index}">P${String(p.partner_index).padStart(2, "0")} — ${escapeHtml(p.partner_name || "")} (${p.pending} à recharger)</option>`,
      )
      .join("");
  if (cur && [...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

function renderListPartnerSelect(partners) {
  const sel = $("#recharges-list-partner");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML =
    '<option value="">Toutes</option>' +
    (partners || [])
      .map(
        (p) =>
          `<option value="${p.partner_index}">P${String(p.partner_index).padStart(2, "0")} — ${escapeHtml(p.partner_name || "")} (${p.drivers_total})</option>`,
      )
      .join("");
  if (cur && [...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

function updateRechargesListHint(view, count) {
  const hint = $("#recharges-list-hint");
  const cnt = $("#recharges-list-count");
  const labels = {
    to_recharge: "Chauffeurs approved non encore rechargés (éligibles Appium).",
    all: "Tous les chauffeurs enregistrés dans state.json.",
    actifs: "Chauffeurs avec admin.reason = assigned_approved.",
    non_assignes: "Chauffeurs sans assignation approuvée (pas assigned_approved).",
    recharged: "Chauffeurs approved déjà marqués rechargés.",
  };
  if (hint) hint.textContent = labels[view] || labels.to_recharge;
  if (cnt) cnt.textContent = count != null ? `${count} ligne(s)` : "";
}

function renderRechargesSummary(summary) {
  const box = $("#recharges-summary");
  if (!box || !summary) return;
  const s = summary;
  box.innerHTML = `
    <div class="kpi"><div class="val">${s.drivers_total ?? 0}</div><div class="lbl">Total chauffeurs</div></div>
    <div class="kpi"><div class="val">${s.actifs ?? 0}</div><div class="lbl">Actifs (approved)</div></div>
    <div class="kpi"><div class="val">${s.non_assignes ?? 0}</div><div class="lbl">Non assignés</div></div>
    <div class="kpi gold-top"><div class="val">${s.a_recharger ?? 0}</div><div class="lbl">À recharger</div></div>
    <div class="kpi"><div class="val">${s.recharges ?? 0}</div><div class="lbl">Déjà rechargés</div></div>
    <div class="kpi"><div class="val">${s.invalid_phone ?? 0}</div><div class="lbl">Tél. invalides</div></div>
  `;
}

function renderRechargesTable(drivers, view) {
  const tbody = $("#table-recharges tbody");
  if (!tbody) return;
  const showCheckboxes = view === "to_recharge";
  lastRechargesDrivers = drivers || [];
  const selectAll = $("#recharges-select-all");
  const thCb = selectAll?.closest("th");
  if (selectAll) {
    selectAll.checked = false;
    selectAll.disabled = !showCheckboxes;
  }
  if (thCb) thCb.style.visibility = showCheckboxes ? "visible" : "hidden";
  const markSel = $("#btn-recharges-mark-selected");
  if (markSel) markSel.style.display = showCheckboxes ? "" : "none";
  if (!drivers?.length) {
    const total = window._lastRechargesSummary?.drivers_total ?? 0;
    let msg = "Aucun chauffeur pour ce filtre.";
    if (view === "to_recharge" && total > 0) {
      msg = `Aucun à recharger (${total} chauffeur(s) au total — choisissez « Tous les chauffeurs »).`;
    } else if (view !== "to_recharge" && total > 0 && !window._rechargesApiHasView) {
      msg = `Liste vide : redémarrez le dashboard (Ctrl+F5). ${total} chauffeur(s) dans state.json.`;
    }
    tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--muted)">${msg}</td></tr>`;
    updateMarkSelectedButton();
    return;
  }
  tbody.innerHTML = drivers
    .map((d) => {
      const note = d.phone_invalid && d.status !== "invalid_phone"
        ? '<span style="color:#c62828">numéro invalide</span>'
        : "";
      const key = escapeHtml(d.match_key || "");
      const canMark = showCheckboxes && d.status === "to_recharge";
      const cb = canMark
        ? `<input type="checkbox" class="recharge-row-cb" value="${key}" />`
        : "";
      return `<tr data-match-key="${key}">
        <td>${cb}</td>
        <td>P${String(d.partner_index).padStart(2, "0")}</td>
        <td>${escapeHtml(d.name)}</td>
        <td>${renderPhoneCell(d)}</td>
        <td>${escapeHtml(d.plate)}</td>
        <td>${statusLabel(d.status, d)}</td>
        <td>${note}</td>
      </tr>`;
    })
    .join("");
  $$("#table-recharges tbody input.recharge-row-cb").forEach((cb) => {
    cb.addEventListener("change", updateMarkSelectedButton);
  });
  updateMarkSelectedButton();
}

async function loadRechargesList() {
  try {
    const view = $("#recharges-view-filter")?.value || "to_recharge";
    const res = await fetch(`/api/recharges?${rechargeListQuery()}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    window._lastRechargesSummary = data.summary;
    window._rechargesApiHasView = data.view != null;
    renderRechargesSummary(data.summary);
    renderPartnersPendingSelect(data.partners_pending);
    renderListPartnerSelect(data.partners);
    renderRechargesTable(data.drivers, data.view || view);
    updateRechargesListHint(data.view || view, data.drivers_count);
    const link = $("#link-recharges-export-state");
    if (link && data.export_filename_example) {
      link.setAttribute("download", data.export_filename_example);
    }
  } catch (e) {
    showToast("Recharges : " + e.message);
  }
}

async function markRechargesSelected() {
  const keys = [...$$("#table-recharges tbody input.recharge-row-cb:checked")]
    .map((cb) => cb.closest("tr")?.dataset.matchKey || cb.value)
    .filter(Boolean);
  if (!keys.length) {
    showToast("Sélectionnez au moins un chauffeur");
    return;
  }
  if (!confirm(`Marquer ${keys.length} chauffeur(s) comme rechargé(s) ?`)) return;
  const btn = $("#btn-recharges-mark-selected");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/recharges/mark", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ match_keys: keys }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data.detail || `HTTP ${res.status}`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    await loadRechargesList();
    showToast(`${data.marked_count} chauffeur(s) marqué(s) rechargé(s)`);
  } catch (e) {
    showToast("Marquage : " + e.message);
  } finally {
    updateMarkSelectedButton();
  }
}

async function markRechargesPartner() {
  const sel = $("#recharges-partner-select");
  const partnerIndex = sel?.value ? parseInt(sel.value, 10) : NaN;
  if (!partnerIndex || Number.isNaN(partnerIndex)) {
    showToast("Choisissez un partenaire");
    return;
  }
  const label = sel.options[sel.selectedIndex]?.text || `P${partnerIndex}`;
  if (
    !confirm(
      `Marquer tous les chauffeurs « à recharger » du partenaire ${label} comme rechargés ?`,
    )
  ) {
    return;
  }
  const btn = $("#btn-recharges-mark-partner");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/recharges/mark", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        partner_index: partnerIndex,
        all_pending_in_partner: true,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data.detail || `HTTP ${res.status}`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    await loadRechargesList();
    showToast(`${data.marked_count} chauffeur(s) marqué(s) pour ${label}`);
  } catch (e) {
    showToast("Marquage partenaire : " + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderCompareResult(data) {
  const box = $("#recharges-compare-box");
  const detail = $("#recharges-compare-detail");
  if (!box) return;
  const sc = data.summary_current || {};
  const su = data.summary_uploaded || {};
  box.innerHTML = `
    <p><strong>Fichier :</strong> ${escapeHtml(data.uploaded_filename || "")}</p>
    <p><strong>State local</strong> — actifs ${sc.actifs}, rechargés ${sc.recharges}, à recharger <strong>${sc.a_recharger}</strong></p>
    <p><strong>State uploadé</strong> — actifs ${su.actifs}, rechargés ${su.recharges}, à recharger <strong>${su.a_recharger}</strong></p>
    <p>Devenus rechargés dans l'upload : <strong>${(data.became_recharged_in_upload || []).length}</strong>
    · Uniquement local à recharger : <strong>${(data.only_current_recharge || []).length}</strong>
    · Uniquement upload à recharger : <strong>${(data.only_uploaded_recharge || []).length}</strong>
    · Écarts détectés : <strong>${data.diff_count ?? 0}</strong></p>
  `;
  if (!detail) return;
  const parts = [];
  if (data.became_recharged_in_upload?.length) {
    parts.push(
      "<h4 style='margin:12px 0 6px'>Nouveaux rechargés (upload vs local)</h4><ul>" +
        data.became_recharged_in_upload
          .map(
            (r) =>
              `<li>P${String(r.partner_index).padStart(2, "0")} ${escapeHtml(r.name)}</li>`,
          )
          .join("") +
        "</ul>",
    );
  }
  if (data.only_current_recharge?.length) {
    parts.push(
      "<h4 style='margin:12px 0 6px'>À recharger seulement dans le state local</h4><ul>" +
        data.only_current_recharge
          .slice(0, 30)
          .map(
            (r) =>
              `<li>P${String(r.partner_index).padStart(2, "0")} ${escapeHtml(r.name)} — ${escapeHtml(r.phone)}</li>`,
          )
          .join("") +
        (data.only_current_recharge.length > 30 ? "<li>…</li>" : "") +
        "</ul>",
    );
  }
  if (data.only_uploaded_recharge?.length) {
    parts.push(
      "<h4 style='margin:12px 0 6px'>À recharger seulement dans l'upload</h4><ul>" +
        data.only_uploaded_recharge
          .slice(0, 30)
          .map(
            (r) =>
              `<li>P${String(r.partner_index).padStart(2, "0")} ${escapeHtml(r.name)} — ${escapeHtml(r.phone)}</li>`,
          )
          .join("") +
        (data.only_uploaded_recharge.length > 30 ? "<li>…</li>" : "") +
        "</ul>",
    );
  }
  detail.innerHTML = parts.join("") || "<p>Aucun écart notable sur la liste à recharger.</p>";
}

async function compareRechargesState() {
  const input = $("#recharges-state-file");
  const file = input?.files?.[0];
  if (!file) {
    showToast("Choisissez un fichier (state.json, state (1).json…)");
    return;
  }
  const fd = new FormData();
  fd.append("file", file, file.name);
  const btn = $("#btn-recharges-compare");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/recharges/compare", { method: "POST", body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data.detail || `HTTP ${res.status}`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    lastRechargeCompare = data;
    renderCompareResult(data);
    const applyBtn = $("#btn-recharges-apply");
    if (applyBtn) applyBtn.disabled = false;
    showToast("Comparaison terminée");
  } catch (e) {
    showToast("Comparaison : " + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function applyRechargesState() {
  const input = $("#recharges-state-file");
  const file = input?.files?.[0];
  if (!file) {
    showToast("Choisissez un fichier (state.json, state (1).json…)");
    return;
  }
  if (
    !confirm(
      `Fusionner « ${file.name} » dans le state local ?\nUne sauvegarde sera créée. Les recharges déjà marquées sont conservées.`,
    )
  ) {
    return;
  }
  const fd = new FormData();
  fd.append("file", file, file.name);
  const btn = $("#btn-recharges-apply");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/recharges/apply", { method: "POST", body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data.detail || `HTTP ${res.status}`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    await loadRechargesList();
    showToast(
      `« ${data.uploaded_filename || file.name} » fusionné — ${data.summary?.a_recharger ?? 0} à recharger`,
    );
    lastRechargeCompare = null;
    $("#recharges-compare-detail").innerHTML = "";
    $("#recharges-compare-box").innerHTML =
      "<p style='color:var(--muted)'>Fusion appliquée. Relancez une comparaison si besoin.</p>";
  } catch (e) {
    showToast("Fusion : " + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function loadScheduler() {
  const res = await fetch("/api/scheduler");
  const s = await res.json();
  $("#chk-auto").checked = s.enabled;
  if (s.time) {
    $("#auto-time").value = s.time;
    const el = $("#sched-time-display");
    if (el) el.textContent = s.time;
  }
  const parts = [
    `Dernière auto : ${s.last_auto_run_date || "—"}`,
    s.last_at ? `à ${s.last_at.replace("T", " ")}` : "",
    s.last_result ? `(${s.last_result})` : "",
  ].filter(Boolean);
  if (s.enabled && s.next_run_label) {
    parts.push(`Prochaine : ${s.next_run_label}`);
  } else if (!s.enabled) {
    parts.push("Auto désactivée");
  }
  $("#scheduler-info").textContent = parts.join(" · ");
}

async function saveScheduler() {
  await fetch("/api/scheduler", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      enabled: $("#chk-auto").checked,
      time: $("#auto-time").value,
    }),
  });
  showToast("Planification enregistrée");
  loadScheduler();
}

function bindEvents() {
  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  });

  $("#btn-refresh").addEventListener("click", () => {
    loadDashboard();
    loadHtmlList();
    loadChauffeursActifsStatus();
    loadRechargesList();
    pollCurrentRun();
  });

  $("#btn-recharges-refresh")?.addEventListener("click", loadRechargesList);
  $("#table-recharges tbody")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".btn-copy-phone");
    if (!btn) return;
    e.preventDefault();
    copyPhoneToClipboard(btn.dataset.phone || "");
  });
  $("#recharges-view-filter")?.addEventListener("change", loadRechargesList);
  $("#recharges-list-partner")?.addEventListener("change", loadRechargesList);
  $("#btn-recharges-compare")?.addEventListener("click", compareRechargesState);
  $("#btn-recharges-apply")?.addEventListener("click", applyRechargesState);
  $("#btn-recharges-mark-selected")?.addEventListener("click", markRechargesSelected);
  $("#btn-recharges-mark-partner")?.addEventListener("click", markRechargesPartner);
  $("#recharges-select-all")?.addEventListener("change", (e) => {
    const checked = e.target.checked;
    $$("#table-recharges tbody input.recharge-row-cb").forEach((cb) => {
      cb.checked = checked;
    });
    updateMarkSelectedButton();
  });
  $("#link-recharges-export-state")?.addEventListener("click", () => {
    setTimeout(loadRechargesList, 1500);
  });

  $("#btn-godseye-download")?.addEventListener("click", () =>
    startRun("/api/runs/godseye-download", { headed: headed() }),
  );
  $("#btn-generate-chauffeurs")?.addEventListener("click", generateChauffeursExcel);

  $("#btn-archive-chauffeurs-bundle")?.addEventListener("click", async () => {
    const btn = $("#btn-archive-chauffeurs-bundle");
    if (btn) btn.disabled = true;
    try {
      const res = await fetch("/api/chauffeurs-actifs/archives", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = data.detail || `HTTP ${res.status}`;
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      showToast(`Archive créée : ${data.archive_name} (${data.size_kb} Ko)`);
      loadChauffeursActifsStatus();
    } catch (e) {
      showToast("Archivage : " + e.message);
    } finally {
      if (btn && !document.body.classList.contains("job-running")) btn.disabled = false;
    }
  });

  const headed = () => $("#chk-headed").checked;

  $("#btn-nightly").addEventListener("click", () => startRun("/api/runs/nightly", nightlyBody()));
  $("#btn-orchestrator").addEventListener("click", () =>
    startRun("/api/runs/orchestrator", { headed: headed() }),
  );
  $("#btn-run-nightly").addEventListener("click", () => startRun("/api/runs/nightly", nightlyBody()));
  $("#btn-run-zip").addEventListener("click", () =>
    startRun("/api/runs/zip-only", { lots: "1-10,11-20" }),
  );
  $("#btn-run-orch").addEventListener("click", () =>
    startRun("/api/runs/orchestrator", { headed: headed() }),
  );
  $("#btn-run-html").addEventListener("click", () => startRun("/api/runs/html-only", {}));
  async function requestStop() {
    const res = await fetch("/api/runs/stop", { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (data.stopped) {
      showToast("Arrêt en cours — fermeture Selenium et scripts…");
    } else {
      showToast("Aucune tâche active à arrêter");
    }
    setTimeout(pollCurrentRun, 400);
    setTimeout(pollCurrentRun, 2000);
  }

  $("#btn-stop").addEventListener("click", requestStop);
  $("#btn-job-stop")?.addEventListener("click", requestStop);
  $("#btn-job-logs")?.addEventListener("click", () => switchTab("runs"));

  $("#campaign-select").addEventListener("change", (e) =>
    renderCampaignDetail(parseInt(e.target.value, 10)),
  );

  $("#btn-save-scheduler").addEventListener("click", saveScheduler);

  $("#report-select").addEventListener("change", (e) => {
    selectedReportPath = e.target.value;
    loadDashboard();
  });
}

document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  loadDashboard();
  loadHtmlList();
  loadChauffeursActifsStatus();
  loadRechargesList();
  loadScheduler();
  connectSSE();
  pollCurrentRun();
  setInterval(pollCurrentRun, 5000);
});
