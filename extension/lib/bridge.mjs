import { NATIVE_HOST, nativeRequest, validateNativeResponse, validateDownloadTask, redactError } from './helpers.mjs';
import { fetchPageImage } from './page-images.mjs';

export class CollectorBridge {
  constructor(browser) {this.browser=browser;}

  async load() {
    const {collectorState} = await this.browser.storage.local.get('collectorState');
    return {jobs:{},downloads:{},outbox:[],...collectorState};
  }

  async save(state) {await this.browser.storage.local.set({collectorState:state});}

  async send(method, params = {}) {
    const request=nativeRequest(method,params);
    try {return validateNativeResponse(await this.browser.runtime.sendNativeMessage(NATIVE_HOST,request),request.id);}
    catch(error) {error.message=redactError(error.message);throw error;}
  }

  async request(method, params = {}) {
    if(method==='capture' && params.retry_failed===true) {
      const state=await this.load();
      await this.reconcile(state);
      await this.flush(state);
    }
    const result=await this.send(method,params);
    if (result?.job_id || Array.isArray(result?.jobs)) {
      const state=await this.load();
      for(const summary of result.jobs || [result]) state.jobs[summary.job_id]=summary;
      state.last_error='';
      await this.save(state);
    }
    if(method==='cancel') {
      const state=await this.load();
      for(const item of Object.values(state.downloads)) if(item.job_id===params.job_id && item.download_id!==undefined && !['complete','failed'].includes(item.status)) {
        try {await this.browser.downloads.cancel(item.download_id);} catch { /* Already completed. */ }
        item.status='failed';
      }
      await this.save(state);
    }
    if(method==='resume' || (method==='capture' && params.retry_failed===true)) {
      const state=await this.load();
      const reset=new Set();
      const jobId=result?.job_id || params.job_id;
      for(const [id,item] of Object.entries(state.downloads)) if(item.job_id===jobId && item.status==='failed') {reset.add(id);delete state.downloads[id];}
      state.outbox=state.outbox.filter(entry=>!reset.has(entry.task_id));
      await this.save(state);
    }
    return result;
  }

  queueResult(state, record, status, extra = {}) {
    if(record.status===status && state.outbox.some(entry=>entry.task_id===record.task_id&&entry.status===status)) return;
    record.status=status;
    state.outbox.push({task_id:record.task_id,download_id:record.download_id ?? null,status,...extra});
  }

  async flush(state) {
    while(state.outbox.length) {
      const receipt=state.outbox[0];
      try {await this.send('browser_result',receipt);}
      catch(error) {
        // A rejected file handoff is an evidence failure. Report it once so a
        // bad/moved download cannot indefinitely block unrelated pending jobs.
        // Connection failures retain the original receipt for reconnection.
        if(receipt.status!=='complete' || error.code!=='collector_error') throw error;
        const failure={task_id:receipt.task_id,download_id:receipt.download_id,status:'failed',error:`附件交接失败：${redactError(error.message)}；请检查下载目录后重试`};
        state.outbox[0]=failure;
        if(state.downloads[receipt.task_id]) state.downloads[receipt.task_id].status='failed';
        await this.save(state);
        continue;
      }
      state.outbox.shift();
      await this.save(state);
    }
  }

  async reconcile(state) {
    for(const record of Object.values(state.downloads)) {
      if(['complete','failed'].includes(record.status)) continue;
      const matches=await this.browser.downloads.search(record.download_id===undefined?{}:{id:record.download_id});
      const item=matches.find(file=>file.filename.replaceAll('\\','/').endsWith('/'+record.relative_path));
      if(!item) {
        if(record.download_id!==undefined) this.queueResult(state,record,'failed',{error:'浏览器下载记录不存在，请重新采集或恢复任务'});
        continue;
      }
      if(record.download_id===undefined) {
        record.download_id=item.id;
        this.queueResult(state,record,'started');
      }
      if(item.state==='complete') this.queueResult(state,record,'complete',{path:item.filename});
      else if(item.state==='interrupted') this.queueResult(state,record,'failed',{error:item.error || '浏览器下载中断'});
    }
    await this.save(state);
  }

  async poll() {
    const state=await this.load();
    try {
      // Browser reconciliation happens even while the local helper is unavailable.
      await this.reconcile(state);
      await this.flush(state);
      const {tasks}=await this.send('browser_poll');
      if(!Array.isArray(tasks)) throw new Error('本地助手下载队列格式不正确');
      for(const untrusted of tasks) {
        const task=validateDownloadTask(untrusted);
        const previous=state.downloads[task.task_id];
        if(previous && previous.status!=='starting') continue;
        const record=previous || {task_id:task.task_id,job_id:task.job_id,name:task.name,relative_path:task.relative_path,status:'starting'};
        state.downloads[task.task_id]=record;
        await this.save(state);
        try {
          // No auth URL is persisted in extension storage. Chromium supplies session cookies.
          let downloadUrl = task.url;
          if (task.page_fetch) {
            const [capture] = await this.browser.scripting.executeScript({
              target: {tabId: task.page_tab_id}, world: 'MAIN', func: fetchPageImage,
              args: [task.url, task.page_url]
            });
            if (!capture?.result?.ok && !capture?.result?.browser_fallback) throw new Error(capture?.result?.error || '评论图片读取失败，请保持原工单页面打开');
            if (capture.result.ok) downloadUrl = capture.result.url;
          }
          record.download_id=await this.browser.downloads.download({url:downloadUrl,filename:task.relative_path,saveAs:false,conflictAction:'overwrite'});
          this.queueResult(state,record,'started');
        } catch(error) {this.queueResult(state,record,'failed',{error:redactError(error.message) || '浏览器无法下载此文件'});}
        await this.save(state);
        await this.flush(state);
      }
      state.last_error='';state.last_sync=new Date().toISOString();
      await this.save(state);
      return state;
    } catch(error) {
      state.last_error=redactError(error.message) || '本地助手连接失败';
      await this.save(state);
      throw error;
    }
  }
}
