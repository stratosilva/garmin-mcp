const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),crypto=require('node:crypto');
const html=fs.readFileSync(process.argv[2],'utf8');
const scripts=Array.from(html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g));
const seed=JSON.parse(scripts.find(x=>x[1].includes('application/json'))[2]);
for(const script of scripts.filter(x=>!x[1].includes('application/json')))new Function(script[2]);
function client(){
  const nodes={};const node=id=>nodes[id]||(nodes[id]={textContent:'',style:{},classList:{remove(){}},focus(){}});
  node('demo-data').textContent=JSON.stringify(seed);
  const ctx=vm.createContext({console,Response,URL,structuredClone,crypto,location:{origin:'https://demo.invalid'},setTimeout,matchMedia:()=>({matches:true}),load(){},document:{getElementById:node,querySelectorAll:()=>[],querySelector:()=>null,addEventListener(){}}});
  ctx.window=ctx;
  vm.runInContext(scripts.find(x=>!x[1].includes('application/json'))[2],ctx);
  return async(path,body,method)=>{const r=await ctx.fetch(path,{method:method||(body?'POST':'GET'),body:body?JSON.stringify(body):undefined});return {status:r.status,body:await r.json()};};
}
(async()=>{
  const a=client(),b=client();
  assert.equal((await a('/api/dashboard')).body.name,'Miles Ahead');
  const definitions=structuredClone(seed.injuries.definitions);definitions[2].enabled=true;
  await a('/api/injury-settings',{definitions});
  assert.equal((await a('/api/injury-settings')).body.definitions[2].enabled,true);
  assert.equal((await b('/api/injury-settings')).body.definitions[2].enabled,false);
  const row={date:seed.date,notes:'A demo note',...Object.fromEntries(definitions.map(x=>[x.id,2]))};
  assert.equal((await a('/api/injury-measurements',row)).status,200);
  assert.equal((await a('/api/dashboard')).body.injuries.records.at(-1).notes,'A demo note');
  assert.notEqual((await b('/api/dashboard')).body.injuries.records.at(-1).notes,'A demo note');
  assert.equal((await a('/api/injury-measurements',{...row,demo_knee:11})).status,400);
  assert.match((await a('/api/recommendation')).body.text,/prepared example does not regenerate/);
  assert.equal((await a('https://external.invalid/unknown')).status,400);
  assert.equal((await a('/mcp')).status,400);
  const newWorkout=await a('/api/manual-strength-workouts',{activityName:'Demo gym',sets:[]});
  assert.ok((await a('/api/dashboard')).body.recent.some(x=>x.manualId===newWorkout.body.manualId));
  await a('/api/manual-strength-workouts/'+newWorkout.body.manualId,undefined,'DELETE');
  assert.ok(!(await a('/api/dashboard')).body.recent.some(x=>x.manualId===newWorkout.body.manualId));
  console.log('Demo scripts parse; local edits, session isolation, validation and no-network fallback passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
