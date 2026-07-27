(function() {
  'use strict';

  let ws = null;
  let wsReconnectTimer = null;
  let wsToken = null;

  // ── Получить токен из localStorage ──────────────────────────────────────
  function getStoredToken() {
    return localStorage.getItem('ds_token')
      || localStorage.getItem('ds_jwt_token')
      || sessionStorage.getItem('ds_token')
      || null;
  }

  // ── Индикатор состояния WS ───────────────────────────────────────────────
  function setWsIndicator(state) {
    const el = document.getElementById('ws-status-indicator');
    if (!el) return;
    el.className = 'ws-indicator ws-' + state;
    el.title = {
      'connected': 'Real-time: подключён',
      'disconnected': 'Real-time: нет соединения',
      'connecting': 'Real-time: подключение...',
    }[state] || state;
  }

  // ── Обновить счётчики дашборда ───────────────────────────────────────────
  function updateSummary(summary) {
    if (!summary) return;
    const fields = {
      'dash-total-screens':  summary.total,
      'dash-online-screens': summary.online,
      'dash-offline-screens': summary.offline,
    };
    for (const [id, val] of Object.entries(fields)) {
      const el = document.getElementById(id);
      if (el && val !== undefined) el.textContent = val;
    }
  }

  // ── Обновить таблицу/карточки экранов ───────────────────────────────────
  function updateScreens(screens) {
    // Ищем строки таблицы экранов по data-screen-id
    for (const sc of screens) {
      const row = document.querySelector(`[data-screen-id="${sc.id}"]`);
      if (!row) continue;

      // Статус
      const statusEl = row.querySelector('.screen-status');
      if (statusEl) {
        statusEl.textContent = sc.status === 'online' ? 'Online' : 'Offline';
        statusEl.className = 'screen-status badge ' +
          (sc.status === 'online' ? 'bg-success' : 'bg-secondary');
      }

      // Файл воспроизведения
      const playEl = row.querySelector('.screen-playing');
      if (playEl) playEl.textContent = sc.playing_file || '—';

      // Диск
      const diskEl = row.querySelector('.screen-disk');
      if (diskEl && sc.disk_free_gb !== undefined) {
        const total = sc.disk_total_gb || 0;
        const free  = sc.disk_free_gb  || 0;
        diskEl.textContent = total > 0
          ? `${free.toFixed(1)} / ${total.toFixed(1)} ГБ`
          : `${free.toFixed(1)} ГБ`;
      }

      // Последний heartbeat
      const seenEl = row.querySelector('.screen-last-seen');
      if (seenEl && sc.last_seen) {
        const d = parseServerTS(sc.last_seen);
        seenEl.textContent = d.toLocaleTimeString('ru-RU');
      }
    }
  }

  // ── Подключить WS ────────────────────────────────────────────────────────
  function connectDashboardWS() {
    wsToken = getStoredToken();
    if (!wsToken) {
      // Токена нет — подождём логина
      setTimeout(connectDashboardWS, 3000);
      return;
    }

    clearTimeout(wsReconnectTimer);
    setWsIndicator('connecting');

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${location.host}/ws/dashboard?token=${encodeURIComponent(wsToken)}`;

    try {
      ws = new WebSocket(wsUrl);
    } catch (e) {
      setWsIndicator('disconnected');
      wsReconnectTimer = setTimeout(connectDashboardWS, 5000);
      return;
    }

    ws.onopen = () => {
      setWsIndicator('connected');
      console.log('[WS Dashboard] Подключён');
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === 'screens_update') {
          updateSummary(msg.summary);
          updateScreens(msg.screens);
          // Обновляем кэш метрик сервера из WS-пуша
          if (msg.server_metrics && !msg.server_metrics.error) {
            _SRV_METRICS_CACHE = msg.server_metrics;
            updateServerMetricsWidget(msg.server_metrics);
          }
        } else if (msg.type === 'screen_heartbeat') {
          // Быстрое обновление одного экрана
          const row = document.querySelector(`[data-screen-id="${msg.screen_id}"]`);
          if (row) {
            const statusEl = row.querySelector('.screen-status');
            if (statusEl) {
              statusEl.textContent = 'Online';
              statusEl.className = 'screen-status badge bg-success';
            }
          }
        }
      } catch (e) {
        console.warn('[WS Dashboard] Ошибка разбора:', e);
      }
    };

    ws.onclose = (e) => {
      setWsIndicator('disconnected');
      console.log('[WS Dashboard] Отключён, переподключение через 5с...');
      wsReconnectTimer = setTimeout(connectDashboardWS, 5000);
    };

    ws.onerror = () => {
      setWsIndicator('disconnected');
    };
  }

  // ── Добавить индикатор в DOM ─────────────────────────────────────────────
  function injectWSIndicator() {
    if (document.getElementById('ws-status-indicator')) return;
    const style = document.createElement('style');
    style.textContent = `
      .ws-indicator {
        display: inline-block;
        width: 10px; height: 10px;
        border-radius: 50%;
        margin-left: 8px;
        vertical-align: middle;
        cursor: help;
        transition: background 0.3s;
      }
      .ws-connected    { background: #28a745; box-shadow: 0 0 4px #28a745; }
      .ws-disconnected { background: #6c757d; }
      .ws-connecting   { background: #ffc107; animation: ws-pulse 1s infinite; }
      @keyframes ws-pulse {
        0%, 100% { opacity: 1; } 50% { opacity: 0.3; }
      }
    `;
    document.head.appendChild(style);

    // Вставить индикатор рядом с заголовком или навбаром
    const target = document.querySelector('h1, .navbar-brand, nav h1, .page-title');
    if (target) {
      const dot = document.createElement('span');
      dot.id = 'ws-status-indicator';
      dot.className = 'ws-indicator ws-disconnected';
      dot.title = 'Real-time: нет соединения';
      target.appendChild(dot);
    }
  }

  // ── Старт ────────────────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      injectWSIndicator();
      connectDashboardWS();
    });
  } else {
    injectWSIndicator();
    connectDashboardWS();
  }

  // Переподключиться если токен появился (после логина)
  window.addEventListener('ds-login', () => {
    if (ws) ws.close();
    connectDashboardWS();
  });

})();
