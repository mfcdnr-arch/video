//=============================================================================
// РЕКЛАМОДАТЕЛИ: карточка одного рекламодателя — эфир, доля, доставка, ролики
//
// Раньше эти данные приходилось собирать из Медиатеки, Отчётов, Кампаний и
// Биллинга. Здесь всё про одного рекламодателя в одном месте — и ровно то,
// что можно доказать журналом показов и телеметрией экранов. Контакты/охват
// сознательно не считаем: без камер и данных о трафике такую цифру нечем
// подтвердить перед рекламодателем.
//=============================================================================

const ADV_TABS = [
  ['overview', 'Обзор'],
  ['airtime', 'Эфир'],
  ['delivery', 'Качество доставки'],
  ['creatives', 'Ролики и 38-ФЗ'],
  ['docs', 'Документы'],
];

const ADV_DOW = ['', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
const ADV_VENUES = {store_alcohol: 'Магазин (алко)', store: 'Магазин', mall: 'ТЦ',
                    office: 'Офис', other: 'Прочее'};
const ADV_REVIEW = {approved: '✅ одобрен', pending: '⏳ на модерации', rejected: '⛔ отклонён'};

// Состояние раздела: выбранный рекламодатель, вкладка и период.
let ADV_STATE = {id: null, tab: 'overview', from: null, to: null};

function advDefaultPeriod(){
  const now = new Date();
  const first = new Date(now.getFullYear(), now.getMonth(), 1);
  const iso = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
                 + '-' + String(d.getDate()).padStart(2, '0');
  return [iso(first), iso(now)];
}

function initAdvertisersViewActions(){
  if(window.__advertisersViewActionsInitialized) return;
  window.__advertisersViewActionsInitialized = true;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;
    const action = el.dataset.action;
    if(!action || !action.startsWith('adv-')) return;

    switch(action){
      case 'adv-open':
        ADV_STATE.id = Number(el.dataset.advId);
        ADV_STATE.tab = 'overview';
        return advRender();
      case 'adv-back':
        ADV_STATE.id = null;
        return advRender();
      case 'adv-tab':
        ADV_STATE.tab = el.dataset.tab;
        return advRender();
      case 'adv-apply-period':
        ADV_STATE.from = val('adv-from');
        ADV_STATE.to = val('adv-to');
        return advRender();
      case 'adv-export-xlsx':
        return advExportAirtime();
      case 'adv-gen-docs':
        return advGenerateDocs(false);
      case 'adv-regen-docs':
        return advGenerateDocs(true);
      case 'adv-doc-download':
        return advDownload(
          `/advertisers/${ADV_STATE.id}/documents/${el.dataset.docId}/download`,
          el.dataset.docName || 'document');
      case 'adv-save-note':
        return advSaveNote();
      case 'adv-save-req':
        return advSaveRequisites();
      case 'adv-add-contract':
        return advAddContract();
      case 'adv-del-contract':
        return advDeleteContract(Number(el.dataset.contractId), el.dataset.number || '');
      case 'adv-comp-apply':
        return advCompensate(el.dataset.kind, Number(el.dataset.amount || 0),
                             Number(el.dataset.plays || 0), Number(el.dataset.missed || 0));
      case 'adv-comp-decline':
        return advCompensate('discount', 0, 0, Number(el.dataset.missed || 0), 'declined');
    }
  });
}

// Скачивание защищённого файла: обычная ссылка не подходит — нужен заголовок
// с токеном, поэтому тянем через fetch и отдаём браузеру как blob.
async function advDownload(path, filename){
  try{
    const r = await fetch('/api' + path, {headers: TOKEN ? {'Authorization': 'Bearer ' + TOKEN} : {}});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }catch(e){ toast('Не удалось скачать: ' + e.message); }
}

async function advExportAirtime(){
  toast('Готовим выгрузку…');
  await advDownload(
    `/advertisers/${ADV_STATE.id}/airtime.xlsx?${advPeriodQuery()}`,
    `Выходы_${ADV_STATE.from}_${ADV_STATE.to}.xlsx`);
}

async function advGenerateDocs(regenerate){
  if(regenerate && !confirm('Собрать НОВУЮ версию документов за этот период?\n\n'
      + 'Прежние останутся в реестре — так подписанный экземпляр не разойдётся с копией.')) return;
  try{
    toast('Формируем документы…');
    const r = await api(`/advertisers/${ADV_STATE.id}/documents`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({date_from: ADV_STATE.from, date_to: ADV_STATE.to, regenerate})
    });
    toast(r.reused ? 'Документы за этот период уже были сформированы' : 'Документы готовы');
    advRender();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function advSaveNote(){
  try{
    await api(`/advertisers/${ADV_STATE.id}/note`, {
      method:'PATCH', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({note: document.getElementById('adv-note').value})
    });
    toast('Заметка сохранена');
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function advSaveRequisites(){
  const body = {};
  ['legal_name','inn','kpp','legal_address','contact_person','phone','email']
    .forEach(f => body[f] = val('advreq-' + f));
  try{
    await api(`/advertisers/${ADV_STATE.id}/requisites`, {
      method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    toast('Реквизиты сохранены');
    advRender();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function advAddContract(){
  const body = {
    number: val('ctr-number'),
    signed_on: val('ctr-signed') || null,
    valid_from: val('ctr-from') || null,
    valid_to: val('ctr-to') || null,
    period_kind: document.getElementById('ctr-kind').value,
    period_days: val('ctr-days') || null,
    period_anchor: val('ctr-anchor') || null,
    payment_days: Number(val('ctr-pay') || 5),
  };
  if(!body.number){ toast('Укажите номер договора'); return; }
  try{
    await api(`/advertisers/${ADV_STATE.id}/contracts`, {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    toast('Договор добавлен');
    advRender();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function advCompensate(kind, amount, plays, missed, decision){
  decision = decision || 'applied';
  const what = decision === 'declined'
    ? 'Зафиксировать, что компенсация НЕ предоставляется?'
    : (kind === 'discount'
        ? `Зафиксировать скидку ${amount.toFixed(2)} ₽ за недоставленные выходы?\n\n`
          + 'Сумма счёта при этом НЕ изменится — скидку проводят корректировкой счёта отдельно.'
        : `Зафиксировать ${plays} допоказов в следующем периоде?`);
  const note = prompt(what + '\n\nКомментарий (необязательно):');
  if(note === null) return;
  try{
    await api(`/advertisers/${ADV_STATE.id}/compensation`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({date_from: ADV_STATE.from, date_to: ADV_STATE.to,
                            kind, amount, plays, missed_plays: missed,
                            decision, note})
    });
    toast('Решение зафиксировано');
    advRender();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function advDeleteContract(id, number){
  if(!confirm(`Удалить договор № ${number}?`)) return;
  try{
    await api('/contracts/' + id, {method:'DELETE'});
    toast('Договор удалён');
    advRender();
  }catch(e){ toast('Ошибка: ' + e.message); }
}
initAdvertisersViewActions();

// Точка входа из меню: всегда открываем список. Иначе, уйдя в другой раздел
// и вернувшись, пользователь попадал бы внутрь карточки, открытой десять
// минут назад, и не понимал, почему видит одного рекламодателя.
async function viewAdvertisers(){
  ADV_STATE.tab = 'overview';
  // Рекламодателю списка чужих кабинетов не существует: сразу открываем его
  // собственный. Какой именно — говорит сервер по токену, не панель.
  if(ME.role === 'advertiser'){
    try{
      const mine = await api('/advertisers/me');
      ADV_STATE.id = mine.advertiser_id;
    }catch(e){
      document.getElementById('view').innerHTML =
        '<div class="empty">Учётная запись не привязана к рекламодателю. Обратитесь к администратору.</div>';
      return;
    }
    return advRender();
  }
  ADV_STATE.id = null;
  return advRender();
}

async function advRender(){
  const view = document.getElementById('view');
  const topright = document.getElementById('topright');
  topright.innerHTML = '';

  if(!ADV_STATE.from){ const [f, t] = advDefaultPeriod(); ADV_STATE.from = f; ADV_STATE.to = t; }

  if(!ADV_STATE.id) return advList(view);

  topright.innerHTML = ME.role === 'advertiser'
    ? ''    // рекламодателю возвращаться некуда — у него один кабинет
    : `<button class="btn" data-action="adv-back">← Все рекламодатели</button>`;
  view.innerHTML = '<div class="empty">Загрузка…</div>';

  const tabs = ADV_TABS.map(([k, label]) => `<button data-action="adv-tab" data-tab="${k}"
      style="padding:7px 20px;background:none;border:none;border-bottom:2px solid ${ADV_STATE.tab===k?'var(--txt)':'transparent'};color:${ADV_STATE.tab===k?'var(--txt)':'var(--muted)'};cursor:pointer;font-size:13px;font-weight:${ADV_STATE.tab===k?'600':'400'};">${label}</button>`).join('');
  const head = `<div style="display:flex;gap:0;margin-bottom:14px;border-bottom:1px solid var(--border);">${tabs}</div>
    <div class="row" style="align-items:flex-end;gap:10px;margin-bottom:14px;">
      <div class="fld"><label>Период с</label><input class="inp" type="date" id="adv-from" value="${ADV_STATE.from}"></div>
      <div class="fld"><label>по (включительно)</label><input class="inp" type="date" id="adv-to" value="${ADV_STATE.to}"></div>
      <button class="btn primary" data-action="adv-apply-period" style="margin-bottom:2px;">Показать</button>
    </div>`;

  try{
    let body = '';
    if(ADV_STATE.tab === 'overview')      body = await advOverview();
    else if(ADV_STATE.tab === 'airtime')  body = await advAirtime();
    else if(ADV_STATE.tab === 'delivery') body = await advDelivery();
    else if(ADV_STATE.tab === 'docs')     body = await advDocs();
    else                                  body = await advCreatives();
    view.innerHTML = head + body;
  }catch(e){
    view.innerHTML = head + '<div class="empty">Ошибка: ' + esc(e.message) + '</div>';
  }
}

// ─── Список рекламодателей ────────────────────────────────────────────────
async function advList(view){
  view.innerHTML = '<div class="empty">Загрузка…</div>';
  try{
    const advs = await api('/advertisers');
    if(!advs.length){
      view.innerHTML = '<div class="empty">Рекламодателей пока нет — добавьте их в Медиатеке</div>';
      return;
    }
    let h = '<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(260px,1fr));">';
    advs.forEach(a => {
      h += `<div class="cell" style="cursor:pointer;" data-action="adv-open" data-adv-id="${a.id}">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
          <span style="color:${a.color || '#7fe3c4'};">●</span>
          <span style="font-weight:500;flex:1;">${esc(a.name)}</span>
        </div>
        <div class="muted" style="font-size:11px;">${a.files || 0} роликов · тариф
          ${a.billing_mode === 'per_play'
            ? (a.price_per_play || 0) + ' ₽/показ'
            : (a.price_per_minute || 0) + ' ₽/мин'}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:6px;">Открыть карточку →</div>
      </div>`;
    });
    view.innerHTML = h + '</div>';
  }catch(e){
    view.innerHTML = '<div class="empty">Ошибка: ' + esc(e.message) + '</div>';
  }
}

function advPeriodQuery(){
  return `date_from=${encodeURIComponent(ADV_STATE.from)}&date_to=${encodeURIComponent(ADV_STATE.to)}`;
}

// ─── Вкладка «Обзор» ──────────────────────────────────────────────────────
async function advOverview(){
  const [d, alerts, live] = await Promise.all([
    api(`/advertisers/${ADV_STATE.id}/overview?${advPeriodQuery()}`),
    api(`/advertisers/${ADV_STATE.id}/alerts`).catch(() => ({items: [], total: 0})),
    api(`/advertisers/${ADV_STATE.id}/now-playing`).catch(() => ({screens: []})),
  ]);
  const t = d.totals, s = d.share, prev = d.previous || {};

  let h = `<div class="sec" style="margin-top:0;">${esc(d.advertiser.name)} · ${d.period.date_from} — ${d.period.date_to} (${d.period.days} дн.)</div>`;

  // Что требует внимания — самым верхом: отклонённый ролик или просроченный
  // счёт важнее любых цифр ниже.
  if(alerts.items && alerts.items.length){
    const col = {danger: 'var(--danger)', warn: '#ffd34d', info: 'var(--muted)'};
    const ico = {danger: '⛔', warn: '⏳', info: 'ⓘ'};
    h += '<div class="cell" style="margin-bottom:10px;">';
    h += `<div style="font-size:12px;font-weight:600;margin-bottom:6px;">Требует внимания (${alerts.items.length})</div>`;
    alerts.items.forEach(a => {
      h += `<div style="display:flex;gap:8px;font-size:12px;padding:3px 0;color:${col[a.level] || 'var(--txt2)'};">
        <span>${ico[a.level] || '•'}</span><span>${esc(a.text)}</span></div>`;
    });
    h += '</div>';
  }

  // Сейчас в эфире — ответ на самый частый вопрос клиента
  const liveScreens = (live && live.screens) || [];
  h += `<div class="cell" style="margin-bottom:10px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
    <span class="dot" style="background:${liveScreens.length ? 'var(--accent)' : 'var(--dim)'};"></span>
    <span style="font-size:12px;font-weight:600;">Сейчас в эфире</span>
    <span style="font-size:12px;color:var(--muted);flex:1;">${
      liveScreens.length
        ? liveScreens.map(x => esc(x.creative) + ' → ' + esc(x.name)
            + (x.display_connected === false ? ' <span style="color:var(--danger);" title="Монитор отключён от видеовыхода">📺</span>' : '')).join(' · ')
        : 'в эту минуту ни один ролик не воспроизводится'}</span>
  </div>`;

  h += '<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));">';
  const arrow = (v) => v == null ? '' : (v > 0 ? `▲ ${v}%` : (v < 0 ? `▼ ${Math.abs(v)}%` : '= без изменений'));
  const acol = (v) => v == null ? undefined : (v > 0 ? 'var(--accent)' : (v < 0 ? 'var(--danger)' : undefined));
  h += kpi('Выходов', t.plays, t.plays_per_day + ' в среднем в день');
  h += kpi('Минут в эфире', t.minutes, '');
  h += kpi('Экранов', t.screens, t.creatives + ' роликов');
  h += kpi('Доля эфира', s.pct_all != null ? s.pct_all + '%' : '—',
           s.pct_commercial != null ? s.pct_commercial + '% среди рекламы' : '');
  h += kpi('К оплате', Number(d.money.amount).toFixed(2) + ' ₽',
           d.money.billing_mode === 'per_play' ? d.money.price + ' ₽/показ' : d.money.price + ' ₽/мин');
  h += '</div>';

  // Сравнение с предыдущим периодом такой же длины
  if(prev.plays != null){
    h += `<div class="cell" style="margin-top:8px;font-size:12px;display:flex;gap:18px;flex-wrap:wrap;align-items:center;">
      <span class="muted">К предыдущему периоду (${prev.date_from} — ${prev.date_to})${
        prev.plays === 0 ? ' — в нём показов не было, сравнивать не с чем' : ''}:</span>
      <span>выходы <b>${prev.plays}</b> → <b>${t.plays}</b>
        <span style="color:${acol(prev.plays_delta_pct) || 'var(--muted)'};">${arrow(prev.plays_delta_pct) || '—'}</span></span>
      <span>минуты <b>${prev.minutes}</b> → <b>${t.minutes}</b>
        <span style="color:${acol(prev.minutes_delta_pct) || 'var(--muted)'};">${arrow(prev.minutes_delta_pct) || '—'}</span></span>
    </div>`;
  }

  // По дням
  h += '<div class="sec">Выходы по дням</div>';
  if(!d.by_day.length){
    h += '<div class="empty">За период показов не было</div>';
  }else{
    const max = Math.max(...d.by_day.map(x => x.plays));
    h += '<div class="cell">';
    d.by_day.forEach(x => {
      const dt = new Date(x.date + 'T00:00:00');
      const w = max ? Math.round(x.plays / max * 100) : 0;
      h += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:3px;font-size:12px;">
        <span class="muted" style="width:104px;flex-shrink:0;">${ADV_DOW[dt.getDay()===0?7:dt.getDay()]}, ${x.date}</span>
        <span style="flex:1;background:var(--panel);border-radius:3px;height:14px;overflow:hidden;">
          <span style="display:block;height:100%;width:${w}%;background:var(--accent);"></span></span>
        <span style="width:96px;text-align:right;">${x.plays} · ${x.minutes} мин</span>
      </div>`;
    });
    h += '</div>';
  }

  // Тепловая карта день недели × час — видно, попадает ли реклама в прайм
  h += '<div class="sec">Когда крутилось (день недели × час, МСК)</div>';
  const grid = {};
  let hmax = 0;
  (d.heatmap || []).forEach(c => { grid[c.dow + ':' + c.hour] = c.plays; if(c.plays > hmax) hmax = c.plays; });
  if(!hmax){
    h += '<div class="empty">Нет данных за период</div>';
  }else{
    // width:auto — иначе глобальное правило table{width:100%} растягивает
    // сетку на всю ширину и клетки уезжают к правому краю.
    h += '<div class="cell" style="overflow-x:auto;"><table style="width:auto;border-collapse:collapse;font-size:10px;">';
    h += '<tr><td></td>';
    for(let hh = 0; hh < 24; hh++) h += `<td style="padding:0 2px;text-align:center;color:var(--muted);border:none;">${hh}</td>`;
    h += '</tr>';
    for(let dw = 1; dw <= 7; dw++){
      h += `<tr><td style="padding:0 6px 0 0;color:var(--muted);border:none;">${ADV_DOW[dw]}</td>`;
      for(let hh = 0; hh < 24; hh++){
        const v = grid[dw + ':' + hh] || 0;
        const op = v ? (0.15 + 0.85 * v / hmax) : 0;
        h += `<td title="${ADV_DOW[dw]}, ${hh}:00 — ${v} выходов"
          style="padding:0;width:15px;height:15px;background:${v ? `rgba(127,227,196,${op})` : 'var(--panel)'};border:1px solid var(--bg);"></td>`;
      }
      h += '</tr>';
    }
    h += '</table></div>';
  }

  // Экраны
  h += '<div class="sec">Где крутилось</div>';
  if(!d.by_screen.length){
    h += '<div class="empty">Нет показов за период</div>';
  }else{
    h += '<div class="cell"><table style="font-size:12px;">'
       + '<tr><th style="text-align:left;">Экран</th><th style="text-align:left;">Адрес</th><th>Тип</th><th style="text-align:right;">Выходов</th><th style="text-align:right;">Минут</th></tr>';
    d.by_screen.forEach(s2 => {
      h += `<tr><td>${esc(s2.name)}</td><td class="muted">${esc([s2.city, s2.location].filter(Boolean).join(', '))}</td>
        <td class="muted" style="text-align:center;">${ADV_VENUES[s2.venue_type] || '—'}</td>
        <td style="text-align:right;">${s2.plays}</td><td style="text-align:right;">${s2.minutes}</td></tr>`;
    });
    h += '</table></div>';
  }

  // Блок для администратора: договор, долги, тариф, заметки. Рекламодателю
  // это не показываем — ему нужны его выходы и документы, а не наша кухня.
  if(canWrite()) h += await advAdminBlock();

  // Счета
  h += '<div class="sec">Счета</div>';
  if(!d.invoices.length){
    h += '<div class="empty">Счетов пока нет</div>';
  }else{
    h += '<div class="cell"><table style="font-size:12px;">'
       + '<tr><th style="text-align:left;">Период</th><th style="text-align:right;">Сумма</th><th>Статус</th><th>Оплатить до</th></tr>';
    d.invoices.forEach(i => {
      const total = Number(i.amount) + Number(i.adjustment_amount || 0);
      const st = {issued: 'Выставлен', paid: 'Оплачен', canceled: 'Отменён'}[i.status] || i.status;
      h += `<tr><td>${i.period_start} — ${i.period_end}</td>
        <td style="text-align:right;">${total.toFixed(2)} ₽</td>
        <td style="text-align:center;">${st}</td>
        <td style="text-align:center;" class="muted">${i.due_date || '—'}</td></tr>`;
    });
    h += '</table></div>';
  }
  return h;
}

// ─── Вкладка «Эфир» (сырой журнал выходов) ────────────────────────────────
async function advAirtime(){
  const d = await api(`/advertisers/${ADV_STATE.id}/airtime?${advPeriodQuery()}&limit=500`);
  let h = `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
      <div class="sec" style="margin-top:0;flex:1;">Журнал выходов — ${d.total} записей за период`
        + (d.total > d.items.length ? ` (показаны первые ${d.items.length})` : '') + `</div>
      <button class="btn" data-action="adv-export-xlsx" title="Выгрузить все выходы за период в Excel">↓ Excel</button>
    </div>`;
  h += '<div class="muted" style="font-size:11px;margin-bottom:8px;">Каждая строка — один фактический показ.'
     + ' Это и есть подтверждение размещения: на его основе формируется эфирная справка.'
     + ' В Excel выгружается весь период целиком, без ограничения на число строк.</div>';
  if(!d.items.length) return h + '<div class="empty">За период показов не было</div>';

  h += '<div class="cell" style="overflow-x:auto;"><table style="font-size:12px;">'
     + '<tr><th style="text-align:left;">Дата и время (МСК)</th><th>День</th><th style="text-align:left;">Ролик</th>'
     + '<th style="text-align:left;">Экран</th><th style="text-align:left;">Адрес</th><th style="text-align:right;">Сек</th></tr>';
  d.items.forEach(r => {
    const dt = String(r.started_msk).replace('T', ' ').slice(0, 19);
    h += `<tr><td>${dt}</td><td style="text-align:center;" class="muted">${ADV_DOW[r.dow] || ''}</td>
      <td>${esc(r.creative)}</td><td>${esc(r.screen)}</td>
      <td class="muted">${esc([r.city, r.location].filter(Boolean).join(', '))}</td>
      <td style="text-align:right;">${r.seconds}</td></tr>`;
  });
  return h + '</table></div>';
}

// ─── Вкладка «Качество доставки» ──────────────────────────────────────────
async function advDelivery(){
  const d = await api(`/advertisers/${ADV_STATE.id}/delivery?${advPeriodQuery()}`);
  let h = '<div class="sec" style="margin-top:0;">Доступность экранов в оплаченный период</div>';
  h += '<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));">';
  h += kpi('Экранов в размещении', d.totals.screens, '');
  h += kpi('Часов простоя', d.totals.offline_hours, 'экран не выходил на связь',
           d.totals.offline_hours > 0 ? 'var(--danger)' : undefined);
  h += kpi('Часов без монитора', d.totals.display_off_hours, 'играло «в стену»',
           d.totals.display_off_hours > 0 ? 'var(--danger)' : undefined);
  h += '</div>';

  if(d.totals.screens_without_telemetry){
    h += `<div class="cell" style="margin-top:8px;font-size:12px;color:var(--muted);">
      ⓘ По ${d.totals.screens_without_telemetry} экрану(ам) телеметрии за период нет — показы были,
      значит экран работал, но точную доступность подтвердить нечем. В отчёт для рекламодателя
      такие строки идут с прочерком, а не с нулём.</div>`;
  }

  if(d.screens.length){
    h += '<div class="cell" style="margin-top:8px;"><table style="font-size:12px;">'
       + '<tr><th style="text-align:left;">Экран</th><th style="text-align:right;">Выходов</th>'
       + '<th style="text-align:right;">Простой, ч</th><th style="text-align:right;">Без монитора, ч</th>'
       + '<th style="text-align:right;">Доступность</th></tr>';
    d.screens.forEach(s => {
      const av = s.availability_pct;
      const col = av == null ? 'var(--muted)' : (av >= 95 ? 'var(--accent)' : 'var(--danger)');
      h += `<tr><td>${esc(s.name)}</td><td style="text-align:right;">${s.plays}</td>
        <td style="text-align:right;">${s.offline_hours == null ? '—' : s.offline_hours}</td>
        <td style="text-align:right;">${s.display_off_hours == null ? '—' : s.display_off_hours}</td>
        <td style="text-align:right;color:${col};">${av == null ? 'нет данных' : av + '%'}</td></tr>`;
    });
    h += '</table></div>';
  }

  h += '<div class="sec">План и факт по кампаниям</div>';
  if(!d.campaigns.length){
    h += '<div class="empty">Кампаний, пересекающихся с периодом, нет</div>';
  }else{
    h += '<div class="cell"><table style="font-size:12px;">'
       + '<tr><th style="text-align:left;">Кампания</th><th>Период</th><th style="text-align:right;">План</th>'
       + '<th style="text-align:right;">Факт</th><th style="text-align:right;">Выполнение</th><th style="text-align:right;">Недобор</th></tr>';
    d.campaigns.forEach(c => {
      const pct = c.fulfillment_pct;
      const col = pct == null ? 'var(--muted)' : (pct >= 95 ? 'var(--accent)' : '#ffd34d');
      h += `<tr><td>${esc(c.name)}</td><td class="muted" style="text-align:center;">${c.date_from} — ${c.date_to}</td>
        <td style="text-align:right;">${c.plan_to_date}</td><td style="text-align:right;">${c.fact_to_date}</td>
        <td style="text-align:right;color:${col};">${pct == null ? '—' : pct + '%'}</td>
        <td style="text-align:right;">${c.shortfall}</td></tr>`;
    });
    h += '</table></div>';
  }
  if(canWrite()) h += await advCompensationBlock();
  return h;
}

// ─── Компенсация за недоставленные выходы ─────────────────────────────────
// Система считает и предлагает, решение принимает человек. Сумма счёта сама
// не меняется никогда — иначе счёт «уезжал» бы без ведома того, кто его выставил.
async function advCompensationBlock(){
  let c;
  try{ c = await api(`/advertisers/${ADV_STATE.id}/compensation?${advPeriodQuery()}`); }
  catch(e){ return ''; }

  let h = '<div class="sec">Компенсация за недобор</div><div class="cell">';
  if(!c.plan){
    h += '<div class="muted" style="font-size:12px;">За период нет кампаний с планом — считать недобор не от чего.</div></div>';
    return h;
  }

  h += `<div style="display:flex;gap:20px;flex-wrap:wrap;font-size:12px;margin-bottom:8px;">
    <span>План <b>${c.plan}</b> · факт <b>${c.fact}</b> · недобор
      <b style="color:${c.shortfall ? 'var(--danger)' : 'var(--accent)'};">${c.shortfall}</b>
      (${c.shortfall_pct}%)</span>
    <span class="muted">из них по нашей вине: <b>${c.our_fault_plays}</b> (${c.our_fault_share_pct}% времени)</span>
  </div>`;

  if(c.by_reason.length){
    h += '<div style="font-size:12px;margin-bottom:8px;">';
    c.by_reason.forEach(r => {
      const v = r.hours != null ? r.hours + ' ч' : (r.plays != null ? r.plays + ' показов' : r.count + ' шт.');
      h += `<div style="padding:2px 0;color:${r.our_fault ? 'var(--danger)' : 'var(--muted)'};">
        ${r.our_fault ? '●' : '○'} ${esc(r.label)} — ${v}</div>`;
    });
    h += '</div>';
  }

  if(!c.proposal.significant){
    h += `<div class="muted" style="font-size:12px;">${esc(c.proposal.hint)}</div>`;
  }else{
    h += `<div style="font-size:12px;margin-bottom:8px;">Предложение: скидка
      <b>${Number(c.proposal.discount_amount).toFixed(2)} ₽</b> либо
      <b>${c.proposal.extra_plays}</b> допоказов.
      ${c.proposal.capped_by_period_amount
        ? '<span class="muted">Скидка ограничена суммой за период (' + Number(c.period_amount).toFixed(2) + ' ₽).</span>'
        : ''}</div>`;
    h += `<div style="display:flex;gap:8px;flex-wrap:wrap;">
      <button class="btn primary" data-action="adv-comp-apply" data-kind="discount"
        data-amount="${c.proposal.discount_amount}" data-missed="${c.shortfall}">Дать скидку</button>
      <button class="btn" data-action="adv-comp-apply" data-kind="extra_plays"
        data-plays="${c.proposal.extra_plays}" data-missed="${c.shortfall}">Дать допоказы</button>
      <button class="btn" data-action="adv-comp-decline" data-missed="${c.shortfall}">Не компенсировать</button>
    </div>
    <div class="muted" style="font-size:11px;margin-top:6px;">Решение попадёт в журнал и в акт.
      Сумма счёта автоматически не меняется — скидку проводят корректировкой счёта.</div>`;
  }

  if(c.decisions.length){
    h += '<div style="margin-top:10px;font-size:12px;"><span class="muted">Решения по периоду:</span>';
    c.decisions.forEach(d => {
      const what = d.status === 'declined' ? 'без компенсации'
        : (d.kind === 'discount' ? `скидка ${Number(d.applied_amount || 0).toFixed(2)} ₽`
                                 : `допоказы ${d.applied_plays || 0}`);
      h += `<div class="muted" style="font-size:11px;padding:2px 0;">
        ${esc(fmtServerTS(d.decided_at))} · ${esc(d.decided_by || '')} · ${what}${d.note ? ' · ' + esc(d.note) : ''}</div>`;
    });
    h += '</div>';
  }
  return h + '</div>';
}

// ─── Вкладка «Ролики и 38-ФЗ» ─────────────────────────────────────────────
async function advCreatives(){
  const rows = await api(`/advertisers/${ADV_STATE.id}/creatives`);
  let h = '<div class="sec" style="margin-top:0;">Ролики рекламодателя</div>';
  if(!rows.length) return h + '<div class="empty">Роликов нет</div>';

  h += '<div class="cell" style="overflow-x:auto;"><table style="font-size:12px;">'
     + '<tr><th style="text-align:left;">Ролик</th><th>Хрон.</th><th>Категория</th><th>Модерация</th>'
     + '<th>Срок действия</th><th style="text-align:right;">Показов</th><th>Хранить до</th></tr>';
  rows.forEach(r => {
    const rev = ADV_REVIEW[r.review_status] || r.review_status || '—';
    const revTitle = r.review_status === 'rejected' && r.reject_reason
      ? ' title="Причина: ' + esc(r.reject_reason) + '"'
      : (r.reviewed_by ? ` title="Решение: ${esc(r.reviewed_by)}, ${esc(fmtServerTS(r.reviewed_at))}"` : '');
    const valid = (r.valid_from || r.valid_until)
      ? `${r.valid_from ? String(r.valid_from).slice(0, 10) : '…'} — ${r.valid_until ? String(r.valid_until).slice(0, 10) : '…'}`
      : 'бессрочно';
    h += `<tr><td>${esc(r.title)}${r.is_broken ? ' <span style="color:var(--danger);" title="Не воспроизводится на экранах">⚠</span>' : ''}</td>
      <td style="text-align:center;" class="muted">${r.duration_seconds ? Math.round(r.duration_seconds) + ' с' : '—'}</td>
      <td style="text-align:center;" class="muted">${esc(r.category || '—')}</td>
      <td style="text-align:center;"${revTitle}>${rev}</td>
      <td style="text-align:center;" class="muted">${valid}</td>
      <td style="text-align:right;">${r.plays_total}</td>
      <td style="text-align:center;" class="muted" title="Год со дня последнего показа — ст. 12 ФЗ «О рекламе»">${r.keep_until || '—'}</td></tr>`;
  });
  h += '</table></div>';
  h += '<div class="muted" style="font-size:11px;margin-top:6px;">«Хранить до» — срок хранения подтверждений'
     + ' по ст. 12 ФЗ «О рекламе»: год со дня последнего распространения. Раньше этой даты удалять материалы нельзя.</div>';
  return h;
}

// ─── Вкладка «Документы» ──────────────────────────────────────────────────
// Рекламодателю здесь только его документы; реквизиты, договоры и кнопка
// формирования — администратору (сервер закрывает эти пути для роли
// advertiser независимо от того, что нарисовано в панели).
async function advDocs(){
  const isAdmin = canWrite();
  const [docs, contracts, adv] = await Promise.all([
    api(`/advertisers/${ADV_STATE.id}/documents`),
    isAdmin ? api(`/advertisers/${ADV_STATE.id}/contracts`).catch(() => []) : Promise.resolve([]),
    isAdmin ? api(`/advertisers/${ADV_STATE.id}/overview?${advPeriodQuery()}`).then(x => x.advertiser)
            : Promise.resolve(null),
  ]);

  let h = '';

  if(isAdmin){
    h += '<div class="sec" style="margin-top:0;">Сформировать документы за период</div>';
    h += `<div class="cell" style="margin-bottom:10px;">
      <div class="muted" style="font-size:12px;margin-bottom:8px;">За выбранный вверху период
      (${ADV_STATE.from} — ${ADV_STATE.to}) собираются сразу три документа: эфирная справка
      (PDF и Excel), акт оказанных услуг и сводный отчёт. Повторное нажатие ничего не
      пересобирает — отдаются уже сформированные, чтобы подписанный экземпляр не разошёлся с копией.</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <button class="btn primary" data-action="adv-gen-docs">Сформировать за период</button>
        <button class="btn" data-action="adv-regen-docs" title="Собрать новую версию; прежняя останется в реестре">↻ Новая версия</button>
      </div></div>`;
  }

  h += '<div class="sec">Документы</div>';
  if(!docs.length){
    h += '<div class="empty">Документов пока нет</div>';
  }else{
    h += '<div class="cell" style="overflow-x:auto;"><table style="font-size:12px;">'
       + '<tr><th style="text-align:left;">Документ</th><th>Номер</th><th>Период</th><th>Формат</th>'
       + '<th style="text-align:right;">Размер</th><th>Сформирован</th><th></th></tr>';
    docs.forEach(x => {
      const kb = x.size_bytes ? Math.max(1, Math.round(x.size_bytes / 1024)) + ' КБ' : '—';
      const fname = `${x.title} ${x.number}.${x.doc_format}`.replace(/[\\/:*?"<>|]/g, '-');
      h += `<tr><td>${esc(x.title)}</td>
        <td style="text-align:center;" class="muted">${esc(x.number || '—')}</td>
        <td style="text-align:center;" class="muted">${x.period_start} — ${x.period_end}</td>
        <td style="text-align:center;">${x.doc_format.toUpperCase()}</td>
        <td style="text-align:right;">${kb}</td>
        <td style="text-align:center;" class="muted">${esc(fmtServerTS(x.created_at))}</td>
        <td style="text-align:right;"><button class="btn" style="padding:3px 8px;font-size:12px;"
          data-action="adv-doc-download" data-doc-id="${x.id}" data-doc-name="${esc(fname)}">↓ Скачать</button></td></tr>`;
    });
    h += '</table></div>';
    h += '<div class="muted" style="font-size:11px;margin-top:6px;">Скачивание отдаёт именно тот файл,'
       + ' который был сформирован: документы не пересобираются заново.</div>';
  }

  if(!isAdmin) return h;

  // ── Реквизиты рекламодателя (идут в акт) ──
  h += '<div class="sec">Реквизиты заказчика</div>';
  h += `<div class="cell">
    <div class="muted" style="font-size:12px;margin-bottom:8px;">Попадают в акт и эфирную справку.
    Без них документы сформируются, но реквизиты заказчика в них будут пустыми.</div>
    <div class="row">
      <div class="fld" style="flex:2;"><label>Полное наименование</label>
        <input class="inp" id="advreq-legal_name" value="${esc(adv && adv.legal_name || '')}" placeholder='ООО «Кофейня Утро»'></div>
      <div class="fld" style="flex:1;"><label>ИНН</label>
        <input class="inp" id="advreq-inn" value="${esc(adv && adv.inn || '')}"></div>
      <div class="fld" style="flex:1;"><label>КПП</label>
        <input class="inp" id="advreq-kpp" value="${esc(adv && adv.kpp || '')}"></div>
    </div>
    <div class="fld"><label>Юридический адрес</label>
      <input class="inp" id="advreq-legal_address" value="${esc(adv && adv.legal_address || '')}"></div>
    <div class="row">
      <div class="fld" style="flex:1;"><label>Контактное лицо</label>
        <input class="inp" id="advreq-contact_person" value="${esc(adv && adv.contact_person || '')}"></div>
      <div class="fld" style="flex:1;"><label>Телефон</label>
        <input class="inp" id="advreq-phone" value="${esc(adv && adv.phone || '')}"></div>
      <div class="fld" style="flex:1;"><label>E-mail</label>
        <input class="inp" id="advreq-email" value="${esc(adv && adv.email || '')}"></div>
    </div>
    <button class="btn primary" data-action="adv-save-req">Сохранить реквизиты</button>
  </div>`;

  // ── Договоры ──
  h += '<div class="sec">Договоры</div>';
  if(contracts.length){
    h += '<div class="cell" style="margin-bottom:10px;overflow-x:auto;"><table style="font-size:12px;">'
       + '<tr><th style="text-align:left;">Номер</th><th>Подписан</th><th>Действует</th>'
       + '<th>Расчётный период</th><th>Оплата, дн.</th><th>Статус</th><th></th></tr>';
    contracts.forEach(c => {
      const period = c.period_kind === 'days'
        ? `каждые ${c.period_days} дн.${c.period_anchor ? ' от ' + c.period_anchor : ''}`
        : 'календарный месяц';
      const active = c.is_active
        ? '<span style="color:var(--accent);">действует</span>'
        : '<span class="muted">закрыт</span>';
      h += `<tr><td>${esc(c.number)}</td>
        <td style="text-align:center;" class="muted">${c.signed_on || '—'}</td>
        <td style="text-align:center;" class="muted">${(c.valid_from || '…') + ' — ' + (c.valid_to || '…')}</td>
        <td style="text-align:center;">${period}</td>
        <td style="text-align:center;">${c.payment_days}</td>
        <td style="text-align:center;">${active}</td>
        <td style="text-align:right;"><button class="btn danger" style="padding:3px 8px;font-size:12px;"
          data-action="adv-del-contract" data-contract-id="${c.id}" data-number="${esc(c.number)}">Удалить</button></td></tr>`;
    });
    h += '</table></div>';
  }else{
    h += '<div class="empty" style="margin-bottom:10px;">Договоров нет. Акт сформируется и без договора, но ссылки на основание в нём не будет.</div>';
  }
  h += `<div class="cell">
    <div style="font-size:12px;font-weight:600;margin-bottom:8px;">Добавить договор</div>
    <div class="row">
      <div class="fld" style="flex:1;"><label>Номер</label><input class="inp" id="ctr-number" placeholder="12/2026"></div>
      <div class="fld" style="flex:1;"><label>Подписан</label><input class="inp" type="date" id="ctr-signed"></div>
      <div class="fld" style="flex:1;"><label>Действует с</label><input class="inp" type="date" id="ctr-from"></div>
      <div class="fld" style="flex:1;"><label>по</label><input class="inp" type="date" id="ctr-to"></div>
    </div>
    <div class="row">
      <div class="fld" style="flex:1;"><label>Расчётный период</label>
        <select class="inp" id="ctr-kind">
          <option value="month">Календарный месяц</option>
          <option value="days">Произвольный, каждые N дней</option>
        </select></div>
      <div class="fld" style="flex:1;"><label>N дней</label><input class="inp" type="number" min="1" id="ctr-days" placeholder="30"></div>
      <div class="fld" style="flex:1;"><label>Отсчёт от даты</label><input class="inp" type="date" id="ctr-anchor"></div>
      <div class="fld" style="flex:1;"><label>Срок оплаты, дн.</label><input class="inp" type="number" min="1" id="ctr-pay" value="5"></div>
    </div>
    <button class="btn primary" data-action="adv-add-contract">Добавить договор</button>
  </div>`;
  return h;
}

// ─── Блок администратора в «Обзоре»: договор, дебиторка, тариф, заметки ────
async function advAdminBlock(){
  const [contracts, receivables, tariff, advInfo] = await Promise.all([
    api(`/advertisers/${ADV_STATE.id}/contracts`).catch(() => []),
    api('/billing/receivables').catch(() => []),
    api(`/advertisers/${ADV_STATE.id}/tariff-history`).catch(() => []),
    api('/advertisers').then(list => list.find(a => a.id === ADV_STATE.id) || {}).catch(() => ({})),
  ]);
  const debt = receivables.find(r => r.advertiser_id === ADV_STATE.id);
  const today = new Date().toISOString().slice(0, 10);
  const active = contracts.find(c => c.is_active
    && (!c.valid_from || c.valid_from <= today) && (!c.valid_to || c.valid_to >= today));

  let h = '<div class="sec">Договор и расчёты</div><div class="cell">';
  h += '<div style="display:flex;gap:22px;flex-wrap:wrap;font-size:12px;">';
  h += active
    ? `<span>Договор <b>№ ${esc(active.number)}</b> <span style="color:var(--accent);">действует</span>
        ${active.valid_to ? '<span class="muted">до ' + active.valid_to + '</span>' : ''}
        <span class="muted">· оплата ${active.payment_days} дн.</span></span>`
    : `<span style="color:#ffd34d;">Действующего договора нет</span>
       <span class="muted">— акт сформируется без ссылки на основание</span>`;
  if(debt){
    const over = debt.days_overdue > 0;
    h += `<span>Не оплачено: <b style="color:${over ? 'var(--danger)' : 'var(--txt)'};">
      ${Number(debt.unpaid_amount).toFixed(2)} ₽</b> (${debt.unpaid_count} сч.)
      ${over ? `<span style="color:var(--danger);">· просрочка ${debt.days_overdue} дн.</span>` : ''}</span>`;
    h += `<span class="muted">Оплачено всего: ${Number(debt.paid_amount).toFixed(2)} ₽</span>`;
  }else{
    h += '<span class="muted">Неоплаченных счетов нет</span>';
  }
  h += '</div>';

  if(tariff.length){
    h += '<div style="margin-top:10px;font-size:12px;"><span class="muted">Изменения тарифа:</span>';
    tariff.slice(0, 5).forEach(t => {
      h += `<div class="muted" style="font-size:11px;padding:2px 0;">${esc(fmtServerTS(t.created_at))} · ${esc(t.actor || '—')} · ${esc(t.detail || t.title)}</div>`;
    });
    h += '</div>';
  }

  h += `<div style="margin-top:10px;">
    <label class="muted" style="font-size:11px;">Заметки по клиенту (договорённости, особенности размещения)</label>
    <textarea class="inp" id="adv-note" rows="2" style="width:100%;resize:vertical;">${esc(advInfo.note || '')}</textarea>
    <button class="btn" style="margin-top:6px;" data-action="adv-save-note">Сохранить заметку</button>
  </div>`;
  h += '</div>';
  return h;
}
