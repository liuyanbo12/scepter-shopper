'use strict';
// Fixed physical Windows input. Struct layout follows Koffi's documented
// Win32 SendInput example: https://koffi.dev/composites#win32-example .
const koffi=require('koffi');
const user32=koffi.load('user32.dll');
const kernel32=koffi.load('kernel32.dll');
const {activateOwnedWindow}=require('./focus_window.cjs');
const {assertInputAccess}=require('./windows_access.cjs');
// Model coordinates are physical screen pixels. Make the OS caller use the
// same coordinate system before SetCursorPos/GetCursorPos/WindowFromPoint.
user32.func('bool __stdcall SetProcessDPIAware()')();
const MOUSEINPUT=koffi.struct('SHOPPER_MOUSEINPUT',{dx:'int32_t',dy:'int32_t',mouseData:'uint32_t',dwFlags:'uint32_t',time:'uint32_t',dwExtraInfo:'uintptr_t'});
const KEYBDINPUT=koffi.struct('SHOPPER_KEYBDINPUT',{wVk:'uint16_t',wScan:'uint16_t',dwFlags:'uint32_t',time:'uint32_t',dwExtraInfo:'uintptr_t'});
const INPUT=koffi.struct('SHOPPER_INPUT',{type:'uint32_t',u:koffi.union({mi:MOUSEINPUT,ki:KEYBDINPUT})});
const POINT=koffi.struct('SHOPPER_POINT',{x:'int32_t',y:'int32_t'});
const RECT=koffi.struct('SHOPPER_RECT',{left:'int32_t',top:'int32_t',right:'int32_t',bottom:'int32_t'});
const MSG=koffi.struct('SHOPPER_MSG',{hwnd:'uintptr_t',message:'uint32_t',wParam:'uintptr_t',lParam:'intptr_t',time:'uint32_t',pt:POINT,lPrivate:'uint32_t'});
const api={
  send:user32.func('uint32_t __stdcall SendInput(uint32_t count, SHOPPER_INPUT *inputs, int size)'),
  foreground:user32.func('uintptr_t __stdcall GetForegroundWindow()'),
  pid:user32.func('uint32_t __stdcall GetWindowThreadProcessId(uintptr_t hwnd, _Out_ uint32_t *pid)'),
  cursor:user32.func('bool __stdcall GetCursorPos(_Out_ SHOPPER_POINT *point)'),
  move:user32.func('bool __stdcall SetCursorPos(int x,int y)'),
  rect:user32.func('bool __stdcall GetWindowRect(uintptr_t hwnd,_Out_ SHOPPER_RECT *rect)'),
  at:user32.func('uintptr_t __stdcall WindowFromPoint(SHOPPER_POINT point)'),
  ancestor:user32.func('uintptr_t __stdcall GetAncestor(uintptr_t hwnd,uint32_t flags)'),
  focus:user32.func('bool __stdcall SetForegroundWindow(uintptr_t hwnd)'),
  show:user32.func('bool __stdcall ShowWindow(uintptr_t hwnd,int state)'),
  iconic:user32.func('bool __stdcall IsIconic(uintptr_t hwnd)'),
  metric:user32.func('int __stdcall GetSystemMetrics(int index)'),
  attach:user32.func('bool __stdcall AttachThreadInput(uint32_t first,uint32_t second,bool attach)'),
  currentThread:kernel32.func('uint32_t __stdcall GetCurrentThreadId()'),
  bring:user32.func('bool __stdcall BringWindowToTop(uintptr_t hwnd)'),
  active:user32.func('uintptr_t __stdcall SetActiveWindow(uintptr_t hwnd)'),
  peek:user32.func('bool __stdcall PeekMessageW(_Out_ SHOPPER_MSG *message,uintptr_t hwnd,uint32_t first,uint32_t last,uint32_t remove)'),
};
api.ensureQueue=()=>api.peek({},0,0,0,0);
const delay=ms=>new Promise(r=>setTimeout(r,ms));
const keyCodes={ctrl:0x11,alt:0x12,shift:0x10,l:0x4c,a:0x41,enter:0x0d,esc:0x1b,tab:9,backspace:8,left:0x25,right:0x27,up:0x26,down:0x28,home:0x24,end:0x23,pageup:0x21,pagedown:0x22};
function key(vk,up=false){return {type:1,u:{ki:{wVk:vk,wScan:0,dwFlags:up?2:0,time:0,dwExtraInfo:0}}};}
function mouse(flags,data=0){return {type:0,u:{mi:{dx:0,dy:0,mouseData:data>>>0,dwFlags:flags,time:0,dwExtraInfo:0}}};}
function emit(events){if(api.send(events.length,events,koffi.sizeof(INPUT))!==events.length)throw new Error('Windows未接受全部输入，已停止。');}
function cursor(){const p={};if(!api.cursor(p))throw new Error('当前桌面无法交互。');return p;}
function assertOwned(hwnd,browserPid,focus=true){
  const out=[0];api.pid(hwnd,out);if(out[0]!==browserPid)throw new Error('窗口不属于本项目浏览器。');
  if(focus&&Number(api.foreground())!==hwnd)throw new Error('专用浏览器失去前台焦点，已停止操作。');
  const p=cursor(),width=api.metric(0),height=api.metric(1);
  if((p.x<=1||p.x>=width-2)&&(p.y<=1||p.y>=height-2))throw new Error('鼠标屏幕角落紧急停止。');
}
async function input(request,browserPid){
  const hwnd=Number(request.hwnd);
  if(!Number.isSafeInteger(hwnd)||hwnd<=0)throw new Error('无效窗口。');
  // Diagnose UIPI/restricted-token failures before ordinary focus checks and
  // before any focus, mouse or keyboard operation. Never trust a caller PID.
  assertInputAccess(browserPid);
  assertOwned(hwnd,browserPid,request.operation!=='focus');
  if(request.operation==='focus'){
    const result=await activateOwnedWindow(api,hwnd,browserPid,delay);
    assertOwned(hwnd,browserPid);return result;
  }
  if(request.operation==='type'){
    if(typeof request.text!=='string'||request.text.length>2000||/[\x00-\x1f]/.test(request.text))throw new Error('输入文本无效。');
    const text=request.text;
    for(let i=0;i<text.length;i++){
      assertOwned(hwnd,browserPid);
      const scan=text.charCodeAt(i);
      emit([false,true].map(up=>({type:1,u:{ki:{wVk:0,wScan:scan,dwFlags:4|(up?2:0),time:0,dwExtraInfo:0}}})));
    }
    await delay(120);return {status:'ok'};
  }
  if(request.operation==='keys'){
    const names=request.keys;
    const allowed=names&&Array.isArray(names)&&names.length>0&&names.length<=2&&names.every(x=>Object.hasOwn(keyCodes,x));
    if(!allowed)throw new Error('按键不在允许范围。');
    if(names.length===2&&!['ctrl+l','ctrl+a','alt+left'].includes(names.join('+')))throw new Error('快捷键不在允许范围。');
    try{emit(names.map(n=>key(keyCodes[n])));await delay(50);assertOwned(hwnd,browserPid);}
    finally{emit([...names].reverse().map(n=>key(keyCodes[n],true)));}
    await delay(150);return {status:'ok'};
  }
  if(request.operation==='move'||request.operation==='click'){
    const x=Math.round(request.x),y=Math.round(request.y),box={};api.rect(hwnd,box);
    if(!Number.isFinite(x)||!Number.isFinite(y)||x<=box.left||x>=box.right||y<=box.top||y>=box.bottom)throw new Error('鼠标目标超出项目窗口。');
    if(Number(api.ancestor(api.at({x,y}),2))!==hwnd)throw new Error('目标被其他窗口遮挡。');
    if(!api.move(x,y))throw new Error('Windows未接受鼠标移动。');await delay(90);
    const p=cursor();if(Math.abs(p.x-x)>2||Math.abs(p.y-y)>2)throw new Error(`鼠标位置未达到目标，已停止（目标${x},${y}；实际${p.x},${p.y}）。`);
    assertOwned(hwnd,browserPid);
    if(request.operation==='click'){try{emit([mouse(2)]);await delay(45);}finally{emit([mouse(4)]);}}
    return {status:'ok'};
  }
  if(request.operation==='scroll'){
    const amount=Number(request.amount);if(!Number.isInteger(amount)||Math.abs(amount)>12)throw new Error('滚动幅度无效。');
    emit([mouse(0x800,amount*120)]);return {status:'ok'};
  }
  throw new Error('不支持的原生输入命令。');
}
module.exports={input,cursor,api};
