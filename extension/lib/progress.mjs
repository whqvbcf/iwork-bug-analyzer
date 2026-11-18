const stages={queued:'排队中',captured:'已建立任务',preparing:'准备采集',searching:'正在检索',
  downloading:'正在下载',extracting:'正在解压',verifying:'正在校验',processing:'正在处理',
  waiting_browser:'等待浏览器附件',waiting:'等待补充',partial:'报告已生成 · 有缺口',
  ready:'采集完成',complete:'采集完成',completed:'采集完成',done:'采集完成',
  failed:'采集失败',cancelled:'已取消'};

export function progressView(job,now=Date.now()) {
  const progress=job.progress||{};
  const active=['processing','running','collecting','queued','captured'].includes(job.status);
  const total=Number(progress.total)||0,completed=Number(progress.completed)||0;
  const parts=[`材料 ${completed} / ${total} 已完成`];
  if(progress.failed)parts.push(`${progress.failed} 项失败`);
  const seconds=Math.max(0,Math.floor((now-Date.parse(progress.started_at))/1000));
  if(active&&Number.isFinite(seconds))parts.push(`已用 ${seconds>=60?`${Math.floor(seconds/60)}分`:''}${seconds%60}秒`);
  if(progress.expanded_bytes)parts.push(`已展开 ${(progress.expanded_bytes/1048576).toFixed(1)} MiB`);
  if(progress.files_done)parts.push(`${progress.files_done} 个文件`);
  if(job.waiting_count)parts.push(`${job.waiting_count} 项浏览器附件待处理`);
  return {stage:stages[progress.phase]||stages[job.status]||'处理中',
    name:active?progress.name||'正在准备材料…':'',text:parts.join(' · '),active,
    percent:total?Math.min(100,completed/total*100):null};
}
