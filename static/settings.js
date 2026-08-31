document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".deadline-type-select").forEach((select) => {
    const row = select.closest("tr");
    const daysInput = row.querySelector(".days-input");
    const cutoffInput = row.querySelector(".cutoff-input");

    function sync() {
      const isNonRef = select.value === "non_ref";
      daysInput.disabled = isNonRef;
      cutoffInput.disabled = isNonRef;
    }

    select.addEventListener("change", sync);
    sync();
  });

  // Quick-select buttons inside a threshold rule's hotel picker: "+ all EUR"
  // checks every chip for that currency (without unchecking others, so you
  // can combine currencies or add a few more by hand), "Clear" unchecks all.
  document.querySelectorAll(".currency-select").forEach((btn) => {
    btn.addEventListener("click", () => {
      const container = document.getElementById(btn.dataset.target);
      if (!container) return;
      container.querySelectorAll(".hotel-chip").forEach((chip) => {
        if (chip.dataset.currency === btn.dataset.currency) {
          chip.querySelector("input").checked = true;
        }
      });
    });
  });

  document.querySelectorAll(".clear-select").forEach((btn) => {
    btn.addEventListener("click", () => {
      const container = document.getElementById(btn.dataset.target);
      if (!container) return;
      container.querySelectorAll('input[type="checkbox"]').forEach((cb) => (cb.checked = false));
    });
  });
});
