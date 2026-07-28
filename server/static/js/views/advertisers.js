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
    }
  });
}
initAdvertisersViewActions();

// Точка входа из меню: всегда открываем список. Иначе, уйдя в другой раздел
// и вернувшись, пользователь попадал бы внутрь карточки, открытой десять
// минут назад, и не понимал, почему видит одного рекламодателя.
async function viewAdvertisers(){
  ADV_STATE.id = null;
  ADV_STATE.tab = 'overview';
  return advRender();
}

async function advRender(){
  const view = document.getElementById('view');
  const topright = document.getElementById('topright');
  topright.innerHTML = '';

  if(!ADV_STATE.from){ const [f, t] = advDefaultPeriod(); ADV_STATE.from = f; ADV_STATE.to = t; }

  if(!ADV_STATE.id) return advList(view);

  topright.innerHTML = `<button class="btn" data-action="adv-back">← Все рекламодатели</button>`;
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
  const d = await api(`/advertisers/${ADV_STATE.id}/overview?${advPeriodQuery()}`);
  const t = d.totals, s = d.share;

  let h = `<div class="sec" style="margin-top:0;">${esc(d.advertiser.name)} · ${d.period.date_from} — ${d.period.date_to} (${d.period.days} дн.)</div>`;
  h += '<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));">';
  h += kpi('Выходов', t.plays, t.plays_per_day + ' в среднем в день');
  h += kpi('Минут в эфире', t.minutes, '');
  h += kpi('Экранов', t.screens, t.creatives + ' роликов');
  h += kpi('Доля эфира', s.pct_all != null ? s.pct_all + '%' : '—',
           s.pct_commercial != null ? s.pct_commercial + '% среди рекламы' : '');
  h += kpi('К оплате', Number(d.money.amount).toFixed(2) + ' ₽',
           d.money.billing_mode === 'per_play' ? d.money.price + ' ₽/показ' : d.money.price + ' ₽/мин');
  h += '</div>';

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
  let h = `<div class="sec" style="margin-top:0;">Журнал выходов — ${d.total} записей за период`
        + (d.total > d.items.length ? ` (показаны первые ${d.items.length})` : '') + '</div>';
  h += '<div class="muted" style="font-size:11px;margin-bottom:8px;">Каждая строка — один фактический показ.'
     + ' Это и есть подтверждение размещения: на его основе формируется эфирная справка.</div>';
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
    h += '<div class="muted" style="font-size:11px;margin-top:6px;">Недобор — основание для компенсации.'
       + ' Расчёт и решение появятся на следующем этапе; сумма счёта сама не меняется.</div>';
  }
  return h;
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
