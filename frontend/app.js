"use strict";

const app = document.getElementById("app");
let screenGeneration = 0;
let currentUserId = null;
const roleViews = {
  candidate: {
    label: "مرشح", description: "تابع رحلة التقديم الخاصة بك من مكان واحد.",
    hero: "هنا ستتمكن من متابعة طلباتك واستكشاف الفرص المتاحة.",
    features: []
  },
  employee: {
    label: "موظف", description: "مساحة مخصصة للإجابة عن أسئلتك المتعلقة بالعمل.",
    hero: "ستجد هنا طريقًا أوضح للوصول إلى معلومات وسياسات الموارد البشرية.",
    features: []
  },
  hr: {
    label: "الموارد البشرية", description: "تابع عملية التوظيف واتخذ قراراتك بصورة أوضح.",
    hero: "ستتمكن من مراجعة المرشحين ومتابعة مراحل التوظيف من مكان واحد.",
    features: []
  }
};

function showTemplate(id) {
  screenGeneration += 1;
  app.replaceChildren(document.getElementById(id).content.cloneNode(true));
  app.setAttribute("aria-busy", "false");
}

function showLogin(message = "") {
  currentUserId = null;
  showTemplate("login-template");
  const form = document.getElementById("login-form");
  const error = document.getElementById("login-error");
  if (message) { error.textContent = message; error.hidden = false; }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const username = form.elements.username.value.trim();
    const password = form.elements.password.value;
    error.hidden = true;
    if (!username || !password) {
      error.textContent = "من فضلك أدخل اسم المستخدم وكلمة المرور.";
      error.hidden = false;
      return;
    }
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    button.firstElementChild.textContent = "جاري تسجيل الدخول...";
    try {
      const response = await fetch("/api/auth/login", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
      if (response.status === 401) throw new Error("اسم المستخدم أو كلمة المرور غير صحيحة.");
      if (!response.ok) throw new Error("تعذّر تسجيل الدخول الآن. حاول مرة أخرى.");
      const user = await response.json();
      form.elements.password.value = "";
      showWorkspace(user);
    } catch (err) {
      error.textContent = err instanceof TypeError ? "تعذّر الاتصال بالخادم. تأكد من تشغيل التطبيق." : err.message;
      error.hidden = false;
      button.disabled = false;
      button.firstElementChild.textContent = "تسجيل الدخول";
    }
  });
}

function showWorkspace(user) {
  const view = roleViews[user?.role];
  if (!view) { showLogin("تعذّر تحديد صلاحية الحساب. يرجى المحاولة لاحقًا."); return; }
  currentUserId = user.id;
  showTemplate("shell-template");
  document.getElementById("display-name").textContent = user.display_name || user.username;
  document.getElementById("role-badge").textContent = view.label;
  document.getElementById("role-description").textContent = view.description;
  document.getElementById("hero-description").textContent = view.hero;
  if (user.role === "candidate" || user.role === "hr") loadRecruitment(user.role);
  setupKnowledge(user.role);
  setupConversations(user.role);
  if (user.role === "hr") setupKnowledgeDocuments();
  if (user.role === "candidate" || user.role === "hr") loadOperations(user.role);
  const grid = document.getElementById("feature-grid");
  if (!view.features.length) grid.closest("section").hidden = true;
  for (const [icon, title, description] of view.features) {
    const card = document.createElement("article");
    card.className = "feature-card";
    const mark = document.createElement("span"); mark.className = "feature-icon"; mark.setAttribute("aria-hidden", "true"); mark.textContent = icon;
    const heading = document.createElement("h3"); heading.textContent = title;
    const copy = document.createElement("p"); copy.textContent = description;
    const status = document.createElement("span"); status.className = "status-label"; status.textContent = "قيد التطوير";
    card.append(mark, heading, copy, status);
    grid.append(card);
  }
  setupLogout();
  setupAdminAccess(user);
}

function setupLogout() {
  document.getElementById("logout").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const response = await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
      if (!response.ok) throw new Error("logout failed");
      showLogin();
    } catch {
      button.disabled = false;
      let notice = document.getElementById("logout-error");
      if (!notice) { notice = document.createElement("p"); notice.id = "logout-error"; notice.className = "form-error"; notice.setAttribute("role", "alert"); button.parentElement.after(notice); }
      notice.textContent = "تعذّر تسجيل الخروج. تحقق من الاتصال وحاول مرة أخرى.";
    }
  });
}

const statusLabels = { submitted: "تم التقديم", advanced: "المرحلة التالية", offered: "عرض توظيف", rejected: "مرفوض", hired: "تم التوظيف" };
const documentErrors = {
  graph_matching_work_limit: "Graph matching supports resumes up to 4,000 characters and at most 20 job skills of 80 characters each. Review larger inputs manually.",
  unsupported_type: "صيغة الملف غير مدعومة. ارفع PDF نصيًا أو DOCX.",
  empty_file: "الملف فارغ.", file_too_large: "حجم الملف أكبر من 5 ميجابايت.",
  document_too_large: "المستند يتجاوز الحد المسموح للمعالجة.",
  protected_document: "الملفات المحمية بكلمة مرور غير مدعومة.",
  invalid_document: "تعذّرت قراءة الملف. تأكد من أنه غير تالف.",
  no_text: "لم نجد نصًا في الملف؛ الملفات المصوّرة تحتاج OCR غير متاح حاليًا."
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, options = {}, { allowNotFound = false, errorMessages = {} } = {}) {
  const generation = screenGeneration;
  const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options });
  if (generation !== screenGeneration) throw new Error("stale-request");
  if (response.status === 401) { showLogin("انتهت الجلسة. سجل الدخول مرة أخرى."); throw new Error("session-expired"); }
  if (allowNotFound && response.status === 404) return null;
  if (!response.ok) {
    let body = null;
    try { body = await response.json(); } catch { /* Non-JSON error response. */ }
    if (generation !== screenGeneration) throw new Error("stale-request");
    const code = body?.detail?.code;
    const message = errorMessages[response.status] || documentErrors[code] || (response.status === 413 ? "حجم الملف أكبر من 5 ميجابايت." : response.status === 403 ? "ليس لديك صلاحية لهذا الإجراء." : response.status === 409 ? "تعذّر تنفيذ الإجراء بسبب حالة الطلب أو تعارض الموعد. حدّث البيانات وتحقق منها." : response.status === 422 ? "تحقق من صحة البيانات المدخلة." : "تعذّر إتمام الطلب. حاول مرة أخرى.");
    throw new Error(message);
  }
  const result = response.status === 204 ? null : await response.json();
  if (generation !== screenGeneration) throw new Error("stale-request");
  return result;
}

function isStale(error) { return error.message === "session-expired" || error.message === "stale-request"; }

function announce(message, isError = false) {
  const node = document.getElementById("recruitment-message");
  if (!node) return;
  node.textContent = message;
  node.classList.toggle("error", isError);
  node.hidden = !message;
}

function section(title) {
  const box = element("section", "data-section");
  box.append(element("h3", "", title));
  return box;
}

async function setupAdminAccess(user) {
  try {
    const access = await api("/api/admin/access");
    if (!access.is_admin) return;
    const nav = document.querySelector("nav[aria-label='التنقل الرئيسي']");
    if (!nav) return;
    const link = element("a", "nav-item", "إدارة الحسابات المحلية"); link.href = "#admin";
    link.addEventListener("click", (event) => { event.preventDefault(); showAdminWorkspace(user); });
    nav.append(link);
  } catch (error) {
    if (!isStale(error)) {
      const sidebar = document.querySelector(".sidebar");
      if (sidebar) sidebar.append(element("p", "subtle-note", "تعذّر التحقق من صلاحية إدارة الحسابات. أعد تحميل الصفحة للمحاولة مجددًا."));
    }
  }
}

function showAdminWorkspace(user) {
  showTemplate("shell-template");
  document.getElementById("role-badge").textContent = "إدارة الحسابات";
  document.querySelector(".header-path").textContent = "إدارة الحسابات المحلية";
  const nav = document.querySelector("nav[aria-label='التنقل الرئيسي']");
  const back = element("a", "nav-item", "العودة لمساحة العمل"); back.href = "#overview";
  back.addEventListener("click", (event) => { event.preventDefault(); showWorkspace(user); });
  nav.replaceChildren(back); setupLogout();
  const content = document.querySelector(".workspace-content");
  const sectionNode = element("section", "knowledge-section"); sectionNode.setAttribute("aria-label", "إدارة الحسابات المحلية");
  const heading = element("div", "section-heading"); heading.append(element("h2", "", "إدارة الحسابات المحلية"));
  const panel = element("div", "knowledge-panel");
  panel.append(element("p", "subtle-note", "صلاحية إدارة الحسابات مستقلة عن دور الموارد البشرية. تغيير الدور أو الحالة أو الصلاحية، أو إعادة ضبط كلمة المرور، ينهي جلسات الحساب المتأثر."));
  const message = element("p", "inline-message"); message.setAttribute("role", "status"); message.hidden = true;
  const create = element("form", "decision-form"); create.setAttribute("aria-label", "إنشاء حساب محلي");
  create.append(element("h3", "", "إنشاء حساب"));
  function textField(labelText, type, maxLength, autocomplete) {
    const label = element("label", "", labelText), input = element("input");
    input.type = type; input.required = true; input.maxLength = maxLength;
    if (autocomplete) input.autocomplete = autocomplete;
    label.append(input); return { label, input };
  }
  function roleSelect(value) {
    const select = element("select");
    select.setAttribute("aria-label", "الدور");
    for (const [key, label] of [["candidate", "مرشح"], ["employee", "موظف"], ["hr", "موارد بشرية"]]) {
      const option = element("option", "", label); option.value = key; select.append(option);
    }
    if (value) select.value = value;
    return select;
  }
  const username = textField("اسم المستخدم (حروف لاتينية وأرقام و _ . -)", "text", 128, "off");
  username.input.pattern = "[A-Za-z0-9_.-]+";
  const display = textField("الاسم الظاهر", "text", 128, "off");
  const password = textField("كلمة المرور الأولية (12 حرفًا على الأقل)", "password", 256, "new-password"); password.input.minLength = 12;
  const roleLabel = element("label", "", "الدور"); const role = roleSelect("candidate"); roleLabel.append(role);
  const adminLabel = element("label", "admin-checkbox"); const admin = element("input"); admin.type = "checkbox";
  adminLabel.append(admin, document.createTextNode(" منح صلاحية إدارة الحسابات"));
  const createButton = element("button", "small-button", "إنشاء الحساب"); createButton.type = "submit";
  create.append(username.label, display.label, password.label, roleLabel, adminLabel, createButton);
  const list = element("div", "data-list"); list.setAttribute("aria-live", "polite");
  const auditButton = element("button", "outline-button", "عرض سجل إدارة الحسابات"); auditButton.type = "button";
  const audit = element("div"); audit.setAttribute("aria-live", "polite");
  panel.append(message, create, element("h3", "", "الحسابات"), list, auditButton, audit);
  sectionNode.append(heading, panel); content.replaceChildren(sectionNode);
  function report(text, isError = false) { message.textContent = text; message.classList.toggle("error", isError); message.hidden = !text; }
  const validationError = "تحقق من اسم المستخدم والاسم والدور وكلمة المرور.";
  async function refreshUsers() {
    const users = await api("/api/admin/users");
    if (!sectionNode.isConnected) return;
    list.replaceChildren();
    if (!users.length) list.append(element("p", "empty-note", "لا توجد حسابات."));
    for (const account of users) {
      const card = element("article", "job-card");
      card.append(element("h4", "", `${account.display_name} (${account.username}) · #${account.id}`));
      const form = element("form", "decision-form");
      const label = element("label", "", "الدور"); const accountRole = roleSelect(account.role); label.append(accountRole);
      const activeLabel = element("label", "admin-checkbox"); const active = element("input"); active.type = "checkbox"; active.checked = account.active;
      activeLabel.append(active, document.createTextNode(" الحساب نشط"));
      const capabilityLabel = element("label", "admin-checkbox"); const capability = element("input"); capability.type = "checkbox"; capability.checked = account.is_admin;
      capabilityLabel.append(capability, document.createTextNode(" لديه صلاحية إدارة الحسابات"));
      const save = element("button", "outline-button", "حفظ التغييرات"); save.type = "submit";
      form.append(label, activeLabel, capabilityLabel, save);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const changes = {};
        if (accountRole.value !== account.role) changes.role = accountRole.value;
        if (active.checked !== account.active) changes.active = active.checked;
        if (capability.checked !== account.is_admin) changes.is_admin = capability.checked;
        if (!Object.keys(changes).length) { report("لم تتغير إعدادات الحساب."); return; }
        if (!window.confirm(`حفظ تغييرات الحساب ${account.username}؟ ستنتهي جلساته الحالية.`)) return;
        save.disabled = true; accountRole.disabled = true; active.disabled = true; capability.disabled = true;
        try {
          await api(`/api/admin/users/${encodeURIComponent(account.id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes) }, { errorMessages: { 409: "لا يمكن تعطيل أو إزالة صلاحية آخر مشرف نشط.", 422: validationError } });
          if (account.id === user.id) { showLogin("تم تحديث حسابك وإنهاء جلساتك. سجل الدخول مرة أخرى."); return; }
          if (!sectionNode.isConnected) return;
          report("تم تحديث الحساب وإنهاء جلساته.");
          try { await refreshUsers(); } catch (error) { if (!isStale(error) && sectionNode.isConnected) report("تم تحديث الحساب، لكن تعذّر تحديث القائمة. أعد تحميل الصفحة.", true); }
        } catch (error) { if (!isStale(error) && sectionNode.isConnected) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
        finally { save.disabled = false; accountRole.disabled = false; active.disabled = false; capability.disabled = false; }
      });
      const reset = element("form", "decision-form"); reset.setAttribute("aria-label", `إعادة ضبط كلمة مرور ${account.username}`);
      const newPassword = textField("كلمة المرور الجديدة (12 حرفًا على الأقل)", "password", 256, "new-password"); newPassword.input.minLength = 12;
      const resetButton = element("button", "outline-button", "إعادة ضبط كلمة المرور"); resetButton.type = "submit";
      reset.append(newPassword.label, resetButton);
      reset.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (Array.from(newPassword.input.value).length < 12) { report("كلمة المرور يجب أن تحتوي 12 حرفًا على الأقل.", true); return; }
        if (!window.confirm(`إعادة ضبط كلمة مرور ${account.username}؟ ستنتهي جلساته الحالية.`)) return;
        resetButton.disabled = true;
        try {
          await api(`/api/admin/users/${encodeURIComponent(account.id)}/password`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: newPassword.input.value }) }, { errorMessages: { 422: validationError } });
          if (account.id === user.id) { showLogin("تم تغيير كلمة مرورك وإنهاء جلساتك. سجل الدخول مجددًا."); return; }
          if (sectionNode.isConnected) report("تم تغيير كلمة المرور وإنهاء جلسات الحساب.");
        } catch (error) { if (!isStale(error) && sectionNode.isConnected) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
        finally { newPassword.input.value = ""; resetButton.disabled = false; }
      });
      card.append(form, reset); list.append(card);
    }
  }
  create.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (Array.from(password.input.value).length < 12) { report("كلمة المرور يجب أن تحتوي 12 حرفًا على الأقل.", true); return; }
    createButton.disabled = true;
    try {
      await api("/api/admin/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: username.input.value.trim(), display_name: display.input.value.trim(), password: password.input.value, role: role.value, is_admin: admin.checked }) }, { errorMessages: { 409: "تعذّر إنشاء الحساب؛ قد يكون اسم المستخدم مستخدمًا بالفعل.", 422: validationError } });
      if (!sectionNode.isConnected) return;
      create.reset(); report("تم إنشاء الحساب المحلي.");
      try { await refreshUsers(); } catch (error) { if (!isStale(error) && sectionNode.isConnected) report("تم إنشاء الحساب، لكن تعذّر تحديث القائمة. أعد تحميل الصفحة.", true); }
    } catch (error) { if (!isStale(error) && sectionNode.isConnected) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
    finally { password.input.value = ""; createButton.disabled = false; }
  });
  auditButton.addEventListener("click", async () => {
    auditButton.disabled = true;
    try {
      const events = await api("/api/admin/audit");
      if (!sectionNode.isConnected) return;
      audit.replaceChildren(element("h3", "", "آخر إجراءات إدارة الحسابات"));
      if (!events.length) audit.append(element("p", "subtle-note", "لا توجد إجراءات مسجلة."));
      else {
        const entries = element("ul", "evidence-list");
        const actions = { bootstrap: "إنشاء المشرف الأول", created: "إنشاء حساب", updated: "تحديث حساب", password_reset: "إعادة ضبط كلمة مرور" };
        for (const event of events) entries.append(element("li", "", `${displayDate(event.created_at)} · ${actions[event.action] || event.action} · الحساب #${event.target_id} · المنفذ #${event.actor_id ?? "—"}`));
        audit.append(entries);
      }
    } catch (error) { if (!isStale(error) && sectionNode.isConnected) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
    finally { auditButton.disabled = false; }
  });
  refreshUsers().catch((error) => { if (!isStale(error) && sectionNode.isConnected) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم لتحميل الحسابات." : error.message, true); });
}

async function loadRecruitment(role) {
  const area = document.getElementById("recruitment");
  area.hidden = false;
  announce("جاري تحميل الوظائف والطلبات...");
  try {
    const [jobs, applications] = await Promise.all([api("/api/jobs"), api("/api/applications")]);
    if (!document.getElementById("recruitment")) return false;
    if (!Array.isArray(jobs) || !Array.isArray(applications)) throw new Error("تعذّر قراءة بيانات الخادم.");
    announce("");
    renderJobs(jobs, role);
    renderApplications(applications, jobs, role);
    return true;
  } catch (error) {
    if (!isStale(error)) announce(error instanceof TypeError ? "تعذّر الاتصال بالخادم. حاول إعادة تحميل الصفحة." : error.message, true);
    return false;
  }
}

function renderJobs(jobs, role) {
  const target = document.getElementById("jobs-area");
  const box = section("الوظائف المتاحة");
  const list = element("div", "data-list");
  if (!jobs.length) list.append(element("p", "empty-note", "لا توجد وظائف متاحة حاليًا."));
  for (const job of jobs) {
    const card = element("article", "job-card");
    card.append(element("h4", "", job.title), element("p", "", job.location), element("p", "skill-line", `المهارات المطلوبة: ${(job.required_skills || []).join("، ")}`));
    if (role === "candidate") {
      const form = element("form", "upload-form");
      const label = element("label", "", "سيرة ذاتية بصيغة PDF نصي أو DOCX (حتى 5 ميجابايت)");
      const input = element("input"); input.type = "file"; input.accept = ".pdf,.docx"; input.required = true;
      label.append(input);
      const ocrLabel = element("label", "", "Optional OCR language for scanned PDF pages. OCR may miss or alter text; review it with HR. English and Arabic language data must be installed for the bilingual option.");
      const ocr = element("select");
      for (const [value, labelText] of [["", "No OCR"], ["eng", "English"], ["eng+ara", "English and Arabic"]]) { const option = element("option", "", labelText); option.value = value; ocr.append(option); }
      ocrLabel.append(ocr);
      const button = element("button", "small-button", "التقديم لهذه الوظيفة"); button.type = "submit";
      form.append(label, ocrLabel, button);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const file = input.files?.[0];
        if (!file) { announce("اختر ملف السيرة الذاتية أولًا.", true); return; }
        if (file.size > 5 * 1024 * 1024) { announce("حجم الملف أكبر من 5 ميجابايت.", true); return; }
        button.disabled = true;
        try {
          const params = new URLSearchParams({ job_id: job.id, filename: file.name });
          if (ocr.value) { params.set("ocr", "true"); params.set("ocr_language", ocr.value); }
          const uploaded = await api(`/api/applications?${params}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
          if (await loadRecruitment(role)) announce("تم إرسال الطلب بنجاح.");
          if (uploaded.extraction?.ocr_pages?.length) announce(`Application submitted. OCR was used on pages ${uploaded.extraction.ocr_pages.join(", ")}; review the extracted text because OCR can make mistakes.`);
        } catch (error) { if (!isStale(error)) announce(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
        finally { button.disabled = false; }
      });
      card.append(form);
    }
    if (role === "hr") {
      const compare = element("button", "outline-button", "مقارنة مراجعات المتقدمين"); compare.type = "button";
      const comparison = element("div", "application-detail"); comparison.hidden = true;
      compare.addEventListener("click", async () => {
        compare.disabled = true; comparison.hidden = false;
        comparison.replaceChildren(element("p", "subtle-note", "جارٍ تحميل المقارنة..."));
        try {
          const data = await api(`/api/jobs/${encodeURIComponent(job.id)}/rubric/comparison`);
          if (comparison.isConnected) renderComparison(comparison, data);
        } catch (error) {
          if (!isStale(error) && comparison.isConnected) comparison.replaceChildren(element("p", "form-error", error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message));
        } finally { compare.disabled = false; }
      });
      card.append(compare, comparison);
      const triage = element("button", "outline-button", "أولوية مراجعة الطلبات"); triage.type = "button";
      const triagePanel = element("div", "application-detail"); triagePanel.hidden = true;
      triage.addEventListener("click", async () => {
        triage.disabled = true; triagePanel.hidden = false;
        try { await loadRubricTriage(triagePanel, job.id); }
        finally { triage.disabled = false; }
      });
      card.append(triage, triagePanel);
      const provisional = element("button", "outline-button", "Provisional upload screening"); provisional.type = "button";
      const provisionalPanel = element("div", "application-detail"); provisionalPanel.hidden = true;
      provisional.addEventListener("click", async () => {
        provisional.disabled = true; provisionalPanel.hidden = false;
        try { await loadProvisionalQueue(provisionalPanel, job.id); }
        finally { provisional.disabled = false; }
      });
      card.append(provisional, provisionalPanel);
    }
    list.append(card);
  }
  box.append(list);
  if (role === "hr") box.append(createJobForm(role));
  target.replaceChildren(box);
}

async function loadProvisionalQueue(target, jobId, after = null) {
  const base = `/api/jobs/${encodeURIComponent(jobId)}`;
  target.replaceChildren(element("p", "subtle-note", "Loading provisional screenings..."));
  try {
    const data = await api(`${base}/provisional-queue${after === null ? "" : `?after=${encodeURIComponent(after)}`}`);
    if (!target.isConnected) return;
    if (!Array.isArray(data.items) || !Number.isSafeInteger(data.version)) throw new Error("Invalid queue response");
    target.replaceChildren(element("h4", "", "Provisional upload screening"), element("p", "subtle-note", data.notice));
    target.append(element("p", "subtle-note", data.worker?.recent_heartbeat ? "Local automatic worker: recent heartbeat." : "No recent automatic worker heartbeat; pending cases need manual review or an enabled worker."));
    target.append(element("p", "subtle-note", "Pending means no provisional score exists; it is not a negative result. Refresh computes a local score only for resumes up to 4,000 characters and jobs with at most 20 required and preferred skills combined, each up to 80 characters. Larger inputs require manual review. Review the resume mask before relying on a result. Automatic masking may leave identifiers. After a resume, mask, job or assignment changes, or if the worker shows superseded or failed, use Refresh or review manually; no new automatic score is scheduled."));
    const thresholdLabel = element("label", "", "Keyword mention threshold (0–100)");
    const threshold = element("input"); threshold.type = "number"; threshold.min = "0"; threshold.max = "100"; threshold.step = "1";
    threshold.value = data.threshold === null ? "" : String(data.threshold); thresholdLabel.append(threshold);
    const save = element("button", "small-button", "Save threshold"); save.type = "button";
    const message = element("p", "subtle-note"); message.setAttribute("aria-live", "polite");
    save.addEventListener("click", async () => {
      const value = Number(threshold.value);
      if (!threshold.value.trim() || !Number.isInteger(value) || value < 0 || value > 100) { message.textContent = "Enter a whole number from 0 to 100."; return; }
      save.disabled = true;
      try {
        await api(`${base}/provisional-threshold`, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({threshold: value, expected_version: data.version})});
        if (target.isConnected) await loadProvisionalQueue(target, jobId);
      } catch (error) { if (!isStale(error) && target.isConnected) { message.textContent = "Could not save. Reload the queue before retrying."; save.disabled = false; } }
    });
    const reload = element("button", "outline-button", "Reload queue"); reload.type = "button";
    reload.addEventListener("click", () => loadProvisionalQueue(target, jobId));
    target.append(thresholdLabel, save, reload, message);
    const labels = {finalized: "Finalized", pending: "Pending: refresh or review manually", not_screened: "No provisional screening", stale: "Stale: refresh after review", mask_review_required: "Mask review required", no_required_skills: "No required skills to compare", threshold_not_configured: "Threshold not configured", priority_review: "Priority review", needs_review: "Needs review"};
    if (!data.items.length) target.append(element("p", "subtle-note", "No applications for this job."));
    for (const item of data.items) {
      const row = element("section", "application-detail");
      row.append(element("h5", "", `Application #${item.application_id}`),
        element("p", "", `${labels[item.recommendation] || "Review required"} · application ${item.application_status}`),
        element("p", "subtle-note", `Source review: ${item.source_review_status}. Automatic masking ${item.automatic_masked === null ? "unavailable" : item.automatic_masked ? "applied" : "did not identify text to mask"}.`));
      if (item.automatic_state) row.append(element("p", "subtle-note", `Automatic worker: ${item.automatic_state}.`));
      if (item.current && item.score !== null) row.append(element("p", "result-badge", `Provisional required-skill mentions: ${item.score} / 100 (${item.origin === "system_worker" ? "system worker" : "HR refresh"})`));
      if (item.recommendation !== "finalized") {
        const refresh = element("button", "outline-button", "Refresh provisional result"); refresh.type = "button";
        refresh.addEventListener("click", async () => {
          refresh.disabled = true;
          try {
            await api(`/api/applications/${encodeURIComponent(item.application_id)}/provisional-screening`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({expected_snapshot_id: item.snapshot_id})});
            if (target.isConnected) await loadProvisionalQueue(target, jobId, after);
          } catch (error) { if (!isStale(error) && target.isConnected) { message.textContent = "Refresh failed. The source or job may exceed the work limit or need mask review; review manually, or reload to retry."; refresh.disabled = false; } }
        });
        row.append(refresh);
      }
      target.append(row);
    }
    if (data.next_after !== null) {
      const next = element("button", "outline-button", "Next page"); next.type = "button";
      next.addEventListener("click", () => loadProvisionalQueue(target, jobId, data.next_after)); target.append(next);
    }
  } catch (error) {
    if (!isStale(error) && target.isConnected) target.replaceChildren(element("p", "form-error", "Could not load provisional screenings."));
  }
}

async function loadRubricTriage(target, jobId) {
  const base = `/api/jobs/${encodeURIComponent(jobId)}/rubric`;
  target.replaceChildren(element("p", "subtle-note", "جارٍ تحميل توصيات المراجعة..."));
  try {
    const data = await api(`${base}/triage`);
    if (!target.isConnected) return;
    if (!data || !Array.isArray(data.items) || !Number.isSafeInteger(data.version)) throw new Error("تعذّر قراءة توصيات المراجعة.");
    const heading = element("h4", "", "أولوية مراجعة الطلبات");
    const notice = element("p", "subtle-note", "توصيات مبنية على نطاق تقييم HR الحالي وعتبة هذه الوظيفة. لا تتخذ قرار توظيف أو رفض، والطلبات غير المؤكدة تبقى للمراجعة.");
    const message = element("p", "subtle-note"); message.setAttribute("aria-live", "polite");
    const form = element("form", "decision-form");
    const label = element("label", "", "عتبة أولوية المراجعة من 0 إلى 100");
    const threshold = element("input"); threshold.type = "number"; threshold.min = "0"; threshold.max = "100"; threshold.step = "1"; threshold.required = true;
    if (data.threshold !== null && data.threshold !== undefined) threshold.value = String(data.threshold);
    label.append(threshold);
    const save = element("button", "small-button", "حفظ العتبة"); save.type = "submit";
    const reload = element("button", "outline-button", "تحديث التوصيات"); reload.type = "button";
    reload.addEventListener("click", () => loadRubricTriage(target, jobId));
    form.append(label, save);
    const listing = element("div", "data-list");
    const recommendationLabels = {
      threshold_not_configured: "لم تُحدد عتبة للوظيفة",
      unknown: "التقييم غير موجود أو قديم؛ يحتاج مراجعة",
      priority_review: "أولوية للمراجعة البشرية",
      needs_review: "يحتاج مراجعة بشرية",
      inconclusive: "النطاق يتداخل مع العتبة؛ النتيجة غير محسومة",
      finalized: "طلب منتهٍ؛ خارج قائمة الأولوية"
    };
    const priorityOrder = { priority_review: 0, inconclusive: 1, unknown: 2, threshold_not_configured: 3, needs_review: 4, finalized: 5 };
    const items = [...data.items].sort((left, right) => (priorityOrder[left.recommendation] ?? 5) - (priorityOrder[right.recommendation] ?? 5));
    for (const item of items) {
      const card = element("article", "rubric-comparison-item");
      card.append(element("h5", "", `طلب #${item.application_id}`),
        element("p", "subtle-note", `حالة الطلب: ${statusLabels[item.application_status] || "غير متاحة"}`),
        element("p", "", recommendationLabels[item.recommendation] || "التوصية غير متاحة؛ راجع الطلب."));
      if (item.assessment_status === "outdated") card.append(element("p", "form-error", "التقييم قديم ويحتاج مراجعة جديدة."));
      else if (item.assessment_status === "not_assessed") card.append(element("p", "subtle-note", "لم يُحفظ تقييم لهذا الطلب."));
      if (item.bounds) card.append(element("p", "subtle-note", `نطاق التقييم: ${item.bounds.minimum}–${item.bounds.maximum} من 100 · التغطية: ${item.bounds.coverage_percent}%`));
      listing.append(card);
    }
    if (!data.items.length) listing.append(element("p", "subtle-note", "لا توجد طلبات لهذه الوظيفة."));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const value = Number(threshold.value);
      if (threshold.value === "" || !Number.isInteger(value) || value < 0 || value > 100) { message.textContent = "أدخل عددًا صحيحًا من 0 إلى 100."; message.className = "form-error"; return; }
      save.disabled = true; threshold.disabled = true; reload.disabled = true;
      let uncertain = false;
      try {
        await api(`${base}/triage-threshold`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ threshold: value, expected_version: data.version }) });
        if (target.isConnected) await loadRubricTriage(target, jobId);
      } catch (error) {
        uncertain = error instanceof TypeError;
        if (!isStale(error) && target.isConnected) { message.className = "form-error"; message.textContent = uncertain ? "تعذّر الاتصال. حدّث التوصيات للتحقق قبل المحاولة." : error.message; }
      } finally { save.disabled = uncertain; threshold.disabled = uncertain; reload.disabled = false; }
    });
    target.replaceChildren(heading, notice, message, form, reload, listing);
  } catch (error) {
    if (!isStale(error) && target.isConnected) target.replaceChildren(element("p", "form-error", error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message));
  }
}

function renderComparison(target, data) {
  target.replaceChildren(element("h4", "", "مقارنة تقييمات الوظيفة"),
    element("p", "subtle-note", "المقارنة بين نطاقات التقييمات البشرية الحالية فقط. تداخل النطاقات أو تلامسها لا يحسم الفرق؛ النتائج وأسئلة الاستيضاح لا تتخذ قرار توظيف."));
  if (!data.applications?.length) { target.append(element("p", "empty-note", "لا توجد طلبات لهذه الوظيفة.")); return; }
  for (const item of data.applications) {
    const card = element("article", "rubric-comparison-item");
    card.append(element("h4", "", `طلب #${item.application_id}`));
    if (item.status === "not_assessed") card.append(element("p", "subtle-note", "لم يُحفظ تقييم لهذا الطلب؛ غير داخل في المقارنة."));
    else if (item.status === "outdated") card.append(element("p", "form-error", "التقييم يحتاج تحديثًا؛ غير داخل في المقارنة."));
    else if (item.bounds) card.append(element("p", "result-badge", `النطاق: ${item.bounds.minimum}–${item.bounds.maximum} من 100 · تغطية المراجعة: ${item.bounds.coverage_percent}%`));
    if (item.questions?.length) {
      card.append(element("p", "subtle-note", "أسئلة مقترحة للاستيضاح، تحتاج مراجعة بشرية:"));
      const list = element("ul", "evidence-list");
      for (const question of item.questions) list.append(element("li", "", question.question_ar || question.question));
      card.append(list);
    }
    target.append(card);
  }
  const current = data.applications.filter((item) => item.status === "current" && item.bounds);
  if (current.length < 2) { target.append(element("p", "subtle-note", "تحتاج المقارنة إلى تقييمين حاليين على الأقل.")); return; }
  const form = element("div", "decision-form");
  const firstLabel = element("label", "", "الطلب الأول"); const first = element("select"); firstLabel.append(first);
  const secondLabel = element("label", "", "الطلب الثاني"); const second = element("select"); secondLabel.append(second);
  for (const item of current) {
    for (const select of [first, second]) { const option = element("option", "", `طلب #${item.application_id}`); option.value = String(item.application_id); select.append(option); }
  }
  second.selectedIndex = 1;
  const result = element("p", "subtle-note"); result.setAttribute("aria-live", "polite");
  function updatePair() {
    if (first.value === second.value) { result.textContent = "اختر طلبين مختلفين."; return; }
    const left = Number(first.value), right = Number(second.value);
    const pair = (data.comparisons || []).find((entry) => (entry.left_application_id === left && entry.right_application_id === right) || (entry.left_application_id === right && entry.right_application_id === left));
    if (!pair || !["left_higher", "right_higher"].includes(pair.relation)) { result.textContent = "الفرق غير محسوم من نطاقات التقييم الحالية."; return; }
    const higher = pair.relation === "left_higher" ? pair.left_application_id : pair.right_application_id;
    result.textContent = `نطاق طلب #${higher} أعلى بالكامل من نطاق الطلب الآخر. هذا لا يمثل قرار توظيف.`;
  }
  first.addEventListener("change", updatePair); second.addEventListener("change", updatePair); updatePair();
  form.append(firstLabel, secondLabel, result); target.append(form);
}

function createJobForm(role) {
  const form = element("form", "decision-form");
  form.setAttribute("aria-label", "إضافة وظيفة");
  form.append(element("h4", "", "إضافة وظيفة"));
  form.append(element("p", "subtle-note", "اكتب كل مهارة أو مسؤولية في سطر مستقل. قد تُرسل مهارات الوظيفة إلى Groq عند تشغيل مساعد المراجعة؛ استخدم بيانات تجريبية فقط في هذه النسخة."));

  function field(name, labelText, multiline = false, required = false, maximum = 2000) {
    const label = element("label", "", labelText);
    const input = element(multiline ? "textarea" : "input");
    input.name = name; input.required = required; input.maxLength = maximum;
    if (!multiline) input.type = "text";
    label.append(input); form.append(label);
    return input;
  }
  const title = field("title", "المسمى الوظيفي", false, true, 200);
  const location = field("location", "مكان العمل", false, true, 200);
  const requiredSkills = field("required_skills", "المهارات المطلوبة", true, true);
  const preferredSkills = field("preferred_skills", "المهارات المفضلة", true);
  const responsibilities = field("responsibilities", "المسؤوليات", true, true);
  const submit = element("button", "small-button", "حفظ الوظيفة"); submit.type = "submit";
  form.append(submit);

  const parseList = (value) => [...new Set(value.split(/\r?\n/u).map((item) => item.trim()).filter(Boolean))];
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const skills = parseList(requiredSkills.value);
    const duties = parseList(responsibilities.value);
    if (!title.value.trim() || !location.value.trim() || !skills.length || !duties.length) {
      announce("أدخل المسمى ومكان العمل ومهارة مطلوبة ومسؤولية واحدة على الأقل.", true); return;
    }
    if (!globalThis.crypto?.randomUUID) { announce("تعذّر إنشاء معرّف آمن للوظيفة في هذا المتصفح.", true); return; }
    submit.disabled = true;
    try {
      await api("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        id: `job-${globalThis.crypto.randomUUID()}`, title: title.value.trim(), location: location.value.trim(),
        required_skills: skills, preferred_skills: parseList(preferredSkills.value), responsibilities: duties,
      }) });
      if (await loadRecruitment(role)) announce("تم حفظ الوظيفة.");
      else announce("حُفظت الوظيفة، لكن تعذّر تحديث القائمة. أعد تحميل الصفحة.", true);
    } catch (error) { if (!isStale(error)) announce(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
    finally { submit.disabled = false; }
  });
  return form;
}

function renderApplications(applications, jobs, role) {
  const target = document.getElementById("application-area");
  const box = section(role === "hr" ? "طلبات المرشحين" : "طلباتي");
  const list = element("div", "data-list");
  if (!applications.length) list.append(element("p", "empty-note", "لا توجد طلبات حتى الآن."));
  const jobTitles = new Map(jobs.map((job) => [job.id, job.title]));
  for (const application of applications) {
    const container = element("div");
    const card = element("article", "application-card");
    const info = element("div");
    info.append(element("h4", "", `${jobTitles.get(application.job_id) || application.job_id} · طلب #${application.id}`));
    info.append(element("p", "", `الحالة: ${statusLabels[application.status] || application.status}`));
    if (role === "hr") info.append(element("p", "", `رقم المرشح: ${application.candidate_id}`));
    const button = element("button", "outline-button", "عرض التفاصيل"); button.type = "button";
    const detail = element("div");
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const data = await api(`/api/applications/${encodeURIComponent(application.id)}`);
        renderApplicationDetail(detail, data, role);
      } catch (error) { if (!isStale(error)) announce(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
      finally { button.disabled = false; }
    });
    card.append(info, button); container.append(card, detail); list.append(container);
  }
  if (role === "hr") {
    const inbox = element("div", "application-detail");
    const open = element("button", "outline-button", "Open submission notifications"); open.type = "button";
    open.addEventListener("click", () => loadHrNotifications(inbox));
    box.append(open, inbox);
  }
  box.append(list); target.replaceChildren(box);
}

async function loadHrNotifications(target, before = null) {
  target.replaceChildren(element("p", "subtle-note", "Loading local notifications..."));
  try {
    const data = await api(`/api/hr/notifications${before === null ? "" : `?before=${encodeURIComponent(before)}`}`);
    if (!target.isConnected) return;
    target.replaceChildren(element("h4", "", "Submission notifications"), element("p", "subtle-note", data.notice));
    if (!data.items.length) target.append(element("p", "subtle-note", "No notifications on this page."));
    for (const item of data.items) {
      const row = element("section", "application-detail");
      row.append(element("p", "", `Application #${item.application_id} · submitted job ${item.submitted_job_id} · current job ${item.current_job_id} · status ${item.current_status} · ${item.is_read ? "read" : "unread"}`));
      if (!item.is_read) {
        const read = element("button", "small-button", "Mark read"); read.type = "button";
        read.addEventListener("click", async () => {
          read.disabled = true;
          try { await api(`/api/hr/notifications/${encodeURIComponent(item.id)}/read`, {method: "POST"}); if (target.isConnected) await loadHrNotifications(target, before); }
          catch (error) { if (!isStale(error) && target.isConnected) { row.append(element("p", "form-error", "Could not acknowledge. Reload to check before trying again.")); read.disabled = false; } }
        });
        row.append(read);
      }
      target.append(row);
    }
    if (data.next_before !== null) { const next = element("button", "outline-button", "Older notifications"); next.type = "button"; next.addEventListener("click", () => loadHrNotifications(target, data.next_before)); target.append(next); }
    if (before !== null) { const first = element("button", "outline-button", "Newest notifications"); first.type = "button"; first.addEventListener("click", () => loadHrNotifications(target)); target.append(first); }
  } catch (error) { if (!isStale(error) && target.isConnected) target.replaceChildren(element("p", "form-error", "Could not load notifications.")); }
}

const rubricCategories = {
  mandatory: "المتطلبات الأساسية (40)", preferred: "المتطلبات المفضلة (15)",
  experience: "الخبرة (20)", projects: "المشاريع (15)", education: "التعليم (10)"
};

async function loadRubric(target, application) {
  target.replaceChildren(element("h4", "", "التقييم الموزون بالمراجعة البشرية"), element("p", "subtle-note", "جارٍ تحميل التقييم..."));
  const jobPath = `/api/jobs/${encodeURIComponent(application.job_id)}/rubric/criteria`;
  const base = `/api/applications/${encodeURIComponent(application.id)}/rubric`;
  try {
    const [criteria, source, evidence, assessment] = await Promise.all([
      api(jobPath), api(`${base}/source`), api(`${base}/evidence`),
      api(`${base}/assessment`, {}, { allowNotFound: true })
    ]);
    if (!target.isConnected) return;
    renderRubric(target, jobPath, base, criteria, source, evidence, assessment);
  } catch (error) {
    if (!isStale(error) && target.isConnected) target.replaceChildren(element("h4", "", "التقييم الموزون بالمراجعة البشرية"), element("p", "form-error", error instanceof TypeError ? "تعذّر الاتصال بالخادم لتحميل التقييم." : error.message));
  }
}

function renderRubric(target, jobPath, base, criteria, source, evidence, assessment) {
  const heading = element("h4", "", "التقييم الموزون بالمراجعة البشرية");
  const notice = element("p", "subtle-note", "الأوزان: الأساسي 40، المفضل 15، الخبرة 20، المشاريع 15، التعليم 10. هذا التقييم يلخص مراجعة بشرية ولا يتخذ قرار توظيف. عدم ذكر دليل لا يثبت عدم امتلاك المهارة.");
  const message = element("p", "subtle-note"); message.setAttribute("aria-live", "polite");
  function setMessage(text, isError = false) { message.textContent = text; message.className = isError ? "form-error" : "subtle-note"; }

  const criterionForm = element("form", "decision-form");
  const categoryLabel = element("label", "", "فئة المعيار");
  const category = element("select");
  for (const [value, label] of Object.entries(rubricCategories)) { const option = element("option", "", label); option.value = value; category.append(option); }
  categoryLabel.append(category);
  const criterionLabel = element("label", "", "معيار الوظيفة");
  const criterionInput = element("input"); criterionInput.required = true; criterionInput.maxLength = 200; criterionInput.placeholder = "مثال: خبرة في Python"; criterionLabel.append(criterionInput);
  const addCriterion = element("button", "outline-button", "إضافة معيار للوظيفة"); addCriterion.type = "submit";
  criterionForm.append(categoryLabel, criterionLabel, addCriterion);

  const sourceLabel = element("label", "", "نص السيرة بعد إخفاء جزئي للبيانات — حدد نصًا لإضافته كدليل");
  const sourceText = element("textarea", "resume-preview rubric-source"); sourceText.readOnly = true; sourceText.rows = 8; sourceText.value = source.text; sourceLabel.append(sourceText);
  const privacyNote = element("p", "subtle-note", "إخفاء البيانات الشخصية جزئي وقد لا يكتشف كل المعلومات المعرِّفة؛ راجع النص قبل استخدامه.");
  const addEvidence = element("button", "outline-button", "حفظ النص المحدد كدليل"); addEvidence.type = "button";
  const suggestEvidence = element("button", "outline-button", "اقتراح مقتطفات لكل معيار"); suggestEvidence.type = "button";
  const suggestions = element("div"); suggestions.setAttribute("aria-live", "polite");
  const evidenceList = element("ul", "evidence-list");
  const modelReview = element("details", "application-detail");
  modelReview.append(element("summary", "", "Optional model-assisted evidence preview"));
  const modelPanel = element("div"); modelReview.append(modelPanel);
  let modelReviewGeneration = 0;
  modelReview.addEventListener("toggle", async () => {
    const generation = ++modelReviewGeneration;
    if (!modelReview.open) { modelPanel.replaceChildren(); return; }
    modelPanel.replaceChildren(element("p", "subtle-note", "Loading current criteria and evidence..."));
    let context, reviewedCriteria, reviewedEvidence;
    try {
      context = await api(`${base}/evidence-bundle/context`);
      [reviewedCriteria, reviewedEvidence] = await Promise.all([api(jobPath), api(`${base}/evidence`)]);
      const verified = await api(`${base}/evidence-bundle/context`);
      if (!modelReview.open || generation !== modelReviewGeneration || !modelPanel.isConnected) return;
      if (context.snapshot !== verified.snapshot || !Array.isArray(reviewedCriteria) || !Array.isArray(reviewedEvidence)) throw new Error("Changed review context");
    } catch (error) {
      if (!isStale(error) && modelReview.open && generation === modelReviewGeneration && modelPanel.isConnected) modelPanel.replaceChildren(element("p", "form-error", "Unable to load a current review. Close and reopen this panel."));
      return;
    }
    const note = element("p", "form-error", "Use synthetic data only. Your selected criterion and partially redacted excerpts are sent to Groq. Redaction can miss personal information. Suggestions can be wrong and do not change judgments or scores.");
    const criterionLabel = element("label", "", "Criterion to review");
    const criterionSelect = element("select");
    for (const criterion of reviewedCriteria) { const option = element("option", "", criterion.label); option.value = criterion.criterion_ref; criterionSelect.append(option); }
    criterionLabel.append(criterionSelect);
    const choices = element("div");
    for (const entry of reviewedEvidence) {
      const label = element("label", "rubric-evidence-choice"); const checkbox = element("input"); checkbox.type = "checkbox"; checkbox.value = entry.evidence_ref;
      label.append(checkbox, document.createTextNode(` ${entry.evidence_ref}: ${entry.excerpt}`)); choices.append(label);
    }
    const consent = element("input"); consent.type = "checkbox";
    const consentLabel = element("label", "", "I reviewed the selected synthetic text and approve sending it to Groq for this preview."); consentLabel.prepend(consent);
    const run = element("button", "outline-button", "Request evidence preview"); run.type = "button";
    const output = element("div"); output.setAttribute("aria-live", "polite");
    run.addEventListener("click", async () => {
      const refs = Array.from(choices.querySelectorAll("input:checked"), (input) => input.value);
      if (!consent.checked || !criterionSelect.value || !refs.length || refs.length > 5) { output.replaceChildren(element("p", "form-error", "Choose a criterion and one to five evidence excerpts, then approve the preview.")); return; }
      run.disabled = true; consent.disabled = true; criterionSelect.disabled = true;
      for (const checkbox of choices.querySelectorAll("input")) checkbox.disabled = true;
      try {
        const response = await api(`${base}/model-evidence-proposal`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({snapshot: context.snapshot, confirmed: true, selections: [{criterion_ref: criterionSelect.value, evidence_refs: refs}]})});
        if (!output.isConnected) return;
        output.replaceChildren(element("p", "subtle-note", response.notice));
        const list = element("ul", "evidence-list");
        for (const proposal of response.proposals || []) {
          const item = element("li", "", `${proposal.evidence_ref}: ${proposal.relation} · ${proposal.reason_code}`);
          if (proposal.model_label_conflict) item.append(element("p", "form-error", "The model's relation and reason appear inconsistent; review the original excerpt."));
          for (const cue of proposal.text_cues || []) item.append(element("p", "subtle-note", `Text cue: ${cue.kind} — ${cue.text}. Its scope is unverified; inspect the context.`));
          list.append(item);
        }
        output.append(list);
      } catch (error) { if (!isStale(error) && output.isConnected) output.replaceChildren(element("p", "form-error", "Preview unavailable or evidence changed. Reload and review the selection before trying again.")); }
      finally { run.disabled = false; consent.disabled = false; criterionSelect.disabled = false; for (const checkbox of choices.querySelectorAll("input")) checkbox.disabled = false; }
    });
    modelPanel.replaceChildren(note, criterionLabel, choices, consentLabel, run, output);
  });
  const evaluation = element("form", "decision-form");
  const judgmentList = element("div");
  const save = element("button", "small-button", "حفظ التقييم البشري"); save.type = "submit";
  const result = element("div"); result.setAttribute("aria-live", "polite");
  evaluation.append(judgmentList, save);
  target.replaceChildren(heading, notice, message, element("h4", "", "معايير الوظيفة"), criterionForm,
    element("h4", "", "أدلة المراجعة"), privacyNote, sourceLabel, addEvidence, suggestEvidence, suggestions, evidenceList, modelReview,
    element("h4", "", "تقييم كل معيار"), evaluation, result);

  let drafts = new Map();
  let criteriaRevision = 0;
  if (assessment) for (const component of assessment.result.components) for (const claim of component.claims) drafts.set(claim.criterion_ref, { status: claim.status, evidence_refs: claim.evidence_refs });

  function rememberDrafts() {
    for (const row of judgmentList.querySelectorAll("[data-criterion-ref]")) {
      drafts.set(row.dataset.criterionRef, { status: row.querySelector("select").value,
        evidence_refs: Array.from(row.querySelectorAll('.rubric-evidence-choice input:checked'), (input) => input.value),
        absence_review_attested: row.querySelector(".rubric-attestation input").checked });
    }
  }
  function renderEvidence() {
    evidenceList.replaceChildren();
    if (!evidence.length) evidenceList.append(element("li", "", "لم تُضف أدلة بعد."));
    for (const item of evidence) evidenceList.append(element("li", "", `${item.evidence_ref}: ${item.excerpt}`));
  }
  function renderJudgments() {
    judgmentList.replaceChildren();
    if (!criteria.length) judgmentList.append(element("p", "empty-note", "أضف معيارًا واحدًا على الأقل للوظيفة."));
    for (const criterion of criteria) {
      const row = element("fieldset", "rubric-criterion"); row.dataset.criterionRef = criterion.criterion_ref;
      row.append(element("legend", "", `${criterion.label} — ${rubricCategories[criterion.category]} (${criterion.criterion_ref})`));
      const statusLabel = element("label", "", "حكم المراجع");
      const status = element("select");
      for (const [value, text] of [["unknown", "غير معروف / لم يُراجع"], ["met", "متحقق وفق الأدلة"], ["missing", "غير مثبت وفق الأدلة"]]) { const option = element("option", "", text); option.value = value; status.append(option); }
      const draft = drafts.get(criterion.criterion_ref) || { status: "unknown", evidence_refs: [] };
      status.value = draft.status; statusLabel.append(status); row.append(statusLabel);
      row.append(element("p", "subtle-note", "اختر دليلًا واحدًا على الأقل عند تحديد متحقق أو غير مثبت؛ اختر غير معروف إذا لم تتوفر أدلة كافية."));
      for (const item of evidence) {
        const label = element("label", "rubric-evidence-choice"); const checkbox = element("input"); checkbox.type = "checkbox"; checkbox.value = item.evidence_ref;
        checkbox.checked = draft.evidence_refs.includes(item.evidence_ref); label.append(checkbox, document.createTextNode(` ${item.evidence_ref}: ${item.excerpt}`)); row.append(label);
      }
      const attestation = element("label", "rubric-attestation");
      const attest = element("input"); attest.type = "checkbox"; attest.checked = draft.absence_review_attested === true;
      attestation.append(attest, document.createTextNode(" أقر أنني راجعت المصدر المتاح كاملًا لهذا المعيار ولم أجد فيه ما يثبت تحققه؛ لا يُعد ذلك إثباتًا لعدم امتلاك المهارة.")); row.append(attestation);
      function updateEvidenceChoices() {
        for (const checkbox of row.querySelectorAll('.rubric-evidence-choice input')) {
          checkbox.disabled = status.value === "unknown";
          if (checkbox.disabled) checkbox.checked = false;
        }
        attestation.hidden = status.value !== "missing";
        if (attestation.hidden) attest.checked = false;
      }
      status.addEventListener("change", updateEvidenceChoices); updateEvidenceChoices();
      judgmentList.append(row);
    }
    save.disabled = !criteria.length;
  }
  function renderResult(item) {
    result.replaceChildren();
    if (!item) { result.append(element("p", "subtle-note", "لا يوجد تقييم محفوظ لهذا الطلب بعد.")); return; }
    const value = item.result;
    result.append(element("h4", "", "آخر تقييم محفوظ"), element("p", "result-badge", `النطاق: ${value.minimum_score}–${value.maximum_score} من 100 · تغطية المراجعة: ${value.coverage_percent}%`));
    if (item.outdated) result.append(element("p", "form-error", "يحتاج هذا التقييم إلى تحديث بعد تغير المعايير أو ضرورة إعادة المراجعة؛ راجع المعايير واحفظ تقييمًا جديدًا."));
    if (item.requires_absence_review) result.append(element("p", "form-error", "يتطلب تقييم سابق إعادة مراجعة أحكام عدم الإثبات وإقرارًا صريحًا قبل اعتمادها."));
    const list = element("ul", "evidence-list");
    for (const part of value.components) list.append(element("li", "", `${rubricCategories[part.category]}: ${part.minimum_points}–${part.maximum_points} نقطة${part.unconfigured ? " (لم تُحدد معايير لهذه الفئة)" : ""}`));
    result.append(list, element("p", "subtle-note", "النتيجة لا تمثل قرار توظيف؛ القيم غير المعروفة تزيد نطاق عدم اليقين."));
  }
  renderEvidence(); renderJudgments(); renderResult(assessment);

  criterionForm.addEventListener("submit", async (event) => {
    event.preventDefault(); addCriterion.disabled = true;
    try {
      const item = await api(jobPath, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ category: category.value, label: criterionInput.value }) });
      if (!target.isConnected) return;
      rememberDrafts(); criteria.push(item); criteriaRevision++; suggestions.replaceChildren(); criterionInput.value = ""; renderJudgments();
      if (assessment) { assessment.outdated = true; renderResult(assessment); }
      setMessage("تمت إضافة معيار الوظيفة. احفظ تقييمًا جديدًا بعد مراجعته.");
    } catch (error) { if (!isStale(error) && target.isConnected) setMessage(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
    finally { addCriterion.disabled = false; }
  });
  suggestEvidence.addEventListener("click", async () => {
    const revision = criteriaRevision;
    suggestEvidence.disabled = true;
    suggestions.replaceChildren(element("p", "subtle-note", "جارٍ البحث عن مقتطفات للمراجعة..."));
    try {
      const bundleContext = await api(`${base}/evidence-bundle/context`);
      const response = await api(`${base}/suggestions`, { method: "POST" });
      if (!target.isConnected) return;
      if (revision !== criteriaRevision) {
        suggestions.replaceChildren(element("p", "form-error", "تغيرت المعايير أثناء البحث؛ اطلب الاقتراحات مجددًا."));
        return;
      }
      if (response.offset_unit !== "unicode_code_points" || !Array.isArray(response.criteria) || !/^[a-f0-9]{64}$/.test(response.source_sha256 || "") || source.source_sha256 !== response.source_sha256 || source.source_context !== response.source_context || bundleContext.source_sha256 !== response.source_sha256 || !/^[a-f0-9]{64}$/.test(bundleContext.snapshot || "")) {
        throw new Error("تعذّر قراءة الاقتراحات؛ حاول مرة أخرى.");
      }
      const sourcePoints = Array.from(source.text);
      const allowed = new Map(criteria.map((criterion) => [criterion.criterion_ref, criterion]));
      const selected = [];
      suggestions.replaceChildren(element("p", "subtle-note", "المقتطفات مقترحة بالتشابه فقط، وقد تكون غير مناسبة أو منفية. راجع السياق بنفسك؛ لا تُثبت الاقتراحات المعيار ولا تغيّر التقييم أو قرار التوظيف."));
      suggestions.append(element("p", "subtle-note", "Search includes recognized sections and unclassified text. Missing results do not prove missing experience; review the entire résumé."));
      if (response.status === "no_recognized_sections") suggestions.append(element("p", "subtle-note", "No section headings were recognized. Suggestions, if any, come from unclassified text."));
      if (response.sections_truncated) suggestions.append(element("p", "form-error", "لم يشمل البحث كل الأقسام بسبب حدود المعالجة؛ راجع السيرة كاملة."));
      if (!response.criteria.length) suggestions.append(element("p", "subtle-note", "لا توجد معايير للبحث؛ أضف معايير الوظيفة أولًا."));
      for (const criterion of response.criteria) {
        const current = allowed.get(criterion.criterion_ref);
        if (!current || current.category !== criterion.category || current.label !== criterion.label || !Array.isArray(criterion.candidates)) {
          suggestions.replaceChildren(element("p", "form-error", "تغيرت المعايير؛ أعد فتح الطلب ثم اطلب الاقتراحات مجددًا."));
          return;
        }
        const group = element("section", "rubric-criterion");
        group.append(element("h4", "", criterion.label));
        if (!criterion.candidates.length) group.append(element("p", "subtle-note", "لم يُقترح مقتطف لهذا المعيار؛ لا يعني ذلك غياب الخبرة."));
        for (const candidate of criterion.candidates) {
          const { start, end, excerpt, similarity } = candidate;
          if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end - start > 500 || sourcePoints.slice(start, end).join("") !== excerpt) {
            suggestions.replaceChildren(element("p", "form-error", "تغير نص السيرة أو تعذّر مطابقة المقتطف؛ أعد فتح الطلب ثم اطلب الاقتراحات مجددًا."));
            return;
          }
          const item = element("div");
          item.append(element("p", "resume-preview", excerpt));
          item.append(element("p", "subtle-note", candidate.section === "unclassified" ? "Source: unclassified résumé text" : `Source section: ${candidate.section || "unknown"}`));
          if (candidate.retrieval_method === "exact_phrase") item.append(element("p", "subtle-note", "Exact phrase match; review the surrounding context. This does not verify a qualification."));
          if (Number.isFinite(similarity)) item.append(element("p", "subtle-note", `Similarity: ${similarity.toFixed(2)} for the matched passage; surrounding text is context. This is not a qualification assessment.`));
          const reviewChoice = element("label", "rubric-evidence-choice");
          const reviewCheckbox = element("input"); reviewCheckbox.type = "checkbox";
          reviewChoice.append(reviewCheckbox, document.createTextNode(" I reviewed this excerpt and want to include it in the evidence bundle."));
          selected.push({checkbox: reviewCheckbox, criterion_ref: criterion.criterion_ref, start, end, excerpt});
          item.append(reviewChoice);
          const accept = element("button", "outline-button", "حفظ المقتطف بعد مراجعته"); accept.type = "button";
          accept.addEventListener("click", async () => {
            if (revision !== criteriaRevision || !target.isConnected || !accept.isConnected) {
              setMessage("تغيرت المعايير؛ اطلب الاقتراحات مجددًا.", true);
              return;
            }
            accept.disabled = true;
            try {
              const saved = await api(`${base}/evidence`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ start, end, expected_source_sha256: response.source_sha256, expected_source_context: response.source_context,
                expected_criterion: { criterion_ref: criterion.criterion_ref, category: criterion.category, label: criterion.label } }) });
              if (!target.isConnected) return;
              rememberDrafts(); evidence.push(saved); renderEvidence(); renderJudgments();
              setMessage("تم حفظ المقتطف كدليل؛ اربطه بالمعيار وحدد حكمك بعد المراجعة.");
              suggestions.replaceChildren(element("p", "subtle-note", "Excerpt saved. Request fresh suggestions before selecting a bundle."));
            } catch (error) {
              if (!isStale(error) && target.isConnected) setMessage(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true);
              accept.disabled = false;
            }
          });
          item.append(accept); group.append(item);
        }
        suggestions.append(group);
      }
      if (selected.length) {
        const saveBundle = element("button", "small-button", "Save selected reviewed excerpts"); saveBundle.type = "button";
        saveBundle.addEventListener("click", async () => {
          const items = selected.filter((item) => item.checkbox.checked).map(({criterion_ref, start, end, excerpt}) => ({criterion_ref, start, end, excerpt, reviewed: true}));
          if (!items.length || items.length > 30) { setMessage("Select between 1 and 30 reviewed excerpts.", true); return; }
          saveBundle.disabled = true;
          try {
            const saved = await api(`${base}/evidence-bundle`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({snapshot: bundleContext.snapshot, items})});
            if (!target.isConnected) return;
            rememberDrafts();
            for (const entry of saved.items) if (!evidence.some((item) => item.evidence_ref === entry.evidence.evidence_ref)) evidence.push(entry.evidence);
            renderEvidence(); renderJudgments(); suggestions.replaceChildren(element("p", "subtle-note", "Reviewed excerpts saved. Assign judgments separately after examining the full context."));
            setMessage("Evidence bundle saved; no assessment or decision was made.");
          } catch (error) {
            if (!isStale(error) && target.isConnected) { suggestions.replaceChildren(element("p", "form-error", "Could not confirm the bundle was saved. Reload the application and evidence before trying again.")); }
          }
        });
        suggestions.append(saveBundle);
      }
    } catch (error) {
      if (!isStale(error) && target.isConnected) suggestions.replaceChildren(element("p", "form-error", error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message));
    } finally { suggestEvidence.disabled = false; }
  });
  addEvidence.addEventListener("click", async () => {
    // Textareas normalize CRLF to LF and JS uses UTF-16 offsets; the API uses
    // Unicode code points in the original source, including both CR and LF.
    function sourceOffset(selectionOffset) {
      let visible = 0, original = 0;
      const points = Array.from(source.text);
      while (original < points.length) {
        if (visible === selectionOffset) return original;
        const point = points[original];
        if (point === "\r" && points[original + 1] === "\n") { visible++; original += 2; }
        else { visible += point.length; original++; }
        if (visible > selectionOffset) return null;
      }
      return visible === selectionOffset ? original : null;
    }
    const start = sourceOffset(sourceText.selectionStart);
    const end = sourceOffset(sourceText.selectionEnd);
    if (start === null || end === null || end <= start || end - start > 500 || !sourceText.value.slice(sourceText.selectionStart, sourceText.selectionEnd).trim()) { setMessage("حدد نصًا ظاهرًا من السيرة بطول من 1 إلى 500 حرف.", true); return; }
    addEvidence.disabled = true;
    try {
      const item = await api(`${base}/evidence`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ start, end, expected_source_sha256: source.source_sha256, expected_source_context: source.source_context }) });
      if (!target.isConnected) return;
      rememberDrafts(); evidence.push(item); renderEvidence(); renderJudgments(); setMessage("تم حفظ الدليل؛ يمكنك ربطه بالمعايير المناسبة.");
    } catch (error) { if (!isStale(error) && target.isConnected) setMessage(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
    finally { addEvidence.disabled = false; }
  });
  evaluation.addEventListener("submit", async (event) => {
    event.preventDefault(); rememberDrafts();
    const judgments = criteria.map((criterion) => {
      const draft = drafts.get(criterion.criterion_ref);
      return { criterion_ref: criterion.criterion_ref, status: draft.status, evidence_refs: draft.evidence_refs,
        ...(draft.status === "missing" && draft.absence_review_attested ? { absence_review_attested: true } : {}) };
    });
    if (judgments.some((item) => item.status !== "unknown" && !item.evidence_refs.length)) { setMessage("اربط دليلًا واحدًا على الأقل بكل حكم تمت مراجعته.", true); return; }
    if (judgments.some((item) => item.status === "missing" && item.absence_review_attested !== true)) { setMessage("أقر بمراجعة المواد المتاحة لكل معيار حددت أنه غير مثبت.", true); return; }
    for (const item of judgments) if (item.status === "unknown") item.evidence_refs = [];
    save.disabled = true;
    try {
      assessment = await api(`${base}/assessment`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ judgments, expected_source_context: source.source_context }) });
      if (!target.isConnected) return;
      renderResult(assessment); setMessage("تم حفظ التقييم البشري دون تغيير قرار الطلب.");
    } catch (error) { if (!isStale(error) && target.isConnected) setMessage(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
    finally { save.disabled = false; }
  });
}

function setupGraphReview(target, application) {
  const storageKey = `tawzeef-graph-${currentUserId}-${application.id}`;
  const title = element("h4", "", "مراجعة متعددة المراحل باستخدام LangGraph");
  const note = element("p", "subtle-note", "تجمع المراجعة نتائج أدوات محلية وتتوقف لتأكيد بشري. يمكن اقتراح موعد عام متاح بعد مراجعة تفاصيله والموافقة عليه؛ يمكن لأي مرشح مؤهل اختياره. لا ينشئ هذا المسار حدث Google أو يرسل دعوة. المواعيد لقطة قد تتغير.");
  note.textContent = "Review local tool results and approve proposed slots. Choose a shared slot or reserve a slot for this application. The candidate books separately; this does not create a Google event or send an invitation. Availability can change.";
  const message = element("p", "subtle-note"); message.setAttribute("aria-live", "polite");
  const form = element("form", "decision-form");
  const intentLabel = element("label", "", "نوع المراجعة");
  const intent = element("select");
  for (const [value, text] of [["full", "مراجعة شاملة"], ["adaptive", "خطة مراجعة حسب حالة الطلب"], ["adaptive_schedule", "خطة جدولة مع مراجعة بشرية"], ["policy", "سؤال سياسة"], ["scheduling", "المواعيد"], ["status", "حالة الطلب"], ["create_slot", "اقتراح موعد متاح"]]) {
    const option = element("option", "", text); option.value = value; intent.append(option);
  }
  const privateSlotOption = element("option", "", "Propose a slot for this candidate only");
  privateSlotOption.value = "create_private_slot"; intent.append(privateSlotOption);
  const advanceOption = element("option", "", "Review and advance this application");
  advanceOption.value = "advance_application"; intent.append(advanceOption);
  const advanceSlotOption = element("option", "", "Review, advance, and reserve a candidate-specific slot");
  advanceSlotOption.value = "advance_and_reserve_slot"; intent.append(advanceSlotOption);
  const semanticOption = element("option", "", "Review semantic résumé evidence");
  semanticOption.value = "semantic_review"; intent.append(semanticOption);
  const semanticAdvanceOption = element("option", "", "Review semantic evidence, advance, and reserve a private slot");
  semanticAdvanceOption.value = "semantic_advance_and_reserve_slot"; intent.append(semanticAdvanceOption);
  const adaptiveAdvanceOption = element("option", "", "Adaptive review, advance, and reserve a private slot");
  adaptiveAdvanceOption.value = "adaptive_advance"; intent.append(adaptiveAdvanceOption);
  const screenActionOption = element("option", "", "Review and record keyword screening");
  screenActionOption.value = "screen_application"; intent.append(screenActionOption);
  const completionOption = element("option", "", "Review completed interview"); completionOption.value = "complete_interview"; intent.append(completionOption);
  const offerOption = element("option", "", "Review and present an offer"); offerOption.value = "present_offer"; intent.append(offerOption);
  intentLabel.append(intent);
  const interviewLabel = element("label", "", "Interview ID"); const interviewId = element("input"); interviewId.type = "number"; interviewId.min = "1"; interviewId.step = "1"; interviewId.disabled = true; interviewLabel.hidden = true; interviewLabel.append(interviewId);
  const offerLabel = element("label", "", "Offer details"); const offerDetails = element("textarea"); offerDetails.maxLength = 4000; offerDetails.disabled = true; offerLabel.hidden = true; offerLabel.append(offerDetails);
  const questionLabel = element("label", "", "سؤال سياسة اختياري للبحث المحلي");
  const question = element("textarea"); question.maxLength = 2000; question.placeholder = "يمكن تركه فارغًا"; questionLabel.append(question);
  const adaptiveSemanticLabel = element("label", "", "Allow the adaptive planner to use local semantic evidence when available; it may omit this step.");
  const adaptiveSemantic = element("input"); adaptiveSemantic.type = "checkbox"; adaptiveSemantic.disabled = true;
  adaptiveSemanticLabel.prepend(adaptiveSemantic); adaptiveSemanticLabel.hidden = true;
  const slotFields = element("div"); slotFields.hidden = true;
  const slotStartLabel = element("label", "", "بداية الموعد"); const slotStart = element("input"); slotStart.type = "datetime-local"; slotStart.disabled = true; slotStartLabel.append(slotStart);
  const slotEndLabel = element("label", "", "نهاية الموعد"); const slotEnd = element("input"); slotEnd.type = "datetime-local"; slotEnd.disabled = true; slotEndLabel.append(slotEnd);
  const interviewerLabel = element("label", "", "اسم المحاور"); const interviewer = element("input"); interviewer.maxLength = 120; interviewer.disabled = true; interviewerLabel.append(interviewer);
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "التوقيت المحلي";
  slotFields.append(element("p", "subtle-note", `الوقت المدخل حسب المنطقة الزمنية لجهازك: ${timezone}.`), slotStartLabel, slotEndLabel, interviewerLabel);
  intent.addEventListener("change", () => {
    const isCompletion = intent.value === "complete_interview"; interviewLabel.hidden = !isCompletion; interviewId.disabled = !isCompletion; interviewId.required = isCompletion;
    const isOffer = intent.value === "present_offer"; offerLabel.hidden = !isOffer; offerDetails.disabled = !isOffer; offerDetails.required = isOffer;
    const adaptiveMode = ["adaptive", "adaptive_schedule", "adaptive_advance"].includes(intent.value);
    adaptiveSemanticLabel.hidden = !adaptiveMode; adaptiveSemantic.disabled = !adaptiveMode;
    if (!adaptiveMode) adaptiveSemantic.checked = false;
    const supportsPolicy = intent.value === "full" || intent.value === "policy";
    questionLabel.hidden = !supportsPolicy; question.disabled = !supportsPolicy;
    question.required = intent.value === "policy";
    questionLabel.firstChild.textContent = question.required ? "سؤال السياسة المطلوب للبحث المحلي" : "سؤال سياسة اختياري للبحث المحلي";
    question.placeholder = question.required ? "اكتب سؤالك عن السياسة" : "يمكن تركه فارغًا";
    if (!supportsPolicy) question.value = "";
    const createSlot = ["create_slot", "create_private_slot", "advance_and_reserve_slot", "semantic_advance_and_reserve_slot", "adaptive_schedule", "adaptive_advance"].includes(intent.value);
    slotFields.hidden = !createSlot;
    for (const field of [slotStart, slotEnd, interviewer]) { field.disabled = !createSlot; field.required = createSlot; }
  });
  const start = element("button", "small-button", "بدء مراجعة المراحل"); start.type = "submit";
  form.append(intentLabel, interviewLabel, offerLabel, adaptiveSemanticLabel, questionLabel, slotFields, element("p", "subtle-note", "يُعرض مقتطف من المصادر المصرح بها عند إدخال سؤال، ويُعاد فحصها عند تحميل المراجعة. لا يُحفظ نص السؤال في المراجعة."), start);
  const restore = element("form", "decision-form");
  const idLabel = element("label", "", "معرّف مراجعة سابقة لهذا الطلب");
  const idInput = element("input"); idInput.maxLength = 100; idLabel.append(idInput);
  const open = element("button", "outline-button", "تحميل المراجعة"); open.type = "submit";
  restore.append(idLabel, open);
  const output = element("div"); output.setAttribute("aria-live", "polite");
  target.replaceChildren(title, note, message, form, restore, output);
  const labels = { parser: "قراءة النص المستخرج", matching: "فحص المهارات المذكورة", policy: "بحث السياسة المحلي", scheduler: "استعراض المواعيد المحلية", status: "حالة الطلب", agent_review: "مساعد أدوات القراءة", prepare_slot: "تجهيز مقترح الموعد", human_review: "مراجعة بشرية", execute_slot: "إضافة الموعد بعد الموافقة" };
  labels.prepare_advance = "Prepare advancement proposal";
  labels.execute_advance = "Advance after HR approval";
  labels.semantic = "Find résumé passages for human review";
  labels.prepare_completion = "Prepare interview completion for HR review"; labels.execute_completion = "Record completion after HR approval";
  labels.prepare_offer = "Prepare offer for HR review"; labels.execute_offer = "Present offer after HR approval";
  labels.prepare_screen = "Prepare screening for HR review";
  labels.execute_screen = "Record approved screening";
  function report(text, error = false) { message.textContent = text; message.className = error ? "form-error" : "subtle-note"; }
  function remember(id) { try { sessionStorage.setItem(storageKey, id); } catch { /* Manual restore remains available. */ } }
  function display(review) {
    if (review.state?.application_id !== application.id) throw new Error("هذه المراجعة لا تخص الطلب المعروض.");
    idInput.value = review.id; remember(review.id);
    output.replaceChildren(element("p", "subtle-note", `معرّف المراجعة: ${review.id}`));
    const plan = element("ol", "evidence-list");
    for (const step of review.state.plan || []) plan.append(element("li", "", labels[step] || step));
    output.append(element("h4", "", "مراحل المراجعة"), plan);
    if (review.state.intent === "adaptive") {
      const planSources = { model: "اقترح النموذج الخطة وتحقق الخادم من خطواتها.", deterministic: "استُخدمت خطة محددة حسب حالة الطلب دون نموذج.", deterministic_fallback: "تعذّر استخدام اقتراح النموذج أو لم يجتز التحقق؛ استُخدمت خطة بديلة محددة." };
      const planNotice = review.state.adaptive_goal === "advance_and_reserve" ? "Advancement and the private slot require explicit HR approval." : review.state.adaptive_goal === "schedule_interview" ? "يمكن إضافة موعد عام فقط بعد الموافقة البشرية على تفاصيله." : "المراحل للقراءة فقط وتحتاج مراجعة بشرية.";
      output.append(element("p", "subtle-note", `${planSources[review.state.plan_source] || "مصدر الخطة غير معروف."} ${planNotice}`));
      if (review.state.plan_stale) output.append(element("p", "form-error", "تغير الطلب أو الوظيفة منذ إعداد الخطة؛ ابدأ مراجعة جديدة."));
    }
    const parsed = review.state.parsed;
    if (parsed) output.append(element("p", "", parsed.text_available ? "يتوفر نص مستخرج؛ تحليل الأقسام تقديري ويحتاج مراجعة في قسم السيرة المنظمة." : "لا يتوفر نص مستخرج من الملف."));
    const fields = review.state.resume_fields;
    if (fields) {
      output.append(element("h4", "", "حقول مقترحة من السيرة"));
      if (fields.status === "limit_exceeded") output.append(element("p", "form-error", "النص بعد الحجب أكبر من حد تحليل الحقول؛ راجع السيرة يدويًا."));
      else if (fields.status !== "current") output.append(element("p", "form-error", "تغيرت السيرة أو لا يمكن التحقق من المراجعة القديمة؛ ابدأ مراجعة جديدة."));
      else {
        output.append(element("p", "subtle-note", "قيم مقتبسة من تسميات صريحة أو صيغ محدودة؛ تجميع السجلات تقديري والحجب جزئي. راجع السيرة قبل الاعتماد عليها، والحقول المفقودة غير معروفة."));
        const sectionLabels = { experience: "الخبرة", education: "التعليم", projects: "المشاريع" };
        const fieldLabels = { role: "الدور", organization: "الجهة", period: "الفترة", qualification: "المؤهل", institution: "المؤسسة التعليمية", name: "اسم المشروع", technologies: "التقنيات" };
        for (const [name, section] of Object.entries(fields.sections || {})) {
          const list = element("ul", "evidence-list");
          for (const record of section.records || []) {
            const item = element("li");
            for (const [field, value] of Object.entries(record.fields || {})) if (value?.value) item.append(element("p", "", `${fieldLabels[field] || field}: ${value.value}`));
            list.append(item);
          }
          output.append(element("h5", "", sectionLabels[name] || name));
          if (list.childNodes.length) output.append(list); else output.append(element("p", "subtle-note", "لم تُستخرج حقول؛ المعلومات غير معروفة."));
          if (section.unclassified_lines || section.truncated) output.append(element("p", "subtle-note", "توجد أسطر غير مصنفة أو اقتُطع التحليل؛ راجع المصدر كاملًا."));
        }
      }
    }
    if (review.state.provisional_screening) {
      const provisional = review.state.provisional_screening;
      output.append(element("h4", "", "Live provisional screening"),
        element("p", "subtle-note", provisional.notice),
        element("p", "", `Review state: ${provisional.recommendation}; source review: ${provisional.source_review_status}.`),
        element("p", "subtle-note", `Origin: ${provisional.origin || "not yet scored"}; threshold: ${provisional.threshold === null ? "not configured" : provisional.threshold}.`));
      if (provisional.current && provisional.score !== null) output.append(element("p", "result-badge", `Provisional skill mentions: ${provisional.score} / 100`));
    }
    if (review.state.matching) { const score = element("div"); renderScore(score, review.state.matching); output.append(score); }
    if (review.state.matching_unavailable) output.append(element("p", "form-error", review.state.matching_unavailable.notice));
    const specialist = review.state.matching_specialist;
    if (specialist) {
      output.append(element("h4", "", "ملخص التقييم البشري الموزون"));
      const reviewedFields = specialist.reviewed_fields;
      if (reviewedFields?.status === "current") {
        output.append(element("p", "subtle-note", "حقول اختارها HR من السيرة المحجوبة جزئيًا؛ تُعرض للمراجعة ولا تغيّر النطاق أو تثبت المؤهلات."));
        const fieldList = element("ul", "evidence-list");
        for (const item of reviewedFields.fields || []) fieldList.append(element("li", "", `${item.section} · ${item.record_ref} · ${item.field}: ${item.value}`));
        if (fieldList.childNodes.length) output.append(fieldList); else output.append(element("p", "subtle-note", "مراجعة الحقول المحفوظة لا تتضمن مقاطع."));
      } else if (reviewedFields?.status === "stale") output.append(element("p", "form-error", "مراجعة الحقول تحتاج إعادة اختيار بعد تغير السيرة."));
      else if (reviewedFields?.status === "limit_exceeded") output.append(element("p", "form-error", "تعذّر عرض مراجعة الحقول لأن النص بعد الحجب تجاوز الحد."));
      const messages = { not_assessed: "لا يوجد تقييم محفوظ لهذا الطلب.", outdated: "التقييم السابق يحتاج تحديثًا بعد تغيير المعايير أو الأدلة.", stale: "تغير الطلب أو تقييمه منذ بدء المراجعة؛ ابدأ مراجعة جديدة." };
      if (specialist.status !== "current") output.append(element("p", "subtle-note", messages[specialist.status] || "ملخص التقييم غير متاح."));
      else {
        if (specialist.notice) output.append(element("p", "subtle-note", specialist.notice));
        if (specialist.bounds) output.append(element("p", "result-badge", `النطاق: ${specialist.bounds.minimum}–${specialist.bounds.maximum} من 100 · التغطية: ${specialist.bounds.coverage_percent}%`));
        renderAssessmentComponents(output, specialist.components);
        const list = element("ul", "evidence-list");
        const claimLabels = { met: "مثبت بالمراجعة", missing: "لم يُثبت بعد المراجعة", unknown: "غير معروف" };
        for (const criterion of specialist.criteria || []) {
          const item = element("li");
          item.append(element("strong", "", criterion.criterion || criterion.criterion_ref), element("p", "", `الحالة: ${claimLabels[criterion.status] || "غير متاحة"}`));
          for (const evidence of criterion.evidence || []) {
            item.append(element("p", "", evidence.excerpt));
            for (const caution of evidence.cautions || []) item.append(element("p", "subtle-note", `Text cue: ${caution.kind} — ${caution.text}. Review context; other issues may be missed.`));
          }
          if (criterion.question_ar || criterion.question) item.append(element("p", "subtle-note", criterion.question_ar || criterion.question));
          list.append(item);
        }
        output.append(list);
      }
    }
    const semantic = review.state.semantic_suggestions;
    if (review.state.interview_guide) renderInterviewGuide(output, review.state.interview_guide);
    if (semantic) {
      output.append(element("h4", "", "Semantic evidence suggestions"));
      if (semantic.status !== "current") output.append(element("p", "form-error", "The résumé or criteria changed; start a new review."));
      else {
        output.append(element("p", "subtle-note", semantic.notice || "Suggestions are unverified; review the full résumé and context."));
        for (const criterion of semantic.criteria || []) {
          output.append(element("h5", "", criterion.label));
          const list = element("ul", "evidence-list");
          for (const candidate of criterion.candidates || []) {
            const item = element("li");
            const method = candidate.retrieval_method === "exact_phrase" ? "Exact phrase match; not a verified qualification." : `Similarity: ${Number.isFinite(candidate.similarity) ? candidate.similarity.toFixed(2) : "unavailable"} for the matched passage; surrounding text is context.`;
            item.append(element("p", "resume-preview", candidate.excerpt), element("p", "subtle-note", `Source: ${candidate.section || "unknown"} · ${method}`));
            list.append(item);
          }
          if (list.childNodes.length) output.append(list); else output.append(element("p", "subtle-note", "No passage was suggested; this does not establish a missing qualification."));
        }
      }
    }
    const policy = review.state.policy;
    if (policy?.status === "answer" && typeof policy.answer === "string" && Array.isArray(policy.sources)) {
      output.append(element("h4", "", "نتيجة بحث السياسة"), element("p", "subtle-note", "مقتطفات من المصادر؛ تحقق من السياق قبل الاعتماد عليها."), element("p", "", policy.answer));
      const sources = element("ul", "evidence-list");
      for (const source of policy.sources) { const item = element("li"); item.append(element("strong", "", knowledgeSourceLabel(source)), element("p", "", source.text)); sources.append(item); }
      output.append(sources);
    } else if (policy?.status === "abstain") {
      output.append(element("p", "", "لم يُعثر على مصدر مناسب لسؤال السياسة."));
      if (policy.next_step) output.append(element("p", "subtle-note", policy.next_step));
    }
    else if (policy?.status === "not_requested") output.append(element("p", "", "لم يُطلب بحث في السياسات."));
    else if (policy?.status === "searched_without_retaining_results") output.append(element("p", "", "أُجري بحث سابق دون حفظ نتائجه؛ استخدم قسم المعرفة للبحث مجددًا."));
    else if (policy) output.append(element("p", "", "لم تعد مصادر السياسة متاحة أو تغيرت؛ أعد البحث بعد التحقق من الصلاحيات."));
    const scheduling = review.state.scheduling;
    if (scheduling) {
      if (scheduling.interview_completed) output.append(element("p", "", "سُجّل إجراء مقابلة سابقة لهذا الطلب."));
      output.append(element("p", "", scheduling.eligible ? "الطلب مؤهل لاستعراض المواعيد المحلية. الحجز إجراء منفصل." : "لا توجد أهلية حاليًا لاستعراض مواعيد الحجز للطلب."));
      if (scheduling.available_slots?.length) {
        const slots = element("ul", "evidence-list");
        for (const slot of scheduling.available_slots) slots.append(element("li", "", `من ${displayDate(slot.start_at)} إلى ${displayDate(slot.end_at)}`));
        output.append(slots);
      }
    }
    if (review.state.status) output.append(element("p", "", `الحالة الحالية للطلب: ${statusLabels[review.state.status.application_status] || review.state.status.application_status}`));
    if (review.state.candidate_progress) {
      const progress = review.state.candidate_progress;
      const labels = {applied: "Applied", screened: "Screened", shortlisted: "Shortlisted", interview_scheduled: "Interview scheduled", interviewed: "Interview completed", offered: "Offer presented", hired: "Hired", rejected: "Rejected"};
      output.append(element("p", "subtle-note", `Live candidate-visible stage: ${labels[progress.stage] || "Unavailable"}. ${progress.notice}`));
    }
    const agent = review.state.agent_review;
    if (agent) {
      const agentStatus = { ok: "اكتملت قراءة الأدوات", disabled: "المساعد غير مفعّل", unavailable: "المساعد غير متاح", rejected: "رُفض رد المساعد", limit_reached: "وصل المساعد للحد المسموح" };
      output.append(element("p", "", `مساعد أدوات القراءة: ${agentStatus[agent.status] || "حالة غير معروفة"}.`));
      const toolLabels = { job_requirements: "متطلبات الوظيفة", workflow_status: "حالة سير الطلب", screening_evidence: "مؤشرات الفحص" };
      if (agent.tool_trace?.length) output.append(element("p", "subtle-note", `الأدوات المستخدمة: ${agent.tool_trace.map((tool) => toolLabels[tool] || "أداة غير معروفة").join("، ")}.`));
    }
    const proposal = review.state.action_proposal;
    const result = review.state.action_result;
    const screeningPreview = review.state.screening_preview;
    if (screeningPreview) {
      output.append(element("h4", "", "Screening preview"));
      if (screeningPreview.status === "current" && screeningPreview.score) { const box = element("div"); renderScore(box, screeningPreview.score); output.append(box); }
      else output.append(element("p", "form-error", "The screening inputs changed. Start a new review."));
    }
    if (proposal?.type === "complete_interview" && review.awaiting_review) { const v = review.state.interview_preview; output.append(element("p", "subtle-note", v?.status === "current" ? `Interview #${v.id}: ${displayDate(v.start_at)} to ${displayDate(v.end_at)}; interviewer ${v.interviewer}. Confirm only if it took place.` : "Interview changed; start a new review.")); }
    if (proposal?.type === "present_offer" && review.awaiting_review) {
      const v = review.state.offer_preview;
      if (v?.status === "current") output.append(element("h4", "", "Offer preview"), element("p", "", `Candidate #${v.candidate_id}: ${v.candidate_name} (${v.candidate_username})`), element("p", "", `Role: ${v.job_title} (${v.job_id})`), element("pre", "resume-preview", v.details), element("p", "subtle-note", "Approval presents this offer in the local application. No message is sent."));
      else output.append(element("p", "form-error", "Offer or application changed; start a new review."));
    }
    if (proposal?.type === "screen_application") output.append(element("p", "subtle-note", `Approval records the reviewed keyword screening for application #${proposal.application_id}, candidate #${proposal.candidate_id}. It does not advance, reject, or hire the candidate.`));
    if (["create_slot", "create_private_slot"].includes(proposal?.type) && proposal.slot) {
      output.append(element("h4", "", "تفاصيل الموعد المقترح"),
        element("p", "", `طلب سياق المراجعة: #${proposal.application_id}`),
        element("p", "", `من ${displayDate(proposal.slot.start_at)} إلى ${displayDate(proposal.slot.end_at)}`),
        element("p", "", `المحاور: ${proposal.slot.interviewer}`),
        element("p", "subtle-note", proposal.type === "create_private_slot" ? "Approval makes this slot available only to the candidate for this application. It does not book the interview or send a message." : "Approval makes this slot available to all eligible candidates. It does not book the interview or send a message."));
    }
    if (proposal?.type === "advance_application") {
      output.append(element("h4", "", "Proposed application advancement"),
        element("p", "", `Application #${proposal.application_id}: ${proposal.current_status} → ${proposal.target_status}`),
        element("p", "subtle-note", "Approval advances this application for scheduling. It does not book an interview, send a message, or make a hiring decision."));
    }
    if (proposal?.type === "advance_and_reserve_slot" && proposal.slot) {
      output.append(element("h4", "", "Proposed advancement and private interview slot"),
        element("p", "", `Application #${proposal.application_id} · candidate #${proposal.candidate_id}: ${proposal.current_status} → ${proposal.target_status}`),
        element("p", "", `${displayDate(proposal.slot.start_at)} – ${displayDate(proposal.slot.end_at)} · ${proposal.slot.interviewer}`),
        element("p", "subtle-note", "Approval advances this application and reserves the slot only for its candidate. The candidate books separately; no message or Google event is sent."));
    }
    if (result?.status === "created") output.append(element("p", "result-badge", proposal?.type === "create_private_slot" ? "The candidate-specific slot was added after approval." : "The shared slot was added after approval."));
    else if (result?.status === "advanced") output.append(element("p", "result-badge", "The application was advanced after HR approval."));
    else if (result?.status === "advanced_and_reserved") output.append(element("p", "result-badge", "The application was advanced and its private slot reserved after HR approval."));
    else if (result?.status === "interview_completed") output.append(element("p", "result-badge", "Interview completion recorded after HR approval."));
    else if (result?.status === "offer_presented") output.append(element("p", "result-badge", "The offer was presented after HR approval."));
    else if (result?.status === "screened") output.append(element("p", "result-badge", "The reviewed keyword screening was recorded."));
    else if (result?.status === "declined") output.append(element("p", "result-badge", "The proposal was declined; no action was taken."));
    if (review.awaiting_review && (proposal || ["create_slot", "create_private_slot", "advance_application", "advance_and_reserve_slot"].includes(review.state.intent))) {
      output.append(element("p", "result-badge", "المقترح بانتظار قرار الموارد البشرية."));
      const validScope = (proposal?.type === "create_slot" && proposal.availability_scope === "all_eligible_candidates") || (proposal?.type === "create_private_slot" && proposal.availability_scope === "selected_application_only");
      const validSlot = validScope && proposal.slot?.start_at && proposal.slot?.end_at && proposal.slot?.interviewer;
      const validAdvance = proposal?.type === "advance_application" && proposal.current_status === "submitted" && proposal.target_status === "advanced" && Number.isInteger(proposal.assessment_id) && proposal.assessment_id > 0;
      const validCombined = proposal?.type === "advance_and_reserve_slot" && proposal.availability_scope === "selected_application_only" && proposal.current_status === "submitted" && proposal.target_status === "advanced" && Number.isInteger(proposal.candidate_id) && proposal.candidate_id > 0 && Number.isInteger(proposal.assessment_id) && proposal.assessment_id > 0 && proposal.slot?.start_at && proposal.slot?.end_at && proposal.slot?.interviewer;
      const validScreen = proposal?.type === "screen_application" && Number.isInteger(proposal.candidate_id) && proposal.candidate_id > 0 && screeningPreview?.status === "current";
      const validCompletion = proposal?.type === "complete_interview" && review.state.interview_preview?.status === "current" && proposal.interview_id === review.state.interview_preview.id;
      const offerPreview = review.state.offer_preview;
      const validOffer = proposal?.type === "present_offer" && offerPreview?.status === "current" && Number.isInteger(offerPreview.candidate_id) && offerPreview.candidate_id > 0 && offerPreview.candidate_name && offerPreview.candidate_username && offerPreview.job_id && offerPreview.job_title && offerPreview.details;
      const discardOnly = proposal?.type === "present_offer" && review.state.intent === "present_offer" && proposal.application_id === application.id && offerPreview?.status === "stale";
      if ((!validSlot && !validAdvance && !validCombined && !validScreen && !validCompletion && !validOffer && !discardOnly) || proposal.application_id !== application.id || (!proposal.approval_fingerprint && !discardOnly)) {
        output.append(element("p", "form-error", "تعذّرت مراجعة تفاصيل المقترح بأمان. أعد تحميل المراجعة."));
        return;
      }
      const approve = element("button", "small-button", validOffer ? "Approve and present offer" : validCompletion ? "Confirm interview took place" : validScreen ? "Approve and record screening" : validCombined ? "Approve advancement and private slot" : validAdvance ? "Approve advancement" : "Approve this slot"); approve.type = "button";
      const decline = element("button", "outline-button", "أرفض المقترح"); decline.type = "button";
      const staleSemantic = (review.state.intent === "semantic_advance_and_reserve_slot" || (review.state.intent === "adaptive" && review.state.plan?.includes("semantic"))) && semantic?.status !== "current";
      if (discardOnly || review.state.plan_stale || staleSemantic) approve.disabled = true;
      if (review.state.plan_stale || staleSemantic) decline.disabled = true;
      async function decide(reviewed) {
        let uncertain = false;
        approve.disabled = true; decline.disabled = true; start.disabled = true; open.disabled = true; report("جارٍ تسجيل القرار...");
        try {
          const body = reviewed ? { reviewed: true, approval_fingerprint: proposal.approval_fingerprint } : { reviewed: false };
          const updated = await api(`/api/graph-reviews/${encodeURIComponent(review.id)}/resume`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
          if (target.isConnected) { display(updated); report(""); }
        } catch (error) {
          uncertain = error instanceof TypeError;
          if (!isStale(error) && target.isConnected) report(uncertain ? "تعذّر الاتصال بالخادم. أعد تحميل المراجعة للتحقق من حالة القرار قبل المحاولة مجددًا." : error.message, true);
        } finally { approve.disabled = discardOnly || uncertain || Boolean(review.state.plan_stale) || staleSemantic; decline.disabled = uncertain || Boolean(review.state.plan_stale) || staleSemantic; start.disabled = false; open.disabled = false; }
      }
      approve.addEventListener("click", () => decide(true)); decline.addEventListener("click", () => decide(false));
      if (discardOnly) {
        decline.textContent = "Discard offer review";
        output.append(decline);
      } else output.append(approve, decline);
    } else if (review.awaiting_review) {
      output.append(element("p", "result-badge", "توقفت المراجعة بانتظار التأكيد البشري."));
      const resume = element("button", "small-button", "أؤكد أنني راجعت النتائج واستكمل المراجعة"); resume.type = "button";
      if (review.state.intent === "adaptive" && review.state.plan_stale) resume.disabled = true;
      if (review.state.intent === "semantic_review" && semantic?.status !== "current") resume.disabled = true;
      if (review.state.intent === "adaptive" && review.state.plan?.includes("semantic") && semantic?.status !== "current") resume.disabled = true;
      resume.addEventListener("click", async () => {
        resume.disabled = true; start.disabled = true; open.disabled = true; report("جارٍ استكمال المراجعة...");
        try {
          const updated = await api(`/api/graph-reviews/${encodeURIComponent(review.id)}/resume`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reviewed: true }) });
          if (target.isConnected) { display(updated); report("اكتملت مراجعة المراحل دون تغيير قرار الطلب أو حجز موعد."); }
        } catch (error) { if (!isStale(error) && target.isConnected) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
        finally { resume.disabled = (review.state.intent === "semantic_review" || (review.state.intent === "adaptive" && review.state.plan?.includes("semantic"))) && semantic?.status !== "current"; start.disabled = false; open.disabled = false; }
      });
      output.append(resume);
    } else if (!result) output.append(element("p", "result-badge", review.state.reviewed === true ? "اكتملت المراجعة بتأكيد بشري." : "انتهت المراجعة دون تأكيد بأن النتائج روجعت."));
  }
  async function load(id) {
    const review = await api(`/api/graph-reviews/${encodeURIComponent(id)}`);
    if (target.isConnected) { display(review); report(""); }
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); start.disabled = true; open.disabled = true; output.replaceChildren(); report("جارٍ تنفيذ المراحل حتى نقطة المراجعة البشرية...");
    try {
      const adaptiveMode = ["adaptive", "adaptive_schedule", "adaptive_advance"].includes(intent.value);
      const body = { application_id: application.id, intent: adaptiveMode ? "adaptive" : intent.value };
      if (intent.value === "complete_interview") body.interview_id = Number(interviewId.value);
      if (intent.value === "present_offer") body.offer_details = offerDetails.value.trim();
      if (adaptiveMode) { body.adaptive_goal = intent.value === "adaptive" ? "review_application" : intent.value === "adaptive_schedule" ? "schedule_interview" : "advance_and_reserve"; body.allow_semantic = adaptiveSemantic.checked; }
      else body.policy_question = question.value.trim();
      if (["create_slot", "create_private_slot", "advance_and_reserve_slot", "semantic_advance_and_reserve_slot", "adaptive_schedule", "adaptive_advance"].includes(intent.value)) {
        const startTime = new Date(slotStart.value), endTime = new Date(slotEnd.value);
        if (!interviewer.value.trim() || Number.isNaN(startTime.valueOf()) || Number.isNaN(endTime.valueOf()) || endTime <= startTime) throw new Error("تحقق من اسم المحاور وأن نهاية الموعد بعد بدايته.");
        body.slot = { start_at: startTime.toISOString(), end_at: endTime.toISOString(), interviewer: interviewer.value.trim() };
      }
      const review = await api("/api/graph-reviews", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (target.isConnected) { question.value = ""; offerDetails.value = ""; display(review); report(""); }
    } catch (error) { if (!isStale(error) && target.isConnected) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
    finally { start.disabled = false; open.disabled = false; }
  });
  restore.addEventListener("submit", async (event) => {
    event.preventDefault(); const id = idInput.value.trim(); if (!id) return;
    open.disabled = true; start.disabled = true; output.replaceChildren(); report("جارٍ تحميل المراجعة...");
    try { await load(id); }
    catch (error) { if (!isStale(error) && target.isConnected) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
    finally { open.disabled = false; start.disabled = false; }
  });
  try { idInput.value = sessionStorage.getItem(storageKey) || ""; } catch { /* Manual restore remains available. */ }
  if (idInput.value) load(idInput.value).catch((error) => { if (!isStale(error) && target.isConnected) report("تعذّر تحميل المراجعة السابقة. تحقق من المعرّف وحاول مجددًا.", true); });
}

async function loadStructuredResume(target, applicationId, openRubric = () => {}) {
  target.replaceChildren(element("h4", "", "الأقسام المستخرجة من السيرة"), element("p", "subtle-note", "جارٍ تحميل الأقسام..."));
  try {
    const result = await api(`/api/applications/${encodeURIComponent(applicationId)}/resume/structured`);
    if (!target.isConnected) return;
    target.replaceChildren(element("h4", "", "الأقسام المستخرجة من السيرة"),
      element("p", "subtle-note", "الاستخراج يعتمد على عناوين الأقسام الظاهرة فقط؛ النصوص المذكورة غير مُتحقق منها، وقد يُحذف أو يختلط ترتيب بعض المحتوى. عدم ظهور قسم يعني أنه غير معروف، لا أنه غير موجود."),
      element("p", "form-error", "تنقيح الهوية جزئي وقد تبقى معلومات تعريفية أو تُحذف معلومات مفيدة؛ راجع النص يدويًا."));
    const extracted = result.skill_candidates;
    if (extracted && Array.isArray(extracted.items)) {
      const details = element("details", "application-detail");
      details.append(element("summary", "", "Individual skills-section mentions"));
      details.append(element("p", "subtle-note", "These are exact snippets from the current partially redacted source, not verified skills. Unclear lists may be skipped. Missing mentions do not establish absence. Review the source and select supporting evidence in the rubric."));
      details.append(element("p", "subtle-note", `Mask review: ${result.mask_review_status}; source revision ${result.source_revision}, mask revision ${result.mask_revision}. Reload after any source or mask change.`));
      const candidates = element("ul", "evidence-list");
      for (const mention of extracted.items) candidates.append(element("li", "", `${mention.text} — source characters ${mention.start}–${mention.end}`));
      if (!extracted.items.length) details.append(element("p", "subtle-note", "No unambiguous mentions extracted; inspect the résumé manually."));
      else details.append(candidates);
      if (extracted.truncated || extracted.skipped_lines_or_fragments) details.append(element("p", "form-error", "Some text was skipped or omitted because it was ambiguous or exceeded extraction limits; inspect the source manually."));
      const review = element("button", "outline-button", "Go to rubric evidence review"); review.type = "button"; review.addEventListener("click", openRubric); details.append(review);
      target.append(details);
    }
    const labels = { skills: "المهارات", experience: "الخبرة", education: "التعليم", projects: "المشاريع", languages: "اللغات" };
    for (const [key, label] of Object.entries(labels)) {
      const section = result.sections?.[key];
      target.append(element("h4", "", label));
      if (!section?.items?.length) { target.append(element("p", "subtle-note", "غير معروف من العناوين والنص المستخرج.")); continue; }
      const list = element("ul", "evidence-list");
      for (const item of section.items) list.append(element("li", "", item.text));
      target.append(list);
      if (section.truncated) target.append(element("p", "subtle-note", "بعض محتوى هذا القسم مختصر أو لم يُعرض بسبب حدود الاستخراج."));
    }
  } catch (error) {
    if (!isStale(error) && target.isConnected) target.replaceChildren(element("h4", "", "الأقسام المستخرجة من السيرة"), element("p", "form-error", error instanceof TypeError ? "تعذّر الاتصال بالخادم لتحميل الأقسام." : error.message));
  }
}

function setupResumeMaskReview(target, applicationId, onSaved = () => {}) {
  const endpoint = `/api/applications/${encodeURIComponent(applicationId)}/resume/mask`;
  let generation = 0;
  async function load() {
    const current = ++generation;
    target.replaceChildren(element("h4", "", "Review résumé masking"), element("p", "subtle-note", "Loading review..."));
    try {
      const review = await api(endpoint);
      if (current !== generation || !target.isConnected) return;
      const points = Array.from(review.source || "");
      if (typeof review.source !== "string" || review.offset_unit !== "unicode_code_points" ||
          !/^[a-f0-9]{64}$/.test(review.source_sha256 || "") || !Number.isSafeInteger(review.source_revision) || review.source_revision < 0 ||
          !Number.isSafeInteger(review.revision) || review.revision < 0 || !["unreviewed", "reviewed", "stale"].includes(review.status) || !Array.isArray(review.spans)) throw new Error("Invalid review response; reload the application.");
      let previousEnd = 0;
      const draft = review.spans.map(({start, end}) => {
        if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < previousEnd || end <= start || end > points.length || !points.slice(start, end).join("").trim()) throw new Error("Invalid saved selections; reload the application.");
        previousEnd = end;
        return {start, end};
      });
      const heading = element("h4", "", "Review résumé masking");
      const notice = element("p", "subtle-note", "Select identifying text that the automatic rules missed. Masking can miss identifying information and is not anonymization or proof of fairness. Review the entire source and verify the resulting text. Saving a mask clears the current keyword screening; run screening again. Earlier results remain historical.");
      const state = element("p", review.status === "stale" ? "form-error" : "subtle-note", review.status === "stale" ? "The previous review is stale. Select the information to mask again before saving." : review.status === "reviewed" ? "A review was saved for this source. Changes are not saved until you confirm them." : "This source has not been reviewed for additional masking.");
      const message = element("p", "subtle-note"); message.setAttribute("aria-live", "polite");
      const label = element("label", "", "Rule-redacted source: select text to hide");
      const source = element("textarea", "resume-preview rubric-source"); source.readOnly = true; source.rows = 10; source.value = review.source; label.append(source);
      const add = element("button", "outline-button", "Add selected text to mask"); add.type = "button";
      const list = element("ol", "evidence-list");
      const previewLabel = element("label", "", "Preview after additional masking");
      const preview = element("textarea", "resume-preview rubric-source"); preview.readOnly = true; preview.rows = 10; previewLabel.append(preview);
      const save = element("button", "small-button", "Save reviewed mask"); save.type = "button";
      const reload = element("button", "outline-button", "Reload mask review"); reload.type = "button";
      const history = element("details", "application-detail"); history.append(element("summary", "", "Historical screenings before mask changes"));
      const historyBody = element("div"); history.append(historyBody);
      history.addEventListener("toggle", async () => {
        if (!history.open) return;
        historyBody.replaceChildren(element("p", "subtle-note", "Loading historical screenings..."));
        try {
          const records = await api(`${endpoint}/prior-scores`);
          if (!target.isConnected || !history.open || current !== generation) return;
          if (!Array.isArray(records.entries)) throw new Error("Invalid historical screening response.");
          historyBody.replaceChildren(element("p", "subtle-note", "These are historical results from before a mask change. They are not current screenings and may contain identifying excerpts."));
          if (!records.entries.length) historyBody.append(element("p", "subtle-note", "No prior screenings are recorded here."));
          for (const record of records.entries) {
            const item = element("details");
            item.append(element("summary", "", `Archived ${record.archived_at || "at an unknown time"} · mask revision ${record.mask_revision}`));
            item.append(element("pre", "resume-preview", JSON.stringify(record.result, null, 2))); historyBody.append(item);
          }
        } catch (error) { if (!isStale(error) && target.isConnected && current === generation) historyBody.replaceChildren(element("p", "form-error", "Could not load historical screenings. Close and reopen to retry.")); }
      });
      let locked = false;
      function report(text, error = false) { message.textContent = text; message.className = error ? "form-error" : "subtle-note"; }
      function renderDraft() {
        list.replaceChildren();
        const masked = points.slice();
        draft.forEach(({start, end}, index) => {
          for (let i = start; i < end; i++) if (masked[i] !== "\r" && masked[i] !== "\n") masked[i] = " ";
          const item = element("li", "", `Selection ${index + 1}: characters ${start}–${end}`);
          const remove = element("button", "outline-button", "Remove selection"); remove.type = "button"; remove.disabled = locked;
          remove.addEventListener("click", () => { if (!locked) { draft.splice(index, 1); renderDraft(); } });
          item.append(remove); list.append(item);
        });
        preview.value = masked.join("");
      }
      // Textarea selections use UTF-16 offsets and normalize CR/CRLF to LF.
      function sourceOffset(offset) {
        let visible = 0, index = 0;
        while (index < points.length) {
          if (visible === offset) return index;
          if (points[index] === "\r" && points[index + 1] === "\n") { visible++; index += 2; }
          else { visible += points[index] === "\r" ? 1 : points[index].length; index++; }
          if (visible > offset) return null;
        }
        return visible === offset ? index : null;
      }
      add.addEventListener("click", () => {
        if (locked) return;
        const start = sourceOffset(source.selectionStart), end = sourceOffset(source.selectionEnd);
        if (start === null || end === null || end <= start || !points.slice(start, end).join("").trim()) { report("Select nonempty text without splitting a Unicode character.", true); return; }
        if (draft.length >= 200 || draft.some((span) => start < span.end && end > span.start)) { report("Selections must not overlap; at most 200 selections are allowed.", true); return; }
        draft.push({start, end}); draft.sort((a, b) => a.start - b.start); renderDraft(); report("Selection added. Review the preview before saving.");
      });
      save.addEventListener("click", async () => {
        if (locked) return;
        locked = true; add.disabled = true; save.disabled = true; reload.disabled = true; renderDraft();
        try {
          await api(endpoint, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({source_sha256: review.source_sha256, expected_source_revision: review.source_revision, expected_revision: review.revision, spans: draft})}, {errorMessages: {409: "The résumé or mask changed. Reload this review before saving."}});
          if (current !== generation || !target.isConnected) return;
          report("Mask review saved. The previous screening is historical; reload the application and screen again before continuing.");
          reload.disabled = false;
          onSaved();
        } catch (error) {
          if (!isStale(error) && target.isConnected) report(error instanceof TypeError ? "The save could not be confirmed. Reload the review before continuing." : `${error.message} Reload the review before continuing.`, true);
          reload.disabled = false;
        }
      });
      reload.addEventListener("click", load);
      target.replaceChildren(heading, notice, state, message, label, add, list, previewLabel, save, reload, history);
      renderDraft();
    } catch (error) {
      if (!isStale(error) && current === generation && target.isConnected) {
        const retry = element("button", "outline-button", "Reload mask review"); retry.type = "button"; retry.addEventListener("click", load);
        target.replaceChildren(element("h4", "", "Review résumé masking"), element("p", "form-error", error instanceof TypeError ? "Unable to connect. Reload the review before continuing." : error.message), retry);
      }
    }
  }
  load();
}

function renderApplicationDetail(target, application, role) {
  const panel = element("div", "application-detail");
  panel.append(element("h4", "", `تفاصيل الطلب #${application.id}`));
  panel.append(element("p", "", `الحالة: ${statusLabels[application.status] || application.status}`));
  panel.append(element("p", "", `تاريخ التقديم: ${application.created_at}`));
  let candidateProgress = null;
  if (role === "candidate") {
    candidateProgress = element("p", "subtle-note", "جارٍ تحميل مرحلة الطلب...");
    candidateProgress.setAttribute("aria-live", "polite");
    panel.append(candidateProgress);
    loadCandidateProgress(candidateProgress, application.id);
    const screeningPanel = element("section", "application-detail");
    screeningPanel.setAttribute("aria-label", "Candidate screening preview");
    panel.append(screeningPanel);
    loadCandidateScreening(screeningPanel, application.id);
    const assessmentPanel = element("section", "application-detail");
    assessmentPanel.setAttribute("aria-label", "Reviewed assessment");
    panel.append(assessmentPanel);
    loadCandidateAssessment(assessmentPanel, application.id);
  }
  if (role === "candidate" && application.status === "advanced") {
    const slotsPanel = element("section", "application-detail");
    slotsPanel.setAttribute("aria-label", "اختيار موعد مقابلة");
    panel.append(slotsPanel);
    loadCandidateSlots(slotsPanel, application.id, candidateProgress);
  }
  if (role === "candidate" || role === "hr") {
    const offersPanel = element("section", "application-detail");
    offersPanel.setAttribute("aria-label", "عروض التوظيف");
    panel.append(offersPanel);
    loadApplicationOffers(offersPanel, application, role);
  }
  if (role === "hr") {
    const maskPanel = element("section", "application-detail");
    maskPanel.setAttribute("aria-label", "Review résumé masking");
    panel.append(maskPanel);
    setupResumeMaskReview(maskPanel, application.id, () => {
      if (!target.isConnected) return;
      target.replaceChildren(element("p", "subtle-note", "Reloading the application after the mask change..."));
      api(`/api/applications/${encodeURIComponent(application.id)}`).then((updated) => {
        if (target.isConnected) renderApplicationDetail(target, updated, role);
      }).catch((error) => {
        if (!isStale(error) && target.isConnected) target.replaceChildren(element("p", "form-error", "The mask was saved, but the application could not be reloaded. Close and reopen it."));
      });
    });
    const publicationPanel = element("section", "application-detail");
    publicationPanel.setAttribute("aria-label", "Candidate assessment publication");
    panel.append(publicationPanel);
    loadAssessmentPublication(publicationPanel, application.id);
    panel.append(element("p", "", `الملف: ${application.filename}`));
    if (application.extraction?.ocr_pages?.length) panel.append(element("p", "form-error", `OCR was used on pages ${application.extraction.ocr_pages.join(", ")}. Extracted text may be inaccurate; review the original document.`));
    const original = element("details", "application-detail");
    original.append(element("summary", "", "Show original extracted résumé text"), element("p", "subtle-note", "The original text can contain identifying information. Review it only when needed."), element("pre", "resume-preview", application.resume_text));
    panel.append(original);
    const duplicatesPanel = element("section", "application-detail");
    duplicatesPanel.append(element("h4", "", "السير ذات النص المتطابق"), element("p", "subtle-note", "مطابقة النص المستخرج لا تثبت التزوير ولا تتخذ قرارًا؛ راجع الطلبات يدويًا."));
    const duplicatesResult = element("div"); duplicatesResult.setAttribute("aria-live", "polite");
    const duplicatesButton = element("button", "outline-button", "فحص التطابق النصي"); duplicatesButton.type = "button";
    duplicatesButton.addEventListener("click", async () => {
      duplicatesButton.disabled = true;
      duplicatesResult.replaceChildren(element("p", "subtle-note", "جارٍ فحص النصوص..."));
      try {
        const result = await api(`/api/applications/${encodeURIComponent(application.id)}/duplicates`);
        if (!duplicatesResult.isConnected) return;
        const ids = result.matches.map((item) => item.application_id).join("، ");
        duplicatesResult.replaceChildren(element("p", "", ids ? `طلبات ذات نص مطابق: ${ids}` : "لم يظهر نص مطابق في الطلبات المفحوصة."));
        duplicatesResult.append(element("p", "subtle-note", `عدد الطلبات المفحوصة: ${result.scanned}. ${result.truncated ? "لم تُفحص جميع الطلبات." : ""}`));
      } catch (error) {
        if (!isStale(error) && duplicatesResult.isConnected) duplicatesResult.replaceChildren(element("p", "form-error", error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message));
      } finally { duplicatesButton.disabled = false; }
    });
    duplicatesPanel.append(duplicatesButton, duplicatesResult); panel.append(duplicatesPanel);
    const structurePanel = element("section", "application-detail");
    structurePanel.setAttribute("aria-label", "الأقسام المستخرجة من السيرة");
    panel.append(structurePanel);
    loadStructuredResume(structurePanel, application.id, () => rubricPanel.scrollIntoView({behavior: "smooth", block: "start"}));
    const reviewFieldsPanel = element("section", "application-detail");
    reviewFieldsPanel.setAttribute("aria-label", "مراجعة حقول السيرة");
    panel.append(reviewFieldsPanel);
    loadResumeFieldReview(reviewFieldsPanel, application.id);
    const result = element("div"); panel.append(result); renderScore(result, application.score);
    const historyPanel = element("section", "application-detail");
    historyPanel.setAttribute("aria-label", "Screening history");
    panel.append(historyPanel); setupScreeningHistory(historyPanel, application.id);
    const rubricPanel = element("section", "application-detail");
    rubricPanel.setAttribute("aria-label", "التقييم الموزون بالمراجعة البشرية");
    panel.append(rubricPanel);
    loadRubric(rubricPanel, application);
    const graphPanel = element("section", "application-detail");
    graphPanel.setAttribute("aria-label", "مراجعة LangGraph");
    panel.append(graphPanel);
    setupGraphReview(graphPanel, application);
    const interviewGuidePanel = element("section", "application-detail");
    interviewGuidePanel.setAttribute("aria-label", "Interview preparation guide");
    panel.append(interviewGuidePanel);
    loadInterviewGuide(interviewGuidePanel, application.id);
    const workflowPanel = element("section", "application-detail");
    workflowPanel.setAttribute("aria-label", "خطة التوظيف");
    panel.append(workflowPanel);
    loadWorkflow(workflowPanel, application.id, target, role);
    const assistantPanel = element("section", "application-detail");
    assistantPanel.setAttribute("aria-label", "مساعد تحضير المراجعة");
    assistantPanel.append(element("h4", "", "مساعد تحضير المراجعة"), element("p", "subtle-note", "يجمع مؤشرات من أدوات قراءة محدودة لتحضير المراجعة. قد يتصل بمزوّد ذكاء اصطناعي عند تفعيله؛ لا يتخذ قرارًا ولا يجدول مقابلة."));
    const assistantResult = element("div"); assistantResult.setAttribute("aria-live", "polite");
    const assistantButton = element("button", "outline-button", "تحضير مراجعة بمساعد الذكاء الاصطناعي"); assistantButton.type = "button";
    assistantButton.addEventListener("click", async () => {
      assistantButton.disabled = true;
      assistantResult.replaceChildren(element("p", "subtle-note", "جاري تحضير المراجعة..."));
      try {
        const response = await api(`/api/agent/${encodeURIComponent(application.id)}/review`, { method: "POST" });
        if (!assistantResult.isConnected) return;
        renderAgentReview(assistantResult, response);
      } catch (error) {
        if (!isStale(error) && assistantResult.isConnected) assistantResult.replaceChildren(element("p", "form-error", error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message));
      } finally { assistantButton.disabled = false; }
    });
    assistantPanel.append(assistantButton, assistantResult); panel.append(assistantPanel);
    const screen = element("button", "outline-button", "فحص المهارات المذكورة"); screen.type = "button";
    screen.addEventListener("click", async () => {
      screen.disabled = true;
      try { renderScore(result, await api(`/api/applications/${encodeURIComponent(application.id)}/screen`, { method: "POST" }, { errorMessages: { 422: "Screening exceeds its work limit or the job requirements are invalid. Review this application manually." } })); announce("تم تسجيل الفحص."); await loadWorkflow(workflowPanel, application.id, target, role); await loadOperations(role); }
      catch (error) { if (!isStale(error)) announce(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
      finally { screen.disabled = false; }
    });
    panel.append(screen);
    const form = element("form", "decision-form");
    const label = element("label", "", "قرار المراجع البشري");
    const select = element("select");
    const decisions = { submitted: [["advance", "نقل للمرحلة التالية"], ["reject", "رفض الطلب"]], advanced: [["reject", "رفض الطلب"]], offered: [["hire", "توظيف بعد قبول العرض"], ["reject", "رفض الطلب"]] };
    const available = decisions[application.status] || [];
    for (const [value, text] of [["", "اختر القرار"], ...available]) { const option = element("option", "", text); option.value = value; select.append(option); }
    select.required = true; label.append(select);
    const noteLabel = element("label", "", "ملاحظة القرار");
    const note = element("textarea"); note.maxLength = 1000; note.placeholder = "أضف سبب القرار أو ملاحظة للمراجعة"; noteLabel.append(note);
    const submit = element("button", "small-button", "تسجيل القرار"); submit.type = "submit";
    form.append(label, noteLabel, submit);
    if (!available.length) {
      select.disabled = true; note.disabled = true; submit.disabled = true;
      form.append(element("p", "subtle-note", "لا توجد قرارات متاحة في حالة الطلب الحالية."));
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!select.value) return;
      submit.disabled = true;
      try {
        await api(`/api/applications/${encodeURIComponent(application.id)}/decision`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: select.value, note: note.value }) });
        if (await loadRecruitment(role)) announce("تم تسجيل قرار المراجع.");
        await loadOperations(role);
      } catch (error) { if (!isStale(error)) announce(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
      finally { submit.disabled = false; }
    });
    panel.append(form);
  }
  target.replaceChildren(panel);
}

async function loadResumeFieldReview(target, applicationId) {
  target.replaceChildren(element("h4", "", "مراجعة حقول السيرة"), element("p", "subtle-note", "جارٍ التحميل..."));
  try {
    const review = await api(`/api/applications/${encodeURIComponent(applicationId)}/resume/field-review`);
    if (!target.isConnected) return;
    const title = element("h4", "", "مراجعة حقول السيرة");
    const notice = element("p", "subtle-note", "حدد مقطعًا من النص المحجوب جزئيًا، واختر القسم والحقل ورقم السجل، ثم أضفه للمراجعة. القيم اختيارات بشرية غير متحقق منها ولا تتخذ قرار توظيف.");
    const message = element("p", "subtle-note"); message.setAttribute("aria-live", "polite");
    const sourceLabel = element("label", "", "النص المحجوب جزئيًا للاختيار");
    const source = element("textarea"); source.readOnly = true; source.value = review.source; source.rows = 10; sourceLabel.append(source);
    const sectionLabel = element("label", "", "القسم"); const section = element("select");
    for (const [value, label] of [["experience", "الخبرة"], ["education", "التعليم"], ["projects", "المشاريع"]]) { const option = element("option", "", label); option.value = value; section.append(option); }
    sectionLabel.append(section);
    const fieldLabel = element("label", "", "الحقل"); const field = element("select"); fieldLabel.append(field);
    const choices = { experience: [["role", "الدور"], ["organization", "الجهة"], ["period", "الفترة"]], education: [["qualification", "المؤهل"], ["institution", "المؤسسة"], ["period", "الفترة"]], projects: [["name", "المشروع"], ["role", "الدور"], ["technologies", "التقنيات"], ["period", "الفترة"]] };
    function updateFields() { field.replaceChildren(); for (const [value, label] of choices[section.value]) { const option = element("option", "", label); option.value = value; field.append(option); } }
    section.addEventListener("change", updateFields); updateFields();
    const recordLabel = element("label", "", "رقم السجل داخل هذا القسم"); const record = element("input"); record.type = "number"; record.min = "1"; record.max = "100"; record.value = "1"; recordLabel.append(record);
    const list = element("ul", "evidence-list");
    const add = element("button", "outline-button", "إضافة المقطع المحدد"); add.type = "button";
    const save = element("button", "small-button", "حفظ مراجعة الحقول"); save.type = "button";
    const reload = element("button", "outline-button", "إعادة تحميل المراجعة"); reload.type = "button";
    let draft = (review.fields || []).map(({ record_ref, section: group, field: name, start, end }) => ({ record_ref, section: group, field: name, start, end }));
    let draftLocked = false;
    function renderDraft() {
      list.replaceChildren();
      const points = Array.from(review.source);
      draft.forEach((item, index) => {
        const li = element("li"); li.append(element("p", "", `${item.section} · ${item.record_ref} · ${item.field}: ${points.slice(item.start, item.end).join("")}`));
        const remove = element("button", "outline-button", "إزالة"); remove.type = "button"; remove.disabled = draftLocked; remove.addEventListener("click", () => { if (!draftLocked) { draft.splice(index, 1); renderDraft(); } }); li.append(remove); list.append(li);
      });
    }
    function splitsPair(index) { return index > 0 && index < source.value.length && /[\uD800-\uDBFF]/.test(source.value[index - 1]) && /[\uDC00-\uDFFF]/.test(source.value[index]); }
    add.addEventListener("click", () => {
      const a = source.selectionStart, b = source.selectionEnd, number = Number(record.value);
      if (a === b || splitsPair(a) || splitsPair(b) || !Number.isInteger(number) || number < 1 || number > 100) { message.textContent = "حدد مقطعًا صحيحًا ورقم سجل من 1 إلى 100."; message.className = "form-error"; return; }
      const start = Array.from(source.value.slice(0, a)).length, end = Array.from(source.value.slice(0, b)).length;
      const record_ref = `r-${section.value}-${number}`;
      if (draft.some((item) => item.record_ref === record_ref && item.field === field.value)) { message.textContent = "هذا الحقل موجود في السجل؛ أزله قبل استبداله."; message.className = "form-error"; return; }
      draft.push({ record_ref, section: section.value, field: field.value, start, end }); renderDraft(); message.textContent = "أضيف المقطع للمراجعة غير المحفوظة."; message.className = "subtle-note";
    });
    save.addEventListener("click", async () => {
      draftLocked = true; renderDraft(); save.disabled = true; add.disabled = true; reload.disabled = true;
      let uncertain = false;
      try {
        await api(`/api/applications/${encodeURIComponent(applicationId)}/resume/field-review`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ source_sha256: review.source_sha256, expected_source_context: review.source_context, expected_version: review.version, fields: draft }) });
        if (target.isConnected) await loadResumeFieldReview(target, applicationId);
      } catch (error) { uncertain = error instanceof TypeError; if (!isStale(error) && target.isConnected) { message.className = "form-error"; message.textContent = uncertain ? "تعذّر الاتصال؛ أعد التحميل للتحقق قبل المحاولة." : error.message; } }
      finally { draftLocked = uncertain; renderDraft(); save.disabled = uncertain; add.disabled = uncertain; reload.disabled = false; }
    });
    reload.addEventListener("click", () => loadResumeFieldReview(target, applicationId));
    target.replaceChildren(title, notice, message, sourceLabel, sectionLabel, fieldLabel, recordLabel, add, list, save, reload);
    if (review.status === "stale") { message.textContent = "تغيرت السيرة منذ المراجعة السابقة؛ أعد اختيار المقاطع قبل الحفظ."; message.className = "form-error"; }
    renderDraft();
  } catch (error) { if (!isStale(error) && target.isConnected) target.replaceChildren(element("h4", "", "مراجعة حقول السيرة"), element("p", "form-error", error instanceof TypeError ? "تعذّر الاتصال لتحميل المراجعة." : error.message)); }
}

async function loadApplicationOffers(target, application, role) {
  target.replaceChildren(element("h4", "", "عروض التوظيف"), element("p", "subtle-note", "جارٍ تحميل العروض..."));
  try {
    const offers = await api(`/api/applications/${encodeURIComponent(application.id)}/offers`);
    if (!target.isConnected) return;
    if (!Array.isArray(offers)) throw new Error("تعذّر قراءة العروض.");
    const heading = element("h4", "", "عروض التوظيف");
    const notice = element("p", "subtle-note", "تُعرض التفاصيل داخل التطبيق؛ لا يُرسل هذا الإجراء رسالة بريد. راجع التفاصيل جيدًا قبل الرد.");
    const message = element("p", "subtle-note"); message.setAttribute("aria-live", "polite");
    const controls = [];
    target.replaceChildren(heading, notice, message);
    const labels = { presented: "بانتظار رد المرشح", accepted: "قبله المرشح", declined: "رفضه المرشح", withdrawn: "تم سحبه" };
    const refresh = element("button", "outline-button", "تحديث العروض والطلب"); refresh.type = "button";
    refresh.addEventListener("click", async () => {
      refresh.disabled = true;
      try {
        if (await loadRecruitment(role)) announce("تم تحديث الطلبات. افتح تفاصيل الطلب لرؤية أحدث العروض.");
        else announce("تعذّر تحديث الطلبات. حاول مجددًا.", true);
      } catch (error) { if (!isStale(error)) announce(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
      finally { refresh.disabled = false; }
    });
    target.append(refresh);
    async function submitAction(path, body, success) {
      for (const control of controls) control.disabled = true;
      message.className = "subtle-note"; message.textContent = "جارٍ تسجيل الإجراء...";
      let uncertain = false;
      try {
        const options = { method: "POST" };
        if (body !== null) { options.headers = { "Content-Type": "application/json" }; options.body = JSON.stringify(body); }
        await api(path, options);
        if (!target.isConnected) return;
        if (await loadRecruitment(role)) announce(success);
        else { message.className = "form-error"; message.textContent = "تم تسجيل الإجراء، لكن تعذّر تحديث الطلب. استخدم زر التحديث للتحقق."; uncertain = true; }
        await loadOperations(role);
      } catch (error) {
        uncertain = error instanceof TypeError;
        if (!isStale(error) && target.isConnected) {
          message.className = "form-error";
          message.textContent = uncertain ? "تعذّر الاتصال. حدّث الطلب والعروض للتحقق قبل إعادة المحاولة." : error.message;
        }
      } finally {
        if (!uncertain) for (const control of controls) control.disabled = false;
      }
    }
    if (!offers.length) target.append(element("p", "empty-note", "لا يوجد عرض توظيف لهذا الطلب حتى الآن."));
    for (const offer of offers) {
      if (!offer || offer.application_id !== application.id || typeof offer.details !== "string") throw new Error("تعذّر قراءة تفاصيل العرض بأمان.");
      const card = element("article", "job-card");
      card.append(element("h5", "", `عرض #${offer.id}`), element("p", "", `الحالة: ${labels[offer.status] || "غير معروفة"}`), element("pre", "resume-preview", offer.details));
      if (offer.created_at) card.append(element("p", "subtle-note", `تاريخ العرض: ${displayDate(offer.created_at)}`));
      if (role === "candidate" && offer.status === "presented") {
        const accept = element("button", "small-button", "أقبل هذا العرض"); accept.type = "button";
        const decline = element("button", "outline-button", "أرفض هذا العرض"); decline.type = "button";
        accept.addEventListener("click", () => submitAction(`/api/offers/${encodeURIComponent(offer.id)}/respond`, { decision: "accepted" }, "تم تسجيل قبولك للعرض داخل التطبيق."));
        decline.addEventListener("click", () => submitAction(`/api/offers/${encodeURIComponent(offer.id)}/respond`, { decision: "declined" }, "تم تسجيل رفضك للعرض داخل التطبيق."));
        controls.push(accept, decline); card.append(accept, decline);
      }
      if (role === "hr" && offer.status === "presented") {
        const withdraw = element("button", "outline-button", "سحب هذا العرض"); withdraw.type = "button";
        withdraw.addEventListener("click", () => submitAction(`/api/offers/${encodeURIComponent(offer.id)}/withdraw`, null, "تم سحب العرض داخل التطبيق."));
        controls.push(withdraw); card.append(withdraw);
      }
      target.append(card);
    }
    if (role === "hr" && application.status === "advanced") {
      const form = element("form", "decision-form");
      const label = element("label", "", "تفاصيل عرض التوظيف للمرشح");
      const details = element("textarea"); details.required = true; details.maxLength = 4000; label.append(details);
      const create = element("button", "small-button", "تسجيل العرض للمرشح"); create.type = "submit";
      form.append(element("p", "subtle-note", "يتطلب العرض تسجيل إجراء مقابلة وانتهاء أي مقابلة جارية أو مستقبلية. لا يتم إرسال بريد."), label, create);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const value = details.value.trim(); if (!value) return;
        submitAction(`/api/applications/${encodeURIComponent(application.id)}/offers`, { details: value }, "تم تسجيل العرض داخل التطبيق.");
      });
      controls.push(details, create); target.append(form);
    }
  } catch (error) {
    if (!isStale(error) && target.isConnected) target.replaceChildren(element("h4", "", "عروض التوظيف"), element("p", "form-error", error instanceof TypeError ? "تعذّر الاتصال بالخادم لتحميل العروض." : error.message));
  }
}

function renderAssessmentComponents(target, components) {
  if (!Array.isArray(components)) return;
  const labels = {mandatory: "Mandatory skills", preferred: "Preferred skills", experience: "Experience", projects: "Projects", education: "Education"};
  const list = element("ul", "evidence-list");
  for (const part of components) {
    const counts = part.counts || {};
    list.append(element("li", "", `${labels[part.category] || part.category}: ${part.minimum_points}–${part.maximum_points} of ${part.weight} points; reviewed weight ${part.covered_weight}. Met ${counts.met ?? 0}, missing evidence ${counts.missing ?? 0}, unknown ${counts.unknown ?? 0}.${part.unconfigured ? " No criteria configured." : ""}`));
  }
  target.append(element("h5", "", "Score breakdown"), list, element("p", "subtle-note", "Ranges reflect reviewed evidence and uncertainty; they are not verified ability or a hiring decision."));
}

function renderCandidateAssessment(target, assessment) {
  const bounds = assessment.bounds;
  target.append(element("p", "result-badge", `Reviewed score range: ${bounds.minimum}–${bounds.maximum} / 100 · coverage ${bounds.coverage_percent}%`),
    element("p", "subtle-note", assessment.notice));
  renderAssessmentComponents(target, assessment.components);
  const list = element("ul", "evidence-list");
  const status = { met: "Evidence reviewed", missing: "Not established from available evidence", unknown: "Not yet established" };
  for (const item of assessment.criteria) {
    const row = element("li");
    row.append(element("strong", "", item.criterion), element("p", "", status[item.status] || "Not yet established"));
    for (const excerpt of item.evidence) row.append(element("p", "", excerpt));
    list.append(row);
  }
  target.append(list);
}

async function loadCandidateAssessment(target, applicationId) {
  target.replaceChildren(element("h4", "", "Reviewed assessment"), element("p", "subtle-note", "Loading..."));
  try {
    const result = await api(`/api/applications/${encodeURIComponent(applicationId)}/candidate-assessment`);
    if (!target.isConnected) return;
    target.replaceChildren(element("h4", "", "Reviewed assessment"));
    if (result.status === "current") renderCandidateAssessment(target, result);
    else target.append(element("p", "subtle-note", "No current reviewed assessment has been shared with you."));
  } catch (error) {
    if (!isStale(error) && target.isConnected) target.replaceChildren(element("h4", "", "Reviewed assessment"), element("p", "form-error", "Unable to load the reviewed assessment."));
  }
}

async function loadAssessmentPublication(target, applicationId) {
  const endpoint = `/api/applications/${encodeURIComponent(applicationId)}/rubric/candidate-publication`;
  target.replaceChildren(element("h4", "", "Share reviewed assessment"), element("p", "subtle-note", "Loading..."));
  try {
    const result = await api(endpoint);
    if (!target.isConnected) return;
    target.replaceChildren(element("h4", "", "Share reviewed assessment"));
    const refresh = element("button", "outline-button", "Refresh assessment preview"); refresh.type = "button";
    refresh.addEventListener("click", () => loadAssessmentPublication(target, applicationId));
    target.append(refresh);
    if (result.preview) {
      target.append(element("p", "subtle-note", "Review everything below before sharing. The candidate will see these criteria, judgments, excerpts and score range."));
      renderCandidateAssessment(target, result.preview);
      const publish = element("button", "outline-button", result.published ? "Shared with candidate" : "Share this assessment with candidate");
      publish.type = "button"; publish.disabled = result.published;
      publish.addEventListener("click", async () => {
        publish.disabled = true;
        try {
          await api(endpoint, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ assessment_id: result.assessment_id, snapshot_digest: result.snapshot_digest }) });
          if (target.isConnected) await loadAssessmentPublication(target, applicationId);
        } catch (error) {
          if (!isStale(error) && target.isConnected) target.append(element("p", "form-error", "Could not share this assessment. Reload the preview and review it again."));
        } finally { publish.disabled = false; }
      });
      target.append(publish);
    } else target.append(element("p", "subtle-note", "A current reviewed assessment is required before sharing."));
    if (result.has_publication) {
      const withdraw = element("button", "outline-button", "Withdraw shared assessment"); withdraw.type = "button";
      withdraw.addEventListener("click", async () => {
        withdraw.disabled = true;
        try { await api(endpoint, { method: "DELETE" }); if (target.isConnected) await loadAssessmentPublication(target, applicationId); }
        catch (error) { if (!isStale(error) && target.isConnected) target.append(element("p", "form-error", "Could not withdraw the assessment.")); }
        finally { withdraw.disabled = false; }
      });
      target.append(withdraw);
    }
  } catch (error) {
    if (!isStale(error) && target.isConnected) target.replaceChildren(element("h4", "", "Share reviewed assessment"), element("p", "form-error", "Unable to load the assessment preview."));
  }
}

async function loadCandidateScreening(target, applicationId) {
  target.replaceChildren(element("h4", "", "Current screening preview"), element("p", "subtle-note", "Loading..."));
  try {
    const result = await api(`/api/applications/${encodeURIComponent(applicationId)}/candidate-screening`);
    if (!target.isConnected) return;
    target.replaceChildren(element("h4", "", "Current screening preview"),
      element("p", "subtle-note", "A keyword-based preview of the current resume and job requirements. It does not verify experience, represent HR's saved assessment, or make a hiring decision."),
      element("p", "result-badge", `Required skill mentions: ${result.score} / 100`));
    for (const [key, label] of [["required", "Required skills"], ["preferred", "Preferred skills"]]) {
      const list = element("ul", "evidence-list");
      for (const skill of result[key] || []) {
        const item = element("li");
        item.append(element("strong", "", skill.skill), element("p", "", skill.mentioned ? "Mention found; review context." : "No reliable mention found; this does not prove a lack of skill."));
        for (const excerpt of skill.evidence || []) item.append(element("p", "", excerpt));
        list.append(item);
      }
      target.append(element("h5", "", label), list);
    }
  } catch (error) {
    if (!isStale(error) && target.isConnected) target.replaceChildren(element("h4", "", "Current screening preview"), element("p", "form-error", "Unable to load the screening preview. The preview supports resumes up to 4,000 characters and jobs with up to 20 skills of 80 characters each; ask HR to review larger inputs."));
  }
}

async function loadCandidateProgress(target, applicationId) {
  const labels = { applied: "تم التقديم", screened: "تم فحص الطلب", shortlisted: "تمت الموافقة على الانتقال للمرحلة التالية", interview_scheduled: "تم تسجيل موعد مقابلة", interviewed: "تم تسجيل إجراء المقابلة", offered: "عرض توظيف", hired: "تم التوظيف", rejected: "انتهى الطلب بالرفض" };
  try {
    const progress = await api(`/api/workflows/${encodeURIComponent(applicationId)}/progress`);
    if (target.isConnected) target.textContent = `مرحلة الطلب: ${labels[progress.stage] || "غير متاحة"}`;
  } catch (error) {
    if (!isStale(error) && target.isConnected) target.textContent = "تعذّر تحميل مرحلة الطلب. حاول مجددًا.";
  }
}

async function loadCandidateSlots(target, applicationId, progressTarget) {
  target.replaceChildren(element("p", "subtle-note", "جاري تحميل المواعيد المتاحة..."));
  try {
    const slots = await api(`/api/slots?${new URLSearchParams({ application_id: applicationId })}`);
    if (!target.isConnected) return;
    target.replaceChildren(
      element("h4", "", "اختيار موعد مقابلة"),
      element("p", "subtle-note", "المواعيد المعروضة قد تتغير؛ حدّث القائمة قبل الحجز. الحجز محلي فقط، ولن تُرسل دعوة أو رسالة بريد إلكتروني.")
    );
    const refresh = element("button", "outline-button", "تحديث المواعيد"); refresh.type = "button";
    refresh.addEventListener("click", () => loadCandidateSlots(target, applicationId, progressTarget));
    target.append(refresh);
    if (!Array.isArray(slots)) throw new Error("تعذّر قراءة المواعيد المتاحة.");
    if (!slots.length) target.append(element("p", "empty-note", "لا توجد مواعيد متاحة حاليًا، أو لديك مقابلة مجدولة لهذا الطلب."));
    for (const slot of slots) {
      const card = element("article", "job-card");
      card.append(element("p", "", `من ${displayDate(slot.start_at)} إلى ${displayDate(slot.end_at)}`), element("p", "", `المحاور: ${slot.interviewer}`));
      const book = element("button", "small-button", "حجز هذا الموعد"); book.type = "button";
      book.addEventListener("click", async () => {
        book.disabled = true;
        try {
          await api(`/api/slots/${encodeURIComponent(slot.id)}/book`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ application_id: applicationId })
          });
          if (target.isConnected) {
            await loadCandidateSlots(target, applicationId, progressTarget);
            if (progressTarget?.isConnected) await loadCandidateProgress(progressTarget, applicationId);
            announce("تم حجز المقابلة محليًا؛ لن تُرسل رسالة بريد إلكتروني.");
            await loadOperations("candidate");
          }
        } catch (error) {
          if (!isStale(error) && target.isConnected) announce(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true);
        } finally { book.disabled = false; }
      });
      card.append(book); target.append(card);
    }
  } catch (error) {
    if (!isStale(error) && target.isConnected) target.replaceChildren(element("p", "form-error", error instanceof TypeError ? "تعذّر الاتصال بالخادم لتحميل المواعيد." : error.message));
  }
}

function renderAgentReview(target, response) {
  const messages = {
    disabled: "المساعد غير مفعّل على الخادم.",
    unavailable: "خدمة المساعد غير متاحة حاليًا.",
    rejected: "توقفت المراجعة لأن طلب المساعد لم يكن مدعومًا.",
    limit_reached: "توقفت المراجعة عند حد استخدام الأدوات."
  };
  target.replaceChildren();
  if (response.status !== "ok") {
    target.append(element("p", "form-error", messages[response.status] || "تعذّر إكمال المراجعة."));
    return;
  }
  target.append(element("p", "", response.answer || "اكتمل تحضير المؤشرات."));
  target.append(element("p", "subtle-note", "النتيجة غير متحقق منها؛ راجع الأدلة بنفسك. لم يُتخذ أي قرار ولم يُحجز موعد."));
  const labels = { job_requirements: "متطلبات الوظيفة", workflow_status: "حالة سير العمل", screening_evidence: "مؤشرات فحص السيرة" };
  if (Array.isArray(response.tool_trace) && response.tool_trace.length) {
    target.append(element("h4", "", "الأدوات المستخدمة"));
    const list = element("ul", "evidence-list");
    for (const name of response.tool_trace) list.append(element("li", "", labels[name] || name));
    target.append(list);
  }
  const evidence = response.evidence || {};
  if (evidence.job_requirements) {
    target.append(element("p", "", `المهارات المطلوبة: ${(evidence.job_requirements.required_skills || []).join("، ")}`));
  }
  if (evidence.workflow_status) {
    target.append(element("p", "", `الخطوة التالية: ${workflowLabels[evidence.workflow_status.next_step] || evidence.workflow_status.next_step}`));
  }
  if (evidence.screening_evidence) {
    const screening = evidence.screening_evidence;
    if (!screening.screened) target.append(element("p", "subtle-note", "لم تُفحص السيرة بعد."));
    else {
      const list = element("ul", "evidence-list");
      for (const item of screening.required || []) list.append(element("li", "", `${item.skill}: ${item.mentioned ? "مذكورة" : "لم يُعثر على ذكرها"}`));
      target.append(list);
    }
  }
}

const workflowLabels = {
  screen: "فحص السيرة",
  human_review: "مراجعة بشرية",
  schedule_interview: "جدولة مقابلة",
  await_interview: "Wait for the scheduled interview",
  confirm_interview: "Confirm whether the interview took place",
  review_interview: "Review the interview details",
  prepare_offer: "Review the interview and prepare an offer",
  await_offer_response: "Wait for the candidate to respond to the offer",
  finalize_hire: "Review the accepted offer and record the hiring decision",
  review_offer: "Review the offer status",
  human_decision: "القرار البشري النهائي",
  complete: "اكتملت المعالجة"
};

async function loadWorkflow(target, applicationId, detailTarget, role) {
  target.replaceChildren(element("p", "subtle-note", "جاري تحميل خطة الطلب..."));
  try {
    const state = await api(`/api/workflows/${encodeURIComponent(applicationId)}`);
    if (!target.isConnected) return;
    renderWorkflow(target, state, applicationId, detailTarget, role);
  } catch (error) {
    if (!isStale(error) && target.isConnected) target.replaceChildren(element("p", "form-error", error instanceof TypeError ? "تعذّر الاتصال بالخادم لتحميل الخطة." : error.message));
  }
}

function renderWorkflow(target, state, applicationId, detailTarget, role) {
  const heading = element("h4", "", "خطة متابعة الطلب");
  const note = element("p", "subtle-note", "The next step reflects recorded application, interview, and offer status. Some steps wait for the candidate; HR decisions remain manual.");
  const plan = element("ol", "evidence-list");
  for (const step of state.plan || []) plan.append(element("li", "", workflowLabels[step] || step));
  const next = element("p", "result-badge", `الخطوة التالية: ${workflowLabels[state.next_step] || state.next_step}`);
  target.replaceChildren(heading, note, plan, next);
  if (state.next_step === "screen") {
    const run = element("button", "small-button", "تنفيذ فحص السيرة"); run.type = "button";
    run.addEventListener("click", async () => {
      run.disabled = true;
      try {
        await api(`/api/workflows/${encodeURIComponent(applicationId)}/run`, { method: "POST" });
        const updated = await api(`/api/applications/${encodeURIComponent(applicationId)}`);
        if (detailTarget.isConnected) renderApplicationDetail(detailTarget, updated, role);
        announce("تم تنفيذ الفحص وتحديث خطة الطلب.");
        await loadOperations(role);
      } catch (error) {
        if (!isStale(error)) announce(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true);
      } finally { run.disabled = false; }
    });
    target.append(run);
  } else if (state.next_step === "await_interview") {
    target.append(element("p", "subtle-note", "The interview is scheduled; wait until it ends before confirming completion."));
  } else if (state.next_step === "await_offer_response") {
    target.append(element("p", "subtle-note", "The offer has been presented; wait for the candidate to respond."));
  } else if (state.next_step !== "complete") {
    target.append(element("p", "subtle-note", "هذه الخطوة تحتاج إجراءً من موظف الموارد البشرية."));
  }
}

function renderInterviewGuide(target, guide) {
  target.append(element("h4", "", "Interview preparation guide"));
  if (guide.notice) target.append(element("p", "subtle-note", guide.notice));
  if (guide.status !== "current") {
    target.append(element("p", "subtle-note", "A current reviewed assessment is required for a guide."));
    return;
  }
  const list = element("ol", "evidence-list");
  for (const question of guide.questions || []) {
    const item = element("li");
    item.append(element("strong", "", question.criterion), element("p", "", question.question));
    if (question.follow_up) item.append(element("p", "subtle-note", question.follow_up));
    if (question.evidence_refs?.length) item.append(element("p", "subtle-note", `Reviewed evidence references: ${question.evidence_refs.join(", ")}`));
    if (question.caution_types?.length) item.append(element("p", "subtle-note", `Text cues for review: ${question.caution_types.join(", ")}`));
    list.append(item);
  }
  if (list.childNodes.length) target.append(list); else target.append(element("p", "subtle-note", "No questions are available for the current assessment."));
  if (guide.omitted_count) target.append(element("p", "subtle-note", `${guide.omitted_count} criteria were omitted; review the job criteria separately.`));
}

function loadInterviewGuide(target, applicationId) {
  const button = element("button", "outline-button", "Load interview preparation guide"); button.type = "button";
  const result = element("div"); result.setAttribute("aria-live", "polite");
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const guide = await api(`/api/applications/${encodeURIComponent(applicationId)}/interview-guide`);
      if (result.isConnected) { result.replaceChildren(); renderInterviewGuide(result, guide); }
    } catch (error) { if (!isStale(error) && result.isConnected) result.replaceChildren(element("p", "form-error", "Unable to load interview guide.")); }
    finally { button.disabled = false; }
  });
  target.replaceChildren(button, result);
}

function setupScreeningHistory(target, applicationId) {
  const heading = element("h4", "", "Screening history");
  const note = element("p", "subtle-note", "HR-only snapshots may contain identifying résumé excerpts and remain in the local database until it is removed. Redaction is partial. Older screenings may not have a retained result.");
  const list = element("div");
  const button = element("button", "outline-button", "Load screening history"); button.type = "button";
  const refresh = element("button", "outline-button", "Refresh history"); refresh.type = "button";
  let cursor = null;
  refresh.addEventListener("click", () => {
    cursor = null; list.replaceChildren(); button.hidden = false; button.disabled = false;
    button.textContent = "Load screening history"; button.click();
  });
  button.addEventListener("click", async () => {
    button.disabled = true; refresh.disabled = true;
    try {
      const query = cursor === null ? "" : `?before=${encodeURIComponent(cursor)}`;
      const page = await api(`/api/applications/${encodeURIComponent(applicationId)}/screening-history${query}`);
      if (!target.isConnected) return;
      for (const entry of page.entries) {
        const record = element("section", "application-detail");
        record.append(element("p", "", `Screening #${entry.audit_event_id} · ${displayDate(entry.created_at)} · reviewer #${entry.actor_id}`));
        if (entry.status === "available" && entry.result) {
          record.append(element("p", "result-badge", `Required skill mentions: ${entry.result.score}%`));
          record.append(element("p", "subtle-note", `Method: keyword mentions · Evidence status: ${entry.result.evidence_status || "unknown"}`));
          for (const [label, items] of [["Required skills", entry.result.required], ["Preferred skills", entry.result.preferred]]) {
            const evidence = element("ul", "evidence-list");
            for (const item of items || []) {
              const row = element("li", "", `${item.skill}: ${item.mentioned ? "mentioned" : "not found"}`);
              for (const excerpt of item.evidence || []) row.append(element("p", "", String(excerpt)));
              evidence.append(row);
            }
            record.append(element("h5", "", label), evidence);
          }
          const details = element("details");
          details.append(element("summary", "", "Show complete retained result"), element("pre", "resume-preview", JSON.stringify(entry.result, null, 2)));
          record.append(details);
        } else record.append(element("p", "subtle-note", entry.status === "integrity_error" ? "The retained result could not be verified." : "No historical result was retained for this screening."));
        list.append(record);
      }
      if (!page.entries.length && cursor === null) list.append(element("p", "subtle-note", "No screening history is available."));
      cursor = page.next_before;
      if (cursor === null) button.hidden = true; else { button.textContent = "Load older screenings"; button.disabled = false; }
    } catch (error) {
      if (!isStale(error) && target.isConnected) list.append(element("p", "form-error", "Unable to load screening history."));
      button.disabled = false;
    } finally { refresh.disabled = false; }
  });
  target.replaceChildren(heading, note, button, refresh, list);
}

function renderScore(target, score) {
  target.replaceChildren();
  if (!score) { target.append(element("p", "subtle-note", "لم يتم فحص هذا الطلب بعد.")); return; }
  target.append(element("p", "result-badge", `المهارات المطلوبة المذكورة: ${score.score}%`));
  target.append(element("p", "subtle-note", "الفحص يعتمد على ذكر الكلمات في النص المستخرج، ولا يثبت الخبرة أو يتخذ قرار توظيف."));
  if (score.blind_screening?.requires_review) {
    const warning = element("p", "form-error", "تنبيه: إخفاء بيانات الهوية آلي ومحدود، وقد يترك بيانات تعريفية أو يحذف أدلة مفيدة. راجع النص والنتيجة يدويًا؛ هذا ليس ضمانًا لإخفاء الهوية أو منع التحيز.");
    warning.setAttribute("role", "alert");
    target.append(warning);
  }
  if (score.evidence_status === "insufficient") target.append(element("p", "subtle-note", "الأدلة غير كافية؛ غياب الذكر لا يثبت غياب المهارة."));
  const list = element("ul", "evidence-list");
  for (const entry of score.required || []) {
    const item = element("li", "", `${entry.skill}: ${entry.mentioned ? "مذكورة" : "لم يُعثر على ذكرها"}`);
    if (Array.isArray(entry.evidence) && entry.evidence.length) {
      const quotes = element("ul", "evidence-list");
      for (const quote of entry.evidence) quotes.append(element("li", "", String(quote)));
      item.append(quotes);
    }
    list.append(item);
  }
  target.append(list);
}

function knowledgeJobSelector(id, allLabel) {
  const label = element("label", "", "الوظيفة (اختياري)");
  const select = element("select"); select.id = id; label.htmlFor = id;
  const all = element("option", "", allLabel); all.value = ""; select.append(all);
  const status = element("span", "subtle-note", "جارٍ تحميل الوظائف...");
  select.disabled = true;
  label.append(select, status);
  api("/api/jobs").then((jobs) => {
    if (!Array.isArray(jobs)) throw new Error("Invalid job list");
    for (const job of jobs) {
      if (typeof job.id !== "string" || typeof job.title !== "string") continue;
      const option = element("option", "", `${job.title} (${job.id})`); option.value = job.id; select.append(option);
    }
    select.disabled = false;
    status.textContent = jobs.length ? "" : "لا توجد وظائف للاختيار.";
  }).catch((error) => {
    if (!isStale(error)) status.textContent = "تعذّر تحميل الوظائف؛ يمكنك المتابعة دون اختيار وظيفة أو إعادة تحميل الصفحة.";
  });
  return { label, select };
}

function setupKnowledgeDocuments() {
  const sectionNode = element("section", "knowledge-section");
  sectionNode.setAttribute("aria-labelledby", "document-management-title");
  const heading = element("div", "section-heading");
  const title = element("h2", "", "إدارة مستندات المعرفة");
  title.id = "document-management-title";
  heading.append(title);
  const panel = element("div", "knowledge-panel");
  panel.append(element("p", "", "أضف سياسات ووصف وظائف تجريبية وحدد من يمكنه استرجاع كل مستند. عند تفعيل الإجابات التوليدية، قد تُرسل المقاطع المسترجعة والسؤال إلى مزوّد الذكاء الاصطناعي؛ استخدم بيانات صناعية في النسخة المحلية."));
  const message = element("p", "inline-message");
  message.setAttribute("role", "status");
  message.hidden = true;
  const list = element("div", "data-list");
  list.setAttribute("aria-live", "polite");

  function report(text, isError = false) {
    message.textContent = text;
    message.classList.toggle("error", isError);
    message.hidden = !text;
  }

  async function refresh() {
    const documents = await api("/api/knowledge/documents");
    if (!Array.isArray(documents)) throw new Error("تعذّر قراءة قائمة المستندات.");
    const labels = { public: "المرشح والموظف وHR", staff: "الموظف وHR", hr: "HR فقط" };
    const collections = { legacy_unclassified: "غير مصنّف", handbook: "دليل الموظفين والسياسات", job_description: "وصف وظيفة", past_hire_patterns: "أنماط توظيف سابقة" };
    const rows = [];
    for (const document of documents) {
      const card = element("article", "job-card");
      const effectiveAudience = document.collection === "past_hire_patterns" ? "hr" : document.audience;
      card.append(element("h4", "", document.title), element("p", "", `النوع: ${collections[document.collection] || "غير مصنّف"}`), element("p", "", `الصلاحية: ${labels[effectiveAudience] || "غير معروفة"}`));
      if (document.job_id) card.append(element("p", "", `معرّف الوظيفة: ${document.job_id}`));
      if (document.filename) card.append(element("p", "", `الملف: ${document.filename}`));
      if (document.active === true) {
        card.append(element("p", "", "الحالة: نشط في البحث"));
        const deactivate = element("button", "outline-button", "إيقاف المستند عن البحث");
        deactivate.type = "button";
        deactivate.addEventListener("click", async () => {
          deactivate.disabled = true;
          try {
            await api(`/api/knowledge/documents/${encodeURIComponent(document.id)}/deactivate`, { method: "POST" });
            report("أُوقف المستند عن البحث مع الاحتفاظ بسجله.");
            try { await refresh(); }
            catch (error) { if (!isStale(error)) report("أُوقف المستند، لكن تعذّر تحديث القائمة. أعد تحميل الصفحة.", true); }
          } catch (error) { if (!isStale(error)) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
          finally { deactivate.disabled = false; }
        });
        card.append(deactivate);
      } else if (document.active === false) card.append(element("p", "", "الحالة: متوقف عن البحث"));
      rows.push(card);
    }
    if (!rows.length) rows.push(element("p", "empty-note", "لا توجد مستندات مضافة بعد."));
    list.replaceChildren(...rows);
  }

  function commonFields(form, prefix) {
    const titleLabel = element("label", "", "عنوان المستند");
    const titleInput = element("input");
    titleInput.id = `${prefix}-title`;
    titleInput.type = "text"; titleInput.maxLength = 200; titleInput.required = true;
    titleLabel.htmlFor = titleInput.id; titleLabel.append(titleInput);
    const audienceLabel = element("label", "", "من يمكنه استرجاع المستند؟");
    const audience = element("select");
    audience.id = `${prefix}-audience`; audience.required = true;
    audienceLabel.htmlFor = audience.id;
    for (const [value, label] of [["", "اختر الصلاحية"], ["public", "المرشح والموظف وHR"], ["staff", "الموظف وHR"], ["hr", "HR فقط"]]) {
      const option = element("option", "", label); option.value = value; audience.append(option);
    }
    audienceLabel.append(audience);
    const collectionLabel = element("label", "", "نوع المستند");
    const collection = element("select"); collection.id = `${prefix}-collection`;
    collectionLabel.htmlFor = collection.id;
    for (const [value, label] of [["legacy_unclassified", "غير مصنّف"], ["handbook", "دليل الموظفين والسياسات"], ["job_description", "وصف وظيفة"], ["past_hire_patterns", "أنماط توظيف سابقة — HR فقط"]]) {
      const option = element("option", "", label); option.value = value; collection.append(option);
    }
    collectionLabel.append(collection);
    collection.addEventListener("change", () => {
      if (collection.value === "past_hire_patterns") audience.value = "hr";
      for (const option of audience.options) option.disabled = collection.value === "past_hire_patterns" && option.value !== "hr";
    });
    const job = knowledgeJobSelector(`${prefix}-job`, "دون ربط بوظيفة محددة");
    form.append(titleLabel, collectionLabel, job.label, audienceLabel, element("p", "subtle-note", "أنماط التوظيف للـHR فقط؛ استخدم أمثلة صناعية ولا ترفع بيانات متقدمين حقيقيين في هذه النسخة."));
    return { titleInput, audience, collection, job: job.select };
  }

  const textForm = element("form", "decision-form");
  textForm.setAttribute("aria-label", "إضافة مستند نصي");
  textForm.append(element("h3", "", "إضافة نص"));
  const textFields = commonFields(textForm, "knowledge-text");
  const textLabel = element("label", "", "النص");
  const text = element("textarea");
  text.id = "knowledge-document-text"; text.required = true; text.maxLength = 200000;
  textLabel.htmlFor = text.id; textLabel.append(text);
  const textButton = element("button", "small-button", "حفظ النص"); textButton.type = "submit";
  textForm.append(textLabel, textButton);
  textForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const documentTitle = textFields.titleInput.value.trim();
    const documentText = text.value.trim();
    if (!documentTitle || !documentText || !textFields.audience.value) { report("أكمل العنوان والنص والصلاحية.", true); return; }
    textButton.disabled = true;
    try {
      const payload = { title: documentTitle, audience: textFields.audience.value, collection: textFields.collection.value, text: documentText };
      if (textFields.job.value) payload.job_id = textFields.job.value;
      await api("/api/knowledge/documents/text", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      textForm.reset();
      textFields.collection.dispatchEvent(new Event("change"));
      textFields.audience.value = "";
      report("حُفظ المستند النصي.");
      try { await refresh(); }
      catch (error) { if (!isStale(error)) report("حُفظ النص، لكن تعذّر تحديث قائمة المستندات. أعد تحميل الصفحة.", true); }
    } catch (error) { if (!isStale(error)) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
    finally { textButton.disabled = false; }
  });

  const fileForm = element("form", "decision-form");
  fileForm.setAttribute("aria-label", "رفع ملف مستند");
  fileForm.append(element("h3", "", "رفع ملف"));
  const fileFields = commonFields(fileForm, "knowledge-file");
  const fileLabel = element("label", "", "PDF نصي أو DOCX حتى 5 ميجابايت");
  const fileInput = element("input");
  fileInput.id = "knowledge-document-file"; fileInput.type = "file"; fileInput.accept = ".pdf,.docx"; fileInput.required = true;
  fileLabel.htmlFor = fileInput.id; fileLabel.append(fileInput);
  const fileOcrLabel = element("label", "", "Optional OCR language for scanned PDF pages. OCR may miss or alter text; review the indexed result. English and Arabic language data must be installed for the bilingual option.");
  const fileOcr = element("select");
  for (const [value, labelText] of [["", "No OCR"], ["eng", "English"], ["eng+ara", "English and Arabic"]]) { const option = element("option", "", labelText); option.value = value; fileOcr.append(option); }
  fileOcrLabel.append(fileOcr);
  const sectionHelp = element("p", "subtle-note", "Optional: preview a PDF and select heading lines to keep policy sections separate. Verify extraction and reading order against the original.");
  const previewButton = element("button", "outline-button", "Preview PDF headings"); previewButton.type = "button";
  const previewResult = element("div", "application-detail"); previewResult.setAttribute("aria-live", "polite");
  let previewState = null;
  let previewGeneration = 0;
  function clearSectionPreview() { previewGeneration++; previewState = null; previewResult.replaceChildren(); }
  fileInput.addEventListener("change", clearSectionPreview);
  fileOcr.addEventListener("change", clearSectionPreview);
  const fileButton = element("button", "small-button", "رفع المستند"); fileButton.type = "submit";
  fileForm.append(fileLabel, fileOcrLabel, sectionHelp, previewButton, previewResult, fileButton);
  previewButton.addEventListener("click", async () => {
    const file = fileInput.files?.[0];
    if (!file || !file.name.toLowerCase().endsWith(".pdf")) { report("Choose a PDF before previewing headings.", true); return; }
    if (file.size > 5 * 1024 * 1024) { report("The file exceeds 5 MiB.", true); return; }
    clearSectionPreview();
    const generation = previewGeneration;
    const language = fileOcr.value;
    previewButton.disabled = true; fileButton.disabled = true; fileInput.disabled = true; fileOcr.disabled = true;
    previewResult.replaceChildren(element("p", "subtle-note", "Extracting a preview. This does not save the document."));
    try {
      const params = new URLSearchParams({filename: file.name});
      if (language) { params.set("ocr", "true"); params.set("ocr_language", language); }
      const preview = await api(`/api/knowledge/pdf-section-preview?${params}`, {method: "POST", headers: {"Content-Type": "application/octet-stream"}, body: file});
      if (generation !== previewGeneration || !previewResult.isConnected) return;
      if (!/^[a-f0-9]{64}$/.test(preview.source_sha256 || "") || !Array.isArray(preview.pages) ||
          preview.pages.some((page, index) => page.page !== index + 1 || !Array.isArray(page.lines) || page.lines.some((line) => typeof line !== "string")) ||
          preview.pages.reduce((count, page) => count + page.lines.length, 0) > 2000) {
        throw new Error("This preview cannot be displayed safely. Upload without selected headings or use a smaller PDF.");
      }
      const controls = [];
      const wrapper = element("div");
      wrapper.append(element("p", "subtle-note", "Select up to 100 heading lines. A selected section continues across pages until the next heading. Similar or repeated headings can be selected separately."));
      for (const page of preview.pages) {
        const group = element("details");
        group.append(element("summary", "", `Page ${page.page}${preview.ocr_pages?.includes(page.page) ? " · OCR text; verify original" : ""}`));
        const rows = element("div");
        page.lines.forEach((line, index) => {
          const label = element("label", "", `Line ${index + 1}: ${line}`);
          const checkbox = element("input"); checkbox.type = "checkbox";
          checkbox.disabled = !line.trim() || line.trim().length > 200 || /[\u0000-\u001f]/.test(line.trim());
          label.prepend(checkbox); rows.append(label);
          controls.push({checkbox, page: page.page, line: index});
        });
        group.append(rows); wrapper.append(group);
      }
      previewState = {fingerprint: preview.source_sha256, file, language, controls};
      previewResult.replaceChildren(wrapper);
      report("Preview ready. Select heading lines or upload normally without selections.");
    } catch (error) {
      if (!isStale(error) && previewResult.isConnected && generation === previewGeneration) {
        previewState = null;
        previewResult.replaceChildren(element("p", "form-error", error instanceof TypeError ? "Could not connect to the server." : error.message));
      }
    } finally {
      previewButton.disabled = false; fileButton.disabled = false; fileInput.disabled = false; fileOcr.disabled = false;
    }
  });
  fileForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = fileInput.files?.[0];
    const documentTitle = fileFields.titleInput.value.trim();
    if (!file || !documentTitle || !fileFields.audience.value) { report("أكمل العنوان والملف والصلاحية.", true); return; }
    if (file.size > 5 * 1024 * 1024) { report("حجم الملف أكبر من 5 ميجابايت.", true); return; }
    const marks = previewState?.controls.filter((item) => item.checkbox.checked).map(({page, line}) => ({page, line})) || [];
    if (marks.length > 100) { report("Select at most 100 heading lines.", true); return; }
    if (marks.length && (previewState.file !== file || previewState.language !== fileOcr.value)) {
      clearSectionPreview(); report("The file or OCR option changed. Preview again.", true); return;
    }
    fileButton.disabled = true; previewButton.disabled = true; fileInput.disabled = true; fileOcr.disabled = true;
    try {
      const params = new URLSearchParams({ title: documentTitle, audience: fileFields.audience.value, collection: fileFields.collection.value, filename: file.name });
      if (fileFields.job.value) params.set("job_id", fileFields.job.value);
      if (fileOcr.value) { params.set("ocr", "true"); params.set("ocr_language", fileOcr.value); }
      if (marks.length) { params.set("pdf_selected_headings", JSON.stringify(marks)); params.set("pdf_preview_sha256", previewState.fingerprint); }
      const uploaded = await api(`/api/knowledge/documents?${params}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file }, {errorMessages: {409: "The file or extracted text changed. Preview and select headings again."}});
      fileForm.reset();
      clearSectionPreview();
      fileFields.collection.dispatchEvent(new Event("change"));
      fileFields.audience.value = "";
      const pages = Number.isInteger(uploaded.page_count)
        ? ` Extracted text from ${uploaded.pages_with_text} of ${uploaded.page_count} pages.${uploaded.ocr_pages?.length ? ` OCR used on pages ${uploaded.ocr_pages.join(", ")}; review the text for errors.` : ""}` : "";
      report(`رُفع المستند وحُفظ نصه.${pages}${uploaded.section_notice ? ` ${uploaded.section_notice}` : ""}`);
      try { await refresh(); }
      catch (error) { if (!isStale(error)) report("رُفع المستند، لكن تعذّر تحديث القائمة. أعد تحميل الصفحة.", true); }
    } catch (error) { if (!isStale(error)) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
    finally { fileButton.disabled = false; previewButton.disabled = false; fileInput.disabled = false; fileOcr.disabled = false; }
  });

  panel.append(message, textForm, fileForm, element("h3", "", "المستندات المضافة"), list);
  sectionNode.append(heading, panel);
  document.getElementById("knowledge-form").closest("section").after(sectionNode);
  refresh().catch((error) => { if (!isStale(error)) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); });
}

function knowledgeSourceLabel(source) {
  const page = Number.isInteger(source.page) && source.page > 0 ? ` · صفحة ${source.page}` : "";
  const section = typeof source.section === "string" && source.section.trim() ? ` · القسم: ${source.section.trim()}` : "";
  const extraction = source.extraction === "ocr" ? " · OCR text: verify against original page" : "";
  return `${source.title} (${source.id})${page}${section}${extraction}`;
}

function setupKnowledge(role) {
  const form = document.getElementById("knowledge-form");
  const result = document.getElementById("knowledge-result");
  const submit = form.querySelector("button");
  let hybridButton = null;
  api("/api/knowledge/hybrid-status").then((status) => {
    if (!status.configured || !form.isConnected) return;
    hybridButton = element("button", "outline-button", "تجربة البحث الهجين");
    hybridButton.type = "button";
    hybridButton.disabled = submit.disabled;
    const collectionLabel = element("label", "", "مجموعة المستندات للبحث الهجين التجريبي");
    const collection = element("select"); collection.id = "hybrid-collection"; collectionLabel.htmlFor = collection.id;
    const options = [["", "كل المستندات المصرح بها"], ["handbook", "دليل الموظفين والسياسات"],
      ["job_description", "وصف وظيفة"], ["legacy_unclassified", "غير مصنّف"]];
    if (role === "hr") options.push(["past_hire_patterns", "أنماط توظيف سابقة — HR فقط"]);
    for (const [value, label] of options) { const option = element("option", "", label); option.value = value; collection.append(option); }
    collectionLabel.append(collection);
    const job = knowledgeJobSelector("hybrid-job", "جميع الوظائف والمستندات العامة");
    const sectionLabel = element("label", "", "اسم القسم (اختياري؛ مطابق تمامًا)");
    const section = element("input"); section.id = "hybrid-section"; section.type = "text"; section.maxLength = 400;
    sectionLabel.htmlFor = section.id; sectionLabel.append(section);
    form.append(element("p", "subtle-note", "تجربة محلية على مستندات المعرفة التي أضافها HR فقط؛ لا تشمل الوظائف المنشورة أو العينات. فلاتر المجموعة والوظيفة والقسم تنطبق على البحث الهجين فقط، وليس الإجابة أو المحادثة العادية. عند اختيار وظيفة لا تظهر المستندات غير المرتبطة بها. تصفية القسم تطابق الاسم تمامًا في ملفات DOCX المرفوعة حديثًا بعناوين Word؛ لا تشمل المصادر بلا أقسام معروفة. المقتطفات قد لا تدعم السؤال ولا تمثل إجابة متحققًا منها."), collectionLabel, job.label, sectionLabel, hybridButton);
    hybridButton.addEventListener("click", async () => {
      const question = document.getElementById("knowledge-question").value.trim();
      if (!question) return;
      const sectionName = section.value.trim();
      if (Array.from(sectionName).length > 200) {
        result.replaceChildren(element("p", "form-error", "اسم القسم يجب ألا يتجاوز 200 حرف."));
        return;
      }
      submit.disabled = true; hybridButton.disabled = true; collection.disabled = true; section.disabled = true;
      result.className = "knowledge-result";
      result.replaceChildren(element("p", "", "جاري البحث التجريبي..."));
      try {
        const payload = { question }; if (collection.value) payload.collection = collection.value;
        if (job.select.value) payload.job_id = job.select.value;
        if (sectionName) payload.section = sectionName;
        const answer = await api("/api/knowledge/experimental-hybrid", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }, { errorMessages: { 429: "البحث مشغول؛ حاول مرة أخرى.", 503: "البحث التجريبي غير متاح؛ تحقق من إعداد النموذج وقاعدة البيانات." } });
        result.replaceChildren(element("h3", "", "مقتطفات تجريبية"), element("p", "subtle-note", "تحقق من كل مصدر؛ التشابه لا يثبت أن المقتطف يجيب عن السؤال."));
        if (!answer.sources?.length) result.append(element("p", "", "لم يُعثر على مقتطف مناسب في المستندات المضافة."));
        else {
          const list = element("ul");
          for (const source of answer.sources) {
            const item = element("li");
            item.append(element("strong", "", knowledgeSourceLabel(source)), element("p", "", source.text));
            list.append(item);
          }
          result.append(list);
        }
      } catch (error) {
        if (!isStale(error) && result.isConnected) result.replaceChildren(element("p", "form-error", error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message));
      } finally { submit.disabled = false; hybridButton.disabled = false; collection.disabled = false; section.disabled = false; }
    });
  }).catch((error) => { if (!isStale(error)) console.warn("Could not check experimental search availability."); });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = document.getElementById("knowledge-question").value.trim();
    if (!question) return;
    const button = submit; button.disabled = true; if (hybridButton) hybridButton.disabled = true;
    result.className = "knowledge-result";
    result.replaceChildren(element("p", "", "جاري البحث..."));
    try {
      const answer = await api("/api/knowledge/answer", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }) });
      result.replaceChildren(element("h3", "", "الإجابة"), element("p", "", answer.answer));
      const mode = answer.mode === "groq" ? "إجابة مولدة بالذكاء الاصطناعي" : answer.mode === "abstain" ? "لم يُعثر على دليل مناسب" : "مقتطفات مسترجعة من المصادر";
      result.append(element("p", "subtle-note", mode));
      if (answer.notice) result.append(element("p", "subtle-note", answer.notice));
      if (answer.sources?.length) {
        result.append(element("h3", "", "المصادر"));
        const sources = element("ul");
        for (const source of answer.sources) {
          const item = element("li", "");
          item.append(element("strong", "", knowledgeSourceLabel(source)), element("p", "", source.text));
          sources.append(item);
        }
        result.append(sources);
      }
    } catch (error) {
      if (!isStale(error)) result.replaceChildren(element("p", "form-error", error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message));
    } finally { button.disabled = false; if (hybridButton) hybridButton.disabled = false; }
  });
}

function setupConversations(role) {
  const sectionNode = element("section", "knowledge-section");
  sectionNode.setAttribute("aria-label", "محادثاتي المحفوظة");
  const heading = element("div", "section-heading"); heading.append(element("h2", "", "محادثاتي المحفوظة"));
  const panel = element("div", "knowledge-panel");
  const note = element("p", "subtle-note", "يُحفظ السجل لحسابك. قد يُستخدم آخر سؤال مؤهل كسياق ويُرسل إلى Groq عند تفعيله، دون إرسال الإجابة السابقة. ابدأ محادثة جديدة عند تغيير الموضوع، وراجع المصادر ولا ترسل بيانات شخصية غير لازمة.");
  const message = element("p", "subtle-note"); message.setAttribute("aria-live", "polite");
  const selectorLabel = element("label", "", "اختر محادثة من سجلك");
  const selector = element("select", "conversation-select"); selectorLabel.append(selector);
  const create = element("button", "outline-button", "بدء محادثة جديدة"); create.type = "button";
  const history = element("div", "knowledge-result"); history.setAttribute("aria-live", "polite");
  const form = element("form");
  const modeLabel = element("label", "", "نوع السؤال");
  const questionMode = element("select");
  const modes = [["general", "بحث عام في المعرفة والوظائف"], ["policy", "وكيل سياسات الموظفين"]];
  if (role === "candidate") modes.push(["application_status", "حالة طلب التوظيف"]);
  for (const [value, label] of modes) {
    const option = element("option", "", label); option.value = value; questionMode.append(option);
  }
  modeLabel.append(questionMode);
  const applicationLabel = element("label", "", "اختر طلبك"); applicationLabel.hidden = true;
  const applicationSelect = element("select"); applicationSelect.disabled = true; applicationLabel.append(applicationSelect);
  questionMode.addEventListener("change", async () => {
    const statusMode = questionMode.value === "application_status"; applicationLabel.hidden = !statusMode; applicationSelect.disabled = !statusMode;
    if (!statusMode) return;
    applicationSelect.replaceChildren(); applicationSelect.disabled = true;
    try {
      const applications = await api("/api/applications");
      if (!sectionNode.isConnected || questionMode.value !== "application_status") return;
      for (const application of applications) { const option = element("option", "", `طلب #${application.id} · ${application.job_id}`); option.value = String(application.id); applicationSelect.append(option); }
      if (!applications.length) report("لا توجد طلبات متاحة لحسابك.", true); else report("");
    } catch (error) { if (!isStale(error) && sectionNode.isConnected) report(error instanceof TypeError ? "تعذّر تحميل طلباتك." : error.message, true); }
    finally { if (sectionNode.isConnected && questionMode.value === "application_status") applicationSelect.disabled = false; }
  });
  const questionLabel = element("label", "", "سؤالك في المحادثة");
  const question = element("textarea"); question.required = true; question.maxLength = 2000; questionLabel.append(question);
  const send = element("button", "small-button", "إرسال السؤال"); send.type = "submit";
  form.append(modeLabel, applicationLabel, element("p", "subtle-note", "وكيل السياسات يبحث في مصادر السياسات المصرح بها فقط؛ راجع المراجع قبل الاعتماد على الإجابة. لا يستخدم سؤالًا سابقًا كسياق."), questionLabel, send);
  panel.append(note, message, selectorLabel, create, history, form); sectionNode.append(heading, panel);
  document.getElementById("knowledge-form").closest("section").after(sectionNode);
  let selected = null, loadGeneration = 0, sendPending = false, creationPending = false;
  function report(text, error = false) { message.textContent = text; message.className = error ? "form-error" : "subtle-note"; }
  function renderHistory(conversation) {
    history.replaceChildren(element("h3", "", conversation.title));
    if (!conversation.messages.length) history.append(element("p", "subtle-note", "لا توجد أسئلة محفوظة في هذه المحادثة بعد."));
    for (const entry of conversation.messages) {
      const item = element("article", "conversation-entry");
      item.append(element("h4", "", "السؤال"), element("p", "", entry.question), element("h4", "", "الإجابة"));
      if (entry.mode === "unavailable") item.append(element("p", "form-error", "هذه الإجابة لم تعد متاحة لحسابك أو تغيرت البيانات التي تعتمد عليها."));
      else {
        item.append(element("p", "", entry.answer));
        if (entry.next_step) item.append(element("p", "subtle-note", entry.next_step));
        const mode = entry.mode === "application_status" ? "مرحلة الطلب الحالية؛ تُحدّث عند تحميل المحادثة." : entry.mode === "groq" ? "إجابة مولدة بالذكاء الاصطناعي؛ تحقق من المصادر." : entry.mode === "abstain" ? "لم يُعثر على دليل مناسب." : "مقتطفات مسترجعة من المصادر.";
        item.append(element("p", "subtle-note", mode));
        if (entry.notice) item.append(element("p", "subtle-note", entry.notice));
        if (entry.mode === "application_status") {
          if (entry.interview) item.append(element("p", "", `Your interview: ${displayDate(entry.interview.start_at)} – ${displayDate(entry.interview.end_at)} · ${entry.interview.interviewer}${entry.interview.completed ? " · completed" : ""}`));
          if (entry.available_slots?.length) {
            const slots = element("ul", "evidence-list");
            for (const slot of entry.available_slots) slots.append(element("li", "", `${displayDate(slot.start_at)} – ${displayDate(slot.end_at)} · ${slot.interviewer}`));
            item.append(element("h5", "", "Currently available interview slots"), slots, element("p", "subtle-note", `Availability can change. Open your application to book.${entry.slots_truncated ? " More slots may be available there." : ""}`));
          }
        }
        if (entry.sources?.length) {
          const list = element("ul", "evidence-list");
          for (const source of entry.sources) { const li = element("li"); li.append(element("strong", "", knowledgeSourceLabel(source)), element("p", "", source.text)); list.append(li); }
          item.append(element("h4", "", "المصادر"), list);
        }
      }
      history.append(item);
    }
  }
  async function openConversation(id) {
    selected = id; const request = ++loadGeneration; send.disabled = !id || sendPending || creationPending; history.replaceChildren();
    if (!id) { history.append(element("p", "subtle-note", "أنشئ محادثة أو اختر واحدة من السجل.")); return; }
    history.append(element("p", "subtle-note", "جارٍ تحميل السجل..."));
    try {
      const conversation = await api(`/api/conversations/${encodeURIComponent(id)}`);
      if (request !== loadGeneration || !sectionNode.isConnected) return;
      renderHistory(conversation); report("");
    } catch (error) {
      if (!isStale(error) && request === loadGeneration && sectionNode.isConnected) { history.replaceChildren(); report(error instanceof TypeError ? "تعذّر الاتصال بالخادم لتحميل السجل." : error.message, true); }
    }
  }
  async function refreshList(preferred = selected) {
    const list = await api("/api/conversations");
    if (!sectionNode.isConnected) return;
    selector.replaceChildren(); const empty = element("option", "", "اختر محادثة"); empty.value = ""; selector.append(empty);
    for (const item of list) { const option = element("option", "", `${item.title} · #${item.id}`); option.value = String(item.id); selector.append(option); }
    selector.value = list.some((item) => String(item.id) === String(preferred)) ? String(preferred) : "";
    await openConversation(selector.value || null);
  }
  selector.addEventListener("change", () => { openConversation(selector.value || null); });
  create.addEventListener("click", async () => {
    creationPending = true; create.disabled = true; selector.disabled = true; question.disabled = true; send.disabled = true;
    try {
      const conversation = await api("/api/conversations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) });
      if (sectionNode.isConnected) await refreshList(conversation.id);
    } catch (error) { if (!isStale(error) && sectionNode.isConnected) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
    finally { creationPending = false; create.disabled = false; selector.disabled = false; question.disabled = false; send.disabled = !selected; }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); const id = selected; const text = question.value.trim();
    if (!id || !text || sendPending || creationPending) return;
    const body = { question: text, mode: questionMode.value };
    if (questionMode.value === "application_status") {
      const applicationId = Number(applicationSelect.value);
      if (!Number.isSafeInteger(applicationId) || applicationId <= 0 || applicationSelect.disabled) { report("اختر طلبًا من طلباتك أولًا.", true); return; }
      body.application_id = applicationId;
    }
    sendPending = true; send.disabled = true; selector.disabled = true; create.disabled = true; question.disabled = true; questionMode.disabled = true; applicationSelect.disabled = true; report("جارٍ البحث عن إجابة...");
    try {
      await api(`/api/conversations/${encodeURIComponent(id)}/messages`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!sectionNode.isConnected) return;
      if (selected === id) { question.value = ""; await openConversation(id); }
    } catch (error) { if (!isStale(error) && sectionNode.isConnected && selected === id) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
    finally {
      sendPending = false;
      if (sectionNode.isConnected) { send.disabled = !selected; selector.disabled = false; create.disabled = false; question.disabled = false; questionMode.disabled = false; applicationSelect.disabled = questionMode.value !== "application_status"; }
    }
  });
  send.disabled = true; selector.disabled = true; create.disabled = true; question.disabled = true;
  refreshList(null).catch((error) => { if (!isStale(error) && sectionNode.isConnected) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم لتحميل المحادثات." : error.message, true); })
    .finally(() => { if (sectionNode.isConnected) { selector.disabled = false; create.disabled = false; question.disabled = false; send.disabled = !selected; } });
}

function operationsMessage(message, isError = false) {
  const node = document.getElementById("operations-message");
  if (!node) return;
  node.textContent = message; node.classList.toggle("error", isError); node.hidden = !message;
}

async function loadOperations(role) {
  const area = document.getElementById("operations");
  if (!area) return false;
  area.hidden = false;
  operationsMessage("جاري تحميل بيانات المتابعة...");
  try {
    const tasks = [api("/api/interviews")];
    if (role === "hr") tasks.push(api("/api/dashboard"), api("/api/applications"), api("/api/slots"));
    const [interviews, dashboard, applications, slots] = await Promise.all(tasks);
    if (!document.getElementById("operations")) return false;
    if (!Array.isArray(interviews) || (role === "hr" && (!Array.isArray(applications) || !Array.isArray(slots)))) throw new Error("تعذّر قراءة بيانات الخادم.");
    operationsMessage("");
    if (role === "hr") renderDashboard(dashboard);
    renderInterviews(interviews, role, applications || [], slots || []);
    return true;
  } catch (error) {
    if (!isStale(error)) operationsMessage(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true);
    return false;
  }
}

function setupCancellationNotice(card, interview) {
  const details = element("details", "application-detail");
  details.append(element("summary", "", "Review cancellation notice"));
  const panel = element("div");
  const refresh = element("button", "outline-button", "Load notice preview"); refresh.type = "button";
  const content = element("div"); content.setAttribute("aria-live", "polite");
  const endpoint = `/api/google/interviews/${encodeURIComponent(interview.id)}/cancellation-notice`;
  async function load() {
    refresh.disabled = true; content.replaceChildren(element("p", "subtle-note", "Loading preview..."));
    try {
      const preview = await api(endpoint);
      if (!content.isConnected) return;
      content.replaceChildren(element("p", "", `Recipient from the earlier confirmation: ${preview.recipient}`),
        element("p", "", `Subject: ${preview.subject}`), element("pre", "resume-preview", preview.body),
        element("p", "form-error", preview.notice || "Review the recipient, sender account, and content before sending."));
      if (preview.last_notice) content.append(element("p", "", `Last attempt: ${preview.last_notice.state}`));
      if (preview.can_send) {
        const checkbox = element("input"); checkbox.type = "checkbox";
        const label = element("label", "", "I reviewed the recipient and notice and approve sending this email."); label.prepend(checkbox);
        const send = element("button", "small-button", "Send cancellation notice"); send.type = "button";
        send.addEventListener("click", async () => {
          if (!checkbox.checked) return;
          send.disabled = true; checkbox.disabled = true;
          try { await api(`${endpoint}/send`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({confirmed: true, fingerprint: preview.fingerprint})}); await load(); }
          catch (error) { if (!isStale(error) && content.isConnected) content.append(element("p", "form-error", "The send result may be uncertain. Reload the notice and check Gmail before taking further action.")); }
        });
        content.append(label, send);
      }
      if (["uncertain", "sending"].includes(preview.last_notice?.state)) {
        content.append(element("p", "form-error", "Verify the result in Gmail before recording either outcome. This is your attestation, not a result verified by the application. A send still in progress cannot be reconciled."));
        for (const [outcome, labelText] of [["sent", "I verified the email was sent"], ["not_sent", "I verified the email was not sent"]]) {
          const resolve = element("button", "outline-button", labelText); resolve.type = "button";
          resolve.addEventListener("click", async () => {
            resolve.disabled = true;
            try { await api(`/api/google/cancellation-notices/${encodeURIComponent(preview.last_notice.id)}/resolve`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({confirmed: true, outcome})}); await load(); }
            catch (error) { if (!isStale(error) && content.isConnected) content.append(element("p", "form-error", "Could not record the verified outcome; reload before trying again.")); }
          });
          content.append(resolve);
        }
      }
    } catch (error) {
      if (!isStale(error) && content.isConnected) content.replaceChildren(element("p", "subtle-note", "No cancellation notice is available for this interview, or Google integration is unavailable."));
    } finally { refresh.disabled = false; }
  }
  refresh.addEventListener("click", load);
  panel.append(refresh, content); details.append(panel); card.append(details);
}

function setupRecruitmentAudit(target, kind = "timeline") {
  const details = element("details", "application-detail");
  details.append(element("summary", "", kind === "rubric" ? "Rubric audit timeline" : "Recruitment audit timeline"));
  const panel = element("div");
  panel.append(element("p", "subtle-note", kind === "rubric" ? "Rubric events only; other audit categories are separate. Résumé excerpts and judgments are excluded." : "Recruitment events only; other audit categories are separate. Notes and résumé excerpts are excluded."));
  const form = element("form", "decision-form");
  const applicationLabel = element("label", "", "Application ID (optional)");
  const application = element("input"); application.type = "number"; application.min = "1"; applicationLabel.append(application);
  const jobLabel = element("label", "", "Job ID (optional)"); const job = element("input"); job.maxLength = 128; jobLabel.append(job);
  const actionLabel = element("label", "", "Action (optional)"); const action = element("input"); action.maxLength = 100; actionLabel.append(action); actionLabel.hidden = kind === "rubric";
  const search = element("button", "outline-button", "Search audit events"); search.type = "submit";
  const more = element("button", "outline-button", "Load older events"); more.type = "button"; more.hidden = true;
  const output = element("div"); output.setAttribute("aria-live", "polite");
  form.append(applicationLabel, jobLabel, actionLabel, search); panel.append(form, output, more); details.append(panel); target.append(details);
  let cursor = null, filters = new URLSearchParams();
  async function load(reset) {
    search.disabled = true; more.disabled = true;
    try {
      const query = new URLSearchParams(filters); if (!reset && cursor !== null) query.set("before", String(cursor));
      const page = await api(`/api/audit/${kind}?${query}`);
      if (!output.isConnected) return;
      if (reset) output.replaceChildren(element("p", "subtle-note", page.notice));
      for (const event of page.events || []) {
        const summary = Object.entries(event.summary || {}).map(([key, value]) => `${key}: ${value}`).join(" · ");
        output.append(element("p", "", `#${event.id} · ${displayDate(event.created_at)} · application ${event.application_id == null ? "not linked" : `#${event.application_id}`} · ${event.job_id || "job not linked"} · actor #${event.actor_id} · ${event.action}${event.reference_id == null ? "" : ` · reference #${event.reference_id}`}${summary ? ` · ${summary}` : ""}`));
      }
      if (reset && !page.events?.length) output.append(element("p", "subtle-note", "No events match these filters."));
      cursor = page.next_before; more.hidden = cursor === null;
    } catch (error) {
      if (!isStale(error) && output.isConnected) output.append(element("p", "form-error", "Unable to load recruitment audit events."));
    } finally { search.disabled = false; more.disabled = false; }
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault(); filters = new URLSearchParams(); cursor = null;
    if (application.value) filters.set("application_id", application.value);
    if (job.value.trim()) filters.set("job_id", job.value.trim());
    if (kind === "timeline" && action.value.trim()) filters.set("action", action.value.trim());
    more.hidden = true; load(true);
  });
  more.addEventListener("click", () => load(false));
}

function renderRecordedMilestones(data) {
  const chart = element("section", "application-detail");
  chart.append(element("h3", "", "Recorded recruitment milestones"), element("p", "subtle-note", "Each bar counts distinct retained applications with the recorded event. Repeated events count once; later cancellations, withdrawals, or rejections do not remove past milestones. Older records without an audit event are not counted. Stages can be recorded out of order, so these are not conversion rates."));
  const total = data.applications_total;
  const labels = {received: "Applications received", screened: "Screened", advanced: "Advanced", interview_scheduled: "Interview scheduled", interview_completed: "Interview completed", offer_presented: "Offer presented", hired: "Hired"};
  const outcomes = {interview_cancelled: "Interview cancelled", offer_accepted: "Offer accepted", offer_declined: "Offer declined", offer_withdrawn: "Offer withdrawn", rejected: "Application rejected"};
  function addBars(entries, names, key) {
    const list = element("ol", "evidence-list");
    for (const entry of entries || []) {
      const name = names[entry[key]];
      if (!name || !Number.isInteger(entry.count) || entry.count < 0 || !Number.isInteger(total) || entry.count > total) continue;
      const item = element("li", "", `${name}: ${entry.count} of ${total} applications`);
      if (total > 0) {
        const meter = element("meter"); meter.min = 0; meter.max = total; meter.value = entry.count; meter.style.width = "100%"; meter.dir = "ltr";
        meter.setAttribute("aria-label", `${name}: ${entry.count} of ${total} applications`); item.append(meter);
      }
      list.append(item);
    }
    chart.append(list);
  }
  addBars(data.recorded_milestones, labels, "stage");
  chart.append(element("h4", "", "Recorded outcomes"));
  addBars(data.recorded_outcomes, outcomes, "outcome");
  return chart;
}

function renderDashboard(data) {
  const target = document.getElementById("dashboard-area");
  const box = section("مؤشرات التوظيف");
  const grid = element("div", "metrics-grid");
  const items = [[data.applications_total, "إجمالي الطلبات"], [data.screening_events, "عمليات فحص مسجلة"], [data.time_to_hire?.average_days == null ? "—" : data.time_to_hire.average_days, "متوسط أيام التوظيف"]];
  for (const [value, label] of items) {
    const card = element("div", "metric-card"); card.append(element("strong", "", String(value)), element("span", "", label)); grid.append(card);
  }
  box.append(grid, element("p", "subtle-note", `متوسط الأيام من وقت التقديم حتى آخر قرار توظيف للطلبات المعينة حاليًا؛ عدد الحالات: ${data.time_to_hire?.count ?? 0}.`));
  box.append(renderRecordedMilestones(data));
  const daily = element("details", "application-detail");
  daily.append(element("summary", "", "Daily screening activity (UTC)"));
  const dailyResult = element("div");
  dailyResult.append(element("p", "subtle-note", "Loading..."));
  daily.append(dailyResult); box.append(daily);
  api("/api/dashboard/screenings/daily").then((result) => {
    if (!dailyResult.isConnected) return;
    dailyResult.replaceChildren(element("p", "subtle-note", result.definition));
    const table = element("table");
    const head = element("thead");
    const header = element("tr");
    for (const label of ["UTC date", "Screening events", "Applications screened"]) header.append(element("th", "", label));
    head.append(header); table.append(head);
    const body = element("tbody");
    for (const day of result.days || []) {
      const row = element("tr");
      for (const value of [day.date, day.screening_events, day.applications_screened]) row.append(element("td", "", String(value)));
      body.append(row);
    }
    table.append(body); dailyResult.append(table);
  }).catch(() => {
    if (dailyResult.isConnected) dailyResult.replaceChildren(element("p", "form-error", "Daily screening activity is unavailable. Reload to try again."));
  });
  const pipeline = element("ul", "evidence-list");
  for (const item of data.pipeline || []) pipeline.append(element("li", "", `${statusLabels[item.status] || item.status}: ${item.count}`));
  if (pipeline.childNodes.length) box.append(element("h3", "", "الطلبات حسب المرحلة"), pipeline);
  setupRecruitmentAudit(box);
  setupRecruitmentAudit(box, "rubric");
  const perJob = element("section", "application-detail");
  perJob.append(element("h4", "", "Recruitment metrics by job"), element("p", "subtle-note", "Loading..."));
  box.append(perJob);
  api("/api/dashboard/jobs").then((result) => {
    if (!perJob.isConnected) return;
    perJob.replaceChildren(element("h4", "", "Recruitment metrics by job"), element("p", "subtle-note", result.definition));
    for (const job of result.jobs || []) {
      const sectionNode = element("section", "application-detail");
      sectionNode.append(element("h5", "", job.title), element("p", "", `Applications: ${job.applications_total} · screenings: ${job.screening_events} · average days to hire: ${job.time_to_hire.average_days ?? "unavailable"} (${job.time_to_hire.count} hires)`));
      const counts = element("ul", "evidence-list");
      for (const stage of job.pipeline || []) counts.append(element("li", "", `${statusLabels[stage.status] || stage.status}: ${stage.count}`));
      sectionNode.append(counts); perJob.append(sectionNode);
    }
  }).catch((error) => { if (!isStale(error) && perJob.isConnected) perJob.replaceChildren(element("h4", "", "Recruitment metrics by job"), element("p", "form-error", "Unable to load job metrics.")); });
  const evaluation = element("section", "application-detail");
  evaluation.setAttribute("aria-label", "التقييم الاصطناعي");
  evaluation.append(element("h4", "", "تقييم المشروع ببيانات اصطناعية"),
    element("p", "subtle-note", "اختبارات محدودة على بيانات وتسميات اصطناعية. استرجاع المصدر ليس قياسًا لدقة الإجابة؛ هذه الاختبارات لا تقيس زمن الاستجابة أو الإنصاف."));
  const run = element("button", "outline-button", "تشغيل التقييم الاصطناعي"); run.type = "button";
  const output = element("div"); output.setAttribute("aria-live", "polite");
  run.addEventListener("click", async () => {
    run.disabled = true; output.replaceChildren(element("p", "subtle-note", "جارٍ تشغيل الاختبارات..."));
    try {
      const report = await api("/api/evaluation/capstone");
      if (!output.isConnected) return;
      const summary = report.summary;
      output.replaceChildren(element("h4", "", "نتائج الاختبارات الاصطناعية"));
      const results = element("ul", "evidence-list");
      for (const [label, passed, total] of [
        ["حالات ترتيب المطابقة الصحيحة", summary.ranking_correct, summary.ranking_total],
        ["حالات وجود المصدر المتوقع ضمن أول 3 نتائج", summary.retrieval_recall_at_3, summary.retrieval_total],
        ["حالات الاختبارات السلبية المجتازة", summary.negative_passed, summary.negative_total]
      ]) results.append(element("li", "", `${label}: ${passed} من ${total}`));
      output.append(results);
      for (const [title, cases] of [
        ["حالات لم يُسترجع فيها المصدر المتوقع", report.missed_retrieval_cases],
        ["حالات ترتيب لم تجتز الاختبار", report.failed_ranking_cases],
        ["حالات سلبية لم تجتز الاختبار", report.failed_negative_cases]
      ]) {
        if (Array.isArray(cases) && cases.length) output.append(element("p", "form-error", `${title}: ${cases.join("، ")}`));
      }
      output.append(element("p", "subtle-note", "التقييم لا يتحقق من صحة الإجابة أو جودة الاستشهاد على صفحات فعلية، ولا يثبت الأداء على بيانات حقيقية."));
    } catch (error) {
      if (!isStale(error) && output.isConnected) output.replaceChildren(element("p", "form-error", error instanceof TypeError ? "تعذّر الاتصال بالخادم لتشغيل التقييم." : error.message));
    } finally { run.disabled = false; }
  });
  evaluation.append(run, output); box.append(evaluation);
  target.replaceChildren(box);
}

function displayDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString("ar-EG", { dateStyle: "medium", timeStyle: "short" });
}

function setupGoogleDeliveries(card, interview) {
  const details = element("details", "google-deliveries");
  details.append(element("summary", "", "تجهيز مسودة Google Calendar أو Gmail"));
  const panel = element("div", "google-delivery-panel");
  panel.append(element("p", "subtle-note", "تجهيز المسودة لا يرسل شيئًا. إنشاء حدث Google Calendar الخاص لا يضيف مدعوين؛ رسالة Gmail إجراء منفصل لكل مستلم. قد تصدر Google إشعارات رغم طلب عدم إرسالها."));
  panel.append(element("p", "subtle-note", "إذا بقيت العملية قيد الإرسال أو كانت نتيجتها غير مؤكدة، تحقق من Google قبل أي إجراء؛ لن يعيد التطبيق المحاولة تلقائيًا."));
  const message = element("p", "subtle-note"); message.setAttribute("aria-live", "polite");
  const list = element("div", "google-delivery-list");
  const reload = element("button", "outline-button", "تحديث سجل Google"); reload.type = "button";
  const proposalForm = element("form", "decision-form");
  const proposalAddressLabel = element("label", "", "عنوان البريد المقترح للمرشح (غير متحقق منه)");
  const proposalAddress = element("input"); proposalAddress.type = "email"; proposalAddress.required = true; proposalAddress.maxLength = 254; proposalAddressLabel.append(proposalAddress);
  const previewProposal = element("button", "outline-button", "مراجعة مقترح التقويم والبريد"); previewProposal.type = "submit";
  const proposalView = element("div"); proposalView.setAttribute("aria-live", "polite");
  proposalForm.append(proposalAddressLabel, previewProposal, proposalView);
  const calendarForm = element("form", "decision-form");
  const calendarReview = element("input"); calendarReview.type = "checkbox";
  const calendarLabel = element("label", "admin-checkbox", "راجعت الموعد وأريد تجهيز مسودة حدث خاص دون مدعوين.");
  calendarLabel.prepend(calendarReview);
  const calendarCreate = element("button", "outline-button", "تجهيز مسودة التقويم"); calendarCreate.type = "submit";
  calendarForm.append(calendarLabel, calendarCreate);
  const emailForm = element("form", "decision-form");
  const recipientLabel = element("label", "", "البريد الإلكتروني للمستلم");
  const recipient = element("input"); recipient.type = "email"; recipient.required = true; recipient.maxLength = 254; recipient.dir = "ltr"; recipient.autocomplete = "off";
  recipientLabel.append(recipient);
  const emailReview = element("input"); emailReview.type = "checkbox";
  const emailLabel = element("label", "admin-checkbox", "راجعت العنوان والموعد وأريد تجهيز مسودة رسالة تأكيد لهذا المستلم.");
  emailLabel.prepend(emailReview);
  const emailCreate = element("button", "outline-button", "تجهيز مسودة الرسالة"); emailCreate.type = "submit";
  emailForm.append(recipientLabel, emailLabel, emailCreate);
  panel.append(message, reload, list, proposalForm, calendarForm, emailForm); details.append(panel); card.append(details);
  let loaded = false, busy = false, loading = false, deliveries = [];
  const endpoint = `/api/google/interviews/${encodeURIComponent(interview.id)}/deliveries`;
  function report(text, isError = false) { message.textContent = text; message.className = isError ? "form-error" : "subtle-note"; }
  proposalAddress.addEventListener("input", () => proposalView.replaceChildren());
  proposalForm.addEventListener("submit", async (event) => {
    event.preventDefault(); const recipient = proposalAddress.value.trim(); if (!recipient || !proposalAddress.checkValidity()) return;
    previewProposal.disabled = true; proposalAddress.disabled = true; proposalView.replaceChildren(element("p", "subtle-note", "جارٍ تجهيز المقترح للمراجعة..."));
    try {
      const path = `/api/google/interviews/${encodeURIComponent(interview.id)}/delivery-proposal`;
      const proposal = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ recipient }) });
      if (!proposalView.isConnected) return;
      proposalView.replaceChildren(element("h5", "", "راجع المقترح قبل تجهيز المسودات"),
        element("p", "form-error", `المستلم: ${proposal.recipient} — أدخله HR ولم يُتحقق منه.`),
        element("p", "", `المقابلة: ${displayDate(proposal.start_at)} إلى ${displayDate(proposal.end_at)} · الطلب #${proposal.application_id}`),
        element("p", "", `التقويم: ${proposal.calendar?.summary || ""} · خاص، دون مدعوين`),
        element("p", "", `موضوع الرسالة: ${proposal.email?.subject || ""}`), element("pre", "resume-preview", proposal.email?.body || ""),
        element("p", "subtle-note", "هذا يجهز مسودتين محليتين فقط؛ لا ينشئ حدثًا ولا يرسل بريدًا. لكل إجراء خارجي تأكيد منفصل."));
      const states = element("ul", "evidence-list"); for (const draft of proposal.drafts || []) states.append(element("li", "", `${draft.kind === "calendar" ? "التقويم" : "البريد"}: ${draft.state}`)); proposalView.append(states);
      if (!proposal.can_prepare) { proposalView.append(element("p", "form-error", "تحتاج المسودات الحالية إلى مراجعة منفصلة قبل المتابعة.")); return; }
      const confirmLabel = element("label", "", "راجعت العنوان غير المتحقق منه والتفاصيل وأوافق على تجهيز المسودات المحلية"); const confirm = element("input"); confirm.type = "checkbox"; confirmLabel.prepend(confirm);
      const prepare = element("button", "small-button", "تجهيز المسودتين محليًا"); prepare.type = "button";
      prepare.addEventListener("click", async () => {
        if (!confirm.checked) { report("أكد مراجعة المقترح أولًا.", true); return; }
        prepare.disabled = true; confirm.disabled = true;
        try {
          await api(`${path}/prepare`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ recipient, fingerprint: proposal.fingerprint, confirmed: true }) });
          if (proposalView.isConnected) { proposalView.replaceChildren(element("p", "subtle-note", "تم تجهيز المسودات محليًا؛ راجع سجل Google قبل أي إجراء آخر.")); reload.click(); }
        } catch (error) { if (!isStale(error) && proposalView.isConnected) { report(error instanceof TypeError ? "تعذّر الاتصال؛ حدّث سجل Google للتحقق قبل أي محاولة أخرى." : error.message, true); } }
      });
      proposalView.append(confirmLabel, prepare);
    } catch (error) { if (!isStale(error) && proposalView.isConnected) proposalView.replaceChildren(element("p", "form-error", error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message)); }
    finally { previewProposal.disabled = false; proposalAddress.disabled = false; }
  });
  function synchronizeControls() {
    const existing = new Set(deliveries.map((item) => item.kind));
    calendarForm.hidden = existing.has("calendar"); emailForm.hidden = existing.has("email");
    for (const control of panel.querySelectorAll("button, input")) control.disabled = busy || !loaded;
    reload.disabled = busy || loading;
  }
  function display() {
    list.replaceChildren();
    if (!deliveries.length) list.append(element("p", "empty-note", "لا توجد مسودات لهذا الموعد."));
    const stateLabels = { pending: "مسودة بانتظار تأكيد الإجراء", sending: "قيد الإرسال أو تحتاج مراجعة بعد انقطاع", confirmed: "قبلت Google الطلب", rejected: "رُفضت المحاولة", uncertain: "النتيجة غير مؤكدة وتحتاج مراجعة" };
    for (const delivery of deliveries) {
      const entry = element("section", "google-delivery-entry");
      const type = delivery.kind === "calendar" ? "حدث التقويم" : "رسالة التأكيد";
      entry.append(element("h5", "", type), element("p", "", `الحالة: ${stateLabels[delivery.state] || "غير معروفة؛ راجع السجل"}.`));
      if (delivery.kind === "email" && delivery.recipient) entry.append(element("p", "", `المستلم: ${delivery.recipient}`));
      entry.append(element("p", "subtle-note", `الموعد: من ${displayDate(delivery.start_at)} إلى ${displayDate(delivery.end_at)}`));
      if (delivery.kind === "calendar") entry.append(element("p", "subtle-note", "عنوان الحدث: TawzeefAI interview · خاص وبدون مدعوين."));
      if (delivery.kind === "email") {
        const start = new Date(delivery.start_at), end = new Date(delivery.end_at);
        if (!Number.isNaN(start.valueOf()) && !Number.isNaN(end.valueOf())) {
          const preview = element("p", "subtle-note", `Subject: Interview confirmation — Your interview is scheduled from ${start.toISOString()} to ${end.toISOString()}.`);
          preview.dir = "ltr";
          entry.append(preview);
        }
      }
      if (delivery.state === "confirmed" && delivery.kind === "email") entry.append(element("p", "subtle-note", "قبول Gmail للطلب لا يثبت تسليم الرسالة إلى المستلم."));
      if (delivery.kind === "calendar" && delivery.state === "confirmed") {
        const removal = delivery.calendar_removal_state;
        const removalLabels = {
          removing: "إزالة الحدث قيد التنفيذ أو تحتاج مراجعة بعد انقطاع.",
          removed: "تأكدت إزالة الحدث من Google Calendar. يمكنك الآن إلغاء الموعد المحلي بشكل منفصل.",
          rejected: "لم تكتمل محاولة الإزالة. راجع سجل العملية قبل إلغاء الموعد.",
          uncertain: "نتيجة إزالة الحدث غير مؤكدة. راجع Google وسجل العملية؛ لن يعيد التطبيق المحاولة تلقائيًا."
        };
        if (removal === null) {
          entry.append(element("p", "subtle-note", "إزالة الحدث من Google إجراء منفصل عن إلغاء الموعد المحلي، ولا ترسل إشعارًا بإلغاء المقابلة عبر Gmail."));
          const confirmRemoval = element("input"); confirmRemoval.type = "checkbox";
          const removalLabel = element("label", "admin-checkbox", "راجعت الحدث وأوافق على إزالته من Google Calendar الآن.");
          removalLabel.prepend(confirmRemoval);
          const remove = element("button", "outline-button", "إزالة حدث Google Calendar"); remove.type = "button";
          remove.addEventListener("click", () => {
            if (!confirmRemoval.checked) { report("يرجى مراجعة الحدث وتأكيد إزالته أولًا.", true); return; }
            perform(async () => {
              const result = await api(`/api/google/deliveries/${encodeURIComponent(delivery.id)}/calendar-removal`, {
                method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirmed: true })
              }, { errorMessages: { 409: "لا يمكن تسوية هذا الحدث تلقائيًا؛ راجع Google وسجل العملية.", 503: "تعذر الاتصال بربط Google؛ راجع حالة العملية قبل أي إجراء آخر." } });
              return result.state === "removed" ? "تمت إزالة حدث Google. ألغِ الموعد المحلي بشكل منفصل عند الحاجة." : "نتيجة إزالة الحدث غير مؤكدة؛ راجع Google وسجل العملية.";
            }, true);
          });
          entry.append(removalLabel, remove);
        } else if (Object.hasOwn(removalLabels, removal)) {
          entry.append(element("p", removal === "removed" ? "subtle-note" : "form-error", removalLabels[removal]));
        } else {
          entry.append(element("p", "form-error", "حالة إزالة الحدث غير متاحة؛ حدّث السجل وراجع Google قبل الإلغاء."));
        }
      }
      if (delivery.state === "sending" || delivery.state === "uncertain") entry.append(element("p", "form-error", "تحقق من Google ومن سجل العملية؛ لا يمكن إعادة الإرسال تلقائيًا."));
      if (delivery.state === "rejected") {
        entry.append(element("p", "form-error", "رُفضت المحاولة. راجع الإعدادات؛ يمكنك إعادة تجهيز المسودة محليًا فقط، ثم مراجعتها وتأكيد الإرسال بشكل منفصل."));
        if (Number.isSafeInteger(delivery.retry_generation) && delivery.retry_generation >= 0) {
          const confirmRetry = element("input"); confirmRetry.type = "checkbox";
          const retryLabel = element("label", "admin-checkbox", "راجعت الرفض وأوافق على إعادة تجهيز المسودة دون إرسالها."); retryLabel.prepend(confirmRetry);
          const retry = element("button", "outline-button", "إعادة تجهيز المسودة"); retry.type = "button";
          retry.addEventListener("click", () => {
            if (!confirmRetry.checked) { report("يرجى مراجعة الرفض وتأكيد إعادة التجهيز أولًا.", true); return; }
            perform(async () => {
              await api(`/api/google/deliveries/${encodeURIComponent(delivery.id)}/reprepare`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ confirmed: true, expected_generation: delivery.retry_generation })
              }, { errorMessages: { 409: "تغيرت حالة المسودة؛ راجع السجل وحدّثه قبل إعادة التجهيز." } });
              return "أُعيد تجهيز المسودة محليًا دون إرسال؛ راجع التفاصيل وأكد الإجراء منفصلًا.";
            });
          });
          entry.append(retryLabel, retry);
        } else entry.append(element("p", "form-error", "تعذر قراءة إصدار المسودة؛ حدّث السجل قبل إعادة التجهيز."));
      }
      if (delivery.state === "pending") {
        if (!Number.isSafeInteger(delivery.retry_generation) || delivery.retry_generation < 0) {
          entry.append(element("p", "form-error", "تعذر قراءة إصدار المسودة؛ حدّث السجل قبل تعديلها أو تنفيذها."));
          list.append(entry);
          continue;
        }
        if (delivery.kind === "email" && delivery.recipient) {
          const changeForm = element("form", "decision-form");
          const changeLabel = element("label", "", "تصحيح بريد المستلم قبل الإرسال");
          const changeInput = element("input"); changeInput.type = "email"; changeInput.required = true;
          changeInput.maxLength = 254; changeInput.dir = "ltr"; changeInput.value = delivery.recipient;
          changeLabel.append(changeInput);
          const changeButton = element("button", "outline-button", "حفظ المستلم الجديد"); changeButton.type = "submit";
          changeForm.append(changeLabel, changeButton);
          changeForm.addEventListener("submit", (event) => {
            event.preventDefault();
            const address = changeInput.value.trim();
            if (!address || !changeInput.checkValidity()) { report("أدخل عنوان بريد صحيحًا لمستلم واحد.", true); return; }
            perform(async () => {
              await api(`/api/google/deliveries/${encodeURIComponent(delivery.id)}/recipient`, {
                method: "PATCH", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ confirmed: true, expected_recipient: delivery.recipient, recipient: address, expected_generation: delivery.retry_generation })
              }, { errorMessages: { 409: "تغيرت المسودة أو لم تعد قابلة للتعديل؛ راجع السجل." } });
              return "تم تحديث المستلم؛ راجع المسودة قبل الإرسال.";
            });
          });
          entry.append(changeForm);
        }
        const confirm = element("input"); confirm.type = "checkbox";
        const label = element("label", "admin-checkbox", delivery.kind === "email" ? "راجعت المستلم والموعد وأوافق على إرسال هذه الرسالة الآن عبر Gmail." : "راجعت الموعد وأوافق على إنشاء الحدث الخاص الآن في Google Calendar.");
        label.prepend(confirm);
        const send = element("button", "small-button", delivery.kind === "email" ? "إرسال رسالة التأكيد" : "إنشاء حدث Google Calendar"); send.type = "button";
        send.addEventListener("click", () => {
          if (!confirm.checked) { report("يرجى تأكيد مراجعة تفاصيل هذا الإجراء أولًا.", true); return; }
          perform(async () => {
            const result = await api(`/api/google/deliveries/${encodeURIComponent(delivery.id)}/send`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify(delivery.kind === "email" ? { confirmed: true, expected_recipient: delivery.recipient, expected_generation: delivery.retry_generation } : { confirmed: true, expected_generation: delivery.retry_generation })
            }, { errorMessages: { 409: "لا يمكن إرسال هذه المسودة في حالتها الحالية؛ راجع سجل العملية.", 503: "تعذر تنفيذ الطلب؛ راجع حالة المسودة وربط Google قبل أي إجراء آخر." } });
            if (result.state === "confirmed") return "قبلت Google الطلب. راجع حالة الحدث أو الرسالة في حساب Google.";
            if (result.state === "rejected") return "رُفضت المحاولة؛ راجع إعدادات الحساب وسجل العملية.";
            return "النتيجة غير مؤكدة؛ تحقق من Google قبل أي إجراء آخر.";
          }, true);
        });
        entry.append(label, send);
      }
      list.append(entry);
    }
    synchronizeControls();
  }
  async function refresh() {
    const items = await api(endpoint);
    if (!Array.isArray(items)) throw new Error("تعذّر قراءة سجل Google.");
    if (!details.isConnected) return;
    deliveries = items; loaded = true; display();
  }
  async function perform(action, isSend = false) {
    if (busy || !loaded) return;
    busy = true; synchronizeControls(); report(isSend ? "جارٍ تنفيذ الإجراء..." : "جارٍ تجهيز المسودة...");
    try {
      const resultMessage = await action();
      if (!details.isConnected) return;
      await refresh();
      report(resultMessage);
    } catch (error) {
      if (!isStale(error) && details.isConnected) {
        try { await refresh(); } catch { loaded = false; }
        report(error instanceof TypeError && isSend ? "تعذر معرفة نتيجة الإجراء؛ راجع سجل العملية وGoogle قبل أي إجراء آخر." : error instanceof TypeError ? "تعذّر الاتصال بالخادم؛ راجع سجل المسودات قبل المحاولة." : error.message, true);
      }
    } finally { busy = false; if (details.isConnected) synchronizeControls(); }
  }
  calendarForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!calendarReview.checked) { report("يرجى مراجعة الموعد وتأكيد تجهيز المسودة.", true); return; }
    perform(async () => {
      await api(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: "calendar", confirmed: true }) });
      calendarReview.checked = false;
      return "تم تجهيز مسودة التقويم محليًا؛ لم يُنشأ حدث في Google بعد.";
    });
  });
  emailForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!emailReview.checked) { report("يرجى مراجعة المستلم والموعد وتأكيد تجهيز المسودة.", true); return; }
    const address = recipient.value.trim();
    if (!address || !recipient.checkValidity()) { report("أدخل عنوان بريد صحيحًا لمستلم واحد.", true); return; }
    perform(async () => {
      await api(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: "email", recipient: address, confirmed: true }) });
      recipient.value = ""; emailReview.checked = false;
      return "تم تجهيز مسودة الرسالة محليًا؛ لم تُرسل رسالة بعد.";
    });
  });
  async function reloadDeliveries() {
    if (busy || loading) return;
    loading = true; synchronizeControls(); report("جارٍ تحميل سجل المسودات...");
    try { await refresh(); if (details.isConnected) report(""); }
    catch (error) { if (!isStale(error) && details.isConnected) report(error instanceof TypeError ? "تعذر الاتصال بالخادم." : error.message, true); }
    finally { loading = false; if (details.isConnected) synchronizeControls(); }
  }
  reload.addEventListener("click", reloadDeliveries);
  details.addEventListener("toggle", async () => {
    if (!details.open || loaded || loading) return;
    await reloadDeliveries();
  });
  synchronizeControls();
}

function renderInterviews(interviews, role, applications, slots) {
  const target = document.getElementById("interviews-area");
  const box = section(role === "hr" ? "المقابلات" : "مقابلاتي");
  box.append(element("p", "subtle-note", "المواعيد مسجلة محليًا. يمكن إضافة ملف التقويم يدويًا، ولن يتحدث تلقائيًا عند تغيير الموعد أو إلغائه. إجراءات Google، عند تهيئتها، تتطلب مراجعة وتأكيدًا منفصلين من HR."));
  const list = element("div", "data-list");
  if (!interviews.length) list.append(element("p", "empty-note", "لا توجد مقابلات مسجلة حتى الآن."));
  for (const interview of interviews) {
    const card = element("article", "job-card");
    const interviewStatus = interview.completed ? "تم تسجيل إجرائها" : interview.status === "scheduled" ? "مجدولة" : interview.status === "cancelled" ? "ملغاة" : "حالة غير معروفة";
    card.append(element("h4", "", `مقابلة الطلب #${interview.application_id}`), element("p", "", `من ${displayDate(interview.start_at)} إلى ${displayDate(interview.end_at)}`), element("p", "", `المحاور: ${interview.interviewer}`), element("p", "", `الحالة: ${interviewStatus}`));
    if (interview.status === "scheduled") {
      const calendar = element("button", "outline-button", "تنزيل ملف التقويم"); calendar.type = "button";
      calendar.addEventListener("click", async () => {
        calendar.disabled = true; const generation = screenGeneration;
        try {
          const response = await fetch(`/api/interviews/${encodeURIComponent(interview.id)}/calendar`, { credentials: "same-origin", cache: "no-store" });
          if (generation !== screenGeneration || !card.isConnected) return;
          if (response.status === 401) { showLogin("انتهت الجلسة. سجل الدخول مرة أخرى."); return; }
          if (!response.ok) throw new Error(response.status === 403 ? "ليس لديك صلاحية لتنزيل هذا الموعد." : response.status === 409 ? "تعذّر إنشاء ملف التقويم لهذا الموعد. حدّث القائمة وتحقق من حالته ووقته." : "تعذّر تنزيل ملف التقويم. حدّث القائمة وحاول مرة أخرى.");
          if (!response.headers.get("content-type")?.toLowerCase().startsWith("text/calendar")) throw new Error("تعذّر قراءة ملف التقويم من الخادم.");
          const blob = await response.blob();
          if (generation !== screenGeneration || !card.isConnected) return;
          const url = URL.createObjectURL(blob), link = element("a"); link.href = url; link.download = `interview-${interview.id}.ics`;
          document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
          operationsMessage("تم تنزيل الملف؛ أضفه يدويًا إلى تقويمك. لن يتحدث تلقائيًا بعد تغيير الموعد أو إلغائه.");
        } catch (error) { if (generation === screenGeneration && card.isConnected) operationsMessage(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
        finally { calendar.disabled = false; }
      });
      card.append(calendar);
    }
    if (role === "hr" && interview.status === "scheduled") {
      if (!interview.completed && new Date(interview.end_at).valueOf() <= Date.now()) {
        const complete = element("button", "small-button", "تأكيد إجراء المقابلة"); complete.type = "button";
        complete.addEventListener("click", async () => {
          complete.disabled = true;
          try {
            await api(`/api/interviews/${encodeURIComponent(interview.id)}/complete`, { method: "POST" });
            if (await loadOperations(role)) operationsMessage("تم تسجيل إجراء المقابلة.");
          } catch (error) {
            if (!isStale(error)) operationsMessage(error instanceof TypeError ? "تعذّر الاتصال بالخادم. حدّث القائمة للتحقق من الحالة." : error.message, true);
          } finally { complete.disabled = false; }
        });
        card.append(complete);
      }
      if (!interview.completed && new Date(interview.start_at).valueOf() > Date.now()) {
        const cancel = element("button", "outline-button", "إلغاء الموعد"); cancel.type = "button";
        cancel.addEventListener("click", async () => {
          cancel.disabled = true;
          try {
            await api(`/api/interviews/${encodeURIComponent(interview.id)}/cancel`, { method: "POST" });
            if (await loadOperations(role)) operationsMessage("تم إلغاء الموعد المحلي.");
          } catch (error) {
            if (!isStale(error)) operationsMessage(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true);
          } finally { cancel.disabled = false; }
        });
        card.append(cancel);
      }
      setupGoogleDeliveries(card, interview);
    }
    if (role === "hr" && interview.status === "cancelled") setupCancellationNotice(card, interview);
    list.append(card);
  }
  box.append(list);
  if (role === "hr") renderSlotsManagement(box, slots, role);
  if (role === "hr") renderBulkSlots(box, applications, role);
  if (role === "hr" && applications.length) {
    const form = element("form", "interview-form");
    const jobLabel = element("label", "", "الطلب");
    const select = element("select"); select.required = true;
    const placeholder = element("option", "", "اختر طلبًا"); placeholder.value = ""; select.append(placeholder);
    for (const application of applications) { const option = element("option", "", `طلب #${application.id} · ${application.job_id}`); option.value = application.id; select.append(option); }
    jobLabel.append(select);
    const startLabel = element("label", "", "بداية المقابلة"); const start = element("input"); start.type = "datetime-local"; start.required = true; startLabel.append(start);
    const endLabel = element("label", "", "نهاية المقابلة"); const end = element("input"); end.type = "datetime-local"; end.required = true; endLabel.append(end);
    const interviewerLabel = element("label", "", "اسم المحاور"); const interviewer = element("input"); interviewer.required = true; interviewer.maxLength = 120; interviewerLabel.append(interviewer);
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "توقيت الجهاز";
    const button = element("button", "small-button", "تسجيل المقابلة محليًا"); button.type = "submit";
    form.append(element("h4", "", "إضافة موعد"), element("p", "subtle-note", `الوقت المدخل حسب المنطقة الزمنية لجهازك: ${timezone}.`), jobLabel, startLabel, endLabel, interviewerLabel, button);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const startTime = new Date(start.value); const endTime = new Date(end.value);
      if (!select.value || !interviewer.value.trim() || Number.isNaN(startTime.valueOf()) || Number.isNaN(endTime.valueOf()) || endTime <= startTime) { operationsMessage("تحقق من الطلب والاسم وأن نهاية المقابلة بعد بدايتها.", true); return; }
      button.disabled = true;
      try {
        await api("/api/interviews", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ application_id: Number(select.value), start_at: startTime.toISOString(), end_at: endTime.toISOString(), interviewer: interviewer.value.trim() }) });
        if (await loadOperations(role)) operationsMessage("تم تسجيل الموعد محليًا.");
      } catch (error) { if (!isStale(error)) operationsMessage(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
      finally { button.disabled = false; }
    });
    box.append(form);
  }
  target.replaceChildren(box);
}

function renderBulkSlots(target, applications, role) {
  const eligible = applications.filter((item) => item.status === "advanced");
  const area = element("section", "application-detail");
  area.setAttribute("aria-label", "جدولة مواعيد جماعية");
  area.append(element("h4", "", "اقتراح مواعيد لعدة طلبات"), element("p", "subtle-note", "كل موعد مخصص للطلب المحدد، ولا يُحجز إلا عندما يختاره المرشح. تجهيز المواعيد محلي ولا يرسل بريدًا أو ينشئ حدث Google."));
  if (!eligible.length) { area.append(element("p", "empty-note", "لا توجد طلبات مؤهلة حاليًا.")); target.append(area); return; }
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "توقيت الجهاز";
  area.append(element("p", "subtle-note", `الأوقات المدخلة حسب المنطقة الزمنية للجهاز: ${timezone}.`));
  const rows = element("div"); const message = element("p", "subtle-note"); message.setAttribute("aria-live", "polite");
  const preview = element("div"); preview.setAttribute("aria-live", "polite");
  const add = element("button", "outline-button", "إضافة طلب آخر"); add.type = "button";
  const inspect = element("button", "small-button", "مراجعة المقترح"); inspect.type = "button";
  let busy = false, commitUncertain = false;
  function clearPreview() { preview.replaceChildren(); }
  function addRow() {
    if (busy || rows.childNodes.length >= 20) return;
    clearPreview();
    const row = element("div", "decision-form");
    const applicationLabel = element("label", "", "الطلب المؤهل"); const application = element("select");
    for (const item of eligible) { const option = element("option", "", `طلب #${item.id} · ${item.job_id}`); option.value = String(item.id); application.append(option); }
    applicationLabel.append(application);
    const startLabel = element("label", "", "البداية"); const start = element("input"); start.type = "datetime-local"; startLabel.append(start);
    const endLabel = element("label", "", "النهاية"); const end = element("input"); end.type = "datetime-local"; endLabel.append(end);
    const interviewerLabel = element("label", "", "المحاور"); const interviewer = element("input"); interviewer.maxLength = 120; interviewerLabel.append(interviewer);
    const remove = element("button", "outline-button", "حذف هذا الطلب من المقترح"); remove.type = "button";
    remove.addEventListener("click", () => { if (!busy) { row.remove(); clearPreview(); add.disabled = false; } });
    for (const input of [application, start, end, interviewer]) input.addEventListener("input", clearPreview);
    row.append(applicationLabel, startLabel, endLabel, interviewerLabel, remove);
    row._bulkFields = { application, start, end, interviewer, remove };
    rows.append(row); add.disabled = rows.childNodes.length >= 20;
  }
  function setBusy(value) {
    busy = value; inspect.disabled = value || commitUncertain; add.disabled = value || commitUncertain || rows.childNodes.length >= 20;
    for (const row of rows.children) for (const control of Object.values(row._bulkFields)) control.disabled = value || commitUncertain;
  }
  add.addEventListener("click", addRow);
  inspect.addEventListener("click", async () => {
    if (busy) return; clearPreview(); message.className = "subtle-note";
    const items = []; const selected = new Set();
    for (const row of rows.children) {
      const { application, start, end, interviewer } = row._bulkFields;
      const applicationId = Number(application.value), from = new Date(start.value), until = new Date(end.value), name = interviewer.value.trim();
      if (!Number.isSafeInteger(applicationId) || applicationId <= 0 || selected.has(applicationId) || !name || Number.isNaN(from.valueOf()) || Number.isNaN(until.valueOf()) || from <= new Date() || until <= from) {
        message.className = "form-error"; message.textContent = "اختر طلبات مختلفة وتحقق من المحاور وأوقات مستقبلية صحيحة لكل طلب."; return;
      }
      selected.add(applicationId); items.push({ application_id: applicationId, start_at: from.toISOString(), end_at: until.toISOString(), interviewer: name });
    }
    if (!items.length) { message.className = "form-error"; message.textContent = "أضف طلبًا واحدًا على الأقل."; return; }
    setBusy(true); message.textContent = "جارٍ مراجعة المقترح والتحقق من التعارض...";
    try {
      const proposal = await api("/api/slots/batches/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items }) });
      if (!preview.isConnected) return;
      if (!proposal.proposal_id || !/^[a-f0-9]{64}$/.test(proposal.fingerprint || "") || !Array.isArray(proposal.items) || proposal.items.length !== items.length || proposal.items.some((item, index) => item.application_id !== items[index].application_id || !Number.isSafeInteger(item.candidate_id) || item.candidate_id <= 0 || !item.start_at || !item.end_at || !item.interviewer)) throw new Error("تعذّرت قراءة المقترح بأمان؛ أعد المحاولة.");
      const list = element("ol", "evidence-list");
      for (const item of proposal.items) list.append(element("li", "", `طلب #${item.application_id} · مرشح #${item.candidate_id} · ${displayDate(item.start_at)} – ${displayDate(item.end_at)} · ${item.interviewer}`));
      const confirmLabel = element("label", "", "راجعت جميع الطلبات والمواعيد والمحاورين وأوافق على إتاحة هذه المواعيد الخاصة");
      const confirm = element("input"); confirm.type = "checkbox"; confirmLabel.prepend(confirm);
      const commit = element("button", "small-button", "إتاحة المواعيد بعد المراجعة"); commit.type = "button";
      commit.addEventListener("click", async () => {
        if (!confirm.checked || busy) { message.className = "form-error"; message.textContent = "أكد مراجعة جميع التفاصيل أولًا."; return; }
        setBusy(true); commit.disabled = true; confirm.disabled = true; message.className = "subtle-note"; message.textContent = "جارٍ تسجيل المواعيد...";
        try {
          await api(`/api/slots/batches/${encodeURIComponent(proposal.proposal_id)}/commit`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fingerprint: proposal.fingerprint }) });
          if (await loadOperations(role)) operationsMessage("أُتيحت المواعيد للطلبات المحددة؛ يختارها المرشحون بأنفسهم.");
        } catch (error) {
          if (!isStale(error) && preview.isConnected) {
            message.className = "form-error";
            commitUncertain = error instanceof TypeError;
            message.textContent = commitUncertain ? "تعذّر الاتصال؛ حدّث قائمة المواعيد للتحقق قبل إعادة المحاولة." : error.message;
            clearPreview();
            if (commitUncertain) {
              const refresh = element("button", "outline-button", "تحديث قائمة المواعيد للتحقق"); refresh.type = "button";
              refresh.addEventListener("click", async () => {
                refresh.disabled = true;
                try { if (!await loadOperations(role) && area.isConnected) { message.textContent = "تعذّر تحديث المواعيد؛ حاول التحديث مجددًا."; refresh.disabled = false; } }
                catch (refreshError) { if (!isStale(refreshError) && area.isConnected) { message.textContent = "تعذّر تحديث المواعيد؛ حاول التحديث مجددًا."; refresh.disabled = false; } }
              });
              preview.append(refresh);
            }
          }
        } finally { setBusy(false); }
      });
      preview.replaceChildren(element("h5", "", "راجع المواعيد الخاصة قبل الإتاحة"), list, element("p", "subtle-note", "هذه ليست حجوزات مؤكدة؛ سيختار كل مرشح موعد طلبه بنفسه."), confirmLabel, commit);
      message.textContent = "راجع المقترح ثم أكد الإتاحة.";
    } catch (error) { if (!isStale(error) && area.isConnected) { message.className = "form-error"; message.textContent = error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message; } }
    finally { setBusy(false); }
  });
  area.append(rows, add, inspect, message, preview); addRow(); target.append(area);
}

function renderSlotsManagement(target, slots, role) {
  const area = element("section", "application-detail");
  area.append(element("h4", "", "المواعيد التي يمكن للمرشحين اختيارها"));
  area.append(element("p", "subtle-note", "تُدار هذه المواعيد محليًا، ولا تُرسل دعوات أو رسائل بريد إلكتروني."));
  if (!Array.isArray(slots) || !slots.length) area.append(element("p", "empty-note", "لم تُضف مواعيد متاحة بعد."));
  else {
    const list = element("ul", "evidence-list");
    for (const slot of slots) {
      const startTime = new Date(slot.start_at);
      const future = !Number.isNaN(startTime.valueOf()) && startTime > new Date();
      const label = slot.withdrawn === true ? "مسحوب" : slot.booked === true ? "محجوز" :
        slot.withdrawn !== false || slot.booked !== false || Number.isNaN(startTime.valueOf()) ? "الحالة غير متاحة" :
          future ? "متاح" : "انتهى الموعد";
      const visibility = slot.target_application_id ? ` · مخصص للطلب #${slot.target_application_id}` : " · متاح للمؤهلين";
      const item = element("li", "", `${displayDate(slot.start_at)} – ${displayDate(slot.end_at)} · ${slot.interviewer} · ${label}${visibility}`);
      if (slot.withdrawn === false && slot.booked === false && future) {
        const withdraw = element("button", "small-button", "سحب الموعد المتاح"); withdraw.type = "button";
        withdraw.setAttribute("aria-label", `سحب موعد ${displayDate(slot.start_at)} مع ${slot.interviewer}`);
        withdraw.addEventListener("click", async () => {
          withdraw.disabled = true;
          try {
            await api(`/api/slots/${encodeURIComponent(slot.id)}/withdraw`, { method: "POST" }, {
              errorMessages: { 409: "تعذّر سحب الموعد؛ قد يكون حُجز أو انتهى أو سُحب بالفعل." }
            });
            if (await loadOperations(role)) operationsMessage("تم سحب الموعد المتاح؛ لن يظهر للمرشحين.");
          } catch (error) {
            if (!isStale(error)) {
              const message = error instanceof TypeError ? "تعذّر الاتصال بالخادم؛ راجع حالة الموعد قبل المحاولة." : error.message;
              await loadOperations(role);
              operationsMessage(message, true);
            }
          } finally { withdraw.disabled = false; }
        });
        item.append(withdraw);
      }
      list.append(item);
    }
    area.append(list);
  }
  const form = element("form", "interview-form");
  const startLabel = element("label", "", "بداية الموعد المتاح");
  const start = element("input"); start.type = "datetime-local"; start.required = true; startLabel.append(start);
  const endLabel = element("label", "", "نهاية الموعد المتاح");
  const end = element("input"); end.type = "datetime-local"; end.required = true; endLabel.append(end);
  const interviewerLabel = element("label", "", "اسم المحاور");
  const interviewer = element("input"); interviewer.required = true; interviewer.maxLength = 120; interviewerLabel.append(interviewer);
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "توقيت الجهاز";
  const add = element("button", "small-button", "إضافة موعد متاح"); add.type = "submit";
  form.append(element("p", "subtle-note", `الوقت المدخل حسب المنطقة الزمنية لجهازك: ${timezone}.`), startLabel, endLabel, interviewerLabel, add);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const startTime = new Date(start.value); const endTime = new Date(end.value);
    if (!interviewer.value.trim() || Number.isNaN(startTime.valueOf()) || Number.isNaN(endTime.valueOf()) || endTime <= startTime || startTime <= new Date()) {
      operationsMessage("تحقق من المحاور وأن الموعد في المستقبل ونهايته بعد بدايته.", true); return;
    }
    add.disabled = true;
    try {
      await api("/api/slots", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        start_at: startTime.toISOString(), end_at: endTime.toISOString(), interviewer: interviewer.value.trim()
      }) });
      if (await loadOperations(role)) operationsMessage("تمت إضافة موعد متاح للمرشحين محليًا.");
    } catch (error) {
      if (!isStale(error)) operationsMessage(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true);
    } finally { add.disabled = false; }
  });
  area.append(form); target.append(area);
}

async function restoreSession() {
  try {
    const response = await fetch("/api/auth/me", { credentials: "same-origin", cache: "no-store" });
    if (response.status === 401) { showLogin(); return; }
    if (!response.ok) { showLogin("تعذّر التحقق من الجلسة. حاول تسجيل الدخول."); return; }
    showWorkspace(await response.json());
  } catch { showLogin("تعذّر الاتصال بالخادم. تأكد من تشغيل التطبيق."); }
}

restoreSession();
