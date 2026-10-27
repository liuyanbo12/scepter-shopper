'use strict';
// Hash only application code and built UI, never configuration/secrets, logs,
// dependencies, or task journals. Keep this format in sync with runtime_build.py.
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');

function runtimeFiles(root) {
  const files=[];
  for(const name of ['app.py','launcher.cjs','service_host.cjs','runtime_build.cjs','restart-services.ps1']) {
    const file=path.join(root,name);
    if(fs.existsSync(file)&&fs.lstatSync(file).isFile())files.push(name);
  }
  const walk=(relative,accept)=>{
    const directory=path.join(root,relative);
    if(!fs.existsSync(directory))return;
    for(const entry of fs.readdirSync(directory,{withFileTypes:true})) {
      const name=relative+'/'+entry.name;
      if(entry.isDirectory()&&!['node_modules','__pycache__'].includes(entry.name))walk(name,accept);
      else if(entry.isFile()&&accept(name))files.push(name);
    }
  };
  walk('shopper',name=>/\.(?:py|js)$/.test(name));
  walk('browser-runtime',name=>/\.(?:cjs|js|json)$/.test(name));
  walk('frontend/dist',()=>true);
  return files.sort();
}
function buildFingerprint(root) {
  const hash=crypto.createHash('sha256');
  for(const relative of runtimeFiles(root))hash.update(relative+'\0','utf8').update(fs.readFileSync(path.join(root,relative))).update('\0');
  return hash.digest('hex');
}
function isCurrentBuild(health,expected) {
  return Boolean(health&&health.app==='scepter-shopper'&&health.runtime_build===expected&&health.host_build===expected);
}
module.exports={runtimeFiles,buildFingerprint,isCurrentBuild};
