let TICK_TARGET = 'all';
let TICK_COLOR = '#ffd34d';
let TICK_SCREENS = {};

//=============================================================================
// БЕГУЩАЯ СТРОКА
//=============================================================================

function initTickerViewActions(){
  if(window.__tickerViewActionsInitialized) return;
  window.__tickerViewActionsInitialized = true;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('ticker-')) return;

    switch(action){
      case 'ticker-stop': {
        const id = Number(el.dataset.tickerId);
        return Signage.stopTicker(id);
      }

      case 'ticker-pick-color': {
        const color = el.dataset.color || '#ffd34d';
        TICK_COLOR = color;
        const prev = document.getElementById('tk-prev');
        if(prev) prev.style.color = color;
        return Signage.renderTickColors();
      }

      case 'ticker-target-all':
        TICK_TARGET = 'all';
        return Signage.viewTicker();

      case 'ticker-target-one':
        TICK_TARGET = 'one';
        return Signage.viewTicker();

      case 'ticker-toggle-screen': {
        const id = Number(el.dataset.screenId);
        TICK_SCREENS[id] = !TICK_SCREENS[id];
        return Signage.viewTicker();
      }

      case 'ticker-start':
        return Signage.startTicker();
    }
  });

  document.addEventListener('input', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(action === 'ticker-preview-input'){
      const prev = document.getElementById('tk-prev');
      if(prev) prev.textContent = (el.value || '') + '   ';
    }
  });
}

async function viewTicker(){
  const view = document.getElementById('view');

  let screens = [];
  let active = [];

  try{
    screens = await api('/minipc');
  }catch(e){}

  try{
    active = await api('/tickers');
  }catch(e){}

  let h = '';

  if(active.length){
    h += '<div class="sec" style="margin-top:0;">Активные строки</div>';
    active.forEach(t => {
      h += `<div class="cell" style="display:flex;align-items:center;gap:10px;margin-bottom:7px;">
        <span style="color:${t.color};">▦</span>
        <span style="flex:1;">${esc(t.message)}</span>
        <span class="muted" style="font-size:11px;">${t.is_all ? 'на всех' : 'на выбранных'}</span>
        <button class="btn danger" data-action="ticker-stop" data-ticker-id="${t.id}">Снять</button>
      </div>`;
    });
  }

  h += '<div class="sec">Новая бегущая строка</div>';
  h += `<div class="tickprev">
    <span class="dim">▶ видео экрана</span>
    <div class="tickbar">
      <span class="tickrun" id="tk-prev" style="color:${TICK_COLOR};">Внимание: пример бегущей строки&nbsp;&nbsp;&nbsp;</span>
    </div>
  </div>`;

  h += `<div class="fld" style="margin-top:12px;">
    <label>Текст сообщения</label>
    <input class="inp" id="tk-msg" placeholder="Введите текст" data-action="ticker-preview-input">
  </div>`;

  h += `<div class="row">
    <div class="fld" style="flex:1;">
      <label>Скорость</label>
      <select class="inp" id="tk-speed">
        <option value="slow">Медленно</option>
        <option value="medium" selected>Средне</option>
        <option value="fast">Быстро</option>
      </select>
    </div>
    <div class="fld" style="flex:1;">
      <label>Цвет</label>
      <div style="display:flex;gap:6px;">
        ${['#ffd34d','#ffffff','#ff6b5e','#7fe3c4'].map(c => `<span
          data-action="ticker-pick-color"
          data-color="${c}"
          data-c="${c}"
          class="tkc"
          style="width:26px;height:26px;border-radius:6px;background:${c};cursor:pointer;border:2px solid ${c === TICK_COLOR ? '#fff' : 'transparent'};"></span>`).join('')}
      </div>
    </div>
  </div>`;

  h += `<div class="fld" style="margin-top:2px;">
    <label>Время показа, сек <span class="muted" style="font-weight:normal;">(0 или пусто — бессрочно, до снятия)</span></label>
    <input class="inp" id="tk-duration" type="number" min="0" step="1" placeholder="0" style="max-width:220px;">
  </div>`;

  h += '<div class="sec">Куда вывести</div>';
  h += `<div class="seg" style="margin-bottom:11px;">
    <button class="${TICK_TARGET === 'all' ? 'on' : ''}" data-action="ticker-target-all">На все экраны</button>
    <button class="${TICK_TARGET === 'one' ? 'on' : ''}" data-action="ticker-target-one">На выбранные</button>
  </div>`;

  if(TICK_TARGET === 'one'){
    h += '<div style="margin-bottom:11px;">';
    screens.forEach(s => {
      const on = s.status === 'online';
      const sel = !!TICK_SCREENS[s.id];

      h += `<div class="cell" style="display:flex;align-items:center;gap:8px;margin-bottom:6px;cursor:pointer;${sel ? 'border-color:var(--accent2);' : ''}${on ? '' : 'opacity:.5;'}"
        data-action="ticker-toggle-screen"
        data-screen-id="${s.id}">
        <span>${sel ? '☑' : '☐'}</span>
        ${esc(s.name)}
        ${on ? '' : '<span class="danger" style="font-size:11px;color:var(--danger);">(офлайн)</span>'}
      </div>`;
    });
    h += '</div>';
  }

  h += `<button class="btn primary" data-action="ticker-start">▶ Запустить бегущую строку</button>`;
  view.innerHTML = h;
}

function renderTickColors(){
  document.querySelectorAll('.tkc').forEach(e => {
    e.style.border = '2px solid ' + (e.dataset.c === TICK_COLOR ? '#fff' : 'transparent');
  });
}

async function startTicker(){
  const msg = val('tk-msg');
  if(!msg){
    toast('Введите текст');
    return;
  }

  const speed = val('tk-speed');
  const duration = Math.max(0, parseInt(val('tk-duration'), 10) || 0);
  const screen_ids = TICK_TARGET === 'one'
    ? Object.keys(TICK_SCREENS).filter(k => TICK_SCREENS[k]).map(Number)
    : [];

  if(TICK_TARGET === 'one' && !screen_ids.length){
    toast('Выберите хотя бы один экран');
    return;
  }

  try{
    await api('/tickers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: msg,
        speed,
        duration,
        color: TICK_COLOR,
        is_all: TICK_TARGET === 'all',
        screen_ids
      })
    });

    toast('Бегущая строка запущена');
    TICK_SCREENS = {};
    TICK_TARGET = 'all';
    viewTicker();
  }catch(e){
    toast('Ошибка: ' + e.message);
  }
}

async function stopTicker(id){
  if(!confirm('Снять бегущую строку?')) return;

  try{
    await api('/tickers/' + id, { method: 'DELETE' });
    toast('Остановлено');
    viewTicker();
  }catch(e){
    toast('Ошибка: ' + e.message);
  }
}

window.Signage = window.Signage || {};
window.Signage.viewTicker = viewTicker;
window.Signage.renderTickColors = renderTickColors;
window.Signage.startTicker = startTicker;
window.Signage.stopTicker = stopTicker;

initTickerViewActions();

