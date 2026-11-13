const $ = (id) => document.getElementById(id),
  el = (tag, text) => {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    return n;
  },
  encoder = new TextEncoder();
let state = null,
  staged = [],
  selected = null,
  comparison = null,
  busy = false;
const bytes = (n) =>
  n < 1024
    ? `${n} B`
    : n < 1048576
      ? `${(n / 1024).toFixed(1)} KiB`
      : `${(n / 1048576).toFixed(2)} MiB`;
function status(s) {
  $('status').textContent = s;
  $('draft-status').textContent = s;
}
function controls() {
  for (const n of document.querySelectorAll('button,input,select')) n.disabled = busy;
  $('save').disabled = busy || !staged.length;
  for (const id of ['verify', 'zip', 'manifest']) $(id).disabled = busy || !selected;
  $('compare').disabled = busy || !state?.snapshots.length;
  $('comparison-export').disabled = busy || !comparison;
}
async function task(fn) {
  if (busy) return;
  busy = true;
  controls();
  try {
    await fn();
  } catch (e) {
    status(e.message);
  } finally {
    busy = false;
    controls();
  }
}
async function api(path, value) {
  const response = await fetch(
      path,
      value === undefined
        ? {}
        : {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(value),
          },
    ),
    data = await response.json();
  if (!response.ok) throw Error(data.error || 'Request failed.');
  return data;
}
function expected() {
  return Math.max(
    0,
    ...(state?.snapshots || [])
      .filter((s) => s.collection === $('collection').value.trim())
      .map((s) => s.version),
  );
}
function renderExpected() {
  $('expected').textContent = `Expected version ${expected()} → ${expected() + 1}`;
}
function renderStaged() {
  const total = staged.reduce((n, f) => n + f.data.length, 0);
  $('staged-count').textContent = `${staged.length} files / ${bytes(total)}`;
  $('staged').replaceChildren();
  for (let i = 0; i < staged.length; i++) {
    const f = staged[i],
      row = el('div');
    row.className = 'staged-file';
    const input = el('input');
    input.value = f.path;
    input.maxLength = 180;
    input.setAttribute('aria-label', `Path for staged file ${i + 1}`);
    input.addEventListener('input', () => {
      f.path = input.value;
    });
    const remove = el('button', 'Remove');
    remove.setAttribute('aria-label', `Remove staged file ${i + 1}`);
    remove.addEventListener('click', () => {
      staged.splice(i, 1);
      renderStaged();
    });
    row.append(input, el('span', bytes(f.data.length)), remove);
    $('staged').append(row);
  }
  if (!staged.length) $('staged').append(el('p', 'Add files to this snapshot.'));
  renderExpected();
  controls();
}
function study(revised) {
  const block = new Uint8Array(16384),
    second = new Uint8Array(16384),
    third = new Uint8Array(16384);
  for (let i = 0; i < block.length; i++) {
    block[i] = i % 251;
    second[i] = (i * 3 + 17) % 256;
    third[i] = (i * 7 + 9) % 256;
  }
  const join = (a, b) => {
    const out = new Uint8Array(a.length + b.length);
    out.set(a);
    out.set(b, a.length);
    return out;
  };
  staged = [
    {
      path: 'notes/readme.txt',
      data: encoder.encode(
        'Fictional field notes. Two sensor files share their first 16 KiB chunk.\r\n',
      ),
    },
    { path: 'samples/signal-a.bin', data: join(block, revised ? third : second) },
    { path: revised ? 'samples/signal-c.bin' : 'samples/signal-b.bin', data: join(block, block) },
  ];
  renderStaged();
  status(
    `${revised ? 'Revised' : 'Baseline'} study staged. Save a snapshot to persist these files.`,
  );
}
$('baseline').addEventListener('click', () => study(false));
$('revised').addEventListener('click', () => study(true));
$('collection').addEventListener('input', renderExpected);
$('files').addEventListener('change', () =>
  task(async () => {
    try {
      const files = Array.from($('files').files);
      if (files.length > 32) throw Error('Choose at most 32 files at a time.');
      const projected = new Map(staged.map((f) => [f.path, f.data.length]));
      for (const f of files) {
        if (f.size > 2 * 1024 * 1024) throw Error('Each file is limited to 2 MiB.');
        projected.set(f.name, f.size);
      }
      if (
        projected.size > 32 ||
        [...projected.values()].reduce((a, b) => a + b, 0) > 4 * 1024 * 1024
      )
        throw Error('Stage at most 32 files and 4 MiB total.');
      let next = staged.map((f) => ({ ...f }));
      for (const f of files) {
        if (f.size > 2 * 1024 * 1024) throw Error('Each file is limited to 2 MiB.');
        const item = { path: f.name, data: new Uint8Array(await f.arrayBuffer()) },
          i = next.findIndex((x) => x.path === item.path);
        if (i < 0) next.push(item);
        else next[i] = item;
      }
      if (next.length > 32 || next.reduce((n, f) => n + f.data.length, 0) > 4 * 1024 * 1024)
        throw Error('Stage at most 32 files and 4 MiB total.');
      staged = next;
      renderStaged();
      status('Files staged locally. Review their paths before saving.');
    } finally {
      $('files').value = '';
    }
  }),
);
function renderState() {
  const s = state.storage;
  $('logical').textContent = bytes(s.logicalBytes);
  $('stored').textContent = bytes(s.cataloguedChunkBytes);
  $('storage-note').textContent =
    `${s.chunks} catalogued chunks · ${bytes(s.physicalChunkBytes)} on disk · ${s.orphanChunks} unreferenced · ${s.missingChunks} missing`;
  $('snapshot-count').textContent = `${state.snapshots.length} snapshots`;
  $('storage-fill').style.width =
    Math.min(100, s.logicalBytes ? (s.cataloguedChunkBytes / s.logicalBytes) * 100 : 0) + '%';
  $('collection-nav').replaceChildren();
  for (const name of new Set(state.snapshots.map((s) => s.collection))) {
    const b = el('button'),
      icon = el('span', '▱'),
      label = el('span', name),
      count = el('small', state.snapshots.filter((s) => s.collection === name).length);
    icon.setAttribute('aria-hidden', 'true');
    b.append(icon, label, count);
    b.dataset.collection = name;
    b.setAttribute('aria-pressed', selected?.collection === name);
    b.onclick = () =>
      task(async () => {
        $('snapshot').value = state.snapshots.find((s) => s.collection === name).id;
        $('collection').value = name;
        renderExpected();
        showView(false);
        await openSnapshot();
      });
    $('collection-nav').append(b);
  }
  if (!state.snapshots.length) $('collection-nav').append(el('p', 'No collections'));
  $('collections').replaceChildren();
  for (const name of new Set(state.snapshots.map((s) => s.collection))) {
    const opt = el('option');
    opt.value = name;
    $('collections').append(opt);
  }
  for (const id of ['snapshot', 'before', 'after']) {
    const old = $(id).value;
    $(id).replaceChildren();
    for (const s of state.snapshots) {
      const opt = el('option', `${s.collection} / v${s.version}`);
      opt.value = s.id;
      $(id).append(opt);
    }
    if (state.snapshots.some((s) => String(s.id) === old)) $(id).value = old;
  }
  $('empty').hidden = Boolean(state.snapshots.length);
  renderExpected();
  controls();
}
async function refresh() {
  state = await api('/api/state');
  renderState();
}
async function openSnapshot() {
  status('');
  selected = null;
  $('snapshot-detail').hidden = true;
  if (!$('snapshot').value) {
    selected = null;
    $('snapshot-detail').hidden = true;
    controls();
    return;
  }
  selected = await api('/api/snapshots/' + $('snapshot').value);
  $('snapshot-detail').hidden = false;
  $('snapshot-title').textContent = `Version ${selected.version}`;
  $('collection-heading').textContent = selected.collection;
  $('breadcrumb').textContent = 'Collections / ' + selected.collection;
  for (const b of document.querySelectorAll('[data-collection]'))
    b.setAttribute('aria-pressed', b.dataset.collection === selected.collection);
  $('created').textContent = selected.created;
  $('verification').textContent = 'Not verified in this session.';
  $('manifest-digest').textContent = selected.digest;
  $('manifest-files').replaceChildren();
  for (const f of selected.manifest.files) {
    const row = el('tr');
    for (const text of [f.path, f.size.toLocaleString(), f.chunks.length, f.sha256.slice(0, 16)])
      row.append(el('td', text));
    $('manifest-files').append(row);
  }
  controls();
}
$('snapshot').addEventListener('change', () => task(openSnapshot));
$('refresh').addEventListener('click', () =>
  task(async () => {
    await refresh();
    await openSnapshot();
    status('Ledger refreshed. Staged files are unchanged; review them before saving.');
  }),
);
function base64(data) {
  let binary = '';
  for (let i = 0; i < data.length; i += 32768)
    binary += String.fromCharCode(...data.subarray(i, i + 32768));
  return btoa(binary);
}
$('save').addEventListener('click', () =>
  task(async () => {
    const old = state.snapshots.find((s) => s.collection === $('collection').value.trim()),
      s = await api('/api/snapshots', {
        collection: $('collection').value.trim(),
        expected: expected(),
        files: staged.map((f) => ({ path: f.path, base64: base64(f.data) })),
      });
    await refresh();
    $('snapshot').value = s.id;
    $('after').value = s.id;
    $('before').value = old ? old.id : s.id;
    comparison = null;
    $('comparison-result').hidden = true;
    await openSnapshot();
    $('staging-dialog').close();
    showView(false);
    status(`Saved ${s.collection} version ${s.version}.`);
  }),
);
$('verify').addEventListener('click', () =>
  task(async () => {
    $('verification').textContent = 'Verification has not completed successfully.';
    const r = await api('/api/verify', { id: selected.id });
    $('verification').textContent =
      `Verified ${r.files} files and ${r.bytes.toLocaleString()} bytes against every chunk and file digest.`;
    status('Snapshot integrity verified.');
  }),
);
function download(name, blob) {
  const url = URL.createObjectURL(blob),
    a = el('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
$('zip').addEventListener('click', () =>
  task(async () => {
    $('verification').textContent = 'Restore verification has not completed successfully.';
    const r = await fetch(`/api/snapshots/${selected.id}/zip`);
    if (!r.ok) {
      const data = await r.json();
      throw Error(data.error);
    }
    download(`${selected.collection}-v${selected.version}.zip`, await r.blob());
    $('verification').textContent = 'All file/chunk digests verified before ZIP restoration.';
    status('Verified ZIP downloaded. Original metadata is not included.');
  }),
);
$('manifest').addEventListener('click', () => {
  if (selected)
    download(
      'snapshot-manifest.json',
      new Blob([JSON.stringify(selected, null, 2)], { type: 'application/json' }),
    );
});
for (const id of ['before', 'after'])
  $(id).addEventListener('change', () => {
    comparison = null;
    $('comparison-result').hidden = true;
    controls();
  });
$('compare').addEventListener('click', () =>
  task(async () => {
    comparison = await api('/api/compare', {
      before: Number($('before').value),
      after: Number($('after').value),
    });
    $('comparison-result').hidden = false;
    $('comparison-summary').textContent = Object.entries(comparison.counts)
      .map(([k, v]) => `${v} ${k}`)
      .join(' · ');
    $('comparison-rows').replaceChildren();
    for (const r of comparison.rows) {
      const row = el('tr');
      row.className = 'change-' + r.change;
      for (const v of [
        r.path,
        r.change,
        r.beforeSize === null ? '—' : r.beforeSize.toLocaleString(),
        r.afterSize === null ? '—' : r.afterSize.toLocaleString(),
      ])
        row.append(el('td', v));
      $('comparison-rows').append(row);
    }
    status('Manifest comparison complete. Use Verify for content-integrity checks.');
  }),
);
$('comparison-export').addEventListener('click', () => {
  if (comparison)
    download(
      'snapshot-comparison.json',
      new Blob([JSON.stringify(comparison, null, 2)], { type: 'application/json' }),
    );
});
function showView(compare) {
  $('files-view').hidden = compare;
  $('compare-view').hidden = !compare;
  $('files-tab').setAttribute('aria-pressed', !compare);
  $('compare-tab').setAttribute('aria-pressed', compare);
}
$('files-tab').onclick = () => showView(false);
$('compare-tab').onclick = () => showView(true);
$('new-snapshot').onclick = () => {
  $('draft-status').textContent = '';
  $('staging-dialog').showModal();
};
$('close-staging').onclick = () => $('staging-dialog').close();
task(async () => {
  await refresh();
  renderStaged();
  if (state.snapshots.length > 1) $('before').value = state.snapshots[1].id;
  await openSnapshot();
  status('');
});
