const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../src/garmin_mcp/dashboard.py'),'utf8');
function functionSource(name){let start=source.indexOf('function '+name+'(');if(source.slice(start-6,start)==='async ')start-=6;return source.slice(start,source.indexOf('\n}',start)+2);}
const names=['strengthManualGroup','addManualStrengthGroup','workoutRequest','saveStrengthDetails','manualDeleteButton','deleteManualWorkout'];
const context=vm.createContext({console,AbortController,setTimeout,clearTimeout,TOKEN:'test',MANUAL_GROUP_SEQUENCE:0,Date,
  esc:x=>String(x??'').replace(/"/g,'&quot;'),inputNumber:x=>x??'',manualGroupKey:r=>String(r.id).split(':').slice(0,-1).join(':')});
for(const name of names)vm.runInContext(functionSource(name),context);
async function main(){
  let groups=[{exercise:'Squat',reps:10}],appended=[],status='',nameInput={value:'My workout'},button={disabled:false,textContent:'Save details'},modal={hidden:false},loads=0;
  const list={querySelectorAll:()=>groups,insertAdjacentHTML:(position,html)=>{assert.equal(position,'beforeend');appended.push(html);groups.push({});},lastElementChild:{scrollIntoView(){},querySelector:()=>({focus(){}})}};
  context.document={querySelector:()=>list,getElementById:id=>({'strength-save':button,'strength-modal':modal,'manual-workout-name':nameInput}[id]),body:{classList:{remove(){}}}};
  context.STRENGTH_STATE={mode:'manual',payload:{activityStart:'2026-09-16T12:00:00Z'}};
  context.setStrengthStatus=(text)=>{status=text;};context.load=()=>{loads++;};context.STRENGTH_DIRTY=true;
  context.collectStrengthSets=()=>{throw new Error('Add must not validate or discard incomplete drafts');};
  context.addManualStrengthGroup();context.addManualStrengthGroup();
  assert.equal(groups[0].exercise,'Squat');assert.equal(groups[0].reps,10);assert.equal(nameInput.value,'My workout');assert.equal(groups.length,3);
  const ids=appended.flatMap(html=>Array.from(html.matchAll(/data-set-id="([^"]+)"/g),m=>m[1]));assert.equal(ids.length,6);assert.equal(new Set(ids).size,6);
  const sets=[{exercise:'Squat',reps:10},{exercise:'Row',reps:12}];context.collectStrengthSets=()=>sets;
  let sent;context.fetch=async(endpoint,options)=>{sent={endpoint,payload:JSON.parse(options.body)};return {ok:true,json:async()=>({manualId:'manual-new',sets})};};
  await context.saveStrengthDetails();assert.equal(sent.endpoint,'/api/manual-strength-workouts');assert.equal(sent.payload.activityName,'My workout');assert.deepEqual(sent.payload.sets,sets);assert.equal(modal.hidden,true);assert.equal(context.STRENGTH_DIRTY,false);assert.equal(button.disabled,false);assert.equal(loads,1);
  modal.hidden=false;context.STRENGTH_DIRTY=true;context.fetch=async()=>({ok:false,json:async()=>({error:'Storage unavailable'})});
  await context.saveStrengthDetails();assert.equal(status,'Storage unavailable');assert.equal(button.disabled,false);assert.equal(modal.hidden,false);assert.equal(context.STRENGTH_DIRTY,true);
  nameInput=null;await context.saveStrengthDetails();assert.match(status,/form could not be read/);assert.equal(button.disabled,false);
  nameInput={value:'My workout'};context.collectStrengthSets=()=>{throw new Error('Enter reps');};await context.saveStrengthDetails();assert.equal(status,'Enter reps');assert.equal(button.disabled,false);
  // A network timeout surfaces an error instead of leaving Save disabled.
  const originalTimeout=context.setTimeout;context.setTimeout=fn=>setTimeout(fn,1);
  context.fetch=(_url,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Object.assign(new Error('aborted'),{name:'AbortError'}))));
  context.collectStrengthSets=()=>sets;await context.saveStrengthDetails();assert.match(status,/timed out/);assert.equal(button.disabled,false);assert.equal(modal.hidden,false);context.setTimeout=originalTimeout;
  // Cancel never sends a delete; confirmation targets only the manual endpoint.
  const del={disabled:false,dataset:{manualDelete:'manual-123',manualKind:'strength',manualName:'Test workout'}};let calls=0;
  context.window={confirm:()=>false,alert:()=>{}};context.fetch=async(url,options)=>{calls++;assert.equal(url,'/api/manual-strength-workouts/manual-123');assert.equal(options.method,'DELETE');return {ok:true,json:async()=>({deleted:true})};};
  await context.deleteManualWorkout(del);assert.equal(calls,0);context.window.confirm=()=>true;await context.deleteManualWorkout(del);assert.equal(calls,1);assert.equal(del.disabled,false);assert.equal(loads,2);
  assert.equal(context.manualDeleteButton({activityId:123,name:'Garmin run'}),'');assert.match(context.manualDeleteButton({manualEnduranceId:'local-run',name:'Run'}),/data-manual-kind="endurance"/);
  assert.equal(context.manualDeleteButton({manualId:'linked',source:'merged',name:'Merged gym'}),'');
  assert.equal(context.manualDeleteButton({manualId:'linked',mergedGarminActivityId:123}),'');
  assert.match(context.manualDeleteButton({manualId:'local',source:'manual'}),/data-manual-delete/);
  console.log('Manual exercise append, draft preservation, save success/failure/timeout and delete confirmation passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
