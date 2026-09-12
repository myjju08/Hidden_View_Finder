/* Local, coordinate-based exploration. Source strings enter the DOM as text or
 * escaped text only; this application never inserts provider-authored HTML. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const arr = (value) => Array.isArray(value) ? value : [];
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num = (value, digits = 0) => Number.isFinite(Number(value)) && value !== null ? Number(value).toLocaleString('ko-KR', {maximumFractionDigits:digits}) : '미확인';
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  const clamp = (value, lo, hi) => Math.max(lo, Math.min(hi, value));
  const labels = {mountain:'산과 능선', ridge:'산과 능선', city:'도시', building:'건물', river:'강', water:'수면', greenery:'녹음', green_space:'녹음', skyline:'스카이라인', visible:'보임', blocked:'가림', unknown:'미확인', excluded:'제외', partial:'일부 표본만 지원 · 전체 미확인', sampled_visible:'표본 보임 · 대상 전체 미확인', sampled_blocked:'표본 가림 · 대상 전체 미확인', supported:'지원', open:'열린 구도', framed:'사이로 보이는 구도', any:'다양한 구도', map_supported:'지도상 접근 근거', map_supported_public:'지도상 접근 근거', public_outdoor:'지도상 접근 근거', exploratory_only:'접근 미확인', quiet:'한적함 선호', unavailable:'사용 불가', disabled:'꺼짐', no_key:'키 없음', not_configured:'설정 없음', local_only:'로컬 전용', review_required:'배포 검토 필요', incomplete:'불완전', storage_blocked:'저장 공간 제한', queued:'대기 중', running:'생성 중', pending:'대기 중', completed:'완료', ready:'완료', succeeded:'완료', failed:'생성 실패', budget_exhausted:'사용 예산 소진', rate_limited:'요청 한도 도달'};
  const label = (value) => labels[value] || String(value ?? '미확인');
  const state = {origin:{lon:126.9780,lat:37.5665}, originName:'서울 중심 좌표', views:[], mapViews:[], capabilities:null, map:null, basemap:null, coverage:null, viewsLayer:null, originLayer:null, selectedLayer:null, coverageVisible:false, requestSerial:0, mapSerial:0, searchSerial:0, requestController:null, mapController:null, searchController:null, result:null, imageJobs:new Map()};
  let toastTimer;
  function toast(text) { $('toast').textContent=text; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,4500); }
  function humanError(error) {
    const code = error.code || '';
    if (code.includes('storage') || code.includes('budget')) return '저장 공간 또는 사용 예산 한도에 도달했습니다. 기존 지도와 기하 미리보기는 계속 사용할 수 있습니다.';
    if (code.includes('queue') || code.includes('busy') || error.status===429) return '지금 다른 계산을 처리하고 있습니다. 잠시 후 다시 시도해 주세요.';
    if (code.includes('coverage') || code.includes('unsupported')) return '이 시선의 지형·장애물 자료가 충분하지 않습니다. 지원되지 않는 부분은 보임으로 처리하지 않습니다.';
    return error.message || '요청을 완료하지 못했습니다. 잠시 후 다시 시도해 주세요.';
  }
  async function api(path, options={}) {
    const response=await fetch(path,{...options,headers:{'Accept':'application/json',...(options.body?{'Content-Type':'application/json'}:{}),...options.headers},credentials:'same-origin'});
    let data;
    try { data=await response.json(); } catch { throw Object.assign(new Error('서버 응답 형식을 확인할 수 없습니다.'),{status:response.status}); }
    if (!response.ok) {
      const detail=data.error || data.detail || data;
      throw Object.assign(new Error(typeof detail==='string'?detail:detail.message || '요청을 처리하지 못했습니다.'),{code:detail.code || data.code,status:response.status});
    }
    return data;
  }
  function bearing(value) {
    if (!finite(value)) return '방향 미확인';
    const names=['북','북동','동','남동','남','남서','서','북서'];
    return `${names[Math.round(((value%360)+360)%360/45)%8]} ${num(((value%360)+360)%360)}°`;
  }
  function coordinates(value) {
    if (Array.isArray(value) && value.length>=2) return {lon:Number(value[0]),lat:Number(value[1])};
    if (value && typeof value==='object') return {lon:Number(value.lon ?? value.longitude),lat:Number(value.lat ?? value.latitude)};
    return null;
  }
  function validCoord(value) { return value && finite(value.lon) && finite(value.lat) && Math.abs(value.lon)<=180 && Math.abs(value.lat)<=90; }
  function targetGroups(samples) {
    const groups=new Map();
    for(const sample of samples){
      let group=groups.get(sample.target_id);
      if(!group){group={id:sample.target_id,name:sample.name,category:sample.category,location:sample.target,counts:{visible:0,blocked:0,unknown:0,excluded:0,intended:0}};groups.set(sample.target_id,group);}
      group.counts.intended++;if(sample.status in group.counts)group.counts[sample.status]++;else group.counts.unknown++;
    }
    return [...groups.values()].map(group=>{const c=group.counts;return {...group,status:c.excluded===c.intended?'excluded':c.visible?(c.blocked||c.unknown||c.excluded?'partial':'sampled_visible'):c.unknown||c.excluded?'unknown':c.blocked?'sampled_blocked':'unknown'};});
  }
  function sceneView(raw) {
    const orientation=raw.orientation || {};
    const requested=coordinates(raw.standing || raw.requested_position || raw.requested_coordinates || raw.location || raw.position);
    const effective=coordinates(raw.effective_position || raw.effective_coordinates || (raw.standing?.effective_lon!=null?{lon:raw.standing.effective_lon,lat:raw.standing.effective_lat}:null));
    const samples=arr(raw.scene_samples || raw.samples || raw.visibility?.samples || raw.scene?.samples).map(sample=>({...sample,status:sample.state || sample.status,elevation_angle_deg:sample.angular_elevation_deg ?? sample.elevation_angle_deg}));
    const counts=raw.coverage || raw.sample_counts || raw.visibility?.sample_counts || raw.scene?.sample_counts || {};
    return {...raw,id:String(raw.id || raw.view_id),name:raw.name || raw.title || '지도에서 찾은 조망',requested,effective,
      bearing:Number(raw.bearing_deg ?? orientation.bearing_deg ?? raw.direction_deg),fov:Number(raw.fov_deg ?? raw.field_of_view_deg ?? orientation.fov_deg ?? 60),
      targets:targetGroups(samples),samples,counts,sun:raw.solar || raw.sun,
      categories:arr(raw.categories || raw.supported_categories || raw.scene?.categories),
      limitations:arr(raw.limitations || raw.uncertainties),scoreValue:Number(raw.score?.total ?? raw.score?.value ?? raw.score),
      description:raw.description || raw.explanation || raw.presentation?.description || '',snap_displacement_m:raw.standing?.snap_displacement_m ?? raw.snap_displacement_m,district:raw.standing?.district || raw.district};
  }
  function setOrigin(value,name='지도에서 선택한 중심', move=true) {
    if (!validCoord(value)) return toast('올바른 경도와 위도를 입력해 주세요.');
    state.origin=value;state.originName=name;
    $('origin-lon').value=value.lon.toFixed(6);$('origin-lat').value=value.lat.toFixed(6);
    $('origin-label').textContent=`${name} · ${value.lat.toFixed(5)}, ${value.lon.toFixed(5)}`;
    drawOrigin();
    if(move && state.map) state.map.setView([value.lat,value.lon],Math.max(12,state.map.getZoom()));
    if(state.result) $('request-summary').textContent='탐색 중심이 바뀌었습니다. 아래 카드는 이전 요청 결과입니다. 풍경 찾기를 눌러 갱신하세요.';
  }
  function drawOrigin() {
    if (!state.map) return;
    state.originLayer.clearLayers();
    const radius=Number(document.querySelector('input[name=radius]:checked').value);
    L.circle([state.origin.lat,state.origin.lon],{radius,color:'#638f9e',weight:1,dashArray:'4 6',fillColor:'#789fa5',fillOpacity:.035,interactive:false}).addTo(state.originLayer);
    L.circleMarker([state.origin.lat,state.origin.lon],{radius:7,color:'#fffefa',weight:3,fillColor:'#638f9e',fillOpacity:1}).bindTooltip('탐색 중심 · 직선거리 기준').addTo(state.originLayer);
  }
  function mapStyle(feature) {
    const p=feature.properties || {}, kind=p.layer || p.category || p.kind || '';
    if (/water|river/.test(kind)) return {color:'#a2bec1',weight:.5,fillColor:'#bad1cf',fillOpacity:.9};
    if (/green|park|wood|public_space/.test(kind)) return {color:'#b6c6a7',weight:.5,fillColor:'#cddabb',fillOpacity:.75};
    if (/boundar|district|seoul/.test(kind)) return {color:'#a7b498',weight:1.2,dashArray:'4 4',fillOpacity:0};
    if (/building/.test(kind)) return {color:'#c6c9bb',weight:.4,fillColor:'#d5d6c8',fillOpacity:.65};
    return {color:state.map.getZoom()>=14?'#c2c8b6':'#c5ccbc',weight:state.map.getZoom()>=14?1.4:.8,opacity:.9,fillOpacity:0};
  }
  function addFeatures(data,layer) {
    const collection=data.type==='FeatureCollection'?data:data.features?.type==='FeatureCollection'?data.features:{type:'FeatureCollection',features:arr(data.features)};
    L.geoJSON(collection,{style:mapStyle,pointToLayer:(feature,latlng)=>L.circleMarker(latlng,{radius:2,color:'#83976f',weight:.5,fillOpacity:.5}),onEachFeature:(feature,item)=>{
      const name=feature.properties?.name;
      if(name){const node=document.createElement('span');node.textContent=String(name);const district=feature.properties?.layer==='districts';item.bindTooltip(node,district?{permanent:true,direction:'center',className:'map-district-label',opacity:1}:{sticky:true});}
    }}).addTo(layer);
  }
  async function refreshMap() {
    if(!state.map) return;
    const serial=++state.mapSerial;
    state.mapController?.abort();state.mapController=new AbortController();
    const b=state.map.getBounds(),bbox=[Math.max(126.2,b.getWest()),Math.max(37.0,b.getSouth()),Math.min(127.8,b.getEast()),Math.min(38.2,b.getNorth())].map(v=>v.toFixed(6)).join(',');
    $('map-status').textContent='현재 화면의 지리 불러오는 중';
    try {
      const data=await api(`/api/map?bbox=${encodeURIComponent(bbox)}&zoom=${state.map.getZoom()}`,{signal:state.mapController.signal});
      if(serial!==state.mapSerial) return;
      state.basemap.clearLayers();addFeatures(data,state.basemap);
      state.coverage.clearLayers();
      let coverage=data.coverage || data.coverage_features;
      if(state.coverageVisible && !coverage)coverage=await api('/api/coverage',{signal:state.mapController.signal});
      if(serial!==state.mapSerial)return;
      if(state.coverageVisible && coverage) drawCoverage(coverage);
      const n=arr(data.features?.features || data.features).length;
      $('map-status').textContent=`로컬 지리 ${num(n)}개${data.truncated||data.sampled?' · 화면별 일부 표본':''}`;
      if(data.attribution) $('map-caption').textContent='간략화한 실제 지리 · 경로 안내용 지도가 아닙니다';
    } catch(error) {
      if(error.name!=='AbortError' && serial===state.mapSerial) $('map-status').textContent=`지도 ${humanError(error)}`;
    }
  }
  function drawCoverage(raw) {
    const collection=raw.type==='FeatureCollection'?raw:{type:'FeatureCollection',features:arr(raw)};
    L.geoJSON(collection,{interactive:true,style:feature=>{
      const p=feature.properties || {}, supported=p.supported ?? p.terrain_supported ?? p.terrain_fraction ?? p.valid_fraction ?? p.coverage_fraction;
      return {color:supported===false||supported===0?'#ba8b67':'#799963',weight:.8,fillColor:supported===false||supported===0?'#d8b694':'#a9c292',fillOpacity:.18,dashArray:'3 3'};
    },onEachFeature:(feature,item)=>{
      const p=feature.properties || {};const text=document.createElement('span');text.textContent=`${p.name || p.tile_id || '타일 집계 범위'}${p.terrain_fraction!=null?` · 지형 유효 격자 ${num(p.terrain_fraction*100,1)}%`:''} · 개별 시선은 별도 검사합니다.`;item.bindTooltip(text);
    }}).addTo(state.coverage);
  }
  function initMap() {
    if(typeof L==='undefined') {$('map-status').textContent='로컬 지도 라이브러리가 없습니다. 설치 상태를 확인하세요.';return;}
    state.map=L.map('map',{center:[37.5665,126.978],zoom:11,minZoom:9,maxZoom:18,maxBounds:[[37.0,126.2],[38.2,127.8]],maxBoundsViscosity:.75,zoomControl:true,attributionControl:true,preferCanvas:true});
    state.map.attributionControl.setPrefix('Leaflet');
    state.basemap=L.layerGroup().addTo(state.map);state.coverage=L.layerGroup().addTo(state.map);state.viewsLayer=L.layerGroup().addTo(state.map);state.originLayer=L.layerGroup().addTo(state.map);state.selectedLayer=L.layerGroup().addTo(state.map);
    L.control.scale({imperial:false,maxWidth:90,position:'bottomleft'}).addTo(state.map);
    state.map.on('click',event=>{setOrigin({lon:event.latlng.lng,lat:event.latlng.lat},'지도에서 선택한 중심',false);toast('탐색 중심을 바꿨습니다. 풍경 찾기를 눌러 주세요.');});
    let timer;state.map.on('moveend',()=>{clearTimeout(timer);timer=setTimeout(refreshMap,200);});
    setOrigin(state.origin,state.originName,false);refreshMap();
  }
  function localDateInput() {
    const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date());
    const values=Object.fromEntries(parts.map(p=>[p.type,p.value]));return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}`;
  }
  function requestBody() {
    return {origin:{...state.origin},radius_m:Number(document.querySelector('input[name=radius]:checked').value),view_at:`${$('view-at').value}:00+09:00`,preferences:[...document.querySelectorAll('#preferences input:checked')].map(input=>input.value),composition:$('composition').value,crowd_preference:$('crowd').value,limit:3};
  }
  function setLoading(loading) {$('loading-panel').hidden=!loading;$('recommend-button').disabled=loading;$('cancel-button').hidden=!loading;$('planner-form').setAttribute('aria-busy',String(loading));}
  async function recommend(event) {
    event.preventDefault();
    if(!$('planner-form').reportValidity()) return;
    if(!document.querySelector('#preferences input:checked')) return toast('풍경을 하나 이상 선택해 주세요.');
    const serial=++state.requestSerial;
    state.requestController?.abort();state.requestController=new AbortController();
    const body=requestBody();setLoading(true);$('error-box').hidden=true;$('empty-panel').hidden=true;
    try {
      const result=await api('/api/recommendations',{method:'POST',body:JSON.stringify(body),signal:state.requestController.signal});
      if(serial!==state.requestSerial)return;
      state.result=result;state.views=arr(result.views || result.recommendations).map(sceneView).slice(0,3);
      renderResults(result,body);
      if(window.innerWidth<=760) $('results').scrollIntoView({behavior:'smooth',block:'start'});
    }catch(error){
      if(error.name!=='AbortError' && serial===state.requestSerial){$('error-box').textContent=humanError(error);$('error-box').hidden=false;if(!state.views.length)showEmpty('이번 요청을 완료하지 못했어요','지도와 기존 데이터는 그대로 사용할 수 있습니다. 오류 내용을 확인한 뒤 다시 시도해 주세요.');}
    }finally{if(serial===state.requestSerial)setLoading(false);}
  }
  function showEmpty(title,text) {
    $('empty-panel').hidden=false;$('empty-panel').innerHTML=`<div class="empty-mark" aria-hidden="true">⌕</div><h3>${esc(title)}</h3><p>${esc(text)}</p>`;
  }
  function sampleCounts(view) {
    const c=view.counts || {};
    const count=(status)=>Number(c[status] ?? c[`${status}_count`] ?? view.samples.filter(sample=>sample.status===status).length);
    return {visible:count('visible'),blocked:count('blocked'),unknown:count('unknown'),excluded:count('excluded'),total:Number(c.total ?? c.intended ?? view.samples.length ?? 0)};
  }
  function samples(view) {
    return view.samples.length?view.samples:view.targets.flatMap(target=>arr(target.samples).map(sample=>({...sample,name:target.name,category:target.category || target.kind})));
  }
  function schematic(view) {
    const width=680,height=180,left=28,right=652,top=20,bottom=148,fov=clamp(view.fov || 60,10,180),bearingValue=finite(view.bearing)?view.bearing:0;
    const xAt=(azimuth)=>{const delta=((azimuth-bearingValue+540)%360)-180;return left+(delta/fov+.5)*(right-left);};
    const sourceSamples=samples(view).filter(sample=>(sample.bearing_deg ?? sample.azimuth_deg)!=null && (sample.elevation_angle_deg ?? sample.apparent_elevation_deg)!=null && finite(Number(sample.bearing_deg ?? sample.azimuth_deg)) && finite(Number(sample.elevation_angle_deg ?? sample.apparent_elevation_deg)));
    const elevations=sourceSamples.map(sample=>Number(sample.elevation_angle_deg ?? sample.apparent_elevation_deg));
    const minElevation=clamp(Math.floor(Math.min(0,...elevations))-1,-90,0),maxElevation=clamp(Math.ceil(Math.max(4,...elevations))+1,5,90);
    const yAt=(elevation)=>bottom-(clamp(elevation,minElevation,maxElevation)-minElevation)/(maxElevation-minElevation)*(bottom-top);
    const horizon=arr(view.horizon?.samples || view.horizon_samples).filter(sample=>finite(Number(sample.bearing_deg ?? sample.azimuth_deg)));
    let body=`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="계산된 대상 표본의 방향과 고도. 세로 고도 축은 표본에 맞춘 각도 축이며 사진이 아닙니다."><rect width="${width}" height="${height}" fill="#edf1e8"/><path d="M${left} ${bottom}H${right}M${left} ${yAt(0)}H${right}M340 ${top}V${bottom}" stroke="#ccd6c2" stroke-dasharray="3 5"/><text x="340" y="16" text-anchor="middle" fill="#5e7255" font-size="11">${esc(bearing(bearingValue))} · 시야각 ${num(fov)}°</text><text x="4" y="31" fill="#7c8b71" font-size="10">${num(maxElevation)}°</text><text x="4" y="${bottom}" fill="#7c8b71" font-size="10">${num(minElevation)}°</text>`;
    for(const sample of samples(view).filter(sample=>sample.status==='unknown').slice(0,80)){const az=Number(sample.bearing_deg ?? sample.azimuth_deg);if(!finite(az))continue;const x=xAt(az);if(x>=left&&x<=right)body+=`<path d="M${x} ${top+10}V${bottom}" stroke="#baa77c" stroke-width="2" opacity=".45" stroke-dasharray="3 5"><title>미확인 표본 방향 · 고도/시선 근거 부족</title></path>`;}
    for(const h of horizon.slice(0,120)){const x=xAt(Number(h.bearing_deg ?? h.azimuth_deg));if(x<left||x>right)continue;const e=Number(h.elevation_angle_deg ?? h.horizon_elevation_deg);if(h.status==='unknown'||!finite(e))body+=`<rect x="${x-5}" y="${top+7}" width="10" height="${bottom-top-7}" fill="#d8d0bb" opacity=".4"/>`;else body+=`<path d="M${x} ${bottom}V${yAt(e)}" stroke="#a9bb97" stroke-width="5" opacity=".6"/>`;}
    for(const [index,sample] of sourceSamples.slice(0,80).entries()){
      const x=xAt(Number(sample.bearing_deg ?? sample.azimuth_deg));if(x<left||x>right)continue;
      const y=yAt(Number(sample.elevation_angle_deg ?? sample.apparent_elevation_deg));const color=sample.status==='visible'?'#426946':sample.status==='blocked'?'#98826a':'#b79c64';
      body+=`<g><title>${esc(sample.name || sample.target_name || sample.target_id || '대상 표본')} · ${esc(label(sample.status))}</title><path d="M${x} ${bottom}V${y}" stroke="${color}" stroke-width="1" opacity=".25"/><circle cx="${x}" cy="${y}" r="${sample.status==='visible'?3:2}" fill="${color}"/>${index<4?`<text x="${clamp(x,70,610)}" y="${Math.max(31,y-8-index%2*12)}" text-anchor="middle" fill="${color}" font-size="9">${esc(String(sample.name || sample.target_name || '').slice(0,18))}</text>`:''}</g>`;
    }
    if(!sourceSamples.length&&!horizon.length)body+=`<text x="340" y="91" text-anchor="middle" fill="#89917f" font-size="12">표본의 각도 자료가 없어 단면을 그리지 않았습니다.</text><text x="340" y="111" text-anchor="middle" fill="#89917f" font-size="10">대상별 계산 상태는 아래 근거에서 확인하세요.</text>`;
    body+=`<text x="${left}" y="169" fill="#7c8b71" font-size="9">${num((bearingValue-fov/2+360)%360)}°</text><text x="${right}" y="169" text-anchor="end" fill="#7c8b71" font-size="9">${num((bearingValue+fov/2)%360)}°</text></svg>`;
    return `<figure class="schematic">${body}<figcaption>Geometry schematic — not a photograph · 기하 개요, 사진이 아닙니다. 세로 축은 각도에 맞춰 조정됩니다. 점선 방향은 미확인 표본입니다.</figcaption></figure>`;
  }
  function accessText(view) {
    const access=view.access || {};
    return label(typeof access==='string'?access:access.status || access.state || view.access_state || 'unknown');
  }
  function aiComparison(view) {
    const ai=view.ai_presentation;if(!ai || ai.view_id!==view.id)return '';
    const focus={supported_scene:'지원되는 장면 요소',direction:'바라보는 방향',partial_evidence:'부분적인 근거와 남은 미확인',composition:'장면의 구도'}[ai.focus];
    const known=new Set(view.samples.map(sample=>sample.evidence_id));
    const refs=arr(ai.evidence_ids).filter(id=>known.has(id));
    if(!focus || !refs.length)return '';
    return `<div class="ai-comparison"><strong>AI 보조 비교 · ${esc(focus)}</strong><p>검증된 표본 근거 ${num(refs.length)}개를 참고한 비교 관점입니다. 순위·시선·접근 조건은 계산 결과를 유지합니다.</p><details><summary>참조한 근거 ID</summary><p>${refs.map(esc).join(' · ')}</p></details></div>`;
  }
  function card(view,index) {
    const counts=sampleCounts(view),categories=view.categories.map(value=>label(typeof value==='string'?value:value.category || value.name));
    const name=view.name;const countTotal=counts.total || counts.visible+counts.blocked+counts.unknown+counts.excluded;
    return `<article class="view-card" data-view-id="${esc(view.id)}" id="view-card-${index}"><div class="card-heading"><span class="rank">${index+1}</span><div><h3>${esc(name)}</h3><small>${esc(view.district || '')} · 지도·모델로 찾은 시선</small></div><div class="score"><strong>${num(view.scoreValue,1)}</strong><small>비교용 점수 · 확률 아님</small></div></div><div class="card-body"><div class="tags">${categories.map(text=>`<span class="tag">${esc(text)}</span>`).join('')}<span class="tag">${esc(accessText(view))}</span><span class="tag unknown">${counts.unknown?'일부 시선 미확인':'전체 파노라마 미확인'}</span></div><p class="card-description">${esc(view.description || `${bearing(view.bearing)} 방향의 지원되는 대상 표본을 비교했습니다. 대상 전체의 가시성과 실제 개방 여부는 별도 확인이 필요합니다.`)}</p><dl class="card-facts"><div><dt>탐색 중심에서</dt><dd>${num(view.proximity_m)} m<small>직선거리 · 경로 미확인</small></dd></div><div><dt>바라볼 방향</dt><dd>${esc(bearing(view.bearing))}<small>시야각 ${num(view.fov)}°</small></dd></div><div><dt>날씨 · 혼잡</dt><dd>미확인<small>없음이 아닌, 정보 부족</small></dd></div></dl>${schematic(view)}<div class="sample-summary"><span class="visible">보임 ${num(counts.visible)}</span><span>가림 ${num(counts.blocked)}</span><span class="unknown">미확인 ${num(counts.unknown)}</span><span>제외 ${num(counts.excluded)}</span><span>동일 대상 집합 ${num(countTotal)}개 표본 · 면적 비율 아님</span></div><div class="card-actions"><button type="button" class="outline-button" data-detail="${esc(view.id)}">위치 · 방향 · 근거 보기 ↗</button><button type="button" class="text-button" data-image="${esc(view.id)}">AI 분위기 미리보기</button></div><div class="image-panel" data-image-panel="${esc(view.id)}" hidden></div></div><details class="card-limits"><summary>이 시선에서 확인할 내용</summary><div><p>출입·개방 상태와 현장 장애물은 별도 확인이 필요합니다. 이동 시간과 경로를 계산하지 않았습니다.</p><ul>${view.limitations.map(item=>`<li>${esc(typeof item==='string'?item:JSON.stringify(item))}</li>`).join('')}</ul><p>설명 방식: ${view.presentation?.method==='ai'?'AI 보조 · 계산 근거 제한':'계산 근거에 따른 템플릿'}</p></div></details></article>`;
  }
  function renderResults(result,request) {
    $('results-title').textContent=state.views.length?'이 방향의 풍경을 제안해요':'지원되는 풍경을 찾지 못했어요';$('result-count').textContent=String(state.views.length);
    $('cards-list').innerHTML=state.views.map(card).join('');
    state.views.forEach(view=>{const description=document.querySelector(`[data-view-id="${CSS.escape(view.id)}"] .card-description`);if(description)description.insertAdjacentHTML('afterend',aiComparison(view));});
    $('empty-panel').hidden=!!state.views.length;
    if(!state.views.length)showEmpty('이번 범위에서는 근거가 부족해요',result.message || '선택한 풍경과 접근·시선 조건을 함께 지원하는 결과를 찾지 못했습니다. 반경이나 취향을 직접 바꿔 다시 탐색할 수 있습니다.');
    const metrics=result.search || result.work || result.metrics || {};
    $('request-summary').hidden=false;
    $('request-summary').innerHTML=`<strong>직선 ${num(request.radius_m/1000,1)} km</strong> 안의 서 있을 위치 · ${esc(request.view_at.slice(0,16).replace('T',' '))} 한국 시간에 볼 풍경<br>대상은 모델 범위 안에서 더 멀리 있을 수 있습니다. ${metrics.sampled===false?'':'후보와 시선을 제한해서 표본 탐색했습니다.'}`;
    $('search-evidence').hidden=false;
    $('search-evidence-body').innerHTML=`<p>보임·가림은 물리적 시선 상태, 제외는 후보 자격, 미확인은 증거 부족입니다. 지원되지 않은 표본도 전체 표본 수에 포함합니다.</p><ul>${arr(result.limitations).map(item=>`<li>${esc(item)}</li>`).join('')}</ul><pre>${esc(JSON.stringify(metrics,null,2))}</pre>`;
    drawViews(result);
  }
  function destination(point,distance,bearingDeg) {
    const r=6371008.8,angle=distance/r,b=bearingDeg*Math.PI/180,lat=point.lat*Math.PI/180,lon=point.lon*Math.PI/180;
    const phi=Math.asin(Math.sin(lat)*Math.cos(angle)+Math.cos(lat)*Math.sin(angle)*Math.cos(b));
    const lambda=lon+Math.atan2(Math.sin(b)*Math.sin(angle)*Math.cos(lat),Math.cos(angle)-Math.sin(lat)*Math.sin(phi));return [phi*180/Math.PI,lambda*180/Math.PI];
  }
  function drawViews(result) {
    if(!state.map)return;state.viewsLayer.clearLayers();state.selectedLayer.clearLayers();
    const more=arr(result.map_results).map(sceneView).filter(v=>!state.views.some(s=>s.id===v.id)).slice(0,17);
    state.mapViews=[...state.views,...more];
    [...state.views,...more].forEach((view,index)=>{
      const point=view.effective || view.requested;if(!validCoord(point))return;
      const marker=L.marker([point.lat,point.lon],{icon:L.divIcon({className:`map-marker${index>=state.views.length?' secondary':''}`,html:String(index+1),iconSize:index>=state.views.length?[23,23]:[29,29],iconAnchor:[14,14]}),keyboard:true});
      const node=document.createElement('span');node.textContent=view.name;marker.bindTooltip(node).on('click',()=>selectView(view.id)).addTo(state.viewsLayer);
    });
    if(state.views[0])focusView(state.views[0],false);
  }
  function focusView(view,move=true) {
    if(!state.map)return;state.selectedLayer.clearLayers();const point=view.effective||view.requested;if(!validCoord(point))return;
    const radius=Math.min(700,Number(view.modeled_distance_m || 700)),cone=[[point.lat,point.lon]];
    for(let i=0;i<=12;i++)cone.push(destination(point,radius,view.bearing-view.fov/2+view.fov*i/12));
    cone.push([point.lat,point.lon]);
    if(finite(view.bearing))L.polygon(cone,{color:'#8aa16d',weight:1,fillColor:'#b3c995',fillOpacity:.2,interactive:false}).addTo(state.selectedLayer);
    view.targets.slice(0,20).forEach(target=>{
      const coord=coordinates(target.location || target.position || target.coordinates || target);if(!validCoord(coord))return;
      const node=document.createElement('span');node.textContent=`${target.name || label(target.category)} · 계산 대상`;
      L.circleMarker([coord.lat,coord.lon],{radius:4,color:'#fffefa',weight:1.5,fillColor:'#b68a46',fillOpacity:1}).bindTooltip(node).addTo(state.selectedLayer);
      L.polyline([[point.lat,point.lon],[coord.lat,coord.lon]],{color:'#a2ac92',weight:.8,dashArray:'3 6',opacity:.5,interactive:false}).addTo(state.selectedLayer);
    });
    if(move)state.map.setView([point.lat,point.lon],Math.max(14,state.map.getZoom()));
    document.querySelectorAll('.view-card').forEach(el=>el.classList.toggle('selected',el.dataset.viewId===view.id));
  }
  function selectView(id) {const view=state.views.find(v=>v.id===id);if(!view){showView(id);return;}focusView(view);document.querySelector(`[data-view-id="${CSS.escape(id)}"]`)?.scrollIntoView({behavior:'smooth',block:'nearest'});}
  async function showView(id) {
    let view=state.views.find(v=>v.id===id) || state.mapViews.find(v=>v.id===id);let detailData=null;if(!view)return;
    if(!view.samples.length){try{detailData=await api(`/api/views/${encodeURIComponent(id)}`);view=sceneView(detailData);}catch(error){toast(humanError(error));return;}}
    focusView(view);
    $('view-dialog-title').textContent=view.name;
    $('view-dialog-body').innerHTML=`${schematic(view)}<p>${esc(view.description)}</p><h3>어디에 서서 바라볼까요?</h3><p>${esc(bearing(view.bearing))} 방향 · 시야각 ${num(view.fov)}° · 탐색 중심에서 직선 ${num(view.proximity_m)} m</p><div class="detail-coordinates">요청 위치: ${esc(JSON.stringify(view.requested))}<br>계산에 사용한 위치: ${esc(JSON.stringify(view.effective))}<br>격자 이동 거리: ${num(view.snap_displacement_m ?? view.observer?.snap_displacement_m,2)} m</div><p>경로 거리·이동 시간은 계산하지 않았습니다. 위 방향은 풍경을 바라볼 방향이며 이동 경로가 아닙니다.</p><h3>대상별 근거</h3><ul>${view.targets.map(target=>`<li>${esc(target.name || label(target.category))} · ${esc(label(target.status || 'unknown'))}${target.counts?` · 표본 ${num(target.counts.intended)}개 중 보임 ${num(target.counts.visible)} / 가림 ${num(target.counts.blocked)} / 미확인 ${num(target.counts.unknown)} / 제외 ${num(target.counts.excluded)}`:''}</li>`).join('') || '<li>추가 대상 정보가 없습니다.</li>'}</ul><h3>확인하지 못한 내용</h3><ul>${view.limitations.map(item=>`<li>${esc(typeof item==='string'?item:JSON.stringify(item))}</li>`).join('')}</ul><h3>태양과 먼 지평선</h3><p>${view.sun?`태양 방위 ${num(view.sun.azimuth_deg,1)}° · 고도 ${num(view.sun.elevation_deg ?? view.sun.altitude_deg,1)}°. `:''}모델 범위 밖 지평선, 구름, 노을 색과 실제 조명은 확인되지 않았습니다.</p><details><summary>전체 계산 근거 보기</summary><pre id="full-view-evidence">상세 근거 불러오는 중</pre></details>`;
    $('view-dialog-body').insertAdjacentHTML('beforeend',aiComparison(view));
    $('view-dialog').showModal();
    try{const data=detailData || await api(`/api/views/${encodeURIComponent(id)}`);if($('view-dialog').open&&$('view-dialog-title').textContent===view.name)$('full-view-evidence').textContent=JSON.stringify(data,null,2);}catch(error){if($('full-view-evidence'))$('full-view-evidence').textContent=humanError(error);}
  }
  async function loadCapabilities() {
    try{state.capabilities=await api('/api/capabilities');}catch(error){state.capabilities={error:humanError(error),local_exploration_available:false};}
  }
  function showData() {
    const c=state.capabilities || {},readiness=c.source_readiness || c.readiness || c.data_status || {},providers=c.provider_status || c.providers || {};
    const lines=(object)=>Object.entries(object).slice(0,15).map(([key,value])=>`<div class="status-line"><span>${esc(label(key))}</span><span>${esc(typeof value==='object'?value.status || JSON.stringify(value):typeof value==='boolean'?(value?'지원':'미충족'):label(value))}</span></div>`).join('');
    $('data-dialog-body').innerHTML=`<div class="data-grid"><div class="data-stat"><small>이 앱에서 할 수 있는 일</small><strong>${c.local_exploration_available?'지원되는 시선의 로컬 탐색':'자료 상태 확인 필요'}</strong></div><div class="data-stat"><small>전체 자료 준비 상태</small><strong>범위 불완전 · 시선별 검사</strong></div></div><p>서울 전역의 후보를 탐색하지만 개별 후보와 시선에 필요한 지형·장애물 자료가 유효해야 결과에 포함합니다. 일부 시선의 성공이 전체 풍경이나 지평선의 지원을 뜻하지 않습니다.</p><h3>공개 배포와 지리적 지원은 별도입니다</h3><p>현재 로컬 검증용입니다. 실제 공개할 원자료와 파생물의 이용 조건 검토가 끝나기 전 공개 배포 준비가 완료되지 않습니다.</p>${lines(readiness)}<h3>제공자 상태</h3>${lines(providers) || '<p>날씨·혼잡·AI 제공자는 미확인입니다. 키 없는 모드에서는 계산 근거와 템플릿 설명을 사용합니다.</p>'}<h3>풍경과 미리보기의 의미</h3><ul><li>서 있을 위치와 방향, 바라볼 시각, 대상 표본을 함께 비교합니다.</li><li>건물 표본의 보임은 외벽 전체나 아름다운 파노라마를 검증하지 않습니다.</li><li>나무와 계절별 잎, 담장, 공사, 실제 출입 가능 여부는 추가 확인이 필요합니다.</li><li>Geometry schematic — not a photograph. 기하 미리보기는 사진이 아닙니다.</li><li>AI-generated atmosphere preview. Actual scenery may differ. AI 이미지는 지리 검증이나 순위 근거가 아닙니다.</li><li>혼잡 정보의 부재는 한적함의 근거가 아닙니다.</li></ul><h3>출처 · 날짜 · 지원 범위</h3><pre>${esc(JSON.stringify(c,null,2))}</pre><p>© OpenStreetMap contributors · 서울특별시 등 데이터별 출처와 조건은 위 상태 정보를 확인하세요. Leaflet 1.9.4 지리 렌더러를 로컬 자산으로 사용합니다.</p>`;
    $('data-dialog').showModal();
  }
  function imagePanel(id) {return document.querySelector(`[data-image-panel="${CSS.escape(id)}"]`);}
  function renderImageJob(id,job) {
    const panel=imagePanel(id);if(!panel)return;panel.hidden=false;
    const status=job.status || 'unknown';const rawUrl=job.image_url || job.url || job.image?.url;
    let url=null;if(typeof rawUrl==='string'){try{const parsed=new URL(rawUrl,location.origin);if(parsed.origin===location.origin&&(parsed.pathname.startsWith('/api/images/')||parsed.pathname.startsWith('/api/image-assets/')))url=parsed.pathname;}catch{}}
    const reasons={disabled:'AI 사용이 꺼져 있습니다. 기하 미리보기는 계속 사용할 수 있습니다.',credentials_missing:'AI 제공자 키가 설정되지 않았습니다. 기하 미리보기를 이용해 주세요.',spending_not_authorized:'유료 사용 예산이 승인되지 않았습니다. 기하 미리보기는 그대로 사용할 수 있습니다.',budget_exhausted:'설정된 사용 예산을 소진했습니다. 기하 미리보기는 계속 사용할 수 있습니다.',storage_blocked:'이미지 저장 공간 한도에 도달했습니다. 지도와 기하 미리보기는 계속 사용할 수 있습니다.',model_not_configured:'이미지 모델이 설정되지 않았습니다.',image_queue_full:'이미지 작업 대기열이 가득 찼습니다. 잠시 후 다시 시도해 주세요.'};
    panel.innerHTML=`<strong>${esc(label(status))}</strong><p>${esc(job.message || reasons[job.reason] || job.reason || '이미지 생성은 계산 결과와 별도로 처리됩니다.')}</p>${url?`<img src="${esc(url)}" alt="AI 생성 분위기 참고 이미지. 실제 풍경과 다를 수 있습니다." loading="lazy"><p class="image-disclaimer">AI-generated atmosphere preview. Actual scenery may differ.<br>AI 생성 분위기 참고 이미지 · 실제 풍경과 다를 수 있습니다.</p>`:''}`;
    return ['queued','pending','running','generating'].includes(status);
  }
  async function requestImage(id) {
    if(state.imageJobs.has(id))return;
    const panel=imagePanel(id);if(!panel)return;panel.hidden=false;panel.textContent='제공자와 허용 예산을 확인하고 있습니다.';
    state.imageJobs.set(id,true);
    try {
      let job=await api('/api/images',{method:'POST',body:JSON.stringify({view_id:id})});
      let active=renderImageJob(id,job);const jobId=job.job_id || job.id || job.key;
      for(let attempts=0;active&&jobId&&attempts<60;attempts++){
        await new Promise(resolve=>setTimeout(resolve,2000));
        if(!document.contains(panel))break;
        job=await api(`/api/images/${encodeURIComponent(jobId)}`);active=renderImageJob(id,job);
      }
      if(active)panel.insertAdjacentHTML('beforeend','<p>화면의 대기 시간에 도달했습니다. 생성 상태는 서버에서 다시 확인할 수 있습니다.</p>');
    }catch(error){panel.textContent=`${humanError(error)} 기하 미리보기는 계속 사용할 수 있습니다.`;}finally{state.imageJobs.delete(id);}
  }
  let searchTimer;
  function searchPlaces(){
    clearTimeout(searchTimer);const q=$('place-search').value.trim();const serial=++state.searchSerial;state.searchController?.abort();
    if(q.length<2){$('place-results').hidden=true;$('place-search').setAttribute('aria-expanded','false');return;}
    searchTimer=setTimeout(async()=>{
      state.searchController=new AbortController();
      try{
        const data=await api(`/api/places?q=${encodeURIComponent(q)}`,{signal:state.searchController.signal});if(serial!==state.searchSerial)return;
        const places=arr(data.places || data.results || data).slice(0,10);const box=$('place-results');box.replaceChildren();
        if(!places.length){const line=document.createElement('p');line.className='search-empty';line.textContent='로컬 자료에서 이름을 찾지 못했습니다. 지도를 클릭해 선택할 수 있습니다.';box.append(line);}
        places.forEach(place=>{const point=coordinates(place.location || place.coordinates || place);if(!validCoord(point))return;const button=document.createElement('button');button.type='button';button.className='place-result';const title=document.createElement('span');title.textContent=place.name || '이름 없는 지도 객체';const sub=document.createElement('small');sub.textContent=`${place.category || place.kind || '로컬 지도'} · ${point.lat.toFixed(4)}, ${point.lon.toFixed(4)}`;button.append(title,sub);button.addEventListener('click',()=>{setOrigin(point,place.name);$('place-search').value=place.name;box.hidden=true;$('place-search').setAttribute('aria-expanded','false');});box.append(button);});box.hidden=false;$('place-search').setAttribute('aria-expanded','true');
      }catch(error){if(error.name!=='AbortError')toast(humanError(error));}
    },300);
  }
  function bindEvents() {
    $('planner-form').addEventListener('submit',recommend);
    $('cancel-button').addEventListener('click',()=>{state.requestSerial++;state.requestController?.abort();setLoading(false);toast('화면 요청을 취소했습니다. 진행 중인 서버 계산도 제한된 작업량 안에서 종료됩니다.');if(!state.views.length)showEmpty('탐색을 취소했어요','조건을 바꿔 다시 시작할 수 있습니다.');});
    $('coordinates-button').addEventListener('click',()=>setOrigin({lon:Number($('origin-lon').value),lat:Number($('origin-lat').value)},'직접 입력한 중심'));
    $('radius-options').addEventListener('change',drawOrigin);
    $('place-search').addEventListener('input',searchPlaces);
    $('place-search').addEventListener('keydown',event=>{if(event.key==='Escape'){$('place-results').hidden=true;$('place-search').setAttribute('aria-expanded','false');}if(event.key==='ArrowDown'&&!$('place-results').hidden){event.preventDefault();$('place-results').querySelector('button')?.focus();}});
    $('location-button').addEventListener('click',()=>{if(!navigator.geolocation)return toast('이 브라우저는 위치 정보를 지원하지 않습니다.');navigator.geolocation.getCurrentPosition(position=>setOrigin({lon:position.coords.longitude,lat:position.coords.latitude},'브라우저 위치'),()=>toast('위치 권한이 없거나 위치를 확인하지 못했습니다. 지도를 클릭해 중심을 선택하세요.'),{enableHighAccuracy:false,timeout:10000,maximumAge:60000});});
    $('coverage-toggle').addEventListener('click',()=>{state.coverageVisible=!state.coverageVisible;$('coverage-toggle').setAttribute('aria-pressed',String(state.coverageVisible));$('coverage-toggle').textContent=state.coverageVisible?'범위 숨기기':'범위 보기';$('coverage-legend').hidden=!state.coverageVisible;refreshMap();});
    for(const id of ['data-button','evidence-button'])$(id).addEventListener('click',showData);
    document.querySelectorAll('.dialog-close').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
    document.querySelectorAll('dialog').forEach(dialog=>dialog.addEventListener('click',event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();}}));
    $('cards-list').addEventListener('click',event=>{const detail=event.target.closest('[data-detail]'),image=event.target.closest('[data-image]');if(detail)showView(detail.dataset.detail);if(image)requestImage(image.dataset.image);});
    $('back-to-map').addEventListener('click',()=>$('map').scrollIntoView({behavior:'smooth',block:'center'}));
    window.addEventListener('pagehide',()=>{state.requestController?.abort();state.mapController?.abort();state.searchController?.abort();});
  }
  $('view-at').value=localDateInput();bindEvents();initMap();loadCapabilities();
})();
