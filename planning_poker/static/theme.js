(() => {
  const key = 'pointy:theme';
  const valid = (value) => value === 'light' || value === 'dark';
  const root = document.documentElement;
  const apply = (theme, persist) => {
    const next = valid(theme) ? theme : 'dark';
    root.dataset.theme = next;
    document.querySelectorAll('input[name="theme"]').forEach((input) => {
      input.checked = input.value === next;
    });
    if (persist) {
      try { localStorage.setItem(key, next); } catch (error) { /* UI remains usable. */ }
    }
  };

  apply(root.dataset.theme, false);
  document.querySelectorAll('input[name="theme"]').forEach((input) => {
    input.addEventListener('change', () => { if (input.checked) apply(input.value, true); });
  });
})();
