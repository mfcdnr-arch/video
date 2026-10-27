let SCH_TGT='network', SCH_MODE='week', SCH_TARGET_ID=null, SCH_MONTH=null, SCH_PLAYLISTS=[];
// Режим «По датам» (SCH_MODE='dates', только для экрана): понедельник показанной недели, клетки,
// выбранная клетка и карточка её показа
let SCH_WEEK=null, SCH_D_LIST=[], SCH_D_CELLS=new Map(), SCH_D_PICK=null, SCH_D_CARD=null;
let SCH_D_SEQ=0, SCH_D_CSEQ=0;       // номера последних загрузок недели и карточки — устаревший ответ отбрасываем
const SCH_D_BUSY=new Set();          // id показов, по которым из карточки сейчас идёт запрос

//=============================================================================
// РАСПИСАНИЕ: недельный шаблон (экран > группа > вся сеть) + датовые переопределения
//=============================================================================

function schTargetQS(q){
  if(SCH_TGT==='network') q.append('network','true');
  else q.append(SCH_TGT==='screen'?'screen_id':'group_id', SCH_TARGET_ID);
  return q;
}

async function viewSchedule(){
  const view=document.getElementById('view');
  let screens=[],groups=[];
  try{ [screens,groups,SCH_PLAYLISTS]=await Promise.all([api('/minipc'),api('/groups'),api('/playlists')]); }catch(e){}
  const list = SCH_TGT==='screen'?screens:(SCH_TGT==='group'?groups:[]);
  if(SCH_TGT!=='network'){
    if(!list.length){ document.getElementById('topright').innerHTML=''; view.innerHTML='<div class="empty">Нет '+(SCH_TGT==='screen'?'экранов':'групп')+'</div>'; return; }
    if(SCH_TARGET_ID===null || !list.find(x=>x.id===SCH_TARGET_ID)) SCH_TARGET_ID=list[0].id;
  }
  if(!SCH_MONTH){ const d=new Date(); SCH_MONTH=d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0'); }
  if(!SCH_WEEK) SCH_WEEK=schMondayOf(schTodayMsk());
  if(SCH_TGT==='network') SCH_MODE='week';  // переопределения дат для «всей сети» не поддерживаются
  else if(SCH_TGT!=='screen' && SCH_MODE==='dates') SCH_MODE='week';  // «По датам» — только для экрана

  document.getElementById('topright').innerHTML=`
    <div class="seg">
      <button class="${SCH_TGT==='network'?'on':''}" data-action="schedule-target-network">Вся сеть</button>
      <button class="${SCH_TGT==='group'?'on':''}" data-action="schedule-target-group">Группа</button>
      <button class="${SCH_TGT==='screen'?'on':''}" data-action="schedule-target-screen">Экран</button>
    </div>
    ${SCH_TGT!=='network'?`<select class="inp" style="width:auto;" data-action="schedule-select-target">${list.map(x=>`<option value="${x.id}" ${x.id===SCH_TARGET_ID?'selected':''}>${esc(x.name)}</option>`).join('')}</select>`:''}
    <div class="seg">
      <button class="${SCH_MODE==='week'?'on':''}" data-action="schedule-mode-week">Неделя</button>
      ${SCH_TGT==='screen'?`<button class="${SCH_MODE==='dates'?'on':''}" data-action="schedule-mode-dates" title="Что играет на экране в реальные даты, с отправленными показами">По датам</button>`:''}
      ${SCH_TGT!=='network'?`<button class="${SCH_MODE==='month'?'on':''}" data-action="schedule-mode-month">Даты</button>`:''}
    </div>
    ${SCH_MODE==='month'?`<input class="inp" type="month" style="width:auto;" value="${SCH_MONTH}" data-action="schedule-change-month">`:''}
    ${SCH_MODE==='dates'?`<div class="schd-nav">
      <button class="btn" data-action="schedule-week-prev" title="Предыдущая неделя" aria-label="Предыдущая неделя">‹</button>
      <span class="schd-week" id="sch-d-label">${esc(schWeekLabel(SCH_WEEK))}</span>
      <button class="btn" data-action="schedule-week-next" title="Следующая неделя" aria-label="Следующая неделя">›</button>
      <button class="btn" data-action="schedule-week-today" title="Вернуться к текущей неделе">Сегодня</button>
    </div>`:''}
    <button class="btn" title="Что реально будет играть на экране в выбранный час" data-action="schedule-simulate">🔎 Симулятор</button>
  `;

  if(SCH_MODE==='week') await renderWeekGrid(view, list);
  else if(SCH_MODE==='dates') await renderDatesGrid(view, list);
  else await renderMonthOverrides(view, list);
}

// ─── Недельная сетка (шаблон) ────────────────────────────────────────────────

const SCH_DOW = ['Пн','Вт','Ср','Чт','Пт','Сб','Вс'];

function schPlColor(pid){
  const palette=['#7fe3c4','#ffd34d','#ff8a5e','#8ab8ff','#d79cff','#7fd0e3','#b6e37f'];
  return palette[pid % palette.length];
}

async function renderWeekGrid(view, list){
  let slots=[];
  try{ slots=await api('/schedule/slots?'+schTargetQS(new URLSearchParams())); }
  catch(e){ view.innerHTML='<div class="empty">Ошибка: '+esc(e.message)+'</div>'; return; }
  const map={}; slots.forEach(s=>{ map[s.day_of_week+':'+(s.hour===null?'all':s.hour)]=s; });

  const tgtName = SCH_TGT==='network' ? 'вся сеть'
    : ((list.find(x=>x.id===SCH_TARGET_ID)||{}).name||'');
  let h=`<div class="muted" style="font-size:12px;margin-bottom:9px;">
    Недельный шаблон — <b style="color:var(--txt);">${esc(tgtName)}</b>.
    Приоритет: экран → группа → вся сеть (экран без своего слота играет групповой, без группового — общесетевой).
    Клик по ячейке — назначить или убрать плейлист. Строка «Весь день» действует на часы без своего слота.
  </div>`;

  const cell = (dow, hour) => {
    const s = map[dow+':'+(hour===null?'all':hour)];
    const label = s ? esc(s.playlist_name.slice(0,10)) : '';
    const style = s ? `background:${hexA(schPlColor(s.playlist_id),.25)};border-color:${schPlColor(s.playlist_id)};` : '';
    return `<td data-action="schedule-week-cell" data-dow="${dow}" data-hour="${hour===null?'':hour}"
      title="${s?esc(s.playlist_name):'пусто'}"
      style="cursor:pointer;font-size:10px;text-align:center;padding:3px 2px;border:0.5px solid var(--border2);border-radius:4px;${style}">${label||'·'}</td>`;
  };

  h+='<div style="overflow-x:auto;"><table style="border-collapse:separate;border-spacing:2px;min-width:640px;">';
  h+='<tr><th style="font-size:11px;"></th>'+SCH_DOW.map((d,di)=>`<th style="font-size:11px;${canWrite()?'cursor:pointer;':''}" ${canWrite()?`data-action="schedule-clear-day" data-dow="${di}" title="Очистить все слоты за ${d}"`:''}>${d}</th>`).join('')+'</tr>';
  h+='<tr><td class="muted" style="font-size:10px;white-space:nowrap;padding-right:6px;">Весь день</td>'
     +SCH_DOW.map((_,d)=>cell(d,null)).join('')+'</tr>';
  for(let hr=0;hr<24;hr++){
    h+=`<tr><td class="muted" style="font-size:10px;text-align:right;padding-right:6px;">${hr}:00</td>`
      +SCH_DOW.map((_,d)=>cell(d,hr)).join('')+'</tr>';
  }
  h+='</table></div>';

  if(canWrite()){
    h+=`<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;">`;
    if(SCH_TGT==='screen') h+=`<button class="btn" data-action="schedule-clone">⧉ Скопировать расписание экрана…</button>`;
    h+=`<button class="btn danger" data-action="schedule-clear-week">🗑 Очистить весь шаблон</button>`;
    h+=`</div>`;
    h+=`<div class="muted" style="font-size:11px;margin-top:6px;">Массовое удаление: «Очистить весь шаблон» убирает все слоты; клик по названию дня в шапке — очистить все слоты за этот день.</div>`;

    // Список назначенных слотов с галочками — удалить несколько конкретных
    if(slots.length){
      h+=`<div class="rhead" data-action="schedule-toggle-list" style="cursor:pointer;user-select:none;margin-top:16px;"><span class="rcaret">▾</span> Назначенные слоты — удалить несколько (${slots.length})</div>`;
      h+=`<div id="sch-slot-list">`;
      slots.slice().sort((a,b)=> (a.day_of_week-b.day_of_week) || ((a.hour===null?-1:a.hour)-(b.hour===null?-1:b.hour)) )
        .forEach(s=>{
          const hourLabel = s.hour===null ? 'весь день' : (s.hour+':00');
          h+=`<label class="cell" style="display:flex;align-items:center;gap:9px;margin-bottom:5px;cursor:pointer;font-size:12px;">
            <input type="checkbox" class="sch-slot-chk" data-dow="${s.day_of_week}" data-hour="${s.hour===null?'':s.hour}" style="width:15px;height:15px;">
            <span style="min-width:120px;">${SCH_DOW[s.day_of_week]}, ${hourLabel}</span>
            <span style="display:inline-block;width:10px;height:10px;border-radius:3px;background:${schPlColor(s.playlist_id)};"></span>
            <span style="flex:1;">${esc(s.playlist_name||('#'+s.playlist_id))}</span>
          </label>`;
        });
      h+=`</div><button class="btn danger" style="margin-top:6px;" data-action="schedule-delete-selected">🗑 Удалить выбранные слоты</button>`;
    }
  }

  view.innerHTML=h;
}

async function setWeekSlot(dow, hour){
  if(!SCH_PLAYLISTS.length){ toast('Сначала создайте плейлист'); return; }
  const names=SCH_PLAYLISTS.map((p,i)=>'№'+(i+1)+': '+p.name).join('\n');
  const hourLabel = hour===null?'весь день':hour+':00';
  const cur = await api('/schedule/slots?'+schTargetQS(new URLSearchParams())).catch(()=>[]);
  const existing = cur.find(s=>s.day_of_week===dow && (s.hour===null?null:s.hour)===(hour===null?null:hour));
  const existingNum = existing ? (SCH_PLAYLISTS.findIndex(p=>p.id===existing.playlist_id)+1) : '';
  const v = prompt('Плейлист на '+SCH_DOW[dow]+', '+hourLabel+':\n'+names+'\n\nВведите № (пусто — убрать слот)', existingNum||'');
  if(v===null) return;
  const q = schTargetQS(new URLSearchParams({day_of_week:dow}));
  if(hour!==null) q.append('hour',hour);
  try{
    if(v.trim()===''){
      await api('/schedule/slot?'+q,{method:'DELETE'});
      toast('Слот убран');
    }else{
      const pl=SCH_PLAYLISTS[parseInt(v,10)-1];
      if(!pl){ toast('Нет плейлиста с таким №'); return; }
      q.append('playlist_id', pl.id);
      await api('/schedule?'+q,{method:'POST'});
      toast('Слот задан');
    }
    viewSchedule();
  }catch(e){ toast('Ошибка: '+e.message); }
}

// ─── «По датам»: реальная неделя экрана с отправленными показами ─────────────
// Неделя — GET /placements/week: 7 дней × 24 часа, у каждой клетки kind (шаблон, показ,
// «по очереди», переопределение, «выключено», пусто). Клик по клетке показа открывает под
// сеткой карточку (GET /placements?playlist_id=) с кнопками «Открыть плейлист» / «Изменить» /
// «Остановить». «Изменить» — окно js/views/placement-editor.js; после его сохранения приходит
// событие placements-changed (слушатель — в initScheduleViewActions).

const SCH_TZ = 'Europe/Moscow';   // «сегодня» и неделя — по Москве, а не по часам браузера
const SCH_MSK_DATE = new Intl.DateTimeFormat('en-CA', {timeZone: SCH_TZ, year: 'numeric', month: '2-digit', day: '2-digit'});

// kind клетки -> класс и подпись; в легенде те же классы
const SCH_D_KIND = {
  template:  {cls: 'tpl', label: 'недельный шаблон'},
  placement: {cls: 'plc', label: 'показ по датам'},
  alternate: {cls: 'alt', label: 'по очереди'},
  override:  {cls: 'ovr', label: 'плейлист на дату'},
  off:       {cls: 'off', label: 'выключено'},
  empty:     {cls: 'emp', label: 'ничего не играет'},
};
const SCH_D_STATUS = {
  active:  {text: 'идёт сейчас',  cls: 'now'},
  planned: {text: 'запланирован', cls: 'plan'},
  done:    {text: 'закончился',   cls: 'done'},
  stopped: {text: 'остановлен',   cls: 'stop'},
};
const SCH_D_LEGEND = `<div class="schd-lg">
  <span><i class="schd-tpl"></i>обычный недельный шаблон</span>
  <span><i class="schd-plc"></i>📅 отправленный показ (по датам)</span>
  <span><i class="schd-alt"></i>⇄ по очереди с другим плейлистом</span>
  <span><i class="schd-ovr"></i>плейлист на дату (переопределение)</span>
  <span><i class="schd-off"></i>выключено на дату</span>
  <span><i class="schd-emp"></i>ничего не играет</span>
</div>`;

function schTodayMsk(){
  const parts = SCH_MSK_DATE.formatToParts(new Date());
  const g = type => parts.find(p => p.type === type).value;
  return g('year') + '-' + g('month') + '-' + g('day');
}
function schAddDays(iso, n){
  const d = new Date(iso + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
function schMondayOf(iso){
  const dow = new Date(iso + 'T00:00:00Z').getUTCDay();   // 0 = воскресенье
  return schAddDays(iso, -((dow + 6) % 7));
}

// ГГГГ-ММ-ДД -> ДД.ММ / ДД.ММ.ГГГГ (строкой, без часовых поясов)
function schDm(iso){ const s = String(iso || ''); return s.slice(8, 10) + '.' + s.slice(5, 7); }
function schDmy(iso){ return schDm(iso) + '.' + String(iso || '').slice(0, 4); }
function schHH(n){ return String(n).padStart(2, '0'); }
// «02.11 – 08.11.2026»; год у начала — только если он другой
function schDates(a, b){
  if(a === b) return schDmy(a);
  return (String(a).slice(0, 4) === String(b).slice(0, 4) ? schDm(a) : schDmy(a)) + ' – ' + schDmy(b);
}
function schWeekLabel(monday){ return schDates(monday, schAddDays(monday, 6)); }

// api() бросает Error(тело ответа); у FastAPI 400/404/409 — {"detail": "текст"}
function schErrText(e){
  const t = String((e && e.message) || e || '');
  try{
    const j = JSON.parse(t);
    if(j && typeof j.detail === 'string') return j.detail;
  }catch(_){}
  return 'Ошибка: ' + t;
}

function schCellText(c){
  const names = c.playlists || [];
  switch(c.kind){
    case 'placement': return '📅 ' + (names[0] || '');
    case 'alternate': return '⇄ ' + names.join(' / ');
    case 'off':       return 'выкл';
    case 'template':
    case 'override':  return names[0] || '';
  }
  return '·';
}

function schCellHtml(c, date, hour, dowIdx){
  c = c || {kind: 'empty', playlists: [], playlist_ids: [], placement_id: null};
  const k = SCH_D_KIND[c.kind] || SCH_D_KIND.empty;
  const names = c.playlists || [];
  const clickable = (c.kind === 'placement' || c.kind === 'alternate') && c.placement_id != null && (c.playlist_ids || []).length > 0;
  const title = SCH_DOW[dowIdx] + ' ' + schDm(date) + ', ' + hour + ':00 — ' + k.label
    + (names.length ? ': ' + names.join(' / ') : '')
    + (clickable ? ' · нажмите, чтобы открыть карточку показа' : '');
  const text = esc(schCellText(c));
  if(!clickable) return `<td class="schd-c schd-${k.cls}" title="${esc(title)}">${text}</td>`;
  const sel = !!SCH_D_PICK && SCH_D_PICK.date === date && SCH_D_PICK.hour === hour;
  return `<td class="schd-c schd-${k.cls}${sel ? ' sel' : ''}"><button type="button" class="schd-cb"
    data-action="schedule-cell-open" data-placement-id="${Number(c.placement_id)}" data-playlist-id="${Number(c.playlist_ids[0])}"
    data-date="${esc(date)}" data-hour="${hour}" aria-pressed="${sel}" title="${esc(title)}">${text}</button></td>`;
}

function schWeekTableHtml(data){
  const days = data.days || [];
  const today = schTodayMsk();
  const byKey = new Map();
  (data.cells || []).forEach(c => byKey.set(c.date + ':' + c.hour, c));
  SCH_D_CELLS = byKey;

  let h = '<table class="schd-tbl"><colgroup><col style="width:44px">' + '<col>'.repeat(days.length) + '</colgroup>';
  h += '<tr><th></th>' + days.map((d, i) =>
    `<th class="${d === today ? 'today' : ''}"${d === today ? ' aria-current="date"' : ''}>${SCH_DOW[i]}<i>${esc(schDm(d))}</i></th>`).join('') + '</tr>';
  for(let hr = 0; hr < 24; hr++){
    h += `<tr><td class="schd-h">${hr}:00</td>`
      + days.map((d, i) => schCellHtml(byKey.get(d + ':' + hr), d, hr, i)).join('') + '</tr>';
  }
  return h + '</table>';
}

async function renderDatesGrid(view, list){
  SCH_D_LIST = list;
  SCH_D_CSEQ++; SCH_D_CARD = null;     // каркас строится заново — карточка перечитается по выбранной клетке
  const scr = (list.find(x => x.id === SCH_TARGET_ID) || {}).name || '';
  view.innerHTML = `<div class="muted schd-note">
    Экран — <b style="color:var(--txt);">${esc(scr)}</b>. Реальная неделя с датами: что играет в каждый час (МСК).
    Отправленные показы идут поверх недельного шаблона. Нажмите на клетку показа — откроется его карточка.
  </div>
  <div id="sch-d-grid" class="schd-scroll"><div class="empty">Загрузка…</div></div>
  ${SCH_D_LEGEND}
  <div id="sch-d-card" aria-live="polite"></div>`;
  await schDatesLoad();
}

// Загрузить неделю в сетку (первый раз, при листании и после любого изменения показов)
async function schDatesLoad(){
  const box = document.getElementById('sch-d-grid');
  if(!box) return;
  const seq = ++SCH_D_SEQ;
  const week = SCH_WEEK, screenId = SCH_TARGET_ID;
  box.classList.add('busy');
  let data;
  try{
    data = await api('/placements/week?screen_id=' + encodeURIComponent(screenId) + '&start=' + encodeURIComponent(week));
  }catch(e){
    // экран переключили, неделю пролистали или раздел закрыли — ответ уже не нужен
    if(seq !== SCH_D_SEQ || !box.isConnected) return;
    box.classList.remove('busy');
    SCH_D_CELLS = new Map();
    box.innerHTML = '<div class="empty">Не удалось получить неделю: ' + esc(schErrText(e)) + '</div>';
    schCardClear();
    return;
  }
  if(seq !== SCH_D_SEQ || !box.isConnected) return;
  box.classList.remove('busy');
  box.innerHTML = schWeekTableHtml(data);
  schCardSync();
}

// Листание: days = ±7, null — к текущей неделе (по Москве)
function schWeekGo(days){
  SCH_WEEK = days === null ? schMondayOf(schTodayMsk()) : schAddDays(SCH_WEEK, days);
  SCH_D_PICK = null;
  schCardClear();
  const label = document.getElementById('sch-d-label');
  if(label) label.textContent = schWeekLabel(SCH_WEEK);
  schDatesLoad();
}

// Подсветить выбранную клетку (и снять подсветку с остальных)
function schMarkPick(){
  document.querySelectorAll('#sch-d-grid .schd-cb').forEach(b => {
    const on = !!SCH_D_PICK && b.dataset.date === SCH_D_PICK.date && Number(b.dataset.hour) === SCH_D_PICK.hour;
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.parentElement.classList.toggle('sel', on);
  });
}

function schCardClear(){
  SCH_D_CSEQ++;
  SCH_D_CARD = null;
  const box = document.getElementById('sch-d-card');
  if(box) box.innerHTML = '';
  schMarkPick();
}

// После перерисовки сетки: выбранная клетка всё ещё в показе — перечитать карточку, иначе убрать
function schCardSync(){
  const p = SCH_D_PICK;
  const cell = p && SCH_D_CELLS.get(p.date + ':' + p.hour);
  if(!p || p.screenId !== SCH_TARGET_ID || !cell || cell.placement_id !== p.placementId){
    SCH_D_PICK = null;
    schCardClear();
    return;
  }
  schMarkPick();
  schCardLoad();
}

function schCellOpen(el){
  const placementId = Number(el.dataset.placementId);
  if(!placementId) return;
  SCH_D_PICK = {screenId: SCH_TARGET_ID, placementId, date: el.dataset.date, hour: Number(el.dataset.hour)};
  schMarkPick();
  schCardLoad(true);
}

// Карточка показа выбранной клетки. Строку показа берём из списка показов плейлиста:
// первым в клетке стоит плейлист новейшего показа (его placement_id), остальные — «по очереди».
// reveal — после клика: сетка высокая (24 часа), карточка под ней может оказаться за краем окна
async function schCardLoad(reveal){
  const box = document.getElementById('sch-d-card');
  const pick = SCH_D_PICK;
  const cell = pick && SCH_D_CELLS.get(pick.date + ':' + pick.hour);
  if(!box || !cell) return;
  const seq = ++SCH_D_CSEQ;
  if(!SCH_D_CARD || SCH_D_CARD.row.id !== pick.placementId){
    SCH_D_CARD = null;
    box.innerHTML = '<div class="schd-card"><span class="muted">Загрузка…</span></div>';
  }
  let row = null, name = '';
  try{
    const ids = cell.playlist_ids || [];
    for(let i = 0; i < ids.length && !row; i++){
      const rows = await api('/placements?playlist_id=' + encodeURIComponent(ids[i]));
      if(seq !== SCH_D_CSEQ || !box.isConnected) return;
      row = (Array.isArray(rows) ? rows : []).find(r => r.id === pick.placementId) || null;
      if(row) name = (cell.playlists || [])[i] || ('Плейлист #' + ids[i]);
    }
  }catch(e){
    if(seq !== SCH_D_CSEQ || !box.isConnected) return;
    SCH_D_CARD = null;
    box.innerHTML = '<div class="schd-card"><span class="muted">Не удалось получить показ: ' + esc(schErrText(e)) + '</span></div>';
    return;
  }
  if(seq !== SCH_D_CSEQ || !box.isConnected) return;
  if(!row){
    SCH_D_CARD = null;
    box.innerHTML = '<div class="schd-card"><span class="muted">Показ не найден — возможно, его уже отменили.</span></div>';
    return;
  }
  SCH_D_CARD = {row, name, cell};
  box.innerHTML = schCardHtml(SCH_D_CARD);
  if(reveal) box.scrollIntoView({block: 'nearest'});
}

// Экраны показа одной строкой (обычный текст, без esc)
function schWhere(row){
  if(row.all_screens) return 'все экраны сети · ' + row.screen_count;
  const names = (row.screens || []).map(s => s.name);
  return names.length > 3 ? names.slice(0, 3).join(', ') + ' и ещё ' + (names.length - 3) : names.join(', ');
}

function schCardActions(row){
  const id = Number(row.id), busy = SCH_D_BUSY.has(id);
  const btn = (action, label, cls) =>
    `<button type="button" class="btn${cls ? ' ' + cls : ''} schd-btn" data-action="${action}" data-placement-id="${id}"${busy ? ' disabled' : ''}>${label}</button>`;
  let h = btn('schedule-plc-open-playlist', 'Открыть плейлист');
  if(!canWrite()) return h;
  switch(row.status){
    case 'active':  h += btn('schedule-plc-edit', 'Изменить') + btn('schedule-plc-stop', 'Остановить', 'danger'); break;
    case 'planned': h += btn('schedule-plc-edit', 'Изменить') + btn('schedule-plc-cancel', 'Отменить', 'danger'); break;
    case 'done':
    case 'stopped': h += btn('schedule-plc-repeat', 'Повторить'); break;
  }
  return h;
}

function schCardHtml(card){
  const {row, name, cell} = card;
  const st = SCH_D_STATUS[row.status] || {text: row.status, cls: 'done'};
  const scr = (SCH_D_LIST.find(x => x.id === SCH_TARGET_ID) || {}).name || '';
  const alt = cell.kind === 'alternate' && (cell.playlists || []).length > 1
    ? `<div class="plb-alt">⇄ В этот час по очереди: ${esc(cell.playlists.join(' / '))}</div>` : '';
  return `<div class="schd-card">
    <div class="schd-card-h"><b>📅 ${esc(name)}${scr ? ' — ' + esc(scr) : ''}</b>
      <button type="button" class="schd-x" data-action="schedule-plc-close" aria-label="Закрыть карточку" title="Закрыть">✕</button></div>
    <div>${esc(schDates(row.date_from, row.date_to))}, ${schHH(row.hour_from)}:00–${schHH(row.hour_to)}:00</div>
    <div>Экраны: ${esc(schWhere(row))} · <span class="plb-st ${st.cls}">${esc(st.text)}</span></div>
    ${alt}
    ${row.created_by ? `<div>Отправил: ${esc(row.created_by)}, ${esc(schDmy(String(row.created_at || '').slice(0, 10)))}</div>` : ''}
    <div class="schd-acts">${schCardActions(row)}</div>
  </div>`;
}

// «Открыть плейлист»: раздел «Плейлисты» и карточка этого плейлиста (nav возвращает промис списка —
// ждём его, иначе список может дорисоваться поверх карточки)
async function schPlcOpenPlaylist(){
  const c = SCH_D_CARD;
  if(!c) return;
  await Signage.nav('playlists');
  Signage.openPlaylist(c.row.playlist_id, c.name);
}

function schPlcEdit(repeat){
  const c = SCH_D_CARD;
  if(!c || !canWrite()) return;
  if(typeof Signage.openPlacementEditor !== 'function'){ toast('Окно отправки на экраны не загружено'); return; }
  Signage.openPlacementEditor({playlistId: c.row.playlist_id, name: c.name,
    placementId: repeat ? undefined : c.row.id, repeatFrom: repeat ? c.row.id : undefined});
}

// Запрос по одному показу: пока он идёт, кнопки карточки заблокированы; по итогу (и успех, и 409
// «статус уже другой») неделя и карточка перечитываются с сервера
async function schPlcRequest(row, method, path, okText){
  const id = row.id;
  if(SCH_D_BUSY.has(id)) return;
  SCH_D_BUSY.add(id);
  document.querySelectorAll('#sch-d-card button').forEach(b => { b.disabled = true; });
  try{
    await api(path, {method});
    toast(okText);
  }catch(e){
    if(!TOKEN) return;                   // сессия истекла — api() уже показал вход
    toast(schErrText(e));
  }finally{
    SCH_D_BUSY.delete(id);
  }
  schDatesLoad();
}

function schPlcStop(){
  const c = SCH_D_CARD;
  if(!c || !canWrite() || SCH_D_BUSY.has(c.row.id)) return;
  if(!confirm('Остановить показ?\n\n' + schDescribe(c)
    + '\n\nС этого момента плейлист на экранах идти не будет. Уже прошедшие часы останутся в истории.')) return;
  return schPlcRequest(c.row, 'POST', '/placements/' + c.row.id + '/stop', 'Показ остановлен');
}

function schPlcCancel(){
  const c = SCH_D_CARD;
  if(!c || !canWrite() || SCH_D_BUSY.has(c.row.id)) return;
  if(!confirm('Отменить запланированный показ?\n\n' + schDescribe(c) + '\n\nПоказ будет удалён.')) return;
  return schPlcRequest(c.row, 'DELETE', '/placements/' + c.row.id, 'Показ отменён');
}

// Показ одной строкой для текста подтверждения (обычный текст — esc не нужен)
function schDescribe(c){
  const r = c.row;
  return '«' + c.name + '» — ' + schWhere(r) + ', ' + schDates(r.date_from, r.date_to)
    + ', ' + schHH(r.hour_from) + ':00–' + schHH(r.hour_to) + ':00';
}

// ─── Календарь дат (переопределения) — как раньше ────────────────────────────

async function renderMonthOverrides(view, list){
  try{
    const q=new URLSearchParams({month:SCH_MONTH}); q.append(SCH_TGT==='screen'?'screen_id':'group_id',SCH_TARGET_ID);
    const overrides=await api('/schedule/overrides?'+q);
    const ovMap={}; overrides.forEach(o=>{ ovMap[o.on_date.slice(8,10).replace(/^0/,'')]=o; });
    const [y,m]=SCH_MONTH.split('-').map(Number);
    const days=new Date(y,m,0).getDate();
    let firstDow=new Date(y,m-1,1).getDay(); firstDow=(firstDow+6)%7; // Пн=0
    let h=`<div class="muted" style="font-size:12px;margin-bottom:10px;">${SCH_TGT==='screen'?'Экран':'Группа'}: <b style="color:var(--txt);">${esc((list.find(x=>x.id===SCH_TARGET_ID)||{}).name||'')}</b> — ${SCH_MONTH}. Нажмите день, чтобы задать плейлист на дату (переопределение поверх недельного шаблона).</div>`;
    h+='<div class="cal">'+SCH_DOW.map(d=>`<div class="dow">${d}</div>`).join('');
    for(let i=0;i<firstDow;i++) h+='<div></div>';
    for(let day=1;day<=days;day++){
      const o=ovMap[day];
      const tag=o?(o.is_off?'выкл':('плейлист #'+o.playlist_id)):'—';
      h+=`<div class="calc" data-action="schedule-set-override" data-day="${day}" ${o?'style="border-color:var(--c-coca);border-width:1.5px;"':''}>
        <div class="d">${day}</div><span class="calt" style="background:${o?hexA('#ff6b5e',.4):'#262b34'};color:${o?'#fff':'var(--dim)'};">${tag}</span>
        ${o?'<span class="ovr"></span>':''}</div>`;
    }
    h+='</div>';
    h+=`<div style="display:flex;gap:8px;margin-top:14px;"><button class="btn" data-action="schedule-clear-overrides">Сбросить переопределения месяца</button></div>`;
    view.innerHTML=h;
  }catch(e){ view.innerHTML='<div class="empty">Ошибка: '+esc(e.message)+'</div>'; }
}

function initScheduleViewActions(){
  if(window.__scheduleViewActionsInitialized) return;
  window.__scheduleViewActionsInitialized = true;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('schedule-')) return;

    switch(action){
      case 'schedule-target-screen':
        SCH_TGT = 'screen'; SCH_TARGET_ID = null;
        return Signage.viewSchedule();

      case 'schedule-target-group':
        SCH_TGT = 'group'; SCH_TARGET_ID = null;
        return Signage.viewSchedule();

      case 'schedule-target-network':
        SCH_TGT = 'network'; SCH_TARGET_ID = null; SCH_MODE='week';
        return Signage.viewSchedule();

      case 'schedule-mode-week':
        SCH_MODE = 'week';
        return Signage.viewSchedule();

      case 'schedule-mode-month':
        SCH_MODE = 'month';
        return Signage.viewSchedule();

      case 'schedule-mode-dates':
        SCH_MODE = 'dates';
        SCH_WEEK = schMondayOf(schTodayMsk());   // входим в режим на текущей неделе
        SCH_D_PICK = null;
        return Signage.viewSchedule();

      case 'schedule-week-prev':
        return schWeekGo(-7);

      case 'schedule-week-next':
        return schWeekGo(7);

      case 'schedule-week-today':
        return schWeekGo(null);

      case 'schedule-cell-open':
        return schCellOpen(el);

      case 'schedule-plc-close':
        SCH_D_PICK = null;
        return schCardClear();

      case 'schedule-plc-open-playlist':
        return schPlcOpenPlaylist();

      case 'schedule-plc-edit':
        return schPlcEdit(false);

      case 'schedule-plc-repeat':
        return schPlcEdit(true);

      case 'schedule-plc-stop':
        return schPlcStop();

      case 'schedule-plc-cancel':
        return schPlcCancel();

      case 'schedule-week-cell': {
        if(!canWrite()) return;
        const dow = Number(el.dataset.dow);
        const hour = el.dataset.hour === '' ? null : Number(el.dataset.hour);
        return Signage.setWeekSlot(dow, hour);
      }

      case 'schedule-set-override': {
        const day = Number(el.dataset.day);
        return Signage.setOverride(day);
      }

      case 'schedule-clear-overrides':
        return Signage.clearOverrides();

      case 'schedule-clear-week':
        return Signage.clearWeekSlots();

      case 'schedule-clear-day': {
        if(!canWrite()) return;
        return Signage.clearWeekDay(Number(el.dataset.dow));
      }

      case 'schedule-delete-selected':
        return Signage.deleteSelectedSlots();

      case 'schedule-toggle-list': {
        const box=document.getElementById('sch-slot-list');
        const car=el.querySelector('.rcaret');
        if(box){ const hide=box.style.display!=='none'; box.style.display=hide?'none':''; if(car) car.textContent=hide?'▸':'▾'; }
        return;
      }

      case 'schedule-clone':
        return Signage.cloneSchedule();

      case 'schedule-simulate':
        return Signage.openSimulator();

      case 'schedule-simulate-run':
        return Signage.runSimulator();
    }
  });

  // Показ создан, изменён или остановлен в окне «Куда и когда» — обновим неделю, если она на экране
  document.addEventListener('placements-changed', () => {
    if(SCH_MODE === 'dates' && document.getElementById('sch-d-grid')) schDatesLoad();
  });

  document.addEventListener('change', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('schedule-')) return;

    switch(action){
      case 'schedule-select-target':
        SCH_TARGET_ID = Number(el.value);
        return Signage.viewSchedule();

      case 'schedule-change-month':
        SCH_MONTH = el.value;
        return Signage.viewSchedule();
    }
  });
}

async function setOverride(day){
  if(!SCH_PLAYLISTS.length){ try{ SCH_PLAYLISTS=await api('/playlists'); }catch(e){} }
  if(!SCH_PLAYLISTS.length){ toast('Сначала создайте плейлист'); return; }
  const names=SCH_PLAYLISTS.map((p,i)=>'№'+(i+1)+': '+p.name).join('\n');
  const num=prompt('Плейлист на '+day+'.'+SCH_MONTH.split('-')[1]+' — введите №:\n'+names+'\n(пусто = выключить показ)');
  if(num===null) return;
  const date=SCH_MONTH+'-'+String(day).padStart(2,'0');
  const q=new URLSearchParams({on_date:date}); q.append(SCH_TGT==='screen'?'screen_id':'group_id',SCH_TARGET_ID);
  if(num.trim()===''){ q.append('is_off','true'); }
  else { const pl=SCH_PLAYLISTS[parseInt(num,10)-1]; if(!pl){ toast('Нет плейлиста с таким №'); return; } q.append('playlist_id',pl.id); }
  try{ await api('/schedule/overrides?'+q,{method:'POST'}); toast('Переопределение задано'); viewSchedule(); }
  catch(e){ toast('Ошибка: '+e.message); }
}

async function clearOverrides(){
  if(!confirm('Сбросить все переопределения месяца к недельному шаблону?')) return;
  const q=new URLSearchParams({month:SCH_MONTH}); q.append(SCH_TGT==='screen'?'screen_id':'group_id',SCH_TARGET_ID);
  try{ await api('/schedule/overrides?'+q,{method:'DELETE'}); toast('Сброшено'); viewSchedule(); }catch(e){ toast('Ошибка: '+e.message); }
}

async function openSimulator(){
  const view=document.getElementById('view');
  let screens=[];
  try{ screens=await api('/minipc'); }catch(e){}
  if(!screens.length){ toast('Нет экранов'); return; }
  const today=new Date().toISOString().slice(0,10);
  const hour=new Date().getHours();
  const box=document.createElement('div');
  box.id='sch-sim';
  box.innerHTML=`<div class="cell" style="margin-bottom:12px;">
    <div style="font-weight:600;margin-bottom:8px;">🔎 Симулятор эфира</div>
    <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;">
      <div class="fld" style="margin:0;"><label>Экран</label>
        <select class="inp" id="sim-screen" style="width:auto;">${screens.map(s=>`<option value="${s.id}">${esc(s.name)}</option>`).join('')}</select></div>
      <div class="fld" style="margin:0;"><label>Дата</label><input class="inp" type="date" id="sim-date" value="${today}" style="width:auto;"></div>
      <div class="fld" style="margin:0;"><label>Час (МСК)</label><input class="inp" type="number" min="0" max="23" id="sim-hour" value="${hour}" style="width:70px;"></div>
      <button class="btn primary" data-action="schedule-simulate-run">Показать</button>
    </div>
    <div id="sim-result" style="margin-top:10px;"></div>
  </div>`;
  const old=document.getElementById('sch-sim');
  if(old){ old.remove(); return; }
  view.prepend(box);
}

async function runSimulator(){
  const out=document.getElementById('sim-result');
  if(!out) return;
  out.innerHTML='<span class="muted">Считаем…</span>';
  try{
    const q=new URLSearchParams({screen_id:val('sim-screen'), on_date:val('sim-date'), hour:val('sim-hour')});
    const r=await api('/schedule/simulate?'+q);
    const srcColor={override:'#ffd34d', override_off:'var(--danger)', slot:'var(--accent)',
                    fallback:'#ffd34d', black:'var(--danger)'}[r.source]||'var(--txt)';
    let h=`<div style="font-size:13px;margin-bottom:6px;">Источник:
      <b style="color:${srcColor};">${esc(r.source_label)}</b>
      ${r.playlist?` · плейлист «${esc(r.playlist)}»`:''}</div>`;
    if(r.items.length){
      h+='<ol style="margin:4px 0 4px 18px;font-size:12px;">'
        + r.items.map(i=>`<li>${esc(i.title)}</li>`).join('') + '</ol>';
    }
    (r.notes||[]).forEach(n=>{ h+=`<div style="font-size:11px;color:#ffd34d;margin-top:4px;">⚠ ${esc(n)}</div>`; });
    out.innerHTML=h;
  }catch(e){ out.innerHTML='<span style="color:var(--danger);">Ошибка: '+esc(e.message)+'</span>'; }
}

async function cloneSchedule(){
  // Источник — выбранный сейчас экран (SCH_TGT==='screen')
  let screens=[], groups=[];
  try{ [screens,groups]=await Promise.all([api('/minipc'),api('/groups')]); }catch(e){ toast('Ошибка: '+e.message); return; }
  const others = screens.filter(s=>s.id!==SCH_TARGET_ID);
  let msg = 'Скопировать расписание на:\n';
  others.forEach(s=>{ msg += 'э'+s.id+': '+s.name+'\n'; });
  groups.forEach(g=>{ msg += 'г'+g.id+': '+g.name+' (вся группа)\n'; });
  msg += '\nВведите код (например э2 или г1). Расписание цели будет ЗАМЕНЕНО.';
  const ans = prompt(msg);
  if(!ans) return;
  const m = ans.trim().match(/^([эг])(\d+)$/i);
  if(!m){ toast('Введите код вида э2 или г1'); return; }
  const q = new URLSearchParams({from_screen_id: SCH_TARGET_ID});
  q.append(m[1].toLowerCase()==='э' ? 'to_screen_id' : 'to_group_id', m[2]);
  try{
    const res = await api('/schedule/clone?'+q, {method:'POST'});
    toast('Скопировано: '+res.slots_copied+' слот(ов) на '+res.targets.length+' экран(ов)');
  }catch(e){ toast('Ошибка: '+e.message); }
}

async function clearWeekSlots(){
  if(!confirm('Очистить ВЕСЬ недельный шаблон для выбранной цели? Все слоты будут удалены.')) return;
  try{
    const r=await api('/schedule/slots/clear?'+schTargetQS(new URLSearchParams()), {method:'DELETE'});
    toast('Очищено слотов: '+(r.removed!=null?r.removed:'—'));
    viewSchedule();
  }catch(e){ toast('Ошибка: '+e.message); }
}

async function clearWeekDay(dow){
  if(!confirm('Очистить все слоты за '+SCH_DOW[dow]+'?')) return;
  try{
    const q=schTargetQS(new URLSearchParams()); q.append('day_of_week', dow);
    const r=await api('/schedule/slots/clear?'+q, {method:'DELETE'});
    toast('Очищено за '+SCH_DOW[dow]+': '+(r.removed!=null?r.removed:'—'));
    viewSchedule();
  }catch(e){ toast('Ошибка: '+e.message); }
}

async function deleteSelectedSlots(){
  const chks=[...document.querySelectorAll('.sch-slot-chk:checked')];
  if(!chks.length){ toast('Ничего не выбрано'); return; }
  if(!confirm('Удалить выбранные слоты ('+chks.length+')?')) return;
  let ok=0, fail=0;
  for(const c of chks){
    const q=schTargetQS(new URLSearchParams()); q.append('day_of_week', c.dataset.dow);
    if(c.dataset.hour!=='') q.append('hour', c.dataset.hour);
    try{ await api('/schedule/slot?'+q, {method:'DELETE'}); ok++; }
    catch(e){ fail++; }
  }
  toast('Удалено слотов: '+ok+(fail?', ошибок: '+fail:''));
  viewSchedule();
}

window.Signage = window.Signage || {};
window.Signage.viewSchedule = viewSchedule;
window.Signage.clearWeekSlots = clearWeekSlots;
window.Signage.clearWeekDay = clearWeekDay;
window.Signage.deleteSelectedSlots = deleteSelectedSlots;
window.Signage.setWeekSlot = setWeekSlot;
window.Signage.setOverride = setOverride;
window.Signage.clearOverrides = clearOverrides;
window.Signage.cloneSchedule = cloneSchedule;
window.Signage.openSimulator = openSimulator;
window.Signage.runSimulator = runSimulator;
initScheduleViewActions();
