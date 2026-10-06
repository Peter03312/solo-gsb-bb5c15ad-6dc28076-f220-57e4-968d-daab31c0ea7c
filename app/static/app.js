// 通用前端逻辑：API 封装、提示、时间格式化、HTML 转义。

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}（UTC${
      -d.getTimezoneOffset() / 60 >= 0 ? "+" : ""
    }${-d.getTimezoneOffset() / 60} 本地）`;
}

let toastTimer = null;
function showToast(message, kind = "success") {
  const el = document.getElementById("toast");
  if (!el) {
    window.alert(message);
    return;
  }
  el.className = `toast ${kind}`;
  el.textContent = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), kind === "success" ? 2600 : 6000);
}

async function api(url, options = {}) {
  const opts = {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  };
  if (options.query) {
    url += "?" + new URLSearchParams(options.query).toString();
    delete opts.query;
  }
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(url, opts);
  let data = null;
  const text = await resp.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!resp.ok) {
    const message =
      (data && data.error && data.error.message) ||
      `请求失败（HTTP ${resp.status}）`;
    const err = new Error(message);
    err.status = resp.status;
    err.kind = resp.status === 409 ? "conflict" : "error";
    err.data = data;
    throw err;
  }
  return data;
}

function formData(form) {
  const out = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "number") {
      out[el.name] = el.value === "" ? null : Number(el.value);
    } else {
      out[el.name] = el.value;
    }
  }
  return out;
}

// 绑定表单：提交 API，成功后按 data-reload 刷新页面
function bindApiForm(form, { onSuccess, confirm: confirmMsg } = {}) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    const btn = form.querySelector("button[type=submit]");
    if (btn) { btn.disabled = true; btn.dataset.label = btn.textContent; btn.textContent = "处理中…"; }
    try {
      const result = await api(form.action, {
        method: form.method || "POST",
        body: formData(form),
      });
      const msg = (result && result.message) || form.dataset.success || "操作成功";
      if (msg) showToast(msg, "success");
      if (onSuccess) {
        await onSuccess(result);
      } else if (form.dataset.reload !== "false") {
        setTimeout(() => window.location.reload(), 600);
      }
    } catch (err) {
      showToast(err.message, err.kind || "error");
      // 状态冲突后刷新页面，展示最新状态
      if (err.status === 409 && form.dataset.reloadOnConflict !== "false") {
        setTimeout(() => window.location.reload(), 1800);
      }
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.label || "提交"; }
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form.api-form").forEach((form) => {
    bindApiForm(form, {
      confirm: form.dataset.confirm || undefined,
    });
  });
});
