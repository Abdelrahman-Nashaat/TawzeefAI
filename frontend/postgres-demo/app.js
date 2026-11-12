"use strict";

const $ = (id) => document.getElementById(id);
let user = null;
let noticeCursor = null;
let submissionCursor = null;
let sessionGeneration = 0;
let jobRevision = null;
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
  submissionCursor = null;
  $("more-submissions").hidden = true;
  $("workspace").hidden = true;
  $("login-panel").hidden = false;
  $("logout").hidden = true;
  $("password").value = "";
  $("resume").value = "";
  $("slot-form").reset();
  $("hr-job-form").reset(); $("hr-job-id").disabled = false; jobRevision = null;
  for (const id of ["applications", "interviews", "notices", "submission-notices", "dashboard-results", "knowledge-results", "knowledge-documents", "hr-job-list"]) $(id).replaceChildren();
  $("knowledge-form").reset(); $("knowledge-add").reset();
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

async function showPrivateSlots(application, container) {
  const generation = sessionGeneration;
  const slots = await api(`/api/applications/${application.id}/private-slots`);
  ensureCurrent(generation);
  const list = element("div");
  if (!slots.length) list.append(element("p", "No upcoming reserved slots."));
  for (const slot of slots) {
    const line = element("p", `${date(slot.start_at)} - ${date(slot.end_at)} | ${slot.interviewer} `);
    if (slot.booked) line.append(element("span", "Booked; cancel the interview before withdrawing."));
    else line.append(button("Withdraw reservation", async () => {
      const current = sessionGeneration;
      await api(`/api/slots/${slot.id}/withdraw`, {method: "POST"});
      ensureCurrent(current); await showPrivateSlots(application, container);
      ensureCurrent(current); message("Reservation withdrawn.");
    }));
    list.append(line);
  }
  container.replaceChildren(list);
}

async function showOffers(application, container) {
  const generation = sessionGeneration;
  const role = user.role;
  const offers = await api(`/api/applications/${application.id}/offers`);
  ensureCurrent(generation);
  const list = element("div");
  if (!offers.length) list.append(element("p", "No offers yet."));
  for (const offer of offers) {
    const card = element("article");
    card.append(element("p", `Offer ${offer.id} - ${offer.status}`));
    card.append(element("pre", offer.details));
    if (offer.status === "presented" && role === "candidate" && application.status === "offered") {
      for (const decision of ["accepted", "declined"]) {
        card.append(button(decision === "accepted" ? "Accept offer" : "Decline offer", async () => {
          const current = sessionGeneration;
          await api(`/api/offers/${offer.id}/respond`, jsonPost({ decision }));
          ensureCurrent(current); await refresh(); ensureCurrent(current); message(`Offer ${decision}.`);
        }));
      }
    }
    if (offer.status === "presented" && role === "hr" && application.status === "offered") {
      card.append(button("Withdraw offer", async () => {
        const current = sessionGeneration;
        await api(`/api/offers/${offer.id}/withdraw`, { method: "POST" });
        ensureCurrent(current); await refresh(); ensureCurrent(current); message("Offer withdrawn.");
      }));
    }
    if (offer.status === "accepted" && role === "hr" && application.status === "offered") {
      card.append(button("Record hire", async () => {
        const current = sessionGeneration;
        await api(`/api/applications/${application.id}/decision`, jsonPost({ decision: "hire" }));
        ensureCurrent(current); await refresh(); ensureCurrent(current); message("Hiring decision recorded.");
      }));
    }
    list.append(card);
  }
  container.replaceChildren(list);
}

async function showRubric(application, container) {
  const generation = sessionGeneration;
  const source = await api(`/api/applications/${application.id}/rubric-source`);
  ensureCurrent(generation);
  const panel = element("div");
  panel.append(element("p", "HR judgment only. Masking is incomplete; evidence does not verify qualifications. Leave a score blank when unknown. Select evidence in the CV and use the corresponding button."));
  panel.append(element("pre", JSON.stringify(source.job, null, 2)));
  const cv = element("textarea");
  cv.readOnly = true; cv.value = source.resume_text; cv.rows = 12;
  cv.setAttribute("aria-label", "Partially masked CV for evidence selection");
  panel.append(cv);
  const form = element("form");
  const categories = [["mandatory_skills", "Mandatory skills (40%)"], ["preferred_skills", "Preferred skills (15%)"], ["experience", "Experience (20%)"], ["projects", "Projects (15%)"], ["education", "Education (10%)"]];
  const fields = [];
  for (const [category, labelText] of categories) {
    const row = element("div");
    const label = element("label", `${labelText} score (0-100, blank if unknown) `);
    const score = element("input"); score.type = "number"; score.min = "0"; score.max = "100"; score.step = "1";
    label.append(score); const evidence = element("span", "No evidence selected");
    let selection = null;
    row.append(label, button("Use selected evidence", async () => {
      ensureCurrent(generation);
      const start = cv.selectionStart, end = cv.selectionEnd;
      if (cv.value !== source.resume_text.replace(/\r\n?/g, "\n")) throw new Error("CV display changed; reopen the rubric.");
      if (start === end || !cv.value.slice(start, end).trim()) throw new Error("Select nonempty CV evidence first.");
      const sourceOffset = (position) => {
        let offset = 0, display = 0;
        while (display < position && offset < source.resume_text.length) {
          if (source.resume_text[offset] === "\r" && source.resume_text[offset + 1] === "\n") offset++;
          offset++; display++;
        }
        return offset;
      };
      const originalStart = sourceOffset(start), originalEnd = sourceOffset(end);
      const splitsSurrogate = (i) => i > 0 && i < source.resume_text.length && /[\uD800-\uDBFF]/.test(source.resume_text[i - 1]) && /[\uDC00-\uDFFF]/.test(source.resume_text[i]);
      if (splitsSurrogate(originalStart) || splitsSurrogate(originalEnd)) throw new Error("Select complete characters.");
      // DOM selections use UTF-16 units; the API uses Unicode code-point offsets.
      selection = { start: Array.from(source.resume_text.slice(0, originalStart)).length,
        end: Array.from(source.resume_text.slice(0, originalEnd)).length };
      if (selection.end - selection.start > 1000) { selection = null; throw new Error("Evidence exceeds 1000 characters."); }
      evidence.textContent = `Selected: ${source.resume_text.slice(originalStart, originalEnd)}`;
    }), evidence);
    fields.push({ category, score, selected: () => selection }); form.append(row);
  }
  const submit = element("button", "Save HR rubric review"); submit.type = "submit"; form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    action(event.submitter, async () => {
      ensureCurrent(generation);
      const items = fields.map(({category, score, selected}) => {
        if (!score.value.trim()) return {category, score: null, start: null, end: null};
        const value = Number(score.value), span = selected();
        if (!Number.isInteger(value) || value < 0 || value > 100 || !span) throw new Error(`Enter a valid score and select evidence for ${category}.`);
        return {category, score: value, ...span};
      });
      const review = await api(`/api/applications/${application.id}/rubric-assessment`, jsonPost({source_digest: source.source_digest, items}));
      ensureCurrent(generation);
      const result = element("div");
      result.append(element("p", `HR assessment range: ${review.lower} - ${review.upper}. This is not an automatic decision.`));
      if (Number.isInteger(review.audit_event_id) && items.some((item) => item.score !== null)) {
        const feedbackForm = element("form");
        const feedbackLabel = element("label", "Candidate-facing explanation ");
        const feedback = element("textarea"); feedback.required = true; feedback.maxLength = 2000;
        feedbackLabel.append(feedback);
        const share = element("button", "Share explanation and score range"); share.type = "submit";
        feedbackForm.append(element("p", "Review your explanation before sharing it with the candidate. Your private rubric judgments and CV excerpts are not shared automatically."), feedbackLabel, share);
        feedbackForm.addEventListener("submit", (event) => {
          event.preventDefault();
          action(event.submitter, async () => {
            ensureCurrent(generation);
            const explanation = feedback.value.trim();
            if (!explanation) throw new Error("Enter an explanation to share.");
            await api(`/api/applications/${application.id}/candidate-feedback`, jsonPost({audit_event_id: review.audit_event_id, explanation}));
            ensureCurrent(generation); feedback.value = ""; message("Assessment feedback shared with candidate.");
          });
        });
        result.append(feedbackForm, button("Retract candidate feedback", async () => {
          ensureCurrent(generation);
          await api(`/api/applications/${application.id}/candidate-feedback`, { method: "DELETE" });
          ensureCurrent(generation); message("Candidate feedback retracted.");
        }));
        result.append(button("Advance using this review", async () => {
          ensureCurrent(generation);
          await api(`/api/applications/${application.id}/decision`, jsonPost({decision: "advance", rubric_event_id: review.audit_event_id}));
          ensureCurrent(generation); await refresh(); ensureCurrent(generation); message("Application advanced by HR using this review.");
        }));
      }
      container.replaceChildren(result);
    });
  });
  panel.append(form); container.replaceChildren(panel);
}

async function manageFeedback(application, container) {
  const generation = sessionGeneration;
  const history = await api(`/api/applications/${application.id}/rubric-history?limit=1`);
  ensureCurrent(generation);
  const panel = element("div");
  const current = history.items[0];
  panel.append(button("Retract candidate feedback", async () => {
    ensureCurrent(generation);
    await api(`/api/applications/${application.id}/candidate-feedback`, { method: "DELETE" });
    ensureCurrent(generation); message("Candidate feedback retracted.");
  }));
  if (!current || current.is_stale) {
    panel.append(element("p", "A current HR rubric review is required before sharing feedback."));
  } else {
    panel.append(element("p", `The candidate will see score range ${current.lower} - ${current.upper} and your explanation. Private rubric judgments and CV excerpts are not shared automatically.`));
    const form = element("form"); const label = element("label", "Candidate-facing explanation ");
    const explanation = element("textarea"); explanation.required = true; explanation.maxLength = 2000; label.append(explanation);
    const submit = element("button", "Share feedback"); submit.type = "submit"; form.append(label, submit);
    form.addEventListener("submit", (event) => {
      event.preventDefault(); action(event.submitter, async () => {
        ensureCurrent(generation);
        const text = explanation.value.trim(); if (!text) throw new Error("Enter an explanation to share.");
        await api(`/api/applications/${application.id}/candidate-feedback`, jsonPost({audit_event_id: current.audit_event_id, explanation: text}));
        ensureCurrent(generation); explanation.value = ""; message("Assessment feedback shared with candidate.");
      });
    });
    panel.append(form);
  }
  container.replaceChildren(panel);
}

function renderApplications(applications, jobs) {
  const target = $("applications"); target.replaceChildren();
  if (!applications.length) target.append(element("p", "No applications yet."));
  const compareSelection = [];
  if (user.role === "hr" && applications.length >= 2) {
    const generation = sessionGeneration;
    const results = element("div");
    target.append(element("p", "Select 2–20 applications for the same job with current HR reviews."));
    target.append(button("Compare HR reviews", async () => {
      ensureCurrent(generation);
      const selected = compareSelection.filter((item) => item.checkbox.checked).map((item) => item.id);
      if (selected.length < 2 || selected.length > 20) throw new Error("Select 2–20 applications.");
      const comparison = await api("/api/hr/rubric-comparison", jsonPost({ application_ids: selected }));
      ensureCurrent(generation);
      const content = [element("p", comparison.notice)];
      for (const review of comparison.reviews) content.push(element("p", `Application ${review.application_id}: ${review.lower}–${review.upper}`));
      for (const pair of comparison.pairs) content.push(element("p", pair.higher_range === null
        ? `Applications ${pair.application_ids.join(" and ")}: ranges overlap; order indeterminate.`
        : `Applications ${pair.application_ids.join(" and ")}: application ${pair.higher_range} has the higher range.`));
      results.replaceChildren(...content);
    }), results);
  }
  for (const application of applications) {
    const card = element("article");
    const title = jobs.find((job) => job.id === application.job_id)?.title || application.job_id;
    card.append(element("h3", `${title} - application ${application.id}`));
    card.append(element("p", `Status: ${application.status}`));
    if (user.role === "candidate") {
      const progress = element("p");
      card.append(button("View current stage", async () => {
        const generation = sessionGeneration;
        const result = await api(`/api/workflows/${application.id}/progress`);
        ensureCurrent(generation);
        progress.textContent = `Current stage: ${result.stage.replaceAll("_", " ")}`;
      }), progress);
      const feedback = element("div");
      card.append(button("View HR feedback", async () => {
        const generation = sessionGeneration;
        const result = await api(`/api/applications/${application.id}/candidate-feedback`);
        ensureCurrent(generation);
        if (result.status !== "current") { feedback.replaceChildren(element("p", "No current feedback has been shared.")); return; }
        feedback.replaceChildren(element("p", `HR assessment range: ${result.lower} - ${result.upper}`),
          element("p", result.explanation), element("p", result.notice));
      }), feedback);
    }
    const offers = element("div");
    card.append(button("View offers", () => showOffers(application, offers)), offers);
    if (user.role === "candidate" && application.status === "advanced") {
      const slots = element("div");
      card.append(button("Find available slots", () => showSlots(application, slots)), slots);
    }
    if (user.role === "hr") {
      card.append(element("p", `Candidate ${application.candidate_id}`));
      const feedback = element("div");
      card.append(button("Manage candidate feedback", () => manageFeedback(application, feedback)), feedback);
      const label = element("label", "Select for HR comparison ");
      const checkbox = element("input"); checkbox.type = "checkbox";
      label.append(checkbox); card.append(label); compareSelection.push({ id: application.id, checkbox });
      if (["submitted", "advanced", "offered"].includes(application.status)) {
        const rejectionForm = element("form");
        const reasonLabel = element("label", "Reason for rejection ");
        const reason = element("textarea");
        reason.required = true; reason.maxLength = 2000;
        reasonLabel.append(reason);
        const reject = element("button", "Reject application");
        reject.type = "submit";
        rejectionForm.append(reasonLabel,
          element("p", "Rejecting cancels future interviews and withdraws any pending offer."), reject);
        rejectionForm.addEventListener("submit", (event) => {
          event.preventDefault();
          action(event.submitter, async () => {
            const generation = sessionGeneration;
            const note = reason.value.trim();
            if (!note) throw new Error("Enter a reason for rejection.");
            if (note.length > 2000) throw new Error("The rejection reason is too long.");
            await api(`/api/applications/${application.id}/decision`, jsonPost({ decision: "reject", note }));
            ensureCurrent(generation); reason.value = ""; await refresh(); ensureCurrent(generation);
            message("Application rejected by HR.");
          });
        });
        card.append(rejectionForm);
      }
      if (application.status === "advanced") {
        const reservationForm = element("form");
        const startLabel = element("label", "Reserved start "), start = element("input");
        const endLabel = element("label", "Reserved end "), end = element("input");
        const interviewerLabel = element("label", "Interviewer "), interviewer = element("input");
        start.type = end.type = "datetime-local"; start.required = end.required = interviewer.required = true;
        interviewer.maxLength = 120;
        startLabel.append(start); endLabel.append(end); interviewerLabel.append(interviewer);
        const reserve = element("button", "Reserve slot for this candidate"); reserve.type = "submit";
        const reservations = element("div");
        reservationForm.append(startLabel, endLabel, interviewerLabel, reserve);
        reservationForm.addEventListener("submit", (event) => {
          event.preventDefault();
          action(event.submitter, async () => {
            const generation = sessionGeneration;
            const from = new Date(start.value), to = new Date(end.value);
            if (Number.isNaN(from.valueOf()) || Number.isNaN(to.valueOf())) throw new Error("Enter valid slot times.");
            await api(`/api/applications/${application.id}/private-slots`, jsonPost({start_at: from.toISOString(), end_at: to.toISOString(), interviewer: interviewer.value}));
            ensureCurrent(generation); reservationForm.reset(); await showPrivateSlots(application, reservations);
            ensureCurrent(generation); message("Slot reserved locally for this candidate.");
          });
        });
        card.append(reservationForm, button("View reserved slots", () => showPrivateSlots(application, reservations)), reservations);
        const offerForm = element("form");
        const label = element("label", "Offer details ");
        const input = element("textarea");
        input.required = true; input.maxLength = 4000;
        label.append(input);
        const submit = element("button", "Present offer");
        submit.type = "submit";
        offerForm.append(label, submit);
        offerForm.addEventListener("submit", (event) => {
          event.preventDefault();
          action(event.submitter, async () => {
            const current = sessionGeneration;
            await api(`/api/applications/${application.id}/offers`, jsonPost({ details: input.value }));
            ensureCurrent(current); input.value = ""; await refresh(); ensureCurrent(current); message("Offer presented locally.");
          });
        });
        card.append(offerForm);
      }
      const details = element("details"); details.append(element("summary", "Review CV and provisional screening"));
      details.append(element("h4", "Extracted CV"), element("pre", application.resume_text));
      details.append(element("h4", "Provisional screening"), element("pre", application.score ? JSON.stringify(application.score, null, 2) : "Not screened"));
      card.append(details);
      if (application.status === "submitted") {
        const rubric = element("div");
        card.append(button("Review weighted rubric", () => showRubric(application, rubric)), rubric);
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

async function loadSubmissionNotices(append = false) {
  const generation = sessionGeneration;
  const query = append && submissionCursor ? `?before=${submissionCursor}` : "";
  const data = await api(`/api/hr/submission-notices${query}`);
  ensureCurrent(generation);
  const target = $("submission-notices");
  if (!append) target.replaceChildren();
  if (!append && !data.items.length) target.append(element("p", "No new application notices."));
  for (const item of data.items) {
    const card = element("article");
    card.append(element("p", `Application ${item.application_id}, job ${item.submitted_job_id}: ${item.is_read ? "Read" : "Unread"}`));
    if (!item.is_read) card.append(button("Mark read", async () => {
      const current = sessionGeneration;
      await api(`/api/hr/submission-notices/${item.id}/read`, {method: "POST"});
      ensureCurrent(current); await loadSubmissionNotices(); ensureCurrent(current); message("Notice marked read.");
    }));
    target.append(card);
  }
  submissionCursor = data.next_before;
  $("more-submissions").hidden = !submissionCursor;
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
  if (!["candidate", "employee", "hr"].includes(user.role)) {
    await api("/api/auth/logout", { method: "POST" }); ensureCurrent(generation); signedOut();
    throw new Error("This demo interface supports candidate and HR accounts only.");
  }
  $("login-panel").hidden = true; $("workspace").hidden = false; $("logout").hidden = false;
  $("candidate-upload").hidden = user.role !== "candidate"; $("hr-slots").hidden = user.role !== "hr";
  $("hr-dashboard").hidden = user.role !== "hr";
  $("submission-notices-panel").hidden = user.role !== "hr";
  $("hr-jobs").hidden = user.role !== "hr";
  $("knowledge-add").hidden = user.role !== "hr";
  $("knowledge-management").hidden = user.role !== "hr";
  for (const id of ["applications-panel", "interviews-panel", "notices-panel"]) $(id).hidden = user.role === "employee";
  $("welcome").textContent = `Welcome, ${user.display_name}`;
  $("role-description").textContent = user.role === "hr" ? "Review applications and manage interview slots." :
    user.role === "employee" ? "Search the documents available to employees." : "Apply and manage your interviews.";
  if (user.role !== "employee") await refresh();
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
$("refresh").addEventListener("click", () => action($("refresh"), async () => { const generation = sessionGeneration; if (user.role !== "employee") await refresh(); ensureCurrent(generation); message("Refreshed."); }));
$("knowledge-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(event.submitter, async () => {
    const generation = sessionGeneration;
    const result = await api('/api/knowledge/answer', jsonPost({question: $("knowledge-question").value,
      experimental_hybrid: event.submitter?.value === 'hybrid'}));
    ensureCurrent(generation);
    const nodes = [element('p', result.notice)];
    if (!result.sources.length) nodes.push(element('p', result.suggestions?.length ?
      'No lexical match found. Contact HR for an answer.' : 'No relevant source found. Contact HR.'));
    for (const source of result.sources) {
      const item = element('article');
      item.append(element('h3', `${source.title}${source.page ? ` - page ${source.page}` : ''}`), element('pre', source.text));
      nodes.push(item);
    }
    if (result.suggestions?.length) nodes.push(element('p', 'Unverified semantic leads: review the sources; they may not answer the question.'));
    for (const source of result.suggestions || []) {
      const item = element('article');
      item.append(element('h3', `${source.title}${source.page ? ` - page ${source.page}` : ''}`), element('pre', source.text));
      nodes.push(item);
    }
    $("knowledge-results").replaceChildren(...nodes);
  });
});
$("knowledge-add").addEventListener("submit", (event) => {
  event.preventDefault();
  action(event.submitter, async () => {
    const generation = sessionGeneration, page = $("knowledge-page").value;
    await api('/api/knowledge/documents/text', jsonPost({title: $("knowledge-title").value, text: $("knowledge-text").value,
      audience: $("knowledge-audience").value, collection: $("knowledge-collection").value, page: page ? Number(page) : null}));
    ensureCurrent(generation); $("knowledge-add").reset(); message('Synthetic document added.');
  });
});
async function loadKnowledgeDocuments() {
  const generation = sessionGeneration;
  const result = await api('/api/knowledge/documents');
  ensureCurrent(generation);
  const nodes = [];
  if (!result.items.length) nodes.push(element('p', 'No active documents.'));
  for (const document of result.items) {
    const row = element('p', `${document.title} - ${document.collection} - ${document.audience}${document.page ? ` - page ${document.page}` : ''} `);
    row.append(button('Deactivate', async () => {
      ensureCurrent(generation);
      await api(`/api/knowledge/documents/${document.id}/deactivate`, {method: 'POST'});
      ensureCurrent(generation);
      await loadKnowledgeDocuments();
      ensureCurrent(generation);
      $("knowledge-results").replaceChildren();
      message('Document deactivated.');
    }));
    nodes.push(row);
  }
  $("knowledge-documents").replaceChildren(...nodes);
}
$("knowledge-list").addEventListener("click", () => action($("knowledge-list"), loadKnowledgeDocuments));
$("load-dashboard").addEventListener("click", () => action($("load-dashboard"), async () => {
  const generation = sessionGeneration;
  const data = await api('/api/dashboard/jobs');
  ensureCurrent(generation);
  const nodes = [element('p', data.definition)];
  for (const job of data.jobs) {
    const row = element('article');
    row.append(element('h3', job.title));
    row.append(element('p', `Applications: ${job.applications_total} | Screening events: ${job.screening_events}`));
    row.append(element('p', job.pipeline.length ? job.pipeline.map((item) => `${item.status}: ${item.count}`).join(' | ') : 'No applications.'));
    const metric = job.time_to_hire;
    row.append(element('p', metric.average_days === null ? 'Time to hire: unavailable (no valid recorded hires).' : `Average time to hire: ${metric.average_days} days (${metric.count} recorded hires).`));
    nodes.push(row);
  }
  $('dashboard-results').replaceChildren(...nodes);
}));
$("more-notices").addEventListener("click", () => action($("more-notices"), () => loadNotices(true)));
$("load-submissions").addEventListener("click", () => action($("load-submissions"), () => loadSubmissionNotices()));
$("more-submissions").addEventListener("click", () => action($("more-submissions"), () => loadSubmissionNotices(true)));
$("new-hr-job").addEventListener("click", () => {
  $("hr-job-form").reset(); $("hr-job-id").disabled = false; jobRevision = null;
});
$("list-hr-jobs").addEventListener("click", () => action($("list-hr-jobs"), async () => {
  const generation = sessionGeneration, jobs = await api('/api/hr/jobs');
  ensureCurrent(generation);
  const rows = jobs.map((job) => {
    const row = element('p', `${job.title} (${job.id}) `);
    row.append(button('Edit', async () => {
      ensureCurrent(generation); jobRevision = job.revision;
      $("hr-job-id").value = job.id; $("hr-job-id").disabled = true;
      $("hr-job-title").value = job.title; $("hr-job-location").value = job.location;
      $("hr-job-required").value = job.required_skills.join('\n');
      $("hr-job-preferred").value = job.preferred_skills.join('\n');
      $("hr-job-responsibilities").value = job.responsibilities.join('\n');
    }));
    return row;
  });
  $("hr-job-list").replaceChildren(...rows);
}));
$("hr-job-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(event.submitter, async () => {
    const generation = sessionGeneration;
    const lines = (id) => $(id).value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
    const job = {id: $("hr-job-id").value.trim(), title: $("hr-job-title").value,
      location: $("hr-job-location").value, required_skills: lines('hr-job-required'),
      preferred_skills: lines('hr-job-preferred'), responsibilities: lines('hr-job-responsibilities')};
    const options = jobRevision ? {method: 'PUT', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({job, expected_revision: jobRevision})} : jsonPost(job);
    await api(jobRevision ? `/api/hr/jobs/${encodeURIComponent(job.id)}` : '/api/hr/jobs', options);
    ensureCurrent(generation); $("hr-job-form").reset(); $("hr-job-id").disabled = false; jobRevision = null;
    $("hr-job-list").replaceChildren(); await refresh(); ensureCurrent(generation); message('Job saved.');
  });
});
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
