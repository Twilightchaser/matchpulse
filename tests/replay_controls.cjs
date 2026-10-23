// Isolated replay-control regression checks, no browser or network required.
const fs = require('fs');
const vm = require('vm');
const assert = require('node:assert/strict');
const nodes = new Map();
const node = selector => {
  if (!nodes.has(selector)) nodes.set(selector, {value:'',textContent:'',style:{},dataset:{},
    addEventListener(){},replaceChildren(){},appendChild(){},classList:{add(){},remove(){},toggle(){}}});
  return nodes.get(selector);
};
const cancelled=[];
const ctx = vm.createContext({console, URLSearchParams, Set, Map,
 document:{querySelector:node,querySelectorAll:()=>[]},
 window:{matchMedia:()=>({matches:false}),setTimeout(){}},
 requestAnimationFrame:()=>42,cancelAnimationFrame:id=>cancelled.push(id),
 EventSource:class {addEventListener(){} close(){this.closed=true;}},
 fetch:async()=>({ok:true,json:async()=>({snapshot:{},overlay:null,trace:[]})})});
ctx.tr = value => value;
ctx.applyLocale = () => {};
const source=fs.readFileSync('app/static/app.js','utf8');
vm.runInContext(source.slice(0,source.lastIndexOf('\nloadMatch()')),ctx);
vm.runInContext(`
 renderPitchAt = () => {};
 renderSnapshot = () => {};
 refresh = async () => {};
 moments = [{second:100,kind:'shot',player:'A'}, {second:220,kind:'corner',player:'B'}];
 seekMoment(0);
 if (replaySecond !== 94) throw Error('Shot preroll missing');
 seekMoment(1);
 if (replaySecond !== 218) throw Error('Corner preroll missing');
 document.querySelector('#replay-speed').value='1';
 startReplay();
 const oldSource = replaySource;
 startReplay();
 if (!oldSource.closed) throw Error('Old stream left open');
 pauseReplay();
 if (replaySource !== null || animationFrame !== 0) throw Error('Pause failed');
`,ctx);
assert.ok(cancelled.length >= 2);
console.log('Replay controls: seek, preroll, stream replacement, animation cleanup PASS');

vm.runInContext(`
 lineup = [{player_id:'n-10',player_name:'Alex'}];
 frames = Array.from({length:1700},(_,i)=>({second:i*.5,positions:[[i*3.1,0]]}));
 document.querySelector('#sprint-threshold').value='30';
 buildTracking();
 if(trackingAlerts.some(a=>a.text.includes('km/h'))) throw Error('Fabricated 30 km/h alert');
 if(!trackingAlerts.some(a=>a.text.includes('5 km covered'))) throw Error('Distance milestone missed');
 document.querySelector('#sprint-threshold').value='22';
 buildTracking();
 const alerts=trackingAlerts.filter(a=>a.text.includes('km/h'));
 if(!alerts.length || alerts.length>30) throw Error('Speed alerts missing or not debounced');
 if(!eventDetails({event_type:'pass',attributes:{distance_m:20,difficulty:.8,ball_speed_kmh:54}}).includes('hard')) throw Error('Pass details missing');
`,ctx);
console.log('Telemetry: true speed threshold, distance milestone, debounce and pass metrics PASS');
