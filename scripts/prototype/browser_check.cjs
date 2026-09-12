/* Real browser smoke checks. Defaults to a sandboxed Chromium headless shell.
 * Run with browser_run.py so profiles, screenshots and child writes are budgeted.
 * Synthetic API failure fixtures below are explicitly recorded as such. */
'use strict';
const fs=require('fs');
const path=require('path');
const root=path.resolve(__dirname,'../..');
const {chromium,firefox}=require(path.join(root,'data/prototype/dependencies/playwright-core'));
const engine=process.env.HVF_BROWSER_ENGINE || 'chromium';
if(!['chromium','firefox'].includes(engine))throw Error('Unsupported browser engine');
const reportPath=path.join(root,'reports/prototype',engine==='firefox'?'firefox-dependencies.json':'browser-dependencies.json');
const dep=JSON.parse(fs.readFileSync(reportPath,'utf8'));
const base=process.env.HVF_BROWSER_BASE || 'http://127.0.0.1:8765';
if(!/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(base))throw Error('Only localhost browser checks are permitted');
const artifacts=process.env.HVF_BROWSER_ARTIFACTS || path.join(root,'data/prototype/staging/artifacts');
const origin=(process.env.HVF_BROWSER_ORIGIN || '').split(',').map(Number);
const result={started_at:new Date().toISOString(),base,browser_engine:engine,sandbox_explicitly_disabled:false,live_real_data_checks:[],synthetic_failure_checks:[],screenshots:[],errors:[],external_requests:[],layout:[]};
function assert(value,message){if(!value)throw Error(message);}
async function screenshot(page,name){const image=await page.screenshot({type:'jpeg',quality:75,fullPage:false});assert(image.length<2_000_000,'Screenshot exceeded2MB cap');fs.writeFileSync(path.join(artifacts,name),image);result.screenshots.push({path:path.relative(root,path.join(artifacts,name)),bytes:image.length});}
async function layout(page,name){const sizes=await page.evaluate(()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,map:document.querySelector('#map').getBoundingClientRect().toJSON()}));assert(sizes.scrollWidth<=sizes.width+1,name+' horizontal overflow');assert(sizes.map.width>250&&sizes.map.height>200,name+' real map not visible');result.layout.push({name,...sizes});}
let browser;
(async()=>{
 fs.mkdirSync(artifacts,{recursive:true});
 browser=engine==='firefox'?await firefox.launch({executablePath:path.join(root,dep.browser_executable),headless:true,timeout:30000,firefoxUserPrefs:{'browser.cache.disk.enable':false,'browser.cache.memory.capacity':16384}}):await chromium.launch({executablePath:path.join(root,dep.browser_executable),headless:true,chromiumSandbox:true,timeout:30000,args:['--disable-gpu','--disable-dev-shm-usage','--disk-cache-size=4194304','--media-cache-size=0','--no-first-run']});
 result.browser_version=browser.version();
 if(process.env.HVF_BROWSER_LAUNCH_ONLY==='1'){result.status='launch_passed_only';return;}
 const context=await browser.newContext({viewport:{width:1440,height:1100},locale:'ko-KR',timezoneId:'Asia/Seoul',reducedMotion:'reduce',serviceWorkers:'block'});
 if(engine==='firefox')result.sandbox_diagnostics='Default Firefox security settings; no MOZ_DISABLE_CONTENT_SANDBOX, sandbox preference or security flag overrides. about:support probe failed separately on this host.';
 context.on('request',request=>{if(!request.url().startsWith(base)&&!request.url().startsWith('data:'))result.external_requests.push(request.url().split('?')[0]);});
 const page=await context.newPage();page.on('pageerror',error=>result.errors.push(error.message));
 await page.goto(base,{waitUntil:'networkidle',timeout:60000});await page.evaluate(()=>document.fonts.ready);assert(await page.evaluate(()=>document.fonts.check('12px "Noto Sans KR Local"')),'Bundled Korean font did not load');
 await page.waitForSelector('#map.leaflet-container');await page.waitForFunction(()=>!document.querySelector('#map-status').textContent.includes('불러오는 중'),null,{timeout:60000});
 assert(await page.title().then(t=>t.includes('Hidden View Finder')),'Wrong application page');
 await layout(page,'desktop');await screenshot(page,'desktop-initial.jpg');
 result.live_real_data_checks.push('Application loaded with bundled Leaflet and local geographic API');
 await page.locator('#coverage-toggle').click();await page.waitForTimeout(1500);assert(await page.locator('#coverage-toggle').getAttribute('aria-pressed')==='true','Coverage toggle failed');
 result.live_real_data_checks.push('Coverage overlay control');
 await page.locator('#map').click({position:{x:500,y:240}});await page.waitForTimeout(200);assert((await page.locator('#origin-label').textContent()).includes('지도에서 선택'),'Map origin click failed');
 result.live_real_data_checks.push('Map click updates origin');
 await page.locator('#place-search').fill('서울');await page.waitForTimeout(1200);assert(await page.locator('#place-results').isVisible(),'Local search results did not open');
 result.live_real_data_checks.push('Local place search');await page.locator('#place-search').press('Escape');
 if(origin.length===2&&origin.every(Number.isFinite)){
  await page.locator('.coordinate-details').evaluate(e=>e.open=true);await page.locator('#origin-lon').fill(String(origin[0]));await page.locator('#origin-lat').fill(String(origin[1]));await page.locator('#coordinates-button').click();
  await page.locator('#view-at').fill('2026-09-11T18:00');
  if(!await page.locator('#preferences input[value=greenery]').isChecked())await page.locator('#preferences label:has(input[value=greenery])').click();
  const responsePromise=page.waitForResponse(response=>response.url()===base+'/api/recommendations',{timeout:90000});await page.locator('#recommend-button').click();const response=await responsePromise;const body=await response.json();
  await page.waitForFunction(()=>document.querySelector('#loading-panel').hidden,null,{timeout:90000});
  assert(response.ok(),'Real recommendation API failed');
  const views=body.views || body.recommendations || [];result.real_query={status:body.status,views:views.length,search:body.search || {},versions:views[0]?.versions || null,view_ids:views.map(v=>v.view_id || v.id)};
  const cards=await page.locator('.view-card').count();assert(cards===Math.min(3,views.length),'Cards do not match actual results');
  result.live_real_data_checks.push('Real recommendation response, exact card count and no padding');
  if(cards){
   assert(await page.locator('.schematic').first().textContent().then(t=>t.includes('Geometry schematic')),'No-key schematic label missing');
   await page.locator('[data-detail]').first().click();await page.waitForTimeout(500);assert(await page.locator('#view-dialog').isVisible(),'Evidence dialog absent');await screenshot(page,'desktop-evidence.jpg');await page.locator('#view-dialog .dialog-close').click();
   await page.locator('[data-image]').first().click();await page.waitForFunction(()=>{const p=document.querySelector('.image-panel');return p&&!p.hidden&&!p.textContent.includes('확인하고 있습니다');},null,{timeout:30000});
   result.real_image_transition=await page.locator('.image-panel').first().textContent();result.live_real_data_checks.push('No-key real image adapter status and retained geometry schematic');
  }
  await page.locator('#results').scrollIntoViewIfNeeded();await screenshot(page,'desktop-results.jpg');
 }
 await page.locator('#data-button').click();assert(await page.locator('#data-dialog').isVisible(),'Data dialog absent');result.live_real_data_checks.push('Provider, source and incomplete-data details');await page.locator('#data-dialog .dialog-close').click();
 await page.setViewportSize({width:390,height:844});await page.evaluate(()=>window.scrollTo(0,0));await page.waitForTimeout(300);await layout(page,'mobile');await screenshot(page,'mobile-controls.jpg');
 await page.locator('#map').scrollIntoViewIfNeeded();await screenshot(page,'mobile-map.jpg');
 if(await page.locator('.view-card').count()){await page.locator('.view-card').first().scrollIntoViewIfNeeded();await screenshot(page,'mobile-result.jpg');}
 // Explicitly synthetic HTTP failures verify usable error states, not geographic success.
 await page.route('**/api/recommendations',route=>route.fulfill({status:429,contentType:'application/json',body:JSON.stringify({error:{code:'queue_full',message:'Synthetic fixture: queue full'}})}));
 await page.locator('#recommend-button').click();await page.waitForFunction(()=>!document.querySelector('#error-box').hidden);assert((await page.locator('#error-box').textContent()).includes('다른 계산'),'Rate-limit message not useful');result.synthetic_failure_checks.push('HTTP429 queue limit retains map and previous cards');
 await page.unroute('**/api/recommendations');
 const injection={view_id:'f'.repeat(24),candidate_id:'synthetic-ui-fixture',name:'<img src=x onerror="window.injected=true">',standing:{lon:126.978,lat:37.5665,effective_lon:126.978,effective_lat:37.5665,snap_displacement_m:0,district:'합성 UI 안전성 검사'},orientation:{bearing_deg:90,fov_deg:60},proximity_m:100,view_at:'2026-09-11T18:00:00+09:00',scene_samples:[{evidence_id:'synthetic-1',target_id:'synthetic-target',name:'<script>window.injected=true</script>',category:'city',state:'visible',bearing_deg:90,angular_elevation_deg:2,target:{lon:126.98,lat:37.5665,z_m:30}}],coverage:{visible:1,blocked:0,unknown:1,intended:2},supported_categories:['city'],score:{value:40},description:'Synthetic fixture: source text is untrusted.',limitations:[],access:{state:'map_supported_public'}};
 injection.scene_samples=['visible','blocked','unknown','excluded'].map((state,index)=>({...injection.scene_samples[0],evidence_id:`synthetic-${index+1}`,state,angular_elevation_deg:state==='unknown'?null:2+index}));
 injection.coverage={visible:1,blocked:1,unknown:1,excluded:1,intended:4};
 await page.route('**/api/recommendations',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({status:'partial',views:[injection],search:{sampled:true}})}));
 await page.locator('#recommend-button').click();await page.waitForFunction(()=>document.querySelector('#loading-panel').hidden);assert((await page.locator('.view-card h3').textContent()).startsWith('<img'),'Source name not preserved as literal text');assert(await page.locator('.view-card img').count()===0,'Untrusted source text created an image');assert(await page.evaluate(()=>window.injected)!==true,'Source script injection executed');assert((await page.locator('.sample-summary').textContent()).includes('미확인 1'),'Unknown sample lost from denominator');result.synthetic_failure_checks.push('Untrusted source HTML/script remains literal text; unknown sample retained against intended denominator');
 assert((await page.locator('.sample-summary').textContent()).includes('제외 1'),'Excluded sample omitted');assert((await page.locator('.sample-summary').textContent()).includes('집합 4개'),'Intended denominator changed');
 await page.locator('[data-detail]').first().click();await page.waitForSelector('#view-dialog[open]');assert((await page.locator('#view-dialog-body').textContent()).includes('일부 표본만 지원 · 전체 미확인'),'Mixed target group mislabelled as whole-target visibility');await page.locator('#view-dialog .dialog-close').click();
 result.synthetic_failure_checks.push('Visible, blocked, unknown and excluded samples retain the same four-sample denominator; mixed target group remains partial');
 await page.unroute('**/api/recommendations');
 await page.route('**/api/recommendations',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({status:'empty',views:[],search:{sampled:true},limitations:['Synthetic fixture: unsupported terrain.']})}));
 await page.locator('#recommend-button').click();await page.waitForFunction(()=>document.querySelector('#loading-panel').hidden);assert(await page.locator('.view-card').count()===0,'Empty fixture padded results');assert(await page.locator('#empty-panel').isVisible(),'Empty state not visible');result.synthetic_failure_checks.push('Unsupported/empty fixture clears stale cards and does not pad Top3');
 await page.unroute('**/api/recommendations');
 assert(result.external_requests.length===0,'Unexpected external browser request');assert(result.errors.length===0,'Browser JavaScript errors');
 result.status='passed';
})().catch(error=>{result.status='failed';result.failure=error.message;process.exitCode=1;}).finally(async()=>{
 if(browser)await browser.close();result.finished_at=new Date().toISOString();const data=JSON.stringify(result,null,2)+'\n';if(Buffer.byteLength(data)>500000)throw Error('Browser report cap');fs.writeFileSync(path.join(artifacts,'browser-report.json'),data);console.log(data);
});
