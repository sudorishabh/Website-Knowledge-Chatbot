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
    {
      verb: "Find",
      rest: " India's renewable energy capacity targets",
      icon: ICON.find,
      bg: "#e7f0ff",
      color: "#3b73d6",
    },
    {
      verb: "Compare",
      rest: " solar and wind energy adoption across states",
      icon: ICON.compare,
      bg: "#ece8ff",
      color: "#6b53d6",
    },
    {
      verb: "Track",
      rest: " progress on India's net-zero commitments",
      icon: ICON.track,
      bg: "#e2f4f1",
      color: "#1f9c86",
    },
    {
      verb: "List",
      rest: " key recommendations on sustainable water management",
      icon: ICON.list,
      bg: "#fdeaf3",
      color: "#cc4f8e",
    },
    {
      verb: "Analyze",
      rest: " the main drivers of urban air pollution",
      icon: ICON.analyze,
      bg: "#e9f6e6",
      color: "#4c9f38",
    },
    {
      verb: "Suggest",
      rest: " actions to improve industrial energy efficiency",
      icon: ICON.suggest,
      bg: "#fff1e0",
      color: "#d9871f",
    },
  ];

  // Status words cycled while the bot is working, before the first token lands.
  const LOADER_PHASES = [
    "Thinking",
    "Reading relevant sources",
    "Generating your answer",
  ];

  // Avatar shown to the left of every AI reply — a simple robot head, the
  // conventional mark for an AI-generated response.
  const BOT_AVATAR =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="12" cy="3" r="1"/>' +
    '<path d="M12 4v2"/>' +
    '<rect x="4" y="6" width="16" height="14" rx="5"/>' +
    '<circle cx="9" cy="13.5" r="1.4" fill="currentColor" stroke="none"/>' +
    '<circle cx="15" cy="13.5" r="1.4" fill="currentColor" stroke="none"/>' +
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
    autoGrow();
    el.input.focus();
  }
  function closePanel() {
    isOpen = false;
    host.classList.remove("open");
    hideCitePop(); // Escape can close the panel with a chip still hovered
  }
  function toggleExpand() {
    const expanded = host.classList.toggle("expanded");
    el.expand.title = expanded ? "Shrink" : "Expand";
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

  function renderCards() {
    el.cards.innerHTML = "";
    for (const s of SUGGESTIONS) {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "card";
      card.innerHTML =
        '<span class="card__icon" style="background:' +
        s.bg +
        ";color:" +
        s.color +
        '">' +
        '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
        s.icon +
        "</svg></span>" +
        '<span class="card__text"><strong>' +
        escapeHtml(s.verb) +
        "</strong>" +
        escapeHtml(s.rest) +
        "</span>";
      card.addEventListener("click", () => {
        el.input.value = (s.verb + s.rest).trim();
        handleSend();
      });
      el.cards.appendChild(card);
    }
  }

  function hideWelcome() {
    if (el.welcome && !el.welcome.hidden) el.welcome.hidden = true;
  }
  /* ---------------------------------------------------------------- *
   * Messages
   * ---------------------------------------------------------------- */
  function addMessage(role, text) {
    hideWelcome();
    const wrap = document.createElement("div");
    wrap.className = "msg msg--" + role;
    if (role === "bot") {
      const avatar = document.createElement("div");
      avatar.className = "msg__avatar";
      avatar.setAttribute("aria-hidden", "true");
      avatar.innerHTML = BOT_AVATAR;
      wrap.appendChild(avatar);
    }
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    wrap.appendChild(bubble);
    el.messages.appendChild(wrap);
    scrollToBottom();
    return { wrap, bubble };
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
    const { bubble } = addMessage("bot", "");
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
      history.push({ role: "user", content: text });
      history.push({ role: "assistant", content: answer });
    } catch (err) {
      stopLoader();
      bubble.classList.remove("bubble--pending");
      // Cancelled by "New chat": the bubble is already gone — stay silent.
      if ((err && err.name === "AbortError") || epoch !== chatEpoch) return;
      bubble.classList.add("bubble--error");
      bubble.textContent =
        "⚠ " + (err && err.message ? err.message : "request failed");
    } finally {
      if (currentAbort === ctrl) currentAbort = null;
      if (epoch === chatEpoch) setStreaming(false);
      scrollToBottom();
    }
  }

  async function streamChat(question, bubble, signal) {
    const body = { question, history };
    if (top_k) body.top_k = top_k;

    const res = await fetch(API_BASE + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    if (!res.ok || !res.body) throw new Error("HTTP " + res.status);

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
  // Bounded by the message list, so the card never covers the header.
  const POP_GAP = 8;
  function placeCitePop(chip) {
    const pop = el.citePop;
    const r = chip.getBoundingClientRect();
    const bounds = el.messages.getBoundingClientRect();
    const w = pop.offsetWidth;
    const h = pop.offsetHeight;
    const below = r.top - h - POP_GAP < bounds.top + POP_GAP;
    const left = Math.max(
      bounds.left + POP_GAP,
      Math.min(r.left + r.width / 2 - w / 2, bounds.right - w - POP_GAP),
    );
    pop.style.top = (below ? r.bottom + POP_GAP : r.top - h - POP_GAP) + "px";
    pop.style.left = left + "px";
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
    warn.className = "answer-warn";
    warn.textContent =
      "⚠ Some figures in this answer could not be verified against the cited sources.";
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
  function boot() {
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
      cards: $("#cards"),
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

    renderCards();
    autoGrow();
  }

  if (document.body) boot();
  else document.addEventListener("DOMContentLoaded", boot);

  /* ================================================================ *
   * Markup
   * ================================================================ */
  function MARKUP() {
    return `
      <button id="launcher" class="launcher" aria-label="Open ${escapeHtml(TITLE)}">
        <svg class="launcher__chat" viewBox="0 0 24 24" width="26" height="26" aria-hidden="true">
          <path fill="currentColor" d="M12 3C6.5 3 2 6.8 2 11.5c0 2.4 1.2 4.6 3.1 6.1-.1 1.2-.6 2.6-1.6 3.7 1.9-.2 3.5-.9 4.7-1.8 1.2.4 2.5.5 3.8.5 5.5 0 10-3.8 10-8.5S17.5 3 12 3z"/>
        </svg>
        <svg class="launcher__close" viewBox="0 0 24 24" width="24" height="24" aria-hidden="true">
          <path fill="currentColor" d="M18.3 5.7 12 12l6.3 6.3-1.4 1.4L10.6 13.4 4.3 19.7 2.9 18.3 9.2 12 2.9 5.7l1.4-1.4L10.6 10.6l6.3-6.3z"/>
        </svg>
      </button>

      <section id="panel" class="panel" role="dialog" aria-label="${escapeHtml(TITLE)}">
        <header class="head">
          <div class="brand">
            <span class="brand__title">${escapeHtml(TITLE)}</span>
          </div>
          <div class="head__actions">
            <button id="new-chat" class="icon-btn" title="New chat" aria-label="New chat">
              <span class="new-chat__label">New chat</span>
            </button>
            <button id="expand" class="icon-btn" title="Expand" aria-label="Expand">
              <svg class="ic-expand" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5"/></svg>
              <svg class="ic-compress" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3v5H4M15 3v5h5M9 21v-5H4M15 21v-5h5"/></svg>
            </button>
            <button id="close" class="icon-btn" title="Close" aria-label="Close">
              <svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M18.3 5.7 13.4 10.6 18.3 15.5 16.9 16.9 12 12 7.1 16.9 5.7 15.5 10.6 10.6 5.7 5.7 7.1 4.3 12 9.2 16.9 4.3z"/></svg>
            </button>
          </div>
        </header>

        <main id="messages" class="messages" aria-live="polite">
          <div id="welcome" class="welcome">
            <h2 class="welcome__title">Welcome to ${escapeHtml(TITLE)}</h2>
            <p class="welcome__hint">What would you like to explore today?</p>
            <div id="cards" class="cards"></div>
          </div>
        </main>

        <footer class="composer">
          <div class="composer__box">
            <textarea id="input" class="composer__input" rows="1"
              placeholder="Ask about policies, best practices, or data insights"></textarea>
            <button id="send" class="composer__send" title="Send" aria-label="Send">
              <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M3 20.5 21 12 3 3.5 3 10l12 2-12 2z"/></svg>
            </button>
          </div>
        </footer>

        <div id="cite-pop" class="cite-pop" aria-hidden="true"></div>
      </section>
    `;
  }

  /* ================================================================ *
   * Styles — fully scoped inside the shadow root.
   * Tweak the TERI palette here once exact brand hex is confirmed.
   * ================================================================ */
  function STYLES() {
    return `<style>
    :host {
      /* ---- AI Sarthi palette (var names kept to minimise churn) ---- */
      --teri-green: #25705e;
      --teri-green-dark: #1c5648;
      --teri-green-soft: #e3f0ec;
      --teri-ink: #1f2330;
      --teri-dim: #6b7280;
      --teri-bg: #ffffff;
      --teri-surface: #f4f6fb;
      --teri-border: #e6e8ef;
      --teri-user: var(--teri-green);
      --teri-bad: #d64545;
      --teri-warn: #c8860d;
      --radius: 14px;

      all: initial;
      font-family: "Segoe UI", system-ui, -apple-system, Roboto, Helvetica, Arial, sans-serif;
      font-size: 15px;
      line-height: 1.5;
      color: var(--teri-ink);
    }
    *, *::before, *::after { box-sizing: border-box; }

    /* ---- Launcher ---- */
    .launcher {
      position: fixed;
      right: 22px;
      bottom: 22px;
      width: 60px;
      height: 60px;
      border: none;
      border-radius: 50%;
      background: var(--teri-green);
      color: #fff;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 6px 20px rgba(0,0,0,.22);
      z-index: 2147483646;
      transition: transform .15s ease, background .15s ease;
    }
    .launcher:hover { background: var(--teri-green-dark); transform: translateY(-2px); }
    .launcher__close { display: none; }
    :host(.open) .launcher__chat { display: none; }
    :host(.open) .launcher__close { display: block; }

    /* ---- Panel (same look docked or expanded; only size/position differ) ---- */
    .panel {
      position: fixed;
      right: 22px;
      bottom: 94px;
      width: 400px;
      max-width: calc(100vw - 32px);
      height: 620px;
      max-height: calc(100vh - 120px);
      background: linear-gradient(135deg, #e6f2ed 0%, #eef2fa 38%, #f6f0f7 70%, #fef5f2 100%);
      border: 1px solid var(--teri-border);
      border-radius: var(--radius);
      box-shadow: 0 12px 40px rgba(0,0,0,.24);
      display: none;
      flex-direction: column;
      overflow: hidden;
      z-index: 2147483646;
    }
    :host(.open) .panel { display: flex; animation: pop .16s ease; }
    @keyframes pop { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: none; } }

    /* Expanded: large floating dialog (not full-bleed), centred over a dimmed
       backdrop; content still centred in a readable column. */
    :host(.expanded) .panel {
      inset: 0;
      margin: auto;
      width: min(1100px, calc(100vw - 54px));
      height: min(900px, calc(100vh - 54px));
      max-width: calc(100vw - 64px);
      max-height: calc(100vh - 64px);
      border-radius: var(--radius);
      box-shadow: 0 0 0 100vmax rgba(15,23,42,.45), 0 24px 60px rgba(0,0,0,.35);
    }
    :host(.expanded) .messages,
    :host(.expanded) .composer {
      padding-left: max(20px, calc((100% - 1040px) / 2));
      padding-right: max(20px, calc((100% - 1040px) / 2));
    }
    :host(.expanded) .composer { padding-bottom: 26px; }
    :host(.expanded) .composer__box { max-width: 1040px; margin: 0 auto; border-radius: 18px; padding: 10px 10px 10px 18px; }
    :host(.expanded) .composer__input { min-height: 48px; }

    /* ---- Header: white bar with logo mark + dark title (AI Sarthi look) ---- */
    .head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 16px;
      background: #fff;
      color: var(--teri-ink);
      border-bottom: 1px solid var(--teri-border);
    }
    .brand { display: flex; align-items: center; gap: 9px; min-width: 0; }
    .brand__title { font-weight: 700; font-size: 1.05rem; white-space: nowrap; letter-spacing: .01em; }
    .head__actions { display: flex; align-items: center; gap: 6px; }

    /* Expanded header just a touch larger. */
    :host(.expanded) .head { padding: 14px 20px; }
    :host(.expanded) .brand__title { font-size: 1.15rem; }

    .ic-compress { display: none; }
    :host(.expanded) .ic-expand { display: none; }
    :host(.expanded) .ic-compress { display: block; }

    .icon-btn {
      background: transparent;
      border: none;
      color: var(--teri-dim);
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      padding: 4px;
      border-radius: 6px;
    }
    .icon-btn:hover { background: var(--teri-surface); }
    #new-chat {
      padding: 5px 12px;
      border: 1px solid var(--teri-border);
      border-radius: 8px;
      color: var(--teri-green);
    }
    #new-chat:hover { border-color: var(--teri-green); background: var(--teri-green-soft); }
    .new-chat__label { font-size: .82rem; font-weight: 600; }
    #close { color: var(--teri-bad); }

    /* ---- Messages ---- */
    .messages {
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      background: transparent;
    }

    .welcome { margin: auto 0; text-align: center; padding: 8px 4px; }
    .welcome__title { margin: 0 0 4px; font-size: 1.4rem; font-weight: 700; letter-spacing: -.01em; color: var(--teri-ink); }
    .welcome__hint { margin: 0 0 16px; font-size: .92rem; color: var(--teri-dim); }
    .cards { display: grid; grid-template-columns: 1fr; gap: 10px; }
    .card {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      text-align: left;
      background: var(--teri-bg);
      border: 1px solid var(--teri-border);
      border-radius: 12px;
      padding: 12px 13px;
      font: inherit;
      font-size: .82rem;
      color: var(--teri-ink);
      cursor: pointer;
      transition: border-color .12s ease, box-shadow .12s ease, transform .12s ease;
    }
    .card:hover { border-color: var(--teri-green); box-shadow: 0 4px 14px rgba(0,0,0,.07); transform: translateY(-1px); }
    .card strong { font-weight: 700; }
    .card__icon {
      width: 34px; height: 34px;
      border-radius: 9px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }
    .card__text { line-height: 1.4; }

    /* Expanded body = AI Sarthi look: soft gradient, big centred welcome, 3 cols. */
    :host(.expanded) .messages { background: linear-gradient(135deg, #e6f2ed 0%, #eef2fa 38%, #f6f0f7 70%, #fef5f2 100%); }
    :host(.expanded) .welcome { margin-top: 7vh; }
    :host(.expanded) .welcome__title { font-size: 2.25rem; margin-bottom: 8px; }
    :host(.expanded) .welcome__hint { font-size: 1.05rem; margin-bottom: 28px; }
    :host(.expanded) .cards {
      grid-template-columns: repeat(3, 1fr);
      gap: 14px;
      max-width: 1040px;
      margin: 0 auto;
    }
    :host(.expanded) .card { font-size: .9rem; padding: 16px; }
    :host(.expanded) .msg { max-width: 92%; }
    :host(.expanded) .msg--bot { max-width: 100%; }

    .msg { display: flex; flex-direction: column; max-width: 96%; }
    .msg--user { align-self: flex-end; align-items: flex-end; }
    /* Bot replies span the full column width; the user bubble stays sized to
       its content on the right. */
    .msg--bot { align-self: stretch; max-width: 100%; flex-direction: row; align-items: flex-start; gap: 10px; }
    .bubble {
      padding: 9px 13px;
      border-radius: var(--radius);
      white-space: pre-wrap;
      word-wrap: break-word;
    }
    .msg--user .bubble { background: var(--teri-user); color: #fff; border-bottom-right-radius: 4px; }
    .msg--bot .bubble { flex: 1; background: transparent; border: none; padding: 3px 0; min-width: 0; }

    /* AI avatar: a bare brand-green robot glyph (no disc), aligned to the first
       line of the reply. */
    .msg__avatar {
      flex-shrink: 0;
      width: 26px;
      height: 26px;
      margin-top: 1px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      color: var(--teri-green);
    }
    .msg__avatar svg { width: 22px; height: 22px; }
    .bubble--pending { color: var(--teri-dim); }
    .msg--bot .bubble--error {
      color: var(--teri-bad);
      border: 1px solid var(--teri-bad);
      border-radius: var(--radius);
      padding: 9px 13px;
    }

    /* ---- Working indicator: shimmering status word + bouncing dots ---- */
    .loader { display: inline-flex; align-items: center; gap: 9px; }
    .loader__text { display: inline-flex; }
    .loader__word {
      display: inline-block;
      font-weight: 600;
      background: linear-gradient(100deg,
        var(--teri-dim) 25%, var(--teri-green) 45%,
        var(--teri-green-dark) 55%, var(--teri-dim) 75%);
      background-size: 220% 100%;
      -webkit-background-clip: text;
              background-clip: text;
      -webkit-text-fill-color: transparent;
      color: transparent;
      animation: word-in .4s ease both, shimmer 2.2s linear infinite;
    }
    .loader__dots { display: inline-flex; align-items: center; gap: 3px; }
    .loader__dots i {
      width: 4px; height: 4px;
      border-radius: 50%;
      background: var(--teri-green);
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
    @media (prefers-reduced-motion: reduce) {
      .loader__word, .loader__dots i { animation: none; }
      .loader__word { -webkit-text-fill-color: var(--teri-green); color: var(--teri-green); }
    }

    .bubble p { margin: 0 0 .55rem; }
    .bubble > :last-child { margin-bottom: 0; }
    .bubble ul, .bubble ol { margin: 0 0 .55rem; padding-left: 1.25rem; }
    .bubble li { margin: .12rem 0; }
    .bubble h1, .bubble h2, .bubble h3, .bubble h4, .bubble h5, .bubble h6 {
      margin: .3rem 0 .35rem; font-size: 1.02em;
    }
    .bubble a { color: var(--teri-green-dark); }
    .bubble code {
      background: var(--teri-surface);
      border: 1px solid var(--teri-border);
      border-radius: 4px;
      padding: .05rem .3rem;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: .85em;
    }
    .bubble pre {
      background: var(--teri-surface);
      border: 1px solid var(--teri-border);
      border-radius: 8px;
      padding: .6rem;
      overflow-x: auto;
      margin: 0 0 .55rem;
    }
    .bubble pre code { background: none; border: none; padding: 0; }

    .bubble .table-wrap { overflow-x: auto; margin: 0 0 .55rem; }
    .bubble table { border-collapse: collapse; width: 100%; font-size: .86em; }
    .bubble th, .bubble td {
      border: 1px solid var(--teri-border);
      padding: 6px 10px;
      text-align: left;
      vertical-align: top;
    }
    .bubble thead th { background: var(--teri-surface); font-weight: 700; }
    .bubble tbody tr:nth-child(even) { background: rgba(0,0,0,.025); }

    /* ---- Answer blocks ---- */
    /* Website-sourced content is the answer proper, so it carries no container
       of its own — it reads as plain prose on the bubble. Only the supplementary
       PDF block is set apart, as a captioned card on the same surface + hairline
       the code blocks and citation chips use, so it reads as part of that family
       rather than a new device. */
    .answer-block--pdf {
      background: var(--teri-surface);
      border: 1px solid var(--teri-border);
      border-radius: 10px;
      padding: 11px 13px;
      margin: 2px 0 10px;
    }
    /* Caption: the uppercase micro-label already used for the source groups, so
       the panel is identified without competing with the answer's own headings. */
    .answer-block__label {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 7px;
      font-size: .66rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: .05em;
      color: var(--teri-dim);
    }
    .answer-block__label svg { width: 13px; height: 13px; flex-shrink: 0; }
    /* Markdown blocks carry their own bottom margin; drop the last one so the
       container's padding sets the gap. */
    .answer-block--pdf > :last-child { margin-bottom: 0; }

    /* ---- Inline citations ---- */
    /* A site-name pill after the claim it supports; its hover card carries
       the title and page. Muted so a cited paragraph still reads as prose, and
       brand green on hover. The .bubble prefix outranks ".bubble a". */
    .bubble .cite {
      display: inline-block;
      margin-left: 4px;
      padding: 0 7px;
      border: 1px solid var(--teri-border);
      border-radius: 999px;
      background: var(--teri-surface);
      color: var(--teri-dim);
      font-size: .7rem;
      font-weight: 500;
      line-height: 1.6;
      vertical-align: 1px;
      text-decoration: none;
      white-space: nowrap;
      transition: background .15s ease, border-color .15s ease, color .15s ease;
    }
    .bubble .cite:hover,
    .bubble .cite:focus-visible {
      background: var(--teri-green-soft);
      border-color: var(--teri-green);
      color: var(--teri-green-dark);
      outline: none;
    }

    /* Hover card: a white card lifted off the answer by a soft two-layer
       shadow. The site row names where the link goes, the title leads, and
       the page/section sits beneath it. Never takes the pointer, so moving
       across it cannot flicker it or swallow the chip's click. */
    .cite-pop {
      position: fixed;
      z-index: 10;
      width: max-content;
      max-width: 280px;
      padding: 10px 12px 11px;
      background: #fff;
      border: 1px solid var(--teri-border);
      border-radius: 12px;
      box-shadow: 0 12px 32px rgba(15, 23, 42, .14), 0 2px 6px rgba(15, 23, 42, .06);
      color: var(--teri-ink);
      font-size: .8rem;
      line-height: 1.4;
      pointer-events: none;
      opacity: 0;
      visibility: hidden;
      transform: translateY(4px);
      transition: opacity .14s ease, transform .14s ease, visibility 0s linear .14s;
    }
    .cite-pop[data-placement="below"] { transform: translateY(-4px); }
    .cite-pop.is-open {
      opacity: 1;
      visibility: visible;
      transform: none;
      transition-delay: 0s;
    }
    .cite-pop__site {
      display: flex;
      align-items: center;
      gap: 7px;
      color: var(--teri-dim);
      font-size: .72rem;
      font-weight: 500;
    }
    .cite-pop__icon {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 20px;
      height: 20px;
      flex-shrink: 0;
      border-radius: 6px;
      background: var(--teri-green-soft);
      color: var(--teri-green);
    }
    .cite-pop__icon svg { width: 12px; height: 12px; }
    .cite-pop__host { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .cite-pop__open { display: inline-flex; color: var(--teri-green); }
    .cite-pop__open svg { width: 13px; height: 13px; }
    .cite-pop__title {
      margin-top: 7px;
      font-size: .86rem;
      font-weight: 600;
      line-height: 1.35;
      display: -webkit-box;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 3;
      overflow: hidden;
    }
    .cite-pop__meta {
      margin-top: 4px;
      color: var(--teri-dim);
      font-size: .72rem;
    }
    @media (prefers-reduced-motion: reduce) {
      .cite-pop, .cite-pop[data-placement="below"] { transform: none; transition: none; }
    }

    /* Unverified-figures notice: the amber token, under the answer. */
    .answer-warn {
      margin-top: 6px;
      font-size: .78rem;
      color: var(--teri-warn);
      display: flex;
      gap: 6px;
      align-items: flex-start;
    }
    /* ---- Composer: a floating rounded box with the send button inside ---- */
    .composer {
      padding: 12px;
      background: transparent;
    }
    .composer__box {
      display: flex;
      gap: 8px;
      align-items: flex-end;
      width: 100%;
      background: #fff;
      border: 1px solid var(--teri-border);
      border-radius: 16px;
      box-shadow: 0 2px 12px rgba(0,0,0,.06);
      padding: 6px 6px 6px 14px;
    }
    .composer__box:focus-within { border-color: var(--teri-green); }
    .composer__input {
      flex: 1;
      resize: none;
      max-height: 120px;
      overflow-y: hidden;
      background: transparent;
      border: none;
      color: var(--teri-ink);
      padding: 8px 0;
      font: inherit;
      line-height: 1.4;
    }
    .composer__input:focus { outline: none; }
    .composer__send {
      flex-shrink: 0;
      width: 42px;
      height: 42px;
      border: none;
      border-radius: 50%;
      background: var(--teri-green);
      color: #fff;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }
    .composer__send:hover { background: var(--teri-green-dark); }
    .composer__send:disabled, .composer__send.busy { opacity: .55; cursor: not-allowed; }

    /* ---- Mobile: full screen ---- */
    @media (max-width: 480px) {
      .panel {
        right: 0; bottom: 0; left: 0; top: 0;
        width: 100%; max-width: 100%;
        height: 100%; max-height: 100%;
        border-radius: 0;
      }
      .launcher { right: 16px; bottom: 16px; }
      .cards, :host(.expanded) .cards { grid-template-columns: 1fr; }
      :host(.expanded) .welcome__title { font-size: 1.5rem; }
    }
    </style>`;
  }
})();
