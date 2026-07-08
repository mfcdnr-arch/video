async function viewBroadcast(){

  const view=document.getElementById('view');
  try{
    const [bc, playlists] = await Promise.all([api('/broadcast'), api('/playlists')]);
    let h='';
    if(bc.is_on){
      h+=`<div class="banner on" style="margin-bottom:8px;"><span class="dot" style="background:var(--accent);"></span>
        <div><div style="font-size:11px;color:var(--accent);text-transform:uppercase;letter-spacing:.4px;">Общий эфир включён</div>
        <div style="font-weight:600;">${esc(bc.playlist_name||'')}</div>
        <div class="muted" style="font-size:11px;">на всех экранах · индивидуальные расписания временно перекрыты</div></div>
        <button class="btn danger" style="margin-left:auto;" data-action="broadcast-off">Снять общее и вернуть индивидуальное</button>
</div>`;
    } else {
      h+=`<div class="banner off" style="margin-bottom:8px;"><span class="dot" style="background:var(--dim);"></span>
        <div><div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;">Общий эфир выключен</div>
        <div style="font-weight:600;">Каждый экран показывает своё расписание</div></div></div>`;
    }
    h+='<div class="sec">'+(bc.is_on?'Сменить общий плейлист':'Выбрать плейлист для всех экранов')+'</div>';
    if(!playlists.length){ h+='<div class="empty">Плейлистов пока нет. Создайте плейлист, чтобы транслировать его на всю сеть.</div>'; }
    playlists.forEach(p=>{
      const sel = bc.is_on && bc.playlist_id===p.id;
      h+=`<div class="cell" style="display:flex;align-items:center;gap:10px;margin-bottom:7px;cursor:pointer;${sel?'border-color:var(--accent2);background:#15241f;':''}" data-action="broadcast-on" data-playlist-id="${p.id}">

        <span style="font-size:16px;color:${sel?'var(--accent)':'var(--dim)'};">▤</span>
        <div style="flex:1;"><div style="font-weight:500;">${esc(p.name)}${sel?' <span style="font-size:10px;color:var(--accent);border:0.5px solid var(--accent2);border-radius:5px;padding:0 6px;">в эфире</span>':''}</div></div>
        <span>${sel?'●':'○'}</span></div>`;
    });
    view.innerHTML=h;
  }catch(e){ view.innerHTML='<div class="empty">Ошибка: '+esc(e.message)+'</div>'; }
}
function initBroadcastViewActions(){
  if(window.__broadcastViewActionsInitialized) return;
  window.__broadcastViewActionsInitialized = true;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('broadcast-')) return;

    switch(action){
      case 'broadcast-off':
        return Signage.broadcastOff();

      case 'broadcast-on': {
        const playlistId = Number(el.dataset.playlistId);
        return Signage.broadcastOn(playlistId);
      }
    }
  });
}

async function broadcastOn(pid){ try{ await api('/broadcast/on?playlist_id='+pid,{method:'POST'}); toast('Общий эфир включён'); viewBroadcast(); }catch(e){ toast('Ошибка: '+e.message); } }
async function broadcastOff(){ try{ await api('/broadcast/off',{method:'POST'}); toast('Возврат к индивидуальным расписаниям'); viewBroadcast(); }catch(e){ toast('Ошибка: '+e.message); } }
window.Signage = window.Signage || {};
window.Signage.viewBroadcast = viewBroadcast;
window.Signage.broadcastOn = broadcastOn;
window.Signage.broadcastOff = broadcastOff;
initBroadcastViewActions();

