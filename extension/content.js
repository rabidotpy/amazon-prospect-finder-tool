// Amazon Prospect Qualifier — content script
// Job: navigation only. It builds queries (links) it cannot get wrong, and
// for the one fuzzy output (website) it abstains instead of guessing.

const RESOLVER = "http://localhost:8765/resolve";
const BADGE_CLASS = "apq-badge";

// ---------- deterministic link builders (never wrong: they are queries) ----------

function metaAdLibraryLink(brand) {
  const q = encodeURIComponent(brand);
  // country=ALL => "All countries" per the chosen setting
  return (
    "https://www.facebook.com/ads/library/?active_status=all&ad_type=all" +
    "&country=ALL&q=" +
    q +
    "&search_type=keyword_unordered&media_type=all"
  );
}

function googleTransparencyByDomain(domain) {
  // region=anywhere => all countries
  return "https://adstransparency.google.com/?region=anywhere&domain=" + encodeURIComponent(domain);
}

const GOOGLE_TRANSPARENCY_HOME = "https://adstransparency.google.com/?region=anywhere";

// ---------- deterministic brand extraction from a product page DOM ----------

function brandFromDoc(doc) {
  const byline = doc.querySelector("#bylineInfo");
  if (byline) {
    const t = byline.textContent.replace(/\s+/g, " ").trim();
    let m = t.match(/Visit the (.+?) Store/i) || t.match(/Brand:\s*(.+)/i);
    if (m) return m[1].trim();
    // sometimes the byline is just the brand text
    if (t && !/^Visit/i.test(t) && t.length < 60) return t.replace(/^Brand:\s*/i, "").trim();
  }
  const rows = doc.querySelectorAll(
    "#productDetails_techSpec_section_1 tr, #productDetails_detailBullets_sections1 tr, #detailBullets_feature_div li, .a-expander-content tr"
  );
  for (const r of rows) {
    const txt = r.textContent.replace(/\s+/g, " ").trim();
    const m = txt.match(/Brand\s*[:‎‏]*\s*(.+)/i);
    if (m) {
      const v = m[1].trim();
      if (v && v.length < 60) return v;
    }
  }
  return null;
}

// ---------- find the product URL for a search-result card ----------

function productUrlFromCard(card) {
  const a =
    card.querySelector("h2 a.a-link-normal") ||
    card.querySelector("a.a-link-normal.s-no-outline") ||
    card.querySelector("h2 a") ||
    card.querySelector("a.a-link-normal[href*='/dp/']");
  if (!a) return null;
  try {
    return new URL(a.getAttribute("href"), location.origin).href;
  } catch {
    return null;
  }
}

// ---------- fetch a product page in the user's own session (no bot wall, no CORS) ----------

async function brandFromProductUrl(url) {
  const res = await fetch(url, { credentials: "include" });
  const html = await res.text();
  const doc = new DOMParser().parseFromString(html, "text/html");
  return brandFromDoc(doc);
}

// ---------- the result panel (fixed, bottom-right) ----------

function ensurePanel() {
  let p = document.getElementById("apq-panel");
  if (p) return p;
  p = document.createElement("div");
  p.id = "apq-panel";
  p.innerHTML = `
    <div class="apq-panel-head">
      <span>Prospect Qualifier</span>
      <button id="apq-close" title="Close">&times;</button>
    </div>
    <div id="apq-body"></div>`;
  document.body.appendChild(p);
  p.querySelector("#apq-close").addEventListener("click", () => (p.style.display = "none"));
  return p;
}

function row(label, href, sublabel) {
  if (!href) {
    return `<div class="apq-row apq-unknown"><span class="apq-k">${label}</span><span class="apq-v">unknown${
      sublabel ? ` — ${sublabel}` : ""
    }</span></div>`;
  }
  return `<div class="apq-row"><span class="apq-k">${label}</span><a class="apq-v" href="${href}" target="_blank" rel="noopener">${
    sublabel || href
  }</a></div>`;
}

function renderPanel(state) {
  const panel = ensurePanel();
  panel.style.display = "block";
  const body = panel.querySelector("#apq-body");
  const { brand, metaLink, website, websiteSource, googleLink, status } = state;

  let googleRowHtml;
  if (googleLink) {
    googleRowHtml = row("Google ads", googleLink, "open transparency center");
  } else if (status === "resolving") {
    googleRowHtml = `<div class="apq-row"><span class="apq-k">Google ads</span><span class="apq-v apq-dim">resolving…</span></div>`;
  } else {
    googleRowHtml = row("Google ads", GOOGLE_TRANSPARENCY_HOME, "no domain — search by name");
  }

  let websiteRowHtml;
  if (website) {
    websiteRowHtml = row("Website", "https://" + website, website + (websiteSource ? ` (${websiteSource})` : ""));
  } else if (status === "resolving") {
    websiteRowHtml = `<div class="apq-row"><span class="apq-k">Website</span><span class="apq-v apq-dim">resolving…</span></div>`;
  } else {
    // abstained — hand off to Claude Code desktop in-chat
    websiteRowHtml = `<div class="apq-row apq-unknown">
        <span class="apq-k">Website</span>
        <span class="apq-v">unknown — <button id="apq-ai" class="apq-link-btn">copy prompt for Claude Code</button></span>
      </div>`;
  }

  body.innerHTML = `
    <div class="apq-brand">${brand ? brand : '<span class="apq-dim">brand not detected</span>'}</div>
    ${row("Meta ads", metaLink, "open ad library")}
    ${websiteRowHtml}
    ${googleRowHtml}
    ${status === "error" ? '<div class="apq-err">resolver offline — Meta link still works. Start resolver.py.</div>' : ""}`;

  const aiBtn = body.querySelector("#apq-ai");
  if (aiBtn) {
    aiBtn.addEventListener("click", () => {
      const prompt =
        `Find the official direct-to-consumer website for the brand "${brand}" ` +
        `(a product/supplement brand sold on Amazon). Use web search to verify. ` +
        `Reply with ONLY the root domain (e.g. example.com). ` +
        `If you are not highly confident, reply exactly UNKNOWN. Do not guess.`;
      navigator.clipboard.writeText(prompt).then(() => {
        aiBtn.textContent = "copied — paste into Claude Code";
      });
    });
  }
}

// ---------- main processing flow ----------

async function processListing({ card }) {
  renderPanel({ brand: null, metaLink: null, status: "resolving" });

  // 1) brand — deterministic. On a product page read the DOM; on a card, fetch the product page.
  let brand = null;
  if (location.pathname.includes("/dp/") || location.pathname.includes("/gp/product/")) {
    brand = brandFromDoc(document);
  }
  if (!brand && card) {
    const url = productUrlFromCard(card);
    if (url) {
      try {
        brand = await brandFromProductUrl(url);
      } catch (e) {
        // network/session issue — fall through to manual entry
      }
    }
  }
  if (!brand) {
    brand = window.prompt("Brand not auto-detected. Type the brand name (or cancel to skip):");
    if (!brand) {
      renderPanel({ brand: null, metaLink: null, status: "idle" });
      return;
    }
    brand = brand.trim();
  }

  // 2) Meta link — instant, deterministic.
  const metaLink = metaAdLibraryLink(brand);
  renderPanel({ brand, metaLink, status: "resolving" });

  // 3) website + google — ask the local resolver (deterministic, AI only as fallback).
  try {
    const res = await fetch(RESOLVER, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ brand }),
    });
    const data = await res.json();
    const website = data.website || null; // resolver returns null when not confident
    const googleLink = website ? googleTransparencyByDomain(website) : null;
    renderPanel({
      brand,
      metaLink,
      website,
      websiteSource: data.source,
      googleLink,
      status: "done",
    });
  } catch (e) {
    renderPanel({ brand, metaLink, website: null, googleLink: null, status: "error" });
  }
}

// ---------- hover badges on search-result cards ----------

function attachBadge(card) {
  if (card.querySelector("." + BADGE_CLASS)) return;
  const badge = document.createElement("button");
  badge.className = BADGE_CLASS;
  badge.textContent = "Process";
  badge.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    processListing({ card });
  });
  card.style.position = card.style.position || "relative";
  card.appendChild(badge);
}

function scanCards() {
  document
    .querySelectorAll('div[data-component-type="s-search-result"]')
    .forEach((card) => {
      card.addEventListener("mouseenter", () => attachBadge(card), { once: true });
    });
}

// product-page floating button
function attachProductPageButton() {
  if (!(location.pathname.includes("/dp/") || location.pathname.includes("/gp/product/"))) return;
  if (document.getElementById("apq-product-btn")) return;
  const btn = document.createElement("button");
  btn.id = "apq-product-btn";
  btn.textContent = "Process this product";
  btn.addEventListener("click", () => processListing({ card: null }));
  document.body.appendChild(btn);
}

// re-scan as Amazon lazy-loads / paginates
const mo = new MutationObserver(() => {
  scanCards();
  attachProductPageButton();
});
mo.observe(document.documentElement, { childList: true, subtree: true });

scanCards();
attachProductPageButton();
