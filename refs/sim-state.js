// Dev-only: paste in the console (or inject) to fake the logged-in screens
// without a backend. Usage: simState('idle' | 'connecting' | 'listening' | 'speaking', withCard)
window.simState = (state, withCard = true) => {
  const $ = (id) => document.getElementById(id);
  document.documentElement.classList.remove('reveal-pending');
  $('logo').classList.add('hidden');
  $('greeting').classList.remove('hidden');
  $('greeting-name').textContent = 'Ciao Marta.';
  $('profile-area').classList.remove('hidden');
  $('profile-initial').textContent = 'M';
  $('dropdown-name').textContent = 'Marta';
  document.querySelectorAll('.screen').forEach(s => s.classList.add('hidden'));
  $('screen-main').classList.remove('hidden');
  document.querySelectorAll('[data-reveal]').forEach(e => { e.style.opacity = 1; e.style.transform = 'none'; });
  const orb = $('call-orb');
  orb.className = 'call-orb';
  const labels = { idle: 'Parla con Shy Order', connecting: 'Inizia la chiamata…', listening: 'Ti ascolto…', speaking: 'Sto parlando…', calling: 'Sto chiamando il ristorante…' };
  if (state === 'connecting') orb.classList.add('connecting');
  if (state === 'listening') orb.classList.add('active', 'listening');
  if (state === 'speaking') orb.classList.add('active', 'speaking');
  if (state === 'calling') orb.classList.add('active', 'listening', 'calling');
  $('call-note').hidden = state !== 'calling';
  $('call-note-text').textContent = "Resta in attesa, ti dico io com'è andata."; $('call-note-mic').textContent = 'Il tuo microfono è in pausa.';
  $('call-label').textContent = labels[state];
  const r = $('scrape-result');
  if (withCard) {
    r.className = 'scrape-result found';
    r.innerHTML = '<strong class="restaurant-name">Osteria Pepe Verde</strong><span class="restaurant-line">02 1234 5678</span><span class="restaurant-line">Via Esempio 12, Milano</span><span class="restaurant-line">12:30–15:00 · 19:30–23:00</span>';
    $('no-data-hint').hidden = true;
  } else { r.className = 'scrape-result'; r.textContent = ''; $('no-data-hint').hidden = false; }
};
