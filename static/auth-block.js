/* Auth block behaviors — shared by the header sign-in <dialog> and the /auth
   fallback page. One init per [data-auth-root]: view switching (login /
   signup / reset steps), password visibility + strength + match, OTP boxes,
   email validation, submit spinners. Plus the modal open/close wiring for
   [data-auth-open] triggers anywhere on the page. */
(function () {
  "use strict";

  var COPY = {
    login: {
      title: "Welcome back",
      sub: "Sign in to pick up where you left off",
      footText: "Don’t have an account?",
      footLabel: "Create one",
      footGoto: "signup"
    },
    signup: {
      title: "Create your account",
      sub: "Book faster, track prices, and manage every trip in one place",
      footText: "Already have an account?",
      footLabel: "Sign in",
      footGoto: "login"
    },
    reset_request: {
      title: "Reset your password",
      sub: "We’ll email you a 6-digit verification code",
      footText: "Remembered it?",
      footLabel: "Back to sign in",
      footGoto: "login"
    },
    reset_verify: {
      title: "Check your email",
      sub: "Enter the 6-digit code we sent you",
      footText: "Remembered it?",
      footLabel: "Back to sign in",
      footGoto: "login"
    },
    reset_complete: {
      title: "Set a new password",
      sub: "Choose a strong password you haven’t used before",
      footText: "Remembered it?",
      footLabel: "Back to sign in",
      footGoto: "login"
    }
  };

  function initAuthRoot(root) {
    var views = root.querySelectorAll("[data-auth-view]");
    var titleEl = root.querySelector("[data-ab-title]");
    var subEl = root.querySelector("[data-ab-sub]");
    var footText = root.querySelector("[data-ab-foot-text]");
    var footLink = root.querySelector("[data-ab-foot-link]");
    var current = null;

    function setView(name) {
      if (!COPY[name]) name = "login";
      current = name;
      views.forEach(function (view) {
        view.hidden = view.dataset.authView !== name;
      });
      var copy = COPY[name];
      if (titleEl) titleEl.textContent = copy.title;
      if (subEl) subEl.textContent = copy.sub;
      if (footText) footText.textContent = copy.footText;
      if (footLink) {
        footLink.textContent = copy.footLabel;
        footLink.dataset.abGoto = copy.footGoto;
      }
      if (name === "reset_verify") {
        window.setTimeout(function () {
          root.querySelector('[data-auth-view="reset_verify"] .ab-otp-box')?.focus();
        }, 60);
      }
    }
    root.SKAIR_SET_AUTH_VIEW = setView;

    // Keep the two reset email fields in sync with whatever the user last typed.
    function seedResetEmail() {
      var source =
        root.querySelector("#abLoginEmail")?.value?.trim() ||
        root.querySelector("[data-ab-reset-email]")?.value?.trim() || "";
      if (!source) return;
      root.querySelectorAll("[data-ab-reset-email]").forEach(function (input) {
        if (!input.value.trim()) input.value = source;
      });
    }

    root.addEventListener("click", function (event) {
      var goTo = event.target.closest("[data-ab-goto]");
      if (goTo && root.contains(goTo)) {
        seedResetEmail();
        setView(goTo.dataset.abGoto);
      }
    });

    /* Password visibility */
    root.querySelectorAll("[data-ab-eye]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var input = root.querySelector("#" + btn.dataset.abEye);
        if (!input) return;
        var show = input.type === "password";
        input.type = show ? "text" : "password";
        btn.setAttribute("aria-pressed", show ? "true" : "false");
      });
    });

    /* Password strength */
    function score(pw) {
      var s = 0;
      if (pw.length >= 8) s++;
      if (pw.length >= 12) s++;
      if (/[A-Z]/.test(pw) && /[a-z]/.test(pw)) s++;
      if (/\d/.test(pw)) s++;
      if (/[^A-Za-z0-9]/.test(pw)) s++;
      return Math.max(1, Math.min(4, Math.floor((s * 4) / 5)));
    }
    var STRENGTH_WORDS = ["", "Weak", "Fair", "Good", "Strong"];
    root.querySelectorAll("[data-ab-strength]").forEach(function (input) {
      var meter = input.closest(".ab-field")?.querySelector(".ab-strength");
      if (!meter) return;
      var label = meter.querySelector(".ab-strength-label");
      input.addEventListener("input", function () {
        var pw = input.value;
        meter.hidden = !pw.length;
        var n = pw.length ? score(pw) : 0;
        meter.setAttribute("data-score", String(n));
        if (label) label.textContent = STRENGTH_WORDS[n] || "";
      });
    });

    /* Confirm-password match */
    root.querySelectorAll("[data-ab-match]").forEach(function (confirm) {
      var source = root.querySelector("#" + confirm.dataset.abMatch);
      var hint = confirm.closest(".ab-field")?.querySelector("[data-ab-match-hint]");
      if (!source || !hint) return;
      function check() {
        if (!confirm.value) { hint.hidden = true; hint.className = "ab-hint"; return; }
        var ok = source.value === confirm.value;
        hint.hidden = false;
        hint.textContent = ok ? "Passwords match" : "Passwords do not match";
        hint.className = "ab-hint " + (ok ? "ab-hint--ok" : "ab-hint--err");
      }
      source.addEventListener("input", check);
      confirm.addEventListener("input", check);
    });

    /* Email format nudge on blur */
    root.querySelectorAll("[data-ab-email]").forEach(function (input) {
      input.addEventListener("blur", function () {
        if (!input.value) { input.classList.remove("ab-invalid"); return; }
        input.classList.toggle("ab-invalid", !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(input.value));
      });
      input.addEventListener("focus", function () { input.classList.remove("ab-invalid"); });
    });

    /* OTP boxes → hidden verification_code */
    var otpWrap = root.querySelector("[data-ab-otp]");
    if (otpWrap) {
      var boxes = Array.prototype.slice.call(otpWrap.querySelectorAll(".ab-otp-box"));
      var hiddenValue = root.querySelector("[data-ab-otp-value]");
      var sync = function () {
        if (hiddenValue) hiddenValue.value = boxes.map(function (b) { return b.value; }).join("");
        boxes.forEach(function (b) { b.classList.toggle("is-filled", b.value.length > 0); });
      };
      boxes.forEach(function (box, idx) {
        box.addEventListener("input", function () {
          box.value = box.value.replace(/\D/g, "").slice(-1);
          sync();
          if (box.value && idx < boxes.length - 1) boxes[idx + 1].focus();
        });
        box.addEventListener("keydown", function (event) {
          if (event.key === "Backspace" && !box.value && idx > 0) {
            boxes[idx - 1].focus();
            boxes[idx - 1].value = "";
            sync();
          }
          if (event.key === "ArrowLeft" && idx > 0) { event.preventDefault(); boxes[idx - 1].focus(); }
          if (event.key === "ArrowRight" && idx < boxes.length - 1) { event.preventDefault(); boxes[idx + 1].focus(); }
        });
        box.addEventListener("paste", function (event) {
          event.preventDefault();
          var text = (event.clipboardData || window.clipboardData).getData("text").replace(/\D/g, "").slice(0, 6);
          text.split("").forEach(function (ch, i) { if (boxes[i]) boxes[i].value = ch; });
          sync();
          boxes[Math.min(text.length, boxes.length - 1)]?.focus();
        });
      });

      // Sandbox flows print "demo code: 123456" in the alert — auto-fill it.
      var alerts = root.querySelectorAll("[data-ab-alert]");
      for (var i = 0; i < alerts.length; i++) {
        var match = String(alerts[i].textContent || "").match(/demo code:\s*(\d{6})/i);
        if (match) {
          match[1].split("").forEach(function (ch, j) { if (boxes[j]) boxes[j].value = ch; });
          sync();
          break;
        }
      }
    }

    /* Submit spinner */
    root.querySelectorAll("form").forEach(function (form) {
      form.addEventListener("submit", function () {
        form.querySelector(".ab-submit")?.classList.add("is-loading");
      });
    });

    /* Carry booking_reference/next onto OAuth links; shake once on a server error */
    var bookingRef = root.dataset.bookingReference || "";
    var nextUrl = root.dataset.next || "";
    if (bookingRef || nextUrl) {
      root.querySelectorAll("[data-ab-social]").forEach(function (link) {
        var href = link.getAttribute("href");
        if (!href) return;
        var params = [];
        if (bookingRef) params.push("booking_reference=" + encodeURIComponent(bookingRef));
        if (nextUrl) params.push("next=" + encodeURIComponent(nextUrl));
        link.setAttribute("href", href + (href.indexOf("?") >= 0 ? "&" : "?") + params.join("&"));
      });
    }
    if (root.querySelector(".ab-alert--err")) {
      window.setTimeout(function () {
        root.classList.add("ab-shake");
        root.addEventListener("animationend", function () { root.classList.remove("ab-shake"); }, { once: true });
      }, 80);
    }

    setView(root.dataset.initialView || "login");
  }

  document.querySelectorAll("[data-auth-root]").forEach(initAuthRoot);

  /* ── Modal wiring ── */
  var modal = document.getElementById("authModal");
  if (modal && typeof modal.showModal === "function") {
    var modalRoot = modal.querySelector("[data-auth-root]");

    function openAuthModal(view) {
      if (modalRoot && modalRoot.SKAIR_SET_AUTH_VIEW) modalRoot.SKAIR_SET_AUTH_VIEW(view || "login");
      if (!modal.open) modal.showModal();
      document.documentElement.classList.add("ab-modal-open");
    }
    window.SKAIR_OPEN_AUTH = openAuthModal;

    document.addEventListener("click", function (event) {
      var trigger = event.target.closest("[data-auth-open]");
      if (!trigger) return;
      event.preventDefault();
      openAuthModal(trigger.dataset.authOpen || "login");
    });

    modal.addEventListener("close", function () {
      document.documentElement.classList.remove("ab-modal-open");
    });
    modal.querySelector("[data-ab-close]")?.addEventListener("click", function () { modal.close(); });

    // Click on the backdrop (the dialog element itself, outside the card
    // content) dismisses, like every modern auth modal.
    modal.addEventListener("mousedown", function (event) {
      if (event.target === modal) modal.close();
    });
  }
})();
