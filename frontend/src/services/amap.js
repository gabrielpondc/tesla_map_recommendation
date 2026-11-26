import { getServerCandidates } from './serverCandidates'

let sdkPromise
const exitDistanceCache = new Map()

export async function loadAmap(config) {
  if (!config?.amap_enabled || !config?.amap_js_key) throw new Error('高德地图 Key 未配置')
  if (window.AMap) return window.AMap
  if (!sdkPromise) {
    if (config.amap_security_code) {
      window._AMapSecurityConfig = { securityJsCode: config.amap_security_code }
    }
    sdkPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script')
      script.src = `https://webapi.amap.com/maps?v=2.0&key=${config.amap_js_key}&plugin=AMap.Scale,AMap.ToolBar,AMap.Driving,AMap.PlaceSearch,AMap.AutoComplete,AMap.Geolocation`
      script.onload = () => resolve(window.AMap)
      script.onerror = () => reject(new Error('高德地图加载失败'))
      document.head.appendChild(script)
    })
  }
  return sdkPromise
}

function ensurePlugins(AMap, names) {
  return new Promise((resolve) => AMap.plugin(names, resolve))
}

function toPoint(raw) {
  if (!raw) return null
  const lng = typeof raw.getLng === 'function' ? raw.getLng() : raw.lng
  const lat = typeof raw.getLat === 'function' ? raw.getLat() : raw.lat
  return Number.isFinite(Number(lng)) && Number.isFinite(Number(lat))
    ? { lng: Number(lng), lat: Number(lat) }
    : null
}

function haversineKm(a, b) {
  const radius = 6371.0088
  const lat1 = a.lat * Math.PI / 180
  const lat2 = b.lat * Math.PI / 180
  const dLat = lat2 - lat1
  const dLng = (b.lng - a.lng) * Math.PI / 180
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2
  return 2 * radius * Math.asin(Math.sqrt(h))
}

function nearestRouteDistance(point, polyline) {
  let best = Infinity
  const stride = Math.max(1, Math.floor(polyline.length / 180))
  for (let index = 0; index < polyline.length; index += stride) {
    best = Math.min(best, haversineKm(point, polyline[index]))
  }
  return best
}

function isTeslaSuperchargerName(name) {
  return /特斯拉.*超级充电|tesla.*supercharger/i.test(name || '')
}

function baiduToGcj(point) {
  const x = point.lng - 0.0065
  const y = point.lat - 0.006
  const z = Math.sqrt(x * x + y * y) - 0.00002 * Math.sin(y * Math.PI)
  const theta = Math.atan2(y, x) - 0.000003 * Math.cos(x * Math.PI)
  return { lng: z * Math.cos(theta), lat: z * Math.sin(theta) }
}

function gcjToBaidu(point) {
  const z = Math.sqrt(point.lng * point.lng + point.lat * point.lat)
    + 0.00002 * Math.sin(point.lat * Math.PI)
  const theta = Math.atan2(point.lat, point.lng) + 0.000003 * Math.cos(point.lng * Math.PI)
  return { lng: z * Math.cos(theta) + 0.0065, lat: z * Math.sin(theta) + 0.006 }
}

export async function getDrivingRoute(config, origin, destination, waypoints = []) {
  const AMap = await loadAmap(config)
  await ensurePlugins(AMap, ['AMap.Driving'])
  const result = await new Promise((resolve, reject) => {
    const driving = new AMap.Driving({ policy: AMap.DrivingPolicy.LEAST_TIME, ferry: 0 })
    const start = new AMap.LngLat(origin.lng, origin.lat)
    const end = new AMap.LngLat(destination.lng, destination.lat)
    const callback = (status, payload) => status === 'complete'
      ? resolve(payload)
      : reject(new Error(payload?.info || '高德未返回驾车路线'))
    if (waypoints.length) {
      driving.search(start, end, {
        waypoints: waypoints.map((point) => new AMap.LngLat(point.lng, point.lat)),
      }, callback)
    } else {
      driving.search(start, end, callback)
    }
  })
  const route = result.routes?.[0]
  if (!route) throw new Error('高德未返回可用的驾车路线')
  const rawPath = []
  for (const step of route.steps || []) {
    for (const raw of step.path || []) {
      const point = toPoint(raw)
      if (point && (!rawPath.length || point.lng !== rawPath.at(-1).lng || point.lat !== rawPath.at(-1).lat)) rawPath.push(point)
    }
  }
  if (rawPath.length < 2) throw new Error('高德道路折线为空')
  const polyline = rawPath.filter((_, index) => index === 0 || index === rawPath.length - 1 || index % 2 === 0)
  return {
    distance_km: Math.round(Number(route.distance) / 100) / 10,
    duration_min: Math.max(1, Math.round(Number(route.time) / 60)),
    tolls_yuan: Math.round(Number(route.tolls || 0) * 10) / 10,
    polyline,
  }
}

async function searchNearbyPage(AMap, keyword, point, radiusMeters, pageIndex) {
  await ensurePlugins(AMap, ['AMap.PlaceSearch'])
  return new Promise((resolve) => {
    const service = new AMap.PlaceSearch({ pageSize: 50, pageIndex, citylimit: false, autoFitView: false })
    service.searchNearBy(keyword, new AMap.LngLat(point.lng, point.lat), radiusMeters, (status, result) => {
      resolve(status === 'complete'
        ? { pois: result.poiList?.pois || [], count: Number(result.poiList?.count || 0) }
        : { pois: [], count: 0 })
    })
  })
}

async function searchNearby(AMap, keyword, point, radiusMeters) {
  const first = await searchNearbyPage(AMap, keyword, point, radiusMeters, 1)
  const pages = Math.ceil(first.count / 50)
  if (pages <= 1) return first.pois
  const rest = await Promise.all(Array.from({ length: pages - 1 }, (_, index) =>
    searchNearbyPage(AMap, keyword, point, radiusMeters, index + 2),
  ))
  return [...first.pois, ...rest.flatMap((page) => page.pois)]
}

async function mapWithConcurrency(items, concurrency, worker) {
  const results = new Array(items.length)
  let cursor = 0
  const runners = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
    while (cursor < items.length) {
      const index = cursor
      cursor += 1
      results[index] = await worker(items[index], index)
    }
  })
  await Promise.all(runners)
  return results
}

export async function getAmapCandidatesForBaiduRoute(config, route, maxDetourKm) {
  const AMap = await loadAmap(config)
  const gcjPolyline = route.polyline.map(baiduToGcj)
  const sampleCount = Math.min(24, Math.max(4, Math.ceil(route.distance_km / 80)))
  const indices = [...new Set(Array.from({ length: sampleCount + 1 }, (_, index) =>
    Math.round(index * (gcjPolyline.length - 1) / sampleCount),
  ))]
  const spacingKm = route.distance_km / sampleCount
  const searchRadius = Math.min(50000, Math.max(
    30000,
    (spacingKm * 0.65 + Math.max(maxDetourKm, 20)) * 1000,
  ))
  const tasks = []
  for (const index of indices) {
    const point = gcjPolyline[index]
    tasks.push({ point, keyword: '充电站', type: 'charging_station' })
    tasks.push({ point, keyword: '特斯拉超级充电站', type: 'tesla_supercharger' })
    tasks.push({ point, keyword: '高速服务区 充电站', type: 'service_area' })
  }
  const groups = await mapWithConcurrency(tasks, 12, async (task) => ({
    pois: await searchNearby(AMap, task.keyword, task.point, searchRadius),
    type: task.type,
  }))
  const seen = new Set()
  const candidates = []
  for (const group of groups) {
    for (const poi of group.pois) {
      const point = toPoint(poi.location)
      if (!point) continue
      const name = poi.name || '充电站'
      const id = `amap-fallback-${String(poi.id || `${name}-${point.lng}-${point.lat}`)}`
      if (seen.has(id)) continue
      seen.add(id)
      let stationType = 'charging_station'
      if (isTeslaSuperchargerName(name)) stationType = 'tesla_supercharger'
      else if (name.includes('服务区')) stationType = 'service_area'
      candidates.push({
        id,
        name,
        address: typeof poi.address === 'string' ? poi.address : '',
        ...point,
        station_type: stationType,
        progress_km: 0,
        distance_to_route_km: 0,
        source: 'amap_js',
      })
    }
  }
  const corridor = candidates.map((candidate) => ({
    ...candidate,
    _routeDistance: nearestRouteDistance(candidate, gcjPolyline),
  })).filter((candidate) => candidate._routeDistance <= 50)
  const tesla = corridor.filter((candidate) => candidate.station_type === 'tesla_supercharger')
  const other = corridor.filter((candidate) => candidate.station_type !== 'tesla_supercharger')
  const enrichedTesla = await mapWithConcurrency(tesla, 6, (candidate) =>
    enrichExitDistance(config, AMap, candidate, Math.max(maxDetourKm, 30)))
  return [...other, ...enrichedTesla].map(({ _routeDistance, ...candidate }) => ({
    ...candidate,
    ...gcjToBaidu(candidate),
    source: 'baidu_amap_fallback_js',
  }))
}

async function enrichExitDistance(config, AMap, candidate, maxDetourKm) {
  const exitSearchRadiusKm = Math.max(30, maxDetourKm)
  const cacheKey = `roundtrip:${candidate.id}:${exitSearchRadiusKm}`
  if (exitDistanceCache.has(cacheKey)) return { ...candidate, ...exitDistanceCache.get(cacheKey) }
  const [tollStations, exits, entrances] = await Promise.all([
    searchNearby(AMap, '收费站', candidate, exitSearchRadiusKm * 1000),
    searchNearby(AMap, '高速公路出口', candidate, exitSearchRadiusKm * 1000),
    searchNearby(AMap, '高速入口', candidate, exitSearchRadiusKm * 1000),
  ])
  function uniqueRoadPoints(pois, pattern, fallbackName) {
    const unique = new Map()
    for (const poi of pois) {
      const point = toPoint(poi.location)
      if (!point || !pattern.test(poi.name || '')) continue
      const id = String(poi.id || `${poi.name}-${point.lng}-${point.lat}`)
      unique.set(id, { id, name: poi.name || fallbackName, ...point })
    }
    return [...unique.values()]
  }
  const exitPoints = uniqueRoadPoints(
    [...tollStations, ...exits], /收费站|高速.*出口|出口/i, '最近高速出口',
  )
  const entryPoints = uniqueRoadPoints(
    [...tollStations, ...entrances], /收费站|高速.*入口|入口/i, '最近高速入口',
  )
  const nearestExits = (exitPoints.length ? exitPoints : entryPoints)
    .sort((a, b) => haversineKm(a, candidate) - haversineKm(b, candidate))
    .slice(0, 3)
  const nearestEntries = (entryPoints.length ? entryPoints : exitPoints)
    .sort((a, b) => haversineKm(a, candidate) - haversineKm(b, candidate))
    .slice(0, 3)
  if (!nearestExits.length && !nearestEntries.length) {
    exitDistanceCache.set(cacheKey, {})
    return candidate
  }

  const [exitRouteResults, entryRouteResults] = await Promise.all([
    Promise.allSettled(nearestExits.map(async (exit) => ({
      roadPoint: exit,
      route: await getDrivingRoute(config, exit, candidate),
    }))),
    Promise.allSettled(nearestEntries.map(async (entry) => ({
      roadPoint: entry,
      route: await getDrivingRoute(config, candidate, entry),
    }))),
  ])
  const validExits = exitRouteResults
    .filter((item) => item.status === 'fulfilled')
    .map((item) => item.value)
    .sort((a, b) => a.route.distance_km - b.route.distance_km)
  const validEntries = entryRouteResults
    .filter((item) => item.status === 'fulfilled')
    .map((item) => item.value)
    .sort((a, b) => a.route.distance_km - b.route.distance_km)
  if (!validExits.length && !validEntries.length) {
    exitDistanceCache.set(cacheKey, {})
    return candidate
  }
  let bestExit = validExits[0]
  let bestEntry = validEntries[0]
  // Some POIs are classified only as an exit even though the same toll plaza
  // is also the practical return point. Try the reverse direction explicitly
  // before leaving the entry fields empty.
  if (bestExit && !bestEntry) {
    try {
      bestEntry = {
        roadPoint: bestExit.roadPoint,
        route: await getDrivingRoute(config, candidate, bestExit.roadPoint),
      }
    } catch {
      // Keep the confirmed exit data even if AMap cannot route back to it.
    }
  }
  if (bestEntry && !bestExit) {
    try {
      bestExit = {
        roadPoint: bestEntry.roadPoint,
        route: await getDrivingRoute(config, bestEntry.roadPoint, candidate),
      }
    } catch {
      // Keep the confirmed entry data even if AMap cannot route from it.
    }
  }
  const roundTripData = {
    ...(bestExit ? {
      exit_name: bestExit.roadPoint.name,
      exit_distance_km: bestExit.route.distance_km,
      exit_duration_min: bestExit.route.duration_min,
      distance_to_route_km: bestExit.route.distance_km,
    } : {}),
    ...(bestEntry ? {
      entry_name: bestEntry.roadPoint.name,
      entry_distance_km: bestEntry.route.distance_km,
      entry_duration_min: bestEntry.route.duration_min,
    } : {}),
  }
  exitDistanceCache.set(cacheKey, roundTripData)
  return { ...candidate, ...roundTripData }
}

export async function getAmapPlanningData(config, origin, destination, maxDetourKm) {
  const AMap = await loadAmap(config)
  const route = await getDrivingRoute(config, origin, destination)
  const serverCandidates = await getServerCandidates(config, route, maxDetourKm, 'amap')
  if (serverCandidates.length) return { route, candidates: serverCandidates }
  const sampleCount = Math.min(80, Math.max(3, Math.ceil(route.distance_km / 40)))
  const indices = [...new Set(Array.from({ length: sampleCount + 1 }, (_, index) =>
    Math.round(index * (route.polyline.length - 1) / sampleCount),
  ))]
  const tasks = []
  for (const index of indices) {
    const point = route.polyline[index]
    const searchRadius = Math.max(30000, (maxDetourKm + 8) * 1000)
    tasks.push({ point, searchRadius, keyword: '高速服务区 充电站', type: 'service_area' })
    tasks.push({ point, searchRadius, keyword: '特斯拉超级充电站', type: 'tesla_supercharger' })
    tasks.push({ point, searchRadius, keyword: '充电站', type: 'charging_station' })
  }
  const groups = await mapWithConcurrency(tasks, 6, async (task) => ({
    pois: await searchNearby(AMap, task.keyword, task.point, task.searchRadius),
    type: task.type,
  }))
  const seen = new Set()
  const candidates = []
  for (const group of groups) {
    for (const poi of group.pois) {
      const point = toPoint(poi.location)
      if (!point) continue
      const name = poi.name || '充电站'
      const id = String(poi.id || `${name}-${point.lng}-${point.lat}`)
      if (seen.has(id)) continue
      seen.add(id)
      let stationType = 'charging_station'
      if (isTeslaSuperchargerName(name)) stationType = 'tesla_supercharger'
      else if (name.includes('服务区')) stationType = 'service_area'
      candidates.push({
        id,
        name,
        address: typeof poi.address === 'string' ? poi.address : '',
        ...point,
        station_type: stationType,
        progress_km: 0,
        distance_to_route_km: 0,
        source: 'amap_js',
      })
    }
  }
  const corridorCandidates = candidates
    .map((candidate) => ({ ...candidate, _routeDistance: nearestRouteDistance(candidate, route.polyline) }))
  const serviceCandidates = corridorCandidates
    .filter((candidate) => candidate.station_type === 'service_area')
    .sort((a, b) => a._routeDistance - b._routeDistance)
    .filter((candidate) => candidate._routeDistance <= 30)
  const generalCandidates = corridorCandidates
    .filter((candidate) => candidate.station_type === 'charging_station')
    .sort((a, b) => a._routeDistance - b._routeDistance)
    .filter((candidate) => candidate._routeDistance <= 30)
  const teslaCandidates = corridorCandidates
    .filter((candidate) => candidate.station_type === 'tesla_supercharger')
    .sort((a, b) => a._routeDistance - b._routeDistance)
  const enrichedTesla = await mapWithConcurrency(teslaCandidates, 4, (candidate) =>
    enrichExitDistance(config, AMap, candidate, maxDetourKm))
  return {
    route,
    candidates: [...serviceCandidates, ...generalCandidates, ...enrichedTesla].map(({ _routeDistance, ...candidate }) => candidate),
  }
}

export async function getAmapSuggestions(config, keywords) {
  const AMap = await loadAmap(config)
  await ensurePlugins(AMap, ['AMap.AutoComplete'])
  const result = await new Promise((resolve) => {
    const service = new AMap.AutoComplete({ city: '全国', citylimit: false })
    service.search(keywords, (status, payload) => resolve(status === 'complete' ? payload : null))
  })
  return (result?.tips || []).map((tip) => {
    const point = toPoint(tip.location)
    return point ? {
      name: tip.name,
      address: typeof tip.address === 'string' ? tip.address : '',
      district: tip.district || '',
      ...point,
    } : null
  }).filter(Boolean).slice(0, 8)
}

export async function locateWithAmap(config) {
  const AMap = await loadAmap(config)
  await ensurePlugins(AMap, ['AMap.Geolocation'])
  const result = await new Promise((resolve, reject) => {
    const service = new AMap.Geolocation({ enableHighAccuracy: true, timeout: 10000 })
    service.getCurrentPosition((status, payload) => status === 'complete' ? resolve(payload) : reject(new Error(payload?.message || '定位失败')))
  })
  const point = toPoint(result.position)
  if (!point) throw new Error('定位结果无坐标')
  return {
    name: result.formattedAddress || '我的位置',
    address: result.addressComponent?.district || '高德定位',
    ...point,
  }
}
