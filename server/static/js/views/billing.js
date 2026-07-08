//=============================================================================
// БИЛЛИНГ: тарифы рекламодателей, закрытие периода, счета
//=============================================================================

const BILL_MODE_LABELS = { per_minute: 'По минутам', per_play: 'По показам' };
const INV_STATUS_LABELS = { issued: 'Выставлен', paid: 'Оплачен', canceled: 'Отменён' };

function initBillingViewActions(){
  if(window.__billingViewActionsInitialized) return;
  window.__billingViewActionsInitialized = true;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('billing-')) return;

    switch(action){
      case 'billing-save-tariff':
        return Signage.saveBillingTariff(Number(el.dataset.advId));

      case 'billing-preview':
        return Signage.billingPreview();

      case 'billing-issue-invoice':
        return Signage.issueInvoice(Number(el.dataset.advId), el.dataset.advName || '');

      case 'billing-details':
        return Signage.toggleBillingDetails(Number(el.dataset.advId));

      case 'billing-invoice-paid':
        return Signage.setInvoiceStatus(Number(el.dataset.invId), 'paid');

      case 'billing-invoice-unpaid':
        return Signage.setInvoiceStatus(Number(el.dataset.invId), 'issued');

      case 'billing-invoice-cancel':
        return Signage.setInvoiceStatus(Number(el.dataset.invId), 'canceled');

      case 'billing-invoice-pdf':
        return Signage.downloadInvoicePdf(Number(el.dataset.invId));

      case 'billing-invoice-adjust':
        return Signage.adjustInvoice(Number(el.dataset.invId), el.dataset.current || '0');

      case 'billing-invoice-delete':
        return Signage.deleteInvoice(Number(el.dataset.invId));
    }
  });
}

function billMoney(v){ return Number(v || 0).toLocaleString('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' ₽'; }
function billPeriodDefaults(){
  const now = new Date();
  const first = new Date(now.getFullYear(), now.getMonth(), 1);
  const last = new Date(now.getFullYear(), now.getMonth() + 1, 0);
  const iso = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  return [iso(first), iso(last)];
}

async function viewBilling(){
  const view = document.getElementById('view');
  view.innerHTML = '<div class="empty">Загрузка…</div>';

  let advs = [], invoices = [];
  try{
    [advs, invoices] = await Promise.all([api('/advertisers'), api('/billing/invoices')]);
  }catch(e){
    view.innerHTML = '<div class="empty">Ошибка: ' + esc(e.message) + '</div>';
    return;
  }

  let h = '';

  // ── Тарифы ────────────────────────────────────────────────────────────
  h += '<div class="sec" style="margin-top:0;">Тарифы рекламодателей</div>';
  if(!advs.length){
    h += '<div class="empty">Рекламодателей пока нет — добавьте их в Медиатеке</div>';
  }else{
    advs.forEach(a => {
      const mode = a.billing_mode || 'per_minute';
      h += `<div class="cell" style="display:flex;align-items:center;gap:10px;margin-bottom:7px;flex-wrap:wrap;">
        <span style="color:${a.color || '#7fe3c4'};">●</span>
        <span style="flex:1;min-width:120px;">${esc(a.name)}</span>
        <select class="inp" id="bmode-${a.id}" style="width:130px;padding:5px 8px;" title="Способ расчёта">
          <option value="per_minute" ${mode === 'per_minute' ? 'selected' : ''}>По минутам</option>
          <option value="per_play" ${mode === 'per_play' ? 'selected' : ''}>По показам</option>
        </select>
        <span style="display:flex;align-items:center;gap:5px;white-space:nowrap;">
          <label class="muted" style="font-size:11px;">₽/мин</label>
          <input class="inp" id="bppm-${a.id}" value="${a.price_per_minute || 0}" style="width:75px;padding:5px 8px;text-align:right;" title="Цена за минуту эфира">
        </span>
        <span style="display:flex;align-items:center;gap:5px;white-space:nowrap;">
          <label class="muted" style="font-size:11px;">₽/показ</label>
          <input class="inp" id="bppp-${a.id}" value="${a.price_per_play || 0}" style="width:75px;padding:5px 8px;text-align:right;" title="Цена за один показ">
        </span>
        <button class="btn primary" data-action="billing-save-tariff" data-adv-id="${a.id}">Сохранить</button>
      </div>`;
    });
  }

  // ── Закрытие периода ──────────────────────────────────────────────────
  const [defFrom, defTo] = billPeriodDefaults();
  h += '<div class="sec">Закрытие периода</div>';
  h += `<div class="row" style="align-items:flex-end;gap:10px;">
    <div class="fld"><label>С</label><input class="inp" type="date" id="bill-from" value="${defFrom}"></div>
    <div class="fld"><label>По (включительно)</label><input class="inp" type="date" id="bill-to" value="${defTo}"></div>
    <button class="btn primary" data-action="billing-preview" style="margin-bottom:2px;">Рассчитать</button>
  </div>
  <div id="bill-preview"></div>`;

  // ── Счета ─────────────────────────────────────────────────────────────
  h += '<div class="sec">Счета</div>';
  h += '<div id="bill-invoices">' + renderInvoicesTable(invoices) + '</div>';

  view.innerHTML = h;
}

function renderInvoicesTable(invoices){
  if(!invoices.length) return '<div class="empty">Счетов пока нет</div>';
  let h = '<table><tr><th>№</th><th>Рекламодатель</th><th>Период</th><th>Расчёт</th><th>Объём</th><th>Сумма</th><th>Статус</th><th></th></tr>';
  invoices.forEach(i => {
    const period = new Date(i.period_start).toLocaleDateString('ru-RU') + ' — ' + new Date(i.period_end).toLocaleDateString('ru-RU');
    const volume = i.billing_mode === 'per_play'
      ? i.plays_total + ' пок. × ' + billMoney(i.price)
      : Number(i.minutes_total).toFixed(1) + ' мин × ' + billMoney(i.price);
    const stColor = i.status === 'paid' ? 'var(--accent2)' : (i.status === 'canceled' ? 'var(--danger)' : '');
    const adj = Number(i.adjustment_amount || 0);
    const totalDue = i.total_due != null ? Number(i.total_due) : Number(i.amount) + adj;
    let btns = `<button class="btn" data-action="billing-invoice-pdf" data-inv-id="${i.id}" title="Скачать PDF">PDF</button> `;
    if(i.status === 'issued'){
      btns += `<button class="btn primary" data-action="billing-invoice-paid" data-inv-id="${i.id}">Оплачен</button> `;
      btns += `<button class="btn" title="Скидка/доплата (например, за простой экранов)" data-action="billing-invoice-adjust" data-inv-id="${i.id}" data-current="${adj}">Скидка</button> `;
      btns += `<button class="btn" data-action="billing-invoice-cancel" data-inv-id="${i.id}">Отменить</button> `;
    }else if(i.status === 'paid'){
      btns += `<button class="btn" data-action="billing-invoice-unpaid" data-inv-id="${i.id}" title="Снять отметку об оплате">Вернуть</button> `;
    }else{ // canceled
      btns += `<button class="btn danger" data-action="billing-invoice-delete" data-inv-id="${i.id}">Удалить</button> `;
    }
    h += `<tr>
      <td>${i.id}</td>
      <td>${esc(i.advertiser)}</td>
      <td class="muted" style="font-size:12px;">${period}</td>
      <td class="muted" style="font-size:12px;">${BILL_MODE_LABELS[i.billing_mode] || esc(i.billing_mode)}</td>
      <td class="muted" style="font-size:12px;">${volume}</td>
      <td><b>${billMoney(totalDue)}</b>${adj ? `<div class="muted" style="font-size:10px;" title="${esc(i.adjustment_note||'')}">${billMoney(i.amount)} ${adj<0?'−':'+'} ${billMoney(Math.abs(adj)).replace(' ₽','')} ₽</div>` : ''}</td>
      <td style="color:${stColor};">${INV_STATUS_LABELS[i.status] || esc(i.status)}${i.paid_at ? '<div class="muted" style="font-size:10px;">' + new Date(i.paid_at).toLocaleDateString('ru-RU') + '</div>' : ''}</td>
      <td style="white-space:nowrap;">${btns}</td>
    </tr>`;
  });
  h += '</table>';
  return h;
}

async function saveBillingTariff(advId){
  const mode = val('bmode-' + advId);
  const ppm = parseFloat(val('bppm-' + advId).replace(',', '.'));
  const ppp = parseFloat(val('bppp-' + advId).replace(',', '.'));
  if(isNaN(ppm) || isNaN(ppp) || ppm < 0 || ppp < 0){ toast('Цены должны быть неотрицательными числами'); return; }
  try{
    await api('/advertisers/' + advId + '/billing', {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({billing_mode: mode, price_per_minute: ppm, price_per_play: ppp})
    });
    toast('Тариф сохранён');
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function billingPreview(){
  const from = val('bill-from'), to = val('bill-to');
  if(!from || !to){ toast('Укажите обе даты периода'); return; }
  const box = document.getElementById('bill-preview');
  box.innerHTML = '<div class="empty">Считаем…</div>';
  try{
    const data = await api('/billing/preview?period_start=' + from + '&period_end=' + to);
    if(!data.items.length){ box.innerHTML = '<div class="empty">Нет рекламодателей</div>'; return; }
    let h = '<table style="margin-top:11px;"><tr><th>Рекламодатель</th><th>Расчёт</th><th>Показы</th><th>Минуты</th><th>Сумма</th><th></th></tr>';
    data.items.forEach(it => {
      let action;
      if(it.existing_invoice_id){
        action = `<span class="muted" style="font-size:11px;">счёт №${it.existing_invoice_id} (${INV_STATUS_LABELS[it.existing_invoice_status] || it.existing_invoice_status})</span>`;
      }else if(it.amount <= 0){
        action = '<span class="muted" style="font-size:11px;">нет показов или тариф 0</span>';
      }else{
        action = `<button class="btn primary" data-action="billing-issue-invoice" data-adv-id="${it.advertiser_id}" data-adv-name="${esc(it.advertiser)}">Выставить счёт</button>`;
      }
      const detBtn = it.plays > 0
        ? `<button class="btn" data-action="billing-details" data-adv-id="${it.advertiser_id}" title="Детализация по роликам" style="margin-right:6px;">▸ Ролики</button>`
        : '';
      h += `<tr id="bprev-row-${it.advertiser_id}">
        <td>${esc(it.advertiser)}</td>
        <td class="muted" style="font-size:12px;">${BILL_MODE_LABELS[it.billing_mode]} (${billMoney(it.price)})</td>
        <td>${it.plays}</td>
        <td>${Number(it.minutes).toFixed(1)}</td>
        <td><b>${billMoney(it.amount)}</b></td>
        <td style="white-space:nowrap;">${detBtn}${action}</td>
      </tr>`;
    });
    h += `<tr><td colspan="4" style="text-align:right;"><b>Итого:</b></td><td><b>${billMoney(data.total)}</b></td><td></td></tr></table>`;
    box.innerHTML = h;

    // Справка о простое экранов за тот же период — основание для скидки.
    try{
      const dt = await api('/billing/downtime?period_start=' + from + '&period_end=' + to);
      const bad = (dt.screens || []).filter(s => s.offline_hours >= 0.5);
      let dh = '<div class="sec" style="font-size:12px;">Простой экранов за период</div>';
      if(!bad.length){
        dh += '<div class="muted" style="font-size:12px;">Существенных простоев не зафиксировано (менее 30 минут на экран).</div>';
      }else{
        dh += '<table style="max-width:520px;"><tr><th>Экран</th><th>Простой</th><th>Аптайм</th></tr>';
        bad.forEach(s => {
          dh += `<tr><td>${esc(s.name)}</td><td style="color:var(--danger);">${s.offline_hours} ч</td><td class="muted">${s.uptime_pct}%</td></tr>`;
        });
        dh += '</table><div class="muted" style="font-size:11px;margin-top:4px;">Показы в простое не тарифицируются автоматически. Если по договору положена компенсация — используйте кнопку «Скидка» на счёте.</div>';
      }
      box.insertAdjacentHTML('beforeend', dh);
    }catch(e){ /* справка не критична */ }
  }catch(e){ box.innerHTML = '<div class="empty">Ошибка: ' + esc(e.message) + '</div>'; }
}

async function toggleBillingDetails(advId){
  // повторное нажатие сворачивает детализацию
  const existing = document.getElementById('bdet-row-' + advId);
  if(existing){ existing.remove(); return; }

  const anchor = document.getElementById('bprev-row-' + advId);
  if(!anchor) return;
  const from = val('bill-from'), to = val('bill-to');

  const tr = document.createElement('tr');
  tr.id = 'bdet-row-' + advId;
  tr.innerHTML = '<td colspan="6" class="muted">Загрузка…</td>';
  anchor.after(tr);

  try{
    const d = await api('/billing/details?advertiser_id=' + advId + '&period_start=' + from + '&period_end=' + to);
    if(!d.items.length){
      tr.innerHTML = '<td colspan="6" class="muted">Показов за период нет</td>';
      return;
    }
    const priceLabel = d.billing_mode === 'per_play' ? billMoney(d.price) + '/показ' : billMoney(d.price) + '/мин';
    let inner = `<table style="margin:4px 0 4px 18px;width:calc(100% - 18px);">
      <tr><th>Ролик</th><th>Показы</th><th>Время показа</th><th>Ожидаемый доход (${priceLabel})</th></tr>`;
    d.items.forEach(it => {
      inner += `<tr>
        <td>${esc(it.title)}</td>
        <td>${it.plays}</td>
        <td>${Number(it.minutes).toFixed(1)} мин</td>
        <td><b>${billMoney(it.income)}</b></td>
      </tr>`;
    });
    inner += `<tr><td colspan="3" style="text-align:right;"><b>Итого:</b></td><td><b>${billMoney(d.total)}</b></td></tr></table>`;
    tr.innerHTML = '<td colspan="6" style="padding:0;">' + inner + '</td>';
  }catch(e){
    tr.innerHTML = '<td colspan="6" class="muted">Ошибка: ' + esc(e.message) + '</td>';
  }
}

async function issueInvoice(advId, advName){
  const from = val('bill-from'), to = val('bill-to');
  if(!confirm('Выставить счёт «' + advName + '» за период ' + from + ' — ' + to + '?\nТариф и объёмы будут зафиксированы.')) return;
  try{
    const inv = await api('/billing/invoices', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({advertiser_id: advId, period_start: from, period_end: to})
    });
    toast('Счёт №' + inv.id + ' на ' + billMoney(inv.amount) + ' выставлен');
    billingPreview();
    refreshInvoices();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function adjustInvoice(invId, current){
  const v = prompt('Скидка/доплата к счёту №' + invId + ', ₽.\nМинус — скидка (например, -500 за простой экранов). 0 — убрать корректировку.', current);
  if(v === null) return;
  const amount = parseFloat(String(v).replace(',', '.'));
  if(isNaN(amount)){ toast('Введите число'); return; }
  let note = null;
  if(amount !== 0){
    note = prompt('Основание (попадёт в PDF счёта):', 'Компенсация за простой экранов');
    if(note === null) return;
  }
  try{
    await api('/billing/invoices/' + invId, {method:'PATCH',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({adjustment_amount: amount, adjustment_note: note})});
    toast(amount === 0 ? 'Корректировка убрана' : 'Корректировка применена: ' + billMoney(amount));
    refreshInvoices();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function setInvoiceStatus(invId, status){
  if(status === 'canceled' && !confirm('Отменить счёт №' + invId + '?')) return;
  try{
    await api('/billing/invoices/' + invId, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status})
    });
    toast(status === 'paid' ? 'Отмечен как оплаченный' : (status === 'canceled' ? 'Счёт отменён' : 'Оплата снята'));
    refreshInvoices();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function deleteInvoice(invId){
  if(!confirm('Удалить счёт №' + invId + ' безвозвратно?')) return;
  try{
    await api('/billing/invoices/' + invId, {method: 'DELETE'});
    toast('Счёт удалён');
    refreshInvoices();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function downloadInvoicePdf(invId){
  toast('Готовим PDF…');
  try{
    const res = await fetch(API + '/billing/invoices/' + invId + '/pdf', {headers: {'Authorization': 'Bearer ' + TOKEN}});
    if(!res.ok){ const t = await res.text(); toast('Ошибка: ' + t.slice(0, 80)); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'invoice_' + invId + '.pdf';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function refreshInvoices(){
  const box = document.getElementById('bill-invoices');
  if(!box) return;
  try{
    const invoices = await api('/billing/invoices');
    box.innerHTML = renderInvoicesTable(invoices);
  }catch(e){}
}

window.Signage = window.Signage || {};
window.Signage.viewBilling = viewBilling;
window.Signage.saveBillingTariff = saveBillingTariff;
window.Signage.billingPreview = billingPreview;
window.Signage.toggleBillingDetails = toggleBillingDetails;
window.Signage.issueInvoice = issueInvoice;
window.Signage.setInvoiceStatus = setInvoiceStatus;
window.Signage.adjustInvoice = adjustInvoice;
window.Signage.deleteInvoice = deleteInvoice;
window.Signage.downloadInvoicePdf = downloadInvoicePdf;

initBillingViewActions();
