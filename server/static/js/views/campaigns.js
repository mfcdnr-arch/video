//=============================================================================
// КАМПАНИИ: план/факт показов («клиент купил N показов в день»)
//=============================================================================

function initCampaignsViewActions(){
  if(window.__campaignsViewActionsInitialized) return;
  window.__campaignsViewActionsInitialized = true;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('campaigns-')) return;

    switch(action){
      case 'campaigns-open-create':
        return Signage.campaignCreateForm();

      case 'campaigns-submit-create':
        return Signage.campaignCreate();

      case 'campaigns-cancel-create':
        return Signage.viewCampaigns();

      case 'campaigns-toggle-daily':
        return Signage.campaignToggleDaily(Number(el.dataset.campaignId));

      case 'campaigns-toggle-active':
        return Signage.campaignToggleActive(Number(el.dataset.campaignId), el.dataset.isActive === 'true');

      case 'campaigns-delete':
        return Signage.campaignDelete(Number(el.dataset.campaignId), el.dataset.campaignName || '');
    }
  });
}

const CAMP_STATUS = {
  active:    ['в эфире',   'var(--accent)'],
  scheduled: ['ожидает',   'var(--muted)'],
  finished:  ['завершена', 'var(--dim)'],
  paused:    ['пауза',     '#ffd34d'],
};

async function viewCampaigns(){
  const view = document.getElementById('view');
  document.getElementById('topright').innerHTML = canWrite()
    ? `<button class="btn primary" data-action="campaigns-open-create">+ Кампания</button>` : '';
  view.innerHTML = '<div class="empty">Загрузка…</div>';

  let list = [];
  try{ list = await api('/campaigns'); }
  catch(e){ view.innerHTML = '<div class="empty">Ошибка: ' + esc(e.message) + '</div>'; return; }

  if(!list.length){
    view.innerHTML = `<div class="empty">Кампаний пока нет.<br>
      Кампания — это обязательство перед клиентом: «N показов в день с … по …».<br>
      Система сравнивает план с фактом из журнала показов и сразу подсвечивает недокрут.</div>`;
    return;
  }

  let h = '';
  list.forEach(c => {
    const [stLabel, stColor] = CAMP_STATUS[c.status] || [c.status, ''];
    const pct = c.fulfillment_pct;
    const behind = c.status === 'active' && pct !== null && pct < 90;
    const barPct = Math.min(100, pct || 0);
    const barColor = pct === null ? 'var(--dim)' : (pct >= 90 ? 'var(--accent)' : (pct >= 70 ? '#ffd34d' : 'var(--danger)'));

    h += `<div class="cell" style="margin-bottom:9px;${behind ? 'border-color:var(--danger);' : ''}">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        <span style="font-weight:600;">${esc(c.name)}</span>
        <span class="muted" style="font-size:12px;">${esc(c.advertiser)}</span>
        <span style="font-size:10px;background:${hexA('#8ab8ff',.15)};border-radius:4px;padding:1px 6px;color:${stColor};">${stLabel}</span>
        <span style="flex:1;"></span>
        <span class="muted" style="font-size:11px;">${c.date_from.split('-').reverse().join('.')} — ${c.date_to.split('-').reverse().join('.')}
          · ${c.target_plays_per_day}/день · ${c.group_name ? esc(c.group_name) : 'вся сеть'}</span>
      </div>
      <div style="display:flex;align-items:center;gap:10px;margin-top:8px;">
        <div style="flex:1;height:8px;background:var(--bg2);border-radius:5px;overflow:hidden;">
          <div style="width:${barPct}%;height:100%;background:${barColor};"></div>
        </div>
        <span style="font-size:12px;white-space:nowrap;">
          ${c.fact_to_date} / ${c.plan_to_date} показов
          ${pct !== null ? `(<b style="color:${barColor};">${pct}%</b>)` : ''}
        </span>
      </div>
      ${behind ? `<div style="font-size:11px;color:var(--danger);margin-top:5px;">⚠ Недокрут ${c.shortfall} показов — проверьте расписание/повторы ролика</div>` : ''}
      <div style="display:flex;gap:5px;margin-top:8px;">
        <button class="btn" data-action="campaigns-toggle-daily" data-campaign-id="${c.id}">▸ По дням</button>
        ${canWrite() ? `
          <button class="btn" data-action="campaigns-toggle-active" data-campaign-id="${c.id}" data-is-active="${c.is_active}">${c.is_active ? '⏸ Пауза' : '▶ Возобновить'}</button>
          <button class="btn danger" style="margin-left:auto;" data-action="campaigns-delete" data-campaign-id="${c.id}" data-campaign-name="${esc(c.name)}">Удалить</button>` : ''}
      </div>
      <div id="camp-daily-${c.id}"></div>
    </div>`;
  });

  view.innerHTML = h;
}

async function campaignToggleDaily(id){
  const box = document.getElementById('camp-daily-' + id);
  if(!box) return;
  if(box.innerHTML){ box.innerHTML = ''; return; }
  box.innerHTML = '<div class="muted" style="font-size:12px;margin-top:8px;">Загрузка…</div>';
  try{
    const c = await api('/campaigns/' + id);
    let h = `<table style="margin-top:9px;font-size:12px;"><tr><th>Дата</th><th>План</th><th>Факт</th><th></th></tr>`;
    c.daily.forEach(d => {
      const ok = d.fact >= d.plan;
      const mark = d.future ? '' : (ok ? '✓' : '⚠');
      const color = d.future ? 'var(--dim)' : (ok ? 'var(--accent)' : 'var(--danger)');
      h += `<tr style="${d.future ? 'opacity:.45;' : ''}">
        <td>${d.date.split('-').reverse().join('.')}</td>
        <td class="muted">${d.plan}</td>
        <td>${d.future ? '—' : d.fact}</td>
        <td style="color:${color};">${mark}</td></tr>`;
    });
    h += '</table>';
    box.innerHTML = h;
  }catch(e){ box.innerHTML = '<div class="muted">Ошибка: ' + esc(e.message) + '</div>'; }
}

async function campaignCreateForm(){
  document.getElementById('topright').innerHTML = '';
  let advs = [], groups = [];
  try{ [advs, groups] = await Promise.all([api('/advertisers'), api('/groups')]); }catch(e){}
  if(!advs.length){ toast('Сначала создайте рекламодателя в Медиатеке'); viewCampaigns(); return; }
  const today = new Date();
  const iso = d => d.toISOString().slice(0,10);
  const monthEnd = new Date(today.getFullYear(), today.getMonth()+1, 0);
  document.getElementById('view').innerHTML = `
    <div style="max-width:460px;">
      <div class="fld"><label>Название кампании</label><input class="inp" id="cmp-name" placeholder="Лето-2026 — Coca-Cola"></div>
      <div class="fld"><label>Рекламодатель</label>
        <select class="inp" id="cmp-adv">${advs.map(a=>`<option value="${a.id}">${esc(a.name)}</option>`).join('')}</select></div>
      <div class="row">
        <div class="fld" style="flex:1;"><label>С</label><input class="inp" type="date" id="cmp-from" value="${iso(today)}"></div>
        <div class="fld" style="flex:1;"><label>По (включительно)</label><input class="inp" type="date" id="cmp-to" value="${iso(monthEnd)}"></div>
      </div>
      <div class="fld"><label>Показов в день (план)</label><input class="inp" type="number" min="1" id="cmp-target" placeholder="500" style="width:140px;"></div>
      <div class="fld"><label>Охват</label>
        <select class="inp" id="cmp-group"><option value="">Вся сеть</option>
          ${groups.map(g=>`<option value="${g.id}">${esc(g.name)}</option>`).join('')}</select></div>
      <div class="fld"><label>Примечание (необязательно)</label><input class="inp" id="cmp-note"></div>
      <div style="display:flex;gap:8px;margin-top:12px;">
        <button class="btn primary" data-action="campaigns-submit-create">Создать кампанию</button>
        <button class="btn" data-action="campaigns-cancel-create">Отмена</button>
      </div>
    </div>`;
}

async function campaignCreate(){
  const name = val('cmp-name');
  const target = parseInt(val('cmp-target'), 10);
  if(!name){ toast('Введите название'); return; }
  if(isNaN(target) || target <= 0){ toast('Укажите план показов в день'); return; }
  const from = val('cmp-from'), to = val('cmp-to');
  if(!from || !to || from > to){ toast('Проверьте даты кампании'); return; }
  try{
    await api('/campaigns', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        advertiser_id: Number(val('cmp-adv')), name,
        date_from: from, date_to: to, target_plays_per_day: target,
        group_id: val('cmp-group') ? Number(val('cmp-group')) : null,
        note: val('cmp-note'),
      })});
    toast('Кампания создана');
    viewCampaigns();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function campaignToggleActive(id, isActive){
  try{
    await api('/campaigns/' + id, {method:'PATCH', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({is_active: !isActive})});
    toast(isActive ? 'Кампания на паузе' : 'Кампания возобновлена');
    viewCampaigns();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function campaignDelete(id, name){
  if(!confirm('Удалить кампанию «' + name + '»?\nИстория показов в журнале останется.')) return;
  try{
    await api('/campaigns/' + id, {method:'DELETE'});
    toast('Удалено');
    viewCampaigns();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

window.Signage = window.Signage || {};
window.Signage.viewCampaigns = viewCampaigns;
window.Signage.campaignCreateForm = campaignCreateForm;
window.Signage.campaignCreate = campaignCreate;
window.Signage.campaignToggleDaily = campaignToggleDaily;
window.Signage.campaignToggleActive = campaignToggleActive;
window.Signage.campaignDelete = campaignDelete;

initCampaignsViewActions();
