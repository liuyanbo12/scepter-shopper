'use strict';

// Read-only token checks. SendInput is subject to UIPI; a focus retry cannot
// overcome a higher-integrity target. No process is elevated or restarted.
// https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput
const QUERY_LIMITED_INFORMATION=0x1000;
const TOKEN_QUERY=0x0008;
const TOKEN_INTEGRITY_LEVEL=25;
const ERROR_INSUFFICIENT_BUFFER=122;
const MEDIUM_INTEGRITY=0x2000;
const HIGH_INTEGRITY=0x3000;

class InputAccessError extends Error {
  constructor(code,message,cause){
    super(message,cause?{cause}:undefined);
    this.name='InputAccessError';
    this.code=code;
  }
}

// The adapter is injectable for pure tests. The sender PID is deliberately
// taken from this Node process, never from a browser request or its parent.
function createWindowsAccess(api){
  function processIntegrity(pid){
    if(!Number.isInteger(pid)||pid<=0||pid>0xffffffff)throw new TypeError('Invalid process id');
    const processHandle=api.openProcess(QUERY_LIMITED_INFORMATION,0,pid);
    if(!processHandle)throw new Error('OpenProcess failed');
    const token=[null];
    try{
      if(!api.openProcessToken(processHandle,TOKEN_QUERY,token)||!token[0])throw new Error('OpenProcessToken failed');
      const length=[0];
      const probe=api.getTokenInformation(token[0],TOKEN_INTEGRITY_LEVEL,null,0,length);
      if(!probe&&api.lastError()!==ERROR_INSUFFICIENT_BUFFER)throw new Error('GetTokenInformation size query failed');
      if(!Number.isInteger(length[0])||length[0]<api.pointerSize+4||length[0]>65536)throw new Error('Invalid token buffer size');
      const buffer=Buffer.alloc(length[0]);
      if(!api.getTokenInformation(token[0],TOKEN_INTEGRITY_LEVEL,buffer,buffer.length,length))throw new Error('GetTokenInformation failed');
      // TOKEN_MANDATORY_LABEL starts with SID_AND_ATTRIBUTES.Sid. Keep the
      // buffer alive while following the SID pointers returned by Windows.
      const sid=api.sidFromBuffer(buffer);
      if(!sid||!api.validSid(sid))throw new Error('Invalid integrity SID');
      const countPointer=api.sidCount(sid);
      if(!countPointer)throw new Error('Missing integrity SID count');
      const count=api.readByte(countPointer);
      if(!Number.isInteger(count)||count<1||count>15)throw new Error('Invalid integrity SID count');
      const ridPointer=api.sidAuthority(sid,count-1);
      if(!ridPointer)throw new Error('Missing integrity RID');
      const rid=api.readDword(ridPointer);
      if(!Number.isInteger(rid)||rid<0||rid>0xffffffff)throw new Error('Invalid integrity RID');
      return rid;
    }finally{
      // Attempt both closes even if the first close itself fails.
      try{
        if(token[0]&&!api.closeHandle(token[0]))throw new Error('CloseHandle(token) failed');
      }finally{
        if(!api.closeHandle(processHandle))throw new Error('CloseHandle(process) failed');
      }
    }
  }

  function assertInputAccess(browserPid){
    let sender,target;
    try{
      sender=processIntegrity(process.pid);
      if(sender<MEDIUM_INTEGRITY){
        throw new InputAccessError('sender_restricted','购物助手的输入执行器当前在受限权限环境中运行，无法可靠控制普通浏览器。请关闭此购物助手，从项目文件夹普通双击“启动.vbs”重新打开后重试；不要从受限开发工具启动，也不需要以管理员身份运行。');
      }
      target=processIntegrity(browserPid);
    }catch(error){
      if(error instanceof InputAccessError)throw error;
      throw new InputAccessError('integrity_check_failed','无法核实输入执行器或浏览器进程权限，已停止操作。请确认浏览器仍在运行，并从项目文件夹普通双击“启动.vbs”打开购物助手后重试；不需要以管理员身份运行。',error);
    }
    if(target>sender){
      const reason=target>=HIGH_INTEGRITY?'浏览器以管理员或更高权限运行':'浏览器进程权限高于购物助手';
      throw new InputAccessError('integrity_mismatch',reason+'，Windows 会阻止真实鼠标和键盘输入。请先保存浏览器中的工作，手动关闭该浏览器，再以普通方式打开浏览器和购物助手后重试；程序不会自动关闭浏览器或提升权限。');
    }
    return {sender,target};
  }
  return {processIntegrity,assertInputAccess};
}

let nativeAccess;
function getNativeAccess(){
  if(nativeAccess)return nativeAccess;
  if(process.platform!=='win32')throw new Error('Windows process tokens are unavailable');
  const koffi=require('koffi');
  const kernel=koffi.load('kernel32.dll');
  const security=koffi.load('advapi32.dll');
  nativeAccess=createWindowsAccess({
    pointerSize:koffi.sizeof('void *'),
    openProcess:kernel.func('void * __stdcall OpenProcess(uint32_t access, int inherit, uint32_t pid)'),
    closeHandle:kernel.func('int __stdcall CloseHandle(void *handle)'),
    lastError:kernel.func('uint32_t __stdcall GetLastError()'),
    openProcessToken:security.func('int __stdcall OpenProcessToken(void *process, uint32_t access, _Out_ void **token)'),
    getTokenInformation:security.func('int __stdcall GetTokenInformation(void *token, int infoClass, void *buffer, uint32_t size, _Out_ uint32_t *required)'),
    validSid:security.func('int __stdcall IsValidSid(void *sid)'),
    sidCount:security.func('void * __stdcall GetSidSubAuthorityCount(void *sid)'),
    sidAuthority:security.func('void * __stdcall GetSidSubAuthority(void *sid, uint32_t index)'),
    sidFromBuffer:buffer=>koffi.decode(buffer,'void *'),
    readByte:pointer=>koffi.decode(pointer,'uint8_t'),
    readDword:pointer=>koffi.decode(pointer,'uint32_t'),
  });
  return nativeAccess;
}

function processIntegrity(pid){return getNativeAccess().processIntegrity(pid);}
function assertInputAccess(browserPid){
  try{return getNativeAccess().assertInputAccess(browserPid);}
  catch(error){
    if(error instanceof InputAccessError)throw error;
    throw new InputAccessError('integrity_check_failed','无法读取 Windows 进程权限，已停止操作。请确认从项目文件夹普通双击“启动.vbs”打开购物助手；不需要以管理员身份运行。',error);
  }
}

module.exports={processIntegrity,assertInputAccess,createWindowsAccess,InputAccessError};
