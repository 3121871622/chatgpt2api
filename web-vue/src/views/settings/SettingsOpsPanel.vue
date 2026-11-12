<template>
  <div class="space-y-5">
    <!-- 智能自适应调度 -->
    <section class="rounded-2xl border border-border bg-card p-5">
      <header class="flex flex-wrap items-start justify-between gap-3">
        <div class="min-w-0">
          <h3 class="text-base font-semibold text-foreground">智能自适应负载调度</h3>
          <p class="mt-1 text-xs leading-5 text-muted-foreground">
            后台保存的并发与线程数值永久保留。系统实时监控 CPU / 内存，过载时临时降配，
            恢复健康并持续 {{ savedRestoreHoldSeconds }} 秒后精准回弹到你的原始设置。
          </p>
        </div>
        <div class="flex items-center gap-2">
          <MetaChip size="xs" :tone="levelTone" strong>{{ levelLabel }}</MetaChip>
          <Button size="xs" variant="outline" :disabled="schedulerLoading" @click="loadScheduler()">
            {{ schedulerLoading ? '刷新中' : '刷新' }}
          </Button>
        </div>
      </header>

      <div v-if="scheduler" class="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
        <div class="space-y-3">
          <div class="grid gap-2 sm:grid-cols-2">
            <div class="rounded-xl border border-border bg-background px-3 py-2">
              <p class="text-[11px] text-muted-foreground">调度模式</p>
              <div class="mt-1 flex gap-1.5">
                <button
                  v-for="option in modeOptions"
                  :key="option.value"
                  type="button"
                  class="flex-1 rounded-lg border px-2 py-1 text-xs font-medium transition-colors disabled:opacity-60"
                  :class="scheduler.mode === option.value
                    ? 'border-primary bg-primary text-primary-foreground'
                    : 'border-border bg-card text-muted-foreground hover:text-foreground'"
                  :disabled="modeSwitching || policySaving"
                  @click="switchMode(option.value)"
                >
                  {{ option.label }}
                </button>
              </div>
              <p class="mt-1 text-[11px] text-muted-foreground">
                {{ modeSwitching
                  ? '正在切换模式…'
                  : scheduler.mode === 'auto' ? '自动保护 + 自动回弹' : '手动锁定：完全按保存值运行' }}
              </p>
              <p v-if="policyDirty" class="mt-0.5 text-[11px] text-amber-600 dark:text-amber-400">
                切换模式只改运行方式，不会动你未保存的阈值
              </p>
            </div>
            <div class="rounded-xl border border-border bg-background px-3 py-2">
              <p class="text-[11px] text-muted-foreground">硬件识别</p>
              <p class="mt-1 text-sm font-semibold text-foreground">
                {{ scheduler.hardware.effective_tier_label }}
                <span class="text-xs font-normal text-muted-foreground">
                  {{ scheduler.hardware.cpu_cores }} 核 · {{ scheduler.hardware.memory_total_gb }} GB
                </span>
              </p>
              <p class="mt-1 truncate text-[11px] text-muted-foreground" :title="scheduler.hardware.cpu_model">
                {{ scheduler.hardware.cpu_model || scheduler.hardware.platform }}
              </p>
            </div>
          </div>

          <div class="rounded-xl border border-border bg-background px-3 py-2.5">
            <div class="flex items-center justify-between text-xs">
              <span class="text-muted-foreground">当前生效比例</span>
              <span class="font-semibold text-foreground">{{ scheduler.scale_percent }}%</span>
            </div>
            <div class="mt-2 h-2 overflow-hidden rounded-full bg-muted">
              <div
                class="h-full rounded-full transition-all"
                :class="scheduler.scale_percent >= 90 ? 'bg-emerald-500' : scheduler.scale_percent >= 50 ? 'bg-amber-500' : 'bg-rose-500'"
                :style="{ width: `${Math.max(4, scheduler.scale_percent)}%` }"
              ></div>
            </div>
            <div class="mt-2 grid gap-1 text-[11px] text-muted-foreground sm:grid-cols-2">
              <span>CPU {{ formatPercent(scheduler.load.cpu_percent) }} / 阈值 {{ scheduler.watermarks.cpu_high }}%</span>
              <span>内存 {{ formatPercent(scheduler.load.memory_percent) }} / 阈值 {{ scheduler.watermarks.mem_high }}%</span>
              <span>降配次数 {{ scheduler.adjustments }}</span>
              <span v-if="scheduler.restore_in_seconds !== null">预计 {{ scheduler.restore_in_seconds }} 秒后回弹</span>
              <span v-else>回弹保持 {{ scheduler.watermarks.restore_hold_seconds }} 秒</span>
            </div>
          </div>

          <div class="overflow-hidden rounded-xl border border-border">
            <table class="w-full text-left text-xs">
              <thead class="bg-muted/40 text-muted-foreground">
                <tr>
                  <th class="px-3 py-1.5 font-medium">资源</th>
                  <th class="px-3 py-1.5 font-medium">用户预设</th>
                  <th class="px-3 py-1.5 font-medium">当前生效</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in knobRows" :key="row.key" class="border-t border-border">
                  <td class="px-3 py-1.5 text-muted-foreground">{{ row.label }}</td>
                  <td class="px-3 py-1.5 font-mono text-foreground">{{ row.base }}</td>
                  <td class="px-3 py-1.5 font-mono" :class="row.effective < row.base ? 'text-amber-600 dark:text-amber-400' : 'text-foreground'">
                    {{ row.effective }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="space-y-3">
          <div class="rounded-xl border border-border bg-background p-3" @input="markPolicyDirty" @change="markPolicyDirty">
            <p class="text-[11px] font-medium text-muted-foreground">策略阈值（保存后立即生效）</p>
            <p class="mt-1 text-[11px] text-muted-foreground">
              过载判定只看这里的 CPU / 内存阈值；系统负载均值仅作诊断参考，不会触发降档。
            </p>
            <div class="mt-2 grid grid-cols-2 gap-2 text-xs">
              <label v-for="field in policyFields" :key="field.key" class="flex items-center justify-between gap-2">
                <span class="text-muted-foreground">{{ field.label }}</span>
                <input
                  v-model.number="policy[field.key]"
                  type="number"
                  step="1"
                  inputmode="numeric"
                  :min="field.min"
                  :max="field.max"
                  class="h-7 w-16 rounded-md border border-border bg-card px-2 text-right font-mono"
                  :class="fieldInvalid(field.key) ? 'ring-1 ring-rose-500' : ''"
                  :aria-invalid="fieldInvalid(field.key) || undefined"
                  @blur="normalizePolicyField(field.key)"
                />
              </label>
              <label class="flex items-center justify-between gap-2">
                <span class="text-muted-foreground">机型覆盖</span>
                <select v-model="policy.hardware_override" class="h-7 w-20 rounded-md border border-border bg-card px-1 text-xs">
                  <option v-for="tier in tierOptions" :key="tier.value" :value="tier.value">{{ tier.label }}</option>
                </select>
              </label>
            </div>
            <p
              v-if="policyError"
              role="alert"
              class="mt-2 rounded-lg border border-rose-500/40 bg-rose-500/5 px-2 py-1.5 text-[11px] leading-4 text-rose-600 dark:text-rose-400"
            >
              {{ policyError }}
            </p>
            <div class="mt-2 flex flex-wrap items-center justify-between gap-2">
              <span class="text-[11px] text-muted-foreground">
                {{ policyDirty ? '有未保存的修改' : policyMessage }}
              </span>
              <div class="flex items-center gap-2">
                <Button size="xs" variant="ghost" :disabled="schedulerLoading || policySaving" @click="loadScheduler(true)">
                  重新载入
                </Button>
                <Button size="xs" variant="primary" :disabled="policySaving || modeSwitching" @click="savePolicy">
                  {{ policySaving ? '保存中' : '保存策略' }}
                </Button>
              </div>
            </div>
          </div>

          <div class="rounded-xl border border-border bg-background p-3">
            <p class="text-[11px] font-medium text-muted-foreground">最近调节记录</p>
            <ul v-if="scheduler.events.length" class="mt-1.5 space-y-1 text-[11px]">
              <li v-for="(event, index) in recentEvents" :key="index" class="flex items-start justify-between gap-2">
                <span class="text-muted-foreground">{{ eventLabel(event.event) }} · {{ event.reason }}</span>
                <span class="shrink-0 font-mono text-foreground">{{ Math.round(event.scale * 100) }}%</span>
              </li>
            </ul>
            <p v-else class="mt-1.5 text-[11px] text-muted-foreground">暂无调节记录，系统运行平稳。</p>
          </div>
        </div>
      </div>
      <p v-else class="mt-4 text-xs text-muted-foreground">{{ schedulerError || '正在读取硬件与负载信息…' }}</p>
    </section>

    <!-- 数据库维护 -->
    <section class="rounded-2xl border border-border bg-card p-5">
      <header class="flex flex-wrap items-start justify-between gap-3">
        <div class="min-w-0">
          <h3 class="text-base font-semibold text-foreground">数据库一键修复 / 优化</h3>
          <p class="mt-1 text-xs leading-5 text-muted-foreground">
            长期运行会产生空闲页碎片与过期统计信息。检查、重建索引与压缩可避免查询变慢、写入阻塞和异常退出。
          </p>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <Button v-for="action in maintenanceActions" :key="action.value" size="xs" variant="outline" :disabled="maintenanceBusy" @click="runMaintenance(action.value)">
            {{ action.label }}
          </Button>
        </div>
      </header>

      <div v-if="database" class="mt-4 grid gap-3 text-xs sm:grid-cols-2 lg:grid-cols-4">
        <div class="rounded-xl border border-border bg-background px-3 py-2">
          <p class="text-[11px] text-muted-foreground">数据库</p>
          <p class="mt-1 truncate font-mono text-foreground" :title="database.database">{{ database.backend }}</p>
        </div>
        <div class="rounded-xl border border-border bg-background px-3 py-2">
          <p class="text-[11px] text-muted-foreground">占用空间</p>
          <p class="mt-1 font-mono text-foreground">{{ formatBytes(database.size_bytes) }}</p>
        </div>
        <div class="rounded-xl border border-border bg-background px-3 py-2">
          <p class="text-[11px] text-muted-foreground">碎片率</p>
          <p class="mt-1 font-mono" :class="(database.fragmentation_percent || 0) >= database.thresholds.fragmentation_percent ? 'text-amber-600 dark:text-amber-400' : 'text-foreground'">
            {{ database.fragmentation_percent === null ? '--' : `${database.fragmentation_percent}%` }}
          </p>
        </div>
        <div class="rounded-xl border border-border bg-background px-3 py-2">
          <p class="text-[11px] text-muted-foreground">WAL / 空闲页</p>
          <p class="mt-1 font-mono text-foreground">{{ formatBytes(database.wal_size_bytes) }} / {{ database.free_pages }}</p>
        </div>
      </div>

      <div v-if="maintenanceResult" class="mt-3 rounded-xl border border-border bg-background p-3 text-xs">
        <div class="flex flex-wrap items-center gap-2">
          <MetaChip size="xs" :tone="maintenanceResult.ok ? 'success' : 'danger'" strong>
            {{ maintenanceResult.ok ? '执行完成' : '存在失败项' }}
          </MetaChip>
          <span class="text-muted-foreground">耗时 {{ (maintenanceResult.duration_ms / 1000).toFixed(1) }}s</span>
          <span class="text-muted-foreground">释放 {{ formatBytes(maintenanceResult.freed_bytes) }}</span>
          <span v-if="maintenanceResult.integrity" class="text-muted-foreground">
            完整性：{{ maintenanceResult.integrity.ok ? '正常' : maintenanceResult.integrity.detail }}
          </span>
        </div>
        <ul class="mt-2 space-y-1">
          <li v-for="(step, index) in maintenanceResult.steps" :key="index" class="flex items-center justify-between gap-2">
            <span class="truncate text-muted-foreground">{{ step.statement || step.operation }}</span>
            <span class="shrink-0 font-mono" :class="step.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'">
              {{ step.ok ? `${step.duration_ms}ms` : step.detail || '失败' }}
            </span>
          </li>
        </ul>
        <p v-for="(error, index) in maintenanceResult.errors" :key="`err-${index}`" class="mt-1 text-rose-600 dark:text-rose-400">{{ error }}</p>
      </div>
    </section>

    <!-- 任务队列 + 代理健康 -->
    <section class="grid gap-5 lg:grid-cols-2">
      <div class="rounded-2xl border border-border bg-card p-5">
        <header class="flex items-start justify-between gap-2">
          <div>
            <h3 class="text-base font-semibold text-foreground">异步检测 / AT 刷新队列</h3>
            <p class="mt-1 text-xs text-muted-foreground">独立通道，互不阻塞，线程数随负载自动伸缩。</p>
          </div>
          <div class="flex items-center gap-2">
            <span v-if="queueUpdatedAt" class="text-[11px] text-muted-foreground">
              更新 {{ new Date(queueUpdatedAt).toLocaleTimeString() }}
            </span>
            <Button size="xs" variant="outline" :disabled="queueLoading" @click="loadQueue">
              {{ queueLoading ? '刷新中' : '刷新' }}
            </Button>
          </div>
        </header>
        <div v-if="queue" class="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
          <div class="rounded-lg border border-border bg-background px-2 py-1.5">
            <p class="text-[11px] text-muted-foreground">工作线程</p>
            <p class="font-mono text-foreground">{{ queue.stats.workers }} / {{ queue.stats.worker_target }}</p>
          </div>
          <div class="rounded-lg border border-border bg-background px-2 py-1.5">
            <p class="text-[11px] text-muted-foreground">待处理</p>
            <p class="font-mono text-foreground">{{ queue.stats.queued_tokens }}</p>
          </div>
          <div class="rounded-lg border border-border bg-background px-2 py-1.5">
            <p class="text-[11px] text-muted-foreground">已完成</p>
            <p class="font-mono text-foreground">{{ queue.stats.processed_total }}</p>
          </div>
          <div class="rounded-lg border border-border bg-background px-2 py-1.5">
            <p class="text-[11px] text-muted-foreground">失败</p>
            <p class="font-mono text-foreground">{{ queue.stats.failed_total }}</p>
          </div>
        </div>
        <div v-if="queue" class="mt-2 flex flex-wrap gap-2 text-[11px] text-muted-foreground">
          <span>检测 {{ queue.stats.lanes.check }}</span>
          <span>·</span>
          <span>AT 刷新 {{ queue.stats.lanes.refresh }}</span>
          <span>·</span>
          <span>配额同步 {{ queue.stats.lanes.sync }}</span>
        </div>
        <ul v-if="queue?.jobs.length" class="mt-3 space-y-2">
          <li v-for="job in queue.jobs.slice(0, 6)" :key="job.job_id" class="rounded-lg border border-border bg-background px-3 py-2 text-xs">
            <div class="flex items-center justify-between gap-2">
              <span class="truncate font-medium text-foreground">{{ job.label }}</span>
              <span class="shrink-0 font-mono text-muted-foreground">{{ job.processed }}/{{ job.total }}</span>
            </div>
            <div class="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted">
              <div class="h-full rounded-full bg-primary transition-all" :style="{ width: `${Math.min(100, job.percent)}%` }"></div>
            </div>
            <div class="mt-1 flex items-center justify-between text-[11px] text-muted-foreground">
              <span>{{ job.state }} · {{ job.rate_per_second }}/s · 失败 {{ job.failed }}</span>
              <button
                v-if="job.state === 'running' || job.state === 'queued'"
                type="button"
                class="text-rose-600 hover:underline dark:text-rose-400"
                @click="cancelJob(job.job_id)"
              >
                取消
              </button>
            </div>
          </li>
        </ul>
        <p v-else class="mt-3 text-xs text-muted-foreground">当前没有批量任务在运行。</p>
      </div>

      <div class="rounded-2xl border border-border bg-card p-5">
        <header class="flex items-start justify-between gap-2">
          <div>
            <h3 class="text-base font-semibold text-foreground">代理健康与自动切换</h3>
            <p class="mt-1 text-xs text-muted-foreground">
              连续失败自动剔除，冷却后自动恢复；全部不可用回退直连。
            </p>
          </div>
          <Button size="xs" variant="outline" :disabled="probing" @click="probeProxy">
            {{ probing ? '探测中' : '立即探测' }}
          </Button>
        </header>
        <div v-if="proxyHealth" class="mt-3 space-y-2">
          <div class="flex flex-wrap gap-1.5 text-[11px]">
            <MetaChip v-for="(count, state) in proxyHealth.health.counts" :key="state" size="xs" :tone="stateTone(String(state))">
              {{ stateLabel(String(state)) }} {{ count }}
            </MetaChip>
            <MetaChip size="xs" tone="muted">回退：{{ proxyHealth.fallback_reference || '直连' }}</MetaChip>
          </div>
          <ul class="max-h-56 space-y-1.5 overflow-y-auto scrollbar-slim pr-1">
            <li v-for="node in proxyHealth.health.nodes" :key="node.key" class="flex items-center justify-between gap-2 rounded-lg border border-border bg-background px-2.5 py-1.5 text-xs">
              <span class="min-w-0 truncate text-foreground" :title="node.key">{{ node.label }}</span>
              <span class="shrink-0 font-mono text-[11px]" :class="stateTextClass(node.state)">
                {{ stateLabel(node.state) }}{{ node.cooldown_remaining ? ` · ${node.cooldown_remaining}s` : '' }}
              </span>
            </li>
          </ul>
          <p v-if="!proxyHealth.health.nodes.length" class="text-xs text-muted-foreground">尚未配置代理组节点。</p>
        </div>
      </div>
    </section>

    <!-- 旧项目目录同步 -->
    <section class="rounded-2xl border border-border bg-card p-5">
      <header class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 class="text-base font-semibold text-foreground">旧项目目录同步</h3>
          <p class="mt-1 text-xs text-muted-foreground">
            读取旧项目持久化目录（只读），增量同步新增账号、功能设置、账号组与日志，并把新账号自动送进测活队列。
          </p>
        </div>
        <label class="flex items-center gap-2 text-xs text-muted-foreground">
          <input v-model="legacyForm.enabled" type="checkbox" class="h-3.5 w-3.5" />
          启用自动同步
        </label>
      </header>

      <div class="mt-4 grid gap-3 lg:grid-cols-2">
        <label class="lg:col-span-2">
          <span class="text-xs font-medium text-muted-foreground">旧项目持久化目录（每行一个，容器内路径）</span>
          <textarea
            v-model="legacyPathsText"
            rows="3"
            spellcheck="false"
            placeholder="/legacy&#10;/old-data/chatgpt2api-data"
            class="mt-1.5 w-full rounded-xl border border-border bg-background px-3 py-2 font-mono text-xs text-foreground outline-none focus:border-primary"
          ></textarea>
        </label>

        <label class="flex flex-col gap-1.5">
          <span class="text-xs font-medium text-muted-foreground">同步间隔（秒，最小 60）</span>
          <input v-model.number="legacyForm.interval_seconds" type="number" min="60" max="86400" class="rounded-xl border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-primary" />
        </label>

        <label class="flex flex-col gap-1.5">
          <span class="text-xs font-medium text-muted-foreground">功能设置同步方式</span>
          <select v-model="legacyForm.sync_settings" class="rounded-xl border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-primary">
            <option value="off">不同步</option>
            <option value="fill">只补缺（推荐）</option>
            <option value="overwrite">旧项目覆盖当前</option>
          </select>
        </label>

        <label class="flex flex-col gap-1.5">
          <span class="text-xs font-medium text-muted-foreground">每轮同步日志上限</span>
          <input v-model.number="legacyForm.max_call_records_per_run" type="number" min="0" max="50000" class="rounded-xl border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-primary" />
        </label>

        <label class="flex flex-col gap-1.5">
          <span class="text-xs font-medium text-muted-foreground">每轮测活账号上限</span>
          <input v-model.number="legacyForm.check_per_run" type="number" min="0" max="5000" class="rounded-xl border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-primary" />
        </label>
      </div>

      <div class="mt-3 flex flex-wrap items-center gap-4 text-xs text-muted-foreground">
        <label class="flex items-center gap-1.5"><input v-model="legacyForm.sync_accounts" type="checkbox" class="h-3.5 w-3.5" />同步账号</label>
        <label class="flex items-center gap-1.5"><input v-model="legacyForm.sync_account_groups" type="checkbox" class="h-3.5 w-3.5" />同步账号组</label>
        <label class="flex items-center gap-1.5"><input v-model="legacyForm.sync_call_records" type="checkbox" class="h-3.5 w-3.5" />同步日志</label>
        <label class="flex items-center gap-1.5"><input v-model="legacyForm.auto_check" type="checkbox" class="h-3.5 w-3.5" />新账号自动测活</label>
      </div>

      <div class="mt-4 flex flex-wrap items-center gap-2">
        <Button size="xs" variant="primary" :disabled="legacyBusy" @click="saveLegacySync">
          {{ legacyBusy ? '保存中…' : '保存配置' }}
        </Button>
        <Button size="xs" variant="outline" :disabled="legacyBusy" @click="runLegacySync(false)">立即同步</Button>
        <Button size="xs" variant="outline" :disabled="legacyBusy" @click="runLegacySync(true)">试运行（不写入）</Button>
        <Button size="xs" variant="ghost" :disabled="legacyBusy" @click="loadLegacySync()">刷新状态</Button>
        <span v-if="legacyMessage" class="text-xs text-muted-foreground">{{ legacyMessage }}</span>
      </div>

      <div v-if="legacyState" class="mt-4 space-y-2 text-xs">
        <div v-for="probe in legacyState.probes" :key="probe.path" class="rounded-xl border border-border/70 bg-muted/20 px-3 py-2">
          <div class="flex flex-wrap items-center gap-2">
            <span class="font-mono text-[11px] text-foreground">{{ probe.path }}</span>
            <MetaChip size="xs" :tone="probe.exists ? 'success' : 'danger'" strong>
              {{ probe.exists ? '可用' : '不可用' }}
            </MetaChip>
          </div>
          <p v-if="probe.exists" class="mt-1 text-muted-foreground">
            账号 {{ probe.accounts }}（待导入 {{ probe.fresh_accounts }}）· 设置 {{ probe.settings_keys }} 项 ·
            账号组 {{ probe.account_groups }} · 日志 {{ probe.call_records }}（待导入 {{ probe.pending_call_records }}）
          </p>
          <p v-else class="mt-1 text-rose-600 dark:text-rose-400">{{ probe.error || '目录内未找到 chatgpt2api.db' }}</p>
        </div>
        <p v-if="legacyState.last_result?.finished_at" class="text-muted-foreground">
          最近同步：{{ legacyState.last_result.finished_at }} ·
          新增账号 {{ legacyState.last_result.added_accounts || 0 }} ·
          导入日志 {{ legacyState.last_result.imported_call_records || 0 }} ·
          测活入队 {{ legacyState.last_result.queued_checks || 0 }} ·
          耗时 {{ legacyState.last_result.duration_seconds || 0 }}s
        </p>
        <p v-if="legacyState.running" class="text-amber-600 dark:text-amber-400">同步任务执行中…</p>
      </div>

      <!-- 从数据库文件导入（本地上传） -->
      <div class="mt-5 border-t border-border pt-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h4 class="text-sm font-semibold text-foreground">从数据库文件导入</h4>
            <p class="mt-1 text-xs text-muted-foreground">
              本地选择旧版 <code class="text-[11px]">chatgpt2api.db</code> 上传，先预览账号再自选导入；设置默认只补缺，不会覆盖现有值。
            </p>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <input ref="legacyFileInput" type="file" accept=".db,.sqlite,.sqlite3" class="hidden" @change="onLegacyFileSelected" />
            <Button size="xs" variant="outline" :disabled="legacyUploading" @click="legacyFileInput?.click()">
              选择数据库文件
            </Button>
            <Button size="xs" variant="primary" :disabled="!legacyFile || legacyUploading" @click="uploadLegacyDb">
              {{ legacyUploading ? '上传中…' : '上传并解析' }}
            </Button>
            <Button v-if="legacyUploading" size="xs" variant="outline" @click="cancelLegacyUpload">
              取消上传
            </Button>
          </div>
        </div>

        <!-- 上传进度：200–300MB 的文件必须看得见真实速率和剩余时间 -->
        <div
          v-if="legacyUploadPhase !== 'idle'"
          class="mt-3 rounded-xl border px-3 py-2"
          :class="legacyUploadPhase === 'error' ? 'border-rose-500/40 bg-rose-500/5' : 'border-border bg-background'"
        >
          <div class="flex flex-wrap items-center justify-between gap-2 text-[11px]">
            <span class="font-medium text-foreground">{{ legacyUploadPhaseLabel }}</span>
            <span class="font-mono text-muted-foreground">
              {{ legacyUploadPercentLabel }} ·
              {{ formatLegacyBytes(legacyUploadProgress.loaded) }} / {{ formatLegacyBytes(legacyUploadProgress.total) }}
              <template v-if="legacyUploadPhase === 'sending' && legacyUploadProgress.speed > 0">
                · {{ formatLegacyBytes(legacyUploadProgress.speed) }}/s · {{ legacyUploadEtaLabel }}
              </template>
            </span>
          </div>
          <div class="mt-1.5 h-2 overflow-hidden rounded-full bg-muted">
            <div
              class="h-full rounded-full transition-all"
              :class="legacyUploadBarClass"
              :style="{ width: `${legacyUploadBarPercent}%` }"
            ></div>
          </div>
          <p
            v-if="legacyUploadError"
            role="alert"
            class="mt-2 whitespace-pre-line break-words text-[11px] leading-4 text-rose-600 dark:text-rose-400"
          >{{ legacyUploadError }}</p>
        </div>

        <p v-if="legacyUploadMessage" class="mt-2 text-xs text-muted-foreground">{{ legacyUploadMessage }}</p>

        <div v-if="legacyImports.length" class="mt-3 space-y-2">
          <div class="flex items-center justify-between gap-2">
            <p class="text-xs text-muted-foreground">已上传 {{ legacyImports.length }} 个旧数据库</p>
            <Button size="xs" variant="ghost" root-class="text-rose-600" @click="clearLegacyImports">
              {{ legacyClearPending ? '确认清空全部' : '清空全部' }}
            </Button>
          </div>
          <div v-for="item in legacyImports" :key="item.import_id"
               class="rounded-xl border border-border/70 bg-muted/20 px-3 py-2">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div>
                <p class="text-xs font-medium text-foreground">
                  {{ item.uploaded_at }} · {{ formatLegacyBytes(item.size_bytes) }}
                </p>
                <p class="mt-0.5 text-[11px] text-muted-foreground">
                  账号 {{ item.accounts }} · 设置 {{ item.settings_keys }} 项 · 账号组 {{ item.account_groups }} · 日志 {{ item.call_records }}
                </p>
              </div>
              <div class="flex items-center gap-2">
                <Button size="xs" variant="outline" @click="openLegacyPreview(item.import_id)">
                  {{ legacyPreviewId === item.import_id ? '正在预览' : '预览/导入' }}
                </Button>
                <Button size="xs" variant="ghost" @click="askDeleteLegacyImport(item.import_id)">
                  {{ legacyDeletePending === item.import_id ? '确认删除' : '删除' }}
                </Button>
              </div>
            </div>
          </div>
        </div>

        <div v-if="legacyPreview" class="mt-3 rounded-xl border border-border p-3">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <p class="text-xs font-medium text-foreground">
              账号预览（共 {{ legacyPreview.total }} 个，已选 {{ legacySelectedKeys.length }} 个）
            </p>
            <div class="flex items-center gap-2">
              <input v-model="legacyPreviewKeyword" type="text" placeholder="按邮箱 / 账号 ID 搜索"
                     class="h-7 w-48 rounded-md border border-border bg-card px-2 text-xs" @keyup.enter="loadLegacyPreview(1)" />
              <Button size="xs" variant="ghost" :disabled="legacyPreviewBusy" @click="loadLegacyPreview(1)">搜索</Button>
              <Button size="xs" variant="ghost" @click="selectPageLegacyAccounts">全选本页</Button>
              <Button size="xs" variant="ghost" @click="clearLegacySelection">清空选择</Button>
            </div>
          </div>

          <div class="mt-2 max-h-64 overflow-auto rounded-lg border border-border/70">
            <table class="w-full text-left text-xs">
              <thead class="sticky top-0 bg-muted/60 text-muted-foreground">
                <tr>
                  <th class="w-8 px-2 py-1.5"></th>
                  <th class="px-2 py-1.5 font-medium">邮箱 / ID</th>
                  <th class="px-2 py-1.5 font-medium">状态</th>
                  <th class="px-2 py-1.5 font-medium">Token</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="account in legacyPreview.items" :key="legacyAccountKey(account)" class="border-t border-border/60">
                  <td class="px-2 py-1.5">
                    <input type="checkbox"
                           :disabled="!legacyAccountKey(account)"
                           :checked="legacySelectedKeys.includes(legacyAccountKey(account))"
                           @change="toggleLegacyAccount(account)" />
                  </td>
                  <td class="px-2 py-1.5">
                    <span class="text-foreground">{{ account.email || account.account_id || '（无邮箱）' }}</span>
                    <span v-if="account.email && account.account_id" class="ml-1 font-mono text-[10px] text-muted-foreground">{{ account.account_id }}</span>
                  </td>
                  <td class="px-2 py-1.5 text-muted-foreground">{{ account.status || '—' }}</td>
                  <td class="px-2 py-1.5 font-mono text-[10px] text-muted-foreground">{{ account.token_hint }}</td>
                </tr>
                <tr v-if="!legacyPreview.items.length">
                  <td colspan="4" class="px-2 py-3 text-center text-muted-foreground">没有匹配的账号</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="mt-2 flex items-center justify-between text-xs text-muted-foreground">
            <span>第 {{ legacyPreview.page }} / {{ Math.max(1, Math.ceil(legacyPreview.total / legacyPreview.page_size)) }} 页</span>
            <div class="flex gap-2">
              <Button size="xs" variant="ghost" :disabled="legacyPreviewBusy || legacyPreview.page <= 1" @click="loadLegacyPreview(legacyPreview.page - 1)">上一页</Button>
              <Button size="xs" variant="ghost" :disabled="legacyPreviewBusy || legacyPreview.page * legacyPreview.page_size >= legacyPreview.total" @click="loadLegacyPreview(legacyPreview.page + 1)">下一页</Button>
            </div>
          </div>

          <div class="mt-3 rounded-lg border border-border/70 bg-background p-3">
            <p class="text-[11px] font-medium text-muted-foreground">导入内容</p>
            <div class="mt-2 grid gap-2 text-xs sm:grid-cols-2">
              <label class="flex items-center gap-2">
                <input v-model="legacyImportForm.accountsEnabled" type="checkbox" class="h-3.5 w-3.5" />
                导入账号
                <select v-model="legacyImportForm.accountsMode" :disabled="!legacyImportForm.accountsEnabled"
                        class="ml-auto h-7 rounded-md border border-border bg-card px-1 text-xs">
                  <option value="all">全部</option>
                  <option value="selected">仅已选（{{ legacySelectedKeys.length }}）</option>
                </select>
              </label>
              <label class="flex items-center gap-2">
                <input v-model="legacyImportForm.settingsEnabled" type="checkbox" class="h-3.5 w-3.5" />
                导入设置
                <select v-model="legacyImportForm.settingsMode" :disabled="!legacyImportForm.settingsEnabled"
                        class="ml-auto h-7 rounded-md border border-border bg-card px-1 text-xs">
                  <option value="fill">只补缺（推荐）</option>
                  <option value="overwrite">覆盖当前</option>
                </select>
              </label>
              <label class="flex items-center gap-2">
                <input v-model="legacyImportForm.accountGroups" type="checkbox" class="h-3.5 w-3.5" />
                导入账号组
              </label>
              <label class="flex items-center gap-2">
                <input v-model="legacyImportForm.callRecords" type="checkbox" class="h-3.5 w-3.5" />
                导入调用日志（最多 2000 条）
              </label>
            </div>
            <div class="mt-3 flex flex-wrap items-center gap-2">
              <Button size="xs" variant="outline" :disabled="legacyImportBusy" @click="dryRunLegacyImport">
                {{ legacyImportBusy ? '处理中…' : '预检（不写入）' }}
              </Button>
              <Button size="xs" variant="primary" :disabled="legacyImportBusy" @click="confirmLegacyImport">
                {{ legacyConfirmArmed ? '再次点击确认导入' : '确认导入' }}
              </Button>
              <span v-if="legacyImportMessage" class="text-xs text-muted-foreground">{{ legacyImportMessage }}</span>
            </div>
            <div v-if="legacyDryResult" class="mt-2 text-[11px] text-muted-foreground">
              预检结果：新增账号 {{ legacyDryResult.accounts_added }} · 跳过 {{ legacyDryResult.accounts_skipped }} ·
              设置 {{ legacyDryResult.settings_updated.length }} 项 · 账号组 {{ legacyDryResult.groups_added }} ·
              日志 {{ legacyDryResult.call_records_imported }}
            </div>
            <div v-if="legacyImportResult" class="mt-1 text-[11px] text-emerald-600 dark:text-emerald-400">
              导入完成：新增账号 {{ legacyImportResult.accounts_added }} · 跳过 {{ legacyImportResult.accounts_skipped }} ·
              设置 {{ legacyImportResult.settings_updated.length }} 项 · 账号组 {{ legacyImportResult.groups_added }} ·
              日志 {{ legacyImportResult.call_records_imported }}
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- 凭据体检 -->
    <section class="rounded-2xl border border-border bg-card p-5">
      <header class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 class="text-base font-semibold text-foreground">凭据体检（AT / RT）</h3>
          <p class="mt-1 text-xs text-muted-foreground">
            RT 失效不影响已签发的 AT 继续请求；AT 过期且 RT 失效才是彻底不可用。此处分桶统计并支持清理死号。
          </p>
        </div>
        <Button size="xs" variant="outline" @click="loadCredentialSummary()">刷新</Button>
      </header>

      <div v-if="credentialSummary" class="mt-4 space-y-3">
        <div class="grid gap-2 sm:grid-cols-3">
          <div class="rounded-xl border border-border/70 bg-muted/20 px-3 py-2">
            <p class="text-[11px] text-muted-foreground">可用（AT 有效）</p>
            <p class="text-lg font-semibold text-emerald-600 dark:text-emerald-400">{{ credentialSummary.availability.usable }}</p>
          </div>
          <div class="rounded-xl border border-border/70 bg-muted/20 px-3 py-2">
            <p class="text-[11px] text-muted-foreground">可恢复（AT 失效、RT 可用）</p>
            <p class="text-lg font-semibold text-amber-600 dark:text-amber-400">{{ credentialSummary.availability.recoverable }}</p>
          </div>
          <div class="rounded-xl border border-border/70 bg-muted/20 px-3 py-2">
            <p class="text-[11px] text-muted-foreground">不可用（AT+RT 都失效）</p>
            <p class="text-lg font-semibold text-rose-600 dark:text-rose-400">{{ credentialSummary.availability.unavailable }}</p>
          </div>
        </div>

        <ul class="space-y-1 text-xs text-muted-foreground">
          <li>AT：有效 {{ credentialSummary.access.valid }} · 24h 内到期 {{ credentialSummary.access.expiring }} · 已失效 {{ credentialSummary.access.invalid }}</li>
          <li>RT：有效 {{ credentialSummary.refresh.valid }} · 缺失 {{ credentialSummary.refresh.missing }} · 已失效 {{ credentialSummary.refresh.invalid }}</li>
          <li>总计 {{ credentialSummary.total }} 个账号，其中死号 {{ credentialSummary.cleanup_candidates }} 个</li>
        </ul>

        <div class="flex flex-wrap items-center gap-2">
          <Button
            size="xs"
            :variant="credentialConfirming ? 'primary' : 'outline'"
            :disabled="credentialBusy || !credentialSummary.cleanup_candidates"
            @click="cleanupCredentials"
          >
            {{ credentialConfirming ? `确认删除 ${credentialSummary.cleanup_candidates} 个死号` : '清理死号（预检）' }}
          </Button>
          <Button
            size="xs"
            variant="outline"
            :disabled="memoryRetryBusy"
            @click="retryMemoryDisable"
          >
            {{ memoryRetryBusy ? '提交中...' : '重试失败的记忆关闭' }}
          </Button>
          <span v-if="memoryRetryMessage" class="text-xs text-muted-foreground">{{ memoryRetryMessage }}</span>
          <span v-if="credentialMessage" class="text-xs text-muted-foreground">{{ credentialMessage }}</span>
        </div>

        <div class="rounded-xl border border-border/70 bg-muted/20 p-3">
          <p class="text-xs font-medium text-foreground">低可用告警</p>
          <p class="mt-0.5 text-[11px] text-muted-foreground">
            可用账号或可续期占比低于阈值时写日志并推送 Webhook（同一问题冷却期内只报一次）。
          </p>
          <div class="mt-2 grid gap-2 sm:grid-cols-2">
            <label class="flex items-center gap-2 text-xs text-muted-foreground">
              <input v-model="poolAlertForm.enabled" type="checkbox" class="h-3.5 w-3.5" />
              启用告警
            </label>
            <input
              v-model="poolAlertForm.webhook_url"
              type="url"
              placeholder="Webhook URL（可选）"
              class="rounded-lg border border-border bg-background px-3 py-1.5 text-xs text-foreground outline-none focus:border-primary"
            />
            <label class="flex items-center gap-2 text-xs text-muted-foreground">
              最低可用账号
              <input v-model.number="poolAlertForm.min_usable" type="number" min="0" class="w-24 rounded-lg border border-border bg-background px-2 py-1.5 text-xs text-foreground outline-none focus:border-primary" />
            </label>
            <label class="flex items-center gap-2 text-xs text-muted-foreground">
              最低可续期占比 %
              <input v-model.number="poolAlertForm.min_refresh_ratio_percent" type="number" min="0" max="100" class="w-24 rounded-lg border border-border bg-background px-2 py-1.5 text-xs text-foreground outline-none focus:border-primary" />
            </label>
          </div>
          <div class="mt-2 flex flex-wrap items-center gap-2">
            <Button size="xs" variant="outline" :disabled="poolAlertBusy" @click="savePoolAlerts">保存告警</Button>
            <Button size="xs" variant="ghost" :disabled="poolAlertBusy" @click="runPoolAlerts">立即体检</Button>
            <span v-if="poolAlertMessage" class="text-xs text-muted-foreground">{{ poolAlertMessage }}</span>
          </div>
        </div>

        <div class="rounded-xl border border-border/70 bg-muted/20 p-3">
          <p class="text-xs font-medium text-foreground">自动治理与日志瘦身</p>
          <p class="mt-0.5 text-[11px] text-muted-foreground">
            按间隔处理死号（默认只报告），并按独立保留期清理账号类日志——这类日志量大、价值低。
          </p>
          <div class="mt-2 grid gap-2 sm:grid-cols-2">
            <label class="flex items-center gap-2 text-xs text-muted-foreground">
              <input v-model="governanceForm.enabled" type="checkbox" class="h-3.5 w-3.5" />
              启用定时治理
            </label>
            <select
              v-model="governanceForm.mode"
              class="rounded-lg border border-border bg-background px-2 py-1.5 text-xs text-foreground outline-none focus:border-primary"
            >
              <option value="off">关闭</option>
              <option value="report">只报告</option>
              <option value="disable">禁用死号</option>
              <option value="purge">删除死号</option>
            </select>
            <label class="flex items-center gap-2 text-xs text-muted-foreground">
              治理间隔（小时）
              <input v-model.number="governanceForm.run_every_hours" type="number" min="1" max="720" class="w-20 rounded-lg border border-border bg-background px-2 py-1.5 text-xs text-foreground outline-none focus:border-primary" />
            </label>
            <label class="flex items-center gap-2 text-xs text-muted-foreground">
              账号日志保留（小时）
              <input v-model.number="governanceForm.account_log_retention_hours" type="number" min="1" max="8760" class="w-20 rounded-lg border border-border bg-background px-2 py-1.5 text-xs text-foreground outline-none focus:border-primary" />
            </label>
          </div>
          <div class="mt-2 flex flex-wrap items-center gap-2">
            <Button size="xs" variant="outline" :disabled="governanceBusy" @click="saveGovernance">保存治理</Button>
            <Button size="xs" variant="ghost" :disabled="governanceBusy" @click="runGovernancePreview">立即预检</Button>
            <Button size="xs" variant="ghost" :disabled="upstreamBusy" @click="checkUpstream">上游连通性</Button>
            <span v-if="governanceMessage" class="text-xs text-muted-foreground">{{ governanceMessage }}</span>
          </div>
          <p v-if="upstreamMessage" class="mt-2 text-xs text-muted-foreground">{{ upstreamMessage }}</p>
        </div>
      </div>
      <p v-else class="mt-3 text-xs text-muted-foreground">正在读取凭据状态…</p>
    </section>

    <!-- 本地自动备份与审计 -->
    <section class="grid gap-5 lg:grid-cols-2">
      <div class="rounded-2xl border border-border bg-card p-5">
        <header class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 class="text-base font-semibold text-foreground">本地自动备份</h3>
            <p class="mt-1 text-xs text-muted-foreground">
              每日用 SQLite 备份 API 生成一致快照到 <code class="text-[11px]">data/backups</code>，按保留份数自动轮换。
            </p>
          </div>
          <label class="flex items-center gap-2 text-xs text-muted-foreground">
            <input v-model="backupForm.enabled" type="checkbox" class="h-3.5 w-3.5" />
            启用
          </label>
        </header>
        <div class="mt-3 grid gap-2 sm:grid-cols-2">
          <label class="flex items-center gap-2 text-xs text-muted-foreground">
            间隔（小时）
            <input v-model.number="backupForm.interval_hours" type="number" min="1" max="720" class="w-20 rounded-lg border border-border bg-background px-2 py-1.5 text-xs text-foreground outline-none focus:border-primary" />
          </label>
          <label class="flex items-center gap-2 text-xs text-muted-foreground">
            保留份数
            <input v-model.number="backupForm.keep" type="number" min="1" max="365" class="w-20 rounded-lg border border-border bg-background px-2 py-1.5 text-xs text-foreground outline-none focus:border-primary" />
          </label>
        </div>
        <div class="mt-3 flex flex-wrap items-center gap-2">
          <Button size="xs" variant="outline" :disabled="backupBusy" @click="saveBackup">保存</Button>
          <Button size="xs" variant="ghost" :disabled="backupBusy" @click="runBackup">立即备份</Button>
          <span v-if="backupMessage" class="text-xs text-muted-foreground">{{ backupMessage }}</span>
        </div>
        <ul v-if="backupState?.backups?.length" class="mt-3 space-y-1 text-xs text-muted-foreground">
          <li v-for="item in backupState.backups.slice(0, 5)" :key="item.name" class="flex justify-between gap-3">
            <span class="truncate font-mono text-[11px]">{{ item.name }}</span>
            <span class="shrink-0">{{ (item.size_bytes / 1024 / 1024).toFixed(1) }} MB · {{ item.created_at }}</span>
          </li>
        </ul>
        <p v-else class="mt-3 text-xs text-muted-foreground">还没有本地备份。</p>
      </div>

      <div class="rounded-2xl border border-border bg-card p-5">
        <header class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 class="text-base font-semibold text-foreground">操作审计</h3>
            <p class="mt-1 text-xs text-muted-foreground">
              记录管理员关键操作（批量改状态、删除、死号清理、治理、备份），只含摘要，不含密钥。
            </p>
          </div>
          <Button size="xs" variant="ghost" @click="loadAudit">刷新</Button>
        </header>
        <ul v-if="auditEntries.length" class="scrollbar-slim mt-3 max-h-64 space-y-1.5 overflow-y-auto text-xs">
          <li v-for="(entry, index) in auditEntries" :key="`${entry.at}-${index}`" class="flex items-start gap-2">
            <span class="shrink-0 font-mono text-[11px] text-muted-foreground">{{ entry.at.slice(5) }}</span>
            <span class="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-foreground">{{ entry.action }}</span>
            <span class="min-w-0 flex-1 truncate text-muted-foreground">{{ entry.target || '—' }}</span>
          </li>
        </ul>
        <p v-else class="mt-3 text-xs text-muted-foreground">暂无审计记录。</p>
      </div>
    </section>

    <!-- 调试日志 -->
    <section class="rounded-2xl border border-border bg-card p-5">
      <header class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 class="text-base font-semibold text-foreground">调试日志</h3>
          <p class="mt-1 text-xs text-muted-foreground">
            默认关闭；开启后把完整应用日志写入本地文件，按「保留天数」或「总体积」自动滚动，导出包已脱敏。
          </p>
        </div>
        <label class="flex items-center gap-2 text-xs text-muted-foreground">
          <input v-model="debugLogForm.enabled" type="checkbox" class="h-3.5 w-3.5" />
          启用调试日志
        </label>
      </header>

      <div class="mt-3 grid gap-3 sm:grid-cols-2">
        <label class="flex flex-col gap-1.5">
          <span class="text-xs font-medium text-muted-foreground">保留方式</span>
          <select v-model="debugLogForm.mode" class="rounded-xl border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-primary">
            <option value="time">按时间（保留最近 N 天）</option>
            <option value="size">按体积（保留最新，总量封顶）</option>
          </select>
        </label>

        <label v-if="debugLogForm.mode === 'time'" class="flex flex-col gap-1.5">
          <span class="text-xs font-medium text-muted-foreground">保留天数</span>
          <input v-model.number="debugLogForm.retention_days" type="number" min="1" max="3650" class="rounded-xl border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-primary" />
        </label>

        <template v-else>
          <label class="flex flex-col gap-1.5">
            <span class="text-xs font-medium text-muted-foreground">总量上限（MB）</span>
            <input v-model.number="debugLogForm.max_size_mb" type="number" min="16" max="65536" class="rounded-xl border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-primary" />
          </label>
          <label class="flex flex-col gap-1.5">
            <span class="text-xs font-medium text-muted-foreground">允许上浮（MB）</span>
            <input v-model.number="debugLogForm.slack_mb" type="number" min="0" max="32768" class="rounded-xl border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-primary" />
          </label>
        </template>

        <label class="flex flex-col gap-1.5">
          <span class="text-xs font-medium text-muted-foreground">采集级别</span>
          <select v-model="debugLogForm.level" class="rounded-xl border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-primary">
            <option value="debug">debug（最详细）</option>
            <option value="info">info</option>
          </select>
        </label>
      </div>

      <div class="mt-3 flex flex-wrap items-center gap-2">
        <Button size="xs" variant="primary" :disabled="debugLogBusy" @click="saveDebugLogs">保存</Button>
        <Button size="xs" variant="outline" :disabled="debugLogBusy" @click="exportDebugLogs">下载调试包</Button>
        <Button size="xs" variant="ghost" :disabled="debugLogBusy" @click="clearDebugLogs">清空日志</Button>
        <span v-if="debugLogMessage" class="text-xs text-muted-foreground">{{ debugLogMessage }}</span>
      </div>

      <p v-if="debugLogStatus" class="mt-2 text-xs text-muted-foreground">
        {{ debugLogStatus.active ? '采集中' : '未启用' }} ·
        {{ debugLogStatus.file_count }} 个文件 · {{ (debugLogStatus.total_bytes / 1024 / 1024).toFixed(1) }} MB
        （预算 {{ (debugLogStatus.budget_bytes / 1024 / 1024).toFixed(0) }} MB + {{ (debugLogStatus.slack_bytes / 1024 / 1024).toFixed(0) }} MB）·
        目录 <code class="text-[11px]">{{ debugLogStatus.directory }}</code>
      </p>
    </section>

    <!-- 主题 -->
    <section class="rounded-2xl border border-border bg-card p-5">
      <header>
        <h3 class="text-base font-semibold text-foreground">界面主题</h3>
        <p class="mt-1 text-xs text-muted-foreground">五套完全独立的视觉体系，一键切换，立即生效。</p>
      </header>
      <div class="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
        <button
          v-for="option in paletteOptions"
          :key="option.id"
          type="button"
          class="rounded-xl border p-3 text-left transition-colors"
          :class="currentPalette === option.id ? 'border-primary bg-primary/5' : 'border-border bg-background hover:border-foreground/30'"
          @click="selectPalette(option.id)"
        >
          <span class="flex items-center gap-1">
            <span v-for="swatch in option.swatches" :key="swatch" class="h-3 w-3 rounded-full border border-black/10" :style="{ backgroundColor: swatch }"></span>
          </span>
          <span class="mt-2 block text-xs font-semibold text-foreground">{{ option.label }}</span>
          <span class="mt-0.5 block text-[11px] leading-4 text-muted-foreground">{{ option.description }}</span>
        </button>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { Button } from 'nanocat-ui'

import { LegacyUploadError, systemOpsApi, type AccountQueueView, type AuditEntry, type DatabaseMaintenanceResult, type DatabaseMaintenanceStatus, type DebugLogStatus, type HardwareTier, type LegacyImportAccount, type LegacyImportOptions, type LegacyImportResult, type LegacyImportSummary, type LegacySyncConfig, type LegacySyncState, type LocalBackupStatus, type ProxyHealthView, type SchedulerMode, type SchedulerPolicy, type SchedulerSnapshot } from '@/api/systemOps'
import { accountsApi, type AccountCredentialSummary } from '@/api/accounts'
import MetaChip from '@/components/ai/MetaChip.vue'
import { useToast } from '@/composables/useToast'
import { getStoredPalette, PALETTE_OPTIONS, setStoredPalette, type PaletteId } from '@/lib/theme'
import { saveBlob } from '@/lib/downloads'

const toast = useToast()

const scheduler = ref<SchedulerSnapshot | null>(null)
const schedulerLoading = ref(false)
const schedulerError = ref('')
const policySaving = ref(false)
const policyMessage = ref('')
const database = ref<DatabaseMaintenanceStatus | null>(null)
const maintenanceBusy = ref(false)
const maintenanceResult = ref<DatabaseMaintenanceResult | null>(null)
const queue = ref<AccountQueueView | null>(null)
const queueLoading = ref(false)
const queueUpdatedAt = ref(0)
const proxyHealth = ref<ProxyHealthView | null>(null)
const probing = ref(false)
const currentPalette = ref<PaletteId>(getStoredPalette())
const legacyState = ref<LegacySyncState | null>(null)
const legacyBusy = ref(false)
const legacyMessage = ref('')
const legacyPathsText = ref('')
const credentialSummary = ref<AccountCredentialSummary | null>(null)
const credentialBusy = ref(false)
const memoryRetryBusy = ref(false)
const memoryRetryMessage = ref('')
const credentialConfirming = ref(false)
const credentialMessage = ref('')
const poolAlertBusy = ref(false)
const poolAlertMessage = ref('')
const poolAlertForm = reactive({
  enabled: false,
  webhook_url: '',
  min_usable: 1,
  min_refresh_ratio_percent: 10,
})
const governanceBusy = ref(false)
const governanceMessage = ref('')
const upstreamBusy = ref(false)
const upstreamMessage = ref('')
const governanceForm = reactive({
  enabled: false,
  mode: 'report' as 'off' | 'report' | 'disable' | 'purge',
  run_every_hours: 24,
  account_log_retention_hours: 168,
})
const backupBusy = ref(false)
const backupMessage = ref('')
const backupState = ref<LocalBackupStatus | null>(null)
const auditEntries = ref<AuditEntry[]>([])
const backupForm = reactive({
  enabled: false,
  interval_hours: 24,
  keep: 7,
})
const debugLogBusy = ref(false)
const debugLogMessage = ref('')
const debugLogStatus = ref<DebugLogStatus | null>(null)
const debugLogForm = reactive({
  enabled: false,
  mode: 'time' as 'time' | 'size',
  retention_days: 30,
  max_size_mb: 1024,
  slack_mb: 100,
  level: 'debug' as 'debug' | 'info',
})
const legacyForm = reactive<LegacySyncConfig>({
  enabled: false,
  paths: [],
  interval_seconds: 300,
  sync_accounts: true,
  sync_settings: 'fill',
  sync_account_groups: true,
  sync_call_records: true,
  max_call_records_per_run: 2000,
  auto_check: true,
  check_per_run: 100,
})

// ---- 旧数据库文件导入（本地上传）----
const legacyFileInput = ref<HTMLInputElement | null>(null)
const legacyFile = ref<File | null>(null)
const legacyUploading = ref(false)
const legacyUploadMessage = ref('')
// 上传进度：idle → sending（发请求体） → parsing（服务端解析） → done / error / cancelled
const legacyUploadPhase = ref<'idle' | 'sending' | 'parsing' | 'done' | 'cancelled' | 'error'>('idle')
const legacyUploadError = ref('')
const legacyUploadProgress = reactive({ loaded: 0, total: 0, percent: 0, speed: 0 })
let legacyUploadController: AbortController | null = null
const legacyImports = ref<LegacyImportSummary[]>([])
const legacyPreview = ref<{
  items: LegacyImportAccount[]
  total: number
  page: number
  page_size: number
  import_id: string
} | null>(null)
const legacyPreviewId = ref('')
const legacyPreviewBusy = ref(false)
const legacyPreviewKeyword = ref('')
const legacySelectedKeys = ref<string[]>([])
const legacyImportBusy = ref(false)
const legacyImportMessage = ref('')
const legacyDryResult = ref<LegacyImportResult | null>(null)
const legacyImportResult = ref<LegacyImportResult | null>(null)
const legacyConfirmArmed = ref(false)
const legacyDeletePending = ref('')
const legacyClearPending = ref(false)
const legacyImportForm = reactive({
  accountsEnabled: true,
  accountsMode: 'all' as 'all' | 'selected',
  settingsEnabled: true,
  settingsMode: 'fill' as 'fill' | 'overwrite',
  accountGroups: true,
  callRecords: false,
})
let pollTimer: number | null = null

// ---- 智能自适应调度：策略草稿 ----
// 数字输入框在编辑过程中可能是空串或字符串（v-model.number 对空值返回 ''），
// 所以草稿按 number | string 存储，保存前统一收敛成整数并校验，绝不把空值 / NaN 提交上去。
type PolicyNumberField =
  | 'cpu_high'
  | 'mem_high'
  | 'cpu_low'
  | 'mem_low'
  | 'restore_hold_seconds'
  | 'sample_interval_seconds'
  | 'min_scale_percent'

type PolicyNumberSpec = {
  key: PolicyNumberField
  label: string
  min: number
  max: number
}

type PolicyDraft = Omit<SchedulerPolicy, PolicyNumberField> & Record<PolicyNumberField, number | string>

type PolicyValidation =
  | { ok: true; payload: SchedulerPolicy }
  | { ok: false; error: string; fields: PolicyNumberField[] }

/** 与后端 contracts/settings.py::AdaptiveSchedulerPatch 的范围保持一致。 */
const policyFields: PolicyNumberSpec[] = [
  { key: 'cpu_high', label: 'CPU 过载', min: 10, max: 100 },
  { key: 'mem_high', label: '内存过载', min: 10, max: 100 },
  { key: 'cpu_low', label: 'CPU 恢复', min: 5, max: 99 },
  { key: 'mem_low', label: '内存恢复', min: 5, max: 99 },
  { key: 'restore_hold_seconds', label: '回弹保持(s)', min: 10, max: 86400 },
  { key: 'sample_interval_seconds', label: '采样间隔(s)', min: 1, max: 300 },
  { key: 'min_scale_percent', label: '最低比例(%)', min: 5, max: 100 },
]

const policy = reactive<PolicyDraft>({
  enabled: true,
  mode: 'auto',
  cpu_high: 85,
  mem_high: 90,
  cpu_low: 70,
  mem_low: 80,
  restore_hold_seconds: 300,
  sample_interval_seconds: 5,
  min_scale_percent: 20,
  hardware_override: 'auto',
})
const policyDirty = ref(false)
const policyError = ref('')
const policyErrorFields = ref<PolicyNumberField[]>([])
const modeSwitching = ref(false)
/** 单调递增的请求序号：过期响应（旧的轮询 / 保存 / 切换模式）一律不回写界面。 */
let schedulerRequestSeq = 0
/** 只用于“加载中”指示，避免被保存请求提前关掉或被过期轮询卡住。 */
let schedulerLoadToken = 0

/** 顶部说明只展示已落库的回弹保持时间，草稿为空串时不会渲染成空值。 */
const savedRestoreHoldSeconds = computed(() => {
  const value = scheduler.value?.watermarks.restore_hold_seconds
  return typeof value === 'number' && Number.isFinite(value) ? value : policy.restore_hold_seconds
})

function readPolicyNumber(raw: unknown): number | null {
  if (raw === null || raw === undefined) return null
  const text = String(raw).trim()
  if (!text) return null
  const value = Number(text)
  return Number.isFinite(value) ? value : null
}

function validatePolicyDraft(): PolicyValidation {
  const numbers = {} as Record<PolicyNumberField, number>
  for (const field of policyFields) {
    const parsed = readPolicyNumber(policy[field.key])
    if (parsed === null) {
      return { ok: false, error: `${field.label}不能为空，请输入 ${field.min}-${field.max} 的整数`, fields: [field.key] }
    }
    const value = Math.round(parsed)
    if (value < field.min || value > field.max) {
      return { ok: false, error: `${field.label}需在 ${field.min}-${field.max} 之间，当前 ${value}`, fields: [field.key] }
    }
    numbers[field.key] = value
  }
  if (numbers.cpu_low >= numbers.cpu_high) {
    return {
      ok: false,
      error: `CPU 恢复阈值（${numbers.cpu_low}%）必须小于过载阈值（${numbers.cpu_high}%）`,
      fields: ['cpu_low'],
    }
  }
  if (numbers.mem_low >= numbers.mem_high) {
    return {
      ok: false,
      error: `内存恢复阈值（${numbers.mem_low}%）必须小于过载阈值（${numbers.mem_high}%）`,
      fields: ['mem_low'],
    }
  }
  return {
    ok: true,
    payload: {
      enabled: policy.enabled,
      mode: policy.mode,
      hardware_override: policy.hardware_override,
      cpu_high: numbers.cpu_high,
      mem_high: numbers.mem_high,
      cpu_low: numbers.cpu_low,
      mem_low: numbers.mem_low,
      restore_hold_seconds: numbers.restore_hold_seconds,
      sample_interval_seconds: numbers.sample_interval_seconds,
      min_scale_percent: numbers.min_scale_percent,
    },
  }
}

function fieldInvalid(key: PolicyNumberField) {
  return policyErrorFields.value.includes(key)
}

function markPolicyDirty() {
  policyDirty.value = true
  policyError.value = ''
  policyErrorFields.value = []
}

/** 失焦时把 85.7 这类小数收敛成整数，空值保持为空等保存时统一报错。 */
function normalizePolicyField(key: PolicyNumberField) {
  const parsed = readPolicyNumber(policy[key])
  if (parsed === null) return
  const rounded = Math.round(parsed)
  if (rounded !== parsed) policy[key] = rounded
}

const paletteOptions = PALETTE_OPTIONS
const modeOptions = [
  { value: 'auto' as const, label: '智能自动' },
  { value: 'manual' as const, label: '手动锁定' },
]
const tierOptions: Array<{ value: HardwareTier; label: string }> = [
  { value: 'auto', label: '自动' },
  { value: 'low', label: '低配' },
  { value: 'medium', label: '中配' },
  { value: 'high', label: '高配' },
  { value: 'flagship', label: '旗舰' },
]
const maintenanceActions = [
  { value: 'check', label: '健康检查' },
  { value: 'optimize', label: '优化统计' },
  { value: 'reindex', label: '重建索引' },
  { value: 'compact', label: '压缩空间' },
  { value: 'repair', label: '一键修复' },
]
const knobLabels: Record<string, string> = {
  account_processing_concurrency: '账号处理并发',
  account_check_workers: '检测队列线程',
  worker_threads: '异步线程池',
  image_task_workers: '出图任务线程',
}

const knobRows = computed(() => {
  if (!scheduler.value) return []
  return Object.keys(scheduler.value.effective).map((key) => ({
    key,
    label: knobLabels[key] || key,
    base: scheduler.value!.base[key] ?? 0,
    effective: scheduler.value!.effective[key] ?? 0,
  }))
})
const recentEvents = computed(() => (scheduler.value?.events || []).slice(-6).reverse())
const levelTone = computed(() => {
  const level = scheduler.value?.level
  if (level === 'critical') return 'danger' as const
  if (level === 'warning') return 'warning' as const
  return 'success' as const
})
const levelLabel = computed(() => {
  const level = scheduler.value?.level
  if (level === 'critical') return '过载保护中'
  if (level === 'warning') return '负载偏高'
  return '运行平稳'
})

function formatPercent(value: number | null | undefined) {
  return value === null || value === undefined ? '--' : `${Number(value).toFixed(1)}%`
}

function formatBytes(value: number | null | undefined) {
  const bytes = Number(value || 0)
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = bytes
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size.toFixed(size >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`
}

function eventLabel(event: string) {
  if (event === 'downgrade') return '智能降配'
  if (event === 'restore') return '精准回弹'
  if (event === 'pinned') return '固定参数'
  return event
}

function stateLabel(state: string) {
  if (state === 'healthy') return '健康'
  if (state === 'degraded') return '观察'
  if (state === 'down') return '熔断'
  return '未知'
}

function stateTone(state: string) {
  if (state === 'healthy') return 'success' as const
  if (state === 'degraded') return 'warning' as const
  if (state === 'down') return 'danger' as const
  return 'muted' as const
}

function stateTextClass(state: string) {
  if (state === 'healthy') return 'text-emerald-600 dark:text-emerald-400'
  if (state === 'degraded') return 'text-amber-600 dark:text-amber-400'
  if (state === 'down') return 'text-rose-600 dark:text-rose-400'
  return 'text-muted-foreground'
}

/**
 * 只同步“不是输入框”的两个字段。
 * 模式和启用开关由 switchMode / 服务端持有，把它们留在草稿里再整包保存，
 * 会把别处刚改过的模式覆盖回旧值。
 */
function syncPolicyMetaFromSnapshot(snapshot: SchedulerSnapshot) {
  policy.enabled = snapshot.enabled
  policy.mode = snapshot.mode
}

function syncPolicyFromSnapshot(snapshot: SchedulerSnapshot) {
  syncPolicyMetaFromSnapshot(snapshot)
  policy.cpu_high = snapshot.watermarks.cpu_high
  policy.mem_high = snapshot.watermarks.mem_high
  policy.cpu_low = snapshot.watermarks.cpu_low
  policy.mem_low = snapshot.watermarks.mem_low
  policy.restore_hold_seconds = snapshot.watermarks.restore_hold_seconds
  policy.sample_interval_seconds = snapshot.watermarks.sample_interval_seconds
  policy.min_scale_percent = snapshot.min_scale_percent
  policy.hardware_override = snapshot.hardware.hardware_override
  policyDirty.value = false
  policyError.value = ''
  policyErrorFields.value = []
}

async function loadScheduler(force = false) {
  // 保存 / 切换模式期间不拉取策略，避免竞态把刚提交的值再抹回去。
  if (policySaving.value || modeSwitching.value) return
  const token = ++schedulerLoadToken
  const seq = ++schedulerRequestSeq
  schedulerLoading.value = true
  try {
    const snapshot = await systemOpsApi.scheduler()
    if (seq !== schedulerRequestSeq) return
    scheduler.value = snapshot
    schedulerError.value = ''
    if (policyDirty.value && !force) {
      // 未保存的阈值原样保留；只把模式 / 启用开关跟服务端对齐。
      syncPolicyMetaFromSnapshot(snapshot)
      policyMessage.value = '有未保存的修改，已保留你的输入；点「重新载入」可放弃修改'
    } else {
      syncPolicyFromSnapshot(snapshot)
      if (force) policyMessage.value = '已重新载入服务端当前策略'
    }
  } catch (error) {
    if (seq !== schedulerRequestSeq) return
    schedulerError.value = error instanceof Error ? error.message : '读取调度状态失败'
  } finally {
    if (token === schedulerLoadToken) schedulerLoading.value = false
  }
}

async function switchMode(mode: SchedulerMode) {
  if (!scheduler.value || scheduler.value.mode === mode || modeSwitching.value || policySaving.value) return
  const seq = ++schedulerRequestSeq
  modeSwitching.value = true
  try {
    const snapshot = await systemOpsApi.setSchedulerMode(mode)
    if (seq === schedulerRequestSeq) {
      scheduler.value = { ...snapshot, mode }
      if (policyDirty.value) {
        // 切模式只换运行方式：未保存的阈值必须原样留着。
        policy.mode = mode
      } else {
        syncPolicyFromSnapshot({ ...snapshot, mode })
      }
    }
    toast.success(mode === 'auto' ? '已开启智能自动调度' : '已切换为手动锁定')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '切换失败')
  } finally {
    modeSwitching.value = false
  }
}

async function savePolicy() {
  if (policySaving.value || modeSwitching.value) return
  const validation = validatePolicyDraft()
  if (!validation.ok) {
    // 校验不通过就停在前端：不把空值 / NaN / 越界值送去后端。
    policyError.value = validation.error
    policyErrorFields.value = validation.fields
    policyMessage.value = ''
    toast.error(validation.error)
    return
  }
  policyError.value = ''
  policyErrorFields.value = []
  policySaving.value = true
  const seq = ++schedulerRequestSeq
  try {
    const snapshot = await systemOpsApi.saveSchedulerPolicy(validation.payload)
    if (seq === schedulerRequestSeq) scheduler.value = snapshot
    syncPolicyFromSnapshot(snapshot)
    policyMessage.value = '策略已保存'
    toast.success('调度策略已保存')
  } catch (error) {
    const message = error instanceof Error ? error.message : '保存失败'
    policyError.value = message
    policyErrorFields.value = []
    policyMessage.value = ''
    toast.error(message)
  } finally {
    policySaving.value = false
  }
}

async function loadDatabase() {
  try {
    database.value = await systemOpsApi.database()
  } catch {
    database.value = null
  }
}

async function runMaintenance(operation: string) {
  maintenanceBusy.value = true
  try {
    maintenanceResult.value = await systemOpsApi.runDatabaseMaintenance([operation])
    await loadDatabase()
    toast.success(maintenanceResult.value.ok ? '数据库维护完成' : '维护完成，存在失败项')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '维护失败')
  } finally {
    maintenanceBusy.value = false
  }
}

async function loadQueue() {
  // A slow /api/system/queue must not stack requests every 5s; stacking is
  // what made the task list look frozen while the UI waited on older polls.
  if (queueLoading.value) return
  queueLoading.value = true
  try {
    queue.value = await systemOpsApi.queue()
    queueUpdatedAt.value = Date.now()
  } catch {
    queue.value = null
  } finally {
    queueLoading.value = false
  }
}

async function cancelJob(jobId: string) {
  try {
    await systemOpsApi.cancelJob(jobId)
    await loadQueue()
    toast.success('任务已取消')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '取消失败')
  }
}

async function loadProxyHealth() {
  try {
    proxyHealth.value = await systemOpsApi.proxyHealth()
  } catch {
    proxyHealth.value = null
  }
}

async function probeProxy() {
  probing.value = true
  try {
    await systemOpsApi.probeProxyHealth()
    await loadProxyHealth()
    toast.success('代理探测完成')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '探测失败')
  } finally {
    probing.value = false
  }
}

function applyLegacyState(state: LegacySyncState) {
  legacyState.value = state
  if (!state?.config) return
  Object.assign(legacyForm, state.config)
  legacyPathsText.value = (state.config.paths || []).join('\n')
}

async function loadLegacySync() {
  try {
    applyLegacyState(await systemOpsApi.legacySync())
  } catch {
    legacyState.value = null
  }
}

function formatLegacyBytes(value: number) {
  if (!value) return '0 B'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function formatDuration(seconds: number) {
  if (!Number.isFinite(seconds) || seconds <= 0) return '0 秒'
  const totalSeconds = Math.max(1, Math.round(seconds))
  if (totalSeconds < 60) return `${totalSeconds} 秒`
  const minutes = Math.floor(totalSeconds / 60)
  const rest = totalSeconds % 60
  if (minutes < 60) return `${minutes} 分 ${rest} 秒`
  const hours = Math.floor(minutes / 60)
  return `${hours} 小时 ${minutes % 60} 分`
}

function truncateUploadDetail(value: string, max = 320) {
  const text = (value || '').replace(/\s+/g, ' ').trim()
  return text.length > max ? `${text.slice(0, max)}…` : text
}

/**
 * JSON 错误体已经被 readUploadErrorMessage 抽成可读文案，优先展示它；
 * Nginx 的 HTML 错误页没有可读 detail，就原样（截断后）展示，方便复制给运维。
 */
function describeLegacyUploadFailure(error: LegacyUploadError, responseText: string) {
  const looksJson = /^[[{]/.test(responseText)
  if (looksJson && error.message && error.message !== responseText) return error.message
  return responseText || error.message || '服务端未返回错误详情'
}

function resetLegacyUploadState() {
  legacyUploadPhase.value = 'idle'
  legacyUploadError.value = ''
  legacyUploadProgress.loaded = 0
  legacyUploadProgress.total = 0
  legacyUploadProgress.percent = 0
  legacyUploadProgress.speed = 0
}

function cancelLegacyUpload() {
  legacyUploadController?.abort()
}

const legacyUploadPercentLabel = computed(() => {
  const phase = legacyUploadPhase.value
  if (phase === 'parsing' || phase === 'done') return '100%'
  return `${Math.min(100, Math.max(0, Math.round(legacyUploadProgress.percent)))}%`
})

const legacyUploadPhaseLabel = computed(() => {
  switch (legacyUploadPhase.value) {
    case 'sending':
      return '正在上传'
    case 'parsing':
      return '已传完，服务端解析中'
    case 'done':
      return '上传完成'
    case 'cancelled':
      return '已取消上传'
    case 'error':
      return '上传失败'
    default:
      return ''
  }
})

const legacyUploadBarPercent = computed(() => {
  const phase = legacyUploadPhase.value
  if (phase === 'parsing' || phase === 'done') return 100
  return Math.min(100, Math.max(0, legacyUploadProgress.percent))
})

const legacyUploadBarClass = computed(() => {
  switch (legacyUploadPhase.value) {
    case 'done':
      return 'bg-emerald-500'
    case 'error':
      return 'bg-rose-500'
    case 'cancelled':
      return 'bg-amber-500'
    default:
      return 'bg-primary'
  }
})

const legacyUploadEtaLabel = computed(() => {
  const { loaded, total, speed } = legacyUploadProgress
  if (total <= 0 || speed <= 0) return ''
  const remaining = total - loaded
  if (remaining <= 0) return '即将完成'
  return `剩余约 ${formatDuration(remaining / speed)}`
})

function onLegacyFileSelected(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] || null
  legacyFile.value = file
  resetLegacyUploadState()
  legacyUploadMessage.value = file ? `已选择：${file.name}（${formatLegacyBytes(file.size)}）` : ''
  input.value = ''
}

async function loadLegacyImports() {
  try {
    legacyImports.value = (await systemOpsApi.listLegacyImports()).items
  } catch {
    legacyImports.value = []
  }
}

async function uploadLegacyDb() {
  if (!legacyFile.value || legacyUploading.value) return
  const file = legacyFile.value
  const controller = new AbortController()
  legacyUploadController = controller
  legacyUploading.value = true
  legacyUploadPhase.value = 'sending'
  legacyUploadError.value = ''
  legacyUploadProgress.loaded = 0
  legacyUploadProgress.total = file.size
  legacyUploadProgress.percent = 0
  legacyUploadProgress.speed = 0
  legacyUploadMessage.value = `正在上传 ${file.name}（${formatLegacyBytes(file.size)}）…`
  try {
    const summary = await systemOpsApi.uploadLegacyDatabase(file, {
      signal: controller.signal,
      onProgress: (progress) => {
        legacyUploadProgress.loaded = progress.loaded
        legacyUploadProgress.total = progress.total > 0 ? progress.total : file.size
        legacyUploadProgress.percent = progress.percent < 0 ? 0 : progress.percent
        legacyUploadProgress.speed = progress.bytes_per_second
        // 请求体发完了但服务端还在解析 300MB 的 SQLite：进度条停在 100%，文案要继续说明。
        if (progress.percent >= 100 && legacyUploadPhase.value === 'sending') {
          legacyUploadPhase.value = 'parsing'
          legacyUploadMessage.value = '文件已发送完毕，服务端正在解析数据库…'
        }
      },
    })
    // 先落到 100% 再刷新列表，避免「上传成功」提示出现时进度条还停在 97%。
    legacyUploadProgress.loaded = legacyUploadProgress.total || file.size
    legacyUploadProgress.percent = 100
    legacyUploadPhase.value = 'done'
    legacyFile.value = null
    legacyUploadMessage.value = `上传成功：账号 ${summary.accounts} 个，设置 ${summary.settings_keys} 项`
    legacyUploading.value = false // 解析已完成，列表刷新期间不必再禁用按钮
    await loadLegacyImports()
    await openLegacyPreview(summary.import_id)
    toast.success('数据库已上传，可预览并选择导入')
  } catch (error) {
    if (error instanceof LegacyUploadError && error.aborted) {
      legacyUploadPhase.value = 'cancelled'
      legacyUploadError.value = ''
      legacyUploadMessage.value = `已取消上传（已传 ${formatLegacyBytes(legacyUploadProgress.loaded)}）`
      toast.info('已取消上传')
      return // 取消不刷新列表
    }
    const uploadError = error instanceof LegacyUploadError ? error : null
    const status = uploadError?.status ?? 0
    const responseText = truncateUploadDetail(uploadError?.response_text || '')
    const reason = uploadError?.message || (error instanceof Error ? error.message : '上传失败')
    legacyUploadPhase.value = 'error'
    if (status === 413 || /413|request entity too large|client_max_body_size/i.test(responseText)) {
      legacyUploadMessage.value = '上传失败：文件超过反向代理限制（HTTP 413）'
      const detail = responseText
        ? (uploadError ? describeLegacyUploadFailure(uploadError, responseText) : responseText)
        : ''
      legacyUploadError.value = [
        `状态码：${status || 413}`,
        detail ? `服务端响应：${detail}` : '',
        '文件超过反向代理限制，请在 Nginx 调大 client_max_body_size（部署模板见 deploy/nginx-qingmeng.conf，其中为 2048m），改完执行 nginx -t && nginx -s reload 再重试。',
        '如果拿不到 Nginx 配置，也可以跳过上传：用上方「旧项目持久化目录」按服务器本地路径直接同步。',
      ].filter(Boolean).join('\n')
      toast.error('文件超过反向代理限制（HTTP 413）')
      return
    }
    if (status === 0 && !responseText) {
      legacyUploadMessage.value = '上传失败：网络中断或代理连接被重置'
      legacyUploadError.value = reason
      toast.error(legacyUploadMessage.value)
      return
    }
    legacyUploadMessage.value = `上传失败（HTTP ${status || '未知'}）`
    legacyUploadError.value = uploadError
      ? describeLegacyUploadFailure(uploadError, responseText)
      : reason || '服务端未返回错误详情'
    toast.error(legacyUploadMessage.value)
  } finally {
    legacyUploading.value = false
    legacyUploadController = null
  }
}

async function openLegacyPreview(importId: string) {
  legacyPreviewId.value = importId
  legacyPreviewKeyword.value = ''
  legacySelectedKeys.value = []
  legacyDryResult.value = null
  legacyImportResult.value = null
  legacyConfirmArmed.value = false
  legacyImportMessage.value = ''
  await loadLegacyPreview(1)
}

async function loadLegacyPreview(page = 1) {
  if (!legacyPreviewId.value) return
  legacyPreviewBusy.value = true
  try {
    legacyPreview.value = await systemOpsApi.previewLegacyAccounts(legacyPreviewId.value, {
      page,
      page_size: 20,
      keyword: legacyPreviewKeyword.value.trim(),
    })
  } catch (error) {
    legacyPreview.value = null
    toast.error(error instanceof Error ? error.message : '读取账号预览失败')
  } finally {
    legacyPreviewBusy.value = false
  }
}

function legacyAccountKey(account: LegacyImportAccount) {
  return account.account_id || account.email || account.token_hint
}

function toggleLegacyAccount(account: LegacyImportAccount) {
  const key = legacyAccountKey(account)
  if (!key) return
  const index = legacySelectedKeys.value.indexOf(key)
  if (index >= 0) legacySelectedKeys.value.splice(index, 1)
  else legacySelectedKeys.value.push(key)
}

function selectPageLegacyAccounts() {
  for (const account of legacyPreview.value?.items || []) {
    const key = legacyAccountKey(account)
    if (key && !legacySelectedKeys.value.includes(key)) legacySelectedKeys.value.push(key)
  }
}

function clearLegacySelection() {
  legacySelectedKeys.value = []
}

function buildLegacyOptions(): LegacyImportOptions {
  const accountIds: string[] = []
  const emails: string[] = []
  for (const key of legacySelectedKeys.value) {
    if (key.includes('@')) emails.push(key)
    else accountIds.push(key)
  }
  return {
    accounts: {
      enabled: legacyImportForm.accountsEnabled,
      mode: legacyImportForm.accountsMode,
      account_ids: legacyImportForm.accountsMode === 'selected' ? accountIds : [],
      emails: legacyImportForm.accountsMode === 'selected' ? emails : [],
    },
    settings: {
      enabled: legacyImportForm.settingsEnabled,
      mode: legacyImportForm.settingsMode,
      keys: [],
    },
    account_groups: legacyImportForm.accountGroups,
    call_records: legacyImportForm.callRecords,
    max_call_records: 2000,
  }
}

async function dryRunLegacyImport() {
  if (!legacyPreviewId.value) return
  legacyImportBusy.value = true
  legacyImportMessage.value = '预检中…'
  legacyDryResult.value = null
  legacyImportResult.value = null
  legacyConfirmArmed.value = false
  try {
    const { result } = await systemOpsApi.importLegacySelection(
      legacyPreviewId.value,
      buildLegacyOptions(),
      true,
      false,
    )
    legacyDryResult.value = result
    legacyImportMessage.value = `预检完成：将新增账号 ${result.accounts_added} 个，设置 ${result.settings_updated.length} 项，账号组 ${result.groups_added} 个`
    toast.success('预检完成，确认无误后再执行导入')
  } catch (error) {
    legacyImportMessage.value = error instanceof Error ? error.message : '预检失败'
    toast.error(legacyImportMessage.value)
  } finally {
    legacyImportBusy.value = false
  }
}

async function confirmLegacyImport() {
  if (!legacyPreviewId.value) return
  if (!legacyConfirmArmed.value) {
    legacyConfirmArmed.value = true
    legacyImportMessage.value = '这是写库操作：再次点击「确认导入」才会执行'
    return
  }
  legacyImportBusy.value = true
  legacyImportMessage.value = '导入中…'
  try {
    const { result } = await systemOpsApi.importLegacySelection(
      legacyPreviewId.value,
      buildLegacyOptions(),
      false,
      true,
    )
    legacyImportResult.value = result
    legacyImportMessage.value = `导入完成：新增账号 ${result.accounts_added} 个，跳过 ${result.accounts_skipped} 个，设置 ${result.settings_updated.length} 项`
    legacyDryResult.value = null
    legacyConfirmArmed.value = false
    await loadLegacyImports()
    await loadLegacyPreview(legacyPreview.value?.page || 1)
    await loadCredentialSummary()
    toast.success('导入完成')
  } catch (error) {
    legacyImportMessage.value = error instanceof Error ? error.message : '导入失败'
    toast.error(legacyImportMessage.value)
  } finally {
    legacyImportBusy.value = false
  }
}

async function askDeleteLegacyImport(importId: string) {
  if (legacyDeletePending.value !== importId) {
    legacyDeletePending.value = importId
    legacyImportMessage.value = '再次点击「确认删除」将删除这个上传文件'
    return
  }
  legacyDeletePending.value = ''
  try {
    await systemOpsApi.deleteLegacyImport(importId)
    if (legacyPreviewId.value === importId) {
      legacyPreviewId.value = ''
      legacyPreview.value = null
    }
    await loadLegacyImports()
    legacyImportMessage.value = '上传文件已删除'
    toast.success('已删除上传文件')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '删除失败')
  }
}

async function clearLegacyImports() {
  if (!legacyClearPending.value) {
    legacyClearPending.value = true
    legacyImportMessage.value = '再次点击「确认清空全部」将删除所有已上传旧数据库'
    return
  }
  legacyClearPending.value = false
  try {
    const result = await systemOpsApi.deleteAllLegacyImports()
    legacyPreviewId.value = ''
    legacyPreview.value = null
    await loadLegacyImports()
    legacyImportMessage.value = `已清空 ${result.removed} 个旧数据库`
    toast.success(`已清空 ${result.removed} 个旧数据库`)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '清空失败')
  }
}

async function loadCredentialSummary() {
  try {
    credentialSummary.value = await accountsApi.credentialSummary()
  } catch {
    credentialSummary.value = null
  }
}

async function retryMemoryDisable() {
  memoryRetryBusy.value = true
  memoryRetryMessage.value = ''
  try {
    const result = await accountsApi.retryMemoryDisable()
    memoryRetryMessage.value = result.targets
      ? `已提交 ${result.targets} 个失败账号，progress ${result.progress_id.slice(0, 8)}`
      : '没有需要重试的失败账号'
    toast.success(memoryRetryMessage.value)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '提交重试失败')
  } finally {
    memoryRetryBusy.value = false
  }
}

async function cleanupCredentials() {
  credentialBusy.value = true
  credentialMessage.value = ''
  try {
    if (!credentialConfirming.value) {
      const preview = await accountsApi.credentialCleanup(true)
      credentialConfirming.value = true
      credentialMessage.value = `预检：将删除 ${preview.candidates} 个死号，再点一次确认执行`
      return
    }
    const result = await accountsApi.credentialCleanup(false)
    credentialConfirming.value = false
    credentialMessage.value = `已删除 ${result.removed || 0} 个死号`
    credentialSummary.value = result.summary
    toast.success(credentialMessage.value)
  } catch (error) {
    credentialConfirming.value = false
    toast.error(error instanceof Error ? error.message : '清理失败')
  } finally {
    credentialBusy.value = false
  }
}

async function loadPoolAlerts() {
  try {
    const status = await systemOpsApi.poolAlerts()
    Object.assign(poolAlertForm, {
      enabled: status.config.enabled,
      webhook_url: status.config.webhook_url,
      min_usable: status.config.min_usable,
      min_refresh_ratio_percent: status.config.min_refresh_ratio_percent,
    })
    const report = status.last_report || {}
    if (report.warnings?.length) {
      poolAlertMessage.value = `上次体检：${report.warnings.join('；')}`
    } else if (report.checked_at) {
      poolAlertMessage.value = `上次体检 ${report.checked_at}：正常`
    }
  } catch {
    /* 告警为可选功能 */
  }
}

async function savePoolAlerts() {
  poolAlertBusy.value = true
  try {
    const status = await systemOpsApi.savePoolAlerts({ ...poolAlertForm })
    poolAlertMessage.value = status.config.enabled
      ? '告警已启用'
      : '告警已保存（未启用）'
    toast.success('告警配置已保存')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '保存失败')
  } finally {
    poolAlertBusy.value = false
  }
}

async function runPoolAlerts() {
  poolAlertBusy.value = true
  try {
    const result = await systemOpsApi.runPoolAlerts(false)
    const report = result.report || {}
    poolAlertMessage.value = report.warnings?.length
      ? report.warnings.join('；')
      : '体检正常'
    toast.success('体检完成')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '体检失败')
  } finally {
    poolAlertBusy.value = false
  }
}

async function loadGovernance() {
  try {
    const status = await systemOpsApi.governance()
    Object.assign(governanceForm, {
      enabled: status.config.enabled,
      mode: status.config.mode,
      run_every_hours: status.config.run_every_hours,
      account_log_retention_hours: status.config.account_log_retention_hours,
    })
    const report = status.last_report || {}
    if (report.finished_at) {
      governanceMessage.value = `上次治理 ${report.finished_at}：死号 ${report.dead_accounts ?? 0}，清理账号日志 ${report.account_logs_removed ?? 0} 条`
    }
  } catch {
    /* 治理为可选功能 */
  }
}

async function saveGovernance() {
  governanceBusy.value = true
  try {
    const status = await systemOpsApi.saveGovernance({ ...governanceForm })
    governanceMessage.value = status.config.enabled ? '治理已启用' : '治理已保存（未启用）'
    toast.success('治理配置已保存')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '保存失败')
  } finally {
    governanceBusy.value = false
  }
}

async function runGovernancePreview() {
  governanceBusy.value = true
  try {
    const result = await systemOpsApi.runGovernance(true)
    const report = result.report || {}
    governanceMessage.value = `预检：死号 ${report.dead_accounts ?? 0}，待清理账号日志 ${report.account_logs_pending ?? 0} 条`
    toast.success('预检完成')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '预检失败')
  } finally {
    governanceBusy.value = false
  }
}

async function loadBackup() {
  try {
    const status = await systemOpsApi.localBackup()
    backupState.value = status
    Object.assign(backupForm, {
      enabled: status.config.enabled,
      interval_hours: status.config.interval_hours,
      keep: status.config.keep,
    })
    const report = status.last_report || {}
    if (report.error) backupMessage.value = `上次备份失败：${report.error}`
    else if (report.skipped) backupMessage.value = report.skipped
    else if (report.finished_at) backupMessage.value = `上次备份 ${report.finished_at}，保留 ${report.kept ?? 0} 份`
  } catch {
    /* 可选功能 */
  }
}

async function saveBackup() {
  backupBusy.value = true
  try {
    backupState.value = await systemOpsApi.saveLocalBackup({ ...backupForm })
    backupMessage.value = backupState.value?.config.enabled ? '本地备份已启用' : '本地备份已保存（未启用）'
    toast.success('备份配置已保存')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '保存失败')
  } finally {
    backupBusy.value = false
  }
}

async function runBackup() {
  backupBusy.value = true
  try {
    const status = await systemOpsApi.runLocalBackup()
    backupState.value = status
    const report = status.report || {}
    backupMessage.value = report.error
      ? `备份失败：${report.error}`
      : `已生成 ${report.file || '备份'}（${Math.round((report.size_bytes || 0) / 1024 / 1024 * 10) / 10} MB）`
    toast.success('备份完成')
    await loadAudit()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '备份失败')
  } finally {
    backupBusy.value = false
  }
}

async function loadAudit() {
  try {
    auditEntries.value = await systemOpsApi.audit(30)
  } catch {
    auditEntries.value = []
  }
}

function applyDebugLogStatus(status: DebugLogStatus) {
  debugLogStatus.value = status
  Object.assign(debugLogForm, {
    enabled: status.config.enabled,
    mode: status.config.mode,
    retention_days: status.config.retention_days,
    max_size_mb: status.config.max_size_mb,
    slack_mb: status.config.slack_mb,
    level: status.config.level,
  })
}

async function loadDebugLogs() {
  try {
    applyDebugLogStatus(await systemOpsApi.debugLogs())
  } catch {
    debugLogStatus.value = null
  }
}

async function saveDebugLogs() {
  debugLogBusy.value = true
  try {
    applyDebugLogStatus(await systemOpsApi.saveDebugLogs({ ...debugLogForm }))
    debugLogMessage.value = debugLogStatus.value?.active ? '调试日志已启用' : '调试日志已保存（未启用）'
    toast.success('调试日志配置已保存')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '保存失败')
  } finally {
    debugLogBusy.value = false
  }
}

async function exportDebugLogs() {
  debugLogBusy.value = true
  try {
    const { blob, filename } = await systemOpsApi.exportDebugLogs()
    saveBlob(blob, filename, 'qingmeng-debug.zip')
    debugLogMessage.value = `已导出 ${filename}（${(blob.size / 1024).toFixed(0)} KB）`
    toast.success('调试包已开始下载')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '导出失败')
  } finally {
    debugLogBusy.value = false
  }
}

async function clearDebugLogs() {
  debugLogBusy.value = true
  try {
    applyDebugLogStatus(await systemOpsApi.clearDebugLogs())
    debugLogMessage.value = '日志已清空'
    toast.success('调试日志已清空')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '清空失败')
  } finally {
    debugLogBusy.value = false
  }
}

async function checkUpstream() {
  upstreamBusy.value = true
  try {
    const report = await systemOpsApi.upstreamDiagnostics()
    upstreamMessage.value = (report.advice || []).join('；') || '上游连通性正常'
    toast.success('连通性自检完成')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '自检失败')
  } finally {
    upstreamBusy.value = false
  }
}

function legacyPayload() {
  const paths = legacyPathsText.value
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean)
  return { ...legacyForm, paths }
}

async function saveLegacySync() {
  legacyBusy.value = true
  legacyMessage.value = ''
  try {
    applyLegacyState(await systemOpsApi.saveLegacySync(legacyPayload()))
    legacyMessage.value = '配置已保存'
    toast.success('目录同步配置已保存')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '保存失败')
  } finally {
    legacyBusy.value = false
  }
}

async function runLegacySync(dryRun: boolean) {
  legacyBusy.value = true
  legacyMessage.value = ''
  try {
    const state = await systemOpsApi.runLegacySync(dryRun)
    applyLegacyState(state)
    const result = state.result || {}
    legacyMessage.value = dryRun
      ? `试运行完成：待导入账号 ${result.probes?.reduce?.((sum: number, probe) => sum + (probe.dry_run_fresh_accounts || 0), 0) ?? 0}`
      : `同步完成：新增账号 ${result.added_accounts || 0}，日志 ${result.imported_call_records || 0}`
    toast.success(legacyMessage.value)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '同步失败')
  } finally {
    legacyBusy.value = false
  }
}

function selectPalette(id: PaletteId) {
  currentPalette.value = setStoredPalette(id)
}

onMounted(async () => {
  await Promise.all([
    loadScheduler(),
    loadDatabase(),
    loadQueue(),
    loadProxyHealth(),
    loadLegacySync(),
    loadLegacyImports(),
    loadCredentialSummary(),
    loadPoolAlerts(),
    loadGovernance(),
    loadBackup(),
    loadAudit(),
    loadDebugLogs(),
  ])
  pollTimer = window.setInterval(() => {
    // 单个轮询还没落地就不再叠加请求；loadScheduler 内部还会挡掉保存 / 切模式期间的回写。
    if (!schedulerLoading.value) void loadScheduler()
    void loadQueue()
  }, 3000)
})

onBeforeUnmount(() => {
  // 离开页面时别让 200MB 的请求继续挂在后台。
  legacyUploadController?.abort()
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
})
</script>
