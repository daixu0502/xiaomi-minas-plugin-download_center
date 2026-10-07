// Mocked UI regression; real CSS/bridge, no NAS network or credentials.
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const ui=path.resolve(__dirname,'../payload/ui');
const mock=()=>{
 window.calls=[];window.failNext=false;
 const task={id:'test',engine:'qbittorrent',status:'active',name:'本地测试种子',directory:'Download/test',isBT:true,totalLength:'10000',completedLength:'200',downloadSpeed:'20'};
 const state={ok:true,running:true,enabled:true,engineVersion:'1.37.0',version:'1.1.0',engines:{aria2:{running:true,version:'1.37.0'},qbittorrent:{running:true,version:'5.2.4'}},stats:{downloadSpeed:20,uploadSpeed:0},tasks:[task],free:1000000,
  settings:{directory:'',concurrent:3,connections:4,downloadKiB:0,uploadKiB:1024,seedRatio:1,seedMinutes:60,ipv6:false,dht:true,pex:true,lpd:true,upnp:false,maxPeers:100,globalPeers:300,trackers:[],sources:[],autoTrackers:false},
  bt:{engine:'qbittorrent',peerPort:19400,ipv6Detected:true,restartRequired:false},trackerStatus:{},job:{},coreUpdate:{},qbUpdate:{},trackerApply:{}};
 window.XiaomiPluginClient.request=async ({action,options})=>{
  const data=JSON.parse(options.body); window.calls.push({action,data}); let out={ok:true};
  if(action==='status'){await new Promise(r=>setTimeout(r,100));out=JSON.parse(JSON.stringify(state));}
  if(action==='task'){await new Promise(r=>setTimeout(r,120));if(window.failNext){window.failNext=false;out={ok:false,error:'模拟失败'};}else task.status=data.command==='pause'?'paused':'active';}
  if(action==='bt_settings'){Object.assign(state.settings,data);state.bt.restartRequired=true;out.restartRequired=true;}
  if(action==='service'){state.bt.restartRequired=false;out.pending=true;}
  if(action==='openlist_info'){out.url='http://downloadcenter:test-password@127.0.0.1:19700/';out.directory='/nas/pool0/test/data/Download';}
  if(action==='qb_check'){state.qbUpdate={state:'done',latest:'5.2.5',libtorrent:'2.0.15',updateAvailable:true,message:'发现新版'};}
  if(action==='tracker_verify'){Object.assign(out,{configured:71,tasks:0,checked:0,rows:[],partial:false});}
  return new Response(JSON.stringify(out));
 };
};
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 try{
  for(const [name,width,height,mobile,dark] of [['desktop-wide',1920,1080,false,false],['desktop-small',800,600,false,false],['android-light',360,740,true,false],['android-dark',360,740,true,true],['ios-light',390,844,true,false],['ios-dark',390,844,true,true]]){
   const ctx=await browser.newContext({viewport:{width,height},isMobile:mobile,hasTouch:mobile,colorScheme:dark?'dark':'light',userAgent:mobile?'Mozilla/5.0 iPhone Mobile Safari':'Mozilla/5.0 Windows Electron'});
   if(!mobile)await ctx.addInitScript(()=>{window.__MICRO_APP_ENVIRONMENT__=true;});
   const page=await ctx.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',route=>{
    const name=path.basename(new URL(route.request().url()).pathname)||'index.html';
    if(!/^(index\.html|[\w-]+\.(css|js))$/.test(name))return route.abort();
    let body=fs.readFileSync(path.join(ui,name),'utf8');
    if(name==='client-bridge.js')body+='\n('+mock.toString()+')();';
    return route.fulfill({body,contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html'});
   });
   await page.goto('http://download.test/index.html');
   await page.locator('#taskList').getByRole('button',{name:'暂停',exact:true}).waitFor();
   const start=Date.now();
   await page.locator('#refresh').click();
   await page.locator('#taskList').getByRole('button',{name:'暂停',exact:true}).click();
   try { await page.locator('#taskList').getByRole('button',{name:'继续',exact:true}).waitFor({timeout:1800}); }
   catch(e){console.error(await page.evaluate(()=>({calls:window.calls,text:document.querySelector('#taskList').innerText,toast:document.querySelector('#toast').innerText,busy:[...document.querySelectorAll('[aria-busy=true]')].map(e=>e.outerHTML)})),errors);throw e;}
   assert(Date.now()-start<1800,'task UI waited for polling');
   await page.evaluate(()=>window.failNext=true);
   await page.locator('#taskList').getByRole('button',{name:'继续',exact:true}).click();
   await page.waitForFunction(()=>!document.querySelector('.task-card [aria-busy=true]'));
   assert(await page.locator('#taskList').getByRole('button',{name:'继续',exact:true}).isEnabled(),'failed operation left spinner');
   await page.locator('[data-page=settings]').first().click();
   await page.locator('#bt-ipv6').check();
   await page.locator('#btForm button[type=submit]').click();
   await page.waitForFunction(()=>!document.querySelector('#btRestart').disabled);
   assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.action==='service').length),0,'saving restarted core');
   await page.locator('#btRestart').click();
   await page.locator('#confirmDialog').waitFor({state:'visible'});
   assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.action==='service').length),0,'restart bypassed confirmation');
   await page.locator('#confirmCancel').click();
   await page.locator('#openlistInfo').click();
   await page.waitForFunction(()=>!document.querySelector('#openlistDetails').hidden);
   const urlBox=await page.locator('#openlistUrl').evaluate(e=>({w:e.getBoundingClientRect().width,parent:e.parentElement.clientWidth}));
   assert(urlBox.w>=urlBox.parent-2,'Openlist URL not full width '+name);
   if(process.env.QB_UI_SCREENSHOTS){fs.mkdirSync(process.env.QB_UI_SCREENSHOTS,{recursive:true});await page.locator('.openlist-panel').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(process.env.QB_UI_SCREENSHOTS,name+'-openlist.png')});}
   await page.locator('#openlistInfo').click();
   assert.equal(await page.locator('#openlistUrl').inputValue(),'','hidden credential retained');
   await page.locator('#qbCheck').click();
   await page.waitForFunction(()=>!document.querySelector('#qbApply').disabled);
   await page.locator('#qbApply').click();
   await page.locator('#confirmDialog').waitFor({state:'visible'});
   assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.action==='qb_apply').length),0,'upgrade bypassed confirmation');
   await page.locator('#confirmCancel').click();
   await page.locator('[data-page=trackers]').first().click();
   await page.locator('#trackerVerify').click();
   await page.waitForFunction(()=>document.querySelector('#trackerVerification').textContent.includes('暂无任务'));
   await page.locator('[data-page=settings]').first().click();
   const order=await page.locator('#page-settings h2, #page-settings h3').allTextContents();
   assert.deepEqual(order,['下载服务','下载偏好','BT 网络与节点发现','Openlist 接入','qBittorrent 内核更新','aria2 内核更新','缓存清理','协议与边界']);
   const grid=await page.locator('.core-grid').evaluate(e=>({w:e.clientWidth,scroll:e.scrollWidth,columns:getComputedStyle(e).gridTemplateColumns.split(' ').length}));
   assert(grid.scroll<=grid.w+1,'core cards overflow '+name);
   assert.equal(grid.columns,width>=1100?2:1,'core card columns '+name);
   if(process.env.QB_UI_SCREENSHOTS){await page.locator('.core-grid').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(process.env.QB_UI_SCREENSHOTS,name+'-cores.png')});}
   const switchStyle=await page.locator('#bt-ipv6').evaluate(e=>({w:e.getBoundingClientRect().width,h:e.getBoundingClientRect().height,appearance:getComputedStyle(e).appearance}));
   assert(switchStyle.w>=40&&switchStyle.w<=48&&switchStyle.h<=30&&switchStyle.appearance==='none',JSON.stringify(switchStyle));
   const layout=await page.locator('#btForm').evaluate(e=>({width:e.clientWidth,scroll:e.scrollWidth}));
   assert(layout.scroll<=layout.width+1,'BT form overflow '+name);
   await page.locator('#btForm').scrollIntoViewIfNeeded();
   if(process.env.QB_UI_SCREENSHOTS){fs.mkdirSync(process.env.QB_UI_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.QB_UI_SCREENSHOTS,name+'.png')});}
   assert.deepEqual(errors,[],name+' script errors');
   console.log('PASS '+name);await ctx.close();
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
