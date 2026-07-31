/* app.js — index.html's behaviour layer.

   Split out of a 268 KB single-file index.html. Loaded as a classic script at
   the end of <body> (NOT type="module"): the markup uses inline on* handlers
   throughout, which resolve against globals, and a module's scope would break
   every one of them. The pre-paint theme script stays inline in index.html —
   it has to run before first paint to avoid flashing the default palette.

   Cached independently of the HTML, same reasoning as app.css. */

  const esc=s=>(s||'').replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));
  const escA=s=>esc(s).replace(/'/g,'&#39;');
  // A 401 from any gated /api/* route means the *same shared session* just
  // died — a real homepage load fires 15+ of these (j() for rails/stats,
  // jp() for playback) in parallel, so without this they used to come back
  // one-by-one as {ok:false}, indistinguishable from an empty rail or a
  // generic network hiccup: every caller silently rendered a blank/partial
  // section (or, in playItem's case, parked "Not logged in" in the video
  // overlay like a codec error) with no sign anything was actually wrong.
  // noteAuthFailure() re-syncs real auth state once (debounced, since a
  // single dead session can trigger this from a dozen concurrent requests
  // at once) via the existing checkAuth() -> lockSite()/showSignedOutToast()
  // path, so a genuine expiry now always surfaces as the same clear
  // "you've been signed out, sign back in" UX instead of cryptic silence.
  // A merely-transient 401 (see has_valid_session()'s retry) just means
  // checkAuth() re-confirms the session is fine and nothing visibly changes.
  let _authRecheckPending = false;
  function noteAuthFailure(){
    if(_authRecheckPending) return;
    _authRecheckPending = true;
    setTimeout(()=>{ _authRecheckPending = false; checkAuth(); }, 50);
  }
  async function j(u){
    try{
      const r=await fetch(u);
      if(r.status===401) noteAuthFailure();
      return await r.json();
    }catch(e){ return {ok:false}; }
  }
  // POST/GET JSON helper for the session-authenticated Phase 2b playback
  // routes (2a's login used bare inline fetches; this is the proper helper
  // the plan called for). credentials:'same-origin' is explicit rather than
  // relying on the fetch default, since these calls carry the session cookie.
  // Tags the parsed body with the real HTTP status (`_status`) so callers
  // that need to tell "not logged in" apart from "logged in, but this
  // specific thing degraded" can — see playItem()'s use of _status===401.
  async function jp(u, body, method){
    try{
      const opts={method: method || (body!==undefined ? 'POST' : 'GET'), credentials:'same-origin'};
      if(body!==undefined){ opts.headers={'Content-Type':'application/json'}; opts.body=JSON.stringify(body); }
      const r=await fetch(u, opts);
      if(r.status===401) noteAuthFailure();
      const d=await r.json();
      if(d && typeof d==='object') d._status=r.status;
      return d;
    }catch(e){ return {ok:false}; }
  }
  const sum=t=>Object.entries(t).reduce((a,[,v])=>a+v,0);

  // ── View Transitions: poster → detail morph ───────────────────────────────
  // The tapped poster physically grows into the detail modal's poster (and
  // shrinks back on close) via a shared view-transition-name. Progressive
  // enhancement: without the API (or under reduced-motion) it's an instant
  // swap exactly as before. Only ever ONE element carries the 'film-poster'
  // name in any captured state — that's what keeps the transition valid.
  const prefersReduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
  const canVT = () => typeof document.startViewTransition === 'function' && !prefersReduced();
  const modalPosterImg = () => document.querySelector('#lbox-poster img');
  // OPEN: apply()=render the modal. srcEl is the clicked .poster (may be null).
  function vtOpenPoster(srcEl, apply){
    const srcImg = srcEl && srcEl.querySelector('img');
    if(!canVT()){ apply(); window._morphSrcImg = srcImg || null; return; }
    window._morphSrcImg = srcImg || null;
    if(srcImg) srcImg.style.viewTransitionName = 'film-poster';   // tagged in OLD snapshot
    const vt = document.startViewTransition(() => {
      if(srcImg) srcImg.style.viewTransitionName = '';            // clear so NEW snapshot is unique
      apply();
      const m = modalPosterImg(); if(m) m.style.viewTransitionName = 'film-poster';
    });
    vt.finished.finally(() => { const m = modalPosterImg(); if(m) m.style.viewTransitionName = ''; });
  }
  // CLOSE: morph the modal poster back down onto the source card (if still on
  // screen); otherwise just a plain fade of the modal.
  function vtCloseFilm(removeModal){
    const m = modalPosterImg(), srcImg = window._morphSrcImg;
    if(!canVT() || !m){ removeModal(); window._morphSrcImg = null; return; }
    m.style.viewTransitionName = 'film-poster';                   // tagged in OLD snapshot
    if(srcImg) srcImg.style.viewTransitionName = '';
    const vt = document.startViewTransition(() => {
      m.style.viewTransitionName = '';
      removeModal();
      if(srcImg && document.contains(srcImg)) srcImg.style.viewTransitionName = 'film-poster';
    });
    vt.finished.finally(() => { if(srcImg) srcImg.style.viewTransitionName = ''; window._morphSrcImg = null; });
  }
  // Generic root cross-fade for whole-view swaps (home ↔ catalog).
  function vtRoot(fn){ if(!canVT()){ fn(); return; } document.startViewTransition(fn); }

  // ── theme system ────────────────────────────────────────────────────────
  // Adding a theme = one [data-theme="x"] CSS block (up top) + one entry here.
  // `swatch` is just preview metadata for the picker buttons below (a two-tone
  // gradient hinting at that theme's accent/link colors); it never drives the
  // page itself — only the matching CSS block's custom properties do that.
  const THEMES = [
    {key:'default',  label:'Nocturne', swatch:['#FF2E9A','#35E8FF']},
    {key:'grid',     label:'Grid',     swatch:['#2E9BFF','#6EE7FF']},
    {key:'ember',    label:'Ember',    swatch:['#FF3B5C','#FFAE4B']},
    {key:'phosphor', label:'Phosphor', swatch:['#2BFF9E','#C6FF3A']},
    {key:'sodium',   label:'Sodium',   swatch:['#FF9E3A','#35D6E8']},
    {key:'dusk',     label:'Dusk',     swatch:['#FF9DBE','#8FD9FF']},
  ];
  const THEME_KEY = 'mf-theme';
  function applyTheme(key, opts){
    const t = THEMES.find(t=>t.key===key) || THEMES[0];
    if(t.key === 'default') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', t.key);
    try{ localStorage.setItem(THEME_KEY, t.key); }catch(e){}
    const mc = document.querySelector('meta[name="theme-color"]');
    if(mc) mc.setAttribute('content', getComputedStyle(document.documentElement).getPropertyValue('--bg').trim());
    if(!opts || !opts.silent) renderThemePicker(t.key);
  }
  function renderThemePicker(active){
    const el=document.getElementById('themepicker'); if(!el) return;
    // Update in place when the buttons already exist: a swatch lives inside the
    // appearance popover, and rebuilding innerHTML on click would detach the
    // just-clicked node mid-event — which the outside-click handler would read
    // as a click outside the menu and close the popover. Toggling aria-pressed
    // on the existing buttons keeps the DOM (and the open menu) stable so you
    // can try several themes in a row.
    const existing=el.querySelectorAll('button[data-theme-key]');
    if(existing.length===THEMES.length){
      existing.forEach(b=>b.setAttribute('aria-pressed', b.dataset.themeKey===active ? 'true' : 'false'));
      return;
    }
    el.innerHTML = THEMES.map(t=>{
      const grad = 'background:linear-gradient(135deg,'+t.swatch[0]+','+t.swatch[1]+')';
      return '<button type="button" data-theme-key="'+t.key+'" style="'+grad+'" title="'+escA(t.label)+'" '
        + 'aria-label="'+escA(t.label)+' theme" aria-pressed="'+(t.key===active?'true':'false')+'" '
        + 'onclick="applyTheme(\''+t.key+'\')"></button>';
    }).join('');
  }
  renderThemePicker(document.documentElement.getAttribute('data-theme') || 'default');

  // ── streaming-app redesign: the old 4-door grid is gone as literal tiles —
  // each service now lands wherever it's contextually relevant instead:
  //   watch (Jellyfin)   -> quiet "open in app" link, next to the browse tabs
  //   request (Jellyseerr) -> the Request affordance next to the Popular rail
  //   read (Kavita) + browse (book requests) -> merged into the Books disclosure
  // Still sourced from /api/stats/services (config/services.json) exactly as
  // before — only where/how they render on the page has changed.
  const SERVICES_FALLBACK = [
    {key:'watch', name:'Watch', desc:'Films & series — Jellyfin', url:'https://watch.example.org'},
    {key:'request', name:'Request', desc:'Movies, series & anime — Jellyseerr', url:'https://request.example.org'},
    {key:'read', name:'Read', desc:'Books & comics — Kavita', url:'https://read.example.org'},
    {key:'browse', name:'Request', desc:'Book requests', url:'https://browse.example.org'},
  ];
  function renderServices(list){
    const svc = Array.isArray(list) ? list : [];
    const byKey = {}; svc.forEach(d=>{ byKey[d.key]=d; });
    const jellyfin = byKey.watch, jellyseerr = byKey.request, kavita = byKey.read, bookreq = byKey.browse;

    const al = document.getElementById('applink');
    if(al) al.innerHTML = jellyfin
      ? `On a TV or phone? <a href="${escA(jellyfin.url)}" target="_blank" rel="noopener">Open in the Jellyfin app →</a>`
      : '';

    const rl = document.getElementById('requestlink');
    if(rl && jellyseerr) rl.href = jellyseerr.url;

    // Nocturne Stage 2: was the Books <details> disclosure's #books-body;
    // now two quiet .cutout links straight in the footer (see the HTML
    // comment there). Neither configured -> render nothing, same
    // never-a-visible-error-fragment-in-the-footer instinct as #applink
    // above, just via an empty string instead of a conditional '' assign.
    const books = document.getElementById('footer-books');
    if(books){
      const rows=[];
      if(kavita) rows.push(`<a class="cutout" href="${escA(kavita.url)}" target="_blank" rel="noopener" title="Read — books &amp; comics (Kavita)">Read →</a>`);
      if(bookreq) rows.push(`<a class="cutout" href="${escA(bookreq.url)}" target="_blank" rel="noopener" title="Request books">Request books →</a>`);
      books.innerHTML = rows.join('');
    }
  }
  async function loadServices(){
    renderServices(SERVICES_FALLBACK);
    const d = await j('/api/stats/services');
    if(d && d.ok && Array.isArray(d.services) && d.services.length) renderServices(d.services);
  }
  // NOT called at parse time — /api/stats/services is session-gated, so this
  // runs from startMemberSurfaces(). renderServices(SERVICES_FALLBACK) inside
  // still paints the static door list the moment it does run, so the footer
  // links never wait on the network.

  // ── Nocturne Stage 2 fidelity pass: scrollable hero strip ────────────────
  // /api/watching/hero returns a randomized recently-added + random mix with
  // backdrop art; the old rotating-banner version cross-faded through all of
  // it (up to 8), one at a time, on a timer. This renders all of them
  // simultaneously as static slices in a horizontally-scrollable strip
  // (see .hero's CSS) instead of picking one to show at a time — restored
  // to the backend's own default limit of 8 (an earlier pass had dropped
  // this to 3 to match a fixed 3-slice grid that no longer exists).
  // renderFilmDetail/startPlayback (defined further down, but function
  // declarations hoist) are reused as-is for "More info" / "Play" — same
  // modal, same player, no special-cased hero-only code paths. The rotation
  // timer, prev/next nav, dots, and pause-on-hover machinery are all retired
  // — nothing to advance between when every slice is already in the DOM,
  // just scrolled to.
  window._hero = {items:[]};

  function heroSliceHtml(it, i, isLead){
    const bg = it.backdrop
      ? `/api/watching/poster?id=${encodeURIComponent(it.id)}&tag=${encodeURIComponent(it.backdrop)}&kind=backdrop&h=1000`
      : (it.tag ? `/api/watching/poster?id=${encodeURIComponent(it.id)}&tag=${encodeURIComponent(it.tag)}&h=1000` : '');
    const genres=(it.genres||[]).slice(0,2).join(' · ');
    const eyebrow = isLead ? ('Featured' + (genres?(' · '+genres):'')) : (genres || (it.type==='Series'?'Series':'Film'));
    const metaBits=[];
    if(it.year) metaBits.push(`<span>${esc(String(it.year))}</span>`);
    if(it.runtime_min) metaBits.push(`<span>${esc(it.runtime_min+' min')}</span>`);
    if(it.community!=null) metaBits.push(`<span class="r">★ ${esc((+it.community).toFixed(1))}</span>`);
    // Only the lead slice gets the neon Play shortcut (straight into
    // playback); every other slice gets the plain wire "More info" tier — same
    // detail-modal action the whole slice's own click already does, offered
    // explicitly for anyone tabbing through by keyboard rather than hovering.
    const actionBtn = isLead
      ? `<button type="button" class="btn hero-play" onclick="event.stopPropagation(); heroPlay(${i});">`
        + `<svg viewBox="0 0 24 24" fill="currentColor" width="15" height="15"><path d="M8 5v14l11-7z"/></svg> Play</button>`
      : `<button type="button" class="btn btn--accent" onclick="event.stopPropagation(); heroInfo(${i});">More info</button>`;
    return `<div class="slice${isLead?' lead':''}" role="button" tabindex="0" aria-label="${escA(it.title)}"`
      + ` onclick="heroInfo(${i})" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();heroInfo(${i});}">`
      + (bg?`<div class="bg" style="background-image:url('${bg}')"></div>`:'')
      + `<div class="in">`
      + `<p class="eyebrow neon hero-eyebrow">${esc(eyebrow)}</p>`
      + `<h2>${esc(it.title)}</h2>`
      + (metaBits.length?`<div class="meta num">${metaBits.join('')}</div>`:'')
      + (isLead && it.overview ? `<p class="slice-syn">${esc(it.overview)}</p>` : '')
      + `<div class="go">${actionBtn}</div>`
      + `</div></div>`;
  }
  function renderHero(items){
    window._hero.items = items||[];
    const slidesEl=document.getElementById('hero-slides');
    if(!slidesEl) return;
    if(!window._hero.items.length){
      // graceful empty state — no Jellyfin locally, empty library, or still
      // loading — keeps the compact topbar's brand line from sitting above
      // a jarring blank band.
      slidesEl.innerHTML = `<div class="hero-empty wrap wrap--wide">`
        + `<p class="wordmark wordmark--lg">Media<span class="bar">|</span>Hub</p>`
        + `<h2 class="hero-title">Everything you watch, in one place.</h2>`
        + `<p class="hero-lede">Browse the library below, or sign in to pick up where you left off.</p>`
        + `</div>`;
      return;
    }
    slidesEl.innerHTML = window._hero.items.map((it,i)=>heroSliceHtml(it,i,i===0)).join('');
  }
  function heroPlay(i){
    const it=window._hero.items[i]; if(!it) return;
    window._currentItem=it; // startPlayback() reads this, same as the detail modal's Play button
    startPlayback();
  }
  function heroInfo(i){
    const it=window._hero.items[i]; if(!it) return;
    renderFilmDetail(it);
  }
  async function loadHero(){
    const d=await j('/api/watching/hero?limit=8');
    renderHero((d && d.ok && Array.isArray(d.items)) ? d.items : []);
  }
  // Session-gated endpoint — started by startMemberSurfaces(), not at parse time.

  // ── streaming-app redesign: Continue Watching rail (signed-in only) ─────
  // data-id/data-title/data-resume + delegated click (below), same pattern
  // as #libgrid and the episode picker — avoids hand-quoting title text
  // (which can contain apostrophes) into an inline onclick string.
  function cwCard(it){
    const posterId=it.poster_id||it.id, posterTag=it.poster_tag||it.tag;
    const img=posterTag
      ? `<img src="/api/watching/poster?id=${encodeURIComponent(posterId)}&tag=${encodeURIComponent(posterTag)}" loading="lazy" alt="${esc(it.title)}">`
      : `<div class="noimg">${esc(it.title)}</div>`;
    const pct = it.runtime_ticks ? Math.min(100, Math.round((it.position_ticks||0)/it.runtime_ticks*100)) : 0;
    const isEp = !!it.series;
    const title = isEp ? esc(it.series) : esc(it.title);
    const sub = isEp ? esc(seLabel({season:it.season, episode:it.episode}) + (it.title?' · '+it.title:'')) : '';
    return `<div class="pcard"><div class="poster" tabindex="0" role="button" `
      + `data-id="${escA(it.id)}" data-title="${escA(it.title||'')}" data-resume="${it.position_ticks||0}" `
      + `aria-label="Resume ${escA(it.title||'')}">`
      + `<span class="cw-badge">${pct}%</span>${img}<div class="cw-bar"><i style="width:${pct}%"></i></div></div>`
      + `<div class="pt" title="${esc(it.title)}">${title}</div>`
      + `${sub?`<div class="cw-sub">${sub}</div>`:''}</div>`;
  }
  (function(){
    const grid=document.getElementById('cwgrid'); if(!grid) return;
    const go=p=>{ if(p && p.dataset.id) playItem(p.dataset.id, p.dataset.title, parseInt(p.dataset.resume||'0', 10)); };
    grid.addEventListener('click', e=>go(e.target.closest('.poster')));
    grid.addEventListener('keydown', e=>{
      if(e.key!=='Enter' && e.key!==' ') return;
      const p=e.target.closest('.poster'); if(p){ e.preventDefault(); go(p); }
    });
  })();
  async function loadResume(){
    const sec=document.getElementById('cw'), grid=document.getElementById('cwgrid');
    if(!sec || !grid) return;
    if(!(window._authUser && window._authUser.username)){ sec.classList.remove('on'); grid.innerHTML=''; window._cw=[]; return; }
    const d = await jp('/api/playback/resume');
    const items = (d && d.ok && Array.isArray(d.items)) ? d.items : [];
    window._cw = items; // ⌘K's "Jump to" reads this (see cmdkRenderDefault) - same data, no extra fetch
    if(!items.length){ sec.classList.remove('on'); grid.innerHTML=''; return; }
    sec.classList.add('on');
    grid.innerHTML = items.map(cwCard).join('');
  }

  // "Under the hood" hardware spec — hand-maintained data (config/host_spec.json)
  // served live instead of hardcoded HTML rows, see Stage 5 (S1).
  async function loadHostSpec(){
    const chart=document.getElementById('spec-chart'), rm=document.getElementById('spec-roadmap');
    const d = await j('/api/stats/host-spec');
    if(!d || !d.ok || !Array.isArray(d.rows) || !d.rows.length){
      if(chart) chart.innerHTML='<div class="crow"><span class="cv">spec unavailable</span></div>';
      return;
    }
    if(chart) chart.innerHTML = d.rows.map(r=>
      '<div class="crow"><span class="ck">'+esc(r.label)+'</span><span class="cv">'+esc(r.value)+'</span></div>'
    ).join('');
    if(rm && d.roadmap) rm.innerHTML = '<span class="ck">Roadmap</span>'+esc(d.roadmap);
  }
  // Called by openInstrument(), not at boot — see that function.

  // Nocturne decorative stage (.grid/.floor/.vig, see the CSS
  // block up top) is static — no starfield generator or aurora-parallax
  // listener needed anymore; both were support code for the old aurora/
  // stars layers this reskin replaced, and would otherwise throw on the
  // now-nonexistent #stars/#aurora/#aurora2 elements.

  function sparkline(points){
    if(!points||points.length<2)return;
    const vals=points.map(p=>p.titles), n=vals.length;
    const min=Math.min(...vals), max=Math.max(...vals), rng=(max-min)||1;
    const W=220,H=34,pad=4;
    const pts=vals.map((v,i)=>[ (i/(n-1))*W, H-pad-((v-min)/rng)*(H-2*pad) ]);
    document.getElementById('sparkpath').setAttribute('d','M'+pts.map(p=>p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' L'));
    const last=pts[pts.length-1];
    const dot=document.getElementById('sparkdot'); dot.setAttribute('cx',last[0].toFixed(1)); dot.setAttribute('cy',last[1].toFixed(1));
  }

  // "S02E05" style label; falls back gracefully if either number is missing
  function seLabel(x){
    if(x.season==null && x.episode==null) return '';
    const s=x.season!=null?'S'+String(x.season).padStart(2,'0'):'';
    const e=x.episode!=null?'E'+String(x.episode).padStart(2,'0'):'';
    return (s+e);
  }
  function fmtT(sec){sec=Math.max(0,Math.round(sec||0));const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=sec%60;
    return h?`${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`:`${m}:${String(s).padStart(2,'0')}`;}
  const BADGE={movie:'Film', tv:'TV', anime:'Anime'};

  // ── live: now playing (polled) ─────────────────────────────────────────────
  // Shared per-session field computation for the #nowgrid .npc cards. btnSize
  // ('sm') keeps the hot-join CTA compact inside the card; kept as a param so
  // a larger variant could opt in later without touching this.
  function nowFields(x, btnSize){
    const kind=x.kind||(x.series?'tv':'movie');
    const badge=BADGE[kind]||esc(x.type||'');
    const se=seLabel(x);
    // TV/anime -> "Series  S02E05 · Episode"; movie -> just the title
    let title;
    if(x.series){
      const seHtml = se ? ` <span class="se">${se}</span>` : '';
      title = esc(x.series) + seHtml + ' <span class="tag">·</span> ' + esc(x.title);
    } else {
      title = esc(x.title);
    }
    const meta=esc((x.genres||[]).slice(0,3).join(' · ')||'');
    const pct=Math.round((x.progress||0)*100);
    // Hot-join affordance: never on your own card, and only when there's a
    // real item id to launch into (watching.py's now() docstring on that
    // field). plainTitle mirrors `title` above but as text, not markup -
    // together.py's item_title is stored/displayed as-is. Same "Watch with
    // {name}" CTA either way (mockup's own copy is generic "Join & watch
    // together"; hub's names the actual person, which is more useful and is
    // the CTA this fidelity pass was told to keep as-is).
    const isMe = window._authUser && x.user===window._authUser.username;
    let watchWithBtn='';
    // x.joinable is the server's opt-in gate (watching.py now()): the CTA only
    // appears for people who turned on "let others watch with me" in their
    // privacy settings. Default is off, so a solo watcher is never offered up
    // for hot-join unless they chose to be. together.py re-checks server-side.
    if(!isMe && x.user && x.item_id && x.joinable){
      const plainTitle = x.series ? (x.series+(se?(' '+se):'')+' - '+x.title) : (x.title||'');
      const sizeCls = btnSize==='sm' ? ' btn--sm' : '';
      watchWithBtn = `<button type="button" class="btn${sizeCls} hero-play np-watchwith" `
        + `data-user="${escA(x.user)}" data-item="${escA(x.item_id)}" data-title="${escA(plainTitle)}" `
        + `data-elapsed="${x.elapsed||0}" data-paused="${x.paused?1:0}" data-runtime="${x.runtime?Math.round(x.runtime/60):''}">`
        + `Watch with ${esc(x.user)}</button>`;
    }
    return {kind, badge, title, meta, pct, watchWithBtn};
  }
  async function refreshNow(){
    const now=await j('/api/watching/now');
    // keep paused streams visible (dimmed) so the panel doesn't blink empty
    const sess=(now.ok&&now.sessions)?now.sessions:[];
    // Watch-together hooks - run every tick regardless of whether the panel
    // itself ends up empty below (checkMyRoom() cares about *my own*
    // playback, not the panel's contents; _syncFromNowPlaying no-ops on its
    // own if there's nothing relevant in `sess`).
    _syncFromNowPlaying(sess);
    checkMyRoom();
    const nowEl=document.getElementById('now'), dot=document.getElementById('livedot');
    if(!sess.length){ nowEl.classList.remove('on'); return; }
    nowEl.classList.add('on');
    if(dot) dot.style.display=(now.ok?'inline-block':'none');

    // design-review #3: every session is the same .npc card — no primary/
    // secondary split, no ring. 1 viewer or several, they all read identically.
    document.getElementById('nowgrid').innerHTML=sess.map(x=>{
      const f=nowFields(x, 'sm');
      const pst = x.img_id ? `<div class="np-poster"><img src="/api/watching/poster?id=${encodeURIComponent(x.img_id)}&tag=${encodeURIComponent(x.img_tag||'')}&h=220" loading="lazy" alt=""></div>` : '';
      const timeRow = (x.runtime || f.watchWithBtn)
        ? `<div class="np-row">${x.runtime?`<div class="time"><span>${fmtT(x.elapsed)}</span><span>${fmtT(x.remaining)} left</span></div>`:''}${f.watchWithBtn}</div>`
        : '';
      return `<div class="npc${x.paused?' paused':''}">${pst}<div class="np-body">
        <div class="row"><span class="who">${esc(x.user||'Someone')}</span>
          <span class="badge ${f.kind}">${f.badge}</span></div>
        <div class="ttl">${f.title}</div>
        <div class="meta">${x.paused?'Paused':'Watching'}${f.meta?` · ${f.meta}`:''}</div>
        <div class="bar"><i style="width:${f.pct}%"></i></div>
        ${timeRow}</div></div>`;}).join('');
  }

  // ── Watch-together: hot-join from Now Playing ────────────────────────────
  // "Watch with {name}" — no invite code. together.py's /hot-join either
  // joins an existing room for that person+item, or (if they're watching
  // solo) silently-but-not-invisibly auto-creates one with them as host —
  // see that endpoint's docstring for the full design reasoning. position/
  // paused/runtime below are exactly what this client's own Now Playing
  // poll just rendered on the card that was clicked - same trust level as
  // every other client-supplied position value this feature already
  // accepts (see together.py's CommandBody).
  async function hotJoin(username, itemId, title, elapsedSecs, paused, runtimeMin){
    if(!username || !itemId) return;
    // Leave whatever room this tab was already in first - hot-joining a
    // second one shouldn't leave a dangling participant slot (and an SSE
    // subscriber that just goes quiet) behind in the first. Fire-and-forget:
    // must never block or fail the new join over the old room's own state.
    // (hostRoom()/joinRoomByCode() have this same latent gap, pre-existing,
    // not introduced here - worth a follow-up, out of scope for hot-join.)
    const priorCode = window._room && window._room.code;
    if(priorCode){
      fetch('/api/together/leave', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({code:priorCode})}).catch(()=>{});
    }
    _disconnectRoomStream();
    window._room = _emptyRoomState(false);
    window._room._loading = true;
    const lb=document.getElementById('togetherlightbox');
    if(lb) lb.classList.add('on');
    renderLobby();
    try{
      const r = await fetch('/api/together/hot-join', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
          username, item_id:itemId, item_title:title,
          position_ticks: Math.round((elapsedSecs||0)*1e7), paused: !!paused, runtime_min: runtimeMin||null,
        }),
      });
      const d = await r.json();
      if(!d.ok){ window._room=_emptyRoomState(false); window._room._error=d.error||'Could not join that watch.'; renderLobby(); return; }
      _applyRoomSnapshot(d.room);
      renderLobby();
      _connectRoomStream(d.room.code);
      localStorage.setItem(ROOM_STORAGE_KEY, d.room.code);
      _pvMaybeAutoJoinHostTitle(window._room);
      _pvApplyRemoteCommand(window._room.playback);
    }catch(err){
      window._room=_emptyRoomState(false);
      window._room._error='Could not join — check your connection.';
      renderLobby();
    }
  }
  // Delegated (like the episode picker's own .pv-ep listener) so it
  // survives refreshNow() replacing #nowgrid's innerHTML every 12s -
  // registered once, on the stable ancestor (#now, not the re-rendered
  // grid), so the hot-join CTA keeps working across re-renders.
  (function(){
    const nowSection=document.getElementById('now');
    if(nowSection) nowSection.addEventListener('click', e=>{
      const btn=e.target.closest('.np-watchwith'); if(!btn) return;
      hotJoin(btn.dataset.user, btn.dataset.item, btn.dataset.title,
        parseInt(btn.dataset.elapsed||'0',10), btn.dataset.paused==='1',
        btn.dataset.runtime?parseInt(btn.dataset.runtime,10):null);
    });
  })();

  // Passive discovery for a solo watcher who just got hot-joined: nobody on
  // their end clicked host/join, so their client has no idea a room exists
  // until this checks. Piggybacks on refreshNow()'s existing 12s poll
  // rather than running its own timer - gated to only fire while actively
  // playing something (there's nothing to discover otherwise) and not
  // already tracking a room (avoids a redundant fetch on every tick once
  // discovered/hosting/joined through any path).
  async function checkMyRoom(){
    if(!window._authUser || !_pv.itemId || (window._room && window._room.code)) return;
    try{
      const r = await fetch('/api/together/my-room');
      const d = await r.json();
      if(d.ok && d.room && d.room.is_host){
        const rawParticipants = d.room.participants; // snake_case server shape - see showHotJoinedToast's note
        _applyRoomSnapshot(d.room);
        _connectRoomStream(d.room.code);
        localStorage.setItem(ROOM_STORAGE_KEY, d.room.code);
        renderLobby();
        showHotJoinedToast(rawParticipants);
      }
    }catch(err){}
  }

  // Passive, continuous drift correction for a guest - NOT a host's
  // deliberate action, so this always applies through the loose
  // PASSIVE_DRIFT_TOLERANCE_SECS window (see _pvApplyRemoteCommand), never
  // the tight command tolerance. Sourced from the SAME Now Playing poll
  // every client already runs - the host's own live Jellyfin session is
  // just another entry in it - so this needs no new backend heartbeat.
  // Matches purely on username + item_id (never a Jellyfin user id - see
  // watching.py's now() docstring on that field): if the room's host isn't
  // visibly playing the room's current item right now (paused their whole
  // session elsewhere, playing something outside the room's knowledge, or
  // simply isn't in this tick's Now Playing list), this deliberately does
  // nothing rather than guess.
  function _syncFromNowPlaying(sessions){
    if(!window._room || window._room.isHost || !window._room.itemId) return;
    const hostP = window._room.participants.find(p=>p.isHost);
    if(!hostP) return;
    const hostSess = (sessions||[]).find(s=>s.user===hostP.name && s.item_id===window._room.itemId);
    if(!hostSess) return;
    _pvApplyRemoteCommand({
      status: hostSess.paused ? 'paused' : 'playing',
      position_ticks: Math.round((hostSess.elapsed||0)*1e7),
      updated_at: Date.now()/1000,
    }, {toleranceSecs: PASSIVE_DRIFT_TOLERANCE_SECS});
  }

  // Told, not surprised: the original solo watcher didn't ask to host
  // anything, so a passive auto-promotion (see /hot-join's design notes)
  // gets a toast, not a silent takeover - dismissible, links into the
  // lobby. participants is the RAW server snapshot shape (username/
  // is_host), not the mapped window._room one - the one place in this file
  // that reads that shape directly, since checkMyRoom() has it in hand
  // before/separately from _applyRoomSnapshot's mapping.
  function showHotJoinedToast(participants){
    if(document.getElementById('toast-hotjoined')) return;
    const guest = (participants||[]).find(p=>!p.is_host);
    const name = guest ? guest.username : 'Someone';
    const el=document.createElement('div');
    el.id='toast-hotjoined'; el.className='toast'; el.setAttribute('role','status');
    el.innerHTML = `<span>🍿 ${esc(name)} joined your watch.</span>`
      + `<div class="toast-actions">`
      +   `<button type="button" class="btn btn--sm" onclick="dismissHotJoinedToast()">Dismiss</button>`
      +   `<button type="button" class="btn btn--sm btn--accent" onclick="dismissHotJoinedToast(); openWatchTogether();">View</button>`
      + `</div>`;
    document.body.appendChild(el);
    requestAnimationFrame(()=>el.classList.add('on'));
    setTimeout(dismissHotJoinedToast, 12000);
  }
  function dismissHotJoinedToast(){
    const el=document.getElementById('toast-hotjoined'); if(!el) return;
    el.classList.remove('on');
    setTimeout(()=>el.remove(), 300);
  }

  // ── one-time: ledger totals + growth sparkline + genre bars ────────────────
  async function loadStatic(){
    const [s,grow,gen,rec]=await Promise.all([
      j('/api/stats/summary'), j('/api/stats/growth'),
      j('/api/watching/by-genre?kind=all'), j('/api/watching/latest')]);

    if(s.ok && s.types){
      const by={}; s.types.forEach(t=>by[t.media_type]=t.title_count);
      document.getElementById('total').textContent=(s.totals?.titles||0).toLocaleString();
      const films=by.Movies||0, series=(by.TV||0)+(by.Anime||0), reads=(by.Books||0)+(by.Comics||0)+(by.Manga||0);
      const zc=v=>v===0?' zero':'';
      document.getElementById('stats').innerHTML=
        `<div class="stat"><div class="n${zc(films)}">${films}</div><div class="l">Films</div></div>`+
        `<div class="stat"><div class="n${zc(series)}">${series}</div><div class="l">Series</div></div>`+
        `<div class="stat"><div class="n${zc(reads)}">${reads}</div><div class="l">On the shelves</div></div>`;
    }
    if(grow.ok) sparkline(grow.points);

    const list=((rec.ok&&rec.items)?rec.items:[]).slice(0,14);
    window._latest=list;
    if(list.length){
      document.getElementById('added').classList.add('on');
      document.getElementById('addgrid').innerHTML=list.map((it,idx)=>{
        const yr=it.year?String(it.year):'';
        const kind=it.type==='Series'?'Series':'Film';
        const img=it.tag
          ? `<img src="/api/watching/poster?id=${encodeURIComponent(it.id)}&tag=${encodeURIComponent(it.tag)}" loading="lazy" alt="${esc(it.title)}">`
          : `<div class="noimg">${esc(it.title)}</div>`;
        const genres=(it.genres||[]).slice(0,3).join(' · ');
        const ov=it.overview?esc(it.overview):'No synopsis available.';
        return `<div class="pcard"><div class="poster" tabindex="0" role="button" onclick="openFilm(${idx}, this)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openFilm(${idx}, this)}">${img}`+
          `<div class="syn"><div class="syn-in"><div class="syn-t">${esc(it.title)}${yr?` <span style="color:var(--muted);font-size:13px">(${yr})</span>`:''}</div>`+
          `${genres?`<div class="syn-g">${esc(genres)}</div>`:''}<p class="syn-o">${ov}</p><div class="syn-more">Details →</div></div></div></div>`+
          `<div class="pt" title="${esc(it.title)}">${esc(it.title)}</div>`+
          `<div class="py">${yr}${yr?' · ':''}${kind}</div></div>`;
      }).join('');
    }

    const rows=(gen.ok&&gen.rows)?gen.rows:[];
    if(rows.length){
      const agg={}; rows.forEach(r=>agg[r.genre]=(agg[r.genre]||0)+r.minutes);
      const top=Object.entries(agg).sort((a,b)=>b[1]-a[1]).slice(0,6);
      const max=top[0][1]||1;
      document.getElementById('genre').classList.add('on');
      document.getElementById('genrelist').innerHTML=top.map(([g,m])=>{
        const hrs=(m/60), w=Math.max(4,Math.round(m/max*100));
        return `<div class="grow"><div class="gl">${esc(g)}</div><div class="gt"><i style="width:${w}%"></i></div><div class="gv">${hrs.toFixed(1)} h</div></div>`;}).join('');
    }
  }

  // blow-out film window (synopsis + ratings + Letterboxd/IMDb/TMDB)
  window._latest=window._latest||[];
  // renderFilmDetail(it) does the actual rendering into #lbox-poster/#lbox-body
  // for any already-in-hand card object, regardless of where it came from.
  // openFilm(i) (just-added/popular rails, by array index into window._latest)
  // and openFilmById(id) (catalog grid, fetches fresh by id) both funnel into
  // this so the modal behaves identically either way.
  function renderFilmDetail(it){
    if(!it) return;
    // Stashed so renderPlaySlot()'s Play trigger (it takes no args) and
    // startPlayback() can reach the item currently shown in the modal —
    // same pattern as window._latest/window._authUser.
    window._currentItem = it;
    const isMovie = it.type!=='Series';
    document.getElementById('lbox-poster').innerHTML = it.tag
      ? `<img src="/api/watching/poster?id=${encodeURIComponent(it.id)}&tag=${encodeURIComponent(it.tag)}&h=600" alt="${esc(it.title)}">`
      : `<div class="noimg">${esc(it.title)}</div>`;
    const meta=[it.year, it.runtime_min?it.runtime_min+' min':'', it.official].filter(Boolean).join(' · ');
    const genres=(it.genres||[]).join(' · ');
    let rate='';
    if(it.community!=null) rate+=`<span class="rbadge"><span class="star">★</span>${(+it.community).toFixed(1)}<span class="rl">IMDb</span></span>`;
    if(it.critic!=null) rate+=`<span class="rbadge">${Math.round(it.critic)}%<span class="rl">Rotten&nbsp;Tomatoes</span></span>`;
    rate+=`<span class="rbadge">${isMovie?'Film':'Series'}</span>`;
    const credits=[];
    if(it.director) credits.push(`<b>Dir</b>${esc(it.director)}`);
    if(it.cast&&it.cast.length) credits.push(`<b>Cast</b>${esc(it.cast.join(', '))}`);
    const links=[];
    if(isMovie){
      const lbx = it.tmdb ? `https://letterboxd.com/tmdb/${encodeURIComponent(it.tmdb)}`
                          : `https://letterboxd.com/search/films/${encodeURIComponent(it.title)}/`;
      links.push(`<a class="lbtn" href="${lbx}" target="_blank" rel="noopener">Letterboxd</a>`);
    }
    if(it.imdb) links.push(`<a class="lbtn" href="https://www.imdb.com/title/${encodeURIComponent(it.imdb)}/" target="_blank" rel="noopener">IMDb</a>`);
    if(it.tmdb) links.push(`<a class="lbtn" href="https://www.themoviedb.org/${isMovie?'movie':'tv'}/${encodeURIComponent(it.tmdb)}" target="_blank" rel="noopener">TMDB</a>`);
    document.getElementById('lbox-body').innerHTML =
      `<h3 class="ltitle">${esc(it.title)}</h3>`+
      `${meta?`<div class="lmeta">${esc(meta)}</div>`:''}`+
      `${genres?`<div class="lgen">${esc(genres)}</div>`:''}`+
      `${rate?`<div class="lrate">${rate}</div>`:''}`+
      // Login slot (Phase 2a): "Log in to play" / "Logged in as X". Actual
      // playback (Phase 2b) is still out of scope — item.id and item.type
      // are already on `it`, so wiring a real Play trigger in later is
      // additive: no re-fetch or reshaping needed.
      `<div class="lplay" id="lbox-play" hidden></div>`+
      `<p class="lsyn">${it.overview?esc(it.overview):'No synopsis available.'}</p>`+
      `${credits.length?`<div class="lcredits">${credits.join('&nbsp; · &nbsp;')}</div>`:''}`+
      `${links.length?`<div class="llinks">${links.join('')}</div>`:''}`;
    document.getElementById('lightbox').classList.add('on');
    renderPlaySlot();
  }
  function openFilm(i, srcEl){
    const it=(window._latest||[])[i]; if(!it) return;
    vtOpenPoster(srcEl, () => renderFilmDetail(it));
  }
  // Catalog grid cards always fetch fresh by id (rather than reusing whatever
  // JSON is already on the page) so the code path is uniform regardless of
  // entry point — same endpoint shape as any future direct-link-to-item case.
  // Fetch completes BEFORE the transition starts, so the old-state snapshot is
  // never frozen waiting on the network.
  async function openFilmById(id, srcEl){
    if(!id) return;
    const d = await j('/api/library/item?id='+encodeURIComponent(id));
    if(!d || !d.ok || !d.item) return;
    vtOpenPoster(srcEl, () => renderFilmDetail(d.item));
  }
  function closeFilm(){ const lb=document.getElementById('lightbox'); if(!lb) return; vtCloseFilm(() => lb.classList.remove('on')); }
  (function(){ const lb=document.getElementById('lightbox'); if(lb) lb.addEventListener('click', e=>{ if(e.target===lb) closeFilm(); }); })();

  // ── login (Phase 2a: real per-user Jellyfin identity, no playback yet) ──
  // window._authUser is populated by checkAuth() (called at boot, then on a
  // slow poll + tab refocus — see below) and kept in sync by
  // submitLogin()/doLogout(). No localStorage anywhere here — the whole
  // point of the Postgres-backed session cookie is that no token or
  // credential-adjacent state ever lives client-side.
  window._authUser = null;
  function renderAuth(){ renderAuthBox(); renderPlaySlot(); loadResume(); }
  // Site lockdown: the whole app sits behind #gate until a valid session
  // exists. body starts .locked (so signed-out visitors — and the split second
  // before /me resolves — never flash media), and checkAuth() is the single
  // place that reflects real auth state into the lock. The server enforces it
  // regardless (every /api/* 401s without a session); this is just the UX.
  function lockSite(){ document.body.classList.add('locked'); }
  function unlockSite(){ document.body.classList.remove('locked'); }
  async function checkAuth(){
    const d = await j('/api/auth/me');
    const wasUser = window._authUser;
    window._authUser = (d && d.ok && d.user) ? d.user : null;
    if(window._authUser) unlockSite(); else lockSite();
    // Sessions slide their expiry forward on activity (auth.py), so this
    // should be rare in practice — but a cookie can still lapse (30 days
    // idle), get revoked, or a login elsewhere can clear it. Only fires on
    // the was-logged-in -> now-logged-out transition, never on first boot
    // (wasUser starts null, so a visitor who was never signed in never sees it).
    if(wasUser && wasUser.username && !window._authUser) showSignedOutToast();
    renderAuth();
    // Watch-together refresh-reconnect: only ever attempted once, on the
    // first successful post-boot /me — not on every 5-minute recheck/tab
    // refocus, or a deliberate leaveRoom() earlier in this same tab session
    // would keep getting silently undone by the next poll.
    if(window._authUser && !_roomReconnectAttempted){
      _roomReconnectAttempted = true;
      tryReconnectRoom();
    }
    // Load (or clear) the member's preferences whenever auth state resolves —
    // the privacy toggle only makes sense for a signed-in identity, so it's
    // hidden until we know who they are and what they've chosen.
    if(window._authUser) loadPrefs(); else applyPrefsToUI(null);
    // Start (or tear down) every session-gated data loader + poll. Both are
    // idempotent, so this runs safely on first boot, on the 5-minute recheck,
    // and on a session lapsing mid-visit. See the lifecycle block at the
    // bottom of this script.
    if(window._authUser) startMemberSurfaces(); else stopMemberSurfaces();
  }

  // ── member preferences (privacy) ─────────────────────────────────────────
  // window._prefs mirrors /api/prefs/me. Kept intentionally tiny — one flag
  // today (allow_hot_join). applyPrefsToUI(null) hides the whole section (used
  // when signed out); a real object shows it and reflects the toggle.
  window._prefs = null;
  async function loadPrefs(){
    const d = await j('/api/prefs/me');
    applyPrefsToUI(d && d.ok ? d : null);
  }
  function applyPrefsToUI(prefs){
    window._prefs = prefs;
    const section=document.getElementById('prefSection');
    const box=document.getElementById('pref-hotjoin');
    if(section) section.hidden = !prefs;
    if(box){ box.checked = !!(prefs && prefs.allow_hot_join); box.disabled=false; }
  }
  async function setHotJoinPref(enabled){
    const box=document.getElementById('pref-hotjoin');
    if(box) box.disabled=true;  // guard against a double-toggle mid-request
    const d = await jp('/api/prefs/hot-join', {enabled: !!enabled});
    if(box) box.disabled=false;
    if(!d || !d.ok){
      // Roll the switch back to the server's truth on failure so it never
      // shows a state that wasn't actually saved.
      if(box) box.checked = !!(d && d.allow_hot_join);
      console.warn('Could not save hot-join preference:', d && d.error);
      return;
    }
    if(window._prefs) window._prefs.allow_hot_join = !!d.allow_hot_join;
    if(box) box.checked = !!d.allow_hot_join;
  }
  // Re-checks that can catch a session lapsing while the tab sits open:
  // a slow poll (mirrors the status-chip poll cadence elsewhere on this
  // page) plus a check whenever the tab regains focus, since that's the
  // moment a visitor is most likely to notice/care.
  setInterval(checkAuth, 5 * 60 * 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) checkAuth(); });

  // ── "You've been signed out" toast ───────────────────────────────────────
  function showSignedOutToast(){
    if(document.getElementById('toast-signedout')) return; // already showing
    const el=document.createElement('div');
    el.id='toast-signedout'; el.className='toast'; el.setAttribute('role','status');
    el.innerHTML = `<span>You’ve been signed out.</span>`
      + `<div class="toast-actions">`
      +   `<button type="button" class="btn btn--sm" onclick="dismissSignedOutToast()">Dismiss</button>`
      +   `<button type="button" class="btn btn--sm btn--accent" onclick="dismissSignedOutToast(); openLogin();">Sign in</button>`
      + `</div>`;
    document.body.appendChild(el);
    requestAnimationFrame(()=>el.classList.add('on'));
  }
  function dismissSignedOutToast(){
    const el=document.getElementById('toast-signedout'); if(!el) return;
    el.classList.remove('on');
    setTimeout(()=>el.remove(), 300);
  }
  // Persistent header control — one sign-in covers all of Hub. Signed out shows
  // "Sign in"; signed in shows the name + "Sign out". This is the single home
  // for auth state, so the play slot below no longer repeats it.
  function renderAuthBox(){
    const el=document.getElementById('authbox');
    if(!el) return;
    if(window._authUser && window._authUser.username){
      // design-review #5: the account avatar + menu replaces the bare
      // "name · Sign out" text. The menu also gives "The instrument" a home
      // in the UI (it was ⌘K-only before), and holds Sign out.
      const name=window._authUser.username;
      const initial=(name.trim()[0]||'?').toUpperCase();
      el.innerHTML =
        `<div class="hmenu" id="usermenu">`
        + `<button type="button" class="avatar" id="avatarBtn" aria-haspopup="true" aria-expanded="false" `
        +   `aria-label="Account: ${escA(name)}" title="${escA(name)}" onclick="toggleUserMenu(event)">${esc(initial)}</button>`
        + `<div class="menu usermenu-pop" id="userMenu" role="menu" hidden>`
        +   `<div class="menu-head"><span class="menu-name">${esc(name)}</span><span class="menu-sub">Signed in to Media Hub</span></div>`
        // Custom-request interface (/static/request.html) intentionally removed
        // from this menu — members request through Jellyseerr instead (the door
        // "Request →" links / request.example.org). Revisit before re-adding.
        +   `<button type="button" class="menu-item" role="menuitem" onclick="closeMenus(); location.href='/static/wall.html';">`
        +     `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>Now Playing</button>`
        +   `<button type="button" class="menu-item" role="menuitem" onclick="closeMenus(); location.href='/static/journey.html';">`
        +     `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/></svg>My Requests</button>`
        +   `<button type="button" class="menu-item" role="menuitem" onclick="closeMenus(); openInstrument();">`
        +     `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M3 12h4l3 8 4-16 3 8h4"/></svg>The instrument</button>`
        +   `<button type="button" class="menu-item" role="menuitem" onclick="doLogout();">`
        +     `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/></svg>Sign out</button>`
        + `</div></div>`;
    } else {
      el.innerHTML = `<button type="button" class="btn btn--accent" onclick="openLogin()">Sign in</button>`;
    }
  }
  // ── header popover menus (appearance + account) ───────────────────────────
  // Both share the .hmenu/.menu shell and this one controller: only one menu
  // is open at a time, a click anywhere outside closes it, and Escape closes
  // it (see handleEscape). aria-expanded on the trigger mirrors open state.
  function closeMenus(){
    document.querySelectorAll('.hmenu .menu:not([hidden])').forEach(m=>{
      m.hidden=true;
      const b=m.parentElement.querySelector('[aria-haspopup]');
      if(b) b.setAttribute('aria-expanded','false');
    });
  }
  function _toggleMenu(btnId, menuId){
    const btn=document.getElementById(btnId), menu=document.getElementById(menuId);
    if(!btn || !menu) return;
    const willOpen=menu.hidden;
    closeMenus();
    if(willOpen){ menu.hidden=false; btn.setAttribute('aria-expanded','true'); }
  }
  function toggleUserMenu(e){ if(e) e.stopPropagation(); _toggleMenu('avatarBtn','userMenu'); }
  function toggleAppearance(e){ if(e) e.stopPropagation(); _toggleMenu('appearanceBtn','appearanceMenu'); }
  // A click that isn't inside any .hmenu dismisses the open menu (clicks on the
  // menu's own items close it via their own handlers, so they stay uninterrupted).
  document.addEventListener('click', e=>{ if(!e.target.closest('.hmenu')) closeMenus(); });
  // Fills #lbox-play (recreated fresh by renderFilmDetail's innerHTML write on
  // every modal open). Auth state lives in the header now, so this is just the
  // Play action (signed in) or a prompt to sign in (signed out).
  function renderPlaySlot(){
    const el=document.getElementById('lbox-play');
    if(!el) return;
    el.hidden=false;
    if(window._authUser && window._authUser.username){
      el.innerHTML = `<a href="#" class="lbtn lplay-go" onclick="event.preventDefault(); startPlayback();">&#9654; Play</a>`;
    } else {
      el.innerHTML = `<a href="#" class="lbtn" onclick="event.preventDefault(); openLogin();">Sign in to play</a>`;
    }
  }
  function openLogin(){
    const lb=document.getElementById('loginlightbox'); if(!lb) return;
    const st=document.getElementById('login-status'); if(st) st.textContent='';
    const f=document.getElementById('loginform'); if(f) f.reset();
    lb.classList.add('on');
    const u=document.getElementById('login-user'); if(u) u.focus();
  }
  function closeLogin(){ const lb=document.getElementById('loginlightbox'); if(lb) lb.classList.remove('on'); }
  (function(){ const lb=document.getElementById('loginlightbox'); if(lb) lb.addEventListener('click', e=>{ if(e.target===lb) closeLogin(); }); })();

  // "The instrument" — ⌘K-launched overlay for what used to be the always-
  // present <details class="sys"> System zone. Pure open/close; the actual
  // data (loadStatic/loadHostSpec/loadStatus/loadUptime) is already running
  // in the background regardless — see the CSS block's comment.
  // ── the instrument: loaded on open, polled only while open ───────────────
  // Every element this overlay shows (#total, #stats, #spark, #genrelist,
  // #statuschip, #statusboard, #stg-bar, #spec-chart) lives INSIDE it — none
  // of it is visible on the page behind. It used to fetch host-spec + storage
  // at boot and then poll /api/stats/status every 60s and the 30-day timeline
  // every 10 minutes for the entire session, whether or not anyone had ever
  // pressed Ctrl-K. Now nothing loads until it's actually opened, and the
  // polls stop when it closes.
  //
  // loadStatic() is deliberately NOT here: it's the one loader that spans both
  // worlds (it fills #total/#stats/#spark for this overlay AND the Recently
  // Added rail on the homepage), so it stays in startMemberSurfaces().
  let _instrumentLoaded=false, _instrumentStatusTimer=null, _instrumentUptimeTimer=null;
  function openInstrument(){
    const lb=document.getElementById('instrumentlightbox'); if(lb) lb.classList.add('on');
    if(!window._authUser) return;          // gated endpoints; nothing to fetch signed out
    if(!_instrumentLoaded){
      _instrumentLoaded=true;              // host-spec and storage are effectively static
      loadHostSpec(); loadStorage();
    }
    loadStatus(); loadUptime();
    clearInterval(_instrumentStatusTimer); clearInterval(_instrumentUptimeTimer);
    _instrumentStatusTimer=setInterval(loadStatus, 60000);
    _instrumentUptimeTimer=setInterval(loadUptime, 600000);
  }
  function closeInstrument(){
    const lb=document.getElementById('instrumentlightbox'); if(lb) lb.classList.remove('on');
    clearInterval(_instrumentStatusTimer); clearInterval(_instrumentUptimeTimer);
    _instrumentStatusTimer=_instrumentUptimeTimer=null;
  }
  (function(){ const lb=document.getElementById('instrumentlightbox'); if(lb) lb.addEventListener('click', e=>{ if(e.target===lb) closeInstrument(); }); })();

  // ── Nocturne Stage 2: ⌘K command palette ─────────────────────────────────
  // window._cmdk holds the live keyboard-nav state: `items` is a flat,
  // DOM-order list of {el, run} for whatever's currently rendered (Jump to +
  // Commands, or Search results — never both at once, see cmdkOnInput), so
  // ↑/↓/Enter don't need to know which "mode" is active. Every action closes
  // the palette itself (closeCmdK()) before doing its real work, except
  // "Switch theme" (cycling in place is more useful with the palette still
  // open, so you can see the swatch change and cycle again).
  window._cmdk = {open:false, query:'', items:[], selIndex:-1, searchSeq:0, searchDebounce:null};
  window._cw = window._cw || []; // populated by loadResume(); empty until the first /api/playback/resume completes
  const CMDK_IS_MAC = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent || '');
  // design-review #4: one consistent icon language for title rows. The icon
  // says what Enter DOES, not what media type it is (type is already in the
  // sub-line): a solid play triangle means "this starts playback right now"
  // (resume + the instant-play latest rows); the open-card glyph means "this
  // opens details" (search results). No row ever mixes the two, so the palette
  // never shows a play triangle on something that only opens a modal. Inline
  // SVG (not a unicode glyph) so both render identically on every platform.
  const CMDK_PLAY_ICON = '<svg viewBox="0 0 24 24" fill="currentColor" width="13" height="13" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
  const CMDK_OPEN_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="2.5"/><path d="M9.5 14.5 14.5 9.5"/><path d="M10.5 9.5h4v4"/></svg>';
  const CMDK_REQ_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 8.5v7M8.5 12h7"/></svg>';

  function openCmdK(){
    const el=document.getElementById('cmdk'); if(!el) return;
    el.classList.add('on');
    window._cmdk.open=true;
    const input=document.getElementById('cmdInput');
    if(input) input.value='';
    window._cmdk.query='';
    cmdkRenderDefault();
    setTimeout(()=>{ if(input) input.focus(); }, 30);
  }
  function closeCmdK(){
    const el=document.getElementById('cmdk'); if(!el) return;
    el.classList.remove('on');
    window._cmdk.open=false;
    clearTimeout(window._cmdk.searchDebounce);
  }
  document.getElementById('cmdk')?.addEventListener('click', e=>{ if(e.target.id==='cmdk') closeCmdK(); });

  function cmdkSetItems(navItems){
    window._cmdk.items=navItems;
    window._cmdk.selIndex=navItems.length?0:-1;
    cmdkHighlight();
  }
  function cmdkHighlight(){
    window._cmdk.items.forEach((it,i)=>it.el.classList.toggle('sel', i===window._cmdk.selIndex));
    const sel=window._cmdk.items[window._cmdk.selIndex];
    if(sel) sel.el.scrollIntoView({block:'nearest'});
  }
  function cmdkMove(delta){
    const n=window._cmdk.items.length; if(!n) return;
    window._cmdk.selIndex=((window._cmdk.selIndex+delta)%n+n)%n;
    cmdkHighlight();
  }
  function cmdkRunSelected(){
    const it=window._cmdk.items[window._cmdk.selIndex];
    if(it && it.run) it.run(it.el);
  }
  // Wires click + builds the flat nav list from whatever .p-item elements
  // now exist under `groupSelector`, in DOM order, each paired with the
  // matching entry in `actionList` (same order the caller rendered them in).
  function cmdkWireItems(groupSelector, actionList){
    const scroll=document.getElementById('cmdk-scroll');
    const els=Array.from(scroll.querySelectorAll(groupSelector+' .p-item'));
    const navItems=els.map((el,i)=>({el, run: actionList[i] ? actionList[i].run : null}));
    navItems.forEach(it=>{ if(it.run) it.el.addEventListener('click', ()=>it.run(it.el)); });
    cmdkSetItems(navItems);
  }
  function cmdkItemHtml(it){
    return `<div class="p-item"><div class="p-ico${it.iconClass?(' '+it.iconClass):''}">${it.icon}</div>`
      + `<div class="p-main"><div class="p-t">${esc(it.title)}</div>${it.sub?`<div class="p-s">${esc(it.sub)}</div>`:''}</div>`
      + (it.hint?`<span class="hint">${esc(it.hint)}</span>`:'') + `</div>`;
  }

  function cmdkCycleTheme(){
    const cur=document.documentElement.getAttribute('data-theme') || 'default';
    const idx=THEMES.findIndex(t=>t.key===cur);
    const next=THEMES[(idx+1+THEMES.length)%THEMES.length];
    applyTheme(next.key);
    cmdkRenderDefault(); // refresh the "Switch theme" row's sub-copy context + keep focus/selection sane
  }

  function cmdkRenderDefault(){
    const scroll=document.getElementById('cmdk-scroll'); if(!scroll) return;
    const jumpItems=[];
    const seenIds=new Set();
    (window._cw||[]).slice(0,3).forEach(it=>{
      if(!it || !it.id || seenIds.has(it.id)) return;
      seenIds.add(it.id);
      const seLabelStr = (it.season!=null && it.episode!=null) ? ` S${it.season}·E${it.episode}` : '';
      jumpItems.push({
        icon:CMDK_PLAY_ICON, iconClass:'', title: it.title||'Untitled',
        sub: (it.series ? `${it.series} · resume${seLabelStr}` : 'Resume'),
        hint:'↵ play',
        run: ()=>{ closeCmdK(); playItem(it.id, it.series||it.title||'', it.position_ticks||0); },
      });
    });
    (window._latest||[]).forEach(it=>{
      if(jumpItems.length>=5 || !it || !it.id || seenIds.has(it.id)) return;
      seenIds.add(it.id);
      jumpItems.push({
        icon: CMDK_PLAY_ICON, iconClass:'', title: it.title,
        sub: `${it.type==='Series'?'Series':'Film'}${it.year?(' · '+it.year):''}`, hint:'↵ play',
        run: ()=>{ closeCmdK(); window._currentItem=it; startPlayback(); },
      });
    });

    const commandItems=[
      {icon:'✦', iconClass:'m', title:'Host a watch-together room', sub:'Invite friends to sync playback',
        run:()=>{ closeCmdK(); openWatchTogether(); }},
      {icon:'⤢', iconClass:'', title:'Open the instrument', sub:'Live server telemetry, storage & uptime — hidden by default',
        run:()=>{ closeCmdK(); openInstrument(); }},
      {icon:'◐', iconClass:'', title:'Switch theme', sub:'Nocturne · Grid · Ember · Phosphor · Sodium · Dusk',
        run:()=>cmdkCycleTheme()},
    ];

    let html='';
    if(jumpItems.length) html += `<div class="p-group"><div class="gl">Jump to</div>${jumpItems.map(cmdkItemHtml).join('')}</div>`;
    html += `<div class="p-group"><div class="gl">Commands</div>${commandItems.map(cmdkItemHtml).join('')}</div>`;
    html += `<div class="p-group cmdk-shortcuts"><div class="gl">Shortcuts</div><div class="sc">`
      + `<div class="sc-col"><h4>⊞ Windows</h4>`
      +   `<div class="sc-row"><span>Command bar</span><span class="keys"><b>Ctrl</b><b>K</b></span></div>`
      +   `<div class="sc-row"><span>Watch together</span><span class="keys"><b>Ctrl</b><b>⇧</b><b>W</b></span></div>`
      +   `<div class="sc-row"><span>The instrument</span><span class="keys"><b>Ctrl</b><b>I</b></span></div>`
      +   `<div class="sc-row"><span>Play / pause</span><span class="keys"><b>Space</b></span></div></div>`
      + `<div class="sc-col"><h4>iOS · Mac</h4>`
      +   `<div class="sc-row"><span>Command bar</span><span class="keys"><b>⌘</b><b>K</b></span></div>`
      +   `<div class="sc-row"><span>Watch together</span><span class="keys"><b>⌘</b><b>⇧</b><b>W</b></span></div>`
      +   `<div class="sc-row"><span>The instrument</span><span class="keys"><b>⌘</b><b>I</b></span></div>`
      +   `<div class="sc-row"><span>Play / pause</span><span class="keys"><b>Space</b></span></div></div>`
      + `</div></div>`;
    scroll.innerHTML=html;
    cmdkWireItems('.p-group:not(.cmdk-shortcuts)', [...jumpItems, ...commandItems]);
  }

  // Real title search — same /api/library/items endpoint (and q/type/sort/
  // limit/offset contract) the catalog view's own search box already calls,
  // just a smaller page and no infinite-scroll. Debounced like the catalog's
  // own libInitControlsOnce() input listener; a stale-response guard
  // (searchSeq) mirrors libState.seq so a fast keystroke can't have an
  // earlier, slower response clobber a later, faster one.
  // Inline request from the palette: POST and update the row in place, so a
  // member can request without leaving Hub. Works for both video (Jellyseerr)
  // and the book-request service (books/audiobooks) — just a different url/body.
  function cmdkRequest(row, url, body){
    if(!row || row.classList.contains('requested')) return;
    const hint=row.querySelector('.hint');
    row.classList.add('requested'); // guard against double-fire
    if(hint) hint.textContent='requesting…';
    fetch(url, {method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
      .then(r=>{ if(r.status===401) throw 'auth'; return r.json(); })
      .then(d=>{
        if(d && d.ok){ if(hint){ hint.textContent = d.already?'already requested':'requested ✓'; hint.style.color='var(--ok-dim)'; } row.style.opacity='.8'; }
        else { row.classList.remove('requested'); if(hint) hint.textContent='failed — ↵ retry'; }
      })
      .catch(e=>{ row.classList.remove('requested'); if(hint) hint.textContent=(e==='auth'?'sign in first':'failed'); });
  }

  // Unified search: owned library + requestable video (Jellyseerr) + requestable
  // books/audiobooks (the book-request service), all in one palette. This is the
  // "stay on one page" front door — find or request anything without leaving Hub.
  async function cmdkSearch(q){
    const mySeq=++window._cmdk.searchSeq;
    const scroll=document.getElementById('cmdk-scroll'); if(!scroll) return;
    scroll.innerHTML=`<div class="p-group"><div class="gl">Search</div><div class="p-empty">Searching…</div></div>`;

    // Movies/series are the FAST sources (owned library + Jellyseerr) — render
    // them immediately and preferred (first). The book search is slower, so
    // it streams in after rather than blocking
    // (or, on error, blanking) the video results the way one Promise.all did.
    let libD=null, vidD=null;
    try {
      [libD, vidD]=await Promise.all([
        j('/api/library/items?'+new URLSearchParams({q, type:'all', sort:'added', limit:6, offset:0})),
        j('/api/request/search?'+new URLSearchParams({q, limit:6})),
      ]);
    } catch(e){ /* one flaky source shouldn't blank the palette */ }
    if(mySeq!==window._cmdk.searchSeq) return; // superseded by a newer keystroke

    const lib =(libD && libD.ok && Array.isArray(libD.items)) ? libD.items : [];
    const vid =(vidD && vidD.ok && Array.isArray(vidD.items)) ? vidD.items.filter(r=>r.status!=='available') : [];

    const libActions=lib.map(it=>({
      icon: CMDK_OPEN_ICON, iconClass:'', title: it.title,
      sub: `${it.type==='Series'?'Series':'Film'}${it.year?(' · '+it.year):''}`, hint:'↵ open',
      run: ()=>{ closeCmdK(); openFilmById(it.id); },
    }));
    const vidActions=vid.map(it=>{
      const ready=(it.status==='none' || !it.status);
      return { icon: CMDK_REQ_ICON, iconClass:'m', title: it.title,
        sub: `${it.type==='tv'?'Series':'Film'}${it.year?(' · '+it.year):''}`,
        hint: ready?'↵ request':'requested',
        run: (rowEl)=>{ if(ready) cmdkRequest(rowEl, '/api/request/submit', {tmdb_id:it.id, media_type:it.type}); } };
    });

    const grp=(label, acts)=> acts.length ? `<div class="p-group"><div class="gl">${label}</div>${acts.map(cmdkItemHtml).join('')}</div>` : '';

    // Repaint with whatever book results we have (null = still searching books).
    function paint(bookActions){
      if(mySeq!==window._cmdk.searchSeq) return;
      const booksDone = bookActions !== null;
      const any = libActions.length || vidActions.length || (booksDone && bookActions.length);
      if(booksDone && !any){
        scroll.innerHTML=`<div class="p-group"><div class="gl">Search</div><div class="p-empty">No matches for "${esc(q)}".</div></div>`;
        cmdkSetItems([]); return;
      }
      let html = grp('In your library', libActions) + grp('Request to watch', vidActions);
      html += booksDone
        ? grp('Request to read &amp; listen', bookActions)
        : `<div class="p-group"><div class="gl">Request to read &amp; listen</div><div class="p-empty">Searching…</div></div>`;
      scroll.innerHTML = html;
      cmdkWireItems('.p-group', [...libActions, ...vidActions, ...(booksDone ? bookActions : [])]);
    }
    paint(null); // movies/series show now, with a "Searching…" placeholder for books

    j('/api/request/books?'+new URLSearchParams({q, kind:'all', limit:6}))
      .then(bookD=>{
        const book=(bookD && bookD.ok && Array.isArray(bookD.items)) ? bookD.items : [];
        paint(book.map(it=>{
          const label={book:'Book', audiobook:'Audiobook', manga:'Manga', comic:'Comic'}[it.type] || 'Book';
          const meta=it.author || it.year || '';
          return { icon: CMDK_REQ_ICON, iconClass:'', title: it.title,
            sub: `${label}${meta?(' · '+meta):''}`, hint:'↵ request',
            run: (rowEl)=>cmdkRequest(rowEl, '/api/request/books/submit', {id:it.id, type:it.type}) };
        }));
      })
      .catch(()=>paint([])); // book search failed — just drop the placeholder
  }
  function cmdkOnInput(val){
    window._cmdk.query=(val||'').trim();
    clearTimeout(window._cmdk.searchDebounce);
    if(!window._cmdk.query){ cmdkRenderDefault(); return; }
    window._cmdk.searchDebounce=setTimeout(()=>cmdkSearch(window._cmdk.query), 250);
  }
  (function(){
    const input=document.getElementById('cmdInput'); if(!input) return;
    input.addEventListener('input', ()=>cmdkOnInput(input.value));
    input.addEventListener('keydown', e=>{
      if(e.key==='ArrowDown'){ e.preventDefault(); cmdkMove(1); }
      else if(e.key==='ArrowUp'){ e.preventDefault(); cmdkMove(-1); }
      else if(e.key==='Enter'){ e.preventDefault(); cmdkRunSelected(); }
      // Escape is handled by the shared handleEscape() keydown listener below.
    });
  })();
  // Global open shortcut (⌘K/Ctrl+K) + the two documented command shortcuts
  // from the Shortcuts panel (Ctrl/⌘+Shift+W, Ctrl/⌘+I) — real bindings, not
  // just reference copy, so the panel doesn't document anything that isn't
  // actually true. Guarded against firing while typing in an unrelated text
  // field (e.g. the catalog search box, the login form).
  addEventListener('keydown', e=>{
    const typing = e.target && (e.target.tagName==='INPUT' || e.target.tagName==='TEXTAREA') && e.target.id!=='cmdInput';
    if(typing) return;
    const mod = e.metaKey || e.ctrlKey;
    if(mod && e.key.toLowerCase()==='k'){ e.preventDefault(); window._cmdk.open?closeCmdK():openCmdK(); }
    else if(mod && e.shiftKey && e.key.toLowerCase()==='w'){ e.preventDefault(); closeCmdK(); openWatchTogether(); }
    else if(mod && e.key.toLowerCase()==='i'){ e.preventDefault(); closeCmdK(); openInstrument(); }
  });
  // Shared by the gate form (#gateform, the homepage) and the legacy in-app
  // login modal (#loginform, still reachable via the header once signed in on
  // another tab). On success we reload rather than re-render: while signed out
  // every media loader fired at boot and got a 401, so the page holds no
  // library data — a reload re-runs those loaders with the fresh session cookie
  // in place, which is far less error-prone than hand-re-firing each scattered
  // loader. `prefix` selects which form's fields/status to read.
  async function submitLoginWith(prefix){
    const user=document.getElementById(prefix+'-user').value.trim();
    const pass=document.getElementById(prefix+'-pass').value;
    const st=document.getElementById(prefix+'-status');
    if(st) st.textContent='Signing in…';
    try{
      const r=await fetch('/api/auth/login',{
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({username:user, password:pass})
      });
      const d=await r.json();
      if(d && d.ok){ if(st) st.textContent=''; location.reload(); }
      else { if(st) st.textContent=(d&&d.error)?d.error:'Sign in failed.'; }
    }catch(err){ if(st) st.textContent='Sign in failed.'; }
    return false;
  }
  function submitGateLogin(e){ e.preventDefault(); return submitLoginWith('gate'); }
  function submitLogin(e){ e.preventDefault(); return submitLoginWith('login'); }
  async function doLogout(){
    try{ await fetch('/api/auth/logout',{method:'POST'}); }catch(e){}
    window._authUser=null;
    // Reload back to the locked gate — clears rendered media and resets the
    // now-playing/status polls in one clean sweep.
    location.reload();
  }

  // ── Real-time sync backend: watch-together lobby ─────────────────────────
  // window._room is the reserved state hook, grown by two fields since the
  // pass-D mock now that sync is real: { code, isHost, itemId, itemTitle,
  // participants:[{name,isHost}], connected, playback:{status,
  // position_ticks,updated_at}, runtimeMin }. `playback`/`runtimeMin` are
  // the new bits — together.py's snapshot/broadcast shape carries both, and
  // the lobby's shared-player status mirror (renderLobby(), below) reads
  // them to show a real icon/position instead of the old permanent mock.
  // `connected` still reflects the SSE connection specifically (true once
  // it's open, false before/between/after).
  //
  // Playback sync itself (host broadcasts, guests apply) lives down in the
  // Phase 2b player block as `_pvHostBroadcast()`/`_pvApplyRemoteCommand()`/
  // `_pvMaybeAutoJoinHostTitle()` — kept there rather than here since it's
  // fundamentally player-integration code that happens to be triggered from
  // room events, not lobby-UI code. This block owns room lifecycle + the
  // SSE subscription; the player block owns what a room's playback state
  // *does* to a `<video>` element.
  window._room = null;
  let _roomStream = null; // EventSource for the open room, if any — not part of the documented window._room shape

  // Refresh-reconnect (browser tab close/reload) — see tryReconnectRoom()
  // below, called once from checkAuth() after the first successful /me.
  // Deliberately just a room *code*, nothing session/credential-adjacent
  // (matches the no-localStorage-for-auth stance already documented above
  // window._authUser) — the actual membership proof is still the
  // hub_session cookie; this is only "which code to ask about."
  const ROOM_STORAGE_KEY = 'mf_together_room';
  let _roomReconnectAttempted = false;

  function _emptyRoomState(isHost, code){
    return { code: code||null, isHost, itemId:null, itemTitle:null, participants:[], connected:false, playback:null, runtimeMin:null };
  }

  function openWatchTogether(){
    const lb=document.getElementById('togetherlightbox'); if(!lb) return;
    // Opened from the player's "Watch together" control with nothing hosted
    // yet -> default straight to hosting the title that's playing. Opened
    // from the header with no active room -> the neutral host-or-join state.
    const playerOpen = document.getElementById('playerlightbox')?.classList.contains('on');
    if(!window._room && playerOpen && window._currentItem) hostRoom(window._currentItem);
    else renderLobby();
    lb.classList.add('on');
  }
  function closeWatchTogether(){
    const lb=document.getElementById('togetherlightbox'); if(lb) lb.classList.remove('on');
  }
  (function(){ const lb=document.getElementById('togetherlightbox'); if(lb) lb.addEventListener('click', e=>{ if(e.target===lb) closeWatchTogether(); }); })();

  // Maps together.py's wire shape (snake_case, like every other Hub API —
  // window._room stays camelCase, per its own documented contract above) onto
  // window._room, preserving whatever `connected` was already set to (the
  // SSE layer owns that flag, not the snapshot).
  function _applyRoomSnapshot(sr){
    const wasConnected = window._room ? window._room.connected : false;
    window._room = {
      code: sr.code, isHost: !!sr.is_host,
      itemId: sr.item_id || null, itemTitle: sr.item_title || null,
      participants: (sr.participants||[]).map(p=>({name:p.username, isHost:!!p.is_host})),
      connected: wasConnected,
      playback: sr.playback ? {status:sr.playback.status, position_ticks:sr.playback.position_ticks||0, updated_at:sr.playback.updated_at||0} : null,
      runtimeMin: sr.runtime_min || null,
    };
  }

  function _disconnectRoomStream(){
    if(_roomStream){ try{ _roomStream.close(); }catch(e){} _roomStream=null; }
  }
  function _connectRoomStream(code){
    _disconnectRoomStream();
    const es = new EventSource('/api/together/stream/'+encodeURIComponent(code));
    _roomStream = es;
    es.addEventListener('snapshot', e=>{
      try{
        _applyRoomSnapshot(JSON.parse(e.data));
        if(window._room) window._room.connected=true;
        _pvMaybeAutoJoinHostTitle(window._room);
        _pvApplyRemoteCommand(window._room.playback);
        renderLobby();
      }catch(err){}
    });
    es.addEventListener('roster', e=>{
      try{
        const d=JSON.parse(e.data);
        if(window._room && window._room.code===d.code) window._room.participants=(d.participants||[]).map(p=>({name:p.username, isHost:!!p.is_host}));
        renderLobby();
      }catch(err){}
    });
    es.addEventListener('playback', e=>{
      try{
        const d=JSON.parse(e.data);
        if(window._room){
          window._room.playback = {status:d.status, position_ticks:d.position_ticks||0, updated_at:d.updated_at||0};
          // The room's item can change mid-session now (episode switches,
          // in principle any title switch — see _pvHostBroadcast's
          // comments), so a guest whose player is showing something else
          // needs to follow, not just re-apply position/status to whatever
          // they already have open.
          if(d.item_id){ window._room.itemId=d.item_id; window._room.itemTitle=d.item_title||window._room.itemTitle; }
          if(d.runtime_min) window._room.runtimeMin=d.runtime_min;
          _pvMaybeAutoJoinHostTitle(window._room); // no-op if already showing that item, or if this is the host
        }
        _pvApplyRemoteCommand(d); // no-op for the host — see that function's guard
        renderLobby();
      }catch(err){}
    });
    es.addEventListener('closed', ()=>{ leaveRoom(true); });
    es.onerror = ()=>{ if(window._room) window._room.connected=false; renderLobby(); }; // EventSource retries on its own
  }

  async function hostRoom(item){
    _disconnectRoomStream();
    // Prefer whatever's actually playing (_pv.itemId/#pv-title) over `item`
    // itself when the player's already open: for a series, `item` here is
    // window._currentItem — the *series* — but the real playable id (and
    // the one _pvHostBroadcast will compare against on every play/pause/
    // seek) is the *episode* actually on-screen. Seeding the room with the
    // series id would make the very first broadcast look like an "item
    // changed" event instead of matching from the start. Falls back to
    // `item` when there's no active player (e.g. hosting from the header
    // before playing anything) - matches the pre-existing behavior there.
    const v=document.getElementById('pv-video');
    const titleEl=document.getElementById('pv-title');
    const activeId = _pv.itemId || (item ? item.id : null);
    const activeTitle = (_pv.itemId && titleEl && titleEl.textContent) || (item ? item.title : null);
    const activeRuntimeMin = (_pv.itemId && v && v.duration && isFinite(v.duration))
      ? Math.round(v.duration/60) : (item && item.runtime_min) || null;
    window._room = _emptyRoomState(true);
    window._room.itemId = activeId;
    window._room.itemTitle = activeTitle;
    window._room._loading = true;
    renderLobby();
    try{
      const r = await fetch('/api/together/host', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ item_id: activeId, item_title: activeTitle, runtime_min: activeRuntimeMin }),
      });
      const d = await r.json();
      if(!d.ok){ window._room = _emptyRoomState(true); window._room._error = d.error || 'Could not create a room.'; renderLobby(); return; }
      _applyRoomSnapshot(d.room);
      renderLobby();
      _connectRoomStream(d.room.code);
      localStorage.setItem(ROOM_STORAGE_KEY, d.room.code);
    }catch(err){
      window._room = _emptyRoomState(true);
      window._room._error = 'Could not create a room — check your connection.';
      renderLobby();
    }
  }
  async function joinRoomByCode(rawCode){
    const code=(rawCode||'').trim().toUpperCase();
    if(!code) return;
    _disconnectRoomStream();
    window._room = _emptyRoomState(false, code);
    window._room._loading = true;
    renderLobby();
    try{
      const r = await fetch('/api/together/join', {
        method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({code}),
      });
      const d = await r.json();
      if(!d.ok){ window._room = _emptyRoomState(false); window._room._error = d.error || 'Room not found.'; renderLobby(); return; }
      _applyRoomSnapshot(d.room);
      renderLobby();
      _connectRoomStream(d.room.code);
      localStorage.setItem(ROOM_STORAGE_KEY, d.room.code);
      // Guest entry: jump straight into the host's current title on their
      // own /api/playback/* stream (never the host's stream itself — see
      // _pvMaybeAutoJoinHostTitle's docstring), then apply whatever
      // play/pause/position the room already has once that player is ready.
      _pvMaybeAutoJoinHostTitle(window._room);
      _pvApplyRemoteCommand(window._room.playback);
    }catch(err){
      window._room = _emptyRoomState(false);
      window._room._error = 'Could not join — check your connection.';
      renderLobby();
    }
  }
  async function leaveRoom(fromServerClose){
    const code = window._room && window._room.code;
    _disconnectRoomStream();
    window._room = null;
    localStorage.removeItem(ROOM_STORAGE_KEY);
    renderLobby();
    // fromServerClose=true means together.py already tore the room down
    // (the SSE `closed` event) — re-POSTing /leave would just be a no-op
    // (leave is idempotent either way), so it's skipped, not required.
    if(code && !fromServerClose){
      try{ await fetch('/api/together/leave', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({code})}); }catch(e){}
    }
  }
  function copyInviteCode(){
    if(!window._room || !window._room.code) return;
    try{ navigator.clipboard && navigator.clipboard.writeText(window._room.code); }catch(e){}
  }

  // Refresh-reconnect: called once, from checkAuth(), after the first
  // successful /me resolves post-boot. A stored code that 404/403s (room
  // already swept, or this session somehow isn't a member anymore) falls
  // back to the clean no-room state and forgets the code — never surfaces
  // an error banner for this one specifically, since silently trying and
  // silently giving up is exactly what "reconnect" should feel like on a
  // routine refresh. A network error (as opposed to a real ok:false)
  // leaves the stored code alone, matching the rest of this module's
  // never-punish-a-transient-failure stance.
  async function tryReconnectRoom(){
    const code = localStorage.getItem(ROOM_STORAGE_KEY);
    if(!code) return;
    try{
      const r = await fetch('/api/together/room/'+encodeURIComponent(code));
      const d = await r.json();
      if(!d.ok){ localStorage.removeItem(ROOM_STORAGE_KEY); return; }
      _applyRoomSnapshot(d.room);
      _connectRoomStream(d.room.code);
      _pvMaybeAutoJoinHostTitle(window._room);
      _pvApplyRemoteCommand(window._room.playback);
      // Deliberately doesn't open the lobby overlay — "silently rejoins,"
      // per the plan. renderLobby() still runs so #together-body is correct
      // whenever the visitor next opens it themselves.
      renderLobby();
    }catch(err){ /* transient network failure - leave the stored code for next time */ }
  }

  function renderLobby(){
    const el=document.getElementById('together-body'); if(!el) return;
    const room=window._room;

    if(room && room._error){
      el.innerHTML = `<h3 class="together-title">Watch together</h3>`
        + `<p class="together-error">${esc(room._error)}</p>`
        + `<button type="button" class="btn btn--accent together-hostbtn" onclick="window._room=null; renderLobby();">Back</button>`;
      return;
    }
    if(room && room._loading){
      el.innerHTML = `<h3 class="together-title">Watch together</h3>`
        + `<p class="together-empty">${room.isHost ? 'Creating your room…' : 'Joining…'}</p>`;
      return;
    }

    if(!room){
      const hostBtn = window._currentItem
        ? `<button type="button" class="btn btn--accent together-hostbtn" onclick="hostRoom(window._currentItem)">Host a room for "${esc(window._currentItem.title)}"</button>`
        : '';
      el.innerHTML = `<h3 class="together-title">Watch together</h3>`
        + `<p class="together-sub">Host a room for what you're watching, or join one with a code.</p>`
        + `<div class="together-join">`
        +   `<input type="text" class="field" id="together-code-input" placeholder="Enter a room code" maxlength="8">`
        +   `<button type="button" class="btn btn--accent" onclick="joinRoomByCode(document.getElementById('together-code-input').value)">Join</button>`
        + `</div>`
        + hostBtn;
      return;
    }

    const people = room.participants.map(p=>
      `<span class="tp-chip${p.isHost?' host':''}">${esc(p.name)}${p.isHost?'<span class="host-badge">Host</span>':''}</span>`
    ).join('');
    const waiting = room.participants.length<2 ? '<p class="together-empty">Waiting for others to join…</p>' : '';

    // Shared-player status mirror: read-only (pointer-events:none in CSS —
    // real controls live in the actual player, only the host's ever do
    // anything), but the icon/position now reflect together.py's live
    // room.playback rather than a permanent "0:00 / --:--" mock. Position is
    // extrapolated forward from playback.updated_at while status is
    // "playing" so it doesn't look frozen between SSE events (accurate as of
    // this render, not a ticking clock — this panel only repaints on room
    // events, there's no dedicated interval for it). Duration stays
    // "--:--" when runtimeMin is unknown (rooms hosted without an item, or
    // before this field existed).
    const pb = room.playback || {status:'paused', position_ticks:0, updated_at:0};
    const curSecs = (pb.position_ticks||0)/1e7 + (pb.status==='playing' && pb.updated_at ? Math.max(0, Date.now()/1000 - pb.updated_at) : 0);
    const durSecs = room.runtimeMin ? room.runtimeMin*60 : 0;
    const frac = durSecs ? Math.min(1, curSecs/durSecs) : 0;
    const playIcon = pb.status==='playing' ? PV_ICONS.pause : PV_ICONS.play;

    el.innerHTML = `<h3 class="together-title">Watch together</h3>`
      + `<p class="together-sub">${room.isHost ? 'You’re hosting.' : 'You’ve joined a room.'} Share the code to bring others in.</p>`
      + `<div class="together-code-row"><div class="together-code">${esc(room.code)}</div>`
      +   `<button type="button" class="btn" onclick="copyInviteCode()">Copy</button></div>`
      + `<div class="together-people"><p class="eyebrow">In the room</p><div class="tp-list">${people}</div>${waiting}</div>`
      + `<div class="together-player">`
      +   `<div class="tplay-title">${room.itemTitle ? esc(room.itemTitle) : 'Waiting for the host to start something…'}</div>`
      +   `<div class="tplay-controls" aria-disabled="true" title="${room.isHost?'Controlled from the player':'Host-controlled'}">`
      +     `<button class="btn pv-btn" disabled aria-label="Playback status">${playIcon}</button>`
      +     `<div class="pv-track"><div class="pv-rail"><div class="pv-played" style="width:${(frac*100).toFixed(2)}%"></div></div></div>`
      +     `<span class="pv-time">${fmtT(curSecs)}&nbsp;/&nbsp;${durSecs?fmtT(durSecs):'--:--'}</span>`
      +   `</div>`
      + `</div>`
      + `<button type="button" class="btn together-leavebtn" onclick="leaveRoom()">${room.isHost?'End room':'Leave room'}</button>`;
  }

  // ── Phase 2b: in-browser video playback ─────────────────────────────────
  // Custom player (not an iframe embed of Jellyfin's own web app) — see the
  // Hub Phase 2b plan for why: a cross-origin iframe can't be themed, and
  // there's no supported way to hand Phase 2a's session token into an
  // embedded Jellyfin SPA. Everything below talks to Hub's own
  // /api/playback/* routes, never to Jellyfin directly from the browser
  // except for the stream/subtitle URLs those routes hand back (the one
  // deliberate token-in-URL exception — see playback.py's module docstring).
  const _pv = {
    hls:null, syncTimer:null, itemId:null, playSessionId:null, mediaSourceId:null,
    seasons:null, activeSeason:null, hideTimer:null,
    // true while _pvApplyRemoteCommand() is programmatically setting
    // currentTime/calling play()/pause() to follow a host broadcast —
    // guests never issue commands themselves (see _pvHostBroadcast's own
    // isHost guard) so this mostly matters for clarity/future-proofing
    // rather than preventing an active feedback loop today.
    applyingRemote:false,
    // Audio/subtitle track state, populated fresh by pvBuildAudioTracks() /
    // pvBuildSubtitleTracks() on every /info response (initial load AND
    // audio-switch reload) — see pvSwitchAudioTrack() for why a switch is a
    // full reload rather than an in-place track change.
    audioTracks:null, selectedAudioIndex:null, selectedSubtitleIndex:-1,
    // Selectable streaming quality (max bitrate, bps; 0 = source/no cap).
    // Seeded from localStorage in initPlayerControls() — NOT here, because the
    // PV_QUALITY_* consts pvLoadQuality() reads are declared further down and
    // would be in their temporal-dead-zone at the moment this literal runs.
    maxBitrate: 0,
  };
  let _pvPendingSync = null; // a playback command that arrived before the video was ready — applied on 'loadedmetadata'

  // hls.js is vendored at /static/vendor/hls.min.js (same-origin, zero
  // external scripts) but lazy-loaded on first Play rather than in <head> —
  // it's ~400KB and most page loads never touch it. Cached Promise so a
  // second Play doesn't re-inject the script tag.
  let _hlsLoadPromise = null;
  function loadHls(){
    if(window.Hls) return Promise.resolve(window.Hls);
    if(_hlsLoadPromise) return _hlsLoadPromise;
    _hlsLoadPromise = new Promise((resolve, reject)=>{
      const s=document.createElement('script');
      s.src='/static/vendor/hls.min.js';
      s.onload=()=>{ window.Hls ? resolve(window.Hls) : reject(new Error('hls.js did not define window.Hls')); };
      s.onerror=()=>reject(new Error('hls.js failed to load'));
      document.head.appendChild(s);
    });
    return _hlsLoadPromise;
  }

  // Browser-generated DeviceProfile, adapted from Jellyfin's own
  // browserDeviceProfile.js canPlayType() probing technique (trimmed —
  // no browser.js/appSettings/userSettings imports, just direct feature
  // detection) so the profile reflects THIS browser accurately rather than
  // a static server-side guess. Posted fresh to /api/playback/info on every
  // Play; nothing here is cached across page loads.
  function buildDeviceProfile(){
    const v=document.createElement('video');
    const canPlay=type=>{ try{ return !!(v.canPlayType && v.canPlayType(type).replace('no','')); }catch(e){ return false; } };

    const videoCodecs=[];
    if(canPlay('video/mp4; codecs="avc1.640029"')||canPlay('video/mp4; codecs="avc1.42E01E"')) videoCodecs.push('h264');
    // Deliberately NOT advertising HEVC direct-play: canPlayType over-reports
    // HEVC support, and much of this library is Dolby Vision / HDR 10-bit HEVC
    // that browsers can't actually decode — so a claimed direct-play just fails
    // in the player. Let Jellyfin transcode HEVC -> H.264 instead (fast on NVENC,
    // verified browser-compatible). h264/vp9/vp8/av1 direct-play stays.
    if(canPlay('video/webm; codecs="vp9"')||canPlay('video/mp4; codecs="vp09.00.10.08"')) videoCodecs.push('vp9');
    if(canPlay('video/webm; codecs="vp8"')) videoCodecs.push('vp8');
    if(canPlay('video/mp4; codecs="av01.0.05M.08"')) videoCodecs.push('av1');

    const audioCodecs=[];
    if(canPlay('audio/mp4; codecs="mp4a.40.2"')) audioCodecs.push('aac');
    if(canPlay('audio/mpeg')) audioCodecs.push('mp3');
    if(canPlay('audio/webm; codecs="opus"')||canPlay('audio/ogg; codecs="opus"')) audioCodecs.push('opus');
    if(canPlay('audio/flac')||canPlay('audio/x-flac')) audioCodecs.push('flac');
    if(canPlay('audio/mp4; codecs="ac-3"')) audioCodecs.push('ac3');
    if(canPlay('audio/mp4; codecs="ec-3"')) audioCodecs.push('eac3');

    const DirectPlayProfiles=[];
    if(canPlay('video/mp4') && videoCodecs.length)
      DirectPlayProfiles.push({Container:'mp4,m4v', Type:'Video', VideoCodec:videoCodecs.join(','), AudioCodec:audioCodecs.join(',')});
    const webmVideo=videoCodecs.filter(c=>c==='vp8'||c==='vp9'||c==='av1');
    if(canPlay('video/webm') && webmVideo.length)
      DirectPlayProfiles.push({Container:'webm', Type:'Video', VideoCodec:webmVideo.join(','), AudioCodec:audioCodecs.filter(c=>c==='opus').join(',')});
    if(!DirectPlayProfiles.length)
      DirectPlayProfiles.push({Container:'mp4', Type:'Video', VideoCodec:'h264', AudioCodec:'aac'});

    // HLS/ts transcode fallback — matches the server's QSV h264/aac output.
    const TranscodingProfiles=[{
      Container:'ts', Type:'Video', AudioCodec:'aac,mp3', VideoCodec:'h264',
      Context:'Streaming', Protocol:'hls', MaxAudioChannels:'6',
      MinSegments:1, BreakOnNonKeyFrames:true,
    }];

    // Text subs offered as native <track> (vtt); anything else is re-encoded
    // server-side if Jellyfin chooses to, and image formats (pgssub/dvdsub)
    // are explicitly out of scope for v1 — see playback.py's subtitle
    // handling ("will re-encode / not shown").
    const SubtitleProfiles=[
      {Format:'vtt', Method:'External'}, {Format:'vtt', Method:'Hls'},
      {Format:'srt', Method:'External'}, {Format:'ass', Method:'Encode'},
      {Format:'ssa', Method:'Encode'}, {Format:'pgssub', Method:'Encode'},
      {Format:'dvdsub', Method:'Encode'},
    ];

    return {
      MaxStreamingBitrate:120000000, MaxStaticBitrate:100000000,
      MusicStreamingTranscodingBitrate:384000,
      DirectPlayProfiles, TranscodingProfiles, ContainerProfiles:[],
      CodecProfiles:[], SubtitleProfiles,
    };
  }

  const PV_ICONS = {
    play:'<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
    pause:'<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 5h4v14H7zM13 5h4v14h-4z"/></svg>',
    volume:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M17 8.5a5 5 0 0 1 0 7"/></svg>',
    muted:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M16 9l5 6M21 9l-5 6"/></svg>',
    cc:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M10 10.3c-.5-.4-1.1-.6-1.7-.6-1.3 0-2.3 1-2.3 2.3s1 2.3 2.3 2.3c.6 0 1.2-.2 1.7-.6M17 10.3c-.5-.4-1.1-.6-1.7-.6-1.3 0-2.3 1-2.3 2.3s1 2.3 2.3 2.3c.6 0 1.2-.2 1.7-.6"/></svg>',
    audio:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 10v4M9 6v12M13 9v6M17 5v14M21 10v4"/></svg>',
    fs:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M21 16v3a2 2 0 0 1-2 2h-3M8 21H5a2 2 0 0 1-2-2v-3"/></svg>',
    together:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="8.5" cy="8" r="3"/><path d="M2.5 19a6 6 0 0 1 12 0"/><circle cx="17" cy="9.5" r="2.4"/><path d="M14.8 12.2a4.6 4.6 0 0 1 6.7 4.1"/></svg>',
    quality:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a9 9 0 1 0 9 9"/><path d="M21 4l-2.5 2.5"/><path d="M12 12l4-2.5"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/></svg>',
  };

  // Streaming-quality presets for the player's quality menu. `bitrate` is the
  // max streaming bitrate in bps sent to /api/playback/info; 0 means "no cap"
  // (source quality — Jellyfin direct-plays or transcodes to the source rate).
  // Chosen to line up with familiar resolution tiers rather than exposing raw
  // Mbps to members. Persisted (see PV_QUALITY_KEY) so a choice sticks across
  // titles and sessions; the backend clamps anything out of range regardless.
  const PV_QUALITY_KEY = 'mf.streamQuality';
  const PV_QUALITY_PRESETS = [
    {bitrate:0,       label:'Auto',       sub:'Source quality'},
    {bitrate:8000000, label:'1080p',      sub:'High · 8 Mbps'},
    {bitrate:4000000, label:'720p',       sub:'4 Mbps'},
    {bitrate:1500000, label:'480p',       sub:'1.5 Mbps'},
    {bitrate:720000,  label:'Data saver', sub:'720 Kbps'},
  ];
  function pvLoadQuality(){
    // Only honour a stored value that still matches a known preset — a stale
    // localStorage entry from an old preset list falls back to Auto (0).
    let raw = 0;
    try{ raw = parseInt(localStorage.getItem(PV_QUALITY_KEY)||'0', 10) || 0; }catch(e){ raw = 0; }
    return PV_QUALITY_PRESETS.some(p=>p.bitrate===raw) ? raw : 0;
  }
  function pvQualityLabel(bitrate){
    const p = PV_QUALITY_PRESETS.find(q=>q.bitrate===bitrate);
    return p ? p.label : 'Auto';
  }

  function pvSetLoading(on, msg){
    const el=document.getElementById('pv-loading'); if(!el) return;
    if(msg){ el.hidden=false; el.textContent=msg; return; }
    el.hidden=!on;
    if(on) el.textContent='Loading…';
  }

  function startPlayback(){
    const it=window._currentItem; if(!it) return;
    if(it.type==='Series'){ openEpisodePicker(it); }
    else { _pv.seasons=null; _pv.activeSeason=null; playItem(it.id, it.title); }
  }

  // preselect: the text-track *position* (not the raw Jellyfin stream index —
  // see the button-building loop below) to restore as "on" — used when this
  // is a reload after an audio-track switch, not a fresh title. Defaults to
  // -1 (Off) for a genuinely fresh load, matching the old always-off behavior.
  function pvBuildSubtitleTracks(video, subtitles, preselect){
    Array.from(video.querySelectorAll('track')).forEach(t=>t.remove());
    const menu=document.getElementById('pv-submenu'), subBtn=document.getElementById('pv-subbtn');
    const hasAny = subtitles && subtitles.length;
    if(subBtn) subBtn.hidden=!hasAny;
    _pv.selectedSubtitleIndex=-1;
    if(!hasAny){ if(menu) menu.innerHTML=''; return; }
    const want = (preselect==null) ? -1 : preselect;
    let html=`<button type="button" onclick="pvSelectSubtitle(-1)" aria-pressed="${want===-1?'true':'false'}">Off</button>`;
    let pos=0;
    subtitles.forEach(s=>{
      const label=s.title||s.language||'Subtitle';
      if(s.url){
        const track=document.createElement('track');
        track.kind='subtitles'; track.label=label; track.srclang=s.language||''; track.src=s.url;
        video.appendChild(track);
        html+=`<button type="button" onclick="pvSelectSubtitle(${pos})" aria-pressed="${want===pos?'true':'false'}">${esc(label)}</button>`;
        pos++;
      } else {
        html+=`<button type="button" disabled title="${escA(s.note||'')}">${esc(label)} — not shown</button>`;
      }
    });
    if(menu) menu.innerHTML=html;
    const validWant = (want>=0 && want<pos) ? want : -1;
    Array.from(video.textTracks).forEach((tt,i)=>{ tt.mode=(i===validWant)?'showing':'disabled'; });
    _pv.selectedSubtitleIndex=validWant;
  }
  function pvSelectSubtitle(idx){
    const video=document.getElementById('pv-video'); if(!video) return;
    Array.from(video.textTracks).forEach((tt,i)=>{ tt.mode=(i===idx)?'showing':'disabled'; });
    _pv.selectedSubtitleIndex=idx;
    const menu=document.getElementById('pv-submenu');
    if(menu) Array.from(menu.querySelectorAll('button:not([disabled])')).forEach((b,i)=>{
      b.setAttribute('aria-pressed', (i-1)===idx ? 'true':'false'); // i=0 is "Off" (idx -1)
    });
    pvSetSubMenuOpen(false);
  }
  function pvCloseMenu(id){
    const menu=document.getElementById(id); if(menu) menu.classList.remove('on');
  }
  function pvSetSubMenuOpen(open){
    const menu=document.getElementById('pv-submenu'); if(menu) menu.classList.toggle('on', !!open);
    // opening the menu (a bottom sheet on mobile — see CSS) always keeps the
    // chrome visible; closing it resumes the normal auto-hide countdown.
    // Also closes the audio menu — the two share the same anchored corner
    // (and the same bottom-sheet slot on mobile), so only one is ever open.
    if(open){ pvCloseMenu('pv-audiomenu'); pvCloseMenu('pv-qualitymenu'); clearTimeout(_pv.hideTimer); const stage=document.getElementById('pv-stage'); if(stage) stage.classList.remove('controls-hidden'); }
    else pvScheduleAutoHide();
  }
  function pvToggleSubMenu(){
    const menu=document.getElementById('pv-submenu'); if(menu) pvSetSubMenuOpen(!menu.classList.contains('on'));
  }

  // ── audio track menu — mirrors the subtitle menu above, but selecting a
  // track means a real re-negotiated stream reload (pvSwitchAudioTrack),
  // not a free client-side switch — see playback.py's module docstring.
  function pvBuildAudioTracks(tracks, selectedIndex){
    const menu=document.getElementById('pv-audiomenu'), btn=document.getElementById('pv-audiobtn');
    const list = tracks || [];
    _pv.audioTracks = list;
    _pv.selectedAudioIndex = (selectedIndex==null && list.length) ? list[0].index : selectedIndex;
    // Only worth showing when there's an actual choice — one track means
    // nothing to switch to.
    const hasChoice = list.length > 1;
    if(btn) btn.hidden=!hasChoice;
    if(!hasChoice){ if(menu) menu.innerHTML=''; return; }
    const html = list.map(t=>{
      const bits=[t.language?t.language.toUpperCase():'', t.codec||'', t.channels?(t.channels+'ch'):''].filter(Boolean);
      const label = t.title || bits.join(' · ') || ('Track '+t.index);
      const pressed = t.index===_pv.selectedAudioIndex ? 'true':'false';
      return `<button type="button" onclick="pvSwitchAudioTrack(${t.index})" aria-pressed="${pressed}">${esc(label)}</button>`;
    }).join('');
    if(menu) menu.innerHTML=html;
  }
  function pvSetAudioMenuOpen(open){
    const menu=document.getElementById('pv-audiomenu'); if(menu) menu.classList.toggle('on', !!open);
    if(open){ pvCloseMenu('pv-submenu'); pvCloseMenu('pv-qualitymenu'); clearTimeout(_pv.hideTimer); const stage=document.getElementById('pv-stage'); if(stage) stage.classList.remove('controls-hidden'); }
    else pvScheduleAutoHide();
  }
  function pvToggleAudioMenu(){
    const menu=document.getElementById('pv-audiomenu'); if(menu) pvSetAudioMenuOpen(!menu.classList.contains('on'));
  }
  // Switching audio means Jellyfin has to renegotiate PlaybackInfo with the
  // new AudioStreamIndex baked into the resulting stream/manifest (see
  // playback.py's docstring for why this can't be an in-place swap like
  // subtitles). Preserves playhead position, play/pause state, and the
  // current subtitle selection across the reload.
  async function pvSwitchAudioTrack(idx){
    const video=document.getElementById('pv-video');
    if(!video || !_pv.itemId || idx===_pv.selectedAudioIndex){ pvSetAudioMenuOpen(false); return; }
    const resumeTicks=Math.round((video.currentTime||0)*1e7);
    const wasPaused=video.paused;
    const id=_pv.itemId;
    const titleEl=document.getElementById('pv-title');
    const title=(titleEl && titleEl.textContent) || '';
    const keepSub=_pv.selectedSubtitleIndex;
    // Tear down the OLD Jellyfin play session/transcode before negotiating a
    // new one — otherwise the abandoned transcode process lingers server-side
    // until Jellyfin's own idle timeout reaps it.
    if(_pv.itemId && _pv.playSessionId) jp('/api/playback/stopped', pvProgressPayload());
    pvSetAudioMenuOpen(false);
    await playItem(id, title, resumeTicks, {audioStreamIndex: idx, preserveSubtitle: keepSub, autoplay: !wasPaused});
  }

  // ── streaming-quality menu — same anchored-corner menu pattern as audio/
  // subtitles. Like audio (and unlike subtitles), changing quality is a real
  // re-negotiated stream reload: MaxStreamingBitrate is baked into the
  // stream/transcode Jellyfin hands back, so a fresh /info call is the only
  // way to change it (see pvSelectQuality). Always shown — every stream has a
  // quality to pick, so there's no "only one choice" hide like audio has.
  function pvBuildQualityMenu(){
    const menu=document.getElementById('pv-qualitymenu'); if(!menu) return;
    menu.innerHTML = PV_QUALITY_PRESETS.map(p=>{
      const pressed = p.bitrate===_pv.maxBitrate ? 'true':'false';
      return `<button type="button" onclick="pvSelectQuality(${p.bitrate})" aria-pressed="${pressed}">`
        + `${esc(p.label)}<span class="pv-menu-sub">${esc(p.sub)}</span></button>`;
    }).join('');
  }
  function pvSetQualityMenuOpen(open){
    const menu=document.getElementById('pv-qualitymenu'); if(menu) menu.classList.toggle('on', !!open);
    if(open){ pvCloseMenu('pv-submenu'); pvCloseMenu('pv-audiomenu'); clearTimeout(_pv.hideTimer); const stage=document.getElementById('pv-stage'); if(stage) stage.classList.remove('controls-hidden'); }
    else pvScheduleAutoHide();
  }
  function pvToggleQualityMenu(){
    const menu=document.getElementById('pv-qualitymenu'); if(menu) pvSetQualityMenuOpen(!menu.classList.contains('on'));
  }
  async function pvSelectQuality(bitrate){
    // Persist first so the choice sticks even if nothing is playing yet (the
    // menu is reachable the moment the player opens) and so the NEXT title
    // launched already honours it.
    _pv.maxBitrate = bitrate;
    try{ localStorage.setItem(PV_QUALITY_KEY, String(bitrate)); }catch(e){}
    pvBuildQualityMenu();  // refresh the pressed state
    pvSetQualityMenuOpen(false);
    // If something's actually playing, re-negotiate at the current position —
    // mirrors pvSwitchAudioTrack exactly (tear the old transcode down first,
    // preserve playhead / play-pause / audio / subtitle across the reload).
    const video=document.getElementById('pv-video');
    if(!video || !_pv.itemId) return;
    const resumeTicks=Math.round((video.currentTime||0)*1e7);
    const wasPaused=video.paused;
    const id=_pv.itemId;
    const titleEl=document.getElementById('pv-title');
    const title=(titleEl && titleEl.textContent) || '';
    if(_pv.itemId && _pv.playSessionId) jp('/api/playback/stopped', pvProgressPayload());
    await playItem(id, title, resumeTicks, {
      audioStreamIndex: _pv.selectedAudioIndex, preserveSubtitle: _pv.selectedSubtitleIndex,
      autoplay: !wasPaused,
    });
  }

  function pvBufferedFrac(v){
    if(!v || !v.duration || !isFinite(v.duration) || !v.buffered || !v.buffered.length) return 0;
    for(let i=0;i<v.buffered.length;i++){
      if(v.buffered.start(i)<=v.currentTime && v.currentTime<=v.buffered.end(i)) return v.buffered.end(i)/v.duration;
    }
    return v.buffered.end(v.buffered.length-1)/v.duration;
  }
  let _pvDragging=false;
  function pvRenderProgress(v){
    if(!v) return;
    const dur=(v.duration&&isFinite(v.duration))?v.duration:0, cur=v.currentTime||0;
    const frac=dur?Math.min(1,cur/dur):0;
    const played=document.getElementById('pv-played'), knob=document.getElementById('pv-knob'),
          buffered=document.getElementById('pv-buffered');
    if(played) played.style.width=(frac*100)+'%';
    if(knob) knob.style.left=(frac*100)+'%';
    if(buffered) buffered.style.width=(pvBufferedFrac(v)*100)+'%';
    const curEl=document.getElementById('pv-cur'), durEl=document.getElementById('pv-dur');
    if(curEl) curEl.textContent=fmtT(cur);
    if(durEl) durEl.textContent=fmtT(dur);
    // Keep the ARIA slider values in step with the visual bar. The element is
    // role="slider" + tabindex="0", so assistive tech will announce it on focus
    // and on change — with no aria-valuenow it announced position-less, which
    // is arguably worse than not exposing it as a slider at all. Values are in
    // whole seconds (the visual bar keeps sub-second precision); valuetext is
    // what actually gets spoken.
    const track=document.getElementById('pv-track');
    if(track){
      track.setAttribute('aria-valuemin','0');
      track.setAttribute('aria-valuemax', String(Math.round(dur)));
      track.setAttribute('aria-valuenow', String(Math.round(cur)));
      track.setAttribute('aria-valuetext', dur ? `${fmtT(cur)} of ${fmtT(dur)}` : fmtT(cur));
    }
  }
  function pvScrubTo(v, frac){
    if(!v || !v.duration || !isFinite(v.duration)) return;
    v.currentTime=frac*v.duration;
    pvRenderProgress(v);
  }
  function pvPosFromEvent(e, rect){
    const x=(e.touches?e.touches[0].clientX:e.clientX)-rect.left;
    return Math.min(1, Math.max(0, x/rect.width));
  }
  function pvUpdatePlayIcon(){
    const v=document.getElementById('pv-video'), btn=document.getElementById('pv-playbtn');
    if(!v||!btn) return;
    btn.innerHTML = v.paused ? PV_ICONS.play : PV_ICONS.pause;
    btn.setAttribute('aria-label', v.paused ? 'Play' : 'Pause');
  }
  function pvTogglePlay(){
    const v=document.getElementById('pv-video'); if(!v) return;
    if(v.paused) v.play().catch(()=>{}); else v.pause();
  }
  function pvToggleMute(){
    const v=document.getElementById('pv-video'); if(!v) return;
    v.muted=!v.muted;
    if(!v.muted && v.volume===0) v.volume=1;
  }
  function pvToggleFullscreen(){
    const stage=document.getElementById('pv-stage'); if(!stage) return;
    if(document.fullscreenElement) document.exitFullscreen().catch(()=>{});
    else if(stage.requestFullscreen) stage.requestFullscreen().catch(()=>{});
  }

  // ── Watch-together playback sync ─────────────────────────────────────────
  // Host-only, as decided: only the host's own <video> events ever call
  // /api/together/room/{code}/command (together.py rejects a non-host call
  // server-side regardless — this is a UX nicety, not the real gate).
  // Guests never call it; their controls stay follow-only, driven entirely
  // by the SSE `playback` events _pvApplyRemoteCommand() below applies.
  //
  // Wired onto the video element's own play/pause/seeked events (in
  // initPlayerControls(), not onto the button handlers) so EVERY source of
  // a state change broadcasts — the play button, autoplay-on-load, a
  // keyboard seek, a scrubber drag — not just the ones a caller happened to
  // remember to instrument individually.
  //
  // Sends whatever's ACTUALLY on-screen right now (_pv.itemId/#pv-title),
  // every time — not gated on it matching the room's last-known item.
  // Earlier this used a strict-equality guard against window._room.itemId,
  // which silently dropped every series broadcast: hostRoom() seeds the
  // room from window._currentItem (the *series*, from the detail page),
  // but the player's real id is the *episode*, so the two could never
  // match and nothing ever synced. together.py now treats item_id/
  // item_title/runtime_min on every command as "update the room to match,"
  // so this just always tells the truth about what's playing — which also
  // means switching episodes (or, in principle, any title) mid-room now
  // just works, closing the "host switches titles" gap flagged earlier.
  function _pvHostBroadcast(action){
    if(!window._room || !window._room.isHost || !window._room.code) return;
    if(!_pv.itemId) return; // nothing loaded yet
    const v=document.getElementById('pv-video'); if(!v) return;
    const position_ticks = Math.round((v.currentTime||0)*1e7);
    const titleEl=document.getElementById('pv-title');
    const item_title = (titleEl && titleEl.textContent) || window._room.itemTitle || null;
    const runtime_min = (v.duration && isFinite(v.duration)) ? Math.round(v.duration/60) : null;
    // Optimistic local update: the host never applies its own SSE
    // broadcasts back to itself (see _pvApplyRemoteCommand's guard), so
    // without this the host's OWN lobby view would keep showing the old
    // item/runtime after switching episodes until... never, nothing would
    // ever correct it locally.
    if(window._room.itemId !== _pv.itemId){
      window._room.itemId = _pv.itemId;
      window._room.itemTitle = item_title;
      if(runtime_min) window._room.runtimeMin = runtime_min;
      renderLobby();
    }
    fetch('/api/together/room/'+encodeURIComponent(window._room.code)+'/command', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action, position_ticks, item_id:_pv.itemId, item_title, runtime_min}),
    }).catch(()=>{});
  }
  // Seeks fire once per completed scrub, but a drag can complete many in
  // quick succession (pvScrubTo runs on every pointermove) — debounced so a
  // drag sends one command on release, not one per pixel.
  let _pvSeekBroadcastTimer=null;
  function _pvHostBroadcastSeek(){
    clearTimeout(_pvSeekBroadcastTimer);
    _pvSeekBroadcastTimer=setTimeout(()=>_pvHostBroadcast('seek'), 250);
  }

  // ── Sync tolerance ────────────────────────────────────────────────────────
  // Two different kinds of position update land here, and they need
  // different tolerances. An explicit host action (play/pause/seek, an SSE
  // `playback` event, or the pending-sync flush right after a guest's
  // player finishes loading) is a deliberate moment and SHOULD land
  // precisely - COMMAND_SYNC_TOLERANCE_SECS stays sub-second, just enough
  // to not fight ordinary timeupdate jitter. Passive, continuous drift
  // (two independent HLS sessions gradually diverging over minutes with no
  // explicit host action in between - see _syncFromNowPlaying, below) is
  // NOT a deliberate moment, and hard-correcting it is exactly what caused
  // the "additional viewer stutters" complaint: once someone's roughly
  // caught up, re-seeking (and re-buffering) them every time they drift
  // past a hair-trigger threshold is worse than leaving them alone and
  // letting ordinary playback close the rest of the gap on its own.
  // PASSIVE_DRIFT_TOLERANCE_SECS is deliberately loose and configurable -
  // default sits in the middle of the 30-60s range this was tuned for.
  const COMMAND_SYNC_TOLERANCE_SECS = 0.75;
  const PASSIVE_DRIFT_TOLERANCE_SECS = 45;

  // Applies an incoming room.playback state (from the SSE `snapshot` or
  // `playback` event, a fresh /host//join/room response, or the passive
  // Now-Playing-derived drift check below) to a guest's own <video> — never
  // the host's, whose player is the source of truth, not a follower (this
  // guard is also what makes the host's own SSE subscription, which DOES
  // receive its own broadcasts back, harmless rather than a feedback loop).
  // If the guest's player isn't ready yet (still loading, or hasn't been
  // launched at all — see _pvMaybeAutoJoinHostTitle), the command is
  // stashed in _pvPendingSync and flushed by initPlayerControls()'s
  // 'loadedmetadata' listener instead, always at the tight tolerance
  // (opts is intentionally dropped on that path — see the comment there).
  function _pvApplyRemoteCommand(d, opts){
    if(!d || !window._room || window._room.isHost) return;
    const v=document.getElementById('pv-video');
    if(!v || !_pv.itemId || !v.duration || !isFinite(v.duration)){
      _pvPendingSync = d;
      return;
    }
    _pvPendingSync = null;
    _pv.applyingRemote = true;
    try{
      if(d.position_ticks!=null){
        // Extrapolated forward from d.updated_at when the room's status is
        // "playing" - relevant on a `snapshot` (fresh connect or an SSE
        // reconnect after a network blip) or a Now-Playing-derived passive
        // check, either of which can be a few seconds stale by the time
        // they're applied; a live `playback` event's updated_at is only
        // ever a few ms old, so this is a no-op there.
        let targetSecs = d.position_ticks/1e7;
        if(d.status==='playing' && d.updated_at) targetSecs += Math.max(0, Date.now()/1000 - d.updated_at);
        const tolerance = (opts && opts.toleranceSecs!=null) ? opts.toleranceSecs : COMMAND_SYNC_TOLERANCE_SECS;
        if(Math.abs((v.currentTime||0) - targetSecs) > tolerance) v.currentTime = targetSecs;
      }
      if(d.status==='playing' && v.paused) v.play().catch(()=>{});
      else if(d.status==='paused' && !v.paused) v.pause();
    } finally {
      setTimeout(()=>{ _pv.applyingRemote=false; }, 50);
    }
  }

  // Guest entry: joining (or reconnecting into) a room jumps straight to
  // the host's current title, on the guest's OWN /api/playback/* stream —
  // streaming stays fully per-participant (their own Jellyfin session,
  // their own transcode/direct-play negotiation); only the play/pause/seek
  // *state* is ever synced, never the media itself. A no-op if the guest's
  // player is already showing that exact item (avoids re-launching/
  // re-teardown on every roster tick).
  //
  // Seeds the FIRST seek with the room's real (extrapolated) position via
  // playItem()'s existing startTicks argument, rather than letting it
  // default to the guest's own — irrelevant here — Jellyfin resume history.
  // This is the actual root cause of the join-time stutter this session
  // diagnosed and fixed: without it, the guest's video started at THEIR
  // last-left-off position, began playing, and was then immediately
  // hard-corrected to the host's position by _pvApplyRemoteCommand's
  // pending-sync flush — two seeks back to back, each forcing a real HLS
  // re-buffer. startTicks is the same mechanism the episode picker's own
  // resume feature already uses, so there's only ever one seek, to the
  // right place, from the start.
  function _pvMaybeAutoJoinHostTitle(room){
    if(!room || room.isHost || !room.itemId) return;
    if(_pv.itemId === room.itemId) return;
    const pb = room.playback;
    let startTicks = (pb && pb.position_ticks) || 0;
    if(pb && pb.status==='playing' && pb.updated_at){
      startTicks += Math.round(Math.max(0, Date.now()/1000 - pb.updated_at) * 1e7);
    }
    playItem(room.itemId, room.itemTitle||'', startTicks);
  }

  // Wires the <video> element + control bar once; safe to call repeatedly
  // (idempotent) since playItem() re-loads sources into the same elements
  // rather than recreating them.
  function initPlayerControls(){
    if(initPlayerControls._done) return;
    initPlayerControls._done=true;
    const v=document.getElementById('pv-video'); if(!v) return;
    v.addEventListener('timeupdate', ()=>{ if(!_pvDragging) pvRenderProgress(v); });
    v.addEventListener('progress', ()=>pvRenderProgress(v));
    v.addEventListener('loadedmetadata', ()=>{
      pvRenderProgress(v);
      // Flush a watch-together sync that arrived while this video was still
      // loading (the common case right after _pvMaybeAutoJoinHostTitle
      // launches a guest into the host's title) — see _pvApplyRemoteCommand.
      if(_pvPendingSync){ const d=_pvPendingSync; _pvPendingSync=null; _pvApplyRemoteCommand(d); }
    });
    v.addEventListener('play', pvUpdatePlayIcon);
    v.addEventListener('pause', pvUpdatePlayIcon);
    // Watch-together: host broadcasts on every play/pause/seek; guests never
    // do (their controls are the mock's follow-only status mirror, not
    // this real control bar's events) — see _pvHostBroadcast's own isHost
    // guard, which makes these three listeners a safe no-op for everyone
    // else rather than something that needs its own registration gate here.
    v.addEventListener('play', ()=>_pvHostBroadcast('play'));
    v.addEventListener('pause', ()=>_pvHostBroadcast('pause'));
    v.addEventListener('seeked', _pvHostBroadcastSeek);
    v.addEventListener('ended', ()=>stopPlayback());
    v.addEventListener('volumechange', ()=>{
      const btn=document.getElementById('pv-mutebtn');
      if(btn) btn.innerHTML=(v.muted||v.volume===0) ? PV_ICONS.muted : PV_ICONS.volume;
      const vol=document.getElementById('pv-vol');
      if(vol && document.activeElement!==vol) vol.value=v.muted?0:v.volume;
    });
    const playBtn=document.getElementById('pv-playbtn'); if(playBtn) playBtn.innerHTML=PV_ICONS.play;
    const muteBtn=document.getElementById('pv-mutebtn'); if(muteBtn) muteBtn.innerHTML=PV_ICONS.volume;
    const subBtn=document.getElementById('pv-subbtn'); if(subBtn) subBtn.innerHTML=PV_ICONS.cc;
    const audBtn=document.getElementById('pv-audiobtn'); if(audBtn) audBtn.innerHTML=PV_ICONS.audio;
    const fsBtn=document.getElementById('pv-fsbtn'); if(fsBtn) fsBtn.innerHTML=PV_ICONS.fs;
    const togetherBtn=document.getElementById('pv-togetherbtn'); if(togetherBtn) togetherBtn.innerHTML=PV_ICONS.together;
    const qBtn=document.getElementById('pv-qualitybtn'); if(qBtn) qBtn.innerHTML=PV_ICONS.quality;
    // Seed the persisted quality choice now (safe: the PV_QUALITY_* consts are
    // initialized by the time any control is wired) and build the menu once.
    _pv.maxBitrate = pvLoadQuality();
    pvBuildQualityMenu();

    const vol=document.getElementById('pv-vol');
    if(vol) vol.addEventListener('input', ()=>{ v.volume=parseFloat(vol.value); v.muted=(v.volume===0); });

    const track=document.getElementById('pv-track');
    if(track){
      const onDown=e=>{ _pvDragging=true; pvScrubTo(v, pvPosFromEvent(e, track.getBoundingClientRect())); e.preventDefault(); };
      const onMove=e=>{ if(_pvDragging) pvScrubTo(v, pvPosFromEvent(e, track.getBoundingClientRect())); };
      const onUp=()=>{ _pvDragging=false; };
      track.addEventListener('pointerdown', onDown);
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
      track.addEventListener('keydown', e=>{
        if(!v.duration||!isFinite(v.duration)) return;
        if(e.key==='ArrowRight'){ v.currentTime=Math.min(v.duration, v.currentTime+5); pvRenderProgress(v); }
        else if(e.key==='ArrowLeft'){ v.currentTime=Math.max(0, v.currentTime-5); pvRenderProgress(v); }
      });
    }

    // Outside-click closes an open menu. Use btn.contains(e.target), NOT
    // e.target!==btn: the toolbar buttons wrap an <svg><path>, so a click
    // landing ON the icon has e.target === that inner SVG element, not the
    // button. With the old !==btn test that read as an *outside* click, so the
    // menu the button's own onclick had just opened was closed again in the
    // same click — the button appeared dead when clicked directly on the icon
    // (only the bare padding above it worked). contains() treats the icon as
    // part of its button, so the toggle sticks wherever you click it.
    document.addEventListener('click', e=>{
      const menu=document.getElementById('pv-submenu'), btn=document.getElementById('pv-subbtn');
      if(menu && menu.classList.contains('on') && btn && !btn.contains(e.target) && !menu.contains(e.target)) pvSetSubMenuOpen(false);
      const audMenu=document.getElementById('pv-audiomenu'), audBtn=document.getElementById('pv-audiobtn');
      if(audMenu && audMenu.classList.contains('on') && audBtn && !audBtn.contains(e.target) && !audMenu.contains(e.target)) pvSetAudioMenuOpen(false);
      const qMenu=document.getElementById('pv-qualitymenu'), qBtn=document.getElementById('pv-qualitybtn');
      if(qMenu && qMenu.classList.contains('on') && qBtn && !qBtn.contains(e.target) && !qMenu.contains(e.target)) pvSetQualityMenuOpen(false);
    });

    const picker=document.getElementById('pv-picker');
    if(picker) picker.addEventListener('click', e=>{
      const btn=e.target.closest('.pv-ep'); if(!btn) return;
      pvPlayEpisode(btn.dataset.id, btn.dataset.title, parseInt(btn.dataset.resume||'0', 10));
    });

    // Auto-hide chrome while playing (mobile/landscape especially — see
    // .controls-hidden in CSS): any interaction on the stage resets a 3s
    // idle timer; pausing always shows it back. Tapping the bare stage
    // (not a button/track) toggles the chrome directly, matching the
    // tap-to-toggle behavior mobile viewers expect.
    const stage=document.getElementById('pv-stage');
    if(stage){
      ['pointermove','pointerdown','touchstart'].forEach(evt=>
        stage.addEventListener(evt, pvShowControls, {passive:true}));
      stage.addEventListener('click', e=>{
        if(e.target!==stage && e.target.id!=='pv-video') return; // ignore clicks on the control bar itself
        if(stage.classList.contains('controls-hidden')) pvShowControls();
        else { clearTimeout(_pv.hideTimer); stage.classList.add('controls-hidden'); }
      });
    }
    v.addEventListener('play', pvScheduleAutoHide);
    v.addEventListener('pause', pvShowControls);

    (function(){ const lb=document.getElementById('playerlightbox'); if(lb) lb.addEventListener('click', e=>{ if(e.target===lb) closePlayer(); }); })();
  }
  function pvShowControls(){
    clearTimeout(_pv.hideTimer);
    const stage=document.getElementById('pv-stage'); if(stage) stage.classList.remove('controls-hidden');
    pvScheduleAutoHide();
  }
  function pvScheduleAutoHide(){
    clearTimeout(_pv.hideTimer);
    const v=document.getElementById('pv-video'), menu=document.getElementById('pv-submenu'), audMenu=document.getElementById('pv-audiomenu'), qMenu=document.getElementById('pv-qualitymenu');
    if(!v || v.paused || (menu && menu.classList.contains('on')) || (audMenu && audMenu.classList.contains('on')) || (qMenu && qMenu.classList.contains('on'))) return;
    _pv.hideTimer=setTimeout(()=>{
      const stage=document.getElementById('pv-stage'); if(stage) stage.classList.add('controls-hidden');
    }, 3000);
  }

  function pvSeekAndPlay(video, resumeTicks, autoplay){
    if(resumeTicks && resumeTicks>0){
      const secs=resumeTicks/1e7;
      if(isFinite(secs) && secs>0) video.currentTime=secs;
    }
    // autoplay defaults to true (existing callers all pass nothing/undefined
    // and expect the old always-play behavior); pvSwitchAudioTrack passes
    // false explicitly to respect a paused video across the reload.
    if(autoplay!==false) video.play().catch(()=>{});
  }

  // opts (all optional): {audioStreamIndex, preserveSubtitle, autoplay} —
  // set by pvSwitchAudioTrack() when this call is a mid-playback audio
  // reload rather than a fresh title. Every existing call site (episode
  // picker, resume cards, watch-together auto-join, command palette) omits
  // opts entirely and gets the original fresh-load behavior unchanged.
  async function playItem(id, title, startTicks, opts){
    opts = opts || {};
    initPlayerControls();
    const lb=document.getElementById('playerlightbox'), video=document.getElementById('pv-video'),
          stage=document.getElementById('pv-stage'), picker=document.getElementById('pv-picker'),
          backBtn=document.getElementById('pv-backep');
    if(!lb || !video) return;
    pvTeardown();
    if(picker) picker.hidden=true;
    if(stage){ stage.hidden=false; stage.classList.toggle('has-backep', !!_pv.seasons); }
    if(backBtn) backBtn.hidden=!_pv.seasons;
    const titleEl=document.getElementById('pv-title'); if(titleEl) titleEl.textContent=title||'';
    lb.classList.add('on');
    pvSetLoading(true);

    // Selectable streaming quality: mirror the chosen cap into the device
    // profile (Jellyfin weighs the profile's MaxStreamingBitrate when deciding
    // whether to transcode) AND pass it as max_bitrate. 0/undefined = no cap;
    // the backend clamps and re-asserts it regardless. See PV_QUALITY_PRESETS.
    const profile=buildDeviceProfile();
    if(_pv.maxBitrate>0) profile.MaxStreamingBitrate=_pv.maxBitrate;
    const info = await jp('/api/playback/info', {
      item_id:id, device_profile:profile, start_ticks:startTicks||0,
      audio_stream_index: (opts.audioStreamIndex!=null ? opts.audioStreamIndex : null),
      max_bitrate: _pv.maxBitrate||0,
    });
    // A real "not logged in" (session expired/lapsed) is not a playback
    // error — leaving "Not logged in" parked in the video overlay reads like
    // a broken player. noteAuthFailure() (already fired inside jp()) is
    // already bringing up the real sign-in gate + toast underneath, so just
    // close the player cleanly instead of showing a cryptic in-player error.
    if(info && info._status===401){ stopPlayback(); return; }
    if(!info || !info.ok || !info.url){
      pvSetLoading(false, (info && info.error) ? info.error : 'Playback unavailable right now.');
      return;
    }

    _pv.itemId=info.item_id; _pv.mediaSourceId=info.media_source_id; _pv.playSessionId=info.play_session_id;
    pvBuildSubtitleTracks(video, info.subtitles||[], opts.preserveSubtitle!=null ? opts.preserveSubtitle : -1);
    pvBuildAudioTracks(info.audio_tracks||[], info.selected_audio_index!=null ? info.selected_audio_index : null);

    const autoplay = opts.autoplay!==false;
    const isNativeHls = video.canPlayType('application/vnd.apple.mpegurl');
    if(info.mode==='hls' && !isNativeHls){
      try{
        const Hls=await loadHls();
        if(!Hls.isSupported()){ pvSetLoading(false, 'HLS is not supported in this browser.'); return; }
        const hls=new Hls();
        _pv.hls=hls;
        hls.on(Hls.Events.MANIFEST_PARSED, ()=>{ pvSetLoading(false); pvSeekAndPlay(video, info.resume_ticks, autoplay); });
        hls.on(Hls.Events.ERROR, (evt, data)=>{ if(data && data.fatal) pvSetLoading(false, 'Playback error.'); });
        hls.loadSource(info.url);
        hls.attachMedia(video);
      }catch(e){
        pvSetLoading(false, 'Could not load the video player.');
        return;
      }
    } else {
      video.src=info.url;
      video.addEventListener('loadedmetadata', function onMeta(){
        video.removeEventListener('loadedmetadata', onMeta);
        pvSetLoading(false); pvSeekAndPlay(video, info.resume_ticks, autoplay);
      });
    }

    startProgressSync();
  }

  function pvProgressPayload(){
    const v=document.getElementById('pv-video');
    return {
      item_id:_pv.itemId, play_session_id:_pv.playSessionId,
      position_ticks: v ? Math.round((v.currentTime||0)*1e7) : 0,
      is_paused: v ? v.paused : false,
      media_source_id:_pv.mediaSourceId,
    };
  }
  function pvSendPlaying(url){
    if(!_pv.itemId || !_pv.playSessionId) return;
    jp(url, pvProgressPayload());
  }
  function startProgressSync(){
    stopProgressSyncTimer();
    pvSendPlaying('/api/playback/start');
    _pv.syncTimer=setInterval(()=>pvSendPlaying('/api/playback/progress'), 10000);
  }
  function stopProgressSyncTimer(){
    if(_pv.syncTimer){ clearInterval(_pv.syncTimer); _pv.syncTimer=null; }
  }
  // Teardown reliability on tab close: fetch is unreliable during
  // pagehide/beforeunload, so this is sendBeacon-only. sendBeacon carries
  // the same-origin session cookie, so _require_session still authenticates
  // it server-side. This is what actually tears down a live transcode if
  // the user just closes the tab instead of hitting the close button.
  function pvBeaconStop(){
    if(!_pv.itemId || !_pv.playSessionId) return;
    try{
      const blob=new Blob([JSON.stringify(pvProgressPayload())], {type:'application/json'});
      navigator.sendBeacon('/api/playback/stopped', blob);
    }catch(e){}
  }
  addEventListener('pagehide', pvBeaconStop);
  addEventListener('beforeunload', pvBeaconStop);

  function pvTeardown(){
    stopProgressSyncTimer();
    const v=document.getElementById('pv-video');
    if(_pv.hls){ try{ _pv.hls.destroy(); }catch(e){} _pv.hls=null; }
    if(v){
      try{ v.pause(); }catch(e){}
      v.removeAttribute('src'); v.src='';
      Array.from(v.querySelectorAll('track')).forEach(t=>t.remove());
      try{ v.load(); }catch(e){}
    }
    _pv.itemId=null; _pv.playSessionId=null; _pv.mediaSourceId=null;
    pvSetSubMenuOpen(false);
    pvSetAudioMenuOpen(false);
    clearTimeout(_pv.hideTimer);
    const stage=document.getElementById('pv-stage'); if(stage) stage.classList.remove('controls-hidden');
  }
  function stopPlayback(){
    if(_pv.itemId && _pv.playSessionId) jp('/api/playback/stopped', pvProgressPayload());
    pvTeardown();
    const lb=document.getElementById('playerlightbox'); if(lb) lb.classList.remove('on');
  }
  function closePlayer(){ stopPlayback(); }

  // ── series episode picker ────────────────────────────────────────────────
  async function openEpisodePicker(it){
    initPlayerControls();
    const lb=document.getElementById('playerlightbox'), stage=document.getElementById('pv-stage'),
          picker=document.getElementById('pv-picker');
    if(!lb || !picker) return;
    pvTeardown();
    if(stage) stage.hidden=true;
    picker.hidden=false;
    picker.innerHTML='<p class="libstatus">Loading episodes…</p>';
    lb.classList.add('on');
    const d = await jp('/api/playback/episodes?series_id='+encodeURIComponent(it.id));
    // Same reasoning as playItem(): a lapsed session isn't "no episodes",
    // it's "you got signed out" — the real gate/toast is already coming up
    // via noteAuthFailure(), so just close the picker instead of claiming
    // the series has no episodes.
    if(d && d._status===401){ stopPlayback(); return; }
    if(!d || !d.ok || !d.seasons || !d.seasons.length){
      picker.innerHTML='<p class="libstatus">No episodes found.</p>';
      return;
    }
    _pv.seasons=d.seasons;
    renderEpisodePicker(d.seasons[0].season);
  }
  function renderEpisodePicker(activeSeason){
    _pv.activeSeason=activeSeason;
    const picker=document.getElementById('pv-picker'); if(!picker || !_pv.seasons) return;
    const seasonBtns=_pv.seasons.map(s=>{
      const label = s.season==null ? 'Specials' : ('Season '+s.season);
      const pressed = s.season===activeSeason ? 'true' : 'false';
      const arg = s.season==null ? 'null' : s.season;
      return `<button type="button" class="pv-season-btn" aria-pressed="${pressed}" onclick="renderEpisodePicker(${arg})">${esc(label)}</button>`;
    }).join('');
    const season=_pv.seasons.find(s=>s.season===activeSeason) || _pv.seasons[0];
    const eps=(season && season.episodes) || [];
    const epHtml=eps.map(ep=>{
      const se = ep.episode_no!=null ? ('E'+String(ep.episode_no).padStart(2,'0')) : '';
      const thumb = ep.tag
        ? `<img src="/api/watching/poster?id=${encodeURIComponent(ep.id)}&tag=${encodeURIComponent(ep.tag)}&h=200" loading="lazy" alt="">`
        : '';
      const resumePct = (ep.resume_ticks && ep.run_time_ticks) ? Math.min(100, Math.round(ep.resume_ticks/ep.run_time_ticks*100)) : 0;
      return `<button type="button" class="pv-ep" data-id="${escA(ep.id)}" data-title="${escA(ep.title||'')}" data-resume="${ep.resume_ticks||0}">`+
        `<div class="pv-ep-thumb">${thumb}</div>`+
        `<div class="pv-ep-body"><div class="pv-ep-title">${se?`<span class="se">${se}</span>`:''}${esc(ep.title||'')}</div>`+
        `<div class="pv-ep-meta">${ep.runtime_min?ep.runtime_min+' min':''}</div>`+
        `${ep.overview?`<div class="pv-ep-ov">${esc(ep.overview)}</div>`:''}`+
        `${resumePct?`<div class="pv-ep-resume"><i style="width:${resumePct}%"></i></div>`:''}`+
        `</div></button>`;
    }).join('');
    picker.innerHTML = `<div class="pv-seasons">${seasonBtns}</div>`+
      `<div class="pv-eplist">${epHtml || '<p class="libstatus">No episodes in this season.</p>'}</div>`;
  }
  function pvPlayEpisode(id, title, resumeTicks){
    const picker=document.getElementById('pv-picker'), stage=document.getElementById('pv-stage');
    if(picker) picker.hidden=true;
    if(stage) stage.hidden=false;
    playItem(id, title, resumeTicks);
  }
  function pvBackToEpisodes(){
    if(_pv.itemId && _pv.playSessionId) jp('/api/playback/stopped', pvProgressPayload());
    pvTeardown();
    const stage=document.getElementById('pv-stage'), picker=document.getElementById('pv-picker');
    if(stage) stage.hidden=true;
    if(picker) picker.hidden=false;
    if(_pv.seasons) renderEpisodePicker(_pv.activeSeason!=null ? _pv.activeSeason : _pv.seasons[0].season);
  }

  // Escape closes only the topmost open overlay: player (90) over login/detail (80).
  function handleEscape(){
    // Header popovers (appearance/account) sit at the top of the stack visually
    // but are lightweight — close them first if any is open.
    if(document.querySelector('.hmenu .menu:not([hidden])')){ closeMenus(); return; }
    // ⌘K sits above every other overlay (z-index 110) and can be opened
    // from on top of any of them, so it must be the first thing Escape
    // checks — otherwise Escape while ⌘K is open over, say, the player
    // would close the player underneath instead of the palette itself.
    if(window._cmdk && window._cmdk.open){ closeCmdK(); return; }
    const together=document.getElementById('togetherlightbox');
    if(together && together.classList.contains('on')){ closeWatchTogether(); return; }
    const player=document.getElementById('playerlightbox');
    if(player && player.classList.contains('on')){ closePlayer(); return; }
    const login=document.getElementById('loginlightbox');
    if(login && login.classList.contains('on')){ closeLogin(); return; }
    const instrument=document.getElementById('instrumentlightbox');
    if(instrument && instrument.classList.contains('on')){ closeInstrument(); return; }
    const film=document.getElementById('lightbox');
    if(film && film.classList.contains('on')){ closeFilm(); return; }
  }
  document.addEventListener('keydown', e=>{ if(e.key==='Escape') handleEscape(); });

  // popular-to-request rail (Jellyseerr trending; deep-links into Jellyseerr to request)
  //
  // Nocturne Stage 2 decision point: the mockup's request breadcrumb chip
  // (Requested › Approved › Downloading 68% › Ready) is NOT wired here.
  // Checked discover.py's one endpoint (/api/request/trending) and Jellyseerr's
  // own API surface hub already talks to: the only status data available is
  // an AGGREGATE, coarse enum per trending title - none/pending/processing/
  // partial/available (already surfaced below as the binary .pstatus "Requested"
  // vs "Request →" badge on each card) - not a per-user request history, not a
  // 4-step pipeline, and no live download percentage anywhere in this stack.
  // Building that would mean new backend work (mapping a hub session to a
  // Jellyseerr user, plus a new endpoint) - real scope beyond "wire it if it
  // exists." Rather than fabricate steps/a percentage that isn't real
  // (actively misleading - "Downloading 68%" reads as precise telemetry),
  // this degrades to simply not rendering the chip. .reqchip's CSS is in
  // place and ready the moment a real per-request data source exists.
  async function loadPopular(){
    const grid=document.getElementById('popgrid'); if(!grid) return;
    const d=await j('/api/request/trending?limit=16');
    const items=(d.ok&&d.items)?d.items:[];
    if(!items.length) return;
    document.getElementById('popular').classList.add('on');
    grid.innerHTML=items.map(it=>{
      const yr=it.year||''; const kind=it.type==='tv'?'Series':'Film';
      const requested = (it.status==='pending'||it.status==='processing'||it.status==='partial');
      const chip = requested ? '<span class="pstatus req">Requested</span>' : '<span class="pstatus want">Request →</span>';
      const ov=it.overview?esc(it.overview):'No synopsis available.';
      const img=it.poster?`<img src="${esc(it.poster)}" loading="lazy" alt="${esc(it.title)}">`:`<div class="noimg">${esc(it.title)}</div>`;
      return `<a class="pcard" href="${esc(it.url)}" target="_blank" rel="noopener">`+
        `<div class="poster">${img}${chip}`+
        `<div class="syn"><div class="syn-in"><div class="syn-t">${esc(it.title)}${yr?` <span style="color:var(--muted);font-size:13px">(${yr})</span>`:''}</div><p class="syn-o">${ov}</p></div></div></div>`+
        `<div class="pt" title="${esc(it.title)}">${esc(it.title)}</div>`+
        `<div class="py">${yr}${yr?' · ':''}${kind}</div></a>`;
    }).join('');
  }

  // ── Catalog: browse/search/detail over the full Jellyfin library ──────────
  // Entered via the client-only Catalog door (?view=catalog, see openCatalog
  // below). No auth, no playback — Phase 1 of "Jellyfin on the Hub site";
  // see Media-Forest handoff docs for the deferred playback follow-up.
  // `seq` guards against a race where two filter changes fire before the
  // first fetch resolves (e.g. type then sort changed in quick succession):
  // every reset bumps seq and any in-flight response tagged with a stale
  // seq is discarded on arrival, so the *last* reset always wins instead of
  // silently losing whichever change's response comes back first.
  const libState = {q:'', type:'all', genre:'', sort:'added', offset:0, limit:24, loading:false, hasMore:true, seq:0};
  let libDebounce=null;

  function libParams(){
    const p=new URLSearchParams();
    if(libState.q) p.set('q', libState.q);
    p.set('type', libState.type);
    if(libState.genre) p.set('genre', libState.genre);
    p.set('sort', libState.sort);
    p.set('limit', libState.limit);
    p.set('offset', libState.offset);
    return p.toString();
  }

  // Same card markup as the just-added/popular rails (.pcard/.poster/.syn),
  // but data-id + delegated click/keydown (below) instead of inline onclick,
  // and it always opens via openFilmById — see that function's comment.
  function libCard(it){
    const yr=it.year?String(it.year):'';
    const kind=it.type==='Series'?'Series':'Film';
    const img=it.tag
      ? `<img src="/api/watching/poster?id=${encodeURIComponent(it.id)}&tag=${encodeURIComponent(it.tag)}" loading="lazy" alt="${esc(it.title)}">`
      : `<div class="noimg">${esc(it.title)}</div>`;
    const genres=(it.genres||[]).slice(0,3).join(' · ');
    const ov=it.overview?esc(it.overview):'No synopsis available.';
    return `<div class="pcard"><div class="poster" tabindex="0" role="button" data-id="${escA(it.id)}" aria-label="${escA(it.title)} details">${img}`+
      `<div class="syn"><div class="syn-in"><div class="syn-t">${esc(it.title)}${yr?` <span style="color:var(--muted);font-size:13px">(${yr})</span>`:''}</div>`+
      `${genres?`<div class="syn-g">${esc(genres)}</div>`:''}<p class="syn-o">${ov}</p><div class="syn-more">Details →</div></div></div></div>`+
      `<div class="pt" title="${esc(it.title)}">${esc(it.title)}</div>`+
      `<div class="py">${yr}${yr?' · ':''}${kind}</div></div>`;
  }

  async function fetchLibraryPage(reset){
    const grid=document.getElementById('libgrid'), status=document.getElementById('libstatus');
    // Non-reset (infinite-scroll) calls dedupe against an in-flight fetch or
    // an exhausted list; a reset always proceeds even mid-fetch (its response
    // will win via the seq check below) so a fast filter change is never lost.
    if(!reset && (libState.loading || !libState.hasMore)) return;
    if(reset){ libState.seq++; libState.offset=0; libState.hasMore=true; if(grid) grid.innerHTML=''; }
    const mySeq=libState.seq;
    libState.loading=true;
    if(status) status.textContent='Loading…';
    let d = await j('/api/library/items?'+libParams());
    // A fresh catalog open (reset) occasionally catches a transient failure on
    // the very first request — a session/cookie settle, a proxy blip — which
    // used to leave an empty grid until the user manually went home and back
    // ("browse doesn't fully load, I have to rebrowse"). Retry a *reset* a
    // couple times with a short backoff so browse recovers on its own.
    // Infinite-scroll (non-reset) pages just wait for the next scroll instead.
    for(let attempt=0; reset && (!d || !d.ok) && attempt<2; attempt++){
      if(mySeq!==libState.seq) return;
      await new Promise(r=>setTimeout(r, 350*(attempt+1)));
      d = await j('/api/library/items?'+libParams());
    }
    if(mySeq!==libState.seq) return; // superseded by a newer reset meanwhile; drop this stale response
    libState.loading=false;
    if(!d || !d.ok){
      libState.hasMore=false;
      if(status) status.textContent = (grid && grid.children.length) ? '' : 'Library unavailable right now.';
      return;
    }
    const items=d.items||[];
    if(grid && items.length) grid.insertAdjacentHTML('beforeend', items.map(libCard).join(''));
    libState.offset = d.offset + items.length;
    libState.hasMore = !!d.has_more;
    if(status) status.textContent = (!items.length && !(grid && grid.children.length)) ? 'No titles match.' : '';
  }
  function libReset(){ fetchLibraryPage(true); }

  let libObserver=null;
  function libObserveSentinel(){
    const sentinel=document.getElementById('libsentinel'); if(!sentinel) return;
    if(libObserver) libObserver.disconnect();
    libObserver = new IntersectionObserver(entries=>{
      if(entries.some(e=>e.isIntersecting)) fetchLibraryPage(false);
    }, {rootMargin:'400px'});
    libObserver.observe(sentinel);
  }

  async function loadLibraryGenres(){
    const sel=document.getElementById('lib-genre'); if(!sel) return;
    const d=await j('/api/library/genres');
    if(d && d.ok && Array.isArray(d.genres) && d.genres.length){
      sel.innerHTML = '<option value="">All genres</option>' +
        d.genres.map(g=>`<option value="${escA(g)}">${esc(g)}</option>`).join('');
    }
  }

  function libInitControlsOnce(){
    if(libInitControlsOnce._done) return;
    libInitControlsOnce._done=true;
    const qEl=document.getElementById('lib-q'), tabs=document.getElementById('lib-tabs'),
          gEl=document.getElementById('lib-genre'), sEl=document.getElementById('lib-sort'),
          grid=document.getElementById('libgrid');
    if(qEl) qEl.addEventListener('input', ()=>{
      clearTimeout(libDebounce);
      libDebounce=setTimeout(()=>{ libState.q=qEl.value.trim(); libReset(); }, 300);
    });
    if(tabs) tabs.addEventListener('click', e=>{
      const b=e.target.closest('.libtab'); if(!b || b.classList.contains('on')) return;
      libState.type=b.dataset.type;
      tabs.querySelectorAll('.libtab').forEach(x=>{
        const on=x===b; x.classList.toggle('on', on); x.setAttribute('aria-selected', on?'true':'false');
      });
      libReset();
    });
    if(gEl) gEl.addEventListener('change', ()=>{ libState.genre=gEl.value; libReset(); });
    if(sEl) sEl.addEventListener('change', ()=>{ libState.sort=sEl.value; libReset(); });
    if(grid){
      grid.addEventListener('click', e=>{
        const p=e.target.closest('.poster'); if(p && p.dataset.id) openFilmById(p.dataset.id, p);
      });
      grid.addEventListener('keydown', e=>{
        if(e.key!=='Enter' && e.key!==' ') return;
        const p=e.target.closest('.poster'); if(p && p.dataset.id){ e.preventDefault(); openFilmById(p.dataset.id, p); }
      });
    }
    libObserveSentinel();
    loadLibraryGenres();
  }

  // View toggle via ?view=catalog — no client router needed. The compact
  // topbar (brand mark/theme picker/auth) stays visible in both modes; the
  // hero banner and #homeview (the streaming rails) swap out for
  // #catalogview. "The instrument" (Nocturne Stage 2) is a ⌘K-launched
  // overlay now, not a homepage section, so it's independent of this
  // toggle — no longer needs including/excluding here.
  function openCatalog(push){
    const hero=document.getElementById('hero'), home=document.getElementById('homeview'),
          cat=document.getElementById('catalogview');
    vtRoot(()=>{
      if(hero) hero.hidden=true;
      if(home) home.hidden=true;
      if(cat) cat.hidden=false;
    });
    libInitControlsOnce();
    const grid=document.getElementById('libgrid');
    if(grid && !grid.children.length) fetchLibraryPage(true);
    if(push!==false) history.pushState({view:'catalog'}, '', '?view=catalog');
    // scroll the catalog into view so it's obvious it opened (it sits below the
    // topbar, so a click from the rails otherwise leaves you looking at the top).
    if(cat) requestAnimationFrame(()=>cat.scrollIntoView({behavior:'smooth', block:'start'}));
  }
  // Media|Forest title returns home: close the catalog if it's open, scroll to top.
  function goHome(){
    const cat=document.getElementById('catalogview');
    if(cat && !cat.hidden) closeCatalog();
    window.scrollTo({top:0, behavior:'smooth'});
  }
  function closeCatalog(push){
    const hero=document.getElementById('hero'), home=document.getElementById('homeview'),
          cat=document.getElementById('catalogview');
    vtRoot(()=>{
      if(hero) hero.hidden=false;
      if(home) home.hidden=false;
      if(cat) cat.hidden=true;
    });
    if(push!==false) history.pushState({}, '', location.pathname);
  }
  addEventListener('popstate', ()=>{
    if(new URLSearchParams(location.search).get('view')==='catalog') openCatalog(false);
    else closeCatalog(false);
  });
  // Direct link/reload into ?view=catalog lands straight in the catalog view.
  if(new URLSearchParams(location.search).get('view')==='catalog') openCatalog(false);

  // ── boot ──────────────────────────────────────────────────────────────────
  // Only two things run unconditionally: paint the header, and ask who we are.
  // EVERY data loader below is gated behind a confirmed session, because every
  // endpoint they call is gated too (app.py's require_session_for_api). Before
  // this, a signed-out visitor's page load fired ~15 requests that all came
  // back 401 — and then kept polling /watching/now every 12s and /stats/status
  // every 60s behind the sign-in gate, forever. checkAuth() now drives
  // startMemberSurfaces()/stopMemberSurfaces() instead.
  renderAuthBox();   // paint "Sign in" immediately; checkAuth() updates it once /me resolves
  checkAuth();

  // live storage bar (real filesystem usage of the media library)
  async function loadStorage(){
    try{
      const d=await j('/api/stats/storage');
      if(!d||!d.ok) return;
      const fmt=b=>{const t=b/1e12; return t>=1?t.toFixed(2)+' TB':(b/1e9).toFixed(0)+' GB';};
      const bar=document.getElementById('stg-bar'), txt=document.getElementById('stg-txt');
      if(bar) requestAnimationFrame(()=>{bar.style.width=d.pct+'%';});
      if(txt) txt.textContent=`${fmt(d.used)} of ${fmt(d.total)} · ${fmt(d.free)} free (${d.pct}%)`;
    }catch(e){}
  }

  // service status chip
  async function loadStatus(){
    const d=await j('/api/stats/status');
    const chip=document.getElementById('statuschip'), tx=document.getElementById('statustxt');
    if(!chip) return;
    if(d&&d.ok){
      chip.className='chip '+d.overall;
      const down=(d.services||[]).filter(x=>!x.up).map(x=>x.name);
      tx.textContent = d.overall==='live'?'All systems live' : d.overall==='down'?'Systems offline'
                     : (down.length===1?down[0]+' offline':down.length+' services down');
    } else { chip.className='chip'; tx.textContent='status unavailable'; }
  }
  // Statuspage-style board: a row per service with a 30-day daily bar timeline + uptime %
  const UP_DAYS=30;
  function daySlots(n){
    const out=[], t=new Date(); t.setHours(0,0,0,0);
    for(let i=n-1;i>=0;i--){ const d=new Date(t); d.setDate(d.getDate()-i); out.push(d.toISOString().slice(0,10)); }
    return out;
  }
  function fmtDay(iso){ return new Date(iso+'T00:00:00').toLocaleDateString([], {month:'short', day:'numeric'}); }
  async function loadUptime(){
    const board=document.getElementById('statusboard');
    if(!board) return;
    const [cur,tl]=await Promise.all([ j('/api/stats/status'), j('/api/stats/status/timeline?days='+UP_DAYS) ]);
    const curMap={}; if(cur&&cur.ok)(cur.services||[]).forEach(s=>{curMap[s.key]=s.up;});
    let svcs=(tl&&tl.ok&&Array.isArray(tl.services))?tl.services:[];
    // Fallback so the board is never empty before history accrues.
    if(!svcs.length && cur&&cur.ok) svcs=(cur.services||[]).map(s=>({key:s.key,name:s.name,uptime_pct:null,days:[]}));
    const slots=daySlots(UP_DAYS);
    board.innerHTML = svcs.map(s=>{
      const dmap={}; (s.days||[]).forEach(x=>{dmap[x.date]=x.pct;});
      const bars=slots.map(dt=>{
        if(!(dt in dmap)) return '<i title="'+fmtDay(dt)+' · no data"></i>';
        const p=dmap[dt], cls=p>=99.5?'up':p>=80?'deg':'down';
        return '<i class="'+cls+'" title="'+fmtDay(dt)+' · '+p+'% up"></i>';
      }).join('');
      const cu=curMap[s.key];
      const stTxt = cu===false?'Down' : cu===true?'Operational' : '—';
      const stCls = cu===false?'down' : cu===true?'up' : '';
      const up = (s.uptime_pct!=null) ? s.uptime_pct.toFixed(s.uptime_pct>=99.95?2:1)+'%' : '—';
      return '<div class="srow">'
        + '<div class="sname"><span class="dot '+stCls+'"></span>'+s.name+'<span class="sstate">'+stTxt+'</span></div>'
        + '<div class="sbars">'+bars+'</div>'
        + '<div class="sup" title="'+UP_DAYS+'-day uptime">'+up+'</div>'
      + '</div>';
    }).join('');
  }
  // ── member surfaces lifecycle ─────────────────────────────────────────────
  // Every loader below hits a session-gated endpoint, so all of it is started
  // by checkAuth() only once /me confirms a session, and torn down the moment
  // it doesn't. Two independent gates apply to the polling:
  //   * signed in?      -> _memberSurfacesOn (this block)
  //   * tab visible?    -> the visibilitychange handler
  // Both must be true for a timer to exist, which is why the handler checks
  // _memberSurfacesOn before restarting anything — otherwise refocusing a
  // signed-out tab would resurrect the very polling this fix removed.
  let _memberSurfacesOn=false;
  let _nowTimer=null;

  // Now Playing is the only thing on the homepage that needs to keep polling.
  // The instrument overlay owns its own status/uptime timers (openInstrument),
  // and starts them only while it's actually open.
  function _startPolling(){ clearInterval(_nowTimer); _nowTimer=setInterval(refreshNow, 12000); }
  function _stopPolling(){ clearInterval(_nowTimer); _nowTimer=null; }

  // Called by checkAuth() on every resolve; idempotent, so the 5-minute
  // re-check doesn't re-fetch the whole homepage each time it confirms the
  // session is still good.
  function startMemberSurfaces(){
    if(_memberSurfacesOn) return;
    _memberSurfacesOn=true;
    loadStatic(); loadPopular(); loadHero(); loadServices();
    refreshNow();
    if(!document.hidden) _startPolling();
  }
  function stopMemberSurfaces(){
    if(!_memberSurfacesOn) return;
    _memberSurfacesOn=false;
    _stopPolling();
    closeInstrument();   // also kills its status/uptime timers if it was open
  }
  document.addEventListener('visibilitychange',()=>{
    if(!_memberSurfacesOn) return;
    if(document.hidden){ _stopPolling(); }
    else { refreshNow(); _startPolling(); }
  });
