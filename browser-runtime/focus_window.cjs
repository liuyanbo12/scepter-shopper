'use strict';
// Explicit activation only: never call this from ordinary input/observation.
// Connecting input queues lets a background worker activate its own window
// after a user pressed Start/Continue in the separate application window.
async function activateOwnedWindow(api,hwnd,browserPid,wait){
  const pid=[0],targetThread=api.pid(hwnd,pid);
  if(pid[0]!==browserPid)throw new Error('窗口不属于本项目浏览器。');
  if(Number(api.foreground())===hwnd)return {status:'ok',activation_method:'already_foreground'};
  api.ensureQueue();
  if(api.iconic(hwnd))api.show(hwnd,9);
  else api.show(hwnd,5);
  api.focus(hwnd);
  await wait(80);
  if(Number(api.foreground())===hwnd)return {status:'ok',activation_method:'foreground_api'};
  const currentThread=api.currentThread();
  for(let attempt=0;attempt<3;attempt++){
    const foreground=Number(api.foreground());
    if(!foreground)throw new Error('当前Windows桌面不可交互，请解锁或恢复远程桌面。');
    const foregroundThread=api.pid(foreground,[0]);
    const attached=[];
    try{
      for(const thread of new Set([foregroundThread,targetThread])){
        if(thread&&thread!==currentThread&&api.attach(currentThread,thread,true))attached.push(thread);
      }
      // Revalidate ownership immediately before activation; no input is sent
      // to the previous foreground window and no global focus setting changes.
      const verified=[0];api.pid(hwnd,verified);
      if(verified[0]!==browserPid)throw new Error('项目浏览器窗口已变化，请重新打开。');
      api.bring(hwnd);api.active(hwnd);api.focus(hwnd);
    }finally{
      for(const thread of attached.reverse())api.attach(currentThread,thread,false);
    }
    await wait(120);
    if(Number(api.foreground())===hwnd)return {status:'ok',activation_method:'attached_input_queues'};
  }
  throw new Error('Windows暂未允许切换到专用京东窗口。请点击该窗口，再按继续。');
}
module.exports={activateOwnedWindow};
