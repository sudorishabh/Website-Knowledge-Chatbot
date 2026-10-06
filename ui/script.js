"use strict";

(function () {
  const SCRIPT = document.currentScript;
  const cfg = (SCRIPT && SCRIPT.dataset) || {};

  // An https host page blocks http:// API calls as mixed content, so upgrade
  // the scheme rather than fail silently. localhost/loopback is exempt —
  // browsers treat it as a trustworthy origin, and dev servers are plain http.
  function normalizeApiBase(raw) {
    let base = (raw || "http://localhost:8000").replace(/\/+$/, "");
    if (
      window.location.protocol === "https:" &&
      /^http:\/\//i.test(base) &&
      !/^http:\/\/(localhost|127\.0\.0\.1|\[::1\])([:/]|$)/i.test(base)
    ) {
      base = "https://" + base.slice(7);
      console.warn(
        "[teri-rag] data-api-base upgraded to https to avoid mixed-content blocking:",
        base,
      );
    }
    return base;
  }
  const API_BASE = normalizeApiBase(cfg.apiBase);
  // data-title on the script tag overrides the header/launcher label. Always
  // rendered through escapeHtml, so a hostile host-page value stays inert.
  const TITLE = (cfg.title || "").trim() || "TERI AI SARTHI";
  const TOP_K = parseInt(cfg.topK || "", 10);
  const top_k = Number.isInteger(TOP_K) && TOP_K > 0 ? TOP_K : null;

  const ICON = {
    find: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    compare:
      '<rect x="5" y="5" width="5" height="14" rx="1"/><rect x="14" y="5" width="5" height="14" rx="1"/>',
    track: '<path d="M3 17l6-6 4 4 7-7"/><path d="M21 8v5h-5"/>',
    list: '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1.2"/><circle cx="4.5" cy="12" r="1.2"/><circle cx="4.5" cy="18" r="1.2"/>',
    analyze: '<path d="M5 21V11M12 21V4M19 21v-7"/>',
    suggest:
      '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-4 10c1 1 1 2 1 3h6c0-1 0-2 1-3a6 6 0 0 0-4-10z"/>',
  };
  const SUGGESTIONS = [
    { text: "Find India's renewable energy capacity targets", icon: ICON.find },
    { text: "Compare solar and wind energy adoption across states", icon: ICON.compare },
    { text: "Track progress on India's net-zero commitments", icon: ICON.track },
    { text: "List key recommendations on sustainable water management", icon: ICON.list },
    { text: "Analyze the main drivers of urban air pollution", icon: ICON.analyze },
    { text: "Suggest actions to improve industrial energy efficiency", icon: ICON.suggest },
  ];

  // The widget's typefaces, linked from the host document: Chrome ignores
  // @font-face declared inside a shadow root, but a family the document loads
  // is usable within it. data-fonts="off" skips the request (for a host with a
  // strict CSP); the font stacks then fall back to system faces.
  const FONT_CSS =
    "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600" +
    "&family=Source+Serif+4:opsz,wght@8..60,600&display=swap";
  const FONTS_ON = (cfg.fonts || "").trim().toLowerCase() !== "off";

  // Status words cycled while the bot is working, before the first token lands.
  const LOADER_PHASES = [
    "Thinking",
    "Reading relevant sources",
    "Generating your answer",
  ];

  // The widget's mark, in the header and beside every AI reply: a sun rising
  // over the horizon, for an institute working on energy. The sun takes the
  // saffron accent and the ground lines take currentColor (see .sun__* rules).
  const SUN_MARK =
    '<svg class="sun" viewBox="0 0 24 24" fill="none" stroke-width="1.8" ' +
    'stroke-linecap="round" aria-hidden="true">' +
    '<path class="sun__rays" d="M12 6.5v2M5.3 9.3l1.4 1.4M18.7 9.3l-1.4 1.4"/>' +
    '<path class="sun__disc" d="M6.5 16a5.5 5.5 0 0 1 11 0z"/>' +
    '<path class="sun__ground" d="M3.5 16h17M7.5 19.5h9"/>' +
    "</svg>";

  // The copy action under a settled answer, and the tick it swaps to.
  const COPY_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="9" y="9" width="11" height="11" rx="2"/>' +
    '<path d="M5 15V6a2 2 0 0 1 2-2h8"/>' +
    "</svg>";
  const CHECK_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M5 12.5l4.5 4.5L19 7.5"/>' +
    "</svg>";

  // Caption mark for the PDF answer block. Outline and fold only — interior
  // rules muddy at the 13px the caption renders it.
  const DOC_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>' +
    '<path d="M14 3v5h5"/>' +
    "</svg>";

  // Hover-card marks: a globe for a web page (a PDF takes DOC_ICON), and an
  // arrow saying the chip opens in a new tab.
  const WEB_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="9"/>' +
    '<path d="M3 12h18"/>' +
    '<path d="M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18z"/>' +
    "</svg>";
  const OPEN_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M7 17 17 7"/>' +
    '<path d="M8 7h9v9"/>' +
    "</svg>";

  // Guard against double-injection.
  if (document.getElementById("teri-rag-widget")) return;

  const history = [];
  let streaming = false;
  let isOpen = false;
  let loaderTimer = null;
  // In-flight request state: aborting the fetch on "New chat" stops the
  // server-side generation, and the epoch guard keeps a stream that raced the
  // reset from leaking its turn into the fresh conversation's history.
  let currentAbort = null;
  let chatEpoch = 0;

  let host, root, el;

  function openPanel() {
    isOpen = true;
    host.classList.add("open");
    el.launcher.setAttribute("aria-expanded", "true");
    autoGrow();
    el.input.focus();
  }
  function closePanel() {
    isOpen = false;
    host.classList.remove("open");
    el.launcher.setAttribute("aria-expanded", "false");
    hideCitePop(); // Escape can close the panel with a chip still hovered
  }
  function toggleExpand() {
    const expanded = host.classList.toggle("expanded");
    const label = expanded ? "Shrink" : "Expand";
    el.expand.title = label;
    el.expand.setAttribute("aria-label", label);
    el.input.focus();
  }

  // Reset to a fresh conversation: cancel any in-flight request, drop history,
  // clear messages, show welcome.
  function resetChat() {
    chatEpoch++;
    if (currentAbort) currentAbort.abort();
    setStreaming(false);
    stopLoader();
    history.length = 0;
    el.messages.querySelectorAll(".msg").forEach((n) => n.remove());
    if (el.welcome) el.welcome.hidden = false;
    el.input.value = "";
    autoGrow();
    el.input.focus();
  }

  function renderSuggestions() {
    el.suggestions.innerHTML = "";
    for (const s of SUGGESTIONS) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "suggestion";
      item.innerHTML =
        '<span class="suggestion__icon">' +
        '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        s.icon +
        "</svg></span>" +
        '<span class="suggestion__text">' +
        escapeHtml(s.text) +
        "</span>";
      item.addEventListener("click", () => {
        el.input.value = s.text;
        handleSend();
      });
      el.suggestions.appendChild(item);
    }
  }

  function hideWelcome() {
    if (el.welcome && !el.welcome.hidden) el.welcome.hidden = true;
  }
  /* ---------------------------------------------------------------- *
   * Messages
   * ---------------------------------------------------------------- */
  // A bot reply is the avatar beside a body column: the answer bubble, then the
  // actions added once it settles. A user message is the bubble alone.
  function addMessage(role, text) {
    hideWelcome();
    const wrap = document.createElement("div");
    wrap.className = "msg msg--" + role;
    let body = wrap;
    if (role === "bot") {
      const avatar = document.createElement("div");
      avatar.className = "msg__avatar";
      avatar.setAttribute("aria-hidden", "true");
      avatar.innerHTML = SUN_MARK;
      body = document.createElement("div");
      body.className = "msg__body";
      wrap.append(avatar, body);
    }
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    body.appendChild(bubble);
    el.messages.appendChild(wrap);
    scrollToBottom();
    return { wrap, body, bubble };
  }

  // Actions under a settled answer. Copy takes the answer's markdown with the
  // citation markers dropped: the chips they became don't survive a paste.
  // The clipboard API needs a secure context, so plain-http hosts get no bar.
  function addAnswerActions(body, answer) {
    if (!navigator.clipboard) return;
    const bar = document.createElement("div");
    bar.className = "msg__actions";
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "action";
    const idle = COPY_ICON + "<span>Copy</span>";
    copy.innerHTML = idle;
    let resetTimer = 0;
    copy.addEventListener("click", () => {
      navigator.clipboard
        .writeText(stripMarkers(cleanBlockText(answer)))
        .then(() => {
          copy.innerHTML = CHECK_ICON + "<span>Copied</span>";
          copy.classList.add("is-done");
          clearTimeout(resetTimer);
          resetTimer = setTimeout(() => {
            copy.innerHTML = idle;
            copy.classList.remove("is-done");
          }, 1600);
        })
        .catch(() => {});
    });
    bar.appendChild(copy);
    body.appendChild(bar);
  }

  function scrollToBottom() {
    el.messages.scrollTop = el.messages.scrollHeight;
  }

  const INPUT_MAX = 120;
  function autoGrow() {
    el.input.style.height = "auto";
    const needed = el.input.scrollHeight;
    el.input.style.height = Math.min(needed, INPUT_MAX) + "px";
    // Only show a scrollbar once the box can't grow any further.
    el.input.style.overflowY = needed > INPUT_MAX ? "auto" : "hidden";
  }

  function setStreaming(on) {
    streaming = on;
    el.send.disabled = on;
    el.send.classList.toggle("busy", on);
  }

  /* Animated "working" indicator: a shimmering status word that steps through
     LOADER_PHASES, plus three bouncing dots. Runs until the first token arrives
     (see streamChat) or the request settles. */
  function startLoader(bubble) {
    bubble.classList.add("bubble--pending");
    bubble.innerHTML =
      '<span class="loader">' +
      '<span class="loader__text" role="status"></span>' +
      '<span class="loader__dots" aria-hidden="true"><i></i><i></i><i></i></span>' +
      "</span>";

    const textEl = bubble.querySelector(".loader__text");
    let idx = -1;
    const advance = () => {
      idx = Math.min(idx + 1, LOADER_PHASES.length - 1);
      const word = document.createElement("span");
      word.className = "loader__word";
      word.textContent = LOADER_PHASES[idx];
      textEl.textContent = "";
      textEl.appendChild(word);
      if (idx >= LOADER_PHASES.length - 1) stopLoader();
    };
    advance();
    loaderTimer = setInterval(advance, 3200);
  }

  function stopLoader() {
    if (loaderTimer) {
      clearInterval(loaderTimer);
      loaderTimer = null;
    }
  }

  async function handleSend() {
    if (streaming) return;
    const text = el.input.value.trim();
    if (!text) return;

    addMessage("user", text);
    el.input.value = "";
    autoGrow();

    setStreaming(true);
    const { body, bubble } = addMessage("bot", "");
    startLoader(bubble);

    const epoch = chatEpoch;
    const ctrl = new AbortController();
    currentAbort = ctrl;
    try {
      const { answer, sources } = await streamChat(text, bubble, ctrl.signal);
      stopLoader();
      if (epoch !== chatEpoch) return; // conversation was reset mid-flight
      bubble.classList.remove("bubble--pending");
      if (answer) {
        bubble.innerHTML = renderAnswer(answer);
        linkCitations(bubble, (sources && sources.citations) || []);
      } else bubble.textContent = "(no response)";
      if (sources) renderNumericWarning(bubble, sources);
      if (answer) addAnswerActions(body, answer);
      history.push({ role: "user", content: text });
      history.push({ role: "assistant", content: answer });
    } catch (err) {
      stopLoader();
      bubble.classList.remove("bubble--pending");
      // Cancelled by "New chat": the bubble is already gone — stay silent.
      if ((err && err.name === "AbortError") || epoch !== chatEpoch) return;
      // The alert icon is drawn by .bubble--error::before.
      bubble.classList.add("bubble--error");
      bubble.textContent =
        err && err.message ? err.message : "The request failed. Please try again.";
    } finally {
      if (currentAbort === ctrl) currentAbort = null;
      if (epoch === chatEpoch) setStreaming(false);
      scrollToBottom();
    }
  }

  async function streamChat(question, bubble, signal) {
    const body = { question, history };
    if (top_k) body.top_k = top_k;

    let res;
    try {
      res = await fetch(API_BASE + "/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
      });
    } catch (err) {
      if (err && err.name === "AbortError") throw err; // "New chat": stays silent
      throw new Error("Couldn't reach the assistant. Check your connection and try again.");
    }
    if (!res.ok || !res.body)
      throw new Error(
        "The assistant is unavailable right now (HTTP " + res.status + "). Please try again shortly.",
      );

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let answer = "";
    let sources = null;

    // Live render sink. Tokens append their DELTA to one persistent text node,
    // batched to at most one DOM write + scroll per animation frame — rewriting
    // the full accumulated answer per token (the old behaviour) costs O(n^2)
    // characters and forces a layout per token on long answers. Block tags are
    // filtered out of the live text only; `answer` keeps the raw stream, which
    // is what gets parsed into blocks, cached, and pushed onto the history.
    let textNode = null;
    let pending = "";
    let raf = 0;
    const filterTags = createTagFilter();
    const filterMarkers = createMarkerFilter();
    const flush = () => {
      raf = 0;
      if (pending && textNode) {
        textNode.appendData(pending);
        pending = "";
        scrollToBottom();
      }
    };

    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let idx;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const raw = buffer.slice(0, idx).trim();
          buffer = buffer.slice(idx + 2);
          if (!raw.startsWith("data:")) continue;
          const payload = raw.slice(5).trim();
          if (!payload) continue;

          let event;
          try {
            event = JSON.parse(payload);
          } catch {
            continue;
          }

          if (event.type === "token") {
            answer += event.text;
            // An opening tag or a marker carries no visible text: keep the
            // loader up until real prose lands rather than flashing an empty
            // bubble.
            const visible = filterMarkers(filterTags(event.text));
            if (!visible) continue;
            if (!textNode) {
              stopLoader();
              bubble.classList.remove("bubble--pending");
              bubble.textContent = "";
              textNode = document.createTextNode("");
              bubble.appendChild(textNode);
            }
            pending += visible;
            if (!raf) raf = requestAnimationFrame(flush);
          } else if (event.type === "correction") {
            // A post-hoc rewrite replaces the streamed draft wholesale (see the
            // event contract in app/api/chat.py). Without this branch the
            // correction was parsed and dropped, so a reader kept the text the
            // server had already rejected — which silently disabled both the
            // faithfulness loop and the publication-date guard.
            answer = event.text;
            pending = "";
            if (raf) {
              cancelAnimationFrame(raf);
              raf = 0;
            }
            // Whole text, so no marker can be part-way through.
            const visible = stripMarkers(filterTags(event.text));
            stopLoader();
            bubble.classList.remove("bubble--pending");
            bubble.textContent = "";
            textNode = document.createTextNode(visible);
            bubble.appendChild(textNode);
            scrollToBottom();
          } else if (event.type === "sources") {
            sources = event;
          } else if (event.type === "done") {
            return { answer, sources };
          } else if (event.type === "error") {
            // Server-signalled mid-stream failure.
            throw new Error("The answer was interrupted. Please try again.");
          }
        }
      }
      // Every complete answer ends with a "done" event; a stream that just
      // stops was truncated (server crash / dropped connection). Surface it
      // instead of presenting the partial answer as complete.
      throw new Error("The connection was interrupted. Please try again.");
    } finally {
      // Every exit (done, truncation, abort/error) supersedes the live text —
      // the caller re-renders the full answer — so drop any queued flush
      // before it writes into a replaced/detached node, and release the
      // connection (the "done" return path otherwise leaves it open).
      if (raf) cancelAnimationFrame(raf);
      reader.cancel().catch(() => {});
    }
  }

  // Escapes for both element text and double-quoted attribute values. renderMarkdown
  // escapes the whole source once through here before any inline HTML is built, so
  // quotes must be escaped too — otherwise a quote inside a markdown link URL breaks
  // out of the href attribute and injects handlers (DOM XSS).
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderInline(text) {
    let out = text.replace(/`([^`]+)`/g, (_, c) => "<code>" + c + "</code>");
    out = out.replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, label, url) =>
        `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`,
    );
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
    return out;
  }

  /* GitHub-style tables: a header row, a |---|---| separator, then body rows. */
  function isTableSeparator(line) {
    return /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/.test(line);
  }
  function isTableStart(line, next) {
    return (
      line != null &&
      line.indexOf("|") !== -1 &&
      next != null &&
      isTableSeparator(next)
    );
  }
  function splitTableRow(line) {
    return line
      .trim()
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((c) => c.trim());
  }
  function tableAligns(sep) {
    return splitTableRow(sep).map((c) => {
      const l = c.startsWith(":");
      const r = c.endsWith(":");
      return l && r ? "center" : r ? "right" : l ? "left" : "";
    });
  }
  function renderTable(header, aligns, rows) {
    const at = (idx) =>
      aligns[idx] ? ' style="text-align:' + aligns[idx] + '"' : "";
    let out = '<div class="table-wrap"><table><thead><tr>';
    header.forEach((c, idx) => {
      out += "<th" + at(idx) + ">" + renderInline(c) + "</th>";
    });
    out += "</tr></thead><tbody>";
    for (const row of rows) {
      out += "<tr>";
      for (let idx = 0; idx < header.length; idx++) {
        out +=
          "<td" +
          at(idx) +
          ">" +
          renderInline(row[idx] != null ? row[idx] : "") +
          "</td>";
      }
      out += "</tr>";
    }
    return out + "</tbody></table></div>";
  }

  function renderMarkdown(src) {
    const lines = escapeHtml(src).split("\n");
    const html = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^```/.test(line)) {
        const code = [];
        i++;
        while (i < lines.length && !/^```\s*$/.test(lines[i]))
          code.push(lines[i++]);
        i++;
        html.push("<pre><code>" + code.join("\n") + "</code></pre>");
        continue;
      }
      if (isTableStart(line, lines[i + 1])) {
        const header = splitTableRow(line);
        const aligns = tableAligns(lines[i + 1]);
        i += 2;
        const rows = [];
        while (
          i < lines.length &&
          lines[i].indexOf("|") !== -1 &&
          !/^\s*$/.test(lines[i])
        ) {
          rows.push(splitTableRow(lines[i]));
          i++;
        }
        html.push(renderTable(header, aligns, rows));
        continue;
      }
      if (/^\s*[-*]\s+/.test(line)) {
        // Indentation sets depth: a deeper item opens a nested <ul> inside the
        // still-open parent <li>. Stripping the leading whitespace and emitting
        // one flat <ul> (as this used to) turns a sub-list into siblings of its
        // parent, so any nested answer rendered flat.
        const out = [];
        const depths = [];
        let openItem = false;
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
          const indent = (lines[i].match(/^[ \t]*/) || [""])[0]
            .replace(/\t/g, "    ").length;
          const content = renderInline(lines[i].replace(/^\s*[-*]\s+/, ""));
          if (!depths.length || indent > depths[depths.length - 1]) {
            depths.push(indent);
            out.push("<ul>");
          } else {
            if (openItem) {
              out.push("</li>");
              openItem = false;
            }
            while (depths.length > 1 && indent < depths[depths.length - 1]) {
              depths.pop();
              out.push("</ul></li>");
            }
          }
          out.push("<li>" + content);
          openItem = true;
          i++;
        }
        if (openItem) out.push("</li>");
        while (depths.length > 1) {
          depths.pop();
          out.push("</ul></li>");
        }
        if (depths.length) out.push("</ul>");
        html.push(out.join(""));
        continue;
      }
      if (/^\s*\d+\.\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          items.push(
            "<li>" +
              renderInline(lines[i].replace(/^\s*\d+\.\s+/, "")) +
              "</li>",
          );
          i++;
        }
        html.push("<ol>" + items.join("") + "</ol>");
        continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        const lvl = heading[1].length;
        html.push(
          "<h" + lvl + ">" + renderInline(heading[2]) + "</h" + lvl + ">",
        );
        i++;
        continue;
      }
      if (/^\s*$/.test(line)) {
        i++;
        continue;
      }
      const para = [];
      while (
        i < lines.length &&
        !/^\s*$/.test(lines[i]) &&
        !/^```/.test(lines[i]) &&
        !/^\s*[-*]\s+/.test(lines[i]) &&
        !/^\s*\d+\.\s+/.test(lines[i]) &&
        !/^#{1,6}\s+/.test(lines[i]) &&
        !isTableStart(lines[i], lines[i + 1])
      ) {
        para.push(lines[i++]);
      }
      html.push("<p>" + renderInline(para.join("<br>")) + "</p>");
    }
    return html.join("");
  }

  /* ---------------------------------------------------------------- *
   * Answer blocks
   *
   * A reader for the retired two-block contract, kept for one reason.
   *
   * Answers used to arrive wrapped in <website_answer> / <pdf_answer>
   * tags, website leading and the PDF block rendered below as a
   * captioned "From our documents" aside. Nothing asks a model for that
   * shape any more: website and PDF evidence is ranked together and
   * answered once, so a live answer arrives untagged and falls straight
   * through to the plain-text branch.
   *
   * What still arrives wrapped is an answer served from the semantic
   * cache that was generated before the change. Without this parser it
   * would render its tags as literal text, so the tolerance stays until
   * the cache has turned over. The rules mirror
   * app/generation/sections.py — keep the two in step, and retire them
   * together.
   * ---------------------------------------------------------------- */
  const WEBSITE_TAG = "website_answer";
  const PDF_TAG = "pdf_answer";
  const TAG_ALT = WEBSITE_TAG + "|" + PDF_TAG;
  // Mirrors PDF_LABEL / PDF_LEAD in app/generation/prompts.py. The model emits
  // the lead as bold body text; the panel promotes it to a real caption, so the
  // markdown copy is stripped to avoid captioning the block twice. A trailing
  // colon lands inside or outside the bold depending on the model, so both are
  // matched.
  const PDF_LABEL = "From our documents";
  const PDF_LEAD_RE = new RegExp(
    "^\\s*\\*\\*\\s*" + PDF_LABEL + "\\s*:?\\s*\\*\\*\\s*:?\\s*(?:\\r?\\n|$)",
    "i",
  );
  // Mirrors REFUSAL in app/generation/prompts.py. Compared after normalizing
  // away the surface variation a model adds — emphasis, quotes, a smart
  // apostrophe, a dropped full stop — and by equality, never substring: an
  // answer that merely says what it could not find still carries content.
  const REFUSAL = "I don't have information on that in the available sources.";
  function normalizeText(text) {
    return text
      .replace(/[‘’]/g, "'")
      .replace(/[“”]/g, '"')
      .replace(/\s+/g, " ")
      .trim()
      .replace(/^[*_"'\s]+|[*_"'\s]+$/g, "")
      .replace(/\.+$/, "")
      .trim()
      .toLowerCase();
  }
  const REFUSAL_NORM = normalizeText(REFUSAL);
  // The block body without the caption line a PDF block opens with. The caption
  // is presentation: renderPdfBlock emits its own when the block keeps its
  // container, and nothing should carry it once the block loses one.
  function withoutLead(text) {
    return text.replace(PDF_LEAD_RE, "").trim();
  }
  // The PDF lead is a caption rather than content, so a block holding the
  // caption and then the refusal is still only a refusal.
  function isRefusal(text) {
    return normalizeText(withoutLead(text)) === REFUSAL_NORM;
  }
  // Longest tag we can be part-way through: "</website_answer >".
  const MAX_TAG_LEN = WEBSITE_TAG.length + 4;
  // Built per call — a shared global regex carries lastIndex between calls.
  const blockRe = () =>
    new RegExp("<(" + TAG_ALT + ")\\s*>([\\s\\S]*?)(?:</\\1\\s*>|$)", "gi");
  const anyTagRe = () => new RegExp("</?(?:" + TAG_ALT + ")\\s*>", "gi");

  function cleanBlockText(text) {
    return text
      .replace(anyTagRe(), "")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  // Parse an answer into the sections to render, in display order. Tolerant by
  // design: the tags come from a model and a stream can be cut mid-tag, so a
  // missing or malformed wrapper degrades to plain text instead of losing the
  // answer. Website always precedes PDF whatever order they arrived in; blocks
  // of one kind merge; untagged text keeps its position around the blocks; a
  // block holding nothing but the refusal drops out beside real content; a PDF
  // block with no website block beside it is demoted to plain prose.
  function splitSections(answer) {
    const leading = [];
    const trailing = [];
    const grouped = { website: [], pdf: [] };
    const re = blockRe();
    let cursor = 0;
    let seenBlock = false;
    let match;
    while ((match = re.exec(answer)) !== null) {
      (seenBlock ? trailing : leading).push(answer.slice(cursor, match.index));
      const kind = match[1].toLowerCase() === WEBSITE_TAG ? "website" : "pdf";
      grouped[kind].push(match[2]);
      cursor = re.lastIndex;
      seenBlock = true;
    }
    (seenBlock ? trailing : leading).push(answer.slice(cursor));

    let leadingText = cleanBlockText(leading.join("\n\n"));
    let trailingText = cleanBlockText(trailing.join("\n\n"));
    let websiteText = cleanBlockText(grouped.website.join("\n\n"));
    let pdfText = cleanBlockText(grouped.pdf.join("\n\n"));

    const parts = [leadingText, websiteText, pdfText, trailingText];
    if (parts.some((t) => t && !isRefusal(t))) {
      // Something real was found, so every refusal beside it is a block the
      // model filled rather than dropped. Left in, it contradicts the content
      // next to it and counts as a website answer the PDF block must defer to.
      const kept = parts.map((t) => (isRefusal(t) ? "" : t));
      leadingText = kept[0];
      websiteText = kept[1];
      pdfText = kept[2];
      trailingText = kept[3];
    } else {
      // Refusals and blanks only: the refusal is the whole answer, said once
      // and unwrapped, whichever block the model happened to put it in.
      const refused = withoutLead(parts.find(Boolean) || "");
      return refused ? [{ kind: "plain", text: refused }] : [];
    }

    let pdfKind = "pdf";
    if (!websiteText) {
      // Standing alone, the PDF block is the answer: demote it, and drop the
      // lead-in that only read as a label under the caption it no longer gets.
      pdfKind = "plain";
      pdfText = withoutLead(pdfText);
    }

    const sections = [];
    [
      ["plain", leadingText],
      ["website", websiteText],
      [pdfKind, pdfText],
      ["plain", trailingText],
    ].forEach(function (entry) {
      if (entry[1]) sections.push({ kind: entry[0], text: entry[1] });
    });
    return sections;
  }

  // The supplementary-documents panel: a captioned card. The caption is emitted
  // by us rather than left as the model's bold first line, so it is always
  // present and always typeset the same way even when the model forgets the lead.
  // Only reached alongside a website block — splitSections demotes a lone PDF
  // block, so the card never wraps an entire answer.
  function renderPdfBlock(text) {
    return (
      '<div class="answer-block answer-block--pdf">' +
      '<div class="answer-block__label">' +
      DOC_ICON +
      "<span>" +
      PDF_LABEL +
      "</span></div>" +
      renderMarkdown(withoutLead(text)) +
      "</div>"
    );
  }

  // The answer body. Website content is the answer proper and reads as plain
  // prose; only the PDF block is set apart. An untagged answer (a refusal,
  // chit-chat, a scoped summary) renders bare, exactly as it did before the
  // blocks existed. Class names come from our own constants, never from model
  // text, so they are safe to interpolate.
  function renderAnswer(answer) {
    const sections = splitSections(answer);
    if (!sections.length) return "";
    return sections
      .map(function (section) {
        if (section.kind === "pdf") return renderPdfBlock(section.text);
        const body = renderMarkdown(section.text);
        if (section.kind === "plain") return body;
        return '<div class="answer-block answer-block--website">' + body + "</div>";
      })
      .join("");
  }

  // Could this held text still grow into a block tag? Anything longer than a
  // tag, or already carrying a character no tag contains, cannot.
  function couldBecomeTag(held) {
    return held.length <= MAX_TAG_LEN && /^<\/?[a-z_]*\s*$/i.test(held);
  }

  // Live-stream tag suppressor: the wrappers must not flash on screen as they
  // stream. A tag can be split across any number of tokens ("<web" +
  // "site_answer>"), so hold back a trailing fragment that could still become
  // one and release it once it cannot. Text held when the stream ends is not
  // lost — the caller re-renders the whole answer from the raw text.
  function createTagFilter() {
    let held = "";
    return function (chunk) {
      held += chunk;
      let out = "";
      for (;;) {
        const lt = held.indexOf("<");
        if (lt === -1) {
          out += held;
          held = "";
          break;
        }
        out += held.slice(0, lt);
        held = held.slice(lt);
        const whole = anyTagRe().exec(held);
        if (whole && whole.index === 0) {
          held = held.slice(whole[0].length); // a complete tag: drop it
          continue;
        }
        if (couldBecomeTag(held)) break; // wait for the rest of the tag
        out += "<"; // a literal "<" in the prose
        held = held.slice(1);
      }
      return out;
    };
  }

  /* ---------------------------------------------------------------- *
   * Citations / sources
   *
   * The answer cites its evidence with [n] markers (`_MARKER` in
   * app/generation/faithfulness.py). Once the answer settles, each run of
   * markers becomes a chip naming the site it links to, so the source sits
   * beside the claim it supports. A citation a reader cannot open — the
   * knowledge graph, a catalog lookup — gets no chip: its marker is dropped
   * rather than left as a number pointing nowhere.
   * ---------------------------------------------------------------- */
  // A run of markers with the whitespace before it ("chains [1][2]."), so a
  // run that yields no chip leaves "chains." behind.
  const MARKER_RUN_RE = /\s*\[\d+\](?:\s*\[\d+\])*/g;
  const MARKER_RE = /\[(\d+)\]/g;
  // Punctuation closing the claim moves ahead of its chips — "chains. teriin"
  // — so a chip never splits a sentence from its full stop. Commas stay put.
  const CLOSING_PUNCT_RE = /^[.;:!?]+/;
  // Second-level labels that name a registry, not a site: "mnre.gov.in".
  const GENERIC_SLD = new Set(["ac", "co", "com", "edu", "gov", "net", "nic", "org", "res"]);

  function linkCitations(container, citations) {
    const byNumber = new Map(citations.map((c) => [c.n, c]));
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      // Code shows its text verbatim, and a link cannot hold another link.
      if (node.parentElement.closest("pre, code, a")) continue;
      linkMarkersIn(node, byNumber);
    }
  }

  function linkMarkersIn(node, byNumber) {
    const text = node.data;
    const frag = document.createDocumentFragment();
    let cursor = 0;
    for (const run of text.matchAll(MARKER_RUN_RE)) {
      const end = run.index + run[0].length;
      const punct = (text.slice(end).match(CLOSING_PUNCT_RE) || [""])[0];
      frag.append(text.slice(cursor, run.index) + punct);
      for (const source of runSources(run[0], byNumber))
        frag.append(citationChip(source));
      cursor = end + punct.length;
    }
    if (!cursor) return; // no markers in this node
    frag.append(text.slice(cursor));
    node.replaceWith(frag);
  }

  // The openable sources a marker run cites, one per distinct link: two
  // passages of the same page are one source to a reader.
  function runSources(run, byNumber) {
    const seen = new Set();
    const sources = [];
    for (const m of run.matchAll(MARKER_RE)) {
      const citation = byNumber.get(Number(m[1]));
      const href = citation && resolveUrl(citation.url);
      if (!href || seen.has(href)) continue;
      seen.add(href);
      sources.push({ citation, href });
    }
    return sources;
  }

  function citationChip({ citation, href }) {
    const chip = document.createElement("a");
    chip.className = "cite";
    chip.href = href;
    chip.target = "_blank";
    chip.rel = "noopener noreferrer";
    chip.textContent = siteName(href);
    // What the hover card shows (showCitePop). The card is visual only, so the
    // label spells the same source out for a screen reader.
    const detail = [];
    if (citation.page != null) detail.push("Page " + citation.page);
    if (citation.section) detail.push(citation.section);
    chip.dataset.host = hostLabel(href);
    chip.dataset.title = citation.title || "";
    chip.dataset.detail = detail.join(" · ");
    chip.dataset.kind = citation.type === "website" ? "page" : "document";
    chip.setAttribute(
      "aria-label",
      [chip.textContent, citation.title, chip.dataset.detail]
        .filter(Boolean)
        .join(", "),
    );
    return chip;
  }

  // The hover card for a citation chip: the site, the source's title, and
  // where in it the claim sits. One shared card, fixed-positioned so neither
  // the scrolling message list nor a table's overflow clips it.
  let popChip = null;
  function showCitePop(chip) {
    if (chip === popChip) return;
    popChip = chip;
    const { host, title, detail, kind } = chip.dataset;
    const pop = el.citePop;
    pop.textContent = "";

    const site = document.createElement("div");
    site.className = "cite-pop__site";
    // Icons are our own constants, never model text.
    site.innerHTML =
      '<span class="cite-pop__icon">' +
      (kind === "page" ? WEB_ICON : DOC_ICON) +
      '</span><span class="cite-pop__host"></span>' +
      '<span class="cite-pop__open">' +
      OPEN_ICON +
      "</span>";
    site.querySelector(".cite-pop__host").textContent = host;
    pop.appendChild(site);
    if (title) pop.appendChild(popLine("cite-pop__title", title));
    if (detail) pop.appendChild(popLine("cite-pop__meta", detail));

    placeCitePop(chip);
    pop.classList.add("is-open");
  }

  function popLine(className, text) {
    const line = document.createElement("div");
    line.className = className;
    line.textContent = text;
    return line;
  }

  function hideCitePop() {
    popChip = null;
    el.citePop.classList.remove("is-open");
  }

  // Above the chip, or below it when there is no room above; centred on it.
  // Bounded by the message list, so the card never covers the header. The
  // caret keeps pointing at the chip when an edge pushes the card aside.
  const POP_GAP = 10;
  const CARET_INSET = 16; // keeps the caret clear of the rounded corners
  function placeCitePop(chip) {
    const pop = el.citePop;
    const r = chip.getBoundingClientRect();
    const bounds = el.messages.getBoundingClientRect();
    const w = pop.offsetWidth;
    const h = pop.offsetHeight;
    const below = r.top - h - POP_GAP < bounds.top + POP_GAP;
    const center = r.left + r.width / 2;
    const left = Math.max(
      bounds.left + POP_GAP,
      Math.min(center - w / 2, bounds.right - w - POP_GAP),
    );
    const caret = Math.max(CARET_INSET, Math.min(center - left, w - CARET_INSET));
    pop.style.top = (below ? r.bottom + POP_GAP : r.top - h - POP_GAP) + "px";
    pop.style.left = left + "px";
    pop.style.setProperty("--caret-x", caret + "px");
    pop.dataset.placement = below ? "below" : "above";
  }

  // The site a link belongs to, as a reader would name it: "www.teriin.org"
  // → "teriin", "mnre.gov.in" → "mnre".
  function siteName(href) {
    const labels = hostLabel(href).split(".");
    if (labels.length > 1) labels.pop();
    if (labels.length > 1 && GENERIC_SLD.has(labels[labels.length - 1]))
      labels.pop();
    return labels[labels.length - 1] || "source";
  }

  function stripMarkers(text) {
    return text.replace(MARKER_RUN_RE, "");
  }

  // Live-stream marker suppressor: markers become chips only once the answer
  // settles, so they must not flash on screen as bare numbers first. A run can
  // be split across tokens ("[1" + "][2]"), and the whitespace before it goes
  // with it, so a trailing fragment that could still become one is held back
  // until it cannot. Text held when the stream ends is not lost — the caller
  // re-renders the whole answer from the raw text.
  const MARKER_TAIL_RE = /\s*(?:\[\d*)?$/;
  function createMarkerFilter() {
    let held = "";
    return function (chunk) {
      const text = stripMarkers(held + chunk);
      held = text.match(MARKER_TAIL_RE)[0];
      return text.slice(0, text.length - held.length);
    };
  }

  // The deterministic numeric check flagged a figure the cited sources don't
  // support: warn the reader without altering the answer.
  function renderNumericWarning(bubble, sources) {
    if (!sources.numeric_mismatch) return;
    const warn = document.createElement("div");
    warn.className = "answer-warn"; // icon drawn by .answer-warn::before
    warn.textContent =
      "Some figures in this answer could not be verified against the cited sources.";
    bubble.appendChild(warn);
  }

  // Citation links are absolute today: a web page cites its own URL and a PDF
  // cites the attachment URL it was downloaded from. The root-relative branch
  // stays as a generic resolver in case the backend ever emits one. Anything
  // else — including a citation with no URL at all — resolves to "", and gets
  // no chip rather than a dead or hostile link.
  function resolveUrl(url) {
    if (!url) return "";
    // Absolute http(s) or protocol-relative — safe to open as-is.
    if (/^https?:\/\//i.test(url) || url.slice(0, 2) === "//") return url;
    // Root-relative backend links resolve against the API origin.
    if (url.charAt(0) === "/") return API_BASE + url;
    // Reject anything else (javascript:, data:, mailto:, bare relative) so a
    // hostile citation URL gets no chip instead of a live link.
    return "";
  }

  // Short, human-friendly host (no "www.") for a citation URL: what a chip's
  // site name is cut from, and its tooltip when the source has no title.
  function hostLabel(url) {
    if (!url) return "";
    try {
      return new URL(url, API_BASE).hostname.replace(/^www\./, "");
    } catch {
      return "";
    }
  }

  /* ---------------------------------------------------------------- *
   * Boot — build the Shadow DOM and wire events. Runs once <body> exists,
   * so the tag works whether placed in <head>, footer, or before </body>.
   * ---------------------------------------------------------------- */
  function loadFonts() {
    if (!FONTS_ON || document.getElementById("teri-rag-fonts")) return;
    const link = document.createElement("link");
    link.id = "teri-rag-fonts";
    link.rel = "stylesheet";
    link.href = FONT_CSS;
    document.head.appendChild(link);
  }

  function boot() {
    loadFonts();
    host = document.createElement("div");
    host.id = "teri-rag-widget";
    root = host.attachShadow({ mode: "open" });
    document.body.appendChild(host);
    root.innerHTML = STYLES() + MARKUP();

    const $ = (sel) => root.querySelector(sel);
    el = {
      launcher: $("#launcher"),
      panel: $("#panel"),
      newChat: $("#new-chat"),
      close: $("#close"),
      expand: $("#expand"),
      messages: $("#messages"),
      welcome: $("#welcome"),
      suggestions: $("#suggestions"),
      input: $("#input"),
      send: $("#send"),
      citePop: $("#cite-pop"),
    };

    el.launcher.addEventListener("click", () =>
      isOpen ? closePanel() : openPanel(),
    );
    el.newChat.addEventListener("click", resetChat);
    el.close.addEventListener("click", closePanel);
    el.expand.addEventListener("click", toggleExpand);
    el.send.addEventListener("click", handleSend);
    el.input.addEventListener("input", autoGrow);
    el.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && isOpen) closePanel();
    });

    // Citation hover cards, delegated: chips arrive with every answer. Focus
    // opens one too, so a keyboard reader sees the same card.
    const chipOf = (e) => e.target.closest && e.target.closest(".cite");
    const showFor = (e) => {
      const chip = chipOf(e);
      if (chip) showCitePop(chip);
    };
    const hideFor = (e) => {
      const chip = chipOf(e);
      if (chip && !chip.contains(e.relatedTarget)) hideCitePop();
    };
    el.messages.addEventListener("mouseover", showFor);
    el.messages.addEventListener("mouseout", hideFor);
    el.messages.addEventListener("focusin", showFor);
    el.messages.addEventListener("focusout", hideFor);
    // The card is placed once, so a scroll would leave it behind.
    el.messages.addEventListener("scroll", hideCitePop, { passive: true });

    renderSuggestions();
    autoGrow();
  }

  if (document.body) boot();
  else document.addEventListener("DOMContentLoaded", boot);

  /* ================================================================ *
   * Markup
   * ================================================================ */
  function MARKUP() {
    return `
      <button id="launcher" class="launcher" aria-label="Open ${escapeHtml(TITLE)}" aria-expanded="false">
        <svg class="launcher__chat" viewBox="0 0 24 24" width="26" height="26" aria-hidden="true">
          <path fill="currentColor" d="M12 3C6.5 3 2 6.8 2 11.5c0 2.4 1.2 4.6 3.1 6.1-.1 1.2-.6 2.6-1.6 3.7 1.9-.2 3.5-.9 4.7-1.8 1.2.4 2.5.5 3.8.5 5.5 0 10-3.8 10-8.5S17.5 3 12 3z"/>
          <circle class="launcher__dot" cx="8" cy="11.5" r="1.3"/>
          <circle class="launcher__dot" cx="12" cy="11.5" r="1.3"/>
          <circle class="launcher__dot" cx="16" cy="11.5" r="1.3"/>
        </svg>
        <svg class="launcher__close" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true">
          <path d="M6 6l12 12M18 6 6 18"/>
        </svg>
      </button>

      <section id="panel" class="panel" role="dialog" aria-label="${escapeHtml(TITLE)}">
        <header class="head">
          <div class="brand">
            <span class="brand__mark">${SUN_MARK}</span>
            <span class="brand__text">
              <span class="brand__title">${escapeHtml(TITLE)}</span>
              <span class="brand__sub">Answers from TERI's research</span>
            </span>
          </div>
          <div class="head__actions">
            <button id="new-chat" class="icon-btn" title="New chat" aria-label="New chat">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-6"/><path d="M17.6 3.6a2.1 2.1 0 0 1 3 3L13 14.2l-4 1 1-4z"/></svg>
            </button>
            <button id="expand" class="icon-btn" title="Expand" aria-label="Expand">
              <svg class="ic-expand" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 4h5v5M9 20H4v-5M20 4l-6 6M4 20l6-6"/></svg>
              <svg class="ic-compress" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4v6h6M10 20v-6H4M14 10l6-6M10 14l-6 6"/></svg>
            </button>
            <button id="close" class="icon-btn" title="Close" aria-label="Close">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>
            </button>
          </div>
        </header>

        <main id="messages" class="messages" aria-live="polite">
          <div id="welcome" class="welcome">
            <h2 class="welcome__title">What would you like to know?</h2>
            <p class="welcome__hint">Ask about TERI's research, publications, people and projects. Every answer links to the sources it draws on.</p>
            <div id="suggestions" class="suggestions"></div>
          </div>
        </main>

        <footer class="composer">
          <div class="composer__box">
            <textarea id="input" class="composer__input" rows="1"
              aria-label="Your question"
              placeholder="Ask a question about TERI's work"></textarea>
            <button id="send" class="composer__send" title="Send" aria-label="Send">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6"/></svg>
            </button>
          </div>
          <p class="composer__note">AI answers can be wrong. Check the linked sources.</p>
        </footer>

        <div id="cite-pop" class="cite-pop" aria-hidden="true"></div>
      </section>
    `;
  }

  /* ================================================================ *
   * Styles — fully scoped inside the shadow root.
   *
   * Indigo (#2D2F7D, the teriin.org navigation bar) carries the brand:
   * header, user messages, send, launcher. Saffron, from the same
   * site's call-to-action buttons, appears only in the sun mark. Every
   * other surface is white or an indigo-tinted grey.
   * ================================================================ */
  function STYLES() {
    return `<style>
    :host {
      --brand: #2d2f7d;
      --brand-deep: #1f2163;
      --brand-mist: #eeeff8;
      --brand-haze: #f6f6fb;
      --sun: #f5b21b;
      --ink: #1a1b3d;
      --slate: #5d6080;
      --line: #e3e4ef;
      --line-strong: #d4d5e5;
      --bad: #b4233a;
      --bad-mist: #fcf0f2;
      --warn: #84570a;
      --warn-mist: #fff6e3;
      --sans: "IBM Plex Sans", "Segoe UI", system-ui, -apple-system, Roboto, Helvetica, Arial, sans-serif;
      --serif: "Source Serif 4", Georgia, "Times New Roman", serif;
      --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      --ease: cubic-bezier(.2, .8, .2, 1);
      /* Alert glyph for the error and unverified-figures notes, painted in
         currentColor through a mask. */
      --alert-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round'%3E%3Ccircle cx='12' cy='12' r='9'/%3E%3Cpath d='M12 7.5v5.5M12 16.5v.01'/%3E%3C/svg%3E");

      all: initial;
      font-family: var(--sans);
      font-size: 14.5px;
      line-height: 1.55;
      color: var(--ink);
      -webkit-font-smoothing: antialiased;
    }
    *, *::before, *::after { box-sizing: border-box; }
    [hidden] { display: none !important; }
    button { font: inherit; }
    :focus-visible { outline: 2px solid var(--brand); outline-offset: 2px; }

    /* ---- Sun mark: saffron sun, ground lines in the surrounding colour ---- */
    .sun__rays { stroke: var(--sun); }
    .sun__disc { fill: var(--sun); }
    .sun__ground { stroke: currentColor; }

    /* ---- Launcher ---- */
    .launcher {
      position: fixed;
      right: 24px;
      bottom: 24px;
      width: 58px;
      height: 58px;
      border: none;
      border-radius: 50%;
      background: var(--brand);
      color: #fff;
      cursor: pointer;
      display: grid;
      place-items: center;
      box-shadow: 0 12px 28px -8px rgba(45, 47, 125, .6), 0 2px 6px rgba(45, 47, 125, .25);
      z-index: 2147483646;
      transition: transform .2s var(--ease), background-color .2s ease;
    }
    .launcher:hover { background: var(--brand-deep); transform: translateY(-2px); }
    .launcher:focus-visible { outline: 3px solid var(--sun); outline-offset: 3px; }
    .launcher svg { grid-area: 1 / 1; transition: opacity .18s ease, transform .25s var(--ease); }
    .launcher__dot { fill: var(--brand); transition: fill .2s ease; }
    .launcher:hover .launcher__dot { fill: var(--brand-deep); }
    .launcher__close { opacity: 0; transform: rotate(-90deg) scale(.6); }
    :host(.open) .launcher__chat { opacity: 0; transform: rotate(90deg) scale(.6); }
    :host(.open) .launcher__close { opacity: 1; transform: none; }

    /* ---- Panel ---- */
    .panel {
      position: fixed;
      right: 24px;
      bottom: 96px;
      width: 404px;
      max-width: calc(100vw - 32px);
      height: 660px;
      max-height: calc(100vh - 124px);
      background: #fff;
      border-radius: 18px;
      box-shadow:
        0 0 0 1px rgba(45, 47, 125, .08),
        0 24px 56px -12px rgba(26, 27, 61, .32),
        0 8px 18px -8px rgba(26, 27, 61, .14);
      display: none;
      flex-direction: column;
      overflow: hidden;
      z-index: 2147483646;
      transform-origin: bottom right;
    }
    :host(.open) .panel { display: flex; animation: panel-in .22s var(--ease); }
    @keyframes panel-in {
      from { opacity: 0; transform: translateY(10px) scale(.97); }
      to   { opacity: 1; transform: none; }
    }

    /* Expanded: a large dialog centred over a dimmed page, its content held to
       a readable column. */
    :host(.expanded) .panel {
      inset: 0;
      margin: auto;
      width: min(1080px, calc(100vw - 48px));
      height: min(880px, calc(100vh - 48px));
      max-width: none;
      max-height: none;
      transform-origin: center;
      box-shadow: 0 0 0 100vmax rgba(17, 18, 48, .5), 0 32px 80px -16px rgba(0, 0, 0, .45);
    }
    :host(.expanded) .messages,
    :host(.expanded) .composer {
      padding-left: max(24px, calc((100% - 720px) / 2));
      padding-right: max(24px, calc((100% - 720px) / 2));
    }

    /* ---- Header: the indigo band of the host site's navigation ---- */
    .head {
      flex-shrink: 0;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 10px 14px 16px;
      background: var(--brand);
      color: #fff;
    }
    .brand { display: flex; align-items: center; gap: 11px; min-width: 0; }
    .brand__mark {
      flex-shrink: 0;
      width: 38px;
      height: 38px;
      display: grid;
      place-items: center;
      border-radius: 11px;
      background: rgba(255, 255, 255, .1);
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, .14);
    }
    .brand__mark svg { width: 25px; height: 25px; }
    .brand__text { display: flex; flex-direction: column; min-width: 0; }
    .brand__title,
    .brand__sub { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .brand__title { font-size: 15.5px; font-weight: 600; line-height: 1.25; letter-spacing: .005em; }
    .brand__sub { font-size: 12.5px; line-height: 1.35; color: rgba(255, 255, 255, .72); }
    .head__actions { flex-shrink: 0; display: flex; align-items: center; gap: 2px; }
    .icon-btn {
      width: 34px;
      height: 34px;
      display: grid;
      place-items: center;
      padding: 0;
      border: none;
      border-radius: 9px;
      background: transparent;
      color: rgba(255, 255, 255, .8);
      cursor: pointer;
      transition: background-color .15s ease, color .15s ease;
    }
    .icon-btn:hover { background: rgba(255, 255, 255, .12); color: #fff; }
    .icon-btn:focus-visible { outline: 2px solid #fff; outline-offset: -2px; }
    .icon-btn svg { width: 18px; height: 18px; }
    .ic-compress { display: none; }
    :host(.expanded) .ic-expand { display: none; }
    :host(.expanded) .ic-compress { display: block; }
    :host(.expanded) .head { padding: 16px 16px 16px 22px; }

    /* ---- Messages ---- */
    .messages {
      flex: 1;
      overflow-y: auto;
      overscroll-behavior: contain;
      padding: 22px 18px 14px;
      display: flex;
      flex-direction: column;
      gap: 20px;
      scrollbar-width: thin;
      scrollbar-color: var(--line-strong) transparent;
    }

    /* ---- Welcome: a heading, one line of help, and a list of questions ---- */
    .welcome { padding: 2px 2px 4px; }
    .welcome__title {
      margin: 0 0 6px;
      font-family: var(--serif);
      font-size: 25px;
      font-weight: 600;
      line-height: 1.2;
      letter-spacing: -.01em;
      color: var(--brand);
    }
    .welcome__hint { margin: 0 0 20px; font-size: 14px; line-height: 1.55; color: var(--slate); }
    /* One bordered list; the 1px grid gap over the hairline background draws
       the dividers, so they stay crisp in one column or two. */
    .suggestions {
      display: grid;
      grid-template-columns: 1fr;
      gap: 1px;
      background: var(--line);
      border: 1px solid var(--line);
      border-radius: 14px;
      overflow: hidden;
    }
    .suggestion {
      display: flex;
      align-items: center;
      gap: 12px;
      width: 100%;
      padding: 11px 14px 11px 12px;
      border: none;
      background: #fff;
      color: var(--ink);
      font-size: 13.5px;
      line-height: 1.4;
      text-align: left;
      cursor: pointer;
      transition: background-color .15s ease;
    }
    .suggestion:hover { background: var(--brand-haze); }
    .suggestion:focus-visible { outline: 2px solid var(--brand); outline-offset: -2px; }
    .suggestion__icon {
      flex-shrink: 0;
      width: 30px;
      height: 30px;
      display: grid;
      place-items: center;
      border-radius: 8px;
      background: var(--brand-mist);
      color: var(--brand);
      transition: background-color .15s ease, color .15s ease;
    }
    .suggestion:hover .suggestion__icon { background: var(--brand); color: #fff; }

    :host(.expanded) .welcome { margin-top: 6vh; }
    :host(.expanded) .welcome__title { font-size: 34px; margin-bottom: 8px; }
    :host(.expanded) .welcome__hint { font-size: 15.5px; margin-bottom: 26px; }
    :host(.expanded) .suggestions { grid-template-columns: 1fr 1fr; }
    :host(.expanded) .suggestion { padding: 14px 16px 14px 14px; font-size: 14px; }

    /* ---- Message rows ---- */
    .msg { display: flex; animation: msg-in .22s var(--ease) both; }
    @keyframes msg-in {
      from { opacity: 0; transform: translateY(6px); }
      to   { opacity: 1; transform: none; }
    }
    .msg--user { justify-content: flex-end; }
    .bubble { white-space: pre-wrap; overflow-wrap: break-word; }
    .msg--user .bubble {
      max-width: 85%;
      padding: 10px 14px;
      border-radius: 18px 18px 5px 18px;
      background: var(--brand);
      color: #fff;
      line-height: 1.5;
    }
    /* Bot replies span the column as prose beside the mark; no bubble. */
    .msg--bot { gap: 12px; align-items: flex-start; }
    .msg__avatar {
      flex-shrink: 0;
      width: 30px;
      height: 30px;
      display: grid;
      place-items: center;
      border-radius: 9px;
      background: var(--brand);
      color: #fff;
    }
    .msg__avatar svg { width: 21px; height: 21px; }
    .msg__body { flex: 1; min-width: 0; padding-top: 4px; }
    .msg--bot .bubble { line-height: 1.65; }
    :host(.expanded) .msg--bot .bubble { font-size: 15px; }
    .bubble--pending { color: var(--slate); }
    .msg--bot .bubble--error {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      padding: 10px 12px;
      border-radius: 12px;
      background: var(--bad-mist);
      color: var(--bad);
      line-height: 1.5;
      white-space: normal;
    }
    .bubble--error::before,
    .answer-warn::before {
      content: "";
      flex-shrink: 0;
      width: 16px;
      height: 16px;
      margin-top: 2px;
      background: currentColor;
      -webkit-mask: var(--alert-icon) center / contain no-repeat;
              mask: var(--alert-icon) center / contain no-repeat;
    }

    /* Actions under a settled answer. */
    .msg__actions { display: flex; gap: 4px; margin: 8px 0 0 -9px; }
    .action {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      height: 28px;
      padding: 0 9px;
      border: none;
      border-radius: 8px;
      background: transparent;
      color: var(--slate);
      font-size: 12.5px;
      font-weight: 500;
      cursor: pointer;
      transition: background-color .15s ease, color .15s ease;
    }
    .action:hover { background: var(--brand-haze); color: var(--brand); }
    .action.is-done { color: var(--brand); }
    .action svg { width: 15px; height: 15px; }

    /* ---- Working indicator: shimmering status word + bouncing dots ---- */
    .loader { display: inline-flex; align-items: center; gap: 10px; min-height: 24px; }
    .loader__text { display: inline-flex; }
    .loader__word {
      display: inline-block;
      font-weight: 500;
      background: linear-gradient(100deg,
        var(--slate) 25%, var(--brand) 45%,
        #7376d0 55%, var(--slate) 75%);
      background-size: 220% 100%;
      -webkit-background-clip: text;
              background-clip: text;
      -webkit-text-fill-color: transparent;
      color: transparent;
      animation: word-in .4s ease both, shimmer 2.2s linear infinite;
    }
    .loader__dots { display: inline-flex; align-items: center; gap: 3px; }
    .loader__dots i {
      width: 4px;
      height: 4px;
      border-radius: 50%;
      background: var(--brand);
      animation: dot-bounce 1.2s ease-in-out infinite;
    }
    .loader__dots i:nth-child(2) { animation-delay: .18s; }
    .loader__dots i:nth-child(3) { animation-delay: .36s; }
    @keyframes shimmer {
      0%   { background-position: 220% 0; }
      100% { background-position: -20% 0; }
    }
    @keyframes word-in {
      from { opacity: 0; transform: translateY(3px); }
      to   { opacity: 1; transform: translateY(0); }
    }
    @keyframes dot-bounce {
      0%, 80%, 100% { opacity: .3; transform: translateY(0); }
      40% { opacity: 1; transform: translateY(-3px); }
    }

    /* ---- Answer typography ---- */
    .bubble > :first-child { margin-top: 0; }
    .bubble > :last-child { margin-bottom: 0; }
    .bubble p { margin: 0 0 .7em; }
    .bubble ul, .bubble ol { margin: 0 0 .7em; padding-left: 1.3em; }
    .bubble li { margin: .2em 0; }
    .bubble li::marker { color: var(--slate); }
    .bubble h1, .bubble h2, .bubble h3, .bubble h4, .bubble h5, .bubble h6 {
      margin: 1em 0 .4em;
      font-size: 1.04em;
      font-weight: 600;
      line-height: 1.35;
      color: var(--ink);
    }
    .bubble strong { font-weight: 600; }
    .bubble a {
      color: var(--brand);
      text-decoration: underline;
      text-decoration-color: rgba(45, 47, 125, .35);
      text-underline-offset: 2px;
    }
    .bubble a:hover { text-decoration-color: currentColor; }
    .bubble code {
      padding: .05em .35em;
      border: 1px solid var(--line);
      border-radius: 5px;
      background: var(--brand-haze);
      font-family: var(--mono);
      font-size: .86em;
    }
    .bubble pre {
      margin: 0 0 .7em;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--brand-haze);
      overflow-x: auto;
    }
    .bubble pre code { padding: 0; border: none; background: none; }

    .bubble .table-wrap {
      margin: 0 0 .8em;
      border: 1px solid var(--line);
      border-radius: 10px;
      overflow-x: auto;
    }
    .bubble table { width: 100%; border-collapse: collapse; font-size: .9em; line-height: 1.45; }
    .bubble th, .bubble td {
      padding: 8px 12px;
      text-align: left;
      vertical-align: top;
      border-bottom: 1px solid var(--line);
    }
    .bubble th + th, .bubble td + td { border-left: 1px solid var(--line); }
    .bubble tbody tr:last-child td { border-bottom: none; }
    .bubble thead th { background: var(--brand-mist); color: var(--brand-deep); font-weight: 600; }
    .bubble tbody tr:nth-child(even) { background: var(--brand-haze); }

    /* ---- Answer blocks ---- */
    /* Website-sourced content is the answer proper, so it carries no container
       of its own. Only the supplementary PDF block is set apart, on the same
       tinted surface + hairline the code blocks and citation chips use. */
    .answer-block--pdf {
      margin: 4px 0 .8em;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--brand-haze);
    }
    .answer-block__label {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 8px;
      font-size: 12.5px;
      font-weight: 600;
      color: var(--brand);
    }
    .answer-block__label svg { width: 14px; height: 14px; flex-shrink: 0; }
    .answer-block--pdf > :last-child { margin-bottom: 0; }

    /* ---- Inline citations ---- */
    /* A site-name pill after the claim it supports; its hover card carries
       the title and page. Muted so a cited paragraph still reads as prose,
       solid indigo on hover. The .bubble prefix outranks ".bubble a". */
    .bubble .cite {
      display: inline-block;
      margin-left: 4px;
      padding: 0 8px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--brand-haze);
      color: var(--slate);
      font-size: 11.5px;
      font-weight: 500;
      line-height: 1.65;
      vertical-align: 1px;
      text-decoration: none;
      white-space: nowrap;
      transition: background-color .15s ease, border-color .15s ease, color .15s ease;
    }
    .bubble .cite:hover,
    .bubble .cite:focus-visible {
      background: var(--brand);
      border-color: var(--brand);
      color: #fff;
      outline: none;
    }

    /* Hover card: lifted off the answer by an indigo-tinted shadow. The site
       row names where the link goes, the title leads, and the page/section
       sits beneath it. Never takes the pointer, so moving across it cannot
       flicker it or swallow the chip's click. */
    .cite-pop {
      position: fixed;
      z-index: 10;
      width: max-content;
      max-width: 290px;
      padding: 11px 13px 12px;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: 0 14px 34px -6px rgba(26, 27, 61, .2), 0 2px 6px rgba(26, 27, 61, .06);
      color: var(--ink);
      font-size: 13px;
      line-height: 1.4;
      pointer-events: none;
      opacity: 0;
      visibility: hidden;
      transform: translateY(4px);
      transition: opacity .14s ease, transform .14s ease, visibility 0s linear .14s;
    }
    .cite-pop[data-placement="below"] { transform: translateY(-4px); }
    /* Caret: a rotated square showing two bordered edges toward the chip;
       its white half covers the card's border so the two read as one shape. */
    .cite-pop::before {
      content: "";
      position: absolute;
      left: var(--caret-x, 50%);
      width: 10px;
      height: 10px;
      background: #fff;
      border: 1px solid var(--line);
      transform: translateX(-50%) rotate(45deg);
    }
    .cite-pop[data-placement="above"]::before { bottom: -6px; border-top: none; border-left: none; }
    .cite-pop[data-placement="below"]::before { top: -6px; border-bottom: none; border-right: none; }
    .cite-pop.is-open {
      opacity: 1;
      visibility: visible;
      transform: none;
      transition-delay: 0s;
    }
    .cite-pop__site {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--slate);
      font-size: 12px;
      font-weight: 500;
    }
    .cite-pop__icon {
      flex-shrink: 0;
      width: 22px;
      height: 22px;
      display: grid;
      place-items: center;
      border-radius: 6px;
      background: var(--brand-mist);
      color: var(--brand);
    }
    .cite-pop__icon svg { width: 13px; height: 13px; }
    .cite-pop__host { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .cite-pop__open { display: inline-flex; color: var(--brand); }
    .cite-pop__open svg { width: 13px; height: 13px; }
    .cite-pop__title {
      margin-top: 8px;
      font-size: 13.5px;
      font-weight: 600;
      line-height: 1.35;
      display: -webkit-box;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 3;
      overflow: hidden;
    }
    .cite-pop__meta { margin-top: 4px; color: var(--slate); font-size: 12px; }

    /* Unverified-figures notice: amber, under the answer. */
    .answer-warn {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      margin-top: 12px;
      padding: 9px 12px;
      border-radius: 10px;
      background: var(--warn-mist);
      color: var(--warn);
      font-size: 12.5px;
      line-height: 1.45;
      white-space: normal;
    }

    /* ---- Composer: a rounded box with the send button inside ---- */
    .composer { flex-shrink: 0; padding: 8px 16px 12px; background: #fff; }
    .composer__box {
      display: flex;
      align-items: flex-end;
      gap: 8px;
      padding: 6px 6px 6px 14px;
      background: #fff;
      border: 1px solid var(--line-strong);
      border-radius: 16px;
      box-shadow: 0 1px 2px rgba(26, 27, 61, .04), 0 6px 18px -8px rgba(26, 27, 61, .14);
      transition: border-color .15s ease, box-shadow .15s ease;
    }
    .composer__box:focus-within {
      border-color: var(--brand);
      box-shadow: 0 0 0 3px rgba(45, 47, 125, .12);
    }
    .composer__input {
      flex: 1;
      min-width: 0;
      max-height: 120px;
      padding: 8px 0;
      resize: none;
      overflow-y: hidden;
      border: none;
      background: transparent;
      color: var(--ink);
      font: inherit;
      line-height: 1.45;
    }
    .composer__input::placeholder { color: #74779a; }
    .composer__input:focus,
    .composer__input:focus-visible { outline: none; }
    .composer__send {
      flex-shrink: 0;
      width: 36px;
      height: 36px;
      display: grid;
      place-items: center;
      padding: 0;
      border: none;
      border-radius: 11px;
      background: var(--brand);
      color: #fff;
      cursor: pointer;
      transition: background-color .15s ease, color .15s ease, transform .1s ease;
    }
    .composer__send:hover { background: var(--brand-deep); }
    .composer__send:active { transform: scale(.94); }
    .composer__send svg { width: 18px; height: 18px; }
    /* Nothing to send yet, or an answer still streaming: the button rests. */
    .composer__box:has(.composer__input:placeholder-shown) .composer__send,
    .composer__send:disabled,
    .composer__send.busy {
      background: var(--brand-mist);
      color: #9496bd;
      cursor: default;
      transform: none;
    }
    .composer__note { margin: 8px 0 0; text-align: center; font-size: 11.5px; line-height: 1.4; color: var(--slate); }

    :host(.expanded) .composer { padding-bottom: 20px; }
    :host(.expanded) .composer__box { padding: 8px 8px 8px 18px; border-radius: 18px; }
    :host(.expanded) .composer__input { min-height: 44px; font-size: 15px; }

    /* ---- Mobile: full screen ---- */
    @media (max-width: 480px) {
      .panel,
      :host(.expanded) .panel {
        inset: 0;
        margin: 0;
        width: 100%;
        height: 100%;
        max-width: none;
        max-height: none;
        border-radius: 0;
        box-shadow: none;
      }
      .head { padding-top: max(14px, env(safe-area-inset-top)); }
      .composer { padding-bottom: max(12px, env(safe-area-inset-bottom)); }
      #expand { display: none; }
      .launcher { right: 16px; bottom: 16px; }
      :host(.expanded) .suggestions { grid-template-columns: 1fr; }
      :host(.expanded) .welcome__title { font-size: 26px; }
    }

    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after {
        animation-duration: .01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: .01ms !important;
      }
      .loader__word { -webkit-text-fill-color: var(--brand); color: var(--brand); }
    }
    </style>`;
  }
})();
