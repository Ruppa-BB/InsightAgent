'use strict';
const $ = id => document.getElementById(id);
const eventLabels={observation:'检查观察',recheck_started:'开始复检',recheck_error:'复检失败',recheck_passed:'复检通过',recheck_failed:'复检未通过',start:'开始处理',note:'处理说明',waive:'人工豁免',close:'有证据关闭',reopen:'重新打开',cancel_recheck:'取消复检'};
const labels = {open:'待处理',in_progress:'处理中',resolved:'复检通过并关闭',waived:'人工豁免'};
let records = [], selected;
function element(tag, text) { const node=document.createElement(tag); node.textContent=text; return node; }
async function request(url, body) {
  const response=await fetch(url, body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {});
  const data=await response.json();
  if (!response.ok) throw new Error(data.error?.message || data.detail || '请求失败，请刷新后重试。');
  return data;
}
function list() {
  $('list').replaceChildren();
  const visible=records.filter(item=>!$('filter').value || item.status===$('filter').value);
  if (!visible.length) $('list').append(element('p','当前没有符合条件的问题单。已通过的检查不会创建问题。'));
  visible.forEach(item=> {const button=element('button',`${item.name} · ${labels[item.status]} · ${item.latest_count} 条`);button.className='catalog-asset';button.onclick=()=>open(item.id);$('list').append(button);});
}
async function load() { records=(await request('/api/data/issues')).issues; list(); }
async function open(id) {
  selected=await request(`/api/data/issues/${id}`);
  const box=$('detail'); box.replaceChildren(element('h2',selected.name),element('p',`${labels[selected.status]} · 修订 ${selected.revision} · 已观察 ${selected.observations} 次`),element('p',`原数据版本：${selected.version_id}`),element('p',`最近违规数量：${selected.latest_count}`));
  const evidence=element('a','查看最新检查证据 ↗');evidence.href=`/data/quality?check=${encodeURIComponent(selected.latest_check_id)}`;box.append(evidence);
  const note=element('textarea','');note.id='issue-note';note.placeholder='填写处理说明、修复方式或接受风险的理由';note.maxLength=2000;note.setAttribute('aria-label','处理说明');box.append(note);
  const active=['open','in_progress'].includes(selected.status);
  const actions=selected.pending ? [['cancel_recheck','取消未完成复检']] : active ? [['start','开始处理'],['note','保存处理说明'],['waive','登记人工豁免'],['close','复检通过后关闭']] : [['reopen','重新打开']];
  actions.forEach(([action,label])=> {const button=element('button',label);button.className='outline-button';button.disabled=action==='close'&&!selected.candidate?.passed;button.onclick=()=>mutate('actions',{action,note:note.value});box.append(button);});
  if(active&&!selected.pending){const button=element('button','重新检查原数据版本 ↗');button.className='primary-button';button.onclick=()=>mutate('recheck',{});box.append(button);}
  if(selected.status==='resolved')box.append(element('p','已依据原版本复检通过的证据关闭。'));
  if(active&&selected.candidate)box.append(element('p',selected.candidate.passed?'最新复检通过，可填写说明后关闭。':'复检未通过，不能关闭问题。'));
  if(selected.pending)box.append(element('p','复检进行中。服务中断后可登记取消，再重新检查。'));
  box.append(element('h3','处理与观察记录'));
  selected.events.forEach(event=>box.append(element('p',`${event.created_at} · ${eventLabels[event.kind] || event.kind}${event.count!==undefined ? ` · 违规 ${event.count} 条` : ''}${event.note ? ` · ${event.note}` : ''}${event.check_id ? ` · 检查 ${event.check_id}` : ''}`)));
}
async function mutate(path,body){
 const id=selected.id, revision=selected.revision;
 $('status').textContent=path==='recheck'?'正在复检，完成前不会标记为修复。':'正在保存…';
 $('detail').querySelectorAll('button').forEach(button=>button.disabled=true);
 try{await request(`/api/data/issues/${id}/${path}`,{expected_revision:revision,...body});await load();await open(id);$('status').textContent='记录已保存。';}
 catch(error){$('status').textContent=error.message;await open(id).catch(()=>{});}
}
$('filter').onchange=list;
$('refresh').onclick=()=>load().catch(error=>$('status').textContent=error.message);
load().then(()=>{const id=new URLSearchParams(location.search).get('issue');if(id)return open(id);}).catch(error=>$('status').textContent=error.message);
