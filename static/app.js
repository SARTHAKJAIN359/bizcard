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
const refreshCardsBtn = document.getElementById("refreshCardsBtn");
const cardsTbody = document.getElementById("cardsTbody");

let confirmedOnce = false;
let selectedFile = null;

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

function formatDate(isoString) {
  if (!isoString) return "";
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return isoString;
  return date.toLocaleString();
}

async function refreshCards() {
  if (!cardsTbody) return;
  refreshCardsBtn && (refreshCardsBtn.disabled = true);

  try {
    const res = await fetch("/cards");
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || "Failed to load saved cards.");

    const cards = payload.cards || [];
    if (!cards.length) {
      cardsTbody.innerHTML = '<tr><td colspan="8" class="muted">No saved cards yet.</td></tr>';
      return;
    }

    cardsTbody.innerHTML = cards
      .map((card) => {
        const json = JSON.stringify(card);
        const compact = JSON.stringify(card, null, 0);
        return `
          <tr>
            <td>${escapeHtml(card.id)}</td>
            <td>${escapeHtml(card.name)}</td>
            <td>${escapeHtml(card.number)}</td>
            <td>${escapeHtml(card.company_name)}</td>
            <td>${escapeHtml(card.designation)}</td>
            <td>${escapeHtml(card.website)}</td>
            <td>${escapeHtml(formatDate(card.confirmed_at))}</td>
            <td class="actions-cell">
              <button class="btn tiny" type="button" data-copy='${escapeHtml(json)}'>Copy JSON</button>
            </td>
          </tr>
        `;
      })
      .join("");
  } catch (error) {
    cardsTbody.innerHTML = `<tr><td colspan="8" class="error-cell">Error: ${escapeHtml(error.message)}</td></tr>`;
  } finally {
    refreshCardsBtn && (refreshCardsBtn.disabled = false);
  }
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
    refreshCards();
  } catch (error) {
    confirmBtn.disabled = false;
    errorMessage.textContent = error.message;
    showToast(error.message);
  }
});

refreshCardsBtn?.addEventListener("click", refreshCards);

document.addEventListener("click", async (e) => {
  const btn = e.target?.closest?.("button[data-copy]");
  if (!btn) return;
  const text = btn.getAttribute("data-copy") || "";
  try {
    await navigator.clipboard.writeText(text);
    showToast("Copied JSON");
  } catch {
    showToast("Copy failed");
  }
});

// Load saved cards on first page load.
refreshCards();
