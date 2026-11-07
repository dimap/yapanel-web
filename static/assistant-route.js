/* Pure route description; never inspects credentials or sends requests. */
(function (root) {
  'use strict';
  function host(base) {
    try { const u = new URL(base); return u.hostname + (u.port ? ':' + u.port : ''); }
    catch { return ''; }
  }
  // Protocol support is explicit: an OpenAI-compatible text endpoint alone
  // does not establish Realtime support (e.g. Hermes or CommandCode).
  function capabilities(base, model = '') {
    let openai = false;
    try { const u = new URL(base); openai = u.hostname === 'api.openai.com' && /^\/v1(?:\/realtime)?\/?$/.test(u.pathname) && ['https:', 'wss:'].includes(u.protocol); } catch {}
    const name = model.trim().toLowerCase();
    const native = /^gpt-realtime(?:-(?:mini|\d+(?:\.\d+)*(?:-mini)?)(?:-\d{4}-\d{2}-\d{2})?)?$/.test(name)
      || /^gpt-realtime-\d{4}-\d{2}-\d{2}$/.test(name);
    const legacy = /^gpt-4o(?:-mini)?-realtime-preview(?:-\d{4}-\d{2}-\d{2})?$/.test(name);
    const realtime = openai && (native || legacy);
    const normalized = String(base || '').trim().replace(/\/+$/, '').toLowerCase();
    return {realtime, openai, modelSTT:['https://api.openai.com/v1', 'https://api.groq.com/openai/v1'].includes(normalized),
      voices: realtime ? (legacy ? ['alloy','ash','ballad','coral','echo','sage','shimmer','verse'] : ['marin','cedar','alloy','ash','ballad','coral','echo','sage','shimmer','verse']) : []};
  }
  function conversationModel(base, model) {
    if (!capabilities(base).openai) return true;
    if (capabilities(base,model).realtime) return true;
    return !/(?:realtime|transcrib|whisper|tts|embedding|moderation|dall-e|image|sora|^gpt-live)/i.test(model);
  }
  function signature(c = {}) {
    const l = c.llm || {}, t = c.tts || {}, s = c.stt || {};
    return JSON.stringify([!!c.enabled, c.microphone?.source || 'alice', l.transport || 'chat', l.base_url || '', l.model || '', l.voice || 'marin',
      t.provider || 'alice', t.base_url || '', t.model ?? 'tts-1', t.voice || '', s.provider || 'alice', s.base_url || '', s.model || '']);
  }
  function build(c = {}, runtime = {}, journey = 'current') {
    const l = c.llm || {}, t = c.tts || {}, s = c.stt || {};
    const realtime = l.transport === 'realtime', panel = journey === 'panel';
    const conversation = journey === 'conversation' || !!runtime.state?.mode;
    const direct = !panel && conversation && c.microphone?.source === 'microphone';
    const nodes = [], notes = [];
    const node = (kind, label, title, detail, output, bad = false) => nodes.push({kind, label, title, detail, output, bad});
    if (panel) node('input', 'Вопрос', 'Текст из панели', 'То, что вы напечатали', 'текст');
    else node('input', 'Вопрос', 'Ваш голос', conversation ? 'Разговор с ассистентом' : '«Алиса, спроси GPT…»', 'звук');
    if (!panel && !c.enabled) {
      node('stt', 'Обычный режим', 'Алиса', 'Распознаёт вопрос и отвечает сама', 'голос');
      node('speaker', 'Ответ', 'Колонка', 'Голос Алисы', '');
      notes.push({text:'Помощник выключен. Включите его, чтобы вопросы уходили выбранной модели.'});
      return {nodes, notes};
    }
    if (!panel) {
      if (!direct) node('stt', 'Распознавание', 'Алиса', 'Облако Яндекса', 'текст');
      else if (!realtime) {
        const inherited = s.provider === 'model', base = inherited ? l.base_url : s.base_url;
        const known = {'https://api.openai.com/v1':'gpt-4o-mini-transcribe', 'https://api.groq.com/openai/v1':'whisper-large-v3-turbo'};
        const model = s.model || (inherited ? known[String(base || '').trim().replace(/\/+$/, '').toLowerCase()] : '') || '';
        const bad = !['model', 'custom'].includes(s.provider) || !host(base) || !model;
        node('stt', 'Распознавание', model || 'STT не настроен', host(base) || 'Выберите провайдера или свой STT', 'текст', bad);
        if (bad) notes.push({bad:true, text:'Для прямого захвата речи нужны адрес и модель STT. У провайдера текстовой модели распознавание может отсутствовать.'});
        if (s.provider === 'custom') notes.push({text:'Свой STT должен быть доступен во время разговора. Локальный вариант требует работающего сервера; установка — только по желанию пользователя, рекомендуем Docker.'});
      }
    }
    node('model', realtime ? 'Голосовая модель' : 'Модель', l.model || 'Модель не выбрана',
      [host(l.base_url), realtime ? 'Realtime · голос ' + (l.voice || 'marin') : 'Готовит текст ответа'].filter(Boolean).join(' · '), realtime ? 'голос' : 'текст', !l.model || !host(l.base_url));
    if (!l.model || !host(l.base_url)) notes.push({bad:true,text:'Укажите адрес и модель ассистента.'});
    if (!realtime) {
      const alice = t.provider !== 'openai';
      node('tts', 'Озвучка', alice ? 'Голос Алисы' : t.voice || t.model || 'Синтезатор не настроен',
        alice ? 'Ответ озвучивает Яндекс' : [t.model, host(t.base_url)].filter(Boolean).join(' · '), 'голос', !alice && (!host(t.base_url) || !t.model));
      if (!alice && (!host(t.base_url) || !t.model)) notes.push({bad:true,text:'Укажите адрес и модель выбранного синтезатора.'});
      if (!alice) notes.push({text:'Если выбранная озвучка недоступна, ответ прочитает Алиса.'});
    } else notes.push({text:'Ответ звучит голосом самой Realtime-модели. Отдельный синтезатор используется для служебных сообщений.'});
    node('speaker', 'Ответ', 'Колонка', 'Воспроизводит готовый голос', '');
    if (runtime.running === false) notes.unshift({bad:true,text:'Программа помощника остановлена. Маршрут начнёт работать после запуска.'});
    if (direct && (!runtime.microphone_installed || !runtime.state?.microphone?.ready)) notes.unshift({bad:true,text:'Прямой захват микрофонов не подключён или пока не передаёт звук.'});
    if (direct) notes.push({text:'Пока работает этот режим, Алиса получает тишину. После выхода микрофоны возвращаются Алисе.'});
    if (!panel && !direct && c.microphone?.source === 'microphone') notes.push({text:'Сейчас показана одноразовая команда через Алису. Прямой захват используется после включения режима ассистента.'});
    if (journey === 'conversation' && !runtime.state?.mode) notes.unshift({text:'Так пойдёт разговор после включения режима ассистента. Сам режим эта схема не включает.'});
    return {nodes, notes};
  }
  const api = {build, signature, capabilities, conversationModel};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AssistantRoute = api;
})(globalThis);
