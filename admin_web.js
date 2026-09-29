"use strict";

let csrfToken = "";
let currentLicense = null;
let pendingAction = null;

const $ = (selector) => document.querySelector(selector);
const loginView = $("#login-view");
const appView = $("#app-view");
const listElement = $("#license-list");
const appMessage = $("#app-message");
const detailDialog = $("#detail-dialog");
const confirmDialog = $("#confirm-dialog");
const deleteDialog = $("#delete-dialog");

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = { "Accept": "application/json", ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  if (!["GET", "HEAD"].includes(method) && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const response = await fetch(path, { ...options, method, headers, credentials: "same-origin" });
  let data = {};
  try { data = await response.json(); } catch (_) { data = {}; }
  if (response.status === 401) {
    showLogin();
    throw new Error("Sessão expirada. Entre novamente.");
  }
  if (!response.ok || !data.ok) throw new Error(data.message || "Operação não concluída.");
  return data;
}

function showLogin() {
  csrfToken = "";
  currentLicense = null;
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  appView.hidden = true;
  loginView.hidden = false;
  $("#password").value = "";
  $("#totp").value = "";
}

function showApp() {
  loginView.hidden = true;
  appView.hidden = false;
}

function statusInfo(item) {
  if (item.status === "blocked") return ["Bloqueada", "status-blocked"];
  if (Number(item.days_remaining) < 0) return ["Vencida", "status-expired"];
  return ["Ativa", "status-active"];
}

function formatDate(value) {
  if (!value) return "-";
  const [year, month, day] = value.slice(0, 10).split("-");
  return `${day}/${month}/${year}`;
}

function meta(label, value) {
  const wrap = document.createElement("div");
  wrap.textContent = label;
  const strong = document.createElement("strong");
  strong.textContent = value;
  wrap.appendChild(strong);
  return wrap;
}

function renderLicenses(items) {
  listElement.replaceChildren();
  if (!items.length) {
    appMessage.textContent = "Nenhuma licença encontrada.";
    return;
  }
  appMessage.textContent = `${items.length} licença(s)`;
  for (const item of items) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "license-card";
    card.addEventListener("click", () => openLicense(item.license_key));
    const top = document.createElement("div");
    top.className = "license-top";
    const client = document.createElement("span");
    client.className = "license-client";
    client.textContent = item.customer || "Sem cliente";
    const [statusText, statusClass] = statusInfo(item);
    const status = document.createElement("span");
    status.className = `status ${statusClass}`;
    status.textContent = statusText;
    top.append(client, status);
    const key = document.createElement("span");
    key.className = "license-key";
    key.textContent = item.license_key;
    const details = document.createElement("div");
    details.className = "license-meta";
    details.append(meta("Produto", item.product), meta("Vencimento", formatDate(item.expires_at)), meta("Dias restantes", String(item.days_remaining)), meta("Máquinas", `${item.machines}/${item.max_machines}`));
    card.append(top, key, details);
    listElement.appendChild(card);
  }
}

async function loadLicenses(search = "") {
  appMessage.textContent = "Carregando...";
  const query = search ? `?search=${encodeURIComponent(search)}` : "";
  try { renderLicenses((await api(`/admin/licenses${query}`)).licenses || []); }
  catch (error) { appMessage.textContent = error.message; }
}

function addDetail(label, value) {
  const wrap = document.createElement("div");
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = label;
  description.textContent = value;
  wrap.append(term, description);
  $("#detail-fields").appendChild(wrap);
}

function addDeleteDetail(label, value) {
  const wrap = document.createElement("div");
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = label;
  description.textContent = value;
  wrap.append(term, description);
  $("#delete-fields").appendChild(wrap);
}

async function openLicense(key) {
  try {
    const [licenseData, activationData] = await Promise.all([
      api(`/admin/licenses/${encodeURIComponent(key)}`),
      api(`/admin/licenses/${encodeURIComponent(key)}/activations`),
    ]);
    currentLicense = licenseData.license;
    $("#detail-customer").textContent = currentLicense.customer || "Sem cliente";
    $("#detail-fields").replaceChildren();
    addDetail("Chave", currentLicense.license_key);
    addDetail("Produto", currentLicense.product);
    addDetail("Status", statusInfo(currentLicense)[0]);
    addDetail("Vencimento", formatDate(currentLicense.expires_at));
    addDetail("Dias restantes", String(currentLicense.days_remaining));
    addDetail("Máquinas", `${currentLicense.machines}/${currentLicense.max_machines}`);
    renderActivations(activationData.activations || []);
    $("#detail-error").textContent = "";
    if (!detailDialog.open) detailDialog.showModal();
  } catch (error) { appMessage.textContent = error.message; }
}

function renderActivations(items) {
  const target = $("#activation-list");
  target.replaceChildren();
  if (!items.length) {
    target.textContent = "Nenhuma máquina vinculada.";
    return;
  }
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "activation-item";
    const text = document.createElement("div");
    const id = document.createElement("div");
    id.className = "activation-id";
    id.textContent = item.machine_id;
    const dates = document.createElement("small");
    dates.textContent = `Primeiro uso: ${item.first_seen} | Último uso: ${item.last_seen}`;
    text.append(id, dates);
    const button = document.createElement("button");
    button.className = "danger";
    button.type = "button";
    button.textContent = "Desvincular";
    button.addEventListener("click", () => confirmAction("Desvincular máquina", `Máquina: ${item.machine_id}`, async () => {
      await api(`/admin/licenses/${encodeURIComponent(currentLicense.license_key)}/activations/${encodeURIComponent(item.machine_id)}`, { method: "DELETE" });
      await openLicense(currentLicense.license_key);
    }));
    row.append(text, button);
    target.appendChild(row);
  }
}

function confirmAction(title, body, action) {
  $("#confirm-title").textContent = title;
  $("#confirm-body").textContent = body;
  pendingAction = action;
  confirmDialog.showModal();
}

$("#confirm-cancel").addEventListener("click", () => { pendingAction = null; confirmDialog.close(); });
$("#confirm-accept").addEventListener("click", async () => {
  const action = pendingAction;
  pendingAction = null;
  confirmDialog.close();
  if (!action) return;
  try { await action(); await loadLicenses($("#search").value.trim()); }
  catch (error) { $("#detail-error").textContent = error.message; }
});

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#login-error").textContent = "";
  try {
    const data = await api("/admin/web-login", {
      method: "POST",
      body: JSON.stringify({ password: $("#password").value, totp: $("#totp").value }),
    });
    csrfToken = data.csrf_token;
    $("#password").value = "";
    $("#totp").value = "";
    showApp();
    await loadLicenses();
  } catch (error) { $("#login-error").textContent = error.message; }
});

$("#logout-button").addEventListener("click", async () => {
  try { await api("/admin/web-logout", { method: "POST" }); } catch (_) {}
  showLogin();
});

$("#search-form").addEventListener("submit", (event) => { event.preventDefault(); loadLicenses($("#search").value.trim()); });
$("#new-license-button").addEventListener("click", () => {
  $("#create-form").reset();
  const defaultDate = new Date();
  defaultDate.setDate(defaultDate.getDate() + 30);
  $("#expires-at").value = defaultDate.toISOString().slice(0, 10);
  $("#created-key").hidden = true;
  $("#create-error").textContent = "";
  $("#create-form").querySelector("button[type='submit']").hidden = false;
  $("#create-dialog").showModal();
});

$("#create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const data = await api("/admin/licenses", { method: "POST", body: JSON.stringify({
      customer: $("#customer").value.trim(), product: $("#product").value,
      expires_at: $("#expires-at").value, max_machines: Number($("#max-machines").value),
    }) });
    const keyBox = $("#created-key");
    keyBox.querySelector("strong").textContent = data.license.license_key;
    keyBox.hidden = false;
    $("#create-form").querySelector("button[type='submit']").hidden = true;
    await loadLicenses();
  } catch (error) { $("#create-error").textContent = error.message; }
});

$("#copy-key-button").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("#created-key strong").textContent);
    $("#copy-key-button").textContent = "Copiada";
  } catch (_) {
    $("#create-error").textContent = "Não foi possível copiar. Selecione a chave manualmente.";
  }
});

$("#expiry-operation").addEventListener("change", () => {
  const exact = $("#expiry-operation").value === "exact";
  $("#expiry-days").hidden = exact;
  $("#expiry-date").hidden = !exact;
});

$("#change-expiry-button").addEventListener("click", () => {
  if (!currentLicense) return;
  const operation = $("#expiry-operation").value;
  let next;
  if (operation === "exact") next = $("#expiry-date").value;
  else {
    const current = new Date(`${currentLicense.expires_at}T12:00:00`);
    const days = Number($("#expiry-days").value);
    current.setDate(current.getDate() + (operation === "add" ? days : -days));
    next = current.toISOString().slice(0, 10);
  }
  if (!next) { $("#detail-error").textContent = "Informe a nova validade."; return; }
  confirmAction("Alterar validade", `Vencimento atual: ${formatDate(currentLicense.expires_at)}\nNovo vencimento: ${formatDate(next)}`, async () => {
    await api(`/admin/licenses/${encodeURIComponent(currentLicense.license_key)}`, { method: "PATCH", body: JSON.stringify({ expires_at: next }) });
    detailDialog.close();
  });
});

$("#block-button").addEventListener("click", () => currentLicense && confirmAction("Bloquear licença", `${currentLicense.customer}\n${currentLicense.license_key}`, async () => {
  await api(`/admin/licenses/${encodeURIComponent(currentLicense.license_key)}`, { method: "PATCH", body: JSON.stringify({ status: "blocked" }) });
  detailDialog.close();
}));

$("#unblock-button").addEventListener("click", () => currentLicense && confirmAction("Liberar licença", `${currentLicense.customer}\n${currentLicense.license_key}`, async () => {
  await api(`/admin/licenses/${encodeURIComponent(currentLicense.license_key)}`, { method: "PATCH", body: JSON.stringify({ status: "active" }) });
  detailDialog.close();
}));

$("#delete-license-button").addEventListener("click", () => {
  if (!currentLicense) return;
  $("#delete-fields").replaceChildren();
  addDeleteDetail("Cliente", currentLicense.customer || "-");
  addDeleteDetail("Produto", currentLicense.product || "-");
  addDeleteDetail("Chave", currentLicense.license_key);
  addDeleteDetail("Status", statusInfo(currentLicense)[0]);
  addDeleteDetail("Vencimento", formatDate(currentLicense.expires_at));
  addDeleteDetail("Máquinas", `${currentLicense.machines}/${currentLicense.max_machines}`);
  $("#delete-confirmation").value = "";
  $("#delete-error").textContent = "";
  $("#delete-confirm-button").disabled = true;
  deleteDialog.showModal();
});

$("#delete-confirmation").addEventListener("input", () => {
  $("#delete-confirm-button").disabled = !currentLicense || $("#delete-confirmation").value !== currentLicense.license_key;
});

$("#delete-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentLicense || $("#delete-confirmation").value !== currentLicense.license_key) {
    $("#delete-error").textContent = "Digite a chave completa exatamente como exibida.";
    return;
  }
  try {
    await api(`/admin/licenses/${encodeURIComponent(currentLicense.license_key)}`, {
      method: "DELETE",
      body: JSON.stringify({ confirmation_key: $("#delete-confirmation").value }),
    });
    deleteDialog.close();
    detailDialog.close();
    currentLicense = null;
    await loadLicenses($("#search").value.trim());
    appMessage.textContent = "Licença excluída.";
  } catch (error) { $("#delete-error").textContent = error.message; }
});

document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => $("#" + button.dataset.close).close()));

(async () => {
  try {
    const session = await api("/admin/session");
    csrfToken = session.csrf_token;
    showApp();
    await loadLicenses();
  } catch (_) { showLogin(); }
})();
