// Independent application entry. Node owns browsers and native Windows input;
// Python handles planning and validation. This launch direction avoids child
// process restrictions observed when Python started Chromium on this machine.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawn} = require('node:child_process');
const {buildFingerprint,isCurrentBuild}=require('./runtime_build.cjs');
const ROOT = __dirname;
const APP_CLIENT = crypto.randomBytes(16).toString('hex');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function jsonFetch(url, options={}) {
  const response = await fetch(url,{...options,signal:AbortSignal.timeout(options.timeout || 1500)});
  if (!response.ok) throw new Error(`本地服务返回 ${response.status}`);
  return response.json();
}
async function descriptor() {
  try {
    const value = JSON.parse(fs.readFileSync(path.join(ROOT,'data/browser-bridge.json'),'utf8'));
    if (!Number.isInteger(value.port) || value.port<1024 || value.port>65535 || typeof value.token!=='string') return null;
    return value;
  } catch {return null;}
}
async function bridgeRequest(info,command,extra={}) {
  const response=await jsonFetch(`http://127.0.0.1:${info.port}/command`,{
    method:'POST',headers:{'Content-Type':'application/json',Authorization:`Bearer ${info.token}`},
    body:JSON.stringify({id:crypto.randomUUID(),client_id:APP_CLIENT,command,...extra}),timeout:command==='status'?1800:35000,
  });
  if(response.error)throw new Error(response.error);
  return response.result;
}
async function spawnHidden(executable,args,logName) {
  const log=fs.openSync(path.join(ROOT,'logs',logName),'a');
  const child=spawn(executable,args,{cwd:ROOT,windowsHide:true,detached:true,stdio:['ignore',log,log]});
  fs.closeSync(log);
  await new Promise((resolve,reject)=>{child.once('spawn',resolve);child.once('error',reject);});
  child.unref();
  return child;
}
async function stopProjectServices() {
  // The PowerShell helper verifies full project script arguments again before
  // stopping each PID. It never targets Edge/Chrome or follows process trees.
  await new Promise((resolve,reject)=>{
    const child=spawn('powershell.exe',['-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',path.join(ROOT,'restart-services.ps1'),'-StopOnly'],
      {cwd:ROOT,windowsHide:true,stdio:'ignore'});
    const timeout=setTimeout(()=>{child.kill();reject(new Error('旧版本服务停止超时，请关闭助手后运行重启助手。'));},60000);
    child.once('error',()=>{clearTimeout(timeout);reject(new Error('无法启动本项目服务更新程序。'));});
    child.once('exit',code=>{clearTimeout(timeout);code===0?resolve():reject(new Error('旧版本服务未能安全停止，已取消启动以免继续使用旧代码。'));});
  });
}

async function ensureCurrentServices({expected,readHealth,readBridge,stopServices,startBridge,startBackend}) {
  let [backend,broker]=await Promise.all([readHealth(),readBridge()]);
  if(backend&&backend.app!=='scepter-shopper')throw new Error('APP_PORT 已被其他应用使用，未停止该应用。');
  const outdated=Boolean((backend&&!isCurrentBuild(backend,expected))||(broker&&broker.runtime_build!==expected));
  // A missing half can leave an old supervisor holding its launch lock. Clear
  // only this project's service processes before rebuilding an incomplete pair.
  if(outdated||!backend||!broker){await stopServices();backend=null;broker=null;}
  if(!broker)await startBridge();
  if(!backend)await startBackend();
  [backend,broker]=await Promise.all([readHealth(),readBridge()]);
  if(!isCurrentBuild(backend,expected)||broker?.runtime_build!==expected)
    throw new Error('运行服务与当前代码版本不一致，已取消打开助手。请完成代码更新后重新启动。');
  return {restarted:outdated,runtime_build:expected};
}

async function withStartupLock(root,operation) {
  const file=path.join(root,'data','launcher-startup.lock');
  fs.mkdirSync(path.dirname(file),{recursive:true});
  const deadline=Date.now()+60000;
  while(true) {
    let fd;
    try {fd=fs.openSync(file,'wx');}
    catch(error) {
      if(error.code!=='EEXIST')throw error;
      let owner=0;try{owner=Number(fs.readFileSync(file,'utf8'));}catch{}
      let alive=false;try{if(owner>0){process.kill(owner,0);alive=true;}}catch{}
      let age;
      try{age=Date.now()-fs.statSync(file).mtimeMs;}catch(error){if(error.code==='ENOENT')continue;throw error;}
      if(!alive&&age>5000){try{fs.unlinkSync(file);}catch{}continue;}
      if(Date.now()>deadline)throw new Error('另一个助手启动过程尚未结束，请稍后再打开。');
      await delay(250);continue;
    }
    try {
      fs.writeFileSync(fd,String(process.pid));fs.closeSync(fd);
      return await operation();
    } finally {
      try{if(fs.readFileSync(file,'utf8')===String(process.pid))fs.unlinkSync(file);}catch{}
    }
  }
}

async function main() {
  fs.mkdirSync(path.join(ROOT,'logs'),{recursive:true});
  fs.mkdirSync(path.join(ROOT,'data'),{recursive:true});
  if(!fs.existsSync(path.join(ROOT,'frontend/dist/index.html')) || !fs.existsSync(path.join(ROOT,'browser-runtime/node_modules/playwright/package.json')) || !fs.existsSync(path.join(ROOT,'browser-runtime/node_modules/koffi/package.json')))
    throw new Error('依赖或界面尚未准备好，请先运行 安装依赖.cmd。');
  const env = fs.existsSync(path.join(ROOT,'.env')) ? fs.readFileSync(path.join(ROOT,'.env'),'utf8') : '';
  const match = env.match(/^\s*APP_PORT\s*=\s*(['"]?)(\d+)\1\s*(?:#.*)?$/m);
  const port = Number(process.env.APP_PORT || match?.[2] || 8766);
  if(!Number.isInteger(port)||port<1024||port>65535) throw new Error('APP_PORT 设置无效。');
  const url=`http://127.0.0.1:${port}`;
  const expected=buildFingerprint(ROOT);
  const inputMatch=env.match(/^\s*INPUT_MODE\s*=\s*(['"]?)(existing_browser|browser_mouse|native)\1\s*(?:#.*)?$/m);
  const inputMode=process.env.INPUT_MODE||inputMatch?.[2]||'existing_browser';
  if(inputMode==='native')require('./browser-runtime/windows_access.cjs').assertInputAccess(process.pid);
  const readHealth=async()=>{try{return await jsonFetch(url+'/api/health');}catch{return null;}};
  const readBridge=async()=>{
    const info=await descriptor();
    if(info){try{return await bridgeRequest(info,'status');}catch{}}
    return null;
  };
  const startBridge=async()=>{
    await spawnHidden(process.execPath,[path.join(ROOT,'browser-runtime/bridge.cjs'),'--serve'],'browser-service.log');
    for(let i=0;i<40;i++){
      const broker=await readBridge();
      if(broker?.runtime_build===expected)return;
      await delay(250);
    }
    throw new Error('当前版本浏览器服务启动失败，请查看 logs/browser-service.log。');
  };
  const startBackend=async()=>{
    const interpreter=path.join(ROOT,'.venv/Scripts/python.exe');
    if(!fs.existsSync(interpreter))throw new Error('Python环境不存在，请先运行 安装依赖.cmd。');
    await spawnHidden(process.execPath,[path.join(ROOT,'service_host.cjs'),String(port)],'service-host-startup.log');
    for(let i=0;i<60;i++){
      if(isCurrentBuild(await readHealth(),expected))return;
      await delay(250);
    }
    throw new Error('当前版本本地服务启动失败，请查看 logs/server.log。');
  };
  await withStartupLock(ROOT,()=>ensureCurrentServices({expected,readHealth,readBridge,stopServices:stopProjectServices,startBridge,startBackend}));
  if(process.argv.includes('--service-only'))return;
  const {spawnAppWindow}=require('./browser-runtime/visible_browser.cjs');
  // A changed entry URL prevents an existing --app window from silently
  // retaining JavaScript loaded before a backend/frontend update.
  const appUrl=new URL(url);
  appUrl.searchParams.set('app_build',expected);
  await spawnAppWindow({profile:path.join(ROOT,'data/app-profile'),appUrl:appUrl.href,channel:'msedge'});
  fs.writeFileSync(path.join(ROOT,'data/app-window.json'),JSON.stringify({client_id:APP_CLIENT,url:appUrl.href,opened_at:Date.now()}));
  if(process.argv.includes('--debug'))console.log('购物助手已打开。服务日志：'+path.join(ROOT,'logs/server.log'));
}
if(require.main===module)main().catch(error=>{
  fs.mkdirSync(path.join(ROOT,'logs'),{recursive:true});
  const destination=path.join(ROOT,'logs/launcher-error.txt');
  fs.writeFileSync(destination,`京东购物助手启动未完成\n\n${error.message}\n\n请确认依赖已安装，并查看同目录服务日志。\n`,'utf8');
  if(process.argv.includes('--debug'))console.error(error.message);
  else spawn('notepad.exe',[destination],{detached:true,stdio:'ignore'}).unref();
  process.exitCode=1;
});
module.exports={ensureCurrentServices,withStartupLock};
