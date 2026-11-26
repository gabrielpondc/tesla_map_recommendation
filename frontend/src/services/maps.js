import {
  getAmapPlanningData,
  getAmapSuggestions,
  getDrivingRoute as getAmapDrivingRoute,
  locateWithAmap,
} from './amap'
import {
  getBaiduDrivingRoute,
  getBaiduPlanningData,
  getBaiduSuggestions,
  locateWithBaidu,
} from './baidu'

export function isSelectedMapEnabled(config) {
  return Boolean(config?.map_enabled)
}

function withPlanningTimeout(promise, provider) {
  const timeoutMs = provider === 'baidu' ? 55000 : 70000
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${provider === 'baidu' ? '百度' : '高德'}沿途充电站检索超时，请重新规划`)), timeoutMs)
    promise.then((value) => {
      clearTimeout(timer)
      resolve(value)
    }, (cause) => {
      clearTimeout(timer)
      reject(cause)
    })
  })
}

export function getMapPlanningData(config, origin, destination, maxDetourKm) {
  const provider = config?.map_provider === 'baidu' ? 'baidu' : 'amap'
  const planning = provider === 'baidu'
    ? getBaiduPlanningData(config, origin, destination, maxDetourKm)
    : getAmapPlanningData(config, origin, destination, maxDetourKm)
  return withPlanningTimeout(planning, provider)
}

export function getMapDrivingRoute(config, origin, destination, waypoints = []) {
  return config?.map_provider === 'baidu'
    ? getBaiduDrivingRoute(config, origin, destination, waypoints)
    : getAmapDrivingRoute(config, origin, destination, waypoints)
}

export function getMapSuggestions(config, keywords) {
  return config?.map_provider === 'baidu'
    ? getBaiduSuggestions(config, keywords)
    : getAmapSuggestions(config, keywords)
}

export function locateWithSelectedMap(config) {
  return config?.map_provider === 'baidu'
    ? locateWithBaidu(config)
    : locateWithAmap(config)
}

export function mapMarkerUrl(config, place) {
  const title = encodeURIComponent(place.name)
  if (config?.map_provider === 'baidu') {
    const content = encodeURIComponent(place.address || place.name)
    return `https://api.map.baidu.com/marker?location=${place.lat},${place.lng}&title=${title}&content=${content}&output=html&src=diantu.evplanner`
  }
  return `https://uri.amap.com/marker?position=${place.lng},${place.lat}&name=${title}&src=diantu.evplanner&coordinate=gaode&callnative=1`
}
