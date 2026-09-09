const code = location.pathname.split('/').filter(Boolean).pop().toUpperCase();
const basePath = location.pathname.replace(/\/room\/[^/]+\/?$/, '');
const sessionKey = `pointy:${code}`;
let session = JSON.parse(sessionStorage.getItem(sessionKey) || 'null');
let socket, state, selectedVote;
let me = session?.participantId || null;
const $ = (selector) => document.querySelector(selector);

$('#room-code').textContent = code;
$('#join-code').textContent = code;

function toast(message) {
  const el = $('#toast'); el.textContent = message; el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 3500);
}

async function join(nickname) {
  const response = await fetch(`${basePath}/api/rooms/${encodeURIComponent(code)}/join`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nickname})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.message || 'Could not join this room.');
  session = result; sessionStorage.setItem(sessionKey, JSON.stringify(result));
  me = result.participantId;
  $('#join-modal').hidden = true; connect();
}

$('#join-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const button = event.target.querySelector('button'); button.disabled = true;
  try { await join(new FormData(event.target).get('nickname')); }
  catch (error) { $('#join-error').textContent = error.message; button.disabled = false; }
});

function connect() {
  if (!session) { $('#join-modal').hidden = false; return; }
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${protocol}://${location.host}${basePath}/ws/${code}`);
  // The token is sent in the first frame instead of the URL so it never lands
  // in a proxy access log.
  socket.onopen = () => {
    socket.send(JSON.stringify({type: 'auth', token: session.token}));
    selectedVote = undefined; // a dropped connection clears the vote server-side
    $('#connection').classList.add('online'); $('#connection span').textContent = 'Live';
  };
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === 'welcome') {
      me = message.participantId;
      if (state) render();
    }
    // States can arrive from several app instances; never render an older one.
    if (message.type === 'state' && (!state || message.state.version >= state.version)) {
      const isNewRound = state?.revealed && !message.state.revealed && message.state.participants.every(p => !p.hasVoted);
      state = message.state;
      if (isNewRound) selectedVote = undefined;
      if (me && !state.participants.some(p => p.id === me)) {
        sessionStorage.removeItem(sessionKey);
        session = null;
        me = null;
        toast('You were removed from this room.');
        $('#join-modal').hidden = false;
        return;
      }
      render();
    }
    if (message.type === 'error') {
      toast(message.message);
      if (['unauthorized','room_not_found','nickname_taken'].includes(message.code)) { sessionStorage.removeItem(sessionKey); session = null; }
    }
  };
  socket.onclose = () => {
    $('#connection').classList.remove('online'); $('#connection span').textContent = 'Offline';
    if (!session) $('#join-modal').hidden = false;
    else setTimeout(connect, 1500);
  };
}

function send(payload) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
  else toast('You are offline. Reconnecting…');
}

function initials(name) { return name.split(/\s+/).map(x => x[0]).join('').slice(0,2).toUpperCase(); }
function escapeHtml(value) { const div=document.createElement('div');div.textContent=String(value);return div.innerHTML; }
function formatTime(value) { return new Date(value * 1000).toLocaleString(); }

function render() {
  document.title = `${state.task} · Pointy`;
  const current = state.participants.find(p => p.id === me);
  const isHost = !!me && (state.hostId === me || current?.isHost);
  const taskInput = $('#task');
  if (document.activeElement !== taskInput) taskInput.value = state.task;
  taskInput.disabled = !isHost;
  $('#round').textContent = `Round ${state.round}`;
  $('#member-count').textContent = state.participants.length;
  $('#participants').innerHTML = state.participants.map(p => `
    <div class="participant"><span class="avatar">${escapeHtml(initials(p.nickname))}</span><span class="participant-name">${escapeHtml(p.nickname)}</span>
    ${p.isHost ? '<span class="host-tag">Host</span>' : ''}
    ${isHost && p.id !== me ? `<button class="kick" type="button" data-kick="${escapeHtml(p.id)}" title="Remove">Remove</button>` : ''}
    ${state.revealed ? `<span class="revealed-vote">${p.hasVoted ? escapeHtml(p.vote) : '—'}</span>` : `<span class="vote-status ${p.hasVoted?'done':''}"></span>`}</div>`).join('');
  $('#cards').innerHTML = state.cards.map(card => `<button class="card ${selectedVote === card ? 'selected':''}" ${state.revealed?'disabled':''} data-vote="${escapeHtml(card)}"><span>${escapeHtml(card)}</span></button>`).join('');
  $('#cards').querySelectorAll('.card').forEach((button, index) => button.onclick = () => { selectedVote = state.cards[index]; send({type:'vote',value:selectedVote}); render(); });
  const voted = state.participants.filter(p => p.hasVoted).length;
  $('#progress').style.width = `${state.participants.length ? voted/state.participants.length*100 : 0}%`;
  $('#progress-label').textContent = `${voted} of ${state.participants.length} voted`;
  $('#prompt').textContent = state.revealed ? 'Cards are on the table' : (current?.hasVoted ? 'Vote locked in — you can still change it' : 'Pick your estimate');
  $('#arena-note').textContent = state.revealed ? 'Talk through the differences, then start another round.' : 'Votes stay hidden until the host reveals them.';
  $('#reveal').hidden = !(isHost && !state.revealed);
  $('#host-controls').hidden = !(isHost && state.revealed);
  const alreadyFinalized = state.history.some(entry => entry.round === state.round);
  $('#finalize').hidden = !(isHost && state.revealed && !alreadyFinalized);
  const effortInput = $('#finalize-round input[name="effort"]');
  if (state.revealed && !alreadyFinalized && document.activeElement !== effortInput) {
    const s = state.statistics;
    effortInput.value = s ? String(s.median) : '';
    $('#finalize-hint').textContent = s ? `Suggestions: average ${s.average}, median ${s.median}, low ${s.lowest}, high ${s.highest}. You can enter any final effort.` : 'No numeric votes. Enter the final effort you agree on.';
  }
  $('#claim-host').hidden = !me || isHost;
  $('#results').hidden = !state.revealed;
  const nextTaskInput = $('#new-round input[name="task"]');
  if (document.activeElement !== nextTaskInput) {
    nextTaskInput.value = state.task && state.task !== 'Untitled task' ? state.task : '';
  }
  if (state.revealed) {
    const s = state.statistics;
    $('#stats').innerHTML = s ? `<div class="results-grid">${[['Average',s.average],['Median',s.median],['Highest',s.highest],['Lowest',s.lowest]].map(([label,value])=>`<div class="stat"><span class="stat-label">${label}</span><span class="stat-value">${value} <small>KPT</small></span></div>`).join('')}</div>` : '<p class="empty-stats">No numeric KPT votes this round.</p>';
  }
  $('#history-empty').hidden = state.history.length > 0;
  $('#history-list').innerHTML = state.history.map(entry => `<article class="history-entry">
    <div><strong>${escapeHtml(entry.task)}</strong><span class="history-meta">Round ${entry.round} · saved ${escapeHtml(formatTime(entry.createdAt))}${entry.updatedAt !== entry.createdAt ? ` · edited ${escapeHtml(formatTime(entry.updatedAt))}` : ''}</span></div>
    <div class="history-effort">${escapeHtml(entry.effort)}</div>
    ${isHost ? `<div class="history-actions"><button class="secondary" type="button" data-reestimate="${escapeHtml(entry.id)}">Re-estimate</button><button class="secondary" type="button" data-remove-history="${escapeHtml(entry.id)}">Remove</button></div>` : ''}
  </article>`).join('');
}

$('#reveal').onclick = () => send({type:'reveal'});
$('#claim-host').onclick = () => send({type:'claim_host'});
$('#task-form').addEventListener('submit', (event) => {
  event.preventDefault();
  send({type:'rename_task', task: new FormData(event.target).get('task')});
});
$('#task').addEventListener('change', (event) => send({type:'rename_task', task: event.target.value}));
$('#participants').addEventListener('click', (event) => {
  const button = event.target.closest('[data-kick]');
  if (button) send({type:'kick', participantId: button.dataset.kick});
});
$('#new-round').addEventListener('submit', (event) => {
  event.preventDefault();
  selectedVote = undefined;
  send({type:'new_round', task: new FormData(event.target).get('task') || ''});
});
$('#finalize-round').addEventListener('submit', (event) => {
  event.preventDefault();
  send({type:'finalize_round', effort: new FormData(event.target).get('effort')});
});
$('#history-list').addEventListener('click', (event) => {
  const reestimate = event.target.closest('[data-reestimate]');
  if (reestimate) { selectedVote = undefined; send({type:'new_round', historyEntryId: reestimate.dataset.reestimate}); return; }
  const remove = event.target.closest('[data-remove-history]');
  if (remove) send({type:'remove_history_entry', historyEntryId: remove.dataset.removeHistory});
});
$('#export-history').onclick = async () => {
  const text = (state?.history || []).map(entry => `${entry.task}\t${entry.effort}`).join('\n');
  if (!text) { toast('There is no history to export yet.'); return; }
  try { await navigator.clipboard.writeText(text); toast('History copied as TSV.'); }
  catch { prompt('Copy this history (TSV):', text); }
};
$('#copy-code').onclick = async () => { try { await navigator.clipboard.writeText(location.href); toast('Invite link copied!'); } catch { toast(`Share this link: ${location.href}`); } };

connect();
