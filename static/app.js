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
const clearBtn = document.getElementById("clearBtn");
const detailsBadge = document.getElementById("detailsBadge");
const sourcePill = document.getElementById("sourcePill");
const steps = Array.from(document.querySelectorAll(".stepper .step"));
const warningsBox = document.getElementById("warnings");

let confirmedOnce = false;
let selectedFile = null;
let lastConfirmedCard = null;
let isScanning = false;
let isSaving = false;
let objectUrl = null;

async function readJsonSafely(res) {
  const contentType = res.headers.get("content-type") || "";
  const isJson = contentType.includes("application/json");

  if (!isJson) {
    const text = await res.text();
    const snippet = text.replace(/\s+/g, " ").slice(0, 180);
    throw new Error(
      `Server returned non-JSON (HTTP ${res.status}). ` +
        `This usually means the app crashed or the route isn't reachable. ` +
        `Response starts with: ${snippet}`
    );
  }

  try {
    return await res.json();
  } catch (err) {
    throw new Error(`Invalid JSON from server (HTTP ${res.status}).`);
  }
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 2200);
}

function setStep(stepNumber) {
  for (const el of steps) {
    const s = Number(el.getAttribute("data-step"));
    el.classList.toggle("active", s === stepNumber);
    el.classList.toggle("done", s < stepNumber);
  }
}

function setBadge(state) {
  if (!detailsBadge) return;
  const map = {
    waiting: { label: "Waiting" },
    scanning: { label: "Scanning..." },
    review: { label: "Review" },
    saved: { label: "Saved" },
  };
  detailsBadge.textContent = map[state]?.label || "Waiting";
}

function setSource(source) {
  if (!sourcePill) return;
  const normalized = source || "waiting";
  sourcePill.textContent = `Source: ${normalized}`;
  sourcePill.classList.toggle("groq", normalized === "groq");
  sourcePill.classList.toggle("heuristic", normalized === "heuristic");
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

function clearReviewMarkers() {
  const fields = ["name", "number", "address", "website", "company_name", "designation"];
  for (const field of fields) {
    const input = form.elements[field];
    const wrapper = input?.closest?.(".field");
    wrapper?.classList?.remove("needs-review");
  }
  if (warningsBox) {
    warningsBox.hidden = true;
    warningsBox.textContent = "";
  }
}

function markNeedsReview(fieldNames = []) {
  for (const name of fieldNames) {
    const input = form.elements[name];
    const wrapper = input?.closest?.(".field");
    wrapper?.classList?.add("needs-review");
  }
}

function showWarnings(warnings = []) {
  if (!warningsBox) return;
  if (!warnings?.length) {
    warningsBox.hidden = true;
    warningsBox.textContent = "";
    return;
  }
  warningsBox.hidden = false;
  warningsBox.textContent = warnings.join(" ");
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
  if (isScanning) return;
  isScanning = true;

  const formData = new FormData();
  formData.append("image", file);

  previewSection.style.display = "block";
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  objectUrl = URL.createObjectURL(file);
  preview.src = objectUrl;
  scanStatus.textContent = "Scanning... this can take a few seconds.";
  errorMessage.textContent = "";
  scanBtn.disabled = true;
  scanBtn.classList.add("loading");
  confirmBtn.disabled = true;
  confirmedOnce = false;
  setStep(2);
  setBadge("scanning");

  try {
    const res = await fetch("/scan", { method: "POST", body: formData });
    const payload = await readJsonSafely(res);
    if (!res.ok) throw new Error(payload.error || `Failed to scan card (HTTP ${res.status}).`);

    clearReviewMarkers();
    setFormData(payload.data || {});
    ocrText.textContent = payload.raw_text || "No text detected.";
    confirmBtn.disabled = false;
    scanStatus.textContent = "Scan complete. Review the fields and tap Save.";
    showToast("Business card scanned successfully");
    setStep(3);
    setBadge("review");

    const meta = payload.meta || {};
    showWarnings(meta.warnings || []);
    markNeedsReview(meta.low_confidence_fields || []);
    setSource(meta.source || "groq");

    // Auto-scroll to the review form on mobile after scan completes.
    setTimeout(() => {
      form?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    }, 50);
  } catch (error) {
    ocrText.textContent = `Error: ${error.message}`;
    scanStatus.textContent = "Scan failed.";
    errorMessage.textContent = error.message;
    showToast(error.message);
    setStep(1);
    setBadge("waiting");
  } finally {
    scanBtn.disabled = !selectedFile;
    scanBtn.classList.remove("loading");
    isScanning = false;
  }
}

function onFileSelected(file) {
  if (!file) return;
  selectedFile = file;
  previewSection.style.display = "block";
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  objectUrl = URL.createObjectURL(file);
  preview.src = objectUrl;
  scanBtn.disabled = false;
  scanStatus.textContent = "Ready. Tap Scan Card to extract details.";
  errorMessage.textContent = "";
  clearBtn && (clearBtn.disabled = false);
  setStep(1);
  setBadge("waiting");
}

uploadInput.addEventListener("change", (e) => onFileSelected(e.target.files[0]));
cameraInput.addEventListener("change", (e) => onFileSelected(e.target.files[0]));
scanBtn.addEventListener("click", () => scanCard(selectedFile));

function clearSelection() {
  selectedFile = null;
  scanBtn.disabled = true;
  confirmBtn.disabled = true;
  confirmedOnce = false;
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  objectUrl = null;
  previewSection.style.display = "none";
  preview.removeAttribute("src");
  scanStatus.textContent = "Choose an image to begin.";
  errorMessage.textContent = "";
  ocrText.textContent = "Scan a business card to see extracted text.";
  setFormData({});
  clearReviewMarkers();
  setSource("waiting");
  clearBtn && (clearBtn.disabled = true);
  setStep(1);
  setBadge("waiting");
}

clearBtn?.addEventListener("click", clearSelection);

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (confirmedOnce) return;
  if (isSaving) return;
  isSaving = true;

  confirmBtn.disabled = true;
  confirmBtn.classList.add("loading");
  const data = getFormData();

  try {
    const res = await fetch("/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data }),
    });
    const payload = await readJsonSafely(res);
    if (!res.ok) throw new Error(payload.error || `Failed to save (HTTP ${res.status}).`);

    confirmedOnce = true;
    scanStatus.textContent = "Saved. You can scan another card.";
    errorMessage.textContent = "";
    showToast("Saved");
    renderLastConfirmedCard(payload.data);
    setStep(4);
    setBadge("saved");
    // Keep preview and form so users can verify, but allow a new scan.
    scanBtn.disabled = !selectedFile;

    // Auto-scroll to the saved summary after saving.
    setTimeout(() => {
      document.getElementById("lastCardFields")?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    }, 50);
  } catch (error) {
    confirmBtn.disabled = false;
    errorMessage.textContent = error.message;
    showToast(error.message);
  }
  confirmBtn.classList.remove("loading");
  isSaving = false;
});

form.addEventListener("input", (e) => {
  const el = e.target;
  if (!el?.name) return;
  const wrapper = el.closest?.(".field");
  wrapper?.classList?.remove("needs-review");
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
setStep(1);
setBadge("waiting");
setSource("waiting");
