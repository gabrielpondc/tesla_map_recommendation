<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import PlaceInput from './components/PlaceInput.vue'
import RouteMap from './components/RouteMap.vue'
import TeslaIcon from './components/TeslaIcon.vue'
import UiIcon from './components/UiIcon.vue'
import { api } from './services/api'
import {
  getMapDrivingRoute,
  getMapPlanningData,
  isSelectedMapEnabled,
  locateWithSelectedMap,
  mapMarkerUrl,
} from './services/maps'

const origin = ref({ name: '', address: '', lng: null, lat: null })
const destination = ref({ name: '', address: '', lng: null, lat: null })
const form = ref({
  initial_range_km: 459,
  reserve_range_km: 40,
  full_range_km: 459,
  charge_to_percent: 80,
  strategy: 'tesla_priority',
  recommendation_mode: 'algorithm',
  max_detour_km: 12,
})
const config = ref(null)
const plan = ref(null)
const loading = ref(false)
const locating = ref(false)
const error = ref('')
const activeTab = ref('plan')
const shareToast = ref('')
const panelScroll = ref(null)
const showAllTesla = ref(false)

const strategies = [
  { value: 'balanced', label: '综合最优', hint: '时间与绕行平衡' },
  { value: 'tesla_priority', label: 'Tesla 超充', hint: '仅选择 Tesla 超级充电站' },
  { value: 'service_area', label: '服务区优先', hint: '尽量不下高速' },
]
const recommendationModes = [
  { value: 'algorithm', label: '传统算法', hint: '速度快，本地计算' },
  { value: 'ai', label: 'AI 推荐', hint: '综合全部超充数据' },
]

const rangeAfterCharge = computed(() => Math.round(form.value.full_range_km * form.value.charge_to_percent / 100))
const reservePercent = computed(() => Math.round(form.value.reserve_range_km / form.value.full_range_km * 100))
const mapProviderName = computed(() => config.value?.map_provider === 'baidu' ? '百度' : '高德')
const teslaAlternatives = computed(() => (plan.value?.candidates || [])
  .filter((candidate) => candidate.station_type === 'tesla_supercharger')
  .sort((a, b) => {
    const aRoundTrip = a.exit_distance_km > 0 && a.entry_distance_km > 0
      ? a.exit_distance_km + a.entry_distance_km : Number.POSITIVE_INFINITY
    const bRoundTrip = b.exit_distance_km > 0 && b.entry_distance_km > 0
      ? b.exit_distance_km + b.entry_distance_km : Number.POSITIVE_INFINITY
    return aRoundTrip - bRoundTrip || (a.exit_distance_km || 999) - (b.exit_distance_km || 999)
  }))
const displayedTeslaAlternatives = computed(() => showAllTesla.value
  ? teslaAlternatives.value : teslaAlternatives.value.slice(0, 12))

const candidateNumberFields = [
  'lng', 'lat', 'progress_km', 'distance_to_route_km',
  'exit_distance_km', 'exit_duration_min', 'entry_distance_km', 'entry_duration_min',
]

function requiredNumber(value, label, min, max) {
  const number = Number(value)
  if (!Number.isFinite(number)) throw new Error(`${label}请输入有效数字`)
  if (number < min || number > max) throw new Error(`${label}需要在 ${min}～${max} 之间`)
  return number
}

function normalizeMapData(data) {
  if (!data) return null
  const route = data.route
  const distance = Number(route?.distance_km)
  const duration = Number(route?.duration_min)
  const polyline = (route?.polyline || []).map((point) => ({
    lng: Number(point?.lng), lat: Number(point?.lat),
  })).filter((point) => Number.isFinite(point.lng) && Number.isFinite(point.lat))
  if (!Number.isFinite(distance) || distance <= 0 || !Number.isFinite(duration) || duration <= 0 || polyline.length < 2) {
    throw new Error('地图返回的道路数据不完整，请重新规划')
  }
  const candidates = (data.candidates || []).map((candidate) => {
    const clean = { ...candidate }
    for (const field of candidateNumberFields) {
      const value = Number(clean[field] ?? 0)
      clean[field] = Number.isFinite(value) ? value : 0
    }
    return clean
  }).filter((candidate) => Number.isFinite(candidate.lng) && Number.isFinite(candidate.lat))
  return {
    route: {
      distance_km: distance,
      duration_min: Math.round(duration),
      tolls_yuan: Number.isFinite(Number(route.tolls_yuan)) ? Number(route.tolls_yuan) : 0,
      polyline,
    },
    candidates,
  }
}

function formatDuration(minutes) {
  if (!minutes) return '—'
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return hours ? `${hours}小时${rest ? ` ${rest}分` : ''}` : `${rest}分钟`
}

function validatePlaces() {
  if (!Number.isFinite(Number(origin.value.lng)) || !Number.isFinite(Number(origin.value.lat))) throw new Error('请从搜索结果中选择起点')
  if (!Number.isFinite(Number(destination.value.lng)) || !Number.isFinite(Number(destination.value.lat))) throw new Error('请从搜索结果中选择目的地')
  const initialRange = requiredNumber(form.value.initial_range_km, '起始可用续航', 20, 1200)
  const fullRange = requiredNumber(form.value.full_range_km, '车辆满电续航', 100, 1200)
  const reserveRange = requiredNumber(form.value.reserve_range_km, '基础安全余量', 0, 500)
  const chargePercent = requiredNumber(form.value.charge_to_percent, '每站充电目标', 40, 100)
  requiredNumber(form.value.max_detour_km, '最远下高速距离', 1, 50)
  if (reserveRange >= initialRange) throw new Error('预留续航必须小于起始续航')
  if (fullRange * chargePercent / 100 <= reserveRange) throw new Error('充电后的目标续航需要高于预留续航')
}

async function submit() {
  error.value = ''
  try {
    validatePlaces()
    loading.value = true
    let mapData = null
    if (isSelectedMapEnabled(config.value)) {
      mapData = normalizeMapData(await getMapPlanningData(
        config.value, origin.value, destination.value, Number(form.value.max_detour_km),
      ))
    }
    const payload = {
      origin: origin.value,
      destination: destination.value,
      ...form.value,
      initial_range_km: Number(form.value.initial_range_km),
      reserve_range_km: Number(form.value.reserve_range_km),
      full_range_km: Number(form.value.full_range_km),
      charge_to_percent: Number(form.value.charge_to_percent),
      max_detour_km: Number(form.value.max_detour_km),
      map_provider: config.value?.map_provider || 'baidu',
      client_route: mapData?.route || null,
      client_candidates: mapData?.candidates || [],
    }
    let result = await api.plan(payload)
    if (mapData && result.stops.length && !result.summary.uses_recommended_locations) {
      const allCandidates = result.candidates
      for (let attempt = 0; attempt < 4; attempt += 1) {
        const stopIds = result.stops.map((stop) => stop.id).join(',')
        const routeWithStops = await getMapDrivingRoute(config.value, origin.value, destination.value, result.stops)
        const refined = await api.plan({
          ...payload,
          client_route: routeWithStops,
          client_candidates: allCandidates,
          client_route_includes_stops: true,
        })
        const refinedIds = refined.stops.map((stop) => stop.id).join(',')
        result = refined
        if (refinedIds === stopIds) break
      }
      result.candidates = allCandidates
    }
    plan.value = result
    showAllTesla.value = false
  } catch (cause) {
    error.value = cause.message || '规划失败，请检查输入'
  } finally {
    loading.value = false
  }
}

async function locate() {
  error.value = ''
  if (!navigator.geolocation) {
    error.value = '当前浏览器不支持定位'
    return
  }
  locating.value = true
  if (isSelectedMapEnabled(config.value)) {
    try {
      origin.value = await locateWithSelectedMap(config.value)
      locating.value = false
      return
    } catch {
      // Continue with browser geolocation as a fallback.
    }
  }
  navigator.geolocation.getCurrentPosition(async (position) => {
    try {
      origin.value = await api.reverse(position.coords.longitude, position.coords.latitude)
    } catch {
      origin.value = {
        name: '我的位置', address: '已通过浏览器定位',
        lng: position.coords.longitude, lat: position.coords.latitude,
      }
    } finally {
      locating.value = false
    }
  }, (cause) => {
    locating.value = false
    error.value = cause.code === 1 ? '定位权限未开启，请在浏览器中允许定位' : '暂时无法获取位置，请手动输入'
  }, { enableHighAccuracy: true, timeout: 10000 })
}

function swapPlaces() {
  const current = origin.value
  origin.value = destination.value
  destination.value = current
}

async function sharePayload(payload, successMessage) {
  try {
    if (navigator.share) {
      await navigator.share(payload)
      shareToast.value = successMessage
    } else {
      await navigator.clipboard.writeText(`${payload.title}\n${payload.text || ''}\n${payload.url || ''}`)
      shareToast.value = '地图信息已复制，请粘贴到 Tesla App'
    }
  } catch (cause) {
    if (cause.name !== 'AbortError') shareToast.value = '分享未完成，请重试'
  }
  if (shareToast.value) setTimeout(() => { shareToast.value = '' }, 3200)
}

async function shareTrip() {
  if (!plan.value) return
  const places = [...plan.value.stops, destination.value]
  const lines = places.map((place, index) => `${index + 1}. ${place.name} ${mapMarkerUrl(config.value, place)}`)
  await sharePayload({
    title: `${origin.value.name} → ${destination.value.name} 完整充电路线`,
    text: `电途完整路线 · ${plan.value.summary.planned_distance_km} km\n起点：${origin.value.name}\n途经与终点：\n${lines.join('\n')}`,
  }, '整段路线已分享')
}

watch(activeTab, async () => {
  await nextTick()
  panelScroll.value?.scrollTo({ top: 0, behavior: 'smooth' })
})

onMounted(async () => {
  try { config.value = await api.config() } catch { config.value = { map_enabled: false, map_provider: 'baidu', mode: 'demo' } }
})
</script>

<template>
  <main class="app-layout">
    <aside class="planner-panel">
      <header class="brand-header">
        <div class="brand-mark"><UiIcon name="route" /></div>
        <div>
          <h1>电途</h1>
          <p>EV ROUTE PLANNER</p>
        </div>
        <div class="header-actions">
          <button class="icon-button" type="button" title="帮助"><UiIcon name="help" /></button>
          <button class="avatar" type="button">G</button>
        </div>
      </header>

      <nav class="tabs" aria-label="主导航">
        <button :class="{ active: activeTab === 'plan' }" @click="activeTab = 'plan'">路线规划</button>
        <button :class="{ active: activeTab === 'detail' }" @click="activeTab = 'detail'" :disabled="!plan">行程详情</button>
      </nav>

      <div ref="panelScroll" class="panel-scroll">
        <template v-if="activeTab === 'plan'">
          <section class="route-form section-block">
            <div class="section-heading">
              <div><span class="eyebrow">01 / ROUTE</span><h2>从哪里出发？</h2></div>
              <button class="locate-button" type="button" @click="locate" :disabled="locating">
                <UiIcon class="locate-icon" name="locate" />{{ locating ? '定位中' : '定位' }}
              </button>
            </div>
            <div class="places-card">
              <PlaceInput v-model="origin" :config="config" placeholder="选择起点" marker="start" />
              <div class="place-divider" />
              <PlaceInput v-model="destination" :config="config" placeholder="选择目的地" marker="end" />
              <button class="swap-button" type="button" title="交换起终点" @click="swapPlaces"><UiIcon name="swap" /></button>
            </div>
          </section>

          <section class="section-block energy-section">
            <div class="section-heading">
              <div><span class="eyebrow">02 / ENERGY</span><h2>续航与充电</h2></div>
              <span class="energy-badge">按续航公里规划</span>
            </div>

            <div class="range-hero">
              <div class="range-ring">
                <svg viewBox="0 0 100 100">
                  <circle cx="50" cy="50" r="42" class="ring-track" />
                  <circle cx="50" cy="50" r="42" class="ring-value" :style="{ strokeDashoffset: 264 - 264 * Math.min(form.initial_range_km / form.full_range_km, 1) }" />
                </svg>
                <span>当前</span>
              </div>
              <label class="hero-input">
                <span>起始可用续航</span>
                <strong><input v-model.number="form.initial_range_km" type="number" min="20" max="1200" /> <em>km</em></strong>
              </label>
              <div class="battery-visual"><i :style="{ width: `${Math.min(100, form.initial_range_km / form.full_range_km * 100)}%` }" /></div>
            </div>

            <div class="metric-grid">
              <label class="metric-card reserve-card">
                <span class="metric-icon shield"><UiIcon name="shield" /></span>
                <span class="metric-copy"><small>基础安全余量</small><strong><input v-model.number="form.reserve_range_km" type="number" min="0" max="500" /> <em>km</em></strong></span>
                <span class="metric-percent">≈ {{ reservePercent }}%</span>
              </label>
              <label class="metric-card">
                <span class="metric-icon bolt"><UiIcon name="bolt" /></span>
                <span class="metric-copy"><small>每站建议充至</small><strong><input v-model.number="form.charge_to_percent" type="number" min="40" max="100" /> <em>%</em></strong></span>
                <span class="metric-percent">{{ rangeAfterCharge }} km</span>
              </label>
            </div>

            <label class="range-control">
              <span><small>车辆满电续航</small><strong><input v-model.number="form.full_range_km" aria-label="车辆满电续航" type="number" min="250" max="800" step="1" /> km</strong></span>
              <input v-model.number="form.full_range_km" aria-label="车辆满电续航滑杆" type="range" min="250" max="800" step="1" />
              <i class="range-scale"><span>250</span><span>800</span></i>
            </label>
          </section>

          <section class="section-block strategy-section">
            <div class="section-heading compact">
              <div><span class="eyebrow">03 / STRATEGY</span><h2>充电偏好</h2></div>
              <span class="detour-label">最远下高速 {{ form.max_detour_km }} km</span>
            </div>
            <div class="recommendation-mode" aria-label="推荐方式">
              <label v-for="item in recommendationModes" :key="item.value" :class="{ selected: form.recommendation_mode === item.value }">
                <input v-model="form.recommendation_mode" type="radio" :value="item.value" />
                <span><strong>{{ item.label }}</strong><small>{{ item.hint }}<template v-if="item.value === 'ai' && config?.ai_model"> · {{ config.ai_model }}</template></small></span>
              </label>
            </div>
            <div class="strategy-list">
              <label v-for="item in strategies" :key="item.value" :class="{ selected: form.strategy === item.value }">
                <input v-model="form.strategy" type="radio" :value="item.value" />
                <span class="radio-dot" />
                <span><strong>{{ item.label }}</strong><small>{{ item.hint }}</small></span>
                <TeslaIcon v-if="item.value === 'tesla_priority'" class="tesla-mark" />
              </label>
            </div>
            <label class="detour-slider">
              <input v-model.number="form.max_detour_km" type="range" min="3" max="30" step="1" />
              <span>3 km</span><span>30 km</span>
            </label>
          </section>

          <div v-if="error" class="error-banner"><UiIcon name="alert" />{{ error }}</div>
          <button class="plan-button" type="button" @click="submit" :disabled="loading">
            <span v-if="loading" class="button-spinner" />
            <UiIcon v-else class="button-arrow" name="arrow-up-right" />
            {{ loading ? (form.recommendation_mode === 'ai' ? 'AI 正在比较全部超充站…' : '正在计算最优充电路线…') : (form.recommendation_mode === 'ai' ? '开始 AI 推荐' : '开始传统算法规划') }}
          </button>
          <p class="key-note"><span :class="['status-dot', { live: config?.map_enabled }]" />{{ config?.map_enabled ? `${config.map_provider === 'baidu' ? '百度' : '高德'}地图已连接${config?.ai_enabled ? ' · GPT 智能推荐已启用' : ''}` : `演示模式 · 配置${config?.map_provider === 'baidu' ? '百度' : '高德'}地图 Key 后使用实时地图` }}</p>
        </template>

        <section v-else-if="plan" class="detail-view">
          <button class="back-link" @click="activeTab = 'plan'"><UiIcon name="arrow-left" />返回修改规划</button>
          <div class="detail-title"><span class="eyebrow">TRIP ITINERARY</span><h2>你的充电行程</h2><p>{{ origin.name }} → {{ destination.name }}</p><button class="share-trip-button" @click="shareTrip"><UiIcon name="share" />分享完整行程</button></div>
          <div class="summary-grid">
            <div><small>总里程</small><strong>{{ plan.summary.planned_distance_km }}</strong><span>km</span></div>
            <div><small>总用时</small><strong>{{ formatDuration(plan.summary.total_duration_min) }}</strong></div>
            <div><small>充电</small><strong>{{ plan.summary.charging_stops }}</strong><span>站</span></div>
            <div><small>到达续航</small><strong>{{ plan.summary.arrival_range_km }}</strong><span>km</span></div>
          </div>
          <div class="timeline">
            <article class="timeline-item origin-item">
              <i><UiIcon name="pin" /></i><div><small>出发</small><h3>{{ origin.name }}</h3><p>起始续航 {{ form.initial_range_km }} km</p></div>
            </article>
            <template v-for="(leg, index) in plan.legs" :key="leg.sequence">
              <div class="leg-row"><span>{{ leg.distance_km }} km</span><span>预计 {{ formatDuration(leg.duration_min) }}</span><span>到达 {{ leg.arrival_range_km }} km</span></div>
              <article v-if="plan.stops[index]" class="timeline-item charge-item">
                <i><TeslaIcon v-if="plan.stops[index].station_type === 'tesla_supercharger'" /><UiIcon v-else name="bolt" /></i>
                <div>
                  <small>{{ plan.stops[index].source?.includes('planning_gap') ? '补能区段 · 站点待确认' : plan.stops[index].station_type === 'tesla_supercharger' ? 'TESLA SUPERCHARGER' : plan.stops[index].station_type === 'service_area' ? '高速服务区充电' : '公共快充' }}</small>
                  <h3>{{ plan.stops[index].name }}</h3>
                  <p>{{ plan.stops[index].address }}</p>
                  <div v-if="plan.stops[index].station_type === 'tesla_supercharger'" class="highway-detour-card">
                    <div>
                      <b>从哪下高速</b>
                      <span>{{ plan.stops[index].exit_name || '最近高速出口待确认' }}</span>
                    </div>
                    <div>
                      <b>出口 → 超充</b>
                      <span v-if="plan.stops[index].exit_distance_km">{{ plan.stops[index].exit_distance_km }} km · 约 {{ plan.stops[index].exit_duration_min || '—' }} 分钟</span>
                      <span v-else>{{ mapProviderName }}暂未返回道路距离</span>
                    </div>
                    <div class="charge-detail-row">
                      <b>超充时间</b>
                      <span>到站 {{ plan.stops[index].arrival_range_km }} km（{{ plan.stops[index].charge_from_percent }}%） · 充至 {{ plan.stops[index].charge_to_percent }}% · 约 {{ plan.stops[index].charge_minutes }} 分钟</span>
                    </div>
                    <div>
                      <b>充电后返回高速</b>
                      <span v-if="plan.stops[index].entry_distance_km">{{ plan.stops[index].entry_name || '最近高速入口' }} · {{ plan.stops[index].entry_distance_km }} km · 约 {{ plan.stops[index].entry_duration_min || '—' }} 分钟</span>
                      <span v-else>高速入口与返程距离待{{ mapProviderName }}确认</span>
                    </div>
                  </div>
                  <div v-else class="charge-tags">
                    <span>{{ plan.stops[index].charge_from_percent }}% → {{ plan.stops[index].charge_to_percent }}%</span>
                    <span>充电约 {{ plan.stops[index].charge_minutes }} 分钟</span>
                    <span v-if="plan.stops[index].distance_to_route_km">绕行 {{ plan.stops[index].distance_to_route_km }} km</span>
                  </div>
                </div>
              </article>
            </template>
            <article class="timeline-item destination-item">
              <i><UiIcon name="flag" /></i><div><small>抵达</small><h3>{{ destination.name }}</h3><p>预计剩余 {{ plan.summary.arrival_range_km }} km</p></div>
            </article>
          </div>
          <section v-if="teslaAlternatives.length" class="candidate-section">
            <div class="candidate-heading"><h3>沿途 Tesla 超充（{{ teslaAlternatives.length }}）</h3><span>按高速往返最短距离排序</span></div>
            <article v-for="candidate in displayedTeslaAlternatives" :key="candidate.id" class="candidate-row">
              <span class="candidate-rank"><TeslaIcon /></span>
              <div><strong>{{ candidate.name }}</strong><small>路线约 {{ candidate.progress_km }} km 处 · {{ candidate.exit_name || '出口待确认' }}<template v-if="candidate.exit_duration_min || candidate.entry_duration_min"> · 往返约 {{ (candidate.exit_duration_min || 0) + (candidate.entry_duration_min || 0) }} 分钟</template></small></div>
              <b>{{ candidate.exit_distance_km ? `去 ${candidate.exit_distance_km} / 回 ${candidate.entry_distance_km || '—'} km` : '待确认' }}</b>
            </article>
            <button v-if="teslaAlternatives.length > 12" class="candidate-toggle" type="button" @click="showAllTesla = !showAllTesla">
              {{ showAllTesla ? '收起列表' : `查看全部 ${teslaAlternatives.length} 个超充站` }}
            </button>
          </section>
          <div class="notice-list"><p v-for="notice in plan.notices" :key="notice"><UiIcon name="info" />{{ notice }}</p></div>
        </section>
      </div>
    </aside>

    <section class="map-panel">
      <RouteMap :plan="plan" :origin="origin" :destination="destination" :config="config" />
      <div v-if="plan" class="trip-summary-card">
        <div class="trip-route-title">
          <div><span class="eyebrow">RECOMMENDED ROUTE</span><h3>{{ origin.name }} <b><UiIcon name="arrow-right" /></b> {{ destination.name }}</h3></div>
          <div class="trip-title-actions"><button class="quick-tesla-button" @click="shareTrip"><UiIcon name="share" />分享整段路线</button><button @click="activeTab = 'detail'">查看详情 <UiIcon name="arrow-right" /></button></div>
        </div>
        <div class="trip-metrics">
          <div><small>规划里程</small><strong>{{ plan.summary.planned_distance_km }}<span> km</span></strong><em v-if="plan.summary.planned_distance_km > plan.summary.direct_distance_km">含绕行 +{{ (plan.summary.planned_distance_km - plan.summary.direct_distance_km).toFixed(1) }} km</em></div>
          <div><small>预计总用时</small><strong>{{ formatDuration(plan.summary.total_duration_min) }}</strong><em>含充电 {{ plan.summary.charge_duration_min }} 分</em></div>
          <div><small>充电安排</small><strong>{{ plan.summary.charging_stops }}<span> 站</span></strong><em>{{ plan.summary.ai_assisted ? 'AI 推荐' : '传统算法' }} · 每站充至 {{ plan.summary.planned_charge_to_percent }}%<template v-if="plan.summary.planned_charge_to_percent > form.charge_to_percent"> · 已提高</template></em></div>
          <div><small>到达剩余</small><strong>{{ plan.summary.arrival_range_km }}<span> km</span></strong><em v-if="plan.summary.planned_reserve_km < plan.summary.requested_reserve_km">Tesla-only：下高速 {{ plan.summary.planned_max_detour_km }} km · 最低到站 {{ plan.summary.planned_reserve_km }} km</em><em v-else-if="plan.summary.planned_max_detour_km > form.max_detour_km">下高速范围自动放宽至 {{ plan.summary.planned_max_detour_km }} km</em><em v-else-if="plan.summary.planned_reserve_km > plan.summary.requested_reserve_km">到充电站：基础 {{ plan.summary.requested_reserve_km }} · 推荐 {{ plan.summary.planned_reserve_km }} km</em><em v-else>到充电站保留 {{ plan.summary.requested_reserve_km }} km</em></div>
        </div>
        <div v-if="plan.stops.length" class="next-stop">
          <span class="next-bolt"><TeslaIcon v-if="plan.stops[0].station_type === 'tesla_supercharger'" /><UiIcon v-else name="bolt" /></span>
          <div><small>第一充电站 · 行驶 {{ plan.legs[0].distance_km }} km 后</small><strong>{{ plan.stops[0].name }}</strong><small v-if="plan.stops[0].exit_name">从 {{ plan.stops[0].exit_name }} 下 · 去超充 {{ plan.stops[0].exit_distance_km }} km / {{ plan.stops[0].exit_duration_min || '—' }} 分 · 充后返回高速 {{ plan.stops[0].entry_distance_km || '—' }} km / {{ plan.stops[0].entry_duration_min || '—' }} 分</small></div>
          <div class="next-charge"><span>{{ plan.stops[0].charge_from_percent }}%</span><i /><span>{{ plan.stops[0].charge_to_percent }}%</span><small>约 {{ plan.stops[0].charge_minutes }} 分钟</small></div>
        </div>
        <div v-else class="next-stop direct">
          <span class="next-bolt"><UiIcon name="check" /></span><div><small>当前续航充足</small><strong>无需途中充电，可以直接抵达</strong></div>
        </div>
      </div>
      <transition name="toast"><div v-if="shareToast" class="share-toast"><span class="tesla-share-icon"><TeslaIcon /></span>{{ shareToast }}</div></transition>
    </section>
  </main>
</template>
