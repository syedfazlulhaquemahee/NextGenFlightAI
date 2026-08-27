document.addEventListener("DOMContentLoaded", () => {
  const input = document.getElementById("supportSearch");
  const items = [...document.querySelectorAll("[data-support-search]")];
  const empty = document.getElementById("supportNoResults");
  if (!input || !empty) return;
  input.addEventListener("input", () => {
    const query = input.value.trim().toLowerCase();
    let visible = 0;
    items.forEach((item) => {
      const match = !query || `${item.textContent} ${item.dataset.supportSearch}`.toLowerCase().includes(query);
      item.hidden = !match;
      if (match) visible += 1;
    });
    empty.hidden = visible > 0;
  });
});
