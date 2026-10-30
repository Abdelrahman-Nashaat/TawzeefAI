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
  setupKnowledge();
  setupConversations();
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

const statusLabels = { submitted: "تم التقديم", advanced: "المرحلة التالية", rejected: "مرفوض", hired: "تم التوظيف" };
const documentErrors = {
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
      const button = element("button", "small-button", "التقديم لهذه الوظيفة"); button.type = "submit";
      form.append(label, button);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const file = input.files?.[0];
        if (!file) { announce("اختر ملف السيرة الذاتية أولًا.", true); return; }
        if (file.size > 5 * 1024 * 1024) { announce("حجم الملف أكبر من 5 ميجابايت.", true); return; }
        button.disabled = true;
        try {
          const params = new URLSearchParams({ job_id: job.id, filename: file.name });
          await api(`/api/applications?${params}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
          if (await loadRecruitment(role)) announce("تم إرسال الطلب بنجاح.");
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
    }
    list.append(card);
  }
  box.append(list);
  if (role === "hr") box.append(createJobForm(role));
  target.replaceChildren(box);
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
  box.append(list); target.replaceChildren(box);
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
  const evaluation = element("form", "decision-form");
  const judgmentList = element("div");
  const save = element("button", "small-button", "حفظ التقييم البشري"); save.type = "submit";
  const result = element("div"); result.setAttribute("aria-live", "polite");
  evaluation.append(judgmentList, save);
  target.replaceChildren(heading, notice, message, element("h4", "", "معايير الوظيفة"), criterionForm,
    element("h4", "", "أدلة المراجعة"), privacyNote, sourceLabel, addEvidence, suggestEvidence, suggestions, evidenceList,
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
      const response = await api(`${base}/suggestions`, { method: "POST" });
      if (!target.isConnected) return;
      if (revision !== criteriaRevision) {
        suggestions.replaceChildren(element("p", "form-error", "تغيرت المعايير أثناء البحث؛ اطلب الاقتراحات مجددًا."));
        return;
      }
      if (response.offset_unit !== "unicode_code_points" || !Array.isArray(response.criteria) || !/^[a-f0-9]{64}$/.test(response.source_sha256 || "")) {
        throw new Error("تعذّر قراءة الاقتراحات؛ حاول مرة أخرى.");
      }
      const sourcePoints = Array.from(source.text);
      const allowed = new Map(criteria.map((criterion) => [criterion.criterion_ref, criterion]));
      suggestions.replaceChildren(element("p", "subtle-note", "المقتطفات مقترحة بالتشابه فقط، وقد تكون غير مناسبة أو منفية. راجع السياق بنفسك؛ لا تُثبت الاقتراحات المعيار ولا تغيّر التقييم أو قرار التوظيف."));
      suggestions.append(element("p", "subtle-note", "يبحث الاقتراح في الأقسام التي تم التعرف عليها من السيرة فقط؛ غياب النتائج لا يثبت غياب الخبرة."));
      if (response.status === "no_recognized_sections") suggestions.append(element("p", "form-error", "تعذّر التعرف على أقسام السيرة للبحث فيها. راجع السيرة كاملة وحدد الأدلة يدويًا."));
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
          if (Number.isFinite(similarity)) item.append(element("p", "subtle-note", `درجة التشابه: ${similarity.toFixed(2)}؛ ليست تقييمًا للمؤهل.`));
          const accept = element("button", "outline-button", "حفظ المقتطف بعد مراجعته"); accept.type = "button";
          accept.addEventListener("click", async () => {
            if (revision !== criteriaRevision || !target.isConnected || !accept.isConnected) {
              setMessage("تغيرت المعايير؛ اطلب الاقتراحات مجددًا.", true);
              return;
            }
            accept.disabled = true;
            try {
              const saved = await api(`${base}/evidence`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ start, end, expected_source_sha256: response.source_sha256,
                expected_criterion: { criterion_ref: criterion.criterion_ref, category: criterion.category, label: criterion.label } }) });
              if (!target.isConnected) return;
              rememberDrafts(); evidence.push(saved); renderEvidence(); renderJudgments();
              setMessage("تم حفظ المقتطف كدليل؛ اربطه بالمعيار وحدد حكمك بعد المراجعة.");
              accept.textContent = "تم حفظ المقتطف";
            } catch (error) {
              if (!isStale(error) && target.isConnected) setMessage(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true);
              accept.disabled = false;
            }
          });
          item.append(accept); group.append(item);
        }
        suggestions.append(group);
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
      const item = await api(`${base}/evidence`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ start, end }) });
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
      assessment = await api(`${base}/assessment`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ judgments }) });
      if (!target.isConnected) return;
      renderResult(assessment); setMessage("تم حفظ التقييم البشري دون تغيير قرار الطلب.");
    } catch (error) { if (!isStale(error) && target.isConnected) setMessage(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
    finally { save.disabled = false; }
  });
}

function setupGraphReview(target, application) {
  const storageKey = `tawzeef-graph-${currentUserId}-${application.id}`;
  const title = element("h4", "", "مراجعة متعددة المراحل باستخدام LangGraph");
  const note = element("p", "subtle-note", "تجمع المراجعة نتائج أدوات محلية وتتوقف لتأكيد بشري. عند تفعيل Groq يختار المساعد أدوات قراءة محددة؛ لا تتخذ المراجعة قرار توظيف أو تحجز موعدًا أو ترسل دعوة. المواعيد لقطة قد تتغير.");
  const message = element("p", "subtle-note"); message.setAttribute("aria-live", "polite");
  const form = element("form", "decision-form");
  const questionLabel = element("label", "", "سؤال سياسة اختياري للبحث المحلي");
  const question = element("textarea"); question.maxLength = 2000; question.placeholder = "يمكن تركه فارغًا"; questionLabel.append(question);
  const start = element("button", "small-button", "بدء مراجعة المراحل"); start.type = "submit";
  form.append(questionLabel, element("p", "subtle-note", "إذا أدخلت سؤالًا، تُسجَّل حالة البحث فقط دون السؤال أو نتائج البحث. استخدم قسم المعرفة للحصول على إجابة ومصادرها."), start);
  const restore = element("form", "decision-form");
  const idLabel = element("label", "", "معرّف مراجعة سابقة لهذا الطلب");
  const idInput = element("input"); idInput.maxLength = 100; idLabel.append(idInput);
  const open = element("button", "outline-button", "تحميل المراجعة"); open.type = "submit";
  restore.append(idLabel, open);
  const output = element("div"); output.setAttribute("aria-live", "polite");
  target.replaceChildren(title, note, message, form, restore, output);
  const labels = { parser: "قراءة النص المستخرج", matching: "فحص المهارات المذكورة", policy: "بحث السياسة المحلي", scheduler: "استعراض المواعيد المحلية", status: "حالة الطلب", agent_review: "مساعد أدوات القراءة", human_review: "مراجعة بشرية" };
  function report(text, error = false) { message.textContent = text; message.className = error ? "form-error" : "subtle-note"; }
  function remember(id) { try { sessionStorage.setItem(storageKey, id); } catch { /* Manual restore remains available. */ } }
  function display(review) {
    if (review.state?.application_id !== application.id) throw new Error("هذه المراجعة لا تخص الطلب المعروض.");
    idInput.value = review.id; remember(review.id);
    output.replaceChildren(element("p", "subtle-note", `معرّف المراجعة: ${review.id}`));
    const plan = element("ol", "evidence-list");
    for (const step of review.state.plan || []) plan.append(element("li", "", labels[step] || step));
    output.append(element("h4", "", "مراحل المراجعة"), plan);
    const parsed = review.state.parsed;
    if (parsed) output.append(element("p", "", parsed.text_available ? "يتوفر نص مستخرج؛ تحليل الأقسام تقديري ويحتاج مراجعة في قسم السيرة المنظمة." : "لا يتوفر نص مستخرج من الملف."));
    if (review.state.matching) { const score = element("div"); renderScore(score, review.state.matching); output.append(score); }
    const policy = review.state.policy?.status;
    if (policy) output.append(element("p", "", policy === "searched_without_retaining_results" ? "أُجري بحث محلي في السياسات دون حفظ نتائجه؛ الإجابة والمصادر في قسم المعرفة." : policy === "not_requested" ? "لم يُطلب بحث في السياسات." : "تفاصيل بحث السياسات غير متاحة."));
    const scheduling = review.state.scheduling;
    if (scheduling) {
      output.append(element("p", "", scheduling.eligible ? "الطلب مؤهل لاستعراض المواعيد المحلية. الحجز إجراء منفصل." : "لا توجد أهلية حاليًا لاستعراض مواعيد الحجز للطلب."));
      if (scheduling.available_slots?.length) {
        const slots = element("ul", "evidence-list");
        for (const slot of scheduling.available_slots) slots.append(element("li", "", `من ${displayDate(slot.start_at)} إلى ${displayDate(slot.end_at)}`));
        output.append(slots);
      }
    }
    if (review.state.status) output.append(element("p", "", `الحالة الحالية للطلب: ${statusLabels[review.state.status.application_status] || review.state.status.application_status}`));
    const agent = review.state.agent_review;
    if (agent) {
      const agentStatus = { ok: "اكتملت قراءة الأدوات", disabled: "المساعد غير مفعّل", unavailable: "المساعد غير متاح", rejected: "رُفض رد المساعد", limit_reached: "وصل المساعد للحد المسموح" };
      output.append(element("p", "", `مساعد أدوات القراءة: ${agentStatus[agent.status] || "حالة غير معروفة"}.`));
      const toolLabels = { job_requirements: "متطلبات الوظيفة", workflow_status: "حالة سير الطلب", screening_evidence: "مؤشرات الفحص" };
      if (agent.tool_trace?.length) output.append(element("p", "subtle-note", `الأدوات المستخدمة: ${agent.tool_trace.map((tool) => toolLabels[tool] || "أداة غير معروفة").join("، ")}.`));
    }
    if (review.awaiting_review) {
      output.append(element("p", "result-badge", "توقفت المراجعة بانتظار التأكيد البشري."));
      const resume = element("button", "small-button", "أؤكد أنني راجعت النتائج واستكمل المراجعة"); resume.type = "button";
      resume.addEventListener("click", async () => {
        resume.disabled = true; start.disabled = true; open.disabled = true; report("جارٍ استكمال المراجعة...");
        try {
          const updated = await api(`/api/graph-reviews/${encodeURIComponent(review.id)}/resume`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reviewed: true }) });
          if (target.isConnected) { display(updated); report("اكتملت مراجعة المراحل دون تغيير قرار الطلب أو حجز موعد."); }
        } catch (error) { if (!isStale(error) && target.isConnected) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
        finally { resume.disabled = false; start.disabled = false; open.disabled = false; }
      });
      output.append(resume);
    } else output.append(element("p", "result-badge", review.state.reviewed === true ? "اكتملت المراجعة بتأكيد بشري." : "انتهت المراجعة دون تأكيد بأن النتائج روجعت."));
  }
  async function load(id) {
    const review = await api(`/api/graph-reviews/${encodeURIComponent(id)}`);
    if (target.isConnected) { display(review); report(""); }
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); start.disabled = true; open.disabled = true; output.replaceChildren(); report("جارٍ تنفيذ المراحل حتى نقطة المراجعة البشرية...");
    try {
      const review = await api("/api/graph-reviews", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ application_id: application.id, policy_question: question.value.trim() }) });
      if (target.isConnected) { question.value = ""; display(review); report(""); }
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

async function loadStructuredResume(target, applicationId) {
  target.replaceChildren(element("h4", "", "الأقسام المستخرجة من السيرة"), element("p", "subtle-note", "جارٍ تحميل الأقسام..."));
  try {
    const result = await api(`/api/applications/${encodeURIComponent(applicationId)}/resume/structured`);
    if (!target.isConnected) return;
    target.replaceChildren(element("h4", "", "الأقسام المستخرجة من السيرة"),
      element("p", "subtle-note", "الاستخراج يعتمد على عناوين الأقسام الظاهرة فقط؛ النصوص المذكورة غير مُتحقق منها، وقد يُحذف أو يختلط ترتيب بعض المحتوى. عدم ظهور قسم يعني أنه غير معروف، لا أنه غير موجود."),
      element("p", "form-error", "تنقيح الهوية جزئي وقد تبقى معلومات تعريفية أو تُحذف معلومات مفيدة؛ راجع النص يدويًا."));
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

function renderApplicationDetail(target, application, role) {
  const panel = element("div", "application-detail");
  panel.append(element("h4", "", `تفاصيل الطلب #${application.id}`));
  panel.append(element("p", "", `الحالة: ${statusLabels[application.status] || application.status}`));
  panel.append(element("p", "", `تاريخ التقديم: ${application.created_at}`));
  if (role === "candidate" && application.status === "advanced") {
    const slotsPanel = element("section", "application-detail");
    slotsPanel.setAttribute("aria-label", "اختيار موعد مقابلة");
    panel.append(slotsPanel);
    loadCandidateSlots(slotsPanel, application.id);
  }
  if (role === "hr") {
    panel.append(element("p", "", `الملف: ${application.filename}`));
    panel.append(element("h4", "", "النص المستخرج من السيرة"), element("pre", "resume-preview", application.resume_text));
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
    loadStructuredResume(structurePanel, application.id);
    const result = element("div"); panel.append(result); renderScore(result, application.score);
    const rubricPanel = element("section", "application-detail");
    rubricPanel.setAttribute("aria-label", "التقييم الموزون بالمراجعة البشرية");
    panel.append(rubricPanel);
    loadRubric(rubricPanel, application);
    const graphPanel = element("section", "application-detail");
    graphPanel.setAttribute("aria-label", "مراجعة LangGraph");
    panel.append(graphPanel);
    setupGraphReview(graphPanel, application);
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
      try { renderScore(result, await api(`/api/applications/${encodeURIComponent(application.id)}/screen`, { method: "POST" })); announce("تم تسجيل الفحص."); await loadWorkflow(workflowPanel, application.id, target, role); await loadOperations(role); }
      catch (error) { if (!isStale(error)) announce(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
      finally { screen.disabled = false; }
    });
    panel.append(screen);
    const form = element("form", "decision-form");
    const label = element("label", "", "قرار المراجع البشري");
    const select = element("select");
    for (const [value, text] of [["", "اختر القرار"], ["advance", "نقل للمرحلة التالية"], ["reject", "رفض الطلب"], ["hire", "توظيف"]]) { const option = element("option", "", text); option.value = value; select.append(option); }
    select.required = true; label.append(select);
    const noteLabel = element("label", "", "ملاحظة القرار");
    const note = element("textarea"); note.maxLength = 1000; note.placeholder = "أضف سبب القرار أو ملاحظة للمراجعة"; noteLabel.append(note);
    const submit = element("button", "small-button", "تسجيل القرار"); submit.type = "submit";
    form.append(label, noteLabel, submit);
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

async function loadCandidateSlots(target, applicationId) {
  target.replaceChildren(element("p", "subtle-note", "جاري تحميل المواعيد المتاحة..."));
  try {
    const slots = await api(`/api/slots?${new URLSearchParams({ application_id: applicationId })}`);
    if (!target.isConnected) return;
    target.replaceChildren(
      element("h4", "", "اختيار موعد مقابلة"),
      element("p", "subtle-note", "الحجز محلي داخل التطبيق فقط؛ لن تُرسل دعوة أو رسالة بريد إلكتروني.")
    );
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
            await loadCandidateSlots(target, applicationId);
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
  const note = element("p", "subtle-note", "خطة ثابتة تعتمد على حالة الطلب؛ المراجع يقرر ويحجز المقابلات يدويًا.");
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
  } else if (state.next_step !== "complete") {
    target.append(element("p", "subtle-note", "هذه الخطوة تحتاج إجراءً من موظف الموارد البشرية."));
  }
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
    const rows = [];
    for (const document of documents) {
      const card = element("article", "job-card");
      card.append(element("h4", "", document.title), element("p", "", `الصلاحية: ${labels[document.audience] || "غير معروفة"}`));
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
    form.append(titleLabel, audienceLabel);
    return { titleInput, audience };
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
      await api("/api/knowledge/documents/text", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: documentTitle, audience: textFields.audience.value, text: documentText }) });
      textForm.reset();
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
  const fileButton = element("button", "small-button", "رفع المستند"); fileButton.type = "submit";
  fileForm.append(fileLabel, element("p", "subtle-note", "الملفات المصوّرة تحتاج OCR غير متاح حاليًا."), fileButton);
  fileForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = fileInput.files?.[0];
    const documentTitle = fileFields.titleInput.value.trim();
    if (!file || !documentTitle || !fileFields.audience.value) { report("أكمل العنوان والملف والصلاحية.", true); return; }
    if (file.size > 5 * 1024 * 1024) { report("حجم الملف أكبر من 5 ميجابايت.", true); return; }
    fileButton.disabled = true;
    try {
      const params = new URLSearchParams({ title: documentTitle, audience: fileFields.audience.value, filename: file.name });
      const uploaded = await api(`/api/knowledge/documents?${params}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
      fileForm.reset();
      const pages = Number.isInteger(uploaded.page_count)
        ? ` رُفع الملف: ${uploaded.pages_with_text} من ${uploaded.page_count} صفحة تحتوي نصًا قابلًا للاستخراج. الصفحات المصوّرة لا تُفهرس لأن OCR غير متاح.` : "";
      report(`رُفع المستند وحُفظ نصه.${pages}`);
      try { await refresh(); }
      catch (error) { if (!isStale(error)) report("رُفع المستند، لكن تعذّر تحديث القائمة. أعد تحميل الصفحة.", true); }
    } catch (error) { if (!isStale(error)) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
    finally { fileButton.disabled = false; }
  });

  panel.append(message, textForm, fileForm, element("h3", "", "المستندات المضافة"), list);
  sectionNode.append(heading, panel);
  document.getElementById("knowledge-form").closest("section").after(sectionNode);
  refresh().catch((error) => { if (!isStale(error)) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); });
}

function knowledgeSourceLabel(source) {
  const page = Number.isInteger(source.page) && source.page > 0 ? ` · صفحة ${source.page}` : "";
  return `${source.title} (${source.id})${page}`;
}

function setupKnowledge() {
  const form = document.getElementById("knowledge-form");
  const result = document.getElementById("knowledge-result");
  const submit = form.querySelector("button");
  let hybridButton = null;
  api("/api/knowledge/hybrid-status").then((status) => {
    if (!status.configured || !form.isConnected) return;
    hybridButton = element("button", "outline-button", "تجربة البحث الهجين");
    hybridButton.type = "button";
    hybridButton.disabled = submit.disabled;
    form.append(element("p", "subtle-note", "تجربة محلية على مستندات السياسات المضافة فقط؛ المقتطفات المرشحة قد لا تدعم السؤال، ولا تمثل إجابة متحققًا منها."), hybridButton);
    hybridButton.addEventListener("click", async () => {
      const question = document.getElementById("knowledge-question").value.trim();
      if (!question) return;
      submit.disabled = true; hybridButton.disabled = true;
      result.className = "knowledge-result";
      result.replaceChildren(element("p", "", "جاري البحث التجريبي..."));
      try {
        const answer = await api("/api/knowledge/experimental-hybrid", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }) }, { errorMessages: { 429: "البحث مشغول؛ حاول مرة أخرى.", 503: "البحث التجريبي غير متاح؛ تحقق من إعداد النموذج وقاعدة البيانات." } });
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
      } finally { submit.disabled = false; hybridButton.disabled = false; }
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

function setupConversations() {
  const sectionNode = element("section", "knowledge-section");
  sectionNode.setAttribute("aria-label", "محادثاتي المحفوظة");
  const heading = element("div", "section-heading"); heading.append(element("h2", "", "محادثاتي المحفوظة"));
  const panel = element("div", "knowledge-panel");
  const note = element("p", "subtle-note", "يُحفظ السجل لحسابك ويمكنك الرجوع إليه. كل سؤال يُجاب عنه مستقلًا؛ الأسئلة والإجابات السابقة لا تُرسل كسياق للنموذج. راجع المصادر، ولا ترسل بيانات شخصية غير لازمة.");
  const message = element("p", "subtle-note"); message.setAttribute("aria-live", "polite");
  const selectorLabel = element("label", "", "اختر محادثة من سجلك");
  const selector = element("select", "conversation-select"); selectorLabel.append(selector);
  const create = element("button", "outline-button", "بدء محادثة جديدة"); create.type = "button";
  const history = element("div", "knowledge-result"); history.setAttribute("aria-live", "polite");
  const form = element("form");
  const questionLabel = element("label", "", "سؤالك في المحادثة");
  const question = element("textarea"); question.required = true; question.maxLength = 2000; questionLabel.append(question);
  const send = element("button", "small-button", "إرسال السؤال"); send.type = "submit";
  form.append(questionLabel, send);
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
      if (entry.mode === "unavailable") item.append(element("p", "form-error", "هذه الإجابة لم تعد متاحة لأن مصادرها لم تعد متاحة لحسابك أو تغيرت."));
      else {
        item.append(element("p", "", entry.answer));
        const mode = entry.mode === "groq" ? "إجابة مولدة بالذكاء الاصطناعي؛ تحقق من المصادر." : entry.mode === "abstain" ? "لم يُعثر على دليل مناسب." : "مقتطفات مسترجعة من المصادر.";
        item.append(element("p", "subtle-note", mode));
        if (entry.notice) item.append(element("p", "subtle-note", entry.notice));
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
    sendPending = true; send.disabled = true; selector.disabled = true; create.disabled = true; question.disabled = true; report("جارٍ البحث عن إجابة...");
    try {
      await api(`/api/conversations/${encodeURIComponent(id)}/messages`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: text }) });
      if (!sectionNode.isConnected) return;
      if (selected === id) { question.value = ""; await openConversation(id); }
    } catch (error) { if (!isStale(error) && sectionNode.isConnected && selected === id) report(error instanceof TypeError ? "تعذّر الاتصال بالخادم." : error.message, true); }
    finally {
      sendPending = false;
      if (sectionNode.isConnected) { send.disabled = !selected; selector.disabled = false; create.disabled = false; question.disabled = false; }
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

function renderDashboard(data) {
  const target = document.getElementById("dashboard-area");
  const box = section("مؤشرات التوظيف");
  const grid = element("div", "metrics-grid");
  const items = [[data.applications_total, "إجمالي الطلبات"], [data.screening_events, "عمليات فحص مسجلة"], [data.time_to_hire?.average_days == null ? "—" : data.time_to_hire.average_days, "متوسط أيام التوظيف"]];
  for (const [value, label] of items) {
    const card = element("div", "metric-card"); card.append(element("strong", "", String(value)), element("span", "", label)); grid.append(card);
  }
  box.append(grid, element("p", "subtle-note", `متوسط الأيام من وقت التقديم حتى آخر قرار توظيف للطلبات المعينة حاليًا؛ عدد الحالات: ${data.time_to_hire?.count ?? 0}.`));
  const pipeline = element("ul", "evidence-list");
  for (const item of data.pipeline || []) pipeline.append(element("li", "", `${statusLabels[item.status] || item.status}: ${item.count}`));
  if (pipeline.childNodes.length) box.append(element("h3", "", "الطلبات حسب المرحلة"), pipeline);
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
  panel.append(message, reload, list, calendarForm, emailForm); details.append(panel); card.append(details);
  let loaded = false, busy = false, loading = false, deliveries = [];
  const endpoint = `/api/google/interviews/${encodeURIComponent(interview.id)}/deliveries`;
  function report(text, isError = false) { message.textContent = text; message.className = isError ? "form-error" : "subtle-note"; }
  function synchronizeControls() {
    const existing = new Set(deliveries.map((item) => item.kind));
    calendarForm.hidden = existing.has("calendar"); emailForm.hidden = existing.has("email");
    for (const control of panel.querySelectorAll("button, input")) control.disabled = busy || !loaded;
    reload.disabled = busy || loading;
  }
  function display() {
    list.replaceChildren();
    if (!deliveries.length) list.append(element("p", "empty-note", "لا توجد مسودات لهذا الموعد."));
    const stateLabels = { pending: "مسودة بانتظار تأكيد الإجراء", sending: "قيد الإرسال أو تحتاج مراجعة بعد انقطاع", confirmed: "قبلت Google الطلب", rejected: "رفضت Google الطلب", uncertain: "النتيجة غير مؤكدة وتحتاج مراجعة" };
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
      if (delivery.state === "rejected") entry.append(element("p", "form-error", "راجع إعدادات Google وسجل العملية؛ لا توجد إعادة إرسال تلقائية."));
      if (delivery.state === "pending") {
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
                body: JSON.stringify({ confirmed: true, expected_recipient: delivery.recipient, recipient: address })
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
              body: JSON.stringify(delivery.kind === "email" ? { confirmed: true, expected_recipient: delivery.recipient } : { confirmed: true })
            }, { errorMessages: { 409: "لا يمكن إرسال هذه المسودة في حالتها الحالية؛ راجع سجل العملية.", 503: "تعذر تنفيذ الطلب؛ راجع حالة المسودة وربط Google قبل أي إجراء آخر." } });
            if (result.state === "confirmed") return "قبلت Google الطلب. راجع حالة الحدث أو الرسالة في حساب Google.";
            if (result.state === "rejected") return "رفضت Google الطلب؛ راجع إعدادات الحساب وسجل العملية.";
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
    const interviewStatus = interview.status === "scheduled" ? "مجدولة" : interview.status === "cancelled" ? "ملغاة" : "حالة غير معروفة";
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
      setupGoogleDeliveries(card, interview);
    }
    list.append(card);
  }
  box.append(list);
  if (role === "hr") renderSlotsManagement(box, slots, role);
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
      const item = element("li", "", `${displayDate(slot.start_at)} – ${displayDate(slot.end_at)} · ${slot.interviewer} · ${label}`);
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
