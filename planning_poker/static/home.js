const form = document.querySelector('#create-form');
const errorBox = document.querySelector('#form-error');
const basePath = location.pathname.replace(/\/+$/, '');

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  errorBox.textContent = '';
  const button = form.querySelector('button');
  button.disabled = true;
  try {
    const data = Object.fromEntries(new FormData(form));
    const response = await fetch(`${basePath}/api/rooms`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || 'Could not create the room.');
    sessionStorage.setItem(`pointy:${result.code}`, JSON.stringify(result));
    location.href = `${basePath}/room/${result.code}`;
  } catch (error) {
    errorBox.textContent = error.message;
    button.disabled = false;
  }
});
