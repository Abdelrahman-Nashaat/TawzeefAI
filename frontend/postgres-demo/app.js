"use strict";

const $ = (id) => document.getElementById(id);
let user = null;
let noticeCursor = null;
let sessionGeneration = 0;
class StaleSession extends Error {}
function ensureCurrent(generation) { if (generation !== sessionGeneration) throw new StaleSession(); }

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function message(text) { $("message").textContent = text; }
function signedOut() {
  sessionGeneration++;
  user = null;
  noticeCursor = null;
  $("workspace").hidden = true;
  $("login-panel").hidden = false;
  $("logout").hidden = true;
  $("password").value = "";
  $("resume").value = "";
  $("slot-form").reset();
  for (const id of ["applications", "interviews", "notices"]) $(id).replaceChildren();
}

async function api(path, options = {}) {
  const generation = sessionGeneration;
  let response, data;
  try {
    response = await fetch(path, { credentials: "same-origin", ...options });
    data = response.status === 204 ? null : await response.json();
  } catch (error) {
    if (generation !== sessionGeneration) throw new StaleSession();
    throw error;
  }
  if (generation !== sessionGeneration) throw new StaleSession();
  if (!response.ok) {
    if (response.status === 401) signedOut();
    const detail = data?.detail;
    throw new Error(typeof detail === "string" ? detail : detail?.message || `Request failed (${response.status})`);
  }
  return data;
}

function jsonPost(body) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

async function action(button, work) {
  button.disabled = true;
  message("");
  try { await work(); } catch (error) { if (!(error instanceof StaleSession)) message(error.message || "Request failed"); }
  finally { button.disabled = false; }
}

function button(text, work) {
  const node = element("button", text);
  node.type = "button";
  node.addEventListener("click", () => action(node, work));
  return node;
}

function date(value) { return new Date(value).toLocaleString(); }

async function showSlots(application, container) {
  const generation = sessionGeneration;
  const slots = await api(`/api/slots?application_id=${encodeURIComponent(application.id)}`);
  ensureCurrent(generation);
  const list = element("div");
  if (!slots.length) list.append(element("p", "No available slots."));
  for (const slot of slots) {
    const line = element("p", `${date(slot.start_at)} - ${date(slot.end_at)} | ${slot.interviewer} `);
    line.append(button("Book", async () => {
      const generation = sessionGeneration;
      await api(`/api/slots/${slot.id}/book`, jsonPost({ application_id: application.id }));
      ensureCurrent(generation); await refresh(); ensureCurrent(generation); message("Interview booked locally.");
    }));
    list.append(line);
  }
  container.replaceChildren(list);
}

function renderApplications(applications, jobs) {
  const target = $("applications"); target.replaceChildren();
  if (!applications.length) target.append(element("p", "No applications yet."));
  for (const application of applications) {
    const card = element("article");
    const title = jobs.find((job) => job.id === application.job_id)?.title || application.job_id;
    card.append(element("h3", `${title} - application ${application.id}`));
    card.append(element("p", `Status: ${application.status}`));
    if (user.role === "candidate" && application.status === "advanced") {
      const slots = element("div");
      card.append(button("Find available slots", () => showSlots(application, slots)), slots);
    }
    if (user.role === "hr") {
      card.append(element("p", `Candidate ${application.candidate_id}`));
      const details = element("details"); details.append(element("summary", "Review CV and provisional screening"));
      details.append(element("h4", "Extracted CV"), element("pre", application.resume_text));
      details.append(element("h4", "Provisional screening"), element("pre", application.score ? JSON.stringify(application.score, null, 2) : "Not screened"));
      card.append(details);
      if (application.status === "submitted") {
        card.append(button("Run provisional screening", async () => {
          const generation = sessionGeneration;
          await api(`/api/applications/${application.id}/screen`, { method: "POST" });
          ensureCurrent(generation); await refresh(); ensureCurrent(generation); message("Screening recorded for HR review.");
        }));
        if (application.score) card.append(button("Advance to scheduling", async () => {
          const generation = sessionGeneration;
          await api(`/api/applications/${application.id}/decision`, jsonPost({ decision: "advance" }));
          ensureCurrent(generation); await refresh(); ensureCurrent(generation); message("Application advanced by HR.");
        }));
      }
    }
    target.append(card);
  }
}

function renderInterviews(interviews) {
  const target = $("interviews"); target.replaceChildren();
  if (!interviews.length) target.append(element("p", "No interviews yet."));
  for (const interview of interviews) {
    const card = element("article");
    card.append(element("p", `Application ${interview.application_id}: ${date(interview.start_at)} - ${date(interview.end_at)} | ${interview.interviewer}`));
    card.append(element("p", `Status: ${interview.completed ? "completed" : interview.status}`));
    if (interview.status === "scheduled" && !interview.completed && new Date(interview.start_at) > new Date()) {
      const suffix = user.role === "hr" ? "cancel" : "candidate-cancel";
      card.append(button("Cancel interview", async () => {
        const generation = sessionGeneration;
        await api(`/api/interviews/${interview.id}/${suffix}`, { method: "POST" });
        ensureCurrent(generation); await refresh(); ensureCurrent(generation); message("Interview cancelled locally.");
      }));
    }
    if (user.role === "hr" && interview.status === "scheduled" && !interview.completed && new Date(interview.end_at) <= new Date()) {
      card.append(button("Confirm interview took place", async () => {
        const generation = sessionGeneration;
        await api(`/api/interviews/${interview.id}/complete`, { method: "POST" });
        ensureCurrent(generation); await refresh(); ensureCurrent(generation); message("Interview completion recorded.");
      }));
    }
    target.append(card);
  }
}

async function loadNotices(append = false) {
  const generation = sessionGeneration;
  const base = user.role === "hr" ? "/api/hr/interview-cancellations" : "/api/candidate/interview-cancellations";
  const query = append && noticeCursor ? `?before=${noticeCursor}` : "";
  const data = await api(base + query);
  ensureCurrent(generation);
  const target = $("notices");
  if (!append) target.replaceChildren();
  if (!append && !data.items.length) target.append(element("p", "No cancellation notices."));
  for (const item of data.items) {
    const card = element("article");
    card.append(element("p", `Application ${item.application_id}, interview ${date(item.start_at)}: ${item.is_read ? "Read" : "Unread"}`));
    if (!item.is_read) card.append(button("Mark read", async () => {
      const generation = sessionGeneration;
      await api(`${base}/${item.id}/read`, { method: "POST" });
      ensureCurrent(generation); await loadNotices(); ensureCurrent(generation); message("Notice marked read.");
    }));
    target.append(card);
  }
  noticeCursor = data.next_before;
  $("more-notices").hidden = !noticeCursor;
}

async function refresh() {
  const generation = sessionGeneration;
  const [jobs, applications, interviews] = await Promise.all([api("/api/jobs"), api("/api/applications"), api("/api/interviews")]);
  ensureCurrent(generation);
  const select = $("job"); select.replaceChildren();
  for (const job of jobs) { const option = element("option", job.title); option.value = job.id; select.append(option); }
  renderApplications(applications, jobs); renderInterviews(interviews); await loadNotices();
}

async function openWorkspace(account) {
  const generation = sessionGeneration;
  user = account;
  if (!["candidate", "hr"].includes(user.role)) {
    await api("/api/auth/logout", { method: "POST" }); ensureCurrent(generation); signedOut();
    throw new Error("This demo interface supports candidate and HR accounts only.");
  }
  $("login-panel").hidden = true; $("workspace").hidden = false; $("logout").hidden = false;
  $("candidate-upload").hidden = user.role !== "candidate"; $("hr-slots").hidden = user.role !== "hr";
  $("welcome").textContent = `Welcome, ${user.display_name}`;
  $("role-description").textContent = user.role === "hr" ? "Review applications and manage interview slots." : "Apply and manage your interviews.";
  await refresh();
}

$("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(event.submitter, async () => {
    sessionGeneration++;
    const generation = sessionGeneration;
    const account = await api("/api/auth/login", jsonPost({ username: $("username").value, password: $("password").value }));
    ensureCurrent(generation); $("password").value = ""; await openWorkspace(account); ensureCurrent(generation); message("Signed in.");
  });
});
$("logout").addEventListener("click", () => action($("logout"), async () => { const generation = ++sessionGeneration; await api("/api/auth/logout", { method: "POST" }); ensureCurrent(generation); signedOut(); message("Signed out."); }));
$("refresh").addEventListener("click", () => action($("refresh"), async () => { const generation = sessionGeneration; await refresh(); ensureCurrent(generation); message("Refreshed."); }));
$("more-notices").addEventListener("click", () => action($("more-notices"), () => loadNotices(true)));
$("upload-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(event.submitter, async () => {
    const generation = sessionGeneration;
    const file = $("resume").files[0];
    if (!file) throw new Error("Choose a PDF or DOCX file.");
    const query = new URLSearchParams({ job_id: $("job").value, filename: file.name });
    await api(`/api/applications?${query}`, { method: "POST", body: file });
    ensureCurrent(generation); $("resume").value = ""; await refresh(); ensureCurrent(generation); message("Application submitted.");
  });
});
$("slot-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(event.submitter, async () => {
    const generation = sessionGeneration;
    const start = new Date($("slot-start").value), end = new Date($("slot-end").value);
    if (Number.isNaN(start.valueOf()) || Number.isNaN(end.valueOf())) throw new Error("Enter valid slot times.");
    await api("/api/slots", jsonPost({ start_at: start.toISOString(), end_at: end.toISOString(), interviewer: $("interviewer").value }));
    ensureCurrent(generation); $("slot-form").reset(); message("Slot created.");
  });
});
const initialGeneration = sessionGeneration;
api("/api/auth/me").then((account) => { ensureCurrent(initialGeneration); return openWorkspace(account); })
  .catch((error) => { if (!(error instanceof StaleSession) && initialGeneration === sessionGeneration) signedOut(); });
