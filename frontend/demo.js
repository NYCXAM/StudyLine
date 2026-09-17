const initialDemoQueue = [
  {
    id: "maya",
    name: "Maya Chen",
    course: "CMSC 216",
    need: "Debugging help",
    question: "My C program stores incoming values in a dynamically allocated array. It works for small inputs, but after the array fills and I call realloc, the program sometimes segfaults or prints corrupted values. Valgrind reports an invalid write near the resizing code, and I am not sure whether I am updating the pointer or capacity incorrectly.",
    summary: "Likely memory-management bug while resizing a dynamic array, centered on pointer or capacity handling after realloc.",
    tags: ["C", "Memory", "Debugging"],
    minutes: 12,
  },
  {
    id: "jordan",
    name: "Jordan Lee",
    course: "MATH 141",
    need: "Concept question",
    question: "I can complete both substitution and integration by parts when the method is given, but I have trouble deciding which one to start with on homework problems. For example, products involving logarithms and polynomials look different from nested functions, yet I still choose the wrong technique. Could someone explain the clues I should look for before beginning the calculation?",
    summary: "Needs a decision framework for choosing between substitution and integration by parts.",
    tags: ["Calculus", "Integration"],
    minutes: 8,
  },
];

const demoSection = {
  title: "Max Cai's Office Hour",
  course: "CMSC 216",
  time: "Today · 2:00–3:30 PM",
  location: "IRB 1207 · 2 TAs holding this section",
  zoom: "https://zoom.us/j/123456789",
};

let demoView = "student";
let demoQueue = cloneQueue(initialDemoQueue);
let currentStudent = null;
let recruiterEntryId = null;
let servedCount = 4;

const demoApp = document.querySelector("#demoApp");
const demoNotice = document.querySelector("#demoNotice");

document.querySelectorAll("[data-demo-view]").forEach((button) => {
  button.addEventListener("click", () => setDemoView(button.dataset.demoView));
});

document.querySelector("#resetDemo").addEventListener("click", resetDemo);

function cloneQueue(queue) {
  return queue.map((entry) => ({ ...entry, tags: [...entry.tags] }));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setDemoView(view) {
  demoView = view;
  document.querySelectorAll("[data-demo-view]").forEach((button) => {
    const active = button.dataset.demoView === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  renderDemo();
}

function showNotice(message) {
  demoNotice.textContent = message;
  demoNotice.classList.add("visible");
}

function clearNotice() {
  demoNotice.textContent = "";
  demoNotice.classList.remove("visible");
}

function resetDemo() {
  demoQueue = cloneQueue(initialDemoQueue);
  currentStudent = null;
  recruiterEntryId = null;
  servedCount = 4;
  clearNotice();
  setDemoView("student");
  showNotice("Demo reset. Start by joining the queue as a student.");
}

function waitMinutesFor(position) {
  if (!position) return 0;
  return Math.ceil((position * 10) / 2);
}

function sectionBanner() {
  return `
    <section class="demo-section-banner">
      <div>
        <p class="eyebrow">Live section · Demo</p>
        <h2>${escapeHtml(demoSection.title)}</h2>
        <p>${escapeHtml(demoSection.course)} · ${escapeHtml(demoSection.time)} · ${escapeHtml(demoSection.location)}</p>
      </div>
      <a class="demo-zoom-link" href="${escapeHtml(demoSection.zoom)}" target="_blank" rel="noopener noreferrer">Join Zoom ↗</a>
    </section>
  `;
}

function studentView() {
  const position = recruiterEntryId ? demoQueue.findIndex((entry) => entry.id === recruiterEntryId) + 1 : 0;
  const isCurrent = currentStudent?.id === recruiterEntryId;
  const joined = Boolean(position || isCurrent);

  return `
    ${sectionBanner()}
    <section class="demo-grid">
      <article class="panel demo-panel">
        <div class="demo-panel-heading">
          <div><p class="eyebrow">Student experience</p><h3>${joined ? "You are in the virtual line" : "Join the virtual line"}</h3></div>
          <span class="turn-badge">${isCurrent ? "Being helped" : position ? `#${position} in line` : "Open"}</span>
        </div>
        ${joined ? studentStatus(position, isCurrent) : studentJoinForm()}
      </article>
      <aside class="panel demo-panel">
        <p class="panel-label">What this demonstrates</p>
        <ul class="demo-feature-list">
          <li><strong>Live queue position</strong><span>Students see their place and estimated wait update.</span></li>
          <li><strong>Section access</strong><span>Meeting details, active TAs, and Zoom link stay together.</span></li>
          <li><strong>AI-assisted intake</strong><span>The TA receives a concise summary without replacing the original question.</span></li>
          <li><strong>Next-in-line notification</strong><span>Students can be notified when it is nearly their turn.</span></li>
        </ul>
      </aside>
    </section>
  `;
}

function studentJoinForm() {
  return `
    <form id="demoJoinForm" class="demo-form">
      <label>Your name<input name="name" value="Alex Recruiter" required /></label>
      <label>Type of help
        <select name="need"><option>Debugging help</option><option>Concept question</option><option>Assignment review</option><option>Exam prep</option></select>
      </label>
      <label>Question details
        <textarea name="question" rows="6" required>My recursive merge sort returns the correct sorted output for every test case, but my runtime analysis keeps giving O(n²) instead of O(n log n). I wrote the recurrence as T(n) = 2T(n/2) + n, then expanded it level by level and probably counted the work incorrectly. I need help finding the mistake and understanding why the merge work across each level totals n.</textarea>
      </label>
      <button class="primary-action" type="submit">Join queue and generate AI summary</button>
    </form>
  `;
}

function studentStatus(position, isCurrent) {
  return `
    <div class="demo-position">
      <div><span>Status</span><strong>${isCurrent ? "Now" : "Waiting"}</strong></div>
      <div><span>Place</span><strong>${isCurrent ? "—" : position}</strong></div>
      <div><span>Estimated wait</span><strong>${isCurrent ? "0 min" : `${waitMinutesFor(position)} min`}</strong></div>
    </div>
    <p>${isCurrent ? "The TA called you. In the real app, a browser notification can alert you when you are next." : "Your question has been added with an AI-assisted summary for the TA."}</p>
    <div class="demo-actions">
      <button id="viewAsTa" class="primary-action" type="button">See what the TA sees</button>
      <button id="leaveDemoQueue" class="secondary-action" type="button">Leave queue</button>
    </div>
  `;
}

function taView() {
  return `
    ${sectionBanner()}
    <section class="demo-grid">
      <article class="panel demo-panel">
        <div class="demo-panel-heading">
          <div><p class="eyebrow">TA workspace</p><h3>Students waiting for help</h3><p>Original questions and AI summaries are intentionally separated.</p></div>
          <span class="forecast-note">${servedCount} served</span>
        </div>
        <div class="demo-queue-list">
          ${demoQueue.length ? demoQueue.map(queueCard).join("") : '<div class="demo-empty">The waiting line is empty.</div>'}
        </div>
      </article>
      <aside class="panel demo-panel">
        <p class="panel-label">Currently helping</p>
        <h3>${escapeHtml(currentStudent?.name || "No student called")}</h3>
        <p>${currentStudent ? `${escapeHtml(currentStudent.course)} · ${escapeHtml(currentStudent.need)}` : "Call the next student when ready."}</p>
        <div class="demo-actions">
          <button id="demoCallNext" class="primary-action" type="button" ${currentStudent || !demoQueue.length ? "disabled" : ""}>Call next</button>
          <button id="demoMarkServed" class="secondary-action" type="button" ${currentStudent ? "" : "disabled"}>Mark served</button>
        </div>
        <hr />
        <div class="demo-position">
          <div><span>Waiting</span><strong>${demoQueue.length}</strong></div>
          <div><span>Section TAs</span><strong>2</strong></div>
          <div><span>Est. wait</span><strong>${waitMinutesFor(demoQueue.length)} min</strong></div>
        </div>
      </aside>
    </section>
  `;
}

function queueCard(entry, index) {
  return `
    <article class="demo-queue-card">
      <span class="queue-position">${index + 1}</span>
      <div class="demo-queue-card-content">
        <strong>${escapeHtml(entry.name)}</strong>
        <span>${escapeHtml(entry.course)} · ${escapeHtml(entry.need)}</span>
        <div class="demo-question-block demo-question-detail">
          <p class="demo-question-label">Question details · Student provided</p>
          <p>${escapeHtml(entry.question)}</p>
        </div>
        <div class="demo-question-block demo-ai-summary">
          <p class="demo-question-label"><span class="demo-ai-mark">AI</span> AI summary · Generated</p>
          <p>${escapeHtml(entry.summary)}</p>
          <div class="demo-tag-row">${entry.tags.map((tag) => `<span class="tag medium">${escapeHtml(tag)}</span>`).join("")}</div>
        </div>
      </div>
      <span class="tag medium">${entry.minutes} min</span>
    </article>
  `;
}

function createDemoSummary(question, need) {
  const trimmed = question.trim();
  const lower = trimmed.toLowerCase();
  if (lower.includes("merge sort") || lower.includes("recurrence")) {
    return "Needs help correcting a merge-sort recurrence analysis and explaining its O(n log n) runtime.";
  }
  if (lower.includes("segfault") || lower.includes("realloc")) {
    return "Likely memory-management bug involving pointer or capacity handling after realloc.";
  }
  const words = trimmed.split(/\s+/).slice(0, 14).join(" ");
  return `${need}: ${words}${trimmed.split(/\s+/).length > 14 ? "…" : ""}`;
}

function renderDemo() {
  demoApp.innerHTML = demoView === "student" ? studentView() : taView();
  bindDemoActions();
}

function bindDemoActions() {
  document.querySelector("#demoJoinForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const question = String(data.get("question") || "").trim();
    const need = String(data.get("need") || "Office-hours help");
    recruiterEntryId = "recruiter";
    demoQueue.push({
      id: recruiterEntryId,
      name: String(data.get("name") || "Alex Recruiter"),
      course: "CMSC 216",
      need,
      question,
      summary: createDemoSummary(question, need),
      tags: need === "Debugging help" ? ["Algorithms", "Runtime", "Debugging"] : ["Concept", "Review"],
      minutes: need === "Debugging help" ? 10 : 8,
    });
    showNotice("You joined the queue. Switch to TA view to compare your question with its AI summary.");
    renderDemo();
  });

  document.querySelector("#viewAsTa")?.addEventListener("click", () => setDemoView("ta"));
  document.querySelector("#leaveDemoQueue")?.addEventListener("click", () => {
    demoQueue = demoQueue.filter((entry) => entry.id !== recruiterEntryId);
    if (currentStudent?.id === recruiterEntryId) currentStudent = null;
    recruiterEntryId = null;
    showNotice("You left the demo queue.");
    renderDemo();
  });
  document.querySelector("#demoCallNext")?.addEventListener("click", () => {
    currentStudent = demoQueue.shift() || null;
    const message = currentStudent?.id === recruiterEntryId
      ? "You are next. The real app can send a browser notification at this point."
      : `${currentStudent?.name || "The next student"} was called. Queue positions and wait estimates updated.`;
    showNotice(message);
    renderDemo();
  });
  document.querySelector("#demoMarkServed")?.addEventListener("click", () => {
    if (!currentStudent) return;
    const servedName = currentStudent.name;
    if (currentStudent.id === recruiterEntryId) recruiterEntryId = null;
    currentStudent = null;
    servedCount += 1;
    showNotice(`${servedName} was marked served. The TA can now call the next student.`);
    renderDemo();
  });
}

setDemoView("student");
