/* Bench UI is separate from text generation; unchanged session data preserves playback. */
let benchEnabled = false, benchStamp = '', benchRefreshing = false;
const previewURLs = new Map();
const postJSON = (path, value) => api(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(value)});
const basicRenderJobs = renderJobs;
renderJobs = function(jobs) {
  basicRenderJobs(jobs);
  document.querySelectorAll('#jobs article').forEach((article, i) => {
    const job = jobs[i], report = job.decision_report;
    if (report?.variants) {
      const detail = document.createElement('details');
      detail.innerHTML = `<summary>Choix musicaux</summary>${report.variants.map(v => `<p><strong>${v.variant}</strong> : ${v.chain.map(s => escapeHtml(`${s.role}: ${s.plugin}${s.assets.length?' / '+s.assets.join(', '):''}`)).join(' → ')}</p>`).join('')}<p>${escapeHtml((report.warnings||[]).join(' · '))}</p><p>${escapeHtml(report.limitations||'')}</p>`;
      article.append(detail);
    }
    if (job.status !== 'completed') return;
    for (const artifact of job.artifacts) {
      const select = document.createElement('select');
      select.innerHTML = `<option value="">Retour ${artifact.variant}</option><option value="preferred">Ma préférée</option><option value="too_saturated">Trop saturée</option><option value="too_bright">Trop brillante</option><option value="too_dark">Trop sombre</option><option value="noisy">Bruit / craquements</option>`;
      select.onchange = async () => {
        if (!select.value) return;
        try { await postJSON('/api/v1/feedback', {job_id:job.job_id, variant:artifact.variant, label:select.value}); select.value=''; }
        catch(e) { alert(e.message); }
      };
      article.append(select);
    }
    const measure = document.createElement('button');
    measure.className='secondary'; measure.textContent='Comparer / optimiser sur la DI';
    measure.onclick = async () => {
      if (!benchEnabled) return alert('Active [bench] enabled = true dans la configuration Pi, puis redémarre PiPedal AI.');
      if (!$('#maintenance').checked || !$('#diSet').value) return alert('Choisis une DI et confirme une séance hors live dans le panneau du banc.');
      try { const r=await postJSON('/api/v1/bench/evaluate',{job_id:job.job_id,set_id:$('#diSet').value,maintenance_confirmed:true,optimize:true}); $('#benchMessage').textContent=`Séance lancée : ${r.session_id}`; await refreshBench(); }
      catch(e) { alert(e.message); }
    };
    article.append(measure);
  });
};

async function refreshBench() {
  if (benchRefreshing) return;
  benchRefreshing=true;
  try {
    const [status, sets, sessions] = await Promise.all([api('/api/v1/status'),api('/api/v1/di'),api('/api/v1/bench')]);
    benchEnabled=status.bench_enabled;
    const current=$('#diSet').value;
    $('#diSet').innerHTML=sets.map(s=>`<option value="${s.set_id}">${escapeHtml(s.set_id)}</option>`).join('');
    if (sets.some(s=>s.set_id===current)) $('#diSet').value=current;
    const details=await Promise.all(sessions.map(s=>api(`/api/v1/bench/${s.session_id}`)));
    const stamp=JSON.stringify(details);
    if (stamp!==benchStamp) { benchStamp=stamp; renderBench(details); }
  } catch(e) { $('#benchMessage').textContent=e.message; }
  finally { benchRefreshing=false; }
}

function renderBench(sessions) {
  for (const u of previewURLs.values()) URL.revokeObjectURL(u);
  previewURLs.clear();
  $('#benchSessions').innerHTML=sessions.map(s=>`<article class="job"><h3>${escapeHtml(s.session_id)} · ${s.status}</h3>
    ${s.error?`<p class="error">${escapeHtml(s.error)}</p>`:''}<p>${escapeHtml((s.report?.warnings||[]).join(' · '))}</p>
    ${(s.report?.candidates||[]).filter(c=>c.original||c.selected).map(c=>`<p>${c.variant} ${c.selected?'retenue':'initiale'} · distance indicative ${c.score.total.toFixed(3)} · ${c.eligible_for_export?'niveau contrôlé':'à vérifier'} <button class="secondary listen" data-id="${c.candidate_id}">Écouter</button><audio controls id="audio-${c.candidate_id}"></audio></p>`).join('')}
    ${(s.report?.optimized_artifacts||[]).map(a=>`<a class="bench-download" href="/api/v1/bench/${s.session_id}/presets/${a.variant}">${a.variant} mesurée ↓</a> <button class="secondary bench-import" data-path="/api/v1/bench/${s.session_id}/presets/${a.variant}/import">Importer</button>`).join('')}
    </article>`).join('');
  document.querySelectorAll('.listen').forEach(b=>b.onclick=async()=>{
    try {
      if (!previewURLs.has(b.dataset.id)) {
        const r=await fetch(`/api/v1/bench/previews/${b.dataset.id}`,{headers:{'X-PiPedal-AI-Key':key()}});
        if (!r.ok) throw new Error('Préécoute refusée');
        previewURLs.set(b.dataset.id,URL.createObjectURL(await r.blob()));
      }
      const audio=$(`#audio-${b.dataset.id}`); audio.src=previewURLs.get(b.dataset.id); await audio.play();
    } catch(e) { alert(e.message); }
  });
  document.querySelectorAll('.bench-download').forEach(a=>a.onclick=async e=>{
    e.preventDefault();
    try {
      const r=await fetch(a.href,{headers:{'X-PiPedal-AI-Key':key()}});
      if (!r.ok) throw new Error('Téléchargement refusé');
      const u=URL.createObjectURL(await r.blob()), x=document.createElement('a');
      x.href=u; x.download='measured.piPreset'; x.click(); setTimeout(()=>URL.revokeObjectURL(u),1000);
    } catch(e) { alert(e.message); }
  });
  document.querySelectorAll('.bench-import').forEach(b=>b.onclick=async()=>{
    try { await postJSON(b.dataset.path,{}); alert('Preset mesuré importé.'); } catch(e) { alert(e.message); }
  });
}

$('#uploadDI').onclick=async()=>{
  const button=$('#uploadDI'); button.disabled=true;
  try {
    const file=$('#diAudio').files[0]; if(!file) throw new Error('Choisis un fichier WAV DI.');
    if(file.size>32*1024*1024) throw new Error('Utilise di-import pour ce fichier volumineux.');
    const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(file);});
    const set=$('#diSetName').value;
    await postJSON('/api/v1/di/import',{manifest:{schema_version:'pipedal-ai.di-manifest/1.0.0',set_id:set,sample_rate_hz:48000,pcm_bits:24,channels:1,
      files:[{di_id:'dynamics',file:'dynamics.wav',purpose:'dynamics',guitar:$('#diNotes').value,pickup_type:$('#profilePickup').value,pickup_position:$('#diPickup').value,
        guitar_volume:Number($('#diVolume').value),guitar_tone:Number($('#diTone').value),input_gain_note:$('#diNotes').value,performance:'Attaques variées',notes:'Import web'}]},
      files:[{di_id:'dynamics',audio_base64:data}]});
    $('#benchMessage').textContent='DI vérifiée et enregistrée.'; await refreshBench();
  } catch(e) { $('#benchMessage').textContent=e.message; }
  finally { button.disabled=false; }
};
setInterval(refreshBench,5000); refreshBench();
