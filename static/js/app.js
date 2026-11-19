const msg = document.getElementById('msg');
const view = document.getElementById('view');
let contracts = [];
let lastCheck = null;

async function api(path, opts={}) {
  const r = await fetch(path, {
    headers: {'Content-Type': 'application/json', ...(opts.headers||{})},
    ...opts
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('HTTP '+r.status));
  return j;
}

function setMsg(t, isErr=false) {
  msg.textContent = t || '';
  msg.className = isErr ? 'err' : 'muted';
}

async function loadContracts() {
  const data = await api('/api/contracts');
  contracts = data.items || [];
}

function renderContracts() {
  view.innerHTML = `<div class="grid">${contracts.map(c => `
    <article class="card">
      <div class="row">
        <h3>${c.table_name} · v${c.version_no}</h3>
        <span class="chip">${c.status}</span>
      </div>
      <p class="muted">${c.note || ''} · 字段 ${c.field_count}</p>
      <div class="grid" data-fields="${c.id}"></div>
      <button type="button" class="ghost" data-detail="${c.id}">字段详情</button>
    </article>`).join('')}</div>`;
  view.querySelectorAll('[data-detail]').forEach(btn => {
    btn.onclick = () => showFieldDetail(Number(btn.dataset.detail));
  });
}

async function showFieldDetail(cid) {
  const c = contracts.find(x => x.id === cid);
  if (!c) return;
  const box = view.querySelector(`[data-fields="${cid}"]`);
  if (!box) return;
  try {
    const detail = await api(`/api/contracts/${cid}/fields`);
    box.innerHTML = detail.fields.map(f => `
      <div class="row">
        <span class="chip">${f.field_name}</span>
        <span class="muted">${f.field_type}</span>
        ${f.watermark ? `<span class="chip water">水位 ${f.watermark.type}</span>
          <span class="muted">快照 ${f.watermark.snapshot_no || '未刷新'}</span>` : ''}
      </div>`).join('');
  } catch (e) { setMsg(e.message, true); }
}

function draftOptions() {
  return contracts.filter(c => c.status === '草稿')
    .map(c => `<option value="${c.id}">${c.table_name} v${c.version_no}</option>`).join('');
}

function renderCheck() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-check" type="button">运行破坏性检查</button>
      <pre id="out" class="muted"></pre>
    </div>`;
  document.getElementById('btn-check').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/breaking-check`, {method:'POST', body:'{}'});
      lastCheck = j.check;
      document.getElementById('out').textContent = JSON.stringify(j.check, null, 2);
      setMsg(`检查结论：${j.check.verdict}`);
    } catch (e) { setMsg(e.message, true); }
  };
}

function renderApply() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-apply" type="button">换版应用</button>
    </div>`;
  document.getElementById('btn-apply').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/apply`, {method:'POST', body:'{}'});
      setMsg(`已应用 ${j.contract.table_name} v${j.contract.version_no}`);
      await loadContracts();
    } catch (e) { setMsg(e.message, true); }
  };
}

async function renderReconcile() {
  const reports = (await api('/api/reconcile-reports')).items || [];
  view.innerHTML = `
    <div class="card grid">
      <div class="row">
        <button class="primary" id="btn-rec" type="button">跑一轮对账</button>
      </div>
      <div class="grid" id="reps">${reports.map(r => `
        <article class="card"><strong>${r.as_of}</strong>
        <p>${r.summary}</p></article>`).join('') || '<p class="muted">暂无对账</p>'}</div>
    </div>`;
  document.getElementById('btn-rec').onclick = async () => {
    try {
      const j = await api('/api/reconcile-reports', {
        method:'POST', body: JSON.stringify({as_of: '2026-11-18'})
      });
      setMsg(j.report.summary);
      await renderReconcile();
    } catch (e) { setMsg(e.message, true); }
  };
}

async function renderWatermark() {
  const data = await api('/api/watermarks');
  const items = data.items || [];
  const rows = items.map(it => {
    if (it.error) {
      return `<article class="card">
        <div class="row"><h3>${it.table_name} · ${it.field_name}</h3>
        <span class="chip bad">${it.error}</span></div>
      </article>`;
    }
    const v = it.progress.verified;
    const okChip = it.same_source
      ? '<span class="chip">同源</span>'
      : '<span class="chip bad">不同源</span>';
    const verified = v.ok
      ? `<span class="chip">校验通过</span><span class="muted">${v.canonical}</span>`
      : `<span class="chip bad">${v.reason || '校验失败'}</span><span class="muted">${v.value ?? '—'}</span>`;
    return `
    <article class="card">
      <div class="row">
        <h3>${it.table_name} · ${it.field_name}</h3>
        <span class="chip">刷新芯片 ${it.chip}</span>
        ${okChip}
      </div>
      <p class="muted">契约 v${it.contract.version_no} · 类型 ${it.contract.field_type}
        · 清洗 ${it.wash.effective_type || '未声明'}</p>
      <div class="row">
        <button type="button" class="primary" data-refresh="${it.table_name}">刷新水位类型</button>
        <input id="adv-${it.table_name}" placeholder="${it.contract.field_type === 'TIMESTAMP' ? '2026-11-18 12:00:00' : '101'}" />
        <button type="button" data-advance="${it.table_name}">推进水位</button>
      </div>
      <div class="grid annex">
        <p><strong>清洗附页</strong> · 生效类型 ${it.wash.effective_type || '未声明'}
          · ${it.wash.verified.ok ? '收口一致' : (it.wash.verified.reason || '收口不一致')}</p>
        <p><strong>进度水线</strong> · 类型 ${it.progress.type || '未刷新'}
          · 水位 ${it.progress.current ?? '—'}
          · ${it.progress.same_source ? '与水位台同源' : '与水位台不同源'}</p>
        <p><strong>字段详情</strong> · ${it.field.table_name}.${it.field.field_name}
          · ${it.field.field_type}（契约 v${it.field.contract_version}）
          · 快照 ${it.snapshot_no || '未刷新'}</p>
      </div>
    </article>`;
  }).join('') || '<p class="muted">暂无水位绑定</p>';

  const snaps = (data.snapshots || []).map(s => `
    <div class="row">
      <span class="chip">${s.snapshot_no}</span>
      <span class="muted">${s.table_name}.${s.field_name} · ${s.resolved_type}
        · ${s.operator} · ${s.created_at}</span>
    </div>`).join('') || '<p class="muted">暂无快照</p>';

  view.innerHTML = `
    <div class="grid">
      <div class="row"><h2>水位台</h2><span class="chip">芯片 ${data.chip}</span></div>
      ${rows}
      <div class="card"><h3>水位类型快照</h3><div class="grid">${snaps}</div></div>
    </div>`;

  view.querySelectorAll('[data-refresh]').forEach(btn => {
    btn.onclick = async () => {
      try {
        const j = await api('/api/watermarks/refresh', {
          method: 'POST', body: JSON.stringify({table: btn.dataset.refresh})
        });
        setMsg(`已刷新 ${j.refreshed.snapshot_no} · ${j.refreshed.resolved_type}`);
        await renderWatermark();
      } catch (e) { setMsg(e.message, true); }
    };
  });
  view.querySelectorAll('[data-advance]').forEach(btn => {
    btn.onclick = async () => {
      const table = btn.dataset.advance;
      const next = document.getElementById(`adv-${table}`).value;
      try {
        const j = await api('/api/watermark/advance', {
          method: 'POST', body: JSON.stringify({table, next})
        });
        setMsg(`水位推进 ${j.type}：${j.from ?? '—'} → ${j.to}`);
        await renderWatermark();
      } catch (e) { setMsg(e.message, true); }
    };
  });
}

async function show(tab) {
  setMsg('');
  await loadContracts();
  if (tab === 'contracts') renderContracts();
  if (tab === 'check') renderCheck();
  if (tab === 'apply') renderApply();
  if (tab === 'reconcile') await renderReconcile();
  if (tab === 'watermark') await renderWatermark();
}

document.querySelectorAll('.pills button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.pills button').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    show(btn.dataset.tab);
  };
});
document.getElementById('btn-logout').onclick = async () => {
  await api('/api/logout', {method:'POST', body:'{}'});
  location.href = '/login';
};
show('contracts');
