'use strict';
// The independent Node host remains alive while Python serves the application.
// It records process exits and retries a bounded number of times. A restarted
// Python service stops interrupted tasks; it never replays shopping actions.
const fs=require('node:fs');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {buildFingerprint}=require('./runtime_build.cjs');

class BackendSupervisor {
  constructor({launch,log=()=>{},delays=[1000,3000,10000],schedule=setTimeout,cancel=clearTimeout}) {
    Object.assign(this,{launch,log,delays,schedule,cancel});
    this.child=null;this.timer=null;this.restarts=0;this.stopped=false;
  }
  start() {
    if(this.stopped||this.child)return;
    const started=Date.now();
    let child,finished=false;
    const ended=(code,signal,error)=>{
      if(finished)return;finished=true;this.child=null;
      this.log({event:'backend_exit',pid:child?.pid??null,code,signal,error:error?String(error.code||error.name):undefined,uptime_ms:Date.now()-started});
      if(this.stopped)return;
      if(this.restarts>=this.delays.length){this.log({event:'retries_exhausted',restarts:this.restarts});return;}
      const delay=this.delays[this.restarts++];
      this.log({event:'restart_scheduled',restart:this.restarts,delay_ms:delay});
      this.timer=this.schedule(()=>{this.timer=null;this.start();},delay);
    };
    try {
      child=this.launch();this.child=child;
      child.once('error',error=>ended(null,null,error));
      child.once('exit',(code,signal)=>ended(code,signal));
      this.log({event:'backend_started',pid:child.pid,restart:this.restarts});
    } catch(error) {ended(null,null,error);}
  }
  stop() {
    this.stopped=true;
    if(this.timer){this.cancel(this.timer);this.timer=null;}
    if(this.child)this.child.kill();
  }
}

function run() {
  const root=__dirname,port=Number(process.argv[2]||8766);
  const runtimeBuild=buildFingerprint(root);
  if(!Number.isInteger(port)||port<1024||port>65535)throw new Error('本地服务端口无效。');
  fs.mkdirSync(path.join(root,'data'),{recursive:true});
  fs.mkdirSync(path.join(root,'logs'),{recursive:true});
  const lock=path.join(root,'data',`backend-${port}.lock`);
  const alive=pid=>{try{process.kill(pid,0);return true;}catch{return false;}};
  let fd;
  for(let attempt=0;attempt<2;attempt++){
    try{fd=fs.openSync(lock,'wx');break;}
    catch(error){
      if(error.code!=='EEXIST')throw error;
      let owner=0;try{owner=Number(fs.readFileSync(lock,'utf8'));}catch{}
      if(owner>0&&alive(owner))return;
      if(!owner&&Date.now()-fs.statSync(lock).mtimeMs<5000)return;
      fs.unlinkSync(lock);
    }
  }
  if(fd===undefined)throw new Error('无法取得本地服务启动锁。');
  fs.writeFileSync(fd,String(process.pid));fs.closeSync(fd);
  const stateFile=path.join(root,'data/backend-service.json');
  const metaLog=path.join(root,'logs/service-host.log');
  const log=event=>{
    const record={at:new Date().toISOString(),host_pid:process.pid,port,runtime_build:runtimeBuild,...event};
    fs.appendFileSync(metaLog,JSON.stringify(record)+'\n');
    fs.writeFileSync(stateFile,JSON.stringify(record));
    if(event.event==='retries_exhausted'){
      fs.writeFileSync(path.join(root,'logs/launcher-error.txt'),'本地服务连续退出，已停止自动重试。\n请查看 logs/service-host.log 与 logs/server.log，然后重新双击启动。\n购物操作不会自动恢复。\n','utf8');
    }
  };
  process.on('exit',()=>{
    try{if(fs.readFileSync(lock,'utf8')===String(process.pid))fs.unlinkSync(lock);}catch{}
  });
  const supervisor=new BackendSupervisor({log,launch:()=>{
    const output=fs.openSync(path.join(root,'logs/server.log'),'a');
    try{
      return spawn(path.join(root,'.venv/Scripts/python.exe'),['-u','-X','faulthandler',path.join(root,'app.py')],{
        cwd:root,windowsHide:true,detached:false,stdio:['ignore',output,output],
        env:{...process.env,APP_PORT:String(port),PYTHONUNBUFFERED:'1',PYTHONFAULTHANDLER:'1',SCEPTER_HOST_BUILD:runtimeBuild},
      });
    }finally{fs.closeSync(output);}
  }});
  supervisor.start();
}

if(require.main===module){
  try{run();}catch(error){console.error(error.message);process.exitCode=1;}
}
module.exports={BackendSupervisor};
