'use strict';
// Fixed local protocol: rendered DOM observations and scoped browser mouse
// actions. Legacy native input is opt-in. No model code/selectors are accepted.
const {chromium}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
const readline=require('node:readline');
const http=require('node:http');
const crypto=require('node:crypto');
const {sameUrl,uniqueVisiblePage}=require('./page_reader.cjs');
const {allowedStartUrl,spawnVisibleBrowser}=require('./visible_browser.cjs');
const root=path.resolve(__dirname,'..');
const runtimeBuild=require('../runtime_build.cjs').buildFingerprint(root);
const virtualBrowsers=new Map();
const existingBrowsers=new Map();
const snapshotScript=fs.readFileSync(path.join(root,'shopper','dom_snapshot.js'),'utf8');
let context=null;
const contexts=new Map();
const profileContexts=new Map();
const spawnedBrowsers=new Map();
const attachedProfiles=new Set();
const send=(id,result,error)=>process.stdout.write(JSON.stringify(error?{id,error}:{id,result})+'\n');
const bounded=(promise,ms)=>Promise.race([promise,new Promise((_,reject)=>{const timer=setTimeout(()=>reject(new Error('浏览器页面响应超时，请重新打开。')),ms);timer.unref();})]);
async function contextInfo(browserContext,profile){
  const session=await browserContext.browser().newBrowserCDPSession();
  try{
    const info=await session.send('SystemInfo.getProcessInfo');
    const browser=info.processInfo.find(item=>item.type==='browser');
    return {status:'ok',profile_dir:profile,browser_pid:browser?browser.id:null,pages:browserContext.pages().map(page=>page.url())};
  }finally{await session.detach();}
}
async function revealWindow(browserContext){
  for(const page of browserContext.pages()){
    const session=await browserContext.newCDPSession(page);
    try{
      const window=await session.send('Browser.getWindowForTarget');
      await session.send('Browser.setWindowBounds',{windowId:window.windowId,bounds:{windowState:'normal'}});
    }finally{await session.detach();}
    await page.bringToFront();
  }
}
async function hasVisibleWindow(browserContext){
  try{
    for(const page of browserContext.pages()){
      const session=await browserContext.newCDPSession(page);
      try{
        const window=await session.send('Browser.getWindowForTarget');
        const box=window.bounds||{};
        if((box.width||0)>=200&&(box.height||0)>=120&&box.windowState!=='minimized')return true;
      }finally{await session.detach();}
    }
  }catch{}
  return false;
}
async function closeProfile(profileKey){
  const existing=profileContexts.get(profileKey);
  if(existing){
    try{await existing.close();}catch{}
    try{await existing.browser()?.close();}catch{}
    profileContexts.delete(profileKey);
  }
  const child=spawnedBrowsers.get(profileKey);
  if(child&&!child.killed){
    try{child.kill();}catch{}
    spawnedBrowsers.delete(profileKey);
  }
}
async function launchShoppingBrowser(request,profile){
  const profileKey=profile.toLowerCase();
  const existing=profileContexts.get(profileKey);
  if(existing&&existing.browser()&&await hasVisibleWindow(existing)){
    context=existing;
    contexts.set(request.client_id||'pipe',context);
    if(request.start_url){
      const href=allowedStartUrl(request.start_url);
      const page=context.pages()[0]||await context.newPage();
      if(!sameUrl(page.url(),href)) await bounded(page.goto(href,{waitUntil:'domcontentloaded'}),20000);
    }
    await revealWindow(context);
    return await contextInfo(context,profile);
  }
  await closeProfile(profileKey);
  const launched=await spawnVisibleBrowser({
    profile,channel:request.channel,startUrl:request.start_url||'https://www.jd.com/',executable:request.executable,
  });
  spawnedBrowsers.set(profileKey,launched.child);
  const browser=await chromium.connectOverCDP(`http://127.0.0.1:${launched.port}`);
  context=browser.contexts()[0];
  if(!context) throw new Error('专用浏览器已启动，但没有可用窗口。');
  profileContexts.set(profileKey,context);
  contexts.set(request.client_id||'pipe',context);
  await revealWindow(context);
  return await contextInfo(context,profile);
}
async function handle(request){
  if(request.command==='shutdown'){
    await Promise.allSettled([...virtualBrowsers.values(),...existingBrowsers.values()].map(browser=>browser.close()));
    virtualBrowsers.clear();
    existingBrowsers.clear();
    return {status:'ok',shutdown:true};
  }
  if(/^(?:virtual|existing)_/.test(String(request.command))){
    const attachOnly=String(request.command).startsWith('existing_');
    const instances=attachOnly?existingBrowsers:virtualBrowsers;
    const operation=request.command.slice(attachOnly?'existing_'.length:'virtual_'.length);
    if(!['open','navigate','snapshot','act','show','close','screenshot'].includes(operation))throw new Error('不支持的虚拟鼠标命令。');
    const client=request.client_id||'stdio-client';
    let browser=instances.get(client);
    if(!browser&&operation==='open'){
      const BrowserClass=attachOnly?require('./existing_browser.cjs').ExistingBrowser:require('./virtual_browser.cjs').VirtualBrowser;
      browser=new BrowserClass({root,allowed_local_origins:attachOnly?[]:request.allowed_local_origins||[]});
      instances.set(client,browser);
    }
    if(!browser)throw new Error(attachOnly?'尚未连接现有京东标签页，请先点击连接。':'专用京东窗口尚未打开，请先打开京东窗口。');
    const parameters=attachOnly&&operation==='open'
      ? {channel:request.channel||'msedge',...(request.page_id===undefined?{}:{page_id:request.page_id})}
      : request;
    const result=await browser[operation](parameters);
    if(operation==='close')instances.delete(client);
    return result;
  }
  if(request.command==='launch'){
    const profile=path.resolve(request.profile_dir||'');
    if(!profile.startsWith(root+path.sep)) throw new Error('浏览器配置必须位于本项目内。');
    if(!['msedge','chrome'].includes(request.channel)) throw new Error('不支持的浏览器。');
    if(request.app_url){
      const url=new URL(request.app_url);
      if(url.protocol!=='http:'||url.hostname!=='127.0.0.1'||url.username||url.password) throw new Error('应用窗口只允许本地地址。');
      const profileKey=profile.toLowerCase();
      const existing=profileContexts.get(profileKey);
      if(existing){
        try{
          if(existing.browser()&&existing.pages().length){
            context=existing;contexts.set(request.client_id||'pipe',context);return await contextInfo(context,profile);
          }
        }catch{}
        await closeProfile(profileKey);
      }
      const {spawnAppWindow}=require('./visible_browser.cjs');
      const launched=await spawnAppWindow({profile,appUrl:url.href,channel:request.channel,executable:request.executable});
      spawnedBrowsers.set(profileKey,launched.child);
      return {status:'ok',profile_dir:profile,browser_pid:launched.pid,pages:[url.href]};
    }
    return await launchShoppingBrowser(request,profile);
  }
  if(request.command==='connect'){
    const port=Number(request.port);
    if(!Number.isInteger(port)||port<1024||port>65535) throw new Error('读取端口无效。');
    const browser=await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
    context=browser.contexts()[0];
    if(!context) throw new Error('已打开的 Edge 没有可读页面。');
    const key=`cdp:${port}`;
    profileContexts.set(key,context);
    attachedProfiles.add(key);
    contexts.set(request.client_id||'pipe',context);
    try{return await contextInfo(context,key);}
    catch{return {status:'ok',profile_dir:key,browser_pid:null,pages:context.pages().map(page=>page.url())};}
  }
  if(request.command==='status')return readOnlyStatus(request.client_id||'pipe');
  if(request.command==='input'||request.command==='input_access'){
    if(!context)throw new Error('项目浏览器尚未启动。');
    const session=await context.browser().newBrowserCDPSession();
    try{
      const info=await session.send('SystemInfo.getProcessInfo');
      const browserPid=info.processInfo.find(p=>p.type==='browser')?.id;
      if(!Number.isInteger(browserPid)||browserPid<=0)throw new Error('无法确认当前浏览器进程。');
      if(request.command==='input_access')return {status:'ok',...require('./windows_access.cjs').assertInputAccess(browserPid)};
      return await require('./native_input.cjs').input(request,browserPid);
    }finally{await session.detach();}
  }
  if(request.command==='current'){
    if(!context)throw new Error('项目浏览器尚未启动。');
    const pages=[];
    const browserSession=await context.browser().newBrowserCDPSession();
    const processInfo=await browserSession.send('SystemInfo.getProcessInfo');
    const browserPid=processInfo.processInfo.find(p=>p.type==='browser').id;
    await browserSession.detach();
    for(const page of context.pages()){
      if(await bounded(page.evaluate('document.visibilityState'),5000)!=='visible')continue;
      const session=await context.newCDPSession(page);
      try{
        const window=await bounded(session.send('Browser.getWindowForTarget'),5000);
        const screen=await bounded(page.evaluate('({dpr:devicePixelRatio,screen_x:screenX,screen_y:screenY,outer_width:outerWidth,outer_height:outerHeight,inner_width:innerWidth,inner_height:innerHeight})'),5000);
        pages.push({url:page.url(),title:await bounded(page.title(),5000),bounds:window.bounds,window_id:window.windowId,browser_pid:browserPid,screen});
      }finally{await session.detach();}
    }
    return {pages};
  }
  if(request.command==='snapshot'){
    if(!context) throw new Error('项目浏览器尚未启动。');
    const url=new URL(request.expected_url);
    if(!['http:','https:'].includes(url.protocol)||url.username||url.password||!(url.hostname==='jd.com'||url.hostname.endsWith('.jd.com')||url.hostname==='127.0.0.1')) throw new Error('页面地址不在允许范围。');
    const page=await uniqueVisiblePage(context.pages(),url.href,bounded);
    const snapshot=await bounded(page.evaluate(snapshotScript),5000);
    if(!sameUrl(snapshot.url,url.href))throw new Error('读取过程中页面已跳转，请重新观察。');
    return snapshot;
  }
  if(request.command==='close'){
    if(context){
      const profile=[...profileContexts.entries()].find(([,value])=>value===context)?.[0];
      if(profile&&(request.detach_only||attachedProfiles.has(profile))){
        profileContexts.delete(profile);
        attachedProfiles.delete(profile);
        context=null;
        return {status:'ok'};
      }
      if(profile)await closeProfile(profile);
      else try{await bounded(context.close(),5000);}catch{}
    }
    context=null;return {status:'ok'};
  }
  throw new Error('不支持的浏览器命令。');
}
function readOnlyStatus(clientId, clientContexts=contexts, build=runtimeBuild){
  // Health checks must not overwrite the context of an in-flight operation.
  const clientContext=clientContexts.get(clientId);
  return {closed:!clientContext||clientContext.pages().length===0,runtime_build:build,input_modes:['existing_browser','browser_mouse','native']};
}
async function dispatchCommand(request){
  context=contexts.get(request.client_id)||null;
  const result=await handle(request);
  if(request.command==='close')contexts.delete(request.client_id);
  return result;
}
function createCommandHandler({token,dispatch=dispatchCommand,readStatus=readOnlyStatus,onShutdown=()=>{}}){
  let pending=Promise.resolve();
  return (req,res)=>{
    const reply=(code,payload)=>{res.writeHead(code,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(payload));};
    if(req.method!=='POST'||req.url!=='/command'||req.headers.authorization!=='Bearer '+token){reply(403,{error:'Forbidden'});return;}
    let input='';
    req.on('data',chunk=>{input+=chunk;if(input.length>16000)req.destroy();});
    req.on('end',()=>{
      let request;
      const replyError=error=>reply(200,{id:request&&request.id,error:String(error.message||error).slice(0,800),code:error.code||'browser_error',needs_user:error.needs_user!==false});
      try{
        request=JSON.parse(input);
        if(!/^[a-f0-9]{32}$/.test(request?.client_id||''))throw new Error('Invalid client');
      }catch(error){replyError(error);return;}
      // Edge may wait minutes for human authorization. Status is read-only and
      // must remain available to the launcher while that mutation is pending.
      if(request.command==='status'){
        try{reply(200,{id:request.id,result:readStatus(request.client_id)});}
        catch(error){replyError(error);}
        return;
      }
      pending=pending.then(async()=>{
        try{
          const result=await dispatch(request);
          reply(200,{id:request.id,result});
          if(request.command==='shutdown')onShutdown();
        }catch(error){replyError(error);}
      });
    });
  };
}
if(require.main===module){
if(process.argv.includes('--serve')){
  const token=crypto.randomBytes(32).toString('hex');
  const manifest=path.join(root,'data','browser-bridge.json');
  const server=http.createServer(createCommandHandler({token,onShutdown:()=>{
    server.close();
    setTimeout(()=>process.exit(0),250).unref();
  }}));
  server.listen(0,'127.0.0.1',()=>{
    fs.mkdirSync(path.dirname(manifest),{recursive:true});
    fs.writeFileSync(manifest,JSON.stringify({port:server.address().port,token,pid:process.pid}));
  });
}else{
let pending=Promise.resolve();
readline.createInterface({input:process.stdin}).on('line',line=>{
  pending=pending.then(async()=>{
    let request;
    try{request=JSON.parse(line);const result=await handle(request);send(request.id,result);}
    catch(error){send(request&&request.id,null,String(error.message||error).slice(0,800));}
  });
}).on('close',async()=>{if(context)try{await bounded(context.close(),5000);}catch{}process.exit(0);});
}
}
module.exports={createCommandHandler,readOnlyStatus};
