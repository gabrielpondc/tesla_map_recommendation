<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { loadAmap } from '../services/amap'
import { loadBaidu } from '../services/baidu'
import UiIcon from './UiIcon.vue'

const props = defineProps({
  plan: { type: Object, default: null },
  origin: { type: Object, required: true },
  destination: { type: Object, required: true },
  config: { type: Object, default: null },
})

const container = ref(null)
const ready = ref(false)
const mapFailed = ref(false)
let map
let overlays = []

const points = computed(() => props.plan?.route?.polyline || [props.origin, props.destination].filter((p) => p?.lng != null))
const svgPoints = computed(() => {
  const list = points.value
  if (list.length < 2) return ''
  const lngs = list.map((p) => p.lng)
  const lats = list.map((p) => p.lat)
  const minLng = Math.min(...lngs), maxLng = Math.max(...lngs)
  const minLat = Math.min(...lats), maxLat = Math.max(...lats)
  return list.map((point) => {
    const x = 8 + ((point.lng - minLng) / (maxLng - minLng || 1)) * 84
    const y = 88 - ((point.lat - minLat) / (maxLat - minLat || 1)) * 72
    return `${x},${y}`
  }).join(' ')
})

const fallbackMarkers = computed(() => {
  if (!props.plan) return []
  const list = points.value
  const lngs = list.map((p) => p.lng), lats = list.map((p) => p.lat)
  const minLng = Math.min(...lngs), maxLng = Math.max(...lngs)
  const minLat = Math.min(...lats), maxLat = Math.max(...lats)
  const normalize = (point) => ({
    x: 8 + ((point.lng - minLng) / (maxLng - minLng || 1)) * 84,
    y: 88 - ((point.lat - minLat) / (maxLat - minLat || 1)) * 72,
  })
  return [
    { ...normalize(props.origin), kind: 'origin', icon: 'pin' },
    ...props.plan.stops.map((stop) => ({ ...normalize(stop), kind: 'charge', icon: 'bolt' })),
    { ...normalize(props.destination), kind: 'destination', icon: 'flag' },
  ]
})

function markerIconSvg(kind) {
  const content = kind === 'charge'
    ? '<path d="m13 2-9 12h8l-1 8 9-12h-8l1-8z"/>'
    : kind === 'destination'
      ? '<path d="M5 22V4M5 5h10l-1 4 1 4H5"/>'
      : '<path d="M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1 1 16 0z"/><circle cx="12" cy="10" r="2.5"/>'
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="display:block;width:15px;height:15px">${content}</svg>`
}

function clearOverlays() {
  if (map && overlays.length) {
    if (props.config?.map_provider === 'baidu') overlays.forEach((overlay) => map.removeOverlay(overlay))
    else map.remove(overlays)
  }
  overlays = []
}

function draw() {
  if (!map || !props.plan) return
  clearOverlays()
  if (props.config?.map_provider === 'baidu') {
    const BMap = window.BMap
    const path = props.plan.route.polyline.map((point) => new BMap.Point(point.lng, point.lat))
    const line = new BMap.Polyline(path, { strokeColor: '#28a745', strokeWeight: 6, strokeOpacity: 0.95 })
    map.addOverlay(line)
    overlays.push(line)
    const markerData = [
      { point: props.plan.route.polyline[0], kind: 'origin', background: '#59645e' },
      ...props.plan.stops.map((stop) => ({ point: stop, kind: 'charge', background: '#218a48' })),
      { point: props.plan.route.polyline.at(-1), kind: 'destination', background: '#303833' },
    ]
    for (const item of markerData) {
      const label = new BMap.Label(markerIconSvg(item.kind), {
        position: new BMap.Point(item.point.lng, item.point.lat),
        offset: new BMap.Size(-15, -15),
      })
      label.setStyle({
        width: '30px', height: '30px', lineHeight: '30px', textAlign: 'center',
        border: '2px solid #fff', borderRadius: '16px', color: '#fff',
        background: item.background, boxShadow: '0 2px 7px rgba(31,50,38,.25)',
        fontSize: '10px', fontWeight: '700', boxSizing: 'border-box',
        display: 'grid', placeItems: 'center',
      })
      map.addOverlay(label)
      overlays.push(label)
    }
    map.setViewport([...path, ...markerData.map((item) => new BMap.Point(item.point.lng, item.point.lat))], {
      margins: window.matchMedia('(max-width: 900px)').matches ? [55, 35, 55, 35] : [70, 70, 250, 70],
    })
    return
  }
  const AMap = window.AMap
  const path = props.plan.route.polyline.map((point) => [point.lng, point.lat])
  const line = new AMap.Polyline({ path, strokeColor: '#28a745', strokeWeight: 6, strokeOpacity: 0.95, lineJoin: 'round' })
  overlays.push(line)
  const markerData = [
    { point: props.origin, kind: 'origin', className: 'start' },
    ...props.plan.stops.map((stop) => ({ point: stop, kind: 'charge', className: 'charge' })),
    { point: props.destination, kind: 'destination', className: 'end' },
  ]
  markerData.forEach((item) => {
    const marker = new AMap.Marker({
      position: [item.point.lng, item.point.lat],
      anchor: 'center',
      content: `<div class="amap-route-marker ${item.className}">${markerIconSvg(item.kind)}</div>`,
      zIndex: 120,
    })
    overlays.push(marker)
  })
  map.add(overlays)
  const mobile = window.matchMedia('(max-width: 900px)').matches
  map.setFitView(overlays, false, mobile ? [55, 35, 55, 35] : [70, 70, 250, 70], 12)
}

async function initMap() {
  if (map || !container.value) return
  mapFailed.value = false
  try {
    if (props.config?.map_provider === 'baidu') {
      const BMap = await loadBaidu(props.config)
      map = new BMap.Map(container.value)
      const hasOrigin = Number.isFinite(Number(props.origin.lng)) && Number.isFinite(Number(props.origin.lat))
      if (hasOrigin) map.centerAndZoom(new BMap.Point(props.origin.lng, props.origin.lat), 7)
      else map.centerAndZoom('中国', 5)
      map.enableScrollWheelZoom(true)
      map.addControl(new BMap.ScaleControl())
      map.addControl(new BMap.NavigationControl({ anchor: window.BMAP_ANCHOR_BOTTOM_RIGHT }))
      ready.value = true
      draw()
      return
    }
    const AMap = await loadAmap(props.config)
    if (!AMap) return
    map = new AMap.Map(container.value, {
      zoom: 7,
      viewMode: '2D',
      features: ['bg', 'road', 'building', 'point'],
    })
    map.addControl(new AMap.Scale())
    const mobile = window.matchMedia('(max-width: 900px)').matches
    map.addControl(new AMap.ToolBar({ position: { right: '16px', bottom: mobile ? '20px' : '260px' } }))
    ready.value = true
    draw()
  } catch {
    mapFailed.value = true
  }
}

onMounted(initMap)
watch(() => props.config?.map_enabled, (enabled) => { if (enabled) initMap() })
watch(() => props.plan, draw, { deep: true })
onBeforeUnmount(() => { if (map?.destroy) map.destroy(); else map?.clearOverlays?.() })
</script>

<template>
  <div class="map-shell">
    <div ref="container" class="amap-container" :class="{ visible: ready && !mapFailed }" />
    <div v-if="!ready || mapFailed" class="fallback-map">
      <div class="fallback-grid" />
      <div class="map-region region-one">江苏</div>
      <div class="map-region region-two">浙江</div>
      <div class="map-region region-three">上海</div>
      <svg v-if="plan && svgPoints" viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="路线示意图">
        <polyline :points="svgPoints" fill="none" stroke="#07110e" stroke-width="3.8" opacity=".55" />
        <polyline :points="svgPoints" fill="none" stroke="#218a48" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
      <div
        v-for="(marker, index) in fallbackMarkers"
        :key="`${marker.kind}-${index}`"
        class="fallback-marker"
        :class="marker.kind"
        :style="{ left: `${marker.x}%`, top: `${marker.y}%` }"
      ><UiIcon :name="marker.icon" /></div>
      <div class="demo-watermark">
        <span>{{ plan ? '路线预览' : '正在准备地图' }}</span>
        <small>{{ config?.map_enabled ? `等待${config?.map_provider === 'baidu' ? '百度' : '高德'}道路数据` : `配置${config?.map_provider === 'baidu' ? '百度' : '高德'}地图 Key 后显示真实地图` }}</small>
      </div>
    </div>

    <div class="map-topbar">
      <div class="status-chip"><span class="status-dot" />{{ !plan ? '等待规划' : plan.mode === 'baidu' ? '百度地图实时路线' : plan.mode === 'amap' ? '高德地图实时路线' : '演示路线' }}</div>
      <div v-if="plan" class="route-chip">{{ plan.summary.planned_distance_km }} km · {{ plan.summary.charging_stops }} 次充电</div>
    </div>

    <div class="map-legend">
      <span><i class="legend-line" />规划路线</span>
      <span><UiIcon class="legend-icon charge" name="bolt" />充电点</span>
      <span><UiIcon class="legend-icon destination" name="flag" />目的地</span>
    </div>
  </div>
</template>
