(() => {
  'use strict';
var document = window.XiaomiPluginClient.document;
  const $ = id => document.getElementById(id), live = ['active', 'waiting', 'paused'];
  const labels = { active: '下载中', waiting: '排队中', paused: '已暂停', complete: '已完成', error: '失败', removed: '已取消' };
  let snapshot = null, page = 'tasks', filter = 'all', settingsLoaded = false, trackersLoaded = false, btLoaded = false;
  let kind = 'url', torrent = '', folderFor = '', folderPath = '', newDirectory = '', defaultDirectory = '';
  let folderNodes = new Map(), folderDraft = null, folderEpoch = 0, folderReady = false;
  let nasTorrentPath = '', nasTorrentEpoch = 0;
  let detailId = '', detailOffset = 0, detailCount = 0, selected = new Set(), fileEdits = new Map();
  let confirmResolve = null, scrollY = 0, focusBefore = null, lastTrackerTime = 0, stopped = false, refreshing = false, lastJob = '', servicePending = null;
  const dialogs = () => Array.from(document.querySelectorAll('.dialog:not([hidden])'));
  const text = (tag, value, cls) => { const el = document.createElement(tag); el.textContent = value; if (cls) el.className = cls; return el; };
  const bytes = n => { n = Number(n) || 0; const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return n.toFixed(i ? 1 : 0) + ' ' + units[i]; };
  const pathLabel = s => '我的文件' + (s ? ' / ' + s : '');
  function toast(message, error = false) { $('toast').textContent = message; $('toast').classList.toggle('error', error); clearTimeout(toast.timer); toast.timer = setTimeout(() => { $('toast').textContent = ''; }, 6000); }
  async function api(action, data = {}) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await window.XiaomiPluginClient.request({ plugin: 'downloadcenter', cgi: 'downloadcenter.cgi', action,
        options: { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data), signal: controller.signal } });
      const raw = await response.text(); let result;
      try { result = JSON.parse(raw); } catch (_) { throw new Error('设备返回了无法解析的数据，请重新打开插件；不要重复提交下载'); }
      if (!response.ok || !result.ok) throw new Error(result.error || '设备请求失败');
      return result;
    } finally { clearTimeout(timer); }
  }
  async function busy(button, fn) { if (button.disabled) return; button.disabled = true; button.setAttribute('aria-busy', 'true'); try { await fn(); } catch (e) { toast(e.name === 'AbortError' ? '请求超时，请刷新确认结果后再操作' : e.message, true); } finally {
    const jobButtons = { pauseAll: 'pause', resumeAll: 'resume', clearHistory: 'clear' };
    const pending = (servicePending && button.id === (servicePending.command === 'restart' ? 'btRestart' : servicePending.command === 'start' ? 'serviceStart' : 'serviceStop')) || (snapshot?.job?.state === 'pending' && jobButtons[button.id] === snapshot.job.command) || (['coreCheck', 'coreApply', 'qbCheck', 'qbApply'].includes(button.id) && [snapshot?.coreUpdate?.state, snapshot?.qbUpdate?.state].some(s => ['pending', 'checking', 'downloading', 'installing'].includes(s)));
    button.disabled = Boolean(pending); if (!pending) button.removeAttribute('aria-busy');
    if (button.id === 'qbApply' && !snapshot?.qbUpdate?.updateAvailable) button.disabled = true;
    if (button.id === 'coreApply' && !snapshot?.coreUpdate?.updateAvailable) button.disabled = true;
    if (button.closest('.task-card') && snapshot && !dialogs().length && !document.querySelector('.task-card [aria-busy=true]')) renderTasks();
  } }
  function lock() {
    const open = dialogs().length > 0, root = document.documentElement, was = root.classList.contains('dialog-scroll-locked');
    if (open && !was) { scrollY = window.scrollY; focusBefore = document.activeElement; root.style.setProperty('--dialog-scroll-top', -scrollY + 'px'); root.classList.add('dialog-scroll-locked'); }
    if (!open && was) { root.classList.remove('dialog-scroll-locked'); root.style.removeProperty('--dialog-scroll-top'); if (!root.classList.contains('desktop-client')) window.scrollTo(0, scrollY); focusBefore?.focus({ preventScroll: true }); }
    $('backdrop').hidden = !open;
    const top = dialogs().sort((a, b) => Number(getComputedStyle(a).zIndex) - Number(getComputedStyle(b).zIndex)).pop();
    document.querySelector('.shell').inert = open;
    dialogs().forEach(d => { d.inert = d !== top; d.dataset.covered = d === top ? 'false' : 'true'; });
  }
  function show(id) { $(id).hidden = false; $(id).inert = false; lock(); $(id).querySelector('button,input,textarea')?.focus({ preventScroll: true }); }
  function close(id) { $(id).hidden = true; $(id).inert = false; if (id === 'folderDialog') { folderEpoch++; folderDraft = null; } lock(); }
  function confirm(title, message, remove = false) { $('confirmTitle').textContent = title; $('confirmMessage').textContent = message; $('deleteFilesRow').hidden = !remove; $('deleteFiles').checked = false; show('confirmDialog'); $('confirmCancel').focus(); return new Promise(r => { confirmResolve = r; }); }
  function answer(ok) { const result = { ok, deleteFiles: ok && $('deleteFiles').checked }; close('confirmDialog'); const resolve = confirmResolve; confirmResolve = null; resolve?.(result); }
  $('confirmCancel').onclick = () => answer(false); $('confirmAccept').onclick = () => answer(true);
  document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => close(b.dataset.close));
  $('backdrop').onclick = () => { if (!$('confirmDialog').hidden) answer(false); else { const d = dialogs().pop(); if (d) close(d.id); } };
  document.addEventListener('keydown', e => {
    const top = dialogs().sort((a, b) => Number(getComputedStyle(a).zIndex) - Number(getComputedStyle(b).zIndex)).pop(); if (!top) return;
    if (e.key === 'Escape') { e.preventDefault(); if (top.id === 'confirmDialog') answer(false); else close(top.id); }
    if (e.key === 'Tab') { const items = Array.from(top.querySelectorAll('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),[role=treeitem][tabindex="0"]')).filter(x => x.getClientRects().length && x.tabIndex >= 0); const first = items[0], last = items[items.length - 1]; if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); } else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); } }
  });
  let touchY = 0, swipe = null;
  document.addEventListener('touchstart', e => { if (e.touches.length === 1) { touchY = e.touches[0].clientY; if (!dialogs().length && !e.target.closest('input,textarea,button,.file-list')) swipe = [e.touches[0].clientX, touchY]; } }, { passive: true });
  document.addEventListener('touchmove', e => {
    if (!dialogs().length || e.touches.length !== 1) return;
    const dy = e.touches[0].clientY - touchY; touchY = e.touches[0].clientY; let el = e.target, can = false;
    if (el.closest('.dialog:not([hidden])')) while (el && el !== document.body) { if (el.scrollHeight > el.clientHeight + 1 && /auto|scroll/.test(getComputedStyle(el).overflowY) && ((dy > 0 && el.scrollTop > 0) || (dy < 0 && el.scrollTop + el.clientHeight < el.scrollHeight - 1))) { can = true; break; } if (el.classList.contains('dialog')) break; el = el.parentElement; }
    if (!can) e.preventDefault();
  }, { passive: false });
  document.addEventListener('touchend', e => { if (!swipe || dialogs().length) { swipe = null; return; } const dx = e.changedTouches[0].clientX - swipe[0], dy = e.changedTouches[0].clientY - swipe[1]; swipe = null; if (Math.abs(dx) > 85 && Math.abs(dx) > Math.abs(dy) * 2) { const pages = ['tasks', 'history', 'trackers', 'settings'], i = pages.indexOf(page) + (dx < 0 ? 1 : -1); if (pages[i]) go(pages[i]); } }, { passive: true });
  function go(name) { if (name !== 'settings') hideCredentials(); page = name; document.querySelectorAll('.page').forEach(e => e.classList.toggle('active', e.id === 'page-' + name)); document.querySelectorAll('[data-page]').forEach(b => { b.classList.toggle('active', b.dataset.page === name); b.setAttribute('aria-current', b.dataset.page === name ? 'page' : 'false'); }); document.querySelector('.shell').scrollTop = 0; if (!document.documentElement.classList.contains('desktop-client')) window.scrollTo(0, 0); }
  document.querySelectorAll('[data-page]').forEach(b => b.onclick = () => go(b.dataset.page));
  document.querySelectorAll('[data-filter]').forEach(b => b.onclick = () => { filter = b.dataset.filter; document.querySelectorAll('[data-filter]').forEach(x => x.classList.toggle('active', x === b)); renderTasks(); });
  $('search').oninput = renderTasks;
  function taskButton(label, fn, cls = '') { const b = text('button', label, cls); b.onclick = () => busy(b, fn); return b; }
  async function operation(t, command) { let deleteFiles = false; if (command === 'remove') { const result = await confirm('删除任务？', '默认保留下载文件。任务自动生成的种子缓存会一并清理，手动导入的原种子不受影响。勾选后删除该任务登记的下载和续传文件；任务创建的文件夹为空则删除，非空则保留。文件删除失败也移除记录并提示残留。', true); if (!result.ok) return; deleteFiles = result.deleteFiles; } const response = await api('task', { id: t.id, command, deleteFiles }); toast(response.warning || (command === 'remove' ? '任务记录已删除' : '操作已提交'), Boolean(response.warning)); await refresh(true); }
  function renderTasks() {
    if (!snapshot) return;
    const query = $('search').value.trim().toLowerCase();
    for (const [id, history] of [['taskList', false], ['historyList', true]]) {
      const list = $(id); list.replaceChildren();
      snapshot.tasks.filter(t => history !== live.includes(t.status)).filter(t => history || ((!query || t.name.toLowerCase().includes(query)) && (filter === 'all' || t.status === filter))).forEach(t => {
        const card = text('article', '', 'task-card'), head = text('div', '', 'task-head'); head.append(text('h3', t.name));
        const isSeed = t.isBT && t.status === 'active' && Number(t.totalLength) > 0 && Number(t.completedLength) >= Number(t.totalLength);
        head.append(text('span', isSeed ? '做种中' : (labels[t.status] || t.status), 'badge' + (t.status === 'error' ? ' failed' : ''))); card.append(head);
        const progress = text('div', '', 'progress'), bar = document.createElement('i'), ratio = Number(t.totalLength) ? Math.min(100, Number(t.completedLength) / Number(t.totalLength) * 100) : 0;
        bar.style.width = ratio + '%'; progress.append(bar); card.append(progress);
        const stats = text('div', '', 'task-stats'); stats.append(text('span', bytes(t.completedLength) + ' / ' + bytes(t.totalLength) + ' · ' + ratio.toFixed(1) + '%'), text('span', '↓ ' + bytes(t.downloadSpeed) + '/s'), text('span', '↑ ' + bytes(t.uploadSpeed) + '/s'));
        if (Number(t.downloadSpeed) > 0 && !isSeed) stats.append(text('span', '约 ' + Math.ceil((Number(t.totalLength) - Number(t.completedLength)) / Number(t.downloadSpeed) / 60) + ' 分钟'));
        card.append(stats, text('p', pathLabel(t.directory)));
        if (t.errorMessage) card.append(text('p', t.errorMessage, 'task-error'));
        const actions = text('div', '', 'actions');
        if (['active', 'waiting'].includes(t.status)) actions.append(taskButton('暂停', () => operation(t, 'pause')));
        if (t.status === 'paused') actions.append(taskButton('继续', () => operation(t, 'resume'), 'primary'));
        if (t.status === 'waiting') actions.append(taskButton('移到队首', () => operation(t, 'top')));
        if (['error', 'removed'].includes(t.status)) actions.append(taskButton('重试', () => operation(t, 'retry')));
        actions.append(taskButton('详情 / 文件', () => openDetail(t.id)), taskButton('删除', () => operation(t, 'remove'), 'danger'));
        card.append(actions); list.append(card);
      });
      if (!list.children.length) list.append(text('div', history ? '暂无历史记录' : '暂无下载任务，点击“新建下载”开始', 'empty'));
    }
  }
  async function refresh(fresh = false) {
    if (fresh && refreshing) await refreshing;
    if (!refreshing) refreshing = loadSnapshot().finally(() => { refreshing = null; });
    return refreshing;
  }
  async function loadSnapshot() {
    try {
      snapshot = await api('status'); const s = snapshot;
      $('health').textContent = s.running ? '服务运行中' : '服务未运行'; $('health').className = 'badge ' + (s.running ? 'enabled' : 'failed');
      $('engine').textContent = 'BT：qBittorrent ' + (s.engines?.qbittorrent?.version || '未连接') + ' · 直链：aria2 ' + (s.engineVersion || '未连接');
      $('version').textContent = '插件版本 ' + s.version; $('down').textContent = bytes(s.stats.downloadSpeed) + '/s'; $('up').textContent = bytes(s.stats.uploadSpeed) + '/s'; $('count').textContent = s.tasks.filter(t => live.includes(t.status)).length; $('free').textContent = bytes(s.free);
      $('errorBanner').hidden = !s.error; $('errorBanner').textContent = s.error;
      for (const id of ['newTask', 'pauseAll', 'resumeAll']) if (!$(id).hasAttribute('aria-busy')) $(id).disabled = !s.running;
      for (const [id, cmd] of [['pauseAll', 'pause'], ['resumeAll', 'resume'], ['clearHistory', 'clear']]) {
        const pending = s.job?.state === 'pending'; $(id).disabled = pending || !s.running;
        if (pending && s.job.command === cmd) $(id).setAttribute('aria-busy', 'true'); else $(id).removeAttribute('aria-busy');
      }
      if (s.job?.id && s.job.state === 'done' && s.job.id !== lastJob) { lastJob = s.job.id; toast('批量操作完成：成功 ' + s.job.count + '，失败 ' + (s.job.failed || []).length, (s.job.failed || []).length > 0); }
      if (!$('serviceStart').hasAttribute('aria-busy')) $('serviceStart').disabled = s.running;
      if (!$('serviceStop').hasAttribute('aria-busy')) $('serviceStop').disabled = !s.running;
      if (servicePending) {
        if (servicePending.command === 'restart' ? s.running && !s.bt?.restartRequired : s.running === (servicePending.command === 'start')) { toast(servicePending.command === 'restart' ? '下载核心已重启，BT 设置已生效' : '下载服务已' + (servicePending.command === 'start' ? '启动' : '停止')); servicePending = null; }
        else if (Date.now() - servicePending.time > 90000) { toast('服务状态未按预期改变，请查看运行日志', true); servicePending = null; }
      }
      for (const [id, command] of [['serviceStart', 'start'], ['serviceStop', 'stop']]) {
        $(id).disabled = Boolean(servicePending) || (command === 'start' ? s.running : !s.running);
        if (servicePending?.command === command) $(id).setAttribute('aria-busy', 'true'); else $(id).removeAttribute('aria-busy');
      }
      $('serviceInfo').textContent = s.enabled ? '开机自动恢复已启用；暂停的任务仍保持暂停。' : '服务已停用；下次开机不会自动启动。';
      for (const [key, prefix] of [['qbittorrent', 'qb'], ['aria2', 'aria']]) {
        const item = s.lanInterfaces?.[key] || {};
        $(prefix + 'LanState').textContent = item.listening ? '已开放 · 端口 ' + item.port : item.configured ? '未监听 · 配置端口 ' + item.port : '未开放到局域网';
        $(prefix + 'LanUrl').textContent = item.url || '仅本机访问';
      }
      if (!$('credentialsSave').hasAttribute('aria-busy')) $('credentialsSave').disabled = !s.credentialsEditable || Boolean(servicePending);
      $('credentialsStatus').textContent = s.credentialsEditable ? '服务已停止，可以保存密钥；保存后请启动服务。' : '服务运行期间不可修改密钥，请先停止下载服务。';
      if (!settingsLoaded) { fillSettings(); settingsLoaded = true; }
      if (!btLoaded) { fillBt(); btLoaded = true; }
      const bt = s.bt || {};
      $('btPort').textContent = 'BT 入站端口：' + (bt.peerPort || '未分配') + '（TCP / UDP）。有公网 IP 时需在路由器转发此端口，不要转发 RPC 管理端口。';
      $('btIpv6Hint').textContent = bt.ipv6Detected ? '检测到系统 IPv6 接口信息；是否拥有可用公网 IPv6 及防火墙放行仍需确认。' : '尚未检测到系统 IPv6 接口信息，开启开关不代表网络已支持 IPv6。';
      $('btApplyStatus').textContent = bt.restartRequired ? '已保存的 BT 参数尚未生效；下次启动应用，或点击下方按钮重启下载核心。' : (s.running ? '保存的 BT 参数与本次核心启动配置一致。' : '下次启动下载核心时应用已保存参数。');
      const restarting = servicePending?.command === 'restart';
      $('btRestart').disabled = !s.running || !bt.restartRequired || Boolean(servicePending) || s.coreUpdate?.state === 'installing';
      if (restarting) $('btRestart').setAttribute('aria-busy', 'true'); else $('btRestart').removeAttribute('aria-busy');
      if (!trackersLoaded) { fillTrackers(); trackersLoaded = true; }
      $('trackerSummary').textContent = '当前附加 Tracker：' + s.settings.trackers.length + ' 个 · 订阅源：' + s.settings.sources.length + ' 个';
      $('trackerStatus').textContent = s.trackerStatus.message || '尚未更新订阅';
      const core = s.coreUpdate || {}, qb = s.qbUpdate || {}, phases = ['pending', 'checking', 'downloading', 'installing'];
      const coreBusy = phases.includes(core.state), qbBusy = phases.includes(qb.state), updatingCore = coreBusy || qbBusy;
      $('coreVersion').textContent = '当前 ' + (s.engineVersion || core.current || '未读取') + (core.latest ? ' · 最新 ' + core.latest : '');
      $('coreStatus').textContent = core.message || '点击检查更新，获取静态核心发布渠道的最新版本。';
      $('coreCheck').disabled = updatingCore; $('coreApply').disabled = updatingCore || !core.updateAvailable;
      $('coreProxy').disabled = updatingCore;
      for (const id of ['coreCheck', 'coreApply']) {
        if (coreBusy && (core.command === 'apply' ? id === 'coreApply' : id === 'coreCheck')) $(id).setAttribute('aria-busy', 'true');
        else $(id).removeAttribute('aria-busy');
      }
      $('qbVersion').textContent = '当前 qBittorrent ' + (s.engines?.qbittorrent?.version || qb.current || '未读取') + ' · libtorrent ' + (s.engines?.qbittorrent?.libtorrent || qb.currentLibtorrent || '未读取') + (qb.latest ? '；最新 ' + qb.latest + ' / ' + qb.libtorrent : '');
      $('qbStatus').textContent = qb.message || '检查兼容渠道的稳定构建，不自动跨大版本升级。';
      $('qbCheck').disabled = updatingCore; $('qbApply').disabled = updatingCore || !qb.updateAvailable; $('qbProxy').disabled = updatingCore;
      for (const id of ['qbCheck', 'qbApply']) {
        if (qbBusy && (qb.command === 'apply' ? id === 'qbApply' : id === 'qbCheck')) $(id).setAttribute('aria-busy', 'true'); else $(id).removeAttribute('aria-busy');
      }
      if (core.state === 'installing' || qb.state === 'installing') { for (const id of ['serviceStart', 'serviceStop', 'btRestart']) $(id).disabled = true; }
      const applied = s.trackerApply;
      $('trackerApplyStatus').textContent = applied?.time ? '最近应用：qB 已追加 ' + (applied.qbApplied || 0) + ' 个任务，私有种子跳过 ' + (applied.qbPrivate || 0) + ' 个，等待元数据 ' + (applied.qbMetadata || 0) + ' 个，失败 ' + (applied.qbFailed || 0) + ' 个。追加成功不代表 Tracker 已连通。' : '';
      $('trackerResults').replaceChildren();
      (s.trackerStatus.sources || []).forEach(source => {
        const row = text('article', '', 'tracker-result');
        row.classList.toggle('failed', source.state === 'error');
        row.append(text('strong', '订阅 ' + source.index + (source.state === 'error' ? ' · 更新失败' : ' · 已更新')),
          text('p', source.url), text('p', source.message));
        $('trackerResults').append(row);
      });
      const updating = ['pending', 'updating'].includes(s.trackerStatus.state); $('updateTrackers').disabled = updating || !s.running;
      if (updating) $('updateTrackers').setAttribute('aria-busy', 'true'); else $('updateTrackers').removeAttribute('aria-busy');
      if (s.trackerStatus.time > lastTrackerTime && ['done', 'partial', 'error'].includes(s.trackerStatus.state)) { if (lastTrackerTime) toast(s.trackerStatus.message, s.trackerStatus.state !== 'done'); lastTrackerTime = s.trackerStatus.time; }
      // Do not replace buttons underneath an ongoing confirmation/operation.
      if (!dialogs().length && !document.querySelector('.task-card [aria-busy=true]')) renderTasks();
    } catch (e) { $('health').textContent = '连接失败'; $('errorBanner').hidden = false; $('errorBanner').textContent = e.message; }
  }
  $('refresh').onclick = () => busy($('refresh'), refresh);
  $('coreCheck').onclick = () => busy($('coreCheck'), async () => { await api('core_check', { proxy: $('coreProxy').checked }); await refresh(); });
  $('qbCheck').onclick = () => busy($('qbCheck'), async () => { await api('qb_check', { proxy: $('qbProxy').checked }); await refresh(); });
  $('qbApply').onclick = () => busy($('qbApply'), async () => {
    if (!(await confirm('升级 qBittorrent 内核？', '校验下载文件后会短暂重启当前用户的两个下载核心，Openlist 共用的下载也会中断并恢复。保留任务和配置；新核心启动失败会尝试恢复原核心与状态。')).ok) return;
    await api('qb_apply', { proxy: $('qbProxy').checked }); await refresh();
  });
  $('trackerVerify').onclick = () => busy($('trackerVerify'), async () => {
    const box = $('trackerVerification'); box.replaceChildren(text('p', '正在读取 qB 任务的实际 Tracker…'));
    try {
      const r = await api('tracker_verify'); box.replaceChildren(text('p', '已保存 ' + r.configured + ' 个 Tracker；qB 当前 ' + r.tasks + ' 个任务。' + (!r.tasks ? '暂无任务，尚不能验证实际调用或连通情况。' : r.partial ? '本次仅核对前 ' + r.checked + ' 个任务。' : '')));
      r.rows.forEach(t => { const row = text('article', '', 'tracker-result'); row.append(text('strong', t.name), text('p', (t.owned ? '下载中心' : '外部 / Openlist') + (t.private ? ' · 私有种子（不追加）' : '') + ' · 匹配附加列表 ' + t.matched + ' 个'), text('p', '实际 Tracker ' + t.total + ' 个，工作中 ' + t.working + ' 个，报告错误 ' + t.failed + ' 个。')); box.append(row); });
    } catch (e) { box.replaceChildren(text('p', '核对失败：' + e.message)); throw e; }
  });
  $('cleanCache').onclick = () => busy($('cleanCache'), async () => {
    const answer = await confirm('清理磁链缓存？', '仅删除插件内已无任务使用的自动种子和临时缓存。保留所有现有任务、下载文件、手动导入的种子及续传数据；不会扫描下载目录。请保持下载服务运行以核对占用。');
    if (!answer.ok) return;
    const result = await api('cache_clean');
    const message = '已清理 ' + result.files + ' 个缓存文件、' + result.directories + ' 个空目录，释放 ' + bytes(result.bytes) + '；保留 ' + result.protected + ' 个任务缓存目录。' +
      (result.retained ? '另有 ' + result.retained + ' 个非标准项目已保留。' : '') +
      (result.partial ? '本次已达处理上限，可再次清理。' : '') +
      (result.failed.length ? '部分缓存未能清理：' + result.failed.slice(0, 3).join('；') : '');
    $('cacheStatus').textContent = message; toast(result.failed.length ? '清理完成，部分缓存已保留，请查看结果' : '磁链缓存清理完成', result.failed.length > 0);
  });
  $('coreApply').onclick = () => busy($('coreApply'), async () => {
    if (!(await confirm('更新 aria2 核心？', '将从第三方静态构建发布渠道下载并校验 SHA-256。切换时下载会短暂中断，任务和设置保留；启动失败会恢复旧核心。')).ok) return;
    await api('core_apply', { proxy: $('coreProxy').checked }); await refresh();
  });
  function fillSettings() { const s = snapshot.settings; defaultDirectory = s.directory; $('defaultDirectory').textContent = pathLabel(defaultDirectory); for (const id of ['concurrent', 'connections', 'downloadKiB', 'uploadKiB', 'seedRatio', 'seedMinutes']) $(id).value = s[id]; }
  const btSwitches = ['ipv6', 'dht', 'pex', 'lpd', 'upnp'];
  function fillBt() { const s = snapshot.settings; for (const key of btSwitches) $('bt-' + key).checked = Boolean(s[key]); $('bt-maxPeers').value = s.maxPeers ?? 100; $('bt-globalPeers').value = s.globalPeers ?? 300; }
  $('btForm').onsubmit = e => { e.preventDefault(); busy(e.submitter, async () => {
    const data = Object.fromEntries(btSwitches.map(key => [key, $('bt-' + key).checked]));
    data.maxPeers = Number($('bt-maxPeers').value); data.globalPeers = Number($('bt-globalPeers').value);
    const r = await api('bt_settings', data); toast(r.restartRequired ? '已保存，重启下载核心后生效；未中断当前任务' : 'BT 设置已保存'); await refresh(true);
  }); };
  $('btRestart').onclick = () => busy($('btRestart'), async () => {
    if (!(await confirm('重启下载核心？', '仅重启当前用户的两个下载核心；共用此核心的 Openlist 下载也会短暂中断并从已保存的会话恢复；暂停任务仍保持暂停。不会重启 NAS 或删除文件。')).ok) return;
    await api('service', { command: 'restart' }); servicePending = { command: 'restart', time: Date.now() }; toast('正在后台重启，完成后应用 BT 参数'); await refresh(true);
  });
  $('openlistInfo').onclick = () => busy($('openlistInfo'), async () => { const box = $('openlistDetails'); if (!box.hidden) { box.hidden = true; $('openlistUrl').value = ''; $('openlistInfo').textContent = '显示 Openlist 连接信息'; return; } const r = await api('openlist_info'); $('openlistUrl').value = r.url; $('openlistDirectory').textContent = '容器临时目录对应 NAS：' + r.directory; box.hidden = false; $('openlistInfo').textContent = '隐藏连接信息'; });
  function hideCredentials() {
    for (const id of ['ariaSecret', 'qbSecret']) { $(id).value = ''; $(id).type = 'password'; }
    $('credentialsShow').textContent = '显示当前密钥';
  }
  $('credentialsShow').onclick = () => busy($('credentialsShow'), async () => {
    if ($('ariaSecret').type === 'text') { hideCredentials(); return; }
    const values = await api('credentials_get');
    for (const [id, key] of [['ariaSecret', 'aria2'], ['qbSecret', 'qbittorrent']]) { $(id).value = values[key] || ''; $(id).type = 'text'; }
    $('credentialsShow').textContent = '隐藏并清空';
  });
  $('credentialsForm').onsubmit = e => {
    e.preventDefault();
    busy($('credentialsSave'), async () => {
      if (!snapshot?.credentialsEditable) throw new Error('请先停止下载服务');
      const data = {aria2: $('ariaSecret').value, qbittorrent: $('qbSecret').value};
      if (!data.aria2 && !data.qbittorrent) throw new Error('请至少填写一项新密钥');
      if (!(await confirm('保存接口密钥？', '保存后请启动下载服务，并更新对应的 Openlist、AriaNg 等客户端密码；未填写的项目保持不变。')).ok) return;
      const result = await api('credentials_save', data); hideCredentials();
      toast(result.message); await refresh(true);
    });
  };
  window.addEventListener('pagehide', hideCredentials);
  window.addEventListener('unmount', hideCredentials);
  $('openlistCopy').onclick = () => busy($('openlistCopy'), async () => { try { await navigator.clipboard.writeText($('openlistUrl').value); toast('连接地址已复制，请勿公开'); } catch (_) { $('openlistUrl').focus(); $('openlistUrl').select(); toast('请长按或使用 Ctrl+C 复制选中的地址'); } });
  function fillTrackers() { const s = snapshot.settings; $('trackerText').value = (s.manualTrackers || s.trackers).join('\n'); $('trackerSources').value = s.sources.join('\n'); $('autoTrackers').checked = s.autoTrackers; }
  $('settingsForm').onsubmit = e => { e.preventDefault(); busy(e.submitter, async () => { const data = { directory: defaultDirectory }; for (const id of ['concurrent', 'connections', 'downloadKiB', 'uploadKiB', 'seedRatio', 'seedMinutes']) data[id] = Number($(id).value); const r = await api('settings', data); toast(r.applied ? '设置已保存并应用' : '设置已保存，下次服务启动时生效'); await refresh(); }); };
  const lines = str => str.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  $('trackerForm').onsubmit = e => { e.preventDefault(); busy(e.submitter, async () => { const r = await api('trackers', { trackers: lines($('trackerText').value), sources: lines($('trackerSources').value), autoTrackers: $('autoTrackers').checked }); toast(r.deferred ? '已保存，服务启动后生效' : '已应用，失败任务 ' + r.failed.length + ' 个'); await refresh(); }); };
  $('updateTrackers').onclick = () => busy($('updateTrackers'), async () => { await api('tracker_update'); toast('订阅在后台更新，可继续使用其他页面'); await refresh(); });
  $('resetTrackers').onclick = () => busy($('resetTrackers'), async () => { if (!(await confirm('恢复默认 Tracker 设置？', '清空手动与订阅列表，关闭自动更新。种子自带 Tracker 和 DHT 保留。')).ok) return; await api('trackers', { trackers: [], sources: [], autoTrackers: false }); trackersLoaded = false; await refresh(); });
  for (const [id, command] of [['serviceStart', 'start'], ['serviceStop', 'stop']]) $(id).onclick = () => busy($(id), async () => { if (command === 'stop' && !(await confirm('停止下载服务？', '当前用户的所有下载和做种将停止，Openlist 共用此核心的任务也会停止；任务会保存，不影响其他用户。')).ok) return; await api('service', { command }); servicePending = { command, time: Date.now() }; toast('服务操作已提交，状态将在几秒后刷新'); await refresh(); });
  for (const [id, command] of [['pauseAll', 'pause'], ['resumeAll', 'resume']]) $(id).onclick = () => busy($(id), async () => { if (command === 'resume' && !(await confirm('继续全部任务？', '当前用户所有暂停任务将继续下载或做种。')).ok) return; await api('batch', { command }); toast('已在后台执行，结果会自动刷新'); await refresh(); });
  $('clearHistory').onclick = () => busy($('clearHistory'), async () => { if (!(await confirm('清空历史记录？', '仅清除已完成、失败和取消的记录，保留所有文件；进行中的任务不受影响。')).ok) return; await api('clear_history'); toast('正在后台清理记录'); await refresh(); });
  function setKind(value) { kind = value; $('urlField').hidden = kind !== 'url'; $('nameField').hidden = kind !== 'url'; $('torrentField').hidden = kind !== 'torrent'; document.querySelectorAll('[data-kind]').forEach(b => b.classList.toggle('active', b.dataset.kind === kind)); }
  document.querySelectorAll('[data-kind]').forEach(b => b.onclick = () => setKind(b.dataset.kind));
  $('newTask').onclick = () => { newDirectory = snapshot?.settings.directory || ''; $('newDirectory').textContent = pathLabel(newDirectory); show('newDialog'); };
  function fileRows(host, files, onChange, editable = true) {
    host.replaceChildren(); host.classList.add('bt-file-browser');
    if (!files.length) { host.append(text('p', '暂无文件', 'empty')); return; }
    const paged = host.id === 'detailFiles', hasProgress = paged, groups = new Map(), rows = [];
    const parents = files.map(f => String(f.displayPath || f.path).split('/').slice(0, -1));
    const common = parents[0].slice();
    parents.forEach(parts => { while (common.some((part, i) => parts[i] !== part)) common.pop(); });
    const toolbar = text('div', '', 'bt-file-tools'), allLabel = text('label', '', 'bt-select-all');
    const all = document.createElement('input'); all.type = 'checkbox'; all.disabled = !editable;
    all.setAttribute('aria-label', paged ? '全选本页文件' : '全选文件');
    const summary = text('span', '', 'bt-selection-summary'), invert = text('button', '反选', 'bt-invert');
    invert.type = 'button'; invert.disabled = !editable;
    allLabel.append(all, text('span', paged ? '本页全选' : '全选')); toolbar.append(allLabel, summary, invert);
    const scroller = text('div', '', 'bt-file-scroll');
    const header = text('div', '', 'bt-file-columns' + (hasProgress ? ' with-progress' : ''));
    header.append(text('span', '文件名称'), text('span', '大小'));
    if (hasProgress) header.append(text('span', '进度'));
    scroller.append(header); host.append(toolbar, scroller);
    files.forEach((file, position) => {
      const path = String(file.displayPath || file.path).split('/'), name = path.pop() || '未命名文件';
      const groupName = path.slice(common.length).join('/');
      if (!groups.has(groupName)) groups.set(groupName, []);
      groups.get(groupName).push({ file, name, position });
    });
    function change(row, checked) { row.input.checked = checked; onChange(row.file.index, checked); }
    function update() {
      const picked = rows.filter(row => row.input.checked);
      all.checked = picked.length === rows.length; all.indeterminate = picked.length > 0 && picked.length < rows.length;
      summary.textContent = (paged ? '本页 ' : '') + '已选 ' + picked.length + ' / ' + rows.length + ' · ' + bytes(picked.reduce((n, row) => n + Number(row.file.length || 0), 0));
      rows.forEach(row => { if (row.progress) row.progress.textContent = row.input.checked ? row.percent : '未选择'; });
    }
    groups.forEach((entries, groupName) => {
      if (groups.size > 1 || groupName) {
        const group = text('div', '', 'bt-file-group'); group.append(text('span', '', 'folder-icon'), text('span', groupName || '当前目录'), text('small', entries.length + ' 项')); scroller.append(group);
      }
      entries.forEach(({file, name}) => {
        const label = text('label', '', 'bt-file-row' + (hasProgress ? ' with-progress' : ''));
        const input = document.createElement('input'); input.type = 'checkbox'; input.checked = file.selected !== 'false'; input.disabled = !editable; input.dataset.index = file.index;
        const cell = text('span', '', 'bt-file-name-cell'), filename = text('span', name, 'bt-file-name'); filename.title = name;
        const video = /\.(mkv|mp4|avi|mov|ts|m2ts|webm)$/i.test(name), archive = /\.(zip|rar|7z|tar|gz)$/i.test(name);
        const icon = text('span', video ? '▶' : archive ? '▣' : '▤', 'bt-file-icon' + (video ? ' video' : '')); icon.setAttribute('aria-hidden', 'true');
        cell.append(input, icon, filename); label.append(cell, text('span', bytes(file.length), 'bt-file-size'));
        const percent = Number(file.length) ? Math.min(100, Math.floor(Number(file.completedLength || 0) / Number(file.length) * 100)) + '%' : '0%';
        const progress = hasProgress ? text('span', percent, 'bt-file-progress') : null;
        if (progress) label.append(progress);
        const row = {file, input, progress, percent}; rows.push(row);
        input.onchange = () => { onChange(file.index, input.checked); update(); };
        scroller.append(label);
      });
    });
    all.onchange = () => { rows.forEach(row => change(row, all.checked)); update(); };
    invert.onclick = () => { rows.forEach(row => change(row, !row.input.checked)); update(); };
    update();
  }
  $('chooseTorrent').onclick = () => { $('torrentInput').value = ''; $('torrentInput').click(); };
  function applyTorrent(info, encoded, filename) {
    torrent = encoded; selected = new Set(info.files.map(f => f.index));
    $('torrentFilename').textContent = filename; $('torrentFilename').classList.add('has-file');
    $('torrentLabel').textContent = info.name + ' · ' + bytes(info.total);
    fileRows($('torrentFiles'), info.files, (i, checked) => checked ? selected.add(i) : selected.delete(i));
  }
  async function browseNasTorrents(path) {
    const epoch = ++nasTorrentEpoch;
    nasTorrentPath = path; $('nasTorrentPath').textContent = pathLabel(path);
    $('nasTorrentUp').disabled = !path;
    $('nasTorrentList').replaceChildren(text('p', '正在读取…'));
    try {
      const result = await api('torrent_browse', { path });
      if (epoch !== nasTorrentEpoch || $('nasTorrentDialog').hidden) return;
      const host = $('nasTorrentList'); host.replaceChildren();
      if (!result.entries.length) host.append(text('p', '此目录没有文件夹或种子文件'));
      result.entries.forEach(entry => {
        const button = text('button', ''); button.type = 'button';
        const icon = text('span', entry.directory ? '' : '⇩', entry.directory ? 'folder-icon' : 'seed-file-icon'); icon.setAttribute('aria-hidden', 'true');
        const label = text('span', entry.name); if (!entry.directory) label.append(text('small', bytes(entry.size)));
        button.append(icon, label);
        button.onclick = () => entry.directory ? browseNasTorrents(entry.path) : busy(button, async () => {
          const info = await api('torrent_nas_preview', { path: entry.path });
          if (epoch !== nasTorrentEpoch || $('nasTorrentDialog').hidden) return;
          applyTorrent(info, info.torrent, 'NAS · ' + entry.path); close('nasTorrentDialog');
        });
        host.append(button);
      });
    } catch (error) {
      if (epoch !== nasTorrentEpoch || $('nasTorrentDialog').hidden) return;
      $('nasTorrentList').replaceChildren(text('p', error.message, 'task-error'));
      const retry = text('button', '重新读取'); retry.type = 'button'; retry.onclick = () => browseNasTorrents(path); $('nasTorrentList').append(retry);
    }
  }
  $('chooseNasTorrent').onclick = () => { show('nasTorrentDialog'); browseNasTorrents(nasTorrentPath); };
  $('nasTorrentRoot').onclick = () => browseNasTorrents('');
  $('nasTorrentUp').onclick = () => browseNasTorrents(nasTorrentPath.split('/').slice(0, -1).join('/'));
  $('torrentInput').onchange = async () => {
    const file = $('torrentInput').files[0]; if (!file) return;
    torrent = ''; selected.clear(); $('torrentFiles').replaceChildren(); $('torrentLabel').textContent = '';
    $('torrentFilename').textContent = file.name; $('torrentFilename').classList.add('has-file');
    $('submitDownload').disabled = true; $('chooseNasTorrent').disabled = true;
    try {
      await busy($('chooseTorrent'), async () => {
        try {
          if (file.size > 4 * 1024 * 1024) throw new Error('种子不能超过 4 MiB');
          $('torrentLabel').textContent = '正在读取种子…';
          const raw = new Uint8Array(await file.arrayBuffer()); let encoded = '';
          for (let i = 0; i < raw.length; i += 8192) encoded += String.fromCharCode(...raw.subarray(i, i + 8192));
          const b64 = btoa(encoded), info = await api('torrent_preview', { torrent: b64 });
          applyTorrent(info, b64, file.name);
        } catch (e) { $('torrentLabel').textContent = '读取失败，请重新选择种子'; throw e; }
      });
    } finally { $('submitDownload').disabled = false; $('chooseNasTorrent').disabled = false; }
  };
  $('newForm').onsubmit = e => { e.preventDefault(); busy(e.submitter, async () => {
    const base = { directory: newDirectory, paused: $('startPaused').checked };
    if (kind === 'torrent') { if (!torrent) throw new Error('请先选择有效种子'); await api('add', { ...base, torrent, selected: Array.from(selected) }); }
    else { const urls = lines($('urls').value); if (!urls.length || urls.length > 20) throw new Error('请输入 1–20 条链接'); if ($('outName').value && urls.length > 1) throw new Error('批量链接不能共用一个保存文件名'); let done = 0; for (const url of urls) { try { await api('add', { ...base, url, name: $('outName').value.trim() }); done++; } catch (err) { $('urls').value = urls.slice(done).join('\n'); throw new Error('已添加 ' + done + ' 条；剩余链接保留。' + err.message); } } }
    close('newDialog'); $('urls').value = ''; $('outName').value = ''; torrent = ''; $('torrentInput').value = ''; $('torrentFilename').textContent = '未选择文件'; $('torrentFilename').classList.remove('has-file'); $('torrentFiles').replaceChildren(); $('torrentLabel').textContent = ''; toast('下载任务已添加'); await refresh();
  }); };
  function folderNode(path, name) { if (!folderNodes.has(path)) folderNodes.set(path, { path, name, children: null, expanded: false, loading: false, error: '', free: null }); return folderNodes.get(path); }
  function treeItem(path) { return Array.from($('folders').querySelectorAll('[role=treeitem]')).find(el => el.dataset.path === path); }
  function folderButtons() { $('createFolder').disabled = !folderReady || Boolean(folderDraft); $('chooseFolder').disabled = !folderReady || Boolean(folderDraft); }
  function selectFolder(path, focus = false) {
    if (folderDraft) return;
    folderPath = path; renderTree();
    if (focus) treeItem(path)?.focus({ preventScroll: true });
  }
  async function loadFolder(node, epoch = folderEpoch) {
    if (node.children !== null) return;
    if (node.pending) return node.pending;
    node.loading = true; node.error = ''; renderTree();
    node.pending = (async () => {
      try {
        const result = await api('browse', { path: node.path });
        if (epoch !== folderEpoch) return;
        node.children = result.entries.map(entry => { folderNode(entry.path, entry.name); return entry.path; }); node.free = result.free; if (!node.path) folderReady = true;
      } catch (error) { if (epoch === folderEpoch) node.error = error.message; throw error; }
      finally { node.loading = false; node.pending = null; if (epoch === folderEpoch) renderTree(); }
    })();
    return node.pending;
  }
  async function toggleFolder(node) {
    if (folderDraft) return;
    const epoch = folderEpoch; node.expanded = !node.expanded; if (!node.expanded && (!node.path || folderPath.startsWith(node.path + '/'))) folderPath = node.path; renderTree();
    if (node.expanded) { try { await loadFolder(node, epoch); } catch (error) { toast(error.message, true); } }
    if (epoch === folderEpoch) treeItem(node.path)?.focus({ preventScroll: true });
  }
  function cancelFolderDraft() { if (!folderDraft || folderDraft.saving) return; const parent = folderDraft.parent; folderDraft = null; selectFolder(parent, true); }
  async function saveFolderDraft() {
    const draft = folderDraft, epoch = folderEpoch; if (!draft || draft.saving) return;
    const name = draft.name.trim();
    if (!name || name === '.' || name === '..' || /[/\\\x00-\x1f]/.test(name)) { draft.error = '请输入有效文件夹名称，不能包含路径符号'; renderTree(); $('folderName')?.focus(); return; }
    draft.saving = true; draft.error = ''; renderTree();
    try {
      const result = await api('mkdir', { path: draft.parent, name });
      if (epoch !== folderEpoch) { toast('文件夹已创建：' + name); return; }
      const parent = folderNodes.get(draft.parent);
      folderNode(result.path, name).children = [];
      parent.children = Array.from(new Set([...(parent.children || []), result.path])).sort((a, b) => folderNodes.get(a).name.localeCompare(folderNodes.get(b).name, 'zh-CN'));
      parent.expanded = true; folderDraft = null; selectFolder(result.path, true); treeItem(result.path)?.scrollIntoView({ block: 'nearest' });
    } catch (error) { if (epoch === folderEpoch) { draft.saving = false; draft.error = error.message; renderTree(); $('folderName')?.focus(); } }
  }
  function renderTree() {
    const host = $('folders'), scroll = host.scrollTop, active = document.activeElement;
    const editing = active?.id === 'folderName', selection = editing ? [active.selectionStart, active.selectionEnd] : null;
    const focusedPath = active?.getAttribute('role') === 'treeitem' ? active.dataset.path : null;
    host.replaceChildren();
    function append(node, container, depth) {
      const item = text('div', '', 'tree-item'); item.dataset.path = node.path; item.setAttribute('role', 'treeitem'); item.setAttribute('aria-level', depth + 1); item.setAttribute('aria-label', node.name); item.setAttribute('aria-selected', String(folderPath === node.path)); item.tabIndex = folderPath === node.path ? 0 : -1;
      const hasChildren = node.children === null || node.children.length > 0 || folderDraft?.parent === node.path;
      if (hasChildren) item.setAttribute('aria-expanded', String(node.expanded));
      const row = text('div', '', 'tree-row' + (folderPath === node.path ? ' selected' : '')); row.style.setProperty('--tree-depth', Math.min(depth, 8));
      const arrow = text('button', hasChildren ? (node.expanded ? '▾' : '▸') : '', 'tree-toggle'); arrow.type = 'button'; arrow.tabIndex = -1; arrow.disabled = !hasChildren || Boolean(folderDraft); arrow.setAttribute('aria-label', (node.expanded ? '折叠 ' : '展开 ') + node.name); if (node.loading) arrow.setAttribute('aria-busy', 'true');
      arrow.onclick = event => { event.stopPropagation(); toggleFolder(node); };
      const icon = text('span', '', 'folder-icon'); icon.setAttribute('aria-hidden', 'true');
      row.append(arrow, icon, text('span', node.name, 'tree-name')); row.onclick = event => { event.stopPropagation(); selectFolder(node.path, true); }; item.append(row); container.append(item);
      item.onkeydown = event => {
        if (event.target !== item || folderDraft) return;
        const visible = Array.from(host.querySelectorAll('[role=treeitem]')), index = visible.indexOf(item);
        if (['ArrowDown', 'ArrowUp', 'Home', 'End', 'ArrowRight', 'ArrowLeft', 'Enter', ' '].includes(event.key)) { event.preventDefault(); event.stopPropagation(); }
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { const target = visible[index + (event.key === 'ArrowDown' ? 1 : -1)]; if (target) selectFolder(target.dataset.path, true); }
        if (event.key === 'Home' || event.key === 'End') selectFolder(visible[event.key === 'Home' ? 0 : visible.length - 1].dataset.path, true);
        if (event.key === 'ArrowRight') { if (!node.expanded && hasChildren) toggleFolder(node); else if (node.children?.length) selectFolder(node.children[0], true); }
        if (event.key === 'ArrowLeft') { if (node.expanded) toggleFolder(node); else if (node.path) selectFolder(node.path.split('/').slice(0, -1).join('/'), true); }
        if (event.key === 'Enter' || event.key === ' ') selectFolder(node.path, true);
      };
      if (node.expanded) {
        const group = text('div', '', 'tree-group'); group.setAttribute('role', 'group'); item.append(group);
        (node.children || []).forEach(path => append(folderNodes.get(path), group, depth + 1));
        if (node.error) group.append(text('p', node.error + '（折叠后再次展开可重试）', 'tree-error'));
        if (folderDraft?.parent === node.path) {
          const draft = folderDraft, wrap = text('div', '', 'tree-draft'), edit = text('div', '', 'tree-row tree-edit'); edit.style.setProperty('--tree-depth', Math.min(depth + 1, 8));
          const icon = text('span', '', 'folder-icon'); icon.setAttribute('aria-hidden', 'true');
          const input = document.createElement('input'); input.id = 'folderName'; input.maxLength = 150; input.value = draft.name; input.disabled = draft.saving; input.setAttribute('aria-label', '新文件夹名称'); input.oninput = () => { draft.name = input.value; if (draft.error) { draft.error = ''; renderTree(); } };
          input.onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); $('saveFolder')?.focus(); } if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); cancelFolderDraft(); } };
          const save = text('button', '✓', 'tree-save'); save.id = 'saveFolder'; save.type = 'button'; save.setAttribute('aria-label', '确认新建文件夹'); save.disabled = draft.saving; if (draft.saving) save.setAttribute('aria-busy', 'true'); save.onclick = saveFolderDraft;
          const cancel = text('button', '×', 'tree-cancel'); cancel.id = 'cancelFolder'; cancel.type = 'button'; cancel.setAttribute('aria-label', '取消新建文件夹'); cancel.disabled = draft.saving; cancel.onclick = cancelFolderDraft;
          edit.append(icon, input, save, cancel); wrap.append(edit); if (draft.error) { const error = text('p', draft.error, 'tree-error'); error.setAttribute('role', 'alert'); wrap.append(error); } group.append(wrap);
        }
      }
    }
    const root = folderNodes.get(''); if (root) append(root, host, 0);
    $('folderPath').textContent = '已选：' + pathLabel(folderPath) + (root?.free != null ? ' · 可用 ' + bytes(root.free) : ''); folderButtons(); host.scrollTop = scroll;
    if (editing && $('folderName')) { $('folderName').focus({ preventScroll: true }); $('folderName').setSelectionRange(...selection); }
    else if (focusedPath !== null) treeItem(focusedPath)?.focus({ preventScroll: true });
  }
  async function revealFolder(path) {
    if (folderDraft) return;
    const epoch = folderEpoch; let node = folderNodes.get('');
    try {
      node.expanded = true; await loadFolder(node, epoch);
      for (const part of path.split('/').filter(Boolean)) {
        if (epoch !== folderEpoch) return;
        const next = node.children?.find(p => folderNodes.get(p).name === part);
        if (!next) throw new Error('目录已移动或不存在，请重新选择');
        node = folderNodes.get(next); node.expanded = true; await loadFolder(node, epoch);
      }
      if (epoch === folderEpoch) { folderReady = true; selectFolder(node.path); treeItem(node.path)?.scrollIntoView({ block: 'nearest' }); }
    } catch (error) { if (epoch === folderEpoch) { folderReady = folderNodes.get('').children !== null; selectFolder(''); toast(error.message, true); } }
  }
  for (const [id, field] of [['newDirectory', 'new'], ['defaultDirectory', 'default']]) $(id).onclick = () => busy($(id), async () => {
    folderFor = field; folderEpoch++; folderNodes = new Map(); folderDraft = null; folderReady = false; folderPath = ''; folderNode('', '我的文件'); renderTree();
    $('recentFolders').replaceChildren(); const recent = Array.from(new Set([snapshot?.settings.directory || '', ...(snapshot?.settings.recentDirectories || [])])).filter(Boolean);
    if (recent.length) { $('recentFolders').append(text('p', '默认 / 最近目录')); recent.forEach(path => $('recentFolders').append(taskButton(path, () => revealFolder(path)))); }
    show('folderDialog'); await revealFolder(field === 'new' ? newDirectory : defaultDirectory);
  });
  $('createFolder').onclick = async () => {
    if (!folderReady || folderDraft) return;
    const epoch = folderEpoch, node = folderNodes.get(folderPath); folderDraft = { parent: node.path, name: '新建文件夹', saving: true, error: '' }; node.expanded = true; renderTree();
    try { await loadFolder(node, epoch); if (epoch !== folderEpoch) return; folderDraft.saving = false; renderTree(); $('folderName').focus(); $('folderName').select(); $('folderName').scrollIntoView({ block: 'nearest' }); }
    catch (error) { if (epoch === folderEpoch) { folderDraft = null; renderTree(); toast(error.message, true); } }
  };
  $('chooseFolder').onclick = () => { if (!folderReady || folderDraft) return; if (folderFor === 'new') { newDirectory = folderPath; $('newDirectory').textContent = pathLabel(folderPath); } else { defaultDirectory = folderPath; $('defaultDirectory').textContent = pathLabel(folderPath); } close('folderDialog'); };
  async function openDetail(id) { detailId = id; detailOffset = 0; fileEdits = new Map(); await loadDetail(); show('detailDialog'); }
  async function loadDetail() { const d = await api('detail', { id: detailId, offset: detailOffset }); detailCount = d.fileCount; $('detailTitle').textContent = d.name; $('detailSummary').textContent = pathLabel(d.directory) + ' · 连接 ' + (d.connections || 0) + ' · 种子 ' + (d.numSeeders || 0); $('detailError').textContent = d.errorMessage || ''; d.files.forEach(f => { if (fileEdits.has(f.index)) f.selected = fileEdits.get(f.index) ? 'true' : 'false'; }); fileRows($('detailFiles'), d.files, (i, checked) => fileEdits.set(i, checked), d.status === 'paused' && d.isBT); $('saveFiles').disabled = d.status !== 'paused' || !d.isBT; $('prevFiles').disabled = detailOffset === 0; $('nextFiles').disabled = detailOffset + 200 >= detailCount; $('filePage').textContent = Math.floor(detailOffset / 200) + 1 + ' / ' + Math.max(1, Math.ceil(detailCount / 200)); $('detailTrackers').textContent = d.trackers.flat().join('\n') || '无（也可通过 DHT 发现节点）'; }
  $('prevFiles').onclick = () => busy($('prevFiles'), async () => { detailOffset = Math.max(0, detailOffset - 200); await loadDetail(); }); $('nextFiles').onclick = () => busy($('nextFiles'), async () => { detailOffset += 200; await loadDetail(); });
  $('saveFiles').onclick = () => busy($('saveFiles'), async () => { const chosen = []; for (let offset = 0; offset < detailCount; offset += 200) { const d = await api('detail', { id: detailId, offset }); d.files.forEach(f => { if (fileEdits.has(f.index) ? fileEdits.get(f.index) : f.selected !== 'false') chosen.push(f.index); }); } await api('select_files', { id: detailId, selected: chosen }); toast('文件选择已保存，可继续任务'); await loadDetail(); });
  async function poll() { if (stopped) return; if (!document.hidden) await refresh(); if (!stopped) setTimeout(poll, 4000); }
  window.addEventListener('pagehide', () => { stopped = true; }); window.addEventListener('unmount', () => { stopped = true; });
  poll();
})();
