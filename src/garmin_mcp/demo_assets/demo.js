// All demo edits stay in this page's memory. No live endpoints or AI calls.
(function(){
  'use strict';
  const seed=JSON.parse(document.getElementById('demo-data').textContent);
  let data=structuredClone(seed), edits=0;
  const workouts={};
  const copy=x=>structuredClone(x);
  const response=(body,status=200)=>Promise.resolve(new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}}));
  const sampleAdvice='ILLUSTRATIVE AI ADVICE · FICTIONAL SCENARIO\n\nCARDIO — Choose an easy 20–30 minute walk or gentle ride today. Sleep was shorter than usual, and the knee still deserves attention. Stop if movement aggravates it.\n\nSTRENGTH — An upper-body session is a useful option: light cable rows, comfortable presses and controlled core work. Avoid painful movements; do not chase a weekly target through pain.\n\nRECOVERY — Give yourself a longer sleep window tonight. Record tomorrow’s pain score and how the session felt before increasing intensity. Persistent or worsening symptoms deserve professional assessment.\n\nWHY — This example brings together sleep, training load, muscle exposure and the athlete’s own injury report. The public demo displays prepared advice; the personal product requests AI advice from its current data.';
  window.fetch=function(url,options={}){
    const path=new URL(url,location.origin).pathname,method=options.method||'GET';
    let body={};try{body=JSON.parse(options.body||'{}');}catch{return response({error:'Please check the entry.'},400);}
    if(path==='/api/dashboard')return response(copy(data));
    if(path==='/api/recommendation')return response({text:sampleAdvice+(edits?'\n\nYou have made local edits. This prepared example does not regenerate from those edits.':'')});
    if(path==='/api/injury-settings'){
      if(method==='POST'){
        const rows=body.definitions||[],names=new Set();
        for(const row of rows){if(!row.name?.trim()||names.has(row.name.trim().toLowerCase())||!/^#[0-9a-f]{6}$/i.test(row.color))return response({error:'Use unique names and valid colours.'},400);names.add(row.name.trim().toLowerCase());row.id=row.id||'demo_'+crypto.randomUUID();}
        data.injuries.definitions=rows;edits++;
      }return response({definitions:copy(data.injuries.definitions)});
    }
    if(path==='/api/injury-measurements'){
      if(method==='POST'){
        let row=data.injuries.records.find(x=>x.date===body.date);
        if(!row)return response({error:'Choose a day within this demo’s date range.'},400);
        for(const def of data.injuries.definitions.filter(x=>x.enabled)){const value=Number(body[def.id]);if(body[def.id]===''||!Number.isInteger(value)||value<0||value>10)return response({error:'Enter scores from 0 to 10.'},400);}
        for(const def of data.injuries.definitions.filter(x=>x.enabled))row[def.id]=Number(body[def.id]);
        row.notes=String(body.notes||'').slice(0,2000);edits++;
      }return response(copy(data.injuries));
    }
    if(path==='/api/body-measurements'){
      if(method==='POST'){for(const key of ['weight_kg','fat_pct','muscle_pct','body_water_pct']){let value=Number(body[key]);if(!Number.isFinite(value))return response({error:'Enter numeric measurements.'},400);data.body.metrics[key]={value,delta:value-data.body.metrics[key].value,date:data.date};}edits++;}
      return response(copy(data.body));
    }
    if(path.includes('/candidates')||path.includes('/merge'))return response({error:'Garmin sync is illustrated here. No Garmin account is connected to this public demo.'},400);
    if(path.startsWith('/api/strength-activities/')||path.startsWith('/api/manual-strength-workouts')){
      const manual=path.includes('manual-strength'),id=path.split('/')[3]||'demo_workout_'+crypto.randomUUID();
      if(method==='DELETE'){data.recent=data.recent.filter(x=>x.manualId!==id);delete workouts[id];edits++;return response({deleted:true});}
      if(method==='POST'){
        workouts[id]={...copy(body),manualId:manual?id:undefined,sets:copy(body.sets||[])};
        let activity=data.recent.find(x=>String(x.activityId)===id||x.manualId===id);
        if(!activity){activity={activityId:'manual:'+id,manualId:id,source:'manual',isStrength:true,sport:'strength',km:0,min:45,cal:250,caloriesEstimated:true,hr:null,start:data.date+'T12:00:00'};data.recent.unshift(activity);}
        activity.name=String(body.activityName||'Demo strength session').replace(/[<>]/g,'');edits++;return response(workouts[id]);
      }
      const sets=['Dumbbell bench press','Seated cable row','Plank'].flatMap((exercise,j)=>Array.from({length:3},(_,i)=>({id:'demo-set-'+j+'-'+i,source:'garmin',setType:'ACTIVE',exercise,reps:j===2?null:10,durationSeconds:j===2?30:null,weightKg:j===0?16:j===1?30:null,perSide:false,rawExercise:exercise,rawReps:j===2?null:10,rawDurationSeconds:j===2?30:null,rawWeightKg:j===0?16:j===1?30:null})));
      return response(workouts[id]||{activityId:id,sets,garminSets:copy(sets),exerciseOptions:['Dumbbell bench press','Seated cable row','Plank','Goblet squat'],warning:'Demo only: edits last until this page is reloaded.'});
    }
    if(path.startsWith('/api/manual-endurance-activities')){
      const id=path.split('/')[3]||'demo_cardio_'+crypto.randomUUID();
      if(method==='DELETE'){data.recent=data.recent.filter(x=>x.manualEnduranceId!==id);return response({deleted:true});}
      if(method==='POST'){data.recent.unshift({activityId:id,manualEnduranceId:id,source:'manual',sport:body.sport,name:'Demo '+body.sport,km:Number(body.distanceKm),min:Number(body.minutes),cal:Number(body.calories),start:data.date+'T12:00:00',hr:null});edits++;return response({manualEnduranceId:id});}
    }
    return response({error:'This action is not connected in the public demo.'},400);
  };
  window.demoReset=function(){data=copy(seed);edits=0;Object.keys(workouts).forEach(k=>delete workouts[k]);document.querySelectorAll('dialog[open]').forEach(x=>x.close());document.getElementById('strength-modal')?.remove();load();};
  window.demoAfterRender=function(){
    document.querySelector('.top .sub').textContent='Fictional athlete · synthetic data · explore freely';
    document.querySelector('#app footer').textContent='Public demo · All data is fictional. Edits stay in this tab and reset on reload. No live account or AI service is connected. Analytics remain illustrative snapshots after edits.';
    document.getElementById('refresh-recommendation').textContent='Sample advice';
    document.getElementById('refresh-recommendation').title='Show the prepared example again';
    document.querySelector('#fitnessc').parentElement.querySelector('.metricnote').textContent='Strava-inspired effort and fitness views, estimated from Garmin inputs. Explore the time windows and click the curve to compare dates. This is a model, not a medical measurement.';
    document.querySelector('.bodygrid').id='demo-body';
    document.getElementById('injuryc').parentElement.id='demo-injuries';
    document.getElementById('debtc').parentElement.parentElement.id='demo-sleep';
    document.querySelector('.read').id='demo-advice';
    document.querySelector('.muscle-card').id='demo-muscles';
    document.querySelector('.grid.tri').id='demo-workouts';
    document.querySelector('.keycharts').id='demo-response';
    document.querySelector('#demo-advice').insertAdjacentHTML('afterbegin','<span class="demo-sample-label">Prepared AI example</span>');
    document.querySelector('#demo-injuries .pain-scale').insertAdjacentHTML('beforebegin','<p class="demo-hint">Try it: hover over a day to read its note, or open Settings to restore a recovered injury.</p>');
    document.querySelectorAll('[data-tour-start]').forEach(button=>{button.disabled=false;});
  };
  const steps=[
    {target:'#demo-intro',tag:'01 / THE OPPORTUNITY',title:'One person. Three disconnected pictures.',body:'Garmin-connected data shows training and recovery. Basic-Fit measurements add body composition. Neither tells the whole story of how a sore knee feels today. This product brings those perspectives into one daily view.',try:'A two-minute tour · Next, Enter or → to continue.'},
    {target:'#demo-advice',tag:'02 / THE DECISION',title:'Turn signals into a useful next step.',body:'The personal dashboard uses AI to suggest cardio, strength and recovery actions from the combined context. Here, a prepared example shows the reasoning: sleep is down, the knee needs care, and an easier day is appropriate.',try:'Read the example, including its “why”. The demo makes no live AI calls.'},
    {target:'#demo-sleep',tag:'03 / THE CONTEXT',title:'A sleep score is only the beginning.',body:'Sleep debt, stages, timing and HRV trends make recovery easier to interpret. The aim is to help the athlete understand a pattern, instead of collecting another isolated number.',try:'After the tour, hover over a night to explore the detail.'},
    {target:'#demo-muscles',tag:'04 / THE TRANSLATION',title:'Make strength work visible.',body:'Exercise sets are mapped to muscle groups. Direct lifting work stays separate from estimated supporting exposure, so a busy week of cardio does not look like a complete strength programme.',try:'Use the week arrows and inspect the exercise allocation table.'},
    {target:'#demo-response',tag:'05 / THE ANALYTICS',title:'Move beyond the source metrics.',body:'Strava-inspired effort and fitness charts turn Garmin inputs into a longer view of training response. These are custom estimates, openly attributed—not a claim to reproduce Strava’s proprietary model.',try:'Switch between one month and two years, or select a week to inspect its daily effort.'},
    {target:'#demo-body',tag:'06 / THE CONNECTION',title:'Bring body composition into the picture.',body:'Sync or enter smart scale body composition measurements which sit beside Garmin-connected training and recovery. Different sources become a shared context for decisions—not another dashboard to check separately.',try:'Smart-scale sync is in development. Manual entry is available now; try it after the tour.'},
    {target:'#demo-injuries',tag:'07 / THE MISSING INPUT',title:'Give the athlete a voice.',body:'Pain scores and daily notes add context that a wearable cannot infer reliably. Injuries can be added, renamed, disabled and restored without losing history. Active injury reports also inform the personal dashboard’s advice.',try:'This is the distinctive feature: combine measured signals with self-reported context.'},
    {target:'#demo-workouts',tag:'08 / THE FEEDBACK LOOP',title:'Let people correct the record.',body:'Athletes can enter missing workouts and fix exercise sets. The personal product can merge manual sessions with a later Garmin sync. Better inputs support more useful analytics and advice.',try:'Try “Edit latest strength details”. Public-demo edits stay in this tab.'},
    {target:'#demo-story',tag:'09 / THE STRATEGY',title:'Start with the decision. Build around it.',body:'The strategic work was defining the daily decision, connecting fragmented inputs, making estimates transparent, and keeping people in control. ChatGPT helped turn that product direction into a working dashboard.',try:'You’re ready to explore. No account, token or installation needed.'}
  ];
  let index=0,active=false,previousFocus;
  const card=document.getElementById('demo-tour'),title=document.getElementById('tour-title');
  const launchers=Array.from(document.querySelectorAll('[data-tour-start]')).map(button=>({button,label:button.innerHTML}));
  function launchState(open){launchers.forEach(({button,label})=>{button.setAttribute('aria-expanded',String(open));button.innerHTML=open?'Continue tour →':label;});}
  const tour={start(i=0){if(active){show(index);return;}previousFocus=document.activeElement;active=true;card.hidden=false;launchState(true);show(i);},end(){active=false;card.hidden=true;launchState(false);document.querySelectorAll('.demo-highlight').forEach(e=>e.classList.remove('demo-highlight'));previousFocus?.focus({preventScroll:true});},next(){if(index===steps.length-1){this.end();document.getElementById('demo-injuries').scrollIntoView({behavior:'smooth',block:'center'});}else show(index+1);}};
  function show(i){index=Math.max(0,Math.min(steps.length-1,i));const s=steps[index];document.querySelectorAll('.demo-highlight').forEach(e=>e.classList.remove('demo-highlight'));const target=document.querySelector(s.target);target?.classList.add('demo-highlight');target?.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'});document.getElementById('tour-tag').textContent=s.tag;title.textContent=s.title;document.getElementById('tour-body').textContent=s.body;document.getElementById('tour-try').textContent=s.try;document.getElementById('tour-count').textContent=(index+1)+' of '+steps.length;document.getElementById('tour-progress').style.width=((index+1)/steps.length*100)+'%';document.getElementById('tour-back').disabled=index===0;document.getElementById('tour-next').textContent=index===steps.length-1?'Explore dashboard':'Next →';title.focus({preventScroll:true});}
  document.getElementById('tour-next').onclick=()=>tour.next();document.getElementById('tour-back').onclick=()=>show(index-1);document.getElementById('tour-skip').onclick=()=>tour.end();
  document.querySelectorAll('[data-tour-start]').forEach(e=>e.onclick=()=>tour.start());
  document.getElementById('demo-reset').onclick=()=>{tour.end();window.demoReset();};
  document.getElementById('demo-explore').onclick=()=>{tour.end();document.getElementById('app').scrollIntoView({behavior:'smooth'});};
  document.addEventListener('keydown',e=>{if(!active||e.altKey||e.ctrlKey||e.metaKey||e.target.closest('input,textarea,select,button,a,[contenteditable]')||document.querySelector('dialog[open]')||document.body.classList.contains('modal-open'))return;if(['ArrowRight','Enter',' '].includes(e.key)){e.preventDefault();tour.next();}else if(e.key==='ArrowLeft'){e.preventDefault();show(index-1);}else if(e.key==='Escape'){e.preventDefault();tour.end();}});
})();
