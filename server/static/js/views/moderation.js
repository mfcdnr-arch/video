//=============================================================================
// МОДЕРАЦИЯ (38-ФЗ): одобрение рекламы перед выходом в эфир
//=============================================================================

function initModerationViewActions(){
  if(window.__moderationViewActionsInitialized) return;
  window.__moderationViewActionsInitialized = true;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('moderation-')) return;

    switch(action){
      case 'moderation-approve':
        return Signage.moderationApprove(Number(el.dataset.mediaId), el.dataset.mediaTitle || '');

      case 'moderation-reject':
        return Signage.moderationReject(Number(el.dataset.mediaId), el.dataset.mediaTitle || '');

      case 'moderation-preview':
        return window.open(API + '/files/download/' + Number(el.dataset.mediaId), '_blank');
    }
  });
}

const MOD_CAT_LABELS = {};

async function viewModeration(){
  const view = document.getElementById('view');
  view.innerHTML = '<div class="empty">Загрузка…</div>';

  let pending = [], cats = [];
  try{
    [pending, cats] = await Promise.all([api('/moderation/pending'), api('/moderation/categories')]);
  }catch(e){
    view.innerHTML = '<div class="empty">Ошибка: ' + esc(e.message) + '</div>';
    return;
  }
  cats.forEach(c => MOD_CAT_LABELS[c.key] = c.label);

  let h = `<div class="muted" style="font-size:12px;margin-bottom:11px;">
    Новая реклама выходит в эфир только после одобрения (закон «О рекламе», 38-ФЗ).
    Проверьте: соответствие заявленной категории, возрастную маркировку и наличие
    обязательного предупреждения в самом ролике. Решение фиксируется в журнале операций.
  </div>`;

  if(!pending.length){
    h += '<div class="empty">Очередь модерации пуста — всё проверено ✓</div>';
    view.innerHTML = h;
    return;
  }

  pending.forEach(m => {
    const cat = MOD_CAT_LABELS[m.category] || m.category;
    h += `<div class="cell" style="display:flex;gap:12px;margin-bottom:9px;align-items:flex-start;">
      <div style="width:120px;height:68px;flex-shrink:0;background:var(--bg2);border-radius:6px;overflow:hidden;position:relative;display:flex;align-items:center;justify-content:center;color:#454b57;">
        <img src="${API}/media/${m.id}/thumbnail" alt="" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover;" onerror="this.remove()">▶
      </div>
      <div style="flex:1;min-width:0;">
        <div style="font-weight:500;">${esc(m.title || m.filename)}</div>
        <div class="muted" style="font-size:11px;margin:2px 0;">
          ${esc(m.advertiser || 'без рекламодателя')} · ${m.duration_seconds ? Math.round(m.duration_seconds) + ' c' : ''}
          · загружен ${m.created_at ? fmtServerTS(m.created_at) : ''}
        </div>
        <div style="font-size:12px;margin:4px 0;">
          <b>${esc(cat)}</b>${m.age_rating ? ' · маркировка <b>' + esc(m.age_rating) + '</b>' : ''}
        </div>
        ${m.disclaimer_text ? `<div class="muted" style="font-size:11px;">Предупреждение: «${esc(m.disclaimer_text)}»</div>` : ''}
        ${m.license_number ? `<div class="muted" style="font-size:11px;">Лицензия: ${esc(m.license_number)}</div>` : ''}
      </div>
      <div style="display:flex;flex-direction:column;gap:6px;flex-shrink:0;">
        <button class="btn" data-action="moderation-preview" data-media-id="${m.id}">▶ Просмотр</button>
        <button class="btn primary" data-action="moderation-approve" data-media-id="${m.id}" data-media-title="${esc(m.title||'')}">✓ Одобрить</button>
        <button class="btn danger" data-action="moderation-reject" data-media-id="${m.id}" data-media-title="${esc(m.title||'')}">✕ Отклонить</button>
      </div>
    </div>`;
  });

  view.innerHTML = h;
}

async function moderationApprove(id, title){
  if(!confirm('Одобрить «' + title + '» к показу?\nВы подтверждаете, что ролик соответствует заявленной категории и закону о рекламе.')) return;
  try{
    await api('/media/' + id + '/approve', {method: 'POST'});
    toast('Одобрено — ролик доступен для эфира');
    viewModeration();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function moderationReject(id, title){
  const reason = prompt('Причина отклонения «' + title + '» (обязательно, попадёт в журнал):');
  if(reason === null) return;
  if(!reason.trim()){ toast('Причина обязательна'); return; }
  try{
    await api('/media/' + id + '/reject', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({reason: reason.trim()})});
    toast('Отклонено');
    viewModeration();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

window.Signage = window.Signage || {};
window.Signage.viewModeration = viewModeration;
window.Signage.moderationApprove = moderationApprove;
window.Signage.moderationReject = moderationReject;

initModerationViewActions();
