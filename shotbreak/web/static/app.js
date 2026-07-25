(function () {
  const app = document.getElementById('app');
  const crumbs = document.getElementById('crumbs');
  const initialData = JSON.parse(document.getElementById('initial-data').textContent || '{}');

  const CATEGORIES = [
    'cast', 'background', 'stunts', 'vehicles', 'props', 'set_dressing',
    'wardrobe', 'makeup_hair', 'practical_fx', 'vfx', 'animals', 'music',
    'sound', 'special_equipment', 'greenery', 'weapons', 'locations', 'notes',
  ];

  function catLabel(cat) {
    return String(cat || '').replace(/_/g, ' ');
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  async function apiGet(path) {
    const res = await fetch(path);
    if (!res.ok) throw new Error(`GET ${path} failed (${res.status})`);
    return res.json();
  }

  async function apiPut(path, body) {
    const res = await fetch(path, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`PUT ${path} failed (${res.status})`);
    return res.json();
  }

  // --- routing: #/  |  #/project/:id  |  #/project/:id/scene/:id  |  #/project/:id/element/:id ---

  function parseHash() {
    const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    if (parts[0] === 'project' && parts[1]) {
      const projectId = Number(parts[1]);
      if (parts[2] === 'element' && parts[3]) {
        return { view: 'element', projectId, elemId: Number(parts[3]) };
      }
      if (parts[2] === 'scene' && parts[3]) {
        return { view: 'breakdown', projectId, sceneId: Number(parts[3]) };
      }
      return { view: 'breakdown', projectId, sceneId: null };
    }
    return { view: 'projects' };
  }

  function navigate(hash) {
    location.hash = hash;
  }

  function setCrumbs(items) {
    crumbs.innerHTML = items.map((it, i) => {
      const sep = i > 0 ? '<span class="sep">/</span>' : '';
      return it.href
        ? `${sep}<a href="${it.href}">${escapeHtml(it.label)}</a>`
        : `${sep}<span class="current">${escapeHtml(it.label)}</span>`;
    }).join('');
  }

  window.addEventListener('hashchange', render);

  async function render() {
    const state = parseHash();
    app.innerHTML = '<div class="loading">Loading&hellip;</div>';
    try {
      if (state.view === 'breakdown') await renderBreakdown(state);
      else if (state.view === 'element') await renderElement(state);
      else await renderProjects();
    } catch (err) {
      app.innerHTML = `<div class="error-banner">Failed to load: ${escapeHtml(err.message)}</div>`;
    }
  }

  // --- project list ---

  async function renderProjects() {
    setCrumbs([{ label: 'Projects' }]);
    const projects = await apiGet('/api/projects');
    if (!projects.length) {
      app.innerHTML = '<div class="empty">No projects yet. Import a screenplay with <code>shotbreak import-script</code>.</div>';
      return;
    }
    app.innerHTML = `
      <h1>Projects</h1>
      <div class="subtitle">${projects.length} project${projects.length === 1 ? '' : 's'}</div>
      <div class="project-grid">${projects.map(projectCard).join('')}</div>
    `;
    app.querySelectorAll('.project-card').forEach((card) => {
      card.addEventListener('click', () => navigate(`#/project/${card.dataset.id}`));
    });
  }

  function projectCard(p) {
    return `
      <div class="project-card" data-id="${p.id}">
        <div class="name">${escapeHtml(p.name)}</div>
        <div class="meta">
          <span>${p.scene_count} scenes</span>
          <span>${p.element_count} elements</span>
          <span>${escapeHtml(p.script_format || '')}</span>
        </div>
      </div>
    `;
  }

  // --- breakdown view: scene list + element grid ---

  async function renderBreakdown(state) {
    const { projectId } = state;
    const [project, scenesResp, elements] = await Promise.all([
      apiGet(`/api/projects/${projectId}`),
      apiGet(`/api/projects/${projectId}/scenes?page_size=1000`),
      apiGet(`/api/projects/${projectId}/elements`),
    ]);
    const scenes = scenesResp.scenes;

    setCrumbs([{ label: 'Projects', href: '#/' }, { label: project.name }]);

    let activeCategory = '';
    let activeSceneId = state.sceneId || null;

    const catCounts = {};
    elements.forEach((e) => { catCounts[e.category] = (catCounts[e.category] || 0) + 1; });
    const presentCats = CATEGORIES.filter((c) => catCounts[c]);

    app.innerHTML = `
      <h1>${escapeHtml(project.name)}</h1>
      <div class="subtitle">${project.scene_count} scenes &middot; ${project.element_count} elements</div>
      <div class="category-filters" id="cat-filters">
        <button class="cat-chip active" data-cat="">All</button>
        ${presentCats.map((c) => `
          <button class="cat-chip" data-category="${c}" data-cat="${c}">
            <span class="dot"></span>${escapeHtml(catLabel(c))} (${catCounts[c]})
          </button>
        `).join('')}
      </div>
      <div class="breakdown-layout">
        <div class="panel scene-list" id="scene-list"></div>
        <div class="panel" id="element-panel"></div>
      </div>
    `;

    const filtersEl = document.getElementById('cat-filters');
    const sceneListEl = document.getElementById('scene-list');
    const elementPanelEl = document.getElementById('element-panel');

    function renderSceneList() {
      sceneListEl.innerHTML = scenes.map((s) => `
        <div class="scene-row ${s.id === activeSceneId ? 'active' : ''}" data-id="${s.id}">
          <div><span class="scene-num">${escapeHtml(s.scene_number || '')}</span><span class="slugline">${escapeHtml(s.slugline || '')}</span></div>
          <span class="count">${s.element_count}</span>
        </div>
      `).join('');
      sceneListEl.querySelectorAll('.scene-row').forEach((row) => {
        row.addEventListener('click', () => {
          activeSceneId = Number(row.dataset.id);
          history.replaceState(null, '', `#/project/${projectId}/scene/${activeSceneId}`);
          renderSceneList();
          renderElementPanel();
        });
      });
    }

    async function renderElementPanel() {
      if (activeSceneId) {
        const scene = await apiGet(`/api/projects/${projectId}/scenes/${activeSceneId}`);
        elementPanelEl.innerHTML = `
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
            <div>
              <h2 style="margin:0;">${escapeHtml(scene.slugline || '')}</h2>
              ${scene.synopsis ? `<div class="subtitle" style="margin:4px 0 0;">${escapeHtml(scene.synopsis)}</div>` : ''}
            </div>
            <button class="btn secondary" id="clear-scene">Show all elements</button>
          </div>
          <div class="element-grid">
            ${scene.elements.length ? scene.elements.map(elementCard).join('') : '<div class="empty">No elements tagged in this scene.</div>'}
          </div>
        `;
        document.getElementById('clear-scene').addEventListener('click', () => {
          activeSceneId = null;
          history.replaceState(null, '', `#/project/${projectId}`);
          renderSceneList();
          renderElementPanel();
        });
      } else {
        const filtered = activeCategory ? elements.filter((e) => e.category === activeCategory) : elements;
        elementPanelEl.innerHTML = `
          <h2>Elements</h2>
          <div class="element-grid">${filtered.length ? filtered.map(elementCard).join('') : '<div class="empty">No elements.</div>'}</div>
        `;
      }
      elementPanelEl.querySelectorAll('.element-card').forEach((card) => {
        card.addEventListener('click', () => navigate(`#/project/${projectId}/element/${card.dataset.id}`));
      });
    }

    filtersEl.querySelectorAll('.cat-chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        activeCategory = chip.dataset.cat || '';
        filtersEl.querySelectorAll('.cat-chip').forEach((c) => c.classList.remove('active'));
        chip.classList.add('active');
        renderElementPanel();
      });
    });

    renderSceneList();
    await renderElementPanel();
  }

  function elementCard(e) {
    return `
      <div class="element-card" data-category="${e.category}" data-id="${e.id}">
        <div class="name">${escapeHtml(e.name)}</div>
        <div class="cat-label">${escapeHtml(catLabel(e.category))}</div>
        <div class="appearances">${e.appearance_count != null ? e.appearance_count + ' appearance' + (e.appearance_count === 1 ? '' : 's') : ''}</div>
      </div>
    `;
  }

  // --- element detail ---

  async function renderElement(state) {
    const { projectId, elemId } = state;
    const [element, project] = await Promise.all([
      apiGet(`/api/projects/${projectId}/elements/${elemId}`),
      apiGet(`/api/projects/${projectId}`),
    ]);

    setCrumbs([
      { label: 'Projects', href: '#/' },
      { label: project.name, href: `#/project/${projectId}` },
      { label: element.name },
    ]);

    app.innerHTML = `
      <div class="detail-header">
        <span class="badge" data-category="${element.category}">${escapeHtml(catLabel(element.category))}</span>
        <h1 style="margin:0;">${escapeHtml(element.name)}</h1>
      </div>

      <div class="detail-section">
        <h3>Edit</h3>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
          <input class="editable-field" id="edit-name" value="${escapeHtml(element.name)}">
          <select class="editable-field" id="edit-category">
            ${CATEGORIES.map((c) => `<option value="${c}" ${c === element.category ? 'selected' : ''}>${escapeHtml(catLabel(c))}</option>`).join('')}
          </select>
          <input class="editable-field" id="edit-type" value="${escapeHtml(element.element_type || '')}" placeholder="element type">
          <button class="btn" id="save-element">Save</button>
        </div>
      </div>

      <div class="detail-section">
        <h3>Descriptions</h3>
        ${element.descriptions.length ? element.descriptions.map(descBlock).join('') : '<div class="empty">No descriptions yet.</div>'}
      </div>

      <div class="detail-section">
        <h3>Scene appearances (${element.appearances.length})</h3>
        <div class="panel">
          ${element.appearances.length ? element.appearances.map((a) => `
            <div class="appearance-row">
              <a href="#/project/${projectId}/scene/${a.scene_id}">${escapeHtml(a.scene_number || '')} &middot; ${escapeHtml(a.slugline || '')}</a>
              <span class="count">${a.context ? escapeHtml(a.context) : ''}</span>
            </div>
          `).join('') : '<div class="empty">No appearances recorded.</div>'}
        </div>
      </div>
    `;

    document.getElementById('save-element').addEventListener('click', async () => {
      const updates = {
        name: document.getElementById('edit-name').value,
        category: document.getElementById('edit-category').value,
        element_type: document.getElementById('edit-type').value,
      };
      try {
        await apiPut(`/api/projects/${projectId}/elements/${elemId}`, updates);
        render();
      } catch (err) {
        alert(`Save failed: ${err.message}`);
      }
    });
  }

  function descBlock(d) {
    const scope = d.scene_id ? `Scene delta &middot; v${d.version}` : `${escapeHtml(d.description_type || 'Bible')} &middot; v${d.version}`;
    return `
      <div class="description-block">
        <div class="desc-meta">${scope}</div>
        <div>${escapeHtml(d.content)}</div>
      </div>
    `;
  }

  // --- bootstrap ---
  // Server-rendered initial_data is only present for the very first paint
  // (via ?project_id=), so hand off to the hash router immediately after.

  if (!location.hash && initialData.project) {
    navigate(`#/project/${initialData.project.id}`);
  } else {
    render();
  }
})();
