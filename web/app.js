(() => {
  const state = {
    destination: "",
    departDate: "",
    returnDate: "",
    budget: "",
  };

  const panels = {
    1: document.getElementById("step-destination"),
    2: document.getElementById("step-date"),
    3: document.getElementById("step-budget"),
    4: document.getElementById("step-summary"),
    5: document.getElementById("step-result"),
  };

  const destinationInput = document.getElementById("destination");
  const departInput = document.getElementById("depart-date");
  const returnInput = document.getElementById("return-date");
  const budgetInput = document.getElementById("budget");

  const sumDestination = document.getElementById("sum-destination");
  const sumDates = document.getElementById("sum-dates");
  const sumBudget = document.getElementById("sum-budget");

  const planBtn = document.getElementById("plan-btn");
  const restartBtn = document.getElementById("restart-btn");
  const loader = document.getElementById("loader");
  const resultBody = document.getElementById("result-body");
  const resultTitle = document.getElementById("result-title");
  const resultHint = document.getElementById("result-hint");
  const resultActions = document.getElementById("result-actions");

  function todayIso() {
    const d = new Date();
    const offset = d.getTimezoneOffset();
    const local = new Date(d.getTime() - offset * 60 * 1000);
    return local.toISOString().slice(0, 10);
  }

  function showStep(step) {
    Object.entries(panels).forEach(([key, el]) => {
      const active = Number(key) === step;
      el.hidden = !active;
      el.classList.toggle("is-active", active);
      if (active) {
        el.style.animation = "none";
        // Force reflow so the enter animation replays.
        void el.offsetWidth;
        el.style.animation = "";
      }
    });

    if (step === 1) destinationInput.focus();
    if (step === 2) departInput.focus();
    if (step === 3) budgetInput.focus();
  }

  function formatMoney(value) {
    const n = Number(value);
    return `HK$${n.toLocaleString("en-HK", { maximumFractionDigits: 0 })}`;
  }

  function formatDates() {
    if (!state.returnDate) return state.departDate;
    return `${state.departDate} → ${state.returnDate}`;
  }

  function fillSummary() {
    sumDestination.textContent = state.destination;
    sumDates.textContent = formatDates();
    sumBudget.textContent = formatMoney(state.budget);
  }

  function resetWizard() {
    state.destination = "";
    state.departDate = "";
    state.returnDate = "";
    state.budget = "";
    destinationInput.value = "";
    departInput.value = "";
    returnInput.value = "";
    budgetInput.value = "";
    resultBody.hidden = true;
    resultBody.textContent = "";
    loader.hidden = false;
    resultActions.hidden = true;
    resultTitle.textContent = "Building your plan…";
    resultHint.textContent = "Searching Trip.com and drafting an itinerary.";
    showStep(1);
  }

  // Minimum date = today
  const minDate = todayIso();
  departInput.min = minDate;
  returnInput.min = minDate;

  document.getElementById("form-destination").addEventListener("submit", (e) => {
    e.preventDefault();
    const value = destinationInput.value.trim();
    if (!value) return;
    state.destination = value;
    showStep(2);
  });

  document.getElementById("form-date").addEventListener("submit", (e) => {
    e.preventDefault();
    const depart = departInput.value;
    const ret = returnInput.value;
    if (!depart) return;
    if (ret && ret < depart) {
      returnInput.setCustomValidity("Return date must be on or after departure.");
      returnInput.reportValidity();
      return;
    }
    returnInput.setCustomValidity("");
    state.departDate = depart;
    state.returnDate = ret;
    showStep(3);
  });

  departInput.addEventListener("change", () => {
    if (departInput.value) {
      returnInput.min = departInput.value;
      if (returnInput.value && returnInput.value < departInput.value) {
        returnInput.value = "";
      }
    }
  });

  document.getElementById("form-budget").addEventListener("submit", (e) => {
    e.preventDefault();
    const budget = budgetInput.value;
    if (!budget || Number(budget) <= 0) return;
    state.budget = budget;
    fillSummary();
    showStep(4);
  });

  document.querySelectorAll("[data-back]").forEach((btn) => {
    btn.addEventListener("click", () => {
      showStep(Number(btn.dataset.back));
    });
  });

  planBtn.addEventListener("click", async () => {
    showStep(5);
    loader.hidden = false;
    resultBody.hidden = true;
    resultActions.hidden = true;
    resultTitle.textContent = "Building your plan…";
    resultHint.textContent = "Searching Trip.com and drafting an itinerary.";
    planBtn.disabled = true;

    try {
      const res = await fetch("/api/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          destination: state.destination,
          depart_date: state.departDate,
          return_date: state.returnDate || null,
          budget_hkd: Number(state.budget),
          origin: "HKG",
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || "Planning failed.");
      }
      resultTitle.textContent = "Your trip plan";
      resultHint.textContent = `Live search for ${state.destination} · budget ${formatMoney(state.budget)}`;
      resultBody.textContent = data.plan;
      resultBody.hidden = false;
    } catch (err) {
      resultTitle.textContent = "Something went wrong";
      resultHint.textContent = "Check that Ollama and the agent server are running.";
      resultBody.textContent = err.message || String(err);
      resultBody.hidden = false;
    } finally {
      loader.hidden = true;
      resultActions.hidden = false;
      planBtn.disabled = false;
    }
  });

  restartBtn.addEventListener("click", resetWizard);

  showStep(1);
})();
