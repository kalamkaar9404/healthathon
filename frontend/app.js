/* CancerCare — client. Vanilla, no framework: the whole app is one screen and
   a handful of tables, and a build step would cost more than it returns. */

const $ = (id) => document.getElementById(id);

const state = {
  documents: [],   // { filename, text }
  record: null,
  audit: null,
  selected: null,
};

// ---------- helpers ----------

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function fmtDate(iso) {
  if (!iso) return "—";
  const [y, m, d] = iso.split("-").map(Number);
  return `${String(d).padStart(2, "0")} ${MONTHS[m - 1]} ${y}`;
}

function el(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined) n.textContent = text;
  return n;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

// ---------- loading documents ----------

function addDocuments(docs) {
  state.documents.push(...docs);
  renderDocList();
  $("process").disabled = state.documents.length === 0;
}

function renderDocList() {
  const list = $("doclist");
  clear(list);
  state.documents.forEach((doc, i) => {
    const li = el("li", "doc");
    li.appendChild(el("div", "doc-name", doc.filename));
    const meta = el("div", "doc-meta");
    const a = state.audit && state.audit[i];
    if (a) {
      li.classList.add("done");
      meta.appendChild(el("span", null, a.kind.replace(/_/g, " ")));
      meta.appendChild(el("span", null, `${a.fields} fields`));
      meta.appendChild(el("span", null, `${a.redaction_count} redacted`));
    } else {
      meta.appendChild(el("span", null, `${(doc.text.length / 1024).toFixed(1)} kB`));
      meta.appendChild(el("span", null, "not yet read"));
    }
    li.appendChild(meta);
    list.appendChild(li);
  });
}

async function loadSample() {
  const res = await fetch("/api/samples");
  const data = await res.json();
  state.documents = [];
  state.audit = null;
  addDocuments(data.documents);
}

function readFiles(fileList) {
  const jobs = [...fileList].map(
    (f) =>
      new Promise((resolve) => {
        const r = new FileReader();
        r.onload = () => resolve({ filename: f.name, text: String(r.result) });
        r.readAsText(f);
      })
  );
  Promise.all(jobs).then(addDocuments);
}

// ---------- build ----------

async function build() {
  const btn = $("process");
  btn.disabled = true;
  btn.textContent = "Building…";

  const res = await fetch("/api/process", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      texts: state.documents.map((d) => d.text),
      filenames: state.documents.map((d) => d.filename),
    }),
  });

  if (!res.ok) {
    btn.textContent = "Build record";
    btn.disabled = false;
    const note = $("privacy-note");
    $("privacy-block").hidden = false;
    note.textContent = "The record could not be built. Check that the documents are readable text and try again.";
    return;
  }

  const data = await res.json();
  state.record = data.record;
  state.audit = data.audit;

  renderDocList();
  renderRecord();

  btn.textContent = "Rebuild record";
  btn.disabled = false;
}

// ---------- rendering ----------

function renderRecord() {
  const r = state.record;
  $("empty").hidden = true;
  $("record").hidden = false;

  $("subject").hidden = false;
  $("subject-id").textContent = r.patient_pseudonym;

  renderDoses(r.exposures);
  renderToxGrid(r.trajectories);
  renderLabGrid(r.lab_trends);
  renderLedger(r.timeline);
  renderFlags(r.flags);
  renderPrivacy();

  $("coverage").textContent =
    r.held_for_review === 0
      ? `All ${r.field_count} fields cleared the confidence gate. `
      : `${r.auto_filled} of ${r.field_count} fields cleared the confidence gate; ${r.held_for_review} held for review. `;
}

function renderDoses(exposures) {
  const wrap = $("doses");
  clear(wrap);

  exposures.forEach((e, i) => {
    const row = el("div", "dose settling");
    row.style.animationDelay = `${i * 50}ms`;

    const name = el("div", "dose-name");
    name.appendChild(document.createTextNode(e.drug_generic ? e.drug_generic[0].toUpperCase() + e.drug_generic.slice(1) : "Unresolved"));
    name.appendChild(el("i", null, `${e.cycles_recorded} cycle${e.cycles_recorded === 1 ? "" : "s"} on file`));
    row.appendChild(name);

    const track = el("div", "track");
    const figures = el("div", "dose-figures");

    if (e.ceiling && e.fraction_of_ceiling !== null) {
      const pct = Math.min(e.fraction_of_ceiling, 1.12);
      const fill = el("div", "fill");
      if (e.fraction_of_ceiling >= 0.9) fill.classList.add("over");
      else if (e.fraction_of_ceiling >= 0.7) fill.classList.add("warn");
      // scale so the ceiling tick sits at 85% of the track, leaving headroom
      // to show an overshoot rather than clipping it at the edge
      fill.style.width = `${Math.min(pct * 85, 100)}%`;
      track.appendChild(fill);

      const tick = el("div", "tick");
      tick.style.left = "85%";
      tick.title = "Lifetime ceiling";
      track.appendChild(tick);

      const total = el("b", null, `${e.cumulative}  of  ${e.ceiling} ${e.unit}`);
      figures.appendChild(total);
      figures.appendChild(
        el(
          "span",
          null,
          e.cycles_remaining_at_current_dose !== null
            ? `${Math.round(e.fraction_of_ceiling * 100)}% — room for about ${e.cycles_remaining_at_current_dose} more`
            : `${Math.round(e.fraction_of_ceiling * 100)}% of ceiling`
        )
      );
    } else {
      figures.appendChild(el("b", null, `${e.cumulative} ${e.unit}`));
      figures.appendChild(el("span", "uncapped", "no lifetime ceiling"));
    }

    row.appendChild(track);
    row.appendChild(figures);
    wrap.appendChild(row);
  });

  if (!exposures.length) {
    wrap.appendChild(el("p", "panel-note", "No dosed drugs found in these documents."));
  }
}

function renderToxGrid(trajectories) {
  const table = $("toxgrid");
  clear(table);

  if (!trajectories.length) {
    $("tox-panel").hidden = true;
    return;
  }
  $("tox-panel").hidden = false;

  const cycles = [...new Set(trajectories.flatMap((t) => t.points.map((p) => p.cycle)).filter((c) => c !== null))].sort((a, b) => a - b);

  const thead = el("thead");
  const hr = el("tr");
  hr.appendChild(el("th", "row-label", "Toxicity"));
  cycles.forEach((c) => hr.appendChild(el("th", null, `C${c}`)));
  hr.appendChild(el("th", "dir", "Direction"));
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = el("tbody");
  trajectories.forEach((t, i) => {
    const tr = el("tr", "settling");
    tr.style.animationDelay = `${i * 45}ms`;
    tr.appendChild(el("td", "row-label", t.ctcae_term));

    cycles.forEach((c) => {
      const pt = t.points.find((p) => p.cycle === c);
      if (pt) tr.appendChild(el("td", `g${pt.grade}`, String(pt.grade)));
      else tr.appendChild(el("td", "none", "·"));
    });

    tr.appendChild(el("td", `dir ${t.direction.replace(/\s/g, "-")}`, t.direction));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
}

function renderLabGrid(trends) {
  const table = $("labgrid");
  clear(table);

  const useful = trends.filter((t) => t.values.length > 1);
  if (!useful.length) {
    $("labs-panel").hidden = true;
    return;
  }
  $("labs-panel").hidden = false;

  const maxLen = Math.max(...useful.map((t) => t.values.length));

  const thead = el("thead");
  const hr = el("tr");
  hr.appendChild(el("th", "row-label", "Test"));
  for (let i = 0; i < maxLen; i++) hr.appendChild(el("th", null, i === 0 ? "Baseline" : `V${i + 1}`));
  hr.appendChild(el("th", "dir", "Direction"));
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = el("tbody");
  useful.forEach((t, i) => {
    const tr = el("tr", "settling");
    tr.style.animationDelay = `${i * 40}ms`;

    const label = el("td", "row-label");
    label.appendChild(document.createTextNode(t.test_canonical));
    if (t.unit) label.appendChild(el("i", null, t.unit));
    tr.appendChild(label);

    for (let j = 0; j < maxLen; j++) {
      const v = t.values[j];
      if (v === undefined) {
        tr.appendChild(el("td", "none", "·"));
      } else {
        const last = j === t.values.length - 1;
        tr.appendChild(el("td", last && t.out_of_range ? "oor" : null, String(v)));
      }
    }

    tr.appendChild(el("td", `dir ${t.direction.replace(/\s/g, "-")}`, t.direction));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
}

function renderLedger(timeline) {
  const table = $("ledger");
  clear(table);

  const tbody = el("tbody");
  timeline.forEach((entry, i) => {
    const tr = el("tr", "settling");
    tr.style.animationDelay = `${Math.min(i, 24) * 18}ms`;
    tr.tabIndex = 0;
    tr.setAttribute("role", "button");
    if (entry.abstain) tr.classList.add("held");

    tr.appendChild(el("td", "l-date", fmtDate(entry.on)));
    tr.appendChild(el("td", "l-cycle", entry.cycle !== null ? `C${entry.cycle}` : ""));
    tr.appendChild(el("td", "l-kind", entry.kind));
    tr.appendChild(el("td", "l-label", entry.label));

    const detail = el("td", "l-detail");
    if (entry.abstain) {
      detail.appendChild(el("span", "held-mark", "held for review"));
    } else {
      detail.textContent = entry.detail;
    }
    tr.appendChild(detail);

    const conf = el("td", "l-conf");
    const pip = el("span", "pip");
    if (entry.abstain) pip.classList.add("held");
    else if (entry.confidence < 0.85) pip.classList.add("mid");
    conf.appendChild(pip);
    conf.appendChild(document.createTextNode(entry.confidence.toFixed(2)));
    tr.appendChild(conf);

    const open = () => selectEntry(entry, tr);
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        open();
      }
    });

    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
}

function renderFlags(flags) {
  const block = $("flags-block");
  const list = $("flaglist");
  clear(list);

  if (!flags.length) {
    block.hidden = true;
    return;
  }
  block.hidden = false;

  flags.forEach((f) => {
    const li = el("li", `flag ${f.severity}`);
    li.appendChild(el("span", "flag-kind", f.kind.replace(/_/g, " ")));
    li.appendChild(document.createTextNode(f.message));
    list.appendChild(li);
  });
}

function renderPrivacy() {
  const block = $("privacy-block");
  const list = $("redactions");
  clear(list);

  const total = state.audit.reduce((n, a) => n + a.redaction_count, 0);
  const kinds = {};
  state.audit.forEach((a) => a.redactions.forEach((r) => { kinds[r.kind] = (kinds[r.kind] || 0) + 1; }));

  block.hidden = false;
  $("privacy-note").textContent =
    `${total} identifier${total === 1 ? "" : "s"} replaced before any page was read. Pseudonyms are stable, so the same patient still links across hospitals; the lookup stays on this machine.`;

  Object.entries(kinds)
    .sort((a, b) => b[1] - a[1])
    .forEach(([kind, n]) => {
      const li = el("li");
      li.appendChild(el("span", null, kind.toLowerCase()));
      li.appendChild(el("span", null, String(n)));
      list.appendChild(li);
    });
}

// ---------- inspector ----------

function selectEntry(entry, tr) {
  document.querySelectorAll('.ledger tr[aria-selected="true"]').forEach((n) => n.removeAttribute("aria-selected"));
  tr.setAttribute("aria-selected", "true");

  const doc = state.record.documents.find((d) => d.id === entry.document_id);
  const audit = state.audit.find((a) => a.document_id === entry.document_id);

  const body = $("insp-body");
  clear(body);

  const add = (term, value, cls) => {
    body.appendChild(el("dt", null, term));
    body.appendChild(el("dd", cls, value));
  };

  add("field", entry.label);
  add("value", entry.abstain ? "held for review" : entry.detail, entry.abstain ? "reason" : null);
  add("recorded", fmtDate(entry.on));
  if (entry.cycle !== null) add("cycle", String(entry.cycle));
  add("source", audit ? audit.filename : entry.document_id);
  if (doc && doc.issuing_site) add("issued by", doc.issuing_site);
  add("confidence", entry.confidence.toFixed(2));

  const raw = findRaw(doc, entry);
  if (raw) add("read from", raw, "quote");

  if (entry.abstain) {
    add("held because", "the drug or test name did not resolve to a known code, so the value is not auto-filled.", "reason");
  }

  $("inspector").hidden = false;
  state.selected = entry;
}

function findRaw(doc, entry) {
  if (!doc) return null;
  const pools = [doc.drugs, doc.labs, doc.toxicities];
  for (const pool of pools) {
    for (const rec of pool) {
      const name = rec.drug_generic || rec.test_canonical || rec.ctcae_term || rec.drug_raw || rec.test_raw || rec.term_raw;
      if (name && entry.label.toLowerCase().includes(String(name).toLowerCase().slice(0, 6))) {
        return rec.provenance && rec.provenance.raw_text;
      }
    }
  }
  return null;
}

// ---------- wiring ----------

$("load-sample").addEventListener("click", loadSample);
$("load-sample-2").addEventListener("click", loadSample);
$("process").addEventListener("click", build);
$("insp-close").addEventListener("click", () => {
  $("inspector").hidden = true;
  document.querySelectorAll('.ledger tr[aria-selected="true"]').forEach((n) => n.removeAttribute("aria-selected"));
});

const dz = $("dropzone");
const input = $("file-input");

dz.addEventListener("click", () => input.click());
dz.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    input.click();
  }
});
input.addEventListener("change", () => {
  if (input.files.length) readFiles(input.files);
  input.value = "";
});

["dragenter", "dragover"].forEach((t) =>
  dz.addEventListener(t, (e) => {
    e.preventDefault();
    dz.classList.add("over");
  })
);
["dragleave", "drop"].forEach((t) =>
  dz.addEventListener(t, (e) => {
    e.preventDefault();
    dz.classList.remove("over");
  })
);
dz.addEventListener("drop", (e) => {
  if (e.dataTransfer.files.length) readFiles(e.dataTransfer.files);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("inspector").hidden) $("insp-close").click();
});
