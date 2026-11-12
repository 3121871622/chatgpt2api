<template>
  <div class="settings-storage-grid">
    <FormSection title="图片存储" class="settings-storage-main">
      <div class="settings-check-grid settings-check-grid--single">
        <div class="settings-check-item">
          <div class="settings-check-control">
            <Checkbox
              v-model="imageStorage.enabled"
              :disabled="fieldReadOnly('image_storage.enabled')"
            >启用图片存储</Checkbox>
          </div>
        </div>
      </div>

      <FormField label="存储模式">
        <div class="w-full">
          <GroupedSelectMenu
            v-model="imageStorage.mode"
            :options="imageStorageModeOptions"
            :disabled="fieldReadOnly('image_storage.mode')"
            selected-indicator="none"
            aria-label="图片存储模式"
            block
          />
        </div>
      </FormField>

      <div v-if="imageStorage.mode === 'r2'" class="settings-r2-section">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div>
            <p class="text-sm font-medium">R2 存储目标</p>
            <p class="mt-1 text-xs text-muted-foreground">按图库已管理的图片字节数分配新上传；停用目标不会影响已有图片读取。</p>
          </div>
          <Button size="xs" variant="outline" :disabled="imageStorageTargetsLoading" @click="$emit('createTarget')">
            新增目标
          </Button>
        </div>

        <div v-if="imageStorageTargetsLoading && !imageStorageTargets.length" class="settings-r2-state">
          正在加载 R2 目标...
        </div>
        <div v-else-if="imageStorageTargetsError" class="settings-r2-state settings-r2-state--error">
          {{ imageStorageTargetsError }}
          <Button size="xs" variant="outline" @click="$emit('loadTargets')">重试</Button>
        </div>
        <div v-else-if="!imageStorageTargets.length" class="settings-r2-state">
          尚未配置 R2 图片存储目标。
        </div>
        <div v-else class="space-y-2">
          <div v-for="target in imageStorageTargets" :key="target.id" class="settings-r2-target">
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-2">
                <strong class="truncate text-sm">{{ target.name }}</strong>
                <span class="settings-r2-badge" :class="target.enabled ? 'is-enabled' : 'is-disabled'">
                  {{ target.enabled ? '启用' : '停用' }}
                </span>
                <span v-if="target.legacy" class="settings-r2-badge">兼容目标</span>
                <span class="settings-r2-badge" :class="healthClass(target.health_status)">
                  {{ healthLabel(target.health_status) }}
                </span>
              </div>
              <p class="mt-1 break-all text-xs text-muted-foreground">
                {{ target.bucket }} / {{ target.prefix }} · {{ target.account_id }}
              </p>
              <p class="mt-1 text-xs text-muted-foreground">
                {{ formatBytes(target.used_bytes) }} · {{ target.asset_count }} 张图片
                <span v-if="target.capacity_error"> · {{ target.capacity_error }}</span>
              </p>
            </div>
            <div class="flex shrink-0 flex-wrap justify-end gap-1">
              <Button size="xs" variant="outline" :disabled="imageStorageBusy === `target-test:${target.id}`" @click="$emit('testTarget', target)">
                {{ imageStorageBusy === `target-test:${target.id}` ? '测试中...' : '测试' }}
              </Button>
              <Button size="xs" variant="outline" :disabled="imageStorageBusy === target.id || target.legacy" @click="$emit('toggleTarget', target)">
                {{ target.enabled ? '停用' : '启用' }}
              </Button>
              <Button size="xs" variant="outline" :disabled="imageStorageBusy === target.id" @click="$emit('editTarget', target)">编辑</Button>
              <Button size="xs" variant="outline" root-class="text-rose-600" :disabled="!target.can_delete || imageStorageBusy === target.id" @click="$emit('deleteTarget', target)">删除</Button>
            </div>
          </div>
        </div>

        <div v-if="imageStorageTargetTestResult" class="rounded-xl border border-border bg-background px-3 py-2 text-xs">
          <p :class="imageStorageTargetTestResult.ok ? 'text-emerald-600' : 'text-slate-600'">
            {{ imageStorageTargetTestResult.ok ? 'R2 可用' : 'R2 不可用' }}
            <span v-if="imageStorageTargetTestResult.status"> · HTTP {{ imageStorageTargetTestResult.status }}</span>
          </p>
          <p v-if="imageStorageTargetTestResult.error" class="mt-1 break-all text-slate-600">{{ imageStorageTargetTestResult.error }}</p>
        </div>
      </div>

      <template v-if="imageStorage.mode === 'webdav' || imageStorage.mode === 'both'">
        <FormField label="WebDAV URL">
          <Input
            v-model.trim="imageStorage.webdav_url"
            block
            :disabled="fieldReadOnly('image_storage.webdav_url')"
            placeholder="https://example.com/dav"
          />
        </FormField>

        <div class="grid grid-cols-1 gap-2.5 md:grid-cols-2">
          <FormField label="用户名">
            <Input
              v-model.trim="imageStorage.webdav_username"
              block
              :disabled="fieldReadOnly('image_storage.webdav_username')"
            />
          </FormField>

          <FormField label="密码">
            <Input
              v-model="imageStorage.webdav_password"
              type="password"
              block
              :disabled="fieldReadOnly('image_storage.webdav_password')"
              :placeholder="imageStorage.has_webdav_password ? '已配置，留空不修改' : '请输入 WebDAV 密码'"
            />
          </FormField>
        </div>

        <FormField label="根路径">
          <Input
            v-model.trim="imageStorage.webdav_root_path"
            block
            :disabled="fieldReadOnly('image_storage.webdav_root_path')"
            placeholder="qingmeng/images"
          />
        </FormField>

        <FormField label="公开访问前缀">
          <Input
            v-model.trim="imageStorage.public_base_url"
            block
            :disabled="fieldReadOnly('image_storage.public_base_url')"
            placeholder="https://cdn.example.com/images"
          />
        </FormField>
      </template>

      <div class="flex flex-wrap items-center gap-2">
        <Button
          v-if="imageStorage.mode === 'webdav' || imageStorage.mode === 'both'"
          size="xs"
          variant="outline"
          :disabled="imageStorageBusy === 'test'"
          @click="$emit('testStorage')"
        >{{ imageStorageBusy === 'test' ? '测试中...' : '测试 WebDAV' }}</Button>
        <Button
          v-if="imageStorage.mode === 'r2' || imageStorage.mode === 'webdav' || imageStorage.mode === 'both'"
          size="xs"
          variant="outline"
          :disabled="imageStorageBusy === 'sync'"
          @click="$emit('syncStorage')"
        >{{ imageStorageBusy === 'sync' ? '同步中...' : '全量同步' }}</Button>
      </div>

      <div v-if="imageStorageTestResult && (imageStorage.mode === 'webdav' || imageStorage.mode === 'both')" class="rounded-xl border border-border bg-background px-3 py-2 text-xs">
        <p :class="imageStorageTestResult.ok ? 'text-emerald-600' : 'text-slate-600'">
          {{ imageStorageTestResult.ok ? 'WebDAV 可用' : 'WebDAV 不可用' }}
          <span v-if="imageStorageTestResult.status"> · HTTP {{ imageStorageTestResult.status }}</span>
        </p>
        <p v-if="imageStorageTestResult.error" class="mt-1 break-all text-slate-600">{{ imageStorageTestResult.error }}</p>
      </div>
    </FormSection>

    <FormSection title="AI 审核" class="settings-storage-side">
      <div class="settings-check-grid settings-check-grid--single">
        <div class="settings-check-item">
          <div class="settings-check-control">
            <Checkbox
              v-model="settings.ai_review.enabled"
              :disabled="fieldReadOnly('ai_review.enabled')"
            >启用 AI 审核</Checkbox>
          </div>
        </div>
      </div>

      <FormField label="Base URL">
        <Input
          v-model.trim="settings.ai_review.base_url"
          block
          :disabled="fieldReadOnly('ai_review.base_url')"
          placeholder="https://api.openai.com"
        />
      </FormField>

      <FormField label="API Key">
        <Input
          v-model="settings.ai_review.api_key"
          type="password"
          block
          :disabled="fieldReadOnly('ai_review.api_key')"
          :placeholder="settings.ai_review.has_api_key ? '已配置，留空不修改' : 'sk-...'"
        />
      </FormField>

      <FormField label="Model">
        <Input
          v-model.trim="settings.ai_review.model"
          block
          :disabled="fieldReadOnly('ai_review.model')"
          placeholder="gpt-5.4-mini"
        />
      </FormField>

      <FormField label="审核提示词">
        <textarea
          v-model="settings.ai_review.prompt"
          rows="5"
          class="ui-textarea-sm"
          :disabled="fieldReadOnly('ai_review.prompt')"
          placeholder="判断用户请求是否允许。只回答 ALLOW 或 REJECT。"
        ></textarea>
      </FormField>
    </FormSection>
  </div>

  <ModalShell
    :open="imageStorageTargetModal !== ''"
    aria-label="R2 图片存储目标"
    :z-index="130"
    scrollable
    close-on-backdrop
    @close="$emit('closeTargetModal')"
  >
    <ModalHeader
      :title="imageStorageTargetModal === 'edit' ? '编辑 R2 图片存储目标' : '新增 R2 图片存储目标'"
      subtitle="密钥不会回显；编辑时留空 Secret Access Key 会保留原值。"
      :close-disabled="imageStorageBusy === 'target-save'"
      :bordered="false"
      @close="$emit('closeTargetModal')"
    />
    <ModalBody class="space-y-3 overflow-y-auto">
      <div class="grid gap-3 md:grid-cols-2">
        <FormField label="名称">
          <Input v-model.trim="imageStorageTargetForm.name" block placeholder="例如：主图库 R2" />
        </FormField>
        <FormField label="Bucket">
          <Input v-model.trim="imageStorageTargetForm.bucket" block placeholder="image-assets" />
        </FormField>
        <FormField label="Account ID">
          <Input v-model.trim="imageStorageTargetForm.account_id" block placeholder="Cloudflare Account ID" />
        </FormField>
        <FormField label="Access Key ID">
          <Input v-model.trim="imageStorageTargetForm.access_key_id" block placeholder="R2 Access Key ID" />
        </FormField>
        <FormField label="Secret Access Key">
          <Input v-model="imageStorageTargetForm.secret_access_key" type="password" block :placeholder="imageStorageTargetModal === 'edit' ? '留空则不修改' : 'R2 Secret Access Key'" />
        </FormField>
        <FormField label="对象前缀">
          <Input v-model.trim="imageStorageTargetForm.prefix" block placeholder="images" />
        </FormField>
        <FormField label="公开访问前缀">
          <Input v-model.trim="imageStorageTargetForm.public_base_url" block placeholder="可选，例如 https://cdn.example.com" />
        </FormField>
        <FormField label="上传超时（秒）">
          <Input v-model.number="imageStorageTargetForm.upload_timeout_secs" type="number" min="5" max="300" step="1" block />
        </FormField>
      </div>
      <div class="settings-check-item">
        <div class="settings-check-control">
          <Checkbox v-model="imageStorageTargetForm.enabled">启用该 R2 目标</Checkbox>
        </div>
      </div>
    </ModalBody>
    <ModalFooter :bordered="false">
      <Button size="sm" variant="outline" :disabled="imageStorageBusy === 'target-save'" @click="$emit('closeTargetModal')">取消</Button>
      <Button size="sm" variant="primary" :disabled="imageStorageBusy === 'target-save'" @click="$emit('saveTarget')">
        {{ imageStorageBusy === 'target-save' ? '保存中...' : '保存' }}
      </Button>
    </ModalFooter>
  </ModalShell>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Button, Checkbox, FormField, FormSection, Input } from 'nanocat-ui'
import type { ImageStorageTarget, ImageStorageTargetTestResult, ImageStorageTestResult } from '@/api/settings'
import type { ImageStorageTargetForm } from '@/views/settings/settingsImageStorageRuntime'
import ModalBody from '@/components/ai/ModalBody.vue'
import ModalFooter from '@/components/ai/ModalFooter.vue'
import ModalHeader from '@/components/ai/ModalHeader.vue'
import ModalShell from '@/components/ai/ModalShell.vue'
import { GroupedSelectMenu } from 'nanocat-ui'
import type { Settings } from '@/types/api'
import {
  settingsFieldOptions,
  settingsFieldReadOnly,
  type SettingsFields,
} from '@/views/settings/settingsView'

const props = defineProps<{
  settings: Settings
  fields: SettingsFields
  imageStorageBusy: string
  imageStorageTestResult: ImageStorageTestResult | null
  imageStorageTargets: ImageStorageTarget[]
  imageStorageTargetsLoading: boolean
  imageStorageTargetsError: string
  imageStorageTargetModal: 'create' | 'edit' | ''
  imageStorageTargetForm: ImageStorageTargetForm
  imageStorageTargetTestResult: ImageStorageTargetTestResult | null
}>()

defineEmits<{
  testStorage: []
  syncStorage: []
  loadTargets: []
  createTarget: []
  editTarget: [target: ImageStorageTarget]
  toggleTarget: [target: ImageStorageTarget]
  deleteTarget: [target: ImageStorageTarget]
  testTarget: [target: ImageStorageTarget]
  closeTargetModal: []
  saveTarget: []
}>()

const imageStorage = computed(() => props.settings.image_storage as NonNullable<Settings['image_storage']>)
const fieldReadOnly = (path: string) => settingsFieldReadOnly(props.fields, path)
const imageStorageModeOptions = computed(() => (
  settingsFieldOptions(props.fields, 'image_storage.mode', imageStorage.value.mode)
))

function formatBytes(value: number) {
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1)
  return `${(value / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`
}

function healthLabel(status: string) {
  if (status === 'healthy') return '健康'
  if (status === 'error') return '异常'
  return '未测试'
}

function healthClass(status: string) {
  return status === 'healthy' ? 'is-enabled' : status === 'error' ? 'is-error' : ''
}
</script>

<style scoped>
.settings-storage-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

@media (min-width: 1280px) {
  .settings-storage-grid {
    grid-template-columns: minmax(0, 2fr) minmax(20rem, 1fr);
  }
}

.settings-storage-main,
.settings-storage-side {
  min-width: 0;
}

.settings-r2-section {
  display: grid;
  gap: 10px;
  border-top: 1px solid hsl(var(--border));
  padding-top: 14px;
}

.settings-r2-state {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  border: 1px dashed hsl(var(--border));
  border-radius: 12px;
  padding: 12px;
  font-size: 12px;
  color: hsl(var(--muted-foreground));
}

.settings-r2-state--error {
  color: hsl(var(--destructive));
}

.settings-r2-target {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  border: 1px solid hsl(var(--border));
  border-radius: 12px;
  padding: 10px 12px;
}

.settings-r2-badge {
  border: 1px solid hsl(var(--border));
  border-radius: 999px;
  padding: 2px 7px;
  font-size: 10px;
  color: hsl(var(--muted-foreground));
}

.settings-r2-badge.is-enabled {
  border-color: hsl(142 70% 40% / 0.35);
  color: hsl(142 70% 30%);
}

.settings-r2-badge.is-disabled,
.settings-r2-badge.is-error {
  border-color: hsl(0 72% 50% / 0.35);
  color: hsl(0 72% 42%);
}

.settings-check-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(13.5rem, 1fr));
  gap: 8px;
}

.settings-check-grid--single {
  grid-template-columns: minmax(0, 1fr);
}

.settings-check-item {
  min-height: 38px;
  border: 1px solid hsl(var(--border));
  border-radius: 14px;
  background: hsl(var(--background) / 0.72);
  transition:
    border-color 0.16s ease,
    background-color 0.16s ease;
}

.settings-check-item:hover {
  border-color: hsl(var(--foreground) / 0.18);
  background: hsl(var(--muted) / 0.24);
}

.settings-check-control {
  display: flex;
  min-height: 36px;
  align-items: center;
  gap: 8px;
  padding: 0 10px;
}
</style>
