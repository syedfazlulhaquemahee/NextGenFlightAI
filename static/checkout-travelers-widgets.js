(function () {
  const flatpickrLib = typeof window.flatpickr === "function" ? window.flatpickr : null;
  const form = document.getElementById("premiumCheckoutForm");
  const widgetsScript = document.querySelector("script[data-dial-codes]");
  const dialCodesUrl = widgetsScript?.dataset?.dialCodes || "/static/dial-codes.json";

  const prevArrow = [
    "<svg viewBox=\"0 0 20 20\" aria-hidden=\"true\" focusable=\"false\">",
    "<path d=\"M11.75 4.75 6.5 10l5.25 5.25\" fill=\"none\" stroke=\"currentColor\" stroke-linecap=\"round\" stroke-linejoin=\"round\" stroke-width=\"1.8\"/>",
    "</svg>",
  ].join("");

  const nextArrow = [
    "<svg viewBox=\"0 0 20 20\" aria-hidden=\"true\" focusable=\"false\">",
    "<path d=\"m8.25 4.75 5.25 5.25-5.25 5.25\" fill=\"none\" stroke=\"currentColor\" stroke-linecap=\"round\" stroke-linejoin=\"round\" stroke-width=\"1.8\"/>",
    "</svg>",
  ].join("");

  const POP_ORDER = [
    "US", "GB", "CA", "AU", "DE", "FR", "IN", "ES", "IT", "NL", "IE", "NZ", "SG", "AE", "JP", "KR",
    "BR", "MX", "ZA", "CH", "AT", "PL", "PT", "SE", "NO", "DK", "FI", "IL", "TR", "PH", "MY", "ID",
    "TH", "VN", "CN", "HK", "TW", "CZ", "RO", "HU", "GR",
  ];

  function clampCalendarToViewport(picker) {
    if (!picker || !picker.calendarContainer) return;
    window.requestAnimationFrame(() => {
      const calendar = picker.calendarContainer;
      const rect = calendar.getBoundingClientRect();
      const width = rect.width;
      const currentLeft = Number.parseFloat(calendar.style.left || "");
      if (!Number.isFinite(currentLeft) || width <= 0) return;
      const pad = 12;
      const minLeft = window.scrollX + pad;
      const maxLeft = Math.max(minLeft, window.scrollX + window.innerWidth - width - pad);
      const clamped = Math.min(maxLeft, Math.max(minLeft, currentLeft));
      if (Math.abs(clamped - currentLeft) > 0.5) calendar.style.left = `${clamped}px`;
    });
  }

  function birthDateBounds() {
    const max = new Date();
    max.setDate(max.getDate() - 1);
    max.setHours(12, 0, 0, 0);
    const min = new Date();
    min.setFullYear(min.getFullYear() - 120);
    min.setHours(12, 0, 0, 0);
    return { min, max };
  }

  /**
   * Size month <select> to the **currently selected** option label only (not the longest option in the list).
   * Uses !important so it wins over global flatpickr/checkout rules; shrinks again when a shorter month is chosen.
   */
  function fitCheckoutDobMonthSelect(inst) {
    const cal = inst?.calendarContainer;
    if (!cal?.classList.contains("checkout-dob-calendar")) return;
    const row = cal.querySelector(".flatpickr-current-month");
    const sel = cal.querySelector(".flatpickr-monthDropdown-months");
    const yearWrap = row?.querySelector(".numInputWrapper");
    if (!row || !sel || !yearWrap) return;

    const opt = sel.options[sel.selectedIndex];
    const label = (opt && opt.textContent) ? opt.textContent.trim() : "";
    const cs = window.getComputedStyle(sel);
    const probe = document.createElement("span");
    probe.setAttribute("aria-hidden", "true");
    probe.style.cssText = [
      "position:fixed",
      "left:-9999px",
      "top:0",
      "white-space:nowrap",
      "visibility:hidden",
      "pointer-events:none",
      `font-family:${cs.fontFamily}`,
      `font-size:${cs.fontSize}`,
      `font-weight:${cs.fontWeight}`,
      `font-style:${cs.fontStyle}`,
      `letter-spacing:${cs.letterSpacing}`,
    ].join(";");
    probe.textContent = label || "—";
    document.body.appendChild(probe);
    const textW = probe.offsetWidth;
    probe.remove();

    const padL = Number.parseFloat(cs.paddingLeft) || 0;
    const padR = Number.parseFloat(cs.paddingRight) || 0;
    const desired = Math.ceil(textW + padL + padR + 2);

    const gap = 6;
    const yearW = yearWrap.offsetWidth || 88;
    const maxMonth = Math.floor(row.clientWidth - yearW - gap);
    const absMin = 48;
    const cap = Math.max(absMin, maxMonth);
    const w = Math.min(Math.max(absMin, desired), cap);
    sel.style.setProperty("width", `${w}px`, "important");
    sel.style.setProperty("max-width", `${cap}px`, "important");
  }

  function scheduleFitCheckoutDobMonth(inst) {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => fitCheckoutDobMonthSelect(inst));
    });
  }

  function initDobPickers() {
    if (!flatpickrLib) return;
    const inputs = document.querySelectorAll("input[data-checkout-dob]");
    const { min, max } = birthDateBounds();

    inputs.forEach((input) => {
      if (input._nxDobFp) return;
      const invalid = input.classList.contains("field-invalid");
      const rawVal = (input.value || "").trim();
      const defaultDate = /^\d{4}-\d{2}-\d{2}$/.test(rawVal) ? rawVal : undefined;

      const fp = flatpickrLib(input, {
        dateFormat: "Y-m-d",
        altInput: true,
        altFormat: "F j, Y",
        altInputClass: `checkout-dob-display ${invalid ? "field-invalid" : ""}`.trim(),
        allowInput: false,
        clickOpens: true,
        disableMobile: true,
        monthSelectorType: "dropdown",
        minDate: min,
        maxDate: max,
        defaultDate,
        appendTo: document.body,
        prevArrow,
        nextArrow,
        onReady(_d, _s, inst) {
          inst.calendarContainer.classList.add("checkout-dob-calendar");
          if (inst.altInput) {
            inst.config.positionElement = inst.altInput;
            const labelId = input.id;
            if (labelId) {
              input.removeAttribute("id");
              inst.altInput.id = labelId;
            }
            inst.altInput.setAttribute("aria-label", "Date of birth");
            inst.altInput.placeholder = "Select date of birth";
          }
          const cal = inst.calendarContainer;
          if (!inst._nxDobMonthChangeDelegation) {
            inst._nxDobMonthChangeDelegation = true;
            cal.addEventListener("change", (e) => {
              const t = e.target;
              if (t && t.classList && t.classList.contains("flatpickr-monthDropdown-months")) {
                scheduleFitCheckoutDobMonth(inst);
              }
            });
          }
          if (!inst._nxDobMonthResizeBound) {
            inst._nxDobMonthResizeBound = true;
            window.addEventListener(
              "resize",
              () => {
                if (inst.isOpen) scheduleFitCheckoutDobMonth(inst);
              },
              { passive: true },
            );
          }
          fitCheckoutDobMonthSelect(inst);
          scheduleFitCheckoutDobMonth(inst);
          clampCalendarToViewport(inst);
        },
        onOpen(_d, _s, inst) {
          inst.calendarContainer.classList.add("checkout-dob-calendar");
          if (inst.altInput) inst.config.positionElement = inst.altInput;
          fitCheckoutDobMonthSelect(inst);
          scheduleFitCheckoutDobMonth(inst);
          clampCalendarToViewport(inst);
        },
        onMonthChange(_d, _s, inst) {
          scheduleFitCheckoutDobMonth(inst);
        },
        onYearChange(_d, _s, inst) {
          scheduleFitCheckoutDobMonth(inst);
        },
        onChange(_d, _s, inst) {
          input.classList.remove("field-invalid");
          if (inst.altInput) inst.altInput.classList.remove("field-invalid");
          scheduleFitCheckoutDobMonth(inst);
        },
      });

      input._nxDobFp = fp;
    });
  }

  function flagEmoji(iso2) {
    if (!iso2 || iso2.length !== 2) return "🏳️";
    const A = 0x1f1e6;
    const up = iso2.toUpperCase();
    const c0 = up.charCodeAt(0);
    const c1 = up.charCodeAt(1);
    if (c0 < 65 || c0 > 90 || c1 < 65 || c1 > 90) return "🏳️";
    return String.fromCodePoint(A + (c0 - 65), A + (c1 - 65));
  }

  function onlyDigits(s) {
    return (s || "").replace(/\D/g, "");
  }

  function sortForMatching(entries) {
    return [...entries].sort((a, b) => {
      const ld = b.dial.length - a.dial.length;
      if (ld !== 0) return ld;
      const ia = POP_ORDER.indexOf(a.iso);
      const ib = POP_ORDER.indexOf(b.iso);
      if (ia !== -1 || ib !== -1) {
        if (ia === -1) return 1;
        if (ib === -1) return -1;
        if (ia !== ib) return ia - ib;
      }
      return a.iso.localeCompare(b.iso);
    });
  }

  function sortForDisplay(entries) {
    return [...entries].sort((a, b) => {
      const ia = POP_ORDER.indexOf(a.iso);
      const ib = POP_ORDER.indexOf(b.iso);
      if (ia !== -1 || ib !== -1) {
        if (ia === -1) return 1;
        if (ib === -1) return -1;
        if (ia !== ib) return ia - ib;
      }
      return a.name.localeCompare(b.name);
    });
  }

  function matchCountryFromE164(digitsNoPlus, sortedMatch) {
    for (let i = 0; i < sortedMatch.length; i += 1) {
      const c = sortedMatch[i];
      if (digitsNoPlus.startsWith(c.dial)) return c;
    }
    return null;
  }

  function defaultCountryFromLocale(entries) {
    try {
      const loc = new Intl.Locale(navigator.language || "en-US");
      const region = loc.region;
      if (region) {
        const hit = entries.find((e) => e.iso === region);
        if (hit) return hit;
      }
    } catch (_e) {
      /* ignore */
    }
    return entries.find((e) => e.iso === "US") || entries[0];
  }

  function validateE164Digits(allDigits) {
    return allDigits.length >= 8 && allDigits.length <= 15;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function escapeRegex(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function highlightQuery(text, query) {
    const esc = escapeHtml(text);
    const tokens = [...new Set(query.trim().toLowerCase().split(/\s+/).filter(Boolean))];
    if (!tokens.length) return esc;
    tokens.sort((a, b) => b.length - a.length);
    const body = tokens.map(escapeRegex).join("|");
    if (!body) return esc;
    return esc.replace(new RegExp(`(${body})`, "gi"), '<mark class="checkout-phone-match">$1</mark>');
  }

  function rowMatchesQuery(c, query) {
    const q = query.trim().toLowerCase();
    if (!q) return true;
    const dialPlus = `+${c.dial}`;
    const hay = `${c.name} ${c.iso} ${c.dial} ${dialPlus}`.toLowerCase();
    const parts = q.split(/\s+/).filter(Boolean).filter((p) => p !== "+");
    if (!parts.length) return true;
    return parts.every((p) => hay.includes(p));
  }

  function buildPhoneRow(root, sortedMatch, displayList) {
    const hidden = root.querySelector("[data-phone-hidden]");
    const national = root.querySelector("[data-phone-national]");
    const btn = root.querySelector("[data-phone-country-btn]");
    const flagEl = root.querySelector("[data-phone-flag]");
    const isoEl = root.querySelector("[data-phone-iso]");
    const dialEl = root.querySelector("[data-phone-dial]");
    const dropdown = root.querySelector("[data-phone-dropdown]");
    const search = root.querySelector("[data-phone-search]");
    const list = root.querySelector("[data-phone-list]");
    if (!hidden || !national || !btn || !flagEl || !dialEl || !dropdown || !search || !list) return;

    document.body.appendChild(dropdown);
    dropdown.classList.add("checkout-phone-dropdown--floating");
    root._nxPhoneDropdown = dropdown;
    dropdown.hidden = true;
    dropdown.setAttribute("hidden", "");
    const composite = root.querySelector(".checkout-phone-row--composite");

    let selected = defaultCountryFromLocale(displayList);
    let keyboardActive = null;
    let viewportListener = null;

    function repositionDropdown() {
      if (!root.classList.contains("is-open")) return;
      const rect = btn.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const margin = 6;
      const edge = 10;
      const narrow = vw <= 560;

      dropdown.style.zIndex = "3000";

      if (narrow) {
        dropdown.classList.add("checkout-phone-dropdown--sheet");
        const sheetH = Math.min(268, Math.floor(vh * 0.42));
        const bottomGap = 12;
        dropdown.style.position = "fixed";
        dropdown.style.left = `${edge}px`;
        dropdown.style.width = `${vw - edge * 2}px`;
        dropdown.style.top = `${vh - sheetH - bottomGap}px`;
        dropdown.style.maxHeight = `${sheetH}px`;
        return;
      }

      dropdown.classList.remove("checkout-phone-dropdown--sheet");
      const w = Math.min(200, Math.max(156, Math.ceil(rect.width + 24)));
      let left = rect.left;
      left = Math.min(left, vw - w - edge);
      left = Math.max(edge, left);

      const desiredMax = 210;
      const minUsable = 88;
      const spaceBelow = vh - rect.bottom - margin - edge;
      const spaceAbove = rect.top - margin - edge;
      let top;
      let maxH;

      if (spaceBelow >= minUsable) {
        top = rect.bottom + margin;
        maxH = Math.min(desiredMax, spaceBelow);
      } else if (spaceAbove >= minUsable) {
        maxH = Math.min(desiredMax, spaceAbove);
        top = rect.top - margin - maxH;
        top = Math.max(edge, top);
      } else {
        top = rect.bottom + margin;
        maxH = Math.max(72, spaceBelow);
      }

      dropdown.style.position = "fixed";
      dropdown.style.left = `${left}px`;
      dropdown.style.top = `${top}px`;
      dropdown.style.width = `${w}px`;
      dropdown.style.maxHeight = `${maxH}px`;
    }

    const raw = (hidden.value || "").trim();
    const digits = onlyDigits(raw.startsWith("+") ? raw.slice(1) : raw);
    if (digits.length >= 8) {
      const hit = matchCountryFromE164(digits, sortedMatch);
      if (hit) {
        selected = hit;
        national.value = digits.slice(hit.dial.length);
      }
    }

    function visibleOptions() {
      return Array.from(list.querySelectorAll('[role="option"]'));
    }

    function setKeyboardActive(li) {
      visibleOptions().forEach((n) => n.classList.remove("checkout-phone-option--keyboard"));
      keyboardActive = li;
      if (li) {
        li.classList.add("checkout-phone-option--keyboard");
        li.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    }

    function renderList(filter) {
      const frag = document.createDocumentFragment();
      displayList.forEach((c) => {
        if (!rowMatchesQuery(c, filter)) return;
        const li = document.createElement("li");
        li.setAttribute("role", "option");
        li.tabIndex = -1;
        li.dataset.iso = c.iso;
        li.className = "checkout-phone-option";
        if (selected && c.iso === selected.iso) li.setAttribute("aria-selected", "true");
        const flagSpan = document.createElement("span");
        flagSpan.className = "checkout-phone-option-flag";
        flagSpan.textContent = flagEmoji(c.iso);
        const isoSpan = document.createElement("span");
        isoSpan.className = "checkout-phone-option-iso";
        isoSpan.innerHTML = highlightQuery(c.iso, filter);
        const dialSpan = document.createElement("span");
        dialSpan.className = "checkout-phone-option-dial";
        dialSpan.innerHTML = highlightQuery(`+${c.dial}`, filter);
        li.append(flagSpan, isoSpan, dialSpan);
        li.title = `${c.name} +${c.dial}`;
        li.setAttribute("aria-label", `${c.name}, ${c.iso}, +${c.dial}`);
        frag.appendChild(li);
      });
      list.replaceChildren(frag);
      setKeyboardActive(null);
    }

    function applySelected(c) {
      selected = c;
      flagEl.textContent = flagEmoji(c.iso);
      if (isoEl) isoEl.textContent = c.iso;
      dialEl.textContent = `+${c.dial}`;
      btn.setAttribute("title", `${c.name} +${c.dial}`);
      root.dataset.phoneDial = c.dial;
      syncHidden();
      renderList(search.value);
    }

    function syncHidden() {
      if (!selected) return;
      const nat = onlyDigits(national.value);
      const all = `${selected.dial}${nat}`;
      hidden.value = nat ? `+${all}` : "";
      if (!validateE164Digits(all)) {
        national.setCustomValidity(
          nat ? "Enter a valid phone number including country code (8–15 digits total)." : "",
        );
      } else {
        national.setCustomValidity("");
      }
      if (composite) {
        const bad =
          root.classList.contains("checkout-phone-field--error") ||
          national.classList.contains("field-invalid") ||
          (!!national.validationMessage && onlyDigits(national.value).length > 0);
        composite.classList.toggle("checkout-phone-row--invalid", !!bad);
      }
    }

    function openDropdown() {
      document.querySelectorAll(".checkout-phone-field.is-open").forEach((el) => {
        if (el === root) return;
        closeOther(el);
        const otherL = el._nxPhoneViewportListener;
        if (otherL) {
          window.removeEventListener("scroll", otherL, true);
          window.removeEventListener("resize", otherL);
          el._nxPhoneViewportListener = null;
        }
      });
      root.classList.add("is-open");
      btn.setAttribute("aria-expanded", "true");
      dropdown.hidden = false;
      dropdown.removeAttribute("hidden");
      search.value = "";
      renderList("");
      repositionDropdown();
      viewportListener = () => repositionDropdown();
      root._nxPhoneViewportListener = viewportListener;
      window.addEventListener("scroll", viewportListener, true);
      window.addEventListener("resize", viewportListener);
      window.setTimeout(() => {
        search.focus();
        repositionDropdown();
      }, 10);
    }

    function closeDropdown() {
      root.classList.remove("is-open");
      btn.setAttribute("aria-expanded", "false");
      dropdown.hidden = true;
      dropdown.setAttribute("hidden", "");
      search.value = "";
      setKeyboardActive(null);
      if (viewportListener) {
        window.removeEventListener("scroll", viewportListener, true);
        window.removeEventListener("resize", viewportListener);
        viewportListener = null;
      }
      root._nxPhoneViewportListener = null;
    }

    function closeOther(el) {
      el.classList.remove("is-open");
      const b = el.querySelector("[data-phone-country-btn]");
      const d = el._nxPhoneDropdown;
      if (b) b.setAttribute("aria-expanded", "false");
      if (d) {
        d.hidden = true;
        d.setAttribute("hidden", "");
      }
    }

    applySelected(selected);
    national.addEventListener("input", syncHidden);
    national.addEventListener("blur", syncHidden);
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      if (root.classList.contains("is-open")) closeDropdown();
      else openDropdown();
    });

    search.addEventListener("input", () => {
      renderList(search.value);
      repositionDropdown();
    });

    search.addEventListener("keydown", (e) => {
      if (!root.classList.contains("is-open")) return;
      const opts = visibleOptions();
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (!opts.length) return;
        const i = keyboardActive ? opts.indexOf(keyboardActive) : -1;
        setKeyboardActive(opts[Math.min(i + 1, opts.length - 1)]);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (!opts.length) return;
        const i = keyboardActive ? opts.indexOf(keyboardActive) : opts.length;
        setKeyboardActive(opts[Math.max(i - 1, 0)]);
      } else if (e.key === "Enter" && keyboardActive) {
        e.preventDefault();
        const iso = keyboardActive.dataset.iso;
        const c = displayList.find((x) => x.iso === iso);
        if (c) applySelected(c);
        closeDropdown();
        national.focus();
      }
    });

    list.addEventListener("mousemove", () => setKeyboardActive(null));

    list.addEventListener("click", (e) => {
      const li = e.target.closest("[role='option']");
      if (!li || !list.contains(li)) return;
      const iso = li.dataset.iso;
      const c = displayList.find((x) => x.iso === iso);
      if (c) applySelected(c);
      closeDropdown();
      national.focus();
    });

    document.addEventListener(
      "mousedown",
      (e) => {
        if (!root.classList.contains("is-open")) return;
        const t = e.target;
        if (root.contains(t) || dropdown.contains(t)) return;
        closeDropdown();
      },
      true,
    );

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && root.classList.contains("is-open")) {
        closeDropdown();
        btn.focus();
      }
    });
  }

  function initPhoneFields(entries) {
    if (!entries || !entries.length) return;
    const sortedMatch = sortForMatching(entries);
    const displayList = sortForDisplay(entries);

    document.querySelectorAll("[data-checkout-phone]").forEach((root) => {
      if (root.dataset.phoneInit === "1") return;
      root.dataset.phoneInit = "1";
      buildPhoneRow(root, sortedMatch, displayList);
    });
  }

  function syncAllPhones() {
    document.querySelectorAll("[data-checkout-phone]").forEach((root) => {
      const national = root.querySelector("[data-phone-national]");
      const hidden = root.querySelector("[data-phone-hidden]");
      if (!national || !hidden) return;
      national.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  if (form) {
    form.addEventListener(
      "submit",
      (ev) => {
        syncAllPhones();
        const nationals = form.querySelectorAll("[data-phone-national]");
        for (let i = 0; i < nationals.length; i += 1) {
          const nat = nationals[i];
          if (!nat.checkValidity()) {
            ev.preventDefault();
            nat.reportValidity();
            return;
          }
        }
      },
      true,
    );
  }

  initDobPickers();

  fetch(dialCodesUrl, { credentials: "same-origin" })
    .then((r) => {
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    })
    .then((data) => {
      if (!Array.isArray(data)) return;
      const entries = data
        .map((row) => ({
          iso: String(row.iso || "").toUpperCase(),
          dial: String(row.dial || "").replace(/\D/g, ""),
          name: String(row.name || "").trim(),
        }))
        .filter((row) => row.iso.length === 2 && row.dial.length >= 1 && row.name);
      initPhoneFields(entries);
    })
    .catch(() => {
      initPhoneFields([
        { iso: "US", dial: "1", name: "United States" },
        { iso: "GB", dial: "44", name: "United Kingdom" },
        { iso: "CA", dial: "1", name: "Canada" },
      ]);
    });
})();
