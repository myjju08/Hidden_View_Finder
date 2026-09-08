'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const html = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const safeUrl = (value) => {
    if (!value) return '';
    try { const url = new URL(value, location.origin); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; } catch { return ''; }
  };
  const labels = {nature:'자연',city:'도시',river:'강',forest:'숲',mountain:'산과 능선',skyline:'스카이라인',night:'야경',sunset:'노을',bridge:'다리',park:'공원',water:'수면',greenery:'녹음',building:'건축물',walking:'도보',transit:'대중교통',driving:'자동차',quiet:'한적한 분위기',balanced:'적당한 활기',lively:'활기찬 분위기',any:'혼잡도 무관',visible:'점 가시성 · 보임',blocked:'점 가시성 · 가림',excluded:'조망 대상에서 제외',unknown:'미확인',unverified:'미확인',verified:'확인됨',computed:'계산됨',estimated:'추정',observed:'관측',forecast:'예보',scenario:'가상 시나리오',simulated:'가상 시나리오',passed:'필수 조건 충족',failed:'필수 조건 불충족',low:'낮음',medium:'보통',high:'높음',not_generated:'이미지 미생성',generated:'AI 이미지'};
  const criteriaLabels = {preferences:'풍경 취향 일치',visibility:'가시성 · 구도',time_weather:'시간 · 날씨',travel:'이동 편의',crowd:'혼잡도 선호'};
  const weights = {preferences:30,visibility:25,time_weather:20,travel:15,crowd:10};
  const reasons = {
    'Travel time exceeds the maximum':'최대 이동 시간을 초과합니다.',
    'Walking distance exceeds the maximum':'최대 걷는 거리를 초과합니다.',
    'Public entry is prohibited':'일반인 출입이 금지된 장소입니다.',
    'Public access is unverified':'일반인 출입 가능 여부가 확인되지 않았습니다.',
    'Travel time is estimated or unverified':'이동 시간이 추정값이며 아직 검증되지 않았습니다.',
    'Walking distance is estimated or unverified':'걷는 거리가 추정값이며 아직 검증되지 않았습니다.',
    'Travel time is unknown':'이동 시간을 확인할 수 없습니다.',
    'Walking distance is unknown':'걷는 거리를 확인할 수 없습니다.',
    'Point visibility is unknown':'대상 점의 가시성이 확인되지 않았습니다.',
    'The route or standing location has stairs':'경로나 서 있을 지점에 계단이 있습니다.',
    'Absence of stairs is unverified':'전체 경로에 계단이 없는지 확인되지 않았습니다.',
    'Arrival plus the requested stay exceeds the available window':'도착 후 머무는 시간이 일정 종료 시간을 넘습니다.',
    'The spot is closed during part or all of the planned stay':'방문 시간의 일부 또는 전부가 개방 시간에 포함되지 않습니다.',
    'Opening hours for the entire planned stay are unverified':'머무는 시간 전체에 대한 개방 여부가 확인되지 않았습니다.',
    'A route for the selected transport mode is not verified':'선택한 이동 수단의 경로가 확인되지 않았습니다.',
    'Wheelchair access requirement is not met':'휠체어 접근 조건을 만족하지 않습니다.',
    'Stroller access requirement is not met':'유모차 접근 조건을 만족하지 않습니다.',
    'Wheelchair access is unverified for the complete route and standing location':'전체 경로와 조망 지점의 휠체어 접근성이 미확인입니다.',
    'Stroller access is unverified for the complete route and standing location':'전체 경로와 조망 지점의 유모차 접근성이 미확인입니다.',
    'Point visibility state is blocked; this endpoint cannot be recommended for the selected target':'대상 점이 가려져 이 조망 지점을 제외했습니다.',
    'Point visibility state is excluded; this endpoint cannot be recommended for the selected target':'건물 점유 등으로 지상 관찰 지점에 해당하지 않습니다.'
  };
  const presets = {scenario:{lon:126.978,lat:37.571,name:'가상 출발점 · 실제 방문 장소 아님'},gwanghwamun:{lon:126.9777,lat:37.578,name:'광화문광장'},cityhall:{lon:126.978,lat:37.5665,name:'서울시청'},gyeongbokgung:{lon:126.9736,lat:37.5758,name:'경복궁역'}};
  let bootstrap = {}, lastResult = null, lastRequest = null, busy = false, activeView = 'cards', loadingTimer = null;
  let defaults = {mode:'scenario',start:presets.scenario,visit_time:'2026-09-08T16:00:00+09:00',available_until:'2026-09-08T19:30:00+09:00',stay_minutes:30,preferences:['nature','river'],crowd_preference:'quiet',transport_mode:'walking',max_travel_minutes:45,max_walk_m:2500,k:3};
  const num = (value, digits=0) => value === null || value === undefined || !Number.isFinite(Number(value)) ? '미확인' : Number(value).toLocaleString('ko-KR',{maximumFractionDigits:digits});
  const translated = (value) => labels[value] || value || '미확인';
  const translatedReason = (value) => reasons[value] || value;
  const badge = (value, text) => `<span class="status-badge ${html(value === 'scenario' ? 'simulated' : value)}">${html(text || translated(value))}</span>`;
  const list = (value) => Array.isArray(value) ? value : value ? [value] : [];
  const coord = (spot) => ({lon:Number(spot.lon ?? spot.location?.lon),lat:Number(spot.lat ?? spot.location?.lat)});
  const finiteCoord = (point) => Number.isFinite(point.lon) && Number.isFinite(point.lat);
  const localTime = (value, includeDate=false) => {
    if (!value) return '미확인';
    try { return new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',...(includeDate ? {month:'long',day:'numeric'} : {}),hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value)); } catch { return String(value); }
  };
  const datetimeInput = (value) => {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value).slice(0,16);
    return new Date(date.getTime()+9*3600000).toISOString().slice(0,16);
  };
  const bearing = (value) => {
    if (value === undefined || value === null || !Number.isFinite(Number(value))) return '방향 미확인';
    const names=['북','북동','동','남동','남','남서','서','북서'];
    const degree=((Number(value)%360)+360)%360;
    return `${names[Math.round(degree/45)%8]}쪽 ${num(degree)}°`;
  };
  const targetNames = (spot) => list(spot.target_names).length ? spot.target_names : list(spot.target_ids).map((id) => lastResult?.landmarks?.find((target) => target.id===id)?.name || id);

  function setSelect(id, value) {
    if (value === undefined || value === null) return;
    const select=$(id);
    if (![...select.options].some((option)=>option.value===String(value))) select.add(new Option(value,value));
    select.value=value;
  }
  function applyDefaults(data=defaults) {
    $('mode').value=data.mode || 'scenario';
    const start=data.start || presets.scenario;
    $('lon').value=start.lon; $('lat').value=start.lat;
    const preset=Object.entries(presets).find(([,v])=>Math.abs(v.lon-start.lon)<1e-6 && Math.abs(v.lat-start.lat)<1e-6);
    $('start-preset').value=preset?.[0] || 'custom';
    $('visit-time').value=datetimeInput(data.visit_time);
    $('available-until').value=datetimeInput(data.available_until);
    $('stay-minutes').value=data.stay_minutes ?? 30;
    setSelect('purpose',data.purpose); setSelect('group',data.group);
    setSelect('crowd',data.crowd_preference || 'quiet');
    setSelect('transport',data.transport_mode || 'walking');
    $('max-travel').value=data.max_travel_minutes ?? 45;
    $('max-walk').value=data.max_walk_m ?? 2500;
    $('max-slope').value=data.max_slope_percent ?? '';
    $('k').value=data.k ?? 3;
    ['wheelchair','stroller','no-stairs'].forEach((id)=>{$(id).checked=Boolean(data[id.replace('-','_')]);});
    document.querySelectorAll('#preferences input').forEach((input)=>{input.checked=(data.preferences || []).includes(input.value);});
    updateMode();
  }
  function updateMode() {
    const real=$('mode').value==='seoul';
    $('mode-note').textContent=real ? '서울 지형·건물과 실제 보행망을 사용합니다. 경로·개방·접근 조건을 확인하지 못한 지점은 확정 추천과 구분합니다.' : '장소·지형·경로·날씨·혼잡은 가상의 예시입니다. 실제 방문 추천이 아닌 추천 과정 체험입니다.';
    $('data-badge').innerHTML=`<i></i> ${real ? '서울 데이터 탐색' : '시나리오 데모'}`;
  }
  function collectRequest() {
    const preset=presets[$('start-preset').value];
    const checked=[...document.querySelectorAll('#preferences input:checked')].map((input)=>input.value);
    const preferences=[...(defaults.preferences || []).filter((value)=>checked.includes(value)),...checked.filter((value)=>!(defaults.preferences || []).includes(value))];
    return {mode:$('mode').value,start:{lon:Number($('lon').value),lat:Number($('lat').value),name:preset?.name || '사용자 지정 출발점'},visit_time:`${$('visit-time').value}:00+09:00`,available_until:`${$('available-until').value}:00+09:00`,timezone:'Asia/Seoul',stay_minutes:Number($('stay-minutes').value),purpose:$('purpose').value,group:$('group').value,activity:$('purpose').value===defaults.purpose ? defaults.activity || 'walking' : $('purpose').value==='사진 촬영' ? 'photography' : 'walking',preferences,crowd_preference:$('crowd').value,transport_mode:$('transport').value,max_travel_minutes:Number($('max-travel').value),max_walk_m:Number($('max-walk').value),wheelchair:$('wheelchair').checked,stroller:$('stroller').checked,no_stairs:$('no-stairs').checked,max_slope_percent:$('max-slope').value===''?null:Number($('max-slope').value),eye_height_m:1.7,k:Number($('k').value)};
  }
  function loading(isBusy) {
    busy=isBusy;
    $('recommend-button').disabled=isBusy;
    $('recommend-button').querySelector('span').textContent=isBusy ? '풍경을 찾고 있어요…' : '나에게 맞는 풍경 찾기';
    $('loading-panel').hidden=!isBusy;
    $('results').setAttribute('aria-busy',String(isBusy));
    clearInterval(loadingTimer);
    if (isBusy) {
      $('empty-panel').hidden=true;
      $('cards-list').hidden=true;
      $('comparison-panel').hidden=true;
      $('unverified-panel').hidden=true;
      $('excluded-panel').hidden=true;
      let step=0;
      const messages=['방문 조건과 데이터 범위를 확인합니다.','바라볼 대상과 조망 지점을 살펴봅니다.','이동 경로와 필수 접근 조건을 확인합니다.','확인된 근거를 바탕으로 후보를 비교합니다.'];
      const tick=()=>{document.querySelectorAll('.pipeline li').forEach((li,index)=>{li.className=index<step?'done':index===step?'current':'';});$('loading-message').textContent=messages[step];step=Math.min(step+1,3);};
      tick(); loadingTimer=setInterval(tick,950);
    }
  }
  async function recommend() {
    if (busy || !$('planner-form').reportValidity()) return;
    const request=collectRequest();
    if (new Date(request.available_until)<=new Date(request.visit_time)) { showError('일정 종료 시간은 출발 시간보다 늦어야 합니다.'); return; }
    $('error-box').hidden=true;
    loading(true);
    try {
      const response=await fetch('/api/recommend',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)});
      const data=await response.json();
      if (!response.ok) throw new Error(typeof data.error==='string' ? data.error : data.error?.message || data.message || `추천 요청 실패 (${response.status})`);
      lastResult=data; lastRequest=request;
      loading(false); renderResult(data,request);
    } catch (error) {
      loading(false); showError(error.message || '서버에 연결하지 못했습니다. 실행 상태를 확인하고 다시 시도해 주세요.');
      document.querySelectorAll('.pipeline li').forEach((li)=>{li.className='';});
      if (lastResult) { $('cards-list').hidden=activeView!=='cards';$('comparison-panel').hidden=activeView!=='table'; }
      else { $('empty-panel').hidden=false; }
    }
  }
  function showError(message) { $('error-box').textContent=message;$('error-box').hidden=false; }

  function renderResult(result,request) {
    const recommendations=list(result.recommendations), unverified=list(result.unverified), excluded=list(result.excluded), real=request.mode==='seoul';
    document.querySelectorAll('.pipeline li').forEach((li)=>{li.className='done';});
    $('results-title').textContent=recommendations.length ? `당신을 위한 ${recommendations.length}개의 시선` : '조건을 확인하며, 다음 시선을 찾습니다';
    $('recommendation-title').textContent=real ? '조건을 충족한 조망점' : '시나리오 추천 조망점';
    $('result-count').textContent=recommendations.length;
    $('data-badge').innerHTML=`<i></i> ${real ? '서울 데이터 · 검증 우선' : '가상 시나리오 결과'}`;
    $('context-banner').classList.toggle('real-mode',real);
    $('context-banner').innerHTML=`<span aria-hidden="true">ⓘ</span><p>${real ? '실제 지형·건물에서 <strong>대상 점의 가시성</strong>을 계산합니다. 접근성과 방문 조건이 미확인인 후보는 아래에 분리해 표시합니다.' : '<strong>실제 서울 추천이 아닌 가상 시나리오입니다.</strong> 장소·가시성·경로·날씨·혼잡은 예시이며, 생성 이미지는 장소 검증에 사용하지 않습니다.'}</p>`;
    const summary=$('request-summary'); summary.hidden=false;
    summary.innerHTML=`<span><b>${html(request.start.name)}</b> 출발</span><span>${html(localTime(request.visit_time,true))}–${html(localTime(request.available_until))}</span><span>${html(translated(request.transport_mode))} ${num(request.max_travel_minutes)}분 이내</span><span>체류 ${num(request.stay_minutes)}분</span><span>희망 ${request.k}곳</span><p class="summary-note">${html(request.preferences.map(translated).join(' · ') || '선택한 풍경 취향 없음')} · ${html(translated(request.crowd_preference))} · 시각 Asia/Seoul · 점 관찰 눈높이 1.7 m${result.timing_s !== undefined ? ` · 처리 ${num(typeof result.timing_s==='number' ? result.timing_s : result.timing_s.total,3)}초` : ''}</p>`;
    renderLandmarks(list(result.landmarks)); renderMap(result,request);
    $('cards-list').innerHTML=recommendations.map((spot,index)=>spotCard(spot,index+1,false)).join('');
    $('cards-list').hidden=activeView!=='cards';
    $('comparison-panel').innerHTML=comparisonTable(recommendations);
    $('comparison-panel').hidden=activeView!=='table' || !recommendations.length;
    $('empty-panel').hidden=recommendations.length>0;
    if (!recommendations.length) $('empty-panel').innerHTML=`<span class="empty-icon" aria-hidden="true">⌕</span><h3>모든 필수 조건을 확인한 후보가 없습니다</h3><p>${unverified.length ? `아래 ${unverified.length}개 후보는 추가 확인이 필요합니다.` : '출발점, 방문 시간, 이동 범위를 조정해 다시 탐색해 보세요.'}<br>미확인 후보를 확정 추천으로 바꾸지 않습니다.</p>`;
    if (recommendations.length && recommendations.length<request.k) $('cards-list').insertAdjacentHTML('afterbegin',`<p class="section-copy">요청한 ${request.k}곳 중 ${recommendations.length}곳이 조건을 충족했습니다. 나머지 후보의 확인 필요 사항과 제외 이유를 아래에서 볼 수 있습니다.</p>`);
    $('unverified-panel').hidden=!unverified.length;
    $('unverified-list').innerHTML=unverified.slice(0,8).map((spot,index)=>spotCard(spot,index+1,true)).join('');
    $('more-unverified').hidden=unverified.length<=8;
    $('more-unverified').textContent=`나머지 ${unverified.length-8}개 확인 필요 후보 보기 ↓`;
    $('excluded-panel').hidden=!excluded.length && !list(result.duplicates).length;
    $('excluded-count').textContent=`${excluded.length}곳${list(result.duplicates).length ? ` · 유사 조망 통합 ${result.duplicates.length}곳` : ''}`;
    $('excluded-list').innerHTML=excluded.map((spot)=>`<div class="excluded-item"><div><strong>${html(spot.name)}</strong><p>${list(spot.hard_constraints?.reasons || spot.reasons).map((reason)=>html(translatedReason(reason))).join('<br>')}</p></div>${badge(spot.visibility || 'excluded')}</div>`).join('')+list(result.duplicates).map((spot)=>`<div class="excluded-item"><div><strong>${html(spot.name)}</strong><p>가까운 위치에서 같은 대상·방향·구도를 보여 대표 지점으로 통합했습니다.</p></div>${badge('excluded','유사 조망 통합')}</div>`).join('');
    $('download-button').hidden=false;
    $('mobile-results').hidden=false;
    $('mobile-results').textContent=recommendations.length?`추천 ${recommendations.length}곳 결과 보기 ↓`:'탐색 결과와 확인할 후보 보기 ↓';
    renderSources(list(result.sources));
  }
  function renderLandmarks(landmarks) {
    $('landmark-panel').hidden=!landmarks.length;
    $('landmark-list').innerHTML=landmarks.map((target)=>`<article class="landmark-item"><h4>${html(target.name)}</h4><p>${html(target.type==='broad_scene'?'넓은 풍경':target.type==='tower_point'?'타워 대표 점':translated(target.type))} · ${html(list(target.features).map(translated).join(' / ') || '장면 전체는 별도 확인')}</p>${badge(target.geometry_status || 'unknown',target.geometry_status==='scenario' ? '가상의 대표 점' : target.supported===false || target.engine_support==='unsupported' ? '추가 처리 필요 · 가시성 미확인' : '대상 점 · 전체 풍경 아님')}</article>`).join('');
  }
  function spotCard(spot,rank,unverified) {
    const location=coord(spot),route=spot.route || {},access=spot.access || {},weather=spot.weather || {},crowd=spot.crowd || {};
    const coverage=spot.evidence_coverage ?? 0, constraints=list(spot.hard_constraints?.reasons), image=spot.image || {};
    const id=`spot-${unverified?'u-':''}${String(spot.id).replace(/[^a-zA-Z0-9_-]/g,'')}`;
    const scenario=spot.evidence_kind==='scenario' || lastRequest?.mode==='scenario';
    const direction=bearing(spot.bearing_deg);
    const description=typeof spot.description==='string' ? spot.description : spot.description?.text || spot.recommendation_description || `${spot.standing_instruction || '표시된 좌표의 조망 지점입니다.'} ${direction}을 향해 ${targetNames(spot).join(', ')}을 바라보는 후보입니다.`;
    const opening=list(access.opening_hours).map((hours)=>`${hours.start}–${hours.end}`).join(', ') || '개방 시간 미확인';
    const stairs=access.step_free===true ? '계단 없는 지점' : access.step_free===false ? '계단 있음' : '계단 여부 미확인';
    const weatherText=weather.status==='unknown' || !weather.status ? '날씨 미확인' : [weather.precipitation_mm!=null ? `강수 ${num(weather.precipitation_mm,1)} mm` : null,weather.wind_m_s!=null ? `바람 ${num(weather.wind_m_s,1)} m/s` : null].filter(Boolean).join(' · ') || translated(weather.status);
    const mapLink=`https://www.openstreetmap.org/?mlat=${encodeURIComponent(location.lat)}&mlon=${encodeURIComponent(location.lon)}#map=18/${encodeURIComponent(location.lat)}/${encodeURIComponent(location.lon)}`;
    return `<article class="spot-card ${unverified?'unverified-card':''}" id="${html(id)}" data-spot-id="${html(spot.id)}"><div class="card-main"><div class="card-heading"><span class="rank-number">${unverified?'?':String(rank).padStart(2,'0')}</span><div class="card-heading-text"><h3>${html(spot.name)}</h3><p>${html(targetNames(spot).join(' · '))}</p></div><div class="card-score"><strong>${num(spot.score,1)}</strong>${spot.score!=null?'<span>/ 100</span>':''}<small>근거 커버리지 ${num(coverage*100)}%</small></div></div><div class="card-tags">${badge(spot.visibility || 'unknown')}${badge(unverified?'unverified':scenario?'scenario':'passed',unverified?'필수 조건 미확인':scenario?'가상 조건 충족':'필수 조건 확인')}${list(spot.scenic_features).slice(0,4).map((feature)=>badge('feature',translated(feature))).join('')}</div><p class="card-description">${html(description)}</p><div class="card-facts"><div><p class="fact-label">이동 · 권장 도착</p><p class="fact-value">${html(translated(route.mode))} ${num(route.travel_minutes)}분</p><p class="fact-note">${html(localTime(spot.arrival_at))} 도착 · 걷기 ${num(route.walking_m)} m<br>${html(translated(route.status))}</p></div><div><p class="fact-label">바라볼 방향</p><p class="fact-value">${html(direction)}</p><p class="fact-note">시야각 ${num(spot.field_of_view_deg || 60)}°${spot.field_of_view_deg?'':' (가정)'} · 눈높이 1.7 m<br>대상 점의 가시성</p></div><div><p class="fact-label">혼잡 · 방문 날씨</p><p class="fact-value">혼잡 ${html(translated(crowd.level))} ${badge(crowd.status || 'unknown')}</p><p class="fact-note">${html(weatherText)}<br>${html(translated(weather.status || 'unknown'))}</p></div></div>${imageBlock(image)}<div class="card-controls"><a class="map-link" href="${html(mapLink)}" target="_blank" rel="noopener noreferrer">${scenario?'데모 좌표 위치':'지도에서 위치 보기'} ↗</a><span class="fact-note">${num(location.lat,6)}, ${num(location.lon,6)}</span></div></div><details class="card-details"><summary>평가 근거와 확인할 내용<span>커버리지 ${num(coverage*100)}%</span></summary><div class="detail-body"><div class="detail-section"><h4>항목별 점수</h4><div class="criteria-grid">${Object.entries(weights).map(([key,weight])=>{const score=spot.criteria?.[key];return `<div class="criterion-row"><span>${criteriaLabels[key]} · ${weight}%</span><div class="criterion-track ${score==null?'missing':''}">${score==null?'':`<div class="criterion-fill" style="width:${Math.min(100,Math.max(0,Number(score)))}%"></div>`}</div><span>${score==null?'미확인':num(score)}</span></div>`;}).join('')}</div><p style="margin-top:10px">미확인 항목은 점수를 채우지 않고 나머지 가중치를 재조정합니다. 총점은 비교용 휴리스틱이며 만족 확률이 아닙니다.</p></div><div class="detail-section"><h4>이동과 접근성</h4><p>${html(stairs)} · ${html(opening)}<br>경로 ${html(translated(route.status))} · 일반인 접근 ${access.public===false?'출입 금지':html(translated(access.public_status || access.status || 'unknown'))}</p>${constraints.length?`<ul>${constraints.map((reason)=>`<li>${html(translatedReason(reason))}</li>`).join('')}</ul>`:''}</div><div class="detail-section"><h4>시간에 따라 달라지는 정보</h4><p>날씨: ${html(translated(weather.status || 'unknown'))} · 기준 ${html(localTime(weather.reference_time,true))}<br>혼잡: ${html(translated(crowd.status || 'unknown'))} · 기준 ${html(localTime(crowd.reference_time,true))}<br>주거 인구밀도를 방문 시간의 혼잡도로 대체하지 않습니다.</p></div><div class="detail-section"><h4>풍경의 근거와 한계</h4><p>${html(spot.visibility_description || '점 가시성은 대상 전체나 구도를 보장하지 않습니다.')}</p><ul>${list(spot.uncertainties).map((item)=>`<li>${html(item)}</li>`).join('')}</ul></div>${spotEvidence(spot)}${imagePrompt(image)}<div class="detail-section"><h4>출처</h4><p>${list(spot.sources || spot.source_ids).map((source)=>{const entry=list(lastResult?.sources).find((item)=>item.id===source);const url=safeUrl(entry?.url);return url?`<a href="${html(url)}" target="_blank" rel="noopener noreferrer">${html(entry.name || source)} ↗</a>`:html(entry?.name || source.name || source);}).join('<br>') || '출처 미확인'}</p></div></div></details></article>`;
  }
  function spotEvidence(spot) {
    const features=list(spot.features || spot.feature_evidence),sun=spot.sunlight || {},composition=spot.composition || {};
    return `<div class="detail-section"><h4>장면 요소별 근거</h4>${features.length?`<ul>${features.map((feature)=>`<li>${html(translated(feature.name || feature.feature))} · ${html(translated(feature.status || 'unknown'))}${feature.confidence?` · 신뢰도 ${html(feature.confidence==='fictional'?'가상 설정':feature.confidence)}`:''}${feature.detail?`<br>${html(feature.detail)}`:''}</li>`).join('')}</ul>`:'<p>장면 요소별 추가 근거가 제공되지 않았습니다.</p>'}<p>대상의 각크기 ${num(composition.angular_size_deg,2)}${composition.angular_size_deg!=null?'°':''} · 개방감 ${html(composition.openness ?? '미확인')}<br>전경 ${html(composition.foreground==='unknown' || !composition.foreground?'미확인':composition.foreground)} · 중경 ${html(composition.middle_ground==='unknown' || !composition.middle_ground?'미확인':composition.middle_ground)} · 배경 ${html(composition.background==='unknown' || !composition.background?'미확인':composition.background)}</p></div>${sun.status==='computed'?`<div class="detail-section"><h4>도착 시각의 태양 위치 · 계산값</h4><p>고도 ${num(sun.altitude_deg,1)}° · 방위각 ${num(sun.azimuth_deg,1)}°<br>천문학적 근사 위치입니다. 구름·건물 그림자·실제 조명과 노을은 확인되지 않았습니다.</p></div>`:''}`;
  }
  function imageBlock(image) {
    const url=safeUrl(image.url);
    if (!url || !['generated','available','generated_illustration','illustrative'].includes(image.status)) return '<div class="image-status"><span aria-hidden="true">◫</span><p>예상 이미지 미생성<small>생성 프롬프트와 한계는 아래 평가 근거에서 확인하세요.</small></p></div>';
    return `<figure class="image-preview" style="margin-left:0;margin-right:0"><img src="${html(url)}" alt="가상 시나리오의 분위기 참고용 AI 생성 이미지. 실제 장소 재현이 아닙니다." loading="lazy"><figcaption class="image-caption">AI-generated anticipated view — actual scenery may differ.<br>${html(image.kind==='geometry_conditioned'?'제공된 기하 구조를 참고한 예상 이미지':'분위기 참고용 이미지 · 실제 장소의 정확한 재현이 아닙니다.')}</figcaption></figure>`;
  }
  function imagePrompt(image) {
    return `<div class="prompt-box"><h4>${safeUrl(image.url)?'순위 확정 후 생성한 이미지':'예상 풍경 이미지 · 미생성'}</h4><p>${safeUrl(image.url)?'생성 이미지는 위치 검증이나 추천 순위의 근거로 사용하지 않았습니다.':'검증된 구도 정보가 부족한 경우 분위기 참고용으로만 생성할 수 있습니다. 아래 프롬프트는 순위 확정 후 작성되었습니다.'}</p><p class="image-label">AI-generated anticipated view — actual scenery may differ.</p>${image.prompt?`<details><summary>이미지 생성 프롬프트 보기</summary><pre>${html(image.prompt)}</pre></details>`:''}</div>`;
  }
  function comparisonTable(spots) {
    if (!spots.length) return '';
    return `<table><thead><tr><th>순위 · 조망점</th><th>바라볼 대상</th><th>이동</th><th>점수</th><th>근거</th><th>추천 이유</th></tr></thead><tbody>${spots.map((spot,index)=>`<tr><td><strong>${index+1}. ${html(spot.name)}</strong><small>${html(bearing(spot.bearing_deg))}</small></td><td>${html(targetNames(spot).join(', '))}</td><td>${num(spot.route?.travel_minutes)}분<small>${html(translated(spot.route?.status))}</small></td><td>${num(spot.score,1)}</td><td>${num((spot.evidence_coverage || 0)*100)}%</td><td>${html(list(spot.scenic_features).map(translated).join(' · '))}<small>${html(translated(spot.visibility))}</small></td></tr>`).join('')}</tbody></table>`;
  }
  function renderMap(result,request) {
    const data=result.map || {}, targets=list(result.landmarks).filter((target)=>finiteCoord(coord(target))), selected=list(result.recommendations), unverified=list(result.unverified).slice(0,12);
    const spots=[...selected,...unverified];
    const paths=list(data.paths).map((path)=>({coordinates:path.coordinates || path.geometry?.coordinates || [],kind:path.kind || 'footway'})).filter((path)=>path.coordinates.length>1);
    const routes=spots.map((spot)=>({coordinates:spot.route?.geometry || [],id:spot.id})).filter((path)=>Array.isArray(path.coordinates)&&path.coordinates.length>1);
    const points=[request.start,...spots.map(coord),...targets.map(coord)];
    if (data.bounds?.length===4) points.push({lon:data.bounds[0],lat:data.bounds[1]},{lon:data.bounds[2],lat:data.bounds[3]});
    paths.forEach((path)=>path.coordinates.forEach((point)=>{if(Array.isArray(point))points.push({lon:point[0],lat:point[1]});}));
    const valid=points.filter(finiteCoord);
    if (!valid.length) return;
    const minLon=Math.min(...valid.map((point)=>point.lon)),maxLon=Math.max(...valid.map((point)=>point.lon)),minLat=Math.min(...valid.map((point)=>point.lat)),maxLat=Math.max(...valid.map((point)=>point.lat));
    const midLat=(minLat+maxLat)/2,cos=Math.cos(midLat*Math.PI/180),cx=(minLon+maxLon)/2,cy=(minLat+maxLat)/2;
    const extentX=Math.max((maxLon-minLon)*cos,.006),extentY=Math.max(maxLat-minLat,.004),scale=Math.min(760/extentX,290/extentY);
    const project=(point)=>[450+(point.lon-cx)*cos*scale,195-(point.lat-cy)*scale];
    const polyline=(path,style)=>`<polyline points="${path.coordinates.filter((point)=>Array.isArray(point)&&point.length>=2).map((point)=>project({lon:Number(point[0]),lat:Number(point[1])})).map((point)=>point.map((value)=>value.toFixed(1)).join(',')).join(' ')}" ${style}/>`;
    const actual=data.kind==='osm_geometry';
    let svg=`<svg viewBox="0 0 900 390" role="img" aria-label="${actual?'제공된 OSM 보행망과 조망 후보 좌표':'가상 시나리오 조망 후보의 좌표 개요'}"><defs><pattern id="result-grid" width="45" height="45" patternUnits="userSpaceOnUse"><path d="M45 0H0V45" fill="none" stroke="#dce2d4" stroke-width=".6"/></pattern><marker id="direction-arrow" markerWidth="5" markerHeight="5" refX="4" refY="2.5" orient="auto"><path d="M0 0 5 2.5 0 5" fill="none" stroke="#749062"/></marker></defs><rect width="900" height="390" fill="#edf0e7"/><rect width="900" height="390" fill="url(#result-grid)"/>`;
    svg+=paths.map((path)=>polyline(path,'fill="none" stroke="#bdc5b1" stroke-width="1.4" stroke-linecap="round"')).join('');
    svg+=routes.map((path)=>polyline(path,'fill="none" stroke="#7c9a6b" stroke-width="2.5" stroke-linecap="round" stroke-dasharray="4 5" opacity=".65"')).join('');
    spots.forEach((spot)=>{const point=coord(spot);if(!finiteCoord(point))return;const [x,y]=project(point);const target=targets.find((entry)=>list(spot.target_ids).includes(entry.id));if(target){const [tx,ty]=project(coord(target));svg+=`<path d="M${x.toFixed(1)} ${y.toFixed(1)} L${tx.toFixed(1)} ${ty.toFixed(1)}" fill="none" stroke="#a9b695" stroke-dasharray="3 6" stroke-width="1" opacity=".7"/>`;}});
    targets.forEach((target)=>{const [x,y]=project(coord(target));svg+=`<g><title>${html(target.name)} · 바라볼 대표 점</title><rect x="${x-5}" y="${y-5}" width="10" height="10" transform="rotate(45 ${x} ${y})" fill="#b98b45" stroke="#fffefa" stroke-width="2"/><text x="${x}" y="${y-14}" text-anchor="middle" font-size="10" font-family="sans-serif" fill="#7f754f" paint-order="stroke" stroke="#edf0e7" stroke-width="4">${html(target.name)}</text></g>`;});
    spots.forEach((spot,index)=>{const point=coord(spot);if(!finiteCoord(point))return;const [x,y]=project(point),isUnverified=index>=selected.length;svg+=`<g class="map-marker" data-focus-spot="${html(spot.id)}" tabindex="0" role="button" aria-label="${html(spot.name)} 카드 보기"><title>${html(spot.name)} · ${isUnverified?'필수 조건 미확인':`${index+1}순위`}</title><circle cx="${x}" cy="${y}" r="15" fill="${isUnverified?'#b39a6b':'#355d42'}" stroke="#fffefa" stroke-width="3"/><text x="${x}" y="${y+4}" text-anchor="middle" fill="#fffefa" font-size="11" font-family="sans-serif" font-weight="600">${isUnverified?'?':index+1}</text></g>`;});
    const [sx,sy]=project(request.start);svg+=`<g><title>출발점</title><circle cx="${sx}" cy="${sy}" r="8" fill="#6a8d9b" stroke="#fffefa" stroke-width="3"/><text x="${sx}" y="${sy+24}" text-anchor="middle" font-size="10" font-family="sans-serif" fill="#567b88" paint-order="stroke" stroke="#edf0e7" stroke-width="4">출발점</text></g><g transform="translate(855 28)"><text text-anchor="middle" fill="#758568" font-size="9" font-family="sans-serif">N</text><path d="m0 8-4 12 4-3 4 3Z" fill="#758568"/></g><text x="20" y="371" font-size="9" font-family="sans-serif" fill="#7d8871">${actual?'OSM 보행망 좌표 · 점선은 제공된 경로':'FICTIONAL SCENARIO · 장소와 경로가 실제 공간을 재현하지 않습니다'}</text></svg>`;
    $('map-stage').innerHTML=svg;
    $('map-caption').textContent=actual ? '실제 OSM 보행망 · 위치 개요' : '가상 조망 시나리오 · 위치 개요';
    $('map-disclaimer').textContent=data.attribution || (actual ? '© OpenStreetMap contributors · 접근성 별도 확인' : '가상 좌표·경로 · 실제 길 안내로 사용하지 마세요');
  }
  function renderSources(sources) {
    $('visible-sources').innerHTML=`<span>사용한 근거</span>${sources.slice(0,8).map((source)=>{const url=safeUrl(source.url);return url?`<a href="${html(url)}" target="_blank" rel="noopener noreferrer">${html(source.name || source.id)} ↗</a>`:`<span class="source-label">${html(source.name || source.id)}</span>`;}).join('')}`;
  }
  function showEvidence() {
    const result=lastResult, sources=list(result?.sources || bootstrap.sources);
    $('dialog-body').innerHTML=`<p>서 있을 지점과 바라볼 대상을 구분하고, 필수 조건을 통과한 후보만 추천합니다. 확인하지 못한 이동·개방·접근 조건은 미확인 후보에 남겨 둡니다.</p><h3>계산과 추정을 구분합니다</h3><ul><li>가시성은 하나의 대상 점에 대한 계산입니다. 강 전체, 산 능선 전체, 건물 전체나 좋은 구도를 보장하지 않습니다.</li><li>열린 땅이라는 이유만으로 일반인 접근을 허용하지 않습니다. 나무, 담장, 공사장 등 데이터에 없는 장애물이 영향을 줄 수 있습니다.</li><li>날씨와 혼잡은 관측·예보·추정·미확인 및 기준 시각을 구분합니다. 주거 인구를 방문객 혼잡으로 대체하지 않습니다.</li><li>기본 시나리오는 허구의 장소와 조건으로 구성됩니다. 실제 서울 모드의 증거와 섞지 않습니다.</li></ul><h3>점수는 비교를 돕는 도구입니다</h3><p>취향 30% · 가시성/구도 25% · 시간/날씨 20% · 이동 15% · 혼잡 10%. 미확인 항목은 제외하고 가중치를 재조정합니다. 원래 가중치의 합을 근거 커버리지로 표시합니다. 점수는 검증된 만족 확률이 아니며, 커버리지가 다른 점수의 확실성도 다릅니다.</p>${result?.request_summary?`<details><summary>적용된 요청 · 가정 · 미확인 입력</summary><pre>${html(JSON.stringify(result.request_summary,null,2))}</pre></details>`:''}<h3>사용 데이터 · 출처</h3>${sources.length?sources.map((source)=>{const url=safeUrl(source.url);return `<div class="source-item">${url?`<a href="${html(url)}" target="_blank" rel="noopener noreferrer">${html(source.name || source.id)} ↗</a>`:`<strong>${html(source.name || source.id)}</strong>`}<div class="source-tags">${badge(source.kind || source.status || 'unknown')}</div><p>기준 시각: ${html(source.reference_time || source.source_date || '미확인')}<br>조회 시각: ${html(source.retrieved_at || '미확인')}${source.description?`<br>${html(source.description)}`:''}</p></div>`;}).join(''):'<p>추천을 실행하면 이 요청에 사용된 데이터와 조회 시각을 표시합니다.</p>'}<h3>이번 결과의 한계</h3><ul>${list(result?.limitations || ['가상 시나리오는 실제 방문 가능성이나 서울의 풍경을 검증하지 않습니다.']).map((limitation)=>`<li>${html(limitation)}</li>`).join('')}</ul><h3>이미지는 순위를 정한 뒤 만듭니다</h3><p>기하 구조가 부족한 경우 분위기 참고용으로만 표시합니다. 생성한 이미지는 위치 검증이나 재평가의 근거가 아닙니다.</p>`;
    $('evidence-dialog').showModal();
  }
  function switchView(view) {
    activeView=view;
    ['cards','table'].forEach((key)=>{$(`${key}-tab`).classList.toggle('active',key===view);$(`${key}-tab`).setAttribute('aria-pressed',String(key===view));});
    $('cards-list').hidden=view!=='cards';
    $('comparison-panel').hidden=view!=='table' || !list(lastResult?.recommendations).length;
  }

  $('planner-form').addEventListener('submit',(event)=>{event.preventDefault();recommend();});
  $('mode').addEventListener('change',()=>{
    updateMode();
    if($('mode').value==='seoul' && $('start-preset').value==='scenario') {
      const realDefaults=bootstrap.seoul_defaults || (bootstrap.seoul_start?{start:bootstrap.seoul_start}:null) || bootstrap.modes?.seoul?.defaults;
      if(realDefaults?.start){$('lon').value=realDefaults.start.lon;$('lat').value=realDefaults.start.lat;$('start-preset').value='custom';}
    }
  });
  $('start-preset').addEventListener('change',()=>{const preset=presets[$('start-preset').value];if(preset){$('lon').value=preset.lon;$('lat').value=preset.lat;}});
  ['lon','lat'].forEach((id)=>$(id).addEventListener('input',()=>{$('start-preset').value='custom';}));
  $('reset-button').addEventListener('click',()=>{if(busy)return;applyDefaults();recommend();});
  $('cards-tab').addEventListener('click',()=>switchView('cards'));
  $('more-unverified').addEventListener('click',()=>{list(lastResult?.unverified).slice(8).forEach((spot,index)=>$('unverified-list').insertAdjacentHTML('beforeend',spotCard(spot,index+9,true)));$('more-unverified').hidden=true;});
  $('mobile-results').addEventListener('click',()=>$('results').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'}));
  $('table-tab').addEventListener('click',()=>switchView('table'));
  ['about-button','evidence-button'].forEach((id)=>$(id).addEventListener('click',showEvidence));
  $('close-dialog').addEventListener('click',()=>$('evidence-dialog').close());
  $('evidence-dialog').addEventListener('click',(event)=>{if(event.target===$('evidence-dialog')) {const rect=$('evidence-dialog').getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)$('evidence-dialog').close();}});
  $('download-button').addEventListener('click',()=>{if(!lastResult)return;const blob=new Blob([JSON.stringify(lastResult,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`hidden-view-${lastRequest.mode}-${lastRequest.visit_time.slice(0,10)}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  function focusSpot(event) {
    const target=event.target.closest('[data-focus-spot]');if(!target)return;
    if(event.type==='keydown' && !['Enter',' '].includes(event.key))return;
    event.preventDefault();switchView('cards');
    if (![...document.querySelectorAll('[data-spot-id]')].some((item)=>item.dataset.spotId===target.dataset.focusSpot) && !$('more-unverified').hidden) $('more-unverified').click();
    const card=[...document.querySelectorAll('[data-spot-id]')].find((item)=>item.dataset.spotId===target.dataset.focusSpot);
    if(card){card.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'center'});card.classList.add('highlight');setTimeout(()=>card.classList.remove('highlight'),2000);}
  }
  $('map-stage').addEventListener('click',focusSpot);$('map-stage').addEventListener('keydown',focusSpot);

  async function init() {
    applyDefaults();
    try {
      const response=await fetch('/api/bootstrap');
      if(!response.ok)throw new Error(`초기 설정을 가져오지 못했습니다 (${response.status}).`);
      bootstrap=await response.json();
      defaults={...defaults,...(bootstrap.defaults || {})};
      applyDefaults();renderSources(list(bootstrap.sources));
      await recommend();
    } catch(error) {showError(`${error.message} 로컬 데모 서버가 실행 중인지 확인해 주세요.`);}
  }
  init();
})();
