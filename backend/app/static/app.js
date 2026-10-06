'use strict';
const $ = id => document.getElementById(id);
let sessionId = null;
let busy = false;
let activeAnalysisId = null;
let feedbackBusy = false;
let activeResult = null;
function element(tag, text, className) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = String(text);
  if (className) e.className = className;
  return e;
}
function number(value) { if(value===null || value===undefined)return '—'; return Number(value).toLocaleString('zh-CN', {maximumFractionDigits: 2}); }
let pendingAPICalls=0;
async function api(url, options) {
  pendingAPICalls++;if($('analysis-version'))$('analysis-version').disabled=true;
  try {
  const version=$('analysis-version')?.value||'baseline-v1';
  const response = await fetch(url, {...options,headers:{...options?.headers,'X-Dataset-Version':version}});
  const data = await response.json();
  if (!response.ok) {
    const message = data.error?.message || (Array.isArray(data.detail) ? data.detail.map(x=>x.msg).join('；') : data.detail) || '请求失败';
    if(data.run_id){try{const record=await fetch('/api/runs/'+data.run_id);if(record.ok)renderRun(await record.json());}catch{}}
    throw new Error(message);
  }
  return data;
  } finally {pendingAPICalls--;if($('analysis-version'))$('analysis-version').disabled=pendingAPICalls>0;}
}
function status(text, error=false) { $('status').textContent = text; $('status').className = error ? 'error' : ''; }
function chartValue(value, unit) { return value===null ? '—' : unit==='ratio' ? (Number(value)*100).toFixed(2)+'%' : number(value); }
function trendChart(chart, section) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 760 260'); svg.setAttribute('class', 'trend');
  svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', chart.title + '，具体值见下方数据表');
  const pts = chart.points; const max = Math.max(...pts.map(p=>Number(p.value)), 1);
  // Position by calendar month, and break lines across missing months.
  const monthIndex = p => Number(String(p.label).slice(0,4))*12 + Number(String(p.label).slice(5,7));
  const first = monthIndex(pts[0]), last = monthIndex(pts.at(-1));
  const x = p => 68 + (monthIndex(p)-first) / Math.max(last-first,1)*650;
  const y = p => 212 - Number(p.value)/max*172;
  const add = (name, attrs, label) => { const e=document.createElementNS(ns,name); Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,v)); if(label!==undefined)e.textContent=label; svg.append(e); return e; };
  [0,.5,1].forEach(r=>{add('line',{x1:68,x2:718,y1:212-r*172,y2:212-r*172,stroke:'#e8edef'});add('text',{x:60,y:216-r*172,'text-anchor':'end'},number(max*r));});
  pts.forEach((p,i)=>{if(i && monthIndex(p)-monthIndex(pts[i-1])===1)add('line',{x1:x(pts[i-1]),y1:y(pts[i-1]),x2:x(p),y2:y(p),stroke:'#ee7f91','stroke-width':2}); const dot=add('circle',{cx:x(p),cy:y(p),r:4,fill:'#ee7f91'}); const title=document.createElementNS(ns,'title');title.textContent=p.label+': '+p.value;dot.append(title);if(i===0||i===pts.length-1||pts.length<=6||i%3===0)add('text',{x:x(p),y:240,'text-anchor':'middle'},String(p.label).slice(0,7));});
  section.append(svg);
}
function renderChart(chart) {
  const section=element('section',undefined,'chart'); section.append(element('h3',chart.title+' · '+(chart.unit==='ratio'?'百分比':chart.unit)));
  if(!chart.points.length){section.append(element('p','没有可绘制的有效数值，原因见数据表。','chart-caption'));}
  else if(chart.type==='line' && chart.points.length){trendChart(chart,section);}
  else {
    const shown=chart.points.slice(0,20); const max=Math.max(...shown.map(p=>Math.abs(Number(p.value))),1);
    shown.forEach(p=>{const row=element('div',undefined,'bar-row');const label=element('span',p.label,'bar-label');label.title=String(p.label);const track=element('div',undefined,'bar-track');const bar=element('div',undefined,'bar'+(Number(p.value)<0?' negative':''));bar.style.width=(Math.abs(Number(p.value))/max*100)+'%';track.append(bar);row.append(label,track,element('span',chartValue(p.value,chart.unit),'bar-value'));section.append(row);});
    if(chart.points.length>20)section.append(element('p','图表只展示前 20 组，更多结果见数据表。','chart-caption'));
  }
  return section;
}
function render(result) {
  const version=result.dataset_source?.version_id||'baseline-v1';
  if(Array.from($('analysis-version').options).some(o=>o.value===version)){const changed=$('analysis-version').value!==version;$('analysis-version').value=version;if(changed)loadDashboard();}
  activeAnalysisId=result.id;activeResult=result;
  renderDrill(result);
  renderRun(result.execution||null);
  renderFeedback(result.feedback || null);
  sessionId=result.session_id; $('session-label').textContent='会话已保存 · 可以继续追问';
  $('result').hidden=false; $('answer').textContent=result.answer;
  const i=result.intent;
  const range=p=>`${p.start_date} 至 ${p.end_date}`;
  const filters=Object.entries(i.filters).filter(([,v])=>v!==null).map(([k,v])=>`${{country:'国家',customer_code:'客户',product_code:'商品'}[k]}：${v}`).join('，') || '全部数据';
  $('intent').textContent=(i.previous_period ? `${range(i.previous_period)} → ` : '')+`${range(i.period)}（结束日不含） · ${filters} · 数据版本 ${version}`;
  appendMetricEvidence($('intent'),result.metric_definitions);
  appendLineage($('intent'),'analysis',result.id);
  $('export-json').href=`/api/analyses/${result.id}/export/json`; $('export-md').href=`/api/analyses/${result.id}/export/md`;
  $('summary').replaceChildren();
  if(result.comparison){[['基期',result.comparison.previous],['当前',result.comparison.current],['变化金额 / 数量',result.comparison.delta]].forEach(([label,value])=>{const box=element('div',undefined,'stat');box.append(element('small',label),element('strong',number(value)));$('summary').append(box);});}
  $('warnings').replaceChildren(...result.warnings.map(w=>element('li',w.replaceAll('incomplete_period','数据覆盖不完整').replaceAll('missing_month','缺少匹配月份').replaceAll('zero_baseline','去年同月为0'))));
  $('charts').replaceChildren(...result.charts.map(renderChart));
  const table=element('table'); const head=element('tr'); head.append(element('th','期间 / 分组'),element('th','数值'));if(result.product_structure)head.append(element('th','销售额占比'));table.append(head);
  (result.data||[]).forEach(row=>{const tr=element('tr');tr.append(element('td',row.dimension),element('td',result.yoy ? chartValue(row.value,'ratio') : row.value===null ? '—' : row.value));if(result.product_structure)tr.append(element('td',chartValue(row.sales_share,'ratio')));table.append(tr);});$('table').replaceChildren(table);
  if(result.yoy) {
    const t=element('table'),h=element('tr');
    ['本月','销售额 GBP','去年同月','同期销售额 GBP','同比','状态'].forEach(label=>h.append(element('th',label)));t.append(h);
    const reasons={incomplete_period:'数据覆盖不完整',missing_month:'缺少匹配月份',zero_baseline:'去年同月为0'};
    result.yoy.rows.forEach(row=>{const tr=element('tr');[row.month,number(row.sales_amount),row.baseline_month,number(row.previous_year_sales_amount),chartValue(row.sales_yoy,'ratio'),reasons[row.null_reason]||'可计算'].forEach(value=>tr.append(element('td',value)));t.append(tr);});
    $('table').replaceChildren(t);
  }
  (result.attributions||[]).forEach(a=>{
    const detail=element('details');detail.append(element('summary',`${{customer:'客户',product:'商品',country:'国家',customer_product:'客户×商品'}[a.dimension]}贡献 · ${a.group_count} 组完整对账`));
    const t=element('table'),h=element('tr'); ['对象','基期','当前','变化','状态','下钻'].forEach(label=>h.append(element('th',label)));t.append(h);
    [...a.negative,...a.positive].forEach(r=>{const tr=element('tr');[r.dimension,r.previous,r.current,r.delta,{new:'新增',lost:'消失',continuing:'持续'}[r.status]||r.status].forEach(v=>tr.append(element('td',v)));const td=element('td');if(r.drill_filters){const b=element('button','只看此对象','quiet-button');b.type='button';b.addEventListener('click',()=>runDrill({filters:{...activeResult.intent.filters,...r.drill_filters}}));td.append(b);}tr.append(td);t.append(tr);});
    detail.append(t,element('p',`表中展示负向与正向各前 10 名，其余对象变化合计 ${a.other_delta} GBP；全部对象变化合计 ${a.delta_sum} GBP。`,'chart-caption'));$('table').append(detail);
  });
  $('trace').textContent=JSON.stringify({steps:result.trace,attributions:result.attributions},null,2);
  const usage=result.model_usage;
  $('usage').textContent=usage?.strategy==='model' ? `本次解析 ${usage.total_tokens || 0} tokens` : usage?.strategy==='cache' ? '解析缓存 · 无新增模型调用' : usage?.strategy==='local_followup' ? '本地追问 · 无新增模型调用' : usage?.strategy==='structured' ? '明确参数 · 无模型调用' : '';
  status('分析完成，结果已保存。');
}
async function loadHistory(){try{const items=await api('/api/analyses');$('history').replaceChildren(...items.map(item=>{const b=element('button',item.question+' · '+new Date(item.created_at).toLocaleString('zh-CN')+' · '+item.id.slice(0,8));b.type='button';b.addEventListener('click',async()=>{if(busy || feedbackBusy)return;try{const saved=await api('/api/analyses/'+item.id);render(saved);$('question').value=saved.question;$('history-dialog').close();}catch(e){status(e.message,true);}});return b;}));}catch(e){status(e.message,true);}}
$('question-form').addEventListener('submit',async event=>{event.preventDefault();if(busy || feedbackBusy)return;busy=true;$('submit').disabled=true;$('new-session').disabled=true;$('result').hidden=true;status('正在解析问题、查询数据库并核对结果…');try{const result=await api('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:$('question').value,session_id:sessionId})});if(result.status==='needs_clarification'){status(result.message);renderRun(result.execution||null);}else{render(result);await loadHistory();}}catch(e){status(e.message,true);}finally{busy=false;$('submit').disabled=false;$('new-session').disabled=false;}});
document.querySelectorAll('[data-question]').forEach(b=>b.addEventListener('click',()=>{$('question').value=b.dataset.question;$('question').focus();}));
$('new-session').addEventListener('click',()=>{if(feedbackBusy)return;activeAnalysisId=null;activeResult=null;sessionId=null;renderRun(null);$('question').value='';$('result').hidden=true;$('session-label').textContent='新会话 · 明确年份，结果更准确';status('');$('question').focus();});
$('uk-followup').addEventListener('click',()=>{if(busy)return;$('question').value='再看英国';$('question-form').requestSubmit();});
api('/health').then(data=>{$('mode').textContent=data.mode==='demo'?'规则演示 · 未接入 LLM':data.mode==='deepseek'?'DeepSeek Flash · 省钱模式':'模型解析 · 受控查询';}).catch(e=>status(e.message,true));
loadHistory();

$('open-history').addEventListener('click',()=>{$('history-dialog').showModal();});
$('close-history').addEventListener('click',()=>{$('history-dialog').close();});

function renderFeedback(feedback) {
  $('feedback-form').reset();
  if (feedback) {
    document.querySelectorAll('input[name="rating"]').forEach(input=>{input.checked=input.value===feedback.rating;});
    $('feedback-reason').value=feedback.reason;
    $('feedback-correction').value=feedback.correction;
  }
  $('feedback-status').textContent=feedback ? `已保存 · 第 ${feedback.revision} 次修订 · 可修改后重新保存` : '尚未提交反馈';
  $('feedback-status').className='';
  $('feedback-submit').textContent=feedback ? '更新反馈' : '保存反馈';
}
$('feedback-form').addEventListener('submit',async event=>{
  event.preventDefault();
  if(feedbackBusy || busy || !activeAnalysisId)return;
  const id=activeAnalysisId;
  const rating=document.querySelector('input[name="rating"]:checked')?.value;
  if(!rating)return;
  feedbackBusy=true;
  const controls=[...$('feedback-form').elements];
  controls.forEach(control=>{control.disabled=true;});
  $('submit').disabled=true;$('new-session').disabled=true;
  $('feedback-status').textContent='正在保存反馈…';
  try {
    const result=await api(`/api/analyses/${id}/feedback`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({rating,reason:$('feedback-reason').value,correction:$('feedback-correction').value})});
    if(activeAnalysisId===id)renderFeedback(result.feedback);
  } catch(error) {
    if(activeAnalysisId===id){$('feedback-status').textContent=`保存失败：${error.message}，填写内容已保留，请重试。`;$('feedback-status').className='error';}
  } finally {
    feedbackBusy=false;controls.forEach(control=>{control.disabled=false;});
    $('submit').disabled=false;$('new-session').disabled=false;
  }
});

let monitorReport=null;
let monitorBusy=false;
function plainTable(headers, rows) {
  const table=element('table'),head=element('tr');
  headers.forEach(value=>head.append(element('th',value)));table.append(head);
  rows.forEach(values=>{const tr=element('tr');values.forEach(value=>tr.append(element('td',value)));table.append(tr);});
  return table;
}
$('open-monitor').addEventListener('click',()=>{$('monitor-dialog').showModal();});
$('close-monitor').addEventListener('click',()=>{$('monitor-dialog').close();});
$('monitor-form').addEventListener('submit',async event=>{
  event.preventDefault();if(monitorBusy)return;monitorBusy=true;
  $('monitor-submit').disabled=true;$('monitor-result').hidden=true;
  $('monitor-status').className='';$('monitor-status').textContent='正在读取数据、核查质量并计算变化…';
  const period={start_date:$('monitor-start').value,end_date:$('monitor-end').value};
  const options=body=>({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  try {
    const report=await api('/api/monitor',options({...period,mom_drop_threshold:Number($('monitor-drop').value)/100,moving_average_threshold:Number($('monitor-deviation').value)/100,moving_average_window:Number($('monitor-window').value)}));
    const {quality,anomalies}=report;
    monitorReport=report;
    $('monitor-export').href=`/api/monitor/${report.id}/export`;
    $('monitor-result').hidden=false;
    $('monitor-summary').textContent=`当前检查：${quality.current.blocking?'发现质量问题，已暂停异常判断':'已定义规则通过'} · 检测 ${anomalies.summary.months} 个月 · ${anomalies.summary.triggered_months} 个月触发提醒 · ${anomalies.summary.unavailable_checks} 项不可判断`;
    const history=quality.historical_cleaning,counts=history.counts;
    $('cleaning-summary').textContent=counts ? `历史清洗：原始 ${number(counts.raw_rows)} 行，保留 ${number(counts.clean_rows)} 行，排除 ${number(counts.invalid_rows)} 行，其中取消 ${number(counts.cancelled_rows)} 行。${history.status==='summary_matches'?'当前汇总匹配。':'当前汇总不匹配，请核查导入版本。'}取消记录不能与排除数相加。` : history.note;
    $('monitor-warnings').replaceChildren(...[...quality.warnings,...anomalies.warnings].map(w=>element('li',w)));
    const ruleNames={null_required:'明细必要字段为空',invalid_numeric:'无效数量或金额',null_confirmed_date:'有效订单缺确认日期',invalid_date:'确认日期异常',duplicate_order_lines:'重复订单行',duplicate_order_numbers:'重复订单编号',orphan_detail_context:'关联或订单上下文不一致',blank_dimension_codes:'维度编码为空'};
    $('quality-rules').replaceChildren(plainTable(['规则','问题数量','结果'],quality.current.rules.map(r=>[ruleNames[r.rule]||r.rule,r.count,r.status==='passed'?'通过':'需核查'])));
    $('quality-months').replaceChildren(plainTable(['月份','覆盖边界','匹配记录','交易天数','检测条件','缺失率'],quality.months.map(r=>[r.month,r.coverage_status==='within_bounds'?'在完整月边界内':'部分或超出范围',r.has_records?'有记录':'无记录',r.observation_days,r.eligible_for_detection?'可进入检测':'不可进入检测','未知'])));
    const reasons={quality_failed:'质量检查失败',incomplete_period:'当前月份覆盖不足',missing_month:'当前月份无记录',incomplete_baseline:'基期覆盖不足',missing_baseline:'基期缺月',zero_baseline:'基期为0',insufficient_history:'历史完整月份不足'};
    const statuses={triggered:'触发提醒',normal:'未触发',unavailable:'不可判断',blocked:'质量阻断'};
    $('anomaly-table').replaceChildren(plainTable(['月份','方法','当前销售额','基线','变化','阈值','状态 / 原因'],anomalies.rows.flatMap(row=>row.checks.map(check=>[row.month,check.method==='mom_drop'?'环比下降':'移动平均偏离',number(check.observed_value),number(check.baseline),chartValue(check.change_ratio,'ratio'),chartValue(check.threshold,'ratio'),statuses[check.status]+(check.reason?' · '+reasons[check.reason]:'')]))));
    $('monitor-version').textContent=`检测数据版本：${anomalies.data_version} · 检查时间：${new Date(anomalies.checked_at).toLocaleString('zh-CN')}`;
    $('monitor-status').textContent='检查完成。可展开覆盖和质量规则，下载 JSON 查看全部基线月份与查询依据。';
  } catch(error) { $('monitor-status').textContent=error.message;$('monitor-status').className='error'; }
  finally {monitorBusy=false;$('monitor-submit').disabled=false;}
});

let dashboardBusy=false;
async function dashboardAnalysis(intent) {
  if(busy || feedbackBusy)return;
  busy=true;$('submit').disabled=true;$('new-session').disabled=true;
  status('正在比较选定月份与上月，核对客户、商品和国家贡献…');
  try {
    const result=await api('/api/analyze/structured',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(intent)});
    render(result);$('question').value=`${intent.period.start_date.slice(0,7)}销售额相对上月变化（总览提醒）`;
    $('result').scrollIntoView({behavior:'smooth',block:'start'});await loadHistory();
  }catch(e){status(e.message,true);$('status').scrollIntoView({block:'center'});}
  finally{busy=false;$('submit').disabled=false;$('new-session').disabled=false;}
}
async function loadDashboard(month='') {
  if(dashboardBusy)return;dashboardBusy=true;$('dashboard-refresh').disabled=true;
  $('dashboard-content').hidden=true;$('dashboard-status').className='';$('dashboard-status').textContent='正在读取同一数据库快照…';
  try {
    const data=await api('/api/dashboard'+(month?'?month='+encodeURIComponent(month+'-01'):''));
    $('dashboard-month').value=data.month.slice(0,7);
    $('dashboard-cards').replaceChildren(...data.cards.map(card=>{
      const box=element('div',undefined,'stat');
      box.append(element('small',card.name+' · '+card.unit),element('strong',number(card.value)),
        element('span',card.change_ratio===null?'环比不可判断：'+({incomplete_baseline:'上月覆盖不足',missing_data:'无匹配数据',zero_baseline:'上月为零'}[card.null_reason]||'数据不足'):'较上月 '+chartValue(card.change_ratio,'ratio'),'card-change'));
      return box;
    }));
    const trendData=element('details',undefined,'data-panel');trendData.append(element('summary','查看月度销售额数据'),plainTable(['月份','销售额 GBP'],data.trend.map(r=>[r.dimension,number(r.value)])));
    $('dashboard-trend').replaceChildren(renderChart({type:'line',title:'最近12个月销售额',unit:'GBP',points:data.trend.filter(r=>r.value!==null).map(r=>({label:r.dimension,value:r.value}))}),
      trendData);
    $('dashboard-rankings').replaceChildren(...['customer','product'].map(dim=>renderChart({type:'bar',title:dim==='customer'?'当月 Top 10 客户':'当月 Top 10 商品',unit:'GBP',points:data.rankings[dim].map(r=>({label:r.dimension,value:r.value}))})));
    $('dashboard-alerts').replaceChildren(...data.alerts.map(alert=>{
      const button=element('button',undefined,'dashboard-alert');button.type='button';
      button.append(element('strong',alert.month.slice(0,7)),element('span',alert.checks.map(c=>(c.method==='mom_drop'?'环比下降':'偏离前三月均值')+' '+chartValue(c.change_ratio,'ratio')).join(' / ')),element('span','比较上月与贡献 →'));
      button.addEventListener('click',()=>dashboardAnalysis(alert.intent));return button;
    }));
    if(!data.alerts.length)$('dashboard-alerts').append(element('p','此期间未触发配置阈值的提醒。数据不足的月份不参与判断。'));
    $('dashboard-warnings').replaceChildren(...data.warnings.map(w=>element('li',w)));
    $('dashboard-evidence').textContent=JSON.stringify(data,null,2);$('dashboard-content').hidden=false;
    $('dashboard-status').textContent=`${data.month.slice(0,7)}完整月 · 与 ${data.previous_month.slice(0,7)} 比较 · ${data.quality.rules.length}项质量规则通过 · 无模型调用`;
  }catch(e){$('dashboard-status').textContent=e.message;$('dashboard-status').className='error';}
  finally{dashboardBusy=false;$('dashboard-refresh').disabled=false;}
}
$('dashboard-form').addEventListener('submit',event=>{event.preventDefault();loadDashboard($('dashboard-month').value);});
loadDashboard();

function renderDrill(result) {
  const i=result.intent;
  $('drill-country').value=i.filters.country||'';$('drill-customer').value=i.filters.customer_code||'';$('drill-product').value=i.filters.product_code||'';
  $('drill-start').value=i.period.start_date;$('drill-end').value=i.period.end_date;
  $('drill-before-start').value=i.previous_period?.start_date||'';$('drill-before-end').value=i.previous_period?.end_date||'';
  $('drill-before-start-label').hidden=!i.previous_period;$('drill-before-end-label').hidden=!i.previous_period;
  $('drill-before-start').required=!!i.previous_period;$('drill-before-end').required=!!i.previous_period;
  $('drill-strategy').value=i.strategy||'auto';
  $('drill-metric').value=i.metric_code;$('drill-dimension').value=i.group_by;$('drill-dimension').disabled=i.action!=='trend';
  $('drill-pairs').hidden=i.action!=='compare'||i.metric_code!=='sales_amount';
  $('drill-parent').hidden=!result.parent_analysis_id;
}
async function runDrill(change) {
  if(busy||feedbackBusy||!activeAnalysisId)return;
  const id=activeAnalysisId;busy=true;$('submit').disabled=true;$('new-session').disabled=true;
  status('正在按明确条件下钻，查询并核对新结果…');
  try{const result=await api(`/api/analyses/${id}/drill`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(change)});render(result);await loadHistory();}
  catch(e){status(e.message,true);}
  finally{busy=false;$('submit').disabled=false;$('new-session').disabled=false;}
}
$('drill-form').addEventListener('submit',event=>{event.preventDefault();runDrill({strategy:$('drill-strategy').value,period:{start_date:$('drill-start').value,end_date:$('drill-end').value},...(activeResult.intent.previous_period?{previous_period:{start_date:$('drill-before-start').value,end_date:$('drill-before-end').value}}:{}),filters:{country:$('drill-country').value.trim()||null,customer_code:$('drill-customer').value.trim()||null,product_code:$('drill-product').value.trim()||null},metric_code:$('drill-metric').value,group_by:$('drill-dimension').value});});
$('drill-pairs').addEventListener('click',()=>runDrill({contribution_dimensions:['customer','product','country','customer_product']}));
$('drill-reset').addEventListener('click',()=>runDrill({filters:{}}));
$('drill-parent').addEventListener('click',async()=>{if(busy||feedbackBusy||!activeResult?.parent_analysis_id)return;try{render(await api('/api/analyses/'+activeResult.parent_analysis_id));}catch(e){status(e.message,true);}});

$('drill-metric').addEventListener('change',()=>{if($('drill-metric').value!=='sales_amount')$('drill-strategy').value='auto';});
function renderRun(run) {
  $('run-panel').hidden=!run;if(!run)return;
  const labels={sales_decline:'销售变化',customer_contribution:'客户贡献',product_mix:'产品结构',basic_query:'基础查询'};
  $('run-summary').textContent=`策略：${labels[run.strategy]||'尚未选定'} · 状态：${{completed:'完成',failed:'失败',running:'执行中',needs_clarification:'待澄清'}[run.status]||run.status} · 步骤 ${run.steps_used}/${run.limits.steps} · 耗时 ${run.duration_ms||0} ms · 模型 ${run.model_calls}/${run.limits.model_calls} 次 · ${run.tokens_status==='unknown_until_response'?'用量未知':run.tokens+'/'+run.limits.tokens+' tokens'} · 重试 ${run.retries} 次`;
  $('run-steps').replaceChildren(plainTable(['序号','工具','状态','耗时 ms','错误'],run.steps.map(s=>[s.number,s.tool,s.status,s.duration_ms,s.error||'—'])));
  $('run-error').textContent=run.error?run.error.code+'：'+run.error.message:'运行ID：'+run.id;
  if(run.status==='failed')$('run-panel').open=true;
}

let reportBusy=false;
function renderReport(report) {
  $('report-content').hidden=false;$('report-title').textContent=report.title;$('report-month').value=report.month.slice(0,7);
  $('report-meta').textContent=`保存于 ${new Date(report.created_at).toLocaleString('zh-CN')} · 快照 ${report.snapshot_sha256} · 无模型调用`;
  $('report-md').href=`/api/reports/${report.id}/export/md`;$('report-json').href=`/api/reports/${report.id}/export/json`;
  appendMetricEvidence($('report-meta'),report.snapshot.metric_definitions);
  appendLineage($('report-meta'),'report',report.id);
  renderReportPreview(report.markdown);
}
async function loadReports() {
  const items=await api('/api/reports');
  $('report-list').replaceChildren(...items.map(item=>{const button=element('button',`${item.title} · ${new Date(item.created_at).toLocaleString('zh-CN')} · ${item.id.slice(0,8)}`,'quiet-button');button.type='button';button.addEventListener('click',async()=>{if(reportBusy)return;try{renderReport(await api('/api/reports/'+item.id));$('report-status').textContent='已从保存的快照重开，没有重新查询数据库。';}catch(e){$('report-status').textContent=e.message;}});return button;}));
  if(!items.length)$('report-list').append(element('p','尚无经营报告。'));
}
$('open-reports').addEventListener('click',async()=>{
  $('reports-dialog').showModal();$('report-month').value=activeResult?.intent?.period?.start_date?.slice(0,7)||$('dashboard-month').value||'2011-11';
  try{await loadReports();}catch(e){$('report-status').textContent=e.message;}
});
$('close-reports').addEventListener('click',()=>$('reports-dialog').close());
$('report-form').addEventListener('submit',async event=>{
  event.preventDefault();if(reportBusy)return;reportBusy=true;$('report-create').disabled=true;
  $('report-status').className='';$('report-status').textContent='正在读取同一快照、核对贡献并生成报告…';$('report-content').hidden=true;
  try{const report=await api('/api/reports',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({month:$('report-month').value+'-01'})});renderReport(report);await loadReports();$('report-status').textContent='报告已保存。下载文件包含完整查询证据。';}
  catch(e){$('report-status').textContent=e.message;$('report-status').className='error';}
  finally{reportBusy=false;$('report-create').disabled=false;}
});
function reportText(text) {
  return text.replace(/\\([\\|\[\]*_`])/g,'$1').replace(/\*\*/g,'').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
}
function renderReportPreview(markdown) {
  const body=$('report-body');body.replaceChildren();
  const content=markdown.split('## 附件：')[0];
  const lines=content.slice(content.indexOf('## 1.')).split('\n');
  let table=null;
  for(const line of lines) {
    if(!line.trim()){table=null;continue;}
    if(line.startsWith('## ')){body.append(element('h3',reportText(line.slice(3))));table=null;}
    else if(line.startsWith('| ')){
      if(/^\|\s*---/.test(line))continue;
      const header=table===null;
      if(header){table=element('table');const wrapper=element('div',undefined,'table-wrap');wrapper.append(table);body.append(wrapper);}
      const row=element('tr');line.slice(1,-1).split(/(?<!\\)\|/).forEach(cell=>row.append(element(header?'th':'td',reportText(cell.trim()))));table.append(row);
    }else{table=null;body.append(element('p',reportText(line)));}
  }
}

// Catalog links prefill a supported question; they never spend tokens on navigation.
const catalogMetric = new URLSearchParams(location.search).get('metric');
const catalogQuestions = {
  sales_amount:'2011年销售额趋势', order_count:'2011年订单数趋势',
  customer_count:'2011年客户数趋势', sales_quantity:'2011年销量趋势',
  average_order_value:'2011年客单价趋势', average_selling_price:'2011年加权平均售价趋势',
  sales_mom:'2011年2月销售额比1月变化', sales_yoy:'2011年销售额同比趋势'
};
if(catalogQuestions[catalogMetric]){$('question').value=catalogQuestions[catalogMetric];status('来自数据目录的指标问题已填入，请确认期间后开始分析。');}

fetch('/api/data/versions').then(r=>r.json()).then(data=>{const select=$('analysis-version');select.replaceChildren();data.versions.forEach(version=>{const option=element('option',version.label);option.value=version.id;select.append(option);});}).catch(e=>status('数据版本读取失败，请检查存储。',true));
$('analysis-version').addEventListener('change',()=>{$('new-session').click();loadDashboard();status('数据版本已切换，新分析使用选中版本；旧历史保持原快照。');});

function appendMetricEvidence(container,definitions) {
  if(!definitions){container.append(element('span',' · 历史口径未登记'));return;}
  for(const [code,item] of Object.entries(definitions)){const link=element('a',` · ${item.name} v${item.version}`);link.href=`/data/metrics?code=${encodeURIComponent(code)}#${encodeURIComponent(code)}-v${item.version}`;link.target='_blank';link.rel='noopener';container.append(link);}
}

function appendLineage(container,kind,id){const link=element('a',' · 追溯数据来源 ↗');link.href='/data/lineage?node='+encodeURIComponent(kind+':'+id)+'&direction=upstream';link.target='_blank';link.rel='noopener';container.append(link);}
