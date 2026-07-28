async function viewDash(){
  const view=document.getElementById('view');
  try{
    const d = await api('/dashboard');
    let h='<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));">';
    h+=kpi('Всего экранов', d.screens_total, '');
    h+=kpi('Онлайн', d.screens_online, Math.round((d.screens_online/(d.screens_total||1))*100)+'% сети', 'var(--accent)');
    h+=kpi('Не работают', d.screens_offline, d.screens_offline? 'требуют внимания':'', 'var(--danger)');
    h+=kpi('Рекламодателей', d.advertisers, d.media_count+' роликов');
    const expN = (d.expiring_media||[]).length;
    h+=kpi('Истекают ≤3 дней', expN, expN?'продлите или замените':'', expN?'#ffd34d':undefined);
    const noDisp = d.no_display||[];
    if(noDisp.length) h+=kpi('Без монитора', noDisp.length, 'играют «в стену»', 'var(--danger)');
    h+='</div>';
    // мини-ПК работает и играет, но монитор не подключён к видеовыходу
    if(noDisp.length){
      h+='<div class="sec">Монитор не подключён</div>';
      noDisp.forEach(s=>{
        h+=`<div class="cell" style="display:flex;align-items:center;gap:10px;margin-bottom:6px;border-color:#7a3535;">
          <span style="color:var(--danger);">📺</span>
          <span style="flex:1;font-size:13px;">${esc(s.name)}</span>
          <span class="muted" style="font-size:11px;">${esc(s.display_outputs||'')}</span>
          <span style="font-size:12px;color:var(--danger);">${s.display_changed_at?'с '+esc(fmtServerTS(s.display_changed_at)):'кабель/питание монитора'}</span>
        </div>`;
      });
    }
    // ролики с истекающим сроком действия
    if(expN){
      h+='<div class="sec">Срок действия истекает</div>';
      d.expiring_media.forEach(m=>{
        const when = m.valid_until ? new Date(m.valid_until).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '';
        h+=`<div class="cell" style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">
          <span style="color:#ffd34d;">⏳</span>
          <span style="flex:1;font-size:13px;">${esc(m.title)}</span>
          <span class="muted" style="font-size:11px;">${esc(m.advertiser||'без рекламодателя')}</span>
          <span style="font-size:12px;color:#ffd34d;">до ${when}</span>
        </div>`;
      });
    }
    // эфир
    h+='<div class="sec">Эфир всей сети</div>';
    if(d.broadcast_on){
      h+=`<div class="banner on"><span class="dot" style="background:var(--accent);"></span>
        <div><div style="font-size:11px;color:var(--accent);text-transform:uppercase;letter-spacing:.4px;">Общий эфир включён</div>
        <div style="font-weight:600;">${esc(d.broadcast_playlist||'')}</div></div>
        <button class="btn" style="margin-left:auto;" data-action="dashboard-open-broadcast">Управлять</button></div>`;
    } else {
      h+=`<div class="banner off"><span class="dot" style="background:var(--dim);"></span>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;">Общий эфир выключен</div>
        <div style="font-weight:600;">Каждый экран показывает своё</div></div>
        <button class="btn" style="margin-left:auto;" data-action="dashboard-open-broadcast">Управлять</button></div>`;
    }
    // что сейчас играет на каждом экране
    if(d.now_playing && d.now_playing.length){
      h+='<div class="sec">Сейчас в эфире</div><div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(230px,1fr));">';
      d.now_playing.forEach(s=>{
        const on=s.status==='online';
        const playing = on && s.playing_file;
        const label = s.media_title || s.playing_file;
        h+=`<div class="cell">
          <div style="display:flex;align-items:center;gap:7px;margin-bottom:5px;"><span class="dot" style="background:${on?'var(--accent)':'var(--danger)'};"></span><span style="font-weight:500;flex:1;">${esc(s.name)}</span></div>
          ${playing
            ? `<div style="font-size:13px;color:var(--txt);">▶ ${esc(label)}</div>${s.advertiser?`<div style="font-size:11px;color:var(--muted);margin-top:2px;">${esc(s.advertiser)}</div>`:''}`
            : (on ? '<div style="font-size:12px;color:var(--muted);">▷ ничего не воспроизводится</div>'
                  : '<div style="font-size:12px;color:var(--danger);">офлайн — нет данных</div>')}
        </div>`;
      });
      h+='</div>';
    }
    // диск — с прогресс-барами
    h+='<div class="sec">Диски мини ПК</div><div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(185px,1fr));">';
    (d.disks || []).forEach(s=>{
      const off=s.status!=='online';
      const free=s.disk_free_gb!=null?Number(s.disk_free_gb):null;
      const total=s.disk_total_gb!=null?Number(s.disk_total_gb):null;
      const used=(free!=null&&total!=null)?total-free:null;
      const pct=(used!=null&&total&&total>0)?Math.round(used/total*100):null;
      const low=!off&&free!=null&&free<15;
      const barCol=off?'var(--danger)':(pct!=null&&pct>90?'#c74b00':(pct!=null&&pct>70?'#c48b00':'var(--accent)'));
      const dotCol=off?'var(--danger)':(low?'var(--danger)':'var(--accent)');
      h+=`<div class="cell" style="${low?'border-color:#7a3535;':''}">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:5px;">
          <span class="dot" style="background:${dotCol};"></span>
          <span style="font-size:12px;font-weight:500;flex:1;">${esc(s.name)}</span>
          ${!off&&pct!=null?`<span style="font-size:11px;color:${barCol};font-weight:600;">${pct}%</span>`:''}
        </div>
        ${off
          ? `<div style="font-size:11px;color:var(--danger);">офлайн</div>`
          : pct!=null
            ? `<div style="height:5px;background:var(--border2);border-radius:3px;overflow:hidden;margin-bottom:4px;">
                 <div style="width:${pct}%;height:100%;background:${barCol};border-radius:3px;"></div></div>
               <div style="font-size:10px;color:var(--muted);">исп.${used.toFixed(0)} / ${total.toFixed(0)} ГБ · св.${free.toFixed(0)} ГБ${low?' · <b style=color:var(--danger)>мало!</b>':''}</div>`
            : `<div style="font-size:11px;color:var(--accent);">своб.${free!=null?free.toFixed(0):'?'} ГБ</div>`
        }
      </div>`;
    });
    h+='</div>';
    // ─── Метрики сервера ─────────────────────────────────────────────────
    await renderServerMetrics(h).then(block=>{ h=block; });
    // лента
    h+='<div class="sec">Последние операции</div>';
    if(!(d.feed || []).length) h+='<div class="empty">Пока нет событий</div>';
(d.feed || []).forEach(f=>{ h+=feedRow(f); });
    // нерабочие ролики
    if(d.broken_media && d.broken_media.length){
      h+='<div class="sec">Нерабочие ролики ('+d.broken_media.length+')</div>';
      h+='<table><tr><th>Ролик</th><th>Рекламодатель</th><th>Ошибок</th></tr>';
      d.broken_media.forEach(b=>{ h+=`<tr><td>${esc(b.title||b.filename)}</td><td class="muted">${esc(b.advertiser||'—')}</td><td>${b.error_count||1}</td></tr>`; });
      h+='</table>';
    }
    view.innerHTML=h;
  }catch(e){ view.innerHTML='<div class="empty">Не удалось загрузить дашборд: '+esc(e.message)+'</div>'; }
}
const FEED_IC={upload:['◫','#5ba3ec'],sync:['⇄','#7fe3c4'],stop:['■','#ff8a7a'],register:['▣','#7fe3c4'],schedule:['▦','#f5b73d'],backup:['⤓','#aeb4be'],broadcast:['⇄','#12a886'],ticker:['▦','#ffd34d'],advertiser:['◧','#5ba3ec']};
function feedRow(f){ const m=FEED_IC[f.event_type]||['•','#aeb4be']; const ago=timeAgo(f.created_at);
  return `<div class="feed"><span class="fi" style="background:${hexA(m[1],.16)};color:${m[1]};">${m[0]}</span>
    <span><span class="ft" style="display:block;">${esc(f.title)}</span><span class="fm">${esc(f.detail||'')}</span></span>
    <span class="fz">${ago}</span></div>`; }
function timeAgo(ts){ if(!ts) return ''; const d=parseServerTS(ts); const s=(Date.now()-d.getTime())/1000;
  if(s<60) return 'только что'; if(s<3600) return Math.floor(s/60)+' мин назад'; if(s<86400) return Math.floor(s/3600)+' ч назад'; return Math.floor(s/86400)+' дн назад'; }

// ─── Метрики сервера ───────────────────────────────────────────────────────────────
let _SRV_METRICS_CACHE = null;   // кэш для реалтайм WS-пуша

/** Получить метрики сервера через API или WS-кэш */
async function fetchServerMetrics(){
  try{ return await api('/server/metrics'); }
  catch(e){ return _SRV_METRICS_CACHE || null; }
}

/** Прогресс-бар (0–100%) */
function pbar(pct, col){
  const safe = Math.min(100, Math.max(0, pct||0));
  const c = pct >= 90 ? '#e05252' : pct >= 70 ? '#f5b73d' : (col||'var(--accent)');
  return `<div style="background:var(--bg2);border-radius:4px;height:8px;margin-top:4px;overflow:hidden;">
    <div style="width:${safe}%;background:${c};height:100%;border-radius:4px;transition:width .4s;"></div></div>`;
}

/** Рендер блока метрик сервера и возвращает обновлённый html */
async function renderServerMetrics(h){
  const m = await fetchServerMetrics();
  if(!m || m.error){
    h += '<div class="sec">Метрики сервера</div><div class="empty">Недоступно (проверьте, что psutil установлен на сервере)</div>';
    return h;
  }
  _SRV_METRICS_CACHE = m;

  // Заголовок + аптайм
  const uph = m.uptime_hours|0, upm = Math.round((m.uptime_hours - uph)*60);
  h += `<div class="sec">Метрики сервера <span id="srv-uptime" style="font-size:11px;font-weight:400;color:var(--muted);margin-left:8px;">аптайм ${uph}ч ${upm}мин</span></div>`;
  h += '<div id="srv-metrics-widget">';

  // CPU + RAM + Сеть
  h += '<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr));margin-bottom:12px;">';

  // CPU
  const cpuCol = m.cpu_pct >= 90 ? 'var(--danger)' : m.cpu_pct >= 70 ? '#f5b73d' : 'var(--accent)';
  h += `<div class="cell">
    <div style="display:flex;justify-content:space-between;align-items:baseline;">
      <span style="font-size:12px;color:var(--muted);">CPU</span>
      <span id="srv-cpu-val" style="font-size:20px;font-weight:700;color:${cpuCol};">${Number(m.cpu_pct || 0).toFixed(1)}%</span>
    </div>
    <div style="background:var(--bg2);border-radius:4px;height:8px;margin-top:4px;overflow:hidden;">
      <div id="srv-cpu-bar" style="width:${Math.min(100,m.cpu_pct|0)}%;background:${cpuCol};height:100%;border-radius:4px;transition:width .4s;"></div></div>
  </div>`;

  // RAM
  const ramCol = m.ram_pct >= 90 ? 'var(--danger)' : m.ram_pct >= 75 ? '#f5b73d' : 'var(--accent)';
  h += `<div class="cell">
    <div style="display:flex;justify-content:space-between;align-items:baseline;">
      <span style="font-size:12px;color:var(--muted);">RAM</span>
      <span id="srv-ram-val" style="font-size:20px;font-weight:700;color:${ramCol};">${Number(m.ram_pct || 0).toFixed(1)}%</span>
    </div>
    <div style="background:var(--bg2);border-radius:4px;height:8px;margin-top:4px;overflow:hidden;">
      <div id="srv-ram-bar" style="width:${Math.min(100,m.ram_pct|0)}%;background:${ramCol};height:100%;border-radius:4px;transition:width .4s;"></div></div>
    <div id="srv-ram-sub" style="font-size:11px;color:var(--muted);margin-top:3px;">${m.ram_used} / ${m.ram_total} ГБ</div>
  </div>`;

  // Сеть
  if(m.net_sent_mb !== undefined || m.net_recv_mb !== undefined){
    h += `<div class="cell">
      <div style="font-size:12px;color:var(--muted);margin-bottom:6px;">Сеть (сервер)</div>
      <div id="srv-net-up" style="font-size:13px;color:var(--txt);">↑ ${(m.net_sent_mb||0).toFixed(2)} МБ/с</div>
      <div id="srv-net-dn" style="font-size:13px;color:var(--txt);">↓ ${(m.net_recv_mb||0).toFixed(2)} МБ/с</div>
    </div>`;
  }

  h += '</div>';

  // Диски сервера
  if(m.disks && m.disks.length){
    h += '<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr));">';
    m.disks.forEach(dk=>{
      const dCol = dk.pct >= 90 ? 'var(--danger)' : dk.pct >= 75 ? '#f5b73d' : 'var(--accent)';
      h += `<div class="cell">
        <div style="display:flex;justify-content:space-between;align-items:baseline;">
          <span style="font-size:11px;color:var(--muted);">${esc(dk.mount)}</span>
          <span style="font-size:16px;font-weight:600;color:${dCol};">${Number(dk.pct || 0).toFixed(1)}%</span>
        </div>
        ${pbar(dk.pct)}
        <div style="font-size:11px;color:var(--muted);margin-top:3px;">${dk.used_gb} / ${dk.total_gb} ГБ &bull; своб. ${dk.free_gb} ГБ</div>
      </div>`;
    });
    h += '</div>';
  }

  h += '</div>'; // /srv-metrics-widget
  return h;
}

/**
 * Живое обновление виджета метрик сервера через WS-пуш (без полной перерисовки)
 */
function updateServerMetricsWidget(m){
  const widget = document.getElementById('srv-metrics-widget');
  if(!widget || !m) return;
  const uph = m.uptime_hours|0, upm = Math.round((m.uptime_hours - uph)*60);
  widget.querySelector('#srv-uptime')?.textContent && (widget.querySelector('#srv-uptime').textContent = `${uph}ч ${upm}мин`);
  // CPU
  const cpuVal = widget.querySelector('#srv-cpu-val');
  const cpuBar = widget.querySelector('#srv-cpu-bar');
  if(cpuVal) cpuVal.textContent = Number(m.cpu_pct || 0).toFixed(1)+'%';
  if(cpuBar){ const safe=Math.min(100,m.cpu_pct|0); cpuBar.style.width=safe+'%';
    cpuBar.style.background = m.cpu_pct>=90?'#e05252':m.cpu_pct>=70?'#f5b73d':'var(--accent)'; }
  // RAM
  const ramVal = widget.querySelector('#srv-ram-val');
  const ramBar = widget.querySelector('#srv-ram-bar');
  const ramSub = widget.querySelector('#srv-ram-sub');
  if(ramVal) ramVal.textContent = Number(m.ram_pct || 0).toFixed(1)+'%';
  if(ramBar){ const safe=Math.min(100,m.ram_pct|0); ramBar.style.width=safe+'%';
    ramBar.style.background = m.ram_pct>=90?'#e05252':m.ram_pct>=75?'#f5b73d':'var(--accent)'; }
  if(ramSub) ramSub.textContent = `${m.ram_used} / ${m.ram_total} ГБ`;
  // Сеть
  const netUp = widget.querySelector('#srv-net-up');
  const netDn = widget.querySelector('#srv-net-dn');
    if(netUp) netUp.textContent = `↑ ${(m.net_sent_mb||0).toFixed(2)} МБ/с`;
  if(netDn) netDn.textContent = `↓ ${(m.net_recv_mb||0).toFixed(2)} МБ/с`;
}
function initDashboardViewActions(){
  if(window.__dashboardViewActionsInitialized) return;
  window.__dashboardViewActionsInitialized = true;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('dashboard-')) return;

    switch(action){
      case 'dashboard-open-broadcast':
        return Signage.nav('broadcast');
    }
  });
}

window.Signage = window.Signage || {};
window.Signage.viewDash = viewDash;
window.Signage.feedRow = feedRow;
window.Signage.timeAgo = timeAgo;
window.Signage.fetchServerMetrics = fetchServerMetrics;
window.Signage.pbar = pbar;
window.Signage.renderServerMetrics = renderServerMetrics;
window.Signage.updateServerMetricsWidget = updateServerMetricsWidget;
initDashboardViewActions();

//=============================================================================
// ЭКРАНЫ
//=============================================================================
