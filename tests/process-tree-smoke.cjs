const fs=require('node:fs'),path=require('node:path'),net=require('node:net'),assert=require('node:assert/strict');
const {spawn,spawnSync}=require('node:child_process'),{prepareProfile}=require('../desktop/profile');
const resources=process.env.TEST_RESOURCES||path.resolve(__dirname,'..');
const root=path.join(__dirname,'artifacts','process-tree-'+Date.now());fs.mkdirSync(root,{recursive:true});
let child,fd,profile;const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
  const port=await new Promise(resolve=>{const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>resolve(p));});});
  const token='tree-test-session';profile=prepareProfile(resources,path.join(root,'profile'),root,token,port);
  fd=fs.openSync(path.join(root,'backend.log'),'a');child=spawn(profile.python,['-B','-E','-X','utf8','-u',profile.server],{env:profile.env,cwd:path.dirname(profile.server),windowsHide:true,stdio:['ignore',fd,fd]});
  async function api(route,body){const r=await fetch(`http://127.0.0.1:${port}${route}`,{headers:{'X-Workbench-Token':token,'Content-Type':'application/json'},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});const d=await r.json();assert.equal(r.status,200,JSON.stringify(d));return d;}
  for(let i=0;i<150;i++){try{if((await api('/desktop/health')).ok)break;}catch{}await pause(100);}
  const script=path.join(profile.root,'pipeline','scripts','tree_fixture.py');
  fs.writeFileSync(script,'import subprocess,sys,time,json,os\np=subprocess.Popen([sys.executable,"-c","import time;time.sleep(90)"],creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))\nprint(json.dumps({"child":os.getpid(),"grandchild":p.pid}),flush=True)\ntime.sleep(90)\n');
  const run=await api('/api/run',{kind:'script',script:'tree_fixture.py'});let pids;
  for(let i=0;i<100;i++){const snap=await api('/api/runs/'+run.id);for(const line of snap.tail||[]){try{const d=JSON.parse(typeof line==='string'?line:line.text);if(d.child&&d.grandchild)pids=d;}catch{}}if(pids)break;await pause(100);}
  assert(pids,'Child process tree did not start');
  assert(fs.readFileSync(path.join(root,'backend.log'),'utf8').includes('owned by a kill-on-close Job'));
  child.kill('SIGKILL');await new Promise(resolve=>child.once('exit',resolve));
  const code=`import ctypes,json,time\nfrom ctypes import wintypes\nk=ctypes.WinDLL('kernel32',use_last_error=True)\nk.OpenProcess.restype=wintypes.HANDLE\nk.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)\nk.GetExitCodeProcess.argtypes=(wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD))\nk.CloseHandle.argtypes=(wintypes.HANDLE,)\ndef alive(pid):\n h=k.OpenProcess(0x1000,False,pid)\n if not h:\n  error=ctypes.get_last_error()\n  if error==87:return False\n  raise RuntimeError('Cannot verify process '+str(pid)+': '+str(error))\n try:\n  status=wintypes.DWORD()\n  if not k.GetExitCodeProcess(h,ctypes.byref(status)):raise RuntimeError('Query failed')\n  return status.value==259\n finally:k.CloseHandle(h)\nids=${JSON.stringify([pids.child,pids.grandchild])}\nfor _ in range(60):\n states=[alive(pid) for pid in ids]\n if not any(states):break\n time.sleep(.1)\nprint(json.dumps({'ids':ids,'alive':states}))\nassert not any(states),states`;
  const verify=spawnSync(profile.python,['-B','-E','-X','utf8','-c',code],{env:profile.env,encoding:'utf8',windowsHide:true});assert.equal(verify.status,0,verify.stdout+verify.stderr);
  fs.writeFileSync(path.join(root,'report.json'),JSON.stringify({ok:true,checks:['backend owns a Windows Job','real child and grandchild launched through current runner','abrupt backend termination reclaims both processes'],processes:JSON.parse(verify.stdout)},null,2));console.log(JSON.stringify({ok:true,root}));
})().catch(e=>{fs.writeFileSync(path.join(root,'failure.txt'),e.stack);console.error(e);process.exitCode=1;}).finally(()=>{if(child&&child.exitCode===null)child.kill('SIGKILL');if(fd!==undefined)fs.closeSync(fd);});
