const uploadInput = document.getElementById("uploadInput");
const cameraInput = document.getElementById("cameraInput");
const scanBtn = document.getElementById("scanBtn");
const preview = document.getElementById("preview");
const previewSection = document.querySelector(".preview-section");
const scanStatus = document.getElementById("scanStatus");
const errorMessage = document.getElementById("errorMessage");
const ocrText = document.getElementById("ocrText");
const form = document.getElementById("detailsForm");
const confirmBtn = document.getElementById("confirmBtn");
const toast = document.getElementById("toast");
const copyLastJsonBtn = document.getElementById("copyLastJsonBtn");
const lastCardFields = document.getElementById("lastCardFields");
const lastCardJson = document.getElementById("lastCardJson");

let confirmedOnce = false;
let selectedFile = null;
let lastConfirmedCard = null;

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 2200);
}

function setFormData(data = {}) {
  const fields = ["name", "number", "address", "website", "company_name", "designation"];
  for (const field of fields) {
    const input = form.elements[field];
    if (input) {
      input.value = data[field] ?? "";
    }
  }
}

function getFormData() {
  const fields = ["name", "number", "address", "website", "company_name", "designation"];
  const data = {};
  for (const field of fields) {
    const input = form.elements[field];
    const value = input?.value.trim();
    data[field] = value ? value : null;
  }
  return data;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function displayValue(value) {
  if (value === null || value === undefined) return "—";
  const text = String(value).trim();
  return text ? text : "—";
}

function formatDate(isoString) {
  if (!isoString) return "";
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return isoString;
  return date.toLocaleString();
}

function renderLastConfirmedCard(card) {
  if (!lastCardFields || !lastCardJson) return;
  lastConfirmedCard = card || null;

  if (!lastConfirmedCard) {
    lastCardFields.innerHTML = '<div class="kv muted">No confirmed card yet.</div>';
    lastCardJson.textContent = "{}";
    copyLastJsonBtn && (copyLastJsonBtn.disabled = true);
    return;
  }

  const fields = [
    ["ID", lastConfirmedCard.id],
    ["Name", lastConfirmedCard.name],
    ["Phone", lastConfirmedCard.number],
    ["Company", lastConfirmedCard.company_name],
    ["Title", lastConfirmedCard.designation],
    ["Website", lastConfirmedCard.website],
    ["Address", lastConfirmedCard.address],
    ["Confirmed", formatDate(lastConfirmedCard.confirmed_at)],
  ];

  lastCardFields.innerHTML = fields
    .map(([label, value]) => {
      return `
        <div class="kv">
          <div class="kv-label">${escapeHtml(label)}</div>
          <div class="kv-value">${escapeHtml(displayValue(value))}</div>
        </div>
      `;
    })
    .join("");

  lastCardJson.textContent = JSON.stringify(lastConfirmedCard, null, 2);
  copyLastJsonBtn && (copyLastJsonBtn.disabled = false);
}

async function scanCard(file) {
  if (!file) return;

  const formData = new FormData();
  formData.append("image", file);

  previewSection.style.display = "block";
  preview.src = URL.createObjectURL(file);
  scanStatus.textContent = "Scanning image... please wait.";
  errorMessage.textContent = "";
  scanBtn.disabled = true;
  confirmBtn.disabled = true;
  confirmedOnce = false;

  try {
    const res = await fetch("/scan", { method: "POST", body: formData });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || "Failed to scan card.");

    setFormData(payload.data || {});
    ocrText.textContent = payload.raw_text || "No text detected.";
    confirmBtn.disabled = false;
    scanStatus.textContent = "Scan complete. Please review and confirm entries.";
    showToast("Business card scanned successfully");
  } catch (error) {
    ocrText.textContent = `Error: ${error.message}`;
    scanStatus.textContent = "Scan failed.";
    errorMessage.textContent = error.message;
    showToast(error.message);
  } finally {
    scanBtn.disabled = !selectedFile;
  }
}

function onFileSelected(file) {
  if (!file) return;
  selectedFile = file;
  previewSection.style.display = "block";
  preview.src = URL.createObjectURL(file);
  scanBtn.disabled = false;
  scanStatus.textContent = "Image ready. Tap Scan Card to start conversion.";
  errorMessage.textContent = "";
}

uploadInput.addEventListener("change", (e) => onFileSelected(e.target.files[0]));
cameraInput.addEventListener("change", (e) => onFileSelected(e.target.files[0]));
scanBtn.addEventListener("click", () => scanCard(selectedFile));

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (confirmedOnce) return;

  confirmBtn.disabled = true;
  const data = getFormData();

  try {
    const res = await fetch("/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data }),
    });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || "Failed to save.");

    confirmedOnce = true;
    scanStatus.textContent = "Entry confirmed and saved to knowledge base.";
    errorMessage.textContent = "";
    showToast("Confirmed and saved to knowledge base");
    renderLastConfirmedCard(payload.data);
  } catch (error) {
    confirmBtn.disabled = false;
    errorMessage.textContent = error.message;
    showToast(error.message);
  }
});

copyLastJsonBtn?.addEventListener("click", async () => {
  if (!lastConfirmedCard) return;
  try {
    await navigator.clipboard.writeText(JSON.stringify(lastConfirmedCard, null, 2));
    showToast("Copied JSON");
  } catch {
    showToast("Copy failed");
  }
});

renderLastConfirmedCard(null);
