import { getStringPreference, preferenceKeys, setStringPreference } from './preferences'

export type ThemeMode = 'light' | 'dark' | 'system'

type ResolvedTheme = 'light' | 'dark'

/**
 * Five independent skins.  A palette is not a hue swap: each one ships its own
 * colour system, radius scale, typography pairing, density and surface
 * treatment (see `style.css`).  `default` is the original console look.
 */
export type PaletteId = 'default' | 'jade' | 'aurora' | 'sakura' | 'obsidian'

export type PaletteOption = {
  id: PaletteId
  label: string
  description: string
  /** Preview swatches: background, surface, accent. */
  swatches: [string, string, string]
  dark: boolean
  /** Layout skin: expanded sidebar width. */
  sidebarWidth: string
}

export const PALETTE_OPTIONS: PaletteOption[] = [
  {
    id: 'default',
    label: '默认主题',
    description: '中性灰白 · 衬线标题 · 圆角卡片，原控制台风格',
    swatches: ['#f7f7f8', '#ffffff', '#1f1f22'],
    dark: false,
    sidebarWidth: '16rem',
  },
  {
    id: 'jade',
    label: '倾梦·青瓷',
    description: '水墨青瓷 · 玉青主色 · 大面积留白与左描边导航',
    swatches: ['#f4f7f4', '#ffffff', '#0f7a5a'],
    dark: false,
    sidebarWidth: '16.5rem',
  },
  {
    id: 'aurora',
    label: '星夜·霓虹',
    description: '深空底色 · 青紫霓虹 · 直角面板与发光描边',
    swatches: ['#0b1020', '#141b33', '#38d6ff'],
    dark: true,
    sidebarWidth: '15rem',
  },
  {
    id: 'sakura',
    label: '樱语·柔光',
    description: '奶油粉调 · 超圆角 · 柔和投影与宽行距',
    swatches: ['#fff7f5', '#fffdfc', '#c2557a'],
    dark: false,
    sidebarWidth: '17rem',
  },
  {
    id: 'obsidian',
    label: '曜石·极简',
    description: '石墨黑金 · 零圆角 · 等宽数字与紧凑密度',
    swatches: ['#0f0f10', '#17171a', '#d9a441'],
    dark: true,
    sidebarWidth: '14.5rem',
  },
]

const DEFAULT_PALETTE: PaletteId = 'default'
const PALETTE_IDS = PALETTE_OPTIONS.map(option => option.id)

export function normalizePalette(value: unknown): PaletteId {
  const candidate = String(value || '').trim() as PaletteId
  return PALETTE_IDS.includes(candidate) ? candidate : DEFAULT_PALETTE
}

export function getStoredPalette(): PaletteId {
  return normalizePalette(getStringPreference(preferenceKeys.themePalette, DEFAULT_PALETTE))
}

export function applyPalette(palette: PaletteId): void {
  if (typeof document === 'undefined') return
  const normalized = normalizePalette(palette)
  const root = document.documentElement
  root.dataset.palette = normalized
  const option = PALETTE_OPTIONS.find(item => item.id === normalized)
  // Palettes with an intrinsic dark surface stay dark even in light mode.
  root.dataset.paletteTone = option?.dark ? 'dark' : 'light'
  const meta = document.querySelector('meta[name="theme-color"]')
  if (meta && option) meta.setAttribute('content', option.swatches[0])
}

export function setStoredPalette(palette: PaletteId): PaletteId {
  const normalized = normalizePalette(palette)
  setStringPreference(preferenceKeys.themePalette, normalized)
  applyPalette(normalized)
  return normalized
}

export function normalizeThemeMode(value: unknown): ThemeMode {
  if (value === 'dark' || value === 'mono-dark') return 'dark'
  if (value === 'light') return 'light'
  return 'system'
}

export function getStoredThemeMode(): ThemeMode {
  return normalizeThemeMode(getStringPreference(preferenceKeys.themeMode, 'system'))
}

export function resolveThemeMode(mode: ThemeMode): ResolvedTheme {
  if (mode !== 'system') return mode
  if (typeof window === 'undefined') return 'light'
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export function applyThemeMode(mode: ThemeMode): void {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  const resolved = resolveThemeMode(mode)
  root.dataset.themePreference = mode
  if (resolved === 'dark') {
    root.dataset.theme = 'dark'
    root.style.colorScheme = 'dark'
    applyPalette(getStoredPalette())
    return
  }
  delete root.dataset.theme
  root.style.colorScheme = 'light'
  applyPalette(getStoredPalette())
}

export function setStoredThemeMode(mode: ThemeMode): void {
  const normalized = normalizeThemeMode(mode)
  setStringPreference(preferenceKeys.themeMode, normalized)
  applyThemeMode(normalized)
}
