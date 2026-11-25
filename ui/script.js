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

  // Suggested questions on view before "Show all". The questions themselves
  // come from the API (see loadSuggestions).
  const SUGGEST_PEEK = 3;

  // The widget's typeface, linked from the host document: Chrome ignores
  // @font-face declared inside a shadow root, but a family the document loads
  // is usable within it. data-fonts="off" skips the request (for a host with a
  // strict CSP); the font stack then falls back to system faces.
  const FONT_CSS =
    "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap";
  const FONTS_ON = (cfg.fonts || "").trim().toLowerCase() !== "off";

  // Status words cycled while the bot is working, before the first token lands.
  const LOADER_PHASES = [
    "Thinking",
    "Reading relevant sources",
    "Generating your answer",
  ];

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

  // The arrow at the end of each suggested question.
  const ARROW_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M5 12h14"/>' +
    '<path d="m12 5 7 7-7 7"/>' +
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
  // Every suggested question the API serves, and the fetch for them.
  let suggestionPool = [];
  let suggestionsLoad = null;

  let host, root, el;

  function openPanel() {
    isOpen = true;
    host.classList.add("open");
    el.launcher.setAttribute("aria-expanded", "true");
    loadSuggestions();
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
    const label = expanded ? "Minimise" : "Expand";
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
    setFresh(true);
    el.input.value = "";
    autoGrow();
    el.input.focus();
  }

  // Layout changes that move the composer are animated FLIP-style: measure
  // where each node sits, apply the change, then play each one from its old
  // spot to its new one.
  const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)");
  function flip(nodes, change) {
    const before = nodes.map((n) => n.getBoundingClientRect().top);
    change();
    if (!isOpen || REDUCED_MOTION.matches) return;
    nodes.forEach((n, i) => {
      const delta = before[i] - n.getBoundingClientRect().top;
      if (delta)
        n.animate(
          [{ transform: "translateY(" + delta + "px)" }, { transform: "none" }],
          { duration: 320, easing: "cubic-bezier(.2, .8, .2, 1)" },
        );
    });
  }

  // A fresh conversation is one column under the header: the welcome heading,
  // the composer, then a few suggested questions, scrolling together if the
  // list runs long. The first message sends the composer to the foot; "New
  // chat" brings it back.
  function setFresh(on) {
    if (host.classList.contains("fresh") === on) return;
    flip([el.composer], () => {
      host.classList.toggle("fresh", on);
      el.welcome.hidden = !on;
      el.stage.scrollTop = 0;
      setBrowsing(false);
      if (on) renderSuggestions(); // a new chat draws a new few
    });
  }

  // "Show all" opens the rest of the list beneath the first few, in place.
  function setBrowsing(open) {
    host.classList.toggle("browsing", open);
    el.suggestToggle.setAttribute("aria-expanded", String(open));
  }
  function toggleBrowsing() {
    setBrowsing(!host.classList.contains("browsing"));
  }

  // The suggested questions are a list kept in data/queries_suggestions.json
  // and served by GET /suggestions. Fetched once, at the first sign the reader
  // means to open the chat (the launcher hovered, focused or clicked), so a
  // page view that never opens it costs no request. A failed fetch leaves the
  // section hidden and is tried again on the next open.
  function loadSuggestions() {
    if (suggestionsLoad) return;
    suggestionsLoad = fetch(API_BASE + "/suggestions")
      .then((res) =>
        res.ok ? res.json() : Promise.reject(new Error(res.status)),
      )
      .then((data) => {
        const list =
          data && Array.isArray(data.suggestions) ? data.suggestions : [];
        suggestionPool = list.filter((s) => typeof s === "string" && s.trim());
        renderSuggestions();
      })
      .catch(() => {
        suggestionsLoad = null;
      });
  }

  // A fresh shuffle each time the start screen shows. The first few are on
  // view; "Show all" adds the rest beneath them, in the same order, so opening
  // the list never reshuffles what the reader was already looking at.
  function renderSuggestions() {
    const order = shuffled(suggestionPool);
    el.suggestions.textContent = "";
    order.forEach((text, idx) => {
      const row = document.createElement("li");
      if (idx >= SUGGEST_PEEK) row.className = "suggestions__extra";
      const item = document.createElement("button");
      item.type = "button";
      item.className = "suggestion";
      item.innerHTML = "<span></span>" + ARROW_ICON;
      item.firstChild.textContent = text;
      item.addEventListener("click", () => {
        el.input.value = text;
        handleSend();
      });
      row.appendChild(item);
      el.suggestions.appendChild(row);
    });
    el.suggestCount.textContent = String(order.length);
    el.suggestToggle.hidden = order.length <= SUGGEST_PEEK;
    el.suggest.hidden = !order.length;
  }

  // Fisher–Yates, on a copy.
  function shuffled(list) {
    const out = list.slice();
    for (let i = out.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      const swap = out[i];
      out[i] = out[j];
      out[j] = swap;
    }
    return out;
  }

  /* ---------------------------------------------------------------- *
   * Messages
   * ---------------------------------------------------------------- */
  // A bot reply is a body column spanning the panel: the answer bubble, then
  // the actions added once it settles. A user message is the bubble alone.
  function addMessage(role, text) {
    setFresh(false);
    const wrap = document.createElement("div");
    wrap.className = "msg msg--" + role;
    let body = wrap;
    if (role === "bot") {
      body = document.createElement("div");
      body.className = "msg__body";
      wrap.appendChild(body);
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
        err && err.message
          ? err.message
          : "The request failed. Please try again.";
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
      throw new Error(
        "Couldn't reach the assistant. Check your connection and try again.",
      );
    }
    if (!res.ok || !res.body)
      throw new Error(
        "The assistant is unavailable right now (HTTP " +
          res.status +
          "). Please try again shortly.",
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
          const indent = (lines[i].match(/^[ \t]*/) || [""])[0].replace(
            /\t/g,
            "    ",
          ).length;
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
        return (
          '<div class="answer-block answer-block--website">' + body + "</div>"
        );
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
  const GENERIC_SLD = new Set([
    "ac",
    "co",
    "com",
    "edu",
    "gov",
    "net",
    "nic",
    "org",
    "res",
  ]);

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
    const caret = Math.max(
      CARET_INSET,
      Math.min(center - left, w - CARET_INSET),
    );
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
    host.className = "fresh";
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
      stage: $("#stage"),
      messages: $("#messages"),
      welcome: $("#welcome"),
      suggest: $("#suggest"),
      suggestToggle: $("#suggest-toggle"),
      suggestCount: $("#suggest-count"),
      suggestions: $("#suggestions"),
      composer: $("#composer"),
      input: $("#input"),
      send: $("#send"),
      citePop: $("#cite-pop"),
    };

    el.launcher.addEventListener("click", () =>
      isOpen ? closePanel() : openPanel(),
    );
    // Start the suggestions fetch a beat before the click lands.
    el.launcher.addEventListener("pointerenter", loadSuggestions);
    el.launcher.addEventListener("focus", loadSuggestions);
    el.newChat.addEventListener("click", resetChat);
    el.close.addEventListener("click", closePanel);
    el.expand.addEventListener("click", toggleExpand);
    el.suggestToggle.addEventListener("click", toggleBrowsing);
    el.send.addEventListener("click", handleSend);
    el.input.addEventListener("input", autoGrow);
    el.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    });
    // Escape folds the full suggestion list first, then closes the panel.
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || !isOpen) return;
      if (host.classList.contains("browsing")) toggleBrowsing();
      else closePanel();
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
            <span class="brand__title">${escapeHtml(TITLE)}</span>
          </div>
          <div class="head__actions">
            <button id="new-chat" class="icon-btn" title="New chat" aria-label="New chat">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.4 2.6a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4Z"/></svg>
            </button>
            <button id="expand" class="icon-btn" title="Expand" aria-label="Expand">
              <svg class="ic-expand" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="m21 3-7 7"/><path d="m3 21 7-7"/></svg>
              <svg class="ic-compress" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 14h6v6"/><path d="M20 10h-6V4"/><path d="m14 10 7-7"/><path d="m3 21 7-7"/></svg>
            </button>
            <button id="close" class="icon-btn" title="Collapse" aria-label="Collapse">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
            </button>
          </div>
        </header>

        <div id="stage" class="stage">
          <main id="messages" class="messages" aria-live="polite">
            <div id="welcome" class="welcome">
              <h2 class="welcome__title">What would you like to know about TERI?</h2>
              <p class="welcome__hint">Ask about its research, publications, people or projects.</p>
            </div>
          </main>

          <footer id="composer" class="composer">
            <div class="composer__box">
              <textarea id="input" class="composer__input" rows="1"
                aria-label="Ask a question about TERI's work"
                placeholder="Ask a question about TERI's work"></textarea>
              <button id="send" class="composer__send" title="Send" aria-label="Send">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5"/><path d="m5 12 7-7 7 7"/></svg>
              </button>
            </div>
            <p class="composer__note">AI answers can be wrong. Check the linked sources.</p>
          </footer>

          <section id="suggest" class="suggest" aria-labelledby="suggest-label" hidden>
            <div class="suggest__head">
              <span id="suggest-label" class="suggest__title">Suggested questions</span>
              <button id="suggest-toggle" class="suggest__toggle" type="button" aria-expanded="false" aria-controls="suggestions">
                <span class="suggest__more">Show all <span id="suggest-count"></span></span>
                <span class="suggest__less">Show fewer</span>
                <svg class="suggest__chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
              </button>
            </div>
            <ul id="suggestions" class="suggestions"></ul>
          </section>
        </div>

        <div id="cite-pop" class="cite-pop" aria-hidden="true"></div>
      </section>
    `;
  }

  /* ================================================================ *
   * Styles — fully scoped inside the shadow root.
   *
   * Indigo (#2F2E8F) carries the brand, and only where it marks
   * something: the title, user messages, send, launcher, links, focus.
   * Every other surface is white or an indigo-tinted grey, so the answer
   * is what the eye lands on.
   * ================================================================ */
  function STYLES() {
    return `<style>
    :host {
      --brand: #2f2e8f;
      --brand-deep: #24236f;
      --brand-mist: #eeeff6;
      --brand-haze: #f6f6fb;
      --tint: #f3f4fa;
      --ink: #1b1a45;
      --text: #2a2a3c;
      --slate: #5e5f78;
      --faint: #6b6c85;
      --icon: #4a4b66;
      --icon-faint: #9a9bb5;
      --line: #e6e7f0;
      --line-strong: #c9cbe0;
      --edge: #dcddea;
      --bad: #b4233a;
      --bad-mist: #fcf0f2;
      --warn: #84570a;
      --warn-mist: #fff6e3;
      --sans: "IBM Plex Sans", "Segoe UI", system-ui, -apple-system, Roboto, Helvetica, Arial, sans-serif;
      --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      --ease: cubic-bezier(.2, .8, .2, 1);
      --shadow: 0 24px 60px -24px rgba(27, 26, 69, .28), 0 2px 6px rgba(27, 26, 69, .06);
      /* Side padding of the panel's one column. The expanded dialog holds the
         column to 720px, gutters included. */
      --gutter: 20px;
      /* Alert glyph for the error and unverified-figures notes, painted in
         currentColor through a mask. */
      --alert-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round'%3E%3Ccircle cx='12' cy='12' r='9'/%3E%3Cpath d='M12 7.5v5.5M12 16.5v.01'/%3E%3C/svg%3E");

      all: initial;
      font-family: var(--sans);
      font-size: 14.5px;
      line-height: 1.55;
      color: var(--text);
      -webkit-font-smoothing: antialiased;
    }
    *, *::before, *::after { box-sizing: border-box; }
    [hidden] { display: none !important; }
    button { font: inherit; }
    :focus-visible { outline: 2px solid var(--brand); outline-offset: 2px; }

    /* ---- Launcher ---- */
    .launcher {
      position: fixed;
      right: 24px;
      bottom: 24px;
      width: 56px;
      height: 56px;
      border: none;
      border-radius: 50%;
      background: var(--brand);
      color: #fff;
      cursor: pointer;
      display: grid;
      place-items: center;
      box-shadow: 0 10px 24px -8px rgba(47, 46, 143, .55), 0 2px 6px rgba(47, 46, 143, .2);
      z-index: 2147483646;
      transition: transform .2s var(--ease), background-color .2s ease;
    }
    .launcher:hover { background: var(--brand-deep); transform: translateY(-1px); }
    .launcher:focus-visible { outline: 2px solid var(--brand); outline-offset: 3px; }
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
      width: 408px;
      max-width: calc(100vw - 32px);
      height: 660px;
      max-height: calc(100vh - 124px);
      background: #fff;
      border: 1px solid var(--edge);
      border-radius: 16px;
      box-shadow: var(--shadow);
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
    :host(.expanded) {
      --gutter: max(32px, calc((100% - 720px) / 2 + 32px));
    }
    :host(.expanded) .panel {
      inset: 0;
      margin: auto;
      width: min(1080px, calc(100vw - 48px));
      height: min(760px, calc(100vh - 48px));
      max-width: none;
      max-height: none;
      transform-origin: center;
      box-shadow: 0 0 0 100vmax rgba(27, 26, 69, .45), var(--shadow);
    }

    /* ---- Header: the title in indigo on white, a hairline below ---- */
    .head {
      flex-shrink: 0;
      height: 56px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 0 10px 0 20px;
      border-bottom: 1px solid var(--line);
    }
    .brand { display: flex; flex-direction: column; min-width: 0; }
    .brand__title { font-size: 16px; font-weight: 600; line-height: 1.3; letter-spacing: .01em; color: var(--brand); }
    .head__actions { flex-shrink: 0; display: flex; align-items: center; gap: 2px; }
    .icon-btn {
      width: 36px;
      height: 36px;
      display: grid;
      place-items: center;
      padding: 0;
      border: none;
      border-radius: 8px;
      background: transparent;
      color: var(--icon);
      cursor: pointer;
      transition: background-color .15s ease, color .15s ease;
    }
    .icon-btn:hover { background: var(--brand-mist); color: var(--ink); }
    .icon-btn svg { width: 18px; height: 18px; }
    .ic-compress { display: none; }
    :host(.expanded) .ic-expand { display: none; }
    :host(.expanded) .ic-compress { display: block; }
    :host(.expanded) .head { height: 64px; padding: 0 18px 0 28px; }
    :host(.expanded) .brand__title { font-size: 17px; }

    /* ---- Stage: everything under the header, on a faint indigo wash that
       fades to white. On the stage, not the message list, so the wash stays
       put while answers scroll over it. ---- */
    .stage {
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
      background: linear-gradient(180deg, var(--tint) 0%, #fff 300px);
    }

    /* ---- Messages ---- */
    .messages {
      flex: 1;
      min-height: 0;
      overflow-y: auto;
      overscroll-behavior: contain;
      padding: 22px var(--gutter) 14px;
      display: flex;
      flex-direction: column;
      gap: 20px;
      scrollbar-width: thin;
      scrollbar-color: var(--line-strong) transparent;
    }

    /* ---- Welcome: the start screen's heading and the line under it ---- */
    .welcome {
      text-align: center;
      animation: fade-in .3s ease both;
    }
    .welcome__title {
      margin: 0;
      font-size: 25px;
      font-weight: 600;
      line-height: 1.2;
      letter-spacing: -.01em;
      color: var(--ink);
      text-wrap: balance;
    }
    .welcome__hint {
      margin: 10px 0 0;
      font-size: 15px;
      line-height: 1.5;
      color: var(--slate);
      text-wrap: pretty;
    }
    :host(.expanded) .welcome__title { font-size: 36px; }
    :host(.expanded) .welcome__hint { margin-top: 12px; font-size: 17px; }
    @keyframes fade-in {
      from { opacity: 0; }
      to   { opacity: 1; }
    }

    /* ---- Fresh-chat layout ---- */
    /* One column from the top: the welcome, the composer, then the suggested
       questions, the whole stage scrolling once "Show all" runs it past the
       foot. Gone once the conversation starts. The space between welcome and
       composer is the list's padding, not the composer's, so the composer's
       own top edge is the same in both layouts and its slide to the foot
       starts from where it was. */
    :host(.fresh) .stage {
      display: block;
      overflow-y: auto;
      overscroll-behavior: contain;
      scrollbar-gutter: stable;
      scrollbar-width: thin;
      scrollbar-color: var(--line-strong) transparent;
    }
    :host(.fresh) .messages { overflow: visible; padding-top: 44px; padding-bottom: 20px; }
    :host(.fresh.expanded) .messages { padding-top: 88px; padding-bottom: 28px; }

    /* ---- Suggestions: a ruled list under the composer ---- */
    .suggest { display: none; }
    :host(.fresh) .suggest {
      display: block;
      padding: 32px var(--gutter) max(28px, env(safe-area-inset-bottom));
      animation: fade-in .25s ease both;
    }
    :host(.fresh.expanded) .suggest { padding-top: 44px; padding-bottom: 48px; }
    .suggest__head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 0 10px 8px;
    }
    :host(.expanded) .suggest__head { padding: 0 12px 10px; }
    .suggest__title { font-size: 13px; font-weight: 500; line-height: 1; color: var(--slate); }
    .suggest__toggle {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px 0;
      border: none;
      border-radius: 4px;
      background: transparent;
      color: var(--brand);
      font-size: 13.5px;
      font-weight: 500;
      line-height: 1;
      cursor: pointer;
      transition: color .15s ease;
    }
    :host(.expanded) .suggest__toggle { font-size: 14px; }
    .suggest__toggle:hover { color: var(--ink); }
    .suggest__less,
    :host(.browsing) .suggest__more { display: none; }
    :host(.browsing) .suggest__less { display: inline; }
    .suggest__chevron { width: 16px; height: 16px; transition: transform .2s var(--ease); }
    :host(.browsing) .suggest__chevron { transform: rotate(180deg); }
    .suggestions {
      margin: 0;
      padding: 0;
      list-style: none;
      border-top: 1px solid var(--line);
    }
    :host(:not(.browsing)) .suggestions__extra { display: none; }
    .suggestions__extra { animation: fade-in .2s ease both; }
    /* A question is a row with an arrow at its end, ruled off from the next;
       the row lights up under the pointer. */
    .suggestion {
      width: 100%;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 12px 10px;
      border: none;
      border-bottom: 1px solid var(--line);
      background: transparent;
      color: var(--text);
      font-size: 14.5px;
      line-height: 1.4;
      text-align: left;
      cursor: pointer;
      transition: background-color .15s ease, color .15s ease;
    }
    .suggestion:hover { background: var(--brand-haze); color: var(--brand); }
    .suggestion svg { flex: none; width: 16px; height: 16px; color: var(--icon-faint); transition: color .15s ease; }
    .suggestion:hover svg { color: var(--brand); }
    :host(.expanded) .suggestion { padding: 14px 12px; font-size: 15.5px; }

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
      padding: 9px 14px;
      border-radius: 14px 14px 4px 14px;
      background: var(--brand);
      color: #fff;
      line-height: 1.5;
    }
    /* Bot replies span the column as prose; no bubble. */
    .msg__body { flex: 1; min-width: 0; }
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
        #6d6cc9 55%, var(--slate) 75%);
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
      text-decoration-color: rgba(47, 46, 143, .35);
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
      background: #fff;
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
       tinted surface + hairline the code blocks use. */
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
      background: #fff;
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
      box-shadow: 0 14px 34px -6px rgba(27, 26, 69, .2), 0 2px 6px rgba(27, 26, 69, .06);
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
    .composer { flex-shrink: 0; padding: 8px var(--gutter) 14px; }
    :host(.expanded) .composer { padding-bottom: 20px; }
    /* A fresh chat's foot is the suggestions, not the composer. */
    :host(.fresh) .composer { padding-bottom: 0; }
    .composer__box {
      display: flex;
      align-items: flex-end;
      gap: 8px;
      padding: 8px 8px 8px 18px;
      background: #fff;
      border: 1px solid var(--line-strong);
      border-radius: 14px;
      box-shadow: 0 1px 2px rgba(27, 26, 69, .04), 0 6px 20px rgba(27, 26, 69, .06);
      transition: border-color .15s ease, box-shadow .15s ease;
    }
    .composer__box:focus-within {
      border-color: var(--brand);
      box-shadow: 0 0 0 3px rgba(47, 46, 143, .14);
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
      font-size: 15px;
      line-height: 1.45;
    }
    .composer__input::placeholder { color: #7a7b94; }
    .composer__input:focus,
    .composer__input:focus-visible { outline: none; }
    .composer__send {
      flex-shrink: 0;
      width: 38px;
      height: 38px;
      display: grid;
      place-items: center;
      padding: 0;
      border: none;
      border-radius: 10px;
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
      color: var(--icon-faint);
      cursor: default;
      transform: none;
    }
    .composer__note { margin: 10px 0 0; text-align: center; font-size: 12px; line-height: 1.4; color: var(--faint); }

    /* The design's 60px box: a 40px send button inset 9px. */
    :host(.expanded) .composer__box { padding: 9px 9px 9px 20px; }
    :host(.expanded) .composer__input { font-size: 16px; }
    :host(.expanded) .composer__send { width: 40px; height: 40px; }
    :host(.expanded) .composer__note { font-size: 12.5px; }

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
        border: none;
        border-radius: 0;
        box-shadow: none;
      }
      .head {
        height: calc(56px + env(safe-area-inset-top));
        padding-top: env(safe-area-inset-top);
      }
      .composer { padding-bottom: max(14px, env(safe-area-inset-bottom)); }
      #expand { display: none; }
      .launcher { right: 16px; bottom: 16px; }
      :host(.expanded) .welcome__title { font-size: 28px; }
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
