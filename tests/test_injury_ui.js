const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../src/garmin_mcp/dashboard.py'),'utf8');
function fn(name){const start=source.indexOf('function '+name+'(');return source.slice(start,source.indexOf('\n}',start)+2);}
const definitions=[{id:'toe',name:'Old toe',color:'#ff0000',enabled:false},{id:'knee',name:'Knee & leg',color:'#0000ff',enabled:true}];
const records=[{date:'2026-09-15',toe:8,knee:0,notes:'Better <today>\nLess stiff'},{date:'2026-09-16',toe:9,knee:3}];
const svg={children:[],set innerHTML(value){this.children=[];},appendChild(child){this.children.push(child);}};
let tooltip;
const inputs=[{name:'knee',value:''},{name:'new',value:'9'}],date={value:'2026-09-15'};
const notes={value:''};
const form={elements:{namedItem:name=>name==='notes'?notes:date},querySelectorAll:()=>inputs};
const ctx=vm.createContext({Date,ns:'svg',css:x=>x,esc:x=>String(x??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'),
  CURRENT_DASHBOARD:{injuries:{definitions,records}},chartTip:(_svg,_rows,formatter)=>{tooltip=formatter;},
  document:{getElementById:id=>id==='injuryc'?svg:form,createElementNS:(_ns,tag)=>({tag,attributes:{},setAttribute(key,value){this.attributes[key]=value;}})}});
for(const name of ['drawInjuries','fillInjuryScores','injurySettingsRow'])vm.runInContext(fn(name),ctx);
ctx.drawInjuries(records,definitions);
assert.equal(svg.children.filter(x=>x.tag==='path').length,1);
assert.equal(svg.children.filter(x=>x.tag==='circle').length,2);
assert.equal(svg.children.find(x=>x.tag==='path').attributes.stroke,'#0000ff');
assert.doesNotMatch(tooltip(records[0]),/Old toe/);assert.match(tooltip(records[0]),/Knee &amp; leg: 0 \/ 10/);
assert.match(tooltip(records[0]),/Better &lt;today><br>Less stiff/);
definitions[0].enabled=true;ctx.drawInjuries(records,definitions);assert.equal(svg.children.filter(x=>x.tag==='path').length,2);
definitions.forEach(x=>x.enabled=false);ctx.drawInjuries(records,definitions);assert.equal(svg.children.filter(x=>x.tag==='path').length,0);
ctx.fillInjuryScores();assert.equal(notes.value,records[0].notes);assert.equal(inputs[0].value,0);assert.equal(inputs[1].value,'');
date.value='2026-09-16';ctx.fillInjuryScores();assert.equal(inputs[0].value,3);assert.equal(notes.value,'');
assert.match(ctx.injurySettingsRow(definitions[0]),/Disabled · history kept/);
assert.doesNotMatch(ctx.injurySettingsRow(definitions[0]),/ checked/);
assert.match(ctx.injurySettingsRow({name:'<unsafe>',enabled:true}),/&lt;unsafe>/);
console.log('Injury chart visibility, re-enable, empty state, escaped labels and score prefill passed.');
