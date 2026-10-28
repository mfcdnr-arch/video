//=============================================================================
// ОКНО «КУДА И КОГДА»: шаг 2 — отправка плейлиста на экраны (создание показа)
//
//   Signage.openPlacementEditor({playlistId, name?, placementId?, repeatFrom?})
//     playlistId  — плейлист, который отправляем (обязателен);
//     name        — его название для заголовка (нет — берём из GET /playlists);
//     placementId — правка показа: окно открывается со всеми значениями показа,
//                   сохранение — PUT /placements/{id}, предпросмотр с exclude_id;
//     repeatFrom  — «Повторить» закончившийся/остановленный показ: подставляются
//                   его экраны и часы, даты пустые; сохранение — новый показ.
//
// Слева: экраны («Все экраны сети (N)» / «Выбрать экраны» — список с галочками,
// поиском по названию и адресу, «ё» = «е», точкой «в сети»), даты (не раньше
// сегодняшней по Москве) и целые часы «с — до». Справа: пересечения с другими
// показами и датовыми переопределениями — по каждому админ обязан выбрать
// «Заменить на это время», «Крутить по очереди» или «Оставить «X» на это время»
// (в эти часы идёт только прежний плейлист), — предупреждения без выбора
// (экран выключен в эти дни, «Эфир сети», реклама не помещается в час) и итог
// одной фразой. «Отправить» неактивна, пока есть пересечение без решения.
//
// При каждом изменении через 300 мс уходит POST /placements/preview. Ответ на
// устаревший запрос отбрасывается: счётчик seq растёт в момент, когда изменение
// ЗАПЛАНИРОВАНО, а не когда запрос ушёл. Пока предпросмотр не пришёл, отправить
// нельзя (решения относятся к пересечениям из ответа). Решения хранятся по ключу
// «экран:вид:id» и переживают пересчёт; те, чьё пересечение исчезло, отбрасываются.
// При правке предпросмотр отдаёт и действующие решения (decisions): свои сохранённые
// или — если другой показ сохранили позже — его решение о правленом, перевёрнутое
// («заменил» нас → «Оставить»). Они подставляются в выбор, как будто админ выбрал их
// сам, но только туда, где выбора ещё нет: выбранное в окне пересчёт не перебивает.
// Пока идёт отправка, кнопка заблокирована, форма заморожена, а Esc/✕/клик по
// фону/«Отмена» не закрывают окно (двойной клик = один показ).
//
// После успеха: тост, закрытие окна и событие placements-changed {playlistId}.
//=============================================================================
(function(){
  const PREVIEW_DELAY = 300;          // мс после последнего изменения до /placements/preview
  const TZ = 'Europe/Moscow';         // «сегодня» считаем по Москве, а не по часам браузера
  const REPLACE = 'replace';
  const ALTERNATE = 'alternate';
  const YIELD = 'yield';
  const MODES = [REPLACE, ALTERNATE, YIELD];
  const OFF_DAYS_SHOWN = 8;           // сколько дат «экран выключен» перечислять в строке

  const STOPPED_TEXT = 'Остановленный показ изменить нельзя — создайте новый';

  let S = null;                       // состояние открытого окна (оно может быть только одно)

  const $ = id => document.getElementById(id);

  // ---- мелочи ---------------------------------------------------------------

  function plural(n, one, few, many){
    const m10 = n % 10, m100 = n % 100;
    if(m10 === 1 && m100 !== 11) return one;
    if(m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }

  // Поиск без учёта регистра и «ё» = «е»: «весла» находит «вёсла»
  function norm(s){
    return String(s == null ? '' : s).toLowerCase().replace(/ё/g, 'е');
  }

  // Название с подсветкой совпадения поиска (qn — уже нормализованный запрос)
  function highlight(text, qn){
    text = String(text == null ? '' : text);
    if(!qn) return esc(text);
    const n = norm(text);
    if(n.length !== text.length) return esc(text);   // экзотический регистр сдвигает индексы
    let out = '', pos = 0, i;
    while((i = n.indexOf(qn, pos)) >= 0){
      out += esc(text.slice(pos, i)) + '<mark>' + esc(text.slice(i, i + qn.length)) + '</mark>';
      pos = i + qn.length;
    }
    return out + esc(text.slice(pos));
  }

  // api() бросает Error(тело ответа); у FastAPI 400/404/409 — {"detail": "текст"}
  function errText(e){
    const t = String((e && e.message) || e || '');
    try{
      const j = JSON.parse(t);
      if(j && typeof j.detail === 'string') return j.detail;
      if(j && Array.isArray(j.detail)) return 'Проверьте экраны, даты и часы показа';
    }catch(_){}
    return t;
  }

  // Сегодняшняя дата ГГГГ-ММ-ДД по Москве
  const MSK_DATE = new Intl.DateTimeFormat('en-CA', {timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit'});
  function todayMsk(){
    const parts = MSK_DATE.formatToParts(new Date());
    const g = type => parts.find(p => p.type === type).value;
    return `${g('year')}-${g('month')}-${g('day')}`;
  }

  function validIso(v){
    if(!/^\d{4}-\d{2}-\d{2}$/.test(v)) return false;
    const d = new Date(v + 'T00:00:00Z');
    return !isNaN(d) && d.toISOString().slice(0, 10) === v;
  }

  // «ДД.ММ»; год — только если он не текущий
  function fmtDM(iso){
    const s = String(iso || '');
    const dm = s.slice(8, 10) + '.' + s.slice(5, 7);
    return s.slice(0, 4) === todayMsk().slice(0, 4) ? dm : dm + '.' + s.slice(0, 4);
  }

  function fmtRange(a, b){
    return a === b ? fmtDM(a) : fmtDM(a) + '–' + fmtDM(b);
  }

  function hh(n){ return String(n).padStart(2, '0') + ':00'; }
  function fmtHours(a, b){ return hh(a) + '–' + hh(b); }

  function keyOf(c){ return `${c.screen_id}:${c.other_kind}:${c.other_id}`; }

  // ---- проверка формы и тело запросов ------------------------------------------

  // Текст «чего не хватает» или '' — тогда предпросмотр можно запрашивать.
  // Правила те же, что у сервера (routers/placements.py parse_body).
  function validate(st){
    if(!st.screens.length) return 'В сети нет ни одного экрана';
    if(!st.allScreens && !st.picked.size) return 'Выберите хотя бы один экран';
    if(!st.dateFrom || !st.dateTo) return 'Укажите даты показа: с какого числа по какое';
    if(!validIso(st.dateFrom) || !validIso(st.dateTo)) return 'Проверьте даты: нужен формат ДД.ММ.ГГГГ';
    if(st.dateFrom < todayMsk() && st.dateFrom !== st.origDateFrom) return 'Дата начала уже прошла';
    if(st.dateTo < st.dateFrom) return 'Дата окончания раньше даты начала';
    if(!(st.hourTo > st.hourFrom)) return 'Время «до» должно быть позже времени «с»';
    return '';
  }

  function formBody(st){
    return {
      playlist_id: st.playlistId,
      all_screens: st.allScreens,
      screen_ids: st.allScreens ? [] : Array.from(st.picked),
      date_from: st.dateFrom, date_to: st.dateTo,
      hour_from: st.hourFrom, hour_to: st.hourTo,
    };
  }

  function unresolvedCount(st){
    return st.conflicts.reduce((n, c) => n + (st.decisions.has(keyOf(c)) ? 0 : 1), 0);
  }

  function isReady(st){
    return st.loaded && !st.busy && !st.blocked && !st.problem && !st.pending
      && st.fresh && !st.errPreview && unresolvedCount(st) === 0;
  }

  // ---- экраны ---------------------------------------------------------------------

  function screenSub(s){
    const parts = [s.city, s.location].filter(Boolean);
    if(!s.online) parts.push(fmtOfflineFor(s.offline_for_seconds));
    return parts.join(' · ');
  }

  function renderScreens(){
    const st = S;
    const qn = norm(st.query.trim());
    const list = qn
      ? st.screens.filter(s => norm([s.name, s.city, s.location].filter(Boolean).join(' ')).includes(qn))
      : st.screens;
    const box = $('plc-scr-list');
    if(!st.screens.length){
      box.innerHTML = '<div class="plc-empty">В сети пока нет экранов. Добавьте экран в разделе «Экраны».</div>';
    }else if(!list.length){
      box.innerHTML = '<div class="plc-empty">Ничего не найдено. Поиск идёт по названию и адресу экрана.</div>';
    }else{
      box.innerHTML = list.map(s => {
        const on = !!s.online;
        const checked = st.picked.has(s.id);
        const sub = screenSub(s);
        return `<label class="plc-scr${checked ? ' on' : ''}">
          <input type="checkbox" data-action="plc-screen" data-screen-id="${s.id}"${checked ? ' checked' : ''}>
          <span class="plc-dot${on ? '' : ' off'}" title="${on ? 'в сети' : 'не в сети'}"></span>
          <span class="plc-scr-n">${highlight(s.name, qn)}${sub ? `<i>${highlight(sub, qn)}</i>` : ''}</span>
        </label>`;
      }).join('');
    }
    refreshPickInfo(list.length);
  }

  function refreshPickInfo(found){
    const st = S;
    let t = `Выбрано ${st.picked.size} из ${st.screens.length}`;
    if(st.query.trim() && found != null) t += ` · найдено ${found}`;
    $('plc-pickinfo').textContent = t;
  }

  function togglePick(id, on){
    if(on) S.picked.add(id); else S.picked.delete(id);
    const cb = S.modal.el.querySelector(`input[data-action="plc-screen"][data-screen-id="${id}"]`);
    if(cb && cb.closest('.plc-scr')) cb.closest('.plc-scr').classList.toggle('on', on);
    refreshPickInfo(S.modal.el.querySelectorAll('input[data-action="plc-screen"]').length);
    onFieldChange(true);
  }

  function setTarget(all){
    S.allScreens = all;
    $('plc-pick').hidden = all;
    onFieldChange(true);
  }

  // ---- пересечения ------------------------------------------------------------------

  function otherName(c){
    return c.other_playlist_name ? `«${c.other_playlist_name}»` : `плейлист №${c.other_playlist_id}`;
  }

  function conflictHtml(c, i){
    const key = keyOf(c);
    const other = esc(otherName(c));
    const hours = fmtHours(c.hour_from, c.hour_to);
    const where = c.other_kind === 'override' ? ' (расписание на эту дату)' : '';
    const screen = c.screen_name || ('Экран #' + c.screen_id);
    const opt = (mode, title, text) => `<label class="plc-opt">
        <input type="radio" name="plc-d-${i}" data-action="plc-decide" data-mode="${mode}" data-key="${esc(key)}">
        <span><b>${title}</b><span class="plc-d">${text}</span></span>
      </label>`;
    return `<div class="plc-conf" data-key="${esc(key)}">
      <div class="plc-conf-h">⚠ <b>${esc(screen)}</b>: ${fmtRange(c.date_from, c.date_to)}, ${hours}
        уже стоит <b>${other}</b>${where}</div>
      <div role="radiogroup" aria-label="Что делать на экране «${esc(screen)}», ${fmtRange(c.date_from, c.date_to)}, ${hours}">
        ${opt(REPLACE, 'Заменить на это время',
          `В ${hours} идёт только новый плейлист, ${other} в эти часы не показывается.`)}
        ${opt(ALTERNATE, 'Крутить по очереди',
          'Ролики обоих плейлистов идут вперемешку в одном часе, заглушки добивают остаток.')}
        ${opt(YIELD, `Оставить ${other} на это время`,
          `В эти часы идёт только ${other}, новый плейлист не показывается.`)}
      </div>
    </div>`;
  }

  function notesHtml(st){
    const rows = [];
    // «Экран выключен» — не выбор, а предупреждение; даты одного экрана собираем в строку
    const byScreen = new Map();
    st.offDays.forEach(d => {
      if(!byScreen.has(d.screen_id)) byScreen.set(d.screen_id, {name: d.screen_name || ('Экран #' + d.screen_id), days: []});
      byScreen.get(d.screen_id).days.push(d.on_date);
    });
    byScreen.forEach(v => {
      const shown = v.days.slice(0, OFF_DAYS_SHOWN).map(fmtDM).join(', ');
      const more = v.days.length > OFF_DAYS_SHOWN ? ` и ещё ${v.days.length - OFF_DAYS_SHOWN}` : '';
      rows.push(`<b>${esc(v.name)}</b>: в эти дни экран выключен, показ в эти дни идти не будет (${esc(shown)}${more})`);
    });
    st.notices.forEach(t => rows.push(esc(t)));
    st.overflow.forEach(t => rows.push(esc(t)));
    return rows.map(r => `<div class="plc-warn">⚠ ${r}</div>`).join('');
  }

  // Пересборка блока справа. Сборка идёт только по ответу предпросмотра; если список
  // пересечений не изменился, DOM не трогаем — фокус на радиокнопке не пропадает.
  function renderResult(){
    const st = S;
    const conf = $('plc-conf');
    const html = st.conflicts.length
      ? `<p class="plc-lbl">Пересечения · ${st.conflicts.length}</p>
         <div class="plc-conflist">${st.conflicts.map(conflictHtml).join('')}</div>
         ${st.conflicts.length > 1 ? `<label class="plc-all"><input type="checkbox" id="plc-apply-all" data-action="plc-apply-all">
           Применить выбранный вариант ко всем пересечениям</label>` : ''}`
      : '<div class="plc-ok">Пересечений с другими показами нет.</div>';
    if(html !== st.lastConfHtml){
      const a = document.activeElement;
      const keep = a && a.dataset && a.dataset.action === 'plc-decide' && conf.contains(a)
        ? {key: a.dataset.key, mode: a.dataset.mode} : null;
      conf.innerHTML = html;
      st.lastConfHtml = html;
      if(keep){
        const again = Array.from(conf.querySelectorAll('input[data-action="plc-decide"]'))
          .find(r => r.dataset.key === keep.key && r.dataset.mode === keep.mode);
        if(again) again.focus();
      }
    }
    syncDecisions();
    $('plc-notes').innerHTML = notesHtml(st);
    const sum = $('plc-summary');
    sum.textContent = st.summary || '';
    sum.hidden = !st.summary;
  }

  // Отметить радиокнопки по сохранённым решениям (без пересборки)
  function syncDecisions(){
    const st = S;
    st.modal.el.querySelectorAll('input[data-action="plc-decide"]').forEach(r => {
      const on = st.decisions.get(r.dataset.key) === r.dataset.mode;
      r.checked = on;
      const opt = r.closest('.plc-opt');
      if(opt) opt.classList.toggle('on', on);
    });
    const all = $('plc-apply-all');
    if(all) all.checked = st.applyAll;
  }

  function decide(key, mode){
    const st = S;
    st.errSend = '';
    st.decisions.set(key, mode);
    st.lastMode = mode;
    if(st.applyAll) st.conflicts.forEach(c => st.decisions.set(keyOf(c), mode));
    syncDecisions();
    renderState();
  }

  function setApplyAll(on){
    const st = S;
    st.errSend = '';
    st.applyAll = on;
    if(on && st.lastMode && st.decisions.size){
      st.conflicts.forEach(c => st.decisions.set(keyOf(c), st.lastMode));
      syncDecisions();
    }
    renderState();
  }

  // ---- состояние окна: подсказка, затемнение, ошибка, кнопки ------------------------

  function renderState(){
    const st = S;
    if(!st || !st.loaded) return;
    const hint = $('plc-hint');
    const result = $('plc-result');
    if(st.problem){
      hint.textContent = st.problem;
      hint.hidden = false;
      result.hidden = true;
    }else if(st.errPreview || (!st.fresh && !st.hadResult)){
      hint.textContent = st.errPreview ? 'Не удалось проверить пересечения' : 'Проверяем пересечения…';
      hint.hidden = false;
      result.hidden = true;
    }else{
      hint.hidden = true;
      result.hidden = false;
    }
    // Результат прошлого запроса остаётся на экране приглушённым, пока идёт новый
    result.classList.toggle('plc-stale', !st.fresh);

    const err = st.blocked || st.errSend || st.errPreview;
    const box = $('plc-err');
    box.textContent = err || '';
    box.hidden = !err;

    refreshFooter();
  }

  function refreshFooter(){
    const st = S;
    const un = unresolvedCount(st);
    const ready = isReady(st);
    st.btnSend.disabled = !ready;
    st.btnSave.disabled = st.busy;
    if(st.btnBack) st.btnBack.disabled = st.busy;
    let note = '', warn = false;
    if(st.busy){
      note = 'Отправляем…';
    }else if(st.pending){
      note = 'Проверяем пересечения…';
    }else if(!st.problem && st.fresh && un > 0){
      note = `Не решено: ${un} ${plural(un, 'пересечение', 'пересечения', 'пересечений')}`;
      warn = true;
    }
    st.note.textContent = note;
    st.note.classList.toggle('warn', warn);
  }

  // ---- предпросмотр -------------------------------------------------------------------

  // Любое изменение формы: сначала обновить состояние, потом запланировать предпросмотр.
  function onFieldChange(skipRead){
    const st = S;
    if(!st || !st.loaded) return;
    if(!skipRead) readFields();
    st.errSend = '';
    schedulePreview();
  }

  function readFields(){
    const st = S;
    st.dateFrom = $('plc-date-from').value;
    st.dateTo = $('plc-date-to').value;
    st.hourFrom = Number($('plc-hour-from').value);
    st.hourTo = Number($('plc-hour-to').value);
    // «по» не раньше «с» — в календаре нельзя выбрать меньшее
    $('plc-date-to').min = st.dateFrom || st.minFrom;
  }

  function schedulePreview(){
    const st = S;
    st.seq++;                       // ответ на любой прошлый запрос уже не нужен
    clearTimeout(st.timer);
    st.fresh = false;
    st.errPreview = '';
    st.problem = validate(st);
    if(st.problem){
      st.pending = false;
      renderState();
      return;
    }
    st.pending = true;
    renderState();
    st.timer = setTimeout(() => runPreview(st), PREVIEW_DELAY);
  }

  async function runPreview(st){
    if(st.closed) return;
    const seq = st.seq;
    let r = null, err = '';
    try{
      const body = formBody(st);
      if(st.placementId) body.exclude_id = st.placementId;
      r = await api('/placements/preview', {method: 'POST', body: JSON.stringify(body)});
    }catch(e){
      if(String(e && e.message) === 'unauthorized'){ if(!st.closed) st.modal.close(); return; }
      err = errText(e) || 'Не удалось проверить пересечения';
    }
    if(st.closed || seq !== st.seq) return;     // пока ждали, форму изменили
    st.pending = false;
    if(err){
      st.errPreview = err;
      st.fresh = false;
      renderState();
      return;
    }
    // по экранам и датам — порядок не прыгает от пересчёта к пересчёту
    st.conflicts = ((r && r.conflicts) || []).slice().sort((x, y) =>
      String(x.screen_name || '').localeCompare(String(y.screen_name || ''), 'ru', {numeric: true})
      || x.screen_id - y.screen_id || String(x.date_from).localeCompare(String(y.date_from))
      || x.hour_from - y.hour_from || String(x.other_kind).localeCompare(String(y.other_kind))
      || x.other_id - y.other_id);
    st.offDays = (r && r.off_days) || [];
    st.notices = (r && r.notices) || [];
    st.overflow = (r && r.overflow) || [];
    st.summary = (r && r.summary) || '';
    // Решения живут, пока живо их пересечение
    const keys = new Set(st.conflicts.map(keyOf));
    Array.from(st.decisions.keys()).forEach(k => { if(!keys.has(k)) st.decisions.delete(k); });
    // Правка: сохранённые решения показа — туда, где в окне ещё ничего не выбрано
    ((r && r.decisions) || []).forEach(d => {
      const k = keyOf(d);
      if(keys.has(k) && !st.decisions.has(k) && MODES.includes(d.mode)) st.decisions.set(k, d.mode);
    });
    if(!st.decisions.size) st.lastMode = null;
    // «Применить ко всем» не должно молча накрыть пересечение, появившееся после пересчёта
    if(st.applyAll && st.decisions.size && unresolvedCount(st) > 0) st.applyAll = false;
    st.fresh = true;
    st.hadResult = true;
    renderResult();
    renderState();
  }

  // ---- отправка -----------------------------------------------------------------------

  function setBusy(on){
    const st = S;
    st.busy = on;
    st.modal.el.querySelector('.modal-body').inert = on;   // форму на время запроса замораживаем
    renderState();
  }

  async function send(){
    const st = S;
    if(!st || st.busy || !isReady(st)) return;
    setBusy(true);                                   // сразу, до await: второй клик ничего не сделает
    st.errSend = '';
    const body = formBody(st);
    body.decisions = st.conflicts.map(c => ({
      screen_id: c.screen_id, other_kind: c.other_kind, other_id: c.other_id,
      mode: st.decisions.get(keyOf(c)),
    }));
    try{
      if(st.placementId){
        await api('/placements/' + st.placementId, {method: 'PUT', body: JSON.stringify(body)});
      }else{
        await api('/placements', {method: 'POST', body: JSON.stringify(body)});
      }
    }catch(e){
      if(String(e && e.message) === 'unauthorized'){ if(!st.closed) st.modal.close(); return; }
      if(st.closed) return;
      st.errSend = errText(e) || 'Не удалось отправить показ';
      setBusy(false);                                // 400/404/409 приходят русским текстом
      schedulePreview();                             // вдруг пересечения изменились (409)
      st.modal.el.focus({preventScroll: true});      // фокус остаётся в окне
      return;
    }
    toast('Показ отправлен на экраны');
    const playlistId = st.playlistId;
    if(!st.closed) st.modal.close();
    document.dispatchEvent(new CustomEvent('placements-changed', {detail: {playlistId}}));
  }

  // «← Назад»: к шагу 1 — окно роликов этого плейлиста в режиме правки
  function goBack(){
    const st = S;
    if(!st || st.busy) return;
    const {playlistId, playlistName} = st;
    st.modal.close();
    if(typeof Signage.openPlaylistEditor !== 'function') return;
    Signage.openPlaylistEditor({
      playlistId, name: playlistName || undefined,
      onSaved: r => refreshPlaylistsView(playlistId, r && r.name),
    });
  }

  // Список плейлистов / карточка за окном показывают старое название — обновим, если они на экране
  function refreshPlaylistsView(playlistId, name){
    try{
      if(typeof CUR_PL !== 'undefined' && CUR_PL && CUR_PL.id === playlistId && Signage.openPlaylist){
        Signage.openPlaylist(playlistId, name || CUR_PL.name);
      }else if(Signage.viewPlaylists && document.querySelector('[data-action="playlists-create"]')){
        Signage.viewPlaylists();
      }
    }catch(e){ console.error('placement-editor refresh:', e); }
  }

  function closeNow(){
    const st = S;
    if(st && !st.busy) st.modal.close();
  }

  // ---- построение окна ---------------------------------------------------------------------

  function hourOptions(from, to, selected){
    let h = '';
    for(let i = from; i <= to; i++) h += `<option value="${i}"${i === selected ? ' selected' : ''}>${hh(i)}</option>`;
    return h;
  }

  function bodyHtml(st){
    return `<div class="plc-grid">
      <div class="plc-col">
        <p class="plc-lbl">Экраны</p>
        <div role="radiogroup" aria-label="На какие экраны отправить">
          <label class="plc-radio"><input type="radio" name="plc-target" data-action="plc-target-all"${st.allScreens ? ' checked' : ''}>
            Все экраны сети <span class="plc-dim">(${st.screens.length})</span></label>
          <label class="plc-radio"><input type="radio" name="plc-target" data-action="plc-target-pick"${st.allScreens ? '' : ' checked'}>
            Выбрать экраны</label>
        </div>
        <div id="plc-pick"${st.allScreens ? ' hidden' : ''}>
          <input class="inp" id="plc-screen-search" autocomplete="off" aria-label="Найти экран по названию или адресу"
            placeholder="🔍 Найти экран по названию или адресу">
          <div class="plc-pickinfo" id="plc-pickinfo" aria-live="polite"></div>
          <div class="plc-scrlist" id="plc-scr-list"></div>
        </div>
        <p class="plc-lbl">Даты показа</p>
        <div class="plc-row">
          <label for="plc-date-from">с</label>
          <input class="inp" type="date" id="plc-date-from" value="${esc(st.dateFrom)}" min="${esc(st.minFrom)}">
          <label for="plc-date-to">по</label>
          <input class="inp" type="date" id="plc-date-to" value="${esc(st.dateTo)}" min="${esc(st.dateFrom || st.minFrom)}">
        </div>
        <p class="plc-lbl">Время показа, каждый день</p>
        <div class="plc-row">
          <label for="plc-hour-from">с</label>
          <select class="inp" id="plc-hour-from">${hourOptions(0, 23, st.hourFrom)}</select>
          <label for="plc-hour-to">до</label>
          <select class="inp" id="plc-hour-to">${hourOptions(1, 24, st.hourTo)}</select>
        </div>
      </div>
      <div class="plc-col">
        <div class="plc-hint" id="plc-hint" hidden></div>
        <div id="plc-result" hidden>
          <div id="plc-conf"></div>
          <div id="plc-notes"></div>
          <div class="plc-summary" id="plc-summary" aria-live="polite" hidden></div>
        </div>
      </div>
    </div>
    <div class="plc-err" id="plc-err" role="alert" hidden></div>`;
  }

  async function openPlacementEditor(opts){
    opts = opts || {};
    if(!canWrite()){ toast('Нет прав на отправку показов'); return; }
    if(S) return;                                  // окно уже открыто (двойной клик)
    const playlistId = Number(opts.playlistId) || 0;
    if(!playlistId){ toast('Не выбран плейлист'); return; }
    const placementId = Number(opts.placementId) || 0;
    const repeatFrom = placementId ? 0 : (Number(opts.repeatFrom) || 0);
    const editing = !!placementId;
    const creating = !editing;                     // новый показ: после шага 1, из карточки или «Повторить»

    const st = S = {
      playlistId, placementId, repeatFrom, playlistName: String(opts.name || ''),
      loaded: false, closed: false, busy: false,
      screens: [], allScreens: false, picked: new Set(), query: '',
      dateFrom: '', dateTo: '', hourFrom: 0, hourTo: 24, origDateFrom: '', minFrom: todayMsk(),
      decisions: new Map(), lastMode: null, applyAll: false,
      conflicts: [], offDays: [], notices: [], overflow: [], summary: '',
      seq: 0, timer: 0, pending: false, fresh: false, hadResult: false, problem: '',
      errSend: '', errPreview: '', blocked: '', lastConfHtml: null,
    };

    const modal = openModal({
      title: 'Куда и когда',
      body: '<div class="empty">Загрузка…</div>',
      footer: `${creating ? '<button type="button" class="btn" id="plc-back" data-action="plc-back">← Назад</button>' : ''}
        <span class="plc-note" id="plc-note" aria-live="polite"></span>
        <button type="button" class="btn" id="plc-save-only" data-action="plc-save-only">${editing ? 'Отмена' : 'Сохранить без отправки'}</button>
        <button type="button" class="btn primary" id="plc-send" data-action="plc-send" aria-describedby="plc-note" disabled>${editing ? 'Сохранить показ' : 'Отправить на экраны'}</button>`,
      width: 980,
      className: 'modal-plc',
      onClose(){
        st.closed = true;
        clearTimeout(st.timer);
        if(S === st) S = null;
      },
    });
    st.modal = modal;
    st.btnBack = $('plc-back');
    st.btnSave = $('plc-save-only');
    st.btnSend = $('plc-send');
    st.note = $('plc-note');

    // Пока идёт отправка, окно не закрывается ни крестиком, ни кликом по фону
    // (Esc гасит общий обработчик ниже). Перехват в capture-фазе — раньше штатного из modal.js.
    const backdrop = modal.el.parentElement;
    backdrop.addEventListener('click', e => {
      if(!st.busy) return;
      const x = e.target.closest && e.target.closest('[data-modal-close]');
      if(e.target === backdrop || (x && modal.el.contains(x))){
        e.stopImmediatePropagation();
        e.preventDefault();
      }
    }, true);

    // Данные: экраны, название плейлиста, при правке/повторе — показы плейлиста
    let screens, plName = st.playlistName, rows = [];
    try{
      [screens, plName, rows] = await Promise.all([
        api('/minipc'),
        opts.name != null ? plName
          : api('/playlists').then(l => ((l || []).find(p => p.id === playlistId) || {}).name || ''),
        (placementId || repeatFrom) ? api('/placements?playlist_id=' + playlistId) : [],
      ]);
    }catch(e){
      if(st.closed) return;
      modal.el.querySelector('.modal-body').innerHTML =
        `<div class="empty">Не удалось загрузить данные: ${esc(errText(e))}</div>`;
      return;
    }
    if(st.closed) return;

    st.screens = (screens || []).slice().sort((a, b) =>
      String(a.name || '').localeCompare(String(b.name || ''), 'ru', {numeric: true}) || a.id - b.id);
    st.playlistName = plName || '';
    const byId = new Map(st.screens.map(s => [s.id, s]));

    let row = null;
    if(placementId || repeatFrom){
      row = (rows || []).find(p => p.id === (placementId || repeatFrom)) || null;
      if(!row){
        modal.el.querySelector('.modal-body').innerHTML = '<div class="empty">Показ не найден — возможно, его уже отменили.</div>';
        return;
      }
      st.allScreens = !!row.all_screens;
      if(!st.allScreens) (row.screens || []).forEach(s => { if(byId.has(s.id)) st.picked.add(s.id); });
      st.hourFrom = Number(row.hour_from);
      st.hourTo = Number(row.hour_to);
      if(editing){
        st.dateFrom = row.date_from;
        st.dateTo = row.date_to;
        st.origDateFrom = row.date_from;
        if(row.date_from < st.minFrom) st.minFrom = row.date_from;   // дату начала прошедшего показа можно оставить
        if(row.status === 'stopped') st.blocked = STOPPED_TEXT;
      }
    }

    // Заголовок: название плейлиста и ход по шагам (как на макете)
    const title = modal.el.querySelector('.modal-title');
    title.textContent = editing ? `Изменить показ · «${st.playlistName}»` : `«${st.playlistName}»`;
    if(creating){
      title.insertAdjacentHTML('afterend',
        '<span class="plc-steps">1 Ролики › <b>2 Куда и когда</b></span>');
    }

    modal.el.querySelector('.modal-body').innerHTML = bodyHtml(st);
    st.loaded = true;
    renderScreens();
    refreshPickInfo(st.screens.length);
    schedulePreview();
    const first = modal.el.querySelector('input[name="plc-target"]:checked');
    if(first) first.focus();
  }

  // ---- события (по одному обработчику на документ) ----------------------------------------------

  function initPlacementEditorActions(){
    if(window.__placementEditorActionsInitialized) return;
    window.__placementEditorActionsInitialized = true;

    document.addEventListener('click', e => {
      const el = e.target.closest && e.target.closest('[data-action]');
      if(!el) return;
      const action = el.dataset.action;
      if(!action || !action.startsWith('plc-') || !S) return;
      switch(action){
        case 'plc-back':       return goBack();
        case 'plc-save-only':  return closeNow();
        case 'plc-send':       return send();
      }
    });

    // Радиокнопки, галочки, даты и часы — по change; даты ещё и по input (набор с клавиатуры)
    const onChange = e => {
      const t = e.target;
      if(!S || !S.loaded || !t || !t.dataset || !S.modal.el.contains(t)) return;
      switch(t.dataset.action){
        case 'plc-target-all':  return setTarget(true);
        case 'plc-target-pick': return setTarget(false);
        case 'plc-screen':      return togglePick(Number(t.dataset.screenId), t.checked);
        case 'plc-decide':      return decide(t.dataset.key, t.dataset.mode);
        case 'plc-apply-all':   return setApplyAll(t.checked);
      }
      if(t.id === 'plc-date-from' || t.id === 'plc-date-to'
         || t.id === 'plc-hour-from' || t.id === 'plc-hour-to'){
        onFieldChange(false);
      }
    };
    document.addEventListener('change', onChange);

    document.addEventListener('input', e => {
      const t = e.target;
      if(!S || !S.loaded || !t || !S.modal.el.contains(t)) return;
      if(t.id === 'plc-screen-search'){
        S.query = t.value;
        renderScreens();
      }else if(t.id === 'plc-date-from' || t.id === 'plc-date-to'){
        onFieldChange(false);
      }
    });

    // Esc во время отправки окно не закрывает (modal.js смотрит на defaultPrevented)
    document.addEventListener('keydown', e => {
      if(e.key === 'Escape' && S && S.busy) e.preventDefault();
    }, true);
  }

  window.Signage = window.Signage || {};
  window.Signage.openPlacementEditor = openPlacementEditor;

  initPlacementEditorActions();
})();
