//=============================================================================
// БЕГУЩАЯ СТРОКА: срочные объявления на сегодня
//
//   Signage.viewTicker() — открыть раздел (меню «Бегущая строка», layout.js).
//
// Одна страница. Сверху «Сейчас идут» (#tk-live): по каждой строке цвет, текст, «где»
// («на всех N экранах» / «группа «…» (N экранов)» / «N экранов: …»), срок («до 21:00,
// осталось 14 мин» / «до снятия» / «ещё 25 мин»), «запустил <кто> в ЧЧ:ММ» и «бежит на
// X из N» — по отчётам самих экранов (кнопка ticker-details раскрывает, где и почему не
// бежит). Ниже конструктор «Новая строка» (#tk-form, только с правом записи): шаблоны,
// текст, цвет и скорость, «где» (все экраны / группы / экраны), срок («До снятия» /
// «До ЧЧ:00 сегодня» / «На N минут»), предупреждение о более чем трёх строках на экране,
// кнопка «▶ Запустить на N экранах» и живое превью (#tk-prev). «✎ Изменить» переводит тот
// же конструктор в правку этой строки («Сохранить изменения» / «Отмена»).
//
// Данные — GET /tickers/overview одним запросом. Страница сама перечитывает их каждые 15 с,
// пока открыта и вкладка видима, и не во время отправки. Введённый текст, открытая правка,
// выбор экранов и раскрытые подробности при этом не сбрасываются: поле текста вне
// перерисовываемых блоков, блок с полем «минут» при вводе не пересобирается, фокус на
// кнопках возвращается на место.
//
// Время от сервера — наивное МСК («2026-11-02T20:46:00»): ЧЧ:ММ берётся срезом строки,
// через new Date() его пропускать нельзя. «Сейчас по Москве» (час для списка часов и
// проверки при отправке) берётся не с часов браузера, а из generated_at ответа плюс
// прошедшее с его получения. «До ЧЧ:00» уходит с on_date; если к отправке час или сутки
// сменились, запрос не уходит — тот же отказ, что в окне общего эфира.
//=============================================================================
(function(){
  const REFRESH_MS = 15000;       // автообновление
  const LOAD_TIMEOUT_MS = 20000;  // запрос дольше — «сервер не ответил», а не вечное ожидание
  const MAX_LEN = 200;            // длина текста после trim (как на сервере)
  const MAX_ON_SCREEN = 3;        // больше — предупреждение «каждая будет проходить реже»
  const TZ = 'Europe/Moscow';
  const STALE_TEXT = 'Время изменилось — проверьте час окончания';   // тот же текст отдаёт сервер (400 по on_date)
  const SAMPLE = 'Внимание: пример бегущей строки';
  // Точные тексты 404 сервера (ticker_store._NOT_FOUND / _TEMPLATE_NOT_FOUND). Сравнение именно точное: 400 «Группа не найдена —
  // обновите страницу» тоже содержит «не найдена», но это не «строку сняли».
  const TICKER_GONE = 'Бегущая строка не найдена';
  const TEMPLATE_GONE = 'Шаблон не найден';
  const COLORS = [['#ffd34d', 'Жёлтый'], ['#ffffff', 'Белый'], ['#ff6b5e', 'Красный'], ['#7fe3c4', 'Бирюзовый']];
  const SPEEDS = [['slow', 'Медленно', 80], ['medium', 'Средне', 140], ['fast', 'Быстро', 220]];   // px/с на кадре 1280 (как в агенте)
  const FRAME_W = 1280, FONT_PX = 42, BOTTOM_PX = 12;      // кадр, шрифт и отступ снизу в агенте (_marquee_overlay_cmd)

  let S = null;                   // состояние открытой страницы (одно на всё приложение)
  let clock = null;               // {base, at}: «настенное» МСК сервера в мс на оси UTC и Date.now() в момент приёма

  const $ = id => document.getElementById(id);
  const hh = n => String(n).padStart(2, '0');

  // ---- мелочи ---------------------------------------------------------------

  const isOne = n => n % 10 === 1 && n % 100 !== 11;
  const isFew = n => n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14);
  const screensWord = n => n + ' ' + (isOne(n) ? 'экран' : isFew(n) ? 'экрана' : 'экранов');
  const onScreens = n => 'на ' + n + ' ' + (isOne(n) ? 'экране' : 'экранах');
  const onAll = n => n === 1 ? 'на единственном экране' : 'на всех ' + n + ' ' + (isOne(n) ? 'экране' : 'экранах');
  const linesWord = n => n + ' ' + (isOne(n) ? 'строка' : isFew(n) ? 'строки' : 'строк');

  const safeColor = c => /^#[0-9a-f]{6}$/i.test(String(c)) ? String(c) : COLORS[0][0];
  const speedKey = s => SPEEDS.some(x => x[0] === s) ? s : 'medium';

  // api() бросает Error(тело ответа); у FastAPI 400/404 — {"detail": "текст"}
  function errText(e){
    if(e instanceof TypeError) return 'нет связи с сервером';
    const t = String((e && e.message) || e || '').trim();
    try{
      const j = JSON.parse(t);
      if(j && typeof j.detail === 'string') return j.detail;
      if(j && Array.isArray(j.detail)) return 'некорректный запрос';
    }catch(_){}
    if(/^</.test(t)) return 'сервер не отвечает';     // страница-заглушка nginx (502/504)
    return t.slice(0, 200) || 'неизвестная ошибка';
  }

  // ---- время сервера ----------------------------------------------------------

  // Наивное МСК «2026-11-02T20:46:00» -> секунды «на одной оси» (разность двух таких значений верна)
  function secs(iso){
    const m = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d))?/.exec(String(iso || ''));
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) / 1000 : NaN;
  }

  function syncClock(iso){
    const t = secs(iso);
    if(isFinite(t)) clock = {base: t * 1000, at: Date.now()};
  }

  // «Сейчас по Москве»: {hour: 0…23, date: 'ГГГГ-ММ-ДД'}. Время сервера + прошедшее с его получения;
  // «настенные» числа МСК лежат на оси UTC, поэтому читаем их getUTC*. Intl с поясом Москвы — запасной путь.
  function mskNow(){
    if(clock){
      const d = new Date(clock.base + (Date.now() - clock.at));
      return {hour: d.getUTCHours(), date: `${d.getUTCFullYear()}-${hh(d.getUTCMonth() + 1)}-${hh(d.getUTCDate())}`};
    }
    try{
      const parts = new Intl.DateTimeFormat('en-CA', {timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', hourCycle: 'h23'}).formatToParts(new Date());
      const get = t => (parts.find(x => x.type === t) || {}).value;
      const hour = parseInt(get('hour'), 10);
      if(hour >= 0 && hour <= 23 && /^\d{4}$/.test(get('year') || '')) return {hour, date: `${get('year')}-${get('month')}-${get('day')}`};
    }catch(_){}
    const d = new Date();
    return {hour: d.getHours(), date: `${d.getFullYear()}-${hh(d.getMonth() + 1)}-${hh(d.getDate())}`};
  }

  // «20:46»; другой день — с датой «01.11 20:46»; полночь следующих суток — «24:00»
  function fmtMoment(iso, refIso){
    const s = String(iso || '');
    if(s.length < 16) return '';
    const hm = s.slice(11, 16);
    if(refIso && s.slice(0, 10) !== String(refIso).slice(0, 10)){
      return hm === '00:00' ? '24:00' : s.slice(8, 10) + '.' + s.slice(5, 7) + ' ' + hm;
    }
    return hm;
  }

  // «1 ч 27 мин» / «45 мин» / «2 ч» / «менее минуты»
  function leftText(sec){
    const min = Math.floor(sec / 60);
    if(!(min >= 1)) return 'менее минуты';
    const h = Math.floor(min / 60), m = min % 60;
    return h && m ? `${h} ч ${m} мин` : h ? `${h} ч` : `${m} мин`;
  }

  // ---- состояние формы ---------------------------------------------------------

  function blankForm(){
    return {message: '', color: COLORS[0][0], speed: 'medium', target: 'all', groups: {}, screens: {},
      term: 'cancel', hour: null, hourMissing: false, hourWas: null, minutes: '15'};
  }

  const tickerById = (st, id) => st.data && st.data.tickers.find(t => t.id === id) || null;
  const minutesOf = f => { const s = String(f.minutes).trim(); return /^\d{1,4}$/.test(s) ? Number(s) : NaN; };

  function selectedScreens(st){
    const f = st.form, all = st.data ? st.data.screens : [];
    if(f.target === 'groups') return all.filter(s => s.group_id != null && f.groups[s.group_id]);
    if(f.target === 'screens') return all.filter(s => f.screens[s.id]);
    return all;
  }

  // Список часов «до»: от следующего часа по МСК до 24:00. st.base — час и дата, на которые он собран.
  function rebuildHours(st, pick){
    const prev = st.base, f = st.form;
    st.base = mskNow();
    const valid = f.hour != null && f.hour > st.base.hour && f.hour <= 24;
    if(pick && (!valid || !prev || prev.date !== st.base.date || st.base.hour < prev.hour)){
      f.hour = st.base.hour + 1;
      f.hourMissing = false;
    }else if(!valid && f.hour != null){
      f.hourMissing = true;
      f.hourWas = f.hour;
      f.hour = null;
    }
  }

  // Почему запустить нельзя (пустая строка — можно)
  function termProblem(st){
    const f = st.form;
    if(f.term === 'hour' && f.hour == null) return 'Выберите час окончания';
    if(f.term === 'minutes'){
      const m = minutesOf(f);
      if(!(m >= 1 && m <= 1440)) return 'Минут — целое от 1 до 1440';
    }
    return '';
  }

  function textProblem(st){
    const len = st.form.message.trim().length;
    if(!len) return 'Введите текст';
    if(len > MAX_LEN) return `Текст — не длиннее ${MAX_LEN} символов`;
    return '';
  }

  function formProblem(st){
    const f = st.form;
    if(!st.data) return 'Данные ещё загружаются';
    if(st.editId != null && !tickerById(st, st.editId)) return 'Эта строка уже снята';
    const bad = textProblem(st);
    if(bad) return bad;
    if(selectedScreens(st).length === 0){
      if(f.target === 'groups') return Object.keys(f.groups).some(k => f.groups[k]) ? 'В выбранных группах нет экранов' : 'Выберите хотя бы одну группу';
      if(f.target === 'screens') return 'Выберите хотя бы один экран';
      return 'Экранов пока нет';
    }
    return termProblem(st);
  }

  // Экраны, где после запуска (правки) будет больше MAX_ON_SCREEN строк: [{name, n}]
  function crowded(st){
    const self = st.editId != null ? tickerById(st, st.editId) : null;
    const covers = sc => !!self && (self.target === 'all'
      || (self.target === 'groups' && (self.group_ids || []).includes(sc.group_id))
      || (self.target === 'screens' && (self.screen_ids || []).includes(sc.id)));
    return selectedScreens(st)
      .map(sc => ({name: sc.name, n: (Number(sc.live_count) || 0) - (covers(sc) ? 1 : 0) + 1}))
      .filter(r => r.n > MAX_ON_SCREEN);
  }

  // ---- отрисовка блоков ----------------------------------------------------------

  // Записать блок, только если разметка изменилась. Фокус на кнопке блока остаётся на месте; блок, в котором
  // печатают (поле «минут»), не трогаем — он перерисуется после ухода фокуса.
  function put(st, id, html){
    const box = $(id);
    if(!box || (st.html[id] === html && st.box[id] === box)) return;
    const a = document.activeElement;
    const inside = !!a && a !== box && box.contains(a);
    if(inside && a.tagName === 'INPUT'){ st.html[id] = null; return; }
    let sel = null;
    if(inside){
      if(a.id) sel = '#' + a.id;
      else{
        const attrs = Array.from(a.attributes).filter(x => x.name.indexOf('data-') === 0);
        const card = a.closest('[data-ticker-id]');
        if(attrs.length) sel = (card ? `[data-ticker-id="${card.dataset.tickerId}"] ` : '') + a.tagName.toLowerCase()
          + attrs.map(x => `[${x.name}="${x.value.replace(/"/g, '\\"')}"]`).join('');
      }
    }
    const y = window.scrollY;
    const picks = Array.from(box.querySelectorAll('.tk-pick')).map(el => el.scrollTop);   // свой скролл у списков групп / экранов
    st.html[id] = html;
    st.box[id] = box;
    box.innerHTML = html;
    if(window.scrollY !== y) window.scrollTo(0, y);
    box.querySelectorAll('.tk-pick').forEach((el, i) => { if(picks[i]) el.scrollTop = picks[i]; });   // «не в сети N мин» меняется раз в минуту — список не прыгает наверх
    if(sel){
      const el = box.querySelector(sel);
      if(el && !el.disabled) el.focus({preventScroll: true});
    }
  }

  function whereText(t, d){
    const n = Number(t.screens_total) || 0;
    if(t.target === 'groups'){
      const names = (t.group_ids || []).map(id => d.groups.find(g => g.id === id)).filter(Boolean).map(g => '«' + g.name + '»');
      if(!names.length) return 'группа удалена (0 экранов)';
      return `${names.length > 1 ? 'группы' : 'группа'} ${names.join(', ')} (${screensWord(n)})`;
    }
    if(t.target === 'screens'){
      const names = (t.screen_ids || []).map(id => d.screens.find(s => s.id === id)).filter(Boolean).map(s => s.name);
      if(!names.length) return 'экраны удалены (0 экранов)';
      return `${screensWord(n)}: ${names.slice(0, 4).join(', ')}${names.length > 4 ? ' и ещё ' + (names.length - 4) : ''}`;
    }
    return n ? onAll(n) : 'экранов пока нет';
  }

  // Срок строки целым часом («до 21:00») или «ещё N мин» (строка на N минут)
  function termText(st, t){
    if(!t.until_at) return 'до снятия';
    const left = Math.max(0, (Number(t.remaining_seconds) || 0) - (Date.now() - st.at) / 1000);
    if(/T\d\d:00:00$/.test(String(t.until_at))){
      const hm = String(t.until_at).slice(11, 16);
      const day = String(t.until_at).slice(0, 10) !== String(st.data.generated_at).slice(0, 10);
      return `до ${hm === '00:00' && day ? '24:00' : fmtMoment(t.until_at, st.data.generated_at)}, осталось ${leftText(left)}`;
    }
    return 'ещё ' + leftText(left);
  }

  function reasonLine(p, d){
    if(p.reason !== 'offline') return p.text;
    // экран — по id (имена могут повторяться); у ответа без screen_id — по имени, как раньше
    const sc = d.screens.find(s => (p.screen_id != null ? s.id === p.screen_id : s.name === p.screen) && !s.online);
    return (sc ? fmtOfflineFor(sc.offline_for_seconds) : p.text) + ' — получит при подключении';
  }

  function confirmHtml(st, t){
    const c = t.confirm || {total: 0, running: 0, problems: []};
    const open = !!st.open[t.id];
    const cls = c.total === 0 ? 'wait' : c.running === c.total ? 'ok' : c.problems.every(p => p.reason === 'pending') ? 'wait' : 'bad';
    const why = [];
    c.problems.forEach(p => {
      const g = why.find(x => x.reason === p.reason);
      if(g) g.n++; else why.push({reason: p.reason, text: p.text, n: 1});
    });
    const whyText = why.map(g => c.total > 1 ? `${g.text}: ${g.n}` : g.text).join('; ');
    let det = '';
    if(open){
      det = !c.total ? '<div class="tk-det">Ни один экран не подходит под эту строку.</div>'
        : !c.problems.length ? '<div class="tk-det">Все экраны подтвердили: строка бежит.</div>'
        : `<ul class="tk-det">${c.problems.map(p => `<li><b>${esc(p.screen)}</b> — ${esc(reasonLine(p, st.data))}</li>`).join('')}</ul>`;
    }
    return `<div class="tk-conf ${cls}">
        <button type="button" class="tk-tog" data-action="ticker-details" aria-expanded="${open}">бежит на ${c.running} из ${c.total} <span aria-hidden="true">${open ? '▴' : '▾'}</span></button>
        ${whyText ? `<span class="tk-why">${esc(whyText)}</span>` : ''}
      </div>${det}`;
  }

  function cardHtml(st, t){
    const d = st.data, write = canWrite();
    const who = [];
    if(t.started_by) who.push('запустил ' + esc(t.started_by));
    if(t.started_at){
      const m = fmtMoment(t.started_at, d.generated_at);      // «20:46» или, если не сегодня, «01.11 20:46»
      who.push((who.length ? '' : 'запущена ') + esc(m.length > 5 ? m.replace(' ', ' в ') : 'в ' + m));
    }
    const editing = st.editId === t.id;
    return `<div class="tk-card${editing ? ' edit' : ''}" data-ticker-id="${t.id}">
      <span class="tk-sw" style="background:${safeColor(t.color)}"></span>
      <div class="tk-ct">
        <b class="tk-text">${esc(t.message)}</b>
        <span class="tk-meta">${esc(whereText(t, d))} · ${esc(termText(st, t))}${who.length ? ' · ' + who.join(' ') : ''}</span>
        ${confirmHtml(st, t)}
      </div>
      ${write ? `<div class="tk-acts">
        <button type="button" class="btn" data-action="ticker-edit"${editing ? ' disabled' : ''}>✎ Изменить</button>
        <button type="button" class="btn danger" data-action="ticker-stop">Снять</button>
      </div>` : ''}
    </div>`;
  }

  function liveHtml(st){
    const d = st.data;
    if(!d){
      return '<div class="sec tk-sec">Сейчас идут</div>' + (st.error
        ? `<div class="empty">Не удалось загрузить данные: ${esc(st.error)}<br><br><button type="button" class="btn" data-action="ticker-retry">Повторить</button></div>`
        : '<div class="empty">Загрузка…</div>');
    }
    const n = d.tickers.length;
    return `<div class="sec tk-sec">Сейчас идут${n ? ' · ' + n : ''}</div>`
      + (st.error ? `<div class="tk-stale" role="status">Не удалось обновить данные: ${esc(st.error)}. Показано состояние на ${esc(String(d.generated_at).slice(11, 19))}.</div>` : '')
      + (n ? d.tickers.map(t => cardHtml(st, t)).join('') : '<div class="tk-none">Сейчас ничего не идёт</div>');
  }

  function termWords(t){
    const k = t.term && t.term.kind;
    if(k === 'minutes') return 'на ' + t.term.minutes + ' мин';
    if(k === 'hour') return 'до ' + hh(t.term.hour) + ':00';
    return 'до снятия';
  }

  function templatesHtml(st){
    const list = st.data ? st.data.templates : [];
    const cur = list.find(t => t.id === st.tplId);          // шаблон, из которого заполнена форма (пока не ушли из его контекста)
    return list.map(t => `<span class="tk-tpl${t === cur ? ' on' : ''}">
        <button type="button" class="tk-tpl-b" data-action="ticker-template" data-template-id="${t.id}" title="${esc(t.message)} — ${esc(termWords(t))}"><i style="background:${safeColor(t.color)}"></i><span>${esc(t.message)}</span></button>
        <button type="button" class="tk-tpl-x" data-action="ticker-template-delete" data-template-id="${t.id}" aria-label="Удалить шаблон «${esc(t.message)}»" title="Удалить шаблон">×</button>
      </span>`).join('')
      + '<button type="button" class="tk-tpl-add" data-action="ticker-save-template">+ сохранить как шаблон</button>'
      + (cur && st.editId == null && canWrite() ? `<button type="button" class="tk-tpl-add" data-action="ticker-update-template"${st.busy ? ' disabled' : ''}
        title="Записать текущие текст, цвет, скорость и срок в шаблон «${esc(cur.message)}»">Обновить шаблон</button>` : '');
  }

  function lookHtml(st){
    const f = st.form;
    const colors = COLORS.some(c => c[0] === f.color) ? COLORS : COLORS.concat([[f.color, 'Свой цвет']]);
    return `<div class="tk-colors" role="group" aria-label="Цвет">${colors.map(c => `<button type="button" class="tk-col${c[0] === f.color ? ' on' : ''}"
        data-action="ticker-pick-color" data-color="${c[0]}" style="background:${c[0]}" title="${c[1]}" aria-label="${c[1]}" aria-pressed="${c[0] === f.color}"></button>`).join('')}</div>
      <div class="seg" role="group" aria-label="Скорость">${SPEEDS.map(s => `<button type="button" class="${s[0] === f.speed ? 'on' : ''}"
        data-action="ticker-speed" data-speed="${s[0]}" aria-pressed="${s[0] === f.speed}">${s[1]}</button>`).join('')}</div>`;
  }

  const optHtml = (on, action, attr, name, extra) => `<button type="button" class="tk-opt${on ? ' on' : ''}" data-action="${action}" ${attr} aria-pressed="${on}">
      <span class="tk-box" aria-hidden="true">${on ? '✓' : ''}</span><span class="tk-nm">${esc(name)}</span> ${extra || ''}</button>`;

  function whereHtml(st){
    const f = st.form, d = st.data;
    const seg = [['all', 'Все экраны'], ['groups', 'Группы'], ['screens', 'Экраны']];
    let h = `<div class="seg" role="group" aria-label="Где показывать">${seg.map(s => `<button type="button" class="${s[0] === f.target ? 'on' : ''}"
      data-action="ticker-target" data-target="${s[0]}" aria-pressed="${s[0] === f.target}">${s[1]}</button>`).join('')}</div>`;
    if(!d) return h;
    if(f.target === 'all'){
      h += '<div class="tk-hint">Определяется в момент показа: экран, добавленный позже, тоже получит строку.</div>';
    }else if(f.target === 'groups'){
      h += d.groups.length
        ? '<div class="tk-pick" role="group" aria-label="Группы">' + d.groups.map(g => optHtml(!!f.groups[g.id], 'ticker-toggle-group',
            `data-group-id="${g.id}"`, g.name, `<span class="muted">${screensWord(Number(g.screen_count) || 0)}</span>`)).join('') + '</div>'
        : '<div class="tk-hint">Групп экранов пока нет.</div>';
    }else{
      h += d.screens.length
        ? '<div class="tk-pick" role="group" aria-label="Экраны">' + d.screens.map(s => optHtml(!!f.screens[s.id], 'ticker-toggle-screen',
            `data-screen-id="${s.id}"`, s.name, s.online ? '' : `<span class="tk-off">${esc(fmtOfflineFor(s.offline_for_seconds))}</span>`)).join('') + '</div>'
        : '<div class="tk-hint">Экранов пока нет.</div>';
    }
    return h;
  }

  function termHtml(st){
    const f = st.form, edit = st.editId != null;
    const seg = (edit ? [['keep', 'Не менять']] : []).concat([['cancel', 'До снятия'], ['hour', 'До ЧЧ:00 сегодня'], ['minutes', 'На N минут']]);
    let h = `<div class="seg" role="group" aria-label="Срок">${seg.map(s => `<button type="button" class="${s[0] === f.term ? 'on' : ''}"
      data-action="ticker-term" data-term="${s[0]}" aria-pressed="${s[0] === f.term}">${s[1]}</button>`).join('')}</div>`;
    if(f.term === 'keep'){
      const t = tickerById(st, st.editId);
      h += `<div class="tk-hint">Срок остаётся прежним${t ? ': ' + esc(termText(st, t)) : ''}.</div>`;
    }else if(f.term === 'cancel'){
      h += '<div class="tk-hint">Строка бежит, пока её не снимут. Полночь её не снимает.</div>';
    }else if(f.term === 'hour'){
      const base = st.base || mskNow();
      let opts = f.hour == null ? '<option value="" selected>— выберите час —</option>' : '';
      for(let i = base.hour + 1; i <= 24; i++) opts += `<option value="${i}"${i === f.hour ? ' selected' : ''}>${hh(i)}:00</option>`;
      h += `<div class="tk-sub"><label for="tk-hour">До</label>
        <select class="inp tk-hour${f.hourMissing ? ' tk-bad' : ''}" id="tk-hour" aria-invalid="${f.hourMissing}">${opts}</select>
        <span>сегодня, затем строка снимется сама</span></div>`
        + (f.hourMissing ? `<div class="tk-hint tk-bad-t">Час шаблона${f.hourWas ? ' («до ' + hh(f.hourWas) + ':00»)' : ''} уже прошёл — выберите новый.</div>` : '');
    }else{
      const m = String(f.minutes).trim();
      h += `<div class="tk-sub">${[[15, '15 мин'], [30, '30 мин'], [60, '1 час']].map(c => `<button type="button" class="tk-min${m === String(c[0]) ? ' on' : ''}"
          data-action="ticker-minutes" data-min="${c[0]}">${c[1]}</button>`).join('')}
        <input class="inp tk-mins" id="tk-minutes" type="number" min="1" max="1440" step="1" inputmode="numeric" value="${esc(f.minutes)}" aria-label="Минут">
        <span>мин${edit ? ' — отсчёт от сохранения' : ''}</span></div>`;
    }
    return h;
  }

  function actionsHtml(st){
    const edit = st.editId != null, problem = formProblem(st);
    const n = selectedScreens(st).length;
    const dis = st.busy || problem ? ' disabled' : '';
    const why = problem && !st.busy ? `<span class="tk-why">${esc(problem)}</span>` : '';
    if(edit){
      return `<button type="button" class="btn primary" id="tk-save" data-action="ticker-save"${dis}>${st.submitting ? 'Сохраняю…' : 'Сохранить изменения'}</button>
        <button type="button" class="btn" data-action="ticker-cancel-edit"${st.busy ? ' disabled' : ''}>Отмена</button>${why}`;
    }
    return `<button type="button" class="btn primary" id="tk-start" data-action="ticker-start"${dis}>${st.submitting ? 'Запускаю…' : '▶ Запустить' + (n > 0 ? ' ' + onScreens(n) : '')}</button>${why}`;
  }

  function editHtml(st){
    if(st.editId == null) return '';
    const t = tickerById(st, st.editId);
    return t ? `<div class="tk-editbar">✎ Меняется идущая строка «${esc(t.message.length > 60 ? t.message.slice(0, 60) + '…' : t.message)}»</div>`
      : '<div class="tk-editbar bad">Эта строка уже снята или её срок вышел — сохранить правку нельзя. Можно запустить её заново: «Отмена» и новая строка.</div>';
  }

  function warnHtml(st){
    const rows = crowded(st);
    if(!rows.length) return '';
    const n = Math.max(...rows.map(r => r.n));
    const names = rows.slice(0, 5).map(r => `${r.name} (${r.n})`).join(', ') + (rows.length > 5 ? ' и ещё ' + (rows.length - 5) : '');
    return `<span aria-hidden="true">⚠</span> Внимание: на этих экранах будет ${linesWord(n)} — каждая будет проходить реже.<br><span class="tk-warn-s">Экраны: ${esc(names)}</span>`;
  }

  function paintCount(st){
    const el = $('tk-count');
    if(!el) return;
    const len = st.form.message.trim().length;
    el.textContent = `${len} / ${MAX_LEN}`;
    el.classList.toggle('over', len > MAX_LEN);
  }

  function paintForm(st){
    if(!$('tk-form')) return;
    $('tk-title').textContent = st.editId != null ? 'Правка строки' : 'Новая строка';
    put(st, 'tk-edit', editHtml(st));
    put(st, 'tk-templates', templatesHtml(st));
    put(st, 'tk-look', lookHtml(st));
    put(st, 'tk-where', whereHtml(st));
    put(st, 'tk-term', termHtml(st));
    const warn = $('tk-warn'), w = warnHtml(st);
    if(warn){ put(st, 'tk-warn', w); warn.hidden = !w; }
    const err = $('tk-err');
    if(err){ err.textContent = st.err; err.hidden = !st.err; }
    put(st, 'tk-actions', actionsHtml(st));
    paintCount(st);
  }

  // Превью на кадре 16:9: тот же вид, что на экране (текст с обводкой у нижнего края), размеры и скорость — как на
  // кадре 1280 пикселей, уменьшенные до ширины превью. Путь строки = кадр + она сама, скорость 80 / 140 / 220 px/с.
  function paintPreview(st, force){
    const frame = $('tk-frame'), run = $('tk-prev'), bar = $('tk-bar');
    if(!frame || !run || !bar) return;
    const w = frame.clientWidth;
    if(!w) return;
    const f = st.form, text = f.message.trim() || SAMPLE;
    const key = [text, f.color, f.speed, w].join('|');
    if(!force && key === st.prevKey) return;
    st.prevKey = key;
    const k = w / FRAME_W, pps = SPEEDS.find(s => s[0] === f.speed)[2] * k;
    run.textContent = text;
    run.style.color = f.color;
    run.style.fontSize = (FONT_PX * k).toFixed(2) + 'px';
    bar.style.paddingBottom = (BOTTOM_PX * k).toFixed(2) + 'px';
    run.style.animation = 'none';
    const total = run.offsetWidth;                          // отступ слева на всю ширину кадра + сама строка
    run.style.animation = `run ${(total / pps).toFixed(2)}s linear infinite`;
  }

  function paintAll(st){
    put(st, 'tk-live', liveHtml(st));
    paintForm(st);
    paintPreview(st);
  }

  // ---- загрузка --------------------------------------------------------------------

  const alive = st => S === st && !!$('tk-root');

  function load(st){
    const p = (async () => {
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), LOAD_TIMEOUT_MS);
      try{
        const d = await api('/tickers/overview', {signal: ctl.signal});
        if(!alive(st)) return;
        st.data = d;
        if(st.tplId != null && !d.templates.some(t => t.id === st.tplId)) st.tplId = null;     // шаблон удалили в другом месте
        st.error = null;
        st.at = Date.now();
        syncClock(d.generated_at);
        if(!st.base) st.base = mskNow();
      }catch(e){
        if(!alive(st) || String(e && e.message) === 'unauthorized') return;
        st.error = ctl.signal.aborted ? `сервер не ответил за ${LOAD_TIMEOUT_MS / 1000} с` : errText(e);
      }finally{
        clearTimeout(timer);
      }
      if(alive(st)) paintAll(st);
    })().finally(() => {
      st.loading = null;
      if(st.again){ st.again = false; load(st); }
    });
    st.loading = p;
    return p;
  }

  function refresh(st){
    if(!st || S !== st || !alive(st)) return Promise.resolve();
    if(st.loading){ st.again = true; return st.loading; }
    return load(st);
  }

  function tick(st){
    if(!alive(st)){ stop(st); return; }
    if(document.visibilityState !== 'visible' || st.busy) return;
    refresh(st);
  }

  function stop(st){
    if(st && st.timer){ clearInterval(st.timer); st.timer = 0; }
  }

  // ---- действия формы --------------------------------------------------------------

  function syncInput(st){
    const el = $('tk-msg');
    if(el) el.value = st.form.message;
    st.prevKey = null;
  }

  function touch(st){ st.err = ''; }

  function repaintForm(st){
    paintForm(st);
    paintPreview(st);
  }

  function applyTemplate(st, id){
    const t = st.data.templates.find(x => x.id === id);
    if(!t){ toast('Шаблон не найден'); refresh(st); return; }
    const f = st.form, term = t.term || {kind: 'cancel'};
    f.message = String(t.message || '');
    f.color = safeColor(t.color);
    f.speed = speedKey(t.speed);
    f.hourMissing = false;
    if(term.kind === 'minutes'){
      f.term = 'minutes';
      f.minutes = String(term.minutes || 15);
    }else if(term.kind === 'hour'){
      f.term = 'hour';
      f.hour = Number(term.hour) || null;
      rebuildHours(st, false);             // час шаблона уже прошёл — не выбран и подсвечен
    }else{
      f.term = 'cancel';
    }
    st.tplId = id;
    touch(st);
    syncInput(st);
    repaintForm(st);
  }

  function bodyOf(st){
    const f = st.form, d = st.data;
    const body = {message: f.message.trim(), color: f.color, speed: f.speed, target: f.target};
    if(f.target === 'groups') body.group_ids = d.groups.filter(g => f.groups[g.id]).map(g => g.id);
    if(f.target === 'screens') body.screen_ids = d.screens.filter(s => f.screens[s.id]).map(s => s.id);
    if(f.term === 'cancel') body.term = {kind: 'cancel'};
    else if(f.term === 'minutes') body.term = {kind: 'minutes', minutes: minutesOf(f)};
    else if(f.term === 'hour') body.term = {kind: 'hour', hour: f.hour, on_date: st.base.date};
    return body;                           // 'keep' (правка без смены срока): term нет — сервер оставит прежний
  }

  function setBusy(st, on){
    st.busy = st.submitting = on;
    const form = $('tk-form');
    if(form) form.inert = on;
    paintForm(st);
  }

  function startEdit(st, id){
    const t = tickerById(st, id);
    if(!t){ toast('Бегущая строка не найдена'); refresh(st); return; }
    if(st.editId == null) st.draft = JSON.parse(JSON.stringify(st.form));     // начатая новая строка ждёт возврата
    const exist = (ids, list) => { const o = {}; (ids || []).forEach(i => { if(list.some(x => x.id === i)) o[i] = true; }); return o; };
    st.editId = id;
    st.tplId = null;
    st.form = Object.assign(blankForm(), {message: t.message, color: safeColor(t.color), speed: speedKey(t.speed),
      target: t.target, groups: exist(t.group_ids, st.data.groups), screens: exist(t.screen_ids, st.data.screens), term: 'keep'});
    st.err = '';
    syncInput(st);
    paintAll(st);
    const form = $('tk-form');
    if(form) form.scrollIntoView({behavior: 'smooth', block: 'start'});
    const msg = $('tk-msg');
    if(msg) msg.focus({preventScroll: true});
  }

  // Выйти из правки: к начатой до неё новой строке (или пустому конструктору); keep — оставить поля как есть (строку сняли)
  function leaveEdit(st, keep){
    st.editId = null;
    st.tplId = null;
    if(keep){
      if(st.form.term === 'keep') st.form.term = 'cancel';
    }else{
      st.form = st.draft || blankForm();
    }
    st.draft = null;
    st.err = '';
    syncInput(st);
    paintAll(st);
  }

  async function submit(){
    const st = S;
    if(!st || st.busy || !canWrite() || !st.data) return;
    const problem = formProblem(st);
    if(problem){ st.err = problem; paintForm(st); return; }
    const f = st.form;
    if(f.term === 'hour'){
      const now = mskNow();
      if(now.hour !== st.base.hour || now.date !== st.base.date){
        rebuildHours(st, true);                        // час (или сутки) сменился, пока страница была открыта: ничего не шлём
        st.err = STALE_TEXT;
        paintForm(st);
        const sel = $('tk-hour');
        if(sel) sel.focus({preventScroll: true});
        return;
      }
    }
    const id = st.editId;
    setBusy(st, true);
    st.err = '';
    let ok = false;
    try{
      await api(id != null ? '/tickers/' + id : '/tickers', {method: id != null ? 'PUT' : 'POST', body: JSON.stringify(bodyOf(st))});
      ok = true;
    }catch(e){
      if(String(e && e.message) === 'unauthorized') return;
      if(!alive(st)) return;
      const msg = errText(e);
      if(id != null && msg === TICKER_GONE){          // строку сняли, пока правили
        setBusy(st, false);
        toast(TICKER_GONE);
        leaveEdit(st, true);
        refresh(st);
        return;
      }
      st.err = msg;
      if(msg === STALE_TEXT) rebuildHours(st, true);
      if(/обновите страницу/i.test(msg)) refresh(st);   // экран или группу удалили
    }
    if(!alive(st)) return;
    setBusy(st, false);
    if(ok){
      toast(id != null ? 'Бегущая строка изменена' : 'Бегущая строка запущена');
      if(id != null){ leaveEdit(st, false); }
      else{ st.form = blankForm(); st.tplId = null; st.err = ''; syncInput(st); paintAll(st); }
      await refresh(st);
    }else{
      const btn = $(id != null ? 'tk-save' : 'tk-start');
      if(btn && !btn.disabled) btn.focus({preventScroll: true});
    }
  }

  // Короткая запись с занятостью: запрос, тост, перечитать данные
  async function act(st, run, done){
    if(st.busy || !canWrite()) return;
    st.busy = true;
    try{
      await run();
      toast(done);
    }catch(e){
      if(String(e && e.message) === 'unauthorized') return;
      toast(errText(e));
    }
    st.busy = false;
    if(alive(st)) await refresh(st);
  }

  function stopTicker(st, id){
    if(st.busy || !canWrite() || !confirm('Снять бегущую строку?')) return;
    act(st, async () => {
      await api('/tickers/' + id, {method: 'DELETE'});
      if(st.editId === id) leaveEdit(st, true);
    }, 'Бегущая строка снята');
  }

  // Срок формы в виде, который принимает /ticker-templates: час без on_date (он у шаблона не хранится)
  const templateTerm = f => f.term === 'hour' ? {kind: 'hour', hour: f.hour}
    : f.term === 'minutes' ? {kind: 'minutes', minutes: minutesOf(f)} : {kind: 'cancel'};

  function templateProblem(st){
    return textProblem(st) || (st.form.term === 'keep' ? 'Выберите срок шаблона: «До снятия», «До ЧЧ:00» или «На N минут»' : termProblem(st));
  }

  function templateBody(st){
    const f = st.form;
    return JSON.stringify({message: f.message.trim(), color: f.color, speed: f.speed, term: templateTerm(f)});
  }

  function saveTemplate(st){
    if(st.busy || !canWrite() || !st.data) return;
    const bad = templateProblem(st);
    if(bad){ toast(bad); return; }
    act(st, async () => {
      await api('/ticker-templates', {method: 'POST', body: templateBody(st)});
      st.tplId = null;                       // новый шаблон — отдельный; «Обновить» не должен переписать прежний
    }, 'Шаблон сохранён');
  }

  // «Обновить шаблон»: текущие текст, цвет, скорость и срок записываются в шаблон, из которого заполнили форму
  async function updateTemplate(st){
    if(st.busy || !canWrite() || !st.data || st.tplId == null || st.editId != null) return;
    const id = st.tplId;
    if(!st.data.templates.some(t => t.id === id)){ st.tplId = null; paintForm(st); toast('Шаблон не найден — список обновлён'); refresh(st); return; }
    const bad = templateProblem(st);
    if(bad){ st.err = bad; paintForm(st); return; }
    st.busy = true;
    st.err = '';
    paintForm(st);                           // кнопка неактивна на время запроса
    let ok = false;
    try{
      await api('/ticker-templates/' + id, {method: 'PUT', body: templateBody(st)});
      ok = true;
    }catch(e){
      if(String(e && e.message) === 'unauthorized') return;
      if(!alive(st)) return;
      const msg = errText(e);
      if(msg === TEMPLATE_GONE){ st.tplId = null; toast(TEMPLATE_GONE + ' — список обновлён'); }   // его удалили в другом месте
      else st.err = msg;
    }
    st.busy = false;
    if(ok) toast('Шаблон обновлён');
    paintForm(st);
    await refresh(st);
  }

  function deleteTemplate(st, id){
    const t = st.data && st.data.templates.find(x => x.id === id);
    if(st.busy || !canWrite() || !t || !confirm(`Удалить шаблон «${t.message}»?`)) return;
    act(st, async () => {
      await api('/ticker-templates/' + id, {method: 'DELETE'});
      if(st.tplId === id) st.tplId = null;
    }, 'Шаблон удалён');
  }

  // ---- вход в раздел ---------------------------------------------------------------------

  function skeleton(write){
    const form = !write ? '' : `<div class="sec" id="tk-title">Новая строка</div>
      <div id="tk-form" class="tk-form">
        <div class="tk-cols">
          <div class="tk-left">
            <div id="tk-edit"></div>
            <div class="tk-lbl">Шаблон</div>
            <div id="tk-templates" class="tk-tpls"></div>
            <div class="tk-lbl"><label for="tk-msg">Текст</label><span id="tk-count" class="tk-count">0 / ${MAX_LEN}</span></div>
            <input class="inp" id="tk-msg" type="text" autocomplete="off" placeholder="Например: Магазин закрывается через 15 минут">
            <div class="tk-lbl">Вид</div>
            <div id="tk-look" class="tk-look"></div>
            <div class="tk-lbl">Где</div>
            <div id="tk-where"></div>
            <div class="tk-lbl">Срок</div>
            <div id="tk-term"></div>
            <div id="tk-warn" class="tk-warn" role="note" hidden></div>
            <div id="tk-err" class="tk-err" role="alert" hidden></div>
            <div id="tk-actions" class="tk-actions"></div>
          </div>
          <div class="tk-right">
            <div class="tickprev" id="tk-frame"><span class="tickvid">ТАК БУДЕТ НА ЭКРАНЕ</span>
              <div class="tickbar" id="tk-bar"><span class="tickrun" id="tk-prev"></span></div></div>
            <div class="tk-hint">Превью бежит с выбранной скоростью, как на экране: кадр 1280×720, у нижнего края.</div>
          </div>
        </div>
      </div>`;
    return `<div id="tk-root" class="tk"><div id="tk-live"></div>${form}</div>`;
  }

  async function viewTicker(){
    stop(S);
    const st = S = {data: null, error: null, loading: null, again: false, timer: 0, at: 0, busy: false,
      form: blankForm(), editId: null, tplId: null, submitting: false, draft: null, open: {}, base: null, err: '', prevKey: null, html: {}, box: {}};
    $('view').innerHTML = skeleton(canWrite());
    paintAll(st);
    st.timer = setInterval(() => tick(st), REFRESH_MS);
    await load(st);
  }

  // ---- события (по одному обработчику на документ) -----------------------------------------

  function initTickerActions(){
    if(window.__tickerViewActionsInitialized) return;
    window.__tickerViewActionsInitialized = true;

    document.addEventListener('click', e => {
      const el = e.target.closest && e.target.closest('[data-action]');
      if(!el) return;
      const action = el.dataset.action;
      const st = S;
      if(!action || action.indexOf('ticker-') !== 0 || !st || !alive(st)) return;
      const card = el.closest('[data-ticker-id]');       // кнопки строки лежат внутри её карточки
      const id = card ? Number(card.dataset.tickerId) : NaN, f = st.form;
      const edit = () => { touch(st); repaintForm(st); };
      switch(action){
        case 'ticker-retry':          return refresh(st);
        case 'ticker-details':        st.open[id] = !st.open[id]; return put(st, 'tk-live', liveHtml(st));
        case 'ticker-edit':           return canWrite() && !st.busy && startEdit(st, id);
        case 'ticker-stop':           return stopTicker(st, id);
        case 'ticker-template':       return canWrite() && !st.busy && st.data && applyTemplate(st, Number(el.dataset.templateId));
        case 'ticker-save-template':  return saveTemplate(st);
        case 'ticker-update-template': return updateTemplate(st);
        case 'ticker-template-delete': return deleteTemplate(st, Number(el.dataset.templateId));
        case 'ticker-cancel-edit':    return !st.busy && leaveEdit(st, false);
        case 'ticker-start':
        case 'ticker-save':           return submit();
      }
      if(!canWrite() || st.busy) return;
      switch(action){
        case 'ticker-pick-color':     f.color = safeColor(el.dataset.color); return edit();
        case 'ticker-speed':          f.speed = speedKey(el.dataset.speed); return edit();
        case 'ticker-target':         f.target = el.dataset.target; return edit();
        case 'ticker-toggle-group':   f.groups[el.dataset.groupId] = !f.groups[el.dataset.groupId]; return edit();
        case 'ticker-toggle-screen':  f.screens[el.dataset.screenId] = !f.screens[el.dataset.screenId]; return edit();
        case 'ticker-term':
          f.term = el.dataset.term;
          if(f.term === 'hour') rebuildHours(st, true);
          return edit();
        case 'ticker-minutes':        f.minutes = String(el.dataset.min); return edit();
      }
    });

    // Ввод не зависит от «занято»: act() / «Обновить шаблон» ставят busy без inert, поле остаётся живым, и пропущенные
    // нажатия оставили бы st.form короче видимого текста. Во время отправки форма inert — события не приходят.
    document.addEventListener('input', e => {
      const st = S, t = e.target;
      if(!st || !alive(st) || !t) return;
      if(t.id === 'tk-msg'){
        st.form.message = t.value;
        touch(st);
        repaintForm(st);
      }else if(t.id === 'tk-minutes'){
        st.form.minutes = t.value;
        touch(st);
        document.querySelectorAll('#tk-term [data-action="ticker-minutes"]').forEach(b => b.classList.toggle('on', b.dataset.min === t.value.trim()));
        paintForm(st);
      }
    });

    document.addEventListener('change', e => {
      const st = S, t = e.target;
      if(!st || !alive(st) || !t || t.id !== 'tk-hour') return;
      st.form.hour = t.value ? Number(t.value) : null;
      if(st.form.hour != null) st.form.hourMissing = false;
      touch(st);
      repaintForm(st);
    });

    // Вернулись на вкладку после долгого отсутствия — не ждём очередного тика
    document.addEventListener('visibilitychange', () => {
      if(document.visibilityState !== 'visible' || !S || !alive(S)) return;
      if(Date.now() - S.at >= REFRESH_MS && !S.busy) refresh(S);
    });

    window.addEventListener('resize', () => { if(S && alive(S)) paintPreview(S); });
  }

  window.Signage = window.Signage || {};
  window.Signage.viewTicker = viewTicker;
  window.viewTicker = viewTicker;           // layout.js вызывает viewTicker по имени

  initTickerActions();
})();
