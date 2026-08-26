/* ============================================================================
   VoxTell landing — the hero instrument and the vocabulary search.

   THE THESIS OF THIS PAGE IS THE INTEGRATION, NOT THE MODEL, so the hero is the
   screen a planner actually recognises: Eclipse's structure list, mid-review.
   Prompts on the left, the slice in the middle, and an APPROVAL SHEET underneath
   — one row per structure, with the structure Id the plugin would write, its
   DicomType, its volume, its loop count on this slice, and a tick.

   The tick is the whole argument. Untick everything and the sheet's own footer
   says "nothing is written", because that is literally what the plugin does: it
   plans the import, shows you the plan, and writes only what you approved. No
   copy on the page has to claim that; the instrument demonstrates it.

   Assets, all real:
     assets/hero-contours.v3.json   VoxTell output for one slice, plus the
                                    vocabulary that resolves to it and the
                                    DicomType per structure (schema 3)
     assets/hero-slice.v2.webp      the windowed axial slice itself
     assets/prompts.v1.json         all 14,194 phrasings from the model's
                                    bundled embedding bank, ~68 KB gzipped

   WHAT CHANGED FROM v2, AND WHY

   * The ledger became the approval sheet (above). v2's ledger said
     "N structures -> written to the open plan", which is the one thing the
     plugin never does on its own.
   * ASSET URLS ARE RESOLVED RELATIVE TO THIS SCRIPT, not from "/assets/...".
     Root-absolute fetches meant the demo only worked when the document was served
     from the site root: any local preview of a parent directory, or a file:// open,
     hit the catch and printed "The demo could not load" over an empty black box.
     That is indistinguishable from a real outage and it cost real debugging time.
   * The staggered draw timers are tracked and cleared. v2 scheduled i*140ms
     timeouts and neither undraw() nor clearAll() cancelled them, so pressing
     "clear" mid-stagger re-added rows to a sheet that had just been emptied.
   * The opening sequence is armed by an IntersectionObserver rather than a bare
     700ms timeout, so the one orchestrated moment on the page plays when somebody
     is looking at it instead of before the fonts have settled.

   Honesty rules, carried over unchanged:
     - The caption comes from the JSON's own `provenance`. Regenerate from a
       different source and the page re-describes itself rather than keeping a
       stale claim.
     - A prompt with no drawable structure is NOT a failure. 14 structures were
       contoured on this one slice; the bank holds 14,194 phrasings. The bank is
       searched for real and the true count reported.

   No dependencies. The catalogue fetch is deferred: the drawable demo works
   before it lands, and if it never lands only the search degrades.
   ========================================================================= */

(function () {
  "use strict";

  /* -- 0. where our assets live ------------------------------------------ */

  // document.currentScript is valid during execution of a deferred classic
  // script, which is how this is loaded. Resolving against it means the whole
  // hero works from any mount point — the site root, a subdirectory, or
  // `python3 -m http.server` run from inside landing/.
  var HERE = (function () {
    var self = document.currentScript;
    if (self && self.src) return self.src.replace(/[^/]*$/, "");
    return "/assets/";
  })();
  var asset = function (name) { return HERE + name; };

  var SCHEMA = 3;

  var panel = document.getElementById("demoStage");
  // The SVG belongs in the .demo__stage viewport, NOT in the .demo panel wrapper.
  // Inserting it into the wrapper once put the slice above the prompt bar and left
  // the viewport an empty box under the chips.
  var stage = panel ? panel.querySelector(".demo__stage") : null;
  var input = document.getElementById("demoInput");
  var hint = document.getElementById("demoHint");
  var chipBox = document.getElementById("demoChips");
  var rowBox = document.getElementById("demoRows");
  var sheetFoot = document.getElementById("demoFoot");
  var sheetCount = document.getElementById("demoSheetCount");
  var caption = document.getElementById("demoCaption");
  var emptyNote = document.getElementById("demoEmpty");

  var vocabInput = document.getElementById("vocabInput");
  var vocabCount = document.getElementById("vocabCount");
  var vocabResults = document.getElementById("vocabResults");

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var SVG_NS = "http://www.w3.org/2000/svg";

  // How many real phrasings to show before the visitor types anything. Enough to
  // fill the panel at desktop width without turning it into a scroller on load.
  var SEED_COUNT = 40;

  // The opening sequence. Five structures rather than three: the sheet has to look
  // like a structure set a planner would recognise, and a cord and a vertebra are
  // what make it read as radiotherapy rather than radiology.
  //
  // OPENING_TEXT IS LENGTH-CONSTRAINED, not merely chosen. The prompt field measures
  // ~283px at the 1180px container and the mono runs ~8.4px/char, so anything past
  // ~33 characters is silently ellipsised — the same class of bug that once
  // truncated a seeded prompt to "spinal c". These 31 characters resolve to exactly
  // the five structures below through the ordinary keyword path, so the field and
  // the sheet cannot disagree about what was asked for.
  var OPENING = ["Liver", "Spleen", "Aorta", "SpinalCord", "Vertebra_T12"];
  var OPENING_TEXT = "liver, spleen, aorta, cord, t12";

  var data = null;        // hero-contours.v3.json
  var catalogue = null;   // prompts.v1.json .prompts
  var shown = [];         // structure names on screen, in draw order
  var nodes = {};         // name -> { paths: [{outline, fill}], spec, row, tick }
  var chipFor = {};       // structure name -> the chip buttons that target it
  var drawTimers = [];    // every pending stagger timeout, so they can be cancelled

  /* -- helpers ----------------------------------------------------------- */

  var el = function (name, attrs) {
    var node = document.createElementNS(SVG_NS, name);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    return node;
  };

  var span = function (cls, text) {
    var node = document.createElement("span");
    node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

  var norm = function (s) {
    return String(s || "").toLowerCase().replace(/[^a-z0-9. ]+/g, " ").replace(/\s+/g, " ").trim();
  };

  var clearTimers = function () {
    drawTimers.forEach(window.clearTimeout);
    drawTimers = [];
  };

  /* -- 1. build the viewport --------------------------------------------- */

  /* The furniture round the image — window/level, a scale bar, orientation
     letters. Every one of these is real: the window comes from the DICOM
     headers, the bar is computed from the JSON's mm_per_px, and R/L are the
     radiological convention (image left is patient right). They are here because
     their absence is most of why a dark rectangle with a few outlines reads as a
     placeholder rather than a viewport — this is the chrome a planner sees all
     day, and putting it back costs nothing. */
  function buildFurniture(svg, W, H) {
    var g = el("g", { class: "vp", "aria-hidden": "true" });

    // Orientation. Image left is the patient's right in every axial display.
    var letters = [
      ["R", 10, H / 2, "start", "middle"],
      ["L", W - 10, H / 2, "end", "middle"],
      ["A", W / 2, 16, "middle", "hanging"],
      ["P", W / 2, H - 8, "middle", "auto"]
    ];
    letters.forEach(function (t) {
      var node = el("text", {
        class: "vp__orient", x: t[1], y: t[2],
        "text-anchor": t[3], "dominant-baseline": t[4]
      });
      node.textContent = t[0];
      g.appendChild(node);
    });

    // Window/level readout, top-left, as Eclipse shows it.
    if (data.window) {
      var wl = el("text", { class: "vp__read", x: 10, y: 18 });
      wl.textContent = "W " + data.window.width + "  L " + data.window.centre;
      g.appendChild(wl);
    }

    // Slice counter, top-right.
    if (data.slice_index != null && data.slice_count) {
      var sl = el("text", { class: "vp__read", x: W - 10, y: 18, "text-anchor": "end" });
      sl.textContent = "IM " + (data.slice_index + 1) + "/" + data.slice_count;
      g.appendChild(sl);
    }

    // A true scale bar. 5 cm if it fits in a third of the frame, else 2 cm.
    if (data.mm_per_px) {
      var mm = (50 / data.mm_per_px) <= W / 3 ? 50 : 20;
      var px = mm / data.mm_per_px;
      var x0 = 14, y0 = H - 16;
      g.appendChild(el("path", {
        class: "vp__scale",
        d: "M" + x0 + " " + (y0 - 4) + "V" + y0 + "H" + (x0 + px) + "V" + (y0 - 4)
      }));
      var lab = el("text", { class: "vp__read", x: x0 + px + 6, y: y0 + 1 });
      lab.textContent = (mm / 10) + " cm";
      g.appendChild(lab);
    }

    svg.appendChild(g);
  }

  function buildStage() {
    var W = data.width, H = data.height;
    var svg = el("svg", {
      viewBox: "0 0 " + W + " " + H,
      role: "img",
      "aria-label": "Axial CT slice with segmented structure contours"
    });

    // The slice itself. preserveAspectRatio is the default (meet) and the JSON's
    // width/height are the raster's, so the image maps 1:1 onto the viewBox and
    // the contour coordinates land exactly where the generator put them.
    svg.appendChild(el("image", {
      href: asset("hero-slice.v2.webp"), x: 0, y: 0, width: W, height: H, class: "vp__img"
    }));

    // Fills as one group, then outlines, so no fill ever paints over a
    // neighbouring structure's boundary.
    var fills = el("g", {});
    var lines = el("g", {});
    svg.appendChild(fills);
    svg.appendChild(lines);

    data.structures.forEach(function (s) {
      var colour = "var(--vx-" + s.token + ")";
      var pairs = [];
      // A structure can be several closed loops on one slice — the colon and the
      // costal cartilages always are. Each loop is its own path with its own
      // precomputed length, and they animate together.
      s.paths.forEach(function (p) {
        var fill = el("path", { d: p.d, class: "contour contour__fill", fill: colour });
        var outline = el("path", { d: p.d, class: "contour", stroke: colour });
        // --len is measured in viewBox units by the generator, and the stroke now
        // scales with the viewBox too. v2 also set vector-effect:non-scaling-stroke,
        // which measures the dash in SCREEN pixels — so at the 1180px container the
        // real path was ~7% longer than its own dash and that much of every
        // undrawn contour was visible before it had been asked for.
        outline.style.setProperty("--len", p.len);
        fills.appendChild(fill);
        lines.appendChild(outline);
        pairs.push({ outline: outline, fill: fill });
      });
      nodes[s.name] = { paths: pairs, spec: s, row: null, tick: null };
    });

    buildFurniture(svg, W, H);

    // Before the empty-state note, so the note keeps sitting on top of the slice
    // until the first structure is drawn.
    stage.insertBefore(svg, stage.firstChild);

    if (caption && data.caption) caption.textContent = data.caption;
  }

  /* -- 2. resolve free text to drawable structures ------------------------ */

  function resolve(query) {
    var q = norm(query);
    if (!q) return [];

    // Author-supplied aliases win: they are the "none of these is the canonical
    // name" cases, and they must not be second-guessed by substring matching.
    var aliases = data.aliases || {};
    var keys = Object.keys(aliases);
    for (var i = 0; i < keys.length; i++) {
      if (q === norm(keys[i])) return aliases[keys[i]].slice();
    }

    var wantsLeft = /\bleft\b|\bl\.\s|\bl\s|sinistr/.test(q);
    var wantsRight = /\bright\b|\br\.\s|\br\s|dextr/.test(q);

    var hits = data.structures.filter(function (s) {
      var matched = s.keywords.some(function (k) { return q.indexOf(norm(k)) !== -1; });
      if (!matched) return false;
      if (s.side === "left" && wantsRight) return false;
      if (s.side === "right" && wantsLeft) return false;
      return true;
    });

    return hits.map(function (s) { return s.name; });
  }

  /* -- 3. the approval sheet --------------------------------------------- */

  /* One row per structure the job returned, carrying the four fields a planner
     checks before an import: the Id that will appear in the structure list, the
     DicomType, the volume, and how many closed loops there are on this slice.
     Everything here comes out of the JSON — nothing is computed for effect.

     A <label> wraps the checkbox so the whole row is a hit target, and the
     children are all elements: the grid that lays this out would auto-place a
     bare text node into the next cell, which is exactly the bug that once
     rendered #safety one word per line down a 17px ribbon. */
  function sheetRow(spec) {
    var row = document.createElement("label");
    row.className = "sheet__row";
    row.setAttribute("data-name", spec.name);

    var tick = document.createElement("input");
    tick.type = "checkbox";
    tick.className = "sheet__tick";
    tick.checked = true;
    tick.setAttribute("aria-label", "Import " + spec.name);
    tick.addEventListener("change", function () {
      setDrawn(spec.name, tick.checked);
      row.classList.toggle("is-skipped", !tick.checked);
      syncFoot();
    });

    // A short contour fragment rather than a swatch: the mark is a small piece of
    // the thing it stands for.
    var mark = document.createElementNS(SVG_NS, "svg");
    mark.setAttribute("class", "sheet__mark");
    mark.setAttribute("viewBox", "0 0 26 12");
    mark.setAttribute("aria-hidden", "true");
    mark.appendChild(el("path", {
      d: "M1 8C5 2 9 1 13 4S21 11 25 5",
      stroke: "var(--vx-" + spec.token + ")"
    }));

    var id = span("sheet__id");
    id.appendChild(mark);
    id.appendChild(span("sheet__name", spec.name));

    row.appendChild(tick);
    row.appendChild(id);
    row.appendChild(span("sheet__type", spec.type || "Organ"));
    row.appendChild(span("sheet__num", spec.vol == null ? "—" : spec.vol.toFixed(1) + " cc"));
    row.appendChild(span("sheet__num sheet__loops", String(spec.paths.length)));
    // Every structure here is new: the reference series ships with an empty
    // structure set. Re-running a prompt against an existing structure is the
    // OVERWRITE case, which the plugin also reports — it just cannot happen here.
    row.appendChild(span("sheet__act", "NEW"));

    nodes[spec.name].row = row;
    nodes[spec.name].tick = tick;
    return row;
  }

  function ticked() {
    return shown.filter(function (n) {
      var t = nodes[n].tick;
      return !t || t.checked;
    });
  }

  function syncFoot() {
    if (sheetCount) {
      sheetCount.textContent = shown.length
        ? shown.length + " returned"
        : "awaiting prompts";
    }
    if (!sheetFoot) return;

    var keep = ticked();
    if (!shown.length) {
      sheetFoot.innerHTML =
        '<span class="sheet__footnote">Nothing segmented yet</span>' +
        '<span class="sheet__total">—</span>';
      return;
    }
    if (!keep.length) {
      // The point of the whole sheet, said by the sheet rather than by the copy.
      sheetFoot.innerHTML =
        '<span class="sheet__footnote sheet__footnote--none">Nothing ticked &mdash; ' +
        "nothing is written</span>" +
        '<span class="sheet__total">0 cc</span>';
      return;
    }
    var cc = keep.reduce(function (sum, n) { return sum + (nodes[n].spec.vol || 0); }, 0);
    sheetFoot.innerHTML =
      '<span class="sheet__footnote">Import <b>' + keep.length + " of " + shown.length +
      "</b> into the open structure set</span>" +
      '<span class="sheet__total">' +
      cc.toLocaleString("en-US", { maximumFractionDigits: 0 }) + " cc</span>";
  }

  // Chips reflect what is actually on screen. v1 set aria-pressed="true" on click
  // and never cleared it, so after a few clicks every chip claimed to be active
  // while the sheet told a different story.
  function syncChips() {
    Object.keys(chipFor).forEach(function (name) {
      chipFor[name].forEach(function (entry) {
        var on = entry.targets.every(function (t) { return shown.indexOf(t) !== -1; });
        entry.btn.setAttribute("aria-pressed", on ? "true" : "false");
      });
    });
  }

  function setDrawn(name, on) {
    nodes[name].paths.forEach(function (p) {
      p.outline.classList.toggle("is-drawn", on);
      p.fill.classList.toggle("is-drawn", on);
    });
  }

  function commit(name) {
    setDrawn(name, true);
    rowBox.appendChild(sheetRow(nodes[name].spec));
    shown.push(name);
    syncFoot();
    syncChips();
    if (emptyNote) emptyNote.hidden = true;
  }

  function draw(names) {
    var fresh = names.filter(function (n) { return nodes[n] && shown.indexOf(n) === -1; });
    if (!fresh.length) return;

    fresh.forEach(function (n, i) {
      if (reduceMotion || i === 0) { commit(n); return; }
      drawTimers.push(window.setTimeout(function () { commit(n); }, i * 140));
    });
  }

  function undraw(names) {
    clearTimers();
    names.forEach(function (n) {
      var at = shown.indexOf(n);
      if (at === -1) return;
      setDrawn(n, false);
      shown.splice(at, 1);
      if (nodes[n].row) nodes[n].row.remove();
      nodes[n].row = null;
      nodes[n].tick = null;
    });
    syncFoot();
    syncChips();
    if (emptyNote && !shown.length) emptyNote.hidden = false;
  }

  function clearAll() {
    clearTimers();
    shown.slice().forEach(function (n) {
      setDrawn(n, false);
      nodes[n].row = null;
      nodes[n].tick = null;
    });
    shown = [];
    rowBox.innerHTML = "";
    syncFoot();
    syncChips();
    if (emptyNote) emptyNote.hidden = false;
  }

  /* -- 4. the hint: what the bank really knows --------------------------- */

  function phrasingCount(query) {
    if (!catalogue) return null;
    var q = norm(query);
    if (q.length < 2) return null;
    var n = 0;
    for (var i = 0; i < catalogue.length; i++) {
      if (catalogue[i].indexOf(q) !== -1) n++;
    }
    return n;
  }

  function setHint(query, drawn) {
    if (!hint) return;
    if (!query.trim()) {
      hint.textContent = "";
      hint.removeAttribute("data-state");
      return;
    }

    // A multi-structure prompt is not a phrase to look up. Searching the bank for
    // the whole comma-joined string can never match, so the opening sequence — the
    // flagship prompt on the page — would announce "not bundled" about structures
    // that are all in the bank. Report what actually happened, and reserve the bank
    // lookup for a single term.
    if (query.indexOf(",") !== -1) {
      hint.textContent = drawn.length + " structure" + (drawn.length === 1 ? "" : "s") + " matched";
      hint.setAttribute("data-state", drawn.length ? "hit" : "zeroshot");
      return;
    }

    var n = phrasingCount(query);
    if (n === null) {
      hint.textContent = drawn.length ? drawn.length + " matched" : "";
      hint.removeAttribute("data-state");
      return;
    }
    if (n > 0) {
      hint.textContent = n.toLocaleString("en-US") + " phrasing" + (n === 1 ? "" : "s") + " in the bank";
      hint.setAttribute("data-state", "hit");
    } else {
      // Truthful: not bundled is not unsupported. The model generalises to related
      // unseen classes, and that is a claim the paper makes.
      hint.textContent = "not bundled — attempted zero-shot";
      hint.setAttribute("data-state", "zeroshot");
    }
  }

  /* -- 5. wiring --------------------------------------------------------- */

  function buildChips() {
    (data.chips || []).forEach(function (chip) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip";
      btn.setAttribute("aria-pressed", "false");

      var first = data.structures.filter(function (s) {
        return chip.targets.indexOf(s.name) !== -1;
      })[0];
      if (first) {
        var sw = document.createElement("span");
        sw.className = "chip__swatch";
        sw.style.color = "var(--vx-" + first.token + ")";
        btn.appendChild(sw);
      }
      btn.appendChild(document.createTextNode(chip.label));

      btn.addEventListener("click", function () {
        var on = chip.targets.every(function (t) { return shown.indexOf(t) !== -1; });
        if (on) {
          undraw(chip.targets);
          input.value = "";
          setHint("", []);
        } else {
          input.value = chip.label;
          draw(chip.targets);
          setHint(chip.label, chip.targets);
        }
      });

      chip.targets.forEach(function (t) {
        (chipFor[t] = chipFor[t] || []).push({ btn: btn, targets: chip.targets });
      });
      chipBox.appendChild(btn);
    });
  }

  var typeTimer = null;
  function onType() {
    window.clearTimeout(typeTimer);
    typeTimer = window.setTimeout(function () {
      var q = input.value;
      var names = resolve(q);
      draw(names);
      setHint(q, names);
    }, 220);
  }

  /* -- 6. the vocabulary search ------------------------------------------ */

  function renderVocab(query) {
    if (!vocabResults) return;
    if (!catalogue) {
      vocabResults.innerHTML = '<li class="vocab__none">Loading the catalogue…</li>';
      return;
    }
    var q = norm(query);
    vocabResults.innerHTML = "";

    if (!q) {
      vocabCount.textContent = catalogue.length.toLocaleString("en-US") + " phrasings";
      // Seed with a real sample rather than a placeholder sentence. An empty
      // 7rem panel under a claim about 14,194 phrasings reads as an unfinished
      // feature; a panel already full of the model's own vocabulary is the trust
      // moment this section exists for. Evenly spaced through the bank, which is
      // roughly grouped by anatomical system, so the sample spans systems — and
      // deterministic, so the page looks the same on every load.
      var frag = document.createDocumentFragment();
      var note = document.createElement("li");
      note.className = "vocab__none";
      note.textContent = "A sample of the bank — start typing to search all " +
        catalogue.length.toLocaleString("en-US") + ".";
      frag.appendChild(note);
      var step = Math.max(1, Math.floor(catalogue.length / SEED_COUNT));
      for (var s = 0; s < catalogue.length && frag.childNodes.length <= SEED_COUNT; s += step) {
        var seed = document.createElement("li");
        seed.textContent = catalogue[s];
        frag.appendChild(seed);
      }
      vocabResults.appendChild(frag);
      return;
    }

    var matches = [];
    for (var i = 0; i < catalogue.length && matches.length < 400; i++) {
      if (catalogue[i].indexOf(q) !== -1) matches.push(catalogue[i]);
    }
    var total = matches.length === 400 ? phrasingCount(query) : matches.length;
    vocabCount.textContent = total.toLocaleString("en-US") + " of " +
                             catalogue.length.toLocaleString("en-US");

    if (!matches.length) {
      vocabResults.innerHTML =
        '<li class="vocab__none">No bundled phrasing contains that. VoxTell still attempts ' +
        'unseen structures zero-shot — the bank is a warm cache, not a whitelist.</li>';
      return;
    }

    var frag2 = document.createDocumentFragment();
    matches.slice(0, 120).forEach(function (phrase) {
      var li = document.createElement("li");
      var at = phrase.indexOf(q);
      li.appendChild(document.createTextNode(phrase.slice(0, at)));
      var m = document.createElement("mark");
      m.textContent = phrase.slice(at, at + q.length);
      li.appendChild(m);
      li.appendChild(document.createTextNode(phrase.slice(at + q.length)));
      frag2.appendChild(li);
    });
    vocabResults.appendChild(frag2);
  }

  var vocabTimer = null;
  function onVocabType() {
    window.clearTimeout(vocabTimer);
    vocabTimer = window.setTimeout(function () { renderVocab(vocabInput.value); }, 160);
  }

  /* -- 7. boot ----------------------------------------------------------- */

  /* The one orchestrated moment on the page: six structures land in the sheet,
     one every 140ms, so the sheet is seen FILLING rather than found already full.
     Armed by an IntersectionObserver instead of a bare timeout — on a slow
     connection the old 700ms fired while the fonts were still swapping, i.e.
     before anybody could see it happen. Reduced motion still gets the result,
     it just arrives at once. */
  function armOpeningSequence() {
    var run = function () {
      input.value = OPENING_TEXT;
      draw(OPENING);
      setHint(OPENING_TEXT, OPENING);
    };

    if (reduceMotion || !("IntersectionObserver" in window)) { run(); return; }

    var once = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        once.unobserve(entry.target);
        run();
      });
    }, { threshold: 0.3 });
    once.observe(panel);
  }

  if (stage && input && rowBox) {
    fetch(asset("hero-contours.v3.json"))
      .then(function (r) { return r.json(); })
      .then(function (json) {
        if (json.schema !== SCHEMA) {
          throw new Error("hero contours schema " + json.schema + ", expected " + SCHEMA);
        }
        data = json;
        buildStage();
        buildChips();
        syncFoot();
        input.addEventListener("input", onType);
        input.addEventListener("keydown", function (e) {
          if (e.key === "Enter") { e.preventDefault(); onType(); }
          if (e.key === "Escape") { input.value = ""; clearAll(); setHint("", []); }
        });
        var reset = document.getElementById("demoReset");
        if (reset) {
          reset.hidden = false;
          reset.addEventListener("click", function () {
            input.value = "";
            clearAll();
            setHint("", []);
            input.focus();
          });
        }
        armOpeningSequence();
      })
      .catch(function () {
        if (emptyNote) emptyNote.textContent = "The demo could not load. The product is unaffected.";
      });
  }

  // Deferred and independent: the drawable demo must not wait on 359 KB.
  var loadCatalogue = function () {
    fetch(asset("prompts.v1.json"))
      .then(function (r) { return r.json(); })
      .then(function (json) {
        catalogue = json.prompts;
        if (vocabInput) renderVocab(vocabInput.value);
        if (input && input.value) setHint(input.value, resolve(input.value));
      })
      .catch(function () {
        if (vocabResults) {
          vocabResults.innerHTML =
            '<li class="vocab__none">The catalogue could not load. All 14,194 phrasings ' +
            "still ship with the model itself.</li>";
        }
      });
  };

  if (vocabInput) {
    vocabInput.addEventListener("input", onVocabType);
    renderVocab("");
  }

  if ("requestIdleCallback" in window) {
    window.requestIdleCallback(loadCatalogue, { timeout: 2500 });
  } else {
    window.setTimeout(loadCatalogue, 900);
  }
})();
