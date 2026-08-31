document.addEventListener("DOMContentLoaded", () => {
  const searchBox = document.getElementById("search-box");
  const propertyFilter = document.getElementById("property-filter");
  const viewSwitch = document.getElementById("view-switch");
  const tableBody = document.getElementById("table-body");
  const emptyState = document.getElementById("empty-state");
  const flagCountEl = document.getElementById("flag-count");
  const checkType = tableBody ? tableBody.dataset.check : null;

  function getSelectedView() {
    const checked = viewSwitch && viewSwitch.querySelector('input[name="view"]:checked');
    return checked ? checked.value : "flagged";
  }

  // "Flagged" = flagged and not yet handled (the daily work queue).
  // "Done" = handled, regardless of whether it was ever flagged.
  // "All" = no filtering by flagged/handled status at all.
  function matchesView(view, isFlagged, isHandled) {
    if (view === "flagged") return isFlagged && !isHandled;
    if (view === "done") return isHandled;
    return true;
  }

  // Independent of the view filters below: how many flagged ones are still
  // outstanding overall, so the header count stays accurate as the agent
  // works through the list instead of freezing at the page-load total. Kept
  // separate from applyFilters() so it can update instantly on a checkbox
  // change, without waiting for that row's fade-out animation to finish.
  function updateFlagCount() {
    if (!tableBody || !flagCountEl) return;
    let remaining = 0;
    tableBody.querySelectorAll("tr").forEach((row) => {
      if (row.dataset.flagged === "true" && row.dataset.handled !== "true") remaining += 1;
    });
    flagCountEl.textContent = remaining;
  }

  function applyFilters() {
    if (!tableBody) return;
    const query = searchBox.value.trim().toLowerCase();
    const property = propertyFilter.value;
    const view = getSelectedView();
    let visibleCount = 0;

    tableBody.querySelectorAll("tr").forEach((row) => {
      const matchesQuery = !query || row.dataset.confirmation.toLowerCase().includes(query);
      const matchesProperty = !property || row.dataset.property === property;
      const isFlagged = row.dataset.flagged === "true";
      const isHandled = row.dataset.handled === "true";

      const visible = matchesQuery && matchesProperty && matchesView(view, isFlagged, isHandled);
      row.hidden = !visible;
      if (visible) visibleCount += 1;
    });

    emptyState.hidden = visibleCount !== 0;
    updateFlagCount();
  }

  [searchBox, propertyFilter].forEach((el) => {
    if (el) el.addEventListener("input", applyFilters);
  });
  if (viewSwitch) viewSwitch.addEventListener("change", applyFilters);

  if (tableBody) {
    tableBody.addEventListener("click", async (event) => {
      const copyBtn = event.target.closest(".copy-btn");
      if (!copyBtn) return;

      const text = copyBtn.dataset.copy;
      try {
        await navigator.clipboard.writeText(text);
      } catch (err) {
        window.prompt("Copy this reference number:", text);
        return;
      }

      const original = copyBtn.textContent;
      copyBtn.textContent = "Copied!";
      copyBtn.classList.add("copy-btn--done");
      setTimeout(() => {
        copyBtn.textContent = original;
        copyBtn.classList.remove("copy-btn--done");
      }, 1200);
    });

    // Checking a row off gets a brief green "done" flash, then settles into
    // the dimmed handled look, then - only if the current view wouldn't show
    // a handled row (i.e. "Flagged") - fades out before it actually
    // disappears. Without this, a checked row just vanishes instantly with
    // no feedback that the click registered.
    function settleRow(row, handled) {
      if (row._settleTimer) {
        clearTimeout(row._settleTimer);
        row._settleTimer = null;
      }
      row.classList.remove("row--just-done", "row--fading");

      if (!handled) {
        row.classList.remove("row--handled");
        applyFilters();
        return;
      }

      row.classList.add("row--just-done");
      row._settleTimer = setTimeout(() => {
        row.classList.remove("row--just-done");
        row.classList.add("row--handled");
        if (getSelectedView() === "flagged") {
          row.classList.add("row--fading");
          row.addEventListener(
            "transitionend",
            () => {
              // Clean up now, not just hide via applyFilters() - otherwise
              // switching to "All" or "Done" later reveals this row again
              // (hidden is lifted) while opacity is still stuck at 0 from
              // this class, i.e. a blank row that takes up space but shows
              // nothing.
              row.classList.remove("row--fading");
              applyFilters();
            },
            { once: true }
          );
        } else {
          applyFilters();
        }
      }, 250);
    }

    tableBody.addEventListener("change", async (event) => {
      const checkbox = event.target;
      if (!checkbox.classList.contains("handled-checkbox")) return;

      const row = checkbox.closest("tr");
      const key = row.dataset.key;
      const findingFingerprint = row.dataset.findingFingerprint;
      const handled = checkbox.checked;

      row.dataset.handled = String(handled);
      updateFlagCount();
      settleRow(row, handled);

      try {
        const response = await fetch("/api/handled", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key, finding_fingerprint: findingFingerprint, handled, check: checkType }),
        });
        if (!response.ok) throw new Error("Request failed");
      } catch (err) {
        // Revert on failure so the UI never lies about saved state.
        checkbox.checked = !handled;
        row.dataset.handled = String(!handled);
        updateFlagCount();
        settleRow(row, !handled);
        alert("Couldn't save that change. Please try again.");
      }
    });
  }

  applyFilters();
});
