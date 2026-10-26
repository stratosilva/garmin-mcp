/* Rehab is a context log, not another source of workout calories or load. */
function rehabDay(row){return String(row.date||row.start||'').slice(0,10);}
function rehabComparison(d){
  var entries=(d.rehab||{}).entries||[],results=[];
  ((d.injuries||{}).definitions||[]).filter(x=>x.enabled).forEach(function(injury){
    var checks=entries.filter(x=>x.kind==='checkin'&&x.injuryId===injury.id&&x.pain!=null),pairs=[];
    checks.filter(x=>x.timing==='before').forEach(function(before){
      var next=new Date(before.date+'T00:00:00Z');next.setUTCDate(next.getUTCDate()+1);
      var morning=checks.find(x=>x.timing==='morning'&&x.date===next.toISOString().slice(0,10));
      var activities=((d.adviceHistory||{}).activities||d.recent||[]).filter(x=>rehabDay(x)===before.date);
      if(morning&&activities.length)pairs.push({date:before.date,delta:morning.pain-before.pain,activities:activities.length,rehab:entries.some(x=>x.kind==='session'&&x.date===before.date)});
    });
    if(pairs.length)results.push(injury.name+': '+pairs.length+' activity days with a before-activity and next-morning pain check-in; '+pairs.filter(x=>x.delta>0).length+' higher, '+pairs.filter(x=>x.delta===0).length+' unchanged, '+pairs.filter(x=>x.delta<0).length+' lower. Rehab was logged on '+pairs.filter(x=>x.rehab).length+' of these days (other days unknown). Dates: '+pairs.map(x=>x.date).join(', ')+'.');
  });
  return results;
}
function rehabTimeline(d,months){
  var windowRows=injuryWindow([],d.date,months),start=windowRows[0].date,end=d.date,days={};
  function add(date,item){if(date>=start&&date<=end)(days[date]||(days[date]=[])).push(item);}
  ((d.adviceHistory||{}).activities||d.recent||[]).forEach(function(a){add(rehabDay(a),{kind:'activity',text:(a.name||a.sport||'Activity')+' · '+(a.min==null?'duration unknown':a.min+' min')+(a.km!=null?' · '+a.km+' km':'')+(a.hr!=null?' · '+a.hr+' bpm':'')});});
  ((d.rehab||{}).entries||[]).forEach(function(e){
    var names=((d.injuries||{}).definitions||[]),name=(names.find(x=>x.id===e.injuryId)||{}).name||'Injury';
    var text=e.kind==='session'?'Rehab: '+e.routine.name+' · '+e.difficulty+' — '+e.routine.exercises.map(x=>x.name+(x.dose?' ('+x.dose+')':'')).join('; '):name+' · '+({morning:'Morning',before:'Before activity',after:'After activity',general:'General check-in'}[e.timing]||'Check-in')+(e.pain!=null?' · pain '+e.pain+'/10':'')+(e.stiffness!=null?' · stiffness '+e.stiffness+' min':'')+(e.function?' · walking/stairs '+e.function:'');
    add(e.date,{kind:e.kind,id:e.id,text:text+(e.notes?' — '+e.notes:'')});
  });
  ((d.injuries||{}).records||[]).forEach(function(row){
    var scores=((d.injuries||{}).definitions||[]).filter(x=>x.enabled&&row[x.id]!=null).map(x=>x.name+': '+row[x.id]+'/10');
    if(scores.length||row.notes)add(row.date,{kind:'daily',text:'Daily score (timing unknown): '+scores.join(', ')+(row.notes?' — '+row.notes:'')});
  });
  return Object.keys(days).sort().reverse().map(date=>({date,items:days[date]}));
}
function renderRehab(parent,d){
  var box=el('section','rehab-panel');box.id='rehab-panel';box.style.cssText='border-top:1px solid var(--border);margin-top:20px;padding-top:16px';
  var data=d.rehab||{routines:[],entries:[]},active=(data.routines||[]).filter(x=>x.enabled);
  box.innerHTML='<h3>Rehab & symptom response</h3><p class="metricnote">Log your routine in one tap. Add a check-in when useful; blank fields stay unknown.</p><div class="entry-actions" style="flex-wrap:wrap;gap:8px"><select id="rehab-quick-routine" aria-label="Rehab routine">'+active.map(r=>'<option value="'+esc(r.id)+'">'+esc(r.name)+'</option>').join('')+'</select><button type="button" class="rf" id="rehab-quick" '+(!active.length?'disabled':'')+'>✓ Completed today</button><button type="button" class="rf" id="rehab-log">Log / edit rehab</button><button type="button" class="rf" id="rehab-check">+ Symptom check-in</button><button type="button" class="rf" id="rehab-routines">Manage routines</button></div><p id="rehab-status" class="entry-status" role="status"></p><details><summary style="cursor:pointer;padding:12px 0">Activity & symptom timeline</summary><div class="charttabs" id="rehab-periods">'+[[1,'Month'],[3,'3 months'],[6,'6 months'],[12,'Year']].map(x=>'<button type="button" data-months="'+x[0]+'">'+x[1]+'</button>').join('')+'</div><p class="metricnote">Garmin context currently covers the latest 30 days. Earlier entries show your saved symptoms and rehab only. Rehab is not counted as an extra workout. No rehab entry means unknown.</p><div id="rehab-patterns"></div><button type="button" class="rf" id="rehab-export">Export this timeline (CSV)</button><div id="rehab-timeline" style="max-height:480px;overflow:auto"></div></details>';
  parent.appendChild(box);
  function timeline(months){
    var rows=rehabTimeline(CURRENT_DASHBOARD,months);
    box.querySelectorAll('#rehab-periods button').forEach(b=>{b.classList.toggle('active',Number(b.dataset.months)===months);b.setAttribute('aria-pressed',String(Number(b.dataset.months)===months));});
    var patterns=rehabComparison(CURRENT_DASHBOARD);
    box.querySelector('#rehab-patterns').innerHTML='<p class="metricnote">'+(patterns.length?patterns.map(esc).join('<br>'):'No paired activity-day / next-morning check-ins yet. General daily scores remain visible below.')+'</p><p class="metricnote">Descriptive observations, not cause and effect. Sessions differ; rehab, sleep and other activity may also explain changes.</p>';
    box.querySelector('#rehab-timeline').innerHTML=rows.length?rows.map(day=>'<div style="padding:12px 0;border-bottom:1px solid var(--border)"><b>'+esc(day.date)+'</b>'+day.items.map(item=>'<p style="white-space:pre-wrap">'+esc(item.text)+(item.id?' <button type="button" class="rf" data-edit="'+esc(item.id)+'">Edit</button>':'')+'</p>').join('')+'</div>').join(''):'<p>No entries in this period.</p>';
    box.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>openRehabEntry((CURRENT_DASHBOARD.rehab.entries||[]).find(e=>e.id===b.dataset.edit)));
    box.querySelector('#rehab-export').onclick=function(){
      var csv=[['Date','Type','Details'],...rows.flatMap(day=>day.items.map(item=>[day.date,item.kind,item.text]))].map(row=>row.map(value=>'"'+String(value).replace(/^\s*[=+@-]/,"'$&").replace(/"/g,'""')+'"').join(',')).join('\r\n');
      var url=URL.createObjectURL(new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='injury-timeline-'+d.date+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    };
  }
  timeline(1);box.querySelectorAll('#rehab-periods button').forEach(b=>b.onclick=()=>timeline(Number(b.dataset.months)));
  box.querySelector('#rehab-routines').onclick=()=>openRehabRoutine();
  box.querySelector('#rehab-log').onclick=()=>openRehabEntry({kind:'session'});
  box.querySelector('#rehab-check').onclick=()=>openRehabEntry({kind:'checkin'});
  box.querySelector('#rehab-quick').onclick=async function(){
    var routineId=box.querySelector('#rehab-quick-routine').value;
    var existing=(CURRENT_DASHBOARD.rehab.entries||[]).find(e=>e.kind==='session'&&e.date===d.date&&e.routineId===routineId);
    if(existing){openRehabEntry(existing);return;}
    this.disabled=true;var status=box.querySelector('#rehab-status');status.textContent='Saving…';
    try{await saveRehabData({kind:'session',id:'day-'+d.date+'-'+routineId,date:d.date,routineId,difficulty:'usual',notes:'',quick:true});refreshRehabView();document.getElementById('rehab-status').textContent='Saved for today. Use Log / edit rehab to add details.';}
    catch(error){status.textContent=error.message;this.disabled=false;}
  };
}
async function saveRehabData(payload){
  var result=await workoutRequest('/api/rehab',{method:'POST',headers:{Authorization:'Bearer '+TOKEN,'Content-Type':'application/json'},body:JSON.stringify(payload)});
  CURRENT_DASHBOARD.rehab=result;return result;
}
function rehabDialog(title){
  var old=document.getElementById('rehab-dialog');if(old)old.remove();
  var dialog=el('dialog','injury-settings-dialog');dialog.id='rehab-dialog';
  dialog.innerHTML='<form><h2>'+esc(title)+'</h2><div data-fields></div><p data-status class="entry-status" role="status"></p><div class="entry-actions" style="gap:8px"><button type="button" class="rf" data-cancel>Cancel</button><button type="submit" class="entry-submit">Save</button></div></form>';
  document.body.appendChild(dialog);dialog.dataset.dirty='false';dialog.addEventListener('input',event=>{if(!event.target.matches('[data-select]'))dialog.dataset.dirty='true';});
  function close(e){if(dialog.dataset.busy==='true'||(dialog.dataset.dirty==='true'&&!confirm('Discard unsaved changes?'))){if(e)e.preventDefault();return;}dialog.close();}
  dialog.querySelector('[data-cancel]').onclick=()=>close();dialog.addEventListener('cancel',close);return dialog;
}
function bindRehabSave(dialog,payload){
  dialog.querySelector('form').onsubmit=async function(event){
    event.preventDefault();var status=dialog.querySelector('[data-status]');
    try{var value=payload();dialog.dataset.busy='true';dialog.querySelectorAll('button,input,select,textarea').forEach(e=>e.disabled=true);status.textContent='Saving…';await saveRehabData(value);dialog.dataset.dirty='false';dialog.close();refreshRehabView();}
    catch(error){status.textContent=error.message;}
    finally{dialog.dataset.busy='false';dialog.querySelectorAll('button,input,select,textarea').forEach(e=>e.disabled=false);}
  };
}
function openRehabRoutine(id){
  var routines=(CURRENT_DASHBOARD.rehab||{}).routines||[],routine=routines.find(r=>r.id===id)||(!id?routines[0]:null)||{name:'',exercises:[{name:'',dose:''}],injuryIds:[],enabled:true};
  var dialog=rehabDialog('Rehab routines'),fields=dialog.querySelector('[data-fields]');
  fields.innerHTML='<label class="strength-field">Choose routine<select data-select>'+routines.map(r=>'<option value="'+esc(r.id)+'" '+(r.id===routine.id?'selected':'')+'>'+esc(r.name)+(r.enabled?'':' (disabled)')+'</option>').join('')+'<option value="new" '+(!routine.id?'selected':'')+'>+ New routine</option></select></label><label class="strength-field">Name<input data-name maxlength="100" required value="'+esc(routine.name)+'"></label><label><input type="checkbox" data-enabled '+(routine.enabled?'checked':'')+'> Active routine</label><p class="metricnote">Enter the dosage you follow (for example sets × reps, hold seconds, resistance). These are your records, not a prescribed programme.</p><div data-exercises></div><button type="button" class="rf" data-add>+ Add exercise</button><p>Related injuries (optional)</p>'+((CURRENT_DASHBOARD.injuries||{}).definitions||[]).map(i=>'<label style="display:block"><input type="checkbox" data-injury value="'+esc(i.id)+'" '+((routine.injuryIds||[]).includes(i.id)?'checked':'')+'> '+esc(i.name)+'</label>').join('');
  function add(exercise){var row=el('div');row.style.cssText='display:flex;gap:8px;flex-wrap:wrap;margin:12px 0';row.innerHTML='<input aria-label="Exercise name" data-exercise maxlength="150" required placeholder="Exercise" value="'+esc(exercise.name)+'" style="flex:1;min-width:160px"><input aria-label="Sets, reps, holds and resistance" data-dose maxlength="200" placeholder="Sets, reps / holds, resistance (optional)" value="'+esc(exercise.dose)+'" style="flex:1;min-width:160px"><button type="button" class="rf" aria-label="Remove exercise">×</button>';row.querySelector('button').onclick=()=>{row.remove();dialog.dataset.dirty='true';};fields.querySelector('[data-exercises]').appendChild(row);}
  routine.exercises.forEach(add);fields.querySelector('[data-add]').onclick=()=>{add({name:'',dose:''});dialog.dataset.dirty='true';};
  fields.querySelector('[data-select]').onchange=function(){if(dialog.dataset.dirty==='true'&&!confirm('Discard unsaved changes?')){this.value=routine.id||'new';return;}openRehabRoutine(this.value);};
  bindRehabSave(dialog,()=>({kind:'routine',id:routine.id,name:fields.querySelector('[data-name]').value,enabled:fields.querySelector('[data-enabled]').checked,injuryIds:Array.from(fields.querySelectorAll('[data-injury]:checked'),e=>e.value),exercises:Array.from(fields.querySelector('[data-exercises]').children,row=>({name:row.querySelector('[data-exercise]').value,dose:row.querySelector('[data-dose]').value}))}));dialog.showModal();
}
function openRehabEntry(entry){
  entry=entry||{kind:'session'};var session=entry.kind==='session',d=CURRENT_DASHBOARD;
  var dialog=rehabDialog(session?'Log rehab':'Symptom check-in'),fields=dialog.querySelector('[data-fields]');
  function options(values,selected){return values.map(x=>'<option value="'+esc(x[0])+'" '+(x[0]===selected?'selected':'')+'>'+esc(x[1])+'</option>').join('');}
  fields.innerHTML='<label class="strength-field">Date<input type="date" name="date" required max="'+esc(d.date)+'" value="'+esc(entry.date||d.date)+'"></label>'+(session?'<label class="strength-field">Routine<select name="routineId">'+options(((d.rehab||{}).routines||[]).filter(r=>r.enabled||r.id===entry.routineId).map(r=>[r.id,r.name]),entry.routineId)+'</select></label><label class="strength-field">How was it?<select name="difficulty">'+options([['usual','Usual routine'],['easier','Easier / reduced'],['harder','Harder / increased'],['approximate','Approximate past entry']],entry.difficulty||'usual')+'</select></label><p class="metricnote">Use notes for changes to sets, holds or resistance. Logging annotates your training; it adds no calories or effort points.</p>':'<label class="strength-field">Injury<select name="injuryId">'+options(((d.injuries||{}).definitions||[]).filter(i=>i.enabled||i.id===entry.injuryId).map(i=>[i.id,i.name]),entry.injuryId)+'</select></label><label class="strength-field">When?<select name="timing">'+options([['morning','Morning / on waking'],['before','Before activity'],['after','After activity'],['general','General / unsure']],entry.timing||'morning')+'</select></label><div class="entry-fields"><label>Pain (optional, 0–10)<input name="pain" type="number" min="0" max="10" step="1" value="'+esc(entry.pain??'')+'"></label><label>Stiffness (optional, minutes)<input name="stiffness" type="number" min="0" max="240" step="1" value="'+esc(entry.stiffness??'')+'"></label></div><label class="strength-field">Walking / stairs<select name="function">'+options([['','Not recorded'],['comfortable','Comfortable'],['limited','Some limitation'],['difficult','Difficult']],entry.function||'')+'</select></label>')+'<label class="strength-field">Notes (optional)<textarea name="notes" rows="3" maxlength="2000" placeholder="What changed, helped, or felt different?">'+esc(entry.notes||'')+'</textarea></label>';
  bindRehabSave(dialog,()=>{var form=dialog.querySelector('form'),payload=Object.fromEntries(new FormData(form));payload.kind=entry.kind;payload.id=entry.id;
    if(!payload.id){var duplicate=(d.rehab.entries||[]).find(e=>e.kind===payload.kind&&e.date===payload.date&&(session?e.routineId===payload.routineId:e.injuryId===payload.injuryId&&e.timing===payload.timing));if(duplicate){throw new Error('An entry already exists for this date and '+(session?'routine':'injury / timing')+'. Edit it in the timeline to keep its notes.');}}
    return payload;});
  if(entry.id){var remove=el('button','rf','Delete entry');remove.type='button';dialog.querySelector('.entry-actions').prepend(remove);remove.onclick=async function(){if(!confirm('Delete this entry? This cannot be undone.'))return;dialog.dataset.busy='true';dialog.querySelectorAll('button').forEach(b=>b.disabled=true);try{await saveRehabData({kind:entry.kind,id:entry.id,action:'delete'});dialog.close();refreshRehabView();}catch(error){dialog.querySelector('[data-status]').textContent=error.message;}finally{dialog.dataset.busy='false';dialog.querySelectorAll('button').forEach(b=>b.disabled=false);}};}
  dialog.showModal();
}

function refreshRehabView(){render(CURRENT_DASHBOARD);if(typeof demoAfterRender==='function')demoAfterRender();}
