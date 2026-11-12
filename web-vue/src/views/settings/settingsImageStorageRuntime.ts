import { ref } from 'vue'

import {
  settingsApi,
  type ImageStorageTarget,
  type ImageStorageTargetInput,
  type ImageStorageTargetTestResult,
  type ImageStorageTestResult,
} from '@/api/settings'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useOperationProgressRuntime } from '@/composables/useOperationProgressRuntime'
import type { usePageRuntime } from '@/composables/usePageRuntime'
import { usePageQuery } from '@/composables/usePageQuery'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'

export type ImageStorageTargetForm = {
  name: string
  enabled: boolean
  account_id: string
  bucket: string
  access_key_id: string
  secret_access_key: string
  prefix: string
  public_base_url: string
  upload_timeout_secs: number
}

type SettingsImageStorageRuntimeOptions = {
  requireSavedSettings: (actionLabel: string) => boolean
  runtime?: ReturnType<typeof usePageRuntime>
  requestKey?: string
}

function createTargetForm(target?: ImageStorageTarget): ImageStorageTargetForm {
  return {
    name: target?.name || 'R2 图片存储',
    enabled: target?.enabled ?? true,
    account_id: target?.account_id || '',
    bucket: target?.bucket || '',
    access_key_id: target?.access_key_id || '',
    secret_access_key: '',
    prefix: target?.prefix || 'images',
    public_base_url: target?.public_base_url || '',
    upload_timeout_secs: target?.upload_timeout_secs || 60,
  }
}

export function useSettingsImageStorageRuntime(options: SettingsImageStorageRuntimeOptions) {
  const imageStorageBusy = ref('')
  const imageStorageTestResult = ref<ImageStorageTestResult | null>(null)
  const imageStorageTargets = ref<ImageStorageTarget[]>([])
  const imageStorageTargetsLoaded = ref(false)
  const imageStorageTargetsLoading = ref(false)
  const imageStorageTargetsError = ref('')
  const imageStorageTargetModal = ref<'create' | 'edit' | ''>('')
  const editingImageStorageTarget = ref<ImageStorageTarget | null>(null)
  const imageStorageTargetForm = ref<ImageStorageTargetForm>(createTargetForm())
  const imageStorageTargetTestResult = ref<ImageStorageTargetTestResult | null>(null)
  const toast = useToast()
  const confirmDialog = useConfirmDialog()
  const progressRuntime = useOperationProgressRuntime()
  const operationProgress = progressRuntime.state
  const targetsQuery = options.runtime
    ? usePageQuery({
        runtime: options.runtime,
        key: options.requestKey || 'settings:image-storage-targets',
        loading: imageStorageTargetsLoading,
        error: imageStorageTargetsError,
        errorMessage: '加载 R2 图片存储目标失败',
      })
    : null

  function upsertTarget(target: ImageStorageTarget) {
    const index = imageStorageTargets.value.findIndex((item) => item.id === target.id)
    imageStorageTargets.value = index < 0
      ? [...imageStorageTargets.value, target]
      : imageStorageTargets.value.map((item) => item.id === target.id ? target : item)
  }

  async function loadImageStorageTargets() {
    const apply = (response: { targets: ImageStorageTarget[] }) => {
      imageStorageTargets.value = Array.isArray(response.targets) ? response.targets : []
      imageStorageTargetsLoaded.value = true
    }
    const onError = (message: string) => {
      imageStorageTargets.value = []
      toast.error(message)
    }
    if (targetsQuery) {
      await targetsQuery.run(() => settingsApi.listImageStorageTargets(), { apply, onError })
      return
    }
    try {
      apply(await settingsApi.listImageStorageTargets())
    } catch (error) {
      onError(errorMessage(error, '加载 R2 图片存储目标失败'))
    }
  }

  function resetImageStorageTargetForm() {
    editingImageStorageTarget.value = null
    imageStorageTargetForm.value = createTargetForm()
    imageStorageTargetTestResult.value = null
  }

  function openCreateImageStorageTarget() {
    resetImageStorageTargetForm()
    imageStorageTargetModal.value = 'create'
  }

  function openEditImageStorageTarget(target: ImageStorageTarget) {
    editingImageStorageTarget.value = target
    imageStorageTargetForm.value = createTargetForm(target)
    imageStorageTargetTestResult.value = null
    imageStorageTargetModal.value = 'edit'
  }

  function closeImageStorageTargetModal() {
    if (imageStorageBusy.value === 'target-save') return
    imageStorageTargetModal.value = ''
    resetImageStorageTargetForm()
  }

  function targetPayload(): ImageStorageTargetInput {
    const form = imageStorageTargetForm.value
    return {
      name: form.name.trim(),
      enabled: form.enabled,
      account_id: form.account_id.trim(),
      bucket: form.bucket.trim(),
      access_key_id: form.access_key_id.trim(),
      ...(form.secret_access_key.trim() ? { secret_access_key: form.secret_access_key.trim() } : {}),
      prefix: form.prefix.trim(),
      public_base_url: form.public_base_url.trim(),
      upload_timeout_secs: Number(form.upload_timeout_secs),
    }
  }

  async function saveImageStorageTarget() {
    const payload = targetPayload()
    if (!payload.name || !payload.account_id || !payload.bucket || !payload.access_key_id || !payload.prefix) {
      toast.warning('请填写 R2 目标名称、Account ID、Bucket、Access Key ID 和对象前缀')
      return
    }
    if (!editingImageStorageTarget.value && !payload.secret_access_key) {
      toast.warning('新增 R2 目标需要填写 Secret Access Key')
      return
    }
    if (!Number.isInteger(payload.upload_timeout_secs) || Number(payload.upload_timeout_secs) < 5) {
      toast.warning('上传超时必须是至少 5 秒的整数')
      return
    }

    imageStorageBusy.value = 'target-save'
    try {
      const response = editingImageStorageTarget.value
        ? await settingsApi.updateImageStorageTarget(editingImageStorageTarget.value.id, payload)
        : await settingsApi.createImageStorageTarget(payload as ImageStorageTargetInput & {
            name: string
            account_id: string
            bucket: string
            access_key_id: string
            secret_access_key: string
          })
      upsertTarget(response.target)
      imageStorageBusy.value = ''
      imageStorageTargetModal.value = ''
      resetImageStorageTargetForm()
      toast.success('R2 图片存储目标已保存')
    } catch (error) {
      toast.error(errorMessage(error, '保存 R2 图片存储目标失败'))
    } finally {
      imageStorageBusy.value = ''
    }
  }

  async function toggleImageStorageTarget(target: ImageStorageTarget) {
    imageStorageBusy.value = target.id
    try {
      const response = await settingsApi.updateImageStorageTarget(target.id, { enabled: !target.enabled })
      upsertTarget(response.target)
      toast.success(target.enabled ? 'R2 目标已停用' : 'R2 目标已启用')
    } catch (error) {
      toast.error(errorMessage(error, '更新 R2 图片存储目标失败'))
    } finally {
      imageStorageBusy.value = ''
    }
  }

  async function deleteImageStorageTarget(target: ImageStorageTarget) {
    const confirmed = await confirmDialog.ask({
      title: '删除 R2 图片存储目标',
      message: `确定删除「${target.name || target.id}」吗？仅未被图片引用的目标可以删除。`,
      confirmText: '删除',
      cancelText: '取消',
    })
    if (!confirmed) return

    imageStorageBusy.value = target.id
    try {
      await settingsApi.deleteImageStorageTarget(target.id)
      imageStorageTargets.value = imageStorageTargets.value.filter((item) => item.id !== target.id)
      toast.success('R2 图片存储目标已删除')
    } catch (error) {
      toast.error(errorMessage(error, '删除 R2 图片存储目标失败'))
    } finally {
      imageStorageBusy.value = ''
    }
  }

  async function testImageStorageTarget(target: ImageStorageTarget) {
    if (!options.requireSavedSettings('测试 R2 图片存储目标')) return
    const confirmed = await confirmDialog.ask({
      title: '测试 R2 图片存储目标',
      message: `即将访问 R2 目标「${target.name || target.id}」执行连接测试，可能访问外部存储服务。是否继续？`,
      confirmText: '开始测试',
      cancelText: '取消',
    })
    if (!confirmed) return

    imageStorageBusy.value = `target-test:${target.id}`
    imageStorageTargetTestResult.value = null
    try {
      const response = await settingsApi.testImageStorageTarget(target.id)
      imageStorageTargetTestResult.value = response.result
      if (response.result.ok) toast.success('R2 连接测试通过')
      else toast.warning(response.result.error || 'R2 连接测试失败')
      await loadImageStorageTargets()
    } catch (error) {
      const message = errorMessage(error, 'R2 连接测试失败')
      imageStorageTargetTestResult.value = { ok: false, target_id: target.id, error: message }
      toast.error(message)
    } finally {
      imageStorageBusy.value = ''
    }
  }

  async function testImageStorageConnection() {
    if (!options.requireSavedSettings('测试 WebDAV')) return
    const confirmed = await confirmDialog.ask({
      title: '确认测试 WebDAV',
      message: '即将使用已保存的图片存储配置发起 WebDAV 连接测试，可能访问外部存储服务。是否继续？',
      confirmText: '开始测试',
      cancelText: '取消',
    })
    if (!confirmed) return

    imageStorageBusy.value = 'test'
    imageStorageTestResult.value = null
    try {
      const response = await settingsApi.testImageStorage()
      imageStorageTestResult.value = response.result
      if (response.result.ok) toast.success('WebDAV 测试通过')
      else toast.warning(response.result.error || 'WebDAV 测试失败')
    } catch (error) {
      const message = errorMessage(error, 'WebDAV 测试失败')
      imageStorageTestResult.value = { ok: false, error: message }
      toast.error(message)
    } finally {
      imageStorageBusy.value = ''
    }
  }

  async function syncImageStorageFiles() {
    if (!options.requireSavedSettings('同步本地图片')) return
    const confirmed = await confirmDialog.ask({
      title: '确认同步图片存储',
      message: '即将扫描本地图片并同步到已配置的图片存储，可能产生外部上传流量。是否继续？',
      confirmText: '开始同步',
      cancelText: '取消',
    })
    if (!confirmed) return

    imageStorageBusy.value = 'sync'
    await progressRuntime.start({
      title: '全量同步图片',
      subtitle: '本地 → 已配置的图片存储',
      message: '正在扫描并上传本地图片...',
    })
    try {
      const response = await settingsApi.syncImageStorage()
      const summary = `上传 ${response.result.uploaded}，跳过 ${response.result.skipped}，失败 ${response.result.failed}`
      if (response.result.failed > 0) progressRuntime.warn(summary)
      else progressRuntime.succeed(summary)
    } catch (error) {
      progressRuntime.fail(errorMessage(error, '同步图片失败'))
    } finally {
      imageStorageBusy.value = ''
    }
  }

  function invalidate() {
    targetsQuery?.invalidate()
    imageStorageTargetsLoaded.value = false
    imageStorageTargetsError.value = ''
    imageStorageTargets.value = []
    imageStorageTargetTestResult.value = null
  }

  return {
    imageStorageBusy,
    imageStorageTestResult,
    imageStorageTargets,
    imageStorageTargetsLoaded,
    imageStorageTargetsLoading,
    imageStorageTargetsError,
    imageStorageTargetModal,
    editingImageStorageTarget,
    imageStorageTargetForm,
    imageStorageTargetTestResult,
    operationProgress,
    closeOperationProgress: progressRuntime.close,
    loadImageStorageTargets,
    openCreateImageStorageTarget,
    openEditImageStorageTarget,
    closeImageStorageTargetModal,
    saveImageStorageTarget,
    toggleImageStorageTarget,
    deleteImageStorageTarget,
    testImageStorageTarget,
    invalidate,
    testImageStorageConnection,
    syncImageStorageFiles,
  }
}
