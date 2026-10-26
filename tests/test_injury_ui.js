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

vm.runInContext(fn('injuryWindow'),ctx);
let windowRows=ctx.injuryWindow(records,'2026-09-16',1);
assert.equal(windowRows[0].date,'2026-08-16');assert.equal(windowRows.at(-1).date,'2026-09-16');
assert.equal(windowRows.at(-2).knee,0);assert.equal(windowRows.at(-2).notes,records[0].notes);
assert.equal(windowRows[0].knee,undefined);
assert.equal(ctx.injuryWindow([],'2026-05-31',3)[0].date,'2026-02-28');
assert.equal(ctx.injuryWindow([],'2024-05-31',3)[0].date,'2024-02-29');
assert.equal(ctx.injuryWindow([],'2024-02-29',12)[0].date,'2023-02-28');
assert.equal(ctx.injuryWindow([],'2025-02-28',12).length,367);
for(const months of [1,3,6,12]){
 const rows=ctx.injuryWindow(records,'2026-10-25',months);
 ctx.drawInjuries(rows,definitions);
 const ticks=svg.children.filter(x=>x.tag==='text'&&x.attributes.y===288);
 assert.equal(ticks.length,5);assert.equal(ticks[0].attributes.x,44);assert.equal(ticks.at(-1).attributes.x,982);
 assert.equal(rows.some(x=>x.notes),months>1);
}
console.log('Injury calendar ranges, leap years, missing data, notes and full-width ticks passed.');

const tip={style:{},hidden:true};
const hoverContext=vm.createContext({document:{getElementById:()=>tip}});
vm.runInContext(fn('chartTip'),hoverContext);
const hoverSvg={getBoundingClientRect:()=>({left:100,width:1000})};
const yearRows=ctx.injuryWindow([],'2026-10-25',12);
hoverContext.chartTip(hoverSvg,yearRows,row=>row.date,{left:0.044,width:0.938});
for(const index of [0,10,100,yearRows.length-1]){
 hoverSvg.onpointermove({clientX:100+44+index*938/(yearRows.length-1),clientY:200});
 assert.equal(tip.innerHTML,yearRows[index].date);
}
console.log('Year-view tooltip dates align with plotted points.');
