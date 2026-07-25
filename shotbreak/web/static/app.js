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

  async function apiPost(path, body) {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`POST ${path} failed (${res.status})`);
    return res.json();
  }

  async function apiPostForm(path, formData) {
    const res = await fetch(path, { method: 'POST', body: formData });
    if (!res.ok) throw new Error(`POST ${path} failed (${res.status})`);
    return res.json();
  }

  // --- routing ---

  function parseHash() {
    const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    if (parts[0] === 'project' && parts[1]) {
      const projectId = Number(parts[1]);
      if (parts[2] === 'element' && parts[3]) {
        return { view: 'element', projectId, elemId: Number(parts[3]) };
      }
      if (parts[2] === 'scene' && parts[3]) {
        return { view: 'scene', projectId, sceneId: Number(parts[3]) };
      }
      return { view: 'breakdown', projectId, sceneId: null };
    }
    if (parts[0] === 'settings') return { view: 'settings' };
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
      else if (state.view === 'scene') await renderScene(state);
      else if (state.view === 'element') await renderElement(state);
      else if (state.view === 'settings') await renderSettings();
      else await renderProjects();
    } catch (err) {
      app.innerHTML = `<div class="error-banner">Failed to load: ${escapeHtml(err.message)}</div>`;
    }
  }

  // --- project list ---

  async function renderProjects() {
    setCrumbs([{ label: 'Projects' }]);
    const projects = await apiGet('/api/projects');
    app.innerHTML = `
      <h1>Projects</h1>
      ${importForm()}
      <div id="import-error"></div>
      ${projects.length ? `
        <div class="subtitle">${projects.length} project${projects.length === 1 ? '' : 's'}</div>
        <div class="project-grid">${projects.map(projectCard).join('')}</div>
      ` : '<div class="empty">No projects yet. Import a screenplay above.</div>'}
    `;
    app.querySelectorAll('.project-card').forEach((card) => {
      card.addEventListener('click', () => navigate(`#/project/${card.dataset.id}`));
    });
    bindImportForm();
  }

  function importForm() {
    return `
      <form id="import-form" class="panel import-form">
        <h3>Import screenplay</h3>
        <div class="import-form-row">
          <input type="file" id="import-file" accept=".fountain,.fdx,.xml,.pdf" required>
          <input type="text" id="import-name" class="editable-field" placeholder="Project name (optional)">
          <button type="submit" class="btn" id="import-submit">Import</button>
        </div>
      </form>
    `;
  }

  function bindImportForm() {
    const form = document.getElementById('import-form');
    const errorEl = document.getElementById('import-error');
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const fileInput = document.getElementById('import-file');
      const nameInput = document.getElementById('import-name');
      const submitBtn = document.getElementById('import-submit');
      if (!fileInput.files.length) return;

      const formData = new FormData();
      formData.append('file', fileInput.files[0]);
      if (nameInput.value.trim()) formData.append('name', nameInput.value.trim());

      submitBtn.disabled = true;
      submitBtn.textContent = 'Importing…';
      errorEl.innerHTML = '';
      try {
        const result = await apiPostForm('/api/projects', formData);
        navigate(`#/project/${result.project_id}`);
      } catch (err) {
        errorEl.innerHTML = `<div class="error-banner">Import failed: ${escapeHtml(err.message)}</div>`;
        submitBtn.disabled = false;
        submitBtn.textContent = 'Import';
      }
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

  // --- breakdown view ---

  async function renderBreakdown(state) {
    const { projectId } = state;
    const [project, scenesResp, elements, providersData] = await Promise.all([
      apiGet(`/api/projects/${projectId}`),
      apiGet(`/api/projects/${projectId}/scenes?page_size=1000`),
      apiGet(`/api/projects/${projectId}/elements`),
      apiGet('/api/providers'),
    ]);
    const scenes = scenesResp.scenes;

    setCrumbs([{ label: 'Projects', href: '#/' }, { label: project.name }]);

    let activeCategory = '';
    let activeSceneId = state.sceneId || null;
    const selectedProvider = providersData.default || (providersData.providers[0]?.name || 'deepseek');

    const catCounts = {};
    elements.forEach((e) => { catCounts[e.category] = (catCounts[e.category] || 0) + 1; });
    const presentCats = CATEGORIES.filter((c) => catCounts[c]);

    const providerOptions = providersData.providers.map((p) =>
      `<option value="${p.name}" ${p.name === selectedProvider ? 'selected' : ''}>${escapeHtml(p.name)} (${escapeHtml(p.model)})</option>`
    ).join('');

    app.innerHTML = `
      <h1>${escapeHtml(project.name)}</h1>
      <div class="subtitle">${project.scene_count} scenes &middot; ${project.element_count} elements</div>
      <div class="toolbar">
        <select class="editable-field provider-select" id="provider-select">${providerOptions}</select>
        <button class="btn" id="run-breakdown">Run Extraction</button>
        <div class="progress-wrap" id="run-progress" style="display:none;">
          <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
          <span class="progress-label" id="progress-label"></span>
        </div>
        <div class="export-group">
          <span class="export-label">Export:</span>
          <button class="btn secondary export-btn" data-fmt="fdx">FDX</button>
          <button class="btn secondary export-btn" data-fmt="fadein">Fade In</button>
          <button class="btn secondary export-btn" data-fmt="csv">CSV</button>
          <button class="btn secondary export-btn" data-fmt="pdf">PDF</button>
        </div>
      </div>
      <div id="run-error"></div>
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
        row.addEventListener('dblclick', () => {
          navigate(`#/project/${projectId}/scene/${row.dataset.id}`);
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

    // --- export buttons ---
    app.querySelectorAll('.export-btn').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const fmt = btn.dataset.fmt;
        btn.textContent = '...';
        btn.disabled = true;
        try {
          const result = await apiPost(`/api/projects/${projectId}/export`, { format: fmt });
          if (result.files && result.files.length) {
            result.files.forEach((f) => {
              const a = document.createElement('a');
              a.href = `/exports/${f.split('/').pop()}`;
              a.download = f.split('/').pop();
              a.click();
            });
          }
        } catch (err) {
          alert(`Export failed: ${err.message}`);
        }
        btn.textContent = fmt.toUpperCase();
        btn.disabled = false;
      });
    });

    // --- run extraction + progress polling ---
    const runBtn = document.getElementById('run-breakdown');
    const progressWrap = document.getElementById('run-progress');
    const progressFill = document.getElementById('progress-fill');
    const progressLabel = document.getElementById('progress-label');
    const runErrorEl = document.getElementById('run-error');

    function updateProgress(status) {
      progressWrap.style.display = '';
      const total = status.scenes_total || 0;
      const done = status.scenes_completed || 0;
      const pct = total ? Math.round((done / total) * 100) : 0;
      progressFill.style.width = `${pct}%`;
      progressLabel.textContent = status.status === 'running'
        ? `Running: ${done}/${total} scenes`
        : `${status.status} (${done}/${total} scenes)`;
    }

    function pollStatus() {
      const poll = async () => {
        let status;
        try {
          status = await apiGet(`/api/projects/${projectId}/breakdown/status`);
        } catch (err) {
          runErrorEl.innerHTML = `<div class="error-banner">Status check failed: ${escapeHtml(err.message)}</div>`;
          runBtn.disabled = false;
          return;
        }
        if (status.status === 'none') return;
        updateProgress(status);
        if (status.status === 'running') {
          setTimeout(poll, 1500);
        } else {
          runBtn.disabled = false;
          if (status.errors && status.errors.length) {
            runErrorEl.innerHTML = `<div class="error-banner">Extraction finished with ${status.errors.length} error(s).</div>`;
          }
          render();
        }
      };
      poll();
    }

    runBtn.addEventListener('click', async () => {
      runBtn.disabled = true;
      runErrorEl.innerHTML = '';
      progressWrap.style.display = '';
      progressLabel.textContent = 'Starting…';
      progressFill.style.width = '0%';
      const provider = document.getElementById('provider-select').value;
      try {
        await apiPost(`/api/projects/${projectId}/breakdown/run`, {
          passes: ['extract', 'coreference'],
          provider_overrides: { extract: provider, coreference: provider },
        });
        pollStatus();
      } catch (err) {
        runErrorEl.innerHTML = `<div class="error-banner">Failed to start extraction: ${escapeHtml(err.message)}</div>`;
        runBtn.disabled = false;
      }
    });

    if (project.last_run && project.last_run.status === 'running') {
      runBtn.disabled = true;
      pollStatus();
    }

    renderSceneList();
    await renderElementPanel();
  }

  // --- scene detail ---

  async function renderScene(state) {
    const { projectId, sceneId } = state;
    const [scene, project] = await Promise.all([
      apiGet(`/api/projects/${projectId}/scenes/${sceneId}`),
      apiGet(`/api/projects/${projectId}`),
    ]);

    setCrumbs([
      { label: 'Projects', href: '#/' },
      { label: project.name, href: `#/project/${projectId}` },
      { label: scene.scene_number || scene.slugline },
    ]);

    app.innerHTML = `
      <div class="detail-header">
        <h1>${escapeHtml(scene.slugline || '')}</h1>
        <span class="badge">${escapeHtml(scene.interior_exterior || '')} &middot; ${escapeHtml(scene.time_of_day || '')}</span>
      </div>
      <div class="subtitle">
        Location: ${escapeHtml(scene.location || '')} &middot;
        Page: ${scene.page_count_eighths}/8 &middot;
        ${scene.element_count} elements
        ${scene.synopsis ? `<br>${escapeHtml(scene.synopsis)}` : ''}
      </div>

      <div class="detail-section">
        <h3>Elements</h3>
        <div class="element-grid">
          ${scene.elements.length ? scene.elements.map(elementCard).join('') : '<div class="empty">No elements in this scene.</div>'}
        </div>
      </div>

      <div class="detail-section">
        <h3>Scene Text</h3>
        <pre class="scene-body">${escapeHtml(scene.raw_body || '')}</pre>
      </div>

      ${scene.descriptions && scene.descriptions.length ? `
        <div class="detail-section">
          <h3>Descriptions</h3>
          ${scene.descriptions.map(descBlock).join('')}
        </div>
      ` : ''}
    `;
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

      <div class="toolbar">
        <button class="btn" id="build-bible">Build Bible</button>
        <button class="btn secondary" id="gen-descriptions">Generate Scene Descriptions</button>
        <span id="desc-status" style="margin-left:8px;font-size:12px;color:var(--text-dim);"></span>
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
        ${element.descriptions.length ? element.descriptions.map(descBlock).join('') : '<div class="empty">No descriptions yet. Generate a bible first.</div>'}
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

    // --- bible / description buttons ---
    const statusEl = document.getElementById('desc-status');
    const bibleBtn = document.getElementById('build-bible');
    const genBtn = document.getElementById('gen-descriptions');

    async function updateElementData() {
      const fresh = await apiGet(`/api/projects/${projectId}/elements/${elemId}`);
      const descSection = document.querySelector('.detail-section:nth-child(4)');
      if (descSection) {
        const h3 = descSection.querySelector('h3');
        descSection.innerHTML = `
          <h3>Descriptions</h3>
          ${fresh.descriptions.length ? fresh.descriptions.map(descBlock).join('') : '<div class="empty">No descriptions yet. Generate a bible first.</div>'}
        `;
      }
    }

    bibleBtn.addEventListener('click', async () => {
      bibleBtn.disabled = true;
      statusEl.textContent = 'Building bible…';
      try {
        await apiPost(`/api/projects/${projectId}/elements/${elemId}/bible`, { provider: 'deepseek' });
        statusEl.textContent = 'Bible built!';
        await updateElementData();
      } catch (err) {
        statusEl.textContent = `Failed: ${err.message}`;
      }
      bibleBtn.disabled = false;
    });

    genBtn.addEventListener('click', async () => {
      genBtn.disabled = true;
      statusEl.textContent = 'Generating scene descriptions…';
      try {
        await apiPost(`/api/projects/${projectId}/elements/${elemId}/describe`, { provider: 'deepseek' });
        statusEl.textContent = 'Descriptions generated!';
        await updateElementData();
      } catch (err) {
        statusEl.textContent = `Failed: ${err.message}`;
      }
      genBtn.disabled = false;
    });

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
    let body = '';
    try {
      const obj = JSON.parse(d.content);
      if (obj.immutable) {
        body += '<div class="desc-group"><strong>Immutable</strong><ul>';
        Object.entries(obj.immutable).forEach(([k, v]) => {
          if (v) body += `<li>${escapeHtml(k.replace(/_/g, ' '))}: ${escapeHtml(String(v))}</li>`;
        });
        body += '</ul></div>';
      }
      if (obj.mutable_baseline) {
        body += '<div class="desc-group"><strong>Mutable baseline</strong><ul>';
        Object.entries(obj.mutable_baseline).forEach(([k, v]) => {
          if (v) body += `<li>${escapeHtml(k.replace(/_/g, ' '))}: ${escapeHtml(String(v))}</li>`;
        });
        body += '</ul></div>';
      }
      if (obj.visible_this_scene) {
        body += '<div class="desc-group"><strong>Visible this scene</strong><ul>';
        Object.entries(obj.visible_this_scene).forEach(([k, v]) => {
          if (v) body += `<li>${escapeHtml(k.replace(/_/g, ' '))}: ${escapeHtml(String(v))}</li>`;
        });
        body += '</ul></div>';
      }
      if (obj.scene_context) {
        body += '<div class="desc-group"><strong>Scene context</strong><ul>';
        Object.entries(obj.scene_context).forEach(([k, v]) => {
          if (v) body += `<li>${escapeHtml(k.replace(/_/g, ' '))}: ${escapeHtml(String(v))}</li>`;
        });
        body += '</ul></div>';
      }
      if (obj.continuity_changes && obj.continuity_changes.length) {
        body += `<div class="desc-group"><strong>Changes</strong><ul>${obj.continuity_changes.map(c => `<li>${escapeHtml(c)}</li>`).join('')}</ul></div>`;
      }
      if (obj.full_composite_prompt) {
        body += `<div class="desc-group"><strong>Full prompt</strong><pre style="white-space:pre-wrap;font-size:12px;">${escapeHtml(obj.full_composite_prompt)}</pre></div>`;
      }
      if (obj.unspecified_fields && obj.unspecified_fields.length) {
        body += `<div class="desc-group"><strong style="color:var(--danger);">Unspecified (needs human input)</strong>: ${escapeHtml(obj.unspecified_fields.join(', '))}</div>`;
      }
      if (obj.inference_tier) {
        body += `<div class="desc-meta" style="margin-top:4px;">Inference: ${escapeHtml(obj.inference_tier)}</div>`;
      }
      if (obj.confidence != null) {
        body += `<div class="desc-meta">Confidence: ${(obj.confidence * 100).toFixed(0)}%</div>`;
      }
    } catch {
      body = escapeHtml(d.content);
    }
    return `
      <div class="description-block">
        <div class="desc-meta">${scope}</div>
        ${body}
      </div>
    `;
  }

  // --- settings ---

  async function renderSettings() {
    setCrumbs([{ label: 'Settings' }]);

    const [cfg, pvd] = await Promise.all([
      apiGet('/api/config'),
      apiGet('/api/providers'),
    ]);

    app.innerHTML = `
      <h1>Configuration</h1>
      <div class="subtitle">Manage API keys, providers, and models</div>

      <div class="detail-section">
        <h3>Providers</h3>
        <div class="provider-list">
          ${pvd.providers.length ? pvd.providers.map((p) => `
            <div class="provider-row">
              <span class="provider-name">${escapeHtml(p.name)}</span>
              <span class="provider-kind badge">${escapeHtml(p.kind)}</span>
              <span class="provider-model">${escapeHtml(p.model)}</span>
              <span class="provider-key-status ${p.has_key ? 'has-key' : 'no-key'}">${p.has_key ? 'Key set' : 'No key'}</span>
              ${p.name === pvd.default ? '<span class="badge" style="background:var(--accent);color:var(--bg);">Default</span>' : ''}
            </div>
          `).join('') : '<div class="empty">No providers configured.</div>'}
        </div>
      </div>

      <div class="detail-section">
        <h3>Raw Config (YAML)</h3>
        <textarea id="config-yaml" class="config-editor" spellcheck="false">${escapeHtml(cfg.yaml)}</textarea>
        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;">
          <button class="btn" id="save-config">Save</button>
          <span id="config-status" style="font-size:12px;color:var(--text-dim);"></span>
        </div>
      </div>
    `;

    document.getElementById('save-config').addEventListener('click', async () => {
      const yaml = document.getElementById('config-yaml').value;
      const statusEl = document.getElementById('config-status');
      try {
        await apiPost('/api/config', { yaml });
        statusEl.textContent = 'Saved! Restart server to apply provider list changes.';
        statusEl.style.color = 'var(--cat-props)';
      } catch (err) {
        statusEl.textContent = `Save failed: ${err.message}`;
        statusEl.style.color = 'var(--danger)';
      }
    });
  }

  // --- bootstrap ---
  if (!location.hash && initialData.project) {
    navigate(`#/project/${initialData.project.id}`);
  } else {
    render();
  }
})();
