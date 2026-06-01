(function () {
  const isoDatePattern = /^\d{4}-\d{2}-\d{2}$/;
  const monthPattern = /^\d{4}-\d{2}$/;

  function clearFieldError(field) {
    if (field) field.setCustomValidity("");
  }

  function setFieldError(field, message) {
    if (!field) return null;
    field.setCustomValidity(message || "");
    return field;
  }

  function parseIsoDate(value) {
    if (!isoDatePattern.test(value || "")) return null;
    const parsed = new Date(`${value}T00:00:00`);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function routeFingerprint(value) {
    const raw = (value || "").trim();
    if (!raw) return "";

    const embeddedCode = raw.match(/\(([A-Za-z]{3})\)/);
    if (embeddedCode) return embeddedCode[1].toUpperCase();

    if (/^[A-Za-z]{3}$/.test(raw)) return raw.toUpperCase();
    return raw.replace(/\s+/g, " ").trim().toUpperCase();
  }

  function focusAndReport(field) {
    if (!field) return;
    if (typeof field.focus === "function") field.focus();
    if (typeof field.reportValidity === "function") field.reportValidity();
  }

  function validateRoute(originInput, destinationInput) {
    clearFieldError(originInput);
    clearFieldError(destinationInput);

    const origin = (originInput?.value || "").trim();
    const destination = (destinationInput?.value || "").trim();

    if (!origin) return setFieldError(originInput, "Please enter an origin airport or city.");
    if (!destination) return setFieldError(destinationInput, "Please enter a destination airport or city.");

    if (routeFingerprint(origin) && routeFingerprint(origin) === routeFingerprint(destination)) {
      return setFieldError(destinationInput, "Origin and destination can't be the same.");
    }

    return null;
  }

  function attachAiValidation() {
    const form = document.getElementById("aiForm");
    const aiText = document.getElementById("aiText");
    if (!form || !aiText) return;

    const clear = () => clearFieldError(aiText);
    aiText.addEventListener("input", clear);
    aiText.addEventListener("blur", clear);

    form.addEventListener("submit", (event) => {
      clear();
      const prompt = (aiText.value || "").trim();
      if (prompt) return;

      event.preventDefault();
      focusAndReport(setFieldError(aiText, "Please describe the trip before searching."));
    });
  }

  function attachUnifiedValidation() {
    const form = document.getElementById("unifiedForm");
    if (!form) return;

    const modeInput = document.getElementById("unifiedMode");
    const tripTypeInput = document.getElementById("tripType");
    const originInput = form.querySelector('input[name="origin"]');
    const destinationInput = form.querySelector('input[name="destination"]');
    const departDisplay = document.getElementById("departPickerDisplay");
    const departHidden = document.getElementById("departPicker");
    const returnDisplay = document.getElementById("returnPickerDisplay");
    const returnHidden = document.getElementById("returnPicker");
    const flexMonthInput = document.getElementById("flexMonthPicker");
    const multiCityLegsWrap = document.getElementById("multiCityLegs");

    if (!modeInput || !tripTypeInput || !originInput || !destinationInput || !departDisplay || !departHidden || !returnDisplay || !returnHidden || !flexMonthInput) {
      return;
    }

    const getFlexMonthDisplay = () => flexMonthInput._monthRollerDisplay || flexMonthInput._flatpickr?.altInput || flexMonthInput;

    const clear = () => {
      [originInput, destinationInput, departDisplay, returnDisplay, getFlexMonthDisplay()].forEach(clearFieldError);
    };

    [originInput, destinationInput, departDisplay, returnDisplay, flexMonthInput, tripTypeInput].forEach((field) => {
      field?.addEventListener("input", clear);
      field?.addEventListener("change", clear);
    });

    form.addEventListener("submit", (event) => {
      clear();

      const isMultiCity = tripTypeInput.value === "multicity";
      if (isMultiCity) {
        const legRows = Array.from((multiCityLegsWrap?.querySelectorAll("[data-multicity-leg]") || []));
        if (legRows.length < 2) {
          event.preventDefault();
          return;
        }
        let previousDate = null;
        for (let i = 0; i < legRows.length; i += 1) {
          const row = legRows[i];
          const originInput = row.querySelector('input[name="leg_origin"]');
          const destinationInput = row.querySelector('input[name="leg_destination"]');
          const dateInput = row.querySelector('input[name="leg_date"]');
          const invalidRoute = validateRoute(originInput, destinationInput);
          if (invalidRoute) {
            event.preventDefault();
            focusAndReport(invalidRoute);
            return;
          }
          const dateValue = (dateInput?.value || "").trim();
          if (!isoDatePattern.test(dateValue)) {
            event.preventDefault();
            focusAndReport(setFieldError(dateInput, `Please choose a valid date for leg ${i + 1}.`));
            return;
          }
          const currentDate = parseIsoDate(dateValue);
          if (previousDate && currentDate && currentDate < previousDate) {
            event.preventDefault();
            focusAndReport(setFieldError(dateInput, `Leg ${i + 1} cannot depart before leg ${i}.`));
            return;
          }
          previousDate = currentDate;
        }
        return;
      }

      let invalidField = validateRoute(originInput, destinationInput);
      if (invalidField) {
        event.preventDefault();
        focusAndReport(invalidField);
        return;
      }

      if (modeInput.value === "flex") {
        const monthDisplay = getFlexMonthDisplay();
        const monthValue = (flexMonthInput.value || "").trim();
        if (!monthValue) {
          event.preventDefault();
          focusAndReport(setFieldError(monthDisplay, "Please choose a month."));
          return;
        }
        if (!monthPattern.test(monthValue)) {
          event.preventDefault();
          focusAndReport(setFieldError(monthDisplay, "Please choose a valid month."));
        }
        return;
      }

      const departValue = (departHidden.value || "").trim();
      if (!departValue) {
        event.preventDefault();
        focusAndReport(setFieldError(departDisplay, "Please choose a departure date."));
        return;
      }

      if (!isoDatePattern.test(departValue)) {
        event.preventDefault();
        focusAndReport(setFieldError(departDisplay, "Please choose a valid departure date."));
        return;
      }

      if (tripTypeInput.value === "oneway") return;

      const returnValue = (returnHidden.value || "").trim();
      if (!returnValue) {
        event.preventDefault();
        focusAndReport(setFieldError(returnDisplay, "Please choose a return date or switch to one-way."));
        return;
      }

      if (!isoDatePattern.test(returnValue)) {
        event.preventDefault();
        focusAndReport(setFieldError(returnDisplay, "Please choose a valid return date."));
        return;
      }

      const departDate = parseIsoDate(departValue);
      const returnDate = parseIsoDate(returnValue);
      if (departDate && returnDate && returnDate < departDate) {
        event.preventDefault();
        focusAndReport(setFieldError(returnDisplay, "Return date must be the same day or after departure."));
      }
    });
  }

  function attachResultsValidation() {
    const form = document.getElementById("resultsQueryForm");
    if (!form) return;

    const tripTypeInput = document.getElementById("resultsTripType");
    const originInput = form.querySelector('input[name="origin"]');
    const destinationInput = form.querySelector('input[name="destination"]');
    const departDisplay = document.getElementById("refineDepartPickerDisplay");
    const departHidden = document.getElementById("refineDepartPicker");
    const returnDisplay = document.getElementById("refineReturnPickerDisplay");
    const returnHidden = document.getElementById("refineReturnPicker");
    const multiCityLegsWrap = document.getElementById("resultsMultiCityLegs");

    if (!tripTypeInput || !originInput || !destinationInput || !departDisplay || !departHidden || !returnDisplay || !returnHidden) {
      return;
    }

    const clear = () => {
      [originInput, destinationInput, departDisplay, returnDisplay].forEach(clearFieldError);
      const legRows = Array.from((multiCityLegsWrap?.querySelectorAll("[data-multicity-leg]") || []));
      legRows.forEach((row) => {
        const o = row.querySelector('input[name="leg_origin"]');
        const d = row.querySelector('input[name="leg_destination"]');
        const dt = row.querySelector('input[name="leg_date"]');
        [o, d, dt].forEach(clearFieldError);
      });
    };

    [originInput, destinationInput, departDisplay, returnDisplay, tripTypeInput].forEach((field) => {
      field?.addEventListener("input", clear);
      field?.addEventListener("change", clear);
    });

    form.addEventListener("submit", (event) => {
      clear();

      if (tripTypeInput.value === "multicity") {
        const legRows = Array.from((multiCityLegsWrap?.querySelectorAll("[data-multicity-leg]") || []));
        if (legRows.length < 2) {
          event.preventDefault();
          return;
        }
        let previousDate = null;
        for (let i = 0; i < legRows.length; i += 1) {
          const row = legRows[i];
          const legOrigin = row.querySelector('input[name="leg_origin"]');
          const legDestination = row.querySelector('input[name="leg_destination"]');
          const legDate = row.querySelector('input[name="leg_date"]');
          const invalidRoute = validateRoute(legOrigin, legDestination);
          if (invalidRoute) {
            event.preventDefault();
            focusAndReport(invalidRoute);
            return;
          }
          const dateValue = (legDate?.value || "").trim();
          if (!isoDatePattern.test(dateValue)) {
            event.preventDefault();
            focusAndReport(setFieldError(legDate, `Please choose a valid date for leg ${i + 1}.`));
            return;
          }
          const currentDate = parseIsoDate(dateValue);
          if (previousDate && currentDate && currentDate < previousDate) {
            event.preventDefault();
            focusAndReport(setFieldError(legDate, `Leg ${i + 1} cannot depart before leg ${i}.`));
            return;
          }
          previousDate = currentDate;
        }
        return;
      }

      let invalidField = validateRoute(originInput, destinationInput);
      if (invalidField) {
        event.preventDefault();
        focusAndReport(invalidField);
        return;
      }

      const departValue = (departHidden.value || "").trim();
      if (!departValue) {
        event.preventDefault();
        focusAndReport(setFieldError(departDisplay, "Please choose a departure date."));
        return;
      }

      if (!isoDatePattern.test(departValue)) {
        event.preventDefault();
        focusAndReport(setFieldError(departDisplay, "Please choose a valid departure date."));
        return;
      }

      if (tripTypeInput.value === "oneway") return;

      const returnValue = (returnHidden.value || "").trim();
      if (!returnValue) {
        event.preventDefault();
        focusAndReport(setFieldError(returnDisplay, "Please choose a return date or switch to one-way."));
        return;
      }

      if (!isoDatePattern.test(returnValue)) {
        event.preventDefault();
        focusAndReport(setFieldError(returnDisplay, "Please choose a valid return date."));
        return;
      }

      const departDate = parseIsoDate(departValue);
      const returnDate = parseIsoDate(returnValue);
      if (departDate && returnDate && returnDate < departDate) {
        event.preventDefault();
        focusAndReport(setFieldError(returnDisplay, "Return date must be the same day or after departure."));
      }
    });
  }

  attachAiValidation();
  attachUnifiedValidation();
  attachResultsValidation();
})();
