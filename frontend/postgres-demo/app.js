"use strict";

const $ = (id) => document.getElementById(id);
let user = null;
let noticeCursor = null;
let submissionCursor = null;
let withdrawalCursor = null;
let sessionGeneration = 0;
let jobRevision = null;
let conversationId = null;
let conversationGeneration = 0;
let pdfReview = null;
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
  withdrawalCursor = null;
  conversationId = null;
  conversationGeneration++;
  $("more-submissions").hidden = true;
  $("workspace").hidden = true;
  $("login-panel").hidden = false;
  $("logout").hidden = true;
  $("password").value = "";
  $("resume").value = "";
  $("slot-form").reset();
  $("hr-job-form").reset(); $("hr-job-id").disabled = false; jobRevision = null;
  for (const id of ["applications", "interviews", "notices", "submission-notices", "withdrawal-notices", "dashboard-results", "knowledge-results", "knowledge-documents", "hr-job-list"]) $(id).replaceChildren();
  $("more-withdrawals").hidden = true;
  $("knowledge-form").reset(); $("knowledge-add").reset(); $("knowledge-pdf").reset();
  pdfReview = null; $("knowledge-pdf-preview").replaceChildren(); $("knowledge-pdf-import").disabled = true;
  $("conversation-form").reset(); $("conversation-form").hidden = true;
  $("conversation-delete").hidden = true;
  $("conversation-current").textContent = "No conversation selected.";
  $("conversation-list-results").replaceChildren(); $("conversation-turns").replaceChildren();
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
      if (["submitted", "advanced", "offered"].includes(application.status)) {
        card.append(button("Withdraw application", async () => {
          if (!window.confirm("Withdraw this application? Future interviews and unanswered offers will be cancelled locally. Contact HR if you have accepted an offer.")) return;
          const generation = sessionGeneration;
          await api(`/api/applications/${application.id}/withdraw`, jsonPost({confirmed: true}));
          ensureCurrent(generation); await refresh(); ensureCurrent(generation);
          message("Application withdrawn locally. No email or calendar update was sent.");
        }));
      }
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
      const auditPanel = element("div");
      const auditRows = element("div");
      let auditCursor = null;
      let auditGeneration = 0;
      async function loadAudit(append) {
        const generation = sessionGeneration;
        const view = append ? auditGeneration : ++auditGeneration;
        const query = append && auditCursor !== null ? `?before=${encodeURIComponent(auditCursor)}` : "";
        const result = await api(`/api/applications/${application.id}/audit${query}`);
        ensureCurrent(generation);
        if (view !== auditGeneration) return;
        const rows = result.items.map(item => element("p", `${item.created_at} · ${item.action}${item.decision ? `: ${item.decision}` : ""} · actor ${item.actor_id}`));
        if (append) auditRows.append(...rows); else auditRows.replaceChildren(...rows);
        if (!append && !rows.length) auditRows.append(element("p", "No audit events."));
        auditCursor = result.next_before; moreAudit.hidden = auditCursor === null;
      }
      const moreAudit = button("Load older audit events", () => loadAudit(true));
      moreAudit.hidden = true;
      auditPanel.append(element("p", "Event metadata only; assessment details are not shown."), auditRows, moreAudit);
      card.append(button("View audit timeline", () => loadAudit(false)), auditPanel);
      const duplicatesPanel = element("div");
      let duplicatesGeneration = 0;
      card.append(button("Check exact CV duplicates", async () => {
        const generation = sessionGeneration, view = ++duplicatesGeneration;
        duplicatesPanel.replaceChildren(element("p", "Checking…"));
        const result = await api(`/api/applications/${application.id}/duplicates`);
        ensureCurrent(generation);
        if (view !== duplicatesGeneration) return;
        const rows = [element("p", "Only identical extracted CV text is checked. A match is not proof of fraud.")];
        if (!result.matches.length) rows.push(element("p", "No exact matches found."));
        for (const match of result.matches) {
          rows.push(element("p", `Application ${match.application_id} · Candidate ${match.candidate_id} · Job ${match.job_id} · ${match.status}`));
        }
        if (result.truncated) rows.push(element("p", "Only the first 50 matches are shown."));
        duplicatesPanel.replaceChildren(...rows);
      }), duplicatesPanel);
      const graphForm = element("form");
      const graphLabel = element("label", "Optional policy question ");
      const graphQuestion = element("input");
      graphQuestion.maxLength = 2000; graphLabel.append(graphQuestion);
      const graphSubmit = element("button", "Run read-only specialist review");
      graphSubmit.type = "submit";
      const graphOutput = element("pre");
      graphForm.append(graphLabel, graphSubmit, graphOutput);
      graphForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const generation = sessionGeneration;
        action(event.submitter, async () => {
          const result = await api(`/api/applications/${application.id}/graph-review`, jsonPost({policy_question: graphQuestion.value}));
          ensureCurrent(generation);
          graphOutput.textContent = JSON.stringify(result, null, 2);
        });
      });
      card.append(graphForm);
      const draftPanel = element("div");
      const draftGeneration = sessionGeneration;
      let draftViewGeneration = 0;
      function ensureDraftView(session, view) {
        ensureCurrent(session);
        if (view !== draftViewGeneration) throw new StaleSession();
      }
      const slotDraftForm = element("form");
      const draftStartLabel = element("label", "Proposed interview start (local time) "), draftStart = element("input");
      const draftEndLabel = element("label", "Proposed interview end (local time) "), draftEnd = element("input");
      const draftInterviewerLabel = element("label", "Proposed interviewer "), draftInterviewer = element("input");
      draftStart.type = draftEnd.type = "datetime-local";
      draftStart.required = draftEnd.required = draftInterviewer.required = true;
      draftInterviewer.maxLength = 120;
      draftStartLabel.append(draftStart); draftEndLabel.append(draftEnd); draftInterviewerLabel.append(draftInterviewer);
      const proposeSlot = element("button", "Propose advancement and private slot"); proposeSlot.type = "submit";
      const resumableSlot = element("button", "Start resumable advancement and private slot"); resumableSlot.type = "submit";
      const reserveSlot = element("button", "Start resumable private slot proposal"); reserveSlot.type = "submit";
      slotDraftForm.append(draftStartLabel, draftEndLabel, draftInterviewerLabel,
        element("p", "The candidate must book the slot after HR approves the proposal."));
      if (application.status === "submitted") slotDraftForm.append(proposeSlot, resumableSlot);
      if (application.status === "advanced") slotDraftForm.append(reserveSlot);
      slotDraftForm.addEventListener("submit", (event) => {
        event.preventDefault();
        if (proposeSlot.disabled || resumableSlot.disabled || reserveSlot.disabled) return;
        const submitter = event.submitter || (application.status === "advanced" ? reserveSlot : proposeSlot);
        proposeSlot.disabled = resumableSlot.disabled = reserveSlot.disabled = true;
        const generation = sessionGeneration;
        action(submitter, async () => {
          ensureCurrent(draftGeneration);
          const view = ++draftViewGeneration;
          const from = new Date(draftStart.value), to = new Date(draftEnd.value);
          if (!draftStart.value || !draftEnd.value || Number.isNaN(from.valueOf()) || Number.isNaN(to.valueOf())) throw new Error("Enter valid slot times.");
          const resumable = submitter === resumableSlot || submitter === reserveSlot;
          const endpoint = resumable ? "graph-review-preparations" : "graph-review-drafts";
          const result = await api(`/api/applications/${application.id}/${endpoint}`, jsonPost({
            policy_question: graphQuestion.value, intent: submitter === reserveSlot ? "reserve_slot" : "advance",
            slot: {start_at: from.toISOString(), end_at: to.toISOString(), interviewer: draftInterviewer.value}
          }));
          ensureDraftView(generation, view); slotDraftForm.reset(); graphQuestion.value = "";
          draftPanel.replaceChildren(element("p", resumable
            ? `Proposal ${result.id} started. Open saved reviews to continue preparation.`
            : `Saved proposal ${result.id}. Reopen to review and approve or discard.`));
        }).finally(() => { proposeSlot.disabled = resumableSlot.disabled = reserveSlot.disabled = false; });
      });
      async function startAdaptiveReview(modelPlan) {
        ensureCurrent(draftGeneration);
        if (modelPlan && !window.confirm("Send only the application stage and assessment status to Groq to choose review steps? Use synthetic data only.")) return;
        ensureCurrent(draftGeneration);
        const view = ++draftViewGeneration;
        const generation = sessionGeneration;
        const result = await api(`/api/applications/${application.id}/adaptive-review-preparations`,
          jsonPost({policy_question: graphQuestion.value, model_plan: modelPlan}));
        ensureDraftView(generation, view); graphQuestion.value = "";
        draftPanel.replaceChildren(element("p", `Adaptive review ${result.id} started. Plan: ${result.plan.join(", ")}. Source: ${result.plan_source}. Open saved reviews to continue preparation.`));
      }
      card.append(button("Start adaptive review", () => startAdaptiveReview(false)),
        button("Start AI-planned adaptive review", () => startAdaptiveReview(true)),
        button("Start resumable review", async () => {
        ensureCurrent(draftGeneration);
        const view = ++draftViewGeneration;
        const generation = sessionGeneration;
        const result = await api(`/api/applications/${application.id}/graph-review-preparations`, jsonPost({policy_question: graphQuestion.value}));
        ensureDraftView(generation, view); graphQuestion.value = "";
        draftPanel.replaceChildren(element("p", `Review ${result.id} started. Open saved reviews to continue preparation.`));
      }), button("Start resumable advancement proposal", async () => {
        ensureCurrent(draftGeneration);
        const view = ++draftViewGeneration;
        const generation = sessionGeneration;
        const result = await api(`/api/applications/${application.id}/graph-review-preparations`, jsonPost({policy_question: graphQuestion.value, intent: "advance"}));
        ensureDraftView(generation, view); graphQuestion.value = "";
        draftPanel.replaceChildren(element("p", `Proposal ${result.id} started. Open saved reviews to continue preparation.`));
      }), button("Save supervised review", async () => {
        ensureCurrent(draftGeneration);
        const view = ++draftViewGeneration;
        const generation = sessionGeneration;
        const result = await api(`/api/applications/${application.id}/graph-review-drafts`, jsonPost({policy_question: graphQuestion.value}));
        ensureDraftView(generation, view); graphQuestion.value = "";
        draftPanel.replaceChildren(element("p", `Saved review ${result.id}. Reopen it to acknowledge or discard.`));
      }), button("Propose advancement for HR approval", async () => {
        ensureCurrent(draftGeneration);
        const view = ++draftViewGeneration;
        const generation = sessionGeneration;
        const result = await api(`/api/applications/${application.id}/graph-review-drafts`, jsonPost({policy_question: graphQuestion.value, intent: "advance"}));
        ensureDraftView(generation, view); graphQuestion.value = "";
        draftPanel.replaceChildren(element("p", `Saved proposal ${result.id}. Reopen to review and approve or discard.`));
      }), button("Open saved reviews", async () => {
        ensureCurrent(draftGeneration);
        const listView = ++draftViewGeneration;
        const generation = sessionGeneration;
        const drafts = await api(`/api/applications/${application.id}/graph-review-drafts`);
        ensureDraftView(generation, listView);
        const list = element("div");
        if (!drafts.length) list.append(element("p", "No saved reviews."));
        for (const draft of drafts) {
          const row = element("div"); row.append(element("p", `Review ${draft.id}: ${draft.status}`));
          row.append(button("Open review", async () => {
            ensureCurrent(generation);
            const viewGeneration = ++draftViewGeneration;
            const current = sessionGeneration;
            async function renderReview() {
            const opened = await api(`/api/graph-review-drafts/${draft.id}`);
            ensureDraftView(current, viewGeneration);
            const view = element("div");
            if (opened.plan) view.append(element("p", `Adaptive plan: ${opened.plan.join(", ")}. Source: ${opened.plan_source}.`));
            view.append(element("pre", opened.stale ? "Sources or availability changed; start a new review." : JSON.stringify(opened.result, null, 2)));
            if (!opened.stale && opened.slot) {
              view.append(element("p", `Proposed private slot (UTC): ${opened.slot.start_at} to ${opened.slot.end_at}; interviewer: ${opened.slot.interviewer}`),
                element("p", opened.intent === "reserve_slot" ? "Approval reserves this slot; the candidate must still book it." : "Approval advances the application and reserves this slot; the candidate must still book it."));
            }
            if (!opened.stale && opened.intent === "present_offer") {
              view.append(element("p", "Proposed offer terms (review before approval):"), element("pre", opened.offer_details));
              view.append(element("p", "Approval presents these terms locally; the candidate must respond."));
            }
            if (opened.status === "pending") {
              if (!opened.stale && opened.preparation && !opened.preparation.ready) {
                view.append(element("p", `Preparation complete: ${opened.preparation.completed.join(", ")}. Next: ${opened.preparation.next}.`));
                view.append(button("Continue preparation", async () => {
                  ensureDraftView(current, viewGeneration);
                  await api(`/api/graph-review-drafts/${draft.id}/prepare`, {method: "POST"});
                  ensureDraftView(current, viewGeneration);
                  await renderReview();
                }));
                view.append(button("Complete remaining preparation", async () => {
                  ensureDraftView(current, viewGeneration);
                  try { await api(`/api/graph-review-drafts/${draft.id}/prepare-all`, {method: "POST"}); }
                  catch (error) {
                    ensureDraftView(current, viewGeneration);
                    draftPanel.replaceChildren(element("p", "Preparation stopped. Reopen the review to inspect saved progress."));
                    throw error;
                  }
                  ensureDraftView(current, viewGeneration);
                  await renderReview();
                }));
              }
              if (!opened.stale && opened.preparation?.ready) {
                const labels = {
                  review_advance: "Review whether to advance the application",
                  review_rubric: "Review the applicant's assessment",
                  candidate_can_book: "Candidate can choose an available interview slot",
                  review_interview_schedule: "Review interview scheduling",
                  review_scheduled_interview: "Review the scheduled interview",
                  review_offer_or_next_interview: "Review an offer or another interview",
                  review_offer_status: "Review the offer and candidate response",
                  review_closed_application: "Review the closed application",
                  review_application_status: "Review the application status"
                };
                view.append(element("p", `Suggested next step: ${labels[opened.preparation.suggested_step] || "Review the application"}. HR must decide.`));
                if (!opened.plan) view.append(button("Ask AI for a review suggestion", async () => {
                  ensureDraftView(current, viewGeneration);
                  if (!window.confirm("Send only the application stage, assessment status, interview status, and slot availability to Groq? Use synthetic data only.")) return;
                  const result = await api(`/api/graph-review-drafts/${draft.id}/ai-suggestion`, {method: "POST"});
                  ensureDraftView(current, viewGeneration);
                  view.append(element("p", `AI suggestion: ${labels[result.suggested_step] || "Review the application"}. ${result.notice}`));
                }));
              }
              for (const reviewed of (opened.stale || (opened.preparation && !opened.preparation.ready) ? [false] : [true, false])) {
                view.append(button(reviewed ? (opened.intent === "present_offer" ? "Approve offer" : opened.intent === "reserve_slot" ? "Approve private slot" : opened.intent === "advance" ? (opened.slot ? "Approve advancement and private slot" : "Approve advancement") : "Acknowledge review") : "Discard review", async () => {
                  ensureDraftView(current, viewGeneration);
                  const active = sessionGeneration;
                  try { await api(`/api/graph-review-drafts/${draft.id}/resume`, jsonPost({reviewed})); }
                  catch (error) {
                    ensureDraftView(active, viewGeneration);
                    draftPanel.replaceChildren(element("p", "The review could not be completed. Reopen it before trying again."));
                    throw error;
                  }
                  ensureDraftView(active, viewGeneration);
                  const confirmation = reviewed ? (opened.intent === "present_offer" ? "Offer presented locally. The candidate must respond." : opened.intent === "reserve_slot" ? "Private slot reserved. The candidate must book it." : opened.intent === "advance" ? (opened.slot ? "Application advanced and private slot reserved. The candidate must book it." : "Application advanced by HR.") : "Review acknowledged. No decision made.") : "Review discarded.";
                  if (reviewed && ["advance", "reserve_slot", "present_offer"].includes(opened.intent)) {
                    try { await refresh(); ensureDraftView(active, viewGeneration); }
                    catch (error) {
                      ensureDraftView(active, viewGeneration);
                      message(`${confirmation} Refresh the page to update the displayed status.`);
                      return;
                    }
                    message(confirmation);
                  } else draftPanel.replaceChildren(element("p", confirmation));
                }));
              }
            }
            draftPanel.replaceChildren(view);
            }
            await renderReview();
          })); list.append(row);
        }
        draftPanel.replaceChildren(list);
      }), slotDraftForm, draftPanel);
      if (application.status === "advanced") {
        const offerReviewForm = element("form");
        const offerReviewLabel = element("label", "Proposed offer terms ");
        const offerReviewInput = element("textarea");
        offerReviewInput.required = true; offerReviewInput.maxLength = 4000;
        offerReviewLabel.append(offerReviewInput);
        const offerReviewSubmit = element("button", "Start resumable offer review"); offerReviewSubmit.type = "submit";
        offerReviewForm.append(offerReviewLabel, element("p", "HR must review and approve these terms before they are presented."), offerReviewSubmit);
        offerReviewForm.addEventListener("submit", event => {
          event.preventDefault();
          action(event.submitter || offerReviewSubmit, async () => {
            ensureCurrent(draftGeneration);
            const generation = sessionGeneration, view = ++draftViewGeneration;
            const details = offerReviewInput.value.trim();
            if (!details) throw new Error("Enter offer terms.");
            const result = await api(`/api/applications/${application.id}/graph-review-preparations`,
              jsonPost({intent: "present_offer", offer_details: details, policy_question: graphQuestion.value}));
            ensureDraftView(generation, view); offerReviewForm.reset(); graphQuestion.value = "";
            draftPanel.replaceChildren(element("p", `Offer review ${result.id} started. Open saved reviews to continue preparation.`));
          });
        });
        card.append(offerReviewForm);
      }
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
      const guide = element("div");
      card.append(button("Show suggested interview questions", async () => {
        const generation = sessionGeneration;
        guide.replaceChildren();
        const result = await api(`/api/applications/${application.id}/interview-guide`);
        ensureCurrent(generation);
        const content = [element("p", result.notice)];
        for (const item of result.questions) {
          const block = element("section");
          block.append(element("h4", `${item.category}: ${item.criterion}`), element("p", item.question));
          block.append(element("p", item.reviewed
            ? `Reviewed category evidence offsets: ${item.evidence_offsets.start}–${item.evidence_offsets.end} in the masked CV.`
            : "No reviewed evidence for this category."));
          content.push(block);
        }
        if (result.omitted_count) content.push(element("p", `${result.omitted_count} criteria were omitted by the safety or size filter.`));
        if (!result.questions.length) content.push(element("p", "No suggested questions are available."));
        guide.replaceChildren(...content);
      }), guide);
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

function googleCalendarControls(interview) {
  const panel = element("div");
  const details = element("div");
  const endpoint = `/api/google/interviews/${encodeURIComponent(interview.id)}/calendar`;
  let revision = 0;

  function showDelivery(delivery) {
    const rows = [element("p", `Google Calendar state: ${delivery.state}`),
      element("p", `Event ID: ${delivery.event_id}`),
      element("p", `Start: ${delivery.start_at}`), element("p", `End: ${delivery.end_at}`)];
    if (delivery.context_changed) rows.push(element("p", "The interview context changed during delivery. Review the event and interview."));
    if (["sending", "uncertain", "removing", "removal_uncertain"].includes(delivery.state)) {
      rows.push(element("p", "The provider outcome is unresolved. Check the calendar and contact an administrator; do not assume the event was created or removed."));
    }
    if (delivery.state === "rejected") rows.push(element("p", "The attempt was rejected. This delivery cannot be retried here."));
    if (delivery.state === "removed") rows.push(element("p", "The event was removed. The local interview is still scheduled unless separately cancelled."));
    const request = (suffix, verb) => async () => {
      const generation = sessionGeneration;
      const reviewed = `${verb} Google Calendar event ${delivery.event_id}?\nStart: ${delivery.start_at}\nEnd: ${delivery.end_at}\nThis does not send an email or cancel the local interview.`;
      if (!window.confirm(reviewed)) return;
      ensureCurrent(generation);
      const view = ++revision;
      // A network failure may follow an accepted provider action. Hide action
      // buttons until HR explicitly reloads the authoritative delivery state.
      details.replaceChildren(element("p", "Request in progress. If it fails, check the delivery status before taking further action."));
      try {
        const updated = await api(`${endpoint}/${suffix}`, jsonPost({ confirmed: true, event_id: delivery.event_id }));
        ensureCurrent(generation);
        if (view === revision) showDelivery(updated);
      } catch (error) {
        if (generation === sessionGeneration && view === revision) {
          details.replaceChildren(element("p", "The request failed or its result is unknown. Check Google Calendar status before taking further action."));
        }
        throw error;
      }
    };
    if (delivery.state === "pending") rows.push(button("Create Google Calendar event", request("send", "Create")));
    if (delivery.state === "confirmed") rows.push(button("Remove Google Calendar event", request("remove", "Remove")));
    if (["uncertain", "removal_uncertain"].includes(delivery.state)) {
      rows.push(element("p", "Inspection checks whether the exact private event exists; a missing event remains unresolved."));
      rows.push(button("Inspect uncertain Google Calendar event", request("reconcile", "Inspect")));
    }
    details.replaceChildren(...rows);
  }

  const load = (prepare) => async () => {
    const generation = sessionGeneration, view = ++revision;
    details.replaceChildren(element("p", prepare ? "Preparing calendar review…" : "Checking calendar status…"));
    try {
      const result = await api(endpoint, prepare ? jsonPost({ confirmed: true }) : {});
      ensureCurrent(generation);
      if (view === revision) showDelivery(result);
    } catch (error) {
      if (generation === sessionGeneration && view === revision) {
        details.replaceChildren(element("p", "Unable to load calendar status. Review the error and try checking the status again."));
      }
      throw error;
    }
  };
  panel.append(element("p", "Google Calendar: explicit HR review only. No invitations or emails are sent."));
  if (interview.status === "scheduled" && !interview.completed && new Date(interview.start_at) > new Date()) {
    panel.append(button("Prepare Google Calendar review", load(true)));
  }
  panel.append(button("Check Google Calendar status", load(false)), details);
  return panel;
}

function renderInterviews(interviews) {
  const target = $("interviews"); target.replaceChildren();
  if (!interviews.length) target.append(element("p", "No interviews yet."));
  for (const interview of interviews) {
    const card = element("article");
    card.append(element("p", `Application ${interview.application_id}: ${date(interview.start_at)} - ${date(interview.end_at)} | ${interview.interviewer}`));
    card.append(element("p", `Status: ${interview.completed ? "completed" : interview.status}`));
    if (user.role === "hr") card.append(googleCalendarControls(interview));
    if (interview.status === "scheduled") {
      const calendar = element("a", "Download calendar file");
      calendar.href = `/api/interviews/${encodeURIComponent(interview.id)}/calendar`;
      calendar.download = `interview-${interview.id}.ics`;
      card.append(calendar, element("p", "A saved calendar file does not update if the interview changes or is cancelled."));
    }
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

async function loadWithdrawalNotices(append = false) {
  const generation = sessionGeneration;
  const query = append && withdrawalCursor ? `?before=${withdrawalCursor}` : "";
  const data = await api(`/api/hr/application-withdrawals${query}`); ensureCurrent(generation);
  const target = $("withdrawal-notices");
  if (!append) target.replaceChildren();
  if (!append && !data.items.length) target.append(element("p", "No application withdrawals."));
  for (const item of data.items) {
    const card = element("article");
    card.append(element("p", `Application ${item.application_id}: ${item.is_read ? "Read" : "Unread"}`));
    if (!item.is_read) card.append(button("Mark read", async () => {
      const current = sessionGeneration;
      await api(`/api/hr/application-withdrawals/${item.id}/read`, {method: "POST"});
      ensureCurrent(current); await loadWithdrawalNotices(); ensureCurrent(current); message("Notice marked read.");
    }));
    target.append(card);
  }
  withdrawalCursor = data.next_before; $("more-withdrawals").hidden = !withdrawalCursor;
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
  $("withdrawal-notices-panel").hidden = user.role !== "hr";
  $("hr-jobs").hidden = user.role !== "hr";
  $("knowledge-add").hidden = user.role !== "hr";
  $("knowledge-pdf").hidden = user.role !== "hr";
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
    const generate = event.submitter?.value === 'generate';
    if (generate && !window.confirm('Send this question and matching excerpts to Groq for an AI-generated answer? Use synthetic data only.')) return;
    const result = await api('/api/knowledge/answer', jsonPost({question: $("knowledge-question").value,
      collection: $("knowledge-collection-filter").value || null, job_id: $("knowledge-job-filter").value.trim() || null, section: $("knowledge-section-filter").value.trim() || null,
      experimental_hybrid: event.submitter?.value === 'hybrid', generate_answer: generate}));
    ensureCurrent(generation);
    const nodes = [element('p', result.notice)];
    if (result.answer) nodes.push(element('p', result.answer));
    if (!result.sources.length) nodes.push(element('p', result.suggestions?.length ?
      'No lexical match found. Contact HR for an answer.' : 'No relevant source found. Contact HR.'));
    for (const source of result.sources) {
      const item = element('article');
      item.append(element('h3', `${source.id ? `[${source.id}] ` : ''}${source.title}${source.collection ? ` - ${source.collection}` : ''}${source.page ? ` - page ${source.page}` : ''}${source.job_id ? ` - job ${source.job_id}` : ''}${source.section ? ` - ${source.section}` : ''}`), element('pre', source.text));
      nodes.push(item);
    }
    if (result.suggestions?.length) nodes.push(element('p', 'Unverified semantic leads: review the sources; they may not answer the question.'));
    for (const source of result.suggestions || []) {
      const item = element('article');
      item.append(element('h3', `${source.title}${source.collection ? ` - ${source.collection}` : ''}${source.page ? ` - page ${source.page}` : ''}${source.job_id ? ` - job ${source.job_id}` : ''}${source.section ? ` - ${source.section}` : ''}`), element('pre', source.text));
      nodes.push(item);
    }
    $("knowledge-results").replaceChildren(...nodes);
  });
});
function showConversationTurns(items) {
  const turns = [];
  for (const item of items) {
    const block = element('article');
    block.append(element('h4', `Question: ${item.question}`));
    if (item.collection_filter) block.append(element('p', `Collection: ${item.collection_filter}`));
    if (item.mode === 'source_unavailable') block.append(element('p', 'A saved source is no longer available.'));
    else if (item.mode === 'abstain') block.append(element('p', 'No supporting excerpt found.'));
    if (item.mode === 'groq' && item.answer) {
      block.append(element('p', 'Generated answer: verify each claim against its source.'), element('p', item.answer));
    }
    for (const source of item.sources || []) {
      block.append(element('h4', `${source.title}${source.collection ? ` - ${source.collection}` : ''}${source.page ? ` - page ${source.page}` : ''}${source.job_id ? ` - job ${source.job_id}` : ''}${source.section ? ` - ${source.section}` : ''}`), element('pre', source.text));
    }
    if (item.suggestions_unavailable) block.append(element('p', 'A saved semantic suggestion is no longer available.'));
    if (item.suggestions?.length) block.append(element('p', 'Unverified semantic suggestions; these are not supporting evidence.'));
    for (const source of item.suggestions || []) {
      block.append(element('h4', `${source.title}${source.collection ? ` - ${source.collection}` : ''}${source.page ? ` - page ${source.page}` : ''}${source.job_id ? ` - job ${source.job_id}` : ''}${source.section ? ` - ${source.section}` : ''}`), element('pre', source.text));
    }
    turns.push(block);
  }
  $("conversation-turns").replaceChildren(...turns);
}
async function selectConversation(id) {
  const session = sessionGeneration, view = ++conversationGeneration;
  conversationId = null; $("conversation-form").hidden = true;
  $("conversation-delete").hidden = true;
  $("conversation-form").reset(); $("conversation-turns").replaceChildren();
  $("conversation-current").textContent = 'Loading conversation...';
  const result = await api(`/api/knowledge/conversations/${id}/turns`);
  ensureCurrent(session); if (view !== conversationGeneration) return;
  conversationId = id; $("conversation-current").textContent = `Conversation ${id}`;
  $("conversation-delete").hidden = false;
  $("conversation-form").hidden = false; showConversationTurns(result.items);
}
$("conversation-new").addEventListener('click', () => action($("conversation-new"), async () => {
  const session = sessionGeneration, view = ++conversationGeneration;
  conversationId = null; $("conversation-form").hidden = true;
  $("conversation-delete").hidden = true;
  $("conversation-form").reset(); $("conversation-turns").replaceChildren();
  $("conversation-current").textContent = 'Creating conversation...';
  const result = await api('/api/knowledge/conversations', {method: 'POST'});
  ensureCurrent(session); if (view !== conversationGeneration) return;
  conversationId = result.id; $("conversation-current").textContent = `Conversation ${result.id}`;
  $("conversation-delete").hidden = false;
  $("conversation-form").hidden = false;
}));
$("conversation-delete").addEventListener('click', () => action($("conversation-delete"), async () => {
  const session = sessionGeneration, view = conversationGeneration, id = conversationId;
  if (id === null || !window.confirm('Delete this conversation and all its saved questions?')) return;
  await api(`/api/knowledge/conversations/${id}`, {method: 'DELETE'});
  ensureCurrent(session);
  if (view === conversationGeneration && id === conversationId) {
    conversationGeneration++; conversationId = null;
    $("conversation-form").reset(); $("conversation-form").hidden = true;
    $("conversation-delete").hidden = true; $("conversation-turns").replaceChildren();
    $("conversation-current").textContent = 'No conversation selected.';
  }
  $("conversation-list-results").replaceChildren();
  message('Conversation deleted.');
}));
$("conversation-list").addEventListener('click', () => action($("conversation-list"), async () => {
  const session = sessionGeneration;
  const result = await api('/api/knowledge/conversations'); ensureCurrent(session);
  const rows = result.items.map(item => button(`Conversation ${item.id}`, () => selectConversation(item.id)));
  $("conversation-list-results").replaceChildren(...(rows.length ? rows : [element('p', 'No saved conversations.')]));
}));
$("conversation-form").addEventListener('submit', event => {
  event.preventDefault();
  action(event.submitter, async () => {
    const session = sessionGeneration, view = conversationGeneration, id = conversationId;
    if (id === null) throw new Error('Select a conversation first.');
    const generate = event.submitter?.value === 'generate';
    if (generate && !window.confirm('Send this question, the previous question in this conversation, and authorized excerpts to Groq? Use synthetic data only.')) return;
    const saved = await api(`/api/knowledge/conversations/${id}/turns`, jsonPost({question: $("conversation-question").value, collection: $("conversation-collection-filter").value || null, job_id: $("conversation-job-filter").value.trim() || null, section: $("conversation-section-filter").value.trim() || null, generate_answer: generate, experimental_hybrid: $("conversation-hybrid").checked === true}));
    ensureCurrent(session); if (view !== conversationGeneration || id !== conversationId) return;
    $("conversation-form").reset(); await selectConversation(id);
    ensureCurrent(session); if (conversationId === id) message(saved.notice);
  });
});
$("knowledge-add").addEventListener("submit", (event) => {
  event.preventDefault();
  action(event.submitter, async () => {
    const generation = sessionGeneration, page = $("knowledge-page").value;
    await api('/api/knowledge/documents/text', jsonPost({title: $("knowledge-title").value, text: $("knowledge-text").value,
      audience: $("knowledge-audience").value, collection: $("knowledge-collection").value, page: page ? Number(page) : null,
      job_id: $("knowledge-job-id").value.trim() || null, section: $("knowledge-section").value.trim() || null}));
    ensureCurrent(generation); $("knowledge-add").reset(); message('Synthetic document added.');
  });
});
$("knowledge-pdf-file").addEventListener('change', () => {
  pdfReview = null; $("knowledge-pdf-preview").replaceChildren(); $("knowledge-pdf-import").disabled = true;
});
$("knowledge-pdf-preview-button").addEventListener('click', () => action($("knowledge-pdf-preview-button"), async () => {
  const generation = sessionGeneration, file = $("knowledge-pdf-file").files[0];
  if (!file) throw new Error('Select a PDF first.');
  pdfReview = null; $("knowledge-pdf-import").disabled = true; $("knowledge-pdf-preview").replaceChildren();
  const result = await api(`/api/knowledge/documents/pdf-preview?filename=${encodeURIComponent(file.name)}`, {method: 'POST', body: file});
  ensureCurrent(generation);
  if ($("knowledge-pdf-file").files[0] !== file) return;
  const container = $("knowledge-pdf-preview");
  for (const page of result.pages) {
    const group = element('fieldset'); group.append(element('legend', `Page ${page.page}`));
    page.lines.forEach((line, index) => {
      const row = element('div');
      if (line.trim() && line.trim().length <= 200 && !/[\x00-\x1f]/.test(line.trim())) {
        const input = document.createElement('input'); input.type = 'checkbox'; input.dataset.page = page.page; input.dataset.line = index;
        const label = element('label'); label.append(input, document.createTextNode(` ${line}`)); row.append(label);
      } else row.textContent = line;
      group.append(row);
    });
    container.append(group);
  }
  pdfReview = {file, digest: result.digest}; $("knowledge-pdf-import").disabled = false;
}));
$("knowledge-pdf").addEventListener('submit', event => {
  event.preventDefault();
  action(event.submitter, async () => {
    const generation = sessionGeneration, review = pdfReview;
    if (!review || $("knowledge-pdf-file").files[0] !== review.file) throw new Error('Preview the PDF first.');
    const marks = [...$("knowledge-pdf-preview").querySelectorAll('input:checked')].map(input => ({page: Number(input.dataset.page), line: Number(input.dataset.line)}));
    const params = new URLSearchParams({filename: review.file.name, title: $("knowledge-pdf-title").value, audience: $("knowledge-pdf-audience").value,
      collection: $("knowledge-pdf-collection").value, digest: review.digest, marks: JSON.stringify(marks)});
    if ($("knowledge-pdf-job").value.trim()) params.set('job_id', $("knowledge-pdf-job").value.trim());
    const result = await api(`/api/knowledge/documents/pdf?${params}`, {method: 'POST', body: review.file});
    ensureCurrent(generation); pdfReview = null; $("knowledge-pdf").reset(); $("knowledge-pdf-preview").replaceChildren();
    $("knowledge-pdf-import").disabled = true; message(`Imported ${result.segments} reviewed text segments.`);
  }).finally(() => { if (!pdfReview) $("knowledge-pdf-import").disabled = true; });
});
async function loadKnowledgeDocuments() {
  const generation = sessionGeneration;
  const result = await api('/api/knowledge/documents');
  ensureCurrent(generation);
  const nodes = [];
  if (!result.items.length) nodes.push(element('p', 'No active documents.'));
  for (const document of result.items) {
    const row = element('p', `${document.title} - ${document.collection} - ${document.audience}${document.page ? ` - page ${document.page}` : ''}${document.job_id ? ` - job ${document.job_id}` : ''}${document.section ? ` - ${document.section}` : ''} `);
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
    if (job.pipeline.length) {
      const chart = element('div'); chart.className = 'pipeline-chart';
      chart.append(element('h4', 'Current application status'));
      for (const item of job.pipeline) {
        const line = element('div'); line.className = 'pipeline-row';
        const label = `${item.status}: ${item.count} of ${job.applications_total}`;
        const bar = element('progress'); bar.max = job.applications_total; bar.value = item.count;
        bar.setAttribute('aria-label', label);
        line.append(element('span', label), bar); chart.append(line);
      }
      row.append(chart);
    }
    const milestones = job.milestones || {};
    row.append(element('p', `Recorded milestones (distinct applications): screened ${milestones.screened ?? 0}; advanced ${milestones.advanced ?? 0}; interview completed ${milestones.interview_completed ?? 0}; offer presented ${milestones.offer_presented ?? 0}; hired ${milestones.hired ?? 0}; rejected ${milestones.rejected ?? 0}.`));
    nodes.push(row);
  }
  $('dashboard-results').replaceChildren(...nodes);
}));
$("more-notices").addEventListener("click", () => action($("more-notices"), () => loadNotices(true)));
$("load-submissions").addEventListener("click", () => action($("load-submissions"), () => loadSubmissionNotices()));
$("more-submissions").addEventListener("click", () => action($("more-submissions"), () => loadSubmissionNotices(true)));
$("load-withdrawals").addEventListener("click", () => action($("load-withdrawals"), () => loadWithdrawalNotices()));
$("more-withdrawals").addEventListener("click", () => action($("more-withdrawals"), () => loadWithdrawalNotices(true)));
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
