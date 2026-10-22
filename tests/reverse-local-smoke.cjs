const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http'),net=require('node:net');
const {spawn,spawnSync}=require('node:child_process');
const {prepareProfile,saveSettings,defaults}=require('../desktop/profile');
const resources=process.env.TEST_RESOURCES||path.resolve(__dirname,'..');
const root=path.join(__dirname,'artifacts','reverse-'+Date.now());fs.mkdirSync(root,{recursive:true});
const checks=[],requests=[];let service,child,fd,profile,badRefine=false;
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const check=name=>checks.push({name,ok:true});
function localPort(){return new Promise(r=>{const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>r(p));});});}
(async()=>{
  service=http.createServer((req,res)=>{let raw='';req.on('data',b=>raw+=b);req.on('end',()=>{
    const body=JSON.parse(raw),content=body.messages[1].content;
    requests.push({auth:req.headers.authorization,model:body.model,images:Array.isArray(content)?content.filter(x=>x.type==='image_url').length:0});
    const text=Array.isArray(content)?content.filter(x=>x.type==='text').map(x=>x.text).join('\n'):content;
    let result;
    if(body.messages[0].content.includes('精修')){
      const input=JSON.parse(text);
      result={prompts:input.report.shots.map(s=>({idx:badRefine?999:s.idx,text:'竖屏 9:16。纯色测试画面，保留原分镜事实。光线：均匀。色调：明亮。'})),narration:{a:'Ein Bild.',b:'Ein Bild.'}};
    }else if(Array.isArray(content)&&content.some(x=>x.type==='image_url')){
      result=content.filter(x=>x.type==='image_url').map(()=>({person:'无人',product:'没有商品，只有合成测试色块',outfit:'不适用',scene:'合成测试图',action:'色块变化',camera:'固定',light:'均匀'}));
    }else if(text.includes('结构分析师')){
      result={product_summary:'合成测试图，不是商品',person_summary:'无人',scene_summary:'纯色背景',hook_line:'测试',phys_proof:'无',why:[],ratio:{hook:25,selling:25,proof:25,ending:25},shots:[{idx:1,name:'测试一',t_start:0,t_end:1,shot_size:'特写',action:'静态色块',info:'测试'},{idx:2,name:'测试二',t_start:1,t_end:2,shot_size:'特写',action:'静态色块',info:'测试'}]};
    }else if(text.includes('本条视频的固定设定')){
      result={prompts:[{idx:1,text:'竖屏 9:16。合成测试图。光线：均匀。色调：明亮。'},{idx:2,text:'竖屏 9:16。合成测试图。光线：均匀。色调：明亮。'}]};
    }else if(text.includes('德语旁白')){result={narration:{a:'Ein Bild.',b:'Ein Bild.',notes:'测试'},compliance:[],template:[]};}
    else result={error:'unexpected mock prompt'};
    res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({choices:[{message:{content:JSON.stringify(result)}}]}));
  });});
  await new Promise(r=>service.listen(0,'127.0.0.1',r));
  const port=await localPort(),token='reverse-test-session',profileRoot=path.join(root,'profile');fs.mkdirSync(profileRoot);
  saveSettings(profileRoot,{...defaults,proxy:'http://127.0.0.1:9',visionBaseUrl:`http://127.0.0.1:${service.address().port}/v1`,visionApiKey:'vision-local-test-key',visionModel:'local-vision-fixture'});
  profile=prepareProfile(resources,profileRoot,path.join(root,'without-workbuddy'),token,port);
  fd=fs.openSync(path.join(root,'backend.log'),'a');child=spawn(profile.python,['-B','-E','-X','utf8','-u',profile.server],{cwd:path.dirname(profile.server),env:profile.env,windowsHide:true,stdio:['ignore',fd,fd]});
  async function raw(route,body,method){return fetch(`http://127.0.0.1:${port}${route}`,{headers:{'X-Workbench-Token':token,'Content-Type':Buffer.isBuffer(body)?'application/octet-stream':'application/json'},...(body===undefined?{}:{method:method||'POST',body:Buffer.isBuffer(body)?body:JSON.stringify(body)})});}
  async function api(route,body){const r=await raw(route,body),data=await r.json();assert.equal(r.status,200,route+JSON.stringify(data));return data;}
  for(let i=0;i<150;i++){try{if((await api('/desktop/health')).ok)break;}catch{}await pause(100);}
  assert.deepEqual((await api('/api/reverse/models')).items,[{id:'local-vision-fixture',name:'local-vision-fixture'}]);check('vision model comes from local application settings');
  for(const route of ['/api/skills','/api/handoff','/api/reverse/handoff/123','/echotik/'])assert.equal((await raw(route)).status,404,route);
  check('retired chat handoff, skill scanning and EchoTik routes are absent');
  const ffmpeg=profile.env.PATH.split(path.delimiter).map(p=>path.join(p,'ffmpeg.exe')).find(f=>fs.existsSync(f));assert(ffmpeg);
  const video=path.join(root,'local-fixture.mp4');const encode=spawnSync(ffmpeg,['-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc=size=96x160:rate=4','-t','2','-c:v','libx264','-pix_fmt','yuv420p',video],{windowsHide:true,encoding:'utf8'});assert.equal(encode.status,0,encode.stderr);
  assert.equal((await raw('/api/reverse/import?filename=empty.mp4',Buffer.alloc(0))).status,400);
  assert.equal((await raw('/api/reverse/import?filename=x.exe',Buffer.from('bad'))).status,400);check('empty and non-video uploads are rejected');
  const imported=await api('/api/reverse/import?filename=local-test.mp4',fs.readFileSync(video));const vid=imported.video_id;
  async function waitStage(allowed){for(let i=0;i<300;i++){const s=await api('/api/reverse/status/'+vid);if(allowed.includes(s.stage))return s;if(s.stage==='failed')throw new Error(JSON.stringify(s));await pause(100);}throw new Error('reverse task timeout');}
  const ready=await waitStage(['ready']);assert(ready.frames>0&&ready.probe.width===96&&ready.probe.height===160);assert.equal(requests.length,0);check('real local FFmpeg import, probe, frame extraction and scene detection use no model/network');
  await api('/api/reverse/analyze/'+vid,{model:'local-vision-fixture'});const analyzed=await waitStage(['done']);assert.equal(analyzed.shots,2);assert(analyzed.has_report);assert(requests.some(r=>r.images>0));
  const dir=path.join(profileRoot,'reverse',vid),before=JSON.parse(fs.readFileSync(path.join(dir,'report.json'),'utf8'));check('vision frames, storyboard, generation prompts, narration and HTML report execute without Agent');
  assert.equal((await raw('/api/reverse/refine/'+vid,{instruction:''})).status,400);
  await api('/api/reverse/refine/'+vid,{instruction:'保持事实，缩短旁白'});await waitStage(['done']);
  const refined=JSON.parse(fs.readFileSync(path.join(dir,'report.json'),'utf8'));assert.deepEqual(refined.shots,before.shots);assert.equal(refined.narration.a,'Ein Bild.');assert(fs.readdirSync(dir).some(f=>f.startsWith('report_before_refine_')));check('local refinement preserves observations and backs up the previous report');
  badRefine=true;const unchanged=fs.readFileSync(path.join(dir,'report.json'));await api('/api/reverse/refine/'+vid,{instruction:'测试不完整分镜'});
  let failed;for(let i=0;i<150;i++){failed=await api('/api/reverse/status/'+vid);if(failed.stage==='failed')break;await pause(100);}assert.equal(failed.stage,'failed');assert(fs.readFileSync(path.join(dir,'report.json')).equals(unchanged));check('invalid refinement does not overwrite the existing report');
  assert(requests.every(r=>r.auth==='Bearer vision-local-test-key'&&r.model==='local-vision-fixture'));
  assert(!fs.readFileSync(path.join(root,'backend.log'),'utf8').includes('vision-local-test-key'));check('vision credential is not exposed through models API or logs');
  const sourceBefore=fs.readFileSync(path.join(dir,'src.mp4')),requestsBefore=requests.length;
  await api('/api/reverse/rebuild/'+vid,{});await waitStage(['ready']);assert(fs.readFileSync(path.join(dir,'src.mp4')).equals(sourceBefore));assert.equal(requests.length,requestsBefore);check('local rebuild preserves the original video and uses no network/model');
  assert.equal((await raw('/api/hot/probe',{})).status,400);assert.equal((await api('/api/hot/latest')).cookie_exists,false);check('unconfigured monitor sources are reported instead of a misleading successful empty run');
  fs.writeFileSync(path.join(root,'report.json'),JSON.stringify({ok:true,checks,modelRequests:requests.length,video:vid},null,2));console.log(JSON.stringify({ok:true,checks:checks.length,root}));
})().catch(e=>{fs.writeFileSync(path.join(root,'failure.txt'),e.stack);console.error(e);process.exitCode=1;}).finally(()=>{if(child&&child.exitCode===null)spawnSync('taskkill.exe',['/PID',String(child.pid),'/T','/F'],{windowsHide:true});if(child&&child.exitCode===null)child.kill('SIGKILL');if(fd!==undefined)fs.closeSync(fd);if(service)service.close();});

