'use strict';
// Launch the shopping browser as a normal visible Edge/Chrome process, then
// attach a read-only Playwright connection. The app window still uses --app.
const fs=require('node:fs');
const net=require('node:net');
const path=require('node:path');
const {spawn}=require('node:child_process');

function allowedStartUrl(raw){
  const url=new URL(raw);
  if(!['http:','https:'].includes(url.protocol)||url.username||url.password) throw new Error('启动地址无效。');
  const host=url.hostname;
  if(!(host==='jd.com'||host.endsWith('.jd.com')||(url.protocol==='http:'&&host==='127.0.0.1'))) throw new Error('启动地址不在允许范围。');
  return url.href;
}

function shoppingArgs(profile,port,startUrl){
  return [
    `--user-data-dir=${profile}`,
    `--remote-debugging-port=${port}`,
    '--remote-debugging-address=127.0.0.1',
    '--no-first-run',
    '--no-default-browser-check',
    '--new-window',
    '--window-size=1280,800',
    '--window-position=120,80',
    startUrl||'https://www.jd.com/',
  ];
}

function appWindowArgs(profile,appUrl){
  const url=new URL(appUrl);
  if(url.protocol!=='http:'||url.hostname!=='127.0.0.1'||url.username||url.password){
    throw new Error('应用窗口只允许本地地址。');
  }
  return [
    `--user-data-dir=${profile}`,
    `--app=${url.href}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--window-size=1400,900',
    '--window-position=80,40',
  ];
}

function resolveBrowserExecutable(channel,supplied){
  const name=channel==='chrome'?'chrome.exe':'msedge.exe';
  const bases=[
    supplied,
    path.join(process.env['PROGRAMFILES(X86)']||'','Microsoft/Edge/Application/msedge.exe'),
    path.join(process.env.PROGRAMFILES||'','Microsoft/Edge/Application/msedge.exe'),
    path.join(process.env.LOCALAPPDATA||'','Microsoft/Edge/Application/msedge.exe'),
    path.join(process.env['PROGRAMFILES(X86)']||'','Google/Chrome/Application/chrome.exe'),
    path.join(process.env.PROGRAMFILES||'','Google/Chrome/Application/chrome.exe'),
    path.join(process.env.LOCALAPPDATA||'','Google/Chrome/Application/chrome.exe'),
  ];
  for(const item of bases){
    if(!item)continue;
    const full=path.resolve(item);
    if(fs.existsSync(full)&&path.basename(full).toLowerCase()===name)return full;
  }
  throw new Error('未找到 Microsoft Edge 或 Google Chrome。');
}

function freePort(){
  return new Promise((resolve,reject)=>{
    const server=net.createServer();
    server.listen(0,'127.0.0.1',()=>{
      const port=server.address().port;
      server.close(error=>error?reject(error):resolve(port));
    });
    server.on('error',reject);
  });
}

async function waitForCdp(port,timeoutMs=20000){
  const deadline=Date.now()+timeoutMs;
  while(Date.now()<deadline){
    try{
      const response=await fetch(`http://127.0.0.1:${port}/json/version`,{signal:AbortSignal.timeout(800)});
      if(response.ok)return await response.json();
    }catch{}
    await new Promise(resolve=>setTimeout(resolve,250));
  }
  throw new Error('专用浏览器窗口未能出现，请查看任务栏后重试。');
}

async function spawnVisibleBrowser({profile,channel,startUrl,executable}){
  const href=allowedStartUrl(startUrl||'https://www.jd.com/');
  const port=await freePort();
  const browser=resolveBrowserExecutable(channel,executable);
  const child=spawn(browser,shoppingArgs(profile,port,href),{
    cwd:path.dirname(browser),
    detached:true,
    stdio:'ignore',
    windowsHide:false,
  });
  child.unref();
  await waitForCdp(port);
  return {port,pid:child.pid,child,startUrl:href};
}

async function spawnAppWindow({profile,appUrl,channel,executable}){
  const args=appWindowArgs(profile,appUrl);
  const browser=resolveBrowserExecutable(channel||'msedge',executable);
  const child=spawn(browser,args,{
    cwd:path.dirname(browser),
    detached:true,
    stdio:'ignore',
    windowsHide:false,
  });
  child.unref();
  return {pid:child.pid,child,appUrl:new URL(appUrl).href};
}

module.exports={
  allowedStartUrl,shoppingArgs,appWindowArgs,resolveBrowserExecutable,freePort,waitForCdp,
  spawnVisibleBrowser,spawnAppWindow,
};
