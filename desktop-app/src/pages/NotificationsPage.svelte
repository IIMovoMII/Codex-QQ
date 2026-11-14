<script lang="ts">
  import { app } from '../app/state.svelte';
  import { observedTime } from '../app/time';
  import { deliveryLabels, deliveryTones, type Delivery, type DeliveryState } from '../bridge/contracts';
  import Button from '../components/Button.svelte';
  import EmptyState from '../components/EmptyState.svelte';
  import Icon from '../components/Icon.svelte';
  import Overlay from '../components/Overlay.svelte';
  import PageHeader from '../components/PageHeader.svelte';
  import Pill from '../components/Pill.svelte';
  import Segmented from '../components/Segmented.svelte';
  let selected = $state<Delivery | null>(null);
  let category = $state('all');
  let timeframe = $state('7');
  let eventFilter = $state('all');
  let stateFilter = $state('all');
  let visible = $state(6);
  let copied = $state(false);
  const counts = $derived({ all: app.deliveries.length, pending: app.deliveries.filter(x => x.state === 'pending').length, action: app.deliveries.filter(x => x.state === 'unknown' || x.state === 'failed').length });
  const filtered = $derived(app.deliveries.filter(item => {
    const inGroup = category === 'all' || (category === 'pending' ? item.state === 'pending' : ['unknown','failed'].includes(item.state));
    const inTime = timeframe === 'all' || item.ageDays < Number(timeframe);
    const inEvent = eventFilter === 'all' || item.event === eventFilter;
    const inState = stateFilter === 'all' || item.state === stateFilter;
    return inGroup && inTime && inEvent && inState;
  }));
  function title(event: Delivery['event']) { return event === 'complete' ? '任务完成' : event === 'attention' ? '需要确认' : '任务出错'; }
  function eventIcon(event: Delivery['event']) { return event === 'complete' ? 'success' : event === 'attention' ? 'info' : 'error'; }
  function toneFor(item: Delivery) { return item.event === 'complete' ? 'success' : item.event === 'attention' ? 'info' : 'danger'; }
  function reset() { category = 'all'; timeframe = '7'; eventFilter = 'all'; stateFilter = 'all'; }
  async function copySummary() {
    if (!selected) return;
    const text = `事件：${title(selected.event)}\n时间：${selected.time}\n目标：${selected.target}\n状态：${deliveryLabels[selected.state]}`;
    try { await navigator.clipboard.writeText(text); copied = true; setTimeout(() => copied = false, 2000); app.notify('已复制脱敏摘要'); }
    catch { app.notify(text.replace(/\n/g, ' · ')); }
  }
</script>

<div class="page">
  <PageHeader title="通知记录" subtitle="更新时间 · {observedTime(app.snapshot.observedAt)}">
    <Button variant="secondary" icon="refresh" onclick={() => app.refresh()} loading={app.pending.includes('check_connections')}>刷新</Button>
  </PageHeader>

  <Segmented label="通知筛选" value={category} onchange={(v) => { category = v; visible = 6; }} options={[{ value: 'all', label: '全部', count: counts.all }, { value: 'pending', label: '待投递', count: counts.pending }, { value: 'action', label: '需处理', count: counts.action }]} />
  <div class="filter-row">
    <label class="visually-hidden" for="time-filter">时间范围</label><select id="time-filter" bind:value={timeframe}><option value="7">最近 7 天</option><option value="1">最近 24 小时</option><option value="all">全部记录</option></select>
    <label class="visually-hidden" for="event-filter">事件类型</label><select id="event-filter" bind:value={eventFilter}><option value="all">全部事件</option><option value="complete">任务完成</option><option value="attention">需要确认</option><option value="error">任务出错</option></select>
    <label class="visually-hidden" for="delivery-filter">投递状态</label><select id="delivery-filter" bind:value={stateFilter}><option value="all">全部状态</option><option value="sent">已送出</option><option value="pending">待投递</option><option value="unknown">结果未确认</option><option value="failed">发送失败</option><option value="terminated">已终止</option></select>
    {#if category !== 'all' || timeframe !== '7' || eventFilter !== 'all' || stateFilter !== 'all'}<Button variant="ghost" onclick={reset}>重置筛选</Button>{/if}
    <span class="spacer"></span><span class="muted small">这些是助手通知，不是 Codex 待发送任务</span>
  </div>

  {#if !app.deliveriesLoaded}<section class="surface"><EmptyState icon="bell" title="通知状态待读取" description="暂时无法确认通知记录，请刷新或查看诊断。" /></section>
  {:else if filtered.length === 0}
    <section class="surface"><EmptyState icon="bell" title="没有匹配的通知" description={app.deliveries.length === 0 ? '尚无可显示的助手通知。' : '调整筛选条件后查看其他记录。'}>{#if app.deliveries.length > 0}<Button variant="secondary" onclick={reset}>清除筛选</Button>{/if}</EmptyState></section>
  {:else}
    <div class="table-wrap">
      <table class="delivery-table"><thead><tr><th scope="col">时间</th><th scope="col">事件</th><th scope="col">目标</th><th scope="col">状态</th><th scope="col"><span class="visually-hidden">查看详情</span></th></tr></thead>
        <tbody>
          {#each filtered.slice(0, visible) as item (item.id)}
            <tr class:selected={selected?.id === item.id} tabindex="0" aria-label="{item.time}，{title(item.event)}，{deliveryLabels[item.state]}" onclick={() => selected = item} onkeydown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selected = item; } }}>
              <td class="time-cell">{item.time}</td><td><span class="event-cell"><Icon name={eventIcon(item.event)} size={18} class="notice-event-icon {toneFor(item)}"/>{title(item.event)}</span></td><td class="row-target">{item.target}</td><td><Pill tone={deliveryTones[item.state]}>{deliveryLabels[item.state]}</Pill></td><td><Icon name="right" size={17}/></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <div class="list-footer"><span>显示 {Math.min(filtered.length, visible)} 条 · 保留时间以后台配置为准</span>{#if visible < filtered.length}<Button variant="secondary" onclick={() => visible += 6}>加载更多</Button>{/if}</div>
  {/if}

  {#if app.deliveries.length < app.deliveryTotal}<Button variant="secondary" onclick={() => void app.refreshDeliveries(true)}>读取更早记录</Button>{/if}
  {#if selected}
    {@const record = selected}
    <Overlay drawer title="通知详情" onclose={() => selected = null}>
      <div class="detail-summary">
        <div class="detail-summary-head"><div class="detail-event-icon"><Icon name={eventIcon(record.event)} size={25}/></div><div><h2>{title(record.event)}</h2><p class="muted" style="margin-top:3px">{record.time}</p><div class="row" style="margin-top:8px"><Icon name="user" size={16}/><span class="muted small">{record.target}</span></div></div></div>
        <Pill tone={deliveryTones[record.state]}>{deliveryLabels[record.state]}</Pill>
      </div>
      <div class="timeline" aria-label="投递阶段">
        {#each record.timeline || [] as phase}<div class="timeline-row"><span class="timeline-marker"><Icon name={phase.icon} size={14}/></span><span class="timeline-time">{phase.time}</span><span class="timeline-label">{phase.label}</span></div>{/each}
        {#if !record.timeline?.length}<p class="muted">没有可核实的阶段记录。</p>{/if}
      </div>
      <div class="detail-note"><Icon name="info" size={17}/><span>{record.state === 'unknown' ? '本次发送结果尚未确认，请先核对。' : record.state === 'sent' ? '服务确认发送成功；手机实收需要在 QQ 中核对。' : record.state === 'pending' ? '通知还在等待投递。' : record.state === 'terminated' ? '此记录已终止，不会自动重新投递。' : '当前投递失败；请查看相关诊断。'}</span></div>
      {#snippet footer()}<Button variant="secondary" icon="copy" onclick={() => void copySummary()}>复制脱敏摘要</Button><Button variant="primary" icon="activity" onclick={() => { selected = null; app.navigate('help', 'diagnostics'); }}>查看相关诊断</Button>{/snippet}
    </Overlay>
    <div class="visually-hidden" aria-live="polite">{copied ? '摘要已复制' : ''}</div>
  {/if}
</div>

<style>
  .spacer { flex:1; }
  .list-footer { display:flex; align-items:center; justify-content:space-between; gap:15px; padding:12px 4px; color:var(--muted); font-size:11px; }
  .delivery-table tr:focus-visible { outline:3px solid var(--blue); outline-offset:-3px; }
  .detail-summary { display:grid; gap:11px; }
  .detail-summary :global(.pill) { margin-left:54px; }
  @media(max-width:820px) { .filter-row > .muted { width:100%; }:global(.overlay.drawer) { z-index:6; } }
  @media(max-width:640px) { .list-footer { align-items:flex-start; font-size:9px; }.filter-row select { flex:1 1 28%; min-width:0; }.filter-row > .muted { order:4; } }
</style>
