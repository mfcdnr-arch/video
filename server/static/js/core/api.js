//=============================================================================
// Digital Signage — Админ-панель (фронтенд). Работает с API под /api/.
//=============================================================================
const API = location.origin + '/api';
let TOKEN = localStorage.getItem('ds_token') || null;   // допустимо: это наш токен сессии в браузере панели
let ME = {username:null, role:null};   // текущий пользователь и его роль
function canWrite(){ return ME.role==='admin' || ME.role==='superadmin'; }
function isSuper(){ return ME.role==='superadmin'; }
const ADV_COLORS = {};   // id -> color, для меток

// ---- HTTP-помощник ----
async function api(path, opts={}){
  const headers = opts.headers || {};
  if(TOKEN) headers['Authorization'] = 'Bearer ' + TOKEN;
  // JSON-тело (строка) без явного Content-Type → проставляем application/json,
  // иначе браузер шлёт text/plain и FastAPI не разбирает тело как объект
  // (ошибка dict_type / «Input should be a valid dictionary»). Для FormData
  // (загрузка файлов) body — объект, не строка, поэтому Content-Type НЕ трогаем
  // (его должен выставить браузер с boundary).
  if(typeof opts.body === 'string' && !headers['Content-Type'] && !headers['content-type']){
    headers['Content-Type'] = 'application/json';
  }
  const res = await fetch(API + path, {...opts, headers});
  if(res.status === 401){ TOKEN=null; localStorage.removeItem('ds_token'); renderLogin(); throw new Error('unauthorized'); }
  if(!res.ok){ const t = await res.text(); throw new Error(t || ('HTTP '+res.status)); }
  const ct = res.headers.get('content-type')||'';
  return ct.includes('json') ? res.json() : res.text();
}
function toast(msg){ const t=document.getElementById('toast'); t.textContent=msg; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),2200); }
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
