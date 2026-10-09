// Real plugin HTML/CSS/JS with a mocked API; never connects to the NAS.
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const ui=path.resolve(__dirname,'../payload/ui');
const state={ok:true,running:true,enabled:true,version:'1.1.4',engineVersion:'1.37.0',engines:{qbittorrent:{version:'5.2.4',libtorrent:'2.0.15'}},stats:{},tasks:[],free:1e9,settings:{directory:'',trackers:[],sources:[]},trackerStatus:{},bt:{},coreUpdate:{},qbUpdate:{}};
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 try{for(const p of [{name:'desktop',width:1440,height:1000},{name:'desktop-small',width:800,height:700},{name:'ios',width:390,height:844,mobile:true},{name:'ios-dark',width:390,height:844,mobile:true,dark:true},{name:'android',width:360,height:740,mobile:true}]){
  let current=structuredClone(state); const errors=[];
  const context=await browser.newContext({viewport:{width:p.width,height:p.height},isMobile:!!p.mobile,hasTouch:!!p.mobile,colorScheme:p.dark?'dark':'light',userAgent:p.mobile?(p.name.startsWith('ios')?'Mozilla/5.0 iPhone Mobile Safari':'Mozilla/5.0 Android Mobile Chrome'):'SmartStorage Electron/30'});
  const page=await context.newPage(); page.on('pageerror',e=>errors.push(e.message));
  if(!p.mobile)await page.addInitScript(()=>window.__MICRO_APP_ENVIRONMENT__=true);
  await page.exposeFunction('coreMock',()=>current);
  await page.route('**/*',route=>{
   const name=path.basename(new URL(route.request().url()).pathname);
   if(!/^[\w.-]+$/.test(name)||!fs.existsSync(path.join(ui,name)))return route.abort();
   let body=fs.readFileSync(path.join(ui,name),'utf8');
   if(name==='client-bridge.js')body+='\nwindow.XiaomiPluginClient.request=async()=>new Response(JSON.stringify(await window.coreMock()));';
   if(name==='index.html'&&!p.mobile)body=body.replace('<body>',`<body style="margin:0"><micro-app style="display:block;position:absolute;left:20px;top:20px;width:${p.width-40}px;height:${p.height-40}px">`).replace('</body>','</micro-app></body>');
   return route.fulfill({body,contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});
  });
  await page.goto('http://core.test/index.html');
  await page.locator('[data-page=settings]').click();
  await page.locator('#qbVersion').filter({hasText:'5.2.4'}).waitFor();
  for(const phase of ['idle','checking','done','error']){
   current.qbUpdate=current.coreUpdate=phase==='idle'?{}:{state:phase,command:'check',updateAvailable:false,message:phase==='error'?'自检失败：无法创建临时目录。'+ '较长错误说明'.repeat(12):'legacy message'};
   await page.locator('#refresh').click();
   await page.waitForFunction(phase=>document.querySelector('#qbStatus').dataset.state===(phase==='checking'?'busy':phase),phase);
   assert.equal(await page.locator('#qbStatus').textContent(),await page.locator('#coreStatus').textContent());
   await page.locator('.core-grid').scrollIntoViewIfNeeded();
   for(const card of await page.locator('.core-panel').all()){
    assert(await card.evaluate(e=>e.scrollWidth<=e.clientWidth+1),'card overflow '+p.name);
   }
   const [qb,aria]=await Promise.all(['#qbCheck','#coreCheck'].map(s=>page.locator(s).boundingBox()));
   if(p.width>900)assert(Math.abs(qb.y-aria.y)<2,'desktop controls not aligned');
   if(p.mobile)assert(aria.y>qb.y,'mobile order');
  }
  current.qbUpdate=current.coreUpdate={state:'done',command:'check',updateAvailable:false};
  await page.locator('#refresh').click();
  await page.waitForFunction(()=>document.querySelector('#qbStatus').dataset.state==='done');
  await page.locator('.core-grid').scrollIntoViewIfNeeded();
  if(process.env.CORE_SCREENSHOTS){fs.mkdirSync(process.env.CORE_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.CORE_SCREENSHOTS,p.name+'.png')});}
  assert.deepEqual(errors,[]);console.log('PASS',p.name);await context.close();
 }}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
