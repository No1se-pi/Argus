'use strict';
const secret = location.hash.slice(1);
history.replaceState(null, '', location.pathname);
const el = id => document.getElementById(id);
const labels = {
  waiting: 'Войдите в ВК в окне ниже. Если уже вошли, нажмите «Открыть сообщество».',
  checking: 'Проверяем доступ к постам, лайкам и профилям…',
  rejected: 'Текущий веб-токен не прошёл проверку лайков. Завершите вход и откройте сообщество.',
  connected: 'Подключение сохранено. Бот готов к сбору.',
  unavailable: 'Не удалось запустить браузер на сервере. Проверьте установку Chromium и получите новую ссылку.',
  cancelled: 'Подключение закрыто. Получить новую ссылку можно в боте.'
};
let running = false, busy = false, previousImage;
let actionQueue = Promise.resolve(), queuedActions = 0;
async function command(action, fields = {}) {
  const response = await fetch('/vk-connect/action', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-Argus-Auth': secret},
    body: JSON.stringify({action, ...fields})
  });
  if (!response.ok) throw new Error('Ссылка истекла или действие недоступно. Получите новую ссылку в боте.');
  const data = await response.json();
  el('status').textContent = labels[data.status] || 'Подключение…';
  if (data.status === 'waiting' && data.view === 'loading') {
    el('status').textContent = 'Загружается страница ВК. Дождитесь появления формы входа.';
  } else if (data.status === 'waiting' && data.view === 'stalled') {
    el('status').textContent = 'ВК завис на загрузке. Нажмите «Перезагрузить ВК» или «Открыть форму входа».';
  } else if (data.status === 'waiting' && data.view === 'challenge') {
    el('status').textContent = 'ВК просит проверку безопасности. Пройдите её в окне ниже.';
  }
  el('host').textContent = data.host || '—';
  if (data.status === 'connected') {
    running = false; el('remote').hidden = true; el('success').hidden = false;
  } else if (['cancelled', 'unavailable'].includes(data.status)) {
    running = false; el('remote').hidden = true;
  }
  return data;
}
function act(action, fields = {}) {
  queuedActions++;
  const execute = async () => {
    while (busy) await new Promise(resolve => setTimeout(resolve, 25));
    busy = true;
    try { return await command(action, fields); }
    catch (error) { el('status').textContent = error.message; running = false; }
    finally { busy = false; queuedActions--; }
  };
  const result = actionQueue.then(execute);
  actionQueue = result.catch(() => {});
  return result;
}
async function update() {
  if (!running) return;
  if (!busy && queuedActions === 0) {
    busy = true;
    try {
      await command('status');
      if (running) {
        const response = await fetch('/vk-connect/frame', {
          credentials: 'same-origin', headers: {'X-Argus-Auth': secret}
        });
        if (response.ok) {
          const image = URL.createObjectURL(await response.blob());
          el('screen').src = image;
          if (previousImage) URL.revokeObjectURL(previousImage);
          previousImage = image;
          el('frame-state').textContent = 'Изображение обновлено: ' + new Date().toLocaleTimeString('ru-RU');
        }
      }
    } catch (error) { el('status').textContent = error.message; running = false; }
    finally { busy = false; }
  }
  if (running) setTimeout(update, 1400);
}
el('open').addEventListener('click', async () => {
  el('open').disabled = true;
  const data = await act('open');
  if (data && ['waiting', 'checking', 'rejected'].includes(data.status)) {
    running = true; el('open').hidden = true; el('remote').hidden = false; update();
  }
});
el('screen').addEventListener('click', event => {
  const bounds = el('screen').getBoundingClientRect();
  act('click', {x: (event.clientX - bounds.left) * 1000 / bounds.width,
                y: (event.clientY - bounds.top) * 760 / bounds.height});
});
el('typing').addEventListener('submit', async event => {
  event.preventDefault();
  const text = el('text').value; el('text').value = '';
  if (text) await act('type', {text});
});
for (const [id, key] of [['enter', 'Enter'], ['tab', 'Tab'], ['backspace', 'Backspace']]) {
  el(id).addEventListener('click', () => act('key', {key}));
}
el('up').addEventListener('click', () => act('scroll', {down: false}));
el('down').addEventListener('click', () => act('scroll', {down: true}));
el('community').addEventListener('click', () => act('community'));
el('reload').addEventListener('click', () => act('reload'));
el('login').addEventListener('click', () => act('login'));
el('cancel').addEventListener('click', () => act('cancel'));
if (!secret) { el('open').disabled = true; el('status').textContent = 'Откройте персональную ссылку из бота.'; }
