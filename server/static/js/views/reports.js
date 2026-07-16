function initReportsViewActions(){
  if(window.__reportsViewActionsInitialized) return;
  window.__reportsViewActionsInitialized = true;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('reports-')) return;

    switch(action){
      case 'reports-open-playlog':
        return openPlaylog();

      case 'reports-open-builder':
        return openBuilder();

      case 'reports-open-export':
        return openExport();

      case 'reports-exp-all-true':
        return expAll(true);

      case 'reports-exp-all-false':
        return expAll(false);

      case 'reports-do-export-xlsx':
        return doExport('xlsx');

      case 'reports-do-export-pdf':
        return doExport('pdf');

      case 'reports-close-export':
        return closeExport();

      case 'reports-back-to-list':
        return viewReports();

      case 'reports-load-playlog':
        return loadPlaylog();

      case 'reports-export-fas':
        return exportFasReport();

      case 'reports-export-playlog-xlsx':
        return exportPlaylog('xlsx');

      case 'reports-export-playlog-pdf':
        return exportPlaylog('pdf');

      case 'reports-builder-run':
        return builderRun();

      case 'reports-builder-save':
        return builderSave();

      case 'reports-builder-export-xlsx':
        return builderExport('xlsx');

      case 'reports-builder-export-pdf':
        return builderExport('pdf');

      case 'reports-delete-saved': {
        const id = Number(el.dataset.savedId);
        return delSaved(id);
      }
      case 'reports-apply-saved': {
        const raw = el.dataset.savedConfig || '{}';
        return applySaved(JSON.parse(raw));
      }
    }
  });
  document.addEventListener('change', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('reports-')) return;

    switch(action){
      case 'reports-builder-filter-change':
        return builderFilterChange();
    }
  });
}

async function viewReports(){
  const view=document.getElementById('view');
  document.getElementById('topright').innerHTML=`<button class="btn" data-action="reports-open-playlog">📋 Журнал показов</button><button class="btn" data-action="reports-open-builder">⚒ Конструктор</button><button class="btn" title="Отчёт о соответствии закону о рекламе (38-ФЗ): декларации, решения модерации, показы" data-action="reports-export-fas">⚖ Для ФАС</button><button class="btn" data-action="reports-open-export">↓ Экспорт отчётов</button>`;
  view.innerHTML='<div class="empty">Загрузка отчётов…</div>';
  try{
    const [sum,offline,playtime,byScr,lowDisk,downtime,broken] = await Promise.all([
      api('/reports/summary').catch(()=>({})),
      api('/reports/offline-screens').catch(()=>({count:0,screens:[]})),
      api('/reports/playtime?days=7').catch(()=>({total:{},by_media:[],by_advertiser:[]})),
      api('/reports/by-screen-advertiser?days=7').catch(()=>[]),
      api('/reports/low-disk?threshold_pct=15').catch(()=>[]),
      api('/reports/downtime?hours_full=8').catch(()=>[]),
      api('/reports/broken-media').catch(()=>[]),
    ]);
    let h='<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));">';
    h+=kpi('Всего экранов', sum.total_screens??'—','');
    h+=kpi('Не работают', offline.count??0, offline.count?'см. список ниже':'все онлайн','var(--danger)');
    h+=kpi('Общее время показа', fmtDur(playtime.total&&playtime.total.seconds), (playtime.total&&playtime.total.plays||0)+' показов за 7 дней');
    h+=kpi('Нерабочих роликов', broken.length, broken.length?'требуют замены':'нет','var(--danger)');
    h+='</div>';

    // Занятость инвентаря (недельный шаблон)
    let inv=null; try{ inv=await api('/reports/inventory'); }catch(e){}
    if(inv && inv.screens_total){
      h+='<div class="rhead">Занятость инвентаря — недельный шаблон (сеть: '+inv.network_fill_pct+'% продано)</div>';
      inv.by_screen.forEach(s=>{
        const p=s.fill_pct||0;
        const col=p>=70?'var(--accent)':(p>=30?'#ffd34d':'var(--danger)');
        h+=`<div style="display:flex;align-items:center;gap:10px;margin-bottom:7px;">
          <span style="width:150px;font-size:12px;">${esc(s.name)}</span>
          <span style="flex:1;height:12px;background:var(--panel);border-radius:6px;overflow:hidden;">
            <span style="display:block;height:100%;width:${p}%;background:${col};"></span></span>
          <span class="muted" style="font-size:11px;width:110px;text-align:right;">${s.hours_busy} из 168 ч (${p}%)</span></div>`;
      });
      h+='<div class="muted" style="font-size:11px;margin-top:2px;">Незакрытые часы играют заглушки — это непроданное эфирное время.</div>';
    }

    // Неработающие экраны
    h+='<div class="rhead">Неработающие экраны ('+(offline.count||0)+')</div>';
    if(!offline.count){ h+='<div class="muted" style="font-size:12px;">Все экраны онлайн.</div>'; }
    else { h+='<table><tr><th>Экран</th><th>Город</th><th>Последний раз на связи</th></tr>';
      offline.screens.forEach(s=>{ h+=`<tr><td>${esc(s.name)}</td><td class="muted">${esc(s.city||'')}</td><td class="muted">${s.last_seen?new Date(s.last_seen).toLocaleString('ru-RU'):'—'}</td></tr>`; });
      h+='</table>'; }

    // Простой экранов (%)
    h+='<div class="rhead">Простой экранов (8 часов = 100%)</div>';
    if(!downtime.length){ h+='<div class="muted" style="font-size:12px;">Нет простаивающих экранов.</div>'; }
    else { downtime.forEach(s=>{ const p=s.downtime_pct||0;
      h+=`<div style="display:flex;align-items:center;gap:10px;margin-bottom:7px;"><span style="width:150px;font-size:12px;">${esc(s.name)}</span><span style="flex:1;height:12px;background:var(--panel);border-radius:6px;overflow:hidden;"><span style="display:block;height:100%;width:${p}%;background:var(--danger);"></span></span><span class="muted" style="font-size:11px;width:60px;text-align:right;">${p}%</span></div>`; }); }

    // ПК с памятью < 15%
    h+='<div class="rhead">ПК со свободной памятью < 15%</div>';
    if(!lowDisk.length){ h+='<div class="muted" style="font-size:12px;">Нет ПК с критично малой памятью.</div>'; }
    else { h+='<table><tr><th>Экран</th><th>Свободно</th><th>Всего</th><th>Свободно %</th></tr>';
      lowDisk.forEach(s=>{ h+=`<tr><td>${esc(s.name)}</td><td>${s.disk_free_gb!=null?Number(s.disk_free_gb).toFixed(0)+' ГБ':'—'}</td><td class="muted">${s.disk_total_gb!=null?Number(s.disk_total_gb).toFixed(0)+' ГБ':'—'}</td><td class="danger" style="color:var(--danger);">${s.free_pct!=null?s.free_pct+'%':'—'}</td></tr>`; });
      h+='</table>'; }

    // Нерабочие ролики
    h+='<div class="rhead">Нерабочие ролики</div>';
    if(!broken.length){ h+='<div class="muted" style="font-size:12px;">Нерабочих роликов нет.</div>'; }
    else { h+='<table><tr><th>Ролик</th><th>Рекламодатель</th><th>Ошибок</th><th>Последняя ошибка</th></tr>';
      broken.forEach(b=>{ h+=`<tr><td>${esc(b.title||b.filename)}</td><td class="muted">${esc(b.advertiser||'—')}</td><td>${b.error_count||1}</td><td class="muted" style="font-size:11px;">${esc(b.last_error||'')}</td></tr>`; });
      h+='</table>'; }

    // Время показа по роликам
    h+='<div class="rhead">Время показа и повторы — по роликам</div>';
    if(!playtime.by_media||!playtime.by_media.length){ h+='<div class="muted" style="font-size:12px;">Нет данных о показах за период.</div>'; }
    else { h+='<table><tr><th>Ролик</th><th>Рекламодатель</th><th>Повторов</th><th>Время показа</th></tr>';
      playtime.by_media.forEach(m=>{ h+=`<tr><td>${esc(m.title||m.filename)}</td><td class="muted">${esc(m.advertiser||'—')}</td><td>${m.plays}</td><td>${fmtDur(m.seconds)}</td></tr>`; });
      h+='</table>'; }

    // По рекламодателям
    h+='<div class="rhead">Время показа и повторы — по рекламодателям</div>';
    if(!playtime.by_advertiser||!playtime.by_advertiser.length){ h+='<div class="muted" style="font-size:12px;">Нет данных.</div>'; }
    else { h+='<table><tr><th>Рекламодатель</th><th>Повторов</th><th>Время показа</th></tr>';
      playtime.by_advertiser.forEach(a=>{ h+=`<tr><td>${esc(a.advertiser)}</td><td>${a.plays}</td><td>${fmtDur(a.seconds)}</td></tr>`; });
      h+='</table>'; }

    // Заработок по рекламодателям
    h+='<div class="rhead">Заработок по рекламодателям</div>';
    let earn=null; try{ earn=await api('/reports/earnings?days=30'); }catch(e){}
    if(!earn||!earn.by_advertiser||!earn.by_advertiser.length){ h+='<div class="muted" style="font-size:12px;">Нет данных.</div>'; }
    else {
      h+='<div class="muted" style="font-size:11px;margin-bottom:6px;">Расчётная оценка по прокрутам за 30 дней: показы × длительность ролика × цена за минуту. Цена задаётся в Медиатеке у рекламодателя.</div>';
      h+='<table><tr><th>Рекламодатель</th><th>Показов</th><th>Минут эфира</th><th>Цена ₽/мин</th><th>Заработано ₽</th></tr>';
      earn.by_advertiser.forEach(a=>{ h+=`<tr><td>${esc(a.advertiser)}</td><td>${a.plays}</td><td>${a.minutes}</td><td class="muted">${a.price_per_minute}</td><td style="font-weight:600;color:var(--accent);">${a.earnings.toLocaleString('ru-RU')}</td></tr>`; });
      h+=`<tr><td colspan="4" style="text-align:right;font-weight:600;">Итого:</td><td style="font-weight:700;color:var(--accent);">${earn.total_earnings.toLocaleString('ru-RU')} ₽</td></tr>`;
      h+='</table>'; }

    // Кто где сколько раз
    h+='<div class="rhead">Показы: ролик · рекламодатель · экран · количество</div>';
    if(!byScr.length){ h+='<div class="muted" style="font-size:12px;">Нет данных.</div>'; }
    else { h+='<table><tr><th>Экран</th><th>Рекламодатель</th><th>Ролик</th><th>Повторов</th></tr>';
      byScr.forEach(r=>{ h+=`<tr><td>${esc(r.screen||'—')}</td><td class="muted">${esc(r.advertiser||'—')}</td><td>${esc(r.title||'')}</td><td>${r.plays}</td></tr>`; });
      h+='</table>'; }

    // Сессии пользователей
    h+='<div class="rhead">Входы пользователей в панель</div>';
    let sessions=[]; try{ sessions=await api('/reports/sessions?days=30'); }catch(e){}
    if(!sessions.length){ h+='<div class="muted" style="font-size:12px;">Нет данных о входах.</div>'; }
    else { h+='<table><tr><th>Пользователь</th><th>Роль</th><th>Вход</th><th>Длительность</th></tr>';
      sessions.forEach(s=>{ h+=`<tr><td>${esc(s.username||'')}</td><td class="muted">${esc(roleLabel(s.role))}</td><td class="muted" style="font-size:12px;">${s.login_at?new Date(s.login_at).toLocaleString('ru-RU'):'—'}</td><td>${fmtDur(s.seconds)}</td></tr>`; });
      h+='</table>'; }

    // Сроки действия роликов
    h+='<div class="rhead">Сроки действия роликов</div>';
    let validity=[]; try{ validity=await api('/reports/validity'); }catch(e){}
    if(!validity.length){ h+='<div class="muted" style="font-size:12px;">Нет роликов.</div>'; }
    else { h+='<table><tr><th>Ролик</th><th>Рекламодатель</th><th>Показывать с</th><th>Показывать по</th><th>Статус</th></tr>';
      const vlabel={active:['в сроке','var(--accent)'],upcoming:['ещё не начался','var(--c-pepsi)'],expired:['просрочен','var(--danger)'],unlimited:['без ограничения','var(--muted)']};
      validity.forEach(v=>{ const vl=vlabel[v.validity_status]||['—','var(--muted)'];
        h+=`<tr><td>${esc(v.title)}</td><td class="muted">${esc(v.advertiser||'—')}</td><td class="muted" style="font-size:12px;">${v.valid_from?new Date(v.valid_from).toLocaleString('ru-RU'):'—'}</td><td class="muted" style="font-size:12px;">${v.valid_until?new Date(v.valid_until).toLocaleString('ru-RU'):'—'}</td><td style="color:${vl[1]};font-size:12px;">${vl[0]}</td></tr>`; });
      h+='</table>'; }

    // Потери связи с ПК
    h+='<div class="rhead">Потери связи с мини ПК</div>';
    let losses=[]; try{ losses=await api('/reports/connection-losses?days=30'); }catch(e){}
    if(!losses.length){ h+='<div class="muted" style="font-size:12px;">За период потерь связи не зафиксировано.</div>'; }
    else { h+='<table><tr><th>Мини ПК</th><th>Связь пропала</th><th>Восстановлена</th><th>Длительность</th><th>Статус</th></tr>';
      losses.forEach(l=>{ const secs=l.seconds||0; const dur=fmtDur(secs);
        h+=`<tr><td>${esc(l.screen||'—')}</td><td class="muted" style="font-size:12px;">${l.lost_at?new Date(l.lost_at).toLocaleString('ru-RU'):'—'}</td><td class="muted" style="font-size:12px;">${l.restored_at?new Date(l.restored_at).toLocaleString('ru-RU'):'—'}</td><td>${dur}</td><td style="color:${l.ongoing?'var(--danger)':'var(--accent)'};font-size:12px;">${l.ongoing?'нет связи':'восстановлена'}</td></tr>`; });
      h+='</table>'; }

    // Версии ПО (ОС и плеер mpv)
    h+='<div class="rhead">Версии ПО на мини ПК (ОС и плеер mpv)</div>';
    let vers=null; try{ vers=await api('/reports/versions'); }catch(e){}
    if(!vers||!vers.screens||!vers.screens.length){ h+='<div class="muted" style="font-size:12px;">Нет данных о версиях.</div>'; }
    else {
      const oc = vers.outdated_count!=null?vers.outdated_count:vers.mismatch_count;
      h+=`<div class="muted" style="font-size:12px;margin-bottom:8px;">Целевые версии — ОС: <b style="color:var(--text);">${esc(vers.target_os||'не задана')}</b>, плеер (mpv): <b style="color:var(--text);">${esc(vers.target_vlc||'не задана')}</b>. Устарели: <b style="color:${oc?'var(--danger)':'var(--accent)'};">${oc}</b>. Задать целевые версии можно в «Настройках».</div>`;
      const vstat=(s)=>s==='ok'?['актуальна','var(--accent)']:s==='outdated'?['устарела — обновить','var(--danger)']:['не сравнить','var(--muted)'];
      h+='<table><tr><th>Мини ПК</th><th>ОС на ПК</th><th>Статус ОС</th><th>Плеер (mpv)</th><th>Статус плеера</th></tr>';
      vers.screens.forEach(s=>{ const os=vstat(s.os_status), vl=vstat(s.vlc_status);
        h+=`<tr><td>${esc(s.name)}</td><td class="muted">${esc(s.os_version||'—')}</td><td style="color:${os[1]};font-size:12px;">${os[0]}</td><td class="muted">${esc(s.vlc_version||'—')}</td><td style="color:${vl[1]};font-size:12px;">${vl[0]}</td></tr>`; });
      h+='</table>'; }

    view.innerHTML=h;
  }catch(e){ view.innerHTML='<div class="empty">Ошибка: '+esc(e.message)+'</div>'; }
}
function fmtDur(sec){ sec=Math.round(sec||0); if(!sec) return '0 мин'; const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60); return (h?h+' ч ':'')+m+' мин'; }
async function exportFasReport(){
  const days = prompt('Отчёт для ФАС: за сколько последних дней?', '90');
  if(days===null) return;
  const n = parseInt(days,10);
  if(isNaN(n) || n<1 || n>3650){ toast('Введите число дней (1–3650)'); return; }
  toast('Готовим отчёт для ФАС…');
  try{
    const res = await fetch(API+'/reports/fas.pdf?days='+n, {headers:{'Authorization':'Bearer '+TOKEN}});
    if(!res.ok){ const t=await res.text(); toast('Ошибка: '+t.slice(0,80)); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'fas_report_'+n+'d.pdf';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url), 4000);
  }catch(e){ toast('Ошибка: '+e.message); }
}

async function exportReport(fmt){
  toast('Готовим '+(fmt==='xlsx'?'Excel':'PDF')+'…');
  try{
    const res = await fetch(API+'/reports/export.'+fmt+'?days=7', {headers:{'Authorization':'Bearer '+TOKEN}});
    if(!res.ok){ const t=await res.text(); toast('Ошибка экспорта: '+t.slice(0,80)); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'ds_report.'+(fmt==='xlsx'?'xlsx':'pdf');
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url), 4000);
  }catch(e){ toast('Ошибка: '+e.message); }
}
const EXPORT_SECTIONS=[
  ['offline','Неработающие экраны'],['downtime','Простой экранов'],
  ['lowdisk','Память < 15%'],['broken','Нерабочие ролики'],
  ['by_media','Время показа по роликам'],['by_advertiser','По рекламодателям'],
  ['by_screen','Показы по экранам'],['sessions','Входы пользователей'],
  ['validity','Сроки действия роликов'],['conn_loss','Потери связи с ПК'],
  ['earnings','Заработок по рекламодателям'],
];
function openExport(){
  const ov=document.createElement('div');
  ov.id='exp-ov';
  ov.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:80;';
  ov.innerHTML=`<div style="background:var(--panel);border:0.5px solid var(--border2);border-radius:12px;padding:20px;width:380px;max-width:92vw;">
    <div style="font-size:15px;font-weight:600;margin-bottom:14px;">Экспорт отчётов</div>
    <div class="fld"><label>Период</label>
      <select class="inp" id="exp-days"><option value="7">Последние 7 дней</option><option value="30">Последние 30 дней</option><option value="90">Последние 90 дней</option><option value="1">Сегодня</option></select></div>
    <div class="fld"><label>Какие отчёты включить</label>
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;"><button class="btn" style="padding:4px 10px;font-size:12px;" o<button class="btn" style="padding:4px 10px;font-size:12px;" data-action="reports-exp-all-true">Все</button>
<button class="btn" style="padding:4px 10px;font-size:12px;" data-action="reports-exp-all-false">Снять</button>
      <div style="max-height:190px;overflow:auto;border:0.5px solid var(--border2);border-radius:8px;padding:8px;">
        ${EXPORT_SECTIONS.map(s=>`<label style="display:flex;align-items:center;gap:8px;padding:5px 4px;font-size:13px;cursor:pointer;"><input type="checkbox" class="exp-chk" value="${s[0]}" checked> ${s[1]}</label>`).join('')}
      </div></div>
    <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;">
  <button class="btn" style="padding:4px 10px;font-size:12px;" data-action="reports-exp-all-true">Все</button>
  <button class="btn" style="padding:4px 10px;font-size:12px;" data-action="reports-exp-all-false">Снять</button>
</div>
<button class="btn primary" data-action="reports-do-export-xlsx">Скачать Excel</button>
<button class="btn primary" data-action="reports-do-export-pdf">Скачать PDF</button>
<button class="btn" style="margin-left:auto;" data-action="reports-close-export">Отмена</button>

    </div>
  </div>`;
  ov.addEventListener('click',e=>{ if(e.target===ov) closeExport(); });
  document.body.appendChild(ov);
}
function closeExport(){ const o=document.getElementById('exp-ov'); if(o) o.remove(); }
function expAll(v){ document.querySelectorAll('.exp-chk').forEach(c=>c.checked=v); }
async function doExport(fmt){
  const days=document.getElementById('exp-days').value;
  const keys=[...document.querySelectorAll('.exp-chk:checked')].map(c=>c.value);
  if(!keys.length){ toast('Выберите хотя бы один отчёт'); return; }
  closeExport();
  toast('Готовим '+(fmt==='xlsx'?'Excel':'PDF')+'…');
  try{
    const q=new URLSearchParams({days, sections:keys.join(',')});
    const res=await fetch(API+'/reports/export.'+fmt+'?'+q,{headers:{'Authorization':'Bearer '+TOKEN}});
    if(!res.ok){ const t=await res.text(); toast('Ошибка экспорта: '+t.slice(0,80)); return; }
    const blob=await res.blob(); const url=URL.createObjectURL(blob);
    const a=document.createElement('a'); a.href=url; a.download='ds_report.'+(fmt==='xlsx'?'xlsx':'pdf');
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url),4000);
  }catch(e){ toast('Ошибка: '+e.message); }
}

//=============================================================================
// ЖУРНАЛ ПОКАЗОВ
//=============================================================================
let PLAYLOG_ADVS=null;
async function openPlaylog(){
  const view=document.getElementById('view');
  document.getElementById('topright').innerHTML=`<button class="btn" data-action="reports-back-to-list">← К отчётам</button>`;

  document.getElementById('vtitle').textContent='Журнал показов';
  if(!PLAYLOG_ADVS){ try{ const a=await api('/advertisers'); PLAYLOG_ADVS=a.map(x=>x.name); }catch(e){ PLAYLOG_ADVS=[]; } }
  view.innerHTML=`
    <div class="muted" style="font-size:12px;margin-bottom:12px;">Журнал фактических показов роликов: дата, время, экран, ролик, рекламодатель. Можно отфильтровать и выгрузить — например, как доказательство показов для рекламодателя.</div>
    <div class="row" style="flex-wrap:wrap;align-items:end;">
      <div class="fld" style="min-width:160px;"><label>Период</label><select class="inp" id="pl-days"><option value="1">Сегодня</option><option value="7" selected>7 дней</option><option value="30">30 дней</option><option value="90">90 дней</option></select></div>
      <div class="fld" style="min-width:200px;"><label>Рекламодатель</label><select class="inp" id="pl-adv"><option value="">Все</option>${PLAYLOG_ADVS.map(a=>`<option>${esc(a)}</option>`).join('')}</select></div>
      <div class="fld" style="flex:0;"><button class="btn primary" data-action="reports-load-playlog">Показать</button></div>
<div class="fld" style="flex:0;"><button class="btn" data-action="reports-export-playlog-xlsx">Excel</button></div>
<div class="fld" style="flex:0;"><button class="btn" data-action="reports-export-playlog-pdf">PDF</button></div>
    </div>
    <div id="pl-summary"></div>
    <div id="pl-result"><div class="muted" style="font-size:12px;">Нажмите «Показать».</div></div>`;
  loadPlaylog();
}
function playlogParams(){
  const days=document.getElementById('pl-days').value;
  const adv=document.getElementById('pl-adv').value;
  const q=new URLSearchParams({days}); if(adv) q.append('advertiser',adv);
  return q;
}
async function loadPlaylog(){
  const q=playlogParams();
  document.getElementById('pl-result').innerHTML='<div class="muted" style="font-size:12px;">Загрузка…</div>';
  try{
    // сводка по рекламодателям
    const summ=await api('/playlog/by-advertiser?days='+document.getElementById('pl-days').value);
    let sh='';
    if(summ.length){
      sh='<div class="rhead" style="margin-top:8px;">Сводка по рекламодателям</div><table><tr><th>Рекламодатель</th><th>Показов</th><th>Дней</th><th>Экранов</th><th>Первый показ</th><th>Последний показ</th></tr>';
      summ.forEach(s=>{ sh+=`<tr><td>${esc(s.advertiser)}</td><td>${s.plays}</td><td>${s.days}</td><td>${s.screens}</td><td class="muted" style="font-size:12px;">${esc(s.first_play||'—')}</td><td class="muted" style="font-size:12px;">${esc(s.last_play||'—')}</td></tr>`; });
      sh+='</table>';
    }
    document.getElementById('pl-summary').innerHTML=sh;
    // сырой журнал
    const rows=await api('/playlog?'+q+'&limit=500');
    let h='<div class="rhead">Журнал показов (последние записи)</div>';
    if(!rows.length){ h+='<div class="muted" style="font-size:12px;">Нет показов за выбранный период. Если экраны работают, но журнал пуст — проверьте, что агент отправляет логи показов.</div>'; }
    else {
      h+=`<div class="muted" style="font-size:11px;margin-bottom:6px;">Показано записей: ${rows.length}${rows.length>=500?' (показаны последние 500 — уточните фильтр или выгрузите файл)':''}</div>`;
      h+='<table><tr><th>Дата</th><th>Время</th><th>Экран</th><th>Ролик</th><th>Рекламодатель</th></tr>';
      rows.forEach(r=>{ h+=`<tr><td class="muted">${esc(r.day)}</td><td class="muted">${esc(r.time)}</td><td>${esc(r.screen||'—')}</td><td>${esc(r.media)}</td><td class="muted">${esc(r.advertiser||'—')}</td></tr>`; });
      h+='</table>';
    }
    document.getElementById('pl-result').innerHTML=h;
  }catch(e){ document.getElementById('pl-result').innerHTML='<div class="muted">Ошибка: '+esc(e.message)+'</div>'; }
}
async function exportPlaylog(fmt){
  const q=playlogParams();
  toast('Готовим '+(fmt==='xlsx'?'Excel':'PDF')+'…');
  try{
    const res=await fetch(API+'/playlog/export.'+fmt+'?'+q,{headers:{'Authorization':'Bearer '+TOKEN}});
    if(!res.ok){ const t=await res.text(); toast('Ошибка: '+t.slice(0,80)); return; }
    const blob=await res.blob(); const url=URL.createObjectURL(blob);
    const a=document.createElement('a'); a.href=url; a.download='playlog.'+(fmt==='xlsx'?'xlsx':'pdf');
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url),4000);
  }catch(e){ toast('Ошибка: '+e.message); }
}

//=============================================================================
// КОНСТРУКТОР ОТЧЁТОВ (из готовых блоков)
//=============================================================================
let BUILDER_OPTS=null;
let BUILDER_LAST=null;   // последний конфиг для экспорта
async function openBuilder(){
  if(!BUILDER_OPTS){ try{ BUILDER_OPTS=await api('/reports/builder/options'); }catch(e){ toast('Ошибка: '+e.message); return; } }
  const o=BUILDER_OPTS;
  const view=document.getElementById('view');
  document.getElementById('topright').innerHTML=`<button class="btn" data-action="reports-back-to-list">← К отчётам</button>`;

  document.getElementById('vtitle').textContent='Конструктор отчётов';
  view.innerHTML=`
    <div class="muted" style="font-size:12px;margin-bottom:12px;">Соберите отчёт из готовых блоков: показатель, разрез, период, фильтр, сортировка. Можно построить таблицу и график, сохранить и выгрузить.</div>
    <div class="row" style="flex-wrap:wrap;">
      <div class="fld" style="min-width:180px;"><label>Показатель</label><select class="inp" id="b-metric">${o.metrics.map(m=>`<option value="${m.key}">${esc(m.label)}</option>`).join('')}</select></div>
      <div class="fld" style="min-width:180px;"><label>Разрез</label><select class="inp" id="b-dim">${o.dimensions.map(d=>`<option value="${d.key}">${esc(d.label)}</option>`).join('')}</select></div>
      <div class="fld" style="min-width:130px;"><label>Период</label><select class="inp" id="b-days">${o.periods.map(p=>`<option value="${p.key}">${esc(p.label)}</option>`).join('')}</select></div>
    </div>
    <div class="row" style="flex-wrap:wrap;align-items:end;">
      <div class="fld" style="min-width:160px;"><label>Фильтр</label><select class="inp" id="b-filter" data-action="reports-builder-filter-change">${o.filters.map(f=>`<option value="${f.key}">${esc(f.label)}</option>`).join('')}</select></div>
      <div class="fld" style="min-width:160px;" id="b-fval-wrap"></div>
      <div class="fld" style="min-width:160px;"><label>Сортировка</label><select class="inp" id="b-sort">${(o.sorts||[]).map(s=>`<option value="${s.key}">${esc(s.label)}</option>`).join('')}</select></div>
      <div class="fld" style="min-width:110px;"><label>Лимит</label><select class="inp" id="b-limit">${(o.limits||[]).map(l=>`<option value="${l.key}">${esc(l.label)}</option>`).join('')}</select></div>
      <div class="fld" style="flex:0;"><button class="btn primary" data-action="reports-builder-run">Построить</button></div>
    </div>
    <div id="b-result"></div>
    <div class="sec">Мои сохранённые отчёты</div>
    <div id="b-saved" class="muted">Загрузка…</div>`;
  builderFilterChange();
  loadSavedReports();
}
function builderFilterChange(){
  const f=document.getElementById('b-filter').value;
  const wrap=document.getElementById('b-fval-wrap');
  const o=BUILDER_OPTS;
  if(f==='advertiser'){ wrap.innerHTML=`<label>Значение</label><select class="inp" id="b-fval">${o.advertisers.map(a=>`<option>${esc(a)}</option>`).join('')||'<option value="">нет данных</option>'}</select>`; }
  else if(f==='screen'){ wrap.innerHTML=`<label>Значение</label><select class="inp" id="b-fval">${o.screens.map(s=>`<option>${esc(s)}</option>`).join('')||'<option value="">нет данных</option>'}</select>`; }
  else if(f==='city'){ const cities=[...new Set(o.screens)]; wrap.innerHTML=`<label>Значение (город)</label><input class="inp" id="b-fval" placeholder="например, Москва">`; }
  else { wrap.innerHTML=''; }
}
function builderConfig(){
  const sort=(document.getElementById('b-sort')||{}).value||'val_desc';
  const [sb,sd]=sort.split('_');
  const c={metric:document.getElementById('b-metric').value, dimension:document.getElementById('b-dim').value,
    days:document.getElementById('b-days').value, filter:document.getElementById('b-filter').value,
    sort_by:sb, sort_dir:sd, limit:(document.getElementById('b-limit')||{}).value||'0'};
  const fv=document.getElementById('b-fval'); if(fv) c.filter_value=fv.value;
  return c;
}
async function builderRun(){
  const cfg=builderConfig(); BUILDER_LAST=cfg;
  try{
    const res=await api('/reports/builder/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
    let h=`<div class="rhead" style="margin-top:16px;">${esc(res.title)}</div>`;
    if(!res.rows.length){ h+='<div class="muted" style="font-size:12px;">Нет данных за выбранный период.</div>'; document.getElementById('b-result').innerHTML=h; return; }
    // график (CSS-столбцы)
    const maxv=Math.max(...res.rows.map(r=>Number(r[1])||0))||1;
    h+='<div style="margin:6px 0 16px;">';
    res.rows.slice(0,20).forEach(r=>{ const pct=Math.round((Number(r[1])||0)/maxv*100);
      h+=`<div style="display:flex;align-items:center;gap:10px;margin-bottom:5px;">
        <span style="width:160px;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(r[0]==null?'—':r[0])}</span>
        <span style="flex:1;height:14px;background:var(--panel);border-radius:7px;overflow:hidden;"><span style="display:block;height:100%;width:${pct}%;background:var(--accent);"></span></span>
        <span class="muted" style="font-size:11px;width:64px;text-align:right;">${esc(r[1])}</span></div>`; });
    h+='</div>';
    // таблица
    h+='<table><tr>'+res.columns.map(c=>`<th>${esc(c)}</th>`).join('')+'</tr>';
    res.rows.forEach(r=>{ h+='<tr>'+r.map(c=>`<td>${esc(c==null?'—':c)}</td>`).join('')+'</tr>'; });
    h+='</table>';
    // действия: сохранить + экспорт
    h+=`<div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
      <input class="inp" id="b-name" placeholder="Название отчёта" style="max-width:240px;">
      <button class="btn primary" data-action="reports-builder-save">Сохранить отчёт</button>
<button class="btn" data-action="reports-builder-export-xlsx">Excel</button>
<button class="btn" data-action="reports-builder-export-pdf">PDF</button>`;
    document.getElementById('b-result').innerHTML=h;
  }catch(e){ toast('Ошибка: '+e.message); }
}
async function builderExport(fmt){
  if(!BUILDER_LAST){ toast('Сначала постройте отчёт'); return; }
  toast('Готовим '+(fmt==='xlsx'?'Excel':'PDF')+'…');
  try{
    const res=await fetch(API+'/reports/builder/export.'+fmt,{method:'POST',headers:{'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json'},body:JSON.stringify(BUILDER_LAST)});
    if(!res.ok){ const t=await res.text(); toast('Ошибка: '+t.slice(0,80)); return; }
    const blob=await res.blob(); const url=URL.createObjectURL(blob);
    const a=document.createElement('a'); a.href=url; a.download='ds_custom_report.'+(fmt==='xlsx'?'xlsx':'pdf');
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url),4000);
  }catch(e){ toast('Ошибка: '+e.message); }
}
async function builderSave(){
  const name=val('b-name'); if(!name){ toast('Введите название отчёта'); return; }
  try{ await api('/reports/builder/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,config:builderConfig()})});
    toast('Отчёт сохранён'); loadSavedReports();
  }catch(e){ toast('Ошибка: '+e.message); }
}
async function loadSavedReports(){
  try{
    const saved=await api('/reports/builder/saved');
    const el=document.getElementById('b-saved');
    if(!saved.length){ el.innerHTML='<div class="muted" style="font-size:12px;">Пока нет сохранённых отчётов.</div>'; return; }
    el.innerHTML=saved.map(s=>`<div class="cell" style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">
      <span style="flex:1;font-weight:500;">${esc(s.name)}</span>
      <button class="btn"
  data-action="reports-apply-saved"
  data-saved-config='${JSON.stringify(s.config)}'>Открыть</button>
      <button class="btn danger" data-action="reports-delete-saved" data-saved-id="${s.id}">Удалить</button></div>`).join('');
  }catch(e){ document.getElementById('b-saved').innerHTML='<div class="muted">Ошибка загрузки</div>'; }
}
function applySaved(cfg){
  document.getElementById('b-metric').value=cfg.metric||'plays';
  document.getElementById('b-dim').value=cfg.dimension||'media';
  document.getElementById('b-days').value=String(cfg.days||7);
  document.getElementById('b-filter').value=cfg.filter||'none';
  builderFilterChange();
  const fv=document.getElementById('b-fval'); if(fv&&cfg.filter_value) fv.value=cfg.filter_value;
  const sortSel=document.getElementById('b-sort'); if(sortSel) sortSel.value=(cfg.sort_by||'val')+'_'+(cfg.sort_dir||'desc');
  const limSel=document.getElementById('b-limit'); if(limSel) limSel.value=String(cfg.limit||0);
  builderRun();
}
async function delSaved(id){
  if(!confirm('Удалить сохранённый отчёт?')) return;
  try{ await api('/reports/builder/saved/'+id,{method:'DELETE'}); toast('Удалено'); loadSavedReports(); }
  catch(e){ toast('Ошибка: '+e.message); }
}
initReportsViewActions();

//=============================================================================
// НАСТРОЙКИ
//=============================================================================

//=============================================================================
// OTA-ОБНОВЛЕНИЕ АГЕНТОВ  (v15)
//=============================================================================
let OTA_FILES_CACHE = [];
