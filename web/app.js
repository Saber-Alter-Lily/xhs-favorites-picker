const $ = id => document.getElementById(id);
const DISCOVER_PAGE_SIZE = 48;
const LOCAL_SESSION_TOKEN = document.querySelector('meta[name="xhs-picker-token"]')?.content || '';

let activeView = 'discover';
let discoverItems = [];
let discoverPage = 1;
let discoverSourceLabel = '我的收藏';
let selectedPosts = new Set();
let currentJob = null;
let currentJobMode = 'discover';
let authorPaging = {id: '', name: '', cursor: '', hasMore: false};

let subscriptionData = {authors: [], updates: [], author_count: 0, pending_count: 0};
let subscriptionAuthorIds = new Set();
let selectedUpdates = new Set();
let subscriptionPolling = false;

let libraryPosts = [];
let libraryAuthors = [];
let libraryMode = 'articles';
let libraryRoot = '';
let selectedImages = new Set();
let currentArticle = null;
let lightImages = [];
let lightIndex = 0;

function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.style.display = 'none', 4200);
}

async function api(url, opt = {}) {
  const headers = new Headers(opt.headers || {});
  headers.set('X-XHS-Picker-Token', LOCAL_SESSION_TOKEN);
  const r = await fetch(url, {...opt, headers});
  let j = {};
  try { j = await r.json(); } catch {}
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

function resetAuthorPaging() {
  authorPaging = {id: '', name: '', cursor: '', hasMore: false};
  const btn = $('loadMoreAuthorBtn');
  if (btn) btn.classList.add('hidden');
}

function updateAuthorPagingButton() {
  const btn = $('loadMoreAuthorBtn');
  if (!btn) return;
  btn.classList.toggle('hidden', !authorPaging.id || !authorPaging.hasMore || !authorPaging.cursor);
}

function setLoading(on) {
  $('main')?.classList.toggle('loading', on);
}

function openLibraryReader() {
  showView('library');
}

function showView(view) {
  activeView = view;
  $('discoverView').classList.toggle('hidden', view !== 'discover');
  $('subscriptionsView').classList.toggle('hidden', view !== 'subscriptions');
  $('libraryView').classList.toggle('hidden', view !== 'library');
  $('discoverBottom').classList.toggle('hidden', view !== 'discover');
  $('subscriptionsBottom').classList.toggle('hidden', view !== 'subscriptions');
  $('libraryBottom').classList.toggle('hidden', view !== 'library');
  $('discoverTab').classList.toggle('active', view === 'discover');
  $('subscriptionsTab').classList.toggle('active', view === 'subscriptions');
  $('libraryTab').classList.toggle('active', view === 'library');
  if (view === 'subscriptions') refreshSubscriptionState();
  if (view === 'library' && !libraryPosts.length) loadLibrary();
}

function isSubscribed(authorId) {
  return Boolean(authorId && subscriptionAuthorIds.has(authorId));
}

function discoverFiltered() {
  const q = $('localSearch').value.trim().toLowerCase();
  return q ? discoverItems.filter(x => (x.title + ' ' + x.author).toLowerCase().includes(q)) : discoverItems;
}

function renderDiscover() {
  const f = discoverFiltered();
  const pages = Math.max(1, Math.ceil(f.length / DISCOVER_PAGE_SIZE));
  discoverPage = Math.min(discoverPage, pages);
  const slice = f.slice((discoverPage - 1) * DISCOVER_PAGE_SIZE, discoverPage * DISCOVER_PAGE_SIZE);
  $('sourceTitle').textContent = discoverSourceLabel;
  $('discoverCount').textContent = `${f.length} 篇 · 已选 ${selectedPosts.size}`;
  $('discoverPageText').textContent = `${discoverPage} / ${pages}`;
  const g = $('discoverGrid');
  g.innerHTML = '';

  for (const x of slice) {
    const c = document.createElement('article');
    c.className = 'card ' + (selectedPosts.has(x.note_id) ? 'sel' : '');
    const subscribed = isSubscribed(x.author_id);
    c.innerHTML = `
      <input class="selectbox" type="checkbox" ${selectedPosts.has(x.note_id) ? 'checked' : ''}>
      ${x.cover_url ? `<img class="cover" loading="lazy" src="${x.cover_url}">` : '<div class="placeholder">无封面</div>'}
      <div class="card-body">
        <div class="note-title"></div>
        <div class="author-row">
          <button class="author" ${x.author_id ? '' : 'disabled'}></button>
          ${x.author_id ? `<button class="subscribe-btn ${subscribed ? 'subscribed' : ''}">${subscribed ? '已订阅' : '订阅'}</button>` : ''}
        </div>
        <div class="meta"><span class="badge">待下载</span><a class="link" target="_blank" rel="noreferrer">原帖 ↗</a></div>
      </div>`;
    c.querySelector('.note-title').textContent = x.title;
    c.querySelector('.author').textContent = '@' + x.author;
    c.querySelector('.link').href = x.canonical_url;
    c.querySelector('.selectbox').onchange = e => togglePost(x.note_id, e.target.checked);
    c.querySelector('.author').onclick = e => { e.stopPropagation(); if (x.author_id) loadAuthor(x.author_id, x.author); };
    const subBtn = c.querySelector('.subscribe-btn');
    if (subBtn) subBtn.onclick = e => { e.stopPropagation(); toggleSubscription(x.author_id, x.author); };
    c.onclick = e => {
      if (e.target.closest('a,button,input')) return;
      togglePost(x.note_id, !selectedPosts.has(x.note_id));
    };
    g.appendChild(c);
  }
  $('discoverSelectedCount').textContent = selectedPosts.size;
}

function togglePost(id, on) {
  on ? selectedPosts.add(id) : selectedPosts.delete(id);
  renderDiscover();
}
function selectDiscoverFiltered() { for (const x of discoverFiltered()) selectedPosts.add(x.note_id); renderDiscover(); }
function clearDiscoverSelected() { selectedPosts.clear(); renderDiscover(); }
function discoverPrevPage() { if (discoverPage > 1) { discoverPage--; renderDiscover(); window.scrollTo({top: 0, behavior: 'smooth'}); } }
function discoverNextPage() {
  const pages = Math.max(1, Math.ceil(discoverFiltered().length / DISCOVER_PAGE_SIZE));
  if (discoverPage < pages) { discoverPage++; renderDiscover(); window.scrollTo({top: 0, behavior: 'smooth'}); }
}

async function resolveShareLink() {
  const text = $('shareLinkInput').value.trim();
  if (!text) return toast('请粘贴小红书分享文案或链接');
  try {
    setLoading(true);
    resetAuthorPaging();
    $('discoverStatus').textContent = '正在解析分享链接……';
    const j = await api('/api/resolve-link', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text}),
    });
    discoverItems = [j.item];
    selectedPosts = new Set([j.item.note_id]);
    discoverPage = 1;
    discoverSourceLabel = '快速保存';
    $('localSearch').value = '';
    $('discoverStatus').textContent = '解析完成，已自动选中这篇作品；确认后可直接下载。';
    renderDiscover();
  } catch (e) {
    $('discoverStatus').textContent = '分享链接解析失败：' + e.message;
    toast(e.message);
  } finally { setLoading(false); }
}

async function loadFavorites(force = false) {
  try {
    setLoading(true);
    resetAuthorPaging();
    $('discoverStatus').textContent = '正在读取收藏列表……';
    const j = await api('/api/favorites' + (force ? '?refresh=1' : ''));
    discoverItems = j.items;
    discoverPage = 1;
    discoverSourceLabel = '我的收藏';
    $('localSearch').value = '';
    $('discoverStatus').textContent = j.cached ? `已从本地缓存加载 ${discoverItems.length} 篇收藏${j.fetched_at ? '（上次同步 ' + j.fetched_at + '）' : ''}。需要最新内容时点“同步收藏”。` : `收藏同步完成：${discoverItems.length} 篇。可勾选、筛选、点击作者名或订阅作者。`;
    renderDiscover();
  } catch (e) {
    $('discoverStatus').textContent = '读取失败：' + e.message;
    toast(e.message);
  } finally { setLoading(false); }
}

async function loadAuthor(id, name) {
  try {
    setLoading(true);
    resetAuthorPaging();
    $('discoverStatus').textContent = `正在加载 @${name} 的最近作品……`;
    const j = await api('/api/author?user_id=' + encodeURIComponent(id));
    discoverItems = j.items;
    discoverPage = 1;
    discoverSourceLabel = '作者：@' + name;
    $('localSearch').value = '';
    authorPaging = {id, name, cursor: j.cursor || '', hasMore: Boolean(j.has_more)};
    updateAuthorPagingButton();
    $('discoverStatus').textContent = `已加载 @${name} 最近 ${discoverItems.length} 篇。需要更早作品时再手动继续加载。`;
    renderDiscover();
  } catch (e) {
    $('discoverStatus').textContent = '作者主页读取失败：' + e.message;
    toast(e.message);
  } finally { setLoading(false); }
}

async function loadAuthorManual() {
  const id = $('authorInput').value.trim();
  if (!id) return toast('请输入作者主页 URL 或 user_id');
  return loadAuthor(id, id);
}

async function loadMoreAuthor() {
  if (!authorPaging.id || !authorPaging.hasMore || !authorPaging.cursor) return;
  try {
    setLoading(true);
    $('discoverStatus').textContent = `正在继续加载 @${authorPaging.name} 的更早作品……`;
    const j = await api('/api/author?user_id=' + encodeURIComponent(authorPaging.id) + '&cursor=' + encodeURIComponent(authorPaging.cursor));
    const seen = new Set(discoverItems.map(x => x.note_id));
    for (const item of j.items || []) if (!seen.has(item.note_id)) discoverItems.push(item);
    authorPaging.cursor = j.cursor || '';
    authorPaging.hasMore = Boolean(j.has_more);
    updateAuthorPagingButton();
    $('discoverStatus').textContent = `当前已加载 @${authorPaging.name} ${discoverItems.length} 篇。${authorPaging.hasMore ? '可继续手动加载。' : '已到当前可读取范围末尾。'}`;
    renderDiscover();
  } catch (e) {
    $('discoverStatus').textContent = '继续加载失败：' + e.message;
    toast(e.message);
  } finally { setLoading(false); }
}

async function globalSearch() {
  const q = $('globalSearch').value.trim();
  if (!q) return toast('请输入平台搜索关键词');
  resetAuthorPaging();
  try {
    setLoading(true);
    $('discoverStatus').textContent = `正在平台搜索“${q}”……`;
    const j = await api('/api/search?q=' + encodeURIComponent(q));
    discoverItems = j.items;
    discoverPage = 1;
    discoverSourceLabel = '平台搜索：' + q;
    $('localSearch').value = '';
    $('discoverStatus').textContent = `搜索得到 ${discoverItems.length} 篇。`;
    renderDiscover();
  } catch (e) {
    $('discoverStatus').textContent = '平台搜索失败：' + e.message;
    toast(e.message);
  } finally { setLoading(false); }
}

async function toggleSubscription(userId, name) {
  if (!userId) return;
  const subscribed = isSubscribed(userId);
  if (subscribed && !confirm(`取消订阅 @${name}？\n已发现但尚未处理的更新记录也会一起删除。`)) return;
  try {
    setLoading(true);
    const path = subscribed ? '/api/subscriptions/unsubscribe' : '/api/subscriptions/subscribe';
    const payload = subscribed ? {user_id: userId} : {user_id: userId, name};
    const j = await api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    applySubscriptionData(j);
    toast(subscribed ? `已取消订阅 @${name}` : `已订阅 @${name}。当前作品只作为基线，不计为更新。`);
    renderDiscover();
  } catch (e) { toast(e.message); }
  finally { setLoading(false); }
}

function applySubscriptionData(data) {
  subscriptionData = data || {authors: [], updates: [], author_count: 0, pending_count: 0};
  subscriptionData.authors ||= [];
  subscriptionData.updates ||= [];
  subscriptionAuthorIds = new Set(subscriptionData.authors.map(a => a.user_id));
  selectedUpdates = new Set([...selectedUpdates].filter(id => subscriptionData.updates.some(x => x.note_id === id)));
  const count = subscriptionData.pending_count || 0;
  $('subscriptionBadge').textContent = count;
  $('subscriptionBadge').classList.toggle('hidden', count === 0);
  $('subscriptionSummary').textContent = `${subscriptionData.author_count || 0} 位作者 · ${count} 篇待处理更新`;
  $('downloadAllUpdatesBtn').disabled = count === 0;
  renderSubscriptionBanner();
  renderSubscriptionAuthors();
  syncSubscriptionAuthorFilter();
  renderSubscriptionUpdates();
  renderDiscover();
}

function renderSubscriptionBanner() {
  const count = subscriptionData.pending_count || 0;
  const box = $('subscriptionBanner');
  if (!count) { box.classList.add('hidden'); return; }
  const updated = subscriptionData.authors.filter(a => a.pending_count > 0);
  const parts = updated.slice(0, 4).map(a => `@${a.name} ${a.pending_count} 篇`);
  const extra = updated.length > 4 ? `，另 ${updated.length - 4} 位作者` : '';
  $('subscriptionBannerTitle').textContent = `订阅更新：共 ${count} 篇`;
  $('subscriptionBannerText').textContent = parts.join('，') + extra;
  box.classList.remove('hidden');
}

function renderSubscriptionAuthors() {
  const g = $('subscriptionAuthors');
  g.innerHTML = '';
  if (!subscriptionData.authors.length) {
    g.innerHTML = '<div class="empty-state">还没有订阅作者。可在“发现 / 下载”的任意作品卡片上点击“订阅”。</div>';
    return;
  }
  for (const a of subscriptionData.authors) {
    const row = document.createElement('div');
    row.className = 'subscription-author-row';
    row.innerHTML = `
      <div class="subscription-author-main">
        <strong></strong>
        <div class="muted submeta"></div>
        <div class="suberror"></div>
      </div>
      <span class="badge update-badge">${a.pending_count} 篇更新</span>
      <button class="btn view-updates">查看更新</button>
      <button class="btn download-author">下载更新</button>
      <button class="btn handled-author">标记已处理</button>
      <button class="btn danger unsubscribe-author">取消订阅</button>`;
    row.querySelector('strong').textContent = '@' + a.name;
    row.querySelector('.submeta').textContent = a.last_checked_at ? `上次检查：${a.last_checked_at}` : '尚未检查';
    const err = row.querySelector('.suberror');
    err.textContent = a.last_error || '';
    err.classList.toggle('hidden', !a.last_error);
    row.querySelector('.view-updates').onclick = () => {
      $('subscriptionAuthorFilter').value = a.user_id;
      renderSubscriptionUpdates();
      document.querySelector('.subscription-update-toolbar')?.scrollIntoView({behavior: 'smooth'});
    };
    row.querySelector('.download-author').disabled = a.pending_count === 0;
    row.querySelector('.download-author').onclick = () => downloadSubscriptionIds(subscriptionData.updates.filter(x => x.author_id === a.user_id).map(x => x.note_id));
    row.querySelector('.handled-author').disabled = a.pending_count === 0;
    row.querySelector('.handled-author').onclick = () => markAuthorUpdatesHandled(a.user_id, a.name);
    row.querySelector('.unsubscribe-author').onclick = () => toggleSubscription(a.user_id, a.name);
    g.appendChild(row);
  }
}

function syncSubscriptionAuthorFilter() {
  const sel = $('subscriptionAuthorFilter');
  const old = sel.value;
  sel.innerHTML = '<option value="">全部作者</option>';
  for (const a of subscriptionData.authors) {
    const o = document.createElement('option');
    o.value = a.user_id;
    o.textContent = `${a.name}（${a.pending_count}篇更新）`;
    sel.appendChild(o);
  }
  if ([...sel.options].some(o => o.value === old)) sel.value = old;
}

function filteredSubscriptionUpdates() {
  const authorId = $('subscriptionAuthorFilter').value;
  const q = $('subscriptionSearch').value.trim().toLowerCase();
  return subscriptionData.updates.filter(x => (!authorId || x.author_id === authorId) && (!q || (x.title + ' ' + x.author).toLowerCase().includes(q)));
}

function renderSubscriptionUpdates() {
  const items = filteredSubscriptionUpdates();
  $('updateSelectedCount').textContent = selectedUpdates.size;
  const g = $('subscriptionGrid');
  g.innerHTML = '';
  if (!items.length) {
    g.innerHTML = '<div class="empty-state">当前筛选范围没有待处理更新。</div>';
    return;
  }
  for (const x of items) {
    const c = document.createElement('article');
    c.className = 'card ' + (selectedUpdates.has(x.note_id) ? 'sel' : '');
    c.innerHTML = `
      <input class="selectbox" type="checkbox" ${selectedUpdates.has(x.note_id) ? 'checked' : ''}>
      ${x.cover_url ? `<img class="cover" loading="lazy" src="${x.cover_url}">` : '<div class="placeholder">无封面</div>'}
      <div class="card-body"><div class="note-title"></div><div class="author-row"><span class="author-static"></span></div><div class="meta"><span class="badge new-badge">新作品</span><a class="link" target="_blank" rel="noreferrer">原帖 ↗</a></div></div>`;
    c.querySelector('.note-title').textContent = x.title;
    c.querySelector('.author-static').textContent = '@' + x.author;
    c.querySelector('.link').href = x.canonical_url;
    c.querySelector('.selectbox').onchange = e => toggleUpdate(x.note_id, e.target.checked);
    c.onclick = e => { if (e.target.closest('a,input')) return; toggleUpdate(x.note_id, !selectedUpdates.has(x.note_id)); };
    g.appendChild(c);
  }
}

function toggleUpdate(id, on) {
  on ? selectedUpdates.add(id) : selectedUpdates.delete(id);
  renderSubscriptionUpdates();
}
function selectAllFilteredUpdates() { for (const x of filteredSubscriptionUpdates()) selectedUpdates.add(x.note_id); renderSubscriptionUpdates(); }
function clearSelectedUpdates() { selectedUpdates.clear(); renderSubscriptionUpdates(); }

async function refreshSubscriptionState() {
  try {
    const j = await api('/api/subscription-check');
    applySubscriptionData(j.subscriptions);
    if (j.status === 'running') {
      subscriptionPolling = true;
      $('subscriptionStatus').textContent = `正在检查订阅更新：${j.done || 0}/${j.total || 0} 位作者……`;
      $('checkSubscriptionsBtn').disabled = true;
      setTimeout(refreshSubscriptionState, 900);
    } else {
      subscriptionPolling = false;
      $('checkSubscriptionsBtn').disabled = false;
      if (j.status === 'failed') $('subscriptionStatus').textContent = '订阅检查失败：' + (j.error || '未知错误');
      else if (j.status === 'completed') $('subscriptionStatus').textContent = `订阅检查完成：${j.subscriptions.pending_count || 0} 篇待处理更新。`;
      else $('subscriptionStatus').textContent = '订阅作者会在启动时自动检查更新。';
    }
  } catch (e) {
    $('subscriptionStatus').textContent = '订阅状态读取失败：' + e.message;
  }
}

async function checkSubscriptions() {
  try {
    $('checkSubscriptionsBtn').disabled = true;
    await api('/api/subscriptions/check', {method: 'POST'});
    $('subscriptionStatus').textContent = '已开始检查订阅作者……';
    refreshSubscriptionState();
  } catch (e) {
    $('checkSubscriptionsBtn').disabled = false;
    toast(e.message);
  }
}

async function markAllUpdatesHandled() {
  if (!(subscriptionData.pending_count || 0)) return;
  if (!confirm(`确认把全部 ${subscriptionData.pending_count} 篇订阅更新标记为已处理？不会删除本地图片或取消订阅。`)) return;
  try {
    const j = await api('/api/subscriptions/handled', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({all: true})});
    applySubscriptionData(j);
    selectedUpdates.clear();
    toast(`已标记 ${j.handled} 篇为已处理`);
  } catch (e) { toast(e.message); }
}

async function markAuthorUpdatesHandled(userId, name) {
  if (!confirm(`把 @${name} 当前全部更新标记为已处理？`)) return;
  try {
    const j = await api('/api/subscriptions/handled', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_id: userId})});
    applySubscriptionData(j);
    toast(`已标记 @${name} 的 ${j.handled} 篇更新`);
  } catch (e) { toast(e.message); }
}

async function startDownloadJob(noteIds, {ackUpdates = false, mode = 'discover'} = {}) {
  if (!noteIds.length) return toast('没有可下载的帖子');
  try {
    currentJobMode = mode;
    if (mode === 'discover') $('downloadBtn').disabled = true;
    else $('downloadSelectedUpdatesBtn').disabled = true;
    const j = await api('/api/download', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({note_ids: noteIds, workers: 2, ack_subscription_updates: ackUpdates})
    });
    currentJob = j.job_id;
    if (mode === 'discover') $('downloadProgress').classList.remove('hidden');
    else $('subscriptionProgress').classList.remove('hidden');
    pollJob();
  } catch (e) {
    toast(e.message);
    $('downloadBtn').disabled = false;
    $('downloadSelectedUpdatesBtn').disabled = false;
  }
}

async function downloadSelected() {
  if (!selectedPosts.size) return toast('请先勾选要下载的帖子');
  if (!confirm(`确认下载已选的 ${selectedPosts.size} 篇帖子中的图片？`)) return;
  return startDownloadJob([...selectedPosts], {mode: 'discover'});
}

async function downloadSelectedUpdates() {
  if (!selectedUpdates.size) return toast('请先选择订阅更新');
  if (!confirm(`下载已选 ${selectedUpdates.size} 篇更新？成功处理的更新会自动从提醒中移除。`)) return;
  return downloadSubscriptionIds([...selectedUpdates]);
}

async function downloadSubscriptionIds(ids) {
  if (!ids.length) return toast('当前没有可下载的订阅更新');
  if (activeView !== 'subscriptions') showView('subscriptions');
  return startDownloadJob(ids, {ackUpdates: true, mode: 'subscription'});
}

async function downloadAllSubscriptionUpdates() {
  const ids = subscriptionData.updates.map(x => x.note_id);
  if (!ids.length) return toast('当前没有订阅更新');
  if (!confirm(`一键下载全部 ${ids.length} 篇订阅更新？成功处理的更新会自动从提醒中移除。`)) return;
  return downloadSubscriptionIds(ids);
}

async function pollJob() {
  if (!currentJob) return;
  try {
    const j = await api('/api/job?id=' + encodeURIComponent(currentJob));
    const pct = j.total ? Math.round(j.done * 100 / j.total) : 0;
    const isSub = currentJobMode === 'subscription';
    $(isSub ? 'subscriptionBar' : 'downloadBar').style.width = pct + '%';
    $(isSub ? 'subscriptionJobText' : 'jobText').textContent = `下载 ${j.done}/${j.total} (${pct}%)`;
    if (j.status === 'completed') {
      const ok = j.results.filter(x => x.status === 'ok').length;
      const partial = j.results.filter(x => x.status === 'partial').length;
      const fail = j.results.filter(x => x.status === 'failed').length;
      $(isSub ? 'subscriptionJobText' : 'jobText').textContent = `完成：成功 ${ok}，部分成功 ${partial}，失败 ${fail}`;
      $('downloadBtn').disabled = false;
      $('downloadSelectedUpdatesBtn').disabled = false;
      toast(`下载完成：成功 ${ok}，部分成功 ${partial}，失败 ${fail}`);
      currentJob = null;
      if (isSub) { selectedUpdates.clear(); await refreshSubscriptionState(); }
      if (activeView === 'library') loadLibrary();
      return;
    }
    if (j.status === 'failed') {
      toast(j.error || '下载任务失败');
      $('downloadBtn').disabled = false;
      $('downloadSelectedUpdatesBtn').disabled = false;
      currentJob = null;
      return;
    }
    setTimeout(pollJob, 800);
  } catch (e) {
    toast(e.message);
    $('downloadBtn').disabled = false;
    $('downloadSelectedUpdatesBtn').disabled = false;
    currentJob = null;
  }
}

function applyLibraryPayload(j) {
  libraryPosts = j.posts || [];
  libraryAuthors = j.authors || [];
  libraryRoot = j.root || '';
  syncAuthorFilter();
  selectedImages = new Set([...selectedImages].filter(key => libraryPosts.some(p => p.images.some(i => i.key === key))));

  const postCount = Number(j.post_count || 0);
  const imageCount = Number(j.image_count || 0);
  const badge = $('libraryBadge');
  badge.textContent = postCount > 99 ? '99+' : String(postCount);
  badge.classList.toggle('hidden', postCount <= 0);

  const banner = $('libraryBanner');
  if (postCount > 0) {
    banner.classList.remove('hidden');
    $('libraryBannerTitle').textContent = `检测到 ${postCount} 篇已下载内容`;
    $('libraryBannerText').textContent = `${imageCount} 张本地图片 · 可直接进入阅读器，不需要重新下载。`;
  } else {
    banner.classList.add('hidden');
  }

  $('libraryStatus').textContent = `已扫描 ${postCount} 篇文章、${imageCount} 张本地图片。${libraryRoot ? ' 当前仓库：' + libraryRoot : ''}`;
  renderLibrary();
}

async function loadLibrary({silent = false} = {}) {
  try {
    if (!silent) setLoading(true);
    $('libraryStatus').textContent = '正在扫描 downloads 目录……';
    const j = await api('/api/library');
    applyLibraryPayload(j);
  } catch (e) {
    $('libraryStatus').textContent = '图库扫描失败：' + e.message;
    if (!silent) toast(e.message);
  } finally { if (!silent) setLoading(false); }
}

function syncAuthorFilter() {
  const sel = $('authorFilter'), old = sel.value;
  sel.innerHTML = '<option value="">全部作者</option>';
  for (const a of libraryAuthors) {
    const o = document.createElement('option');
    o.value = a.author;
    o.textContent = `${a.author}（${a.posts}篇 / ${a.images}图）`;
    sel.appendChild(o);
  }
  if ([...sel.options].some(o => o.value === old)) sel.value = old;
}
function filteredLibraryPosts() {
  const author = $('authorFilter').value, q = $('librarySearch').value.trim().toLowerCase();
  return libraryPosts.filter(p => (!author || p.author === author) && (!q || (p.title + ' ' + p.author).toLowerCase().includes(q)));
}
function currentLibraryImageKeys() { return filteredLibraryPosts().flatMap(p => p.images.map(i => i.key)); }
function setLibraryMode(mode) {
  libraryMode = mode;
  $('articleModeBtn').classList.toggle('active', mode === 'articles');
  $('imageModeBtn').classList.toggle('active', mode === 'images');
  renderLibrary();
}
function renderLibrary() {
  const posts = filteredLibraryPosts();
  const images = posts.flatMap(p => p.images.map(i => ({post: p, image: i})));
  $('libraryCount').textContent = libraryMode === 'articles' ? `${posts.length} 篇文章 · ${images.length} 张图片` : `${images.length} 张图片 · 来自 ${posts.length} 篇文章`;
  $('imageSelectedCount').textContent = selectedImages.size;
  const g = $('libraryGrid');
  g.innerHTML = '';
  g.className = 'grid ' + (libraryMode === 'articles' ? 'post-grid' : 'flat-grid');
  if (!posts.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.textContent = libraryRoot
      ? `当前没有检测到可阅读的本地图片。正在扫描：${libraryRoot}。如果旧 v0.4 仓库在其他目录，请从旧安装目录运行升级助手，或把新版程序覆盖到旧安装目录后重新扫描。`
      : '当前没有检测到可阅读的本地图片。';
    g.appendChild(empty);
    return;
  }
  if (libraryMode === 'articles') for (const p of posts) g.appendChild(articleCard(p));
  else for (const pair of images) g.appendChild(flatImageCard(pair.post, pair.image));
}
function articleCard(p) {
  const c = document.createElement('article');
  const allSelected = p.images.length && p.images.every(i => selectedImages.has(i.key));
  c.className = 'card ' + (allSelected ? 'sel' : '');
  c.innerHTML = `<input class="selectbox" type="checkbox" ${allSelected ? 'checked' : ''}><img class="cover" loading="lazy" src="${p.cover_url}"><div class="card-body"><div class="note-title"></div><div class="author"></div><div class="meta"><span class="badge">${p.image_count} 张</span><a class="link" target="_blank" rel="noreferrer">原帖 ↗</a></div></div>`;
  c.querySelector('.note-title').textContent = p.title;
  c.querySelector('.author').textContent = '@' + p.author;
  c.querySelector('.link').href = p.canonical_url;
  c.querySelector('.selectbox').onchange = e => { e.stopPropagation(); for (const i of p.images) toggleImage(i.key, e.target.checked, false); renderLibrary(); };
  c.querySelector('.author').onclick = e => { e.stopPropagation(); $('authorFilter').value = p.author; renderLibrary(); };
  c.onclick = e => { if (e.target.closest('a,input')) return; openArticle(p.note_id); };
  return c;
}
function flatImageCard(p, i) {
  const c = document.createElement('article');
  c.className = 'image-card ' + (selectedImages.has(i.key) ? 'sel' : '');
  c.innerHTML = `<input class="selectbox" type="checkbox" ${selectedImages.has(i.key) ? 'checked' : ''}><img loading="lazy" src="${i.url}"><div class="image-caption"></div>`;
  c.querySelector('.image-caption').textContent = `${p.author} · ${p.title} · ${i.filename}`;
  c.querySelector('.selectbox').onchange = e => { e.stopPropagation(); toggleImage(i.key, e.target.checked); };
  c.onclick = e => { if (e.target.closest('input')) return; openLightbox(p.note_id, p.images.findIndex(x => x.key === i.key)); };
  return c;
}
function toggleImage(key, on, rerender = true) { on ? selectedImages.add(key) : selectedImages.delete(key); $('imageSelectedCount').textContent = selectedImages.size; if (rerender) renderLibrary(); }
function selectLibraryFiltered() { for (const key of currentLibraryImageKeys()) selectedImages.add(key); renderLibrary(); if (currentArticle) renderArticleImages(); }
function clearImageSelection() { selectedImages.clear(); renderLibrary(); if (currentArticle) renderArticleImages(); }
function openArticle(noteId) {
  currentArticle = libraryPosts.find(p => p.note_id === noteId) || null;
  if (!currentArticle) return;
  $('articleTitle').textContent = currentArticle.title;
  $('articleMeta').textContent = `@${currentArticle.author} · ${currentArticle.image_count} 张图片${currentArticle.saved_at ? ' · ' + currentArticle.saved_at : ''}`;
  renderArticleImages();
  $('articleModal').classList.remove('hidden');
}
function renderArticleImages() {
  if (!currentArticle) return;
  const g = $('articleImages'); g.innerHTML = '';
  currentArticle.images.forEach((i, idx) => {
    const d = document.createElement('div');
    d.className = 'detail-image ' + (selectedImages.has(i.key) ? 'sel' : '');
    d.innerHTML = `<input class="selectbox" type="checkbox" ${selectedImages.has(i.key) ? 'checked' : ''}><img loading="lazy" src="${i.url}"><div class="detail-name"></div>`;
    d.querySelector('.detail-name').textContent = i.filename;
    d.querySelector('.selectbox').onchange = e => { e.stopPropagation(); toggleImage(i.key, e.target.checked, false); renderArticleImages(); renderLibrary(); };
    d.onclick = e => { if (e.target.closest('input')) return; openLightbox(currentArticle.note_id, idx); };
    g.appendChild(d);
  });
}
function toggleCurrentArticle(on) { if (!currentArticle) return; for (const i of currentArticle.images) toggleImage(i.key, on, false); renderArticleImages(); renderLibrary(); }
function closeArticle() { $('articleModal').classList.add('hidden'); currentArticle = null; }
function modalBackdrop(e, id) { if (e.target.id === id) closeArticle(); }
function openLightbox(noteId, index) {
  const p = libraryPosts.find(p => p.note_id === noteId);
  if (!p || !p.images.length) return;
  lightImages = p.images.map(i => ({url: i.url, label: `${p.author} · ${p.title} · ${i.filename}`}));
  lightIndex = Math.max(0, Math.min(index, lightImages.length - 1));
  renderLightbox(); $('lightbox').classList.remove('hidden');
}
function renderLightbox() { const x = lightImages[lightIndex]; if (!x) return; $('lightImage').src = x.url; $('lightCaption').textContent = `${lightIndex + 1} / ${lightImages.length} · ${x.label}`; }
function closeLightbox() { $('lightbox').classList.add('hidden'); $('lightImage').src = ''; lightImages = []; }
function lightPrev(e) { e?.stopPropagation(); if (!lightImages.length) return; lightIndex = (lightIndex - 1 + lightImages.length) % lightImages.length; renderLightbox(); }
function lightNext(e) { e?.stopPropagation(); if (!lightImages.length) return; lightIndex = (lightIndex + 1) % lightImages.length; renderLightbox(); }
function lightboxBackdrop(e) { if (e.target.id === 'lightbox') closeLightbox(); }
document.addEventListener('keydown', e => {
  if (!$('lightbox').classList.contains('hidden')) {
    if (e.key === 'ArrowLeft') lightPrev(); else if (e.key === 'ArrowRight') lightNext(); else if (e.key === 'Escape') closeLightbox();
    return;
  }
  if (!$('articleModal').classList.contains('hidden') && e.key === 'Escape') closeArticle();
});

async function pickDestination() {
  try { const j = await api('/api/pick-folder', {method: 'POST'}); if (j.path) $('destinationInput').value = j.path; }
  catch (e) { toast(e.message); }
}
async function transferImages(action) {
  if (!selectedImages.size) return toast('请先选择图片');
  let dest = $('destinationInput').value.trim();
  if (!dest) { await pickDestination(); dest = $('destinationInput').value.trim(); if (!dest) return; }
  const word = action === 'move' ? '移动' : '复制';
  if (action === 'move' && !confirm(`确认把选中的 ${selectedImages.size} 张图片移动到：\n${dest}\n\n移动后原 downloads 中的对应图片会消失。`)) return;
  try {
    setLoading(true);
    const j = await api('/api/library-transfer', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({image_keys: [...selectedImages], destination: dest, action, preserve_post_folders: $('preserveFolders').checked})});
    toast(`${word}完成：成功 ${j.completed}，失败 ${j.failed}\n${j.destination}`);
    if (action === 'move') selectedImages.clear();
    await loadLibrary();
  } catch (e) { toast(e.message); }
  finally { setLoading(false); }
}
async function openOutput() { try { await api('/api/open-output', {method: 'POST'}); } catch (e) { toast(e.message); } }

showView('discover');
refreshSubscriptionState();
loadLibrary({silent: true});
loadFavorites();
