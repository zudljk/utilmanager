// Use the browser's public origin, including HTTPS terminated at a proxy.
// The server supplies url_for's mount-aware path; no credentials are embedded.
for (const address of document.querySelectorAll('[data-public-path]')) {
  address.value = window.location.origin + address.dataset.publicPath;
}
document.getElementById('shortcut-origin').value = window.location.origin;
document.getElementById('local-address-warning').hidden =
  !['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname);

for (const button of document.querySelectorAll('[data-copy]')) {
  button.hidden = false;
  button.addEventListener('click', async () => {
    const field = document.getElementById(button.dataset.copy);
    const status = document.getElementById('copy-status');
    try {
      await navigator.clipboard.writeText(field.value);
      status.textContent = 'Adresse kopiert.';
    } catch {
      field.focus();
      field.select();
      field.setSelectionRange(0, field.value.length);
      status.textContent = 'Adresse markiert. Bitte über das Kopiermenü deines Geräts kopieren.';
    }
  });
}
