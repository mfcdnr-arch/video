//=============================================================================
// ОБЩИЙ ЭФИР СЕТИ: полоса над таблицей «Эфира сети» и окно включения / смены
//
//   Signage.renderBroadcastBar(data) — нарисовать полосу в <div id="nl-broadcast">;
//                                      data — ответ GET /network/live (его вызывает
//                                      network-live.js после каждой загрузки);
//   Signage.openBroadcastWindow({mode: 'enable' | 'change'}) — окно «Включить общий
//                                      эфир на все экраны» / «Сменить плейлист».
//
// Полоса видна всегда. Выключен — серая пунктирная, кнопка «Включить общий эфир…»
// (data-action bc-open). Включён — жёлтая: плейлист и число экранов, «с ЧЧ:ММ · до
// ЧЧ:ММ (осталось X ч Y мин) · включил <кто>» или «· до отмены», кнопки «Сменить
// плейлист» (bc-change) и «Выключить» (bc-off, с confirm). Если эфиру на части
// экранов нечего играть (unplayable_screens) — строка «на K экранах нечего играть —
// там идёт обычное расписание». Кнопки только с правом записи (canWrite).
//
// Окно: плейлист (#bc-playlist: поиск и список; серые — те, в которых нет роликов,
// которые могут идти, их не выбрать), «Как долго» (радио bc-duration: «До отмены» /
// «До ЧЧ:00 сегодня», час — #bc-until-hour, от следующего часа по МСК до 24),
// жёлтое предупреждение с числами N (всего экранов) и K (не в сети), кнопки
// «Отмена» (bc-cancel) и «Включить на N экранах» / «Сменить плейлист» (bc-submit).
// Если у выбранного плейлиста нет роликов, которые могут идти без алколицензии
// (GET /playlists → playable_unlicensed = false, реклама алкоголя), окно считает
// экраны не на площадке store_alcohol (venue_type из /network/live) и ещё ДО
// включения пишет «На K экранах нечего играть — там идёт обычное расписание»;
// число обновляется при смене плейлиста.
// POST /broadcast/on {playlist_id, until_hour, on_date}; ошибка сервера — внутри окна.
// Пока идёт запрос, кнопки отключены, форма заморожена, а Esc / ✕ / клик по фону
// окно не закрывают. После успеха окно закрывается, страница перечитывается.
// Час «до» собран от часа МСК на момент открытия: если к отправке час (или сутки)
// сменился, запрос не уходит — список пересобирается, окно просит проверить час
// (иначе «24:00», выбранное в 23:xx, после полуночи дало бы эфир на сутки). Вторая
// страховка — on_date (МСК-дата, на которую собран список): сервер сам отвечает 400
// «Время изменилось…», если к его приходу сутки уже сменились.
//
// Время от сервера — наивное МСК («2026-10-29T16:00:00»): ЧЧ:ММ берётся срезом
// строки, через new Date() его пропускать нельзя. «Сейчас по Москве» (час и дата для
// списка часов и этой проверки) берётся не с часов браузера, а со времени сервера:
// generated_at из /network/live + сколько прошло с его получения (часы компьютера
// могут уходить; пояс браузера тоже ни при чём). Intl с поясом Europe/Moscow —
// запасной путь, пока данных сервера ещё нет.
//=============================================================================
(function(){
  const TZ = 'Europe/Moscow';
  const PENDING_MS = 6000;          // сколько после успеха ждать свежих данных, не размораживая кнопки
  const STALE_TEXT = 'Время изменилось — проверьте час окончания';   // тот же текст отдаёт сервер (400 по on_date)

  let barData = null;               // последние данные /network/live
  let barBusy = false;              // идёт запрос «Выключить»
  let barPending = false;           // запрос прошёл, ждём перерисовки по свежим данным
  let barTimer = 0;
  let barHtml = '';                 // что сейчас нарисовано (не перерисовываем то же самое)
  let barBox = null;                // контейнер, в который это нарисовано
  let W = null;                     // открытое окно (оно может быть только одно)
  let clock = null;                 // {base, at}: «настенное» МСК сервера в мс на оси UTC и Date.now() в момент приёма

  const $ = id => document.getElementById(id);
  const hh = n => String(n).padStart(2, '0');

  // ---- мелочи ---------------------------------------------------------------

  function isOne(n){ return n % 10 === 1 && n % 100 !== 11; }
  function isFew(n){ return n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14); }

  // «1 экран / 2 экрана / 5 экранов»
  function screensWord(n){ return n + ' ' + (isOne(n) ? 'экран' : isFew(n) ? 'экрана' : 'экранов'); }
  // «на 1 экране / на 5 экранах»
  function onScreens(n){ return 'на ' + n + ' ' + (isOne(n) ? 'экране' : 'экранах'); }
  // «на единственном экране / на всех 12 экранах»
  function onAll(n){ return n === 1 ? 'на единственном экране' : 'на всех ' + n + ' ' + (isOne(n) ? 'экране' : 'экранах'); }

  // Поиск без учёта регистра и «ё» = «е»
  function norm(s){ return String(s == null ? '' : s).toLowerCase().replace(/ё/g, 'е'); }

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

  // Наивное МСК «2026-10-29T16:00:00» -> секунды «на одной оси» (разность двух таких значений верна)
  function secs(iso){
    const m = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d))?/.exec(String(iso || ''));
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) / 1000 : NaN;
  }

  // «16:00»; другой день — с датой «28.10 14:30»; полночь следующих суток — «24:00»
  function fmtMoment(iso, refIso){
    const s = String(iso || '');
    if(s.length < 16) return '';
    const hm = s.slice(11, 16);
    if(refIso && s.slice(0, 10) !== String(refIso).slice(0, 10)){
      return hm === '00:00' ? '24:00' : s.slice(8, 10) + '.' + s.slice(5, 7) + ' ' + hm;
    }
    return hm;
  }

  // «до 16:00» / «до 24:00» (полночь следующих суток) / «до отмены»
  function untilText(iso, refIso){
    if(!iso) return 'до отмены';
    if(String(iso).slice(11, 16) === '00:00') return 'до 24:00';
    return 'до ' + fmtMoment(iso, refIso);
  }

  // «1 ч 27 мин» / «45 мин» / «2 ч» / «менее минуты»
  function leftText(sec){
    const min = Math.floor(sec / 60);
    if(!(min >= 1)) return 'менее минуты';
    const h = Math.floor(min / 60), m = min % 60;
    return h && m ? `${h} ч ${m} мин` : h ? `${h} ч` : `${m} мин`;
  }

  // Запомнить время сервера из свежего ответа /network/live (generated_at — наивное МСК)
  function syncClock(iso){
    const t = secs(iso);
    if(isFinite(t)) clock = {base: t * 1000, at: Date.now()};
  }

  // «Сейчас по Москве»: {hour: 0…23, date: 'ГГГГ-ММ-ДД'}. Время сервера + прошедшее с его получения;
  // «настенные» числа МСК лежат на оси UTC (Date.UTC), поэтому читаем их getUTC*, не локальными.
  function mskNow(){
    if(clock){
      const d = new Date(clock.base + (Date.now() - clock.at));
      return {hour: d.getUTCHours(), date: `${d.getUTCFullYear()}-${hh(d.getUTCMonth() + 1)}-${hh(d.getUTCDate())}`};
    }
    try{                                                            // данных сервера ещё нет — Intl с поясом Москвы
      const parts = new Intl.DateTimeFormat('en-CA', {timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', hourCycle: 'h23'}).formatToParts(new Date());
      const get = t => (parts.find(x => x.type === t) || {}).value;
      const hour = parseInt(get('hour'), 10);
      if(hour >= 0 && hour <= 23 && /^\d{4}$/.test(get('year') || '')) return {hour, date: `${get('year')}-${get('month')}-${get('day')}`};
    }catch(_){}
    const d = new Date();                                           // совсем запасной путь: часы компьютера
    return {hour: d.getHours(), date: `${d.getFullYear()}-${hh(d.getMonth() + 1)}-${hh(d.getDate())}`};
  }

  // ---- полоса ---------------------------------------------------------------------

  function barContent(d){
    const b = d && d.broadcast;
    if(!b) return '';
    const write = canWrite();
    const dis = barBusy || barPending ? ' disabled' : '';
    const n = Number(b.screens_total) || 0;

    if(!b.is_on){
      return `<div class="bc-bar off" role="group" aria-label="Общий эфир">
        <span class="bc-ic" aria-hidden="true">📡</span>
        <div class="bc-t"><b>Общий эфир выключен</b><span>Каждый экран показывает своё расписание</span></div>
        ${write ? `<div class="bc-acts"><button type="button" class="btn" data-action="bc-open"${dis}>Включить общий эфир…</button></div>` : ''}
      </div>`;
    }

    const ref = d.generated_at;
    const parts = [];
    if(b.enabled_at) parts.push('с ' + esc(fmtMoment(b.enabled_at, ref)));
    if(b.until_at){
      const left = secs(b.until_at) - secs(ref);
      parts.push(esc(untilText(b.until_at, ref)) + (isFinite(left) ? ` (осталось ${leftText(left)})` : ''));
    }else{
      parts.push('до отмены');
    }
    if(b.enabled_by) parts.push('включил ' + esc(b.enabled_by));
    parts.push('расписание на это время перекрыто');

    const skip = Number(b.unplayable_screens) || 0;
    const title = n > 0
      ? `Общий эфир: «${esc(b.playlist_name || 'плейлист')}» ${onAll(n)}`
      : `Общий эфир: «${esc(b.playlist_name || 'плейлист')}» (экранов пока нет)`;
    return `<div class="bc-bar on" role="group" aria-label="Общий эфир">
      <span class="bc-ic" aria-hidden="true">📡</span>
      <div class="bc-t">
        <b>${title}</b>
        <span>${parts.join(' · ')}</span>
        ${skip > 0 ? `<span class="bc-skip">${onScreens(skip)} нечего играть — там идёт обычное расписание</span>` : ''}
      </div>
      ${write ? `<div class="bc-acts">
        <button type="button" class="btn" data-action="bc-change"${dis}>Сменить плейлист</button>
        <button type="button" class="btn danger" data-action="bc-off"${dis}>Выключить</button>
      </div>` : ''}
    </div>`;
  }

  // Записать полосу, только если разметка изменилась; фокус на кнопке полосы остаётся на месте
  function paintBar(){
    const box = $('nl-broadcast');
    if(!box) return;
    const html = barContent(barData);
    if(html === barHtml && box === barBox) return;       // то же самое в том же контейнере
    const a = document.activeElement;
    const focused = a && a.dataset && a.dataset.action && box.contains(a) ? a.dataset.action : null;
    barHtml = html;
    barBox = box;
    box.innerHTML = html;
    if(focused){
      const el = box.querySelector(`[data-action="${focused}"]`);
      if(el && !el.disabled) el.focus({preventScroll: true});
    }
  }

  function renderBroadcastBar(data){
    barData = data || null;
    if(data) syncClock(data.generated_at);       // сюда приходят только свежие данные /network/live
    barPending = false;            // свежие данные пришли — кнопки снова живые
    clearTimeout(barTimer);
    paintBar();
  }

  // Запрос прошёл: пока не пришли свежие данные, кнопки полосы отключены (иначе
  // второй клик по «Выключить» успел бы записать в журнал ещё одно выключение).
  function markPending(){
    barPending = true;
    paintBar();
    clearTimeout(barTimer);
    barTimer = setTimeout(() => { barPending = false; paintBar(); }, PENDING_MS);
    if(typeof Signage.refreshNetworkLive === 'function') Signage.refreshNetworkLive();
  }

  async function turnOff(){
    if(barBusy || barPending || !canWrite()) return;
    const name = barData && barData.broadcast && barData.broadcast.playlist_name;
    if(!confirm(`Выключить общий эфир${name ? ` «${name}»` : ''}? Экраны вернутся к своему расписанию в течение минуты.`)) return;
    barBusy = true;
    paintBar();
    let ok = false;
    try{
      await api('/broadcast/off', {method: 'POST'});
      ok = true;
    }catch(e){
      if(String(e && e.message) !== 'unauthorized') toast('Не удалось выключить общий эфир: ' + errText(e));
    }
    barBusy = false;
    if(ok){
      toast('Общий эфир выключен');
      markPending();
    }else{
      paintBar();
    }
  }

  // ---- окно: состояние и мелочи ---------------------------------------------------

  // Часы «до»: от следующего часа по МСК до 24 включительно
  function hoursHtml(nowHour, selected){
    let h = '';
    for(let i = nowHour + 1; i <= 24; i++) h += `<option value="${i}"${i === selected ? ' selected' : ''}>${hh(i)}:00</option>`;
    return h;
  }

  // «Включить на N экранах» — число N это экраны, на которых эфир реально пойдёт: если ему нигде нечего
  // играть (предупреждение «Эфир ничего не перекроет»), подпись без числа, чтобы не обещать лишнего
  function submitLabel(st){
    if(st.mode === 'change') return 'Сменить плейлист';
    if(!(st.n > 0) || (st.loaded && coverage(st).play === 0)) return 'Включить';
    return 'Включить ' + onScreens(st.n);
  }

  function playlistCaption(p){ return p.playable ? '' : 'нечего играть — нет одобренных роликов в сроке'; }

  // Список плейлистов под поиск: сначала те, что могут идти, потом серые
  function renderList(st){
    const box = $('bc-pls');
    if(!box) return;
    const qn = norm(st.query.trim());
    const shown = st.playlists.filter(p => !qn || norm(p.name).includes(qn));
    if(!st.playlists.length){
      box.innerHTML = '<div class="bc-empty">Плейлистов пока нет — создайте плейлист в разделе «Плейлисты».</div>';
      return;
    }
    if(!shown.length){
      box.innerHTML = '<div class="bc-empty">Ничего не найдено</div>';
      return;
    }
    box.innerHTML = shown.map(p => {
      const cap = playlistCaption(p);
      const id = Number(p.id);
      return `<label class="bc-pl${p.playable ? '' : ' dis'}${st.picked === id ? ' sel' : ''}" data-id="${id}">
        <input type="radio" name="bc-pl" value="${id}" data-action="bc-pick"${st.picked === id ? ' checked' : ''}${p.playable ? '' : ' disabled'}>
        <span class="bc-pln">${esc(p.name)}${cap ? `<i>${esc(cap)}</i>` : ''}</span>
      </label>`;
    }).join('');
  }

  function renderPicked(st){
    const el = $('bc-picked');
    if(!el) return;
    const p = st.playlists.find(x => x.id === st.picked);
    el.innerHTML = p ? `Выбран: <b>«${esc(p.name)}»</b>` : 'Плейлист не выбран';
  }

  // Что эфир с выбранным плейлистом сделает с экранами: на скольких он пойдёт, на скольких играть
  // нечего (нет роликов без алколицензии, а площадка не store_alcohol) и сколько из «пойдёт» не в сети.
  // Плейлист не выбран или флаг не пришёл — предупреждать не о чем: нечего играть = 0.
  function coverage(st){
    const pl = st.playlists.find(p => p.id === st.picked);
    const needLicense = !!pl && pl.unlicensedOk === false;
    let skip = 0, offline = 0;
    st.screens.forEach(sc => {
      if(needLicense && !sc.licensed) skip++;
      else if(!sc.online) offline++;
    });
    return {total: st.screens.length, play: st.screens.length - skip, skip, offline};
  }

  // Предупреждение: на скольких экранах пойдёт эфир (N), где нечего играть, сколько не в сети (K)
  function renderWarn(st){
    const el = $('bc-warn');
    if(!el) return;
    const c = coverage(st);
    const lines = [];
    if(st.n > 0){
      const nothing = c.skip > 0 ? `${onScreens(c.skip).replace(/^на/, 'На')} нечего играть — там идёт обычное расписание.` : '';
      if(c.skip === 0){
        lines.push(`${onAll(st.n).replace(/^на/, 'На')} будет идти только этот плейлист. Показы по датам, расписание и замены на это время перекрываются.`);
      }else if(c.play > 0){
        lines.push(`${onScreens(c.play).replace(/^на/, 'На')} (из ${st.n}) будет идти только этот плейлист. Показы по датам, расписание и замены на это время на них перекрываются.`);
        lines.push(nothing);
      }else{
        lines.push('Эфир ничего не перекроет. ' + nothing);
      }
      if(c.offline > 0){
        lines.push(`${screensWord(c.offline)} сейчас не в сети — ${isOne(c.offline) ? 'получит' : 'получат'} эфир при подключении.`);
      }
    }else{
      lines.push('Экранов пока нет — эфир заработает на новых экранах, как только они появятся.');
    }
    el.innerHTML = '<span aria-hidden="true">⚠</span> ' + lines.map(esc).join(' ');
  }

  function renderState(st){
    const submit = $('bc-submit'), cancel = $('bc-cancel');
    if(submit){
      submit.disabled = st.busy || !st.loaded || st.picked == null;
      submit.textContent = st.busy ? (st.mode === 'change' ? 'Меняю…' : 'Включаю…') : submitLabel(st);
    }
    if(cancel) cancel.disabled = st.busy;
  }

  function showErr(text){
    const el = $('bc-err');
    if(!el) return;
    el.textContent = text || '';
    el.hidden = !text;
  }

  function setBusy(st, on){
    st.busy = on;
    st.modal.el.querySelector('.modal-body').inert = on;     // форму на время запроса замораживаем
    renderState(st);
    if(on) st.modal.el.focus({preventScroll: true});         // отключённая кнопка фокус не держит
  }

  function formHtml(st){
    return `<div class="bc-form">
      <div class="bc-lbl">Плейлист</div>
      <div id="bc-playlist">
        <input class="inp" id="bc-playlist-search" type="text" autocomplete="off" placeholder="🔍 найти плейлист"
          aria-label="Поиск плейлиста" aria-controls="bc-pls">
        <div class="bc-pls" id="bc-pls" role="radiogroup" aria-label="Плейлист общего эфира"></div>
        <div class="bc-picked" id="bc-picked" aria-live="polite"></div>
      </div>
      <div class="bc-lbl">Как долго</div>
      <label class="bc-opt${st.dur === 'cancel' ? ' on' : ''}" data-opt="cancel">
        <input type="radio" name="bc-dur" data-action="bc-duration" data-mode="cancel"${st.dur === 'cancel' ? ' checked' : ''}>
        <span>До отмены — выключу сам</span>
      </label>
      <label class="bc-opt${st.dur === 'until' ? ' on' : ''}" data-opt="until">
        <input type="radio" name="bc-dur" data-action="bc-duration" data-mode="until"${st.dur === 'until' ? ' checked' : ''}>
        <span>До <select class="inp" id="bc-until-hour" aria-label="Час окончания эфира">${hoursHtml(st.nowHour, st.hour)}</select> сегодня, затем экраны вернутся к расписанию</span>
      </label>
      <div class="bc-warn" id="bc-warn" role="note"></div>
      <div class="bc-err" id="bc-err" role="alert" hidden></div>
    </div>`;
  }

  // Режим «Как долго»: подсветка выбранного варианта
  function paintDuration(st){
    document.querySelectorAll('.bc-opt').forEach(el => el.classList.toggle('on', el.dataset.opt === st.dur));
    const r = document.querySelector(`input[name="bc-dur"][data-mode="${st.dur}"]`);
    if(r) r.checked = true;
  }

  // Список часов пересобирается от текущего часа по МСК (при открытии окна, после отказа сервера и
  // если к отправке час сменился). Наступили новые сутки (изменилась дата или час стал меньше прежнего) —
  // выбранный час сбрасывается на ближайший: прежнее «24:00» теперь означало бы эфир на сутки вперёд.
  function rebuildHours(st){
    const prevHour = st.nowHour, prevDate = st.nowDate;
    const now = mskNow();
    st.nowHour = now.hour;
    st.nowDate = now.date;
    if(now.hour < prevHour || now.date !== prevDate || !(st.hour > st.nowHour && st.hour <= 24)) st.hour = st.nowHour + 1;
    const sel = $('bc-until-hour');
    if(sel) sel.innerHTML = hoursHtml(st.nowHour, st.hour);
  }

  // ---- окно: отправка --------------------------------------------------------------

  async function submit(){
    const st = W;
    if(!st || st.busy || !st.loaded || st.picked == null) return;
    const pl = st.playlists.find(p => p.id === st.picked);
    if(!pl || !pl.playable) return;
    const now = mskNow();
    if(st.dur === 'until' && (now.hour !== st.nowHour || now.date !== st.nowDate)){
      rebuildHours(st);                        // час (или сутки) сменился, пока окно было открыто: ничего не шлём
      showErr(STALE_TEXT);
      const sel = $('bc-until-hour');
      if(sel) sel.focus({preventScroll: true});
      return;
    }
    setBusy(st, true);                         // сразу, до await: второй клик ничего не сделает
    showErr('');
    let r;
    try{
      const body = {playlist_id: st.picked, until_hour: st.dur === 'until' ? st.hour : null};
      if(st.dur === 'until') body.on_date = st.nowDate;      // дата, на которую собран список часов (сервер сверяет)
      r = await api('/broadcast/on', {method: 'POST', body: JSON.stringify(body)});
    }catch(e){
      if(String(e && e.message) === 'unauthorized'){ if(!st.closed) st.modal.close(); return; }
      if(st.closed) return;
      showErr(errText(e) || 'Не удалось включить общий эфир');   // 400 / 404 приходят русским текстом
      rebuildHours(st);                        // вдруг наступил следующий час и выбранный уже в прошлом
      setBusy(st, false);
      const btn = $('bc-submit');
      if(btn && !btn.disabled) btn.focus({preventScroll: true});
      return;
    }
    const verb = st.mode === 'change' ? 'сменён' : 'включён';
    const term = untilText(r && r.until_at, barData && barData.generated_at);
    if(!st.closed) st.modal.close();
    toast(`Общий эфир ${verb}: «${pl.name}» ${term}`);
    markPending();
  }

  // ---- окно: открытие --------------------------------------------------------------

  async function openBroadcastWindow(opts){
    opts = opts || {};
    if(!canWrite()){ toast('Нет прав на управление общим эфиром'); return; }
    if(W) return;                               // окно уже открыто (двойной клик)
    const mode = opts.mode === 'change' ? 'change' : 'enable';

    const st = W = {
      mode, loaded: false, closed: false, busy: false,
      playlists: [], picked: null, query: '', dur: 'until', hour: 0, nowHour: 0, nowDate: '',
      n: 0, screens: [], currentPid: null, modal: null,
    };
    const modal = openModal({
      title: mode === 'change' ? 'Сменить плейлист общего эфира' : 'Включить общий эфир на все экраны',
      body: '<div class="empty">Загрузка…</div>',
      footer: `<button type="button" class="btn" id="bc-cancel" data-action="bc-cancel">Отмена</button>
        <button type="button" class="btn primary" id="bc-submit" data-action="bc-submit" disabled>${submitLabel(st)}</button>`,
      width: 520,
      className: 'modal-bc',
      onClose(){
        st.closed = true;
        if(W === st) W = null;
      },
    });
    st.modal = modal;

    // Пока идёт отправка, окно не закрывается ни ✕, ни кликом по фону (иначе ответ ушёл бы «в никуда»).
    // Клики — в capture-фазе на подложке, раньше обработчика modal.js; mousedown по подложке гасим,
    // чтобы фокус не уходил на body; Esc гасит общий обработчик внизу файла.
    const back = modal.el.parentElement;
    back.addEventListener('mousedown', e => { if(st.busy && e.target === back) e.preventDefault(); }, true);
    back.addEventListener('click', e => {
      if(!st.busy) return;
      const x = e.target.closest && e.target.closest('[data-modal-close]');
      if(e.target === back || (x && modal.el.contains(x))){
        e.stopImmediatePropagation();
        e.preventDefault();
      }
    }, true);

    let data, list;
    try{
      // свежие данные /network/live — часы сервера запоминаем в момент ответа, не дожидаясь /playlists
      const live = barData || api('/network/live').then(d => { if(d) syncClock(d.generated_at); return d; });
      [data, list] = await Promise.all([live, api('/playlists')]);
    }catch(e){
      if(st.closed) return;
      modal.el.querySelector('.modal-body').innerHTML =
        `<div class="empty">Не удалось загрузить данные: ${esc(errText(e))}</div>`;
      return;
    }
    if(st.closed) return;

    const b = (data && data.broadcast) || {};
    const screens = (data && data.screens) || [];
    st.n = Number(b.screens_total != null ? b.screens_total : screens.length) || 0;
    st.screens = screens.map(sc => ({online: !!sc.online, licensed: (sc.venue_type || 'other') === 'store_alcohol'}));
    st.playlists = (list || []).map(p => ({
      id: Number(p.id), name: String(p.name == null ? '' : p.name), playable: p.playable !== false,
      unlicensedOk: p.playable_unlicensed !== false,   // false — только реклама алкоголя: без алколицензии играть нечего
    }))
      .sort((x, y) => (y.playable - x.playable)
        || x.name.localeCompare(y.name, 'ru', {numeric: true, sensitivity: 'base'}) || x.id - y.id);

    const now = mskNow();
    st.nowHour = now.hour;
    st.nowDate = now.date;
    st.hour = st.nowHour + 1;
    if(mode === 'change' && b.is_on){
      st.currentPid = b.playlist_id != null ? Number(b.playlist_id) : null;
      const cur = st.playlists.find(p => p.id === st.currentPid);
      if(cur && cur.playable) st.picked = cur.id;
      if(!b.until_at){
        st.dur = 'cancel';
      }else{
        const h = parseInt(String(b.until_at).slice(11, 13), 10);
        const until = h === 0 ? 24 : h;                      // полночь следующих суток = «до 24:00»
        if(until > st.nowHour && until <= 24) st.hour = until;
      }
    }

    modal.el.querySelector('.modal-body').innerHTML = formHtml(st);
    st.loaded = true;
    renderList(st);
    renderPicked(st);
    renderWarn(st);
    renderState(st);
    const search = $('bc-playlist-search');
    if(search) search.focus({preventScroll: true});
  }

  // ---- события (по одному обработчику на документ) ---------------------------------

  function initBroadcastActions(){
    if(window.__broadcastWindowActionsInitialized) return;
    window.__broadcastWindowActionsInitialized = true;

    document.addEventListener('click', e => {
      const el = e.target.closest && e.target.closest('[data-action]');
      if(!el) return;
      const action = el.dataset.action;
      if(!action || !action.startsWith('bc-')) return;
      switch(action){
        case 'bc-open':   return openBroadcastWindow({mode: 'enable'});
        case 'bc-change': return openBroadcastWindow({mode: 'change'});
        case 'bc-off':    return turnOff();
        case 'bc-cancel': if(W && !W.busy && W.modal.el.contains(el)) W.modal.close(); return;
        case 'bc-submit': if(W && W.modal.el.contains(el)) submit(); return;
      }
    });

    document.addEventListener('change', e => {
      const t = e.target;
      const st = W;
      if(!st || !st.loaded || !t || !st.modal.el.contains(t)) return;
      if(t.dataset && t.dataset.action === 'bc-pick'){
        st.picked = Number(t.value);
        document.querySelectorAll('#bc-pls .bc-pl').forEach(l => l.classList.toggle('sel', Number(l.dataset.id) === st.picked));
        renderPicked(st);
        renderWarn(st);
        renderState(st);
      }else if(t.dataset && t.dataset.action === 'bc-duration'){
        st.dur = t.dataset.mode === 'cancel' ? 'cancel' : 'until';
        paintDuration(st);
      }else if(t.id === 'bc-until-hour'){
        st.hour = Number(t.value);
        st.dur = 'until';                       // выбрали час — значит «до ЧЧ:00»
        paintDuration(st);
      }
    });

    document.addEventListener('input', e => {
      const st = W;
      if(!st || !st.loaded || !e.target || e.target.id !== 'bc-playlist-search') return;
      st.query = e.target.value;
      renderList(st);
    });

    // Esc во время отправки окно не закрывает (modal.js смотрит на defaultPrevented)
    document.addEventListener('keydown', e => {
      if(e.key === 'Escape' && W && W.busy) e.preventDefault();
    }, true);
  }

  window.Signage = window.Signage || {};
  window.Signage.renderBroadcastBar = renderBroadcastBar;
  window.Signage.openBroadcastWindow = openBroadcastWindow;

  initBroadcastActions();
})();
