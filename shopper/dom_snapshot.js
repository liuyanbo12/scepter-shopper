(() => {
  const deadline = performance.now() + 1800;
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const clean = s => (s || '').replace(/\s+/g,' ').trim();
  const centerHit = (el,r,textNode=false) => {
    const left=Math.max(0,r.left),top=Math.max(0,r.top),right=Math.min(innerWidth,r.right),bottom=Math.min(innerHeight,r.bottom);
    const hit=document.elementFromPoint((left+right)/2,(top+bottom)/2);
    if(!hit)return false;
    if(textNode)return hit===el;
    return hit===el || el.contains(hit);
  };
  const textGeometry = (range,el) => {
    // A multiline Range's union can include hidden line-clamp content and the
    // price/control below it. Pick an actually visible line, not that union.
    const clip={left:0,top:0,right:innerWidth,bottom:innerHeight};
    for(let ancestor=el,depth=0;ancestor && depth<40 && performance.now()<deadline;ancestor=ancestor.parentElement,depth++) {
      const style=getComputedStyle(ancestor), box=ancestor.getBoundingClientRect();
      if(/^(hidden|clip|auto|scroll)$/.test(style.overflowX)) {
        clip.left=Math.max(clip.left,box.left);clip.right=Math.min(clip.right,box.right);
      }
      if(/^(hidden|clip|auto|scroll)$/.test(style.overflowY)) {
        clip.top=Math.max(clip.top,box.top);clip.bottom=Math.min(clip.bottom,box.bottom);
      }
    }
    let covered=null, visits=0;
    for(const line of range.getClientRects()) {
      if(visits++>=80 || performance.now()>deadline)break;
      const r={left:Math.max(clip.left,line.left),top:Math.max(clip.top,line.top),
        right:Math.min(clip.right,line.right),bottom:Math.min(clip.bottom,line.bottom)};
      if(r.right-r.left<=2 || r.bottom-r.top<=2)continue;
      if(!covered)covered=r;
      // Never accept a sibling control just because it shares a clickable
      // ancestor. The point sent to the mouse must hit this text's own element.
      if(centerHit(el,r,true))return {rect:r,occluded:false};
    }
    return covered?{rect:covered,occluded:true}:null;
  };
  const path = el => {
    const bits = [];
    for (let n=el; n && n.nodeType===1 && n!==document.documentElement; n=n.parentElement) {
      let index=1; for(let p=n.previousElementSibling;p;p=p.previousElementSibling) if(p.localName===n.localName) index++;
      bits.unshift(n.localName+':nth-of-type('+index+')');
    }
    return 'html>'+bits.join('>');
  };
  const name = el => clean(el.getAttribute('aria-label') || el.getAttribute('title') ||
    (el.labels && Array.from(el.labels).map(l=>l.innerText).join(' ')) ||
    el.getAttribute('placeholder') || el.getAttribute('alt') || el.innerText ||
    (el.type==='submit' ? el.value : '')).slice(0,300);
  const sensitive = el => /password|email|tel|hidden|file/i.test(el.type||'') || /密码|验证码|短信|手机号|手机号码|身份证|银行卡|password|verification|otp/i.test(name(el));
  const role = el => {
    const explicit = el.getAttribute('role');
    const roles={button:'Button',link:'Hyperlink',textbox:'Edit',combobox:'ComboBox',spinbutton:'Edit',checkbox:'CheckBox',radio:'RadioButton',tab:'TabItem'};
    if(roles[explicit]) return roles[explicit];
    if(el.localName==='a') return 'Hyperlink';
    if(el.localName==='button' || /^(button|submit|reset)$/.test(el.type||'')) return 'Button';
    if(el.localName==='select') return 'ComboBox';
    if(el.localName==='input') return el.type==='checkbox'?'CheckBox':el.type==='radio'?'RadioButton':'Edit';
    if(el.localName==='textarea' || el.isContentEditable) return 'Edit';
    return 'Text';
  };
  const inputPurpose = el => {
    // A numeric value, type=number, CSS class, or broad ancestor text alone is
    // not quantity evidence. Only explicit field identifiers/labels qualify.
    const editable = el.localName==='input' && /^(?:text|number|)$/.test(el.type||'') ||
      el.isContentEditable && el.getAttribute('role')==='spinbutton';
    if(!editable || sensitive(el)) return {};
    const quantityLabel = text => /^(?:(?:购买|商品|选购)?数量|件数|quantity|qty)(?:[：:]|[（(]件[）)])?$/i.test(clean(text).replace(/\s/g,''));
    const fieldId = /^(?:buy[-_]?(?:num|count|quantity)|purchase[-_]?(?:quantity|qty)|quantity|qty|changeQuantity|change[-_]?num|(?:product|sku|goods|item)[-_]?(?:quantity|qty))$/i;
    for(const attr of ['id','name']) {
      const value=el.getAttribute(attr)||'';
      if(fieldId.test(value)) return {input_purpose:'quantity',input_purpose_evidence:`${attr}:${value}`};
    }
    if(quantityLabel(name(el))) return {input_purpose:'quantity',input_purpose_evidence:'explicit_label'};
    if(/cart\.jd\.com$/i.test(location.hostname) && /\bitxt\b/i.test(el.className||''))
      return {input_purpose:'quantity',input_purpose_evidence:'jd_cart_itxt'};
    const labelledBy=(el.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean).map(id=>document.getElementById(id));
    if(labelledBy.length && labelledBy.every(label=>label && visible(label)) && quantityLabel(labelledBy.map(label=>label.innerText).join(' '))) {
      return {input_purpose:'quantity',input_purpose_evidence:'aria-labelledby'};
    }
    const parent=el.parentElement;
    const controls='input,select,textarea,[contenteditable="true"],[role="spinbutton"]';
    if(parent && parent.querySelectorAll(controls).length===1) {
      for(const sibling of [el.previousElementSibling,el.nextElementSibling]) {
        if(sibling && /^(?:LABEL|SPAN|DIV|DT)$/.test(sibling.tagName) && !sibling.querySelector(controls) && visible(sibling) && quantityLabel(sibling.innerText)) {
          return {input_purpose:'quantity',input_purpose_evidence:'adjacent_label'};
        }
      }
      const directLabel=Array.from(parent.childNodes).filter(node=>node.nodeType===Node.TEXT_NODE).map(node=>node.nodeValue).join(' ');
      if(quantityLabel(directLabel)) return {input_purpose:'quantity',input_purpose_evidence:'adjacent_text'};
    }
    return {};
  };
  const cartCache=new WeakMap();
  const cartIdentity = el => {
    if(location.hostname!=='cart.jd.com')return {};
    if(cartCache.has(el))return cartCache.get(el);
    const skuOf = href => {
      try {const u=new URL(href,location.href);return u.hostname==='item.jd.com'?(u.pathname.match(/^\/(\d+)\.html$/)||[])[1]||'':'';}catch{return '';}
    };
    let result={};
    for(let row=el,depth=0;row && row!==document.body && depth<9 && performance.now()<deadline;row=row.parentElement,depth++) {
      // A row must contain one exact product identity and one quantity field.
      // The outer cart container and recommendation cards cannot qualify.
      const controls=Array.from(row.querySelectorAll('input,[role="spinbutton"]')).filter(node=>inputPurpose(node).input_purpose==='quantity');
      if(controls.length!==1)continue;
      const skus=new Set(Array.from(row.querySelectorAll('a[href]')).map(a=>skuOf(a.href)).filter(Boolean));
      if(skus.size!==1)continue;
      result={cart_row:path(row),cart_sku:[...skus][0]};break;
    }
    cartCache.set(el,result);return result;
  };
  const elements=[];
  const selected=document.querySelectorAll('a[href],button,input,select,textarea,[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="textbox"],[role="combobox"],[role="spinbutton"],[tabindex],label,[contenteditable="true"]');
  for(const el of selected) {
    if(elements.length>=500 || performance.now()>deadline) break;
    if(!visible(el) || sensitive(el)) continue;
    const r=el.getBoundingClientRect(), n=name(el), ro=role(el);
    if(!n && !['Edit','ComboBox'].includes(ro)) continue;
    let parent=el.parentElement, ancestors=[];
    for(let i=0;parent && i<3;i++,parent=parent.parentElement) {
      const t=clean(parent.getAttribute('aria-label') || parent.innerText).slice(0,200); if(t && t!==n) ancestors.push(t);
    }
    const group=el.closest('[data-sku],article,.gl-item,.item-item,.goods-item');
    elements.push({dom_path:path(el),name:n||'未标注输入框',role:ro,value:['Edit','ComboBox'].includes(ro)?String(el.value||'').slice(0,500):'',...inputPurpose(el),...cartIdentity(el),
      css_rect:[Math.max(0,r.left),Math.max(0,r.top),Math.min(innerWidth,r.right),Math.min(innerHeight,r.bottom)],
      enabled:!el.disabled && el.getAttribute('aria-disabled')!=='true',visible:true,href:el.localName==='a'?el.href:undefined,
      occluded:!centerHit(el,r),
      parent_name:ancestors[0]||'',ancestor_names:ancestors.reverse(),group_text:group?clean(group.innerText).slice(0,700):'',
      sku:group?(group.getAttribute('data-sku')||''):'',checked:typeof el.checked==='boolean'?el.checked:undefined,
      selected:el.getAttribute('aria-selected')==='true'||el.getAttribute('aria-checked')==='true'||el.checked===true||el.selected===true,
      class_name:String(el.className||'').slice(0,160)});
  }
  const texts=[], seen=new Set(), textElements=[]; let length=0, visits=0;
  const walker=document.createTreeWalker(document.body||document.documentElement,NodeFilter.SHOW_TEXT);
  while(walker.nextNode() && visits++<20000 && length<18000 && performance.now()<deadline) {
    const node=walker.currentNode, el=node.parentElement;
    if(!el || /^(SCRIPT|STYLE|NOSCRIPT|TEXTAREA)$/.test(el.tagName) || !visible(el)) continue;
    const t=clean(node.nodeValue);
    if(t && !seen.has(t)) {seen.add(t);texts.push(t);length+=t.length+1;}
    if(t && textElements.length<300) {
      const range=document.createRange(); range.selectNodeContents(node);
      const geometry=textGeometry(range,el);
      if(geometry) {
        const r=geometry.rect;
        const owner=el.closest('a[href],button,[role="button"],[role="link"]');
        if(!owner || name(owner)!==t) {
          const group=el.closest('[data-sku],article,.gl-item,.item-item,.goods-item');
          const parentText=clean(el.parentElement?el.parentElement.innerText:'').slice(0,200);
          textElements.push({dom_path:path(el)+'::text('+Array.from(el.childNodes).indexOf(node)+')',name:t.slice(0,300),role:'Text',value:'',...cartIdentity(el),
            css_rect:[Math.max(0,r.left),Math.max(0,r.top),Math.min(innerWidth,r.right),Math.min(innerHeight,r.bottom)],enabled:true,visible:true,
            occluded:geometry.occluded,
            parent_name:parentText,ancestor_names:[parentText],group_text:group?clean(group.innerText).slice(0,700):'',
            sku:group?(group.getAttribute('data-sku')||''):'',href:owner&&owner.localName==='a'?owner.href:undefined,
            click_owner_name:owner?name(owner):''});
        }
      }
    }
  }
  const active=document.activeElement;
  const selected_specs=Array.from(document.querySelectorAll('#choose-attrs .selected,#choose-attrs input:checked,.sku-choose [aria-selected="true"],.sku-choose [aria-checked="true"],[data-sku-option][aria-selected="true"]'))
    .filter(visible).map(el=>name(el)).filter(Boolean).slice(0,30);
  const rootScroller=document.scrollingElement||document.documentElement;
  const scroll_meta={scroll_x:window.scrollX||0,scroll_y:window.scrollY||0,
    scroll_height:rootScroller?rootScroller.scrollHeight:0,client_height:rootScroller?rootScroller.clientHeight:innerHeight,
    max_scroll_y:rootScroller?Math.max(0,rootScroller.scrollHeight-rootScroller.clientHeight):0,
    viewport_height:innerHeight};
  return {url:location.href,title:document.title,ready_state:document.readyState,text:texts.join('\n').slice(0,18000),elements:elements.concat(textElements),selected_specs,
    viewport:{width:innerWidth,height:innerHeight,dpr:devicePixelRatio,visual_scale:visualViewport?visualViewport.scale:1},
    scroll_meta,
    active_path:active?path(active):'',active_edit:!!active && ['Edit','ComboBox'].includes(role(active)) && !sensitive(active)};
})()
