'use strict';
function sameUrl(a,b){
  try{const left=new URL(a),right=new URL(b);left.hash='';right.hash='';return decodeURI(left.href)===decodeURI(right.href);}
  catch{return false;}
}
async function uniqueVisiblePage(pages,expectedUrl,bounded){
  const matching=[];
  for(const page of pages){
    if(!sameUrl(page.url(),expectedUrl))continue;
    if(await bounded(page.evaluate('document.visibilityState'),5000)==='visible')matching.push(page);
  }
  if(matching.length>1)throw new Error('多个可见项目窗口打开同一地址，无法唯一匹配。请关闭多余窗口后重新观察。');
  if(matching.length===0)throw new Error('当前标签与前台地址不一致，请重新观察。');
  return matching[0];
}
module.exports={sameUrl,uniqueVisiblePage};
