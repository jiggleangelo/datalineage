const q = selector => document.querySelector(selector);

async function request(path, value) {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-DataLineage-Request': '1'},
    body: JSON.stringify(value)
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.error || 'Request could not be processed');
  }
  return response;
}

async function post(path, value) {
  return (await request(path, value)).json();
}

function notice(message) {
  q('#status').textContent = message;
}

async function download(reportId, format, suffix) {
  try {
    const response = await request('/api/report', {report_id: reportId, format});
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = 'datalineage-' + reportId + suffix;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    notice('Export downloaded.');
  } catch (error) {
    notice(error.message);
  }
}

function render(report) {
  q('#result').hidden = false;
  const roots = report.roots.map(id => report.nodes[id]);
  q('#declared').textContent = roots.map(node => node.title + ': ' + (node.declared.id || node.declared.raw || 'Unknown')).join('; ');
  q('#detected').textContent = roots.map(node => node.title + ': ' + (node.detected.map(info => info.id || info.raw).join(', ') || 'None detected')).join('; ');
  q('#coverage').textContent = report.coverage.pct + '% coverage';
  q('#limits').textContent = report.limits_hit.length ? 'Limits: ' + report.limits_hit.join(', ') + '. Unexpanded references remain unknown.' : '';
  q('#flags').replaceChildren(...report.flags.map(flag => {
    const item = document.createElement('li');
    item.className = flag.severity;
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    const path = document.createElement('span');
    summary.textContent = flag.code + ': ' + flag.message;
    path.textContent = flag.path.join(' → ');
    details.append(summary, path);
    item.append(details);
    return item;
  }));
  q('#tree').replaceChildren(...report.edges.map(edge => {
    const item = document.createElement('li');
    item.textContent = edge.parent + ' → ' + edge.child + ' [' + edge.evidence + ', ' + edge.confidence + (edge.traversed ? '' : ', not expanded') + ']';
    return item;
  }));
  const formats = [
    ['PDF', 'pdf', '.pdf'],
    ['JSON', 'json', '.json'],
    ['CycloneDX', 'cyclonedx', '.cdx.json'],
    ['Template JSON', 'template-json', '.template.json'],
    ['Template PDF', 'template-pdf', '.template.pdf']
  ];
  q('#exports').replaceChildren(...formats.map(([label, format, suffix]) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    button.onclick = () => download(report.report_id, format, suffix);
    return button;
  }));
  notice('Report ready.');
}

q('#check').addEventListener('submit', async event => {
  event.preventDefault();
  notice('Checking dataset…');
  try {
    render(await post('/api/check', {id: q('#dataset').value.trim()}));
  } catch (error) {
    notice(error.message);
  }
});

q('#manifest-file').addEventListener('change', async event => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    if (file.size > 65536) throw new Error('Manifest must be at most 64 KB.');
    q('#manifest').value = await file.text();
    notice('Manifest loaded. Select Build report to continue.');
  } catch (error) {
    notice(error.message);
  }
});

q('#report').onclick = async () => {
  notice('Building report…');
  try {
    const text = q('#manifest').value;
    let manifest;
    try { manifest = JSON.parse(text); } catch { manifest = {text}; }
    render(await post('/api/manifest', manifest));
  } catch (error) {
    notice(error.message);
  }
};

q('#signup').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await post('/api/signup', {email: q('#email').value, provider_answer: q('#provider').value});
    q('#email').value = '';
    notice('Signup saved.');
  } catch (error) {
    notice(error.message);
  }
});

q('#pilot').onclick = async () => {
  try {
    const contact = q('#pilot-contact').value.trim();
    await post('/api/pilot', contact ? {contact} : {});
    q('#pilot-contact').value = '';
    notice('Interest recorded. No payment has been taken.');
  } catch (error) {
    notice(error.message);
  }
};
