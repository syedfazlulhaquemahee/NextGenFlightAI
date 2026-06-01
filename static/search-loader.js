(function () {
  const forms = document.querySelectorAll("form");
  if (!forms.length) return;

  function releaseButtons() {
    forms.forEach((form) => {
      form.removeAttribute("aria-busy");
      form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach((button) => {
        button.disabled = false;
      });
    });
  }

  function setFormSubmitBusy(form) {
    if (!form) return;
    form.setAttribute("aria-busy", "true");
  }

  function isFlexStreamForm(form) {
    if (form.id === "unifiedForm") {
      return (document.getElementById("unifiedMode")?.value || "standard") === "flex";
    }
    // AI uses the same first hop as manual Cheapest week: `/search/flex-shell` returns the streaming
    // results shell immediately; `/search/flex-stream` parses intent (flex vs standard handoff).
    if (form.id === "aiForm") {
      return true;
    }
    return false;
  }

  forms.forEach((form) => {
    form.addEventListener("submit", function (event) {
      if (event.defaultPrevented) return;

      const flexStream = isFlexStreamForm(form);
      const isSearchForm = form.id === "aiForm" || form.id === "unifiedForm";
      setFormSubmitBusy(form);
      // For flex searches, jump straight to the shell page so users see the
      // results UI/animation immediately while streaming continues there.
      if (flexStream) {
        form.setAttribute("action", "/search/flex-shell");
        form.setAttribute("method", "POST");
      } else if (isSearchForm) {
        form.setAttribute("action", "/search/shell");
        form.setAttribute("method", "POST");
      }
    });
  });
})();
